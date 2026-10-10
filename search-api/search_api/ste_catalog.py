from __future__ import annotations

import argparse
import fcntl
import json
import tempfile
import threading
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

import pysqlite3 as sqlite3
import pyarrow.parquet as pq

from search_api.category_taxonomy import (
    UNRESOLVED_CATEGORY_ID,
    CategoryTaxonomy,
    _display_name,
    build_kpgz_taxonomy,
    load_kpgz_reference,
)
from search_api.config import DATA, settings


@dataclass(frozen=True, slots=True)
class Product:
    id: int
    name: str
    description: str
    category: str
    category_id: int
    # Portal STE leaf name — kept for embedding_document stability / sku_embeddings.
    portal_category: str
    manufacturer: str
    country: str
    unit: str
    part_number: str
    model_name: str
    okpd2: str
    okpd2_name: str
    kpgz_code: str
    kpgz_name: str
    spgz_id: int | None
    price: float | None
    image_ids: list[int]
    characteristics: list[dict[str, str]]
    source_updated_at: str


_COLUMNS = [
    "id", "name", "description", "manufacturer", "okei", "oksm",
    "okpd2_code", "okpd2_name", "production_code", "production_name",
    "category_id", "category", "model", "barcode",
    "median_price", "reference_price", "cost_per_unit",
    "sku_image_ids", "characteristics", "created_date",
]

_lock = threading.Lock()
_fingerprint: str | None = None
_catalog: sqlite3.Connection | None = None
_category_stats: list[dict] | None = None
_taxonomy: CategoryTaxonomy | None = None
_CATALOG_SCHEMA = "sqlite-catalog-v2"
_reference_path: Path | None = None


def _ktru_from_characteristics(chars: list[dict[str, str]]) -> str:
    for item in chars:
        if item["name"].casefold() == "ктру" and item["value"].strip():
            return item["value"].strip()
    return ""


def _row_to_product(
    row: dict,
    *,
    category_id: int,
    category_name: str,
    portal_category: str,
    kpgz_code: str,
    kpgz_name: str,
) -> Product:
    chars = []
    for item in row.get("characteristics") or []:
        if not item:
            continue
        name = (item.get("name") or "").strip()
        value = item.get("value")
        if not name or value is None or value == "":
            continue
        chars.append({"name": name, "value": str(value), "unit": (item.get("unit") or "").strip()})
    price = next(
        (p for p in (row.get("reference_price"), row.get("median_price"), row.get("cost_per_unit"))
         if isinstance(p, (int, float)) and p == p and p > 0),
        None,
    )
    images = [int(i) for i in (row.get("sku_image_ids") or []) if isinstance(i, (int, float)) and int(i) > 0]
    return Product(
        id=int(row["id"]),
        name=(row.get("name") or "").strip(),
        description=(row.get("description") or "").strip(),
        category=category_name,
        category_id=category_id,
        portal_category=portal_category,
        manufacturer=(row.get("manufacturer") or "").strip(),
        country=(row.get("oksm") or "").strip(),
        unit=(row.get("okei") or "").strip(),
        part_number=(row.get("barcode") or "").strip(),
        model_name=(row.get("model") or "").strip(),
        okpd2=(row.get("okpd2_code") or "").strip(),
        okpd2_name=(row.get("okpd2_name") or "").strip(),
        kpgz_code=kpgz_code,
        kpgz_name=kpgz_name,
        spgz_id=None,
        price=float(price) if price is not None else None,
        image_ids=list(dict.fromkeys(images)),
        characteristics=chars,
        source_updated_at=row.get("created_date") or "",
    )


def _kpgz_path() -> Path:
    return _reference_path or DATA / "pp-kpgz.parquet"


def _paths_fingerprint(paths: list[Path]) -> str:
    return json.dumps([
        (str(path.resolve()), str(path.stat().st_mtime_ns), str(path.stat().st_size))
        for path in [*paths, _kpgz_path()]
    ], ensure_ascii=False, separators=(",", ":"))


