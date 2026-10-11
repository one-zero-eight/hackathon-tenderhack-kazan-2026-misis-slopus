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
        prompt = body["messages"][0]["content"]
        if "три необязательных реквизита" in prompt:
            assert body["response_format"]["type"] == "json_object"
            content = {
                "model": None,
                "manufacturer": None,
                "article": "AB-12",
            }
        else:
            assert body["response_format"]["type"] == "json_object"
            content = {"handle": "Дерево", "mass": "500 г"}
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}],
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


def test_autofill_logs_stages_and_total(monkeypatch, caplog):
    import logging

    mock_providers(monkeypatch)
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        asyncio.run(autofill.autofill_characteristics(request()))
    for stage in ("llm_schema", "llm_product", "jev_verify"):
        assert f"stage={stage} duration_ms=" in caplog.text
    assert "finished duration_ms=" in caplog.text
    assert "validation" in caplog.text
    assert "Молоток 500 г" not in caplog.text


def test_llm_extraction_stages_overlap(monkeypatch):
    mock_providers(monkeypatch)
    schema_started = asyncio.Event()
    product_started = asyncio.Event()

    async def schema(*args, **kwargs):
        schema_started.set()
        await asyncio.wait_for(product_started.wait(), timeout=1)
        return autofill.Extraction(fields=[], additional=[])

    async def product(*args, **kwargs):
        product_started.set()
        await asyncio.wait_for(schema_started.wait(), timeout=1)
        return autofill.product_missing()

    monkeypatch.setattr(autofill, "extract_schema", schema)
    monkeypatch.setattr(autofill, "extract_product", product)
    asyncio.run(autofill.autofill_characteristics(request()))


@pytest.mark.parametrize("disconnect", [False, True])
def test_parallel_extraction_cancels_pending_sibling(monkeypatch, disconnect):
    mock_providers(monkeypatch)
    product_started = asyncio.Event()
    product_cancelled = asyncio.Event()

    async def schema(*args, **kwargs):
        await product_started.wait()
        if disconnect:
            await asyncio.Future()
        raise autofill.AutofillError("schema failed")

    async def product(*args, **kwargs):
        product_started.set()
        try:
            await asyncio.Future()
        finally:
            product_cancelled.set()

    monkeypatch.setattr(autofill, "extract_schema", schema)
    monkeypatch.setattr(autofill, "extract_product", product)

    async def run():
        task = asyncio.create_task(autofill.autofill_characteristics(request()))
        await asyncio.wait_for(product_started.wait(), timeout=1)
        if disconnect:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if disconnect else autofill.AutofillError):
            await asyncio.wait_for(task, timeout=1)
        assert product_cancelled.is_set()

    asyncio.run(run())


def test_autofill_logs_failed_stage(monkeypatch, caplog):
    import logging

    def fail(req):
        return httpx.Response(503)

    mock_providers(monkeypatch, llm_handler=fail)
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        with pytest.raises(autofill.AutofillError):
            asyncio.run(autofill.autofill_characteristics(request()))
    assert "stage=llm_schema" in caplog.text
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
        payload = json.loads(body["messages"][1]["content"])
        assert "schema" in payload and "text" in payload
        assert set(payload["schema"]) == {"0", "1"}
        assert "spgz:" not in json.dumps(payload["schema"])
        assert "selected_schema" not in payload
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"0":"Дерево","1":"500 г"}'}}]})

    mock_providers(monkeypatch, llm_handler=llm_handler)

    async def extract():
        async with autofill.httpx.AsyncClient() as client:
            return await autofill.extract_schema(client, "Молоток 500 г, рукоятка деревянная", request().characteristics,
                additional_allowed=False, recommended=[], standards=[], usage={"input_tokens": 0, "output_tokens": 0})

    extraction = asyncio.run(extract())
    assert calls == ["json_object"]
    assert {item.field_id for item in extraction.fields} == {"handle", "mass"}


