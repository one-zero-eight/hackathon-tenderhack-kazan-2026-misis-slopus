import assert from "node:assert/strict";
import { after, before, mock, test } from "node:test";
import { existsSync, mkdtempSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { gzipSync } from "node:zlib";
import Database from "better-sqlite3";
import pl from "nodejs-polars";
import { getDirectoryRaw, getSpgzSpecialization } from "../src/lib/server/classifiers";
import { getKtruNodeByCode, getKtruRecord, getKtruSpecialization, ktruStats } from "../src/lib/server/ktru-tree";

const directory = mkdtempSync(join(tmpdir(), "classifier-cache-"));
const environment = { ...process.env };
const template = "25.73.30.141-00000011";
const childCodes = ["25.73.30.141-00000012", "25.73.30.141-00000013"];
const spgzRows = [1, 2, 3].map(id => ({
  id, name: `Позиция ${id}`, kpgzId: id === 3 ? 20 : 10,
  okpd2Code: "25.73.30.141", okpdCode: "25.73.30.141", ktruId: id, isExcluded: 0,
  spgzCharacteristics: [{ characteristicName: "Материал", value1: id === 1 ? "Сталь" : "Медь", value2: "", kpgzCharacteristicId: 7 }],
  okeis: [], descriptions: `Описание ${id}`,
  auctionProduct: 0, isStandartProduct: 0, innovationProduct: 0, niokr: 0, applyLifecycle: 0,
  allowLimited: 0, allowTwoStage: 0, allowCall: 0, regulationRequired: 0, art: 0, project: 0,
}));
let parquetReads = 0;
let payloadQueries = 0;

function writeParquet(path: string, rows: unknown[]) {
  pl.readJSON(Buffer.from(JSON.stringify(rows))).writeParquet(path);
}

before(() => {
  process.env.SPGZ_PARQUET = join(directory, "spgz.parquet");
  process.env.CLASSIFIERS_SQLITE = join(directory, "classifiers.sqlite");
  process.env.KTRU_PARQUET = join(directory, "ktru.parquet");
  process.env.KTRU_SQLITE = join(directory, "ktru.sqlite");
  writeParquet(process.env.SPGZ_PARQUET, spgzRows);
  writeParquet(process.env.KTRU_PARQUET, [{ code: template }]);

  const classifiers = new Database(process.env.CLASSIFIERS_SQLITE);
  classifiers.exec(`
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE okpd(id INTEGER, code TEXT, name TEXT, parent_id INTEGER);
    CREATE TABLE kpgz(code TEXT, external_id INTEGER);
    CREATE TABLE spgz(id INTEGER, kpgz_external_id INTEGER, is_excluded INTEGER);
    INSERT INTO kpgz VALUES ('01', 10), ('02', 20);
    INSERT INTO spgz VALUES (1, 10, 0), (2, 10, 0), (3, 20, 0);
  `);
  const sources = ["okpd2-moy-zakupki.parquet", "pp-kpgz.parquet", "spgz.parquet", "okei.parquet"]
    .map(name => resolve(import.meta.dirname, "../../data", name));
  const mtime = String(Math.trunc(Math.max(...sources.filter(existsSync).map(path => statSync(path).mtimeMs))));
  classifiers.prepare("INSERT INTO meta VALUES (?, ?)").run("source_mtime_ms", mtime);
  classifiers.prepare("INSERT INTO meta VALUES (?, ?)").run("schema", "classifiers-slim-v2");
  classifiers.close();

  const ktru = new Database(process.env.KTRU_SQLITE);
  ktru.exec(`
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE ktru(code TEXT PRIMARY KEY, name TEXT, status TEXT, okpd2 TEXT, okpd2_name TEXT, parent_code TEXT, is_template INTEGER, payload BLOB);
  `);
  ktru.prepare("INSERT INTO meta VALUES (?, ?)").run("source_mtime_ms", String(Math.trunc(statSync(process.env.KTRU_PARQUET).mtimeMs)));
  for (const [index, code] of [template, ...childCodes].entries()) {
    const raw = {
      code, name: index ? `Вариант ${index}` : "Шаблон", status: "ACTIVE", is_template: index === 0,
      okpd2: { code: "25.73.30.141", name: "Молотки" },
      characteristics: [{ code: "material", name: "Материал", required: true, type: "1", kind: "1", values: [{ text: index === 2 ? "Медь" : "Сталь" }] }],
      nsi: { standards: [{ document_name: "ГОСТ 123" }] },
    };
    ktru.prepare("INSERT INTO ktru VALUES (?, ?, ?, ?, ?, ?, ?, ?)")
      .run(code, raw.name, "ACTIVE", raw.okpd2.code, raw.okpd2.name, index ? template : null, index === 0 ? 1 : 0, gzipSync(JSON.stringify(raw)));
  }
  ktru.close();

  const scanParquet = pl.scanParquet;
  mock.method(pl, "scanParquet", (...args: Parameters<typeof pl.scanParquet>) => {
    parquetReads += 1;
    assert.deepEqual(args[1], { cache: false, lowMemory: true, parallel: "none" });
    return scanParquet(...args);
  });
  mock.method(pl, "readParquet", () => {
    assert.fail("Lookups must not load a whole Parquet file into memory");
  });
  const prepare = Database.prototype.prepare;
  mock.method(Database.prototype, "prepare", function (this: Database.Database, sql: string) {
    if (sql === "SELECT payload FROM ktru WHERE code=?") payloadQueries += 1;
    return prepare.call(this, sql);
  });
});

after(() => {
  mock.restoreAll();
  for (const key of ["SPGZ_PARQUET", "CLASSIFIERS_SQLITE", "KTRU_PARQUET", "KTRU_SQLITE"]) {
    if (environment[key] === undefined) delete process.env[key];
    else process.env[key] = environment[key];
  }
  rmSync(directory, { recursive: true });
});

test("SPGZ raw rows and sibling lookups share a lazy scan without loading the whole file", () => {
  assert.deepEqual(getDirectoryRaw("spgz", "1"), spgzRows[0]);
  assert.deepEqual(getDirectoryRaw("spgz", "2"), spgzRows[1]);
  const specialization = getSpgzSpecialization({ spgzCode: "1" });
  assert.deepEqual(specialization, {
    characteristicName: "Материал", characteristicCode: "7",
    options: [
      { value: "Медь", code: "2", name: "Позиция 2" },
      { value: "Сталь", code: "1", name: "Позиция 1" },
    ],
    currentValue: "Сталь", kpgzCode: "01",
  });
  assert.equal(getSpgzSpecialization({ kpgzCode: "02" }), null);
  assert.equal(getDirectoryRaw("spgz", "999"), null);
  assert.equal(getDirectoryRaw("spgz", "invalid"), null);
  assert.equal(parquetReads, 1);
});

test("KTRU record cache avoids payload reads and returns independent nested values", () => {
  const first = getKtruRecord(template)!;
  const expected = structuredClone(first);
  const queries = payloadQueries;
  assert.equal(first.name, "Шаблон");
  assert.equal(first.characteristics[0].values, "Сталь");
  first.characteristics[0].options.push("Изменено");
  first.standards.push("Изменено");
  assert.deepEqual(getKtruRecord(template), expected);
  assert.equal(payloadQueries, queries);
  assert.equal(getKtruRecord(""), null);
  assert.equal(getKtruRecord("missing"), null);
  assert.deepEqual(ktruStats(), { total: 3, roots: 1 });
});

test("KTRU specialization is cached by template without losing each child's current value", () => {
  const first = getKtruSpecialization(childCodes[0])!;
  assert.equal(first.currentValue, "Сталь");
  const queries = payloadQueries;
  first.options[0].value = "Изменено";
  const second = getKtruSpecialization(childCodes[1])!;
  assert.equal(second.currentValue, "Медь");
  assert.deepEqual(second.options.map(option => option.value), ["Медь", "Сталь"]);
  assert.equal(getKtruSpecialization(template)?.currentValue, null);
  assert.equal(getKtruNodeByCode(childCodes[0])?.variantLabel, "Материал: Сталь");
  assert.equal(payloadQueries, queries);
  assert.equal(getKtruSpecialization("missing"), null);
});
