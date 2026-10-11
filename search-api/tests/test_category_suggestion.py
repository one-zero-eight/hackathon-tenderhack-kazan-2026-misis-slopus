import math

import pytest

from search_api import category_suggestion as suggest


def candidate(side, parent="P", leaf="L", source="neighbors"):
    def vote(code):
        return {"code": code, "name": code, "score": 0.8, "source": source} if code else None
    return suggest._candidate(vote(parent), vote(leaf), side, source)


def test_softmax_votes_preserve_original_temperature():
    rows = [
        {"code": "A", "name": "А", "score": 0.81},
        {"code": "A", "name": "А", "score": 0.79},
        {"code": "B", "name": "Б", "score": 0.78},
    ]
    votes = suggest.vote_codes(rows)
    expected_a = (1 + math.exp(-0.02 / 0.05)) / (1 + math.exp(-0.02 / 0.05) + math.exp(-0.03 / 0.05))
    assert votes[0]["score"] == pytest.approx(expected_a)
    assert sum(vote["score"] for vote in votes) == pytest.approx(1)
    assert votes[0]["source"] == "neighbors"


def test_threshold_filters_before_display_and_voting(monkeypatch):
    monkeypatch.setattr(suggest.settings, "neighbor_score_threshold", 0.74)
    rows = [{"id": i, "score": score} for i, score in enumerate([0.68, 0.74, 0.77, None, float("nan"), float("inf")])]
    assert [item["id"] for item in suggest.relevant_neighbors(rows)] == [1, 2]
    assert suggest.vote_codes([{ "code": "X", "score": float("nan") }]) == []


def test_pair_conflict_never_creates_cross_parent_leaf_pair():
    selected, decision = suggest.decide_pair(
        ([candidate("okpdKtru", "A", "A-1")], True),
        ([candidate("okpdKtru", "B", "B-1", "classifier")], True), "okpdKtru",
    )
    assert selected is None
    assert decision["reason"] == "branch_conflict"
    assert [(pair["okpd2"]["code"], pair["ktru"]["code"]) for pair in decision["alternatives"]] == [("A", "A-1"), ("B", "B-1")]


def test_category_families_collapse_ktru_siblings_to_template(monkeypatch):
    monkeypatch.setattr(suggest.catalog, "get_ktru_specialization", lambda code: {
        "templateCode": "T", "templateName": "Шкаф для медицинских карт",
    })
    monkeypatch.setattr(suggest, "_enrich", lambda vote, tab: {**vote, "isTemplate": True})
    families = suggest._category_families([
        candidate("okpdKtru", "P", "L1"),
        candidate("okpdKtru", "P", "L2", "classifier"),
        candidate("okpdKtru", "P", None, "classifier"),
    ], "okpdKtru")
    assert len(families) == 1
    assert families[0]["okpd2"]["code"] == "P"
    assert families[0]["ktru"]["code"] == "T"
    assert families[0]["ktru"]["isTemplate"]
    assert families[0]["sources"] == ["neighbors", "classifier"]


def test_category_families_collapse_spgz_to_parent(monkeypatch):
    monkeypatch.setattr(suggest.catalog, "get_spgz_specialization", lambda input: {"characteristics": []})
    families = suggest._category_families([
        candidate("kpgzSpgz", "P", "L1"),
        candidate("kpgzSpgz", "P", "L2", "classifier"),
    ], "kpgzSpgz")
    assert len(families) == 1
    assert families[0]["kpgz"]["code"] == "P"
    assert families[0]["spgz"] is None


def test_category_families_do_not_merge_unrelated_ktru_families(monkeypatch):
    monkeypatch.setattr(suggest.catalog, "get_ktru_specialization", lambda code: None)
    families = suggest._category_families([
        candidate("okpdKtru", "P", "L1"),
        candidate("okpdKtru", "P", "L2"),
    ], "okpdKtru")
    assert len(families) == 2


def test_category_families_do_not_apply_different_spgz_children(monkeypatch):
    monkeypatch.setattr(suggest.catalog, "get_spgz_specialization", lambda input: None)
    families = suggest._category_families([
        candidate("kpgzSpgz", "P", "L1"),
        candidate("kpgzSpgz", "P", "L2"),
        candidate("kpgzSpgz", "P", "L2"),
    ], "kpgzSpgz")
    assert len(families) == 1
    assert families[0]["spgz"] is None


