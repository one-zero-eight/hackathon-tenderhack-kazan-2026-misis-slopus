import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from search_api import characteristic_autofill as autofill
from search_api.config import settings
from search_api.main import app


def request(**kwargs):
    return autofill.AutofillRequest(
        text=kwargs.pop("text", "Молоток 500 г, рукоятка деревянная, артикул AB-12"),
        characteristics=kwargs.pop("characteristics", [
            {"id": "handle", "name": "Материал рукоятки", "options": ["Дерево", "Металл"], "required": True},
            {"id": "mass", "name": "Масса", "unit": "кг", "numeric": True,
             "options": ["< 1", "≥ 1"],
             "ranges": [{"option": "< 1", "max": 1, "maxInclusive": False}, {"option": "≥ 1", "min": 1}]},
        ]), **kwargs,
    )


def quoted_fields(mapping, quotes=None):
    """Build provider field objects with exact, test-authored evidence."""
    quotes = quotes or {}
    result = {}
    for key, value in mapping.items():
        if key == "product" or isinstance(value, dict):
            result[key] = value
        elif isinstance(value, list):
            evidence = quotes.get(key, value)
            result[key] = [
                {"value": item, "quote": quote}
                for item, quote in zip(value, evidence, strict=True)
            ]
        else:
            quote = quotes.get(key, "рукоятка деревянная" if value == "Дерево" else str(value))
            result[key] = {"value": value, "quote": quote}
    return result


def mock_providers(monkeypatch, *, llm_handler=None, jev_handler=None):
    monkeypatch.setattr(settings, "typesafe_api_key", SecretStr("test-secret"))
    monkeypatch.setattr(settings, "typesafe_confidence", 0.85)
    monkeypatch.setattr(settings, "openai_base_url", "https://api.innohassle.ru/llama/v1")
    monkeypatch.setattr(settings, "openai_model", "ggml-org/gemma-4-E4B-it-GGUF:Q8_0")
    async def no_web(*_args, **_kwargs):
        return None
    monkeypatch.setattr(autofill, "enrich_from_web", no_web)

    def default_llm(req):
        body = json.loads(req.content)
        assert "Authorization" not in req.headers
        assert req.headers["User-Agent"] == "zakupki-search-api/0.1"
        assert body["model"] == "ggml-org/gemma-4-E4B-it-GGUF:Q8_0"
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
        assert body["response_format"]["type"] == "json_object"
        content = {
            "0": "Дерево", "1": "500 г",
            "product": {
                "name": "Молоток 500 г, рукоятка деревянная, арт. AB-12",
                "article": "AB-12",
            },
        }
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(quoted_fields(content), ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        })

    def default_jev(req):
        body = json.loads(req.content)
        assert str(req.url).endswith("/v1/systemone")
        assert req.headers["Authorization"] == "Bearer test-secret"
        assert "source_text" in body["state"]
        answers = {
            qid: {"type": "noul", "noul": 0.95}
            for qid in body["questions"]
        }
        return httpx.Response(200, json={"model": "jev", "answers": answers, "usage": {"input_tokens": 3, "output_tokens": 1}})

    llm_handler = llm_handler or default_llm
    jev_handler = jev_handler or default_jev

    def router(req: httpx.Request):
        url = str(req.url)
        if url.endswith("/chat/completions"):
            return llm_handler(req)
        if url.endswith("/v1/systemone"):
            return jev_handler(req)
        raise AssertionError(url)

    client_class = httpx.AsyncClient
    monkeypatch.setattr(autofill.httpx, "AsyncClient", lambda **kwargs: client_class(
        **kwargs, transport=httpx.MockTransport(router),
    ))


def test_product_model_and_manufacturer_are_extracted_without_explicit_labels(monkeypatch):
    text = 'Дрель-шуруповерт аккумуляторная Ставр "ДА-10,8/2 (2,6 Л)"'

    def llm(req):
        body = json.loads(req.content)
        prompt = body["messages"][0]["content"]
        assert "product.name НЕ заменяет отдельные поля" in prompt
        assert "включая числа, запятые, дроби и скобки" in prompt
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({
                "product": {
                    "name": text,
                    "manufacturer": "Ставр",
                    "model": "ДА-10,8/2 (2,6 Л)",
                },
            }, ensure_ascii=False)}}],
        })

    mock_providers(monkeypatch, llm_handler=llm)
    result = asyncio.run(autofill.autofill_characteristics(request(
        text=text, characteristics=[], additionalAllowed=False,
    )))
    for key, expected in (("manufacturer", "Ставр"), ("model", "ДА-10,8/2 (2,6 Л)")):
        row = result["product"][key]
        assert row["status"] == "accepted"
        assert row["value"] == expected
        assert row["quote"] == expected
        assert text[row["evidence"]["start"]:row["evidence"]["end"]] == expected
    assert result["product"]["article"]["status"] == "missing"


