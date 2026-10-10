"""Микротест скорости дообучения Laya в Kaggle (2×T4, тот же train_ddp.py, что в основном ноутбуке).

    uv run python experiments/make_kaggle_speedtest.py                    →  …/laya_speedtest_2xT4_kaggle.ipynb
    uv run python experiments/make_kaggle_speedtest.py --platform colab   →  …/laya_speedtest_colab.ipynb

Обучает 1 эпоху на первых N_ITEMS вопросах train_00.jsonl, берёт чистое время эпохи из вывода
train_ddp.py (без загрузки модели и калибровки) и пересчитывает на весь train: сколько часов на эпоху
и помещается ли обучение в одну сессию Kaggle (12 ч) или нужны шарды.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_kaggle_notebook import OUT, SRC, code, md  # noqa: E402

INTRO = """
# Микротест скорости дообучения `laya-multilingual` (Kaggle GPU T4 ×2)

Тот же `train_ddp.py` (DDP, RLCD, fp16, gradient checkpointing), что в основном ноутбуке, но 1 эпоха на
первых `N_ITEMS` вопросах `train_00.jsonl`. Итог — вопросов в секунду и прогноз на весь train.

**Настройки:** Accelerator `GPU T4 x2`, Internet `On`, Add Input — датасет `laya-ste-tenderhack`.
Время: ~10–15 минут (загрузка модели, 1 короткая эпоха, калибровка).
"""

DATA = """
import os, glob, json, time, statistics, torch
from transformers import AutoTokenizer
from huggingface_hub import snapshot_download
from laya.agent import _fix_tokenizer_config
from laya.train import items_from_rows

N_ITEMS = 3000   # вопросов в тесте; больше — точнее, но дольше

shards = sorted(glob.glob("/kaggle/input/**/train_*.jsonl", recursive=True))
assert shards, "Не найдены train_NN.jsonl: добавьте датасет laya-ste-tenderhack через Add Input"
DATA_DIR = os.path.dirname(shards[0])

model_dir = os.path.join(snapshot_download("convaiinnovations/laya", allow_patterns=["multilingual/*"]), "multilingual")
_fix_tokenizer_config(model_dir)
tok = AutoTokenizer.from_pretrained(os.path.join(model_dir, "tokenizer"))
with open(os.path.join(model_dir, "rl_agent_config.json")) as f:
    cfg = json.load(f)

# Сколько вопросов во всём train (для прогноза)
total_q = 0
for s in shards:
    with open(s, encoding="utf-8") as f:
        total_q += sum(len(json.loads(l)["questions"]) for l in f if l.strip())
print(f"Шардов {len(shards)}, вопросов во всём train: {total_q}")

# Первые вопросы train_00 → items (как в основном ноутбуке)
rows = []
with open(shards[0], encoding="utf-8") as f:
    for line in f:
        r = json.loads(line)
        rows.append({"state": r["state"], "questions": r["questions"], "gold": r["gold"]})
        if sum(len(x["questions"]) for x in rows) >= N_ITEMS:
            break
items, skipped = items_from_rows(tok, rows, 1024, 256)
items = items[:N_ITEMS]
torch.save(items, "/kaggle/working/train_items.pt")

state_tokens = [len(tok(r["state"])["input_ids"]) for r in rows]
print(f"Вопросов в тесте: {len(items)} (пропущено {skipped}) из {len(rows)} СТЕ")
print(f"Длина state в токенах: медиана {statistics.median(state_tokens):.0f}, "
      f"90-й перцентиль {sorted(state_tokens)[int(0.9 * len(state_tokens))]}, максимум {max(state_tokens)}")
"""

RUN = r"""
import re, subprocess