def test_resolver_auto_selects_verified_family_over_branch_conflict(monkeypatch):
    import asyncio
    from search_api import characteristic_autofill as autofill, jev_rerank

    monkeypatch.setattr(suggest.settings, "jev_rerank_enabled", True)
    monkeypatch.setattr(suggest, "_category_families", lambda pairs, side: pairs)
    monkeypatch.setattr(autofill, "typesafe_key", lambda: "test")
    winner = candidate("okpdKtru", "B", "B-1", "classifier")
    neighbors = {"pairs": {"okpdKtru": ([candidate("okpdKtru", "A", "A-1")], True), "kpgzSpgz": ([], False)}}
    classifiers = {"pairs": {"okpdKtru": ([winner], True), "kpgzSpgz": ([], False)}}

    async def ranks(query, questions):
        assert query == "Шкаф для медицинских карт"
        assert set(questions) == {"okpdKtru"}
        labels = list(questions["okpdKtru"]["criteria"])
        return {"okpdKtru": {labels[0]: 0.25, labels[1]: 0.75}}

    async def verify(client, key, state, questions, usage):
        assert state == {"текст": "Шкаф для медицинских карт"}
        assert "B-1" in questions["okpdKtru"]["instructions"]["question"]
        return {"okpdKtru": {"type": "noul", "noul": 0.95}}, "test"

    monkeypatch.setattr(jev_rerank, "_ask_questions", ranks)
    monkeypatch.setattr(autofill, "ask_jev", verify)
    result = asyncio.run(suggest._resolve_category_choices("Шкаф для медицинских карт", neighbors, classifiers))
    selected, decision = result["okpdKtru"]
    assert selected == winner
    assert not decision["needsChoice"]
    assert decision["alternatives"] == [winner]


def test_resolver_limits_unverified_choices_to_two_ranked_families(monkeypatch):
    import asyncio
    from search_api import characteristic_autofill as autofill, jev_rerank

    monkeypatch.setattr(suggest.settings, "jev_rerank_enabled", True)
    monkeypatch.setattr(suggest, "_category_families", lambda pairs, side: pairs)
    monkeypatch.setattr(autofill, "typesafe_key", lambda: "test")
    alternatives = [candidate("okpdKtru", str(index), str(index)) for index in range(6)]
    neighbors = {"pairs": {"okpdKtru": (alternatives, False), "kpgzSpgz": ([], False)}}
    classifiers = {"pairs": {side: ([], False) for side in suggest.PAIR_FIELDS}}

    async def ranks(query, questions):
        labels = list(questions["okpdKtru"]["criteria"])
        return {"okpdKtru": {label: index / 20 for index, label in enumerate(labels)}}

    async def verify(*args):
        return {"okpdKtru": {"type": "noul", "noul": 0.4}}, "test"

    monkeypatch.setattr(jev_rerank, "_ask_questions", ranks)
    monkeypatch.setattr(autofill, "ask_jev", verify)
    selected, decision = asyncio.run(suggest._resolve_category_choices("шкаф", neighbors, classifiers))["okpdKtru"]
    assert selected is None
    assert decision["needsChoice"]
    assert [pair["ktru"]["code"] for pair in decision["alternatives"]] == ["5", "4"]


def test_resolver_applies_sole_family_without_model_call(monkeypatch):
    import asyncio
    from search_api import jev_rerank

    family = candidate("okpdKtru", "P", "T")
    monkeypatch.setattr(suggest, "_category_families", lambda pairs, side: [family])

    async def unexpected(*args):
        raise AssertionError("One family requires no choice")

    monkeypatch.setattr(jev_rerank, "_ask_questions", unexpected)
    neighbors = {"pairs": {"okpdKtru": ([candidate("okpdKtru", "P", "L1"), candidate("okpdKtru", "P", "L2")], False), "kpgzSpgz": ([], False)}}
    classifiers = {"pairs": {side: ([], False) for side in suggest.PAIR_FIELDS}}
    selected, decision = asyncio.run(suggest._resolve_category_choices("шкаф", neighbors, classifiers))["okpdKtru"]
    assert selected == family
    assert not decision["needsChoice"]


def test_agreement_combines_provenance_and_keeps_real_vote_share():
    selected, decision = suggest.decide_pair(
        ([candidate("okpdKtru")], True),
        ([candidate("okpdKtru", source="classifier")], False), "okpdKtru",
    )
    assert selected["sources"] == ["neighbors", "classifier"]
    assert selected["ktru"]["source"] == "both"
    assert selected["ktru"]["score"] == 0.8
    assert not decision["needsChoice"]
    assert len(decision["alternatives"]) == 1


