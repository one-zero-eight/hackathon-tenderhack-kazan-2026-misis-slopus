import asyncio

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


def test_semantic_search_reports_stage_timings(search_db, monkeypatch):
    db, vector = search_db
    async def embed(query):
        return vector
    monkeypatch.setattr(search, "get_db", lambda: db)
    monkeypatch.setattr(search, "embed_query", embed)
    monkeypatch.setattr(search, "_hydrate_hits", lambda rows: rows)
    result = asyncio.run(search.run_search(query="Молоток слесарный", mode="semantic"))
    assert result["items"][0]["id"] == 1
    assert set(result["timingsMs"]) == {
        "setup", "vectorCheck", "embedding", "knn", "ranking", "hydrate",
    }
    assert all(value >= 0 for value in result["timingsMs"].values())
