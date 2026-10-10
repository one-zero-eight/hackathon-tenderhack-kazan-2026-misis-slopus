"""Импорт выгрузки КТРУ (zakupki.gov.ru, датасет Meln1kz/KTRU_Codes) в формат каталога.

    python -m ktru_autofill.ktru_import data-archived/raw/ktru.json data-archived/ktru_real.json

Значения КТРУ записаны как «80», «≥ 80 и < 90», «≤ 5000», «Для лазерной печати»;
единицы — «Кубический сантиметр;^миллилитр», «Дюйм (25,4 мм)». Числовые значения
становятся диапазоном в канонической единице справочника `units`, остальные — enum.
Код ОКПД2 позиции — часть кода КТРУ до дефиса.
"""

from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from . import units as U

_NUM = r"[+-]?\s*\d+(?:[.,]\d+)?"
_BOUND_RE = re.compile(rf"^(?P<op>[≥>≤<])\s*(?P<num>{_NUM})$")
_EXACT_RE = re.compile(rf"^{_NUM}$")
NO_DATA = "нет данных"


def _f(s: str) -> float:
    return float(s.replace(" ", "").replace(",", "."))


def parse_range(value: str) -> tuple[float, float] | None:
    """«80» → (80, 80); «≥ 80 и < 90» → (80, 90); «≤ 5000» → (0, 5000); иначе None.

    Строгие границы считаем нестрогими: для сужения кодов разница несущественна.
    """
    v = value.strip()
    if _EXACT_RE.match(v):
        x = _f(v)
        return x, x
    lo, hi = -math.inf, math.inf
    parts = [p.strip() for p in v.split(" и ")]
    if not 1 <= len(parts) <= 2:
        return None
    for p in parts:
        m = _BOUND_RE.match(p)
        if not m:
            return None
        x = _f(m.group("num"))
        if m.group("op") in "≥>":
            lo = max(lo, x)
        else:
            hi = min(hi, x)
    if lo == -math.inf:
        lo = 0.0 if hi >= 0 else -math.inf
    return lo, hi


def canonical_unit(ktru_unit: str) -> str | None:
    """«Кубический сантиметр;^миллилитр» → «мл»; неизвестную возвращаем как есть, в нижнем регистре."""
    u = (ktru_unit or "").strip()
    if not u or u == NO_DATA:
        return None
    for part in re.split(r"[;^]", u):
        part = re.sub(r"\(.*?\)", "", part).strip()
        if part and (unit := U.lookup(part)):
            return unit.name
    return re.sub(r"\(.*?\)", "", u.split(";")[0]).strip().lower()


def convert_characteristic(attr: dict[str, Any]) -> dict[str, Any] | None:
    name = attr.get("attr_name", "").strip()
    values = [v for v in attr.get("attr_values", []) if v.get("value") and v["value"] != NO_DATA]
    if not name or name == NO_DATA or not values:
        return None
    ranges = [parse_range(v["value"]) for v in values]
    if all(ranges):
        units = Counter(canonical_unit(v.get("value_unit", "")) for v in values)
        unit = units.most_common(1)[0][0]
        lo = min(r[0] for r in ranges)
        hi = max(r[1] for r in ranges)
        # Безразмерные и штучные величины отдаём числом без единицы.
        kind = "count" if unit in (None, "шт", "штука", "единица") else "number"
        d = {"name": name, "type": kind, "unit": unit}
        if lo > -math.inf or hi < math.inf:
            d["range"] = [lo, hi]  # json.dumps запишет inf как Infinity — это допустимо для json.loads
        return d
    return {"name": name, "type": "enum", "values": list(dict.fromkeys(v["value"].strip() for v in values))}


def convert(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for item in raw:
        code = item.get("ktru_code", "")
        if "-" not in code:
            continue
        chars = [c for c in map(convert_characteristic, item.get("attributes", [])) if c]
        out.append({
            "code": code,
            "name": item.get("title", "").strip(),
            "okpd2": code.split("-")[0],
            "unit": item.get("unit"),
            "characteristics": chars,
        })
    return out


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    src = Path(argv[0]) if argv else Path("data-archived/raw/ktru.json")
    dst = Path(argv[1]) if len(argv) > 1 else Path("data-archived/ktru_real.json")
    positions = convert(json.loads(src.read_text(encoding="utf-8")))
    dst.write_text(json.dumps(positions, ensure_ascii=False), encoding="utf-8")
    kinds = Counter(c["type"] for p in positions for c in p["characteristics"])
    print(f"{len(positions)} позиций → {dst}; характеристики: {dict(kinds)}")


if __name__ == "__main__":
    main()
