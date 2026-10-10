"""Векторные представления фрагментов и значений."""

from __future__ import annotations

import os
from typing import Protocol

import numpy as np

DEFAULT_MODEL = os.environ.get("KTRU_EMBED_MODEL", "intfloat/multilingual-e5-small")

# Модели обучены с префиксами задачи. Сравнение у нас симметричное (фрагмент ↔ значение,
# запрос ↔ название), поэтому берём «запросный»/«сходственный» вариант.
_PREFIXES = {
    "e5": "query: ",
    "embeddinggemma": "task: sentence similarity | query: ",
}


def prefix_for(model_name: str) -> str:
    name = model_name.lower()
    return next((p for key, p in _PREFIXES.items() if key in name), "")


class Embedder(Protocol):
    def encode(self, texts: list[str]) -> np.ndarray:
        """Нормированные векторы, по строке на текст."""
        ...


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str = DEFAULT_MODEL):
        from sentence_transformers import SentenceTransformer

        import torch

        # На CPU без AMX bfloat16 медленнее float32 в разы (EmbeddingGemma 2: 4 → 18 текстов/с).
        self.model = SentenceTransformer(model_name, device="cpu", model_kwargs={"torch_dtype": torch.float32})
        # Фрагменты, значения и названия короткие; без предела длинное описание раздувает память.
        self.model.max_seq_length = min(self.model.max_seq_length or 256, 256)
        self.prefix = prefix_for(model_name)
        self._cache: dict[str, np.ndarray] = {}

    def encode(self, texts: list[str]) -> np.ndarray:
        missing = [t for t in dict.fromkeys(texts) if t not in self._cache]
        if missing:
            vecs = self.model.encode(
                [self.prefix + t for t in missing], normalize_embeddings=True, batch_size=64
            )
            self._cache.update(zip(missing, vecs))
        if not texts:
            return np.zeros((0, 1), dtype=np.float32)
        return np.stack([self._cache[t] for t in texts])


class RemoteEmbedder:
    """Векторы с сервера meow-embed (POST {url}/embed): локально ничего не считается.

    Модель — EmbeddingGemma-2 (задача «сходство предложений», как в `_PREFIXES`); батчи по `batch_size` текстов."""

    MODEL = "google/embeddinggemma-2"

    def __init__(self, url: str, *, model_id: str = MODEL, dimensions: int = 768, batch_size: int = 64,
                 timeout: float = 60.0, client=None):
        self.url, self.model_id, self.dimensions = url.rstrip("/"), model_id, dimensions
        self.batch_size, self.timeout, self._client = batch_size, timeout, client
        self.prefix = prefix_for(model_id)
        self._cache: dict[str, np.ndarray] = {}

    def _embed(self, texts: list[str]) -> np.ndarray:
        import base64

        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=self.timeout)
        body = {"dense_model_id": self.model_id, "dense_task": "query", "dense_prompt": self.prefix, "texts": texts}
        response = self._client.post(f"{self.url}/embed", json=body)
        if response.status_code >= 400:
            raise RuntimeError(f"meow-embed HTTP {response.status_code}: {response.text[:200]}")
        dense = response.json().get("dense") or {}
        if dense.get("model_id") != self.model_id or dense.get("dtype") != "float32" \
                or dense.get("shape") != [len(texts), self.dimensions]:
            raise RuntimeError("meow-embed вернул неверную модель, размерность или формат векторов")
        vectors = np.frombuffer(base64.b64decode(dense["data"]), dtype="<f4").astype(np.float32)
        vectors = vectors.reshape(len(texts), self.dimensions)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if not np.isfinite(vectors).all() or (norms == 0).any():
            raise RuntimeError("meow-embed вернул нулевой или нечисловой вектор")
        return vectors / norms

    def encode(self, texts: list[str]) -> np.ndarray:
        missing = [t for t in dict.fromkeys(texts) if t not in self._cache]
        for i in range(0, len(missing), self.batch_size):
            batch = missing[i:i + self.batch_size]
            self._cache.update(zip(batch, self._embed(batch)))
        if not texts:
            return np.zeros((0, self.dimensions), dtype=np.float32)
        return np.stack([self._cache[t] for t in texts])


def load_default() -> Embedder | None:
    """`KTRU_EMBED_URL` (в окружении или .env) — векторы с сервера meow-embed, локально не считаются.
    Иначе локальная модель, если установлен extra `embeddings`; без неё None — только rapidfuzz."""
    if os.environ.get("KTRU_NO_EMBEDDINGS"):
        return None
    from dotenv import load_dotenv

    from .jev_client import ROOT

    load_dotenv(ROOT / ".env")
    if url := os.environ.get("KTRU_EMBED_URL"):
        return RemoteEmbedder(url, model_id=os.environ.get("KTRU_EMBED_REMOTE_MODEL", RemoteEmbedder.MODEL))
    try:
        return SentenceTransformerEmbedder()
    except ImportError:
        return None
