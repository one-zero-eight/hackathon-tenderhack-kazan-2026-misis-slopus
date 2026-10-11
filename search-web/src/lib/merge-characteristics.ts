import { isRangeCharacteristic, matchCharacteristicOption, matchNumberToRange, parseCharacteristicNumber, shortUnitName } from "./characteristic-options";
import type { CatalogCharacteristic, CharacteristicAutofillInput, DraftCharacteristic, WebEnrichmentSuggestion } from "./types";

function normName(name: string): string {
  return name.normalize("NFKC").toLocaleLowerCase("ru").replaceAll("ё", "е")
    .replace(/[\s_,;:!?"'«»„“”()\[\]{}]+/g, " ").trim().replace(/^[ .]+|[ .]+$/g, "");
}

/** Internet facts stay separate from official schemas and never overwrite their values. */
export function appendWebCharacteristics(
  fields: DraftCharacteristic[], enrichment: WebEnrichmentSuggestion | null | undefined,
): DraftCharacteristic[] {
  if (!enrichment) return fields;
  const seen = new Set(fields.map((field) => field.webSource
    ? normName(field.webSource.fact)
    : normName(`${field.name}: ${field.value}${field.unit ? ` ${field.unit}` : ""}`)));
  const added: DraftCharacteristic[] = [];
  for (const raw of enrichment.facts) {
    const fact = raw.trim();
    if (!fact || seen.has(normName(fact))) continue;
    seen.add(normName(fact));
    const separator = fact.indexOf(":");
    if (separator <= 0) continue;
    const name = fact.slice(0, separator).trim();
    const value = fact.slice(separator + 1).trim();
    if (!name || !value) continue;
    added.push({
      id: `web:${encodeURIComponent(normName(fact))}`, name, value, unit: "",
      rangeOption: "", required: false, badges: [], options: [], optionLabels: {},
      ranges: [], multiple: false, source: "custom", custom: true, hint: "",
      webSource: { fact, sources: enrichment.sources.filter((source) => /^https?:\/\//i.test(source.url)) },
    });
  }
  return [...fields, ...added];
}

/** Observed catalog values are examples, not an exhaustive schema or product facts. */
export function appendCatalogCharacteristics(
  fields: DraftCharacteristic[], statistics: CatalogCharacteristic[], previous: DraftCharacteristic[] = [],
): DraftCharacteristic[] {
  const base = fields.filter((item) => item.source !== "catalog").map((field) => {
    if (field.custom || field.source === "custom") return field;
    const stats = statistics.find((item) => normName(item.name) === normName(field.name)
      && characteristicUnitsEquivalent(item, field));
    if (!stats && !field.catalogFrequency) return field;
    return { ...field, catalogFrequency: stats };
  });
  const names = new Set(base.filter((item) => !item.webSource).map((item) => normName(item.name)));
  const identities = new Set<string>();
  const added: DraftCharacteristic[] = [];
  for (const item of statistics) {
    const key = normName(item.name);
    const identity = `${item.nameKey ?? key}\0${item.okeiCode ?? item.unit}`;
    if (names.has(key) || identities.has(identity)) continue;
    identities.add(identity);
    const prior = previous.find((field) => field.source === "catalog"
      && characteristicUnitsEquivalent(field, item)
      && (field.id === item.id || normName(field.name) === key));
    added.push({
      id: item.id, name: item.name, unit: item.unit, okeiCode: item.okeiCode, numeric: item.numeric,
      value: prior?.value ?? "", rangeOption: "", required: false, badges: [],
      options: [], optionLabels: {}, ranges: [], multiple: false, source: "catalog",
      hint: "", catalogFrequency: item,
      autofill: prior?.autofill, autofillDiagnostics: prior?.autofillDiagnostics,
    });
  }
  return [...base, ...added];
}

export function catalogOptionPopularity(field: DraftCharacteristic, option: string): { count: number; total: number; percent: number } | undefined {
  const stats = field.catalogFrequency;
  if (!stats) return undefined;
  if (option === "—") {
    if (!stats.total) return undefined;
    const count = stats.total - stats.count;
    return { count, total: stats.total, percent: Math.round(count / stats.total * 100) };
  }
  if (!stats.count) return undefined;
  if (isRangeCharacteristic(field)) {
    if (!stats.numericValueCounts) return undefined;
    const range = field.ranges.find((entry) => entry.option === option);
    if (!range) return undefined;
    const count = Object.entries(stats.numericValueCounts).reduce((sum, [value, products]) => {
      const number = Number(value);
      return sum + (matchNumberToRange(number, [range]) === option ? products : 0);
    }, 0);
    return { count, total: stats.count, percent: Math.round(count / stats.count * 100) };
  }
  const labels = field.options.map((value) => field.optionLabels[value] || value);
  const label = field.optionLabels[option] || option;
  const matches = Object.entries(stats.exampleCounts).filter(([value]) => matchCharacteristicOption(value, labels) === label);
  // Multiple distinct catalog values can occur on the same product; don't sum overlapping votes.
  if (matches.length !== 1) return undefined;
  const count = matches[0][1];
  return { count, total: stats.count, percent: Math.round(count / stats.count * 100) };
}

export function catalogFrequencyLabel(item: CatalogCharacteristic): string {
  if (item.scope === "schema") {
    return item.examples.length
      ? `Официальная характеристика КПГЗ (${item.examples.length} допустимых значений).`
      : "Официальная характеристика КПГЗ из схемы категории.";
  }
  const scope = item.scope === "category" ? "этой категории и её подкатегорий" : "смежных категорий";
  return `Встречается у ${item.count} из ${item.total} товаров ${scope} (${Math.round(item.frequency * 100)}%).`;
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

export function characteristicUnitsEquivalent(
  a: { unit: string; okeiCode?: string }, b: { unit: string; okeiCode?: string },
): boolean {
  if (a.okeiCode || b.okeiCode) return a.okeiCode === b.okeiCode;
  return shortUnitName(a.unit) === shortUnitName(b.unit);
}

export function schemaEquivalent(a: DraftCharacteristic, b: DraftCharacteristic): boolean {
  if (!characteristicUnitsEquivalent(a, b)) return false;
  if (a.source === "catalog" && b.source === "catalog") return a.id === b.id;
  if (normName(a.name) !== normName(b.name)) return false;
  if (!!a.numeric !== !!b.numeric) return false;
  if (!!a.multiple !== !!b.multiple) return false;
  if (!optionsEquivalent(a.options ?? [], b.options ?? [])) return false;
  if (a.ranges.length !== b.ranges.length) return false;
  if (!a.ranges.every((left) => b.ranges.some((right) =>
    normalizeOptionKey(left.option) === normalizeOptionKey(right.option)
    && left.min === right.min && left.max === right.max
    && left.minInclusive === right.minInclusive && left.maxInclusive === right.maxInclusive))) return false;
  return true;
}

export function mapValueToOptions(value: string, options: string[]): string {
  const raw = value.trim();
  if (!raw || !options.length) return raw;
  const key = normalizeOptionKey(raw);
  return options.find((option) => normalizeOptionKey(option) === key) ?? raw;
}

/** Keep each directory's own option and range identifiers when sharing a value. */
export function characteristicValueForSchema(
  field: DraftCharacteristic, value: string, rangeOption = "",
): Pick<DraftCharacteristic, "value" | "rangeOption"> {
  if (isRangeCharacteristic(field)) {
    const number = Number(value.trim().replace(",", "."));
    return {
      value,
      rangeOption: value.trim() && Number.isFinite(number)
        ? matchNumberToRange(number, field.ranges) ?? ""
        : rangeOption ? mapValueToOptions(rangeOption, field.options) : "",
    };
  }
  return {
    value: field.multiple
      ? value.split(/\s*·\s*/).map((part) => mapValueToOptions(part, field.options)).join(" · ")
      : mapValueToOptions(value, field.options),
    rangeOption: "",
  };
}

function acceptsSharedValue(field: DraftCharacteristic, value: string, rangeOption: string): boolean {
  if (isRangeCharacteristic(field)) {
    if (!value.trim()) return false; // A bucket alone is not a concrete measurement.
    const number = Number(value.trim().replace(",", "."));
    return Number.isFinite(number) && matchNumberToRange(number, field.ranges) !== undefined;
  }
  if (!value.trim() || rangeOption) return false;
  if (field.numeric && !Number.isFinite(Number(value.replace(",", ".")))) return false;
  if (!field.options.length) return !field.hint; // Do not ignore unstructured directory restrictions.
  const values = field.multiple ? value.split(/\s*·\s*/) : [value];
  return values.every((part) => field.options.some((option) => normalizeOptionKey(part) === normalizeOptionKey(option)));
}

/** Separate selectors retain full choices; only a value allowed by both is copied. */
export function characteristicValueUpdates(
  items: DraftCharacteristic[], ids: Set<string>, value: string, rangeOption = "",
): Map<string, Pick<DraftCharacteristic, "value" | "rangeOption">> {
  const updates = new Map<string, Pick<DraftCharacteristic, "value" | "rangeOption">>();
  const sources = items.filter((item) => ids.has(item.id));
  for (const source of sources) updates.set(source.id, characteristicValueForSchema(source, value, rangeOption));
  for (const source of sources) {
    if (source.custom || !["ktru", "spgz"].includes(source.source)
      || !acceptsSharedValue(source, value, rangeOption)) continue;
    for (const target of items) {
      if (ids.has(target.id) || target.custom || !["ktru", "spgz"].includes(target.source)
        || target.source === source.source || normName(target.name) !== normName(source.name)
        || !characteristicUnitsEquivalent(source, target)
        || !!source.numeric !== !!target.numeric || !!source.multiple !== !!target.multiple
        || !acceptsSharedValue(target, value, rangeOption)) continue;
      updates.set(target.id, characteristicValueForSchema(target, value, rangeOption));
    }
  }
  return updates;
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
    autofillDiagnostics: ktru.autofillDiagnostics ?? spgz.autofillDiagnostics,
    required: ktru.required || spgz.required,
    multiple: ktru.multiple || spgz.multiple,
    numeric: ktru.numeric || spgz.numeric,
    source: "both",
    catalogFrequency: ktru.catalogFrequency ?? spgz.catalogFrequency,
    hint: ktru.hint || spgz.hint,
    linkedIds: [ktru.id, spgz.id],
  };
}

/**
 * One table row when KTRU and SPGZ schemas match fully; otherwise keep separate rows.
 * Required rows precede optional rows; discriminating rows lead each group.
 * Otherwise preserve source order: KTRU, leftover SPGZ, catalog, then custom.
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
    ...items.filter((item) => item.source === "catalog" && !item.custom
      && !catalog.some((field) => normName(field.name) === normName(item.name))
      && !custom.some((field) => !field.webSource && normName(field.name) === normName(item.name)))
      .map((item) => ({ ...item, linkedIds: [item.id] })),
    ...custom.map((item) => ({ ...item, linkedIds: [item.id] })),
  ];
  return [
    ...ordered.filter((item) => item.refinesKtru),
    ...ordered.filter((item) => !item.refinesKtru),
  ].sort((left, right) => Number(right.required) - Number(left.required));
}

/** Refined positions can introduce fields that were absent from the extraction schema. */
export function hasNewAutofillFields(
  previous: DraftCharacteristic[], current: DraftCharacteristic[],
): boolean {
  const signature = (field: DraftCharacteristic) => JSON.stringify([
    field.id, field.name, field.source, field.options, field.optionLabels,
    field.ranges, field.unit, field.okeiCode, !!field.numeric, !!field.multiple, field.hint,
  ]);
  const known = new Set(previous.map(signature));
  return current.some((field) => !field.custom && field.source !== "custom" && !field.webSource
    && !field.value.trim() && !field.rangeOption.trim() && !known.has(signature(field)));
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

/** Keep the extraction schema identical to the displayed official/catalog rows. */
export function autofillCharacteristicFields(fields: DraftCharacteristic[]): CharacteristicAutofillInput["characteristics"] {
  return fields.filter((item) => !item.custom && item.source !== "custom")
    .map(({ id, name, options, optionLabels, ranges, unit, okeiCode, numeric, multiple, required, hint, source, catalogFrequency }) =>
      ({ id, name, options, optionLabels, ranges, unit, okeiCode, numeric, multiple, required, hint, source,
        examples: catalogFrequency?.examples ?? [] }));
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
    case "catalog": return "из каталога";
    case "custom": return "доп.";
    default: return "из КТРУ";
  }
}
