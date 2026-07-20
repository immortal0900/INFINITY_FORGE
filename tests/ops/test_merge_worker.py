from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import importlib.util
import json
import multiprocessing
from pathlib import Path
import sqlite3
import threading
from typing import Any

import pytest

from forge.ops.contracts import CheckRun
from forge.ops.github import GitHubMergeEvidence, PullRequestWriteState
from forge.ops.github_merge import BranchRefreshResult, MergeWriteResult
from forge.ops.hermes import GateError
from forge.ops.surface_events import SurfaceEventStore, TrustedTurnContext
from forge.ops.task_messages import TaskMessageStore
from forge.ops.merge_decision import (
    AUTO_MERGE_ALLOWED,
    MANUAL_MERGE_REQUIRED,
    ProjectMergeProof,
    RESTART_FLOW,
)
from forge.ops.merge_runtime import (
    MergeRuntimeError,
    MergeRunReport,
    TaskDatabaseProjectMergeStore,
    ProjectMergeSnapshot,
    ProjectMergeTask,
    TaskMergeReport,
    load_project_merge_tasks,
    run_merge_tasks,
    run_project_merge_tasks,
)
from forge.ops.safe_files import (
    ChangedFile,
    SafeFilesEvidence,
    check_safe_files,
)
from forge.ops.task_flow import TaskFlowState, TaskFlowStatus, required_steps
from forge.ops.task_options import MergeMode, TaskFlow
from forge.ops.task_service import TaskCreationRequest
from forge.ops.task_projects import TaskProject
from forge.ops.task_database import TaskDatabase
from forge.ops.task_revisions import TaskRevisionService
from forge.ops.task_settings import (
    TASK_SETTINGS_FORMAT,
    BranchRefreshIntent,
    TaskContent,
    TaskSettings,
    TaskSettingsStatus,
)
from forge.ops.task_settings_v2 import TaskRequestV2, TaskSettingsV2


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


def test_merge_cli_without_v1_repo_and_outbox_selects_v2_registry() -> None:
    module = _load_merge_worker_script()

    args = module._parser().parse_args(
        [
            "--settings-db",
            "task.db",
            "--hermes-db",
            "kanban.db",
            "--gh",
            "gh-custom",
        ]
    )

    assert args.settings_db == Path("task.db")
    assert args.hermes_db == Path("kanban.db")
    assert args.repo is None
    assert args.outbox is None


def test_build_runtime_without_v1_repo_uses_v2_loader_and_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_merge_worker_script()
    calls: dict[str, object] = {}

    class FakeGitHub:
        def __init__(self, path: str) -> None:
            calls["github_path"] = path

    class FakeTaskGitHub:
        def __init__(self, path: str) -> None:
            calls["task_github_path"] = path

    class FakeWriter:
        def __init__(self, path: str) -> None:
            calls["writer_path"] = path

    class FakeStore:
        def __init__(self, path: Path) -> None:
            calls["store_path"] = path

    def load(**kwargs: object) -> tuple[()]:
        calls["load"] = kwargs
        return ()

    monkeypatch.setattr(module, "GitHubClient", FakeGitHub)
    monkeypatch.setattr(module, "GitHubMergeClient", FakeWriter)
    monkeypatch.setattr(module, "GitHubTaskRuntimeClient", FakeTaskGitHub)
    monkeypatch.setattr(module, "TaskDatabaseProjectMergeStore", FakeStore)
    monkeypatch.setattr(module, "load_project_merge_tasks", load)
    args = module._parser().parse_args(
        [
            "--settings-db",
            "task.db",
            "--hermes-db",
            "kanban.db",
            "--gh",
            "gh-custom",
        ]
    )

    runtime = module.build_runtime(args)

    assert calls["load"] == {
        "settings_db": Path("task.db"),
        "hermes_db": Path("kanban.db"),
        "github": calls["load"]["github"],  # type: ignore[index]
    }
    assert calls["task_github_path"] == "gh-custom"
    assert calls["store_path"] == Path("task.db")
    assert runtime.tasks == ()


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


class FakeProjectMergeStore:
    def __init__(self, timeline: list[str]) -> None:
        self.timeline = timeline
        self.merge_attempt_created = True
        self.merged: list[str] = []
        self.partial: list[tuple[tuple[str, ...], str, tuple[str, ...]]] = []
        self.completed = False
        self.fail_record_once_for: str | None = None
        self.pending: list[str] = []
        self.failed: list[str] = []

    def prepare_barrier(
        self,
        task: ProjectMergeTask,
        proofs: tuple[object, ...],
        *,
        occurred_at: datetime,
    ) -> None:
        assert len(proofs) == len(task.projects)
        assert occurred_at == NOW
        self.timeline.append("barrier")

    def reserve_project(
        self,
        task: ProjectMergeTask,
        snapshot: ProjectMergeSnapshot,
        *,
        expected_head_commit: str,
        occurred_at: datetime,
    ) -> bool:
        assert snapshot in task.projects
        assert snapshot.task_flow_state is not None
        assert expected_head_commit == snapshot.task_flow_state.current_commit
        assert occurred_at == NOW
        self.timeline.append(f"reserve:{snapshot.project.repository}")
        return self.merge_attempt_created

    @contextmanager
    def guard_project(
        self,
        task: ProjectMergeTask,
        snapshot: ProjectMergeSnapshot,
        *,
        expected_head_commit: str,
    ) -> Any:
        assert snapshot in task.projects
        assert snapshot.task_flow_state is not None
        assert expected_head_commit == snapshot.task_flow_state.current_commit
        self.timeline.append(f"guard:{snapshot.project.repository}")
        yield self

    def mark_merged(
        self,
        snapshot: ProjectMergeSnapshot,
        result: MergeWriteResult,
        *,
        occurred_at: datetime,
    ) -> None:
        assert result.merged_head_commit == snapshot.task_flow_state.current_commit
        assert occurred_at == NOW
        if self.fail_record_once_for == snapshot.project.project_id:
            self.fail_record_once_for = None
            raise GateError("database commit response was lost")
        self.merged.append(snapshot.project.project_id)
        self.timeline.append(f"record:{snapshot.project.repository}")

    def finish_merged(
        self,
        task: ProjectMergeTask,
        *,
        occurred_at: datetime,
    ) -> None:
        assert occurred_at == NOW
        assert set(self.merged) == {item.project.project_id for item in task.projects}
        self.completed = True
        self.timeline.append("parent:merged")

    def finish_partial(
        self,
        task: ProjectMergeTask,
        *,
        merged_project_ids: tuple[str, ...],
        failed_project_id: str,
        remaining_project_ids: tuple[str, ...],
        reason: str,
        occurred_at: datetime,
    ) -> None:
        assert task.settings.parent_issue_number == 21
        assert reason
        assert occurred_at == NOW
        self.partial.append(
            (merged_project_ids, failed_project_id, remaining_project_ids)
        )
        self.timeline.append("parent:partially_merged")

    def converge_observed_merge(
        self,
        task: ProjectMergeTask,
        snapshot: ProjectMergeSnapshot,
        result: MergeWriteResult,
        *,
        occurred_at: datetime,
    ) -> None:
        assert snapshot in task.projects
        assert result.already_merged is True
        assert occurred_at == NOW
        if snapshot.project.project_id not in self.merged:
            self.merged.append(snapshot.project.project_id)
        self.timeline.append(f"observed:{snapshot.project.repository}")

    def mark_reconciliation_pending(
        self,
        task: ProjectMergeTask,
        snapshot: ProjectMergeSnapshot,
        *,
        occurred_at: datetime,
    ) -> None:
        assert snapshot in task.projects
        assert occurred_at == NOW
        self.pending.append(snapshot.project.project_id)
        self.timeline.append(f"pending:{snapshot.project.repository}")

    def mark_failed(
        self,
        task: ProjectMergeTask,
        snapshot: ProjectMergeSnapshot,
        *,
        reason: str,
        occurred_at: datetime,
    ) -> None:
        assert snapshot in task.projects
        assert reason
        assert occurred_at == NOW
        self.failed.append(snapshot.project.project_id)
        self.timeline.append(f"failed:{snapshot.project.repository}")


