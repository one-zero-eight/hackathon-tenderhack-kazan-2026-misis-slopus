"""Precision / recall / F1 для поиска категории (одна верная категория на запрос).

При ответе на каждый запрос micro-P = micro-R = micro-F1 = доля верных, поэтому считаются:
  macro / weighted — по категориям: precision(c) — доля верных среди ответов «c», recall(c) — доля
                     найденных среди запросов с эталоном c; weighted — среднее с весом числа эталонов;
  с отказом       — ответ только при уверенности ≥ порога: precision = верные / отвеченные,
                     recall = верные / все запросы (coverage — доля отвеченных);
  набор кандидатов — top-k категорий: recall = эталон в наборе, precision = попадания / выдано категорий.
"""

from __future__ import annotations

from collections import Counter
from typing import Sequence


def f1(p: float, r: float) -> float:
    return 2 * p * r / (p + r) if p + r else 0.0


def per_category(gold: Sequence[str], pred: Sequence[str]) -> dict[str, dict[str, float]]:
    """Метрики по каждой категории, встреченной в эталоне или ответах."""
    tp, predicted, actual = Counter(), Counter(pred), Counter(gold)
    for g, p in zip(gold, pred):
        tp[g] += g == p
    out = {}
    for c in actual.keys() | predicted.keys():
        p = tp[c] / predicted[c] if predicted[c] else 0.0
        r = tp[c] / actual[c] if actual[c] else 0.0
        out[c] = {"precision": p, "recall": r, "f1": f1(p, r), "support": actual[c], "predicted": predicted[c]}
    return out


def classification(gold: Sequence[str], pred: Sequence[str]) -> dict[str, float]:
    """micro (= accuracy), macro и weighted P/R/F1. Macro — по категориям из эталона: категория,
    которую только предсказывали, понижает precision своих ответов, но не входит в среднее."""
    if len(gold) != len(pred) or not gold:
        raise ValueError("gold и pred должны быть непустыми и одной длины")
    cats = per_category(gold, pred)
    in_gold = [c for c in cats if cats[c]["support"]]
    n = len(gold)
    accuracy = sum(g == p for g, p in zip(gold, pred)) / n
    out = {"n": n, "categories": len(in_gold), "micro_precision": accuracy, "micro_recall": accuracy,
           "micro_f1": accuracy}
    for avg, weight in (("macro", lambda c: 1), ("weighted", lambda c: cats[c]["support"])):
        total = sum(weight(c) for c in in_gold)
        for m in ("precision", "recall", "f1"):
            out[f"{avg}_{m}"] = sum(cats[c][m] * weight(c) for c in in_gold) / total
    return out


def with_abstention(gold: Sequence[str], pred: Sequence[str], confidence: Sequence[float],
                    thresholds: Sequence[float]) -> list[dict[str, float]]:
    """Ответ только при confidence ≥ t; на каждый порог — precision, recall, F1 и coverage."""
    n, out = len(gold), []
    for t in thresholds:
        answered = [(g, p) for g, p, c in zip(gold, pred, confidence) if c >= t]
        correct = sum(g == p for g, p in answered)
        p = correct / len(answered) if answered else 0.0
        r = correct / n
        out.append({"threshold": t, "answered": len(answered), "coverage": len(answered) / n,
                    "precision": p, "recall": r, "f1": f1(p, r)})
    return out


def candidate_sets(gold: Sequence[str], ranked: Sequence[Sequence[str]], ks: Sequence[int]) -> list[dict[str, float]]:
    """top-k категорий как набор: recall@k — эталон в наборе; precision@k — попадания / выдано категорий
    (выдано может быть меньше k, если у соседей меньше разных категорий)."""
    out = []
    for k in ks:
        hits = sum(g in r[:k] for g, r in zip(gold, ranked))
        shown = sum(len(r[:k]) for r in ranked)
        p, rec = hits / shown, hits / len(gold)
        out.append({"k": k, "recall": rec, "precision": p, "f1": f1(p, rec), "mean_set_size": shown / len(gold)})
    return out
