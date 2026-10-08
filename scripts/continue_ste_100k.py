import asyncio
import fcntl
import gzip
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "ste.jsonl.gz"
META = ROOT / "data" / "ste.jsonl.gz.meta.json"
DOWNLOADER = str(ROOT / "scripts" / "download_ste_sample.py")
LOCK_PATH = "/tmp/slider-ste-download.lock"
CHAIN_LOCK = open("/tmp/slider-ste-continuation.lock", "a")
fcntl.flock(CHAIN_LOCK.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
EXPECTED_SOURCE = "https://api.zakupki.mos.ru/api/v2/Sku/public/Search"
EXPECTED_ORDER = [{"field": "id", "desc": False}]


def active():
    with open(LOCK_PATH, "a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return False


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


def load_items():
    items = list(iter_records(OUTPUT))
    metadata = json.loads(META.read_text(encoding="utf-8"))
    return items, metadata


def check(target):
    items, metadata = load_items()
    if metadata["source"] != EXPECTED_SOURCE or metadata["order"] != EXPECTED_ORDER or metadata.get("filter") is not None:
        raise RuntimeError("Unexpected data source, order or filter")
    if metadata.get("format") != "jsonl" or metadata.get("compression") != "gzip":
        raise RuntimeError("Unexpected data format")
    if metadata["stopReason"] != "sample_limit" or metadata.get("targetCount") != target:
        raise RuntimeError(f'Stage did not finish normally: {metadata.get("stopReason")}')
    ids = [item["id"] for item in items]
    if len(items) != target or metadata["recordCount"] != target or len(set(ids)) != target:
        raise RuntimeError("Incomplete dataset or duplicate IDs")
    if any(a >= b for a, b in zip(ids, ids[1:])):
        raise RuntimeError("IDs are not strictly ordered")
    if not all(isinstance(item, dict) and "name" in item for item in items):
        raise RuntimeError("Invalid record structure")
    token = (ROOT / "secrets" / "zakupki-token").read_text().strip()
    if token.lower().startswith("bearer "):
        token = token[7:]
    if token:
        for item in items:
            if token in json.dumps(item, ensure_ascii=False):
                raise RuntimeError("Credential detected in dataset")
    print(f"VALIDATED: {target:,} records; unique ordered IDs; JSONL gzip; bytes={OUTPUT.stat().st_size}", flush=True)
    return items, metadata


async def main():
    deadline = time.monotonic() + 6 * 60 * 60 + 120
    print("Waiting for the running stage; no API calls while waiting", flush=True)
    while active():
        if time.monotonic() >= deadline:
            raise RuntimeError("First stage did not finish within its allowed duration")
        await asyncio.sleep(15)
    items, metadata = check(50000)
    metadata["verifiedMilestones"] = [{"recordCount": 50000, "checkpointAt": metadata["finishedAt"]}]
    META.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    del items
    print("Starting continuation to 100,000: 100 records/request; 10-second pauses; no retries", flush=True)
    completed = await asyncio.to_thread(subprocess.run, [sys.executable, DOWNLOADER, "--target", "100000"], cwd=ROOT)
    if completed.returncode:
        raise RuntimeError(f"Continuation exited with code {completed.returncode}")
    check(100000)
    print("DONE: ste.jsonl.gz contains 100,000 verified STE records", flush=True)


asyncio.run(main())