def test_autofill_logs_stages_and_total(monkeypatch, caplog):
    import logging

    mock_providers(monkeypatch)
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        asyncio.run(autofill.autofill_characteristics(request()))
    for stage in ("llm_extract", "jev_verify", "web_enrichment"):
        assert f"stage={stage} duration_ms=" in caplog.text
    assert "finished duration_ms=" in caplog.text
    assert "validation" in caplog.text
    assert "Молоток 500 г" not in caplog.text


@pytest.mark.parametrize("additional_allowed", [False, None, True])
def test_autofill_extracts_product_once_and_reviews_allowed_additional_fields(monkeypatch, additional_allowed):
    calls = []

    def llm_handler(req):
        calls.append(json.loads(req.content))
        if "missing" in json.loads(calls[-1]["messages"][1]["content"]):
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            })
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({
                "0": {"value": "Дерево", "quote": "рукоятка деревянная"},
                "1": {"value": "500 г", "quote": "500 г"},
                "Цвет": {"value": "синий", "quote": "цвет синий"},
                "product": {"model": "M500", "manufacturer": "Bosch", "article": "AB-12"},
            })}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        })

    mock_providers(monkeypatch, llm_handler=llm_handler)
    result = asyncio.run(autofill.autofill_characteristics(request(
        text="Молоток Bosch M500, 500 г, рукоятка деревянная, цвет синий, арт. AB-12",
        additionalAllowed=additional_allowed,
    )))
    assert len(calls) == (2 if additional_allowed else 1)
    assert result["filled"] == 2
    assert result["product"]["model"]["value"] == "M500"
    assert result["product"]["manufacturer"]["value"] == "Bosch"
    assert result["product"]["article"]["source"] == "rules"
    assert result["usage"]["llm"] == (
        {"input_tokens": 20, "output_tokens": 10} if additional_allowed
        else {"input_tokens": 10, "output_tokens": 5}
    )
    assert [row["name"] for row in result["additional_fields"]] == (
        ["Цвет"] if additional_allowed else []
    )


def test_combined_extraction_is_cancelled_on_disconnect(monkeypatch):
    mock_providers(monkeypatch)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def llm(*args, **kwargs):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(autofill, "llm_chat", llm)

    async def run():
        task = asyncio.create_task(autofill.autofill_characteristics(request()))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert cancelled.is_set()

    asyncio.run(run())


def test_autofill_logs_failed_stage(monkeypatch, caplog):
    import logging

    def fail(req):
        return httpx.Response(503)

    mock_providers(monkeypatch, llm_handler=fail)
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        with pytest.raises(autofill.AutofillError):
            asyncio.run(autofill.autofill_characteristics(request()))
    assert "stage=llm_extract" in caplog.text
    assert "status=error" in caplog.text
    assert "finished duration_ms=" in caplog.text


def test_autofill_llm_jev_converts_units_and_returns_statuses(monkeypatch):
    mock_providers(monkeypatch)
    body = request()
    result = asyncio.run(autofill.autofill_characteristics(body))
    rows = {item["id"]: item for item in result["characteristics"]}
    assert result["status"] in {"accepted", "needs_review"}
    assert rows["handle"]["status"] == "accepted"
    assert rows["handle"]["value"] == "Дерево"
    assert rows["mass"]["status"] == "accepted"
    assert rows["mass"]["value"] == "0.5"
    assert rows["mass"]["rangeOption"] == "< 1"
    assert rows["mass"]["conversion"]["from"] == "г"
    assert rows["mass"]["evidence"]["quote"] == "500 г"
    assert rows["mass"]["evidence"]["source"] == "llm"
    assert body.text[rows["mass"]["evidence"]["start"]:rows["mass"]["evidence"]["end"]] == rows["mass"]["evidence"]["quote"]
    assert rows["mass"]["evidence"]["conversion"]["from"] == "г"
    assert rows["mass"]["evidence"]["conversion"]["to"] == "кг"
    assert rows["mass"]["evidence"]["conversion"]["result"] == 0.5
    assert result["product"]["article"]["status"] == "accepted"
    assert result["product"]["article"]["value"] == "AB-12"
    assert result["product"]["article"]["evidence"]["source"] == "rules"
    assert body.text[
        result["product"]["article"]["evidence"]["start"]:result["product"]["article"]["evidence"]["end"]
    ] == result["product"]["article"]["evidence"]["quote"]
    assert "optionProbabilities" not in result
    assert result["model"] == "ggml-org/gemma-4-E4B-it-GGUF:Q8_0"
    assert result["threshold"] == 0.85


