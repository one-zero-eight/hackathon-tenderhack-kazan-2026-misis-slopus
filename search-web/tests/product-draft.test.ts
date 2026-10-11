import assert from "node:assert/strict";
import test from "node:test";
import { categories, extractCharacteristics, guessCategory, suggestKtruCategory } from "../src/lib/product-draft";
import {
  captureQueryDraft,
  restoreQueryDraft,
  clearCategoryDecisions,
  currentRefineSeq,
  ktruLocked, setKtruLocked,
  spgzLocked, setSpgzLocked,
  okpdKtruDecision, setOkpdKtruDecision,
  kpgzSpgzDecision, setKpgzSpgzDecision,
  characteristics, setCharacteristics,
  characteristicEditKey,
  manuallyEditedCharacteristics,
  manuallyEditedCharacteristicValues,
  preserveManualCharacteristics,
  name, setName,
  description, setDescription,
  manuallyEditedProductFields,
  autofilledProductValues,
  autofilledProductEvidence,
} from "../src/lib/create-product-state";
import type { CatalogCharacteristic, CategoryPairDecision, DraftCharacteristic } from "../src/lib/types";
import { characteristicRangeProblem } from "../src/lib/characteristic-options";
import { draftFromSpgzCharacteristic } from "../src/lib/spgz-characteristics";
import { appendCatalogCharacteristics, autofillConfidencePresentation, buildDisplayCharacteristics, catalogFrequencyLabel, catalogOptionPopularity, characteristicSourceLabel, retainTemplateCharacteristics, schemaEquivalent } from "../src/lib/merge-characteristics";


test("query drafts preserve independent pair decisions, manual locks and edited values", () => {
  const original = captureQueryDraft();
  const decision: CategoryPairDecision = {
    needsChoice: true, reason: "branch_conflict",
    alternatives: [{
      okpd2: { code: "25.73", name: "Инструменты", score: 1, source: "classifier" },
      ktru: { code: "25.73-01", name: "Молоток", score: 1, source: "classifier" },
      kpgz: null, spgz: null, sources: ["classifier"], reason: "branch_conflict",
    }],
  };
  const second: CategoryPairDecision = {
    needsChoice: true, reason: "multiple_candidates",
    alternatives: [{ okpd2: null, ktru: null, kpgz: { code: "01", name: "Инструменты", score: 1 }, spgz: null, sources: ["neighbors"], reason: null }],
  };
  const field = { ...draftFromSpgzCharacteristic({ name: "Материал", options: ["Сталь", "Медь"] }), value: "Медь" };
  try {
    setKtruLocked(true); setSpgzLocked(false);
    setOkpdKtruDecision(decision); setKpgzSpgzDecision(second);
    setCharacteristics([field]);
    manuallyEditedCharacteristics.add(characteristicEditKey(field));
    manuallyEditedCharacteristicValues.set(characteristicEditKey(field), field);
    const saved = captureQueryDraft();
    setOkpdKtruDecision(null);
    assert.deepEqual(kpgzSpgzDecision(), second, "choosing the KTRU pair must not clear the SPGZ decision");
    setKtruLocked(false); setSpgzLocked(true); clearCategoryDecisions(); setCharacteristics([]);
    manuallyEditedCharacteristics.clear(); manuallyEditedCharacteristicValues.clear();
    const priorSeq = currentRefineSeq();
    restoreQueryDraft(saved);
    assert.equal(ktruLocked(), true); assert.equal(spgzLocked(), false);
    assert.deepEqual(okpdKtruDecision(), decision); assert.deepEqual(kpgzSpgzDecision(), second);
    assert.equal(characteristics()[0].value, "Медь");
    assert.equal(manuallyEditedCharacteristicValues.get(characteristicEditKey(field))?.value, "Медь");
    assert.ok(currentRefineSeq() > priorSeq, "restoring a query invalidates pending refinement");
    decision.alternatives[0].ktru!.name = "Changed";
    assert.equal(saved.okpdKtruDecision?.alternatives[0].ktru?.name, "Молоток", "saved drafts must not share pair objects");
  } finally { restoreQueryDraft(original); }
});

