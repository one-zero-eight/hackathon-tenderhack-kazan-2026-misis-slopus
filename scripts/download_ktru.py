#!/usr/bin/env python3
"""Download the public Moy Zakupki KTRU or OKPD2 mirror with page checkpoints."""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
ENDPOINT = "https://moy-zakupki.ru/api/app/ktru/list"
PARAMS = {"is_last_version": "true", "ordering": "code", "per_page": 50}
TABLE = "ktru"
NOTES = "Full source records including characteristics_jsonb and mirror-added annotations. Source freshness and nationwide coverage are not independently verified."
LOCAL = threading.local()
RATE_LOCK = threading.Lock()
NEXT_REQUEST = 0.0
REQUEST_INTERVAL = 1.25


def iter_records(source: Path):
    opener = gzip.open if source.suffix == ".gz" else open
    with opener(source, "rt", encoding="utf-8") as stream:
        if source.name.removesuffix(".gz").endswith(".jsonl"):
            for line in stream:
                if line.strip():
                    yield json.loads(line)
        else:
            value = json.load(stream)
            yield from value["items"] if isinstance(value, dict) else value


def write_json_gz(output: Path, records) -> None:
    temporary = output.with_name(output.name + ".part")
    with gzip.open(temporary, "wt", encoding="utf-8") as stream:
        stream.write("[\n")
        separator = ""
        for item in records:
            stream.write(separator + json.dumps(item, ensure_ascii=False, separators=(",", ":")))
            separator = ",\n"
        stream.write("\n]\n")
    temporary.replace(output)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_page(page: int) -> dict:
    if not hasattr(LOCAL, "session"):
        LOCAL.session = requests.Session()
        LOCAL.session.headers.update({"Accept": "application/json", "User-Agent": "zakupki-reverse/1.0"})
    global NEXT_REQUEST
    for attempt in range(5):
        with RATE_LOCK:
            delay = NEXT_REQUEST - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            NEXT_REQUEST = time.monotonic() + REQUEST_INTERVAL
        response = LOCAL.session.get(ENDPOINT, params={**PARAMS, "page": page}, timeout=90)
        if response.status_code != 429:
            break
        retry_after = response.headers.get("Retry-After")
        cooldown = 120.0
        if retry_after:
            if retry_after.isdigit():
                cooldown = max(1.0, float(retry_after))
            else:
                cooldown = max(1.0, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
        with RATE_LOCK:
            NEXT_REQUEST = max(NEXT_REQUEST, time.monotonic() + cooldown)
        print(f"RATE LIMIT page={page}; cooldown={cooldown:.0f}s; attempt={attempt + 1}/5", flush=True)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("count"), int) or not isinstance(data.get("results"), list):
        raise ValueError(f"Unexpected response on page {page}")
    return data


