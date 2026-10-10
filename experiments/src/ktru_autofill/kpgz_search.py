"""Поиск категории КПГЗ по тексту товара через EmbeddingGemma: схемы КПГЗ или соседи-СТЕ (+ Jev)."""

from __future__ import annotations

from collections import Counter, defaultdict
import gzip
import hashlib
import heapq
import html
import json
import logging
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Iterator

import numpy as np

MODEL = "google/embeddinggemma-2"
CACHE = Path(__file__).resolve().parents[2] / "data" / "cache" / "kpgz_embeddinggemma.npz"
NEIGHBORS_CACHE = CACHE.parent / "ste_neighbors_embeddinggemma.npz"
QUERY_PROMPT = "task: search result | query: "
JEV_MODEL = "jev-latest"
log = logging.getLogger(__name__)


class KpgzSearch:
    """Нормированные векторы схем; точный cosine top-k без категории из СТЕ.

    С `neighbors` категория — голосование ближайших карточек СТЕ, а не ближайшая схема."""

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
        self.by_code = {n["code"]: n for n in self.nodes}
        self.documents, self.paths = [], {}
        for node in self.nodes:
            parts = node["code"].split(".")
            ancestors = [all_nodes[code]["name"] for i in range(1, len(parts))
                         if (code := ".".join(parts[:i])) in all_nodes]
            self.paths[node["code"]] = " / ".join(ancestors + [node["name"]])
            characteristics = ", ".join(ch["characteristicName"] for ch in node["kpgzCharacteristics"])
            self.documents.append(f"title: {node['name']} | text: {' / '.join(ancestors + [node['name']])}. "
                                  f"Характеристики: {characteristics}")
        self.model_name, self.device = model_name, device
        self.batch_size, self.max_length = batch_size, max_length
        self._model = None
        self.neighbors: SteNeighbors | None = None
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

    def cached_encode(self, texts: list[str], cache: Path, *, query: bool = False,
                      prompt: str | None = None) -> np.ndarray:
        """Эмбеддинги с кешем по отпечатку модели, длины и текстов; prompt — свой префикс задачи
        (например, кластеризации) вместо поискового."""
        key = {"model": self.model_name, "length": self.max_length, "texts": texts, "query": query}
        if prompt is not None:
            key["prompt"] = prompt
        fingerprint = hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()
        if cache.exists():
            with np.load(cache, allow_pickle=False) as data:
                if data["fingerprint"].item() == fingerprint:
                    vectors = data["vectors"]
                    self._validate(vectors, len(texts))
                    return vectors
        if prompt is None:
            prompt = QUERY_PROMPT if query else ""
        vectors = self._encode(texts, prompt=prompt, progress=True)
        cache.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(cache, fingerprint=fingerprint, vectors=vectors)
        return vectors

    def encode_queries(self, texts: list[str]) -> np.ndarray:
        if any(not text.strip() for text in texts):
            raise ValueError("Для поиска категории нужен непустой текст")
        return self._encode(texts, prompt=QUERY_PROMPT)

    def search_many(self, texts: list[str], top_k: int = 5) -> list[list[dict[str, Any]]]:
        if top_k <= 0:
            raise ValueError("top_k должен быть положительным")
        if not texts:
            return []
        queries = self.encode_queries(texts)
        if self.neighbors is not None:
            return self.neighbors.rank(queries, top_k)
        scores = queries @ self.vectors.T
        return [[{"code": self.nodes[i]["code"], "name": self.nodes[i]["name"], "score": float(row[i])}
                 for i in np.argsort(-row, kind="stable")[:top_k]] for row in scores]

    def search(self, text: str, top_k: int = 5) -> list[dict[str, Any]]:
        return self.search_many([text], top_k)[0]

    def node(self, candidates: list[dict[str, Any]], method: str | None = None) -> dict[str, Any]:
        code = candidates[0]["code"]
        if method is None:
            method = "ste_neighbors" if self.neighbors is not None else "cosine"
        retrieval = {"method": method, "model": self.model_name, "top_k": candidates}
        if self.neighbors is not None:
            retrieval["neighbors"] = self.neighbors.config
        return self.by_code[code] | {"_retrieval": retrieval}


def norm(s: Any) -> str:
    return re.sub(r"\W+", " ", str(s).lower().replace("ё", "е")).strip()


