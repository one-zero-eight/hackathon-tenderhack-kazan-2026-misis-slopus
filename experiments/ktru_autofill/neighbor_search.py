"""Поиск товара по соседям-СТЕ: ближайшие карточки каталога голосуют за узел ОКПД2, название выбирает Jev.

Вариант Б поиска (вариант А — спуск по дереву, `jev_search.py`). Векторы документов каталога СТЕ (EmbeddingGemma-2,
задача `document`) читаются из LMDB-кеша `meow-embed` через `experiments/meow_cache.py` — только чтение, при промахе
ничего не считается. Запрос пользователя кодируется на сервере `meow-embed` (`MEOW_EMBED_URL`, задача `query`).

    запрос → вектор → k ближайших СТЕ → голос exp((сходство − лучшее) / T) за ОКПД2 карточки → узел классификатора
    (сам код или ближайший предок, у которого есть позиции КТРУ) → Jev выбирает название среди позиций узлов

Индекс сохраняется в npz (`NEIGHBOR_INDEX`), чтобы не читать LMDB заново. Если индекса или эмбеддингов нет,
`JevAutofiller` откатывается на спуск по дереву и сообщает об этом в предупреждениях.
"""

from __future__ import annotations

import base64
import importlib.util
import math
import os
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .jev_search import JevSearch, SearchResult, _Path
from .ktru_store import KtruStore
from .numbers import product_text

ROOT = Path(__file__).resolve().parents[2]
MODEL = "google/embeddinggemma-2"
QUERY_PROMPT = "task: search result | query: "
DIMENSIONS = 768
DEFAULT_CACHE = Path("/home/d-bevz/embedding-cache.lmdb")
DEFAULT_INDEX = ROOT / "data" / "cache" / "ste_neighbors_meow.npz"
CATALOGS = ["Инструменты_from_api.parquet", "Мебель_from_api.parquet", "Одежда_from_api.parquet", "ste.parquet"]


class EmbedError(RuntimeError):
    """Кодирование запроса не удалось (нет сети, ответ неверной формы)."""


