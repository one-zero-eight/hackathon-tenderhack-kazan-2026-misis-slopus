#!/usr/bin/env python3
"""Compact STE dumps into a shared Zstandard Parquet schema.

Supports:
  - QueryIndex category dumps: *_from_api.jsonl.gz → *_from_api.parquet
  - UI GetSku dumps: *_getsku.jsonl.gz → *_getsku.parquet
  - Merged catalog: ste.jsonl.gz + ste-details.jsonl.gz → ste.parquet
"""
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DEFAULT_SOURCES = (
    sorted((DATA / "deprecated").glob("*_from_api.jsonl.gz"))
    + sorted(DATA.glob("*_from_api.jsonl.gz"))
    + sorted(DATA.glob("*_getsku.jsonl.gz"))
)

CHAR_SCHEMA = pa.struct([
    ("name", pa.string()),
    ("value", pa.string()),
    ("value_number", pa.float64()),
    ("value_bool", pa.bool_()),
    ("unit", pa.string()),
    ("unit_name", pa.string()),
    ("type_id", pa.int64()),
    ("characteristic_id", pa.int64()),
    ("value_id", pa.int64()),
])

ROW_SCHEMA = pa.schema([
    ("id", pa.int64()),
    ("entity_id", pa.int64()),
    ("name", pa.string()),
    ("model", pa.string()),
    ("manufacturer_id", pa.int64()),
    ("manufacturer", pa.string()),
    ("barcode", pa.string()),
    ("description", pa.string()),
    ("okei_id", pa.int64()),
    ("okei", pa.string()),
    ("okpd_id", pa.int64()),
    ("okpd_path", pa.string()),
    ("okpd2_code", pa.string()),
    ("okpd2_name", pa.string()),
    ("production_code", pa.string()),
    ("production_name", pa.string()),
    ("category_id", pa.int64()),
    ("category", pa.string()),
    ("category_path", pa.string()),
    ("cost_per_unit", pa.float64()),
    ("median_price", pa.float64()),
    ("reference_price", pa.float64()),
    ("offers_count", pa.int64()),
    ("created_date", pa.string()),
    ("oksm_id", pa.int64()),
    ("oksm", pa.string()),
    ("state_id", pa.int64()),
    ("entity_state", pa.int64()),
    ("is_demanded", pa.bool_()),
    ("is_innovation", pa.bool_()),
    ("is_moscow_production", pa.bool_()),
    ("is_manual_applied_domestic", pa.bool_()),
    ("has_gisp", pa.bool_()),
    ("has_actual_gisp", pa.bool_()),
    ("has_linked_gisp", pa.bool_()),
    ("has_offer_with_discounts", pa.bool_()),
    ("sku_image_id", pa.int64()),
    ("sku_image_ids", pa.list_(pa.int64())),
    ("characteristics", pa.list_(CHAR_SCHEMA)),
])


def fmt_num(value: float) -> str:
    return f"{value:.12g}"


def coalesce(*values):
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if value == []:
            continue
        return value
    return None


def compact_characteristic(item: dict) -> dict | None:
    meta = item.get("productCharacteristicValue") or {}
    name = (meta.get("productCharacteristicName") or "").strip()
    if not name:
        return None
    unit = meta.get("productCharacteristicUnitDesignationNational")
    unit_name = meta.get("productCharacteristicUnitName")
    value = None
    value_number = None
    value_bool = None
    if item.get("characteristicValueBoolValue") is not None:
        value_bool = bool(item["characteristicValueBoolValue"])
        value = "Да" if value_bool else "Нет"
        unit = None
        unit_name = None
    elif item.get("characteristicValueDecimalValue") is not None:
        value_number = float(item["characteristicValueDecimalValue"])
        value = fmt_num(value_number)
    elif item.get("characteristicValueIntValue") is not None:
        value_number = float(item["characteristicValueIntValue"])
        value = fmt_num(value_number)
    elif item.get("characteristicValueDateTimeValue") is not None:
        value = str(item["characteristicValueDateTimeValue"])
    else:
        raw = item.get("characteristicValueStringValue")
        if raw is None or not str(raw).strip():
            return None
        value = " ".join(str(raw).split())
    return {
        "name": name,
        "value": value,
        "value_number": value_number,
        "value_bool": value_bool,
        "unit": unit,
        "unit_name": unit_name,
        "type_id": meta.get("productCharacteristicDescriptionTypeId"),
        "characteristic_id": meta.get("productCharacteristicId"),
        "value_id": item.get("productCharacteristicValueId") or meta.get("id"),
    }


