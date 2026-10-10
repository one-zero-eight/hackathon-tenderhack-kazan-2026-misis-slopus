import { existsSync, statSync } from "node:fs";
import pl from "nodejs-polars";
import type { Characteristic, Product } from "../types";
import { catalogParquetPaths } from "./config";
import { documentHash, embeddingDocument } from "./normalize";

export interface SteParquetRow {
  id: number;
  name: string | null;
  description: string | null;
  manufacturer: string | null;
  okei: string | null;
  oksm: string | null;
  okpd2_code: string | null;
  okpd2_name: string | null;
  production_code: string | null;
  production_name: string | null;
  category_id: number | null;
  category: string | null;
  model: string | null;
  barcode: string | null;
  median_price: number | null;
  reference_price: number | null;
  cost_per_unit: number | null;
  sku_image_ids: number[] | null;
  characteristics: Array<{
    name: string | null;
    value: string | null;
    unit: string | null;
  }> | null;
  created_date: string | null;
}

interface SteCatalog {
  fingerprint: string;
  frame: ReturnType<typeof pl.readParquet>;
  imageIds: Set<number>;
  categoryStats: { id: number; name: string; count: number }[];
}

let catalog: SteCatalog | undefined;

export function resetSteCatalog() {
  catalog = undefined;
}

const STE_COLUMNS = [
  "id", "name", "description", "manufacturer", "okei", "oksm",
  "okpd2_code", "okpd2_name", "production_code", "production_name",
  "category_id", "category", "model", "barcode",
  "median_price", "reference_price", "cost_per_unit",
  "sku_image_ids", "characteristics", "created_date",
] as const;

function buildImageIds(frame: ReturnType<typeof pl.readParquet>): Set<number> {
  const ids = new Set<number>();
  const column = frame.getColumn("sku_image_ids").toArray() as unknown as Array<number[] | null>;
  for (const list of column) {
    if (!list) continue;
    for (const id of list) {
      if (Number.isSafeInteger(id) && id > 0) ids.add(id);
    }
  }
  return ids;
}

function buildCategoryStats(frame: ReturnType<typeof pl.readParquet>): { id: number; name: string; count: number }[] {
  const grouped = frame
    .groupBy("category_id", "category")
    .agg(pl.count("id").alias("count"))
    .sort(["count", "category"], [true, false])
    .toRecords() as { category_id: number | null; category: string | null; count: number }[];
  return grouped
    .filter((row) => row.category_id != null)
    .map((row) => ({ id: Number(row.category_id), name: row.category || "Без категории", count: Number(row.count) }));
}

function pathsFingerprint(paths: string[]): string {
  return paths.map((path) => `${path}:${existsSync(path) ? Math.trunc(statSync(path).mtimeMs) : -1}`).join("|");
}

function loadCatalog(): SteCatalog {
  const paths = catalogParquetPaths();
  const missing = paths.filter((path) => !existsSync(path));
  if (missing.length) throw new Error(`Не найдены parquet каталога: ${missing.join(", ")}`);
  const frames = paths.map((path) => pl.readParquet(path, { columns: [...STE_COLUMNS] }));
  // First file wins on duplicate ids (categories before optional ste).
  const frame = pl.concat(frames).unique({ subset: ["id"], maintainOrder: true });
  return {
    fingerprint: pathsFingerprint(paths),
    frame,
    imageIds: buildImageIds(frame),
    categoryStats: buildCategoryStats(frame),
  };
}

export function getSteCatalog(): SteCatalog {
  const fingerprint = pathsFingerprint(catalogParquetPaths());
  if (!catalog || catalog.fingerprint !== fingerprint) catalog = loadCatalog();
  return catalog;
}

