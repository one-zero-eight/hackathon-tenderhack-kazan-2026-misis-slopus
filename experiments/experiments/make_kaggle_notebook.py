"""Ноутбук Kaggle для дообучения laya-multilingual на СТЕ организаторов.

    uv run python experiments/make_kaggle_notebook.py   →  data/laya/kaggle/

Берёт оригинальный ноутбук Laya (2×T4, DDP, RLCD) и меняет только нужное:
данные — из Kaggle Dataset (train.jsonl / eval.jsonl из laya_build_dataset.py), база —
convaiinnovations/laya/multilingual, эпохи — LAYA_EPOCHS (по умолчанию 2), оценка — на наших
отложенных СТЕ (до и после), публикация на HF — по желанию.
"""

from __future__ import annotations

import json
import shutil
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ("https://raw.githubusercontent.com/NandhaKishorM/laya/main/notebooks/"
       "laya_finetune_typed_decisions_2xT4_kaggle.ipynb")
OUT = ROOT / "data" / "laya" / "kaggle"
DATASET_SLUG = "laya-ste-tenderhack"


def md(text: str) -> dict:
    return {"cell_type": "markdown", "metadata": {}, "source": text.strip("\n").splitlines(keepends=True)}


def code(text: str) -> dict:
    return {"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [],
            "source": text.strip("\n").splitlines(keepends=True)}


INTRO = """
# Дообучение `laya-multilingual` на СТЕ Портала поставщиков (Kaggle GPU T4 ×2, DDP)

Основа — официальный ноутбук Laya (`laya_finetune_typed_decisions_2xT4_kaggle.ipynb`): та же DDP-схема,
RLCD и калибровка температур. Изменено: данные, базовая модель, число эпох, оценка.

**Задача.** Состояние — «Товар: <наименование> Категория: … Описание: <описание>» (карточки GetSku zakupki.mos.ru), вопрос на характеристику —
«Какое значение характеристики «X» указано в описании товара?», варианты — значения анкеты категории
и «не указано». Положительные примеры — заполненные у СТЕ характеристики, отрицательные — частые в
категории характеристики, которые у СТЕ не заполнены (ответ «не указано»).

### Настройки Kaggle
* **Accelerator:** `GPU T4 x2`; **Internet:** `On`.
* **Add Input → Datasets:** датасет `laya-ste-tenderhack` (файлы `train.jsonl`, `eval.jsonl`).
* Данные разбиты на шарды `train_NN.jsonl` по ~100 тыс. вопросов: одна сессия = один шард (~4 ч).
  Первая сессия: `LAYA_SHARD = "0"`. Следующие: сохраните Output прошлой сессии, добавьте его как Input
  (папка `laya_ste`) и поставьте следующий номер шарда — обучение продолжится с этого чекпойнта.
* Разбиение карточек 70/20/10: `train_NN.jsonl` / `val.jsonl` / `test.jsonl`. После каждой сессии — оценка
  на val; test — один раз в конце (`EVAL_SPLIT=test`). `EVAL_LIMIT` ограничивает число карточек оценки.
* Результат: `/kaggle/working/laya_ste` (папка для `laya.load`); после каждой эпохи —
  `/kaggle/working/laya_ste/checkpoint_latest`.
"""

