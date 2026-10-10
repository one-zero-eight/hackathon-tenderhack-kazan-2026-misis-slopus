#!/usr/bin/env python3
"""Build mmap-backed classifier trees and full gzip-compressed source records."""
from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import time
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data" / "classifiers.sqlite"
SCHEMA = "classifiers-full-v4"
BATCH_SIZE = 128


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def fold_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")


def search_grams(text: str) -> set[str]:
    return {f"g{ord(char):06x}" for char in text} | {
        f"g{ord(left):06x}{ord(right):06x}" for left, right in zip(text, text[1:])
    }


def rows(path: Path) -> Iterator[dict[str, Any]]:
    with pq.ParquetFile(path, memory_map=True) as parquet:
        for batch in parquet.iter_batches(batch_size=BATCH_SIZE, use_threads=False):
            yield from batch.to_pylist()


def payload(row: dict[str, Any]) -> bytes:
    return gzip.compress(
        json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"),
        compresslevel=1,
        mtime=0,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--kpgz", type=Path, default=ROOT / "data" / "pp-kpgz.parquet")
    parser.add_argument("--spgz", type=Path, default=ROOT / "data" / "spgz.parquet")
    parser.add_argument("--okpd", type=Path, default=ROOT / "data" / "okpd2-moy-zakupki.parquet")
    parser.add_argument("--okei", type=Path, default=ROOT / "data" / "okei.parquet")
    args = parser.parse_args()
    started = time.monotonic()
    temporary = args.output.with_suffix(args.output.suffix + ".part")
    temporary.unlink(missing_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    source_paths = (args.okpd, args.kpgz, args.spgz, args.okei)
    for path in source_paths:
        if not path.exists():
            raise SystemExit(f"Нет источника: {path}")

    db = sqlite3.connect(temporary)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.execute("PRAGMA mmap_size=0")
    db.execute("PRAGMA cache_size=-2048")
    db.execute("PRAGMA temp_store=FILE")
    db.executescript(
        """
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE raw_records(
          tab TEXT NOT NULL,
          code TEXT NOT NULL,
          payload BLOB NOT NULL,
          PRIMARY KEY(tab, code)
        ) WITHOUT ROWID;
        CREATE TABLE okpd(
          id INTEGER PRIMARY KEY,
          code TEXT NOT NULL,
          name TEXT NOT NULL,
          name_fold TEXT NOT NULL,
          synonyms_fold TEXT NOT NULL DEFAULT '',
          parent_id INTEGER,
          level INTEGER,
          children_count INTEGER NOT NULL,
          is_actual INTEGER
        );
        CREATE TABLE kpgz(
          id INTEGER PRIMARY KEY,
          code TEXT NOT NULL,
          name TEXT NOT NULL,
          name_fold TEXT NOT NULL,
          parent_id INTEGER,
          external_id INTEGER,
          tree_level INTEGER,
          is_last INTEGER NOT NULL,
          okpd_code TEXT,
          okpd_name TEXT
        );
        CREATE TABLE spgz(
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          name_fold TEXT NOT NULL,
          kpgz_external_id INTEGER,
          okpd2_code TEXT,
          ktru_id INTEGER,
          is_excluded INTEGER NOT NULL
        );
        CREATE TABLE okei(
          id INTEGER PRIMARY KEY,
          code TEXT,
          name TEXT NOT NULL,
          name_fold TEXT NOT NULL,
          razdel TEXT NOT NULL,
          podrazdel TEXT NOT NULL,
          kind TEXT NOT NULL,
          parent_id INTEGER,
          children_count INTEGER NOT NULL
        );
        """
    )
    raw_insert = "INSERT OR IGNORE INTO raw_records VALUES (?,?,?)"

    okpd_count = 0
    for row in rows(args.okpd):
        name = row.get("name") or ""
        synonyms = row.get("similar_products") or []
        if not isinstance(synonyms, list):
            synonyms = []
        synonyms_fold = " ".join(fold_text(str(item)) for item in synonyms if str(item).strip())
        db.execute(
            "INSERT INTO okpd VALUES (?,?,?,?,?,?,?,?,?)",
            (
                row["id"], row.get("code") or "", name, fold_text(name), synonyms_fold,
                row.get("parent"), row.get("level"), int(row.get("children_count") or 0),
                1 if row.get("is_actual") else 0,
            ),
        )
        db.execute(raw_insert, ("okpd", str(row.get("code") or ""), payload(row)))
        okpd_count += 1
    db.execute("CREATE INDEX ix_okpd_code ON okpd(code)")
    db.execute("CREATE INDEX ix_okpd_name_fold ON okpd(name_fold)")
    orphans = db.execute(
        "SELECT id, code FROM okpd WHERE parent_id IS NULL AND length(code) > 1 ORDER BY length(code) DESC, code"
    )
    for orphan_id, code in orphans:
        parts = code.split(".")
        for index in range(len(parts) - 1, 0, -1):
            parent = db.execute(
                "SELECT id FROM okpd WHERE code=? ORDER BY id DESC LIMIT 1",
                (".".join(parts[:index]),),
            ).fetchone()
            if parent is not None:
                db.execute("UPDATE okpd SET parent_id=? WHERE id=?", (parent[0], orphan_id))
                break
    db.execute("CREATE INDEX ix_okpd_parent ON okpd(parent_id)")
    db.execute(
        """
        UPDATE okpd SET children_count = (
          SELECT count(*) FROM okpd child WHERE child.parent_id = okpd.id
        )
        """
    )

    kpgz_count = 0
    for row in rows(args.kpgz):
        name = row.get("name") or ""
        db.execute(
            "INSERT INTO kpgz VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                row["id"], row.get("code") or "", name, fold_text(name),
                row.get("parentId"), row.get("externalId"), row.get("treeLevel"),
                1 if row.get("isLastLevel") else 0,
                (row.get("okpd") or {}).get("code"), (row.get("okpd") or {}).get("name"),
            ),
        )
        db.execute(raw_insert, ("kpgz", str(row.get("code") or ""), payload(row)))
        kpgz_count += 1

    spgz_count = 0
    for row in rows(args.spgz):
        name = row.get("name") or ""
        db.execute(
            "INSERT INTO spgz VALUES (?,?,?,?,?,?,?)",
            (
                row["id"], name, fold_text(name), row.get("kpgzId"),
                row.get("okpd2Code") or row.get("okpdCode"), row.get("ktruId"),
                1 if row.get("isExcluded") else 0,
            ),
        )
        db.execute(raw_insert, ("spgz", str(row["id"]), payload(row)))
        spgz_count += 1

    okei_id = 0
    section_ids: dict[str, int] = {}
    group_ids: dict[tuple[str, str], int] = {}
    # Three bounded passes preserve section/group/unit ids and source-row ordering.
    for row in rows(args.okei):
        razdel = str(row.get("razdel") or "")
        podrazdel = str(row.get("podrazdel") or "")
        code = row.get("code")
        raw_code = str(code) if code is not None else (
            f"s:{razdel}" if podrazdel == "0" else f"g:{razdel}:{podrazdel}"
        )
        db.execute(raw_insert, ("okei", raw_code, payload(row)))
        name = (row.get("name") or "").strip()
        if not name or code is not None or podrazdel != "0":
            continue
        okei_id += 1
        section_ids[razdel] = okei_id
        db.execute(
            "INSERT INTO okei VALUES (?,?,?,?,?,?,?,?,?)",
            (okei_id, raw_code, name, fold_text(name), razdel, podrazdel, "section", None, 0),
        )
    for row in rows(args.okei):
        razdel = str(row.get("razdel") or "")
        podrazdel = str(row.get("podrazdel") or "")
        code = row.get("code")
        name = (row.get("name") or "").strip()
        if not name or code is not None or podrazdel == "0":
            continue
        okei_id += 1
        group_ids[(razdel, podrazdel)] = okei_id
        db.execute(
            "INSERT INTO okei VALUES (?,?,?,?,?,?,?,?,?)",
            (okei_id, f"g:{razdel}:{podrazdel}", name, fold_text(name), razdel, podrazdel, "group", section_ids.get(razdel), 0),
        )
    for row in rows(args.okei):
        razdel = str(row.get("razdel") or "")
        podrazdel = str(row.get("podrazdel") or "")
        code = row.get("code")
        name = (row.get("name") or "").strip()
        if not name or code is None:
            continue
        okei_id += 1
        parent = group_ids.get((razdel, podrazdel)) or section_ids.get(razdel)
        db.execute(
            "INSERT INTO okei VALUES (?,?,?,?,?,?,?,?,?)",
            (okei_id, str(code), name, fold_text(name), razdel, podrazdel, "unit", parent, 0),
        )
    db.execute("CREATE INDEX ix_okei_parent ON okei(parent_id)")
    db.execute(
        """
        UPDATE okei SET children_count = (
          SELECT count(*) FROM okei child WHERE child.parent_id = okei.id
        )
        """
    )

    # Build bounded, disk-backed lexical indexes after the narrow tree rows.
    for tab in ("okpd", "kpgz", "spgz", "okei"):
        db.execute(f"CREATE VIRTUAL TABLE {tab}_search USING fts5(grams, content='', tokenize='unicode61')")
        code = "cast(id AS TEXT)" if tab == "spgz" else "coalesce(code,'')"
        synonyms = "synonyms_fold" if tab == "okpd" else "''"
        cursor = db.execute(f"SELECT id, {code}, name_fold, {synonyms} FROM {tab}")
        while batch := cursor.fetchmany(BATCH_SIZE):
            db.executemany(
                f"INSERT INTO {tab}_search(rowid,grams) VALUES (?,?)",
                [(row_id, " ".join(sorted(search_grams(fold_text(code_value)) | search_grams(name) | search_grams(similar))))
                 for row_id, code_value, name, similar in batch],
            )
            db.commit()

    sources = {
        "schema": SCHEMA,
        "lexical_schema": "folded-grams-v1",
        "okpd": rel(args.okpd),
        "kpgz": rel(args.kpgz),
        "spgz": rel(args.spgz),
        "okei": rel(args.okei),
        "source_mtime_ms": str(int(max(p.stat().st_mtime_ns for p in source_paths) // 1_000_000)),
        "okpd_count": str(okpd_count),
        "kpgz_count": str(kpgz_count),
        "spgz_count": str(spgz_count),
        "okei_count": str(okei_id),
    }
    sources.update({f"{key}_mtime_ms": str(path.stat().st_mtime_ns // 1_000_000)
                    for key, path in zip(("okpd", "kpgz", "spgz", "okei"), source_paths)})
    db.executemany("INSERT INTO meta(key,value) VALUES(?,?)", sources.items())
    db.executescript(
        """
        CREATE INDEX ix_kpgz_parent ON kpgz(parent_id);
        CREATE INDEX ix_kpgz_code ON kpgz(code);
        CREATE INDEX ix_kpgz_name_fold ON kpgz(name_fold);
        CREATE INDEX ix_kpgz_external ON kpgz(external_id);
        CREATE INDEX ix_kpgz_okpd ON kpgz(okpd_code);
        CREATE INDEX ix_spgz_kpgz ON spgz(kpgz_external_id);
        CREATE INDEX ix_spgz_name_fold ON spgz(name_fold);
        CREATE INDEX ix_spgz_okpd ON spgz(okpd2_code);
        CREATE INDEX ix_okei_code ON okei(code);
        CREATE INDEX ix_okei_name_fold ON okei(name_fold);
        CREATE INDEX ix_okei_kind ON okei(kind);
        """
    )
    db.commit()
    db.close()
    temporary.replace(args.output)
    print(
        f"Wrote {args.output} okpd={okpd_count} kpgz={kpgz_count} spgz={spgz_count} "
        f"okei={okei_id} in {time.monotonic() - started:.1f}s "
        f"({args.output.stat().st_size / (1024 * 1024):.1f} MiB)",
        flush=True,
    )


if __name__ == "__main__":
    main()
