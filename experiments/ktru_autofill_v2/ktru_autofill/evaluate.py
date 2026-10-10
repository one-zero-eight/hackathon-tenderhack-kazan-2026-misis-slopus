"""Оценка качества: характеристики, приведение единиц и сужение кода КТРУ.

    ktru-eval data-archived/test_handwritten.jsonl data-archived/test_generated.jsonl
    ktru-eval ste_организаторов.xlsx -c анкеты.json --text-col ... --chars-col ... --code-col ...

Характеристики:
  точность  = верных извлечённых / всех извлечённых;
  полнота   = верных извлечённых / всех характеристик эталона;
  выдумано  = извлечённых значений, которые неверны и не подтверждаются текстом описания
              (ни слов значения/его синонима, ни такого числа в тексте нет) / всех извлечённых —
              худшая ошибка для госзакупок: в заявку попадает то, чего заказчик не писал;
  «есть в тексте» — доля эталонных значений, которые вообще упомянуты (потолок полноты).
Статус каждой пары «СТЕ × характеристика»: верно | неверно (значение из текста, но не то) |
  выдумано | лишнее (в эталоне характеристики нет, значение из текста) | пропуск (в тексте
  было, не извлекли) | нет в тексте (не извлекли, но и в описании этого нет).

После прогона в `--out` (по умолчанию reports/<дата-время>/<набор>/) сохраняются:
  metrics.json — все метрики; items.jsonl — по каждой СТЕ товар/код найдено vs ожидалось и
  все характеристики со статусом; values.csv — то же плоской таблицей; report.md — сводка.
Единицы: сколько значений, записанных не в единице КТРУ («80 грамм», «1,2 м»),
  приведено к ней верно.
КТРУ: семейство товара угадано; верный код не потерян при сужении; код определён
  однозначно и верно; сколько кодов остаётся после каждого шага.

Без `-c` извлечение идёт по всему каталогу (data-archived/ktru_catalog.json). С `-c` —
по анкете кода КТРУ СТЕ (шаг 2 считается идеальным); `--forms-from-data` —
анкета строится из самого датасета.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import random
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean, median
from typing import Any

from rapidfuzz import fuzz

from . import units as U
from .autofill import Autofiller
from .catalog import Catalog
from .numbers import extract_numbers
from .schema import Characteristic, KtruCandidate, build_form
from .text import lemmatize, normalize_code, tokenize

_NUM_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(.*?)\s*$")

TEXT_COLUMNS = ("text", "description", "name", "Описание", "Наименование СТЕ", "Наименование", "Название СТЕ")
CHARS_COLUMNS = ("characteristics", "Характеристики", "Характеристики СТЕ")
CODE_COLUMNS = ("code", "ktru_code", "Код КТРУ", "КТРУ")
FAMILY_COLUMNS = ("family", "Наименование КТРУ", "category", "Категория")
ID_COLUMNS = ("id", "Идентификатор СТЕ", "ID СТЕ", "ste_id")


@dataclass
class Ste:
    id: str
    text: str
    gold: dict[str, str]  # характеристика → значение как в эталоне
    code: str = ""
    family: str = ""
    titles: list[str] = field(default_factory=list)  # допустимые названия позиций КТРУ (реальный каталог)


# --- загрузка ---------------------------------------------------------------


def parse_chars(raw: Any) -> dict[str, str]:
    """dict | [{"name","value","unit"}] | JSON-строка | «Имя: значение; Имя: значение»."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        s = raw.strip()
        if s[:1] in "[{":
            return parse_chars(json.loads(s))
        out = {}
        for part in re.split(r";|\n", s):
            if ":" in part:
                k, v = part.split(":", 1)
                if k.strip() and v.strip():
                    out[k.strip()] = v.strip()
        return out
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if v not in (None, "")}
    out = {}
    for item in raw:
        name = item.get("name") or item.get("Наименование") or item.get("characteristic")
        value = item.get("value") if "value" in item else item.get("Значение")
        if name and value not in (None, ""):
            unit = item.get("unit") or item.get("Единица измерения")
            out[str(name)] = f"{value} {unit}" if unit else str(value)
    return out


def _pick(row: dict[str, Any], names: tuple[str, ...], override: str | None) -> Any:
    if override:
        return row.get(override)
    for n in names:
        if n in row:
            return row[n]
    return None


def _rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]
    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    if path.suffix in (".csv", ".tsv"):
        with path.open(encoding="utf-8-sig", newline="") as f:
            header = f.readline()
            f.seek(0)
            # Sniffer путается, когда «;» есть и внутри строки характеристик — смотрим только шапку.
            delim = max("\t;,", key=header.count)
            return list(csv.DictReader(f, delimiter=delim))
    if path.suffix in (".xlsx", ".xlsm"):
        import openpyxl

        ws = openpyxl.load_workbook(path, read_only=True).active
        it = ws.iter_rows(values_only=True)
        header = [str(h) for h in next(it)]
        return [dict(zip(header, r)) for r in it]
    if path.suffix == ".parquet":
        import pandas as pd

        return pd.read_parquet(path).to_dict("records")
    raise SystemExit(f"неизвестный формат: {path}")


