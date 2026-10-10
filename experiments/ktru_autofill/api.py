"""LLM + Jev pipeline API: uvicorn ktru_autofill.api:app.

Also available at POST /fill/pipeline in the main `ktru-api` application.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Literal

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

from .jev_client import JevError
from .neighbor_search import EmbedError
from .pipeline import AutofillPipeline, PipelineError, default_pipeline
from .pipeline_retrieval import PipelineConfigError


class FillRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    neighbors_k: int = Field(default=25, ge=1, le=100)
    schemas_k: int = Field(default=20, ge=1, le=100)
    details: bool = False
    search: Literal["single", "multi_query"] = "single"

    @field_validator("text")
    @classmethod
    def nonblank(cls, text: str) -> str:
        if not text.strip():
            raise ValueError("текст не должен быть пустым")
        return text


def create_pipeline_router(factory: Callable[[], AutofillPipeline] = default_pipeline,
                           *, path: str = "/fill") -> APIRouter:
    router = APIRouter()
    instance: AutofillPipeline | None = None
    lock = threading.Lock()

    @router.post(path)
    def fill(req: FillRequest) -> dict[str, Any]:
        nonlocal instance
        with lock:
            if instance is None:
                try:
                    instance = factory()
                except (PipelineConfigError, JevError, ValueError) as e:
                    raise HTTPException(503, str(e)) from e
        try:
            return instance.fill(req.text, neighbors_k=req.neighbors_k,
                                 schemas_k=req.schemas_k, details=req.details, search=req.search)
        except (PipelineError, EmbedError) as e:
            raise HTTPException(502, str(e)) from e

    return router


def create_app(factory: Callable[[], AutofillPipeline] = default_pipeline) -> FastAPI:
    app = FastAPI(title="КТРУ: Embeddings → LLM → Jev → валидация")
    from .api_category import create_category_router
    from .api_extract import create_extract_router

    app.include_router(create_pipeline_router(factory))
    app.include_router(create_category_router())
    app.include_router(create_extract_router())
    return app


app = create_app()
