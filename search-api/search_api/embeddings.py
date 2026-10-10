from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import json
import struct
import tempfile
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Protocol

import pysqlite3 as sqlite3

import httpx
import lmdb
import numpy as np
import pyarrow.parquet as pq

from search_api.config import settings

_TRANSIENT_STATUS = {502, 503, 504}
_RETRY_DELAYS = (1.0, 3.0, 8.0, 20.0)

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
    """Parquet-derived float16 vectors read on demand from a mmap SQLite snapshot."""

    def __init__(self, db: sqlite3.Connection):
        self._db = db

    def __len__(self) -> int:
        return int(self._db.execute("SELECT value FROM metadata WHERE key='count'").fetchone()[0])

    def close(self) -> None:
        self._db.close()

    @classmethod
    def load(cls, path: Path) -> ParquetSkuVectorStore:
        path = path.resolve()
        fingerprint = json.dumps([str(path), path.stat().st_mtime_ns, path.stat().st_size, settings.dimensions])
        destination = settings.catalog_database.parent / (path.stem + ".vectors.sqlite")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            valid = False
            if destination.exists():
                probe = sqlite3.connect(destination.resolve().as_uri() + "?mode=ro", uri=True)
                try:
                    valid = dict(probe.execute("SELECT key,value FROM metadata")).get("fingerprint") == fingerprint
                finally:
                    probe.close()
            if not valid:
                with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".tmp") as file:
                    temporary = Path(file.name)
                writer = sqlite3.connect(temporary)
                try:
                    writer.execute("PRAGMA cache_size=-2048")
                    writer.execute("PRAGMA temp_store=FILE")
                    writer.executescript("""
                        CREATE TABLE vectors(id INTEGER PRIMARY KEY, embedding BLOB NOT NULL);
                        CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    """)
                    with pq.ParquetFile(path, memory_map=True) as parquet:
                        for batch in parquet.iter_batches(columns=["id", "embedding"], batch_size=256, use_threads=False):
                            rows = []
                            for row in batch.to_pylist():
                                blob = bytes(row["embedding"])
                                if len(blob) != settings.dimensions * 2:
                                    raise RuntimeError(f"Неверная размерность в parquet для id={row['id']}: {len(blob)}")
                                rows.append((int(row["id"]), blob))
                            with writer:
                                writer.executemany("INSERT OR REPLACE INTO vectors VALUES(?,?)", rows)
                    if fingerprint != json.dumps([str(path), path.stat().st_mtime_ns, path.stat().st_size, settings.dimensions]):
                        raise RuntimeError("SKU parquet изменился во время построения дискового индекса")
                    with writer:
                        count = writer.execute("SELECT count(*) FROM vectors").fetchone()[0]
                        writer.executemany("INSERT INTO metadata VALUES(?,?)", [("fingerprint", fingerprint), ("count", str(count))])
                except BaseException:
                    writer.close()
                    temporary.unlink(missing_ok=True)
                    raise
                else:
                    writer.close()
                temporary.replace(destination)
        db = sqlite3.connect(destination.resolve().as_uri() + "?mode=ro", uri=True)
        db.execute(f"PRAGMA mmap_size={settings.catalog_mmap_size}")
        db.execute("PRAGMA cache_size=-2048")
        db.execute("PRAGMA query_only=ON")
        return cls(db)

    def get_vectors(self, product_ids: list[int]) -> list[bytes]:
        vectors = []
        for product_id in product_ids:
            row = self._db.execute("SELECT embedding FROM vectors WHERE id=?", (int(product_id),)).fetchone()
            if row is None:
                raise RuntimeError(f"Нет эмбеддинга в SKU parquet: product_id={product_id}")
            vectors.append(_float16_to_sqlite(row[0]))
        return vectors


def cached_sku_vectors(transaction: lmdb.Transaction, product_ids: list[int]) -> list[bytes]:
    return LmdbSkuVectorStore(transaction).get_vectors(product_ids)


def open_sku_vector_store(path: Path, resources: ExitStack) -> SkuVectorStore:
    resolved = path.resolve()
    if resolved.is_file() and resolved.suffix == ".parquet":
        print(f"SKU parquet: открытие mmap-индекса {resolved}…", flush=True)
        store = ParquetSkuVectorStore.load(resolved)
        resources.callback(store.close)
        print(f"SKU mmap: {len(store):,} векторов", flush=True)
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
    if not np.isfinite(np.frombuffer(raw, dtype="<f4")).all():
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
        last_error: Exception | None = None
        for delay in (*_RETRY_DELAYS, None):
            try:
                response = await http.post(_endpoint(), json=body)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_error = exc
                if delay is None:
                    raise RuntimeError(f"Ошибка meow-embed: {exc}") from exc
                await asyncio.sleep(delay)
                continue
            if response.status_code in _TRANSIENT_STATUS and delay is not None:
                last_error = RuntimeError(
                    f"Ошибка meow-embed HTTP {response.status_code}: {response.text[:500]}"
                )
                await asyncio.sleep(delay)
                continue
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Ошибка meow-embed HTTP {response.status_code}: {response.text[:500]}"
                )
            return decode_dense(response.json(), len(texts))
        assert last_error is not None
        raise last_error

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
