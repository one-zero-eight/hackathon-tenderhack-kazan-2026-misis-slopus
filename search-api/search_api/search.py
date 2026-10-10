from __future__ import annotations

import logging
import re
import time
from typing import Any, Literal

import httpx

from search_api.config import settings
from search_api.database import get_db, has_vectors
from search_api.embeddings import embed_query, embed_texts
from search_api.ste_catalog import (
    category_stats,
    category_tree,
    find_product,
    hydrate,
    list_products,
    product_count,
    product_to_dict,
    resolve_category_ids,
)

logger = logging.getLogger("uvicorn.error")

SearchMode = Literal["hybrid", "semantic", "text"]


def fts_query(query: str) -> str:
    tokens = list(dict.fromkeys(re.findall(r"[\w]+", query.lower(), flags=re.UNICODE)))[:32]
    return " OR ".join(f'"{token}"*' for token in tokens)


def fuse_ranks(semantic: list[dict], textual: list[dict]) -> list[dict]:
    ranks: dict[int, dict[str, Any]] = {}
    for index, item in enumerate(semantic):
        ranks[item["id"]] = {"rank": 1 / (60 + index + 1), "score": 1 - item["distance"], "match": "semantic"}
    for index, item in enumerate(textual):
        existing = ranks.get(item["id"])
        ranks[item["id"]] = {
            "rank": (existing["rank"] if existing else 0) + 1 / (60 + index + 1),
            "score": existing["score"] if existing else None,
            "match": "both" if existing else "text",
        }
    ordered = sorted(ranks.items(), key=lambda pair: (-pair[1]["rank"], pair[0]))
    return [{"id": product_id, "score": meta["score"], "match": meta["match"]} for product_id, meta in ordered]


def _hydrate_hits(ranked: list[dict]) -> list[dict]:
    products = {p.id: p for p in hydrate([row["id"] for row in ranked])}
    out = []
    for row in ranked:
        product = products.get(row["id"])
        if not product:
            continue
        hit = product_to_dict(product)
        hit["score"] = row["score"]
        hit["match"] = row["match"]
        out.append(hit)
    return out


def validate_search(query: str, mode: str, page: int, category_id: int | None) -> tuple[str, SearchMode, int, int | None]:
    if not isinstance(query, str) or len(query) > 500:
        raise ValueError("Запрос должен содержать не более 500 символов")
    if mode not in ("text", "semantic", "hybrid"):
        raise ValueError("Неизвестный режим поиска")
    if not isinstance(page, int) or page < 1 or page > 10000:
        raise ValueError("Некорректная страница")
    if category_id is not None and (
        not isinstance(category_id, int)
        or (category_id < 0 and category_id != -1)
    ):
        raise ValueError("Некорректная категория")
    return query.strip(), mode, page, category_id  # type: ignore[return-value]


