"""Клиент Jev (TypeSafe System One) для API: пакеты вопросов, параллельные запросы, кеш ответов.

Адрес и ключ — `TYPESAFE_BASE_URL` / `TYPESAFE_API_KEY` (или `.env` в корне проекта); с `TYPESAFE_BASE_URL` на
локальный сервер (experiments/jev9b_server.py) ключ может быть любым. Вопросы одного состояния идут пачками по
`per_request` (иначе растёт контекст вариантов), пачки — параллельно.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

MODEL = "jev-latest"
ROOT = Path(__file__).resolve().parents[2]


class JevError(RuntimeError):
    """Jev недоступен или вернул ошибку."""


def api_key(env_file: Path = ROOT / ".env") -> str | None:
    if key := os.environ.get("TYPESAFE_API_KEY"):
        return key
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "TYPESAFE_API_KEY" and value.strip():
                return value.strip().strip("'\"")
    return None


class JevClient:
    def __init__(self, call: Callable[[dict[str, Any]], dict[str, Any]], model: str = MODEL, workers: int = 8,
                 per_request: int = 4, cache_size: int = 4096):
        self.call, self.model, self.per_request = call, model, per_request
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self._cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._cache_size, self._lock = cache_size, threading.Lock()
        self.stats = {"запросов": 0, "из кеша": 0, "input_tokens": 0, "output_tokens": 0}

    @classmethod
    def from_env(cls, **kwargs: Any) -> JevClient:
        from dotenv import load_dotenv

        from .system_one import sdk_caller

        load_dotenv(ROOT / ".env")  # TYPESAFE_BASE_URL из .env; уже заданные в окружении переменные не перезаписываются
        base_url = os.environ.get("TYPESAFE_BASE_URL")
        key = api_key() or ("local" if base_url else None)
        if not key:
            raise JevError("Нет TYPESAFE_API_KEY ни в окружении, ни в .env")
        return cls(sdk_caller(base_url=base_url, api_key=key), model=os.environ.get("JEV_MODEL", MODEL), **kwargs)

    def _request(self, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        request = {"model": self.model, "state": state, "questions": questions}
        key = hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                self.stats["из кеша"] += 1
                return self._cache[key]
        try:
            response = self.call(request)
        except Exception as e:  # ошибки SDK (сеть, 4xx/5xx) — единым типом для API
            raise JevError(f"Jev: {e}") from e
        with self._lock:
            self.stats["запросов"] += 1
            for name in ("input_tokens", "output_tokens"):
                self.stats[name] += (response.get("usage") or {}).get(name, 0)
            self._cache[key] = response
            if len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return response

    def ask(self, state: Any, questions: dict[str, Any],
            on_batch: Callable[[dict[str, dict[str, Any]]], None] | None = None) -> dict[str, dict[str, Any]]:
        """Ответы по именам вопросов; пачки по per_request идут параллельно."""
        return self.ask_many([(state, questions)], (lambda _, a: on_batch(a)) if on_batch else None)[0]

    def ask_many(self, jobs: list[tuple[Any, dict[str, Any]]],
                 on_batch: Callable[[int, dict[str, dict[str, Any]]], None] | None = None
                 ) -> list[dict[str, dict[str, Any]]]:
        """Независимые (state, questions) параллельно. Все пачки всех задач — одним списком в пул: вложенных
        вызовов пула нет, взаимной блокировки при любом числе задач не будет. on_batch(номер задачи, ответы) —
        в вызывающем потоке по мере готовности каждой пачки."""
        futures = {}
        for index, (state, questions) in enumerate(jobs):
            items = list(questions.items())
            for i in range(0, len(items), self.per_request):
                futures[self.pool.submit(self._request, state, dict(items[i:i + self.per_request]))] = index
        answers: list[dict[str, dict[str, Any]]] = [{} for _ in jobs]
        for future in as_completed(futures):
            index, got = futures[future], future.result()["answers"]
            answers[index].update(got)
            if on_batch:
                on_batch(index, got)
        return answers