def export(db: sqlite3.Connection, output: Path, total: int, started_at: str, complete: bool) -> None:
    count = db.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]
    metadata = {
        "source": ENDPOINT,
        "sourceKind": "third-party mirror, not an official EIS snapshot",
        "parameters": PARAMS,
        "expectedCount": total,
        "recordCount": count,
        "complete": complete,
        "startedAt": started_at,
        "exportedAt": now(),
        "format": "json",
        "compression": "gzip",
        "notes": NOTES,
    }

    if TABLE == "ktru":
        metadata["deprecated"] = True
        metadata["preferredDataset"] = "ktru-actual.parquet"
        metadata["deprecationReason"] = "Prefer the actual XML-derived Parquet for source characteristics, ranges, units and lifecycle metadata; this mirror remains useful only for its own annotations."

    def records():
        for (payload,) in db.execute(f"SELECT payload FROM {TABLE} ORDER BY code,id"):
            yield json.loads(payload)

    write_json_gz(output, records())
    Path(str(output) + ".meta.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"EXPORTED {output}: {count}/{total}; complete={complete}", flush=True)


def export_okpd2_links(db: sqlite3.Connection, output: Path) -> None:
    """Resolve mirror-local KTRU references without modifying either catalog."""
    old_codes = {item["code"]: item["name"] for item in iter_records(ROOT / "data/deprecated/okpd2.json.gz") if item["code"]}
    mirror_codes = dict(db.execute("SELECT code,json_extract(payload,'$.name') FROM okpd2"))
    shared = old_codes.keys() & mirror_codes.keys()
    name_differences = [
        {"code": code, "originalName": old_codes[code], "mirrorName": mirror_codes[code]}
        for code in sorted(shared) if old_codes[code] != mirror_codes[code]
    ]
    comparison = {
        "originalSource": "data/deprecated/okpd2.json.gz",
        "originalCodeCount": len(old_codes),
        "mirrorCodeCount": len(mirror_codes),
        "sharedCodeCount": len(shared),
        "onlyOriginal": sorted(old_codes.keys() - mirror_codes.keys()),
        "onlyMirror": sorted(mirror_codes.keys() - old_codes.keys()),
        "nameDifferences": name_differences,
        "note": "Differences are not automatically treated as corrections; mirror annotations remain separate.",
    }
    temporary_directory = ROOT / "data/tmp"
    temporary_directory.mkdir(parents=True, exist_ok=True)
    comparison_path = temporary_directory / "okpd2-moy-zakupki-comparison.json"
    db.execute("ATTACH DATABASE ? AS ktru_source", (f"file:{ROOT / 'data/ktru.sqlite'}?mode=ro",))
    link_path = temporary_directory / "ktru-okpd2-moy-zakupki.json.gz"
    linked = 0
    total = 0
    missing_ids: set[int] = set()
    prefix_mismatches = []
    query = """
        SELECT k.id,k.code,json_extract(k.payload,'$.okpd2'),o.code,json_extract(o.payload,'$.name')
        FROM ktru_source.ktru k
        LEFT JOIN okpd2 o ON o.id=json_extract(k.payload,'$.okpd2')
        ORDER BY k.code,k.id
    """

    def links():
        for ktru_id, ktru_code, okpd2_id, okpd2_code, okpd2_name in db.execute(query):
            yield {"ktruId": ktru_id, "ktruCode": ktru_code, "okpd2Id": okpd2_id, "okpd2Code": okpd2_code, "okpd2Name": okpd2_name}

    for item in links():
        total += 1
        if item["okpd2Code"]:
            linked += 1
            if item["ktruCode"].split("-", 1)[0] != item["okpd2Code"]:
                prefix_mismatches.append(item)
        elif item["okpd2Id"] is not None:
            missing_ids.add(item["okpd2Id"])
    write_json_gz(link_path, links())
    comparison["ktruCodePrefixDifferences"] = prefix_mismatches
    temporary = comparison_path.with_name(comparison_path.name + ".part")
    temporary.write_text(json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(comparison_path)
    meta_path = Path(str(output) + ".meta.json")
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    metadata["comparison"] = {
        "file": os.path.relpath(comparison_path, output.parent),
        "sharedCodeCount": len(shared),
        "onlyOriginalCount": len(comparison["onlyOriginal"]),
        "onlyMirrorCount": len(comparison["onlyMirror"]),
        "nameDifferenceCount": len(name_differences),
    }
    metadata["ktruLinks"] = {
        "file": os.path.relpath(link_path, output.parent),
        "ktruSource": "data/ktru.sqlite",
        "recordCount": total,
        "resolvedCount": linked,
        "unresolvedCount": total - linked,
        "missingOkpd2Ids": sorted(missing_ids),
        "codePrefixDifferenceCount": len(prefix_mismatches),
        "codePrefixDifferencesFile": os.path.relpath(comparison_path, output.parent),
    }
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"KTRU LINKS resolved={linked}/{total}; missing IDs={len(missing_ids)}; prefix mismatches={len(prefix_mismatches)}", flush=True)
    print(f"OKPD2 COMPARISON shared={len(shared)}; original-only={len(comparison['onlyOriginal'])}; mirror-only={len(comparison['onlyMirror'])}; different names={len(name_differences)}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--interval", type=float, default=1.25, help="Minimum seconds between requests")
    parser.add_argument("--catalog", choices=("ktru", "okpd2"), default="ktru")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.workers < 1 or args.workers > 4:
        parser.error("--workers must be between 1 and 4")
    if args.interval < 1.25:
        parser.error("--interval must be at least 1.25 seconds")
    global REQUEST_INTERVAL, ENDPOINT, PARAMS, TABLE, NOTES
    REQUEST_INTERVAL = args.interval
    TABLE = args.catalog
    if args.catalog == "okpd2":
        ENDPOINT = "https://moy-zakupki.ru/api/app/okpd/list"
        PARAMS = {"ordering": "code", "per_page": 50}
        NOTES = "Full source records including internal IDs, hierarchy, actuality flags, synonyms and mirror-added limitations. Separate from data/deprecated/okpd2.json.gz; source freshness is not independently verified."
    basename = "okpd2-moy-zakupki" if args.catalog == "okpd2" else "ktru"
    args.db = args.db or ROOT / f"data/{basename}.sqlite"
    output_directory = ROOT / "data" if args.catalog == "okpd2" else ROOT / "data/deprecated"
    args.out = args.out or output_directory / f"{basename}.json.gz"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    first = fetch_page(1)
    total = first["count"]
    page_size = len(first["results"])
    if not total or page_size != PARAMS["per_page"]:
        raise ValueError(f"Unexpected count/page size: {total}/{page_size}")
    pages = math.ceil(total / page_size)
    db = sqlite3.connect(args.db)
    db.executescript(f"""
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS downloaded_pages(page INTEGER PRIMARY KEY, fetched_at TEXT NOT NULL, record_count INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS {TABLE}(id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, page INTEGER NOT NULL, payload TEXT NOT NULL CHECK(json_valid(payload)));
    """)
    signature = json.dumps({"source": ENDPOINT, "parameters": PARAMS, "count": total}, sort_keys=True)
    previous = db.execute("SELECT value FROM metadata WHERE key='signature'").fetchone()
    if previous and previous[0] != signature:
        db.close()
        raise ValueError("Source count/parameters changed since checkpoint; use a separate database for a new snapshot")
    with db:
        db.execute("INSERT OR IGNORE INTO metadata VALUES('signature',?)", (signature,))
        db.execute("INSERT OR IGNORE INTO metadata VALUES('startedAt',?)", (now(),))
    started_at = db.execute("SELECT value FROM metadata WHERE key='startedAt'").fetchone()[0]

    def save(page: int, data: dict) -> None:
        items = data["results"]
        expected = min(page_size, total - (page - 1) * page_size)
        if data["count"] != total or len(items) != expected:
            raise ValueError(f"Count changed or incomplete page {page}: {data['count']}/{len(items)}")
        if bool(data.get("next")) != (page < pages):
            raise ValueError(f"Unexpected pagination on page {page}")
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), int) or not isinstance(item.get("code"), str) or not isinstance(item.get("name"), str):
                raise ValueError(f"Invalid {TABLE} item on page {page}")
        with db:
            db.executemany(f"INSERT INTO {TABLE}(id,code,page,payload) VALUES(?,?,?,?)", [(item["id"], item["code"], page, json.dumps(item, ensure_ascii=False, separators=(",", ":"))) for item in items])
            db.execute("INSERT INTO downloaded_pages VALUES(?,?,?)", (page, now(), len(items)))

    saved_pages = {row[0] for row in db.execute("SELECT page FROM downloaded_pages")}
    complete = False
    try:
        if 1 not in saved_pages:
            save(1, first)
            saved_pages.add(1)
        pending = [page for page in range(2, pages + 1) if page not in saved_pages]
        print(f"SOURCE {total} records; {pages} pages; saved={len(saved_pages)}; workers={args.workers}", flush=True)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for offset in range(0, len(pending), 20):
                batch = pending[offset:offset + 20]
                for page, result in zip(batch, pool.map(fetch_page, batch)):
                    save(page, result)
                    saved_pages.add(page)
                actual = db.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]
                print(f"CHECKPOINT pages={len(saved_pages)}/{pages}; records={actual}/{total}", flush=True)
        actual = db.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]
        if len(saved_pages) != pages or actual != total:
            raise ValueError(f"Incomplete catalog: {actual}/{total}")
        complete = True
    finally:
        try:
            export(db, args.out, total, started_at, complete)
            if complete and TABLE == "okpd2":
                export_okpd2_links(db, args.out)
        finally:
            db.close()


if __name__ == "__main__":
    main()
