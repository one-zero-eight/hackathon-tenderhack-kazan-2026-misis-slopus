#!/usr/bin/env python3
"""Fast anonymous STE fetcher via zakupki UI APIs (no Playwright).

SOCKS5 from zakupki-proxy -> 94.79.51.128 with Host/SNI zakupki.mos.ru.
  GET /classifier/api/Sku/GetSku?id=...
  GET /newapi/api/Sku/QueryIndex?queryFilter=...
"""
from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests

ROOT = Path(__file__).resolve().parents[1]
PROXY_FILE = ROOT / "secrets" / "zakupki-proxy"
DIRECT_IP = "94.79.51.126"
HOST = "zakupki.mos.ru"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
BOT_MARKERS = (
    "captcha",
    "cloudflare",
    "access denied",
    "bot detection",
    "cf-challenge",
    "attention required",
)


class BotProtected(RuntimeError):
    pass


class GatewayDown(RuntimeError):
    pass


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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Client:
    def __init__(
        self,
        proxy_url: str | None = None,
        bridge: str | None = None,
        *,
        direct: bool = False,
    ):
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Origin": f"https://{HOST}",
                "User-Agent": UA,
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            }
        )
        if bridge:
            # Bridge already remaps CONNECT host -> IP; use real hostname URLs.
            self.base = f"https://{HOST}"
            self.session.proxies = {"http": bridge, "https": bridge}
            self._force_host = False
        elif direct or not proxy_url:
            # Hostname HTTPS (correct SNI). Bypass VPN for 94.79.51.0/24 first.
            self.base = f"https://{HOST}"
            self._force_host = False
        else:
            self.base = f"https://{DIRECT_IP}"
            self.session.proxies = {"http": proxy_url, "https": proxy_url}
            self._force_host = True
        from requests.adapters import HTTPAdapter

        adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=0)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]

    def _get(self, path: str, referer: str, timeout: float = 60.0) -> dict:
        headers = {"Referer": referer}
        if self._force_host:
            headers["Host"] = HOST
        r = self.session.get(self.base + path, headers=headers, timeout=timeout)
        ctype = r.headers.get("Content-Type", "")
        text = r.text
        low = text[:1000].lower()
        if r.status_code in (502, 504) or ("bad gateway" in low) or ("gateway time" in low):
            raise GatewayDown(f"HTTP {r.status_code}: upstream gateway")
        if self._looks_bot(text, ctype) or (r.status_code in (403, 503) and "html" in ctype.lower() and "bad gateway" not in low):
            raise BotProtected(f"HTTP {r.status_code}: possible bot protection")
        if r.status_code == 429:
            raise requests.HTTPError(f"429 {text[:200]}", response=r)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {text[:300]}")
        if "json" not in ctype.lower() and text.lstrip()[:1] not in "{[":
            raise BotProtected(f"HTTP {r.status_code}: non-json body")
        return r.json()

    @staticmethod
    def _looks_bot(text: str, ctype: str) -> bool:
        low = text[:4000].lower()
        if "html" in (ctype or "").lower() and any(m in low for m in BOT_MARKERS):
            return True
        return "<html" in low and any(m in low for m in BOT_MARKERS)

    def get_sku(self, sku_id: int) -> dict:
        path = f"/classifier/api/Sku/GetSku?id={sku_id}&withRegionPriceStatistics=false"
        return self._get(path, referer=f"https://{HOST}/sku/view/{sku_id}")

    def query_index_ids(self, ids: list[int]) -> dict:
        q = {
            "filter": {"ids": {"values": ids}},
            "take": len(ids),
            "skip": 0,
            "withCount": True,
        }
        qf = json.dumps(q, separators=(",", ":"), ensure_ascii=False)
        path = "/newapi/api/Sku/QueryIndex?" + urlencode({"queryFilter": qf})
        return self._get(path, referer=f"https://{HOST}/catalog")

    def query_index_page(
        self,
        *,
        skip: int,
        take: int,
        filt: dict | None = None,
        order: list | None = None,
        with_count: bool = False,
    ) -> dict:
        q = {
            "filter": filt if filt is not None else {},
            "order": order if order is not None else [{"field": "id", "desc": False}],
            "skip": skip,
            "take": take,
            "withCount": with_count,
        }
        qf = json.dumps(q, separators=(",", ":"), ensure_ascii=False)
        path = "/newapi/api/Sku/QueryIndex?" + urlencode({"queryFilter": qf})
        return self._get(path, referer=f"https://{HOST}/catalog")


