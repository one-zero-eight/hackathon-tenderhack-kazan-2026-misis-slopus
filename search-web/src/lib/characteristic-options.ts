import type { CharacteristicRange } from "./types";

/** Display aliases only: never infer an OKEI code or conversion from these labels. */
const UNIT_DISPLAY_ALIASES: Record<string, string> = {
  миллиметр: "мм",
  сантиметр: "см",
  метр: "м",
  дециметр: "дм",
  километр: "км",
  "дюйм (25,4 мм)": "дюйм",
  килограмм: "кг",
  грамм: "г",
  миллиграмм: "мг",
  центнер: "ц",
  "квадратный метр": "м²",
  гектар: "га",
  "кубический метр": "м³",
  ватт: "Вт",
  киловатт: "кВт",
  мегаватт: "МВт",
  вольт: "В",
  ампер: "А",
  секунда: "с",
  минута: "мин",
  час: "ч",
  сутки: "сут",
  месяц: "мес",
  год: "г",
  герц: "Гц",
  килогерц: "кГц",
  мегагерц: "МГц",
  гигагерц: "ГГц",
  тонна: "т",
  литр: "л",
  миллилитр: "мл",
  штука: "шт.",
  пара: "пар",
  комплект: "компл.",
  упаковка: "упак.",
};

/** Exactly «Да» / «Нет» catalog pair → toggle UI. */
export function isYesNoOptions(options: string[]): boolean {
  if (options.length !== 2) return false;
  const normalized = new Set(options.map((option) => option.trim().toLocaleLowerCase("ru")));
  return normalized.has("да") && normalized.has("нет");
}

export function yesNoOptionPair(options: string[]): { yes: string; no: string } {
  const yes = options.find((option) => option.trim().toLocaleLowerCase("ru") === "да") ?? "Да";
  const no = options.find((option) => option.trim().toLocaleLowerCase("ru") === "нет") ?? "Нет";
  return { yes, no };
}

export function isRangeCharacteristic(item: { ranges?: CharacteristicRange[] }): boolean {
  return (item.ranges?.length ?? 0) > 0;
}

export function parseCharacteristicNumber(value: string): number | null {
  const match = value.trim().replace(",", ".").match(/-?\d+(?:\.\d+)?/);
  if (!match) return null;
  const number = Number(match[0]);
  return Number.isFinite(number) ? number : null;
}

/** Strip unit suffix from neighbor/STE values («350 мм» → «350»). */
export function extractCharacteristicNumber(value: string): string {
  const number = parseCharacteristicNumber(value);
  return number == null ? "" : String(number);
}

export function matchNumberToRange(value: number, ranges: CharacteristicRange[]): string | undefined {
  for (const range of ranges) {
    const minOk = range.min == null || (range.minInclusive ? value >= range.min : value > range.min);
    const maxOk = range.max == null || (range.maxInclusive ? value <= range.max : value < range.max);
    if (minOk && maxOk) return range.option;
  }
  return undefined;
}

export function characteristicScaleWarning(item: { value: string; unit: string; ranges: CharacteristicRange[] }): string | null {
  const raw = item.value.trim();
  if (!raw) return null;
  const value = Number(raw.replace(",", "."));
  if (!Number.isFinite(value)) return null;
  const boundaries = item.ranges.flatMap((range) => [range.min, range.max])
    .filter((bound): bound is number => bound != null && Number.isFinite(bound));
  const upper = Math.max(...boundaries);
  if (upper <= 0 || value / upper < 50) return null;
  const shown = [raw, shortUnitName(item.unit)].filter(Boolean).join(" ");
  const limit = [upper, shortUnitName(item.unit)].filter((part) => part !== "").join(" ");
  return `Подозрительное значение ${shown}: в ${Number((value / upper).toPrecision(6))} раз больше наибольшей границы схемы ${limit}. Проверьте число и единицу измерения.`;
}

/** Validate the number independently of the selected catalog bucket, including manual edits. */
export function characteristicRangeProblem(item: {
  value: string;
  unit: string;
  ranges: CharacteristicRange[];
  optionLabels: Record<string, string>;
}): string | null {
  const raw = item.value.trim();
  if (!raw || !item.ranges.length) return null;
  const value = Number(raw.replace(",", "."));
  if (!Number.isFinite(value)) return "Введите числовое значение характеристики.";
  if (matchNumberToRange(value, item.ranges) !== undefined) return null;
  const shown = [raw, shortUnitName(item.unit)].filter(Boolean).join(" ");
  const allowed = item.ranges.map((range) => item.optionLabels[range.option] || range.option).join("; ");
  return `Значение ${shown} не входит ни в один допустимый диапазон характеристики. Допустимые диапазоны: ${allowed}. Проверьте число и единицу измерения.`;
}

/** Short OKEI name for option labels («Миллиметр» → «мм»). */
export function shortUnitName(name: string | null | undefined): string {
  const full = String(name ?? "").split(";")[0]?.trim() ?? "";
  if (!full) return "";
  return UNIT_DISPLAY_ALIASES[full.toLocaleLowerCase("ru")] ?? full;
}

