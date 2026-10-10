"""Embedding-only STE retrieval and lossless raw KTRU schema adaptation.

Production startup loads an existing NPZ index only. Building the potentially
large LMDB index is an explicit offline operation, never a request-time fallback.
"""
from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Callable, Mapping
from functools import lru_cache
from itertools import islice
from pathlib import Path
from typing import Any

import numpy as np

from . import units
from .ktru_import import canonical_unit
from .ktru_store import DEFAULT_DATA, KtruStore
from .neighbor_search import DEFAULT_CACHE, DEFAULT_INDEX, MeowQueryEmbedder, NeighborIndex
from .pipeline_schema import Neighbor, NumericRule, PipelineField, PipelineSchema, Retrieval


class PipelineConfigError(RuntimeError):
    """Missing retrieval assets or a schema that cannot be represented safely."""


_OPERATORS = {
    "greaterOrEqual": ">=", "greater": ">",
    "lessOrEqual": "<=", "less": "<",
}


def _number(value: Any) -> float:
    try:
        result = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError) as exc:
        raise PipelineConfigError(f"Invalid KTRU numeric schema value: {value!r}") from exc
    if not math.isfinite(result):
        raise PipelineConfigError(f"Non-finite KTRU numeric schema value: {value!r}")
    return result


def _field_key(name: str) -> str:
    # Schema-name matching only; never applied to the product description.
    return re.sub(r"\W+", " ", name.casefold().replace("ё", "е")).strip()


def _raw_field(raw: dict[str, Any], position: int) -> PipelineField | None:
    if raw.get("actual") is False:
        return None
    name = (raw.get("name") or "").strip()
    if not name:
        return None
    values = [v for v in raw.get("values") or [] if v]
    texts = list(dict.fromkeys(v["text"].strip() for v in values if (v.get("text") or "").strip()))
    numeric = str(raw.get("type")) == "2" or any(
        v.get("ranges") or v.get("concrete_values") for v in values
    )
    unit_names = [canonical_unit((v.get("unit") or {}).get("name")) for v in values]
    unit = next((u for u in unit_names if u), None)
    alternatives: list[list[NumericRule]] = []
    if numeric:
        for value, source_unit in zip(values, unit_names):
            def convert(bound: Any) -> float:
                number = _number(bound)
                if source_unit and unit and source_unit != unit:
                    src, dst = units.canonical(source_unit), units.canonical(unit)
                    compatible = src and dst and (
                        src.dim == dst.dim or (
                            src.dim.startswith("count") and dst.dim.startswith("count")
                            and "шт" in (src.name, dst.name)
                        )
                    )
                    if not compatible:
                        raise PipelineConfigError(f"Incompatible schema units for {name}: {source_unit}, {unit}")
                    # Do not round strict bounds (units.convert rounds to six decimals).
                    number = number * src.factor / dst.factor
                return number

            for interval in value.get("ranges") or []:
                rules = []
                for side in ("min", "max"):
                    bound = interval.get(side)
                    if bound is None or bound == "":
                        continue
                    operator = interval.get(f"{side}_operator")
                    allowed = ("greater", "greaterOrEqual") if side == "min" else ("less", "lessOrEqual")
                    if operator not in allowed:
                        raise PipelineConfigError(f"Invalid KTRU {side} operator for {name}: {operator!r}")
                    rules.append(NumericRule(op=_OPERATORS[operator], value=convert(bound)))
                # An empty conjunction correctly represents an unrestricted interval.
                alternatives.append(rules)
            alternatives.extend(
                [NumericRule(op="=", value=convert(exact))]
                for exact in value.get("concrete_values") or []
            )
    return PipelineField(
        id=str(raw.get("code") or f"field:{position}"), name=name,
        type="number" if numeric else "enum" if texts else "text",
        unit=unit, values=texts, alternatives=alternatives,
        required=raw.get("required") is True,
    )


