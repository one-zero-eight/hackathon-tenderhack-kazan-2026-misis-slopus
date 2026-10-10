from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq


UNRESOLVED_CATEGORY_ID = -1

# Catalog verticals: tools / furniture / clothes (matches default parquet dumps).
_CATALOG_ROOT_CODES = ("01.11.02", "01.16", "01.19")
_FRIENDLY_ROOT_NAMES = {
    "01.11.02": "Инструменты",
    "01.16": "Мебель",
    "01.19": "Одежда",
}


@dataclass(slots=True)
class CategoryTaxonomy:
    tree: list[dict]
    descendants: dict[int, frozenset[int]]
    summary: dict
    code_to_id: dict[str, int]

    def diagnostics(self) -> dict:
        return {"summary": self.summary}


def _display_name(code: str, raw: str) -> str:
    if code in _FRIENDLY_ROOT_NAMES:
        return _FRIENDLY_ROOT_NAMES[code]
    name = (raw or "").strip().strip('"')
    if not name:
        return f"КПГЗ {code}"
    if name.isupper():
        return name[:1] + name[1:].lower()
    return name


def _root_for_code(code: str) -> str | None:
    for root in _CATALOG_ROOT_CODES:
        if code == root or code.startswith(root + "."):
            return root
    return None


def _fallback_root_code(codes: list[str]) -> str:
    """L2 prefix for codes outside the known catalog verticals."""
    if not codes:
        return ""
    parts = codes[0].split(".")
    if len(parts) < 2:
        return codes[0]
    return ".".join(parts[:2])


def load_kpgz_reference(path: Path) -> list[dict]:
    rows = pq.read_table(path, columns=["id", "code", "name", "parentId"]).to_pylist()
    return [
        {
            "id": int(row["id"]),
            "code": (row.get("code") or "").strip(),
            "name": (row.get("name") or "").strip(),
            "parentId": int(row["parentId"]) if row.get("parentId") is not None else None,
        }
        for row in rows
        if row.get("id") is not None and (row.get("code") or "").strip()
    ]


def build_kpgz_taxonomy(
    reference: list[dict],
    direct_counts: dict[str, int],
) -> CategoryTaxonomy:
    """Build a product-count tree from КПГЗ codes used in the catalog.

    Roots are the deepest common prefixes of each top-level КПГЗ branch that
    actually has products (for the default dump: инструменты / мебель / одежда).
    """
    by_code = {row["code"]: row for row in reference}
    by_id = {row["id"]: row for row in reference}
    code_to_id = {row["code"]: row["id"] for row in reference}
    issues: Counter[str] = Counter()

    known_counts: dict[str, int] = {}
    unresolved_count = 0
    for code, count in direct_counts.items():
        if not code or code not in by_code:
            unresolved_count += count
            issues["unknown_kpgz_code"] += 1
            continue
        known_counts[code] = known_counts.get(code, 0) + count

    root_codes: set[str] = set()
    fallback_groups: dict[str, list[str]] = defaultdict(list)
    for code in known_counts:
        known = _root_for_code(code)
        if known and known in by_code:
            root_codes.add(known)
            continue
        fallback_groups[_fallback_root_code([code])].append(code)
    for key, codes in fallback_groups.items():
        prefix = _fallback_root_code(codes)
        if prefix in by_code:
            root_codes.add(prefix)
            issues["fallback_catalog_root"] += 1
    root_codes_list = sorted(root_codes)
    root_ids = {code_to_id[code] for code in root_codes_list}

    # Keep each product's node and ancestors down to (and including) its catalog root.
    active_codes: set[str] = set()
    for code in known_counts:
        cursor: str | None = code
        while cursor and cursor in by_code:
            active_codes.add(cursor)
            if cursor in root_codes:
                break
            parent_id = by_code[cursor]["parentId"]
            cursor = by_id[parent_id]["code"] if parent_id is not None and parent_id in by_id else None
        else:
            issues["outside_catalog_roots"] += 1

    children: dict[int, list[int]] = defaultdict(list)
    for code in active_codes:
        if code in root_codes:
            continue
        row = by_code[code]
        parent_id = row["parentId"]
        if parent_id is not None and parent_id in by_id and by_id[parent_id]["code"] in active_codes:
            children[parent_id].append(row["id"])

    descendants: dict[int, frozenset[int]] = {}

    def collect(cid: int) -> frozenset[int]:
        if cid in descendants:
            return descendants[cid]
        row = by_id[cid]
        result = {cid} if known_counts.get(row["code"], 0) > 0 else set()
        for child in children.get(cid, []):
            result.update(collect(child))
        descendants[cid] = frozenset(result)
        return descendants[cid]

    for code in active_codes:
        collect(code_to_id[code])

    tree: list[dict] = []
    active_ids = {cid for cid, leaves in descendants.items() if leaves}
    for code in sorted(active_codes, key=lambda value: (value.count("."), value)):
        row = by_code[code]
        cid = row["id"]
        if cid not in active_ids:
            continue
        parent_id = None if cid in root_ids else row["parentId"]
        tree.append({
            "id": cid,
            "name": _display_name(code, row["name"]),
            "code": code,
            "parentId": parent_id,
            "count": sum(known_counts.get(by_id[leaf]["code"], 0) for leaf in descendants[cid]),
            "hasChildren": any(child in active_ids for child in children.get(cid, [])),
            "reconstructed": False,
            "nameResolved": True,
        })

    if unresolved_count:
        descendants[UNRESOLVED_CATEGORY_ID] = frozenset({UNRESOLVED_CATEGORY_ID})
        tree.append({
            "id": UNRESOLVED_CATEGORY_ID,
            "name": "Без кода КПГЗ",
            "code": "",
            "parentId": None,
            "count": unresolved_count,
            "hasChildren": False,
            "reconstructed": False,
            "nameResolved": True,
        })

    tree.sort(key=lambda node: (-node["count"], node["name"], node["id"]))
    summary = {
        "kpgzNodes": len(active_codes),
        "rootCodes": root_codes_list,
        "unknownProducts": unresolved_count,
        "issuesByKind": dict(sorted(issues.items())),
    }
    return CategoryTaxonomy(tree, descendants, summary, code_to_id)