@pytest.mark.parametrize("additional_allowed", [False, None, True])
def test_catalog_fields_are_extracted_by_id_without_constraining_examples(monkeypatch, additional_allowed):
    def llm_handler(req):
        body = json.loads(req.content)
        payload = json.loads(body["messages"][1]["content"])
        assert payload["schema"]["0"] == {"n": "Высота", "t": "number", "u": "мм"}
        assert payload["schema"]["1"] == {"n": "Материал", "t": "text"}
        content = {} if "missing" in payload else {
            "0": {"value": "1,8 м", "quote": "высота 1,8 м"},
            "1": {"value": "сталь", "quote": "материал сталь"},
        }
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(quoted_fields(content), ensure_ascii=False)}}]})

    mock_providers(monkeypatch, llm_handler=llm_handler)
    body = request(
        text="Шкаф, высота 1,8 м, материал сталь",
        characteristics=[
            {"id": "catalog:height", "name": "Высота", "source": "catalog",
             "numeric": True, "unit": "мм", "examples": ["750", "2000"]},
            {"id": "catalog:material", "name": "Материал", "source": "catalog", "examples": ["Дерево"]},
            {"id": "catalog:color", "name": "Цвет", "source": "catalog", "examples": ["Белый"]},
        ],
        additionalAllowed=additional_allowed,
    )
    result = TestClient(app).post("/characteristics/autofill", json=body.model_dump())
    assert result.status_code == 200
    result = result.json()
    rows = {item["id"]: item for item in result["characteristics"]}
    assert result["total"] == 3 and result["filled"] == 2
    assert rows["catalog:height"]["status"] == "accepted"
    assert rows["catalog:height"]["value"] == "1800"
    assert rows["catalog:material"]["status"] == "accepted"
    assert rows["catalog:material"]["value"] == "сталь"
    assert rows["catalog:color"]["status"] == "missing"
    assert result["additional_fields"] == []
    for field_id in ("catalog:height", "catalog:material"):
        evidence = rows[field_id]["evidence"]
        assert body.text[evidence["start"]:evidence["end"]] == evidence["quote"]


@pytest.mark.parametrize(("value", "confidence", "error"), [
    ("1800 мм", 0.84, "Jev"),
    ("1800 кг", 0.95, "несовместимые единицы"),
    ("2000 мм", 0.95, "цитата"),
])
def test_catalog_fields_still_require_verification_units_and_quotes(monkeypatch, value, confidence, error):
    def jev_handler(req):
        body = json.loads(req.content)
        return httpx.Response(200, json={"answers": {
            qid: {"type": "noul", "noul": confidence} for qid in body["questions"]
        }})

    mock_providers(monkeypatch, llm_handler=mapping_handler({"0": value}), jev_handler=jev_handler)
    result = asyncio.run(autofill.autofill_characteristics(request(
        text="Шкаф, высота 1800 мм, масса 1800 кг",
        characteristics=[{"id": "catalog:height", "name": "Высота", "source": "catalog",
                          "numeric": True, "unit": "мм"}],
        additionalAllowed=False,
    )))
    row = result["characteristics"][0]
    assert row["id"] == "catalog:height"
    assert row["status"] == "rejected" and row["value"] is None
    assert any(error in message for message in row["errors"])


def test_compact_schema_keeps_only_minimal_field_tokens():
    body = request()
    schema = autofill.compact_schema_for_llm(body.characteristics)
    assert schema["mass"] == {"n": "Масса", "t": "number", "u": "кг"}
    assert schema["handle"] == {"n": "Материал рукоятки", "t": "enum", "v": ["Дерево", "Металл"]}
    assert "ranges" not in json.dumps(schema)
    assert "hint" not in json.dumps(schema)


def test_extraction_uses_json_object_once_with_short_keys(monkeypatch):
    calls = []

    def llm_handler(req):
        body = json.loads(req.content)
        calls.append(body["response_format"]["type"])
        prompt = body["messages"][0]["content"]
        assert '"k":' not in prompt
        assert '"0":{"value":"500 г","quote":"Молоток 500 г"}' in prompt
        assert '"1":{"value":"деревянная","quote":"рукоятка деревянная"}' in prompt
        assert '"product":{"name":' in prompt
        payload = json.loads(body["messages"][1]["content"])
        assert "schema" in payload and "text" in payload
        assert set(payload["schema"]) == {"0", "1"}
        assert "spgz:" not in json.dumps(payload["schema"])
        assert "selected_schema" not in payload
        return mapping_handler({"0": "Дерево", "1": "500 г"})(req)

    mock_providers(monkeypatch, llm_handler=llm_handler)

    async def extract():
        async with autofill.httpx.AsyncClient() as client:
            return await autofill.extract_characteristics_and_product(
                client, "Молоток 500 г, рукоятка деревянная", request().characteristics,
                additional_allowed=False, usage={"input_tokens": 0, "output_tokens": 0})

    extraction, product = asyncio.run(extract())
    assert all(row["status"] == "missing" for row in product.values())
    assert calls == ["json_object"]
    assert {item.field_id for item in extraction.fields} == {"handle", "mass"}


