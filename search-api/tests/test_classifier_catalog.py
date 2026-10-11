import gzip
import json

import pysqlite3 as sqlite3
import pytest

from search_api import classifier_catalog as module


def _payload(value):
    return gzip.compress(json.dumps(value, ensure_ascii=False).encode())


def _characteristic(name="Тип", value="Слесарный", **overrides):
    return {"name": name, "code": "type", "type": "1", "kind": "1", "required": True,
            "actual": True, "values": [{"text": value}], **overrides}


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    ktru_path = tmp_path / "ktru.sqlite"
    classifiers_path = tmp_path / "classifiers.sqlite"
    with sqlite3.connect(ktru_path) as db:
        db.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO meta VALUES('schema','ktru-mmap-v2');
            CREATE TABLE ktru(
                code TEXT PRIMARY KEY,name TEXT NOT NULL,official_name TEXT NOT NULL,
                shortname TEXT,similar_products TEXT,status TEXT,okpd2 TEXT,okpd2_name TEXT,
                parent_code TEXT,is_template INTEGER NOT NULL,payload BLOB NOT NULL,
                name_fold TEXT NOT NULL,effective_okpd TEXT NOT NULL,is_okpd_root INTEGER NOT NULL
            );
            CREATE TABLE ktru_okpd(code TEXT PRIMARY KEY,name TEXT NOT NULL);
            INSERT INTO ktru_okpd VALUES('25.73','Инструменты');
            CREATE INDEX ix_ktru_parent ON ktru(parent_code,code);
            CREATE INDEX ix_ktru_roots ON ktru(effective_okpd,is_okpd_root,code);
        """)
    with sqlite3.connect(classifiers_path) as db:
        db.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO meta VALUES('schema','classifiers-full-v4');
            CREATE TABLE raw_records(tab TEXT NOT NULL,code TEXT NOT NULL,payload BLOB NOT NULL,
                                     PRIMARY KEY(tab,code)) WITHOUT ROWID;
            CREATE TABLE okpd(id INTEGER PRIMARY KEY,code TEXT,name TEXT,name_fold TEXT,synonyms_fold TEXT,
                              parent_id INTEGER,level INTEGER,children_count INTEGER,is_actual INTEGER);
            CREATE TABLE kpgz(id INTEGER PRIMARY KEY,code TEXT,name TEXT,name_fold TEXT,parent_id INTEGER,
                              external_id INTEGER,tree_level INTEGER,is_last INTEGER,okpd_code TEXT,okpd_name TEXT);
            CREATE TABLE spgz(id INTEGER PRIMARY KEY,name TEXT,name_fold TEXT,kpgz_external_id INTEGER,
                              okpd2_code TEXT,ktru_id INTEGER,is_excluded INTEGER);
            CREATE TABLE okei(id INTEGER PRIMARY KEY,code TEXT,name TEXT,name_fold TEXT,razdel TEXT,
                              podrazdel TEXT,kind TEXT,parent_id INTEGER,children_count INTEGER);
            INSERT INTO okpd VALUES(1,'25.73','Инструменты','инструменты','',NULL,2,0,1);
            INSERT INTO kpgz VALUES(1,'01.10','Молотки','молотки',NULL,100,1,1,'25.73','Инструменты');
            INSERT INTO okei VALUES(1,'003','Миллиметр; millimetre','миллиметр','1','1','unit',NULL,0);
            INSERT INTO okei VALUES(2,'166','Килограмм','килограмм','1','1','unit',NULL,0);
            INSERT INTO okei VALUES(3,'796','Штука','штука','1','1','unit',NULL,0);
            CREATE INDEX ix_spgz_kpgz ON spgz(kpgz_external_id);
        """)
    monkeypatch.setattr(module.settings, "ktru_sqlite", ktru_path)
    monkeypatch.setattr(module.settings, "classifiers_sqlite", classifiers_path)
    return module.ClassifierCatalog(ktru_path, classifiers_path)


