"""Disk-backed, explicitly built hybrid classifier index (never rebuilt by requests)."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator, Literal

import pyarrow.parquet as pq
import pymorphy3
import pysqlite3 as sqlite3
import sqlite_vec

from search_api.config import settings
from search_api.embeddings import _float16_to_sqlite, embed_client, embed_query, embed_texts

logger = logging.getLogger("uvicorn.error")
SCHEMA_VERSION = "classifiers-search-v2"
CLASSIFIER_EMBEDDINGS_SCHEMA = "classifier-embeddings-v2"
BATCH_SIZE = 128
RRF_K = 60
Kind = Literal["okpd", "ktru", "kpgz", "spgz"]
_FOLD_TABLE = str.maketrans({"ё": "е", "Ё": "е"})
_MORPH = pymorphy3.MorphAnalyzer()
_TIERS = {"exact_code": 6, "exact_name": 5, "name": 4, "synonym": 3,
          "semantic": 2, "weak_synonym": 1, "link": 0}
_SOURCE_SETTINGS = {"okpd": "okpd2_parquet", "ktru": "ktru_parquet",
                    "moy_ktru": "moy_ktru_parquet", "kpgz": "kpgz_parquet", "spgz": "spgz_parquet"}
_REBUILD = "uv run zakupki-search-index classifiers --embed"


def fold_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).translate(_FOLD_TABLE).casefold()


@lru_cache(maxsize=32768)
def _lemma(word: str) -> str:
    return fold_text(_MORPH.parse(word)[0].normal_form) if re.search("[а-я]", word) else word


def _tokens(value: str) -> list[str]:
    return list(dict.fromkeys(_lemma(word) for word in re.findall(r"\w+", fold_text(value))))[:64]


def _normal(value: str) -> str:
    return " ".join(sorted(_tokens(value)))


def fts_query(query: str) -> str:
    # Require every term: an unrelated штукатурный object must not match a hammer.
    return " AND ".join(f'"{token}"' for token in _tokens(query)[:32])


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _content_hash(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()


def _model_signature() -> str:
    return _content_hash(_json([settings.embedding_signature, settings.model_id, settings.dimensions,
                                settings.document_prompt, settings.query_prompt, SCHEMA_VERSION]))


def _source_stat(path: Path) -> dict[str, Any]:
    path = path.resolve()
    if not path.exists():
        return {"path": str(path), "stat": None}
    stat = path.stat()
    return {"path": str(path), "stat": [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]}


def classifier_staging_path(output: Path | None = None) -> Path:
    output = output or settings.classifier_database
    return output.with_suffix(output.suffix + ".part")


def open_classifier_db(path: Path | None = None, *, readonly: bool = False) -> sqlite3.Connection:
    db_path = path or settings.classifier_database
    if readonly:
        if not db_path.exists():
            raise FileNotFoundError(f"Не найден индекс справочников: {db_path}. Соберите: {_REBUILD}")
        conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA mmap_size={settings.search_mmap_size}")
    conn.execute(f"PRAGMA cache_size=-{settings.search_cache_size_kib}")
    conn.execute("PRAGMA temp_store=FILE")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    if readonly:
        conn.execute("PRAGMA query_only=ON")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(f"""
        CREATE TABLE metadata(key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL);
        CREATE TABLE entries(
          id INTEGER PRIMARY KEY, kind TEXT NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL,
          name_fold TEXT NOT NULL, parent_code TEXT, link_code TEXT, status TEXT,
          is_template INTEGER NOT NULL DEFAULT 0, is_excluded INTEGER NOT NULL DEFAULT 0,
          synonyms_json TEXT, weak_synonyms_json TEXT, path TEXT, embed_text TEXT NOT NULL DEFAULT '',
          content_hash TEXT NOT NULL DEFAULT '', embedded_hash TEXT, UNIQUE(kind,code));
        CREATE INDEX ix_entries_code ON entries(code);
        CREATE INDEX ix_entries_link ON entries(kind,link_code);
        CREATE INDEX ix_entries_pending ON entries(id)
          WHERE embedded_hash IS NULL OR embedded_hash != content_hash;
        CREATE TABLE aliases(entry_id INTEGER NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL,
                             folded TEXT NOT NULL, normal TEXT NOT NULL,
                             PRIMARY KEY(entry_id,role,folded));
        CREATE INDEX ix_aliases_fold ON aliases(folded,role,entry_id);
        CREATE INDEX ix_aliases_normal ON aliases(normal,role,entry_id);
        CREATE TABLE vocabulary(term TEXT PRIMARY KEY) WITHOUT ROWID;
        CREATE TABLE spelling(key TEXT NOT NULL,term TEXT NOT NULL,PRIMARY KEY(key,term)) WITHOUT ROWID;
        CREATE VIRTUAL TABLE entries_fts USING fts5(code,name,synonyms,weak_synonyms,path,
          content='',contentless_delete=1,tokenize='unicode61 remove_diacritics 2');
        CREATE VIRTUAL TABLE entry_vectors USING vec0(
          entry_id INTEGER PRIMARY KEY, embedding FLOAT[{settings.dimensions}] distance_metric=cosine);
        CREATE TABLE mirror(code TEXT PRIMARY KEY, names TEXT NOT NULL, synonyms TEXT NOT NULL);
        CREATE TABLE okpd_context(code TEXT PRIMARY KEY, synonyms TEXT NOT NULL);
        CREATE TABLE kpgz_external(external_id TEXT PRIMARY KEY, code TEXT NOT NULL UNIQUE, parent_id TEXT);
    """)


def _metadata(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        return dict(conn.execute("SELECT key,value FROM metadata"))
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"Некорректный индекс справочников. Пересоберите: {_REBUILD}") from exc


def validate_classifier_index(conn: sqlite3.Connection, *, require_embeddings: bool = False) -> None:
    meta = _metadata(conn)
    if meta.get("schema") != SCHEMA_VERSION or meta.get("build_complete") != "1":
        raise RuntimeError(f"Устаревший или незавершённый индекс справочников. Пересоберите: {_REBUILD}")
    sources = json.loads(meta["sources"])
    for kind, source in sources.items():
        if _source_stat(Path(source["path"])) != source:
            raise RuntimeError(f"Изменился источник {kind}. Пересоберите: {_REBUILD}")
        if meta.get("configured_sources") == "1" and Path(getattr(settings, _SOURCE_SETTINGS[kind])).resolve() != Path(source["path"]):
            raise RuntimeError(f"Изменился путь источника {kind}. Пересоберите: {_REBUILD}")
    if require_embeddings:
        if meta.get("embedding_signature") != _model_signature():
            raise RuntimeError(f"Изменилась модель или prompt эмбеддингов. Пересоберите: {_REBUILD}")
        pending = conn.execute("SELECT 1 FROM entries WHERE embedded_hash IS NULL OR embedded_hash!=content_hash LIMIT 1").fetchone()
        if meta.get("embeddings_complete") != "1" or pending:
            raise RuntimeError(f"Эмбеддинги справочников не завершены или устарели. Соберите: {_REBUILD}")


def _iter_parquet(path: Path, columns: list[str]) -> Iterator[dict[str, Any]]:
    with pq.ParquetFile(path, memory_map=True) as parquet:
        names = set(parquet.schema_arrow.names)
        for batch in parquet.iter_batches(columns=[c for c in columns if c in names],
                                          batch_size=BATCH_SIZE, use_threads=False):
            yield from batch.to_pylist()


def _synonyms(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: dict[str, str] = {}
    for item in value:
        text = str(item or "").strip()
        if text:
            out.setdefault(fold_text(text), text)
    return list(out.values())


def _code(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("code")
    return str(value).strip() if value is not None and str(value).strip() else None


def _paths(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    chain: list[str] = []
    seen: set[tuple[str, str]] = set()
    current = row
    for _ in range(32):
        key = (current["kind"], current["code"])
        if key in seen:
            break
        seen.add(key)
        chain.append(f"{current['code']} {current['name']}")
        parent_kind, parent_code = current["kind"], current["parent_code"]
        if not parent_code and current["kind"] in {"ktru", "spgz"}:
            parent_kind = "okpd" if current["kind"] == "ktru" else "kpgz"
            parent_code = current["link_code"]
        if not parent_code:
            break
        parent = conn.execute("SELECT * FROM entries WHERE kind=? AND code=?", (parent_kind, parent_code)).fetchone()
        if parent is None and current["kind"] == "ktru" and current["link_code"]:
            parent = conn.execute("SELECT * FROM entries WHERE kind='okpd' AND code=?", (current["link_code"],)).fetchone()
        if parent is None:
            break
        current = parent
    return " / ".join(reversed(chain))


def build_classifier_index(*, output: Path | None = None, okpd_path: Path | None = None,
                           ktru_path: Path | None = None, moy_ktru_path: Path | None = None,
                           kpgz_path: Path | None = None, spgz_path: Path | None = None,
                           publish: bool = True) -> dict[str, int]:
    """Stream sources to a disk staging DB. ``publish=False`` leaves a resumable .part file."""
    configured = all(p is None for p in (okpd_path, ktru_path, moy_ktru_path, kpgz_path, spgz_path))
    output = output or settings.classifier_database
    paths = {"okpd": okpd_path or settings.okpd2_parquet, "ktru": ktru_path or settings.ktru_parquet,
             "moy_ktru": moy_ktru_path or settings.moy_ktru_parquet,
             "kpgz": kpgz_path or settings.kpgz_parquet, "spgz": spgz_path or settings.spgz_parquet}
    for kind, path in paths.items():
        if kind != "moy_ktru" and not path.exists():
            raise FileNotFoundError(f"Нет источника: {path}")
    sources = {kind: _source_stat(path) for kind, path in paths.items()}
    temporary = classifier_staging_path(output)
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.unlink(missing_ok=True)
    conn = open_classifier_db(temporary)
    conn.execute("PRAGMA mmap_size=0")
    conn.execute("PRAGMA cache_size=-4096")
    counts = dict.fromkeys(("okpd", "ktru", "kpgz", "spgz"), 0)
    try:
        _ensure_schema(conn)
        # Large mirror and context maps never become Python dictionaries.
        if paths["moy_ktru"].exists():
            for row in _iter_parquet(paths["moy_ktru"], ["code", "tree_display_name", "shortname", "similar_products"]):
                if code := _code(row.get("code")):
                    names = _synonyms([row.get("tree_display_name"), row.get("shortname")])
                    conn.execute("INSERT OR REPLACE INTO mirror VALUES(?,?,?)",
                                 (code, _json(names), _json(_synonyms(row.get("similar_products")))))
        conn.commit()

        def insert(kind: str, code: str, name: str, *, names: list[str] | None = None,
                   synonyms: list[str] | None = None, weak: list[str] | None = None,
                   parent: str | None = None, link: str | None = None, status: str | None = None,
                   template: bool = False, excluded: bool = False) -> None:
            synonyms, weak = synonyms or [], weak or []
            name = name.strip() or code
            cursor = conn.execute("""INSERT INTO entries(kind,code,name,name_fold,parent_code,link_code,status,
                                      is_template,is_excluded,synonyms_json,weak_synonyms_json)
                                      VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                                  (kind, code, name, fold_text(name), parent, link, status, int(template),
                                   int(excluded), _json(synonyms), _json(weak)))
            for role, values in (("name", _synonyms([name, *(names or [])])), ("synonym", synonyms), ("weak_synonym", weak)):
                conn.executemany("INSERT OR IGNORE INTO aliases VALUES(?,?,?,?,?)",
                                 [(cursor.lastrowid, role, text, fold_text(text), _normal(text)) for text in values])
            counts[kind] += 1
            if counts[kind] % 1000 == 0:
                conn.commit()

        for row in _iter_parquet(paths["okpd"], ["code", "name", "parent", "is_actual", "similar_products"]):
            if not (code := _code(row.get("code"))):
                continue
            synonyms = _synonyms(row.get("similar_products"))
            conn.execute("INSERT INTO okpd_context VALUES(?,?)", (code, _json(synonyms)))
            insert("okpd", code, str(row.get("name") or ""), parent=_code(row.get("parent")),
                   status="ACTIVE" if row.get("is_actual") else "INACTIVE", synonyms=synonyms)
        conn.commit()
        for row in _iter_parquet(paths["ktru"], ["code", "name", "status", "is_template", "okpd2", "parent"]):
            if not (code := _code(row.get("code"))):
                continue
            mirror = conn.execute("SELECT names,synonyms FROM mirror WHERE code=?", (code,)).fetchone()
            names = json.loads(mirror["names"]) if mirror else []
            official = str(row.get("name") or "")
            insert("ktru", code, names[0] if names else official, names=[official, *names],
                   synonyms=json.loads(mirror["synonyms"]) if mirror else [],
                   parent=_code(row.get("parent")), link=_code(row.get("okpd2")) or code.split("-", 1)[0],
                   status=str(row.get("status") or ""), template=bool(row.get("is_template")))
        conn.commit()

        def context(code: str | None) -> list[str]:
            row = conn.execute("SELECT synonyms FROM okpd_context WHERE code=?", (code,)).fetchone()
            return json.loads(row["synonyms"]) if row else []

        for row in _iter_parquet(paths["kpgz"], ["code", "name", "externalId", "parentId", "okpd"]):
            if not (code := _code(row.get("code"))):
                continue
            if (external := _code(row.get("externalId"))) is not None:
                conn.execute("INSERT INTO kpgz_external VALUES(?,?,?)", (external, code, _code(row.get("parentId"))))
            link = _code(row.get("okpd"))
            insert("kpgz", code, str(row.get("name") or ""), link=link, weak=context(link))
        conn.execute("""UPDATE entries SET parent_code=(
            SELECT parent.code FROM kpgz_external child JOIN kpgz_external parent ON parent.external_id=child.parent_id
            WHERE child.code=entries.code) WHERE kind='kpgz'""")
        conn.commit()
        for row in _iter_parquet(paths["spgz"], ["id", "name", "kpgzId", "okpd2Code", "okpdCode", "isExcluded"]):
            if not (code := _code(row.get("id"))):
                continue
            linked = conn.execute("""SELECT e.code,e.link_code FROM kpgz_external k JOIN entries e
                                     ON e.kind='kpgz' AND e.code=k.code WHERE k.external_id=?""",
                                  (_code(row.get("kpgzId")),)).fetchone()
            okpd = _code(row.get("okpd2Code")) or _code(row.get("okpdCode")) or (linked["link_code"] if linked else None)
            insert("spgz", code, str(row.get("name") or ""), link=linked["code"] if linked else None,
                   excluded=bool(row.get("isExcluded")), weak=context(okpd))
        conn.commit()
        last_id = 0
        while rows := conn.execute("SELECT * FROM entries WHERE id>? ORDER BY id LIMIT ?", (last_id, BATCH_SIZE)).fetchall():
            for row in rows:
                aliases = conn.execute("SELECT role,text FROM aliases WHERE entry_id=?", (row["id"],)).fetchall()
                names = [alias["text"] for alias in aliases if alias["role"] == "name"]
                synonyms = json.loads(row["synonyms_json"])
                weak = json.loads(row["weak_synonyms_json"])
                path = _paths(conn, row)
                text = " | ".join(["title: " + row["name"], f"{row['kind'].upper()}: {row['code']}",
                                   "names: " + "; ".join(names), "path: " + path,
                                   "synonyms: " + "; ".join(synonyms), "related: " + "; ".join(weak)])
                conn.execute("UPDATE entries SET path=?,embed_text=?,content_hash=? WHERE id=?",
                             (path, text, _content_hash(text), row["id"]))
                fields = [row["code"], " ".join(names), " ".join(synonyms), " ".join(weak), path]
                normalized = [" ".join(_tokens(field)) for field in fields]
                conn.execute("INSERT INTO entries_fts(rowid,code,name,synonyms,weak_synonyms,path) VALUES(?,?,?,?,?,?)",
                             (row["id"], *normalized))
                for token in {token for field in normalized for token in field.split()}:
                    fresh = conn.execute("INSERT OR IGNORE INTO vocabulary VALUES(?)", (token,)).rowcount
                    if fresh and 5 <= len(token) <= 32:
                        keys = {token, *(token[:i] + token[i + 1:] for i in range(len(token)))}
                        conn.executemany("INSERT OR IGNORE INTO spelling VALUES(?,?)", [(key, token) for key in keys])
            last_id = rows[-1]["id"]
            conn.commit()
        if sources != {kind: _source_stat(path) for kind, path in paths.items()}:
            raise RuntimeError("Источники изменились во время сборки индекса справочников")
        conn.executescript("DROP TABLE mirror; DROP TABLE okpd_context; DROP TABLE kpgz_external;")
        conn.executemany("INSERT INTO metadata VALUES(?,?)", {
            "schema": SCHEMA_VERSION, "sources": _json(sources), "configured_sources": str(int(configured)),
            "embedding_signature": _model_signature(), "build_complete": "1", "embeddings_complete": "0",
            **{f"{kind}_count": str(count) for kind, count in counts.items()},
        }.items())
        conn.commit()
    finally:
        conn.close()
    if publish:
        publish_classifier_index(output=output, require_embeddings=False)
    return counts


