"""Live, fail-closed orchestration for one pass of Forge Task merging."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .contracts import CheckRun
from .displayed_status import displayed_label
from .github import GitHubMergeEvidence, PullRequestWriteState, validate_commit_sha
from .github_merge import BranchRefreshResult, MergeWriteResult
from .merge_decision import (
    AUTO_MERGE_ALLOWED,
    CHECK_ERROR,
    MANUAL_MERGE_REQUIRED,
    REFRESH_BRANCH,
    RESTART_FLOW,
    WAIT,
    MergeContext,
    MergeDecision,
    MergePullRequest,
    decide_merge,
)
from .safe_files import SafeFilesEvidence, check_safe_files
from .task_flow import TaskFlowState, TaskFlowStatus
from .task_options import MergeMode
from .task_service import TaskCreationRequest
from .task_settings import (
    BranchRefreshIntent,
    TaskSettings,
    TaskSettingsStatus,
    task_content_hash,
)


class MergeRuntimeError(RuntimeError):
    """Raised when live evidence cannot be bound to one immutable Task."""


class TaskFlowSnapshotLike(Protocol):
    request: TaskCreationRequest
    settings: TaskSettings
    issue_number: int
    root_task_id: str | None
    pr: PullRequestWriteState | None
    state: TaskFlowState | None
    branch_refresh_count: int


class MergeEvidenceReader(Protocol):
    def get_merge_evidence(
        self,
        pr_url: str,
        required_check_names: Sequence[str],
        *,
        include_safe_files: bool,
    ) -> GitHubMergeEvidence: ...


class MergeWriter(Protocol):
    def merge_expected_commit(
        self,
        pr_url: str,
        expected_commit: str,
        *,
        expected_base_commit: str,
    ) -> MergeWriteResult: ...

    def refresh_branch(
        self,
        pr_url: str,
        *,
        expected_commit: str,
        expected_base_commit: str,
        branch_refresh_count: int,
    ) -> BranchRefreshResult: ...


class ActiveSettingsStore(Protocol):
    def get_active(self, request_id: str) -> TaskSettings | None: ...

    def append_lifecycle_event(
        self,
        request_id: str,
        status: TaskSettingsStatus,
        *,
        occurred_at: datetime | None = None,
    ) -> TaskSettings: ...

    def get_branch_refresh_replay(
        self,
        request_id: str,
        *,
        applied_refresh_count: int,
    ) -> BranchRefreshIntent | None: ...

    def reserve_branch_refresh(
        self,
        request_id: str,
        *,
        pr_url: str,
        expected_base_commit: str,
        expected_head_commit: str,
        applied_refresh_count: int,
        occurred_at: datetime | None = None,
    ) -> BranchRefreshIntent: ...

    def guard_active(
        self,
        expected: TaskSettings,
    ) -> AbstractContextManager[ActiveSettingsGuard]: ...


class ActiveSettingsGuard(Protocol):
    def finish(
        self,
        status: TaskSettingsStatus,
        *,
        occurred_at: datetime | None = None,
    ) -> TaskSettings: ...

    def complete_branch_refresh(
        self,
        intent: BranchRefreshIntent,
        *,
        current_base_commit: str,
        current_head_commit: str,
        occurred_at: datetime | None = None,
    ) -> BranchRefreshIntent: ...


class BranchRefreshRecorder(Protocol):
    def record_branch_refresh(
        self,
        snapshot: TaskFlowSnapshotLike,
        result: BranchRefreshResult,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class TaskMergeReport:
    request_id: str
    issue_number: int | None
    decision: str
    action: str
    reason: str
    pr_url: str | None
    tested_commit: str | None

    @property
    def ok(self) -> bool:
        return self.action != "error"

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "issue_number": self.issue_number,
            "decision": self.decision,
            "action": self.action,
            "reason": self.reason,
            "pr_url": self.pr_url,
            "tested_commit": self.tested_commit,
        }


@dataclass(frozen=True, slots=True)
class MergeRunReport:
    tasks: tuple[TaskMergeReport, ...]

    @property
    def ok(self) -> bool:
        return all(task.ok for task in self.tasks)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "tasks": [task.to_dict() for task in self.tasks],
        }


def run_merge_tasks(
    snapshots: Sequence[TaskFlowSnapshotLike],
    *,
    evidence_reader: MergeEvidenceReader,
    merge_writer: MergeWriter,
    settings_store: ActiveSettingsStore,
    flow_updates: BranchRefreshRecorder,
    required_check: str,
    environment: Mapping[str, str],
    clock: Callable[[], datetime],
) -> MergeRunReport:
    """Process independent Task snapshots and report every result as data."""

    if isinstance(snapshots, (str, bytes)) or not isinstance(snapshots, Sequence):
        raise MergeRuntimeError("Task flow snapshots must be a sequence")
    if not isinstance(required_check, str) or not required_check.strip():
        raise MergeRuntimeError("required check must be non-empty text")
    if not isinstance(environment, Mapping):
        raise MergeRuntimeError("environment must be a mapping")
    if not callable(clock):
        raise MergeRuntimeError("clock must be callable")

    reports: list[TaskMergeReport] = []
    for snapshot in snapshots:
        try:
            reports.append(
                _run_one(
                    snapshot,
                    evidence_reader=evidence_reader,
                    merge_writer=merge_writer,
                    settings_store=settings_store,
                    flow_updates=flow_updates,
                    required_check=required_check,
                    environment=environment,
                    clock=clock,
                )
            )
        except Exception as error:
            reports.append(_error_report(snapshot, str(error)))
    return MergeRunReport(tasks=tuple(reports))


def _run_one(
    snapshot: TaskFlowSnapshotLike,
    *,
    evidence_reader: MergeEvidenceReader,
    merge_writer: MergeWriter,
    settings_store: ActiveSettingsStore,
    flow_updates: BranchRefreshRecorder,
    required_check: str,
    environment: Mapping[str, str],
    clock: Callable[[], datetime],
) -> TaskMergeReport:
    if not _validate_snapshot(snapshot):
        return _report(
            snapshot,
            decision=WAIT,
            action="none",
            reason="Task flow is not ready to merge",
            tested_commit=None,
        )
    settings = snapshot.settings
    assert snapshot.pr is not None
    assert snapshot.state is not None
    include_safe_files = settings.merge_mode is MergeMode.SAFE_AUTO
    evidence = evidence_reader.get_merge_evidence(
        snapshot.pr.pr_url,
        (required_check,),
        include_safe_files=include_safe_files,
    )
    context = _merge_context(
        snapshot,
        evidence,
        required_check=required_check,
        now=_read_time(clock),
    )
    decision = decide_merge(context)

    # RISK(race): this second read happens after the pure decision and before
    # every write. A cancellation or settings replacement closes the gate.
    current_settings = settings_store.get_active(settings.request_id)
    if current_settings is None:
        raise MergeRuntimeError("Task settings are no longer active")
    if (
        not isinstance(current_settings, TaskSettings)
        or current_settings.status is not TaskSettingsStatus.ACTIVE
    ):
        raise MergeRuntimeError("re-read Task settings are invalid")
    if (
        current_settings.task_settings_hash is None
        or current_settings.task_settings_hash != settings.task_settings_hash
    ):
        raise MergeRuntimeError("Task settings changed after the merge decision")

    if decision.code == CHECK_ERROR:
        return _report(
            snapshot,
            decision=decision.code,
            action="error",
            reason=decision.reason,
            tested_commit=decision.tested_commit,
        )

    # An exact historical base/head pair proves that GitHub is already merged.
    # This local lifecycle sync is safe even when automatic GitHub writes are
    # disabled, and prevents manually merged Tasks from remaining active.
    if decision.already_merged:
        settings_store.append_lifecycle_event(
            settings.request_id,
            TaskSettingsStatus.MERGED,
            occurred_at=_read_time(clock),
        )
        return _report_from_decision(snapshot, decision, action="observed_merge")

    # Manual means exactly zero GitHub merge or branch-refresh writes.
    if settings.merge_mode is MergeMode.MANUAL:
        return _report_from_decision(snapshot, decision, action="none")

    refresh_intent = settings_store.get_branch_refresh_replay(
        settings.request_id,
        applied_refresh_count=snapshot.branch_refresh_count,
    )
    if refresh_intent is not None:
        return _resume_branch_refresh(
            snapshot,
            intent=refresh_intent,
            decision=decision,
            context=context,
            merge_writer=merge_writer,
            settings_store=settings_store,
            flow_updates=flow_updates,
            current_settings=current_settings,
            environment=environment,
            clock=clock,
        )

    if decision.code == RESTART_FLOW:
        return _report_from_decision(snapshot, decision, action="restart_required")

    if decision.code not in {AUTO_MERGE_ALLOWED, REFRESH_BRANCH}:
        return _report_from_decision(snapshot, decision, action="none")

    if decision.code == REFRESH_BRANCH:
        write_time = _read_time(clock)
        blocked = _automatic_write_block(
            snapshot,
            current_settings=current_settings,
            context=context,
            decision=decision,
            environment=environment,
            write_time=write_time,
        )
        if blocked is not None:
            return blocked
        intent = settings_store.reserve_branch_refresh(
            settings.request_id,
            pr_url=context.pull_request.pr_url,
            expected_head_commit=context.pull_request.head_commit,
            expected_base_commit=context.pull_request.base_commit,
            applied_refresh_count=snapshot.branch_refresh_count,
            occurred_at=write_time,
        )
        return _resume_branch_refresh(
            snapshot,
            intent=intent,
            decision=decision,
            context=context,
            merge_writer=merge_writer,
            settings_store=settings_store,
            flow_updates=flow_updates,
            current_settings=current_settings,
            environment=environment,
            clock=clock,
            write_time=write_time,
        )

    # RISK(race): the active check, GitHub merge, and MERGED event have one
    # database-serialized order relative to cancellation and expiry.
    with settings_store.guard_active(current_settings) as guard:
        write_time = _read_time(clock)
        blocked = _automatic_write_block(
            snapshot,
            current_settings=current_settings,
            context=context,
            decision=decision,
            environment=environment,
            write_time=write_time,
        )
        if blocked is not None:
            return blocked
        merge_result = merge_writer.merge_expected_commit(
            context.pull_request.pr_url,
            context.pull_request.head_commit,
            expected_base_commit=context.pull_request.base_commit,
        )
        _validate_merge_result(
            merge_result,
            expected_base_commit=context.pull_request.base_commit,
            expected_commit=context.pull_request.head_commit,
        )
        guard.finish(
            TaskSettingsStatus.MERGED,
            occurred_at=write_time,
        )
    return _report_from_decision(snapshot, decision, action="merged")


def _automatic_write_block(
    snapshot: TaskFlowSnapshotLike,
    *,
    current_settings: TaskSettings,
    context: MergeContext,
    decision: MergeDecision,
    environment: Mapping[str, str],
    write_time: datetime,
) -> TaskMergeReport | None:
    expires_at = current_settings.auto_merge_expires_at
    if expires_at is None:
        raise MergeRuntimeError("automatic merge permission has no expiry")
    if write_time >= expires_at:
        return _report(
            snapshot,
            decision=MANUAL_MERGE_REQUIRED,
            action="none",
            reason="automatic merge permission expired before write",
            tested_commit=context.pull_request.head_commit,
        )
    if environment.get("AUTO_MERGE_ENABLED") != "true":
        return _report_from_decision(snapshot, decision, action="disabled")
    return None


def _resume_branch_refresh(
    snapshot: TaskFlowSnapshotLike,
    *,
    intent: BranchRefreshIntent,
    decision: MergeDecision,
    context: MergeContext,
    merge_writer: MergeWriter,
    settings_store: ActiveSettingsStore,
    flow_updates: BranchRefreshRecorder,
    current_settings: TaskSettings,
    environment: Mapping[str, str],
    clock: Callable[[], datetime],
    write_time: datetime | None = None,
) -> TaskMergeReport:
    _validate_branch_refresh_intent(snapshot, intent)
    pull_request = context.pull_request
    if intent.completed:
        refresh = _branch_refresh_result_from_intent(intent)
        if (
            refresh.current_base_commit != pull_request.base_commit
            or refresh.current_commit != pull_request.head_commit
        ):
            raise MergeRuntimeError(
                "completed branch refresh does not match GitHub readback"
            )
    else:
        remote_changed = (
            pull_request.base_commit != intent.expected_base_commit
            or pull_request.head_commit != intent.expected_head_commit
        )
        if remote_changed:
            if pull_request.is_merged or not pull_request.is_open:
                raise MergeRuntimeError(
                    "pull request closed during branch refresh recovery"
                )
            refresh = BranchRefreshResult(
                code=RESTART_FLOW,
                reason="branch refresh recovered by durable GitHub readback",
                current_commit=pull_request.head_commit,
                current_base_commit=pull_request.base_commit,
                branch_refresh_count=intent.refresh_number,
                next_step="build",
                invalidate_existing_proofs=True,
                flow_completed=False,
                final_tested_commit=None,
            )
            _validate_branch_refresh_result(snapshot, refresh)
            with settings_store.guard_active(current_settings) as guard:
                guard.complete_branch_refresh(
                    intent,
                    current_base_commit=refresh.current_base_commit,
                    current_head_commit=refresh.current_commit,
                    occurred_at=_read_time(clock),
                )
        else:
            if decision.code != REFRESH_BRANCH:
                raise MergeRuntimeError(
                    "reserved branch refresh no longer has a refresh decision"
                )
            if write_time is None:
                write_time = _read_time(clock)
                blocked = _automatic_write_block(
                    snapshot,
                    current_settings=current_settings,
                    context=context,
                    decision=decision,
                    environment=environment,
                    write_time=write_time,
                )
                if blocked is not None:
                    return blocked
            with settings_store.guard_active(current_settings) as guard:
                guarded_write_time = _read_time(clock)
                blocked = _automatic_write_block(
                    snapshot,
                    current_settings=current_settings,
                    context=context,
                    decision=decision,
                    environment=environment,
                    write_time=guarded_write_time,
                )
                if blocked is not None:
                    return blocked
                refresh = merge_writer.refresh_branch(
                    pull_request.pr_url,
                    expected_commit=pull_request.head_commit,
                    expected_base_commit=pull_request.base_commit,
                    branch_refresh_count=intent.refresh_number - 1,
                )
                _validate_branch_refresh_result(snapshot, refresh)
                guard.complete_branch_refresh(
                    intent,
                    current_base_commit=refresh.current_base_commit,
                    current_head_commit=refresh.current_commit,
                    occurred_at=guarded_write_time,
                )

    _validate_branch_refresh_result(snapshot, refresh)
    # The second active guard closes the completion-to-projection cancellation
    # gap. If projection fails, the completed intent is replayed idempotently.
    with settings_store.guard_active(current_settings):
        flow_updates.record_branch_refresh(snapshot, refresh)
    return _report(
        snapshot,
        decision=REFRESH_BRANCH,
        action="branch_refreshed",
        reason=refresh.reason,
        tested_commit=refresh.current_commit,
    )


def _validate_branch_refresh_intent(
    snapshot: TaskFlowSnapshotLike,
    intent: BranchRefreshIntent,
) -> None:
    if not isinstance(intent, BranchRefreshIntent):
        raise MergeRuntimeError("branch refresh intent has an unexpected type")
    assert snapshot.pr is not None
    if (
        intent.request_id != snapshot.request.request_id
        or intent.refresh_number != snapshot.branch_refresh_count + 1
        or intent.pr_url != snapshot.pr.pr_url
        or intent.expected_base_commit != snapshot.pr.base_commit
        or intent.expected_head_commit != snapshot.pr.head_commit
    ):
        raise MergeRuntimeError("branch refresh intent does not match Task proof")


def _branch_refresh_result_from_intent(
    intent: BranchRefreshIntent,
) -> BranchRefreshResult:
    if (
        not intent.completed
        or intent.current_base_commit is None
        or intent.current_head_commit is None
    ):
        raise MergeRuntimeError("branch refresh intent has no completed readback")
    return BranchRefreshResult(
        code=RESTART_FLOW,
        reason="branch refresh recovered from durable readback",
        current_commit=intent.current_head_commit,
        current_base_commit=intent.current_base_commit,
        branch_refresh_count=intent.refresh_number,
        next_step="build",
        invalidate_existing_proofs=True,
        flow_completed=False,
        final_tested_commit=None,
    )


def _validate_merge_result(
    result: object,
    *,
    expected_base_commit: str,
    expected_commit: str,
) -> None:
    if not isinstance(result, MergeWriteResult):
        raise MergeRuntimeError("GitHub merge result has an unexpected type")
    if (
        result.expected_base_commit != expected_base_commit
        or result.expected_commit != expected_commit
        or result.merged_base_commit != expected_base_commit
        or result.merged_head_commit != expected_commit
    ):
        raise MergeRuntimeError("GitHub merge result does not match expected commits")
    try:
        validate_commit_sha(result.merged_commit, "merged commit")
    except Exception as error:
        raise MergeRuntimeError("GitHub merge result commit is invalid") from error
    if (
        type(result.already_merged) is not bool
        or type(result.recovered_by_readback) is not bool
    ):
        raise MergeRuntimeError("GitHub merge result flags are invalid")


def _validate_branch_refresh_result(
    snapshot: TaskFlowSnapshotLike,
    result: object,
) -> None:
    if not isinstance(result, BranchRefreshResult):
        raise MergeRuntimeError("GitHub branch refresh result has an unexpected type")
    if (
        result.code != RESTART_FLOW
        or result.next_step != "build"
        or result.invalidate_existing_proofs is not True
        or result.flow_completed is not False
        or result.final_tested_commit is not None
        or result.branch_refresh_count != snapshot.branch_refresh_count + 1
    ):
        raise MergeRuntimeError("GitHub branch refresh result is not an exact restart")
    try:
        validate_commit_sha(result.current_base_commit, "refreshed base commit")
        validate_commit_sha(result.current_commit, "refreshed commit")
    except Exception as error:
        raise MergeRuntimeError("GitHub branch refresh result commits are invalid") from error
    assert snapshot.pr is not None
    if (
        result.current_base_commit == snapshot.pr.base_commit
        and result.current_commit == snapshot.pr.head_commit
    ):
        raise MergeRuntimeError("GitHub branch refresh result did not change the PR")


def _validate_snapshot(snapshot: TaskFlowSnapshotLike) -> bool:
    request = getattr(snapshot, "request", None)
    settings = getattr(snapshot, "settings", None)
    state = getattr(snapshot, "state", None)
    pr = getattr(snapshot, "pr", None)
    if not isinstance(request, TaskCreationRequest):
        raise MergeRuntimeError("Task snapshot request has an unexpected type")
    if not isinstance(settings, TaskSettings):
        raise MergeRuntimeError("Task snapshot settings have an unexpected type")
    if settings.status is not TaskSettingsStatus.ACTIVE:
        raise MergeRuntimeError("Task settings are not active")
    if state is not None and not isinstance(state, TaskFlowState):
        raise MergeRuntimeError("Task flow snapshot has an unexpected type")
    if pr is not None and not isinstance(pr, PullRequestWriteState):
        raise MergeRuntimeError("Task pull request snapshot has an unexpected type")
    if type(getattr(snapshot, "issue_number", None)) is not int:
        raise MergeRuntimeError("Task snapshot issue number is invalid")
    if snapshot.issue_number != settings.issue_number:
        raise MergeRuntimeError("Task snapshot issue does not match settings")
    if (
        type(getattr(snapshot, "branch_refresh_count", None)) is not int
        or snapshot.branch_refresh_count < 0
    ):
        raise MergeRuntimeError("Task branch refresh count is invalid")
    if (
        request.request_id != settings.request_id
        or request.repository != settings.repository
        or request.task_flow is not settings.task_flow
        or request.merge_mode is not settings.merge_mode
        or request.confirmed_by != settings.confirmed_by
        or request.confirmed_at != settings.confirmed_at
    ):
        raise MergeRuntimeError("Task request does not match immutable settings")
    if task_content_hash(request.content) != settings.task_content_hash:
        raise MergeRuntimeError("Task content hash does not match immutable settings")
    if settings.task_settings_hash is None:
        raise MergeRuntimeError("Task settings hash is unavailable")
    root_task_id = getattr(snapshot, "root_task_id", None)
    if root_task_id is not None and (
        not isinstance(root_task_id, str) or not root_task_id.strip()
    ):
        raise MergeRuntimeError("Task root card id is invalid")
    if state is None or pr is None:
        if state is not None or pr is not None:
            raise MergeRuntimeError("Task flow and pull request must appear together")
        return False
    if state.task_settings_hash != settings.task_settings_hash:
        raise MergeRuntimeError("Task flow settings hash does not match")
    if state.task_flow is not settings.task_flow:
        raise MergeRuntimeError("Task flow choice does not match settings")
    if (
        state.pr_url != pr.pr_url
        or state.current_base_commit != pr.base_commit
        or state.current_commit != pr.head_commit
    ):
        raise MergeRuntimeError("Task flow does not match its stored pull request")
    if pr.repository != settings.repository:
        raise MergeRuntimeError("Task pull request repository does not match settings")
    if state.status is not TaskFlowStatus.READY_TO_MERGE:
        return False
    if displayed_label(state) != "forge:ready-to-merge":
        raise MergeRuntimeError("Task displayed status is not ready to merge")
    return True


def _merge_context(
    snapshot: TaskFlowSnapshotLike,
    evidence: GitHubMergeEvidence,
    *,
    required_check: str,
    now: datetime,
) -> MergeContext:
    if not isinstance(evidence, GitHubMergeEvidence):
        raise MergeRuntimeError("GitHub merge evidence has an unexpected type")
    state = snapshot.state
    if not isinstance(state, TaskFlowState):
        raise MergeRuntimeError("Task flow state is unavailable")
    check = _required_check(evidence.checks, required_check)
    safe_files = _verified_safe_files(snapshot.settings.merge_mode, evidence)
    pull_request = MergePullRequest(
        pr_url=evidence.pr_url,
        repository=evidence.repository,
        base_commit=evidence.base_commit,
        head_commit=evidence.head_commit,
        is_open=evidence.is_open,
        is_draft=evidence.is_draft,
        is_merged=evidence.is_merged,
        merged_commit=evidence.merged_commit,
        merged_base_commit=evidence.merged_base_commit,
        merged_head_commit=evidence.merged_head_commit,
        has_conflict=evidence.has_conflict,
        base_is_current=evidence.base_is_current,
        rules_allow_merge=evidence.rules_allow_merge,
        unresolved_review_threads=evidence.unresolved_review_threads,
        eval_status=_check_result(check),
        eval_commit=check.head_sha,
        eval_check_count=1,
    )
    return MergeContext(
        settings=snapshot.settings,
        repository=snapshot.settings.repository,
        issue_number=snapshot.issue_number,
        task_content_hash=task_content_hash(snapshot.request.content),
        task_flow_state=state,
        pull_request=pull_request,
        displayed_status=displayed_label(state),
        safe_files=safe_files,
        now=now,
        branch_refresh_count=snapshot.branch_refresh_count,
    )


def _verified_safe_files(
    merge_mode: MergeMode,
    evidence: GitHubMergeEvidence,
) -> SafeFilesEvidence | None:
    if merge_mode is not MergeMode.SAFE_AUTO:
        return None
    if evidence.files_pagination_complete is not True:
        raise MergeRuntimeError("GitHub safe-file pagination is incomplete")
    safe_files = evidence.safe_files
    if not isinstance(safe_files, SafeFilesEvidence):
        raise MergeRuntimeError("GitHub safe-file evidence is unavailable")
    if (
        safe_files.base_commit != evidence.base_commit
        or safe_files.head_commit != evidence.head_commit
    ):
        raise MergeRuntimeError("GitHub safe-file evidence commits do not match")
    recomputed = check_safe_files(
        evidence.changed_files,
        pagination_complete=evidence.files_pagination_complete,
    )
    if safe_files.result != recomputed:
        raise MergeRuntimeError("GitHub safe-file result does not match changed files")
    return safe_files


def _required_check(checks: Sequence[CheckRun], required_check: str) -> CheckRun:
    matches = tuple(check for check in checks if check.name == required_check)
    if len(matches) != 1:
        raise MergeRuntimeError("required check must appear exactly once")
    return matches[0]


def _check_result(check: CheckRun) -> str:
    if check.status in {"queued", "in_progress"}:
        if check.conclusion is not None:
            raise MergeRuntimeError("pending required check has a conclusion")
        return check.status
    if check.status != "completed" or not isinstance(check.conclusion, str):
        raise MergeRuntimeError("required check result is invalid")
    return check.conclusion


def _read_time(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise MergeRuntimeError("merge runtime clock must include a timezone")
    if value.utcoffset() is None:
        raise MergeRuntimeError("merge runtime clock must include a timezone")
    return value


def _report_from_decision(
    snapshot: TaskFlowSnapshotLike,
    decision: MergeDecision,
    *,
    action: str,
) -> TaskMergeReport:
    return _report(
        snapshot,
        decision=decision.code,
        action=action,
        reason=decision.reason,
        tested_commit=decision.tested_commit,
    )


def _report(
    snapshot: TaskFlowSnapshotLike,
    *,
    decision: str,
    action: str,
    reason: str,
    tested_commit: str | None,
) -> TaskMergeReport:
    request = getattr(snapshot, "request", None)
    pr = getattr(snapshot, "pr", None)
    return TaskMergeReport(
        request_id=(
            request.request_id
            if isinstance(request, TaskCreationRequest)
            else "unknown"
        ),
        issue_number=(
            snapshot.issue_number
            if type(getattr(snapshot, "issue_number", None)) is int
            else None
        ),
        decision=decision,
        action=action,
        reason=reason,
        pr_url=pr.pr_url if isinstance(pr, PullRequestWriteState) else None,
        tested_commit=tested_commit,
    )


def _error_report(
    snapshot: object,
    reason: str,
) -> TaskMergeReport:
    request = getattr(snapshot, "request", None)
    pr = getattr(snapshot, "pr", None)
    return TaskMergeReport(
        request_id=(
            request.request_id
            if isinstance(request, TaskCreationRequest)
            else "unknown"
        ),
        issue_number=(
            getattr(snapshot, "issue_number")
            if type(getattr(snapshot, "issue_number", None)) is int
            else None
        ),
        decision=CHECK_ERROR,
        action="error",
        reason=reason or "unexpected merge runtime error",
        pr_url=pr.pr_url if isinstance(pr, PullRequestWriteState) else None,
        tested_commit=None,
    )


__all__ = [
    "ActiveSettingsStore",
    "BranchRefreshRecorder",
    "MergeEvidenceReader",
    "MergeRunReport",
    "MergeRuntimeError",
    "MergeWriter",
    "TaskFlowSnapshotLike",
    "TaskMergeReport",
    "run_merge_tasks",
]
