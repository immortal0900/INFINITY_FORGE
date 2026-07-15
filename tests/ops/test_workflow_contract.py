"""Contracts for the stable GitHub gate and VPS one-shot timers."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "capability-eval.yml"
DEPLOY = ROOT / "forge" / "scripts" / "deploy-vps.sh"


def test_eval_is_the_single_stable_ruleset_context() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert re.findall(r"(?m)^  eval:\s*$", workflow) == ["  eval:"]
    assert "ruleset required status context" in workflow
    assert "private/free" not in workflow


def test_repo_importing_services_run_from_repo_root() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")

    assert "WorkingDirectory=$REPO_DIR" in deploy
    assert "Environment=PYTHONPATH=$REPO_DIR" in deploy
    assert (
        'mkunit stage  "$PIPELINE_LOCK /usr/bin/python3 '
        '$REPO_DIR/forge/scripts/stage-reconciler.py"'
        in deploy
    )
    assert (
        'mkunit mirror  "$PIPELINE_LOCK /usr/bin/python3 '
        '$REPO_DIR/forge/scripts/label-mirror.py"'
        in deploy
    )


def test_stage_and_mirror_are_non_overlapping_one_minute_oneshots() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")

    assert "Type=oneshot" in deploy
    assert "AccuracySec=1s" in deploy
    assert "[ -x /usr/bin/flock ]" in deploy
    assert (
        'PIPELINE_LOCK="/usr/bin/flock --nonblock --conflict-exit-code 0 '
        '%t/forge-pipeline.lock"'
        in deploy
    )
    assert deploy.count("mkunit stage ") == 1
    assert deploy.count("mkunit mirror ") == 1
    assert deploy.count("$PIPELINE_LOCK /usr/bin/python3 $REPO_DIR/forge/scripts/") == 2
    assert '"OnCalendar=*-*-* *:*:00"' in deploy
    assert '"OnCalendar=*-*-* *:*:30"' in deploy
    assert "for T in ledger stage mirror canary drift morning" in deploy