def publish_classifier_index(*, output: Path | None = None, require_embeddings: bool = True) -> None:
    output = output or settings.classifier_database
    temporary = classifier_staging_path(output)
    conn = open_classifier_db(temporary, readonly=True)
    try:
        validate_classifier_index(conn, require_embeddings=require_embeddings)
    finally:
        conn.close()
    temporary.replace(output)


def apply_classifier_embeddings_from_parquet(conn: sqlite3.Connection, path: Path) -> int:
    """Fill pending entry_vectors from id/kind/code + content_hash float16 parquet (Git LFS)."""
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    meta = pq.read_schema(path).metadata or {}
    schema = (meta.get(b"schema") or b"").decode()
    signature = (meta.get(b"embedding_signature") or b"").decode()
    if schema and schema != CLASSIFIER_EMBEDDINGS_SCHEMA:
        raise RuntimeError(f"Неверная схема classifier embeddings: {schema}")
    if signature and signature != _model_signature():
        logger.warning("Classifier parquet signature mismatch, skip %s", path)
        print(f"Classifier parquet пропущен (другая модель/prompt): {path}", flush=True)
        return 0
    expected = settings.dimensions * 2
    # (kind, code) → (content_hash, float32 embedding); id is stable blake2b(kind||code).
    by_key: dict[tuple[str, str], tuple[str, bytes]] = {}
    with pq.ParquetFile(path, memory_map=True) as parquet:
        cols = ["kind", "code", "content_hash", "embedding"]
        for batch in parquet.iter_batches(columns=cols, batch_size=1024, use_threads=False):
            for row in batch.to_pylist():
                blob = bytes(row["embedding"])
                if len(blob) != expected:
                    raise RuntimeError(f"Неверная размерность в {path}: {len(blob)} байт")
                by_key[(str(row["kind"]), str(row["code"]))] = (
                    str(row["content_hash"]),
                    _float16_to_sqlite(blob),
                )
    if not by_key:
        return 0
    rows = conn.execute("""
        SELECT id, kind, code, content_hash FROM entries
        WHERE embedded_hash IS NULL OR embedded_hash != content_hash
    """).fetchall()
    applied = 0
    with conn:
        for row in rows:
            cached = by_key.get((row["kind"], row["code"]))
            if cached is None or cached[0] != row["content_hash"]:
                continue
            conn.execute("DELETE FROM entry_vectors WHERE entry_id=?", (row["id"],))
            conn.execute("INSERT INTO entry_vectors VALUES(?,?)", (row["id"], cached[1]))
            conn.execute("UPDATE entries SET embedded_hash=? WHERE id=?", (row["content_hash"], row["id"]))
            applied += 1
    logger.info("Classifier embeddings from parquet: %s", applied)
    return applied


