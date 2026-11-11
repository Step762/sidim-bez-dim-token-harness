"""Offline A--F routing comparison; no client, credentials, task text or LIVE mode.

Reuses the existing replay loop, real Planner and shared BudgetManager. Probability
bands are sensitivity assumptions, never measured Qwen success probabilities.
"""
import argparse
from copy import deepcopy
from fractions import Fraction
import json
from pathlib import Path
from statistics import median
from unittest.mock import patch

from diagnostics import token_economics as economics
from planner import Planner


POLICIES = ('A_q5', 'B_local', 'C_reward_cost', 'D_conservative', 'E_two_stage', 'F_adaptive')
PROFILES = ('cheap', 'long_output', 'time_pressure', 'unknown_usage')
_BASE = economics.ReplayPlanner


def replay(rows, policy='A_q5', *, budget=100000, band='1/4', deadline=3540):
    """Outcomes are frozen before ranking; only settled observations can adapt F.

    All arms retain one local probe before paid work, full safe reservation,
    no retry, shared accounting, Coding blocked and the existing time limit.
    """
    if policy not in POLICIES:
        raise ValueError('unknown offline routing policy')
    band = Fraction(band)
    if not 0 <= band <= 1:
        raise ValueError('probability sensitivity band must lie in [0,1]')
    # Copy prevents caller mutation. Existing replay validates counts, models,
    # durations, costs and rejects LIVE/protocol/holdout tapes before execution.
    rows = deepcopy(rows)
    if any(set(r) - economics.TRACE_FIELDS for r in rows):
        raise ValueError('only allowlisted trace metadata is accepted')
    def allowed(row):
        if row.get('trace_source') in {'mock', 'local'}:
            return row.get('scope') == 'development'
        return (row.get('trace_source') == 'synthetic'
                and row.get('scope') in {'hypothesis_' + p for p in PROFILES})
    if not all(allowed(r) for r in rows):
        raise ValueError('only development/mock or synthetic traces are permitted')
    costs = [Fraction(r['estimated_cost']) for r in rows if r['eligible'] and r['task_type'] != 'coding']
    threshold = median(costs) if costs else Fraction(0)

    class OfflinePlanner(_BASE):
        def _sort_key(self, entry, estimate=None):
            row = self.rows[entry.task.task_id]
            reward = Fraction(str(entry.task.reward))
            if estimate is None:
                if self.policy == 'B_local':
                    return (not entry.features.native_form, entry.features.complexity, entry.key)
                return Planner._sort_key(self, entry, estimate)
            if self.policy == 'A_q5':
                return Planner._sort_key(self, entry, estimate)
            cost = Fraction(row['estimated_cost'])
            lower = max(Fraction(0), Fraction(row['estimated_success']) - band)
            if self.policy == 'B_local':
                return (cost, entry.key)
            if self.policy == 'C_reward_cost':
                return (-reward / cost, entry.key)
            if self.policy == 'D_conservative':
                return (-reward * lower / Fraction(estimate.reservation_tenths, 10), entry.key)
            if self.policy == 'E_two_stage':
                return (cost > threshold, -reward / cost, entry.key)
            # F uses exactly the existing confirmed-cost/accepted-result update.
            # A already adapts too; F differs only by conservative probability
            # band. "solved" here is validator acceptance, NOT judge correctness.
            lower = max(Fraction(0), estimate.probability - band)
            return (-reward * lower / estimate.cost_tenths, entry.key)

    with patch.object(economics, 'ReplayPlanner', OfflinePlanner), patch.object(economics, 'POLICIES', POLICIES):
        result = economics.replay(rows, policy, budget=budget, deadline=deadline)
    result.update(probability_band=str(band), cheap_stage_threshold=str(threshold),
                  probability_source='uncalibrated interval sensitivity; NOT measured Qwen',
                  real_requests=0)
    return result


def compare(rows, *, budgets=(100000, 10000, 0), bands=('0', '1/4', '1/2')):
    return [replay(rows, policy, budget=budget, band=band)
            for budget in budgets for band in bands for policy in POLICIES]


def experiment(*, seeds=(20261109, 20261110)):
    """Same seeded tape per arm. No task outcomes can choose the initial order."""
    runs = []
    for profile in PROFILES:
        for seed in seeds:
            rows = economics.synthetic_cohort(profile, seed=seed)
            results = compare(rows)
            runs.append({'profile': profile, 'seed': seed, 'tasks': len(rows),
                         'paid_eligible': sum(r['eligible'] for r in rows), 'results': results})
    return {'scope': 'synthetic scheduling sensitivity, NOT a Qwen quality/cost calibration',
            'real_requests': 0, 'production_changed': False,
            'assumptions': {'reward': 'random independently of complexity',
                            'probability': 'existing uncalibrated synthetic priors +/- declared band',
                            'result': 'frozen synthetic acceptance/correctness tape',
                            'adaptation': 'settled cost + validator outcome; no judge oracle'},
            'runs': runs}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error('choose a new output; previous results must not be overwritten')
    result = experiment()
    with args.output.open('x') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({'output': str(args.output), 'cohorts': len(result['runs']),
                      'simulations': sum(len(r['results']) for r in result['runs']), 'real_requests': 0}))


if __name__ == '__main__':
    main()
