import asyncio
import threading

import numpy as np
import pytest
from fastapi.testclient import TestClient

from search_api import database, search
from search_api.config import settings
from search_api.index_db import open_index_db
from search_api.main import app


@pytest.fixture
def search_db(tmp_path, monkeypatch):
    path = tmp_path / "index.sqlite"
    writer = open_index_db(path)
    vector = np.ones(settings.dimensions, dtype="<f4").tobytes()
    with writer:
        writer.execute(
            "INSERT INTO product_vectors(product_id, embedding, category_id) VALUES(?,?,?)",
            (1, vector, 7),
        )
    writer.close()
    db = database.open_database(path)
    monkeypatch.setattr(database, "get_db", lambda: db)
    monkeypatch.setattr(database, "product_count", lambda: 1)
    yield db, vector
    db.close()


def test_search_accepts_long_description_without_truncation():
    query = "Шкаф металлический для медицинских карт. " * 40
    assert len(query) > 500
    assert search.validate_search(query, "semantic", 1, None)[0] == query.strip()
    with pytest.raises(ValueError, match="20000"):
        search.validate_search("а" * 20_001, "semantic", 1, None)


def test_search_connection_memory_settings(search_db):
    db, _ = search_db
    assert db.execute("PRAGMA mmap_size").fetchone()[0] == settings.search_mmap_size
    assert db.execute("PRAGMA cache_size").fetchone()[0] == -settings.search_cache_size_kib


def test_warm_search_loads_catalog_without_scanning_vectors(search_db):
    db, _ = search_db
    statements = []
    db.set_trace_callback(statements.append)
    warmed = database.warm_search()
    assert warmed["products"] == 1
    assert not any("product_vectors" in statement for statement in statements)


def test_populated_index_is_checked_only_once(search_db, monkeypatch):
    db, _ = search_db
    monkeypatch.setattr(database, "_vector_ready_connection", None)
    statements = []
    db.set_trace_callback(statements.append)
    assert database.has_vectors(db)
    assert database.has_vectors(db)
    assert sum("SELECT 1 FROM product_vectors" in sql for sql in statements) == 1


def test_empty_index_is_rechecked_after_vectors_are_added(tmp_path, monkeypatch):
    db = open_index_db(tmp_path / "building.sqlite")
    monkeypatch.setattr(database, "_vector_ready_connection", None)
    try:
        assert not database.has_vectors(db)
        vector = np.ones(settings.dimensions, dtype="<f4").tobytes()
        with db:
            db.execute(
                "INSERT INTO product_vectors(product_id, embedding, category_id) VALUES(?,?,?)",
                (1, vector, 7),
            )
        assert database.has_vectors(db)
    finally:
        db.close()


def test_vector_readiness_is_not_shared_between_connections(search_db, tmp_path, monkeypatch):
    db, _ = search_db
    monkeypatch.setattr(database, "_vector_ready_connection", None)
    assert database.has_vectors(db)
    empty = open_index_db(tmp_path / "empty.sqlite")
    try:
        assert not database.has_vectors(empty)
    finally:
        empty.close()


def test_vector_readiness_is_invalidated_by_index_writer(search_db, tmp_path, monkeypatch):
    db, vector = search_db
    monkeypatch.setattr(database, "_vector_ready_connection", None)
    assert database.has_vectors(db)
    writer = open_index_db(tmp_path / "index.sqlite")
    try:
        with writer:
            writer.execute("DELETE FROM product_vectors")
        assert not database.has_vectors(db)
        with writer:
            writer.execute(
                "INSERT INTO product_vectors(product_id, embedding, category_id) VALUES(?,?,?)",
                (2, vector, 7),
            )
        assert database.has_vectors(db)
    finally:
        writer.close()


def test_lifespan_warms_before_accepting_requests(monkeypatch):
    events = []
    monkeypatch.setattr("search_api.main.warm_search", lambda: (
        events.append("warm") or {"products": 1, "mmap_size": 0, "cache_size_kib": 1}
    ))
    monkeypatch.setattr("search_api.main.close_database", lambda: events.append("close"))
    with TestClient(app) as client:
        assert events == ["warm"]
        assert client.get("/health").status_code == 200
    assert events == ["warm", "close"]


