"""Проверка Jev на одной СТЕ: заполняет характеристики по схеме КПГЗ и сверяет с карточкой.

    uv run python experiments/ste_to_jev.py 1160210
    uv run python experiments/ste_to_jev.py 1160210 --dry-run   # без запроса к API
    uv run --extra embeddings python experiments/ste_to_jev.py 1160210 --search-category --embedding-device cuda
    uv run --extra embeddings python experiments/ste_to_jev.py 1160210 --search-category --category-neighbors zakupki-reverse/data/ste-details.jsonl.gz --category-jev
    uv run python experiments/ste_to_jev.py --sample 100 --focus dims --log-dir reports/ste_jev_context_dims
    uv run python experiments/ste_to_jev.py --compare reports/ste_jev_focus_base/eval_dims_n100_seed0.json reports/ste_jev_context_dims/eval_dims_n100_seed0.json

state = {товар, категория, описание} из ste-details.jsonl.gz; характеристики карточки в state не идут —
это эталон. questions — схема КПГЗ категории (production.code, kpgz.json.gz ЕАИСТ), по вопросу на
характеристику; числа-кандидаты ищутся в названии и описании (размеры «100х250х320 мм» — по одному
числу), единицы — из okei.csv.gz. Эталон
сопоставляется с характеристикой КПГЗ по названию. Ключ API — TYPESAFE_API_KEY из окружения или .env.

Вердикты:
  верно / неверно    — ответ сравнён со значением в карточке (перечень: совпадение строки или
                        выполнение условия вида «>= 80 < 90»; число: совпадение числа). Единицы
                        переводятся внутри величины: «8 см» сверяется с эталоном в мм как 80;
  частично           — одна строка содержит другую («Быстрозажимной одномуфтовый» / «Быстрозажимной»);
  пропуск            — Jev ответил «не указано», а в карточке значение есть;
  нет эталона        — в карточке такой характеристики нет (числовой 0 тоже считаем пустым), проверить нечем;
  —                  — Noul (только вероятность, что значение указано в тексте).
Для каждой строки отмечено, встречается ли значение из карточки в тексте (`в тексте`): пропуск при
«нет» — вина данных, а не модели.

Лог (state, запрос, ответ, сверка, характеристики карточки вне схемы) пишется в reports/ste_jev_context/<id>.json.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterator

from ktru_autofill.kpgz_search import add_search_arguments, from_args, jev_category
from ktru_autofill.system_one import sdk_caller

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "zakupki-reverse" / "data"
DEFAULT_STE = DATA / "ste-details.jsonl.gz"
DEFAULT_KPGZ = DATA / "kpgz.json.gz"
DEFAULT_OKEI = DATA / "okei.csv.gz"
DEFAULT_ENV = ROOT / ".env"
DEFAULT_LOG_DIR = ROOT / "reports" / "ste_jev_context"

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
MAX_OPTIONS = 255
QUESTIONS_PER_REQUEST = 4  # Контекст вариантов увеличивает токены; отправляем ограниченные пакеты.
NONE = "не указано"

log = logging.getLogger("ste_to_jev")

# Схема КПГЗ (`kpgzCharacteristics`); коды восстановлены по данным, документации нет.
ENUM, NUMBER, TEXT = 8, 5, 1  # conditionTypeId: перечень, число, свободный текст
MATH = {3: ">", 4: "<", 6: "≥", 7: "≤"}  # min/maxMathNotation
# Число. Точка перед ровно тремя цифрами — разделитель тысяч: «3.000 об/мин» = 3000, «1.000.000» = 10⁶;
# с ведущим нулём («0.200») и с запятой («0,5») — дробь.
THOUSANDS = r"[1-9]\d{0,2}(?:\.\d{3})+(?![\d.,])"
NUM = rf"-?(?:{THOUSANDS}|\d+(?:[.,]\d+)?)"
# Кандидат: число или размеры «100х250х320» и единица сразу после. Пробелы — только внутри строки:
# единица не может начаться на следующей строке.
NUMBER_RE = re.compile(rf"(?<![\w.,-]){NUM}(?:[ \t\xa0]*[xх×*][ \t\xa0]*{NUM})*"
                       r"(?:[ \t\xa0]*(?:[^\W\d_]|°)[\w/%°.²³]{0,7}|[ \t\xa0]*%)?")
COND_RE = re.compile(rf"(>=|<=|≥|≤|>|<|=)\s*({NUM})")
OPS = {">=": float.__ge__, "≥": float.__ge__, "<=": float.__le__, "≤": float.__le__,
       ">": float.__gt__, "<": float.__lt__, "=": float.__eq__}

# Величина, множитель к базовой единице, написания: сокращения из текста и полные названия ОКЕИ
# (в карточках СТЕ единица — productCharacteristicUnitName, «Миллиметр», «Литр; кубический дециметр»).
UNITS: list[tuple[str, float, list[str]]] = [
    ("length", 0.001, ["мкм", "микрон", "микрометр"]),
    ("length", 1, ["мм", "миллиметр"]),
    ("length", 10, ["см", "сантиметр"]),
    ("length", 100, ["дм", "дециметр"]),
    ("length", 1000, ["м", "метр"]),
    ("length", 1_000_000, ["км", "километр"]),
    ("mass", 0.001, ["мг", "миллиграмм"]),
    ("mass", 1, ["г", "гр", "грамм"]),
    ("mass", 1000, ["кг", "килограмм"]),
    ("mass", 1_000_000, ["т", "тонна"]),
    ("volume", 1, ["мл", "см3", "миллилитр", "кубический сантиметр"]),
    ("volume", 1000, ["л", "дм3", "литр", "кубический дециметр"]),
    ("volume", 1_000_000, ["м3", "кубический метр"]),
    ("area", 1, ["см2", "квадратный сантиметр"]),
    ("area", 10_000, ["м2", "квадратный метр"]),
    ("power", 1, ["вт", "ватт"]),
    ("power", 1000, ["квт", "киловатт"]),
    ("voltage", 1, ["в", "вольт"]),
    ("time", 1, ["с", "сек", "секунда"]),
    ("time", 60, ["мин", "минута"]),
    ("time", 3600, ["ч", "час"]),
]
UNIT_INDEX = {s: (dim, factor) for dim, factor, spellings in UNITS for s in spellings}
# Полные названия в тексте склоняются («метров», «литра», «минут») — ищем по основе без конечной
# гласной, длинные первыми, чтобы «миллиметров» не ушло в «метр».
UNIT_STEMS = sorted(((re.sub(r"[аяоеиыь]$", "", s), u) for s, u in UNIT_INDEX.items() if len(s) >= 4 or s == "час"),
                    key=lambda x: -len(x[0]))


def norm(s: Any) -> str:
    return re.sub(r"\W+", " ", str(s).lower().replace("ё", "е")).strip()


def to_float(s: str) -> float:
    s = s.strip()
    if re.fullmatch(rf"-?{THOUSANDS}", s):
        return float(s.replace(".", ""))
    return float(s.replace(",", "."))


def find_card(path: Path, ste_id: int) -> dict[str, Any]:
    needle = re.compile(rf'"id":\s*{ste_id}\b')  # дешёвый фильтр до json.loads; совпадение проверяем ниже
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if needle.search(line):
                card = json.loads(line)
                if card.get("id") == ste_id:
                    return card
    sys.exit(f"СТЕ {ste_id} нет в {path}")


def iter_array(path: Path, chunk: int = 1 << 20) -> Iterator[dict[str, Any]]:
    """Элементы JSON-массива из .json.gz по одному — без загрузки файла целиком."""
    dec = json.JSONDecoder()
    buf, pos = "", 0
    with gzip.open(path, "rt", encoding="utf-8") as f:
        while True:
            data = f.read(chunk)
            buf = buf[pos:] + data
            pos = 0
            while True:
                while pos < len(buf) and buf[pos] in " \t\r\n,[]":
                    pos += 1
                try:
                    item, end = dec.raw_decode(buf, pos)
                except json.JSONDecodeError:
                    break  # элемент обрезан концом блока — дочитываем
                yield item
                pos = end
            if not data:
                return


def find_node(path: Path, code: str) -> dict[str, Any]:
    for node in iter_array(path):
        if node.get("code") == code:
            return node
    sys.exit(f"КПГЗ {code} нет в {path}")


def load_okei(path: Path) -> dict[str, str]:
    """Код → наименование из выгрузки Росстата (Razdel;Podrazdel;Code;Name, без заголовка).

    Строки без кода — заголовки разделов; `^` в наименовании — перенос: «Кубический сантиметр;^миллилитр».
    """
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        return {code.strip(): name.replace(";^", " / ").replace("^", " ")
                for _, _, code, name in csv.reader(f, delimiter=";") if code.strip()}


def api_key(env_file: Path) -> str:
    if key := os.environ.get("TYPESAFE_API_KEY"):
        return key
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "TYPESAFE_API_KEY" and value.strip():
                return value.strip().strip("'\"")
    sys.exit(f"Нет TYPESAFE_API_KEY ни в окружении, ни в {env_file}")


def call_jev(request: dict[str, Any], key: str) -> dict[str, Any]:
    req = urllib.request.Request(
        API_URL, data=json.dumps(request, ensure_ascii=False).encode(), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        sys.exit(f"TypeSafe API {e.code}: {e.read().decode(errors='replace')[:500]}")


def unit_of(ch: dict[str, Any], okei: dict[str, str]) -> str | None:
    code = ch.get("okeiCode")
    return okei.get(code, f"ОКЕИ {code}") if code else None


def bounds(ch: dict[str, Any]) -> str | None:
    """«≥ 65», «≥ 120 и ≤ 160» — допустимые значения числовой характеристики."""
    parts = [f"{MATH.get(ch.get(m), '=')} {ch[v]}"
             for v, m in (("value1", "minMathNotation"), ("value2", "maxMathNotation")) if ch.get(v) is not None]
    return " и ".join(parts) or None


def number_spans(text: str) -> Iterator[tuple[str, str | None, int, int]]:
    """Числа-кандидаты с исходными координатами, включая все компоненты «100х250х320 мм»."""
    for m in NUMBER_RE.finditer(text):
        span = m.group().rstrip(" .,")
        nums = list(re.finditer(NUM, span))
        if len(nums) == 1:
            yield span, None, m.start(), m.start() + len(span)
            continue
        unit = span[nums[-1].end():].strip()
        for i, n in enumerate(nums, 1):
            yield (f"{n.group()} {unit}".strip(), f"{i}-е число из «{span}»",
                   m.start() + n.start(), m.start() + n.end())


def number_candidates(text: str) -> dict[str, dict[str, Any]]:
    """Вариант Choice → исходные фрагменты всех его вхождений, без порядка размеров по категории."""
    out: dict[str, dict[str, Any]] = {}
    for value, detail, start, end in number_spans(text):
        candidate = out.setdefault(value, {"фрагменты": []})
        occurrence = {"текст": snippet(text, start, end, pad=60)}
        if detail:
            occurrence["позиция в записи"] = detail
        if occurrence not in candidate["фрагменты"]:
            candidate["фрагменты"].append(occurrence)
    # Голое «650» не удаляем: его контекст может отличаться от контекста «650 Вт».
    return out


def context(ch: dict[str, Any], okei: dict[str, str]) -> dict[str, Any]:
    """Данные вопроса; категория находится только в state."""
    ctx = {"характеристика": ch["characteristicName"].strip()}
    if unit := unit_of(ch, okei):
        ctx["единица"] = unit
    if b := bounds(ch):
        ctx["допустимо"] = b
    return ctx


def is_bins(values: list[str]) -> bool:
    """Перечень из одних числовых условий: «>= 80 < 90», «< 80», «>= 500»."""
    return bool(values) and all(COND_RE.findall(v) and not COND_RE.sub("", v).strip() for v in values)


def presence(ch, okei, what: str) -> dict[str, Any]:
    return {
        "type": "noul",
        "instructions": context(ch, okei) | {
            "question": f"Указано ли в `товар` или `описание` {what} `характеристика`?"},
        "criteria": {"true": "В `товар` или `описание` явно есть это значение или сведения о характеристике",
                     "false": "`товар` и `описание` ничего не говорят об этой характеристике"},
    }


def to_questions(node: dict[str, Any], text: str, okei: dict[str, str]) -> tuple[dict, dict]:
    """Вопрос на характеристику КПГЗ: перечень → Choice по значениям; число → Choice по числам из текста
    (Jev выбирает, код копирует — цифры модель не пишет); текст, число без кандидатов и перечень
    длиннее лимита Choice → Noul «указано ли». К каждому Choice добавлен вариант «не указано».

    Перечень из числовых диапазонов («>= 80 < 90») спрашивается как число: сравнение с границами —
    арифметика, её по документации TypeSafe делает код (`bin_of`), а не модель."""
    questions, fields = {}, {}
    numbers = number_candidates(text)
    for ch in node.get("kpgzCharacteristics") or []:
        qid = f"c{ch['characteristicId']}"
        cond = ch.get("conditionTypeId")
        values = [e["value"] for e in ch.get("characteristicKpgzEnums") or []]
        field = {"characteristic": ch["characteristicName"].strip(), "conditionTypeId": cond,
                 "catalogVariantId": ch.get("catalogVariantId"), "unit": unit_of(ch, okei), "bounds": bounds(ch),
                 "numeric": False, "bins": values if cond == ENUM and is_bins(values) else None}
        if cond == ENUM and 0 < len(values) < MAX_OPTIONS and not field["bins"]:
            questions[qid] = {
                "type": "choice",
                "instructions": context(ch, okei) | {
                    "question": "Какое значение `характеристика` у товара, судя по `товар` и `описание`?"},
                "criteria": dict.fromkeys(values) | {NONE: "В `товар` и `описание` нет значения этой характеристики"},
            }
        elif (cond == NUMBER or field["bins"]) and numbers:
            questions[qid] = {
                "type": "choice",
                "instructions": context(ch, okei) | {
                    "question": "Какое из чисел в `товар` или `описание` — значение `характеристика`?",
                    "правило": "Выбирай по связи числа с характеристикой в исходном тексте. "
                               "Фрагменты вариантов — цитаты, а не назначение числа характеристике. "
                               "Используй явные подписи, включая Ш×Г×В и аналогичные обозначения. "
                               "Не угадывай порядок неподписанных размеров по категории или типичному товару. "
                               "Если связь неоднозначна, выбирай «не указано». "
                               "Не подменяй размер товара размером упаковки или отдельной детали."},
                "criteria": dict(list(numbers.items())[: MAX_OPTIONS - 1])
                            | {NONE: "Нет числа, которое текст позволяет однозначно связать с этой характеристикой"},
            }
            field["numeric"] = True
        else:
            what = "значение" if cond in (ENUM, NUMBER) else "сведения о"
            questions[qid] = presence(ch, okei, what)
            if cond == ENUM and len(values) >= MAX_OPTIONS:
                field["note"] = f"{len(values)} значений — больше лимита Choice, нужен поиск кандидатов в коде"
        fields[qid] = field
    return questions, fields


def clean(text: str | None) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def card_values(card: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Характеристики карточки: нормализованное название → {name, values, unit, type, kpgz_master}."""
    out: dict[str, dict[str, Any]] = {}
    for x in card.get("skuCharacteristics") or []:
        v = x.get("productCharacteristicValue") or {}
        value = next((x[k] for k in ("characteristicValueStringValue", "characteristicValueIntValue",
                                     "characteristicValueDecimalValue", "characteristicValueBoolValue")
                      if x.get(k) not in (None, "")), None)
        if value is None:
            continue
        if isinstance(value, bool):
            value = "Да" if value else "Нет"
        elif isinstance(value, (int, float)) and value == 0:
            continue  # 0 в числовых полях карточек — незаполненное значение, а не эталон
        name = (v.get("productCharacteristicName") or "").strip()
        if not name:
            continue
        item = out.setdefault(norm(name), {"name": name, "values": [], "unit": v.get("productCharacteristicUnitName"),
                                           "type": v.get("productCharacteristicDescriptionTypeName"),
                                           "kpgz_master": bool(v.get("isKpgzMaster"))})
        item["values"].append(value)
    return out


