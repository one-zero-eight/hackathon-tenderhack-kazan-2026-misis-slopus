import unittest
from decimal import Decimal

from validate_ste_ktru import compare_value, evaluate, inside, number, ste_attributes


class ConstraintTests(unittest.TestCase):
    def test_open_closed_ranges(self):
        bounds = {'min': '10', 'min_operator': 'greater', 'max': '20', 'max_operator': 'lessOrEqual'}
        self.assertFalse(inside(Decimal('10'), bounds))
        self.assertTrue(inside(Decimal('20'), bounds))
        self.assertFalse(inside(Decimal('21'), bounds))

    def test_unit_mismatch_is_unknown(self):
        options = [{'unit': {'code': '112'}, 'concrete_values': ['10']}]
        self.assertEqual(compare_value(10, '112', options, str), 'compatible')
        self.assertEqual(compare_value(11, '112', options, str), 'conflict')
        self.assertEqual(compare_value(10, '111', options, str), 'unknown')

    def test_empty_alternative_prevents_false_conflict(self):
        options = [{'concrete_values': ['10']}, {}]
        self.assertEqual(compare_value(11, None, options, str), 'unknown')
        self.assertIsNone(inside(Decimal('5'), {}))

    def test_synonym_and_missing_are_not_conflicts(self):
        self.assertEqual(compare_value('пластик', None, [{'text': 'полимер'}], str), 'unknown')
        self.assertEqual(compare_value(5, None, [], str), 'unknown')
        self.assertIsNone(number('5 мм'))
        self.assertIsNone(number(True))
        self.assertIsNone(number('NaN'))

    def test_any_allowed_range_is_enough(self):
        options = [{'ranges': [{'min': '1', 'min_operator': 'greaterOrEqual', 'max': '10', 'max_operator': 'lessOrEqual'}]},
                   {'ranges': [{'min': '20', 'min_operator': 'greaterOrEqual', 'max': '30', 'max_operator': 'lessOrEqual'}]}]
        self.assertEqual(compare_value(25, None, options, str), 'compatible')
        self.assertEqual(compare_value(15, None, options, str), 'conflict')

    def test_missing_required_remains_unknown(self):
        chars = [{'name': 'Объем', 'required': True, 'actual': True, 'values': [{'concrete_values': ['10']}]}]
        self.assertEqual(evaluate(chars, {}, str, str)['constraint_status'], 'unknown')
        self.assertEqual(evaluate(chars, {'Объем': [(10, None)]}, str, str)['constraint_status'], 'required_constraints_supported')
        self.assertEqual(evaluate(chars, {'Объем': [(11, None)]}, str, str)['constraint_status'], 'contradiction')

    def test_zero_ui_default_is_not_evidence(self):
        card = {'skuCharacteristics': [{'productCharacteristicValue': {'productCharacteristicName': 'Объем'},
                                        'characteristicValueDecimalValue': 0.0}]}
        self.assertEqual(ste_attributes(card, str), {})


if __name__ == '__main__':
    unittest.main()
