#!/usr/bin/env python3
"""Generate auditable STE -> KTRU candidates, not certified product matches.

Run with: uv run --with pyarrow --with pymorphy3 python scripts/match_ste_ktru.py
Research outputs default to data/tmp/ste-ktru-matching/. Only graph ancestor /
descendant relations are allowed; sibling branches and division-wide retrieval
are deliberately excluded. Text scores are heuristics, not probabilities.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from functools import lru_cache
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time

import pyarrow as pa
import pyarrow.parquet as pq
import pymorphy3

ROOT = Path(__file__).resolve().parents[1]
STOP_WORDS = set("и в на для из по с со к от до или не прочий прочее прочие другой услуга работа изделие продукция товар включенный группировка вид тип назначение использование комплект поставка выполнение оказание".split())
TOKEN = re.compile(r"[а-яёa-z]+", re.I)


def normalized(value):
    return " ".join(TOKEN.findall((value or "").casefold().replace("ё", "е")))


def digits(code):
    return len(code.replace(".", ""))


class Hierarchy:
    def __init__(self, records):
        by_id = {r['id']: r for r in records}
        self.names = {r['code']: r['name'] for r in records}
        self.parent = {r['code']: by_id[r['parent']]['code'] for r in records if r['parent'] in by_id}

    @lru_cache(maxsize=None)
    def ancestors(self, code):
        result, seen = [], {code}
        while code in self.parent:
            code = self.parent[code]
            if code in seen:
                raise ValueError(f'Cycle in OKPD2 hierarchy: {code}')
            seen.add(code)
            result.append(code)
        return tuple(result)


class CandidateIndex:
    def __init__(self, records, hierarchy, min_ancestor_digits=4):
        self.records = records
        self.hierarchy = hierarchy
        self.min_ancestor_digits = min_ancestor_digits
        self.exact = defaultdict(set)
        self.main = defaultdict(set)
        self.descendant = defaultdict(set)
        for i, r in enumerate(records):
            main = r.get('okpd2')
            codes = {main['code']} if main else set()
            if main:
                self.main[main['code']].add(i)
            codes.update(v['code'] for c in (r.get('nsi') or {}).get('classifiers') or []
                         if c['nsi_code'] == 'nsiOKPD2' for v in c.get('values') or [] if v.get('code'))
            for code in codes:
                self.exact[code].add(i)
                for parent in hierarchy.ancestors(code):
                    if digits(parent) >= min_ancestor_digits:
                        self.descendant[parent].add(i)

    @lru_cache(maxsize=None)
    def pool(self, code):
        if not code:
            return 'unmapped', (), None
        # Keep all exact candidates; never broaden an already covered class.
        if self.exact.get(code):
            return 'exact_okpd', tuple(sorted(self.exact[code])), code
        if self.descendant.get(code):
            return 'source_broader', tuple(sorted(self.descendant[code])), code
        for parent in self.hierarchy.ancestors(code):
            if digits(parent) < self.min_ancestor_digits:
                break
            if self.exact.get(parent):
                return 'ktru_broader', tuple(sorted(self.exact[parent])), parent
        return 'unmapped', (), None


class TextIndex:
    def __init__(self, records):
        self.morph = pymorphy3.MorphAnalyzer()
        self.records = records
        self.tokens = [self.words(r['name']) for r in records]
        self.names = [normalized(r['name']) for r in records]
        df = Counter(word for words in self.tokens for word in words)
        self.idf = {w: math.log((1 + len(records)) / (1 + n)) + 1 for w, n in df.items()}
        self.postings = defaultdict(set)
        for i, words in enumerate(self.tokens):
            for w in words:
                self.postings[w].add(i)

    @lru_cache(maxsize=100000)
    def lemma(self, word):
        if re.fullmatch('[а-я]+', word):
            return self.morph.parse(word)[0].normal_form
        return word

    @lru_cache(maxsize=100000)
    def words(self, text):
        return frozenset(w for token in TOKEN.findall((text or '').casefold().replace('ё', 'е'))
                         if len(token) > 2 and (w := self.lemma(token)) not in STOP_WORDS)

    def score(self, query, candidate):
        target = self.tokens[candidate]
        overlap = query & target
        if not overlap:
            return 0.0, 0.0
        weight = lambda words: sum(self.idf.get(w, 5.0) for w in words)
        shared = weight(overlap)
        precision = shared / weight(query)
        recall = shared / weight(target)
        return 2 * precision * recall / (precision + recall), recall

    def short_list(self, words, pool, limit=30):
        # Only classify within the pool selected by the source's own OKPD2.
        possible = set()
        for w in words:
            possible.update(self.postings.get(w, set()) & pool)
        ranked = [(self.score(words, i)[0], i) for i in possible]
        ranked.sort(key=lambda pair: (-pair[0], self.records[pair[1]]['code']))
        return [i for _, i in ranked[:limit]]


def load_json(path):
    with gzip.open(path, 'rt', encoding='utf-8') as stream:
        return json.load(stream)


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def load_details(path):
    """Read projected live data in one SELECT snapshot; never modify the DB."""
    if path is None:
        return {}, None
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    details = {}
    started = time.time()
    query = '''SELECT sku_id, json_extract(payload,'$._fetchError') IS NOT NULL,
        json_extract(payload,'$.production.okpd.code'),
        json_extract(payload,'$.productionDirectoryName'),
        json_extract(payload,'$.production.name'),
        json_extract(payload,'$.okeiName') FROM sku_details'''
    for sku_id, error, okpd, category, production, unit in db.execute(query):
        details[sku_id] = {'error': bool(error), 'okpd': okpd, 'category': category,
                           'production': production, 'unit': unit}
    db.close()
    return details, {'rows': len(details), 'fetch_errors': sum(r['error'] for r in details.values()),
                     'read_started_unix': started, 'note': 'Live SQLite SELECT snapshot, not the published commit; only projected classification fields are read.'}


def source_profile(ste, details, pp, categories):
    card = details.get(ste['id'])
    p = pp.get(ste.get('productionCode'))
    category = categories.get(ste.get('productionDirectoryId'))
    sources = []
    if card and not card['error'] and card['okpd']:
        sources.append(('card', card['okpd']))
    if ste.get('okpdCode'):
        sources.append(('list', ste['okpdCode']))
    if p and p.get('okpd'):
        sources.append(('pp_kpgz', p['okpd']['code']))
    if category and category.get('production') and category['production'].get('okpd'):
        sources.append(('category', category['production']['okpd']['code']))
    origin, code = sources[0] if sources else ('missing', None)
    # Card and list have precedence; do not select a stale alternative just
    # because it happens to retrieve more KTRU candidates.
    title = ((card or {}).get('category') or (category or {}).get('name') or
             (card or {}).get('production') or (p or {}).get('name') or '')
    return origin, code, title, len({c for _, c in sources}) > 1


def match(data, output, details_db, active_only=True):
    output.mkdir(parents=True, exist_ok=True)
    pp = {r['code']: r for r in load_json(data / 'pp-kpgz.json.gz')}
    categories = {r['id']: r for r in load_json(data / 'ste-categories.json.gz')}
    hierarchy = Hierarchy(load_json(data / 'okpd2-moy-zakupki.json.gz'))
    records = pq.read_table(data / 'ktru-actual.parquet', columns=['code', 'name', 'status', 'okpd2', 'nsi', 'units']).to_pylist()
    if active_only:
        records = [r for r in records if r['status'] == 'ACTIVE']
    index = CandidateIndex(records, hierarchy)
    text = TextIndex(records)
    print(f'INDEX: {len(records)} KTRU positions; {len(index.exact)} OKPD2 codes', flush=True)
    details, details_info = load_details(details_db)
    print(f'DETAILS: {len(details)} projected cards', flush=True)
    profiles = {}
    groups = []
    counts = Counter()
    tier_counts = Counter()
    candidate_sizes = Counter()
    review_examples = defaultdict(list)
    schema = pa.schema([
        ('ste_id', pa.int64()), ('group_id', pa.int32()), ('route', pa.string()),
        ('okpd_source', pa.string()), ('okpd_code', pa.string()), ('matched_okpd_code', pa.string()),
        ('classification_conflict', pa.bool_()), ('candidate_count', pa.int32()), ('evidence', pa.string()),
        ('top_candidates', pa.list_(pa.struct([
            ('code', pa.string()), ('name', pa.string()), ('text_score', pa.float32()),
            ('ktru_name_token_coverage', pa.float32()), ('unit_matches', pa.bool_()),
            ('same_category_name', pa.bool_()), ('requires_characteristic_check', pa.bool_()),
        ]))),
    ])
    target = output / 'ste-ktru-candidates.parquet'
    partial = target.with_suffix('.parquet.part')
    pending = []
    with pq.ParquetWriter(partial, schema, compression='zstd') as writer:
        with gzip.open(data / 'ste.jsonl.gz', 'rt', encoding='utf-8') as stream:
            for line in stream:
                ste = json.loads(line)
                origin, code, title, conflict = source_profile(ste, details, pp, categories)
                key = (origin, code, title)
                if key not in profiles:
                    route, pool, matched = index.pool(code)
                    pool_set = set(pool)
                    query = text.words(title)
                    shortlist = text.short_list(query, pool_set, limit=30)
                    group_id = len(groups)
                    profiles[key] = (group_id, route, pool_set, matched, shortlist, query)
                    groups.append({'group_id': group_id, 'okpd_source': origin, 'okpd_code': code,
                                   'category_name': title, 'route': route, 'matched_okpd_code': matched,
                                   'candidate_count': len(pool), 'ste_count': 0})
                group_id, route, pool_set, matched, shortlist, category_words = profiles[key]
                groups[group_id]['ste_count'] += 1
                product_words = text.words(ste['name'])
                candidates = set(shortlist)
                # Product words can rescue a more specific candidate missed by
                # a generic category name, still inside the same graph pool.
                candidates.update(text.short_list(product_words, pool_set, limit=15))
                ranked = []
                unit = normalized((details.get(ste['id']) or {}).get('unit') or ste.get('okeiName'))
                for i in candidates:
                    cs, cr = text.score(category_words, i)
                    ps, pr = text.score(product_words, i)
                    name_exact = bool(title) and normalized(title) == text.names[i]
                    score = max(ps, 0.85 * cs)
                    if name_exact:
                        score = max(score, 0.95)
                    units = records[i]['units'] or []
                    unit_matches = bool(unit) and any(normalized(u['name']) == unit for u in units)
                    ranked.append((score, max(cr, pr), name_exact, unit_matches, i))
                ranked.sort(key=lambda r: (-r[0], -r[3], records[r[4]]['code']))
                # No supported text evidence means classification-only; do not
                # emit arbitrary first IDs from a large set as selected matches.
                supported = [r for r in ranked if r[0] >= 0.30 and r[1] >= 0.50]
                if not pool_set:
                    tier = 'unmapped'
                elif supported and (supported[0][2] or (supported[0][0] >= 0.60 and supported[0][1] >= 0.70)):
                    tier = 'name_supported_candidates'
                elif supported:
                    tier = 'partial_text_candidates'
                else:
                    tier = 'classification_only'
                top = [{'code': records[i]['code'], 'name': records[i]['name'], 'text_score': round(s, 4),
                        'ktru_name_token_coverage': round(recall, 4), 'unit_matches': um,
                        'same_category_name': exact, 'requires_characteristic_check': True}
                       for s, recall, exact, um, i in supported[:5]]
                row = {'ste_id': ste['id'], 'group_id': group_id, 'route': route, 'okpd_source': origin,
                       'okpd_code': code, 'matched_okpd_code': matched, 'classification_conflict': conflict,
                       'candidate_count': len(pool_set), 'evidence': tier, 'top_candidates': top}
                pending.append(row)
                counts[route] += 1
                counts['rows'] += 1
                counts['conflicting_source_codes'] += int(conflict)
                tier_counts[tier] += 1
                candidate_sizes[len(pool_set)] += 1
                if len(review_examples[tier]) < 10:
                    review_examples[tier].append({'ste_name': ste['name'], 'category_name': title, **row})
                if len(pending) >= 2000:
                    writer.write_table(pa.Table.from_pylist(pending, schema=schema))
                    pending.clear()
                if counts['rows'] % 30000 == 0:
                    print(f"MATCHED {counts['rows']}; covered={counts['rows'] - counts['unmapped']}; groups={len(groups)}", flush=True)
        if pending:
            writer.write_table(pa.Table.from_pylist(pending, schema=schema))
    partial.replace(target)
    group_path = output / 'classification-groups.jsonl.gz'
    with gzip.open(group_path, 'wt', encoding='utf-8') as f:
        for group in groups:
            f.write(json.dumps(group, ensure_ascii=False) + '\n')
    total = counts['rows']
    nonzero = sum(n for size, n in candidate_sizes.items() if size)
    quantiles = {}
    acc = 0
    for size, n in sorted(candidate_sizes.items()):
        if not size:
            continue
        acc += n
        for q in (0.5, 0.9, 0.95):
            if str(q) not in quantiles and acc >= q * nonzero:
                quantiles[str(q)] = size
    summary = {'ste_rows': total, 'active_only': active_only, 'ktru_positions': len(records),
               'coverage_count': total - counts['unmapped'], 'coverage_percent': 100 * (total - counts['unmapped']) / total,
               'route_counts': {k: counts[k] for k in ('exact_okpd', 'source_broader', 'ktru_broader', 'unmapped')},
               'evidence_counts': dict(tier_counts), 'source_conflicts': counts['conflicting_source_codes'],
               'candidate_count_quantiles': quantiles, 'classification_groups': len(groups),
               'details_snapshot': details_info, 'source_sha256': {name: sha256(data / name) for name in
                   ('ste.jsonl.gz', 'pp-kpgz.json.gz', 'ste-categories.json.gz', 'okpd2-moy-zakupki.json.gz', 'ktru-actual.parquet')},
               'policy': {'okpd_source_priority': ['card', 'list', 'pp_kpgz', 'category'], 'min_ancestor_digits': 4,
                          'sibling_branches': False, 'exact_pool_not_broadened': True, 'top_candidate_limit': 5,
                          'text_thresholds': {'min_score': 0.30, 'min_name_token_coverage': 0.50,
                                              'strong_score': 0.60, 'strong_name_token_coverage': 0.70},
                          'score_is_probability': False, 'confirmed_matches': 0,
                          'characteristic_validation': 'Not performed; every emitted candidate requires characteristic checking.'},
               'review_examples': dict(review_examples)}
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    pq.ParquetFile(target).metadata.num_rows == total or (_ for _ in ()).throw(AssertionError('Output row count differs'))
    print(json.dumps({k: v for k, v in summary.items() if k not in ('review_examples', 'source_sha256')}, ensure_ascii=False, indent=2))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=ROOT / 'data')
    parser.add_argument('--out', type=Path, default=ROOT / 'data/tmp/ste-ktru-matching')
    parser.add_argument('--details-db', type=Path, help='Use a read-only snapshot of projected live card classification fields')
    parser.add_argument('--include-terminated', action='store_true')
    args = parser.parse_args()
    match(args.data, args.out, args.details_db, active_only=not args.include_terminated)


if __name__ == '__main__':
    main()