async def embed_classifier_pending(*, path: Path | None = None, limit: int | None = None,
                                   batch_size: int | None = None, concurrency: int | None = None,
                                   embeddings: Path | None = None) -> int:
    batch_size = settings.embed_batch_size if batch_size is None else batch_size
    concurrency = settings.embed_concurrency if concurrency is None else concurrency
    if batch_size <= 0 or concurrency <= 0 or (limit is not None and limit < 0):
        raise ValueError("batch и concurrency должны быть положительными, limit — неотрицательным")
    conn = open_classifier_db(path)
    try:
        validate_classifier_index(conn)
        if _metadata(conn)["embedding_signature"] != _model_signature():
            raise RuntimeError(f"Изменилась модель эмбеддингов. Пересоберите: {_REBUILD}")
        done = 0
        with conn:
            conn.execute("UPDATE metadata SET value='0' WHERE key='embeddings_complete'")
        cache = embeddings if embeddings is not None else (
            settings.classifier_embeddings if settings.classifier_embeddings.exists() else None
        )
        if cache is not None:
            print(f"Classifier parquet: {cache}", flush=True)
            done += apply_classifier_embeddings_from_parquet(conn, cache)
        still = conn.execute(
            "SELECT 1 FROM entries WHERE embedded_hash IS NULL OR embedded_hash!=content_hash LIMIT 1"
        ).fetchone()
        meow_budget = None if limit is None else max(limit - done, 0)
        if still and meow_budget != 0:
            import asyncio
            from collections import deque

            last_id = 0
            async with embed_client() as client:
                pending = deque()
                scheduled = 0
                meow_done = 0
                exhausted = False
                try:
                    while pending or not exhausted:
                        while not exhausted and len(pending) < concurrency:
                            take = batch_size if meow_budget is None else min(batch_size, meow_budget - meow_done - scheduled)
                            if take <= 0:
                                exhausted = True
                                break
                            rows = conn.execute("""SELECT id,embed_text,content_hash FROM entries WHERE id>?
                                AND (embedded_hash IS NULL OR embedded_hash!=content_hash) ORDER BY id LIMIT ?""",
                                                (last_id, take)).fetchall()
                            if not rows:
                                exhausted = True
                                break
                            task = asyncio.create_task(embed_texts([row["embed_text"] for row in rows], "document", client=client))
                            pending.append((rows, task))
                            scheduled += len(rows)
                            last_id = rows[-1]["id"]
                        if not pending:
                            break
                        rows, task = pending.popleft()
                        vectors = await task
                        with conn:
                            for row, vector in zip(rows, vectors, strict=True):
                                conn.execute("DELETE FROM entry_vectors WHERE entry_id=?", (row["id"],))
                                conn.execute("INSERT INTO entry_vectors VALUES(?,?)", (row["id"], vector))
                                conn.execute("UPDATE entries SET embedded_hash=? WHERE id=?", (row["content_hash"], row["id"]))
                        done += len(rows)
                        meow_done += len(rows)
                        scheduled -= len(rows)
                        logger.info("Classifier embeddings: %s", done)
                finally:
                    tasks = [task for _, task in pending]
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        validate_classifier_index(conn)
        pending = conn.execute("SELECT 1 FROM entries WHERE embedded_hash IS NULL OR embedded_hash!=content_hash LIMIT 1").fetchone()
        if not pending:
            with conn:
                conn.execute("UPDATE metadata SET value='1' WHERE key='embeddings_complete'")
        return done
    finally:
        conn.close()


