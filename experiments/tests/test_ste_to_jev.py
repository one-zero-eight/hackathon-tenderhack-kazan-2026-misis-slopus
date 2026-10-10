import argparse
import gzip
import importlib.util
import json
from pathlib import Path
from threading import Barrier, Event

import pytest

spec = importlib.util.spec_from_file_location(
    "ste_to_jev", Path(__file__).resolve().parents[1] / "experiments" / "ste_to_jev.py")
ste = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ste)


def card(ste_id=1, code="01.16.01.01", text="830х620х1320 мм"):
    return {
        "id": ste_id, "name": text, "description": "Диван модульный",
        "production": {"code": code, "name": "Диваны"},
        "skuCharacteristics": [
            {"characteristicValueDecimalValue": value,
             "productCharacteristicValue": {"productCharacteristicName": name,
                                            "productCharacteristicUnitName": "Миллиметр"}}
            for name, value in [("Высота", 830), ("Глубина", 620), ("Ширина", 1320)]],
    }


def node(code="01.16.01.01"):
    return {"code": code, "name": "Диваны", "kpgzCharacteristics": [
        {"characteristicId": i, "characteristicName": name, "conditionTypeId": ste.NUMBER,
         "okeiCode": "003"} for i, name in enumerate(["Высота", "Глубина", "Ширина"], 1)]}


@pytest.mark.parametrize("name,value", [("Высота", 830), ("Глубина", 620), ("Ширина", 1320)])
def test_all_dimension_components_have_evidence(name, value):
    text = "Диван 830х620х1320 мм"
    truth = {"name": name, "values": [value], "unit": "Миллиметр"}
    field = {"characteristic": name, "numeric": True, "unit": "Миллиметр"}
    evidence, reason = ste.evidence(truth, field, {"type": "choice"}, text)
    assert evidence is None
    assert reason.startswith(ste.UNLABELED)
    assert str(value) in reason


def test_number_spans_preserve_original_positions():
    text = "830х620х1320 мм; вес 8 кг"
    spans = list(ste.number_spans(text))
    assert [v for v, _, _, _ in spans] == ["830 мм", "620 мм", "1320 мм", "8 кг"]
    assert [text[start:end] for _, _, start, end in spans] == ["830", "620", "1320", "8 кг"]
    detail = ste.number_candidates(text)["620 мм"]["фрагменты"][0]
    assert detail["позиция в записи"] == "2-е число из «830х620х1320 мм»"
    assert "830х620х1320 мм" in detail["текст"]


@pytest.mark.parametrize("text,options", [
    ("830х620х1320 (В х Ш х Г)", ["830", "620", "1320"]),
    ("830х620х1320 мм (В х Ш х Г)", ["830 мм", "620 мм", "1320 мм"]),
    ("830х620х1320 (В х Ш х Г) мм", ["830", "620", "1320"]),
    ("(В х Ш х Г) 830х620х1320 мм", ["830 мм", "620 мм", "1320 мм"]),
    ("100х100х100 (В х Ш х Г)", ["100"]),
])
def test_dimension_labels_reach_model_without_assigning_numbers(text, options):
    candidates = ste.number_candidates(text)
    assert list(candidates) == options
    for candidate in candidates.values():
        for occurrence in candidate["фрагменты"]:
            assert "(В х Ш х Г)" in occurrence["текст"]
            assert set(occurrence) == {"текст", "позиция в записи"}
    if len(options) == 1:
        assert len(candidates["100"]["фрагменты"]) == 3


def test_candidate_evidence_does_not_borrow_another_occurrence():
    truth = {"name": "Высота", "values": [80], "unit": "Миллиметр"}
    field = {"characteristic": "Высота", "numeric": True, "unit": "Миллиметр"}
    text = "Высота модели 80 кг. " + "Описание " * 20 + "80х90х100 мм"
    evidence, reason = ste.evidence(truth, field, {"type": "choice"}, text)
    assert evidence is None
    assert reason.startswith(ste.UNLABELED)


def test_candidates_keep_all_occurrences_and_unitless_context():
    text = "Ширина 80 мм. " + "Описание " * 20 + "Высота 80 мм. Мощность 650 Вт. Код 650"
    candidates = ste.number_candidates(text)
    fragments = candidates["80 мм"]["фрагменты"]
    assert len(fragments) == 2
    assert "Ширина" in fragments[0]["текст"]
    assert "Высота" in fragments[1]["текст"]
    assert "650" in candidates and "650 Вт" in candidates


def test_questions_use_source_context_without_dimension_defaults():
    questions, _ = ste.to_questions(node(), "Диван (Ш×Г×В): 830х620х1320 мм", {"003": "Миллиметр"})
    instructions = questions["c1"]["instructions"]
    assert "размеры_без_подписей" not in instructions
    assert "Не угадывай порядок" in instructions["правило"]
    assert "Ш×Г×В" in questions["c1"]["criteria"]["620 мм"]["фрагменты"][0]["текст"]
    assert ste.NONE in questions["c1"]["criteria"]


def test_request_does_not_depend_on_card_truth(tmp_path):
    original = card()
    changed = card()
    changed["skuCharacteristics"][0]["characteristicValueDecimalValue"] = 999
    before = ste.check_card(original, node(), {"003": "Миллиметр"}, None, tmp_path / "a")
    after = ste.check_card(changed, node(), {"003": "Миллиметр"}, None, tmp_path / "b")
    assert before["request"] == after["request"]


