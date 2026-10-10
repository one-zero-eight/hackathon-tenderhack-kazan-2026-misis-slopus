"""Вызов System One через `typesafe_sdk`: облачный Jev или совместимый сервер (experiments/jev9b_server.py).

Адрес — `base_url` или `TYPESAFE_BASE_URL`; для локального сервера ключ любой непустой.
"""

from __future__ import annotations

from typing import Any, Callable


def sdk_caller(*, base_url: str | None = None, api_key: str | None = None,
               timeout: float = 120) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Функция «тело запроса → тело ответа» (dict), как прежний `call_jev`; клиент общий для потоков."""
    from typesafe_sdk import TypeSafeClient

    client = TypeSafeClient(api_key=api_key, base_url=base_url, timeout=timeout)

    def call(request: dict[str, Any]) -> dict[str, Any]:
        response = client.system_one(state=request["state"], questions=request["questions"], model=request.get("model"))
        return {"model": response.model, "usage": response.usage.model_dump(),
                "answers": {name: answer.model_dump(mode="json") for name, answer in response.answers.items()}}

    return call