def _add_ktru(catalog, code, name="Молоток", parent=None, template=False, status="ACTIVE", okpd="25.73", characteristics=None, **raw_extra):
    raw = {"code": code, "name": "Официальный молоток", "status": status, "is_template": template,
           "okpd2": {"code": okpd, "name": "Инструменты"}, "characteristics": characteristics or [],
           **raw_extra}
    if parent:
        raw["parent"] = {"code": parent, "version": "1"}
    with sqlite3.connect(catalog.ktru_path) as db:
        db.execute("INSERT INTO ktru VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            code, name, "Официальный молоток", None, None, status, okpd, "Инструменты", parent,
            int(template), _payload(raw), name.lower(), okpd, int(parent is None),
        ))


def _add_spgz(catalog, id, name="Молоток", excluded=False, external=100, characteristics=None, **raw_extra):
    raw = {"id": id, "name": "Название в исходной выгрузке", "kpgzId": external,
           "spgzCharacteristics": characteristics or [], **raw_extra}
    with sqlite3.connect(catalog.classifiers_path) as db:
        db.execute("INSERT INTO spgz VALUES(?,?,?,?,?,?,?)", (id, name, name.lower(), external, "25.73", None, int(excluded)))
        db.execute("INSERT INTO raw_records VALUES('spgz',?,?)", (str(id), _payload(raw)))


def _spgz_characteristic(value, name="Тип", **overrides):
    return {"characteristicName": name, "kpgzCharacteristicId": 42, "valueTypeId": 5,
            "catalogVariantId": 1, "conditionTypeId": 8,
            "characteristicSpgzEnums": [{"value": value}], **overrides}


def test_ktru_official_schema_uses_beautiful_name_without_mirror_characteristics(catalog):
    _add_ktru(catalog, "template", name="Молотки ручные", template=True,
              characteristics=[_characteristic()], no_new_features=True,
              no_new_features_reason="Запрет дополнительных свойств", description=" Описание ",
              version="4", version_code="v4", nsi={"standards": [{"document_name": " ГОСТ 2310 "}]},
              inclusion_date="2025-02-03T12:00:00", units=[{"code": "796", "name": "Штука; Piece"}],
              rubricators=[{"code": "R", "name": "Рубрика"}], print_form={"url": "https://example.test/print"})
    record = module.get_ktru_record("template")
    assert record["name"] == "Молотки ручные"
    assert record["officialName"] == "Официальный молоток"
    assert record["characteristics"][0]["options"] == ["Слесарный"]
    assert record["standards"] == ["ГОСТ 2310"]
    assert record["noNewFeatures"] is True
    assert record["noNewFeaturesReason"] == "Запрет дополнительных свойств"
    _add_ktru(catalog, "unknown-policy", name="Без флага", characteristics=[_characteristic()])
    assert module.get_ktru_record("unknown-policy")["noNewFeatures"] is None
    _add_ktru(catalog, "allows-extra", name="С доп.", characteristics=[_characteristic()], no_new_features=False)
    assert module.get_ktru_record("allows-extra")["noNewFeatures"] is False
    assert record["units"] == [{"code": "796", "name": "Штука"}]
    assert record["dates"] == [{"label": "Дата включения", "value": "2025-02-03"}]
    assert record["versionCode"] == "v4"
    assert record["printFormUrl"] == "https://example.test/print"
    # Repeated lookups never share editable drafts or mutate stored schemas.
    record["characteristics"][0]["options"].append("Произвольное значение")
    assert catalog.get_ktru_record("template")["characteristics"][0]["options"] == ["Слесарный"]