def compact_characteristics(items) -> list[dict]:
    out = []
    for item in items or []:
        compact = compact_characteristic(item)
        if compact is not None:
            out.append(compact)
    return out


def load_okpd_by_production(path: Path) -> dict[str, tuple[str | None, str | None]]:
    if not path.exists():
        return {}
    mapping = {}
    for row in pq.read_table(path, columns=["code", "okpd"]).to_pylist():
        okpd = row.get("okpd") or {}
        mapping[row["code"]] = (okpd.get("code"), okpd.get("name"))
    return mapping


def resolve_okpd2(production_code, explicit_code, explicit_name, okpd_by_production):
    joined_code, joined_name = okpd_by_production.get(production_code, (None, None))
    code = coalesce(explicit_code, joined_code)
    name = coalesce(explicit_name, joined_name if code == joined_code else None)
    if code and not name and joined_code == code:
        name = joined_name
    return code, name


def image_ids(details: dict, list_row: dict | None = None) -> list[int]:
    images = details.get("images")
    if isinstance(images, list) and images:
        ids = []
        for image in images:
            file_id = image.get("fileStorageId")
            if file_id is None:
                file_id = (image.get("fileStorage") or {}).get("id")
            if file_id is None:
                file_id = image.get("id")
            if file_id is not None:
                ids.append(int(file_id))
        if ids:
            return ids
    ids = details.get("skuImageIds")
    if ids:
        return [int(x) for x in ids if x is not None]
    if details.get("skuImageId") is not None:
        return [int(details["skuImageId"])]
    if list_row and list_row.get("imageId") is not None:
        return [int(list_row["imageId"])]
    return []


def convert_row(raw: dict, okpd_by_production: dict[str, tuple[str | None, str | None]], list_row: dict | None = None) -> dict:
    production_code = coalesce(raw.get("productionCode"), (list_row or {}).get("productionCode"))
    okpd2_code, okpd2_name = resolve_okpd2(
        production_code, (list_row or {}).get("okpdCode"), None, okpd_by_production
    )
    ids = image_ids(raw, list_row)
    return {
        "id": raw.get("id"),
        "entity_id": coalesce(raw.get("entityId"), (list_row or {}).get("entityId")),
        "name": coalesce(raw.get("name"), (list_row or {}).get("name")),
        "model": coalesce(raw.get("modelName"), (list_row or {}).get("modelName")),
        "manufacturer_id": raw.get("manufacturerId"),
        "manufacturer": coalesce(raw.get("manufacturerName"), (list_row or {}).get("vendorName")),
        "barcode": coalesce(raw.get("barcode"), (list_row or {}).get("barcode")),
        "description": raw.get("description"),
        "okei_id": coalesce(raw.get("okeiId"), (list_row or {}).get("okeiId")),
        "okei": coalesce(raw.get("okeiName"), (list_row or {}).get("okeiName")),
        "okpd_id": raw.get("okpdId"),
        "okpd_path": raw.get("okpdPath"),
        "okpd2_code": okpd2_code,
        "okpd2_name": okpd2_name,
        "production_code": production_code,
        "production_name": raw.get("productionName"),
        "category_id": coalesce(raw.get("productionDirectoryId"), (list_row or {}).get("productionDirectoryId")),
        "category": raw.get("productionDirectoryName"),
        "category_path": raw.get("productionDirectoryPath"),
        "cost_per_unit": coalesce(raw.get("costPerUnit"), (list_row or {}).get("costPerUnit")),
        "median_price": coalesce(raw.get("medianPrice"), (list_row or {}).get("medianPrice")),
        "reference_price": coalesce(raw.get("referencePrice"), (list_row or {}).get("referencePrice")),
        "offers_count": raw.get("offersCount"),
        "created_date": coalesce(raw.get("createdDate"), (list_row or {}).get("createDate")),
        "oksm_id": raw.get("oksmId"),
        "oksm": raw.get("oksmName"),
        "state_id": raw.get("stateId"),
        "entity_state": raw.get("entityState"),
        "is_demanded": coalesce(raw.get("isDemanded"), (list_row or {}).get("isDemanded")),
        "is_innovation": raw.get("isInnovation"),
        "is_moscow_production": raw.get("isMoscowProduction"),
        "is_manual_applied_domestic": raw.get("isManualAppliedDomestic"),
        "has_gisp": raw.get("hasGisp"),
        "has_actual_gisp": raw.get("hasActualGisp"),
        "has_linked_gisp": raw.get("hasLinkedGisp"),
        "has_offer_with_discounts": raw.get("hasOfferWithDiscounts"),
        "sku_image_id": ids[0] if ids else None,
        "sku_image_ids": ids,
        "characteristics": compact_characteristics(raw.get("skuCharacteristics")),
    }


