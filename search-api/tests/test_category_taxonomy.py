import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from search_api import ste_catalog
from search_api.category_taxonomy import (
    UNRESOLVED_CATEGORY_ID,
    build_kpgz_taxonomy,
)


def kpgz_node(cid: int, code: str, parent=None, name=None):
    return {"id": cid, "code": code, "parentId": parent, "name": name or code.upper()}


def assert_tree_integrity(result, counts_by_code: dict[str, int]):
    nodes = {node["id"]: node for node in result.tree}
    code_to_id = result.code_to_id
    counted = {code_to_id[code]: n for code, n in counts_by_code.items() if code in code_to_id}
    if any(code not in code_to_id for code in counts_by_code):
        counted[UNRESOLVED_CATEGORY_ID] = sum(
            n for code, n in counts_by_code.items() if code not in code_to_id
        )
    assert sum(node["count"] for node in nodes.values() if node["parentId"] is None) == sum(counts_by_code.values())
    covered = set()
    for node in nodes.values():
        assert node["count"] == sum(counted.get(cid, 0) for cid in result.descendants[node["id"]])
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
    assert covered == set(counted)


def test_three_catalog_roots_tools_furniture_clothes():
    reference = [
        kpgz_node(1, "01", name="ТОВАРЫ"),
        kpgz_node(11, "01.11", 1, "ТОВАРЫ СТРОИТЕЛЬНЫЕ"),
        kpgz_node(1102, "01.11.02", 11, "ИНСТРУМЕНТ"),
        kpgz_node(110201, "01.11.02.01", 1102, "СВЕРЛА"),
        kpgz_node(16, "01.16", 1, "МЕБЕЛЬ ОБЩЕГО НАЗНАЧЕНИЯ"),
        kpgz_node(1603, "01.16.03", 16, "СТОЛЫ"),
        kpgz_node(19, "01.19", 1, "ОДЕЖДА"),
        kpgz_node(1901, "01.19.01", 19, "ОДЕЖДА ДЛЯ ВЗРОСЛЫХ"),
        kpgz_node(1904, "01.19.04", 19, "ОБУВЬ"),
    ]
    reference.append(kpgz_node(110202, "01.11.02.02", 1102, "ОТВЕРТКИ"))
    counts = {
        "01.11.02.01": 10,
        "01.11.02.02": 2,
        "01.16.03": 7,
        "01.19.01": 5,
        "01.19.04": 3,
    }
    result = build_kpgz_taxonomy(reference, counts)
    roots = [node for node in result.tree if node["parentId"] is None]
    assert sorted(node["code"] for node in roots) == ["01.11.02", "01.16", "01.19"]
    assert {node["name"] for node in roots} == {"Инструменты", "Мебель", "Одежда"}
    assert result.descendants[1102] == frozenset({110201, 110202})
    assert result.descendants[19] == frozenset({1901, 1904})
    assert_tree_integrity(result, counts)


def test_unknown_codes_go_to_unresolved():
    reference = [kpgz_node(1, "01"), kpgz_node(16, "01.16", 1, "МЕБЕЛЬ")]
    result = build_kpgz_taxonomy(reference, {"01.16": 2, "99.99": 4})
    nodes = {node["id"]: node for node in result.tree}
    assert nodes[UNRESOLVED_CATEGORY_ID]["count"] == 4
    assert nodes[16]["count"] == 2
    assert_tree_integrity(result, {"01.16": 2, "99.99": 4})


def test_duplicate_reference_codes_keep_last_by_dict():
    # build uses dict by code; last row wins — ensure no crash on unique ids.
    reference = [kpgz_node(1, "01"), kpgz_node(2, "01.16", 1, "МЕБЕЛЬ")]
    result = build_kpgz_taxonomy(reference, {"01.16": 1})
    assert result.descendants[2] == frozenset({2})


