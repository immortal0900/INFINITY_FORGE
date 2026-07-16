from __future__ import annotations

import pytest

from forge.ops.contracts import (
    ContractError,
    parse_build_result,
    parse_deep_check_result,
    parse_review_result,
    parse_step_proof,
    source_result_hash,
)
from forge.ops.task_flow import (
    TaskFlowError,
    TaskFlowStatus,
    TaskStep,
    mark_task_step_running,
    next_task_action,
    observe_current_commit,
    record_fix_proof,
    record_task_result,
    required_steps,
    role_for_step,
    start_task_flow,
)
from forge.ops.task_options import TaskFlow, TaskRole


SETTINGS_HASH = "a" * 64
PR_URL = "https://github.com/owner/repo/pull/17"
BASE_COMMIT = "0" * 40


def _build(commit: str, *, remaining: list[str] | None = None):
    return parse_build_result(
        {
            "format_version": "forge-build-result/v1",
            "task_settings_hash": SETTINGS_HASH,
            "pr_url": PR_URL,
            "built_commit": commit,
            "changed_files": ["forge/ops/task_flow.py"],
            "completed_items": ["AC1"],
            "remaining_items": remaining or [],
            "checks_by_item": {"AC1": "tests/ops/test_task_flow.py::test_ac1"},
        }
    )


def _review(commit: str, source_hash: str, *, problems: bool = False):
    return parse_review_result(
        {
            "format_version": "forge-review-result/v1",
            "task_settings_hash": SETTINGS_HASH,
            "result": "changes_needed" if problems else "approve",
            "source_result_hash": source_hash,
            "pr_url": PR_URL,
            "reviewed_commit": commit,
            "change_check": {
                "confirmed_work": [] if problems else ["AC1"],
                "problems": ["missing AC2"] if problems else [],
            },
            "requirements_check": {
                "completed": ["AC1"],
                "missing": ["AC2"] if problems else [],
            },
            "fix_notes": "implement AC2" if problems else None,
        }
    )


def _deep_check(
    reviewed_commit: str,
    tested_commit: str,
    source_hash: str,
    *,
    problems: bool = False,
):
    return parse_deep_check_result(
        {
            "format_version": "forge-deep-check-result/v1",
            "task_settings_hash": SETTINGS_HASH,
            "result": "problems_found" if problems else "pass",
            "source_result_hash": source_hash,
            "pr_url": PR_URL,
            "reviewed_commit": reviewed_commit,
            "tested_commit": tested_commit,
            "added_tests": ["tests/ops/test_task_flow.py"],
            "tested_cases": ["empty input"],
            "fix_notes": "handle empty input" if problems else None,
        }
    )


def _proof(commit: str, source_hash: str, fix_notes: str):
    return parse_step_proof(
        {
            "format_version": "forge-step-proof/v1",
            "tested_commit": commit,
            "pr_url": PR_URL,
            "fix_notes": fix_notes,
            "source_result_hash": source_hash,
            "source_run_id": 12,
            "source_task_id": "t_fix_12",
            "task_settings_hash": SETTINGS_HASH,
        }
    )


@pytest.mark.parametrize(
    ("flow", "steps"),
    [
        (TaskFlow.BUILD, (TaskStep.BUILD,)),
        (TaskFlow.BUILD_REVIEW, (TaskStep.BUILD, TaskStep.REVIEW)),
        (
            TaskFlow.BUILD_REVIEW_DEEP_CHECK,
            (TaskStep.BUILD, TaskStep.REVIEW, TaskStep.DEEP_CHECK),
        ),
    ],
)
def test_each_flow_runs_only_selected_steps(
    flow: TaskFlow, steps: tuple[TaskStep, ...]
) -> None:
    assert required_steps(flow) == steps
    commit = "b" * 40
    state = start_task_flow(
        flow,
        task_settings_hash=SETTINGS_HASH,
        pr_url=PR_URL,
        current_base_commit=BASE_COMMIT,
        current_commit=commit,
    )
    visited: list[TaskStep] = []

    visited.append(next_task_action(state))
    build = _build(commit)
    state = record_task_result(state, build, current_commit=commit)
    if next_task_action(state) is TaskStep.REVIEW:
        visited.append(TaskStep.REVIEW)
        review = _review(commit, source_result_hash(build))
        state = record_task_result(state, review, current_commit=commit)
    if next_task_action(state) is TaskStep.DEEP_CHECK:
        visited.append(TaskStep.DEEP_CHECK)
        review_hash = state.expected_source_result_hash
        assert review_hash is not None
        deep_check = _deep_check(commit, commit, review_hash)
        state = record_task_result(state, deep_check, current_commit=commit)

    assert tuple(visited) == steps
    assert state.status is TaskFlowStatus.READY_TO_MERGE
    assert next_task_action(state) is None


