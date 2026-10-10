import json
import os
from pathlib import Path

import pytest

from ktru_autofill import Autofiller
from ktru_autofill.numbers import extract_numbers
from ktru_autofill.text import lemmatize

CANDIDATES = json.loads((Path(__file__).parent.parent / "data-archived" / "candidates_paper.json").read_text())


@pytest.fixture(scope="module")
def fuzzy():
    return Autofiller(embedder=None)


def test_example_from_task(fuzzy):
    r = fuzzy.fill("бумага для принтера А4 80 грамм 500 листов белая", CANDIDATES)
    assert r.values == {
        "Формат": "A4",
        "Плотность": {"значение": 80, "единица": "г/м2", "как_написано": "80 грамм"},
        "Количество листов в пачке": 500,
        "Цвет": "Белый",
        "Класс бумаги": None,
        "Назначение": None,
    }
    # «Тип поверхности» есть только у цветной бумаги — после сужения его нет в анкете.
    assert [c.code for c in r.candidates] == ["17.12.14.129-00000001"]


def test_lemmatize_drops_stop_words():
    assert lemmatize("для лазерной печати") == "лазерный печать"
    assert lemmatize("белая") == "белый"


def test_numbers_skip_codes_and_keep_units():
    got = [(m.number, m.unit_raw, m.dims) for m in extract_numbers("А4, 80г/м², 500 л., 0,7 мм, 210x297")]
    assert got == [(80, "г/м²", ()), (500, "л.", ()), (0.7, "мм", ()), (210, None, (210, 297))]


def test_typo_is_fixed_and_reported(fuzzy):
    r = fuzzy.fill("бумвга офисная белоя А4", CANDIDATES)
    assert r.values["Цвет"] == "Белый"
    typos = {(t.как_написано, t.исправление): t.похожесть for t in r.typos}
    assert typos[("бумвга", "бумага")] == pytest.approx(0.83)


def test_cyrillic_class_letter(fuzzy):
    assert fuzzy.fill("бумага А4 класс С", CANDIDATES).values["Класс бумаги"] == "C"


def test_unitless_number_by_name_and_range(fuzzy):
    v = fuzzy.fill("бумага плотность 80, 500 шт", CANDIDATES).values
    assert v["Плотность"] == {"значение": 80, "единица": "г/м2", "как_написано": None}
    assert v["Количество листов в пачке"] == 500


def test_out_of_range_is_flagged(fuzzy):
    r = fuzzy.fill("картон 1000 г/м2", CANDIDATES)
    (m,) = [m for m in r.matches if m.характеристика == "Плотность"]
    assert m.предупреждение


def test_unrelated_text_fills_nothing(fuzzy):
    r = fuzzy.fill("ручка шариковая синяя 0,7 мм", CANDIDATES)
    assert all(v is None for v in r.values.values())


@pytest.mark.skipif(os.environ.get("KTRU_NO_EMBEDDINGS") is not None, reason="без модели")
def test_embeddings_suggest_paraphrase():
    """Пересказ ловится эмбеддингами, но только подсказкой: сам не заполняется."""
    pytest.importorskip("sentence_transformers")
    r = Autofiller().fill("бумага А4 для лазерного принтера", CANDIDATES)
    assert r.values["Назначение"] is None
    assert ("Назначение", "Для лазерной печати") in {(m.характеристика, m.значение) for m in r.suggestions}
    assert r.values["Цвет"] is None
