"""Каталог КТРУ: распознавание товара по названию, сужение кодов, следующий вопрос."""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from . import units as U
from .schema import Characteristic, KtruCandidate, build_form, value_key
from .text import lemmatize, tokenize

DEFAULT_CATALOG = Path(__file__).resolve().parents[2] / "data-archived" / "ktru_catalog.json"
REAL_CATALOG = Path(__file__).resolve().parents[2] / "data-archived" / "ktru_real.json"

NAME_MIN_SCORE = 0.5  # ниже — название не распознано, сужаем только по характеристикам
NAME_KEEP_DELTA = 0.08  # семейства в пределах delta от лучшего остаются кандидатами
NAME_FUZZY = 0.88


@dataclass
class Family:
    """Позиции КТРУ с одним названием: «Бумага для офисной техники белая» и её синонимы."""

    name: str
    phrases: list[list[str]]  # леммы названия и синонимов
    positions: list[KtruCandidate]


def _phrase_score(phrase: list[str], query: set[str]) -> float:
    """Доля слов фразы в запросе (главное слово — первое — весит вдвое) с бонусом за длину.

    Бонус нужен, чтобы «стул школьный» (2 слова) обгонял голое «стул».
    """
    if not phrase:
        return 0.0
    weights = [2.0] + [1.0] * (len(phrase) - 1)
    found = 0.0
    hits = 0
    for w, lemma in zip(weights, phrase):
        ok = lemma in query or (
            len(lemma) >= 4 and any(len(q) >= 4 and fuzz.ratio(lemma, q) / 100 >= NAME_FUZZY for q in query)
        )
        if ok:
            found += w
            hits += 1
    return found / sum(weights) * (1 + 0.1 * hits)


class Catalog:
    """`retriever` (дерево ОКПД2) заменяет сопоставление названий по синонимам —
    для настоящего КТРУ, где синонимов нет, а позиций десятки тысяч."""

    def __init__(self, positions: list[KtruCandidate], retriever: Any = None):
        self.positions = positions
        self.retriever = retriever
        by_name: dict[str, Family] = {}
        for p in positions:
            fam = by_name.get(p.name)
            if fam is None:
                phrases = [lemmatize(s).split() for s in [p.name, *p.synonyms]]
                fam = by_name[p.name] = Family(p.name, [ph for ph in phrases if ph], [])
            fam.positions.append(p)
        self.families = list(by_name.values())

    @classmethod
    def load(cls, path: str | Path = DEFAULT_CATALOG) -> Catalog:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls([KtruCandidate.from_dict(d) for d in data])

    @classmethod
    def load_real(cls, embedder: Any, path: str | Path = REAL_CATALOG) -> Catalog:
        """Настоящий КТРУ (`ktru_import`) с поиском товара по дереву ОКПД2."""
        from .hierarchy import OkpdTree

        cat = cls.load(path)
        cat.retriever = OkpdTree.load(cat.positions, embedder, path_mode="raw", alpha=0.0, beam=10)
        return cat

    @classmethod
    def of(cls, candidates: list[KtruCandidate] | list[dict]) -> Catalog:
        return cls([c if isinstance(c, KtruCandidate) else KtruCandidate.from_dict(c) for c in candidates])

    # --- словари --------------------------------------------------------

    @cached_property
    def vocab(self) -> dict[str, str]:
        """Словоформа → лемма для исправления опечаток: названия, синонимы, характеристики, значения."""
        texts: set[str] = set()
        for fam in self.families:
            p = fam.positions[0]
            texts.update([p.name, *p.synonyms])
        for ch in build_form(self.positions):
            texts.add(ch.name)
            texts.update(ch.values)
            texts.update(s for syn in ch.synonyms.values() for s in syn)
        vocab: dict[str, str] = {}
        for t in (t for s in texts for t in tokenize(s)):
            if t.surface.isalpha() and len(t.surface) >= 4:
                vocab.setdefault(t.surface, t.lemma)
                vocab.setdefault(t.lemma, t.lemma)
        return vocab

    @cached_property
    def unit_words(self) -> set[str]:
        """Все написания единиц, известные каталогу — чтобы понимать, что слово после числа — единица."""
        return {U.unit_key(u) for p in self.positions for ch in p.characteristics for u in ch.units}

    def is_unit(self, written: str) -> bool:
        return U.lookup(written) is not None or U.unit_key(written) in self.unit_words

    # --- шаг «название» ---------------------------------------------------

    def match_name(self, query_lemmas: set[str]) -> tuple[list[KtruCandidate], list[tuple[str, float]]]:
        """Позиции семейств, чьё название/синоним лучше всего покрыто словами запроса."""
        scored = sorted(
            ((max(_phrase_score(ph, query_lemmas) for ph in fam.phrases), fam) for fam in self.families),
            key=lambda x: -x[0],
        )
        if not scored or scored[0][0] < NAME_MIN_SCORE:
            return list(self.positions), []
        best = scored[0][0]
        kept = [(s, f) for s, f in scored if s >= best - NAME_KEEP_DELTA]
        return [p for _, f in kept for p in f.positions], [(f.name, round(s, 3)) for s, f in kept]


# --- сужение и следующий вопрос ---------------------------------------------


def narrow(cands: list[KtruCandidate], char_name: str, value: Any) -> list[KtruCandidate]:
    return [c for c in cands if c.accepts(char_name, value)]


def _signature(ch: Characteristic | None) -> Any:
    if ch is None:
        return None
    if ch.type == "enum":
        return tuple(sorted(value_key(v) for v in ch.values))
    return ch.range


def _fmt_option(ch: Characteristic) -> str:
    if ch.type == "enum":
        return " / ".join(ch.values) if ch.values else "любое"
    lo, hi = ch.range or (None, None)
    unit = f" {ch.unit}" if ch.unit else ""
    if lo is None:
        return "любое"
    fmt = lambda x: f"{x:g}"  # noqa: E731
    return f"{fmt(lo)}{unit}" if lo == hi else f"{fmt(lo)}–{fmt(hi)}{unit}"


def next_question(cands: list[KtruCandidate], filled: set[str]) -> dict[str, Any] | None:
    """Незаполненная характеристика, лучше всего делящая оставшиеся коды (макс. энтропия)."""
    if len(cands) <= 1:
        return None
    best = None
    for ch in build_form(cands):
        if ch.name in filled:
            continue
        groups = Counter(_signature(c.char(ch.name)) for c in cands)
        if len(groups) < 2:
            continue
        n = len(cands)
        entropy = -sum(k / n * math.log2(k / n) for k in groups.values())
        if best is None or entropy > best[0]:
            options: dict[str, int] = {}
            for c in cands:
                own = c.char(ch.name)
                if own is not None:
                    key = _fmt_option(own)
                    options[key] = options.get(key, 0) + 1
            best = (entropy, ch.name, options)
    if best is None:
        return None
    return {"характеристика": best[1], "варианты": best[2]}
