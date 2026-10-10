import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { spawnSync } from "node:child_process";
import { openDatabase, type CatalogDatabase } from "../src/lib/server/database";
import { importProducts } from "../src/lib/server/importer";
import { cleanText, normalizeProduct, embeddingDocument, type SourceCard } from "../src/lib/server/normalize";
import { runSearch, ftsQuery, fuseRanks, findProduct, findSimilarProducts, validateSearch } from "../src/lib/server/search";
import { decodeDense } from "../src/lib/server/embeddings";
import { DIMENSIONS, MODEL_ID } from "../src/lib/server/config";
import { resetSteCatalog } from "../src/lib/server/ste-catalog";

const directory = mkdtempSync(join(tmpdir(), "zakupki-search-"));
const sourcePath = join(directory, "ste.parquet");
let catalog: CatalogDatabase;

const defaultChars = [
  { name: "Мощность", value: "0", value_number: 0, value_bool: null, unit: "Вт", unit_name: "Вт", type_id: 1, characteristic_id: 1, value_id: 1 },
  { name: "Беспроводной", value: "Нет", value_number: null, value_bool: false, unit: null, unit_name: null, type_id: 6, characteristic_id: 2, value_id: 2 },
];

const steRows = [
  {
    id: 101, name: "Пылесос Neolux", description: "Надёжная техника для дома и офиса",
    manufacturer: "Test", okei: "шт", oksm: "РОССИЯ", okpd2_code: "", okpd2_name: "",
    production_code: "", production_name: "", category_id: 1, category: "Бытовая техника",
    model: "", barcode: "", median_price: null, reference_price: null, cost_per_unit: 0,
    sku_image_ids: [42], created_date: "2026-10-07", characteristics: defaultChars,
  },
  {
    id: 102, name: "Кофемашина", description: "Надёжная техника для дома и офиса",
    manufacturer: "Test", okei: "шт", oksm: "РОССИЯ", okpd2_code: "", okpd2_name: "",
    production_code: "", production_name: "", category_id: 1, category: "Бытовая техника",
    model: "", barcode: "", median_price: null, reference_price: null, cost_per_unit: 0,
    sku_image_ids: [42], created_date: "2026-10-07", characteristics: defaultChars,
  },
  {
    id: 103, name: "Пылесос офисный", description: "Надёжная техника для дома и офиса",
    manufacturer: "Test", okei: "шт", oksm: "РОССИЯ", okpd2_code: "", okpd2_name: "",
    production_code: "", production_name: "", category_id: 2, category: "Офисная техника",
    model: "", barcode: "", median_price: null, reference_price: null, cost_per_unit: 0,
    sku_image_ids: [42], created_date: "2026-10-07", characteristics: defaultChars,
  },
];

function writeSource(rows = steRows) {
  const jsonPath = join(directory, "rows.json");
  writeFileSync(jsonPath, JSON.stringify(rows));
  const script = `
import json, pyarrow as pa, pyarrow.parquet as pq
from pathlib import Path
rows = json.loads(Path(${JSON.stringify(jsonPath)}).read_text())
char = pa.struct([
  ('name', pa.string()), ('value', pa.string()), ('value_number', pa.float64()),
  ('value_bool', pa.bool_()), ('unit', pa.string()), ('unit_name', pa.string()),
  ('type_id', pa.int64()), ('characteristic_id', pa.int64()), ('value_id', pa.int64()),
])
schema = pa.schema([
  ('id', pa.int64()), ('name', pa.string()), ('description', pa.string()),
  ('manufacturer', pa.string()), ('okei', pa.string()), ('oksm', pa.string()),
  ('okpd2_code', pa.string()), ('okpd2_name', pa.string()),
  ('production_code', pa.string()), ('production_name', pa.string()),
  ('category_id', pa.int64()), ('category', pa.string()),
  ('model', pa.string()), ('barcode', pa.string()),
  ('median_price', pa.float64()), ('reference_price', pa.float64()), ('cost_per_unit', pa.float64()),
  ('sku_image_ids', pa.list_(pa.int64())), ('characteristics', pa.list_(char)),
  ('created_date', pa.string()),
])
pq.write_table(pa.Table.from_pylist(rows, schema=schema), ${JSON.stringify(sourcePath)})
`;
  const result = spawnSync("uv", ["run", "--with", "pyarrow", "python", "-c", script], { encoding: "utf8" });
  if (result.status !== 0) throw new Error(result.stderr || result.stdout || "failed to write test parquet");
  resetSteCatalog();
}

