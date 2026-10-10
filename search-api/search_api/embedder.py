from __future__ import annotations

import asyncio
import json
import signal
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import sqlite3
import lmdb
from tqdm import tqdm

from search_api.config import settings
from search_api.embeddings import (
    cached_document_vectors,
    embed_client,
    embed_texts,
    open_sku_vector_store,
)
from search_api.normalize import document_hash, embedding_document
from search_api.ste_catalog import find_product, get_catalog, product_count


@dataclass(slots=True)
class _ReadyBatch:
    rows: list[sqlite3.Row]
    documents: list[str]
    product_ids: list[int]


def _prepare_batch(rows: list[sqlite3.Row], *, by_sku: bool) -> _ReadyBatch | None:
    documents: list[str] = []
    product_ids: list[int] = []
    ready: list[sqlite3.Row] = []
    for row in rows:
        product = find_product(int(row["id"]))
        if product is None:
            continue
        if by_sku:
            ready.append(row)
            product_ids.append(int(row["id"]))
            documents.append("")
            continue
        document = embedding_document(product)
        if document_hash(document) != row["hash"]:
            continue
        documents.append(document)
        product_ids.append(int(row["id"]))
        ready.append(row)
    if not ready:
        return None
    return _ReadyBatch(rows=ready, documents=documents, product_ids=product_ids)


