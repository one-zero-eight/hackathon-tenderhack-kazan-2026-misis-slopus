from __future__ import annotations

import fcntl
import hashlib
import json
import math
import tempfile
import threading
from collections import Counter
from pathlib import Path

import pysqlite3 as sqlite3

from search_api.category_taxonomy import load_kpgz_reference
from search_api.characteristic_normalization import (
    display_name,
    display_text,
    is_numeric,
    normalize_key,
    normalize_unit,
    normalize_value,
)
from search_api.config import settings
from search_api.ste_catalog import _kpgz_path, category_tree, get_catalog, iterate_products

_SCHEMA = "catalog-characteristics-v2"
_METADATA_NAMES = {
    "ктру", "окпд", "окпд2", "кпгз", "спгз", "артикул", "штрихкод",
    "производитель", "страна происхождения", "модель", "наименование", "описание",
}
_lock = threading.Lock()


def _build(destination: Path, fingerprint: str) -> None:
    """Persist normalized counts and display votes in bounded streaming batches."""
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=destination.name, suffix=".tmp") as file:
        temporary = Path(file.name)
    conn = sqlite3.connect(temporary)
    try:
        conn.execute("PRAGMA cache_size=-2048")
        conn.execute("PRAGMA temp_store=FILE")
        conn.execute(f"PRAGMA mmap_size={settings.catalog_mmap_size}")
        conn.executescript("""
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE categories(id INTEGER PRIMARY KEY, code TEXT UNIQUE, parent_id INTEGER, count INTEGER);
            CREATE INDEX categories_parent ON categories(parent_id);
            CREATE TABLE fields(
                category_id INTEGER, name_key TEXT, unit TEXT,
                count INTEGER, numeric_count INTEGER,
                PRIMARY KEY(category_id, name_key, unit)
            ) WITHOUT ROWID;
            CREATE TABLE field_names(
                category_id INTEGER, name_key TEXT, unit TEXT, name TEXT, count INTEGER,
                PRIMARY KEY(category_id, name_key, unit, name)
            ) WITHOUT ROWID;
            CREATE TABLE field_values(
                category_id INTEGER, name_key TEXT, unit TEXT, value_key TEXT, count INTEGER,
                PRIMARY KEY(category_id, name_key, unit, value_key)
            ) WITHOUT ROWID;
            CREATE TABLE value_displays(
                category_id INTEGER, name_key TEXT, unit TEXT, value_key TEXT, value TEXT, count INTEGER,
                PRIMARY KEY(category_id, name_key, unit, value_key, value)
            ) WITHOUT ROWID;
        """)
        counts = dict(get_catalog().execute("SELECT category_id,count(*) FROM products GROUP BY category_id"))
        roots = {node["id"] for node in category_tree() if node["parentId"] is None}
        with conn:
            conn.executemany("INSERT INTO categories VALUES(?,?,?,?)", (
                (row["id"], row["code"], None if row["id"] in roots else row["parentId"], counts.get(row["id"], 0))
                for row in load_kpgz_reference(_kpgz_path())
            ))
        metadata_names = {normalize_key(name) for name in _METADATA_NAMES}
        for batch in iterate_products():
            fields: Counter = Counter()
            numeric: Counter = Counter()
            values: Counter = Counter()
            names: Counter = Counter()
            displays: Counter = Counter()
            for product in batch:
                seen: dict[tuple[str, str], dict[str, set[str]]] = {}
                seen_names: dict[tuple[str, str], set[str]] = {}
                for item in product.characteristics:
                    name = display_name(item.get("name"))
                    unit = normalize_unit(item.get("unit"))
                    value = display_text(item.get("value"))
                    if not name or not value or len(name) > 500 or len(unit) > 200 or len(value) > 2000:
                        continue
                    key = normalize_key(name)
                    value_key = normalize_value(value)
                    if not key or key in metadata_names or not value_key:
                        continue
                    field = (key, unit)
                    seen.setdefault(field, {}).setdefault(value_key, set()).add(value)
                    seen_names.setdefault(field, set()).add(name)
                for (key, unit), options in seen.items():
                    field = (product.category_id, key, unit)
                    fields[field] += 1
                    numeric[field] += int(all(is_numeric(value) for value in options))
                    names.update((*field, name) for name in seen_names[key, unit])
                    values.update((*field, value) for value in options)
                    displays.update((*field, value_key, display) for value_key, variants in options.items() for display in variants)
            with conn:
                conn.executemany("""
                    INSERT INTO fields VALUES(?,?,?,?,?)
                    ON CONFLICT(category_id,name_key,unit) DO UPDATE SET
                        count=count+excluded.count, numeric_count=numeric_count+excluded.numeric_count
                """, ((*field, count, numeric[field]) for field, count in fields.items()))
                conn.executemany("""
                    INSERT INTO field_names VALUES(?,?,?,?,?)
                    ON CONFLICT(category_id,name_key,unit,name) DO UPDATE SET count=count+excluded.count
                """, ((*name, count) for name, count in names.items()))
                conn.executemany("""
                    INSERT INTO field_values VALUES(?,?,?,?,?)
                    ON CONFLICT(category_id,name_key,unit,value_key) DO UPDATE SET count=count+excluded.count
                """, ((*value, count) for value, count in values.items()))
                conn.executemany("""
                    INSERT INTO value_displays VALUES(?,?,?,?,?,?)
                    ON CONFLICT(category_id,name_key,unit,value_key,value) DO UPDATE SET count=count+excluded.count
                """, ((*display, count) for display, count in displays.items()))
        with conn:
            conn.executemany("INSERT INTO metadata VALUES(?,?)", [("schema", _SCHEMA), ("fingerprint", fingerprint)])
    except BaseException:
        conn.close()
        temporary.unlink(missing_ok=True)
        raise
    else:
        conn.close()
    temporary.replace(destination)