class ProjectEvidenceReader:
    def __init__(
        self,
        evidence: dict[str, list[GitHubMergeEvidence | Exception]],
    ) -> None:
        self.evidence = evidence
        self.calls: list[str] = []

    def get_merge_evidence(
        self,
        pr_url: str,
        required_check_names: tuple[str, ...],
        *,
        include_safe_files: bool,
    ) -> GitHubMergeEvidence:
        assert required_check_names == ("eval",)
        assert include_safe_files is False
        self.calls.append(pr_url)
        values = self.evidence[pr_url]
        value = values.pop(0) if len(values) > 1 else values[0]
        if isinstance(value, Exception):
            raise value
        return value


class OrderedProjectWriter(FakeMergeWriter):
    def __init__(
        self,
        timeline: list[str],
        *,
        fail_on_call: int | None = None,
    ) -> None:
        super().__init__(timeline=timeline)
        self.fail_on_call = fail_on_call

    def merge_expected_commit(
        self,
        pr_url: str,
        expected_commit: str,
        *,
        expected_base_commit: str,
    ) -> MergeWriteResult:
        if self.fail_on_call == len(self.merge_calls) + 1:
            self.timeline.append("github_merge")
            self.merge_calls.append((pr_url, expected_commit, expected_base_commit))
            raise GateError("merge response and readback were lost")
        return super().merge_expected_commit(
            pr_url,
            expected_commit,
            expected_base_commit=expected_base_commit,
        )


class AmbiguousRaceWriter(FakeMergeWriter):
    def __init__(self) -> None:
        super().__init__()
        self.ambiguous_started = threading.Event()
        self.owner_thread: int | None = None
        self._lock = threading.Lock()

    def merge_expected_commit(
        self,
        pr_url: str,
        expected_commit: str,
        *,
        expected_base_commit: str,
    ) -> MergeWriteResult:
        with self._lock:
            call_number = len(self.merge_calls) + 1
            self.merge_calls.append((pr_url, expected_commit, expected_base_commit))
        if call_number == 1:
            self.owner_thread = threading.get_ident()
            self.ambiguous_started.set()
            raise GateError("merge response was lost")
        return MergeWriteResult(
            expected_commit=expected_commit,
            expected_base_commit=expected_base_commit,
            merged_commit=MERGED,
            merged_base_commit=expected_base_commit,
            merged_head_commit=expected_commit,
            already_merged=False,
            recovered_by_readback=False,
        )


class AmbiguousRaceEvidenceReader:
    def __init__(
        self,
        task: ProjectMergeTask,
        *,
        writer: AmbiguousRaceWriter,
        release_recovery: threading.Event,
    ) -> None:
        self._by_url = {
            snapshot.task_flow_state.pr_url: _project_evidence(snapshot)
            for snapshot in task.projects
            if snapshot.task_flow_state is not None
        }
        self._writer = writer
        self._release_recovery = release_recovery

    def get_merge_evidence(
        self,
        pr_url: str,
        required_check_names: tuple[str, ...],
        *,
        include_safe_files: bool,
    ) -> GitHubMergeEvidence:
        assert required_check_names == ("eval",)
        assert include_safe_files is False
        if (
            self._writer.ambiguous_started.is_set()
            and self._writer.owner_thread == threading.get_ident()
        ):
            assert self._release_recovery.wait(timeout=10)
        return self._by_url[pr_url]


class PermitRaceEvidenceReader:
    def __init__(self, task: ProjectMergeTask, *, target_url: str) -> None:
        self._by_url = {
            snapshot.task_flow_state.pr_url: _project_evidence(snapshot)
            for snapshot in task.projects
            if snapshot.task_flow_state is not None
        }
        self._target_url = target_url
        self._target_calls = 0
        self._lock = threading.Lock()
        self.fresh_started = threading.Event()
        self.release_fresh = threading.Event()

    def get_merge_evidence(
        self,
        pr_url: str,
        required_check_names: tuple[str, ...],
        *,
        include_safe_files: bool,
    ) -> GitHubMergeEvidence:
        assert required_check_names == ("eval",)
        assert include_safe_files is False
        should_block = False
        if pr_url == self._target_url:
            with self._lock:
                self._target_calls += 1
                should_block = self._target_calls == 2
        if should_block:
            self.fresh_started.set()
            assert self.release_fresh.wait(timeout=20)
        return self._by_url[pr_url]


class PausingReservationStore:
    def __init__(self, inner: TaskDatabaseProjectMergeStore) -> None:
        self._inner = inner
        self.reservation_committed = threading.Event()
        self.release_reservation = threading.Event()

    def reserve_project(
        self,
        task: ProjectMergeTask,
        snapshot: ProjectMergeSnapshot,
        *,
        expected_head_commit: str,
        occurred_at: datetime,
    ) -> bool:
        created = self._inner.reserve_project(
            task,
            snapshot,
            expected_head_commit=expected_head_commit,
            occurred_at=occurred_at,
        )
        if created:
            self.reservation_committed.set()
            assert self.release_reservation.wait(timeout=20)
        return created

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _insert_lifecycle_barrier(
    database_path: str,
    task: ProjectMergeTask,
    event_type: str,
) -> None:
    with TaskDatabase(database_path).transaction() as connection:
        connection.execute(
            """
            INSERT INTO task_events (
                request_id, task_settings_hash, project_id, event_type,
                event_key, event_json, occurred_at
            ) VALUES (?, ?, NULL, ?, ?, ?, ?)
            """,
            (
                task.request.request_id,
                task.settings.task_settings_hash,
                event_type,
                f"{event_type}:race",
                json.dumps({"reason": "race"}, sort_keys=True),
                NOW.isoformat(),
            ),
        )


def _reserve_project_in_process(
    task: ProjectMergeTask,
    database_path: str,
    project_id: str,
    start: Any,
    results: Any,
) -> None:
    snapshot = next(
        item for item in task.projects if item.project.project_id == project_id
    )
    assert snapshot.task_flow_state is not None
    proofs = tuple(
        ProjectMergeProof(
            project_id=item.project.project_id,
            repository=item.project.repository,
            decision=AUTO_MERGE_ALLOWED,
            expected_head_commit=item.task_flow_state.current_commit,  # type: ignore[union-attr]
        )
        for item in task.projects
    )
    store = TaskDatabaseProjectMergeStore(database_path)
    assert start.wait(timeout=20)
    store.prepare_barrier(task, proofs, occurred_at=NOW)
    reservation_created = store.reserve_project(
        task,
        snapshot,
        expected_head_commit=snapshot.task_flow_state.current_commit,
        occurred_at=NOW,
    )
    results.put(reservation_created)


def _hold_merge_permit_in_process(
    task: ProjectMergeTask,
    database_path: str,
    project_id: str,
    acquired: Any,
    release: Any,
) -> None:
    snapshot = next(
        item for item in task.projects if item.project.project_id == project_id
    )
    assert snapshot.task_flow_state is not None
    store = TaskDatabaseProjectMergeStore(database_path)
    with store.guard_project(
        task,
        snapshot,
        expected_head_commit=snapshot.task_flow_state.current_commit,
    ):
        acquired.set()
        assert release.wait(timeout=30)


def _try_merge_permit_in_process(
    task: ProjectMergeTask,
    database_path: str,
    project_id: str,
    result: Any,
) -> None:
    snapshot = next(
        item for item in task.projects if item.project.project_id == project_id
    )
    assert snapshot.task_flow_state is not None
    store = TaskDatabaseProjectMergeStore(database_path)
    try:
        with store.guard_project(
            task,
            snapshot,
            expected_head_commit=snapshot.task_flow_state.current_commit,
        ):
            result.put("acquired")
    except Exception:
        result.put("blocked")