def export_jsonl_gz(db: Path, out: Path, meta_extra: dict, list_source: str = "ste.jsonl.gz"):
    con = sqlite3.connect(db)
    try:
        # Pin one SQLite snapshot for the count and streaming JSONL export.
        con.execute("BEGIN")
        count = con.execute("SELECT COUNT(*) FROM sku_details").fetchone()[0]
        use_gzip = out.suffix == ".gz" or out.name.endswith(".jsonl.gz")
        meta = {
            "source": f"https://{HOST}/classifier/api/Sku/GetSku",
            "listSource": list_source,
            "exportedAt": now_iso(),
            **meta_extra,
            "recordCount": count,
            "format": "jsonl",
        }
        if use_gzip:
            meta["compression"] = "gzip"
        meta_path = Path(str(out) + ".meta.json")
        if meta_path.exists():
            try:
                prev = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(prev, dict):
                    meta = {**prev, **meta}
            except (OSError, json.JSONDecodeError):
                pass

        temporary = out.with_name(out.name + ".part")
        opener = gzip.open if use_gzip else open
        with opener(temporary, "wt", encoding="utf-8") as stream:
            for (payload,) in con.execute(
                "SELECT d.payload FROM sku_details d "
                "JOIN targets t USING(sku_id) ORDER BY t.ordinal"
            ):
                stream.write(json.dumps(json.loads(payload), ensure_ascii=False, separators=(",", ":")) + "\n")
        temporary.replace(out)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    finally:
        con.close()


def make_client(args) -> Client:
    if getattr(args, "bridge", None):
        return Client(bridge=args.bridge)
    if getattr(args, "direct", False):
        return Client(direct=True)
    return Client(proxy_url=load_socks_url())


def cmd_get(args):
    client = make_client(args)
    card = client.get_sku(args.id)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"ok id={card.get('id')} name={(card.get('name') or '')[:80]} "
        f"chars={len(card.get('skuCharacteristics') or [])} desc={len(card.get('description') or [])}"
    )


def cmd_query(args):
    client = make_client(args)
    ids = [int(x) for x in args.ids.split(",")]
    data = client.query_index_ids(ids)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"ok count={data.get('count')} items={len(data.get('items') or [])}")


def fetch_one(client: Client, sku_id: int, pause: float) -> tuple[int, dict | None, str | None]:
    attempt = 0
    while True:
        attempt += 1
        try:
            card = client.get_sku(sku_id)
            if not isinstance(card.get("id"), int):
                raise RuntimeError(f"bad payload keys={list(card)[:10]}")
            if pause > 0:
                time.sleep(pause)
            return sku_id, card, None
        except GatewayDown as e:
            if attempt >= 8:
                return sku_id, None, f"GatewayDown: {e}"
            time.sleep(min(120, 3 * attempt))
            continue
        except BotProtected:
            raise
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429 and attempt < 6:
                time.sleep(min(60, 5 * attempt))
                continue
            return sku_id, None, f"HTTPERR {e}"
        except RuntimeError as e:
            msg = str(e)
            # Permanent access denial — do not retry forever.
            if "HTTP 403" in msg and "Недостаточно полномочий" in msg:
                return sku_id, {
                    "id": sku_id,
                    "_fetchError": {
                        "httpStatus": 403,
                        "message": "Недостаточно полномочий для совершения операции",
                        "skippedAt": now_iso(),
                    },
                }, None
            if attempt >= 4:
                return sku_id, None, f"RuntimeError: {e}"
            time.sleep(1.5 * attempt)
        except Exception as e:
            if attempt >= 4:
                return sku_id, None, f"{type(e).__name__}: {e}"
            time.sleep(1.5 * attempt)


def iter_source_records(source: Path):
    opener = gzip.open if source.suffix == ".gz" else open
    with opener(source, "rt", encoding="utf-8") as stream:
        if source.name.removesuffix(".gz").endswith(".jsonl"):
            for line in stream:
                if line.strip():
                    yield json.loads(line)
        else:
            value = json.load(stream)
            yield from value["items"] if isinstance(value, dict) else value