def lookup(gt: dict[str, dict[str, Any]], name: str) -> dict[str, Any] | None:
    """Точное совпадение названия, затем без хвоста «, единица»: «Толщина, мкм» ↔ «Толщина»."""
    return gt.get(norm(name)) or gt.get(norm(name.split(",")[0]))


def numbers_in(value: Any) -> list[float]:
    if isinstance(value, (int, float)):
        return [float(value)]
    return [to_float(m) for m in re.findall(NUM, str(value))]


def parse_unit(written: str | None) -> tuple[str, float] | None:
    """«см», «Миллиметр», «Литр; кубический дециметр», «м[2*]», «литров» → (величина, множитель)."""
    if not written:
        return None
    for part in written.lower().replace("ё", "е").split(";"):
        part = re.sub(r"\[(\d)\*?\]", r"\1", part).replace("²", "2").replace("³", "3")
        part = re.sub(r"[^\w\s]", "", part).strip()
        if part in UNIT_INDEX:
            return UNIT_INDEX[part]
        for stem, unit in UNIT_STEMS:
            if part.startswith(stem):
                return unit
    return None


def convert(x: float, src: str | None, dst: str | None) -> float:
    """Перевод между единицами одной величины; неизвестная или другая величина — значение как есть."""
    a, b = parse_unit(src), parse_unit(dst)
    if a and b and a[0] == b[0]:
        return x * a[1] / b[1]
    return x


