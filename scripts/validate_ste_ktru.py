#!/usr/bin/env python3
"""Check candidate characteristics conservatively; missing data is unknown.

uv run --with pyarrow --with pymorphy3 python scripts/validate_ste_ktru.py
Only identically normalized characteristic names and identical OKEI units are
compared. Numeric closed sets / ranges can establish a contradiction. Different
text values remain unknown because unrecognized synonyms are not disproof.
"""
import argparse
from collections import Counter
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import gzip
import json
from pathlib import Path
import re
import sqlite3

import pyarrow as pa
import pyarrow.parquet as pq

from match_ste_ktru import ROOT, TextIndex, sha256


def number(value):
    if isinstance(value, bool):
        return None
    if value is None:
        return None
    text = str(value).strip().replace(',', '.')
    # A value containing a unit or multiple numbers is not a scalar.
    try:
        parsed = Decimal(text)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def inside(value, bounds):
    if bounds.get('min') is None and bounds.get('max') is None:
        return None
    for field, operator_field in (('min', 'min_operator'), ('max', 'max_operator')):
        bound = number(bounds.get(field))
        if bound is None:
            if bounds.get(field) is not None:
                return None
            continue
        operator = bounds.get(operator_field)
        if operator not in ('greater', 'greaterOrEqual', 'less', 'lessOrEqual', 'equal'):
            return None
        if operator == 'equal' and value != bound:
            return False
        if operator == 'greater' and value <= bound:
            return False
        if operator == 'greaterOrEqual' and value < bound:
            return False
        if operator == 'less' and value >= bound:
            return False
        if operator == 'lessOrEqual' and value > bound:
            return False
    return True


def compare_value(actual, unit_code, options, text_key):
    numeric = number(actual)
    supported = False
    unknown = False
    for option in options:
        option_unit = (option.get('unit') or {}).get('code')
        if option_unit and unit_code != option_unit:
            unknown = True
            continue
        if numeric is not None:
            concrete = [number(v) for v in option.get('concrete_values') or []]
            concrete = [v for v in concrete if v is not None]
            literal = number(option.get('text'))
            if literal is not None:
                concrete.append(literal)
            if not concrete and not option.get('ranges'):
                unknown = True
            if concrete:
                supported = True
                if numeric in concrete:
                    return 'compatible'
            for bounds in option.get('ranges') or []:
                result = inside(numeric, bounds)
                if result is True:
                    return 'compatible'
                if result is False:
                    supported = True
                else:
                    unknown = True
        else:
            text_values = [option.get('text')] + (option.get('concrete_values') or [])
            if any(v is not None and text_key(str(actual)) == text_key(str(v)) for v in text_values):
                return 'compatible'
            unknown = True
    return 'conflict' if supported and not unknown else 'unknown'


def ste_attributes(payload, name_key):
    attrs = {}
    for item in payload.get('skuCharacteristics') or []:
        prop = item.get('productCharacteristicValue') or {}
        if prop.get('deleted') or not prop.get('productCharacteristicName'):
            continue
        # Numeric zero is often an unfilled UI default in these cards. It is
        # not reliable evidence unless positive; retain false bools as values.
        value = next((item.get(k) for k in ('characteristicValueStringValue', 'characteristicValueIntValue',
                      'characteristicValueDecimalValue', 'characteristicValueBoolValue') if item.get(k) is not None), None)
        if value is None:
            value = next((prop.get(k) for k in ('stringValue', 'intValue', 'decimalValue', 'boolValue')
                          if prop.get(k) is not None), None)
        if value is None or value == '' or (not isinstance(value, bool) and number(value) == 0):
            continue
        key = name_key(prop['productCharacteristicName'])
        if not key:
            continue
        attrs.setdefault(key, []).append((value, prop.get('productCharacteristicUnitCode')))
    return attrs


def evaluate(characteristics, attrs, name_key, text_key):
    counts = Counter()
    conflicts = []
    for characteristic in characteristics or []:
        if characteristic.get('actual') is False:
            continue
        required = characteristic.get('required') is True
        if required:
            counts['required'] += 1
        values = attrs.get(name_key(characteristic['name']), [])
        if not values:
            counts['unknown'] += 1
            continue
        results = [compare_value(value, unit, characteristic.get('values') or [], text_key) for value, unit in values]
        if 'compatible' in results:
            counts['checked'] += 1
            counts['required_checked'] += int(required)
        elif all(r == 'conflict' for r in results):
            counts['conflicts'] += 1
            conflicts.append(characteristic['name'])
        else:
            counts['unknown'] += 1
    if counts['conflicts']:
        status = 'contradiction'
    elif counts['checked'] and counts['required'] and counts['required_checked'] == counts['required']:
        status = 'required_constraints_supported'
    elif counts['checked']:
        status = 'partial_constraints_supported'
    else:
        status = 'unknown'
    return {'constraint_status': status, 'checked_characteristics': counts['checked'],
            'required_characteristics': counts['required'], 'required_checked': counts['required_checked'],
            'conflicting_characteristics': conflicts}


