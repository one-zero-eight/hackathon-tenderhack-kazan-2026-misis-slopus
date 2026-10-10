import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import Database from "better-sqlite3";
import { drizzle } from "drizzle-orm/better-sqlite3";
import { migrate } from "drizzle-orm/better-sqlite3/migrator";
import * as sqliteVec from "sqlite-vec";
import { DIMENSIONS, INDEX_SCHEMA_VERSION, MODEL_ID, searchDatabasePath } from "./config";
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

  const stored = sqlite.prepare("SELECT value FROM metadata WHERE key='index_schema_version'").get() as { value: string } | undefined;
  if (stored && stored.value !== INDEX_SCHEMA_VERSION) {
    sqlite.close();
    throw new Error(`Устаревшая SEARCH_DATABASE (ожидается ${INDEX_SCHEMA_VERSION}). Удалите ${path} и выполните uv run zakupki-search-index в search-api`);
  }

  // Drop legacy FTS shapes (content-synced products table or older column lists).
  const ftsSql = (sqlite.prepare("SELECT sql FROM sqlite_master WHERE name='products_fts'").get() as { sql: string } | undefined)?.sql ?? "";
  if (ftsSql && (!ftsSql.includes("content=''") || ftsSql.includes("category_id"))) {
    sqlite.exec("DROP TABLE IF EXISTS products_fts");
  }

  sqlite.exec(`
    CREATE VIRTUAL TABLE IF NOT EXISTS product_vectors USING vec0(
      product_id INTEGER PRIMARY KEY,
      embedding FLOAT[${DIMENSIONS}] distance_metric=cosine,
      category_id INTEGER
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS products_fts USING fts5(
      name, manufacturer, part_number, description, category, characteristics,
      content='', contentless_delete=1,
      tokenize='unicode61 remove_diacritics 2'
    );
  `);

  const signature = `${MODEL_ID}:${DIMENSIONS}:search-result:document-v2`;
  const embeddingSignature = sqlite.prepare("SELECT value FROM metadata WHERE key='embedding_signature'").get() as { value: string } | undefined;
  if (embeddingSignature && embeddingSignature.value !== signature) {
    sqlite.close();
    throw new Error("Индекс создан для другой модели. Создайте новую SEARCH_DATABASE.");
  }
  sqlite.prepare("INSERT INTO metadata(key,value) VALUES('embedding_signature',?) ON CONFLICT(key) DO NOTHING").run(signature);
  sqlite.prepare("INSERT INTO metadata(key,value) VALUES('index_schema_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").run(INDEX_SCHEMA_VERSION);
  return { sqlite, db };
}

export type CatalogDatabase = ReturnType<typeof openDatabase>;
let catalog: CatalogDatabase | undefined;
export function getDatabase(): CatalogDatabase {
  return catalog ??= openDatabase();
}
