import assert from "node:assert/strict";
import { test } from "node:test";
import {
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
