"""Датасеты из выгрузки API (zakupki-reverse): карточки СТЕ, КТРУ, поисковые запросы.

    uv run python experiments/build_api_datasets.py            # все шаги
    uv run python experiments/build_api_datasets.py --only ste  # ste | forms | search | ktru | laya

Источник: ~/tenderhack/zakupki-reverse/data (снимки 6–8.10.2026: zakupki.mos.ru, ЕАИСТ, зеркало КТРУ).

1. ste    — ~/tenderhack/derived/ste_api.jsonl: карточки GetSku в едином виде — наименование,
            описание, категория (id, имя), КПГЗ, ОКПД2, характеристики {имя: значение[ единица]} и их
            типы. Значения — из полей товара (characteristicValue*), «Признак» → Да/Нет.
2. forms  — ~/tenderhack/derived/forms_ste_api.json: анкеты категорий по карточкам (code = имя
            категории) — значения, диапазоны и единицы; свободный текст отброшен.
3. search — data/eval/search_queries.jsonl: 1 000 запросов поискового датасета в формате ktru-eval:
            text = запрос, code/family = категория, characteristics = critical_constraints (что задано
            в самом запросе, с единицами), плюс тип шума, сплит и id релевантных СТЕ.
4. ktru   — ~/tenderhack/derived/ktru_api.json: КТРУ (81 тыс.) в формате каталога: характеристики
            (выбор / диапазон, единицы), синонимы товара (similar_products), код ОКПД2.
5. laya   — data/laya_v3/{train,eval}.jsonl: ТОЛЬКО карточки zakupki.mos.ru (ste-details, 134 тыс.).
            state — «Товар … Категория … Описание …» (описание всегда, «нет», если пусто).
            Разбиение 70/20/10 по crc32(id) % 10: 3–9 — train (шарды), 1–2 — val.jsonl, 0 — test.jsonl;
            частоты значений и отвлекающие варианты — только по train.
            Варианты-дубли склеены, ответы-числа и варианты > 150 символов выброшены (clean_case).
            По умолчанию — все карточки; train режется на train_NN.jsonl по ~100 тыс. вопросов
            (одна сессия Kaggle на шард, следующая продолжает с чекпойнта предыдущей).
Устаревшие, по запросу (--only): search, laya-mixed, merge — используют поисковый датасет, размеченный
LLM (DeepSeek), и выгрузку организаторов; в набор по умолчанию не входят. Только текстовые характеристики и «Да/Нет»: числа разбирают
            правила. Из обучения исключены карточки из поискового датасета и тест организаторов
            (crc32(id) % 10 == 0).
"""

from __future__ import annotations

import argparse
import ast
import gzip
import json
import math
import random
import re
import sys
import zlib
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ktru_autofill.ktru_import import canonical_unit  # noqa: E402

SRC = Path.home() / "tenderhack" / "zakupki-reverse" / "data"
DERIVED = Path.home() / "tenderhack" / "derived"
NONE = "не указано"
INSTR = "Какое значение характеристики «{}» указано в описании товара?"
_ID_CHAIN = re.compile(r"^\d+(\.\d+){2,}$")
_NUM = re.compile(r"^-?\d+(?:[.,]\d+)?$")


