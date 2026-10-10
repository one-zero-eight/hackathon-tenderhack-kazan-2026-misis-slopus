import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from search_api import characteristic_autofill as autofill
from search_api.config import settings
from search_api.main import app


def request(**kwargs):
    return autofill.AutofillRequest(
        text=kwargs.pop("text", "Молоток 500 г, рукоятка деревянная"),
        characteristics=kwargs.pop("characteristics", [
            {"id": "handle", "name": "Материал рукоятки", "options": ["Дерево", "Металл"]},
            {"id": "mass", "name": "Масса", "unit": "Килограмм", "options": ["< 1", "≥ 1"],
             "ranges": [{"option": "< 1", "max": 1, "maxInclusive": False}, {"option": "≥ 1", "min": 1}]},
        ]), **kwargs,
    )


def mock_typesafe(monkeypatch, handler):
    client_class = httpx.AsyncClient
    monkeypatch.setattr(autofill.httpx, "AsyncClient", lambda **kwargs: client_class(
        **kwargs, transport=httpx.MockTransport(handler),
    ))
    monkeypatch.setattr(settings, "typesafe_api_key", SecretStr("test-secret"))


def test_autofill_uses_direct_http_and_converts_units(monkeypatch):
    calls = []

    def handler(req):
        import json
        body = json.loads(req.content)
        calls.append(body)
        assert str(req.url) == "https://api.typesafe.ai/v1/systemone"
        assert req.headers["Authorization"] == "Bearer test-secret"
        assert body["model"] == "jev-latest"
        assert body["state"] == {"товар": "Молоток 500 г, рукоятка деревянная"}
        answers = {}
        for qid, question in body["questions"].items():
            choice = "Дерево" if qid == "c0" else next(label for label in question["criteria"] if label.startswith("500 г"))
            probabilities = dict.fromkeys(question["criteria"], 0.0)
            probabilities[choice] = 0.95
            probabilities[autofill.NONE] = 0.05
            answers[qid] = {"type": "choice", "choice": choice, "probabilities": probabilities}
        return httpx.Response(200, json={"model": "jev", "answers": answers, "usage": {"input_tokens": 20, "output_tokens": 2}})

    mock_typesafe(monkeypatch, handler)
    result = asyncio.run(autofill.autofill_characteristics(request()))
    assert len(calls) == 1
    assert result["model"] == "jev"
    assert result["usage"] == {"input_tokens": 20, "output_tokens": 2}
    rows = {item["id"]: item for item in result["characteristics"]}
    assert rows["handle"]["value"] == "Дерево"
    assert rows["mass"]["value"] == "0.5"
    assert rows["mass"]["rangeOption"] == "< 1"
    assert result["optionProbabilities"]["handle"] == {"Дерево": 0.95, "Металл": 0.0}
    assert result["optionProbabilities"]["mass"] == {"< 1": 0.95, "≥ 1": 0.0}


@pytest.mark.parametrize("source, expected, bucket", [
    ("10 тонн", "10000", ""),
    ("500 г", "0.5", "0.1–2"),
])
def test_autofill_returns_converted_number_even_outside_catalog_ranges(monkeypatch, source, expected, bucket):
    def handler(req):
        import json
        questions = json.loads(req.content)["questions"]
        criteria = questions["c0"]["criteria"]
        choice = next(label for label in criteria if label != autofill.NONE)
        probabilities = dict.fromkeys(criteria, 0.0)
        probabilities[choice] = 0.95
        probabilities[autofill.NONE] = 0.05
        return httpx.Response(200, json={"answers": {"c0": {"choice": choice, "probabilities": probabilities}}})

    mock_typesafe(monkeypatch, handler)
    body = request(text=f"Молоток слесарный {source}, боёк стальной круглый, рукоятка деревянная.", characteristics=[{
        "id": "mass", "name": "Масса", "unit": "кг", "numeric": True,
        "options": ["0.1–2"], "ranges": [{"option": "0.1–2", "min": 0.1, "max": 2}],
    }])
    result = asyncio.run(autofill.autofill_characteristics(body))
    row = result["characteristics"][0]
    assert row["value"] == expected
    assert row["rangeOption"] == bucket
    assert source in row["evidence"]
    assert result["optionProbabilities"]["mass"]["0.1–2"] == (0.95 if bucket else 0.0)


