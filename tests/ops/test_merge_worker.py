from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from forge.ops.contracts import CheckRun
from forge.ops.github import GitHubMergeEvidence, PullRequestWriteState
from forge.ops.github_merge import BranchRefreshResult, MergeWriteResult
from forge.ops.hermes import GateError
from forge.ops.merge_decision import (
    AUTO_MERGE_ALLOWED,
    MANUAL_MERGE_REQUIRED,
    RESTART_FLOW,
)
from forge.ops.merge_runtime import (
    MergeRunReport,
    TaskMergeReport,
    run_merge_tasks,
)
from forge.ops.safe_files import (
    ChangedFile,
    SafeFilesEvidence,
    check_safe_files,
)
from forge.ops.task_flow import TaskFlowState, TaskFlowStatus, required_steps
from forge.ops.task_options import MergeMode, TaskFlow
from forge.ops.task_service import TaskCreationRequest
from forge.ops.task_settings import (
    TASK_SETTINGS_FORMAT,
    BranchRefreshIntent,
    TaskContent,
    TaskSettings,
    TaskSettingsStatus,
)


NOW = datetime(2026, 7, 16, 1, 0, tzinfo=UTC)
BASE = "a" * 40
HEAD = "b" * 40
NEW_HEAD = "c" * 40
MERGED = "d" * 40
PR_URL = "https://github.com/owner/repo/pull/7"
ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class Snapshot:
    request: TaskCreationRequest
    settings: TaskSettings
    issue_number: int
    root_task_id: str
    pr: PullRequestWriteState
    state: TaskFlowState
    branch_refresh_count: int = 0


class FakeEvidenceReader:
    def __init__(self, evidence: GitHubMergeEvidence) -> None:
        self.evidence = evidence
        self.calls: list[tuple[str, tuple[str, ...], bool]] = []

    def get_merge_evidence(
        self,
        pr_url: str,
        required_check_names: tuple[str, ...],
        *,
        include_safe_files: bool,
    ) -> GitHubMergeEvidence:
        self.calls.append((pr_url, required_check_names, include_safe_files))
        return self.evidence


class FakeMergeWriter:
    def __init__(
        self,
        *,
        merge_error: Exception | None = None,
        refresh_error: Exception | None = None,
        refresh_result: BranchRefreshResult | None = None,
        timeline: list[str] | None = None,
    ) -> None:
        self.merge_error = merge_error
        self.refresh_error = refresh_error
        self.refresh_result = refresh_result
        self.merge_calls: list[tuple[str, str, str]] = []
        self.refresh_calls: list[tuple[str, str, str, int]] = []
        self.timeline = timeline if timeline is not None else []

    def merge_expected_commit(
        self,
        pr_url: str,
        expected_commit: str,
        *,
        expected_base_commit: str,
    ) -> MergeWriteResult:
        self.timeline.append("github_merge")
        self.merge_calls.append((pr_url, expected_commit, expected_base_commit))
        if self.merge_error is not None:
            raise self.merge_error
        return MergeWriteResult(
            expected_commit=expected_commit,
            expected_base_commit=expected_base_commit,
            merged_commit=MERGED,
            merged_base_commit=expected_base_commit,
            merged_head_commit=expected_commit,
            already_merged=False,
            recovered_by_readback=False,
        )

    def refresh_branch(
        self,
        pr_url: str,
        *,
        expected_commit: str,
        expected_base_commit: str,
        branch_refresh_count: int,
    ) -> BranchRefreshResult:
        self.timeline.append("github_refresh")
        self.refresh_calls.append(
            (
                pr_url,
                expected_commit,
                expected_base_commit,
                branch_refresh_count,
            )
        )
        if self.refresh_error is not None:
            raise self.refresh_error
        if self.refresh_result is None:
            raise AssertionError("unexpected branch refresh")
        return self.refresh_result


