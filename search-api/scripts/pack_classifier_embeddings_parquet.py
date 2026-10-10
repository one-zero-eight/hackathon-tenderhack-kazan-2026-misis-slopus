#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "numpy>=2",
#   "pyarrow>=18",
#   "pysqlite3-binary>=0.5",
#   "sqlite-vec>=0.1",
# ]
# ///
"""Pack classifier-search.sqlite vectors → dense parquet for Git LFS (zstd).

Columns:
  id:int64 — stable blake2b-64(kind || NUL || code) as uint64 LE,
  kind, code,
  content_hash — blake2b-128 hex of embed_text (version of the vector),
  embedding: fixed_size_binary(1536) = float16[768] LE.

Usage:
  uv run search-api/scripts/pack_classifier_embeddings_parquet.py \\
    --input data/classifier-search.sqlite --output data/classifier_embeddings.parquet
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pysqlite3 as sqlite3
import sqlite_vec

VECTOR_BYTES_F16 = 768 * 2
VECTOR_BYTES_F32 = 768 * 4
SCHEMA = "classifier-embeddings-v2"
BATCH = 4096


def stable_id(kind: str, code: str) -> int:
    digest = hashlib.blake2b(f"{kind}\0{code}".encode("utf-8"), digest_size=8).digest()
    return struct.unpack("<Q", digest)[0]


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="classifier-search.sqlite")
    parser.add_argument("--output", type=Path, required=True, help="output .parquet")
    parser.add_argument("--compression", default="zstd")
    args = parser.parse_args()

    conn = open_db(args.input)
    try:
        meta = dict(conn.execute("SELECT key, value FROM metadata"))
        if meta.get("embeddings_complete") != "1":
            raise SystemExit("embeddings_complete != 1 — сначала доэмбеддьте индекс")
        signature = meta.get("embedding_signature") or ""
        pending = conn.execute(
            "SELECT COUNT(*) FROM entries WHERE embedded_hash IS NULL OR embedded_hash != content_hash"
        ).fetchone()[0]
        if pending:
            raise SystemExit(f"есть {pending} незавершённых эмбеддингов")

        ids: list[int] = []
        kinds: list[str] = []
        codes: list[str] = []
        hashes: list[str] = []
        vecs: list[bytes] = []
        last_id = 0
        while rows := conn.execute(
            """
            SELECT e.id, e.kind, e.code, e.content_hash, v.embedding
            FROM entries e
            JOIN entry_vectors v ON v.entry_id = e.id
            WHERE e.id > ? AND e.embedded_hash = e.content_hash
            ORDER BY e.id
            LIMIT ?
            """,
            (last_id, BATCH),
        ).fetchall():
            for row in rows:
                blob = bytes(row["embedding"])
                if len(blob) != VECTOR_BYTES_F32:
                    raise SystemExit(f"bad vector size {len(blob)} for {row['kind']}:{row['code']}")
                ids.append(stable_id(row["kind"], row["code"]))
                kinds.append(row["kind"])
                codes.append(row["code"])
                hashes.append(row["content_hash"])
                vecs.append(np.frombuffer(blob, dtype="<f4").astype("<f2").tobytes())
            last_id = rows[-1]["id"]
            print(f"packed {len(hashes):,}", flush=True)
    finally:
        conn.close()

    if not hashes:
        raise SystemExit("нет векторов для упаковки")

    order = np.argsort(np.asarray(ids, dtype=np.uint64), kind="stable")
    table = pa.table(
        {
            "id": pa.array(np.asarray(ids, dtype=np.uint64)[order].astype(np.int64), type=pa.int64()),
            "kind": pa.array([kinds[int(i)] for i in order], type=pa.string()),
            "code": pa.array([codes[int(i)] for i in order], type=pa.string()),
            "content_hash": pa.array([hashes[int(i)] for i in order], type=pa.string()),
            "embedding": pa.array([vecs[int(i)] for i in order], type=pa.binary(VECTOR_BYTES_F16)),
        }
    ).replace_schema_metadata({
        "schema": SCHEMA,
        "embedding_signature": signature,
        "dtype": "float16",
        "dimensions": "768",
        "id_key": "blake2b-64(kind || NUL || code)",
        "source": str(args.input.resolve()),
        "counts": json.dumps({"rows": len(hashes)}, ensure_ascii=False),
    })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, args.output, compression=args.compression, compression_level=3)
    print(f"wrote {len(hashes):,} rows → {args.output} ({args.output.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
