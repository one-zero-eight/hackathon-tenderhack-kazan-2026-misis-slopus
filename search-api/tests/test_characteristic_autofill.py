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

    def default_llm(req):
        body = json.loads(req.content)
        assert "Authorization" not in req.headers
        assert req.headers["User-Agent"] == "zakupki-search-api/0.1"
        assert body["model"] == "ggml-org/gemma-4-E4B-it-GGUF:Q8_0"
        assert body["response_format"]["type"] == "json_schema"
        prompt = body["messages"][0]["content"]
        if "три необязательных реквизита" in prompt:
            content = {
                "model": None,
                "manufacturer": None,
                "article": {"value": "AB-12", "quote": "артикул AB-12"},
            }
        else:
            content = {
                "fields": [
                    {
                        "field_id": "handle", "value": "Дерево", "source_unit": None,
                        "source_literal": None, "quote": "рукоятка деревянная",
                    },
                    {
                        "field_id": "mass", "value": 500, "source_unit": "г",
                        "source_literal": "500", "quote": "Молоток 500 г",
                    },
                ],
                "additional": [],
            }
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
    assert rows["mass"]["evidence"]["quote"] == "Молоток 500 г"
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
            content = {"fields": [{
                "field_id": "mass", "value": 10, "source_unit": "т",
                "source_literal": "10", "quote": "Масса 10 т",
            }], "additional": []}
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
            content = {"fields": [{
                "field_id": "handle", "value": "Дерево", "source_unit": None,
                "source_literal": None, "quote": "этой цитаты нет в тексте",
            }], "additional": []}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]})

    mock_providers(monkeypatch, llm_handler=llm_handler)
    result = asyncio.run(autofill.autofill_characteristics(request(characteristics=[
        {"id": "handle", "name": "Рукоятка", "options": ["Дерево"]},
    ])))
    assert result["characteristics"][0]["status"] == "rejected"
    assert "цитата" in result["characteristics"][0]["errors"][0]


def test_multiple_values_are_joined(monkeypatch):
    def llm_handler(req):
        body = json.loads(req.content)
        if "три необязательных реквизита" in body["messages"][0]["content"]:
            content = {"model": None, "manufacturer": None, "article": None}
        else:
            content = {"fields": [
                {"field_id": "colors", "value": "Красный", "source_unit": None, "source_literal": None, "quote": "Красный и Синий"},
                {"field_id": "colors", "value": "Синий", "source_unit": None, "source_literal": None, "quote": "Красный и Синий"},
            ], "additional": []}
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
            content = {"fields": [{
                "field_id": "width", "value": 450, "source_unit": "мм",
                "source_literal": "450", "quote": "450 мм",
            }], "additional": []}
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
            content = {"fields": [], "additional": [{
                "name": "Цвет", "type": "text", "value": "синий",
                "source_unit": None, "source_literal": None, "quote": "цвет синий",
            }]}
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


def test_product_rules_win_without_llm_product_call(monkeypatch):
    calls = []

    def llm_handler(req):
        body = json.loads(req.content)
        calls.append(body["messages"][0]["content"][:40])
        if "три необязательных реквизита" in body["messages"][0]["content"]:
            raise AssertionError("product LLM should be skipped when rules fill all fields")
        content = {"fields": [], "additional": []}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content)}}]})

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
            content = {"fields": [], "additional": []}
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


def llm_value_handler(value, unit, literal, quote):
    def handler(req):
        body = json.loads(req.content)
        if "три необязательных реквизита" in body["messages"][0]["content"]:
            content = {"model": None, "manufacturer": None, "article": None}
        else:
            content = {"fields": [{"field_id": "size", "value": value, "source_unit": unit,
                                   "source_literal": literal, "quote": quote}], "additional": []}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]})
    return handler


def test_autofill_returns_scale_hint_for_rejected_value_but_never_fills_it(monkeypatch):
    mock_providers(monkeypatch, llm_handler=llm_value_handler(3, "м", "3", "Длина 3 м"))
    body = request(text="Длина 3 м", characteristics=[{
        "id": "size", "name": "Размер", "unit": "см", "numeric": True,
        "options": ["1–5"], "ranges": [{"option": "1–5", "min": 1, "max": 5}]}])
    row = asyncio.run(autofill.autofill_characteristics(body))["characteristics"][0]
    assert row["status"] == "rejected" and row["value"] is None  # значение не подставляется
    assert any("диапазон" in error for error in row["errors"])
    assert row["suggestion"]["unit"] == "см" and row["suggestion"]["converted"] == 3


