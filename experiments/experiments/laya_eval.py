"""Laya-multilingual на СТЕ организаторов: выбор значения характеристики и проверка подсказок.

    USE_TF=0 uv run python experiments/laya_eval.py --n-test 200

Те же тест/справочник, что в tenderhack_eval.py (10 % СТЕ по crc32 id — тест, анкеты — по остальным).

1. Выбор значения. Для каждой характеристики СТЕ со значением из анкеты: варианты — эталон,
   до 7 других частых значений этой характеристики в категории и «не указано».
   - «есть в тексте» — эталон упомянут в наименовании: нужен эталон;
   - «нет в тексте»  — не упомянут: правильный ответ «не указано», значение = выдумка.
   Сравнение с ответом нашей системы (e5-small) на тех же парах.
2. Подсказки системы (не заполнены, ждут подтверждения): Laya выбирает между подсказкой,
   другими значениями и «не указано»; принятие — если выбрала подсказку с p ≥ порога.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "experiments"))

from tenderhack_eval import load, to_ste  # noqa: E402

from ktru_autofill import Autofiller  # noqa: E402
from ktru_autofill.embedder import SentenceTransformerEmbedder  # noqa: E402
from ktru_autofill.evaluate import forms_from_dataset, is_correct, mentioned  # noqa: E402

NONE = "не указано"
INSTR = "Какое значение характеристики «{}» указано в описании товара?"


def question(char: str, options: list[str]) -> dict:
    crit = {o: None for o in options}
    crit[NONE] = "в описании этого нет"
    return {"type": "choice", "instructions": INSTR.format(char), "criteria": crit}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--data", type=Path, default=Path.home() / "data/tenderhack/ste_2026-03-13.jsonl")
    p.add_argument("--n-test", type=int, default=200)
    p.add_argument("--distractors", type=int, default=7)
    p.add_argument("--accept", type=float, default=0.5, help="порог принятия подсказки")
    p.add_argument("--out", type=Path, default=ROOT / "reports" / time.strftime("%Y%m%d-%H%M%S") / "laya")
    args = p.parse_args()

    import laya

    _, test, train, _ = load(args.data, args.n_test, 20, 0, 0)
    forms = forms_from_dataset([to_ste(r) for r in train])
    # частоты значений в категории — для отвлекающих вариантов
    freq: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for r in train:
        for k, v in r["characteristics"].items():
            freq[(r["family"], k)][v] += 1

    agent = laya.load("convaiinnovations/laya", subfolder="multilingual")
    filler = Autofiller(embedder=SentenceTransformerEmbedder())
    rnd = random.Random(0)

    stats = defaultdict(Counter)  # группа → счётчики
    rows = []
    t_laya = 0.0
    for r in test:
        ste = to_ste(r)
        form = forms.get(ste.code)
        if form is None:
            continue
        res = filler.fill(ste.text, [form])
        qs, meta = {}, {}
        for ch in form.characteristics:
            gold = ste.gold.get(ch.name)
            if ch.type != "enum" or gold is None or gold not in ch.values:
                continue
            others = [v for v, _ in freq[(ste.code, ch.name)].most_common(30) if v != gold][: args.distractors]
            opts = [gold, *others]
            rnd.shuffle(opts)
            key = f"q{len(qs)}"
            qs[key] = question(ch.name, opts)
            meta[key] = ("value", ch.name, gold, mentioned(ste.text, gold))
        for sg in res.suggestions:
            if not isinstance(sg.значение, str):
                continue
            ch = form.char(sg.характеристика)
            if ch is None or ch.type != "enum":
                continue
            others = [v for v, _ in freq[(ste.code, ch.name)].most_common(30) if v != sg.значение][: args.distractors]
            opts = [sg.значение, *others]
            rnd.shuffle(opts)
            key = f"s{len(qs)}"
            qs[key] = question(ch.name, opts)
            meta[key] = ("suggestion", ch.name, ste.gold.get(ch.name), sg.значение)
        if not qs:
            continue
        t = time.perf_counter()
        answers = agent.predict(ste.text, qs)["answers"]
        t_laya += time.perf_counter() - t

        for key, (kind, char, gold, extra) in meta.items():
            a = answers[key]
            choice, prob = a["choice"], a["probabilities"][a["choice"]]
            if kind == "value":
                group = "есть в тексте" if extra else "нет в тексте"
                ours = res.values.get(char)
                ours_s = None if ours is None else str(ours)
                s = stats[group]
                s["пар"] += 1
                s["laya: эталон"] += choice == gold
                s["laya: «не указано»"] += choice == NONE
                s["laya: другое значение"] += choice not in (gold, NONE)
                s["система: эталон"] += ours is not None and is_correct(ours, gold)
                s["система: пусто"] += ours is None
                s["система: другое значение"] += ours is not None and not is_correct(ours, gold)
                rows.append({"text": ste.text, "характеристика": char, "эталон": gold, "в тексте": extra,
                             "laya": choice, "p": round(prob, 3), "система": ours_s})
            else:
                s = stats["подсказки"]
                ok = gold is not None and is_correct(extra, gold)
                accepted = choice == extra and prob >= args.accept
                s["подсказок"] += 1
                s["верных подсказок"] += ok
                s["Laya приняла"] += accepted
                s["Laya приняла верных"] += accepted and ok
                rows.append({"text": ste.text, "характеристика": char, "эталон": gold, "подсказка": extra,
                             "laya": choice, "p": round(prob, 3), "принята": accepted, "верна": ok})

    print(f"Laya: {sum(1 for _ in rows)} вопросов, {t_laya:.0f} с на модель")
    for group in ("есть в тексте", "нет в тексте"):
        s = stats[group]
        n = s["пар"] or 1
        print(f"\n[{group}] пар {s['пар']}")
        for who in ("laya", "система"):
            parts = [f"{k.split(': ')[1]} {100 * v / n:5.1f}%" for k, v in s.items() if k.startswith(who)]
            print(f"  {who:8} " + "   ".join(parts))
    s = stats["подсказки"]
    if s["подсказок"]:
        acc = s["Laya приняла"]
        print(f"\n[подсказки] всего {s['подсказок']}, верных {s['верных подсказок']} "
              f"({100 * s['верных подсказок'] / s['подсказок']:.1f}%); Laya приняла {acc}, из них верных "
              f"{s['Laya приняла верных']} ({100 * s['Laya приняла верных'] / max(acc, 1):.1f}%)")

    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "pairs.jsonl").open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    (args.out / "summary.json").write_text(json.dumps({k: dict(v) for k, v in stats.items()}, ensure_ascii=False,
                                                      indent=2), encoding="utf-8")
    print(f"\nАртефакты: {args.out}/")


if __name__ == "__main__":
    main()