@pytest.mark.parametrize(
    ("step", "role"),
    [
        (TaskStep.BUILD, TaskRole.BUILDER),
        (TaskStep.REVIEW, TaskRole.REVIEWER),
        (TaskStep.DEEP_CHECK, TaskRole.DEEP_CHECKER),
        (TaskStep.FIX, TaskRole.FIX),
    ],
)
def test_every_new_step_has_one_plain_role(step: TaskStep, role: TaskRole) -> None:
    assert role_for_step(step) is role


def test_build_rejects_remaining_work_and_stale_commit() -> None:
    current_commit = "b" * 40
    state = start_task_flow(
        TaskFlow.BUILD,
        task_settings_hash=SETTINGS_HASH,
        pr_url=PR_URL,
        current_base_commit=BASE_COMMIT,
        current_commit=current_commit,
    )

    with pytest.raises(TaskFlowError, match="remaining_items"):
        record_task_result(
            state,
            _build(current_commit, remaining=["AC2"]),
            current_commit=current_commit,
        )
    with pytest.raises(ContractError, match="current commit"):
        record_task_result(
            state,
            _build("c" * 40),
            current_commit=current_commit,
        )


def test_review_requires_the_exact_build_result_hash() -> None:
    commit = "b" * 40
    state = start_task_flow(
        TaskFlow.BUILD_REVIEW,
        task_settings_hash=SETTINGS_HASH,
        pr_url=PR_URL,
        current_base_commit=BASE_COMMIT,
        current_commit=commit,
    )
    state = record_task_result(state, _build(commit), current_commit=commit)

    with pytest.raises(TaskFlowError, match="source_result_hash"):
        record_task_result(
            state,
            _review(commit, "f" * 64),
            current_commit=commit,
        )


def test_deep_check_binds_reviewed_and_final_current_commits() -> None:
    reviewed_commit = "b" * 40
    tested_commit = "c" * 40
    state = start_task_flow(
        TaskFlow.BUILD_REVIEW_DEEP_CHECK,
        task_settings_hash=SETTINGS_HASH,
        pr_url=PR_URL,
        current_base_commit=BASE_COMMIT,
        current_commit=reviewed_commit,
    )
    build = _build(reviewed_commit)
    state = record_task_result(state, build, current_commit=reviewed_commit)
    review = _review(reviewed_commit, source_result_hash(build))
    state = record_task_result(state, review, current_commit=reviewed_commit)

    deep_check = _deep_check(
        reviewed_commit,
        tested_commit,
        source_result_hash(review),
    )
    state = record_task_result(state, deep_check, current_commit=tested_commit)

    assert state.status is TaskFlowStatus.READY_TO_MERGE
    assert state.current_commit == tested_commit


def test_rejection_runs_fix_at_most_three_times_then_fails() -> None:
    state = start_task_flow(
        TaskFlow.BUILD_REVIEW,
        task_settings_hash=SETTINGS_HASH,
        pr_url=PR_URL,
        current_base_commit=BASE_COMMIT,
        current_commit="1" * 40,
    )

    for attempt in range(1, 5):
        commit = state.current_commit
        build = _build(commit)
        state = record_task_result(state, build, current_commit=commit)
        review = _review(commit, source_result_hash(build), problems=True)
        state = record_task_result(state, review, current_commit=commit)

        if attempt <= 3:
            assert next_task_action(state) is TaskStep.FIX
            assert state.fix_count == attempt
            fixed_commit = str(attempt + 1) * 40
            state = record_fix_proof(
                state,
                _proof(
                    fixed_commit,
                    source_result_hash(review),
                    "implement AC2",
                ),
                current_commit=fixed_commit,
            )
            assert next_task_action(state) is TaskStep.BUILD
            assert state.completed_steps == ()
        else:
            assert state.status is TaskFlowStatus.FAILED
            assert state.fix_count == 3
            assert next_task_action(state) is None


