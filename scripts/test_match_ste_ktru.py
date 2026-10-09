"""Offline graph and evidence tests for the STE/KTRU candidate generator."""
import unittest

from match_ste_ktru import CandidateIndex, Hierarchy, TextIndex, source_profile


def record(code, okpd, name='Компьютер персональный', extras=()):
    return {'code': code, 'name': name, 'okpd2': {'code': okpd}, 'units': [],
            'nsi': {'classifiers': [{'nsi_code': 'nsiOKPD2', 'code': 'OKPD2',
                                    'values': [{'code': c} for c in extras]}]}}


def hierarchy():
    return Hierarchy([
        {'id': 1, 'parent': None, 'code': '26', 'name': 'Division'},
        {'id': 2, 'parent': 1, 'code': '26.20', 'name': 'Computers'},
        {'id': 3, 'parent': 2, 'code': '26.20.1', 'name': 'Computer products'},
        {'id': 4, 'parent': 3, 'code': '26.20.11', 'name': 'Laptop'},
        {'id': 5, 'parent': 3, 'code': '26.20.12', 'name': 'Desktop'},
        {'id': 6, 'parent': 1, 'code': '26.30', 'name': 'Communications'},
    ])


class MatcherTests(unittest.TestCase):
    def test_extra_codes_and_exact_pool_not_expanded(self):
        index = CandidateIndex([record('A', '26.20.11', extras=['26.20.12']),
                                record('B', '26.20.1')], hierarchy())
        self.assertEqual(index.pool('26.20.12'), ('exact_okpd', (0,), '26.20.12'))
        self.assertNotIn('OKPD2', index.exact)

    def test_descendants_and_ancestors_without_siblings(self):
        index = CandidateIndex([record('A', '26.20.11')], hierarchy())
        self.assertEqual(index.pool('26.20')[0:2], ('source_broader', (0,)))
        self.assertEqual(index.pool('26.20.12')[0], 'unmapped')
        self.assertEqual(index.pool('26')[0], 'unmapped')
        self.assertEqual(index.pool('26.30')[0], 'unmapped')
        parent_index = CandidateIndex([record('B', '26.20.1')], hierarchy())
        self.assertEqual(parent_index.pool('26.20.12'), ('ktru_broader', (0,), '26.20.1'))

    def test_unknown_code_does_not_use_raw_prefix(self):
        index = CandidateIndex([record('A', '26.20.1')], hierarchy())
        self.assertEqual(index.pool('26.20.199999')[0], 'unmapped')

    def test_cycle_is_rejected(self):
        graph = Hierarchy([{'id': 1, 'parent': 2, 'code': '26.20', 'name': 'A'},
                           {'id': 2, 'parent': 1, 'code': '26.20.1', 'name': 'B'}])
        with self.assertRaises(ValueError):
            graph.ancestors('26.20')

    def test_card_precedence_and_conflict_preserved(self):
        ste = {'id': 1, 'okpdCode': '26.20.12', 'productionCode': '01', 'productionDirectoryId': 5}
        details = {1: {'error': False, 'okpd': '26.20.11', 'category': 'Ноутбуки'}}
        pp = {'01': {'name': 'Компьютеры', 'okpd': {'code': '26.20.1'}}}
        origin, code, title, conflict = source_profile(ste, details, pp, {})
        self.assertEqual((origin, code, title, conflict), ('card', '26.20.11', 'Ноутбуки', True))

    def test_error_card_not_used(self):
        ste = {'id': 1, 'okpdCode': '26.20.12'}
        origin, code, _, _ = source_profile(ste, {1: {'error': True, 'okpd': None}}, {}, {})
        self.assertEqual((origin, code), ('list', '26.20.12'))

    def test_shared_modifiers_do_not_change_product_type(self):
        text = TextIndex([record('A', '26.20.11', 'Игла для швейной машины')])
        self.assertFalse(text.supports_head(0, text.words('Нитевдеватель для швейной машины')))
        self.assertTrue(text.supports_head(0, text.words('Иглы для промышленных швейных машин')))

    def test_text_scoring_is_bounded_and_pool_restricted(self):
        text = TextIndex([record('A', '26.20.11', 'Компьютер персональный'),
                          record('B', '26.20.12', 'Ноутбук')])
        query = text.words('Компьютеры персональные')
        self.assertAlmostEqual(text.score(query, 0)[0], 1.0)
        self.assertEqual(text.short_list(query, {1}), [])
        self.assertEqual(text.score(text.words('Услуга'), 0), (0.0, 0.0))


if __name__ == '__main__':
    unittest.main()