def _row_hit(row: sqlite3.Row, *, score: float | None = None, match: str = "link", evidence: str = "link") -> dict[str, Any]:
    return {"id": int(row["id"]), "kind": row["kind"], "code": row["code"], "name": row["name"],
            "linkCode": row["link_code"], "status": row["status"], "isTemplate": bool(row["is_template"]),
            "isExcluded": bool(row["is_excluded"]), "score": score, "rank": 0.0, "match": match,
            "evidence": evidence, "synonyms": json.loads(row["synonyms_json"] or "[]"),
            "weakSynonyms": json.loads(row["weak_synonyms_json"] or "[]")}


def _exact_boosts(conn: sqlite3.Connection, query: str) -> list[dict[str, Any]]:
    hits: dict[int, dict[str, Any]] = {}
    # Indexed equality, including Cyrillic case/ё and reordered inflections. Never JSON LIKE.
    for row in conn.execute("SELECT * FROM entries WHERE code=? LIMIT 256", (query.strip(),)):
        hits[row["id"]] = _row_hit(row, match="exact", evidence="exact_code")
    rows = conn.execute("""SELECT e.*,a.role,a.folded FROM aliases a JOIN entries e ON e.id=a.entry_id
                            WHERE a.normal=? ORDER BY CASE a.role WHEN 'name' THEN 0
                            WHEN 'synonym' THEN 1 ELSE 2 END,e.id LIMIT 512""", (_normal(query),)).fetchall()
    for row in rows:
        evidence = "exact_name" if row["role"] == "name" else row["role"]
        if row["id"] not in hits or _TIERS[evidence] > _TIERS[hits[row["id"]]["evidence"]]:
            hits[row["id"]] = _row_hit(row, match="exact", evidence=evidence)
    return sorted(hits.values(), key=_hit_key)


