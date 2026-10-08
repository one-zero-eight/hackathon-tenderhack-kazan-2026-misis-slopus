#!/usr/bin/env python3
"""Wait until GetSku is healthy, then resume ste-details collection."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings()

ROOT = Path(__file__).resolve().parents[1]
PROXY_FILE = ROOT / "secrets" / "zakupki-proxy"
DIRECT_IP = "94.79.51.128"
HOST = "zakupki.mos.ru"
PROBE_ID = 1185798
POLL_SECONDS = 60
MAX_WAIT = 6 * 60 * 60


def socks_url() -> str:
    for line in PROXY_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        endpoint, credentials = line.rsplit("@", 1)
        host, port = endpoint.split(":", 1)
        user, password = credentials.split(":", 1)
        return f"socks5h://{user}:{password}@{host}:{port}"
    raise RuntimeError("no proxy")


def healthy() -> tuple[bool, str]:
    s = requests.Session()
    s.verify = False
    s.proxies = {"http": socks_url(), "https": socks_url()}
    try:
        r = s.get(
            f"https://{DIRECT_IP}/classifier/api/Sku/GetSku?id={PROBE_ID}&withRegionPriceStatistics=false",
            headers={
                "Host": HOST,
                "Accept": "application/json",
                "Origin": f"https://{HOST}",
                "Referer": f"https://{HOST}/sku/view/{PROBE_ID}",
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            },
            timeout=45,
        )
    except Exception as e:
        return False, f"exc {type(e).__name__}: {e}"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    try:
        data = r.json()
    except Exception:
        return False, f"non-json {r.headers.get('Content-Type')} {r.text[:80]!r}"
    if not isinstance(data.get("id"), int):
        return False, f"bad payload keys={list(data)[:8]}"
    return True, f"ok id={data['id']}"


def main():
    t0 = time.time()
    while True:
        ok, msg = healthy()
        print(f"probe {time.strftime('%H:%M:%S')} {msg}", flush=True)
        if ok:
            break
        if time.time() - t0 > MAX_WAIT:
            print("gave up waiting for API", flush=True)
            sys.exit(1)
        time.sleep(POLL_SECONDS)

    print("API healthy — resuming details", flush=True)
    cmd = [
        sys.executable,
        "-u",
        str(ROOT / "scripts" / "ste_ui_fetch.py"),
        "resume-sample",
        "--source",
        str(ROOT / "data" / "ste.jsonl.gz"),
        "--db",
        str(ROOT / "data" / "ste-details.sqlite"),
        "--out",
        str(ROOT / "data" / "ste-details.jsonl.gz"),
        "--workers",
        "4",
        "--pause",
        "0.15",
    ]
    # lower concurrency after outage
    raise SystemExit(subprocess.call(cmd))


if __name__ == "__main__":
    main()
