"""Поиск категории голосом соседей-СТЕ с кластеризацией и без неё.

    .venv/bin/python experiments/cluster_corpus_eval.py --family-holdout

Те же 300 СТЕ (seed 1) и те же исключения, что в category_hypotheses_eval.py. Кластеры — средняя связь
по косинусу (ktru_autofill.ste_clusters, порог 0,05) по названиям карточек, оставшихся после исключений,
внутри категории — проверяемые карточки в кластеризации не участвуют. Сравниваются корпуса:
  hash20   — до 20 карточек на категорию по хешу ID (как сейчас);
  medoid20 — до 20 на категорию: медоиды кластеров от крупных к мелким, затем вторые по близости и т. д.;
  full     — с --full: все допустимые карточки (после тех же исключений и дедупликации);
и голосование: plain — k ближайших карточек; dedup — k ближайших кластеров (из кластера голосует
только ближайшая карточка). Метрики — без модели выбора: верная категория среди кандидатов голоса
5 соседей / ≤10 категорий 25 соседей, точность голоса 5 / 25, точность и полнота при доле голосов ≥ 0,5.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
import logging
from pathlib import Path
import time

import numpy as np

import category_hypotheses_eval as hyp
import ste_to_jev as ste
from ktru_autofill.kpgz_search import CACHE, KpgzSearch, card_text, collect_corpus, iter_cards
from ktru_autofill.ste_clusters import average_labels, normalize_name, truncate

log = logging.getLogger(__name__)
CLUSTER_PROMPT = "task: clustering | query: "
THRESHOLD = .05
T = .05


def ranked_neighbors(scores, corpus, cluster_of, k, dedup):
    order = np.argsort(-scores, kind="stable")
    out, seen = [], set()
    for i in order:
        if dedup:
            c = cluster_of[corpus[i]["id"]]
            if c in seen:
                continue
            seen.add(c)
        out.append((corpus[i]["code"], float(scores[i])))
        if len(out) == k:
            break
    return out


def vote(neighbors):
    top = neighbors[0][1]
    weights = defaultdict(float)
    for code, s in neighbors:
        weights[code] += np.exp((s - top) / T)
    ranked = sorted(weights, key=lambda c: (-weights[c], c))
    return ranked, weights[ranked[0]] / sum(weights.values())


def medoid_corpus(pool, name_vectors, row_of, cluster_of, per_category=20):
    """Медоиды кластеров по убыванию размера, затем следующие по близости к центру — по кругу."""
    by_cat = defaultdict(lambda: defaultdict(list))
    for c in pool:
        by_cat[c["code"]][cluster_of[c["id"]]].append(c)
    out = []
    for code in sorted(by_cat):
        queues = []
        for members in by_cat[code].values():
            vecs = name_vectors[[row_of[m["id"]] for m in members]]
            center = vecs.mean(axis=0)
            order = np.argsort(-(vecs @ center), kind="stable")
            queues.append([members[i] for i in order])
        queues.sort(key=lambda q: (-len(q), q[0]["id"]))
        picked, r = [], 0
        while len(picked) < per_category and any(r < len(q) for q in queues):
            picked += [q[r] for q in queues if r < len(q)][:per_category - len(picked)]
            r += 1
        out += picked
    return out


def build_corpora(search, ste_path, queries, family_holdout, tag, full=False):
    """Пул карточек после исключений, кластеры (средняя связь по косинусу) и корпуса hash20 / medoid20 / full."""
    codes = set(search.by_code)
    # Все карточки: документы для поиска (один кеш на оба варианта) и нормализованные названия
    # (тот же список, что у experiments/cluster_ste.py, — его кеш эмбеддингов).
    cards, all_normalized = [], []
    for c in iter_cards(ste_path):
        code = (c.get("production") or {}).get("code")
        name = c.get("name") or c.get("modelName")
        if c.get("_fetchError") or not code or not name:
            continue
        all_normalized.append(normalize_name(name) or name.lower())
        if code in codes and card_text(c):
            cards.append({"id": c["id"], "code": code, "name": name, "text": card_text(c),
                          "normalized": all_normalized[-1]})
    unique = sorted(set(all_normalized))
    pos = {t: i for i, t in enumerate(unique)}
    name_unique = search.cached_encode(unique, CACHE.parent / "ste_names_clustering.npz", prompt=CLUSTER_PROMPT)
    name_vectors = truncate(name_unique[[pos[c["normalized"]] for c in cards]], None)
    row_of = {c["id"]: i for i, c in enumerate(cards)}
    log.info("карточек со схемой: %d", len(cards))

    pool, pool_stats = collect_corpus(iter_cards(ste_path), codes, queries, 4000, 10 ** 9, family_holdout)  # 4000 > крупнейшей категории
    hash20, hash_stats = collect_corpus(iter_cards(ste_path), codes, queries, 20, 20000, family_holdout)
    pool = [c for c in pool if c["id"] in row_of]
    hash20 = [c for c in hash20 if c["id"] in row_of]
    cluster_of, by_cat = {}, defaultdict(list)
    for c in pool:
        by_cat[c["code"]].append(c)
    n_clusters = 0
    for code, members in by_cat.items():
        labels = average_labels(name_vectors[[row_of[m["id"]] for m in members]], THRESHOLD)
        for m, label in zip(members, labels):
            cluster_of[m["id"]] = n_clusters + int(label)
        n_clusters += int(labels.max()) + 1
    for c in hash20:  # карточки hash20 — подмножество пула
        cluster_of.setdefault(c["id"], -c["id"])
    medoid20 = medoid_corpus(pool, name_vectors, row_of, cluster_of)
    corpora = {"hash20": hash20, "medoid20": medoid20} | ({"full": pool} if full else {})
    log.info("пул %d карточек, %d кластеров; корпуса: %s", len(pool), n_clusters,
             {k: len(v) for k, v in corpora.items()})

    return {"corpora": corpora, "cluster_of": cluster_of, "pool_stats": pool_stats, "hash_stats": hash_stats,
            "n_clusters": n_clusters}


def corpus_vectors(search, corpus, corpus_name, tag):
    """Векторы корпуса; hash20 совпадает с корпусом category_hypotheses_eval (его кеш)."""
    cache = CACHE.parent / (f"{tag}_ste.npz" if corpus_name == "hash20" else f"{tag}_{corpus_name}_ste.npz")
    return search.cached_encode([f"title: {c['name']} | text: {c['text']}" for c in corpus], cache)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ste", type=Path, default=ste.DEFAULT_STE)
    ap.add_argument("--kpgz", type=Path, default=ste.DEFAULT_KPGZ)
    ap.add_argument("--baseline", type=Path, default=ste.ROOT / "reports/ste_jev_search_dry/eval_all_n20_seed0.json")
    ap.add_argument("--sample", type=int, default=300)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--family-holdout", action="store_true")
    ap.add_argument("--full", action="store_true", help="ещё и полный корпус (≈ 127 тыс. карточек, ≈ 30 мин на V100)")
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    start = time.perf_counter()
    variant = "family" if args.family_holdout else "std"
    tag = f"category_sample_n{args.sample}_seed{args.seed}_{variant}"
    output = args.output or ste.ROOT / f"reports/{tag}/cluster_corpus.json"

    search = KpgzSearch(ste.iter_array(args.kpgz), device="cuda", cache=CACHE, batch_size=16)
    codes = set(search.by_code)
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    queries, _ = hyp.sample_queries(args.ste, codes, args.sample, args.seed, {x["ste"] for x in baseline["category_search"]})

    built = build_corpora(search, args.ste, queries, args.family_holdout, tag, args.full)
    corpora, cluster_of = built["corpora"], built["cluster_of"]
    pool_stats, hash_stats, n_clusters = built["pool_stats"], built["hash_stats"], built["n_clusters"]

    query_vectors = search.cached_encode([q["text"] for q in queries], CACHE.parent / f"{tag}_queries.npz", query=True)
    results, hits = {}, {}
    for corpus_name, corpus in corpora.items():
        vectors = corpus_vectors(search, corpus, corpus_name, tag)
        scores = query_vectors @ vectors.T
        n_corpus_clusters = len({cluster_of[c["id"]] for c in corpus})
        for dedup in (False, True):
            name = f"{corpus_name}_{'dedup' if dedup else 'plain'}"
            m = defaultdict(int)
            per_query = {}
            for q, row in zip(queries, scores):
                n5 = ranked_neighbors(row, corpus, cluster_of, 5, dedup)
                n25 = ranked_neighbors(row, corpus, cluster_of, 25, dedup)
                r5, share5 = vote(n5)
                share5 = float(share5)
                r25, _ = vote(n25)
                flags = {"reach@5": q["code"] in r5, "reach@25/10": q["code"] in r25[:10],
                         "vote_5": r5[0] == q["code"], "vote_25": r25[0] == q["code"],
                         "answered_0.5": share5 >= .5, "correct_0.5": share5 >= .5 and r5[0] == q["code"]}
                flags = {k: bool(v) for k, v in flags.items()}
                for k, v in flags.items():
                    m[k] += int(v)
                per_query[str(q["id"])] = flags
            n = len(queries)
            m = dict(m) | {"corpus": len(corpus), "corpus_clusters": n_corpus_clusters,
                           "precision_0.5": m["correct_0.5"] / max(m["answered_0.5"], 1),
                           "recall_0.5": m["correct_0.5"] / n}
            log.info("%-16s %s", name, m)
            results[name], hits[name] = m, per_query
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"n": len(queries), "variant": variant, "threshold": THRESHOLD,
                                  "pool": pool_stats, "hash20": hash_stats, "pool_clusters": n_clusters,
                                  "elapsed_seconds": round(time.perf_counter() - start, 1),
                                  "results": results, "hits": hits}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{'корпус / голос':18} {'корпус':>7} {'кластеров':>9} {'в 5':>5} {'в 25/10':>7} {'голос5':>6} {'голос25':>7} "
          f"{'P@0.5':>6} {'R@0.5':>6}")
    for name, r in results.items():
        print(f"{name:18} {r['corpus']:7} {r['corpus_clusters']:9} {r['reach@5']:5} {r['reach@25/10']:7} "
              f"{r['vote_5']:6} {r['vote_25']:7} {r['precision_0.5']:6.1%} {r['recall_0.5']:6.1%}")


if __name__ == "__main__":
    main()
