from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

import pymorphy3

# Keep technical separators, signs, units and model numbers meaningful. Only
# cosmetic punctuation is discarded; tokens are never sorted or fuzzy-matched.
_COSMETIC = re.compile(r'[\s_,;:!?"\'«»„“”()\[\]{}]+')
_CYRILLIC_WORD = re.compile(r"[а-яё]+", re.IGNORECASE)
_NUMBER = re.compile(r"(?<![\w.])[+-]?\d+(?:[.,]\d+)?(?![\w.]|,\d)")
_NUMERIC = re.compile(r"[+-]?\d+(?:[.,]\d+)?")
_PROPERTY_POS = frozenset({"NOUN", "ADJF", "ADJS", "PRTF", "PRTS"})
_CASED_UNITS = re.compile(
    r"(?<!\w)(?:[kMGTmunµ]?Pa|[kMGTmunµ]?W|[kMGTmunµ]?V|[kMGTmunµ]?A|"
    r"[kMGTmunµ]?Hz|[кМГТм]?Па|[кМГТм]?Вт|[кМГТм]?Гц)(?!\w)"
)
# Units must not become dictionary words (e.g. 'м' / 'с') or be inflected.
_UNIT_WORDS = frozenset({"мм", "см", "дм", "м", "км", "мкм", "нм", "г", "кг", "мг", "л", "мл", "с", "мин", "ч", "шт", "па", "кпа", "мпа", "вт", "квт", "мвт", "в", "а", "ма", "гц", "кгц", "мгц"})


def display_text(value: object) -> str:
    if value is None:
        return ""
    return " ".join(unicodedata.normalize("NFC", str(value)).split()).strip(' ,;:!?"\'«»„“”')


def display_name(value: object) -> str:
    text = _COSMETIC.sub(" ", display_text(value)).strip(" .")
    if text.isupper() and _CYRILLIC_WORD.search(text):
        text = text.capitalize()
    return text


@lru_cache(maxsize=1)
def _morph() -> pymorphy3.MorphAnalyzer:
    # Loading the dictionary is unnecessary until the snapshot is actually built.
    return pymorphy3.MorphAnalyzer()


@lru_cache(maxsize=16_384)
def _lemma(word: str) -> str:
    if word in _UNIT_WORDS or len(word) > 64:
        return word
    parses = _morph().parse(word)
    # Characteristic phrases describe properties, not verbs: 'стали' means
    # steel here, rather than the usually more likely verb lemma 'стать'.
    lexical = next((parse for parse in parses if parse.is_known and parse.tag.POS in _PROPERTY_POS), None)
    if lexical is None:
        return word
    return lexical.normal_form.replace("ё", "е")


def _decimal(match: re.Match[str]) -> str:
    raw = match.group().replace(",", ".")
    sign = "-" if raw.startswith("-") else ""
    integer, _, fraction = raw.lstrip("+-").partition(".")
    integer = integer.lstrip("0") or "0"
    fraction = fraction.rstrip("0")
    if integer == "0" and not fraction:
        sign = ""
    return sign + integer + ("." + fraction if fraction else "")


def _normalize(value: object) -> str:
    parts = re.split(f"({_CASED_UNITS.pattern})", display_text(value))
    text = "".join(part if index % 2 else part.casefold().replace("ё", "е") for index, part in enumerate(parts))
    text = text.translate(str.maketrans({"−": "-", "–": "-", "—": "-"}))
    text = _NUMBER.sub(_decimal, text)
    text = _COSMETIC.sub(" ", text)
    text = re.sub(r"(?<!\w)\.|\.(?!\w)", " ", text)
    text = " ".join(text.split())
    text = re.sub(r"\s*([/%°])\s*", r"\1", text)
    # Only whole Cyrillic words are eligible. Never lemmatize part of an
    # alphanumeric model, a mixed-script identifier or a unit expression.
    return re.sub(r"(?<!\w)[а-яе]+(?!\w)", lambda match: _lemma(match.group()), text)


def normalize_key(value: object) -> str:
    return _normalize(value)


def normalize_value(value: object) -> str:
    return _normalize(value)


def normalize_unit(value: object) -> str:
    # Case-sensitive SI prefixes distinguish МПа/мПа and MW/mW. Units are
    # neither lemmatized nor converted: г, кг, мм and см remain distinct.
    return re.sub(r"\s*([/·*^])\s*", r"\1", display_text(value)).rstrip(".")


def is_numeric(value: object) -> bool:
    return _NUMERIC.fullmatch(display_text(value)) is not None