def test_batches_multiple_choices_and_skips_low_confidence(monkeypatch):
    counts = []

    def handler(req):
        import json
        questions = json.loads(req.content)["questions"]
        counts.append(len(questions))
        answers = {}
        for qid, question in questions.items():
            choice = next(iter(question["criteria"]))
            if qid == "c0_1":
                choice = autofill.NONE
            probabilities = dict.fromkeys(question["criteria"], 0.0)
            probabilities[choice] = 0.3 if qid == "c1" else 0.9
            other = next(label for label in question["criteria"] if label != choice)
            probabilities[other] = 1 - probabilities[choice]
            answers[qid] = {"choice": choice, "probabilities": probabilities}
        return httpx.Response(200, json={"answers": answers, "usage": {}})

    mock_typesafe(monkeypatch, handler)
    body = request(characteristics=[
        {"id": "colors", "name": "Цвета", "multiple": True, "options": ["Красный", "Белый", "Синий"]},
        *[{"id": str(i), "name": f"Поле {i}", "options": ["Да", "Нет"]} for i in range(4)],
    ])
    result = asyncio.run(autofill.autofill_characteristics(body))
    assert sorted(counts) == [3, 4]
    rows = {item["id"]: item for item in result["characteristics"]}
    assert rows["colors"]["value"] == "Красный · Синий"
    assert "0" not in rows
    assert result["optionProbabilities"]["colors"] == pytest.approx({"Красный": 0.9, "Белый": 0.1, "Синий": 0.9})
    assert result["optionProbabilities"]["0"]["Да"] == 0.3


def test_unfilled_fields_keep_all_option_probabilities(monkeypatch):
    def handler(req):
        return httpx.Response(200, json={"answers": {"c0": {
            "choice": autofill.NONE,
            "probabilities": {"Дерево": 0.2, "Металл": 0.1, autofill.NONE: 0.7},
        }}})

    mock_typesafe(monkeypatch, handler)
    body = request(characteristics=[{"id": "handle", "name": "Рукоятка", "options": ["Дерево", "Металл"]}])
    result = asyncio.run(autofill.autofill_characteristics(body))
    assert result["characteristics"] == []
    assert result["optionProbabilities"]["handle"] == {"Дерево": 0.2, "Металл": 0.1}


def test_range_probabilities_sum_converted_candidates(monkeypatch):
    def handler(req):
        import json
        criteria = json.loads(req.content)["questions"]["c0"]["criteria"]
        labels = [label for label in criteria if label != autofill.NONE]
        return httpx.Response(200, json={"answers": {"c0": {
            "choice": autofill.NONE,
            "probabilities": dict(zip(labels + [autofill.NONE], [0.2, 0.3, 0.1, 0.4], strict=True)),
        }}})

    mock_typesafe(monkeypatch, handler)
    body = request(text="Масса 200 г, масса 500 г, масса 2 кг", characteristics=[{
        "id": "mass", "name": "Масса", "unit": "кг", "options": ["< 1", "≥ 1"],
        "ranges": [{"option": "< 1", "max": 1, "maxInclusive": False}, {"option": "≥ 1", "min": 1}],
    }])
    result = asyncio.run(autofill.autofill_characteristics(body))
    assert result["optionProbabilities"]["mass"] == pytest.approx({"< 1": 0.5, "≥ 1": 0.1})


