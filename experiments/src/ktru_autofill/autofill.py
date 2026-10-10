"""Автозаполнение характеристик и сужение кода КТРУ по тексту позиции.

1. Числа: «80 грамм», «1200х600 мм», «15,6"» вынимаются из текста.
2. Слова → леммы без стоп-слов → исправление опечаток по словарю каталога.
3. Название: по словам запроса выбираются семейства КТРУ («бумага для принтера» →
   «Бумага для офисной техники белая»); их характеристики — анкета товара.
4. Числа назначаются характеристикам анкеты по единице, слову рядом
   («высота 760 мм») и допустимым диапазонам; значения приводятся к единице
   характеристики («80 грамм» → 80 г/м², «1,2 м» → 1200 мм).
5. N-граммы 1..3 сравниваются со значениями и синонимами значений анкеты:
   точно по леммам, rapidfuzz, эмбеддинги. Один кусок текста — одна характеристика.
6. Найденные значения по порядку в тексте сужают коды КТРУ; значение, которое
   не допускает ни одна оставшаяся позиция, не применяется. Если код ещё не
   один — предлагается следующий вопрос.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field, replace
from typing import Any

import numpy as np
from rapidfuzz import fuzz, process

from . import embedder as emb
from .catalog import Catalog, next_question
from .numbers import NumberMention, extract_numbers, number_value, unit_match
from .schema import Characteristic, KtruCandidate, build_form
from .text import Fragment, Token, content_tokens, lemmatize, ngrams, normalize_code, tokenize

# Слова в названиях характеристик, которые ничего не говорят о том, к чему относится число.
_GENERIC_NAME_WORDS = {"количество", "тип", "наличие", "максимальный", "номинальный", "минимальный"}
_LENGTH_ORDER = ("Длина", "Ширина", "Глубина", "Высота")
_WEAK_METHODS = {"вектор", "число_по_диапазону", "нечёткое", "подсказка"}
_NO_POSITION = "нет позиции КТРУ с таким значением среди оставшихся"
_SUGGESTION = "подсказка"


@dataclass
class Match:
    характеристика: str
    значение: Any
    фрагмент: str
    метод: str  # точное | синоним | нечёткое | вектор | число+единица | число_по_названию | число_по_диапазону | размеры
    оценка: float
    предупреждение: str | None = None


@dataclass
class Typo:
    как_написано: str
    исправление: str
    похожесть: float


@dataclass
class Step:
    шаг: str
    осталось: int


@dataclass
class Result:
    values: dict[str, Any]
    matches: list[Match] = field(default_factory=list)
    typos: list[Typo] = field(default_factory=list)
    candidates: list[KtruCandidate] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    families: list[tuple[str, float]] = field(default_factory=list)
    question: dict[str, Any] | None = None
    suggestions: list[Match] = field(default_factory=list)  # не заполнены: нужно подтверждение

    @property
    def code(self) -> str | None:
        return self.candidates[0].code if len(self.candidates) == 1 else None

    def to_dict(self, details: bool = False) -> dict[str, Any]:
        names = list(dict.fromkeys(c.name for c in self.candidates))
        out: dict[str, Any] = {
            "товар": names[0] if len(names) == 1 else names,
            "характеристики": self.values,
            "ктру": {
                "код": self.code,
                "осталось": len(self.candidates),
                "сужение": [asdict(s) for s in self.steps],
                "следующий_вопрос": self.question,
            },
        }
        if self.suggestions:
            # Похоже, но не наверняка: в характеристики не пишем, спрашиваем пользователя.
            out["подсказки"] = [{"характеристика": m.характеристика, "значение": m.значение,
                                 "фрагмент": m.фрагмент, "оценка": m.оценка} for m in self.suggestions]
        if details:
            out["ктру"]["кандидаты"] = [c.code for c in self.candidates[:20]]
            out["совпадения"] = [asdict(m) for m in self.matches]
            out["опечатки"] = [asdict(t) for t in self.typos]
        return out


@dataclass
class _Value:
    char: Characteristic
    text: str  # значение как в КТРУ
    lemma: str  # леммы значения или его синонима
    exact_only: bool  # коды («A4», «M») сравниваем только точно
    synonym: bool


@dataclass
class _Candidate:
    char: Characteristic
    value: Any
    fragment: str
    method: str
    score: float
    span: frozenset[int]
    pos: int  # позиция в тексте — порядок сужения
    n_tokens: int = 1
    warning: str | None = None


_AUTO = object()


class Autofiller:
    def __init__(
        self,
        catalog: Catalog | None = None,
        embedder: emb.Embedder | None | object = _AUTO,
        fuzzy_threshold: float = 0.80,
        typo_threshold: float = 0.80,
        emb_threshold: float = 1.01,
        suggest_threshold: float = 0.88,
        emb_contrast: float = 0.06,
        emb_margin: float = 0.02,
    ):
        self.catalog = catalog
        self.embedder = emb.load_default() if embedder is _AUTO else embedder
        self.fuzzy_threshold = fuzzy_threshold
        self.typo_threshold = typo_threshold
        # Вектор ≥ emb_threshold — значение заполняется; от suggest_threshold до emb_threshold —
        # только подсказка. На синтетике ниже 0.94 эмбеддинги путали «кожзам» с «натуральной
        # кожей», на СТЕ организаторов совпадения по вектору верны лишь в 41 % — поэтому по
        # умолчанию (1.01) вектор значения не заполняет никогда, только подсказывает.
        self.emb_threshold = emb_threshold
        self.suggest_threshold = suggest_threshold
        self.emb_contrast = emb_contrast
        self.emb_margin = emb_margin

    # --- публичный API -------------------------------------------------

    def fill(self, text: str, candidates: list[KtruCandidate] | list[dict] | None = None) -> Result:
        """`candidates` — кандидаты шага 2; без них — весь каталог."""
        catalog = self._catalog_for(candidates) if candidates is not None else self._default_catalog()

        mentions, consumed = self._numbers(text, catalog)
        tokens = [t for t in tokenize(text) if not any(a <= t.start < b for a, b in consumed)]
        tokens, typos = self._fix_typos(content_tokens(tokens), catalog)

        steps = [Step("весь каталог" if candidates is None else "кандидаты шага 2", len(catalog.positions))]
        if catalog.retriever is not None:
            found_name = catalog.retriever.retrieve(text)
            cands = [catalog.positions[i] for i in found_name.candidates]
            families = found_name.titles
            steps += [Step(desc, n) for desc, n in found_name.steps]
        else:
            cands, families = catalog.match_name({t.lemma for t in tokens if not t.has_digit})
            if families:
                steps.append(Step(f"товар: {', '.join(n for n, _ in families)}", len(cands)))
        form = build_form(cands)

        found = self._number_candidates(text, mentions, form, cands)
        found += self._enum_candidates(ngrams(tokens), form)
        chosen = self._assign(found)

        # Сужение — в порядке упоминания, как если бы пользователь вводил по одному.
        matches: list[Match] = []
        # Сужают только надёжные совпадения, в порядке упоминания. Позицию отбрасывает
        # противоречие (характеристика есть, значение не подходит); если характеристики
        # у позиции нет — это не повод: в настоящем КТРУ наборы характеристик разные даже
        # у позиций с одним названием. Вместо этого в конце остаются позиции, у которых
        # совпало больше всего значений. Слабые совпадения (вектор, нечёткое, диапазон) не сужают.
        matches: list[Match] = []
        strong = []
        for c in sorted(chosen, key=lambda c: c.pos):
            warning = c.warning
            if c.method not in _WEAK_METHODS:
                rest = [p for p in cands if not _conflicts(p, c.char.name, c.value)]
                # Сужаем, только если кто-то значение явно принимает: иначе выжили бы
                # одни позиции без этой характеристики («Стул эргономичный» без анкеты).
                if any(p.accepts(c.char.name, c.value) for p in rest):
                    if len(rest) < len(cands):
                        steps.append(Step(f"{c.char.name} = {_show(c.value)}", len(rest)))
                    cands = rest
                    strong.append(c)
                else:
                    warning = _NO_POSITION
            matches.append(Match(c.char.name, c.value, c.fragment, c.method, round(c.score, 3), warning))
        if strong and len(cands) > 1:
            hits = [sum(p.accepts(c.char.name, c.value) for c in strong) for p in cands]
            best = max(hits)
            rest = [p for p, h in zip(cands, hits) if h == best]
            if len(rest) < len(cands):
                steps.append(Step(f"больше всего совпавших характеристик ({best} из {len(strong)})", len(rest)))
                cands = rest

        # Анкета товара — характеристики оставшихся позиций; найденное для чужих отбрасываем.
        final_form = build_form(cands)
        values: dict[str, Any] = {ch.name: None for ch in final_form}
        for m in matches:
            if m.характеристика in values and m.предупреждение != _NO_POSITION and (
                    m.метод not in _WEAK_METHODS or any(p.accepts(m.характеристика, m.значение) for p in cands)):
                values[m.характеристика] = m.значение
        matches = [m for m in matches if m.характеристика in values]
        suggestions = [m for m in matches if m.метод == _SUGGESTION and values.get(m.характеристика) is not None]
        for m in suggestions:
            values[m.характеристика] = None
        matches = [m for m in matches if m.метод != _SUGGESTION]
        question = next_question(cands, {k for k, v in values.items() if v is not None})
        return Result(values, matches, typos, cands, steps, families, question, suggestions)

    def _catalog_for(self, candidates: list) -> Catalog:
        """Каталог из кандидатов шага 2; для тех же объектов-кандидатов — из кеша
        (словарь опечаток и единиц строится один раз на анкету)."""
        key = tuple(id(c) for c in candidates)
        cache = self.__dict__.setdefault("_catalogs", {})
        if key not in cache:
            if len(cache) > 2048:
                cache.clear()
            cache[key] = (candidates, Catalog.of(candidates))  # держим ссылку, чтобы id не переиспользовался
        return cache[key][1]

    def _default_catalog(self) -> Catalog:
        if self.catalog is None:
            self.catalog = Catalog.load()
        return self.catalog

    # --- шаг 1: числа ---------------------------------------------------

    def _numbers(self, text: str, catalog: Catalog) -> tuple[list[NumberMention], list[tuple[int, int]]]:
        """Числа и куски текста, которые они занимают (единицу съедаем, только если это единица)."""
        mentions, consumed = [], []
        for m in extract_numbers(text):
            if m.unit_raw and not m.dims and not catalog.is_unit(m.unit_raw):
                if m.compound:
                    continue  # «одноразовый» — не число
                m = replace(m, unit_raw=None, end=m.num_end)  # «500 белая»: «белая» — не единица
            mentions.append(m)
            if not m.dims:  # размеры съедаем, только если назначим их длинам
                consumed.append((m.start, m.end))
        return mentions, consumed

    def _number_candidates(
        self, text: str, mentions: list[NumberMention], form: list[Characteristic], cands: list[KtruCandidate]
    ) -> list[_Candidate]:
        numeric = [ch for ch in form if ch.type in ("number", "count")]
        fits = lambda ch, v: any(p.accepts(ch.name, v) for p in cands)  # noqa: E731
        found: list[_Candidate] = []
        unitless: list[NumberMention] = []

        bounds = {}  # окно подсказки — между соседними числами
        for i, m in enumerate(mentions):
            lo = mentions[i - 1].end if i else 0
            hi = mentions[i + 1].start if i + 1 < len(mentions) else len(text)
            bounds[id(m)] = (lo, hi)

        for m in mentions:
            frag = text[m.start : m.end]
            if m.dims:
                found += self._dims(m, frag, numeric)
                continue
            if not m.unit_raw:
                if not _is_range_part(text, m):  # «ростовая группа 5-6» — не одно значение
                    unitless.append(m)
                continue
            hints = self._hint(text, m, numeric, *bounds[id(m)])
            scored = []
            for ch in numeric:
                score, factor = unit_match(m.unit_raw, ch)
                if score:
                    value = m.value * factor
                    scored.append((ch, score, value, fits(ch, value)))
            # Без подсказки из подходящих по диапазону берём самый узкий: «стол 754 мм» —
            # скорее высота (700–780), чем ширина (600–2000).
            narrowest = None
            if not hints:
                fitting = [(ch.range[1] - ch.range[0], ch.name) for ch, _, _, ok in scored if ok and ch.range]
                narrowest = min(fitting)[1] if len(fitting) > 1 else None
            for ch, score, value, ok in scored:
                score += 0.05 * (ch.name in hints) + 0.02 * ok + 0.03 * (ch.name == narrowest)
                warn = None if ok else "вне допустимых значений КТРУ"
                found.append(_Candidate(ch, number_value(ch, value, frag), frag, "число+единица",
                                        score * (1.0 if ok else 0.9), frozenset({m.start}), m.start, 2, warn))

        # Без единицы: слово рядом («плотность 80», «6 ростовая группа»), иначе — диапазон,
        # если подходит ровно одна характеристика, не занятая числом с единицей.
        taken = {c.char.name for c in found}
        for m in unitless:
            frag = text[m.start : m.num_end]
            names = self._hint(text, m, numeric, *bounds[id(m)])
            hints = [ch for ch in numeric if ch.name in names]
            for ch in hints:
                ok = fits(ch, m.value)
                found.append(_Candidate(ch, number_value(ch, m.value, None), frag, "число_по_названию",
                                        0.9 if ok else 0.6, frozenset({m.start}), m.start,
                                        warning=None if ok else "вне допустимых значений КТРУ"))
            if hints:
                continue
            fitting = [ch for ch in numeric if ch.name not in taken and ch.range and fits(ch, m.value)]
            if len(fitting) == 1:
                ch = fitting[0]
                found.append(_Candidate(ch, number_value(ch, m.value, None), frag, "число_по_диапазону", 0.5,
                                        frozenset({m.start}), m.start))
        return found

    def _dims(self, m: NumberMention, frag: str, numeric: list[Characteristic]) -> list[_Candidate]:
        """«1200х600х750 мм» → длины анкеты по порядку Длина/Ширина, Глубина, Высота."""
        lengths = sorted(
            (ch for ch in numeric if ch.unit in ("мм", "см", "м") and ch.name.split()[0] in _LENGTH_ORDER),
            key=lambda ch: _LENGTH_ORDER.index(ch.name.split()[0]),
        )
        if len(lengths) < len(m.dims):
            return []
        out = []
        for i, (ch, v) in enumerate(zip(lengths, m.dims)):
            factor = unit_match(m.unit_raw, ch)[1] if m.unit_raw else 1.0
            # Порядок Д×Ш×В в реальных СТЕ разный (на данных организаторов «по порядку» верно
            # лишь в 38 %), поэтому размеры без названий — подсказка, а не значение.
            out.append(_Candidate(ch, number_value(ch, v * (factor or 1.0), frag), frag, _SUGGESTION, 0.9,
                                  frozenset({m.start + i}), m.start))
        return out

    @staticmethod
    def _hint(text: str, m: NumberMention, numeric: list[Characteristic], lo: int, hi: int) -> set[str]:
        """Характеристики, названные рядом с числом: два слова до, иначе два слова после
        (не дальше соседних чисел). Слова сверяются с названием и единицами, с опечатками."""
        before = [t for t in tokenize(text[lo : m.start]) if t.surface.isalpha()][-2:]
        after = [t for t in tokenize(text[m.end : hi]) if t.surface.isalpha()][:2]
        for window in (before, after):
            hit = set()
            for ch in numeric:
                words = set(lemmatize(ch.name).split()) - _GENERIC_NAME_WORDS
                words |= {lemmatize(u) for u in ch.unit_aliases if len(u) >= 4}
                if any(t.lemma in words or (len(t.surface) >= 5 and any(
                        len(w) >= 5 and fuzz.ratio(t.surface, w) >= 80 for w in words)) for t in window):
                    hit.add(ch.name)
            if hit:
                return hit
        return set()

    # --- шаг 2: опечатки -----------------------------------------------

    def _fix_typos(self, tokens: list[Token], catalog: Catalog) -> tuple[list[Token], list[Typo]]:
        """Незнакомое pymorphy3 слово сверяем со словарём каталога: «бумвга» → «бумага»."""
        vocab = catalog.vocab
        keys = list(vocab)
        out, typos = [], []
        for t in tokens:
            if t.known or not t.surface.isalpha() or len(t.surface) < 4 or t.surface in vocab:
                out.append(t)
                continue
            best = process.extractOne(t.surface, keys, scorer=fuzz.ratio)
            if best and best[1] / 100 >= self.typo_threshold:
                typos.append(Typo(t.surface, best[0], round(best[1] / 100, 2)))
                t = replace(t, lemma=vocab[best[0]], known=True)
            out.append(t)
        return out, typos

    # --- шаг 3: фрагменты ↔ значения анкеты ------------------------------

    def _enum_candidates(self, frags: list[Fragment], form: list[Characteristic]) -> list[_Candidate]:
        values: list[_Value] = []
        for ch in form:
            if ch.type != "enum":
                continue
            for v in ch.values:
                for phrase, syn in [(v, False)] + [(s, True) for s in ch.synonyms.get(v, [])]:
                    lemma = lemmatize(phrase) or normalize_code(phrase)
                    exact_only = any(c.isdigit() for c in lemma) or len(lemma) < 3
                    values.append(_Value(ch, v, lemma, exact_only, syn))
        if not frags or not values:
            return []

        # Пары-кандидаты отбираются векторно: точное совпадение лемм, rapidfuzz-матрица
        # и косинусы. В реальных анкетах сотни значений на характеристику — поэлементный
        # перебор пар «фрагмент × значение × соседи» был квадратичным.
        f_lemmas = [f.lemma for f in frags]
        v_lemmas = [v.lemma for v in values]
        fz = process.cdist(f_lemmas, v_lemmas, scorer=fuzz.ratio, score_cutoff=100 * self.fuzzy_threshold,
                           dtype=np.uint8, workers=1)
        sims = self._similarity(frags, values)
        rival = self._rivals(sims, values) if sims is not None else None
        by_lemma: dict[str, list[int]] = defaultdict(list)
        for j, v in enumerate(values):
            by_lemma[v.lemma].append(j)

        found = []
        for i, f in enumerate(frags):
            pairs = set(by_lemma.get(f.lemma, ()))
            pairs.update(np.nonzero(fz[i])[0].tolist())
            if sims is not None:
                pairs.update(np.nonzero(sims[i] >= self.suggest_threshold)[0].tolist())
            bg = float(sims[i].mean()) if sims is not None else 0.0
            for j in pairs:
                v = values[j]
                score, method = self._score(f, v, j, sims[i] if sims is not None else None, bg,
                                            rival[i, j] if rival is not None else None, fz[i, j] / 100)
                if score > 0:
                    found.append(_Candidate(v.char, v.text, f.surface, method, score, f.span,
                                            f.tokens[0].start, len(f.tokens)))
        return found

    @staticmethod
    def _rivals(sims: np.ndarray, values: list[_Value]) -> np.ndarray:
        """Для каждой пары — лучший косинус среди других значений той же характеристики."""
        out = np.full(sims.shape, -1.0, dtype=np.float32)
        groups: dict[int, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
        for j, v in enumerate(values):
            groups[id(v.char)][v.text].append(j)
        for texts in groups.values():
            if len(texts) < 2:
                continue
            idx = list(texts.values())
            best = np.stack([sims[:, cols].max(1) for cols in idx], axis=1)  # фрагменты × тексты
            order = np.argsort(-best, axis=1)
            top1 = np.take_along_axis(best, order[:, :1], 1)[:, 0]
            top2 = np.take_along_axis(best, order[:, 1:2], 1)[:, 0]
            for t, cols in enumerate(idx):
                r = np.where(order[:, 0] == t, top2, top1)
                out[:, cols] = r[:, None]
        return out

    def _similarity(self, frags: list[Fragment], values: list[_Value]) -> np.ndarray | None:
        if self.embedder is None:
            return None
        a = self.embedder.encode([f.lemma for f in frags])
        b = self.embedder.encode([v.lemma for v in values])
        return a @ b.T

    def _score(
        self, f: Fragment, v: _Value, j: int, row: np.ndarray | None, bg: float, rival: float | None, fz: float
    ) -> tuple[float, str]:
        if f.lemma == v.lemma:
            return 1.0, "синоним" if v.synonym else "точное"
        if v.exact_only or f.has_digit:
            return 0.0, ""

        best, method = 0.0, ""
        # Нечёткое — только от опечаток: у каждого слова значения должна быть пара во фрагменте,
        # иначе «картофель продовольственный» превращается в «…ранний», которого в тексте нет.
        if fz >= self.fuzzy_threshold:
            frag_words = f.lemma.split()
            if all(any(fuzz.ratio(w, x) >= 80 for x in frag_words) for w in v.lemma.split()):
                best, method = fz, "нечёткое"

        if row is not None:
            s = float(row[j])
            # Короткие фразы у моделей «слипаются» (0.8–0.9 почти для всего),
            # поэтому кроме порога требуем, чтобы значение выделялось на фоне
            # остальных значений анкеты и обгоняло другие значения своей характеристики.
            margin = s - rival if rival is not None and rival > -1 else 1.0
            if s >= self.suggest_threshold and s - bg >= self.emb_contrast and margin >= self.emb_margin and s > best:
                best, method = s, "вектор" if s >= self.emb_threshold else _SUGGESTION
        return best, method

    # --- шаг 4: распределение -------------------------------------------

    @staticmethod
    def _assign(found: list[_Candidate]) -> list[_Candidate]:
        """Жадно: лучшие совпадения первыми; характеристика и кусок текста — по разу."""
        chosen, used_chars, used_span = [], set(), set()
        # При равной оценке — характеристика, которую в категории заполняют чаще:
        # «красный» скорее «Цвет», чем «Цвет корпуса».
        for c in sorted(found, key=lambda c: (c.score, c.n_tokens, c.char.frequency), reverse=True):
            if c.char.name in used_chars or c.span & used_span:
                continue
            chosen.append(c)
            used_chars.add(c.char.name)
            used_span |= c.span
        return chosen


def _conflicts(p: KtruCandidate, char_name: str, value: Any) -> bool:
    ch = p.char(char_name)
    return ch is not None and not ch.accepts(value)


def _is_range_part(text: str, m: NumberMention) -> bool:
    """«5-6»: число — край диапазона."""
    after = text[m.num_end : m.num_end + 4].lstrip()
    before = text[max(0, m.start - 3) : m.start].rstrip()
    return (after[:1] in ("-", "–") and after[1:].lstrip()[:1].isdigit()) or (
        before[-1:] in ("-", "–") and before[-2:-1].isdigit()
    )


def _show(value: Any) -> str:
    if isinstance(value, dict):
        return f"{value['значение']:g} {value['единица'] or ''}".strip()
    return str(value)
