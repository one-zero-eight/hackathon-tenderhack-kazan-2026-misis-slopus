"""Шаг 1 эксперимента «LLM + guardrail»: запросы «как пишет пользователь» и кандидаты КПГЗ по эмбеддингу.

    .venv/bin/python experiments/llm_guardrail_prepare.py --n 60          # на inh-compute (GPU)

Запросы строятся кодом из карточек выборки category_hypotheses_eval (--sample 300 --seed 1), без LLM:
  typos       — название с опечатками (перестановка, пропуск, удвоение букв), нижний регистр;
  typos_desc  — то же плюс начало описания (до 120 символов) с опечатками;
  short       — первые три слова названия.
Берутся карточки, у которых ≥ 3 характеристики схемы КПГЗ заполнены. Кандидаты — 5 категорий, у каждой
путь, схема (тип, допустимые значения, единица) и до 3 названий похожих карточек:
  --retrieval neighbors (по умолчанию) — взвешенный голос 25 ближайших карточек-соседей СТЕ; корпус —
      как в category_hypotheses_eval --family-holdout (карточки выборки, их тексты, названия и товарные
      семьи в корпус не входят), без кластеризации;
  --retrieval kpgz — ближайшие схемы КПГЗ по названию (KpgzSearch), как в первом прогоне.
Эталон — категория и значения характеристик карточки (только для метрик). Шаг 2 — experiments/llm_guardrail.py
(локально: API OpenAI и TypeSafe с inh-compute недоступны).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import random

import category_hypotheses_eval as hyp
import ste_to_jev as ste
from ktru_autofill.kpgz_search import CACHE, KpgzSearch, SteNeighbors, clean, collect_corpus, iter_cards

MAX_ENUM = 40
VARIANTS = ["typos", "typos_desc", "short"]


def typos(text: str, rng: random.Random, rate: float = .35) -> str:
    words = []
    for w in text.lower().split():
        if len(w) >= 5 and rng.random() < rate:
            i = rng.randrange(1, len(w) - 1)
            op = rng.choice(["swap", "drop", "double"])
            w = (w[:i] + w[i + 1] + w[i] + w[i + 2:] if op == "swap" else
                 w[:i] + w[i + 1:] if op == "drop" else w[:i] + w[i] + w[i:])
        words.append(w)
    return " ".join(words)


def schema(node, okei):
    out = []
    for ch in node.get("kpgzCharacteristics") or []:
        values = [e["value"] for e in ch.get("characteristicKpgzEnums") or []]
        kind = {ste.ENUM: "перечень", ste.NUMBER: "число", ste.TEXT: "текст"}.get(ch.get("conditionTypeId"), "текст")
        item = {"name": ch["characteristicName"].strip(), "type": kind}
        if values:
            item["values"] = values[:MAX_ENUM]
            if len(values) > MAX_ENUM:
                item["more_values"] = len(values) - MAX_ENUM
        if unit := ste.unit_of(ch, okei):
            item["unit"] = unit
        if b := ste.bounds(ch):
            item["bounds"] = b
        out.append(item)
    return out


def neighbor_candidates(search, ste_path, sample, texts, seed, k=25, top=5):
    """5 категорий взвешенного голоса k ближайших карточек-соседей и до 3 их названий на категорию.
    Корпус — как в category_hypotheses_eval (--family-holdout): до 20 карточек на категорию по хешу ID,
    без карточек выборки, их текстов, названий и товарных семей; векторы — из его кеша."""
    tag = f"category_sample_n300_seed{seed}_family"
    corpus, _ = collect_corpus(iter_cards(ste_path), set(search.by_code), sample, 20, 20000, True)
    neighbors = SteNeighbors(search, corpus, cache=CACHE.parent / f"{tag}_ste.npz", k=k)
    out = []
    for ranked in neighbors.rank(search.encode_queries(texts), top):
        out.append([c | {"examples": [n["name"] for n in c["neighbors"]][:3]} for c in ranked])
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ste", type=Path, default=ste.DEFAULT_STE)
    ap.add_argument("--kpgz", type=Path, default=ste.DEFAULT_KPGZ)
    ap.add_argument("--okei", type=Path, default=ste.DEFAULT_OKEI)
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--retrieval", choices=["neighbors", "kpgz"], default="neighbors")
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()
    okei = ste.load_okei(args.okei)
    search = KpgzSearch(ste.iter_array(args.kpgz), device="cuda", cache=CACHE, batch_size=16)
    baseline = json.loads((ste.ROOT / "reports/ste_jev_search_dry/eval_all_n20_seed0.json").read_text(encoding="utf-8"))
    sample, _ = hyp.sample_queries(args.ste, set(search.by_code), 300, args.seed,
                                   {x["ste"] for x in baseline["category_search"]})
    wanted = {q["id"] for q in sample}
    cards = {}
    for c in iter_cards(args.ste):
        if c.get("id") in wanted:
            cards[c["id"]] = c
    rows = []
    for q in sample:
        card = cards[q["id"]]
        node = search.by_code[q["code"]]
        names = {ch["characteristicName"].strip() for ch in node["kpgzCharacteristics"]}
        gt = ste.card_values(card)
        gold = {n: t["values"] for n in names if (t := ste.lookup(gt, n))}
        if len(gold) < 3:
            continue
        variant = VARIANTS[len(rows) % len(VARIANTS)]
        rng = random.Random(int(hashlib.sha256(f"{args.seed}:{q['id']}".encode()).hexdigest(), 16))
        name = card.get("name") or card.get("modelName")
        text = (typos(name, rng) if variant == "typos" else
                typos(f"{name}. {clean(card.get('description'))[:120]}", rng) if variant == "typos_desc" else
                " ".join(name.split()[:3]))
        rows.append({"id": q["id"], "variant": variant, "query": text, "gold_code": q["code"],
                     "gold_values": gold, "card_name": name})
        if len(rows) == args.n:
            break
    if args.retrieval == "kpgz":
        found = search.search_many([r["query"] for r in rows], 5)
    else:
        found = neighbor_candidates(search, args.ste, sample, [r["query"] for r in rows], args.seed)
    for r, cands in zip(rows, found):
        r["candidates"] = [{"code": c["code"], "name": c["name"], "path": search.paths[c["code"]],
                            "score": round(c["score"], 4), "examples": c.get("examples", []),
                            "schema": schema(search.by_code[c["code"]], okei)} for c in cands]
        r["gold_in_candidates"] = any(c["code"] == r["gold_code"] for c in cands)
        r["retrieval"] = args.retrieval
    args.output = args.output or ste.ROOT / f"reports/llm_guardrail_{args.retrieval}/prepared.jsonl"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"{len(rows)} запросов, верная категория среди 5 кандидатов: {sum(r['gold_in_candidates'] for r in rows)}")
    for r in rows[:6]:
        print(f"  [{r['variant']}] {r['query'][:80]}  ← {r['card_name'][:50]}")


if __name__ == "__main__":
    main()
