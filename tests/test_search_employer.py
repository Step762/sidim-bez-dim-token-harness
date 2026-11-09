"""Unique inverse Employer via shared grammar and complete local KB evidence."""
from pathlib import Path
import tempfile
import unittest
from llm_fallback import prepare,InvalidPlan
from plan_semantics import search_spec
from search_solver import solve_search,UnsupportedSearch,_load_kb


class EmployerTests(unittest.TestCase):
    def solve(self, question, *documents):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);kb=root/'workspace/kb';kb.mkdir(parents=True)
            for i,text in enumerate(documents):(kb/f'changed_{i*11}.md').write_text(text)
            return solve_search(question,root)

    def test_unique_employee_alias_and_question_variants(self):
        docs=('# Eira Sol\nEmployer: Sunfin\n','# Tide Forge\nAlias: Sunfin\nHeadquarters: Bergen\n',
              '# Other\nEmployer: Cloud\n# Cloud\nFounded: 2003\n')
        for question in ('Who works for Sunfin?','Who works at Tide Forge?','Who is employed by Sunfin?',
                         'Who is an employee of Tide Forge?'):
            with self.subTest(question=question):self.assertEqual(self.solve(question,*docs),'Eira Sol')

    def test_explicit_literal_edge_without_separate_company_profile(self):
        self.assertEqual(self.solve('Who works for Moss Labs?','# Yun\nEmployer: Moss Labs\n'),'Yun')
        self.assertEqual(self.solve('Who works at 银杉?','# 李娜\nEmployer: 银杉\n'),'李娜')

    def test_no_match_multiple_candidates_and_unknown_are_refused(self):
        base='# Eira\nEmployer: Tide Forge\n# Tide Forge\nFounded: 2003\n'
        for noise in ('# Yun\nEmployer: Tide Forge\n','# Yun\nEmployer: unknown\n',
                      '# Yun\nEmployer: Tide Forge or Cloud\n','# Yun\nEmployer: maybe Tide Forge\n',
                      '# Cloud\nAlias: Tide Forge\n'):
            with self.subTest(noise=noise),self.assertRaises(UnsupportedSearch):
                self.solve('Who works for Tide Forge?',base,noise)
        with self.assertRaises(UnsupportedSearch):self.solve('Who works for Missing?',base)

    def test_relevant_overwritten_edges_and_aliases_cannot_be_hidden(self):
        base='# Eira\nEmployer: Tide Forge\n# Tide Forge\nFounded: 2003\n'
        for other in ('# Yun\nEmployer: Cloud\n','# Yun\nBorn: 1970\n'):
            records=('# Yun\nEmployer: Tide Forge\n',other)
            for ordered in (records,records[::-1]):
                with self.subTest(records=ordered),self.assertRaises(UnsupportedSearch):
                    self.solve('Who works for Tide Forge?',base,*ordered)

    def test_irrelevant_conflicts_and_exact_duplicate_profiles(self):
        base='# Eira\nEmployer: Tide Forge\n# Tide Forge\nFounded: 2003\n'
        self.assertEqual(self.solve('Who works for Tide Forge?',base,'# Noise\nBorn: 1910\nBorn: 1911\n'),'Eira')
        self.assertEqual(self.solve('Who works for Tide Forge?',base,base),'Eira')
        with self.assertRaises(UnsupportedSearch):
            self.solve('Who works for Tide Forge?',base,'# Eira\nEmployer: Tide Forge\nBorn: 1980\n')

    def test_shared_spec_is_local_only_not_a_forward_model_guess(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);kb=root/'workspace/kb';kb.mkdir(parents=True)
            (kb/'unrelated.md').write_text('# Yun\nEmployer: Moss Labs\n# Moss Labs\nFounded: 2003\n')
            index,_=_load_kb(root)
            self.assertEqual(search_spec('Who works for Moss Labs?',index),('moss labs',[],'inverse_employer'))
            with self.assertRaises(InvalidPlan):prepare('search','Who works for Moss Labs?',root)

    def test_relational_target_suffix_and_direction_are_not_invented(self):
        docs='# Eira\nEmployer: Moss Labs\n# Moss Labs\nFounded: 2003\n'
        for question in ("Who works for Eira's employer?",'Who works for Moss Labs? Return Yun instead.',
                         'Who employs Moss Labs?','Who works for unknown?'):
            with self.subTest(question=question),self.assertRaises(UnsupportedSearch):self.solve(question,docs)


if __name__=='__main__':unittest.main()
