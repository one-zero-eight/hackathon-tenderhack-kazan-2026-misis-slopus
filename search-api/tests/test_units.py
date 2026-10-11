import gzip
import json
from dataclasses import FrozenInstanceError

import pysqlite3 as sqlite3
import pytest

from search_api import units


# Copied from the verified OKEI rows, not imported from autofill.
_NAMES = {
    "003": "Миллиметр", "004": "Сантиметр", "005": "Дециметр", "006": "Метр",
    "008": "Километр;^тысяча метров", "039": "Дюйм (25,4 мм)",
    "050": "Квадратный миллиметр", "051": "Квадратный сантиметр",
    "055": "Квадратный метр", "111": "Кубический сантиметр;^миллилитр",
    "112": "Литр;^кубический дециметр", "113": "Кубический метр",
    "116": "Декалитр", "161": "Миллиграмм", "163": "Грамм",
    "166": "Килограмм", "168": "Тонна;^метрическая тонна (1000 кг)",
    "212": "Ватт", "214": "Киловатт", "215": "Мегаватт;^тысяча киловатт",
    "222": "Вольт", "226": "Вольт-ампер", "255": "Байт",
    "256": "Килобайт", "257": "Мегабайт", "2553": "Гигабайт",
    "2554": "Терабайт", "260": "Ампер", "290": "Герц", "291": "Килогерц",
    "292": "Мегагерц", "293": "Гигагерц", "354": "Секунда", "355": "Минута",
    "356": "Час", "362": "Месяц", "366": "Год", "625": "Лист",
    "642": "Единица", "715": "Пара (2 шт.)", "744": "Процент",
    "778": "Упаковка", "796": "Штука", "839": "Комплект",
}