def test_retrieved_category_replaces_card_category_without_leakage(tmp_path):
    schema = node("99.01") | {"name": "Найденная категория", "_retrieval": {
        "model": "google/embeddinggemma-2", "top_k": [{"code": "99.01", "name": "Найденная категория", "score": 0.7}]}}
    original = card()
    original["productionDirectory"] = {"name": "Секретная исходная категория"}
    changed = card(code="88.01")
    changed["productionDirectory"] = {"name": "Другая исходная категория"}
    before = ste.check_card(original, schema, {}, None, tmp_path / "a")
    after = ste.check_card(changed, schema, {}, None, tmp_path / "b")
    assert before["request"] == after["request"]
    assert before["request"]["state"]["категория"] == "Найденная категория"
    assert before["category_search"]["expected_code"] == "01.16.01.01"
    assert not before["category_search"]["top1_correct"]
    assert not before["category_search"]["topk_correct"]
    assert "Секретная" not in json.dumps(before["request"], ensure_ascii=False)


def test_api_questions_are_batched_without_losing_answers(tmp_path, monkeypatch):
    schema = node()
    schema["kpgzCharacteristics"].extend(
        {"characteristicId": i, "characteristicName": f"Параметр {i}", "conditionTypeId": ste.NUMBER}
        for i in range(4, 10))
    calls = []

    def api(request, key):
        calls.append(request)
        return {"answers": {qid: {"type": "choice", "choice": ste.NONE, "confidence": 0.8}
                            for qid in request["questions"]},
                "usage": {"input_tokens": 100, "output_tokens": 20}}

    monkeypatch.setattr(ste, "call_jev", api)
    record = ste.check_card(card(), schema, {"003": "Миллиметр"}, "test-key", tmp_path / "logs")
    assert [len(c["questions"]) for c in calls] == [4, 4, 1]
    assert len(record["response"]["answers"]) == 9
    assert record["response"]["usage"] == {"input_tokens": 300, "output_tokens": 60}
    assert all(c["state"] == record["request"]["state"] for c in calls)


def test_parallel_cards_overlap_and_preserve_sample_order(tmp_path, monkeypatch):
    barrier = Barrier(3)
    second_done = Event()
    completed = []

    def check(c, schema, okei, key, log_dir):
        barrier.wait(timeout=5)  # Последовательный запуск не сможет пройти барьер.
        if c["id"] == 1:
            assert second_done.wait(timeout=5)
        completed.append(c["id"])
        if c["id"] == 2:
            second_done.set()
        return {"ste": {"id": c["id"]}}

    monkeypatch.setattr(ste, "check_card", check)
    selected = [(card(i), node()) for i in (1, 2, 3)]
    results = ste.check_cards(selected, {}, None, tmp_path, workers=3)
    assert [r["ste"]["id"] for r in results] == [1, 2, 3]
    assert completed.index(2) < completed.index(1)


def test_parallel_cards_reject_invalid_worker_count(tmp_path):
    with pytest.raises(ValueError, match="workers"):
        ste.check_cards([], {}, None, tmp_path, workers=0)


def test_focus_dims_selects_all_components(tmp_path):
    ste_path, kpgz_path, okei_path = [tmp_path / p for p in ("ste.gz", "kpgz.gz", "okei.gz")]
    with gzip.open(ste_path, "wt", encoding="utf-8") as f:
        f.write(json.dumps(card(), ensure_ascii=False) + "\n")
    with gzip.open(kpgz_path, "wt", encoding="utf-8") as f:
        json.dump([node()], f, ensure_ascii=False)
    with gzip.open(okei_path, "wt", encoding="utf-8") as f:
        f.write("1;1;003;Миллиметр\n")
    args = argparse.Namespace(ste=ste_path, kpgz=kpgz_path, okei=okei_path,
                              focus="dims", sample=1, seed=0, min_pairs=3)
    selected = ste.select_cards(args)
    assert len(selected) == 1
    record = ste.check_card(*selected[0], {"003": "Миллиметр"}, None, tmp_path / "logs")
    assert all(r["dimension_in_pattern"] for r in record["rows"])
    assert not ste.metrics([record])["pairs"]
    for r in record["rows"]:
        r.update(answer=ste.NONE, confidence=0.9, verdict="пропуск")
    metrics = ste.metrics([record])
    assert metrics["неподписанные размеры"]["n"] == 3
    assert metrics["неподписанные размеры"]["пропуск"] == 3
    assert metrics["неподписанные размеры"]["доля ответов"] == 0
    assert metrics["неподписанные размеры"]["точность ответов"] is None
    assert len(metrics["ошибки неподписанных размеров"]) == 3


def test_compare_evals_matches_questions_and_rejects_different_samples(tmp_path):
    record = ste.check_card(card(), node(), {"003": "Миллиметр"}, None, tmp_path / "logs")
    for row in record["rows"]:
        row.update(answer=ste.NONE, confidence=0.9, verdict="пропуск")
    base = {"ste_ids": [1], "pairs": [r | {"ste": 1} for r in record["rows"]],
            "metrics": ste.metrics([record])}
    base_path, hints_path = tmp_path / "base.json", tmp_path / "hints.json"
    base_path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    record["rows"][0].update(answer="830 мм", verdict="верно")
    hints = {"ste_ids": [1], "pairs": [r | {"ste": 1} for r in record["rows"]],
             "metrics": ste.metrics([record])}
    hints_path.write_text(json.dumps(hints, ensure_ascii=False), encoding="utf-8")
    report = ste.compare_evals(base_path, hints_path)
    assert report["исправлено"] == 1 and report["испорчено"] == 0
    assert report["изменения"][0]["characteristic"] == "Высота"
    hints["ste_ids"] = [2]
    hints_path.write_text(json.dumps(hints, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="Выборки СТЕ различаются"):
        ste.compare_evals(base_path, hints_path)