def load_ste(path: Path, text_col=None, chars_col=None, code_col=None) -> list[Ste]:
    out = []
    for i, row in enumerate(_rows(path)):
        text = _pick(row, TEXT_COLUMNS, text_col)
        gold = parse_chars(_pick(row, CHARS_COLUMNS, chars_col))
        titles = row.get("titles") or []
        if text and (gold or titles):
            sid = _pick(row, ID_COLUMNS, None)
            out.append(Ste(
                f"{path.stem}:{sid if sid is not None else i}", str(text), gold,
                str(_pick(row, CODE_COLUMNS, code_col) or ""), str(_pick(row, FAMILY_COLUMNS, None) or ""),
                list(titles),
            ))
    return out


# --- анкета из датасета (нет ни каталога, ни анкет) ---------------------------


def _split_number(value: str) -> tuple[float, str] | None:
    m = _NUM_RE.match(str(value))
    return (float(m.group(1).replace(",", ".")), m.group(2)) if m else None


def forms_from_dataset(stes: list[Ste], free_text_ratio: float = 0.5) -> dict[str, KtruCandidate]:
    """Анкета на каждый код (категорию) по эталонам: все встреченные значения.

    Числовая характеристика — если ≥ 80 % значений числа. У Портала поставщиков числа
    записаны без единиц («Объем:35»), поэтому единицы узнаём из наименований: если
    в «Мусорные пакеты 35л» число 35 совпало с эталонным объёмом, «л» — единица объёма
    этой категории. Свободный текст («Описание», «Модель»: почти все значения разные)
    в анкету как список вариантов не попадает — выбирать из него нечего.
    """
    by_code: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    unit_votes: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for s in stes:
        mentions = None
        for name, value in s.gold.items():
            by_code[s.code][name].append(value)
            num = _split_number(value)
            if num and not num[1]:
                mentions = mentions if mentions is not None else extract_numbers(s.text)
                for m in mentions:
                    if m.unit_raw and abs(m.value - num[0]) < 1e-9:
                        unit_votes[(s.code, name)][U.unit_key(m.unit_raw)] += 1

    forms = {}
    for code, chars in by_code.items():
        result = []
        for name, values in chars.items():
            nums = [n for n in map(_split_number, values) if n]
            if len(nums) >= 0.8 * len(values):
                lo, hi = min(v for v, _ in nums), max(v for v, _ in nums)
                units = Counter(u for _, u in nums if u)
                votes = unit_votes.get((code, name), Counter())
                total = sum(votes.values())
                learned = [u for u, k in votes.most_common() if k >= 2 and k >= 0.15 * total]
                unit = (units.most_common(1)[0][0] if units else None) or (learned[0] if learned else None)
                aliases = [u for u in [*units, *learned] if u != unit]
                result.append(Characteristic(name, "number" if unit else "count", [], {}, unit, aliases, (lo, hi),
                                             frequency=len(values)))
                continue
            distinct = list(dict.fromkeys(values))
            if len(values) >= 10 and len(distinct) > free_text_ratio * len(values):
                continue  # свободный текст
            result.append(Characteristic(name, "enum", distinct, frequency=len(values)))
        forms[code] = KtruCandidate(code, code, result)
    return forms


# --- сравнение ---------------------------------------------------------------


def _norm(s: str) -> str:
    return lemmatize(s) or normalize_code(s)


def is_correct(pred: Any, gold: str) -> bool:
    """Число сравнивается в единице предсказания: эталон «1,2 м» ≡ 1200 мм."""
    unit = None
    if isinstance(pred, dict):
        pred, unit = pred.get("значение"), pred.get("единица")
    if isinstance(pred, (int, float)):
        g = _split_number(gold)
        if g is None:
            return False
        value, gunit = g
        src, dst = (U.lookup(gunit) if gunit else None), U.canonical(unit)
        if src and dst and src.name != dst.name:
            conv = U.convert(value, src, dst)
            value = conv if conv is not None else value
        return abs(value - pred) < 1e-6 * max(1.0, abs(value))
    return _norm(str(pred)) == _norm(gold)


