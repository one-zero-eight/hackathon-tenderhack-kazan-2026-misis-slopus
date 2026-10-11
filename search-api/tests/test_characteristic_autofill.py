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


@pytest.mark.parametrize("count_value", [500, "500 листов"])
def test_paper_grammage_and_sheets_are_accepted_with_ktru_units(monkeypatch, count_value):
    text = (
        "Бумага офисная для принтера и копира, формат А4, белая, "
        "плотность 80 г/м2, 500 листов в пачке, для ежедневной печати в канцелярии."
    )
    mock_providers(monkeypatch, llm_handler=mapping_handler(
        {"0": "80 г/м2", "1": count_value},
        {"0": "плотность 80 г/м2", "1": "500 листов в пачке"},
    ))
    result = asyncio.run(autofill.autofill_characteristics(request(
        text=text, additionalAllowed=False, characteristics=[
            {"id": "grammage", "name": "Масса бумаги площадью 1м2", "unit": "г",
             "options": ["≥80 … <90 г"],
             "ranges": [{"option": "≥80 … <90 г", "min": 80, "max": 90, "maxInclusive": False}]},
            {"id": "sheets", "name": "Количество листов в пачке", "unit": "шт.",
             "options": ["≥50 шт.", "≥100 шт.", "≥500 шт."],
             "ranges": [{"option": "≥50 шт.", "min": 50},
                        {"option": "≥100 шт.", "min": 100},
                        {"option": "≥500 шт.", "min": 500}]},
        ],
    )))
    rows = {row["id"]: row for row in result["characteristics"]}
    assert result["filled"] == 2
    assert rows["grammage"]["value"] == "80"
    assert rows["grammage"]["rangeOption"] == "≥80 … <90 г"
    assert rows["grammage"]["conversion"]["from"] == "г/м2"
    assert rows["sheets"]["value"] == "500"
    assert rows["sheets"]["rangeOption"] == "≥500 шт."
    for row in rows.values():
        assert row["status"] == "accepted"
        assert not row["errors"]
        assert text[row["evidence"]["start"]:row["evidence"]["end"]] == row["quote"]


@pytest.mark.parametrize("name, expected", [
    ("Масса бумаги площадью 1 м²", 80),
    ("Масса бумаги площадью 2м2", 160),
    ("Масса бумаги площадью 100 см²", 0.8),
    ("Масса бумаги", None),
    ("Масса бумаги площадью 0м2", None),
])
def test_density_to_mass_requires_explicit_reference_area(name, expected):
    field = autofill.AutofillCharacteristic(id="mass", name=name, numeric=True, unit="г")
    item = autofill.ExtractedValue(
        field_id="mass", value=80, source_unit="г/м²", source_literal="80", quote="плотность 80 г/м²",
    )
    value, errors, _ = autofill.validate_value(field, item, item.quote)
    if expected is None:
        assert "несовместимые единицы измерения" in errors
    else:
        assert not errors
        assert value == pytest.approx(expected)
    assert autofill.convert_units(80, "г/м²", "г")[1] == ["несовместимые единицы измерения"]


@pytest.mark.parametrize("raw_value", [1.8, "1.8", "1.8 м", "1,8 м"])
def test_mouse_cable_length_accepts_decimal_point_from_llm_for_source_comma(monkeypatch, raw_value):
    text = (
        "Мышь компьютерная проводная, оптическая, USB, кабель длиной 1,8 м, "
        "есть боковые кнопки и функция тихого клика, для офисных ноутбуков"
    )
    mock_providers(monkeypatch, llm_handler=mapping_handler(
        {"0": raw_value}, {"0": "кабель длиной 1,8 м"},
    ))
    result = asyncio.run(autofill.autofill_characteristics(request(
        text=text, additionalAllowed=False, characteristics=[
            {"id": "cable", "name": "Длина кабеля", "unit": "м", "numeric": True,
             "options": ["≥1.5 … <2 м"],
             "ranges": [{"option": "≥1.5 … <2 м", "min": 1.5, "max": 2, "maxInclusive": False}]},
        ],
    )))
    row = result["characteristics"][0]
    assert row["status"] == "accepted"
    assert row["value"] == "1.8"
    assert row["rangeOption"] == "≥1.5 … <2 м"
    assert not row["errors"]
    assert row["quote"] == "кабель длиной 1,8 м"
    assert text[row["evidence"]["start"]:row["evidence"]["end"]] == row["quote"]


