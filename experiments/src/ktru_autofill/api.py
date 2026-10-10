"""HTTP API: uvicorn ktru_autofill.api:app"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from .autofill import Autofiller

app = FastAPI(title="КТРУ: автозаполнение характеристик")
_filler: Autofiller | None = None


class FillRequest(BaseModel):
    text: str
    candidates: list[dict[str, Any]]
    details: bool = False


def filler() -> Autofiller:
    global _filler
    if _filler is None:
        _filler = Autofiller()
    return _filler


@app.post("/fill")
def fill(req: FillRequest) -> dict[str, Any]:
    return filler().fill(req.text, req.candidates).to_dict(details=req.details)
