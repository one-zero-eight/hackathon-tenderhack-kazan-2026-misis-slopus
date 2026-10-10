"""Извлечение значений характеристик без эмбеддингов: регулярки собирают числа, Jev выбирает значение.

Вместо нечёткого и векторного сопоставления из `autofill.py` (`_enum_candidates`, `_number_candidates`):
  перечень — Choice по допустимым значениям анкеты + «не указано»; Jev сам понимает опечатки и синонимы;
  число    — регулярки (`numbers.extract_numbers`) находят числа с единицами, размеры «1200х600х750 мм» режутся на
             компоненты; Jev выбирает, какое из чисел — значение характеристики. Число копирует код, цифры модель не
             пишет; единицы переводит `units`. Одинаковые числа в разных местах («450х450х450») — отдельные
             варианты «450 мм (вариант N)» с позицией в записи.
Вероятность выбора — оценка. Выше `strong` значение применяется и сужает коды, от `weak` до `strong` — только
подсказка пользователю (метод «подсказка»), ниже — не учитывается.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from typing import Any, Callable

from . import units as U
from .autofill import _Candidate
from .jev_client import JevClient
from .numbers import NumberMention, extract_numbers, number_value, unit_match
from .schema import Characteristic
from .text import lemmatize

NONE = "не указано"
MAX_OPTIONS = 250
# Порог 0,5: на 311 парах ответы выше него верны в 98,9%; ответы без опоры в тексте совпали с карточкой в 55 из 60.
STRONG = float(os.environ.get("JEV_STRONG", 0.5))
WEAK = float(os.environ.get("JEV_WEAK", 0.25))
VARIANT_RE = re.compile(r"\s*\(вариант \d+\)\s*$")
RULE_NUMBER = (
    "Выбирай по связи числа с характеристикой в исходном тексте. Фрагменты вариантов — цитаты, а не назначение "
    "числа характеристике. Используй явные подписи, включая Ш×Г×В и аналогичные обозначения. Не угадывай порядок "
    "неподписанных размеров по типичному товару. Если связь неоднозначна, выбирай «не указано». "
    "Не подменяй размер товара размером упаковки или отдельной детали.")
RULE_VARIANTS = (
    "Варианты вида «значение (вариант N)» — одно и то же число, встреченное в тексте несколько раз: N — порядковый "
    "номер вхождения этого значения в тексте слева направо (вариант 1 — первое вхождение, вариант 2 — второе и т. д.); "
    "точное место указано в «позиция в записи» и во фрагменте. Если характеристике соответствует это число, но неясно, "
    "какое именно из одинаковых вхождений к ней относится, это не повод выбирать «не указано»: у вариантов одно "
    "значение — выбери вхождение, которое подходит по позиции и подписи, а без подписи — любое из них. "
    "«Не указано» выбирай, только если число к характеристике не относится.")


def strip_variant(label: str) -> str:
    return VARIANT_RE.sub("", label)


def snippet(text: str, start: int, end: int, pad: int = 50) -> str:
    return ("…" if start > pad else "") + text[max(0, start - pad):end + pad].replace("\n", " ") + (
        "…" if end + pad < len(text) else "")


@dataclass
class NumberOption:
    label: str
    value: float
    unit_raw: str | None
    start: int
    end: int
    detail: str | None = None


def number_options(text: str) -> list[NumberOption]:
    """Числа текста как варианты; размеры «AхBхC» — по компоненту; одинаковые подписи нумеруются."""
    raw: list[NumberOption] = []
    for m in extract_numbers(text):
        unit = (m.unit_raw or "").strip() or None
        src = text[m.start:m.end].strip()
        if m.dims:
            n = len(m.dims)
            for i, x in enumerate(m.dims, 1):
                raw.append(NumberOption(f"{x:g} {unit or ''}".strip(), x, unit, m.start, m.end,
                                        f"{i}-е число из «{src}»"))
        else:
            raw.append(NumberOption(f"{m.value:g} {unit or ''}".strip(), m.value, unit, m.start, m.end,
                                    "число внутри слова «%s»" % src if m.compound else None))
    counts: dict[str, int] = {}
    for o in raw:
        counts[o.label] = counts.get(o.label, 0) + 1
    seen: dict[str, int] = {}
    out = []
    for o in raw:
        if counts[o.label] > 1:
            seen[o.label] = seen.get(o.label, 0) + 1
            o.label = f"{o.label} (вариант {seen[o.label]})"
        out.append(o)
    return out[:MAX_OPTIONS - 1]


def _bounds(ch: Characteristic) -> str | None:
    if ch.range is None:
        return None
    lo, hi = ch.range
    if math.isinf(lo) and math.isinf(hi):
        return None
    if math.isinf(hi):
        return f"≥ {lo:g}"
    if math.isinf(lo):
        return f"≤ {hi:g}"
    return f"от {lo:g} до {hi:g}"


def _context(ch: Characteristic) -> dict[str, Any]:
    ctx: dict[str, Any] = {"характеристика": ch.name}
    if ch.unit:
        ctx["единица"] = ch.unit
    if bound := _bounds(ch):
        ctx["допустимо"] = bound
    return ctx


def build_questions(text: str, form: list[Characteristic], options: list[NumberOption]
                    ) -> tuple[dict[str, Any], dict[str, Characteristic]]:
    """Вопрос Jev на каждую характеристику анкеты, у которой есть что выбирать. qid → характеристика."""
    questions: dict[str, Any] = {}
    chars: dict[str, Characteristic] = {}
    variants = any(VARIANT_RE.search(o.label) for o in options)
    for i, ch in enumerate(form):
        qid = f"c{i}"
        if ch.type == "enum":
            if not ch.values:
                continue
            values = ch.values[:MAX_OPTIONS - 1]
            questions[qid] = {"type": "choice", "instructions": _context(ch) | {
                "question": "Какое значение `характеристика` у товара, судя по `товар`?"},
                "criteria": dict.fromkeys(values) | {NONE: "В `товар` нет значения этой характеристики"}}
        elif options:
            criteria: dict[str, Any] = {}
            for o in options:
                fragment: dict[str, Any] = {"текст": snippet(text, o.start, o.end, 60)}
                if o.detail:
                    fragment["позиция в записи"] = o.detail
                criteria[o.label] = {"фрагменты": [fragment]}
            rule = RULE_NUMBER + (" " + RULE_VARIANTS if variants else "")
            questions[qid] = {"type": "choice", "instructions": _context(ch) | {
                "question": "Какое из чисел в `товар` — значение `характеристика`?", "правило": rule},
                "criteria": criteria | {NONE: "Нет числа, которое текст позволяет однозначно связать с этой характеристикой"}}
        else:
            continue
        chars[qid] = ch
    return questions, chars


def _same_kind(src: U.Unit | None, dst: U.Unit | None) -> bool:
    """Единицы несовместимы только если обе известны, величины разные и это не «плотность бумаги ~ масса»."""
    if not src or not dst or src.dim == dst.dim:
        return True
    return {src.dim, dst.dim} == {"paper_density", "mass"} or src.dim.startswith("count") or dst.dim.startswith("count")


def _position(text: str, value: Any) -> int:
    """Где в тексте упомянуто значение перечня — порядок сужения; нет в тексте — после всех упоминаний."""
    lemma = lemmatize(str(value))
    if lemma:
        words = lemmatize(text).split()
        first = lemma.split()[0]
        if first in words:
            return text.lower().find(first[:4]) if first[:4] in text.lower() else words.index(first)
    return 10 ** 6


def parse_answer(text: str, ch: Characteristic, answer: dict[str, Any], options: dict[str, NumberOption]
                 ) -> _Candidate | None:
    """Ответ Jev по одной характеристике → найденное значение (или None)."""
    label = answer["choice"]
    probs = answer.get("probabilities") or {}
    score = float(probs.get(label, answer.get("confidence", 0.0)))
    if label == NONE or score < WEAK:
        return None
    weak = score < STRONG
    if ch.type == "enum":
        return _Candidate(ch, label, "по смыслу текста", "подсказка" if weak else "jev", score, frozenset(),
                          _position(text, label))
    o = options.get(label)
    if o is None:
        return None
    warning = None
    number = o.value
    if o.unit_raw:
        _, factor = unit_match(o.unit_raw, ch)
        if factor is not None:
            number = o.value * factor
        elif not _same_kind(U.lookup(o.unit_raw), U.canonical(ch.unit)):
            warning = f"единица «{o.unit_raw}» не сопоставлена с «{ch.unit}»"
    value = number_value(ch, number, o.unit_raw)
    fragment = text[o.start:o.end].strip()
    if not ch.accepts(value):
        warning = warning or f"значение вне допустимого диапазона {_bounds(ch)}"
    return _Candidate(ch, value, fragment, "подсказка" if weak else "jev+число", score, frozenset(), o.start, warning=warning)


def extract(jev: JevClient, text: str, form: list[Characteristic],
            emit: Callable[[str, dict], None] | None = None) -> list[_Candidate]:
    """Найденные значения анкеты. emit("характеристика", …) — по мере ответов Jev."""
    options = number_options(text)
    questions, chars = build_questions(text, form, options)
    if not questions:
        return []
    by_label = {o.label: o for o in options}
    found: list[_Candidate] = []

    def done(batch_answers: dict[str, dict[str, Any]]) -> None:
        for qid, answer in batch_answers.items():
            cand = parse_answer(text, chars[qid], answer, by_label)
            if cand:
                found.append(cand)
            if emit:
                emit("характеристика", {"характеристика": chars[qid].name,
                                        "значение": cand.value if cand else None,
                                        "оценка": round(cand.score, 3) if cand else None,
                                        "метод": cand.method if cand else None})

    jev.ask_many([({"товар": text}, questions)], on_batch=lambda _, a: done(a))
    return found