@pytest.mark.parametrize("value, literal", [(1.7, "1.7"), (18, "18")])
def test_decimal_separator_normalization_does_not_accept_changed_number(value, literal):
    quote = "кабель длиной 1,8 м"
    field = autofill.AutofillCharacteristic(id="cable", name="Длина кабеля", numeric=True, unit="м")
    item = autofill.ExtractedValue(
        field_id="cable", value=value, source_unit="м", source_literal=literal, quote=quote,
    )
    _, errors, _ = autofill.validate_value(field, item, quote)
    assert "исходное число отсутствует в цитате" in errors


@pytest.mark.parametrize("with_units", [False, True])
def test_laptop_memory_cores_and_screen_units_are_grounded(monkeypatch, with_units):
    text = (
        "Ноутбук для офисной работы: экран 15,6 дюйма Full HD, "
        "оперативная память 16 ГБ, накопитель SSD 512 ГБ, процессор с 8 ядрами."
    )
    values = {"0": "16 ГБ", "1": "512 ГБ", "2": "8 ядрами", "3": "15,6 дюйма"} if with_units else {
        "0": 16, "1": 512, "2": 8, "3": 15.6,
    }
    quotes = {"0": "оперативная память 16 ГБ", "1": "накопитель SSD 512 ГБ",
              "2": "процессор с 8 ядрами", "3": "экран 15,6 дюйма"}
    fields = [
        {"id": "ram", "name": "Общий объем установленной оперативной памяти", "unit": "Гигабайт"},
        {"id": "ssd", "name": "Общий объем накопителей SSD", "unit": "Гигабайт"},
        {"id": "cores", "name": "Количество ядер процессора", "unit": "шт."},
        {"id": "screen", "name": "Диагональ экрана", "unit": "Дюйм (25,4 мм)"},
    ]
    for field, lower in zip(fields, [16, 500, 8, 15.6], strict=True):
        option = f"≥{lower} {field['unit']}"
        field.update(options=[option], ranges=[{"option": option, "min": lower}])
    mock_providers(monkeypatch, llm_handler=mapping_handler(values, quotes))
    result = asyncio.run(autofill.autofill_characteristics(request(
        text=text, additionalAllowed=False, characteristics=fields,
    )))
    assert result["filled"] == 4
    for row, expected in zip(result["characteristics"], ["16", "512", "8", "15.6"], strict=True):
        assert row["status"] == "accepted"
        assert row["value"] == expected
        assert not row["errors"]
        assert text[row["evidence"]["start"]:row["evidence"]["end"]] == row["quote"]


@pytest.mark.parametrize("source, target, value, expected", [
    ("ГБ", "Гигабайт", 16, 16),
    ("GB", "Гигабайт", 512, 512),
    ("МБ", "Мегабайт", 32, 32),
    ("ТБ", "Гигабайт", 1, 1000),
    ("ядрами", "шт.", 8, 8),
    ("ядер", "шт.", 8, 8),
    ("дюйма", "Дюйм (25,4 мм)", 15.6, 15.6),
    ("дюйм", "мм", 1, 25.4),
])
def test_computer_units_are_converted(source, target, value, expected):
    converted, errors = autofill.convert_units(value, source, target)
    assert not errors
    assert converted == pytest.approx(expected)


def test_memory_units_are_not_mass_or_unqualified_counts():
    assert autofill.convert_units(16, "ГБ", "г")[1] == ["несовместимые единицы измерения"]
    field = autofill.AutofillCharacteristic(id="cores", name="Количество ядер", numeric=True, unit="шт.")
    extraction = autofill.map_to_extraction(
        {"cores": {"value": 8, "quote": "процессор 8"}}, [field], "процессор 8", additional_allowed=False,
    )
    _, errors, _ = autofill.validate_value(field, extraction.fields[0], "процессор 8")
    assert "не указана исходная единица измерения" in errors


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


