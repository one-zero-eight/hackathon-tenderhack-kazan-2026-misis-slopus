from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from search_api.config import settings
from search_api.embedder import embed_pending_sync
from search_api.importer import import_products
from search_api.index_db import open_index_db


def _embed_classifier_stage(args: argparse.Namespace) -> int:
    import asyncio

    from search_api.classifier_search import (
        classifier_staging_path,
        embed_classifier_pending,
        open_classifier_db,
        publish_classifier_index,
    )

    output = args.output or settings.classifier_database
    stage = classifier_staging_path(output)
    if not stage.exists():
        raise FileNotFoundError(f"Нет незавершённой сборки: {stage}. Запустите classifiers --embed")
    embedded = asyncio.run(embed_classifier_pending(
        path=stage, limit=args.limit, batch_size=args.batch, concurrency=args.concurrency,
        embeddings=args.embeddings,
    ))
    print(f"Эмбеддинги справочников: {embedded:,}".replace(",", "\u00a0"))
    conn = open_classifier_db(stage, readonly=True)
    try:
        complete = conn.execute("SELECT value FROM metadata WHERE key='embeddings_complete'").fetchone()[0] == "1"
    finally:
        conn.close()
    if complete:
        publish_classifier_index(output=output)
        print(f"Опубликован полный индекс: {output}")
    else:
        print(f"Неполная сборка сохранена: {stage}; опубликованный индекс не изменён.")
        print(f"Продолжить: uv run zakupki-search-index classifiers-embed --output {output}")
    return 0


def _cmd_classifiers(args: argparse.Namespace) -> int:
    from search_api.classifier_search import build_classifier_index

    started = time.perf_counter()
    counts = build_classifier_index(output=args.output, publish=not args.embed)
    print(
        "Справочники: "
        + ", ".join(f"{kind}={count:,}" for kind, count in counts.items()).replace(",", "\u00a0")
        + f" → {args.output or settings.classifier_database}"
    )
    if args.embed:
        _embed_classifier_stage(args)
    print(f"Готово за {time.perf_counter() - started:.1f} с")
    return 0


def _cmd_import(_args: argparse.Namespace) -> int:
    from search_api.ste_catalog import catalog_paths

    for path in catalog_paths():
        print(f"Источник: {path}")
    if settings.ste_parquet is None:
        print("STE_PARQUET не задан — ste.parquet не подключается")
    conn = open_index_db()
    try:
        started = time.perf_counter()

        def progress(n: int) -> None:
            if n % 5000 == 0:
                print(f"Импортировано {n:,} карточек".replace(",", "\u00a0"))

        count = import_products(conn, on_progress=progress)
        elapsed = time.perf_counter() - started
        print(
            f"Готово: {count:,} карточек за {elapsed:.1f} с → {settings.search_database}".replace(",", "\u00a0")
        )
        return 0
    finally:
        conn.close()


def _cmd_embed(args: argparse.Namespace) -> int:
    conn = open_index_db()
    try:
        embed_pending_sync(
            conn,
            limit=args.limit,
            batch_size=args.batch,
            concurrency=args.concurrency,
            cache_path=args.cache,
            sku_embeddings=args.sku_embeddings,
        )
        return 0
    finally:
        conn.close()


def _cmd_index(args: argparse.Namespace) -> int:
    code = _cmd_import(args)
    if code != 0:
        return code
    return _cmd_embed(args)


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["all"]
    elif argv[0] not in ("import", "embed", "all", "classifiers", "classifiers-embed", "-h", "--help"):
        argv = ["all", *argv]

    parser = argparse.ArgumentParser(prog="zakupki-search-index", description="FTS + vector index for STE catalog")
    sub = parser.add_subparsers(dest="command", required=True)

    p_cls = sub.add_parser("classifiers", help="Build hybrid OKPD2/KTRU/KPGZ/SPGZ search index")
    p_cls.add_argument("--output", type=Path, default=None)
    p_cls.add_argument("--embed", action="store_true", help="Embed pending classifier documents")
    p_cls.add_argument("--limit", type=int, default=None)
    p_cls.add_argument("--batch", type=int, default=None)
    p_cls.add_argument("--concurrency", type=int, default=None)
    p_cls.add_argument(
        "--embeddings",
        type=Path,
        default=None,
        help="Load vectors by content_hash from .parquet (default: data/classifier_embeddings.parquet if present)",
    )
    p_cls.set_defaults(func=_cmd_classifiers)

    p_cls_embed = sub.add_parser("classifiers-embed", help="Resume staged classifier embeddings and publish when complete")
    p_cls_embed.add_argument("--output", type=Path, default=None)
    p_cls_embed.add_argument("--limit", type=int, default=None)
    p_cls_embed.add_argument("--batch", type=int, default=None)
    p_cls_embed.add_argument("--concurrency", type=int, default=None)
    p_cls_embed.add_argument(
        "--embeddings",
        type=Path,
        default=None,
        help="Load vectors by content_hash from .parquet (default: data/classifier_embeddings.parquet if present)",
    )
    p_cls_embed.set_defaults(func=_embed_classifier_stage)

    p_import = sub.add_parser("import", help="Build FTS + index_state from STE parquet")
    p_import.set_defaults(func=_cmd_import)

    p_embed = sub.add_parser("embed", help="Embed pending rows via meow-embed")
    p_embed.add_argument("--limit", type=int, default=None)
    p_embed.add_argument("--batch", type=int, default=None)
    p_embed.add_argument("--concurrency", type=int, default=None)
    p_embed.add_argument("--cache", type=Path, help="Load vectors only from an existing meow-embed LMDB cache")
    p_embed.add_argument(
        "--sku-embeddings",
        type=Path,
        default=None,
        help="Load vectors by product_id from .parquet or LMDB (default: data/sku_embeddings.parquet if present)",
    )
    p_embed.set_defaults(func=_cmd_embed)

    p_all = sub.add_parser("all", help="import then embed")
    p_all.add_argument("--limit", type=int, default=None)
    p_all.add_argument("--batch", type=int, default=None)
    p_all.add_argument("--concurrency", type=int, default=None)
    p_all.add_argument("--cache", type=Path, help="Load vectors only from an existing meow-embed LMDB cache")
    p_all.add_argument(
        "--sku-embeddings",
        type=Path,
        default=None,
        help="Load vectors by product_id from .parquet or LMDB (default: data/sku_embeddings.parquet if present)",
    )
    p_all.set_defaults(func=_cmd_index)

    args = parser.parse_args(argv)
    try:
        raise SystemExit(args.func(args))
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
