import assert from "node:assert/strict";
import { test } from "node:test";
import {
  cascadingSpecializationValues,
  filterSpecializationOptions,
  pickDiscriminatingCharacteristics,
  specializationOptionLabel,
  type SpecChild,
} from "../src/lib/specialization";

function child(code: string, values: Record<string, string>): SpecChild {
  return {
    code,
    name: code,
    values: new Map(Object.entries(values).map(([name, value]) => [name, { value, code: null, required: true, kind: "1" }])),
  };
}

test("single characteristic still partitions when values are unique", () => {
  const picked = pickDiscriminatingCharacteristics([
    child("1", { Материал: "Сталь", Цвет: "Серый" }),
    child("2", { Материал: "Медь", Цвет: "Серый" }),
  ])!;
  assert.deepEqual(picked.characteristics.map((item) => item.name), ["Материал"]);
  assert.equal(picked.fullyPartitions, true);
});

test("combination is chosen when no single characteristic partitions", () => {
  const picked = pickDiscriminatingCharacteristics([
    child("1", { Материал: "Сталь", Размер: "10", Цвет: "Серый" }),
    child("2", { Материал: "Сталь", Размер: "20", Цвет: "Серый" }),
    child("3", { Материал: "Медь", Размер: "10", Цвет: "Серый" }),
    child("4", { Материал: "Медь", Размер: "20", Цвет: "Серый" }),
  ])!;
  assert.deepEqual(picked.characteristics.map((item) => item.name).sort(), ["Материал", "Размер"]);
  assert.equal(picked.fullyPartitions, true);
});

test("constant characteristics are ignored", () => {
  const picked = pickDiscriminatingCharacteristics([
    child("1", { Материал: "Сталь", Тип: "А" }),
    child("2", { Материал: "Сталь", Тип: "Б" }),
  ])!;
  assert.deepEqual(picked.characteristics.map((item) => item.name), ["Тип"]);
});

test("cascading values respect other selected fields", () => {
  const options = [
    { values: { Материал: "Сталь", Размер: "10" } },
    { values: { Материал: "Сталь", Размер: "20" } },
    { values: { Материал: "Медь", Размер: "10" } },
  ];
  assert.deepEqual(
    cascadingSpecializationValues(options, "Размер", { Материал: "Сталь" }),
    ["10", "20"],
  );
  assert.deepEqual(
    filterSpecializationOptions(options, { Материал: "Медь", Размер: "10" }).map((item) => item.values),
    [{ Материал: "Медь", Размер: "10" }],
  );
  assert.equal(
    specializationOptionLabel(
      [{ name: "Материал", code: null }, { name: "Размер", code: null }],
      { Материал: "Сталь", Размер: "10" },
    ),
    "Материал: Сталь · Размер: 10",
  );
});

test("identical profiles return a best-effort set without claiming a full partition", () => {
  const picked = pickDiscriminatingCharacteristics([
    child("1", { Материал: "Сталь", Размер: "10" }),
    child("2", { Материал: "Сталь", Размер: "10" }),
    child("3", { Материал: "Медь", Размер: "10" }),
  ]);
  assert.ok(picked);
  assert.equal(picked!.fullyPartitions, false);
  assert.ok(picked!.characteristics.some((item) => item.name === "Материал"));
});
