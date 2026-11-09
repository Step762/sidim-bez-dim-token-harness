"""Inverse relation evidence is scoped without forgetting conflicting records."""
from pathlib import Path
import tempfile
import unittest

from search_solver import solve_search, UnsupportedSearch


class InverseScopeTests(unittest.TestCase):
    def solve(self, documents, question='Who is mentored by Oren?'):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); kb = root/'workspace/kb'; kb.mkdir(parents=True)
            for i,text in enumerate(documents): (kb/f'{i}.md').write_text(text)
            return solve_search(question, root)

    def test_unrelated_nonrelation_conflicts_do_not_hide_the_unique_mentee(self):
        base = '# Zuri\nMentor: Oren\n# Oren\nAlias: North\nBorn: 1978\n'
        for noise in (['# Remote\nBorn: 1920\n', '# Remote\nBorn: 1930\n'],
                      ['# Remote\nEmployer: X\nEmployer: Y\n'],
                      ['# Remote\nHeadquarters: Kiel\nHeadquarters: Jena\n']):
            with self.subTest(noise=noise):
                self.assertEqual(self.solve([base,*noise]), 'Zuri')
                self.assertEqual(self.solve([*reversed(noise),base], 'Who is mentored by North?'), 'Zuri')

    def test_earlier_mentor_evidence_is_not_lost_when_later_record_omits_it(self):
        base = '# Zuri\nMentor: Oren\n# Oren\nBorn: 1978\n'
        variants = [('# Veda\nMentor: Oren\n', '# Veda\nBorn: 1981\n'),
                    ('# Veda\nMentor: Oren\n', '# Veda\nMentor: Ivo\n'),
                    ('# Veda\nMentor: unknown\n', '# Veda\nBorn: 1981\n')]
        for records in variants:
            for ordered in (records, records[::-1]):
                with self.subTest(records=ordered), self.assertRaises(UnsupportedSearch):
                    self.solve([base,*ordered])

    def test_conflicts_on_target_candidate_and_alias_are_still_rejected(self):
        base = '# Zuri\nMentor: Oren\n# Oren\nBorn: 1978\n'
        for noise in ('# Oren\nBorn: 1980\n', '# Zuri\nMentor: Oren\nBorn: 1981\n',
                      '# Other\nAlias: Oren\n', '# Veda\nMentor: Oren\n',
                      '# Veda\nMentor: unknown\n'):
            with self.subTest(noise=noise), self.assertRaises(UnsupportedSearch):
                self.solve([base,noise])

    def test_forward_search_keeps_strict_profile_ambiguity(self):
        with self.assertRaises(UnsupportedSearch):
            self.solve(['# Zuri\nBorn: 1971\n', '# Zuri\nEmployer: Reed\n'], 'When was Zuri born?')


if __name__ == '__main__': unittest.main()