def test_parent_agreement_does_not_apply_ambiguous_child():
    selected, decision = suggest.decide_pair(
        ([candidate("okpdKtru", leaf=None)], True),
        ([candidate("okpdKtru", leaf="L1", source="classifier"), candidate("okpdKtru", leaf="L2", source="classifier")], False),
        "okpdKtru",
    )
    assert selected["okpd2"]["code"] == "P"
    assert selected["ktru"] is None
    # Parent-only auto application must still leave the leaf choice visible.
    assert decision["needsChoice"]


def test_sole_alternative_applies_without_choice():
    selected, decision = suggest.decide_pair(
        ([], False),
        ([candidate("kpgzSpgz", "01.02", None, "classifier")], False),
        "kpgzSpgz",
    )
    assert selected["kpgz"]["code"] == "01.02"
    assert selected["spgz"] is None
    assert not decision["needsChoice"]
    assert decision["reason"] is None
    assert len(decision["alternatives"]) == 1


def test_sides_have_independent_decisions():
    _, first = suggest.decide_pair(
        ([candidate("okpdKtru")], True),
        ([candidate("okpdKtru", leaf="other", source="classifier")], True), "okpdKtru",
    )
    selected, second = suggest.decide_pair(
        ([candidate("kpgzSpgz")], True), ([], False), "kpgzSpgz",
    )
    assert first["needsChoice"]
    assert not second["needsChoice"]
    assert selected["spgz"]["code"] == "L"


def test_classifier_rrf_rank_is_not_probability(monkeypatch):
    monkeypatch.setattr(suggest, "_enrich", lambda vote, tab: vote)
    result = {"okpdKtru": [{
        "okpd2": {"code": "P", "name": "P", "score": None}, "ktru": None,
        "rank": 0.033, "evidence": "semantic", "ambiguous": False,
    }]}
    pairs, confident = suggest._classifier_pairs(result, "okpdKtru")
    assert not confident
    assert pairs[0]["okpd2"]["score"] == 0


def test_unknown_leaf_is_rejected_and_sole_schema_resolved(monkeypatch):
    monkeypatch.setattr(suggest, "_enrich", lambda vote, tab: None if vote and vote["code"] == "unknown" else vote)
    monkeypatch.setattr(suggest.catalog, "sole_spgz_for_kpgz", lambda code: {"code": "sole", "name": "Шкаф"})
    pairs, confident = suggest._neighbor_pairs([
        {"score": 0.81, "kpgzCode": "P", "spgzId": "unknown", "kpgzName": "Шкафы"}
    ], "kpgzSpgz")
    assert confident
    assert pairs[0]["spgz"]["code"] == "sole"


def test_invalid_link_never_wins_pair(monkeypatch):
    monkeypatch.setattr(suggest, "_enrich", lambda vote, tab: vote)
    monkeypatch.setattr(suggest.catalog, "get_directory_record", lambda tab, code: {"links": [{"tab": "kpgz", "code": "WRONG"}]})
    monkeypatch.setattr(suggest.catalog, "sole_spgz_for_kpgz", lambda code: None)
    pairs, confident = suggest._neighbor_pairs([
        {"score": 0.81, "kpgzCode": "P", "spgzId": "bad", "kpgzName": "Шкафы"}
    ], "kpgzSpgz")
    assert pairs[0]["spgz"] is None


async def _quality_ok(text: str):
    return {"verdict": "ok", "reasons": [], "probabilities": {"is_product": 0.9, "specific": 0.9, "plausible": 0.9}}


@pytest.mark.parametrize("description", ["", "  ", "аб"])
def test_short_query_never_calls_embedding(monkeypatch, description):
    import asyncio
    async def unexpected(*args, **kwargs):
        raise AssertionError("Short query should not retrieve")
    monkeypatch.setattr(suggest, "run_search", unexpected)
    monkeypatch.setattr(suggest, "search_classifiers", unexpected)
    result = asyncio.run(suggest.suggest_categories(description))
    assert result["neighbors"] == []
    assert result["characteristics"] == []
    assert not result["needsChoice"]
    assert result["queryQuality"] is None


