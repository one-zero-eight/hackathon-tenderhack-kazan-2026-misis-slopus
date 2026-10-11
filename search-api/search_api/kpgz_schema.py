"""Official leaf КПГЗ characteristic schemas from portal export (Лист2)."""
from __future__ import annotations

import threading
from collections import defaultdict
from pathlib import Path

import pyarrow.parquet as pq

from search_api.characteristic_normalization import display_name, normalize_key, normalize_unit
from search_api.config import settings

_NUMERIC_TYPES = frozenset({"Целое числовое значение", "Дробное числовое значение", "Диапазон"})
_DISCRETE_TYPES = frozenset({"Справочник", "Признак"})
_SKIP_KINDS = frozenset({"Категореобразующая"})
# Product-form guards: long kit/description enums drown autofill and the create UI.
MAX_OPTION_LEN = 80
MAX_ENUM_OPTIONS = 16
MAX_SCHEMA_GAP_FIELDS = 4
_lock = threading.Lock()
_cache: dict[str, object] = {"path": None, "mtime": None, "by_code": None}


def usable_enum_values(values: list[str], *, limit: int = MAX_ENUM_OPTIONS) -> list[str]:
    """Keep short discrete options; drop free-text kit / description blobs."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = display_name(value)
        if not text or len(text) > MAX_OPTION_LEN:
            continue
        key = text.casefold().replace("ё", "е")
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def suitable_for_product_form(item: dict) -> bool:
    """Эталонные short enums / признаки only — not every базовая portal field."""
    if item.get("kind") != "Эталонная":
        return False
    values = usable_enum_values(item.get("values") or [])
    if item.get("value_type") == "Признак":
        return True
    return 2 <= len(values) <= MAX_ENUM_OPTIONS


def schema_path() -> Path:
    return Path(settings.kpgz_characteristics_parquet)


def _load(path: Path) -> dict[str, list[dict]]:
    table = pq.read_table(path)
    by_code: dict[str, list[dict]] = defaultdict(list)
    columns = table.to_pydict()
    count = table.num_rows
    for index in range(count):
        kind = columns["kind"][index]
        if kind in _SKIP_KINDS:
            continue
        name = display_name(columns["name"][index])
        if not name:
            continue
        unit = normalize_unit(columns["unit"][index] or "")
        value_type = columns["value_type"][index] or ""
        values = [display_name(value) for value in (columns["values"][index] or []) if display_name(value)]
        # Keep typed empty fields and enums; drop blank free-text noise.
        if not values and value_type == "Текст":
            continue
        by_code[columns["kpgz_code"][index]].append({
            "name": name,
            "name_key": normalize_key(name),
            "kind": kind,
            "value_type": value_type,
            "values": values,
            "unit": unit,
            "numeric": value_type in _NUMERIC_TYPES,
            "discrete": value_type in _DISCRETE_TYPES or bool(values),
            "spgz_characteristic_id": columns["spgz_characteristic_id"][index],
        })
    for code, items in by_code.items():
        items.sort(key=lambda item: (
            0 if item["kind"] == "Эталонная" else 1,
            0 if item["discrete"] else 1,
            0 if item["values"] else 1,
            item["name"],
            item["unit"],
        ))
        by_code[code] = items
    return dict(by_code)


def schema_by_code() -> dict[str, list[dict]]:
    path = schema_path()
    if not path.exists():
        return {}
    mtime = path.stat().st_mtime_ns
    with _lock:
        if _cache["by_code"] is not None and _cache["path"] == path and _cache["mtime"] == mtime:
            return _cache["by_code"]  # type: ignore[return-value]
        loaded = _load(path)
        _cache.update(path=path, mtime=mtime, by_code=loaded)
        return loaded


def schema_fingerprint() -> str:
    path = schema_path()
    if not path.exists():
        return "missing"
    return f"{path.resolve()}:{path.stat().st_mtime_ns}"


def characteristics_for(kpgz_code: str, *, limit: int | None = None) -> list[dict]:
    code = kpgz_code.strip()
    if not code:
        return []
    items = schema_by_code().get(code, [])
    return items if limit is None else items[:limit]


# conditionTypeId codes used by experiments/kpgz_to_jev (recovered from portal dumps).
_ENUM, _NUMBER, _TEXT = 8, 5, 1


def as_kpgz_characteristics(kpgz_code: str) -> list[dict]:
    """Shape expected by experiments (`kpgzCharacteristics` on leaf КПГЗ nodes)."""
    result = []
    for index, item in enumerate(characteristics_for(kpgz_code), start=1):
        if item["value_type"] in _DISCRETE_TYPES or item["values"]:
            condition = _ENUM
        elif item["numeric"]:
            condition = _NUMBER
        else:
            condition = _TEXT
        result.append({
            "characteristicId": item["spgz_characteristic_id"] or index,
            "characteristicName": item["name"],
            "conditionTypeId": condition,
            "catalogVariantId": 1 if item["kind"] == "Эталонная" else 2,
            "characteristicKpgzEnums": [{"value": value} for value in item["values"]],
            "unitName": item["unit"] or None,
            "kind": item["kind"],
            "valueType": item["value_type"],
        })
    return result
