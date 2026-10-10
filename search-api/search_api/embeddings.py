from __future__ import annotations

import base64
import hashlib
import struct
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Mapping, Protocol

import httpx
import lmdb
import numpy as np
import pyarrow.parquet as pq

from search_api.config import settings

_query_cache: dict[str, bytes] = {}


def _float16_to_sqlite(raw: bytes) -> bytes:
    if len(raw) != settings.dimensions * 2:
        raise RuntimeError(f"Неверная размерность вектора: {len(raw)} байт")
    vector = np.frombuffer(raw, dtype="<f2").astype("<f4")
    if not np.isfinite(vector).all():
        raise RuntimeError("Нечисловой вектор в кэше эмбеддингов")
    return vector.tobytes()


def cached_document_vectors(transaction: lmdb.Transaction, texts: list[str]) -> list[bytes]:
    vectors = []
    for text in texts:
        key = hashlib.blake2b(
            b"\0".join([
                b"dense", settings.model_id.encode("utf-8"), (0).to_bytes(4, "little"),
                b"dense_prompt", settings.document_prompt.encode("utf-8"),
                b"dense_task", b"document", text.encode("utf-8"),
            ]),
            digest_size=32,
        ).digest()
        raw = transaction.get(key)
        if raw is None:
            raise RuntimeError(f"Нет эмбеддинга в LMDB-кеше: {text[:160]}")
        vectors.append(_float16_to_sqlite(raw))
    return vectors


def sku_embedding_key(product_id: int) -> bytes:
    return struct.pack("<Q", int(product_id))


class SkuVectorStore(Protocol):
    def get_vectors(self, product_ids: list[int]) -> list[bytes]: ...


class LmdbSkuVectorStore:
    def __init__(self, transaction: lmdb.Transaction):
        self._txn = transaction

    def get_vectors(self, product_ids: list[int]) -> list[bytes]:
        vectors = []
        for product_id in product_ids:
            raw = self._txn.get(sku_embedding_key(product_id))
            if raw is None:
                raise RuntimeError(f"Нет эмбеддинга в SKU LMDB: product_id={product_id}")
            vectors.append(_float16_to_sqlite(raw))
        return vectors


class ParquetSkuVectorStore:
    """In-memory map loaded once from Git-LFS parquet (id + float16 bytes)."""

    def __init__(self, by_id: Mapping[int, bytes]):
        self._by_id = by_id

    def __len__(self) -> int:
        return len(self._by_id)

    @classmethod
    def load(cls, path: Path) -> ParquetSkuVectorStore:
        table = pq.read_table(path, columns=["id", "embedding"])
        expected = settings.dimensions * 2
        by_id: dict[int, bytes] = {}
        for batch in table.to_batches():
            ids = batch.column(0).to_pylist()
            embeddings = batch.column(1).to_pylist()
            for product_id, raw in zip(ids, embeddings, strict=True):
                blob = bytes(raw) if not isinstance(raw, (bytes, bytearray)) else bytes(raw)
                if len(blob) != expected:
                    raise RuntimeError(f"Неверная размерность в parquet для id={product_id}: {len(blob)}")
                by_id[int(product_id)] = blob
        return cls(by_id)

    def get_vectors(self, product_ids: list[int]) -> list[bytes]:
        vectors = []
        for product_id in product_ids:
            raw = self._by_id.get(int(product_id))
            if raw is None:
                raise RuntimeError(f"Нет эмбеддинга в SKU parquet: product_id={product_id}")
            vectors.append(_float16_to_sqlite(raw))
        return vectors


def cached_sku_vectors(transaction: lmdb.Transaction, product_ids: list[int]) -> list[bytes]:
    return LmdbSkuVectorStore(transaction).get_vectors(product_ids)


def open_sku_vector_store(path: Path, resources: ExitStack) -> SkuVectorStore:
    resolved = path.resolve()
    if resolved.is_file() and resolved.suffix == ".parquet":
        print(f"SKU parquet: загрузка {resolved}…", flush=True)
        store = ParquetSkuVectorStore.load(resolved)
        print(f"SKU parquet: {len(store):,} векторов", flush=True)
        return store

    cache = lmdb.open(str(resolved), readonly=True, lock=False, create=False)
    resources.callback(cache.close)
    txn = resources.enter_context(cache.begin())
    return LmdbSkuVectorStore(txn)


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
