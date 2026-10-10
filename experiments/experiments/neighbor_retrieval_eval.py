"""Поиск соседей-СТЕ: EmbeddingGemma против bge-m3 (dense / sparse / ColBERT / их смеси) на одной выборке.

    .venv/bin/python experiments/neighbor_retrieval_eval.py --family-holdout

Выборка и корпус — как в category_hypotheses_eval.py (--sample 300 --seed 1): те же запросы, те же
исключения из корпуса. Меняется только поиск соседей. Метрики — без модели выбора:
  reach@5        верная категория среди категорий 5 ближайших карточек (кандидаты Jev на 5 соседях);
  reach@25/10    среди ≤10 категорий голоса 25 соседей (кандидаты Jev на 25 соседях);
  nearest_1      категория ближайшей карточки;
  vote_5         взвешенный голос 5 соседей, exp((s − s_max) / T), T = 0,05 × (разброс шкалы способа).
Шкалы разные (sparse — сумма весов совпавших токенов), поэтому T масштабируется по медиане
разности score(1-й) − score(25-й) способа относительно EmbeddingGemma.
bge-m3: FlagEmbedding BGEM3FlagModel, fp16, без префиксов; документ — «название\\nописание».
Смесь m3 — веса 0,4 dense + 0,2 sparse + 0,4 ColBERT (рекомендация BGE M3); rrf — Reciprocal
Rank Fusion (k = 60) EmbeddingGemma и смеси m3.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from pathlib import Path
import time

import numpy as np

import category_hypotheses_eval as hyp
import ste_to_jev as ste
from ktru_autofill.kpgz_search import CACHE, KpgzSearch, SteNeighbors, collect_corpus, iter_cards, neighbor_ranking

log = logging.getLogger(__name__)
BGE = "BAAI/bge-m3"
M3_WEIGHTS = (.4, .2, .4)


def bge_encode(model, texts, cache: Path, max_length: int, batch_size: int):
    """dense [n, 1024], sparse как CSR (token id → вес), ColBERT — склеенные векторы и смещения."""
    fingerprint = hashlib.sha256(json.dumps({"model": BGE, "len": max_length, "texts": texts},
                                            ensure_ascii=False).encode()).hexdigest()
    if cache.exists():
        with np.load(cache, allow_pickle=False) as d:
            if d["fingerprint"].item() == fingerprint:
                return {k: d[k] for k in d.files if k != "fingerprint"}
    out = model.encode(texts, batch_size=batch_size, max_length=max_length, return_dense=True,
                       return_sparse=True, return_colbert_vecs=True)
    indptr, indices, weights = [0], [], []
    for lw in out["lexical_weights"]:
        indices.extend(int(t) for t in lw)
        weights.extend(float(w) for w in lw.values())
        indptr.append(len(indices))
    lengths = [len(v) for v in out["colbert_vecs"]]
    data = {"dense": np.asarray(out["dense_vecs"], dtype=np.float32),
            "sp_indptr": np.array(indptr, dtype=np.int64), "sp_indices": np.array(indices, dtype=np.int64),
            "sp_weights": np.array(weights, dtype=np.float32),
            "colbert": np.concatenate(out["colbert_vecs"]).astype(np.float16),
            "colbert_offsets": np.concatenate([[0], np.cumsum(lengths)]).astype(np.int64)}
    cache.parent.mkdir(parents=True, exist_ok=True)
    np.savez(cache, fingerprint=fingerprint, **data)
    return data


def sparse_matrix(d, vocab):
    from scipy.sparse import csr_matrix
    return csr_matrix((d["sp_weights"], d["sp_indices"], d["sp_indptr"]), shape=(len(d["sp_indptr"]) - 1, vocab))


def colbert_scores(q, d, device="cuda"):
    """MaxSim: для каждого токена запроса — лучший токен документа, среднее по токенам запроса."""
    import torch
    docs = torch.tensor(d["colbert"], device=device)
    offsets = d["colbert_offsets"]
    owner = torch.repeat_interleave(torch.arange(len(offsets) - 1, device=device),
                                    torch.tensor(np.diff(offsets), device=device))
    out = np.zeros((len(q["colbert_offsets"]) - 1, len(offsets) - 1), dtype=np.float32)
    for i in range(len(out)):
        a, b = q["colbert_offsets"][i], q["colbert_offsets"][i + 1]
        sims = torch.tensor(q["colbert"][a:b], device=device) @ docs.T  # [Lq, всего токенов]
        best = torch.full((b - a, len(offsets) - 1), -1e4, device=device, dtype=sims.dtype)
        best.scatter_reduce_(1, owner.expand(b - a, -1), sims, reduce="amax")
        out[i] = best.float().mean(0).cpu().numpy()
    return out


def rrf(*score_sets, k=60):
    total = np.zeros_like(score_sets[0])
    for s in score_sets:
        ranks = np.argsort(np.argsort(-s, axis=1, kind="stable"), axis=1)
        total += 1 / (k + ranks + 1)
    return total


def evaluate(name, scores, corpus, queries, temperature):
    rows = {"reach@5": 0, "reach@25/10": 0, "nearest_1": 0, "vote_5": 0, "vote_25": 0}
    hits = {}
    for q, row in zip(queries, scores):
        r5, _ = neighbor_ranking(row, corpus, 5, temperature=temperature)
        r25, _ = neighbor_ranking(row, corpus, 25, temperature=temperature)
        r1, _ = neighbor_ranking(row, corpus, 1, vote=False)
        flags = {"reach@5": q["code"] in r5, "reach@25/10": q["code"] in r25[:10], "nearest_1": r1[0] == q["code"],
                 "vote_5": r5[0] == q["code"], "vote_25": r25[0] == q["code"]}
        for k, v in flags.items():
            rows[k] += v
        hits[q["id"]] = flags
    log.info("%s: %s", name, rows)
    return rows, hits


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ste", type=Path, default=ste.DEFAULT_STE)
    ap.add_argument("--kpgz", type=Path, default=ste.DEFAULT_KPGZ)
    ap.add_argument("--baseline", type=Path, default=ste.ROOT / "reports/ste_jev_search_dry/eval_all_n20_seed0.json")
    ap.add_argument("--sample", type=int, default=300)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--family-holdout", action="store_true")
    ap.add_argument("--max-length", type=int, default=512)
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    variant = "family" if args.family_holdout else "std"
    tag = f"category_sample_n{args.sample}_seed{args.seed}_{variant}"
    output = args.output or ste.ROOT / f"reports/{tag}/neighbor_retrieval.json"
    start = time.perf_counter()

    search = KpgzSearch(ste.iter_array(args.kpgz), device="cuda", cache=CACHE, batch_size=16)
    codes = {n["code"] for n in search.nodes}
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    queries, _ = hyp.sample_queries(args.ste, codes, args.sample, args.seed, {x["ste"] for x in baseline["category_search"]})
    corpus, stats = collect_corpus(iter_cards(args.ste), codes, queries, 20, 20000, args.family_holdout)
    log.info("Корпус: %s", stats)

    # EmbeddingGemma — те же кеши, что у category_hypotheses_eval (иначе пересчёт).
    gq = search.cached_encode([q["text"] for q in queries], CACHE.parent / f"{tag}_queries.npz", query=True)
    gd = SteNeighbors(search, corpus, cache=CACHE.parent / f"{tag}_ste.npz").vectors
    scores = {"gemma": gq @ gd.T}

    from FlagEmbedding import BGEM3FlagModel
    model = BGEM3FlagModel(BGE, use_fp16=True, devices="cuda:0")
    bq = bge_encode(model, [q["text"] for q in queries], CACHE.parent / f"bge_m3_{tag}_queries.npz",
                    args.max_length, args.batch_size)
    bd = bge_encode(model, [c["text"] for c in corpus], CACHE.parent / f"bge_m3_{tag}_ste.npz",
                    args.max_length, args.batch_size)
    del model
    vocab = int(max(bq["sp_indices"].max(), bd["sp_indices"].max())) + 1
    scores["bge_dense"] = bq["dense"] @ bd["dense"].T
    scores["bge_sparse"] = (sparse_matrix(bq, vocab) @ sparse_matrix(bd, vocab).T).toarray().astype(np.float32)
    scores["bge_colbert"] = colbert_scores(bq, bd)
    w = M3_WEIGHTS
    scores["bge_m3"] = w[0] * scores["bge_dense"] + w[1] * scores["bge_sparse"] + w[2] * scores["bge_colbert"]
    scores["rrf_gemma_m3"] = rrf(scores["gemma"], scores["bge_m3"])
    scores["rrf_gemma_dense_sparse"] = rrf(scores["gemma"], scores["bge_dense"], scores["bge_sparse"])

    def spread(s):
        top = -np.sort(-s, axis=1)[:, :25]
        return float(np.median(top[:, 0] - top[:, 24]))

    base = spread(scores["gemma"])
    results, hits = {}, {}
    for name, s in scores.items():
        t = .05 * spread(s) / base
        results[name], hits[name] = evaluate(name, s, corpus, queries, t)
        results[name]["temperature"] = round(t, 4)
    n = len(queries)
    report = {"n": n, "corpus": stats, "variant": variant, "elapsed_seconds": round(time.perf_counter() - start, 1),
              "config": {"bge": BGE, "max_length": args.max_length, "m3_weights": M3_WEIGHTS, "rrf_k": 60},
              "results": results, "hits": {k: {str(i): v for i, v in h.items()} for k, h in hits.items()}}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{'способ':24} " + " ".join(f"{k:>12}" for k in next(iter(results.values())) if k != "temperature"))
    for name, r in results.items():
        print(f"{name:24} " + " ".join(f"{r[k]:>12}" for k in r if k != "temperature"))
    log.info("Отчёт: %s", output)


if __name__ == "__main__":
    main()