def test_suggest_logs_quality_gate_and_total_without_description(monkeypatch, caplog):
    import asyncio
    import logging

    async def reject(query):
        return {"verdict": "reject", "reasons": [], "probabilities": {}}

    monkeypatch.setattr("search_api.query_quality.check_query_quality", reject)
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        asyncio.run(suggest.suggest_categories("секретное описание товара"))
    assert "stage=quality duration_ms=" in caplog.text
    assert "finished duration_ms=" in caplog.text
    assert "status=reject" in caplog.text
    assert "секретное описание товара" not in caplog.text


def test_partial_branch_failure_is_visible(monkeypatch):
    import asyncio
    async def neighbors(query):
        return {"neighbors": [], "error": "Нет индекса товаров"}
    async def classifiers(query):
        return {"result": {"okpdKtru": [], "kpgzSpgz": [], "hits": []}, "error": None}
    async def no_jev(*args, **kwargs):
        return None, None
    monkeypatch.setattr(suggest, "_neighbors_retrieve", neighbors)
    monkeypatch.setattr(suggest, "_classifiers_retrieve", classifiers)
    monkeypatch.setattr("search_api.jev_rerank.rerank_suggest_shortlists", no_jev)
    monkeypatch.setattr("search_api.query_quality.check_query_quality", _quality_ok)
    result = asyncio.run(suggest.suggest_categories("сахар"))
    assert result["neighborError"] == "Нет индекса товаров"
    assert result["classifierError"] is None
    assert result["neighbors"] == []
    assert result["queryQuality"]["verdict"] == "ok"


def test_product_branch_filters_before_pair_votes(monkeypatch):
    import asyncio
    async def search(**kwargs):
        assert kwargs == {"query": "сахар", "mode": "semantic", "page": 1}
        return {"items": [{"id": 1, "score": 0.68, "okpd2": "furniture"}]}
    monkeypatch.setattr(suggest, "run_search", search)
    monkeypatch.setattr(suggest.settings, "neighbor_score_threshold", 0.74)
    def pairs(items, side):
        assert items == []
        return [], False
    monkeypatch.setattr(suggest, "_neighbor_pairs", pairs)
    result = asyncio.run(suggest._neighbors_branch("сахар"))
    assert result["neighbors"] == []
    assert result["error"] is None


def test_excluded_and_inactive_votes_are_not_applied(monkeypatch):
    monkeypatch.setattr(suggest.catalog, "get_directory_record", lambda tab, code: {
        "name": "Archived", "excluded": tab == "spgz", "actual": tab != "okpd",
    })
    vote = {"code": "P", "name": "P", "score": 1, "source": "neighbors"}
    assert suggest._enrich(vote, "spgz") is None
    assert suggest._enrich(vote, "okpd") is None
    assert suggest._enrich(vote, "kpgz") is not None


def test_assembly_keeps_schema_parts_and_template_metadata(monkeypatch):
    record = {"status": "ACTIVE", "standards": ["ГОСТ"], "noNewFeatures": True, "noNewFeaturesReason": "Правило"}
    ktru_field = {"id": "type", "name": "Тип", "source": "ktru", "options": ["Слесарный"]}
    spgz_field = {"id": "length", "name": "Длина", "source": "spgz", "numeric": True}
    monkeypatch.setattr(suggest.catalog, "get_ktru_record", lambda code: {"characteristics": [ktru_field]} if code == "template" else record)
    monkeypatch.setattr(suggest.catalog, "get_ktru_node", lambda code: {"code": code, "name": "Молоток слесарный"})
    monkeypatch.setattr(suggest.catalog, "get_directory_record", lambda tab, code: {"code": code})
    monkeypatch.setattr(suggest.catalog, "draft_ktru_characteristics", lambda record: [ktru_field])
    monkeypatch.setattr(suggest.catalog, "draft_spgz_characteristics", lambda record: [spgz_field])
    monkeypatch.setattr(suggest.catalog, "get_catalog_specialization", lambda input: {"templateKtruCode": "template"})
    neighbors = {"neighbors": [{"id": index} for index in range(10)], "error": None, "pairs": {
        side: ([candidate(side)], True) for side in suggest.PAIR_FIELDS
    }}
    classifiers = {"error": None, "pairs": {side: ([], False) for side in suggest.PAIR_FIELDS}}
    result = suggest._assemble("Молоток слесарный. Описание", neighbors, classifiers)
    assert result["characteristics"] == [ktru_field, spgz_field]
    assert result["templateCharacteristics"] == [ktru_field]
    assert result["standards"] == ["ГОСТ"]
    assert result["noNewFeatures"] and result["noNewFeaturesReason"] == "Правило"
    assert result["ktruNode"]["name"] == "Молоток слесарный"
    assert result["productName"] == "Молоток слесарный"
    assert len(result["neighbors"]) == 6


