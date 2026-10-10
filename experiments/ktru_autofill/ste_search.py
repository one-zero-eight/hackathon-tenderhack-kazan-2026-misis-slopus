"""Категория КПГЗ по похожим СТЕ: EmbeddingGemma и взвешенные голоса соседей."""
from __future__ import annotations

from collections import Counter, defaultdict
import gzip
import hashlib
import heapq
import html
import json
from pathlib import Path
import re

import numpy as np

from .kpgz_search import CACHE, KpgzSearch

STE_CACHE = CACHE.with_name('ste_embeddinggemma.npz')


def normalize(text):
    return re.sub(r'\W+', ' ', str(text).lower().replace('ё', 'е')).strip()


def text_of(card):
    description = html.unescape(re.sub(r'<[^>]+>', ' ', card.get('description') or ''))
    description = re.sub(r'\s+', ' ', description).strip()
    return f"{card.get('name') or card.get('modelName') or ''}\n{description}".strip()


def signature(text):
    return hashlib.sha256(normalize(text).encode()).hexdigest()


def collect_corpus(path, valid_codes, queries, per_category=20, limit=20000):
    """Отбор без тестовых ID/текстов/названий и без предпочтения эталонных категорий."""
    if min(per_category, limit) <= 0:
        raise ValueError('per_category и limit должны быть положительными')
    excluded_ids = {q['id'] for q in queries}
    excluded_texts = {signature(q['text']) for q in queries}
    excluded_names = {normalize(q['name']) for q in queries if q['name']}
    heaps, stats, counts = defaultdict(list), Counter(), Counter()
    with gzip.open(path, 'rt', encoding='utf-8') as f:
        for line in f:
            card = json.loads(line)
            stats['scanned'] += 1
            code = (card.get('production') or {}).get('code')
            if card.get('_fetchError') or code not in valid_codes:
                continue
            name = card.get('name') or card.get('modelName') or ''
            text = text_of(card)
            if not name or not text:
                continue
            if card['id'] in excluded_ids:
                stats['excluded_test_id'] += 1
                continue
            if signature(text) in excluded_texts or normalize(name) in excluded_names:
                stats['excluded_test_duplicate'] += 1
                continue
            counts[code] += 1
            rank = int(hashlib.sha256(str(card['id']).encode()).hexdigest(), 16)
            record = {'id': card['id'], 'code': code, 'name': name, 'text': text,
                      'skuCharacteristics': card.get('skuCharacteristics') or []}
            item = (-rank, card['id'], record)
            heap = heaps[code]
            if len(heap) < per_category:
                heapq.heappush(heap, item)
            elif item[:2] > heap[0][:2]:
                heapq.heapreplace(heap, item)
    groups = [sorted(heaps[code], reverse=True) for code in sorted(heaps)]
    pool = [group[i][2] for i in range(per_category) for group in groups if i < len(group)][:limit]
    unique, conflicts = {}, set()
    for card in pool:
        key = signature(card['text'])
        if key in unique and unique[key]['code'] != card['code']:
            conflicts.add(key)
        else:
            unique.setdefault(key, card)
    corpus = [card for key, card in unique.items() if key not in conflicts]
    stats.update({'eligible': sum(counts.values()), 'eligible_categories': len(counts),
                  'corpus': len(corpus), 'corpus_categories': len({c['code'] for c in corpus}),
                  'conflicting_texts_removed': len(conflicts), 'duplicates_removed': len(pool) - len(unique)})
    return corpus, dict(stats)


def neighbor_ranking(scores, corpus, k, vote=True, top_k=5):
    if min(k, top_k) <= 0:
        raise ValueError('k и top_k должны быть положительными')
    if not len(corpus):
        raise ValueError('Нет соседей СТЕ: выбор категории невозможен')
    ids = np.argsort(-scores, kind='stable')[:k]
    groups = defaultdict(list)
    for i in ids:
        groups[corpus[i]['code']].append(float(scores[i]))
    if vote:
        # Не вероятность правильности: температура фиксирована в эксперименте.
        ranked = sorted(groups, key=lambda c: (-sum(np.exp((s - float(scores[ids[0]])) / .05)
                                                    for s in groups[c]), -max(groups[c]), c))
    else:
        ranked = list(groups)
    neighbors = [corpus[i] | {'score': float(scores[i])} for i in ids]
    return ranked[:top_k], neighbors


def cached_embeddings(search, texts, cache, query=False):
    fingerprint = hashlib.sha256(json.dumps({'model': search.model_name, 'length': search.max_length,
        'texts': texts, 'query': query}, ensure_ascii=False).encode()).hexdigest()
    if cache.exists():
        with np.load(cache, allow_pickle=False) as data:
            if data['fingerprint'].item() == fingerprint:
                vectors = data['vectors']
                search._validate(vectors, len(texts))
                return vectors
    vectors = search._encode(texts, prompt='task: search result | query: ' if query else '', progress=True)
    cache.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(cache, fingerprint=fingerprint, vectors=vectors)
    return vectors


class SteSearch:
    """Общий корпус для всей партии запросов: все тестовые карточки исключаются сразу."""

    def __init__(self, schemas: KpgzSearch, path: Path, queries: list[dict], *,
                 cache: Path = STE_CACHE, per_category=20, limit=20000, neighbors=5):
        if neighbors <= 0:
            raise ValueError('neighbors должен быть положительным')
        self.schemas, self.neighbors = schemas, neighbors
        self.nodes = {n['code']: n for n in schemas.nodes}
        self.corpus, self.stats = collect_corpus(path, set(self.nodes), queries, per_category, limit)
        if not self.corpus:
            raise ValueError('Корпус соседей СТЕ пуст')
        documents = [f"title: {c['name']} | text: {c['text']}" for c in self.corpus]
        self.vectors = cached_embeddings(schemas, documents, cache)

    def search_many(self, texts, top_k=5):
        if top_k <= 0 or any(not text.strip() for text in texts):
            raise ValueError('Нужны непустые тексты и положительный top_k')
        if not texts:
            return []
        queries = self.schemas._encode(texts, prompt='task: search result | query: ')
        scores = queries @ self.vectors.T
        results = []
        for text, row in zip(texts, scores):
            codes, neighbors = neighbor_ranking(row, self.corpus, self.neighbors, top_k=top_k)
            maximum = neighbors[0]['score']
            candidates = [{'code': code, 'name': self.nodes[code]['name'],
                           'score': sum(float(np.exp((n['score'] - maximum) / .05))
                                        for n in neighbors if n['code'] == code)} for code in codes]
            results.append(self.nodes[codes[0]] | {'_retrieval': {
                'method': 'ste_neighbors_vote', 'model': self.schemas.model_name,
                'neighbors_k': self.neighbors, 'temperature': .05, 'top_k': candidates,
                'neighbors': neighbors, 'query_text': text, 'corpus': self.stats}})
        return results


def from_args(nodes, cards, args):
    from .kpgz_search import from_args as schema_search

    queries = [{'id': c['id'], 'name': c.get('name') or c.get('modelName') or '', 'text': text_of(c)} for c in cards]
    search = SteSearch(schema_search(nodes, args), args.ste, queries, cache=args.ste_embedding_index,
                       per_category=args.ste_per_category, limit=args.ste_corpus_limit, neighbors=args.ste_neighbors)
    return search.search_many([q['text'] for q in queries], args.category_top_k)
