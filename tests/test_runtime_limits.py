"""Real workflow deadlines; all LLM requests below use MockTransport."""

import asyncio
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import signal
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
import harness
import planner
from budget_manager import BudgetManager
from llm_gateway import LLMGateway
from runtime_limits import RuntimeLimits, OperationTimeout, DEFAULT_RUN_SECONDS
from test_harness import write_task
from test_llm import MATH, MATH_PLAN, envelope


class RuntimeLimitTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)

    def run_tasks(self,limits,**kwargs):
        report={}
        with redirect_stdout(io.StringIO()):
            counts=asyncio.run(harness.run_tasks(self.root,limits=limits,strategy='original',report=report,**kwargs))
        return counts,report

    def test_hour_margin_and_invalid_configuration(self):
        self.assertEqual(DEFAULT_RUN_SECONDS,3540)
        self.assertLessEqual(RuntimeLimits().remaining,3540)
        for total,operation in ((0,1),(-1,1),(3600,1),(float('nan'),1),(2,0),(2,float('inf'))):
            with self.assertRaises(ValueError):RuntimeLimits(total,operation)

    def test_sync_sleep_is_interrupted_and_alarm_is_restored(self):
        previous=signal.getsignal(signal.SIGALRM)
        started=time.monotonic()
        with self.assertRaises(OperationTimeout),RuntimeLimits(2,.03).sync():time.sleep(.5)
        self.assertLess(time.monotonic()-started,.3)
        self.assertEqual(signal.getsignal(signal.SIGALRM),previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL),(0.0,0.0))

    def test_cpu_loop_is_interrupted(self):
        with self.assertRaises(OperationTimeout),RuntimeLimits(2,.02).sync():
            while True: pass

    def test_slow_first_local_does_not_block_second_or_trigger_fallback(self):
        a=write_task(self.root,'a',instruction='slow');b=write_task(self.root,'b',instruction='fast')
        def solver(text):
            if text=='slow':time.sleep(.5)
            return 7
        with patch.dict(harness.SOLVERS,{'math':solver}):
            counts,report=self.run_tasks(RuntimeLimits(2,.04))
        self.assertEqual(counts,{'solved':1,'skipped':1,'failed':0})
        self.assertFalse((a/'answer.txt').exists())
        self.assertEqual((b/'answer.txt').read_text().strip(),'7')
        self.assertEqual(report['outcomes'][str(a)]['outcome'],'timed_out')
        self.assertEqual(report['outcomes'][str(a)]['llm_attempts'],0)

    def test_global_expiry_keeps_first_answer_and_stops_remaining_tasks(self):
        async def warm():harness.build_workflow()
        asyncio.run(warm())  # Build SDK graph inside its actual event-loop context.
        a=write_task(self.root,'a',instruction='fast');write_task(self.root,'b',instruction='slow');c=write_task(self.root,'c',instruction='fast')
        def solver(text):
            if text=='slow':time.sleep(1)
            return 7
        with patch.dict(harness.SOLVERS,{'math':solver}):
            counts,report=self.run_tasks(RuntimeLimits(.4,1))
        self.assertEqual((a/'answer.txt').read_text().strip(),'7')
        self.assertFalse((c/'answer.txt').exists())
        self.assertTrue(report['time_exhausted'])
        self.assertEqual(counts['solved'],1)

    def test_outer_workflow_timeout_cancels_network_and_holds_reserve(self):
        a=write_task(self.root,'a');write_task(self.root,'b',instruction=MATH)
        calls=[]
        async def respond(request):
            calls.append(1)
            await asyncio.sleep(5)
            raise AssertionError('stalled request should be cancelled')
        manager=BudgetManager(2000)
        gateway=LLMGateway('http://mock.invalid/v1','synthetic-deadline-key',manager=manager,
                           max_tokens=64,timeout=5,transport=httpx.MockTransport(respond))
        with patch.dict(os.environ,{'BUDGET':'2000'}):
            counts,report=self.run_tasks(RuntimeLimits(2,.08),llm=gateway,manager=manager,budget=2000)
        self.assertEqual(len(calls),1)
        self.assertTrue(manager.snapshot()['frozen'])
        self.assertGreater(manager.snapshot()['reserved'],0)
        self.assertTrue((a/'answer.txt').exists())
        self.assertEqual(counts['solved'],1)

    def test_slow_fallback_preparation_does_not_block_next_local_task(self):
        a=write_task(self.root,'a',instruction=MATH);b=write_task(self.root,'b')
        manager=BudgetManager(2000)
        gateway=LLMGateway('http://mock.invalid/v1','synthetic-preflight-key',manager=manager,max_tokens=64,
                           transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(AssertionError('no HTTP'))))
        def slow(*args,**kwargs):time.sleep(.5)
        with patch.object(planner,'prepare',side_effect=slow),patch.dict(os.environ,{'BUDGET':'2000'}):
            _,report=self.run_tasks(RuntimeLimits(2,.03),llm=gateway,manager=manager,budget=2000)
        self.assertEqual(report['outcomes'][str(a)]['outcome'],'timed_out')
        self.assertTrue((b/'answer.txt').exists())
        self.assertEqual(manager.snapshot()['requests'],())

    def test_synchronous_fallback_execution_is_bounded(self):
        import llm_fallback
        a=write_task(self.root,'a',instruction=MATH);b=write_task(self.root,'b')
        async def warm():harness.build_workflow()
        asyncio.run(warm())
        manager=BudgetManager(2000)
        gateway=LLMGateway('http://mock.invalid/v1','synthetic-validation-key',manager=manager,max_tokens=64,
            transport=httpx.MockTransport(lambda request:httpx.Response(200,json=envelope(json.dumps(MATH_PLAN)))))
        original=llm_fallback.calculate_math
        def slow(*args):
            time.sleep(.4)
            return original(*args)
        started=time.monotonic()
        with patch.object(llm_fallback,'calculate_math',side_effect=slow),patch.dict(os.environ,{'BUDGET':'2000'}):
            _,report=self.run_tasks(RuntimeLimits(2,.04),llm=gateway,manager=manager,budget=2000)
        self.assertLess(time.monotonic()-started,.3)
        self.assertTrue((b/'answer.txt').exists())
        self.assertFalse((a/'answer.txt').exists())
        self.assertEqual(report['outcomes'][str(a)]['outcome'],'timed_out')
        self.assertEqual(len(manager.snapshot()['requests']),1)
        self.assertFalse(manager.snapshot()['frozen'])  # Usage was confirmed before local validation.

    def test_slow_feature_probe_is_bounded_independently(self):
        a=write_task(self.root,'a');b=write_task(self.root,'b')
        original=planner.features
        def features(task,*args):
            if task.directory==a:time.sleep(.5)
            return original(task,*args)
        with patch.object(planner,'features',side_effect=features):
            _,report=self.run_tasks(RuntimeLimits(2,.03))
        self.assertEqual(report['outcomes'][str(a)]['outcome'],'timed_out')
        self.assertTrue((b/'answer.txt').exists())

    def test_write_error_isolated_from_second_task(self):
        a=write_task(self.root,'a');b=write_task(self.root,'b')
        original=harness.save_answer
        def save(directory,answer):
            if directory==a:raise PermissionError('controlled fixture write failure')
            return original(directory,answer)
        with patch.object(harness,'save_answer',side_effect=save):counts,_=self.run_tasks(RuntimeLimits(2,.2))
        self.assertEqual((counts['solved'],counts['failed']),(1,1))
        self.assertTrue((b/'answer.txt').exists())

    def test_unknown_type_does_not_stop_supported_second_task(self):
        write_task(self.root,'a',task_type='unknown');b=write_task(self.root,'b')
        counts,_=self.run_tasks(RuntimeLimits(2,.2))
        self.assertEqual((counts['solved'],counts['skipped']),(1,1))
        self.assertTrue((b/'answer.txt').exists())

    def test_incomplete_workflow_isolated_from_next_task(self):
        a=write_task(self.root,'a');b=write_task(self.root,'b')
        original=harness.Workflow.invoke
        async def invoke(workflow,inputs,*args,**kwargs):
            if inputs['task_dir']==str(a):return SimpleNamespace(state='FAILED')
            return await original(workflow,inputs,*args,**kwargs)
        with patch.object(harness.Workflow,'invoke',invoke):counts,_=self.run_tasks(RuntimeLimits(2,.2))
        self.assertEqual((counts['solved'],counts['failed']),(1,1))
        self.assertTrue((b/'answer.txt').exists())

    def test_interrupted_publication_preserves_old_file_and_other_answers(self):
        a=write_task(self.root,'a');b=write_task(self.root,'b');c=write_task(self.root,'c')
        (b/'answer.txt').write_text('previous\n')
        fsync=harness.os.fsync
        count=0
        def slow(fd):
            nonlocal count
            count+=1
            if count==2:time.sleep(.5)
            return fsync(fd)
        with patch.object(harness.os,'fsync',side_effect=slow):counts,_=self.run_tasks(RuntimeLimits(2,.03))
        self.assertEqual((b/'answer.txt').read_text(),'previous\n')
        self.assertTrue((a/'answer.txt').exists() and (c/'answer.txt').exists())
        self.assertEqual(list(b.glob('.answer-*.tmp')),[])
        self.assertEqual(counts['solved'],2)