def mentioned(text: str, gold: str, synonyms: list[str] = ()) -> bool:
    """Упомянуто ли эталонное значение в описании: число (с пересчётом единиц),
    все слова значения или одного из его синонимов из полного каталога."""
    g = _split_number(gold)
    if g:
        src = U.lookup(g[1]) if g[1] else None
        for m in extract_numbers(text):
            if any(abs(v - g[0]) < 1e-6 for v in (m.value, *m.dims)):
                return True
            unit = U.lookup(m.unit_raw) if m.unit_raw else None
            conv = U.convert(m.value, unit, src) if src and unit else None
            if conv is not None and abs(conv - g[0]) < 1e-6:
                return True
        return False
    words = set(_norm(text).split()) | {t.lemma for t in tokenize(text)}
    return any(all(w in words for w in _norm(ph).split()) for ph in [gold, *synonyms] if _norm(ph))


def supported(text: str, pred: Any, synonyms: list[str] = ()) -> bool:
    """Подтверждается ли извлечённое значение текстом: для числа — такое число есть
    в тексте (с пересчётом единиц), для значения из списка — все его слова или слова
    его синонима есть в тексте (с опечатками: нечёткое сравнение ≥ 0.85)."""
    unit = None
    if isinstance(pred, dict):
        pred, unit = pred.get("значение"), pred.get("единица")
    if isinstance(pred, (int, float)):
        dst = U.canonical(unit)
        for m in extract_numbers(text):
            if any(abs(v - pred) < 1e-6 for v in (m.value, *m.dims)):
                return True
            src = U.lookup(m.unit_raw) if m.unit_raw else None
            conv = U.convert(m.value, src, dst) if src and dst else None
            if conv is not None and abs(conv - pred) < 1e-6:
                return True
        return False
    words = set(_norm(text).split()) | {t.lemma for t in tokenize(text)} | {t.surface for t in tokenize(text)}

    def has(w: str) -> bool:
        return w in words or (len(w) >= 4 and any(len(x) >= 4 and fuzz.ratio(w, x) >= 85 for x in words))

    return any(all(has(w) for w in _norm(ph).split()) for ph in [str(pred), *synonyms] if _norm(ph))


@dataclass
class Counts:
    gold: int = 0
    extracted: int = 0
    correct: int = 0
    mentioned: int = 0
    correct_mentioned: int = 0
    verifiable: int = 0  # извлечено и в эталоне эта характеристика есть
    hallucinated: int = 0  # неверно и не подтверждается текстом
    extra: int = 0  # извлечено, а в эталоне такой характеристики нет

    def add(self, other: Counts) -> None:
        for k in vars(self):
            setattr(self, k, getattr(self, k) + getattr(other, k))

    @property
    def precision(self) -> float:
        return self.correct / self.extracted if self.extracted else 0.0

    @property
    def recall(self) -> float:
        return self.correct / self.gold if self.gold else 0.0

    @property
    def precision_verifiable(self) -> float:
        """Точность там, где её можно проверить: у СТЕ эта характеристика заполнена."""
        return self.correct / self.verifiable if self.verifiable else 0.0

    @property
    def hallucination_rate(self) -> float:
        return self.hallucinated / self.extracted if self.extracted else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0


@dataclass
class Report:
    total: Counts = field(default_factory=Counts)
    by_char: dict[str, Counts] = field(default_factory=lambda: defaultdict(Counts))
    by_method: dict[str, Counts] = field(default_factory=lambda: defaultdict(Counts))
    by_family: dict[str, Counts] = field(default_factory=lambda: defaultdict(Counts))
    errors: list[dict[str, Any]] = field(default_factory=list)
    n_ste: int = 0
    units_total: int = 0  # значения, записанные не в единице КТРУ
    units_ok: int = 0
    family_known: int = 0
    family_hit: int = 0  # верное семейство среди оставшихся
    family_exact: int = 0  # осталось только верное семейство
    code_known: int = 0
    code_kept: int = 0  # верный код не отброшен при сужении
    code_exact: int = 0  # код определён однозначно и верно
    code_wrong: int = 0  # код определён однозначно, но неверно
    code_determinable: int = 0  # эталон однозначно задаёт код
    code_exact_determinable: int = 0
    remaining: list[int] = field(default_factory=list)
    trajectory: dict[int, list[int]] = field(default_factory=lambda: defaultdict(list))
    suggested: int = 0  # подсказок (не заполнено, ждёт подтверждения)
    suggested_correct: int = 0
    product_known: int = 0  # у СТЕ известен ожидаемый товар
    product_top: int = 0  # первый найденный товар — ожидаемый
    product_hit: int = 0  # ожидаемый товар среди оставшихся
    items: list[dict[str, Any]] = field(default_factory=list)  # по каждой СТЕ: найдено vs ожидалось