class FakeSettingsStore:
    def __init__(
        self,
        current: TaskSettings | None,
        *,
        refresh_intent: BranchRefreshIntent | None = None,
        timeline: list[str] | None = None,
    ) -> None:
        self.current = current
        self.refresh_intent = refresh_intent
        self.reads: list[str] = []
        self.events: list[tuple[str, TaskSettingsStatus]] = []
        self.timeline = timeline if timeline is not None else []

    def get_active(self, request_id: str) -> TaskSettings | None:
        self.reads.append(request_id)
        return self.current

    def append_lifecycle_event(
        self,
        request_id: str,
        status: TaskSettingsStatus,
        *,
        occurred_at: datetime | None = None,
    ) -> TaskSettings:
        del occurred_at
        self.events.append((request_id, status))
        assert self.current is not None
        self.current = replace(self.current, status=status)
        return self.current

    def get_branch_refresh_replay(
        self,
        request_id: str,
        *,
        applied_refresh_count: int,
    ) -> BranchRefreshIntent | None:
        assert self.current is not None and request_id == self.current.request_id
        if self.refresh_intent is None:
            if applied_refresh_count:
                raise AssertionError("Hermes refresh count has no durable proof")
            return None
        if applied_refresh_count == self.refresh_intent.refresh_number:
            return None
        assert applied_refresh_count == self.refresh_intent.refresh_number - 1
        return self.refresh_intent

    def reserve_branch_refresh(
        self,
        request_id: str,
        *,
        pr_url: str,
        expected_base_commit: str,
        expected_head_commit: str,
        applied_refresh_count: int,
        occurred_at: datetime | None = None,
    ) -> BranchRefreshIntent:
        del occurred_at
        self.timeline.append("reserve")
        if self.refresh_intent is None:
            self.refresh_intent = BranchRefreshIntent(
                request_id=request_id,
                refresh_number=applied_refresh_count + 1,
                pr_url=pr_url,
                expected_base_commit=expected_base_commit,
                expected_head_commit=expected_head_commit,
                created_at=NOW,
                current_base_commit=None,
                current_head_commit=None,
                completed_at=None,
            )
        return self.refresh_intent

    @contextmanager
    def guard_active(self, expected: TaskSettings) -> Any:
        assert self.current == expected
        self.timeline.append("guard_enter")
        try:
            yield self
        finally:
            self.timeline.append("guard_exit")

    def complete_branch_refresh(
        self,
        intent: BranchRefreshIntent,
        *,
        current_base_commit: str,
        current_head_commit: str,
        occurred_at: datetime | None = None,
    ) -> BranchRefreshIntent:
        del occurred_at
        self.timeline.append("complete")
        assert self.refresh_intent == intent
        self.refresh_intent = replace(
            intent,
            current_base_commit=current_base_commit,
            current_head_commit=current_head_commit,
            completed_at=NOW,
        )
        return self.refresh_intent

    def finish(
        self,
        status: TaskSettingsStatus,
        *,
        occurred_at: datetime | None = None,
    ) -> TaskSettings:
        del occurred_at
        self.timeline.append("finish")
        assert self.current is not None
        self.events.append((self.current.request_id, status))
        self.current = replace(self.current, status=status)
        return self.current


class FakeFlowUpdates:
    def __init__(self, timeline: list[str] | None = None) -> None:
        self.calls: list[tuple[Snapshot, BranchRefreshResult]] = []
        self.timeline = timeline if timeline is not None else []

    def record_branch_refresh(
        self,
        snapshot: Snapshot,
        result: BranchRefreshResult,
    ) -> None:
        self.timeline.append("project")
        self.calls.append((snapshot, result))


def _snapshot(
    merge_mode: MergeMode,
    *,
    status: TaskSettingsStatus = TaskSettingsStatus.ACTIVE,
    task_flow: TaskFlow = TaskFlow.BUILD_REVIEW,
) -> Snapshot:
    content = TaskContent(
        title="Build it",
        description="Confirmed work",
        acceptance_criteria=("Works",),
    )
    confirmed_at = NOW - timedelta(minutes=10)
    request = TaskCreationRequest(
        request_id="12345678-1234-4234-8234-123456789abc",
        repository="owner/repo",
        content=content,
        task_flow=task_flow,
        merge_mode=merge_mode,
        confirmed_by="user-1",
        confirmed_at=confirmed_at,
    )
    prepared = TaskSettings.create(
        request_id=request.request_id,
        repository=request.repository,
        task_content=request.content,
        task_flow=request.task_flow,
        merge_mode=request.merge_mode,
        confirmed_by=request.confirmed_by,
        confirmed_at=request.confirmed_at,
        auto_merge_expires_at=(
            None
            if merge_mode is MergeMode.MANUAL
            else NOW + timedelta(hours=1)
        ),
    )
    settings = TaskSettings(
        format_version=TASK_SETTINGS_FORMAT,
        request_id=prepared.request_id,
        repository=prepared.repository,
        issue_number=19,
        mode=prepared.mode,
        task_content_hash=prepared.task_content_hash,
        task_flow=prepared.task_flow,
        merge_mode=prepared.merge_mode,
        confirmed_by=prepared.confirmed_by,
        confirmed_at=prepared.confirmed_at,
        auto_merge_expires_at=prepared.auto_merge_expires_at,
        status=status,
    )
    assert settings.task_settings_hash is not None
    state = TaskFlowState(
        task_flow=task_flow,
        task_settings_hash=settings.task_settings_hash,
        pr_url=PR_URL,
        current_base_commit=BASE,
        current_commit=HEAD,
        current_step=None,
        status=TaskFlowStatus.READY_TO_MERGE,
        completed_steps=required_steps(task_flow),
    )
    pr = PullRequestWriteState(
        pr_url=PR_URL,
        repository="owner/repo",
        pr_number=7,
        base_commit=BASE,
        base_ref="main",
        head_commit=HEAD,
        is_open=True,
        is_merged=False,
        merged_commit=None,
        merged_base_commit=None,
        merged_head_commit=None,
    )
    return Snapshot(
        request=request,
        settings=settings,
        issue_number=19,
        root_task_id="task-19",
        pr=pr,
        state=state,
    )