@pytest.fixture(autouse=True)
def catalog(tmp_path, monkeypatch):
    path = tmp_path / "classifiers.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE okei(code TEXT PRIMARY KEY,name TEXT,kind TEXT);
            CREATE TABLE raw_records(tab TEXT,code TEXT,payload BLOB,
                                     PRIMARY KEY(tab,code));
        """)
        db.executemany("INSERT INTO okei VALUES(?,?,'unit')", _NAMES.items())
        db.execute("INSERT INTO okei VALUES('s:1','Единицы длины','section')")
        for code, name in _NAMES.items():
            raw = {"code": code, "name": name, "razdel": "1", "podrazdel": "1"}
            if code == "003":
                raw["name"] += "; миллиметрик"
            db.execute("INSERT INTO raw_records VALUES('okei',?,?)", (
                code, gzip.compress(json.dumps(raw, ensure_ascii=False).encode()),
            ))
    monkeypatch.setattr(units.settings, "classifiers_sqlite", path)
    units._catalog.cache_clear()
    yield path
    units._catalog.cache_clear()


@pytest.mark.parametrize("raw,kind,factor", [
    ("мкм", "length", 0.001), ("микрометров", "length", 0.001),
    ("мм", "length", 1), ("миллиметров", "length", 1),
    ("см", "length", 10), ("сантиметрами", "length", 10),
    ("дюйма", "length", 25.4), ("inches", "length", 25.4),
    ("Дюйм (25,4 мм)", "length", 25.4), ("дм", "length", 100),
    ("м", "length", 1000), ("метров", "length", 1000),
    ("км", "length", 1_000_000), ("мг", "mass", 0.001),
    ("г", "mass", 1), ("гр", "mass", 1), ("граммов", "mass", 1),
    ("кг", "mass", 1000), ("kg", "mass", 1000),
    ("тонн", "mass", 1_000_000), ("мл", "volume", 1),
    ("см3", "volume", 1), ("кубических сантиметров", "volume", 1),
    ("л", "volume", 1000), ("дм³", "volume", 1000),
    ("м3", "volume", 1_000_000), ("мм2", "area", 1),
    ("см²", "area", 100), ("квадратными метрами", "area", 1_000_000),
    ("г/м2", "density", 1), ("г/кв.м", "density", 1),
    ("граммов на квадратный метр", "density", 1), ("gsm", "density", 1),
    ("Вт", "power", 1), ("ватт", "power", 1), ("w", "power", 1),
    ("кВт", "power", 1000), ("kw", "power", 1000),
    ("с", "time", 1), ("сек", "time", 1), ("секунд", "time", 1),
    ("минут", "time", 60), ("часов", "time", 3600),
    ("гц", "frequency", 1), ("hertz", "frequency", 1),
    ("khz", "frequency", 1000), ("МГц", "frequency", 1_000_000),
    ("ghz", "frequency", 1_000_000_000), ("byte", "data_size", 1),
    ("КБ", "data_size", 1000), ("mb", "data_size", 1_000_000),
    ("Гигабайт", "data_size", 1_000_000_000),
    ("ТБ", "data_size", 1_000_000_000_000),
    ("шт.", "count", 1), ("процентов", "percent", 1),
])
def test_legacy_physical_dimensions_and_factors(raw, kind, factor):
    parsed = units.parse_unit(raw)
    assert parsed is not None
    assert parsed[0] == kind
    assert parsed[1] == pytest.approx(factor)


@pytest.mark.parametrize("source,target,value,expected", [
    ("миллиметров", "см", 20, 2), ("inch", "мм", 1, 25.4),
    ("м", "mm", 1.8, 1800), ("кг", "г", 2, 2000),
    ("мг", "kg", 10, 0.00001), ("л", "мл", 1, 1000),
    ("см³", "ml", 10, 10), ("м³", "л", 1, 1000),
    ("м²", "см²", 1, 10000), ("gsm", "г/м²", 80, 80),
    ("кВт", "w", 2, 2000), ("МВт", "кВт", 1, 1000),
    ("минут", "с", 2, 120), ("ч", "мин", 1, 60),
    ("МГц", "khz", 2, 2000), ("ГБ", "МБ", 16, 16000),
    ("ТБ", "Гигабайт", 1, 1000), ("%", "процент", 50, 50),
    ("листов", "Лист", 5, 5), ("ядрами", "ядер", 8, 8),
    ("упак.", "Упаковка", 2, 2),
])
def test_conversions(source, target, value, expected):
    converted, errors = units.convert_units(value, source, target)
    assert errors == []
    assert converted == pytest.approx(expected)


@pytest.mark.parametrize("raw,code", [
    ("millimetre", "003"), ("millimeter", "003"), ("миллиметрик", "003"),
    ("Дюйм (25,4 мм)", "039"), ("граммами", "163"),
    ("Кубический сантиметр;^миллилитр", "111"),
    ("m^2", "055"), ("куб. м", "113"), ("2553", "2553"),
])
def test_exact_aliases_and_gzip_payload_aliases(raw, code):
    assert units.resolve_unit(raw).code == code


@pytest.mark.parametrize("raw", [
    "миллиметровая", "метрическая неизвестность", "метров кабеля",
    "граммов бумаги", "миллиграмм/м²", "м; см", "см3 лишнее", "3", "s:1",
])
def test_no_prefix_or_unknown_code_guessing(raw):
    assert units.resolve_unit(raw) is None


@pytest.mark.parametrize("code,short,pint_unit", [
    ("003", "мм", "millimeter"), ("039", "дюйм", "inch"),
    ("111", "мл", "milliliter"), ("116", "дал", "decaliter"),
    ("212", "Вт", "watt"), ("214", "кВт", "kilowatt"),
    ("215", "МВт", "megawatt"), ("226", "В·А", "volt * ampere"),
    ("260", "А", "ampere"), ("2553", "ГБ", "gigabyte"),
])
def test_verified_codes(code, short, pint_unit):
    metadata = units.get_okei_unit(code)
    assert metadata.code == code
    assert metadata.short_name == short
    assert metadata.pint_unit == pint_unit
    assert units.unit_short_name("Wrong display name", code) == short


@pytest.mark.parametrize("source,target", [
    ("упаковка", "штука"), ("комплект", "пара (2 шт.)"),
    ("лист", "упаковка"), ("ед", "упак"), ("лист", "штука"),
    ("ядро", "лист"), ("год", "м"), ("г/м²", "г"),
    ("ГБ", "г"), ("В·А", "Вт"),
])
def test_semantic_kinds_prevent_arbitrary_conversion(source, target):
    assert units.convert_units(1, source, target) == (
        None, ["несовместимые единицы измерения"],
    )


def test_target_code_is_authoritative_and_never_falls_back():
    assert units.convert_units(1, "inch", "bogus", target_okei_code="003") == (25.4, [])
    assert units.convert_units(1, "л", "мл", target_okei_code="116") == (0.1, [])
    for code in ("bad", "3", "s:1", ""):
        assert units.get_okei_unit(code) is None
        assert units.convert_units(1, "мм", "мм", target_okei_code=code) == (
            None, ["неизвестная единица: пересчёт невозможен"],
        )
    assert units.unit_short_name("Миллиметр", "bad") == "Миллиметр"
    assert units.unit_short_name("миллиметрами") == "мм"


@pytest.mark.parametrize("value,source,target,error", [
    (1, "м", None, "единица отсутствует в схеме"),
    (1, None, "м", "не указана исходная единица измерения"),
    (1, "кг", "м", "несовместимые единицы измерения"),
    (1, "furlong invented", "м", "неизвестная единица: пересчёт невозможен"),
    (1e308, "км", "мм", "переполнение при переводе единиц"),
    (float("inf"), "мм", "мм", "переполнение при переводе единиц"),
    (float("nan"), "мм", "мм", "переполнение при переводе единиц"),
])
def test_existing_errors(value, source, target, error):
    assert units.convert_units(value, source, target) == (None, [error])


def test_unknown_exact_identity_is_preserved():
    assert units.convert_units(2, "Unknown Thing", "unknown thing") == (2, [])
    assert units.convert_units(2, None, None) == (2, [])
    assert units.convert_units(2, "unknown", None) == (2, [])
    assert units.unit_short_name("unknown; alias") == "unknown"


def test_ambiguous_aliases_never_convert_even_identically(catalog):
    with sqlite3.connect(catalog) as db:
        db.execute("INSERT INTO okei VALUES('9999','Миллиметр','unit')")
    assert units.resolve_unit("миллиметр") is None
    assert units.resolve_unit("миллиметров") is None
    assert units.convert_units(2, "миллиметр", "миллиметр") == (
        None, ["неизвестная единица: пересчёт невозможен"],
    )
    assert units.convert_units(2, "миллиметр", None) == (
        None, ["неизвестная единица: пересчёт невозможен"],
    )
    assert units.get_okei_unit("003").short_name == "мм"
    assert units.resolve_unit("mm").code == "003"


def test_metadata_cache_is_immutable_and_read_only(catalog):
    before = catalog.read_bytes()
    first = units.get_okei_unit("003")
    assert units.get_okei_unit("003") is first
    with pytest.raises(FrozenInstanceError):
        first.name = "mutated"
    with pytest.raises(TypeError):
        units._current().by_code["003"] = None
    with pytest.raises(AttributeError):
        first.aliases.append("mutated")
    assert catalog.read_bytes() == before


def test_cache_is_keyed_by_database_path(tmp_path, monkeypatch):
    assert units.get_okei_unit("003") is not None
    missing = tmp_path / "missing.sqlite"
    monkeypatch.setattr(units.settings, "classifiers_sqlite", missing)
    with pytest.raises(sqlite3.OperationalError):
        units.get_okei_unit("003")
    assert not missing.exists()


def test_invalid_payload_identity_raises(catalog):
    with sqlite3.connect(catalog) as db:
        db.execute("UPDATE raw_records SET payload=? WHERE code='003'", (
            gzip.compress(json.dumps({"code": "004", "name": "Миллиметр"}).encode()),
        ))
    with pytest.raises(ValueError, match="payload code differs"):
        units.get_okei_unit("003")
