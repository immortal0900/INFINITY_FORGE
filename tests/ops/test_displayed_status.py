from __future__ import annotations

from dataclasses import replace

from forge.ops.displayed_status import (
    FORGE_STATUS_LABELS,
    DisplayedStatus,
    displayed_label,
)
from forge.ops.task_flow import (
    TaskFlowStatus,
    TaskStep,
    mark_task_step_running,
    start_task_flow,
)
from forge.ops.task_options import TaskFlow


SETTINGS_HASH = "a" * 64
PR_URL = "https://github.com/owner/repo/pull/17"
COMMIT = "b" * 40
BASE_COMMIT = "c" * 40


def test_only_the_nine_plain_status_labels_are_recognized() -> None:
    assert FORGE_STATUS_LABELS == frozenset(
        {
            "forge:needs-details",
            "forge:needs-decision",
            "forge:ready-to-build",
            "forge:building",
            "forge:reviewing",
            "forge:deep-checking",
            "forge:ready-to-merge",
            "forge:waiting-for-help",
            "forge:failed",
        }
    )
    assert {displayed_label(status) for status in DisplayedStatus} == set(
        FORGE_STATUS_LABELS
    )


def test_build_waiting_and_running_have_different_labels() -> None:
    state = start_task_flow(
        TaskFlow.BUILD,
        task_settings_hash=SETTINGS_HASH,
        pr_url=PR_URL,
        current_base_commit=BASE_COMMIT,
        current_commit=COMMIT,
    )

    assert displayed_label(state) == "forge:ready-to-build"
    assert displayed_label(mark_task_step_running(state)) == "forge:building"


def test_review_deep_check_ready_and_failed_labels_are_exact() -> None:
    state = start_task_flow(
        TaskFlow.BUILD_REVIEW_DEEP_CHECK,
        task_settings_hash=SETTINGS_HASH,
        pr_url=PR_URL,
        current_base_commit=BASE_COMMIT,
        current_commit=COMMIT,
    )

    assert (
        displayed_label(replace(state, current_step=TaskStep.REVIEW))
        == "forge:reviewing"
    )
    assert (
        displayed_label(replace(state, current_step=TaskStep.DEEP_CHECK))
        == "forge:deep-checking"
    )
    assert (
        displayed_label(
            replace(
                state,
                current_step=None,
                status=TaskFlowStatus.READY_TO_MERGE,
            )
        )
        == "forge:ready-to-merge"
    )
    assert (
        displayed_label(
            replace(state, current_step=None, status=TaskFlowStatus.FAILED)
        )
        == "forge:failed"
    )