class SchemaBuilder:
    """Схема позиции КТРУ по коду: без индекса соседей и эмбеддингов."""

    def __init__(self, store: KtruStore, *,
                 additional_allowed: Mapping[str, bool | None] | None = None,
                 recommended_fields: Mapping[str, list[dict[str, Any]]] | None = None):
        self.store = store
        self.additional_allowed = dict(additional_allowed or {})
        self.recommended_fields = dict(recommended_fields or {})
        self._schema = lru_cache(maxsize=2048)(self._build_schema)

    def _build_schema(self, row: int) -> PipelineSchema:
        fields = []
        seen = set()
        for position, raw in enumerate(self.store._characteristics(row)):
            field = _raw_field(raw, position)
            if field is not None:
                if field.id in seen:
                    raise PipelineConfigError(f"Duplicate KTRU field ID in {self.store.codes[row]}: {field.id}")
                fields.append(field)
                seen.add(field.id)
        existing_names = {_field_key(f.name) for f in fields}
        existing_names.update(_field_key(f.name.split(",")[0]) for f in fields)
        recommended = []
        for rec in self.recommended_fields.get(self.store.codes[row], []):
            kind = {"число": "number", "перечень": "enum"}.get(rec.get("kind"))
            name = (rec.get("name") or "").strip()
            key = _field_key(name)
            if kind is None or not name or _number(rec.get("support", 0)) < .3:
                continue
            if key in existing_names or _field_key(name.split(",")[0]) in existing_names:
                continue
            field_id = f"recommended:{rec.get('key') or key}"
            if field_id in seen:
                continue
            recommended.append(PipelineField(
                id=field_id, name=name, type=kind,
                unit=canonical_unit(rec.get("unit")),
                values=list(dict.fromkeys(rec.get("values") or [])) if kind == "enum" else [],
            ))
            seen.add(field_id)
            existing_names.add(key)
        code = self.store.codes[row]
        return PipelineSchema(
            code=code, name=self.store.names[row], fields=fields,
            additional_allowed=self.additional_allowed.get(code), recommended=recommended,
        )

    def schema_for_code(self, code: str) -> PipelineSchema | None:
        """Схема активной позиции КТРУ по коду; None — позиции нет в каталоге."""
        row = self.store.row_of.get(code)
        return None if row is None else self._schema(row).model_copy(deep=True)


class PipelineRetriever(SchemaBuilder):
    """Callable ``(text, neighbors_k, schemas_k) -> Retrieval``.

    Schemas inherit the highest-ranked neighbor's priority; ties use stable STE
    IDs and schema codes. No name matching, LLM selection, or text extraction
    happens here. ``additional_allowed`` is a code-to-permission mapping, not
    the inverted raw ``no_new_features`` flag.
    """

    def __init__(
        self, store: KtruStore, index: NeighborIndex,
        embed: Callable[[str], np.ndarray], *,
        additional_allowed: Mapping[str, bool | None] | None = None,
        recommended_fields: Mapping[str, list[dict[str, Any]]] | None = None,
    ):
        if index is None:
            raise PipelineConfigError("NEIGHBOR_INDEX is not configured; an existing STE NPZ index is required")
        super().__init__(store, additional_allowed=additional_allowed, recommended_fields=recommended_fields)
        self.index, self.embed = index, embed
        self._rows = lru_cache(maxsize=2048)(self._rows_for_node)

    def node_for(self, okpd: str) -> str | None:
        tree, code = self.store.tree, okpd
        while code:
            node = tree.nodes.get(code)
            if node is not None and node.total:
                return code
            # Prefer actual classifier parent links, then missing-code prefixes.
            code = node.parent if node is not None and node.parent else code.rsplit(".", 1)[0] if "." in code else ""
        return None

    def _rows_for_node(self, node: str) -> tuple[int, ...]:
        return tuple(sorted(self.store.tree.positions_under(node), key=lambda row: (self.store.codes[row], row)))

    def _nearest(self, vector: np.ndarray, k: int) -> list[tuple[int, float]]:
        # NeighborIndex.nearest uses argpartition without stable cutoff ties.
        # Use the same embedding-only similarities with deterministic cutoff ties.
        scores = self.index.vectors @ vector
        if scores.ndim != 1 or not np.isfinite(scores).all():
            raise PipelineConfigError("STE index/query produced invalid embedding similarities")
        k = min(k, len(scores))
        cutoff = np.partition(scores, len(scores) - k)[len(scores) - k]
        above = np.flatnonzero(scores > cutoff).tolist()
        tied = np.flatnonzero(scores == cutoff).tolist()
        tied.sort(key=lambda i: (int(self.index.ids[i]), i))
        chosen = above + tied[:k - len(above)]
        chosen.sort(key=lambda i: (-float(scores[i]), int(self.index.ids[i]), i))
        return [(i, float(scores[i])) for i in chosen]

    def __call__(self, text: str, neighbors_k: int = 25, schemas_k: int = 20) -> Retrieval:
        if neighbors_k < 0 or schemas_k < 0:
            raise ValueError("neighbors_k and schemas_k must be non-negative")
        if not len(self.index):
            return Retrieval(schemas=[], neighbors=[])
        if neighbors_k == 0:
            return Retrieval(schemas=[], neighbors=[], truncated=True)
        near = self._nearest(self.embed(text), neighbors_k)
        return self.from_neighbors(near, schemas_k)

    def from_neighbors(self, near: list[tuple[int, float]], schemas_k: int,
                       *, balanced: bool = False) -> Retrieval:
        """Adapt an ordered list of index rows; multi-query can share category slots."""
        if schemas_k < 0:
            raise ValueError("schemas_k must be non-negative")
        rows: list[int] = []
        selected: set[str] = set()
        mapped: list[tuple[int, float, str | None]] = []
        visited_nodes: set[str] = set()
        truncated = False
        for i, score in near:
            node = self.node_for(self.index.okpd[i])
            mapped.append((i, score, node))
            if node is None or node in visited_nodes:
                continue
            visited_nodes.add(node)
            for row in self._rows(node):
                code = self.store.codes[row]
                if code in selected:
                    continue
                if len(rows) == schemas_k:
                    truncated = True
                    break
                rows.append(row)
                selected.add(code)
        if balanced:
            # Do not let one broad category consume the entire schema budget.
            groups = [self._rows(node) for node in dict.fromkeys(
                node for _, _, node in mapped if node is not None)]
            def candidates():
                seen_rows = set()
                for offset in range(max(map(len, groups), default=0)):
                    for group in groups:
                        if offset < len(group) and group[offset] not in seen_rows:
                            seen_rows.add(group[offset])
                            yield group[offset]
            bounded = list(islice(candidates(), schemas_k + 1))
            rows = bounded[:schemas_k]
            truncated = len(bounded) > schemas_k
        # Neighbor codes refer only to returned schemas; broad ancestor nodes
        # must not evade the candidate cap through the evidence payload.
        neighbors = [Neighbor(
            id=int(self.index.ids[i]), name=self.index.names[i], score=score,
            schema_codes=[self.store.codes[row] for row in rows if node and self._row_under(row, node)],
        ) for i, score, node in mapped]
        return Retrieval(schemas=[self._schema(row).model_copy(deep=True) for row in rows],
                         neighbors=neighbors, truncated=truncated)

    def _row_under(self, row: int, node: str) -> bool:
        return row in self._rows(node)


