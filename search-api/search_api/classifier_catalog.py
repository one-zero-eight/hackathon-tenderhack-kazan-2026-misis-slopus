"""Read-only catalog schemas and sibling specializations from prebuilt SQLite files."""
from __future__ import annotations

import gzip
import json
import math
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pysqlite3 as sqlite3

from search_api.config import settings
from search_api.units import unit_short_name

Json = dict[str, Any]
_RANGE_OP = {"greaterOrEqual": "≥", "lessOrEqual": "≤", "greater": ">", "less": "<", "equal": "="}
_MATH_OP = {3: ">", 4: "<", 6: "≥", 7: "≤"}
_CONDITIONS = {1: "без ограничения значения", 3: "больше", 4: "меньше", 5: "диапазон",
               6: "не менее", 7: "не более", 8: "перечень значений"}


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _fold(value: str) -> str:
    return value.strip().lower().replace("ё", "е")


def _option_label(characteristics: list[Json], values: dict[str, str]) -> str:
    parts = []
    for item in characteristics:
        value = _text(values.get(item["name"])).strip()
        if value:
            parts.append(f"{item['name']}: {value}")
    return " · ".join(parts)


def _values_key(characteristics: list[Json], values: dict[str, str]) -> str:
    return "\0".join(_text(values.get(item["name"])) for item in characteristics)


def _char_score(entry: Json, name: str, preferred: set[str]) -> int:
    score = 10
    if entry.get("required"):
        score += 20
    if entry.get("kind") == "1":
        score += 10
    if re.match(r"^(тип|вид|назначение)\b", name, re.I):
        score += 5
    if _fold(name) in preferred:
        score += 50
    return score


def _pick_discriminating(children: list[Json], preferred_names: list[str] | None = None, max_size: int = 3) -> list[Json] | None:
    """Compact characteristic set whose value tuples distinguish sibling positions."""
    if len(children) < 2:
        return None
    preferred = {_fold(name) for name in (preferred_names or []) if _text(name).strip()}
    meta: dict[str, Json] = {}
    for child in children:
        for name, entry in child["values"].items():
            if not entry.get("value"):
                continue
            score = _char_score(entry, name, preferred)
            existing = meta.get(name)
            if existing is None:
                meta[name] = {"name": name, "code": entry.get("code"), "score": score}
            else:
                existing["code"] = existing["code"] or entry.get("code")
                existing["score"] = max(existing["score"], score)

    candidates = []
    for name, item in meta.items():
        distinct: set[str] = set()
        missing = False
        for child in children:
            entry = child["values"].get(name)
            if not entry or not entry.get("value"):
                missing = True
                break
            distinct.add(entry["value"])
        if missing or len(distinct) < 2:
            continue
        candidates.append(item)
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item["score"], _fold(item["name"])))
    max_size = max(1, min(max_size, 3))

    def partitions(names: list[str]) -> bool:
        seen: set[str] = set()
        for child in children:
            parts = []
            for name in names:
                entry = child["values"].get(name)
                if not entry or not entry.get("value"):
                    return False
                parts.append(entry["value"])
            key = "\0".join(parts)
            if key in seen:
                return False
            seen.add(key)
        return len(seen) == len(children)

    def combinations(items: list[Json], size: int) -> list[list[Json]]:
        if size <= 0:
            return [[]]
        if size > len(items):
            return []
        out: list[list[Json]] = []

        def walk(start: int, path: list[Json]) -> None:
            if len(path) == size:
                out.append(path[:])
                return
            for index in range(start, len(items)):
                path.append(items[index])
                walk(index + 1, path)
                path.pop()

        walk(0, [])
        return out

    for size in range(1, min(max_size, len(candidates)) + 1):
        pool = candidates if size == 1 else candidates[: max(size, 12)]
        ranked = sorted(
            combinations(pool, size),
            key=lambda set_: (-sum(item["score"] for item in set_), "\0".join(_fold(item["name"]) for item in set_)),
        )
        for chosen in ranked:
            names = [item["name"] for item in chosen]
            if partitions(names):
                return sorted(chosen, key=lambda item: (-item["score"], _fold(item["name"])))

    # Best-effort greedy set when a full partition is impossible.
    chosen: list[Json] = []
    best_count = 1
    for _ in range(min(max_size, len(candidates))):
        improved = None
        improved_count = best_count
        for candidate in candidates:
            if any(item["name"] == candidate["name"] for item in chosen):
                continue
            names = [item["name"] for item in chosen] + [candidate["name"]]
            keys = set()
            for child in children:
                parts = []
                ok = True
                for name in names:
                    entry = child["values"].get(name)
                    if not entry or not entry.get("value"):
                        ok = False
                        break
                    parts.append(entry["value"])
                if ok:
                    keys.add("\0".join(parts))
            if len(keys) > improved_count:
                improved = candidate
                improved_count = len(keys)
        if improved is None:
            break
        chosen.append(improved)
        best_count = improved_count
        if best_count == len(children):
            break
    if not chosen or best_count < 2:
        return None
    return sorted(chosen, key=lambda item: (-item["score"], _fold(item["name"])))