def gold_codes(s: Ste, catalog: Catalog | None) -> set[str]:
    """Истинный код; если его нет — все позиции семейства, совместимые с эталоном."""
    if s.code:
        return {s.code}
    if catalog is None or not s.family:
        return set()
    return {p.code for p in catalog.positions if p.name == s.family
            and all(p.char(k) is None or p.accepts(k, _gold_value(p.char(k), v)) for k, v in s.gold.items())}


def _gold_value(ch: Characteristic, v: str) -> Any:
    if ch.type == "enum":
        return v
    g = _split_number(v)
    if not g:
        return float("nan")
    src, dst = (U.lookup(g[1]) if g[1] else None), U.canonical(ch.unit)
    conv = U.convert(g[0], src, dst) if src and dst else None
    return conv if conv is not None else g[0]


def evaluate(stes: list[Ste], filler: Autofiller, forms: dict[str, KtruCandidate] | None = None,
             catalog: Catalog | None = None, synonyms_from: Catalog | None = None) -> Report:
    """`synonyms_from` — каталог с синонимами значений, чтобы считать «упомянуто в тексте»
    одинаково и при абляции, когда у `catalog` синонимы убраны."""
    rep = Report()
    syn_catalog = synonyms_from or catalog
    forms_by_family: dict[str, dict[str, Characteristic]] = {}

    def syn(family: str, char: str, value: str) -> list[str]:
        if syn_catalog is None or not family:
            return []
        if family not in forms_by_family:
            fam = [p for p in syn_catalog.positions if p.name == family]
            forms_by_family[family] = {c.name: c for c in build_form(fam)}
        ch = forms_by_family[family].get(char)
        return ch.synonyms.get(value, []) if ch else []

    for s in stes:
        cands = None
        if forms is not None:
            if s.code not in forms:
                continue
            cands = [forms[s.code]]
        rep.n_ste += 1
        res = filler.fill(s.text, cands)
        by_name = {m.характеристика: m for m in res.matches}

        rows = []
        for name in res.values.keys() | s.gold.keys():
            pred, gold = res.values.get(name), s.gold.get(name)
            synonyms = syn(s.family, name, gold) if gold is not None else []
            c = Counts(gold=int(gold is not None), extracted=int(pred is not None),
                       verifiable=int(pred is not None and gold is not None))
            c.correct = int(pred is not None and gold is not None and is_correct(pred, gold))
            c.mentioned = int(gold is not None and mentioned(s.text, gold, synonyms))
            c.correct_mentioned = c.correct * c.mentioned
            if pred is not None and not c.correct:
                pred_syn = syn(s.family, name, str(pred)) if not isinstance(pred, (dict, int, float)) else []
                c.hallucinated = int(not supported(s.text, pred, pred_syn))
                c.extra = int(gold is None)
            status = ("верно" if c.correct else "выдумано" if c.hallucinated else "лишнее" if c.extra
                      else "неверно" if pred is not None else "пропуск" if c.mentioned else "нет в тексте")
            rep.total.add(c)
            rep.by_char[name].add(c)
            rep.by_family[s.family or "?"].add(c)
            m = by_name.get(name)
            if pred is not None:
                rep.by_method[m.метод if m else "?"].add(c)
            rows.append({"характеристика": name, "ожидалось": gold, "найдено": pred, "статус": status,
                         "метод": m.метод if m else None, "фрагмент": m.фрагмент if m else None,
                         "оценка": m.оценка if m else None})
            # Единица в тексте отличается от единицы КТРУ — проверяем приведение.
            if isinstance(pred, dict) and gold is not None and pred.get("как_написано"):
                written = re.sub(r"^[\d\s.,xх×*]+", "", pred["как_написано"])
                if written and U.unit_key(written) != U.unit_key(pred["единица"] or ""):
                    rep.units_total += 1
                    rep.units_ok += c.correct
            if status in ("неверно", "выдумано", "лишнее", "пропуск"):
                rep.errors.append({"id": s.id, "text": s.text, "характеристика": name, "тип": status,
                                   "извлечено": pred, "эталон": gold, "метод": m.метод if m else None,
                                   "фрагмент": m.фрагмент if m else None})
        for sg in res.suggestions:
            gold = s.gold.get(sg.характеристика)
            ok = gold is not None and is_correct(sg.значение, gold)
            rep.suggested += 1
            rep.suggested_correct += ok
            rows.append({"характеристика": sg.характеристика, "ожидалось": gold, "найдено": sg.значение,
                         "статус": "подсказка верна" if ok else "подсказка неверна", "метод": sg.метод,
                         "фрагмент": sg.фрагмент, "оценка": sg.оценка})
        order = {"выдумано": 0, "неверно": 1, "лишнее": 2, "пропуск": 3, "подсказка неверна": 4,
                 "подсказка верна": 5, "нет в тексте": 6, "верно": 7}
        rows.sort(key=lambda r: (order[r["статус"]], r["характеристика"]))

        # Товар: найденный (первый — самый похожий) vs ожидаемый.
        found_products = list(dict.fromkeys(
            [t for t, _ in res.families if isinstance(t, str)] + [c.name for c in res.candidates]))
        expected_products = list(s.titles) or ([s.family] if s.family else [])
        product = None
        if expected_products:
            rep.product_known += 1
            top_ok = bool(found_products) and found_products[0] in expected_products
            hit_ok = bool(set(found_products) & set(expected_products))
            rep.product_top += top_ok
            rep.product_hit += hit_ok
            product = {"ожидался": expected_products, "найден": found_products[:5],
                       "верно_первым": top_ok, "среди_найденных": hit_ok}
            if not hit_ok:
                rep.errors.append({"id": s.id, "text": s.text, "характеристика": "ТОВАР", "тип": "товар не найден",
                                   "извлечено": found_products[:3], "эталон": expected_products,
                                   "метод": None, "фрагмент": None})
        item = {"id": s.id, "text": s.text, "товар": product,
                "код": {"ожидался": s.code or None, "найден": res.code, "осталось": len(res.candidates),
                        "кандидаты": [c.code for c in res.candidates[:10]]},
                "характеристики": rows,
                "сужение": [f"{st.шаг} [{st.осталось}]" for st in res.steps],
                "опечатки": [f"{t.как_написано}→{t.исправление}" for t in res.typos]}
        rep.items.append(item)

        names = {c.name for c in res.candidates}
        if s.family and forms is None and not s.titles:
            rep.family_known += 1
            rep.family_hit += s.family in names
            rep.family_exact += names == {s.family}
        golds = gold_codes(s, catalog) if forms is None else set()
        if golds:
            rep.code_known += 1
            remaining = {c.code for c in res.candidates}
            rep.code_kept += bool(golds & remaining)
            rep.code_exact += res.code in golds
            rep.code_wrong += res.code is not None and res.code not in golds
            said = Ste(s.id, s.text, {k: v for k, v in s.gold.items()
                                      if mentioned(s.text, v, syn(s.family, k, v))}, "", s.family)
            if catalog is not None and len(gold_codes(said, catalog)) == 1:
                rep.code_determinable += 1
                rep.code_exact_determinable += res.code in golds
            item["код"]["верный_не_потерян"] = bool(golds & remaining)
            if not golds & remaining:
                rep.errors.append({"id": s.id, "text": s.text, "характеристика": "КОД КТРУ", "тип": "код потерян",
                                   "извлечено": sorted(remaining)[:5], "эталон": sorted(golds)[:5], "метод": None,
                                   "фрагмент": " → ".join(f"{st.шаг} [{st.осталось}]" for st in res.steps)})
        rep.remaining.append(len(res.candidates))
        for i, st in enumerate(res.steps):
            rep.trajectory[i].append(st.осталось)
    return rep