def test_confirmed_web_facts_fill_characteristics_without_changing_description(monkeypatch):
    text = "Молоток, рукоятка деревянная, артикул AB-12"
    source_text = f"{text}\n\nМасса: 500 г"

    def llm(req):
        body = json.loads(req.content)
        state = json.loads(body["messages"][1]["content"])
        assert state["text"] == source_text
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(quoted_fields({
                "0": "Дерево", "1": "500 г",
                "product": {"name": text, "article": "AB-12"},
            }), ensure_ascii=False)}}],
        })

    def jev(req):
        body = json.loads(req.content)
        assert body["state"]["source_text"] == source_text
        return httpx.Response(200, json={
            "model": "jev",
            "answers": {qid: {"type": "noul", "noul": 0.95} for qid in body["questions"]},
        })

    async def unexpected_web_search(*_args, **_kwargs):
        pytest.fail("Confirmed web facts must not trigger another internet search")

    mock_providers(monkeypatch, llm_handler=llm, jev_handler=jev)
    monkeypatch.setattr(autofill, "enrich_from_web", unexpected_web_search)
    body = request(text=text, webFacts=["Масса: 500 г"], additionalAllowed=False)
    result = asyncio.run(autofill.autofill_characteristics(body))
    mass = next(item for item in result["characteristics"] if item["id"] == "mass")
    assert mass["status"] == "accepted"
    assert mass["value"] == "0.5"
    assert mass["rangeOption"] == "< 1"
    assert body.text == text
    assert result["webEnrichment"] is None


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


@pytest.mark.parametrize("no_new_features,allowed", [(None, True), (False, True), (True, False)])
def test_ktru_additional_fields_are_allowed_unless_explicitly_forbidden(monkeypatch, no_new_features, allowed):
    from search_api import classifier_catalog

    monkeypatch.setattr(classifier_catalog, "get_ktru_record", lambda code: {"noNewFeatures": no_new_features})
    calls = []

    def llm_handler(req):
        payload = json.loads(json.loads(req.content)["messages"][1]["content"])
        calls.append(payload)
        assert payload.get("extra") == (1 if allowed else None)
        content = {} if "missing" in payload else {
            "Подсветка рабочей зоны": {"value": "Да", "quote": "подсветка рабочей зоны"},
        }
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}],
        })

    mock_providers(monkeypatch, llm_handler=llm_handler)
    result = asyncio.run(autofill.autofill_characteristics(request(
        text="Дрель, подсветка рабочей зоны", characteristics=[],
        ktruCode="25.73.30.141-00000011", additionalAllowed=not allowed,
    )))
    assert result["additional_allowed"] is allowed
    assert len(calls) == (2 if allowed else 1)
    assert [row["name"] for row in result["additional_fields"]] == (
        ["Подсветка рабочей зоны"] if allowed else []
    )
    if allowed:
        assert result["additional_fields"][0]["status"] == "accepted"


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


def test_jev_verification_does_not_send_unrelated_schema_or_all_enum_options(monkeypatch):
    seen = []

    def jev(req):
        payload = json.loads(req.content)
        seen.append(payload)
        assert payload["state"] == {"source_text": "Молоток 500 г"}
        instructions = payload["questions"]["f0"]["instructions"]
        assert instructions["field_name"] == "Масса"
        assert instructions["target_unit"] == "кг"
        assert instructions["field_hint"] == "Масса всего товара"
        assert instructions["claim"]["value"] == 500
        assert "Неизвлечённое поле" not in req.content.decode()
        assert len(req.content) < 6000
        return httpx.Response(200, json={
            "answers": {qid: {"type": "noul", "noul": 0.95} for qid in payload["questions"]},
        })

    mock_providers(monkeypatch, llm_handler=mapping_handler({"0": "500 г"}), jev_handler=jev)
    fields = [{"id": "mass", "name": "Масса", "numeric": True, "unit": "кг", "hint": "Масса всего товара"}]
    fields.extend({
        "id": f"unused:{i}", "name": f"Неизвлечённое поле {i}",
        "options": [f"Вариант {j}" for j in range(100)],
    } for i in range(39))
    result = asyncio.run(autofill.autofill_characteristics(request(
        text="Молоток 500 г", characteristics=fields, additionalAllowed=False,
    )))
    assert result["characteristics"][0]["value"] == "0.5"
    assert seen