def test_ktru_ranges_preserve_decimal_bounds_and_inclusivity(catalog):
    _add_ktru(catalog, "ranged", characteristics=[_characteristic(
        name="Длина", type="2", values=[{"unit": {"code": "003", "name": "Миллиметр"}, "ranges": [
            {"min": "1,5", "min_operator": "greater", "max": "10", "max_operator": "lessOrEqual"},
            {"min": "10", "min_operator": "greater", "max": "", "max_operator": "less"},
        ]}])])
    item = catalog.get_ktru_record("ranged")["characteristics"][0]
    assert item["options"] == [">1,5 … ≤10 мм", ">10 мм"]
    assert item["ranges"] == [
        {"option": ">1,5 … ≤10 мм", "min": 1.5, "max": 10, "minInclusive": False, "maxInclusive": True},
        {"option": ">10 мм", "min": 10, "max": None, "minInclusive": False, "maxInclusive": False},
    ]
    assert item["unit"] == "мм"
    draft = module.draft_ktru_characteristics(catalog.get_ktru_record("ranged"))[0]
    assert draft["value"] == draft["rangeOption"] == ""
    assert draft["ranges"] == item["ranges"]
    assert draft["badges"] == []
    assert draft["source"] == "ktru"


def test_concrete_values_are_separate_options_and_choice_type_is_not_multiselect(catalog):
    _add_ktru(catalog, "concrete", characteristics=[
        _characteristic(name="Материал", kind="3", choice_type="1", values=[{"concrete_values": ["Сталь", "Дерево"]}]),
        _characteristic(name="Форма", kind="2", choice_type="3", values=[{"concrete_values": ["Квадратная", "квадратный", "Круглая"]}]),
        _characteristic(name="Старое поле", actual=False),
        _characteristic(name="Смешанный", values=[{"concrete_values": ["10"], "ranges": [{"min": "0", "max": "20"}]}]),
    ])
    items = catalog.get_ktru_record("concrete")["characteristics"]
    assert len(items) == 3
    assert items[0]["options"] == ["Сталь", "Дерево"]
    assert items[0]["multiple"] is True
    assert items[1]["options"] == ["Квадратная", "Круглая"]
    assert items[1]["multiple"] is False
    assert items[2]["ranges"] == []
    drafts = module.draft_ktru_characteristics(catalog.get_ktru_record("concrete"))
    assert all(row["value"] == "" for row in drafts)
    assert drafts[0]["multiple"] is True


def test_opaque_options_keep_codes_and_show_standard_hint(catalog):
    _add_ktru(catalog, "opaque", characteristics=[_characteristic(values=[{"concrete_values": ["1", "2"]}])],
              nsi={"standards": [{"document_name": "ГОСТ 2310"}]})
    item = catalog.get_ktru_record("opaque")["characteristics"][0]
    assert item["optionLabels"] == {"1": "1", "2": "2"}
    assert item["hint"] == "ГОСТ 2310"


def test_spgz_schema_units_badges_and_manual_drafts(catalog):
    _add_spgz(catalog, 1, name="Молоток слесарный", okeis=[{"code": "003"}, {"code": "166"}, {"code": "796"}], characteristics=[
        _spgz_characteristic("Слесарный"),
        _spgz_characteristic("", name="Длина", valueTypeId=1, conditionTypeId=5,
                             characteristicSpgzEnums=[], value1="100", value2="200", minMathNotation=6, maxMathNotation=4),
        _spgz_characteristic("", name="Масса", valueTypeId=1, conditionTypeId=6,
                             characteristicSpgzEnums=[], value1="1,5", minMathNotation=6),
        _spgz_characteristic("", name="Твёрдость", valueTypeId=1, conditionTypeId=1, characteristicSpgzEnums=[]),
    ])
    record = module.get_directory_record("spgz", "1")
    assert record["name"] == "Молоток слесарный"
    assert record["links"][1] == {"tab": "kpgz", "code": "01.10", "label": "КПГЗ 01.10 — Молотки"}
    assert record["characteristics"][1]["value"] == "≥100 … <200 мм"
    assert record["characteristics"][2]["value"] == "≥ 1,5 кг"
    assert record["characteristics"][3]["parts"] == []
    drafts = module.draft_spgz_characteristics(record)
    assert drafts[0]["id"] == "spgz:42"
    assert drafts[0]["options"] == ["Слесарный"]
    assert drafts[0]["value"] == ""  # Even sole enums await an explicit manual/model choice.
    assert drafts[0]["required"] is True
    assert drafts[1]["unit"] == "мм"
    assert drafts[1]["numeric"] is True
    assert drafts[1]["ranges"] == []  # SPGZ constraints remain hints, not KTRU selectable ranges.
    assert drafts[1]["hint"] == "Ограничение СПГЗ: ≥100 … <200 мм"
    assert drafts[3]["numeric"] is True
    assert drafts[3]["unit"] == ""
    assert all(row["value"] == row["rangeOption"] == "" for row in drafts)


