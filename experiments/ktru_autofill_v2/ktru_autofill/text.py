"""Подготовка текста: нормализация, лемматизация, стоп-слова, n-граммы."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

import pymorphy3

# Кириллические буквы, похожие на латинские. В кодах вроде «А4», «С3» их
# путают постоянно, а в КТРУ форматы записаны латиницей.
_LOOKALIKES = str.maketrans("авсенкмортху", "abcehkmoptxy")

# Стоп-слова убираются одинаково и из запроса, и из значений КТРУ, поэтому
# «для лазерной печати» и «для лазерного принтера» сравниваются как
# «лазерный печать» и «лазерный принтер». «Без» и «не» оставлены: они
# меняют смысл значения («без покрытия» ≠ «покрытие»).
STOP_WORDS = frozenset(
    """
    и в во на по с со к ко о об от до из за для при про через над под у а но
    или либо же ли бы то это этот эта эти тот та те весь все всё вся так как
    шт штука упаковка уп
    """.split()
)

_TOKEN_RE = re.compile(r"[a-zа-яё0-9]+(?:[.,/][a-zа-яё0-9²]+)*²?", re.IGNORECASE)

_morph = pymorphy3.MorphAnalyzer()


@dataclass(frozen=True)
class Token:
    surface: str  # как написано, в нижнем регистре
    lemma: str
    start: int
    end: int
    known: bool  # есть ли слово в словаре pymorphy3

    @property
    def has_digit(self) -> bool:
        return any(c.isdigit() for c in self.surface)


def normalize_code(s: str) -> str:
    """«А4» → «a4»: латинизирует смешанные буквенно-цифровые коды."""
    return s.strip().lower().replace("ё", "е").translate(_LOOKALIKES)


@lru_cache(maxsize=65536)
def _parse(word: str) -> tuple[str, bool]:
    if any(c.isdigit() for c in word) or not word.isalpha():
        return normalize_code(word), True
    return _morph.parse(word)[0].normal_form.replace("ё", "е"), _morph.word_is_known(word)


def tokenize(text: str) -> list[Token]:
    tokens = []
    for m in _TOKEN_RE.finditer(text):
        raw = m.group()
        surface = raw.lower()
        if len(raw) == 1 and raw.isupper():
            # Одиночная заглавная буква — код («класс А»), а не союз «а».
            lemma, known = normalize_code(raw), True
        else:
            lemma, known = _parse(surface)
        tokens.append(Token(surface, lemma, m.start(), m.end(), known))
    return tokens


def content_tokens(tokens: list[Token]) -> list[Token]:
    return [t for t in tokens if t.lemma not in STOP_WORDS]


def lemmatize(text: str) -> str:
    """Строка из лемм без стоп-слов — каноническая форма для сравнения."""
    return " ".join(t.lemma for t in content_tokens(tokenize(text)))


@dataclass(frozen=True)
class Fragment:
    tokens: tuple[Token, ...]

    @property
    def lemma(self) -> str:
        return " ".join(t.lemma for t in self.tokens)

    @property
    def surface(self) -> str:
        return " ".join(t.surface for t in self.tokens)

    @property
    def span(self) -> frozenset[int]:
        """Позиции токенов — чтобы один кусок текста не заполнил две характеристики."""
        return frozenset(t.start for t in self.tokens)

    @property
    def has_digit(self) -> bool:
        return any(t.has_digit for t in self.tokens)


def ngrams(tokens: list[Token], sizes: tuple[int, ...] = (1, 2, 3)) -> list[Fragment]:
    out = []
    for n in sizes:
        for i in range(len(tokens) - n + 1):
            out.append(Fragment(tuple(tokens[i : i + n])))
    return out
