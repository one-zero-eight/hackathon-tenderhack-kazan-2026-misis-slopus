import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { mkdtempSync, rmSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { gzipSync } from "node:zlib";
import Database from "better-sqlite3";
import { getKtruRecord } from "../src/lib/server/ktru-tree";
import { getDirectoryRecord } from "../src/lib/server/classifiers";
import { buildKtruDraftFields } from "../src/lib/ktru-characteristics";
import { draftFromSpgzCharacteristic } from "../src/lib/spgz-characteristics";
import { autofillCharacteristicFields, buildDisplayCharacteristics, hasNewAutofillFields } from "../src/lib/merge-characteristics";
import { draftFromCharacteristicSchema, schemaFromCharacteristic } from "../src/lib/characteristic-editor";
import { apiAutofillCharacteristics } from "../src/lib/api";
import { characteristicEditKey } from "../src/lib/create-product-state";

const directory = mkdtempSync(join(tmpdir(), "okei-propagation-"));
const environment = { ...process.env };
const code = "01-00000001";
const environmentKeys = ["KTRU_SQLITE", "CLASSIFIERS_SQLITE", "KTRU_PARQUET", "MOY_KTRU_PARQUET", "OKPD2_PARQUET", "KPGZ_PARQUET", "SPGZ_PARQUET", "OKEI_PARQUET"];
before(() => {
  for (const [key, file] of [["KTRU_SQLITE", "ktru.sqlite"], ["CLASSIFIERS_SQLITE", "classifiers.sqlite"], ["KTRU_PARQUET", "ktru.parquet"], ["MOY_KTRU_PARQUET", "missing.parquet"]]) {
    process.env[key] = join(directory, file);
  }
  writeFileSync(process.env.KTRU_PARQUET!, "fixture-not-parquet");
  const classifiers = new Database(process.env.CLASSIFIERS_SQLITE!);
  classifiers.exec(`
    CREATE TABLE okei(code TEXT, name TEXT, kind TEXT);
    CREATE TABLE okpd(id INTEGER, code TEXT, name TEXT, parent_id INTEGER);
    CREATE TABLE spgz(id INTEGER, name TEXT, kpgz_external_id INTEGER, okpd2_code TEXT, ktru_id INTEGER, is_excluded INTEGER);
    CREATE TABLE raw_records(tab TEXT, code TEXT, payload BLOB);
    CREATE TABLE meta(key TEXT, value TEXT);
    INSERT INTO spgz VALUES (1, 'Монитор', NULL, NULL, NULL, 0);
  `);
  const sources = [["okpd", "OKPD2_PARQUET"], ["kpgz", "KPGZ_PARQUET"], ["spgz", "SPGZ_PARQUET"], ["okei", "OKEI_PARQUET"]];
  const classifierMeta = classifiers.prepare("INSERT INTO meta VALUES (?, ?)");
  for (const [name, key] of sources) {
    process.env[key] = join(directory, `${name}.parquet`);
    writeFileSync(process.env[key]!, "fixture-not-parquet");
    classifierMeta.run(name, process.env[key]);
    classifierMeta.run(`${name}_mtime_ms`, String(Math.trunc(statSync(process.env[key]!).mtimeMs)));
  }
  classifierMeta.run("source_mtime_ms", String(Math.trunc(Math.max(...sources.map(([, key]) => statSync(process.env[key]!).mtimeMs)))));
  classifierMeta.run("schema", "classifiers-full-v4");
  classifierMeta.run("lexical_schema", "folded-grams-v1");
  classifiers.prepare("INSERT INTO raw_records VALUES ('spgz', '1', ?)").run(gzipSync(JSON.stringify({
    okeis: [{ code: "039" }, { code: "290" }],
    spgzCharacteristics: [
      { characteristicName: "Диагональ", valueTypeId: 1, value1: "27", kpgzCharacteristicId: 1 },
      { characteristicName: "Частота обновления", valueTypeId: 1, kpgzCharacteristicId: 2 },
      { characteristicName: "Вид частоты", valueTypeId: 5, value1: "Переменная", kpgzCharacteristicId: 3 },
    ],
  })));
  classifiers.prepare("INSERT INTO okei VALUES (?, ?, 'unit')").run("039", "Дюйм (25,4 мм)");
  classifiers.prepare("INSERT INTO okei VALUES (?, ?, 'unit')").run("290", "Герц");
  classifiers.close();
  const ktru = new Database(process.env.KTRU_SQLITE!);
  ktru.exec("CREATE TABLE ktru(code TEXT, payload BLOB, name TEXT, official_name TEXT); CREATE TABLE ktru_okpd(code TEXT, name TEXT); CREATE TABLE meta(key TEXT, value TEXT)");
  const meta = ktru.prepare("INSERT INTO meta VALUES (?, ?)");
  for (const [key, value] of [
    ["record_count", "1"], ["schema", "ktru-mmap-v2"], ["lexical_schema", "folded-grams-v1"],
    ["source", process.env.KTRU_PARQUET!], ["source_mtime_ms", String(Math.trunc(statSync(process.env.KTRU_PARQUET!).mtimeMs))],
    ["names_source", ""], ["names_mtime_ms", ""],
  ]) meta.run(key, value);
  const raw = { code, name: "Монитор", characteristics: [
    { code: "diagonal", name: "Диагональ", type: "2", values: [{ concrete_values: ["27"], unit: { code: "039", name: "Дюйм (25,4 мм)" } }] },
    { code: "frequency", name: "Частота обновления", type: "2", values: [{ ranges: [{ min: "60", min_operator: "greaterOrEqual", max: "120", max_operator: "less" }], unit: { code: "290", name: "Герц" } }] },
  ] };
  ktru.prepare("INSERT INTO ktru VALUES (?, ?, ?, ?)").run(code, gzipSync(JSON.stringify(raw)), raw.name, raw.name);
  ktru.close();
});
after(() => {
  for (const key of environmentKeys) {
    if (environment[key] === undefined) delete process.env[key]; else process.env[key] = environment[key];
  }
  rmSync(directory, { recursive: true });
});

test("official KTRU records keep 039 inches and Hz through drafts and extraction transport", async () => {
  const record = getKtruRecord(code)!;
  assert.equal(record.characteristics[0].parts[0].okeiCode, "039");
  assert.equal(record.characteristics[0].unit, "дюйм");
  assert.equal(record.characteristics[0].okeiCode, "039");
  assert.equal(record.characteristics[1].unit, "Гц");
  const fields = buildKtruDraftFields(record, [], false);
  const characteristics = autofillCharacteristicFields(buildDisplayCharacteristics(fields));
  assert.deepEqual(characteristics.map(({ unit, okeiCode }) => ({ unit, okeiCode })), [
    { unit: "дюйм", okeiCode: "039" }, { unit: "Гц", okeiCode: "290" },
  ]);
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async (_url, init) => {
      assert.deepEqual(JSON.parse(String(init?.body)).characteristics, characteristics);
      return Response.json({ characteristics: [] });
    };
    await apiAutofillCharacteristics({ text: "Монитор 27 дюймов, 75 Гц", characteristics, standards: [] });
  } finally { globalThis.fetch = originalFetch; }
});

