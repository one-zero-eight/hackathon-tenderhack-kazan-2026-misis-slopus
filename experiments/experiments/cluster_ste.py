"""Кластеризация всех СТЕ внутри категорий КПГЗ и подбор порога по группам эквивалентов портала.

    .venv/bin/python experiments/cluster_ste.py --ste ~/tenderhack/zakupki-reverse/data/ste-details.jsonl.gz

Метод — ktru_autofill.ste_clusters: нормализованное название → EmbeddingGemma с промптом кластеризации
→ внутри категории средняя связь по косинусу (по умолчанию, все 768 координат) или Ward
(`--linkage ward --dim 256`, как в one-zero-eight/hackathon-tenderhack).
Порог выбирается по попарному F1 на карточках с `equivalentGroupId` (группы эквивалентов портала):
пара из одной группы должна попасть в один кластер, из разных групп — в разные. Таких карточек
около 700 из 134 тыс. — это проверка на узком срезе, а не на всём каталоге.
Пишет reports/ste_clusters/: sweep.json (метрики по порогам), clusters.jsonl.gz (кластеры).
"""

from __future__ import annotations

import argparse
from collections import Counter
import gzip
import json
import logging
from pathlib import Path
import time

import numpy as np

import ste_to_jev as ste
from ktru_autofill.kpgz_search import CACHE, KpgzSearch, iter_cards
from ktru_autofill.ste_clusters import cluster_ste, normalize_name, pair_metrics, truncate

log = logging.getLogger(__name__)
PROMPT = "task: clustering | query: "  # промпт EmbeddingGemma для кластеризации
THRESHOLDS = {"average": [.02, .03, .04, .05, .06, .08], "ward": [.2, .3, .4, .5, .6, .7, .8, 1.0]}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ste", type=Path, default=ste.DEFAULT_STE)
    ap.add_argument("--kpgz", type=Path, default=ste.DEFAULT_KPGZ)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--linkage", choices=["average", "ward"], default="average")
    ap.add_argument("--dim", type=int, default=0, help="первые координаты эмбеддинга (Matryoshka); 0 — все 768")
    ap.add_argument("--threshold", type=float,
                    help="порог (average — косинусное расстояние); по умолчанию — лучший F1 по группам эквивалентов")
    ap.add_argument("--output", type=Path, default=ste.ROOT / "reports/ste_clusters")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    start = time.perf_counter()

    names_of = {n["code"]: n["name"] for n in ste.iter_array(args.kpgz)}
    ids, categories, names, normalized, groups = [], [], [], [], {}
    for c in iter_cards(args.ste):
        code = (c.get("production") or {}).get("code")
        name = c.get("name") or c.get("modelName")
        if c.get("_fetchError") or not code or not name:
            continue
        ids.append(c["id"])
        categories.append(code)
        names.append(name)
        normalized.append(normalize_name(name) or name.lower())
        if c.get("equivalentGroupId"):
            groups[c["id"]] = c["equivalentGroupId"]
    unique = sorted(set(normalized))
    log.info("карточек %d, категорий %d, уникальных нормализованных названий %d, с группой эквивалентов %d",
             len(ids), len(set(categories)), len(unique), len(groups))

    search = KpgzSearch(ste.iter_array(args.kpgz), device=args.device, cache=CACHE, batch_size=64)
    search.max_length = 64  # короткие названия; общий кеш индекса КПГЗ (512 токенов) не трогаем
    unique_vectors = search.cached_encode(unique, CACHE.parent / "ste_names_clustering.npz", prompt=PROMPT)
    position = {t: i for i, t in enumerate(unique)}
    vectors = truncate(unique_vectors[[position[t] for t in normalized]], args.dim)

    cross = Counter()
    category_of = dict(zip(ids, categories))
    for g in set(groups.values()):
        cross[len({category_of[s] for s, x in groups.items() if x == g})] += 1
    log.info("групп эквивалентов по числу категорий: %s", dict(cross))

    sweep = []
    for t in ([args.threshold] if args.threshold else THRESHOLDS[args.linkage]):
        clusters = cluster_ste(ids, categories, names, vectors, t, args.linkage)
        sizes = [len(c.ste_ids) for c in clusters]
        m = pair_metrics(clusters, groups) | {
            "threshold": t, "clusters": len(clusters), "singletons": sum(s == 1 for s in sizes),
            "mean_size": len(ids) / len(clusters), "max_size": max(sizes)}
        log.info("порог %.2f: %s", t, {k: round(v, 3) if isinstance(v, float) else v for k, v in m.items()})
        sweep.append(m)
    best = max(sweep, key=lambda m: (m["f1"], -m["threshold"]))
    clusters = cluster_ste(ids, categories, names, vectors, best["threshold"], args.linkage)

    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "sweep.json").write_text(json.dumps(
        {"config": {"dim": args.dim, "prompt": PROMPT, "model": search.model_name, "linkage": args.linkage},
         "cards": len(ids), "equivalent_labeled": len(groups), "chosen_threshold": best["threshold"],
         "elapsed_seconds": round(time.perf_counter() - start, 1), "sweep": sweep},
        ensure_ascii=False, indent=2), encoding="utf-8")
    with gzip.open(args.output / "clusters.jsonl.gz", "wt", encoding="utf-8") as f:
        for c in clusters:
            f.write(json.dumps({"id": c.id, "category": c.category, "category_name": names_of.get(c.category),
                                "name": c.name, "size": len(c.ste_ids), "medoid": c.medoid, "ste_ids": c.ste_ids},
                               ensure_ascii=False) + "\n")
    print(json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in best.items()}, ensure_ascii=False))
    name_of = dict(zip(ids, names))
    for c in sorted(clusters, key=lambda c: -len(c.ste_ids))[:8]:
        print(f"{len(c.ste_ids):5} {names_of.get(c.category, c.category)[:40]:40} «{c.name[:40]}»  напр.: "
              + " | ".join(name_of[s][:35] for s in c.ste_ids[:3]))
    log.info("Кластеры: %s", args.output / "clusters.jsonl.gz")


if __name__ == "__main__":
    main()
