"""Замер на датасете организаторов (TenderHack_СТЕ: наименование, категория, характеристики).

    uv run python experiments/tenderhack_eval.py --data ~/data/tenderhack/ste_2026-03-13.jsonl \
        --model intfloat/multilingual-e5-small --n-test 1000

Разбиение по идентификатору СТЕ: 10 % — тест, остальное — «справочник». Из справочника
строятся анкеты категорий (допустимые значения, единицы) и примеры названий; тестовые
значения в анкеты не попадают.

1. Категория по наименованию СТЕ (аналог шага «какой это товар»), 5 311 категорий:
   - «по названию категории» — косинус наименования и названия категории;
   - «kNN по СТЕ» — категория ближайших наименований справочника (≤ 10 на категорию).
   Метрики: верная категория первой (acc@1) и в первой пятёрке (acc@5).
2. Характеристики: извлечение по анкете верной категории — точность, полнота, выдумано;
   артефакт как у ktru-eval (report.md, items.jsonl, values.csv, metrics.json).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import zlib
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ktru_autofill import Autofiller  # noqa: E402
from ktru_autofill.embedder import SentenceTransformerEmbedder  # noqa: E402
from ktru_autofill.evaluate import Ste, evaluate, forms_from_dataset, print_report, write_artifacts  # noqa: E402
from ktru_autofill.numbers import product_text  # noqa: E402


def is_test(ste_id) -> bool:
    return zlib.crc32(str(ste_id).encode()) % 10 == 0


def rows(path: Path):
    with path.open(encoding="utf-8") as f:
        for line in f:
            yield json.loads(line)


def load(path: Path, n_test: int, min_cat: int, per_cat: int, seed: int, cand_min: int = 0):
    counts = Counter(r["family"] for r in rows(path))
    eligible = {c for c, k in counts.items() if k >= min_cat}
    test_pool = [r for r in rows(path) if is_test(r["id"]) and r["family"] in eligible and r["characteristics"]]
    test = random.Random(seed).sample(test_pool, min(n_test, len(test_pool)))
    test_cats = {r["family"] for r in test}
    train_for_forms: list[dict] = []
    examples: dict[str, list[str]] = defaultdict(list)
    for r in rows(path):
        if is_test(r["id"]):
            continue
        if r["family"] in test_cats:
            train_for_forms.append(r)
        if counts[r["family"]] >= cand_min and len(examples[r["family"]]) < per_cat:
            examples[r["family"]].append(r["text"])
    return counts, test, train_for_forms, examples


def to_ste(r: dict) -> Ste:
    return Ste(str(r["id"]), r["text"], r["characteristics"], r["family"], r["family"])


def category_benchmark(emb, test: list[dict], examples: dict[str, list[str]]) -> dict:
    cats = sorted(examples)
    t0 = time.time()
    cat_vecs = emb.encode([c.lower() for c in cats])
    ex_texts, ex_cat = [], []
    for i, c in enumerate(cats):
        for t in examples[c]:
            ex_texts.append(product_text(t).lower())
            ex_cat.append(i)
    ex_vecs = emb.encode(ex_texts)
    ex_cat = np.array(ex_cat)
    q = emb.encode([product_text(r["text"]).lower() for r in test])
    encode_s = time.time() - t0

    gold = np.array([cats.index(r["family"]) for r in test])
    res = {}
    # по названию категории
    s = q @ cat_vecs.T
    top = np.argsort(-s, axis=1)[:, :5]
    res["по названию категории"] = (float((top[:, 0] == gold).mean()), float((top == gold[:, None]).any(1).mean()))
    # kNN: оценка категории — максимум сходства с её примерами
    sims = q @ ex_vecs.T
    best = np.full((len(test), len(cats)), -1.0, dtype=np.float32)
    for c in range(len(cats)):
        cols = np.where(ex_cat == c)[0]
        best[:, c] = sims[:, cols].max(1)
    top = np.argsort(-best, axis=1)[:, :5]
    res["kNN по СТЕ"] = (float((top[:, 0] == gold).mean()), float((top == gold[:, None]).any(1).mean()))
    res["_encode_s"] = encode_s
    res["_texts"] = len(cats) + len(ex_texts) + len(test)
    return res


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--data", type=Path, default=Path.home() / "data/tenderhack/ste_2026-03-13.jsonl")
    p.add_argument("--model", default="intfloat/multilingual-e5-small", help="или «none» — без эмбеддингов")
    p.add_argument("--n-test", type=int, default=1000)
    p.add_argument("--min-cat", type=int, default=20)
    p.add_argument("--per-cat", type=int, default=10)
    p.add_argument("--cand-min", type=int, default=0,
                   help="кандидаты-категории только с ≥ N СТЕ (быстрый замер; тестовые категории всегда в их числе)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--skip-categories", action="store_true")
    p.add_argument("--skip-extraction", action="store_true")
    p.add_argument("--out", type=Path, default=ROOT / "reports" / time.strftime("%Y%m%d-%H%M%S"))
    args = p.parse_args()

    t0 = time.time()
    counts, test, train, examples = load(args.data, args.n_test, args.min_cat, args.per_cat, args.seed, args.cand_min)
    print(f"СТЕ {sum(counts.values())}, категорий {len(counts)}; тест {len(test)} СТЕ из "
          f"{len({r['family'] for r in test})} категорий; справочник для анкет {len(train)} СТЕ "
          f"({time.time() - t0:.0f} с)")

    emb = None if args.model == "none" else SentenceTransformerEmbedder(args.model)
    tag = args.model.split("/")[-1]
    out = args.out / tag
    summary: dict = {"model": args.model, "n_test": len(test)}

    if emb is not None and not args.skip_categories:
        cb = category_benchmark(emb, test, examples)
        print(f"\nКатегория по наименованию ({len(examples)} категорий), {cb['_texts']} текстов за {cb['_encode_s']:.0f} с:")
        for k, v in cb.items():
            if not k.startswith("_"):
                print(f"  {k:24} acc@1 {100 * v[0]:5.1f}%   acc@5 {100 * v[1]:5.1f}%")
        summary["categories"] = cb

    if args.skip_extraction:
        out.mkdir(parents=True, exist_ok=True)
        (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return
    forms = forms_from_dataset([to_ste(r) for r in train])
    filler = Autofiller(embedder=emb)
    t1 = time.time()
    rep = evaluate([to_ste(r) for r in test], filler, forms)
    print(f"\nХарактеристики ({time.time() - t1:.0f} с):")
    print_report(rep, title=f"TenderHack СТЕ · {tag}", top=15)
    write_artifacts(rep, out, f"TenderHack_СТЕ_20260313 (тест {len(test)})",
                    {"эмбеддер": args.model, "анкеты": "из справочной части, по категории"})
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nАртефакты: {out}/")


if __name__ == "__main__":
    main()
