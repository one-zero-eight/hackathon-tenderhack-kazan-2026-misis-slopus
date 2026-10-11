from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pysqlite3 as sqlite3
import pytest
from fastapi.testclient import TestClient

from search_api import catalog_characteristics as module
from search_api.main import app
from search_api.characteristic_normalization import normalize_key, normalize_unit, normalize_value


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    reference = [
        {"id": 1, "code": "01", "name": "Товары", "parentId": None},
        {"id": 16, "code": "01.16", "name": "Мебель", "parentId": 1},
        {"id": 1601, "code": "01.16.01", "name": "Столы", "parentId": 16},
        {"id": 1602, "code": "01.16.02", "name": "Стулья", "parentId": 16},
        {"id": 1603, "code": "01.16.03", "name": "Нет товаров", "parentId": 16},
        {"id": 19, "code": "01.19", "name": "Одежда", "parentId": 1},
    ]
    path = tmp_path / "pp-kpgz.parquet"
    pq.write_table(pa.Table.from_pylist(reference), path)
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript("""
        CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
        INSERT INTO metadata VALUES('schema','test'),('fingerprint','1');
        CREATE TABLE products(id INTEGER PRIMARY KEY,category_id INTEGER);
    """)
    products = []

    def char(name, value, unit=""):
        return {"name": name, "value": value, "unit": unit}

    for index in range(10):
        fields = [char("Материал", "Сталь"), char("  МАТЕРИАЛ  ", "Сталь"), char("КТРУ", "123")]
        if index < 8:
            fields.append(char("Высота", str(100 + index), "мм"))
        if index == 0:
            fields.append(char("Редкое свойство", "Да"))
        fields.append(char("Масса", "5", "кг" if index < 7 else "г"))
        products.append(SimpleNamespace(category_id=1601, characteristics=fields))
    products.extend(SimpleNamespace(category_id=1602, characteristics=[
        char("Материал", "Дерево"), char("Цвет", "Белый"),
    ]) for _ in range(10))
    products.extend(SimpleNamespace(category_id=19, characteristics=[char("Размер одежды", "XL")]) for _ in range(10))
    conn.executemany("INSERT INTO products VALUES(?,?)", [(i + 1, p.category_id) for i, p in enumerate(products)])
    monkeypatch.setattr(module, "get_catalog", lambda: conn)
    monkeypatch.setattr(module, "iterate_products", lambda: iter([products[:7], products[7:]]))
    monkeypatch.setattr(module, "_kpgz_path", lambda: path)
    monkeypatch.setattr(module, "category_tree", lambda: [{"id": 16, "parentId": None}, {"id": 19, "parentId": None}])
    monkeypatch.setattr(module, "characteristics_for", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(module, "schema_fingerprint", lambda: "test")
    monkeypatch.setattr(module.settings, "catalog_database", tmp_path / "catalog.sqlite")
    yield conn, products
    conn.close()


def test_frequency_counts_products_and_prioritizes_own_category(catalog):
    fields = module.frequent_catalog_characteristics("01.16.01")
    assert [field["name"] for field in fields] == ["Материал", "Высота", "Масса", "Масса", "Цвет"]
    material, height, mass, mass_grams, color = fields
    assert material["count"] == material["total"] == 10
    assert material["frequency"] == 1
    assert material["examples"] == ["Сталь"]
    assert material["exampleCounts"] == {"Сталь": 10}
    assert material["scope"] == "category"
    assert height["numeric"] is True
    assert height["frequency"] == 0.8
    assert len(height["examples"]) == 5
    assert height["numericValueCounts"] == {str(100 + index): 1 for index in range(8)}
    assert material["numericValueCounts"] is None
    assert mass["unit"] == "кг"
    assert mass_grams["unit"] == "г"
    assert mass_grams["count"] == 3
    assert mass["id"] != mass_grams["id"]
    assert color["scope"] == "adjacent"
    assert color["count"] == color["total"] == 10
    assert len(module.frequent_catalog_characteristics("01.16.01", limit=2)) == 2


def test_subtrees_and_empty_categories_stay_in_their_catalog_branch(catalog):
    fields = module.frequent_catalog_characteristics("01.16")
    assert fields[0]["count"] == fields[0]["total"] == 20
    assert set(field["scope"] for field in fields) == {"category"}
    assert "Размер одежды" not in {field["name"] for field in fields}
    empty = module.frequent_catalog_characteristics("01.16.03")
    assert empty and all(field["scope"] == "adjacent" for field in empty)
    assert module.frequent_catalog_characteristics("unknown") == []
    assert module.frequent_catalog_characteristics("") == []


def test_snapshot_is_reused_and_rebuilt_when_catalog_changes(catalog, monkeypatch):
    conn, products = catalog
    module.frequent_catalog_characteristics("01.16.01")
    monkeypatch.setattr(module, "iterate_products", lambda: pytest.fail("Snapshot should be reused"))
    assert module.frequent_catalog_characteristics("01.16.01")
    monkeypatch.setattr(module, "iterate_products", lambda: iter([products]))
    for product in products[:10]:
        product.characteristics.append({"name": "Новое поле", "value": "Да", "unit": ""})
    conn.execute("UPDATE metadata SET value='2' WHERE key='fingerprint'")
    assert "Новое поле" in {field["name"] for field in module.frequent_catalog_characteristics("01.16.01")}


def test_endpoint_returns_statistics_and_validates_category(catalog):
    client = TestClient(app)
    response = client.get("/catalog/characteristics", params={"kpgzCode": "01.16.01"})
    assert response.status_code == 200
    assert response.json()[0]["name"] == "Материал"
    assert client.get("/catalog/characteristics").status_code == 422
    assert isinstance(response.json()[0]["nameKey"], str)
    assert all(isinstance(value, str) for field in response.json() for value in field["examples"])


@pytest.mark.parametrize(("left", "right"), [
    ("  МАТЕРИАЛЫ\nРУКОЯТОК: ", "Материал рукоятки"),
    ("Жёсткость (корпуса)", "жесткости корпуса"),
    ("Цвет, покрытия.", "цвет покрытия"),
    (" Масса\tнетто! ", "массы нетто"),
])
def test_morphological_keys_ignore_only_cosmetic_variants(left, right):
    assert normalize_key(left) == normalize_key(right)


@pytest.mark.parametrize(("left", "right"), [
    ("  нержавеющей\nстали! ", "Нержавеющая сталь"),
    ("БЕЛЫМИ", "белый"),
    ("жёлтые", "желтый"),
    ("001,5000", "1.5"),
    ("-00,000", "0"),
    ("+0002.000", "2"),
    ("10,00 мм", "10.0 мм"),
    ("«Сталь»;", "сталь"),
])
def test_values_use_morphology_and_exact_decimal_canonicalization(left, right):
    assert normalize_value(left) == normalize_value(right)


@pytest.mark.parametrize(("left", "right"), [
    ("Материал рукоятки", "Материал бойка"),
    ("Длина рукоятки", "Ширина рукоятки"),
    ("Длина", "Общая длина"),
    ("Габариты длина", "Габариты ширина"),
    ("A-10", "A10"),
    ("12/24", "12 24"),
])
def test_no_fuzzy_or_partial_field_alias_merging(left, right):
    assert normalize_key(left) != normalize_key(right)


def test_unit_semantics_and_numeric_precision_are_preserved():
    assert normalize_unit(" кг. ") == "кг"
    assert normalize_unit("кг / м³") == normalize_unit("кг/м³")
    assert normalize_unit("г") != normalize_unit("кг")
    assert normalize_unit("МПа") != normalize_unit("мПа")
    assert normalize_unit("MW") != normalize_unit("mW")
    assert normalize_value("10 мм") != normalize_value("10 см")
    assert normalize_value("10 м/с") != normalize_value("10 мс")
    assert normalize_value("10 MW") != normalize_value("10 mW")
    assert normalize_value("10 МПа") != normalize_value("10 мПа")
    assert normalize_value("0.123456789123456789") == "0.123456789123456789"
    assert normalize_value("2²") != normalize_value("22")


def _replace_characteristics(catalog, rows):
    _, products = catalog
    for product in products:
        product.characteristics = []
    for product, characteristics in zip(products, rows):
        product.characteristics = characteristics


def _char(name, value, unit=""):
    return {"name": name, "value": value, "unit": unit}


def test_inflected_duplicates_count_once_per_product_and_choose_readable_variants(catalog):
    _replace_characteristics(catalog, [
        [_char("Материал рукоятки", "Стали")] * 30 + [
            _char("МАТЕРИАЛЫ РУКОЯТОК:", "Сталь"),
            _char("Материала рукоятки", "сталь"),
        ],
        [_char("Материал рукоятки", "Стали")],
        [_char("Материал рукоятки", "Сталь")],
        [_char("Материалы рукояток", "сталь")],
        [_char("Материалы рукояток", "Пластик")],
        [_char("Материалы рукояток", "Пластик")],
        [_char("Материалы рукояток", "Пластик")],
    ])
    fields = module.frequent_catalog_characteristics("01.16.01")
    assert len(fields) == 1
    field = fields[0]
    assert field["count"] == 7
    assert field["total"] == 10
    assert field["frequency"] == 0.7
    assert field["name"] == "Материалы рукояток"
    assert field["nameKey"] == normalize_key("Материал рукоятки")
    # Four products contribute to the steel group, not 32 observations.
    assert field["examples"] == ["Стали", "Пластик"]
    assert field["exampleCounts"] == {"Стали": 4, "Пластик": 3}
    assert field["numeric"] is False
    conn = module._database()
    try:
        counts = dict(conn.execute("SELECT value_key,count FROM field_values WHERE category_id=1601"))
        assert counts == {normalize_value("Сталь"): 4, normalize_value("Пластик"): 3}
    finally:
        conn.close()


def test_duplicate_display_votes_do_not_overwhelm_common_readable_variant(catalog):
    _replace_characteristics(catalog, [
        [_char("Цвета", "белыми")] * 100,
        [_char("Цвет", "Белый")],
        [_char("Цвет", "Белый")],
        [_char("Цвет", "Белый")],
    ])
    field = module.frequent_catalog_characteristics("01.16.01")[0]
    assert field["name"] == "Цвет"
    assert field["count"] == 4
    assert field["examples"] == ["Белый"]


def test_zero_boolean_empty_values_and_numeric_flags(catalog):
    characteristics = [
        _char("Количество", 0), _char("Количество", "-0,000"),
        _char("Количество", "+000.000"), _char("Наличие", False),
        _char("Наличие", "false"), _char("Включено", True),
        _char("Пустое поле", None), _char("Пустое поле", "  "),
        _char("Страна происхождения:", "Россия"),
    ]
    _replace_characteristics(catalog, [characteristics for _ in range(10)])
    fields = {field["name"]: field for field in module.frequent_catalog_characteristics("01.16.01")}
    assert set(fields) == {"Количество", "Наличие", "Включено"}
    assert fields["Количество"]["numeric"] is True
    assert len(fields["Количество"]["examples"]) == 1
    assert fields["Наличие"]["numeric"] is False
    assert len(fields["Наличие"]["examples"]) == 1
    assert fields["Включено"]["examples"] == ["True"]
    assert all(field["count"] == 10 for field in fields.values())


def test_parts_units_and_embedded_units_remain_distinct_in_snapshot(catalog):
    characteristics = [
        _char("Материал рукоятки", "Сталь"), _char("Материал бойка", "Сталь"),
        _char("Длина рукоятки", "10,00", "мм"), _char("Ширина рукоятки", "10", "мм"),
        _char("Длина рукоятки", "10", "см"),
        _char("Диаметр", "10 мм"), _char("Диаметр", "10 см"),
    ]
    _replace_characteristics(catalog, [characteristics for _ in range(10)])
    fields = module.frequent_catalog_characteristics("01.16.01")
    assert len(fields) == 6
    assert len({field["id"] for field in fields}) == 6
    assert all(field["count"] == 10 for field in fields)
    lengths = [field for field in fields if field["name"] == "Длина рукоятки"]
    assert {field["unit"] for field in lengths} == {"мм", "см"}
    assert all(field["numeric"] is True for field in lengths)
    diameter = next(field for field in fields if field["name"] == "Диаметр")
    assert set(diameter["examples"]) == {"10 мм", "10 см"}
    assert diameter["numeric"] is False


def test_decimal_popularity_is_aggregated_without_duplicate_product_votes(catalog):
    _replace_characteristics(catalog, [
        [_char("Толщина", "001,5000", "мм"), _char("ТОЛЩИНА:", "1.5", "мм")],
        [_char("Толщина", "001,5000", "мм")],
        [_char("Толщина", "1.5", "мм")],
        [_char("Толщина", "2", "мм")],
        [_char("Толщина", "2.0", "мм")],
    ])
    field = module.frequent_catalog_characteristics("01.16.01")[0]
    assert field["count"] == 5
    assert field["numeric"] is True
    assert field["examples"] == ["001,5000", "2"]
    assert field["numericValueCounts"] == {"1.5": 3, "2": 2}
    conn = module._database()
    try:
        assert dict(conn.execute("SELECT value_key,count FROM field_values")) == {"1.5": 3, "2": 2}
    finally:
        conn.close()


def test_numeric_distribution_does_not_sum_overlapping_product_votes(catalog):
    _replace_characteristics(catalog, [
        [_char("Масса", "0.1", "кг"), _char("Масса", "0.2", "кг")],
        [_char("Масса", "0.1", "кг")],
    ])
    field = module.frequent_catalog_characteristics("01.16.01")[0]
    assert field["numeric"] is True
    assert field["count"] == 2
    assert field["numericValueCounts"] is None


def test_numeric_flag_requires_every_product_value_to_be_numeric(catalog):
    _replace_characteristics(catalog, [
        [_char("Толщина", "1.5", "мм")],
        [_char("Толщина", "1.5", "мм"), _char("Толщина", "не задано", "мм")],
    ])
    field = module.frequent_catalog_characteristics("01.16.01")[0]
    assert field["count"] == 2
    assert field["numeric"] is False


def test_schema_change_forces_normalized_snapshot_rebuild(catalog, monkeypatch):
    module.frequent_catalog_characteristics("01.16.01")
    destination = module.settings.catalog_database.with_suffix(".characteristics.sqlite")
    conn = sqlite3.connect(destination)
    with conn:
        conn.execute("UPDATE metadata SET value='catalog-characteristics-v1' WHERE key='schema'")
    conn.close()
    calls = []
    products = catalog[1]

    def batches():
        calls.append("build")
        for index in range(0, len(products), 2):
            yield products[index:index + 2]

    monkeypatch.setattr(module, "iterate_products", batches)
    assert module.frequent_catalog_characteristics("01.16.01")
    assert calls == ["build"]
    conn = module._database()
    try:
        assert conn.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()[0] == module._SCHEMA
        assert conn.execute("PRAGMA temp_store").fetchone()[0] == 1
        assert conn.execute("PRAGMA cache_size").fetchone()[0] == -2048
    finally:
        conn.close()


def test_official_kpgz_schema_enriches_examples_and_fills_gaps(catalog, monkeypatch):
    material_key = normalize_key("Материал")
    long_kit = "A" * 120
    monkeypatch.setattr(module, "characteristics_for", lambda code: [
        {
            "name": "Материал", "name_key": material_key, "kind": "Эталонная",
            "value_type": "Справочник", "values": ["Сталь", "Дерево", "Пластик", long_kit],
            "unit": "", "numeric": False, "discrete": True, "spgz_characteristic_id": 1,
        },
        {
            "name": "Тип разъема", "name_key": normalize_key("Тип разъема"), "kind": "Эталонная",
            "value_type": "Справочник", "values": ["RJ-45", "BNC", "RJ-11"],
            "unit": "", "numeric": False, "discrete": True, "spgz_characteristic_id": 2,
        },
        {
            "name": "Тип столешницы", "name_key": normalize_key("Тип столешницы"), "kind": "Базовая",
            "value_type": "Справочник", "values": ["Прямая", "Угловая"],
            "unit": "", "numeric": False, "discrete": True, "spgz_characteristic_id": None,
        },
        {
            "name": "Комплектация", "name_key": normalize_key("Комплектация"), "kind": "Эталонная",
            "value_type": "Справочник", "values": [long_kit, "B" * 100],
            "unit": "", "numeric": False, "discrete": True, "spgz_characteristic_id": 3,
        },
        {
            "name": "Вид продукции", "name_key": normalize_key("Вид продукции"), "kind": "Категореобразующая",
            "value_type": "Справочник", "values": ["Товары"],
            "unit": "", "numeric": False, "discrete": True, "spgz_characteristic_id": None,
        },
    ] if code == "01.16.01" else [])
    fields = {field["name"]: field for field in module.frequent_catalog_characteristics("01.16.01")}
    assert "Сталь" in fields["Материал"]["examples"]
    assert "Дерево" in fields["Материал"]["examples"]
    assert long_kit not in fields["Материал"]["examples"]
    assert fields["Материал"]["exampleCounts"]["Дерево"] == 0
    assert fields["Тип разъема"]["scope"] == "schema"
    assert fields["Тип разъема"]["examples"] == ["RJ-45", "BNC", "RJ-11"]
    assert "Тип столешницы" not in fields  # базовые не тащим в форму
    assert "Комплектация" not in fields  # длинные enum’ы отбрасываем
    assert "Вид продукции" not in fields
