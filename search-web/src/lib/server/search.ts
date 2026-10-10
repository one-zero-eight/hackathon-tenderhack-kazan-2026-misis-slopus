import type { CatalogStats, Product, ProductHit, SearchInput, SearchResult } from "../types";
import { DIMENSIONS, MODEL_ID, PAGE_SIZE, RETRIEVAL_LIMIT } from "./config";
import { getDatabase, type CatalogDatabase } from "./database";
import { embedQuery, embedTexts } from "./embeddings";
import { getKtruNodeByCode } from "./ktru-tree";
import { embeddingDocument } from "./normalize";
import { findSteProduct, hydrateProducts, listSteProducts, steCategoryStats, steProductCount } from "./ste-catalog";

function withResolvedKtru(product: Product): Product {
  if (product.ktruCode) return product;
  const characteristic = product.characteristics.find((item) => {
    const name = item.name.toLocaleLowerCase("ru");
    return name === "ктру" || name.startsWith("ктру ");
  });
  return characteristic?.value ? { ...product, ktruCode: characteristic.value.trim() } : product;
}

function resolveProduct(product: Product): Product {
  const resolved = withResolvedKtru(product);
  if (!resolved.ktruCode || resolved.ktruName) return resolved;
  return { ...resolved, ktruName: getKtruNodeByCode(resolved.ktruCode)?.name ?? "" };
}

export function validateSearch(input: SearchInput): Required<Omit<SearchInput, "categoryId">> & { categoryId?: number } {
  if (!input || typeof input.query !== "string" || input.query.length > 500) throw new Error("Запрос должен содержать не более 500 символов");
  if (!["text", "semantic", "hybrid"].includes(input.mode)) throw new Error("Неизвестный режим поиска");
  const page = input.page ?? 1;
  if (!Number.isSafeInteger(page) || page < 1 || page > 10000) throw new Error("Некорректная страница");
  if (input.categoryId !== undefined && (!Number.isSafeInteger(input.categoryId) || input.categoryId < 0)) throw new Error("Некорректная категория");
  return { query: input.query.trim(), mode: input.mode, categoryId: input.categoryId, page };
}

export function ftsQuery(query: string): string {
  return [...new Set(query.toLocaleLowerCase("ru").match(/[\p{L}\p{N}]+/gu) ?? [])].slice(0, 32).map((token) => `"${token}"*`).join(" OR ");
}

interface RankedRow { id: number; score: number | null; match: ProductHit["match"] }
interface VectorRow { id: number; distance: number }

export function fuseRanks(semantic: VectorRow[], textual: { id: number }[]): RankedRow[] {
  const ranks = new Map<number, { rank: number; score: number | null; match: ProductHit["match"] }>();
  semantic.forEach((item, index) => ranks.set(item.id, { rank: 1 / (60 + index + 1), score: 1 - item.distance, match: "semantic" }));
  textual.forEach((item, index) => {
    const existing = ranks.get(item.id);
    ranks.set(item.id, { rank: (existing?.rank ?? 0) + 1 / (60 + index + 1), score: existing?.score ?? null, match: existing ? "both" : "text" });
  });
  return [...ranks].sort((a, b) => b[1].rank - a[1].rank || a[0] - b[0]).map(([id, item]) => ({ id, score: item.score, match: item.match }));
}

function hydrate(ranked: RankedRow[]): ProductHit[] {
  if (!ranked.length) return [];
  const products = hydrateProducts(ranked.map((row) => row.id));
  const lookup = new Map(products.map((product) => [product.id, resolveProduct(product)]));
  return ranked.flatMap((row) => {
    const product = lookup.get(row.id);
    return product ? [{ ...product, score: row.score, match: row.match }] : [];
  });
}

