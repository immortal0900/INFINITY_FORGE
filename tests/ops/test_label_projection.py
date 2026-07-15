from __future__ import annotations

import pytest

from forge.ops.contracts import PipelineStage, StageOutcome
from forge.ops.label_projection import ProjectionState, projected_label


def test_reviewer_ready_projects_need_review() -> None:
    state = ProjectionState(PipelineStage.REVIEWER, "ready", None, False, 0)

    assert projected_label(state) == "forge:need-review"


def test_reviewer_reject_projects_need_execution() -> None:
    state = ProjectionState(
        PipelineStage.REVIEWER,
        "done",
        StageOutcome.REJECT,
        False,
        1,
    )

    assert projected_label(state) == "forge:need-execution"


def test_rework_ready_projects_need_execution() -> None:
    state = ProjectionState(PipelineStage.EXECUTOR_REWORK, "ready", None, False, 1)

    assert projected_label(state) == "forge:need-execution"


def test_critic_running_projects_need_critic() -> None:
    state = ProjectionState(PipelineStage.CRITIC, "running", None, False, 0)

    assert projected_label(state) == "forge:need-critic"


def test_critic_pass_pending_ci_stays_need_critic() -> None:
    state = ProjectionState(
        PipelineStage.CRITIC,
        "done",
        StageOutcome.PASS,
        False,
        0,
    )

    assert projected_label(state) == "forge:need-critic"


def test_critic_pass_green_current_head_projects_mergeable() -> None:
    state = ProjectionState(
        PipelineStage.CRITIC,
        "done",
        StageOutcome.PASS,
        True,
        0,
    )

    assert projected_label(state) == "forge:mergeable"


@pytest.mark.parametrize("outcome", [StageOutcome.REJECT, StageOutcome.DEFECT_FOUND])
def test_rework_limit_projects_failed(outcome: StageOutcome) -> None:
    state = ProjectionState(
        PipelineStage.EXECUTOR_REWORK,
        "done",
        outcome,
        False,
        3,
    )

    assert projected_label(state) == "forge:failed"


def test_raw_executor_done_never_projects_mergeable() -> None:
    state = ProjectionState(PipelineStage.EXECUTOR, "done", None, True, 0)

    assert projected_label(state) == "forge:need-review"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("triage", "forge:spec-draft"),
        ("todo", "forge:need-execution"),
        ("ready", "forge:need-execution"),
        ("running", "forge:in-progress"),
        ("blocked", "forge:blocked"),
        ("failed", "forge:failed"),
    ],
)
def test_executor_status_projection(status: str, expected: str) -> None:
    state = ProjectionState(PipelineStage.EXECUTOR, status, None, False, 0)

    assert projected_label(state) == expected


def test_unknown_task_status_has_no_projection() -> None:
    state = ProjectionState(PipelineStage.REVIEWER, "cancelled", None, False, 0)

    assert projected_label(state) is None


@pytest.mark.parametrize("max_reworks", [0, 4])
def test_projection_rejects_invalid_rework_limit(max_reworks: int) -> None:
    state = ProjectionState(PipelineStage.REVIEWER, "ready", None, False, 0)

    with pytest.raises(ValueError, match="max_reworks"):
        projected_label(state, max_reworks=max_reworks)