def _load_additional_policy(data_dir: Path) -> dict[str, bool | None]:
    import pyarrow.parquet as pq

    parquet = pq.ParquetFile(data_dir / "ktru-actual.parquet")
    # This is the actual XML-derived column in ktru-actual.parquet, not a
    # guessed allow flag. Older/missing metadata leaves permission unknown.
    if "no_new_features" not in parquet.schema_arrow.names:
        return {}
    policy = {}
    for batch in parquet.iter_batches(columns=["code", "status", "no_new_features"]):
        for record in batch.to_pylist():
            if record["status"] == "ACTIVE":
                flag = record["no_new_features"]
                policy[record["code"]] = not flag if isinstance(flag, bool) else None
    return policy


def default_retriever() -> PipelineRetriever:
    """Load NEIGHBOR_INDEX and KTRU_DATA; never implicitly rebuild MEOW_CACHE."""
    index_path = Path(os.environ.get("NEIGHBOR_INDEX", str(DEFAULT_INDEX))).expanduser()
    cache_path = Path(os.environ.get("MEOW_CACHE", str(DEFAULT_CACHE))).expanduser()
    data_dir = Path(os.environ.get("KTRU_DATA", str(DEFAULT_DATA))).expanduser()
    if not index_path.is_file():
        raise PipelineConfigError(
            f"NEIGHBOR_INDEX does not exist: {index_path}. Build the STE NPZ index offline "
            f"from MEOW_CACHE={cache_path}; automatic rebuild/fallback is disabled."
        )
    try:
        index = NeighborIndex.load(index_path)
        store = KtruStore.load(data_dir)
        policy = _load_additional_policy(data_dir)
        recommended_path = os.environ.get("RECOMMENDED_FIELDS")
        if recommended_path:
            with Path(recommended_path).expanduser().open(encoding="utf-8") as stream:
                recommended = json.load(stream)
            if not isinstance(recommended, dict) or any(not isinstance(recs, list) for recs in recommended.values()):
                raise PipelineConfigError("RECOMMENDED_FIELDS must map schema codes to lists of field records")
        else:
            recommended = {}
    except (OSError, ValueError, KeyError) as exc:
        raise PipelineConfigError(f"Unable to load pipeline retrieval assets: {exc}") from exc
    return PipelineRetriever(store, index, MeowQueryEmbedder(),
                             additional_allowed=policy, recommended_fields=recommended)
