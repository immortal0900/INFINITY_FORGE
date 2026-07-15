from __future__ import annotations

import json
from dataclasses import replace

import pytest

from forge.ops.contracts import (
    CheckRun,
    CriticResult,
    ExecutorResult,
    PipelineStage,
    PullRequestSnapshot,
    ReviewerResult,
    RunRecord,
    StageOutcome,
    TaskRecord,
)
from forge.ops.stage_reconciler import (
    ActionKind,
    PipelineSnapshot,
    StageCardSpec,
    build_stage_card_spec,
    decide_next_action,
)


REPOSITORY = "owner/repo"
ISSUE_NUMBER = 7
PR_URL = "https://github.com/owner/repo/pull/17"
OTHER_PR_URL = "https://github.com/owner/repo/pull/18"
BOUND_SOURCE_DIGEST = "a" * 64
SOURCE_DIGEST = "b" * 64
BOUND_HEAD_SHA = "c" * 40
LIVE_HEAD_SHA = "d" * 40


def _check(
    *,
    conclusion: str | None = "success",
    status: str = "completed",
    head_sha: str = LIVE_HEAD_SHA,
    name: str = "eval",
) -> CheckRun:
    return CheckRun(
        name=name,
        status=status,
        conclusion=conclusion,
        head_sha=head_sha,
    )


def _source_records(stage: PipelineStage) -> tuple[TaskRecord, RunRecord]:
    if stage is PipelineStage.EXECUTOR:
        idempotency_key = f"github-issue:{REPOSITORY}#{ISSUE_NUMBER}"
    else:
        idempotency_key = (
            f"forge-stage:{REPOSITORY}#{ISSUE_NUMBER}:"
            f"{stage.value}:{BOUND_SOURCE_DIGEST[:16]}"
        )
    task = TaskRecord(
        task_id=f"t_{stage.value}",
        title=f"Forge {stage.value}",
        status="done",
        idempotency_key=idempotency_key,
    )
    run = RunRecord(
        run_id=12,
        task_id=task.task_id,
        status="completed",
        outcome="success",
        summary={},
        metadata={},
    )
    return task, run


def _pull_request(
    *,
    live_head: str = LIVE_HEAD_SHA,
    checks: tuple[CheckRun, ...] | None = None,
    is_open: bool = True,
    is_draft: bool = False,
) -> PullRequestSnapshot:
    if checks is None:
        checks = (_check(head_sha=live_head),)
    return PullRequestSnapshot(
        pr_url=PR_URL,
        repository=REPOSITORY,
        pr_number=17,
        head_sha=live_head,
        is_open=is_open,
        is_draft=is_draft,
        checks=checks,
    )


def executor_snapshot(
    *,
    stage: PipelineStage = PipelineStage.EXECUTOR,
    check_conclusion: str | None = "success",
    check_status: str = "completed",
    checks: tuple[CheckRun, ...] | None = None,
    result_pr_url: str = PR_URL,
    is_open: bool = True,
    is_draft: bool = False,
) -> PipelineSnapshot:
    task, run = _source_records(stage)
    if checks is None:
        checks = (
            _check(conclusion=check_conclusion, status=check_status),
        )
    return PipelineSnapshot(
        stage=stage,
        issue_number=ISSUE_NUMBER,
        source_task=task,
        source_run=run,
        result=ExecutorResult(
            pr_url=result_pr_url,
            changed_files=("forge/ops/stage_reconciler.py",),
            implemented=("AC1",),
            not_implemented=(),
            verified_by={"AC1": "tests/ops/test_stage_reconciler.py"},
        ),
        source_digest=SOURCE_DIGEST,
        pull_request=_pull_request(
            checks=checks,
            is_open=is_open,
            is_draft=is_draft,
        ),
        bound_source_digest=(
            BOUND_SOURCE_DIGEST
            if stage is PipelineStage.EXECUTOR_REWORK
            else None
        ),
        bound_pr_url=(PR_URL if stage is PipelineStage.EXECUTOR_REWORK else None),
        rework_count=1 if stage is PipelineStage.EXECUTOR_REWORK else 0,
    )


