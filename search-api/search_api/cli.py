from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from search_api.config import settings
from search_api.embedder import embed_pending_sync
from search_api.importer import import_products
from search_api.index_db import open_index_db


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
    elif argv[0] not in ("import", "embed", "all", "-h", "--help"):
        argv = ["all", *argv]

    parser = argparse.ArgumentParser(prog="zakupki-search-index", description="FTS + vector index for STE catalog")
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="Build FTS + index_state from STE parquet")
    p_import.set_defaults(func=_cmd_import)

    p_embed = sub.add_parser("embed", help="Embed pending rows via meow-embed")
    p_embed.add_argument("--limit", type=int, default=None)
    p_embed.add_argument("--batch", type=int, default=None)
    p_embed.add_argument("--concurrency", type=int, default=None)
    p_embed.add_argument("--cache", type=Path, help="Load vectors only from an existing meow-embed LMDB cache")
    p_embed.set_defaults(func=_cmd_embed)

    p_all = sub.add_parser("all", help="import then embed")
    p_all.add_argument("--limit", type=int, default=None)
    p_all.add_argument("--batch", type=int, default=None)
    p_all.add_argument("--concurrency", type=int, default=None)
    p_all.add_argument("--cache", type=Path, help="Load vectors only from an existing meow-embed LMDB cache")
    p_all.set_defaults(func=_cmd_index)

    args = parser.parse_args(argv)
    try:
        raise SystemExit(args.func(args))
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
