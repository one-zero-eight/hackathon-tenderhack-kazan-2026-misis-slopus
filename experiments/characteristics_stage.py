from __future__ import annotations

import math
import pickle
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pymorphy3

from src.logging_ import logger
from src.services import upload_service
from src.tasks.upload.common import CHARACTERISTICS_FILE


# -----------------------------
# Data structures
# -----------------------------
@dataclass
class ImportanceScore:
    coverage: float
    entropy: float
    unique_ratio: float
    reliability: float
    score: float


@dataclass
class ClusterMeta:
    cluster_id: int
    canonical: str
    aliases: List[str]


@dataclass
class ScoredCharacteristic:
    name_norm: str  # нормализованное имя (для анализа и фильтрации)
    name_display: str  # отображаемое имя (для фронтенда)
    aliases: List[str]  # нормализованные алиасы
    present_count: int
    unique_values: int
    example_values: List[Dict[str, str]]  # [{norm: ..., display: ...}, ...]
    coverage: float
    entropy: float
    unique_ratio: float
    reliability: float
    score: float
    important: bool


@dataclass
class CategoryResult:
    category_id: Any
    category_name: str
    n_products: int
    min_present_for_important: int
    characteristics: List[ScoredCharacteristic]


@dataclass
class SKUInput:
    category_id: Any
    category_name: str
    characteristics: Dict[str, Any]


# === Column names are fixed and MUST be used directly ===
CATEGORY_ID_COL = "id категории"
CATEGORY_NAME_COL = "название категории"
CHARACTERISTICS_COL = "характеристики"

# -----------------------------
# Text utils (generic)
# -----------------------------
_WS_RE = re.compile(r"\s+")
_ENUM_RE = re.compile(r"\s*\d+\)\s*")  # "1) ... 2) ..." -> delimiter
_MORPH = pymorphy3.MorphAnalyzer()


@lru_cache(maxsize=100_000)
def _lemma(word: str) -> str:
    return _MORPH.parse(word)[0].normal_form


def _normalize_space(s: str) -> str:
    return _WS_RE.sub(" ", s).strip()


def _normalize_word_morph(word: str) -> str:
    """Normalize a single word using pymorphy3."""
    if not word or not word.isalpha():
        return word
    try:
        return _lemma(word)
    except Exception:
        return word


def normalize_key(s: str, with_morph: bool = True) -> str:
    s = str(s or "")
    s = s.replace("\n", " ").replace("\r", " ")
    s = _normalize_space(s).lower().replace("ё", "е")
    s = re.sub(r"[^\w\s/%\-.]+", " ", s, flags=re.UNICODE)
    s = _normalize_space(s)
    if with_morph:
        # Apply morphological normalization to words
        words = s.split()
        normalized_words = [_normalize_word_morph(w) for w in words]
        return " ".join(normalized_words)
    else:
        return s


def tokenize(s: str) -> List[str]:
    s = normalize_key(s, with_morph=True)
    if not s:
        return []
    # keep tokens like "205/70", "22.5", "r15", etc.
    toks = re.findall(r"[\w]+(?:[./-][\w]+)*|[%]", s, flags=re.UNICODE)
    return [t for t in toks if t and t != "_"]


def normalize_value(s: str, with_morph: bool = True) -> str:
    s = str(s or "")
    s = s.replace("\n", " ").replace("\r", " ")
    s = _normalize_space(s).lower().replace("ё", "е")

    def _norm_num(m: re.Match) -> str:
        raw = m.group(0).replace(",", ".")
        try:
            f = float(raw)
            if abs(f - round(f)) < 1e-9:
                return str(int(round(f)))
            return f"{f:.6f}".rstrip("0").rstrip(".")
        except Exception:
            return m.group(0)

    s = re.sub(r"(?<!\w)(\d+[.,]\d+|\d+)(?!\w)", _norm_num, s)
    s = _normalize_space(s)
    if with_morph:
        words = s.split()
        normalized_words = [_normalize_word_morph(w) for w in words]
        return " ".join(normalized_words)
    else:
        return s


