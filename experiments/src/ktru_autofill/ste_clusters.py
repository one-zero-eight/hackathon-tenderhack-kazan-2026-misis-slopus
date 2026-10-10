"""Кластеризация СТЕ внутри категории КПГЗ: группы взаимозаменяемых карточек.

По мотивам one-zero-eight/hackathon-tenderhack (backend/src/tasks/upload/cluster_stage.py):
  1. название без цифр и пунктуации, в леммах (`normalize_name`);
  2. эмбеддинг названия (EmbeddingGemma, промпт кластеризации), при желании усечённый (Matryoshka);
  3. внутри каждой категории — агломеративная кластеризация: по умолчанию средняя связь по косинусу
     (`average_labels`, порог — косинусное расстояние), как в референсе — Ward (`ward_labels`);
  4. название кластера — слова, общие для всех названий (`cluster_name`).
Сравнение алгоритмов — experiments/cluster_benchmark.py: средняя связь по косинусу на 768 координатах
с порогом 0,05 даёт B-cubed F1 0,77 против 0,68 у Ward 256 / 0,8 из референса.
ITML по ручным связям «вместе / порознь» из референса не переносится: таких связей у нас нет.
Карточки разных категорий в один кластер не попадают; категория из одной карточки — кластер из одной.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import re
from typing import Iterable, Sequence

import numpy as np

from .text import lemmatize

DIGITS = re.compile(r"\d+")
PUNCT = re.compile(r"[^\w\s]|_")
EDGE = ".,;:!?\"'()[]{}<>«»"


def normalize_name(name: str) -> str:
    """Название для кластеризации: без цифр и пунктуации, леммы без стоп-слов."""
    return lemmatize(PUNCT.sub(" ", DIGITS.sub(" ", name or "")))


def truncate(vectors: np.ndarray, dim: int | None) -> np.ndarray:
    """Первые dim координат (Matryoshka) и повторная L2-нормировка."""
    if dim:
        vectors = vectors[:, :dim]
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if np.any(norms == 0):
        raise ValueError("Нулевой вектор после усечения")
    return vectors / norms


def ward_labels(vectors: np.ndarray, threshold: float) -> np.ndarray:
    """Метки Ward с порогом расстояния; одна карточка — метка 0."""
    if len(vectors) < 2:
        return np.zeros(len(vectors), dtype=int)
    from sklearn.cluster import AgglomerativeClustering

    return AgglomerativeClustering(n_clusters=None, distance_threshold=threshold, metric="euclidean",
                                   linkage="ward").fit_predict(vectors)


def average_labels(vectors: np.ndarray, threshold: float) -> np.ndarray:
    """Средняя связь по косинусу: кластеры сливаются, пока среднее расстояние 1 − cos < threshold."""
    if len(vectors) < 2:
        return np.zeros(len(vectors), dtype=int)
    from sklearn.cluster import AgglomerativeClustering

    return AgglomerativeClustering(n_clusters=None, distance_threshold=threshold, metric="cosine",
                                   linkage="average").fit_predict(vectors)


LINKAGES = {"average": average_labels, "ward": ward_labels}


def cluster_name(names: Sequence[str]) -> str:
    """Слова, которые есть в каждом названии (без цифр и короче трёх букв), в порядке первого названия.
    Если общих нет — слова из большинства названий; если и их нет — первое название."""
    docs = [[t.strip(EDGE) for t in n.split()] for n in names if n]
    if not docs:
        return ""
    keep = lambda t: len(t) > 2 and not any(ch.isdigit() for ch in t)  # «на», «и», «мл» — не название
    freq = Counter(t.lower() for d in docs for t in {x for x in d if keep(x)})
    for need in (len(docs), len(docs) / 2):
        seen, words = set(), []
        for t in docs[0]:
            if keep(t) and freq[t.lower()] >= need and t.lower() not in seen:
                seen.add(t.lower())
                words.append(t)
        if words:
            return " ".join(words)
    return names[0]


@dataclass
class Cluster:
    id: int
    category: str
    name: str
    ste_ids: list[int] = field(default_factory=list)
    medoid: int | None = None  # карточка, ближайшая к центру кластера


def cluster_ste(ids: Sequence[int], categories: Sequence[str], names: Sequence[str], vectors: np.ndarray,
                threshold: float, linkage: str = "average") -> list[Cluster]:
    """Кластеры по категориям; vectors — нормированные эмбеддинги нормализованных названий."""
    labels_of = LINKAGES[linkage]
    if not (len(ids) == len(categories) == len(names) == len(vectors)):
        raise ValueError("ids, categories, names и vectors должны быть одной длины")
    by_category = defaultdict(list)
    for i, c in enumerate(categories):
        by_category[c].append(i)
    clusters = []
    for category in sorted(by_category):
        idx = by_category[category]
        labels = labels_of(vectors[idx], threshold)
        groups = defaultdict(list)
        for i, label in zip(idx, labels):
            groups[label].append(i)
        for members in sorted(groups.values(), key=lambda m: (-len(m), ids[m[0]])):
            center = vectors[members].mean(axis=0)
            medoid = members[int(np.argmax(vectors[members] @ center))]
            clusters.append(Cluster(len(clusters), category, cluster_name([names[i] for i in members]),
                                    [ids[i] for i in members], ids[medoid]))
    return clusters


def pair_metrics(clusters: Iterable[Cluster], groups: dict[int, str]) -> dict[str, float]:
    """Попарные P/R/F1 по эталонным группам (например, equivalentGroupId) на размеченных карточках:
    пара верна, если обе карточки в одной группе; найдена, если в одном кластере."""
    of = {s: c.id for c in clusters for s in c.ste_ids}
    labeled = [s for s in groups if s in of]
    tp = fp = fn = 0
    for a_pos, a in enumerate(labeled):
        for b in labeled[a_pos + 1:]:
            same_group, same_cluster = groups[a] == groups[b], of[a] == of[b]
            tp += same_group and same_cluster
            fp += same_cluster and not same_group
            fn += same_group and not same_cluster
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return {"labeled": len(labeled), "pairs_true": tp + fn, "precision": p, "recall": r,
            "f1": 2 * p * r / (p + r) if p + r else 0.0}
