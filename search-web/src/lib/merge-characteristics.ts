import { isRangeCharacteristic, matchNumberToRange, parseCharacteristicNumber } from "./characteristic-options";
import type { DraftCharacteristic } from "./types";

function normName(name: string): string {
  return name.trim().toLocaleLowerCase("ru").replaceAll("ё", "е");
}

/** Canonical key for option equality (ranges across KTRU/SPGZ formats). */
export function normalizeOptionKey(value: string): string {
  let text = value.trim().toLocaleLowerCase("ru").replaceAll("ё", "е");
  text = text.replace(/\s+/g, "");
  text = text.replace(/[≥≧]/g, ">=").replace(/[≤≦]/g, "<=");
  text = text.replace(/…|\.\.\./g, "..");
  text = text.replace(/(?:кг|мм|см|м|г|шт\.?)$/u, "");
  text = text.replace(/none:none\.\.less:/g, "<");
  text = text.replace(/greaterorequal:([^.]*)\.\.less:/g, ">=$1<");
  text = text.replace(/greaterorequal:([^.]*)\.\.none:none/g, ">=$1");
  text = text.replace(/greaterorequal:/g, ">=");
  text = text.replace(/lessorequal:/g, "<=");
  text = text.replace(/less:/g, "<");
  text = text.replace(/\.\./g, "");
  return text;
}

export function optionsEquivalent(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  const left = new Set(a.map(normalizeOptionKey));
  const right = new Set(b.map(normalizeOptionKey));
  if (left.size !== right.size) return false;
  for (const key of left) if (!right.has(key)) return false;
  return true;
}

export function schemaEquivalent(a: DraftCharacteristic, b: DraftCharacteristic): boolean {
  if (normName(a.name) !== normName(b.name)) return false;
  return optionsEquivalent(a.options ?? [], b.options ?? []);
}

export function mapValueToOptions(value: string, options: string[]): string {
  const raw = value.trim();
  if (!raw || !options.length) return raw;
  const key = normalizeOptionKey(raw);
  return options.find((option) => normalizeOptionKey(option) === key) ?? raw;
}

export type DisplayCharacteristic = DraftCharacteristic & {
  /** Underlying stored row ids this display row writes back to. */
  linkedIds: string[];
};

function sourceLabel(sources: Array<"ktru" | "spgz">): DraftCharacteristic["source"] {
  if (sources.includes("ktru") && sources.includes("spgz")) return "both";
  return sources[0] ?? "ktru";
}

function mergePair(ktru: DraftCharacteristic, spgz: DraftCharacteristic): DisplayCharacteristic {
  const value = ktru.value.trim()
    ? ktru.value
    : mapValueToOptions(spgz.value, ktru.options);
  let rangeOption = ktru.rangeOption;
  if (isRangeCharacteristic(ktru) && !rangeOption) {
    const fromSpgz = mapValueToOptions(spgz.rangeOption || spgz.value, ktru.options);
    const number = parseCharacteristicNumber(spgz.value);
    rangeOption = fromSpgz
      || (number == null ? "" : (matchNumberToRange(number, ktru.ranges) ?? ""));
  }
  return {
    ...ktru,
    id: `merge:${ktru.id}:${spgz.id}`,
    value,
    rangeOption,
    required: ktru.required || spgz.required,
    multiple: ktru.multiple || spgz.multiple,
    source: "both",
    hint: ktru.hint || spgz.hint,
    linkedIds: [ktru.id, spgz.id],
  };
}

/**
 * One table row when KTRU and SPGZ schemas match fully; otherwise keep separate rows.
 * Custom fields stay at the end.
 */
export function buildDisplayCharacteristics(items: DraftCharacteristic[]): DisplayCharacteristic[] {
  const custom = items.filter((item) => item.source === "custom" || item.custom);
  const ktru = items.filter((item) => item.source === "ktru");
  const spgz = items.filter((item) => item.source === "spgz");
  const usedSpgz = new Set<string>();
  const catalog: DisplayCharacteristic[] = [];

  for (const left of ktru) {
    const match = spgz.find((right) => !usedSpgz.has(right.id) && schemaEquivalent(left, right));
    if (match) {
      usedSpgz.add(match.id);
      catalog.push(mergePair(left, match));
    } else {
      catalog.push({ ...left, linkedIds: [left.id], source: sourceLabel(["ktru"]) });
    }
  }
  for (const right of spgz) {
    if (usedSpgz.has(right.id)) continue;
    catalog.push({ ...right, linkedIds: [right.id], source: sourceLabel(["spgz"]) });
  }

  return [
    ...sortRelatedNames(catalog),
    ...custom.map((item) => ({ ...item, linkedIds: [item.id] })),
  ];
}

function namesRelated(a: string, b: string): boolean {
  const left = normName(a);
  const right = normName(b);
  if (left === right) return true;
  if (left.includes(right) || right.includes(left)) return true;
  const tokens = (value: string) =>
    value.split(/[^\p{L}\p{N}]+/u).filter((token) => token.length >= 4);
  const ta = new Set(tokens(left));
  const tb = new Set(tokens(right));
  if (!ta.size || !tb.size) return false;
  let shared = 0;
  for (const token of ta) if (tb.has(token)) shared += 1;
  return shared > 0 && shared / Math.min(ta.size, tb.size) >= 0.5;
}

function sourceRank(source: DraftCharacteristic["source"]): number {
  switch (source) {
    case "both": return 0;
    case "ktru": return 1;
    case "spgz": return 2;
    default: return 3;
  }
}

/** Keep same/similar names adjacent; required clusters first; KTRU before SPGZ within a name. */
function sortRelatedNames(list: DisplayCharacteristic[]): DisplayCharacteristic[] {
  if (list.length <= 1) return list;
  const parent = list.map((_, index) => index);
  const find = (index: number): number => {
    if (parent[index] !== index) parent[index] = find(parent[index]!);
    return parent[index]!;
  };
  const unite = (a: number, b: number) => {
    const pa = find(a);
    const pb = find(b);
    if (pa !== pb) parent[pb] = pa;
  };
  for (let i = 0; i < list.length; i += 1) {
    for (let j = i + 1; j < list.length; j += 1) {
      if (namesRelated(list[i]!.name, list[j]!.name)) unite(i, j);
    }
  }
  const clusterName = new Map<number, string>();
  for (let i = 0; i < list.length; i += 1) {
    const root = find(i);
    const name = normName(list[i]!.name);
    const prev = clusterName.get(root);
    if (!prev || name.length < prev.length) clusterName.set(root, name);
  }
  return list
    .map((item, index) => ({ item, index }))
    .sort((a, b) => {
      const byRequired = Number(b.item.required) - Number(a.item.required);
      if (byRequired) return byRequired;
      const ca = find(a.index);
      const cb = find(b.index);
      const byCluster = (clusterName.get(ca) ?? "").localeCompare(clusterName.get(cb) ?? "", "ru");
      if (byCluster) return byCluster;
      if (ca === cb) {
        const byName = normName(a.item.name).localeCompare(normName(b.item.name), "ru");
        if (byName) return byName;
        return sourceRank(a.item.source) - sourceRank(b.item.source);
      }
      return a.index - b.index;
    })
    .map(({ item }) => item);
}

export function characteristicSourceLabel(source: DraftCharacteristic["source"]): string {
  switch (source) {
    case "both": return "из КТРУ, СПГЗ";
    case "spgz": return "из СПГЗ";
    case "custom": return "доп.";
    default: return "из КТРУ";
  }
}
