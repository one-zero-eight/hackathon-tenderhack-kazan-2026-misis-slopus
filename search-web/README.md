# Поиск по каталогу СТЕ

UI для каталога стандартных товарных единиц [Портала поставщиков Москвы](https://zakupki.mos.ru/).

**Поиск** (FTS / semantic / hybrid / similar) — отдельный FastAPI backend: [`../search-api`](../search-api).  
**КТРУ**, карточки товаров и прокси картинок — этот Solid-сервер (Parquet через nodejs-polars).

## Возможности

- Каталог: режимы «по словам» / «по смыслу» / гибрид, фильтр по категории, пагинация.
- Карточка СТЕ и до шести похожих товаров (similar — через API).
- Дерево и пикер КТРУ из `ktru-actual.parquet`.
- Картинки с Портала поставщиков + локальный дисковый кеш (`/api/images/...`).

Стек UI: TypeScript, SolidStart 2 / SolidJS, Vite 8, Nitro, Tailwind CSS 4, Kobalte.  
Компоненты: [`src/ui-kit`](src/ui-kit/README.md), демо — `/ui-kit`.

## Требования

- Node.js **24+**, pnpm **11.18.0**
- Python **3.12+** / [uv](https://docs.astral.sh/uv/) для `search-api`
- Parquet: `../data/ste.parquet`, `ktru-actual.parquet`, `okpd2-moy-zakupki.parquet` ([данные](../data/README.md))
- Для semantic: доступный meow-embed с EmbeddingGemma-2 (по умолчанию удалённый API)

## Установка

```bash
cd search-web
pnpm install --frozen-lockfile
cp .env.example .env
```

```bash
git lfs pull --include="data/ste.parquet,data/ktru-actual.parquet,data/okpd2-moy-zakupki.parquet"
```

### `.env`

| Variable | Назначение |
|----------|------------|
| `SEARCH_API_URL` | FastAPI для Solid-сервера, по умолчанию `http://127.0.0.1:8070`. Браузер обращается только к `/api/search/...` на сервере UI |
| `CATEGORY_PARQUETS` | Основные parquet (по умолчанию Инструменты/Мебель/Одежда) |
| `STE_PARQUET` | Опциональный доп. ste; пусто = не грузить |
| `KTRU_PARQUET` / `OKPD2_PARQUET` | Дерево КТРУ |
| `SEARCH_DATABASE` | Путь к индексу (кеш картинок рядом); индекс собирает `search-api` |
| `MEOW_EMBED_URL` | Только для локального `meow:start` / бенчей; поиск эмбеддит API |

## Запуск

### 1. Индекс + API (`search-api`)

```bash
cd ../search-api
uv sync
uv run zakupki-search-index          # import + embed
uv run zakupki-search-api            # :8070
```

Подробности — в [README search-api](../search-api/README.md).

Опционально локальный эмбеддер:

```bash
cd ../search-web
pnpm run meow:start                  # :8067
# в search-api: MEOW_EMBED_URL=http://127.0.0.1:8067
```

### 2. UI

```bash
cd search-web
pnpm run dev                         # http://127.0.0.1:3000
```

- `/` — создание товара / пикер КТРУ  
- `/catalog` — поиск  
- `/ktru` — дерево КТРУ  
- `/products/:id` — карточка  

Production:

```bash
pnpm run build && pnpm start         # + запущенный zakupki-search-api
```

## Проверки

```bash
pnpm run typecheck
pnpm test
pnpm run bench:embed --batches 8,16,32,64   # нужен meow-embed
```

Live-эмбеддинги:

```bash
LIVE_EMBED=1 node --env-file-if-exists=.env --import tsx --test tests/live.test.ts
```

## Ограничения

- Semantic/hybrid без векторов или без meow-embed на стороне API — явная ошибка; текстовый режим работает после `zakupki-search-index import`.
- Индекс привязан к EmbeddingGemma-2 (768d) и формату `title:…|text:…`; старый BERTA-индекс несовместим.
- Изображения не в снимке данных — качаются по запросу.
- Полнота полей и цен зависит от исходных карточек.
