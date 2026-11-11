"""New supported classes stay local and preserve safe publication with LLM enabled."""
import unittest
from fractions import Fraction
from diagnostics.q5_probe import run_probe


class Q5WorkflowTests(unittest.TestCase):
    def check_probe(self,enabled):
        r=run_probe(enabled=enabled)
        self.assertEqual(r['counts'],{'solved':10,'skipped':13,'failed':0})
        self.assertEqual(r['2/2'],5)
        self.assertTrue(all(row['passed'] for row in r['rows']),r)
        self.assertTrue(all(row['mode']==0o644 for row in r['rows'] if row['actual'] is not None))
        self.assertEqual(r['requests'],0);self.assertEqual(Fraction(r['spent']),0)
        self.assertEqual(r['wrong'],0)

    def test_independent_five_pairs_and_thirteen_refusals_at_zero_budget(self):self.check_probe(False)
    def test_same_inputs_never_call_model_even_when_gateway_enabled(self):self.check_probe(True)


if __name__=='__main__':unittest.main()
