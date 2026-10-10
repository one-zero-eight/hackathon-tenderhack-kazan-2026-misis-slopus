"""Иерархическая классификация по дереву ОКПД2 с оценкой эмбеддингами.

Дерево: класс (17) → подкласс (17.1) → группа (17.12) → … → категория (17.12.14.110);
к узлам привязаны позиции КТРУ (код позиции = код ОКПД2 + номер). Узлы без позиций
в поддереве отрезаны, цепочки из одного ребёнка проходятся без решения.

Поиск — beam search, как в кулинарной книге TypeSafe «Hierarchical classification»:
в каждом раскрытом узле вероятности детей — softmax по оценкам, путь оценивается
средним геометрическим вероятностей рёбер, сохраняются K лучших путей.

Оценка ребёнка = α·cos(запрос, название узла ОКПД2)
              + (1−α)·среднее top-3 cos(запрос, названия позиций КТРУ в поддереве).
Названия ОКПД2 канцелярские («Мебель для офисов и предприятий торговли»), а названия
КТРУ ближе к языку заказчика («Парта»), поэтому нужны оба сигнала.

Последний шаг — внутри выбранных кодов ОКПД2 оставляются позиции с самыми похожими
названиями; дальше коды сужаются характеристиками (autofill).

`FlatRetriever` — базовая линия без дерева: ближайшие названия КТРУ по косинусу.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .embedder import Embedder
from .numbers import product_text
from .schema import KtruCandidate

DEFAULT_OKPD2 = Path(__file__).resolve().parents[2] / "data-archived" / "raw" / "okpd2.json"
CACHE_DIR = Path(__file__).resolve().parents[2] / "data-archived" / "cache"
TERMINAL = "*"  # псевдо-ребёнок «позиции, привязанные прямо к этому узлу»


@dataclass
class Node:
    code: str
    name: str
    children: list[str] = field(default_factory=list)
    positions: list[int] = field(default_factory=list)  # позиции, привязанные прямо сюда
    titles: list[int] = field(default_factory=list)  # индексы названий КТРУ во всём поддереве


@dataclass
class Path_:
    nodes: list[str]  # коды узлов от корня; последний — TERMINAL-узел или лист
    logp: float = 0.0
    decisions: int = 0
    done: bool = False
    edges: list[tuple[str, float]] = field(default_factory=list)  # (код, вероятность) по решениям
    raw: bool = False  # logp — сумма сырых оценок узлов, а не логарифмов вероятностей

    @property
    def score(self) -> float:
        """Среднее геометрическое вероятностей рёбер — глубокие и мелкие листья сравнимы.
        В режиме raw — средняя сырая оценка узлов пути."""
        if not self.decisions:
            return 1.0
        return self.logp / self.decisions if self.raw else math.exp(self.logp / self.decisions)


@dataclass
class Retrieval:
    candidates: list[int]  # индексы позиций каталога
    steps: list[tuple[str, int]]  # (описание шага, сколько позиций осталось)
    paths: list[Path_] = field(default_factory=list)
    titles: list[tuple[str, float]] = field(default_factory=list)


def _cache_key(texts: list[str], model: str) -> str:
    h = hashlib.sha1(model.encode())
    for t in texts:
        h.update(t.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def encode_cached(embedder: Embedder, texts: list[str], tag: str) -> np.ndarray:
    """Эмбеддинги справочника считаются один раз и кладутся в data/cache/."""
    model = getattr(getattr(embedder, "model", None), "name_or_path", type(embedder).__name__)
    path = CACHE_DIR / f"{tag}-{_cache_key(texts, str(model))}.npy"
    if path.exists():
        return np.load(path)
    vecs = np.asarray(embedder.encode(texts), dtype=np.float32)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    np.save(path, vecs)
    return vecs


class _Titles:
    """Уникальные названия позиций КТРУ и их эмбеддинги."""

    def __init__(self, positions: list[KtruCandidate], embedder: Embedder):
        self.texts = list(dict.fromkeys(p.name for p in positions))
        self.index = {t: i for i, t in enumerate(self.texts)}
        self.of_position = np.array([self.index[p.name] for p in positions])
        self.vecs = encode_cached(embedder, [t.lower() for t in self.texts], "titles")


def _refine_by_title(
    query: np.ndarray, pos_ids: list[int], titles: _Titles, delta: float
) -> tuple[list[int], list[tuple[str, float]]]:
    """Внутри кандидатов оставляем позиции с названиями не дальше delta от лучшего."""
    tids = sorted({int(titles.of_position[i]) for i in pos_ids})
    sims = titles.vecs[tids] @ query
    best = float(sims.max())
    keep = {t for t, s in zip(tids, sims) if s >= best - delta}
    ranked = sorted(((titles.texts[t], round(float(s), 3)) for t, s in zip(tids, sims) if t in keep),
                    key=lambda x: -x[1])
    return [i for i in pos_ids if int(titles.of_position[i]) in keep], ranked


class FlatRetriever:
    """Без дерева: позиции с ближайшими по косинусу названиями КТРУ."""

    def __init__(self, positions: list[KtruCandidate], embedder: Embedder, delta: float = 0.03):
        self.positions = positions
        self.embedder = embedder
        self.titles = _Titles(positions, embedder)
        self.delta = delta

    def retrieve(self, text: str) -> Retrieval:
        q = self.embedder.encode([product_text(text).lower()])[0]
        cands, ranked = _refine_by_title(q, list(range(len(self.positions))), self.titles, self.delta)
        name = ranked[0][0] if len(ranked) == 1 else f"{ranked[0][0]} (+{len(ranked) - 1})"
        return Retrieval(cands, [(f"название КТРУ: {name}", len(cands))], titles=ranked)


class OkpdTree:
    """Дерево ОКПД2, обрезанное до узлов, под которыми есть позиции КТРУ."""

    def __init__(self, records: list[dict], positions: list[KtruCandidate], embedder: Embedder,
                 alpha: float = 0.5, temperature: float = 0.03, beam: int = 5, top_titles: int = 3,
                 keep_ratio: float = 0.9, title_delta: float = 0.03, path_mode: str = "prob"):
        self.positions = positions
        self.embedder = embedder
        self.alpha, self.temperature, self.beam = alpha, temperature, beam
        self.top_titles, self.keep_ratio, self.title_delta = top_titles, keep_ratio, title_delta
        # prob — как в кулинарной книге TypeSafe (softmax в узле, среднее геометрическое);
        # raw — сырые оценки без нормировки в узле: нет перекоса в пользу узлов с малым числом детей.
        self.path_mode = path_mode

        okpd = {r["c"]: r for r in records}
        self.titles = _Titles(positions, embedder)
        self.nodes: dict[str, Node] = {"": Node("", "ОКПД2")}

        def anc(code: str) -> str:
            while code and code not in okpd:
                code = code[:-1].rstrip(".")
            return code

        def ensure(code: str) -> Node:
            if code in self.nodes:
                return self.nodes[code]
            r = okpd[code]
            node = self.nodes[code] = Node(code, r["n"])
            parent = r.get("p") if r.get("p") in okpd else ""  # разделы-буквы пропускаем: корень → классы
            (ensure(parent) if parent else self.nodes[""]).children.append(code)
            return node

        for i, p in enumerate(positions):
            code = anc(p.okpd2)
            if not code:
                continue
            ensure(code).positions.append(i)

        # Индексы названий КТРУ в поддереве — снизу вверх.
        def collect(code: str) -> set[int]:
            node = self.nodes[code]
            acc = {int(self.titles.of_position[i]) for i in node.positions}
            for c in node.children:
                acc |= collect(c)
            node.titles = sorted(acc)
            return acc

        collect("")
        codes = [c for c in self.nodes if c]
        self.code_index = {c: i for i, c in enumerate(codes)}
        self.name_vecs = encode_cached(embedder, [self.nodes[c].name.lower() for c in codes], "okpd2")

    @classmethod
    def load(cls, positions: list[KtruCandidate], embedder: Embedder, path: str | Path = DEFAULT_OKPD2,
             **kw) -> OkpdTree:
        return cls(json.loads(Path(path).read_text(encoding="utf-8")), positions, embedder, **kw)

    # --- оценка и поиск -------------------------------------------------

    def _options(self, code: str) -> list[str]:
        node = self.nodes[code]
        opts = list(node.children)
        if node.positions:
            opts.append(code + TERMINAL)
        return opts

    def _score(self, q: np.ndarray, option: str) -> float:
        terminal = option.endswith(TERMINAL)
        code = option.rstrip(TERMINAL)
        node = self.nodes[code]
        name_sim = float(self.name_vecs[self.code_index[code]] @ q) if code else 0.0
        tids = sorted({int(self.titles.of_position[i]) for i in node.positions}) if terminal else node.titles
        sims = np.sort(self.titles.vecs[tids] @ q)[::-1][: self.top_titles]
        return self.alpha * name_sim + (1 - self.alpha) * float(sims.mean())

    def child_probs(self, q: np.ndarray, code: str) -> list[tuple[str, float]]:
        opts = self._options(code)
        scores = np.array([self._score(q, o) for o in opts]) / self.temperature
        p = np.exp(scores - scores.max())
        p /= p.sum()
        return sorted(zip(opts, p.tolist()), key=lambda x: -x[1])

    def search(self, text: str) -> tuple[np.ndarray, list[Path_]]:
        q = self.embedder.encode([product_text(text).lower()])[0]
        beams = [Path_([""])]
        while not all(b.done for b in beams):
            nxt: list[Path_] = []
            for b in beams:
                if b.done:
                    nxt.append(b)
                    continue
                code = b.nodes[-1]
                opts = self._options(code)
                if len(opts) == 1:  # единственный ребёнок — не решение, просто спускаемся
                    o = opts[0]
                    nxt.append(Path_(b.nodes + [o], b.logp, b.decisions, o.endswith(TERMINAL), b.edges, b.raw))
                    continue
                if self.path_mode == "raw":
                    scored = sorted(((o, self._score(q, o)) for o in opts), key=lambda x: -x[1])
                    for o, sc in scored[: self.beam]:
                        nxt.append(Path_(b.nodes + [o], b.logp + sc, b.decisions + 1, o.endswith(TERMINAL),
                                         b.edges + [(o, round(sc, 4))], raw=True))
                    continue
                for o, p in self.child_probs(q, code)[: self.beam]:
                    nxt.append(Path_(b.nodes + [o], b.logp + math.log(max(p, 1e-12)), b.decisions + 1,
                                     o.endswith(TERMINAL), b.edges + [(o, round(p, 4))]))
            beams = sorted(nxt, key=lambda b: -b.score)[: self.beam]
        return q, beams

    def retrieve(self, text: str) -> Retrieval:
        q, paths = self.search(text)
        best = paths[0].score
        if self.path_mode == "raw":
            kept = [p for p in paths if p.score >= best - (1 - self.keep_ratio) * 0.1]
        else:
            kept = [p for p in paths if p.score >= best * self.keep_ratio]
        pos_ids = [i for p in kept for i in self.nodes[p.nodes[-1].rstrip(TERMINAL)].positions]
        codes = [p.nodes[-1].rstrip(TERMINAL) for p in kept]

        cands, ranked = _refine_by_title(q, pos_ids, self.titles, self.title_delta)
        # Показываем путь того луча, в чьём листе лежит выбранный товар.
        best_title = ranked[0][0]
        shown = next((p for p in kept if any(self.positions[i].name == best_title
                                             for i in self.nodes[p.nodes[-1].rstrip(TERMINAL)].positions)), paths[0])
        steps = [(f"ОКПД2 {c} {self.nodes[c].name[:60]}", len(self._subtree_positions(c)))
                 for c in shown.nodes[1:] if not c.endswith(TERMINAL)]
        if len(kept) > 1:
            steps.append((f"кодов ОКПД2 в луче: {len(codes)}", len(pos_ids)))
        name = ranked[0][0] if len(ranked) == 1 else f"{ranked[0][0]} (+{len(ranked) - 1})"
        steps.append((f"название КТРУ: {name}", len(cands)))
        return Retrieval(cands, steps, paths, ranked)

    def _subtree_positions(self, code: str) -> list[int]:
        out, stack = [], [code]
        while stack:
            node = self.nodes[stack.pop()]
            out += node.positions
            stack += node.children
        return out


class GroupedRetriever(FlatRetriever):
    """Снизу вверх: ближайшие названия КТРУ голосуют за узел ОКПД2 уровня `level`
    (весом exp(cos/τ)); дальше — позиции этого узла с самыми похожими названиями.
    Дерево здесь не ведёт поиск, а гасит одиночные случайные совпадения."""

    def __init__(self, positions: list[KtruCandidate], embedder: Embedder, level: int = 4,
                 top_n: int = 30, temperature: float = 0.02, delta: float = 0.03):
        super().__init__(positions, embedder, delta)
        self.level, self.top_n, self.temperature = level, top_n, temperature
        self.title_codes: dict[int, set[str]] = {}
        for i, p in enumerate(positions):
            self.title_codes.setdefault(int(self.titles.of_position[i]), set()).add(self._group(p.okpd2))

    def _group(self, okpd2: str) -> str:
        digits = okpd2.replace(".", "")[: self.level]
        return digits

    def retrieve(self, text: str) -> Retrieval:
        q = self.embedder.encode([product_text(text).lower()])[0]
        sims = self.titles.vecs @ q
        top = np.argsort(-sims)[: self.top_n]
        votes: dict[str, float] = {}
        for t in top:
            w = math.exp((float(sims[t]) - float(sims[top[0]])) / self.temperature)
            for g in self.title_codes[int(t)]:
                votes[g] = votes.get(g, 0.0) + w
        group = max(votes, key=votes.get)
        pos_ids = [i for i, p in enumerate(self.positions) if self._group(p.okpd2) == group]
        cands, ranked = _refine_by_title(q, pos_ids, self.titles, self.delta)
        name = ranked[0][0] if len(ranked) == 1 else f"{ranked[0][0]} (+{len(ranked) - 1})"
        return Retrieval(cands, [(f"группа ОКПД2 {group}", len(pos_ids)), (f"название КТРУ: {name}", len(cands))],
                         titles=ranked)