def _build_catalog(paths: list[Path], destination: Path, fingerprint: str) -> None:
    """Build a disk snapshot in bounded batches, then publish it atomically."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=destination.name + ".", suffix=".tmp") as file:
        temporary = Path(file.name)
    reference = load_kpgz_reference(_kpgz_path())
    code_to_id = {row["code"]: row["id"] for row in reference}
    code_to_name = {row["code"]: row["name"] for row in reference}
    direct_counts: Counter[str] = Counter()
    conn = sqlite3.connect(temporary)
    try:
        conn.execute("PRAGMA cache_size=-2048")
        conn.execute("PRAGMA temp_store=FILE")
        conn.executescript("""
            CREATE TABLE products(
                id INTEGER PRIMARY KEY, category_id INTEGER NOT NULL, payload TEXT NOT NULL,
                portal_category_id INTEGER, portal_category TEXT NOT NULL,
                name TEXT NOT NULL, okpd2 TEXT NOT NULL, kpgz_code TEXT NOT NULL,
                kpgz_name TEXT NOT NULL, unit TEXT NOT NULL
            );
            CREATE TABLE images(id INTEGER PRIMARY KEY);
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        for path in paths:
            with pq.ParquetFile(path, memory_map=True) as parquet:
                for batch in parquet.iter_batches(batch_size=256, columns=_COLUMNS, use_threads=False):
                    with conn:
                        for row in batch.to_pylist():
                            product_id = int(row["id"] or 0)
                            if product_id <= 0 or not (row.get("name") or "").strip():
                                continue
                            kpgz_code = (row.get("production_code") or "").strip()
                            kpgz_name = (row.get("production_name") or "").strip() or code_to_name.get(kpgz_code, "")
                            category_id = code_to_id.get(kpgz_code, UNRESOLVED_CATEGORY_ID)
                            category_name = (_display_name(kpgz_code, kpgz_name or code_to_name[kpgz_code])
                                             if kpgz_code in code_to_id else kpgz_name or "Без кода КПГЗ")
                            product = _row_to_product(
                                row, category_id=category_id, category_name=category_name,
                                portal_category=(row.get("category") or "").strip() or "Без категории",
                                kpgz_code=kpgz_code, kpgz_name=kpgz_name,
                            )
                            inserted = conn.execute(
                                "INSERT OR IGNORE INTO products VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (product.id, product.category_id, json.dumps(asdict(product), ensure_ascii=False),
                                 row.get("category_id"), product.portal_category, product.name, product.okpd2,
                                 product.kpgz_code, product.kpgz_name, product.unit),
                            ).rowcount
                            if not inserted:
                                continue
                            conn.executemany("INSERT OR IGNORE INTO images VALUES(?)", [(i,) for i in product.image_ids])
                            direct_counts[kpgz_code] += 1
        taxonomy = build_kpgz_taxonomy(reference, dict(direct_counts))
        names = {node["id"]: node["name"] for node in taxonomy.tree}
        stats = [{"id": cid, "name": names.get(cid, f"КПГЗ #{cid}"), "count": count}
                 for cid, count in conn.execute("SELECT category_id, count(*) FROM products GROUP BY category_id")]
        stats.sort(key=lambda item: (-item["count"], item["name"], item["id"]))
        portal_stats = [{"id": cid, "name": name, "count": count} for cid, name, count in conn.execute("""
            SELECT portal_category_id,portal_category,count(*) FROM products
            WHERE portal_category_id IS NOT NULL GROUP BY portal_category_id,portal_category
            ORDER BY count(*) DESC,portal_category
        """)]
        metadata = {
            "schema": _CATALOG_SCHEMA, "fingerprint": fingerprint,
            "portal_stats": json.dumps(portal_stats, ensure_ascii=False),
            "count": str(conn.execute("SELECT count(*) FROM products").fetchone()[0]),
            "stats": json.dumps(stats, ensure_ascii=False),
            "taxonomy": json.dumps({"tree": taxonomy.tree, "summary": taxonomy.summary,
                "descendants": {str(k): sorted(v) for k, v in taxonomy.descendants.items()},
                "code_to_id": taxonomy.code_to_id}, ensure_ascii=False),
        }
        with conn:
            for column in ("category_id", "portal_category_id"):
                conn.execute(f"CREATE INDEX products_{column} ON products({column}, id)")
            for column in ("okpd2", "kpgz_code", "unit"):
                conn.execute(f"CREATE INDEX products_{column} ON products({column}, name, id)")
            conn.executemany("INSERT INTO metadata VALUES(?,?)", metadata.items())
    except BaseException:
        conn.close()
        temporary.unlink(missing_ok=True)
        raise
    else:
        conn.close()
    if _paths_fingerprint(paths) != fingerprint:
        temporary.unlink()
        raise RuntimeError("Каталог изменился во время построения дискового индекса")
    temporary.replace(destination)


def reset_catalog() -> None:
    global _fingerprint, _catalog, _category_stats, _taxonomy
    with _lock:
        if _catalog is not None:
            _catalog.close()
        _fingerprint = None
        _catalog = None
        _category_stats = None
        _taxonomy = None


def catalog_paths() -> list[Path]:
    paths = settings.catalog_parquet_paths()
    missing = [path for path in [*paths, _kpgz_path()] if not path.exists()]
    if missing:
        raise FileNotFoundError("Не найдены parquet каталога: " + ", ".join(str(p) for p in missing))
    return paths