def quantity(option: str) -> tuple[float, str] | None:
    """Вариант-число «8 см», «0,2 л» → (8.0, «см»)."""
    m = re.match(rf"\s*({NUM})\s*(.*)", option)
    return (to_float(m.group(1)), m.group(2).strip()) if m else None


def close(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-6 * max(1.0, abs(a), abs(b))


def satisfies(option: str, value: Any, value_unit: str | None, option_unit: str | None) -> bool | None:
    """Выполняет ли значение условие-вариант («>= 80 < 90», «< 80»); None — вариант не условие.

    Значение карточки переводится в единицу характеристики КПГЗ, в которой записаны границы."""
    conds = COND_RE.findall(option)
    if not conds or COND_RE.sub("", option).strip():
        return None
    return any(all(OPS[op](convert(x, value_unit, option_unit), to_float(n)) for op, n in conds)
               for x in numbers_in(value))


def bin_of(choice: str, field: dict[str, Any]) -> str | None:
    """Диапазон перечня КПГЗ, в который попадает выбранное Jev число (в единице характеристики)."""
    q = quantity(choice)
    if q is None or not field["bins"]:
        return None
    x = convert(q[0], q[1] or field["unit"], field["unit"])
    return next((b for b in field["bins"] if satisfies(b, x, field["unit"], field["unit"])), None)


def snippet(text: str, start: int, end: int, pad: int = 50) -> str:
    return ("…" if start > pad else "") + text[max(0, start - pad):end + pad].replace("\n", " ") + ("…" if end + pad < len(text) else "")


# Размеры: сопоставление с эталоном только для отбора и метрик, не для построения вопросов.
DIM_NAMES = {"ширин": "Ширина", "высот": "Высота", "глубин": "Глубина", "длин": "Длина", "толщин": "Толщина"}
DIM_RE = re.compile(rf"(?<![\w.,-])({NUM})\s*[xх×*]\s*({NUM})(?:\s*[xх×*]\s*({NUM}))?\s*(мм|см|м)\b", re.I)
DIM_LABELS = re.compile(r"(ш\s*[xх×*/]\s*[гвд]|[двг]\s*[xх×*/]\s*ш|дшв|шгв|швг|вшг|ширин|высот|глубин|длин|\([швгдл]\))", re.I)

def dim_of(name: str) -> str | None:
    """«Высота шкафа» → «Высота»; «Длина верхней части, не более» — не размер изделия."""
    words = norm(name).split()
    if not words or len(words) > 2:
        return None
    return next((d for st, d in DIM_NAMES.items() if words[0].startswith(st)), None)


def dim_pattern(card: dict[str, Any]) -> str | None:
    """Порядок измерений в первой неподписанной записи размеров карточки: «3: Ширина × Глубина × Высота».
    None — записи нет, она подписана или числа не сопоставляются с характеристиками однозначно."""
    dims: dict[str, tuple[float, str | None]] = {}
    for v in card_values(card).values():
        d = dim_of(v["name"])
        xs = [x for x in v["values"] if isinstance(x, (int, float))]
        if d and xs and d not in dims:
            dims[d] = (float(xs[0]), v["unit"])
    if len(dims) < 2:
        return None
    text = f"{card.get('name')}\n{clean(card.get('description'))}"
    for m in DIM_RE.finditer(text):
        if DIM_LABELS.search(text[max(0, m.start() - 40):m.start()]) or DIM_LABELS.search(text[m.end():m.end() + 15]):
            continue
        order = []
        for g in m.groups()[:3]:
            if g:
                hit = [d for d, (val, u) in dims.items() if close(convert(to_float(g), m.group(4), u), val)]
                order.append(hit[0] if len(hit) == 1 else None)
        if None not in order and len(set(order)) == len(order):
            return f"{len(order)}: " + " × ".join(order)
        return None
    return None


UNLABELED = "число есть, но без подписи"  # «830х620х1320 мм»: какое из чисел высота — по тексту не понять
GENERIC_STEMS = {"колич", "макси", "миним", "значе", "налич", "общая", "общий", "номин", "показ", "диапа", "харак"}


def name_stems(name: str) -> set[str]:
    """Основы значимых слов названия характеристики: «Количество розеток» → {«розет»}."""
    return {w[:5] for w in norm(name).split() if len(w) >= 4} - GENERIC_STEMS


def evidence(truth: dict[str, Any], field: dict[str, Any], question: dict[str, Any], text: str) -> tuple[str | None, str]:
    """Можно ли заполнить характеристику по тексту: (фрагмент-доказательство | None, причина).

    Строго, чтобы метрика считалась только там, где ответ действительно есть во входе Jev:
      перечень — значение карточки совпадает с вариантом КПГЗ и стоит в тексте целым словом/фразой;
      число    — среди кандидатов из текста есть равное эталону (с переводом единиц одной величины),
                 и его подтверждают единицы той же величины или слово из названия характеристики рядом;
      «Да/Нет» и Noul-вопросы текстом не проверить.
    """
    values = truth["values"]
    if question["type"] == "noul":
        return None, "Noul: значение не извлекается"
    if all(norm(v) in ("да", "нет") for v in values):
        return None, "признак Да/Нет"
    if field["numeric"]:
        if any(isinstance(v, str) and re.search(rf"{NUM}\s*[-–—]\s*{NUM}", v) for v in values):
            return None, "в карточке диапазон («10-40»), а вопрос про одно число"
        target = [x for v in values for x in numbers_in(v)]
        stems = name_stems(f"{field['characteristic']} {truth['name']}")
        truth_dim = parse_unit(truth["unit"])
        # Сколько в тексте чисел той же величины: единица подтверждает значение, только если оно одно такое.
        same_dim = sum(1 for c in number_candidates(text)
                       if (q := quantity(c)) and truth_dim and (u := parse_unit(q[1])) and u[0] == truth_dim[0])
        unlabeled = None
        for cand, _, start, end in number_spans(text):
            q = quantity(cand)
            if q is None:
                continue
            a, b = parse_unit(q[1]), parse_unit(truth["unit"])
            if a and b and a[0] != b[0]:
                continue  # «8 бит» не может быть шириной в мм
            x = convert(q[0], q[1] or field["unit"], truth["unit"])
            if not any(close(x, t) for t in target):
                continue
            window = norm(text[max(0, start - 60):end + 25])
            # Подтверждает название рядом или единственное число той же величины.
            # Координаты исходного кандидата не дают спутать «80 мм» с другим «80» в тексте.
            if any(st in window for st in stems) or (a and b and same_dim == 1):
                return snippet(text, start, end), "число есть в тексте"
            unlabeled = unlabeled or snippet(text, start, end)
        if unlabeled:
            return None, f"{UNLABELED}: {unlabeled}"
        return None, "числа нет в тексте"
    options = {norm(o) for o in question["criteria"] if o != NONE}
    hits = [v for v in values if norm(v) in options]
    if not hits:
        return None, "значения карточки нет среди вариантов КПГЗ"
    padded = f" {norm(text)} "
    stems = name_stems(f"{field['characteristic']} {truth['name']}")
    for v in hits:
        if re.fullmatch(NUM, norm(v).replace(" ", ".")):
            # «1», «4» встречаются где угодно — нужен рядом признак из названия характеристики.
            for m in re.finditer(rf"(?<![\w.,]){re.escape(str(v).strip())}(?![\w])", text):
                if any(st in norm(text[max(0, m.start() - 60):m.end() + 25]) for st in stems):
                    return snippet(text, m.start(), m.end()), "значение есть в тексте"
            continue
        if f" {norm(v)} " in padded:
            m = re.search(r"\W*".join(map(re.escape, norm(v).split())), text, re.I)
            return (snippet(text, m.start(), m.end()) if m else norm(v)), "значение есть в тексте"
    return None, "значения нет в тексте"


def judge(answer: dict[str, Any], truth: dict[str, Any] | None, field: dict[str, Any]) -> str:
    if answer["type"] == "noul":
        return "—"
    choice = answer["choice"]
    if truth is None:
        return "нет эталона"
    if choice == NONE:
        return "пропуск"
    values = truth["values"]
    if field["numeric"]:  # выбрано число из текста: «8 см» → в единицу карточки (без единицы — в единицу КПГЗ)
        q = quantity(choice)
        if q is None:
            return "неверно"
        picked = convert(q[0], q[1] or field["unit"], truth["unit"])
        return "верно" if any(close(picked, x) for v in values for x in numbers_in(v)) else "неверно"
    if norm(choice) in {norm(v) for v in values}:
        return "верно"
    value = values[0] if len(values) == 1 else " ".join(map(str, values))
    if (ok := satisfies(choice, value, truth["unit"], field["unit"])) is not None:
        return "верно" if ok else "неверно"
    if any(norm(choice) in norm(v) or norm(v) in norm(choice) for v in values if norm(v)):
        return "частично"
    return "неверно"


def card_state(card: dict[str, Any]) -> dict[str, str]:
    category = (card.get("productionDirectory") or {}).get("name") or (card.get("production") or {}).get("name")
    return {"товар": card.get("name") or card.get("modelName"), "категория": category,
            "описание": clean(card.get("description"))}


def check_card(card: dict[str, Any], node: dict[str, Any], okei: dict[str, str], key: str | None,
               log_dir: Path) -> dict[str, Any]:
    """Запрос к Jev по схеме КПГЗ, сверка с карточкой, лог в log_dir/<id>.json. key=None — без запроса.

    Ответ берётся из лога, если там тот же запрос: повторный прогон не тратит токены."""
    state = card_state(card)
    if node.get("_retrieval"):
        state["категория"] = node["name"]  # Найденная категория, не production/productionDirectory карточки.
    text = f"{state['товар']}\n{state['описание']}"
    pattern = dim_pattern(card)
    questions, fields = to_questions(node, text, okei)
    request = {"model": MODEL, "state": state, "questions": questions}
    gt = card_values(card)
    path = log_dir / f"{card['id']}.json"

    response: dict[str, Any] = {}
    if path.exists() and (old := json.loads(path.read_text(encoding="utf-8"))).get("request") == request and old.get("response"):
        response = old["response"]
        log.info("СТЕ %s: ответ из %s", card["id"], path.name)
    elif key:
        t = time.perf_counter()
        response = {"answers": {}, "usage": {"input_tokens": 0, "output_tokens": 0}}
        items = list(questions.items())
        for offset in range(0, len(items), QUESTIONS_PER_REQUEST):
            part = call_jev(request | {"questions": dict(items[offset:offset + QUESTIONS_PER_REQUEST])}, key)
            response["answers"].update(part["answers"])
            for name in response["usage"]:
                response["usage"][name] += part["usage"][name]
        log.info("СТЕ %s: Jev %.1f с, токены %s", card["id"], time.perf_counter() - t, response.get("usage"))

    rows, used = [], set()
    for qid, field in fields.items():
        truth = lookup(gt, field["characteristic"])
        if truth:
            used.add(norm(truth["name"]))
        ev, reason = evidence(truth, field, questions[qid], text) if truth else (None, "нет в карточке")
        row = {"qid": qid, "characteristic": field["characteristic"], "type": questions[qid]["type"],
               "kind": "диапазон" if field["bins"] else "число" if field["numeric"] else
                       "перечень" if questions[qid]["type"] == "choice" else "noul",
               "unit": field["unit"], "bounds": field["bounds"], "catalogVariantId": field["catalogVariantId"],
               "dimension": dim_of(field["characteristic"]) is not None,
               "dimension_in_pattern": bool(pattern and dim_of(field["characteristic"]) in pattern.split(": ")[1].split(" × ")),
               "dimension_pattern": pattern,
               "truth": truth and truth["values"], "truth_unit": truth and truth["unit"],
               "truth_kpgz_master": truth and truth["kpgz_master"],
               "truth_in_text": ev is not None, "evidence": ev, "evidence_reason": reason}
        answer = response.get("answers", {}).get(qid)
        if answer:
            row |= {"answer": answer.get("choice", answer.get("noul")), "confidence": answer.get("confidence"),
                    "probabilities": answer.get("probabilities"), "verdict": judge(answer, truth, field)}
            if field["bins"] and answer.get("choice") not in (None, NONE):
                row["bin"] = bin_of(answer["choice"], field)
        rows.append(row)
    outside = [{"name": v["name"], "values": v["values"], "unit": v["unit"], "kpgz_master": v["kpgz_master"]}
               for k, v in gt.items() if k not in used]
    verdicts: dict[str, int] = {}
    for r in rows:
        if "verdict" in r:
            verdicts[r["verdict"]] = verdicts.get(r["verdict"], 0) + 1

    production = card.get("production") or {}
    record = {"ste": {"id": card["id"], "name": card.get("name"), "category": state["категория"],
                      "productionDirectoryId": card.get("productionDirectoryId"), "kpgz": production.get("code"),
                      "okpd2": (production.get("okpd") or {}).get("code")},
              "kpgz": {"code": node["code"], "name": node["name"]}, "request": request,
              "response": response,
              "rows": rows, "card_outside_schema": outside, "verdicts": verdicts}
    if node.get("_retrieval"):
        record["category_search"] = node["_retrieval"] | {
            "expected_code": production.get("code"), "top1_correct": node["code"] == production.get("code"),
            "topk_correct": any(n["code"] == production.get("code") for n in node["_retrieval"]["top_k"])}
    log_dir.mkdir(parents=True, exist_ok=True)
    if response or not path.exists():
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def print_card(record: dict[str, Any]) -> None:
    print(f"\nСТЕ {record['ste']['id']}: {record['ste']['name']}\nКПГЗ {record['kpgz']['code']} {record['kpgz']['name']}\n")
    print(f"{'характеристика':32} {'ответ Jev':22} {'conf':>5}  {'в карточке':24} {'в тексте':8} вердикт")
    for r in record["rows"]:
        ans = r.get("answer")
        ans = f"{ans:.2f}" if isinstance(ans, float) else str(ans if ans is not None else "")
        if r.get("bin"):
            ans = f"{ans} → {r['bin']}"
        conf = f"{r['confidence']:.2f}" if r.get("confidence") is not None else ""
        truth = "; ".join(map(str, r["truth"])) if r["truth"] else ""
        seen = "" if r["truth"] is None else "да" if r["truth_in_text"] else "нет"
        print(f"{r['characteristic'][:32]:32} {ans[:22]:22} {conf:>5}  {truth[:24]:24} {seen:8} {r.get('verdict', '')}")
    if record["card_outside_schema"]:
        print(f"\nВ карточке, но нет в схеме КПГЗ ({len(record['card_outside_schema'])}): "
              + ", ".join(o["name"] for o in record["card_outside_schema"]))
    print("\nИтог:", record["verdicts"] or "запрос не отправлялся (--dry-run)")


def schema_names(path: Path) -> dict[str, set[str]]:
    """КПГЗ со схемой → нормализованные названия характеристик (с вариантом без «, единица»)."""
    out = {}
    for node in iter_array(path):
        if chars := node.get("kpgzCharacteristics"):
            names = {n["characteristicName"] for n in chars}
            out[node["code"]] = {norm(n) for n in names} | {norm(n.split(",")[0]) for n in names}
    return out


PER_GROUP = 6


def select_cards(args: argparse.Namespace) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Выборка для замера: СТЕ с описанием и характеристиками, у которых ≥ min_pairs значений можно
    взять из текста (строгая проверка `evidence`). Для dims — минимум min_pairs размеров из
    неподписанной записи. Группы КПГЗ (три сегмента) обходятся по кругу для разнообразия,
    порядок карточек детерминирован по хешу (seed, id)."""
    names = schema_names(args.kpgz)
    log.info("КПГЗ со схемой: %d", len(names))
    best: dict[str, list[tuple[str, str]]] = {}  # группа КПГЗ → до PER_GROUP (ключ, строка) с наименьшими ключами
    seen = 0
    with gzip.open(args.ste, "rt", encoding="utf-8") as f:
        for line in f:
            card = json.loads(line)
            code = (card.get("production") or {}).get("code")
            if card.get("_fetchError") or code not in names or not card.get("description"):
                continue
            if args.focus == "dims":  # только карточки с неподписанной записью размеров
                if dim_pattern(card):
                    seen += 1
                    k = hashlib.sha1(f"{args.seed}:{card['id']}".encode()).hexdigest()
                    kept = best.setdefault(".".join(code.split(".")[:3]), [])
                    kept.append((k, line))
                continue
            gt = card_values(card)
            text = f"{card.get('name')}\n{clean(card['description'])}"
            # Дешёвый фильтр: ≥ min_pairs характеристик из схемы, значение которых похоже есть в тексте.
            loose = sum(1 for k, v in gt.items() if (k in names[code] or norm(v["name"].split(",")[0]) in names[code])
                        and any(str(x).replace(".0", "") in text if isinstance(x, (int, float)) else norm(x) in norm(text)
                                for x in v["values"] if x not in ("Да", "Нет")))
            if loose < args.min_pairs:
                continue
            seen += 1
            k = hashlib.sha1(f"{args.seed}:{card['id']}".encode()).hexdigest()
            group = ".".join(code.split(".")[:3])  # иначе выборку забивают лекарства: у них много КПГЗ
            kept = best.setdefault(group, [])
            if len(kept) < PER_GROUP or k < kept[-1][0]:
                kept.append((k, line))
                kept.sort()
                del kept[PER_GROUP:]
    log.info("кандидатов после грубого фильтра: %d СТЕ, %d групп КПГЗ", seen, len(best))
    # По кругу: сначала лучшая СТЕ каждой группы, потом вторая и т. д. — разнообразие сохраняется.
    for kept in best.values():
        kept.sort()
    groups = sorted(best.values(), key=lambda kept: kept[0][0])
    rounds = max((len(kept) for kept in groups), default=0)
    order = [json.loads(kept[i][1]) for i in range(rounds) for kept in groups if i < len(kept)]
    need = {c["production"]["code"] for c in order}
    nodes = {n["code"]: n for n in iter_array(args.kpgz) if n.get("code") in need}
    okei = load_okei(args.okei)
    picked = []
    for card in order:
        code = card["production"]["code"]
        text = f"{card.get('name')}\n{clean(card['description'])}"
        questions, fields = to_questions(nodes[code], text, okei)
        gt = card_values(card)
        if args.focus == "dims":
            pattern_dims = dim_pattern(card).split(": ")[1].split(" × ")
            strict = sum(1 for qid, fd in fields.items()
                         if dim_of(fd["characteristic"]) in pattern_dims and questions[qid]["type"] == "choice"
                         and (t := lookup(gt, fd["characteristic"]))
                         and ((ev := evidence(t, fd, questions[qid], text))[0] or ev[1].startswith(UNLABELED)))
        else:
            strict = sum(1 for qid, fd in fields.items()
                         if (t := lookup(gt, fd["characteristic"])) and evidence(t, fd, questions[qid], text)[0])
        if strict >= args.min_pairs and card["id"] not in {c["id"] for c, _ in picked}:
            picked.append((card, nodes[code]))
            if len(picked) == args.sample:
                break
    log.info("в выборке %d СТЕ (≥ %d характеристик подтверждены текстом)", len(picked), args.min_pairs)
    return picked


def metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Метрики по парам (СТЕ, характеристика), где эталон подтверждён текстом и вопрос — Choice."""
    pairs = [r | {"ste": rec["ste"]["id"]} for rec in records for r in rec["rows"]
             if r["truth_in_text"] and r.get("verdict") not in (None, "—")]
    out: dict[str, Any] = {"pairs": len(pairs), "ste": len(records)}

    def block(rs):
        n = len(rs)
        c = {v: sum(r["verdict"] == v for r in rs) for v in ("верно", "частично", "неверно", "пропуск")}
        answered = [r for r in rs if r["answer"] != NONE]
        return {"n": n, **c, "точность": round(c["верно"] / n, 3) if n else None,
                "точность_с_частичными": round((c["верно"] + c["частично"]) / n, 3) if n else None,
                "ответов": len(answered), "доля ответов": round(len(answered) / n, 3) if n else None,
                "точность ответов": round(c["верно"] / len(answered), 3) if answered else None}

    out["всего"] = block(pairs)
    out["по типу"] = {k: block([r for r in pairs if r["kind"] == k]) for k in ("перечень", "число", "диапазон")
                      if any(r["kind"] == k for r in pairs)}
    out["по уверенности"] = {}
    for t in (0.5, 0.8, 0.9):
        confident = [r for r in pairs if r["answer"] != NONE and (r["confidence"] or 0) >= t]
        ok = sum(r["verdict"] == "верно" for r in confident)
        out["по уверенности"][f">= {t}"] = {"доля ответов": round(len(confident) / len(pairs), 3) if pairs else None,
                                            "точность": round(ok / len(confident), 3) if confident else None}
    # Контроль выдумывания: значение в карточке есть, но в тексте его нет — правильный ответ скорее «не указано».
    absent = [r for rec in records for r in rec["rows"]
              if r["truth"] and not r["truth_in_text"] and r["type"] == "choice" and "answer" in r
              and r["evidence_reason"] in ("значения нет в тексте", "числа нет в тексте")]
    out["нет в тексте"] = {"n": len(absent), "ответил значением": sum(r["answer"] != NONE for r in absent),
                           "из них верно": sum(r["verdict"] == "верно" for r in absent)}
    out["размеры"] = block([r for r in pairs if r["dimension"]])
    out["не размеры"] = block([r for r in pairs if not r["dimension"]])
    # Число в тексте есть, но без подписи: человек тоже не скажет, где высота, а где ширина. Не в основной метрике.
    out["без подписи"] = block([r | {"ste": rec["ste"]["id"]} for rec in records for r in rec["rows"]
                               if r["evidence_reason"].startswith(UNLABELED) and r.get("verdict") not in (None, "—")])
    unlabeled_dims = [r | {"ste": rec["ste"]["id"]} for rec in records for r in rec["rows"]
                      if r["dimension_in_pattern"] and r.get("verdict") not in (None, "—", "нет эталона")]
    out["неподписанные размеры"] = block(unlabeled_dims)
    out["неподписанные размеры"]["СТЕ"] = len({r["ste"] for r in unlabeled_dims})
    error_keys = ("ste", "characteristic", "kind", "answer", "confidence", "truth", "truth_unit", "verdict", "evidence")
    out["ошибки неподписанных размеров"] = [{k: r[k] for k in error_keys}
                                           for r in unlabeled_dims if r["verdict"] != "верно"]
    out["ошибки"] = [{k: r[k] for k in error_keys} for r in pairs if r["verdict"] != "верно"]
    return out


def compare_evals(base_path: Path, candidate_path: Path) -> dict[str, Any]:
    """Парное сравнение по одним СТЕ и вопросам, включая неподписанные размеры вне основной метрики."""
    base, candidate = [json.loads(p.read_text(encoding="utf-8")) for p in (base_path, candidate_path)]
    if base["ste_ids"] != candidate["ste_ids"]:
        raise ValueError("Выборки СТЕ различаются: парное сравнение некорректно")
    b = {(r["ste"], r["qid"]): r for r in base["pairs"]}
    h = {(r["ste"], r["qid"]): r for r in candidate["pairs"]}
    if b.keys() != h.keys():
        raise ValueError("Наборы вопросов различаются")
    for key in b:
        for field in ("characteristic", "truth", "truth_unit", "dimension_pattern", "truth_in_text"):
            if b[key][field] != h[key][field]:
                raise ValueError(f"Эталон или фильтры различаются: {key}, {field}")
        if "verdict" not in b[key] or "verdict" not in h[key]:
            raise ValueError("Прогон не содержит ответов; --dry-run сравнивать нельзя")
    changes = []
    for key, before in b.items():
        after = h[key]
        if before["dimension_in_pattern"] and before.get("verdict") != after.get("verdict"):
            changes.append({"ste": key[0], "qid": key[1], "characteristic": before["characteristic"],
                            "truth": before["truth"], "base": {k: before[k] for k in ("answer", "confidence", "verdict")},
                            "candidate": {k: after[k] for k in ("answer", "confidence", "verdict")}})
    corrected = sum(r["base"]["verdict"] != "верно" and r["candidate"]["verdict"] == "верно" for r in changes)
    broken = sum(r["base"]["verdict"] == "верно" and r["candidate"]["verdict"] != "верно" for r in changes)
    report = {"ste": len(base["ste_ids"]), "sources": {"base": str(base_path), "candidate": str(candidate_path)},
              "base": base["metrics"]["неподписанные размеры"],
              "candidate": candidate["metrics"]["неподписанные размеры"],
              "подтверждено текстом": {"base": base["metrics"]["всего"], "candidate": candidate["metrics"]["всего"]},
              "исправлено": corrected, "испорчено": broken,
              "ошибочный ответ → отказ": sum(r["base"]["verdict"] == "неверно"
                                              and r["candidate"]["verdict"] == "пропуск" for r in changes),
              "верный ответ → отказ": sum(r["base"]["verdict"] == "верно"
                                          and r["candidate"]["verdict"] == "пропуск" for r in changes),
              "изменения": changes,
              "примечание": "Совпадение неподписанных размеров с карточкой не доказывает однозначность извлечения. "
                             "Нужно учитывать отказы от ответа и точность среди выданных ответов. "
                             "Выборка отобрана по наличию сопоставимого эталона."}
    path = candidate_path.parent / f"compare_{candidate_path.stem}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "изменения"}, ensure_ascii=False, indent=2))
    log.info("парное сравнение: %s", path)
    return report


def find_categories(cards: list[dict[str, Any]], args: argparse.Namespace, key: str | None) -> list[dict[str, Any]]:
    """КПГЗ по тексту карточек: ближайшая схема или голос соседей-СТЕ; с --category-jev выбор среди
    категорий соседей делает Jev. Сами карточки исключены из корпуса соседей.
    Ответ Jev — в log_dir/<id>.category.json; при том же запросе API не вызывается."""
    texts = [f"{c.get('name') or c.get('modelName') or ''}\n{clean(c.get('description'))}" for c in cards]
    exclude = [{"id": c["id"], "name": c.get("name") or c.get("modelName"), "text": t} for c, t in zip(cards, texts)]
    search = from_args(iter_array(args.kpgz), args, exclude)
    rankings = search.search_many(texts, args.category_top_k)
    if not args.category_jev:
        return [search.node(ranked) for ranked in rankings]
    args.log_dir.mkdir(parents=True, exist_ok=True)
    call = None
    if key:  # без ключа (--dry-run) — только запросы; локальному серверу ключ не нужен
        call = sdk_caller(base_url=args.jev_base_url, api_key="local" if args.jev_base_url else key)

    def choose(item):
        card, ranked = item
        path = args.log_dir / f"{card['id']}.category.json"
        cached = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        result = jev_category(search, card_state(card), ranked, call, cached, model=args.jev_model)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if result["response"] is None:  # dry run: категория по голосу соседей
            return search.node(ranked)
        node = search.node(result["top_k"], "ste_neighbors+jev")
        node["_retrieval"]["jev"] = {"confidence": result["confidence"], "usage": result["response"].get("usage")}
        return node

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        return list(pool.map(choose, zip(cards, rankings)))


def check_cards(selected: list[tuple[dict[str, Any], dict[str, Any]]], okei: dict[str, str],
                key: str | None, log_dir: Path, workers: int) -> list[dict[str, Any]]:
    """Параллельные проверки СТЕ; порядок результатов совпадает с порядком выборки."""
    if workers <= 0:
        raise ValueError("workers должен быть положительным")
    log.info("проверка %d СТЕ, параллельных работников: %d", len(selected), workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda item: check_card(item[0], item[1], okei, key, log_dir), selected))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    add_search_arguments(ap)
    ap.add_argument("ste_id", type=int, nargs="?", help="одна СТЕ; без него нужен --sample")
    ap.add_argument("--sample", type=int, help="замер на N СТЕ, у которых значения характеристик есть в тексте")
    ap.add_argument("--min-pairs", type=int, default=2, help="минимум подтверждённых текстом характеристик на СТЕ")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8, help="число параллельно проверяемых СТЕ (по умолчанию 8)")
    ap.add_argument("--focus", choices=["all", "dims"], default="all",
                    help="dims — только СТЕ с неподписанной записью размеров («830х620х1320 мм»)")
    ap.add_argument("--dry-run", action="store_true", help="собрать запрос и эталон, но не звать API")
    ap.add_argument("--ste", type=Path, default=DEFAULT_STE)
    ap.add_argument("--kpgz", type=Path, default=DEFAULT_KPGZ)
    ap.add_argument("--okei", type=Path, default=DEFAULT_OKEI)
    ap.add_argument("--env", type=Path, default=DEFAULT_ENV)
    ap.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    ap.add_argument("--compare", type=Path, nargs=2, metavar=("BASE", "CANDIDATE"),
                    help="сравнить два сохранённых замера без вызовов API")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    if args.compare:
        compare_evals(*args.compare)
        return
    if (args.ste_id is None) == (args.sample is None):
        ap.error("укажите либо ste_id, либо --sample N")
    if args.sample is not None and args.sample <= 0:
        ap.error("--sample должен быть положительным")
    if args.min_pairs <= 0:
        ap.error("--min-pairs должен быть положительным")
    if args.workers <= 0:
        ap.error("--workers должен быть положительным")
    if (args.category_neighbors or args.category_jev) and not args.search_category:
        ap.error("--category-neighbors и --category-jev работают с --search-category")
    if args.category_jev and not args.category_neighbors:
        ap.error("--category-jev выбирает среди категорий соседей: нужен --category-neighbors")
    if args.neighbors_k <= 0:
        ap.error("--neighbors-k должен быть положительным")
    if args.search_category and args.log_dir == DEFAULT_LOG_DIR:
        args.log_dir = ROOT / "reports" / "ste_jev_search"
    key = None if args.dry_run else api_key(args.env)
    if args.sample:
        okei = load_okei(args.okei)
        selected = select_cards(args)
        if not selected:
            sys.exit("Ни одна СТЕ не прошла фильтры выборки")
        if args.search_category:
            nodes = find_categories([card for card, _ in selected], args, key)
            selected = [(card, node) for (card, _), node in zip(selected, nodes)]
        records = check_cards(selected, okei, key, args.log_dir, args.workers)
        m = metrics(records)
        if args.search_category:
            m["поиск категории"] = {"n": len(records),
                "top1": sum(r["category_search"]["top1_correct"] for r in records),
                "topk": sum(r["category_search"]["topk_correct"] for r in records),
                "k": args.category_top_k}
            log.info("Поиск категории: %s", m["поиск категории"])
        path = args.log_dir / f"eval_{args.focus}_n{args.sample}_seed{args.seed}.json"
        path.write_text(json.dumps({"ste_ids": [r["ste"]["id"] for r in records], "metrics": m,
                                    "config": {"focus": args.focus, "sample": args.sample, "seed": args.seed,
                                               "min_pairs": args.min_pairs, "workers": args.workers,
                                               "method": "числа с исходным контекстом",
                                               "search_category": args.search_category,
                                               "embedding_model": args.embedding_model if args.search_category else None,
                                               "category_neighbors": args.search_category and bool(args.category_neighbors),
                                               "neighbors_k": args.neighbors_k if args.category_neighbors else None,
                                               "category_jev": args.category_jev},
                                    "category_search": [r["category_search"] | {"ste": r["ste"]["id"]}
                                                        for r in records if "category_search" in r],
                                    "pairs": [r | {"ste": rec["ste"]["id"]} for rec in records for r in rec["rows"]]},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in m.items() if not k.startswith("ошибки")}, ensure_ascii=False, indent=2))
        print(f"\nОшибки ({len(m['ошибки'])}):")
        for e in m["ошибки"]:
            print(f"  {e['ste']} {e['characteristic'][:30]:30} [{e['kind']}] Jev: {str(e['answer'])[:25]:25} "
                  f"conf {e['confidence'] or 0:.2f} | карточка: {'; '.join(map(str, e['truth']))[:25]} | {e['verdict']}")
        log.info("лог замера: %s", path.relative_to(ROOT) if path.is_relative_to(ROOT) else path)
        return

    card = find_card(args.ste, args.ste_id)
    if card.get("_fetchError"):
        sys.exit(f"СТЕ {args.ste_id}: карточка не скачалась ({card['_fetchError']})")
    production = card.get("production") or {}
    if args.search_category:
        node = find_categories([card], args, key)[0]
        log.info("Найден КПГЗ %s %s; в карточке %s", node["code"], node["name"], production.get("code"))
    else:
        if not production.get("code"):
            sys.exit("У карточки нет КПГЗ — схему взять неоткуда")
        node = find_node(args.kpgz, production["code"])
    if not node.get("kpgzCharacteristics"):
        sys.exit(f"У КПГЗ {node['code']} «{node['name']}» нет схемы характеристик")
    if not card.get("description"):
        log.warning("у карточки нет описания — Jev видит только название")
    record = check_card(card, node, load_okei(args.okei), key, args.log_dir)
    print_card(record)
    log.info("лог: %s", (args.log_dir / f"{card['id']}.json"))


if __name__ == "__main__":
    main()