def _refresh_intent(
    snapshot: Snapshot,
    *,
    completed: bool = False,
) -> BranchRefreshIntent:
    return BranchRefreshIntent(
        request_id=snapshot.request.request_id,
        refresh_number=snapshot.branch_refresh_count + 1,
        pr_url=PR_URL,
        expected_base_commit=BASE,
        expected_head_commit=HEAD,
        created_at=NOW - timedelta(minutes=1),
        current_base_commit=BASE if completed else None,
        current_head_commit=NEW_HEAD if completed else None,
        completed_at=NOW if completed else None,
    )


def _evidence(
    *,
    head: str = HEAD,
    base_is_current: bool = True,
    safe: bool = True,
) -> GitHubMergeEvidence:
    changed_files = (
        ChangedFile(
            path="docs/guide.md",
            status="modified",
            is_text=True,
            file_type="file",
            data_complete=True,
            patch_complete=True,
            tree_entry_complete=True,
        ),
    )
    safe_files = (
        SafeFilesEvidence(
            base_commit=BASE,
            head_commit=head,
            result=check_safe_files(changed_files, pagination_complete=True),
        )
        if safe
        else None
    )
    return GitHubMergeEvidence(
        pr_url=PR_URL,
        repository="owner/repo",
        pr_number=7,
        head_commit=head,
        base_commit=BASE,
        is_open=True,
        is_draft=False,
        is_merged=False,
        merged_commit=None,
        merged_base_commit=None,
        merged_head_commit=None,
        has_conflict=False,
        base_is_current=base_is_current,
        rules_allow_merge=True,
        server_requires_current_base=True,
        unresolved_review_threads=0,
        checks=(
            CheckRun(
                name="eval",
                status="completed",
                conclusion="success",
                head_sha=head,
            ),
        ),
        changed_files=changed_files if safe else (),
        files_pagination_complete=True if safe else None,
        safe_files=safe_files,
    )


def _run(
    snapshot: Snapshot,
    *,
    evidence: GitHubMergeEvidence,
    environment: dict[str, str] | None = None,
    store_current: TaskSettings | None | object = ...,
    store_intent: BranchRefreshIntent | None = None,
    writer: FakeMergeWriter | None = None,
    updates: FakeFlowUpdates | None = None,
    clock: Any = None,
) -> tuple[Any, FakeEvidenceReader, FakeMergeWriter, FakeSettingsStore, FakeFlowUpdates]:
    timeline: list[str] = []
    reader = FakeEvidenceReader(evidence)
    merge_writer = writer or FakeMergeWriter(timeline=timeline)
    merge_writer.timeline = timeline
    store = FakeSettingsStore(
        snapshot.settings if store_current is ... else store_current,  # type: ignore[arg-type]
        refresh_intent=store_intent,
        timeline=timeline,
    )
    flow_updates = updates or FakeFlowUpdates(timeline)
    flow_updates.timeline = timeline
    report = run_merge_tasks(
        (snapshot,),
        evidence_reader=reader,
        merge_writer=merge_writer,
        settings_store=store,
        flow_updates=flow_updates,
        required_check="eval",
        environment=environment or {},
        clock=(lambda: NOW) if clock is None else clock,
    )
    return report, reader, merge_writer, store, flow_updates