def jl(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def is_org_test(ste_id) -> bool:
    return zlib.crc32(str(ste_id).encode()) % 10 == 0


def split_of(ste_id) -> str:
    """70/20/10 по crc32(id) % 10: 0 — test (те же, что прежняя проверка), 1–2 — val, 3–9 — train."""
    k = zlib.crc32(str(ste_id).encode()) % 10
    return "test" if k == 0 else "val" if k <= 2 else "train"


def fmt_num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


# --- 1. карточки СТЕ ------------------------------------------------------------


def ste_value(c: dict) -> tuple[str, str | None, str] | None:
    """(значение, единица, тип) характеристики товара или None.

    Значение лежит в полях самого элемента skuCharacteristics (characteristicValue*), а в
    productCharacteristicValue — общее определение характеристики (имя, тип, единица, умолчания):
    его stringValue у «Справочника» — цепочка id, boolValue — умолчание, а не значение товара.
    """
    v = c.get("productCharacteristicValue") or {}
    kind = v.get("productCharacteristicDescriptionTypeName") or ""
    unit = canonical_unit(v.get("productCharacteristicUnitName") or v.get("productCharacteristicUnitDesignationNational") or "")
    if c.get("characteristicValueBoolValue") is not None:
        return ("Да" if c["characteristicValueBoolValue"] else "Нет"), None, "bool"
    for k in ("characteristicValueDecimalValue", "characteristicValueIntValue"):
        if c.get(k) is not None:
            return fmt_num(float(c[k])), unit, "number"
    raw = c.get("characteristicValueStringValue")
    if raw is None or not str(raw).strip():
        return None
    raw = " ".join(str(raw).split())
    if kind.endswith("числовое значение") and _NUM.match(raw.replace(" ", "")):
        return fmt_num(float(raw.replace(" ", "").replace(",", "."))), unit, "number"
    return raw, None, "text"


def build_ste(out: Path) -> Counter:
    stats = Counter()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for d in jl(SRC / "ste-details.jsonl.gz"):
            stats["карточек"] += 1
            if d.get("_fetchError") or not d.get("name"):
                stats["пропущено"] += 1
                continue
            chars, types, units = {}, {}, {}
            for c in d.get("skuCharacteristics") or []:
                v = c.get("productCharacteristicValue") or {}
                name = (v.get("productCharacteristicName") or "").strip()
                got = ste_value(c)
                if not name or got is None:
                    continue
                value, unit, kind = got
                chars[name] = f"{value} {unit}" if unit else value
                types[name] = kind
                if unit:
                    units[name] = unit
            prod = d.get("production") or {}
            desc = re.sub(r"<[^>]+>", " ", d.get("description") or "")
            rec = {
                "id": d["id"], "text": d["name"].strip(), "description": " ".join(desc.split())[:2000],
                "family": d.get("productionDirectoryName"), "category_id": d.get("productionDirectoryId"),
                "kpgz": prod.get("code"), "okpd2": (prod.get("okpd") or {}).get("code"),
                "manufacturer": d.get("manufacturerName"), "model": d.get("modelName"),
                "characteristics": chars, "types": types, "units": units,
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            stats["записано"] += 1
            stats["с описанием"] += bool(rec["description"])
            stats["характеристик"] += len(chars)
            stats["числовых с единицей"] += len(units)
    return stats


# --- 2. анкеты категорий --------------------------------------------------------


def build_forms(ste_path: Path, out: Path, free_text_ratio: float = 0.5, min_ste: int = 3) -> Counter:
    vals: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    kinds: dict[tuple[str, str], Counter] = defaultdict(Counter)
    units: dict[tuple[str, str], Counter] = defaultdict(Counter)
    n_cat = Counter()
    for r in jl(ste_path):
        cat = r["family"]
        n_cat[cat] += 1
        for name, value in r["characteristics"].items():
            vals[cat][name].append(value)
            kinds[(cat, name)][r["types"].get(name, "text")] += 1
            if name in r["units"]:
                units[(cat, name)][r["units"][name]] += 1
    forms, stats = [], Counter()
    for cat, chars in vals.items():
        if n_cat[cat] < min_ste:
            continue
        out_chars = []
        for name, values in chars.items():
            kind = kinds[(cat, name)].most_common(1)[0][0]
            if kind == "number":
                nums = [float(v.split()[0].replace(",", ".")) for v in values if _NUM.match(v.split()[0])]
                unit = units[(cat, name)].most_common(1)[0][0] if units[(cat, name)] else None
                if nums:
                    out_chars.append({"name": name, "type": "number" if unit else "count", "unit": unit,
                                      "range": [min(nums), max(nums)], "frequency": len(values)})
                    stats["числовых"] += 1
                continue
            distinct = list(dict.fromkeys(values))
            if len(values) >= 10 and len(distinct) > free_text_ratio * len(values):
                stats["свободный текст, пропущено"] += 1
                continue
            out_chars.append({"name": name, "type": "enum", "values": distinct, "frequency": len(values)})
            stats["списков"] += 1
        forms.append({"code": cat, "name": cat, "characteristics": out_chars})
        stats["категорий"] += 1
    out.write_text(json.dumps(forms, ensure_ascii=False), encoding="utf-8")
    return stats


# --- 3. поисковые запросы -------------------------------------------------------


def _cv(c: dict) -> str:
    v = c.get("value")
    v = fmt_num(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v).strip()
    return f"{v} {c['unit']}" if c.get("unit") else v


def _norm(s: str) -> str:
    from ktru_autofill.text import lemmatize, normalize_code
    return lemmatize(s) or normalize_code(s)


def _num_of(v) -> float | None:
    m = re.match(r"^\s*(-?\d+(?:[.,]\d+)?)", str(v))
    return float(m.group(1).replace(",", ".")) if m else None


def map_constraint(c: dict, ste_chars: list[dict]) -> dict | None:
    """Атрибут запроса («Длина», придуман разметчиком) → характеристика релевантной СТЕ
    («Длина намотки»): по названию, иначе по значению (леммы / число), если совпадение одно."""
    by_name = {x["name"].lower(): x for x in ste_chars}
    hit = by_name.get(str(c.get("attribute", "")).lower())
    if hit is not None:
        return hit
    num = _num_of(c.get("value"))
    cand = []
    for x in ste_chars:
        xn = _num_of(x.get("value"))
        if num is not None and xn is not None:
            if abs(xn - num) < 1e-9:
                cand.append(x)
        elif _norm(str(x.get("value", ""))) == _norm(str(c.get("value", ""))):
            cand.append(x)
    return cand[0] if len(cand) == 1 else None


def build_search(out: Path, forms_out: Path) -> Counter:
    """Запросы в формате ktru-eval. Анкета — по кандидатам из выдачи портала для этого запроса
    (настоящие названия характеристик, значения, единицы); code = query_id."""
    out.parent.mkdir(parents=True, exist_ok=True)
    stats, forms = Counter(), []
    with out.open("w", encoding="utf-8") as f:
        for split in ("train", "validation", "test"):
            for q in jl(SRC / "ste-search-dataset" / f"{split}.jsonl.gz"):
                rel = q.get("relevant_ste") or {}
                rel_chars = rel.get("characteristics") or []
                gold, unmapped = {}, {}
                for c in q.get("critical_constraints") or []:
                    if not c.get("attribute") or c.get("value") in (None, ""):
                        continue
                    hit = map_constraint(c, rel_chars)
                    if hit is not None:
                        gold[hit["name"]] = _cv(hit)
                    else:
                        unmapped[c["attribute"]] = _cv(c)
                stats["значений привязано"] += len(gold)
                stats["не привязано"] += len(unmapped)
                values: dict[str, list] = defaultdict(list)
                units: dict[str, Counter] = defaultdict(Counter)
                seen = set()
                for ste in [rel, *(q.get("hard_negatives") or []), *(q.get("candidates") or [])]:
                    if not ste or ste.get("ste_id") in seen:
                        continue
                    seen.add(ste.get("ste_id"))
                    for x in ste.get("characteristics") or []:
                        if x.get("value") in (None, ""):
                            continue
                        values[x["name"]].append(x["value"])
                        if x.get("unit"):
                            units[x["name"]][x["unit"]] += 1
                chars = []
                for name, vs in values.items():
                    nums = [v for v in vs if isinstance(v, (int, float)) and not isinstance(v, bool)]
                    if nums and len(nums) == len(vs):
                        raw_unit = units[name].most_common(1)[0][0] if units[name] else None
                        unit = canonical_unit(raw_unit) if raw_unit else None
                        aliases = list(dict.fromkeys(u for u in units[name] if u != unit))
                        chars.append({"name": name, "type": "number" if unit else "count", "unit": unit,
                                      "unit_aliases": aliases, "range": [min(nums), max(nums)],
                                      "frequency": len(vs)})
                    else:
                        chars.append({"name": name, "type": "enum", "frequency": len(vs),
                                      "values": list(dict.fromkeys(str(v).strip() for v in vs))})
                forms.append({"code": q["query_id"], "name": q.get("category"), "characteristics": chars})
                rec = {"id": q["query_id"], "text": q["query"], "code": q["query_id"], "family": q.get("category"),
                       "characteristics": gold, "unmapped": unmapped, "query_type": q.get("query_type"),
                       "split": split, "relevant_ste_ids": q.get("all_relevant_ste_ids") or [rel.get("ste_id")],
                       "hard_negative_ids": [h.get("ste_id") for h in q.get("hard_negatives") or []]}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                stats[f"запросов {split}"] += 1
    forms_out.write_text(json.dumps(forms, ensure_ascii=False), encoding="utf-8")
    return stats


# --- 4. КТРУ ---------------------------------------------------------------------


def _parse(s):
    if isinstance(s, (list, dict)) or s is None:
        return s or []
    if s in ("None", ""):
        return []
    try:
        return json.loads(s)
    except Exception:
        return ast.literal_eval(s)


def ktru_char(ch: dict) -> dict | None:
    unit_info = ch.get("unit") or {}
    unit = canonical_unit(unit_info.get("fullname") or unit_info.get("shortname") or "")
    if unit in ("---", "-"):
        unit = None
    choices, lo, hi = [], math.inf, -math.inf
    for v in ch.get("values") or []:
        if v.get("type") == "range":
            for op, x in ((v.get("range"), v.get("value")), (v.get("extra_range"), v.get("extra_value"))):
                if op and x not in (None, ""):
                    x = float(str(x).replace(",", "."))
                    if op.startswith("greater"):
                        lo = min(lo, x)
                    else:
                        hi = max(hi, x)
            if not v.get("extra_range") and (v.get("range") or "").startswith("greater"):
                hi = math.inf
            if not v.get("extra_range") and (v.get("range") or "").startswith("less"):
                lo = min(lo, 0.0)
        elif v.get("value") not in (None, ""):
            choices.append(str(v["value"]).strip())
    if choices:
        if all(_NUM.match(c) for c in choices) and unit:
            nums = [float(c.replace(",", ".")) for c in choices]
            return {"name": ch["name"], "type": "number", "unit": unit, "range": [min(nums), max(nums)]}
        return {"name": ch["name"], "type": "enum", "values": list(dict.fromkeys(choices))}
    if lo != math.inf or hi != -math.inf:
        lo = 0.0 if lo == math.inf else lo
        return {"name": ch["name"], "type": "number" if unit else "count", "unit": unit, "range": [lo, hi]}
    return None


def build_ktru(out: Path) -> Counter:
    links = {r["ktruCode"]: r["okpd2Code"] for r in json.load(gzip.open(SRC / "ktru-okpd2-moy-zakupki.json.gz", "rt",
                                                                         encoding="utf-8"))}
    stats, positions = Counter(), []
    for r in json.load(gzip.open(SRC / "ktru.json.gz", "rt", encoding="utf-8")):
        chars = [c for c in map(ktru_char, _parse(r.get("characteristics_jsonb"))) if c]
        syn = [s for s in (_parse(r.get("similar_products")) or []) if isinstance(s, str)]
        positions.append({"code": r["code"], "name": r["name"], "okpd2": links.get(r["code"]) or r["code"].split("-")[0],
                          "synonyms": syn, "characteristics": chars})
        stats["позиций"] += 1
        stats["с характеристиками"] += bool(chars)
        stats["с синонимами"] += bool(syn)
        stats["характеристик"] += len(chars)
    out.write_text(json.dumps(positions, ensure_ascii=False), encoding="utf-8")
    return stats


# --- 5. Laya v2 --------------------------------------------------------------------


def laya_question(char: str, options: list[str]) -> dict:
    crit = {o: None for o in options}
    crit[NONE] = "в описании этого нет"
    return {"type": "choice", "instructions": INSTR.format(char), "criteria": crit}


def laya_gold(q: dict, answer: str, smooth: float = 0.05) -> dict:
    opts = list(q["criteria"])
    rest = smooth / (len(opts) - 1)
    return {"label": answer, "probabilities": {o: (1 - smooth if o == answer else rest) for o in opts}}


def laya_state(cat: str, text: str, desc: str = "") -> str:
    s = f"Категория: {cat}. Товар: {text}"
    return f"{s}. Описание: {desc[:400]}" if desc else s


def build_laya(ste_path: Path, forms_path: Path, search_path: Path, search_forms_path: Path, out_dir: Path,
               n_train: int,
               pos: int, neg: int, distractors: int, desc_share: float) -> Counter:
    forms = {f["code"]: {c["name"]: c for c in f["characteristics"]} for f in json.loads(forms_path.read_text())}
    freq: dict[tuple[str, str], Counter] = defaultdict(Counter)
    share: dict[str, Counter] = defaultdict(Counter)
    n_cat = Counter()
    search = list(jl(search_path))
    held = {int(i) for q in search for i in [*q["relevant_ste_ids"], *q["hard_negative_ids"]] if i}
    pool = []
    for r in jl(ste_path):
        cat = r["family"]
        n_cat[cat] += 1
        for k, v in r["characteristics"].items():
            if r["types"].get(k) in ("text", "bool"):
                freq[(cat, k)][v] += 1
            share[cat][k] += 1
        if r["id"] not in held and not is_org_test(r["id"]) and cat in forms:
            pool.append(r)
    rnd = random.Random(0)
    train = rnd.sample(pool, min(n_train, len(pool)))

    def options(cat, name, answer):
        top = [v for v, _ in freq[(cat, name)].most_common(30)]
        opts = top[: distractors + 1] if answer == NONE else [answer, *[v for v in top if v != answer][:distractors]]
        rnd.shuffle(opts)
        return opts

    stats = Counter()
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "train.jsonl").open("w", encoding="utf-8") as f:
        for r in train:
            cat, gold = r["family"], r["characteristics"]
            p = [k for k, v in gold.items() if r["types"].get(k) in ("text", "bool") and len(freq[(cat, k)]) >= 2]
            n = [k for k, c in share[cat].items() if k not in gold and c / n_cat[cat] >= 0.2 and len(freq[(cat, k)]) >= 2]
            rnd.shuffle(p)
            rnd.shuffle(n)
            qs, exp = {}, {}
            for name, ans in [(k, gold[k]) for k in p[:pos]] + [(k, NONE) for k in n[:neg]]:
                key = f"q{len(qs)}"
                qs[key] = laya_question(name, options(cat, name, ans))
                exp[key] = ans
            if not qs:
                continue
            desc = r["description"] if rnd.random() < desc_share else ""
            f.write(json.dumps({"id": str(r["id"]), "state": laya_state(cat, r["text"], desc), "questions": qs,
                                "gold": {k: laya_gold(qs[k], a) for k, a in exp.items()}, "expected": exp},
                               ensure_ascii=False) + "\n")
            stats["train: записей"] += 1
            stats["train: вопросов"] += len(qs)
            stats["train: «не указано»"] += sum(a == NONE for a in exp.values())
            stats["train: с описанием"] += bool(desc)
    # Проверка: настоящие пользовательские запросы (validation + test). Анкета — кандидаты выдачи
    # портала для этого запроса; эталон — значения из запроса, привязанные к характеристикам
    # релевантной СТЕ; частые в выдаче характеристики, которых в запросе нет, — «не указано».
    qforms = {f["code"]: f for f in json.loads(search_forms_path.read_text())}
    with (out_dir / "eval.jsonl").open("w", encoding="utf-8") as f:
        for q in search:
            if q["split"] == "train" or q["id"] not in qforms:
                continue
            form = {c["name"]: c for c in qforms[q["id"]]["characteristics"]}
            qs, exp = {}, {}

            def ask(name, answer):
                ch = form[name]
                top = list(dict.fromkeys(ch.get("values") or []))[:30]
                if answer == NONE:
                    opts = top[: distractors + 1]
                else:
                    opts = [answer, *[v for v in top if v != answer][:distractors]]
                if len(opts) < 2:
                    return False
                rnd.shuffle(opts)
                key = f"q{len(qs)}"
                qs[key] = laya_question(name, opts)
                exp[key] = answer
                return True

            for name, value in q["characteristics"].items():
                if name in form and form[name]["type"] == "enum" and value in form[name]["values"]:
                    stats["eval: в запросе"] += ask(name, value)
            absent = sorted((c for n, c in form.items() if n not in q["characteristics"] and c["type"] == "enum"
                             and len(c["values"]) >= 2), key=lambda c: -c["frequency"])[:2]
            for c in absent:
                stats["eval: «не указано»"] += ask(c["name"], NONE)
            if qs:
                f.write(json.dumps({"id": q["id"], "query_type": q["query_type"],
                                    "state": laya_state(q["family"], q["text"]), "questions": qs,
                                    "gold": {k: laya_gold(qs[k], a) for k, a in exp.items()}, "expected": exp},
                                   ensure_ascii=False) + "\n")
                stats["eval: запросов"] += 1
    return stats


def _opt_key(o: str) -> str:
    return " ".join(str(o).lower().replace("ё", "е").split()).strip(" .,;:")


def clean_case(r: dict, max_option_len: int = 150) -> tuple[dict | None, Counter]:
    """Варианты, различающиеся только регистром/пробелами/знаками на конце («черный» / «Черный»,
    «Полидиоксанон.»), склеиваются в один (первый встретившийся), ответ переводится на него —
    иначе один и тот же смысл оказывается и верным, и неверным. Вопросы с ответом-числом
    выбрасываются (числа разбирают правила), как и вопросы с вариантом длиннее max_option_len
    или меньше чем с двумя вариантами после склейки."""
    st = Counter()
    qs, exp = {}, {}
    for qid, q in r["questions"].items():
        ans = r["expected"][qid]
        if ans != NONE and _NUM.match(str(ans).replace(" ", "")):
            st["выброшено: ответ — число"] += 1
            continue
        canon: dict[str, str] = {}
        for o in q["criteria"]:
            if o != NONE:
                canon.setdefault(_opt_key(o), o)
        opts = list(canon.values())
        if len(opts) < len(q["criteria"]) - 1:
            st["склеены дубли вариантов"] += 1
        if len(opts) < 2:
            st["выброшено: < 2 вариантов"] += 1
            continue
        if any(len(o) > max_option_len for o in opts):
            st["выброшено: длинный вариант"] += 1
            continue
        if ans != NONE:
            ans = canon.get(_opt_key(ans))
            if ans is None:
                st["выброшено: ответ не среди вариантов"] += 1
                continue
        key = f"q{len(qs)}"
        qs[key] = {**q, "criteria": {**{o: None for o in opts}, NONE: q["criteria"].get(NONE, "в описании этого нет")}}
        exp[key] = ans
    if not qs:
        return None, st
    out = {k: v for k, v in r.items() if k not in ("questions", "expected", "gold")}
    out.update(questions=qs, expected=exp, gold={k: laya_gold(qs[k], a) for k, a in exp.items()})
    return out, st


# --- 6. итоговый набор Laya v2 ----------------------------------------------------


def merge_laya(org_train: Path, api_dir: Path, search_path: Path) -> Counter:
    """train_all.jsonl = СТЕ организаторов (data/laya/train.jsonl, только наименование) + карточки API
    (с описанием у части). СТЕ из поискового датасета (релевантные и сложные отрицательные) выброшены
    из обоих, чтобы проверка на запросах осталась честной. Порядок перемешан."""
    held = {str(i) for q in jl(search_path) for i in [*q["relevant_ste_ids"], *q["hard_negative_ids"]] if i}
    rows, stats = [], Counter()
    for src, path in (("организаторы", org_train), ("API", api_dir / "train.jsonl")):
        for r in jl(path):
            if str(r.get("id")) in held:
                stats[f"{src}: выброшено (есть в поиске)"] += 1
                continue
            r, st = clean_case(r)
            stats.update(st)
            if r is None:
                continue
            rows.append(r)
            stats[f"{src}: записей"] += 1
            stats["вопросов"] += len(r["questions"])
            stats["«не указано»"] += sum(v == NONE for v in r["expected"].values())
    # проверочный набор — той же очисткой
    clean_eval = []
    for r in jl(api_dir / "eval.jsonl"):
        r, st = clean_case(r)
        stats.update({f"eval: {k}": v for k, v in st.items()})
        if r is not None:
            clean_eval.append(r)
            stats["eval: вопросов"] += len(r["questions"])
    with (api_dir / "eval_clean.jsonl").open("w", encoding="utf-8") as f:
        for r in clean_eval:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    random.Random(1).shuffle(rows)
    with (api_dir / "train_all.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return stats


# --- 5. Laya: только карточки zakupki.mos.ru ---------------------------------------


def card_state(r: dict, desc_chars: int) -> str:
    """Наименование, категория и описание — всегда (пустое, если у карточки его нет)."""
    desc = (r.get("description") or "").strip()[:desc_chars]
    return f"Товар: {r['text']}. Категория: {r['family']}. Описание: {desc or 'нет'}"


def build_laya_cards(ste_path: Path, out_dir: Path, n_train: int, n_eval: int, pos: int, neg: int,
                     distractors: int, desc_chars: int, only_with_desc: bool,
                     shard_questions: int = 100_000) -> Counter:
    """Набор Laya только из карточек GetSku (zakupki.mos.ru): 133 735 СТЕ с характеристиками.

    Разбиение по crc32(id) % 10: 0 — проверка, остальное — обучение; частоты значений и отвлекающие
    варианты — только по обучающей части. Вопрос на характеристику (текст и «Да/Нет»; числа
    разбирают правила): варианты — эталон + до `distractors` частых значений категории + «не
    указано». Положительные — заполненные у СТЕ (до `pos`), отрицательные — частые в категории
    (≥ 20 % СТЕ), но пустые у этой СТЕ (до `neg`, ответ «не указано»). Затем clean_case.
    """
    rows = [r for r in jl(ste_path) if r["characteristics"] and (r.get("description") or not only_with_desc)]
    parts = {"train": [], "val": [], "test": []}
    for r in rows:
        parts[split_of(r["id"])].append(r)
    train_rows = parts["train"]

    freq: dict[tuple[str, str], Counter] = defaultdict(Counter)
    share: dict[str, Counter] = defaultdict(Counter)
    n_cat = Counter()
    for r in train_rows:
        n_cat[r["family"]] += 1
        for k, v in r["characteristics"].items():
            if r["types"].get(k) in ("text", "bool"):
                freq[(r["family"], k)][v] += 1
            share[r["family"]][k] += 1

    rnd = random.Random(0)

    def case(r: dict) -> dict | None:
        cat, gold = r["family"], r["characteristics"]
        p = [k for k, v in gold.items() if r["types"].get(k) in ("text", "bool") and len(freq[(cat, k)]) >= 2]
        n = [k for k, c in share[cat].items()
             if k not in gold and c / max(n_cat[cat], 1) >= 0.2 and len(freq[(cat, k)]) >= 2]
        rnd.shuffle(p)
        rnd.shuffle(n)
        qs, exp = {}, {}
        for name, ans in [(k, gold[k]) for k in p[:pos]] + [(k, NONE) for k in n[:neg]]:
            top = [v for v, _ in freq[(cat, name)].most_common(30)]
            opts = top[: distractors + 1] if ans == NONE else [ans, *[v for v in top if v != ans][:distractors]]
            rnd.shuffle(opts)
            key = f"q{len(qs)}"
            qs[key] = laya_question(name, opts)
            exp[key] = ans
        if not qs:
            return None
        raw = {"id": str(r["id"]), "state": card_state(r, desc_chars), "questions": qs, "expected": exp,
               "gold": {k: laya_gold(qs[k], a) for k, a in exp.items()}}
        return clean_case(raw)[0]

    stats = Counter(карточек=len(rows), **{f"карточек {k}": len(v) for k, v in parts.items()})
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in [*out_dir.glob("train*.jsonl"), *out_dir.glob("eval*.jsonl")]:
        old.unlink()
    for name, pool, n in (("train", train_rows, n_train), ("val", parts["val"], n_eval), ("test", parts["test"], n_eval)):
        sample = rnd.sample(pool, n) if 0 < n < len(pool) else rnd.sample(pool, len(pool))
        shard, in_shard, f = 0, 0, None
        for r in sample:
            c = case(r)
            if c is None:
                continue
            if f is None or (name == "train" and in_shard >= shard_questions):
                if f is not None:
                    f.close()
                    shard += 1
                fname = f"train_{shard:02d}.jsonl" if name == "train" else f"{name}.jsonl"
                f = (out_dir / fname).open("w", encoding="utf-8")
                in_shard = 0
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
            in_shard += len(c["questions"])
            stats[f"{name}: записей"] += 1
            stats[f"{name}: вопросов"] += len(c["questions"])
            stats[f"{name}: «не указано»"] += sum(a == NONE for a in c["expected"].values())
            stats[f"{name}: с описанием"] += "Описание: нет" not in c["state"]
        if f is not None:
            f.close()
        if name == "train":
            stats["train: шардов"] = shard + 1
    return stats


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--only", choices=["ste", "forms", "search", "ktru", "laya", "laya-mixed", "merge"])
    p.add_argument("--n-train", type=int, default=0, help="обучающих карточек (0 — все)")
    p.add_argument("--n-eval", type=int, default=0, help="карточек в val и в test (0 — все)")
    p.add_argument("--shard-questions", type=int, default=100_000,
                   help="вопросов в шарде train_NN.jsonl (одна сессия Kaggle ≈ 4 ч)")
    p.add_argument("--desc-chars", type=int, default=1000)
    p.add_argument("--only-with-desc", action="store_true", help="только карточки с описанием")
    p.add_argument("--pos", type=int, default=4)
    p.add_argument("--neg", type=int, default=2)
    p.add_argument("--distractors", type=int, default=7)
    p.add_argument("--desc-share", type=float, default=0.3, help="доля обучающих записей с описанием в state")
    args = p.parse_args()

    ste, forms = DERIVED / "ste_api.jsonl", DERIVED / "forms_ste_api.json"
    search, ktru = ROOT / "data" / "eval" / "search_queries.jsonl", DERIVED / "ktru_api.json"
    search_forms = ROOT / "data" / "eval" / "search_forms.json"
    steps = {
        "ste": lambda: build_ste(ste),
        "forms": lambda: build_forms(ste, forms),
        "search": lambda: build_search(search, search_forms),
        "ktru": lambda: build_ktru(ktru),
        "laya": lambda: build_laya_cards(ste, ROOT / "data" / "laya_v3", args.n_train, args.n_eval, args.pos,
                                         args.neg, args.distractors, args.desc_chars, args.only_with_desc,
                                         args.shard_questions),
        "laya-mixed": lambda: build_laya(ste, forms, search, search_forms, ROOT / "data" / "laya_v2", args.n_train, args.pos, args.neg,
                                   args.distractors, args.desc_share),
        "merge": lambda: merge_laya(ROOT / "data" / "laya" / "train.jsonl", ROOT / "data" / "laya_v2", search),
    }
    default = ("ste", "forms", "ktru", "laya")  # search / laya-mixed / merge — поисковый датасет (LLM), по запросу
    for name, fn in steps.items():
        if (args.only and name != args.only) or (not args.only and name not in default):
            continue
        print(f"[{name}]", dict(fn()), flush=True)


if __name__ == "__main__":
    main()
