#!/usr/bin/env python3
"""Download full STE cards via GetDetails for IDs from *_from_api.jsonl."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import download_ste_details as details

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
LOG_DIR = DATA / "logs"
SOURCES = ("Инструменты", "Мебель", "Одежда")


def setup_logging(stem: str | None = None) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("download_ste_details_from_api")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logger.addHandler(stream)
    log_path = LOG_DIR / ("getdetails-all.log" if stem is None else f"{stem}_getdetails.log")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    if stem is not None:
        combined = logging.FileHandler(LOG_DIR / "getdetails-all.log", encoding="utf-8")
        combined.setFormatter(fmt)
        logger.addHandler(combined)
    return logger


def load_ids(source: Path) -> list[int]:
    ids, seen = [], set()
    with open(source, encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            sku_id = int(json.loads(line)["id"])
            if sku_id not in seen:
                seen.add(sku_id)
                ids.append(sku_id)
    return ids


def run_stem(stem: str, pause: float, batch_size: int, limit: int | None, max_run_hours: float) -> str:
    logger = setup_logging(stem)
    source = DATA / f"{stem}_from_api.jsonl"
    try:
        token = details.load_token()
        ids = load_ids(source)
        if limit is not None:
            ids = ids[:limit]
        logger.info(
            "%s: %s IDs from %s pause=%s batch=%s",
            stem,
            len(ids),
            source.name,
            pause,
            batch_size,
        )
        reason = asyncio.run(
            details.download_details(
                ids,
                database=DATA / "tmp" / f"{stem}-getdetails.sqlite",
                export=DATA / f"{stem}_getdetails.jsonl.gz",
                list_source=source,
                token=token,
                cooldown_seconds=0,
                pause_seconds=pause,
                batch_size=batch_size,
                max_run_seconds=max_run_hours * 3600,
            )
        )
        if reason != "complete":
            raise RuntimeError(f"GetDetails incomplete stop={reason}")
        logger.info("%s: finished ok", stem)
        return f"{stem}:ok"
    except Exception as e:
        logger.exception("AGENT_ALERT kind=stem_crash stem=%s detail=%s", stem, e)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=SOURCES, action="append")
    parser.add_argument("--pause", type=float, default=0.2)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--limit", type=int, default=None, help="First N IDs (smoke test)")
    parser.add_argument("--max-run-hours", type=float, default=12.0)
    parser.add_argument("--parallel", action="store_true", help="One process per stem")
    args = parser.parse_args()
    stems = tuple(args.only) if args.only else SOURCES
    setup_logging()
    log = logging.getLogger("download_ste_details_from_api")
    log.info(
        "start stems=%s parallel=%s pause=%s batch=%s",
        stems,
        args.parallel,
        args.pause,
        args.batch_size,
    )

    if args.parallel and len(stems) > 1:
        with ProcessPoolExecutor(max_workers=len(stems)) as pool:
            futs = {
                pool.submit(run_stem, stem, args.pause, args.batch_size, args.limit, args.max_run_hours): stem
                for stem in stems
            }
            for fut in as_completed(futs):
                stem = futs[fut]
                try:
                    log.info("%s", fut.result())
                except Exception as e:
                    log.error("AGENT_ALERT kind=process_fail stem=%s detail=%s", stem, e)
                    raise
    else:
        for stem in stems:
            run_stem(stem, args.pause, args.batch_size, args.limit, args.max_run_hours)

    log.info("all stems complete")


if __name__ == "__main__":
    main()