def test_numeric_candidates_preserve_dimension_components_and_filter_units():
    candidates = autofill.number_candidates("Ш×Г×В: 1200х600х750 мм, масса 2,5 кг, мощность 1.000 Вт")
    assert [item.value for item in candidates] == ["1200", "600", "750", "2.5", "1000"]
    assert [item.unit for item in candidates[:3]] == ["мм", "мм", "мм"]
    field = autofill.AutofillCharacteristic(id="width", name="Ширина", unit="Сантиметр")
    converted = autofill.numeric_candidates(field, candidates)
    assert [item.value for item in converted] == ["120"]


def test_cabinet_numeric_fields_without_catalog_units_keep_source_units(monkeypatch):
    def handler(req):
        import json
        questions = json.loads(req.content)["questions"]
        answers = {}
        for qid, question in questions.items():
            expected = {"c0": "450 мм", "c1": "1800 мм", "c2": "5 "}[qid]
            choice = next(label for label in question["criteria"] if label.startswith(expected))
            probabilities = dict.fromkeys(question["criteria"], 0.0)
            probabilities[choice] = 1.0
            answers[qid] = {"choice": choice, "probabilities": probabilities}
        return httpx.Response(200, json={"answers": answers})

    mock_typesafe(monkeypatch, handler)
    body = request(
        text="Шкаф металлический для медицинских карт, 1200х450х1800 мм (ДхШхВ), 5 полок, замок",
        characteristics=[
            {"id": "width", "name": "Ширина", "numeric": True},
            {"id": "height", "name": "Высота", "numeric": True},
            {"id": "shelves", "name": "Количество полок", "numeric": True, "unit": "шт"},
        ],
    )
    questions, bindings = autofill.build_questions(body)
    assert [len(question["criteria"]) for question in questions.values()] == [2, 2, 2]
    assert [candidate.value for candidate in bindings["c0"][1].values()] == ["450"]
    rows = {item["id"]: item for item in asyncio.run(autofill.autofill_characteristics(body))["characteristics"]}
    assert rows["width"]["value"] == "450 мм"
    assert rows["height"]["value"] == "1800 мм"
    assert rows["shelves"]["value"] == "5"


def test_cabinet_numbers_are_not_reused_for_parts_or_other_counts():
    text = "Шкаф металлический для медицинских карт, 1200х450х1800 мм (ДхШхВ), 5 полок, замок"
    candidates = autofill.number_candidates(text)
    for name in ["Глубина", "Высота ящика", "Глубина открытой полки", "Количество отделений", "Количество открытых полок", "Количество замков", "Максимальная нагрузка на полку"]:
        field = autofill.AutofillCharacteristic(id=name, name=name, numeric=True)
        assert autofill.numeric_candidates(field, candidates) == [], name
    field = autofill.AutofillCharacteristic(id="length", name="Длина", numeric=True)
    assert [item.value for item in autofill.numeric_candidates(field, candidates)] == ["1200"]


@pytest.mark.parametrize("text", ["1200х450х1800 мм", "Габариты 1200×450×1800 мм"])
def test_unlabelled_dimensions_do_not_guess_axes(text):
    candidates = autofill.number_candidates(text)
    for name in ["Длина", "Ширина", "Высота", "Глубина"]:
        assert autofill.numeric_candidates(autofill.AutofillCharacteristic(id=name, name=name, numeric=True), candidates) == []


def test_part_and_packaging_dimensions_stay_with_their_object():
    candidates = autofill.number_candidates("ШхГхВ: 1200х450х1800 мм; ящик ШхГхВ: 300х200х150 мм; упаковка ШхГхВ: 1250х500х1850 мм")
    for name, expected in [("Высота", "1800"), ("Высота ящика", "150"), ("Высота упаковки", "1850")]:
        field = autofill.AutofillCharacteristic(id=name, name=name, numeric=True)
        assert [item.value for item in autofill.numeric_candidates(field, candidates)] == [expected]