def test_map_to_extraction_grounds_number_unit_and_enum_stem():
    body = request()
    extraction = autofill.map_to_extraction(
        {"handle": "Дерево", "mass": "500 г"},
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
        body = json.loads(req.content)
        if "три необязательных реквизита" in body["messages"][0]["content"]:
            content = {"model": None, "manufacturer": None, "article": None}
        else:
            content = {"mass": "10 т"}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]})

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
        body = json.loads(req.content)
        if "три необязательных реквизита" in body["messages"][0]["content"]:
            content = {"model": None, "manufacturer": None, "article": None}
        else:
            content = {"handle": "Пластик"}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]})

    mock_providers(monkeypatch, llm_handler=llm_handler)
    result = asyncio.run(autofill.autofill_characteristics(request(
        text="Молоток стальной",
        characteristics=[{"id": "handle", "name": "Рукоятка", "options": ["Дерево", "Пластик"]}],
    )))
    assert result["characteristics"][0]["status"] == "rejected"
    assert "цитата" in result["characteristics"][0]["errors"][0]


def test_multiple_values_are_joined(monkeypatch):
    def llm_handler(req):
        body = json.loads(req.content)
        if "три необязательных реквизита" in body["messages"][0]["content"]:
            content = {"model": None, "manufacturer": None, "article": None}
        else:
            content = {"colors": ["Красный", "Синий"]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]})

    mock_providers(monkeypatch, llm_handler=llm_handler)
    result = asyncio.run(autofill.autofill_characteristics(request(
        text="Красный и Синий",
        characteristics=[{"id": "colors", "name": "Цвета", "multiple": True, "options": ["Красный", "Белый", "Синий"]}],
    )))
    assert result["characteristics"][0]["value"] == "Красный · Синий"
    assert result["characteristics"][0]["status"] == "accepted"


def test_unitless_number_with_physical_unit_is_rejected(monkeypatch):
    def llm_handler(req):
        body = json.loads(req.content)
        if "три необязательных реквизита" in body["messages"][0]["content"]:
            content = {"model": None, "manufacturer": None, "article": None}
        else:
            content = {"width": "450 мм"}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]})

    mock_providers(monkeypatch, llm_handler=llm_handler)
    result = asyncio.run(autofill.autofill_characteristics(request(
        text="Ширина 450 мм",
        characteristics=[{"id": "width", "name": "Ширина", "numeric": True}],
    )))
    assert result["characteristics"][0]["status"] == "rejected"
    assert any("единица отсутствует" in error for error in result["characteristics"][0]["errors"])


def test_additional_fields_require_explicit_permission(monkeypatch):
    def llm_handler(req):
        body = json.loads(req.content)
        if "три необязательных реквизита" in body["messages"][0]["content"]:
            content = {"model": None, "manufacturer": None, "article": None}
        else:
            content = {"Цвет": "синий"}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]})

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
            "model": "ДА-10,8/2", "manufacturer": "Ставр", "article": "fabricated",
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


def test_product_rules_win_without_llm_product_call(monkeypatch):
    calls = []

    def llm_handler(req):
        body = json.loads(req.content)
        calls.append(body["messages"][0]["content"][:40])
        if "три необязательных реквизита" in body["messages"][0]["content"]:
            raise AssertionError("product LLM should be skipped when rules fill all fields")
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    mock_providers(monkeypatch, llm_handler=llm_handler)
    text = "Модель ДА-10, производитель Ставр, артикул AB-12"
    result = asyncio.run(autofill.autofill_characteristics(request(text=text, characteristics=[])))
    assert result["product"]["model"]["source"] == "rules"
    assert result["product"]["manufacturer"]["source"] == "rules"
    assert result["product"]["article"]["source"] == "rules"
    assert all("реквизита" not in prompt for prompt in calls)


def test_missing_required_field_marks_needs_review(monkeypatch):
    def llm_handler(req):
        body = json.loads(req.content)
        if "три необязательных реквизита" in body["messages"][0]["content"]:
            content = {"model": None, "manufacturer": None, "article": None}
        else:
            content = {}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content)}}]})

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
