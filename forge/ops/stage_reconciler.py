"""Pure decisions for advancing one Forge pipeline stage."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum

from .contracts import (
    CheckRun,
    ContractError,
    CriticResult,
    ExecutorResult,
    PipelineStage,
    PullRequestSnapshot,
    ReviewerResult,
    RunRecord,
    StageOutcome,
    StageResult,
    TaskRecord,
    validate_stage_result_binding,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ActionKind(str, Enum):
    WAIT = "wait"
    CREATE_REVIEWER = "create-reviewer"
    CREATE_CRITIC = "create-critic"
    CREATE_REWORK = "create-rework"
    MARK_MERGEABLE = "mark-mergeable"
    MARK_FAILED = "mark-failed"
    GATE_ERROR = "gate-error"


@dataclass(frozen=True)
class PipelineSnapshot:
    """Complete, already-fetched evidence for one side-effect-free decision."""

    stage: PipelineStage
    source_task: TaskRecord
    source_run: RunRecord
    result: StageResult
    source_digest: str
    pull_request: PullRequestSnapshot
    bound_source_digest: str | None = None
    bound_pr_url: str | None = None
    bound_head_sha: str | None = None
    rework_count: int = 0
    max_reworks: int = 3
    required_check_name: str = "eval"


@dataclass(frozen=True)
class StageAction:
    kind: ActionKind
    target_stage: PipelineStage | None = None
    reason: str = ""
    reflection: str | None = None


@dataclass(frozen=True)
class StageCardSpec:
    """Deterministic arguments for one Hermes stage-card creation."""

    target_stage: PipelineStage
    title: str
    body: str
    parent_id: str
    assignee: str
    skill: str
    idempotency_key: str


def _gate_error(reason: str) -> StageAction:
    return StageAction(ActionKind.GATE_ERROR, reason=reason)


def _wait(reason: str) -> StageAction:
    return StageAction(ActionKind.WAIT, reason=reason)


def _creation_action(
    kind: ActionKind,
    target_stage: PipelineStage,
    *,
    reason: str,
    reflection: str | None = None,
) -> StageAction:
    return StageAction(
        kind=kind,
        target_stage=target_stage,
        reason=reason,
        reflection=reflection,
    )


def _validate_snapshot_shape(snapshot: PipelineSnapshot) -> StageAction | None:
    if not isinstance(snapshot.stage, PipelineStage):
        return _gate_error("stage is not a PipelineStage")
    if not isinstance(snapshot.source_task, TaskRecord):
        return _gate_error("source task is malformed")
    if not isinstance(snapshot.source_run, RunRecord):
        return _gate_error("source run is malformed")
    if snapshot.source_run.task_id != snapshot.source_task.task_id:
        return _gate_error("source run is bound to a different task")
    if not isinstance(snapshot.source_digest, str) or _SHA256_RE.fullmatch(
        snapshot.source_digest
    ) is None:
        return _gate_error("source digest has an invalid format")
    if not isinstance(snapshot.pull_request, PullRequestSnapshot):
        return _gate_error("PR snapshot is malformed")
    if _GIT_SHA_RE.fullmatch(snapshot.pull_request.head_sha) is None:
        return _gate_error("live PR head has an invalid format")
    if (
        not isinstance(snapshot.rework_count, int)
        or isinstance(snapshot.rework_count, bool)
        or snapshot.rework_count < 0
    ):
        return _gate_error("rework count must be a non-negative integer")
    if (
        not isinstance(snapshot.max_reworks, int)
        or isinstance(snapshot.max_reworks, bool)
        or snapshot.max_reworks < 1
    ):
        return _gate_error("max reworks must be a positive integer")
    if (
        not isinstance(snapshot.required_check_name, str)
        or not snapshot.required_check_name.strip()
    ):
        return _gate_error("required check name must be non-empty")
    return None


def _validate_result_binding(snapshot: PipelineSnapshot) -> StageAction | None:
    result = snapshot.result
    pr = snapshot.pull_request

    expected_type: type[ExecutorResult] | type[ReviewerResult] | type[CriticResult]
    if snapshot.stage in {PipelineStage.EXECUTOR, PipelineStage.EXECUTOR_REWORK}:
        expected_type = ExecutorResult
    elif snapshot.stage is PipelineStage.REVIEWER:
        expected_type = ReviewerResult
    else:
        expected_type = CriticResult
    if not isinstance(result, expected_type):
        return _gate_error("stage result type does not match the source stage")

    if result.pr_url != pr.pr_url:
        return _gate_error("stage result PR does not match the live PR")

    try:
        if isinstance(result, ExecutorResult):
            validate_stage_result_binding(
                result,
                expected_repository=pr.repository,
            )
            return None

        if (
            snapshot.bound_source_digest is None
            or snapshot.bound_pr_url is None
            or snapshot.bound_head_sha is None
        ):
            return _gate_error(
                "stage result is missing its source, PR, or head binding"
            )
        if snapshot.bound_pr_url != pr.pr_url:
            return _gate_error("bound PR does not match the live PR")

        validate_stage_result_binding(
            result,
            expected_repository=pr.repository,
            expected_pr_url=snapshot.bound_pr_url,
            expected_source_digest=snapshot.bound_source_digest,
            expected_head_sha=snapshot.bound_head_sha,
        )
    except ContractError as error:
        return _gate_error(f"stale or malformed stage binding: {error}")

    if isinstance(result, ReviewerResult):
        if result.head_sha != pr.head_sha:
            return _gate_error("reviewer head does not match the live PR head")
    elif result.result_head_sha != pr.head_sha:
        return _gate_error("critic result head does not match the live PR head")
    return None


def _required_check_decision(
    snapshot: PipelineSnapshot,
    *,
    expected_head_sha: str,
) -> StageAction | None:
    matches = tuple(
        check
        for check in snapshot.pull_request.checks
        if isinstance(check, CheckRun) and check.name == snapshot.required_check_name
    )
    if not matches:
        return _gate_error(
            f"missing required check: {snapshot.required_check_name}"
        )
    if len(matches) > 1:
        return _gate_error(
            f"duplicate required check: {snapshot.required_check_name}"
        )

    check = matches[0]
    if check.head_sha != expected_head_sha:
        return _gate_error("required check head does not match the required PR head")
    if check.status == "completed" and check.conclusion == "success":
        return None
    return _wait(
        f"required check is not successful: status={check.status}, "
        f"conclusion={check.conclusion}"
    )


def _rework_or_failure(snapshot: PipelineSnapshot, reflection: str | None) -> StageAction:
    if not isinstance(reflection, str) or not reflection.strip():
        return _gate_error("rework result requires a non-empty reflection")
    if snapshot.rework_count >= snapshot.max_reworks:
        return StageAction(
            ActionKind.MARK_FAILED,
            reason="maximum rework count reached",
            reflection=reflection,
        )
    return _creation_action(
        ActionKind.CREATE_REWORK,
        PipelineStage.EXECUTOR_REWORK,
        reason="stage result requires executor rework",
        reflection=reflection,
    )


def decide_next_action(snapshot: PipelineSnapshot) -> StageAction:
    """Return the deterministic next action without performing external I/O."""

    if not isinstance(snapshot, PipelineSnapshot):
        return _gate_error("pipeline snapshot is malformed")

    invalid = _validate_snapshot_shape(snapshot)
    if invalid is not None:
        return invalid
    invalid = _validate_result_binding(snapshot)
    if invalid is not None:
        return invalid

    pr = snapshot.pull_request
    if not pr.is_open or pr.is_draft:
        return _wait("PR must be open and non-draft")

    result = snapshot.result
    if isinstance(result, ExecutorResult):
        check_decision = _required_check_decision(
            snapshot,
            expected_head_sha=pr.head_sha,
        )
        if check_decision is not None:
            return check_decision
        return _creation_action(
            ActionKind.CREATE_REVIEWER,
            PipelineStage.REVIEWER,
            reason="executor result and required check are green",
        )

    if isinstance(result, ReviewerResult):
        if result.verdict is StageOutcome.REJECT:
            return _rework_or_failure(snapshot, result.reflection)
        if result.verdict is not StageOutcome.APPROVE:
            return _gate_error("reviewer verdict is unsupported")
        return _creation_action(
            ActionKind.CREATE_CRITIC,
            PipelineStage.CRITIC,
            reason="reviewer approved the bound PR head",
        )

    if result.outcome is StageOutcome.DEFECT_FOUND:
        return _rework_or_failure(snapshot, result.reflection)
    if result.outcome is not StageOutcome.PASS:
        return _gate_error("critic outcome is unsupported")
    check_decision = _required_check_decision(
        snapshot,
        expected_head_sha=result.result_head_sha,
    )
    if check_decision is not None:
        return check_decision
    return StageAction(
        ActionKind.MARK_MERGEABLE,
        reason="critic passed and the result head required check is green",
    )


_CARD_TARGETS = {
    ActionKind.CREATE_REVIEWER: (
        PipelineStage.REVIEWER,
        "reviewer",
        "reviewer-verdict",
    ),
    ActionKind.CREATE_CRITIC: (
        PipelineStage.CRITIC,
        "critic",
        "critic-adversarial",
    ),
    ActionKind.CREATE_REWORK: (
        PipelineStage.EXECUTOR_REWORK,
        "executor",
        "kanban-codex-delegate",
    ),
}


def build_stage_card_spec(
    snapshot: PipelineSnapshot,
    action: StageAction,
) -> StageCardSpec:
    """Build a replay-stable child card for a creation action."""

    if not isinstance(action, StageAction) or action.kind not in _CARD_TARGETS:
        raise ValueError("stage card requires a creation action")

    expected_action = decide_next_action(snapshot)
    if action != expected_action:
        raise ValueError("creation action does not match the pipeline snapshot")

    target_stage, assignee, skill = _CARD_TARGETS[action.kind]
    if action.target_stage is not target_stage:
        raise ValueError("creation action has an invalid target stage")

    pr = snapshot.pull_request
    payload = {
        "bound_head_sha": pr.head_sha,
        "pr_url": pr.pr_url,
        "reflection": action.reflection,
        "source_digest": snapshot.source_digest,
        "source_run_id": snapshot.source_run.run_id,
        "source_task_id": snapshot.source_task.task_id,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return StageCardSpec(
        target_stage=target_stage,
        title=f"Forge {target_stage.value}: {pr.repository}#{pr.issue_number}",
        body=f"```json\n{canonical}\n```",
        parent_id=snapshot.source_task.task_id,
        assignee=assignee,
        skill=skill,
        idempotency_key=(
            f"forge-stage:{pr.repository}#{pr.issue_number}:"
            f"{target_stage.value}:{snapshot.source_digest[:16]}"
        ),
    )
