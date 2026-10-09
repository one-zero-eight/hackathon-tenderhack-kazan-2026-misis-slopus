#!/usr/bin/env python3
"""Gzip-pack local JSON / JSONL exports under data/."""
from __future__ import annotations

import argparse
import gzip
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
EXCLUDED = {"ste-anonymous-network.json"}


def compress(source: Path) -> Path:
    target = Path(str(source) + ".gz")
    partial = Path(str(target) + ".part")
    with source.open("rb") as incoming, partial.open("wb") as outgoing:
        with gzip.GzipFile(filename="", mode="wb", fileobj=outgoing, mtime=0) as stream:
            shutil.copyfileobj(incoming, stream, length=1024 * 1024)
    partial.replace(target)
    return target


def main() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    directories = [DATA, DATA / "deprecated", DATA / "tmp"]
    for directory in directories:
        for source in sorted(directory.glob("*.json")):
            if source.name not in EXCLUDED and not source.name.endswith(".meta.json"):
                print(f"PACKED {compress(source).relative_to(ROOT)}", flush=True)
    for workspace in (DATA / "ste-search-dataset", DATA / "tmp/ste-search-dataset"):
        for source in sorted(workspace.glob("*.jsonl")):
            print(f"PACKED {compress(source).relative_to(ROOT)}", flush=True)


if __name__ == "__main__":
    main()
