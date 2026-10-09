#!/usr/bin/env python3
"""Package JSON exports as gzip and include Parquet catalogs in snapshot manifests."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import ijson
import pyarrow.parquet as pq

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


def count_records(path: Path) -> int | None:
    if path.suffix == ".parquet":
        parquet = pq.ParquetFile(path)
        for _ in parquet.iter_batches():
            pass
        return parquet.metadata.num_rows
    with gzip.open(path, "rb") as stream:
        if path.name.endswith(".jsonl.gz"):
            return sum(1 for line in stream if line.strip())
        if path.name.endswith(".json.gz"):
            prefix = stream.read(256).lstrip()
            stream.seek(0)
            if prefix.startswith(b"["):
                return sum(1 for _ in ijson.items(stream, "item"))
    return None


def staged_manifest() -> None:
    """Describe and validate immutable LFS objects in the Git index."""
    names = subprocess.check_output(["git", "ls-files", "-z", "data"], cwd=ROOT).decode().split("\0")
    files = []
    for name in names:
        if not name.startswith("data/") or not name.endswith((".gz", ".parquet")) or name == "data/publish-manifest.json.gz":
            continue
        pointer = subprocess.check_output(["git", "show", f":{name}"], cwd=ROOT).decode()
        fields = dict(line.split(" ", 1) for line in pointer.strip().splitlines())
        if fields.get("version") != "https://git-lfs.github.com/spec/v1":
            raise ValueError(f"Not an LFS pointer: {name}")
        digest = fields["oid"].removeprefix("sha256:")
        blob = ROOT / ".git/lfs/objects" / digest[:2] / digest[2:4] / digest
        actual = hashlib.sha256()
        with blob.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                actual.update(chunk)
        if actual.hexdigest() != digest or blob.stat().st_size != int(fields["size"]):
            raise ValueError(f"Invalid LFS object: {name}")
        item = {"file": str(Path(name).relative_to("data")), "bytes": blob.stat().st_size, "sha256": digest}
        if name.endswith(".parquet"):
            parquet = pq.ParquetFile(blob)
            for _ in parquet.iter_batches():
                pass
            item["records"] = parquet.metadata.num_rows
        else:
            with gzip.open(blob, "rb") as stream:
                if name.endswith(".jsonl.gz"):
                    item["records"] = sum(1 for line in stream if line.strip())
                elif name.endswith(".json.gz"):
                    prefix = stream.read(256).lstrip()
                    stream.seek(0)
                    if prefix.startswith(b"["):
                        item["records"] = sum(1 for _ in ijson.items(stream, "item"))
                    else:
                        json.load(stream)
                else:
                    while stream.read(1024 * 1024):
                        pass
        files.append(item)
        print(json.dumps(item), flush=True)
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "format": "gzip and parquet", "files": files,
                "note": "Counts and hashes verified against immutable staged Git LFS objects; live exports can differ.",
                "excluded": ["SQLite databases", "web app", "secrets", "browser diagnostics"]}
    target = DATA / "publish-manifest.json.gz"
    partial = Path(str(target) + ".part")
    with gzip.open(partial, "wt", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    partial.replace(target)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged-manifest", action="store_true", help="Validate staged LFS gzip/Parquet objects and rebuild their manifest")
    args = parser.parse_args()
    if args.staged_manifest:
        staged_manifest()
        return
    for source in sorted(DATA.glob("*.json")):
        if source.name not in EXCLUDED:
            target = compress(source)
            print(f"PACKED {target.relative_to(ROOT)}", flush=True)
    workspace = DATA / "ste-search-dataset"
    if workspace.exists():
        for source in sorted(workspace.glob("*.jsonl")):
            print(f"PACKED {compress(source).relative_to(ROOT)}", flush=True)
    files = []
    paths = sorted(DATA.glob("*.gz")) + sorted(DATA.glob("*.parquet")) + sorted(workspace.glob("*.jsonl.gz"))
    for path in paths:
        if path.name in {"publish-manifest.json.gz", "ste-search-dataset.tar.gz"}:
            continue
        digest = hashlib.sha256()
        # One file descriptor pins a completed export even if its producer replaces the path.
        with path.open("rb") as stream:
            size = 0
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
        item = {"file": str(path.relative_to(DATA)), "bytes": size, "sha256": digest.hexdigest()}
        count = count_records(path)
        if count is not None:
            item["records"] = count
        files.append(item)
        print(json.dumps(item), flush=True)
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "format": "gzip and parquet", "files": files,
                "excluded": ["SQLite databases", "web app", "secrets", "browser diagnostics", "search workspace archive"]}
    target = DATA / "publish-manifest.json.gz"
    partial = Path(str(target) + ".part")
    with gzip.open(partial, "wt", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    partial.replace(target)


if __name__ == "__main__":
    main()
