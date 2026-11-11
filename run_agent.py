"""Command-line entry point for the competition harness."""

import argparse
import asyncio
import os
import time
from pathlib import Path


def main() -> int:
    started = time.monotonic()
    from llm_gateway import DEFAULT_MODEL, MODELS
    from planner import STRATEGIES

    parser = argparse.ArgumentParser(description="Run local task routes with optional Coding repair.")
    parser.add_argument("--tasks-dir", required=True, type=Path)
    coding_mode = parser.add_mutually_exclusive_group()
    coding_mode.add_argument(
        "--trusted-code", action="store_true",
        help="execute coding tests only in explicitly trusted working copies; this is not a sandbox",
    )
    coding_mode.add_argument(
        "--static-coding", dest="static_coding", action="store_const", const=True,
        help="infer bounded Coding repairs from literal tests without executing task Python",
    )
    coding_mode.add_argument(
        "--disable-static-coding", dest="static_coding", action="store_const", const=False,
        help="skip Coding tasks without executing task Python",
    )
    llm_mode = parser.add_mutually_exclusive_group()
    llm_mode.add_argument("--enable-llm", dest="llm", action="store_const", const=True,
                          help="require explicit LLM configuration and enable bounded fallback")
    llm_mode.add_argument("--disable-llm", dest="llm", action="store_const", const=False,
                          help="disable all LLM calls, even with configured environment")
    parser.set_defaults(llm=None, static_coding=None)
    parser.add_argument("--llm-model", choices=sorted(MODELS), default=DEFAULT_MODEL)
    parser.add_argument("--llm-timeout", type=float, default=20)
    parser.add_argument("--llm-max-tokens", type=int, default=1024)
    parser.add_argument("--time-limit",type=float,default=3540,help="total seconds including startup; maximum 3540")
    parser.add_argument("--operation-timeout",type=float,default=30,help="seconds per synchronous operation or workflow attempt")
    parser.add_argument("--strategy", choices=STRATEGIES, default="priority",
                        help="task ordering: priority (default), original, reward")
    args = parser.parse_args()
    from runtime_limits import RuntimeLimits
    try:
        limits = RuntimeLimits(args.time_limit,args.operation_timeout,started=started)
    except ValueError as exc: parser.error(str(exc))
    if not args.tasks_dir.is_dir():
        parser.error("--tasks-dir must name an existing directory")

    budget = os.environ.get("BUDGET")
    if budget is not None:
        try:
            if int(budget) < 0:
                raise ValueError
        except ValueError:
            parser.error("BUDGET must be a nonnegative integer")

    from harness import run_tasks
    from llm_gateway import LLMGateway, LLMError
    from budget_manager import BudgetError, BudgetManager

    try:
        manager = BudgetManager.from_env() if budget is not None else BudgetManager(0)
    except BudgetError as exc:
        parser.error(str(exc))

    gateway = None
    configured = all(os.environ.get(name, "").strip() for name in ("BUDGET", "LLM_BASE_URL", "LLM_API_KEY"))
    enable_llm = args.llm is True or (args.llm is None and configured)
    if enable_llm:
        if budget is None:
            parser.error("--enable-llm requires an explicit BUDGET")
        if int(budget) > 0:
            try:
                gateway = LLMGateway.from_env(
                    manager=manager,
                    model=args.llm_model, timeout=args.llm_timeout, max_tokens=args.llm_max_tokens,
                )
            except LLMError as exc:
                parser.error(str(exc))

    report = {}
    counts = asyncio.run(run_tasks(
        args.tasks_dir, allow_trusted_code=args.trusted_code,
        allow_static_coding=(args.static_coding is True or (args.static_coding is None and not args.trusted_code)),
        llm=gateway,
        budget=int(budget) if budget is not None else None,
        manager=manager,
        strategy=args.strategy, report=report,limits=limits,
    ))
    usage = gateway.summary() if gateway is not None else "llm_tokens_used=0"
    print(
        f"solved={counts['solved']} skipped={counts['skipped']} "
        f"failed={counts['failed']} {usage} "
        f"budget={budget if budget is not None else 'not_set'}",
        flush=True,
    )
    print(manager.summary(), flush=True)
    print(f"planner_strategy={args.strategy} reward_solved={report['reward_solved']} "
          f"unsuccessful_attempts={report['unsuccessful_attempts']}", flush=True)
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
