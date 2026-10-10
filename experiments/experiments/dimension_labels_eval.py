"""Диагностический тест Jev: порядок размеров, явно подписанный во входном тексте.

    uv run python experiments/dimension_labels_eval.py

Это искусственные контрольные примеры, не оценка качества на реальных СТЕ.
Запросы строятся текущим ste_to_jev; ожидаемые значения в state не передаются.
"""

import argparse
import itertools
import json
import logging
from pathlib import Path

import ste_to_jev as ste

NAMES = {"В": "Высота", "Ш": "Ширина", "Г": "Глубина"}
NUMBERS = (830, 620, 1320)
LOG_DIR = ste.ROOT / "reports" / "jev_dimension_labels"
SCHEMA = {"code": "diagnostic", "name": "Размеры", "kpgzCharacteristics": [
    {"characteristicId": i, "characteristicName": name, "conditionTypeId": ste.NUMBER, "okeiCode": "003"}
    for i, name in enumerate(NAMES.values(), 1)]}


def cases():
    out = []
    for order in itertools.permutations(NAMES):
        label = " х ".join(order)
        out.append((f"830х620х1320 ({label})", dict(zip(order, NUMBERS)), "подпись после"))
    out.extend([
        ("100х100х100 (В х Ш х Г)", dict.fromkeys(NAMES, 100), "равные числа"),
        ("830х620х1320 мм (В х Ш х Г)", dict(zip(NAMES, NUMBERS)), "единица перед подписью"),
        ("830х620х1320 (В х Ш х Г) мм", dict(zip(NAMES, NUMBERS)), "единица после подписи"),
        ("(В х Ш х Г) 830х620х1320 мм", dict(zip(NAMES, NUMBERS)), "подпись перед"),
        ("830х620х1320 мм", dict.fromkeys(NAMES, ste.NONE), "без подписи"),
    ])
    return out


def make_card(index, text, expected):
    return {"id": index, "name": "Диван", "description": text,
            "production": {"code": "diagnostic", "name": "Диваны"},
            "skuCharacteristics": [
                {"characteristicValueIntValue": value,
                 "productCharacteristicValue": {"productCharacteristicName": NAMES[letter],
                                                "productCharacteristicUnitName": "Миллиметр"}}
                for letter, value in expected.items() if value != ste.NONE]}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--log-dir", type=Path, default=LOG_DIR)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    examples = cases()
    selected = [(make_card(i, text, expected), SCHEMA)
                for i, (text, expected, _) in enumerate(examples, 1)]
    records = ste.check_cards(selected, {"003": "Миллиметр"}, ste.api_key(ste.DEFAULT_ENV),
                              args.log_dir, args.workers)
    results = []
    for (text, expected, group), record in zip(examples, records):
        rows = []
        for row in record["rows"]:
            letter = next(k for k, name in NAMES.items() if name == row["characteristic"])
            answer = row["answer"]
            target = expected[letter]
            q = ste.quantity(answer) if answer != ste.NONE else None
            correct = answer == ste.NONE if target == ste.NONE else bool(
                q and ste.close(ste.convert(q[0], q[1] or "Миллиметр", "Миллиметр"), target))
            rows.append({"характеристика": row["characteristic"], "ожидается": target,
                         "ответ": answer, "confidence": row["confidence"], "верно": correct})
        results.append({"текст": text, "группа": group, "все верно": all(r["верно"] for r in rows), "ответы": rows})
    labeled = [r for r in results if r["группа"] not in ("равные числа", "без подписи")]
    summary = {"model": ste.MODEL, "диагностика, не реальные СТЕ": True,
               "подписанные примеры": {"всего": len(labeled), "все верно": sum(r["все верно"] for r in labeled)},
               "results": results}
    path = args.log_dir / "summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in results:
        print(r["текст"], "|", "; ".join(
            f"{v['характеристика']}: {v['ответ']} (conf {v['confidence']:.2f}, {'верно' if v['верно'] else 'неверно'})"
            for v in r["ответы"]))
    print("Подписанные примеры:", summary["подписанные примеры"])
    print("Лог:", path)


if __name__ == "__main__":
    main()
