"""Independent corpus/oracle/grader checks; no training Python is executed."""

import ast
from collections import Counter
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from benchmark.corpus import cases, generate, sql_reference
from benchmark.evaluate import DATASET, compare, evaluate, load_dataset, mock_proxy, summary, runtime_fingerprint


class BenchmarkTests(unittest.TestCase):
    def test_distribution_split_and_task_input_separation(self):
        manifest,refs = load_dataset(DATASET)
        self.assertEqual(len(refs),128)
        self.assertEqual(Counter((r["task_type"],r["split"]) for r in refs.values()),
                         Counter({(kind,split):16 for kind in ("math","search","data_analysis","coding")
                                  for split in ("development","holdout")}))
        self.assertEqual(sum(r["answerable"] for r in refs.values()),104)
        self.assertGreater(len({r["reward"] for r in refs.values()}),4)
        for kind in ("math","search","data_analysis","coding"):
            groups={split:{r["family"] for r in refs.values() if r["task_type"]==kind and r["split"]==split}
                    for split in ("development","holdout")}
            self.assertFalse(groups["development"] & groups["holdout"])
            self.assertEqual({r["difficulty"] for r in refs.values() if r["task_type"]==kind},{"easy","medium","hard"})
        self.assertNotIn("references.json",manifest["files"])

    def test_generation_is_byte_reproducible_and_has_no_agent_imports(self):
        with tempfile.TemporaryDirectory() as first,tempfile.TemporaryDirectory() as second:
            generate(first)
            generate(second)
            one={str(p.relative_to(first)):p.read_bytes() for p in Path(first).rglob("*") if p.is_file()}
            two={str(p.relative_to(second)):p.read_bytes() for p in Path(second).rglob("*") if p.is_file()}
            self.assertEqual(one,two)
            checked={str(p.relative_to(DATASET)):p.read_bytes() for p in DATASET.rglob("*") if p.is_file()}
            self.assertEqual(one,checked)
        source=Path("benchmark/corpus.py").read_text()
        imports={node.module for node in ast.walk(ast.parse(source)) if isinstance(node,ast.ImportFrom)}
        self.assertFalse(imports & {"harness","math_solver","search_solver","data_solver","coding_solver","plan_semantics"})

    def test_manifest_detects_mutation_and_generator_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            generate(directory)
            path=Path(directory)/"tasks/math_d01/task.json"
            path.write_text("{}")
            with self.assertRaises(ValueError):
                load_dataset(directory)
            with self.assertRaises(ValueError):
                generate(directory)
            self.assertEqual(path.read_text(),"{}")
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/".dockerignore").write_text("**\n!agent.py\n")
            (root/"agent.py").write_text("pass\n")
            before=runtime_fingerprint(root)
            (root/"agent.py").write_text("pass # changed\n")
            self.assertNotEqual(before,runtime_fingerprint(root))

    def test_sql_references_recompute_from_csv_not_solver(self):
        import csv
        manifest,refs=load_dataset(DATASET)
        count=0
        for key,ref in refs.items():
            oracle=ref["oracle"]
            if not oracle or oracle["method"]!="sqlite":
                continue
            with (DATASET/"tasks"/key/"workspace/observations.csv").open(newline="") as stream:
                rows=[(r["sku"],int(r["period"]),int(r["units"]),r["region"],int(r["price"]))
                      for r in csv.DictReader(stream)]
            self.assertEqual(sql_reference(rows,oracle["query"],shape=oracle["shape"]),ref["answer"],key)
            count+=1
        self.assertGreaterEqual(count,20)

    def test_authored_numeric_anchors_have_second_checks(self):
        _,refs=load_dataset(DATASET)
        expected={"math_d02":Fraction(-51,10),"math_d03":Fraction(3,4),"math_d06":Fraction(129),
                  "math_d09":Fraction(181,25),"math_h03":Fraction(2),"math_h08":Fraction(1331,10),
                  "math_h15":Fraction(53),"data_analysis_h05":Fraction(3,20)}
        for key,value in expected.items():
            self.assertEqual(Fraction(refs[key]["answer"]),value,key)

    def test_numeric_policy_is_exact_and_rejects_explanations(self):
        ref={"answerable":True,"answer":"1/2","policy":"numeric"}
        for text in ("0.5","2/4"," 0.500\n"):
            self.assertEqual(compare(text,ref),"correct")
        for text in ("0.50001","1/0","NaN","The answer is 0.5","0.5\nignored"):
            self.assertEqual(compare(text,ref),"wrong")

    def test_exact_json_and_coding_are_not_optimistically_graded(self):
        self.assertEqual(compare("osaka",{"answerable":True,"answer":"Osaka","policy":"exact"}),"wrong")
        ref={"answerable":True,"answer":'{"a":1,"b":2}',"policy":"json"}
        self.assertEqual(compare('{"b":2,"a":1}',ref),"correct")
        self.assertEqual(compare('{"a":0,"a":1,"b":2}',ref),"wrong")
        self.assertEqual(compare('{"a":true,"b":2}',ref),"wrong")
        self.assertEqual(compare('{"a":1.0,"b":2}',ref),"wrong")
        self.assertEqual(compare("DONE",{"answerable":True,"answer":"DONE","policy":"coding"}),"unverified")

    def test_refusal_and_false_positive_metrics_have_explicit_denominators(self):
        def row(status,answerable,actual,reward):
            return dict(status=status,answerable=answerable,actual=actual,reward=reward,
                        seconds=0,llm_spent="0",llm_reserved="0",llm_calls=0)
        rows=[row("correct",True,"3",5),row("wrong",True,"4",9),
              row("skipped",False,None,8),row("skipped",True,None,2)]
        result=summary(rows)
        self.assertEqual(result["reward"],5)
        self.assertEqual(result["possible_reward"],16)
        self.assertEqual(result["false_positive_fraction"],.5)
        self.assertEqual(result["justified_refusal_recall"],1)
        self.assertEqual(result["false_negative_skips"],1)
        rows[0]["llm_reserved"]="3/5"
        self.assertEqual(summary(rows)["llm_reserved"],.6)
        self.assertIsNone(summary([])["false_positive_fraction"])
        self.assertEqual(compare("42",{"answerable":False}),"wrong")
        self.assertEqual(compare(None,{"answerable":False}),"skipped")

    def test_pairs_are_predeclared_diverse_and_answerable(self):
        manifest,refs=load_dataset(DATASET)
        self.assertEqual(len(manifest["pairs"]),20)
        self.assertEqual(Counter(pair["category"] for pair in manifest["pairs"]),
                         Counter({kind:4 for kind in ("math+math","math+search","math+data_analysis","search+data_analysis","coding+any")}))
        for pair in manifest["pairs"]:
            self.assertEqual(len(set(pair["tasks"])),2)
            self.assertTrue(all(refs[key]["answerable"] for key in pair["tasks"]))

    def test_coding_fixtures_remain_data_and_worker_has_no_trust_flag(self):
        for case in cases():
            if case["task"]["task_type"]!="coding":
                continue
            for name,text in case["files"].items():
                try:
                    tree=ast.parse(text)
                except SyntaxError:
                    self.assertEqual(case["reference"]["family"],"damaged-source-syntax")
                    continue
                self.assertFalse(any(isinstance(node,(ast.While,ast.AsyncFunctionDef)) for node in ast.walk(tree)))
                self.assertNotIn("subprocess",text)
                self.assertNotIn("socket",text)
        worker=Path("benchmark/worker.py").read_text()
        self.assertNotIn("allow_trusted_code=",worker)
        self.assertNotIn("references.json",worker)
        self.assertNotIn("mocks.json",worker)

    def test_host_benchmark_uses_real_agent_without_reference_inputs(self):
        # Integration assertions use development only; holdout quality is a
        # separate explicit benchmark, not a CI target for production rules.
        result=evaluate(task_ids=["math_d01","search_d01","data_analysis_d01","coding_d01"])
        self.assertEqual(result["summary"]["correct"],3)
        self.assertEqual(result["summary"]["skipped"],1)
        self.assertEqual(result["summary"]["wrong"],0)
        self.assertFalse(result["trusted_code"])
        self.assertEqual(result["budget_state"]["spent"],"0.0")
        self.assertTrue(all(row["answer_mode"]==0o644 for row in result["rows"] if row["actual"] is not None))
        self.assertEqual(result["changed_inputs"],[])
        self.assertEqual(result["runtime_sha256"],runtime_fingerprint())

    def test_real_gateway_mock_accepts_valid_and_rejects_invalid_plans(self):
        for mode,correct in (("mock-good",1),("mock-wrong",0)):
            result=evaluate(task_ids=["math_d15"],mode=mode)
            self.assertEqual(result["summary"]["correct"],correct)
            self.assertEqual(result["summary"]["llm_calls"],1)
            self.assertGreater(result["summary"]["llm_spent"],0)
            self.assertEqual(result["summary"]["wrong"],0)

    def test_unknown_usage_and_outage_freeze_budget_but_preserve_local_answer(self):
        for mode in ("unavailable","mock-no-usage","mock-timeout"):
            result=evaluate(task_ids=["math_d01","math_d15"],mode=mode)
            self.assertEqual(result["summary"]["correct"],1)
            self.assertEqual(result["summary"]["skipped"],1)
            self.assertEqual(result["summary"]["llm_calls"],1)
            self.assertTrue(result["budget_state"]["frozen"])
            self.assertEqual(result["budget_state"]["uncertain_calls"],1)
            self.assertGreater(float(result["budget_state"]["reserved"]),0)

    def test_mock_request_never_receives_hidden_reference_keys(self):
        result=evaluate(task_ids=["search_d15","data_analysis_d14"],mode="mock-good")
        self.assertEqual(result["summary"]["correct"],2)
        self.assertEqual(result["summary"]["llm_calls"],1)
        # Worker observes only the current request; fixtures stay in the parent.
        for request in result["mock_requests"]:
            self.assertTrue(request["fixture_available"])
            self.assertFalse(request["reference_fields_present"])
        self.assertEqual(result["budget_state"]["uncertain_calls"],0)

    def test_docker_mock_mode_and_unknown_ids_are_rejected_before_execution(self):
        with patch("benchmark.evaluate.execute",side_effect=AssertionError("must not execute")):
            with self.assertRaises(ValueError):
                evaluate(backend="docker",mode="mock-good")
            with self.assertRaises(ValueError):
                evaluate(task_ids=["absent"])


if __name__ == "__main__":
    unittest.main()