# -----------------------------
# BM25 (Okapi)
# -----------------------------
@dataclass
class BM25Index:
    docs_tokens: List[List[str]]
    k1: float = 1.2
    b: float = 0.75

    def __post_init__(self) -> None:
        self.doc_lens = [len(d) for d in self.docs_tokens]
        self.avgdl = (sum(self.doc_lens) / len(self.doc_lens)) if self.doc_lens else 0.0
        self.df: Counter[str] = Counter()
        self.tfs: List[Counter[str]] = []
        for d in self.docs_tokens:
            tf = Counter(d)
            self.tfs.append(tf)
            self.df.update(tf.keys())
        self.N = len(self.docs_tokens)

    def idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        return math.log(1.0 + (self.N - df + 0.5) / (df + 0.5))

    def score(self, query_tokens: Sequence[str], doc_idx: int) -> float:
        if self.N == 0:
            return 0.0
        tf = self.tfs[doc_idx]
        dl = self.doc_lens[doc_idx] or 1
        denom_norm = (1 - self.b) + self.b * (dl / (self.avgdl or 1.0))
        s = 0.0
        for t in query_tokens:
            f = tf.get(t, 0)
            if f <= 0:
                continue
            idf = self.idf(t)
            num = f * (self.k1 + 1.0)
            den = f + self.k1 * denom_norm
            s += idf * (num / den)
        return s

    def top_k(
        self, query_tokens: Sequence[str], k: int = 6, exclude_idx: Optional[int] = None
    ) -> List[Tuple[int, float]]:
        scores: List[Tuple[int, float]] = []
        for i in range(self.N):
            if exclude_idx is not None and i == exclude_idx:
                continue
            sc = self.score(query_tokens, i)
            if sc > 0:
                scores.append((i, sc))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:k]


# -----------------------------
# Union-Find for clustering keys
# -----------------------------
class UnionFind:
    def __init__(self, n: int) -> None:
        self.p = list(range(n))
        self.r = [0] * n

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.r[ra] < self.r[rb]:
            self.p[ra] = rb
        elif self.r[ra] > self.r[rb]:
            self.p[rb] = ra
        else:
            self.p[rb] = ra
            self.r[ra] += 1


