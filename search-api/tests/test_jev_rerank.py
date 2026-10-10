import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr

from search_api import category_suggestion as suggest
from search_api import jev_rerank as rerank
from search_api import search as search_mod
from search_api.config import settings


def product(product_id: int, name: str, **extra):
    return {
        "id": product_id,
        "name": name,
        "description": extra.pop("description", ""),
        "okpd2": extra.pop("okpd2", "31.01"),
        "okpd2Name": extra.pop("okpd2Name", "Мебель"),
        "ktruCode": extra.pop("ktruCode", ""),
        "characteristics": extra.pop("characteristics", [{"name": "Материал", "value": "Сталь"}]),
        "score": extra.pop("score", 0.8),
        "match": "semantic",
        **extra,
    }


def category_hit(entry_id: int, kind: str, code: str, name: str, **extra):
    return {
        "id": entry_id, "kind": kind, "code": code, "name": name,
        "linkCode": extra.pop("linkCode", ""),
        "status": "ACTIVE", "isTemplate": False, "isExcluded": False,
        "score": extra.pop("score", 0.7), "rank": extra.pop("rank", 0.1),
        "match": "semantic", "evidence": extra.pop("evidence", "semantic"),
        "synonyms": [], "weakSynonyms": [],
        **extra,
    }


def mock_jev(monkeypatch, handler):
    monkeypatch.setattr(settings, "typesafe_api_key", SecretStr("test-secret"))
    monkeypatch.setattr(settings, "jev_rerank_enabled", True)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(rerank.httpx, "AsyncClient", client_factory)