def clean(text: str | None) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def card_text(card: dict[str, Any]) -> str:
    """Текст карточки для поиска: название и очищенное описание."""
    return f"{card.get('name') or card.get('modelName') or ''}\n{clean(card.get('description'))}".strip()


def signature(text: str) -> str:
    return hashlib.sha256(norm(text).encode()).hexdigest()


def family(name: str) -> str:
    """Товарная семья: название без цифр и латиницы (кодов моделей). «» — если ничего не осталось."""
    return " ".join(re.sub(r"[0-9a-z_]+", " ", norm(name)).split())


def iter_cards(path: Path) -> Iterator[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            yield json.loads(line)


def collect_corpus(cards: Iterable[dict[str, Any]], valid_codes: set[str], exclude: Iterable[dict[str, Any]] = (),
                   per_category: int = 20, limit: int = 20000,
                   families: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """До per_category карточек на КПГЗ, общий лимит limit; отбор по хешу ID, не по запросу.

    exclude — запросы {id, name, text}: их ID, тексты и названия в корпус не попадают;
    families=True — ещё и карточки той же товарной семьи (`family`), строгий holdout для замера."""
    exclude = list(exclude)
    excluded_ids = {q["id"] for q in exclude}
    excluded_texts = {signature(q["text"]) for q in exclude}
    excluded_names = {norm(q["name"]) for q in exclude if q.get("name")}
    excluded_families = {f for q in exclude if families and (f := family(q.get("name") or ""))}
    heaps, stats, counts = defaultdict(list), Counter(), Counter()
    for c in cards:
        stats["scanned"] += 1
        code = (c.get("production") or {}).get("code")
        if c.get("_fetchError") or code not in valid_codes:
            continue
        name = c.get("name") or c.get("modelName") or ""
        text = card_text(c)
        if not name or not text:
            continue
        if c["id"] in excluded_ids:
            stats["excluded_test_id"] += 1
            continue
        if signature(text) in excluded_texts or norm(name) in excluded_names:
            stats["excluded_test_duplicate"] += 1
            continue
        if excluded_families and family(name) in excluded_families:
            stats["excluded_test_family"] += 1
            continue
        counts[code] += 1
        rank = int(hashlib.sha256(str(c["id"]).encode()).hexdigest(), 16)
        item = (-rank, c["id"], {"id": c["id"], "code": code, "name": name, "text": text})
        heap = heaps[code]
        if len(heap) < per_category:
            heapq.heappush(heap, item)
        elif item[:2] > heap[0][:2]:
            heapq.heapreplace(heap, item)
    groups = [sorted(heaps[code], reverse=True) for code in sorted(heaps)]
    pool = [group[i][2] for i in range(per_category) for group in groups if i < len(group)][:limit]
    # Дедупликация не даёт одинаковым карточкам создавать много голосов.
    unique, conflicts = {}, set()
    for c in pool:
        key = signature(c["text"])
        if key in unique and unique[key]["code"] != c["code"]:
            conflicts.add(key)
        else:
            unique.setdefault(key, c)
    corpus = [c for key, c in unique.items() if key not in conflicts]
    stats.update({"eligible": sum(counts.values()), "eligible_categories": len(counts),
                  "corpus": len(corpus), "corpus_categories": len({c["code"] for c in corpus}),
                  "conflicting_texts_removed": len(conflicts), "duplicates_removed": len(pool) - len(unique)})
    return corpus, dict(stats)


def neighbor_ranking(scores: np.ndarray, corpus: list[dict[str, Any]], k: int, vote: bool = True,
                     temperature: float = .05) -> tuple[list[str], list[dict[str, Any]]]:
    """Категории k ближайших карточек: по весу exp((s - s_max) / T) или по порядку первой встречи."""
    ids = np.argsort(-scores, kind="stable")[:k]
    groups = defaultdict(list)
    for i in ids:
        groups[corpus[i]["code"]].append(float(scores[i]))
    if vote:
        # Фиксированное взвешенное голосование, не вероятность правильности.
        top = float(scores[ids[0]])
        ranked = sorted(groups, key=lambda c: (-sum(np.exp((s - top) / temperature) for s in groups[c]),
                                               -max(groups[c]), c))
    else:
        ranked = list(groups)
    neighbors = [corpus[i] | {"score": float(scores[i])} for i in ids]
    return ranked, neighbors


class SteNeighbors:
    """Индекс карточек СТЕ: категория запроса — взвешенный голос k ближайших карточек."""

    def __init__(self, search: KpgzSearch, corpus: list[dict[str, Any]], *, cache: Path = NEIGHBORS_CACHE,
                 k: int = 5, temperature: float = .05):
        if not corpus:
            raise ValueError("Корпус соседей пуст")
        if k <= 0 or temperature <= 0:
            raise ValueError("k и temperature должны быть положительными")
        unknown = {c["code"] for c in corpus} - set(search.by_code)
        if unknown:
            raise ValueError(f"В корпусе соседей КПГЗ без схемы: {sorted(unknown)[:5]}")
        self.search, self.corpus, self.k, self.temperature = search, corpus, k, temperature
        self.vectors = search.cached_encode([f"title: {c['name']} | text: {c['text']}" for c in corpus], cache)
        self.config = {"k": k, "temperature": temperature, "corpus": len(corpus),
                       "corpus_categories": len({c["code"] for c in corpus})}

    def rank(self, query_vectors: np.ndarray, top_k: int = 5) -> list[list[dict[str, Any]]]:
        out = []
        for row in query_vectors @ self.vectors.T:
            ranked, neighbors = neighbor_ranking(row, self.corpus, self.k, temperature=self.temperature)
            top = neighbors[0]["score"]
            weights = {c: sum(np.exp((n["score"] - top) / self.temperature) for n in neighbors if n["code"] == c)
                       for c in ranked}
            total = sum(weights.values())
            out.append([{"code": c, "name": self.search.by_code[c]["name"], "score": float(weights[c] / total),
                         "neighbors": [{"id": n["id"], "name": n["name"], "score": n["score"]}
                                       for n in neighbors if n["code"] == c]}
                        for c in ranked[:top_k]])
        return out


def category_question(search: KpgzSearch, candidates: list[dict[str, Any]],
                      examples: int = 3) -> tuple[dict[str, Any], dict[str, str]]:
    """Choice Jev среди категорий соседей-СТЕ; варианты — путь КПГЗ и названия похожих карточек.

    Возвращает вопрос и соответствие варианта коду КПГЗ."""
    if not candidates:
        raise ValueError("Нет категорий-кандидатов")
    names = Counter(c["name"] for c in candidates)
    criteria, codes = {}, {}
    for c in candidates:
        label = c["name"] if names[c["name"]] == 1 else f"{c['name']} ({c['code']})"
        criteria[label] = {"раздел КПГЗ": search.paths[c["code"]],
                           "похожие карточки этой категории": [n["name"] for n in c.get("neighbors", [])[:examples]]}
        codes[label] = c["code"]
    question = {
        "type": "choice",
        "instructions": {
            "question": "К какой категории КПГЗ относится `товар`, судя по `товар` и `описание`?",
            "правило": [
                "Решай по назначению и предмету закупки, а не по общим словам, цифрам и маркировке.",
                "Похожие карточки найдены по сходству текста: они показывают, что лежит в категории, "
                "но совпадение может быть случайным.",
                "Отличай товар от работы или услуги: изготовление, монтаж, обустройство, ремонт — работы; "
                "готовое изделие — товар.",
                "Запчасть или комплектующее относи к категории оборудования, для которого оно предназначено, "
                "если такая категория есть среди вариантов.",
            ]},
        "criteria": criteria,
    }
    return question, codes


def jev_category(search: KpgzSearch, state: dict[str, Any], candidates: list[dict[str, Any]],
                 call: Callable[[dict[str, Any]], dict[str, Any]] | None,
                 cached: dict[str, Any] | None = None, model: str = JEV_MODEL) -> dict[str, Any]:
    """Выбор Jev среди категорий соседей. call=None — только запрос (dry run).

    cached — прежний лог {request, response}: при том же запросе API не вызывается.
    Порядок кандидатов: по вероятности Jev, при равенстве — по голосу соседей."""
    question, codes = category_question(search, candidates)
    request = {"model": model, "state": _state(state), "questions": {"category": question}}
    if cached and cached.get("request") == request and cached.get("response"):
        response = cached["response"]
    elif call is None:
        return {"request": request, "response": None, "top_k": candidates}
    else:
        response = call(request)
    answer = response["answers"]["category"]
    probs = {codes[label]: p for label, p in answer["probabilities"].items()}
    order = {c["code"]: i for i, c in enumerate(candidates)}
    ranked = sorted(candidates, key=lambda c: (-probs.get(c["code"], 0.), order[c["code"]]))
    return {"request": request, "response": response, "confidence": answer.get("confidence"),
            "top_k": [c | {"jev": probs.get(c["code"], 0.)} for c in ranked]}


RULES = [
    "Решай по назначению и предмету закупки, а не по общим словам, цифрам и маркировке.",
    "Похожие карточки найдены по сходству текста: они показывают, что лежит в категории, "
    "но совпадение может быть случайным.",
    "Отличай товар от работы или услуги: изготовление, монтаж, обустройство, ремонт — работы; "
    "готовое изделие — товар.",
]


def category_noul(search: KpgzSearch, candidate: dict[str, Any], examples: int = 3) -> dict[str, Any]:
    """Noul «относится ли товар к этой категории» — независимая оценка одного кандидата."""
    return {
        "type": "noul",
        "instructions": {
            "категория": candidate["name"], "раздел КПГЗ": search.paths[candidate["code"]],
            "похожие карточки этой категории": [n["name"] for n in candidate.get("neighbors", [])[:examples]],
            "question": "Относится ли `товар` к этой категории КПГЗ, судя по `товар` и `описание`?",
            "правило": RULES + ["Запчасть или комплектующее относится к категории оборудования, "
                                "для которого оно предназначено, а не к категории самого оборудования, "
                                "если в названии категории нет запчастей."]},
        "criteria": {"true": "Предмет категории совпадает с назначением товара",
                     "false": "Товар относится к другой категории"},
    }


class _Calls:
    """Запросы одного выбора с кешем по полному телу запроса; call=None — только собрать запросы."""

    def __init__(self, call, cached):
        self.call, self.log = call, []
        self.cached = {json.dumps(c["request"], ensure_ascii=False, sort_keys=True): c["response"]
                       for c in (cached or {}).get("calls", []) if c.get("response")}
        self.pending = False

    def __call__(self, request):
        key = json.dumps(request, ensure_ascii=False, sort_keys=True)
        response = self.cached.get(key)
        if response is None and self.call is not None:
            response = self.call(request)
        self.pending |= response is None
        self.log.append({"request": request, "response": response})
        return response

    def result(self, top_k, confidence):
        usage = {"input_tokens": 0, "output_tokens": 0}
        for c in self.log:
            for name in usage:
                usage[name] += ((c["response"] or {}).get("usage") or {}).get(name, 0)
        return {"calls": self.log, "response": None if self.pending else {"usage": usage},
                "confidence": confidence, "top_k": top_k}


def jev_category_noul(search: KpgzSearch, state: dict[str, Any], candidates: list[dict[str, Any]],
                      call, cached: dict[str, Any] | None = None, model: str = JEV_MODEL) -> dict[str, Any]:
    """По Noul на кандидата в одном запросе; порядок — по P(да), при равенстве — по голосу соседей.
    confidence — P(да) лучшего кандидата."""
    calls = _Calls(call, cached)
    questions = {f"c{i}": category_noul(search, c) for i, c in enumerate(candidates)}
    response = calls({"model": model, "state": _state(state), "questions": questions})
    if response is None:
        return calls.result(candidates, None)
    yes = {c["code"]: response["answers"][f"c{i}"]["noul"] for i, c in enumerate(candidates)}
    ranked = sorted(enumerate(candidates), key=lambda x: (-yes[x[1]["code"]], x[0]))
    return calls.result([c | {"jev": yes[c["code"]]} for _, c in ranked], max(yes.values()))


def jev_category_knockout(search: KpgzSearch, state: dict[str, Any], candidates: list[dict[str, Any]],
                          call, cached: dict[str, Any] | None = None, model: str = JEV_MODEL,
                          group: int = 5) -> dict[str, Any]:
    """Турнир: Choice в группах по `group` кандидатов (по порядку голоса), затем Choice среди победителей.

    Вероятности разных групп не сравниваются: в финале победители выбираются заново. Порядок —
    победители по финальной вероятности, затем остальные по вероятности в своей группе."""
    calls = _Calls(call, cached)
    groups = [candidates[i:i + group] for i in range(0, len(candidates), group)]
    asked = {f"g{i}": category_question(search, g) for i, g in enumerate(groups) if len(g) > 1}
    group_probs = {}
    if asked:
        response = calls({"model": model, "state": _state(state),
                          "questions": {name: q for name, (q, _) in asked.items()}})
        if response is None:
            return calls.result(candidates, None)
        for name, (_, codes) in asked.items():
            for label, p in response["answers"][name]["probabilities"].items():
                group_probs[codes[label]] = p
    for g in groups:
        if len(g) == 1:
            group_probs[g[0]["code"]] = 1.0
    winners = [max(g, key=lambda c: group_probs[c["code"]]) for g in groups]
    final = {winners[0]["code"]: 1.0}
    if len(winners) > 1:
        question, codes = category_question(search, winners)
        response = calls({"model": model, "state": _state(state), "questions": {"final": question}})
        if response is None:
            return calls.result(candidates, None)
        final = {codes[label]: p for label, p in response["answers"]["final"]["probabilities"].items()}
    order = {c["code"]: i for i, c in enumerate(candidates)}
    win = {c["code"] for c in winners}
    ranked = sorted(candidates, key=lambda c: (c["code"] not in win, -final.get(c["code"], 0.),
                                               -group_probs[c["code"]], order[c["code"]]))
    top_k = [c | {"jev": final.get(c["code"], 0.), "group_jev": group_probs[c["code"]]} for c in ranked]
    return calls.result(top_k, max(final.values()))


def _state(state: dict[str, Any]) -> dict[str, str]:
    return {"товар": state["товар"], "описание": state.get("описание") or "нет"}


def add_search_arguments(parser):
    parser.add_argument("--search-category", action="store_true", help="найти КПГЗ по тексту через EmbeddingGemma")
    parser.add_argument("--embedding-model", default=MODEL)
    parser.add_argument("--embedding-device", default="cpu", help="cpu или cuda")
    parser.add_argument("--embedding-index", type=Path, default=CACHE)
    parser.add_argument("--embedding-batch-size", type=int, default=32)
    parser.add_argument("--category-top-k", type=int, default=5, help="число кандидатов в логе; для схемы выбирается top-1")
    parser.add_argument("--category-neighbors", type=Path, metavar="STE_JSONL_GZ",
                        help="категория голосованием ближайших карточек СТЕ из этого файла, а не ближайшей схемой")
    parser.add_argument("--neighbors-k", type=int, default=5, help="число соседей-СТЕ в голосовании")
    parser.add_argument("--neighbors-per-category", type=int, default=20)
    parser.add_argument("--neighbors-limit", type=int, default=20000)
    parser.add_argument("--neighbors-index", type=Path, default=NEIGHBORS_CACHE)
    parser.add_argument("--category-jev", action="store_true",
                        help="Jev выбирает категорию среди категорий соседей (нужен --category-neighbors)")
    parser.add_argument("--jev-base-url", help="совместимый System One сервер (experiments/jev9b_server.py) вместо TypeSafe")
    parser.add_argument("--jev-model", default=JEV_MODEL, help="имя модели в запросе выбора категории")


def from_args(nodes, args, exclude: Iterable[dict[str, Any]] = ()) -> KpgzSearch:
    """exclude — запросы {id, name, text}, которых не должно быть среди соседей (замер)."""
    if args.category_top_k <= 0:
        raise ValueError("category-top-k должен быть положительным")
    if args.category_jev and not args.category_neighbors:
        raise ValueError("--category-jev работает только с --category-neighbors")
    search = KpgzSearch(nodes, model_name=args.embedding_model, device=args.embedding_device,
                        cache=args.embedding_index, batch_size=args.embedding_batch_size)
    if args.category_neighbors:
        corpus, stats = collect_corpus(iter_cards(args.category_neighbors), set(search.by_code), exclude,
                                       args.neighbors_per_category, args.neighbors_limit)
        log.info("Корпус соседей-СТЕ: %s", stats)
        search.neighbors = SteNeighbors(search, corpus, cache=args.neighbors_index, k=args.neighbors_k)
    return search
