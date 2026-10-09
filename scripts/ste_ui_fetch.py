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
DIRECT_IP = "94.79.51.128"
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
    def __init__(self, proxy_url: str | None = None, bridge: str | None = None):
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
        else:
            self.base = f"https://{DIRECT_IP}"
            self.session.proxies = {"http": proxy_url, "https": proxy_url}
            self._force_host = True
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


def export_jsonl_gz(db: Path, out: Path, meta_extra: dict):
    con = sqlite3.connect(db)
    try:
        # Pin one SQLite snapshot for the count and streaming JSONL export.
        con.execute("BEGIN")
        count = con.execute("SELECT COUNT(*) FROM sku_details").fetchone()[0]
        meta = {
            "source": f"https://{HOST}/classifier/api/Sku/GetSku",
            "listSource": "ste.jsonl.gz",
            "exportedAt": now_iso(),
            **meta_extra,
            "recordCount": count,
            "format": "jsonl",
            "compression": "gzip",
        }
        meta_path = Path(str(out) + ".meta.json")
        if meta_path.exists():
            try:
                prev = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(prev, dict):
                    meta = {**prev, **meta}
            except (OSError, json.JSONDecodeError):
                pass

        temporary = out.with_name(out.name + ".part")
        with gzip.open(temporary, "wt", encoding="utf-8") as stream:
            for (payload,) in con.execute("SELECT payload FROM sku_details ORDER BY sku_id"):
                stream.write(json.dumps(json.loads(payload), ensure_ascii=False, separators=(",", ":")) + "\n")
        temporary.replace(out)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    finally:
        con.close()


def make_client(args) -> Client:
    if args.bridge:
        return Client(bridge=args.bridge)
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


def cmd_resume_sample(args):
    db = Path(args.db)
    source = Path(args.source)
    out = Path(args.out)
    all_ids = load_ids(source)

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
    workers = max(1, int(args.workers))
    print(
        f"resume: list={len(all_ids)} done={len(done)} pending={len(pending)} "
        f"pause={args.pause}s workers={workers}",
        flush=True,
    )
    if not pending:
        export_jsonl_gz(
            db,
            out,
            {"stopReason": "complete", "recordCount": len(done), "targetCount": len(all_ids), "via": "ui-GetSku"},
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
                    futs[pool.submit(fetch_one, clients[len(futs) % workers], sku_id, args.pause)] = sku_id

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
                        futs[pool.submit(fetch_one, clients[0], sku_id, args.pause)] = sku_id
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
                    if n % 2000 == 0:
                        export_jsonl_gz(
                            db,
                            out,
                            {
                                "stopReason": "running",
                                "via": "ui-GetSku",
                                "proxyMode": "socks5-direct-ip" if not args.bridge else "http-bridge",
                                "workers": workers,
                                "failedCount": fail,
                            },
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
        )
        con.close()
        print(f"done saved={total}/{len(all_ids)} ok_run={ok} fail={fail}", flush=True)


def main():
    p = argparse.ArgumentParser(description="STE UI API fetcher")
    p.add_argument("--bridge", default=None, help="HTTP bridge e.g. http://127.0.0.1:18080")
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
    r.add_argument("--db", default=str(ROOT / "data" / "ste-details.sqlite"))
    r.add_argument("--out", default=str(ROOT / "data" / "ste-details.jsonl.gz"))
    r.add_argument("--pause", type=float, default=0.05)
    r.add_argument("--workers", type=int, default=4)
    r.set_defaults(func=cmd_resume_sample)

    args = p.parse_args()
    try:
        args.func(args)
    except BotProtected:
        sys.exit(2)


if __name__ == "__main__":
    main()
