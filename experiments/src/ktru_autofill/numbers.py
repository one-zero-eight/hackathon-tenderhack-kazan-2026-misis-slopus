"""Числа в тексте: «80 грамм», «80г/м²», «15,6"», «1200х600х750 мм»."""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from . import units as U
from .schema import Characteristic

_NUM = r"\d+(?:[.,]\d+)?"
_UNIT = r'%|["″]|[a-zа-яё°]+\.?(?:\s?/\s?[a-zа-яё.]+[²³23]?)?'

# Число не приклеено к букве слева — иначе «А4» дало бы 4.
_NUM_UNIT_RE = re.compile(
    rf"(?<![\w.,])(?P<num>{_NUM})(?:\s*(?P<unit>{_UNIT}))?(?![\w])", re.IGNORECASE
)
_DIMS_RE = re.compile(
    rf"(?<![\w.,])(?P<a>{_NUM})\s*[xх×*]\s*(?P<b>{_NUM})(?:\s*[xх×*]\s*(?P<c>{_NUM}))?"
    rf"(?:\s*(?P<unit>мм|см|м)\.?)?(?![\w])",
    re.IGNORECASE,
)

# «двухместная», «трёхслойная», «4-х секционная» — число в составе слова.
_NUMERAL_PREFIXES = {
    "одно": 1, "двух": 2, "дву": 2, "трех": 3, "трёх": 3, "четырех": 4, "четырёх": 4,
    "пяти": 5, "шести": 6, "семи": 7, "восьми": 8, "десяти": 10,
}
_NUMERAL_RE = re.compile(
    rf"(?<![\w])(?:(?P<word>{'|'.join(sorted(_NUMERAL_PREFIXES, key=len, reverse=True))})"
    r"|(?P<digit>\d+)\s*-?\s*х?\s*-?\s*)(?P<unit>[а-яё]{4,})(?![\w])",
    re.IGNORECASE,
)

UNIT_FUZZY_THRESHOLD = 0.85


@dataclass
class NumberMention:
    value: float
    unit_raw: str | None  # единица как написано
    start: int
    num_end: int  # конец числа
    end: int  # конец единицы (или числа, если единицы нет)
    dims: tuple[float, ...] = ()  # для «1200х600х750»
    compound: bool = False  # число внутри слова: «двухместная»

    @property
    def number(self) -> int | float:
        return _num(self.value)


def _num(v: float) -> int | float:
    return int(v) if float(v).is_integer() else v


def _f(s: str) -> float:
    return float(s.replace(",", "."))


def extract_numbers(text: str) -> list[NumberMention]:
    out = []
    masked = text
    for m in _DIMS_RE.finditer(text):
        dims = tuple(_f(g) for g in (m.group("a"), m.group("b"), m.group("c")) if g)
        out.append(NumberMention(dims[0], m.group("unit"), m.start(), m.end("b"), m.end(), dims))
        masked = masked[: m.start()] + " " * (m.end() - m.start()) + masked[m.end() :]
    for m in _NUMERAL_RE.finditer(masked):
        if m.group("digit") and not re.search(r"[-х]", m.group(0)[len(m.group("digit")) :]):
            continue  # «80 грамм» — обычное число с единицей, разберём ниже
        value = _NUMERAL_PREFIXES[m.group("word").lower()] if m.group("word") else _f(m.group("digit"))
        out.append(NumberMention(value, m.group("unit"), m.start(), m.start("unit"), m.end(), compound=True))
        masked = masked[: m.start()] + " " * (m.end() - m.start()) + masked[m.end() :]
    for m in _NUM_UNIT_RE.finditer(masked):
        out.append(NumberMention(_f(m.group("num")), m.group("unit"), m.start(), m.end("num"), m.end()))
    return sorted(out, key=lambda m: m.start)


def unit_match(unit_raw: str, ch: Characteristic) -> tuple[float, float | None]:
    """(оценка, множитель к единице характеристики); (0, None) — единица не подходит.

    1.0 — написание из `unit_aliases` характеристики («грамм» у плотности);
    0.95 — та же величина в общем справочнике, с пересчётом («1,2 м» → мм).
    """
    key = U.unit_key(unit_raw)
    for u in ch.units:
        if key == U.unit_key(u):
            return 1.0, 1.0
    src, dst = U.lookup(unit_raw), U.canonical(ch.unit)
    if src and dst:
        conv = U.convert(1.0, src, dst)
        if conv is not None:
            return (0.95 if src.dim == dst.dim else 0.8), conv
    # Опечатка в длинной единице: «граммов» ~ «грамм» уже лемма, а «миллиметор» — нет.
    for u in ch.units:
        cand = U.unit_key(u)
        if min(len(key), len(cand)) >= 4 and fuzz.ratio(key, cand) / 100 >= UNIT_FUZZY_THRESHOLD:
            return 0.85, 1.0
    return 0.0, None


def number_value(ch: Characteristic, number: float, unit_raw: str | None) -> int | float | dict:
    """Значение в виде для ответа: счётные — число, остальные — с канонической единицей."""
    number = _num(round(number, 6))
    if ch.type == "count":
        return number
    return {"значение": number, "единица": ch.unit, "как_написано": unit_raw}


def product_text(text: str) -> str:
    """Текст без чисел и единиц — для поиска товара: «монитор 27" IPS» → «монитор IPS».
    Числа описывают характеристики, а в эмбеддинге названия только сбивают."""
    out = text
    for m in sorted(extract_numbers(text), key=lambda m: -m.start):
        out = out[: m.start] + " " + out[m.end :]
    return " ".join(out.split()) or text
