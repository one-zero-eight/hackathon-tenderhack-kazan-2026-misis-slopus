import { matchNumberToRange, shortUnitName } from "./characteristic-options";
import type { CatalogCharacteristic, CharacteristicRange, DraftCharacteristic } from "./types";

export type CharacteristicKind = "text" | "number" | "choice" | "boolean" | "range";
export type CharacteristicSchema = {
  name: string;
  kind: CharacteristicKind;
  unit: string;
  options: string[];
  ranges: CharacteristicRange[];
  multiple: boolean;
  required: boolean;
  hint: string;
};

export function characteristicNameKey(name: string): string {
  return name.normalize("NFKC").toLocaleLowerCase("ru").replaceAll("ё", "е")
    .replace(/[\s_,;:!?"'«»„“”()\[\]{}]+/g, " ").trim().replace(/^[ .]+|[ .]+$/g, "");
}

export function isVisibleCharacteristic(field: DraftCharacteristic): boolean {
  return field.source !== "catalog" || !!field.custom || !!field.value.trim() || !!field.rangeOption.trim();
}

/** Prefer the most frequently observed schema, not a unit guessed from the name. */
export function popularCatalogCharacteristics(statistics: CatalogCharacteristic[]): CatalogCharacteristic[] {
  const ranked = [...statistics].sort((a, b) => b.frequency - a.frequency || b.count - a.count || a.name.localeCompare(b.name, "ru"));
  const seen = new Set<string>();
  return ranked.filter((item) => {
    const key = item.nameKey ?? characteristicNameKey(item.name);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export function inferCatalogSchema(item: CatalogCharacteristic): CharacteristicSchema {
  const boolean = item.examples.length > 0 && item.examples.every((value) => /^(да|нет)$/iu.test(value.trim()));
  return {
    name: item.name,
    kind: item.numeric ? "number" : boolean ? "boolean" : "text",
    unit: item.unit,
    options: boolean ? ["Да", "Нет"] : [],
    ranges: [], multiple: false, required: false, hint: "",
  };
}

export function schemaFromCharacteristic(item: DraftCharacteristic): CharacteristicSchema {
  return {
    name: item.name,
    kind: item.ranges.length ? "range" : item.options.length === 2 && item.options.every((option) => /^(да|нет)$/iu.test(option))
      ? "boolean" : item.options.length ? "choice" : item.numeric ? "number" : "text",
    unit: item.unit, options: [...item.options], ranges: item.ranges.map((range) => ({ ...range })),
    multiple: item.multiple, required: item.required, hint: item.hint,
  };
}

export function characteristicSchemaProblem(schema: CharacteristicSchema): string | null {
  if (!schema.name.trim()) return "Укажите название характеристики.";
  if (schema.kind === "choice" && !schema.options.some((option) => option.trim())) return "Добавьте хотя бы одно допустимое значение.";
  if (schema.kind === "range") {
    if (!schema.ranges.length) return "Добавьте хотя бы один диапазон.";
    for (const range of schema.ranges) {
      if (range.min == null && range.max == null) return "Укажите хотя бы одну границу диапазона.";
      if ((range.min != null && !Number.isFinite(range.min)) || (range.max != null && !Number.isFinite(range.max))) return "Границы диапазона должны быть числами.";
      if (range.min != null && range.max != null && (range.min > range.max || (range.min === range.max && (!range.minInclusive || !range.maxInclusive)))) return "Минимум диапазона должен быть меньше максимума.";
    }
  }
  return null;
}

export function characteristicValueProblem(schema: CharacteristicSchema, value: string): string | null {
  const raw = value.trim();
  if (!raw) return null;
  if (schema.kind === "number" || schema.kind === "range") {
    const number = Number(raw.replace(",", "."));
    if (!Number.isFinite(number)) return "Введите числовое значение характеристики.";
    if (schema.kind === "range" && matchNumberToRange(number, schema.ranges) === undefined) return "Значение не входит ни в один допустимый диапазон.";
  }
  const options = schema.kind === "boolean" ? ["Да", "Нет"] : schema.kind === "choice" ? schema.options : [];
  const values = schema.multiple ? raw.split(/\s*·\s*/u) : [raw];
  if (options.length && values.some((item) => !options.some((option) => option.trim().toLocaleLowerCase("ru") === item.toLocaleLowerCase("ru")))) {
    return "Выберите значение из допустимого списка или измените схему.";
  }
  return null;
}

export function draftFromCharacteristicSchema(
  schema: CharacteristicSchema, id: string, prior?: Pick<DraftCharacteristic, "value">, statistics?: CatalogCharacteristic,
): DraftCharacteristic {
  const numeric = schema.kind === "number" || schema.kind === "range";
  const ranges = schema.kind === "range" ? schema.ranges.map((range) => ({
    ...range,
    option: [
      range.min == null ? "" : `${range.minInclusive ? "≥" : ">"} ${range.min}`,
      range.max == null ? "" : `${range.maxInclusive ? "≤" : "<"} ${range.max}`,
    ].filter(Boolean).join(" · "),
  })) : [];
  const options = schema.kind === "boolean" ? ["Да", "Нет"]
    : schema.kind === "choice" ? [...new Set(schema.options.map((value) => value.trim()).filter(Boolean))]
      : ranges.map((range) => range.option);
  const matchingStatistics = statistics && shortUnitName(statistics.unit) === shortUnitName(schema.unit)
    && statistics.numeric === numeric ? statistics : undefined;
  return {
    id, name: schema.name.trim(), value: prior?.value ?? "",
    rangeOption: ranges.length && prior?.value.trim() && Number.isFinite(Number(prior.value.replace(",", ".")))
      ? matchNumberToRange(Number(prior.value.replace(",", ".")), ranges) ?? "" : "",
    unit: schema.unit.trim(), numeric, required: schema.required,
    options, optionLabels: {}, ranges,
    multiple: (schema.kind === "text" || schema.kind === "choice") && schema.multiple,
    hint: schema.hint.trim(), badges: [], source: "custom", custom: true,
    catalogFrequency: matchingStatistics,
  };
}