def test_units_are_not_guessed_across_dimensions(catalog):
    _add_spgz(catalog, 1, okeis=[{"code": "796"}], characteristics=[
        _spgz_characteristic("", name="Длина", valueTypeId=1, characteristicSpgzEnums=[], value1="12"),
    ])
    draft = module.draft_spgz_characteristics(catalog.get_directory_record("spgz", "1"))[0]
    assert draft["unit"] == ""
    assert draft["hint"] == "Ограничение СПГЗ: 12"


def test_sole_ktru_requires_one_active_template_root(catalog):
    _add_ktru(catalog, "template", name="Молотки ручные", template=True)
    _add_ktru(catalog, "child", parent="template")
    assert module.sole_ktru_for_okpd("25.73") == {"code": "template", "name": "Молотки ручные"}
    assert module.sole_ktru_for_okpd("unknown") is None
    with sqlite3.connect(catalog.ktru_path) as db:
        db.execute("UPDATE ktru SET status='CANCELLED' WHERE code='template'")
    assert catalog.sole_ktru_for_okpd("25.73") is None
    with sqlite3.connect(catalog.ktru_path) as db:
        db.execute("UPDATE ktru SET status=NULL,is_template=0 WHERE code='template'")
    assert catalog.sole_ktru_for_okpd("25.73") is None
    with sqlite3.connect(catalog.ktru_path) as db:
        db.execute("UPDATE ktru SET is_template=1 WHERE code='template'")
    assert catalog.sole_ktru_for_okpd("25.73")
    _add_ktru(catalog, "second-root", status="CANCELLED")
    assert catalog.sole_ktru_for_okpd("25.73") is None  # Never silently ignore another root.


def test_sole_spgz_ignores_excluded_but_not_multiple_active_children(catalog):
    _add_spgz(catalog, 1, name="Молоток слесарный")
    _add_spgz(catalog, 2, excluded=True)
    assert module.sole_spgz_for_kpgz("01.10") == {"code": "1", "name": "Молоток слесарный"}
    assert module.get_directory_record("spgz", "2")["excluded"] is True  # Manual inspection is still allowed.
    _add_spgz(catalog, 3)
    assert catalog.sole_spgz_for_kpgz("01.10") is None
    assert catalog.sole_spgz_for_kpgz("unknown") is None


def _add_siblings(catalog):
    _add_ktru(catalog, "template", name="Молотки ручные", template=True)
    _add_ktru(catalog, "steel", name="Молоток слесарный", parent="template", characteristics=[_characteristic()])
    _add_ktru(catalog, "wood", name="Молоток столярный", parent="template", characteristics=[_characteristic(value="Столярный")])
    _add_spgz(catalog, 1, name="СПГЗ молоток слесарный", characteristics=[_spgz_characteristic("Слесарный")])
    _add_spgz(catalog, 2, name="СПГЗ молоток столярный", characteristics=[_spgz_characteristic("Столярный")])


def test_specialization_pairs_siblings_and_preserves_names_and_current_value(catalog):
    _add_siblings(catalog)
    spec = module.get_catalog_specialization({"ktruCode": "steel", "spgzCode": "1"})
    assert spec == {
        "characteristics": [{"name": "Тип", "code": "type"}],
        "templateKtruCode": "template", "templateKtruName": "Молотки ручные",
        "currentValues": {"Тип": "Слесарный"},
        "options": [
            {"values": {"Тип": "Слесарный"}, "ktruCode": "steel", "ktruName": "Молоток слесарный", "spgzCode": "1", "spgzName": "СПГЗ молоток слесарный"},
            {"values": {"Тип": "Столярный"}, "ktruCode": "wood", "ktruName": "Молоток столярный", "spgzCode": "2", "spgzName": "СПГЗ молоток столярный"},
        ],
    }
    assert catalog.get_catalog_specialization({"ktruCode": "template", "kpgzCode": "01.10"})["currentValues"] is None
    node = catalog.get_ktru_node("steel")
    assert node["name"] == "Молоток слесарный"
    assert node["variantLabel"] == "Тип: Слесарный"
    assert node["isTemplate"] is False
    assert catalog.get_ktru_node("template")["childCount"] == 2