def validate(data, output, details_db):
    source = output / 'ste-ktru-candidates.parquet'
    with gzip.open(output / 'classification-groups.jsonl.gz', 'rt', encoding='utf-8') as stream:
        groups = {r['group_id']: r for r in map(json.loads, stream)}
    characteristics = {}
    names = {}
    for batch in pq.ParquetFile(data / 'ktru-actual.parquet').iter_batches(columns=['code', 'name', 'status', 'characteristics']):
        for row in batch.to_pylist():
            if row['status'] == 'ACTIVE':
                if row['code'] in characteristics:
                    raise ValueError(f"Duplicate active KTRU code: {row['code']}")
                characteristics[row['code']] = row['characteristics']
                names[row['code']] = row['name']
    text = TextIndex([])
    @lru_cache(maxsize=30000)
    def name_key(value):
        return tuple(sorted(text.lemma(v) for v in re.findall(r'[а-яёa-z]+|\d+', value.lower().replace('ё', 'е'))))
    @lru_cache(maxsize=100000)
    def text_key(value):
        # Preserve numeric tokens and units in text; morphology only for words.
        return tuple(text.lemma(v) for v in re.findall(r'[а-яёa-z]+|\d+(?:[.,]\d+)?', str(value).lower().replace('ё', 'е')))
    db = sqlite3.connect(details_db.resolve().as_uri() + '?mode=ro', uri=True)
    # A single read transaction gives a consistent view while the downloader
    # continues appending new cards in WAL mode.
    db.execute('BEGIN')
    extra_fields = [pa.field('constraint_status', pa.string()), pa.field('checked_characteristics', pa.int16()),
                    pa.field('required_characteristics', pa.int16()), pa.field('required_checked', pa.int16()),
                    pa.field('conflicting_characteristics', pa.list_(pa.string()))]
    original = pq.ParquetFile(source).schema_arrow
    top_type = original.field('top_candidates').type.value_type
    schema = pa.schema([f if f.name != 'top_candidates' else
                        pa.field('top_candidates', pa.list_(pa.struct(list(top_type) + extra_fields))) for f in original])
    target = output / 'ste-ktru-validated-candidates.parquet'
    counts = Counter()
    statuses = Counter()
    review = []
    with pq.ParquetWriter(target.with_suffix('.parquet.part'), schema, compression='zstd') as writer:
        for batch in pq.ParquetFile(source).iter_batches(batch_size=1000):
            rows = batch.to_pylist()
            ids = [r['ste_id'] for r in rows if r['top_candidates']]
            placeholders = ','.join('?' for _ in ids)
            cards = {i: json.loads(p) for i, p in db.execute(f'SELECT sku_id,payload FROM sku_details WHERE sku_id IN ({placeholders})', ids)} if ids else {}
            for row in rows:
                attrs = ste_attributes(cards.get(row['ste_id'], {}), name_key)
                # Title scores cannot distinguish same-name variants. Expand
                # their entire classification-compatible family before checking
                # constraints, instead of validating only the first five IDs.
                templates = {name_key(c['name']): c for c in row['top_candidates']}
                expanded = []
                for code in groups[row['group_id']]['candidate_codes']:
                    template = templates.get(name_key(names[code]))
                    if template is not None:
                        expanded.append({**template, 'code': code, 'name': names[code]})
                row['top_candidates'] = expanded
                usable = False
                supported = False
                required = False
                rejected = False
                for candidate in row['top_candidates']:
                    assessment = evaluate(characteristics.get(candidate['code']), attrs, name_key, text_key)
                    candidate.update(assessment)
                    statuses[assessment['constraint_status']] += 1
                    usable |= assessment['constraint_status'] != 'contradiction'
                    rejected |= assessment['constraint_status'] == 'contradiction'
                    supported |= assessment['checked_characteristics'] > 0 and assessment['constraint_status'] != 'contradiction'
                    required |= assessment['constraint_status'] == 'required_constraints_supported'
                    if assessment['constraint_status'] == 'contradiction' and len(review) < 12:
                        review.append({'ste_id': row['ste_id'], 'ste_name': cards.get(row['ste_id'], {}).get('name'),
                                       'ktru_code': candidate['code'], 'ktru_name': candidate['name'], **assessment})
                counts['rows'] += 1
                counts['rows_with_text_candidates'] += bool(row['top_candidates'])
                counts['rows_with_noncontradicting_candidates'] += usable
                counts['rows_with_supported_constraints'] += supported
                counts['rows_with_all_required_supported'] += required
                counts['rows_with_rejected_candidate'] += rejected
                counts['rows_all_candidates_rejected'] += bool(row['top_candidates']) and not usable
            writer.write_table(pa.Table.from_pylist(rows, schema=schema))
            if counts['rows'] % 30000 == 0:
                print(f"VALIDATED {counts['rows']}; contradictions={statuses['contradiction']}", flush=True)
    db.close()
    target.with_suffix('.parquet.part').replace(target)
    summary = {'counts': dict(counts), 'candidate_status_counts': dict(statuses), 'review_contradictions': review,
               'candidate_input_sha256': sha256(source), 'policy': {'missing_is_unknown': True,
               'different_unit_is_unknown': True, 'numeric_zero_is_unknown': True,
               'unrecognized_text_is_unknown': True, 'only_same_characteristic_name': True,
               'same_name_variants_expanded_before_validation': True,
               'certified_matches': 0, 'note': 'Support for supplied catalog constraints does not establish product identity or validate unprovided optional attributes.'}}
    (output / 'validation-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=ROOT / 'data')
    parser.add_argument('--out', type=Path, default=ROOT / 'data/tmp/ste-ktru-matching')
    parser.add_argument('--details-db', type=Path, default=ROOT / 'data/tmp/ste-details.sqlite')
    args = parser.parse_args()
    validate(args.data, args.out, args.details_db)
