import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, statSync } from "node:fs";
import { gunzipSync } from "node:zlib";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import Database from "better-sqlite3";
import type { KtruChildrenPage, KtruTreeNode } from "../types";
import { ktruParquetPath, ktruSqlitePath } from "./config";

interface LightRow {
  code: string;
  name: string;
  status: string | null;
  okpd2: string | null;
  okpd2Name: string | null;
  parentCode: string | null;
  isTemplate: number;
}

interface Catalog {
  db: Database.Database;
  okpdNames: Map<string, string>;
  okpdChildCodes: Map<string, string[]>;
  okpdKtruRoots: Map<string, string[]>;
  ktruChildren: Map<string, string[]>;
  byCode: Map<string, LightRow>;
  total: number;
}

const CHILDREN_PAGE = 200;
const SEARCH_LIMIT = 40;
let catalog: Catalog | undefined;

function repoRoot(): string {
  return resolve(dirname(fileURLToPath(import.meta.url)), "../../../..");
}

function ensureSqlite(): string {
  const parquet = ktruParquetPath();
  const sqlite = ktruSqlitePath();
  if (!existsSync(parquet)) throw new Error(`Не найден файл КТРУ: ${parquet}`);
  const parquetMtime = String(Math.trunc(statSync(parquet).mtimeMs));
  const needsBuild = !existsSync(sqlite) || (() => {
    try {
      const db = new Database(sqlite, { readonly: true, fileMustExist: true });
      const stored = db.prepare("SELECT value FROM meta WHERE key='source_mtime_ms'").get() as { value: string } | undefined;
      db.close();
      return stored?.value !== parquetMtime;
    } catch {
      return true;
    }
  })();
  if (!needsBuild) return sqlite;
  const script = resolve(repoRoot(), "scripts/build_ktru_sqlite.py");
  const result = spawnSync("uv", ["run", "--with", "pyarrow", "python", script, "--source", parquet, "--output", sqlite], {
    cwd: repoRoot(),
    encoding: "utf8",
  });
  if (result.status !== 0) {
    throw new Error(`Не удалось построить кэш КТРУ: ${result.stderr || result.stdout || "unknown error"}`);
  }
  return sqlite;
}

function prefixes(okpd: string): string[] {
  const parts = okpd.split(".");
  const out: string[] = [];
  for (let i = 0; i < parts.length; i++) out.push(parts.slice(0, i + 1).join("."));
  return out;
}

