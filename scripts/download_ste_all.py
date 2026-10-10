#!/usr/bin/env python3
"""Download full STE catalog -> data/ste-all.jsonl.gz (wrapper around ste_ui_fetch)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ste_ui_fetch as ui

ROOT = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bridge", default=None)
    p.add_argument("--shards", type=int, default=6)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--take", type=int, default=100)
    p.add_argument("--enrich-workers", type=int, default=8)
    p.add_argument("--enrich-batch", type=int, default=50)
    p.add_argument("--skip-enrich", action="store_true")
    p.add_argument("--out", default=str(ROOT / "data" / "ste-all.jsonl.gz"))
    p.add_argument("--shard-dir", default=str(ROOT / "data" / "ste-all-shards"))
    args = p.parse_args()
    ui.resume_download_catalog(
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


if __name__ == "__main__":
    main()
