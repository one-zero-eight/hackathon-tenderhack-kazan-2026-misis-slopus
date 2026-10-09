#!/usr/bin/env python3
"""Convert only actual KTRU XML parts to nested, Zstandard-compressed Parquet."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
from pathlib import Path
from typing import Callable
import xml.etree.ElementTree as ET
import zipfile

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
OOS = "http://zakupki.gov.ru/oos/types/1"
CMN = "http://zakupki.gov.ru/oos/common/1"
EXPORT = "http://zakupki.gov.ru/oos/export/1"


@dataclass(frozen=True)
class Field:
    xml: str
    name: str
    type: pa.DataType
    convert: Callable | None = None
    children: tuple[Field, ...] = ()
    item: Field | None = None
    namespace: str = OOS

    @property
    def tag(self) -> str:
        return f"{{{self.namespace}}}{self.xml}"


def string(xml: str, name: str | None = None, namespace: str = OOS) -> Field:
    return Field(xml, name or xml, pa.string(), str, namespace=namespace)


def boolean(value: str) -> bool:
    return {"true": True, "false": False, "1": True, "0": False}[value.strip()]


def scalar(xml: str, name: str, kind: pa.DataType, convert: Callable) -> Field:
    return Field(xml, name, kind, convert)


def struct(xml: str, name: str, *children: Field, namespace: str = OOS) -> Field:
    return Field(xml, name, pa.struct([pa.field(c.name, c.type) for c in children]), children=children, namespace=namespace)


def listing(xml: str, name: str, item: Field) -> Field:
    return Field(xml, name, pa.list_(pa.field("element", item.type)), item=item)


UNIT = struct("OKEI", "unit", string("code"), string("name"))
RANGE = struct("valueRange", "range", string("minMathNotation", "min_operator"),
               string("min"), string("maxMathNotation", "max_operator"), string("max"))
VALUE = struct("value", "value", string("valueCode", "code"), UNIT,
               string("qualityDescription", "text"), string("valueFormat", "format"),
               listing("rangeSet", "ranges", RANGE),
               listing("valueSet", "concrete_values", string("concreteValue", "value")))
CHARACTERISTIC = struct("characteristic", "characteristic", string("code"), string("name"),
                        string("type"), string("kind"),
                        scalar("actual", "actual", pa.bool_(), boolean),
                        scalar("isRequired", "required", pa.bool_(), boolean),
                        string("choiceType", "choice_type"), listing("values", "values", VALUE))
CLASSIFIER_VALUE = struct("value", "value", string("code"), string("name"), string("descriptionValue", "description"))
CLASSIFIER = struct("classifier", "classifier", string("name"), string("NSIcode", "nsi_code"),
                    string("code"), string("source"), listing("values", "values", CLASSIFIER_VALUE))
DATA = struct("data", "data",
    string("code"), string("versionCode", "version_code"), scalar("version", "version", pa.int32(), int),
    string("name"), string("status"), string("inclusionDate", "inclusion_date"),
    string("publishDate", "publish_date"), string("updateDate", "update_date"),
    string("applicationDateStart", "application_date_start"), string("applicationDateEnd", "application_date_end"),
    struct("OKPD2", "okpd2", string("code"), string("name")), listing("OKEIs", "units", UNIT),
    struct("NSI", "nsi", listing("classifiers", "classifiers", CLASSIFIER),
           listing("standarts", "standards", struct("standart", "standard", string("docName", "document_name")))),
    listing("characteristics", "characteristics", CHARACTERISTIC),
    listing("rubricators", "rubricators", struct("rubricatorInfo", "rubricator", string("code"), string("name"))),
    string("nsiDescription", "description"), scalar("isTemplate", "is_template", pa.bool_(), boolean),
    string("externalCode", "external_code"), scalar("noNewFeatures", "no_new_features", pa.bool_(), boolean),
    string("noNewFeaturesReason", "no_new_features_reason"),
    struct("cancelInfo", "cancellation", string("cancelDate", "date"), string("cancelReason", "reason")),
    struct("parentPositionInfo", "parent", string("code"), scalar("version", "version", pa.int32(), int), string("externalCode", "external_code")),
    listing("attachments", "attachments", struct("attachment", "attachment", string("fileName", "file_name"),
            string("fileSize", "file_size"), string("docDescription", "description"), string("docDate", "date"), string("url"))),
)
PRINT_FORM = struct("printForm", "print_form", string("url", namespace=CMN),
                    struct("signatureInfo", "signature", string("dsUid", "ds_uid", namespace=CMN), namespace=CMN))
POSITION = struct("position", "position", DATA, PRINT_FORM)
SCHEMA = pa.schema([
    pa.field("source_zip", pa.string(), nullable=False),
    pa.field("source_xml", pa.string(), nullable=False),
    pa.field("source_row", pa.int32(), nullable=False),
    *[pa.field(c.name, c.type) for c in DATA.children],
    pa.field("print_form", PRINT_FORM.type),
])


def parse(element: ET.Element, spec: Field):
    if element.tag != spec.tag or element.attrib:
        raise ValueError(f"Unexpected XML element/attributes: {element.tag} {element.attrib}")
    if spec.convert:
        if len(element):
            raise ValueError(f"Nested content in scalar: {element.tag}")
        return spec.convert(element.text or "")
    if element.text and element.text.strip():
        raise ValueError(f"Mixed XML content: {element.tag}")
    if any(c.tail and c.tail.strip() for c in element):
        raise ValueError(f"Mixed XML tail content: {element.tag}")
    if spec.item:
        return [parse(c, spec.item) for c in element]
    fields = {c.tag: c for c in spec.children}
    result = {c.name: None for c in spec.children}
    seen = set()
    for child in element:
        if child.tag not in fields or child.tag in seen:
            raise ValueError(f"Unknown or repeated field in {element.tag}: {child.tag}")
        seen.add(child.tag)
        field = fields[child.tag]
        result[field.name] = parse(child, field)
    return result


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def row_bytes(row: dict) -> bytes:
    return (json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def write_metadata(path: Path, metadata: dict) -> None:
    temporary = Path(str(path) + ".part")
    with temporary.open("wb") as output:
        with gzip.GzipFile(filename="", fileobj=output, mode="wb", mtime=0) as compressed:
            compressed.write((json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode())
    temporary.replace(path)


def convert(source: Path, output: Path, batch_size: int = 1000) -> dict:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if source.resolve() == output.resolve():
        raise ValueError("Source and output must differ")
    source_hash = sha256(source)
    schema = SCHEMA.with_metadata({
        b"ktru.schema_version": b"1",
        b"ktru.source_sha256": source_hash.encode(),
        b"ktru.scope": b"actual: latest versions, including TERMINATED; not ACTIVE-only",
        b"ktru.strings": b"Exact XML text; dates retain ISO offsets; numeric bounds retain decimal text; absent=null, present empty string='', present empty list=[]",
        b"ktru.identity": b"source_zip + source_xml + source_row",
        b"ktru.conflict_policy": b"exclude all rows of conflicting code + version pairs",
    })
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(str(output) + ".part")
    counts = Counter()
    statuses = Counter()
    keys = defaultdict(list)
    parts = []
    skipped = []
    expected_digest = hashlib.sha256()
    rows = []
    with pq.ParquetWriter(temporary, schema, compression="zstd", compression_level=9, write_statistics=True) as writer:
        with zipfile.ZipFile(source) as outer:
            for member in outer.infolist():
                if member.is_dir():
                    continue
                if not member.filename.endswith(".zip"):
                    raise ValueError(f"Unexpected outer member: {member.filename}")
                with zipfile.ZipFile(io.BytesIO(outer.read(member))) as inner:
                    for info in inner.infolist():
                        if info.is_dir():
                            continue
                        if info.filename.startswith("nsiKTRUNew_all_not-actual_") and info.filename.endswith(".xml"):
                            skipped.append(info.filename)
                            continue
                        if not (info.filename.startswith("nsiKTRUNew_all_actual_") and info.filename.endswith(".xml")):
                            raise ValueError(f"Unexpected inner member: {info.filename}")
                        record_count = 0
                        with inner.open(info) as stream:
                            events = ET.iterparse(stream, events=("start", "end"))
                            _, root = next(events)
                            if root.tag != f"{{{EXPORT}}}export" or root.attrib:
                                raise ValueError(f"Unexpected root: {root.tag}")
                            stack = [root.tag]
                            for event, element in events:
                                if event == "start":
                                    if len(stack) == 1 and element.tag != f"{{{EXPORT}}}nsiKTRUs":
                                        raise ValueError(f"Unexpected export child: {element.tag}")
                                    if len(stack) == 2 and element.tag != POSITION.tag:
                                        raise ValueError(f"Unexpected nsiKTRUs child: {element.tag}")
                                    stack.append(element.tag)
                                    continue
                                if element.tag == POSITION.tag:
                                    position = parse(element, POSITION)
                                    data = position["data"]
                                    if not data or not data["code"] or data["version"] is None:
                                        raise ValueError("Missing KTRU identity")
                                    record_count += 1
                                    row = {"source_zip": member.filename, "source_xml": info.filename,
                                           "source_row": record_count, **data, "print_form": position["print_form"]}
                                    expected_digest.update(row_bytes(row))
                                    statuses[row["status"]] += 1
                                    counts["records"] += 1
                                    counts["characteristics"] += len(row["characteristics"] or [])
                                    counts["values"] += sum(len(c["values"] or []) for c in row["characteristics"] or [])
                                    content_hash = hashlib.sha256(row_bytes(position)).hexdigest()
                                    keys[(row["code"], row["version"])].append({
                                        "name": row["name"], "source_zip": member.filename,
                                        "source_xml": info.filename, "source_row": record_count,
                                        "content_sha256": content_hash,
                                    })
                                    rows.append(row)
                                    if len(rows) >= batch_size:
                                        writer.write_table(pa.Table.from_pylist(rows, schema=schema))
                                        rows.clear()
                                    element.clear()
                                    root.clear()
                                stack.pop()
                        parts.append({"zip": member.filename, "xml": info.filename, "xml_bytes": info.file_size, "records": record_count})
                        print(f"ACTUAL parts={len(parts)} records={counts['records']}", flush=True)
        if rows:
            writer.write_table(pa.Table.from_pylist(rows, schema=schema))
    if not counts["records"]:
        raise ValueError("No actual records found")
    parquet = pq.ParquetFile(temporary)
    if not parquet.schema_arrow.equals(schema, check_metadata=True):
        raise ValueError("Parquet schema round-trip mismatch")
    actual_digest = hashlib.sha256()
    read_count = 0
    for batch in parquet.iter_batches(batch_size=batch_size):
        for row in batch.to_pylist():
            actual_digest.update(row_bytes(row))
            read_count += 1
    if read_count != counts["records"] or actual_digest.digest() != expected_digest.digest():
        raise ValueError("Parquet full-data round-trip mismatch")
    duplicates = [{"code": code, "version": version, "conflicting": len({v['content_sha256'] for v in values}) > 1, "records": values}
                  for (code, version), values in sorted(keys.items()) if len(values) > 1]
    conflicts = {(item["code"], item["version"]) for item in duplicates if item["conflicting"]}
    excluded_records = 0
    if conflicts:
        cleaned = Path(str(output) + ".clean.part")
        counts.clear()
        statuses.clear()
        expected_digest = hashlib.sha256()
        with pq.ParquetWriter(cleaned, schema, compression="zstd", compression_level=9, write_statistics=True) as writer:
            for batch in parquet.iter_batches(batch_size=batch_size):
                kept = []
                for row in batch.to_pylist():
                    if (row["code"], row["version"]) in conflicts:
                        excluded_records += 1
                        continue
                    expected_digest.update(row_bytes(row))
                    statuses[row["status"]] += 1
                    counts["records"] += 1
                    counts["characteristics"] += len(row["characteristics"] or [])
                    counts["values"] += sum(len(c["values"] or []) for c in row["characteristics"] or [])
                    kept.append(row)
                if kept:
                    writer.write_table(pa.Table.from_pylist(kept, schema=schema))
        cleaned_parquet = pq.ParquetFile(cleaned)
        actual_digest = hashlib.sha256()
        read_count = 0
        for batch in cleaned_parquet.iter_batches(batch_size=batch_size):
            for row in batch.to_pylist():
                actual_digest.update(row_bytes(row))
                read_count += 1
        if (not cleaned_parquet.schema_arrow.equals(schema, check_metadata=True)
                or read_count != counts["records"] or actual_digest.digest() != expected_digest.digest()):
            raise ValueError("Filtered Parquet round-trip mismatch")
        cleaned.replace(temporary)
        for key in conflicts:
            del keys[key]
    metadata = {
        "format": "parquet", "compression": "zstd", "compressionLevel": 9, "schemaVersion": 1,
        "source": str(source), "sourceKind": "EIS-format XML archive; archive provenance not independently verified",
        "sourceSha256": source_hash, "sourceBytes": source.stat().st_size,
        "scope": "actual only: latest versions including TERMINATED; not ACTIVE-only",
        "recordCount": counts["records"], "uniqueCodeCount": len({code for code, _ in keys}),
        "uniqueCodeVersionCount": len(keys), "statuses": dict(statuses),
        "characteristicCount": counts["characteristics"], "valueCount": counts["values"],
        "actualPartCount": len(parts), "skippedHistoricalPartCount": len(skipped),
        "actualXmlBytes": sum(p["xml_bytes"] for p in parts), "parts": parts,
        "duplicateCodeVersions": [item for item in duplicates if not item["conflicting"]],
        "excludedConflictingCodeVersions": [item for item in duplicates if item["conflicting"]],
        "excludedRecordCount": excluded_records,
        "sourceRecordCount": sum(p["records"] for p in parts),
        "duplicatePolicy": "exclude all source rows of conflicting code + version pairs; preserve exact duplicates",
        "rowIdentity": ["source_zip", "source_xml", "source_row"],
        "stringPolicy": "XML text is preserved without trimming. Numeric bounds and ISO dates are strings, preserving precision and timezone offsets. Missing optional fields are null; present empty scalar/list is empty string/list.",
        "schema": str(schema), "xmlFieldMapping": field_mapping(POSITION),
        "validated": True, "validation": "Full Parquet read-back: identical canonical row SHA-256 and schema",
        "rowContentSha256": expected_digest.hexdigest(), "bytes": temporary.stat().st_size,
        "sha256": sha256(temporary), "exportedAt": datetime.now(timezone.utc).isoformat(),
    }
    temporary.replace(output)
    write_metadata(Path(str(output) + ".meta.json.gz"), metadata)
    print(f"EXPORTED {output}: {counts['records']} rows, {metadata['bytes']} bytes; validated", flush=True)
    return metadata


def field_mapping(field: Field, xml_path: str = "", parquet_path: str = "") -> list[dict]:
    xml_path = f"{xml_path}/{field.xml}"
    parquet_path = f"{parquet_path}.{field.name}".strip(".")
    if field.convert:
        return [{"xml": xml_path, "parquet": parquet_path.removeprefix("position.data.").removeprefix("position."), "type": str(field.type)}]
    if field.item:
        values = field_mapping(field.item, xml_path, parquet_path + "[]")
        item_path = (parquet_path + "[]." + field.item.name).removeprefix("position.data.").removeprefix("position.")
        list_path = (parquet_path + "[]").removeprefix("position.data.").removeprefix("position.")
        for value in values:
            value["parquet"] = value["parquet"].replace(item_path, list_path, 1)
        return values
    return [v for child in field.children for v in field_mapping(child, xml_path, parquet_path)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "data/КТРУ/КТРУ xml.zip")
    parser.add_argument("--out", type=Path, default=ROOT / "data/ktru-actual.parquet")
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()
    convert(args.source, args.out, args.batch_size)


if __name__ == "__main__":
    main()
