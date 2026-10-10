#!/usr/bin/env python3
"""Build meow-embed LMDB cache for STE parquet catalogs (document task)."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx
import pyarrow.parquet as pq
from tqdm import tqdm

# Prefer sibling meow-embed checkout / venv.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "meow-embed"))

from meow_embed import EmbedCache, MeowEmbedClient  # noqa: E402

MAX_CHARACTERISTICS = 24
MAX_CHAR_VALUE_LEN = 120
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


def load_docs(paths: list[Path]) -> list[str]:
    """Load documents; later files win on id collision (call with ste last so categories win if we reverse…).

    Call order: categories first, then ste — first id wins (categories preferred).
    """
    seen: set[int] = set()
    docs: list[str] = []
    for path in paths:
        print(f"load {path.name}…", flush=True)
        table = pq.read_table(path, columns=COLUMNS)
        n_new = 0
        for batch in table.to_batches(max_chunksize=5000):
            for row in batch.to_pylist():
                pid = int(row["id"] or 0)
                if pid <= 0 or pid in seen:
                    continue
                doc = embedding_document(row)
                if not doc:
                    continue
                seen.add(pid)
                docs.append(doc)
                n_new += 1
        print(f"  +{n_new:,} (total {len(docs):,})", flush=True)
    return docs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--meow-url", default="http://127.0.0.1:8067")
    parser.add_argument("--batch", type=int, default=32)
    parser.add_argument("--map-size-gb", type=float, default=4.0)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    data = args.data_dir
    paths = [
        data / "Инструменты_from_api.parquet",
        data / "Мебель_from_api.parquet",
        data / "Одежда_from_api.parquet",
        data / "ste.parquet",
    ]
    missing = [p for p in paths if not p.exists()]
    if missing:
        raise SystemExit(f"missing: {missing}")

    docs = load_docs(paths)
    if args.limit:
        docs = docs[: args.limit]
    print(f"documents: {len(docs):,}", flush=True)

    args.cache.parent.mkdir(parents=True, exist_ok=True)
    map_size = int(args.map_size_gb * 1024**3)
    cache = EmbedCache.open(args.cache, map_size=map_size)
    client = MeowEmbedClient(
        httpx.Client(base_url=args.meow_url, timeout=600.0),
        cache=cache,
    )
    models = client.models()
    print("models:", json.dumps(models, ensure_ascii=False)[:200], flush=True)

    started = time.perf_counter()
    done = 0
    try:
        with tqdm(total=len(docs), unit="шт", dynamic_ncols=True) as bar:
            for i in range(0, len(docs), args.batch):
                chunk = docs[i : i + args.batch]
                client.embed(
                    {
                        "texts": chunk,
                        "dense_model_id": "google/embeddinggemma-2",
                        "dense_task": "document",
                        "dense_prompt": "",
                    },
                    use_cache=True,
                )
                done += len(chunk)
                bar.update(len(chunk))
                elapsed = max(time.perf_counter() - started, 1e-9)
                bar.set_postfix_str(f"{done / elapsed:.1f}/s")
    finally:
        cache.close()

    elapsed = time.perf_counter() - started
    print(f"done {done:,} in {elapsed / 60:.1f} min → {args.cache}", flush=True)


if __name__ == "__main__":
    main()
