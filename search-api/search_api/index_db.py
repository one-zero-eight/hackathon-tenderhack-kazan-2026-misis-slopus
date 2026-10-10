from __future__ import annotations

from pathlib import Path

try:
    import pysqlite3 as sqlite3  # contentless_delete / newer FTS (stdlib on Ubuntu is often too old)
except ImportError:  # pragma: no cover
    import sqlite3
import sqlite_vec

from search_api.config import settings


def open_index_db(path: Path | None = None, *, readonly: bool = False) -> sqlite3.Connection:
    db_path = path or settings.search_database
    if readonly:
        if not db_path.exists():
            raise FileNotFoundError(
                f"Не найден поисковый индекс: {db_path}. "
                "Соберите его: uv run zakupki-search-index"
            )
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")

    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    if not readonly:
        _ensure_schema(conn)
    else:
        _check_schema(conn)
    return conn


def _check_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT value FROM metadata WHERE key='index_schema_version'").fetchone()
    if row and row["value"] != settings.index_schema_version:
        raise RuntimeError(
            f"Устаревшая SEARCH_DATABASE (ожидается {settings.index_schema_version}). "
            f"Удалите {settings.search_database} и выполните uv run zakupki-search-index"
        )


# Keep in sync with search-web/drizzle/meta/_journal.json (sha256 of each .sql file).
_DRIZZLE_MIGRATIONS = (
    ("5f560feac82fb379c328af1b7e35443859fd21514135d0e8247894cf83717c5c", 1791387951545),
    ("15df8921b619004a0fa1e7d7297d5c7ad0cafd58c3c74fa14f7dff714b231dc2", 1791390659568),
    ("dd9c6ff70d07fc9c0f3ebbe5530a506d787a67b38a112b2b7367e937720d531e", 1791400000000),
    ("424efb7ee3b4784b450ee26a8c0da02b811ad4ce73ff7ecd29b2b9cf6b9c9aaa", 1791600000000),
)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY NOT NULL,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS index_state (
            product_id INTEGER PRIMARY KEY NOT NULL,
            content_hash TEXT NOT NULL,
            embedded_hash TEXT,
            category_id INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS __drizzle_migrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hash text NOT NULL,
            created_at numeric
        );
        """
    )
    for hash_, created_at in _DRIZZLE_MIGRATIONS:
        conn.execute(
            "INSERT INTO __drizzle_migrations (hash, created_at) "
            "SELECT ?, ? WHERE NOT EXISTS (SELECT 1 FROM __drizzle_migrations WHERE hash = ?)",
            (hash_, created_at, hash_),
        )

    stored = conn.execute("SELECT value FROM metadata WHERE key='index_schema_version'").fetchone()
    if stored and stored["value"] != settings.index_schema_version:
        raise RuntimeError(
            f"Устаревшая SEARCH_DATABASE (ожидается {settings.index_schema_version}). "
            f"Удалите {settings.search_database} и выполните uv run zakupki-search-index"
        )

    fts_sql = (
        conn.execute("SELECT sql FROM sqlite_master WHERE name='products_fts'").fetchone() or {"sql": ""}
    )["sql"] or ""
    if fts_sql and ("content=''" not in fts_sql or "category_id" in fts_sql):
        conn.execute("DROP TABLE IF EXISTS products_fts")

    dims = settings.dimensions
    conn.executescript(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS product_vectors USING vec0(
          product_id INTEGER PRIMARY KEY,
          embedding FLOAT[{dims}] distance_metric=cosine,
          category_id INTEGER
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS products_fts USING fts5(
          name, manufacturer, part_number, description, category, characteristics,
          content='', contentless_delete=1,
          tokenize='unicode61 remove_diacritics 2'
        );
        """
    )

    emb = conn.execute("SELECT value FROM metadata WHERE key='embedding_signature'").fetchone()
    if emb and emb["value"] != settings.embedding_signature:
        prefix = f"{settings.model_id}:{settings.dimensions}:"
        if emb["value"].startswith(prefix):
            # Document text format bump — drop vectors; FTS rebuilt on next import.
            conn.execute("DELETE FROM product_vectors")
            conn.execute("UPDATE index_state SET embedded_hash = NULL")
            conn.execute(
                "UPDATE metadata SET value=? WHERE key='embedding_signature'",
                (settings.embedding_signature,),
            )
        else:
            raise RuntimeError("Индекс создан для другой модели. Создайте новую SEARCH_DATABASE.")
    conn.execute(
        "INSERT INTO metadata(key,value) VALUES('embedding_signature',?) ON CONFLICT(key) DO NOTHING",
        (settings.embedding_signature,),
    )
    conn.execute(
        "INSERT INTO metadata(key,value) VALUES('index_schema_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (settings.index_schema_version,),
    )
    conn.commit()
