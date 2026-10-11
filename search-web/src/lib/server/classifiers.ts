import { existsSync, statSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { gunzipSync } from "node:zlib";
import Database from "better-sqlite3";
import {
  filterSpecializationOptions,
  normSpecName,
  pickDiscriminatingCharacteristics,
  specializationOptionLabel,
  specializationValuesKey,
  type SpecChild,
} from "../specialization";
import type {
  CatalogSpecialization,
  CatalogSpecializationOption,
  DirChildrenPage,
  DirectoryTab,
  DirTreeNode,
  SpecializationCharacteristic,
  VotedCode,
} from "../types";
import {
  classifiersSqlitePath,
  kpgzParquetPath,
  okpd2ParquetPath,
  okeiParquetPath,
  spgzParquetPath,
  searchDatabasePath,
} from "./config";
import { getKtruNodeByCode, getKtruRecord, getKtruSpecialization, getSoleTemplateKtruForOkpd, listKtruByOkpd } from "./ktru-tree";
import { getSteCatalog } from "./ste-catalog";
import { getOkeiUnit } from "./okei-unit";

const CLASSIFIERS_SCHEMA = "classifiers-full-v4";

function foldQuery(value: string): string {
  return value.trim().normalize("NFKC").toLocaleLowerCase("ru-RU").replaceAll("ё", "е").replace(/\s+/g, " ");
}

export type { DirChildrenPage, DirectoryTab, DirTreeNode };

export interface DirCrossLink {
  tab: DirectoryTab;
  code: string;
  label: string;
}

export interface DirCharacteristicPart {
  text: string;
  okeiCode?: string;
  okeiName?: string;
  unit?: string;
}

export interface DirCharacteristic {
  name: string;
  value: string;
  required?: boolean;
  badges?: string[];
  code?: string;
  /** Selectable catalog values (SPGZ enums). Absent for free-text / constraint-only rows. */
  options?: string[];
  parts?: DirCharacteristicPart[];
  unit?: string;
  okeiCode?: string;
}

export interface DirListBlock {
  title: string;
  items: { text: string; href?: string; meta?: string }[];
  collapsible?: boolean;
  inline?: boolean;
}

export interface DirRecord {
  tab: DirectoryTab;
  code: string;
  name: string;
  fields: { key: string; label: string; value: string; href?: string }[];
  description?: string;
  characteristics: DirCharacteristic[];
  lists?: DirListBlock[];
  links: DirCrossLink[];
  backlinks: DirCrossLink[];
  backlinksMore: number;
}

export interface DirProductHit {
  id: number;
  name: string;
  okpd2: string;
  kpgzCode: string;
  kpgzName: string;
}

const PAGE = 200;
const FILTER_LIMIT = 800;
const LINK_LIMIT = 12;
let db: Database.Database | undefined;
const filterCache = new Map<string, { visible: Set<string>; matches: Set<string>; matchCount: number; truncated: boolean }>();

function repoRoot(): string {
  return resolve(dirname(fileURLToPath(import.meta.url)), "../../../..");
}

function ensureDb(): Database.Database {
  if (db) return db;
  const sqlite = classifiersSqlitePath();
  const sources = [
    ["okpd", okpd2ParquetPath()],
    ["kpgz", kpgzParquetPath()],
    ["spgz", spgzParquetPath()],
    ["okei", okeiParquetPath()],
  ] as const;
  const command = `uv run --with pyarrow python ${resolve(repoRoot(), "scripts/build_classifiers_sqlite.py")} --output ${JSON.stringify(sqlite)} ${sources.map(([key, path]) => `--${key} ${JSON.stringify(path)}`).join(" ")}`;
  const rebuildError = () => new Error(`Кэш справочников отсутствует, устарел или несовместим. Постройте его явно: ${command}`);
  if (sources.some(([, path]) => !existsSync(path))) throw rebuildError();
  const mtime = String(Math.trunc(Math.max(...sources.map(([, path]) => statSync(path).mtimeMs))));
  const needsBuild = !existsSync(sqlite) || (() => {
    try {
      const probe = new Database(sqlite, { readonly: true, fileMustExist: true });
      try {
        const meta = probe.prepare("SELECT value FROM meta WHERE key=?");
        const value = (key: string) => (meta.get(key) as { value: string } | undefined)?.value;
        return value("source_mtime_ms") !== mtime || value("schema") !== CLASSIFIERS_SCHEMA
          || value("lexical_schema") !== "folded-grams-v1" || sources.some(([key, path]) => {
          const source = value(key);
          return !source || resolve(repoRoot(), source) !== path
            || value(`${key}_mtime_ms`) !== String(Math.trunc(statSync(path).mtimeMs));
        });
      } finally {
        probe.close();
      }
    } catch {
      return true;
    }
  })();
  if (needsBuild) throw rebuildError();
  db = new Database(sqlite, { readonly: true, fileMustExist: true });
  db.pragma("mmap_size = 1073741824");
  db.pragma("cache_size = -2048");
  db.pragma("temp_store = FILE");
  db.pragma("query_only = ON");
  return db;
}

function pageSlice<T>(items: T[], offset: number) {
  const total = items.length;
  const start = Math.max(0, offset);
  const slice = items.slice(start, start + PAGE);
  return { items: slice, total, offset: start, hasMore: start + slice.length < total };
}

export function classifierStats() {
  const database = ensureDb();
  const get = (key: string) => Number((database.prepare("SELECT value FROM meta WHERE key=?").get(key) as { value: string } | undefined)?.value ?? 0);
  return { okpd: get("okpd_count"), kpgz: get("kpgz_count"), spgz: get("spgz_count"), okei: get("okei_count") };
}

function okeiNode(row: { id: number; code: string | null; name: string; kind: string; childCount: number }): DirTreeNode {
  const code = row.code || String(row.id);
  return {
    id: `okei:${row.id}`,
    code,
    name: row.name,
    kind: row.kind === "unit" ? "okei" : row.kind === "group" ? "okei-group" : "okei-section",
    childCount: row.childCount,
    hasChildren: row.childCount > 0,
  };
}

function okeiChildren(parentId: number | null): DirTreeNode[] {
  const database = ensureDb();
  const rows = (parentId == null
    ? database.prepare("SELECT id, code, name, kind, children_count AS childCount FROM okei WHERE parent_id IS NULL ORDER BY razdel, podrazdel, code").all()
    : database.prepare("SELECT id, code, name, kind, children_count AS childCount FROM okei WHERE parent_id=? ORDER BY razdel, podrazdel, code").all(parentId)) as {
    id: number; code: string | null; name: string; kind: string; childCount: number;
  }[];
  return rows.map(okeiNode);
}

function okpdChildren(parentId: number | null): DirTreeNode[] {
  const database = ensureDb();
  const rows = parentId == null
    ? database.prepare("SELECT id, code, name, children_count AS childCount FROM okpd WHERE parent_id IS NULL ORDER BY code").all()
    : database.prepare("SELECT id, code, name, children_count AS childCount FROM okpd WHERE parent_id=? ORDER BY code").all(parentId);
  return (rows as { id: number; code: string; name: string; childCount: number }[]).map(row => ({
    id: `okpd:${row.id}`,
    code: row.code,
    name: row.name,
    kind: "okpd",
    childCount: row.childCount,
    hasChildren: row.childCount > 0,
  }));
}

function kpgzChildren(parentId: number | null): DirTreeNode[] {
  const database = ensureDb();
  const rows = (parentId == null
    ? database.prepare(`
        SELECT k.id, k.code, k.name, k.is_last AS isLast, k.external_id AS externalId,
          (SELECT count(*) FROM kpgz c WHERE c.parent_id = k.id) AS kids,
          CASE WHEN k.is_last = 1 THEN (SELECT count(*) FROM spgz s WHERE s.kpgz_external_id = k.external_id) ELSE 0 END AS spgz
        FROM kpgz k WHERE k.parent_id IS NULL ORDER BY k.code
      `).all()
    : database.prepare(`
        SELECT k.id, k.code, k.name, k.is_last AS isLast, k.external_id AS externalId,
          (SELECT count(*) FROM kpgz c WHERE c.parent_id = k.id) AS kids,
          CASE WHEN k.is_last = 1 THEN (SELECT count(*) FROM spgz s WHERE s.kpgz_external_id = k.external_id) ELSE 0 END AS spgz
        FROM kpgz k WHERE k.parent_id = ? ORDER BY k.code
      `).all(parentId)) as { id: number; code: string; name: string; isLast: number; kids: number; spgz: number }[];
  return rows.map(row => {
    const count = row.kids || row.spgz;
    return {
      id: `kpgz:${row.id}`,
      code: row.code,
      name: row.name,
      kind: "kpgz",
      childCount: count,
      hasChildren: count > 0,
      meta: { isLast: !!row.isLast },
    };
  });
}

function spgzUnderKpgz(kpgzId: number): DirTreeNode[] {
  const database = ensureDb();
  const external = database.prepare("SELECT external_id AS externalId FROM kpgz WHERE id=?").get(kpgzId) as { externalId: number } | undefined;
  if (!external?.externalId) return [];
  const rows = database.prepare("SELECT id, name, okpd2_code AS okpd2, is_excluded AS excluded FROM spgz WHERE kpgz_external_id=? ORDER BY name").all(external.externalId) as {
    id: number; name: string; okpd2: string | null; excluded: number;
  }[];
  return rows.map(row => ({
    id: `spgz:${row.id}`,
    code: String(row.id),
    name: row.name,
    kind: "spgz",
    childCount: 0,
    hasChildren: false,
    meta: { okpd2: row.okpd2, excluded: !!row.excluded },
  }));
}

/** SPGZ leaves under a КПГЗ code (for create-product suggestion / pickers). */
export function listSpgzUnderKpgzCode(kpgzCode: string): { code: string; name: string }[] {
  const database = ensureDb();
  const row = database.prepare("SELECT id FROM kpgz WHERE code=? LIMIT 1").get(kpgzCode) as { id: number } | undefined;
  if (!row) return [];
  return spgzUnderKpgz(row.id).map(node => ({ code: node.code, name: node.name }));
}

export function listDirectoryChildren(tab: DirectoryTab, parentId: string | null = null, offset = 0, filter = ""): DirChildrenPage {
  if (tab === "ktru") throw new Error("КТРУ обслуживается отдельным модулем");
  const sets = buildFilter(tab, filter);
  let nodes: DirTreeNode[] = [];
  if (tab === "okpd") {
    const parent = parentId?.startsWith("okpd:") ? Number(parentId.slice(5)) : null;
    nodes = okpdChildren(Number.isFinite(parent) ? parent : null);
  } else if (tab === "kpgz") {
    if (parentId?.startsWith("kpgz:")) {
      const id = Number(parentId.slice(5));
      const kids = kpgzChildren(id);
      nodes = kids.length ? kids : spgzUnderKpgz(id).map(node => ({ ...node, kind: "spgz" }));
    } else {
      nodes = kpgzChildren(null);
    }
  } else if (tab === "spgz") {
    if (!parentId) {
      nodes = kpgzChildren(null);
    } else if (parentId.startsWith("kpgz:")) {
      const id = Number(parentId.slice(5));
      const kids = kpgzChildren(id);
      nodes = kids.length ? kids : spgzUnderKpgz(id);
    } else {
      nodes = [];
    }
  } else if (tab === "okei") {
    const parent = parentId?.startsWith("okei:") ? Number(parentId.slice(5)) : null;
    nodes = okeiChildren(Number.isFinite(parent) ? parent : null);
  }
  if (sets) {
    nodes = nodes.filter(node => sets.visible.has(node.id)).map(node => {
      if (!node.hasChildren) return { ...node, childCount: 0, hasChildren: false };
      const preview = listDirectoryChildrenRaw(tab, node.id);
      const count = preview.filter(child => sets.visible.has(child.id)).length;
      return { ...node, childCount: count, hasChildren: count > 0 };
    });
  }
  const page = pageSlice(nodes, offset);
  return { parentId, nodes: page.items, total: page.total, offset: page.offset, hasMore: page.hasMore, filterMatches: sets?.matchCount ?? null };
}

function listDirectoryChildrenRaw(tab: DirectoryTab, parentId: string): DirTreeNode[] {
  if (tab === "okpd" && parentId.startsWith("okpd:")) return okpdChildren(Number(parentId.slice(5)));
  if ((tab === "kpgz" || tab === "spgz") && parentId.startsWith("kpgz:")) {
    const id = Number(parentId.slice(5));
    const kids = kpgzChildren(id);
    return kids.length ? kids : spgzUnderKpgz(id);
  }
  if (tab === "okei" && parentId.startsWith("okei:")) return okeiChildren(Number(parentId.slice(5)));
  return [];
}

function buildFilter(tab: DirectoryTab, filter: string) {
  const q = foldQuery(filter);
  if (q.length < 2) return null;
  const tokens = q.split(/[^\p{L}\p{N}.-]+/u).filter(Boolean);
  const key = `${tab}:${q}`;
  const cached = filterCache.get(key);
  if (cached) return cached;
  const database = ensureDb();
  const matches = new Set<string>();
  const visible = new Set<string>();
  let matchCount = 0;
  let truncated = false;

  const addOkpdAncestors = (id: number) => {
    let current: number | null = id;
    const seen = new Set<number>();
    while (current && !seen.has(current)) {
      seen.add(current);
      visible.add(`okpd:${current}`);
      const row = database.prepare("SELECT parent_id AS parentId FROM okpd WHERE id=?").get(current) as { parentId: number | null } | undefined;
      current = row?.parentId ?? null;
    }
  };
  const addKpgzAncestors = (id: number) => {
    let current: number | null = id;
    const seen = new Set<number>();
    while (current && !seen.has(current)) {
      seen.add(current);
      visible.add(`kpgz:${current}`);
      const row = database.prepare("SELECT parent_id AS parentId FROM kpgz WHERE id=?").get(current) as { parentId: number | null } | undefined;
      current = row?.parentId ?? null;
    }
  };

  const tokenClauses = (tab: string, code: string, columns: string[]) => {
    if (!tokens.length) return { sql: "1=0", params: [] as string[], order: "id", ranking: [] as string[] };
    const grams = tokens.map(token => {
      const points = [...token];
      const terms = new Set<string>();
      if (points.length === 1) terms.add(`g${points[0]!.codePointAt(0)!.toString(16).padStart(6, "0")}`);
      for (let i = 1; i < points.length; i++) terms.add(`g${points[i - 1]!.codePointAt(0)!.toString(16).padStart(6, "0")}${points[i]!.codePointAt(0)!.toString(16).padStart(6, "0")}`);
      return `(${[...terms].join(" AND ")})`;
    }).join(" AND ");
    return {
      sql: `id IN (SELECT rowid FROM ${tab}_search WHERE ${tab}_search MATCH ?) AND ${tokens.map(() => `(${columns.map(column => `instr(${column},?)>0`).join(" OR ")})`).join(" AND ")}`,
      params: [grams, ...tokens.flatMap(token => columns.map(() => token))],
      order: `CASE WHEN ${code}=? THEN 0 WHEN name_fold=? THEN 1 ELSE 2 END, length(name), id`,
      ranking: [q, q],
    };
  };
  const addOkeiAncestors = (id: number) => {
    let current: number | null = id;
    const seen = new Set<number>();
    while (current && !seen.has(current)) {
      seen.add(current);
      visible.add(`okei:${current}`);
      const row = database.prepare("SELECT parent_id AS parentId FROM okei WHERE id=?").get(current) as { parentId: number | null } | undefined;
      current = row?.parentId ?? null;
    }
  };
  if (tab === "okpd") {
    const tokenFilter = tokenClauses("okpd", "code", ["code", "name_fold", "synonyms_fold"]);
    const rows = database.prepare(
      `SELECT id FROM okpd WHERE ${tokenFilter.sql} ORDER BY ${tokenFilter.order} LIMIT ?`,
    ).all(...tokenFilter.params, ...tokenFilter.ranking, FILTER_LIMIT + 1) as { id: number }[];
    truncated = rows.length > FILTER_LIMIT;
    matchCount = truncated ? FILTER_LIMIT : rows.length;
    for (const row of rows.slice(0, FILTER_LIMIT)) {
      matches.add(`okpd:${row.id}`);
      addOkpdAncestors(row.id);
    }
  } else if (tab === "okei") {
    const tokenFilter = tokenClauses("okei", "coalesce(code,'')", ["coalesce(code,'')", "name_fold"]);
    const rows = database.prepare(
      `SELECT id FROM okei WHERE ${tokenFilter.sql} ORDER BY ${tokenFilter.order} LIMIT ?`,
    ).all(...tokenFilter.params, ...tokenFilter.ranking, FILTER_LIMIT + 1) as { id: number }[];
    truncated = rows.length > FILTER_LIMIT;
    matchCount = truncated ? FILTER_LIMIT : rows.length;
    for (const row of rows.slice(0, FILTER_LIMIT)) {
      matches.add(`okei:${row.id}`);
      addOkeiAncestors(row.id);
    }
  } else {
    const tokenFilter = tokenClauses("kpgz", "code", ["code", "name_fold"]);
    const kpgzRows = database.prepare(
      `SELECT id FROM kpgz WHERE ${tokenFilter.sql} ORDER BY ${tokenFilter.order} LIMIT ?`,
    ).all(...tokenFilter.params, ...tokenFilter.ranking, FILTER_LIMIT + 1) as { id: number }[];
    truncated = kpgzRows.length > FILTER_LIMIT;
    matchCount = Math.min(kpgzRows.length, FILTER_LIMIT);
    for (const row of kpgzRows.slice(0, FILTER_LIMIT)) {
      matches.add(`kpgz:${row.id}`);
      addKpgzAncestors(row.id);
    }
    if (tab === "spgz" || tab === "kpgz") {
      const remaining = FILTER_LIMIT - matches.size;
      if (remaining > 0) {
        const spgzToken = tokenClauses("spgz", "cast(id AS TEXT)", ["cast(id AS TEXT)", "name_fold"]);
        const spgzRows = database.prepare(
          `SELECT id, kpgz_external_id AS kpgzExternalId FROM spgz
           WHERE ${spgzToken.sql} ORDER BY ${spgzToken.order}
           LIMIT ?`,
        ).all(...spgzToken.params, ...spgzToken.ranking, remaining + 1) as { id: number; kpgzExternalId: number | null }[];
        if (spgzRows.length > remaining) truncated = true;
        matchCount += Math.min(spgzRows.length, remaining);
        const kpgzByExternal = database.prepare("SELECT id FROM kpgz WHERE external_id=?");
        for (const row of spgzRows.slice(0, remaining)) {
          matches.add(`spgz:${row.id}`);
          visible.add(`spgz:${row.id}`);
          if (row.kpgzExternalId) {
            const kpgz = kpgzByExternal.get(row.kpgzExternalId) as { id: number } | undefined;
            if (kpgz) addKpgzAncestors(kpgz.id);
          }
        }
      }
    }
  }
  for (const id of matches) visible.add(id);
  const sets = { visible, matches, matchCount, truncated };
  if (filterCache.size > 48) filterCache.clear();
  filterCache.set(key, sets);
  return sets;
}

export function directoryFilterMeta(tab: DirectoryTab, filter: string) {
  const sets = buildFilter(tab, filter);
  if (!sets) return { active: false, matchCount: 0, truncated: false, matchIds: [] as string[] };
  return { active: true, matchCount: sets.matchCount, truncated: sets.truncated, matchIds: [...sets.matches] };
}

export function getDirectoryNode(tab: DirectoryTab, code: string): DirTreeNode | null {
  const database = ensureDb();
  if (tab === "okpd") {
    const row = database.prepare("SELECT id, code, name, children_count AS childCount FROM okpd WHERE code=? LIMIT 1").get(code) as { id: number; code: string; name: string; childCount: number } | undefined;
    if (!row) return null;
    return { id: `okpd:${row.id}`, code: row.code, name: row.name, kind: "okpd", childCount: row.childCount, hasChildren: row.childCount > 0 };
  }
  if (tab === "kpgz") {
    const row = database.prepare("SELECT id, code, name FROM kpgz WHERE code=? LIMIT 1").get(code) as { id: number; code: string; name: string } | undefined;
    if (row) return { id: `kpgz:${row.id}`, code: row.code, name: row.name, kind: "kpgz", childCount: 1, hasChildren: true };
    // Unified КПГЗ/СПГЗ tab: numeric codes are SPGZ ids.
    return getDirectoryNode("spgz", code);
  }
  if (tab === "spgz") {
    const id = Number(code);
    if (!Number.isSafeInteger(id)) return null;
    const row = database.prepare("SELECT id, name FROM spgz WHERE id=?").get(id) as { id: number; name: string } | undefined;
    if (!row) return null;
    return { id: `spgz:${row.id}`, code: String(row.id), name: row.name, kind: "spgz", childCount: 0, hasChildren: false };
  }
  if (tab === "okei") {
    const row = database.prepare("SELECT id, code, name, kind, children_count AS childCount FROM okei WHERE code=? LIMIT 1").get(code) as {
      id: number; code: string | null; name: string; kind: string; childCount: number;
    } | undefined;
    if (!row) return null;
    return okeiNode(row);
  }
  return null;
}

/** Ancestor+self node ids for expanding the directory tree to `code`. */
export function getDirectoryRevealPath(tab: DirectoryTab, code: string): { node: DirTreeNode | null; pathIds: string[] } {
  const node = getDirectoryNode(tab, code);
  if (!node) return { node: null, pathIds: [] };
  const database = ensureDb();
  if (tab === "okpd") {
    const pathIds: string[] = [];
    let current = database.prepare("SELECT id, parent_id AS parentId FROM okpd WHERE code=? LIMIT 1").get(code) as { id: number; parentId: number | null } | undefined;
    const seen = new Set<number>();
    while (current && !seen.has(current.id)) {
      seen.add(current.id);
      pathIds.push(`okpd:${current.id}`);
      current = current.parentId == null
        ? undefined
        : database.prepare("SELECT id, parent_id AS parentId FROM okpd WHERE id=?").get(current.parentId) as { id: number; parentId: number | null } | undefined;
    }
    pathIds.reverse();
    return { node, pathIds };
  }
  if (tab === "kpgz") {
    const pathIds: string[] = [];
    let current = database.prepare("SELECT id, parent_id AS parentId FROM kpgz WHERE code=? LIMIT 1").get(code) as { id: number; parentId: number | null } | undefined;
    if (!current) return getDirectoryRevealPath("spgz", code);
    const seen = new Set<number>();
    while (current && !seen.has(current.id)) {
      seen.add(current.id);
      pathIds.push(`kpgz:${current.id}`);
      current = current.parentId == null
        ? undefined
        : database.prepare("SELECT id, parent_id AS parentId FROM kpgz WHERE id=?").get(current.parentId) as { id: number; parentId: number | null } | undefined;
    }
    pathIds.reverse();
    return { node, pathIds };
  }
  if (tab === "spgz") {
    const id = Number(code);
    if (!Number.isSafeInteger(id)) return { node: null, pathIds: [] };
    const row = database.prepare("SELECT id, kpgz_external_id AS kpgzExternalId FROM spgz WHERE id=?").get(id) as {
      id: number; kpgzExternalId: number | null;
    } | undefined;
    if (!row) return { node: null, pathIds: [] };
    const pathIds: string[] = [];
    if (row.kpgzExternalId) {
      let current = database.prepare("SELECT id, parent_id AS parentId FROM kpgz WHERE external_id=? LIMIT 1").get(row.kpgzExternalId) as {
        id: number; parentId: number | null;
      } | undefined;
      const seen = new Set<number>();
      while (current && !seen.has(current.id)) {
        seen.add(current.id);
        pathIds.push(`kpgz:${current.id}`);
        current = current.parentId == null
          ? undefined
          : database.prepare("SELECT id, parent_id AS parentId FROM kpgz WHERE id=?").get(current.parentId) as { id: number; parentId: number | null } | undefined;
      }
      pathIds.reverse();
    }
    pathIds.push(`spgz:${row.id}`);
    return { node, pathIds };
  }
  if (tab === "okei") {
    const pathIds: string[] = [];
    let current = database.prepare("SELECT id, parent_id AS parentId FROM okei WHERE code=? LIMIT 1").get(code) as { id: number; parentId: number | null } | undefined;
    const seen = new Set<number>();
    while (current && !seen.has(current.id)) {
      seen.add(current.id);
      pathIds.push(`okei:${current.id}`);
      current = current.parentId == null
        ? undefined
        : database.prepare("SELECT id, parent_id AS parentId FROM okei WHERE id=?").get(current.parentId) as { id: number; parentId: number | null } | undefined;
    }
    pathIds.reverse();
    return { node, pathIds };
  }
  return { node, pathIds: [node.id] };
}

function pushSlice(target: DirCrossLink[], more: { n: number }, total: number, items: DirCrossLink[]) {
  target.push(...items);
  more.n += Math.max(0, total - items.length);
}

export function reverseLinksFor(tab: DirectoryTab, code: string, okpdCode = ""): { backlinks: DirCrossLink[]; backlinksMore: number } {
  const database = ensureDb();
  const backlinks: DirCrossLink[] = [];
  const more = { n: 0 };
  const okpd = okpdCode || (tab === "okpd" ? code : "");

  if (tab === "okpd" || (okpd && (tab === "ktru" || tab === "kpgz" || tab === "spgz"))) {
    if (tab === "okpd" || tab === "ktru") {
      const total = (database.prepare("SELECT count(*) AS n FROM kpgz WHERE okpd_code=?").get(okpd) as { n: number }).n;
      const rows = database.prepare("SELECT code, name FROM kpgz WHERE okpd_code=? ORDER BY code LIMIT ?").all(okpd, LINK_LIMIT) as { code: string; name: string }[];
      pushSlice(backlinks, more, total, rows.map(row => ({ tab: "kpgz", code: row.code, label: `КПГЗ ${row.code} — ${row.name}` })));
    }
    if (tab === "okpd" || tab === "ktru") {
      const total = (database.prepare("SELECT count(*) AS n FROM spgz WHERE okpd2_code=?").get(okpd) as { n: number }).n;
      const rows = database.prepare("SELECT id, name FROM spgz WHERE okpd2_code=? ORDER BY name LIMIT ?").all(okpd, LINK_LIMIT) as { id: number; name: string }[];
      pushSlice(backlinks, more, total, rows.map(row => ({ tab: "spgz", code: String(row.id), label: `СПГЗ ${row.id} — ${row.name}` })));
    }
    if (tab === "okpd" || tab === "kpgz" || tab === "spgz") {
      const { total, items } = listKtruByOkpd(okpd, LINK_LIMIT);
      pushSlice(backlinks, more, total, items.map(row => ({ tab: "ktru", code: row.code, label: `КТРУ ${row.code} — ${row.name}` })));
    }
  }

  if (tab === "kpgz") {
    const row = database.prepare("SELECT external_id AS externalId FROM kpgz WHERE code=? LIMIT 1").get(code) as { externalId: number | null } | undefined;
    if (row?.externalId) {
      const total = (database.prepare("SELECT count(*) AS n FROM spgz WHERE kpgz_external_id=?").get(row.externalId) as { n: number }).n;
      const rows = database.prepare("SELECT id, name FROM spgz WHERE kpgz_external_id=? ORDER BY name LIMIT ?").all(row.externalId, LINK_LIMIT) as { id: number; name: string }[];
      pushSlice(backlinks, more, total, rows.map(item => ({ tab: "spgz", code: String(item.id), label: `СПГЗ ${item.id} — ${item.name}` })));
    }
  }

  return { backlinks, backlinksMore: more.n };
}

/** EAIST numeric math-notation ids used in SPGZ characteristics. */
function spgzMathOp(code: string | number | null | undefined): string {
  switch (Number(code)) {
    case 3: return ">";
    case 4: return "<";
    case 6: return "≥";
    case 7: return "≤";
    default: return "";
  }
}

type SpgzOkeiRef = { id?: number; code?: string | number | null };

function resolveSpgzOkeis(refs: SpgzOkeiRef[] | null | undefined): { code: string; name: string; short: string }[] {
  if (!Array.isArray(refs) || !refs.length) return [];
  const out: { code: string; name: string; short: string }[] = [];
  const seen = new Set<string>();
  for (const ref of refs) {
    const code = String(ref?.code ?? "").trim();
    if (!code || seen.has(code)) continue;
    seen.add(code);
    const unit = getOkeiUnit(code);
    if (unit) out.push(unit);
  }
  return out;
}

/** Pick position OKEI only when it matches the characteristic dimension (per-char units are absent in SPGZ export). */
function unitForSpgzCharacteristic<T extends { code: string }>(name: string, okeis: T[]): T | undefined {
  if (!okeis.length) return undefined;
  const n = name.toLocaleLowerCase("ru-RU");
  const mass = /масс|вес|нетто|брутто/.test(n);
  const length = /длин|ширин|высот|толщин|диаметр|диагонал|глубин|радиус|пролёт|пролет/.test(n);
  const area = /площад/.test(n);
  const volume = /объ[её]м|ёмкост|емкост/.test(n);
  const power = /мощност/.test(n);
  const time = /время|срок|продолжительн|возраст/.test(n);
  const count = /количество|число|кол-во/.test(n);
  const pick = (allowed: string[]) => okeis.find(item => allowed.includes(item.code));
  if (mass) return pick(["161", "163", "166", "168", "206"]);
  if (length) return pick(["003", "004", "005", "006", "008", "018", "039"]);
  if (area) return pick(["055", "059", "058", "061"]);
  if (volume) return pick(["111", "112", "113", "116", "118"]);
  if (power) return pick(["214", "215", "216"]);
  if (time) return pick(["354", "355", "356", "359", "360", "362", "366"]);
  if (count) return pick(["796", "778", "839", "868", "875", "876"]);
  if (/частот/.test(n)) return pick(["290", "291", "292", "293"]);
  return undefined;
}

function spgzConditionLabel(conditionTypeId: number | null | undefined): string {
  switch (Number(conditionTypeId)) {
    case 1: return "без ограничения значения";
    case 3: return "больше";
    case 4: return "меньше";
    case 5: return "диапазон";
    case 6: return "не менее";
    case 7: return "не более";
    case 8: return "перечень значений";
    default: return "";
  }
}

function formatSpgzCharacteristic(item: {
  characteristicName?: string;
  value1?: string | null;
  value2?: string | null;
  valueTypeId?: number | null;
  conditionTypeId?: number | null;
  catalogTypeId?: number | null;
  catalogVariantId?: number | null;
  minMathNotation?: string | number | null;
  maxMathNotation?: string | number | null;
  characteristicSpgzEnums?: { value?: string }[] | null;
  codeCharKTRU?: string | null;
  kpgzCharacteristicId?: number | null;
}, okeis: { code: string; name: string; short: string }[] = []): DirCharacteristic | null {
  const name = String(item.characteristicName ?? "").trim();
  if (!name) return null;
  const badges: string[] = [];
  if (item.valueTypeId === 1) badges.push("количественная");
  if (item.valueTypeId === 5) badges.push("качественная");
  const condition = spgzConditionLabel(item.conditionTypeId);
  if (condition) badges.push(condition);
  if (item.catalogVariantId === 1) badges.push("обязательная для описания");
  if (item.catalogVariantId === 2) badges.push("выбираемая");
  if (item.codeCharKTRU) badges.push(`КТРУ ${item.codeCharKTRU}`);

  const unitMeta = item.valueTypeId === 1 ? unitForSpgzCharacteristic(name, okeis) : undefined;
  const unitShort = unitMeta?.short || "";
  const withUnit = (value: string) => (unitShort ? `${value} ${unitShort}` : value);

  const required = item.catalogVariantId === 1;
  const enums = (item.characteristicSpgzEnums ?? []).map(entry => String(entry.value ?? "").trim()).filter(Boolean);
  if (enums.length) {
    return {
      name,
      value: enums.join(" · "),
      required,
      badges,
      options: enums,
      unit: unitShort,
      okeiCode: unitMeta?.code,
      parts: enums.map(text => ({ text, unit: unitShort, okeiCode: unitMeta?.code, okeiName: unitMeta?.name })),
      code: item.kpgzCharacteristicId != null ? String(item.kpgzCharacteristicId) : undefined,
    };
  }

  const v1 = item.value1 != null && String(item.value1).trim() ? String(item.value1).trim() : "";
  const v2 = item.value2 != null && String(item.value2).trim() ? String(item.value2).trim() : "";
  const minOp = spgzMathOp(item.minMathNotation);
  const maxOp = spgzMathOp(item.maxMathNotation);

  let value = "";
  if (v1 && v2 && v1 !== v2) {
    const left = minOp ? `${minOp}${v1}` : v1;
    const right = maxOp ? `${maxOp}${v2}` : v2;
    value = withUnit(`${left} … ${right}`);
  } else if (v1 || v2) {
    const num = v1 || v2;
    const op = v1 ? minOp : maxOp;
    value = withUnit(op ? `${op} ${num}` : num);
  } else if (minOp || maxOp) {
    value = [minOp, maxOp].filter(Boolean).join(" … ") || "—";
  } else {
    value = "—";
  }
  return {
    name,
    value,
    required,
    badges,
    code: item.kpgzCharacteristicId != null ? String(item.kpgzCharacteristicId) : undefined,
    unit: unitShort,
    okeiCode: unitMeta?.code,
    parts: value === "—" ? [] : [{
      text: value,
      unit: unitShort,
      okeiCode: unitMeta?.code,
      okeiName: unitMeta?.name,
    }],
  };
}

function flagYes(value: unknown): boolean {
  return value === 1 || value === true || value === "1";
}

export function getDirectoryRecord(tab: DirectoryTab, code: string): DirRecord | null {
  const database = ensureDb();
  if (tab === "okpd") {
    const row = database.prepare("SELECT code, name, level, is_actual AS isActual FROM okpd WHERE code=? LIMIT 1").get(code) as {
      code: string; name: string; level: number | null; isActual: number;
    } | undefined;
    if (!row) return null;
    const raw = getDirectoryRaw("okpd", row.code) ?? {};
    const reverse = reverseLinksFor("okpd", row.code);
    const limitations = Array.isArray(raw.limitations) ? raw.limitations as {
      name?: string; shortname?: string; description?: string; section?: string; link?: string; is_expired?: boolean; recommendation?: { annotation?: string; link?: string };
    }[] : [];
    const similar = Array.isArray(raw.similar_products) ? raw.similar_products.map(String).filter(Boolean) : [];
    const lists: DirListBlock[] = [];
    if (limitations.length) {
      lists.push({
        title: "Ограничения и примечания",
        collapsible: true,
        items: limitations.map(item => ({
          text: [item.shortname || item.name, item.description].filter(Boolean).join(" — ") || "—",
          href: item.link || item.recommendation?.link || undefined,
          meta: [item.section, item.is_expired ? "не действует" : "", item.recommendation?.annotation].filter(Boolean).join(" · ") || undefined,
        })),
      });
    }
    if (similar.length) {
      lists.push({ title: "Как ещё ищут", inline: true, items: similar.map(text => ({ text })) });
    }
    return {
      tab, code: row.code, name: row.name,
      fields: [
        { key: "code", label: "Код ОКПД2", value: row.code },
        { key: "name", label: "Название", value: row.name },
        { key: "level", label: "Уровень", value: String(row.level ?? "") },
        { key: "actual", label: "Актуальность", value: row.isActual ? "да" : "нет" },
        ...(raw.visitors_count != null ? [{ key: "visitors", label: "Просмотры", value: String(raw.visitors_count) }] : []),
      ],
      characteristics: [],
      lists,
      links: [],
      ...reverse,
    };
  }
  if (tab === "kpgz") {
    const row = database.prepare(
      "SELECT code, name, okpd_code AS okpdCode, okpd_name AS okpdName, tree_level AS treeLevel, is_last AS isLast, external_id AS externalId FROM kpgz WHERE code=? LIMIT 1",
    ).get(code) as {
      code: string; name: string; okpdCode: string | null; okpdName: string | null; treeLevel: number | null; isLast: number; externalId: number | null;
    } | undefined;
    if (!row) return getDirectoryRecord("spgz", code);
    const raw = getDirectoryRaw("kpgz", row.code) ?? {};
    const links: DirCrossLink[] = [];
    if (row.okpdCode) links.push({ tab: "okpd", code: row.okpdCode, label: `ОКПД2 ${row.okpdCode}${row.okpdName ? ` — ${row.okpdName}` : ""}` });
    const reverse = reverseLinksFor("kpgz", row.code, row.okpdCode || "");
    return {
      tab, code: row.code, name: row.name,
      fields: [
        { key: "code", label: "Код КПГЗ", value: row.code },
        { key: "name", label: "Название", value: row.name },
        { key: "level", label: "Уровень", value: String(row.treeLevel ?? "") },
        { key: "last", label: "Последний уровень", value: row.isLast ? "да" : "нет" },
        { key: "innovation", label: "Инновация", value: flagYes(raw.innovation) ? "да" : "нет" },
        ...(row.externalId != null ? [{ key: "external", label: "External ID", value: String(row.externalId) }] : []),
      ],
      characteristics: [],
      links,
      ...reverse,
    };
  }
  if (tab === "spgz") {
    const id = Number(code);
    if (!Number.isSafeInteger(id)) return null;
    const row = database.prepare(
      "SELECT id, name, kpgz_external_id AS kpgzExternalId, okpd2_code AS okpd2, ktru_id AS ktruId, is_excluded AS excluded FROM spgz WHERE id=?",
    ).get(id) as {
      id: number; name: string; kpgzExternalId: number | null; okpd2: string | null; ktruId: number | null; excluded: number;
    } | undefined;
    if (!row) return null;
    const raw = getDirectoryRaw("spgz", String(row.id)) ?? {};
    const okeis = resolveSpgzOkeis(raw.okeis as SpgzOkeiRef[] | undefined);
    const links: DirCrossLink[] = [];
    if (row.okpd2) links.push({ tab: "okpd", code: row.okpd2, label: `ОКПД2 ${row.okpd2}` });
    if (row.kpgzExternalId) {
      const kpgz = database.prepare("SELECT code, name FROM kpgz WHERE external_id=?").get(row.kpgzExternalId) as { code: string; name: string } | undefined;
      if (kpgz) links.push({ tab: "kpgz", code: kpgz.code, label: `КПГЗ ${kpgz.code} — ${kpgz.name}` });
    }
    for (const unit of okeis) {
      links.push({ tab: "okei", code: unit.code, label: `ОКЕИ ${unit.code} — ${unit.name}` });
    }
    const chars = Array.isArray(raw.spgzCharacteristics)
      ? (raw.spgzCharacteristics as Parameters<typeof formatSpgzCharacteristic>[0][]).flatMap(item => {
        const parsed = formatSpgzCharacteristic(item, okeis);
        return parsed ? [parsed] : [];
      })
      : [];
    const reverse = reverseLinksFor("spgz", String(row.id), row.okpd2 || "");
    const activeFlags = [
      flagYes(raw.auctionProduct) && "аукционный товар",
      flagYes(raw.isStandartProduct) && "стандартный продукт",
      flagYes(raw.innovationProduct) && "инновационный",
      flagYes(raw.niokr) && "НИОКР",
      flagYes(raw.applyLifecycle) && "жизненный цикл",
      flagYes(raw.allowLimited) && "ограниченное участие",
      flagYes(raw.allowTwoStage) && "двухэтапная",
      flagYes(raw.allowCall) && "запрос предложений",
      flagYes(raw.regulationRequired) && "нужно нормирование",
      flagYes(raw.art) && "произведение искусства",
      flagYes(raw.project) && "проект",
    ].filter(Boolean) as string[];
    const description = String(raw.descriptions ?? "").trim();
    return {
      tab, code: String(row.id), name: row.name,
      description: description && description !== row.name ? description : undefined,
      fields: [
        { key: "id", label: "ID СПГЗ", value: String(row.id) },
        { key: "name", label: "Название", value: row.name },
        { key: "okpd2", label: "ОКПД2", value: row.okpd2 || "—" },
        { key: "okei", label: "Ед. изм. позиции", value: okeis.length ? okeis.map(unit => `${unit.name} (${unit.code})`).join(", ") : "—" },
        ...(row.ktruId != null ? [{ key: "ktruId", label: "КТРУ (id ЕАИСТ)", value: String(row.ktruId) }] : []),
        { key: "flags", label: "Признаки", value: activeFlags.length ? activeFlags.join(", ") : "—" },
        { key: "excluded", label: "Исключён", value: row.excluded ? "да" : "нет" },
      ],
      characteristics: chars,
      links,
      ...reverse,
    };
  }
  if (tab === "okei") {
    const row = database.prepare("SELECT code, name, kind, razdel, podrazdel FROM okei WHERE code=? LIMIT 1").get(code) as {
      code: string; name: string; kind: string; razdel: string; podrazdel: string;
    } | undefined;
    if (!row) return null;
    const kindLabel = row.kind === "unit" ? "единица" : row.kind === "group" ? "группа" : "раздел";
    return {
      tab, code: row.code, name: row.name,
      fields: [
        { key: "code", label: "Код ОКЕИ", value: row.kind === "unit" ? row.code : "—" },
        { key: "name", label: "Название", value: row.name },
        { key: "kind", label: "Тип", value: kindLabel },
        { key: "razdel", label: "Раздел", value: row.razdel },
        { key: "podrazdel", label: "Подраздел", value: row.podrazdel },
      ],
      characteristics: [],
      links: [],
      backlinks: [],
      backlinksMore: 0,
    };
  }
  return null;
}

function decodeRaw(payload: Buffer): Record<string, unknown> {
  return JSON.parse(gunzipSync(payload).toString("utf8")) as Record<string, unknown>;
}

export function getDirectoryRaw(tab: DirectoryTab, code: string): Record<string, unknown> | null {
  if (tab === "ktru") return null;
  if (tab === "spgz") {
    const id = Number(code);
    if (!Number.isSafeInteger(id)) return null;
    code = String(id);
  }
  const row = ensureDb().prepare("SELECT payload FROM raw_records WHERE tab=? AND code=?").get(tab, code) as
    | { payload: Buffer }
    | undefined;
  return row ? decodeRaw(row.payload) : null;
}

const attachedSearchIndexes = new WeakMap<Database.Database, { path: string; identity: string }>();

function indexedCatalogDb(): { database: Database.Database; table: string; idColumn: string } | null {
  const path = searchDatabasePath();
  if (!existsSync(path)) return null;
  const { db: database } = getSteCatalog();
  const stat = statSync(path, { bigint: true });
  const identity = `${stat.dev}:${stat.ino}`;
  const attached = attachedSearchIndexes.get(database);
  if (attached?.path !== path || attached.identity !== identity) {
    // Module reloads can reset the cache while the shared connection stays open.
    const existing = (database.pragma("database_list") as { name: string }[])
      .some(row => row.name === "directory_search_index");
    if (existing) database.exec("DETACH DATABASE directory_search_index");
    // ATTACH inherits SQLITE_OPEN_READONLY from the shared catalog connection.
    // better-sqlite3 disables URI filenames, so pass the actual filesystem path.
    database.prepare("ATTACH DATABASE ? AS directory_search_index").run(path);
    database.pragma("directory_search_index.mmap_size = 1073741824");
    database.pragma("directory_search_index.cache_size = -2048");
    attachedSearchIndexes.set(database, { path, identity });
  }
  const hasIndex = database.prepare("SELECT 1 FROM directory_search_index.sqlite_master WHERE type='table' AND name='index_state'").get();
  return hasIndex ? { database, table: "index_state", idColumn: "product_id" } : null;
}

function listIndexedCatalogProducts(
  column: "okpd2" | "kpgz_code" | "unit",
  values: string[],
  limit: number,
): { total: number; items: DirProductHit[] } {
  if (!values.length) return { total: 0, items: [] };
  const index = indexedCatalogDb();
  if (!index) return { total: 0, items: [] };
  const { database, table, idColumn } = index;
  const where = `p.${column} IN (${values.map(() => "?").join(",")})
    AND EXISTS (SELECT 1 FROM directory_search_index.${table} idx WHERE idx.${idColumn}=p.id)`;
  return database.transaction(() => {
    const total = (database.prepare(`SELECT count(*) AS n FROM main.products p WHERE ${where}`).get(...values) as { n: number }).n;
    const rows = database.prepare(`
      SELECT p.id, p.name, p.okpd2, p.kpgz_code AS kpgzCode, p.kpgz_name AS kpgzName
      FROM main.products p WHERE ${where} ORDER BY p.name, p.id LIMIT ?
    `).all(...values, Math.max(0, Math.ceil(limit))) as DirProductHit[];
    return {
      total,
      items: rows.map(row => ({
        ...row,
        kpgzName: row.kpgzName || (row.kpgzCode
          ? (ensureDb().prepare("SELECT name FROM kpgz WHERE code=? LIMIT 1").get(row.kpgzCode) as { name: string } | undefined)?.name ?? ""
          : ""),
      })),
    };
  })();
}

export function listDirectoryProducts(tab: DirectoryTab, code: string, limit = 24): { total: number; items: DirProductHit[] } {
  if (tab === "okpd") return listIndexedCatalogProducts("okpd2", [code], limit);
  if (tab === "kpgz") return listIndexedCatalogProducts("kpgz_code", [code], limit);
  if (tab === "spgz") {
    const record = getDirectoryRecord("spgz", code);
    const okpd2 = record?.fields.find((field) => field.key === "okpd2")?.value;
    if (!okpd2 || okpd2 === "—") return { total: 0, items: [] };
    return listDirectoryProducts("okpd", okpd2, limit);
  }
  if (tab === "okei") {
    const unit = ensureDb().prepare("SELECT kind, name FROM okei WHERE code=? LIMIT 1").get(code) as { kind: string; name: string } | undefined;
    if (!unit || unit.kind !== "unit") return { total: 0, items: [] };
    const needle = unit.name.toLocaleLowerCase("ru");
    // SQLite lower()/NOCASE only fold ASCII. Stream distinct units through the
    // covering unit index, then use indexed equality for matching Cyrillic variants.
    const values: string[] = [];
    for (const row of getSteCatalog().db.prepare("SELECT DISTINCT unit FROM products ORDER BY unit").iterate() as Iterable<{ unit: string }>) {
      if (row.unit.toLocaleLowerCase("ru") === needle) values.push(row.unit);
    }
    return listIndexedCatalogProducts("unit", values, limit);
  }
  // Directory KTRU cards fall back to products by OKPD2 instead.
  return { total: 0, items: [] };
}

const SPGZ_SPEC_SIBLING_LIMIT = 80;

type SpgzSiblingRow = {
  id: number;
  name: string;
  spgzCharacteristics?: {
    characteristicName?: string | null;
    value1?: string | null;
    value2?: string | null;
    characteristicSpgzEnums?: { value?: string }[] | null;
    kpgzCharacteristicId?: number | null;
  }[] | null;
};

function* loadSpgzSiblingRows(kpgzExternalId: number): Generator<SpgzSiblingRow> {
  const records = ensureDb().prepare(`
    SELECT raw.payload FROM spgz s
    JOIN raw_records raw ON raw.tab='spgz' AND raw.code=cast(s.id AS TEXT)
    WHERE s.kpgz_external_id=? AND s.is_excluded=0
  `).iterate(kpgzExternalId) as Iterable<{ payload: Buffer }>;
  for (const row of records) yield decodeRaw(row.payload) as SpgzSiblingRow;
}

function spgzConcreteCharValues(item: {
  characteristicName?: string | null;
  value1?: string | null;
  value2?: string | null;
  characteristicSpgzEnums?: { value?: string }[] | null;
  kpgzCharacteristicId?: number | null;
}): { name: string; value: string; code: string | null } | null {
  const name = String(item.characteristicName ?? "").trim();
  if (!name) return null;
  const enums = (item.characteristicSpgzEnums ?? [])
    .map((entry) => String(entry.value ?? "").trim())
    .filter(Boolean);
  if (enums.length === 1) {
    return {
      name,
      value: enums[0]!,
      code: item.kpgzCharacteristicId != null ? String(item.kpgzCharacteristicId) : null,
    };
  }
  if (enums.length > 1) return null;
  const v1 = item.value1 != null ? String(item.value1).trim() : "";
  const v2 = item.value2 != null ? String(item.value2).trim() : "";
  if (v1 && !v2) {
    return {
      name,
      value: v1,
      code: item.kpgzCharacteristicId != null ? String(item.kpgzCharacteristicId) : null,
    };
  }
  return null;
}

type SpgzSiblingSpec = {
  characteristics: SpecializationCharacteristic[];
  options: { values: Record<string, string>; code: string; name: string }[];
  currentValues: Record<string, string> | null;
  kpgzCode: string | null;
};

/** Sibling СПГЗ under one КПГЗ, partitioned by a compact set of concrete characteristic values. */
export function getSpgzSpecialization(input: {
  spgzCode?: string | null;
  kpgzCode?: string | null;
  preferredCharNames?: string[] | null;
}): SpgzSiblingSpec | null {
  const database = ensureDb();
  let kpgzExternalId: number | null = null;
  let kpgzCode: string | null = input.kpgzCode?.trim() || null;
  let currentSpgzId: number | null = null;

  if (input.spgzCode) {
    const id = Number(input.spgzCode);
    if (!Number.isSafeInteger(id)) return null;
    currentSpgzId = id;
    const row = database.prepare(
      "SELECT kpgz_external_id AS kpgzExternalId FROM spgz WHERE id=?",
    ).get(id) as { kpgzExternalId: number | null } | undefined;
    if (!row?.kpgzExternalId) return null;
    kpgzExternalId = row.kpgzExternalId;
    if (!kpgzCode) {
      const kpgz = database.prepare("SELECT code FROM kpgz WHERE external_id=? LIMIT 1").get(kpgzExternalId) as { code: string } | undefined;
      kpgzCode = kpgz?.code ?? null;
    }
  } else if (kpgzCode) {
    const row = database.prepare("SELECT external_id AS externalId FROM kpgz WHERE code=? LIMIT 1").get(kpgzCode) as { externalId: number | null } | undefined;
    if (!row?.externalId) return null;
    kpgzExternalId = row.externalId;
  } else {
    return null;
  }

  const count = (database.prepare(
    "SELECT count(*) AS n FROM spgz WHERE kpgz_external_id=? AND is_excluded=0",
  ).get(kpgzExternalId) as { n: number }).n;
  if (count < 2 || count > SPGZ_SPEC_SIBLING_LIMIT) return null;

  const rows = [...loadSpgzSiblingRows(kpgzExternalId)];

  if (rows.length < 2) return null;

  const children: SpecChild[] = rows.map((row) => {
    const values = new Map<string, { value: string; code: string | null }>();
    for (const item of row.spgzCharacteristics ?? []) {
      const concrete = spgzConcreteCharValues(item);
      if (!concrete) continue;
      values.set(concrete.name, { value: concrete.value, code: concrete.code });
    }
    return { code: String(row.id), name: row.name || String(row.id), values };
  });

  const picked = pickDiscriminatingCharacteristics(children, {
    preferredNames: input.preferredCharNames ?? [],
  });
  if (!picked) return null;

  const characteristics = picked.characteristics.map(({ name, code }) => ({ name, code }));
  const options: SpgzSiblingSpec["options"] = [];
  for (const child of children) {
    const values: Record<string, string> = {};
    let complete = true;
    for (const characteristic of characteristics) {
      const value = child.values.get(characteristic.name)?.value;
      if (!value) {
        complete = false;
        break;
      }
      values[characteristic.name] = value;
    }
    if (!complete) continue;
    options.push({ code: child.code, name: child.name, values });
  }
  if (options.length < 2) return null;
  options.sort((a, b) =>
    specializationOptionLabel(characteristics, a.values)
      .localeCompare(specializationOptionLabel(characteristics, b.values), "ru")
    || a.code.localeCompare(b.code));
  const current = currentSpgzId != null
    ? options.find((item) => item.code === String(currentSpgzId)) ?? null
    : null;
  return {
    characteristics,
    options,
    currentValues: current ? { ...current.values } : null,
    kpgzCode,
  };
}

/** Resolve only an unambiguous active child; never pick the first SPGZ sibling. */
export function getSoleSpgzForKpgz(kpgzCode: string): { code: string; name: string } | null {
  const rows = ensureDb().prepare(`
    SELECT s.id, s.name FROM spgz s
    JOIN kpgz k ON k.external_id = s.kpgz_external_id
    WHERE k.code=? AND s.is_excluded=0
    LIMIT 2
  `).all(kpgzCode) as { id: number; name: string }[];
  return rows.length === 1 ? { code: String(rows[0].id), name: rows[0].name } : null;
}

function sameCharacteristicSet(
  left: SpecializationCharacteristic[],
  right: SpecializationCharacteristic[],
): boolean {
  if (left.length !== right.length) return false;
  const rightNames = new Set(right.map((item) => normSpecName(item.name)));
  return left.every((item) => rightNames.has(normSpecName(item.name)));
}

export function getCatalogSpecialization(input: {
  ktruCode?: string | null;
  spgzCode?: string | null;
  kpgzCode?: string | null;
}): CatalogSpecialization | null {
  const ktru = input.ktruCode ? getKtruSpecialization(input.ktruCode) : null;
  const spgz = getSpgzSpecialization({
    spgzCode: input.spgzCode,
    kpgzCode: input.kpgzCode,
    preferredCharNames: ktru?.characteristics.map((item) => item.name) ?? null,
  });
  if (!ktru && !spgz) return null;

  const characteristics = ktru?.characteristics
    ?? spgz!.characteristics;
  const byKey = new Map<string, CatalogSpecializationOption>();

  const touch = (values: Record<string, string>) => {
    const key = specializationValuesKey(characteristics, values);
    let row = byKey.get(key);
    if (!row) {
      row = { values: { ...values }, ktruCode: null, ktruName: null, spgzCode: null, spgzName: null };
      byKey.set(key, row);
    }
    return row;
  };

  if (ktru) {
    for (const option of ktru.options) {
      const row = touch(option.values);
      row.ktruCode = option.code;
      row.ktruName = option.name;
    }
  }
  if (spgz && (!ktru || sameCharacteristicSet(characteristics, spgz.characteristics))) {
    for (const option of spgz.options) {
      const aligned: Record<string, string> = {};
      for (const characteristic of characteristics) {
        const key = Object.keys(option.values).find((name) =>
          normSpecName(name) === normSpecName(characteristic.name));
        if (!key) continue;
        aligned[characteristic.name] = option.values[key]!;
      }
      if (Object.keys(aligned).length !== characteristics.length) continue;
      const row = touch(aligned);
      row.spgzCode = option.code;
      row.spgzName = option.name;
    }
  }

  const options = [...byKey.values()]
    .filter((item) => item.ktruCode || item.spgzCode)
    .sort((a, b) =>
      specializationOptionLabel(characteristics, a.values)
        .localeCompare(specializationOptionLabel(characteristics, b.values), "ru"));
  if (options.length < 2) return null;

  const currentValues = ktru?.currentValues || spgz?.currentValues || null;
  return {
    characteristics,
    templateKtruCode: ktru?.templateCode ?? null,
    templateKtruName: ktru?.templateName ?? null,
    options,
    currentValues,
  };
}

/** Normalized characteristic names present in KTRU and/or SPGZ for this product's classifiers. */
export function classifierCharacteristicNames(input: {
  ktruCode?: string | null;
  okpd2?: string | null;
  spgzCode?: string | null;
  kpgzCode?: string | null;
}): { ktru: string[]; spgz: string[] } {
  const ktruNames = new Set<string>();
  const spgzNames = new Set<string>();

  let ktruCode = input.ktruCode?.trim() || "";
  if (!ktruCode && input.okpd2?.trim()) {
    const sole = getSoleTemplateKtruForOkpd(input.okpd2.trim());
    if (sole) ktruCode = sole.code;
    else {
      const listed = listKtruByOkpd(input.okpd2.trim(), 1);
      ktruCode = listed.items[0]?.code ?? "";
    }
  }
  if (ktruCode) {
    const record = getKtruRecord(ktruCode);
    for (const item of record?.characteristics ?? []) {
      const name = normSpecName(item.name);
      if (name) ktruNames.add(name);
    }
  }

  if (input.spgzCode?.trim()) {
    const record = getDirectoryRecord("spgz", input.spgzCode.trim());
    for (const item of record?.characteristics ?? []) {
      const name = normSpecName(item.name);
      if (name) spgzNames.add(name);
    }
  } else if (input.kpgzCode?.trim()) {
    const database = ensureDb();
    const kpgz = database.prepare("SELECT external_id AS externalId FROM kpgz WHERE code=? LIMIT 1").get(input.kpgzCode.trim()) as
      | { externalId: number | null }
      | undefined;
    if (kpgz?.externalId) {
      for (const row of loadSpgzSiblingRows(kpgz.externalId)) {
        for (const item of row.spgzCharacteristics ?? []) {
          const name = normSpecName(String(item.characteristicName ?? ""));
          if (name) spgzNames.add(name);
        }
      }
    }
  }

  return { ktru: [...ktruNames], spgz: [...spgzNames] };
}

function vote(code: string, name: string, isTemplate?: boolean): VotedCode {
  return { code, name: name || code, score: 1, ...(isTemplate ? { isTemplate: true } : {}) };
}

/** Resolve OKPD2/KTRU + КПГЗ/СПГЗ votes for a STE product card (same layout as create flow). */
export function resolveProductCategoryVotes(input: {
  okpd2?: string | null;
  okpd2Name?: string | null;
  ktruCode?: string | null;
  ktruName?: string | null;
  kpgzCode?: string | null;
  kpgzName?: string | null;
  spgzId?: number | null;
  characteristics?: { name: string; value: string }[] | null;
}): {
  okpd2: VotedCode | null;
  ktru: VotedCode | null;
  kpgz: VotedCode | null;
  spgz: VotedCode | null;
} {
  const okpd2Code = input.okpd2?.trim() || "";
  const okpd2 = okpd2Code
    ? vote(okpd2Code, input.okpd2Name?.trim() || okpd2Code)
    : null;

  let ktru: VotedCode | null = null;
  const ktruCode = input.ktruCode?.trim() || "";
  if (ktruCode) {
    const record = getKtruRecord(ktruCode);
    const node = getKtruNodeByCode(ktruCode);
    ktru = {
      ...vote(
        ktruCode,
        input.ktruName?.trim() || record?.name || ktruCode,
        !!record?.isTemplate,
      ),
      variantLabel: node?.variantLabel ?? null,
    };
  } else if (okpd2Code) {
    const sole = getSoleTemplateKtruForOkpd(okpd2Code);
    if (sole) ktru = vote(sole.code, sole.name, true);
  }

  const kpgzCode = input.kpgzCode?.trim() || "";
  const kpgz = kpgzCode
    ? vote(kpgzCode, input.kpgzName?.trim() || kpgzCode)
    : null;

  let spgz: VotedCode | null = null;
  if (input.spgzId != null && Number.isSafeInteger(input.spgzId)) {
    const node = getDirectoryNode("spgz", String(input.spgzId));
    if (node?.kind === "spgz") spgz = vote(node.code, node.name);
  } else if (ktru?.code || kpgzCode) {
    const spec = getCatalogSpecialization({
      ktruCode: ktru?.code ?? null,
      spgzCode: null,
      kpgzCode: kpgzCode || null,
    });
    if (spec) {
      const byName = new Map(
        (input.characteristics ?? []).map((item) => [normSpecName(item.name), item.value.trim()]),
      );
      const selected: Record<string, string> = {};
      for (const characteristic of spec.characteristics) {
        const value = byName.get(normSpecName(characteristic.name)) ?? "";
        if (value) selected[characteristic.name] = value;
      }
      const matches = filterSpecializationOptions(spec.options, selected);
      const match = matches.length === 1 && Object.keys(selected).length === spec.characteristics.length
        ? matches[0]!
        : null;
      if (match?.spgzCode) {
        spgz = vote(match.spgzCode, match.spgzName || match.spgzCode);
        if (match.ktruCode && (!ktru || ktru.isTemplate)) {
          ktru = vote(match.ktruCode, match.ktruName || match.ktruCode, match.ktruCode === spec.templateKtruCode);
        }
      }
    }
  }

  return { okpd2, ktru, kpgz, spgz };
}
