"""Замер качества на ручном наборе: после прогона в reports/pytest/ остаются артефакты
(report.md, metrics.json, items.jsonl, values.csv) — найдено vs ожидалось по каждой СТЕ.

Пороги — защита от регрессии, а не цель: выдуманных значений быть не должно вовсе.
"""

from pathlib import Path

from ktru_autofill import Autofiller
from ktru_autofill.catalog import Catalog
from ktru_autofill.evaluate import evaluate, load_ste, write_artifacts

ROOT = Path(__file__).resolve().parents[1]


def test_quality_handwritten_and_save_artifacts():
    catalog = Catalog.load()
    stes = load_ste(ROOT / "data-archived" / "test_handwritten.jsonl")
    rep = evaluate(stes, Autofiller(catalog, embedder=None), catalog=catalog)
    out = write_artifacts(rep, ROOT / "reports" / "pytest" / "test_handwritten", "test_handwritten.jsonl",
                          {"каталог": "синтетический", "эмбеддинги": False})

    t = rep.total
    assert {p.name for p in out.iterdir()} >= {"report.md", "metrics.json", "items.jsonl", "values.csv"}
    assert len(rep.items) == len(stes)
    assert t.hallucinated == 0, [e for e in rep.errors if e["тип"] == "выдумано"]
    assert t.precision >= 0.95
    assert t.recall >= 0.90
    assert rep.product_hit == rep.product_known