def reviewer_snapshot(
    *,
    verdict: StageOutcome = StageOutcome.APPROVE,
    reflection: str | None = None,
    result_source_digest: str = BOUND_SOURCE_DIGEST,
    bound_source_digest: str = BOUND_SOURCE_DIGEST,
    result_pr_url: str = PR_URL,
    bound_pr_url: str = PR_URL,
    result_head: str = LIVE_HEAD_SHA,
    bound_head: str = LIVE_HEAD_SHA,
    live_head: str = LIVE_HEAD_SHA,
    rework_count: int = 0,
) -> PipelineSnapshot:
    task, run = _source_records(PipelineStage.REVIEWER)
    return PipelineSnapshot(
        stage=PipelineStage.REVIEWER,
        issue_number=ISSUE_NUMBER,
        source_task=task,
        source_run=run,
        result=ReviewerResult(
            schema_version="forge-reviewer-result/v1",
            verdict=verdict,
            source_digest=result_source_digest,
            pr_url=result_pr_url,
            head_sha=result_head,
            delta_check={"implemented_verified": ("AC1",), "discrepancies": ()},
            spec_check={"met": ("AC1",), "unmet": ()},
            reflection=reflection,
        ),
        source_digest=SOURCE_DIGEST,
        pull_request=_pull_request(live_head=live_head),
        bound_source_digest=bound_source_digest,
        bound_pr_url=bound_pr_url,
        bound_head_sha=bound_head,
        rework_count=rework_count,
    )


def critic_snapshot(
    *,
    outcome: StageOutcome = StageOutcome.PASS,
    reflection: str | None = None,
    result_source_digest: str = BOUND_SOURCE_DIGEST,
    bound_source_digest: str = BOUND_SOURCE_DIGEST,
    result_pr_url: str = PR_URL,
    bound_pr_url: str = PR_URL,
    reviewed_head: str = BOUND_HEAD_SHA,
    bound_head: str = BOUND_HEAD_SHA,
    result_head: str = LIVE_HEAD_SHA,
    live_head: str = LIVE_HEAD_SHA,
    check_conclusion: str | None = "success",
    check_status: str = "completed",
    checks: tuple[CheckRun, ...] | None = None,
    rework_count: int = 0,
) -> PipelineSnapshot:
    task, run = _source_records(PipelineStage.CRITIC)
    if checks is None:
        checks = (
            _check(
                conclusion=check_conclusion,
                status=check_status,
                head_sha=live_head,
            ),
        )
    return PipelineSnapshot(
        stage=PipelineStage.CRITIC,
        issue_number=ISSUE_NUMBER,
        source_task=task,
        source_run=run,
        result=CriticResult(
            schema_version="forge-critic-result/v1",
            outcome=outcome,
            source_digest=result_source_digest,
            pr_url=result_pr_url,
            reviewed_head_sha=reviewed_head,
            result_head_sha=result_head,
            added_tests=("tests/ops/test_stage_reconciler.py",),
            scenarios=("required check binding",),
            reflection=reflection,
        ),
        source_digest=SOURCE_DIGEST,
        pull_request=_pull_request(live_head=live_head, checks=checks),
        bound_source_digest=bound_source_digest,
        bound_pr_url=bound_pr_url,
        bound_head_sha=bound_head,
        rework_count=rework_count,
    )


@pytest.mark.parametrize(
    "stage",
    [PipelineStage.EXECUTOR, PipelineStage.EXECUTOR_REWORK],
)
def test_green_executor_creates_reviewer_once(stage: PipelineStage) -> None:
    action = decide_next_action(executor_snapshot(stage=stage))

    assert action.kind is ActionKind.CREATE_REVIEWER
    assert action.target_stage is PipelineStage.REVIEWER


@pytest.mark.parametrize(
    ("checks", "reason"),
    [
        ((), "missing"),
        ((_check(), _check()), "duplicate"),
        ((_check(name="Eval"),), "missing"),
    ],
)
def test_required_check_must_exist_exactly_once(
    checks: tuple[CheckRun, ...],
    reason: str,
) -> None:
    action = decide_next_action(executor_snapshot(checks=checks))

    assert action.kind is ActionKind.GATE_ERROR
    assert reason in action.reason