test("server SPGZ records resolve loaded inches and Hz without attaching units to qualitative fields", () => {
  const record = getDirectoryRecord("spgz", "1")!;
  assert.equal(record.characteristics[0].parts?.[0].okeiCode, "039");
  const fields = record.characteristics.map(draftFromSpgzCharacteristic);
  assert.deepEqual(autofillCharacteristicFields(fields).map(({ unit, okeiCode }) => ({ unit, okeiCode })), [
    { unit: "дюйм", okeiCode: "039" }, { unit: "Гц", okeiCode: "290" }, { unit: "", okeiCode: undefined },
  ]);
});

test("SPGZ parts retain OKEI even when an authoritative display unit differs from the full name", () => {
  const draft = draftFromSpgzCharacteristic({ name: "Диагональ", unit: "in", parts: [{ text: "27 in", okeiCode: "039", okeiName: "Дюйм (25,4 мм)" }], badges: ["количественная"] });
  assert.equal(draft.unit, "in");
  assert.equal(autofillCharacteristicFields([draft])[0].okeiCode, "039");
  const hz = draftFromSpgzCharacteristic({ name: "Частота", parts: [{ text: "75 Гц", unit: "Гц", okeiCode: "290", okeiName: "Герц" }], badges: ["количественная"] });
  assert.equal(hz.unit, "Гц");
  assert.equal(hz.okeiCode, "290");
});

test("conflicting OKEI schemas stay separate and do not retain old values", () => {
  const [inches] = buildKtruDraftFields(getKtruRecord(code)!, [], false);
  const conflicting = { ...inches, id: "spgz:diagonal", source: "spgz" as const, okeiCode: "003" };
  assert.equal(buildDisplayCharacteristics([inches, conflicting]).length, 2);
  assert.equal(buildDisplayCharacteristics([inches, { ...conflicting, okeiCode: "039" }]).length, 1);
  assert.notEqual(characteristicEditKey(inches), characteristicEditKey({ ...inches, okeiCode: "003" }));
  assert.equal(hasNewAutofillFields([inches], [{ ...inches, okeiCode: "003" }]), true);
  const changed = structuredClone(getKtruRecord(code)!);
  changed.characteristics[0].okeiCode = "003";
  const [draft] = buildKtruDraftFields(changed, [{ ...inches, value: "27" }], true);
  assert.equal(draft.value, "");
});

test("schema edits retain OKEI only for the unchanged unit", () => {
  const [draft] = buildKtruDraftFields(getKtruRecord(code)!, [], false);
  const schema = schemaFromCharacteristic(draft);
  assert.equal(draftFromCharacteristicSchema(schema, draft.id, draft).okeiCode, "039");
  assert.equal(draftFromCharacteristicSchema({ ...schema, unit: "мм" }, draft.id, draft).okeiCode, undefined);
});
