#!/usr/bin/env python3
"""Run GetSku 300k download on Wi‑Fi and restart on stalls/crashes."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
LOG = DATA / "logs" / "getsku-watchdog.log"
STEMS = ("Инструменты", "Мебель", "Одежда")
STALL_SECONDS = 420
CHECK_EVERY = 30
WORKERS = 32
SCRIPT = ROOT / "scripts" / "download_ste_getsku_from_api.py"
PYTHON = "/usr/bin/python3"


def log(msg: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def ensure_route() -> None:
    route = subprocess.check_output(["ip", "-json", "route", "show", "default"], text=True)
    rows = [r for r in json.loads(route) if r.get("dev") and not str(r["dev"]).startswith("tun")]
    if not rows or "gateway" not in rows[0]:
        raise RuntimeError("no non-tun default route")
    gw, dev = rows[0]["gateway"], rows[0]["dev"]
    cmd = ["sudo", "ip", "route", "replace", "94.79.51.0/24", "via", gw, "dev", dev]
    subprocess.check_call(cmd)
    via = subprocess.check_output(["ip", "route", "get", "94.79.51.126"], text=True).splitlines()[0]
    log(f"route ok: {via}")


def counts() -> dict[str, int]:
    out = {}
    for stem in STEMS:
        db = DATA / "tmp" / f"{stem}-getsku.sqlite"
        if not db.exists():
            out[stem] = 0
            continue
        con = sqlite3.connect(db)
        try:
            out[stem] = con.execute("select count(*) from sku_details").fetchone()[0]
        except sqlite3.Error:
            out[stem] = 0
        finally:
            con.close()
    return out


def targets() -> dict[str, int]:
    out = {}
    for stem in STEMS:
        src = DATA / f"{stem}_from_api.jsonl"
        out[stem] = sum(1 for line in src.open() if line.strip())
    return out


def main() -> int:
    ensure_route()
    tgt = targets()
    total_target = sum(tgt.values())
    log(f"start watchdog workers={WORKERS} target={total_target} stems={tgt}")
    t0 = time.time()
    last = counts()
    last_progress_at = time.time()
    proc: subprocess.Popen | None = None
    restarts = 0

    child_log = DATA / "logs" / "getsku-child.out"

    def start() -> subprocess.Popen:
        nonlocal restarts
        ensure_route()
        restarts += 1
        log(f"AGENT_ALERT kind=spawn restart={restarts} log={child_log}")
        child_log.parent.mkdir(parents=True, exist_ok=True)
        out = child_log.open("a", encoding="utf-8")
        return subprocess.Popen(
            [
                PYTHON,
                "-u",
                str(SCRIPT),
                "--ensure-route",
                "--workers",
                str(WORKERS),
                "--pause",
                "0",
            ],
            cwd=str(ROOT),
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    proc = start()
    while True:
        time.sleep(CHECK_EVERY)
        cur = counts()
        done = sum(cur.values())
        delta = done - sum(last.values())
        elapsed = max(time.time() - t0, 1e-6)
        rate = done / elapsed
        eta_h = (total_target - done) / rate / 3600 if rate > 0 and done < total_target else 0
        log(
            f"progress total={done}/{total_target} delta={delta}/{CHECK_EVERY}s "
            f"rate={rate:.2f}/s eta_h={eta_h:.1f} stems={cur} child_alive={proc.poll() is None}"
        )

        if done >= total_target:
            log("COMPLETE all stems saved")
            if proc.poll() is None:
                try:
                    proc.wait(timeout=120)
                except subprocess.TimeoutExpired:
                    pass
            return 0

        if delta > 0:
            last_progress_at = time.time()
            last = cur

        if proc.poll() is not None:
            code = proc.returncode
            log(f"AGENT_ALERT kind=child_exit code={code}")
            cur = counts()
            if sum(cur.values()) >= total_target:
                log("COMPLETE after child exit")
                return 0
            time.sleep(5)
            proc = start()
            continue

        if time.time() - last_progress_at > STALL_SECONDS:
            log(f"AGENT_ALERT kind=stall seconds={int(time.time()-last_progress_at)} killing child")
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
            time.sleep(3)
            last_progress_at = time.time()
            proc = start()


if __name__ == "__main__":
    raise SystemExit(main())
