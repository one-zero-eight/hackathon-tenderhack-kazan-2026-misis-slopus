"""Category classification router shared by both API applications."""
from __future__ import annotations

import threading
from typing import Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from .category_classifier import CategoryClassifier, MAX_TEXT, default_category_classifier
from .category_index import CategoryIndexError
from .neighbor_search import EmbedError
from .pipeline import PipelineError


class CategoryRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    candidates_k: int = Field(default=12, ge=2, le=30)
    details: bool = False

    @field_validator("text")
    @classmethod
    def nonblank(cls, text: str) -> str:
        if not text.strip():
            raise ValueError("текст не должен быть пустым")
        return text


def create_category_router(factory: Callable[[], CategoryClassifier] = default_category_classifier) -> APIRouter:
    router = APIRouter()
    instance: CategoryClassifier | None = None
    lock = threading.Lock()

    @router.post("/classify/category")
    def classify(req: CategoryRequest) -> dict:
        nonlocal instance
        with lock:
            if instance is None:
                try:
                    instance = factory()
                except (CategoryIndexError, ValueError) as exc:
                    raise HTTPException(503, str(exc)) from exc
        try:
            return instance.classify(req.text, candidates_k=req.candidates_k, details=req.details)
        except (PipelineError, EmbedError) as exc:
            raise HTTPException(502, str(exc)) from exc
        except CategoryIndexError as exc:
            raise HTTPException(503, str(exc)) from exc

    return router