def load_ids(source: Path) -> list[int]:
    return [int(obj["id"]) for obj in iter_source_records(source)]


def resume_download(
    all_ids: list[int],
    db: Path,
    out: Path,
    *,
    bridge: str | None = None,
    direct: bool = False,
    pause: float = 0.05,
    workers: int = 4,
    list_source: str = "ste.jsonl.gz",
):
    class Args:
        pass

    args = Args()
    args.bridge = bridge
    args.direct = direct

    con = sqlite3.connect(db, check_same_thread=False)
    con.execute(
        "create table if not exists sku_details ("
        "sku_id integer primary key, fetched_at text not null, payload text not null)"
    )
    con.execute(
        "create table if not exists targets (ordinal integer not null, sku_id integer primary key)"
    )
    # keep targets in sync with current list (expandable)
    con.executemany(
        "insert or ignore into targets(ordinal, sku_id) values(?,?)",
        list(enumerate(all_ids)),
    )
    con.commit()

    done = {r[0] for r in con.execute("select sku_id from sku_details")}
    pending = [i for i in all_ids if i not in done]
    workers = max(1, int(workers))
    print(
        f"resume: list={len(all_ids)} done={len(done)} pending={len(pending)} "
        f"pause={pause}s workers={workers}",
        flush=True,
    )
    if not pending:
        export_jsonl_gz(
            db,
            out,
            {"stopReason": "complete", "recordCount": len(done), "targetCount": len(all_ids), "via": "ui-GetSku"},
            list_source=list_source,
        )
        print("nothing pending", flush=True)
        con.close()
        return

    clients = [make_client(args) for _ in range(workers)]
    lock = threading.Lock()
    ok = 0
    fail = 0
    t0 = time.perf_counter()
    bot_hit = False
    inflight_limit = workers * 8

    def save(sku_id: int, card: dict):
        with lock:
            con.execute(
                "insert or replace into sku_details(sku_id, fetched_at, payload) values(?,?,?)",
                (sku_id, now_iso(), json.dumps(card, ensure_ascii=False)),
            )
            con.commit()

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending_iter = iter(pending)
            futs = {}

            def fill():
                while len(futs) < inflight_limit:
                    try:
                        sku_id = next(pending_iter)
                    except StopIteration:
                        break
                    futs[pool.submit(fetch_one, clients[len(futs) % workers], sku_id, pause)] = sku_id

            fill()
            n = 0
            while futs:
                done_futs = []
                # wait for any completion
                for fut in as_completed(list(futs.keys()), timeout=None):
                    done_futs.append(fut)
                    break
                for fut in done_futs:
                    sku_id = futs.pop(fut)
                    try:
                        sku_id, card, err = fut.result()
                    except BotProtected as e:
                        bot_hit = True
                        print(f"BOT_PROTECTION: {e}; cooldown 180s then retry sku={sku_id}", flush=True)
                        time.sleep(180)
                        futs[pool.submit(fetch_one, clients[0], sku_id, pause)] = sku_id
                        fill()
                        continue
                    n += 1
                    if card is not None:
                        save(sku_id, card)
                        ok += 1
                    else:
                        fail += 1
                        print(f"FAIL sku={sku_id}: {err}", flush=True)
                        # if many consecutive gateway fails, back off
                        if err and "GatewayDown" in err:
                            print("gateway failures — sleeping 60s", flush=True)
                            time.sleep(60)
                    if n % 50 == 0 or n == len(pending):
                        rate = ok / max(time.perf_counter() - t0, 1e-6)
                        print(
                            f"progress {len(done)+ok}/{len(all_ids)} ok={ok} fail={fail} "
                            f"{rate:.2f}/s last={sku_id}",
                            flush=True,
                        )
                    # Full gzip rewrite gets slow on large DBs; keep rare checkpoints only.
                    if n % 25000 == 0:
                        export_jsonl_gz(
                            db,
                            out,
                            {
                                "stopReason": "running",
                                "via": "ui-GetSku",
                                "proxyMode": (
                                    "http-bridge"
                                    if bridge
                                    else ("hostname-direct" if direct else "socks5-direct-ip")
                                ),
                                "workers": workers,
                                "failedCount": fail,
                            },
                            list_source=list_source,
                        )
                    fill()
    finally:
        total = con.execute("select count(*) from sku_details").fetchone()[0]
        export_jsonl_gz(
            db,
            out,
            {
                "stopReason": "bot" if bot_hit else ("complete" if total >= len(all_ids) else "paused"),
                "recordCount": total,
                "targetCount": len(all_ids),
                "via": "ui-GetSku",
                "finishedAt": now_iso(),
                "failedCount": fail,
                "workers": workers,
            },
            list_source=list_source,
        )
        con.close()
        print(f"done saved={total}/{len(all_ids)} ok_run={ok} fail={fail}", flush=True)