test("query drafts preserve generated product names and manual name edits", () => {
  const original = captureQueryDraft();
  try {
    setName("Дрель Ставр ДА-10");
    autofilledProductValues.set("name", name());
    manuallyEditedProductFields.delete("name");
    const generated = captureQueryDraft();
    setName("Моё наименование");
    manuallyEditedProductFields.add("name");
    const manual = captureQueryDraft();

    restoreQueryDraft(generated);
    assert.equal(name(), "Дрель Ставр ДА-10");
    assert.equal(autofilledProductValues.get("name"), name());
    assert.equal(manuallyEditedProductFields.has("name"), false);

    restoreQueryDraft(manual);
    assert.equal(name(), "Моё наименование");
    assert.equal(manuallyEditedProductFields.has("name"), true);
    assert.equal(autofilledProductValues.get("name"), "Дрель Ставр ДА-10");
  } finally { restoreQueryDraft(original); }
});

test("changing description clears product name and its manual/autofill state", () => {
  const original = captureQueryDraft();
  const originalDescription = description();
  try {
    setDescription("Молоток 500 г");
    setName("Моё название молотка");
    manuallyEditedProductFields.add("name");
    autofilledProductValues.set("name", "Молоток 500 г");
    autofilledProductValues.set("manufacturer", "Зубр");
    autofilledProductEvidence.set("name", { quote: "Молоток", start: 0, end: 7, source: "llm", conversion: null });

    setDescription("Молоток 500 г");
    assert.equal(name(), "Моё название молотка", "unchanged description must not reset the name");

    const saved = captureQueryDraft();
    setDescription("Дрель 600 Вт");
    assert.equal(name(), "");
    assert.equal(manuallyEditedProductFields.has("name"), false);
    assert.equal(autofilledProductValues.has("name"), false);
    assert.equal(autofilledProductEvidence.has("name"), false);
    assert.equal(autofilledProductValues.get("manufacturer"), "Зубр");

    setDescription("Молоток 500 г");
    restoreQueryDraft(saved);
    assert.equal(name(), "Моё название молотка", "saved query names must still restore");
    setDescription("");
    assert.equal(name(), "", "clearing description must also clear the name");
  } finally {
    setDescription(originalDescription);
    restoreQueryDraft(original);
  }
});

test("catalog fields supplement schemas without duplicate names or observed-value restrictions", () => {
  const schema = draftFromSpgzCharacteristic({ name: "Материал", options: ["Сталь"] });
  const statistics: CatalogCharacteristic[] = [
    { id: "catalog:material", name: "  МАТЕРИАЛ  ", unit: "", numeric: false, count: 10, total: 10, frequency: 1, scope: "category" as const, examples: ["Дерево"], numericValueCounts: null, exampleCounts: { "Дерево": 10 } },
    { id: "catalog:color", name: "Цвет", unit: "", numeric: false, count: 8, total: 10, frequency: 0.8, scope: "adjacent" as const, examples: ["Белый", "Чёрный"], numericValueCounts: null, exampleCounts: { "Белый": 5, "Чёрный": 3 } },
    { id: "catalog:height", name: "Высота", unit: "мм", numeric: true, count: 7, total: 10, frequency: 0.7, scope: "category" as const, examples: ["100", "200"], numericValueCounts: null, exampleCounts: { "100": 2, "200": 1 } },
  ];
  const fields = appendCatalogCharacteristics([schema], statistics);
  assert.equal(fields.length, 3);
  assert.deepEqual(fields[0], { ...schema, catalogFrequency: statistics[0] });
  assert.deepEqual(fields[1].options, []);
  assert.equal(fields[1].value, "");
  assert.equal(fields[1].required, false);
  assert.equal(fields[1].hint, "");
  assert.deepEqual(fields[1].catalogFrequency?.examples, ["Белый", "Чёрный"]);
  assert.deepEqual(fields[1].catalogFrequency?.exampleCounts, { "Белый": 5, "Чёрный": 3 });
  assert.equal(fields[2].numeric, true);
  assert.equal(characteristicSourceLabel(fields[1].source), "из каталога");
  assert.match(catalogFrequencyLabel(statistics[1]), /8 из 10.*смежных.*80%/);
  assert.deepEqual(buildDisplayCharacteristics(fields).map(field => field.source), ["spgz", "catalog", "catalog"]);
  const edited = { ...fields[1], value: "Зелёный" };
  const next = appendCatalogCharacteristics(fields, statistics, [edited]);
  assert.equal(next[1].value, "Зелёный");
  assert.equal(appendCatalogCharacteristics(fields, []).length, 1);
  const custom = { ...edited, id: "custom", custom: true, source: "custom" as const };
  assert.equal(appendCatalogCharacteristics([schema, custom], statistics).length, 3);
  assert.equal(buildDisplayCharacteristics([...fields, custom]).filter(field => field.name === "Цвет").length, 1);
});