def test_manual_mode_collects_common_evidence_but_performs_no_write() -> None:
    snapshot = _snapshot(MergeMode.MANUAL)

    report, reader, writer, store, _ = _run(
        snapshot,
        evidence=_evidence(safe=False),
        environment={"AUTO_MERGE_ENABLED": "true"},
    )

    assert report.ok is True
    assert report.tasks[0].decision == MANUAL_MERGE_REQUIRED
    assert report.tasks[0].action == "none"
    assert reader.calls == [(PR_URL, ("eval",), False)]
    assert writer.merge_calls == []
    assert writer.refresh_calls == []
    assert store.events == []


def test_manual_mode_syncs_only_an_exact_observed_human_merge() -> None:
    snapshot = _snapshot(MergeMode.MANUAL)
    observed_merge = replace(
        _evidence(safe=False),
        is_open=False,
        is_merged=True,
        merged_commit=MERGED,
        merged_base_commit=BASE,
        merged_head_commit=HEAD,
    )

    report, _, writer, store, _ = _run(
        snapshot,
        evidence=observed_merge,
    )

    assert report.ok is True
    assert report.tasks[0].decision == AUTO_MERGE_ALLOWED
    assert report.tasks[0].action == "observed_merge"
    assert writer.merge_calls == []
    assert writer.refresh_calls == []
    assert store.events == [
        (snapshot.request.request_id, TaskSettingsStatus.MERGED)
    ]


def test_manual_mode_rejects_a_merged_pr_with_the_wrong_historical_head() -> None:
    snapshot = _snapshot(MergeMode.MANUAL)
    wrong_merge = replace(
        _evidence(safe=False),
        is_open=False,
        is_merged=True,
        merged_commit=MERGED,
        merged_base_commit=BASE,
        merged_head_commit=NEW_HEAD,
    )

    report, _, writer, store, _ = _run(snapshot, evidence=wrong_merge)

    assert report.ok is False
    assert "merged head" in report.tasks[0].reason
    assert writer.merge_calls == []
    assert writer.refresh_calls == []
    assert store.events == []


def test_running_task_waits_without_creating_a_merge_context() -> None:
    snapshot = _snapshot(MergeMode.FULL_AUTO)
    snapshot = replace(
        snapshot,
        state=replace(
            snapshot.state,
            current_step=snapshot.state.completed_steps[0],
            status=TaskFlowStatus.RUNNING,
            completed_steps=(),
        ),
    )

    report, reader, writer, store, _ = _run(
        snapshot,
        evidence=_evidence(safe=False),
        environment={"AUTO_MERGE_ENABLED": "true"},
    )

    assert report.ok is True
    assert report.tasks[0].decision == "WAIT"
    assert report.tasks[0].action == "none"
    assert reader.calls == []
    assert writer.merge_calls == []
    assert store.reads == []
    assert store.events == []


@pytest.mark.parametrize(
    ("merge_mode", "include_safe_files"),
    [
        (MergeMode.SAFE_AUTO, True),
        (MergeMode.FULL_AUTO, False),
    ],
)
def test_auto_modes_merge_exact_validated_base_and_head(
    merge_mode: MergeMode,
    include_safe_files: bool,
) -> None:
    snapshot = _snapshot(merge_mode)

    report, reader, writer, store, _ = _run(
        snapshot,
        evidence=_evidence(safe=include_safe_files),
        environment={"AUTO_MERGE_ENABLED": "true"},
    )

    assert report.ok is True
    assert report.tasks[0].decision == AUTO_MERGE_ALLOWED
    assert report.tasks[0].action == "merged"
    assert reader.calls == [(PR_URL, ("eval",), include_safe_files)]
    assert writer.merge_calls == [(PR_URL, HEAD, BASE)]
    assert store.events == [
        (snapshot.request.request_id, TaskSettingsStatus.MERGED)
    ]
    assert store.timeline == [
        "guard_enter",
        "github_merge",
        "finish",
        "guard_exit",
    ]


def test_safe_mode_rejects_incomplete_file_pagination() -> None:
    snapshot = _snapshot(MergeMode.SAFE_AUTO)
    incomplete = replace(_evidence(), files_pagination_complete=False)

    report, _, writer, store, _ = _run(
        snapshot,
        evidence=incomplete,
        environment={"AUTO_MERGE_ENABLED": "true"},
    )

    assert report.ok is False
    assert "pagination" in report.tasks[0].reason
    assert writer.merge_calls == []
    assert store.events == []