def test_category_endpoint_forwards_backend_result_and_errors(monkeypatch):
    from fastapi.testclient import TestClient
    from search_api.main import app

    async def handler(description, *, force=False):
        assert description == "молоток"
        assert force is False
        return {"neighbors": [], "characteristics": [], "needsChoice": False}
    monkeypatch.setattr(suggest, "suggest_categories", handler)
    client = TestClient(app)
    response = client.post("/categories/suggest", json={"description": "молоток"})
    assert response.status_code == 200
    assert response.json()["characteristics"] == []
    assert client.post("/categories/suggest", json={}).status_code == 422

    async def forced(description, *, force=False):
        assert force is True
        return {"neighbors": [], "characteristics": [], "needsChoice": False, "queryQuality": {"verdict": "suspicious"}}
    monkeypatch.setattr(suggest, "suggest_categories", forced)
    assert client.post("/categories/suggest", json={"description": "молоток", "force": True}).status_code == 200

    async def broken(description, *, force=False):
        raise RuntimeError("Нет индекса")
    monkeypatch.setattr(suggest, "suggest_categories", broken)
    response = client.post("/categories/suggest", json={"description": "молоток"})
    assert response.status_code == 503
    assert response.json() == {"detail": "Нет индекса"}


def test_both_branch_failures_are_api_error(monkeypatch):
    import asyncio
    async def neighbors(query):
        return {"neighbors": [], "error": "Нет индекса"}
    async def classifiers(query):
        return {"result": None, "error": "Нет индекса"}
    monkeypatch.setattr(suggest, "_neighbors_retrieve", neighbors)
    monkeypatch.setattr(suggest, "_classifiers_retrieve", classifiers)
    monkeypatch.setattr("search_api.query_quality.check_query_quality", _quality_ok)
    with pytest.raises(RuntimeError, match="По товарам"):
        asyncio.run(suggest.suggest_categories("молоток"))


def test_long_description_reaches_quality_search_and_rerank_untruncated(monkeypatch):
    import asyncio

    text = "Шкаф металлический для медицинских карт, 5 полок, замок. " * 30
    assert len(text) > 500
    calls = []

    async def quality(query):
        assert query == text.strip()
        calls.append("quality")
        return await _quality_ok(query)

    async def neighbors(query):
        assert query == text.strip()
        assert calls == ["quality"]
        calls.append("neighbors")
        return {"neighbors": [], "error": None}

    async def classifiers(query):
        assert query == text.strip()
        calls.append("classifiers")
        return {"result": {"okpdKtru": [], "kpgzSpgz": [], "hits": []}, "error": None}

    async def rerank(query, **kwargs):
        assert query == text.strip()
        calls.append("rerank")
        return None, None

    monkeypatch.setattr("search_api.query_quality.check_query_quality", quality)
    monkeypatch.setattr(suggest, "_neighbors_retrieve", neighbors)
    monkeypatch.setattr(suggest, "_classifiers_retrieve", classifiers)
    monkeypatch.setattr("search_api.jev_rerank.rerank_suggest_shortlists", rerank)
    result = asyncio.run(suggest.suggest_categories(text))
    assert result["queryQuality"]["verdict"] == "ok"
    assert calls == ["quality", "neighbors", "classifiers", "rerank"]
    with pytest.raises(ValueError, match="20000"):
        asyncio.run(suggest.suggest_categories("а" * 20_001))


