// Demonstration-only suggestions. No catalog or supplier-portal requests are made here.
export type DraftCategory = { id: string; name: string; path: string; attributes: string[] };
export type DraftCharacteristic = { name: string; value: string };
export type KtruSuggestion = { categoryId: string; code: string; name: string };

// Small frontend demo set, copied from data/ktru-actual.parquet (ACTIVE positions).
// This is not a full catalog search or a certified classification result.
const ktruExamples: (KtruSuggestion & { matches: RegExp })[] = [
  { categoryId: "paper", code: "17.12.14.110-00000001", name: "Бумага для офисной техники", matches: /бумаг.{0,40}(?:офис|принтер|печати|[аa][345])|(?:офисн|принтер).{0,20}бумаг/i },
  { categoryId: "furniture", code: "31.01.12.160-00000005", name: "Кресло офисное", matches: /кресл.{0,25}офис|офис.{0,25}кресл/i },
  { categoryId: "furniture", code: "31.01.11.150-00000001", name: "Стул на металлическом каркасе", matches: /стул.{0,40}металл|металл.{0,25}стул/i },
  { categoryId: "furniture", code: "31.01.12.160-00000001", name: "Стул на деревянном каркасе", matches: /стул.{0,40}дерев|дерев.{0,25}стул/i },
  { categoryId: "furniture", code: "31.09.14.110-00000001", name: "Стул пластиковый", matches: /стул.{0,40}пластик|пластик.{0,25}стул/i },
  { categoryId: "computers", code: "26.20.11.110-00000001", name: "Ноутбук", matches: /ноутбук/i },
  { categoryId: "cleaning", code: "20.41.32.111-00000009", name: "Средство для мытья посуды", matches: /(?:средств|гель|жидкост|порошок).{0,40}(?:мытья\s+)?посуд/i },
];

export function suggestKtruCategory(description: string): KtruSuggestion | null {
  const text = description.trim().replace(/\s+/g, " ");
  const match = ktruExamples.find(item => item.matches.test(text));
  return match ? { categoryId: match.categoryId, code: match.code, name: match.name } : null;
}

export const categories: DraftCategory[] = [
  { id: "paper", name: "Офисная бумага", path: "Канцелярские товары › Бумага › Офисная бумага", attributes: ["Формат", "Плотность", "Количество листов", "Цвет"] },
  { id: "furniture", name: "Мебель и стулья", path: "Мебель › Офисная мебель › Стулья и кресла", attributes: ["Материал", "Цвет", "Ширина"] },
  { id: "computers", name: "Компьютеры и ноутбуки", path: "Электроника › Компьютерная техника › Компьютеры и ноутбуки", attributes: ["Оперативная память", "Накопитель", "Диагональ экрана"] },
  { id: "cleaning", name: "Средства для уборки", path: "Хозяйственные товары › Моющие и чистящие средства", attributes: ["Объём", "Назначение", "Аромат"] },
  { id: "manicure", name: "Инструменты для маникюра", path: "Красота и гигиена › Маникюр и педикюр › Инструменты", attributes: ["Материал", "Назначение", "Длина"] },
  { id: "other", name: "Другая категория", path: "Прочие товары", attributes: ["Материал", "Цвет", "Размер"] },
];

export function guessCategory(name: string): string {
  const text = name.toLocaleLowerCase("ru");
  if (/бумаг|лист(ы|ов)?\s+(а[345]|для\s+принтера)|картон/.test(text)) return "paper";
  if (/стул|кресл|стол|шкаф|тумб|мебел/.test(text)) return "furniture";
  if (/ноутбук|компьютер|моноблок|системн.{0,12}блок|пк/.test(text)) return "computers";
  if (/моющ|чистящ|средств.{0,15}(уборк|мыть)|порошок\s+стиральн|гель\s+для\s+стирк/.test(text)) return "cleaning";
  if (/кусачк|кутикул|маникюр|педикюр|пилочк.{0,10}ногт/.test(text)) return "manicure";
  return "";
}

function first(text: string, pattern: RegExp): string {
  return text.match(pattern)?.[1]?.trim() ?? "";
}