def test_ktru_discriminator_prefers_required_single_choice_characteristics(catalog):
    _add_ktru(catalog, "template", template=True)
    for code, value in [("a", "Сталь"), ("b", "Дерево")]:
        _add_ktru(catalog, code, parent="template", characteristics=[
            _characteristic(name="Вид", value=value, code="optional", required=False),
            _characteristic(name="Материал", value=value, code="preferred"),
        ])
    spec = catalog.get_ktru_specialization("template")
    assert [item["name"] for item in spec["characteristics"]] == ["Материал"]
    assert spec["characteristics"][0]["code"] == "preferred"


def test_different_spgz_discriminator_does_not_pair_unrelated_values(catalog):
    _add_ktru(catalog, "template", template=True)
    _add_ktru(catalog, "a", parent="template", characteristics=[_characteristic(value="Сталь")])
    _add_ktru(catalog, "b", parent="template", characteristics=[_characteristic(value="Дерево")])
    _add_spgz(catalog, 1, characteristics=[_spgz_characteristic("Сталь", name="Материал")])
    _add_spgz(catalog, 2, characteristics=[_spgz_characteristic("Дерево", name="Материал")])
    spec = catalog.get_catalog_specialization({"ktruCode": "template", "spgzCode": "1"})
    assert [item["name"] for item in spec["characteristics"]] == ["Тип"]
    assert all(option["spgzCode"] is None for option in spec["options"])


def test_variant_label_always_surfaces_discriminating_characteristics(catalog):
    _add_siblings(catalog)
    assert catalog.get_ktru_node("steel")["variantLabel"] == "Тип: Слесарный"
    assert catalog.get_ktru_node("wood")["variantLabel"] == "Тип: Столярный"
    _add_ktru(catalog, "density-template", name="Наволочки", template=True)
    _add_ktru(
        catalog, "density-soft", name="Наволочка ХБ, плотность от 160 г/кв.м", parent="density-template",
        characteristics=[_characteristic(name="Плотность ткани, гр/кв.м", value="≥160", code="density", type="2", kind="2")],
    )
    _add_ktru(
        catalog, "density-exact", name="Наволочка ХБ ≥155 г/кв.м", parent="density-template",
        characteristics=[_characteristic(name="Плотность ткани, гр/кв.м", value="≥155", code="density", type="2", kind="2")],
    )
    assert catalog.get_ktru_node("density-soft")["variantLabel"] == "Плотность ткани, гр/кв.м: ≥160"
    assert catalog.get_ktru_node("density-exact")["variantLabel"] == "Плотность ткани, гр/кв.м: ≥155"


def test_specialization_filters_inactive_ktru_and_excluded_spgz(catalog):
    _add_siblings(catalog)
    _add_ktru(catalog, "cancelled", parent="template", status="CANCELLED", characteristics=[_characteristic()])
    _add_spgz(catalog, 3, excluded=True, characteristics=[_spgz_characteristic("Слесарный")])
    spec = catalog.get_catalog_specialization({"ktruCode": "template", "kpgzCode": "01.10"})
    assert len(spec["options"]) == 2
    assert spec["currentValues"] is None


def test_discriminator_requires_unique_single_values_for_all_siblings(catalog):
    _add_ktru(catalog, "template", template=True)
    _add_ktru(catalog, "a", parent="template", characteristics=[_characteristic()])
    _add_ktru(catalog, "b", parent="template", characteristics=[_characteristic()])
    assert catalog.get_ktru_specialization("template") is None
    _add_spgz(catalog, 1, characteristics=[_spgz_characteristic("Сталь")])
    _add_spgz(catalog, 2, characteristics=[_spgz_characteristic("Дерево", characteristicSpgzEnums=[{"value": "Дерево"}, {"value": "Сталь"}])])
    assert catalog.get_spgz_specialization({"kpgzCode": "01.10"}) is None


