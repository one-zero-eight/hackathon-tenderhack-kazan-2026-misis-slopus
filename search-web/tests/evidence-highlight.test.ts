import assert from "node:assert/strict";
import { test } from "node:test";
import {
  collectCategorySpans,
  collectEvidenceSpans,
  evidenceSourceLabel,
  formatEvidenceConversion,
  highlightDescription,
} from "../src/lib/evidence-highlight";
import type { DraftCharacteristic } from "../src/lib/types";
import type { SpellerError } from "../src/lib/yandex-speller";

function field(partial: Partial<DraftCharacteristic> & Pick<DraftCharacteristic, "id" | "name" | "value">): DraftCharacteristic {
  return {
    rangeOption: "", required: false, badges: [], hint: "", options: [], optionLabels: {},
    ranges: [], unit: "", multiple: false, source: "ktru",
    ...partial,
  };
}

test("collectEvidenceSpans keeps grounded quotes and drops stale offsets", () => {
  const text = "Молоток 500 г, рукоятка деревянная";
  const rows = [
    field({
      id: "mass", name: "Масса", value: "0.5", unit: "кг",
      autofill: {
        confidence: 0.9, model: "m",
        evidence: {
          quote: "500 г", start: text.indexOf("500 г"), end: text.indexOf("500 г") + 5,
          source: "llm",
          conversion: { from: "г", to: "кг", factor: 0.001, result: 0.5 },
        },
      },
    }),
    field({
      id: "stale", name: "Цвет", value: "красный",
      autofill: {
        confidence: 0.9, model: "m",
        evidence: { quote: "красный", start: 0, end: 7, source: "llm", conversion: null },
      },
    }),
  ];
  const spans = collectEvidenceSpans(text, rows);
  assert.equal(spans.length, 1);
  assert.equal(spans[0]!.id, "mass");
  assert.equal(formatEvidenceConversion(spans[0]!.conversion), "500 г → 0.5 кг");
  assert.equal(evidenceSourceLabel("rules"), "регулярка");
});

test("category highlighting uses whole words and groups matching classifiers", () => {
  const text = "Бумага офисная для принтера, А4, белая";
  const spans = collectCategorySpans(text, [
    { classifier: "КТРУ", category: { code: "17.12-05", name: "Бумага офисная А4, белая", score: 1 } },
    { classifier: "ОКПД2", category: { code: "17.12", name: "Бумага для печати прочая", score: 1 } },
    { classifier: "СПГЗ", category: null },
  ]);
  assert.deepEqual(spans.map((span) => text.slice(span.start, span.end)), ["Бумага", "офисная", "белая"]);
  assert.deepEqual(spans[0]!.categories!.map((category) => category.classifier), ["КТРУ", "ОКПД2"]);
  assert.equal(evidenceSourceLabel(spans[0]!.source), "категория");
  assert.match(highlightDescription(text, [], spans), /autofill-evidence category-evidence/);
  assert.equal(collectCategorySpans("Бумажный пакет", [
    { classifier: "КТРУ", category: { code: "1", name: "Бумага", score: 1 } },
  ]).length, 0);
});

test("category highlighting coexists with overlapping characteristic evidence", () => {
  const text = "Бумага белая";
  const spans = collectCategorySpans(text, [
    { classifier: "КТРУ", category: { code: "1", name: text, score: 1 } },
  ]);
  const html = highlightDescription(text, [], [...spans, {
    id: "color", start: 7, end: 12, name: "Цвет", value: "Белая", unit: "",
    source: "llm", conversion: null,
  }]);
  assert.match(html, /category-evidence/);
  assert.match(html, /data-evidence-id="color"/);
  assert.equal(html.replace(/<[^>]+>/g, "").trim(), text);
});

test("highlightDescription merges spell and evidence spans", () => {
  const text = "Молоток 500 г";
  const errors: SpellerError[] = [
    { code: 1, pos: 0, len: 7, row: 0, col: 0, word: "Молоток", s: ["Молоток"] },
  ];
  const html = highlightDescription(
    text,
    errors,
    [{
      id: "mass", start: 8, end: 13, name: "Масса", value: "0.5", unit: "кг",
      source: "llm", conversion: { from: "г", to: "кг", factor: 0.001, result: 0.5 },
    }],
    "mass",
  );
  assert.match(html, /spell-error/);
  assert.match(html, /autofill-evidence is-flash/);
  assert.match(html, /data-evidence-id="mass"/);
});
