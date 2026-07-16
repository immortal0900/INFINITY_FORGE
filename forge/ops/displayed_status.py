"""The nine official Forge labels shown on GitHub issues."""

from __future__ import annotations

from enum import Enum

from .task_flow import TaskFlowState, TaskFlowStatus, TaskStep


class DisplayedStatus(str, Enum):
    NEEDS_DETAILS = "needs-details"
    NEEDS_DECISION = "needs-decision"
    READY_TO_BUILD = "ready-to-build"
    BUILDING = "building"
    REVIEWING = "reviewing"
    DEEP_CHECKING = "deep-checking"
    READY_TO_MERGE = "ready-to-merge"
    WAITING_FOR_HELP = "waiting-for-help"
    FAILED = "failed"


FORGE_STATUS_LABELS = frozenset(
    f"forge:{status.value}" for status in DisplayedStatus
)


def displayed_label(state: TaskFlowState | DisplayedStatus) -> str:
    """Return one and only one official status label."""

    if isinstance(state, DisplayedStatus):
        return f"forge:{state.value}"
    if not isinstance(state, TaskFlowState):
        raise TypeError("state must be a TaskFlowState or DisplayedStatus")
    if state.status is TaskFlowStatus.FAILED:
        return "forge:failed"
    if state.status is TaskFlowStatus.READY_TO_MERGE:
        return "forge:ready-to-merge"
    if state.current_step is TaskStep.BUILD:
        return "forge:building" if state.step_running else "forge:ready-to-build"
    if state.current_step is TaskStep.FIX:
        return "forge:building"
    if state.current_step is TaskStep.REVIEW:
        return "forge:reviewing"
    if state.current_step is TaskStep.DEEP_CHECK:
        return "forge:deep-checking"
    raise ValueError("running Task must have an official current step")
