"""Автозаполнение КТРУ на Jev + регулярках: то же, что `Autofiller`, без дерева-эмбеддингов и векторов.

    поиск товара   JevSearch   спуск по ОКПД2 (Jev выбирает ветку) → название позиции (Jev)
    анкета         build_form  характеристики оставшихся позиций, объединённые по имени
    значения       jev_extract числа находят регулярки, значение выбирает Jev
    сужение кодов  narrow_result общий с CLI: в порядке упоминания, противоречие отбрасывает позицию

Результат — тот же `Result`, поэтому JSON совпадает с `ktru-autofill`; поверх добавляются предупреждения
(опечатки и явные несоответствия) и текст «что уточнить».
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .autofill import Result, Step, narrow_result
from .jev_client import JevClient
from .jev_extract import extract
from .jev_search import JevSearch, _Path
from .ktru_store import KtruStore
from .schema import build_form

Emit = Callable[[str, dict], None]


@dataclass
class Outcome:
    result: Result
    rows: list[int]  # кандидаты после поиска товара (до сужения характеристиками) — для диалога
    families: list[tuple[str, float]]
    warnings: list[str] = field(default_factory=list)
    search_steps: list[Step] = field(default_factory=list)  # шаги поиска товара — для диалога
    seconds: float = 0.0
    jev: dict[str, int] = field(default_factory=dict)

    def to_dict(self, details: bool = False) -> dict[str, Any]:
        r = self.result
        out = r.to_dict(details=details)
        problems = [{"характеристика": m.характеристика, "значение": m.значение, "фрагмент": m.фрагмент,
                     "причина": m.предупреждение} for m in r.matches if m.предупреждение]
        problems += [{"характеристика": None, "значение": None, "фрагмент": None, "причина": w} for w in self.warnings]
        out["предупреждения"] = problems
        out["уточнить"] = clarification(r, self.warnings)
        out["служебное"] = {"секунд": round(self.seconds, 2), "jev": self.jev}
        if details:
            out["товары"] = [{"название": n, "оценка": p} for n, p in self.families]
        return out


def clarification(r: Result, warnings: list[str]) -> dict[str, Any] | None:
    """Что сделать пользователю, если код КТРУ не определился однозначно."""
    if r.code:
        return None
    if not r.candidates:
        return {"сообщение": warnings[0] if warnings else "Подходящих позиций КТРУ не найдено.",
                "шаги": ["Назовите товар одним-двумя словами (например, «бумага для принтера»).",
                         "Добавьте назначение или тип, затем размеры и материал."]}
    steps = []
    if r.question:
        options = ", ".join(f"{k} ({n})" for k, n in list(r.question["варианты"].items())[:8])
        steps.append(f"Укажите «{r.question['характеристика']}»: {options}.")
    for m in r.matches:
        if m.предупреждение:
            steps.append(f"Проверьте «{m.характеристика}» = {m.значение}: {m.предупреждение}.")
    if r.suggestions:
        steps += [f"Подтвердите «{m.характеристика}» = {m.значение} (фрагмент «{m.фрагмент}»)." for m in r.suggestions]
    return {"сообщение": f"Осталось позиций КТРУ: {len(r.candidates)}.", "шаги": steps or ["Добавьте характеристики товара."]}


class JevAutofiller:
    """`neighbors` — фабрика поиска по соседям-СТЕ (`NeighborSearch`), вызывается при первом запросе `search="neighbors"`;
    бросает исключение или возвращает None, если индекса или эмбеддингов нет — тогда поиск идёт по дереву."""

    def __init__(self, store: KtruStore, jev: JevClient, beam: int = 3,
                 neighbors: Callable[[JevSearch], Any] | None = None):
        self.store, self.jev = store, jev
        self.search = JevSearch(store, jev, beam=beam)
        self._neighbors_factory, self._neighbors, self._neighbors_error = neighbors, None, None

    def neighbor_search(self) -> Any | None:
        if self._neighbors is None and self._neighbors_factory is not None and self._neighbors_error is None:
            try:
                self._neighbors = self._neighbors_factory(self.search)
                if self._neighbors is None:
                    self._neighbors_error = "нет индекса соседей-СТЕ (LMDB-кеша векторов или npz)"
            except Exception as e:
                self._neighbors_error = f"{type(e).__name__}: {e}"
        return self._neighbors

    @property
    def neighbors_available(self) -> bool | None:
        """None — ещё не проверялось."""
        if self._neighbors_factory is None:
            return False
        return None if self._neighbors is None and self._neighbors_error is None else self._neighbors is not None

    def candidates(self, text: str, emit: Emit | None = None, okpd2: str | None = None,
                   codes: list[str] | None = None, search: str = "tree"
                   ) -> tuple[list[int], list[tuple[str, float]], list[Step], list[str]]:
        """Шаг поиска товара: строки каталога, семейства, шаги сужения, предупреждения."""
        steps = [Step("весь каталог", len(self.store))]
        if codes is not None:
            rows = [self.store.row_of[c] for c in codes if c in self.store.row_of]
            missing = [c for c in codes if c not in self.store.row_of]
            steps.append(Step("кандидаты шага 2", len(rows)))
            return rows, [], steps, [f"нет в каталоге КТРУ: {', '.join(missing)}"] if missing else []
        if okpd2:
            if okpd2 not in self.store.tree.nodes:
                return [], [], steps, [f"нет узла ОКПД2 {okpd2}"]
            node = self.store.tree.nodes[okpd2]
            leaf = _Path(okpd2, done=True)
            rows, families, _ = self.search.pick_names(text, [leaf], emit)
            steps += [Step(f"ОКПД2 {n.title}", n.total) for n in self.store.tree.path(okpd2) if not self.store.tree.is_section(n.code)]
            if families:
                steps.append(Step(f"товар: {', '.join(n for n, _ in families)}", len(rows)))
            return rows, families, steps, [] if rows else [f"в узле {node.title} не нашлось подходящей позиции"]
        notes: list[str] = []
        found = None
        if search == "neighbors":
            neighbors = self.neighbor_search()
            if neighbors is None:
                notes.append(f"поиск по соседям-СТЕ недоступен ({self._neighbors_error or 'не настроен'}): использован спуск по дереву")
            else:
                try:
                    found = neighbors.find(text, emit)
                except Exception as e:  # EmbedError и сбои чтения: не роняем запрос
                    notes.append(f"поиск по соседям-СТЕ не удался ({e}): использован спуск по дереву")
                if found is not None and not found.rows:
                    notes += found.warnings + ["по соседям-СТЕ товар не найден: использован спуск по дереву"]
                    found = None
        if found is None:
            found = self.search.find(text, emit)
        steps += [Step(desc, n) for desc, n in found.steps]
        return found.rows, found.families, steps, notes + found.warnings

    def fill(self, text: str, emit: Emit | None = None, *, rows: list[int] | None = None,
             families: list[tuple[str, float]] | None = None, okpd2: str | None = None,
             codes: list[str] | None = None, search_steps: list[Step] | None = None, search: str = "tree") -> Outcome:
        """`rows` — кандидаты прежнего поиска (диалог): поиск товара не повторяется, `search_steps` — его шаги."""
        started, before = time.perf_counter(), dict(self.jev.stats)
        warnings: list[str] = []
        if rows is None:
            rows, families, steps, warnings = self.candidates(text, emit, okpd2, codes, search)
        else:
            steps = list(search_steps or [Step("весь каталог", len(self.store)), Step("кандидаты поиска", len(rows))])
        search_done = list(steps)
        cands = self.store.candidates(rows)
        if emit:
            emit("кандидаты", {"позиций": len(cands), "товар": list(dict.fromkeys(c.name for c in cands))[:8]})
        found = extract(self.jev, text, build_form(cands), emit) if cands else []
        result = narrow_result(cands, steps, found, [], families or [])
        if emit:
            for step in result.steps:
                emit("сужение", {"шаг": step.шаг, "осталось": step.осталось})
        used = {k: self.jev.stats[k] - before[k] for k in before}
        return Outcome(result, rows, families or [], warnings, search_done, time.perf_counter() - started, used)
