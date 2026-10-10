"""Векторный поиск схем КПГЗ по тексту товара через EmbeddingGemma."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Iterable

import numpy as np

MODEL = "google/embeddinggemma-2"
CACHE = Path(__file__).resolve().parents[2] / "data" / "cache" / "kpgz_embeddinggemma.npz"
log = logging.getLogger(__name__)


class KpgzSearch:
    """Нормированные векторы схем; точный cosine top-k без категории из СТЕ."""

    def __init__(self, nodes: Iterable[dict[str, Any]], *, model_name: str = MODEL,
                 device: str = "cpu", cache: Path = CACHE, batch_size: int = 32,
                 max_length: int = 512):
        if batch_size <= 0 or max_length <= 0:
            raise ValueError("batch_size и max_length должны быть положительными")
        all_nodes = {n["code"]: n for n in nodes}
        self.nodes = sorted((n for n in all_nodes.values() if n.get("kpgzCharacteristics")),
                            key=lambda n: n["code"])
        if not self.nodes:
            raise ValueError("В каталоге нет КПГЗ со схемами характеристик")
        self.documents = []
        for node in self.nodes:
            parts = node["code"].split(".")
            ancestors = [all_nodes[code]["name"] for i in range(1, len(parts))
                         if (code := ".".join(parts[:i])) in all_nodes]
            characteristics = ", ".join(ch["characteristicName"] for ch in node["kpgzCharacteristics"])
            self.documents.append(f"title: {node['name']} | text: {' / '.join(ancestors + [node['name']])}. "
                                  f"Характеристики: {characteristics}")
        self.model_name, self.device = model_name, device
        self.batch_size, self.max_length = batch_size, max_length
        self._model = None
        fingerprint = hashlib.sha256(json.dumps(
            {"model": model_name, "max_length": max_length, "documents": self.documents,
             "codes": [n["code"] for n in self.nodes]}, ensure_ascii=False).encode()).hexdigest()
        self.vectors = None
        if cache.exists():
            with np.load(cache, allow_pickle=False) as stored:
                if stored["fingerprint"].item() == fingerprint:
                    self.vectors = stored["vectors"]
                    self._validate(self.vectors, len(self.nodes))
                    log.info("Индекс КПГЗ из %s: %d схем", cache, len(self.nodes))
        if self.vectors is None:
            log.info("Индексирование %d схем КПГЗ: %s на %s", len(self.nodes), model_name, device)
            self.vectors = self._encode(self.documents, prompt="", progress=True)
            cache.parent.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(cache, fingerprint=fingerprint, vectors=self.vectors)

    def _load_model(self):
        import torch
        from sentence_transformers import SentenceTransformer

        # V100 не поддерживает native bf16; fp16 для EmbeddingGemma даёт NaN.
        dtype = torch.bfloat16 if self.device.startswith("cuda") and torch.cuda.is_bf16_supported(including_emulation=False) else torch.float32
        model = SentenceTransformer(self.model_name, device=self.device,
                                    model_kwargs={"dtype": dtype},
                                    config_kwargs={"vision_config": None, "audio_config": None})
        model.to(dtype=dtype)
        log.info("EmbeddingGemma загружена: %s, %s", self.device, next(model.parameters()).dtype)
        model.max_seq_length = self.max_length
        return model

    @staticmethod
    def _validate(vectors: np.ndarray, count: int):
        if vectors.ndim != 2 or len(vectors) != count or not np.isfinite(vectors).all():
            raise ValueError("Некорректные эмбеддинги КПГЗ")
        if not np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-3):
            raise ValueError("Эмбеддинги должны быть L2-нормированы")

    def _encode(self, texts: list[str], *, prompt: str, progress: bool = False) -> np.ndarray:
        if self._model is None:
            self._model = self._load_model()
        vectors = np.asarray(self._model.encode(texts, prompt=prompt, batch_size=self.batch_size,
                                               normalize_embeddings=True, show_progress_bar=progress),
                             dtype=np.float32)
        if vectors.ndim != 2 or len(vectors) != len(texts) or not np.isfinite(vectors).all():
            raise ValueError("Некорректные эмбеддинги КПГЗ")
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if np.any(norms == 0):
            raise ValueError("Нулевые эмбеддинги КПГЗ")
        # Нормализация в float32: encode может округлить норму в bf16 перед переводом в numpy.
        vectors /= norms
        self._validate(vectors, len(texts))
        return vectors

    def search_many(self, texts: list[str], top_k: int = 5) -> list[list[dict[str, Any]]]:
        if top_k <= 0:
            raise ValueError("top_k должен быть положительным")
        if any(not text.strip() for text in texts):
            raise ValueError("Для поиска категории нужен непустой текст")
        if not texts:
            return []
        queries = self._encode(texts, prompt="task: search result | query: ")
        scores = queries @ self.vectors.T
        return [[{"code": self.nodes[i]["code"], "name": self.nodes[i]["name"], "score": float(row[i])}
                 for i in np.argsort(-row, kind="stable")[:top_k]] for row in scores]

    def search(self, text: str, top_k: int = 5) -> list[dict[str, Any]]:
        return self.search_many([text], top_k)[0]

    def node(self, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        code = candidates[0]["code"]
        node = next(n for n in self.nodes if n["code"] == code)
        return node | {"_retrieval": {"method": "cosine", "model": self.model_name, "top_k": candidates}}


def add_search_arguments(parser):
    parser.add_argument("--search-category", action="store_true", help="найти КПГЗ по тексту через EmbeddingGemma")
    parser.add_argument("--embedding-model", default=MODEL)
    parser.add_argument("--embedding-device", default="cpu", help="cpu или cuda")
    parser.add_argument("--embedding-index", type=Path, default=CACHE)
    parser.add_argument("--embedding-batch-size", type=int, default=32)
    parser.add_argument("--category-top-k", type=int, default=5, help="число кандидатов в логе; для схемы выбирается top-1")


def from_args(nodes, args) -> KpgzSearch:
    if args.category_top_k <= 0:
        raise ValueError("category-top-k должен быть положительным")
    return KpgzSearch(nodes, model_name=args.embedding_model, device=args.embedding_device,
                      cache=args.embedding_index, batch_size=args.embedding_batch_size)