def test_catalog_uses_kpgz_not_ste_categories(tmp_path, monkeypatch):
    kpgz = [
        kpgz_node(1, "01", name="ТОВАРЫ"),
        kpgz_node(11, "01.11", 1, "ТОВАРЫ СТРОИТЕЛЬНЫЕ"),
        kpgz_node(1102, "01.11.02", 11, "ИНСТРУМЕНТ"),
        kpgz_node(110201, "01.11.02.01", 1102, "СВЕРЛА"),
        kpgz_node(110202, "01.11.02.02", 1102, "ОТВЕРТКИ"),
        kpgz_node(16, "01.16", 1, "МЕБЕЛЬ"),
        kpgz_node(1603, "01.16.03", 16, "СТОЛЫ"),
        kpgz_node(1607, "01.16.07", 16, "ШКАФЫ"),
        kpgz_node(19, "01.19", 1, "ОДЕЖДА"),
        kpgz_node(1901, "01.19.01", 19, "ОДЕЖДА ДЛЯ ВЗРОСЛЫХ"),
        kpgz_node(1904, "01.19.04", 19, "ОБУВЬ"),
    ]
    pq.write_table(pa.Table.from_pylist(kpgz), tmp_path / "pp-kpgz.parquet")

    def product_row(pid, code, name):
        row = dict.fromkeys(ste_catalog._COLUMNS)
        row.update(
            id=pid,
            name=name,
            production_code=code,
            production_name=code,
            category_id=999,
            category="STE leaf",
            category_path=".1.999.",
        )
        return row

    tools = tmp_path / "tools.parquet"
    furniture = tmp_path / "furniture.parquet"
    clothes = tmp_path / "clothes.parquet"
    pq.write_table(pa.Table.from_pylist([
        product_row(1, "01.11.02.01", "Сверло"),
        {**product_row(4, "01.11.02.02", "Отвертка"), "sku_image_ids": [42, 42]},
    ]), tools)
    pq.write_table(pa.Table.from_pylist([
        product_row(2, "01.16.03", "Стол"),
        product_row(5, "01.16.07", "Шкаф"),
        product_row(1, "01.16.03", "Дубликат"),
        product_row(0, "01.16.03", "Некорректный"),
    ]), furniture)
    pq.write_table(pa.Table.from_pylist([
        product_row(3, "01.19.04", "Ботинки"),
        product_row(6, "01.19.01", "Куртка"),
    ]), clothes)

    monkeypatch.setattr(ste_catalog, "DATA", tmp_path)
    monkeypatch.setattr(ste_catalog.settings, "catalog_database", tmp_path / "catalog.sqlite")
    monkeypatch.setattr(ste_catalog, "catalog_paths", lambda: [tools, furniture, clothes])
    ste_catalog.reset_catalog()
    try:
        db = ste_catalog.get_catalog()
        assert db.execute("PRAGMA mmap_size").fetchone()[0] > 0
        assert ste_catalog.product_count() == 6
        assert ste_catalog.find_product(1).category_id == 110201
        roots = [node for node in ste_catalog.category_tree() if node["parentId"] is None]
        assert sorted(node["name"] for node in roots) == ["Инструменты", "Мебель", "Одежда"]
        items, total = ste_catalog.list_products(category_id=19, offset=0, limit=24)
        assert total == 2
        assert {item.id for item in items} == {3, 6}
        assert [p.id for p in ste_catalog.hydrate([6, 1, 6, 999])] == [6, 1, 6]
        assert ste_catalog.find_product(1).name == "Сверло"
        assert ste_catalog.has_image_id(42)
        assert not ste_catalog.has_image_id(43)
        assert [p.id for batch in ste_catalog.iterate_products(2) for p in batch] == [1, 2, 3, 4, 5, 6]
        assert ste_catalog.list_products(category_id=123456, offset=0, limit=24) == ([], 0)
        page, total = ste_catalog.list_products(category_id=None, offset=2, limit=2)
        assert total == 6 and [p.id for p in page] == [3, 4]
        from search_api.importer import import_products
        from search_api.index_db import open_index_db
        index = open_index_db(tmp_path / "index.sqlite")
        try:
            assert import_products(index) == 6
            assert import_products(index) == 6
            assert index.execute("SELECT count(*) FROM index_state").fetchone()[0] == 6
            index.execute("INSERT INTO index_state VALUES(999,'stale',NULL,19)")
            index.commit()
            assert import_products(index) == 6
            assert index.execute("SELECT 1 FROM index_state WHERE product_id=999").fetchone() is None
        finally:
            index.close()
        ste_catalog.reset_catalog()
        monkeypatch.setattr(ste_catalog, "_build_catalog", lambda *a: pytest.fail("valid disk snapshot must be reused"))
        assert ste_catalog.product_count() == 6
    finally:
        ste_catalog.reset_catalog()


def test_classifier_builder_streams_full_records(tmp_path, monkeypatch):
    import gzip
    import importlib.util
    import json
    import sys
    from pathlib import Path
    import pysqlite3 as sqlite3

    script = Path(__file__).resolve().parents[2] / "scripts" / "build_classifiers_sqlite.py"
    spec = importlib.util.spec_from_file_location("classifier_builder", script)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    sources = {
        "okpd": [{"id": 1, "code": "25", "name": "Металлы", "parent": None},
                 {"id": 2, "code": "25.73", "name": "Инструменты", "parent": None}],
        "kpgz": [{"id": 1, "code": "01", "name": "Товары", "externalId": 10}],
        "spgz": [{"id": 1, "name": "Молоток", "kpgzId": 10, "descriptions": "Полная карточка",
                  "spgzCharacteristics": [{"characteristicName": "Материал", "value1": "Сталь"}]}],
        "okei": [{"code": None, "name": "Раздел", "razdel": "1", "podrazdel": "0"},
                 {"code": None, "name": "Группа", "razdel": "1", "podrazdel": "1"},
                 {"code": "796", "name": "Штука", "razdel": "1", "podrazdel": "1"}],
    }
    arguments = [str(script), "--output", str(tmp_path / "classifiers.sqlite")]
    for key, rows in sources.items():
        path = tmp_path / (key + ".parquet")
        pq.write_table(pa.Table.from_pylist(rows), path)
        arguments.extend(["--" + key, str(path)])
    monkeypatch.setattr(sys, "argv", arguments)
    monkeypatch.setattr(pq, "read_table", lambda *a, **kw: pytest.fail("builder must stream Parquet batches"))
    builder.main()
    conn = sqlite3.connect(tmp_path / "classifiers.sqlite")
    try:
        assert conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0] == "classifiers-full-v4"
        assert conn.execute("SELECT parent_id FROM okpd WHERE id=2").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM okei").fetchone()[0] == 3
        raw = conn.execute("SELECT payload FROM raw_records WHERE tab='spgz' AND code='1'").fetchone()[0]
        assert json.loads(gzip.decompress(raw)) == sources["spgz"][0]
        assert conn.execute("SELECT count(*) FROM raw_records").fetchone()[0] == 7
    finally:
        conn.close()
