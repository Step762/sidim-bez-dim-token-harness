"""Regression tests for untrusted task paths and nonblocking input handling."""

import asyncio
from contextlib import redirect_stdout
import io
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness import SOLVERS, run_tasks, save_answer
from math_solver import solve_math
import safe_files
from safe_files import UnsafeFile, open_text, read_text


LOCAL = ("A warehouse starts with 100 boxes. It ships 7 boxes per day for 3 days. "
         "How many boxes does it have at the end?")


class TaskFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "tasks"
        self.root.mkdir()

    def task(self, name, kind="math", instruction=LOCAL, root=None):
        directory = (root or self.root) / name
        directory.mkdir(parents=True)
        (directory / "task.json").write_text(json.dumps({
            "task_type": kind, "instruction": instruction, "reward": 1,
        }))
        return directory

    def run_bounded(self):
        # fork inherits the already imported framework; a regression must not
        # hang the test runner itself. No task-supplied Python is executed.
        context = multiprocessing.get_context("fork")
        receiver, sender = context.Pipe(duplex=False)

        def worker():
            try:
                with redirect_stdout(io.StringIO()):
                    counts = asyncio.run(run_tasks(self.root, strategy="original"))
                sender.send((True, counts))
            except Exception as exc:
                sender.send((False, f"{type(exc).__name__}: {exc}"))
            finally:
                sender.close()

        process = context.Process(target=worker)
        process.start()
        sender.close()
        try:
            process.join(10)
            self.assertFalse(process.is_alive(), "untrusted input blocked the task set")
            self.assertTrue(receiver.poll(), "worker did not return a result")
            success, result = receiver.recv()
            self.assertTrue(success, result)
            return result
        finally:
            if process.is_alive():
                process.terminate()
                process.join(3)
            receiver.close()
            process.close()

    def assert_good(self, directory):
        path = directory / "answer.txt"
        self.assertEqual(path.read_text(), "79\n")
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_external_task_directory_link_cannot_publish_outside_root(self):
        external = self.task("outside", root=self.base)
        (self.root / "a_alias").symlink_to(external, target_is_directory=True)
        good = self.task("z_good")
        self.run_bounded()
        self.assertFalse((external / "answer.txt").exists())
        self.assert_good(good)

    def test_external_task_json_link_is_not_read(self):
        external = self.task("outside", root=self.base)
        bad = self.root / "a_bad"
        bad.mkdir()
        (bad / "task.json").symlink_to(external / "task.json")
        good = self.task("z_good")
        self.run_bounded()
        self.assertFalse((bad / "answer.txt").exists())
        self.assert_good(good)

    def test_directory_alias_does_not_abort_independent_tasks(self):
        first = self.task("a_first")
        (self.root / "b_alias").symlink_to(first, target_is_directory=True)
        good = self.task("z_good")
        self.run_bounded()
        self.assert_good(first)
        self.assert_good(good)

    def test_fifo_task_json_does_not_block(self):
        bad = self.root / "a_bad"
        bad.mkdir()
        os.mkfifo(bad / "task.json")
        good = self.task("z_good")
        self.run_bounded()
        self.assertFalse((bad / "answer.txt").exists())
        self.assert_good(good)

    def test_fifo_markdown_does_not_block(self):
        bad = self.task("a_bad", "search", "What year was Ada born?")
        kb = bad / "workspace/kb"
        kb.mkdir(parents=True)
        os.mkfifo(kb / "record.md")
        good = self.task("z_good")
        self.run_bounded()
        self.assertFalse((bad / "answer.txt").exists())
        self.assert_good(good)

    def test_fifo_csv_does_not_block_direct_solver(self):
        bad = self.task("a_bad", "data_analysis", "The file sales.csv. What is the sum of units?")
        workspace = bad / "workspace"
        workspace.mkdir()
        os.mkfifo(workspace / "sales.csv")
        # The planner already rejects a FIFO here; also test the solver boundary.
        context = multiprocessing.get_context("fork")
        def worker():
            from data_solver import UnsupportedData, solve_data
            try:
                solve_data("The file sales.csv. What is the sum of units?", bad)
            except UnsupportedData:
                return
            raise AssertionError("FIFO was accepted")
        process = context.Process(target=worker)
        process.start()
        try:
            process.join(3)
            self.assertFalse(process.is_alive(), "CSV FIFO blocked the solver")
            self.assertEqual(process.exitcode, 0)
        finally:
            if process.is_alive():
                process.terminate()
                process.join(3)
            process.close()

    def test_oversized_json_does_not_block_valid_tasks(self):
        bad = self.task("a_bad")
        with (bad / "task.json").open("ab") as stream:
            stream.truncate(safe_files.TASK_JSON_BYTES + 1)
        good = self.task("z_good")
        self.run_bounded()
        self.assertFalse((bad / "answer.txt").exists())
        self.assert_good(good)

    def test_oversized_markdown_does_not_block_valid_tasks(self):
        bad = self.task("a_bad", "search", "What year was Ada born?")
        kb = bad / "workspace/kb"
        kb.mkdir(parents=True)
        with (kb / "record.md").open("wb") as stream:
            stream.truncate(safe_files.MARKDOWN_BYTES + 1)
        good = self.task("z_good")
        self.run_bounded()
        self.assertFalse((bad / "answer.txt").exists())
        self.assert_good(good)

    def test_oversized_csv_does_not_block_valid_tasks(self):
        bad = self.task("a_bad", "data_analysis", "The file sales.csv. What is the sum of units?")
        workspace = bad / "workspace"
        workspace.mkdir()
        with (workspace / "sales.csv").open("wb") as stream:
            stream.truncate(safe_files.CSV_BYTES + 1)
        good = self.task("z_good")
        self.run_bounded()
        self.assertFalse((bad / "answer.txt").exists())
        self.assert_good(good)

    def test_bounded_reader_accepts_exact_limit_and_rejects_growth(self):
        path = self.base / "input.txt"
        path.write_text("abcd")
        self.assertEqual(read_text(self.base, path.name, maximum=4), "abcd")
        with self.assertRaises(UnsafeFile):
            read_text(self.base, path.name, maximum=3)
        with self.assertRaises(UnsafeFile):
            with open_text(self.base, path.name, maximum=4) as stream:
                with path.open("a") as writer:
                    writer.write("e")
                stream.read()

    def test_mutation_during_read_is_rejected(self):
        path = self.base / "input.txt"
        path.write_text("before")
        with self.assertRaises(UnsafeFile):
            with open_text(self.base, path.name, maximum=100) as stream:
                stream.read()
                path.write_text("changed input")

    def test_leaf_symlink_replacement_between_stat_and_open_is_rejected(self):
        path = self.base / "input.md"
        path.write_text("inside")
        external = self.base / "external.txt"
        external.write_text("outside")
        original_open = os.open

        def swap(name, flags, *args, **kwargs):
            if name == path.name:
                path.rename(self.base / "original.md")
                path.symlink_to(external)
            return original_open(name, flags, *args, **kwargs)

        with patch("safe_files.os.open", side_effect=swap), self.assertRaises((OSError, UnsafeFile)):
            read_text(self.base, path.name, maximum=100)

    def test_leaf_fifo_replacement_uses_nonblocking_open_and_fstat(self):
        path = self.base / "input.md"
        path.write_text("inside")
        original_open = os.open

        def swap(name, flags, *args, **kwargs):
            if name == path.name:
                self.assertTrue(flags & os.O_NONBLOCK)
                path.rename(self.base / "original.md")
                os.mkfifo(path)
            return original_open(name, flags, *args, **kwargs)

        with patch("safe_files.os.open", side_effect=swap), self.assertRaises(UnsafeFile):
            read_text(self.base, path.name, maximum=100)

    def test_regular_file_replacement_between_stat_and_open_is_rejected(self):
        path = self.base / "input.txt"
        path.write_text("inside")
        original_open = os.open

        def swap(name, flags, *args, **kwargs):
            if name == path.name:
                path.rename(self.base / "original.txt")
                path.write_text("changed")
            return original_open(name, flags, *args, **kwargs)

        with patch("safe_files.os.open", side_effect=swap), self.assertRaises(UnsafeFile):
            read_text(self.base, path.name, maximum=100)

    def test_parent_symlink_replacement_before_open_is_rejected(self):
        directory = self.base / "nested"
        directory.mkdir()
        (directory / "input.txt").write_text("inside")
        external = self.base / "external"
        external.mkdir()
        (external / "input.txt").write_text("outside")
        original_open = os.open

        def swap(name, flags, *args, **kwargs):
            if name == "nested":
                directory.rename(self.base / "original")
                directory.symlink_to(external, target_is_directory=True)
            return original_open(name, flags, *args, **kwargs)

        with patch("safe_files.os.open", side_effect=swap), self.assertRaises((OSError, UnsafeFile)):
            read_text(self.base, "nested/input.txt", maximum=100)

    def test_open_parent_descriptor_cannot_redirect_read_to_external_file(self):
        directory = self.base / "nested"
        directory.mkdir()
        (directory / "input.txt").write_text("inside")
        external = self.base / "external"
        external.mkdir()
        (external / "input.txt").write_text("outside")
        original_open = os.open

        def swap(name, flags, *args, **kwargs):
            if name == "input.txt":
                directory.rename(self.base / "original")
                directory.symlink_to(external, target_is_directory=True)
            return original_open(name, flags, *args, **kwargs)

        with patch("safe_files.os.open", side_effect=swap):
            self.assertEqual(read_text(self.base, "nested/input.txt", maximum=100), "inside")

    def test_task_directory_replacement_during_workflow_is_isolated(self):
        bad = self.task("a_bad")
        good = self.task("z_good")
        external = self.base / "external"
        external.mkdir()
        calls = 0

        def swap(instruction):
            nonlocal calls
            calls += 1
            if calls == 1:
                bad.rename(self.root / "moved")
                bad.symlink_to(external, target_is_directory=True)
            return solve_math(instruction)

        with patch.dict(SOLVERS, {"math": swap}), redirect_stdout(io.StringIO()):
            counts = asyncio.run(run_tasks(self.root, strategy="original"))
        self.assertEqual(counts, {"solved": 1, "skipped": 0, "failed": 1})
        self.assertFalse((external / "answer.txt").exists())
        self.assert_good(good)

    def test_new_directory_at_same_task_path_is_not_accepted(self):
        bad = self.task("a_bad")
        good = self.task("z_good")
        calls = 0

        def swap(instruction):
            nonlocal calls
            calls += 1
            if calls == 1:
                bad.rename(self.root / "moved")
                bad.mkdir()
            return solve_math(instruction)

        with patch.dict(SOLVERS, {"math": swap}), redirect_stdout(io.StringIO()):
            counts = asyncio.run(run_tasks(self.root, strategy="original"))
        self.assertEqual(counts["failed"], 1)
        self.assertFalse((bad / "answer.txt").exists())
        self.assert_good(good)

    def test_answer_symlink_is_replaced_without_touching_its_target(self):
        good = self.task("good")
        external = self.base / "external.txt"
        external.write_text("untouched")
        (good / "answer.txt").symlink_to(external)
        self.run_bounded()
        self.assertEqual(external.read_text(), "untouched")
        self.assertFalse((good / "answer.txt").is_symlink())
        self.assert_good(good)

    def test_answer_directory_swap_cleans_up_through_original_descriptor(self):
        directory = self.root / "task"
        directory.mkdir()
        external = self.base / "external"
        external.mkdir()
        moved = self.root / "moved"
        original_fsync = os.fsync

        def swap(fd):
            original_fsync(fd)
            directory.rename(moved)
            directory.symlink_to(external, target_is_directory=True)

        with patch("harness.os.fsync", side_effect=swap), self.assertRaises((OSError, UnsafeFile)):
            save_answer(directory, "79")
        self.assertEqual(list(external.iterdir()), [])
        self.assertEqual(list(moved.iterdir()), [])

    def test_hard_linked_input_is_rejected(self):
        bad = self.task("a_bad")
        os.link(bad / "task.json", self.base / "alias.json")
        good = self.task("z_good")
        self.run_bounded()
        self.assertFalse((bad / "answer.txt").exists())
        self.assert_good(good)

    def test_kb_total_size_and_document_count_are_bounded(self):
        from search_solver import UnsupportedSearch, solve_search
        task = self.task("search", "search", "What year was Ada born?")
        kb = task / "workspace/kb"
        kb.mkdir(parents=True)
        (kb / "one.md").write_text("# Ada\nBorn: 1983\n")
        (kb / "two.md").write_text("# Bea\nBorn: 1990\n")
        with patch("search_solver.KB_BYTES", 16), self.assertRaises(UnsupportedSearch):
            solve_search("What year was Ada born?", task)
        with patch("safe_files.MAX_KB_FILES", 1), self.assertRaises(UnsupportedSearch):
            solve_search("What year was Ada born?", task)

    def test_special_file_is_rejected_before_open(self):
        original_open = os.open

        def guarded(name, *args, **kwargs):
            self.assertNotEqual(name, "null", "must not open a character device")
            return original_open(name, *args, **kwargs)

        with patch("safe_files.os.open", side_effect=guarded), self.assertRaises(UnsafeFile):
            read_text(Path("/dev"), "null", maximum=100)

    def test_internal_workspace_directory_link_is_also_rejected(self):
        from data_solver import UnsupportedData, solve_data
        task = self.task("data", "data_analysis", "The file alias/sales.csv. What is the sum of units?")
        real = task / "workspace/real"
        real.mkdir(parents=True)
        (real / "sales.csv").write_text("units\n7\n")
        (task / "workspace/alias").symlink_to(real, target_is_directory=True)
        with self.assertRaises(UnsupportedData):
            solve_data("The file alias/sales.csv. What is the sum of units?", task)

    def test_kb_depth_and_entry_limits(self):
        kb = self.base / "kb"
        nested = kb / "one/two"
        nested.mkdir(parents=True)
        (nested / "facts.md").write_text("# Ada\nBorn: 1983\n")
        with patch("safe_files.MAX_TREE_DEPTH", 1), self.assertRaises(UnsafeFile):
            safe_files.markdown_files(kb)
        with patch("safe_files.MAX_TREE_ENTRIES", 1), self.assertRaises(UnsafeFile):
            safe_files.markdown_files(kb)

    def test_fallback_csv_preparation_uses_the_same_protected_reader(self):
        from llm_fallback import prepare
        instruction = "Calculate total units in sales.csv for all rows."
        task = self.task("data", "data_analysis", instruction)
        workspace = task / "workspace"
        workspace.mkdir()
        path = workspace / "sales.csv"
        path.write_text("units\n7\n")
        original_open = os.open

        def swap(name, flags, *args, **kwargs):
            if name == "sales.csv":
                self.assertTrue(flags & os.O_NONBLOCK)
                path.rename(workspace / "original.csv")
                os.mkfifo(path)
            return original_open(name, flags, *args, **kwargs)

        with patch("safe_files.os.open", side_effect=swap), self.assertRaises(UnsafeFile):
            prepare("data_analysis", instruction, task)

    def test_normal_nested_markdown_and_quoted_csv_still_work(self):
        math = self.task("a_math")
        search = self.task("b_search", "search", "What year was Ada born?")
        kb = search / "workspace/kb/nested"
        kb.mkdir(parents=True)
        (kb / "record.md").write_text("# Ada\nBorn: 1983\n")
        data = self.task("c_data", "data_analysis",
                         "The file nested/sales.csv. What is the sum of units where product = 'kit, deluxe'?")
        workspace = data / "workspace/nested"
        workspace.mkdir(parents=True)
        (workspace / "sales.csv").write_bytes(b'\xef\xbb\xbfproduct,units\r\n"kit, deluxe",7\r\nother,100\r\n')
        self.assertEqual(self.run_bounded(), {"solved": 3, "skipped": 0, "failed": 0})
        self.assert_good(math)
        self.assertEqual((search / "answer.txt").read_text(), "1983\n")
        self.assertEqual((data / "answer.txt").read_text(), "7\n")


if __name__ == "__main__":
    unittest.main()