def test_jev_http_error_preserves_provider_reason_in_logs_and_response(monkeypatch, caplog):
    import logging

    message = "A prompt has 11903 tokens, more than the maximum context length of 8192 tokens."
    mock_providers(monkeypatch, jev_handler=lambda req: httpx.Response(400, json={"error": {"message": message}}))
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        with pytest.raises(autofill.AutofillError, match="maximum context length of 8192"):
            asyncio.run(autofill.autofill_characteristics(request()))
    assert "failed: TypeSafe API: HTTP 400" in caplog.text
    assert message in caplog.text


def test_claim_question_includes_selected_value_label_not_the_entire_enum():
    field = autofill.AutofillCharacteristic(
        id="material", name="Материал", options=["01", "02"],
        optionLabels={"01": "Дерево", "02": "Металл"}, hint="Материал рукоятки",
    )
    extraction = autofill.Extraction(fields=[autofill.ExtractedValue(
        field_id="material", value="01", quote="деревянная", source_unit=None, source_literal=None,
    )], additional=[])
    instructions = autofill.build_claim_questions(extraction, [field])["f0"]["instructions"]
    assert instructions["value_label"] == "Дерево"
    assert instructions["field_hint"] == "Материал рукоятки"
    assert "Металл" not in json.dumps(instructions, ensure_ascii=False)


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
    calls = []

    def llm_handler(req):
        payload = json.loads(json.loads(req.content)["messages"][1]["content"])
        calls.append(payload)
        content = {} if "missing" in payload else {"0": ["Красный", "Синий"]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(quoted_fields(content), ensure_ascii=False)}}]})

    mock_providers(monkeypatch, llm_handler=llm_handler)
    result = asyncio.run(autofill.autofill_characteristics(request(
        text="Красный и Синий",
        characteristics=[{"id": "colors", "name": "Цвета", "multiple": True, "options": ["Красный", "Белый", "Синий"]}],
    )))
    assert result["characteristics"][0]["value"] == "Красный · Синий"
    assert result["characteristics"][0]["status"] == "accepted"
    assert len(calls) == 2
    assert calls[1]["missing"] == []
    assert calls[1]["already"]["fields"] == [
        {"id": "0", "value": "Красный", "quote": "Красный"},
        {"id": "0", "value": "Синий", "quote": "Синий"},
    ]


@pytest.mark.parametrize("text,name,value", [
    ("Ширина 450 мм", "Ширина", "450 мм"),
    ("Перфоратор Makita HR2450FT 780 Вт", "Мощность", "780 Вт"),
    ("Перфоратор, мощность 0,78 кВт", "Мощность", "0.78 кВт"),
])
def test_missing_schema_unit_keeps_explicit_unit_in_value(monkeypatch, text, name, value):
    source_value = value.replace("0.78", "0,78")
    mock_providers(monkeypatch, llm_handler=mapping_handler({"0": source_value}))
    result = asyncio.run(autofill.autofill_characteristics(request(
        text=text, additionalAllowed=False,
        characteristics=[{"id": "power", "name": name, "numeric": True, "source": "spgz"}],
    )))
    row = result["characteristics"][0]
    assert row["status"] == "accepted"
    assert row["value"] == value
    assert row["conversion"] is None
    assert text[row["evidence"]["start"]:row["evidence"]["end"]] == source_value


