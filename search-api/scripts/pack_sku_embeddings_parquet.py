#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "lmdb>=1.5",
#   "numpy>=2",
#   "pyarrow>=18",
# ]
# ///
"""Pack sku_id LMDB → dense parquet for Git LFS (zstd).

Columns: id:int64, embedding:fixed_size_binary(1536) = float16[768] LE.

Usage:
  uv run search-api/scripts/pack_sku_embeddings_parquet.py \\
    --input data/sku_embeddings.lmdb --output data/sku_embeddings.parquet
"""

from __future__ import annotations

import argparse
import struct
from pathlib import Path

import lmdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

VECTOR_BYTES = 768 * 2


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="sku_id-keyed LMDB dir")
    parser.add_argument("--output", type=Path, required=True, help="output .parquet")
    parser.add_argument("--compression", default="zstd")
    args = parser.parse_args()

    env = lmdb.open(str(args.input.resolve()), readonly=True, lock=False, readahead=False)
    ids: list[int] = []
    vecs: list[bytes] = []
    try:
        with env.begin() as txn:
            for key, value in txn.cursor():
                if len(value) != VECTOR_BYTES:
                    raise SystemExit(f"bad vector size {len(value)} for key {key!r}")
                ids.append(struct.unpack("<Q", key)[0])
                vecs.append(bytes(value))
    finally:
        env.close()

    order = np.argsort(np.asarray(ids, dtype=np.int64))
    ids_sorted = np.asarray(ids, dtype=np.int64)[order]
    vecs_sorted = [vecs[int(i)] for i in order]
    table = pa.table({
        "id": pa.array(ids_sorted, type=pa.int64()),
        "embedding": pa.array(vecs_sorted, type=pa.binary(VECTOR_BYTES)),
    })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, args.output, compression=args.compression, compression_level=3)
    print(f"wrote {len(ids_sorted):,} rows → {args.output} ({args.output.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