def convert_getsku_row(details: dict, list_row: dict, okpd_by_production: dict[str, tuple[str | None, str | None]]) -> dict:
    production = details.get("production") or {}
    okpd = production.get("okpd") or {}
    directory = details.get("productionDirectory") or {}
    production_code = coalesce(production.get("code"), details.get("productionCode"), list_row.get("productionCode"))
    okpd2_code, okpd2_name = resolve_okpd2(
        production_code, coalesce(okpd.get("code"), list_row.get("okpdCode")), okpd.get("name"), okpd_by_production
    )
    ids = image_ids(details, list_row)
    return {
        "id": coalesce(details.get("id"), list_row.get("id")),
        "entity_id": coalesce(details.get("entityId"), list_row.get("entityId")),
        "name": coalesce(details.get("name"), list_row.get("name")),
        "model": coalesce(details.get("modelName"), list_row.get("modelName")),
        "manufacturer_id": details.get("manufacturerId"),
        "manufacturer": coalesce(details.get("manufacturerName"), list_row.get("vendorName")),
        "barcode": coalesce(details.get("barcode"), list_row.get("barcode")),
        "description": details.get("description"),
        "okei_id": coalesce(details.get("okeiId"), list_row.get("okeiId")),
        "okei": coalesce(details.get("okeiName"), list_row.get("okeiName")),
        "okpd_id": okpd.get("id"),
        "okpd_path": okpd.get("treePathId"),
        "okpd2_code": okpd2_code,
        "okpd2_name": okpd2_name,
        "production_code": production_code,
        "production_name": production.get("name"),
        "category_id": coalesce(details.get("productionDirectoryId"), directory.get("id"), list_row.get("productionDirectoryId")),
        "category": coalesce(details.get("productionDirectoryName"), directory.get("name")),
        "category_path": coalesce(
            details.get("productionDirectoryTreePathId"),
            directory.get("treePathId"),
            details.get("productionDirectoryPath"),
        ),
        "cost_per_unit": coalesce(details.get("costPerUnit"), list_row.get("costPerUnit")),
        "median_price": coalesce(details.get("medianPrice"), list_row.get("medianPrice")),
        "reference_price": coalesce(details.get("referencePrice"), list_row.get("referencePrice")),
        "offers_count": details.get("offersCount"),
        "created_date": coalesce(details.get("createdDate"), list_row.get("createDate")),
        "oksm_id": details.get("oksmId"),
        "oksm": details.get("oksmName"),
        "state_id": coalesce(details.get("skuStateId"), details.get("stateId")),
        "entity_state": details.get("entityState"),
        "is_demanded": coalesce(details.get("isDemanded"), list_row.get("isDemanded")),
        "is_innovation": details.get("isInnovation"),
        "is_moscow_production": details.get("isMoscowProduction"),
        "is_manual_applied_domestic": details.get("isManualAppliedDomestic"),
        "has_gisp": details.get("hasGisp"),
        "has_actual_gisp": details.get("hasActualGisp"),
        "has_linked_gisp": details.get("hasLinkedGisp"),
        "has_offer_with_discounts": details.get("hasOfferWithDiscounts"),
        "sku_image_id": ids[0] if ids else None,
        "sku_image_ids": ids,
        "characteristics": compact_characteristics(details.get("skuCharacteristics")),
    }


def merge_row(list_row: dict, details: dict, okpd_by_production: dict[str, tuple[str | None, str | None]]) -> dict:
    if list_row.get("id") != details.get("id"):
        raise ValueError(f"ID mismatch: list={list_row.get('id')} details={details.get('id')}")
    if details.get("production") is not None:
        return convert_getsku_row(details, list_row, okpd_by_production)
    return convert_row(details, okpd_by_production, list_row)