def load_meow_cache():
    """experiments/meow_cache.py — единственный способ читать LMDB-кеш векторов (ничего не пишет и не считает)."""
    spec = importlib.util.spec_from_file_location("meow_cache", ROOT / "experiments" / "meow_cache.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MeowQueryEmbedder:
    """Вектор запроса с сервера meow-embed: POST {url}/embed, задача `query`, ответ float32 в base64."""

    def __init__(self, url: str | None = None, *, model_id: str = MODEL, prompt: str = QUERY_PROMPT,
                 dimensions: int = DIMENSIONS, timeout: float = 60.0, client: Any = None, cache_size: int = 256):
        self.url = (url or os.environ.get("MEOW_EMBED_URL", "https://api.innohassle.ru/meow-embed")).rstrip("/")
        self.model_id, self.prompt, self.dimensions, self.timeout = model_id, prompt, dimensions, timeout
        self._client, self._cache, self._cache_size = client, OrderedDict(), cache_size

    def __call__(self, text: str) -> np.ndarray:
        if text in self._cache:
            self._cache.move_to_end(text)
            return self._cache[text]
        body = {"dense_model_id": self.model_id, "dense_task": "query", "dense_prompt": self.prompt, "texts": [text]}
        try:
            if self._client is None:
                import httpx

                self._client = httpx.Client(timeout=self.timeout)
            for attempt in range(2):  # один повтор: туннель и сервер изредка не отвечают в срок
                try:
                    response = self._client.post(f"{self.url}/embed", json=body)
                    break
                except Exception:
                    if attempt:
                        raise
            if response.status_code >= 400:
                raise EmbedError(f"meow-embed HTTP {response.status_code}: {response.text[:200]}")
            vector = self.decode(response.json())
        except EmbedError:
            raise
        except Exception as e:  # сеть, JSON, формат
            raise EmbedError(f"meow-embed: {e}") from e
        self._cache[text] = vector
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return vector

    def decode(self, payload: dict[str, Any]) -> np.ndarray:
        dense = payload.get("dense") or {}
        if dense.get("model_id") != self.model_id or dense.get("dtype") != "float32" \
                or dense.get("shape") != [1, self.dimensions]:
            raise EmbedError("meow-embed вернул неверную модель, размерность или формат вектора")
        vector = np.frombuffer(base64.b64decode(dense["data"]), dtype="<f4").astype(np.float32)
        if vector.size != self.dimensions or not np.isfinite(vector).all():
            raise EmbedError("meow-embed вернул неполный или нечисловой вектор")
        norm = np.linalg.norm(vector)
        if norm == 0:
            raise EmbedError("meow-embed вернул нулевой вектор")
        return vector / norm


@dataclass
class NeighborIndex:
    vectors: np.ndarray  # (N, D) float32, L2-нормированные
    okpd: list[str]  # ОКПД2 карточки
    names: list[str]  # название карточки — для показа соседей
    ids: np.ndarray  # id СТЕ
    blocked: np.ndarray | None = None  # маска карточек, исключённых из поиска (замеры без копии матрицы)

    def __len__(self) -> int:
        return len(self.okpd)

    @classmethod
    def build(cls, vectors: np.ndarray, okpd: list[str], names: list[str], ids: Any) -> NeighborIndex:
        vectors = np.asarray(vectors, dtype=np.float32)
        if vectors.ndim != 2 or len(vectors) != len(okpd) or len(okpd) != len(names):
            raise ValueError("векторы, ОКПД2 и названия должны совпадать по числу")
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if len(vectors) and (norms == 0).any():
            raise ValueError("в индексе есть нулевые векторы")
        return cls(vectors / np.where(norms == 0, 1, norms), list(okpd), list(names), np.asarray(ids, dtype=np.int64))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(path, vectors=self.vectors.astype(np.float16), okpd=np.array(self.okpd),
                            names=np.array(self.names), ids=self.ids)

    @classmethod
    def load(cls, path: Path) -> NeighborIndex:
        with np.load(path, allow_pickle=False) as d:
            return cls.build(d["vectors"].astype(np.float32), d["okpd"].tolist(), d["names"].tolist(), d["ids"])

    @classmethod
    def from_meow_cache(cls, cache: Path, data_dir: Path, limit: int = 10 ** 9) -> NeighborIndex:
        """Векторы документов из LMDB-кеша (`meow_cache.read_dense`); карточки без вектора пропускаются."""
        import pyarrow.parquet as pq

        meow = load_meow_cache()
        ids, payload = meow.catalog_request(data_dir, limit=limit)
        indices, vectors, misses = meow.read_dense(cache, payload)
        meta: dict[int, tuple[str, str]] = {}
        for name in CATALOGS:  # порядок производителя кеша: первый id побеждает
            for batch in pq.ParquetFile(data_dir / name).iter_batches(batch_size=50000, columns=["id", "okpd2_code", "name"]):
                for pid, code, title in zip(*(batch.column(c).to_pylist() for c in ("id", "okpd2_code", "name"))):
                    meta.setdefault(int(pid or 0), (code or "", title or ""))
        keep = [(i, ids[i]) for i in indices.tolist() if meta.get(ids[i], ("", ""))[0]]
        position = {int(i): row for row, i in enumerate(indices.tolist())}
        rows = [position[i] for i, _ in keep]
        return cls.build(vectors[rows] if rows else np.empty((0, DIMENSIONS), dtype=np.float32),
                         [meta[pid][0] for _, pid in keep], [meta[pid][1] for _, pid in keep], [pid for _, pid in keep])

    @classmethod
    def open(cls, index: Path | None = None, cache: Path | None = None, data_dir: Path | None = None) -> NeighborIndex | None:
        """Индекс из npz; нет npz — из LMDB-кеша с сохранением npz; нет и кеша — None."""
        index = index or Path(os.environ.get("NEIGHBOR_INDEX", DEFAULT_INDEX))
        cache = cache or Path(os.environ.get("MEOW_CACHE", DEFAULT_CACHE))
        data_dir = data_dir or Path(os.environ.get("KTRU_DATA", ROOT / "zakupki-reverse" / "data"))
        if index.exists():
            return cls.load(index)
        if not cache.exists():
            return None
        built = cls.from_meow_cache(cache, data_dir)
        if len(built):
            built.save(index)
        return built

    def nearest(self, vector: np.ndarray, k: int) -> list[tuple[int, float]]:
        scores = self.vectors @ vector
        if self.blocked is not None:
            scores = np.where(self.blocked, -np.inf, scores)
        k = min(k, len(scores))
        top = np.argpartition(-scores, k - 1)[:k]
        return [(int(i), float(scores[i])) for i in sorted(top, key=lambda i: -scores[i]) if np.isfinite(scores[i])]


class NeighborSearch:
    """Тот же интерфейс `find(text, emit) -> SearchResult`, что у `JevSearch`."""

    def __init__(self, store: KtruStore, index: NeighborIndex, embed: Callable[[str], np.ndarray], picker: JevSearch,
                 k: int = 25, nodes: int = 3, temperature: float = 0.05):
        self.store, self.index, self.embed, self.picker = store, index, embed, picker
        self.k, self.nodes, self.temperature = k, nodes, temperature

    def node_for(self, okpd: str) -> str | None:
        """Узел классификатора для ОКПД2 карточки: сам код или ближайший предок, у которого есть позиции КТРУ."""
        tree, code = self.store.tree, okpd
        while code:
            node = tree.nodes.get(code)
            if node is not None and node.total:
                return code
            code = code.rsplit(".", 1)[0] if "." in code else ""
        return None

    def vote(self, vector: np.ndarray) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
        """Узлы по убыванию веса (доли голосов) и ближайшие карточки (название, сходство)."""
        near = self.index.nearest(vector, self.k)
        best = near[0][1]
        weights: dict[str, float] = defaultdict(float)
        for i, score in near:
            node = self.node_for(self.index.okpd[i])
            if node:
                weights[node] += math.exp((score - best) / self.temperature)
        total = sum(weights.values()) or 1.0
        ranked = sorted(((n, w / total) for n, w in weights.items()), key=lambda x: -x[1])
        return ranked, [(self.index.names[i], round(s, 3)) for i, s in near[:5]]

    def find(self, text: str, emit: Callable[[str, dict], None] | None = None) -> SearchResult:
        if not len(self.index):
            return SearchResult([], [], [], ["индекс соседей-СТЕ пуст"])
        ranked, shown = self.vote(self.embed(text))
        if emit:
            emit("поиск", {"соседи": [{"название": n, "сходство": s} for n, s in shown],
                           "узлы": [{"узел": self.store.tree.nodes[n].title, "доля": round(w, 3)} for n, w in ranked[:self.nodes]]})
        if not ranked:
            return SearchResult([], [], [], ["ближайшие карточки не относятся ни к одному узлу КТРУ"])
        leaves = [_Path(n, math.log(w), 1, done=True) for n, w in ranked[:self.nodes]]
        rows, families, best_node = self.picker.pick_names(text, leaves, emit)
        if not rows:
            return SearchResult([], [], [], ["Не удалось определить название позиции КТРУ"])
        tree = self.store.tree
        path = [n for n in tree.path(best_node or ranked[0][0]) if not tree.is_section(n.code)]
        voted = sum(tree.nodes[n].total for n, _ in ranked[:self.nodes])
        steps = [(f"соседи СТЕ: {', '.join(n for n, _ in shown[:3])}", voted)]
        steps += [(f"ОКПД2 {n.title}", n.total) for n in path]
        steps.append((f"товар: {', '.join(n for n, _ in families)}", len(rows)))
        return SearchResult(rows, families, steps, [], [n.code for n in path])


DEFAULT_NAME_INDEX = ROOT / "data" / "cache" / "ktru_names_meow.npz"


class MeowDocumentEmbedder:
    """Векторы документов (названия позиций КТРУ) с сервера meow-embed тем же способом, что в LMDB-кеше СТЕ."""

    def __init__(self, url: str | None = None, *, model_id: str = MODEL, dimensions: int = DIMENSIONS,
                 timeout: float = 300.0, batch: int = 64, client: Any = None):
        self.query = MeowQueryEmbedder(url, model_id=model_id, dimensions=dimensions, timeout=timeout, client=client)
        self.batch = batch

    def __call__(self, texts: list[str]) -> np.ndarray:
        q, out = self.query, []
        for i in range(0, len(texts), self.batch):
            part = texts[i:i + self.batch]
            body = {"dense_model_id": q.model_id, "dense_task": "document", "dense_prompt": "", "texts": part}
            try:
                if q._client is None:
                    import httpx

                    q._client = httpx.Client(timeout=q.timeout)
                response = q._client.post(f"{q.url}/embed", json=body)
                if response.status_code >= 400:
                    raise EmbedError(f"meow-embed HTTP {response.status_code}: {response.text[:200]}")
                dense = response.json().get("dense") or {}
                if dense.get("model_id") != q.model_id or dense.get("shape") != [len(part), q.dimensions]:
                    raise EmbedError("meow-embed вернул неверную модель или форму")
                out.append(np.frombuffer(base64.b64decode(dense["data"]), dtype="<f4").reshape(len(part), -1))
            except EmbedError:
                raise
            except Exception as e:
                raise EmbedError(f"meow-embed: {e}") from e
        return np.concatenate(out) if out else np.empty((0, q.dimensions), dtype=np.float32)


@dataclass
class KtruNameIndex:
    """Векторы названий позиций КТРУ: поиск напрямую по справочнику КТРУ, без карточек СТЕ."""
    vectors: np.ndarray  # (N, D) float32, L2-нормированные
    names: list[str]  # уникальные названия позиций

    def __len__(self) -> int:
        return len(self.names)

    @classmethod
    def build(cls, names: list[str], embed: Callable[[list[str]], np.ndarray]) -> KtruNameIndex:
        unique = list(dict.fromkeys(names))
        vectors = np.asarray(embed(unique), dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if len(vectors) != len(unique) or (norms == 0).any():
            raise ValueError("векторы названий неполные или нулевые")
        return cls(vectors / norms, unique)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(path, vectors=self.vectors.astype(np.float16), names=np.array(self.names))

    @classmethod
    def load(cls, path: Path) -> KtruNameIndex:
        with np.load(path, allow_pickle=False) as d:
            v = d["vectors"].astype(np.float32)
            return cls(v / np.linalg.norm(v, axis=1, keepdims=True), d["names"].tolist())

    def nearest(self, vector: np.ndarray, k: int) -> list[tuple[str, float]]:
        scores = self.vectors @ vector
        k = min(k, len(scores))
        top = np.argpartition(-scores, k - 1)[:k]
        return [(self.names[i], float(scores[i])) for i in sorted(top, key=lambda i: -scores[i])]


class HybridSearch:
    """Поиск по КТРУ: голоса за узлы ОКПД2 от соседей-СТЕ и от ближайших названий позиций КТРУ (gemma2), при
    неуверенном голосе — плюс beam search по дереву (Choice Jev). Затем Jev выбирает название позиции.

    mode: "ktru" — только названия КТРУ; "neighbors" — только СТЕ; "hybrid" — оба голоса, дерево при неуверенности.
    Вес узла = a·голос(СТЕ) + (1−a)·голос(КТРУ); если лучший вес < `tau`, к узлам добавляются листья дерева
    с весом их вероятности спуска (смешиваются с долей `tree_share`)."""

    def __init__(self, store: KtruStore, names: KtruNameIndex | None, neighbors: NeighborSearch | None, embed: Callable[[str], np.ndarray],
                 picker: JevSearch, mode: str = "hybrid", a: float = 0.5, k_names: int = 25, tau: float = 0.6,
                 tree_share: float = 0.5, nodes: int = 3, temperature: float = 0.05):
        self.store, self.names, self.neighbors, self.embed, self.picker = store, names, neighbors, embed, picker
        self.mode, self.a, self.k_names, self.tau, self.tree_share = mode, a, k_names, tau, tree_share
        self.nodes, self.temperature = nodes, temperature
        self._rows_by_name: dict[str, list[int]] | None = None

    def rows_for(self, name: str) -> list[int]:
        if self._rows_by_name is None:
            self._rows_by_name = {}
            for r, n in enumerate(self.store.names):
                self._rows_by_name.setdefault(n, []).append(r)
        return self._rows_by_name.get(name, [])

    def name_vote(self, vector: np.ndarray) -> tuple[dict[str, float], list[tuple[str, float]]]:
        """Доли голосов узлов от ближайших названий позиций КТРУ."""
        near = self.names.nearest(vector, self.k_names)
        best, weights = near[0][1], defaultdict(float)
        for name, score in near:
            rows = self.rows_for(name)
            for r in rows:  # голос названия делится между его позициями
                weights[self.store.okpd[r]] += math.exp((score - best) / self.temperature) / len(rows)
        total = sum(weights.values()) or 1.0
        return {n: w / total for n, w in weights.items()}, [(n, round(s, 3)) for n, s in near[:5]]

    def combine(self, vector: np.ndarray) -> tuple[dict[str, float], list[tuple[str, float]]]:
        ste_w: dict[str, float] = {}
        shown: list[tuple[str, float]] = []
        if self.mode != "ktru" and self.neighbors is not None:
            ranked, shown = self.neighbors.vote(vector)
            ste_w = dict(ranked)
        name_w: dict[str, float] = {}
        if self.mode != "neighbors" and self.names is not None:
            raw, name_shown = self.name_vote(vector)
            shown = shown or name_shown
            for code, w in raw.items():  # узел ОКПД2 позиции → ближайший узел с позициями
                node = (self.neighbors.node_for(code) if self.neighbors else code) or code
                name_w[node] = name_w.get(node, 0.0) + w
        if ste_w and name_w:
            keys = ste_w.keys() | name_w.keys()
            return {n: self.a * ste_w.get(n, 0.0) + (1 - self.a) * name_w.get(n, 0.0) for n in keys}, shown
        return ste_w or name_w, shown

    def find(self, text: str, emit: Callable[[str, dict], None] | None = None) -> SearchResult:
        vector = self.embed(text)
        weights, shown = self.combine(vector)
        if not weights:
            return SearchResult([], [], [], ["ближайшие названия не относятся ни к одному узлу КТРУ"])
        ranked = sorted(weights.items(), key=lambda x: -x[1])
        used_tree = False
        if self.mode == "hybrid" and ranked[0][1] < self.tau:  # голос неуверен — спрашиваем дерево
            used_tree = True
            for p in self.picker.descend(product_text(text), emit):
                share = math.exp(p.score) * self.tree_share
                weights = {n: w * (1 - self.tree_share) for n, w in weights.items()}
                weights[p.node] = weights.get(p.node, 0.0) + share
            ranked = sorted(weights.items(), key=lambda x: -x[1])
        if emit:
            emit("поиск", {"соседи": [{"название": n, "сходство": s} for n, s in shown],
                           "узлы": [{"узел": self.store.tree.nodes[n].title, "доля": round(w, 3)} for n, w in ranked[:self.nodes]
                                    if n in self.store.tree.nodes], "дерево": used_tree})
        leaves = [_Path(n, math.log(max(w, 1e-9)), 1, done=True) for n, w in ranked[:self.nodes] if n in self.store.tree.nodes]
        prior = dict(self.names.nearest(vector, 400)) if self.names is not None else None  # порядок пула названий
        rows, families, best_node = self.picker.pick_names(text, leaves, emit, prior)
        if not rows:
            return SearchResult([], [], [], ["Не удалось определить название позиции КТРУ"])
        tree = self.store.tree
        path = [n for n in tree.path(best_node or ranked[0][0]) if not tree.is_section(n.code)]
        steps = [(f"КТРУ ({self.mode}): {', '.join(n for n, _ in shown[:3])}" + (" + дерево" if used_tree else ""),
                  sum(tree.nodes[n].total for n, _ in ranked[:self.nodes] if n in tree.nodes))]
        steps += [(f"ОКПД2 {n.title}", n.total) for n in path]
        steps.append((f"товар: {', '.join(n for n, _ in families)}", len(rows)))
        return SearchResult(rows, families, steps, [], [n.code for n in path])
