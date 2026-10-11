from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

from search_api import search as search_mod
from search_api.cancel_on_disconnect import CancelOnDisconnectMiddleware
from search_api.catalog_characteristics import frequent_catalog_characteristics
from search_api.characteristic_autofill import (
    AutofillConfigError, AutofillError, AutofillRequest, autofill_characteristics,
)
from search_api.config import settings
from search_api.database import close_database, warm_search
from search_api.images import get_image
from search_api.ste_catalog import category_diagnostics, find_product, product_to_dict

logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    started = time.perf_counter()
    try:
        warmed = await asyncio.to_thread(warm_search)
        logger.info(
            "Search ready in %.2fs: products=%s mmap=%s cache_kib=%s",
            time.perf_counter() - started,
            warmed["products"], warmed["mmap_size"], warmed["cache_size_kib"],
        )
        yield
    finally:
        close_database()


app = FastAPI(title="zakupki-search-api", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(CancelOnDisconnectMiddleware)


class SearchBody(BaseModel):
    query: str = ""
    mode: str = "hybrid"
    page: int = 1
    categoryId: int | None = None


@app.get("/health")
def health():
    return {"ok": True, "model": settings.model_id}


@app.get("/catalog/stats")
def catalog_stats():
    try:
        return search_mod.catalog_stats()
    except FileNotFoundError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/catalog/categories/diagnostics")
def catalog_category_diagnostics():
    try:
        return category_diagnostics()
    except FileNotFoundError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/catalog/characteristics")
def catalog_characteristics(kpgzCode: str = Query(min_length=1, max_length=100)):
    try:
        return frequent_catalog_characteristics(kpgzCode)
    except FileNotFoundError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/catalog/search")
async def catalog_search(body: SearchBody):
    try:
        return await search_mod.run_search(
            query=body.query,
            mode=body.mode,  # type: ignore[arg-type]
            page=body.page,
            category_id=body.categoryId,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(503, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


class CategorySuggestionBody(BaseModel):
    description: str
    force: bool = False


@app.post("/categories/suggest")
async def categories_suggest(body: CategorySuggestionBody):
    from search_api.category_suggestion import suggest_categories

    try:
        return await suggest_categories(body.description, force=body.force)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(503, str(exc)) from exc


class ClassifierSearchBody(BaseModel):
    query: str = ""
    mode: str = "hybrid"
    limit: int = 20


@app.post("/classifiers/search")
async def classifiers_search(body: ClassifierSearchBody):
    from search_api.classifier_search import search_classifiers

    try:
        return await search_classifiers(query=body.query, mode=body.mode, limit=body.limit)  # type: ignore[arg-type]
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(503, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/characteristics/autofill")
async def characteristic_autofill(body: AutofillRequest):
    try:
        return await autofill_characteristics(body)
    except AutofillConfigError as exc:
        raise HTTPException(503, str(exc)) from exc
    except AutofillError as exc:
        raise HTTPException(502, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/catalog/products/{product_id}")
def product_detail(product_id: int):
    product = find_product(product_id)
    if product is None:
        raise HTTPException(404, "Товар не найден")
    return product_to_dict(product)


@app.get("/catalog/products/{product_id}/similar")
async def product_similar(product_id: int):
    try:
        return await search_mod.find_similar(product_id)
    except FileNotFoundError as exc:
        raise HTTPException(503, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/images/{image_id}")
async def image_proxy(image_id: int):
    """Serve catalog images with on-disk cache. On FastAPI because Nitro/Vite
    returns 404 for `<img>` → `/api/images/*` when Sec-Fetch-Dest: image."""
    try:
        data, media_type = await get_image(image_id)
    except FileNotFoundError:
        raise HTTPException(404, "Not found") from None
    except (RuntimeError, ValueError):
        raise HTTPException(502, "Image unavailable") from None
    return Response(
        content=data,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=604800", "X-Content-Type-Options": "nosniff"},
    )
