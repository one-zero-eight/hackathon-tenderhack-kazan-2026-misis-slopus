import gzip
import importlib.util
import json
from pathlib import Path
import sys

import numpy as np

EXPERIMENTS = Path(__file__).resolve().parents[1] / 'experiments'
spec = importlib.util.spec_from_file_location('ste_to_jev', EXPERIMENTS / 'ste_to_jev.py')
ste = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ste)
sys.modules['ste_to_jev'] = ste
spec = importlib.util.spec_from_file_location('category_hypotheses', EXPERIMENTS / 'category_hypotheses_eval.py')
eval_ = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eval_)


def test_corpus_excludes_all_queries_and_identical_names(tmp_path):
    cards = [{'id': i, 'name': name, 'description': description, 'production': {'code': '01.01'}}
             for i, name, description in [(1, 'Диван', 'тест'), (2, 'Диван', 'другое'),
                                         (3, 'Диван другой', 'корпус'), (4, 'Диван другой', 'корпус')]]
    path = tmp_path / 'cards.gz'
    with gzip.open(path, 'wt', encoding='utf-8') as f:
        for c in cards:
            f.write(json.dumps(c, ensure_ascii=False) + '\n')
    queries = [{'id': 1, 'name': 'Диван', 'text': 'Диван\nтест'}]
    corpus, stats = eval_.collect_corpus(eval_.iter_cards(path), {'01.01'}, queries, per_category=10)
    assert len(corpus) == 1
    assert corpus[0]['id'] in {3, 4}
    assert stats['excluded_test_id'] == 1
    assert stats['excluded_test_duplicate'] == 1
    assert stats['duplicates_removed'] == 1


def test_neighbor_vote_and_unique_nearest_categories():
    corpus = [{'id': i, 'code': code} for i, code in enumerate(['A', 'B', 'B', 'C'])]
    scores = np.array([.9, .89, .88, .5])
    ranked, neighbors = eval_.neighbor_ranking(scores, corpus, 4, False)
    assert ranked == ['A', 'B', 'C']
    assert neighbors[0]['id'] == 0
    ranked, _ = eval_.neighbor_ranking(scores, corpus, 4, True)
    assert ranked == ['B', 'A', 'C']


def test_tree_preserves_internal_schema_terminals_and_ranks():
    codes = ['01.01', '01.01.01', '01.02']
    vectors = np.array([[1., 0.], [.98, .2], [0., 1.]])
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    tree = eval_.CategoryTree(codes, vectors)
    assert '01.01#' in tree.children['01.01']
    assert '01.01.01' in tree.children['01.01']
    assert tree.rank(np.array([0., 1.]), beam=1) == ['01.02']
    assert set(tree.rank(np.array([1., 0.]), beam=5)) == set(codes)
    assert tree.group_rank(np.array([.9, .8, .3]), depth=2, keep=1) == ['01.01', '01.01.01']


def test_family_holdout_and_sample(tmp_path):
    cards = [{'id': 1, 'name': 'Прожектор PAR 64 18x12', 'production': {'code': '01.01'}},
             {'id': 2, 'name': 'Прожектор PAR 6 12W', 'production': {'code': '01.01'}},
             {'id': 3, 'name': 'Прожектор уличный', 'production': {'code': '01.01'}},
             {'id': 4, 'name': 'Без схемы', 'production': {'code': '09'}}]
    path = tmp_path / 'cards.gz'
    with gzip.open(path, 'wt', encoding='utf-8') as f:
        for c in cards:
            f.write(json.dumps(c, ensure_ascii=False) + '\n')
    assert eval_.family('Прожектор PAR 64 18x12') == eval_.family('Прожектор PAR 6 12W') == 'прожектор'
    queries, counts = eval_.sample_queries(path, {'01.01'}, 1, 0, {2})
    assert len(queries) == 1 and queries[0]['id'] in {1, 3}
    assert counts['01.01'] == 3 and '09' not in counts
    q = [{'id': 1, 'name': cards[0]['name'], 'text': cards[0]['name']}]
    corpus, stats = eval_.collect_corpus(eval_.iter_cards(path), {'01.01'}, q, families=True)
    assert [c['id'] for c in corpus] == [3]
    assert stats['excluded_test_family'] == 1
