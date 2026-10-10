"""Lossless schema and typed LLM output for the retrieval/extraction pipeline."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, StrictStr


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class NumericRule(StrictModel):
    op: Literal["=", ">", ">=", "<", "<="]
    value: float


class PipelineField(StrictModel):
    id: str
    name: str
    type: Literal["number", "enum", "text"]
    unit: str | None = None
    values: list[str] = Field(default_factory=list)
    # OR of alternatives; each alternative is an AND of comparisons.
    alternatives: list[list[NumericRule]] = Field(default_factory=list)
    pattern: str | None = None
    required: bool = False


class PipelineSchema(StrictModel):
    code: str
    name: str
    catalog: Literal["ktru", "kpgz"] = "ktru"
    fields: list[PipelineField]
    # Unknown policy is not permission to add characteristics.
    additional_allowed: bool | None = None
    recommended: list[PipelineField] = Field(default_factory=list)


class Neighbor(StrictModel):
    id: int
    name: str
    score: float
    schema_codes: list[str]


class Retrieval(StrictModel):
    schemas: list[PipelineSchema]
    neighbors: list[Neighbor]
    truncated: bool = False
    search_details: dict[str, Any] = Field(default_factory=dict)


class QueryViews(StrictModel):
    identity: str
    purpose: str
    specifications: str


class RankedNeighbor(StrictModel):
    id: int
    reason: str


class NeighborRanking(StrictModel):
    selected: list[RankedNeighbor]


class Selection(StrictModel):
    code: str | None
    reason: str


class ExtractedValue(StrictModel):
    field_id: str
    # Numbers MUST be in the source unit, never pre-converted by the LLM.
    value: StrictInt | StrictFloat | StrictStr
    source_unit: str | None
    source_literal: str | None
    quote: str


class AdditionalValue(StrictModel):
    name: str
    type: Literal["number", "enum", "text"]
    value: StrictInt | StrictFloat | StrictStr
    source_unit: str | None
    source_literal: str | None
    quote: str


class Extraction(StrictModel):
    fields: list[ExtractedValue]
    additional: list[AdditionalValue]
