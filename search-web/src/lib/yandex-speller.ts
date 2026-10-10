/** Yandex Speller JSON API — https://yandex.ru/dev/speller/doc/ru/ */

export type SpellerError = {
  code: number;
  pos: number;
  row: number;
  col: number;
  len: number;
  word: string;
  s: string[];
};

/** IGNORE_DIGITS | IGNORE_URLS | IGNORE_CAPITALIZATION */
const DEFAULT_OPTIONS = 2 | 4 | 512;

const SPELLER_URL = "https://speller.yandex.net/services/spellservice.json/checkText";

const ERROR_LABELS: Record<number, string> = {
  1: "Возможная опечатка",
  2: "Повтор слова",
  3: "Регистр",
  4: "Слишком много ошибок",
};

export function spellerErrorLabel(code: number): string {
  return ERROR_LABELS[code] ?? "Орфография";
}

export async function checkSpelling(
  text: string,
  signal?: AbortSignal,
): Promise<SpellerError[]> {
  const trimmed = text.trim();
  if (!trimmed) return [];

  const body = new URLSearchParams({
    text,
    lang: "ru,en",
    options: String(DEFAULT_OPTIONS),
    format: "plain",
  });

  const res = await fetch(SPELLER_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8" },
    body,
    signal,
  });
  if (!res.ok) throw new Error(`Speller HTTP ${res.status}`);
  return (await res.json()) as SpellerError[];
}

function escapeHtml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

/** Mirror text with error spans; trailing \\n matches textarea layout. */
export function highlightSpellingErrors(text: string, errors: SpellerError[]): string {
  if (!text) return "\n";
  if (!errors.length) return `${escapeHtml(text)}\n`;

  const indexed = errors
    .map((err, index) => ({ err, index }))
    .sort((a, b) => a.err.pos - b.err.pos);
  let html = "";
  let cursor = 0;
  for (const { err, index } of indexed) {
    if (err.pos < cursor || err.len <= 0) continue;
    const end = Math.min(err.pos + err.len, text.length);
    if (err.pos >= text.length) break;
    html += escapeHtml(text.slice(cursor, err.pos));
    html += `<span class="spell-error" data-spell-index="${index}">${escapeHtml(text.slice(err.pos, end))}</span>`;
    cursor = end;
  }
  html += escapeHtml(text.slice(cursor));
  return `${html}\n`;
}
