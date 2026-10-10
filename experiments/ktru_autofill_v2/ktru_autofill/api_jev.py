"""HTTP API автозаполнения КТРУ на Jev + регулярках: то же, что CLI `ktru-autofill`, без дерева-эмбеддингов.

    uv run uvicorn ktru_autofill.api_jev:app            # или: uv run ktru-api

    GET  /health
    POST /fill/llm[/stream]     {"text": ..., "details": false} — пайплайн C: соседи → LLM (схема, поля) → Jev (правдоподобие) → проверка кодом
    POST /fill                  {"text": ..., "details": false, "okpd2": "17.12.14.110", "codes": ["…"], "search": "tree|neighbors"}
                                → тот же JSON, что у CLI, + предупреждения, «уточнить», служебное
    POST /fill/stream           то же, но потоком событий (SSE): поиск → характеристика → кандидаты → сужение → результат
    POST /dialog                → {"id"}: диалог, как `ktru-autofill -i`: каждое сообщение дописывается к описанию
    POST /dialog/{id}/message   {"text": ..., "reset": false}
    GET  /dialog/{id}   DELETE /dialog/{id}
    GET  /ktru/{code}           позиция КТРУ и её характеристики
    GET  /okpd2/{code}          узел ОКПД2 и дети (с числом позиций)

Поиск товара: `tree` — спуск по ОКПД2, выбирает Jev; `neighbors` — ближайшие карточки СТЕ по эмбеддингам (векторы из
LMDB-кеша meow-embed: MEOW_CACHE / NEIGHBOR_INDEX, запрос кодирует MEOW_EMBED_URL), при недоступности — откат на дерево.
Умолчание — KTRU_SEARCH. Данные — KTRU_DATA (по умолчанию zakupki-reverse/data: ktru-actual.parquet, okpd2-moy-zakupki.parquet), Jev —
TYPESAFE_API_KEY / TYPESAFE_BASE_URL (см. jev_client). Каталог и клиент создаются при первом запросе.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .jev_autofill import JevAutofiller, Outcome
from .jev_client import JevClient, JevError
from .jev_search import JevSearch
from .ktru_store import DEFAULT_DATA, KtruStore
from .llm_pipeline import LlmClient, LlmError, LlmPipeline
from .neighbor_search import MeowQueryEmbedder, NeighborIndex, NeighborSearch

MAX_DIALOGS = 1000


class FillRequest(BaseModel):
    text: str = Field(min_length=1, description="описание товара своими словами, возможны опечатки")
    details: bool = False
    okpd2: str | None = Field(None, description="искать название позиции только в этом узле ОКПД2")
    codes: list[str] | None = Field(None, description="явные кандидаты КТРУ — поиск товара пропускается")
    search: Literal["tree", "neighbors"] | None = Field(
        None, description="поиск товара: tree — спуск по ОКПД2 (Jev), neighbors — соседи-СТЕ по эмбеддингам; "
                          "по умолчанию KTRU_SEARCH или tree")


class LlmFillRequest(BaseModel):
    text: str = Field(min_length=1, description="описание товара своими словами")
    details: bool = False


class MessageRequest(BaseModel):
    text: str = Field(min_length=1)
    details: bool = False
    reset: bool = False  # начать описание заново
    search: Literal["tree", "neighbors"] | None = None


class Dialog:
    def __init__(self) -> None:
        self.text = ""
        self.rows: list[int] | None = None
        self.families: list[tuple[str, float]] = []
        self.steps: list = []
        self.last: dict[str, Any] | None = None
        self.lock = threading.Lock()


def default_search() -> str:
    return os.environ.get("KTRU_SEARCH", "tree")


def neighbors_factory(store: KtruStore) -> Callable[[JevSearch], NeighborSearch | None]:
    """Поиск по соседям-СТЕ: индекс из npz или LMDB-кеша векторов, запрос кодирует MEOW_EMBED_URL."""
    def build(picker: JevSearch) -> NeighborSearch | None:
        index = NeighborIndex.open()
        return NeighborSearch(store, index, MeowQueryEmbedder(), picker) if index is not None else None
    return build


def default_factory() -> JevAutofiller:
    store = KtruStore.load(Path(os.environ.get("KTRU_DATA", DEFAULT_DATA)))
    return JevAutofiller(store, JevClient.from_env(), beam=int(os.environ.get("JEV_BEAM", "3")),
                         neighbors=neighbors_factory(store))


def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def default_llm_pipeline(f: JevAutofiller) -> LlmPipeline:
    """Пайплайн C: соседи-СТЕ → LLM (схема, поля) → Jev (правдоподобие) → проверка кодом. Нужны индекс соседей и OPENAI_API_KEY."""
    neighbors = f.neighbor_search()
    if neighbors is None:
        raise JevError(f"поиск по соседям-СТЕ недоступен ({f._neighbors_error or 'не настроен'})")
    try:
        llm = LlmClient.from_env()
    except LlmError as e:
        raise JevError(str(e)) from e
    return LlmPipeline(f.store, f.jev, llm, neighbors.find)


def create_app(factory: Callable[[], JevAutofiller] = default_factory,
               llm_factory: Callable[[JevAutofiller], LlmPipeline] = default_llm_pipeline) -> FastAPI:
    from .api import create_pipeline_router
    from .api_category import create_category_router
    from .api_extract import create_extract_router

    app = FastAPI(title="КТРУ: автозаполнение на Jev + регулярках")
    app.include_router(create_pipeline_router(path="/fill/pipeline"))
    app.include_router(create_category_router())
    app.include_router(create_extract_router())
    state: dict[str, Any] = {"filler": None, "llm": None}
    lock = threading.Lock()
    dialogs: OrderedDict[str, Dialog] = OrderedDict()

    def filler() -> JevAutofiller:
        with lock:
            if state["filler"] is None:
                try:
                    state["filler"] = factory()
                except JevError as e:
                    raise HTTPException(503, str(e)) from e
        return state["filler"]

    def llm_pipeline() -> LlmPipeline:
        f = filler()
        with lock:
            if state["llm"] is None:
                try:
                    state["llm"] = llm_factory(f)
                except JevError as e:
                    raise HTTPException(503, str(e)) from e
        return state["llm"]

    def guarded(run: Callable[[], Outcome]) -> Outcome:
        try:
            return run()
        except JevError as e:
            raise HTTPException(502, str(e)) from e
        except KeyError as e:
            raise HTTPException(404, f"нет в каталоге: {e}") from e

    def stream(run: Callable[[Callable[[str, dict], None]], dict[str, Any]]) -> StreamingResponse:
        events: queue.Queue = queue.Queue()

        def worker() -> None:
            try:
                events.put(("результат", run(lambda event, data: events.put((event, data)))))
            except Exception as e:  # поток уже начат: ошибку отдаём событием
                events.put(("ошибка", {"сообщение": str(e), "тип": type(e).__name__}))
            finally:
                events.put(None)

        threading.Thread(target=worker, daemon=True).start()

        def generate():
            while (item := events.get()) is not None:
                yield sse(*item)

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/health")
    def health() -> dict[str, Any]:
        f = state["filler"]
        return {"status": "ok", "загружено": f is not None, "позиций": len(f.store) if f else None,
                "jev": dict(f.jev.stats) if f else None, "поиск по умолчанию": default_search(),
                "соседи-СТЕ": (None if f is None else f.neighbors_available)}

    @app.post("/fill")
    def fill(req: FillRequest) -> dict[str, Any]:
        f = filler()
        return guarded(lambda: f.fill(req.text, okpd2=req.okpd2, codes=req.codes,
                                      search=req.search or default_search())).to_dict(req.details)

    @app.post("/fill/stream")
    def fill_stream(req: FillRequest) -> StreamingResponse:
        f = filler()
        return stream(lambda emit: f.fill(req.text, emit, okpd2=req.okpd2, codes=req.codes,
                                          search=req.search or default_search()).to_dict(req.details))

    @app.post("/fill/llm")
    def fill_llm(req: LlmFillRequest) -> dict[str, Any]:
        p = llm_pipeline()
        try:
            return p.run(req.text).to_dict(req.details)
        except (JevError, LlmError) as e:
            raise HTTPException(502, str(e)) from e

    @app.post("/fill/llm/stream")
    def fill_llm_stream(req: LlmFillRequest) -> StreamingResponse:
        p = llm_pipeline()
        return stream(lambda emit: p.run(req.text, emit).to_dict(req.details))

    # --- диалог: как `ktru-autofill -i` ---------------------------------------------------------------------------

    @app.post("/dialog")
    def dialog_create() -> dict[str, str]:
        did = uuid.uuid4().hex[:12]
        with lock:
            dialogs[did] = Dialog()
            while len(dialogs) > MAX_DIALOGS:
                dialogs.popitem(last=False)
        return {"id": did}

    def get_dialog(did: str) -> Dialog:
        with lock:
            if did not in dialogs:
                raise HTTPException(404, "диалог не найден")
            dialogs.move_to_end(did)
            return dialogs[did]

    def advance(d: Dialog, req: MessageRequest, emit: Callable[[str, dict], None] | None) -> dict[str, Any]:
        f = filler()
        with d.lock:
            if req.reset:
                d.text, d.rows, d.families, d.steps, d.last = "", None, [], [], None
            d.text = f"{d.text} {req.text}".strip()
            out = guarded(lambda: f.fill(d.text, emit, rows=d.rows or None, families=d.families,
                                         search_steps=d.steps or None, search=req.search or default_search()))
            if out.rows:  # поиск товара повторяется, только пока ничего не найдено
                d.rows, d.families, d.steps = out.rows, out.families, out.search_steps
            d.last = out.to_dict(req.details) | {"описание": d.text}
            return d.last

    @app.post("/dialog/{did}/message")
    def dialog_message(did: str, req: MessageRequest) -> dict[str, Any]:
        return advance(get_dialog(did), req, None)

    @app.post("/dialog/{did}/message/stream")
    def dialog_message_stream(did: str, req: MessageRequest) -> StreamingResponse:
        d = get_dialog(did)
        return stream(lambda emit: advance(d, req, emit))

    @app.get("/dialog/{did}")
    def dialog_get(did: str) -> dict[str, Any]:
        d = get_dialog(did)
        return {"id": did, "описание": d.text, "результат": d.last}

    @app.delete("/dialog/{did}")
    def dialog_delete(did: str) -> dict[str, str]:
        with lock:
            if dialogs.pop(did, None) is None:
                raise HTTPException(404, "диалог не найден")
        return {"status": "удалён"}

    # --- справочники ----------------------------------------------------------------------------------------------

    @app.get("/ktru/{code}")
    def ktru_position(code: str) -> dict[str, Any]:
        store = filler().store
        if code not in store.row_of:
            raise HTTPException(404, f"нет позиции КТРУ {code}")
        c = store.candidate(store.row_of[code])
        return {"код": c.code, "название": c.name, "окпд2": c.okpd2, "характеристики": [
            {"название": ch.name, "тип": ch.type, "значения": ch.values[:50], "единица": ch.unit,
             "диапазон": list(ch.range) if ch.range and all(abs(x) != float("inf") for x in ch.range) else None}
            for ch in c.characteristics]}

    @app.get("/okpd2/{code}")
    def okpd2_node(code: str) -> dict[str, Any]:
        tree = filler().store.tree
        if code not in tree.nodes:
            raise HTTPException(404, f"нет узла ОКПД2 {code}")
        node = tree.nodes[code]
        return {"код": node.code, "название": node.name, "позиций": node.total,
                "путь": [n.title for n in tree.path(code) if not tree.is_section(n.code)],
                "дети": [{"код": c.code, "название": c.name, "позиций": c.total} for c in tree.children(code)]}

    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run("ktru_autofill.api_jev:app", host=os.environ.get("HOST", "127.0.0.1"),
                port=int(os.environ.get("PORT", "8000")))
