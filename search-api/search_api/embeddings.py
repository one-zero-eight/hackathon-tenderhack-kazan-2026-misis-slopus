from __future__ import annotations

import base64
import struct
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
import numpy as np

from search_api.config import settings

_query_cache: dict[str, bytes] = {}


def _endpoint() -> str:
    return f"{settings.meow_embed_url.rstrip('/')}/embed"


def decode_dense(payload: dict, count: int) -> list[bytes]:
    dense = payload.get("dense") or {}
    if (
        dense.get("model_id") != settings.model_id
        or dense.get("dtype") != "float32"
        or dense.get("shape") != [count, settings.dimensions]
    ):
        raise RuntimeError("meow-embed вернул неверную модель, размерность или формат векторов")
    raw = base64.b64decode(dense["data"])
    vector_bytes = settings.dimensions * 4
    if len(raw) != count * vector_bytes:
        raise RuntimeError("meow-embed вернул неполный набор векторов")
    for i in range(0, len(raw), 4):
        if not np.isfinite(struct.unpack_from("<f", raw, i)[0]):
            raise RuntimeError("meow-embed вернул нечисловой вектор")
    return [raw[i : i + vector_bytes] for i in range(0, len(raw), vector_bytes)]


@asynccontextmanager
async def embed_client(timeout: float = 600.0) -> AsyncIterator[httpx.AsyncClient]:
    limits = httpx.Limits(max_connections=32, max_keepalive_connections=32)
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        yield client


async def embed_texts(
    texts: list[str],
    task: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[bytes]:
    if not texts:
        return []
    prompt = settings.query_prompt if task == "query" else settings.document_prompt
    timeout = 60.0 if task == "query" else 600.0
    body = {
        "dense_model_id": settings.model_id,
        "dense_task": task,
        "dense_prompt": prompt,
        "texts": texts,
    }

    async def _post(http: httpx.AsyncClient) -> list[bytes]:
        response = await http.post(_endpoint(), json=body)
        if response.status_code >= 400:
            raise RuntimeError(f"Ошибка meow-embed HTTP {response.status_code}: {response.text[:500]}")
        return decode_dense(response.json(), len(texts))

    if client is not None:
        return await _post(client)
    async with embed_client(timeout=timeout) as owned:
        return await _post(owned)


async def embed_query(query: str) -> bytes:
    cached = _query_cache.get(query)
    if cached is not None:
        return cached
    vector = (await embed_texts([query], "query"))[0]
    _query_cache[query] = vector
    if len(_query_cache) > 128:
        _query_cache.pop(next(iter(_query_cache)))
    return vector