OUTPUT_DIR = "/kaggle/working/laya_speedtest"
os.environ["LAYA_EPOCHS"] = "1"
cmd = ["torchrun", "--standalone", "--nproc_per_node=2", "/kaggle/working/train_ddp.py", model_dir, OUTPUT_DIR]
t0 = time.time()
proc = subprocess.run(cmd, capture_output=True, text=True)
wall = time.time() - t0
log = proc.stdout + proc.stderr
print("\n".join(l for l in log.splitlines() if re.search(r"Starting|Epoch|Completed|Fitted|saved|Error|error", l))[-3000:])
assert proc.returncode == 0, log[-3000:]

train_items = int(re.search(r"Starting 2xT4 DDP training: (\d+) train items", log).group(1))
epoch_s = float(re.search(r"Completed in ([\d.]+)s", log).group(1))
rate = train_items / epoch_s
hours = total_q / rate / 3600
print(f"\nЧистая эпоха: {train_items} вопросов за {epoch_s:.0f} с → {rate:.1f} вопр/с = {rate * 3600:,.0f} вопр/ч "
      f"(весь запуск с загрузкой и калибровкой — {wall:.0f} с)")
print(f"Прогноз на весь train ({total_q} вопросов): {hours:.1f} ч на эпоху")
if hours <= 10:
    print("→ помещается в одну сессию Kaggle (12 ч): шарды не нужны, обучайте на всех сразу.")
else:
    print(f"→ не помещается в одну сессию: нужно ~{int(hours // 10) + 1} сессий (шардов) по ≤ 10 ч.")
"""


COLAB_INTRO = """
# Микротест скорости дообучения `laya-multilingual` — Google Colab

Тот же `train_ddp.py`, что в основном ноутбуке, но на тех GPU, что даёт Colab (обычно одна T4; L4/A100 в
платных тарифах). Эффективный батч держится 64: шагов накопления = 8 / число GPU.

**Подготовка.** Runtime → Change runtime type → GPU. Положите `train_00.jsonl` (из `laya_v3/kaggle/`) на Google
Drive в папку `MyDrive/laya-ste-tenderhack/` — для замера хватит одного шарда; если положить все
`train_*.jsonl`, общее число вопросов посчитается по ним, иначе берётся зашитое значение.
Время: ~10–20 минут.
"""

COLAB_GPU = """
import os, torch
n_gpu = torch.cuda.device_count()
assert n_gpu >= 1, "GPU не найдена: Runtime → Change runtime type → GPU"
for i in range(n_gpu):
    p = torch.cuda.get_device_properties(i)
    print(f"GPU {i}: {p.name} ({p.total_memory / 1e9:.1f} GB)")
