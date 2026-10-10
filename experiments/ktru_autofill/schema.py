"""Позиции КТРУ и анкета характеристик (шаг 3.1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Literal

CharType = Literal["enum", "number", "count"]


def value_key(v: Any) -> str:
    """Ключ сравнения значений: «Белая» ≡ «белый», «А4» ≡ «A4»."""
    from .text import lemmatize, normalize_code

    s = str(v)
    return lemmatize(s) or normalize_code(s)


@dataclass
class Characteristic:
    """Характеристика позиции КТРУ.

    enum   — выбор из `values`; `synonyms` — как это значение пишут в тексте
             («Нитрил»: «нитриловые»; «Да»: «регулируемый»);
    number — число с единицей `unit` (каноническая, «г/м²»), в ответе
             {"значение", "единица", "как_написано"};
    count  — счётная величина («500 листов»), в ответе просто число.

    `unit_aliases` — написания, которые для этой характеристики означают `unit`
    без пересчёта («грамм» у плотности бумаги — это г/м², а не масса).
    `range` у позиции — допустимый диапазон; пустые `values` — значение свободное.
    """

    name: str
    type: CharType = "enum"
    values: list[str] = field(default_factory=list)
    synonyms: dict[str, list[str]] = field(default_factory=dict)
    unit: str | None = None
    unit_aliases: list[str] = field(default_factory=list)
    range: tuple[float, float] | None = None
    frequency: int = 0  # сколько СТЕ категории заполняют характеристику (из данных); 0 — неизвестно

    @property
    def units(self) -> list[str]:
        return [u for u in [self.unit, *self.unit_aliases] if u]

    @cached_property
    def value_keys(self) -> frozenset[str]:
        return frozenset(value_key(v) for v in self.values)

    def accepts(self, value: Any) -> bool:
        if self.type == "enum":
            return not self.values or value_key(value) in self.value_keys
        if isinstance(value, dict):
            value = value["значение"]
        return self.range is None or self.range[0] - 1e-9 <= float(value) <= self.range[1] + 1e-9

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Characteristic:
        rng = d.get("range")
        return cls(
            name=d["name"],
            type=d.get("type", "enum"),
            values=[str(v) for v in d.get("values", [])],
            synonyms={k: list(v) for k, v in d.get("synonyms", {}).items()},
            unit=d.get("unit"),
            unit_aliases=list(d.get("unit_aliases", [])),
            range=(float(rng[0]), float(rng[1])) if rng else None,
        )


@dataclass
class KtruCandidate:
    code: str
    name: str
    characteristics: list[Characteristic]
    synonyms: list[str] = field(default_factory=list)  # как товар называют в запросах
    okpd2: str = ""  # код ОКПД2 — узел иерархии, к которому относится позиция

    def char(self, name: str) -> Characteristic | None:
        return self._by_name.get(name)

    @cached_property
    def _by_name(self) -> dict[str, Characteristic]:
        return {c.name: c for c in self.characteristics}

    def accepts(self, char_name: str, value: Any) -> bool:
        """Совместима ли позиция со значением: характеристика есть и значение допустимо."""
        ch = self.char(char_name)
        return ch is not None and ch.accepts(value)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> KtruCandidate:
        return cls(
            code=d.get("code", ""),
            name=d.get("name", ""),
            characteristics=[Characteristic.from_dict(c) for c in d.get("characteristics", [])],
            synonyms=list(d.get("synonyms", [])),
            okpd2=d.get("okpd2") or d.get("code", "").split("-")[0],
        )


def build_form(candidates: list[KtruCandidate]) -> list[Characteristic]:
    """Анкета: характеристики кандидатов, объединённые по имени."""
    merged: dict[str, Characteristic] = {}
    for cand in candidates:
        for ch in cand.characteristics:
            cur = merged.get(ch.name)
            if cur is None:
                merged[ch.name] = Characteristic(
                    ch.name, ch.type, list(ch.values), {k: list(v) for k, v in ch.synonyms.items()},
                    ch.unit, list(ch.unit_aliases), ch.range, ch.frequency,
                )
                continue
            cur.values += [v for v in ch.values if v not in cur.values]
            for k, syn in ch.synonyms.items():
                cur.synonyms.setdefault(k, [])
                cur.synonyms[k] += [s for s in syn if s not in cur.synonyms[k]]
            cur.frequency += ch.frequency
            cur.unit = cur.unit or ch.unit
            cur.unit_aliases += [u for u in ch.unit_aliases if u not in cur.unit_aliases]
            if ch.range and cur.range:
                cur.range = (min(cur.range[0], ch.range[0]), max(cur.range[1], ch.range[1]))
            else:
                cur.range = cur.range or ch.range
    return list(merged.values())