DATA = """
import os, glob, json, torch
from transformers import AutoTokenizer
from huggingface_hub import snapshot_download
from laya.agent import _fix_tokenizer_config
from laya.train import items_from_rows

# Номер шарда этой сессии: 0, 1, 2, … (train_00.jsonl, train_01.jsonl, …).
os.environ["LAYA_SHARD"] = "0"
SHARD = int(os.environ["LAYA_SHARD"])

shards = sorted(glob.glob("/kaggle/input/**/train_*.jsonl", recursive=True))
assert shards, "Не найдены train_NN.jsonl: добавьте датасет laya-ste-tenderhack через Add Input"
DATA_DIR = os.path.dirname(shards[0])
print(f"Данные: {DATA_DIR}; шардов {len(shards)}; эта сессия — шард {SHARD}")

# База — многоязычная Laya (mmBERT, 100+ языков). Если к ноутбуку подключён выход прошлой сессии
# (папка laya_ste: rl_agent_config.json + model.safetensors), обучение продолжается с него.
base_dir = os.path.join(snapshot_download("convaiinnovations/laya", allow_patterns=["multilingual/*"]), "multilingual")
_fix_tokenizer_config(base_dir)
prev = sorted(os.path.dirname(p) for p in glob.glob("/kaggle/input/**/rl_agent_config.json", recursive=True)
              if "checkpoint_latest" not in p and os.path.exists(os.path.join(os.path.dirname(p), "model.safetensors")))
model_dir = prev[0] if prev else base_dir
print("Старт обучения с:", model_dir, "(продолжение)" if prev else "(базовая модель)")
tok = AutoTokenizer.from_pretrained(os.path.join(model_dir, "tokenizer"))
with open(os.path.join(model_dir, "rl_agent_config.json")) as f:
    cfg = json.load(f)

def read_rows(name):
    with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]

train_rows = read_rows(f"train_{SHARD:02d}.jsonl")
rows = [{"state": r["state"], "questions": r["questions"], "gold": r["gold"]} for r in train_rows]
items, skipped = items_from_rows(tok, rows, 1024, 256)
print(f"СТЕ {len(rows)}, обучающих вопросов {len(items)}, пропущено {skipped}")
torch.save(items, "/kaggle/working/train_items.pt")
"""

LAUNCH = """
OUTPUT_DIR = "/kaggle/working/laya_ste"
MODEL_DIR = model_dir
os.environ["LAYA_EPOCHS"] = "1"   # шард ~100 тыс. вопросов: 1 эпоха ≈ 4 часа на 2×T4 (лимит сессии — 12 ч)

cmd = f"torchrun --standalone --nproc_per_node=2 /kaggle/working/train_ddp.py {MODEL_DIR} {OUTPUT_DIR}"
print("Запуск:", cmd)
!{cmd}
"""

EVAL = """
import time, numpy as np, laya

NONE = "не указано"
EVAL_SPLIT = os.environ.get("EVAL_SPLIT", "val")   # val — после каждой сессии; test — только в самом конце
eval_rows = read_rows(f"{EVAL_SPLIT}.jsonl")
print(f"Оценка на {EVAL_SPLIT}: {len(eval_rows)} карточек")
EVAL_LIMIT = int(os.environ.get("EVAL_LIMIT", "0"))   # 0 — все отложенные карточки
eval_rows = eval_rows[:EVAL_LIMIT] if EVAL_LIMIT else eval_rows

def evaluate(agent, label):
    total = correct = 0
    by = {"значение": [0, 0], "не указано": [0, 0]}
    by_type = {}
    picked = []          # (уверенность, верно) для ответов-значений
    t0 = time.time()
    for r in eval_rows:
        ans = agent.predict(r["state"], r["questions"])["answers"]
        for qid, gold in r["expected"].items():
            a = ans[qid]
            ok = a["choice"] == gold
            group = "не указано" if gold == NONE else "значение"
            by[group][0] += ok
            by[group][1] += 1
            if r.get("query_type"):
                t = by_type.setdefault(r["query_type"], [0, 0])
                t[0] += ok
                t[1] += 1
            total += 1
            correct += ok
            if a["choice"] != NONE:
                picked.append((a["probabilities"][a["choice"]], ok))
    print(f"\\n[{label}] {total} вопросов за {time.time() - t0:.0f} с: точность {correct / total:.3f}")
    for g, (ok, n) in by.items():
        print(f"  эталон «{g}»: {ok}/{n} = {ok / max(n, 1):.3f}")
    for th in (0.5, 0.7, 0.8, 0.9):
        sel = [ok for p, ok in picked if p >= th]
        print(f"  выбрано значение с p ≥ {th}: {len(sel)}, из них верно {sum(sel) / max(len(sel), 1):.3f}")
    for t, (ok, n) in sorted(by_type.items()):
        print(f"  тип запроса {t:22} {ok}/{n} = {ok / max(n, 1):.3f}")
    return {"accuracy": correct / total, "by_group": by, "by_query_type": by_type}

base = laya.Agent(base_dir, device="cuda")
report = {"base": evaluate(base, "laya-multilingual, без дообучения")}
del base
torch.cuda.empty_cache()
tuned = laya.Agent(OUTPUT_DIR, device="cuda")
report["tuned"] = evaluate(tuned, "после дообучения")
json.dump(report, open(os.path.join(OUTPUT_DIR, "ste_eval.json"), "w"), ensure_ascii=False, indent=2)
"""

