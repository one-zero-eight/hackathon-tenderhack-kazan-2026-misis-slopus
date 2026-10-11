"""Предупреждение о подозрительном масштабе значения: ревью-сигнал `warnings`, а не жёсткая ошибка."""
import asyncio

import pytest

from search_api import characteristic_autofill as autofill
from test_characteristic_autofill import mock_providers, quoted_fields, request


def mass_field(*ranges, unit="кг"):
    return {"id": "mass", "name": "Масса", "unit": unit, "numeric": True,
            "options": [r["option"] for r in ranges], "ranges": list(ranges)}


def autofill_row(monkeypatch, text, field, answer):
    mock_providers(monkeypatch)

    async def llm(client, prompt, payload, usage, **kwargs):
        if prompt == autofill.EXTRACT_PROMPT and answer is not None:
            return quoted_fields({"0": answer})
        return {}

    monkeypatch.setattr(autofill, "llm_chat", llm)
    result = asyncio.run(autofill.autofill_characteristics(request(text=text, characteristics=[field], additionalAllowed=False)))
    return result["characteristics"][0]


def characteristic(*ranges):
    return autofill.AutofillCharacteristic(
        id="mass", name="Масса", unit="кг", numeric=True, options=[r.option for r in ranges], ranges=list(ranges))


def span(option, low, high):
    return autofill.CharacteristicRange(option=option, min=low, max=high)


def test_suspicious_scale_problem_triggers_at_the_factor_and_not_below():
    field = characteristic(span("1–2", 1, 2))
    assert autofill.SUSPICIOUS_SCALE_FACTOR == 50
    message = autofill.suspicious_scale_problem(field, 100)  # ровно 50 × наибольшая граница
    assert message == ("Подозрительное значение 100 кг: в 50 раз больше наибольшей границы схемы 2 кг. "
                       "Проверьте число и единицу измерения.")
    assert autofill.suspicious_scale_problem(field, 99) is None  # 49,5 раза


@pytest.mark.parametrize("ranges", [
    [],                                              # нет диапазонов
    [autofill.CharacteristicRange(option="a")],      # границ нет вообще
    [autofill.CharacteristicRange(option="a", min=0, max=0)],  # граница не положительная
])
def test_suspicious_scale_problem_needs_a_positive_finite_boundary(ranges):
    field = autofill.AutofillCharacteristic(id="m", name="М", unit="кг", numeric=True, options=["a"] * bool(ranges),
                                            ranges=ranges)
    assert autofill.suspicious_scale_problem(field, 1e9) is None


def test_open_range_uses_its_lower_bound_as_the_highest_finite_boundary():
    field = characteristic(span("<1", None, 1), span("≥2", 2, None))
    assert "в 50 раз" in autofill.suspicious_scale_problem(field, 100)
    assert autofill.suspicious_scale_problem(field, 99.9) is None


def test_rejected_out_of_range_value_also_carries_the_scale_warning(monkeypatch):
    row = autofill_row(monkeypatch, "Масса 500 кг", mass_field({"option": "0.1–2", "min": 0.1, "max": 2}), "500 кг")
    assert row["status"] == "rejected" and row["value"] is None
    assert row["warnings"] == [
        "Подозрительное значение 500 кг: в 250 раз больше наибольшей границы схемы 2 кг. "
        "Проверьте число и единицу измерения."]


def test_accepted_value_in_an_open_range_keeps_status_and_gets_the_warning(monkeypatch):
    row = autofill_row(monkeypatch, "Масса 5000 кг", mass_field({"option": "≥ 1", "min": 1}), "5000 кг")
    assert row["status"] == "accepted" and row["value"] == "5000"  # предупреждение не отклоняет значение
    assert len(row["warnings"]) == 1 and "в 5000 раз" in row["warnings"][0]


def test_normal_value_has_empty_warnings(monkeypatch):
    row = autofill_row(monkeypatch, "Масса 1,5 кг", mass_field({"option": "0.1–2", "min": 0.1, "max": 2}), "1,5 кг")
    assert row["status"] == "accepted" and row["warnings"] == []


def test_field_without_ranges_has_empty_warnings(monkeypatch):
    unbounded = {"id": "mass", "name": "Масса", "unit": "кг", "numeric": True}
    assert autofill_row(monkeypatch, "Масса 500 кг", unbounded, "500 кг")["warnings"] == []


def test_missing_field_has_empty_warnings(monkeypatch):
    missing = autofill_row(monkeypatch, "Просто молоток", mass_field({"option": "0.1–2", "min": 0.1, "max": 2}), None)
    assert missing["status"] == "missing" and missing["warnings"] == []
