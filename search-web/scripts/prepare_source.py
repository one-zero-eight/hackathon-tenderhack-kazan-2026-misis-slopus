#!/usr/bin/env python3
"""Deprecated: catalog cards are read directly from data/ste.parquet.

Previously this script rebuilt a temporary source.sqlite from ste-details.jsonl.gz.
The search importer now indexes from parquet; display fields stay in parquet,
and SEARCH_DATABASE holds only FTS + vectors (+ index_state).
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1].parent
PARQUET = ROOT / "data" / "ste.parquet"

if __name__ == "__main__":
    if PARQUET.exists():
        print(f"Используйте напрямую: {PARQUET}")
        print("Импорт индекса: cd search-api && uv run zakupki-search-index import")
    else:
        raise SystemExit(
            f"Не найден {PARQUET}. Соберите ste.parquet (compact STE) или задайте STE_PARQUET."
        )
