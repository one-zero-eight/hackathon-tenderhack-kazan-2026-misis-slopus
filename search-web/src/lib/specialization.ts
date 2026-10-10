import type { SpecializationCharacteristic } from "./types";

export type SpecChildValue = {
  value: string;
  code: string | null;
  required?: boolean;
  kind?: string;
};

export type SpecChild = {
  code: string;
  name: string;
  values: Map<string, SpecChildValue>;
};

export type SpecCharacteristicPick = SpecializationCharacteristic & {
  score: number;
};

const MAX_SET_SIZE = 3;
const MAX_COMBINATION_CANDIDATES = 12;

export function normSpecName(name: string): string {
  return name.trim().toLocaleLowerCase("ru").replaceAll("ё", "е");
}

export function specializationOptionLabel(
  characteristics: SpecializationCharacteristic[],
  values: Record<string, string>,
): string {
  return characteristics
    .map((item) => {
      const value = values[item.name]?.trim() ?? "";
      return value ? `${item.name}: ${value}` : "";
    })
    .filter(Boolean)
    .join(" · ");
}

export function specializationValuesKey(
  characteristics: SpecializationCharacteristic[],
  values: Record<string, string>,
): string {
  return characteristics.map((item) => values[item.name] ?? "").join("\0");
}

/** Options whose values match every provided (non-empty) selection. */
export function filterSpecializationOptions<T extends { values: Record<string, string> }>(
  options: T[],
  selected: Record<string, string>,
): T[] {
  const entries = Object.entries(selected).filter(([, value]) => value.trim());
  if (!entries.length) return options;
  return options.filter((option) => entries.every(([name, value]) => {
    const key = Object.keys(option.values).find((item) => normSpecName(item) === normSpecName(name));
    return !!key && option.values[key] === value;
  }));
}

/** Distinct values of `characteristicName` among options compatible with other selections. */
export function cascadingSpecializationValues(
  options: { values: Record<string, string> }[],
  characteristicName: string,
  selected: Record<string, string>,
): string[] {
  const others = Object.fromEntries(
    Object.entries(selected).filter(([name]) => normSpecName(name) !== normSpecName(characteristicName)),
  );
  const matched = filterSpecializationOptions(options, others);
  const seen = new Set<string>();
  const out: string[] = [];
  for (const option of matched) {
    const key = Object.keys(option.values).find((item) => normSpecName(item) === normSpecName(characteristicName));
    const value = key ? option.values[key] : "";
    if (!value || seen.has(value)) continue;
    seen.add(value);
    out.push(value);
  }
  return out.sort((a, b) => a.localeCompare(b, "ru"));
}

function charScore(entry: SpecChildValue, name: string, preferredNames: Set<string>): number {
  let score = 10;
  if (entry.required) score += 20;
  if (entry.kind === "1") score += 10;
  if (/^(тип|вид|назначение)\b/i.test(name)) score += 5;
  if (preferredNames.has(normSpecName(name))) score += 50;
  return score;
}

function combinations<T>(items: T[], size: number): T[][] {
  if (size <= 0) return [[]];
  if (size > items.length) return [];
  const out: T[][] = [];
  const walk = (start: number, path: T[]) => {
    if (path.length === size) {
      out.push(path.slice());
      return;
    }
    for (let index = start; index < items.length; index++) {
      path.push(items[index]!);
      walk(index + 1, path);
      path.pop();
    }
  };
  walk(0, []);
  return out;
}

function signatureFor(child: SpecChild, names: string[]): string | null {
  const parts: string[] = [];
  for (const name of names) {
    const value = child.values.get(name)?.value;
    if (!value) return null;
    parts.push(value);
  }
  return parts.join("\0");
}

function partitions(children: SpecChild[], names: string[]): boolean {
  const seen = new Set<string>();
  for (const child of children) {
    const key = signatureFor(child, names);
    if (!key || seen.has(key)) return false;
    seen.add(key);
  }
  return seen.size === children.length;
}

