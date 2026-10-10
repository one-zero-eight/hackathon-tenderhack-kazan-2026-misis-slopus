from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import httpx

from search_api.config import settings
from search_api.ste_catalog import has_image_id

_MAX_BYTES = 12 * 1024 * 1024
_UNAVAILABLE_TTL = 300.0
_in_flight: dict[int, asyncio.Task[tuple[bytes, str]]] = {}
_unavailable: dict[int, float] = {}


def image_cache_dir() -> Path:
    if settings.image_cache_dir is not None:
        return Path(settings.image_cache_dir)
    return settings.search_database.parent / "images"


def _mime(data: bytes) -> str:
    if len(data) >= 3 and data[0] == 0xFF and data[1] == 0xD8 and data[2] == 0xFF:
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("Unsupported image format")


def _read_cache(path: Path) -> tuple[bytes, str] | None:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    return data, _mime(data)


def _write_cache(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


async def _download(image_id: int) -> tuple[bytes, str]:
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
        response = await client.get(
            f"https://zakupki.mos.ru/newapi/api/FileStorage/Download?id={image_id}"
        )
    if response.status_code >= 400:
        raise RuntimeError("Image unavailable")
    content_type = response.headers.get("content-type") or ""
    if not content_type.startswith("image/"):
        raise RuntimeError("Image unavailable")
    data = response.content
    if len(data) > _MAX_BYTES:
        raise RuntimeError("Image too large")
    mime = _mime(data)
    _write_cache(image_cache_dir() / f"{image_id}.image", data)
    return data, mime


async def get_image(image_id: int) -> tuple[bytes, str]:
    if image_id <= 0:
        raise FileNotFoundError("Not found")

    # Serve disk cache before loading parquet catalog (has_image_id is expensive).
    cached = _read_cache(image_cache_dir() / f"{image_id}.image")
    if cached is not None:
        return cached

    if not has_image_id(image_id):
        raise FileNotFoundError("Not found")

    now = asyncio.get_running_loop().time()
    until = _unavailable.get(image_id)
    if until is not None and until > now:
        raise RuntimeError("Image unavailable")

    task = _in_flight.get(image_id)
    if task is None:
        task = asyncio.create_task(_download(image_id))
        _in_flight[image_id] = task

        def _clear(done: asyncio.Task[tuple[bytes, str]]) -> None:
            _in_flight.pop(image_id, None)
            if done.cancelled():
                return
            exc = done.exception()
            if exc is not None:
                _unavailable[image_id] = asyncio.get_running_loop().time() + _UNAVAILABLE_TTL
                if len(_unavailable) > 2000:
                    _unavailable.pop(next(iter(_unavailable)), None)

        task.add_done_callback(_clear)

    return await task
