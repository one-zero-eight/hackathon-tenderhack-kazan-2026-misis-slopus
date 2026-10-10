import { spawnSync } from "node:child_process";
import { existsSync, statSync } from "node:fs";
import { gunzipSync } from "node:zlib";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import Database from "better-sqlite3";
import type { KtruChildrenPage, KtruTreeNode } from "../types";
import { classifiersSqlitePath, ktruParquetPath, ktruSqlitePath } from "./config";

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
  okpdParentOf: Map<string, string | null>;
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

function loadOkpdMeta(rows: LightRow[]): { names: Map<string, string>; parentOf: Map<string, string | null> } {
  const names = new Map<string, string>();
  const parentOf = new Map<string, string | null>();
  const classifiersPath = classifiersSqlitePath();
  if (existsSync(classifiersPath)) {
    const classifiers = new Database(classifiersPath, { readonly: true, fileMustExist: true });
    classifiers.pragma("query_only = ON");
    const okpdRows = classifiers.prepare("SELECT id, code, name, parent_id AS parentId FROM okpd").all() as {
      id: number; code: string; name: string; parentId: number | null;
    }[];
    const idToCode = new Map(okpdRows.map(row => [row.id, row.code]));
    for (const row of okpdRows) {
      if (!row.code) continue;
      if (row.name) names.set(row.code, row.name);
      parentOf.set(row.code, row.parentId == null ? null : idToCode.get(row.parentId) ?? null);
    }
    classifiers.close();
  }
  for (const row of rows) {
    if (row.okpd2 && row.okpd2Name && !names.has(row.okpd2)) names.set(row.okpd2, row.okpd2Name);
  }
  return { names, parentOf };
}

function okpdChain(okpd: string, parentOf: Map<string, string | null>): string[] {
  if (!parentOf.size || !parentOf.has(okpd)) return prefixes(okpd);
  const chain: string[] = [];
  const seen = new Set<string>();
  let current: string | null = okpd;
  while (current && !seen.has(current)) {
    seen.add(current);
    chain.push(current);
    if (!parentOf.has(current)) break;
    current = parentOf.get(current) ?? null;
  }
  chain.reverse();
  return chain;
}