async def embed_pending(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    batch_size: int | None = None,
    concurrency: int | None = None,
    cache_path: Path | None = None,
    sku_embeddings: Path | None = None,
) -> int:
    batch_size = batch_size or settings.embed_batch_size
    concurrency = concurrency or settings.embed_concurrency
    if batch_size < 1 or batch_size > 1024:
        raise ValueError("--batch: 1–1024")
    if concurrency < 1 or concurrency > 32:
        raise ValueError("--concurrency: 1–32")
    if limit is not None and limit < 1:
        raise ValueError("--limit должен быть положительным целым")

    owner = f"{uuid.uuid4()}"
    stopped = False

    def _stop(*_args):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    def lease() -> None:
        row = conn.execute("SELECT value FROM metadata WHERE key='indexer_lease'").fetchone()
        if row:
            lock = json.loads(row["value"])
            if lock.get("owner") != owner and lock.get("expires", 0) > time.time() * 1000:
                raise RuntimeError("Индексатор уже запущен в другом процессе")
        expires = int(time.time() * 1000) + 600_000
        with conn:
            conn.execute(
                "INSERT INTO metadata(key,value) VALUES('indexer_lease',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps({"owner": owner, "expires": expires}),),
            )

    def write_batch(ready: list[sqlite3.Row], vectors: list[bytes]) -> int:
        written = 0
        with conn:
            for row, vector in zip(ready, vectors, strict=True):
                current = conn.execute(
                    "SELECT content_hash AS hash FROM index_state WHERE product_id=?",
                    (row["id"],),
                ).fetchone()
                if not current or current["hash"] != row["hash"]:
                    continue
                conn.execute("DELETE FROM product_vectors WHERE product_id=?", (row["id"],))
                conn.execute(
                    "INSERT INTO product_vectors(product_id,embedding,category_id) VALUES(?,?,?)",
                    (row["id"], vector, row["categoryId"]),
                )
                conn.execute(
                    "UPDATE index_state SET embedded_hash=? WHERE product_id=? AND content_hash=?",
                    (row["hash"], row["id"], row["hash"]),
                )
                written += 1
        return written

    try:
        lease()
        total = int(
            conn.execute(
                "SELECT count(*) AS n FROM index_state WHERE embedded_hash IS NULL OR embedded_hash <> content_hash"
            ).fetchone()["n"]
        )
        target = total if limit is None else min(total, limit)
        if target == 0:
            print("Нечего индексировать.")
            return 0

        print(f"{settings.model_id}: загрузка parquet…", flush=True)
        get_catalog()
        print(
            f"Каталог: {product_count():,} · к эмбеддингу: {target:,} · "
            f"batch={batch_size} · concurrency={concurrency}".replace(",", "\u00a0"),
            flush=True,
        )

        completed = 0
        started = time.perf_counter()
        embed_ms = 0.0
        database_ms = 0.0

        sku_path = sku_embeddings if sku_embeddings is not None else (
            settings.sku_embeddings if settings.sku_embeddings.exists() else None
        )
        by_sku = sku_path is not None
        if by_sku and cache_path is not None:
            raise ValueError("Задайте либо --sku-embeddings, либо --cache, не оба")
        if by_sku:
            print(f"SKU-эмбеддинги: {sku_path}", flush=True)

        with tqdm(
            total=target,
            unit="шт",
            unit_scale=False,
            dynamic_ncols=True,
            bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}{postfix}]",
        ) as bar, ExitStack() as resources:
            cache_transaction = None
            sku_store = None
            if by_sku:
                assert sku_path is not None
                sku_store = open_sku_vector_store(sku_path, resources)
            elif cache_path is not None:
                cache = lmdb.open(str(cache_path.resolve()), readonly=True, lock=False, create=False)
                resources.callback(cache.close)
                cache_transaction = resources.enter_context(cache.begin())
            async with embed_client() as client:
                while not stopped and (limit is None or completed < limit):
                    lease()
                    remaining = target - completed if limit is not None else batch_size * concurrency
                    take = min(batch_size * concurrency, remaining)
                    if take <= 0:
                        break
                    rows = conn.execute(
                        """
                        SELECT product_id AS id, content_hash AS hash, category_id AS categoryId
                        FROM index_state
                        WHERE embedded_hash IS NULL OR embedded_hash <> content_hash
                        ORDER BY product_id
                        LIMIT ?
                        """,
                        (take,),
                    ).fetchall()
                    if not rows:
                        break

                    chunks: list[list[sqlite3.Row]] = [
                        rows[i : i + batch_size] for i in range(0, len(rows), batch_size)
                    ]
                    prepared = [_prepare_batch(chunk, by_sku=by_sku) for chunk in chunks]
                    prepared = [b for b in prepared if b is not None]
                    if not prepared:
                        continue

                    bar.set_postfix_str(f"embed×{len(prepared)}…", refresh=True)

                    async def run_one(batch: _ReadyBatch) -> tuple[_ReadyBatch, list[bytes], float]:
                        t0 = time.perf_counter()
                        if sku_store is not None:
                            vectors = sku_store.get_vectors(batch.product_ids)
                        elif cache_transaction is not None:
                            vectors = cached_document_vectors(cache_transaction, batch.documents)
                        else:
                            vectors = await embed_texts(batch.documents, "document", client=client)
                        return batch, vectors, time.perf_counter() - t0

                    results = await asyncio.gather(*[run_one(b) for b in prepared])

                    bar.set_postfix_str("sqlite…", refresh=False)
                    wave_written = 0
                    for batch, vectors, dt in results:
                        embed_ms += dt
                        t1 = time.perf_counter()
                        written = write_batch(batch.rows, vectors)
                        database_ms += time.perf_counter() - t1
                        wave_written += written

                    completed += wave_written
                    bar.update(wave_written)
                    seconds = max(time.perf_counter() - started, 1e-9)
                    bar.set_postfix_str(
                        f"×{concurrency} · embed {embed_ms:.0f}s · db {database_ms:.0f}s · "
                        f"{completed / seconds:.1f}/s",
                        refresh=True,
                    )

            if stopped:
                bar.set_postfix_str("остановлено", refresh=True)

        print("Индекс сохранён. Повторный запуск продолжит с оставшихся карточек.")
        return completed
    finally:
        with conn:
            conn.execute(
                "DELETE FROM metadata WHERE key='indexer_lease' AND json_extract(value,'$.owner')=?",
                (owner,),
            )


def embed_pending_sync(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    batch_size: int | None = None,
    concurrency: int | None = None,
    cache_path: Path | None = None,
    sku_embeddings: Path | None = None,
) -> int:
    return asyncio.run(
        embed_pending(
            conn,
            limit=limit,
            batch_size=batch_size,
            concurrency=concurrency,
            cache_path=cache_path,
            sku_embeddings=sku_embeddings,
        )
    )
