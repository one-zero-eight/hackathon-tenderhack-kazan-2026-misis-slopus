"""Каталог КТРУ (ktru-actual.parquet) и дерево ОКПД2 (okpd2-moy-zakupki.parquet) для API на Jev + регулярках.

Позиции КТРУ привязаны к узлам ОКПД2 (в данных — 2 730 узлов 6–7 уровня). Характеристика КТРУ переводится в
`Characteristic`: тип «1» (значения `text`) — перечень; тип «2» (диапазоны `ranges` и точные `concrete_values`
с единицей ОКЕИ) — число или счётная величина. Несколько диапазонов характеристики — альтернативы, в `range`
хранится их оболочка. Свободные характеристики (без значений) пропускаются: сужать нечем.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from .schema import Characteristic, KtruCandidate

DEFAULT_DATA = Path(__file__).resolve().parents[2] / "zakupki-reverse" / "data"
COUNT_UNITS = {"штука", "единица", "пара", "комплект", "набор"}


def clean_unit(name: str | None) -> str | None:
    """«Кубический сантиметр;^миллилитр» → «Кубический сантиметр; миллилитр»."""
    if not name:
        return None
    return name.replace(";^", "; ").replace("^", " ").strip() or None


def _num(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def convert_characteristic(raw: dict[str, Any]) -> Characteristic | None:
    """Характеристика КТРУ (как в parquet) → `Characteristic`; None — свободная, значений нет."""
    name = (raw.get("name") or "").strip()
    values = [v for v in raw.get("values") or [] if v]
    if not name or not values:
        return None
    texts = list(dict.fromkeys(v["text"].strip() for v in values if (v.get("text") or "").strip()))
    if texts:
        return Characteristic(name, "enum", texts)
    lows, highs, unit = [], [], None
    for v in values:
        unit = unit or clean_unit((v.get("unit") or {}).get("name"))
        for r in v.get("ranges") or []:
            lo, hi = _num(r.get("min")), _num(r.get("max"))
            lows.append(-math.inf if lo is None else lo)
            highs.append(math.inf if hi is None else hi)
        for c in v.get("concrete_values") or []:
            x = _num(c)
            if x is not None:
                lows.append(x)
                highs.append(x)
    if not lows:
        return None
    count = bool(unit) and unit.lower().split(";")[0].strip() in COUNT_UNITS
    return Characteristic(name, "count" if count else "number", [], unit=unit,
                          unit_aliases=["шт", "штука"] if count else [], range=(min(lows), max(highs)))


@dataclass
class OkpdNode:
    code: str
    name: str
    parent: str | None = None
    children: list[str] = field(default_factory=list)
    positions: list[int] = field(default_factory=list)  # строки каталога, привязанные прямо к узлу
    total: int = 0  # позиций в поддереве

    @property
    def title(self) -> str:
        return f"{self.code} {self.name}"


class OkpdTree:
    """Дерево ОКПД2, обрезанное до узлов, в поддереве которых есть позиции КТРУ."""

    def __init__(self, nodes: dict[str, OkpdNode], roots: list[str]):
        self.nodes, self.roots = nodes, roots

    @classmethod
    def build(cls, rows: Iterable[dict[str, Any]], position_okpd: list[str]) -> OkpdTree:
        """rows — id, code, name, parent (id родителя или None); position_okpd[i] — узел i-й позиции."""
        rows = list(rows)
        code_of = {r["id"]: r["code"] for r in rows}
        nodes = {r["code"]: OkpdNode(r["code"], r["name"], code_of.get(r.get("parent"))) for r in rows}
        for row, okpd in enumerate(position_okpd):
            if okpd not in nodes:  # узла нет в классификаторе — вешаем на ближайшего предка по префиксу
                parts = okpd.split(".")
                okpd = next((".".join(parts[:i]) for i in range(len(parts) - 1, 0, -1) if ".".join(parts[:i]) in nodes), None)
                if okpd is None:
                    continue
            nodes[okpd].positions.append(row)
        for node in nodes.values():
            node.total = len(node.positions)
        # накопить поддеревья снизу вверх: сначала самые глубокие коды
        for node in sorted(nodes.values(), key=lambda n: -len(n.code)):
            if node.parent in nodes and node.total:
                nodes[node.parent].total += node.total
        for node in nodes.values():
            if node.parent in nodes and node.total:
                nodes[node.parent].children.append(node.code)
        for node in nodes.values():
            node.children.sort()
        roots = sorted(n.code for n in nodes.values() if n.total and n.parent not in nodes)
        # Секции (A–U) слишком общие для выбора: первый вопрос задаётся по классам («17 Бумага и изделия из бумаги»).
        flat: list[str] = []
        for code in roots:
            node = nodes[code]
            flat += node.children if not code[:1].isdigit() and node.children else [code]
        return cls(nodes, sorted(flat))

    @staticmethod
    def is_section(code: str) -> bool:
        return not code[:1].isdigit()

    def children(self, code: str | None) -> list[OkpdNode]:
        codes = self.roots if code is None else self.nodes[code].children
        return [self.nodes[c] for c in codes]

    def path(self, code: str) -> list[OkpdNode]:
        out, cur = [], self.nodes.get(code)
        while cur is not None:
            out.append(cur)
            cur = self.nodes.get(cur.parent) if cur.parent else None
        return out[::-1]

    def positions_under(self, code: str) -> list[int]:
        out, stack = [], [code]
        while stack:
            node = self.nodes[stack.pop()]
            out += node.positions
            stack += node.children
        return sorted(out)


class KtruStore:
    """Позиции КТРУ по строкам каталога: код, название, узел ОКПД2; характеристики читаются по требованию."""

    def __init__(self, tree: OkpdTree, codes: list[str], names: list[str], okpd: list[str],
                 characteristics: Any):
        self.tree, self.codes, self.names, self.okpd = tree, codes, names, okpd
        self._characteristics = characteristics  # row -> список характеристик parquet
        self.row_of = {c: i for i, c in enumerate(codes)}
        self.candidate = lru_cache(maxsize=20000)(self._candidate)
        self._examples: dict[str, list[str]] = {}

    @classmethod
    def load(cls, data_dir: Path = DEFAULT_DATA) -> KtruStore:
        import pyarrow.compute as pc
        import pyarrow.parquet as pq

        table = pq.read_table(data_dir / "ktru-actual.parquet", columns=["code", "name", "status", "okpd2", "characteristics"])
        table = table.filter(pc.equal(table["status"], "ACTIVE"))
        okpd = [(o or {}).get("code") or "" for o in table["okpd2"].to_pylist()]
        keep = [i for i, o in enumerate(okpd) if o]
        if len(keep) != len(okpd):
            table = table.take(keep)
            okpd = [okpd[i] for i in keep]
        rows = pq.read_table(data_dir / "okpd2-moy-zakupki.parquet", columns=["id", "code", "name", "parent"]).to_pylist()
        rows = [{**r, "parent": None if r["parent"] is None or r["parent"] != r["parent"] else int(r["parent"])} for r in rows]
        tree = OkpdTree.build(rows, okpd)
        column = table["characteristics"]
        return cls(tree, table["code"].to_pylist(), table["name"].to_pylist(), okpd, lambda i: column[i].as_py() or [])

    @classmethod
    def from_records(cls, records: list[dict[str, Any]], okpd_rows: list[dict[str, Any]]) -> KtruStore:
        """Для тестов: records — code, name, okpd2, characteristics (как в parquet)."""
        tree = OkpdTree.build(okpd_rows, [r["okpd2"] for r in records])
        return cls(tree, [r["code"] for r in records], [r["name"] for r in records], [r["okpd2"] for r in records],
                   lambda i: records[i].get("characteristics") or [])

    def __len__(self) -> int:
        return len(self.codes)

    def _candidate(self, row: int) -> KtruCandidate:
        chars, seen = [], set()
        for raw in self._characteristics(row):
            ch = convert_characteristic(raw)
            if ch and ch.name not in seen:
                seen.add(ch.name)
                chars.append(ch)
        return KtruCandidate(self.codes[row], self.names[row], chars, okpd2=self.okpd[row])

    def candidates(self, rows: Iterable[int]) -> list[KtruCandidate]:
        return [self.candidate(r) for r in rows]

    def examples(self, node_code: str, k: int = 3) -> list[str]:
        """Самые частые названия позиций в поддереве узла — подсказка для выбора ветки."""
        if node_code not in self._examples:
            counter = Counter(self.names[r] for r in self.tree.positions_under(node_code))
            self._examples[node_code] = [n for n, _ in counter.most_common(3)]
        return self._examples[node_code][:k]