def fetch_query_batch(
    client: Client, batch_ids: list[int], pause: float
) -> tuple[list[int], list[dict], list[int], str | None]:
    """Return (batch_ids, items, missing_ids, error)."""
    attempt = 0
    while True:
        attempt += 1
        try:
            data = client.query_index_ids(batch_ids)
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise RuntimeError(f"bad QueryIndex shape keys={list(data)[:10] if isinstance(data, dict) else type(data)}")
            wanted = set(batch_ids)
            returned = []
            seen = set()
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("id"), int):
                    continue
                if item["id"] in seen or item["id"] not in wanted:
                    continue
                seen.add(item["id"])
                returned.append(item)
            missing = [i for i in batch_ids if i not in seen]
            if pause > 0:
                time.sleep(pause)
            return batch_ids, returned, missing, None
        except GatewayDown as e:
            if attempt >= 8:
                return batch_ids, [], batch_ids, f"GatewayDown: {e}"
            time.sleep(min(120, 3 * attempt))
        except BotProtected:
            raise
        except Exception as e:
            if attempt >= 4:
                return batch_ids, [], batch_ids, f"{type(e).__name__}: {e}"
            time.sleep(1.5 * attempt)


def resume_download_query_index(
    all_ids: list[int],
    db: Path,
    out: Path,
    *,
    bridge: str | None = None,
    pause: float = 0.0,
    workers: int = 8,
    batch_size: int = 50,
    list_source: str = "ste.jsonl.gz",
):
    """Batch-fetch cards via anonymous QueryIndex?filter.ids (much faster than GetSku)."""

    class Args:
        pass

    args = Args()
    args.bridge = bridge

    con = sqlite3.connect(db, check_same_thread=False)
    con.execute(
        "create table if not exists sku_details ("
        "sku_id integer primary key, fetched_at text not null, payload text not null)"
    )
    con.execute(
        "create table if not exists targets (ordinal integer not null, sku_id integer primary key)"
    )
    con.executemany(
        "insert or ignore into targets(ordinal, sku_id) values(?,?)",
        list(enumerate(all_ids)),
    )
    con.commit()

    done = {r[0] for r in con.execute("select sku_id from sku_details")}
    pending = [i for i in all_ids if i not in done]
    workers = max(1, int(workers))
    batch_size = max(1, int(batch_size))
    chunks = [pending[i : i + batch_size] for i in range(0, len(pending), batch_size)]
    print(
        f"resume-qi: list={len(all_ids)} done={len(done)} pending={len(pending)} "
        f"chunks={len(chunks)} batch={batch_size} pause={pause}s workers={workers}",
        flush=True,
    )
    via = "ui-QueryIndex"
    source_url = f"https://{HOST}/newapi/api/Sku/QueryIndex"
    if not pending:
        export_jsonl_gz(
            db,
            out,
            {
                "stopReason": "complete",
                "recordCount": len(done),
                "targetCount": len(all_ids),
                "via": via,
                "source": source_url,
            },
            list_source=list_source,
        )
        print("nothing pending", flush=True)
        con.close()
        return

    clients = [make_client(args) for _ in range(workers)]
    lock = threading.Lock()
    ok = 0
    missing_marked = 0
    fail = 0
    t0 = time.perf_counter()
    bot_hit = False
    inflight_limit = workers * 2

    def save_many(cards: list[dict]):
        with lock:
            fetched_at = now_iso()
            con.executemany(
                "insert or replace into sku_details(sku_id, fetched_at, payload) values(?,?,?)",
                [(card["id"], fetched_at, json.dumps(card, ensure_ascii=False)) for card in cards],
            )
            con.commit()

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            chunk_iter = iter(chunks)
            futs = {}

            def fill():
                while len(futs) < inflight_limit:
                    try:
                        batch = next(chunk_iter)
                    except StopIteration:
                        break
                    futs[pool.submit(fetch_query_batch, clients[len(futs) % workers], batch, pause)] = batch

            fill()
            n = 0
            while futs:
                done_futs = []
                for fut in as_completed(list(futs.keys()), timeout=None):
                    done_futs.append(fut)
                    break
                for fut in done_futs:
                    batch = futs.pop(fut)
                    try:
                        _batch, items, missing, err = fut.result()
                    except BotProtected as e:
                        bot_hit = True
                        print(
                            f"AGENT_ALERT kind=bot_protection detail={e!s}; "
                            f"cooldown 180s then retry batch={batch[:3]}...",
                            flush=True,
                        )
                        time.sleep(180)
                        futs[pool.submit(fetch_query_batch, clients[0], batch, pause)] = batch
                        fill()
                        continue
                    n += 1
                    if err:
                        fail += 1
                        print(
                            f"AGENT_ALERT kind=batch_fail "
                            f"batch={batch[0]}..{batch[-1]} size={len(batch)} detail={err}",
                            flush=True,
                        )
                        if "GatewayDown" in err or "Host unreachable" in err:
                            print(
                                "AGENT_ALERT kind=proxy_unreachable sleeping=30s",
                                flush=True,
                            )
                            time.sleep(30)
                        # re-queue once-worth by putting back? leave pending for next run
                    else:
                        if items:
                            save_many(items)
                            ok += len(items)
                        if missing:
                            stubs = [
                                {
                                    "id": sku_id,
                                    "_fetchError": {
                                        "httpStatus": 403,
                                        "message": "Not returned by QueryIndex (likely access-restricted)",
                                        "skippedAt": now_iso(),
                                    },
                                }
                                for sku_id in missing
                            ]
                            save_many(stubs)
                            missing_marked += len(missing)
                    if n % 10 == 0 or n == len(chunks):
                        rate = (ok + missing_marked) / max(time.perf_counter() - t0, 1e-6)
                        print(
                            f"progress {len(done)+ok+missing_marked}/{len(all_ids)} "
                            f"cards={ok} missing={missing_marked} fail_batches={fail} "
                            f"{rate:.1f}/s",
                            flush=True,
                        )
                    # Full jsonl export of 300k cards is multi-minute; only finalize in `finally`.
                    fill()
    finally:
        total = con.execute("select count(*) from sku_details").fetchone()[0]
        export_jsonl_gz(
            db,
            out,
            {
                "stopReason": "bot" if bot_hit else ("complete" if total >= len(all_ids) else "paused"),
                "recordCount": total,
                "targetCount": len(all_ids),
                "via": via,
                "source": source_url,
                "finishedAt": now_iso(),
                "failedCount": fail,
                "missingCount": missing_marked,
                "workers": workers,
                "batchSize": batch_size,
            },
            list_source=list_source,
        )
        con.close()
        print(
            f"done saved={total}/{len(all_ids)} cards={ok} missing={missing_marked} fail_batches={fail}",
            flush=True,
        )