PUSH = """
# (Необязательно) публикация на Hugging Face: Add-ons → Secrets → HF_TOKEN, задайте NEW_REPO.
NEW_REPO = None  # например "your-name/laya-multilingual-ste"
if NEW_REPO:
    from huggingface_hub import HfApi
    from kaggle_secrets import UserSecretsClient
    api = HfApi(token=UserSecretsClient().get_secret("HF_TOKEN"))
    api.create_repo(NEW_REPO, exist_ok=True, private=True)
    api.upload_folder(folder_path=OUTPUT_DIR, repo_id=NEW_REPO, ignore_patterns=["checkpoint_latest/*"])
    print("Загружено:", NEW_REPO)
else:
    print("Публикация пропущена: скачайте /kaggle/working/laya_ste из вкладки Output.")
"""


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", type=Path, nargs="+", default=[ROOT / "data" / "laya" / "train.jsonl"],
                    help="один train.jsonl или шарды train_NN.jsonl")
    ap.add_argument("--eval", type=Path, nargs="+", default=[ROOT / "data" / "laya" / "eval.jsonl"],
                    help="val.jsonl и test.jsonl (или один eval.jsonl)")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    nb = json.loads(urllib.request.urlopen(SRC).read())
    cells = nb["cells"]
    train_src = "".join(cells[8]["source"])
    for old, new in [
        ("    EPOCHS = 4\n", "    EPOCHS = int(os.environ.get(\"LAYA_EPOCHS\", \"4\"))\n"),
        ('out_cfg["model_name"] = "laya-typed-decisions"', 'out_cfg["model_name"] = "laya-multilingual-ste"'),
    ]:
        assert old in train_src, f"в train_ddp.py нет: {old!r} — ноутбук Laya изменился"
        train_src = train_src.replace(old, new)

    new_cells = [
        md(INTRO),
        cells[1], cells[2],                       # проверка двух T4
        cells[3], cells[4],                       # установка
        md("## 3. Данные СТЕ и предобработка для DDP"), code(DATA),
        cells[7], code(train_src),                # train_ddp.py (эпохи из LAYA_EPOCHS)
        md("## 5. Запуск обучения на двух T4"), code(LAUNCH),
        md("## 6. Оценка: val после каждой сессии, test — в конце (EVAL_SPLIT)"), code(EVAL),
        md("## 7. (Необязательно) публикация на Hugging Face"), code(PUSH),
    ]
    nb["cells"] = new_cells
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    (out / "laya_finetune_ste_2xT4_kaggle.ipynb").write_text(json.dumps(nb, ensure_ascii=False, indent=1),
                                                             encoding="utf-8")
    for old in out.glob("train*.jsonl"):
        old.unlink()
    for i, t in enumerate(args.train):
        shutil.copy(t, out / (t.name if t.name.startswith("train_") else f"train_{i:02d}.jsonl"))
    for old in [*out.glob("eval*.jsonl"), *out.glob("val.jsonl"), *out.glob("test.jsonl")]:
        old.unlink()
    for e in args.eval:
        shutil.copy(e, out / e.name)
    (out / "dataset-metadata.json").write_text(json.dumps({
        "title": "laya-ste-tenderhack", "id": f"YOUR_KAGGLE_USERNAME/{DATASET_SLUG}",
        "licenses": [{"name": "other"}]}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Готово:", sorted(p.name for p in out.iterdir()))


if __name__ == "__main__":
    main()