def _database() -> sqlite3.Connection:
    catalog_metadata = dict(get_catalog().execute("SELECT key,value FROM metadata"))
    fingerprint = json.dumps([catalog_metadata["schema"], catalog_metadata["fingerprint"]])
    destination = settings.catalog_database.with_suffix(".characteristics.sqlite")
    with _lock, destination.with_suffix(".lock").open("a") as build_lock:
        fcntl.flock(build_lock, fcntl.LOCK_EX)
        valid = False
        if destination.exists():
            probe = sqlite3.connect(destination.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                metadata = dict(probe.execute("SELECT key,value FROM metadata"))
                valid = metadata.get("schema") == _SCHEMA and metadata.get("fingerprint") == fingerprint
            finally:
                probe.close()
        if not valid:
            _build(destination, fingerprint)
    conn = sqlite3.connect(destination.resolve().as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA cache_size=-2048")
    conn.execute(f"PRAGMA mmap_size={settings.catalog_mmap_size}")
    conn.execute("PRAGMA temp_store=FILE")
    conn.execute("PRAGMA query_only=ON")
    return conn


def _descendants(conn: sqlite3.Connection, category_id: int) -> list[int]:
    return [row[0] for row in conn.execute("""
        WITH RECURSIVE subtree(id) AS (
            SELECT id FROM categories WHERE id=?
            UNION ALL SELECT c.id FROM categories c JOIN subtree s ON c.parent_id=s.id
        ) SELECT c.id FROM categories c JOIN subtree s ON c.id=s.id WHERE c.count>0
    """, (category_id,))]


def frequent_catalog_characteristics(kpgz_code: str, limit: int = 20) -> list[dict]:
    """Category/subtree first, then its immediate parent's other branches; no global mixing."""
    if not kpgz_code.strip():
        return []
    conn = _database()
    try:
        category = conn.execute("SELECT id,parent_id FROM categories WHERE code=?", (kpgz_code.strip(),)).fetchone()
        if category is None:
            return []
        own = _descendants(conn, category[0])
        adjacent = sorted(set(_descendants(conn, category[1])) - set(own)) if category[1] is not None else []
        result = []
        used: set[tuple[str, str]] = set()
        for scope, ids in (("category", own), ("adjacent", adjacent)):
            if not ids or len(result) >= limit:
                continue
            placeholders = ",".join("?" for _ in ids)
            total = conn.execute(f"SELECT sum(count) FROM categories WHERE id IN ({placeholders})", ids).fetchone()[0]
            minimum = min(total, max(2, math.ceil(total * 0.1)))
            rows = conn.execute(f"""
                SELECT name_key,unit,sum(count),sum(numeric_count)
                FROM fields WHERE category_id IN ({placeholders})
                GROUP BY name_key,unit HAVING sum(count)>=?
                ORDER BY sum(count) DESC,name_key,unit
            """, [*ids, minimum])
            for key, unit, count, numeric_count in rows:
                if (key, unit) in used:
                    continue
                used.add((key, unit))
                name = conn.execute(f"""
                    SELECT name FROM field_names
                    WHERE category_id IN ({placeholders}) AND name_key=? AND unit=?
                    GROUP BY name ORDER BY sum(count) DESC,name LIMIT 1
                """, [*ids, key, unit]).fetchone()[0]
                examples = []
                example_counts = {}
                for value_key, value_count in conn.execute(f"""
                    SELECT value_key,sum(count) FROM field_values
                    WHERE category_id IN ({placeholders}) AND name_key=? AND unit=?
                    GROUP BY value_key ORDER BY sum(count) DESC,value_key LIMIT 5
                """, [*ids, key, unit]):
                    display = conn.execute(f"""
                        SELECT value FROM value_displays
                        WHERE category_id IN ({placeholders}) AND name_key=? AND unit=? AND value_key=?
                        GROUP BY value ORDER BY sum(count) DESC,value LIMIT 1
                    """, [*ids, key, unit, value_key]).fetchone()[0]
                    examples.append(display)
                    example_counts[display] = value_count
                numeric_value_counts = None
                if numeric_count == count:
                    distribution = dict(conn.execute(f"""
                        SELECT value_key,sum(count) FROM field_values
                        WHERE category_id IN ({placeholders}) AND name_key=? AND unit=?
                        GROUP BY value_key
                    """, [*ids, key, unit]))
                    # Bucket frequencies can be summed exactly only for single-valued products.
                    if sum(distribution.values()) == count:
                        numeric_value_counts = distribution
                field_id = hashlib.sha256(f"{key}\0{unit}".encode()).hexdigest()[:24]
                result.append({
                    "id": f"catalog:{field_id}", "name": name, "nameKey": key, "unit": unit,
                    "numeric": numeric_count == count, "count": count, "total": total,
                    "frequency": count / total, "scope": scope, "examples": examples,
                    "exampleCounts": example_counts, "numericValueCounts": numeric_value_counts,
                })
                if len(result) >= limit:
                    break
        return result
    finally:
        conn.close()
