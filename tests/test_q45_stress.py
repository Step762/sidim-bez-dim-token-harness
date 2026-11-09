"""Independent oracles through the actual offline workflow, not solver stubs."""
import unittest
from fractions import Fraction

from diagnostics.q45_stress import CASES, normalized, run_probe


class QualificationStressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run_probe()
        cls.rows = {row['id']: row for row in cls.result['rows']}

    def test_independent_set_has_fixed_oracles_and_no_requests(self):
        self.assertEqual(len(CASES), 36)
        self.assertEqual(len({case['id'] for case in CASES}), 36)
        self.assertEqual(self.result['llm_requests'], 0)
        self.assertEqual(Fraction(self.result['spent']), 0)
        self.assertEqual(self.result['counts']['failed'], 0)
        for row in self.result['rows']:
            self.assertTrue(row['oracle'])
            self.assertTrue(row['property'])
            self.assertEqual(row['outcome']['llm_attempts'], 0)
            if row['actual'] is not None:
                self.assertEqual(row['answer_mode'], 0o644)

    def test_grader_compares_exact_rationals_not_float_or_spelling(self):
        self.assertEqual(normalized('7.75'), normalized('31/4'))
        self.assertEqual(normalized('{"b":1,"a":2}'), normalized('{"a":2,"b":1}'))
        self.assertNotEqual(normalized('7.75'), normalized('7.75000001'))


def scenario_test(key):
    def test(self):
        row = self.rows[key]
        if row['required'] or key in {'s_inverse_employer_boundary', 'm_scientific_boundary'}:
            self.assertIn(row['status'], ('correct', 'safe_refusal'), row)
        else:
            # Known unsupported boundary; recorded as a miss, never a guess.
            self.assertEqual(row['status'], 'missed', row)
            self.assertIsNone(row['actual'])
    return test


for item in CASES:
    setattr(QualificationStressTests, 'test_' + item['id'], scenario_test(item['id']))


if __name__ == '__main__':
    unittest.main()
