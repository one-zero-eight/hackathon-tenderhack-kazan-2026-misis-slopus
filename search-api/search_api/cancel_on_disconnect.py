"""Cancel the request handler task when the ASGI client disconnects.

FastAPI/Starlette do not cancel non-streaming handlers on disconnect, so long
httpx calls to embeddings/LLM keep running after the browser aborts. This
middleware proxies ``receive`` (so POST bodies are not stolen) and cancels the
downstream task on ``http.disconnect``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("uvicorn.error")

_SENTINEL = object()


class CancelOnDisconnectMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        queue: asyncio.Queue[Any] = asyncio.Queue()

        async def message_poller(handler_task: asyncio.Task[None]) -> object:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    handler_task.cancel()
                    return _SENTINEL
                await queue.put(message)

        handler_task = asyncio.create_task(self.app(scope, queue.get, send))
        poller_task = asyncio.create_task(message_poller(handler_task))
        try:
            await handler_task
        except asyncio.CancelledError:
            path = scope.get("path", "")
            logger.info("Request cancelled (client disconnected): %s %s", scope.get("method"), path)
        finally:
            if not poller_task.done():
                poller_task.cancel()
                try:
                    await poller_task
                except asyncio.CancelledError:
                    pass