test("catalog identities preserve distinct units and manual values after display spelling changes", () => {
  const stats: CatalogCharacteristic[] = [
    { id: "catalog:kg", name: "Масса", nameKey: "масса", unit: "кг", numeric: true, count: 7, total: 10, frequency: 0.7, scope: "category" as const, examples: ["1", "2"], numericValueCounts: null, exampleCounts: { "1": 4, "2": 3 } },
    { id: "catalog:g", name: "Масса", nameKey: "масса", unit: "г", numeric: true, count: 3, total: 10, frequency: 0.3, scope: "category" as const, examples: ["100", "200"], numericValueCounts: null, exampleCounts: { "100": 2, "200": 1 } },
  ];
  const fields = appendCatalogCharacteristics([], stats);
  assert.deepEqual(fields.map(item => item.unit), ["кг", "г"]);
  assert.equal(buildDisplayCharacteristics(fields).length, 2);
  const previous = [{ ...fields[0], value: "1,5" }];
  const refreshed = appendCatalogCharacteristics([], [{ ...stats[0], name: "Массы" }], previous);
  assert.equal(refreshed[0].value, "1,5");
  const edited = previous[0];
  const editKey = characteristicEditKey(edited);
  manuallyEditedCharacteristics.add(editKey);
  manuallyEditedCharacteristicValues.set(editKey, edited);
  try {
    const fields = appendCatalogCharacteristics([], [{ ...stats[0], name: "Массы" }]);
    assert.equal(preserveManualCharacteristics(fields, [edited])[0].value, "1,5");
  } finally {
    manuallyEditedCharacteristics.delete(editKey);
    manuallyEditedCharacteristicValues.delete(editKey);
  }
  const schema = draftFromSpgzCharacteristic({ name: "Материал рукоятки", options: ["Сталь"] });
  const duplicate = { ...stats[0], name: " МАТЕРИАЛ (рукоятки): ", unit: "", numeric: false };
  assert.deepEqual(appendCatalogCharacteristics([schema], [duplicate]), [{ ...schema, catalogFrequency: duplicate }]);
});

test("schema selectors get catalog popularity without changing options or source", () => {
  const schema = draftFromSpgzCharacteristic({ name: "Форма бойка", options: ["Квадратный", "Круглый", "Клиновидный"] });
  const stats: CatalogCharacteristic = {
    id: "catalog:shape", name: "Форма бойка", unit: "", numeric: false,
    count: 100, total: 120, frequency: 100 / 120, scope: "category",
    examples: ["квадратная", "круглая"], numericValueCounts: null, exampleCounts: { "квадратная": 70, "круглая": 20 },
  };
  const [field] = appendCatalogCharacteristics([schema], [stats]);
  assert.equal(field.source, "spgz");
  assert.deepEqual(field.options, schema.options);
  assert.deepEqual(catalogOptionPopularity(field, "Квадратный"), { count: 70, total: 100, percent: 70 });
  assert.equal(catalogOptionPopularity(field, "Клиновидный"), undefined);
  const ktru = { ...schema, id: "ktru:shape", source: "ktru" as const };
  const enriched = appendCatalogCharacteristics([ktru, schema], [stats]);
  const [merged] = buildDisplayCharacteristics(enriched);
  assert.equal(merged.source, "both");
  assert.deepEqual(catalogOptionPopularity(merged, "Круглый"), { count: 20, total: 100, percent: 20 });
  assert.equal(appendCatalogCharacteristics([field], [])[0].catalogFrequency, undefined);
  const differentUnit = { ...stats, unit: "мм" };
  assert.equal(appendCatalogCharacteristics([schema], [differentUnit])[0].catalogFrequency, undefined);
  const ambiguous = { ...field, catalogFrequency: { ...stats, numericValueCounts: null, exampleCounts: { "квадратная": 70, "квадратный": 20 } } };
  assert.equal(catalogOptionPopularity(ambiguous, "Квадратный"), undefined);
  assert.deepEqual(catalogOptionPopularity(field, "—"), { count: 20, total: 120, percent: 17 });
  assert.deepEqual(catalogOptionPopularity(merged, "—"), { count: 20, total: 120, percent: 17 });
  assert.deepEqual(catalogOptionPopularity({ ...field, catalogFrequency: { ...stats, count: 0 } }, "—"), { count: 120, total: 120, percent: 100 });
  assert.deepEqual(catalogOptionPopularity({ ...field, catalogFrequency: { ...stats, count: 120 } }, "—"), { count: 0, total: 120, percent: 0 });
  assert.equal(catalogOptionPopularity({ ...field, catalogFrequency: { ...stats, count: 0, total: 0 } }, "—"), undefined);
  assert.equal(catalogOptionPopularity(schema, "—"), undefined);
});