def test_map_to_extraction_preserves_supplied_number_and_enum_quotes():
    body = request()
    extraction = autofill.map_to_extraction(
        quoted_fields({"handle": "Дерево", "mass": "500 г"}),
        body.characteristics,
        body.text,
        additional_allowed=False,
    )
    by_id = {item.field_id: item for item in extraction.fields}
    assert by_id["mass"].value == 500
    assert by_id["mass"].source_unit == "г"
    assert by_id["mass"].source_literal == "500"
    assert by_id["mass"].quote == "500 г"
    assert by_id["handle"].value == "Дерево"
    assert "дерев" in by_id["handle"].quote.casefold()


def test_ground_quote_prefers_occurrence_with_literal():
    text = "масса 10 кг; масса 500 г"
    start, end = autofill.ground_quote(text, "масса 500 г", literal="500")
    assert text[start:end] == "масса 500 г"
    assert text[start:end].find("500") >= 0


def test_rejects_out_of_range_after_conversion(monkeypatch):
    def llm_handler(req):
        content = {"0": "10 т"}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(quoted_fields(content), ensure_ascii=False)}}]})

    mock_providers(monkeypatch, llm_handler=llm_handler)
    body = request(text="Масса 10 т", characteristics=[{
        "id": "mass", "name": "Масса", "unit": "кг", "numeric": True,
        "options": ["0.1–2"], "ranges": [{"option": "0.1–2", "min": 0.1, "max": 2}],
    }])
    result = asyncio.run(autofill.autofill_characteristics(body))
    row = result["characteristics"][0]
    assert row["status"] == "rejected"
    assert row["value"] is None
    assert any("диапазон" in error for error in row["errors"])


def test_rejects_below_jev_threshold(monkeypatch):
    def jev_handler(req):
        body = json.loads(req.content)
        answers = {qid: {"type": "noul", "noul": 0.84} for qid in body["questions"]}
        return httpx.Response(200, json={"answers": answers})

    mock_providers(monkeypatch, jev_handler=jev_handler)
    result = asyncio.run(autofill.autofill_characteristics(request()))
    rows = {item["id"]: item for item in result["characteristics"]}
    assert rows["handle"]["status"] == "rejected"
    assert any("Jev" in error for error in rows["handle"]["errors"])


def test_accepts_threshold_boundary(monkeypatch):
    def jev_handler(req):
        body = json.loads(req.content)
        answers = {qid: {"type": "noul", "noul": 0.85} for qid in body["questions"]}
        return httpx.Response(200, json={"answers": answers})

    mock_providers(monkeypatch, jev_handler=jev_handler)
    result = asyncio.run(autofill.autofill_characteristics(request()))
    rows = {item["id"]: item for item in result["characteristics"]}
    assert rows["handle"]["status"] == "accepted"
    assert rows["mass"]["status"] == "accepted"


@pytest.mark.parametrize("payload", [
    {"answers": {}},
    {"answers": {"f0": {"type": "choice", "noul": 0.9}}},
    {"answers": {"f0": {"type": "noul", "noul": True}}},
    {"answers": {"f0": {"type": "noul", "noul": 1.2}}},
    {"answers": {"f0": {"type": "noul"}}},
])
def test_invalid_jev_answers_are_rejected(monkeypatch, payload):
    def jev_handler(req):
        body = json.loads(req.content)
        answers = dict(payload.get("answers") or {})
        # Keep shape incomplete relative to requested questions.
        if answers and set(answers) != set(body["questions"]):
            # Force incomplete/invalid envelope for the first question id.
            first = next(iter(body["questions"]))
            answers = {first: next(iter(answers.values()))}
        return httpx.Response(200, json={"answers": answers})

    mock_providers(monkeypatch, jev_handler=jev_handler)
    with pytest.raises(autofill.AutofillError):
        asyncio.run(autofill.autofill_characteristics(request(characteristics=[
            {"id": "handle", "name": "Рукоятка", "options": ["Дерево"]},
        ])))


def test_fabricated_quote_is_rejected(monkeypatch):
    def llm_handler(req):
        content = {"0": {"value": "Пластик", "quote": "рукоятка пластиковая"}}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(quoted_fields(content), ensure_ascii=False)}}]})

    mock_providers(monkeypatch, llm_handler=llm_handler)
    result = asyncio.run(autofill.autofill_characteristics(request(
        text="Молоток стальной",
        characteristics=[{"id": "handle", "name": "Рукоятка", "options": ["Дерево", "Пластик"]}],
    )))
    assert result["characteristics"][0]["status"] == "rejected"
    assert "цитата" in result["characteristics"][0]["errors"][0]


