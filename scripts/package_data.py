#!/usr/bin/env python3
"""Package local JSON exports and the STE search workspace as gzip snapshots."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import shutil
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import ijson

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
EXCLUDED = {"ste-anonymous-network.json"}
PRIVATE_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}


def compress(source: Path) -> Path:
    target = Path(str(source) + ".gz")
    partial = Path(str(target) + ".part")
    with source.open("rb") as incoming, partial.open("wb") as outgoing:
        with gzip.GzipFile(filename="", mode="wb", fileobj=outgoing, mtime=0) as stream:
            shutil.copyfileobj(incoming, stream, length=1024 * 1024)
    partial.replace(target)
    return target


def count_records(path: Path) -> int | None:
    with gzip.open(path, "rb") as stream:
        if path.name.endswith(".jsonl.gz"):
            return sum(1 for line in stream if line.strip())
        if path.name.endswith(".json.gz"):
            prefix = stream.read(256).lstrip()
            stream.seek(0)
            if prefix.startswith(b"["):
                return sum(1 for _ in ijson.items(stream, "item"))
    return None


def archive_search_workspace() -> Path:
    source = DATA / "ste-search-dataset"
    target = DATA / "ste-search-dataset.tar.gz"
    partial = Path(str(target) + ".part")
    with partial.open("wb") as output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|") as archive:
                for path in sorted(source.rglob("*")):
                    if not path.is_file() or (path.name.endswith((".part", ".sqlite", ".sqlite-wal", ".sqlite-shm", ".gz")) and not path.name.endswith(".body.gz")):
                        continue
                    name = str(path.relative_to(DATA))
                    if path.name.endswith(".meta.json"):
                        metadata = json.loads(path.read_text())
                        for key in ("response_headers", "request_headers", "headers"):
                            if isinstance(metadata.get(key), dict):
                                metadata[key] = {k: v for k, v in metadata[key].items() if k.lower() not in PRIVATE_HEADERS}
                        content = (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode()
                        info = archive.gettarinfo(str(path), arcname=name)
                        info.size = len(content)
                        archive.addfile(info, io.BytesIO(content))
                    else:
                        archive.add(path, arcname=name, recursive=False)
    partial.replace(target)
    return target


def staged_manifest() -> None:
    """Describe and validate immutable LFS objects in the Git index."""
    names = subprocess.check_output(["git", "diff", "--cached", "--name-only", "-z"], cwd=ROOT).decode().split("\0")
    files = []
    for name in names:
        if not name.startswith("data/") or not name.endswith(".gz") or name == "data/publish-manifest.json.gz":
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
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "format": "gzip", "files": files,
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
    parser.add_argument("--staged-manifest", action="store_true", help="Validate staged LFS gzip objects and rebuild their manifest")
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
        print(f"PACKED {archive_search_workspace().relative_to(ROOT)}", flush=True)
    files = []
    paths = sorted(DATA.glob("*.gz")) + sorted(workspace.glob("*.jsonl.gz"))
    for path in paths:
        if path.name == "publish-manifest.json.gz":
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
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "format": "gzip", "files": files,
                "excluded": ["SQLite databases", "web app", "secrets", "browser diagnostics"],
                "archive_note": "HTTP authentication and Set-Cookie headers are excluded from archived metadata; response body bytes are unchanged."}
    target = DATA / "publish-manifest.json.gz"
    partial = Path(str(target) + ".part")
    with gzip.open(partial, "wt", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    partial.replace(target)


if __name__ == "__main__":
    main()
