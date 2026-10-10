from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow.parquet as pq


UNRESOLVED_CATEGORY_ID = -1
ROOT_IDS = frozenset({1, 2, 3})
_PATH = re.compile(r"\.?\d+(?:\.\d+)*\.?")


@dataclass(slots=True)
class CategoryEvidence:
    """Aggregate source observations without retaining every product row."""

    names: dict[int, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    paths: dict[tuple[int, str, str], dict] = field(default_factory=dict)

    def observe(self, row: dict, source: str) -> None:
        cid = int(row.get("category_id") or 0)
        name = (row.get("category") or "").strip()
        if name:
            self.names[cid][name] += 1
        path = (row.get("category_path") or "").strip()
        key = (cid, path, source)
        observation = self.paths.get(key)
        if observation is None:
            self.paths[key] = {
                "categoryId": cid,
                "path": path,
                "sourceFile": source,
                "productId": int(row["id"]),
                "observations": 1,
            }
        else:
            observation["observations"] += 1
            observation["productId"] = min(observation["productId"], int(row["id"]))


@dataclass(slots=True)
class CategoryTaxonomy:
    tree: list[dict]
    descendants: dict[int, frozenset[int]]
    summary: dict
    issues: list[dict]
    recovered: list[dict]

    def diagnostics(self) -> dict:
        return {"summary": self.summary, "issues": self.issues, "recoveredNodes": self.recovered}


def load_category_reference(path: Path) -> list[dict]:
    return pq.read_table(path, columns=["id", "name", "parentId"]).to_pylist()


def build_category_taxonomy(
    reference: list[dict],
    direct_counts: dict[int, int],
    evidence: CategoryEvidence,
    *,
    reference_source: str = "ste-categories.parquet",
) -> CategoryTaxonomy:
    """Combine source parent links and unanimous, namespace-checked SKU paths.

    Category IDs remain in the portal namespace. Unknown intermediate nodes keep
    their original ID, not a guessed KPGZ name. The unresolved folder is only a
    presentation node; it is never attached to the goods/work/service hierarchy.
    """
    nodes: dict[int, dict] = {}
    issues: list[dict] = []
    for row in reference:
        cid = int(row["id"])
        if cid in nodes:
            raise ValueError(f"Повторный ID категории СТЕ: {cid}")
        nodes[cid] = {
            "id": cid,
            "name": (row.get("name") or "").strip(),
            "parentId": int(row["parentId"]) if row["parentId"] is not None else None,
            "nameSource": reference_source,
            "parentSource": reference_source,
            "reconstructed": False,
            "evidence": [],
        }
    reference_ids = set(nodes)
    source_missing = {cid for cid in direct_counts if cid not in reference_ids}
    missing_parents = {
        node["parentId"] for node in nodes.values()
        if node["parentId"] is not None and node["parentId"] not in reference_ids
    }

    def ensure_node(cid: int) -> None:
        if cid in nodes:
            return
        names = evidence.names.get(cid, Counter())
        name = sorted(names, key=lambda value: (-names[value], value))[0] if names else f"Категория СТЕ #{cid}"
        nodes[cid] = {
            "id": cid,
            "name": name,
            "parentId": None,
            "nameSource": "product.category" if names else "unresolved",
            "parentSource": "unresolved",
            "reconstructed": True,
            "evidence": [],
        }
        if len(names) > 1:
            issues.append({"kind": "name_conflict", "categoryId": cid, "names": sorted(names)})

    for cid in sorted(set(direct_counts) | missing_parents):
        ensure_node(cid)

    # Validate an entire path before letting it provide any parent assertions.
    candidates: dict[int, dict[int, list[dict]]] = defaultdict(lambda: defaultdict(list))
    valid_observations = 0
    invalid_observations = 0
    for key in sorted(evidence.paths):
        observation = evidence.paths[key]
        path = observation["path"]
        cid = observation["categoryId"]
        ids = tuple(int(value) for value in path.strip(".").split(".")) if _PATH.fullmatch(path) else ()
        kind = None
        if not ids:
            kind = "missing_path" if not path else "malformed_path"
        elif any(value <= 0 for value in ids):
            kind = "invalid_path_id"
        elif ids[-1] != cid:
            kind = "path_endpoint_mismatch"
        elif len(set(ids)) != len(ids):
            kind = "path_cycle"
        elif ids[0] not in ROOT_IDS:
            kind = "path_namespace_mismatch"
        else:
            for parent, child in zip(ids, ids[1:]):
                if child in ROOT_IDS:
                    kind = "path_root_conflict"
                    break
                if child in reference_ids and nodes[child]["parentId"] != parent:
                    kind = "path_reference_conflict"
                    break
        if kind is not None:
            invalid_observations += observation["observations"]
            issues.append({"kind": kind, **observation})
            continue
        valid_observations += observation["observations"]
        for value in ids:
            ensure_node(value)
        for parent, child in zip(ids, ids[1:]):
            candidates[child][parent].append({
                "sourceFile": observation["sourceFile"],
                "sourceField": "category_path",
                "productId": observation["productId"],
                "path": path,
                "observations": observation["observations"],
            })

    for cid in sorted(candidates):
        node = nodes[cid]
        options = candidates[cid]
        if len(options) != 1:
            issues.append({"kind": "parent_conflict", "categoryId": cid, "parentIds": sorted(options)})
            continue
        parent, assertions = next(iter(options.items()))
        if cid in reference_ids:
            # A source category already supplies this edge. Paths corroborate it;
            # its missing parent may separately be recovered from these paths.
            continue
        node["parentId"] = parent
        node["parentSource"] = "product.category_path"
        node["evidence"] = assertions

    # Check the combined graph, not just each individual input path.
    completed: set[int] = set()
    for start in sorted(nodes):
        chain: list[int] = []
        positions: dict[int, int] = {}
        cursor: int | None = start
        while cursor is not None and cursor in nodes and cursor not in completed:
            if cursor in positions:
                cycle = chain[positions[cursor]:]
                recovered_edges = [cid for cid in cycle if nodes[cid]["parentSource"] == "product.category_path"]
                if not recovered_edges:
                    raise ValueError(f"Цикл в справочнике категорий СТЕ: {cycle}")
                issues.append({"kind": "combined_cycle", "categoryIds": cycle})
                for cid in recovered_edges:
                    nodes[cid]["parentId"] = None
                    nodes[cid]["parentSource"] = "unresolved"
                break
            positions[cursor] = len(chain)
            chain.append(cursor)
            cursor = nodes[cursor]["parentId"]
        completed.update(chain)

    unresolved_roots = [
        cid for cid, node in nodes.items()
        if cid not in ROOT_IDS and node["parentId"] is None
    ]
    for cid in unresolved_roots:
        issues.append({"kind": "unresolved_parent", "categoryId": cid})

    children: dict[int, list[int]] = defaultdict(list)
    for cid, node in nodes.items():
        parent = node["parentId"]
        if parent is not None:
            children[parent].append(cid)
    descendants: dict[int, frozenset[int]] = {}

    def collect(cid: int) -> frozenset[int]:
        if cid in descendants:
            return descendants[cid]
        result = {cid} if direct_counts.get(cid, 0) > 0 else set()
        for child in children.get(cid, []):
            result.update(collect(child))
        descendants[cid] = frozenset(result)
        return descendants[cid]

    for cid in nodes:
        collect(cid)

    tree: list[dict] = []
    active = {cid for cid, ids in descendants.items() if ids}
    for cid in sorted(active):
        node = nodes[cid]
        parent = node["parentId"]
        tree.append({
            "id": cid,
            "name": node["name"] or f"Категория СТЕ #{cid}",
            "parentId": UNRESOLVED_CATEGORY_ID if cid in unresolved_roots else parent,
            "count": sum(direct_counts[leaf] for leaf in descendants[cid]),
            "hasChildren": any(child in active for child in children.get(cid, [])),
            "reconstructed": node["reconstructed"],
            "nameResolved": node["nameSource"] != "unresolved",
        })
    unresolved_ids: set[int] = set()
    for cid in unresolved_roots:
        unresolved_ids.update(descendants[cid])
    if unresolved_ids:
        descendants[UNRESOLVED_CATEGORY_ID] = frozenset(unresolved_ids)
        tree.append({
            "id": UNRESOLVED_CATEGORY_ID,
            "name": "Без подтверждённой иерархии",
            "parentId": None,
            "count": sum(direct_counts[cid] for cid in unresolved_ids),
            "hasChildren": True,
            "reconstructed": False,
            "nameResolved": True,
        })
    tree.sort(key=lambda node: (-node["count"], node["name"], node["id"]))
    recovered = [nodes[cid] for cid in sorted(active) if nodes[cid]["reconstructed"]]
    summary = {
        "sourceMissingCategories": len(source_missing),
        "recoveredNodes": len(recovered),
        "recoveredParentLinks": sum(node["parentSource"] == "product.category_path" for node in recovered),
        "validPathObservations": valid_observations,
        "rejectedPathObservations": invalid_observations,
        "unresolvedCategories": len(unresolved_ids),
        "unresolvedProducts": sum(direct_counts[cid] for cid in unresolved_ids),
        "issuesByKind": dict(sorted(Counter(issue["kind"] for issue in issues).items())),
    }
    return CategoryTaxonomy(tree, descendants, summary, issues, recovered)