def test_multiple_values_are_joined(monkeypatch):
    def llm_handler(req):
        content = {"0": ["Красный", "Синий"]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(quoted_fields(content), ensure_ascii=False)}}]})

    mock_providers(monkeypatch, llm_handler=llm_handler)
    result = asyncio.run(autofill.autofill_characteristics(request(
        text="Красный и Синий",
        characteristics=[{"id": "colors", "name": "Цвета", "multiple": True, "options": ["Красный", "Белый", "Синий"]}],
    )))
    assert result["characteristics"][0]["value"] == "Красный · Синий"
    assert result["characteristics"][0]["status"] == "accepted"


def test_unitless_number_with_physical_unit_is_rejected(monkeypatch):
    def llm_handler(req):
        content = {"0": "450 мм"}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(quoted_fields(content), ensure_ascii=False)}}]})

    mock_providers(monkeypatch, llm_handler=llm_handler)
    result = asyncio.run(autofill.autofill_characteristics(request(
        text="Ширина 450 мм",
        characteristics=[{"id": "width", "name": "Ширина", "numeric": True}],
    )))
    assert result["characteristics"][0]["status"] == "rejected"
    assert any("единица отсутствует" in error for error in result["characteristics"][0]["errors"])


def test_additional_fields_require_explicit_permission(monkeypatch):
    def llm_handler(req):
        content = {"Цвет": "синий"}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(quoted_fields(content), ensure_ascii=False)}}]})

    mock_providers(monkeypatch, llm_handler=llm_handler)
    denied = asyncio.run(autofill.autofill_characteristics(request(
        text="цвет синий", characteristics=[], additionalAllowed=False,
    )))
    assert denied["additional_fields"] == []
    assert denied["additional_allowed"] is False

    unknown = asyncio.run(autofill.autofill_characteristics(request(
        text="цвет синий", characteristics=[], additionalAllowed=None,
    )))
    assert unknown["additional_fields"] == []
    assert unknown["additional_allowed"] is None

    allowed = asyncio.run(autofill.autofill_characteristics(request(
        text="цвет синий", characteristics=[], additionalAllowed=True,
    )))
    assert allowed["additional_fields"][0]["status"] == "accepted"
    assert allowed["additional_fields"][0]["value"] == "синий"


def test_product_json_mapping_is_grounded_without_generated_quotes(monkeypatch):
    calls = []

    def llm_handler(req):
        body = json.loads(req.content)
        calls.append(body)
        assert body["response_format"] == {"type": "json_object"}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "product": {"model": "ДА-10,8/2", "manufacturer": "Ставр", "article": "fabricated"},
        }, ensure_ascii=False)}}]})

    mock_providers(monkeypatch, llm_handler=llm_handler)
    body = request(text="Дрель Ставр ДА-10,8/2", characteristics=[])
    result = asyncio.run(autofill.autofill_characteristics(body))
    assert len(calls) == 1
    product = result["product"]
    assert product["model"]["value"] == "ДА-10,8/2"
    assert product["manufacturer"]["value"] == "Ставр"
    assert product["article"]["status"] == "rejected"
    assert product["article"]["value"] is None
    for key in ("model", "manufacturer"):
        evidence = product[key]["evidence"]
        assert body.text[evidence["start"]:evidence["end"]] == evidence["quote"]


def test_product_rules_win_over_combined_llm_values(monkeypatch):
    mock_providers(monkeypatch, llm_handler=mapping_handler({
        "0": "Дерево", "1": "500 г",
        "product": {"model": "wrong", "manufacturer": "wrong", "article": "wrong"},
    }))
    result = asyncio.run(autofill.autofill_characteristics(request(
        text="Модель ДА-10, производитель Ставр, артикул AB-12; молоток 500 г, рукоятка деревянная",
    )))
    assert result["filled"] == 2
    for key, value in (("model", "ДА-10"), ("manufacturer", "Ставр"), ("article", "AB-12")):
        assert result["product"][key]["value"] == value
        assert result["product"][key]["source"] == "rules"


@pytest.mark.parametrize("product", [None, "model", []])
def test_invalid_product_envelope_is_rejected(monkeypatch, product):
    mock_providers(monkeypatch, llm_handler=mapping_handler({"product": product}))
    with pytest.raises(autofill.AutofillError, match="некорректные реквизиты"):
        asyncio.run(autofill.autofill_characteristics(request()))


