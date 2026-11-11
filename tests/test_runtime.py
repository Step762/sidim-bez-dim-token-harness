"""Offline checks for the official CLI contract and Docker build inputs."""

import json
import os
from pathlib import Path
import subprocess
import sys
import stat
import tempfile
import unittest

from test_llm import local_server, MATH, MATH_PLAN


ROOT = Path(__file__).resolve().parents[1]


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        task = self.root / "math_mock"
        task.mkdir()
        (task / "task.json").write_text(json.dumps({
            "task_type": "math", "instruction": MATH, "reward": 1,
        }))
        self.answer = task / "answer.txt"

    def cli(self, environment, *arguments):
        return subprocess.run(
            [sys.executable, "-B", "run_agent.py", "--tasks-dir", str(self.root), *arguments],
            cwd=ROOT, env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1", **environment},
            capture_output=True, text=True, timeout=60,
        )

    def test_official_command_auto_enables_configured_mock_llm(self):
        with local_server(json.dumps(MATH_PLAN)) as server:
            result = self.cli({"BUDGET": "100000", "LLM_BASE_URL": server["base"], "LLM_API_KEY": server["key"]})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(self.answer.read_text(), "45\n")
            self.assertEqual(len(server["requests"]), 0)
            self.assertIn("llm_tokens_used=0", result.stdout)
            self.assertNotIn(server["key"], result.stdout + result.stderr)

    def test_explicit_disable_overrides_complete_environment(self):
        with local_server(json.dumps(MATH_PLAN)) as server:
            result = self.cli({"BUDGET": "100000", "LLM_BASE_URL": server["base"], "LLM_API_KEY": server["key"]}, "--disable-llm")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.answer.read_text(),"45\n")
            self.assertEqual(server["requests"], [])
            self.assertIn("llm_tokens_used=0", result.stdout)

    def test_zero_budget_overrides_complete_environment(self):
        with local_server(json.dumps(MATH_PLAN)) as server:
            result = self.cli({"BUDGET": "0", "LLM_BASE_URL": server["base"], "LLM_API_KEY": server["key"]})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.answer.read_text(),"45\n")
            self.assertEqual(server["requests"], [])

    def test_partial_environment_does_not_enable_llm(self):
        # These partial configurations cannot trigger HTTP.
        for environment in ({}, {"BUDGET": "100000"},
                            {"BUDGET": "100000", "LLM_BASE_URL": "http://127.0.0.1:1/v1"},
                            {"LLM_BASE_URL": "http://127.0.0.1:1/v1", "LLM_API_KEY": "mock-only"}):
            with self.subTest(keys=list(environment)):
                result = self.cli(environment)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.answer.read_text(),"45\n")
                self.assertIn("llm_tokens_used=0", result.stdout)

    def test_explicit_enable_requires_configuration(self):
        result = self.cli({}, "--enable-llm")
        self.assertEqual(result.returncode, 2)
        self.assertIn("explicit BUDGET", result.stderr)
        self.assertFalse(self.answer.exists())

    def test_auto_enable_validates_complete_configuration_before_local_work(self):
        result=self.cli({'BUDGET':'1000','LLM_BASE_URL':'invalid-url','LLM_API_KEY':'synthetic-key'})
        self.assertEqual(result.returncode,2)
        self.assertIn('LLM_BASE_URL',result.stderr)
        self.assertFalse(self.answer.exists())

    def test_llm_flags_are_mutually_exclusive(self):
        result = self.cli({}, "--enable-llm", "--disable-llm")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.answer.exists())

    def test_invalid_budget_fails_closed(self):
        for budget in ("-1", "invalid", "1.5"):
            with self.subTest(budget=budget):
                result = self.cli({"BUDGET": budget})
                self.assertEqual(result.returncode, 2)
                self.assertFalse(self.answer.exists())

    def test_build_context_is_an_explicit_allowlist(self):
        lines = (ROOT / ".dockerignore").read_text().splitlines()
        rules = [line for line in lines if line and not line.startswith("#")]
        self.assertEqual(rules[0], "**")
        allowed = {line[1:] for line in rules[1:]}
        expected = {"Dockerfile", ".dockerignore", "requirements.txt", "run_agent.py", "harness.py",
                    "planner.py", "budget_manager.py", "llm_gateway.py", "llm_fallback.py",
                    "math_solver.py", "search_solver.py", "relation_graph.py", "data_solver.py",
                    "coding_solver.py", "safe_files.py",
                    "plan_semantics.py", "runtime_limits.py", "coding_static.py",
                    "diagnostics/coding_acceptance.py", "diagnostics/coding_research.py",
                    "diagnostics/static_coding.py"}
        self.assertEqual(allowed, expected)
        self.assertTrue(all((ROOT / name).is_file() for name in allowed))
        self.assertTrue(all(line.startswith("!") for line in rules[1:]))

    def test_published_answer_is_readable_by_the_host_grader(self):
        from harness import save_answer
        save_answer(self.answer.parent, "45")
        self.assertEqual(stat.S_IMODE(self.answer.stat().st_mode), 0o644)
        self.assertEqual(self.answer.read_text(), "45\n")

    def test_docker_configuration_pins_base_and_avoids_runtime_installation(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertRegex(dockerfile.splitlines()[0], r"^FROM python:3\.12\.12-slim-bookworm@sha256:[0-9a-f]{64}$")
        self.assertIn("--no-compile", dockerfile)
        self.assertIn("PIP_NO_CACHE_DIR=1", dockerfile)
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", dockerfile)
        self.assertIn('ENTRYPOINT ["python"]', dockerfile)
        self.assertIn('CMD ["/app/run_agent.py", "--tasks-dir", "/data/tasks"]', dockerfile)
        self.assertNotIn("COPY . ", dockerfile)
        self.assertIn("search_solver.py relation_graph.py", dockerfile)
        self.assertNotIn("LLM_API_KEY=", dockerfile)
        self.assertNotIn("LLM_BASE_URL=", dockerfile)
        requirements = [line for line in (ROOT / "requirements.txt").read_text().splitlines()
                        if line and not line.startswith("#")]
        self.assertEqual(len(requirements), 169)
        self.assertTrue(all("==" in line for line in requirements))
        self.assertIn("openjiuwen==0.1.20.post1", requirements)


if __name__ == "__main__":
    unittest.main()
