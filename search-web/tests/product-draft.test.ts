import assert from "node:assert/strict";
import test from "node:test";
import { categories, extractCharacteristics, guessCategory, suggestKtruCategory } from "../src/lib/product-draft";

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