def test_spgz_specialization_uses_preferred_name_and_concrete_values(catalog):
    for id, kind, material in [(1, "Слесарный", "Сталь"), (2, "Столярный", "Дерево")]:
        _add_spgz(catalog, id, name=f"Красивое название {id}", characteristics=[
            _spgz_characteristic(kind),
            _spgz_characteristic("", name="Материал", characteristicSpgzEnums=[], value1=material, value2=None),
        ])
    spec = catalog.get_spgz_specialization({"spgzCode": "2", "preferredCharNames": [" материал "]})
    assert [item["name"] for item in spec["characteristics"]] == ["Материал"]
    assert spec["currentValues"] == {"Материал": "Дерево"}
    assert spec["options"][0]["name"] == "Красивое название 2"
    merged = catalog.get_catalog_specialization({"spgzCode": "1"})
    assert merged["templateKtruCode"] is None
    assert all(option["ktruCode"] is None for option in merged["options"])


def test_multi_characteristic_specialization_partitions_by_value_tuples(catalog):
    _add_ktru(catalog, "template", template=True)
    for code, material, size in [
        ("a", "Сталь", "10"),
        ("b", "Сталь", "20"),
        ("c", "Медь", "10"),
        ("d", "Медь", "20"),
    ]:
        _add_ktru(catalog, code, parent="template", characteristics=[
            _characteristic(name="Материал", value=material, code="material"),
            _characteristic(name="Размер", value=size, code="size"),
        ])
    spec = catalog.get_ktru_specialization("b")
    assert sorted(item["name"] for item in spec["characteristics"]) == ["Материал", "Размер"]
    assert spec["currentValues"] == {"Материал": "Сталь", "Размер": "20"}
    assert len(spec["options"]) == 4


def test_spgz_specialization_is_bounded_to_eighty_siblings(catalog):
    for id in range(81):
        _add_spgz(catalog, id, characteristics=[_spgz_characteristic(str(id))])
    assert catalog.get_spgz_specialization({"kpgzCode": "01.10"}) is None


def test_catalog_databases_are_read_only_and_versions_are_checked(catalog):
    with catalog._db("ktru") as db:
        assert db.execute("PRAGMA query_only").fetchone()[0] == 1
        assert db.execute("PRAGMA cache_size").fetchone()[0] == -2048
        assert db.execute("PRAGMA temp_store").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("DELETE FROM ktru")
    with sqlite3.connect(catalog.ktru_path) as db:
        db.execute("UPDATE meta SET value='old-schema'")
    with pytest.raises(RuntimeError, match="ktru-mmap-v2"):
        catalog.get_ktru_record("unknown")


def test_missing_catalog_is_never_built_on_lookup(tmp_path):
    path = tmp_path / "missing.sqlite"
    catalog = module.ClassifierCatalog(path, path)
    with pytest.raises(sqlite3.OperationalError, match="unable to open"):
        catalog.get_ktru_record("code")
    assert not path.exists()


def test_unknown_and_invalid_codes_and_empty_drafts(catalog):
    assert catalog.get_ktru_record("unknown") is None
    assert catalog.get_ktru_node("unknown") is None
    assert catalog.get_directory_record("spgz", "unknown") is None
    assert catalog.get_directory_record("spgz", str(2**63)) is None
    assert catalog.get_directory_record("kpgz", "unknown") is None
    assert catalog.get_directory_record("unknown", "code") is None
    assert catalog.get_ktru_record("x" * 65) is None
    assert module.draft_ktru_characteristics(None) == []
    assert module.draft_spgz_characteristics(None) == []
    assert catalog.get_directory_record("okpd", "25.73")["name"] == "Инструменты"
    assert catalog.get_directory_record("okei", "003")["name"] == "Миллиметр; millimetre"
