"""Read-only streaming access to the merged STE SQLite dataset."""
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import zlib

DATABASE = Path(__file__).resolve().parents[2] / "data/cards.sqlite"
KPGZ_SOURCE = "data-archived/raw/kpgz.json"
OKEI_SOURCE = "zakupki-reverse/data/okei.parquet"


def unpack(payload):
    return json.loads(zlib.decompress(payload))


def connect(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    db.execute("PRAGMA cache_size=-8192")
    db.execute("PRAGMA temp_store=FILE")
    db.execute("PRAGMA query_only=ON")
    return db


def adapt_card(row, source, field_sources, conflicts):
    """Translate compact columns into the evaluation's card interface; no ground truth in text."""
    characteristics = []
    for ch in row.get("characteristics") or []:
        item = {"productCharacteristicValue": {
            "productCharacteristicName": ch["name"],
            "productCharacteristicUnitName": ch.get("unit_name") or ch.get("unit"),
            "productCharacteristicDescriptionTypeId": ch.get("type_id"),
            "productCharacteristicId": ch.get("characteristic_id")}}
        if ch.get("value_bool") is not None:
            item["characteristicValueBoolValue"] = ch["value_bool"]
        elif ch.get("value_number") is not None:
            item["characteristicValueDecimalValue"] = ch["value_number"]
        elif row.get("archived_columns") and re.fullmatch(r"-?\d+(?:[.,]\d+)?", ch.get("value") or ""):
            item["characteristicValueDecimalValue"] = float(ch["value"].replace(",", "."))
        else:
            item["characteristicValueStringValue"] = ch.get("value")
        characteristics.append(item)
    return {"id": row["id"], "name": row.get("name"), "modelName": row.get("model"),
            "description": row.get("description"),
            "production": {"code": row.get("production_code"), "name": row.get("production_name"),
                           "okpd": {"code": row.get("okpd2_code"), "name": row.get("okpd2_name")}},
            "productionDirectoryId": row.get("category_id"),
            "productionDirectory": {"name": row.get("category")},
            "skuCharacteristics": characteristics,
            "_dataset": {"primary_source": source, "field_sources": field_sources,
                         "conflicting_fields": conflicts}}


def iter_cards(path, ste_id=None):
    with closing(connect(path)) as db:
        sql = "SELECT payload,primary_source,field_sources,conflicting_fields FROM cards"
        params = ()
        if ste_id is not None:
            sql += " WHERE id=?"
            params = (ste_id,)
        else:
            sql += " WHERE production_code IS NOT NULL"
        sql += " ORDER BY id"
        for payload, source, fields, conflicts in db.execute(sql, params):
            yield adapt_card(unpack(payload), source, unpack(fields), json.loads(conflicts))


def find_card(path, ste_id):
    card = next(iter_cards(path, ste_id), None)
    if card is None:
        raise ValueError(f"СТЕ {ste_id} нет в {path}")
    return card


def iter_nodes(path, code=None):
    with closing(connect(path)) as db:
        sql = "SELECT payload FROM catalog_entries WHERE source=?"
        params = (KPGZ_SOURCE,)
        if code is not None:
            sql += " AND key=?"
            params += (code,)
        for (payload,) in db.execute(sql + " ORDER BY row_number", params):
            yield unpack(payload)


def load_okei(path):
    with closing(connect(path)) as db:
        return {row["code"]: row["name"].replace(";^", " / ").replace("^", " ")
                for (payload,) in db.execute("SELECT payload FROM catalog_entries WHERE source=?", (OKEI_SOURCE,))
                for row in [unpack(payload)]}
