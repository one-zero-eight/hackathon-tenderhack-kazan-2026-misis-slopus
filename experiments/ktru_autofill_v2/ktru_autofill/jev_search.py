"""Поиск товара без эмбеддингов: спуск по дереву ОКПД2, где на каждом узле ребёнка выбирает Jev.

Вместо `hierarchy.py` (оценка узла эмбеддингом): на узле Jev получает Choice по детям с названием, числом позиций КТРУ
и примерами их названий, плюс вариант «ни один не подходит». Путь оценивается средним геометрическим вероятностей
выбора (как в кулинарной книге TypeSafe «Hierarchical classification»), сохраняются `beam` лучших. Цепочки из одного
ребёнка проходятся без вопроса. В конце Jev выбирает название позиции среди позиций выбранных узлов.

События для интерфейса: emit("поиск", {...}) на каждом уровне и при выборе названия.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any, Callable

from .jev_client import JevClient
from .ktru_store import KtruStore, OkpdNode
from .numbers import product_text
from .text import lemmatize

NONE = "ни один из вариантов не подходит"
OWN = "позиции, отнесённые непосредственно к этому узлу"
MAX_OPTIONS = 250
RULES = [
    "Решай по виду и назначению товара, а не по цифрам, брендам и артикулам.",
    "Описание может быть с опечатками и неполным: опирайся на смысл.",
    "Отличай товар от работы и услуги.",
]


def describe(count: int, examples: list[str]) -> str:
    """Описание варианта одной короткой строкой: у Nimble лимит 6–8 тыс. токенов на запрос, а вложенный JSON
    у 60 вариантов его превышает (7 581 токен на верхнем уровне)."""
    return f"{count} поз.; напр.: " + "; ".join(e[:60] for e in examples)


@dataclass
class _Path:
    node: str | None  # текущий узел; None — вершина классификатора
    logp: float = 0.0
    decisions: int = 0
    done: bool = False
    stopped: bool = False  # Jev выбрал «ни один не подходит»
    trail: list[tuple[str, float]] = field(default_factory=list)  # (код узла, вероятность выбора)

    @property
    def score(self) -> float:
        return self.logp / self.decisions if self.decisions else 0.0


@dataclass
class SearchResult:
    rows: list[int]
    families: list[tuple[str, float]]
    steps: list[tuple[str, int]]
    warnings: list[str] = field(default_factory=list)
    path: list[str] = field(default_factory=list)


class JevSearch:
    def __init__(self, store: KtruStore, jev: JevClient, beam: int = 3, min_prob: float = 0.03,
                 name_limit: int = int(os.environ.get("JEV_NAME_LIMIT", MAX_OPTIONS)), pick_prob: float = 0.15):
        self.store, self.jev, self.beam, self.min_prob = store, jev, beam, min_prob
        self.name_limit, self.pick_prob = name_limit, pick_prob

    # --- спуск по дереву --------------------------------------------------------------------------------------

    def _question(self, node: OkpdNode | None, children: list[OkpdNode], own: bool) -> dict[str, Any]:
        tree = self.store.tree
        criteria: dict[str, Any] = {}
        for ch in children:
            criteria[ch.title[:160]] = describe(ch.total, self.store.examples(ch.code, 2))
        if own and node is not None:
            criteria[OWN] = describe(len(node.positions), [self.store.names[r] for r in node.positions[:2]])
        criteria[NONE] = "Товар не относится ни к одному из перечисленных разделов"
        return {"type": "choice", "instructions": {
            "question": "К какому из разделов классификатора относится `товар`?",
            "текущий раздел": node.title if node else "верхний уровень (секции классификатора)", "правило": RULES},
            "criteria": criteria}

    def _options(self, children: list[OkpdNode], text: str) -> list[OkpdNode]:
        if len(children) <= MAX_OPTIONS - 2:
            return children
        words = set(lemmatize(text).split())  # слишком много детей: оставляем лексически близких
        score = lambda n: len(words & set(lemmatize(n.name).split()))
        return sorted(children, key=lambda n: (-score(n), -n.total))[:MAX_OPTIONS - 2]

    def descend(self, text: str, emit: Callable[[str, dict], None] | None = None) -> list[_Path]:
        """Лучшие `beam` путей до листьев. Остановленные Jev («ни один не подходит») лежат в `self.stopped`."""
        tree = self.store.tree
        frontier = [_Path(None)]
        self.stopped: list[_Path] = []
        for level in range(1, 14):
            expanded: list[_Path] = []
            jobs: list[tuple[_Path, list[OkpdNode], bool]] = []
            for p in frontier:
                if p.done:
                    expanded.append(p)
                    continue
                node = tree.nodes[p.node] if p.node else None
                children = tree.children(p.node)
                own = bool(node and node.positions)
                if not children:  # лист
                    p.done = True
                    expanded.append(p)
                elif len(children) == 1 and not own:  # цепочка без выбора
                    expanded.append(_Path(children[0].code, p.logp, p.decisions, False, False,
                                          p.trail + [(children[0].code, 1.0)]))
                else:
                    jobs.append((p, self._options(children, text), own))
            if jobs:
                state = {"товар": text}
                answers = self.jev.ask_many([(state, {"q": self._question(tree.nodes[p.node] if p.node else None, ch, own)})
                                             for p, ch, own in jobs])
                for (p, children, own), answer in zip(jobs, answers):
                    by_title = {c.title[:160]: c for c in children}
                    for label, prob in answer["q"]["probabilities"].items():
                        if prob < self.min_prob:
                            continue
                        logp = p.logp + math.log(max(prob, 1e-9))
                        if label == NONE:
                            self.stopped.append(_Path(p.node, logp, p.decisions + 1, True, True, p.trail))
                        elif label == OWN:
                            expanded.append(_Path(p.node, logp, p.decisions + 1, True, False, p.trail + [(p.node, prob)]))
                        elif label in by_title:
                            c = by_title[label]
                            expanded.append(_Path(c.code, logp, p.decisions + 1, False, False, p.trail + [(c.code, prob)]))
            expanded.sort(key=lambda q: -q.score)
            frontier = expanded[: self.beam]
            if emit and (jobs or level == 1):
                emit("поиск", {"уровень": level, "ветви": [
                    {"узел": tree.nodes[p.node].title if p.node else None, "оценка": round(math.exp(p.score), 3)}
                    for p in frontier]})
            if frontier and all(p.done for p in frontier):
                break
        return [p for p in frontier if p.done and p.node]

    # --- название позиции ----------------------------------------------------------------------------------------

    def pick_names(self, text: str, leaves: list[_Path], emit: Callable[[str, dict], None] | None = None,
                   prior: dict[str, float] | None = None) -> tuple[list[int], list[tuple[str, float]], str | None]:
        tree, names = self.store.tree, self.store.names
        entries: dict[tuple[str, str], list[int]] = {}
        for p in leaves:
            for row in tree.positions_under(p.node):
                entries.setdefault((names[row], p.node), []).append(row)
        if not entries:
            return [], [], None
        keys = list(entries)
        if len(keys) > self.name_limit:  # слишком много названий: лексически близкие и частые
            words = set(lemmatize(text).split())
            near = prior or {}  # близость названия к запросу по эмбеддингу — первый ключ, лексика и частота — запасные
            keys.sort(key=lambda k: (-near.get(k[0], -1.0), -len(words & set(lemmatize(k[0]).split())), -len(entries[k])))
            keys = keys[: self.name_limit]
        clash = {n for n, c in __import__("collections").Counter(k[0] for k in keys).items() if c > 1}
        label = {k: (f"{k[0]} [{k[1]}]" if k[0] in clash else k[0]) for k in keys}
        criteria: dict[str, Any] = {}
        for k in keys:
            cand = self.store.candidate(entries[k][0])
            criteria[label[k]] = (f"{tree.nodes[k[1]].name[:70]}; {len(entries[k])} поз.; хар-ки: "
                                  + ", ".join(c.name[:30] for c in cand.characteristics[:5]))
        criteria[NONE] = "Ни одно из названий не подходит товару"
        question = {"type": "choice", "instructions": {
            "question": "Какое из названий позиций КТРУ соответствует `товар`?", "правило": RULES}, "criteria": criteria}
        probs = self.jev.ask({"товар": text}, {"q": question})["q"]["probabilities"]
        ranked = sorted(((probs.get(label[k], 0.0), k) for k in keys), reverse=True)
        top = ranked[0][0]
        # Лучшее название берём всегда (если Jev не отдал всё «ни одному»), остальные — по абсолютному и относительному порогу:
        # вероятность между 200 названиями бывает размазана, и абсолютный порог в одиночку не пропускал никого.
        none = probs.get(NONE, 0.0)
        chosen = [(p, k) for i, (p, k) in enumerate(ranked)
                  if (i == 0 and p >= 0.05 and p >= 0.5 * none) or p >= max(self.pick_prob, 0.35 * top)][:5]
        rows = sorted(r for _, k in chosen for r in entries[k])
        families = [(k[0], round(p, 3)) for p, k in chosen]
        if emit:
            emit("поиск", {"название": [{"название": n, "оценка": p} for n, p in families]})
        return rows, families, chosen[0][1][1] if chosen else None

    # --- всё вместе --------------------------------------------------------------------------------------------------

    def find(self, text: str, emit: Callable[[str, dict], None] | None = None) -> SearchResult:
        tree = self.store.tree
        query = product_text(text)
        leaves = self.descend(query, emit)
        if not leaves:
            return SearchResult([], [], [], ["Не удалось определить раздел классификатора: опишите товар подробнее"])
        rows, families, best_node = self.pick_names(query, leaves, emit)
        if not rows:
            return SearchResult([], [], [], ["Не удалось определить название позиции КТРУ"])
        path = [n for n in tree.path(best_node or leaves[0].node) if not tree.is_section(n.code)]
        steps = [(f"ОКПД2 {n.title}", n.total) for n in path]
        steps.append((f"товар: {', '.join(n for n, _ in families)}", len(rows)))
        return SearchResult(rows, families, steps, [], [n.code for n in path])
