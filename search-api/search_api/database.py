from __future__ import annotations

import threading

from search_api.index_db import open_index_db

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
