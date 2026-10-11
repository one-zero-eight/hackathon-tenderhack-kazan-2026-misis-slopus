import asyncio

import pytest

from search_api import query_quality as qq
from search_api.config import settings


def _thresholds(monkeypatch):
    monkeypatch.setattr(settings, "quality_reject", 0.3)
    monkeypatch.setattr(settings, "quality_doubt", 0.5)
    monkeypatch.setattr(settings, "quality_specific", 0.3)


def test_verdict_reject_when_not_a_product(monkeypatch):
    _thresholds(monkeypatch)
    verdict, reasons = qq.verdict_from_probs({"is_product": 0.1, "specific": 0.9, "plausible": 0.9})
    assert verdict == "reject"
    assert reasons == [qq.REASON]


def test_verdict_reject_when_absurd(monkeypatch):
    _thresholds(monkeypatch)
    verdict, reasons = qq.verdict_from_probs({"is_product": 0.9, "specific": 0.9, "plausible": 0.1})
    assert verdict == "reject"
    assert reasons == [qq.REASON]


def test_verdict_suspicious_for_vague_or_doubtful_product(monkeypatch):
    _thresholds(monkeypatch)
    verdict, reasons = qq.verdict_from_probs({"is_product": 0.4, "specific": 0.9, "plausible": 0.9})
    assert verdict == "suspicious"
    assert reasons == [qq.REASON]
    verdict, reasons = qq.verdict_from_probs({"is_product": 0.9, "specific": 0.1, "plausible": 0.9})
    assert verdict == "suspicious"
    assert reasons == [qq.REASON]


def test_verdict_suspicious_when_plausibility_doubtful(monkeypatch):
    _thresholds(monkeypatch)
    verdict, reasons = qq.verdict_from_probs({"is_product": 0.9, "specific": 0.9, "plausible": 0.4})
    assert verdict == "suspicious"
    assert reasons == [qq.REASON]


def test_verdict_ok(monkeypatch):
    _thresholds(monkeypatch)
    assert qq.verdict_from_probs({"is_product": 0.9, "specific": 0.8, "plausible": 0.85}) == ("ok", [])


def test_check_disabled_is_unchecked(monkeypatch):
    monkeypatch.setattr(settings, "query_quality_enabled", False)
    result = asyncio.run(qq.check_query_quality("привет"))
    assert result == {"verdict": "unchecked", "reasons": [], "probabilities": {}, "durationMs": 0}


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
            "plausible": {"type": "noul", "noul": 0.95},
        }, "jev"

    monkeypatch.setattr(qq.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(qq, "ask_jev", ask)
    result = asyncio.run(qq.check_query_quality("молоток 500 г"))
    assert seen == [""]
    assert result["verdict"] == "ok"


def test_check_uses_jev_probabilities(monkeypatch):
    monkeypatch.setattr(settings, "query_quality_enabled", True)
    _thresholds(monkeypatch)
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
        assert set(questions) == {"is_product", "specific", "plausible"}
        return {
            "is_product": {"type": "noul", "noul": 0.95},
            "specific": {"type": "noul", "noul": 0.9},
            "plausible": {"type": "noul", "noul": 0.92},
        }, "jev-latest"

    monkeypatch.setattr(qq.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(qq, "ask_jev", ask)
    result = asyncio.run(qq.check_query_quality("молоток 500 г"))
    assert result["verdict"] == "ok"
    assert result["probabilities"] == {"is_product": 0.95, "specific": 0.9, "plausible": 0.92}
    assert isinstance(result["durationMs"], int)
    assert result["durationMs"] >= 0
