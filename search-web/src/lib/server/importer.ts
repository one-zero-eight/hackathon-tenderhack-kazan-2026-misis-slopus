import { existsSync } from "node:fs";
import { sql } from "drizzle-orm";
import { steParquetPath } from "./config";
import type { CatalogDatabase } from "./database";
import { indexState } from "./schema";
import { iterateSteProducts, productIndexFields, resetSteCatalog } from "./ste-catalog";

export function importProducts(catalog: CatalogDatabase, sourcePath = steParquetPath(), onProgress: (count: number) => void = () => {}): number {
  if (!existsSync(sourcePath)) throw new Error(`Не найден parquet СТЕ: ${sourcePath}`);
  process.env.STE_PARQUET = sourcePath;
  resetSteCatalog();

  let imported = 0;
  const upsertFts = catalog.sqlite.prepare(`
    INSERT INTO products_fts(rowid,name,manufacturer,part_number,description,category,characteristics)
    VALUES (@id,@name,@manufacturer,@part_number,@description,@category,@characteristics)
  `);
  const deleteFts = catalog.sqlite.prepare("DELETE FROM products_fts WHERE rowid=?");
  const existing = catalog.sqlite.prepare("SELECT content_hash AS hash, category_id AS categoryId FROM index_state WHERE product_id=?");
  const removeVector = catalog.sqlite.prepare("DELETE FROM product_vectors WHERE product_id=?");

  for (const batch of iterateSteProducts(500)) {
    catalog.db.transaction((tx) => {
      for (const product of batch) {
        const { contentHash, fts } = productIndexFields(product);
        const previous = existing.get(product.id) as { hash: string; categoryId: number } | undefined;
        if (previous?.hash === contentHash && previous.categoryId === product.categoryId) continue;
        if (previous) {
          deleteFts.run(product.id);
          removeVector.run(BigInt(product.id));
        }
        upsertFts.run({
          id: product.id,
          name: fts.name,
          manufacturer: fts.manufacturer,
          part_number: fts.part_number,
          description: fts.description,
          category: fts.category,
          characteristics: fts.characteristics,
        });
        tx.insert(indexState).values({
          productId: product.id,
          contentHash,
          embeddedHash: null,
          categoryId: product.categoryId,
        }).onConflictDoUpdate({
          target: indexState.productId,
          set: {
            contentHash: sql`excluded.content_hash`,
            categoryId: sql`excluded.category_id`,
            embeddedHash: sql`CASE WHEN index_state.content_hash = excluded.content_hash THEN index_state.embedded_hash ELSE NULL END`,
          },
        }).run();
      }
    }, { behavior: "immediate" });
    imported += batch.length;
    onProgress(imported);
  }

  catalog.sqlite.prepare("INSERT INTO metadata(key,value) VALUES('last_imported_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    .run(new Date().toISOString());
  catalog.sqlite.prepare("INSERT INTO metadata(key,value) VALUES('ste_parquet',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    .run(sourcePath);
  return imported;
}