test("range selectors aggregate the full numeric distribution with exact boundaries", () => {
  const field: DraftCharacteristic = {
    id: "ktru:mass", name: "Масса", value: "", rangeOption: "", unit: "кг", source: "ktru",
    required: false, badges: [], hint: "", multiple: false, optionLabels: {},
    options: ["<0.1", ">=0.1<0.2", ">=0.2<0.3", ">=0.3", ">=2"],
    ranges: [
      { option: "<0.1", min: null, max: 0.1, minInclusive: false, maxInclusive: false },
      { option: ">=0.1<0.2", min: 0.1, max: 0.2, minInclusive: true, maxInclusive: false },
      { option: ">=0.2<0.3", min: 0.2, max: 0.3, minInclusive: true, maxInclusive: false },
      { option: ">=0.3", min: 0.3, max: null, minInclusive: true, maxInclusive: false },
      { option: ">=2", min: 2, max: null, minInclusive: true, maxInclusive: false },
    ],
    catalogFrequency: {
      id: "catalog:mass", name: "Масса", unit: "кг", numeric: true, scope: "category",
      count: 10, total: 12, frequency: 10 / 12,
      examples: ["0.05"], exampleCounts: { "0.05": 1 },
      numericValueCounts: { "0.05": 1, "0.1": 2, "0.15": 2, "0.2": 1, "0.25": 1, "0.3": 2, "1": 1 },
    },
  };
  for (const [option, count] of [["<0.1", 1], [">=0.1<0.2", 4], [">=0.2<0.3", 2], [">=0.3", 3], [">=2", 0]] as const) {
    assert.deepEqual(catalogOptionPopularity(field, option), { count, total: 10, percent: count * 10 });
  }
  assert.deepEqual(catalogOptionPopularity(field, "—"), { count: 2, total: 12, percent: 17 });
  assert.equal(catalogOptionPopularity(field, "unknown"), undefined);
  assert.equal(catalogOptionPopularity({ ...field, catalogFrequency: { ...field.catalogFrequency!, numericValueCounts: null } }, ">=0.3"), undefined);
});

test("catalog manual values survive refreshed frequency statistics", () => {
  const stats = [{ id: "catalog:color", name: "Цвет", unit: "", numeric: false, count: 5, total: 10, frequency: 0.5, scope: "category" as const, examples: ["Белый"], numericValueCounts: null, exampleCounts: { "Белый": 5 } }];
  const original = { ...appendCatalogCharacteristics([], stats)[0], value: "Зелёный" };
  const key = characteristicEditKey(original);
  manuallyEditedCharacteristics.add(key);
  manuallyEditedCharacteristicValues.set(key, original);
  try {
    const fields = preserveManualCharacteristics(appendCatalogCharacteristics([], [{ ...stats[0], examples: ["Чёрный"] }]), []);
    assert.equal(fields[0].value, "Зелёный");
    assert.equal(fields[0].hint, "");
    assert.deepEqual(fields[0].catalogFrequency?.examples, ["Чёрный"]);
  } finally {
    manuallyEditedCharacteristics.delete(key);
    manuallyEditedCharacteristicValues.delete(key);
  }
});

test("SPGZ quantitative fields keep their type even without characteristic units", () => {
  const height = draftFromSpgzCharacteristic({ name: "Высота", value: "≥ 1", badges: ["количественная", "не менее"] });
  assert.equal(height.numeric, true);
  assert.equal(height.unit, "");
  assert.equal(height.hint, "Ограничение СПГЗ: ≥ 1");
  assert.equal(height.value, "");
  const shelves = draftFromSpgzCharacteristic({ name: "Количество полок", badges: ["количественная"], parts: [{ text: "≥ 0 шт", okeiName: "Штука" }] });
  assert.equal(shelves.numeric, true);
  assert.equal(shelves.unit, "шт.");
  const doors = draftFromSpgzCharacteristic({ name: "Тип дверей шкафа", options: ["Распашные", "Раздвижные (купе)"], badges: ["качественная"] });
  assert.equal(doors.numeric, false);
  assert.equal(doors.value, "");
});