def _one_edit(left: str, right: str) -> bool:
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        diffs = [i for i, (a, b) in enumerate(zip(left, right)) if a != b]
        return len(diffs) <= 1 or (len(diffs) == 2 and diffs[1] == diffs[0] + 1
                                  and left[diffs[0]] == right[diffs[1]] and left[diffs[1]] == right[diffs[0]])
    short, long = (left, right) if len(left) < len(right) else (right, left)
    i = 0
    while i < len(short) and short[i] == long[i]:
        i += 1
    return short[i:] == long[i + 1:]


def _text_hits(conn: sqlite3.Connection, query: str, limit: int) -> list[dict[str, Any]]:
    tokens = _tokens(query)[:32]
    if not tokens:
        return []
    variants: list[list[str]] = []
    typo = False
    for token in tokens:
        choices = [token]
        if len(token) >= 5 and not conn.execute("SELECT 1 FROM vocabulary WHERE term=?", (token,)).fetchone():
            keys = sorted({token, *(token[:i] + token[i + 1:] for i in range(len(token)))})
            placeholders = ",".join("?" for _ in keys)
            candidates = conn.execute(f"SELECT DISTINCT term FROM spelling WHERE key IN ({placeholders}) ORDER BY term LIMIT 64", keys)
            choices += [r["term"] for r in candidates if _one_edit(token, r["term"])][:4]
            typo |= len(choices) > 1
        variants.append(choices)
    expression = " AND ".join("(" + " OR ".join(f'"{v}"' for v in choices) + ")" for choices in variants)
    rows = conn.execute("""SELECT e.*,bm25(entries_fts,8,6,3,0.2,0.5) AS bm
                           FROM entries_fts JOIN entries e ON e.id=entries_fts.rowid
                           WHERE entries_fts MATCH ? ORDER BY bm,e.id LIMIT ?""", (expression, limit)).fetchall()
    hits = []
    for row in rows:
        best = None
        for alias in conn.execute("SELECT role,text FROM aliases WHERE entry_id=?", (row["id"],)):
            words = set(_tokens(alias["text"]))
            if all(any(v in words for v in choices) for choices in variants):
                if best is None or _TIERS[alias["role"]] > _TIERS[best]:
                    best = alias["role"]
        if best is None:
            # Path labels are searchable as context, but don't combine unrelated
            # words from a broad parent and the leaf into a fabricated name match.
            segments = [set(_tokens(segment)) for segment in (row["path"] or "").split(" / ")]
            if not any(all(any(v in words for v in choices) for choices in variants) for words in segments):
                continue
            best = "weak_synonym"
        hit = _row_hit(row, match="text", evidence=best)
        hit["_typo"] = typo
        hits.append(hit)
    return hits