@pytest.mark.parametrize("constraints", [
    {"options": ["0–1000"], "ranges": [{"option": "0–1000", "min": 0, "max": 1000}]},
    {"options": ["780", "800"]},
])
def test_missing_unit_never_compares_physical_values_with_unitless_constraints(monkeypatch, constraints):
    mock_providers(monkeypatch, llm_handler=mapping_handler({"0": "780 Вт"}))
    row = asyncio.run(autofill.autofill_characteristics(request(
        text="Перфоратор Makita HR2450FT 780 Вт", additionalAllowed=False,
        characteristics=[{"id": "power", "name": "Мощность", "numeric": True, **constraints}],
    )))["characteristics"][0]
    assert row["status"] == "rejected"
    assert any("единица отсутствует" in error for error in row["errors"])


def test_claim_verification_includes_field_name_with_opaque_id():
    fields = [autofill.AutofillCharacteristic(
        id="spgz:1609061850", name="Мощность", numeric=True, source="spgz",
    )]
    extraction = autofill.map_to_extraction(
        {fields[0].id: {"value": "780 Вт", "quote": "780 Вт"}},
        fields, "Перфоратор Makita HR2450FT 780 Вт", additional_allowed=False,
    )
    instructions = autofill.build_claim_questions(extraction, fields)["f0"]["instructions"]
    assert instructions["field_name"] == "Мощность"
    assert instructions["claim"]["source_unit"] == "Вт"
    assert "field_name" in instructions["question"]
    assert "Отсутствие единицы" in instructions["question"]


@pytest.mark.parametrize("raw,source,target,expected,bucket", [
    ("20 см", "20 см", "м", "0.2", "<1"),
    ("0.2", "20 см", "м", "0.2", "<1"),
    ("0.2 м", "20 см", "м", "0.2", "<1"),
    ("200 мм", "200 мм", "м", "0.2", "<1"),
    ("200", "2 м", "см", "200", "≥2"),
    ("200 см", "2 м", "см", "200", "≥2"),
    ("2 м", "2 м", "см", "200", "≥2"),
    ("0,002 км", "0,002 км", "м", "2", "≥2"),
])
def test_cable_length_converts_both_scales_and_recovers_exact_llm_conversion(monkeypatch, raw, source, target, expected, bucket):
    text = f"Мышь компьютерная беспроводная Logitech M185, USB-приёмник, батарейка в комплекте. длина кабеля {source}"
    quote = f"длина кабеля {source}"

    def jev(req):
        payload = json.loads(req.content)
        claim = payload["questions"]["f0"]["instructions"]["claim"]
        assert claim["source_literal"] in quote
        assert claim["source_unit"] in quote
        assert claim["value"] == float(claim["source_literal"].replace(",", "."))
        return httpx.Response(200, json={"answers": {"f0": {"type": "noul", "noul": 0.95}}})

    mock_providers(monkeypatch, llm_handler=mapping_handler({"0": raw}, {"0": quote}), jev_handler=jev)
    row = asyncio.run(autofill.autofill_characteristics(request(
        text=text, additionalAllowed=False, characteristics=[{
            "id": "cable", "name": "Длина кабеля", "unit": target, "numeric": True,
            "options": ["<1", "≥1 … <2", "≥2"],
            "ranges": [{"option": "<1", "max": 1, "maxInclusive": False},
                       {"option": "≥1 … <2", "min": 1, "max": 2, "maxInclusive": False},
                       {"option": "≥2", "min": 2}],
        }],
    ))) ["characteristics"][0]
    assert row["status"] == "accepted", row["errors"]
    assert row["value"] == expected
    assert row["rangeOption"] == bucket
    assert row["conversion"]["to"] == target
    assert row["evidence"]["quote"] == quote
    assert row["evidence"]["conversion"]["result"] == float(expected)


