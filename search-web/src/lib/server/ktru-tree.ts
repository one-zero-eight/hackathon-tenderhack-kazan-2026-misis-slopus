import { spawnSync } from "node:child_process";
import { existsSync, statSync } from "node:fs";
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
const FILTER_MATCH_LIMIT = 800;
let catalog: Catalog | undefined;
const filterCache = new Map<string, FilterSets>();

interface FilterSets {
  visible: Set<string>;
  matches: Set<string>;
  matchCount: number;
  truncated: boolean;
}

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
  // Optional OKPD2 name enrichment used to live in a JSON.gz next to the parquet catalogs.
  // Names already present on KTRU rows are enough for the tree.
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

function okpdNode(code: string, data: Catalog, childCount?: number): KtruTreeNode {
  const okpdKids = data.okpdChildCodes.get(code) ?? [];
  const ktruRoots = okpdKids.length ? [] : (data.okpdKtruRoots.get(code) ?? []);
  const count = childCount ?? (okpdKids.length || ktruRoots.length);
  return {
    id: `okpd:${code}`,
    code,
    name: data.okpdNames.get(code) || `ОКПД2 ${code}`,
    kind: "okpd",
    status: null,
    isTemplate: false,
    childCount: count,
    hasChildren: count > 0,
  };
}

function ktruNode(code: string, data: Catalog, childCount?: number): KtruTreeNode {
  const row = data.byCode.get(code)!;
  const count = childCount ?? (data.ktruChildren.get(code)?.length ?? 0);
  return {
    id: `ktru:${code}`,
    code,
    name: row.name,
    kind: "ktru",
    status: row.status,
    isTemplate: !!row.isTemplate,
    childCount: count,
    hasChildren: count > 0,
  };
}

function pageSlice<T>(items: T[], offset: number, limit = CHILDREN_PAGE): { items: T[]; total: number; offset: number; hasMore: boolean } {
  const total = items.length;
  const start = Math.max(0, offset);
  const slice = items.slice(start, start + limit);
  return { items: slice, total, offset: start, hasMore: start + slice.length < total };
}

function normalizeFilter(filter: string): string {
  return filter.trim().toLocaleLowerCase("ru-RU");
}

function buildFilterSets(filter: string): FilterSets | null {
  const q = normalizeFilter(filter);
  if (q.length < 2) return null;
  const cached = filterCache.get(q);
  if (cached) return cached;
  const data = getCatalog();
  const matches = new Set<string>();
  const visible = new Set<string>();
  let matchCount = 0;
  let truncated = false;

  const markOkpdAncestors = (code: string) => {
    for (const prefix of prefixes(code)) visible.add(`okpd:${prefix}`);
  };

  for (const codes of data.okpdChildCodes.values()) {
    for (const code of codes) {
      const name = (data.okpdNames.get(code) || "").toLocaleLowerCase("ru-RU");
      if (code.toLocaleLowerCase("ru-RU").includes(q) || name.includes(q)) {
        matchCount += 1;
        if (matches.size < FILTER_MATCH_LIMIT) {
          const id = `okpd:${code}`;
          matches.add(id);
          markOkpdAncestors(code);
        } else truncated = true;
      }
    }
  }

  for (const row of data.byCode.values()) {
    if (!row.code.toLocaleLowerCase("ru-RU").includes(q) && !row.name.toLocaleLowerCase("ru-RU").includes(q)) continue;
    matchCount += 1;
    if (matches.size >= FILTER_MATCH_LIMIT) {
      truncated = true;
      continue;
    }
    const id = `ktru:${row.code}`;
    matches.add(id);
    visible.add(id);
    let parent = row.parentCode;
    const seen = new Set<string>([row.code]);
    while (parent && data.byCode.has(parent) && !seen.has(parent)) {
      seen.add(parent);
      visible.add(`ktru:${parent}`);
      parent = data.byCode.get(parent)!.parentCode;
    }
    const okpd = row.okpd2 || row.code.split("-", 1)[0];
    if (okpd) markOkpdAncestors(okpd);
  }

  for (const id of matches) visible.add(id);
  const sets = { visible, matches, matchCount, truncated };
  if (filterCache.size > 32) filterCache.clear();
  filterCache.set(q, sets);
  return sets;
}

function rawChildCodes(parentId: string | null, data: Catalog): { kind: "okpd" | "ktru"; codes: string[] } {
  if (!parentId) return { kind: "okpd", codes: data.okpdChildCodes.get("") ?? [] };
  if (parentId.startsWith("okpd:")) {
    const code = parentId.slice(5);
    const next = data.okpdChildCodes.get(code) ?? [];
    if (next.length) return { kind: "okpd", codes: next };
    return { kind: "ktru", codes: data.okpdKtruRoots.get(code) ?? [] };
  }
  if (parentId.startsWith("ktru:")) return { kind: "ktru", codes: data.ktruChildren.get(parentId.slice(5)) ?? [] };
  throw new Error(`Неизвестный узел дерева: ${parentId}`);
}

function filteredChildCount(id: string, data: Catalog, filter: FilterSets): number {
  const { kind, codes } = rawChildCodes(id, data);
  let count = 0;
  for (const code of codes) {
    if (filter.visible.has(`${kind}:${code}`)) count += 1;
  }
  return count;
}

export function ktruStats() {
  const data = getCatalog();
  return { total: data.total, roots: data.okpdChildCodes.get("")?.length ?? 0 };
}

export function listKtruChildren(parentId: string | null = null, offset = 0, filter = ""): KtruChildrenPage {
  const data = getCatalog();
  const sets = buildFilterSets(filter);
  const { kind, codes } = rawChildCodes(parentId, data);
  const filtered = sets ? codes.filter(code => sets.visible.has(`${kind}:${code}`)) : codes;
  const page = pageSlice(filtered, offset);
  const nodes = page.items.map(code => {
    const id = `${kind}:${code}`;
    const childCount = sets ? filteredChildCount(id, data, sets) : undefined;
    return kind === "okpd" ? okpdNode(code, data, childCount) : ktruNode(code, data, childCount);
  });
  return {
    parentId,
    nodes,
    total: page.total,
    offset: page.offset,
    hasMore: page.hasMore,
    filterMatches: sets ? sets.matchCount : null,
  };
}

export function getKtruRecord(code: string): Record<string, unknown> | null {
  if (!code || code.length > 64) return null;
  const data = getCatalog();
  const row = data.db.prepare("SELECT payload FROM ktru WHERE code=?").get(code) as { payload: Buffer } | undefined;
  if (!row) return null;
  return JSON.parse(gunzipSync(row.payload).toString("utf8")) as Record<string, unknown>;
}

export function getKtruNodeByCode(code: string): KtruTreeNode | null {
  if (!code || code.length > 64) return null;
  const data = getCatalog();
  if (data.byCode.has(code)) return ktruNode(code, data);
  if (data.okpdNames.has(code) || data.okpdChildCodes.has(code) || data.okpdKtruRoots.has(code)) {
    return okpdNode(code, data);
  }
  return null;
}

export function ktruFilterMeta(filter: string) {
  const sets = buildFilterSets(filter);
  if (!sets) return { active: false, matchCount: 0, truncated: false, matchIds: [] as string[] };
  return {
    active: true,
    matchCount: sets.matchCount,
    truncated: sets.truncated,
    matchIds: [...sets.matches],
  };
}
