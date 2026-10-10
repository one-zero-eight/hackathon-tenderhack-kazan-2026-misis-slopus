import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from search_api import ste_catalog
from search_api.category_taxonomy import (
    CategoryEvidence,
    UNRESOLVED_CATEGORY_ID,
    build_category_taxonomy,
)


def reference_node(cid, parent=None, name=None):
    return {"id": cid, "parentId": parent, "name": name or f"Категория {cid}"}


def evidence_for(*observations):
    evidence = CategoryEvidence()
    for index, (cid, path, name) in enumerate(observations):
        evidence.observe({"id": index + 1, "category_id": cid, "category_path": path, "category": name}, "sku.parquet")
    return evidence


def assert_tree_integrity(result, counts):
    nodes = {node["id"]: node for node in result.tree}
    assert sum(node["count"] for node in nodes.values() if node["parentId"] is None) == sum(counts.values())
    covered = set()
    for node in nodes.values():
        assert node["count"] == sum(counts[cid] for cid in result.descendants[node["id"]])
        assert node["hasChildren"] == any(child["parentId"] == node["id"] for child in nodes.values())
        visited = set()
        cursor = node
        while cursor["parentId"] is not None:
            assert cursor["id"] not in visited
            visited.add(cursor["id"])
            cursor = nodes[cursor["parentId"]]
        if node["parentId"] is None:
            assert not covered.intersection(result.descendants[node["id"]])
            covered.update(result.descendants[node["id"]])
    assert covered == set(counts)


def test_reconstructs_missing_leaf_and_intermediate_parent():
    reference = [reference_node(1), reference_node(10, 1), reference_node(30, 20)]
    counts = {30: 3, 40: 5, 10: 2}
    evidence = evidence_for((30, ".1.10.20.30.", "Линейки"), (40, ".1.10.40.", "Сверла"))
    result = build_category_taxonomy(reference, counts, evidence)
    nodes = {node["id"]: node for node in result.tree}
    assert nodes[40]["parentId"] == 10
    assert nodes[40]["name"] == "Сверла"
    assert nodes[20]["parentId"] == 10
    assert nodes[20]["name"] == "Категория СТЕ #20"
    assert not nodes[20]["nameResolved"]
    assert result.descendants[10] == frozenset({10, 30, 40})
    assert result.summary["unresolvedProducts"] == 0
    recovered = {node["id"]: node for node in result.recovered}
    assert recovered[20]["parentSource"] == "product.category_path"
    assert recovered[20]["evidence"][0]["path"] == ".1.10.20.30."
    assert_tree_integrity(result, counts)


@pytest.mark.parametrize("path,kind", [
    ("", "missing_path"),
    (".1.nope.40.", "malformed_path"),
    (".1.10.41.", "path_endpoint_mismatch"),
    (".1.10.10.40.", "path_cycle"),
    (".72123354.10.40.", "path_namespace_mismatch"),
    (".1.0.40.", "invalid_path_id"),
    (".1.2.40.", "path_root_conflict"),
])
def test_invalid_paths_do_not_invent_parent_links(path, kind):
    result = build_category_taxonomy([reference_node(1)], {40: 7}, evidence_for((40, path, "Сверла")))
    nodes = {node["id"]: node for node in result.tree}
    assert nodes[40]["parentId"] == UNRESOLVED_CATEGORY_ID
    assert nodes[UNRESOLVED_CATEGORY_ID]["parentId"] is None
    assert {issue["kind"] for issue in result.issues} == {kind, "unresolved_parent"}
    assert result.summary["unresolvedProducts"] == 7
    assert_tree_integrity(result, {40: 7})


def test_reference_conflict_rejects_entire_path():
    reference = [reference_node(1), reference_node(2), reference_node(10, 1)]
    result = build_category_taxonomy(reference, {40: 1}, evidence_for((40, ".2.10.40.", "Сверла")))
    assert 10 not in {node["id"] for node in result.tree}
    assert result.summary["recoveredParentLinks"] == 0
    assert result.summary["issuesByKind"]["path_reference_conflict"] == 1
    assert_tree_integrity(result, {40: 1})


def test_conflicting_paths_never_choose_majority_parent():
    reference = [reference_node(1), reference_node(10, 1), reference_node(11, 1)]
    evidence = evidence_for((40, ".1.10.40.", "Сверла"), (40, ".1.10.40.", "Сверла"), (40, ".1.11.40.", "Сверла"))
    result = build_category_taxonomy(reference, {40: 2}, evidence)
    assert result.summary["recoveredParentLinks"] == 0
    assert result.summary["issuesByKind"]["parent_conflict"] == 1
    assert_tree_integrity(result, {40: 2})


def test_counts_product_categories_on_internal_nodes():
    reference = [reference_node(1), reference_node(10, 1), reference_node(20, 10)]
    counts = {1: 1, 10: 2, 20: 4}
    result = build_category_taxonomy(reference, counts, CategoryEvidence())
    assert result.descendants[1] == frozenset({1, 10, 20})
    assert result.descendants[10] == frozenset({10, 20})
    assert_tree_integrity(result, counts)


def test_reference_cycle_is_an_error():
    with pytest.raises(ValueError, match="Цикл"):
        build_category_taxonomy([reference_node(10, 20), reference_node(20, 10)], {10: 1}, CategoryEvidence())


def test_duplicate_source_ids_are_an_error():
    with pytest.raises(ValueError, match="Повторный ID"):
        build_category_taxonomy([reference_node(1), reference_node(1)], {}, CategoryEvidence())


def test_all_real_roots_stay_separate():
    reference = [reference_node(1), reference_node(2), reference_node(3)]
    counts = {10: 1, 20: 2, 30: 3}
    evidence = evidence_for((10, ".1.10.", "Товар"), (20, ".2.20.", "Работа"), (30, ".3.30.", "Услуга"))
    result = build_category_taxonomy(reference, counts, evidence)
    assert result.descendants[1] == frozenset({10})
    assert result.descendants[2] == frozenset({20})
    assert result.descendants[3] == frozenset({30})
    assert_tree_integrity(result, counts)


def test_catalog_deduplicates_products_not_path_evidence(tmp_path, monkeypatch):
    pq.write_table(pa.Table.from_pylist([reference_node(1), reference_node(10, 1), reference_node(11, 1)]), tmp_path / "ste-categories.parquet")

    def product_row(pid, category, path):
        row = dict.fromkeys(ste_catalog._COLUMNS)
        row.update(id=pid, name="Товар", category_id=40, category=category, category_path=path)
        return row

    first = tmp_path / "first.parquet"
    second = tmp_path / "second.parquet"
    pq.write_table(pa.Table.from_pylist([product_row(100, "Сверла", ".1.10.40."), product_row(101, "Сверла новые", ".1.10.40.")]), first)
    pq.write_table(pa.Table.from_pylist([product_row(100, "Сверла", ".1.11.40.")]), second)
    monkeypatch.setattr(ste_catalog, "DATA", tmp_path)
    monkeypatch.setattr(ste_catalog, "catalog_paths", lambda: [first, second])
    ste_catalog.reset_catalog()
    try:
        products, _, stats = ste_catalog.get_catalog()
        assert len(products) == 2
        assert stats == [{"id": 40, "name": "Сверла", "count": 2}]
        assert ste_catalog.category_diagnostics()["summary"]["issuesByKind"]["parent_conflict"] == 1
        items, total = ste_catalog.list_products(category_id=-1, offset=0, limit=24)
        assert total == 2
        assert {item.id for item in items} == {100, 101}
    finally:
        ste_catalog.reset_catalog()
