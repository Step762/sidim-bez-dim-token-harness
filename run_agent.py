"""Command-line entry point for the competition harness."""

import argparse
import asyncio
import os
from pathlib import Path


def main() -> int:
    from llm_gateway import DEFAULT_MODEL, MODELS

    parser = argparse.ArgumentParser(description="Run local task routes with optional trusted code repair.")
    parser.add_argument("--tasks-dir", required=True, type=Path)
    parser.add_argument(
        "--trusted-code", action="store_true",
        help="execute coding tests only in explicitly trusted working copies; this is not a sandbox",
    )
    parser.add_argument("--enable-llm", action="store_true", help="enable bounded LLM fallback")
    parser.add_argument("--llm-model", choices=sorted(MODELS), default=DEFAULT_MODEL)
    parser.add_argument("--llm-timeout", type=float, default=20)
    parser.add_argument("--llm-max-tokens", type=int, default=1024)
    args = parser.parse_args()
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
    if args.enable_llm:
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

    counts = asyncio.run(run_tasks(
        args.tasks_dir, allow_trusted_code=args.trusted_code, llm=gateway,
        budget=int(budget) if budget is not None else None,
        manager=manager,
    ))
    usage = gateway.summary() if gateway is not None else "llm_tokens_used=0"
    print(
        f"solved={counts['solved']} skipped={counts['skipped']} "
        f"failed={counts['failed']} {usage} "
        f"budget={budget if budget is not None else 'not_set'}",
        flush=True,
    )
    print(manager.summary(), flush=True)
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
