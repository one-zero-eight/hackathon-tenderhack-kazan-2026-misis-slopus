"""Веб-обогащение идёт параллельно с LLM; evidence и цитаты относятся к описанию, а не к дописанным веб-фактам."""
import asyncio

import pytest

from search_api import characteristic_autofill as autofill
from test_characteristic_autofill import mock_providers, quoted_fields, request

MODEL_TEXT = "Молоток Stayer, модель M500, рукоятка деревянная"


def fake_enrichment(order, *, forever=False, on_cancel=None):
    """Подмена enrich_from_web: запись момента вызова = момент создания задачи (до её первого шага)."""
    def enrich(*_args, **_kwargs):
        order.append("web")

        async def run():
            try:
                if forever:
                    await asyncio.Future()
            finally:
                if on_cancel is not None:
                    on_cancel.append(True)
            return None

        return run()

    return enrich


def extract_only(order=None):
    async def llm(client, prompt, payload, usage, **kwargs):
        if order is not None:
            order.append("llm")
        return quoted_fields({"0": "Дерево"}) if prompt == autofill.EXTRACT_PROMPT else {}

    return llm


def test_web_enrichment_starts_before_llm_when_rules_already_know_the_model(monkeypatch):
    mock_providers(monkeypatch)
    order = []
    monkeypatch.setattr(autofill, "enrich_from_web", fake_enrichment(order))
    monkeypatch.setattr(autofill, "llm_chat", extract_only(order))
    asyncio.run(autofill.autofill_characteristics(request(text=MODEL_TEXT, additionalAllowed=False)))
    assert order[0] == "web" and "llm" in order  # поиск запущен до LLM-этапа
    assert order.count("web") == 1  # и не запускался повторно после него


def test_web_enrichment_waits_for_llm_when_no_model_is_known_yet(monkeypatch):
    mock_providers(monkeypatch)
    order = []
    monkeypatch.setattr(autofill, "enrich_from_web", fake_enrichment(order))
    monkeypatch.setattr(autofill, "llm_chat", extract_only(order))
    asyncio.run(autofill.autofill_characteristics(
        request(text="Молоток слесарный, рукоятка деревянная", additionalAllowed=False)))
    assert order[0] == "llm" and order.count("web") == 1  # модели нет: поиск, как раньше, после LLM


def test_early_web_enrichment_is_cancelled_when_llm_fails(monkeypatch):
    mock_providers(monkeypatch)
    cancelled, order = [], []

    async def llm(client, prompt, payload, usage, **kwargs):
        await asyncio.sleep(0)  # дать фоновому поиску начаться
        raise autofill.AutofillError("LLM недоступна")

    monkeypatch.setattr(autofill, "enrich_from_web", fake_enrichment(order, forever=True, on_cancel=cancelled))
    monkeypatch.setattr(autofill, "llm_chat", llm)
    with pytest.raises(autofill.AutofillError, match="LLM недоступна"):
        asyncio.run(autofill.autofill_characteristics(request(text=MODEL_TEXT, additionalAllowed=False)))
    assert order == ["web"] and cancelled == [True]  # фоновый запрос не остался висеть


def test_confirmed_web_facts_never_start_a_new_search(monkeypatch):
    mock_providers(monkeypatch)
    order = []
    monkeypatch.setattr(autofill, "enrich_from_web", fake_enrichment(order))
    monkeypatch.setattr(autofill, "llm_chat", extract_only())
    asyncio.run(autofill.autofill_characteristics(
        request(text=MODEL_TEXT, webFacts=["Масса: 500 г"], additionalAllowed=False)))
    assert order == []


def run_with_web_facts(monkeypatch, description):
    mock_providers(monkeypatch)

    async def llm(client, prompt, payload, usage, **kwargs):
        return quoted_fields({"0": "Дерево", "1": "500 г"}) if prompt == autofill.EXTRACT_PROMPT else {}

    monkeypatch.setattr(autofill, "llm_chat", llm)
    body = request(text=description, webFacts=["Масса: 500 г"], additionalAllowed=False)
    result = asyncio.run(autofill.autofill_characteristics(body))
    return {row["id"]: row for row in result["characteristics"]}


def test_evidence_of_a_web_fact_is_not_placed_into_the_description(monkeypatch):
    description = "Молоток, рукоятка деревянная"
    rows = run_with_web_facts(monkeypatch, description)
    handle, mass = rows["handle"], rows["mass"]
    evidence = handle["evidence"]
    assert description[evidence["start"]:evidence["end"]] == evidence["quote"] == "рукоятка деревянная"
    assert "fromWebFacts" not in handle
    # «500 г» есть только в веб-факте: значение принято, но подсвечивать в описании нечего
    assert mass["status"] == "accepted" and mass["value"] == "0.5"
    assert mass["quote"] == "500 г" and mass["evidence"] is None and mass["fromWebFacts"] is True


def test_quote_present_in_description_and_facts_is_located_in_the_description(monkeypatch):
    description = "Молоток 500 г, рукоятка деревянная"
    mass = run_with_web_facts(monkeypatch, description)["mass"]
    evidence = mass["evidence"]
    assert description[evidence["start"]:evidence["end"]] == evidence["quote"] == "500 г"
    assert "fromWebFacts" not in mass


def test_evidence_is_unchanged_without_web_facts(monkeypatch):
    mock_providers(monkeypatch)
    monkeypatch.setattr(autofill, "llm_chat", extract_only())
    description = "Молоток, рукоятка деревянная"
    result = asyncio.run(autofill.autofill_characteristics(request(text=description, additionalAllowed=False)))
    handle = next(row for row in result["characteristics"] if row["id"] == "handle")
    assert description[handle["evidence"]["start"]:handle["evidence"]["end"]] == "рукоятка деревянная"
    assert all("fromWebFacts" not in row for row in result["characteristics"])


def test_rebase_helper_handles_rows_without_quotes_and_foreign_quotes():
    rows = [
        {"quote": None, "evidence": None},
        {"quote": "нигде", "evidence": {"quote": "нигде", "start": 40, "end": 45, "source": "llm"}},
    ]
    autofill.rebase_evidence_to_description(rows, "Короткое описание", "Масса: 500 г")
    assert rows[0] == {"quote": None, "evidence": None}  # нечего подсвечивать — не трогаем
    assert rows[1]["evidence"] is None and "fromWebFacts" not in rows[1]  # нигде не найдена: без пометки