function color(text: string): string {
  const match = text.match(/(бел(?:ый|ая|ое|ые|ого)|ч[её]рн(?:ый|ая|ое|ые|ого)|син(?:ий|яя|ее|ие)|сер(?:ый|ая|ое|ые)|красн(?:ый|ая|ое|ые)|зел[её]н(?:ый|ая|ое|ые))/i);
  if (!match) return "";
  const stem = match[1].toLocaleLowerCase("ru");
  if (stem.startsWith("бел")) return "Белый";
  if (stem.startsWith("ч")) return "Чёрный";
  if (stem.startsWith("син")) return "Синий";
  if (stem.startsWith("сер")) return "Серый";
  if (stem.startsWith("крас")) return "Красный";
  return "Зелёный";
}

export function extractCharacteristics(categoryId: string, description: string): DraftCharacteristic[] {
  const category = categories.find(item => item.id === categoryId);
  if (!category) return [];
  const text = description.toLocaleLowerCase("ru");
  const material = first(text, /(металл(?:а|ический)?|пластик(?:а|овый)?|дерев(?:о|янный)|ткань|кожа|кожзам|стекло)/i);
  const values: Record<string, Record<string, string>> = {
    paper: {
      "Формат": first(text, /(а[345])/i).toUpperCase(),
      "Плотность": first(text, /(\d{2,3}(?:[.,]\d+)?)\s*(?:г\s*\/\s*м\s*(?:²|2)|грамм(?:ов|а)?\s+на\s+(?:кв(?:адратный)?\.?\s*)?м(?:етр)?)/i) ? `${first(text, /(\d{2,3}(?:[.,]\d+)?)\s*(?:г\s*\/\s*м\s*(?:²|2)|грамм(?:ов|а)?\s+на\s+(?:кв(?:адратный)?\.?\s*)?м(?:етр)?)/i)} г/м²` : "",
      "Количество листов": first(text, /(\d{1,4})\s*лист(?:ов|а)?/i) ? `${first(text, /(\d{1,4})\s*лист(?:ов|а)?/i)} шт.` : "",
      "Цвет": color(text),
    },
    furniture: {
      "Материал": material,
      "Цвет": color(text),
      "Ширина": first(text, /ширин[а-я]*\s*(?:[:—-]\s*)?(\d{2,3}(?:[.,]\d+)?\s*(?:см|мм))/i),
    },
    computers: {
      "Оперативная память": first(text, /(\d{1,3}\s*гб)\s*(?:озу|оперативн(?:ой|ая)\s+памят[ьи])/i) || first(text, /(?:озу|оперативн(?:ая|ой)\s+памят[ьи])\s*[:—-]?\s*(\d{1,3}\s*гб)/i),
      "Накопитель": first(text, /((?:ssd|hdd)\s*\d{2,4}\s*(?:гб|тб))/i) || first(text, /(\d{2,4}\s*(?:гб|тб)\s*(?:ssd|hdd))/i),
      "Диагональ экрана": first(text, /(?:диагональ(?:\s+экрана)?\s*[:—-]?\s*)(\d{1,2}(?:[.,]\d+)?\s*(?:дюйм(?:ов|а)?|"))/i),
    },
    cleaning: {
      "Объём": first(text, /(?:объ[её]м\s*[:—-]?\s*)?(\d+(?:[.,]\d+)?\s*(?:мл|л(?:итр(?:ов|а)?)?))/i),
      "Назначение": first(text, /для\s+(уборки|мытья\s+посуды|стирки|мытья\s+пола)/i) ? `Для ${first(text, /для\s+(уборки|мытья\s+посуды|стирки|мытья\s+пола)/i)}` : "",
      "Аромат": first(text, /(?:аромат|запах)\s*[:—-]?\s*(лимон(?:а)?|лаванд(?:ы|а)|яблок(?:а)|мят(?:ы|а))/i),
    },
    manicure: {
      "Материал": first(text, /(нержавеющ(?:ая|ей)\s+стал[ьи]|стал[ьи]|металл|пластик)/i),
      "Назначение": first(text, /для\s+(ногтей|кутикулы|маникюра|педикюра)/i) ? `Для ${first(text, /для\s+(ногтей|кутикулы|маникюра|педикюра)/i)}` : "",
      "Длина": first(text, /длин[а-я]*\s*[:—-]?\s*(\d{1,3}(?:[.,]\d+)?\s*(?:см|мм))/i),
    },
    other: {
      "Материал": material,
      "Цвет": color(text),
      "Размер": first(text, /размер\s*[:—-]?\s*(\d{1,4}(?:\s*[xх×]\s*\d{1,4}){1,2}\s*(?:мм|см))/i),
    },
  };
  return category.attributes.map(name => ({ name, value: values[categoryId]?.[name] ?? "" }));
}
