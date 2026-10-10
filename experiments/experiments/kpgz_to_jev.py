"""Категория КПГЗ (ЕАИСТ) → запрос к Jev (TypeSafe System One): по вопросу на характеристику.

    uv run python experiments/kpgz_to_jev.py 01.15.01.01.01
    uv run python experiments/kpgz_to_jev.py 01.15.01.01.01 --text "бумага А4 80 г/м2, белизна 146%"
    uv run --extra embeddings python experiments/kpgz_to_jev.py --search-category --embedding-device cuda --text "бумага А4 80 г/м2, белизна 146%"

Печатает тело POST https://api.typesafe.ai/v1/systemone и рядом `fields` — что значит каждый ответ
(код по нему собирает значения характеристик). С --call отправляет запрос и печатает ответы;
ключ — TYPESAFE_API_KEY из окружения или из .env в корне проекта.

Перевод характеристики (`kpgzCharacteristics`, коды восстановлены по данным, документации нет):
  перечень (conditionTypeId=8)  → Choice по допустимым значениям + «не указано»;
  число    (conditionTypeId=5)  → с --text: Choice по числам, найденным в тексте регуляркой, + «не указано»
                                  (Jev выбирает, код копирует значение — цифры модель не пишет);
                                  без текста: Noul «указано ли значение»;
  текст    (conditionTypeId=1)  → Noul «упомянута ли характеристика».
Перечни длиннее лимита Choice (255 вариантов) тоже сводятся к Noul.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import logging
import re
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator

from ktru_autofill.kpgz_search import add_search_arguments, from_args

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KPGZ = ROOT / "zakupki-reverse" / "data" / "kpgz.json.gz"
DEFAULT_OKEI = ROOT / "zakupki-reverse" / "data" / "okei.csv.gz"

DEFAULT_ENV = ROOT / ".env"
API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
MAX_OPTIONS = 255
NONE = "не указано"

ENUM, NUMBER, TEXT = 8, 5, 1
MATH = {3: ">", 4: "<", 6: "≥", 7: "≤"}
# Число с необязательной единицей сразу после: «80 г/м2», «0,5 л», «210х297 мм»; «А4», «Т-94» — не числа.
NUMBER_RE = re.compile(r"(?<![\w.,-])-?\d+(?:[.,]\d+)?(?:\s*[xх×*]\s*\d+(?:[.,]\d+)?)*(?:\s*(?:[^\W\d_]|°)[\w/%°.²³]{0,7}|\s*%)?")


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


def number_candidates(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for m in NUMBER_RE.finditer(text):
        seen.setdefault(m.group().strip(" .,"), None)
    return list(seen)


def context(node: dict[str, Any], ch: dict[str, Any], okei: dict[str, str]) -> dict[str, Any]:
    ctx = {"категория": node["name"].capitalize(), "характеристика": ch["characteristicName"].strip()}
    if unit := unit_of(ch, okei):
        ctx["единица"] = unit
    if b := bounds(ch):
        ctx["допустимо"] = b
    return ctx


def presence(node, ch, okei, what: str) -> dict[str, Any]:
    return {
        "type": "noul",
        "instructions": context(node, ch, okei) | {
            "question": f"Указано ли в описании товара {what} `характеристика` для товара из `категория`?"},
        "criteria": {"true": "В тексте явно есть это значение или сведения о характеристике",
                     "false": "Текст ничего не говорит об этой характеристике"},
    }


def to_questions(node: dict[str, Any], text: str | None, okei: dict[str, str]) -> tuple[dict, dict]:
    questions, fields = {}, {}
    numbers = number_candidates(text) if text else []
    for ch in node.get("kpgzCharacteristics") or []:
        qid = f"c{ch['characteristicId']}"
        cond = ch.get("conditionTypeId")
        values = [e["value"] for e in ch.get("characteristicKpgzEnums") or []]
        field = {"characteristic": ch["characteristicName"].strip(), "conditionTypeId": cond,
                 "catalogVariantId": ch.get("catalogVariantId"), "unit": unit_of(ch, okei), "bounds": bounds(ch)}
        if cond == ENUM and 0 < len(values) < MAX_OPTIONS:
            questions[qid] = {
                "type": "choice",
                "instructions": context(node, ch, okei) | {
                    "question": "Какое значение `характеристика` у товара из `категория` по его описанию?"},
                "criteria": dict.fromkeys(values) | {NONE: "В описании нет значения этой характеристики"},
            }
            field["answer"] = "choice: значение или «не указано»"
        elif cond == NUMBER and numbers:
            questions[qid] = {
                "type": "choice",
                "instructions": context(node, ch, okei) | {
                    "question": "Какое из чисел в описании товара — значение `характеристика`?"},
                "criteria": dict.fromkeys(numbers[: MAX_OPTIONS - 1]) | {NONE: "Ни одно число не относится к этой характеристике"},
            }
            field["answer"] = "choice: фрагмент текста с числом (код разбирает число) или «не указано»"
        else:
            what = "значение" if cond in (ENUM, NUMBER) else "сведения о"
            questions[qid] = presence(node, ch, okei, what)
            field["answer"] = "noul: вероятность, что характеристика указана"
            if cond == ENUM and len(values) >= MAX_OPTIONS:
                field["note"] = f"{len(values)} значений — больше лимита Choice, нужен поиск кандидатов в коде"
        fields[qid] = field
    return questions, fields


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("code", nargs="?", help="код КПГЗ; с --search-category не нужен")
    add_search_arguments(ap)
    ap.add_argument("--text", help="описание товара — пойдёт в state и даст кандидатов для чисел")
    ap.add_argument("--kpgz", type=Path, default=DEFAULT_KPGZ)
    ap.add_argument("--okei", type=Path, default=DEFAULT_OKEI)
    ap.add_argument("--call", action="store_true", help="отправить запрос в TypeSafe (нужен --text)")
    ap.add_argument("--env", type=Path, default=DEFAULT_ENV)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    if args.call and not args.text:
        ap.error("--call без --text бессмыслен: state пустой")

    if args.search_category:
        if args.code or not args.text:
            ap.error("--search-category требует --text и не принимает код КПГЗ")
        search = from_args(iter_array(args.kpgz), args)
        node = search.node(search.search(args.text, args.category_top_k))
    else:
        if not args.code:
            ap.error("укажите код КПГЗ или --search-category --text")
        node = find_node(args.kpgz, args.code)
    if not node.get("kpgzCharacteristics"):
        sys.exit(f"У {args.code} «{node['name']}» нет характеристик (схема обычно только у листьев)")
    questions, fields = to_questions(node, args.text, load_okei(args.okei))
    request = {"model": MODEL, "state": {"описание_товара": args.text or "<текст заявки>"}, "questions": questions}
    out = {"kpgz": {"code": node["code"], "name": node["name"]}, "request": request, "fields": fields}
    if args.call:
        resp = call_jev(request, api_key(args.env))
        out = {"kpgz": out["kpgz"], "model": resp.get("model"), "usage": resp.get("usage"), "answers": {
            qid: {"characteristic": fields[qid]["characteristic"], "unit": fields[qid]["unit"]} | answer
            for qid, answer in resp.get("answers", {}).items()}}
    if node.get("_retrieval"):
        out["category_search"] = node["_retrieval"]
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    print()


if __name__ == "__main__":
    main()
