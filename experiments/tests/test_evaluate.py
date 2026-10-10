from pathlib import Path

from ktru_autofill import Autofiller
from ktru_autofill.evaluate import Ste, evaluate, forms_from_dataset, is_correct, load_ste, parse_chars

DATA = Path(__file__).parent.parent / "data-archived"


def test_parse_chars_formats():
    want = {"Формат": "A4", "Плотность": "80 г/м2"}
    assert parse_chars("Формат: A4; Плотность: 80 г/м2") == want
    assert parse_chars('{"Формат": "A4", "Плотность": "80 г/м2"}') == want
    assert parse_chars([{"name": "Формат", "value": "A4"}, {"name": "Плотность", "value": 80, "unit": "г/м2"}]) == want


def test_is_correct():
    assert is_correct({"значение": 80, "единица_как_написано": "грамм"}, "80 г/м2")
    assert is_correct(500, "500")
    assert is_correct("A4", "А4")  # кириллица в эталоне
    assert is_correct("Белый", "белая")
    assert not is_correct("Цветной", "Голубой")


def test_metrics():
    stes = [
        Ste("1", "бумага А4 80 г/м2 белая", {"Формат": "A4", "Плотность": "80 г/м2", "Цвет": "Белый", "Класс": "C"}, "x"),
        Ste("2", "бумага А3 голубая", {"Формат": "A3", "Цвет": "Голубой"}, "x"),
    ]
    rep = evaluate(stes, Autofiller(embedder=None), forms_from_dataset(stes))
    t = rep.total
    assert (t.gold, t.extracted, t.correct) == (6, 5, 5)
    assert t.precision == 1.0 and abs(t.recall - 5 / 6) < 1e-9
    assert [e["характеристика"] for e in rep.errors] == []  # «Класс C» в тексте нет — не ошибка, а потолок


def test_load_sample():
    stes = load_ste(DATA / "sample_ste.jsonl")
    assert len(stes) == 10 and stes[0].gold["Формат"] == "A4" and stes[0].code