os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
os.environ["LAYA_GRAD_ACCUM"] = str(max(1, 8 // n_gpu))   # эффективный батч 8 × n_gpu × accum = 64
WORK = "/content/laya_work"
os.makedirs(WORK, exist_ok=True)
"""

COLAB_INSTALL = """
!pip install -q -U "laya>=0.1.6" "transformers>=4.48.0" safetensors huggingface_hub scipy accelerate
import laya, transformers
print("laya", laya.__version__, "| transformers", transformers.__version__, "| torch", torch.__version__)
"""

COLAB_DRIVE = """
from google.colab import drive
drive.mount("/content/drive")
"""


def colab_notebook(nb: dict, train_src: str, total_q: int) -> dict:
    data = DATA.replace(
        'shards = sorted(glob.glob("/kaggle/input/**/train_*.jsonl", recursive=True))\n'
        'assert shards, "Не найдены train_NN.jsonl: добавьте датасет laya-ste-tenderhack через Add Input"',
        'shards = sorted(glob.glob("/content/drive/MyDrive/laya-ste-tenderhack/train_*.jsonl"))\n'
        'assert shards, "Не найдены train_NN.jsonl в MyDrive/laya-ste-tenderhack/"',
    ).replace(
        "# Сколько вопросов во всём train (для прогноза)\ntotal_q = 0\nfor s in shards:",
        f"# Сколько вопросов во всём train (для прогноза): по шардам на Drive или зашитое значение\n"
        f"TOTAL_Q_ALL = {total_q}\ntotal_q = 0\nfor s in shards:",
    ).replace(
        'print(f"Шардов {len(shards)}, вопросов во всём train: {total_q}")',
        'if len(shards) < 5:\n    total_q = TOTAL_Q_ALL\nprint(f"Шардов на Drive {len(shards)}, вопросов во всём train: {total_q}")',
    ).replace("/kaggle/working", WORK_PATH)
    run = RUN.replace('"--nproc_per_node=2"', 'f"--nproc_per_node={n_gpu}"').replace("/kaggle/working", WORK_PATH)
    run = run.replace(
        'print(f"Прогноз на весь train ({total_q} вопросов): {hours:.1f} ч на эпоху")',
        'print(f"Прогноз на весь train ({total_q} вопросов) на этой машине: {hours:.1f} ч на эпоху")\n'
        'if n_gpu == 1 and "T4" in torch.cuda.get_device_name(0):\n'
        '    print(f"Грубо для Kaggle 2×T4 (DDP, ~×1.8): {hours / 1.8:.1f} ч на эпоху")',
    )
    train = (train_src.replace("    GRAD_ACCUM = 4 ", '    GRAD_ACCUM = int(os.environ.get("LAYA_GRAD_ACCUM", "4")) ')
             .replace("/kaggle/working", WORK_PATH))
    nb["cells"] = [
        md(COLAB_INTRO),
        md("## 1. GPU"), code(COLAB_GPU),
        md("## 2. Установка и Google Drive"), code(COLAB_INSTALL), code(COLAB_DRIVE),
        md("## 3. Данные: первые N_ITEMS вопросов, длина входа, объём train"), code(data),
        md("## 4. Скрипт обучения (как в основном ноутбуке)"), code("%%writefile " + WORK_PATH + "/train_ddp.py\n" + train.split("\n", 1)[1]),
        md("## 5. Одна короткая эпоха и прогноз"), code(run.replace(WORK_PATH + "/train_ddp.py", WORK_PATH + "/train_ddp.py")),
    ]
    nb.setdefault("metadata", {})["accelerator"] = "GPU"
    return nb


WORK_PATH = "/content/laya_work"


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", choices=["kaggle", "colab"], default="kaggle")
    args = ap.parse_args()
    nb = json.loads(urllib.request.urlopen(SRC).read())
    cells = nb["cells"]
    train_src = "".join(cells[8]["source"])
    old = "    EPOCHS = 4\n"
    assert old in train_src, "в train_ddp.py нет EPOCHS = 4 — ноутбук Laya изменился"
    train_src = train_src.replace(old, '    EPOCHS = int(os.environ.get("LAYA_EPOCHS", "4"))\n')
    if args.platform == "colab":
        data_dir = OUT.parent.parent / "laya_v3"
        total_q = sum(len(json.loads(l)["questions"]) for t in sorted(data_dir.glob("train_*.jsonl"))
                      for l in t.open(encoding="utf-8") if l.strip())
        nb = colab_notebook(nb, train_src, total_q)
        out = data_dir / "kaggle"
        path = out / "laya_speedtest_colab.ipynb"
        path.write_text(json.dumps(nb, ensure_ascii=False, indent=1), encoding="utf-8")
        print("Готово:", path, "| вопросов в train:", total_q)
        return
    nb["cells"] = [
        md(INTRO),
        cells[1], cells[2],          # проверка двух T4
        cells[3], cells[4],          # установка
        md("## 3. Данные: первые N_ITEMS вопросов, длина входа, объём train"), code(DATA),
        cells[7], code(train_src),   # train_ddp.py — как в основном ноутбуке
        md("## 5. Одна короткая эпоха и прогноз на весь train"), code(RUN),
    ]
    out = OUT.parent.parent / "laya_v3" / "kaggle"
    out.mkdir(parents=True, exist_ok=True)
    path = out / "laya_speedtest_2xT4_kaggle.ipynb"
    path.write_text(json.dumps(nb, ensure_ascii=False, indent=1), encoding="utf-8")
    print("Готово:", path)


if __name__ == "__main__":
    main()