async def run_search(
    *,
    query: str,
    mode: SearchMode = "hybrid",
    page: int = 1,
    category_id: int | None = None,
) -> dict:
    query, mode, page, category_id = validate_search(query, mode, page, category_id)
    started = time.perf_counter()
    timings: dict[str, int] = {}
    offset = (page - 1) * settings.page_size
    db = get_db()
    category_ids = resolve_category_ids(category_id)
    category_list = sorted(category_ids) if category_ids is not None else None
    timings["setup"] = round((time.perf_counter() - started) * 1000)

    def hydrate_hits(ranked: list[dict]) -> list[dict]:
        stage = time.perf_counter()
        items = _hydrate_hits(ranked)
        timings["hydrate"] = round((time.perf_counter() - stage) * 1000)
        return items

    def finish(items: list[dict], total: int) -> dict:
        duration_ms = round((time.perf_counter() - started) * 1000)
        logger.info("Search mode=%s duration_ms=%s stages_ms=%s", mode, duration_ms, timings)
        return {
            "items": items,
            "total": total,
            "hasMore": offset + len(items) < total,
            "page": page,
            "mode": mode,
            "durationMs": duration_ms,
            "timingsMs": dict(timings),
        }

    if not query:
        items, total = list_products(category_id=category_id, offset=offset, limit=settings.page_size)
        return finish([{**product_to_dict(p), "score": None, "match": "catalog"} for p in items], total)

    if query.isdigit():
        product = find_product(int(query))
        if product and (category_ids is None or product.category_id in category_ids):
            return finish(
                [{**product_to_dict(product), "score": None, "match": "text"}] if page == 1 else [],
                1,
            )

    tokens = fts_query(query)
    if category_list is None:
        category_sql = ""
        category_params: list[Any] = []
    else:
        placeholders = ",".join("?" * len(category_list))
        category_sql = f" AND s.category_id IN ({placeholders})"
        category_params = list(category_list)

    def text_rows(limit: int, skip: int = 0) -> list[dict]:
        if not tokens:
            return []
        sql = f"""
            SELECT products_fts.rowid AS id FROM products_fts
            JOIN index_state s ON s.product_id = products_fts.rowid
            WHERE products_fts MATCH ?{category_sql}
            ORDER BY bm25(products_fts,8,3,4,1,2,1), products_fts.rowid
            LIMIT ? OFFSET ?
        """
        return [{"id": int(row["id"])} for row in db.execute(sql, [tokens, *category_params, limit, skip]).fetchall()]

    if mode == "text":
        stage = time.perf_counter()
        total = 0
        if tokens:
            total = int(db.execute(
                f"""
                SELECT count(*) AS n FROM products_fts
                JOIN index_state s ON s.product_id = products_fts.rowid
                WHERE products_fts MATCH ?{category_sql}
                """,
                [tokens, *category_params],
            ).fetchone()["n"])
        ranked = [{"id": row["id"], "score": None, "match": "text"} for row in text_rows(settings.page_size, offset)]
        timings["text"] = round((time.perf_counter() - stage) * 1000)
        return finish(hydrate_hits(ranked), total)

    def text_page() -> dict[str, Any]:
        nonlocal mode
        mode = "text"
        stage_local = time.perf_counter()
        total = 0
        if tokens:
            total = int(db.execute(
                f"""
                SELECT count(*) AS n FROM products_fts
                JOIN index_state s ON s.product_id = products_fts.rowid
                WHERE products_fts MATCH ?{category_sql}
                """,
                [tokens, *category_params],
            ).fetchone()["n"])
        ranked = [{"id": row["id"], "score": None, "match": "text"} for row in text_rows(settings.page_size, offset)]
        timings["text"] = round((time.perf_counter() - stage_local) * 1000)
        return finish(hydrate_hits(ranked), total)

    stage = time.perf_counter()
    vectors_ready = has_vectors(db)
    timings["vectorCheck"] = round((time.perf_counter() - stage) * 1000)
    if not vectors_ready:
        # Vectors not ready yet — fall back to FTS instead of failing the request.
        return text_page()

    stage = time.perf_counter()
    try:
        vector = await embed_query(query)
    except (httpx.HTTPError, RuntimeError, TimeoutError, OSError) as exc:
        logger.warning("Embedding unavailable, falling back to text search: %s", exc)
        timings["embedding"] = round((time.perf_counter() - stage) * 1000)
        timings["embeddingFallback"] = 1
        return text_page()
    timings["embedding"] = round((time.perf_counter() - stage) * 1000)
    if category_list is None:
        vec_sql = """
            SELECT product_id AS id, distance FROM product_vectors
            WHERE embedding MATCH ? AND k = ?
            ORDER BY distance
        """
        vec_params: list[Any] = [vector, settings.retrieval_limit]
    else:
        placeholders = ",".join("?" * len(category_list))
        vec_sql = f"""
            SELECT product_id AS id, distance FROM product_vectors
            WHERE embedding MATCH ? AND k = ?
              AND category_id IN ({placeholders})
            ORDER BY distance
        """
        vec_params = [vector, settings.retrieval_limit, *category_list]
    stage = time.perf_counter()
    vector_rows = [{"id": int(r["id"]), "distance": float(r["distance"])} for r in db.execute(vec_sql, vec_params).fetchall()]
    timings["knn"] = round((time.perf_counter() - stage) * 1000)

    stage = time.perf_counter()
    if mode == "hybrid":
        ranked = fuse_ranks(vector_rows, text_rows(settings.retrieval_limit))[: settings.retrieval_limit]
    else:
        ranked = [{"id": row["id"], "score": 1 - row["distance"], "match": "semantic"} for row in vector_rows]

    timings["ranking"] = round((time.perf_counter() - stage) * 1000)
    page_rows = ranked[offset: offset + settings.page_size]
    return finish(hydrate_hits(page_rows), len(ranked))


SIMILAR_RETRIEVAL_K = 16
SIMILAR_RESULT_LIMIT = 6


async def find_similar(product_id: int) -> list[dict]:
    from search_api.jev_rerank import product_query_text, rerank_products

    product = find_product(product_id)
    db = get_db()
    if product is None or not has_vectors(db):
        return []
    row = db.execute("SELECT embedding FROM product_vectors WHERE product_id = ?", (product_id,)).fetchone()
    if row is not None:
        vector = row["embedding"]
    else:
        from search_api.normalize import embedding_document
        vector = (await embed_texts([embedding_document(product)], "document"))[0]
    neighbors = db.execute(
        """
        SELECT product_id AS id, distance FROM product_vectors
        WHERE embedding MATCH ? AND k = ? ORDER BY distance
        """,
        (vector, SIMILAR_RETRIEVAL_K),
    ).fetchall()
    ranked = [
        {"id": int(r["id"]), "score": 1 - float(r["distance"]), "match": "semantic"}
        for r in neighbors if int(r["id"]) != product_id
    ][: SIMILAR_RETRIEVAL_K - 1]
    hits = _hydrate_hits(ranked)
    hits = await rerank_products(product_query_text(product), hits, truncate=False)
    return hits[:SIMILAR_RESULT_LIMIT]


def catalog_stats() -> dict:
    db = get_db()
    embedded = int(db.execute(
        "SELECT count(*) AS n FROM index_state WHERE embedded_hash IS NOT NULL AND embedded_hash = content_hash"
    ).fetchone()["n"])
    last = db.execute("SELECT value FROM metadata WHERE key='last_imported_at'").fetchone()
    return {
        "products": product_count(),
        "embedded": embedded,
        "model": settings.model_id,
        "dimensions": settings.dimensions,
        "categories": category_stats(),
        "categoryTree": category_tree(),
        "lastImportedAt": last["value"] if last else None,
    }
