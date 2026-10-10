"""Открытые System One модели за контрактом TypeSafe `/v1/systemone`: autotrust/JEV-9B, LiquidAI/d1-3B,
bespokelabs/Bespoke-Nimble-9B-v3 (`Nimble`).

LiquidAI/d1-3B (`D1`) понимает схему TypeSafe сама; ниже — перевод для JEV-9B.

Запрос TypeSafe переводится в шаблон модели «bare-v1» (`[kind] … [state] … [question] … [options] …
[decision]:`), ответ собирается в схему TypeSafe (`answers`, `usage`), поэтому клиент — `typesafe_sdk` —
не знает, какая модель отвечает. Перевод:

  state        — строка как есть, JSON — `json.dumps` без экранирования кириллицы;
  instructions — строка как есть, объект — JSON;
  criteria     — варианты модели — только названия (в корпусе модели это короткие метки); описания
                 вариантов, если есть, дописываются в state отдельным JSON-блоком;
  один вариант — вероятность 1 без вызова модели;
  confidence   — 1 − H(p) / log n. Формула TypeSafe не опубликована: пороги между моделями не переносятся.

Поддерживаются choice (1–16 вариантов, ограничение головы модели) и noul; score модели — ровно 6 уровней,
здесь не реализован.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from typing import Any

log = logging.getLogger(__name__)

REPO = "autotrust/JEV-9B"
MAX_CHOICE_OPTIONS = 16
LETTERS = "ABCDEFGHIJKLMNOP"


class UnsupportedQuestion(ValueError):
    """Вопрос, который модель не умеет: ответ сервера 422."""


def as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def render(kind: str, state: Any, question: dict[str, Any]) -> tuple[str, list[str]]:
    """Промпт шаблона bare-v1 и варианты (метки TypeSafe) в порядке слотов модели."""
    criteria = question.get("criteria") or {}
    if kind == "choice":
        options = list(criteria)
        if not 1 <= len(options) <= MAX_CHOICE_OPTIONS:
            raise UnsupportedQuestion(f"choice: {len(options)} вариантов, модель принимает 1–{MAX_CHOICE_OPTIONS}")
        lines = [f"{LETTERS[i]}) {o}" for i, o in enumerate(options)]
    elif kind == "noul":
        options = ["false", "true"]
        lines = options
    else:
        raise UnsupportedQuestion(f"тип вопроса {kind!r} не поддерживается")
    instructions = question.get("instructions")
    if instructions is None:
        if kind != "noul" or not criteria.get("true"):
            raise UnsupportedQuestion("нет instructions")
        instructions = criteria["true"]
    state_text = as_text(state)
    described = {k: v for k, v in criteria.items() if v is not None}
    if described:
        state_text += "\n\nOption descriptions:\n" + json.dumps(described, ensure_ascii=False)
    prompt = (f"[kind] {kind}\n[state] {state_text}\n[question] {as_text(instructions)}\n[options]\n"
              + "\n".join(lines) + "\n[decision]:")
    return prompt, options


def confidence(probs: list[float]) -> float:
    if len(probs) < 2:
        return 1.0
    entropy = -sum(p * math.log(p) for p in probs if p > 0)
    return max(0.0, 1 - entropy / math.log(len(probs)))


class Jev9B:
    """transformers + peft (адаптер слит с бэкбоном) + линейная голова на 24 слота, как в карточке модели."""

    def __init__(self, repo: str = REPO, device: str = "cuda", dtype: str = "bfloat16", max_tokens: int = 4096):
        import torch
        from huggingface_hub import hf_hub_download
        from peft import PeftModel
        from safetensors.torch import load_file
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.torch, self.device, self.max_tokens = torch, device, max_tokens
        self.dtype = getattr(torch, dtype)
        self.tokenizer = AutoTokenizer.from_pretrained(repo)
        base = AutoModelForCausalLM.from_pretrained(repo, dtype=self.dtype, device_map=device)
        self.model = PeftModel.from_pretrained(base, repo, subfolder="adapter").merge_and_unload().eval()
        head = load_file(hf_hub_download(repo, "head.safetensors"))
        self.weight, self.bias = head["proj.weight"].to(device).float(), head["proj.bias"].to(device).float()
        self.slots = json.load(open(hf_hub_download(repo, "judge_config.json")))["slots"]["ranges"]
        self.temperature = json.load(open(hf_hub_download(repo, "calibration.json")))["per_kind"]
        self.name = repo
        self._lock = threading.Lock()  # одна GPU: прогоны по очереди
        log.info("%s загружена: %s, %s", repo, device, dtype)

    def decide(self, prompt: str, kind: str, n: int) -> tuple[list[float], int]:
        """Распределение по n вариантам и число токенов промпта."""
        torch = self.torch
        ids = self.tokenizer(prompt, return_tensors="pt", add_special_tokens=False)["input_ids"]
        if ids.shape[1] > self.max_tokens:
            raise UnsupportedQuestion(f"промпт {ids.shape[1]} токенов, лимит {self.max_tokens}")
        with self._lock, torch.no_grad():
            hidden = self.model.model(input_ids=ids.to(self.device)).last_hidden_state[0, -1].float()
        z = (self.weight @ hidden + self.bias) / self.temperature[kind]
        start = self.slots[kind][0]
        probs = torch.softmax(z[start:start + n], 0).tolist()
        return probs, ids.shape[1]

    def system_one(self, body: dict[str, Any]) -> dict[str, Any]:
        """Тело запроса TypeSafe → тело ответа TypeSafe."""
        if body.get("state") is None or not body.get("questions"):
            raise UnsupportedQuestion("нужны state и questions")
        answers, tokens = {}, 0
        for name, question in body["questions"].items():
            kind = question.get("type")
            prompt, options = render(kind, body["state"], question)
            if len(options) == 1:  # голова модели — от 2 вариантов; выбора нет, как в cookbook TypeSafe
                probs, used = [1.0], 0
            else:
                probs, used = self.decide(prompt, kind, len(options))
            tokens += used
            if kind == "noul":
                answers[name] = {"type": "noul", "noul": probs[1]}
            else:
                dist = dict(zip(options, probs))
                answers[name] = {"type": "choice", "choice": max(dist, key=dist.get),
                                 "confidence": confidence(probs), "probabilities": dist}
        return {"model": self.name, "answers": answers,
                "usage": {"input_tokens": tokens, "output_tokens": len(answers)}}


def d1_question(question: dict[str, Any]) -> dict[str, Any]:
    """Вопрос TypeSafe для d1: объекты — в JSON; d1 печатает у варианта только описание, поэтому
    метка дописывается перед ним."""
    q = dict(question)
    if q.get("instructions") is not None:
        q["instructions"] = as_text(q["instructions"])
    if q.get("type", "choice") == "choice":
        q["criteria"] = {label: None if desc is None else f"{label}: {as_text(desc)}"
                         for label, desc in q["criteria"].items()}
    elif isinstance(q.get("criteria"), dict):
        q["criteria"] = {k: None if v is None else as_text(v) for k, v in q["criteria"].items()}
    return q


class D1:
    """LiquidAI/d1-3B: собственный `system_one` модели (схема Decision Index = схема TypeSafe)."""

    def __init__(self, repo: str = "LiquidAI/d1-3B", device: str = "cuda", dtype: str = "bfloat16"):
        import torch
        from transformers import AutoModel

        self.model = AutoModel.from_pretrained(repo, trust_remote_code=True, dtype=getattr(torch, dtype)).to(device)
        self.name = repo
        self._lock = threading.Lock()
        log.info("%s загружена: %s, %s", repo, device, dtype)

    def system_one(self, body: dict[str, Any]) -> dict[str, Any]:
        if not body.get("questions"):
            raise UnsupportedQuestion("нужны questions")
        questions = {name: d1_question(q) for name, q in body["questions"].items()}
        with self._lock:
            result = self.model.system_one(body.get("state"), questions)
        return {"model": self.name} | result


class Nimble:
    """bespokelabs/Bespoke-Nimble-9B-v3: LoRA на Qwen/Qwen3.5-9B, вероятности по токенам-кодам вариантов.

    Промпт строит их `extended_schema.py` (датасет bespokelabs/bespoke-nimble-9b-v3-decision-index):
    его SHA-256 сверяется с `schema_config.json` адаптера. Перевод вопросов TypeSafe — как в их движке
    Decision Index: instructions → description, criteria → choices и choice_descriptions, noul → boolean.
    Ничего не обрезается: длиннее max_tokens — 422. confidence — максимальная вероятность.
    Лицензия адаптера CC BY-NC 4.0: только некоммерческое использование.
    """

    def __init__(self, adapter: str, code: str, device: str = "cuda", dtype: str = "bfloat16",
                 max_tokens: int = 8192):
        import hashlib
        import importlib.util
        from pathlib import Path

        import torch
        from peft import PeftModel
        from transformers import AutoTokenizer, Qwen3_5ForConditionalGeneration

        adapter, code = Path(adapter), Path(code)
        contract = json.loads((adapter / "schema_config.json").read_text())
        source = code / "extended_schema.py"
        if hashlib.sha256(source.read_bytes()).hexdigest() != contract["prompt_source_sha256"]["extended_schema.py"]:
            raise ValueError("extended_schema.py отличается от контракта адаптера")
        spec = importlib.util.spec_from_file_location("nimble_extended_schema", source)
        self.schema = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.schema)
        self.torch, self.device, self.max_tokens = torch, device, max_tokens
        self.tokenizer = AutoTokenizer.from_pretrained(contract["model"], revision=contract["revision"])
        base = Qwen3_5ForConditionalGeneration.from_pretrained(
            contract["model"], revision=contract["revision"], dtype=getattr(torch, dtype),
            attn_implementation="sdpa").to(device)
        self.model = PeftModel.from_pretrained(base, str(adapter)).eval()
        self.name = "bespokelabs/Bespoke-Nimble-9B-v3"
        self._lock = threading.Lock()
        log.info("%s загружена: %s, %s", self.name, device, dtype)

    @staticmethod
    def native_schema(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        schema = {}
        for name, q in body["questions"].items():
            criteria = q.get("criteria") or {}
            if q.get("instructions") is None:
                raise UnsupportedQuestion(f"{name}: нет instructions")
            field = {"description": as_text(q["instructions"])}
            if q["type"] == "noul":
                if not set(criteria) <= {"true", "false"}:
                    raise UnsupportedQuestion(f"{name}: у noul критерии только true/false")
                field.update(type="boolean", choices=[False, True],
                             choice_descriptions={k: as_text(v) for k, v in criteria.items() if v is not None})
            elif q["type"] == "choice":
                field.update(type="enum", choices=list(criteria),
                             choice_descriptions={k: as_text(v) for k, v in criteria.items() if v is not None})
            else:
                raise UnsupportedQuestion(f"тип вопроса {q['type']!r} не поддерживается")
            schema[name] = field
        state = body["state"]
        if isinstance(state, str) and not state.strip():
            state = json.dumps(state)
        return as_text(state), schema

    def system_one(self, body: dict[str, Any]) -> dict[str, Any]:
        if body.get("state") is None or not body.get("questions"):
            raise UnsupportedQuestion("нужны state и questions")
        torch = self.torch
        context, schema = self.native_schema(body)
        try:
            prompt = self.schema.prepare_prompts(self.tokenizer, context, schema, self.max_tokens)
        except ValueError as e:
            raise UnsupportedQuestion(str(e)) from None
        answers, tokens = {}, 0
        for i, name in enumerate(prompt.names):
            ids = prompt.full_ids[i]
            tokens += len(ids)
            with self._lock, torch.inference_mode():
                logits = self.model(input_ids=torch.tensor([ids], device=self.device), use_cache=False,
                                    logits_to_keep=1).logits[0, -1].float()
            probs = logits[torch.tensor(prompt.candidate_ids[i], device=self.device)].double().softmax(-1).tolist()
            dist = {self.schema.choice_key(c): p for c, p in zip(prompt.choices[i], probs)}
            if body["questions"][name]["type"] == "noul":
                answers[name] = {"type": "noul", "noul": dist["true"]}
            else:
                best = max(dist, key=dist.get)
                answers[name] = {"type": "choice", "choice": best, "confidence": dist[best], "probabilities": dist}
        return {"model": self.name, "answers": answers, "usage": {"input_tokens": tokens, "output_tokens": 0}}