def test_quality_gate_skips_retrieve_until_forced(monkeypatch):
    import asyncio

    async def reject(text: str):
        return {
            "verdict": "reject",
            "reasons": ["описание товара выглядит противоречивым, абсурдным или не похоже на товар"],
            "probabilities": {"is_product": 0.1, "specific": 0.9, "plausible": 0.9},
        }

    async def suspicious(text: str):
        return {
            "verdict": "suspicious",
            "reasons": ["описание товара выглядит противоречивым, абсурдным или не похоже на товар"],
            "probabilities": {"is_product": 0.9, "specific": 0.1, "plausible": 0.9},
        }

    async def unexpected(*args, **kwargs):
        raise AssertionError("gated query must not retrieve")

    monkeypatch.setattr(suggest, "_neighbors_retrieve", unexpected)
    monkeypatch.setattr(suggest, "_classifiers_retrieve", unexpected)
    monkeypatch.setattr("search_api.query_quality.check_query_quality", reject)
    blocked = asyncio.run(suggest.suggest_categories("привет"))
    assert blocked["neighbors"] == []
    assert blocked["queryQuality"]["verdict"] == "reject"

    monkeypatch.setattr("search_api.query_quality.check_query_quality", suspicious)
    soft = asyncio.run(suggest.suggest_categories("товар для офиса"))
    assert soft["neighbors"] == []
    assert soft["queryQuality"]["verdict"] == "suspicious"

    calls = {"n": 0, "c": 0}

    async def neighbors(query):
        calls["n"] += 1
        return {"neighbors": [], "error": None}

    async def classifiers(query):
        calls["c"] += 1
        return {"result": {"okpdKtru": [], "kpgzSpgz": [], "hits": []}, "error": None}

    async def no_jev(*args, **kwargs):
        return None, None

    monkeypatch.setattr(suggest, "_neighbors_retrieve", neighbors)
    monkeypatch.setattr(suggest, "_classifiers_retrieve", classifiers)
    monkeypatch.setattr("search_api.jev_rerank.rerank_suggest_shortlists", no_jev)
    forced = asyncio.run(suggest.suggest_categories("товар для офиса", force=True))
    assert calls == {"n": 1, "c": 1}
    assert forced["queryQuality"]["verdict"] == "suspicious"


def test_progress_precedes_classifier_completion_and_family_selection(monkeypatch):
    import asyncio

    async def exercise():
        events = []
        products_ready = asyncio.Event()

        def progress(data):
            events.append(data)
            if "neighbors" in data:
                products_ready.set()

        async def neighbors(query):
            assert events[0]["queryQuality"]["verdict"] == "ok"
            return {"neighbors": [{"id": 1, "score": 0.9}], "error": None}

        async def classifiers(query):
            await products_ready.wait()
            assert events[1]["neighbors"][0]["id"] == 1
            return {"result": {"okpdKtru": [], "kpgzSpgz": [], "hits": []}, "error": None}

        async def rerank(*args, **kwargs):
            return [{"id": 2, "score": 0.95}], None

        async def resolve(query, neighbors, classifiers):
            assert events[-1]["neighbors"][0]["id"] == 2
            return {side: (None, {"needsChoice": False, "reason": None, "alternatives": []})
                    for side in suggest.PAIR_FIELDS}

        monkeypatch.setattr("search_api.query_quality.check_query_quality", _quality_ok)
        monkeypatch.setattr(suggest, "_neighbors_retrieve", neighbors)
        monkeypatch.setattr(suggest, "_classifiers_retrieve", classifiers)
        monkeypatch.setattr("search_api.jev_rerank.rerank_suggest_shortlists", rerank)
        monkeypatch.setattr(suggest, "_resolve_category_choices", resolve)
        result = await asyncio.wait_for(suggest.suggest_categories("молоток", on_progress=progress), timeout=2)
        assert result["neighbors"][0]["id"] == 2
        assert len(events) == 3

    asyncio.run(exercise())


def test_category_stream_forwards_progress_result_and_errors(monkeypatch):
    import json
    from fastapi.testclient import TestClient
    from search_api.main import app

    async def handler(description, *, force=False, on_progress):
        assert description == "молоток"
        assert force
        on_progress({"queryQuality": {"verdict": "ok", "durationMs": 10}})
        on_progress({"neighbors": [{"id": 1}]})
        return {"neighbors": [{"id": 1}], "characteristics": []}

    monkeypatch.setattr(suggest, "suggest_categories", handler)
    client = TestClient(app)
    response = client.post("/categories/suggest/stream", json={"description": "молоток", "force": True})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in response.iter_lines()]
    assert [event["type"] for event in events] == ["progress", "progress", "result"]
    assert events[0]["data"]["queryQuality"]["verdict"] == "ok"
    assert events[1]["data"]["neighbors"] == [{"id": 1}]
    assert events[2]["data"]["characteristics"] == []

    async def broken(*args, **kwargs):
        raise RuntimeError("Нет индекса")

    monkeypatch.setattr(suggest, "suggest_categories", broken)
    response = client.post("/categories/suggest/stream", json={"description": "молоток"})
    assert response.json() == {"type": "error", "detail": "Нет индекса"}
    assert client.post("/categories/suggest/stream", json={"description": "а" * 20_001}).status_code == 400