def test_auto_merge_is_disabled_unless_environment_is_exact_lowercase_true() -> None:
    snapshot = _snapshot(MergeMode.FULL_AUTO)

    report, _, writer, store, _ = _run(
        snapshot,
        evidence=_evidence(safe=False),
        environment={"AUTO_MERGE_ENABLED": "TRUE"},
    )

    assert report.ok is True
    assert report.tasks[0].decision == AUTO_MERGE_ALLOWED
    assert report.tasks[0].action == "disabled"
    assert writer.merge_calls == []
    assert store.events == []


def test_expired_auto_permission_never_writes() -> None:
    snapshot = _snapshot(MergeMode.FULL_AUTO)
    expired = replace(snapshot.settings, auto_merge_expires_at=NOW)
    assert expired.task_settings_hash is not None
    snapshot = replace(
        snapshot,
        settings=expired,
        state=replace(
            snapshot.state,
            task_settings_hash=expired.task_settings_hash,
        ),
    )

    report, _, writer, store, _ = _run(
        snapshot,
        evidence=_evidence(safe=False),
        environment={"AUTO_MERGE_ENABLED": "true"},
    )

    assert report.ok is True
    assert report.tasks[0].decision == MANUAL_MERGE_REQUIRED
    assert "expired" in report.tasks[0].reason
    assert writer.merge_calls == []
    assert store.events == []


def test_permission_expiring_after_decision_is_checked_again_before_write() -> None:
    snapshot = _snapshot(MergeMode.FULL_AUTO)
    expires_at = snapshot.settings.auto_merge_expires_at
    assert expires_at is not None
    times = iter((expires_at - timedelta(microseconds=1), expires_at))

    report, _, writer, store, _ = _run(
        snapshot,
        evidence=_evidence(safe=False),
        environment={"AUTO_MERGE_ENABLED": "true"},
        clock=lambda: next(times),
    )

    assert report.ok is True
    assert report.tasks[0].decision == MANUAL_MERGE_REQUIRED
    assert "expired before write" in report.tasks[0].reason
    assert writer.merge_calls == []
    assert store.events == []


def test_check_error_is_not_hidden_by_permission_expiry() -> None:
    snapshot = _snapshot(MergeMode.FULL_AUTO)
    expires_at = snapshot.settings.auto_merge_expires_at
    assert expires_at is not None
    failed_check = replace(
        _evidence(safe=False),
        checks=(
            CheckRun(
                name="eval",
                status="completed",
                conclusion="failure",
                head_sha=HEAD,
            ),
        ),
    )
    times = iter((expires_at - timedelta(microseconds=1), expires_at))

    report, _, writer, store, _ = _run(
        snapshot,
        evidence=failed_check,
        environment={"AUTO_MERGE_ENABLED": "true"},
        clock=lambda: next(times),
    )

    assert report.ok is False
    assert report.tasks[0].decision == "CHECK_ERROR"
    assert "eval check" in report.tasks[0].reason
    assert writer.merge_calls == []
    assert store.events == []


def test_changed_pull_request_commit_returns_restart_without_a_write() -> None:
    snapshot = _snapshot(MergeMode.FULL_AUTO)

    report, _, writer, store, _ = _run(
        snapshot,
        evidence=_evidence(head=NEW_HEAD, safe=False),
        environment={"AUTO_MERGE_ENABLED": "true"},
    )

    assert report.ok is True
    assert report.tasks[0].decision == RESTART_FLOW
    assert report.tasks[0].action == "restart_required"
    assert writer.merge_calls == []
    assert writer.refresh_calls == []
    assert store.events == []


def test_cancelled_snapshot_fails_before_github_evidence_is_read() -> None:
    snapshot = _snapshot(
        MergeMode.FULL_AUTO,
        status=TaskSettingsStatus.CANCELLED,
    )

    report, reader, writer, store, _ = _run(
        snapshot,
        evidence=_evidence(safe=False),
        store_current=None,
        environment={"AUTO_MERGE_ENABLED": "true"},
    )

    assert report.ok is False
    assert report.tasks[0].action == "error"
    assert "active" in report.tasks[0].reason
    assert reader.calls == []
    assert writer.merge_calls == []
    assert store.events == []


def test_settings_cancelled_after_decision_are_re_read_before_write() -> None:
    snapshot = _snapshot(MergeMode.FULL_AUTO)

    report, reader, writer, store, _ = _run(
        snapshot,
        evidence=_evidence(safe=False),
        store_current=None,
        environment={"AUTO_MERGE_ENABLED": "true"},
    )

    assert report.ok is False
    assert reader.calls == [(PR_URL, ("eval",), False)]
    assert store.reads == [snapshot.request.request_id]
    assert writer.merge_calls == []
    assert store.events == []