test("SPGZ selector options are not repeated in generated hints", () => {
  const options = [">= 0.5 < 0.6", ">= 0.6 < 0.8"];
  const field = draftFromSpgzCharacteristic({
    name: "Масса", value: options.join(" · "), options, badges: ["количественная"],
  });
  assert.equal(field.hint, "");
  assert.deepEqual(field.options, options);
});

test("refined positions retain missing template fields without overriding child constraints", () => {
  const makeField = (id: string, name: string, options: string[], value = ""): DraftCharacteristic => ({
    id, name, options, value, rangeOption: "", optionLabels: {}, ranges: [], unit: "",
    source: "ktru", required: false, badges: [], hint: "", multiple: false,
  });
  const material = { ...makeField("material", "Материал бойка", ["Сталь", "Алюминий", "Медь"], "Сталь"), autofill: { confidence: 0.98, model: "jev-latest" } };
  const empty = makeField("empty", "Безынерционный", ["Да", "Нет"], "   ");
  const rangeOnly = { ...makeField("range", "Масса", ["<1 кг"]), rangeOption: "<1 кг" };
  const templateShape = makeField("shape", "Форма бойка", ["Круглый", "Квадратный", "Клиновидный"]);
  const childShape = makeField("shape", "Форма бойка", ["Круглый", "Квадратный"], "Круглый");
  const templateCode = "25.73.30.141-00000011";
  const fields = retainTemplateCharacteristics([childShape], [templateShape, material], templateCode);
  assert.equal(fields.length, 2);
  assert.equal(fields[0], childShape);
  assert.equal(fields[0].templateKtruCode, undefined);
  assert.equal(fields[1].value, "Сталь");
  assert.equal(fields[1].templateKtruCode, templateCode);
  assert.equal(characteristicSourceLabel(fields[1].source, fields[1].templateKtruCode), "из шаблона КТРУ");
  assert.equal(fields[1].autofill?.confidence, 0.98);
  assert.equal(retainTemplateCharacteristics([childShape], [templateShape, empty], templateCode).length, 1);
  assert.equal(retainTemplateCharacteristics([], [rangeOnly], templateCode)[0].rangeOption, "<1 кг");
  assert.equal(retainTemplateCharacteristics([{ ...empty, templateKtruCode: templateCode }], [], templateCode).length, 0);
  assert.equal(retainTemplateCharacteristics([empty], [], templateCode).length, 1);
  assert.equal(retainTemplateCharacteristics(fields, [templateShape, material], templateCode).length, 2);
  const spgzMaterial = { ...material, id: "spgz:material", source: "spgz" as const };
  const grouped = buildDisplayCharacteristics([...fields, spgzMaterial]);
  const groupedMaterial = grouped.find(item => item.name === material.name)!;
  assert.equal(groupedMaterial.templateKtruCode, templateCode);
  assert.equal(characteristicSourceLabel(groupedMaterial.source, groupedMaterial.templateKtruCode), "из шаблона КТРУ, СПГЗ");
  assert.deepEqual(groupedMaterial.linkedIds, ["material", "spgz:material"]);
  assert.equal(groupedMaterial.autofill?.confidence, 0.98);
  const manualMaterial = { ...fields[1], autofill: undefined };
  assert.equal(buildDisplayCharacteristics([manualMaterial, spgzMaterial])[0].autofill, undefined);
  const emptyMaterial = { ...fields[1], value: "", rangeOption: "", autofill: undefined };
  assert.equal(buildDisplayCharacteristics([emptyMaterial, spgzMaterial])[0].autofill?.confidence, 0.98);
  const otherPosition = [makeField("power", "Мощность", [], "500")];
  assert.equal(buildDisplayCharacteristics(otherPosition).some(item => item.templateKtruCode), false);
});