function distinctGroupCount(children: SpecChild[], names: string[]): number {
  const seen = new Set<string>();
  for (const child of children) {
    const key = signatureFor(child, names);
    if (key) seen.add(key);
  }
  return seen.size;
}

/**
 * Compact characteristic set whose value tuples distinguish sibling positions.
 * Prefers a full partition; otherwise returns a best-effort set without inventing a winner.
 */
export function pickDiscriminatingCharacteristics(
  children: SpecChild[],
  options?: { preferredNames?: string[]; maxSize?: number },
): { characteristics: SpecCharacteristicPick[]; fullyPartitions: boolean } | null {
  if (children.length < 2) return null;
  const preferredNames = new Set((options?.preferredNames ?? []).map(normSpecName).filter(Boolean));
  const maxSize = Math.max(1, Math.min(options?.maxSize ?? MAX_SET_SIZE, MAX_SET_SIZE));

  const nameMeta = new Map<string, SpecCharacteristicPick>();
  for (const child of children) {
    for (const [name, entry] of child.values) {
      if (!entry.value) continue;
      const existing = nameMeta.get(name);
      if (!existing) {
        nameMeta.set(name, {
          name,
          code: entry.code,
          score: charScore(entry, name, preferredNames),
        });
      } else {
        existing.code = existing.code ?? entry.code;
        existing.score = Math.max(existing.score, charScore(entry, name, preferredNames));
      }
    }
  }

  const candidates: SpecCharacteristicPick[] = [];
  for (const [name, meta] of nameMeta) {
    let missing = false;
    const distinct = new Set<string>();
    for (const child of children) {
      const entry = child.values.get(name);
      if (!entry?.value) {
        missing = true;
        break;
      }
      distinct.add(entry.value);
    }
    if (missing || distinct.size < 2) continue;
    candidates.push(meta);
  }
  if (!candidates.length) return null;

  candidates.sort((a, b) => b.score - a.score || a.name.localeCompare(b.name, "ru"));

  for (let size = 1; size <= Math.min(maxSize, candidates.length); size++) {
    const pool = size === 1 ? candidates : candidates.slice(0, Math.max(size, MAX_COMBINATION_CANDIDATES));
    const ranked = combinations(pool, size)
      .map((set) => ({
        set,
        score: set.reduce((sum, item) => sum + item.score, 0),
      }))
      .sort((a, b) => b.score - a.score || a.set.map((item) => item.name).join("\0").localeCompare(
        b.set.map((item) => item.name).join("\0"),
        "ru",
      ));
    for (const entry of ranked) {
      const names = entry.set.map((item) => item.name);
      if (partitions(children, names)) {
        return {
          characteristics: entry.set
            .slice()
            .sort((a, b) => b.score - a.score || a.name.localeCompare(b.name, "ru")),
          fullyPartitions: true,
        };
      }
    }
  }

  // Best-effort: greedily grow a set that maximizes distinct signatures.
  const chosen: SpecCharacteristicPick[] = [];
  let bestCount = 1;
  for (let step = 0; step < Math.min(maxSize, candidates.length); step++) {
    let improved: SpecCharacteristicPick | null = null;
    let improvedCount = bestCount;
    for (const candidate of candidates) {
      if (chosen.some((item) => item.name === candidate.name)) continue;
      const count = distinctGroupCount(children, [...chosen.map((item) => item.name), candidate.name]);
      if (count > improvedCount) {
        improved = candidate;
        improvedCount = count;
      }
    }
    if (!improved) break;
    chosen.push(improved);
    bestCount = improvedCount;
    if (bestCount === children.length) break;
  }
  if (!chosen.length || bestCount < 2) return null;
  return {
    characteristics: chosen.sort((a, b) => b.score - a.score || a.name.localeCompare(b.name, "ru")),
    fullyPartitions: bestCount === children.length,
  };
}
