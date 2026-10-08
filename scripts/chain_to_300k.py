#!/usr/bin/env python3
"""After ~100k details finish: grow list to 300k, then fetch remaining cards."""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ste-details.sqlite"
UV_BASE = ["uv", "run", "--with", "requests", "--with", "PySocks", "python", "-u"]
WORKERS = "16"
PAUSE = "0.02"
TARGET_DETAILS_100 = 100000
TARGET_LIST = 300000
LOG = Path("/tmp/ste-chain-300k.log")


def details_count() -> int:
    if not DB.exists():
        return 0
    return sqlite3.connect(DB).execute("select count(*) from sku_details").fetchone()[0]


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except Exception:
        return ""


def fetch_pids() -> list[int]:
    # Real interpreter only — uv's cmdline also contains "python -u ... ste_ui_fetch.py".
    r = subprocess.run(["pgrep", "-f", "ste_ui_fetch.py resume-sample"], capture_output=True, text=True)
    out: list[int] = []
    for line in (r.stdout or "").splitlines():
        if not line.strip().isdigit():
            continue
        pid = int(line)
        cmd = _cmdline(pid)
        if not cmd or cmd.startswith("uv ") or "uv run" in cmd[:80]:
            continue
        first = cmd.split(" ", 1)[0]
        if "python" not in Path(first).name:
            continue
        if "ste_ui_fetch.py" in cmd and "resume-sample" in cmd:
            out.append(pid)
    return out


def fetch_running() -> bool:
    return bool(fetch_pids())


def run(cmd: list[str]) -> int:
    print(f"RUN {' '.join(cmd)}", flush=True)
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(cmd)}\n")
        stream.flush()
        proc = subprocess.Popen(cmd, stdout=stream, stderr=subprocess.STDOUT)
        return proc.wait()


def resume_cmd() -> list[str]:
    return UV_BASE + [
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
    ]


def main():
    min_100 = TARGET_DETAILS_100 - 5
    print(
        f"waiting for ~100k details (now={details_count()}, min={min_100}, fetch_running={fetch_running()})",
        flush=True,
    )
    empty_streak = 0
    while True:
        n = details_count()
        running = fetch_running()
        print(f"watch {time.strftime('%H:%M:%S')} saved={n} running={running} pids={fetch_pids()}", flush=True)
        if n >= min_100 and not running:
            break
        if not running and n < min_100:
            print(f"fetcher stopped early at {n}; restarting toward 100k", flush=True)
            before = n
            code = run(resume_cmd())
            after = details_count()
            if after <= before and after >= min_100 - 5:
                empty_streak += 1
                print(f"no progress streak={empty_streak} at {after}", flush=True)
                if empty_streak >= 2:
                    break
            else:
                empty_streak = 0
            if code != 0:
                print(f"resume exit {code}; sleep 120", flush=True)
                time.sleep(120)
                continue
        time.sleep(60)

    print(f"100k details done ({details_count()}); extending list to {TARGET_LIST}", flush=True)
    code = run(
        UV_BASE
        + [
            str(ROOT / "scripts" / "extend_ste_list_qi.py"),
            "--target",
            str(TARGET_LIST),
            "--batch",
            "100",
            "--pause",
            "0.05",
        ]
    )
    if code != 0:
        print(f"list extend failed exit={code}", flush=True)
        sys.exit(code)

    print(f"list ready; resuming details toward {TARGET_LIST}", flush=True)
    code = run(resume_cmd())
    print(f"DONE details={details_count()} exit={code}", flush=True)
    sys.exit(code)


if __name__ == "__main__":
    main()
