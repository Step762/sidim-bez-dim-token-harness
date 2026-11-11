"""Post-failure evidence for the existing offline acceptance runner.

Run in the sanitized Docker test view, never against the mixed repository dataset:
  python /audit/timeout_diagnosis.py /audit/config.json /audit-result/probe.json
Config may select an explicit subset of the already allowed test modules.
No harness/solver/deadline is replaced. The observer runs only after a failure.
"""
import json
import os
from pathlib import Path
import sys
import unittest

TARGET = 'test_runtime_limits.RuntimeLimitTests.test_outer_workflow_timeout_cancels_network_and_holds_reserve'


def capture_failure(test, error):
    """Read only the synthetic target's result fields, never arbitrary locals."""
    if test.id() != TARGET:
        return None
    traceback = error[2]
    while traceback:
        frame = traceback.tb_frame
        if frame.f_code.co_name == test._testMethodName:
            local = frame.f_locals
            report = local.get('report')
            report = report if isinstance(report, dict) else {}
            counts = local.get('counts')
            counts = counts if isinstance(counts, dict) else {}
            first = local.get('a')
            return {
                'counts': {key: value for key, value in counts.items()
                           if key in {'solved', 'skipped', 'failed'} and type(value) is int},
                'time_exhausted': report.get('time_exhausted'),
                'outcomes': {Path(key).name: {field: value for field, value in outcome.items()
                    if (field in {'outcome', 'local_outcome'} and isinstance(value, str) and value in {'', 'solved', 'failed', 'timed_out',
                        'unsupported', 'invalid_data', 'unsafe', 'insufficient_evidence', 'budget_blocked', 'llm_failed', 'time_exhausted'})
                    or (field in {'local_attempts', 'llm_attempts'} and type(value) is int)}
                    for key, outcome in report.get('outcomes', {}).items()},
                'first_answer_exists': (first / 'answer.txt').exists() if isinstance(first, Path) else None,
            }
        traceback = traceback.tb_next
    return {'target_frame_available': False}


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        raise ValueError('expected config.json and a new output.json')
    config_path, output = map(Path, args)
    if output.exists():
        raise ValueError('refusing to overwrite an earlier diagnosis')
    config = json.loads(config_path.read_text())
    if not isinstance(config, dict) or set(config) - {'modules', 'root'}:
        raise ValueError('unknown diagnostic parameters')
    root = Path(config.get('root', '/app')).resolve(strict=True)
    # Reject the original mixed tree before any dataset file is opened.
    if (root / 'benchmark/corpus.py').exists() or (root / '.git').exists():
        raise ValueError('a physical sanitized acceptance view is required')
    sys.path[:0] = [str(root), str(root / 'tests')]
    import allowed_acceptance as acceptance
    if Path(acceptance.__file__).resolve().parents[1] != root:
        raise ValueError('acceptance helper does not belong to the selected view')
    tasks = root / 'benchmark/dataset/tasks'
    if set(os.listdir(tasks)) != acceptance.IDS:
        raise ValueError('dataset must contain exactly the 64 allowed development IDs')
    if any((tasks / key).is_symlink() for key in acceptance.IDS):
        raise ValueError('linked task directories are not permitted')
    modules = config.get('modules', list(acceptance.TESTS))
    if (not isinstance(modules, list) or not modules or not all(isinstance(name, str) and name in acceptance.TESTS for name in modules)
            or len(modules) != len(set(modules))):
        raise ValueError('modules must be a unique explicit subset of the test allowlist')
    acceptance.TESTS = tuple(modules)
    acceptance.CLOSED_TESTS = frozenset(
        name for name in acceptance.CLOSED_TESTS if name.split('.')[0] in modules)
    original = unittest.TestResult.addFailure
    evidence = []

    def observed(result, test, error):
        snapshot = capture_failure(test, error)
        if snapshot is not None:
            evidence.append(snapshot)
        return original(result, test, error)

    unittest.TestResult.addFailure = observed
    try:
        exit_code = acceptance.suite(output)
    finally:
        unittest.TestResult.addFailure = original
    result = json.loads(output.read_text())
    result.update(failure_snapshots=evidence, runner_exit_code=exit_code,
                  observation='post-failure only; original tests and deadlines unchanged',
                  selected_modules=modules)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
