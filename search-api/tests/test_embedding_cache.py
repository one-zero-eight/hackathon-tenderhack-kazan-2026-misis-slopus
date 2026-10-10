import base64
import hashlib

import lmdb
import numpy as np
import pytest

from search_api.config import settings
from search_api.embeddings import (
    ParquetSkuVectorStore,
    cached_document_vectors,
    cached_sku_vectors,
    decode_dense,
    sku_embedding_key,
)


@pytest.mark.parametrize("invalid", [np.nan, np.inf, -np.inf])
def test_dense_response_rejects_nonfinite_values(invalid):
    vector = np.ones(settings.dimensions, dtype="<f4")
    vector[-1] = invalid
    payload = {"dense": {
        "model_id": settings.model_id, "dtype": "float32",
        "shape": [1, settings.dimensions],
        "data": base64.b64encode(vector.tobytes()).decode(),
    }}
    with pytest.raises(RuntimeError, match="нечисловой вектор"):
        decode_dense(payload, 1)


def test_dense_response_preserves_float32_vectors():
    vectors = np.arange(settings.dimensions * 2, dtype="<f4").reshape(2, -1)
    payload = {"dense": {
        "model_id": settings.model_id, "dtype": "float32",
        "shape": [2, settings.dimensions],
        "data": base64.b64encode(vectors.tobytes()).decode(),
    }}
    assert decode_dense(payload, 2) == [vector.tobytes() for vector in vectors]


def cache_key(text):
    parts = [
        b"dense", settings.model_id.encode(), b"\0\0\0\0",
        b"dense_prompt", settings.document_prompt.encode(),
        b"dense_task", b"document", text.encode(),
    ]
    digest = hashlib.blake2b(digest_size=32)
    for index, part in enumerate(parts):
        if index:
            digest.update(b"\0")
        digest.update(part)
    return digest.digest()


def test_reads_meow_float16_as_sqlite_float32(tmp_path):
    text = "title: Молоток | text: Категория: Инструменты"
    vector = np.linspace(-1, 1, settings.dimensions).astype("<f2")
    with lmdb.open(str(tmp_path / "cache")) as cache:
        with cache.begin(write=True) as transaction:
            transaction.put(cache_key(text), vector.tobytes())
        with cache.begin() as transaction:
            result = cached_document_vectors(transaction, [text, text])
    assert result == [vector.astype("<f4").tobytes()] * 2


def test_missing_document_fails_without_remote_request(tmp_path):
    with lmdb.open(str(tmp_path / "cache")) as cache:
        with cache.begin() as transaction:
            with pytest.raises(RuntimeError, match="Нет эмбеддинга"):
                cached_document_vectors(transaction, ["missing"])


@pytest.mark.parametrize("raw", [b"bad", np.full(settings.dimensions, np.nan, dtype="<f2").tobytes()])
def test_invalid_cached_vector_fails(tmp_path, raw):
    with lmdb.open(str(tmp_path / "cache")) as cache:
        with cache.begin(write=True) as transaction:
            transaction.put(cache_key("bad"), raw)
        with cache.begin() as transaction:
            with pytest.raises(RuntimeError):
                cached_document_vectors(transaction, ["bad"])


def test_reads_sku_float16_as_sqlite_float32(tmp_path):
    vector = np.linspace(-1, 1, settings.dimensions).astype("<f2")
    with lmdb.open(str(tmp_path / "sku")) as cache:
        with cache.begin(write=True) as transaction:
            transaction.put(sku_embedding_key(42), vector.tobytes())
        with cache.begin() as transaction:
            result = cached_sku_vectors(transaction, [42, 42])
    assert result == [vector.astype("<f4").tobytes()] * 2


def test_missing_sku_fails(tmp_path):
    with lmdb.open(str(tmp_path / "sku")) as cache:
        with cache.begin() as transaction:
            with pytest.raises(RuntimeError, match="SKU LMDB"):
                cached_sku_vectors(transaction, [1])


def test_parquet_sku_store_roundtrip(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    vector = np.linspace(-1, 1, settings.dimensions).astype("<f2")
    path = tmp_path / "sku.parquet"
    pq.write_table(
        pa.table({
            "id": pa.array([7], type=pa.int64()),
            "embedding": pa.array([vector.tobytes()], type=pa.binary(settings.dimensions * 2)),
        }),
        path,
    )
    store = ParquetSkuVectorStore.load(path)
    assert store.get_vectors([7]) == [vector.astype("<f4").tobytes()]
