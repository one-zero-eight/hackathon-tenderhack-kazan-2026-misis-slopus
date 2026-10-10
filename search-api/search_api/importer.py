from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone

import sqlite3

from search_api.normalize import document_hash, embedding_document
from search_api.ste_catalog import Product, catalog_paths, get_catalog, reset_catalog


def _product_index_fields(product: Product) -> tuple[str, dict[str, str]]:
    document = embedding_document(product)
    return document_hash(document), {
        "name": product.name,
        "manufacturer": product.manufacturer,
        "part_number": product.part_number,
        "description": product.description,
        "category": product.category,
        "characteristics": json.dumps(product.characteristics, ensure_ascii=False),
    }


def import_products(
    conn: sqlite3.Connection,
    on_progress: Callable[[int], None] | None = None,
) -> int:
    paths = catalog_paths()
    reset_catalog()
    by_id, *_ = get_catalog(paths)
    products = sorted(by_id.values(), key=lambda p: p.id)

    upsert_fts = """
        INSERT INTO products_fts(rowid,name,manufacturer,part_number,description,category,characteristics)
        VALUES (?,?,?,?,?,?,?)
    """
    imported = 0
    batch: list[Product] = []

    def flush(items: list[Product]) -> None:
        nonlocal imported
        if not items:
            return
        with conn:
            for product in items:
                content_hash, fts = _product_index_fields(product)
                previous = conn.execute(
                    "SELECT content_hash AS hash, category_id AS categoryId FROM index_state WHERE product_id=?",
                    (product.id,),
                ).fetchone()
                if previous and previous["hash"] == content_hash and previous["categoryId"] == product.category_id:
                    continue
                if previous:
                    conn.execute("DELETE FROM products_fts WHERE rowid=?", (product.id,))
                    conn.execute("DELETE FROM product_vectors WHERE product_id=?", (product.id,))
                conn.execute(
                    upsert_fts,
                    (
                        product.id,
                        fts["name"],
                        fts["manufacturer"],
                        fts["part_number"],
                        fts["description"],
                        fts["category"],
                        fts["characteristics"],
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO index_state(product_id, content_hash, embedded_hash, category_id)
                    VALUES (?,?,NULL,?)
                    ON CONFLICT(product_id) DO UPDATE SET
                      content_hash=excluded.content_hash,
                      category_id=excluded.category_id,
                      embedded_hash=CASE
                        WHEN index_state.content_hash = excluded.content_hash THEN index_state.embedded_hash
                        ELSE NULL
                      END
                    """,
                    (product.id, content_hash, product.category_id),
                )
        imported += len(items)
        if on_progress:
            on_progress(imported)

    for product in products:
        batch.append(product)
        if len(batch) >= 500:
            flush(batch)
            batch = []
    flush(batch)

    keep = {product.id for product in products}
    stale = [
        int(row["product_id"])
        for row in conn.execute("SELECT product_id FROM index_state").fetchall()
        if int(row["product_id"]) not in keep
    ]
    if stale:
        with conn:
            for product_id in stale:
                conn.execute("DELETE FROM products_fts WHERE rowid=?", (product_id,))
                conn.execute("DELETE FROM product_vectors WHERE product_id=?", (product_id,))
                conn.execute("DELETE FROM index_state WHERE product_id=?", (product_id,))

    with conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO metadata(key,value) VALUES('last_imported_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (now,),
        )
        conn.execute(
            "INSERT INTO metadata(key,value) VALUES('catalog_parquets',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps([str(p.resolve()) for p in paths], ensure_ascii=False),),
        )
    return imported
