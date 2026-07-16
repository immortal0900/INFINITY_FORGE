from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import product
from uuid import uuid4

import pytest

from forge.ops.merge_decision import (
    AUTO_MERGE_ALLOWED,
    CHECK_ERROR,
    MANUAL_MERGE_REQUIRED,
    REFRESH_BRANCH,
    RESTART_FLOW,
    WAIT,
    MergeContext,
    MergePullRequest,
    decide_merge,
)
from forge.ops.safe_files import (
    AUTO_MERGE_ALLOWED as SAFE_FILES_ALLOWED,
    CHECK_ERROR as SAFE_FILES_ERROR,
    MANUAL_MERGE_REQUIRED as SAFE_FILES_MANUAL,
    SafeFilesEvidence,
    SafeFilesResult,
)
from forge.ops.task_options import MergeMode, TaskFlow
from forge.ops.task_flow import (
    TaskFlowState,
    TaskFlowStatus,
    TaskStep,
    required_steps,
)
from forge.ops.task_settings import (
    TaskContent,
    TaskSettings,
    TaskSettingsStatus,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
BASE = "0" * 40
HEAD = "a" * 40
MERGE_COMMIT = "c" * 40
PR_URL = "https://github.com/openai/infinity-forge/pull/9"


def _settings(
    *,
    task_flow: TaskFlow = TaskFlow.BUILD_REVIEW,
    merge_mode: MergeMode = MergeMode.FULL_AUTO,
) -> TaskSettings:
    prepared = TaskSettings.create(
        request_id=str(uuid4()),
        repository="openai/infinity-forge",
        task_content=TaskContent(
            title="Merge safely",
            description="Merge only the tested commit.",
            acceptance_criteria=("The tested commit is unchanged.",),
        ),
        task_flow=task_flow,
        merge_mode=merge_mode,
        confirmed_by="user-7",
        confirmed_at=NOW - timedelta(hours=1),
    )
    return replace(
        prepared,
        issue_number=7,
        status=TaskSettingsStatus.ACTIVE,
    )


def _safe_evidence(
    code: str = SAFE_FILES_ALLOWED,
    *,
    base_commit: str = BASE,
    head_commit: str = HEAD,
) -> SafeFilesEvidence:
    return SafeFilesEvidence(
        base_commit=base_commit,
        head_commit=head_commit,
        result=SafeFilesResult(
            code=code,
            reason="fixture",
            paths=("docs/guide.md",),
        ),
    )


def _flow_state(settings: TaskSettings) -> TaskFlowState:
    assert settings.task_settings_hash is not None
    return TaskFlowState(
        task_flow=settings.task_flow,
        task_settings_hash=settings.task_settings_hash,
        pr_url=PR_URL,
        current_base_commit=BASE,
        current_commit=HEAD,
        current_step=None,
        status=TaskFlowStatus.READY_TO_MERGE,
        step_running=False,
        completed_steps=required_steps(settings.task_flow),
    )


def _context(
    *,
    task_flow: TaskFlow = TaskFlow.BUILD_REVIEW,
    merge_mode: MergeMode = MergeMode.FULL_AUTO,
    safe_code: str = SAFE_FILES_ALLOWED,
    **changes: object,
) -> MergeContext:
    settings = _settings(task_flow=task_flow, merge_mode=merge_mode)
    values: dict[str, object] = {
        "settings": settings,
        "repository": settings.repository,
        "issue_number": settings.issue_number,
        "task_content_hash": settings.task_content_hash,
        "task_flow_state": _flow_state(settings),
        "pull_request": MergePullRequest(
            pr_url=PR_URL,
            repository=settings.repository,
            base_commit=BASE,
            head_commit=HEAD,
            is_open=True,
            is_draft=False,
            is_merged=False,
            merged_commit=None,
            merged_base_commit=None,
            merged_head_commit=None,
            has_conflict=False,
            base_is_current=True,
            rules_allow_merge=True,
            unresolved_review_threads=0,
            eval_status="success",
            eval_commit=HEAD,
            eval_check_count=1,
        ),
        "displayed_status": "forge:ready-to-merge",
        "safe_files": _safe_evidence(safe_code),
        "now": NOW,
        "branch_refresh_count": 0,
    }
    values.update(changes)
    return MergeContext(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("task_flow", "merge_mode"),
    tuple(product(tuple(TaskFlow), tuple(MergeMode))),
)
def test_all_nine_combinations_keep_flow_and_merge_mode_independent(
    task_flow: TaskFlow,
    merge_mode: MergeMode,
) -> None:
    decision = decide_merge(_context(task_flow=task_flow, merge_mode=merge_mode))

    expected = (
        MANUAL_MERGE_REQUIRED
        if merge_mode is MergeMode.MANUAL
        else AUTO_MERGE_ALLOWED
    )
    assert decision.code == expected


def test_full_auto_never_skips_current_eval_check() -> None:
    pr = replace(_context().pull_request, eval_status="failure")

    decision = decide_merge(_context(pull_request=pr))

    assert decision.code == CHECK_ERROR
    assert "eval" in decision.reason


@pytest.mark.parametrize(
    ("safe_code", "expected"),
    [
        (SAFE_FILES_ALLOWED, AUTO_MERGE_ALLOWED),
        (SAFE_FILES_MANUAL, MANUAL_MERGE_REQUIRED),
        (SAFE_FILES_ERROR, CHECK_ERROR),
    ],
)
def test_safe_auto_uses_the_deterministic_file_result(
    safe_code: str,
    expected: str,
) -> None:
    decision = decide_merge(
        _context(merge_mode=MergeMode.SAFE_AUTO, safe_code=safe_code)
    )

    assert decision.code == expected


def test_full_auto_ignores_file_risk_only_not_common_checks() -> None:
    decision = decide_merge(
        _context(merge_mode=MergeMode.FULL_AUTO, safe_code=SAFE_FILES_ERROR)
    )

    assert decision.code == AUTO_MERGE_ALLOWED


def test_full_auto_does_not_require_safe_file_evidence() -> None:
    decision = decide_merge(
        _context(merge_mode=MergeMode.FULL_AUTO, safe_files=None)
    )

    assert decision.code == AUTO_MERGE_ALLOWED


@pytest.mark.parametrize(
    "changes",
    [
        {"repository": "other/repository"},
        {"issue_number": 8},
        {"task_content_hash": "b" * 64},
    ],
)
def test_settings_and_content_mismatch_stop_automatic_merge(
    changes: dict[str, object],
) -> None:
    assert decide_merge(_context(**changes)).code == CHECK_ERROR


def test_task_flow_must_match_settings_hash_flow_and_pull_request() -> None:
    context = _context()
    different_flow = replace(
        context.task_flow_state,
        task_flow=TaskFlow.BUILD,
    )
    different_hash = replace(
        context.task_flow_state,
        task_settings_hash="b" * 64,
    )
    different_pr = replace(
        context.task_flow_state,
        pr_url="https://github.com/openai/infinity-forge/pull/10",
    )

    assert (
        decide_merge(replace(context, task_flow_state=different_flow)).code
        == CHECK_ERROR
    )
    assert (
        decide_merge(replace(context, task_flow_state=different_hash)).code
        == CHECK_ERROR
    )
    assert (
        decide_merge(replace(context, task_flow_state=different_pr)).code
        == CHECK_ERROR
    )


@pytest.mark.parametrize(
    "flow_change",
    [
        {"status": TaskFlowStatus.RUNNING},
        {"completed_steps": (TaskStep.BUILD,)},
        {
            "completed_steps": (
                TaskStep.BUILD,
                TaskStep.REVIEW,
                TaskStep.REVIEW,
            )
        },
        {"current_step": TaskStep.BUILD},
        {"step_running": True},
    ],
)
def test_only_an_exact_ready_to_merge_flow_can_merge(
    flow_change: dict[str, object],
) -> None:
    context = _context()
    incomplete = replace(context.task_flow_state, **flow_change)

    assert (
        decide_merge(replace(context, task_flow_state=incomplete)).code
        == CHECK_ERROR
    )


def test_changed_pr_commit_restarts_the_selected_flow() -> None:
    pr = replace(
        _context().pull_request,
        head_commit="b" * 40,
        eval_commit="b" * 40,
    )

    decision = decide_merge(_context(pull_request=pr))

    assert decision.code == RESTART_FLOW


def test_changed_pr_base_restarts_the_selected_flow() -> None:
    pr = replace(_context().pull_request, base_commit="b" * 40)

    decision = decide_merge(_context(pull_request=pr))

    assert decision.code == RESTART_FLOW


@pytest.mark.parametrize(
    "evidence",
    [
        _safe_evidence(base_commit="b" * 40),
        _safe_evidence(head_commit="b" * 40),
        None,
    ],
)
def test_safe_auto_requires_file_evidence_for_the_exact_base_and_head(
    evidence: SafeFilesEvidence | None,
) -> None:
    decision = decide_merge(
        _context(
            merge_mode=MergeMode.SAFE_AUTO,
            safe_files=evidence,
        )
    )

    assert decision.code == CHECK_ERROR


def test_pending_eval_waits_but_duplicate_or_wrong_commit_is_an_error() -> None:
    pending = replace(_context().pull_request, eval_status="in_progress")
    duplicate = replace(_context().pull_request, eval_check_count=2)
    wrong_commit = replace(_context().pull_request, eval_commit="b" * 40)

    assert decide_merge(_context(pull_request=pending)).code == WAIT
    assert decide_merge(_context(pull_request=duplicate)).code == CHECK_ERROR
    assert decide_merge(_context(pull_request=wrong_commit)).code == CHECK_ERROR


def test_expired_permission_and_conflict_fall_back_to_a_person() -> None:
    base_context = _context()
    expired_settings = replace(
        base_context.settings,
        auto_merge_expires_at=NOW,
    )
    assert expired_settings.task_settings_hash is not None
    expired_flow = replace(
        base_context.task_flow_state,
        task_settings_hash=expired_settings.task_settings_hash,
    )
    conflict = replace(base_context.pull_request, has_conflict=True)

    assert (
        decide_merge(
            replace(
                base_context,
                settings=expired_settings,
                task_flow_state=expired_flow,
            )
        ).code
        == MANUAL_MERGE_REQUIRED
    )
    assert (
        decide_merge(_context(pull_request=conflict)).code
        == MANUAL_MERGE_REQUIRED
    )


def test_auto_mode_refreshes_an_outdated_branch_at_most_three_times() -> None:
    behind = replace(_context().pull_request, base_is_current=False)
    behind_and_blocked_by_rules = replace(
        behind,
        rules_allow_merge=False,
    )

    assert decide_merge(_context(pull_request=behind)).code == REFRESH_BRANCH
    assert (
        decide_merge(_context(pull_request=behind_and_blocked_by_rules)).code
        == REFRESH_BRANCH
    )
    assert (
        decide_merge(_context(pull_request=behind, branch_refresh_count=3)).code
        == MANUAL_MERGE_REQUIRED
    )
    assert (
        decide_merge(
            _context(
                merge_mode=MergeMode.MANUAL,
                pull_request=behind,
            )
        ).code
        == MANUAL_MERGE_REQUIRED
    )


@pytest.mark.parametrize(
    "displayed_status",
    [
        "forge:waiting-for-help",
        "forge:failed",
        "forge:needs-decision",
    ],
)
def test_blocked_task_status_never_auto_merges(displayed_status: str) -> None:
    assert (
        decide_merge(_context(displayed_status=displayed_status)).code
        == CHECK_ERROR
    )


def test_draft_closed_rules_block_and_unresolved_threads_require_attention() -> None:
    base = _context().pull_request

    assert decide_merge(_context(pull_request=replace(base, is_draft=True))).code == MANUAL_MERGE_REQUIRED
    assert decide_merge(_context(pull_request=replace(base, is_open=False))).code == MANUAL_MERGE_REQUIRED
    assert decide_merge(_context(pull_request=replace(base, rules_allow_merge=False))).code == MANUAL_MERGE_REQUIRED
    assert decide_merge(
        _context(pull_request=replace(base, unresolved_review_threads=1))
    ).code == MANUAL_MERGE_REQUIRED


def test_same_commit_already_merged_is_idempotent() -> None:
    merged = replace(
        _context().pull_request,
        is_open=False,
        is_merged=True,
        merged_commit=MERGE_COMMIT,
        merged_base_commit=BASE,
        merged_head_commit=HEAD,
    )

    decision = decide_merge(_context(pull_request=merged))

    assert decision.code == AUTO_MERGE_ALLOWED
    assert decision.already_merged is True


def test_already_merged_requires_the_recorded_head_not_merge_result_commit() -> None:
    wrong_head = replace(
        _context().pull_request,
        is_open=False,
        is_merged=True,
        merged_commit=MERGE_COMMIT,
        merged_base_commit=BASE,
        merged_head_commit="b" * 40,
    )

    decision = decide_merge(_context(pull_request=wrong_head))

    assert decision.code == CHECK_ERROR
    assert "merged head" in decision.reason


def test_already_merged_requires_the_recorded_base_commit() -> None:
    wrong_base = replace(
        _context().pull_request,
        is_open=False,
        is_merged=True,
        merged_commit=MERGE_COMMIT,
        merged_base_commit="b" * 40,
        merged_head_commit=HEAD,
    )

    decision = decide_merge(_context(pull_request=wrong_base))

    assert decision.code == CHECK_ERROR
    assert "merged base" in decision.reason


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("is_open", "yes"),
        ("is_draft", 0),
        ("is_merged", None),
        ("has_conflict", []),
        ("base_is_current", "true"),
        ("rules_allow_merge", 1),
        ("eval_status", []),
    ],
)
def test_malformed_pull_request_values_return_check_error(
    field: str,
    value: object,
) -> None:
    malformed = replace(_context().pull_request, **{field: value})

    assert decide_merge(_context(pull_request=malformed)).code == CHECK_ERROR
