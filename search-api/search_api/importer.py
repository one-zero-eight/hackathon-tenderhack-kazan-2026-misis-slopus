from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone

import sqlite3

from search_api.normalize import document_hash, embedding_document
from search_api.ste_catalog import Product, catalog_paths, get_catalog, iterate_products, reset_catalog


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
    get_catalog(paths)
    conn.execute("DROP TABLE IF EXISTS temp.current_products")
    conn.execute("CREATE TEMP TABLE current_products(product_id INTEGER PRIMARY KEY)")

    upsert_fts = """
        INSERT INTO products_fts(rowid,name,manufacturer,part_number,description,category,characteristics)
        VALUES (?,?,?,?,?,?,?)
    """
    imported = 0
    def flush(items: list[Product]) -> None:
        nonlocal imported
        if not items:
            return
        with conn:
            conn.executemany("INSERT INTO current_products VALUES(?)", [(p.id,) for p in items])
            for product in items:
                content_hash, fts = _product_index_fields(product)
                previous = conn.execute(
                    "SELECT content_hash AS hash, category_id AS categoryId FROM index_state WHERE product_id=?",
                    (product.id,),
                ).fetchone()
                if previous and previous["hash"] == content_hash and previous["categoryId"] == product.category_id:
                    continue
                # Category-only change: keep embeddings, just retarget the filter id.
                if previous and previous["hash"] == content_hash:
                    conn.execute("DELETE FROM products_fts WHERE rowid=?", (product.id,))
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
                        "UPDATE index_state SET category_id=? WHERE product_id=?",
                        (product.category_id, product.id),
                    )
                    conn.execute(
                        "UPDATE product_vectors SET category_id=? WHERE product_id=?",
                        (product.category_id, product.id),
                    )
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

    for batch in iterate_products():
        flush(batch)

    last_stale_id = 0
    while True:
        stale = conn.execute("""
            SELECT product_id FROM index_state
            WHERE product_id>? AND NOT EXISTS (
                SELECT 1 FROM current_products c WHERE c.product_id=index_state.product_id
            ) ORDER BY product_id LIMIT 500
        """, (last_stale_id,)).fetchall()
        if not stale:
            break
        last_stale_id = int(stale[-1]["product_id"])
        with conn:
            for row in stale:
                product_id = int(row["product_id"])
                conn.execute("DELETE FROM products_fts WHERE rowid=?", (product_id,))
                conn.execute("DELETE FROM product_vectors WHERE product_id=?", (product_id,))
                conn.execute("DELETE FROM index_state WHERE product_id=?", (product_id,))
    conn.execute("DROP TABLE current_products")

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