def test_autofill_has_no_hint_when_nothing_fits(monkeypatch):
    mock_providers(monkeypatch, llm_handler=llm_value_handler(10, "т", "10", "Масса 10 т"))
    body = request(text="Масса 10 т", characteristics=[{
        "id": "size", "name": "Масса", "unit": "кг", "numeric": True,
        "options": ["0.1–2"], "ranges": [{"option": "0.1–2", "min": 0.1, "max": 2}]}])
    row = asyncio.run(autofill.autofill_characteristics(body))["characteristics"][0]
    assert row["status"] == "rejected" and row["suggestion"] is None


# --- модель без «размышлений»: числа строкой, дополнительные поля запроса ----------------------------------------------

def test_written_number_parses_strings_and_rejects_the_rest():
    assert autofill.written_number("1,8") == 1.8
    assert autofill.written_number(" 1 600 ".replace(" ", "")) == 1600
    assert autofill.written_number(2) == 2.0 and autofill.written_number(True) is None
    assert autofill.written_number("до 2") is None and autofill.written_number("nan") is None
    assert autofill.written_number(None) is None


@pytest.mark.parametrize("raw, literal, text, accepted", [
    ("1,8", "1,8", "Длина 1,8 м", True),      # число строкой с запятой
    ("1600", "1600", "Длина 1600 м", True),
    ("2,5", "1,8", "Длина 1,8 м", False),     # не совпадает с source_literal: проверка остаётся
    ("до 2", "2", "Длина до 2 м", False),     # не число
])
def test_numeric_value_returned_as_string_is_validated_not_trusted(monkeypatch, raw, literal, text, accepted):
    mock_providers(monkeypatch, llm_handler=llm_value_handler(raw, "м", literal, text))
    body = request(text=text, characteristics=[{
        "id": "size", "name": "Размер", "unit": "м", "numeric": True,
        "options": ["1–2000"], "ranges": [{"option": "1–2000", "min": 1, "max": 2000}]}])
    row = asyncio.run(autofill.autofill_characteristics(body))["characteristics"][0]
    assert (row["status"] == "accepted") is accepted
    if accepted:
        assert row["value"] == autofill.format_number(autofill.written_number(raw)) and row["errors"] == []


def captured_llm_bodies(monkeypatch):
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        content = {"model": None, "manufacturer": None, "article": None} \
            if "три необязательных реквизита" in body["messages"][0]["content"] else {"fields": [], "additional": []}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content)}}]})

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


def test_canonical_option_matches_case_and_yo_but_only_unambiguously():
    options = ["Черный", "Белый", "Серый"]
    assert autofill.canonical_option(options, "Чёрный") == "Черный"
    assert autofill.canonical_option(options, " белый ") == "Белый"
    assert autofill.canonical_option(options, "Чёрная") == "Черный"  # другая форма слова из текста запроса
    assert autofill.canonical_option(options, "Красный") is None
    assert autofill.canonical_option(["Серый", "серый"], "СЕРЫЙ") is None  # два варианта подходят — не угадываем


def test_enum_value_in_other_case_is_filled_with_the_schema_option(monkeypatch):
    def handler(req):
        body = json.loads(req.content)
        if "три необязательных реквизита" in body["messages"][0]["content"]:
            content = {"model": None, "manufacturer": None, "article": None}
        else:
            content = {"fields": [{"field_id": "color", "value": "Чёрный", "source_unit": None,
                                   "source_literal": None, "quote": "чёрная"}], "additional": []}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]})

    mock_providers(monkeypatch, llm_handler=handler)
    body = request(text="Мышь чёрная", characteristics=[{"id": "color", "name": "Цвет", "options": ["Черный", "Белый"]}])
    row = asyncio.run(autofill.autofill_characteristics(body))["characteristics"][0]
    assert row["status"] == "accepted" and row["value"] == "Черный" and row["errors"] == []
    assert row["quote"] == "чёрная"  # доказательство — слова запроса, как написал пользователь