def _insert_stop_in_process(
    database_path: str,
    task: ProjectMergeTask,
    started: Any,
    committed: Any,
) -> None:
    started.set()
    _insert_lifecycle_barrier(database_path, task, "stop_requested")
    committed.set()


def _project_merge_task(
    tmp_path: Path,
    *,
    merge_mode: MergeMode = MergeMode.FULL_AUTO,
    second_ready: bool = True,
    expired: bool = False,
) -> ProjectMergeTask:
    host_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    projects = []
    for index, repository in enumerate(("owner/project-a", "owner/project-b"), start=1):
        workspace = tmp_path / f"project-{index}"
        workspace.mkdir()
        projects.append(
            TaskProject.create(
                repository=repository,
                workspace=str(workspace.resolve()),
                remote_name="origin",
                base_branch="main",
                base_commit=str(index) * 40,
                host_id=host_id,
            )
        )
    selected = tuple(projects)
    request = TaskRequestV2.create(
        request_id="12345678-1234-4234-8234-123456789abc",
        management_repository="owner/management",
        task_content=TaskContent(
            title="Merge Projects",
            description="Merge only after both are ready.",
            acceptance_criteria=("Use the confirmed order.",),
        ),
        task_flow=TaskFlow.BUILD_REVIEW,
        merge_mode=merge_mode,
        merge_order=(
            tuple(project.project_id for project in reversed(selected))
            if merge_mode is MergeMode.FULL_AUTO
            else None
        ),
        projects=selected,
        task_owner_host=host_id,
        confirmed_by="user-1",
        confirmed_at=NOW - timedelta(minutes=10),
        auto_merge_expires_at=(
            None
            if merge_mode is MergeMode.MANUAL
            else (
                NOW - timedelta(minutes=1)
                if expired
                else NOW + timedelta(hours=1)
            )
        ),
    )
    settings = TaskSettingsV2.create(request=request, parent_issue_number=21)
    snapshots = []
    for index, project in enumerate(settings.projects):
        pr_url = f"https://github.com/{project.repository}/pull/{index + 1}"
        state = TaskFlowState(
            task_flow=settings.task_flow,
            task_settings_hash=settings.task_settings_hash,
            pr_url=pr_url,
            current_base_commit=project.base_commit,
            current_commit=("a" if index == 0 else "b") * 40,
            current_step=None,
            status=TaskFlowStatus.READY_TO_MERGE,
            completed_steps=required_steps(settings.task_flow),
        )
        snapshots.append(
            ProjectMergeSnapshot(
                request=request,
                settings=settings,
                project=project,
                project_state="running",
                task_flow_state=(None if index == 1 and not second_ready else state),
            )
        )
    return ProjectMergeTask(
        request=request,
        settings=settings,
        projects=tuple(snapshots),
    )


def _project_evidence(
    snapshot: ProjectMergeSnapshot,
    *,
    merged: bool = False,
) -> GitHubMergeEvidence:
    assert snapshot.task_flow_state is not None
    state = snapshot.task_flow_state
    return GitHubMergeEvidence(
        pr_url=state.pr_url,
        repository=snapshot.project.repository,
        pr_number=int(state.pr_url.rsplit("/", 1)[1]),
        head_commit=state.current_commit,
        base_commit=state.current_base_commit,
        is_open=not merged,
        is_draft=False,
        is_merged=merged,
        merged_commit=MERGED if merged else None,
        merged_base_commit=state.current_base_commit if merged else None,
        merged_head_commit=state.current_commit if merged else None,
        has_conflict=False,
        base_is_current=True,
        rules_allow_merge=True,
        server_requires_current_base=True,
        unresolved_review_threads=0,
        checks=(CheckRun("eval", "completed", "success", state.current_commit),),
        changed_files=(),
        files_pagination_complete=None,
        safe_files=None,
    )


@pytest.mark.parametrize("merge_mode", (MergeMode.MANUAL, MergeMode.SAFE_AUTO))
def test_multi_project_manual_and_safe_auto_have_zero_merge_writes(
    tmp_path: Path,
    merge_mode: MergeMode,
) -> None:
    task = _project_merge_task(tmp_path, merge_mode=merge_mode)
    timeline: list[str] = []
    store = FakeProjectMergeStore(timeline)
    reader = ProjectEvidenceReader(
        {
            snapshot.task_flow_state.pr_url: [_project_evidence(snapshot)]
            for snapshot in task.projects
            if snapshot.task_flow_state is not None
        }
    )
    writer = OrderedProjectWriter(timeline)

    report = run_project_merge_tasks(
        (task,),
        evidence_reader=reader,
        merge_writer=writer,
        merge_store=store,
        required_check="eval",
        environment={"AUTO_MERGE_ENABLED": "true"},
        clock=lambda: NOW,
    )

    assert report.ok is True
    assert writer.merge_calls == []
    assert store.merged == []
    assert "barrier" not in timeline


@pytest.mark.parametrize(
    ("merge_mode", "environment", "expired"),
    (
        (MergeMode.MANUAL, {"AUTO_MERGE_ENABLED": "true"}, False),
        (MergeMode.SAFE_AUTO, {"AUTO_MERGE_ENABLED": "true"}, False),
        (MergeMode.FULL_AUTO, {}, False),
        (MergeMode.FULL_AUTO, {"AUTO_MERGE_ENABLED": "true"}, True),
    ),
)
def test_write_blocked_policy_still_converges_exact_remote_merges(
    tmp_path: Path,
    merge_mode: MergeMode,
    environment: dict[str, str],
    expired: bool,
) -> None:
    task = _project_merge_task(
        tmp_path,
        merge_mode=merge_mode,
        expired=expired,
    )
    timeline: list[str] = []
    store = FakeProjectMergeStore(timeline)
    reader = ProjectEvidenceReader(
        {
            snapshot.task_flow_state.pr_url: [
                _project_evidence(snapshot, merged=True)
            ]
            for snapshot in task.projects
            if snapshot.task_flow_state is not None
        }
    )
    writer = OrderedProjectWriter(timeline)

    report = run_project_merge_tasks(
        (task,),
        evidence_reader=reader,
        merge_writer=writer,
        merge_store=store,
        required_check="eval",
        environment=environment,
        clock=lambda: NOW,
    )

    assert report.ok is True
    assert len(reader.calls) == len(task.projects)
    assert writer.merge_calls == []
    assert set(store.merged) == {
        snapshot.project.project_id for snapshot in task.projects
    }
    assert store.completed is True
    assert all(item.action == "observed_merge" for item in report.tasks)


def test_full_auto_does_not_start_when_one_project_is_not_verified(
    tmp_path: Path,
) -> None:
    task = _project_merge_task(tmp_path, second_ready=False)
    timeline: list[str] = []
    store = FakeProjectMergeStore(timeline)
    first = task.projects[0]
    reader = ProjectEvidenceReader(
        {first.task_flow_state.pr_url: [_project_evidence(first)]}  # type: ignore[union-attr]
    )
    writer = OrderedProjectWriter(timeline)

    report = run_project_merge_tasks(
        (task,),
        evidence_reader=reader,
        merge_writer=writer,
        merge_store=store,
        required_check="eval",
        environment={"AUTO_MERGE_ENABLED": "true"},
        clock=lambda: NOW,
    )

    assert report.ok is True
    assert writer.merge_calls == []
    assert timeline == []


