#!/usr/bin/env python3
"""Convert catalog JSON / JSON.gz arrays to Zstandard Parquet."""
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

import ijson
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DEPRECATED = DATA / "deprecated"
# Preferred catalogs land in data/; EAIST/portal archive JSON stay under deprecated/.
DEFAULTS = {
    DEPRECATED / "pp-kpgz.json": DATA / "pp-kpgz.parquet",
    DEPRECATED / "ste-categories.json": DATA / "ste-categories.parquet",
    DEPRECATED / "spgz.json": DATA / "spgz.parquet",
    DEPRECATED / "okpd2-moy-zakupki.json.gz": DATA / "okpd2-moy-zakupki.parquet",
    DEPRECATED / "kpgz.json": DEPRECATED / "kpgz.parquet",
    DEPRECATED / "okpd2.json": DEPRECATED / "okpd2.parquet",
}


def load_records(source: Path) -> list[dict]:
    if source.name.endswith(".json.gz"):
        with gzip.open(source, "rb") as stream:
            prefix = stream.read(256).lstrip()
            stream.seek(0)
            if not prefix.startswith(b"["):
                raise ValueError(f"Expected JSON array: {source}")
            return list(ijson.items(stream, "item"))
    if source.suffix == ".json":
        if source.stat().st_size > 100_000_000:
            with source.open("rb") as stream:
                return list(ijson.items(stream, "item"))
        data = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"Expected JSON array: {source}")
        return data
    raise ValueError(f"Unsupported source: {source}")


def cast_null_types(field: pa.Field) -> pa.Field:
    """Replace Arrow null placeholders with nullable string/list types."""
    if pa.types.is_null(field.type):
        name = field.name.lower()
        if name.endswith("id") or name.endswith("date") or name in {"buyover", "usefullymodels"}:
            return pa.field(field.name, pa.int64() if name.endswith("id") or name == "buyover" else pa.string(), nullable=True)
        return pa.field(field.name, pa.string(), nullable=True)
    if pa.types.is_struct(field.type):
        return pa.field(field.name, pa.struct([cast_null_types(child) for child in field.type]), nullable=field.nullable)
    if pa.types.is_list(field.type):
        item = cast_null_types(pa.field("item", field.type.value_type))
        return pa.field(field.name, pa.list_(item), nullable=field.nullable)
    return field


def default_output(source: Path) -> Path:
    name = source.name
    if name.endswith(".json.gz"):
        return source.with_name(name.removesuffix(".json.gz") + ".parquet")
    if name.endswith(".json"):
        return source.with_suffix(".parquet")
    raise ValueError(f"Unsupported source: {source}")


def convert(source: Path, output: Path | None = None) -> Path:
    records = load_records(source)
    table = pa.Table.from_pylist(records)
    schema = pa.schema([cast_null_types(field) for field in table.schema])
    table = table.cast(schema)
    target = output or default_output(source)
    partial = Path(str(target) + ".part")
    pq.write_table(table, partial, compression="zstd", compression_level=9, write_statistics=True)
    written = pq.ParquetFile(partial)
    if written.metadata.num_rows != len(records):
        raise ValueError(f"Row count mismatch for {source}: {written.metadata.num_rows} != {len(records)}")
    partial.replace(target)
    print(f"WROTE {target.relative_to(ROOT)} records={len(records)} bytes={target.stat().st_size}", flush=True)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="*", type=Path, help="JSON / JSON.gz arrays; default: main catalogs")
    parser.add_argument("--out-dir", type=Path, help="Optional output directory")
    args = parser.parse_args()
    if args.sources:
        jobs = [(source.resolve(), None if not args.out_dir else (args.out_dir / default_output(source).name).resolve()) for source in args.sources]
    else:
        jobs = [(source, target if not args.out_dir else args.out_dir / target.name) for source, target in DEFAULTS.items() if source.exists()]
    for source, output in jobs:
        convert(source, output)


if __name__ == "__main__":
    main()
