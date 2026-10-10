"""POST /extract: код позиции КТРУ + описание пользователя → готовая схема характеристик для фронтенда."""
from __future__ import annotations

import threading
from typing import Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from .extract import STRATEGIES, SchemaExtractor, Strategy, default_extractor
from .jev_client import JevError
from .neighbor_search import EmbedError
from .pipeline import PipelineError
from .pipeline_retrieval import PipelineConfigError


class ExtractRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64, description="код позиции КТРУ (category.code из /classify/category)")
    text: str = Field(min_length=1, max_length=20000, description="описание товара пользователем")
    strategy: Strategy = Field("jev", description=f"способ извлечения: {', '.join(STRATEGIES)}")
    details: bool = False

    @field_validator("code", "text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("значение не должно быть пустым")
        return value.strip()


def create_extract_router(factory: Callable[[], SchemaExtractor] = default_extractor) -> APIRouter:
    router = APIRouter()
    instance: SchemaExtractor | None = None
    lock = threading.Lock()

    @router.post("/extract")
    def extract(req: ExtractRequest) -> dict:
        nonlocal instance
        with lock:
            if instance is None:
                try:
                    instance = factory()
                except (PipelineConfigError, JevError, ValueError, OSError) as e:
                    raise HTTPException(503, str(e)) from e
        if req.strategy not in instance.available():
            raise HTTPException(503, f"стратегия {req.strategy} не настроена (доступны: {', '.join(instance.available())})")
        try:
            return instance.extract(req.code, req.text, strategy=req.strategy, details=req.details)
        except KeyError as e:
            raise HTTPException(404, f"нет позиции КТРУ {e.args[0]}") from e
        except (PipelineError, EmbedError, JevError) as e:
            raise HTTPException(502, str(e)) from e

    return router