def test_full_auto_merges_only_in_confirmed_order_with_a_fresh_guard(
    tmp_path: Path,
) -> None:
    task = _project_merge_task(tmp_path)
    timeline: list[str] = []
    store = FakeProjectMergeStore(timeline)
    reader = ProjectEvidenceReader(
        {
            snapshot.task_flow_state.pr_url: [
                _project_evidence(snapshot),
                _project_evidence(snapshot),
            ]
            for snapshot in task.projects
            if snapshot.task_flow_state is not None
        }
    )
    writer = OrderedProjectWriter(timeline)

    report = run_project_merge_tasks(
        (task,),
        evidence_reader=reader,
        merge_writer=writer,
        merge_store=store,
        required_check="eval",
        environment={"AUTO_MERGE_ENABLED": "true"},
        clock=lambda: NOW,
    )

    expected = [
        next(
            snapshot
            for snapshot in task.projects
            if snapshot.project.project_id == project_id
        )
        for project_id in task.settings.merge_order or ()
    ]
    assert report.ok is True
    assert [call[0] for call in writer.merge_calls] == [
        snapshot.task_flow_state.pr_url for snapshot in expected  # type: ignore[union-attr]
    ]
    assert timeline[0] == "barrier"
    assert timeline[-1] == "parent:merged"
    assert store.completed is True


def test_second_project_failure_records_partial_and_never_merges_the_rest(
    tmp_path: Path,
) -> None:
    task = _project_merge_task(tmp_path)
    timeline: list[str] = []
    store = FakeProjectMergeStore(timeline)
    reader = ProjectEvidenceReader(
        {
            snapshot.task_flow_state.pr_url: [
                _project_evidence(snapshot),
                _project_evidence(snapshot),
            ]
            for snapshot in task.projects
            if snapshot.task_flow_state is not None
        }
    )
    writer = OrderedProjectWriter(timeline, fail_on_call=2)

    report = run_project_merge_tasks(
        (task,),
        evidence_reader=reader,
        merge_writer=writer,
        merge_store=store,
        required_check="eval",
        environment={"AUTO_MERGE_ENABLED": "true"},
        clock=lambda: NOW,
    )

    assert report.ok is False
    assert len(writer.merge_calls) == 2
    assert len(store.merged) == 1
    assert len(store.partial) == 1
    assert store.partial[0][0] == tuple(store.merged)
    assert "parent:partially_merged" in timeline


def test_unknown_second_merge_result_stays_pending_without_false_partial(
    tmp_path: Path,
) -> None:
    task = _project_merge_task(tmp_path)
    ordered = [
        next(
            snapshot
            for snapshot in task.projects
            if snapshot.project.project_id == project_id
        )
        for project_id in task.settings.merge_order or ()
    ]
    timeline: list[str] = []
    store = FakeProjectMergeStore(timeline)
    evidence: dict[str, list[GitHubMergeEvidence | Exception]] = {}
    for index, snapshot in enumerate(ordered):
        assert snapshot.task_flow_state is not None
        evidence[snapshot.task_flow_state.pr_url] = [
            _project_evidence(snapshot),
            _project_evidence(snapshot),
            *(
                [
                    GateError("GitHub merge readback is unavailable"),
                    GateError("GitHub merge readback is still unavailable"),
                ]
                if index == 1
                else []
            ),
        ]
    reader = ProjectEvidenceReader(evidence)
    writer = OrderedProjectWriter(timeline, fail_on_call=2)

    report = run_project_merge_tasks(
        (task,),
        evidence_reader=reader,
        merge_writer=writer,
        merge_store=store,
        required_check="eval",
        environment={"AUTO_MERGE_ENABLED": "true"},
        clock=lambda: NOW,
    )

    assert report.ok is False
    assert len(store.merged) == 1
    assert store.failed == []
    assert store.partial == []
    assert store.pending == [ordered[1].project.project_id]
    assert report.tasks[-1].action == "reconciliation_pending"
    assert "parent:partially_merged" not in timeline


def test_pending_retry_only_reconciles_and_starts_no_new_merge(
    tmp_path: Path,
) -> None:
    original = _project_merge_task(tmp_path)
    pending = original.projects[0]
    task = replace(
        original,
        projects=tuple(
            replace(
                snapshot,
                project_state="waiting_for_help",
                merge_attempt_pending=True,
            )
            if snapshot == pending
            else snapshot
            for snapshot in original.projects
        ),
    )
    timeline: list[str] = []
    store = FakeProjectMergeStore(timeline)
    reader = ProjectEvidenceReader(
        {
            snapshot.task_flow_state.pr_url: [
                _project_evidence(snapshot, merged=snapshot.project == pending.project)
            ]
            for snapshot in task.projects
            if snapshot.task_flow_state is not None
        }
    )
    writer = OrderedProjectWriter(timeline)

    report = run_project_merge_tasks(
        (task,),
        evidence_reader=reader,
        merge_writer=writer,
        merge_store=store,
        required_check="eval",
        environment={"AUTO_MERGE_ENABLED": "true"},
        clock=lambda: NOW,
    )

    assert report.ok is True
    assert writer.merge_calls == []
    assert store.merged == [pending.project.project_id]
    assert store.partial == []
    assert store.completed is False
    assert any(item.action == "observed_merge" for item in report.tasks)


def test_pending_exact_unmerged_readback_waits_without_failing_or_retrying(
    tmp_path: Path,
) -> None:
    original = _project_merge_task(tmp_path)
    pending = original.projects[0]
    task = replace(
        original,
        projects=tuple(
            replace(
                snapshot,
                project_state="waiting_for_help",
                merge_attempt_pending=True,
            )
            if snapshot == pending
            else snapshot
            for snapshot in original.projects
        ),
    )
    timeline: list[str] = []
    store = FakeProjectMergeStore(timeline)
    reader = ProjectEvidenceReader(
        {
            snapshot.task_flow_state.pr_url: [_project_evidence(snapshot)]
            for snapshot in task.projects
            if snapshot.task_flow_state is not None
        }
    )
    writer = OrderedProjectWriter(timeline)

    report = run_project_merge_tasks(
        (task,),
        evidence_reader=reader,
        merge_writer=writer,
        merge_store=store,
        required_check="eval",
        environment={"AUTO_MERGE_ENABLED": "true"},
        clock=lambda: NOW,
    )

    assert report.ok is False
    assert writer.merge_calls == []
    assert store.failed == []
    assert store.partial == []
    assert store.pending == [pending.project.project_id]
    assert any(item.action == "reconciliation_pending" for item in report.tasks)


def test_lost_merge_response_converges_from_exact_remote_readback(
    tmp_path: Path,
) -> None:
    task = _project_merge_task(tmp_path)
    ordered = [
        next(
            snapshot
            for snapshot in task.projects
            if snapshot.project.project_id == project_id
        )
        for project_id in task.settings.merge_order or ()
    ]
    timeline: list[str] = []
    store = FakeProjectMergeStore(timeline)
    evidence: dict[str, list[GitHubMergeEvidence]] = {}
    for index, snapshot in enumerate(ordered, start=1):
        assert snapshot.task_flow_state is not None
        evidence[snapshot.task_flow_state.pr_url] = [
            _project_evidence(snapshot),
            _project_evidence(snapshot),
            *([_project_evidence(snapshot, merged=True)] if index == 2 else []),
        ]
    reader = ProjectEvidenceReader(evidence)
    writer = OrderedProjectWriter(timeline, fail_on_call=2)

    report = run_project_merge_tasks(
        (task,),
        evidence_reader=reader,
        merge_writer=writer,
        merge_store=store,
        required_check="eval",
        environment={"AUTO_MERGE_ENABLED": "true"},
        clock=lambda: NOW,
    )

    assert report.ok is True
    assert len(writer.merge_calls) == 2
    assert store.partial == []
    assert store.completed is True
    assert report.tasks[-1].action == "observed_merge"


