"""Дозаполняет ответы Jev в логах {request, response: null} — для хостов, где API TypeSafe доступен.

    python3 -I experiments/jev_fill.py reports/category_hypotheses/jev --env .env

Только стандартная библиотека: скрипт можно запускать вне окружения проекта. Ключ — TYPESAFE_API_KEY
из окружения или из --env. Файлы с ответом не трогает; после заполнения повторите замер — ответ
возьмётся из лога, потому что запрос тот же.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

API_URL = "https://api.typesafe.ai/v1/systemone"


def api_key(env_file: Path) -> str:
    if key := os.environ.get("TYPESAFE_API_KEY"):
        return key
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "TYPESAFE_API_KEY" and value.strip():
                return value.strip().strip("'\"")
    sys.exit(f"Нет TYPESAFE_API_KEY ни в окружении, ни в {env_file}")


def post(request: dict, key: str, name: str) -> dict:
    req = urllib.request.Request(API_URL, data=json.dumps(request, ensure_ascii=False).encode(),
                                 method="POST", headers={"Authorization": f"Bearer {key}",
                                                         "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        sys.exit(f"{name}: TypeSafe API {e.code}: {e.read().decode(errors='replace')[:300]}")


def fill(path: Path, key: str) -> bool:
    """Лог {request, response} или многошаговый {calls: [{request, response}]}: заполняет пустые ответы.
    Следующий шаг турнира появится только после повторного замера — тогда заполните ещё раз."""
    record = json.loads(path.read_text(encoding="utf-8"))
    steps = record.get("calls") or [record]
    empty = [s for s in steps if s.get("request") and not s.get("response")]
    for step in empty:
        step["response"] = post(step["request"], key, path.name)
    if empty:
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return bool(empty)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("directory", type=Path)
    ap.add_argument("--env", type=Path, default=Path(".env"))
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    key = api_key(args.env)
    paths = sorted(args.directory.glob("*.json"))
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        filled = sum(pool.map(lambda p: fill(p, key), paths))
    print(f"заполнено {filled} из {len(paths)}")


if __name__ == "__main__":
    main()
