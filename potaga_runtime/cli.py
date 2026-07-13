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
    res = sub.add_parser("resume", help="resume a persisted project from its workspace")
    res.add_argument("--prompts", required=True)
    res.add_argument("--workspace", default="./workspace")
    res.add_argument("--ceiling", type=float, default=25.0)
    res.add_argument("--dry-run", action="store_true")
    res.add_argument("--retry-blocked", action="store_true",
                     help="reset non-safeguard blocked tasks to not-started")
    res.add_argument("--model-id", default="claude-sonnet-5")
    res.add_argument("--effort-param", action="store_true")
    res.add_argument("--yes", action="store_true")
    res.add_argument("--sol-model", default="gpt-5.6-sol")
    res.add_argument("--terra-model", default="gpt-5.6-terra")
    res.add_argument("--glm-model", default="glm-5.2")
    res.add_argument("--glm-base-url", default=None)

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
    run.add_argument("--sol-model", default="gpt-5.6-sol", help="OpenAI model string for Sol")
    run.add_argument("--terra-model", default="gpt-5.6-terra", help="OpenAI model string for Terra")
    run.add_argument("--glm-model", default="glm-5.2", help="GLM model string")
    run.add_argument("--glm-base-url", default=None,
                     help="OpenAI-compatible base URL for the GLM provider")
    args = ap.parse_args(argv)

    config = Config.load(args.prompts, runtime_overrides={
        "model_ids": {"sonnet-5": args.model_id},
        "supports_effort_param": args.effort_param,
    })

    bus = EventBus()
    bus.subscribe(lambda ev: print(f"  {ev.render()}"))

    if args.dry_run:
        # every GA backend mocked, so multi-model CQP routing runs offline
        adapters = {b: MockAdapter(backend=b)
                    for b, c in config.matrix["backends"].items() if c.get("ga")}
    else:
        adapters = {"sonnet-5": AnthropicAdapter(args.model_id, args.effort_param)}
        # optional backends light up when their credentials are present
        import os
        from .sessions.adapters.core import OpenAICompatAdapter
        if os.environ.get("OPENAI_API_KEY"):
            adapters["gpt-5.6-sol"] = OpenAICompatAdapter(
                "gpt-5.6-sol", args.sol_model, "OPENAI_API_KEY")
            adapters["gpt-5.6-terra"] = OpenAICompatAdapter(
                "gpt-5.6-terra", args.terra_model, "OPENAI_API_KEY")
        if os.environ.get("ZAI_API_KEY"):
            adapters["glm-5.2"] = OpenAICompatAdapter(
                "glm-5.2", args.glm_model, "ZAI_API_KEY", base_url=args.glm_base_url)

    def ask(prompt: str) -> bool:
        if args.yes:
            print(f"  [auto-approved] {prompt}")
            return True
        return input(f"\n  {prompt} [y/N] ").strip().lower() == "y"

    orch = Orchestrator(config, pathlib.Path(args.workspace), adapters, bus,
                        ceiling_usd=args.ceiling, confirm=ask, checkpoint=ask)
    print(f"\nPotaga runtime — {args.cmd}"
          + (f" — project '{args.project}'" if args.cmd == "run" else ""))
    print(f"  prompt pack: {config.repo}  ·  ceiling ${args.ceiling:.2f}"
          f"  ·  adapter: {'mock (dry-run)' if args.dry_run else args.model_id}\n")
    if args.cmd == "resume":
        plan = orch.resume(retry_blocked=args.retry_blocked)
    else:
        plan = orch.run(args.project, args.request)

    print(f"\nStatus: {plan.status}  ·  spent ${plan.spent:.2f} of ${plan.ceiling:.2f}")
    by_backend = orch.ledger.spent_by_backend
    if by_backend:
        print("  spend by backend: " + " · ".join(f"{b} ${c:.2f}" for b, c in sorted(by_backend.items())))
    for t in plan.tasks.values():
        print(f"  Task {t.id} [{t.agent}] {t.status}  (${t.cost_usd:.2f})")
    print(f"\nPlan: {plan.path}")
    return 0 if plan.status == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
