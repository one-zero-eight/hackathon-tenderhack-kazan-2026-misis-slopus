import { spawnSync } from "node:child_process";
import { existsSync, statSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import Database from "better-sqlite3";
import pl from "nodejs-polars";
import type { DirChildrenPage, DirectoryTab, DirTreeNode } from "../types";
import {
  classifiersSqlitePath,
  kpgzParquetPath,
  okpd2ParquetPath,
  okeiParquetPath,
  spgzParquetPath,
} from "./config";
import { getDatabase } from "./database";
import { listKtruByOkpd } from "./ktru-tree";
import { getSteCatalog } from "./ste-catalog";

const CLASSIFIERS_SCHEMA = "classifiers-slim-v2";

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
}

export interface DirCharacteristic {
  name: string;
  value: string;
  required?: boolean;
  badges?: string[];
  code?: string;
  parts?: DirCharacteristicPart[];
}

export interface DirListBlock {
  title: string;
  items: { text: string; href?: string; meta?: string }[];
  collapsible?: boolean;
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
  const sources = ["okpd2-moy-zakupki.parquet", "pp-kpgz.parquet", "spgz.parquet", "okei.parquet"].map(name => resolve(repoRoot(), "data", name));
  const mtime = String(Math.trunc(Math.max(...sources.filter(existsSync).map(path => statSync(path).mtimeMs))));
  const needsBuild = !existsSync(sqlite) || (() => {
    try {
      const probe = new Database(sqlite, { readonly: true, fileMustExist: true });
      const stored = probe.prepare("SELECT value FROM meta WHERE key='source_mtime_ms'").get() as { value: string } | undefined;
      const schema = probe.prepare("SELECT value FROM meta WHERE key='schema'").get() as { value: string } | undefined;
      probe.close();
      return stored?.value !== mtime || schema?.value !== CLASSIFIERS_SCHEMA;
    } catch {
      return true;
    }
  })();
  if (needsBuild) {
    const script = resolve(repoRoot(), "scripts/build_classifiers_sqlite.py");
    const result = spawnSync("uv", ["run", "--with", "pyarrow", "python", script, "--output", sqlite], { cwd: repoRoot(), encoding: "utf8" });
    if (result.status !== 0) throw new Error(`Не удалось построить кэш справочников: ${result.stderr || result.stdout || "unknown error"}`);
  }
  db = new Database(sqlite, { readonly: true, fileMustExist: true });
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
  const q = filter.trim().toLocaleLowerCase("ru-RU");
  if (q.length < 2) return null;
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

