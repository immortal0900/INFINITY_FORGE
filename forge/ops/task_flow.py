"""Deterministic Build, Review, Deep Check, and Fix Task transitions."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum

from .contracts import (
    BuildResult,
    DeepCheckDecision,
    DeepCheckResult,
    ReviewDecision,
    ReviewResult,
    StepProof,
    TaskResult,
    source_result_hash,
    validate_task_result_binding,
)
from .task_options import TaskFlow, TaskRole


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_GITHUB_PR_RE = re.compile(
    r"^https://github\.com/[^/]+/[^/]+/pull/[1-9][0-9]*$"
)
MAX_FIXES = 3


class TaskFlowError(ValueError):
    """Raised when evidence cannot make the requested Task transition."""


class TaskStep(str, Enum):
    BUILD = "build"
    REVIEW = "review"
    DEEP_CHECK = "deep_check"
    FIX = "fix"


class TaskFlowStatus(str, Enum):
    RUNNING = "running"
    READY_TO_MERGE = "ready_to_merge"
    FAILED = "failed"


@dataclass(frozen=True)
class TaskCardSpec:
    """Exact data needed to create one new Hermes step card."""

    step: TaskStep
    title: str
    body: str
    parent_id: str
    skill: str
    idempotency_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.step, TaskStep):
            raise TaskFlowError("step must be a TaskStep")
        for field in ("title", "body", "parent_id", "skill", "idempotency_key"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise TaskFlowError(f"{field} must be a non-empty string")

    @property
    def role(self) -> TaskRole:
        return role_for_step(self.step)


@dataclass(frozen=True)
class TaskFlowState:
    """Replay-safe state for one selected Task flow."""

    task_flow: TaskFlow
    task_settings_hash: str
    pr_url: str
    # RISK(breaking): every caller must now bind the flow to the observed base.
    current_base_commit: str
    current_commit: str
    current_step: TaskStep | None = TaskStep.BUILD
    status: TaskFlowStatus = TaskFlowStatus.RUNNING
    step_running: bool = False
    fix_count: int = 0
    completed_steps: tuple[TaskStep, ...] = ()
    expected_source_result_hash: str | None = None
    fix_notes: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.task_flow, TaskFlow):
            raise TaskFlowError("task_flow must be a TaskFlow")
        if _SHA256_RE.fullmatch(self.task_settings_hash) is None:
            raise TaskFlowError("task_settings_hash must be a lowercase SHA-256")
        if _GITHUB_PR_RE.fullmatch(self.pr_url) is None:
            raise TaskFlowError("pr_url must be a GitHub pull request URL")
        _require_commit(self.current_base_commit, field="current base commit")
        _require_commit(self.current_commit)
        if self.current_step is not None and not isinstance(
            self.current_step, TaskStep
        ):
            raise TaskFlowError("current_step must be a TaskStep or null")
        if not isinstance(self.status, TaskFlowStatus):
            raise TaskFlowError("status must be a TaskFlowStatus")
        if not isinstance(self.step_running, bool):
            raise TaskFlowError("step_running must be a boolean")
        if (
            not isinstance(self.fix_count, int)
            or isinstance(self.fix_count, bool)
            or not 0 <= self.fix_count <= MAX_FIXES
        ):
            raise TaskFlowError("fix_count must be between 0 and 3")
        if any(not isinstance(step, TaskStep) for step in self.completed_steps):
            raise TaskFlowError("completed_steps must contain TaskStep values")
        if (
            self.expected_source_result_hash is not None
            and _SHA256_RE.fullmatch(self.expected_source_result_hash) is None
        ):
            raise TaskFlowError(
                "expected_source_result_hash must be a lowercase SHA-256 or null"
            )
        if self.fix_notes is not None and (
            not isinstance(self.fix_notes, str) or not self.fix_notes.strip()
        ):
            raise TaskFlowError("fix_notes must be null or a non-empty string")


def _require_commit(commit: str, *, field: str = "current commit") -> None:
    if not isinstance(commit, str) or _GIT_SHA_RE.fullmatch(commit) is None:
        raise TaskFlowError(
            f"{field} must be a lowercase 40-character Git SHA"
        )


def required_steps(flow: TaskFlow) -> tuple[TaskStep, ...]:
    """Return only the steps selected by the user."""

    if not isinstance(flow, TaskFlow):
        raise TaskFlowError("flow must be a TaskFlow")
    return {
        TaskFlow.BUILD: (TaskStep.BUILD,),
        TaskFlow.BUILD_REVIEW: (TaskStep.BUILD, TaskStep.REVIEW),
        TaskFlow.BUILD_REVIEW_DEEP_CHECK: (
            TaskStep.BUILD,
            TaskStep.REVIEW,
            TaskStep.DEEP_CHECK,
        ),
    }[flow]


def role_for_step(step: TaskStep) -> TaskRole:
    """Map every official step to its one official Hermes role."""

    if not isinstance(step, TaskStep):
        raise TaskFlowError("step must be a TaskStep")
    return {
        TaskStep.BUILD: TaskRole.BUILDER,
        TaskStep.REVIEW: TaskRole.REVIEWER,
        TaskStep.DEEP_CHECK: TaskRole.DEEP_CHECKER,
        TaskStep.FIX: TaskRole.FIX,
    }[step]


def start_task_flow(
    flow: TaskFlow,
    *,
    task_settings_hash: str,
    pr_url: str,
    current_base_commit: str,
    current_commit: str,
) -> TaskFlowState:
    """Start an explicitly selected Task flow at Build."""

    return TaskFlowState(
        task_flow=flow,
        task_settings_hash=task_settings_hash,
        pr_url=pr_url,
        current_base_commit=current_base_commit,
        current_commit=current_commit,
    )


def next_task_action(state: TaskFlowState) -> TaskStep | None:
    """Return the current official step, or null after a terminal result."""

    _require_state(state)
    if state.status is not TaskFlowStatus.RUNNING:
        return None
    return state.current_step


def mark_task_step_running(state: TaskFlowState) -> TaskFlowState:
    """Mark the current step as claimed by a worker."""

    _require_state(state)
    if state.status is not TaskFlowStatus.RUNNING or state.current_step is None:
        raise TaskFlowError("Task has no step to start")
    if state.step_running:
        raise TaskFlowError("Task step is already running")
    return replace(state, step_running=True)


def record_task_result(
    state: TaskFlowState,
    result: TaskResult,
    *,
    current_commit: str,
) -> TaskFlowState:
    """Apply one exact worker result to the current Task step."""

    _require_state(state)
    _require_commit(current_commit)
    if state.status is not TaskFlowStatus.RUNNING or state.current_step is None:
        raise TaskFlowError("Task does not accept another result")
    if state.current_step is TaskStep.FIX:
        raise TaskFlowError("Fix requires a step proof, not a worker result")

    expected_type: type[BuildResult] | type[ReviewResult] | type[DeepCheckResult]
    if state.current_step is TaskStep.BUILD:
        expected_type = BuildResult
    elif state.current_step is TaskStep.REVIEW:
        expected_type = ReviewResult
    else:
        expected_type = DeepCheckResult
    if not isinstance(result, expected_type):
        raise TaskFlowError(
            f"{state.current_step.value} requires its matching result format"
        )
    if state.current_step is not TaskStep.DEEP_CHECK and (
        current_commit != state.current_commit
    ):
        raise TaskFlowError("current commit changed; restart from build")

    validate_task_result_binding(
        result,
        expected_task_settings_hash=state.task_settings_hash,
        expected_pr_url=state.pr_url,
        current_commit=current_commit,
    )
    if isinstance(result, BuildResult):
        return _record_build(state, result)
    if isinstance(result, ReviewResult):
        return _record_review(state, result)
    return _record_deep_check(state, result, current_commit=current_commit)


def _record_build(state: TaskFlowState, result: BuildResult) -> TaskFlowState:
    if result.remaining_items:
        raise TaskFlowError("remaining_items must be empty before Build completes")
    completed = (*state.completed_steps, TaskStep.BUILD)
    if state.task_flow is TaskFlow.BUILD:
        return replace(
            state,
            current_step=None,
            status=TaskFlowStatus.READY_TO_MERGE,
            step_running=False,
            completed_steps=completed,
            expected_source_result_hash=source_result_hash(result),
        )
    return replace(
        state,
        current_step=TaskStep.REVIEW,
        step_running=False,
        completed_steps=completed,
        expected_source_result_hash=source_result_hash(result),
    )


def _record_review(state: TaskFlowState, result: ReviewResult) -> TaskFlowState:
    _require_source_hash(state, result.source_result_hash)
    completed = (*state.completed_steps, TaskStep.REVIEW)
    result_hash = source_result_hash(result)
    if result.result is ReviewDecision.CHANGES_NEEDED:
        if result.fix_notes is None:
            raise TaskFlowError("changes_needed requires fix_notes")
        return _request_fix(
            state,
            source_hash=result_hash,
            fix_notes=result.fix_notes,
        )
    if state.task_flow is TaskFlow.BUILD_REVIEW:
        return replace(
            state,
            current_step=None,
            status=TaskFlowStatus.READY_TO_MERGE,
            step_running=False,
            completed_steps=completed,
            expected_source_result_hash=result_hash,
        )
    return replace(
        state,
        current_step=TaskStep.DEEP_CHECK,
        step_running=False,
        completed_steps=completed,
        expected_source_result_hash=result_hash,
    )


def _record_deep_check(
    state: TaskFlowState,
    result: DeepCheckResult,
    *,
    current_commit: str,
) -> TaskFlowState:
    _require_source_hash(state, result.source_result_hash)
    if result.reviewed_commit != state.current_commit:
        raise TaskFlowError("reviewed_commit does not match the reviewed commit")
    result_hash = source_result_hash(result)
    if result.result is DeepCheckDecision.PROBLEMS_FOUND:
        if result.fix_notes is None:
            raise TaskFlowError("problems_found requires fix_notes")
        return _request_fix(
            replace(state, current_commit=current_commit),
            source_hash=result_hash,
            fix_notes=result.fix_notes,
        )
    return replace(
        state,
        current_commit=current_commit,
        current_step=None,
        status=TaskFlowStatus.READY_TO_MERGE,
        step_running=False,
        completed_steps=(*state.completed_steps, TaskStep.DEEP_CHECK),
        expected_source_result_hash=result_hash,
    )


def _request_fix(
    state: TaskFlowState,
    *,
    source_hash: str,
    fix_notes: str,
) -> TaskFlowState:
    if state.fix_count >= MAX_FIXES:
        return replace(
            state,
            current_step=None,
            status=TaskFlowStatus.FAILED,
            step_running=False,
            expected_source_result_hash=source_hash,
            fix_notes=fix_notes,
        )
    return replace(
        state,
        current_step=TaskStep.FIX,
        step_running=False,
        fix_count=state.fix_count + 1,
        expected_source_result_hash=source_hash,
        fix_notes=fix_notes,
    )


def record_fix_proof(
    state: TaskFlowState,
    proof: StepProof,
    *,
    current_commit: str,
) -> TaskFlowState:
    """Accept exact Fix proof, discard prior proof, and restart at Build."""

    _require_state(state)
    _require_commit(current_commit)
    if (
        state.status is not TaskFlowStatus.RUNNING
        or state.current_step is not TaskStep.FIX
    ):
        raise TaskFlowError("Task is not waiting for Fix proof")
    if not isinstance(proof, StepProof):
        raise TaskFlowError("proof must be a StepProof")
    if proof.task_settings_hash != state.task_settings_hash:
        raise TaskFlowError("task_settings_hash does not match Task settings")
    if proof.pr_url != state.pr_url:
        raise TaskFlowError("pr_url does not match the Task PR")
    if proof.tested_commit != current_commit:
        raise TaskFlowError("tested_commit does not match current commit")
    _require_source_hash(state, proof.source_result_hash)
    if proof.fix_notes is None or proof.fix_notes != state.fix_notes:
        raise TaskFlowError("fix_notes do not match the requested Fix")
    return replace(
        state,
        current_commit=current_commit,
        current_step=TaskStep.BUILD,
        step_running=False,
        completed_steps=(),
        expected_source_result_hash=None,
        fix_notes=None,
    )


def observe_current_commit(
    state: TaskFlowState,
    current_commit: str,
    *,
    current_base_commit: str,
) -> TaskFlowState:
    """Restart Build when the observed pull-request base or head changed."""

    _require_state(state)
    _require_commit(current_commit)
    _require_commit(current_base_commit, field="current base commit")
    if (
        current_base_commit == state.current_base_commit
        and current_commit == state.current_commit
    ):
        return state
    if state.status is TaskFlowStatus.FAILED:
        return replace(
            state,
            current_base_commit=current_base_commit,
            current_commit=current_commit,
        )
    if state.current_step is TaskStep.FIX:
        return replace(
            state,
            current_base_commit=current_base_commit,
            current_commit=current_commit,
        )
    return replace(
        state,
        current_base_commit=current_base_commit,
        current_commit=current_commit,
        current_step=TaskStep.BUILD,
        status=TaskFlowStatus.RUNNING,
        step_running=False,
        completed_steps=(),
        expected_source_result_hash=None,
        fix_notes=None,
    )


def _require_source_hash(state: TaskFlowState, source_hash: str) -> None:
    if (
        state.expected_source_result_hash is None
        or source_hash != state.expected_source_result_hash
    ):
        raise TaskFlowError("source_result_hash does not match the previous result")


def _require_state(state: TaskFlowState) -> None:
    if not isinstance(state, TaskFlowState):
        raise TypeError("state must be a TaskFlowState")
