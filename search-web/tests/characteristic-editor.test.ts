import assert from "node:assert/strict";
import test from "node:test";
import {
  characteristicSchemaProblem, characteristicValueProblem, draftFromCharacteristicSchema, inferCatalogSchema,
  isVisibleCharacteristic, popularCatalogCharacteristics, schemaFromCharacteristic,
} from "../src/lib/characteristic-editor";
import { appendCatalogCharacteristics, buildDisplayCharacteristics } from "../src/lib/merge-characteristics";
import { captureQueryDraft, catalogCharacteristicSuggestions, restoreQueryDraft, setCatalogCharacteristicSuggestions } from "../src/lib/create-product-state";
import type { CatalogCharacteristic } from "../src/lib/types";
import { shortUnitName, matchNumberToRange, characteristicRangeProblem } from "../src/lib/characteristic-options";

const mass: CatalogCharacteristic = {
  id: "catalog:mass:kg", name: "Масса", nameKey: "масса", unit: "кг", numeric: true,
  count: 80, total: 100, frequency: 0.8, scope: "category",
  examples: ["1", "2"], exampleCounts: { "1": 50, "2": 30 }, numericValueCounts: { "1": 50, "2": 30 },
};
const color: CatalogCharacteristic = {
  ...mass, id: "catalog:color", name: "Цвет", nameKey: "цвет", unit: "", numeric: false,
  count: 60, frequency: 0.6, examples: ["Белый", "Синий"], exampleCounts: { "Белый": 40, "Синий": 20 }, numericValueCounts: null,
};

test("empty catalog rows stay available to autofill but are hidden from the table", () => {
  const fields = appendCatalogCharacteristics([], [mass, color]);
  assert.equal(buildDisplayCharacteristics(fields).length, 2);
  assert.equal(buildDisplayCharacteristics(fields).filter(isVisibleCharacteristic).length, 0);
  assert.equal(isVisibleCharacteristic({ ...fields[0], value: "  " }), false);
  assert.equal(isVisibleCharacteristic({ ...fields[0], value: "0" }), true);
  assert.equal(isVisibleCharacteristic({ ...fields[0], rangeOption: ">=1" }), true);
  assert.equal(isVisibleCharacteristic({ ...fields[0], source: "ktru" }), true);
  assert.equal(isVisibleCharacteristic({ ...fields[0], source: "spgz" }), true);
  assert.equal(isVisibleCharacteristic({ ...fields[0], source: "custom", custom: true }), true);
});

test("popular suggestions infer the most frequent unit without imposing observed values", () => {
  const grams = { ...mass, id: "catalog:mass:g", unit: "г", count: 20, frequency: 0.2 };
  assert.deepEqual(popularCatalogCharacteristics([grams, color, mass]), [mass, color]);
  assert.equal(inferCatalogSchema(mass).kind, "number");
  assert.equal(inferCatalogSchema(mass).unit, "кг");
  assert.deepEqual(inferCatalogSchema(color).options, []);
  assert.equal(inferCatalogSchema(color).kind, "text");
  assert.equal(inferCatalogSchema({ ...color, examples: ["Да", "Нет"] }).kind, "boolean");
});

test("new schemas keep every manual override and do not copy catalog examples into facts", () => {
  const inferred = inferCatalogSchema(mass);
  const empty = draftFromCharacteristicSchema(inferred, "custom:mass", undefined, mass);
  assert.equal(empty.value, "");
  assert.equal(empty.source, "custom");
  assert.equal(empty.custom, true);
  assert.equal(empty.numeric, true);
  assert.equal(empty.catalogFrequency, mass);
  assert.equal(isVisibleCharacteristic(empty), true);
  const changed = draftFromCharacteristicSchema({ ...inferred, kind: "choice", unit: "г", options: ["Лёгкий", "Тяжёлый"], multiple: true, required: true, hint: "ГОСТ" }, "custom:mass", { value: "Лёгкий" }, mass);
  assert.equal(changed.numeric, false);
  assert.equal(changed.unit, "г");
  assert.equal(changed.catalogFrequency, undefined);
  assert.equal(changed.multiple, true);
  assert.equal(changed.required, true);
  assert.equal(changed.hint, "ГОСТ");
  assert.deepEqual(changed.options, ["Лёгкий", "Тяжёлый"]);
  assert.equal(schemaFromCharacteristic(changed).kind, "choice");
});

test("editable numeric ranges match KTRU semantics and validate their boundaries", () => {
  const schema = { ...inferCatalogSchema(mass), kind: "range" as const, ranges: [
    { option: "", min: 0, max: 10, minInclusive: true, maxInclusive: false },
    { option: "", min: 10, max: null, minInclusive: true, maxInclusive: false },
  ] };
  assert.equal(characteristicSchemaProblem(schema), null);
  assert.equal(characteristicValueProblem(schema, "10"), null);
  assert.match(characteristicValueProblem(schema, "-1")!, /диапазон/);
  assert.match(characteristicValueProblem(schema, "не число")!, /числовое/);
  assert.equal(characteristicValueProblem(schema, ""), null);
  assert.match(characteristicValueProblem({ ...schema, kind: "boolean" }, "Возможно")!, /списка/);
  const field = draftFromCharacteristicSchema(schema, "custom:mass", { value: "10" });
  assert.deepEqual(field.options, ["≥ 0 · < 10", "≥ 10"]);
  assert.equal(field.rangeOption, "≥ 10");
  assert.equal(field.numeric, true);
  assert.equal(schemaFromCharacteristic(field).kind, "range");
  assert.match(characteristicSchemaProblem({ ...schema, ranges: [{ ...schema.ranges[0], min: 12 }] })!, /Минимум/);
  assert.match(characteristicSchemaProblem({ ...schema, ranges: [{ ...schema.ranges[0], min: null, max: null }] })!, /границу/);
  assert.match(characteristicSchemaProblem({ ...schema, ranges: [{ ...schema.ranges[0], min: NaN }] })!, /числами/);
  assert.match(characteristicSchemaProblem({ ...schema, kind: "choice", options: [" "] })!, /значение/);
});

test("screen refresh frequency uses short hertz units and matches the numeric range", () => {
  assert.equal(shortUnitName("Герц"), "Гц");
  assert.equal(shortUnitName("Герц; Гц"), "Гц");
  assert.equal(shortUnitName("Килогерц"), "кГц");
  assert.equal(shortUnitName("Мегагерц"), "МГц");
  assert.equal(shortUnitName("Гигагерц"), "ГГц");
  const ranges = [{ option: "60–120", min: 60, max: 120, minInclusive: true, maxInclusive: false }];
  assert.equal(matchNumberToRange(75, ranges), "60–120");
  assert.equal(characteristicRangeProblem({ value: "75", unit: "Герц", ranges, optionLabels: {} }), null);
});

test("catalog suggestions survive independent query drafts", () => {
  const original = captureQueryDraft();
  try {
    setCatalogCharacteristicSuggestions([mass]);
    const first = captureQueryDraft();
    setCatalogCharacteristicSuggestions([color]);
    const second = captureQueryDraft();
    restoreQueryDraft(first);
    assert.deepEqual(catalogCharacteristicSuggestions(), [mass]);
    restoreQueryDraft(second);
    assert.deepEqual(catalogCharacteristicSuggestions(), [color]);
  } finally { restoreQueryDraft(original); }
});