export async function runSearch(input: SearchInput, catalog = getDatabase(), embed = embedQuery): Promise<SearchResult> {
  const { query, mode, categoryId, page } = validateSearch(input);
  const started = performance.now();
  const offset = (page - 1) * PAGE_SIZE;
  const finish = (items: ProductHit[], total: number): SearchResult => ({
    items, total, hasMore: offset + items.length < total, page, mode, durationMs: Math.round(performance.now() - started),
  });
  if (!query) {
    const { items, total } = listSteProducts({ categoryId, offset, limit: PAGE_SIZE });
    return finish(items.map((product) => ({ ...resolveProduct(product), score: null, match: "catalog" })), total);
  }
  if (/^\d+$/.test(query)) {
    const item = findSteProduct(Number(query));
    if (item && (categoryId === undefined || item.categoryId === categoryId)) {
      return finish(page === 1 ? [{ ...resolveProduct(item), score: null, match: "text" }] : [], 1);
    }
  }
  const tokens = ftsQuery(query);
  const categorySql = categoryId === undefined ? "" : " AND s.category_id = ?";
  const params = categoryId === undefined ? [tokens] : [tokens, categoryId];
  const textRows = (limit: number, skip = 0) => tokens ? catalog.sqlite.prepare(`
    SELECT products_fts.rowid AS id FROM products_fts
    JOIN index_state s ON s.product_id = products_fts.rowid
    WHERE products_fts MATCH ?${categorySql}
    ORDER BY bm25(products_fts,8,3,4,1,2,1), products_fts.rowid LIMIT ? OFFSET ?
  `).all(...params, limit, skip) as { id: number }[] : [];
  if (mode === "text") {
    const total = tokens
      ? (catalog.sqlite.prepare(`
        SELECT count(*) AS n FROM products_fts
        JOIN index_state s ON s.product_id = products_fts.rowid
        WHERE products_fts MATCH ?${categorySql}
      `).get(...params) as { n: number }).n
      : 0;
    return finish(hydrate(textRows(PAGE_SIZE, offset).map((row) => ({ ...row, score: null, match: "text" as const }))), total);
  }
  const ready = catalog.sqlite.prepare("SELECT 1 FROM product_vectors LIMIT 1").get();
  if (!ready) throw new Error("Семантический индекс пока пуст. Запустите uv run zakupki-search-index embed в search-api; поиск по словам доступен отдельно.");
  const vector = await embed(query);
  const vectorRows = catalog.sqlite.prepare(`
    SELECT product_id AS id,distance FROM product_vectors
    WHERE embedding MATCH ? AND k = ?${categoryId === undefined ? "" : " AND category_id = ?"}
    ORDER BY distance
  `).all(...(categoryId === undefined ? [vector, BigInt(RETRIEVAL_LIMIT)] : [vector, BigInt(RETRIEVAL_LIMIT), BigInt(categoryId)])) as VectorRow[];
  const ranked = mode === "hybrid"
    ? fuseRanks(vectorRows, textRows(RETRIEVAL_LIMIT)).slice(0, RETRIEVAL_LIMIT)
    : vectorRows.map((row): RankedRow => ({ id: row.id, score: 1 - row.distance, match: "semantic" }));
  return finish(hydrate(ranked.slice(offset, offset + PAGE_SIZE)), ranked.length);
}

export function findProduct(id: number): Product | null {
  if (!Number.isSafeInteger(id) || id <= 0) return null;
  const product = findSteProduct(id);
  return product ? resolveProduct(product) : null;
}

export async function findSimilarProducts(id: number, catalog = getDatabase(), embed = embedTexts): Promise<ProductHit[]> {
  const product = findSteProduct(id);
  if (!product || !catalog.sqlite.prepare("SELECT 1 FROM product_vectors LIMIT 1").get()) return [];
  const stored = catalog.sqlite.prepare("SELECT embedding FROM product_vectors WHERE product_id = ?").get(BigInt(id)) as { embedding: Buffer } | undefined;
  const vector = stored?.embedding ?? (await embed([embeddingDocument(product)], "document"))[0];
  const neighbors = catalog.sqlite.prepare(`
    SELECT product_id AS id, distance FROM product_vectors
    WHERE embedding MATCH ? AND k = ? ORDER BY distance
  `).all(vector, 7n) as VectorRow[];
  return hydrate(neighbors.filter((row) => row.id !== id).slice(0, 6).map((row) => ({ id: row.id, score: 1 - row.distance, match: "semantic" })));
}

let statsCache: { value: CatalogStats; expires: number } | undefined;
export function catalogStats(catalog = getDatabase()): CatalogStats {
  if (statsCache && statsCache.expires > Date.now()) return statsCache.value;
  const embedded = (catalog.sqlite.prepare("SELECT count(*) AS n FROM index_state WHERE embedded_hash IS NOT NULL AND embedded_hash = content_hash").get() as { n: number }).n;
  const lastImported = catalog.sqlite.prepare("SELECT value FROM metadata WHERE key='last_imported_at'").get() as { value: string } | undefined;
  const value = {
    products: steProductCount(),
    embedded,
    categories: steCategoryStats(),
    categoryTree: [] as CatalogStats["categoryTree"],
    model: MODEL_ID,
    dimensions: DIMENSIONS,
    lastImportedAt: lastImported?.value ?? null,
  };
  statsCache = { value, expires: Date.now() + 10000 };
  return value;
}
