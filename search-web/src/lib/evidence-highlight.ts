import type { AutofillEvidence, DraftCharacteristic, VotedCode } from "./types";
import type { SpellerError } from "./yandex-speller";

export type EvidenceSpan = {
  id: string;
  start: number;
  end: number;
  name: string;
  value: string;
  unit: string;
  source: AutofillEvidence["source"] | "category";
  conversion: AutofillEvidence["conversion"];
  categories?: { name: string; code: string; classifier: string }[];
};

function escapeHtml(text: string): string {
  return text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

export function evidenceFromField(item: DraftCharacteristic): AutofillEvidence | null {
  return item.autofill?.evidence ?? item.autofillDiagnostics?.evidence ?? null;
}

/** Collect grounded evidence spans for the current description text. */
export function collectEvidenceSpans(
  text: string,
  characteristics: DraftCharacteristic[],
  product: { id: string; name: string; value: string; evidence: AutofillEvidence | null }[] = [],
): EvidenceSpan[] {
  const spans: EvidenceSpan[] = [];
  for (const item of characteristics) {
    const evidence = evidenceFromField(item);
    if (!evidence || !item.value.trim()) continue;
    if (evidence.start < 0 || evidence.end > text.length || evidence.start >= evidence.end) continue;
    if (text.slice(evidence.start, evidence.end) !== evidence.quote) continue;
    spans.push({
      id: item.id,
      start: evidence.start,
      end: evidence.end,
      name: item.name,
      value: item.value,
      unit: item.unit,
      source: evidence.source,
      conversion: evidence.conversion ?? null,
    });
  }
  for (const item of product) {
    const evidence = item.evidence;
    if (!evidence || !item.value.trim()) continue;
    if (evidence.start < 0 || evidence.end > text.length || evidence.start >= evidence.end) continue;
    if (text.slice(evidence.start, evidence.end) !== evidence.quote) continue;
    spans.push({
      id: item.id,
      start: evidence.start,
      end: evidence.end,
      name: item.name,
      value: item.value,
      unit: "",
      source: evidence.source,
      conversion: evidence.conversion ?? null,
    });
  }
  return spans.sort((a, b) => a.start - b.start || a.end - b.end);
}

const CATEGORY_STOP_WORDS = new Set(["для", "или", "при", "без", "под", "над", "тип", "вид", "прочие", "прочий", "прочая", "прочее", "прочих", "изделия", "изделие", "товары", "товар", "услуги", "услуга"]);

/** Lexical matches only: these spans are not LLM evidence for classification. */
export function collectCategorySpans(
  text: string,
  categories: { classifier: string; category: VotedCode | null }[],
): EvidenceSpan[] {
  const tokens = [...text.matchAll(/[\p{L}\p{N}]+(?:-[\p{L}\p{N}]+)*/gu)];
  const fold = (word: string) => word.toLocaleLowerCase("ru").replaceAll("ё", "е");
  const matches = new Map<number, EvidenceSpan>();
  for (const { classifier, category } of categories) {
    if (!category) continue;
    const words = new Set((category.name.match(/[\p{L}\p{N}]+(?:-[\p{L}\p{N}]+)*/gu) ?? [])
      .map(fold).filter((word) => word.length >= 3 && !CATEGORY_STOP_WORDS.has(word)));
    for (const token of tokens) {
      if (!words.has(fold(token[0]))) continue;
      const start = token.index!;
      const categoryInfo = { name: category.name, code: category.code, classifier };
      const prior = matches.get(start);
      if (prior) {
        prior.categories!.push(categoryInfo);
        continue;
      }
      matches.set(start, {
        id: `category:${start}`, start, end: start + token[0].length,
        name: "Совпадение с названием категории", value: category.name, unit: "",
        source: "category", conversion: null, categories: [categoryInfo],
      });
    }
  }
  return [...matches.values()].sort((a, b) => a.start - b.start);
}

export function formatEvidenceConversion(conversion: AutofillEvidence["conversion"]): string | null {
  if (!conversion) return null;
  const source = conversion.factor != null && conversion.factor !== 0
    ? formatCompact(conversion.result / conversion.factor)
    : null;
  if (source == null) return `${conversion.result} ${conversion.to}`;
  return `${source} ${conversion.from} → ${conversion.result} ${conversion.to}`;
}

function formatCompact(value: number): string {
  return Number.isFinite(value) ? Number(value.toPrecision(12)).toString() : String(value);
}

export function evidenceSourceLabel(source: EvidenceSpan["source"]): string {
  if (source === "category") return "категория";
  return source === "rules" ? "регулярка" : "из текста";
}

type Marker = { pos: number; kind: "start" | "end"; id: string; type: "evidence" | "spell"; index: number };

/** Mirror text with spell-error and autofill-evidence spans. */
export function highlightDescription(
  text: string,
  errors: SpellerError[],
  evidence: EvidenceSpan[],
  flashId: string | null = null,
): string {
  if (!text) return "\n";
  const markers: Marker[] = [];
  for (const [index, span] of evidence.entries()) {
    if (span.start < 0 || span.end > text.length || span.start >= span.end) continue;
    markers.push({ pos: span.start, kind: "start", id: span.id, type: "evidence", index });
    markers.push({ pos: span.end, kind: "end", id: span.id, type: "evidence", index });
  }
  for (const [index, err] of errors.entries()) {
    if (err.pos < 0 || err.len <= 0 || err.pos >= text.length) continue;
    const end = Math.min(err.pos + err.len, text.length);
    markers.push({ pos: err.pos, kind: "start", id: `spell-${index}`, type: "spell", index });
    markers.push({ pos: end, kind: "end", id: `spell-${index}`, type: "spell", index });
  }
  if (!markers.length) return `${escapeHtml(text)}\n`;

  markers.sort((a, b) => {
    if (a.pos !== b.pos) return a.pos - b.pos;
    if (a.kind !== b.kind) return a.kind === "end" ? -1 : 1;
    return 0;
  });

  let html = "";
  let cursor = 0;
  const openEvidence: Marker[] = [];
  const openSpell: Marker[] = [];

  const openTags = () => {
    const spell = openSpell.at(-1);
    const evidenceMark = openEvidence.at(-1);
    let out = "";
    if (spell) out += `<span class="spell-error" data-spell-index="${spell.index}">`;
    if (evidenceMark) {
      const span = evidence[evidenceMark.index]!;
      const flash = flashId != null && span.id === flashId ? " is-flash" : "";
      const categoryClass = span.source === "category" ? " category-evidence" : "";
      out += `<span class="autofill-evidence${categoryClass}${flash}" data-evidence-id="${escapeHtml(span.id)}" data-evidence-index="${evidenceMark.index}">`;
    }
    return out;
  };
  const closeTags = () => {
    let out = "";
    if (openEvidence.length) out += "</span>";
    if (openSpell.length) out += "</span>";
    return out;
  };

  for (const marker of markers) {
    if (marker.pos > cursor) {
      html += openTags();
      html += escapeHtml(text.slice(cursor, marker.pos));
      html += closeTags();
      cursor = marker.pos;
    }
    if (marker.kind === "start") {
      if (marker.type === "evidence") openEvidence.push(marker);
      else openSpell.push(marker);
    } else if (marker.type === "evidence") {
      const index = openEvidence.findLastIndex((item) => item.id === marker.id);
      if (index >= 0) openEvidence.splice(index, 1);
    } else {
      const index = openSpell.findLastIndex((item) => item.id === marker.id);
      if (index >= 0) openSpell.splice(index, 1);
    }
  }
  if (cursor < text.length) {
    html += openTags();
    html += escapeHtml(text.slice(cursor));
    html += closeTags();
  }
  return `${html}\n`;
}