def test_settings_hash_is_rechecked_after_the_decision() -> None:
    snapshot = _snapshot(MergeMode.FULL_AUTO)
    changed = replace(snapshot.settings, confirmed_by="different-user")
    assert changed.task_settings_hash != snapshot.settings.task_settings_hash

    report, _, writer, store, _ = _run(
        snapshot,
        evidence=_evidence(safe=False),
        store_current=changed,
        environment={"AUTO_MERGE_ENABLED": "true"},
    )

    assert report.ok is False
    assert "settings changed" in report.tasks[0].reason
    assert writer.merge_calls == []
    assert store.events == []


def test_ambiguous_merge_result_exits_as_error_after_one_write_attempt() -> None:
    snapshot = _snapshot(MergeMode.FULL_AUTO)
    writer = FakeMergeWriter(
        merge_error=GateError("GitHub merge result is ambiguous and readback failed")
    )

    report, _, writer, store, _ = _run(
        snapshot,
        evidence=_evidence(safe=False),
        environment={"AUTO_MERGE_ENABLED": "true"},
        writer=writer,
    )

    assert report.ok is False
    assert report.tasks[0].action == "error"
    assert "ambiguous" in report.tasks[0].reason
    assert writer.merge_calls == [(PR_URL, HEAD, BASE)]
    assert store.events == []


def test_malformed_merge_readback_result_never_marks_settings_merged() -> None:
    snapshot = _snapshot(MergeMode.FULL_AUTO)

    class MalformedWriter(FakeMergeWriter):
        def merge_expected_commit(  # type: ignore[override]
            self,
            pr_url: str,
            expected_commit: str,
            *,
            expected_base_commit: str,
        ) -> object:
            self.merge_calls.append((pr_url, expected_commit, expected_base_commit))
            return object()

    writer = MalformedWriter()

    report, _, writer, store, _ = _run(
        snapshot,
        evidence=_evidence(safe=False),
        environment={"AUTO_MERGE_ENABLED": "true"},
        writer=writer,
    )

    assert report.ok is False
    assert report.tasks[0].action == "error"
    assert "merge result" in report.tasks[0].reason
    assert writer.merge_calls == [(PR_URL, HEAD, BASE)]
    assert store.events == []


def test_branch_refresh_passes_exact_pair_and_persists_proof_invalidation() -> None:
    snapshot = _snapshot(MergeMode.FULL_AUTO)
    refreshed = BranchRefreshResult(
        code=RESTART_FLOW,
        reason="branch was refreshed; restart validation from Build",
        current_commit=NEW_HEAD,
        current_base_commit=BASE,
        branch_refresh_count=1,
        next_step="build",
        invalidate_existing_proofs=True,
        flow_completed=False,
        final_tested_commit=None,
    )
    writer = FakeMergeWriter(refresh_result=refreshed)

    report, _, writer, store, updates = _run(
        snapshot,
        evidence=_evidence(base_is_current=False, safe=False),
        environment={"AUTO_MERGE_ENABLED": "true"},
        writer=writer,
    )

    assert report.ok is True
    assert report.tasks[0].decision == "REFRESH_BRANCH"
    assert report.tasks[0].action == "branch_refreshed"
    assert writer.refresh_calls == [(PR_URL, HEAD, BASE, 0)]
    assert updates.calls == [(snapshot, refreshed)]
    assert store.timeline.index("reserve") < store.timeline.index("github_refresh")
    assert store.timeline.index("complete") < store.timeline.index("project")
    assert store.refresh_intent is not None and store.refresh_intent.completed
    assert store.events == []


def test_branch_refresh_write_failure_keeps_one_reserved_intent() -> None:
    snapshot = _snapshot(MergeMode.FULL_AUTO)
    writer = FakeMergeWriter(refresh_error=GateError("refresh result is ambiguous"))

    report, _, writer, store, updates = _run(
        snapshot,
        evidence=_evidence(base_is_current=False, safe=False),
        environment={"AUTO_MERGE_ENABLED": "true"},
        writer=writer,
    )

    assert report.ok is False
    assert writer.refresh_calls == [(PR_URL, HEAD, BASE, 0)]
    assert store.refresh_intent is not None
    assert store.refresh_intent.refresh_number == 1
    assert store.refresh_intent.completed is False
    assert store.timeline.index("reserve") < store.timeline.index("github_refresh")
    assert updates.calls == []


