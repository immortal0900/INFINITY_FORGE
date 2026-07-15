"""Pure projection of one Forge pipeline frontier to one GitHub label."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import PipelineStage, StageOutcome


@dataclass(frozen=True)
class ProjectionState:
    """The minimum evidence needed to display one pipeline frontier."""

    stage: PipelineStage
    task_status: str
    outcome: StageOutcome | None
    current_head_green: bool
    rework_count: int


def projected_label(snapshot: ProjectionState, max_reworks: int = 3) -> str | None:
    """Return the only forge label allowed for the supplied frontier.

    ``forge:mergeable`` deliberately has one narrow path: a completed critic
    result with outcome ``pass`` whose current PR HEAD is green.
    """

    if (
        not isinstance(max_reworks, int)
        or isinstance(max_reworks, bool)
        or not 1 <= max_reworks <= 3
    ):
        raise ValueError("max_reworks must be an integer from 1 to 3")
    if not isinstance(snapshot, ProjectionState):
        raise TypeError("snapshot must be a ProjectionState")
    if not isinstance(snapshot.stage, PipelineStage):
        raise ValueError("stage must be a PipelineStage")
    if not isinstance(snapshot.task_status, str) or not snapshot.task_status:
        raise ValueError("task_status must be a non-empty string")
    if snapshot.outcome is not None and not isinstance(snapshot.outcome, StageOutcome):
        raise ValueError("outcome must be a StageOutcome or None")
    if not isinstance(snapshot.current_head_green, bool):
        raise ValueError("current_head_green must be a boolean")
    if (
        not isinstance(snapshot.rework_count, int)
        or isinstance(snapshot.rework_count, bool)
        or snapshot.rework_count < 0
    ):
        raise ValueError("rework_count must be a non-negative integer")
    if snapshot.rework_count > max_reworks:
        raise ValueError("rework_count exceeds max_reworks")

    status = snapshot.task_status
    if status == "blocked":
        return "forge:blocked"
    if status == "failed":
        return "forge:failed"

    needs_rework = snapshot.outcome in {
        StageOutcome.REJECT,
        StageOutcome.DEFECT_FOUND,
    }
    if status == "done" and needs_rework:
        if snapshot.rework_count >= max_reworks:
            return "forge:failed"
        return "forge:need-execution"

    if snapshot.stage in {PipelineStage.EXECUTOR, PipelineStage.EXECUTOR_REWORK}:
        if status == "triage":
            return "forge:spec-draft"
        if status in {"todo", "ready"}:
            return "forge:need-execution"
        if status == "running":
            return "forge:in-progress"
        if status == "done":
            return "forge:need-review"
        return None

    if snapshot.stage is PipelineStage.REVIEWER:
        if status == "done" and snapshot.outcome is StageOutcome.APPROVE:
            return "forge:need-critic"
        if status in {"triage", "todo", "ready", "running", "done"}:
            return "forge:need-review"
        return None

    if snapshot.stage is PipelineStage.CRITIC:
        if (
            status == "done"
            and snapshot.outcome is StageOutcome.PASS
            and snapshot.current_head_green
        ):
            return "forge:mergeable"
        if status in {"triage", "todo", "ready", "running", "done"}:
            return "forge:need-critic"
        return None

    return None