async def _semantic_hits(conn: sqlite3.Connection, query: str, limit: int) -> list[dict[str, Any]]:
    vector = await embed_query(query)
    rows = conn.execute("""SELECT e.*,v.distance FROM entry_vectors v JOIN entries e ON e.id=v.entry_id
                           WHERE v.embedding MATCH ? AND k=? ORDER BY v.distance""", (vector, limit)).fetchall()
    return [_row_hit(row, score=1 - float(row["distance"]), match="semantic", evidence="semantic") for row in rows]


def _hit_key(hit: dict[str, Any]) -> tuple:
    jev = hit.get("jevScore")
    if isinstance(jev, (int, float)) and not isinstance(jev, bool):
        return (0, -float(jev), hit["kind"], hit["code"])
    similarity = (hit.get("score") or 0) if hit["evidence"] == "semantic" else 0
    return (1, -_TIERS[hit["evidence"]], -similarity, -hit["rank"], hit["kind"], hit["code"])


def _fuse(exact: list[dict], semantic: list[dict], textual: list[dict]) -> list[dict]:
    hits: dict[int, dict[str, Any]] = {}
    for bucket in (exact, semantic, textual):
        for index, item in enumerate(bucket):
            contribution = 1 / (RRF_K + index + 1)
            old = hits.get(item["id"])
            if old is None:
                hits[item["id"]] = {**item, "rank": contribution}
                continue
            old["rank"] += contribution
            if item["score"] is not None:
                old["score"] = item["score"]
            if _TIERS[item["evidence"]] > _TIERS[old["evidence"]]:
                old.update(evidence=item["evidence"], match=item["match"], _typo=item.get("_typo", False))
            elif old["match"] != "exact" and old["match"] != item["match"]:
                old["match"] = "both"
    return sorted(hits.values(), key=_hit_key)


