"""Сравнение соседей СТЕ и иерархического поиска по КПГЗ на фиксированной выборке.

    .venv/bin/python experiments/category_hypotheses_eval.py --device cuda

    .venv/bin/python experiments/category_hypotheses_eval.py --device cuda --jev   # + выбор Jev среди соседей
    .venv/bin/python experiments/category_hypotheses_eval.py --sample 200 --seed 1 --family-holdout \
        --jev-dry-run --output reports/category_sample_n200_seed1_family/summary.json   # новая выборка

Без --sample запросы — 20 СТЕ прежнего прогона (--baseline). С --sample N — новые СТЕ по хешу (seed, id)
со схемой КПГЗ, кроме СТЕ прежнего прогона; метрики ещё и по частоте категории в каталоге.
--family-holdout убирает из корпуса карточки той же товарной семьи (название без цифр и латиницы).

Test IDs, тексты и совпадающие названия исключены из корпуса соседей. С --jev Jev выбирает среди
категорий соседей-СТЕ (neighbors_jev_K: голос K соседей, варианты — их категории с путём КПГЗ и
названиями похожих карточек); ответы кешируются в <output>/../jev/, повтор не тратит токены.
Параметры фиксированы до прогона; это диагностический замер, не подбор на holdout.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import logging
from pathlib import Path
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

import ste_to_jev as ste
from ktru_autofill import category_metrics as cm
from ktru_autofill.system_one import sdk_caller
from ktru_autofill.kpgz_search import (CACHE, MODEL, KpgzSearch, SteNeighbors, card_text, clean, collect_corpus, family,
                                       iter_cards, JEV_MODEL, jev_category_knockout, jev_category_noul,
                                       jev_category, neighbor_ranking)

log = logging.getLogger(__name__)
# Частота категории в каталоге (с запросом): редкие — в корпусе почти все их карточки.
FREQUENCY_BUCKETS = [(20, 'до 20 карточек'), (200, '21–200'), (float('inf'), 'больше 200')]
# Jev: (соседей в голосовании, максимум категорий-вариантов). Кандидаты — только категории соседей.
JEV_SETTINGS = [(5, 5), (25, 10)]
# P/R/F1 без модели выбора; пороги отказа — по доле голосов лучшей категории.
PRF_METHODS = ['neighbors_nearest_1', 'neighbors_vote_5', 'neighbors_vote_25']
SHARE_THRESHOLDS = [0, .5, .6, .7, .8, .9, .95]
# Как спрашивать модель: один Choice; Noul на кандидата; турнир групп по 5 (см. kpgz_search).
STRATEGIES = {'choice': jev_category, 'noul': jev_category_noul, 'knockout': jev_category_knockout}


class CategoryTree:
    """Coarse-to-fine: центроиды схем потомков и beam по условным softmax на каждом разветвлении."""

    def __init__(self, codes, vectors):
        self.codes = codes
        self.children, self.descendants, self.terminals = defaultdict(set), defaultdict(list), {}
        for i, code in enumerate(codes):
            parts = code.split('.')
            parent = ''
            for depth in range(1, len(parts) + 1):
                child = '.'.join(parts[:depth])
                self.children[parent].add(child)
                self.descendants[child].append(i)
                parent = child
            # Категория со схемой может иметь дочерние категории: отдельный терминал.
            terminal = code + '#'
            self.children[code].add(terminal)
            self.descendants[terminal] = [i]
            self.terminals[terminal] = i
        self.centroids = {}
        for node, indices in self.descendants.items():
            vec = vectors[indices].mean(axis=0)
            length = np.linalg.norm(vec)
            if length == 0:
                raise ValueError('Нулевой центроид категории')
            self.centroids[node] = vec / length

    def rank(self, query, beam=5, temperature=.05):
        frontier = [('', 0.)]
        terminals = {}
        max_depth = max(len(c.split('.')) for c in self.codes) + 1
        for _ in range(max_depth):
            expanded = []
            for node, score in frontier:
                if node in self.terminals:
                    terminals[self.terminals[node]] = score
                    continue
                children = sorted(self.children[node])
                sims = np.array([query @ self.centroids[c] for c in children]) / temperature
                log_probs = sims - (sims.max() + np.log(np.exp(sims - sims.max()).sum()))
                expanded.extend((c, score + float(p)) for c, p in zip(children, log_probs))
            frontier = sorted(expanded, key=lambda x: (-x[1], x[0]))[:beam]
            if not frontier:
                break
        for node, score in frontier:
            if node in self.terminals:
                terminals[self.terminals[node]] = score
        return [self.codes[i] for i in sorted(terminals, key=lambda i: (-terminals[i], self.codes[i]))[:5]]

    def group_rank(self, leaf_scores, depth=3, top_n=50, keep=2):
        """Bottom-up: похожие схемы голосуют за группу, затем ближайшие схемы внутри лучших групп."""
        groups = defaultdict(list)
        for i in np.argsort(-leaf_scores, kind='stable')[:top_n]:
            group = '.'.join(self.codes[i].split('.')[:depth])
            groups[group].append(float(leaf_scores[i]))
        maximum = max(leaf_scores)
        winners = sorted(groups, key=lambda g: (-sum(np.exp((s - maximum) / .05) for s in groups[g]), g))[:keep]
        return [self.codes[i] for i in np.argsort(-leaf_scores, kind='stable')
                if '.'.join(self.codes[i].split('.')[:depth]) in winners][:5]


def sample_queries(path, valid_codes, n, seed, skip_ids):
    """N карточек с наименьшим хешем (seed, id) и частота их категорий среди всех карточек."""
    picked, counts = [], Counter()
    for c in iter_cards(path):
        code = (c.get('production') or {}).get('code')
        name = c.get('name') or c.get('modelName')
        if c.get('_fetchError') or code not in valid_codes or not name:
            continue
        counts[code] += 1
        if c['id'] in skip_ids:
            continue
        key = hashlib.sha256(f'{seed}:{c["id"]}'.encode()).hexdigest()
        picked.append((key, {'id': c['id'], 'code': code, 'name': name, 'description': clean(c.get('description')),
                             'text': card_text(c)}))
        if len(picked) > 4 * n:
            picked = sorted(picked, key=lambda x: x[0])[:n]
    return [q for _, q in sorted(picked, key=lambda x: x[0])[:n]], counts


def frequency_bucket(count):
    return next(name for limit, name in FREQUENCY_BUCKETS if count <= limit)


def vote_share(neighbors, category, temperature=.05):
    """Доля веса голосования у категории — та же exp-взвешенная сумма, что в neighbor_ranking."""
    top = neighbors[0]['score']
    weights = [np.exp((n['score'] - top) / temperature) for n in neighbors]
    return float(sum(w for w, n in zip(weights, neighbors) if n['code'] == category) / sum(weights))


def prf_report(rows):
    """P/R/F1 для способов без модели (см. ktru_autofill.category_metrics)."""
    gold = [r['code'] for r in rows]
    out = {}
    for method in PRF_METHODS:
        pred = [r['rankings'][method][0] for r in rows]
        conf = [r['confidence'][method] for r in rows]
        thresholds = SHARE_THRESHOLDS if 'vote' in method else sorted({round(float(np.quantile(conf, q)), 3)
                                                                         for q in (0, .1, .2, .3, .5)})
        out[method] = {'top1': cm.classification(gold, pred),
                       'abstention': cm.with_abstention(gold, pred, conf, thresholds),
                       'candidates': cm.candidate_sets(gold, [r['rankings'][method] for r in rows], (1, 3, 5))}
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--baseline', type=Path, default=ste.ROOT / 'reports/ste_jev_search_dry/eval_all_n20_seed0.json')
    ap.add_argument('--ste', type=Path, default=ste.DEFAULT_STE)
    ap.add_argument('--kpgz', type=Path, default=ste.DEFAULT_KPGZ)
    ap.add_argument('--device', default='cuda')
    ap.add_argument('--per-category', type=int, default=20)
    ap.add_argument('--corpus-limit', type=int, default=20000)
    ap.add_argument('--batch-size', type=int, default=16)
    ap.add_argument('--jev', action='store_true', help='звать Jev для neighbors_jev_* (typesafe_sdk)')
    ap.add_argument('--jev-base-url', help='совместимый System One сервер, например experiments/jev9b_server.py')
    ap.add_argument('--jev-model', default=JEV_MODEL, help='имя модели в запросе (ключ кеша)')
    ap.add_argument('--jev-strategies', nargs='+', choices=list(STRATEGIES), default=['choice'],
                    help='способы вопроса; метод — neighbors_jev_K или neighbors_jev_K_<способ>')
    ap.add_argument('--jev-name', default='jev', help='подкаталог кеша ответов рядом с --output')
    ap.add_argument('--jev-dry-run', action='store_true',
                    help='только записать запросы Jev в jev/ (ответы дозаполняет experiments/jev_fill.py)')
    ap.add_argument('--env', type=Path, default=ste.DEFAULT_ENV)
    ap.add_argument('--workers', type=int, default=8)
    ap.add_argument('--sample', type=int, help='новая выборка N СТЕ вместо прежних 20')
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--limit', type=int, help='спросить модели только первые N запросов выборки; корпус — как для всей')
    ap.add_argument('--family-holdout', action='store_true', help='убрать из корпуса товарные семьи запросов')
    ap.add_argument('--output', type=Path, default=ste.ROOT / 'reports/category_hypotheses/summary.json')
    args = ap.parse_args()
    if min(args.per_category, args.corpus_limit, args.batch_size, args.workers) <= 0:
        ap.error('числовые параметры должны быть положительными')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', datefmt='%H:%M:%S')
    start = time.perf_counter()
    baseline = json.loads(args.baseline.read_text(encoding='utf-8'))
    queries = []
    for item in [] if args.sample else baseline['category_search']:
        record = json.loads((args.baseline.parent / f"{item['ste']}.json").read_text(encoding='utf-8'))
        state = record['request']['state']
        queries.append({'id': item['ste'], 'code': item['expected_code'], 'name': state['товар'],
                        'description': state['описание'], 'baseline': item['top_k'],
                        'text': f"{state['товар']}\n{state['описание']}".strip()})
    search = KpgzSearch(ste.iter_array(args.kpgz), device=args.device, cache=CACHE, batch_size=args.batch_size)
    codes = [n['code'] for n in search.nodes]
    counts = None
    if args.sample:
        queries, counts = sample_queries(args.ste, set(codes), args.sample, args.seed,
                                         {x['ste'] for x in baseline['category_search']})
        log.info('Выборка: %d СТЕ, %d категорий', len(queries), len({q['code'] for q in queries}))
    corpus, stats = collect_corpus(iter_cards(args.ste), set(codes), queries, args.per_category, args.corpus_limit,
                                   args.family_holdout)
    log.info('Корпус соседей: %s', stats)
    if args.limit:
        queries = queries[:args.limit]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    (args.output.parent / 'corpus_stats.json').write_text(json.dumps(stats, ensure_ascii=False, indent=2))
    tag = 'hypotheses' if not args.sample else f'{args.output.parent.name}'
    query_tag = f'{tag}_limit{args.limit}' if args.limit else tag
    query_vectors = search.cached_encode([q['text'] for q in queries], CACHE.parent / f'{query_tag}_queries.npz', query=True)
    neighbors = SteNeighbors(search, corpus, cache=CACHE.parent / f'{tag}_ste.npz')
    vectors = neighbors.vectors
    tree = CategoryTree(codes, search.vectors)
    category_scores = query_vectors @ search.vectors.T
    neighbor_scores = query_vectors @ vectors.T
    corpus_codes = {c['code'] for c in corpus}
    rows = []
    for q, vec, cat_scores, nbr_scores in zip(queries, query_vectors, category_scores, neighbor_scores):
        methods, details, confidence = {}, {}, {}
        for k, vote in [(1, False), (25, False), (5, True), (25, True)]:
            name = f"neighbors_{'vote' if vote else 'nearest'}_{k}"
            ranked, details[name] = neighbor_ranking(nbr_scores, corpus, k, vote)
            methods[name] = ranked[:5]
            confidence[name] = vote_share(details[name], ranked[0]) if vote else details[name][0]['score']
        for beam in (1, 5, 10):
            methods[f'hierarchy_beam_{beam}'] = tree.rank(vec, beam)
        methods['hierarchy_group_vote'] = tree.group_rank(cat_scores)
        rows.append(q | {'covered_by_corpus': q['code'] in corpus_codes, 'rankings': methods, 'confidence': confidence,
                         'neighbors': {k: [{a: b for a, b in c.items() if a != 'text'} for c in v[:5]]
                                       for k, v in details.items()}})
    jev_runs = {}
    if args.jev or args.jev_dry_run:
        call = None
        if not args.jev_dry_run:
            call = sdk_caller(base_url=args.jev_base_url,
                              api_key='local' if args.jev_base_url else ste.api_key(args.env))
        jev_dir = args.output.parent / args.jev_name
        jev_dir.mkdir(parents=True, exist_ok=True)
        for k, top in JEV_SETTINGS:
            neighbors.k = k
            candidates = neighbors.rank(query_vectors, top)
            for strategy in args.jev_strategies:
                suffix = '' if strategy == 'choice' else f'_{strategy}'

                def choose(item, k=k, suffix=suffix, ask=STRATEGIES[strategy]):
                    q, cands = item
                    path = jev_dir / f"{q['id']}_k{k}{suffix}.json"
                    cached = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
                    result = ask(search, {'товар': q['name'], 'описание': q['description']}, cands,
                                 call, cached, model=args.jev_model)
                    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
                    return result

                with ThreadPoolExecutor(max_workers=args.workers) as pool:
                    jev_runs[f'neighbors_jev_{k}{suffix}'] = list(pool.map(choose, zip(queries, candidates)))
        neighbors.k = 5
        pending = sum(r['response'] is None for run in jev_runs.values() for r in run)
        if pending:
            log.warning('Jev: %d запросов без ответа в %s — дозаполните experiments/jev_fill.py и повторите',
                        pending, jev_dir)
            jev_runs = {}
        for i, r in enumerate(rows if jev_runs else []):
            for method, run in jev_runs.items():
                result = run[i]
                r['rankings'][method] = [c['code'] for c in result['top_k']]
                r.setdefault('jev', {})[method] = {
                    'confidence': result['confidence'], 'usage': result['response'].get('usage'),
                    'top_k': [{a: b for a, b in c.items() if a != 'neighbors'} for c in result['top_k']]}
    metrics = {}
    for method in rows[0]['rankings']:
        correct = [r for r in rows if r['rankings'][method] and r['rankings'][method][0] == r['code']]
        top5 = sum(r['code'] in r['rankings'][method][:5] for r in rows)
        correct_ids = {r['id'] for r in correct}
        vote_ok = {r['id'] for r in rows if r['rankings']['neighbors_vote_5'][0] == r['code']}
        metrics[method] = {'n': len(rows), 'top1': len(correct), 'top5': top5,
                           'fixed_vs_vote_5': sorted(correct_ids - vote_ok),
                           'broken_vs_vote_5': sorted(vote_ok - correct_ids)}
        if method.startswith('neighbors_jev'):
            metrics[method] |= {
                                'candidates_contain_expected': top5}
    by_frequency = {}
    if counts is not None:
        for r in rows:
            r['category_cards'] = counts[r['code']]
        for _, bucket in FREQUENCY_BUCKETS:
            part = [r for r in rows if frequency_bucket(r['category_cards']) == bucket]
            by_frequency[bucket] = {'n': len(part)} | {
                m: sum(r['rankings'][m][:1] == [r['code']] for r in part)
                for m in ['neighbors_vote_5', 'neighbors_vote_25'] + [m for m in rows[0]['rankings']
                                                                         if m.startswith('neighbors_jev')]}
    names = {n['code']: n['name'] for n in search.nodes}
    errors = [{'ste': r['id'], 'product': r['name'], 'expected': names[r['code']],
               'predicted': names[r['rankings']['neighbors_vote_5'][0]],
               'neighbors': r['neighbors']['neighbors_vote_5']}
              for r in rows if r['rankings']['neighbors_vote_5'][0] != r['code']]
    jev_errors = {method: [
        {'ste': r['id'], 'product': r['name'], 'expected': names[r['code']],
         'predicted': names[r['rankings'][method][0]], 'candidates': r['jev'][method]['top_k']}
        for r in rows if r['rankings'][method][0] != r['code']] for method in jev_runs}
    report = {'errors_neighbors_vote_5': errors, 'errors_jev': jev_errors,
              'model': MODEL, 'elapsed_seconds': round(time.perf_counter() - start, 1),
              'config': {'per_category': args.per_category, 'corpus_limit': args.corpus_limit,
                         'temperature': .05, 'max_length': search.max_length, 'sample': args.sample,
                         'seed': args.seed if args.sample else None, 'family_holdout': args.family_holdout, 'limit': args.limit,
                         'jev': [{'k': k, 'candidates': top} for k, top in JEV_SETTINGS] if jev_runs else None,
                         'jev_model': args.jev_model if jev_runs else None,
                         'jev_strategies': args.jev_strategies if jev_runs else None, 'jev_base_url': args.jev_base_url},
              'corpus': stats, 'test_categories_covered': sum(r['covered_by_corpus'] for r in rows),
              'metrics': metrics, 'metrics_by_frequency': by_frequency, 'prf': prf_report(rows), 'rows': rows}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'rows'}, ensure_ascii=False, indent=2))
    log.info('Отчёт: %s', args.output)


if __name__ == '__main__':
    main()
