import asyncio

import httpx
import pytest
from fastapi import FastAPI

from search_api.cancel_on_disconnect import CancelOnDisconnectMiddleware
from search_api import embeddings as embeddings_mod


@pytest.mark.anyio
async def test_disconnect_cancels_in_flight_handler():
    """ASGI http.disconnect must cancel the handler at the next await."""
    app = FastAPI()
    app.add_middleware(CancelOnDisconnectMiddleware)
    upstream_started = asyncio.Event()
    upstream_cancelled = asyncio.Event()

    async def slow_upstream():
        upstream_started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            upstream_cancelled.set()
            raise
        return b"vector"

    @app.post("/embed-like")
    async def embed_like():
        await slow_upstream()
        return {"ok": True}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/embed-like",
        "raw_path": b"/embed-like",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json"), (b"content-length", b"2")],
        "client": ("127.0.0.1", 123),
        "server": ("test", 80),
    }
    body_sent = False
    disconnected = asyncio.Event()

    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": b"{}", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    messages: list[dict] = []

    async def send(message):
        messages.append(message)

    task = asyncio.create_task(app(scope, receive, send))
    await asyncio.wait_for(upstream_started.wait(), timeout=2)
    disconnected.set()
    await asyncio.wait_for(task, timeout=2)
    await asyncio.wait_for(upstream_cancelled.wait(), timeout=2)
    assert not any(m.get("type") == "http.response.start" for m in messages)


@pytest.mark.anyio
async def test_completed_request_is_not_treated_as_cancel():
    app = FastAPI()
    app.add_middleware(CancelOnDisconnectMiddleware)

    @app.post("/fast")
    async def fast():
        return {"ok": True}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/fast",
        "raw_path": b"/fast",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json"), (b"content-length", b"2")],
        "client": ("127.0.0.1", 123),
        "server": ("test", 80),
    }
    body_sent = False

    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": b"{}", "more_body": False}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    messages: list[dict] = []

    async def send(message):
        messages.append(message)

    await asyncio.wait_for(app(scope, receive, send), timeout=2)
    assert any(m.get("type") == "http.response.start" for m in messages)
    body = b"".join(m["body"] for m in messages if m.get("type") == "http.response.body")
    assert b'"ok":true' in body.replace(b" ", b"")


@pytest.mark.anyio
async def test_embed_texts_does_not_retry_after_cancel(monkeypatch):
    attempts = 0

    async def boom(self, url, **kwargs):
        nonlocal attempts
        attempts += 1
        raise asyncio.CancelledError()

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)
    with pytest.raises(asyncio.CancelledError):
        await embeddings_mod.embed_texts(["шкаф"], "query")
    assert attempts == 1
