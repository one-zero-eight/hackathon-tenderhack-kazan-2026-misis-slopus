import assert from "node:assert/strict";
import { after, before, mock, test } from "node:test";
import { mkdtempSync, realpathSync, renameSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { gzipSync } from "node:zlib";
import Database from "better-sqlite3";
import { classifierStats, directoryFilterMeta, getDirectoryRaw, getSpgzSpecialization, listDirectoryProducts } from "../src/lib/server/classifiers";
import { getSteCatalog, resetSteCatalog } from "../src/lib/server/ste-catalog";
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
let payloadQueries = 0;
const rawFixtures = [
  ["okpd", "25.73.30.141", { id: 1, code: "25.73.30.141", name: "Молотки", limitations: [{ name: "Примечание" }] }],
  ["kpgz", "01", { id: 1, code: "01", name: "Инструменты", innovation: 1, treePath: [1] }],
  ["okei", "796", { code: "796", name: "Штука", razdel: "1", podrazdel: "1" }],
  ["okei", "s:1", { code: null, name: "Раздел", razdel: "1", podrazdel: "0" }],
  ["okei", "g:1:1", { code: null, name: "Группа", razdel: "1", podrazdel: "1" }],
] as const;
const environmentKeys = [
  "OKPD2_PARQUET", "KPGZ_PARQUET", "SPGZ_PARQUET", "OKEI_PARQUET", "CLASSIFIERS_SQLITE",
  "KTRU_PARQUET", "KTRU_SQLITE", "MOY_KTRU_PARQUET", "CATEGORY_PARQUETS", "CATALOG_DATABASE", "SEARCH_DATABASE",
  "STE_PARQUET", "STE_DETAILS_PARQUET",
];

before(() => {
  const sources = [
    ["okpd", "OKPD2_PARQUET"], ["kpgz", "KPGZ_PARQUET"],
    ["spgz", "SPGZ_PARQUET"], ["okei", "OKEI_PARQUET"],
  ] as const;
  // Only stat()/fingerprint checks may touch these placeholders. Reading them
  // as parquet or triggering a builder fails rather than opening production data.
  for (const [name, key] of sources) {
    process.env[key] = join(directory, `${name}.parquet`);
    writeFileSync(process.env[key]!, "fixture-source-not-parquet");
  }
  process.env.CLASSIFIERS_SQLITE = join(directory, "classifiers.sqlite");
  process.env.KTRU_PARQUET = join(directory, "ktru.parquet");
  process.env.KTRU_SQLITE = join(directory, "ktru.sqlite");
  process.env.MOY_KTRU_PARQUET = join(directory, "absent-names.parquet");
  process.env.CATEGORY_PARQUETS = join(directory, "catalog.parquet");
  process.env.CATALOG_DATABASE = join(directory, "catalog.sqlite");
  process.env.SEARCH_DATABASE = join(directory, "index.sqlite");
  delete process.env.STE_PARQUET;
  delete process.env.STE_DETAILS_PARQUET;
  writeFileSync(process.env.KTRU_PARQUET, "fixture-source-not-parquet");
  writeFileSync(process.env.CATEGORY_PARQUETS, "fixture-source-not-parquet");

  const classifiers = new Database(process.env.CLASSIFIERS_SQLITE);
  classifiers.exec(`
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE raw_records(tab TEXT, code TEXT, payload BLOB, PRIMARY KEY(tab,code)) WITHOUT ROWID;
    CREATE TABLE okpd(id INTEGER PRIMARY KEY, code TEXT, name TEXT, parent_id INTEGER, name_fold TEXT, synonyms_fold TEXT);
    CREATE TABLE kpgz(id INTEGER PRIMARY KEY, code TEXT, name TEXT, external_id INTEGER);
    CREATE TABLE spgz(id INTEGER PRIMARY KEY, kpgz_external_id INTEGER, is_excluded INTEGER);
    CREATE TABLE okei(id INTEGER PRIMARY KEY, code TEXT, kind TEXT, name TEXT);
    CREATE VIRTUAL TABLE okpd_search USING fts5(grams, content='', tokenize='unicode61');
    INSERT INTO okpd VALUES (1, '25.73.30.141', 'Молотки ёлочные', NULL, 'молотки елочные', 'штукатурный инструмент'),
      (2, '25.73.30.142', 'Молотки ёлочные специальные', NULL, 'молотки елочные специальные', '');
    CREATE INDEX ix_kpgz_code ON kpgz(code);
    CREATE INDEX ix_spgz_kpgz ON spgz(kpgz_external_id);
    INSERT INTO kpgz VALUES (1, '01', 'Инструменты', 10), (2, '02', 'Прочее', 20);
    INSERT INTO spgz VALUES (1, 10, 0), (2, 10, 0), (3, 20, 0);
    INSERT INTO okei VALUES (1, '796', 'unit', 'Штука'), (2, 's:1', 'section', 'Раздел');
  `);
  const searchInsert = classifiers.prepare("INSERT INTO okpd_search(rowid,grams) VALUES (?,?)");
  for (const row of classifiers.prepare("SELECT id, code, name_fold AS name, synonyms_fold AS synonyms FROM okpd").all() as { id: number; code: string; name: string; synonyms: string }[]) {
    const text = `${row.code} ${row.name} ${row.synonyms}`;
    const grams = new Set<string>([...text].map(char => `g${char.codePointAt(0)!.toString(16).padStart(6, "0")}`));
    for (let i = 1; i < text.length; i++) grams.add(`g${text.codePointAt(i - 1)!.toString(16).padStart(6, "0")}${text.codePointAt(i)!.toString(16).padStart(6, "0")}`);
    searchInsert.run(row.id, [...grams].join(" "));
  }
  const mtime = String(Math.trunc(Math.max(...sources.map(([, key]) => statSync(process.env[key]!).mtimeMs))));
  const meta = classifiers.prepare("INSERT INTO meta VALUES (?, ?)");
  meta.run("source_mtime_ms", mtime);
  meta.run("schema", "classifiers-full-v4");
  meta.run("lexical_schema", "folded-grams-v1");
  for (const [name, key] of sources) {
    meta.run(name, process.env[key]!);
    meta.run(`${name}_mtime_ms`, String(Math.trunc(statSync(process.env[key]!).mtimeMs)));
  }
  const insertRaw = classifiers.prepare("INSERT INTO raw_records VALUES (?,?,?)");
  for (const [tab, code, raw] of rawFixtures) insertRaw.run(tab, code, gzipSync(JSON.stringify(raw)));
  for (const row of spgzRows) insertRaw.run("spgz", String(row.id), gzipSync(JSON.stringify(row)));
  classifiers.close();

  const catalog = new Database(process.env.CATALOG_DATABASE);
  catalog.exec(`
    CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE products(id INTEGER PRIMARY KEY, category_id INTEGER, payload TEXT,
      portal_category_id INTEGER, portal_category TEXT, name TEXT, okpd2 TEXT, kpgz_code TEXT, kpgz_name TEXT, unit TEXT);
    CREATE INDEX products_okpd2 ON products(okpd2,name,id);
    CREATE INDEX products_kpgz_code ON products(kpgz_code,name,id);
    CREATE INDEX products_unit ON products(unit,name,id);
  `);
  const fingerprint = JSON.stringify([process.env.CATEGORY_PARQUETS, process.env.KPGZ_PARQUET].map(path => {
    const stat = statSync(path!, { bigint: true });
    return [realpathSync(path!), String(stat.mtimeNs), String(stat.size)];
  }));
  const catalogMeta = catalog.prepare("INSERT INTO metadata VALUES (?,?)");
  for (const [key, value] of [["schema", "sqlite-catalog-v2"], ["fingerprint", fingerprint], ["portal_stats", "[]"]]) {
    catalogMeta.run(key, value);
  }
  const insertProduct = catalog.prepare("INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?,?)");
  for (const [id, name, unit] of [[101, "А", "штука"], [102, "А", "ШТУКА"], [103, "Б", "Штука"], [104, "Не индексирован", "Штука"]] as const) {
    insertProduct.run(id, 1, "{}", 1, "Инструменты", name, "25.73.30.141", "01", "", unit);
  }
  catalog.close();
  const index = new Database(process.env.SEARCH_DATABASE);
  index.exec("CREATE TABLE index_state(product_id INTEGER PRIMARY KEY); INSERT INTO index_state VALUES (101),(102),(103)");
  index.close();

  const ktru = new Database(process.env.KTRU_SQLITE);
  ktru.exec(`
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE ktru(code TEXT PRIMARY KEY, name TEXT, official_name TEXT, status TEXT, okpd2 TEXT, okpd2_name TEXT, parent_code TEXT, is_template INTEGER, payload BLOB,
      name_fold TEXT, effective_okpd TEXT, is_okpd_root INTEGER);
    CREATE TABLE ktru_okpd(code TEXT PRIMARY KEY, name TEXT);
    CREATE INDEX ix_ktru_parent ON ktru(parent_code,code);
    CREATE INDEX ix_ktru_roots ON ktru(effective_okpd,is_okpd_root,code);
    INSERT INTO ktru_okpd VALUES ('25.73.30.141', 'Молотки');
  `);
  ktru.prepare("INSERT INTO meta VALUES (?, ?)").run("source_mtime_ms", String(Math.trunc(statSync(process.env.KTRU_PARQUET).mtimeMs)));
  ktru.prepare("INSERT INTO meta VALUES (?, ?)").run("schema", "ktru-mmap-v2");
  ktru.prepare("INSERT INTO meta VALUES (?, ?)").run("lexical_schema", "folded-grams-v1");
  ktru.prepare("INSERT INTO meta VALUES (?, ?)").run("source", process.env.KTRU_PARQUET);
  ktru.prepare("INSERT INTO meta VALUES (?, ?)").run("names_source", "");
  ktru.prepare("INSERT INTO meta VALUES (?, ?)").run("names_mtime_ms", "");
  ktru.prepare("INSERT INTO meta VALUES (?, ?)").run("record_count", "3");
  for (const [index, code] of [template, ...childCodes].entries()) {
    const raw = {
      code, name: index ? `Вариант ${index}` : "Шаблон", status: "ACTIVE", is_template: index === 0,
      okpd2: { code: "25.73.30.141", name: "Молотки" },
      characteristics: [{ code: "material", name: "Материал", required: true, type: "1", kind: "1", values: [{ text: index === 2 ? "Медь" : "Сталь" }] }],
      nsi: { standards: [{ document_name: "ГОСТ 123" }] },
    };
    ktru.prepare("INSERT INTO ktru VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
      .run(code, raw.name, raw.name, "ACTIVE", raw.okpd2.code, raw.okpd2.name, index ? template : null, index === 0 ? 1 : 0,
        gzipSync(JSON.stringify(raw)), raw.name.toLocaleLowerCase("ru-RU"), raw.okpd2.code, index === 0 ? 1 : 0);
  }
  ktru.close();

  const prepare = Database.prototype.prepare;
  mock.method(Database.prototype, "prepare", function (this: Database.Database, sql: string) {
    if (sql === "SELECT payload, name, official_name AS officialName FROM ktru WHERE code=?") payloadQueries += 1;
    return prepare.call(this, sql);
  });
});

after(() => {
  mock.restoreAll();
  resetSteCatalog();
  for (const key of environmentKeys) {
    if (environment[key] === undefined) delete process.env[key];
    else process.env[key] = environment[key];
  }
  rmSync(directory, { recursive: true });
});

test("classifier builder writes folded indexes in bounded batches without schema changes", () => {
  const fixtureDir = join(directory, "builder");
  const json = join(directory, "classifier-builder.json");
  writeFileSync(json, JSON.stringify({
    okpd: Array.from({ length: 260 }, (_, id) => ({ id: id + 1, code: `99.${id}`, name: "ЁЛОЧНЫЕ молотки", similar_products: ["ШТУКАТУРНЫЙ"], children_count: 0, is_actual: true })),
    kpgz: [{ id: 1, code: "01", name: "Ёлочный инструмент" }],
    spgz: [{ id: 1, name: "Ёлочный молоток", kpgzId: 1 }],
    okei: [{ code: "796", name: "Штука", razdel: "1", podrazdel: "1" }],
  }));
  const parquet = spawnSync("uv", ["run", "--with", "pyarrow", "python", "-c",
    "import json, sys, pyarrow as pa, pyarrow.parquet as pq; from pathlib import Path; root=Path(sys.argv[2]); root.mkdir(); data=json.loads(Path(sys.argv[1]).read_text()); [pq.write_table(pa.Table.from_pylist(rows), root / (tab + '.parquet')) for tab, rows in data.items()]",
    json, fixtureDir], { encoding: "utf8" });
  assert.equal(parquet.status, 0, parquet.stderr || parquet.stdout);
  const output = join(fixtureDir, "classifiers.sqlite");
  const result = spawnSync("uv", ["run", "--with", "pyarrow", "python", resolve(import.meta.dirname, "../../scripts/build_classifiers_sqlite.py"),
    "--output", output, ...["okpd", "kpgz", "spgz", "okei"].flatMap(tab => [`--${tab}`, join(fixtureDir, `${tab}.parquet`)])], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr || result.stdout);
  const fixture = new Database(output, { readonly: true });
  assert.deepEqual(fixture.prepare("SELECT value FROM meta WHERE key='schema'").get(), { value: "classifiers-full-v4" });
  assert.deepEqual(fixture.prepare("SELECT name_fold, synonyms_fold FROM okpd WHERE id=1").get(), { name_fold: "елочные молотки", synonyms_fold: "штукатурный" });
  assert.deepEqual(fixture.prepare("SELECT count(*) AS n FROM okpd_search WHERE okpd_search MATCH 'g00043500043b'").get(), { n: 260 });
  for (const tab of ["okpd", "kpgz", "spgz", "okei"]) assert.equal((fixture.prepare("SELECT value FROM meta WHERE key=?").get(`${tab}_mtime_ms`) as { value: string }).value, String(Math.trunc(statSync(join(fixtureDir, `${tab}.parquet`)).mtimeMs)));
  fixture.close();
});

test("stale or absent caches fail with explicit build commands and no writes", () => {
  const sqlite = process.env.CLASSIFIERS_SQLITE!;
  const ktruSqlite = process.env.KTRU_SQLITE!;
  process.env.CLASSIFIERS_SQLITE = join(directory, "missing-classifiers.sqlite");
  process.env.KTRU_SQLITE = join(directory, "missing-ktru.sqlite");
  try {
    assert.throws(() => classifierStats(), /uv run --with pyarrow python .*build_classifiers_sqlite\.py/);
    assert.throws(() => ktruStats(), /uv run --with pyarrow python .*build_ktru_sqlite\.py/);
  } finally {
    process.env.CLASSIFIERS_SQLITE = sqlite;
    process.env.KTRU_SQLITE = ktruSqlite;
  }
  const fixture = new Database(sqlite);
  fixture.prepare("UPDATE meta SET value='old' WHERE key='lexical_schema'").run();
  try {
    assert.throws(() => classifierStats(), /Постройте его явно/);
  } finally {
    fixture.prepare("UPDATE meta SET value='folded-grams-v1' WHERE key='lexical_schema'").run();
    fixture.close();
  }
});

test("classifier lexical filter is folded, multifield, any-order AND and ranked", () => {
  assert.deepEqual(directoryFilterMeta("okpd", "ЁЛОЧНЫЕ МОЛОТКИ").matchIds, ["okpd:1", "okpd:2"]);
  assert.deepEqual(directoryFilterMeta("okpd", "инструмент молотки").matchIds, ["okpd:1"]);
  assert.deepEqual(directoryFilterMeta("okpd", "МОЛОТКИ Е\u0308ЛОЧНЫЕ").matchIds, ["okpd:1", "okpd:2"]);
  assert.deepEqual(directoryFilterMeta("okpd", "молотки несуществующее").matchIds, []);
  assert.deepEqual(directoryFilterMeta("okpd", "25.73.30.142").matchIds, ["okpd:2"]);
  const fixture = new Database(process.env.CLASSIFIERS_SQLITE!, { readonly: true });
  const plan = fixture.prepare("EXPLAIN QUERY PLAN SELECT id FROM okpd WHERE id IN (SELECT rowid FROM okpd_search WHERE okpd_search MATCH ?) LIMIT 800")
    .all("g00043c00043e") as { detail: string }[];
  assert.ok(plan.some(row => /VIRTUAL TABLE INDEX/.test(row.detail)));
  assert.ok(plan.some(row => /INTEGER PRIMARY KEY/.test(row.detail)));
  assert.ok(!plan.some(row => /SCAN okpd$/.test(row.detail)));
  fixture.close();
});

test("classifier raw rows and SPGZ siblings use gzip SQLite records without reading source files", () => {
  for (const [tab, code, raw] of rawFixtures) assert.deepEqual(getDirectoryRaw(tab, code), raw);
  const mutated = getDirectoryRaw("okpd", "25.73.30.141")!;
  (mutated.limitations as { name: string }[])[0].name = "Изменено";
  assert.deepEqual(getDirectoryRaw("okpd", "25.73.30.141"), rawFixtures[0][2]);
  assert.deepEqual(getDirectoryRaw("spgz", "1"), spgzRows[0]);
  assert.deepEqual(getDirectoryRaw("spgz", "2"), spgzRows[1]);
  const specialization = getSpgzSpecialization({ spgzCode: "1" });
  assert.deepEqual(specialization, {
    characteristics: [{ name: "Материал", code: "7" }],
    options: [
      { values: { Материал: "Медь" }, code: "2", name: "Позиция 2" },
      { values: { Материал: "Сталь" }, code: "1", name: "Позиция 1" },
    ],
    currentValues: { Материал: "Сталь" }, kpgzCode: "01",
  });
  assert.equal(getSpgzSpecialization({ kpgzCode: "02" }), null);
  assert.equal(getDirectoryRaw("spgz", "999"), null);
  assert.equal(getDirectoryRaw("spgz", "invalid"), null);
  assert.equal(getDirectoryRaw("spgz", "01")?.id, 1);
  assert.equal(getDirectoryRaw("ktru", template), null);
});

test("directory listings recover an attachment left by a previous module instance", () => {
  const { db } = getSteCatalog();
  db.prepare("ATTACH DATABASE ? AS directory_search_index").run(process.env.SEARCH_DATABASE!);
  assert.deepEqual(listDirectoryProducts("okpd", "25.73.30.141", 3).items.map(row => row.id), [101, 102, 103]);
  assert.equal((db.pragma("database_list") as { name: string }[])
    .filter(row => row.name === "directory_search_index").length, 1);
});

test("directory listings attach the search index read-only under query_only and use SQL paging", () => {
  const { db } = getSteCatalog();
  assert.equal(db.pragma("query_only", { simple: true }), 1);
  assert.equal(db.pragma("mmap_size", { simple: true }), 1073741824);
  assert.equal(db.pragma("cache_size", { simple: true }), -2048);
  const page = listDirectoryProducts("okpd", "25.73.30.141", 2);
  assert.equal(page.total, 3);
  assert.deepEqual(page.items.map(row => row.id), [101, 102]);
  assert.equal(page.items[0].kpgzName, "Инструменты");
  assert.equal(listDirectoryProducts("kpgz", "01", 0).total, 3);
  assert.deepEqual(listDirectoryProducts("kpgz", "01", 0).items, []);
  assert.deepEqual(listDirectoryProducts("okei", "796", 3).items.map(row => row.id), [101, 102, 103]);
  assert.deepEqual(listDirectoryProducts("okei", "s:1"), { total: 0, items: [] });
  const attached = (db.pragma("database_list") as { name: string; file: string }[])
    .find(row => row.name === "directory_search_index");
  assert.equal(attached?.file, process.env.SEARCH_DATABASE);
  assert.equal(db.pragma("directory_search_index.mmap_size", { simple: true }), 1073741824);
  assert.equal(db.pragma("directory_search_index.cache_size", { simple: true }), -2048);
  assert.equal(db.pragma("temp_store", { simple: true }), 1);
  db.pragma("query_only = OFF");
  try {
    assert.throws(() => db.prepare("INSERT INTO directory_search_index.index_state VALUES (999)").run(), /readonly/i);
  } finally {
    db.pragma("query_only = ON");
  }
  const index = new Database(process.env.SEARCH_DATABASE!);
  index.exec("DELETE FROM index_state WHERE product_id=102; INSERT INTO index_state VALUES (104)");
  index.close();
  assert.deepEqual(listDirectoryProducts("okpd", "25.73.30.141", 4).items.map(row => row.id), [101, 103, 104]);
});

test("directory listings reattach when the index file is replaced at the same path", () => {
  const replacement = join(directory, "replacement-index.sqlite");
  const index = new Database(replacement);
  index.exec("CREATE TABLE index_state(product_id INTEGER PRIMARY KEY); INSERT INTO index_state VALUES (102)");
  index.close();
  renameSync(replacement, process.env.SEARCH_DATABASE!);
  assert.deepEqual(listDirectoryProducts("okpd", "25.73.30.141", 4).items.map(row => row.id), [102]);
});

test("directory listings reject a search database without index_state", () => {
  process.env.SEARCH_DATABASE = join(directory, "invalid-index.sqlite");
  const index = new Database(process.env.SEARCH_DATABASE);
  index.exec("CREATE TABLE products(id INTEGER PRIMARY KEY); INSERT INTO products VALUES (102)");
  index.close();
  assert.deepEqual(listDirectoryProducts("kpgz", "01"), { total: 0, items: [] });
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
  assert.deepEqual(first.currentValues, { Материал: "Сталь" });
  const queries = payloadQueries;
  first.options[0]!.values.Материал = "Изменено";
  const second = getKtruSpecialization(childCodes[1])!;
  assert.deepEqual(second.currentValues, { Материал: "Медь" });
  assert.deepEqual(second.options.map(option => option.values.Материал), ["Медь", "Сталь"]);
  assert.equal(getKtruSpecialization(template)?.currentValues, null);
  assert.equal(getKtruNodeByCode(childCodes[0])?.variantLabel, "Материал: Сталь");
  assert.equal(payloadQueries, queries);
  assert.equal(getKtruSpecialization("missing"), null);
});
