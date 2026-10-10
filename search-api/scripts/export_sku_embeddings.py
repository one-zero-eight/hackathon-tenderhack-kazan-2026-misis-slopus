#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "lmdb>=1.5",
#   "numpy>=2",
#   "pyarrow>=18",
#   "tqdm>=4.66",
# ]
# ///
"""Remap meow-embed text-keyed LMDB → sku_id-keyed LMDB using *_from_api_old.parquet.

Does not call meow-embed. Looks up vectors for documents built from the old
parquets and stores them as product_id (uint64 LE) → float16[768].

Usage:
  uv run search-api/scripts/export_sku_embeddings.py \\
    --data-dir data --meow-cache /path/to/embedding-cache.lmdb \\
    --output data/sku_embeddings.lmdb
"""

from __future__ import annotations

import argparse
import hashlib
import struct
import sys
from pathlib import Path

import lmdb
import numpy as np
import pyarrow.parquet as pq
from tqdm import tqdm

CATEGORIES = ("Инструменты", "Мебель", "Одежда")
MAX_CHARACTERISTICS = 24
MAX_CHAR_VALUE_LEN = 120
MODEL_ID = "google/embeddinggemma-2"
DIMENSIONS = 768
COLUMNS = [
    "id", "name", "description", "manufacturer", "okei", "oksm",
    "okpd2_code", "okpd2_name", "production_code", "production_name",
    "category_id", "category", "model", "barcode",
    "median_price", "reference_price", "cost_per_unit",
    "sku_image_ids", "characteristics", "created_date",
]


def embedding_document(row: dict) -> str | None:
    name = (row.get("name") or "").strip()
    if not name or not row.get("id"):
        return None
    chars = []
    for item in (row.get("characteristics") or [])[:MAX_CHARACTERISTICS]:
        if not item:
            continue
        cname = (item.get("name") or "").strip()
        value = item.get("value")
        if not cname or value is None or value == "":
            continue
        value = str(value)
        if len(value) > MAX_CHAR_VALUE_LEN:
            value = value[: MAX_CHAR_VALUE_LEN - 1] + "…"
        unit = (item.get("unit") or "").strip()
        chars.append(f"{cname}: {value}" + (f" {unit}" if unit else ""))
    category = (row.get("category") or "").strip() or "Без категории"
    manufacturer = (row.get("manufacturer") or "").strip()
    part = (row.get("barcode") or "").strip()
    okpd2 = (row.get("okpd2_code") or "").strip()
    description = (row.get("description") or "").strip()[:2000]
    parts = [
        f"Категория: {category}",
        f"Производитель: {manufacturer}" if manufacturer else "",
        f"Артикул: {part}" if part else "",
        f"ОКПД2: {okpd2}" if okpd2 else "",
        *chars,
        description,
    ]
    content = "\n".join(p for p in parts if p)
    return f"title: {name} | text: {content}"


def meow_key(text: str) -> bytes:
    return hashlib.blake2b(
        b"\0".join([
            b"dense", MODEL_ID.encode(), (0).to_bytes(4, "little"),
            b"dense_prompt", b"",
            b"dense_task", b"document", text.encode(),
        ]),
        digest_size=32,
    ).digest()


def sku_key(product_id: int) -> bytes:
    return struct.pack("<Q", product_id)


def load_old_docs(data_dir: Path) -> dict[int, str]:
    docs: dict[int, str] = {}
    for name in CATEGORIES:
        path = data_dir / f"{name}_from_api_old.parquet"
        if not path.exists():
            raise SystemExit(f"missing: {path}")
        print(f"load {path.name}…", flush=True)
        n = 0
        table = pq.read_table(path, columns=COLUMNS)
        for batch in table.to_batches(max_chunksize=5000):
            for row in batch.to_pylist():
                pid = int(row["id"] or 0)
                if pid <= 0 or pid in docs:
                    continue
                doc = embedding_document(row)
                if not doc:
                    continue
                docs[pid] = doc
                n += 1
        print(f"  +{n:,} (total {len(docs):,})", flush=True)
    return docs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--meow-cache", type=Path, required=True, help="Text-keyed meow-embed LMDB")
    parser.add_argument("--output", type=Path, required=True, help="New sku_id-keyed LMDB")
    parser.add_argument("--map-size-gb", type=float, default=2.0)
    args = parser.parse_args()

    docs = load_old_docs(args.data_dir.resolve())
    if not docs:
        raise SystemExit("no documents")

    src = lmdb.open(str(args.meow_cache.resolve()), readonly=True, lock=False, readahead=False)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        for child in args.output.iterdir():
            child.unlink()
        args.output.rmdir()
    args.output.mkdir(parents=True, exist_ok=True)
    dst = lmdb.open(str(args.output.resolve()), map_size=int(args.map_size_gb * 1024**3))

    hits = miss = 0
    expected = DIMENSIONS * 2
    try:
        with src.begin() as txn, dst.begin(write=True) as out, tqdm(
            total=len(docs), unit="шт", dynamic_ncols=True
        ) as bar:
            batch: list[tuple[bytes, bytes]] = []
            for pid, doc in docs.items():
                raw = txn.get(meow_key(doc))
                if raw is None:
                    miss += 1
                    bar.update(1)
                    continue
                if len(raw) != expected:
                    raise RuntimeError(f"bad vector bytes for sku {pid}: {len(raw)}")
                batch.append((sku_key(pid), bytes(raw)))
                hits += 1
                if len(batch) >= 4096:
                    for k, v in batch:
                        out.put(k, v)
                    batch.clear()
                bar.update(1)
            for k, v in batch:
                out.put(k, v)
    finally:
        src.close()
        dst.sync()
        dst.close()

    print(
        f"done hits={hits:,} miss={miss:,} → {args.output} "
        f"(entries={hits:,})",
        flush=True,
    )
    if miss:
        print(f"WARNING: {miss:,} SKUs missing from meow cache", file=sys.stderr)
        raise SystemExit(2 if hits == 0 else 0)


if __name__ == "__main__":
    main()