function getCatalog(): Catalog {
  if (catalog) return catalog;
  const sqlite = ensureSqlite();
  const db = new Database(sqlite, { readonly: true, fileMustExist: true });
  db.pragma("query_only = ON");
  const rows = db.prepare("SELECT code, name, status, okpd2, okpd2_name AS okpd2Name, parent_code AS parentCode, is_template AS isTemplate FROM ktru").all() as LightRow[];
  const byCode = new Map(rows.map(row => [row.code, row]));
  const { names: okpdNames, parentOf: okpdParentOf } = loadOkpdMeta(rows);
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
    const chain = okpdChain(okpd, okpdParentOf);
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
  catalog = { db, okpdNames, okpdParentOf, okpdChildCodes, okpdKtruRoots, ktruChildren, byCode, total: rows.length };
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
    for (const part of okpdChain(code, data.okpdParentOf)) visible.add(`okpd:${part}`);
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

export type KtruCharacteristicPart = {
  text: string;
  okeiCode?: string;
  okeiName?: string;
};

export type KtruCharacteristic = {
  name: string;
  required: boolean;
  values: string;
  badges: string[];
  code?: string;
  parts: KtruCharacteristicPart[];
};

export type KtruRecordSummary = {
  code: string;
  name: string;
  status: string;
  version: string;
  versionCode: string;
  isTemplate: boolean;
  noNewFeatures: boolean;
  description: string;
  okpd2: { code: string; name: string } | null;
  units: { code: string; name: string }[];
  rubricators: { code: string; name: string }[];
  standards: string[];
  dates: { label: string; value: string }[];
  parent: { code: string; version: string } | null;
  printFormUrl: string | null;
  cancellation: { date: string; reason: string } | null;
  characteristics: KtruCharacteristic[];
};

function parseKtruPayload(payload: Buffer): Record<string, unknown> {
  return JSON.parse(gunzipSync(payload).toString("utf8")) as Record<string, unknown>;
}

function formatRangeOperator(value: string | null | undefined): string {
  switch (value) {
    case "greaterOrEqual": return "≥";
    case "lessOrEqual": return "≤";
    case "greater": return ">";
    case "less": return "<";
    case "equal": return "=";
    default: return value ? String(value) : "";
  }
}

function formatDate(value: unknown): string {
  const text = String(value ?? "").trim();
  if (!text) return "";
  return text.match(/^(\d{4}-\d{2}-\d{2})/)?.[1] || text;
}

function formatKtruValueParts(value: {
  unit?: { code?: string; name?: string } | null;
  text?: string | null;
  concrete_values?: string[] | null;
  ranges?: { min_operator?: string; min?: string; max_operator?: string; max?: string }[] | null;
}): KtruCharacteristicPart[] {
  const chunks: string[] = [];
  if (Array.isArray(value.concrete_values) && value.concrete_values.length) {
    chunks.push(value.concrete_values.map(String).join(", "));
  }
  if (value.text) chunks.push(String(value.text));
  if (Array.isArray(value.ranges)) {
    for (const range of value.ranges) {
      const left = range.min != null && range.min !== "" ? `${formatRangeOperator(range.min_operator)}${range.min}` : "";
      const right = range.max != null && range.max !== "" ? `${formatRangeOperator(range.max_operator)}${range.max}` : "";
      const span = left && right ? `${left} … ${right}` : left || right;
      if (span) chunks.push(span);
    }
  }
  const okeiCode = value.unit?.code ? String(value.unit.code) : undefined;
  const okeiName = value.unit?.name ? String(value.unit.name) : undefined;
  const shortUnit = okeiName?.split(";")[0].trim();
  if (!chunks.length && shortUnit) return [{ text: shortUnit, okeiCode, okeiName }];
  return chunks.map(text => ({
    text: shortUnit ? `${text} ${shortUnit}` : text,
    okeiCode,
    okeiName,
  }));
}

function parseKtruCharacteristics(raw: Record<string, unknown>): KtruCharacteristic[] {
  const list = Array.isArray(raw.characteristics) ? raw.characteristics : [];
  return list.flatMap(item => {
    if (!item || typeof item !== "object") return [];
    const row = item as {
      code?: string;
      name?: string;
      type?: string | number;
      kind?: string | number;
      required?: boolean;
      actual?: boolean;
      choice_type?: string | number;
      values?: {
        unit?: { code?: string; name?: string } | null;
        text?: string | null;
        concrete_values?: string[] | null;
        ranges?: { min_operator?: string; min?: string; max_operator?: string; max?: string }[] | null;
      }[];
    };
    if (row.actual === false) return [];
    const name = String(row.name ?? "").trim();
    if (!name) return [];
    const badges: string[] = [];
    if (row.required) badges.push("обязательная");
    if (String(row.type) === "1") badges.push("качественная");
    if (String(row.type) === "2") badges.push("количественная");
    if (String(row.kind) === "1") badges.push("неизменяемая заказчиком");
    if (String(row.kind) === "2") badges.push("заказчик выбирает одно");
    if (String(row.kind) === "3") badges.push("заказчик выбирает несколько");
    if (String(row.choice_type) === "1") badges.push("единственный выбор");
    if (String(row.choice_type) === "2") badges.push("множественный выбор");
    if (String(row.choice_type) === "3") badges.push("неизменяемый выбор");
    const parts = (row.values ?? []).flatMap(formatKtruValueParts);
    const values = parts.map(part => part.text).filter(Boolean).join(" · ") || "—";
    return [{ name, required: !!row.required, values, badges, code: row.code ? String(row.code) : undefined, parts }];
  });
}

export function getKtruRecord(code: string): KtruRecordSummary | null {
  if (!code || code.length > 64) return null;
  const data = getCatalog();
  const row = data.db.prepare("SELECT payload FROM ktru WHERE code=?").get(code) as { payload: Buffer } | undefined;
  if (!row) return null;
  const raw = parseKtruPayload(row.payload);
  const okpd = raw.okpd2 && typeof raw.okpd2 === "object" ? raw.okpd2 as { code?: string; name?: string } : null;
  const units = Array.isArray(raw.units)
    ? (raw.units as { code?: string; name?: string }[]).flatMap(unit => {
      const unitCode = String(unit.code ?? "").trim();
      if (!unitCode) return [];
      return [{ code: unitCode, name: String(unit.name ?? unitCode).split(";")[0].trim() }];
    })
    : [];
  const rubricators = Array.isArray(raw.rubricators)
    ? (raw.rubricators as { code?: string; name?: string }[]).flatMap(item => {
      const itemCode = String(item.code ?? "").trim();
      if (!itemCode && !item.name) return [];
      return [{ code: itemCode, name: String(item.name ?? "") }];
    })
    : [];
  const standards = Array.isArray((raw.nsi as { standards?: { document_name?: string }[] } | undefined)?.standards)
    ? ((raw.nsi as { standards: { document_name?: string }[] }).standards)
      .map(item => String(item.document_name ?? "").trim())
      .filter(Boolean)
    : [];
  const dates = [
    { label: "Дата включения", value: formatDate(raw.inclusion_date) },
    { label: "Дата публикации", value: formatDate(raw.publish_date) },
    { label: "Дата обновления", value: formatDate(raw.update_date) },
    { label: "Применение с", value: formatDate(raw.application_date_start) },
    { label: "Применение по", value: formatDate(raw.application_date_end) },
  ].filter(item => item.value);
  const parentRaw = raw.parent && typeof raw.parent === "object" ? raw.parent as { code?: string; version?: string } : null;
  const cancelRaw = raw.cancellation && typeof raw.cancellation === "object" ? raw.cancellation as { date?: string; reason?: string } : null;
  const printForm = raw.print_form && typeof raw.print_form === "object" ? raw.print_form as { url?: string } : null;
  return {
    code: String(raw.code ?? code),
    name: String(raw.name ?? ""),
    status: String(raw.status ?? ""),
    version: String(raw.version ?? ""),
    versionCode: String(raw.version_code ?? ""),
    isTemplate: !!raw.is_template,
    noNewFeatures: !!raw.no_new_features,
    description: String(raw.description ?? "").trim(),
    okpd2: okpd?.code ? { code: String(okpd.code), name: String(okpd.name ?? "") } : null,
    units,
    rubricators,
    standards,
    dates,
    parent: parentRaw?.code ? { code: String(parentRaw.code), version: String(parentRaw.version ?? "") } : null,
    printFormUrl: printForm?.url ? String(printForm.url) : null,
    cancellation: cancelRaw ? { date: formatDate(cancelRaw.date), reason: String(cancelRaw.reason ?? "").trim() } : null,
    characteristics: parseKtruCharacteristics(raw),
  };
}

export function getKtruRaw(code: string): Record<string, unknown> | null {
  if (!code || code.length > 64) return null;
  const data = getCatalog();
  const row = data.db.prepare("SELECT payload FROM ktru WHERE code=?").get(code) as { payload: Buffer } | undefined;
  if (!row) return null;
  return parseKtruPayload(row.payload);
}

export function listKtruByOkpd(okpd: string, limit = 12): { total: number; items: { code: string; name: string }[] } {
  if (!okpd) return { total: 0, items: [] };
  const data = getCatalog();
  const total = (data.db.prepare("SELECT count(*) AS n FROM ktru WHERE okpd2=?").get(okpd) as { n: number }).n;
  const items = data.db.prepare("SELECT code, name FROM ktru WHERE okpd2=? ORDER BY code LIMIT ?").all(okpd, limit) as { code: string; name: string }[];
  return { total, items };
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

/** Ancestor+self node ids for expanding the KTRU tree to `code`. */
export function getKtruRevealPath(code: string): { node: KtruTreeNode | null; pathIds: string[] } {
  const node = getKtruNodeByCode(code);
  if (!node) return { node: null, pathIds: [] };
  const data = getCatalog();
  const pathIds: string[] = [];
  if (node.kind === "okpd") {
    for (const part of okpdChain(code, data.okpdParentOf)) pathIds.push(`okpd:${part}`);
    return { node, pathIds };
  }
  const row = data.byCode.get(code)!;
  const okpd = row.okpd2 || code.split("-", 1)[0];
  if (okpd) {
    for (const part of okpdChain(okpd, data.okpdParentOf)) pathIds.push(`okpd:${part}`);
  }
  const ktruChain: string[] = [];
  const seen = new Set<string>();
  let current: string | null = code;
  while (current && data.byCode.has(current) && !seen.has(current)) {
    seen.add(current);
    ktruChain.push(current);
    const parentCode: string | null = data.byCode.get(current)?.parentCode ?? null;
    current = parentCode && data.byCode.has(parentCode) ? parentCode : null;
  }
  ktruChain.reverse();
  for (const item of ktruChain) pathIds.push(`ktru:${item}`);
  return { node, pathIds };
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