def short_unit_name(value: Any, code: str | None = None) -> str:
    return unit_short_name(_text(value), code)


def _dedupe_options(options: list[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for option in options:
        key = re.sub(r"(ый|ий|ой|ая|яя|ое|ее|ые|ие|ых|их|ую|юю)$", "", _fold(option))
        if key not in seen:
            seen.add(key)
            result.append(option)
    return result


def _number(value: Any) -> float | None:
    text = _text(value).replace(",", ".").strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _date(value: Any) -> str:
    text = _text(value).strip()
    match = re.match(r"\d{4}-\d{2}-\d{2}", text)
    return match.group() if match else text


def _decode(payload: bytes) -> Json:
    return json.loads(gzip.decompress(payload))


def _ktru_parts(value: Json) -> list[Json]:
    # Each concrete value remains a separate option, not a concatenated enum.
    chunks = [_text(item) for item in value.get("concrete_values") or []]
    if value.get("text"):
        chunks.append(_text(value["text"]))
    for span in value.get("ranges") or []:
        left = (_RANGE_OP.get(span.get("min_operator"), _text(span.get("min_operator")))
                + _text(span["min"])) if span.get("min") not in (None, "") else ""
        right = (_RANGE_OP.get(span.get("max_operator"), _text(span.get("max_operator")))
                 + _text(span["max"])) if span.get("max") not in (None, "") else ""
        text = f"{left} … {right}" if left and right else left or right
        if text:
            chunks.append(text)
    unit = value.get("unit") or {}
    meta = {key: _text(unit[source]) for key, source in (("okeiCode", "code"), ("okeiName", "name"))
            if unit.get(source)}
    short = short_unit_name(unit.get("name"), unit.get("code"))
    if not chunks and short:
        return [{"text": short, **meta}]
    return [{"text": f"{text} {short}" if short else text, **meta} for text in chunks]


def _ktru_ranges(values: list[Json]) -> list[Json]:
    if not values or not all(value.get("ranges") and not value.get("text")
                             and not value.get("concrete_values") for value in values):
        return []
    result = []
    for value in values:
        parts = _ktru_parts(value)
        for index, span in enumerate(value["ranges"]):
            if not parts:
                continue
            result.append({
                "option": parts[index if index < len(parts) else 0]["text"],
                "min": _number(span.get("min")), "max": _number(span.get("max")),
                "minInclusive": span.get("min_operator") != "greater",
                "maxInclusive": span.get("max_operator") != "less",
            })
    return result


def _ktru_characteristics(raw: Json, standards: list[str]) -> list[Json]:
    result = []
    for row in raw.get("characteristics") or []:
        if not isinstance(row, dict) or row.get("actual") is False:
            continue
        name = _text(row.get("name")).strip()
        if not name:
            continue
        values = row.get("values") or []
        parts = [part for value in values for part in _ktru_parts(value)]
        options = _dedupe_options([part["text"] for part in parts if part["text"]])
        unit_part = next((part for part in parts if part.get("okeiCode") or part.get("okeiName")), {})
        unit = short_unit_name(unit_part.get("okeiName"), unit_part.get("okeiCode"))
        badges = ["обязательная"] if row.get("required") else []
        if _text(row.get("type")) == "1":
            badges.append("качественная")
        if _text(row.get("type")) == "2":
            badges.append("количественная")
        hints = []
        opaque = len(options) >= 2 and all(re.fullmatch(r"\d+([.,]\d+)?", option.strip()) for option in options)
        if opaque:
            hints.append(", ".join(standards) if standards else "В КТРУ только числовые коды, без текстовой расшифровки")
        if unit and not any(unit in option or unit.lower() in option.lower() for option in options):
            hints.append(f"Ед. изм.: {unit}")
        parsed = {
            "name": name, "required": bool(row.get("required")),
            "values": " · ".join(part["text"] for part in parts if part["text"]) or "—",
            "badges": badges, "parts": parts, "options": options,
            "optionLabels": {option: option.strip() for option in options},
            "ranges": _ktru_ranges(values), "hint": ". ".join(hints),
            # kind is the purchase description choice mode; choice_type is NOT multiselect.
            "multiple": _text(row.get("kind")) == "3",
        }
        if row.get("code"):
            parsed["code"] = _text(row["code"])
        if unit:
            parsed["unit"] = unit
        if unit_part.get("okeiCode"):
            parsed["okeiCode"] = unit_part["okeiCode"]
        result.append(parsed)
    return result


def draft_ktru_characteristics(record: Json | None) -> list[Json]:
    return [{
        "id": item.get("code") or item["name"], "name": item["name"],
        "value": "", "rangeOption": "", "required": bool(item.get("required")),
        "badges": [badge for badge in item.get("badges", [])
                   if badge.strip().lower() not in {"обязательная", "качественная", "количественная"}],
        "hint": item.get("hint") or "", "options": list(item.get("options") or []),
        "optionLabels": dict(item.get("optionLabels") or {}), "ranges": list(item.get("ranges") or []),
        "unit": item.get("unit") or "", "okeiCode": item.get("okeiCode"),
        "multiple": bool(item.get("multiple")), "source": "ktru",
    } for item in (record or {}).get("characteristics", [])]


def _spgz_enum_ranges(options: list[str]) -> list[Json]:
    """Parse numeric SPGZ choices, preserving exact option keys and inclusivity."""
    number = r"[+-]?\d+(?:[.,]\d+)?"
    comparison = re.compile(rf"(>=|<=|>|<|=)\s*({number})")
    result = []
    for option in options:
        text = option.strip().replace("≥", ">=").replace("≤", "<=").replace("…", " ")
        if re.fullmatch(number, text):
            value = _number(text)
            result.append({"option": option, "min": value, "max": value,
                           "minInclusive": True, "maxInclusive": True})
            continue
        matches = list(comparison.finditer(text))
        if not matches or len(matches) > 2 or comparison.sub("", text).strip():
            return []
        bounds: Json = {"option": option, "min": None, "max": None,
                        "minInclusive": True, "maxInclusive": True}
        for match in matches:
            op, raw = match.groups()
            value = _number(raw)
            if op == "=":
                if len(matches) != 1:
                    return []
                bounds.update(min=value, max=value)
            else:
                side = "min" if op.startswith(">") else "max"
                if bounds[side] is not None:
                    return []
                bounds[side] = value
                bounds[f"{side}Inclusive"] = op.endswith("=")
        if bounds["min"] is not None and bounds["max"] is not None and bounds["min"] > bounds["max"]:
            return []
        result.append(bounds)
    return result


def draft_spgz_characteristics(record: Json | None) -> list[Json]:
    result = []
    for item in (record or {}).get("characteristics", []):
        options = [option.strip() for option in item.get("options", []) if option.strip()]
        badges = item.get("badges") or []
        unit_part = next((part for part in item.get("parts", []) if part.get("okeiCode") or part.get("okeiName")), {})
        unit = short_unit_name(unit_part.get("okeiName"), unit_part.get("okeiCode"))
        result.append({
            "id": f"spgz:{item.get('code') or item['name']}", "name": item["name"],
            "value": "", "rangeOption": "", "required": bool(item.get("required")) or any("обязательн" in b.lower() for b in badges),
            "badges": [b for b in badges if not re.search("обязательн|качественн|количественн", b, re.I)],
            "hint": f"Ограничение СПГЗ: {item['value']}" if not options and item.get("value") and item["value"] != "—" else "",
            "options": options, "optionLabels": {}, "ranges": [], "unit": unit, "okeiCode": unit_part.get("okeiCode"),
            "numeric": any(b.lower() == "количественная" for b in badges), "multiple": False, "source": "spgz",
        })
    return result


class ClassifierCatalog:
    """Point lookups; cursors stream KTRU siblings, SPGZ groups are capped at 80."""

    def __init__(self, ktru_path: Path | None = None, classifiers_path: Path | None = None):
        self.ktru_path = ktru_path
        self.classifiers_path = classifiers_path

    @contextmanager
    def _db(self, kind: str) -> Iterator[sqlite3.Connection]:
        path = Path((self.ktru_path or settings.ktru_sqlite) if kind == "ktru"
                    else (self.classifiers_path or settings.classifiers_sqlite)).resolve()
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute(f"PRAGMA mmap_size={settings.catalog_mmap_size}")
            conn.execute("PRAGMA cache_size=-2048")
            conn.execute("PRAGMA temp_store=FILE")
            expected = "ktru-mmap-v2" if kind == "ktru" else "classifiers-full-v4"
            version = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if version is None or version["value"] != expected:
                raise RuntimeError(f"Catalog {path} must be built with schema {expected}")
            yield conn
        finally:
            conn.close()

    def get_ktru_record(self, code: str) -> Json | None:
        if not code or len(code) > 64:
            return None
        with self._db("ktru") as db:
            row = db.execute("SELECT name,official_name,payload FROM ktru WHERE code=?", (code,)).fetchone()
        if row is None:
            return None
        raw = _decode(row["payload"])
        standards = [_text(item.get("document_name")).strip() for item in (raw.get("nsi") or {}).get("standards") or []]
        standards = [item for item in standards if item]
        okpd = raw.get("okpd2") or {}
        parent = raw.get("parent") or {}
        cancel = raw.get("cancellation")
        official = row["official_name"] or _text(raw.get("name"))
        dates = [("Дата включения", "inclusion_date"), ("Дата публикации", "publish_date"),
                 ("Дата обновления", "update_date"), ("Применение с", "application_date_start"),
                 ("Применение по", "application_date_end")]
        return {
            "code": _text(raw.get("code", code)), "name": row["name"] or official, "officialName": official,
            "status": _text(raw.get("status")), "version": _text(raw.get("version")),
            "versionCode": _text(raw.get("version_code")), "isTemplate": bool(raw.get("is_template")),
            "noNewFeatures": None if raw.get("no_new_features") is None else bool(raw.get("no_new_features")),
            "noNewFeaturesReason": _text(raw.get("no_new_features_reason")).strip(),
            "description": _text(raw.get("description")).strip(),
            "okpd2": {"code": _text(okpd["code"]), "name": _text(okpd.get("name"))} if okpd.get("code") else None,
            "units": [{"code": _text(unit["code"]).strip(), "name": _text(unit.get("name", unit["code"])).split(";")[0].strip()}
                      for unit in raw.get("units") or [] if _text(unit.get("code")).strip()],
            "rubricators": [{"code": _text(item.get("code")).strip(), "name": _text(item.get("name"))}
                            for item in raw.get("rubricators") or [] if item.get("code") or item.get("name")],
            "standards": standards,
            "dates": [{"label": label, "value": _date(raw.get(key))} for label, key in dates if _date(raw.get(key))],
            "parent": {"code": _text(parent["code"]), "version": _text(parent.get("version"))} if parent.get("code") else None,
            "printFormUrl": (raw.get("print_form") or {}).get("url") or None,
            "cancellation": {"date": _date(cancel.get("date")), "reason": _text(cancel.get("reason")).strip()} if cancel is not None else None,
            "characteristics": _ktru_characteristics(raw, standards),
        }

    def get_ktru_node(self, code: str) -> Json | None:
        if not code or len(code) > 64:
            return None
        with self._db("ktru") as db:
            row = db.execute("SELECT code,name,status,is_template,parent_code FROM ktru WHERE code=?", (code,)).fetchone()
            if row is None:
                return None
            count = db.execute("SELECT count(*) FROM ktru WHERE parent_code=?", (code,)).fetchone()[0]
        label = None
        if not row["is_template"] and row["parent_code"]:
            spec = self.get_ktru_specialization(code)
            if spec and spec.get("currentValues"):
                # Always surface discriminating characteristics in the tree.
                label = _option_label(spec["characteristics"], spec["currentValues"]) or None
        return {"id": f"ktru:{code}", "code": code, "name": row["name"], "kind": "ktru",
                "status": row["status"], "isTemplate": bool(row["is_template"]),
                "childCount": count, "hasChildren": count > 0, "variantLabel": label}

    def sole_ktru_for_okpd(self, code: str) -> Json | None:
        if not code:
            return None
        with self._db("ktru") as db:
            rows = db.execute("SELECT code,name,status,is_template FROM ktru WHERE effective_okpd=? AND is_okpd_root=1 ORDER BY code LIMIT 2", (code,)).fetchall()
        if len(rows) != 1 or not rows[0]["is_template"] or rows[0]["status"] not in (None, "", "ACTIVE"):
            return None
        return {"code": rows[0]["code"], "name": rows[0]["name"]}

    def sole_spgz_for_kpgz(self, code: str) -> Json | None:
        if not code:
            return None
        with self._db("classifiers") as db:
            rows = db.execute("SELECT s.id,s.name FROM spgz s JOIN kpgz k ON k.external_id=s.kpgz_external_id WHERE k.code=? AND s.is_excluded=0 LIMIT 2", (code,)).fetchall()
        return {"code": str(rows[0]["id"]), "name": rows[0]["name"]} if len(rows) == 1 else None

    @staticmethod
    def _okeis(db: sqlite3.Connection, raw: Json) -> list[Json]:
        result = []
        seen = set()
        for ref in raw.get("okeis") or []:
            code = _text(ref.get("code")).strip()
            if not code or code in seen:
                continue
            seen.add(code)
            row = db.execute("SELECT name FROM okei WHERE code=? AND kind='unit' LIMIT 1", (code,)).fetchone()
            name = row["name"].split(";")[0].strip() if row else code
            result.append({"code": code, "name": name, "short": short_unit_name(name, code)})
        return result

    @staticmethod
    def _spgz_characteristic(item: Json, units: list[Json]) -> Json | None:
        name = _text(item.get("characteristicName")).strip()
        if not name:
            return None
        badges = []
        if item.get("valueTypeId") == 1:
            badges.append("количественная")
        if item.get("valueTypeId") == 5:
            badges.append("качественная")
        if item.get("conditionTypeId") in _CONDITIONS:
            badges.append(_CONDITIONS[item["conditionTypeId"]])
        if item.get("catalogVariantId") == 1:
            badges.append("обязательная для описания")
        if item.get("catalogVariantId") == 2:
            badges.append("выбираемая")
        if item.get("codeCharKTRU"):
            badges.append(f"КТРУ {item['codeCharKTRU']}")
        result = {"name": name, "required": item.get("catalogVariantId") == 1, "badges": badges}
        if item.get("kpgzCharacteristicId") is not None:
            result["code"] = str(item["kpgzCharacteristicId"])
        enums = [_text(entry.get("value")).strip() for entry in item.get("characteristicSpgzEnums") or []]
        enums = [entry for entry in enums if entry]
        dimensions = [
            (r"масс|вес|нетто|брутто", {"161", "163", "166", "168", "206"}),
            (r"длин|ширин|высот|толщин|диаметр|диагонал|глубин|радиус|пролёт|пролет", {"003", "004", "005", "006", "008", "018", "039"}),
            (r"площад", {"055", "059", "058", "061"}),
            (r"объ[её]м|ёмкост|емкост", {"111", "112", "113", "116", "118"}),
            (r"мощност", {"214", "215", "216"}),
            (r"частот", {"290", "291", "292", "293", "295"}),
            (r"время|срок|продолжительн|возраст", {"354", "355", "356", "359", "360", "362", "366"}),
            (r"количество|число|кол-во", {"796", "778", "839", "868", "875", "876"}),
        ]
        unit = None
        for pattern, codes in dimensions:
            if re.search(pattern, name.lower()):
                candidates = [entry for entry in units if entry["code"] in codes]
                unit = candidates[0] if len(candidates) == 1 else None
                break
        if enums:
            parts = [{"text": value} for value in enums]
            if unit:
                for part in parts:
                    part.update(okeiCode=unit["code"], okeiName=unit["name"])
                result.update(unit=unit["short"], okeiCode=unit["code"])
            return {**result, "value": " · ".join(enums), "options": enums, "parts": parts}
        v1, v2 = (_text(item.get(key)).strip() for key in ("value1", "value2"))
        low, high = (_MATH_OP.get(_number(item.get(key)), "") for key in ("minMathNotation", "maxMathNotation"))
        if v1 and v2 and v1 != v2:
            value = f"{low}{v1} … {high}{v2}"
        elif v1 or v2:
            operator = low if v1 else high
            value = f"{operator} {v1 or v2}" if operator else v1 or v2
        else:
            value = " … ".join(op for op in (low, high) if op) or "—"
        if (v1 or v2) and unit:
            value += f" {unit['short']}"
        part = {"text": value}
        if unit:
            part.update(okeiCode=unit["code"], okeiName=unit["name"])
        return {**result, "value": value, "parts": [] if value == "—" else [part]}

    def get_directory_record(self, tab: str, code: str) -> Json | None:
        if tab not in {"okpd", "kpgz", "spgz", "okei"} or not code or len(code) > 64:
            return None
        if tab == "spgz" and not re.fullmatch(r"\d+", code):
            return None
        with self._db("classifiers") as db:
            if tab == "spgz":
                if int(code) > 2**53 - 1:
                    return None
                row = db.execute("SELECT * FROM spgz WHERE id=?", (int(code),)).fetchone()
            else:
                row = db.execute(f"SELECT * FROM {tab} WHERE code=? LIMIT 1", (code,)).fetchone()
            if row is None:
                return None
            code = str(row["id"]) if tab == "spgz" else row["code"]
            payload = db.execute("SELECT payload FROM raw_records WHERE tab=? AND code=?", (tab, code)).fetchone()
            raw = _decode(payload["payload"]) if payload else {}
            result = {"tab": tab, "code": code, "name": row["name"], "characteristics": [], "links": [],
                      "fields": [], "backlinks": [], "backlinksMore": 0}
            if tab == "spgz":
                result["excluded"] = bool(row["is_excluded"])
                units = self._okeis(db, raw)
                result["characteristics"] = [parsed for item in raw.get("spgzCharacteristics") or []
                                             if (parsed := self._spgz_characteristic(item, units))]
                if row["okpd2_code"]:
                    result["links"].append({"tab": "okpd", "code": row["okpd2_code"], "label": f"ОКПД2 {row['okpd2_code']}"})
                if row["kpgz_external_id"]:
                    kpgz = db.execute("SELECT code,name FROM kpgz WHERE external_id=? LIMIT 1", (row["kpgz_external_id"],)).fetchone()
                    if kpgz:
                        result["links"].append({"tab": "kpgz", "code": kpgz["code"], "label": f"КПГЗ {kpgz['code']} — {kpgz['name']}"})
                result["links"].extend({"tab": "okei", "code": unit["code"], "label": f"ОКЕИ {unit['code']} — {unit['name']}"} for unit in units)
                description = _text(raw.get("descriptions")).strip()
                if description and description != row["name"]:
                    result["description"] = description
            elif tab == "kpgz" and row["okpd_code"]:
                result["links"].append({"tab": "okpd", "code": row["okpd_code"], "label": f"ОКПД2 {row['okpd_code']}"})
            elif tab == "okpd":
                result["actual"] = bool(row["is_actual"])
            return result

    @staticmethod
    def _ktru_char_values(raw: Json) -> Json:
        chars = {}
        for item in raw.get("characteristics") or []:
            if not isinstance(item, dict) or item.get("actual") is False:
                continue
            name = _text(item.get("name")).strip()
            if name:
                chars[name] = {"values": _dedupe_options([part["text"] for value in item.get("values") or []
                                                       for part in _ktru_parts(value) if part["text"]]),
                               "code": _text(item.get("code")) or None,
                               "required": bool(item.get("required")), "kind": _text(item.get("kind"))}
        return chars

    def get_ktru_specialization(self, code: str) -> Json | None:
        if not code or len(code) > 64:
            return None
        with self._db("ktru") as db:
            template = db.execute("SELECT code,name,is_template,parent_code FROM ktru WHERE code=?", (code,)).fetchone()
            if template is None:
                return None
            if not template["is_template"]:
                template = db.execute("SELECT code,name,is_template FROM ktru WHERE code=?", (template["parent_code"],)).fetchone()
                if template is None or not template["is_template"]:
                    return None
            sql = "SELECT code,name,payload FROM ktru WHERE parent_code=? AND effective_okpd<>'' AND (status IS NULL OR status='' OR status='ACTIVE') ORDER BY code"
            children = []
            for child in db.execute(sql, (template["code"],)):
                chars = self._ktru_char_values(_decode(child["payload"]))
                values = {
                    name: {"value": entry["values"][0], "code": entry["code"], "required": entry["required"], "kind": entry["kind"]}
                    for name, entry in chars.items() if len(entry["values"]) == 1
                }
                children.append({"code": child["code"], "name": child["name"], "values": values})
            if len(children) < 2:
                return None
            picked = _pick_discriminating(children)
            if not picked:
                return None
            characteristics = [{"name": item["name"], "code": item["code"]} for item in picked]
            options = []
            for child in children:
                values = {}
                complete = True
                for characteristic in characteristics:
                    entry = child["values"].get(characteristic["name"])
                    if not entry or not entry.get("value"):
                        complete = False
                        break
                    values[characteristic["name"]] = entry["value"]
                if complete:
                    options.append({"values": values, "code": child["code"], "name": child["name"]})
            if len(options) < 2:
                return None
            options.sort(key=lambda option: (_fold(_option_label(characteristics, option["values"])), option["code"]))
            current = next((option["values"] for option in options if option["code"] == code), None)
            return {"templateCode": template["code"], "templateName": template["name"],
                    "characteristics": characteristics, "options": options, "currentValues": current}

    def get_spgz_specialization(self, input: Json) -> Json | None:
        kpgz_code = _text(input.get("kpgzCode")).strip() or None
        current = input.get("spgzCode")
        with self._db("classifiers") as db:
            if current:
                current = _text(current)
                if not re.fullmatch(r"\d+", current) or int(current) > 2**53 - 1:
                    return None
                current = str(int(current))
                row = db.execute("SELECT kpgz_external_id FROM spgz WHERE id=?", (int(current),)).fetchone()
                external = row[0] if row else None
                if external and not kpgz_code:
                    kpgz = db.execute("SELECT code FROM kpgz WHERE external_id=? LIMIT 1", (external,)).fetchone()
                    kpgz_code = kpgz[0] if kpgz else None
            elif kpgz_code:
                row = db.execute("SELECT external_id FROM kpgz WHERE code=? LIMIT 1", (kpgz_code,)).fetchone()
                external = row[0] if row else None
            else:
                return None
            if not external:
                return None
            count = db.execute("SELECT count(*) FROM spgz WHERE kpgz_external_id=? AND is_excluded=0", (external,)).fetchone()[0]
            if not 2 <= count <= 80:
                return None
            children = []
            for row in db.execute("SELECT s.id,s.name,raw.payload FROM spgz s JOIN raw_records raw ON raw.tab='spgz' AND raw.code=cast(s.id AS TEXT) WHERE s.kpgz_external_id=? AND s.is_excluded=0 LIMIT 80", (external,)):
                values = {}
                for item in _decode(row["payload"]).get("spgzCharacteristics") or []:
                    name = _text(item.get("characteristicName")).strip()
                    enums = [_text(entry.get("value")).strip() for entry in item.get("characteristicSpgzEnums") or []]
                    enums = [entry for entry in enums if entry]
                    v1, v2 = (_text(item.get(key)).strip() for key in ("value1", "value2"))
                    value = enums[0] if len(enums) == 1 else v1 if not enums and v1 and not v2 else None
                    if name and value is not None:
                        values[name] = {
                            "value": value,
                            "code": _text(item.get("kpgzCharacteristicId")) if item.get("kpgzCharacteristicId") is not None else None,
                        }
                children.append({"code": str(row["id"]), "name": row["name"], "values": values})
        if len(children) < 2:
            return None
        preferred = input.get("preferredCharNames") or []
        if input.get("preferredCharName"):
            preferred = [_text(input.get("preferredCharName"))] + list(preferred)
        picked = _pick_discriminating(children, preferred_names=preferred)
        if not picked:
            return None
        characteristics = [{"name": item["name"], "code": item["code"]} for item in picked]
        options = []
        for child in children:
            values = {}
            complete = True
            for characteristic in characteristics:
                entry = child["values"].get(characteristic["name"])
                if not entry or not entry.get("value"):
                    complete = False
                    break
                values[characteristic["name"]] = entry["value"]
            if complete:
                options.append({"values": values, "code": child["code"], "name": child["name"]})
        if len(options) < 2:
            return None
        options.sort(key=lambda option: (_fold(_option_label(characteristics, option["values"])), option["code"]))
        current_values = next((option["values"] for option in options if option["code"] == current), None)
        return {"characteristics": characteristics, "options": options, "kpgzCode": kpgz_code, "currentValues": current_values}

    def get_catalog_specialization(self, input: Json) -> Json | None:
        ktru = self.get_ktru_specialization(input["ktruCode"]) if input.get("ktruCode") else None
        preferred = [item["name"] for item in ktru["characteristics"]] if ktru else None
        spgz = self.get_spgz_specialization({"spgzCode": input.get("spgzCode"), "kpgzCode": input.get("kpgzCode"),
                                             "preferredCharNames": preferred})
        if not ktru and not spgz:
            return None
        characteristics = (ktru or spgz)["characteristics"]
        by_key: dict[str, Json] = {}

        def touch(values: dict[str, str]) -> Json:
            key = _values_key(characteristics, values)
            row = by_key.get(key)
            if row is None:
                row = {"values": dict(values), "ktruCode": None, "ktruName": None, "spgzCode": None, "spgzName": None}
                by_key[key] = row
            return row

        if ktru:
            for option in ktru["options"]:
                row = touch(option["values"])
                row["ktruCode"] = option["code"]
                row["ktruName"] = option["name"]
        if spgz:
            spgz_names = {_fold(item["name"]) for item in spgz["characteristics"]}
            same_set = len(spgz["characteristics"]) == len(characteristics) and all(
                _fold(item["name"]) in spgz_names for item in characteristics
            )
            if not ktru or same_set:
                for option in spgz["options"]:
                    aligned = {}
                    for characteristic in characteristics:
                        key = next((name for name in option["values"] if _fold(name) == _fold(characteristic["name"])), None)
                        if key is None:
                            continue
                        aligned[characteristic["name"]] = option["values"][key]
                    if len(aligned) != len(characteristics):
                        continue
                    row = touch(aligned)
                    row["spgzCode"] = option["code"]
                    row["spgzName"] = option["name"]
        options = [row for row in by_key.values() if row["ktruCode"] or row["spgzCode"]]
        options.sort(key=lambda option: _fold(_option_label(characteristics, option["values"])))
        if len(options) < 2:
            return None
        return {
            "characteristics": characteristics,
            "templateKtruCode": (ktru or {}).get("templateCode"),
            "templateKtruName": (ktru or {}).get("templateName"),
            "options": options,
            "currentValues": (ktru or {}).get("currentValues") or (spgz or {}).get("currentValues"),
        }


_catalog = ClassifierCatalog()
get_ktru_record = _catalog.get_ktru_record
get_ktru_node = _catalog.get_ktru_node
get_directory_record = _catalog.get_directory_record
sole_ktru_for_okpd = _catalog.sole_ktru_for_okpd
sole_spgz_for_kpgz = _catalog.sole_spgz_for_kpgz
get_catalog_specialization = _catalog.get_catalog_specialization
get_ktru_specialization = _catalog.get_ktru_specialization
get_spgz_specialization = _catalog.get_spgz_specialization