CATALOG_FILTER = {
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
CATALOG_ORDER = [{"field": "id", "desc": False}]


def fetch_query_page(
    client: Client, skip: int, take: int, *, with_count: bool = False
) -> tuple[dict | None, str | None]:
    attempt = 0
    while True:
        attempt += 1
        try:
            return (
                client.query_index_page(
                    skip=skip,
                    take=take,
                    filt=CATALOG_FILTER,
                    order=CATALOG_ORDER,
                    with_count=with_count,
                ),
                None,
            )
        except GatewayDown as e:
            if attempt >= 8:
                return None, f"GatewayDown: {e}"
            time.sleep(min(120, 3 * attempt))
        except BotProtected:
            raise
        except Exception as e:
            if attempt >= 6:
                return None, f"{type(e).__name__}: {e}"
            time.sleep(min(60, 1.5 * attempt))


def catalog_total(bridge: str | None = None) -> int:
    class Args:
        pass

    args = Args()
    args.bridge = bridge
    data, err = fetch_query_page(make_client(args), 0, 1, with_count=True)
    if err or not data or not isinstance(data.get("count"), int) or data["count"] <= 0:
        raise RuntimeError(f"catalog count failed: {err or data}")
    return data["count"]


def _list_shard_worker(args: tuple) -> dict:
    shard_id, n_shards, take, total, bridge, workers, shard_dir = args
    shard_dir = Path(shard_dir)
    db = shard_dir / f"shard-{shard_id:02d}.sqlite"
    out = shard_dir / f"shard-{shard_id:02d}.jsonl.gz"
    page_count = (total + take - 1) // take
    pages = list(range(shard_id, page_count, n_shards))

    class Args:
        pass

    a = Args()
    a.bridge = bridge
    con = sqlite3.connect(db, check_same_thread=False)
    con.execute(
        "create table if not exists sku_details ("
        "sku_id integer primary key, fetched_at text not null, payload text not null)"
    )
    con.execute(
        "create table if not exists targets (ordinal integer not null, sku_id integer primary key)"
    )
    con.execute(
        "create table if not exists pages_done (page_index integer primary key, item_count integer not null)"
    )
    con.commit()
    done = {r[0] for r in con.execute("select page_index from pages_done")}
    pending = [p for p in pages if p not in done]
    print(f"shard {shard_id}: pending_pages={len(pending)}/{len(pages)} workers={workers}", flush=True)
    if not pending:
        export_jsonl_gz(db, out, {"via": "ui-QueryIndex-list", "shard": shard_id}, list_source="catalog")
        n = con.execute("select count(*) from sku_details").fetchone()[0]
        con.close()
        return {"shard": shard_id, "saved": n, "pages": 0}

    clients = [make_client(a) for _ in range(max(1, workers))]
    lock = threading.Lock()
    ok = 0
    t0 = time.perf_counter()

    def one(page_index: int):
        data, err = fetch_query_page(clients[page_index % len(clients)], page_index * take, take)
        if err:
            return page_index, None, err
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return page_index, None, "bad items"
        return page_index, items, None

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(one, p): p for p in pending}
        for fut in as_completed(futs):
            page_index = futs[fut]
            try:
                page_index, items, err = fut.result()
            except BotProtected as e:
                print(f"AGENT_ALERT kind=bot_protection shard={shard_id} detail={e}", flush=True)
                time.sleep(180)
                continue
            if err or items is None:
                print(f"AGENT_ALERT kind=page_fail shard={shard_id} page={page_index} detail={err}", flush=True)
                continue
            rows = []
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("id"), int):
                    rows.append(
                        (
                            item["id"],
                            now_iso(),
                            json.dumps(item, ensure_ascii=False, separators=(",", ":")),
                        )
                    )
            with lock:
                with con:
                    if rows:
                        con.executemany(
                            "insert or replace into sku_details(sku_id,fetched_at,payload) values(?,?,?)",
                            rows,
                        )
                        con.executemany(
                            "insert or ignore into targets(ordinal,sku_id) values(?,?)",
                            [(page_index * take + i, sku_id) for i, (sku_id, _, _) in enumerate(rows)],
                        )
                    con.execute(
                        "insert or replace into pages_done(page_index,item_count) values(?,?)",
                        (page_index, len(rows)),
                    )
            ok += 1
            if ok % 25 == 0 or ok == len(pending):
                saved = con.execute("select count(*) from sku_details").fetchone()[0]
                rate = ok * take / max(time.perf_counter() - t0, 1e-6)
                print(f"shard {shard_id}: {ok}/{len(pending)} pages saved={saved} ~{rate:.0f}/s", flush=True)

    export_jsonl_gz(db, out, {"via": "ui-QueryIndex-list", "shard": shard_id}, list_source="catalog")
    saved = con.execute("select count(*) from sku_details").fetchone()[0]
    con.close()
    return {"shard": shard_id, "saved": saved, "pages": ok}


