"""Read-only OKEI metadata and explicit, dimension-safe Pint conversions.

Codes are exact catalog identifiers (including leading zeroes). Text aliases must
resolve uniquely; Russian inflections are lemmatized, never prefix-matched.
Nonphysical OKEI units retain separate kinds rather than sharing a dimensionless
Pint unit. Unsupported catalog units are available for display and identity only.
"""

from __future__ import annotations

import gzip
import json
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import pysqlite3 as sqlite3
import pymorphy3
from pint import UnitRegistry

from search_api.config import settings


@dataclass(frozen=True, slots=True)
class OkeiUnit:
    """Immutable metadata; ``code=None`` denotes an explicit non-OKEI unit.

    ``kind`` gates conversion, ``pint_unit`` is an audited Pint expression, and
    ``factor`` uses the legacy parse_unit base (mm, g, ml, mm², seconds, bytes).
    ``aliases`` contains catalog names plus curated Russian/Latin spellings.
    """

    code: str | None
    name: str
    short_name: str
    kind: str
    pint_unit: str | None
    factor: float
    aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Spec:
    short: str
    kind: str
    pint: str | None
    aliases: tuple[str, ...] = ()


# Verified against classifiers.sqlite okei and its gzip raw_records payloads.
# In particular: 111 is ml, 116 is decalitre, 212 is W, 214 is kW,
# 215 is MW, 226 is VA, 260 is A, and 039 is the 25.4 mm inch.
_SPECS: Mapping[str, _Spec] = MappingProxyType({
    "003": _Spec("мм", "length", "millimeter", ("mm", "millimeter", "millimetre")),
    "004": _Spec("см", "length", "centimeter", ("cm", "centimeter", "centimetre")),
    "005": _Spec("дм", "length", "decimeter", ("dm",)),
    "006": _Spec("м", "length", "meter", ("m", "meter", "metre")),
    "008": _Spec("км", "length", "kilometer", ("km", "kilometer", "kilometre")),
    "009": _Spec("Мегаметр", "length", "megameter"),
    "015": _Spec("нм", "length", "nanometer", ("nm",)),
    "039": _Spec("дюйм", "length", "inch", ("дюйм", "in", "inch", "inches", '"', "″")),
    "050": _Spec("мм²", "area", "millimeter ** 2", ("mm2", "sq mm", "кв. мм")),
    "051": _Spec("см²", "area", "centimeter ** 2", ("cm2", "sq cm", "кв. см")),
    "053": _Spec("дм²", "area", "decimeter ** 2", ("dm2", "кв. дм")),
    "055": _Spec("м²", "area", "meter ** 2", ("m2", "sq m", "кв. м")),
    "059": _Spec("га", "area", "hectare", ("ha",)),
    "061": _Spec("км²", "area", "kilometer ** 2", ("km2", "кв. км")),
    "071": _Spec("дюйм²", "area", "inch ** 2", ("in2", "square inch")),
    "110": _Spec("мм³", "volume", "millimeter ** 3", ("mm3", "куб. мм")),
    "111": _Spec("мл", "volume", "milliliter", ("ml", "milliliter", "millilitre", "см3", "cm3", "куб. см")),
    "112": _Spec("л", "volume", "liter", ("l", "liter", "litre", "дм3", "dm3", "куб. дм")),
    "113": _Spec("м³", "volume", "meter ** 3", ("m3", "куб. м")),
    "116": _Spec("дал", "volume", "decaliter", ("dal",)),
    "118": _Spec("дл", "volume", "deciliter", ("dl",)),
    "122": _Spec("гл", "volume", "hectoliter", ("hl",)),
    "126": _Spec("Мегалитр", "volume", "megaliter"),
    "131": _Spec("дюйм³", "volume", "inch ** 3", ("in3", "cubic inch")),
    "160": _Spec("гг", "mass", "hectogram", ("hg",)),
    "161": _Spec("мг", "mass", "milligram", ("mg", "milligram")),
    "163": _Spec("г", "mass", "gram", ("гр", "g", "gram")),
    "164": _Spec("мкг", "mass", "microgram", ("ug", "µg")),
    "166": _Spec("кг", "mass", "kilogram", ("kg", "kilogram")),
    "168": _Spec("т", "mass", "metric_ton", ("t", "tonne", "тонна", "метрическая тонна")),
    "170": _Spec("кт", "mass", "kilometric_ton"),
    "173": _Spec("сг", "mass", "centigram"),
    "206": _Spec("ц", "mass", "okei_centner", ("центнер",)),
    "212": _Spec("Вт", "power", "watt", ("w", "watt")),
    "214": _Spec("кВт", "power", "kilowatt", ("kw", "kilowatt")),
    "215": _Spec("МВт", "power", "megawatt", ("mw", "megawatt")),
    "222": _Spec("В", "voltage", "volt", ("v", "volt")),
    "223": _Spec("кВ", "voltage", "kilovolt", ("kv", "kilovolt")),
    "226": _Spec("В·А", "apparent_power", "volt * ampere", ("va",)),
    "227": _Spec("кВ·А", "apparent_power", "kilovolt * ampere", ("kva",)),
    "228": _Spec("МВ·А", "apparent_power", "megavolt * ampere", ("mva",)),
    "243": _Spec("Вт·ч", "energy", "watt * hour", ("wh",)),
    "245": _Spec("кВт·ч", "energy", "kilowatt * hour", ("kwh",)),
    "246": _Spec("МВт·ч", "energy", "megawatt * hour", ("mwh",)),
    "247": _Spec("ГВт·ч", "energy", "gigawatt * hour", ("gwh",)),
    "255": _Spec("байт", "data_size", "byte", ("byte", "bytes", "b")),
    "256": _Spec("КБ", "data_size", "kilobyte", ("kb", "kilobyte")),
    "257": _Spec("МБ", "data_size", "megabyte", ("mb", "megabyte")),
    "2553": _Spec("ГБ", "data_size", "gigabyte", ("gb", "gigabyte")),
    "2554": _Spec("ТБ", "data_size", "terabyte", ("tb", "terabyte")),
    "260": _Spec("А", "current", "ampere", ("a", "amp", "ampere")),
    "263": _Spec("А·ч", "charge", "ampere * hour", ("ah",)),
    "290": _Spec("Гц", "frequency", "hertz", ("hz", "hertz")),
    "291": _Spec("кГц", "frequency", "kilohertz", ("khz",)),
    "292": _Spec("МГц", "frequency", "megahertz", ("mhz",)),
    "293": _Spec("ГГц", "frequency", "gigahertz", ("ghz",)),
    "295": _Spec("ТГц", "frequency", "terahertz", ("thz",)),
    "353": _Spec("мс", "time", "millisecond", ("ms",)),
    "354": _Spec("с", "time", "second", ("сек", "s", "sec", "second")),
    "355": _Spec("мин", "time", "minute", ("min", "minute")),
    "356": _Spec("ч", "time", "hour", ("h", "hr", "hour")),
    "359": _Spec("сут", "time", "day", ("сутки", "day")),
    "360": _Spec("нед", "time", "week", ("week",)),
    "625": _Spec("лист", "okei:625", None, ("sheet", "sheets")),
    "642": _Spec("ед", "okei:642", None, ("unit",)),
    "715": _Spec("пар", "okei:715", None, ("пара", "pair", "pairs")),
    "744": _Spec("%", "percent", "percent", ("percent",)),
    "778": _Spec("упак", "okei:778", None, ("pack", "package", "упак.")),
    "796": _Spec("шт", "count", None, ("шт.", "pcs", "piece", "pieces")),
    "839": _Spec("компл", "okei:839", None, ("set", "компл.")),
})
_BASES: Mapping[str, str] = MappingProxyType({
    "length": "millimeter", "mass": "gram", "volume": "milliliter",
    "area": "millimeter ** 2", "density": "gram / meter ** 2",
    "power": "watt", "time": "second", "frequency": "hertz",
    "data_size": "byte", "percent": "percent", "voltage": "volt",
    "current": "ampere", "apparent_power": "volt * ampere",
    "energy": "joule", "charge": "coulomb",
})
_VIRTUAL = (
    ("Микрометр", _Spec("мкм", "length", "micrometer", ("микрон", "um", "µm", "micrometer", "micrometre"))),
    ("Грамм на квадратный метр", _Spec("г/м²", "density", "gram / meter ** 2", ("г/кв.м", "гр/м2", "g/m2", "gsm"))),
    ("Ядро", _Spec("ядро", "count", None, ("core", "cores"))),
)