# --- вывод -------------------------------------------------------------------


def _pct(x: float) -> str:
    return f"{100 * x:5.1f}%"


def _ratio(a: int, b: int) -> str:
    return f"{_pct(a / b) if b else '    —'} ({a}/{b})"


def print_report(rep: Report, title: str = "", top: int = 30, out=sys.stdout) -> None:
    p = lambda *a: print(*a, file=out)  # noqa: E731
    t = rep.total
    if title:
        p(f"\n{'=' * 20} {title} {'=' * 20}")
    p(f"СТЕ: {rep.n_ste}   характеристик в эталоне: {t.gold}   извлечено: {t.extracted}   верно: {t.correct}")
    if not t.gold and t.extracted:
        p(f"Эталона характеристик нет — проверяется товар; из {t.extracted} извлечённых значений "
          f"не подтверждаются текстом {_ratio(t.hallucinated, t.extracted)}")
    elif t.gold:
        p(f"Точность: {_pct(t.precision)} (на проверяемых {_ratio(t.correct, t.verifiable)})   "
          f"Полнота: {_pct(t.recall)}   F1: {_pct(t.f1)}   Выдумано: {_ratio(t.hallucinated, t.extracted)}")
    if rep.suggested:
        tail = f", из них верных {_ratio(rep.suggested_correct, rep.suggested)}" if t.gold else ""
        p(f"Подсказки (не заполнены, нужно подтверждение): {rep.suggested}{tail}")
    if rep.product_known:
        p(f"Товар: первым найден ожидаемый {_ratio(rep.product_top, rep.product_known)}; "
          f"ожидаемый среди найденных {_ratio(rep.product_hit, rep.product_known)}")
    p(f"Значение эталона есть в описании: {_ratio(t.mentioned, t.gold)} — потолок полноты; "
      f"полнота среди них: {_ratio(t.correct_mentioned, t.mentioned)}")
    p(f"Приведение единиц (написано не в единице КТРУ): {_ratio(rep.units_ok, rep.units_total)}")

    if rep.family_known:
        p("\nКТРУ:")
        p(f"  товар верный и единственный:            {_ratio(rep.family_exact, rep.family_known)}")
        p(f"  верный товар среди оставшихся:          {_ratio(rep.family_hit, rep.family_known)}")
    if rep.code_known:
        p(f"  верный код не потерян при сужении:      {_ratio(rep.code_kept, rep.code_known)}")
        p(f"  код определён однозначно и верно:       {_ratio(rep.code_exact, rep.code_known)}"
          f"   однозначно, но неверно: {rep.code_wrong}")
        p(f"  … если описание однозначно задаёт код:  {_ratio(rep.code_exact_determinable, rep.code_determinable)}")
    if rep.remaining and rep.family_known:
        p(f"  кодов осталось: медиана {median(rep.remaining):g}, среднее {mean(rep.remaining):.1f}")
        traj = " → ".join(f"{mean(v):.1f}" for _, v in sorted(rep.trajectory.items()) if len(v) >= 0.2 * rep.n_ste)
        p(f"  среднее число кодов по шагам (каталог → товар → 1-я, 2-я… характеристика): {traj}")

    p("\nПо методам:")
    for m, c in sorted(rep.by_method.items(), key=lambda kv: -kv[1].extracted):
        p(f"  {m:20} извлечено {c.extracted:6}  точность {_pct(c.precision)}  выдумано {_pct(c.hallucination_rate)}")

    if len(rep.by_family) > 1:
        p("\nПо товарам (худшие сверху):")
        p(f"  {'товар':44} {'эталон':>7} {'точн.':>7} {'полн.':>7} {'в тексте':>9}")
        for name, c in sorted(rep.by_family.items(), key=lambda kv: kv[1].f1):
            p(f"  {name[:44]:44} {c.gold:7} {_pct(c.precision):>7} {_pct(c.recall):>7} "
              f"{_pct(c.mentioned / c.gold if c.gold else 0):>9}")

    p(f"\nПо характеристикам (топ-{top} по частоте):")
    p(f"  {'характеристика':40} {'эталон':>7} {'извл.':>6} {'точн.':>7} {'полн.':>7} {'в тексте':>9}")
    for name, c in sorted(rep.by_char.items(), key=lambda kv: -kv[1].gold)[:top]:
        p(f"  {name[:40]:40} {c.gold:7} {c.extracted:6} {_pct(c.precision):>7} {_pct(c.recall):>7} "
          f"{_pct(c.mentioned / c.gold if c.gold else 0):>9}")

    kinds = Counter(e["тип"] for e in rep.errors)
    p(f"\nОшибки: {dict(kinds)}")


