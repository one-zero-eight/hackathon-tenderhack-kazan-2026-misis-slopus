"""Заполнение схемы характеристик позиции КТРУ для фронтенда: код позиции + описание → готовая форма.

Категория (код КТРУ, например из /classify/category) уже известна, поэтому поиск и выбор схемы пропускаются.
Стратегии извлечения значений:

    llm_jev  LLM извлекает значения с цитатами → Jev проверяет правдоподобие → проверка кодом (самая строгая)
    llm      то же без Jev: быстрее и дешевле, остаётся только проверка кодом
    rules    без LLM: числа и единицы регулярками, перечни — точным/нечётким сравнением (Autofiller)
    auto     llm_jev, при сбое провайдера (LLM/Jev) — откат на rules

Ответ один и тот же для всех стратегий: каждое поле схемы с типом, единицей, допустимыми значениями и
заполненным значением, статусом (accepted | rejected | missing) и причинами отказа.
"""
from __future__ import annotations

from typing import Any, Callable, Literal

from .pipeline import AutofillPipeline, PipelineError
from .pipeline_schema import PipelineField, PipelineSchema

Strategy = Literal["llm_jev", "llm", "rules", "auto"]
STRATEGIES = ("llm_jev", "llm", "rules", "auto")

# code, text -> {имя характеристики: (значение, цитата, оценка, предупреждение)}
RulesFn = Callable[[str, str], dict[str, tuple[Any, str, float, str | None]]]


def field_view(field: PipelineField) -> dict[str, Any]:
    """Описание поля для построения формы: что вводить и что допустимо."""
    return {"field_id": field.id, "name": field.name, "type": field.type, "unit": field.unit,
            "values": field.values, "required": field.required,
            "ranges": [[{"op": r.op, "value": r.value} for r in alt] for alt in field.alternatives]}


def _empty(field: PipelineField) -> dict[str, Any]:
    return field_view(field) | {"value": None, "status": "missing", "quote": None,
                                "confidence": None, "errors": []}


