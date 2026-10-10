"""Как найти товар в реальном КТРУ: плоский поиск по названиям vs дерево ОКПД2 (beam search).

    uv run python experiments/retrieval_eval.py

Метрики по data-archived/test_real_retrieval.jsonl (эталон — допустимые названия позиций КТРУ):
  попал    — среди оставшихся позиций есть эталонная;
  топ-1    — самое похожее из оставшихся названий — эталонное;
  в луче   — (дерево) эталонная позиция есть в листьях лучей до выбора по названию;
  позиций  — медиана числа оставшихся позиций (чем меньше, тем сильнее сужение).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ktru_autofill.embedder import SentenceTransformerEmbedder  # noqa: E402
from ktru_autofill.hierarchy import TERMINAL, FlatRetriever, GroupedRetriever, OkpdTree  # noqa: E402
from ktru_autofill.schema import KtruCandidate  # noqa: E402


def run(name, retriever, rows, positions, errors=None):
    hit = top1 = beam_hit = 0
    sizes, times = [], []
    for r in rows:
        gold = set(r["titles"])
        t = time.perf_counter()
        res = retriever.retrieve(r["text"])
        times.append(time.perf_counter() - t)
        titles = {positions[i].name for i in res.candidates}
        hit += bool(titles & gold)
        top1 += bool(res.titles and res.titles[0][0] in gold)
        sizes.append(len(res.candidates))
        if res.paths:
            leaves = [p.nodes[-1].rstrip(TERMINAL) for p in res.paths]
            in_beam = {positions[i].name for c in leaves for i in retriever.nodes[c].positions}
            beam_hit += bool(in_beam & gold)
        if errors is not None and not titles & gold:
            errors.append((name, r["text"], res.titles[:2], [s for s, _ in res.steps][:2]))
    n = len(rows)
    beam = f"{100 * beam_hit / n:5.1f}%" if hasattr(retriever, "nodes") else "    —"
    print(f"{name:42} попал {100 * hit / n:5.1f}%  топ-1 {100 * top1 / n:5.1f}%  в луче {beam}  "
          f"позиций (медиана) {median(sizes):5.0f}  {1000 * median(times):4.0f} мс")


def main():
    positions = [KtruCandidate.from_dict(d) for d in json.loads((ROOT / "data-archived/ktru_real.json").read_text())]
    rows = [json.loads(line) for line in (ROOT / "data-archived/test_real_retrieval.jsonl").open()]
    emb = SentenceTransformerEmbedder()
    print(f"{len(rows)} запросов, {len(positions)} позиций КТРУ\n")
    errors: list = []
    run("плоский: ближайшие названия КТРУ", FlatRetriever(positions, emb), rows, positions, errors)
    for level in (2, 4, 6):
        run(f"снизу вверх: голоса за группу ОКПД2 ({level} цифры)", GroupedRetriever(positions, emb, level=level),
            rows, positions)
    tree = OkpdTree.load(positions, emb)
    for mode in ("prob", "raw"):
        for alpha in (0.0, 0.25):
            for beam in (5, 10):
                tree.path_mode, tree.alpha, tree.top_titles, tree.beam = mode, alpha, 1, beam
                run(f"дерево {mode} α={alpha} K={beam}", tree, rows, positions,
                    errors if (mode, alpha, beam) == ("raw", 0.0, 10) else None)
    print("\nПромахи (плоский и дерево raw α=0 K=10):")
    for e in errors:
        print("  ", e)


if __name__ == "__main__":
    main()