def test_rerank_orders_by_probabilities(monkeypatch):
    products = [product(1, "Стул"), product(2, "Стол"), product(3, "Шкаф")]

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        assert body["state"] == {"товар": "письменный стол"}
        assert set(body["questions"]) == {"q"}
        criteria = body["questions"]["q"]["criteria"]
        assert rerank.NONE in criteria
        answers = {
            label: (0.7 if label == "Стол" else 0.1 if label == "Стул" else 0.05 if label == "Шкаф" else 0.15)
            for label in criteria
        }
        return httpx.Response(200, json={
            "model": "jev",
            "answers": {"q": {"type": "choice", "choice": "Стол", "probabilities": answers}},
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    mock_jev(monkeypatch, handler)
    ranked = asyncio.run(rerank.rerank_products("письменный стол", products, truncate=False))
    assert [item["id"] for item in ranked] == [2, 1, 3]
    assert ranked[0]["score"] == pytest.approx(0.7)


def test_truncate_keeps_strong_top_and_drops_tail(monkeypatch):
    products = [product(1, "A"), product(2, "B"), product(3, "C")]

    def handler(req: httpx.Request):
        criteria = json.loads(req.content)["questions"]["q"]["criteria"]
        probs = {label: 0.02 for label in criteria}
        probs["A"] = 0.55
        probs["B"] = 0.25
        probs["C"] = 0.03
        probs[rerank.NONE] = 0.15
        return httpx.Response(200, json={
            "answers": {"q": {"type": "choice", "choice": "A", "probabilities": probs}},
        })

    mock_jev(monkeypatch, handler)
    ranked = asyncio.run(rerank.rerank_products("товар", products, truncate=True))
    assert [item["id"] for item in ranked] == [1, 2]


def test_truncate_drops_all_when_none_dominates(monkeypatch):
    products = [product(1, "A"), product(2, "B")]

    def handler(req: httpx.Request):
        criteria = json.loads(req.content)["questions"]["q"]["criteria"]
        probs = {label: 0.04 for label in criteria}
        probs[rerank.NONE] = 0.9
        return httpx.Response(200, json={
            "answers": {"q": {"type": "choice", "choice": rerank.NONE, "probabilities": probs}},
        })

    mock_jev(monkeypatch, handler)
    assert asyncio.run(rerank.rerank_products("товар", products, truncate=True)) == []


def test_verify_category_hits_filters_and_scores(monkeypatch):
    hits = [
        category_hit(1, "ktru", "A-1", "Стул"),
        category_hit(2, "ktru", "B-1", "Стол"),
        category_hit(3, "okpd", "31.01", "Мебель"),
    ]

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        assert set(body["questions"]) == {"categories"}
        criteria = body["questions"]["categories"]["criteria"]
        probs = {label: 0.05 for label in criteria}
        preferred = next(label for label in criteria if "B-1" in label)
        probs[preferred] = 0.7
        probs[rerank.NONE] = 0.1
        return httpx.Response(200, json={
            "answers": {"categories": {"type": "choice", "choice": preferred, "probabilities": probs}},
        })

    mock_jev(monkeypatch, handler)
    verified = asyncio.run(rerank.verify_category_hits("письменный стол", hits, truncate=True))
    assert verified is not None
    assert verified[0]["code"] == "B-1"
    assert verified[0]["jevScore"] == pytest.approx(0.7)


def test_verify_category_none_returns_empty(monkeypatch):
    hits = [category_hit(1, "ktru", "A-1", "Стул"), category_hit(2, "ktru", "B-1", "Стол")]

    def handler(req: httpx.Request):
        criteria = json.loads(req.content)["questions"]["categories"]["criteria"]
        probs = {label: 0.03 for label in criteria}
        probs[rerank.NONE] = 0.9
        return httpx.Response(200, json={
            "answers": {"categories": {
                "type": "choice", "choice": rerank.NONE, "probabilities": probs,
            }},
        })

    mock_jev(monkeypatch, handler)
    assert asyncio.run(rerank.verify_category_hits("мусор", hits, truncate=True)) == []


def test_suggest_shortlists_one_request_both_sides(monkeypatch):
    products = [product(1, "Стул"), product(2, "Стол")]
    hits = [category_hit(10, "ktru", "A-1", "Стул"), category_hit(11, "ktru", "B-1", "Стол")]

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        assert set(body["questions"]) == {"neighbors", "categories"}
        answers = {}
        for qid, question in body["questions"].items():
            criteria = question["criteria"]
            probs = {label: 0.1 for label in criteria}
            preferred = next(label for label in criteria if "Стол" in label or "B-1" in label)
            probs[preferred] = 0.7
            probs[rerank.NONE] = 0.1
            answers[qid] = {"type": "choice", "choice": preferred, "probabilities": probs}
        return httpx.Response(200, json={"answers": answers})

    mock_jev(monkeypatch, handler)
    ranked_products, ranked_hits = asyncio.run(rerank.rerank_suggest_shortlists(
        "письменный стол", products=products, category_hits=hits, truncate=False,
    ))
    assert ranked_products is not None and ranked_products[0]["id"] == 2
    assert ranked_hits is not None and ranked_hits[0]["code"] == "B-1"


def test_verify_skipped_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "jev_rerank_enabled", False)
    assert asyncio.run(rerank.verify_category_hits("x", [category_hit(1, "okpd", "1", "A")])) is None


def test_disabled_or_missing_key_keeps_order(monkeypatch):
    products = [product(1, "A", score=0.9), product(2, "B", score=0.8)]
    monkeypatch.setattr(settings, "jev_rerank_enabled", False)
    assert asyncio.run(rerank.rerank_products("x", products)) == products

    monkeypatch.setattr(settings, "jev_rerank_enabled", True)
    monkeypatch.setattr(
        rerank, "typesafe_key",
        lambda: (_ for _ in ()).throw(rerank.AutofillConfigError("no key")),
    )
    kept = asyncio.run(rerank.rerank_products("x", products))
    assert [item["id"] for item in kept] == [1, 2]
    assert kept[0]["score"] == 0.9


def test_http_error_falls_back(monkeypatch):
    products = [product(1, "A"), product(2, "B")]

    def handler(req: httpx.Request):
        return httpx.Response(503, json={"detail": "busy"})

    mock_jev(monkeypatch, handler)
    kept = asyncio.run(rerank.rerank_products("стол", products))
    assert [item["id"] for item in kept] == [1, 2]


def test_unique_labels_on_name_collision():
    candidates = rerank.candidates_from_products([
        product(10, "Стол"), product(11, "Стол"), product(12, "Стул"),
    ])
    labels = [item.label for item in candidates]
    assert len(labels) == len(set(labels))
    assert "Стол [10]" in labels and "Стол [11]" in labels


def test_neighbors_branch_reranks_products_before_votes(monkeypatch):
    async def search(**kwargs):
        return {"items": [
            product(1, "Стул", score=0.9, okpd2="A"),
            product(2, "Стол", score=0.85, okpd2="B"),
        ]}

    async def fake_rerank(text, products, *, truncate=False, limit=None):
        assert text == "письменный стол"
        assert truncate is True
        return [products[1], products[0]]

    seen: list[list[dict]] = []

    def pairs(items, side):
        seen.append(list(items))
        return [], False

    monkeypatch.setattr(suggest, "run_search", search)
    monkeypatch.setattr(suggest.settings, "neighbor_score_threshold", 0.74)
    monkeypatch.setattr(suggest, "_neighbor_pairs", pairs)
    monkeypatch.setattr(rerank, "rerank_products", fake_rerank)

    result = asyncio.run(suggest._neighbors_branch("письменный стол"))
    assert [item["id"] for item in result["neighbors"]] == [2, 1]
    assert [item["id"] for item in seen[0]] == [2, 1]


def test_classifiers_branch_verifies_hits_then_builds_pairs(monkeypatch):
    hits = [
        category_hit(1, "ktru", "A-1", "Стул", linkCode="A"),
        category_hit(2, "ktru", "B-1", "Стол", linkCode="B"),
    ]

    async def search(**kwargs):
        assert kwargs["limit"] == settings.jev_rerank_limit
        return {
            "okpdKtru": [{"okpd2": {"code": "A"}, "ktru": {"code": "A-1"}, "evidence": "semantic"}],
            "kpgzSpgz": [],
            "hits": hits,
        }

    async def fake_verify(text, items, *, truncate=True, limit=None):
        assert text == "молоток"
        assert truncate is True
        assert items == hits
        return [{**hits[1], "jevScore": 0.8}]

    def fake_pairs_from_hits(items, *, limit=8):
        assert [item["code"] for item in items] == ["B-1"]
        assert limit == suggest.CLASSIFIER_PAIR_LIMIT
        return {
            "okpdKtru": [{
                "okpd2": {"code": "B", "name": "B", "score": 0.8},
                "ktru": {"code": "B-1", "name": "Стол", "score": 0.8},
                "evidence": "semantic", "confident": True, "ambiguous": False,
            }],
            "kpgzSpgz": [],
        }

    monkeypatch.setattr(suggest, "search_classifiers", search)
    monkeypatch.setattr(rerank, "verify_category_hits", fake_verify)
    monkeypatch.setattr(suggest, "pairs_from_hits", fake_pairs_from_hits)
    monkeypatch.setattr(suggest, "_enrich", lambda vote, tab: vote)

    result = asyncio.run(suggest._classifiers_branch("молоток"))
    pairs, confident = result["pairs"]["okpdKtru"]
    assert [pair["ktru"]["code"] for pair in pairs] == ["B-1"]
    assert confident


def test_classifiers_branch_keeps_retrieval_when_verify_skipped(monkeypatch):
    async def search(**kwargs):
        return {
            "okpdKtru": [{
                "okpd2": {"code": "A", "name": "A", "score": 0.9},
                "ktru": {"code": "A-1", "name": "A-1", "score": 0.9},
                "evidence": "exact_name", "confident": True, "ambiguous": False,
            }],
            "kpgzSpgz": [],
            "hits": [category_hit(1, "ktru", "A-1", "A-1")],
        }

    async def fake_verify(*args, **kwargs):
        return None

    def unexpected(*args, **kwargs):
        raise AssertionError("pairs_from_hits must not run when verify is skipped")

    monkeypatch.setattr(suggest, "search_classifiers", search)
    monkeypatch.setattr(rerank, "verify_category_hits", fake_verify)
    monkeypatch.setattr(suggest, "pairs_from_hits", unexpected)
    monkeypatch.setattr(suggest, "_enrich", lambda vote, tab: vote)

    result = asyncio.run(suggest._classifiers_branch("молоток"))
    pairs, confident = result["pairs"]["okpdKtru"]
    assert pairs[0]["ktru"]["code"] == "A-1"
    assert confident


def test_find_similar_reranks_and_limits(monkeypatch):
    class FakeProduct:
        id = 99
        name = "Исходный стол"
        description = "деревянный"

    class FakeDb:
        def execute(self, sql, params=None):
            if "SELECT embedding" in sql:
                return type("R", (), {"fetchone": staticmethod(lambda: {"embedding": b"x"})})()
            return type("R", (), {"fetchall": staticmethod(lambda: [
                {"id": i, "distance": 0.1 + i * 0.01} for i in range(1, 16)
            ])})()

    monkeypatch.setattr(search_mod, "find_product", lambda product_id: FakeProduct())
    monkeypatch.setattr(search_mod, "get_db", lambda: FakeDb())
    monkeypatch.setattr(search_mod, "has_vectors", lambda db: True)
    monkeypatch.setattr(search_mod, "_hydrate_hits", lambda ranked: [
        product(row["id"], f"Товар {row['id']}", score=row["score"]) for row in ranked
    ])

    async def fake_rerank(text, products, *, truncate=False, limit=None):
        assert "Исходный стол" in text
        assert truncate is False
        assert len(products) >= 6
        return list(reversed(products))

    monkeypatch.setattr(rerank, "rerank_products", fake_rerank)

    result = asyncio.run(search_mod.find_similar(99))
    assert len(result) == 6
    assert result[0]["id"] == 15