def test_lost_database_result_after_remote_merge_is_read_back_before_partial(
    tmp_path: Path,
) -> None:
    task = _project_merge_task(tmp_path)
    ordered = [
        next(
            snapshot
            for snapshot in task.projects
            if snapshot.project.project_id == project_id
        )
        for project_id in task.settings.merge_order or ()
    ]
    timeline: list[str] = []
    store = FakeProjectMergeStore(timeline)
    store.fail_record_once_for = ordered[1].project.project_id
    evidence: dict[str, list[GitHubMergeEvidence]] = {}
    for index, snapshot in enumerate(ordered):
        assert snapshot.task_flow_state is not None
        evidence[snapshot.task_flow_state.pr_url] = [
            _project_evidence(snapshot),
            _project_evidence(snapshot),
            *([_project_evidence(snapshot, merged=True)] if index == 1 else []),
        ]
    reader = ProjectEvidenceReader(evidence)
    writer = OrderedProjectWriter(timeline)

    report = run_project_merge_tasks(
        (task,),
        evidence_reader=reader,
        merge_writer=writer,
        merge_store=store,
        required_check="eval",
        environment={"AUTO_MERGE_ENABLED": "true"},
        clock=lambda: NOW,
    )

    assert report.ok is True
    assert store.partial == []
    assert store.completed is True
    assert set(store.merged) == {snapshot.project.project_id for snapshot in ordered}


def _insert_project_merge_task_database(
    path: Path,
    task: ProjectMergeTask,
) -> None:
    database = TaskDatabase(path)
    request = task.request
    settings = task.settings
    request_payload = json.loads(request.to_json())
    settings_payload = json.loads(settings.to_json())
    project_payloads = {
        item["project_id"]: json.dumps(
            item,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        for item in request_payload["projects"]
    }
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO task_requests (
                request_id, format_version, request_json, request_hash,
                management_repository, task_owner_host, confirmed_by,
                confirmed_at, replaces_request_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                request.request_id,
                request.format_version,
                request.to_json(),
                request.request_hash,
                request.management_repository,
                request.task_owner_host,
                request.confirmed_by,
                request_payload["confirmed_at"],
            ),
        )
        connection.execute(
            """
            INSERT INTO task_settings_v2 (
                task_settings_hash, request_id, request_hash, format_version,
                settings_json, management_repository, parent_issue_number,
                task_owner_host, confirmed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                settings.task_settings_hash,
                settings.request_id,
                settings.request_hash,
                settings.format_version,
                settings.to_json(),
                settings.management_repository,
                settings.parent_issue_number,
                settings.task_owner_host,
                settings_payload["confirmed_at"],
            ),
        )
        for index, snapshot in enumerate(task.projects, start=1):
            connection.execute(
                """
                INSERT INTO task_projects (
                    request_id, project_id, task_settings_hash, project_json,
                    state, root_card_id, branch_name, worktree_path, pr_url,
                    head_commit, merge_commit, updated_at
                ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, NULL, NULL, NULL, ?)
                """,
                (
                    request.request_id,
                    snapshot.project.project_id,
                    settings.task_settings_hash,
                    project_payloads[snapshot.project.project_id],
                    f"root-{index}",
                    f"forge/task-{index}",
                    snapshot.project.workspace,
                    request_payload["confirmed_at"],
                ),
            )
        for event_type in ("settings_activated", "active"):
            connection.execute(
                """
                INSERT INTO task_events (
                    request_id, task_settings_hash, project_id, event_type,
                    event_key, event_json, occurred_at
                ) VALUES (?, ?, NULL, ?, ?, ?, ?)
                """,
                (
                    request.request_id,
                    settings.task_settings_hash,
                    event_type,
                    event_type,
                    json.dumps(
                        {"task_settings_hash": settings.task_settings_hash},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    request_payload["confirmed_at"],
                ),
            )
        connection.execute(
            """
            INSERT INTO task_access (
                request_id, surface, subject_id, role, granted_by,
                granted_at, revoked_at
            ) VALUES (?, 'desktop', ?, 'owner', ?, ?, NULL)
            """,
            (
                request.request_id,
                request.confirmed_by,
                request.confirmed_by,
                request_payload["confirmed_at"],
            ),
        )


def _create_empty_hermes_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                body TEXT,
                idempotency_key TEXT,
                assignee TEXT,
                skills TEXT
            );
            CREATE TABLE task_links (
                parent_id TEXT NOT NULL,
                child_id TEXT NOT NULL
            );
            """
        )


@dataclass(frozen=True, slots=True)
class FixedProjectSnapshotGitHub:
    state: PullRequestWriteState

    def get_pr_write_state(self, pr_url: str) -> PullRequestWriteState:
        assert pr_url == self.state.pr_url
        return self.state


def _pending_loader_fixture(
    tmp_path: Path,
    *,
    merged_commit: str | None = MERGED,
    merged_base_commit: str | None = None,
    merged_head_commit: str | None = None,
) -> tuple[Path, Path, ProjectMergeTask, ProjectMergeSnapshot, PullRequestWriteState]:
    task = _project_merge_task(tmp_path)
    database_path = tmp_path / "task.db"
    hermes_path = tmp_path / "hermes.db"
    _insert_project_merge_task_database(database_path, task)
    _create_empty_hermes_database(hermes_path)
    pending = task.projects[0]
    assert pending.task_flow_state is not None
    with TaskDatabase(database_path).transaction() as connection:
        connection.execute(
            """
            UPDATE task_projects
            SET state = 'waiting_for_help', pr_url = ?, head_commit = ?
            WHERE request_id = ? AND project_id = ?
            """,
            (
                pending.task_flow_state.pr_url,
                pending.task_flow_state.current_commit,
                task.request.request_id,
                pending.project.project_id,
            ),
        )
    candidate = PullRequestWriteState(
        pr_url=pending.task_flow_state.pr_url,
        repository=pending.project.repository,
        pr_number=1,
        base_commit="e" * 40,
        base_ref=pending.project.base_branch,
        head_commit="f" * 40,
        is_open=False,
        is_merged=True,
        merged_commit=merged_commit,
        merged_base_commit=(
            pending.project.base_commit
            if merged_base_commit is None
            else merged_base_commit
        ),
        merged_head_commit=(
            pending.task_flow_state.current_commit
            if merged_head_commit is None
            else merged_head_commit
        ),
    )
    return database_path, hermes_path, task, pending, candidate


