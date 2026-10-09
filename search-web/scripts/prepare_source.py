#!/usr/bin/env python3
import gzip
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

SEARCH_WEB = Path(__file__).resolve().parents[1]
ROOT = SEARCH_WEB.parent
SOURCE_JSONL = ROOT / "data" / "ste-details.jsonl.gz"
TARGET_DB = SEARCH_WEB / "data" / "source.sqlite"

if not SOURCE_JSONL.exists():
    raise FileNotFoundError(f"Файл {SOURCE_JSONL} не найден. Проверьте скачивание через git lfs pull.")

if TARGET_DB.exists():
    print(f"База {TARGET_DB} уже существует.")
    exit(0)

TARGET_DB.parent.mkdir(parents=True, exist_ok=True)
restored_at = datetime.now(timezone.utc).isoformat()

print(f"Читаем: {SOURCE_JSONL}")
print(f"Создаём: {TARGET_DB}")
print("Импорт записей в SQLite (займет около 15-30 секунд)...")

started = time.time()
with sqlite3.connect(TARGET_DB) as db:
    db.execute("CREATE TABLE sku_details (sku_id INTEGER PRIMARY KEY, fetched_at TEXT NOT NULL, payload TEXT NOT NULL)")
    with gzip.open(SOURCE_JSONL, "rt", encoding="utf-8") as source:
        db.executemany(
            "INSERT INTO sku_details VALUES (?, ?, ?)",
            ((json.loads(line)["id"], restored_at, line.strip()) for line in source if line.strip()),
        )

elapsed = time.time() - started
print(f"Готово за {elapsed:.1f} с! Файл {TARGET_DB} успешно создан.")
