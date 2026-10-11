import assert from "node:assert/strict";
import { test } from "node:test";
import { buildDisplayCharacteristics, hasNewAutofillFields } from "../src/lib/merge-characteristics";
import type { DraftCharacteristic } from "../src/lib/types";

function field(id: string, name: string, partial: Partial<DraftCharacteristic> = {}): DraftCharacteristic {
  return {
    id, name, value: "", rangeOption: "", required: false, badges: [], hint: "",
    options: [], optionLabels: {}, ranges: [], unit: "", multiple: false,
    source: "ktru", ...partial,
  };
}

test("refining hammer template requests extraction for SPGZ handle material", () => {
  const template = [field("type", "Тип", { options: ["Слесарный"] })];
  const refined = [
    field("type", "Тип", { value: "Слесарный", options: ["Слесарный"] }),
    field("spgz:handle", "Материал рукоятки", { source: "spgz", options: ["Дерево", "Пластик"] }),
  ];
  assert.equal(hasNewAutofillFields(buildDisplayCharacteristics(template), buildDisplayCharacteristics(refined)), true);
  assert.equal(hasNewAutofillFields(buildDisplayCharacteristics(refined), buildDisplayCharacteristics(refined)), false);
});

test("missing existing fields and value changes do not cause repeated extraction", () => {
  const previous = [field("type", "Тип"), field("length", "Длина рукоятки")];
  const current = [field("type", "Тип", { value: "Слесарный" }), field("length", "Длина рукоятки")];
  assert.equal(hasNewAutofillFields(previous, current), false);
});

test("changed schema options require a new extraction, unrelated values do not", () => {
  const previous = [field("handle", "Материал рукоятки", { source: "spgz", options: ["Резина"] })];
  const current = [field("handle", "Материал рукоятки", { source: "spgz", options: ["Резина", "Дерево"] })];
  assert.equal(hasNewAutofillFields(previous, current), true);
});

test("new filled, custom and web fields do not cause another extraction", () => {
  assert.equal(hasNewAutofillFields([], [
    field("handle", "Материал рукоятки", { value: "Дерево", source: "spgz" }),
    field("custom", "Особенность", { source: "custom", custom: true }),
    field("web", "Свойство", { webSource: { fact: "Свойство из интернета", sources: [] } }),
  ]), false);
});