def report_json(rep: Report) -> dict[str, Any]:
    t = rep.total
    return {
        "ste": rep.n_ste, "gold": t.gold, "extracted": t.extracted, "correct": t.correct,
        "precision": t.precision, "precision_verifiable": t.precision_verifiable, "verifiable": t.verifiable,
        "recall": t.recall, "f1": t.f1, "mentioned": t.mentioned,
        "hallucinated": t.hallucinated, "hallucination_rate": t.hallucination_rate, "extra": t.extra,
        "suggestions": {"total": rep.suggested, "correct": rep.suggested_correct},
        "product": {"known": rep.product_known, "top": rep.product_top, "hit": rep.product_hit},
        "statuses": dict(Counter(r["статус"] for it in rep.items for r in it["характеристики"])),
        "by_method": {k: {**vars(v), "precision": v.precision, "hallucination_rate": v.hallucination_rate}
                      for k, v in rep.by_method.items()},
        "units": {"total": rep.units_total, "ok": rep.units_ok},
        "ktru": {"family_exact": rep.family_exact, "family_hit": rep.family_hit, "family_known": rep.family_known,
                 "code_kept": rep.code_kept, "code_exact": rep.code_exact, "code_wrong": rep.code_wrong,
                 "code_known": rep.code_known, "remaining_median": median(rep.remaining) if rep.remaining else None},
        "by_char": {k: {**vars(v), "precision": v.precision, "recall": v.recall,
                        "hallucination_rate": v.hallucination_rate} for k, v in rep.by_char.items()},
    }


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, dict):
        return f"{v.get('значение'):g} {v.get('единица') or ''}".strip()
    return str(v)