@pytest.mark.parametrize(
    ("status", "conclusion"),
    [
        ("queued", None),
        ("in_progress", None),
        ("completed", "pending"),
        ("completed", "failure"),
        ("completed", "cancelled"),
    ],
)
def test_non_green_required_check_waits(
    status: str,
    conclusion: str | None,
) -> None:
    action = decide_next_action(
        executor_snapshot(check_status=status, check_conclusion=conclusion)
    )

    assert action.kind is ActionKind.WAIT


def test_required_check_from_stale_head_is_gate_error() -> None:
    action = decide_next_action(
        executor_snapshot(checks=(_check(head_sha=BOUND_HEAD_SHA),))
    )

    assert action.kind is ActionKind.GATE_ERROR
    assert "head" in action.reason


@pytest.mark.parametrize(
    "snapshot",
    [
        executor_snapshot(is_open=False),
        executor_snapshot(is_draft=True),
    ],
)
def test_executor_waits_until_pr_is_open_and_non_draft(
    snapshot: PipelineSnapshot,
) -> None:
    assert decide_next_action(snapshot).kind is ActionKind.WAIT


def test_executor_result_pr_must_match_live_pr() -> None:
    action = decide_next_action(executor_snapshot(result_pr_url=OTHER_PR_URL))

    assert action.kind is ActionKind.GATE_ERROR
    assert "PR" in action.reason


def test_rework_result_cannot_switch_to_another_pr_in_same_repository() -> None:
    snapshot = executor_snapshot(stage=PipelineStage.EXECUTOR_REWORK)
    switched = replace(
        snapshot,
        result=replace(snapshot.result, pr_url=OTHER_PR_URL),
        pull_request=replace(
            snapshot.pull_request,
            pr_url=OTHER_PR_URL,
            pr_number=18,
        ),
    )

    action = decide_next_action(switched)

    assert action.kind is ActionKind.GATE_ERROR
    assert "bound PR" in action.reason


def test_reviewer_approve_creates_critic() -> None:
    action = decide_next_action(reviewer_snapshot())

    assert action.kind is ActionKind.CREATE_CRITIC
    assert action.target_stage is PipelineStage.CRITIC


def test_reviewer_reject_creates_rework_and_never_critic() -> None:
    action = decide_next_action(
        reviewer_snapshot(
            verdict=StageOutcome.REJECT,
            reflection="AC2 누락",
        )
    )

    assert action.kind is ActionKind.CREATE_REWORK
    assert action.target_stage is PipelineStage.EXECUTOR_REWORK
    assert action.reflection == "AC2 누락"


def test_reviewer_reject_without_reflection_is_gate_error() -> None:
    action = decide_next_action(reviewer_snapshot(verdict=StageOutcome.REJECT))

    assert action.kind is ActionKind.GATE_ERROR
    assert "reflection" in action.reason


def test_reviewer_fourth_rejection_marks_pipeline_failed() -> None:
    action = decide_next_action(
        reviewer_snapshot(
            verdict=StageOutcome.REJECT,
            reflection="여전히 AC2 누락",
            rework_count=3,
        )
    )

    assert action.kind is ActionKind.MARK_FAILED
    assert action.target_stage is None


@pytest.mark.parametrize(
    "snapshot",
    [
        reviewer_snapshot(bound_source_digest="e" * 64),
        reviewer_snapshot(bound_pr_url=OTHER_PR_URL),
        reviewer_snapshot(bound_head=BOUND_HEAD_SHA),
        reviewer_snapshot(result_head=BOUND_HEAD_SHA),
        reviewer_snapshot(live_head=BOUND_HEAD_SHA),
    ],
)
def test_stale_reviewer_binding_is_gate_error(snapshot: PipelineSnapshot) -> None:
    action = decide_next_action(snapshot)

    assert action.kind is ActionKind.GATE_ERROR


def test_critic_defect_creates_rework() -> None:
    action = decide_next_action(
        critic_snapshot(
            outcome=StageOutcome.DEFECT_FOUND,
            reflection="재시도 race 재현",
        )
    )

    assert action.kind is ActionKind.CREATE_REWORK
    assert action.target_stage is PipelineStage.EXECUTOR_REWORK
    assert action.reflection == "재시도 race 재현"


def test_critic_fourth_defect_marks_pipeline_failed() -> None:
    action = decide_next_action(
        critic_snapshot(
            outcome=StageOutcome.DEFECT_FOUND,
            reflection="결함이 남음",
            rework_count=3,
        )
    )

    assert action.kind is ActionKind.MARK_FAILED


