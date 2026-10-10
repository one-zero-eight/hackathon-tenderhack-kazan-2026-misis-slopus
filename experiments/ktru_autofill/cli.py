"""ktru-autofill "бумага для принтера А4 80 грамм 500 листов" [-c candidates.json] [-d] [-i]

Без -c текст сопоставляется со всем каталогом КТРУ (data-archived/ktru_catalog.json).
-i — диалог: каждая строка дописывается к описанию, видно, как сужаются коды.
"""

from __future__ import annotations

import argparse
import json
import sys

from .autofill import Autofiller, Result
from .catalog import Catalog


def _short(r: Result) -> str:
    lines = [f"товар: {r.to_dict()['товар']}"]
    for k, v in r.values.items():
        if v is not None:
            lines.append(f"  {k}: {v['значение']:g} {v['единица'] or ''}" if isinstance(v, dict) else f"  {k}: {v}")
    lines.append("сужение: " + " → ".join(f"{s.шаг} [{s.осталось}]" for s in r.steps))
    if r.code:
        lines.append(f"код КТРУ: {r.code}")
    elif r.question:
        opts = ", ".join(f"{k} ({n})" for k, n in list(r.question["варианты"].items())[:8])
        lines.append(f"осталось {len(r.candidates)} кодов; уточните «{r.question['характеристика']}»: {opts}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="ktru-autofill", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("text", nargs="*", help="текст позиции; без аргумента — по строке из stdin")
    p.add_argument("-c", "--candidates", help="JSON со списком кандидатов КТРУ (шаг 2) вместо каталога")
    p.add_argument("--catalog", help="другой каталог КТРУ (JSON)")
    p.add_argument("--real", action="store_true",
                   help="настоящий КТРУ (data-archived/ktru_real.json) с поиском товара по дереву ОКПД2")
    p.add_argument("-d", "--details", action="store_true", help="фрагменты, методы, опечатки, коды-кандидаты")
    p.add_argument("-i", "--interactive", action="store_true", help="диалог с пошаговым сужением")
    p.add_argument("--no-embeddings", action="store_true", help="только rapidfuzz, без модели")
    args = p.parse_args(argv)

    catalog = Catalog.load(args.catalog) if args.catalog else None
    filler = Autofiller(catalog, embedder=None) if args.no_embeddings else Autofiller(catalog)
    if args.real:
        if filler.embedder is None:
            p.error("--real требует эмбеддингов (дерево ОКПД2 оценивается ими)")
        filler.catalog = Catalog.load_real(filler.embedder)
    candidates = None
    if args.candidates:
        with open(args.candidates, encoding="utf-8") as f:
            candidates = json.load(f)

    if args.interactive:
        text = " ".join(args.text)
        print("Опишите товар; каждая строка дополняет описание. Пустая строка — заново, Ctrl+D — выход.")
        while True:
            if text:
                print(_short(filler.fill(text, candidates)) + "\n")
            try:
                line = input("> ").strip()
            except EOFError:
                break
            text = f"{text} {line}".strip() if line else ""
        return

    lines = [" ".join(args.text)] if args.text else [line.strip() for line in sys.stdin if line.strip()]
    for line in lines:
        result = filler.fill(line, candidates).to_dict(details=args.details)
        print(json.dumps(result, ensure_ascii=False, indent=2))
