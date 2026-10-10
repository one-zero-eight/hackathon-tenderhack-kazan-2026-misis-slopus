"""Шаг 2 эксперимента «LLM + guardrail»: gpt-4o-mini заполняет схему КПГЗ, Jev проверяет, LLM просит уточнить.

    uv run --no-project --with openai --with typesafe-sdk python experiments/llm_guardrail.py \\
        reports/llm_guardrail/prepared.jsonl --env .env           # локально: API с inh-compute недоступны

Вход — prepared.jsonl из llm_guardrail_prepare.py (запрос пользователя, top-5 КПГЗ со схемами, эталон).
Для каждого запроса:
  1. gpt-4o-mini выбирает категорию среди кандидатов (или «нет подходящей»);
  2. gpt-4o-mini заполняет схему выбранной категории только тем, что есть в тексте, остальное — null;
  3. guardrail:
       код — значение перечня из допустимых, число — число, имя характеристики — из схемы;
       Jev (typesafe_sdk) — Noul «категория подходит товару» и по Noul на каждое заполненное значение
       «значение следует из описания и соответствует схеме»; P(да) < --threshold — замечание;
  4. есть замечания или ничего не заполнено — gpt-4o-mini пишет пользователю уточняющий вопрос.
Ответы API кешируются в <output>/cache/*.json по полному запросу: повтор не тратит токены.
Метрики сравнивают с категорией и характеристиками карточки (эталон в модели не передаётся).
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import sys

LLM_MODEL = "gpt-4o-mini"
JEV_MODEL = "jev-latest"
NONE_CODE = "нет подходящей"


def load_env(path: Path) -> None:
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() and value.strip() and name.strip() not in os.environ:
                os.environ[name.strip()] = value.strip().strip("'\"")


class Cache:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def get(self, kind: str, request: dict, fn):
        key = hashlib.sha256(json.dumps([kind, request], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        path = self.root / f"{kind}_{key[:24]}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))["response"]
        response = fn(request)
        path.write_text(json.dumps({"request": request, "response": response}, ensure_ascii=False, indent=1),
                        encoding="utf-8")
        return response


# --- LLM -----------------------------------------------------------------------------------------------
def llm_json(client, system: str, user: dict) -> dict:
    r = client.chat.completions.create(
        model=LLM_MODEL, temperature=0, response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": json.dumps(user, ensure_ascii=False)}])
    return {"content": json.loads(r.choices[0].message.content),
            "usage": {"input_tokens": r.usage.prompt_tokens, "output_tokens": r.usage.completion_tokens}}


CHOOSE = """Ты подбираешь категорию КПГЗ для товара из госзакупок по описанию пользователя.
Описание может быть с опечатками и неполным. Выбери ровно одну категорию из `кандидаты` по назначению
товара, а не по случайным словам. Отличай товар от работы или услуги. Если ни одна не подходит —
верни код «нет подходящей». Ответ — JSON: {"kpgz_code": "<код или нет подходящей>", "причина": "<коротко>"}."""

FILL = """Ты заполняешь характеристики товара по схеме категории КПГЗ, только по тексту `описание`.
Правила: для типа «перечень» — ровно одно значение из `values` дословно; для «число» — число в единице `unit`
(если в тексте другая единица — переведи); для «текст» — короткая строка из описания. Если значения в тексте
нет или оно неоднозначно — null. Ничего не придумывай и не бери «типичные» значения. Опечатки в описании
исправляй мысленно. Ответ — JSON: {"characteristics": {"<имя характеристики дословно>": значение или null}}."""

CLARIFY = """Ты помощник заказчика на портале закупок. По описанию товара заполнили характеристики, но проверка
нашла проблемы (`замечания`). Напиши пользователю короткое сообщение на русском: что удалось понять,
и 1–4 конкретных вопроса — сначала о категории, если она под сомнением, затем о важных характеристиках
без значения или с сомнительным значением.
Варианты категорий предлагай ТОЛЬКО из `кандидаты` (их названия дословно, до 5), никаких своих категорий.
Варианты значений — ТОЛЬКО из `values` в `схема` (до 5); для чисел спроси число в единице `unit`.
Не выдумывай значения. Ответ — JSON: {"message": "<текст>"}."""


# --- проверка кодом -------------------------------------------------------------------------------------
def norm(s) -> str:
    return re.sub(r"\W+", " ", str(s).lower().replace("ё", "е")).strip()


def to_number(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.fullmatch(r"\s*(-?\d+(?:[.,]\d+)?)\s*", str(v))
    return float(m.group(1).replace(",", ".")) if m else None


def structural(schema: list[dict], filled: dict) -> tuple[dict, list[str]]:
    """Значения, прошедшие проверку схемой, и замечания."""
    by_name = {s["name"]: s for s in schema}
    ok, issues = {}, []
    for name, value in filled.items():
        if value in (None, "", []):
            continue
        s = by_name.get(name)
        if s is None:
            issues.append(f"«{name}» нет в схеме категории")
            continue
        if s["type"] == "перечень" and s.get("values"):
            match = next((v for v in s["values"] if norm(v) == norm(value)), None)
            if match is None and not s.get("more_values"):
                issues.append(f"«{name}»: значения «{value}» нет среди допустимых")
                continue
            ok[name] = match if match is not None else value
        elif s["type"] == "число":
            x = to_number(value)
            if x is None:
                issues.append(f"«{name}»: «{value}» — не число")
                continue
            ok[name] = x
        else:
            ok[name] = value
    return ok, issues


# --- Jev guardrail --------------------------------------------------------------------------------------
def guardrail_request(query, category, schema, filled):
    by_name = {s["name"]: s for s in schema}
    questions = {"category": {
        "type": "noul",
        "instructions": "Подходит ли категория `категория` товару, который описан в `описание` (с учётом опечаток)?",
        "criteria": {"true": "Предмет категории совпадает с назначением товара",
                     "false": "Товар относится к другой категории или описание слишком общее, чтобы судить"}}}
    names = list(filled)
    for i, name in enumerate(names):
        s = by_name[name]
        rule = {k: s[k] for k in ("type", "values", "unit", "bounds") if k in s}
        questions[f"v{i}"] = {
            "type": "noul",
            "instructions": {"характеристика": name, "значение": filled[name], "схема": rule,
                             "question": "Следует ли это значение характеристики из `описание` (прямо или однозначно) "
                                         "и соответствует ли оно схеме?"},
            "criteria": {"true": "Значение есть в описании или однозначно из него следует и допустимо по схеме",
                         "false": "Значения в описании нет, оно угадано, противоречит тексту или схеме"}}
    state = {"описание": query, "категория": category, "заполненная схема": filled}
    return {"model": JEV_MODEL, "state": state, "questions": questions}, names


# --- сверка с эталоном -----------------------------------------------------------------------------------
def matches(value, gold_values) -> bool:
    x = to_number(value)
    for g in gold_values:
        y = to_number(g)
        if x is not None and y is not None and abs(x - y) <= 1e-6 * max(1, abs(y)):
            return True
        if norm(value) == norm(g):
            return True
    return False


COND = re.compile(r"(>=|<=|≥|≤|>|<|=)\s*(-?\d+(?:[.,]\d+)?)")
OPS = {">=": float.__ge__, "≥": float.__ge__, "<=": float.__le__, "≤": float.__le__,
       ">": float.__gt__, "<": float.__lt__, "=": float.__eq__}


def matches_schema(value, gold_values) -> bool:
    """Мягкая сверка: строгое совпадение; диапазон перечня («>= 60 < 90») содержит число карточки;
    значение карточки — часть значения перечня («Скоба/Скрепка» ⊇ «скрепка», «Картон мелованный» ⊇ «картон»)."""
    if matches(value, gold_values):
        return True
    conds = COND.findall(str(value))
    if conds and not COND.sub("", str(value)).strip():
        return any((y := to_number(g)) is not None and all(OPS[op](y, float(x.replace(",", "."))) for op, x in conds)
                   for g in gold_values)
    words = set(norm(value).split())
    parts = {norm(p) for p in re.split(r"[/;,]", str(value))}
    return any(norm(g) in parts or (norm(g) and set(norm(g).split()) <= words) for g in gold_values)


def process(row, llm, jev_call, cache, threshold):
    cands = row["candidates"]
    # 1. категория
    choose_in = {"описание": row["query"],
                 "кандидаты": [{"code": c["code"], "name": c["name"], "path": c["path"],
                                "похожие карточки": c.get("examples", []),
                                "характеристики": [s["name"] for s in c["schema"]][:12]} for c in cands]}
    chosen = cache.get("choose", choose_in, lambda r: llm_json(llm, CHOOSE, r))
    code = chosen["content"].get("kpgz_code")
    cand = next((c for c in cands if c["code"] == code), None)
    out = {"id": row["id"], "variant": row["variant"], "query": row["query"], "gold_code": row["gold_code"],
           "gold_in_candidates": row["gold_in_candidates"], "chosen_code": code,
           "category_correct": code == row["gold_code"], "usage": [chosen["usage"]]}
    issues, filled, ok = [], {}, {}
    if cand is None:
        issues.append("подходящая категория среди найденных не определена")
        schema = []
    else:
        schema = cand["schema"]
        fill_in = {"описание": row["query"], "категория": f"{cand['name']} ({cand['path']})", "схема": schema}
        fill = cache.get("fill", fill_in, lambda r: llm_json(llm, FILL, r))
        out["usage"].append(fill["usage"])
        filled = fill["content"].get("characteristics") or {}
        ok, struct_issues = structural(schema, filled)
        issues += struct_issues
        # 3. Jev
        request, names = guardrail_request(row["query"], f"{cand['name']} ({cand['path']})", schema, ok)
        response = cache.get("jev", request, jev_call)
        answers = response["answers"]
        out["jev_category"] = answers["category"]["noul"]
        out["jev_values"] = {n: answers[f"v{i}"]["noul"] for i, n in enumerate(names)}
        if out["jev_category"] < threshold:
            issues.append(f"категория «{cand['name']}» под сомнением (Jev {out['jev_category']:.2f})")
        for n, p in out["jev_values"].items():
            if p < threshold:
                issues.append(f"«{n}» = «{ok[n]}» не подтверждается описанием (Jev {p:.2f})")
        if not ok:
            issues.append("ни одной характеристики не удалось заполнить по описанию")
    out |= {"filled": ok, "issues": issues}
    # 4. уточнение
    if issues:
        clar_in = {"описание": row["query"], "категория": cand and f"{cand['name']} ({cand['path']})",
                   "кандидаты": [c["name"] for c in cands],
                   "схема": schema[:25], "заполнено": {k: v for k, v in ok.items()
                                                        if out.get("jev_values", {}).get(k, 1) >= threshold},
                   "замечания": issues}
        clar = cache.get("clarify", clar_in, lambda r: llm_json(llm, CLARIFY, r))
        out["usage"].append(clar["usage"])
        out["clarification"] = clar["content"].get("message")
    # сверка значений — только при верной категории
    if out["category_correct"]:
        gold = row["gold_values"]
        out["values"] = [{"name": n, "value": v, "gold": gold.get(n), "correct": n in gold and matches(v, gold[n]),
                          "correct_schema": n in gold and matches_schema(v, gold[n]),
                          "jev": out["jev_values"].get(n)} for n, v in ok.items()]
    return out


def summary(results, threshold):
    n = len(results)
    s = {"n": n, "gold_in_candidates": sum(r["gold_in_candidates"] for r in results),
         "category_correct": sum(r["category_correct"] for r in results),
         "clarification_asked": sum(bool(r.get("clarification")) for r in results)}
    vals = [v for r in results for v in r.get("values", [])]
    with_gold = [v for v in vals if v["gold"] is not None]
    s |= {"values_filled_on_correct_category": len(vals), "values_with_gold": len(with_gold),
          "values_correct": sum(v["correct"] for v in with_gold),
          "values_correct_schema": sum(v["correct_schema"] for v in with_gold),
          "values_without_gold": len(vals) - len(with_gold)}
    wrong = [v for v in with_gold if not v["correct_schema"]]  # ошибки — по мягкой сверке
    right = [v for v in with_gold if v["correct_schema"]]
    s |= {"jev_flags_wrong_values": f"{sum(v['jev'] < threshold for v in wrong)}/{len(wrong)}",
          "jev_flags_correct_values": f"{sum(v['jev'] < threshold for v in right)}/{len(right)}",
          "jev_flags_values_without_gold": f"{sum(v['jev'] < threshold for v in vals if v['gold'] is None)}/"
                                           f"{len(vals) - len(with_gold)}"}
    cat = [r for r in results if "jev_category" in r]
    s |= {"jev_flags_wrong_category": f"{sum(r['jev_category'] < threshold for r in cat if not r['category_correct'])}/"
                                      f"{sum(not r['category_correct'] for r in cat)}",
          "jev_flags_correct_category": f"{sum(r['jev_category'] < threshold for r in cat if r['category_correct'])}/"
                                        f"{sum(r['category_correct'] for r in cat)}"}
    by_variant = {}
    for v in sorted({r["variant"] for r in results}):
        part = [r for r in results if r["variant"] == v]
        by_variant[v] = {"n": len(part), "category_correct": sum(r["category_correct"] for r in part),
                         "clarification": sum(bool(r.get("clarification")) for r in part)}
    s["by_variant"] = by_variant
    tokens = [u for r in results for u in r["usage"]]
    s["llm_tokens"] = {k: sum(u[k] for u in tokens) for k in ("input_tokens", "output_tokens")}
    return s


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("prepared", type=Path)
    ap.add_argument("--env", type=Path, default=Path(".env"))
    ap.add_argument("--threshold", type=float, default=.5)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()
    load_env(args.env)
    from openai import OpenAI
    from typesafe_sdk import TypeSafeClient

    llm, jev = OpenAI(), TypeSafeClient()

    def jev_call(request):
        r = jev.system_one(state=request["state"], questions=request["questions"], model=request["model"])
        return {"answers": {k: a.model_dump(mode="json") for k, a in r.answers.items()}, "usage": r.usage.model_dump()}

    output = args.output or args.prepared.parent
    cache = Cache(output / "cache")
    rows = [json.loads(line) for line in args.prepared.read_text(encoding="utf-8").splitlines() if line.strip()]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda r: process(r, llm, jev_call, cache, args.threshold), rows))
    s = summary(results, args.threshold)
    (output / "results.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in results) + "\n",
                                          encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(s, ensure_ascii=False, indent=2))
    for r in [r for r in results if r.get("clarification")][:3]:
        print(f"\n[{r['variant']}] {r['query']}\n  замечания: {r['issues']}\n  → {r['clarification']}")


if __name__ == "__main__":
    sys.exit(main())
