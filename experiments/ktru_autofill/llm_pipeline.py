"""Пайплайн C: соседи-СТЕ → LLM выбирает схему → LLM извлекает поля → Jev оценивает правдоподобие → проверка кодом.

    поиск          NeighborSearch  ближайшие СТЕ → позиции КТРУ, сгруппированные по названию (это «схемы»)
    схема          LLM             выбирает одну из найденных схем (или «ни одна»)
    поля           LLM             значения полей схемы + дополнительные поля, которых в схеме нет; без регулярок
    правдоподобие  Jev (Choice)    «значение верно / неверно» по каждому полю и по схеме в целом
    проверка       код             перечень, диапазон «больше/меньше», число и цитата есть в тексте, единицы
                                   переводит `units` (не вышло — LLM), расхождение кода и LLM помечается
    классификация  принято / сомнительно / отклонено по каждому полю; принятые сужают коды (`narrow_result`)

Число и единицу LLM переписывает из текста как есть, пересчёт в единицу характеристики — код, а не модель.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable

from . import units as U
from .autofill import Step, _Candidate, narrow_result
from .jev_autofill import Outcome
from .jev_client import ROOT, JevClient
from .jev_extract import STRONG, WEAK
from .ktru_store import KtruStore
from .numbers import number_value, unit_match
from .schema import Characteristic, KtruCandidate, build_form, value_key

Emit = Callable[[str, dict], None]
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")
MAX_SCHEMAS = 8
MAX_ATTEMPTS = 3
PAREN_RE = re.compile(r"\s*\([^)]*\)")
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
ACCEPTED, DOUBTFUL, REJECTED = "принято", "сомнительно", "отклонено"


class LlmError(RuntimeError):
    """LLM недоступна или ответила не JSON."""


def openai_key(env_file=ROOT / ".env") -> str | None:
    if key := os.environ.get("OPENAI_API_KEY"):
        return key
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "OPENAI_API_KEY" and value.strip():
                return value.strip().strip("'\"")
    return None


class LlmClient:
    """JSON-ответы LLM с кешем и счётчиками. `call(messages) -> str` подменяется в тестах."""

    def __init__(self, call: Callable[[list[dict[str, str]]], str], cache_size: int = 2048):
        self.call, self._cache, self._cache_size, self._lock = call, OrderedDict(), cache_size, threading.Lock()
        self.stats = {"запросов": 0, "из кеша": 0}

    @classmethod
    def from_env(cls, model: str = LLM_MODEL) -> LlmClient:
        key = openai_key()
        if not key:
            raise LlmError("Нет OPENAI_API_KEY ни в окружении, ни в .env")
        from openai import OpenAI

        client = OpenAI(api_key=key, timeout=60)

        def call(messages: list[dict[str, str]]) -> str:
            r = client.chat.completions.create(model=model, messages=messages, temperature=0,
                                               response_format={"type": "json_object"})
            return r.choices[0].message.content or ""

        return cls(call)

    def ask(self, system: str, user: str) -> dict[str, Any]:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        key = hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode()).hexdigest()
        with self._lock:
            if key in self._cache:
                self.stats["из кеша"] += 1
                return self._cache[key]
        try:
            data = json.loads(self.call(messages))
        except json.JSONDecodeError as e:
            raise LlmError(f"LLM вернула не JSON: {e}") from e
        except Exception as e:
            raise LlmError(f"LLM: {e}") from e
        if not isinstance(data, dict):
            raise LlmError("LLM вернула не объект JSON")
        with self._lock:
            self.stats["запросов"] += 1
            self._cache[key] = data
            if len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return data


# ---------- схемы ----------

@dataclass
class Schema:
    name: str
    rows: list[int]
    form: list[Characteristic]

    def brief(self) -> str:
        parts = []
        for ch in self.form[:12]:
            if ch.type == "enum":
                parts.append(f"{ch.name} ({', '.join(ch.values[:5])}{'…' if len(ch.values) > 5 else ''})")
            else:
                parts.append(f"{ch.name} [{ch.unit or 'число'}]")
        return "; ".join(parts) or "без характеристик"


def group_schemas(store: KtruStore, rows: list[int]) -> list[Schema]:
    """Позиции КТРУ по названию: у одного товара несколько позиций с разными наборами значений — одна схема."""
    groups: dict[str, list[int]] = {}
    for r in rows:
        groups.setdefault(store.names[r], []).append(r)
    schemas = [Schema(n, rs, build_form(store.candidates(rs))) for n, rs in groups.items()]
    return sorted(schemas, key=lambda s: -len(s.rows))[:MAX_SCHEMAS]


SCHEMA_SYSTEM = ("Ты сопоставляешь описание товара со схемой позиции каталога КТРУ. Выбери ОДНУ схему, чьё название и "
                 "набор характеристик подходят товару. Если ни одна не подходит — null. "
                 'Ответ JSON: {"схема": <номер из списка или null>, "причина": "<кратко>"}')


def choose_schema(llm: LlmClient, text: str, schemas: list[Schema]) -> tuple[Schema | None, str]:
    if len(schemas) == 1:
        return schemas[0], "единственная схема"
    listing = "\n".join(f"{i}. {s.name} — {s.brief()}" for i, s in enumerate(schemas, 1))
    answer = llm.ask(SCHEMA_SYSTEM, f"Товар: {text}\n\nСхемы:\n{listing}")
    n = answer.get("схема")
    if isinstance(n, str) and n.strip().isdigit():
        n = int(n)
    if isinstance(n, int) and 1 <= n <= len(schemas):
        return schemas[n - 1], str(answer.get("причина") or "")
    return None, str(answer.get("причина") or "")


# ---------- извлечение ----------

EXTRACT_SYSTEM = (
    "Ты извлекаешь характеристики товара из его описания по схеме. Для каждой характеристики схемы укажи значение, "
    "если оно есть в тексте; иначе не включай её. Перечень: значение берётся из допустимых, дословно. Число: перепиши "
    "число и единицу из текста как написано, не пересчитывай и не округляй. К каждому значению дай короткую цитату из "
    "текста. Если в тексте есть важные характеристики товара, которых нет в схеме, добавь их в «дополнительные» "
    "(значение, единица, цитата). Не выдумывай то, чего нет в тексте. "
    'Ответ JSON: {"поля":[{"характеристика":"","значение":"","единица":null,"цитата":""}],'
    '"дополнительные":[{"характеристика":"","значение":"","единица":null,"цитата":""}]}')


def describe_form(form: list[Characteristic]) -> str:
    lines = []
    for ch in form:
        if ch.type == "enum":
            lines.append(f"- {ch.name} (перечень: {'; '.join(ch.values[:40]) or 'свободное значение'})")
        else:
            bound = ""
            if ch.range and not all(abs(x) == float("inf") for x in ch.range):
                bound = f", допустимо {ch.range[0]:g}…{ch.range[1]:g}"
            lines.append(f"- {ch.name} ({'число' if ch.type == 'number' else 'количество'}, единица: {ch.unit or '—'}{bound})")
    return "\n".join(lines)


@dataclass
class Field:
    name: str
    raw: Any  # как вернула LLM
    unit_raw: str | None
    quote: str
    extra: bool = False
    value: Any = None  # после проверки кодом (приведённое к схеме)
    plausibility: float | None = None
    checks: list[str] = field(default_factory=list)  # что нашла проверка
    status: str = REJECTED
    converted: str | None = None  # чем переведено: «код» / «LLM» / «без пересчёта»

    def to_dict(self) -> dict[str, Any]:
        return {"характеристика": self.name, "значение": self.value if self.value is not None else self.raw,
                "как_написано": self.raw, "единица": self.unit_raw, "цитата": self.quote,
                "дополнительная": self.extra, "правдоподобие": self.plausibility, "статус": self.status,
                "проверка": self.checks, "перевод": self.converted}


def extract_fields(llm: LlmClient, text: str, schema: Schema) -> list[Field]:
    answer = llm.ask(EXTRACT_SYSTEM, f"Товар: {text}\n\nСхема «{schema.name}»:\n{describe_form(schema.form)}")
    fields: list[Field] = []
    for key, extra in (("поля", False), ("дополнительные", True)):
        for item in answer.get(key) or []:
            if not isinstance(item, dict) or not item.get("характеристика") or item.get("значение") in (None, ""):
                continue
            unit = item.get("единица")
            fields.append(Field(str(item["характеристика"]).strip(), item["значение"],
                                str(unit).strip() if unit not in (None, "") else None, str(item.get("цитата") or "").strip(), extra))
    return fields


# ---------- проверка кодом ----------

def to_number(raw: Any) -> float | None:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    m = NUMBER_RE.search(str(raw).replace(" ", " ").replace(" ", "")) if raw is not None else None
    return float(m.group().replace(",", ".")) if m else None


def number_in_text(number: float, text: str) -> bool:
    """Число (в любой записи — «1,5», «1.5», «1 500») встречается в тексте."""
    flat = text.replace(" ", " ")
    for m in re.finditer(r"\d+(?:[ ]\d{3})*(?:[.,]\d+)?", flat):
        lit = m.group().replace(" ", "").replace(",", ".")
        try:
            if abs(float(lit) - number) < 1e-9:
                return True
        except ValueError:
            continue
    return any(abs(float(x.replace(",", ".")) - number) < 1e-9 for x in NUMBER_RE.findall(flat))


CONVERT_SYSTEM = ('Переведи значение из одной единицы измерения в другую; пояснение в скобках — лишь уточнение единицы, '
                  'а не новая единица. Ответ JSON: {"значение": <число>}.')


def convert_value(ch: Characteristic, number: float, unit_raw: str | None, llm: LlmClient | None
                  ) -> tuple[float | None, str, str | None]:
    """(число в единице характеристики, чем переведено, замечание). Сначала код; нет таблицы — LLM; оба — сверка."""
    if not unit_raw or not ch.unit:
        return number, "без пересчёта", None
    _, factor = unit_match(unit_raw, ch)
    by_code = number * factor if factor is not None else None
    if by_code is None:
        src, dst = U.lookup(unit_raw), U.canonical(PAREN_RE.sub("", ch.unit).strip())  # «Дюйм (25,4 мм)» → «Дюйм»
        if src and dst:
            by_code = U.convert(number, src, dst)
    by_llm = None
    if llm is not None and (by_code is None or os.environ.get("LLM_CHECK_CONVERT")):
        try:
            by_llm = to_number(llm.ask(CONVERT_SYSTEM, f"{number:g} {unit_raw} → {ch.unit}").get("значение"))
        except LlmError:
            by_llm = None
    if by_code is not None and by_llm is not None and abs(by_code - by_llm) > 1e-6 * max(1, abs(by_code)):
        return by_code, "код", f"код и LLM расходятся: {by_code:g} против {by_llm:g} {ch.unit}"
    if by_code is not None:
        return by_code, "код", None
    if by_llm is not None:
        return by_llm, "LLM", f"единица «{unit_raw}» переведена моделью, таблицы нет"
    return None, "нет", f"единицу «{unit_raw}» не удалось перевести в «{ch.unit}»"


def check_field(f: Field, ch: Characteristic | None, text: str, llm: LlmClient | None = None) -> bool:
    """Заполняет f.value и f.checks; True — проверка кодом пройдена без отказов."""
    ok = True
    if f.quote and f.quote.lower().replace("ё", "е") not in text.lower().replace("ё", "е") and \
            difflib.SequenceMatcher(None, f.quote.lower(), text.lower()).find_longest_match(0, len(f.quote), 0, len(text)).size < 0.8 * len(f.quote):
        f.checks.append("цитаты нет в тексте")
        ok = False
    if ch is None:  # дополнительное поле: значение должно опираться на текст
        number = to_number(f.raw)
        if number is not None and isinstance(f.raw, (int, float, str)) and any(c.isdigit() for c in str(f.raw)) \
                and not number_in_text(number, text):
            f.checks.append("числа нет в тексте")
            ok = False
        elif number is None and value_key(f.raw) and not set(value_key(f.raw).split()) & set(value_key(text).split()):
            f.checks.append("значения нет в тексте")
            ok = False
        f.value = f.raw
        return ok
    if ch.type == "enum":
        keys = {value_key(v): v for v in ch.values}
        key = value_key(f.raw)
        if not ch.values:
            f.value = f.raw
        elif key in keys:
            f.value = keys[key]
        else:
            close = difflib.get_close_matches(key, list(keys), n=1, cutoff=0.85)
            if close:
                f.value = keys[close[0]]
                f.checks.append(f"«{f.raw}» заменено ближайшим значением перечня «{f.value}»")
            else:
                f.checks.append(f"«{f.raw}» нет в перечне допустимых значений")
                ok = False
        return ok
    number = to_number(f.raw)
    if number is None:
        f.checks.append(f"«{f.raw}» не число")
        return False
    if not number_in_text(number, text):
        f.checks.append(f"числа {number:g} нет в тексте")
        ok = False
    converted, how, note = convert_value(ch, number, f.unit_raw, llm)
    f.converted = how
    if note:
        f.checks.append(note)
    if converted is None:
        return False
    if not ch.accepts(converted):
        lo, hi = ch.range if ch.range else (None, None)
        f.checks.append(f"{converted:g} {ch.unit or ''} вне допустимого диапазона {lo:g}…{hi:g}".replace("-inf", "−∞").replace("inf", "∞"))
        ok = False
    f.value = number_value(ch, converted, f.unit_raw)
    return ok


# ---------- правдоподобие (Jev) ----------

def plausibility_questions(fields: list[Field], schema: Schema) -> dict[str, Any]:
    questions: dict[str, Any] = {"schema": {"type": "choice", "instructions": {
        "question": "Подходит ли позиция каталога `схема` к `товар`?"},
        "criteria": {"подходит": "Название и назначение схемы соответствуют товару",
                     "не подходит": "Схема описывает другой товар"}}}
    for i, f in enumerate(fields):
        questions[f"f{i}"] = {"type": "choice", "instructions": {
            "характеристика": f.name, "значение": f"{f.raw} {f.unit_raw or ''}".strip(), "цитата": f.quote,
            "question": "Верно ли, что у `товар` характеристика `характеристика` равна `значение`?"},
            "criteria": {"верно": "Текст прямо подтверждает это значение этой характеристики",
                         "неверно": "Значение выдумано, относится к другой характеристике или противоречит тексту"}}
    return questions


def probability(answer: dict[str, Any], good: str) -> float:
    probs = answer.get("probabilities") or {}
    if good in probs:
        return float(probs[good])
    return float(answer.get("confidence", 0.0)) if answer.get("choice") == good else 0.0


def classify(f: Field, code_ok: bool) -> str:
    p = f.plausibility if f.plausibility is not None else 0.0
    if not code_ok or p < WEAK:
        return REJECTED
    if p < STRONG or any("расходятся" in c or "заменено" in c or "моделью" in c for c in f.checks):
        return DOUBTFUL
    return ACCEPTED


def to_candidate(f: Field, ch: Characteristic, text: str) -> _Candidate:
    pos = text.lower().find(f.quote.lower()) if f.quote else -1
    return _Candidate(ch, f.value, f.quote, "llm+jev" if f.status == ACCEPTED else "подсказка", f.plausibility or 0.0,
                      frozenset(), pos if pos >= 0 else 10 ** 6, warning="; ".join(f.checks) or None)


# ---------- пайплайн ----------

@dataclass
class PipelineOutcome:
    outcome: Outcome
    schema: str | None
    schema_plausibility: float | None
    fields: list[Field]
    verdict: str  # итоговая классификация ответа
    notes: list[str] = field(default_factory=list)

    def to_dict(self, details: bool = False) -> dict[str, Any]:
        out = self.outcome.to_dict(details=details)
        out["схема"] = {"название": self.schema, "правдоподобие": self.schema_plausibility}
        out["поля"] = [f.to_dict() for f in self.fields]
        out["итог"] = self.verdict
        out["заметки"] = self.notes
        return out


class LlmPipeline:
    def __init__(self, store: KtruStore, jev: JevClient, llm: LlmClient, find: Callable[..., Any]):
        """`find(text, emit) -> SearchResult` — поиск по соседям-СТЕ (`NeighborSearch.find`)."""
        self.store, self.jev, self.llm, self.find = store, jev, llm, find

    def run(self, text: str, emit: Emit | None = None) -> PipelineOutcome:
        started, before = time.perf_counter(), dict(self.jev.stats)
        emit = emit or (lambda *_: None)
        found = self.find(text, emit)
        steps = [Step("весь каталог", len(self.store))] + [Step(d, n) for d, n in found.steps]
        notes = list(found.warnings)

        def finish(rows, families, chosen, schema_name, schema_p, fields, verdict) -> PipelineOutcome:
            cands = self.store.candidates(rows)
            result = narrow_result(cands, steps, chosen, [], families)
            used = {k: self.jev.stats[k] - before[k] for k in before}
            out = Outcome(result, rows, families, notes, steps[:], time.perf_counter() - started, used)
            return PipelineOutcome(out, schema_name, schema_p, fields, verdict, notes)

        if not found.rows:
            return finish([], [], [], None, None, [], REJECTED)
        schemas = group_schemas(self.store, found.rows)
        emit("схемы", {"найдено": [s.name for s in schemas]})
        remaining, schema, schema_p, fields, chosen = list(schemas), None, None, [], []
        for attempt in range(MAX_ATTEMPTS):  # схема, которую Jev отверг, заменяется следующей по выбору LLM
            schema, why = choose_schema(self.llm, text, remaining)
            if schema is None:
                notes.append(f"LLM не выбрала схему: {why}")
                break
            emit("схема", {"название": schema.name, "причина": why, "попытка": attempt + 1})
            fields = extract_fields(self.llm, text, schema)
            by_name = {ch.name.lower(): ch for ch in schema.form}
            for f in fields:  # поле схемы, названное иначе, считается дополнительным
                if f.name.lower() not in by_name:
                    f.extra = True
            checked = [(f, check_field(f, None if f.extra else by_name[f.name.lower()], text, self.llm)) for f in fields]
            verdicts = self.jev.ask({"товар": text, "схема": schema.name}, plausibility_questions(fields, schema))
            schema_p = probability(verdicts["schema"], "подходит") if "schema" in verdicts else None
            chosen = []
            for i, (f, ok) in enumerate(checked):
                f.plausibility = round(probability(verdicts[f"f{i}"], "верно"), 3) if f"f{i}" in verdicts else None
                f.status = classify(f, ok)
                if not f.extra and f.status != REJECTED:
                    chosen.append(to_candidate(f, by_name[f.name.lower()], text))
            if schema_p is None or schema_p >= WEAK:
                break
            notes.append(f"Jev считает схему «{schema.name}» неподходящей")
            remaining = [x for x in remaining if x is not schema]
            if not remaining or attempt == MAX_ATTEMPTS - 1:
                break
        if schema is None:
            return finish(found.rows, found.families, [], None, None, [], DOUBTFUL)
        for f in fields:
            emit("поле", f.to_dict())
        rows = schema.rows
        statuses = [f.status for f in fields if not f.extra]
        if schema_p is not None and schema_p < WEAK:
            verdict, chosen, rows = REJECTED, [], found.rows  # значения отвергнутой схемы коды не сужают
        elif statuses and all(s == ACCEPTED for s in statuses):
            verdict = ACCEPTED
        elif ACCEPTED in statuses or DOUBTFUL in statuses:
            verdict = DOUBTFUL
        else:
            verdict = DOUBTFUL if schema_p is None or schema_p >= STRONG else REJECTED
        return finish(rows, found.families, chosen, schema.name, None if schema_p is None else round(schema_p, 3), fields, verdict)