@pytest.mark.parametrize("raw,quote", [
    ("0.3 м", "длина кабеля 20 см"),
    ("0.2 кг", "длина кабеля 20 см"),
    ("20 м", "длина кабеля 20 см"),
    ("0.2 м", "длина кабеля не менее 20 см"),
    ("0.2 м", "длина кабеля 20–30 см"),
    ("0.2 м", "длина кабеля 20 см, удлинитель 200 мм"),
])
def test_numeric_recovery_does_not_accept_wrong_units_values_limits_or_ambiguity(monkeypatch, raw, quote):
    mock_providers(monkeypatch, llm_handler=mapping_handler({"0": raw}, {"0": quote}))
    row = asyncio.run(autofill.autofill_characteristics(request(
        text=f"Мышь компьютерная; {quote}", additionalAllowed=False,
        characteristics=[{"id": "cable", "name": "Длина кабеля", "unit": "м", "numeric": True}],
    )))["characteristics"][0]
    assert row["status"] == "rejected"
    assert row["value"] is None


def test_power_supply_paraphrased_quote_is_replaced_with_grounded_evidence(monkeypatch):
    text = 'Дрель-шуруповерт аккумуляторная Ставр "ДА-10,8/2 (2,6 Л)"'
    calls = []

    def llm(req):
        payload = json.loads(json.loads(req.content)["messages"][1]["content"])
        calls.append(payload)
        if len(calls) == 1:
            quote = "От аккумулятора"
        else:
            assert payload["missing"] == ["0"]
            assert payload["already"]["fields"] == []
            quote = "аккумуляторная"
        return mapping_handler({"0": "От аккумулятора"}, {"0": quote})(req)

    mock_providers(monkeypatch, llm_handler=llm)
    row = asyncio.run(autofill.autofill_characteristics(request(
        text=text, additionalAllowed=False, characteristics=[{
            "id": "supply", "name": "Тип питания", "options": ["От сети", "От аккумулятора"],
        }],
    )))["characteristics"][0]
    assert len(calls) == 2
    assert row["status"] == "accepted" and row["value"] == "От аккумулятора"
    assert row["quote"] == "аккумуляторная"
    assert text[row["evidence"]["start"]:row["evidence"]["end"]] == row["quote"]


def test_additional_fields_require_explicit_permission(monkeypatch):
    def llm_handler(req):
        payload = json.loads(json.loads(req.content)["messages"][1]["content"])
        content = {} if "missing" in payload else {"Цвет": {"value": "синий", "quote": "цвет синий"}}
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


def test_compact_schema_keeps_hints_labels_and_all_40_options():
    options = [f"option-{index}" for index in range(40)]
    labels = {option: f"Описание {index}" for index, option in enumerate(options)}
    field = autofill.AutofillCharacteristic(
        id="kind", name="Тип", options=options, optionLabels=labels,
        hint="Тип именно рукоятки", multiple=True,
    )
    assert autofill.compact_schema_for_llm([field]) == {"kind": {
        "n": "Тип", "t": "enum", "v": options, "labels": labels,
        "h": "Тип именно рукоятки", "m": 1,
    }}


def test_equal_numbers_keep_explicit_field_context_in_evidence(monkeypatch):
    text = "Высота 1800 мм; ширина 1800 мм"
    mock_providers(monkeypatch, llm_handler=mapping_handler(
        {"0": "1800 мм", "1": "1800 мм"},
        {"0": "Высота 1800 мм", "1": "ширина 1800 мм"},
    ))
    result = asyncio.run(autofill.autofill_characteristics(request(
        text=text, additionalAllowed=False, characteristics=[
            {"id": "height", "name": "Высота", "numeric": True, "unit": "мм"},
            {"id": "width", "name": "Ширина", "numeric": True, "unit": "мм"},
        ],
    )))
    for row, quote in zip(result["characteristics"],
                          ["Высота 1800 мм", "ширина 1800 мм"], strict=True):
        assert row["status"] == "accepted"
        assert row["value"] == "1800"
        assert row["quote"] == quote
        evidence = row["evidence"]
        assert text[evidence["start"]:evidence["end"]] == quote
    assert result["characteristics"][0]["evidence"]["start"] != result["characteristics"][1]["evidence"]["start"]


