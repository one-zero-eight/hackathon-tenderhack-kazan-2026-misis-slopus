#!/usr/bin/env python3
"""After 50k details finish: grow list to 100k, then fetch remaining cards."""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "tmp" / "ste-details.sqlite"
LIST_META = ROOT / "data" / "ste.jsonl.gz.meta.json"
PY = sys.executable
UV_BASE = ["uv", "run", "--with", "requests", "--with", "PySocks", "python", "-u"]
WORKERS = "16"
PAUSE = "0.02"


def details_count() -> int:
    if not DB.exists():
        return 0
    return sqlite3.connect(DB).execute("select count(*) from sku_details").fetchone()[0]


def fetch_running() -> bool:
    r = subprocess.run(["pgrep", "-f", "ste_ui_fetch.py resume-sample"], capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip())


def run(cmd: list[str], log: Path) -> int:
    print(f"RUN {' '.join(cmd)}", flush=True)
    with log.open("a", encoding="utf-8") as stream:
        stream.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(cmd)}\n")
        stream.flush()
        proc = subprocess.Popen(cmd, stdout=stream, stderr=subprocess.STDOUT)
        return proc.wait()


def main():
    target_details_50 = 50000
    target_list = 100000
    log = Path("/tmp/ste-chain-100k.log")

    # Allow a tiny skip budget for permanent 403 (no-access) SKUs.
    min_details_50 = target_details_50 - 5
    print(
        f"waiting for ~50k details (now={details_count()}, min={min_details_50}, fetch_running={fetch_running()})",
        flush=True,
    )
    empty_pending_streak = 0
    while True:
        n = details_count()
        running = fetch_running()
        print(f"watch {time.strftime('%H:%M:%S')} saved={n} running={running}", flush=True)
        if n >= min_details_50 and not running:
            break
        if not running and n < min_details_50:
            print(f"fetcher stopped early at {n}; restarting 50k resume", flush=True)
            code = run(
                UV_BASE
                + [
                    str(ROOT / "scripts" / "ste_ui_fetch.py"),
                    "resume-sample",
                    "--source",
                    str(ROOT / "data" / "ste.jsonl.gz"),
                    "--db",
                    str(DB),
                    "--out",
                    str(ROOT / "data" / "ste-details.jsonl.gz"),
                    "--workers",
                    WORKERS,
                    "--pause",
                    PAUSE,
                ],
                log,
            )
            n2 = details_count()
            if n2 <= n and n >= min_details_50 - 5:
                empty_pending_streak += 1
                print(f"no progress (streak={empty_pending_streak}); treating as done enough at {n2}", flush=True)
                if empty_pending_streak >= 2:
                    break
            else:
                empty_pending_streak = 0
            if code != 0:
                print(f"resume exit {code}; will retry after sleep", flush=True)
                time.sleep(120)
                continue
        time.sleep(60)

    print(f"50k details done ({details_count()}); extending list to {target_list}", flush=True)
    code = run(
        UV_BASE + [str(ROOT / "scripts" / "extend_ste_list_qi.py"), "--target", str(target_list), "--batch", "100", "--pause", "0.1"],
        log,
    )
    if code != 0:
        print(f"list extend failed exit={code}", flush=True)
        sys.exit(code)

    print(f"list ready; resuming details toward {target_list}", flush=True)
    code = run(
        UV_BASE
        + [
            str(ROOT / "scripts" / "ste_ui_fetch.py"),
            "resume-sample",
            "--source",
            str(ROOT / "data" / "ste.jsonl.gz"),
            "--db",
            str(DB),
            "--out",
            str(ROOT / "data" / "ste-details.jsonl.gz"),
            "--workers",
            WORKERS,
            "--pause",
            PAUSE,
        ],
        log,
    )
    print(f"DONE details={details_count()} exit={code}", flush=True)
    sys.exit(code)


if __name__ == "__main__":
    main()
