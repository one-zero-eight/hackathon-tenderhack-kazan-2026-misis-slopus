import { existsSync, statSync } from "node:fs";
import { gunzipSync } from "node:zlib";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import Database from "better-sqlite3";
import {
  buildCharacteristicHint,
  buildCharacteristicOptionLabels,
  dedupeCharacteristicOptions,
} from "../characteristic-options";
import {
  pickDiscriminatingCharacteristics,
  specializationOptionLabel,
  type SpecChild,
} from "../specialization";
import type { CharacteristicRange, KtruChildrenPage, KtruSpecialization, KtruTreeNode } from "../types";
import { classifiersSqlitePath, ktruParquetPath, ktruSqlitePath, moyKtruParquetPath } from "./config";

import { getOkeiUnit } from "./okei-unit";

interface LightRow {
  code: string;
  name: string;
  officialName: string;
  status: string | null;
  okpd2: string | null;
  okpd2Name: string | null;
  parentCode: string | null;
  isTemplate: number;
}

interface Catalog {
  db: Database.Database;
  statements: Map<string, Database.Statement>;
  okpdNames: Map<string, string>;
  okpdParentOf: Map<string, string | null>;
  okpdChildCodes: Map<string, string[]>;
  okpdCodes: Set<string>;
  total: number;
}

const SCHEMA_VERSION = "ktru-mmap-v2";
const LIGHT_COLUMNS = "code, name, official_name AS officialName, status, okpd2, okpd2_name AS okpd2Name, parent_code AS parentCode, is_template AS isTemplate";
const LIGHT_CACHE_LIMIT = 512;
const SPECIALIZATION_CACHE_LIMIT = 32;
const FILTER_CACHE_LIMIT = 8;
const SPECIALIZATION_CACHE_OPTION_LIMIT = 800;
const lightCache = new Map<string, LightRow | null>();

function statement(data: Catalog, sql: string): Database.Statement {
  let prepared = data.statements.get(sql);
  if (!prepared) {
    prepared = data.db.prepare(sql);
    if (data.statements.size >= 128) data.statements.delete(data.statements.keys().next().value!);
    data.statements.set(sql, prepared);
  }
  return prepared;
}

function lightRow(code: string, data: Catalog): LightRow | null {
  if (lightCache.has(code)) {
    const row = lightCache.get(code)!;
    lightCache.delete(code);
    lightCache.set(code, row);
    return row;
  }
  const row = statement(data, `SELECT ${LIGHT_COLUMNS} FROM ktru WHERE code=?`).get(code) as LightRow | undefined;
  if (lightCache.size >= LIGHT_CACHE_LIMIT) lightCache.delete(lightCache.keys().next().value!);
  lightCache.set(code, row ?? null);
  return row ?? null;
}

const CHILDREN_PAGE = 200;
const FILTER_MATCH_LIMIT = 800;
let catalog: Catalog | undefined;
const filterCache = new Map<string, FilterSets>();
const specializationCache = new Map<string, KtruSpecialization | null>();
const RECORD_CACHE_LIMIT = 128;
const recordCache = new Map<string, KtruRecordSummary>();

interface FilterSets {
  visible: Set<string>;
  matches: Set<string>;
  matchCount: number;
  truncated: boolean;
  ktruCodes: string;
}

function repoRoot(): string {
  return resolve(dirname(fileURLToPath(import.meta.url)), "../../../..");
}