def write_artifacts(rep: Report, out: Path, title: str, meta: dict[str, Any] | None = None) -> Path:
    """metrics.json, items.jsonl, values.csv, report.md — в папку `out`."""
    out.mkdir(parents=True, exist_ok=True)
    metrics = {"dataset": title, "created": dt.datetime.now().isoformat(timespec="seconds"),
               **(meta or {}), **report_json(rep)}
    (out / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with (out / "items.jsonl").open("w", encoding="utf-8") as f:
        for it in rep.items:
            f.write(json.dumps(it, ensure_ascii=False, default=str) + "\n")
    with (out / "values.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["id", "описание", "товар ожидался", "товар найден", "код найден", "кодов осталось",
                    "характеристика", "ожидалось", "найдено", "статус", "метод", "фрагмент"])
        for it in rep.items:
            prod = it["товар"] or {}
            exp, fnd = "; ".join(prod.get("ожидался", [])), "; ".join(prod.get("найден", [])[:1])
            rows = it["характеристики"] or [{}]
            for r in rows:
                w.writerow([it["id"], it["text"], exp, fnd, it["код"]["найден"] or "", it["код"]["осталось"],
                            r.get("характеристика", ""), _cell(r.get("ожидалось")), _cell(r.get("найдено")),
                            r.get("статус", ""), r.get("метод") or "", r.get("фрагмент") or ""])

    t = rep.total
    md = [f"# Качество автозаполнения: {title}", "",
          f"Создано: {metrics['created']}" + "".join(f" · {k}: {v}" for k, v in (meta or {}).items()), "",
          "| метрика | значение |", "|---|---|", f"| СТЕ | {rep.n_ste} |"]
    if not t.gold and t.extracted:
        md += [f"| извлечено значений (эталона характеристик нет) | {t.extracted} |",
               f"| не подтверждаются текстом | {_ratio(t.hallucinated, t.extracted).strip()} |"]
    elif t.gold:
        md += [f"| характеристик в эталоне | {t.gold} |", f"| извлечено значений | {t.extracted} |",
               f"| **точность** (верных среди извлечённых) | **{_ratio(t.correct, t.extracted).strip()}** |",
               f"| точность на проверяемых (у СТЕ эта характеристика заполнена) | {_ratio(t.correct, t.verifiable).strip()} |",
               f"| **полнота** (найдено из эталона) | **{_ratio(t.correct, t.gold).strip()}** |",
               f"| **выдумано** (неверно и нет в тексте) | **{_ratio(t.hallucinated, t.extracted).strip()}** |",
               f"| лишнее (характеристики нет в эталоне) | {_ratio(t.extra, t.extracted).strip()} |",
               f"| подсказки на подтверждение (верных) | {_ratio(rep.suggested_correct, rep.suggested).strip()} |",
               f"| потолок полноты (значение есть в описании) | {_ratio(t.mentioned, t.gold).strip()} |",
               f"| приведение единиц | {_ratio(rep.units_ok, rep.units_total).strip()} |"]
    if rep.product_known:
        md += [f"| товар: первым найден ожидаемый | {_ratio(rep.product_top, rep.product_known).strip()} |",
               f"| товар: ожидаемый среди найденных | {_ratio(rep.product_hit, rep.product_known).strip()} |"]
    if rep.code_known:
        md += [f"| верный код КТРУ не потерян | {_ratio(rep.code_kept, rep.code_known).strip()} |",
               f"| код определён однозначно и верно | {_ratio(rep.code_exact, rep.code_known).strip()} |",
               f"| код определён однозначно, но неверно | {rep.code_wrong} |"]
    if rep.remaining:
        md.append(f"| кодов осталось (медиана) | {median(rep.remaining):g} |")

    statuses = Counter(r["статус"] for it in rep.items for r in it["характеристики"])
    if statuses:
        md += ["", "## Статусы значений", "", "| статус | число |", "|---|---|"]
        md += [f"| {k} | {v} |" for k, v in statuses.most_common()]

    def table(rows: list[list[Any]], head: list[str]) -> list[str]:
        out_rows = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
        return out_rows + ["| " + " | ".join(str(c).replace("|", "/") for c in r) + " |" for r in rows]

    halluc = [(it, r) for it in rep.items for r in it["характеристики"] if r["статус"] == "выдумано"]
    md += ["", f"## Выдуманные значения ({len(halluc)})", ""]
    md += table([[it["text"], r["характеристика"], _cell(r["найдено"]), _cell(r["ожидалось"]) or "—",
                  r["метод"], r["фрагмент"]] for it, r in halluc],
                ["описание", "характеристика", "найдено", "ожидалось", "метод", "фрагмент"]) if halluc else ["нет"]

    wrong = [(it, r) for it in rep.items for r in it["характеристики"] if r["статус"] == "неверно"]
    md += ["", f"## Неверные значения ({len(wrong)})", ""]
    md += table([[it["text"], r["характеристика"], _cell(r["найдено"]), _cell(r["ожидалось"]), r["метод"],
                  r["фрагмент"]] for it, r in wrong],
                ["описание", "характеристика", "найдено", "ожидалось", "метод", "фрагмент"]) if wrong else ["нет"]

    if rep.product_known:
        md += ["", "## Товар: найдено vs ожидалось", ""]
        md += table([["✅" if it["товар"]["верно_первым"] else "⚠️" if it["товар"]["среди_найденных"] else "❌",
                      it["text"], "; ".join(it["товар"]["ожидался"]), "; ".join(it["товар"]["найден"][:2]),
                      it["код"]["осталось"]] for it in rep.items if it["товар"]],
                    ["", "описание", "ожидался", "найден (первые)", "позиций осталось"])

    if rep.by_char:
        md += ["", "## По характеристикам (худшие сверху)", ""]
        md += table([[k, v.gold, v.extracted, _pct(v.precision).strip(), _pct(v.recall).strip(), v.hallucinated]
                     for k, v in sorted(rep.by_char.items(), key=lambda kv: (kv[1].f1, -kv[1].gold))[:40]],
                    ["характеристика", "в эталоне", "извлечено", "точность", "полнота", "выдумано"])
    (out / "report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="ktru-eval", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("datasets", type=Path, nargs="+", help="СТЕ: .jsonl/.json/.csv/.xlsx/.parquet")
    p.add_argument("-c", "--candidates", type=Path, help="анкеты КТРУ по кодам (JSON-список с code)")
    p.add_argument("--catalog", type=Path, help="каталог КТРУ (по умолчанию data-archived/ktru_catalog.json)")
    p.add_argument("--forms-from-data", action="store_true", help="анкеты из самого датасета, без каталога")
    p.add_argument("--text-col")
    p.add_argument("--chars-col")
    p.add_argument("--code-col")
    p.add_argument("--limit", type=int, help="случайная выборка из N СТЕ на файл")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--no-embeddings", action="store_true")
    p.add_argument("--strip-synonyms", action="store_true",
                   help="абляция: убрать из каталога синонимы значений — как система справится с незнакомыми формулировками")
    p.add_argument("--real", action="store_true", help="настоящий КТРУ + дерево ОКПД2 (data-archived/ktru_real.json)")
    p.add_argument("--out", type=Path, help="папка артефактов (по умолчанию reports/<дата-время>)")
    p.add_argument("--errors", type=Path, help="записать ошибки в JSONL")
    p.add_argument("--json", type=Path, help="записать метрики в JSON")
    args = p.parse_args(argv)

    catalog = full_catalog = None
    if not args.candidates and not args.forms_from_data:
        catalog = Catalog.load(args.catalog) if args.catalog else Catalog.load()
        full_catalog = catalog
        if args.strip_synonyms:
            catalog = Catalog.load(args.catalog) if args.catalog else Catalog.load()
            for pos in catalog.positions:
                for ch in pos.characteristics:
                    ch.synonyms = {}
    filler = Autofiller(catalog, embedder=None) if args.no_embeddings else Autofiller(catalog)
    if args.real:
        if filler.embedder is None:
            p.error("--real требует эмбеддингов")
        catalog = full_catalog = filler.catalog = Catalog.load_real(filler.embedder)
    out_root = args.out or Path("reports") / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    meta = {"каталог": "реальный КТРУ + ОКПД2" if args.real else str(args.catalog or "синтетический"),
            "эмбеддинги": not args.no_embeddings, "без синонимов": args.strip_synonyms}

    all_errors, all_json = [], {}
    for path in args.datasets:
        stes = load_ste(path, args.text_col, args.chars_col, args.code_col)
        if not stes:
            raise SystemExit(f"{path}: не нашёл СТЕ с описанием и характеристиками — укажите --text-col/--chars-col")
        forms = None
        if args.candidates:
            cands = [KtruCandidate.from_dict(d) for d in json.loads(args.candidates.read_text(encoding="utf-8"))]
            forms = {c.code: c for c in cands}
        elif args.forms_from_data:
            forms = forms_from_dataset(stes)  # до выборки: анкета — по всему датасету, как в КТРУ
        if args.limit and args.limit < len(stes):
            stes = random.Random(args.seed).sample(stes, args.limit)
        rep = evaluate(stes, filler, forms, catalog, full_catalog)
        print_report(rep, title=path.name)
        saved = write_artifacts(rep, out_root / path.stem, path.name, meta)
        print(f"\nАртефакты: {saved}/ (report.md, metrics.json, items.jsonl, values.csv)")
        all_errors += rep.errors
        all_json[path.name] = report_json(rep)

    if args.errors:
        with args.errors.open("w", encoding="utf-8") as f:
            for e in all_errors:
                f.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")
    if args.json:
        args.json.write_text(json.dumps(all_json, ensure_ascii=False, indent=2), encoding="utf-8")