  const like = `%${q.replace(/[%_]/g, "")}%`;
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
    const rows = database.prepare("SELECT id FROM okpd WHERE lower(code) LIKE ? OR lower(name) LIKE ? LIMIT ?").all(like, like, FILTER_LIMIT + 1) as { id: number }[];
    truncated = rows.length > FILTER_LIMIT;
    matchCount = truncated ? FILTER_LIMIT : rows.length;
    for (const row of rows.slice(0, FILTER_LIMIT)) {
      matches.add(`okpd:${row.id}`);
      addOkpdAncestors(row.id);
    }
  } else if (tab === "okei") {
    const rows = database.prepare("SELECT id FROM okei WHERE lower(coalesce(code,'')) LIKE ? OR lower(name) LIKE ? LIMIT ?").all(like, like, FILTER_LIMIT + 1) as { id: number }[];
    truncated = rows.length > FILTER_LIMIT;
    matchCount = truncated ? FILTER_LIMIT : rows.length;
    for (const row of rows.slice(0, FILTER_LIMIT)) {
      matches.add(`okei:${row.id}`);
      addOkeiAncestors(row.id);
    }
  } else {
    const kpgzRows = database.prepare("SELECT id FROM kpgz WHERE lower(code) LIKE ? OR lower(name) LIKE ? LIMIT ?").all(like, like, FILTER_LIMIT + 1) as { id: number }[];
    truncated = kpgzRows.length > FILTER_LIMIT;
    matchCount = Math.min(kpgzRows.length, FILTER_LIMIT);
    for (const row of kpgzRows.slice(0, FILTER_LIMIT)) {
      matches.add(`kpgz:${row.id}`);
      addKpgzAncestors(row.id);
    }
    if (tab === "spgz" || tab === "kpgz") {
      const remaining = FILTER_LIMIT - matches.size;
      if (remaining > 0) {
        const spgzRows = database.prepare("SELECT id, kpgz_external_id AS kpgzExternalId FROM spgz WHERE cast(id AS TEXT) LIKE ? OR lower(name) LIKE ? LIMIT ?").all(like, like, remaining + 1) as { id: number; kpgzExternalId: number | null }[];
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

/** Short national labels for common OKEI codes (full names stay in the ОКЕИ field). */
const OKEI_SHORT: Record<string, string> = {
  "003": "мм", "004": "см", "005": "дм", "006": "м", "008": "км",
  "055": "м²", "059": "га",
  "111": "мл", "112": "л", "113": "м³", "116": "мл",
  "161": "мг", "163": "г", "166": "кг", "168": "т", "206": "ц",
  "214": "Вт", "215": "кВт", "222": "В", "226": "А",
  "356": "ч", "359": "сут", "362": "мес", "366": "г",
  "796": "шт", "778": "упак", "839": "компл",
};

function okeiShort(code: string, fullName?: string): string {
  if (OKEI_SHORT[code]) return OKEI_SHORT[code];
  const base = (fullName || "").split(";")[0].trim().toLocaleLowerCase("ru-RU");
  return base || code;
}

type SpgzOkeiRef = { id?: number; code?: string | number | null };

function resolveSpgzOkeis(refs: SpgzOkeiRef[] | null | undefined): { code: string; name: string; short: string }[] {
  if (!Array.isArray(refs) || !refs.length) return [];
  const database = ensureDb();
  const out: { code: string; name: string; short: string }[] = [];
  const seen = new Set<string>();
  for (const ref of refs) {
    const code = String(ref?.code ?? "").trim();
    if (!code || seen.has(code)) continue;
    seen.add(code);
    const row = database.prepare("SELECT name FROM okei WHERE code=? AND kind='unit' LIMIT 1").get(code) as { name: string } | undefined;
    const name = row?.name?.split(";")[0].trim() || code;
    out.push({ code, name, short: okeiShort(code, name) });
  }
  return out;
}

/** Pick position OKEI only when it matches the characteristic dimension (per-char units are absent in SPGZ export). */
function unitForSpgzCharacteristic(name: string, okeis: { code: string; short: string }[]): string {
  if (!okeis.length) return "";
  const n = name.toLocaleLowerCase("ru-RU");
  const mass = /масс|вес|нетто|брутто/.test(n);
  const length = /длин|ширин|высот|толщин|диаметр|глубин|радиус|пролёт|пролет/.test(n);
  const area = /площад/.test(n);
  const volume = /объ[её]м|ёмкост|емкост/.test(n);
  const power = /мощност/.test(n);
  const time = /время|срок|продолжительн|возраст/.test(n);
  const count = /количество|число|кол-во/.test(n);
  const pick = (allowed: string[]) => okeis.find(item => allowed.includes(item.code))?.short || "";
  if (mass) return pick(["161", "163", "166", "168", "206"]);
  if (length) return pick(["003", "004", "005", "006", "008", "018"]);
  if (area) return pick(["055", "059", "058", "061"]);
  if (volume) return pick(["111", "112", "113", "116", "118"]);
  if (power) return pick(["214", "215", "216"]);
  if (time) return pick(["354", "355", "356", "359", "360", "362", "366"]);
  if (count) return pick(["796", "778", "839", "868", "875", "876"]);
  return "";
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

  const unitCode = (() => {
    const short = unitForSpgzCharacteristic(name, okeis);
    return okeis.find(item => item.short === short)?.code || "";
  })();
  const unitMeta = unitCode ? okeis.find(item => item.code === unitCode) : undefined;
  const unitShort = unitMeta?.short || "";
  const withUnit = (value: string) => (unitShort ? `${value} ${unitShort}` : value);

  const enums = (item.characteristicSpgzEnums ?? []).map(entry => String(entry.value ?? "").trim()).filter(Boolean);
  if (enums.length) {
    return {
      name,
      value: enums.join(" · "),
      badges,
      parts: enums.map(text => ({ text })),
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
    badges,
    code: item.kpgzCharacteristicId != null ? String(item.kpgzCharacteristicId) : undefined,
    parts: value === "—" ? [] : [{
      text: value,
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
      lists.push({ title: "Похожие названия", items: similar.map(text => ({ text })) });
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

export function getDirectoryRaw(tab: DirectoryTab, code: string): Record<string, unknown> | null {
  // Full source rows live in parquet; sqlite only keeps tree columns.
  try {
    if (tab === "okpd") {
      const rows = pl.readParquet(okpd2ParquetPath(), {
        columns: ["id", "code", "name", "parent", "level", "is_actual", "children_count", "comment", "visitors_count", "similar_products", "limitations"],
      }).filter(pl.col("code").eq(pl.lit(code))).toRecords();
      return (rows[0] as Record<string, unknown> | undefined) ?? null;
    }
    if (tab === "kpgz") {
      const rows = pl.readParquet(kpgzParquetPath(), {
        columns: ["id", "code", "name", "parentId", "externalId", "treeLevel", "isLastLevel", "okpd", "treePath", "innovation"],
      }).filter(pl.col("code").eq(pl.lit(code))).toRecords();
      return (rows[0] as Record<string, unknown> | undefined) ?? null;
    }
    if (tab === "spgz") {
      const id = Number(code);
      if (!Number.isSafeInteger(id)) return null;
      const rows = pl.readParquet(spgzParquetPath(), {
        columns: [
          "id", "name", "kpgzId", "okpd2Code", "okpdCode", "ktruId", "isExcluded", "spgzCharacteristics", "okeis", "descriptions",
          "auctionProduct", "isStandartProduct", "innovationProduct", "niokr", "applyLifecycle", "allowLimited",
          "allowTwoStage", "allowCall", "regulationRequired", "art", "project",
        ],
      }).filter(pl.col("id").eq(pl.lit(id))).toRecords();
      return (rows[0] as Record<string, unknown> | undefined) ?? null;
    }
    if (tab === "okei") {
      const rows = pl.readParquet(okeiParquetPath()).toRecords() as Array<Record<string, unknown>>;
      const unit = rows.find((row) => row.code != null && String(row.code) === code);
      if (unit) return unit;
      // Synthetic section/group codes: s:{razdel} / g:{razdel}:{podrazdel}
      if (code.startsWith("s:")) {
        const razdel = code.slice(2);
        return rows.find((row) => row.code == null && String(row.podrazdel) === "0" && String(row.razdel) === razdel) ?? null;
      }
      if (code.startsWith("g:")) {
        const [, razdel, podrazdel] = code.split(":");
        return rows.find((row) =>
          row.code == null && String(row.razdel) === razdel && String(row.podrazdel) === podrazdel) ?? null;
      }
      return null;
    }
  } catch {
    return null;
  }
  return null;
}

let indexedIds: Set<number> | undefined;
let indexedFingerprint = "";

function indexedProductIds(): Set<number> {
  const { sqlite } = getDatabase();
  const hasIndex = sqlite.prepare("SELECT 1 AS ok FROM sqlite_master WHERE type='table' AND name='index_state'").get();
  const table = hasIndex ? "index_state" : "products";
  const idColumn = hasIndex ? "product_id" : "id";
  const fingerprint = `${table}:${(sqlite.prepare(`SELECT count(*) AS n FROM ${table}`).get() as { n: number }).n}`;
  if (!indexedIds || indexedFingerprint !== fingerprint) {
    indexedIds = new Set(
      (sqlite.prepare(`SELECT ${idColumn} AS id FROM ${table}`).all() as { id: number }[]).map((row) => row.id),
    );
    indexedFingerprint = fingerprint;
  }
  return indexedIds;
}

function listIndexedCatalogProducts(
  match: (frame: ReturnType<typeof getSteCatalog>["frame"]) => ReturnType<typeof getSteCatalog>["frame"],
  limit: number,
): { total: number; items: DirProductHit[] } {
  const indexed = indexedProductIds();
  const kpgzName = (code: string) => {
    if (!code) return "";
    const row = ensureDb().prepare("SELECT name FROM kpgz WHERE code=? LIMIT 1").get(code) as { name: string } | undefined;
    return row?.name ?? "";
  };
  const rows = match(getSteCatalog().frame)
    .select("id", "name", "okpd2_code", "production_code", "production_name")
    .sort(["name", "id"])
    .toRecords() as {
    id: number;
    name: string | null;
    okpd2_code: string | null;
    production_code: string | null;
    production_name: string | null;
  }[];
  const hits: DirProductHit[] = [];
  let total = 0;
  for (const row of rows) {
    if (!indexed.has(row.id)) continue;
    total += 1;
    if (hits.length >= limit) continue;
    const kpgzCode = (row.production_code ?? "").trim();
    hits.push({
      id: row.id,
      name: (row.name ?? "").trim(),
      okpd2: (row.okpd2_code ?? "").trim(),
      kpgzCode,
      kpgzName: (row.production_name ?? "").trim() || kpgzName(kpgzCode),
    });
  }
  return { total, items: hits };
}

export function listDirectoryProducts(tab: DirectoryTab, code: string, limit = 24): { total: number; items: DirProductHit[] } {
  if (tab === "okpd") {
    return listIndexedCatalogProducts((frame) => frame.filter(pl.col("okpd2_code").eq(pl.lit(code))), limit);
  }
  if (tab === "kpgz") {
    return listIndexedCatalogProducts((frame) => frame.filter(pl.col("production_code").eq(pl.lit(code))), limit);
  }
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
    return listIndexedCatalogProducts(
      (frame) => frame.filter(pl.col("okei").str.toLowerCase().eq(pl.lit(needle))),
      limit,
    );
  }
  if (tab === "ktru") {
    const { sqlite } = getDatabase();
    const total = (sqlite.prepare("SELECT count(*) AS n FROM products WHERE ktru_code = ?").get(code) as { n: number }).n;
    if (!total) return { total: 0, items: [] };
    const items = sqlite.prepare(`
      SELECT id, name,
        coalesce(okpd2, '') AS okpd2,
        coalesce(kpgz_code, '') AS kpgzCode,
        coalesce(kpgz_name, '') AS kpgzName
      FROM products
      WHERE ktru_code = ?
      ORDER BY name LIMIT ?
    `).all(code, limit) as DirProductHit[];
    return { total, items };
  }
  return { total: 0, items: [] };
}