def test_pending_loader_uses_merged_parent_proof_not_current_pr_heads(
    tmp_path: Path,
) -> None:
    database_path, hermes_path, task, pending, candidate = (
        _pending_loader_fixture(tmp_path)
    )

    loaded = load_project_merge_tasks(
        settings_db=database_path,
        hermes_db=hermes_path,
        github=FixedProjectSnapshotGitHub(candidate),
    )

    assert len(loaded) == 1
    recovered = next(
        snapshot
        for snapshot in loaded[0].projects
        if snapshot.project.project_id == pending.project.project_id
    )
    assert recovered.merge_attempt_pending is True
    assert recovered.task_flow_state is not None
    assert recovered.task_flow_state.current_base_commit == pending.project.base_commit
    assert (
        recovered.task_flow_state.current_commit
        == pending.task_flow_state.current_commit  # type: ignore[union-attr]
    )
    assert candidate.base_commit != pending.project.base_commit
    assert candidate.head_commit != recovered.task_flow_state.current_commit
    assert loaded[0].request == task.request


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("merged_commit", "not-a-commit"),
        ("merged_base_commit", "9" * 40),
        ("merged_head_commit", "8" * 40),
    ),
)
def test_pending_loader_rejects_malformed_merged_parent_proof(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    database_path, hermes_path, _, pending, candidate = _pending_loader_fixture(
        tmp_path
    )
    assert pending.task_flow_state is not None
    malformed = replace(
        candidate,
        base_commit=pending.project.base_commit,
        head_commit=pending.task_flow_state.current_commit,
        **{field: value},
    )

    with pytest.raises(MergeRuntimeError, match="pending Project remote proof"):
        load_project_merge_tasks(
            settings_db=database_path,
            hermes_db=hermes_path,
            github=FixedProjectSnapshotGitHub(malformed),
        )


def test_database_project_guard_rechecks_head_and_records_terminal_partial(
    tmp_path: Path,
) -> None:
    task = _project_merge_task(tmp_path)
    database_path = tmp_path / "task.db"
    _insert_project_merge_task_database(database_path, task)
    store = TaskDatabaseProjectMergeStore(database_path)
    proofs = tuple(
        ProjectMergeProof(
            project_id=snapshot.project.project_id,
            repository=snapshot.project.repository,
            decision=AUTO_MERGE_ALLOWED,
            expected_head_commit=snapshot.task_flow_state.current_commit,  # type: ignore[union-attr]
        )
        for snapshot in task.projects
    )
    store.prepare_barrier(task, proofs, occurred_at=NOW)
    first, second = task.projects
    first_state = first.task_flow_state
    assert first_state is not None
    result = MergeWriteResult(
        expected_commit=first_state.current_commit,
        expected_base_commit=first_state.current_base_commit,
        merged_commit=MERGED,
        merged_base_commit=first_state.current_base_commit,
        merged_head_commit=first_state.current_commit,
        already_merged=False,
        recovered_by_readback=False,
    )
    assert store.reserve_project(
        task,
        first,
        expected_head_commit=first_state.current_commit,
        occurred_at=NOW,
    )
    with store.guard_project(
        task,
        first,
        expected_head_commit=first_state.current_commit,
    ) as guard:
        guard.mark_merged(first, result, occurred_at=NOW)
    store.finish_partial(
        task,
        merged_project_ids=(first.project.project_id,),
        failed_project_id=second.project.project_id,
        remaining_project_ids=(),
        reason="second Project failed",
        occurred_at=NOW,
    )

    with TaskDatabase(database_path).read() as connection:
        rows = connection.execute(
            """
            SELECT project_id, state, pr_url, head_commit, merge_commit
            FROM task_projects WHERE request_id = ? ORDER BY project_id
            """,
            (task.request.request_id,),
        ).fetchall()
        states = {row[0]: tuple(row[1:]) for row in rows}
        terminal = connection.execute(
            """
            SELECT event_type, event_json FROM task_events
            WHERE request_id = ? AND event_type = 'partially_merged'
            """,
            (task.request.request_id,),
        ).fetchone()
    assert states[first.project.project_id][0] == "merged"
    assert states[first.project.project_id][3] == MERGED
    assert states[second.project.project_id][0] == "failed"
    assert terminal is not None and terminal[0] == "partially_merged"
    assert json.loads(terminal[1])["failed_project_id"] == second.project.project_id


def test_database_store_converges_human_merges_without_remote_writes(
    tmp_path: Path,
) -> None:
    task = _project_merge_task(tmp_path, merge_mode=MergeMode.MANUAL)
    database_path = tmp_path / "task.db"
    _insert_project_merge_task_database(database_path, task)
    store = TaskDatabaseProjectMergeStore(database_path)
    reader = ProjectEvidenceReader(
        {
            snapshot.task_flow_state.pr_url: [
                _project_evidence(snapshot, merged=True)
            ]
            for snapshot in task.projects
            if snapshot.task_flow_state is not None
        }
    )
    writer = OrderedProjectWriter([])

    report = run_project_merge_tasks(
        (task,),
        evidence_reader=reader,
        merge_writer=writer,
        merge_store=store,
        required_check="eval",
        environment={"AUTO_MERGE_ENABLED": "true"},
        clock=lambda: NOW,
    )

    with TaskDatabase(database_path).read() as connection:
        states = connection.execute(
            """
            SELECT state, pr_url, head_commit, merge_commit
            FROM task_projects WHERE request_id = ? ORDER BY project_id
            """,
            (task.request.request_id,),
        ).fetchall()
        terminal = connection.execute(
            """
            SELECT event_type FROM task_events
            WHERE request_id = ? AND event_type = 'merged'
            """,
            (task.request.request_id,),
        ).fetchone()
    assert report.ok is True
    assert writer.merge_calls == []
    assert all(row[0] == "merged" and row[3] == MERGED for row in states)
    assert all(row[1] is not None and row[2] is not None for row in states)
    assert terminal is not None


def test_database_store_keeps_unknown_merge_nonterminal_and_retryable(
    tmp_path: Path,
) -> None:
    task = _project_merge_task(tmp_path)
    database_path = tmp_path / "task.db"
    _insert_project_merge_task_database(database_path, task)
    ordered = [
        next(
            snapshot
            for snapshot in task.projects
            if snapshot.project.project_id == project_id
        )
        for project_id in task.settings.merge_order or ()
    ]
    evidence: dict[str, list[GitHubMergeEvidence | Exception]] = {}
    for index, snapshot in enumerate(ordered):
        assert snapshot.task_flow_state is not None
        evidence[snapshot.task_flow_state.pr_url] = [
            _project_evidence(snapshot),
            _project_evidence(snapshot),
            *(
                [GateError("GitHub readback is unavailable")]
                if index == 1
                else []
            ),
        ]
    writer = OrderedProjectWriter([], fail_on_call=2)

    report = run_project_merge_tasks(
        (task,),
        evidence_reader=ProjectEvidenceReader(evidence),
        merge_writer=writer,
        merge_store=TaskDatabaseProjectMergeStore(database_path),
        required_check="eval",
        environment={"AUTO_MERGE_ENABLED": "true"},
        clock=lambda: NOW,
    )

    with TaskDatabase(database_path).read() as connection:
        rows = connection.execute(
            """
            SELECT project_id, state, merge_commit FROM task_projects
            WHERE request_id = ?
            """,
            (task.request.request_id,),
        ).fetchall()
        terminal = connection.execute(
            """
            SELECT event_type FROM task_events
            WHERE request_id = ? AND event_type = 'partially_merged'
            """,
            (task.request.request_id,),
        ).fetchone()
    state_by_id = {row[0]: (row[1], row[2]) for row in rows}
    assert report.ok is False
    assert state_by_id[ordered[0].project.project_id][0] == "merged"
    assert state_by_id[ordered[1].project.project_id] == (
        "waiting_for_help",
        None,
    )
    assert terminal is None


def test_two_workers_never_repeat_same_reserved_remote_merge(
    tmp_path: Path,
) -> None:
    task = _project_merge_task(tmp_path)
    database_path = tmp_path / "task.db"
    _insert_project_merge_task_database(database_path, task)
    target_id = (task.settings.merge_order or ())[0]
    target = next(
        snapshot
        for snapshot in task.projects
        if snapshot.project.project_id == target_id
    )
    assert target.task_flow_state is not None
    writer = AmbiguousRaceWriter()
    release_recovery = threading.Event()
    reader = AmbiguousRaceEvidenceReader(
        task,
        writer=writer,
        release_recovery=release_recovery,
    )

    def worker() -> MergeRunReport:
        return run_project_merge_tasks(
            (task,),
            evidence_reader=reader,
            merge_writer=writer,
            merge_store=TaskDatabaseProjectMergeStore(database_path),
            required_check="eval",
            environment={"AUTO_MERGE_ENABLED": "true"},
            clock=lambda: NOW,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker)
        assert writer.ambiguous_started.wait(timeout=10)
        with TaskDatabase(database_path).read() as connection:
            reserved = connection.execute(
                """
                SELECT state, pr_url, head_commit, merge_commit
                FROM task_projects
                WHERE request_id = ? AND project_id = ?
                """,
                (task.request.request_id, target.project.project_id),
            ).fetchone()
        assert reserved is not None
        assert tuple(reserved) == (
            "waiting_for_help",
            target.task_flow_state.pr_url,
            target.task_flow_state.current_commit,
            None,
        )
        second = pool.submit(worker)
        try:
            second_report = second.result(timeout=10)
        finally:
            release_recovery.set()
        first_report = first.result(timeout=10)

    target_calls = [
        call for call in writer.merge_calls if call[0] == target.task_flow_state.pr_url
    ]
    assert len(target_calls) == 1
    assert any(
        item.action == "reconciliation_pending"
        for item in (*first_report.tasks, *second_report.tasks)
    )


def test_merge_permit_delays_stop_until_remote_write_safe_point(
    tmp_path: Path,
) -> None:
    task = _project_merge_task(tmp_path)
    database_path = tmp_path / "task.db"
    _insert_project_merge_task_database(database_path, task)
    target_id = (task.settings.merge_order or ())[0]
    target = next(
        snapshot
        for snapshot in task.projects
        if snapshot.project.project_id == target_id
    )
    assert target.task_flow_state is not None
    reader = PermitRaceEvidenceReader(
        task,
        target_url=target.task_flow_state.pr_url,
    )
    writer = OrderedProjectWriter([])
    stop_started = threading.Event()
    stop_committed = threading.Event()

    def merge_worker() -> MergeRunReport:
        return run_project_merge_tasks(
            (task,),
            evidence_reader=reader,
            merge_writer=writer,
            merge_store=TaskDatabaseProjectMergeStore(database_path),
            required_check="eval",
            environment={"AUTO_MERGE_ENABLED": "true"},
            clock=lambda: NOW,
        )

    def stop_worker() -> None:
        stop_started.set()
        _insert_lifecycle_barrier(str(database_path), task, "stop_requested")
        stop_committed.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        merge_result = pool.submit(merge_worker)
        assert reader.fresh_started.wait(timeout=10)
        stop_result = pool.submit(stop_worker)
        assert stop_started.wait(timeout=10)
        try:
            assert stop_committed.wait(timeout=0.5) is False
        finally:
            reader.release_fresh.set()
        merge_result.result(timeout=20)
        stop_result.result(timeout=20)

    target_calls = [
        call for call in writer.merge_calls if call[0] == target.task_flow_state.pr_url
    ]
    assert len(target_calls) == 1
    assert stop_committed.is_set()


@pytest.mark.parametrize("event_type", ("stop_requested", "revision_requested"))
def test_lifecycle_committed_before_merge_permit_blocks_remote_write(
    tmp_path: Path,
    event_type: str,
) -> None:
    task = _project_merge_task(tmp_path)
    database_path = tmp_path / "task.db"
    _insert_project_merge_task_database(database_path, task)
    store = PausingReservationStore(
        TaskDatabaseProjectMergeStore(database_path)
    )
    writer = OrderedProjectWriter([])
    reader = ProjectEvidenceReader(
        {
            snapshot.task_flow_state.pr_url: [_project_evidence(snapshot)]
            for snapshot in task.projects
            if snapshot.task_flow_state is not None
        }
    )

    def merge_worker() -> MergeRunReport:
        return run_project_merge_tasks(
            (task,),
            evidence_reader=reader,
            merge_writer=writer,
            merge_store=store,
            required_check="eval",
            environment={"AUTO_MERGE_ENABLED": "true"},
            clock=lambda: NOW,
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(merge_worker)
        assert store.reservation_committed.wait(timeout=10)
        _insert_lifecycle_barrier(str(database_path), task, event_type)
        store.release_reservation.set()
        report = result.result(timeout=20)

    assert writer.merge_calls == []
    assert report.ok is False
    with TaskDatabase(database_path).read() as connection:
        target_id = (task.settings.merge_order or ())[0]
        state = connection.execute(
            """
            SELECT state, merge_commit FROM task_projects
            WHERE request_id = ? AND project_id = ?
            """,
            (task.request.request_id, target_id),
        ).fetchone()
    assert state is not None and tuple(state) == ("waiting_for_help", None)


def test_two_processes_create_only_one_exact_merge_reservation(
    tmp_path: Path,
) -> None:
    task = _project_merge_task(tmp_path)
    database_path = tmp_path / "task.db"
    _insert_project_merge_task_database(database_path, task)
    project_id = (task.settings.merge_order or ())[0]
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_reserve_project_in_process,
            args=(task, str(database_path), project_id, start, results),
        )
        for _ in range(2)
    ]

    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=30)

    assert [process.exitcode for process in processes] == [0, 0]
    ownership = sorted(results.get(timeout=10) for _ in processes)
    assert ownership == [False, True]


