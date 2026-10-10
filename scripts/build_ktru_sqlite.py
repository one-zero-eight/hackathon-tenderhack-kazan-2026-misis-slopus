#!/usr/bin/env python3
"""Build a bounded-memory SQLite cache for the KTRU tree UI."""
from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import time
import unicodedata
from pathlib import Path

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "data" / "ktru-actual.parquet"
DEFAULT_NAMES = ROOT / "data" / "moy-zakupki-ktru.parquet"
DEFAULT_OUTPUT = ROOT / "data" / "ktru-actual.sqlite"
SCHEMA_VERSION = "ktru-mmap-v2"
BATCH_SIZE = 128


def fold_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")


def search_grams(text: str) -> set[str]:
    # ASCII terms avoid unicode61/SQLite's inconsistent Cyrillic folding.
    return {f"g{ord(char):06x}" for char in text} | {
        f"g{ord(left):06x}{ord(right):06x}" for left, right in zip(text, text[1:])
    }


def stage_display_names(db: sqlite3.Connection, path: Path) -> int:
    # A disk-backed primary-key join, not a corpus-sized Python dictionary.
    db.execute("CREATE TABLE display_names(code TEXT PRIMARY KEY, display TEXT, shortname TEXT, similar TEXT) WITHOUT ROWID")
    count = 0
    if not path.exists():
        return count
    columns = ["code", "tree_display_name", "shortname", "similar_products"]
    with pq.ParquetFile(path, memory_map=True) as parquet:
        selected = [column for column in columns if column in parquet.schema_arrow.names]
        for batch in parquet.iter_batches(columns=selected, batch_size=BATCH_SIZE, use_threads=False):
            staged = []
            for row in batch.to_pylist():
                if not row.get("code"):
                    continue
                similar = row.get("similar_products") or []
                if not isinstance(similar, list):
                    similar = []
                staged.append((
                    str(row["code"]),
                    (row.get("tree_display_name") or row.get("shortname") or "").strip(),
                    (row.get("shortname") or "").strip(),
                    json.dumps([str(item).strip() for item in similar if str(item).strip()], ensure_ascii=False),
                ))
            db.executemany("INSERT OR REPLACE INTO display_names VALUES (?,?,?,?)", staged)
            db.commit()
            count += len(staged)
    return count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--names", type=Path, default=DEFAULT_NAMES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    started = time.monotonic()
    temporary = args.output.with_suffix(args.output.suffix + ".part")
    temporary.unlink(missing_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(temporary)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    # mmap is for read-only serving; keep the growing output DB off the mapping.
    db.execute("PRAGMA mmap_size=0")
    db.execute("PRAGMA cache_size=-2048")
    db.execute("PRAGMA temp_store=FILE")
    db.executescript(
        """
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE ktru(
          code TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          official_name TEXT NOT NULL,
          shortname TEXT,
          similar_products TEXT,
          status TEXT,
          okpd2 TEXT,
          okpd2_name TEXT,
          parent_code TEXT,
          is_template INTEGER NOT NULL,
          payload BLOB NOT NULL,
          name_fold TEXT NOT NULL,
          effective_okpd TEXT NOT NULL,
          is_okpd_root INTEGER NOT NULL DEFAULT 1,
          official_name_fold TEXT NOT NULL,
          synonyms_fold TEXT NOT NULL
        );
        CREATE TABLE ktru_okpd(code TEXT PRIMARY KEY, name TEXT NOT NULL);
        CREATE VIRTUAL TABLE ktru_search USING fts5(grams, content='', tokenize='unicode61');
        """
    )
    names_count = stage_display_names(db, args.names)
    mirror_lookup = db.cursor()
    statement = """INSERT INTO ktru
        (code,name,official_name,shortname,similar_products,status,okpd2,okpd2_name,parent_code,is_template,payload,name_fold,effective_okpd,official_name_fold,synonyms_fold)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
    record_count = 0
    with pq.ParquetFile(args.source, memory_map=True) as parquet:
        for batch in parquet.iter_batches(batch_size=BATCH_SIZE, use_threads=False):
            for row in batch.to_pylist():
                okpd = row.get("okpd2") or {}
                parent = row.get("parent") or {}
                code = row["code"]
                official = row["name"] or ""
                mirror = mirror_lookup.execute("SELECT display, shortname, similar FROM display_names WHERE code=?", (code,)).fetchone()
                display = (mirror[0] if mirror else "") or official
                shortname = (mirror[1] if mirror else "") or None
                similar = json.loads(mirror[2]) if mirror else []
                similar_json = json.dumps(similar, ensure_ascii=False, separators=(",", ":")) if similar else None
                name_fold = fold_text(display)
                official_fold = fold_text(official)
                synonyms_fold = fold_text(" ".join([shortname or "", *similar]))
                effective_okpd = okpd.get("code") or code.split("-", 1)[0]
                payload = gzip.compress(
                    json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"),
                    compresslevel=6,
                )
                cursor = db.execute(
                    statement,
                    (
                        code, display, official, shortname, similar_json, row.get("status"),
                        okpd.get("code"), okpd.get("name"), parent.get("code"),
                        1 if row.get("is_template") else 0, payload, name_fold, effective_okpd,
                        official_fold, synonyms_fold,
                    ),
                )
                grams = search_grams(fold_text(code)) | search_grams(name_fold) | search_grams(official_fold) | search_grams(synonyms_fold)
                db.execute("INSERT INTO ktru_search(rowid,grams) VALUES (?,?)", (cursor.lastrowid, " ".join(sorted(grams))))
                if okpd.get("code") and okpd.get("name"):
                    db.execute("INSERT OR IGNORE INTO ktru_okpd VALUES (?,?)", (okpd["code"], okpd["name"]))
                record_count += 1
            # Bound SQLite's pending FTS segments, not just Arrow's Python rows.
            db.commit()
    db.execute("DROP TABLE display_names")
    db.execute("CREATE INDEX ix_ktru_name_fold ON ktru(name_fold)")
    db.execute("CREATE INDEX ix_ktru_official_fold ON ktru(official_name_fold)")
    db.execute("INSERT OR IGNORE INTO ktru_okpd SELECT DISTINCT effective_okpd, '' FROM ktru")
    db.execute("""UPDATE ktru SET is_okpd_root=0 WHERE EXISTS (
        SELECT 1 FROM ktru AS parent WHERE parent.code=ktru.parent_code
        AND parent.effective_okpd=ktru.effective_okpd
    )""")
    db.execute("CREATE INDEX ix_ktru_okpd2 ON ktru(okpd2,code)")
    db.execute("CREATE INDEX ix_ktru_parent ON ktru(parent_code,code)")
    db.execute("CREATE INDEX ix_ktru_roots ON ktru(effective_okpd,is_okpd_root,code)")
    names_mtime = str(args.names.stat().st_mtime_ns // 1_000_000) if args.names.exists() else ""
    db.executemany(
        "INSERT INTO meta(key,value) VALUES(?,?)",
        [
            ("schema", SCHEMA_VERSION),
            ("lexical_schema", "folded-grams-v1"),
            ("source", str(args.source.relative_to(ROOT)) if args.source.is_relative_to(ROOT) else str(args.source)),
            ("source_mtime_ms", str(args.source.stat().st_mtime_ns // 1_000_000)),
            ("names_source", str(args.names.relative_to(ROOT)) if args.names.exists() and args.names.is_relative_to(ROOT) else (str(args.names) if args.names.exists() else "")),
            ("names_mtime_ms", names_mtime),
            ("record_count", str(record_count)),
        ],
    )
    db.commit()
    db.close()
    temporary.replace(args.output)
    print(f"Wrote {args.output} ({record_count} rows, {names_count} display names) in {time.monotonic() - started:.1f}s", flush=True)


if __name__ == "__main__":
    main()