def test_semantic_search_reports_stage_timings(search_db, tmp_path, monkeypatch):
    db, vector = search_db
    monkeypatch.setattr(search, "get_db", lambda: db)
    async def embed(query):
        return vector
    monkeypatch.setattr(search.settings, "search_database", tmp_path / "index.sqlite")
    monkeypatch.setattr(search, "embed_query", embed)
    monkeypatch.setattr(search, "_hydrate_hits", lambda rows: rows)
    result = asyncio.run(search.run_search(query="Молоток слесарный", mode="semantic"))
    assert result["items"][0]["id"] == 1
    assert set(result["timingsMs"]) == {
        "setup", "vectorCheck", "embedding", "knn", "ranking", "hydrate",
    }
    assert all(value >= 0 for value in result["timingsMs"].values())


def test_product_knn_does_not_block_event_loop(search_db, tmp_path, monkeypatch):
    db, vector = search_db
    monkeypatch.setattr(search, "get_db", lambda: db)
    monkeypatch.setattr(search.settings, "search_database", tmp_path / "index.sqlite")
    entered = threading.Event()
    release = threading.Event()
    event_loop_thread = threading.get_ident()
    worker_threads = []
    connections = []

    class Connection:
        def __init__(self):
            self.db = database.open_database(tmp_path / "index.sqlite")
            self.closed = False
            connections.append(self)
            worker_threads.append(threading.get_ident())

        def execute(self, sql, *args):
            if "embedding MATCH" in sql:
                entered.set()
                assert release.wait(5), "event loop did not release the KNN worker"
            return self.db.execute(sql, *args)

        def close(self):
            self.closed = True
            self.db.close()

    async def embed(query):
        assert threading.get_ident() == event_loop_thread
        return vector

    monkeypatch.setattr(search, "open_database", Connection)
    monkeypatch.setattr(search, "embed_query", embed)
    monkeypatch.setattr(search, "_hydrate_hits", lambda rows: rows)

    async def exercise():
        task = asyncio.create_task(search.run_search(query="Молоток", mode="semantic"))
        try:
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0.001)
            assert not task.done()
            # Another coroutine can run while SQLite is still busy.
            release.set()
            result = await task
            assert result["items"][0]["id"] == 1
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())
    assert all(thread != event_loop_thread for thread in worker_threads)
    assert len(connections) == 1
    assert all(connection.closed for connection in connections)


def test_text_search_runs_in_worker(search_db, tmp_path, monkeypatch):
    monkeypatch.setattr(search.settings, "search_database", tmp_path / "index.sqlite")
    loop_thread = threading.get_ident()
    worker_threads = []
    original = search.open_database

    def open_database():
        worker_threads.append(threading.get_ident())
        return original()

    monkeypatch.setattr(search, "open_database", open_database)
    monkeypatch.setattr(search, "_hydrate_hits", lambda rows: rows)
    result = asyncio.run(search.run_search(query="Молоток", mode="text"))
    assert result["mode"] == "text"
    assert result["items"] == []
    assert worker_threads and all(thread != loop_thread for thread in worker_threads)


def test_product_worker_closes_connection_on_search_error(search_db, tmp_path, monkeypatch):
    db, vector = search_db
    monkeypatch.setattr(search, "get_db", lambda: db)
    monkeypatch.setattr(search.settings, "search_database", tmp_path / "index.sqlite")
    connections = []

    class Connection:
        def __init__(self):
            self.db = database.open_database(tmp_path / "index.sqlite")
            self.closed = False
            connections.append(self)

        def execute(self, sql, *args):
            if "embedding MATCH" in sql:
                raise RuntimeError("KNN failed")
            return self.db.execute(sql, *args)

        def close(self):
            self.closed = True
            self.db.close()

    async def embed(query):
        return vector

    monkeypatch.setattr(search, "open_database", Connection)
    monkeypatch.setattr(search, "embed_query", embed)
    with pytest.raises(RuntimeError, match="KNN failed"):
        asyncio.run(search.run_search(query="Молоток", mode="semantic"))
    assert len(connections) == 1
    assert all(connection.closed for connection in connections)