@pytest.mark.parametrize(
    ("status", "conclusion"),
    [
        ("in_progress", None),
        ("completed", "pending"),
        ("completed", "failure"),
    ],
)
def test_critic_pass_needs_green_result_head(
    status: str,
    conclusion: str | None,
) -> None:
    action = decide_next_action(
        critic_snapshot(check_status=status, check_conclusion=conclusion)
    )

    assert action.kind is ActionKind.WAIT


def test_green_critic_pass_marks_pipeline_mergeable() -> None:
    action = decide_next_action(critic_snapshot())

    assert action.kind is ActionKind.MARK_MERGEABLE


def test_critic_pass_requires_a_new_result_commit() -> None:
    unchanged = critic_snapshot(
        reviewed_head=BOUND_HEAD_SHA,
        bound_head=BOUND_HEAD_SHA,
        result_head=BOUND_HEAD_SHA,
        live_head=BOUND_HEAD_SHA,
    )

    action = decide_next_action(unchanged)

    assert action.kind is ActionKind.GATE_ERROR
    assert "result HEAD" in action.reason


@pytest.mark.parametrize(
    "snapshot",
    [
        critic_snapshot(bound_source_digest="e" * 64),
        critic_snapshot(bound_pr_url=OTHER_PR_URL),
        critic_snapshot(bound_head="e" * 40),
        critic_snapshot(reviewed_head="e" * 40),
        critic_snapshot(result_head="e" * 40),
        critic_snapshot(live_head="e" * 40),
        critic_snapshot(checks=(_check(head_sha=BOUND_HEAD_SHA),)),
    ],
)
def test_stale_critic_binding_is_gate_error(snapshot: PipelineSnapshot) -> None:
    action = decide_next_action(snapshot)

    assert action.kind is ActionKind.GATE_ERROR


def test_stage_result_type_mismatch_is_gate_error() -> None:
    snapshot = executor_snapshot()
    mismatched = replace(snapshot, stage=PipelineStage.REVIEWER)

    action = decide_next_action(mismatched)

    assert action.kind is ActionKind.GATE_ERROR


@pytest.mark.parametrize(
    ("task_status", "run_status", "run_outcome"),
    [
        ("running", "completed", "success"),
        ("done", "running", None),
        ("done", "completed", "failure"),
    ],
)
def test_source_must_be_a_successfully_completed_run(
    task_status: str,
    run_status: str,
    run_outcome: str | None,
) -> None:
    snapshot = executor_snapshot()
    invalid = replace(
        snapshot,
        source_task=replace(snapshot.source_task, status=task_status),
        source_run=replace(
            snapshot.source_run,
            status=run_status,
            outcome=run_outcome,
        ),
    )

    action = decide_next_action(invalid)

    assert action.kind is ActionKind.GATE_ERROR
    assert "completed" in action.reason or "success" in action.reason


@pytest.mark.parametrize(
    "snapshot",
    [
        replace(executor_snapshot(), issue_number=17),
        replace(
            executor_snapshot(),
            source_task=replace(
                executor_snapshot().source_task,
                idempotency_key=f"github-issue:{REPOSITORY}#17",
            ),
        ),
        replace(
            reviewer_snapshot(),
            source_task=replace(
                reviewer_snapshot().source_task,
                idempotency_key=(
                    f"forge-stage:{REPOSITORY}#{ISSUE_NUMBER}:critic:"
                    f"{BOUND_SOURCE_DIGEST[:16]}"
                ),
            ),
        ),
        replace(
            reviewer_snapshot(),
            source_task=replace(
                reviewer_snapshot().source_task,
                idempotency_key=(
                    f"forge-stage:{REPOSITORY}#{ISSUE_NUMBER}:reviewer:"
                    f"{'e' * 16}"
                ),
            ),
        ),
        replace(
            executor_snapshot(stage=PipelineStage.EXECUTOR_REWORK),
            bound_source_digest="e" * 64,
        ),
    ],
)
def test_root_issue_identity_is_bound_to_source_task_key(
    snapshot: PipelineSnapshot,
) -> None:
    action = decide_next_action(snapshot)

    assert action.kind is ActionKind.GATE_ERROR
    assert "identity" in action.reason or "stage" in action.reason