test("range diagnostics flag out-of-range numbers without hiding the value", () => {
  const field = {
    value: "10000", unit: "Килограмм", optionLabels: { bucket: "От 0,1 до 2 кг" },
    ranges: [{ option: "bucket", min: 0.1, max: 2, minInclusive: true, maxInclusive: false }],
  };
  const problem = characteristicRangeProblem(field)!;
  assert.match(problem, /10000 кг/);
  assert.match(problem, /От 0,1 до 2 кг/);
  assert.equal(field.value, "10000");
  for (const value of ["0.1", "0,5", "1.99"]) {
    assert.equal(characteristicRangeProblem({ ...field, value }), null);
  }
  assert.ok(characteristicRangeProblem({ ...field, value: "2" }));
  assert.ok(characteristicRangeProblem({ ...field, value: "0" }));
  assert.ok(characteristicRangeProblem({ ...field, value: "0,09" }));
  assert.equal(characteristicRangeProblem({ ...field, value: "abc" }), "Введите числовое значение характеристики.");
  assert.equal(characteristicRangeProblem({ ...field, value: "  " }), null);
  assert.equal(characteristicRangeProblem({ ...field, ranges: [] }), null);
  assert.equal(characteristicRangeProblem({ ...field, ranges: [{ ...field.ranges[0], max: null }] }), null);
});

test("merged range fields retain out-of-range numbers without inventing a catalog bucket", () => {
  const field: DraftCharacteristic = {
    id: "ktru:mass", name: "Масса", value: "10000", rangeOption: "", options: ["0.1–2"],
    optionLabels: {}, ranges: [{ option: "0.1–2", min: 0.1, max: 2, minInclusive: true, maxInclusive: true }],
    unit: "кг", source: "ktru", required: true, badges: [], hint: "", multiple: false,
    autofill: { confidence: 0.95, model: "jev" },
  };
  const spgz = { ...field, id: "spgz:mass", source: "spgz" as const, value: "", autofill: undefined };
  const merged = buildDisplayCharacteristics([field, spgz])[0];
  assert.equal(merged.value, "10000");
  assert.equal(merged.rangeOption, "");
  assert.equal(merged.autofill?.confidence, 0.95);
  assert.ok(characteristicRangeProblem(merged));
  const fromSpgz = buildDisplayCharacteristics([{ ...field, value: "" }, { ...spgz, value: "10000" }])[0];
  assert.equal(fromSpgz.value, "10000");
  assert.equal(fromSpgz.rangeOption, "");
  assert.ok(characteristicRangeProblem(fromSpgz));
  const valid = buildDisplayCharacteristics([{ ...field, value: "0.5" }, { ...spgz, value: "10000" }])[0];
  assert.equal(valid.rangeOption, "0.1–2");
  assert.equal(characteristicRangeProblem(valid), null);
});

test("schema merge requires compatible units, numeric flags and multiplicity", () => {
  const ktru: DraftCharacteristic = {
    id: "ktru:mass", name: "Масса", value: "", rangeOption: "", options: [], optionLabels: {},
    ranges: [], unit: "кг", numeric: true, source: "ktru", required: false, badges: [], hint: "", multiple: false,
  };
  const spgzSame = { ...ktru, id: "spgz:mass", source: "spgz" as const };
  const spgzDifferentUnit = { ...spgzSame, unit: "г" };
  assert.equal(schemaEquivalent(ktru, spgzSame), true);
  assert.equal(schemaEquivalent(ktru, spgzDifferentUnit), false);
  assert.equal(buildDisplayCharacteristics([ktru, spgzDifferentUnit]).length, 2);
  assert.equal(buildDisplayCharacteristics([ktru, spgzSame]).length, 1);
});

test("autofill confidence uses readable levels without presenting it as guaranteed accuracy", () => {
  assert.deepEqual(autofillConfidencePresentation(0.99), { percent: 99, level: "high", label: "Высокая уверенность" });
  assert.equal(autofillConfidencePresentation(0.9).level, "high");
  assert.equal(autofillConfidencePresentation(0.81).level, "high");
  assert.equal(autofillConfidencePresentation(0.8001).level, "high");
  assert.equal(autofillConfidencePresentation(0.8).level, "medium");
  assert.equal(autofillConfidencePresentation(0.7).level, "medium");
  assert.equal(autofillConfidencePresentation(0.69).level, "low");
});


