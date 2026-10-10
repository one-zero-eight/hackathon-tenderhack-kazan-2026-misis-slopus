#!/usr/bin/env python3
"""Build SQLite cache for OKPD2 / КПГЗ / СПГЗ / ОКЕИ trees.

Tree columns only — no gzip payloads, no STE catalog. Full rows stay in parquet.
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data" / "classifiers.sqlite"
SCHEMA = "classifiers-slim-v2"


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


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
    for path in (args.okpd, args.kpgz, args.spgz, args.okei):
        if not path.exists():
            raise SystemExit(f"Нет источника: {path}")

    db = sqlite3.connect(temporary)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.executescript(
        """
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE okpd(
          id INTEGER PRIMARY KEY,
          code TEXT NOT NULL,
          name TEXT NOT NULL,
          parent_id INTEGER,
          level INTEGER,
          children_count INTEGER NOT NULL,
          is_actual INTEGER
        );
        CREATE TABLE kpgz(
          id INTEGER PRIMARY KEY,
          code TEXT NOT NULL,
          name TEXT NOT NULL,
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
          kpgz_external_id INTEGER,
          okpd2_code TEXT,
          ktru_id INTEGER,
          is_excluded INTEGER NOT NULL
        );
        CREATE TABLE okei(
          id INTEGER PRIMARY KEY,
          code TEXT,
          name TEXT NOT NULL,
          razdel TEXT NOT NULL,
          podrazdel TEXT NOT NULL,
          kind TEXT NOT NULL,
          parent_id INTEGER,
          children_count INTEGER NOT NULL
        );
        """
    )

    okpd_rows = pq.read_table(
        args.okpd,
        columns=["id", "code", "name", "parent", "level", "children_count", "is_actual"],
    ).to_pylist()
    db.executemany(
        "INSERT INTO okpd VALUES (?,?,?,?,?,?,?)",
        [
            (
                row["id"],
                row.get("code") or "",
                row.get("name") or "",
                row.get("parent"),
                row.get("level"),
                int(row.get("children_count") or 0),
                1 if row.get("is_actual") else 0,
            )
            for row in okpd_rows
        ],
    )
    by_code = {code: id_ for id_, code in db.execute("SELECT id, code FROM okpd") if code}
    orphans = list(db.execute(
        "SELECT id, code FROM okpd WHERE parent_id IS NULL AND length(code) > 1 ORDER BY length(code) DESC, code"
    ))
    for orphan_id, code in orphans:
        parts = code.split(".")
        parent_id = None
        for index in range(len(parts) - 1, 0, -1):
            candidate = ".".join(parts[:index])
            if candidate in by_code:
                parent_id = by_code[candidate]
                break
        if parent_id is not None:
            db.execute("UPDATE okpd SET parent_id=? WHERE id=?", (parent_id, orphan_id))
    db.execute("CREATE INDEX ix_okpd_parent ON okpd(parent_id)")
    db.execute(
        """
        UPDATE okpd SET children_count = (
          SELECT count(*) FROM okpd child WHERE child.parent_id = okpd.id
        )
        """
    )

    kpgz_rows = pq.read_table(
        args.kpgz,
        columns=["id", "code", "name", "parentId", "externalId", "treeLevel", "isLastLevel", "okpd"],
    ).to_pylist()
    db.executemany(
        "INSERT INTO kpgz VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                row["id"],
                row.get("code") or "",
                row.get("name") or "",
                row.get("parentId"),
                row.get("externalId"),
                row.get("treeLevel"),
                1 if row.get("isLastLevel") else 0,
                (row.get("okpd") or {}).get("code"),
                (row.get("okpd") or {}).get("name"),
            )
            for row in kpgz_rows
        ],
    )

    spgz_rows = pq.read_table(
        args.spgz,
        columns=["id", "name", "kpgzId", "okpd2Code", "okpdCode", "ktruId", "isExcluded"],
    ).to_pylist()
    db.executemany(
        "INSERT INTO spgz VALUES (?,?,?,?,?,?)",
        [
            (
                row["id"],
                row.get("name") or "",
                row.get("kpgzId"),
                row.get("okpd2Code") or row.get("okpdCode"),
                row.get("ktruId"),
                1 if row.get("isExcluded") else 0,
            )
            for row in spgz_rows
        ],
    )

    okei_rows = pq.read_table(args.okei).to_pylist()
    okei_id = 0
    section_ids: dict[str, int] = {}
    group_ids: dict[tuple[str, str], int] = {}
    for row in okei_rows:
        razdel = str(row.get("razdel") or "")
        podrazdel = str(row.get("podrazdel") or "")
        code = row.get("code")
        name = (row.get("name") or "").strip()
        if not name:
            continue
        if code is None and podrazdel == "0":
            okei_id += 1
            section_ids[razdel] = okei_id
            db.execute(
                "INSERT INTO okei VALUES (?,?,?,?,?,?,?,?)",
                (okei_id, f"s:{razdel}", name, razdel, podrazdel, "section", None, 0),
            )
    for row in okei_rows:
        razdel = str(row.get("razdel") or "")
        podrazdel = str(row.get("podrazdel") or "")
        code = row.get("code")
        name = (row.get("name") or "").strip()
        if not name or code is not None or podrazdel == "0":
            continue
        okei_id += 1
        group_ids[(razdel, podrazdel)] = okei_id
        parent = section_ids.get(razdel)
        db.execute(
            "INSERT INTO okei VALUES (?,?,?,?,?,?,?,?)",
            (okei_id, f"g:{razdel}:{podrazdel}", name, razdel, podrazdel, "group", parent, 0),
        )
    for row in okei_rows:
        razdel = str(row.get("razdel") or "")
        podrazdel = str(row.get("podrazdel") or "")
        code = row.get("code")
        name = (row.get("name") or "").strip()
        if not name or code is None:
            continue
        okei_id += 1
        parent = group_ids.get((razdel, podrazdel)) or section_ids.get(razdel)
        db.execute(
            "INSERT INTO okei VALUES (?,?,?,?,?,?,?,?)",
            (okei_id, str(code), name, razdel, podrazdel, "unit", parent, 0),
        )
    db.execute("CREATE INDEX ix_okei_parent ON okei(parent_id)")
    db.execute(
        """
        UPDATE okei SET children_count = (
          SELECT count(*) FROM okei child WHERE child.parent_id = okei.id
        )
        """
    )

    source_paths = (args.okpd, args.kpgz, args.spgz, args.okei)
    sources = {
        "schema": SCHEMA,
        "okpd": rel(args.okpd),
        "kpgz": rel(args.kpgz),
        "spgz": rel(args.spgz),
        "okei": rel(args.okei),
        "source_mtime_ms": str(int(max(p.stat().st_mtime_ns for p in source_paths) // 1_000_000)),
        "okpd_count": str(len(okpd_rows)),
        "kpgz_count": str(len(kpgz_rows)),
        "spgz_count": str(len(spgz_rows)),
        "okei_count": str(okei_id),
    }
    db.executemany("INSERT INTO meta(key,value) VALUES(?,?)", list(sources.items()))
    db.executescript(
        """
        CREATE INDEX ix_okpd_code ON okpd(code);
        CREATE INDEX ix_kpgz_parent ON kpgz(parent_id);
        CREATE INDEX ix_kpgz_code ON kpgz(code);
        CREATE INDEX ix_kpgz_external ON kpgz(external_id);
        CREATE INDEX ix_kpgz_okpd ON kpgz(okpd_code);
        CREATE INDEX ix_spgz_kpgz ON spgz(kpgz_external_id);
        CREATE INDEX ix_spgz_okpd ON spgz(okpd2_code);
        CREATE INDEX ix_okei_code ON okei(code);
        CREATE INDEX ix_okei_kind ON okei(kind);
        """
    )
    db.commit()
    db.close()
    temporary.replace(args.output)
    print(
        f"Wrote {args.output} okpd={len(okpd_rows)} kpgz={len(kpgz_rows)} spgz={len(spgz_rows)} "
        f"okei={okei_id} in {time.monotonic() - started:.1f}s "
        f"({args.output.stat().st_size / (1024 * 1024):.1f} MiB)",
        flush=True,
    )


if __name__ == "__main__":
    main()