class SchemaExtractor:
    def __init__(self, pipeline: AutofillPipeline | None = None, rules: RulesFn | None = None,
                 schema_for_code: Callable[[str], PipelineSchema | None] | None = None):
        self.pipeline, self.rules = pipeline, rules
        self.schema_for_code = schema_for_code or (pipeline.schema_for_code if pipeline else None)

    def available(self) -> list[str]:
        out = []
        if self.pipeline is not None:
            out += ["llm_jev", "llm"]
        if self.rules is not None:
            out.append("rules")
        if self.pipeline is not None and self.rules is not None:
            out.append("auto")
        return out

    def _schema(self, code: str) -> PipelineSchema:
        schema = self.schema_for_code(code) if self.schema_for_code else None
        if schema is None:
            raise KeyError(code)
        return schema

    def extract(self, code: str, text: str, *, strategy: Strategy = "llm_jev",
                details: bool = False) -> dict[str, Any]:
        if strategy not in STRATEGIES:
            raise ValueError(f"Неизвестная стратегия: {strategy}")
        schema = self._schema(code)
        used, notes = strategy, []
        if strategy == "auto":
            try:
                out = self._llm(schema, text, use_jev=True, details=details)
                used = "llm_jev"
            except PipelineError as e:
                notes.append(f"LLM/Jev недоступны ({e}): использованы правила")
                out, used = self._rules(schema, text), "rules"
        elif strategy == "rules":
            out = self._rules(schema, text)
        else:
            out = self._llm(schema, text, use_jev=strategy == "llm_jev", details=details)
        out["strategy"], out["requested_strategy"] = used, strategy
        out["issues"] = notes + out["issues"]
        return out

    def _llm(self, schema: PipelineSchema, text: str, *, use_jev: bool, details: bool) -> dict[str, Any]:
        if self.pipeline is None:
            raise PipelineError("LLM-стратегии не настроены")
        raw = self.pipeline.fill(text, schema_code=schema.code, use_jev=use_jev, details=details)
        by_id = {v["field_id"]: v for v in raw["schema_fields"]}
        fields = []
        for field in schema.fields:
            got = by_id.get(field.id)
            if got is None:
                fields.append(_empty(field))
                continue
            fields.append(field_view(field) | {
                "value": got["value"], "status": got["status"], "quote": got["quote"],
                "confidence": got["jev_probability"] if use_jev else None, "errors": got["errors"]})
        extra = [{"name": v["name"], "value": v["value"], "unit": v["unit"], "quote": v["quote"],
                  "status": v["status"], "confidence": v["jev_probability"] if use_jev else None,
                  "errors": v["errors"]} for v in raw["additional_fields"]]
        out = self._shape(schema, raw["status"], fields, extra, raw["issues"])
        if use_jev:
            out["jev"] = raw["jev"]
        if details:
            out["details"] = raw.get("details")
        return out

    def _rules(self, schema: PipelineSchema, text: str) -> dict[str, Any]:
        if self.rules is None:
            raise PipelineError("Стратегия rules не настроена")
        found = self.rules(schema.code, text)
        fields, issues = [], []
        for field in schema.fields:
            hit = found.get(field.name)
            if hit is None:
                fields.append(_empty(field))
                if field.required:
                    issues.append(f"не заполнено обязательное поле: {field.name}")
                continue
            value, quote, score, warning = hit
            fields.append(field_view(field) | {
                "value": None if warning else value, "status": "rejected" if warning else "accepted",
                "quote": quote, "confidence": score, "errors": [warning] if warning else []})
        status = "needs_review" if issues or any(f["status"] == "rejected" for f in fields) else "accepted"
        if not any(f["status"] == "accepted" for f in fields):
            status = "no_match" if not fields else "needs_review"
            issues.append("правила не нашли ни одного значения")
        return self._shape(schema, status, fields, [], issues)

    @staticmethod
    def _shape(schema: PipelineSchema, status: str, fields: list[dict[str, Any]],
               additional: list[dict[str, Any]], issues: list[str]) -> dict[str, Any]:
        return {"status": status, "category": {"code": schema.code, "name": schema.name, "catalog": schema.catalog},
                "fields": fields, "additional_fields": additional,
                "additional_allowed": schema.additional_allowed, "issues": issues,
                "filled": sum(f["status"] == "accepted" for f in fields), "total": len(fields)}


def rules_from_store(store: Any) -> RulesFn:
    """Правила на Autofiller без эмбеддингов: схема позиции берётся из каталога КТРУ."""
    from .autofill import Autofiller

    filler = Autofiller(embedder=None)

    def run(code: str, text: str) -> dict[str, tuple[Any, str, float, str | None]]:
        row = store.row_of.get(code)
        if row is None:
            raise KeyError(code)
        result = filler.fill(text, [store.candidate(row)])
        out = {}
        for m in result.matches:
            value = m.значение
            if isinstance(value, dict):
                value = value.get("значение")
            out[m.характеристика] = (value, m.фрагмент, m.оценка, m.предупреждение)
        return out

    return run


def default_extractor() -> SchemaExtractor:
    import os
    from pathlib import Path

    from .ktru_store import DEFAULT_DATA, KtruStore
    from .pipeline import default_pipeline
    from .pipeline_retrieval import PipelineConfigError, SchemaBuilder

    try:
        pipeline = default_pipeline()
    except PipelineConfigError:
        # Нет ключа LLM или индекса соседей: схема по коду и правила работают без них.
        store = KtruStore.load(Path(os.environ.get("KTRU_DATA", DEFAULT_DATA)))
        return SchemaExtractor(None, rules_from_store(store), SchemaBuilder(store).schema_for_code)
    return SchemaExtractor(pipeline, rules_from_store(pipeline.retrieve.store))