function compareCodes(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

function loadOkpdNames(rows: LightRow[]): Map<string, string> {
  const names = new Map<string, string>();
  for (const row of rows) {
    if (row.okpd2 && row.okpd2Name && !names.has(row.okpd2)) names.set(row.okpd2, row.okpd2Name);
  }
  const okpdPath = resolve(repoRoot(), "data/okpd2-moy-zakupki.json.gz");
  if (!existsSync(okpdPath)) return names;
  try {
    const items = JSON.parse(gunzipSync(readFileSync(okpdPath)).toString("utf8")) as { code?: string; name?: string }[];
    for (const item of items) {
      if (item.code && item.name && !names.has(item.code)) names.set(item.code, item.name);
    }
  } catch {
    // Optional enrichment; tree works without OKPD2 names.
  }
  return names;
}

function getCatalog(): Catalog {
  if (catalog) return catalog;
  const sqlite = ensureSqlite();
  const db = new Database(sqlite, { readonly: true, fileMustExist: true });
  db.pragma("query_only = ON");
  const rows = db.prepare("SELECT code, name, status, okpd2, okpd2_name AS okpd2Name, parent_code AS parentCode, is_template AS isTemplate FROM ktru").all() as LightRow[];
  const byCode = new Map(rows.map(row => [row.code, row]));
  const okpdNames = loadOkpdNames(rows);
  const okpdChildSet = new Map<string, Set<string>>();
  const addChild = (parent: string, child: string) => {
    let set = okpdChildSet.get(parent);
    if (!set) {
      set = new Set();
      okpdChildSet.set(parent, set);
    }
    set.add(child);
  };
  for (const row of rows) {
    const okpd = row.okpd2 || row.code.split("-", 1)[0];
    if (!okpd) continue;
    const chain = prefixes(okpd);
    addChild("", chain[0]!);
    for (let i = 1; i < chain.length; i++) addChild(chain[i - 1]!, chain[i]!);
  }
  const okpdChildCodes = new Map<string, string[]>();
  for (const [parent, set] of okpdChildSet) {
    okpdChildCodes.set(parent, [...set].sort(compareCodes));
  }
  const ktruChildren = new Map<string, string[]>();
  const okpdMembers = new Map<string, string[]>();
  for (const row of rows) {
    const okpd = row.okpd2 || row.code.split("-", 1)[0];
    if (!okpd) continue;
    let members = okpdMembers.get(okpd);
    if (!members) {
      members = [];
      okpdMembers.set(okpd, members);
    }
    members.push(row.code);
    if (row.parentCode && byCode.has(row.parentCode)) {
      let kids = ktruChildren.get(row.parentCode);
      if (!kids) {
        kids = [];
        ktruChildren.set(row.parentCode, kids);
      }
      kids.push(row.code);
    }
  }
  for (const kids of ktruChildren.values()) kids.sort(compareCodes);
  const okpdKtruRoots = new Map<string, string[]>();
  for (const [okpd, members] of okpdMembers) {
    const memberSet = new Set(members);
    const roots = members.filter(code => {
      const parent = byCode.get(code)?.parentCode;
      return !parent || !memberSet.has(parent);
    }).sort(compareCodes);
    okpdKtruRoots.set(okpd, roots);
  }
  catalog = { db, okpdNames, okpdChildCodes, okpdKtruRoots, ktruChildren, byCode, total: rows.length };
  return catalog;
}

function okpdNode(code: string, data: Catalog): KtruTreeNode {
  const okpdKids = data.okpdChildCodes.get(code) ?? [];
  const ktruRoots = okpdKids.length ? [] : (data.okpdKtruRoots.get(code) ?? []);
  const childCount = okpdKids.length || ktruRoots.length;
  return {
    id: `okpd:${code}`,
    code,
    name: data.okpdNames.get(code) || `ОКПД2 ${code}`,
    kind: "okpd",
    status: null,
    isTemplate: false,
    childCount,
    hasChildren: childCount > 0,
  };
}

function ktruNode(code: string, data: Catalog): KtruTreeNode {
  const row = data.byCode.get(code)!;
  const childCount = data.ktruChildren.get(code)?.length ?? 0;
  return {
    id: `ktru:${code}`,
    code,
    name: row.name,
    kind: "ktru",
    status: row.status,
    isTemplate: !!row.isTemplate,
    childCount,
    hasChildren: childCount > 0,
  };
}

function pageSlice<T>(items: T[], offset: number, limit = CHILDREN_PAGE): { items: T[]; total: number; offset: number; hasMore: boolean } {
  const total = items.length;
  const start = Math.max(0, offset);
  const slice = items.slice(start, start + limit);
  return { items: slice, total, offset: start, hasMore: start + slice.length < total };
}

export function ktruStats() {
  const data = getCatalog();
  return { total: data.total, roots: data.okpdChildCodes.get("")?.length ?? 0 };
}

export function listKtruChildren(parentId: string | null = null, offset = 0): KtruChildrenPage {
  const data = getCatalog();
  if (!parentId) {
    const codes = data.okpdChildCodes.get("") ?? [];
    const page = pageSlice(codes, offset);
    return { parentId: null, nodes: page.items.map(code => okpdNode(code, data)), total: page.total, offset: page.offset, hasMore: page.hasMore };
  }
  if (parentId.startsWith("okpd:")) {
    const code = parentId.slice(5);
    const next = data.okpdChildCodes.get(code) ?? [];
    if (next.length) {
      const page = pageSlice(next, offset);
      return { parentId, nodes: page.items.map(item => okpdNode(item, data)), total: page.total, offset: page.offset, hasMore: page.hasMore };
    }
    const roots = data.okpdKtruRoots.get(code) ?? [];
    const page = pageSlice(roots, offset);
    return { parentId, nodes: page.items.map(item => ktruNode(item, data)), total: page.total, offset: page.offset, hasMore: page.hasMore };
  }
  if (parentId.startsWith("ktru:")) {
    const code = parentId.slice(5);
    const kids = data.ktruChildren.get(code) ?? [];
    const page = pageSlice(kids, offset);
    return { parentId, nodes: page.items.map(item => ktruNode(item, data)), total: page.total, offset: page.offset, hasMore: page.hasMore };
  }
  throw new Error(`Неизвестный узел дерева: ${parentId}`);
}

export function getKtruRecord(code: string): Record<string, unknown> | null {
  if (!code || code.length > 64) return null;
  const data = getCatalog();
  const row = data.db.prepare("SELECT payload FROM ktru WHERE code=?").get(code) as { payload: Buffer } | undefined;
  if (!row) return null;
  return JSON.parse(gunzipSync(row.payload).toString("utf8")) as Record<string, unknown>;
}

export function searchKtru(query: string): KtruTreeNode[] {
  const q = query.trim().toLocaleLowerCase("ru-RU");
  if (q.length < 2) return [];
  const data = getCatalog();
  const matches: KtruTreeNode[] = [];
  for (const row of data.byCode.values()) {
    if (row.code.toLocaleLowerCase("ru-RU").includes(q) || row.name.toLocaleLowerCase("ru-RU").includes(q)) {
      matches.push(ktruNode(row.code, data));
      if (matches.length >= SEARCH_LIMIT) break;
    }
  }
  return matches;
}
