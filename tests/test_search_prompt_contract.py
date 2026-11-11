"""Starting subjects and ordered production Search plans stay independently grounded."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import harness
import httpx
from budget_manager import BudgetManager
from llm_fallback import InvalidPlan, prepare, solve_fallback
from llm_gateway import DEFAULT_MODEL, LLMGateway


class SearchPromptContractTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);kb=self.root/'workspace/kb';kb.mkdir(parents=True)
        # Both traversal orders exist and lead to different records.
        docs=['# Qira Holt\nMentor: Vern Reed\nEmployer: South Loom\n',
              '# Vern Reed\nEmployer: North Loom\n',
              '# North Loom\nHeadquarters: Lima\n',
              '# South Loom\nMentor: Alia West\n',
              '# Alia West\nHeadquarters: Suva\n']
        for i,text in enumerate(docs):(kb/f'part{i}.md').write_text(text)
        self.question="Where is Qira Holt's mentor's employer headquartered?"
        self.plan=dict(entity='Qira Holt',relations=['mentor','employer'],field='headquarters',answer='Lima')
        self.prepared=prepare('search',self.question,self.root)
        self.enterContext(patch.dict('os.environ',{'BUDGET':'10000'}))
        self.enterContext(patch('socket.socket.connect',side_effect=AssertionError('offline only')))

    def test_prompt_defines_root_and_individual_hops_without_literal_pipe_template(self):
        self.assertIn('"relations":[]',self.prepared.system)
        self.assertIn('["mentor", "employer"]',self.prepared.system)
        self.assertIn('starting subject',self.prepared.system)
        self.assertIn('Never use "mentor|employer" as a literal value',self.prepared.system)
        self.assertNotIn('["mentor|employer"]',self.prepared.system)

    def test_observed_terminal_entity_and_truncated_path_pattern_is_still_rejected(self):
        bad=dict(self.plan,entity='North Loom',relations=['employer'])
        with self.assertRaisesRegex(InvalidPlan,'unsupported or ungrounded relations'):
            self.prepared.validate(bad)
        with self.assertRaisesRegex(InvalidPlan,'unsupported or ungrounded relations'):
            self.prepared.validate(dict(bad,entity='Qira Holt'))
        with self.assertRaisesRegex(InvalidPlan,'unrelated entity'):
            self.prepared.validate(dict(bad,relations=['mentor','employer']))
        self.assertEqual(self.prepared.validate(self.plan),'Lima')

    def test_reversed_truncated_and_literal_pipe_paths_do_not_pass(self):
        for path in (['employer','mentor'],['mentor'],['employer'],['mentor|employer']):
            with self.subTest(path=path),self.assertRaises(InvalidPlan):
                self.prepared.validate(dict(self.plan,relations=path))

    def test_reverse_question_requires_reverse_path_even_with_valid_other_facts(self):
        reverse=prepare('search',"Where is Qira Holt's employer's mentor headquartered?",self.root)
        self.assertEqual(reverse.validate(dict(self.plan,relations=['employer','mentor'],answer='Suva')),'Suva')
        with self.assertRaises(InvalidPlan):reverse.validate(self.plan)

    def test_false_fact_or_changed_source_remains_rejected(self):
        with self.assertRaises(InvalidPlan):self.prepared.validate(dict(self.plan,answer='Suva'))
        (self.root/'workspace/kb/part2.md').write_text('# North Loom\nHeadquarters: Perth\n')
        with self.assertRaises(InvalidPlan):self.prepared.validate(self.plan)

    def test_real_production_function_accepts_full_mock_plan_and_rejects_bad_one(self):
        for plan,good in [(self.plan,True),(dict(self.plan,entity='North Loom',relations=['employer']),False)]:
            calls=[]
            def handler(request):
                calls.append(1)
                system=json.loads(request.content)['messages'][0]['content']
                self.assertEqual(system,self.prepared.system)
                return httpx.Response(200,json={'model':DEFAULT_MODEL,
                    'choices':[{'message':{'role':'assistant','content':json.dumps(plan)},'finish_reason':'stop'}],
                    'usage':{'prompt_tokens':100,'completion_tokens':50,'total_tokens':150}})
            gateway=LLMGateway('https://synthetic.invalid/v1','synthetic-key',manager=BudgetManager(10000),
                               timeout=60,max_tokens=512,transport=httpx.MockTransport(handler))
            if good:
                self.assertEqual(asyncio.run(solve_fallback(gateway,'search',self.question,self.root)),'Lima')
            else:
                with self.assertRaises(InvalidPlan):asyncio.run(solve_fallback(gateway,'search',self.question,self.root))
            self.assertEqual(calls,[1])