@lru_cache(maxsize=1)
def _pint() -> UnitRegistry:
    registry = UnitRegistry()
    registry.define("okei_centner = 100 * kilogram")
    return registry


@lru_cache(maxsize=1)
def _morph() -> pymorphy3.MorphAnalyzer:
    return pymorphy3.MorphAnalyzer()


def _key(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().casefold().replace("ё", "е").replace("²", "2").replace("³", "3").replace("^2", "2").replace("^3", "3").replace("µ", "u").replace("μ", "u")).rstrip(".").replace(" ", "").replace(".", "")


@lru_cache(maxsize=4096)
def _lemma(text: str) -> str:
    # Abbreviations/symbols are resolved exactly first. Only full Cyrillic words
    # are inflected; all intervening punctuation and words remain significant.
    return re.sub(r"[а-яё]{3,}", lambda m: _morph().parse(m.group())[0].normal_form, text.casefold().replace("ё", "е"))


def _names(name: str) -> tuple[str, ...]:
    return tuple(part.lstrip("^ ").strip() for part in name.split(";") if part.lstrip("^ ").strip())


def _make(code: str | None, name: str, spec: _Spec | None, aliases: tuple[str, ...]) -> OkeiUnit:
    if spec is None:
        return OkeiUnit(code, name, _names(name)[0], f"okei:{code}", None, 1.0, aliases)
    factor = float(_pint().Quantity(1, spec.pint).to(_BASES[spec.kind]).magnitude) if spec.pint else 1.0
    return OkeiUnit(code, name, spec.short, spec.kind, spec.pint, factor, tuple(dict.fromkeys((*aliases, spec.short, *spec.aliases))))