function ensureSqlite(): string {
  const parquet = ktruParquetPath();
  const names = moyKtruParquetPath();
  const sqlite = ktruSqlitePath();
  const command = `uv run --with pyarrow python ${resolve(repoRoot(), "scripts/build_ktru_sqlite.py")} --source ${JSON.stringify(parquet)} --names ${JSON.stringify(names)} --output ${JSON.stringify(sqlite)}`;
  const rebuildError = () => new Error(`Кэш КТРУ отсутствует, устарел или несовместим. Постройте его явно: ${command}`);
  if (!existsSync(parquet)) throw rebuildError();
  const parquetMtime = String(Math.trunc(statSync(parquet).mtimeMs));
  const namesMtime = existsSync(names) ? String(Math.trunc(statSync(names).mtimeMs)) : "";
  const needsBuild = !existsSync(sqlite) || (() => {
    try {
      const db = new Database(sqlite, { readonly: true, fileMustExist: true });
      try {
        const meta = (key: string) =>
          (db.prepare("SELECT value FROM meta WHERE key=?").get(key) as { value: string } | undefined)?.value;
        return meta("source_mtime_ms") !== parquetMtime
          || meta("names_mtime_ms") !== namesMtime
          || meta("schema") !== SCHEMA_VERSION
          || meta("lexical_schema") !== "folded-grams-v1"
          || !meta("source") || resolve(repoRoot(), meta("source")!) !== parquet
          || (meta("names_source") || "") !== (existsSync(names) ? (names.startsWith(`${repoRoot()}/`) ? names.slice(repoRoot().length + 1) : names) : "");
      } finally {
        db.close();
      }
    } catch {
      return true;
    }
  })();
  if (needsBuild) throw rebuildError();
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

function loadOkpdMeta(rows: { code: string; name: string }[]): { names: Map<string, string>; parentOf: Map<string, string | null> } {
  const names = new Map<string, string>();
  const parentOf = new Map<string, string | null>();
  const classifiersPath = classifiersSqlitePath();
  if (existsSync(classifiersPath)) {
    const classifiers = new Database(classifiersPath, { readonly: true, fileMustExist: true });
    classifiers.pragma("query_only = ON");
    classifiers.pragma("mmap_size = 1073741824");
    classifiers.pragma("cache_size = -2048");
    classifiers.pragma("temp_store = FILE");
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
    if (row.code && row.name && !names.has(row.code)) names.set(row.code, row.name);
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
  db.pragma("mmap_size = 1073741824");
  db.pragma("cache_size = -2048");
  db.pragma("temp_store = FILE");
  // Only the small, precomputed OKPD taxonomy is retained on the JS heap.
  const rows = db.prepare("SELECT code, name FROM ktru_okpd").all() as { code: string; name: string }[];
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
  const okpdCodes = new Set<string>();
  for (const row of rows) {
    if (!row.code) continue;
    okpdCodes.add(row.code);
    const chain = okpdChain(row.code, okpdParentOf);
    addChild("", chain[0]!);
    for (let i = 1; i < chain.length; i++) addChild(chain[i - 1]!, chain[i]!);
  }
  const okpdChildCodes = new Map<string, string[]>();
  for (const [parent, set] of okpdChildSet) okpdChildCodes.set(parent, [...set].sort(compareCodes));
  db.function("ktru_token_score", { deterministic: true }, (text: unknown, token: unknown) => tokenScore(String(text), String(token)));
  const total = Number((db.prepare("SELECT value FROM meta WHERE key='record_count'").get() as { value: string }).value);
  catalog = { db, statements: new Map(), okpdNames, okpdParentOf, okpdChildCodes, okpdCodes, total };
  return catalog;
}

function variantLabelFor(code: string, data: Catalog, pageSpecializations?: Map<string, KtruSpecialization | null>): string | null {
  const row = lightRow(code, data);
  if (!row || row.isTemplate || !row.parentCode) return null;
  const parent = lightRow(row.parentCode, data);
  if (!parent?.isTemplate) return null;
  let spec: KtruSpecialization | null;
  if (pageSpecializations?.has(row.parentCode)) {
    spec = pageSpecializations.get(row.parentCode)!;
  } else {
    spec = specializationForTemplate(row.parentCode, data);
    pageSpecializations?.set(row.parentCode, spec);
  }
  const option = spec?.options.find(item => item.code === code);
  if (!spec || !option) return null;
  // Always surface discriminating characteristics in the tree, even when the
  // display name already contains the value (e.g. «Молоток слесарный»).
  return specializationOptionLabel(spec.characteristics, option.values) || null;
}

function okpdNode(code: string, data: Catalog, childCount?: number): KtruTreeNode {
  const okpdKids = data.okpdChildCodes.get(code) ?? [];
  const count = childCount ?? (okpdKids.length || ktruChildCount({ kind: "ktru", where: "effective_okpd=? AND is_okpd_root=1", key: code }, data));
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

function ktruNode(code: string, data: Catalog, childCount?: number, pageSpecializations?: Map<string, KtruSpecialization | null>): KtruTreeNode {
  const row = lightRow(code, data)!;
  const count = childCount ?? ktruChildCount({ kind: "ktru", where: "parent_code=?", key: code }, data);
  return {
    id: `ktru:${code}`,
    code,
    name: row.name,
    kind: "ktru",
    status: row.status,
    isTemplate: !!row.isTemplate,
    childCount: count,
    hasChildren: count > 0,
    variantLabel: variantLabelFor(code, data, pageSpecializations),
  };
}

function pageSlice<T>(items: T[], offset: number, limit = CHILDREN_PAGE): { items: T[]; total: number; offset: number; hasMore: boolean } {
  const total = items.length;
  const start = Math.max(0, offset);
  const slice = items.slice(start, start + limit);
  return { items: slice, total, offset: start, hasMore: start + slice.length < total };
}

function normalizeFilter(filter: string): string {
  return filter.trim().normalize("NFKC").toLocaleLowerCase("ru-RU").replaceAll("ё", "е").replace(/\s+/g, " ");
}

function gramQuery(text: string): string {
  const points = [...text];
  const grams = new Set<string>();
  if (points.length === 1) grams.add(`g${points[0]!.codePointAt(0)!.toString(16).padStart(6, "0")}`);
  for (let i = 1; i < points.length; i++) {
    grams.add(`g${points[i - 1]!.codePointAt(0)!.toString(16).padStart(6, "0")}${points[i]!.codePointAt(0)!.toString(16).padStart(6, "0")}`);
  }
  return `(${[...grams].join(" AND ")})`;
}

function lexicalTokens(query: string): string[] {
  return [...new Set(query.match(/[\p{L}\p{N}./№+%\-]+/gu) ?? [])];
}

function tokenStem(token: string): string {
  if (!/^[а-я]+$/.test(token) || token.length < 5) return token;
  const stem = token.replace(/(?:иями|ами|ого|ему|ому|ыми|ими|ая|яя|ое|ее|ые|ие|ый|ий|ой|ую|юю|ов|ев|ам|ям|ах|ях|ом|ем|а|я|ы|и|у|ю|е|о|ь)$/u, "");
  return stem.length >= 4 ? stem : token;
}

function oneEdit(left: string, right: string): boolean {
  if (Math.abs(left.length - right.length) > 1) return false;
  let i = 0; let j = 0; let edits = 0;
  while (i < left.length && j < right.length) {
    if (left[i] === right[j]) { i++; j++; continue; }
    if (++edits > 1) return false;
    if (left.length >= right.length) i++;
    if (right.length >= left.length) j++;
  }
  return edits + Number(i < left.length || j < right.length) <= 1;
}

function tokenScore(text: string, token: string): number {
  const words = lexicalTokens(text);
  if (words.includes(token)) return 5;
  if (/^\d+$/.test(token)) return 0;
  if (words.some(word => word.startsWith(token))) return 4;
  if (text.includes(token)) return 3;
  const stem = tokenStem(token);
  if (words.some(word => tokenStem(word) === stem || (/^[а-я]+$/.test(token) && stem.length >= 4 && word.startsWith(stem)))) return 2;
  if (/^[а-я]+$/.test(token) && token.length >= 6 && words.some(word => oneEdit(word, token))) return 1;
  return 0;
}

function tokenCandidate(token: string): string {
  const stem = tokenStem(token);
  const alternatives = new Set([gramQuery(stem)]);
  // A single edit destroys at most two adjacent bigrams. Omit those pairs,
  // then verify actual edit distance on the indexed candidates only.
  if (/^[а-я]+$/.test(token) && token.length >= 6) {
    for (let index = 0; index < token.length; index++) {
      const pieces = [token.slice(0, index), token.slice(index + 1)].filter(piece => piece.length >= 2);
      if (pieces.length) alternatives.add(`(${pieces.map(gramQuery).join(" AND ")})`);
    }
  }
  return `(${[...alternatives].join(" OR ")})`;
}

function lexicalFilter(q: string) {
  const tokens = lexicalTokens(q);
  if (!tokens.length) return null;
  const fields = "name_fold || ' ' || official_name_fold || ' ' || synonyms_fold";
  return {
    predicate: `rowid IN (SELECT rowid FROM ktru_search WHERE ktru_search MATCH ?) AND ${tokens.map(token => `ktru_token_score(${/[.-]/.test(token) || /^\d{2,}$/.test(token) ? `code || ' ' || ${fields}` : fields},?)>0`).join(" AND ")}`,
    params: [tokens.map(tokenCandidate).join(" AND "), ...tokens],
    order: `CASE WHEN code=? THEN 0 WHEN name_fold=? OR official_name_fold=? THEN 1 WHEN instr(name_fold,?)>0 OR instr(official_name_fold,?)>0 THEN 2 ELSE 3 END, (${tokens.map(() => `ktru_token_score(${fields},?)`).join(" + ")}) DESC, status='ACTIVE' DESC, length(name), code`,
    ranking: [q, q, q, q, q, ...tokens],
  };
}

function buildFilterSets(query: string): FilterSets | null {
  const q = normalizeFilter(query);
  if ([...q].length < 2) return null;
  const cached = filterCache.get(q);
  if (cached) return cached;
  const data = getCatalog();
  const matches = new Set<string>();
  const visible = new Set<string>();
  let matchCount = 0;
  const markOkpdAncestors = (code: string) => {
    for (const part of okpdChain(code, data.okpdParentOf)) visible.add(`okpd:${part}`);
  };
  const okpdMatches = new Set<string>();
  for (const codes of data.okpdChildCodes.values()) {
    for (const code of codes) {
      const text = `${code} ${normalizeFilter(data.okpdNames.get(code) || "")}`;
      const tokens = lexicalTokens(q);
      if (!tokens.length || !tokens.every(token => tokenScore(text, token) > 0)) continue;
      okpdMatches.add(code);
    }
  }
  const filter = lexicalFilter(q);
  if (!filter) return null;
  const total = (statement(data, `SELECT count(*) AS n FROM ktru WHERE ${filter.predicate}`).get(...filter.params) as { n: number }).n;
  matchCount += total;
  const rows = statement(data, `SELECT ${LIGHT_COLUMNS} FROM ktru WHERE ${filter.predicate} ORDER BY ${filter.order} LIMIT ?`)
    .all(...filter.params, ...filter.ranking, FILTER_MATCH_LIMIT - matches.size) as LightRow[];
  for (const row of rows) {
    const id = `ktru:${row.code}`;
    matches.add(id);
    visible.add(id);
    let parent = row.parentCode;
    const seen = new Set<string>([row.code]);
    while (parent && !seen.has(parent)) {
      const parentRow = lightRow(parent, data);
      if (!parentRow) break;
      seen.add(parent);
      visible.add(`ktru:${parent}`);
      parent = parentRow.parentCode;
    }
    const okpd = row.okpd2 || row.code.split("-", 1)[0];
    if (okpd) markOkpdAncestors(okpd);
  }
  matchCount += okpdMatches.size;
  for (const code of okpdMatches) {
    if (matches.size >= FILTER_MATCH_LIMIT) break;
    matches.add(`okpd:${code}`);
    markOkpdAncestors(code);
  }
  for (const id of matches) visible.add(id);
  const ktruCodes = JSON.stringify([...visible].filter(id => id.startsWith("ktru:")).map(id => id.slice(5)));
  const sets = { visible, matches, matchCount, truncated: matchCount > matches.size, ktruCodes };
  if (filterCache.size >= FILTER_CACHE_LIMIT) filterCache.delete(filterCache.keys().next().value!);
  filterCache.set(q, sets);
  return sets;
}

type ChildSource = { kind: "okpd"; codes: string[] } | { kind: "ktru"; where: string; key: string };

function childSource(parentId: string | null, data: Catalog): ChildSource {
  if (!parentId) return { kind: "okpd", codes: data.okpdChildCodes.get("") ?? [] };
  if (parentId.startsWith("okpd:")) {
    const code = parentId.slice(5);
    const next = data.okpdChildCodes.get(code) ?? [];
    if (next.length) return { kind: "okpd", codes: next };
    return { kind: "ktru", where: "effective_okpd=? AND is_okpd_root=1", key: code };
  }
  if (parentId.startsWith("ktru:")) {
    const code = parentId.slice(5);
    return { kind: "ktru", where: "parent_code=? AND effective_okpd<>'' AND EXISTS (SELECT 1 FROM ktru AS parent WHERE parent.code=ktru.parent_code)", key: code };
  }
  throw new Error(`Неизвестный узел дерева: ${parentId}`);
}

function childParams(source: Extract<ChildSource, { kind: "ktru" }>, filter?: FilterSets): string[] {
  const params = [source.key];
  if (filter) params.push(filter.ktruCodes);
  return params;
}

function childWhere(source: Extract<ChildSource, { kind: "ktru" }>, filter?: FilterSets): string {
  return source.where + (filter ? " AND code IN (SELECT value FROM json_each(?))" : "");
}

function ktruChildCount(source: Extract<ChildSource, { kind: "ktru" }>, data: Catalog, filter?: FilterSets): number {
  return (statement(data, `SELECT count(*) AS n FROM ktru WHERE ${childWhere(source, filter)}`)
    .get(...childParams(source, filter)) as { n: number }).n;
}

function filteredChildCount(id: string, data: Catalog, filter: FilterSets): number {
  const source = childSource(id, data);
  return source.kind === "ktru" ? ktruChildCount(source, data, filter)
    : source.codes.filter(code => filter.visible.has(`okpd:${code}`)).length;
}

export function ktruStats() {
  const data = getCatalog();
  return { total: data.total, roots: data.okpdChildCodes.get("")?.length ?? 0 };
}

export function listKtruChildren(parentId: string | null = null, offset = 0, filter = ""): KtruChildrenPage {
  const data = getCatalog();
  const sets = buildFilterSets(filter);
  const source = childSource(parentId, data);
  const start = Math.max(0, Math.trunc(offset) || 0);
  const page = source.kind === "okpd"
    ? pageSlice(sets ? source.codes.filter(code => sets.visible.has(`okpd:${code}`)) : source.codes, start)
    : (() => {
      const total = ktruChildCount(source, data, sets ?? undefined);
      const rows = statement(data, `SELECT code FROM ktru WHERE ${childWhere(source, sets ?? undefined)} ORDER BY code LIMIT ? OFFSET ?`)
        .all(...childParams(source, sets ?? undefined), CHILDREN_PAGE, start) as { code: string }[];
      const items = rows.map(row => row.code);
      return { items, total, offset: start, hasMore: start + items.length < total };
    })();
  const pageSpecializations = new Map<string, KtruSpecialization | null>();
  const nodes = page.items.map(code => {
    const id = `${source.kind}:${code}`;
    const childCount = sets ? filteredChildCount(id, data, sets) : undefined;
    return source.kind === "okpd" ? okpdNode(code, data, childCount) : ktruNode(code, data, childCount, pageSpecializations);
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
  unit?: string;
};

export type KtruCharacteristic = {
  name: string;
  required: boolean;
  values: string;
  badges: string[];
  code?: string;
  parts: KtruCharacteristicPart[];
  options: string[];
  optionLabels: Record<string, string>;
  ranges: CharacteristicRange[];
  unit?: string;
  okeiCode?: string;
  hint: string;
  multiple: boolean;
};

export type KtruRecordSummary = {
  code: string;
  /** Display name (beautiful mirror name when available). */
  name: string;
  /** Official EIS name from ktru-actual. */
  officialName: string;
  status: string;
  version: string;
  versionCode: string;
  isTemplate: boolean;
  noNewFeatures: boolean | null;
  noNewFeaturesReason: string;
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

function parseRangeNumber(value: string | null | undefined): number | null {
  if (value == null || value === "") return null;
  const number = Number(String(value).replace(",", "."));
  return Number.isFinite(number) ? number : null;
}

function buildCharacteristicRanges(
  values: {
    unit?: { code?: string; name?: string } | null;
    text?: string | null;
    concrete_values?: string[] | null;
    ranges?: { min_operator?: string; min?: string; max_operator?: string; max?: string }[] | null;
  }[],
): CharacteristicRange[] {
  const rangeOnly = values.length > 0 && values.every((entry) =>
    Array.isArray(entry.ranges) && entry.ranges.length > 0
    && !entry.text
    && !(Array.isArray(entry.concrete_values) && entry.concrete_values.length),
  );
  if (!rangeOnly) return [];
  const out: CharacteristicRange[] = [];
  for (const entry of values) {
    const parts = formatKtruValueParts(entry);
    for (const [index, range] of (entry.ranges ?? []).entries()) {
      const option = parts[index]?.text || parts[0]?.text;
      if (!option) continue;
      out.push({
        option,
        min: parseRangeNumber(range.min),
        max: parseRangeNumber(range.max),
        minInclusive: range.min_operator !== "greater",
        maxInclusive: range.max_operator !== "less",
      });
    }
  }
  return out;
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
    chunks.push(...value.concrete_values.map(String));
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
  const metadata = okeiCode ? getOkeiUnit(okeiCode) : undefined;
  const okeiName = metadata?.name ?? value.unit?.name;
  const shortUnit = metadata?.short ?? okeiName ?? "";
  if (!chunks.length && shortUnit) return [{ text: shortUnit, okeiCode, okeiName, unit: shortUnit }];
  return chunks.map(text => ({
    text: shortUnit ? `${text} ${shortUnit}` : text,
    okeiCode,
    okeiName,
    unit: shortUnit,
  }));
}

function parseKtruCharacteristics(raw: Record<string, unknown>, standards: string[] = []): KtruCharacteristic[] {
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
    const ranges = buildCharacteristicRanges(row.values ?? []);
    const badges: string[] = [];
    if (row.required) badges.push("обязательная");
    if (String(row.type) === "1") badges.push("качественная");
    if (String(row.type) === "2") badges.push("количественная");
    // kind — режим для заказчика в закупке (ТЗ), не блокировка поля у поставщика.
    // choice_type в выгрузке часто = «есть варианты в справочнике», а не мультивыбор.
    const multiple = String(row.kind) === "3";
    const parts = (row.values ?? []).flatMap(formatKtruValueParts);
    const values = parts.map(part => part.text).filter(Boolean).join(" · ") || "—";
    const options = dedupeCharacteristicOptions(parts.map(part => part.text).filter(Boolean));
    const unitPart = parts.find((part) => part.okeiCode || part.unit);
    const unit = unitPart?.unit ?? "";
    const optionLabels = buildCharacteristicOptionLabels(name, options, standards);
    const hint = buildCharacteristicHint({ name, options, unit, standards });
    return [{
      name,
      required: !!row.required,
      values,
      badges,
      code: row.code ? String(row.code) : undefined,
      parts,
      options,
      optionLabels,
      ranges,
      unit: unit || undefined,
      okeiCode: unitPart?.okeiCode,
      hint,
      multiple,
    }];
  });
}

export function getKtruRecord(code: string): KtruRecordSummary | null {
  if (!code || code.length > 64) return null;
  const cached = recordCache.get(code);
  if (cached) {
    recordCache.delete(code);
    recordCache.set(code, cached);
    return structuredClone(cached);
  }
  const data = getCatalog();
  const row = data.db.prepare("SELECT payload, name, official_name AS officialName FROM ktru WHERE code=?").get(code) as {
    payload: Buffer; name: string; officialName: string;
  } | undefined;
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
  const officialName = row.officialName || String(raw.name ?? "");
  const result: KtruRecordSummary = {
    code: String(raw.code ?? code),
    name: row.name || officialName,
    officialName,
    status: String(raw.status ?? ""),
    version: String(raw.version ?? ""),
    versionCode: String(raw.version_code ?? ""),
    isTemplate: !!raw.is_template,
    noNewFeatures: raw.no_new_features == null ? null : !!raw.no_new_features,
    noNewFeaturesReason: String(raw.no_new_features_reason ?? "").trim(),
    description: String(raw.description ?? "").trim(),
    okpd2: okpd?.code ? { code: String(okpd.code), name: String(okpd.name ?? "") } : null,
    units,
    rubricators,
    standards,
    dates,
    parent: parentRaw?.code ? { code: String(parentRaw.code), version: String(parentRaw.version ?? "") } : null,
    printFormUrl: printForm?.url ? String(printForm.url) : null,
    cancellation: cancelRaw ? { date: formatDate(cancelRaw.date), reason: String(cancelRaw.reason ?? "").trim() } : null,
    characteristics: parseKtruCharacteristics(raw, standards),
  };
  if (recordCache.size >= RECORD_CACHE_LIMIT) recordCache.delete(recordCache.keys().next().value!);
  recordCache.set(code, result);
  return structuredClone(result);
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
  const boundedLimit = Math.min(FILTER_MATCH_LIMIT, Math.max(0, Math.trunc(limit) || 0));
  const items = statement(data, "SELECT code, name FROM ktru WHERE okpd2=? ORDER BY code LIMIT ?").all(okpd, boundedLimit) as { code: string; name: string }[];
  return { total, items };
}

/**
 * If OKPD2 has exactly one KTRU root and it is an active template — safe auto-pick for STE.
 * Example: 25.73.30.141 → 25.73.30.141-00000011 (ШАБЛОН).
 */
export function getSoleTemplateKtruForOkpd(okpd2: string): { code: string; name: string } | null {
  if (!okpd2) return null;
  const data = getCatalog();
  const roots = statement(data, `SELECT ${LIGHT_COLUMNS} FROM ktru WHERE effective_okpd=? AND is_okpd_root=1 ORDER BY code LIMIT 2`)
    .all(okpd2) as LightRow[];
  if (roots.length !== 1) return null;
  const row = roots[0];
  if (!row?.isTemplate) return null;
  if (row.status && row.status !== "ACTIVE") return null;
  return { code: row.code, name: row.name };
}

function characteristicValueTexts(raw: Record<string, unknown>): Map<string, { values: string[]; code: string | null; required: boolean; kind: string }> {
  const out = new Map<string, { values: string[]; code: string | null; required: boolean; kind: string }>();
  const list = Array.isArray(raw.characteristics) ? raw.characteristics : [];
  for (const item of list) {
    if (!item || typeof item !== "object") continue;
    const row = item as {
      code?: string;
      name?: string;
      kind?: string | number;
      required?: boolean;
      actual?: boolean;
      values?: {
        unit?: { code?: string; name?: string } | null;
        text?: string | null;
        concrete_values?: string[] | null;
        ranges?: { min_operator?: string; min?: string; max_operator?: string; max?: string }[] | null;
      }[];
    };
    if (row.actual === false) continue;
    const name = String(row.name ?? "").trim();
    if (!name) continue;
    const values = dedupeCharacteristicOptions((row.values ?? []).flatMap(formatKtruValueParts).map(part => part.text).filter(Boolean));
    out.set(name, {
      values,
      code: row.code ? String(row.code) : null,
      required: !!row.required,
      kind: String(row.kind ?? ""),
    });
  }
  return out;
}

/**
 * For a template (or a child under a template): find the compact characteristic set that
 * partitions sibling positions, and map each value tuple → child KTRU code.
 */
export function getKtruSpecialization(code: string): KtruSpecialization | null {
  if (!code || code.length > 64) return null;
  const data = getCatalog();
  const row = lightRow(code, data);
  if (!row) return null;

  let templateCode = code;
  if (!row.isTemplate) {
    const parent = row.parentCode ? lightRow(row.parentCode, data) : null;
    if (!parent?.isTemplate) return null;
    templateCode = parent.code;
  }

  const cached = specializationForTemplate(templateCode, data);
  if (!cached) return null;
  const clone = structuredClone(cached);
  const current = clone.options.find(item => item.code === code);
  return {
    ...clone,
    currentValues: current ? { ...current.values } : null,
  };
}

function specializationForTemplate(templateCode: string, data: Catalog): KtruSpecialization | null {
  if (specializationCache.has(templateCode)) return specializationCache.get(templateCode)!;
  const specialization = buildKtruSpecialization(templateCode, data);
  // An API response may contain a whole sibling group, but never retain large groups.
  if (specialization && specialization.options.length > SPECIALIZATION_CACHE_OPTION_LIMIT) return specialization;
  if (specializationCache.size >= SPECIALIZATION_CACHE_LIMIT) specializationCache.delete(specializationCache.keys().next().value!);
  specializationCache.set(templateCode, specialization);
  return specialization;
}

function buildKtruSpecialization(templateCode: string, data: Catalog): KtruSpecialization | null {
  const children: SpecChild[] = [];
  let afterCode = "";
  // Keyset pages and one decoded payload at a time, even for huge sibling groups.
  while (true) {
    const rows = statement(data, "SELECT code, name FROM ktru WHERE parent_code=? AND code>? AND effective_okpd<>'' AND (status IS NULL OR status='' OR status='ACTIVE') ORDER BY code LIMIT ?")
      .all(templateCode, afterCode, CHILDREN_PAGE) as { code: string; name: string }[];
    if (!rows.length) break;
    for (const child of rows) {
      const rowPayload = statement(data, "SELECT payload FROM ktru WHERE code=?").get(child.code) as { payload: Buffer };
      const chars = characteristicValueTexts(parseKtruPayload(rowPayload.payload));
      const values = new Map<string, { value: string; code: string | null; required?: boolean; kind?: string }>();
      for (const [name, entry] of chars) {
        if (entry.values.length !== 1) continue;
        values.set(name, {
          value: entry.values[0]!,
          code: entry.code,
          required: entry.required,
          kind: entry.kind,
        });
      }
      children.push({ code: child.code, name: child.name, values });
    }
    afterCode = rows[rows.length - 1]!.code;
  }
  if (children.length < 2) return null;

  const picked = pickDiscriminatingCharacteristics(children);
  if (!picked) return null;

  const characteristics = picked.characteristics.map(({ name, code }) => ({ name, code }));
  const options: KtruSpecialization["options"] = [];
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
  const templateRow = lightRow(templateCode, data)!;
  return {
    templateCode,
    templateName: templateRow.name,
    characteristics,
    options,
    currentValues: null,
  };
}

export function getKtruNodeByCode(code: string): KtruTreeNode | null {
  if (!code || code.length > 64) return null;
  const data = getCatalog();
  if (lightRow(code, data)) return ktruNode(code, data);
  if (data.okpdNames.has(code) || data.okpdChildCodes.has(code) || data.okpdCodes.has(code)) {
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
  const row = lightRow(code, data)!;
  const okpd = row.okpd2 || code.split("-", 1)[0];
  if (okpd) {
    for (const part of okpdChain(okpd, data.okpdParentOf)) pathIds.push(`okpd:${part}`);
  }
  const ktruChain: string[] = [];
  const seen = new Set<string>();
  let current: string | null = code;
  while (current && !seen.has(current)) {
    const currentRow = lightRow(current, data);
    if (!currentRow) break;
    seen.add(current);
    ktruChain.push(current);
    current = currentRow.parentCode;
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

const SEARCH_LIMIT = 40;
export function searchKtru(query: string): KtruTreeNode[] {
  const q = normalizeFilter(query);
  if ([...q].length < 2) return [];
  const filter = lexicalFilter(q);
  if (!filter) return [];
  const data = getCatalog();
  const rows = statement(data, `SELECT code FROM ktru WHERE ${filter.predicate} ORDER BY ${filter.order} LIMIT ?`)
    .all(...filter.params, ...filter.ranking, SEARCH_LIMIT) as { code: string }[];
  const pageSpecializations = new Map<string, KtruSpecialization | null>();
  return rows.map(row => ktruNode(row.code, data, undefined, pageSpecializations));
}
