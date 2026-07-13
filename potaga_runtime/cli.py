"""CLI: `python -m potaga_runtime.cli run "Build X" --prompts ../potaga`

Phase 1 interface layer: intake, live event tail, human checkpoints on
stdin, cost summary at exit. --dry-run wires the MockAdapter so the full
dispatch path runs offline.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

from .config import Config
from .events import EventBus
from .orchestrator import Orchestrator
from .sessions.adapters.core import AnthropicAdapter, MockAdapter


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="potaga", description="Potaga runtime — Phase 1")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run a project end-to-end")
    run.add_argument("request", help="the software request, verbatim")
    run.add_argument("--prompts", required=True, help="path to the potaga prompt-pack repo")
    run.add_argument("--workspace", default="./workspace", help="project workspace directory")
    run.add_argument("--project", default="potaga-project")
    run.add_argument("--ceiling", type=float, default=25.0, help="budget ceiling in USD")
    run.add_argument("--dry-run", action="store_true", help="use the offline mock adapter")
    run.add_argument("--model-id", default="claude-sonnet-5",
                     help="API model string for the sonnet-5 backend "
                          "(set to a model your account can access; see docs.claude.com)")
    run.add_argument("--effort-param", action="store_true",
                     help="pass effort as an API parameter (only if your deployment supports it); "
                          "otherwise effort is folded into the system prompt")
    run.add_argument("--yes", action="store_true", help="auto-approve human checkpoints (CI use)")
    args = ap.parse_args(argv)

    config = Config.load(args.prompts, runtime_overrides={
        "model_ids": {"sonnet-5": args.model_id},
        "supports_effort_param": args.effort_param,
    })

    bus = EventBus()
    bus.subscribe(lambda ev: print(f"  {ev.render()}"))

    if args.dry_run:
        adapters = {"sonnet-5": MockAdapter()}
    else:
        adapters = {"sonnet-5": AnthropicAdapter(args.model_id, args.effort_param)}

    def ask(prompt: str) -> bool:
        if args.yes:
            print(f"  [auto-approved] {prompt}")
            return True
        return input(f"\n  {prompt} [y/N] ").strip().lower() == "y"

    orch = Orchestrator(config, pathlib.Path(args.workspace), adapters, bus,
                        ceiling_usd=args.ceiling, confirm=ask, checkpoint=ask)
    print(f"\nPotaga runtime — project '{args.project}'")
    print(f"  prompt pack: {config.repo}  ·  ceiling ${args.ceiling:.2f}"
          f"  ·  adapter: {'mock (dry-run)' if args.dry_run else args.model_id}\n")
    plan = orch.run(args.project, args.request)

    print(f"\nStatus: {plan.status}  ·  spent ${plan.spent:.2f} of ${plan.ceiling:.2f}")
    for t in plan.tasks.values():
        print(f"  Task {t.id} [{t.agent}] {t.status}  (${t.cost_usd:.2f})")
    print(f"\nPlan: {plan.path}")
    return 0 if plan.status == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
