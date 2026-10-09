import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import Database from "better-sqlite3";
import { drizzle } from "drizzle-orm/better-sqlite3";
import { migrate } from "drizzle-orm/better-sqlite3/migrator";
import * as sqliteVec from "sqlite-vec";
import { DIMENSIONS, MODEL_ID, searchDatabasePath } from "./config";
import * as schema from "./schema";

export function openDatabase(path = searchDatabasePath()) {
  mkdirSync(dirname(path), { recursive: true });
  const sqlite = new Database(path);
  sqlite.pragma("journal_mode = WAL");
  sqlite.pragma("busy_timeout = 10000");
  sqlite.pragma("foreign_keys = ON");
  sqliteVec.load(sqlite);
  const db = drizzle(sqlite, { schema });
  migrate(db, { migrationsFolder: resolve("drizzle") });
  // Virtual tables are not represented by Drizzle's schema DSL.
  sqlite.exec(`
    CREATE VIRTUAL TABLE IF NOT EXISTS product_vectors USING vec0(
      product_id INTEGER PRIMARY KEY,
      embedding FLOAT[${DIMENSIONS}] distance_metric=cosine,
      category_id INTEGER
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS products_fts USING fts5(
      name, manufacturer, part_number, description, category, characteristics,
      content='products', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
    );
    CREATE TRIGGER IF NOT EXISTS products_fts_insert AFTER INSERT ON products BEGIN
      INSERT INTO products_fts(rowid,name,manufacturer,part_number,description,category,characteristics)
      VALUES(new.id,new.name,new.manufacturer,new.part_number,new.description,new.category,new.characteristics);
    END;
    CREATE TRIGGER IF NOT EXISTS products_fts_delete AFTER DELETE ON products BEGIN
      INSERT INTO products_fts(products_fts,rowid,name,manufacturer,part_number,description,category,characteristics)
      VALUES('delete',old.id,old.name,old.manufacturer,old.part_number,old.description,old.category,old.characteristics);
      DELETE FROM product_vectors WHERE product_id=old.id;
    END;
    CREATE TRIGGER IF NOT EXISTS products_fts_update AFTER UPDATE OF name,manufacturer,part_number,description,category,characteristics ON products BEGIN
      INSERT INTO products_fts(products_fts,rowid,name,manufacturer,part_number,description,category,characteristics)
      VALUES('delete',old.id,old.name,old.manufacturer,old.part_number,old.description,old.category,old.characteristics);
      INSERT INTO products_fts(rowid,name,manufacturer,part_number,description,category,characteristics)
      VALUES(new.id,new.name,new.manufacturer,new.part_number,new.description,new.category,new.characteristics);
    END;
  `);
  const signature = `${MODEL_ID}:${DIMENSIONS}:document-v1`;
  const stored = sqlite.prepare("SELECT value FROM metadata WHERE key='embedding_signature'").get() as { value: string } | undefined;
  if (stored && stored.value !== signature) {
    sqlite.close();
    throw new Error("Индекс создан для другой модели. Создайте новую SEARCH_DATABASE.");
  }
  sqlite.prepare("INSERT INTO metadata(key,value) VALUES('embedding_signature',?) ON CONFLICT(key) DO NOTHING").run(signature);
  return { sqlite, db };
}

export type CatalogDatabase = ReturnType<typeof openDatabase>;
let catalog: CatalogDatabase | undefined;
export function getDatabase(): CatalogDatabase {
  return catalog ??= openDatabase();
}