def test_counts_bind_to_nouns_and_explicit_qualifiers():
    candidates = autofill.number_candidates("5 полок; 2 открытые полки; количество отделений 3")
    shelves = autofill.AutofillCharacteristic(id="shelves", name="Количество полок", numeric=True)
    sections = autofill.AutofillCharacteristic(id="sections", name="Количество отделений", numeric=True)
    assert [item.value for item in autofill.numeric_candidates(shelves, candidates)] == ["5"]
    assert [item.value for item in autofill.numeric_candidates(sections, candidates)] == ["3"]
    open_shelves = autofill.AutofillCharacteristic(id="open", name="Количество открытых полок", numeric=True)
    assert [item.value for item in autofill.numeric_candidates(open_shelves, candidates)] == ["2"]


def test_numeric_schema_without_units_does_not_generate_text_choices():
    questions, _ = autofill.build_questions(request(text="Шкаф металлический", characteristics=[
        {"id": "height", "name": "Высота", "numeric": True},
    ]))
    assert questions == {}


def test_questions_keep_frontend_grouped_ids_labels_and_multiplicity():
    body = request(characteristics=[
        {"id": "merge:ktru:spgz", "name": "Тип", "options": ["1", "2"],
         "optionLabels": {"1": "Слесарный", "2": "Столярный"}, "hint": "Уточняет позицию"},
        {"id": "shape", "name": "Форма бойка", "options": ["Круглый", "Квадратный"], "multiple": True},
    ], standards=["ГОСТ Р 58518-2019"])
    questions, bindings = autofill.build_questions(body)
    assert list(questions) == ["c0", "c1_0", "c1_1"]
    assert questions["c0"]["criteria"] == {
        "1": "Слесарный", "2": "Столярный", autofill.NONE: "В описании нет однозначного значения этой характеристики",
    }
    assert questions["c0"]["instructions"]["подсказка схемы"] == "Уточняет позицию"
    assert questions["c0"]["instructions"]["стандарты"] == ["ГОСТ Р 58518-2019"]
    assert bindings["c0"][0].id == "merge:ktru:spgz"
    assert set(questions["c1_0"]["criteria"]) == {"Круглый", autofill.NONE}
    assert set(questions["c1_1"]["criteria"]) == {"Квадратный", autofill.NONE}


def test_range_boundaries():
    bucket = autofill.CharacteristicRange(option="bucket", min=0.5, max=1, maxInclusive=False)
    assert bucket.contains(0.5)
    assert not bucket.contains(1)


@pytest.mark.parametrize("payload", [
    {"answers": {}},
    {"answers": {"c0": {"choice": "invented", "probabilities": {"invented": 1}}}},
    {"answers": {"c0": {"choice": "Дерево", "probabilities": {"Дерево": 2}}}},
    {"answers": {"c0": {"choice": "Дерево"}}},
    {"answers": {"c0": {"choice": "Дерево", "probabilities": {"Дерево": 0.9}}}},
    {"answers": {"c0": {"choice": "Дерево", "probabilities": {"Дерево": 0.9, autofill.NONE: -0.1}}}},
])
def test_invalid_upstream_answers_are_rejected(monkeypatch, payload):
    mock_typesafe(monkeypatch, lambda req: httpx.Response(200, json=payload))
    body = request(characteristics=[{"id": "handle", "name": "Рукоятка", "options": ["Дерево"]}])
    with pytest.raises(autofill.AutofillError):
        asyncio.run(autofill.autofill_characteristics(body))


def test_missing_numeric_value_does_not_generate_question():
    questions, _ = autofill.build_questions(request(text="Молоток деревянный", characteristics=[
        {"id": "mass", "name": "Масса", "unit": "кг"},
    ]))
    assert questions == {}


def test_schema_rejects_duplicate_ids_and_empty_description():
    with pytest.raises(ValidationError):
        request(characteristics=[{"id": "same", "name": "A"}, {"id": "same", "name": "B"}])
    with pytest.raises(ValidationError):
        request(text="  ")


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
