import asyncio
import json

import httpx
import pytest

from search_api import characteristic_autofill as autofill
from test_characteristic_autofill import mapping_handler, mock_providers, request


@pytest.mark.parametrize("value, source, target, expected", [
    (18, "В", "В", 18), (18000, "мВ", "В", 18), (0.018, "кВ", "В", 18),
    (18, "В", "мВ", 18000), (18, "В", "кВ", 0.018),
    (50, "Гц", "Гц", 50), (2, "кГц", "Гц", 2000),
    (2, "МГц", "кГц", 2000), (2.4, "ГГц", "МГц", 2400),
    (18000, "millivolt", "V", None), (18000, "mV", "V", 18),
    (2.4, "GHz", "MHz", 2400), (18, "Вольт", "мВ", 18000),
])
def test_voltage_frequency_conversion(value, source, target, expected):
    converted, errors = autofill.convert_units(value, source, target)
    if expected is None:
        assert errors
    else:
        assert not errors
        assert converted == pytest.approx(expected)


@pytest.mark.parametrize("source,target", [("В", "Вт"), ("Гц", "В"), ("МГц", "кг")])
def test_voltage_frequency_dimensions_cannot_mix(source, target):
    assert autofill.convert_units(18, source, target)[1] == ["несовместимые единицы измерения"]


def test_voltage_is_converted_with_grounded_evidence(monkeypatch):
    text = "Дрель, напряжение 18000 мВ"
    mock_providers(monkeypatch, llm_handler=mapping_handler(
        {"0": "18000 мВ"}, {"0": "напряжение 18000 мВ"},
    ))
    row = asyncio.run(autofill.autofill_characteristics(request(
        text=text, additionalAllowed=False, characteristics=[
            {"id": "voltage", "name": "Напряжение", "numeric": True, "unit": "В"},
        ],
    )))["characteristics"][0]
    assert row["status"] == "accepted" and row["value"] == "18"
    assert row["conversion"]["from"] == "мВ"
    assert text[row["evidence"]["start"]:row["evidence"]["end"]] == row["quote"]


def screen_field(**kwargs):
    return autofill.AutofillCharacteristic(
        id="ratio", name="Соотношение сторон экрана", options=kwargs.pop("options", ["4:3", "16:9"]), **kwargs,
    )


@pytest.mark.parametrize("separator", ["×", "x", "х", "*"])
def test_screen_ratio_from_resolution(separator):
    assert autofill.aspect_ratio_from_text(screen_field(), f"Монитор, разрешение 2560{separator}1440") == "16:9"


@pytest.mark.parametrize("quote", [
    "Габариты корпуса 2560×1440 мм", "Размер изображения 2560×1440",
    "2560×1440", "Экран 0×1440", "Экран 2560×1440×1080",
    "Монитор, разрешение 2560×1440 или 1920×1200", "Экран 2560×1440 см",
])
def test_unrelated_or_ambiguous_dimensions_do_not_prove_screen_ratio(quote):
    assert autofill.aspect_ratio_from_text(screen_field(), quote) is None


def test_ratio_not_present_in_options_is_not_added():
    assert autofill.aspect_ratio_from_text(screen_field(options=["4:3"]), "Разрешение экрана 2560×1440") is None


def test_wrong_llm_ratio_is_rejected_by_arithmetic(monkeypatch):
    mock_providers(monkeypatch, llm_handler=mapping_handler(
        {"0": "4:3"}, {"0": "разрешение экрана 2560×1440"},
    ))
    row = asyncio.run(autofill.autofill_characteristics(request(
        text="Монитор, разрешение экрана 2560×1440", additionalAllowed=False,
        characteristics=[screen_field()],
    )))["characteristics"][0]
    assert row["status"] == "rejected" and row["value"] is None
    assert "соотношение сторон не соответствует разрешению в цитате" in row["errors"]


@pytest.mark.parametrize("quote, accepted", [
    ("соотношение сторон экрана 16 : 9", True),
    ("размер изображения 2560×1440", False),
    ("габариты монитора 2560×1440 мм", False),
    ("разрешение экрана 2560×1440 или 1920×1200", False),
])
def test_ratio_needs_direct_or_calculable_evidence(monkeypatch, quote, accepted):
    mock_providers(monkeypatch, llm_handler=mapping_handler({"0": "16:9"}, {"0": quote}))
    row = asyncio.run(autofill.autofill_characteristics(request(
        text=quote, additionalAllowed=False, characteristics=[screen_field()],
    )))["characteristics"][0]
    assert row["status"] == ("accepted" if accepted else "rejected")


@pytest.mark.parametrize("confirmed", [True, False])
def test_derived_ratio_requires_jev_and_exact_evidence(monkeypatch, confirmed):
    seen = []

    def jev(req):
        body = json.loads(req.content)
        seen.append(body)
        return httpx.Response(200, json={"model": "jev", "answers": {
            key: {"type": "noul", "noul": 0.99 if confirmed else 0.1}
            for key in body["questions"]
        }})

    mock_providers(monkeypatch, llm_handler=mapping_handler({}), jev_handler=jev)
    text = "Монитор, разрешение экрана 2560×1440"
    row = asyncio.run(autofill.autofill_characteristics(request(
        text=text, additionalAllowed=False, characteristics=[screen_field()],
    )))["characteristics"][0]
    assert row["value"] == ("16:9" if confirmed else None)
    assert row["status"] == ("accepted" if confirmed else "rejected")
    assert seen[0]["questions"]["f0"]["instructions"]["claim"]["value"] == "16:9"
    assert text[row["evidence"]["start"]:row["evidence"]["end"]] == row["quote"]


def test_hammer_handle_material_uses_schema_option_and_component_evidence(monkeypatch):
    text = "Молоток слесарный 500 кг, боёк стальной круглый, рукоятка деревянная. Для слесарных и монтажных работ."
    mock_providers(monkeypatch, llm_handler=mapping_handler(
        {"0": "Дерево"}, {"0": "рукоятка деревянная"},
    ))
    row = asyncio.run(autofill.autofill_characteristics(request(
        text=text, additionalAllowed=False, characteristics=[{
            "id": "handle", "name": "Материал рукоятки", "options": ["Резина", "Дерево", "Стекловолокно"],
        }],
    )))["characteristics"][0]
    assert row["status"] == "accepted" and row["value"] == "Дерево"
    assert row["quote"] == "рукоятка деревянная"
