"""Typed characteristic extraction through LangChain and an OpenAI-compatible vLLM server."""
from __future__ import annotations

import json
import os

from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from .pipeline import PipelineError

DEFAULT_MODEL = "nvidia/Qwen3.6-35B-A3B"


class LangChainVLLM:
    def __init__(self, model: ChatOpenAI):
        self.model = model
        self.prompt = ChatPromptTemplate.from_messages([
            ("system", "{instructions}"), ("human", "{payload}"),
        ])

    @classmethod
    def from_env(cls) -> LangChainVLLM:
        return cls(ChatOpenAI(
            model=os.environ.get("VLLM_MODEL", DEFAULT_MODEL),
            base_url=os.environ.get("VLLM_BASE_URL", "http://127.0.0.1:8000/v1"),
            api_key=os.environ.get("VLLM_API_KEY", "local"),
            temperature=0,
            max_tokens=int(os.environ.get("VLLM_MAX_TOKENS", "4096")),
            timeout=float(os.environ.get("VLLM_TIMEOUT", "120")),
            max_retries=0,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        ))

    def __call__(self, prompt: str, payload: dict, output: type[BaseModel]) -> BaseModel:
        structured = self.model.with_structured_output(
            output, method="json_schema", strict=True, include_raw=True,
        )
        chain = self.prompt | structured
        try:
            response = chain.invoke({
                "instructions": prompt,
                "payload": json.dumps(payload, ensure_ascii=False),
            })
        except Exception as exc:
            raise PipelineError(f"vLLM/LangChain: {exc}") from exc
        if response["parsing_error"] is not None or response["parsed"] is None:
            raise PipelineError("vLLM/LangChain: модель не вернула корректный структурированный ответ")
        return response["parsed"]
