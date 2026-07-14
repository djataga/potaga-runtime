# Live-run harness

Runs eval-set items through the real Orchestrator and turns each run into
`metrics.json` + an aggregated `runs/REPORT.md`: plan status, per-task
routing/cost/tokens, gate and event counts, success-criteria results, and the
**observed loop multiplier** (billed ÷ logical tokens) — the calibration
number that should eventually replace the ×10 guess in the routing defaults.

```bash
# validate the harness itself (offline)
python -m eval.harness run --all --prompts ../potaga --dry-run

# the real thing (7 weeks of Sonnet 5 intro pricing left as of mid-July 2026)
export ANTHROPIC_API_KEY=...
python -m eval.harness run --item hello_cli --prompts ../potaga --live \
    --model-id <model your account can access> [--effort-param]

python -m eval.harness report
```

Safety posture for `--live`: requires the explicit flag AND the key; the
per-item ceiling from `eval_set.yaml` is the budget ceiling, and the 90%
hard-pause **auto-declines** (a run that hits it fails rather than
overspends); human checkpoints auto-approve because these are unattended
benchmarks of benign projects — do not reuse this posture for real projects.

Suggested first live session: `hello_cli` alone (≤$1.50), read the events
log and plan, then `--all`. Dry-mode numbers (loop ×0.17, $0.01) are mock
artifacts; live numbers are the point.