def resume_download_catalog(
    out: Path,
    shard_dir: Path,
    *,
    bridge: str | None = None,
    shards: int = 6,
    workers: int = 4,
    take: int = 100,
    enrich_workers: int = 8,
    enrich_batch: int = 50,
    skip_enrich: bool = False,
) -> dict:
    """Full-catalog QueryIndex dump with parallel skip shards; optional ids enrich."""
    from concurrent.futures import ProcessPoolExecutor

    shard_dir.mkdir(parents=True, exist_ok=True)
    total = catalog_total(bridge)
    print(f"catalog total={total} shards={shards} workers={workers} take={take}", flush=True)
    t0 = time.perf_counter()

    with ProcessPoolExecutor(max_workers=shards) as pool:
        jobs = [
            (i, shards, take, total, bridge, workers, str(shard_dir)) for i in range(shards)
        ]
        for result in pool.map(_list_shard_worker, jobs):
            print(f"list {result}", flush=True)

    pending_ids: list[int] = []
    if not skip_enrich:
        for i in range(shards):
            db = shard_dir / f"shard-{i:02d}.sqlite"
            if not db.exists():
                continue
            con = sqlite3.connect(db)
            for (payload,) in con.execute("select payload from sku_details"):
                card = json.loads(payload)
                if not isinstance(card.get("skuCharacteristics"), list):
                    pending_ids.append(card["id"])
            con.close()
        pending_ids = sorted(set(pending_ids))
        print(f"enrich pending={len(pending_ids)}", flush=True)
        if pending_ids:
            resume_download_query_index(
                pending_ids,
                shard_dir / "enrich.sqlite",
                shard_dir / "enrich.jsonl.gz",
                bridge=bridge,
                pause=0.0,
                workers=enrich_workers,
                batch_size=enrich_batch,
                list_source="catalog-enrich",
            )
            eco = sqlite3.connect(shard_dir / "enrich.sqlite")
            by_id = {r[0]: r[1] for r in eco.execute("select sku_id, payload from sku_details")}
            eco.close()
            for i in range(shards):
                db = shard_dir / f"shard-{i:02d}.sqlite"
                if not db.exists():
                    continue
                con = sqlite3.connect(db)
                ids = [r[0] for r in con.execute("select sku_id from sku_details")]
                with con:
                    for sku_id in ids:
                        if sku_id in by_id:
                            con.execute(
                                "update sku_details set payload=?, fetched_at=? where sku_id=?",
                                (by_id[sku_id], now_iso(), sku_id),
                            )
                con.close()

    temporary = out.with_name(out.name + ".part")
    count = 0
    seen: set[int] = set()
    with gzip.open(temporary, "wt", encoding="utf-8") as stream:
        cons = []
        cursors = []
        for i in range(shards):
            db = shard_dir / f"shard-{i:02d}.sqlite"
            if not db.exists():
                continue
            con = sqlite3.connect(db)
            cur = con.execute("select sku_id, payload from sku_details order by sku_id")
            cons.append(con)
            cursors.append([cur, cur.fetchone()])
        while True:
            best_i, best_id = None, None
            for i, (_c, row) in enumerate(cursors):
                if row and (best_id is None or row[0] < best_id):
                    best_i, best_id = i, row[0]
            if best_i is None:
                break
            row = cursors[best_i][1]
            if best_id not in seen:
                seen.add(best_id)
                payload = row[1]
                stream.write(payload if payload.endswith("\n") else payload + "\n")
                count += 1
            for i, (cur, r) in enumerate(cursors):
                while r is not None and r[0] == best_id:
                    r = cur.fetchone()
                cursors[i][1] = r
            if count % 200000 == 0:
                print(f"export {count}...", flush=True)
        for con in cons:
            con.close()
    temporary.replace(out)
    elapsed = time.perf_counter() - t0
    meta = {
        "source": f"https://{HOST}/newapi/api/Sku/QueryIndex",
        "filter": CATALOG_FILTER,
        "order": CATALOG_ORDER,
        "apiTotalCount": total,
        "recordCount": count,
        "shards": shards,
        "workers": workers,
        "take": take,
        "elapsedSeconds": round(elapsed, 1),
        "itemsPerSecond": round(count / max(elapsed, 1e-6), 1),
        "finishedAt": now_iso(),
        "stopReason": "complete" if count >= total * 0.99 else "partial",
        "format": "jsonl",
        "compression": "gzip",
        "via": "ui-QueryIndex-catalog",
    }
    Path(str(out) + ".meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"DONE {count}/{total} in {elapsed/60:.1f}m ({count/max(elapsed,1e-6):.0f}/s) -> {out}", flush=True)
    return meta


def cmd_resume_sample(args):
    source = Path(args.source)
    resume_download(
        load_ids(source),
        Path(args.db),
        Path(args.out),
        bridge=args.bridge,
        direct=getattr(args, "direct", False),
        pause=args.pause,
        workers=args.workers,
        list_source=str(source),
    )


def cmd_resume_qi(args):
    source = Path(args.source)
    resume_download_query_index(
        load_ids(source),
        Path(args.db),
        Path(args.out),
        bridge=args.bridge,
        pause=args.pause,
        workers=args.workers,
        batch_size=args.batch_size,
        list_source=str(source),
    )


def cmd_download_all(args):
    resume_download_catalog(
        Path(args.out),
        Path(args.shard_dir),
        bridge=args.bridge,
        shards=args.shards,
        workers=args.workers,
        take=args.take,
        enrich_workers=args.enrich_workers,
        enrich_batch=args.enrich_batch,
        skip_enrich=args.skip_enrich,
    )


def main():
    p = argparse.ArgumentParser(description="STE UI API fetcher")
    p.add_argument("--bridge", default=None, help="HTTP bridge e.g. http://127.0.0.1:18080")
    p.add_argument(
        "--direct",
        action="store_true",
        help="Call zakupki.mos.ru without SOCKS (hostname HTTPS)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("get")
    g.add_argument("--id", type=int, required=True)
    g.add_argument("--out", default=str(ROOT / "data/tmp/ste-getsku-one.json"))
    g.set_defaults(func=cmd_get)

    q = sub.add_parser("query")
    q.add_argument("--ids", required=True, help="comma-separated ids")
    q.add_argument("--out", default=str(ROOT / "data/tmp/ste-query-batch.json"))
    q.set_defaults(func=cmd_query)

    r = sub.add_parser("resume-sample")
    r.add_argument("--source", default=str(ROOT / "data" / "ste.jsonl.gz"), help="JSONL (.jsonl or .jsonl.gz), JSON array or JSON items wrapper")
    r.add_argument("--db", default=str(ROOT / "data" / "tmp" / "ste-details.sqlite"))
    r.add_argument("--out", default=str(ROOT / "data" / "ste-details.jsonl.gz"))
    r.add_argument("--pause", type=float, default=0.05)
    r.add_argument("--workers", type=int, default=4)
    r.set_defaults(func=cmd_resume_sample)

    rq = sub.add_parser("resume-qi", help="Batch cards via QueryIndex?filter.ids (faster than GetSku)")
    rq.add_argument("--source", default=str(ROOT / "data" / "ste.jsonl.gz"))
    rq.add_argument("--db", default=str(ROOT / "data" / "tmp" / "ste-details.sqlite"))
    rq.add_argument("--out", default=str(ROOT / "data" / "ste-details.jsonl.gz"))
    rq.add_argument("--pause", type=float, default=0.0)
    rq.add_argument("--workers", type=int, default=8)
    rq.add_argument("--batch-size", type=int, default=50)
    rq.set_defaults(func=cmd_resume_qi)

    da = sub.add_parser("download-all", help="Full catalog via parallel QueryIndex pages")
    da.add_argument("--out", default=str(ROOT / "data" / "ste-all.jsonl.gz"))
    da.add_argument("--shard-dir", default=str(ROOT / "data" / "ste-all-shards"))
    da.add_argument("--shards", type=int, default=6)
    da.add_argument("--workers", type=int, default=4)
    da.add_argument("--take", type=int, default=100)
    da.add_argument("--enrich-workers", type=int, default=8)
    da.add_argument("--enrich-batch", type=int, default=50)
    da.add_argument("--skip-enrich", action="store_true")
    da.set_defaults(func=cmd_download_all)

    args = p.parse_args()
    try:
        args.func(args)
    except BotProtected:
        sys.exit(2)


if __name__ == "__main__":
    main()
