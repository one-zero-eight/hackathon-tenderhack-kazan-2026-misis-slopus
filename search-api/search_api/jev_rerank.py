"""Listwise Jev Choice rerank for similar products and classifier categories.

Reorder only: no «ни один» option and no probability cutoffs. Position/top-k cuts
belong in the caller (display limit, vote shortlist), not in the Choice itself.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

import httpx

from search_api.characteristic_autofill import AutofillConfigError, typesafe_headers, typesafe_key
from search_api.config import settings

logger = logging.getLogger("uvicorn.error")

PRODUCT_QUESTION = "Какой из товаров каталога лучше всего соответствует описанию в `товар`?"
CATEGORY_QUESTION = "Какая категория классификатора лучше всего соответствует товару в `товар`?"
PRODUCT_RULES = (
    "Опирайся на название и назначение товара, а не на частичное совпадение бренда или модели.",
    "Не выбирай вариант другого типа товара, даже если совпадают отдельные слова.",
    "Всегда выбирай наиболее близкий из перечисленных вариантов; не отказывайся от выбора.",
)
CATEGORY_RULES = (
    "Решай по виду и назначению товара, а не по цифрам, брендам и артикулам.",
    "Выбирай наиболее точную подходящую категорию; широкий раздел хуже точной позиции.",
    "Всегда выбирай наиболее близкий из перечисленных вариантов; не отказывайся от выбора.",
)
KIND_TITLES = {"okpd": "ОКПД2", "ktru": "КТРУ", "kpgz": "КПГЗ", "spgz": "СПГЗ"}


@dataclass(frozen=True)
class Candidate:
    key: str
    label: str
    evidence: str


def product_query_text(product: Any) -> str:
    if isinstance(product, dict):
        name = str(product.get("name") or "").strip()
        description = str(product.get("description") or "").strip()
    else:
        name = str(getattr(product, "name", "") or "").strip()
        description = str(getattr(product, "description", "") or "").strip()
    return " ".join(part for part in (name, description) if part)[:500]


def candidates_from_products(products: list[dict], *, limit: int | None = None) -> list[Candidate]:
    cap = settings.jev_rerank_limit if limit is None else limit
    rows = products[:cap]
    names = [str(item.get("name") or "").strip() or f"#{item.get('id')}" for item in rows]
    clash = {name for name in names if names.count(name) > 1}
    out: list[Candidate] = []
    for item, name in zip(rows, names):
        key = str(item["id"])
        label = f"{name[:140]} [{key}]" if name in clash else name[:160]
        parts: list[str] = []
        okpd = str(item.get("okpd2Name") or item.get("okpd2") or "").strip()
        if okpd:
            parts.append(okpd[:70])
        ktru = str(item.get("ktruCode") or "").strip()
        if ktru:
            parts.append(f"КТРУ {ktru}")
        chars = [
            str(char.get("name") or "").strip()[:30]
            for char in (item.get("characteristics") or [])[:5]
            if str(char.get("name") or "").strip()
        ]
        if chars:
            parts.append("хар-ки: " + ", ".join(chars))
        out.append(Candidate(key=key, label=label, evidence="; ".join(parts) or name[:80]))
    return out


def candidates_from_category_hits(hits: list[dict], *, limit: int | None = None) -> list[Candidate]:
    cap = settings.jev_rerank_limit if limit is None else limit
    rows = hits[:cap]
    bases: list[str] = []
    for hit in rows:
        kind = KIND_TITLES.get(str(hit.get("kind") or ""), str(hit.get("kind") or "кат"))
        code = str(hit.get("code") or "").strip()
        name = str(hit.get("name") or code).strip()
        bases.append(f"{kind} {code} {name}".strip()[:160] or f"#{hit.get('id')}")
    clash = {base for base in bases if bases.count(base) > 1}
    out: list[Candidate] = []
    for hit, base in zip(rows, bases):
        key = str(hit["id"])
        label = f"{base[:140]} [{key}]" if base in clash else base
        parts = [f"evidence={hit.get('evidence') or 'unknown'}"]
        if hit.get("isTemplate"):
            parts.append("шаблон")
        if hit.get("linkCode"):
            parts.append(f"связь {hit['linkCode']}")
        out.append(Candidate(key=key, label=label, evidence="; ".join(parts)))
    return out


def _build_question(
    candidates: list[Candidate],
    question: str,
    *,
    rules: tuple[str, ...] = PRODUCT_RULES,
) -> dict[str, Any]:
    return {
        "type": "choice",
        "instructions": {"question": question, "правило": list(rules)},
        "criteria": {item.label: item.evidence for item in candidates},
    }


async def _ask_questions(text: str, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    key = typesafe_key()
    base_url = settings.typesafe_base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=settings.typesafe_timeout) as client:
        try:
            response = await client.post(
                f"{base_url}/v1/systemone",
                headers=typesafe_headers(key),
                json={
                    "model": settings.typesafe_model,
                    "state": {"товар": text},
                    "questions": questions,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise RuntimeError("TypeSafe не ответил вовремя") from exc
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"TypeSafe API: HTTP {exc.response.status_code}") from exc
        except (httpx.RequestError, ValueError) as exc:
            raise RuntimeError("Не удалось получить ответ TypeSafe") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("answers"), dict):
        raise RuntimeError("TypeSafe вернул некорректный ответ")
    if set(payload["answers"]) != set(questions):
        raise RuntimeError("TypeSafe вернул неполный набор ответов")
    probabilities: dict[str, dict[str, Any]] = {}
    for qid, question in questions.items():
        answer = payload["answers"][qid]
        if not isinstance(answer, dict) or answer.get("choice") not in question["criteria"]:
            raise RuntimeError("TypeSafe вернул недопустимый вариант")
        probs = answer.get("probabilities")
        if not isinstance(probs, dict) or set(probs) != set(question["criteria"]):
            raise RuntimeError("TypeSafe вернул неполный набор вероятностей")
        for score in probs.values():
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
                raise RuntimeError("TypeSafe вернул некорректную вероятность")
        probabilities[qid] = probs
    return probabilities


def _rank_side(
    candidates: list[Candidate],
    probabilities: dict[str, Any],
) -> list[tuple[str, float]]:
    return sorted(
        ((item.key, float(probabilities.get(item.label, 0.0))) for item in candidates),
        key=lambda pair: (-pair[1], pair[0]),
    )


async def rerank_candidates(
    text: str,
    candidates: list[Candidate],
    *,
    question: str = PRODUCT_QUESTION,
    rules: tuple[str, ...] = PRODUCT_RULES,
) -> list[tuple[str, float]] | None:
    """Rank candidates by one System One choice. None means keep the input order."""
    if not settings.jev_rerank_enabled or not candidates or not text.strip():
        return None
    if len(candidates) > settings.jev_rerank_limit:
        candidates = candidates[: settings.jev_rerank_limit]
    labels = [item.label for item in candidates]
    if len(set(labels)) != len(labels):
        raise ValueError("Метки кандидатов для реранка должны быть уникальны")
    try:
        probabilities = await _ask_questions(
            text, {"q": _build_question(candidates, question, rules=rules)},
        )
    except (AutofillConfigError, RuntimeError, ValueError) as exc:
        logger.warning("Jev rerank skipped: %s", exc)
        return None
    return _rank_side(candidates, probabilities["q"])


def _apply_product_ranks(
    products: list[dict],
    ranked: list[tuple[str, float]],
) -> list[dict]:
    by_id = {str(item["id"]): item for item in products}
    out: list[dict] = []
    for key, score in ranked:
        item = by_id.get(key)
        if item is None:
            continue
        updated = dict(item)
        updated["score"] = score
        out.append(updated)
    return out


def _apply_hit_ranks(
    hits: list[dict],
    ranked: list[tuple[str, float]],
) -> list[dict]:
    by_id = {str(hit["id"]): hit for hit in hits}
    out: list[dict] = []
    for key, score in ranked:
        hit = by_id.get(key)
        if hit is None:
            continue
        updated = dict(hit)
        updated["jevScore"] = score
        out.append(updated)
    return out


async def rerank_suggest_shortlists(
    text: str,
    *,
    products: list[dict],
    category_hits: list[dict],
) -> tuple[list[dict] | None, list[dict] | None]:
    """One System One request: listwise choice over STE neighbors and/or category names.

    Each side is None when that side was skipped (keep retrieval order / pairs).
    """
    if not settings.jev_rerank_enabled or not text.strip():
        return None, None
    product_candidates = candidates_from_products(products) if products else []
    hit_candidates = candidates_from_category_hits(category_hits) if category_hits else []
    questions: dict[str, dict[str, Any]] = {}
    if product_candidates:
        labels = [item.label for item in product_candidates]
        if len(set(labels)) != len(labels):
            raise ValueError("Метки товаров для реранка должны быть уникальны")
        questions["neighbors"] = _build_question(
            product_candidates, PRODUCT_QUESTION, rules=PRODUCT_RULES,
        )
    if hit_candidates:
        labels = [item.label for item in hit_candidates]
        if len(set(labels)) != len(labels):
            raise ValueError("Метки категорий для проверки должны быть уникальны")
        questions["categories"] = _build_question(
            hit_candidates, CATEGORY_QUESTION, rules=CATEGORY_RULES,
        )
    if not questions:
        return None, None
    try:
        probabilities = await _ask_questions(text, questions)
    except (AutofillConfigError, RuntimeError, ValueError) as exc:
        logger.warning("Jev suggest rerank skipped: %s", exc)
        return None, None
    products_out = None
    if "neighbors" in probabilities:
        ranked = _rank_side(product_candidates, probabilities["neighbors"])
        products_out = _apply_product_ranks(products, ranked)
    hits_out = None
    if "categories" in probabilities:
        ranked = _rank_side(hit_candidates, probabilities["categories"])
        hits_out = _apply_hit_ranks(category_hits, ranked)
    return products_out, hits_out


async def rerank_products(
    text: str,
    products: list[dict],
    *,
    limit: int | None = None,
) -> list[dict]:
    """Reorder product dicts; scores become Jev probabilities when rerank succeeds."""
    if not products:
        return products
    candidates = candidates_from_products(products, limit=limit)
    ranked = await rerank_candidates(text, candidates)
    if ranked is None:
        return products[: len(candidates)] if limit is not None else products
    return _apply_product_ranks(products, ranked)


async def verify_category_hits(
    text: str,
    hits: list[dict],
    *,
    limit: int | None = None,
) -> list[dict] | None:
    """Reorder classifier name hits (tests / standalone). Prefer rerank_suggest_shortlists in suggest."""
    products_out, hits_out = await rerank_suggest_shortlists(
        text, products=[], category_hits=hits[: limit or settings.jev_rerank_limit],
    )
    del products_out
    return hits_out
