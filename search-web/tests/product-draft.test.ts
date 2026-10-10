import assert from "node:assert/strict";
import test from "node:test";
import { categories, extractCharacteristics, guessCategory, suggestKtruCategory } from "../src/lib/product-draft";
import {
  characteristicEditKey,
  manuallyEditedCharacteristics,
  manuallyEditedCharacteristicValues,
  preserveManualCharacteristics,
} from "../src/lib/create-product-state";
import type { DraftCharacteristic } from "../src/lib/types";
import { characteristicRangeProblem } from "../src/lib/characteristic-options";
import { draftFromSpgzCharacteristic } from "../src/lib/spgz-characteristics";
import { autofillConfidencePresentation, buildDisplayCharacteristics, characteristicSourceLabel, mapOptionProbabilities, retainTemplateCharacteristics } from "../src/lib/merge-characteristics";


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

test("option probabilities follow matching schema options, including zero probabilities", () => {
  const probabilities = { text: "Молоток", values: { "≥ 0.5 кг": 0.85, "< 0.5 кг": 0, "Сталь": 0.99 } };
  assert.deepEqual(mapOptionProbabilities(probabilities, [">=0.5", "<0.5", "Медь"]), {
    text: "Молоток", values: { ">=0.5": 0.85, "<0.5": 0 },
  });
  assert.equal(mapOptionProbabilities(undefined, ["Сталь"]), undefined);
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
