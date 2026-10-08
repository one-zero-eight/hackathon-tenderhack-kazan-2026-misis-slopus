import argparse
import asyncio
import fcntl
import gzip
import json
import os
import signal
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKEN_FILE = ROOT / "secrets" / "zakupki-token"
ENDPOINT = "https://api.zakupki.mos.ru/api/v2/Sku/public/Search"
OUTPUT = ROOT / "data" / "ste.jsonl.gz"
META = ROOT / "data" / "ste.jsonl.gz.meta.json"
BATCH_SIZE = 100
PARSER = argparse.ArgumentParser()
PARSER.add_argument("--target", type=int, default=50000)
TARGET_COUNT = PARSER.parse_args().target
PAUSE_SECONDS = 10
MAX_RUN_SECONDS = 6 * 60 * 60
ORDER = [{"field": "id", "desc": False}]
STOP_REQUESTED = False


def request_stop(signum, frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True


signal.signal(signal.SIGTERM, request_stop)
signal.signal(signal.SIGINT, request_stop)
LOCK = open("/tmp/slider-ste-download.lock", "a")
fcntl.flock(LOCK.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def request_page(token, query):
    url = ENDPOINT + "?" + urllib.parse.quote(json.dumps(query, separators=(",", ":")))
    request = urllib.request.Request(url, headers={"Accept": "application/json", "Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def dumps_line(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def write_meta(metadata: dict, token: str):
    encoded = json.dumps(metadata, ensure_ascii=False, indent=2) + "\n"
    if token in encoded:
        raise RuntimeError("Credential found in metadata; refusing to save")
    partial = META.with_name(META.name + ".part")
    with partial.open("w", encoding="utf-8") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(partial, META)


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


def append_items(batch: list[dict], token: str):
    lines = [dumps_line(item) for item in batch]
    if any(token in line for line in lines):
        raise RuntimeError("Credential found in response; refusing to save")
    with gzip.open(OUTPUT, "at", encoding="utf-8") as stream:
        for line in lines:
            stream.write(line + "\n")


def load_existing():
    metadata = {
        "source": ENDPOINT,
        "order": ORDER,
        "filter": None,
        "format": "jsonl",
        "compression": "gzip",
        "startedAt": timestamp(),
        "isSample": True,
        "detailCardsDownloaded": False,
    }
    items = list(iter_records(OUTPUT)) if OUTPUT.exists() else []
    if META.exists():
        metadata.update(json.loads(META.read_text(encoding="utf-8")))
    metadata.update({"format": "jsonl", "compression": "gzip"})
    return items, metadata


async def main():
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if token.lower().startswith("bearer "):
        token = token[7:]
    if not token or any(c.isspace() for c in token):
        raise RuntimeError("Invalid token file format; value not logged")

    items, metadata = load_existing()
    if metadata.get("source") != ENDPOINT or metadata.get("order") != ORDER or metadata.get("filter") is not None:
        raise RuntimeError("Existing download uses a different query; refusing to continue")
    ids = {record["id"] for record in items}
    if len(ids) != len(items):
        raise RuntimeError("Existing file has duplicate IDs")
    if any(items[i]["id"] >= items[i + 1]["id"] for i in range(len(items) - 1)):
        raise RuntimeError("Existing records are not strictly ordered by ID")
    metadata["recordCount"] = len(items)
    if len(items) >= TARGET_COUNT:
        print("Target already reached:", len(items), flush=True)
        write_meta(metadata, token)
        return

    deadline = time.monotonic() + MAX_RUN_SECONDS
    metadata.update(
        {
            "maxRunSeconds": MAX_RUN_SECONDS,
            "targetCount": TARGET_COUNT,
            "pauseSeconds": PAUSE_SECONDS,
            "batchSize": BATCH_SIZE,
            "resumeStartedAt": timestamp(),
            "isSample": True,
            "finishedAt": None,
            "stopReason": "in_progress",
            "detailCardsDownloaded": False,
            "format": "jsonl",
            "compression": "gzip",
        }
    )
    write_meta(metadata, token)
    requests = 0
    stop_reason = "sample_limit"
    print(
        f"Resuming {OUTPUT.name}: {len(items)} records; target={TARGET_COUNT}; pause={PAUSE_SECONDS}s; no retries",
        flush=True,
    )
    try:
        while len(items) < TARGET_COUNT:
            if STOP_REQUESTED:
                stop_reason = "stop_requested"
                break
            if time.monotonic() + PAUSE_SECONDS + 60 >= deadline:
                stop_reason = "time_limit"
                break
            if requests:
                await asyncio.sleep(PAUSE_SECONDS)
            if STOP_REQUESTED:
                stop_reason = "stop_requested"
                break
            query = {
                "withCount": requests == 0,
                "order": ORDER,
                "skip": len(items),
                "take": min(BATCH_SIZE, TARGET_COUNT - len(items)),
            }
            requests += 1
            metadata["requestCount"] = metadata.get("requestCount", 0) + 1
            try:
                result = await asyncio.to_thread(request_page, token, query)
            except urllib.error.HTTPError as error:
                stop_reason = f"http_{error.code}"
                print(f'STOP: HTTP {error.code}; Retry-After={error.headers.get("Retry-After")}; no retries', flush=True)
                break
            except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError):
                stop_reason = "network_error"
                print("STOP: network error; no retries", flush=True)
                break
            if not isinstance(result, dict) or not isinstance(result.get("items"), list):
                stop_reason = "unexpected_response"
                print("STOP: unexpected response shape", flush=True)
                break
            if requests == 1:
                metadata["apiTotalCount"] = result.get("count")
                print("API total count:", metadata["apiTotalCount"], flush=True)
            batch = result["items"]
            batch_ids = [item.get("id") for item in batch if isinstance(item, dict)]
            if (
                len(batch_ids) != len(batch)
                or None in batch_ids
                or len(set(batch_ids)) != len(batch_ids)
                or ids.intersection(batch_ids)
            ):
                stop_reason = "invalid_or_duplicate_ids"
                print("STOP: invalid or duplicate IDs; preserving last good checkpoint", flush=True)
                break
            if batch_ids and (
                items
                and batch_ids[0] <= items[-1]["id"]
                or any(batch_ids[i] >= batch_ids[i + 1] for i in range(len(batch_ids) - 1))
            ):
                stop_reason = "invalid_id_order"
                print("STOP: unexpected ID order; preserving last good checkpoint", flush=True)
                break
            ids.update(batch_ids)
            append_items(batch, token)
            items.extend(batch)
            metadata.update({"recordCount": len(items), "checkpointAt": timestamp()})
            write_meta(metadata, token)
            print(
                f"Checkpoint {requests}: +{len(batch)} records; {len(items)}/{TARGET_COUNT}; bytes={OUTPUT.stat().st_size}",
                flush=True,
            )
            total = metadata["apiTotalCount"]
            if len(batch) < query["take"] or isinstance(total, int) and len(items) >= total:
                stop_reason = "end_of_results"
                break
    except BaseException:
        stop_reason = "interrupted_or_error"
        raise
    finally:
        metadata.update(
            {
                "recordCount": len(items),
                "finishedAt": timestamp(),
                "stopReason": stop_reason,
                "lastRunRequestCount": requests,
            }
        )
        write_meta(metadata, token)
        n = sum(1 for _ in iter_records(OUTPUT))
        assert n == len(ids) == metadata["recordCount"]
        print("SAVED:", OUTPUT.name, "records:", len(items), "bytes:", OUTPUT.stat().st_size, "stop:", stop_reason, flush=True)


asyncio.run(main())
