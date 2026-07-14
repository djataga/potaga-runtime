"""Potaga live-run harness.

Runs eval-set items through the real Orchestrator, collects a metrics.json
per run (status, per-task routing/cost/tokens, gate and event counts,
success-criteria results, observed loop multipliers), and aggregates runs
into a markdown report.

    python -m eval.harness run --item hello_cli --prompts ../potaga --dry-run
    python -m eval.harness run --all          --prompts ../potaga --dry-run
    python -m eval.harness run --item todo_cli --prompts ../potaga --live
    python -m eval.harness report

Safety posture for --live:
- refuses to run without an explicit --live flag AND ANTHROPIC_API_KEY;
- the per-item ceiling from eval_set.yaml is the budget ceiling (the 90%
  hard-pause auto-declines in harness runs — a run that hits it fails,
  it does not overspend);
- human checkpoints auto-approve (these are unattended benchmark runs of
  benign projects; do NOT reuse this harness posture for real projects).

Calibration output: per task, observed_loop_multiplier = billed / logical
tokens — the number that should eventually replace the ×10 guess in
config/routing_matrix.yaml defaults.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import os
import pathlib
import sys
import time

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from potaga_runtime.config import Config                      # noqa: E402
from potaga_runtime.events import EventBus                    # noqa: E402
from potaga_runtime.orchestrator import Orchestrator          # noqa: E402
from potaga_runtime.router import BudgetExceeded              # noqa: E402
from potaga_runtime.sessions.adapters.core import (           # noqa: E402
    AnthropicAdapter, MockAdapter, OpenAICompatAdapter)

EVAL_DIR = pathlib.Path(__file__).resolve().parent
DEFAULT_RUNS_DIR = EVAL_DIR / "runs"


# ---------------------------------------------------------------- adapters
def build_adapters(config: Config, live: bool, model_id: str, effort_param: bool):
    if not live:
        return {b: MockAdapter(backend=b)
                for b, c in config.matrix["backends"].items() if c.get("ga")}
    adapters = {"sonnet-5": AnthropicAdapter(model_id, effort_param)}
    if os.environ.get("OPENAI_API_KEY"):
        adapters["gpt-5.6-sol"] = OpenAICompatAdapter("gpt-5.6-sol", "gpt-5.6-sol", "OPENAI_API_KEY")
        adapters["gpt-5.6-terra"] = OpenAICompatAdapter("gpt-5.6-terra", "gpt-5.6-terra", "OPENAI_API_KEY")
    if os.environ.get("ZAI_API_KEY"):
        adapters["glm-5.2"] = OpenAICompatAdapter("glm-5.2", "glm-5.2", "ZAI_API_KEY",
                                                  base_url=os.environ.get("GLM_BASE_URL"))
    return adapters


# ---------------------------------------------------------------- criteria
def evaluate_criteria(criteria: dict, plan, workspace: pathlib.Path,
                      bus: EventBus, total_cost: float) -> dict:
    results = {}
    if criteria.get("require_complete"):
        results["require_complete"] = plan.status == "complete"
    if "min_completed_tasks" in criteria:
        done = sum(1 for t in plan.tasks.values() if t.status == "completed")
        results["min_completed_tasks"] = done >= int(criteria["min_completed_tasks"])
    if "max_cost_usd" in criteria:
        results["max_cost_usd"] = total_cost <= float(criteria["max_cost_usd"])
    for glob in criteria.get("artifact_globs", []):
        store_dir, _, pattern = glob.partition("/")
        hits = [p for p in (workspace / store_dir).rglob("*")
                if p.is_file() and ".versions" not in p.parts
                and fnmatch.fnmatch(str(p.relative_to(workspace / store_dir)), pattern)]
        results[f"artifacts:{glob}"] = len(hits) > 0
    for ev in criteria.get("forbid_events", []):
        results[f"no:{ev}"] = not any(e.type.value == ev for e in bus.history)
    results["_success"] = all(v for k, v in results.items() if not k.startswith("_"))
    return results


# ---------------------------------------------------------------- one run
def run_item(item: dict, prompts: pathlib.Path, runs_dir: pathlib.Path,
             live: bool, model_id: str, effort_param: bool) -> dict:
    config = Config.load(prompts)
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = runs_dir / f"{ts}_{item['id']}_{'live' if live else 'dry'}"
    workspace = run_dir / "workspace"
    bus = EventBus()
    log_path = run_dir / "events.log"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_f = open(log_path, "a")
    bus.subscribe(lambda ev: (log_f.write(ev.render() + "\n"), log_f.flush()))

    adapters = build_adapters(config, live, model_id, effort_param)
    ceiling = float(item["ceiling_usd"])
    orch = Orchestrator(
        config, workspace, adapters, bus, ceiling_usd=ceiling,
        confirm=lambda _msg: False,   # 90% hard pause auto-DECLINES: fail, don't overspend
        checkpoint=lambda _msg: True)  # unattended benchmark: auto-approve checkpoints

    t0 = time.monotonic()
    status_note = ""
    try:
        plan = orch.run(item["id"], item["prompt"])
    except BudgetExceeded as e:
        status_note = f"budget-exceeded: {e}"
        plan = None
    wall_s = round(time.monotonic() - t0, 2)
    log_f.close()

    loop_default = float(config.defaults.get("loop_multiplier", 10))
    tasks_out, multipliers = [], []
    total_cost = 0.0
    if plan is not None:
        total_cost = plan.spent
        for t in plan.tasks.values():
            logical = t.est_tokens_in + t.est_tokens_out
            billed = t.tokens_in + t.tokens_out
            mult = round(billed / logical, 2) if logical and billed else None
            if mult:
                multipliers.append(mult)
            tasks_out.append({
                "id": t.id, "agent": t.agent, "backend": f"{t.model}@{t.effort}",
                "status": t.status, "cost_usd": round(t.cost_usd, 4),
                "tokens_logical": logical, "tokens_billed": billed,
                "observed_loop_multiplier": mult,
            })

    events_by_type: dict = {}
    for e in bus.history:
        events_by_type[e.type.value] = events_by_type.get(e.type.value, 0) + 1

    criteria = evaluate_criteria(item.get("criteria", {}), plan, workspace, bus,
                                 total_cost) if plan is not None else {"_success": False}

    metrics = {
        "item": item["id"], "mode": "live" if live else "dry", "started": ts,
        "wall_s": wall_s, "plan_status": plan.status if plan else "aborted",
        "note": status_note,
        "success": criteria["_success"], "criteria": criteria,
        "total_cost_usd": round(total_cost, 4), "ceiling_usd": ceiling,
        "spend_by_backend": {k: round(v, 4) for k, v in
                             (orch.ledger.spent_by_backend or {}).items()},
        "events": events_by_type, "tasks": tasks_out,
        "calibration": {
            "loop_multiplier_config": loop_default,
            "loop_multiplier_observed_mean":
                round(sum(multipliers) / len(multipliers), 2) if multipliers else None,
        },
    }
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))
    print(f"[{item['id']}] {metrics['plan_status']} · success={metrics['success']} "
          f"· ${metrics['total_cost_usd']:.2f}/{ceiling:.2f} · {wall_s}s "
          f"· loop×{metrics['calibration']['loop_multiplier_observed_mean']}")
    return metrics


# ---------------------------------------------------------------- report
def report(runs_dir: pathlib.Path) -> str:
    rows = []
    for f in sorted(runs_dir.glob("*/metrics.json")):
        rows.append(json.loads(f.read_text()))
    if not rows:
        return "no runs found\n"
    lines = ["# Potaga live-run report", "",
             "| run | item | mode | status | success | cost | ceiling | loop× obs/cfg | gates ✓/✗ | events |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for m in rows:
        ev = m["events"]
        lines.append("| {started} | {item} | {mode} | {plan_status} | {s} | ${cost:.2f} | ${ceil:.2f} "
                     "| {obs}/{cfg:.0f} | {gp}/{gf} | {n} |".format(
                         started=m["started"], item=m["item"], mode=m["mode"],
                         plan_status=m["plan_status"], s="✅" if m["success"] else "❌",
                         cost=m["total_cost_usd"], ceil=m["ceiling_usd"],
                         obs=m["calibration"]["loop_multiplier_observed_mean"],
                         cfg=m["calibration"]["loop_multiplier_config"],
                         gp=ev.get("gate-pass", 0), gf=ev.get("gate-fail", 0),
                         n=sum(ev.values())))
    live = [m for m in rows if m["mode"] == "live"
            and m["calibration"]["loop_multiplier_observed_mean"]]
    if live:
        mean = sum(m["calibration"]["loop_multiplier_observed_mean"] for m in live) / len(live)
        lines += ["", f"**Calibration (live runs only):** observed loop multiplier mean "
                      f"×{mean:.1f} vs configured ×{live[0]['calibration']['loop_multiplier_config']:.0f} "
                      f"— feed this back into `defaults.loop_multiplier`."]
    text = "\n".join(lines) + "\n"
    (runs_dir / "REPORT.md").write_text(text)
    return text


# ---------------------------------------------------------------- cli
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="eval.harness")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--prompts", required=True, help="path to the potaga prompt pack")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--item", help="eval item id")
    g.add_argument("--all", action="store_true")
    mode = r.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--live", action="store_true")
    r.add_argument("--model-id", default="claude-sonnet-5")
    r.add_argument("--effort-param", action="store_true")
    r.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR))
    p = sub.add_parser("report")
    p.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR))
    args = ap.parse_args(argv)

    runs_dir = pathlib.Path(args.runs_dir)
    if args.cmd == "report":
        print(report(runs_dir))
        return 0

    if args.live and not os.environ.get("ANTHROPIC_API_KEY"):
        print("--live requires ANTHROPIC_API_KEY", file=sys.stderr)
        return 2
    items = yaml.safe_load((EVAL_DIR / "eval_set.yaml").read_text())["items"]
    selected = items if args.all else [i for i in items if i["id"] == args.item]
    if not selected:
        print(f"unknown item '{args.item}'", file=sys.stderr)
        return 2
    ok = True
    for item in selected:
        m = run_item(item, pathlib.Path(args.prompts), runs_dir,
                     live=args.live, model_id=args.model_id,
                     effort_param=args.effort_param)
        ok = ok and m["success"]
    print()
    print(report(runs_dir))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
