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

export function mapOptionProbabilities(
  probabilities: DraftCharacteristic["optionProbabilities"], options: string[],
): DraftCharacteristic["optionProbabilities"] {
  if (!probabilities) return undefined;
  const values: Record<string, number> = {};
  for (const option of options) {
    const match = Object.keys(probabilities.values).find((value) => normalizeOptionKey(value) === normalizeOptionKey(option));
    if (match !== undefined) values[option] = probabilities.values[match];
  }
  return { text: probabilities.text, values };
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
    const number = parseCharacteristicNumber(value);
    rangeOption = number == null
      ? (ktru.options.includes(fromSpgz) ? fromSpgz : "")
      : (matchNumberToRange(number, ktru.ranges) ?? "");
  }
  return {
    ...ktru,
    id: `merge:${ktru.id}:${spgz.id}`,
    value,
    rangeOption,
    autofill: ktru.value.trim() || ktru.rangeOption.trim() ? ktru.autofill : spgz.autofill,
    optionProbabilities: mapOptionProbabilities(ktru.optionProbabilities ?? spgz.optionProbabilities, ktru.options),
    required: ktru.required || spgz.required,
    multiple: ktru.multiple || spgz.multiple,
    numeric: ktru.numeric || spgz.numeric,
    source: "both",
    hint: ktru.hint || spgz.hint,
    linkedIds: [ktru.id, spgz.id],
  };
}

/**
 * One table row when KTRU and SPGZ schemas match fully; otherwise keep separate rows.
 * Order: KTRU source order (XML list), then leftover SPGZ, then custom.
 * Discriminating characteristic is pinned first.
 */
export function buildDisplayCharacteristics(items: DraftCharacteristic[]): DisplayCharacteristic[] {
  const custom = items.filter((item) => item.source === "custom" || item.custom);
  const both = items.filter((item) => item.source === "both" && !item.custom);
  const ktru = items.filter((item) => item.source === "ktru");
  const spgz = items.filter((item) => item.source === "spgz");
  const usedSpgz = new Set<string>();
  const catalog: DisplayCharacteristic[] = [];

  // Preserve KTRU array order from the position payload.
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
  for (const item of both) {
    catalog.push({ ...item, linkedIds: [item.id], source: "both" });
  }

  const ordered = [
    ...catalog,
    ...custom.map((item) => ({ ...item, linkedIds: [item.id] })),
  ];
  return [
    ...ordered.filter((item) => item.refinesKtru),
    ...ordered.filter((item) => !item.refinesKtru),
  ];
}

export function retainTemplateCharacteristics(
  position: DraftCharacteristic[],
  template: DraftCharacteristic[],
  templateCode: string,
): DraftCharacteristic[] {
  const retainedPosition = position.filter((item) => !item.templateKtruCode || item.value.trim() || item.rangeOption.trim());
  const names = new Set(retainedPosition.map((item) => normName(item.name)));
  const ids = new Set(retainedPosition.map((item) => item.id));
  return [
    ...retainedPosition,
    ...template.filter((item) => (item.value.trim() || item.rangeOption.trim())
      && !names.has(normName(item.name)) && !ids.has(item.id))
      .map((item) => ({ ...item, templateKtruCode: templateCode })),
  ];
}

export function autofillConfidencePresentation(confidence: number): { percent: number; level: "high" | "medium" | "low"; label: string } {
  const percent = Math.round(confidence * 100);
  if (confidence > 0.8) return { percent, level: "high", label: "Высокая уверенность" };
  if (confidence >= 0.7) return { percent, level: "medium", label: "Средняя уверенность" };
  return { percent, level: "low", label: "Низкая уверенность — проверьте значение" };
}

export function characteristicSourceLabel(source: DraftCharacteristic["source"], templateKtruCode?: string): string {
  if (templateKtruCode) return source === "both" ? "из шаблона КТРУ, СПГЗ" : "из шаблона КТРУ";
  switch (source) {
    case "both": return "из КТРУ, СПГЗ";
    case "spgz": return "из СПГЗ";
    case "custom": return "доп.";
    default: return "из КТРУ";
  }
}