def _entry_lookup(conn: sqlite3.Connection, kind: str, code: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM entries WHERE kind=? AND code=?", (kind, code)).fetchone()
    return _row_hit(row) if row else None


def _active(hit: dict[str, Any]) -> bool:
    if hit["isExcluded"] or hit["isTemplate"]:
        return False
    if hit["kind"] in {"okpd", "ktru"}:
        return (hit.get("status") or "").upper() in {"ACTIVE", "ACTUAL"}
    return True


def _pairs(hits: list[dict[str, Any]], conn: sqlite3.Connection, limit: int,
           parent_kind: str, leaf_kind: str, parent_key: str, leaf_key: str) -> list[dict[str, Any]]:
    pairs = []
    for hit in hits:
        if hit["kind"] not in {parent_kind, leaf_kind} or not _active(hit):
            continue
        parent = _entry_lookup(conn, parent_kind, hit["linkCode"]) if hit["kind"] == leaf_kind and hit["linkCode"] else None
        if parent is not None and not _active(parent):
            continue
        pairs.append({parent_key: parent if hit["kind"] == leaf_kind else hit,
                      leaf_key: hit if hit["kind"] == leaf_kind else None,
                      "rank": hit["rank"], "evidence": hit["evidence"], "sources": ["classifier"],
                      "ambiguous": False, "needsChoice": True, "confident": False,
                      "reason": "not_top_candidate", "_hit": hit})
    pairs.sort(key=lambda pair: _hit_key(pair["_hit"]))
    if not pairs:
        return []
    top = pairs[0]
    first = top["_hit"]
    # A linked broad parent is not a competing leaf; all OTHER branches compete globally.
    runner = next((p for p in pairs[1:] if not (
        top[leaf_key] and not p[leaf_key] and top[parent_key] and p[parent_key]
        and p[parent_key]["code"] == top[parent_key]["code"])), None)
    incomplete = bool(top[leaf_key] and not top[parent_key])
    jev = first.get("jevScore")
    if isinstance(jev, (int, float)) and not isinstance(jev, bool):
        runner_jev = runner["_hit"].get("jevScore") if runner else None
        runner_jev = runner_jev if isinstance(runner_jev, (int, float)) and not isinstance(runner_jev, bool) else 0.0
        close = bool(runner) and (float(jev) - float(runner_jev) < 0.12)
        strong = float(jev) >= 0.35
        reason = ("incomplete_pair" if incomplete else "ambiguous_classifier" if close else
                  "weak_evidence" if not strong else None)
    else:
        tier = _TIERS[first["evidence"]]
        close = False
        if runner:
            second = runner["_hit"]
            other_tier = _TIERS[second["evidence"]]
            close = other_tier == tier
            if tier == _TIERS["semantic"]:
                close = (first["score"] or 0) - (second["score"] or 0) < 0.08
        # No RRF-to-probability mapping. Cosine is only a similarity, not confidence.
        strong = tier >= _TIERS["name"] and not first.get("_typo")
        semantic_strong = first["evidence"] == "semantic" and (first["score"] or 0) >= 0.82
        reason = ("incomplete_pair" if incomplete else "ambiguous_classifier" if close else
                  "typo_match" if first.get("_typo") else "weak_evidence" if not (strong or semantic_strong) else None)
    top.update(confident=reason is None, needsChoice=reason is not None, ambiguous=close, reason=reason)
    if close:
        for pair in pairs[1:]:
            other = pair["_hit"]
            if isinstance(jev, (int, float)) and not isinstance(jev, bool):
                other_jev = other.get("jevScore")
                if isinstance(other_jev, (int, float)) and not isinstance(other_jev, bool) and float(jev) - float(other_jev) < 0.12:
                    pair.update(ambiguous=True, reason="ambiguous_classifier")
            elif _TIERS[pair["evidence"]] == _TIERS[first["evidence"]]:
                pair.update(ambiguous=True, reason="ambiguous_classifier")
    for pair in pairs:
        pair.pop("_hit")
    return pairs[:limit]


def _pair_okpd_ktru(hits: list[dict[str, Any]], conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    return _pairs(hits, conn, limit, "okpd", "ktru", "okpd2", "ktru")


def _pair_kpgz_spgz(hits: list[dict[str, Any]], conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    return _pairs(hits, conn, limit, "kpgz", "spgz", "kpgz", "spgz")


def pairs_from_hits(hits: list[dict[str, Any]], *, limit: int = 8) -> dict[str, list[dict[str, Any]]]:
    """Build OKPD/KTRU and KPGZ/SPGZ pairs from an already-filtered hit shortlist."""
    conn = open_classifier_db(readonly=True)
    try:
        return {
            "okpdKtru": _pair_okpd_ktru(hits, conn, limit),
            "kpgzSpgz": _pair_kpgz_spgz(hits, conn, limit),
        }
    finally:
        conn.close()


async def search_classifiers(*, query: str, mode: Literal["hybrid", "semantic", "text"] = "hybrid",
                             limit: int = 20) -> dict[str, Any]:
    query = (query or "").strip()
    if not query or len(query) > 500:
        raise ValueError("Запрос должен содержать от 1 до 500 символов")
    if mode not in {"hybrid", "semantic", "text"}:
        raise ValueError("Неизвестный режим поиска")
    limit = max(1, min(int(limit), 50))
    started = time.perf_counter()
    conn = open_classifier_db(readonly=True)
    try:
        validate_classifier_index(conn, require_embeddings=mode != "text")
        exact = _exact_boosts(conn, query)
        textual = _text_hits(conn, query, settings.retrieval_limit) if mode in {"hybrid", "text"} else []
        semantic = await _semantic_hits(conn, query, settings.retrieval_limit) if mode in {"hybrid", "semantic"} else []
        fused = _fuse(exact, semantic, textual)
        okpd_ktru = _pair_okpd_ktru(fused, conn, limit)
        kpgz_spgz = _pair_kpgz_spgz(fused, conn, limit)
        tops = [pairs[0] for pairs in (okpd_ktru, kpgz_spgz) if pairs]
        needs_choice = bool(tops) and (any(pair["ambiguous"] for pair in tops)
                                      or not any(pair["confident"] for pair in tops))
        for hit in fused:
            hit.pop("_typo", None)
        return {"query": query, "mode": mode, "okpdKtru": okpd_ktru, "kpgzSpgz": kpgz_spgz,
                "hits": fused[:limit], "needsChoice": needs_choice,
                "durationMs": round((time.perf_counter() - started) * 1000)}
    finally:
        conn.close()