def test_branch_refresh_expiry_is_rechecked_inside_write_guard() -> None:
    snapshot = _snapshot(MergeMode.FULL_AUTO)
    expires_at = snapshot.settings.auto_merge_expires_at
    assert expires_at is not None
    times = iter(
        (
            expires_at - timedelta(microseconds=2),
            expires_at - timedelta(microseconds=1),
            expires_at,
        )
    )

    report, _, writer, store, updates = _run(
        snapshot,
        evidence=_evidence(base_is_current=False, safe=False),
        environment={"AUTO_MERGE_ENABLED": "true"},
        clock=lambda: next(times),
    )

    assert report.ok is True
    assert report.tasks[0].decision == MANUAL_MERGE_REQUIRED
    assert "expired before write" in report.tasks[0].reason
    assert store.refresh_intent is not None
    assert store.refresh_intent.completed is False
    assert writer.refresh_calls == []
    assert updates.calls == []


def test_pending_branch_refresh_intent_replays_without_spending_another_count() -> None:
    snapshot = _snapshot(MergeMode.FULL_AUTO)
    pending = _refresh_intent(snapshot)
    refreshed = BranchRefreshResult(
        code=RESTART_FLOW,
        reason="branch was refreshed; restart validation from Build",
        current_commit=NEW_HEAD,
        current_base_commit=BASE,
        branch_refresh_count=1,
        next_step="build",
        invalidate_existing_proofs=True,
        flow_completed=False,
        final_tested_commit=None,
    )
    writer = FakeMergeWriter(refresh_result=refreshed)

    report, _, writer, store, updates = _run(
        snapshot,
        evidence=_evidence(base_is_current=False, safe=False),
        environment={"AUTO_MERGE_ENABLED": "true"},
        store_intent=pending,
        writer=writer,
    )

    assert report.ok is True
    assert writer.refresh_calls == [(PR_URL, HEAD, BASE, 0)]
    assert "reserve" not in store.timeline
    assert store.refresh_intent is not None
    assert store.refresh_intent.refresh_number == 1
    assert store.refresh_intent.completed
    assert len(updates.calls) == 1


@pytest.mark.parametrize("completed", (False, True))
def test_remote_refresh_readback_replays_same_intent_without_another_github_write(
    completed: bool,
) -> None:
    snapshot = _snapshot(MergeMode.FULL_AUTO)
    intent = _refresh_intent(snapshot, completed=completed)

    report, _, writer, store, updates = _run(
        snapshot,
        evidence=_evidence(head=NEW_HEAD, safe=False),
        environment={"AUTO_MERGE_ENABLED": "true"},
        store_intent=intent,
    )

    assert report.ok is True
    assert report.tasks[0].action == "branch_refreshed"
    assert writer.refresh_calls == []
    assert "reserve" not in store.timeline
    assert store.refresh_intent is not None and store.refresh_intent.completed
    assert store.refresh_intent.refresh_number == 1
    assert len(updates.calls) == 1
    assert updates.calls[0][1].current_commit == NEW_HEAD


def test_malformed_branch_refresh_result_is_not_persisted() -> None:
    snapshot = _snapshot(MergeMode.FULL_AUTO)
    malformed = BranchRefreshResult(
        code=RESTART_FLOW,
        reason="bad count",
        current_commit=NEW_HEAD,
        current_base_commit=BASE,
        branch_refresh_count=2,
        next_step="build",
        invalidate_existing_proofs=True,
        flow_completed=False,
        final_tested_commit=None,
    )
    writer = FakeMergeWriter(refresh_result=malformed)

    report, _, writer, store, updates = _run(
        snapshot,
        evidence=_evidence(base_is_current=False, safe=False),
        environment={"AUTO_MERGE_ENABLED": "true"},
        writer=writer,
    )

    assert report.ok is False
    assert report.tasks[0].action == "error"
    assert "branch refresh result" in report.tasks[0].reason
    assert writer.refresh_calls == [(PR_URL, HEAD, BASE, 0)]
    assert updates.calls == []
    assert store.events == []


def test_report_has_one_json_object_per_task() -> None:
    manual = _snapshot(MergeMode.MANUAL)

    report, *_ = _run(manual, evidence=_evidence(safe=False))
    payload = report.to_dict()

    assert payload["ok"] is True
    assert payload["tasks"] == [report.tasks[0].to_dict()]
    assert payload["tasks"][0]["request_id"] == manual.request.request_id
    assert payload["tasks"][0]["issue_number"] == 19


