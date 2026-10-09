#!/usr/bin/env python3
"""Build a SQLite cache for the KTRU tree UI from ktru-actual.parquet."""
from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import time
from pathlib import Path

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "data" / "ktru-actual.parquet"
DEFAULT_OUTPUT = ROOT / "data" / "ktru-actual.sqlite"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    started = time.monotonic()
    rows = pq.read_table(args.source).to_pylist()
    temporary = args.output.with_suffix(args.output.suffix + ".part")
    temporary.unlink(missing_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(temporary)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.executescript(
        """
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE ktru(
          code TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          status TEXT,
          okpd2 TEXT,
          okpd2_name TEXT,
          parent_code TEXT,
          is_template INTEGER NOT NULL,
          payload BLOB NOT NULL
        );
        """
    )
    insert = db.execute
    statement = "INSERT INTO ktru VALUES (?,?,?,?,?,?,?,?)"
    for row in rows:
        okpd = row.get("okpd2") or {}
        parent = row.get("parent") or {}
        payload = gzip.compress(
            json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"),
            compresslevel=6,
        )
        insert(
            statement,
            (
                row["code"],
                row["name"] or "",
                row.get("status"),
                okpd.get("code"),
                okpd.get("name"),
                parent.get("code"),
                1 if row.get("is_template") else 0,
                payload,
            ),
        )
    db.execute(
        "INSERT INTO meta(key,value) VALUES(?,?), (?,?), (?,?)",
        (
            "source",
            str(args.source.relative_to(ROOT)) if args.source.is_relative_to(ROOT) else str(args.source),
            "source_mtime_ms",
            str(int(args.source.stat().st_mtime_ns // 1_000_000)),
            "record_count",
            str(len(rows)),
        ),
    )
    db.execute("CREATE INDEX ix_ktru_okpd2 ON ktru(okpd2)")
    db.execute("CREATE INDEX ix_ktru_parent ON ktru(parent_code)")
    db.commit()
    db.close()
    temporary.replace(args.output)
    print(f"Wrote {args.output} ({len(rows)} rows) in {time.monotonic() - started:.1f}s", flush=True)


if __name__ == "__main__":
    main()
