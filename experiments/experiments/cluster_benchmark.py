"""Сравнение алгоритмов кластеризации СТЕ на одних данных.

    .venv/bin/python experiments/cluster_benchmark.py --ste ~/tenderhack/zakupki-reverse/data/ste-details.jsonl.gz

Все методы кластеризуют внутри категории КПГЗ (карточки разных категорий не смешиваются).
Две проверки (плюс базовый уровень — случайные пары из одной категории):
  equivalent — попарные P/R/F1 по `equivalentGroupId` портала (719 карточек, понятие «аналог»);
  consistency — на всём каталоге: доля пар карточек одного кластера без противоречия характеристик
                (обе заполнены, значения не пересекаются) среди сравнимых пар. Считается по
                «проверочным» характеристикам категории — заполненным у ≥ 50% её карточек, кроме трёх
                самых заполненных: те три — «подпись» для методов по характеристикам, чтобы метод
                не проверял сам себя. До 200 случайных пар на кластер (seed 0).
Гранулярность: число кластеров, доля карточек в кластерах от двух.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import logging
from pathlib import Path
import random
import time

import numpy as np
from scipy.sparse.csgraph import connected_components

import ste_to_jev as ste
from ktru_autofill.kpgz_search import CACHE, KpgzSearch, iter_cards, norm
from ktru_autofill.ste_clusters import Cluster, normalize_name, pair_metrics, truncate, ward_labels

log = logging.getLogger(__name__)
PROMPT = "task: clustering | query: "
SIGNATURE = 3        # характеристик в подписи
MIN_FILL = .5        # ключевая характеристика заполнена у ≥ 50% карточек категории
PAIRS_PER_CLUSTER = 200
MAX_DISTINCT_SHARE, MIN_DISTINCT = .2, 5  # проверочная: разных значений ≤ max(5, 20% заполненных)


def values_of(card):
    """Характеристика → множество нормализованных значений."""
    out = {}
    for name, v in ste.card_values(card).items():
        vals = set()
        for x in v["values"]:
            if isinstance(x, (int, float)):
                vals.add(f"{float(x):g}")
            elif str(x).strip():
                vals.add(norm(x))
        if vals:
            out[name] = frozenset(vals)
    return out


def key_characteristics(indices, chars):
    """(подпись, проверочные) характеристики категории по заполненности. Проверочные — ещё и с небольшим
    числом разных значений: модель, артикул, цвет различаются у любых двух карточек и шумят."""
    fill = Counter(k for i in indices for k in chars[i])
    keys = [k for k, n in sorted(fill.items(), key=lambda x: (-x[1], x[0])) if n >= MIN_FILL * len(indices)]
    check = []
    for k in keys[SIGNATURE:]:
        distinct = len({chars[i][k] for i in indices if k in chars[i]})
        if distinct <= max(MIN_DISTINCT, MAX_DISTINCT_SHARE * fill[k]):
            check.append(k)
    return keys[:SIGNATURE], check


# --- алгоритмы: индексы карточек категории → метки -------------------------------------------------
def average_cosine(vectors, threshold):
    if len(vectors) < 2:
        return np.zeros(len(vectors), dtype=int)
    from sklearn.cluster import AgglomerativeClustering
    return AgglomerativeClustering(n_clusters=None, distance_threshold=threshold, metric="cosine",
                                   linkage="average").fit_predict(vectors)


def graph_components(vectors, similarity):
    """Single linkage: ребро, если косинус ≥ similarity; кластер — связная компонента."""
    if len(vectors) < 2:
        return np.zeros(len(vectors), dtype=int)
    adjacency = (vectors @ vectors.T) >= similarity
    return connected_components(adjacency, directed=False)[1]


def hdbscan(vectors, min_cluster_size):
    """cluster_selection_epsilon > 0 в sklearn 1.9 падает в _tree.pyx — не используется."""
    if len(vectors) <= min_cluster_size:
        return np.zeros(len(vectors), dtype=int) if len(vectors) == 1 else np.arange(len(vectors))
    from sklearn.cluster import HDBSCAN
    labels = HDBSCAN(min_cluster_size=min_cluster_size, min_samples=1, metric="euclidean",
                     copy=True).fit_predict(vectors)
    # шум — отдельные одиночные кластеры
    nxt = labels.max() + 1
    for i in np.where(labels == -1)[0]:
        labels[i] = nxt
        nxt += 1
    return labels


def signature_labels(indices, chars, signature, fallback=None):
    """Одинаковые значения подписи — один кластер; без полной подписи — fallback (или одиночка)."""
    labels, groups, rest = np.empty(len(indices), dtype=int), {}, []
    for pos, i in enumerate(indices):
        if signature and all(k in chars[i] for k in signature):
            key = tuple(chars[i][k] for k in signature)
            labels[pos] = groups.setdefault(key, len(groups))
        else:
            rest.append(pos)
    if rest:
        sub = fallback(rest) if fallback else np.arange(len(rest))
        for pos, label in zip(rest, sub):
            labels[pos] = len(groups) + label
    return labels


def run(method, categories_index, vectors, chars, names):
    """Кластеры всего каталога: method(индексы категории) → метки."""
    clusters = []
    for category, idx in categories_index.items():
        labels = method(category, idx)
        groups = defaultdict(list)
        for i, label in zip(idx, labels):
            groups[int(label)].append(i)
        for members in groups.values():
            clusters.append(Cluster(len(clusters), category, "", members))
    return clusters


def consistency(clusters, chars, check_keys, rng_seed=0):
    rng = random.Random(rng_seed)
    comparable = consistent = 0
    for c in clusters:
        m = c.ste_ids  # здесь — индексы карточек
        if len(m) < 2:
            continue
        keys = check_keys[c.category]
        if not keys:
            continue
        pairs = [(m[a], m[b]) for a in range(len(m)) for b in range(a + 1, len(m))] if len(m) <= 21 else \
                [tuple(rng.sample(m, 2)) for _ in range(PAIRS_PER_CLUSTER)]
        if len(pairs) > PAIRS_PER_CLUSTER:
            pairs = rng.sample(pairs, PAIRS_PER_CLUSTER)
        for a, b in pairs:
            shared = [k for k in keys if k in chars[a] and k in chars[b]]
            if not shared:
                continue
            comparable += 1
            consistent += all(chars[a][k] & chars[b][k] for k in shared)
    return {"comparable_pairs": comparable, "consistency": consistent / comparable if comparable else 0.0}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ste", type=Path, default=ste.DEFAULT_STE)
    ap.add_argument("--kpgz", type=Path, default=ste.DEFAULT_KPGZ)
    ap.add_argument("--output", type=Path, default=ste.ROOT / "reports/ste_clusters/benchmark.json")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    start = time.perf_counter()

    ids, cats, names, normalized, chars, groups = [], [], [], [], [], {}
    for c in iter_cards(args.ste):
        code = (c.get("production") or {}).get("code")
        name = c.get("name") or c.get("modelName")
        if c.get("_fetchError") or not code or not name:
            continue
        pos = len(ids)
        ids.append(c["id"]); cats.append(code); names.append(name)
        normalized.append(normalize_name(name) or name.lower())
        chars.append(values_of(c))
        if c.get("equivalentGroupId"):
            groups[pos] = c["equivalentGroupId"]
    index = defaultdict(list)
    for i, c in enumerate(cats):
        index[c].append(i)
    keys = {c: key_characteristics(idx, chars) for c, idx in index.items()}
    check_keys = {c: k[1] for c, k in keys.items()}
    log.info("карточек %d, категорий %d; категорий с проверочными характеристиками %d (%d карточек)",
             len(ids), len(index), sum(bool(k) for k in check_keys.values()),
             sum(len(index[c]) for c, k in check_keys.items() if k))

    search = KpgzSearch(ste.iter_array(args.kpgz), device="cuda", cache=CACHE, batch_size=64)
    search.max_length = 64  # короткие названия; общий кеш индекса КПГЗ (512 токенов) не трогаем

    def encoded(texts, cache):
        unique = sorted(set(texts))
        v = search.cached_encode(unique, cache, prompt=PROMPT)
        pos = {t: i for i, t in enumerate(unique)}
        return v[[pos[t] for t in texts]]

    gemma_norm = encoded(normalized, CACHE.parent / "ste_names_clustering.npz")
    gemma_raw = encoded(names, CACHE.parent / "ste_raw_names_clustering.npz")
    vectors = {"gemma_norm_256": truncate(gemma_norm, 256), "gemma_norm_768": truncate(gemma_norm, None),
               "gemma_raw_768": truncate(gemma_raw, None)}

    V = vectors["gemma_norm_256"]
    methods = {
        "ward_gemma_norm_256_t0.5": lambda c, idx: ward_labels(V[idx], .5),
        "ward_gemma_norm_256_t0.8": lambda c, idx: ward_labels(V[idx], .8),
        "signature_only": lambda c, idx: signature_labels(idx, chars, keys[c][0]),
        "signature+ward_name_t0.5": lambda c, idx: signature_labels(
            idx, chars, keys[c][0], lambda rest: ward_labels(V[[idx[p] for p in rest]], .5)),
        "signature_blocks→avgcos_name_0.3": lambda c, idx: hybrid(c, idx, keys, chars, V, .3),
    }
    for vec_name, W in vectors.items():
        for t in (.01, .02, .03, .05):
            methods[f"avgcos_{vec_name}_d{t}"] = lambda c, idx, W=W, t=t: average_cosine(W[idx], t)
    for s in (.97, .98, .99):
        methods[f"graph_gemma_norm_768_s{s}"] = lambda c, idx, s=s: graph_components(vectors["gemma_norm_768"][idx], s)
    for m in (2, 3):
        methods[f"hdbscan_gemma_norm_768_min{m}"] = lambda c, idx, m=m: hdbscan(vectors["gemma_norm_768"][idx], m)

    # Базовый уровень согласованности: случайные пары из одной категории (вся категория — один «кластер»).
    random_level = consistency([Cluster(i, c, "", idx) for i, (c, idx) in enumerate(index.items())],
                               chars, check_keys)
    log.info("случайные пары внутри категории: %s", random_level)
    results = {}

    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"cards": len(ids), "equivalent_labeled": len(groups),
                                           "random_pairs_in_category": random_level,
                                           "elapsed_seconds": round(time.perf_counter() - start, 1),
                                           "config": {"signature": SIGNATURE, "min_fill": MIN_FILL,
                                                      "pairs_per_cluster": PAIRS_PER_CLUSTER,
                                                      "max_distinct_share": MAX_DISTINCT_SHARE},
                                           "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")

    for name, method in methods.items():
        t = time.perf_counter()
        try:
            clusters = run(method, index, None, chars, names)
        except Exception as e:  # один метод не должен ронять сравнение
            log.exception("%s упал", name)
            results[name] = {"error": repr(e)}
            save()
            continue
        sizes = [len(c.ste_ids) for c in clusters]
        eq = pair_metrics(clusters, groups)
        r = {"clusters": len(clusters), "in_multi": sum(s for s in sizes if s > 1) / len(ids),
             "max_size": max(sizes), **{f"eq_{k}": v for k, v in eq.items() if k in ("precision", "recall", "f1")},
             **consistency(clusters, chars, check_keys), "seconds": round(time.perf_counter() - t, 1)}
        log.info("%-36s %s", name, {k: round(v, 3) if isinstance(v, float) else v for k, v in r.items()})
        results[name] = r
        save()
    print(f"случайные пары в категории: согласованность {random_level['consistency']:.1%}")
    print(f"{'метод':38} {'кластеров':>9} {'в группах':>9} {'eq P':>6} {'eq R':>6} {'eq F1':>6} {'согласов.':>9} {'пар':>8}")
    for name, r in results.items():
        if "error" in r:
            print(f"{name:38} ошибка: {r['error'][:60]}")
            continue
        print(f"{name:38} {r['clusters']:9} {r['in_multi']:9.1%} {r['eq_precision']:6.2f} {r['eq_recall']:6.2f} "
              f"{r['eq_f1']:6.2f} {r['consistency']:9.1%} {r['comparable_pairs']:8}")


def hybrid(category, idx, keys, chars, V, threshold):
    """Блоки по подписи характеристик; внутри блока и среди карточек без подписи — средняя связь по названию."""
    signature = keys[category][0]
    blocks = defaultdict(list)
    for pos, i in enumerate(idx):
        key = tuple(chars[i][k] for k in signature) if signature and all(k in chars[i] for k in signature) else None
        blocks[key].append(pos)
    labels, offset = np.empty(len(idx), dtype=int), 0
    for members in blocks.values():
        sub = average_cosine(V[[idx[p] for p in members]], threshold)
        for p, label in zip(members, sub):
            labels[p] = offset + label
        offset += int(sub.max()) + 1
    return labels


if __name__ == "__main__":
    main()