@dataclass(frozen=True, slots=True)
class _Catalog:
    by_code: Mapping[str, OkeiUnit]
    exact: Mapping[str, frozenset[OkeiUnit]]
    inflected: Mapping[str, frozenset[OkeiUnit]]


@lru_cache(maxsize=1)
def _catalog(path: Path) -> _Catalog:
    by_code: dict[str, OkeiUnit] = {}
    # mode=ro fails if the database is absent; never creates or updates it.
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        db.execute("PRAGMA query_only=ON")
        rows = db.execute("SELECT o.code,o.name,r.payload FROM okei o LEFT JOIN raw_records r ON r.tab='okei' AND r.code=o.code WHERE o.kind='unit' ORDER BY o.code")
        for code, name, payload in rows:
            raw = json.loads(gzip.decompress(payload)) if payload is not None else {}
            if raw.get("code", code) != code:
                raise ValueError(f"OKEI payload code differs from catalog: {code}")
            aliases = tuple(dict.fromkeys((*_names(name), *_names(raw.get("name") or name))))
            by_code[code] = _make(code, name, _SPECS.get(code), aliases)
    finally:
        db.close()
    units = [*by_code.values(), *(_make(None, name, spec, (name,)) for name, spec in _VIRTUAL)]
    exact: dict[str, set[OkeiUnit]] = {}
    inflected: dict[str, set[OkeiUnit]] = {}
    for unit in units:
        for alias in unit.aliases:
            exact.setdefault(_key(alias), set()).add(unit)
            inflected.setdefault(_key(_lemma(alias)), set()).add(unit)
    return _Catalog(MappingProxyType(by_code), MappingProxyType({key: frozenset(value) for key, value in exact.items()}), MappingProxyType({key: frozenset(value) for key, value in inflected.items()}))


def _current() -> _Catalog:
    return _catalog(settings.classifiers_sqlite.expanduser().resolve())