def get_catalog(paths: list[Path] | None = None) -> sqlite3.Connection:
    global _catalog, _fingerprint, _category_stats, _taxonomy
    resolved = paths or catalog_paths()
    fingerprint = _paths_fingerprint(resolved)
    with _lock:
        if _catalog is not None and _fingerprint == fingerprint:
            return _catalog
        destination = settings.catalog_database
        if _catalog is not None:
            _catalog.close()
            _catalog = None
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.with_suffix(destination.suffix + ".lock").open("a") as build_lock:
            fcntl.flock(build_lock, fcntl.LOCK_EX)
            valid = False
            if destination.exists():
                probe = sqlite3.connect(destination.resolve().as_uri() + "?mode=ro", uri=True)
                try:
                    metadata = dict(probe.execute("SELECT key,value FROM metadata"))
                    valid = metadata.get("schema") == _CATALOG_SCHEMA and metadata.get("fingerprint") == fingerprint
                finally:
                    probe.close()
            if not valid:
                _build_catalog(resolved, destination, fingerprint)
        db = sqlite3.connect(destination.resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False)
        db.execute(f"PRAGMA mmap_size={settings.catalog_mmap_size}")
        db.execute("PRAGMA cache_size=-2048")
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA temp_store=FILE")
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
        raw = json.loads(metadata["taxonomy"])
        _category_stats = json.loads(metadata["stats"])
        _taxonomy = CategoryTaxonomy(raw["tree"],
            {int(k): frozenset(v) for k, v in raw["descendants"].items()}, raw["summary"], raw["code_to_id"])
        _catalog, _fingerprint = db, fingerprint
        return db


def product_count() -> int:
    return int(get_catalog().execute("SELECT value FROM metadata WHERE key='count'").fetchone()[0])


def category_stats() -> list[dict]:
    get_catalog()
    assert _category_stats is not None
    return _category_stats


def category_tree() -> list[dict]:
    get_catalog()
    assert _taxonomy is not None
    return _taxonomy.tree


def category_diagnostics() -> dict:
    get_catalog()
    assert _taxonomy is not None
    return _taxonomy.diagnostics()


def resolve_category_ids(category_id: int | None) -> frozenset[int] | None:
    if category_id is None:
        return None
    get_catalog()
    assert _taxonomy is not None
    return _taxonomy.descendants.get(category_id, frozenset({category_id}))


def has_image_id(image_id: int) -> bool:
    return get_catalog().execute("SELECT 1 FROM images WHERE id=?", (image_id,)).fetchone() is not None


def find_product(product_id: int) -> Product | None:
    if not isinstance(product_id, int) or product_id <= 0:
        return None
    return next(iter(hydrate([product_id])), None)


def hydrate(ids: list[int]) -> list[Product]:
    if not ids:
        return []
    db = get_catalog()
    products: dict[int, Product] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        rows = db.execute(f"SELECT id,payload FROM products WHERE id IN ({','.join('?' for _ in chunk)})", chunk)
        products.update((pid, Product(**json.loads(payload))) for pid, payload in rows)
    return [products[pid] for pid in ids if pid in products]


def iterate_products(batch_size: int = 500) -> Iterator[list[Product]]:
    db = get_catalog()
    last_id = 0
    while True:
        rows = db.execute("SELECT id,payload FROM products WHERE id>? ORDER BY id LIMIT ?", (last_id, batch_size)).fetchall()
        if not rows:
            return
        last_id = rows[-1][0]
        yield [Product(**json.loads(payload)) for _, payload in rows]


def list_products(*, category_id: int | None, offset: int, limit: int) -> tuple[list[Product], int]:
    db = get_catalog()
    allowed = resolve_category_ids(category_id)
    args = sorted(allowed) if allowed is not None else []
    condition = f" WHERE category_id IN ({','.join('?' for _ in args)})" if allowed is not None else ""
    total = db.execute("SELECT count(*) FROM products" + condition, args).fetchone()[0]
    rows = db.execute("SELECT payload FROM products" + condition + " ORDER BY id LIMIT ? OFFSET ?", [*args, limit, offset])
    return [Product(**json.loads(payload)) for (payload,) in rows], total


def product_to_dict(product: Product) -> dict:
    ktru_code = _ktru_from_characteristics(product.characteristics)
    return {
        "id": product.id,
        "name": product.name,
        "description": product.description,
        "category": product.category,
        "categoryId": product.category_id,
        "manufacturer": product.manufacturer,
        "country": product.country,
        "unit": product.unit,
        "partNumber": product.part_number,
        "modelName": product.model_name,
        "okpd2": product.okpd2,
        "okpd2Name": product.okpd2_name,
        "ktruCode": ktru_code,
        "ktruName": "",
        "kpgzCode": product.kpgz_code,
        "kpgzName": product.kpgz_name,
        "spgzId": product.spgz_id,
        "price": product.price,
        "imageIds": product.image_ids,
        "characteristics": product.characteristics,
        "sourceUpdatedAt": product.source_updated_at,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--kpgz", type=Path, default=DATA / "pp-kpgz.parquet")
    args = parser.parse_args()
    settings.catalog_database = args.output
    global _reference_path
    _reference_path = args.kpgz
    get_catalog(args.source)
    reset_catalog()


if __name__ == "__main__":
    main()
