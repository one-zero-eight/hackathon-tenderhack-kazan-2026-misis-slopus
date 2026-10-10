import { spawnSync } from "node:child_process";
import { existsSync, realpathSync, statSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import Database from "better-sqlite3";
import { catalogDatabasePath, catalogParquetPaths, kpgzParquetPath } from "./config";

interface SteCatalog {
  fingerprint: string;
  db: Database.Database;
}

let catalog: SteCatalog | undefined;
const CATALOG_SCHEMA = "sqlite-catalog-v2";

export function resetSteCatalog() {
  catalog?.db.close();
  catalog = undefined;
}

function pathsFingerprint(paths: string[]): string {
  return JSON.stringify([...paths, kpgzParquetPath()].map(path => {
    const stat = statSync(path, { bigint: true });
    return [realpathSync(path), String(stat.mtimeNs), String(stat.size)];
  }));
}

export function getSteCatalog(): SteCatalog {
  const paths = catalogParquetPaths();
  const fingerprint = pathsFingerprint(paths);
  if (catalog?.fingerprint === fingerprint) return catalog;
  resetSteCatalog();
  const sqlite = catalogDatabasePath();
  let valid = false;
  if (existsSync(sqlite)) {
    const probe = new Database(sqlite, { readonly: true, fileMustExist: true });
    const meta = probe.prepare("SELECT key,value FROM metadata").all() as { key: string; value: string }[];
    probe.close();
    const values = new Map(meta.map(row => [row.key, row.value]));
    valid = values.get("schema") === CATALOG_SCHEMA && values.get("fingerprint") === fingerprint;
  }
  if (!valid) {
    const root = resolve(dirname(fileURLToPath(import.meta.url)), "../../../..");
    const result = spawnSync("uv", ["run", "python", "-m", "search_api.ste_catalog",
      "--output", sqlite, "--kpgz", kpgzParquetPath(),
      ...paths.flatMap(path => ["--source", path])], { cwd: resolve(root, "search-api"), encoding: "utf8" });
    if (result.status !== 0) throw new Error(`Не удалось построить каталог: ${result.stderr || result.stdout}`);
  }
  const db = new Database(sqlite, { readonly: true, fileMustExist: true });
  db.pragma("mmap_size = 1073741824");
  db.pragma("cache_size = -2048");
  db.pragma("temp_store = FILE");
  db.pragma("query_only = ON");
  catalog = { fingerprint, db };
  return catalog;
}
