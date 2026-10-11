import assert from "node:assert/strict";
import test from "node:test";
import { characteristicRangeProblem, characteristicScaleWarning } from "../src/lib/characteristic-options";
import { buildDisplayCharacteristics } from "../src/lib/merge-characteristics";
import { draftFromSpgzCharacteristic } from "../src/lib/spgz-characteristics";
import type { DraftCharacteristic } from "../src/lib/types";

test("display characteristics put required rows first while preserving order within each group", () => {
  const makeField = (id: string, source: DraftCharacteristic["source"], required: boolean): DraftCharacteristic => ({
    ...draftFromSpgzCharacteristic({ name: id, options: [] }), id, source, required,
    custom: source === "custom",
  });
  const fields = [
    makeField("optional-ktru", "ktru", false),
    makeField("required-ktru", "ktru", true),
    makeField("merged", "ktru", false),
    makeField("optional-spgz", "spgz", false),
    makeField("required-spgz", "spgz", true),
    { ...makeField("merged-spgz", "spgz", true), name: "merged" },
    makeField("optional-catalog", "catalog", false),
    makeField("optional-custom", "custom", false),
    makeField("required-custom", "custom", true),
    { ...makeField("optional-refine", "ktru", false), refinesKtru: true },
    { ...makeField("required-refine", "ktru", true), refinesKtru: true },
  ];
  const snapshot = structuredClone(fields);
  const rows = buildDisplayCharacteristics(fields);
  assert.deepEqual(rows.map((field) => field.id), [
    "required-refine", "required-ktru", "merge:merged:merged-spgz", "required-spgz", "required-custom",
    "optional-refine", "optional-ktru", "optional-spgz", "optional-catalog", "optional-custom",
  ]);
  assert.deepEqual(rows.map((field) => field.required), [true, true, true, true, true, false, false, false, false, false]);
  assert.deepEqual(rows[2].linkedIds, ["merged", "merged-spgz"]);
  assert.deepEqual(fields, snapshot, "display sorting must not mutate stored characteristics");
  assert.deepEqual(buildDisplayCharacteristics([]), []);
});

const range = (option: string, min: number | null, max: number | null, maxInclusive = true) =>
  ({ option, min, max, minInclusive: true, maxInclusive });

test("a value far above the largest schema boundary gets a scale warning but stays editable", () => {
  const field = { value: "10000", unit: "кг", optionLabels: {}, ranges: [range("0.1–2", 0.1, 2)] };
  assert.match(characteristicScaleWarning(field)!, /Подозрительное значение.*10000 кг.*5000 раз.*границы схемы 2 кг/);
  assert.match(characteristicRangeProblem(field)!, /10000 кг/, "range error and scale warning are independent");
  assert.equal(field.value, "10000");
});

test("suspicious scale checks the highest boundary even when the final range is open", () => {
  const field = {
    value: "100", unit: "кг", optionLabels: {},
    ranges: [range("low", null, 1, false), range("high", 2, null)],
  };
  assert.match(characteristicScaleWarning(field)!, /Подозрительное значение.*50 раз.*2 кг/);
  assert.match(characteristicScaleWarning({ ...field, value: "100,1" })!, /Подозрительное значение/);
  assert.equal(characteristicScaleWarning({ ...field, value: "99.9" }), null);
  assert.equal(characteristicScaleWarning({ ...field, ranges: [] }), null);
  assert.equal(characteristicScaleWarning({ ...field, ranges: [range("high", 0, null)] }), null);
  assert.equal(characteristicScaleWarning({ ...field, ranges: [range("high", null, null)] }), null);
  assert.equal(characteristicScaleWarning({ ...field, value: "" }), null);
  assert.equal(characteristicScaleWarning({ ...field, value: "abc" }), null);
  assert.equal(characteristicScaleWarning({ ...field, value: "Infinity" }), null);
  assert.equal(characteristicRangeProblem(field), null, "a scale warning is not a range validation error");
  assert.equal(field.value, "100");
});
