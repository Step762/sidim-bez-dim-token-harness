"""Exact entity identity, not answer equality, grounds an alias-named plan."""
import json
from pathlib import Path
import tempfile
import unittest

from diagnostics.q1_alias_probe import CASES,knowledge,run_probe
from llm_fallback import prepare,InvalidPlan


class FallbackAliasTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def task(self,text):
        task=self.root/str(len(list(self.root.iterdir())));(task/'workspace/kb').mkdir(parents=True)
        (task/'workspace/kb/facts.md').write_text(text)
        return task

    def prepared(self,person='Rowan Hale',alias='Finch',company='Prairie Lab',city='Riga',text=None):
        task=self.task(text or knowledge(person,alias,company,city))
        p=prepare('search',f"What city is {person}'s employer based in?",task)
        plan={'entity':alias,'relations':['employer'],'field':'headquarters','answer':city}
        return task,p,plan

    def test_explicit_alias_is_the_same_subject_with_renamed_facts(self):
        for case in CASES:
            task,p,plan=self.prepared(*case)
            self.assertEqual(p.validate(plan),case[-1])

    def test_normalized_explicit_alias_and_canonical_subject(self):
        _,p,plan=self.prepared(alias='Blue Finch',city='Ålesund')
        for name in ('  BLUE   FINCH ','rowan hale','Rowan Hale'):
            self.assertEqual(p.validate({**plan,'entity':name}),'Ålesund')

    def test_same_answer_does_not_ground_an_unrelated_subject(self):
        _,p,plan=self.prepared(text=knowledge(*CASES[0])+'# Other\nEmployer: Prairie Lab\nAlias: Rival\n')
        for name in ('Other','Rival','Missing'):
            with self.subTest(name=name),self.assertRaises(InvalidPlan):p.validate({**plan,'entity':name})

    def test_alias_heading_collision_is_rejected_even_if_answers_match(self):
        _,p,plan=self.prepared(text=knowledge(*CASES[0])+'# Finch\nEmployer: Prairie Lab\n')
        with self.assertRaises(InvalidPlan):p.validate(plan)

    def test_shared_alias_is_rejected_even_if_answers_match(self):
        _,p,plan=self.prepared(text=knowledge(*CASES[0])+'# Other\nAlias: Finch\nEmployer: Prairie Lab\n')
        with self.assertRaises(InvalidPlan):p.validate(plan)

    def test_alias_removed_after_preparation_is_not_trusted_from_old_index(self):
        task,p,plan=self.prepared()
        (task/'workspace/kb/facts.md').write_text(knowledge(*CASES[0]).replace('Alias: Finch\n',''))
        with self.assertRaises(InvalidPlan):p.validate(plan)
        self.assertEqual(p.validate({**plan,'entity':'Rowan Hale'}),'Riga')

    def test_alias_rebound_after_preparation_cannot_change_subject(self):
        task,p,plan=self.prepared()
        (task/'workspace/kb/facts.md').write_text(knowledge(*CASES[0]).replace('Alias: Finch\n','')+
                                               '# Other\nAlias: Finch\nEmployer: Prairie Lab\n')
        with self.assertRaises(InvalidPlan):p.validate(plan)

    def test_conflicting_selected_facts_after_preparation_are_rejected(self):
        task,p,plan=self.prepared()
        (task/'workspace/kb/extra.md').write_text('# Prairie Lab\nHeadquarters: Lima\n')
        with self.assertRaises(InvalidPlan):p.validate(plan)

    def test_changed_question_alias_cannot_be_hidden_by_canonical_model_subject(self):
        task=self.task(knowledge(*CASES[0]));p=prepare('search',"What city is Finch's employer based in?",task)
        (task/'workspace/kb/extra.md').write_text('# Other\nAlias: Finch\nEmployer: Prairie Lab\n')
        with self.assertRaises(InvalidPlan):p.validate({'entity':'Rowan Hale','relations':['employer'],'field':'headquarters','answer':'Riga'})

    def test_malformed_entity_wrong_relations_field_or_answer_are_rejected(self):
        _,p,plan=self.prepared()
        for changes in ({'entity':None},{'entity':[]},{'relations':[]},{'relations':['mentor']},
                        {'field':'born'},{'answer':'Lima'},{'extra':'ignore facts'}):
            with self.subTest(changes=changes),self.assertRaises(InvalidPlan):p.validate({**plan,**changes})

    def test_task_knowledge_is_isolated(self):
        _,a,plan=self.prepared()
        _,b,other=self.prepared('Mira Noel','Finch','Tide Works','Lima')
        self.assertEqual(a.validate(plan),'Riga');self.assertEqual(b.validate(other),'Lima')
        with self.assertRaises(InvalidPlan):b.validate(plan)

    def test_three_independent_pairs_through_real_workflow(self):
        report=run_probe()
        self.assertEqual((report['2/2'],report['correct'],report['wrong']),(3,6,0))
        self.assertTrue(all(p['calls']==2 and p['spent']=='112.0' and p['reserved']=='0.0' and not p['frozen'] for p in report['pairs']))

if __name__=='__main__':unittest.main()