def jaccard(a: Sequence[str], b: Sequence[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# -----------------------------
# Importance scoring (handles partial coverage correctly)
# -----------------------------
def normalized_entropy(counter: Counter[str]) -> float:
    total = sum(counter.values())
    if total <= 0 or len(counter) <= 1:
        return 0.0
    ent = 0.0
    for c in counter.values():
        p = c / total
        if p > 0:
            ent -= p * math.log(p)
    return ent / math.log(len(counter))


def reliability_from_unique_ratio(unique_ratio: float) -> float:
    # "Too unique" values => likely unstructured/noisy, reduce reliability.
    if unique_ratio <= 0.7:
        return 1.0
    if unique_ratio >= 1.0:
        return 0.0
    return 1.0 - (unique_ratio - 0.7) / 0.3


def importance_score(n_products: int, present_count: int, value_counter: Counter[str]) -> ImportanceScore:
    if n_products <= 0 or present_count <= 0:
        return ImportanceScore(coverage=0.0, entropy=0.0, unique_ratio=0.0, reliability=0.0, score=0.0)

    # coverage is relative to ALL products in category:
    coverage = present_count / n_products

    # We cap "observations" by present_count when computing unique_ratio proxy:
    # use distinct values count over present_count to describe how "variable" it is across products.
    unique_values = len(value_counter)
    unique_ratio = unique_values / present_count if present_count else 0.0

    ent = normalized_entropy(value_counter)
    rel = reliability_from_unique_ratio(unique_ratio)

    # Main score: coverage is the primary signal; entropy differentiates within-present;
    # rel penalizes wildly-unique attributes.
    score = coverage * (0.65 + 0.35 * ent) * rel

    return ImportanceScore(
        coverage=coverage,
        entropy=ent,
        unique_ratio=unique_ratio,
        reliability=rel,
        score=score,
    )


def safe_scalar(x: Any) -> Any:
    # converts numpy/pandas scalars (int64/float64/etc.) to native python types
    if hasattr(x, "item") and callable(getattr(x, "item")):
        try:
            return x.item()
        except Exception:
            return x
    return x


# -----------------------------
# Main analysis
# -----------------------------
def analyze_skus(sku_dicts: List[SKUInput]) -> List[CategoryResult]:
    """Analyze parsed SKU dictionaries and return characteristics analysis results."""
    # Group SKUs by category
    from collections import defaultdict as dd

    category_groups: Dict[Tuple[Any, str], List[SKUInput]] = dd(list)

    for sku in sku_dicts:
        key = (sku.category_id, sku.category_name)
        category_groups[key].append(sku)

    results: List[CategoryResult] = []

    for (cat_id, cat_name), skus in category_groups.items():
        products_chars: List[List[Tuple[str, str]]] = []
        # Маппинг нормализованных ключей на оригинальные (для display)
        norm_key_to_display: Dict[str, str] = {}

        for sku in skus:
            # Characteristics are already parsed as dict
            if isinstance(sku.characteristics, dict):
                # Сохраняем оригинальные ключи и значения для display
                pairs = []
                for k, v in sku.characteristics.items():
                    if k and v:
                        original_key = str(k).strip()
                        norm_key = normalize_key(original_key, with_morph=True)
                        # Сохраняем маппинг для display (нормализованный без морфологии)
                        if norm_key and norm_key not in norm_key_to_display:
                            norm_key_to_display[norm_key] = normalize_key(original_key, with_morph=False)
                        # Сохраняем (norm_key, original_value) для последующего использования display
                        pairs.append((norm_key, str(v)))
            else:
                pairs = []
            products_chars.append(pairs)

        n_products = len(products_chars)

        # Count raw keys in this category (нормализованные)
        key_counts: Counter[str] = Counter()
        for pairs in products_chars:
            for k, _ in pairs:
                key_counts[k] += 1

        unique_raw_keys = list(key_counts.keys())
        key_tokens = [tokenize(k) for k in unique_raw_keys]

        # Cluster same-ish keys via BM25 retrieval + jaccard check
        uf = UnionFind(len(unique_raw_keys))
        bm25 = BM25Index(key_tokens)

        for i, toks in enumerate(key_tokens):
            if not toks:
                continue
            for j, bm25_sc in bm25.top_k(toks, k=6, exclude_idx=i):
                if bm25_sc <= 0:
                    continue
                if jaccard(toks, key_tokens[j]) >= 0.4:
                    uf.union(i, j)

        clusters: Dict[int, List[int]] = defaultdict(list)
        for i in range(len(unique_raw_keys)):
            clusters[uf.find(i)].append(i)

        # canonical + alias mapping
        rawkey_to_cluster: Dict[str, int] = {}
        cluster_meta: List[ClusterMeta] = []

        for root, idxs in clusters.items():
            members = [unique_raw_keys[i] for i in idxs]
            members.sort(key=lambda rk: (-key_counts[rk], len(normalize_key(rk, with_morph=True)), rk))
            canonical = members[0]

            for rk in members:
                rawkey_to_cluster[rk] = root

            cluster_meta.append(ClusterMeta(cluster_id=root, canonical=canonical, aliases=members))

        # Presence is counted per-product (handles "present only in part of products")
        cluster_present: Counter[int] = Counter()
        cluster_values: Dict[int, Counter[str]] = defaultdict(Counter)  # norm -> count
        cluster_value_display: Dict[int, Dict[str, str]] = defaultdict(dict)  # norm -> display

        for pairs in products_chars:
            seen: set[int] = set()
            for k, v in pairs:
                cid = rawkey_to_cluster.get(k)
                if cid is None:
                    continue
                if cid not in seen:
                    seen.add(cid)
                    cluster_present[cid] += 1
                nv = normalize_value(v, with_morph=True)
                if nv:
                    cluster_values[cid][nv] += 1
                    # Сохраняем display значение (нормализованное без морфологии, первое встреченное для каждого norm)
                    if nv not in cluster_value_display[cid]:
                        cluster_value_display[cid][nv] = normalize_value(str(v), with_morph=False)

        scored_chars: List[ScoredCharacteristic] = []

        for cm in cluster_meta:
            cid = cm.cluster_id
            present = int(cluster_present.get(cid, 0))
            vc = cluster_values.get(cid, Counter())
            value_display_map = cluster_value_display.get(cid, {})

            stats = importance_score(n_products, present, vc)

            # Формируем примеры значений с display и norm
            example_values = []
            for val_norm, _ in vc.most_common(5):
                example_values.append({"norm": val_norm, "display": value_display_map.get(val_norm, val_norm)})

            # Нормализуем алиасы (они должны быть norm, с морфологией)
            normalized_aliases = [normalize_key(alias, with_morph=True) for alias in cm.aliases]
            # Убираем дубликаты и канонический ключ из алиасов
            canonical_norm = normalize_key(cm.canonical, with_morph=True)
            normalized_aliases = [a for a in normalized_aliases if a != canonical_norm]
            # Убираем дубликаты, сохраняя порядок
            seen_aliases = set()
            unique_aliases = []
            for a in normalized_aliases:
                if a not in seen_aliases:
                    seen_aliases.add(a)
                    unique_aliases.append(a)

            # Получаем display значение для канонического ключа
            canonical_display = norm_key_to_display.get(canonical_norm, canonical_norm)

            scored_chars.append(
                ScoredCharacteristic(
                    name_norm=canonical_norm,
                    name_display=canonical_display,
                    aliases=unique_aliases,
                    present_count=present,
                    unique_values=len(vc),
                    example_values=example_values,
                    coverage=stats.coverage,
                    entropy=stats.entropy,
                    unique_ratio=stats.unique_ratio,
                    reliability=stats.reliability,
                    score=stats.score,
                    important=False,  # Will be set below
                )
            )

        scored_chars.sort(key=lambda x: x.score, reverse=True)

        # "important" decision: category-relative + minimum coverage gate
        scores = [x.score for x in scored_chars]
        if scores:
            s_sorted = sorted(scores)
            q_idx = int(math.floor(0.7 * (len(s_sorted) - 1)))
            q70 = s_sorted[q_idx]
        else:
            q70 = 0.0

        # Adaptive minimal coverage:
        # at least 2 products, and at least 5% of category (whichever is larger)
        min_present = max(2, int(math.ceil(0.05 * n_products))) if n_products > 0 else 0

        for x in scored_chars:
            x.important = bool((x.present_count >= min_present) and (x.score >= q70))

        results.append(
            CategoryResult(
                category_id=safe_scalar(cat_id),
                category_name=cat_name,
                n_products=n_products,
                min_present_for_important=min_present,
                characteristics=scored_chars,
            )
        )

    return results


def save_characteristics_pickle(results: List[CategoryResult], output_path: Path) -> None:
    """Save characteristics analysis results to pickle file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        pickle.dump(results, f)


def get_characteristics_cache() -> Optional[List[CategoryResult]]:
    """Load characteristics analysis results from pickle file or return None."""
    if not CHARACTERISTICS_FILE.exists():
        return None
    try:
        with open(CHARACTERISTICS_FILE, "rb") as f:
            cache = pickle.load(f)
        if isinstance(cache, list) and all(isinstance(item, CategoryResult) for item in cache):
            return cache
        else:
            logger.warning("Кеш характеристик имеет неверный формат")
    except Exception as e:
        logger.warning(f"Ошибка при чтении кеша характеристик: {e}")
    return None


# -----------------------------
# Stage function
# -----------------------------
async def analyze_characteristics_stage(db, job_id: int, sku_dicts: List[SKUInput]) -> None:
    """Этап статистического анализа характеристик."""
    from src.tasks.upload.common import PROGRESS_AFTER_CLUSTER, PROGRESS_COMPLETE
    
    await upload_service.update_upload_job(db, job_id, message="Статистический анализ характеристик...", progress=PROGRESS_AFTER_CLUSTER)

    if not sku_dicts:
        logger.warning("Пропуск анализа характеристик: нет данных")
        return

    try:
        results = analyze_skus(sku_dicts)
        save_characteristics_pickle(results, CHARACTERISTICS_FILE)
        logger.info(f"Сохранен анализ характеристик: {len(results)} категорий в {CHARACTERISTICS_FILE}")
        await upload_service.update_upload_job(
            db,
            job_id,
            message=f"Анализ характеристик завершен: {len(results)} категорий",
            progress=PROGRESS_COMPLETE,
        )
    except Exception as e:
        logger.warning(f"Ошибка при анализе характеристик: {e}", exc_info=True)
        # Don't fail the whole upload if characteristics analysis fails
        await upload_service.update_upload_job(
            db,
            job_id,
            message=f"Анализ характеристик пропущен: {str(e)}",
            progress=PROGRESS_COMPLETE,
        )
