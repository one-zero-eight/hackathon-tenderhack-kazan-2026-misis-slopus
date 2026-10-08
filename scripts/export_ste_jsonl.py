#!/usr/bin/env python3
"""Export audited STE records to JSONL independently of catalog storage format."""
import gzip
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "data" / "ste-search-dataset"


def normal(value):
    return " ".join(re.findall(r"[a-zа-яё0-9]+", str(value).lower().replace("ё", "е")))


def evidence_valid(negative):
    mismatch = negative["mismatch"]
    attr, actual = normal(mismatch["attribute"]), normal(mismatch["candidate_value"])
    for prop in negative["characteristics"]:
        name = normal(prop["name"])
        if attr in name or name in attr:
            value = prop["value"]
            if value == mismatch["candidate_value"] or normal(value) == actual:
                return True
            if isinstance(value, bool):
                allowed = {"true", "есть", "да", "с клапаном", "имеется"} if value else {"false", "нет", "без клапана", "отсутствует"}
                if actual in allowed:
                    return True
            elif isinstance(value, (int, float)):
                number = str(mismatch["candidate_value"]).strip().replace(",", ".")
                if re.fullmatch(r"-?\d+(?:\.\d+)?", number) and float(number) == float(value):
                    return True
    for token, field in {"бренд": "manufacturer", "производитель": "manufacturer", "марка": "manufacturer", "модель": "model", "наименование": "name"}.items():
        if token in attr and actual in normal(negative.get(field, "")):
            return True
    return False


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main():
    records = [json.loads(p.read_text()) for p in sorted((ROOT / "records").glob("*.json"))]
    assert len(records) == 1000, len(records)
    assert Counter(r["split"] for r in records) == {"train": 800, "validation": 100, "test": 100}
    assert len({normal(r["query"]) for r in records}) == 1000
    assert len(Counter(r["query_type"] for r in records)) == 10
    assert all(count == 100 for count in Counter(r["query_type"] for r in records).values())
    owners, family_owners = {}, {}
    for r in records:
        assert r.get("hard_negative_audit"), r["query_id"]
        assert r["relevant_ste"]["relevance"] == 2
        assert 3 <= len(r["hard_negatives"]) <= 5
        assert len({p["ste_id"] for p in r["hard_negatives"]}) == len(r["hard_negatives"])
        assert r["relevant_ste"]["ste_id"] not in {p["ste_id"] for p in r["hard_negatives"]}
        family_owners.setdefault(r["leakage_group"], set()).add(r["split"])
        for n in r["hard_negatives"]:
            assert n["relevance"] == 0 and n["hard_negative"]
            assert n["category_id"] == r["relevant_ste"]["category_id"]
            assert evidence_valid(n), (r["query_id"], n["ste_id"])
            assert normal(n["mismatch"]["required_value"]) != normal(n["mismatch"]["candidate_value"])
        negative_ids = {n["ste_id"] for n in r["hard_negatives"]}
        for p in r["candidates"]:
            if p["relevance"] == 2 or p["ste_id"] in negative_ids:
                owners.setdefault(p["ste_id"], set()).add(r["split"])
    assert all(len(s) == 1 for s in family_owners.values())
    assert all(len(s) == 1 for s in owners.values())
    for index, a in enumerate(records):
        at = normal(a["query"])
        aset = set(at.split())
        for b in records[index + 1:]:
            if a["split"] == b["split"]:
                continue
            bt, bset = normal(b["query"]), set(normal(b["query"]).split())
            assert len(aset & bset) / max(1, len(aset | bset)) < 0.8
            assert min(len(at), len(bt)) < 12 or SequenceMatcher(None, at, bt).ratio() < 0.9
    archived = Counter()
    for path in (ROOT / "raw").rglob("*.meta.json"):
        metadata = json.loads(path.read_text())
        body = gzip.decompress((ROOT / metadata["body_file"]).read_bytes())
        assert hashlib.sha256(body).hexdigest() == metadata["body_sha256"], path
        assert len(body) == metadata["body_bytes"], path
        archived[metadata["service"]] += 1
    checksums = {}
    for split in ["train", "validation", "test", "all"]:
        output = ROOT / (split + ".jsonl")
        temp = output.with_suffix(".jsonl.part")
        with temp.open("w", encoding="utf-8") as stream:
            for r in records:
                if split == "all" or r["split"] == split:
                    stream.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
        temp.replace(output)
        checksums[output.name] = hashlib.sha256(output.read_bytes()).hexdigest()
    report = {"status": "complete", "records": 1000, "split_counts": dict(Counter(r["split"] for r in records)), "query_type_counts": dict(Counter(r["query_type"] for r in records)), "semantic_families": len(family_owners), "audited_records": 1000, "negative_evidence_failures": [], "positive_negative_product_overlaps": {}, "cross_split_near_duplicates": [], "archive_checksum_failures": [], "archived_responses": dict(archived), "validated_at": datetime.now(timezone.utc).isoformat()}
    write_json(ROOT / "final-integrity.json", report)
    write_json(ROOT / "validation-report.json", report)
    write_json(ROOT / "jsonl-manifest.json", {**report, "format": "jsonl", "files_sha256": checksums, "rubric": {"0": "explicit critical mismatch or unrelated", "1": "partial or unknown critical parameter", "2": "all explicit requirements match"}, "split_policy": "Indivisible connected semantic/product families; lexical near-duplicate and relevant/hard-negative STE-ID isolation", "raw_restore": "Read each raw/**/*.meta.json. Gzip-decompress body_file to restore original complete response bytes; verify body_sha256. Metadata stores request parameters, timestamp, response status and headers.", "limitations": ["Synthetic, DeepSeek-annotated, not human-verified ground truth", "Search stores the complete first 20 results, not all matching pages", "Positive and hard-negative candidates absent from exact-query results are explicitly marked family_search", "Automated lexical and STE-ID isolation is not proof of exhaustive semantic leakage absence"]})
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