def test_enum_synonym_uses_canonical_option_with_direct_quote(monkeypatch):
    mock_providers(monkeypatch, llm_handler=mapping_handler(
        {"0": "Нет"}, {"0": "без колёс"},
    ))
    body = request(text="Шкаф без колёс", additionalAllowed=False, characteristics=[
        {"id": "wheels", "name": "Наличие колёс", "options": ["Да", "Нет"]},
    ])
    row = asyncio.run(autofill.autofill_characteristics(body))["characteristics"][0]
    assert row["status"] == "accepted" and row["value"] == "Нет"
    assert row["quote"] == "без колёс"
    assert body.text[row["evidence"]["start"]:row["evidence"]["end"]] == "без колёс"


@pytest.mark.parametrize("value, quote", [
    ("1800 мм", "Высота 2000 мм"),
    ("180 мм", "Высота 1800 мм"),
    ("1800 мм", "Высота 900 мм"),
    ("1800 см", "Высота 1800 мм"),
    ("1800 мм", "Высота 1800"),
    ("12 мм", "Поправка -12 мм"),
])
def test_numeric_wrong_quote_substring_and_invented_unit_are_rejected(monkeypatch, value, quote):
    mock_providers(monkeypatch, llm_handler=mapping_handler({"0": value}, {"0": quote}))
    row = asyncio.run(autofill.autofill_characteristics(request(
        text="Высота 1800 мм; ширина 900 мм; Поправка -12 мм", additionalAllowed=False,
        characteristics=[{"id": "height", "name": "Высота", "numeric": True, "unit": "мм"}],
    )))["characteristics"][0]
    assert row["status"] == "rejected" and row["value"] is None
    assert row["errors"]


@pytest.mark.parametrize("quote", [
    "Длина не менее 120 см", "Длина не более 120 см", "Длина до 120 см",
    "Длина от 120 см", "Длина ≥ 120 см", "Длина <120 см",
    "Длина 120–150 см", "Длина около 120 см", "Длина 120 см ± 5 см",
])
def test_numeric_limits_are_not_exact_values(monkeypatch, quote):
    mock_providers(monkeypatch, llm_handler=mapping_handler({"0": "120 см"}, {"0": quote}))
    row = asyncio.run(autofill.autofill_characteristics(request(
        text=quote, additionalAllowed=False,
        characteristics=[{"id": "length", "name": "Длина", "numeric": True, "unit": "см"}],
    )))["characteristics"][0]
    assert row["status"] == "rejected" and row["value"] is None
    assert any("ограничение" in error for error in row["errors"])


@pytest.mark.parametrize("literal, value", [("-12", "-12"), ("+12", "12"), ("-1,8", "-1.8")])
def test_signed_numbers_are_grounded_without_losing_the_sign(monkeypatch, literal, value):
    text = f"Поправка {literal} мм"
    mock_providers(monkeypatch, llm_handler=mapping_handler({"0": f"{literal} мм"}, {"0": text}))
    row = asyncio.run(autofill.autofill_characteristics(request(
        text=text, additionalAllowed=False,
        characteristics=[{"id": "offset", "name": "Поправка", "numeric": True, "unit": "мм"}],
    )))["characteristics"][0]
    assert row["status"] == "accepted" and row["value"] == value
    assert row["quote"] == text


def test_scalar_field_mappings_are_not_supported():
    body = request()
    extraction = autofill.map_to_extraction(
        {"handle": "Дерево", "mass": "500 г", "Цвет": "синий"},
        body.characteristics, body.text, additional_allowed=True,
    )
    assert extraction.fields == [] and extraction.additional == []