def test_product_rules_win_and_llm_still_generates_name(monkeypatch):
    calls = []

    def llm_handler(req):
        body = json.loads(req.content)
        calls.append(body)
        content = {"product": {
            "name": "Дрель Ставр ДА-10, арт. AB-12",
            "model": "fabricated", "manufacturer": "fabricated", "article": "fabricated",
        }}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(quoted_fields(content))}}]})

    mock_providers(monkeypatch, llm_handler=llm_handler)
    text = "Дрель, модель ДА-10, производитель Ставр, артикул AB-12"
    result = asyncio.run(autofill.autofill_characteristics(request(text=text, characteristics=[])))
    assert len(calls) == 1
    assert result["product"]["name"]["value"] == "Дрель Ставр ДА-10, арт. AB-12"
    assert result["product"]["name"]["source"] == "llm_jev"
    for key, value in (("model", "ДА-10"), ("manufacturer", "Ставр"), ("article", "AB-12")):
        assert result["product"][key]["source"] == "rules"
        assert result["product"][key]["value"] == value


def test_generated_product_name_can_reorder_words_without_fabricating_evidence(monkeypatch):
    generated = "Бумага офисная Снегурочка, А4, 80 г/м², 500 листов"
    text = "Снегурочка: бумага офисная. Плотность 80 г/м², формат А4, в пачке 500 листов."

    def llm_handler(req):
        body = json.loads(req.content)
        prompt = body["messages"][0]["content"]
        assert "наименование СТЕ" in prompt
        assert json.loads(body["messages"][1]["content"]) == {"text": text, "schema": {}}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"product": {"name": generated}})}}]})

    def jev_handler(req):
        body = json.loads(req.content)
        assert body["state"]["source_text"] == text
        assert set(body["questions"]) == {"product_name"}
        assert body["questions"]["product_name"]["instructions"]["claim"] == generated
        return httpx.Response(200, json={"answers": {"product_name": {"type": "noul", "noul": 0.95}}})

    mock_providers(monkeypatch, llm_handler=llm_handler, jev_handler=jev_handler)
    result = asyncio.run(autofill.autofill_characteristics(request(text=text, characteristics=[])))
    name = result["product"]["name"]
    assert generated not in text
    assert name["value"] == generated
    assert name["status"] == "accepted"
    assert name["confidence"] == 0.95
    assert name["quote"] is None
    assert name["evidence"] is None


@pytest.mark.parametrize("invalid_name", ["", "  ", "Товар\nМодель", "Товар\rМодель", 42, True, {}])
def test_product_name_rejects_invalid_format_before_jev(monkeypatch, invalid_name):
    def llm_handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"product": {"name": invalid_name}})}}]})

    def jev_handler(req):
        raise AssertionError("Invalid name must not be sent to Jev")

    mock_providers(monkeypatch, llm_handler=llm_handler, jev_handler=jev_handler)
    result = asyncio.run(autofill.autofill_characteristics(request(characteristics=[])))
    assert result["product"]["name"]["status"] == "rejected"
    assert result["product"]["name"]["value"] is None
    assert result["status"] == "needs_review"


@pytest.mark.parametrize("length", [201, 1000])
def test_product_name_has_no_200_character_limit(monkeypatch, length):
    generated = "А" * length

    def llm_handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"product": {"name": generated}})}}]})

    mock_providers(monkeypatch, llm_handler=llm_handler)
    result = asyncio.run(autofill.autofill_characteristics(request(text=generated, characteristics=[])))
    assert result["product"]["name"]["status"] == "accepted"
    assert result["product"]["name"]["value"] == generated


def test_product_name_rejected_by_jev_does_not_discard_characteristics(monkeypatch):
    def jev_handler(req):
        body = json.loads(req.content)
        return httpx.Response(200, json={"answers": {
            key: {"type": "noul", "noul": 0.1 if key == "product_name" else 0.95}
            for key in body["questions"]
        }})

    mock_providers(monkeypatch, jev_handler=jev_handler)
    result = asyncio.run(autofill.autofill_characteristics(request()))
    assert result["product"]["name"]["status"] == "rejected"
    assert result["product"]["name"]["value"] is None
    assert result["product"]["name"]["errors"] == ["Jev не подтвердил наименование"]
    assert all(item["status"] == "accepted" for item in result["characteristics"])
    assert result["status"] == "needs_review"


def test_product_name_is_missing_when_llm_cannot_identify_product(monkeypatch):
    def llm_handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    mock_providers(monkeypatch, llm_handler=llm_handler)
    result = asyncio.run(autofill.autofill_characteristics(request(text="Модель ДА-10", characteristics=[])))
    assert result["product"]["name"]["status"] == "missing"
    assert result["product"]["name"]["value"] is None
    assert result["product"]["model"]["value"] == "ДА-10"

def test_missing_required_field_marks_needs_review(monkeypatch):
    def llm_handler(req):
        content = {}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(quoted_fields(content))}}]})

    mock_providers(monkeypatch, llm_handler=llm_handler)
    result = asyncio.run(autofill.autofill_characteristics(request(characteristics=[
        {"id": "handle", "name": "Рукоятка", "options": ["Дерево"], "required": True},
    ])))
    assert result["characteristics"][0]["status"] == "missing"
    assert result["status"] == "needs_review"
    assert any("обязательное" in issue for issue in result["issues"])


