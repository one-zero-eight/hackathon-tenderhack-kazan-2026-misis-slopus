#!/usr/bin/env python3
"""Download full STE cards via one-by-one UI GetSku for IDs from *_from_api.jsonl."""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ste_ui_fetch as ui

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
LOG_DIR = DATA / "logs"
SOURCES = ("Инструменты", "Мебель", "Одежда")


def setup_logging(stem: str | None = None) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("download_ste_getsku_from_api")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logger.addHandler(stream)
    log_path = LOG_DIR / ("getsku-all.log" if stem is None else f"{stem}_getsku.log")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    if stem is not None:
        combined = logging.FileHandler(LOG_DIR / "getsku-all.log", encoding="utf-8")
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


def run_stem(
    stem: str,
    *,
    bridge: str | None,
    direct: bool,
    pause: float,
    workers: int,
    limit: int | None,
) -> str:
    logger = setup_logging(stem)
    source = DATA / f"{stem}_from_api.jsonl"
    try:
        ids = load_ids(source)
        if limit is not None:
            ids = ids[:limit]
        logger.info(
            "%s: %s IDs from %s workers=%s pause=%s direct=%s bridge=%s",
            stem,
            len(ids),
            source.name,
            workers,
            pause,
            direct,
            bool(bridge),
        )
        ui.resume_download(
            ids,
            DATA / "tmp" / f"{stem}-getsku.sqlite",
            DATA / f"{stem}_getsku.jsonl.gz",
            bridge=bridge,
            direct=direct,
            pause=pause,
            workers=workers,
            list_source=str(source),
        )
        logger.info("%s: finished ok", stem)
        return f"{stem}:ok"
    except ui.BotProtected as e:
        logger.error("AGENT_ALERT kind=bot_protection stem=%s detail=%s", stem, e)
        raise
    except Exception as e:
        logger.exception("AGENT_ALERT kind=stem_crash stem=%s detail=%s", stem, e)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=SOURCES, action="append")
    parser.add_argument("--bridge", default=None)
    parser.add_argument(
        "--direct",
        action="store_true",
        default=True,
        help="Hostname HTTPS without SOCKS (default on; SOCKS often broken)",
    )
    parser.add_argument("--socks", action="store_true", help="Use secrets/zakupki-proxy SOCKS5")
    parser.add_argument("--pause", type=float, default=0.0)
    parser.add_argument(
        "--workers",
        type=int,
        default=32,
        help="Threads per stem; ~30+/s after VPN bypass for 94.79.51.0/24 (aim <6h)",
    )
    parser.add_argument(
        "--ensure-route",
        action="store_true",
        help="sudo ip route: send 94.79.51.0/24 via Wi‑Fi, bypass tun0",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--parallel", action="store_true", help="One process per stem")
    args = parser.parse_args()
    direct = False if args.socks else args.direct
    stems = tuple(args.only) if args.only else SOURCES
    setup_logging()
    log = logging.getLogger("download_ste_getsku_from_api")
    if args.ensure_route:
        # Prefer the non-tun default route (Wi‑Fi/Ethernet) so GetSku skips VPN.
        route = subprocess.check_output(["ip", "-json", "route", "show", "default"], text=True)
        rows = [r for r in json.loads(route) if r.get("dev") and not str(r["dev"]).startswith("tun")]
        if not rows or "gateway" not in rows[0]:
            raise RuntimeError("no non-tun default route for --ensure-route")
        gw, dev = rows[0]["gateway"], rows[0]["dev"]
        cmd = ["sudo", "ip", "route", "replace", "94.79.51.0/24", "via", gw, "dev", dev]
        log.info("ensure-route: %s", " ".join(cmd))
        subprocess.check_call(cmd)
    log.info(
        "start stems=%s parallel=%s workers=%s pause=%s direct=%s",
        stems,
        args.parallel,
        args.workers,
        args.pause,
        direct,
    )

    if args.parallel and len(stems) > 1:
        with ProcessPoolExecutor(max_workers=len(stems)) as pool:
            futs = {
                pool.submit(
                    run_stem,
                    stem,
                    bridge=args.bridge,
                    direct=direct,
                    pause=args.pause,
                    workers=args.workers,
                    limit=args.limit,
                ): stem
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
            run_stem(
                stem,
                bridge=args.bridge,
                direct=direct,
                pause=args.pause,
                workers=args.workers,
                limit=args.limit,
            )

    log.info("all stems complete")


if __name__ == "__main__":
    main()
