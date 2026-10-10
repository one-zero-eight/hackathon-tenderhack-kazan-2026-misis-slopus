"""Устойчивость сравнения: попарные метрики без крупнейшей группы эквивалентов и B-cubed (вес карточки 1)."""
import sys
from collections import Counter, defaultdict
STE = sys.argv[1]
import numpy as np
import cluster_benchmark as cb
import ste_to_jev as ste
from ktru_autofill.kpgz_search import CACHE, KpgzSearch, iter_cards
from ktru_autofill.ste_clusters import normalize_name, pair_metrics, truncate, ward_labels

ids, cats, names, normalized, groups = [], [], [], [], {}
for c in iter_cards(ste.Path(STE)):
    code = (c.get("production") or {}).get("code"); name = c.get("name") or c.get("modelName")
    if c.get("_fetchError") or not code or not name:
        continue
    pos = len(ids); ids.append(c["id"]); cats.append(code); names.append(name)
    normalized.append(normalize_name(name) or name.lower())
    if c.get("equivalentGroupId"):
        groups[pos] = c["equivalentGroupId"]
index = defaultdict(list)
for i, c in enumerate(cats):
    index[c].append(i)
search = KpgzSearch(ste.iter_array(ste.DEFAULT_KPGZ), device="cuda", cache=CACHE, batch_size=64)
search.max_length = 64  # короткие названия; общий кеш индекса КПГЗ (512 токенов) не трогаем
def enc(texts, cache):
    u = sorted(set(texts)); v = search.cached_encode(u, cache, prompt=cb.PROMPT); p = {t: i for i, t in enumerate(u)}
    return v[[p[t] for t in texts]]
norm768 = truncate(enc(normalized, CACHE.parent / "ste_names_clustering.npz"), None)
raw768 = truncate(enc(names, CACHE.parent / "ste_raw_names_clustering.npz"), None)
norm256 = truncate(norm768, 256)
methods = {
    "ward_norm_256_t0.8 (текущий)": lambda c, idx: ward_labels(norm256[idx], .8),
    "avgcos_norm_768_d0.05": lambda c, idx: cb.average_cosine(norm768[idx], .05),
    "avgcos_raw_768_d0.03": lambda c, idx: cb.average_cosine(raw768[idx], .03),
    "avgcos_raw_768_d0.04": lambda c, idx: cb.average_cosine(raw768[idx], .04),
    "graph_norm_768_s0.97": lambda c, idx: cb.graph_components(norm768[idx], .97),
    "hdbscan_norm_768_min3": lambda c, idx: cb.hdbscan(norm768[idx], 3),
}
biggest = Counter(groups.values()).most_common(1)[0][0]
no_big = {k: v for k, v in groups.items() if v != biggest}

def bcubed(clusters, gold):
    of = {s: c.id for c in clusters for s in c.ste_ids}
    lab = [s for s in gold if s in of]
    by_c, by_g = defaultdict(set), defaultdict(set)
    for s in lab:
        by_c[of[s]].add(s); by_g[gold[s]].add(s)
    p = np.mean([len(by_c[of[s]] & by_g[gold[s]]) / len(by_c[of[s]]) for s in lab])
    r = np.mean([len(by_c[of[s]] & by_g[gold[s]]) / len(by_g[gold[s]]) for s in lab])
    return p, r, 2 * p * r / (p + r)

print(f"{'метод':30} {'пары P/R/F1 (все)':>20} {'без 67 СВЧ':>20} {'B-cubed P/R/F1':>20}")
for name, m in methods.items():
    cl = cb.run(m, index, None, None, None)
    a, b = pair_metrics(cl, groups), pair_metrics(cl, no_big)
    p, r, f = bcubed(cl, groups)
    fmt = lambda x: f"{x['precision']:.2f}/{x['recall']:.2f}/{x['f1']:.2f}"
    print(f"{name:30} {fmt(a):>20} {fmt(b):>20} {p:.2f}/{r:.2f}/{f:.2f}".rjust(0), f" кластеров {len(cl)}")