def test_schema_allows_empty_characteristics_and_rejects_blank_text():
    autofill.AutofillRequest(text="Только реквизиты", characteristics=[])
    with pytest.raises(ValidationError):
        request(text="  ")
    with pytest.raises(ValidationError):
        request(characteristics=[{"id": "same", "name": "A"}, {"id": "same", "name": "B"}])


def test_secret_file_and_environment_override(monkeypatch, tmp_path):
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "typesafe-token").write_text("file-token\n")
    monkeypatch.setattr(autofill, "ROOT", tmp_path)
    monkeypatch.setattr(settings, "typesafe_api_key", None)
    assert autofill.typesafe_key() == "file-token"
    monkeypatch.setattr(settings, "typesafe_api_key", SecretStr("env-token"))
    assert autofill.typesafe_key() == "env-token"
    monkeypatch.setattr(settings, "typesafe_api_key", None)
    monkeypatch.setattr(autofill, "ROOT", tmp_path / "missing")
    assert autofill.typesafe_key() == ""
    assert autofill.typesafe_headers("") == {}
    assert autofill.typesafe_headers("x") == {"Authorization": "Bearer x"}


def test_endpoint_reports_configuration_and_upstream_errors(monkeypatch):
    async def fail_config(body):
        raise autofill.AutofillConfigError("Не настроен ключ")

    monkeypatch.setattr("search_api.main.autofill_characteristics", fail_config)
    client = TestClient(app)
    assert client.post("/characteristics/autofill", json=request().model_dump()).status_code == 503

    async def fail_upstream(body):
        raise autofill.AutofillError("TypeSafe API: HTTP 429")

    monkeypatch.setattr("search_api.main.autofill_characteristics", fail_upstream)
    response = client.post("/characteristics/autofill", json=request().model_dump())
    assert response.status_code == 502
    assert response.json()["detail"] == "TypeSafe API: HTTP 429"


def test_range_boundaries():
    bucket = autofill.CharacteristicRange(option="bucket", min=0.5, max=1, maxInclusive=False)
    assert bucket.contains(0.5)
    assert not bucket.contains(1)


# --- подсказка по единицам ---------------------------------------------------------------------------------------------

def numeric_field(unit, low, high):
    return autofill.AutofillCharacteristic(
        id="size", name="Размер", unit=unit, numeric=True, options=[f"{low}–{high}"],
        ranges=[autofill.CharacteristicRange(option=f"{low}–{high}", min=low, max=high)])


def test_scale_suggestion_points_to_the_nearest_unit_that_fits():
    hint = autofill.scale_suggestion(numeric_field("см", 1, 5), 3, "м")  # 3 м = 300 см вне 1–5, а 3 см подходит
    assert hint["value"] == 3 and hint["unit"] == "см" and hint["converted"] == 3
    assert "3 см вместо 3 м" in hint["reason"] and "подтверждение" in hint["reason"]
    assert autofill.scale_suggestion(numeric_field("кг", 0.1, 2), 1, "т")["unit"] == "кг"
    assert set(hint) == {"value", "unit", "converted", "reason"}  # формат AutofillDiagnostics["suggestion"] фронтенда


@pytest.mark.parametrize("field, number, unit", [
    (numeric_field("см", 1, 5), 3, "см"),     # число уже допустимо: ошибка не в масштабе
    (numeric_field("кг", 0.1, 2), 10, "т"),   # ни одна единица той же величины не подходит
    (numeric_field("кг", 0.1, 2), 3, "м"),    # другая величина
    (numeric_field("см", 1, 5), 0, "м"),      # ноль: масштаб ни на что не влияет
    (numeric_field("см", 1, 5), 3, "штук"),   # неизвестная единица
    (numeric_field("", 1, 5), 3, "м"),        # у схемы нет единицы
])
def test_scale_suggestion_is_absent_when_scale_is_not_the_problem(field, number, unit):
    assert autofill.scale_suggestion(field, number, unit) is None


def test_scale_suggestion_needs_ranges():
    field = autofill.AutofillCharacteristic(id="size", name="Размер", unit="см", numeric=True)
    assert autofill.scale_suggestion(field, 3, "м") is None


def mapping_handler(mapping, quotes=None):
    """Return provider field objects, preserving product's string values."""
    def handler(req):
        content = quoted_fields(mapping, quotes)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]})
    return handler


def size_request(text, unit, low, high):
    return request(text=text, characteristics=[{
        "id": "size", "name": "Размер", "unit": unit, "numeric": True,
        "options": [f"{low}–{high}"], "ranges": [{"option": f"{low}–{high}", "min": low, "max": high}]}])


