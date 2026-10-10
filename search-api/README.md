# zakupki-search-api

Поиск по каталогу СТЕ: FTS5 + sqlite-vec + [meow-embed](https://github.com/dantetemplar/meow-embed) / [EmbeddingGemma-2](https://huggingface.co/google/embeddinggemma-2).

**Источники parquet (по умолчанию):** `Инструменты_from_api`, `Мебель_from_api`, `Одежда_from_api`.  
`ste.parquet` подключается только если задан `STE_PARQUET` (дубликаты id не перезаписывают категории).

КТРУ, детальные карточки в UI и картинки — в `search-web` (Solid); карточка товара для UI читается из этого API.

## Быстрый старт

```bash
cd search-api
uv sync

uv run zakupki-search-index import   # FTS из категорий
# uv run zakupki-search-index embed # когда нужны векторы
uv run zakupki-search-api            # http://127.0.0.1:8070
```

## Индекс

```bash
uv run zakupki-search-index              # import + embed
uv run zakupki-search-index import
uv run zakupki-search-index embed \
  --limit 1000 --batch 256 --concurrency 4

# Старые эмбеддинги по sku id (Git LFS: data/sku_embeddings.parquet):
# uv run scripts/export_sku_embeddings.py --data-dir ../data \
#   --meow-cache /path/to/embedding-cache.lmdb --output ../data/sku_embeddings.lmdb
# uv run scripts/pack_sku_embeddings_parquet.py \
#   --input ../data/sku_embeddings.lmdb --output ../data/sku_embeddings.parquet
uv run zakupki-search-index embed --sku-embeddings ../data/sku_embeddings.parquet
```

## Env

| Variable | Default |
|----------|---------|
| `CATEGORY_PARQUETS` | `../data/Инструменты_from_api.parquet,../data/Мебель_from_api.parquet,../data/Одежда_from_api.parquet` |
| `STE_PARQUET` | _(пусто — ste не грузится)_ |
| `SEARCH_DATABASE` | `../search-web/data/search-index.sqlite` |
| `IMAGE_CACHE_DIR` | `<рядом с SEARCH_DATABASE>/images` |
| `MEOW_EMBED_URL` | `https://api.innohassle.ru/meow-embed` |
| `EMBED_BATCH_SIZE` | `256` |
| `EMBED_CONCURRENCY` | `4` |
| `HOST` / `PORT` | `127.0.0.1` / `8070` |
| `CORS_ORIGINS` | `http://127.0.0.1:3000,http://localhost:3000` |
| `OPENAI_BASE_URL` | `https://api.innohassle.ru/llama/v1` |
| `OPENAI_MODEL` | `ggml-org/gemma-4-E4B-it-GGUF:Q8_0` |
| `OPENAI_TIMEOUT` | `120` |
| `TYPESAFE_API_KEY` | _(или `secrets/typesafe-token`)_ |
| `TYPESAFE_BASE_URL` | `https://api.typesafe.ai` |
| `TYPESAFE_MODEL` | `jev-latest` |
| `TYPESAFE_CONFIDENCE` | `0.85` |

Пример со ste:

```bash
STE_PARQUET=../data/ste.parquet uv run zakupki-search-api
```

## API

- `GET /health`
- `GET /catalog/stats`
- `POST /catalog/search` — `{ query, mode: text|semantic|hybrid, page, categoryId }`
- `GET /catalog/products/{id}`
- `GET /catalog/products/{id}/similar`
- `GET /images/{id}` — прокси картинок (не через Solid: Nitro ломает `<img>` на `/api/images`)
