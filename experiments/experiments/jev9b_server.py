"""Локальный сервер System One (autotrust/JEV-9B или LiquidAI/d1-3B) с контрактом TypeSafe `/v1/systemone`.

    .venv/bin/python experiments/jev9b_server.py --port 8765            # JEV-9B, cuda, bfloat16
    .venv/bin/python experiments/jev9b_server.py --backend d1 --port 8766
    .venv/bin/python experiments/jev9b_server.py --backend nimble --max-tokens 8192 --port 8767   # CC BY-NC 4.0
    TYPESAFE_BASE_URL=http://127.0.0.1:8765 TYPESAFE_API_KEY=local …    # клиент typesafe_sdk

Перевод запроса и ограничения — в ktru_autofill.jev_local. Ключ не проверяется: слушает только 127.0.0.1.
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import time

from ktru_autofill.jev_local import REPO, D1, Jev9B, Nimble, UnsupportedQuestion

log = logging.getLogger("jev9b_server")


def handler(model: Jev9B | D1 | Nimble):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/v1/models":
                self.reply(200, {"data": [{"id": model.name}]})
            else:
                self.reply(404, {"detail": "not found"})

        def do_POST(self):
            if self.path != "/v1/systemone":
                return self.reply(404, {"detail": "not found"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                start = time.perf_counter()
                result = model.system_one(body)
                log.info("%d вопросов, %d токенов, %.2f с", len(result["answers"]),
                         result["usage"]["input_tokens"], time.perf_counter() - start)
                self.reply(200, result)
            except (UnsupportedQuestion, json.JSONDecodeError, AttributeError, TypeError) as e:
                self.reply(422, {"detail": str(e)})

        def log_message(self, *args):  # access-лог не нужен, время пишет do_POST
            pass

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--backend", choices=["jev9b", "d1", "nimble"], default="jev9b")
    ap.add_argument("--nimble-adapter", default=str(Path.home() / "models/nimble-v3"))
    ap.add_argument("--nimble-code", default=str(Path.home() / "models/nimble-v3-code/code/nimble/evaluation"))
    ap.add_argument("--repo", help=f"по умолчанию {REPO} или LiquidAI/d1-3B")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--dtype", default="bfloat16", choices=["bfloat16", "float16", "float32"])
    ap.add_argument("--max-tokens", type=int, default=4096)
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    if args.backend == "nimble":
        model = Nimble(args.nimble_adapter, args.nimble_code, args.device, args.dtype, args.max_tokens)
    elif args.backend == "d1":
        model = D1(args.repo or "LiquidAI/d1-3B", args.device, args.dtype)
    else:
        model = Jev9B(args.repo or REPO, args.device, args.dtype, args.max_tokens)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(model))
    log.info("слушаю http://127.0.0.1:%d/v1/systemone", args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