def _load_merge_worker_script() -> Any:
    path = ROOT / "forge" / "scripts" / "merge-worker.py"
    spec = importlib.util.spec_from_file_location("merge_worker_live_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_supports_every_live_runtime_path() -> None:
    module = _load_merge_worker_script()

    args = module._parser().parse_args(
        [
            "--settings-db",
            "settings.db",
            "--outbox",
            "outbox.db",
            "--hermes-db",
            "kanban.db",
            "--gh",
            "gh-custom",
            "--repo",
            "owner/repo",
            "--required-check",
            "eval",
            "--hermes",
            "hermes-custom",
            "--workspace",
            "dir:/work/repo",
        ]
    )

    assert args.settings_db == Path("settings.db")
    assert args.outbox == Path("outbox.db")
    assert args.hermes_db == Path("kanban.db")
    assert args.gh == "gh-custom"
    assert args.repo == "owner/repo"
    assert args.required_check == "eval"
    assert args.hermes == "hermes-custom"
    assert args.workspace == "dir:/work/repo"


def test_cli_prints_the_full_report_and_returns_two_on_task_error(capsys: Any) -> None:
    module = _load_merge_worker_script()
    report = MergeRunReport(
        tasks=(
            TaskMergeReport(
                request_id="12345678-1234-4234-8234-123456789abc",
                issue_number=19,
                decision="CHECK_ERROR",
                action="error",
                reason="readback failed",
                pr_url=PR_URL,
                tested_commit=None,
            ),
        )
    )

    class FakeRuntime:
        def run_once(self) -> MergeRunReport:
            return report

    exit_code = module.main(
        [
            "--settings-db",
            "settings.db",
            "--outbox",
            "outbox.db",
            "--hermes-db",
            "kanban.db",
            "--repo",
            "owner/repo",
        ],
        runtime_builder=lambda args: FakeRuntime(),
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out) == report.to_dict()


def test_build_runtime_uses_the_shared_snapshot_loader_and_refresh_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_merge_worker_script()
    from forge.ops import task_runtime

    calls: dict[str, object] = {}

    class FakeGitHub:
        def __init__(self, path: str) -> None:
            calls["github_path"] = path

    class FakeWriter:
        def __init__(self, path: str) -> None:
            calls["writer_path"] = path

    class FakeTaskRuntimeGitHub:
        def __init__(self, path: str) -> None:
            calls["task_runtime_github_path"] = path

    class FakeStore:
        def __init__(self, path: Path) -> None:
            calls["settings_path"] = path

    def load(**kwargs: object) -> tuple[()]:
        calls["load"] = kwargs
        return ()

    def make_recorder(**kwargs: object) -> Any:
        calls["recorder"] = kwargs
        return lambda snapshot, result: None

    monkeypatch.setattr(module, "GitHubClient", FakeGitHub)
    monkeypatch.setattr(module, "GitHubMergeClient", FakeWriter)
    monkeypatch.setattr(module, "TaskSettingsStore", FakeStore)
    monkeypatch.setattr(task_runtime, "load_ready_to_merge_snapshots", load)
    monkeypatch.setattr(task_runtime, "build_branch_refresh_recorder", make_recorder)
    monkeypatch.setattr(
        task_runtime,
        "GitHubTaskRuntimeClient",
        FakeTaskRuntimeGitHub,
    )
    args = module._parser().parse_args(
        [
            "--settings-db",
            "settings.db",
            "--outbox",
            "outbox.db",
            "--hermes-db",
            "kanban.db",
            "--gh",
            "gh-custom",
            "--repo",
            "owner/repo",
            "--hermes",
            "hermes-custom",
            "--workspace",
            "dir:/work/repo",
        ]
    )

    runtime = module.build_runtime(args)

    load_call = calls["load"]
    assert isinstance(load_call, dict)
    assert load_call == {
        "settings_db": Path("settings.db"),
        "outbox_db": Path("outbox.db"),
        "hermes_db": Path("kanban.db"),
        "github": load_call["github"],
        "repository": "owner/repo",
    }
    assert isinstance(load_call["github"], FakeTaskRuntimeGitHub)
    assert load_call["github"] is not runtime.evidence_reader
    assert calls["recorder"] == {
        "hermes_db": Path("kanban.db"),
        "hermes_path": "hermes-custom",
        "workspace": "dir:/work/repo",
    }
    assert calls["writer_path"] == "gh-custom"
    assert calls["task_runtime_github_path"] == "gh-custom"
    assert calls["settings_path"] == Path("settings.db")
