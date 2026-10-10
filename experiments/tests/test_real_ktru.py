import math
import zlib

import numpy as np
import pytest

from ktru_autofill import Autofiller, units as U
from ktru_autofill.catalog import Catalog
from ktru_autofill.hierarchy import FlatRetriever, OkpdTree
from ktru_autofill.ktru_import import canonical_unit, convert_characteristic, parse_range
from ktru_autofill.schema import KtruCandidate


class TrigramEmbedder:
    """Игрушечный эмбеддер: мешок символьных триграмм — чтобы тесты не грузили модель."""

    dim = 512

    def encode(self, texts):
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            t = f"  {t.lower()}  "
            for j in range(len(t) - 2):
                out[i, zlib.crc32(t[j : j + 3].encode()) % self.dim] += 1
            out[i] /= np.linalg.norm(out[i]) or 1
        return out


def test_parse_ktru_values():
    assert parse_range("80") == (80, 80)
    assert parse_range("≥ 80 и < 90") == (80, 90)
    assert parse_range("> 0,5 и ≤ 1") == (0.5, 1)
    assert parse_range("≤ 5000") == (0, 5000)
    assert parse_range("≥ 10") == (10, math.inf)
    assert parse_range("Для лазерной печати") is None


def test_ktru_units():
    assert canonical_unit("Кубический сантиметр;^миллилитр") == "мл"
    assert canonical_unit("Дюйм (25,4 мм)") == "дюйм"
    assert canonical_unit("Литр; кубический дециметр") == "л"
    assert canonical_unit("") is None
    ch = convert_characteristic({"attr_name": "Объем", "attr_values": [
        {"value": "≥ 250 и < 500", "value_unit": "Кубический сантиметр;^миллилитр"}]})
    assert ch == {"name": "Объем", "type": "number", "unit": "мл", "range": [250, 500]}


def test_unit_conversion_and_prepositions():
    assert U.convert(1.2, U.lookup("м"), U.canonical("мм")) == 1200
    assert U.convert(500, U.lookup("листов"), U.canonical("шт")) == 500
    assert U.lookup("в") is None and U.lookup("с") is None  # «3 в 1», «2 с иглой»


@pytest.fixture(autouse=True)
def _tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr("ktru_autofill.hierarchy.CACHE_DIR", tmp_path)


def _positions():
    raw = [
        ("31.01.12.122-00000001", "Парта", "31.01.12.122", [("Тип", ["Двухместная"]), ("Ростовая группа", (1, 4))]),
        ("31.01.12.122-00000002", "Парта", "31.01.12.122", [("Тип", ["Одноместная"]), ("Ростовая группа", (1, 4))]),
        ("31.01.11.150-00000001", "Стул ученический", "31.01.11.150", [("Ростовая группа", (0, 6))]),
        ("17.12.14.110-00000001", "Бумага для офисной техники", "17.12.14.110", [("Формат", ["A4"])]),
        ("17.12.14.110-00000002", "Бумага для офисной техники", "17.12.14.110", [("Формат", ["A3"])]),
        ("32.50.30.110-00000001", "Стул эргономичный", "32.50.30.110", []),
    ]
    out = []
    for code, name, okpd, chars in raw:
        cs = [{"name": n, "type": "enum", "values": v} if isinstance(v, list)
              else {"name": n, "type": "count", "range": list(v)} for n, v in chars]
        out.append(KtruCandidate.from_dict({"code": code, "name": name, "okpd2": okpd, "characteristics": cs}))
    return out


OKPD = [
    {"c": "31", "n": "Мебель", "p": "C"}, {"c": "31.0", "n": "Мебель", "p": "31"},
    {"c": "31.01", "n": "Мебель для офисов", "p": "31.0"},
    {"c": "31.01.11", "n": "Мебель металлическая", "p": "31.01"},
    {"c": "31.01.11.150", "n": "Мебель для сидения", "p": "31.01.11"},
    {"c": "31.01.12", "n": "Мебель деревянная", "p": "31.01"},
    {"c": "31.01.12.122", "n": "Столы ученические", "p": "31.01.12"},
    {"c": "17", "n": "Бумага и изделия из бумаги", "p": "C"},
    {"c": "17.12.14.110", "n": "Бумага для печати", "p": "17"},
    {"c": "32", "n": "Изделия готовые прочие", "p": "C"},
    {"c": "32.50.30.110", "n": "Мебель медицинская", "p": "32"},
]


def test_tree_finds_product_and_skips_single_child_chains():
    emb = TrigramEmbedder()
    pos = _positions()
    tree = OkpdTree(OKPD, pos, emb, path_mode="raw", alpha=0.0, beam=3)
    assert set(tree.nodes[""].children) == {"31", "17", "32"}
    r = tree.retrieve("парта двухместная")
    assert {pos[i].name for i in r.candidates} == {"Парта"}
    # «31 → 31.0 → 31.01» — цепочка из одного ребёнка: решений меньше, чем узлов.
    best = r.paths[0]
    assert best.decisions < len(best.nodes) - 1
    assert {pos[i].name for i in FlatRetriever(pos, emb).retrieve("бумага офисная").candidates} == {
        "Бумага для офисной техники"}


def test_narrowing_skips_value_nobody_accepts():
    emb = TrigramEmbedder()
    cat = Catalog(_positions())
    cat.retriever = OkpdTree(OKPD, cat.positions, emb, path_mode="raw", alpha=0.0, beam=3)
    filler = Autofiller(cat, embedder=None)
    r = filler.fill("парта двухместная ростовая группа 6")
    # Группы 6 у парт нет: значение не применяется, «Стул эргономичный» без анкеты не всплывает.
    assert {c.name for c in r.candidates} == {"Парта"}
    assert r.code == "31.01.12.122-00000001"
    assert any(m.предупреждение for m in r.matches if m.характеристика == "Ростовая группа") or \
        r.values.get("Ростовая группа") is None