/**
 * Opaque numeric catalog codes → readable select labels.
 * Only data from KTRU itself — no guessed GOST diagram meanings.
 */
export function labelCharacteristicOption(_charName: string, value: string, _standards: string[] = []): string {
  const text = value.trim();
  if (!text) return text;
  // Already human-readable (ranges, units, words).
  if (/[≥≤<>…]/.test(text) || /[а-яa-z]/i.test(text)) return text;
  if (!/^\d+([.,]\d+)?$/.test(text)) return text;
  return text;
}

export function buildCharacteristicOptionLabels(
  charName: string,
  options: string[],
  standards: string[] = [],
): Record<string, string> {
  return Object.fromEntries(options.map((option) => [option, labelCharacteristicOption(charName, option, standards)]));
}

/** Extra hint under the field: GOST, unit — not the full options dump. */
export function buildCharacteristicHint(input: {
  name: string;
  options: string[];
  unit?: string;
  standards?: string[];
}): string {
  const bits: string[] = [];
  const options = input.options ?? [];
  const opaque = options.length >= 2 && options.every((option) => /^\d+([.,]\d+)?$/.test(option.trim()));
  const standards = (input.standards ?? []).filter(Boolean);
  if (opaque && standards.length) bits.push(standards.join(", "));
  else if (opaque) bits.push("В КТРУ только числовые коды, без текстовой расшифровки");
  const unit = shortUnitName(input.unit);
  if (unit && !options.some((option) => option.includes(unit) || option.toLocaleLowerCase("ru").includes(String(input.unit).toLocaleLowerCase("ru")))) {
    bits.push(`Ед. изм.: ${unit}`);
  }
  return bits.join(". ");
}

/** Fold Russian adjective endings so «квадратная» matches «Квадратный». */
export function normalizeCharacteristicOption(value: string): string {
  return value
    .trim()
    .toLocaleLowerCase("ru")
    .replaceAll("ё", "е")
    .replace(/(ый|ий|ой|ая|яя|ое|ее|ые|ие|ых|их|ую|юю)$/u, "");
}

export function matchCharacteristicOption(value: string, options: string[]): string | undefined {
  const needle = value.trim();
  if (!needle || !options.length) return undefined;
  const exact = options.find((option) => option === needle);
  if (exact) return exact;
  const lower = needle.toLocaleLowerCase("ru");
  const byCase = options.find((option) => option.toLocaleLowerCase("ru") === lower);
  if (byCase) return byCase;
  const stem = normalizeCharacteristicOption(needle);
  if (!stem) return undefined;
  return options.find((option) => normalizeCharacteristicOption(option) === stem);
}

export function splitCharacteristicValues(value: string): string[] {
  return value.split(/\s*[·,]\s*/u).map((item) => item.trim()).filter(Boolean);
}

/** Map free-text / neighbor value onto catalog option labels. */
export function canonicalizeCharacteristicValue(value: string, options: string[], multiple = false): string {
  if (!value.trim() || !options.length) return value;
  if (!multiple) return matchCharacteristicOption(value, options) ?? value;
  const seen = new Set<string>();
  const out: string[] = [];
  for (const entry of splitCharacteristicValues(value)) {
    const matched = matchCharacteristicOption(entry, options) ?? entry;
    if (seen.has(matched)) continue;
    seen.add(matched);
    out.push(matched);
  }
  return out.join(" · ");
}

/** Prefer first spelling; drop case/gender duplicates. */
export function dedupeCharacteristicOptions(options: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const option of options) {
    const key = normalizeCharacteristicOption(option) || option.toLocaleLowerCase("ru");
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(option);
  }
  return out;
}

/** Longest catalog option mentioned in free text (e.g. «слесарный» → «Слесарный»). */
export function pickOptionFromText(text: string, options: string[]): string | undefined {
  const lower = text.toLocaleLowerCase("ru");
  if (!lower.trim() || !options.length) return undefined;
  let best: string | undefined;
  for (const option of options) {
    const needle = option.toLocaleLowerCase("ru");
    if (needle.length < 2 || !lower.includes(needle)) continue;
    if (!best || option.length > best.length) best = option;
  }
  return best;
}

/**
 * Resolve a draft value against catalog options.
 * Drops free-text that is not an option (e.g. «молоток» for «Тип»).
 */
export function resolveCharacteristicValue(
  raw: string,
  options: string[],
  multiple = false,
  contextText = "",
): string {
  if (!options.length) return raw.trim();
  const fromRaw = canonicalizeCharacteristicValue(raw, options, multiple);
  if (!multiple) {
    const matched = matchCharacteristicOption(fromRaw, options);
    if (matched) return matched;
    return pickOptionFromText(contextText, options) ?? (options.length === 1 ? options[0]! : "");
  }
  const parts = splitCharacteristicValues(fromRaw)
    .map((entry) => matchCharacteristicOption(entry, options))
    .filter((entry): entry is string => !!entry);
  if (parts.length) return [...new Set(parts)].join(" · ");
  const picked = pickOptionFromText(contextText, options);
  if (picked) return picked;
  return options.length === 1 ? options[0]! : "";
}
