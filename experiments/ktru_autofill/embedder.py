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


def load_default() -> Embedder | None:
    """Модель, если установлен extra `embeddings`; иначе None — работаем только на rapidfuzz."""
    if os.environ.get("KTRU_NO_EMBEDDINGS"):
        return None
    try:
        return SentenceTransformerEmbedder()
    except ImportError:
        return None