def test_fix_proof_must_bind_rejection_and_restarts_from_build() -> None:
    commit = "b" * 40
    state = start_task_flow(
        TaskFlow.BUILD_REVIEW,
        task_settings_hash=SETTINGS_HASH,
        pr_url=PR_URL,
        current_base_commit=BASE_COMMIT,
        current_commit=commit,
    )
    build = _build(commit)
    state = record_task_result(state, build, current_commit=commit)
    review = _review(commit, source_result_hash(build), problems=True)
    state = record_task_result(state, review, current_commit=commit)

    with pytest.raises(TaskFlowError, match="source_result_hash"):
        record_fix_proof(
            state,
            _proof("c" * 40, "f" * 64, "implement AC2"),
            current_commit="c" * 40,
        )

    state = record_fix_proof(
        state,
        _proof("c" * 40, source_result_hash(review), "implement AC2"),
        current_commit="c" * 40,
    )
    assert next_task_action(state) is TaskStep.BUILD
    assert state.completed_steps == ()
    assert state.expected_source_result_hash is None


def test_new_push_invalidates_all_completed_proofs_and_restarts_build() -> None:
    old_commit = "b" * 40
    state = start_task_flow(
        TaskFlow.BUILD_REVIEW,
        task_settings_hash=SETTINGS_HASH,
        pr_url=PR_URL,
        current_base_commit=BASE_COMMIT,
        current_commit=old_commit,
    )
    build = _build(old_commit)
    state = record_task_result(state, build, current_commit=old_commit)
    review = _review(old_commit, source_result_hash(build))
    state = record_task_result(state, review, current_commit=old_commit)
    assert state.status is TaskFlowStatus.READY_TO_MERGE

    state = observe_current_commit(
        state,
        "c" * 40,
        current_base_commit=BASE_COMMIT,
    )

    assert state.status is TaskFlowStatus.RUNNING
    assert next_task_action(state) is TaskStep.BUILD
    assert state.completed_steps == ()
    assert state.expected_source_result_hash is None


def test_mark_running_preserves_action_but_rejects_duplicate_start() -> None:
    state = start_task_flow(
        TaskFlow.BUILD,
        task_settings_hash=SETTINGS_HASH,
        pr_url=PR_URL,
        current_base_commit=BASE_COMMIT,
        current_commit="b" * 40,
    )
    state = mark_task_step_running(state)

    assert next_task_action(state) is TaskStep.BUILD
    assert state.step_running is True
    with pytest.raises(TaskFlowError, match="already running"):
        mark_task_step_running(state)


def test_flow_preserves_the_exact_base_and_head_commits() -> None:
    state = start_task_flow(
        TaskFlow.BUILD,
        task_settings_hash=SETTINGS_HASH,
        pr_url=PR_URL,
        current_base_commit=BASE_COMMIT,
        current_commit="b" * 40,
    )

    assert state.current_base_commit == BASE_COMMIT
    assert state.current_commit == "b" * 40


def test_changed_base_commit_invalidates_completed_proof_and_restarts_build() -> None:
    head_commit = "b" * 40
    state = start_task_flow(
        TaskFlow.BUILD,
        task_settings_hash=SETTINGS_HASH,
        pr_url=PR_URL,
        current_base_commit=BASE_COMMIT,
        current_commit=head_commit,
    )
    state = record_task_result(state, _build(head_commit), current_commit=head_commit)
    assert state.status is TaskFlowStatus.READY_TO_MERGE

    state = observe_current_commit(
        state,
        head_commit,
        current_base_commit="d" * 40,
    )

    assert state.current_base_commit == "d" * 40
    assert state.current_commit == head_commit
    assert state.status is TaskFlowStatus.RUNNING
    assert state.current_step is TaskStep.BUILD
    assert state.completed_steps == ()
