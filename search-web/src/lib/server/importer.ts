import Database from "better-sqlite3";
import { sql } from "drizzle-orm";
import type { CatalogDatabase } from "./database";
import { documentHash, embeddingDocument, normalizeProduct, type SourceCard } from "./normalize";
import { products } from "./schema";

interface SourceRow { sku_id: number; fetched_at: string; payload: string }

export function importProducts(catalog: CatalogDatabase, sourcePath: string, onProgress: (count: number) => void = () => {}): number {
  // The downloader owns this database. Never migrate it or change its journal mode.
  const source = new Database(sourcePath, { readonly: true, fileMustExist: true });
  source.pragma("busy_timeout = 10000");
  let imported = 0;
  let skipped = 0;
  let batch: SourceRow[] = [];
  const flush = () => {
    catalog.db.transaction((tx) => {
      const existing = catalog.sqlite.prepare("SELECT content_hash AS hash FROM products WHERE id=?");
      const removeVector = catalog.sqlite.prepare("DELETE FROM product_vectors WHERE product_id=?");
      for (const row of batch) {
        const card = JSON.parse(row.payload) as SourceCard & { _fetchError?: unknown };
        // The downloader stores explicit HTTP-error checkpoints alongside cards.
        if (card._fetchError) { skipped++; continue; }
        const product = normalizeProduct(card);
        if (product.id !== row.sku_id) throw new Error(`ID карточки не совпадает: ${row.sku_id}`);
        const document = embeddingDocument(product);
        const contentHash = documentHash(document);
        const previous = existing.get(product.id) as { hash: string } | undefined;
        if (previous && previous.hash !== contentHash) removeVector.run(BigInt(product.id));
        const values = { ...product, sourceFetchedAt: row.fetched_at, document, contentHash };
        tx.insert(products).values(values).onConflictDoUpdate({
          target: products.id,
          set: {
            name: sql`excluded.name`, description: sql`excluded.description`, category: sql`excluded.category`,
            categoryId: sql`excluded.category_id`, manufacturer: sql`excluded.manufacturer`, country: sql`excluded.country`,
            unit: sql`excluded.unit`, partNumber: sql`excluded.part_number`, modelName: sql`excluded.model_name`,
            okpd2: sql`excluded.okpd2`, okpd2Name: sql`excluded.okpd2_name`,
            kpgzCode: sql`excluded.kpgz_code`, kpgzName: sql`excluded.kpgz_name`, spgzId: sql`excluded.spgz_id`,
            price: sql`excluded.price`, imageIds: sql`excluded.image_ids`, characteristics: sql`excluded.characteristics`,
            sourceUpdatedAt: sql`excluded.source_updated_at`, sourceFetchedAt: sql`excluded.source_fetched_at`,
            document: sql`excluded.document`, contentHash: sql`excluded.content_hash`,
            embeddedHash: sql`CASE WHEN products.content_hash = excluded.content_hash THEN products.embedded_hash ELSE NULL END`,
          },
          setWhere: sql`${products.sourceFetchedAt} <> excluded.source_fetched_at OR ${products.contentHash} <> excluded.content_hash
            OR ${products.modelName} IS NOT excluded.model_name OR ${products.okpd2Name} IS NOT excluded.okpd2_name
            OR ${products.kpgzCode} IS NOT excluded.kpgz_code OR ${products.kpgzName} IS NOT excluded.kpgz_name
            OR ${products.spgzId} IS NOT excluded.spgz_id`,
        }).run();
      }
    }, { behavior: "immediate" });
    imported += batch.length;
    onProgress(imported);
    batch = [];
  };
  try {
    // Fetch at most 500 rows per cursor: avoids holding a read transaction on the
    // downloader's WAL for the whole import, and allows checkpointing to continue.
    const rows = source.prepare("SELECT sku_id,fetched_at,payload FROM sku_details WHERE sku_id > ? ORDER BY sku_id LIMIT 500");
    let cursor = 0;
    while (true) {
      batch = rows.all(cursor) as SourceRow[];
      if (!batch.length) break;
      cursor = batch[batch.length - 1].sku_id;
      flush();
    }
    catalog.sqlite.prepare("INSERT INTO metadata(key,value) VALUES('last_imported_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").run(new Date().toISOString());
    if (skipped) console.log(`Пропущено ${skipped} записей с ошибками загрузки источника`);
    return imported - skipped;
  } finally {
    source.close();
  }
}
