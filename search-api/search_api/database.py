from __future__ import annotations

import threading

from search_api.index_db import open_index_db
from search_api.ste_catalog import get_catalog

_lock = threading.Lock()
_connection = None


def open_database(path=None):
    return open_index_db(path, readonly=True)


def get_db():
    global _connection
    with _lock:
        if _connection is None:
            _connection = open_database()
        return _connection


def warm_search() -> dict[str, int]:
    """Load catalog before accepting requests, without scanning vectors."""
    db = get_db()
    products, *_ = get_catalog()
    return {
        "products": len(products),
        "mmap_size": db.execute("PRAGMA mmap_size").fetchone()[0],
        "cache_size_kib": -db.execute("PRAGMA cache_size").fetchone()[0],
    }


def close_database() -> None:
    global _connection
    with _lock:
        if _connection is not None:
            _connection.close()
            _connection = None
