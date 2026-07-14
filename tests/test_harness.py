"""Harness tests: one eval item end-to-end in dry mode, criteria evaluation,
calibration fields, and report aggregation."""
from __future__ import annotations

import json
import os
import pathlib

from eval.harness import main as harness_main
from eval.harness import report

REPO = str(pathlib.Path(os.environ.get(
    "POTAGA_REPO", pathlib.Path(__file__).parent.parent.parent / "repo")))


def test_harness_dry_run_item_and_report(tmp_path) -> None:
    rc = harness_main(["run", "--item", "hello_cli", "--prompts", REPO,
                       "--dry-run", "--runs-dir", str(tmp_path)])
    assert rc == 0
    metrics_files = list(tmp_path.glob("*/metrics.json"))
    assert len(metrics_files) == 1
    m = json.loads(metrics_files[0].read_text())
    assert m["item"] == "hello_cli" and m["mode"] == "dry"
    assert m["plan_status"] == "complete" and m["success"] is True
    assert m["criteria"]["require_complete"] is True
    assert m["criteria"]["artifacts:potaga-code/**/*"] is True
    assert m["total_cost_usd"] <= m["ceiling_usd"]
    # calibration fields present per task
    assert all("observed_loop_multiplier" in t for t in m["tasks"])
    assert m["calibration"]["loop_multiplier_config"] == 10
    # events log captured
    assert (metrics_files[0].parent / "events.log").read_text().count("routing") >= 1
    # report aggregates
    text = report(tmp_path)
    assert "hello_cli" in text and "✅" in text
    assert (tmp_path / "REPORT.md").exists()


def test_harness_unknown_item_errors(tmp_path) -> None:
    rc = harness_main(["run", "--item", "nope", "--prompts", REPO,
                       "--dry-run", "--runs-dir", str(tmp_path)])
    assert rc == 2