def test_two_processes_permit_first_makes_stop_wait_for_safe_point(
    tmp_path: Path,
) -> None:
    task = _project_merge_task(tmp_path)
    database_path = tmp_path / "task.db"
    _insert_project_merge_task_database(database_path, task)
    store = TaskDatabaseProjectMergeStore(database_path)
    proofs = tuple(
        ProjectMergeProof(
            snapshot.project.project_id,
            snapshot.project.repository,
            AUTO_MERGE_ALLOWED,
            snapshot.task_flow_state.current_commit,  # type: ignore[union-attr]
        )
        for snapshot in task.projects
    )
    store.prepare_barrier(task, proofs, occurred_at=NOW)
    project_id = (task.settings.merge_order or ())[0]
    target = next(
        snapshot
        for snapshot in task.projects
        if snapshot.project.project_id == project_id
    )
    assert target.task_flow_state is not None
    assert store.reserve_project(
        task,
        target,
        expected_head_commit=target.task_flow_state.current_commit,
        occurred_at=NOW,
    )
    context = multiprocessing.get_context("spawn")
    acquired = context.Event()
    release = context.Event()
    stop_started = context.Event()
    stop_committed = context.Event()
    holder = context.Process(
        target=_hold_merge_permit_in_process,
        args=(task, str(database_path), project_id, acquired, release),
    )
    stopper = context.Process(
        target=_insert_stop_in_process,
        args=(str(database_path), task, stop_started, stop_committed),
    )

    holder.start()
    assert acquired.wait(timeout=20)
    stopper.start()
    assert stop_started.wait(timeout=20)
    try:
        assert stop_committed.wait(timeout=0.5) is False
    finally:
        release.set()
    holder.join(timeout=30)
    stopper.join(timeout=30)

    assert (holder.exitcode, stopper.exitcode) == (0, 0)
    assert stop_committed.is_set()


def test_two_processes_stop_first_blocks_merge_permit(
    tmp_path: Path,
) -> None:
    task = _project_merge_task(tmp_path)
    database_path = tmp_path / "task.db"
    _insert_project_merge_task_database(database_path, task)
    store = TaskDatabaseProjectMergeStore(database_path)
    proofs = tuple(
        ProjectMergeProof(
            snapshot.project.project_id,
            snapshot.project.repository,
            AUTO_MERGE_ALLOWED,
            snapshot.task_flow_state.current_commit,  # type: ignore[union-attr]
        )
        for snapshot in task.projects
    )
    store.prepare_barrier(task, proofs, occurred_at=NOW)
    project_id = (task.settings.merge_order or ())[0]
    target = next(
        snapshot
        for snapshot in task.projects
        if snapshot.project.project_id == project_id
    )
    assert target.task_flow_state is not None
    assert store.reserve_project(
        task,
        target,
        expected_head_commit=target.task_flow_state.current_commit,
        occurred_at=NOW,
    )
    context = multiprocessing.get_context("spawn")
    stop_started = context.Event()
    stop_committed = context.Event()
    stopper = context.Process(
        target=_insert_stop_in_process,
        args=(str(database_path), task, stop_started, stop_committed),
    )
    result = context.Queue()
    permit = context.Process(
        target=_try_merge_permit_in_process,
        args=(task, str(database_path), project_id, result),
    )

    stopper.start()
    assert stop_committed.wait(timeout=20)
    stopper.join(timeout=30)
    permit.start()
    permit.join(timeout=30)

    assert (stopper.exitcode, permit.exitcode) == (0, 0)
    assert result.get(timeout=10) == "blocked"


