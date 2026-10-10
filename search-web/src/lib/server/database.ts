import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import Database from "better-sqlite3";
import { drizzle } from "drizzle-orm/better-sqlite3";
import { migrate } from "drizzle-orm/better-sqlite3/migrator";
import * as sqliteVec from "sqlite-vec";
import { DIMENSIONS, INDEX_SCHEMA_VERSION, MODEL_ID, searchDatabasePath } from "./config";
import * as schema from "./schema";

/** search-api builds metadata/index_state without drizzle; mark journal entries applied so migrate is a no-op. */
function syncDrizzleJournal(sqlite: Database.Database, migrationsFolder: string) {
  const hasMetadata = sqlite.prepare("SELECT 1 AS ok FROM sqlite_master WHERE type='table' AND name='metadata'").get();
  const hasIndexState = sqlite.prepare("SELECT 1 AS ok FROM sqlite_master WHERE type='table' AND name='index_state'").get();
  if (!hasMetadata || !hasIndexState) return;

  const journalPath = join(migrationsFolder, "meta/_journal.json");
  if (!existsSync(journalPath)) return;
  const journal = JSON.parse(readFileSync(journalPath, "utf8")) as {
    entries: { tag: string; when: number }[];
  };

  sqlite.exec(`
    CREATE TABLE IF NOT EXISTS __drizzle_migrations (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      hash text NOT NULL,
      created_at numeric
    )
  `);
  const last = sqlite.prepare("SELECT created_at FROM __drizzle_migrations ORDER BY created_at DESC LIMIT 1").get() as
    | { created_at: number | string } | undefined;
  const lastAt = last ? Number(last.created_at) : -1;
  const insert = sqlite.prepare(
    "INSERT INTO __drizzle_migrations (hash, created_at) SELECT ?, ? WHERE NOT EXISTS (SELECT 1 FROM __drizzle_migrations WHERE hash = ?)",
  );
  for (const entry of journal.entries) {
    if (lastAt >= entry.when) continue;
    const sql = readFileSync(join(migrationsFolder, `${entry.tag}.sql`), "utf8");
    const hash = createHash("sha256").update(sql).digest("hex");
    insert.run(hash, entry.when, hash);
  }
}

export function openDatabase(path = searchDatabasePath()) {
  mkdirSync(dirname(path), { recursive: true });
  const sqlite = new Database(path);
  sqlite.pragma("journal_mode = WAL");
  sqlite.pragma("busy_timeout = 10000");
  sqlite.pragma("foreign_keys = ON");
  sqliteVec.load(sqlite);
  const db = drizzle(sqlite, { schema });
  const migrationsFolder = resolve("drizzle");
  syncDrizzleJournal(sqlite, migrationsFolder);
  migrate(db, { migrationsFolder });

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