export function steRowToProduct(row: SteParquetRow): Product {
  if (!Number.isSafeInteger(row.id) || row.id <= 0 || typeof row.name !== "string" || !row.name.trim()) {
    throw new Error(`Некорректная строка СТЕ: ${row.id}`);
  }
  const characteristics: Characteristic[] = (row.characteristics ?? []).flatMap((item) => {
    if (!item) return [];
    const name = (item.name ?? "").trim();
    const value = item.value;
    if (!name || value === null || value === undefined || value === "") return [];
    return [{ name, value: String(value), unit: (item.unit ?? "").trim() }];
  });
  const price = [row.reference_price, row.median_price, row.cost_per_unit]
    .find((value) => typeof value === "number" && Number.isFinite(value) && value > 0) ?? null;
  const ktruCharacteristic = characteristics.find((item) => item.name.toLocaleLowerCase("ru") === "ктру");
  return {
    id: row.id,
    name: row.name.trim(),
    description: (row.description ?? "").trim(),
    category: (row.category ?? "").trim() || "Без категории",
    categoryId: row.category_id ?? 0,
    manufacturer: (row.manufacturer ?? "").trim(),
    country: (row.oksm ?? "").trim(),
    unit: (row.okei ?? "").trim(),
    partNumber: (row.barcode ?? "").trim(),
    modelName: (row.model ?? "").trim(),
    okpd2: (row.okpd2_code ?? "").trim(),
    okpd2Name: (row.okpd2_name ?? "").trim(),
    ktruCode: (ktruCharacteristic?.value ?? "").trim(),
    ktruName: "",
    kpgzCode: (row.production_code ?? "").trim(),
    kpgzName: (row.production_name ?? "").trim(),
    spgzId: null,
    price,
    imageIds: [...new Set((row.sku_image_ids ?? []).filter((id) => Number.isSafeInteger(id) && id > 0))],
    characteristics,
    sourceUpdatedAt: row.created_date ?? "",
  };
}

export function hydrateProducts(ids: number[]): Product[] {
  if (!ids.length) return [];
  const { frame } = getSteCatalog();
  const rows = frame.filter(pl.col("id").isIn(pl.lit(ids))).toRecords() as unknown as SteParquetRow[];
  const lookup = new Map(rows.map((row) => [row.id, steRowToProduct(row)]));
  return ids.flatMap((id) => {
    const product = lookup.get(id);
    return product ? [product] : [];
  });
}

export function findSteProduct(id: number): Product | null {
  if (!Number.isSafeInteger(id) || id <= 0) return null;
  return hydrateProducts([id])[0] ?? null;
}

export function listSteProducts(options: { categoryId?: number; offset: number; limit: number }): { items: Product[]; total: number } {
  const { frame } = getSteCatalog();
  let filtered = frame;
  if (options.categoryId !== undefined) {
    filtered = filtered.filter(pl.col("category_id").eq(pl.lit(options.categoryId)));
  }
  const total = filtered.height;
  const page = filtered.sort("id").slice(options.offset, options.limit);
  const items = (page.toRecords() as unknown as SteParquetRow[]).map(steRowToProduct);
  return { items, total };
}

export function steHasImageId(imageId: number): boolean {
  return getSteCatalog().imageIds.has(imageId);
}

export function steCategoryStats() {
  return getSteCatalog().categoryStats;
}

export function steProductCount() {
  return getSteCatalog().frame.height;
}

export function* iterateSteProducts(batchSize = 500): Generator<Product[]> {
  const { frame } = getSteCatalog();
  for (let offset = 0; offset < frame.height; offset += batchSize) {
    const rows = frame.slice(offset, batchSize).toRecords() as unknown as SteParquetRow[];
    yield rows.map(steRowToProduct);
  }
}

export function productIndexFields(product: Product) {
  const document = embeddingDocument(product);
  return {
    document,
    contentHash: documentHash(document),
    fts: {
      name: product.name,
      manufacturer: product.manufacturer,
      part_number: product.partNumber,
      description: product.description,
      category: product.category,
      characteristics: JSON.stringify(product.characteristics),
    },
  };
}