def test_crash_after_reservation_recovers_by_readback_without_new_write(
    tmp_path: Path,
) -> None:
    task = _project_merge_task(tmp_path)
    database_path = tmp_path / "task.db"
    _insert_project_merge_task_database(database_path, task)
    store = TaskDatabaseProjectMergeStore(database_path)
    proofs = tuple(
        ProjectMergeProof(
            snapshot.project.project_id,
            snapshot.project.repository,
            AUTO_MERGE_ALLOWED,
            snapshot.task_flow_state.current_commit,  # type: ignore[union-attr]
        )
        for snapshot in task.projects
    )
    store.prepare_barrier(task, proofs, occurred_at=NOW)
    target_id = (task.settings.merge_order or ())[0]
    target = next(
        snapshot
        for snapshot in task.projects
        if snapshot.project.project_id == target_id
    )
    assert target.task_flow_state is not None
    reservation_created = store.reserve_project(
        task,
        target,
        expected_head_commit=target.task_flow_state.current_commit,
        occurred_at=NOW,
    )
    assert reservation_created is True
    # Simulate a process exit after durable intent, before GitHub write permit.

    retry_task = replace(
        task,
        projects=tuple(
            replace(
                snapshot,
                project_state="waiting_for_help",
                merge_attempt_pending=True,
            )
            if snapshot == target
            else snapshot
            for snapshot in task.projects
        ),
    )
    writer = OrderedProjectWriter([])
    report = run_project_merge_tasks(
        (retry_task,),
        evidence_reader=ProjectEvidenceReader(
            {
                snapshot.task_flow_state.pr_url: [_project_evidence(snapshot)]
                for snapshot in retry_task.projects
                if snapshot.task_flow_state is not None
            }
        ),
        merge_writer=writer,
        merge_store=TaskDatabaseProjectMergeStore(database_path),
        required_check="eval",
        environment={"AUTO_MERGE_ENABLED": "true"},
        clock=lambda: NOW,
    )

    with TaskDatabase(database_path).read() as connection:
        state = connection.execute(
            """
            SELECT state, pr_url, head_commit, merge_commit
            FROM task_projects WHERE request_id = ? AND project_id = ?
            """,
            (task.request.request_id, target_id),
        ).fetchone()
    assert report.ok is False
    assert writer.merge_calls == []
    assert state is not None
    assert tuple(state) == (
        "waiting_for_help",
        target.task_flow_state.pr_url,
        target.task_flow_state.current_commit,
        None,
    )


def test_database_project_guard_rejects_head_tampering_before_remote_write(
    tmp_path: Path,
) -> None:
    task = _project_merge_task(tmp_path)
    database_path = tmp_path / "task.db"
    _insert_project_merge_task_database(database_path, task)
    store = TaskDatabaseProjectMergeStore(database_path)
    proofs = tuple(
        ProjectMergeProof(
            snapshot.project.project_id,
            snapshot.project.repository,
            AUTO_MERGE_ALLOWED,
            snapshot.task_flow_state.current_commit,  # type: ignore[union-attr]
        )
        for snapshot in task.projects
    )
    store.prepare_barrier(task, proofs, occurred_at=NOW)
    first = task.projects[0]
    with TaskDatabase(database_path).transaction() as connection:
        connection.execute(
            "UPDATE task_projects SET head_commit = ? WHERE project_id = ?",
            ("f" * 40, first.project.project_id),
        )

    with pytest.raises(Exception, match="head|proof|Project"):
        store.reserve_project(
            task,
            first,
            expected_head_commit=first.task_flow_state.current_commit,  # type: ignore[union-attr]
            occurred_at=NOW,
        )


def test_database_barrier_rejects_project_state_change_before_any_merge(
    tmp_path: Path,
) -> None:
    task = _project_merge_task(tmp_path)
    database_path = tmp_path / "task.db"
    _insert_project_merge_task_database(database_path, task)
    store = TaskDatabaseProjectMergeStore(database_path)
    proofs = tuple(
        ProjectMergeProof(
            snapshot.project.project_id,
            snapshot.project.repository,
            AUTO_MERGE_ALLOWED,
            snapshot.task_flow_state.current_commit,  # type: ignore[union-attr]
        )
        for snapshot in task.projects
    )
    changed = task.projects[0]
    with TaskDatabase(database_path).transaction() as connection:
        connection.execute(
            "UPDATE task_projects SET state = 'reviewing' WHERE project_id = ?",
            (changed.project.project_id,),
        )

    with pytest.raises(Exception, match="state|Project|proof"):
        store.prepare_barrier(task, proofs, occurred_at=NOW)

    with TaskDatabase(database_path).read() as connection:
        proofs_after = connection.execute(
            "SELECT pr_url, head_commit FROM task_projects WHERE request_id = ?",
            (task.request.request_id,),
        ).fetchall()
    assert all(tuple(row) == (None, None) for row in proofs_after)


@pytest.mark.parametrize("event_type", ("stop_requested", "revision_requested"))
def test_database_reservation_rejects_lifecycle_change_after_barrier(
    tmp_path: Path,
    event_type: str,
) -> None:
    task = _project_merge_task(tmp_path)
    database_path = tmp_path / "task.db"
    _insert_project_merge_task_database(database_path, task)
    store = TaskDatabaseProjectMergeStore(database_path)
    proofs = tuple(
        ProjectMergeProof(
            snapshot.project.project_id,
            snapshot.project.repository,
            AUTO_MERGE_ALLOWED,
            snapshot.task_flow_state.current_commit,  # type: ignore[union-attr]
        )
        for snapshot in task.projects
    )
    store.prepare_barrier(task, proofs, occurred_at=NOW)
    target = task.projects[0]
    assert target.task_flow_state is not None
    with TaskDatabase(database_path).transaction() as connection:
        connection.execute(
            """
            INSERT INTO task_events (
                request_id, task_settings_hash, project_id, event_type,
                event_key, event_json, occurred_at
            ) VALUES (?, ?, NULL, ?, ?, ?, ?)
            """,
            (
                task.request.request_id,
                task.settings.task_settings_hash,
                event_type,
                f"{event_type}:test",
                json.dumps({"reason": "test"}, sort_keys=True),
                NOW.isoformat(),
            ),
        )

    with pytest.raises(Exception, match="blocked|lifecycle|state"):
        store.reserve_project(
            task,
            target,
            expected_head_commit=target.task_flow_state.current_commit,
            occurred_at=NOW,
        )

    with TaskDatabase(database_path).read() as connection:
        state = connection.execute(
            """
            SELECT state FROM task_projects
            WHERE request_id = ? AND project_id = ?
            """,
            (task.request.request_id, target.project.project_id),
        ).fetchone()
    assert state is not None and state[0] == target.project_state


def test_project_merge_store_allows_only_explicitly_resumed_revision(
    tmp_path: Path,
) -> None:
    task = _project_merge_task(tmp_path)
    database_path = tmp_path / "task.db"
    _insert_project_merge_task_database(database_path, task)
    database = TaskDatabase(database_path)
    context = TrustedTurnContext(
        owner_host=task.request.task_owner_host,
        subject_id=task.request.confirmed_by,
        session_id="merge-resume-session",
        surface="desktop",
        source_event_id="desktop:merge-resume",
        working_directory=None,
    )
    SurfaceEventStore(database).receive(
        context,
        "cancel this merge update",
        at=NOW + timedelta(seconds=1),
    )
    receipt = TaskMessageStore(database).send(
        task.request.request_id,
        context,
        "cancel this merge update",
        at=NOW + timedelta(seconds=1),
    )
    proofs = tuple(
        ProjectMergeProof(
            snapshot.project.project_id,
            snapshot.project.repository,
            AUTO_MERGE_ALLOWED,
            snapshot.task_flow_state.current_commit,  # type: ignore[union-attr]
        )
        for snapshot in task.projects
    )
    store = TaskDatabaseProjectMergeStore(database_path)

    with pytest.raises(MergeRuntimeError, match="lifecycle"):
        store.prepare_barrier(task, proofs, occurred_at=NOW + timedelta(seconds=2))
    revisions = TaskRevisionService(database)
    revisions.cancel(
        receipt.revision_request_id,
        reason="user cancelled",
        at=NOW + timedelta(seconds=2),
    )
    revisions.resume(
        receipt.revision_request_id,
        at=NOW + timedelta(seconds=3),
    )

    store.prepare_barrier(task, proofs, occurred_at=NOW + timedelta(seconds=4))