def units_for_kind(kind: str) -> tuple[OkeiUnit, ...]:
    """Audited catalog units available for scale suggestions."""
    return tuple(unit for unit in _current().by_code.values() if unit.kind == kind and unit.pint_unit)


def get_okei_unit(code: str) -> OkeiUnit | None:
    """Look up an exact OKEI identifier; unknown/group codes return None.

    Metadata and mappings are immutable and cached by the configured DB path.
    This intentionally does not zero-pad codes or infer a unit from a name.
    """
    return _current().by_code.get(code.strip())


def _candidates(text: str, catalog: _Catalog) -> frozenset[OkeiUnit]:
    key = _key(text)
    if key in catalog.exact:
        return catalog.exact[key]
    return catalog.inflected.get(_key(_lemma(text)), frozenset())


def resolve_unit(text: str) -> OkeiUnit | None:
    """Resolve an exact code, Latin/Russian alias, or Russian inflection.

    Ambiguous aliases return None, never the first match. Semicolon-separated
    names must all resolve to the same unit; arbitrary suffixes are rejected.
    Symbol case is folded for legacy input; colliding aliases are rejected
    rather than silently choosing a physical scale.
    """
    catalog = _current()
    text = text.strip()
    if text in catalog.by_code:
        return catalog.by_code[text]
    parts = _names(text)
    if not parts:
        return None
    matches = [_candidates(part, catalog) for part in parts]
    if any(len(match) != 1 for match in matches):
        return None
    units = set().union(*matches)
    return next(iter(units)) if len(units) == 1 else None


def unit_short_name(name: str | None, code: str | None = None) -> str:
    """Return a catalog short label; an explicit code is authoritative.

    Unknown explicit codes do not fall back to name-based unit resolution.
    For display only, unknown/ambiguous names are returned unchanged (first
    semicolon component); this does not authorize conversion.
    """
    unit = get_okei_unit(code) if code is not None else resolve_unit(name or "")
    return unit.short_name if unit else next(iter(_names(name or "")), "")


def parse_unit(raw: str) -> tuple[str, float] | None:
    """Legacy dimension/factor API, including density == ('density', 1).

    Count/pack kinds remain distinct. Unknown and ambiguous aliases return None.
    """
    unit = resolve_unit(raw)
    return (unit.kind, unit.factor) if unit else None


def convert_units(value: float, source_unit: str | None, target_unit: str | None, *, target_okei_code: str | None = None) -> tuple[float | None, list[str]]:
    """Convert with Pint only between audited units of the same semantic kind.

    The supplied target OKEI code overrides its display name; an unknown code
    fails without name fallback. Identical *unknown* text units retain the old
    identity behavior, but ambiguous aliases never do. Error strings match the
    autofill validator. Nonphysical OKEI identities never cross kinds.
    """
    if not math.isfinite(value):
        return None, ["переполнение при переводе единиц"]
    source = resolve_unit(source_unit) if source_unit else None
    if target_okei_code is not None:
        target = get_okei_unit(target_okei_code)
        if target is None:
            return None, ["неизвестная единица: пересчёт невозможен"]
    elif not target_unit:
        if source is not None:
            return None, ["единица отсутствует в схеме"]
        if source_unit and any(_candidates(part, _current()) for part in _names(source_unit)):
            return None, ["неизвестная единица: пересчёт невозможен"]
        return value, []
    else:
        target = resolve_unit(target_unit)
    if not source_unit:
        return None, ["не указана исходная единица измерения"]
    if source is not None and target is not None:
        if source.kind != target.kind:
            return None, ["несовместимые единицы измерения"]
        converted = float(_pint().Quantity(value, source.pint_unit).to(target.pint_unit).magnitude) if source.pint_unit and target.pint_unit else value
        if not math.isfinite(converted):
            return None, ["переполнение при переводе единиц"]
        return converted, []
    # Preserve exact unknown identity, not ambiguity or an explicit target code.
    if target_okei_code is None and source_unit.strip().casefold() == (target_unit or "").strip().casefold():
        if all(not _candidates(part, _current()) for part in _names(source_unit)):
            return value, []
    return None, ["неизвестная единица: пересчёт невозможен"]