def test_second_pass_recovers_only_missed_field_and_sums_usage(monkeypatch):
    calls = []

    def llm(req):
        body = json.loads(req.content)
        payload = json.loads(body["messages"][1]["content"])
        calls.append(payload)
        if len(calls) == 1:
            assert body["messages"][0]["content"] == autofill.EXTRACT_PROMPT
            content = quoted_fields({"0": "Дерево", "product": {"model": "M500"}})
        else:
            assert body["messages"][0]["content"] == autofill.COVERAGE_PROMPT
            assert payload["missing"] == ["1"]
            assert payload["already"] == {"fields": [
                {"id": "0", "value": "Дерево", "quote": "рукоятка деревянная"},
            ], "additional": []}
            content = quoted_fields({"1": "500 г"})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        })

    mock_providers(monkeypatch, llm_handler=llm)
    result = asyncio.run(autofill.autofill_characteristics(request(
        text="Молоток M500, 500 г, рукоятка деревянная", additionalAllowed=False,
    )))
    assert len(calls) == 2 and result["filled"] == 2
    assert all(row["status"] == "accepted" for row in result["characteristics"])
    assert result["product"]["model"]["value"] == "M500"
    assert result["usage"]["llm"] == {"input_tokens": 20, "output_tokens": 10}


def test_second_pass_deduplicates_multiple_and_additional_without_replacing_single(monkeypatch):
    calls = []
    text = "Рукоятка деревянная; корпус металлический; цвета красный и синий; отделка матовая"

    def llm(req):
        payload = json.loads(json.loads(req.content)["messages"][1]["content"])
        calls.append(payload)
        if len(calls) == 1:
            mapping = {"0": "Дерево", "1": ["Красный"], "Отделка": "матовая"}
            quotes = {"0": "Рукоятка деревянная", "1": ["цвета красный"], "Отделка": "отделка матовая"}
        else:
            assert payload["missing"] == []
            assert len(payload["already"]["fields"]) == 2
            assert payload["already"]["additional"][0]["name"] == "Отделка"
            mapping = {
                "0": "Металл", "1": ["Красный", "Синий"],
                " отделка ": "матовая", "Материал рукоятки": "Металл",
                "Корпус": "металлический", "product": {"model": "invented"},
            }
            quotes = {
                "0": "корпус металлический", "1": ["цвета красный", "синий"],
                " отделка ": "отделка матовая", "Материал рукоятки": "корпус металлический",
                "Корпус": "корпус металлический",
            }
        return mapping_handler(mapping, quotes)(req)

    mock_providers(monkeypatch, llm_handler=llm)
    body = request(text=text, additionalAllowed=True, characteristics=[
        {"id": "handle", "name": "Материал рукоятки", "options": ["Дерево", "Металл"]},
        {"id": "colors", "name": "Цвета", "multiple": True, "options": ["Красный", "Синий"]},
    ])
    result = asyncio.run(autofill.autofill_characteristics(body))
    assert len(calls) == 2
    rows = {row["id"]: row for row in result["characteristics"]}
    assert rows["handle"]["value"] == "Дерево"
    assert rows["colors"]["value"] == "Красный · Синий"
    assert [row["name"] for row in result["additional_fields"]] == ["Отделка", "Корпус"]
    assert result["product"]["model"]["status"] == "missing"


@pytest.mark.parametrize("failure", ["http", "non_object"])
def test_second_pass_errors_are_reported(monkeypatch, failure):
    calls = []

    def llm(req):
        calls.append(json.loads(req.content))
        if len(calls) == 1:
            return mapping_handler({"0": "Дерево"})(req)
        if failure == "http":
            return httpx.Response(503)
        return httpx.Response(200, json={"choices": [{"message": {"content": "[]"}}]})

    mock_providers(monkeypatch, llm_handler=llm)
    with pytest.raises(autofill.AutofillError):
        asyncio.run(autofill.autofill_characteristics(request(additionalAllowed=False)))
    assert len(calls) == 2


def test_second_pass_is_cancelled_on_disconnect(monkeypatch):
    mock_providers(monkeypatch)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def llm(client, prompt, payload, usage, **kwargs):
        if prompt == autofill.EXTRACT_PROMPT:
            return quoted_fields({"0": "Дерево"})
        assert prompt == autofill.COVERAGE_PROMPT
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(autofill, "llm_chat", llm)

    async def run():
        task = asyncio.create_task(autofill.autofill_characteristics(request(additionalAllowed=False)))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert cancelled.is_set()

    asyncio.run(run())
