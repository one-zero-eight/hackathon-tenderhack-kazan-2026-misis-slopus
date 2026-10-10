import asyncio

import pytest

from search_api import query_quality as qq
from search_api.config import settings


def test_verdict_reject_when_not_a_product(monkeypatch):
    monkeypatch.setattr(settings, "quality_reject", 0.3)
    monkeypatch.setattr(settings, "quality_doubt", 0.5)
    monkeypatch.setattr(settings, "quality_specific", 0.3)
    verdict, reasons = qq.verdict_from_probs({"is_product": 0.1, "specific": 0.9})
    assert verdict == "reject"
    assert reasons == [qq.QUESTIONS["is_product"][1]]


def test_verdict_suspicious_for_vague_or_doubtful_product(monkeypatch):
    monkeypatch.setattr(settings, "quality_reject", 0.3)
    monkeypatch.setattr(settings, "quality_doubt", 0.5)
    monkeypatch.setattr(settings, "quality_specific", 0.3)
    verdict, reasons = qq.verdict_from_probs({"is_product": 0.4, "specific": 0.9})
    assert verdict == "suspicious"
    assert qq.QUESTIONS["is_product"][1] in reasons
    verdict, reasons = qq.verdict_from_probs({"is_product": 0.9, "specific": 0.1})
    assert verdict == "suspicious"
    assert reasons == [qq.QUESTIONS["specific"][1]]


def test_verdict_ok(monkeypatch):
    monkeypatch.setattr(settings, "quality_reject", 0.3)
    monkeypatch.setattr(settings, "quality_doubt", 0.5)
    monkeypatch.setattr(settings, "quality_specific", 0.3)
    assert qq.verdict_from_probs({"is_product": 0.9, "specific": 0.8}) == ("ok", [])


def test_check_disabled_is_unchecked(monkeypatch):
    monkeypatch.setattr(settings, "query_quality_enabled", False)
    result = asyncio.run(qq.check_query_quality("привет"))
    assert result == {"verdict": "unchecked", "reasons": [], "probabilities": {}}


def test_check_empty_key_still_calls_jev(monkeypatch):
    """InnoHassle TypeSafe does not require a bearer token."""
    monkeypatch.setattr(settings, "query_quality_enabled", True)
    monkeypatch.setattr(qq, "typesafe_key", lambda: "")
    seen: list[str] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    async def ask(client, key, state, questions, usage):
        seen.append(key)
        return {
            "is_product": {"type": "noul", "noul": 0.95},
            "specific": {"type": "noul", "noul": 0.9},
        }, "jev"

    monkeypatch.setattr(qq.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(qq, "ask_jev", ask)
    result = asyncio.run(qq.check_query_quality("молоток 500 г"))
    assert seen == [""]
    assert result["verdict"] == "ok"


def test_check_uses_jev_probabilities(monkeypatch):
    monkeypatch.setattr(settings, "query_quality_enabled", True)
    monkeypatch.setattr(settings, "quality_reject", 0.3)
    monkeypatch.setattr(settings, "quality_doubt", 0.5)
    monkeypatch.setattr(settings, "quality_specific", 0.3)
    monkeypatch.setattr(qq, "typesafe_key", lambda: "token")

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    async def ask(client, key, state, questions, usage):
        assert state == {"текст": "молоток 500 г"}
        assert set(questions) == {"is_product", "specific"}
        return {
            "is_product": {"type": "noul", "noul": 0.95},
            "specific": {"type": "noul", "noul": 0.9},
        }, "jev-latest"

    monkeypatch.setattr(qq.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(qq, "ask_jev", ask)
    result = asyncio.run(qq.check_query_quality("молоток 500 г"))
    assert result["verdict"] == "ok"
    assert result["probabilities"] == {"is_product": 0.95, "specific": 0.9}