test("manual linked values survive a template schema that temporarily removes SPGZ rows", () => {
  const makeField = (source: "ktru" | "spgz", options = ["0.5", "0.7"]): DraftCharacteristic => ({
    id: source, source, name: "Масса головки молотка", unit: source === "ktru" ? "кг" : "",
    value: "0.7", rangeOption: "0.7", options, optionLabels: {}, ranges: [],
    required: false, badges: [], hint: "", multiple: false,
  });
  const previous = [makeField("ktru"), makeField("spgz")];
  manuallyEditedCharacteristics.clear();
  manuallyEditedCharacteristicValues.clear();
  for (const field of previous) {
    manuallyEditedCharacteristics.add(characteristicEditKey(field));
    manuallyEditedCharacteristicValues.set(characteristicEditKey(field), { ...field });
  }
  const template = preserveManualCharacteristics([{ ...previous[0], value: "", rangeOption: "", autofill: { confidence: 0.99, model: "jev-latest" } }], previous);
  assert.equal(template[0].autofill, undefined);
  const refined = preserveManualCharacteristics(previous.map(field => ({ ...field, value: "", rangeOption: "" })), template);
  assert.deepEqual(refined.map(field => field.value), ["0.7", "0.7"]);
  assert.deepEqual(refined.map(field => field.rangeOption), ["0.7", "0.7"]);
  const differentSchema = { ...makeField("spgz", ["0.9"]), value: "", rangeOption: "" };
  assert.equal(preserveManualCharacteristics([differentSchema], template)[0].value, "");
  const unedited = { ...makeField("ktru"), name: "Длина рукоятки", value: "" };
  assert.equal(preserveManualCharacteristics([unedited], previous)[0].value, "");
  manuallyEditedCharacteristics.clear();
  manuallyEditedCharacteristicValues.clear();
});

test("guesses a category from a Russian product name but not unknown products", () => {
  assert.equal(guessCategory("Бумага офисная А4"), "paper");
  assert.equal(guessCategory("Стул офисный"), "furniture");
  assert.equal(guessCategory("Ноутбук"), "computers");
  assert.equal(guessCategory("Чистящее средство для уборки"), "cleaning");
  assert.equal(guessCategory("Кусачки для ногтей и кутикулы"), "manicure");
  assert.equal(guessCategory("Необычная вещь"), "");
});

test("fills only properties explicitly present in the description", () => {
  const extracted = extractCharacteristics("paper", "Бумага А4, плотность 80 г/м², белая, 500 листов.");
  assert.deepEqual(extracted, [
    { name: "Формат", value: "А4" },
    { name: "Плотность", value: "80 г/м²" },
    { name: "Количество листов", value: "500 шт." },
    { name: "Цвет", value: "Белый" },
  ]);
  assert.equal(extractCharacteristics("paper", "Офисная бумага").every(item => item.value === ""), true);
  assert.deepEqual(extractCharacteristics("nonexistent", "А4"), []);
});

test("uses different characteristics for different categories", () => {
  assert.deepEqual(extractCharacteristics("furniture", "Металлический стул, ширина 45 см, чёрный").map(item => item.name), categories.find(item => item.id === "furniture")!.attributes);
  assert.equal(extractCharacteristics("computers", "Ноутбук с 16 ГБ ОЗУ и SSD 512 ГБ")[0].value, "16 гб");
  assert.equal(extractCharacteristics("cleaning", "Средство для мытья посуды, объем 500 мл, аромат лимона")[0].value, "500 мл");
  assert.deepEqual(extractCharacteristics("manicure", "Кусачки из нержавеющей стали для кутикулы, длина 10 см").map(item => item.value), ["нержавеющей стали", "Для кутикулы", "10 см"]);
});

test("suggests a KTRU position from the description without a product name or selected category", () => {
  const description = "Бумага офисная\nформата А4, плотность 80 г/м², белая, 500 листов.";
  const suggestion = suggestKtruCategory(description);
  assert.deepEqual(suggestion, {
    categoryId: "paper",
    code: "17.12.14.110-00000001",
    name: "Бумага для офисной техники",
  });
  assert.equal(extractCharacteristics(suggestion!.categoryId, description).filter(item => item.value).length, 4);
  assert.equal(suggestKtruCategory("Ноутбук с 16 ГБ ОЗУ и SSD 512 ГБ")?.categoryId, "computers");
  assert.equal(suggestKtruCategory("Металлический стул, ширина 45 см, чёрный")?.name, "Стул на металлическом каркасе");
  assert.equal(suggestKtruCategory("Средство для мытья посуды, объем 500 мл")?.categoryId, "cleaning");
});

test("does not invent KTRU suggestions for empty descriptions or products outside the demo set", () => {
  for (const description of ["", "   ", "Необычная вещь", "Картон", "Кусачки для ногтей"]) {
    assert.equal(suggestKtruCategory(description), null);
  }
});