const raw = (id: number, name: string, categoryId = 1): SourceCard => ({
  id, name, productionDirectoryId: categoryId, productionDirectoryName: categoryId === 1 ? "Бытовая техника" : "Офисная техника",
  description: "Надёжная техника для дома и офиса", manufacturerName: "Test", referencePrice: 0,
  images: [{ fileStorageId: 42 }],
  skuCharacteristics: [
    { productCharacteristicValue: { productCharacteristicName: "Мощность", productCharacteristicUnitDesignationNational: "Вт" }, characteristicValueIntValue: 0 },
    { productCharacteristicValue: { productCharacteristicName: "Беспроводной" }, characteristicValueBoolValue: false },
  ],
});

const vector = (x: number, y: number) => {
  const buffer = Buffer.alloc(DIMENSIONS * 4);
  buffer.writeFloatLE(x, 0); buffer.writeFloatLE(y, 4);
  return buffer;
};

const catalogEnvironment = {
  CATEGORY_PARQUETS: process.env.CATEGORY_PARQUETS,
  STE_PARQUET: process.env.STE_PARQUET,
  STE_DETAILS_PARQUET: process.env.STE_DETAILS_PARQUET,
};

before(() => {
  // Test retrieval against three fixture rows, never the full production catalog.
  process.env.CATEGORY_PARQUETS = sourcePath;
  delete process.env.STE_PARQUET;
  delete process.env.STE_DETAILS_PARQUET;
  writeSource();
  catalog = openDatabase(join(directory, "search.sqlite"));
});
after(() => {
  catalog?.sqlite.close();
  resetSteCatalog();
  for (const [key, value] of Object.entries(catalogEnvironment)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  rmSync(directory, { recursive: true });
});

test("normalize preserves zero/false characteristics and missing prices", () => {
  const product = normalizeProduct(raw(101, "Пылесос"));
  assert.equal(product.price, null);
  assert.equal(product.characteristics[0].value, "0");
  assert.equal(product.characteristics[1].value, "Нет");
  assert.equal(cleanText("<p>Привет&nbsp;мир</p>"), "Привет мир");
  assert.match(embeddingDocument(product), /^title: Пылесос \| text: Категория:/);
});

test("normalize preserves model and classification data without inventing missing values", () => {
  const base = normalizeProduct(raw(101, "Пылесос"));
  assert.deepEqual([base.modelName, base.okpd2Name, base.ktruCode, base.ktruName, base.kpgzCode, base.kpgzName, base.spgzId], ["", "", "", "", "", "", null]);
  const product = normalizeProduct({ ...raw(101, "Пылесос"), modelName: " <b>Model 15</b> ", partNumber: "A-42", production: { code: "01.19.09.01", name: " СУМКИ ПОВСЕДНЕВНЫЕ ", okpd: { code: "15.12.12.190", name: "Изделия из кожи" } }, ktru: { code: "15.12.12.190-00000001", name: " Сумка " }, spgzId: 1383539 });
  assert.equal(product.modelName, "Model 15");
  assert.equal(product.partNumber, "A-42");
  assert.equal(product.okpd2, "15.12.12.190");
  assert.equal(product.okpd2Name, "Изделия из кожи");
  assert.equal(product.ktruCode, "15.12.12.190-00000001");
  assert.equal(product.ktruName, "Сумка");
  assert.equal(product.kpgzCode, "01.19.09.01");
  assert.equal(product.kpgzName, "СУМКИ ПОВСЕДНЕВНЫЕ");
  assert.equal(product.spgzId, 1383539);
  assert.equal(normalizeProduct({ ...raw(101, "Пылесос"), production: null, spgzId: 0 }).spgzId, null);
  const fromCharacteristic = normalizeProduct({
    ...raw(101, "Пылесос"),
    skuCharacteristics: [{ productCharacteristicValue: { productCharacteristicName: "Ктру" }, characteristicValueStringValue: "31.01.12.160-00000005" }],
  });
  assert.equal(fromCharacteristic.ktruCode, "31.01.12.160-00000005");
});

test("FTS escapes punctuation and accepts Russian and part numbers", () => {
  assert.equal(ftsQuery('Пылесос OR " x-y:*'), '"пылесос"* OR "or"* OR "x"* OR "y"*');
  assert.equal(ftsQuery("***"), "");
  assert.throws(() => validateSearch({ query: "x", mode: "text", page: -1 }));
});

test("imports idempotently and searches catalog, FTS, ID, pagination, categories", async () => {
  assert.equal(importProducts(catalog, sourcePath), 3);
  assert.equal(importProducts(catalog, sourcePath), 3);
  assert.equal((catalog.sqlite.prepare("SELECT count(*) n FROM index_state").get() as { n: number }).n, 3);
  const result = await runSearch({ query: "пылес", mode: "text" }, catalog);
  assert.equal(result.total, 2);
  assert.equal(result.items[0].name.includes("Пылесос"), true);
  assert.equal((await runSearch({ query: "пылес", mode: "text", categoryId: 2 }, catalog)).total, 1);
  assert.equal((await runSearch({ query: "", mode: "text" }, catalog)).total, 3);
  assert.equal((await runSearch({ query: "101", mode: "text" }, catalog)).items[0].id, 101);
  assert.equal((await runSearch({ query: "101", mode: "text", page: 2 }, catalog)).items.length, 0);
  assert.equal((await runSearch({ query: "' OR 1=1 --", mode: "text" }, catalog)).total, 0);
  assert.equal(findProduct(101)?.characteristics[0].value, "0");
  assert.equal(findProduct(-1), null);
});

test("empty semantic index fails explicitly, without a silent fallback", async () => {
  await assert.rejects(runSearch({ query: "уборка", mode: "semantic" }, catalog), /индекс пока пуст/);
  const noEmbedding = async (): Promise<Buffer[]> => { throw new Error("Embedding should not be called"); };
  assert.deepEqual(await findSimilarProducts(101, catalog, noEmbedding), []);
});

test("sqlite-vec cosine search, metadata filters, hybrid fusion", async () => {
  const insert = catalog.sqlite.prepare("INSERT INTO product_vectors(product_id,embedding,category_id) VALUES(?,?,?)");
  insert.run(101n, vector(1, 0), 1n); insert.run(102n, vector(0, 1), 1n); insert.run(103n, vector(0.9, 0.1), 2n);
  catalog.sqlite.exec("UPDATE index_state SET embedded_hash=content_hash");
  const embed = async () => vector(1, 0);
  const semantic = await runSearch({ query: "уборка", mode: "semantic" }, catalog, embed);
  assert.equal(semantic.items[0].id, 101);
  assert.equal(semantic.items[0].score, 1);
  const filtered = await runSearch({ query: "уборка", mode: "semantic", categoryId: 2 }, catalog, embed);
  assert.deepEqual(filtered.items.map((item) => item.id), [103]);
  const hybrid = await runSearch({ query: "пылес", mode: "hybrid" }, catalog, embed);
  assert.equal(hybrid.items[0].match, "both");
  assert.equal(fuseRanks([{ id: 1, distance: 0.1 }, { id: 2, distance: 0.2 }], [{ id: 2 }, { id: 3 }])[0].id, 2);
});

test("similar products reuse document vectors, exclude the current product and rank neighbors", async () => {
  const noEmbedding = async (): Promise<Buffer[]> => { throw new Error("Stored vectors should be reused"); };
  const similar = await findSimilarProducts(101, catalog, noEmbedding);
  assert.deepEqual(similar.map((item) => item.id), [103, 102]);
  assert.ok(similar[0].score! > similar[1].score!);
  assert.equal(similar[0].match, "semantic");
  assert.deepEqual(await findSimilarProducts(-1, catalog, noEmbedding), []);
  assert.deepEqual(await findSimilarProducts(999, catalog, noEmbedding), []);
});

test("similar products encode unindexed products as documents without mutating the index", async () => {
  catalog.sqlite.prepare("DELETE FROM product_vectors WHERE product_id=?").run(101n);
  try {
    let calls = 0;
    const similar = await findSimilarProducts(101, catalog, async (texts, task) => {
      calls++;
      assert.deepEqual(texts, [embeddingDocument(findProduct(101)!)]);
      assert.equal(task, "document");
      return [vector(1, 0)];
    });
    assert.equal(calls, 1);
    assert.deepEqual(similar.map((item) => item.id), [103, 102]);
    assert.equal(catalog.sqlite.prepare("SELECT 1 FROM product_vectors WHERE product_id=?").get(101n), undefined);
  } finally {
    catalog.sqlite.prepare("INSERT INTO product_vectors(product_id,embedding,category_id) VALUES(?,?,?)").run(101n, vector(1, 0), 1n);
  }
});

test("re-import backfills classification and model and preserves vectors when embedding text unchanged", () => {
  const updated = steRows.map((row) => row.id === 102
    ? { ...row, model: "Coffee 200", okpd2_name: "Оборудование", production_code: "01.01", production_name: "ТЕХНИКА" }
    : row);
  writeSource(updated);
  const before = catalog.sqlite.prepare("SELECT embedded_hash,content_hash FROM index_state WHERE product_id=102").get();
  importProducts(catalog, sourcePath);
  const product = findProduct(102)!;
  assert.equal(product.modelName, "Coffee 200");
  assert.equal(product.okpd2Name, "Оборудование");
  assert.equal(product.kpgzCode, "01.01");
  assert.equal(product.kpgzName, "ТЕХНИКА");
  assert.deepEqual(catalog.sqlite.prepare("SELECT embedded_hash,content_hash FROM index_state WHERE product_id=102").get(), before);
  assert.equal((catalog.sqlite.prepare("SELECT count(*) n FROM product_vectors").get() as { n: number }).n, 3);
});

test("re-import preserves vectors and invalidates only changed embedding content", () => {
  importProducts(catalog, sourcePath);
  assert.equal((catalog.sqlite.prepare("SELECT count(*) n FROM product_vectors").get() as { n: number }).n, 3);
  const updated = steRows.map((row) => row.id === 101 ? { ...row, name: "Совершенно другой товар", model: "Coffee 200", okpd2_name: "Оборудование", production_code: "01.01", production_name: "ТЕХНИКА" } : row.id === 102 ? { ...row, model: "Coffee 200", okpd2_name: "Оборудование", production_code: "01.01", production_name: "ТЕХНИКА" } : row);
  writeSource(updated);
  importProducts(catalog, sourcePath);
  assert.equal((catalog.sqlite.prepare("SELECT embedded_hash from index_state WHERE product_id=101").get() as { embedded_hash: string | null }).embedded_hash, null);
  assert.equal((catalog.sqlite.prepare("SELECT count(*) n FROM product_vectors").get() as { n: number }).n, 2);
  assert.equal((catalog.sqlite.prepare("SELECT rowid FROM products_fts WHERE products_fts MATCH 'совершенно'").get() as { rowid: number }).rowid, 101);
  assert.throws(() => importProducts(catalog, join(directory, "missing.parquet")));
});

test("meow-embed float32 base64 decoding validates contract", () => {
  const bytes = vector(1, 2);
  const dense = { model_id: MODEL_ID, dtype: "float32", shape: [1, DIMENSIONS], data: bytes.toString("base64") };
  assert.deepEqual(decodeDense({ dense }, 1)[0], bytes);
  assert.throws(() => decodeDense({ dense: { ...dense, data: "" } }, 1), /неполный/);
  assert.throws(() => decodeDense({ dense: { ...dense, shape: [1, 256] } }, 1), /размерность/);
  bytes.writeFloatLE(NaN, 0);
  assert.throws(() => decodeDense({ dense: { ...dense, data: bytes.toString("base64") } }, 1), /нечисловой/);
});