def test_autofill_returns_scale_hint_for_rejected_value_but_never_fills_it(monkeypatch):
    mock_providers(monkeypatch, llm_handler=mapping_handler({"size": "3 м"}))
    row = asyncio.run(autofill.autofill_characteristics(size_request("Длина 3 м", "см", 1, 5)))["characteristics"][0]
    assert row["status"] == "rejected" and row["value"] is None  # значение не подставляется
    assert any("диапазон" in error for error in row["errors"])
    assert row["suggestion"]["unit"] == "см" and row["suggestion"]["converted"] == 3


def test_autofill_has_no_hint_when_nothing_fits(monkeypatch):
    mock_providers(monkeypatch, llm_handler=mapping_handler({"size": "10 т"}))
    row = asyncio.run(autofill.autofill_characteristics(size_request("Масса 10 т", "кг", 0.1, 2)))["characteristics"][0]
    assert row["status"] == "rejected" and row["suggestion"] is None


# --- числа строкой и запятая, варианты перечня в другой форме слова, дополнительные поля запроса ------------------------

def test_written_number_parses_strings_and_rejects_the_rest():
    assert autofill.written_number("1,8") == 1.8 and autofill.written_number("1600") == 1600
    assert autofill.written_number(2) == 2.0 and autofill.written_number(True) is None
    assert autofill.written_number("до 2") is None and autofill.written_number("nan") is None
    assert autofill.written_number(None) is None


@pytest.mark.parametrize("raw, text, accepted", [
    ("1,8 м", "Длина 1,8 м", True),     # число с запятой и единицей одной строкой
    ("1600 м", "Длина 1600 м", True),
    ("1,8", "Длина 1,8 м", True),       # единицу «м» код берёт из предоставленной цитаты
    ("до 2 м", "Длина до 2 м", False),  # не число — поле не заполняется
])
def test_numeric_value_as_string_is_grounded_in_the_text(monkeypatch, raw, text, accepted):
    mock_providers(monkeypatch, llm_handler=mapping_handler({"size": raw}, {"size": text}))
    row = asyncio.run(autofill.autofill_characteristics(size_request(text, "м", 1, 2000)))["characteristics"][0]
    assert (row["status"] == "accepted") is accepted
    if accepted:
        assert row["value"] == autofill.format_number(autofill.written_number(raw.split()[0])) and row["errors"] == []


def test_canonical_option_matches_case_and_yo_but_only_unambiguously():
    options = ["Черный", "Белый", "Серый"]
    assert autofill.canonical_option(options, "Чёрный") == "Черный"
    assert autofill.canonical_option(options, " белый ") == "Белый"
    assert autofill.canonical_option(options, "Чёрная") == "Черный"  # другая форма слова из текста запроса
    assert autofill.canonical_option(options, "Красный") is None
    assert autofill.canonical_option(["Серый", "серый"], "СЕРЫЙ") is None  # два варианта подходят — не угадываем


def test_enum_value_in_other_form_is_filled_with_the_schema_option(monkeypatch):
    mock_providers(monkeypatch, llm_handler=mapping_handler({"color": "Чёрная"}, {"color": "чёрная"}))
    body = request(text="Мышь чёрная", characteristics=[{"id": "color", "name": "Цвет", "options": ["Черный", "Белый"]}])
    row = asyncio.run(autofill.autofill_characteristics(body))["characteristics"][0]
    assert row["status"] == "accepted" and row["value"] == "Черный" and row["errors"] == []
    assert row["quote"].casefold().startswith("чёрн")  # доказательство — слова запроса, как написал пользователь


def captured_llm_bodies(monkeypatch):
    seen = []
    inner = mapping_handler({})

    def handler(req):
        seen.append(json.loads(req.content))
        return inner(req)

    mock_providers(monkeypatch, llm_handler=handler)
    return seen


def test_thinking_is_disabled_for_the_llm_by_default(monkeypatch):
    seen = captured_llm_bodies(monkeypatch)
    asyncio.run(autofill.autofill_characteristics(request()))
    assert seen and all(body["chat_template_kwargs"] == {"enable_thinking": False} for body in seen)


def test_extra_body_is_configurable_and_cannot_override_the_request(monkeypatch):
    seen = captured_llm_bodies(monkeypatch)
    monkeypatch.setattr(settings, "openai_extra_body", {})
    asyncio.run(autofill.autofill_characteristics(request()))
    assert seen and all("chat_template_kwargs" not in body for body in seen)
    seen.clear()
    monkeypatch.setattr(settings, "openai_extra_body", {"model": "чужая", "temperature": 1, "top_k": 20})
    asyncio.run(autofill.autofill_characteristics(request()))
    assert all(body["model"] == settings.openai_model and body["temperature"] == 0 and body["top_k"] == 20 for body in seen)
