"""Обучающий набор для дообучения Laya на СТЕ организаторов (формат laya-train JSONL).

    uv run python experiments/laya_build_dataset.py --n-train 8000 --out data/laya

Тест/справочник — как в tenderhack_eval.py (crc32(id) % 10 == 0 — тест). Обучающие СТЕ берутся
только из справочной части; анкеты и частоты значений — тоже по ней.

Вопрос на характеристику — тот же, что в experiments/laya_eval.py: «Какое значение
характеристики «X» указано в описании товара?», варианты — значения анкеты категории и
«не указано».
  - положительные: характеристика СТЕ заполнена, эталон есть в анкете → ответ эталон
    (до --pos на СТЕ; варианты — эталон + до 7 частых значений категории);
  - отрицательные: частая в категории характеристика (≥ 20 % СТЕ), которую у этой СТЕ
    не заполнили → ответ «не указано» (до --neg на СТЕ). Это учит воздерживаться.
Состояние (state) — «Категория: … Товар: …»: категорию система знает на шаге 2, а модели она
подсказывает, какие значения вообще возможны. laya_eval.py задаёт вопросы в том же виде.

Выход — схема ноутбука Laya (state, questions, gold: label + probabilities) и одновременно
laya-train (expected): train.jsonl и eval.jsonl (тест, те же 200 СТЕ, что берёт laya_eval.py).
Цели жёсткие со сглаживанием: 0.95 правильному варианту, 0.05 поровну остальным.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments"))

from tenderhack_eval import is_test, load, rows  # noqa: E402

NONE = "не указано"
SMOOTH = 0.05


def state_of(r: dict) -> str:
    return f"Категория: {r['family']}. Товар: {r['text']}"


def gold_of(q: dict, answer: str) -> dict:
    opts = list(q["criteria"])
    rest = SMOOTH / (len(opts) - 1)
    return {"label": answer, "probabilities": {o: (1 - SMOOTH if o == answer else rest) for o in opts}}
INSTR = "Какое значение характеристики «{}» указано в описании товара?"


def question(char: str, options: list[str]) -> dict:
    crit = {o: None for o in options}
    crit[NONE] = "в описании этого нет"
    return {"type": "choice", "instructions": INSTR.format(char), "criteria": crit}


def build_case(r: dict, freq, char_share, n_cat, rnd, n_pos: int, n_neg: int, distractors: int):
    cat, gold = r["family"], r["characteristics"]
    pos, neg = [], []
    for name, value in gold.items():
        values = freq.get((cat, name))
        if values and values[value] >= 1 and len(values) >= 2:
            pos.append(name)
    for name, share in char_share[cat].items():
        if name not in gold and share >= 0.2 and len(freq[(cat, name)]) >= 2:
            neg.append(name)
    rnd.shuffle(pos)
    rnd.shuffle(neg)
    qs, expected = {}, {}
    for name, answer in [(n, gold[n]) for n in pos[:n_pos]] + [(n, NONE) for n in neg[:n_neg]]:
        top = [v for v, _ in freq[(cat, name)].most_common(30)]
        if answer == NONE:
            opts = top[: distractors + 1]
        else:
            opts = [answer, *[v for v in top if v != answer][:distractors]]
        rnd.shuffle(opts)
        key = f"q{len(qs)}"
        qs[key] = question(name, opts)
        expected[key] = answer
    if not qs:
        return None
    gold = {k: gold_of(qs[k], a) for k, a in expected.items()}
    return {"id": str(r["id"]), "state": state_of(r), "questions": qs, "gold": gold, "expected": expected}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--data", type=Path, default=Path.home() / "data/tenderhack/ste_2026-03-13.jsonl")
    p.add_argument("--n-train", type=int, default=8000, help="обучающих СТЕ")
    p.add_argument("--n-test", type=int, default=200, help="тестовых СТЕ (как в laya_eval.py)")
    p.add_argument("--pos", type=int, default=4)
    p.add_argument("--neg", type=int, default=2)
    p.add_argument("--distractors", type=int, default=7)
    p.add_argument("--out", type=Path, default=ROOT / "data" / "laya")
    args = p.parse_args()

    counts = Counter(r["family"] for r in rows(args.data))
    eligible = {c for c, k in counts.items() if k >= 20}
    freq: dict[tuple[str, str], Counter] = defaultdict(Counter)
    has_char: dict[str, Counter] = defaultdict(Counter)
    n_cat: Counter = Counter()
    pool = []
    for r in rows(args.data):
        if is_test(r["id"]) or r["family"] not in eligible:
            continue
        n_cat[r["family"]] += 1
        for k, v in r["characteristics"].items():
            freq[(r["family"], k)][v] += 1
            has_char[r["family"]][k] += 1
        if r["characteristics"]:
            pool.append(r)
    char_share = {c: {k: n / n_cat[c] for k, n in ks.items()} for c, ks in has_char.items()}

    rnd = random.Random(0)
    train = rnd.sample(pool, min(args.n_train, len(pool)))
    _, test, _, _ = load(args.data, args.n_test, 20, 0, 0)

    args.out.mkdir(parents=True, exist_ok=True)
    stats = Counter()
    for name, items in (("train", train), ("eval", test)):
        with (args.out / f"{name}.jsonl").open("w", encoding="utf-8") as f:
            for r in items:
                case = build_case(r, freq, char_share, n_cat, rnd, args.pos, args.neg, args.distractors)
                if case:
                    f.write(json.dumps(case, ensure_ascii=False) + "\n")
                    stats[f"{name}: СТЕ"] += 1
                    stats[f"{name}: вопросов"] += len(case["questions"])
                    stats[f"{name}: «не указано»"] += sum(v == NONE for v in case["expected"].values())
    print(dict(stats), "→", args.out)


if __name__ == "__main__":
    main()
