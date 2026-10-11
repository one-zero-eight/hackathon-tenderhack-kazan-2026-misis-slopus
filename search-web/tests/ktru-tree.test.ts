import assert from "node:assert/strict";
import { after, before, mock, test } from "node:test";
import { mkdtempSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import Database from "better-sqlite3";
import {
  getKtruNodeByCode, getKtruRaw, getKtruRecord, getKtruRevealPath, getKtruSpecialization, getSoleTemplateKtruForOkpd,
  ktruFilterMeta, ktruStats, listKtruByOkpd, listKtruChildren, searchKtru,
} from "../src/lib/server/ktru-tree";

// Every input/output below is a small temporary fixture, never a production catalog.
const directory = mkdtempSync(join(tmpdir(), "ktru-mmap-"));
const environment = { ...process.env };
const okpd = "25.73.30.141";
const template = `${okpd}-00000001`;
const childCodes = [`${okpd}-00000002`, `${okpd}-00000003`];
const rows = [template, ...childCodes].map((code, index) => ({
  code, name: index ? `Вариант ${index}` : "Шаблон Ёлочный 50%", status: "ACTIVE", is_template: index === 0,
  version: "1", update_date: "2026-06-03T12:00:00Z",
  okpd2: { code: okpd, name: "Молотки" }, parent: index ? { code: template } : null,
  characteristics: [{ name: "Материал", code: "material", required: true, kind: "1", values: [{ text: index === 2 ? "Медь" : "Сталь", concrete_values: [] as string[] }] }],
}));
const manyOkpd = "31.01.11.150";
for (let index = 0; index < 805; index++) rows.push({
  ...rows[0], code: `${manyOkpd}-${String(index).padStart(8, "0")}`, name: `Мебель офисная ${index}`,
  okpd2: { code: manyOkpd, name: "Мебель" }, is_template: false, parent: null,
});
rows.push({ ...rows[0], code: "99.01-00000001", name: "Без ОКПД", okpd2: null as unknown as typeof rows[0]["okpd2"], parent: { code: "missing" }, is_template: false });
const largeOkpd = "88.01";
const largeTemplate = `${largeOkpd}-00000000`;
rows.push({ ...rows[0], code: largeTemplate, name: "Большой шаблон", okpd2: { code: largeOkpd, name: "Большие группы" }, parent: null });
for (let index = 1; index <= 805; index++) rows.push({
  ...rows[1], code: `${largeOkpd}-${String(index).padStart(8, "0")}`, name: `Большой вариант ${index}`,
  okpd2: { code: largeOkpd, name: "Большие группы" }, parent: { code: largeTemplate },
  characteristics: [{ ...rows[1].characteristics[0], values: [{ text: `Значение ${index}`, concrete_values: [] }] }],
});

rows.unshift({
  ...rows[0], code: "77.01-00000001", name: "Молоток с вариантами материала", version: "3",
  is_template: false, okpd2: { code: "77.01", name: "Тестовые варианты" },
  characteristics: [{
    ...rows[0].characteristics[0],
    values: [{ text: "", concrete_values: ["Сталь", "Алюминий", "Медь"] }],
  }],
});

const woodOkpd = "66.01";
const woodTemplate = `${woodOkpd}-00000001`;
const woodChild = `${woodOkpd}-00000002`;
const falseWoodChild = `${woodOkpd}-00000003`;
for (const [index, code] of [woodTemplate, woodChild, falseWoodChild].entries()) rows.push({
  ...rows[0], code, name: index ? "Официальная рукоятка" : "Рукоятки", is_template: index === 0,
  okpd2: { code: woodOkpd, name: "Рукоятки" }, parent: index ? { code: woodTemplate } : null,
  characteristics: [{ ...rows[0].characteristics[0], values: [{ text: index === 2 ? "Деревянный" : "Дерево", concrete_values: [] }] }],
});

const multiOkpd = "55.01";
const multiTemplate = `${multiOkpd}-00000001`;
const multiChildren = [
  { code: `${multiOkpd}-00000002`, material: "Сталь", size: "10" },
  { code: `${multiOkpd}-00000003`, material: "Сталь", size: "20" },
  { code: `${multiOkpd}-00000004`, material: "Медь", size: "10" },
  { code: `${multiOkpd}-00000005`, material: "Медь", size: "20" },
];
rows.push({
  ...rows[0], code: multiTemplate, name: "Мультишаблон", is_template: true,
  okpd2: { code: multiOkpd, name: "Мульти" }, parent: null,
  characteristics: [
    { name: "Материал", code: "material", required: true, kind: "1", values: [{ text: "Сталь", concrete_values: [] as string[] }] },
    { name: "Размер", code: "size", required: true, kind: "1", values: [{ text: "10", concrete_values: [] as string[] }] },
  ],
});
for (const [index, child] of multiChildren.entries()) {
  rows.push({
    ...rows[0], code: child.code, name: `Мультивариант ${index + 1}`, is_template: false,
    okpd2: { code: multiOkpd, name: "Мульти" }, parent: { code: multiTemplate },
    characteristics: [
      { name: "Материал", code: "material", required: true, kind: "1", values: [{ text: child.material, concrete_values: [] as string[] }] },
      { name: "Размер", code: "size", required: true, kind: "1", values: [{ text: child.size, concrete_values: [] as string[] }] },
    ],
  });
}

const densityOkpd = "44.01";
const densityTemplate = `${densityOkpd}-00000001`;
const densityChild = `${densityOkpd}-00000002`;
const densityChildExact = `${densityOkpd}-00000003`;
rows.push({
  ...rows[0], code: densityTemplate, name: "Наволочки", is_template: true,
  okpd2: { code: densityOkpd, name: "Наволочки" }, parent: null,
  characteristics: [
    { name: "Плотность ткани, гр/кв.м", code: "density", required: true, kind: "2", values: [{ text: "≥160", concrete_values: [] as string[] }] },
  ],
});
rows.push({
  ...rows[0], code: densityChild, name: "Наволочка ХБ, плотность от 160 г/кв.м", is_template: false,
  okpd2: { code: densityOkpd, name: "Наволочки" }, parent: { code: densityTemplate },
  characteristics: [
    { name: "Плотность ткани, гр/кв.м", code: "density", required: true, kind: "2", values: [{ text: "≥160", concrete_values: [] as string[] }] },
  ],
});
rows.push({
  ...rows[0], code: densityChildExact, name: "Наволочка ХБ ≥155 г/кв.м", is_template: false,
  okpd2: { code: densityOkpd, name: "Наволочки" }, parent: { code: densityTemplate },
  characteristics: [
    { name: "Плотность ткани, гр/кв.м", code: "density", required: true, kind: "2", values: [{ text: "≥155", concrete_values: [] as string[] }] },
  ],
});

before(() => {
  process.env.KTRU_PARQUET = join(directory, "fixture.parquet");
  process.env.KTRU_SQLITE = join(directory, "fixture.sqlite");
  process.env.MOY_KTRU_PARQUET = join(directory, "mirror.parquet");
  process.env.CLASSIFIERS_SQLITE = join(directory, "absent-classifiers.sqlite");
  const jsonPath = join(directory, "fixture.json");
  writeFileSync(jsonPath, JSON.stringify(rows));
  const parquet = spawnSync("uv", ["run", "--with", "pyarrow", "python", "-c",
    "import json, sys, pyarrow as pa, pyarrow.parquet as pq; from pathlib import Path; pq.write_table(pa.Table.from_pylist(json.loads(Path(sys.argv[1]).read_text())), sys.argv[2])",
    jsonPath, process.env.KTRU_PARQUET], { encoding: "utf8" });
  assert.equal(parquet.status, 0, parquet.stderr || parquet.stdout);
  const namesJson = join(directory, "mirror.json");
  writeFileSync(namesJson, JSON.stringify([
    { code: template, tree_display_name: "Молоток ёлочный красивый", shortname: "Ёлочный молоток", similar_products: ["ШТУКАТУРНЫЙ инструмент", "ёлка"] },
    { code: woodTemplate, tree_display_name: "Рукоятка", shortname: "", similar_products: [] },
    { code: woodChild, tree_display_name: "Рукоятка деревянная", shortname: "", similar_products: [] },
    { code: falseWoodChild, tree_display_name: "Рукоятка недеревянная", shortname: "", similar_products: [] },
  ]));
  const mirror = spawnSync("uv", ["run", "--with", "pyarrow", "python", "-c",
    "import json, sys, pyarrow as pa, pyarrow.parquet as pq; from pathlib import Path; pq.write_table(pa.Table.from_pylist(json.loads(Path(sys.argv[1]).read_text())), sys.argv[2])",
    namesJson, process.env.MOY_KTRU_PARQUET], { encoding: "utf8" });
  assert.equal(mirror.status, 0, mirror.stderr || mirror.stdout);
  const result = spawnSync("uv", ["run", "--with", "pyarrow", "python", resolve(import.meta.dirname, "../../scripts/build_ktru_sqlite.py"),
    "--source", process.env.KTRU_PARQUET, "--names", process.env.MOY_KTRU_PARQUET,
    "--output", process.env.KTRU_SQLITE], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr || result.stdout);
  const prepare = Database.prototype.prepare;
  mock.method(Database.prototype, "prepare", function (this: Database.Database, sql: string) {
    if (/SELECT .* FROM ktru(?:\s|$)/i.test(sql) && !sql.includes("count(*)") && !sql.includes("WHERE code=?")) {
      assert.match(sql, /LIMIT/i, `Unbounded KTRU materialization: ${sql}`);
    }
    return prepare.call(this, sql);
  });
});

after(() => {
  mock.restoreAll();
  for (const key of ["KTRU_PARQUET", "KTRU_SQLITE", "MOY_KTRU_PARQUET", "CLASSIFIERS_SQLITE"]) {
    if (environment[key] === undefined) delete process.env[key];
    else process.env[key] = environment[key];
  }
  rmSync(directory, { recursive: true });
});

test("streaming builder writes versioned root/parent/search indexes", () => {
  const db = new Database(process.env.KTRU_SQLITE!, { readonly: true });
  assert.deepEqual(db.prepare("SELECT value FROM meta WHERE key='schema'").get(), { value: "ktru-mmap-v2" });
  assert.deepEqual(db.prepare("SELECT value FROM meta WHERE key='record_count'").get(), { value: String(rows.length) });
  assert.deepEqual(db.prepare("SELECT value FROM meta WHERE key='source_mtime_ms'").get(), {
    value: String(Math.trunc(statSync(process.env.KTRU_PARQUET!).mtimeMs)),
  });
  const plan = preparePlan(db, "SELECT code FROM ktru WHERE effective_okpd=? AND is_okpd_root=1 ORDER BY code LIMIT 200", manyOkpd);
  assert.match(plan, /ix_ktru_roots/);
  assert.match(preparePlan(db, "SELECT code FROM ktru WHERE parent_code=? ORDER BY code LIMIT 200", template), /ix_ktru_parent/);
  db.close();
});

function preparePlan(db: Database.Database, sql: string, key: string): string {
  return (db.prepare(`EXPLAIN QUERY PLAN ${sql}`).all(key) as { detail: string }[]).map(row => row.detail).join("\n");
}

test("indexed root pagination retains exact counts and lexicographic ordering", () => {
  assert.equal(ktruStats().total, rows.length);
  const first = listKtruChildren(`okpd:${manyOkpd}`);
  assert.equal(first.total, 805);
  assert.equal(first.nodes.length, 200);
  assert.equal(first.hasMore, true);
  assert.equal(first.nodes[0].code, `${manyOkpd}-00000000`);
  const last = listKtruChildren(`okpd:${manyOkpd}`, 800);
  assert.equal(last.nodes.length, 5);
  assert.equal(last.hasMore, false);
  assert.equal(listKtruByOkpd(manyOkpd, -1).items.length, 0);
  assert.equal(listKtruByOkpd(manyOkpd, 100000).items.length, 800);
  assert.equal(listKtruChildren("ktru:missing").nodes.length, 0);
  assert.equal(listKtruChildren("okpd:99.01").nodes[0].code, "99.01-00000001");
});

test("Russian substring filtering is indexed, bounded, and retains ancestors", () => {
  const meta = ktruFilterMeta("МЕБЕЛЬ ОФИСНАЯ");
  assert.equal(meta.matchCount, 805);
  assert.equal(meta.matchIds.length, 800);
  assert.equal(meta.truncated, true);
  assert.equal(listKtruChildren(`okpd:${manyOkpd}`, 600, "МЕБЕЛЬ ОФИСНАЯ").nodes.length, 200);
  assert.equal(listKtruChildren(`okpd:${manyOkpd}`, 800, "МЕБЕЛЬ ОФИСНАЯ").nodes.length, 0);
  assert.deepEqual(ktruFilterMeta("ЁЛОЧНЫЙ").matchIds, [`ktru:${template}`]);
  assert.deepEqual(ktruFilterMeta("50%").matchIds, [`ktru:${template}`]);
  assert.ok(ktruFilterMeta("ЁЛ").matchIds.includes(`ktru:${template}`));
  assert.equal(listKtruChildren(`okpd:${okpd}`, 0, "ВАРИАНТ 2").nodes[0].code, template);
  assert.equal(listKtruChildren(`ktru:${template}`, 0, "ВАРИАНТ 2").nodes[0].code, childCodes[1]);
});

test("specialization, sole templates, variant labels and reveal paths retain behavior", () => {
  assert.equal(getSoleTemplateKtruForOkpd(okpd)?.code, template);
  assert.equal(getSoleTemplateKtruForOkpd(manyOkpd), null);
  assert.deepEqual(getKtruSpecialization(childCodes[0])?.currentValues, { Материал: "Сталь" });
  assert.deepEqual(getKtruSpecialization(childCodes[1])?.currentValues, { Материал: "Медь" });
  assert.deepEqual(getKtruSpecialization(childCodes[0])?.characteristics.map((item) => item.name), ["Материал"]);
  assert.equal(getKtruNodeByCode(childCodes[0])?.variantLabel, "Материал: Сталь");
  assert.deepEqual(getKtruRevealPath(childCodes[0]).pathIds.slice(-2), [`ktru:${template}`, `ktru:${childCodes[0]}`]);
  assert.deepEqual(getKtruRevealPath("missing"), { node: null, pathIds: [] });
});

test("record preserves each concrete value as a separate characteristic option", () => {
  const record = getKtruRecord("77.01-00000001")!;
  assert.equal(record.version, "3");
  assert.equal(record.officialName, "Молоток с вариантами материала");
  assert.equal(record.name, record.officialName);
  assert.equal(record.dates.find(item => item.label === "Дата обновления")?.value, "2026-06-03");
  assert.equal(record.characteristics[0].required, true);
  assert.deepEqual(record.characteristics[0].parts.map(part => part.text), ["Сталь", "Алюминий", "Медь"]);
  assert.deepEqual(record.characteristics[0].options, ["Сталь", "Алюминий", "Медь"]);
});

test("mirror display names override tree labels without changing official payload name", () => {
  const namesPath = join(directory, "names.parquet");
  const namesJson = join(directory, "names.json");
  writeFileSync(namesJson, JSON.stringify([{
    code: template,
    tree_display_name: "Молоток ёлочный красивый",
    shortname: "Молоток ёлочный красивый",
    similar_products: ["молоток штукатурный"],
  }]));
  const parquet = spawnSync("uv", ["run", "--with", "pyarrow", "python", "-c",
    "import json, sys, pyarrow as pa, pyarrow.parquet as pq; from pathlib import Path; pq.write_table(pa.Table.from_pylist(json.loads(Path(sys.argv[1]).read_text())), sys.argv[2])",
    namesJson, namesPath], { encoding: "utf8" });
  assert.equal(parquet.status, 0, parquet.stderr || parquet.stdout);
  const rebuilt = join(directory, "named.sqlite");
  const result = spawnSync("uv", ["run", "--with", "pyarrow", "python", resolve(import.meta.dirname, "../../scripts/build_ktru_sqlite.py"),
    "--source", process.env.KTRU_PARQUET!, "--names", namesPath, "--output", rebuilt], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr || result.stdout);
  const db = new Database(rebuilt, { readonly: true, fileMustExist: true });
  const row = db.prepare("SELECT name, official_name AS officialName, similar_products AS similar FROM ktru WHERE code=?").get(template) as {
    name: string; officialName: string; similar: string | null;
  };
  db.close();
  assert.equal(row.name, "Молоток ёлочный красивый");
  assert.equal(row.officialName, "Шаблон Ёлочный 50%");
  assert.ok(row.similar?.includes("молоток штукатурный"));
});

test("specializations above the match limit are complete rather than truncated", () => {
  const specialization = getKtruSpecialization(largeTemplate)!;
  assert.equal(specialization.options.length, 805);
  assert.equal(new Set(specialization.options.map(option => option.code)).size, 805);
  assert.ok(specialization.options.some(option => option.values.Материал === "Значение 805"));
});

test("display, official names and authoritative payload remain separate", () => {
  assert.equal(getKtruNodeByCode(template)?.name, "Молоток ёлочный красивый");
  assert.equal(getSoleTemplateKtruForOkpd(okpd)?.name, "Молоток ёлочный красивый");
  assert.equal(listKtruByOkpd(okpd).items[0]?.name, "Молоток ёлочный красивый");
  assert.equal(getKtruRecord(template)?.name, "Молоток ёлочный красивый");
  assert.equal(getKtruRecord(template)?.officialName, "Шаблон Ёлочный 50%");
  assert.equal(getKtruRaw(template)?.name, "Шаблон Ёлочный 50%");
});

test("variant labels always surface discriminating characteristics in the tree", () => {
  assert.equal(getKtruNodeByCode(woodChild)?.variantLabel, "Материал: Дерево");
  assert.equal(getKtruNodeByCode(falseWoodChild)?.variantLabel, "Материал: Деревянный");
  assert.equal(getKtruNodeByCode(densityChild)?.variantLabel, "Плотность ткани, гр/кв.м: ≥160");
  assert.equal(getKtruNodeByCode(densityChildExact)?.variantLabel, "Плотность ткани, гр/кв.м: ≥155");
  assert.equal(
    listKtruChildren(`ktru:${densityTemplate}`).nodes.find(node => node.code === densityChild)?.variantLabel,
    "Плотность ткани, гр/кв.м: ≥160",
  );
});

test("multi-characteristic specialization partitions by value tuples", () => {
  const specialization = getKtruSpecialization(multiTemplate)!;
  assert.deepEqual(specialization.characteristics.map((item) => item.name).sort(), ["Материал", "Размер"]);
  assert.equal(specialization.options.length, 4);
  assert.deepEqual(
    getKtruSpecialization(multiChildren[1]!.code)?.currentValues,
    { Материал: "Сталь", Размер: "20" },
  );
  assert.equal(
    getKtruNodeByCode(multiChildren[2]!.code)?.variantLabel,
    "Материал: Медь · Размер: 10",
  );
  const steelOnly = specialization.options.filter((option) => option.values.Материал === "Сталь");
  assert.equal(steelOnly.length, 2);
  assert.equal(new Set(steelOnly.map((option) => option.values.Размер)).size, 2);
});

test("multifield AND matching is order-independent, folded and indexed", () => {
  for (const query of ["елочный", "Е\u0308ЛОЧНЫЙ", "красивый молот", "инструмент молот", "ШТУКАТУРНЫЙ", "красивый 50%", "молоток елочныи"]) {
    assert.equal(searchKtru(query)[0]?.code, template, query);
    assert.ok(ktruFilterMeta(query).matchIds.includes(`ktru:${template}`), query);
  }
  assert.deepEqual(searchKtru("молоток несуществующее"), []);
  assert.equal(searchKtru(template)[0]?.code, template);
  const db = new Database(process.env.KTRU_SQLITE!, { readonly: true });
  const sql = "SELECT code FROM ktru WHERE rowid IN (SELECT rowid FROM ktru_search WHERE ktru_search MATCH ?) LIMIT 40";
  const plan = preparePlan(db, sql, "g00043c000435 AND g00043e000444");
  assert.match(plan, /VIRTUAL TABLE INDEX/);
  assert.match(plan, /INTEGER PRIMARY KEY/);
  assert.doesNotMatch(plan, /SCAN ktru(?:\s|$)/);
  assert.deepEqual(db.prepare("SELECT count(*) AS n FROM sqlite_master WHERE name='display_names'").get(), { n: 0 });
  db.close();
});

test("SQL-scored search supports Cyrillic case, substring punctuation and stems", () => {
  assert.equal(searchKtru("ЁЛОЧНЫЙ")[0]?.code, template);
  assert.equal(searchKtru("50%")[0]?.code, template);
  assert.equal(searchKtru("ЁЛ")[0]?.code, template);
  assert.deepEqual(searchKtru("варианта 2").map(node => node.code), [childCodes[1], `${largeOkpd}-00000002`]);
  assert.equal(searchKtru("МЕБЕЛЬ ОФИСНАЯ").length, 40);
  assert.deepEqual(searchKtru("несуществующее"), []);
});
