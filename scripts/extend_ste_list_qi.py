#!/usr/bin/env python3
"""Extend ste.jsonl.gz to target count via anonymous QueryIndex (no official Search quota)."""
from __future__ import annotations

import argparse
import gzip
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings()

ROOT = Path(__file__).resolve().parents[1]
PROXY_FILE = ROOT / "secrets" / "zakupki-proxy"
OUTPUT = ROOT / "data" / "ste.jsonl.gz"
META = ROOT / "data" / "ste.jsonl.gz.meta.json"
DIRECT_IP = "94.79.51.128"
HOST = "zakupki.mos.ru"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
FILTER = {
    "productionDirectoryPaths": {"values": ["."]},
    "entityState": {"values": [2]},
    "skuStateId": {"values": [61000002]},
    "characteristicFilter": [],
    "characteristicName": {"contains": True},
    "characteristicValue": {"contains": True},
    "ids": {},
    "keyword": "",
    "modelName": {"contains": True},
    "name": {"contains": True},
    "offersCount": {},
    "referencePrice": {},
    "okpdPaths": {},
    "productionDirectoryId": {},
    "productionDirectoryRequestType": {},
    "productionPath": {},
    "manufacturerId": {},
    "supplierKeyword": "",
    "tagIds": {},
    "tags": {},
    "vendorId": {},
}
ORDER = [{"field": "id", "desc": False}]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_socks_url() -> str:
    for line in PROXY_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        endpoint, credentials = line.rsplit("@", 1)
        host, port = endpoint.split(":", 1)
        user, password = credentials.split(":", 1)
        return f"socks5h://{user}:{password}@{host}:{port}"
    raise RuntimeError(f"No proxy in {PROXY_FILE}")


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


def read_all():
    items = list(iter_records(OUTPUT)) if OUTPUT.exists() else []
    meta = {}
    if META.exists():
        meta = json.loads(META.read_text(encoding="utf-8"))
    return items, meta


def write_meta(meta: dict):
    partial = META.with_name(META.name + ".part")
    partial.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(partial, META)


def make_session() -> requests.Session:
    s = requests.Session()
    s.verify = False
    s.proxies = {"http": load_socks_url(), "https": load_socks_url()}
    s.headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "Origin": f"https://{HOST}",
            "User-Agent": UA,
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Host": HOST,
        }
    )
    return s


def query_page(session: requests.Session, skip: int, take: int) -> dict:
    q = {
        "filter": FILTER,
        "order": ORDER,
        "skip": skip,
        "take": take,
        "withCount": skip == 0,
    }
    r = session.get(
        f"https://{DIRECT_IP}/newapi/api/Sku/QueryIndex",
        params={"queryFilter": json.dumps(q, separators=(",", ":"), ensure_ascii=False)},
        headers={"Referer": f"https://{HOST}/catalog"},
        timeout=90,
    )
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=50000)
    ap.add_argument("--batch", type=int, default=100)
    ap.add_argument("--pause", type=float, default=0.15)
    args = ap.parse_args()

    items, meta = read_all()
    ids = {x["id"] for x in items}
    if any(items[i]["id"] >= items[i + 1]["id"] for i in range(len(items) - 1)):
        raise RuntimeError("existing list not ordered by id")
    print(f"start count={len(items)} target={args.target}", flush=True)
    if len(items) >= args.target:
        print("already at target", flush=True)
        return

    session = make_session()
    meta.update(
        {
            "format": "jsonl",
            "compression": "gzip",
            "listVia": "QueryIndex",
            "order": ORDER,
            "filter": FILTER,
            "targetCount": args.target,
            "batchSize": args.batch,
            "pauseSeconds": args.pause,
            "resumeStartedAt": now_iso(),
            "stopReason": "in_progress",
            "finishedAt": None,
        }
    )
    # keep original Search source note if present
    if "source" not in meta:
        meta["source"] = "https://zakupki.mos.ru/newapi/api/Sku/QueryIndex"
    meta["primaryListSource"] = "https://zakupki.mos.ru/newapi/api/Sku/QueryIndex"
    write_meta(meta)

    stop = "target_reached"
    req = 0
    t0 = time.perf_counter()
    try:
        while len(items) < args.target:
            take = min(args.batch, args.target - len(items))
            skip = len(items)
            attempt = 0
            while True:
                attempt += 1
                try:
                    data = query_page(session, skip=skip, take=take)
                    break
                except Exception as e:
                    if attempt >= 12:
                        raise
                    wait = min(120, 3 * attempt)
                    print(f"retry skip={skip} attempt={attempt}/{12} wait={wait}s: {e}", flush=True)
                    time.sleep(wait)
                    # refresh session after proxy/auth blips
                    if attempt % 3 == 0:
                        session = make_session()
            req += 1
            if "count" in data and data["count"] is not None:
                meta["apiTotalCount"] = data["count"]
            batch = data.get("items") or []
            if not batch:
                stop = "end_of_results"
                break
            batch_ids = [x.get("id") for x in batch]
            if None in batch_ids or len(set(batch_ids)) != len(batch_ids):
                stop = "invalid_ids"
                break
            if ids.intersection(batch_ids):
                # overlap — keep only new
                batch = [x for x in batch if x["id"] not in ids]
                batch_ids = [x["id"] for x in batch]
                if not batch:
                    stop = "no_progress_overlap"
                    break
            if items and batch_ids[0] <= items[-1]["id"]:
                stop = "invalid_order"
                print(f"order break last={items[-1]['id']} next={batch_ids[0]}", flush=True)
                break
            if any(batch_ids[i] >= batch_ids[i + 1] for i in range(len(batch_ids) - 1)):
                stop = "invalid_batch_order"
                break
            with gzip.open(OUTPUT, "at", encoding="utf-8") as stream:
                for item in batch:
                    stream.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
            ids.update(batch_ids)
            items.extend(batch)
            meta.update({"recordCount": len(items), "checkpointAt": now_iso(), "requestCount": meta.get("requestCount", 0) + 1})
            write_meta(meta)
            print(
                f"list {len(items)}/{args.target} +{len(batch)} skip={skip} "
                f"{(len(items)-(skip))/max(time.perf_counter()-t0,1e-6):.1f}/s last={batch_ids[-1]}",
                flush=True,
            )
            if len(batch) < take:
                stop = "end_of_results"
                break
            if args.pause > 0:
                time.sleep(args.pause)
    except BaseException:
        stop = "interrupted_or_error"
        raise
    finally:
        meta.update(
            {
                "recordCount": len(items),
                "finishedAt": now_iso(),
                "stopReason": stop if len(items) < args.target else "target_reached",
                "lastRunRequestCount": req,
            }
        )
        write_meta(meta)
        print(f"DONE list={len(items)} stop={meta['stopReason']} bytes={OUTPUT.stat().st_size}", flush=True)


if __name__ == "__main__":
    main()