def test_rework_limit_cannot_be_configured_above_three() -> None:
    snapshot = reviewer_snapshot(
        verdict=StageOutcome.REJECT,
        reflection="AC2 누락",
        rework_count=3,
    )

    action = decide_next_action(replace(snapshot, max_reworks=4))

    assert action.kind is ActionKind.GATE_ERROR
    assert "rework" in action.reason


@pytest.mark.parametrize(
    "checks",
    [
        (_check(status="unknown"),),
        (_check(status="in_progress", conclusion="success"),),
        (_check(conclusion="mystery"),),
        (_check(), object()),
    ],
)
def test_malformed_check_evidence_is_gate_error(
    checks: tuple[object, ...],
) -> None:
    snapshot = executor_snapshot()
    invalid = replace(
        snapshot,
        pull_request=replace(snapshot.pull_request, checks=checks),
    )

    action = decide_next_action(invalid)

    assert action.kind is ActionKind.GATE_ERROR
    assert "check" in action.reason


def test_decision_is_deterministic_for_same_snapshot() -> None:
    snapshot = reviewer_snapshot(
        verdict=StageOutcome.REJECT,
        reflection="AC2 누락",
    )

    assert decide_next_action(snapshot) == decide_next_action(snapshot)


@pytest.mark.parametrize(
    ("snapshot", "expected_stage", "expected_assignee", "expected_skill"),
    [
        (
            executor_snapshot(),
            PipelineStage.REVIEWER,
            "reviewer",
            "reviewer-verdict",
        ),
        (
            reviewer_snapshot(),
            PipelineStage.CRITIC,
            "critic",
            "critic-adversarial",
        ),
        (
            reviewer_snapshot(
                verdict=StageOutcome.REJECT,
                reflection="AC2 누락",
            ),
            PipelineStage.EXECUTOR_REWORK,
            "executor",
            "kanban-codex-delegate",
        ),
    ],
)
def test_stage_card_spec_binds_parent_skill_and_deterministic_key(
    snapshot: PipelineSnapshot,
    expected_stage: PipelineStage,
    expected_assignee: str,
    expected_skill: str,
) -> None:
    action = decide_next_action(snapshot)

    spec = build_stage_card_spec(snapshot, action)

    assert isinstance(spec, StageCardSpec)
    assert spec.target_stage is expected_stage
    assert spec.parent_id == snapshot.source_task.task_id
    assert spec.assignee == expected_assignee
    assert spec.skill == expected_skill
    assert spec.idempotency_key == (
        f"forge-stage:{REPOSITORY}#{ISSUE_NUMBER}:"
        f"{expected_stage.value}:{SOURCE_DIGEST[:16]}"
    )


def test_stage_card_body_is_exact_canonical_json_block() -> None:
    snapshot = reviewer_snapshot(
        verdict=StageOutcome.REJECT,
        reflection="AC2 누락",
    )
    action = decide_next_action(snapshot)
    expected_payload = {
        "bound_head_sha": LIVE_HEAD_SHA,
        "pr_url": PR_URL,
        "reflection": "AC2 누락",
        "source_digest": SOURCE_DIGEST,
        "source_run_id": 12,
        "source_task_id": snapshot.source_task.task_id,
    }
    canonical = json.dumps(
        expected_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    spec = build_stage_card_spec(snapshot, action)

    assert spec.body == f"```json\n{canonical}\n```"


def test_card_spec_is_identical_when_same_receipt_is_replayed() -> None:
    snapshot = executor_snapshot()
    action = decide_next_action(snapshot)

    first = build_stage_card_spec(snapshot, action)
    replay = build_stage_card_spec(snapshot, decide_next_action(snapshot))

    assert first == replay


@pytest.mark.parametrize(
    "action",
    [
        decide_next_action(executor_snapshot(check_conclusion="pending")),
        decide_next_action(
            reviewer_snapshot(
                verdict=StageOutcome.REJECT,
                reflection="AC2 누락",
                rework_count=3,
            )
        ),
        decide_next_action(critic_snapshot()),
    ],
)
def test_non_creation_action_cannot_build_stage_card(action: object) -> None:
    with pytest.raises(ValueError, match="creation"):
        build_stage_card_spec(executor_snapshot(), action)  # type: ignore[arg-type]