def write_rows(rows_iter, output: Path, batch_size: int = 2000) -> tuple[int, int]:
    partial = Path(str(output) + ".part")
    rows = []
    count = 0
    chars = 0
    with pq.ParquetWriter(partial, ROW_SCHEMA, compression="zstd", compression_level=9, write_statistics=True) as writer:
        for row in rows_iter:
            chars += len(row["characteristics"])
            rows.append(row)
            count += 1
            if len(rows) >= batch_size:
                writer.write_table(pa.Table.from_pylist(rows, schema=ROW_SCHEMA))
                rows.clear()
                if count % 50000 == 0:
                    print(f"  … {count}", flush=True)
        if rows:
            writer.write_table(pa.Table.from_pylist(rows, schema=ROW_SCHEMA))
    written = pq.ParquetFile(partial)
    if written.metadata.num_rows != count:
        raise ValueError(f"Row count mismatch for {output}: {written.metadata.num_rows} != {count}")
    partial.replace(output)
    return count, chars


def is_getsku_source(source: Path) -> bool:
    return "_getsku" in source.name


def convert(
    source: Path,
    output: Path,
    okpd_by_production: dict[str, tuple[str | None, str | None]],
    batch_size: int = 2000,
    *,
    getsku: bool | None = None,
) -> None:
    use_getsku = is_getsku_source(source) if getsku is None else getsku

    def rows():
        with gzip.open(source, "rt", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                raw = json.loads(line)
                if use_getsku:
                    yield convert_getsku_row(raw, {}, okpd_by_production)
                else:
                    yield convert_row(raw, okpd_by_production)

    count, chars = write_rows(rows(), output, batch_size)
    print(
        f"WROTE {output.relative_to(ROOT)} records={count} characteristics={chars} "
        f"bytes={output.stat().st_size} source_bytes={source.stat().st_size}",
        flush=True,
    )


def convert_merged_catalog(
    list_path: Path,
    details_path: Path,
    output: Path,
    okpd_by_production: dict[str, tuple[str | None, str | None]],
    batch_size: int = 2000,
) -> None:
    def rows():
        with gzip.open(list_path, "rt", encoding="utf-8") as lists, gzip.open(details_path, "rt", encoding="utf-8") as details:
            for list_line, details_line in zip(lists, details, strict=True):
                yield merge_row(json.loads(list_line), json.loads(details_line), okpd_by_production)

    count, chars = write_rows(rows(), output, batch_size)
    print(
        f"WROTE {output.relative_to(ROOT)} records={count} characteristics={chars} "
        f"bytes={output.stat().st_size} list_bytes={list_path.stat().st_size} details_bytes={details_path.stat().st_size}",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="*", type=Path)
    parser.add_argument("--out-dir", type=Path, default=DATA)
    parser.add_argument("--pp-kpgz", type=Path, default=DATA / "pp-kpgz.parquet")
    parser.add_argument("--merge-catalog", action="store_true", help="Merge ste.jsonl.gz + ste-details.jsonl.gz → ste.parquet")
    parser.add_argument("--list", type=Path, default=None)
    parser.add_argument("--details", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    okpd = load_okpd_by_production(args.pp_kpgz)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    if args.merge_catalog or args.list or args.details:
        list_path = (args.list or DATA / "ste.jsonl.gz").resolve()
        if not list_path.exists():
            list_path = (DATA / "deprecated" / "ste.jsonl.gz").resolve()
        details_path = (args.details or DATA / "ste-details.jsonl.gz").resolve()
        if not details_path.exists():
            details_path = (DATA / "deprecated" / "ste-details.jsonl.gz").resolve()
        output = (args.out or args.out_dir / "ste.parquet").resolve()
        convert_merged_catalog(list_path, details_path, output, okpd)
        return

    sources = [p.resolve() for p in args.sources] if args.sources else [p for p in DEFAULT_SOURCES if p.exists()]
    if not sources:
        raise SystemExit("No *_from_api.jsonl.gz / *_getsku.jsonl.gz sources found")
    for source in sources:
        name = source.name.removesuffix(".jsonl.gz") + ".parquet"
        convert(source, args.out_dir / name, okpd)


if __name__ == "__main__":
    main()
