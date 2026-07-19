"""Live, fail-closed orchestration for one pass of Forge Task merging."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator, Protocol

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
    ProjectMergeProof,
    decide_project_group,
    decide_merge,
)
from .safe_files import SafeFilesEvidence, check_safe_files
from .task_flow import TaskFlowState, TaskFlowStatus, required_steps
from .task_options import MergeMode
from .task_service import TaskCreationRequest
from .task_database import TaskDatabase, TaskDatabaseError
from .task_projects import TaskProject
from .task_settings import (
    BranchRefreshIntent,
    TaskSettings,
    TaskSettingsStatus,
    task_content_hash,
)
from .task_settings_v2 import TaskRequestV2, TaskSettingsV2


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


class ProjectSnapshotGitHub(Protocol):
    def get_pr_write_state(self, pr_url: str) -> PullRequestWriteState: ...


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
class ProjectMergeSnapshot:
    """One exact v2 Project and its replayed, current flow proof."""

    request: TaskRequestV2
    settings: TaskSettingsV2
    project: TaskProject
    project_state: str
    task_flow_state: TaskFlowState | None
    merge_attempt_pending: bool = False


@dataclass(frozen=True, slots=True)
class ProjectMergeTask:
    """All Projects that share one immutable v2 parent settings record."""

    request: TaskRequestV2
    settings: TaskSettingsV2
    projects: tuple[ProjectMergeSnapshot, ...]


@dataclass(frozen=True, slots=True)
class _ProjectMergeRecovery:
    """Classification of a read after a possibly successful remote write."""

    status: str
    detail: str
    result: MergeWriteResult | None = None


class ProjectMergeStore(Protocol):
    def converge_observed_merge(
        self,
        task: ProjectMergeTask,
        snapshot: ProjectMergeSnapshot,
        result: MergeWriteResult,
        *,
        occurred_at: datetime,
    ) -> None: ...

    def mark_reconciliation_pending(
        self,
        task: ProjectMergeTask,
        snapshot: ProjectMergeSnapshot,
        *,
        occurred_at: datetime,
    ) -> None: ...

    def mark_failed(
        self,
        task: ProjectMergeTask,
        snapshot: ProjectMergeSnapshot,
        *,
        reason: str,
        occurred_at: datetime,
    ) -> None: ...

    def prepare_barrier(
        self,
        task: ProjectMergeTask,
        proofs: tuple[ProjectMergeProof, ...],
        *,
        occurred_at: datetime,
    ) -> None: ...

    def guard_project(
        self,
        task: ProjectMergeTask,
        snapshot: ProjectMergeSnapshot,
        *,
        expected_head_commit: str,
    ) -> AbstractContextManager[ProjectMergeGuard]: ...

    def finish_merged(
        self,
        task: ProjectMergeTask,
        *,
        occurred_at: datetime,
    ) -> None: ...

    def finish_partial(
        self,
        task: ProjectMergeTask,
        *,
        merged_project_ids: tuple[str, ...],
        failed_project_id: str,
        remaining_project_ids: tuple[str, ...],
        reason: str,
        occurred_at: datetime,
    ) -> None: ...


class ProjectMergeGuard(Protocol):
    def mark_merged(
        self,
        snapshot: ProjectMergeSnapshot,
        result: MergeWriteResult,
        *,
        occurred_at: datetime,
    ) -> None: ...


_V2_MERGE_BARRIERS = frozenset(
    {
        "revision_requested",
        "stop_requested",
        "changing",
        "stopping",
        "cancelled",
        "expired",
        "merged",
        "replaced",
        "partially_merged",
    }
)


class TaskDatabaseProjectMergeStore:
    """Transaction-scoped v2 merge proof and lifecycle store."""

    def __init__(self, path: str | Path) -> None:
        try:
            self._database = TaskDatabase(path)
        except TaskDatabaseError as error:
            raise MergeRuntimeError("v2 merge database could not be opened") from error

    def converge_observed_merge(
        self,
        task: ProjectMergeTask,
        snapshot: ProjectMergeSnapshot,
        result: MergeWriteResult,
        *,
        occurred_at: datetime,
    ) -> None:
        """Persist an exact remote merge without authorizing a new merge write."""

        state = snapshot.task_flow_state
        if state is None or not result.already_merged:
            raise MergeRuntimeError("observed Project merge proof is incomplete")
        _validate_merge_result(
            result,
            expected_base_commit=state.current_base_commit,
            expected_commit=state.current_commit,
        )
        timestamp = _v2_timestamp(occurred_at)
        with self._database.transaction() as connection:
            self._require_exact_task(connection, task)
            row = connection.execute(
                """
                SELECT state, pr_url, head_commit, merge_commit
                FROM task_projects
                WHERE request_id = ? AND project_id = ?
                  AND task_settings_hash = ?
                """,
                (
                    task.request.request_id,
                    snapshot.project.project_id,
                    task.settings.task_settings_hash,
                ),
            ).fetchone()
            if row is None:
                raise MergeRuntimeError("observed Project disappeared")
            if row[0] == "merged":
                if tuple(row[1:]) != (
                    state.pr_url,
                    state.current_commit,
                    result.merged_commit,
                ):
                    raise MergeRuntimeError("stored Project merge proof changed")
                return
            if (
                row[0] != snapshot.project_state
                or row[1] not in {None, state.pr_url}
                or row[2] not in {None, state.current_commit}
                or row[3] is not None
            ):
                raise MergeRuntimeError("stored Project observation target changed")
            updated = connection.execute(
                """
                UPDATE task_projects
                SET state = 'merged', pr_url = ?, head_commit = ?,
                    merge_commit = ?, updated_at = ?
                WHERE request_id = ? AND project_id = ?
                  AND task_settings_hash = ? AND state = ?
                  AND (pr_url IS NULL OR pr_url = ?)
                  AND (head_commit IS NULL OR head_commit = ?)
                  AND merge_commit IS NULL
                """,
                (
                    state.pr_url,
                    state.current_commit,
                    result.merged_commit,
                    timestamp,
                    task.request.request_id,
                    snapshot.project.project_id,
                    task.settings.task_settings_hash,
                    snapshot.project_state,
                    state.pr_url,
                    state.current_commit,
                ),
            )
            if updated.rowcount != 1:
                raise MergeRuntimeError("observed Project merge could not be recorded")

    def mark_reconciliation_pending(
        self,
        task: ProjectMergeTask,
        snapshot: ProjectMergeSnapshot,
        *,
        occurred_at: datetime,
    ) -> None:
        """Keep an indeterminate remote write non-terminal and retryable."""

        state = snapshot.task_flow_state
        if state is None:
            raise MergeRuntimeError("pending Project has no durable merge proof")
        timestamp = _v2_timestamp(occurred_at)
        with self._database.transaction() as connection:
            self._require_exact_task(connection, task)
            row = connection.execute(
                """
                SELECT state, pr_url, head_commit, merge_commit
                FROM task_projects
                WHERE request_id = ? AND project_id = ?
                  AND task_settings_hash = ?
                """,
                (
                    task.request.request_id,
                    snapshot.project.project_id,
                    task.settings.task_settings_hash,
                ),
            ).fetchone()
            if row is None:
                raise MergeRuntimeError("pending Project disappeared")
            if row[0] == "merged" and row[3] is not None:
                return
            if (
                row[0] not in {snapshot.project_state, "waiting_for_help"}
                or row[1] not in {None, state.pr_url}
                or row[2] not in {None, state.current_commit}
                or row[3] is not None
            ):
                raise MergeRuntimeError("pending Project proof changed")
            updated = connection.execute(
                """
                UPDATE task_projects
                SET state = 'waiting_for_help', pr_url = ?, head_commit = ?,
                    updated_at = ?
                WHERE request_id = ? AND project_id = ?
                  AND task_settings_hash = ?
                  AND state IN ('ready', 'running', 'reviewing',
                                'waiting_for_help')
                  AND (pr_url IS NULL OR pr_url = ?)
                  AND (head_commit IS NULL OR head_commit = ?)
                  AND merge_commit IS NULL
                """,
                (
                    state.pr_url,
                    state.current_commit,
                    timestamp,
                    task.request.request_id,
                    snapshot.project.project_id,
                    task.settings.task_settings_hash,
                    state.pr_url,
                    state.current_commit,
                ),
            )
            if updated.rowcount != 1:
                raise MergeRuntimeError("pending Project could not be recorded")

    def mark_failed(
        self,
        task: ProjectMergeTask,
        snapshot: ProjectMergeSnapshot,
        *,
        reason: str,
        occurred_at: datetime,
    ) -> None:
        """Record a failure only after exact remote evidence says unmerged."""

        if not isinstance(reason, str) or not reason.strip():
            raise MergeRuntimeError("failed Project reason is missing")
        timestamp = _v2_timestamp(occurred_at)
        with self._database.transaction() as connection:
            self._require_exact_task(connection, task)
            row = connection.execute(
                """
                SELECT state, merge_commit FROM task_projects
                WHERE request_id = ? AND project_id = ?
                  AND task_settings_hash = ?
                """,
                (
                    task.request.request_id,
                    snapshot.project.project_id,
                    task.settings.task_settings_hash,
                ),
            ).fetchone()
            if row is None or row[0] == "merged" or row[1] is not None:
                raise MergeRuntimeError("failed Project state is not exact")
            if row[0] == "failed":
                return
            updated = connection.execute(
                """
                UPDATE task_projects SET state = 'failed', updated_at = ?
                WHERE request_id = ? AND project_id = ?
                  AND task_settings_hash = ?
                  AND state IN ('ready', 'running', 'reviewing',
                                'waiting_for_help')
                  AND merge_commit IS NULL
                """,
                (
                    timestamp,
                    task.request.request_id,
                    snapshot.project.project_id,
                    task.settings.task_settings_hash,
                ),
            )
            if updated.rowcount != 1:
                raise MergeRuntimeError("failed Project could not be recorded")

    def prepare_barrier(
        self,
        task: ProjectMergeTask,
        proofs: tuple[ProjectMergeProof, ...],
        *,
        occurred_at: datetime,
    ) -> None:
        proof_by_id = {proof.project_id: proof for proof in proofs}
        if len(proof_by_id) != len(task.projects) or set(proof_by_id) != {
            snapshot.project.project_id for snapshot in task.projects
        }:
            raise MergeRuntimeError("Project barrier proofs are not exact")
        timestamp = _v2_timestamp(occurred_at)
        with self._database.transaction() as connection:
            self._require_exact_task(connection, task)
            for snapshot in task.projects:
                proof = proof_by_id[snapshot.project.project_id]
                state = snapshot.task_flow_state
                if (
                    state is None
                    or proof.decision != AUTO_MERGE_ALLOWED
                    or proof.repository != snapshot.project.repository
                    or proof.expected_head_commit != state.current_commit
                ):
                    raise MergeRuntimeError("Project barrier proof is not mergeable")
                row = connection.execute(
                    """
                    SELECT state, pr_url, head_commit FROM task_projects
                    WHERE request_id = ? AND project_id = ?
                    """,
                    (task.request.request_id, snapshot.project.project_id),
                ).fetchone()
                if (
                    row is None
                    or row[0] != snapshot.project_state
                    or row[1] not in {None, state.pr_url}
                    or row[2] not in {None, state.current_commit}
                ):
                    raise MergeRuntimeError("stored Project merge proof changed")
                updated = connection.execute(
                    """
                    UPDATE task_projects
                    SET pr_url = ?, head_commit = ?, updated_at = ?
                    WHERE request_id = ? AND project_id = ?
                      AND task_settings_hash = ?
                      AND state IN ('ready', 'running', 'reviewing', 'merged')
                      AND (pr_url IS NULL OR pr_url = ?)
                      AND (head_commit IS NULL OR head_commit = ?)
                    """,
                    (
                        state.pr_url,
                        state.current_commit,
                        timestamp,
                        task.request.request_id,
                        snapshot.project.project_id,
                        task.settings.task_settings_hash,
                        state.pr_url,
                        state.current_commit,
                    ),
                )
                if updated.rowcount != 1:
                    raise MergeRuntimeError("Project barrier proof update failed")

    @contextmanager
    def guard_project(
        self,
        task: ProjectMergeTask,
        snapshot: ProjectMergeSnapshot,
        *,
        expected_head_commit: str,
    ) -> Iterator[ProjectMergeGuard]:
        state = snapshot.task_flow_state
        if state is None or expected_head_commit != state.current_commit:
            raise MergeRuntimeError("Project guard head is not the replayed proof")
        with self._database.transaction() as connection:
            self._require_exact_task(connection, task)
            row = connection.execute(
                """
                SELECT state, pr_url, head_commit, merge_commit
                FROM task_projects
                WHERE request_id = ? AND project_id = ?
                  AND task_settings_hash = ?
                """,
                (
                    task.request.request_id,
                    snapshot.project.project_id,
                    task.settings.task_settings_hash,
                ),
            ).fetchone()
            if (
                row is None
                or row[0] != snapshot.project_state
                or row[1] != state.pr_url
                or row[2] != expected_head_commit
                or (row[0] != "merged" and row[3] is not None)
            ):
                raise MergeRuntimeError(
                    "Project state or head changed before the merge write"
                )
            yield _TaskDatabaseProjectMergeGuard(connection, task, snapshot)

    def finish_merged(
        self,
        task: ProjectMergeTask,
        *,
        occurred_at: datetime,
    ) -> None:
        timestamp = _v2_timestamp(occurred_at)
        with self._database.transaction() as connection:
            self._require_exact_task(connection, task)
            states = connection.execute(
                """
                SELECT project_id, state, merge_commit FROM task_projects
                WHERE request_id = ? ORDER BY project_id
                """,
                (task.request.request_id,),
            ).fetchall()
            if len(states) != len(task.projects) or any(
                row[1] != "merged" or row[2] is None for row in states
            ):
                raise MergeRuntimeError("not every Project is durably merged")
            self._ensure_terminal_event(
                connection,
                task,
                event_type="merged",
                payload={
                    "merged_project_ids": [row[0] for row in states],
                    "task_settings_hash": task.settings.task_settings_hash,
                },
                occurred_at=timestamp,
            )

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
        if not merged_project_ids or not isinstance(reason, str) or not reason.strip():
            raise MergeRuntimeError("partial merge evidence is incomplete")
        all_ids = {snapshot.project.project_id for snapshot in task.projects}
        supplied = {*merged_project_ids, failed_project_id, *remaining_project_ids}
        if supplied != all_ids or len(supplied) != (
            len(merged_project_ids) + 1 + len(remaining_project_ids)
        ):
            raise MergeRuntimeError("partial merge Project sets are not exact")
        timestamp = _v2_timestamp(occurred_at)
        with self._database.transaction() as connection:
            self._require_exact_task(connection, task)
            rows = connection.execute(
                """
                SELECT project_id, state, merge_commit FROM task_projects
                WHERE request_id = ? ORDER BY project_id
                """,
                (task.request.request_id,),
            ).fetchall()
            state_by_id = {row[0]: (row[1], row[2]) for row in rows}
            if any(
                state_by_id[project_id][0] != "merged"
                or state_by_id[project_id][1] is None
                for project_id in merged_project_ids
            ):
                raise MergeRuntimeError("partial merge lacks exact merged readback")
            if state_by_id[failed_project_id][0] == "merged" or any(
                state_by_id[project_id][0] == "merged"
                for project_id in remaining_project_ids
            ):
                raise MergeRuntimeError("partial merge failed or remaining set is wrong")
            updated = connection.execute(
                """
                UPDATE task_projects SET state = 'failed', updated_at = ?
                WHERE request_id = ? AND project_id = ?
                  AND state IN ('ready', 'running', 'reviewing',
                                'waiting_for_help')
                  AND merge_commit IS NULL
                """,
                (timestamp, task.request.request_id, failed_project_id),
            )
            if updated.rowcount != 1:
                raise MergeRuntimeError("failed Project could not be recorded")
            self._ensure_terminal_event(
                connection,
                task,
                event_type="partially_merged",
                payload={
                    "failed_project_id": failed_project_id,
                    "merged_project_ids": list(merged_project_ids),
                    "reason": reason,
                    "remaining_project_ids": list(remaining_project_ids),
                    "task_settings_hash": task.settings.task_settings_hash,
                },
                occurred_at=timestamp,
            )

    @staticmethod
    def _require_exact_task(
        connection: sqlite3.Connection,
        task: ProjectMergeTask,
    ) -> None:
        request = connection.execute(
            """
            SELECT request_json, request_hash, management_repository,
                   task_owner_host, confirmed_by
            FROM task_requests WHERE request_id = ?
            """,
            (task.request.request_id,),
        ).fetchone()
        if request is None or tuple(request) != (
            task.request.to_json(),
            task.request.request_hash,
            task.request.management_repository,
            task.request.task_owner_host,
            task.request.confirmed_by,
        ):
            raise MergeRuntimeError("v2 merge request changed")
        settings = connection.execute(
            """
            SELECT settings_json, request_hash, management_repository,
                   parent_issue_number, task_owner_host
            FROM task_settings_v2 WHERE request_id = ?
            """,
            (task.request.request_id,),
        ).fetchone()
        if settings is None or tuple(settings) != (
            task.settings.to_json(),
            task.settings.request_hash,
            task.settings.management_repository,
            task.settings.parent_issue_number,
            task.settings.task_owner_host,
        ):
            raise MergeRuntimeError("v2 merge settings changed")
        barriers = connection.execute(
            """
            SELECT event_type FROM task_events
            WHERE request_id = ?
            ORDER BY event_id
            """,
            (task.request.request_id,),
        ).fetchall()
        event_types = [row[0] for row in barriers]
        if event_types.count("active") != 1 or any(
            event_type in _V2_MERGE_BARRIERS for event_type in event_types
        ):
            raise MergeRuntimeError("v2 merge is blocked by lifecycle state")
        active = connection.execute(
            """
            SELECT task_settings_hash, project_id, event_key, event_json
            FROM task_events
            WHERE request_id = ? AND event_type = 'active'
            """,
            (task.request.request_id,),
        ).fetchone()
        active_json = json.dumps(
            {"task_settings_hash": task.settings.task_settings_hash},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        if active is None or tuple(active) != (
            task.settings.task_settings_hash,
            None,
            "active",
            active_json,
        ):
            raise MergeRuntimeError("v2 active merge event changed")
        request_payload = json.loads(task.request.to_json())
        canonical_projects = {
            item["project_id"]: json.dumps(
                item,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            for item in request_payload["projects"]
        }
        project_rows = connection.execute(
            """
            SELECT project_id, task_settings_hash, project_json, state,
                   root_card_id
            FROM task_projects WHERE request_id = ? ORDER BY project_id
            """,
            (task.request.request_id,),
        ).fetchall()
        if len(project_rows) != len(task.projects):
            raise MergeRuntimeError("v2 Project registry count changed")
        for row in project_rows:
            if (
                row[0] not in canonical_projects
                or row[1] != task.settings.task_settings_hash
                or row[2] != canonical_projects[row[0]]
                or row[3]
                not in {
                    "ready",
                    "running",
                    "reviewing",
                    "waiting_for_help",
                    "failed",
                    "merged",
                }
                or not isinstance(row[4], str)
                or not row[4]
            ):
                raise MergeRuntimeError("v2 Project registry changed")

    @staticmethod
    def _ensure_terminal_event(
        connection: sqlite3.Connection,
        task: ProjectMergeTask,
        *,
        event_type: str,
        payload: Mapping[str, object],
        occurred_at: str,
    ) -> None:
        event_json = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        existing = connection.execute(
            """
            SELECT task_settings_hash, project_id, event_type, event_json,
                   occurred_at
            FROM task_events WHERE request_id = ? AND event_key = ?
            """,
            (task.request.request_id, event_type),
        ).fetchone()
        expected = (
            task.settings.task_settings_hash,
            None,
            event_type,
            event_json,
            occurred_at,
        )
        if existing is None:
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
                    event_type,
                    event_json,
                    occurred_at,
                ),
            )
        elif tuple(existing) != expected:
            raise MergeRuntimeError("v2 terminal merge event does not match")


@dataclass(slots=True)
class _TaskDatabaseProjectMergeGuard:
    connection: sqlite3.Connection
    task: ProjectMergeTask
    expected: ProjectMergeSnapshot

    def mark_merged(
        self,
        snapshot: ProjectMergeSnapshot,
        result: MergeWriteResult,
        *,
        occurred_at: datetime,
    ) -> None:
        if snapshot != self.expected:
            raise MergeRuntimeError("Project merge result targets another snapshot")
        state = snapshot.task_flow_state
        if state is None:
            raise MergeRuntimeError("Project merge result has no replayed proof")
        timestamp = _v2_timestamp(occurred_at)
        row = self.connection.execute(
            """
            SELECT state, merge_commit FROM task_projects
            WHERE request_id = ? AND project_id = ?
            """,
            (self.task.request.request_id, snapshot.project.project_id),
        ).fetchone()
        if row is None:
            raise MergeRuntimeError("Project disappeared after merge readback")
        if row[0] == "merged":
            if row[1] != result.merged_commit:
                raise MergeRuntimeError("stored Project merge commit changed")
            return
        updated = self.connection.execute(
            """
            UPDATE task_projects
            SET state = 'merged', merge_commit = ?, updated_at = ?
            WHERE request_id = ? AND project_id = ?
              AND task_settings_hash = ?
              AND state IN ('ready', 'running', 'reviewing')
              AND pr_url = ? AND head_commit = ? AND merge_commit IS NULL
            """,
            (
                result.merged_commit,
                timestamp,
                self.task.request.request_id,
                snapshot.project.project_id,
                self.task.settings.task_settings_hash,
                state.pr_url,
                result.expected_commit,
            ),
        )
        if updated.rowcount != 1:
            raise MergeRuntimeError("Project merge result could not be recorded")


def _v2_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MergeRuntimeError("v2 merge timestamp must include a timezone")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class TaskMergeReport:
    request_id: str
    issue_number: int | None
    decision: str
    action: str
    reason: str
    pr_url: str | None
    tested_commit: str | None
    project_id: str | None = None
    repository: str | None = None
    merge_commit: str | None = None

    @property
    def ok(self) -> bool:
        return self.action not in {"error", "reconciliation_pending"}

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "request_id": self.request_id,
            "issue_number": self.issue_number,
            "decision": self.decision,
            "action": self.action,
            "reason": self.reason,
            "pr_url": self.pr_url,
            "tested_commit": self.tested_commit,
        }
        if self.project_id is not None:
            payload["project_id"] = self.project_id
        if self.repository is not None:
            payload["repository"] = self.repository
        if self.merge_commit is not None:
            payload["merge_commit"] = self.merge_commit
        return payload


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


def run_project_merge_tasks(
    tasks: Sequence[ProjectMergeTask],
    *,
    evidence_reader: MergeEvidenceReader,
    merge_writer: MergeWriter,
    merge_store: ProjectMergeStore,
    required_check: str,
    environment: Mapping[str, str],
    clock: Callable[[], datetime],
) -> MergeRunReport:
    """Run v2 parent barriers without changing the exact v1 merge path."""

    if isinstance(tasks, (str, bytes)) or not isinstance(tasks, Sequence):
        raise MergeRuntimeError("Project merge tasks must be a sequence")
    if not isinstance(required_check, str) or not required_check.strip():
        raise MergeRuntimeError("required check must be non-empty text")
    if not isinstance(environment, Mapping):
        raise MergeRuntimeError("environment must be a mapping")
    if not callable(clock):
        raise MergeRuntimeError("clock must be callable")
    reports: list[TaskMergeReport] = []
    for task in tasks:
        try:
            reports.extend(
                _run_project_task(
                    task,
                    evidence_reader=evidence_reader,
                    merge_writer=merge_writer,
                    merge_store=merge_store,
                    required_check=required_check,
                    environment=environment,
                    clock=clock,
                )
            )
        except Exception as error:
            request_id = getattr(getattr(task, "request", None), "request_id", "unknown")
            issue_number = getattr(
                getattr(task, "settings", None),
                "parent_issue_number",
                None,
            )
            reports.append(
                TaskMergeReport(
                    request_id=request_id,
                    issue_number=issue_number,
                    decision=CHECK_ERROR,
                    action="error",
                    reason=str(error) or "unexpected Project merge error",
                    pr_url=None,
                    tested_commit=None,
                )
            )
    return MergeRunReport(tasks=tuple(reports))


def load_project_merge_tasks(
    *,
    settings_db: str | Path,
    hermes_db: str | Path,
    github: ProjectSnapshotGitHub,
) -> tuple[ProjectMergeTask, ...]:
    """Rebuild active v2 Project flow proofs from DB, Hermes, and GitHub."""

    from .contracts import parse_task_result  # noqa: PLC0415
    from .hermes import (  # noqa: PLC0415
        HermesStore,
        project_task_card_key,
    )
    from .task_runtime import (  # noqa: PLC0415
        ProjectRuntimeSnapshot,
        _project_cards,
        _replay_project_flow,
    )
    from .task_settings_v2 import TaskSettingsV2Error  # noqa: PLC0415

    database = TaskDatabase(settings_db)
    hermes = HermesStore(hermes_db)
    all_cards = hermes.list_project_runtime_cards()
    with database.read() as connection:
        rows = connection.execute(
            """
            SELECT r.request_json, s.settings_json, p.project_id,
                   p.project_json, p.state, p.root_card_id, p.branch_name,
                   p.worktree_path, p.pr_url, p.head_commit, p.merge_commit
            FROM task_settings_v2 AS s
            JOIN task_requests AS r ON r.request_id = s.request_id
            JOIN task_projects AS p
              ON p.request_id = s.request_id
             AND p.task_settings_hash = s.task_settings_hash
            WHERE EXISTS (
                SELECT 1 FROM task_events AS active_event
                WHERE active_event.request_id = s.request_id
                  AND active_event.task_settings_hash = s.task_settings_hash
                  AND active_event.event_type = 'active'
            )
              AND NOT EXISTS (
                SELECT 1 FROM task_events AS barrier_event
                WHERE barrier_event.request_id = s.request_id
                  AND barrier_event.event_type IN (
                      'revision_requested', 'stop_requested', 'changing',
                      'stopping', 'cancelled', 'expired', 'merged',
                      'replaced', 'partially_merged'
                  )
            )
            ORDER BY r.request_id, p.project_id
            """
        ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        try:
            request = TaskRequestV2.from_json(row[0])
            settings = TaskSettingsV2.from_json(row[1], request=request)
        except TaskSettingsV2Error as error:
            raise MergeRuntimeError("stored v2 merge settings are invalid") from error
        grouped.setdefault(request.request_id, []).append(row)

    tasks: list[ProjectMergeTask] = []
    for request_id in sorted(grouped):
        task_rows = grouped[request_id]
        request = TaskRequestV2.from_json(task_rows[0][0])
        settings = TaskSettingsV2.from_json(task_rows[0][1], request=request)
        canonical_projects = {
            item["project_id"]: json.dumps(
                item,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            for item in json.loads(request.to_json())["projects"]
        }
        snapshots: list[ProjectMergeSnapshot] = []
        for row in task_rows:
            project_id = row[2]
            matches = tuple(
                project for project in settings.projects if project.project_id == project_id
            )
            if (
                len(matches) != 1
                or project_id not in canonical_projects
                or row[3] != canonical_projects[project_id]
                or not isinstance(row[4], str)
            ):
                raise MergeRuntimeError("stored v2 Project row is invalid")
            project = matches[0]
            runtime_snapshot = ProjectRuntimeSnapshot(
                request=request,
                settings=settings,
                project=project,
                project_state=row[4],
                branch_name=row[6],
                worktree_path=row[7],
            )
            state: TaskFlowState | None = None
            stored_pr_url, stored_head, merge_commit = row[8], row[9], row[10]
            merge_attempt_pending = row[4] == "waiting_for_help"
            if (stored_pr_url is None) != (stored_head is None):
                raise MergeRuntimeError("stored Project merge proof is incomplete")
            if row[4] == "merged" or merge_commit is not None:
                if (
                    row[4] != "merged"
                    or not isinstance(stored_pr_url, str)
                    or not isinstance(stored_head, str)
                    or not isinstance(merge_commit, str)
                ):
                    raise MergeRuntimeError("stored merged Project proof is incomplete")
                pr = github.get_pr_write_state(stored_pr_url)
                if (
                    not pr.is_merged
                    or pr.repository != project.repository
                    or pr.base_ref != project.base_branch
                    or pr.merged_base_commit != project.base_commit
                    or pr.merged_head_commit != stored_head
                    or pr.merged_commit != merge_commit
                ):
                    raise MergeRuntimeError("merged Project readback changed")
                state = _ready_project_state(
                    settings,
                    pr_url=stored_pr_url,
                    base_commit=project.base_commit,
                    head_commit=stored_head,
                )
            elif merge_attempt_pending:
                if not isinstance(stored_pr_url, str) or not isinstance(
                    stored_head, str
                ):
                    raise MergeRuntimeError(
                        "pending Project merge proof is incomplete"
                    )
                candidate = github.get_pr_write_state(stored_pr_url)
                if (
                    candidate.pr_url != stored_pr_url
                    or candidate.repository != project.repository
                    or candidate.base_ref != project.base_branch
                    or candidate.base_commit != project.base_commit
                    or candidate.head_commit != stored_head
                    or (
                        candidate.is_merged
                        and (
                            candidate.merged_base_commit != project.base_commit
                            or candidate.merged_head_commit != stored_head
                            or candidate.merged_commit is None
                        )
                    )
                    or (
                        not candidate.is_merged
                        and any(
                            value is not None
                            for value in (
                                candidate.merged_commit,
                                candidate.merged_base_commit,
                                candidate.merged_head_commit,
                            )
                        )
                    )
                ):
                    raise MergeRuntimeError(
                        "pending Project remote proof does not match"
                    )
                state = _ready_project_state(
                    settings,
                    pr_url=stored_pr_url,
                    base_commit=project.base_commit,
                    head_commit=stored_head,
                )
            else:
                recovered_pr = None
                if isinstance(stored_pr_url, str) and isinstance(stored_head, str):
                    candidate = github.get_pr_write_state(stored_pr_url)
                    if candidate.is_merged:
                        if (
                            candidate.repository != project.repository
                            or candidate.base_ref != project.base_branch
                            or candidate.merged_base_commit != project.base_commit
                            or candidate.merged_head_commit != stored_head
                            or candidate.merged_commit is None
                        ):
                            raise MergeRuntimeError(
                                "remote Project merge does not match durable proof"
                            )
                        recovered_pr = candidate
                        state = _ready_project_state(
                            settings,
                            pr_url=stored_pr_url,
                            base_commit=project.base_commit,
                            head_commit=stored_head,
                        )
                project_cards = _project_cards(all_cards, runtime_snapshot)
                replay = None
                if recovered_pr is None:
                    replay = _replay_project_flow(
                        runtime_snapshot,
                        project_cards,
                        hermes=hermes,
                        github=github,
                    )
                if (
                    replay is not None
                    and replay.status == TaskFlowStatus.READY_TO_MERGE.value
                ):
                    root_key = project_task_card_key(request.request_id, project_id)
                    roots = [card for card in project_cards if card.idempotency_key == root_key]
                    if len(roots) != 1:
                        raise MergeRuntimeError("Project Build root is not exact")
                    runs = hermes.completed_runs(roots[0].task_id)
                    if len(runs) != 1:
                        raise MergeRuntimeError("Project Build proof is not exact")
                    build = parse_task_result("build", runs[0].summary)
                    pr = github.get_pr_write_state(build.pr_url)
                    if (
                        pr.repository != project.repository
                        or pr.base_ref != project.base_branch
                        or pr.is_merged
                        or not pr.is_open
                    ):
                        raise MergeRuntimeError("Project pull request changed after replay")
                    state = _ready_project_state(
                        settings,
                        pr_url=pr.pr_url,
                        base_commit=pr.base_commit,
                        head_commit=pr.head_commit,
                    )
            snapshots.append(
                ProjectMergeSnapshot(
                    request=request,
                    settings=settings,
                    project=project,
                    project_state=row[4],
                    task_flow_state=state,
                    merge_attempt_pending=merge_attempt_pending,
                )
            )
        task = ProjectMergeTask(
            request=request,
            settings=settings,
            projects=tuple(snapshots),
        )
        _validate_project_task(task)
        tasks.append(task)
    return tuple(tasks)


def _ready_project_state(
    settings: TaskSettingsV2,
    *,
    pr_url: str,
    base_commit: str,
    head_commit: str,
) -> TaskFlowState:
    return TaskFlowState(
        task_flow=settings.task_flow,
        task_settings_hash=settings.task_settings_hash,
        pr_url=pr_url,
        current_base_commit=base_commit,
        current_commit=head_commit,
        current_step=None,
        status=TaskFlowStatus.READY_TO_MERGE,
        step_running=False,
        completed_steps=required_steps(settings.task_flow),
    )


def _run_project_task(
    task: ProjectMergeTask,
    *,
    evidence_reader: MergeEvidenceReader,
    merge_writer: MergeWriter,
    merge_store: ProjectMergeStore,
    required_check: str,
    environment: Mapping[str, str],
    clock: Callable[[], datetime],
) -> tuple[TaskMergeReport, ...]:
    _validate_project_task(task)
    settings = task.settings
    initial_proofs: list[ProjectMergeProof] = []
    initial_evidence: dict[str, GitHubMergeEvidence] = {}
    include_safe_files = (
        settings.merge_mode is MergeMode.SAFE_AUTO and len(task.projects) == 1
    )
    for snapshot in task.projects:
        state = snapshot.task_flow_state
        if state is None:
            initial_proofs.append(
                ProjectMergeProof(
                    project_id=snapshot.project.project_id,
                    repository=snapshot.project.repository,
                    decision=WAIT,
                    expected_head_commit="0" * 40,
                )
            )
            continue
        evidence = evidence_reader.get_merge_evidence(
            state.pr_url,
            (required_check,),
            include_safe_files=include_safe_files,
        )
        initial_evidence[snapshot.project.project_id] = evidence
        initial_proofs.append(
            _project_merge_proof(
                snapshot,
                evidence,
                required_check=required_check,
                now=_read_time(clock),
            )
        )
    proofs = tuple(initial_proofs)
    if any(snapshot.merge_attempt_pending for snapshot in task.projects):
        return _resolve_pending_project_task(
            task,
            proofs=proofs,
            evidence_by_project=initial_evidence,
            merge_store=merge_store,
            clock=clock,
        )

    group = decide_project_group(settings, proofs)
    automatic_writes_enabled = environment.get("AUTO_MERGE_ENABLED") == "true"
    expires_at = settings.auto_merge_expires_at
    permission_active = expires_at is not None and _read_time(clock) < expires_at
    if (
        group.code != AUTO_MERGE_ALLOWED
        or not automatic_writes_enabled
        or not permission_active
    ):
        permission_expired = (
            group.code == AUTO_MERGE_ALLOWED and not permission_active
        )
        return _converge_without_merge_writes(
            task,
            proofs=proofs,
            evidence_by_project=initial_evidence,
            merge_store=merge_store,
            decision=group.code,
            reason=(
                group.reason
                if group.code != AUTO_MERGE_ALLOWED
                else (
                    "automatic merge permission expired"
                    if permission_expired
                    else "automatic merge is disabled"
                )
            ),
            disabled=(
                group.code == AUTO_MERGE_ALLOWED and not automatic_writes_enabled
            ),
            clock=clock,
        )

    by_project = {snapshot.project.project_id: snapshot for snapshot in task.projects}
    proof_by_project = {proof.project_id: proof for proof in proofs}
    barrier_time = _read_time(clock)
    merge_store.prepare_barrier(task, proofs, occurred_at=barrier_time)
    reports: list[TaskMergeReport] = []
    merged_ids: list[str] = []
    order = group.ordered_project_ids
    for index, project_id in enumerate(order):
        snapshot = by_project[project_id]
        proof = proof_by_project[project_id]
        state = snapshot.task_flow_state
        assert state is not None
        write_started = False
        try:
            # RISK(race): each remote write is enclosed by a fresh exact
            # settings/revision/Project/head guard. Cross-repository merging
            # cannot be atomic, so a later failure becomes partially_merged.
            with merge_store.guard_project(
                task,
                snapshot,
                expected_head_commit=proof.expected_head_commit,
            ) as guard:
                write_time = _read_time(clock)
                fresh = evidence_reader.get_merge_evidence(
                    state.pr_url,
                    (required_check,),
                    include_safe_files=include_safe_files,
                )
                fresh_proof = _project_merge_proof(
                    snapshot,
                    fresh,
                    required_check=required_check,
                    now=write_time,
                )
                if (
                    fresh_proof.decision != AUTO_MERGE_ALLOWED
                    or fresh_proof.expected_head_commit != proof.expected_head_commit
                ):
                    raise MergeRuntimeError(
                        "Project merge proof changed before its ordered write"
                    )
                if fresh_proof.already_merged:
                    result = _observed_project_merge_result(fresh)
                else:
                    write_started = True
                    result = merge_writer.merge_expected_commit(
                        state.pr_url,
                        proof.expected_head_commit,
                        expected_base_commit=state.current_base_commit,
                    )
                _validate_merge_result(
                    result,
                    expected_base_commit=state.current_base_commit,
                    expected_commit=proof.expected_head_commit,
                )
                guard.mark_merged(snapshot, result, occurred_at=write_time)
            merged_ids.append(project_id)
            reports.append(
                _project_report(
                    snapshot,
                    decision=AUTO_MERGE_ALLOWED,
                    action=("observed_merge" if result.already_merged else "merged"),
                    reason="Project merged at its confirmed dependency position",
                    tested_commit=proof.expected_head_commit,
                    merge_commit=result.merged_commit,
                )
            )
        except Exception as error:
            failure_detail = str(error) or "Project merge failed"
            recovery = _read_project_recovery(
                snapshot,
                evidence_reader=evidence_reader,
                required_check=required_check,
                clock=clock,
            )
            if recovery.status == "merged":
                assert recovery.result is not None
                merge_store.converge_observed_merge(
                    task,
                    snapshot,
                    recovery.result,
                    occurred_at=_read_time(clock),
                )
                merged_ids.append(project_id)
                reports.append(
                    _project_report(
                        snapshot,
                        decision=AUTO_MERGE_ALLOWED,
                        action="observed_merge",
                        reason="Project merge recovered by exact remote readback",
                        tested_commit=proof.expected_head_commit,
                        merge_commit=recovery.result.merged_commit,
                    )
                )
                continue
            failure_detail = f"{failure_detail}; remote readback: {recovery.detail}"
            remaining = tuple(order[index + 1 :])
            if recovery.status == "unknown":
                merge_store.mark_reconciliation_pending(
                    task,
                    snapshot,
                    occurred_at=_read_time(clock),
                )
                reports.append(
                    _project_report(
                        snapshot,
                        decision=CHECK_ERROR,
                        action="reconciliation_pending",
                        reason=(
                            "merge result is unknown; no later Project merge was "
                            f"started; detail={failure_detail}"
                        ),
                        tested_commit=proof.expected_head_commit,
                    )
                )
                reports.extend(
                    _project_report(
                        by_project[remaining_id],
                        decision=WAIT,
                        action="none",
                        reason=(
                            "automatic merge stopped until the earlier Project "
                            "is reconciled"
                        ),
                        tested_commit=proof_by_project[
                            remaining_id
                        ].expected_head_commit,
                    )
                    for remaining_id in remaining
                )
                return tuple(reports)

            if merged_ids:
                merge_store.finish_partial(
                    task,
                    merged_project_ids=tuple(merged_ids),
                    failed_project_id=project_id,
                    remaining_project_ids=remaining,
                    reason=failure_detail,
                    occurred_at=_read_time(clock),
                )
            elif write_started:
                merge_store.mark_failed(
                    task,
                    snapshot,
                    reason=failure_detail,
                    occurred_at=_read_time(clock),
                )
            reports.append(
                _project_report(
                    snapshot,
                    decision=CHECK_ERROR,
                    action="error",
                    reason=(
                        f"merge failed; merged={','.join(merged_ids) or 'none'}; "
                        f"failed={project_id}; remaining={','.join(remaining) or 'none'}; "
                        f"detail={failure_detail}"
                    ),
                    tested_commit=proof.expected_head_commit,
                )
            )
            reports.extend(
                _project_report(
                    by_project[remaining_id],
                    decision=WAIT,
                    action="none",
                    reason="automatic merge stopped after an earlier Project failure",
                    tested_commit=proof_by_project[remaining_id].expected_head_commit,
                )
                for remaining_id in remaining
            )
            return tuple(reports)
    merge_store.finish_merged(task, occurred_at=_read_time(clock))
    return tuple(reports)


def _converge_without_merge_writes(
    task: ProjectMergeTask,
    *,
    proofs: tuple[ProjectMergeProof, ...],
    evidence_by_project: Mapping[str, GitHubMergeEvidence],
    merge_store: ProjectMergeStore,
    decision: str,
    reason: str,
    disabled: bool,
    clock: Callable[[], datetime],
) -> tuple[TaskMergeReport, ...]:
    """Apply exact remote observations while keeping policy write-free."""

    proof_by_project = {proof.project_id: proof for proof in proofs}
    reports: list[TaskMergeReport] = []
    every_project_merged = True
    for snapshot in task.projects:
        project_id = snapshot.project.project_id
        proof = proof_by_project[project_id]
        state = snapshot.task_flow_state
        if proof.already_merged:
            result = _observed_project_merge_result(
                evidence_by_project[project_id]
            )
            assert state is not None
            _validate_merge_result(
                result,
                expected_base_commit=state.current_base_commit,
                expected_commit=state.current_commit,
            )
            merge_store.converge_observed_merge(
                task,
                snapshot,
                result,
                occurred_at=_read_time(clock),
            )
            reports.append(
                _project_report(
                    snapshot,
                    decision=AUTO_MERGE_ALLOWED,
                    action="observed_merge",
                    reason="exact remote Project merge was recorded",
                    tested_commit=proof.expected_head_commit,
                    merge_commit=result.merged_commit,
                )
            )
            continue
        every_project_merged = False
        reports.append(
            _project_report(
                snapshot,
                decision=decision,
                action=(
                    "disabled"
                    if disabled
                    else ("error" if decision == CHECK_ERROR else "none")
                ),
                reason=reason,
                tested_commit=(None if state is None else state.current_commit),
            )
        )
    if every_project_merged:
        merge_store.finish_merged(task, occurred_at=_read_time(clock))
    return tuple(reports)


def _resolve_pending_project_task(
    task: ProjectMergeTask,
    *,
    proofs: tuple[ProjectMergeProof, ...],
    evidence_by_project: Mapping[str, GitHubMergeEvidence],
    merge_store: ProjectMergeStore,
    clock: Callable[[], datetime],
) -> tuple[TaskMergeReport, ...]:
    """Reconcile an earlier unknown write before authorizing any later write."""

    proof_by_project = {proof.project_id: proof for proof in proofs}
    observed: dict[str, MergeWriteResult] = {}
    for snapshot in task.projects:
        project_id = snapshot.project.project_id
        proof = proof_by_project[project_id]
        if not proof.already_merged:
            continue
        result = _observed_project_merge_result(evidence_by_project[project_id])
        state = snapshot.task_flow_state
        assert state is not None
        _validate_merge_result(
            result,
            expected_base_commit=state.current_base_commit,
            expected_commit=state.current_commit,
        )
        merge_store.converge_observed_merge(
            task,
            snapshot,
            result,
            occurred_at=_read_time(clock),
        )
        observed[project_id] = result

    pending = tuple(
        snapshot for snapshot in task.projects if snapshot.merge_attempt_pending
    )
    unknown = tuple(
        snapshot
        for snapshot in pending
        if snapshot.project.project_id not in observed
        and not _is_exact_unmerged_project_evidence(
            snapshot,
            evidence_by_project.get(snapshot.project.project_id),
        )
    )
    if unknown:
        unknown_ids = {snapshot.project.project_id for snapshot in unknown}
        for snapshot in unknown:
            merge_store.mark_reconciliation_pending(
                task,
                snapshot,
                occurred_at=_read_time(clock),
            )
        return tuple(
            _pending_resolution_report(
                snapshot,
                proof=proof_by_project[snapshot.project.project_id],
                observed=observed.get(snapshot.project.project_id),
                reconciliation_pending=(
                    snapshot.project.project_id in unknown_ids
                ),
            )
            for snapshot in task.projects
        )

    exact_unmerged = tuple(
        snapshot
        for snapshot in pending
        if snapshot.project.project_id not in observed
    )
    if exact_unmerged:
        failed = exact_unmerged[0]
        failed_id = failed.project.project_id
        merged_ids = tuple(
            snapshot.project.project_id
            for snapshot in task.projects
            if snapshot.project.project_id in observed
            or snapshot.project_state == "merged"
        )
        remaining_ids = tuple(
            snapshot.project.project_id
            for snapshot in task.projects
            if snapshot.project.project_id not in {*merged_ids, failed_id}
        )
        reason = "exact remote readback confirmed the pending Project was not merged"
        if merged_ids:
            merge_store.finish_partial(
                task,
                merged_project_ids=merged_ids,
                failed_project_id=failed_id,
                remaining_project_ids=remaining_ids,
                reason=reason,
                occurred_at=_read_time(clock),
            )
        else:
            merge_store.mark_failed(
                task,
                failed,
                reason=reason,
                occurred_at=_read_time(clock),
            )
        return tuple(
            _project_report(
                snapshot,
                decision=(CHECK_ERROR if snapshot == failed else WAIT),
                action=("error" if snapshot == failed else "none"),
                reason=(
                    reason
                    if snapshot == failed
                    else "no new merge starts during reconciliation"
                ),
                tested_commit=(
                    None
                    if snapshot.task_flow_state is None
                    else snapshot.task_flow_state.current_commit
                ),
                merge_commit=(
                    observed[snapshot.project.project_id].merged_commit
                    if snapshot.project.project_id in observed
                    else None
                ),
            )
            for snapshot in task.projects
        )

    if all(
        proof_by_project[snapshot.project.project_id].already_merged
        for snapshot in task.projects
    ):
        merge_store.finish_merged(task, occurred_at=_read_time(clock))
    return tuple(
        _pending_resolution_report(
            snapshot,
            proof=proof_by_project[snapshot.project.project_id],
            observed=observed.get(snapshot.project.project_id),
            reconciliation_pending=False,
        )
        for snapshot in task.projects
    )


def _pending_resolution_report(
    snapshot: ProjectMergeSnapshot,
    *,
    proof: ProjectMergeProof,
    observed: MergeWriteResult | None,
    reconciliation_pending: bool,
) -> TaskMergeReport:
    if observed is not None:
        return _project_report(
            snapshot,
            decision=AUTO_MERGE_ALLOWED,
            action="observed_merge",
            reason="pending Project merge was recovered by exact remote readback",
            tested_commit=proof.expected_head_commit,
            merge_commit=observed.merged_commit,
        )
    return _project_report(
        snapshot,
        decision=(CHECK_ERROR if reconciliation_pending else WAIT),
        action=("reconciliation_pending" if reconciliation_pending else "none"),
        reason=(
            "pending Project merge is still unknown"
            if reconciliation_pending
            else "no new merge starts during reconciliation"
        ),
        tested_commit=(
            None
            if snapshot.task_flow_state is None
            else snapshot.task_flow_state.current_commit
        ),
    )


def _read_project_recovery(
    snapshot: ProjectMergeSnapshot,
    *,
    evidence_reader: MergeEvidenceReader,
    required_check: str,
    clock: Callable[[], datetime],
) -> _ProjectMergeRecovery:
    state = snapshot.task_flow_state
    if state is None:
        return _ProjectMergeRecovery("unknown", "Project flow proof is unavailable")
    try:
        evidence = evidence_reader.get_merge_evidence(
            state.pr_url,
            (required_check,),
            include_safe_files=False,
        )
    except Exception as error:
        return _ProjectMergeRecovery(
            "unknown",
            str(error) or "remote readback failed",
        )
    proof = _project_merge_proof(
        snapshot,
        evidence,
        required_check=required_check,
        now=_read_time(clock),
    )
    if proof.already_merged:
        try:
            result = _observed_project_merge_result(evidence)
            _validate_merge_result(
                result,
                expected_base_commit=state.current_base_commit,
                expected_commit=state.current_commit,
            )
        except Exception as error:
            return _ProjectMergeRecovery(
                "unknown",
                str(error) or "merged readback is incomplete",
            )
        return _ProjectMergeRecovery(
            "merged",
            "exact remote readback confirmed the merge",
            result,
        )
    if _is_exact_unmerged_project_evidence(snapshot, evidence):
        return _ProjectMergeRecovery(
            "unmerged",
            "exact remote readback confirmed the Project is not merged",
        )
    return _ProjectMergeRecovery(
        "unknown",
        "remote readback does not match the exact Project proof",
    )


def _is_exact_unmerged_project_evidence(
    snapshot: ProjectMergeSnapshot,
    evidence: object,
) -> bool:
    state = snapshot.task_flow_state
    return bool(
        isinstance(state, TaskFlowState)
        and isinstance(evidence, GitHubMergeEvidence)
        and evidence.pr_url == state.pr_url
        and evidence.repository == snapshot.project.repository
        and evidence.base_commit == state.current_base_commit
        and evidence.head_commit == state.current_commit
        and evidence.is_merged is False
        and evidence.merged_commit is None
        and evidence.merged_base_commit is None
        and evidence.merged_head_commit is None
    )


def _validate_project_task(task: ProjectMergeTask) -> None:
    if not isinstance(task, ProjectMergeTask):
        raise MergeRuntimeError("Project merge task has an unexpected type")
    if not isinstance(task.request, TaskRequestV2) or not isinstance(
        task.settings, TaskSettingsV2
    ):
        raise MergeRuntimeError("Project merge task settings are invalid")
    if task.settings.request_id != task.request.request_id:
        raise MergeRuntimeError("Project merge request does not match settings")
    if not isinstance(task.projects, tuple):
        raise MergeRuntimeError("Project merge snapshots must be a tuple")
    expected = {project.project_id: project for project in task.settings.projects}
    actual = {snapshot.project.project_id for snapshot in task.projects}
    if len(task.projects) != len(expected) or actual != set(expected):
        raise MergeRuntimeError("Project merge snapshots are not exact")
    for snapshot in task.projects:
        if (
            not isinstance(snapshot, ProjectMergeSnapshot)
            or snapshot.request != task.request
            or snapshot.settings != task.settings
            or snapshot.project != expected[snapshot.project.project_id]
            or type(snapshot.merge_attempt_pending) is not bool
            or (
                snapshot.merge_attempt_pending
                and snapshot.project_state != "waiting_for_help"
            )
            or snapshot.project_state
            not in {
                "ready",
                "running",
                "reviewing",
                "waiting_for_help",
                "failed",
                "merged",
            }
        ):
            raise MergeRuntimeError("Project merge snapshot changed from settings")


def _project_merge_proof(
    snapshot: ProjectMergeSnapshot,
    evidence: GitHubMergeEvidence,
    *,
    required_check: str,
    now: datetime,
) -> ProjectMergeProof:
    state = snapshot.task_flow_state
    if not isinstance(state, TaskFlowState):
        return ProjectMergeProof(
            snapshot.project.project_id,
            snapshot.project.repository,
            WAIT,
            "0" * 40,
        )
    local_ready = not (
        state.task_settings_hash != snapshot.settings.task_settings_hash
        or state.task_flow is not snapshot.settings.task_flow
        or state.status is not TaskFlowStatus.READY_TO_MERGE
        or state.completed_steps != required_steps(snapshot.settings.task_flow)
        or state.current_step is not None
        or state.step_running
    )
    decision = AUTO_MERGE_ALLOWED if local_ready else WAIT
    if not isinstance(evidence, GitHubMergeEvidence):
        return ProjectMergeProof(
            project_id=snapshot.project.project_id,
            repository=snapshot.project.repository,
            decision=CHECK_ERROR,
            expected_head_commit=state.current_commit,
        )
    if (
        evidence.pr_url != state.pr_url
        or evidence.repository != snapshot.project.repository
        or evidence.base_commit != state.current_base_commit
        or evidence.head_commit != state.current_commit
    ):
        decision = CHECK_ERROR
    elif any(
        type(value) is not bool
        for value in (
            evidence.is_open,
            evidence.is_draft,
            evidence.is_merged,
            evidence.has_conflict,
            evidence.base_is_current,
            evidence.rules_allow_merge,
        )
    ):
        decision = CHECK_ERROR
    already_merged = evidence.is_merged is True
    if already_merged:
        if (
            evidence.is_open
            or evidence.merged_commit is None
            or evidence.merged_base_commit != state.current_base_commit
            or evidence.merged_head_commit != state.current_commit
        ):
            decision = CHECK_ERROR
        elif local_ready and decision != CHECK_ERROR:
            # A policy only controls new merge writes. Exact remote state must
            # still converge after a human merge or a lost write response.
            decision = AUTO_MERGE_ALLOWED
        return ProjectMergeProof(
            project_id=snapshot.project.project_id,
            repository=snapshot.project.repository,
            decision=decision,
            expected_head_commit=state.current_commit,
            already_merged=decision == AUTO_MERGE_ALLOWED,
        )

    if any(
        value is not None
        for value in (
            evidence.merged_commit,
            evidence.merged_base_commit,
            evidence.merged_head_commit,
        )
    ):
        decision = CHECK_ERROR
    check: CheckRun | None = None
    if decision != CHECK_ERROR:
        try:
            check = _required_check(evidence.checks, required_check)
        except MergeRuntimeError:
            decision = CHECK_ERROR
    if check is not None and (
        check.head_sha != state.current_commit or _check_result(check) != "success"
    ):
        decision = CHECK_ERROR
    if decision != CHECK_ERROR and (
        not evidence.is_open
        or evidence.is_draft
        or evidence.has_conflict
        or not evidence.base_is_current
        or not evidence.rules_allow_merge
        or type(evidence.unresolved_review_threads) is not int
        or evidence.unresolved_review_threads != 0
    ):
        decision = WAIT
    expires_at = snapshot.settings.auto_merge_expires_at
    if decision != CHECK_ERROR and (expires_at is None or now >= expires_at):
        decision = MANUAL_MERGE_REQUIRED
    if (
        snapshot.settings.merge_mode is MergeMode.SAFE_AUTO
        and len(snapshot.settings.projects) == 1
        and decision == AUTO_MERGE_ALLOWED
    ):
        try:
            _verified_safe_files(snapshot.settings.merge_mode, evidence)
        except MergeRuntimeError:
            decision = CHECK_ERROR
    return ProjectMergeProof(
        project_id=snapshot.project.project_id,
        repository=snapshot.project.repository,
        decision=decision,
        expected_head_commit=state.current_commit,
        already_merged=False,
    )


def _observed_project_merge_result(evidence: GitHubMergeEvidence) -> MergeWriteResult:
    if (
        evidence.merged_commit is None
        or evidence.merged_base_commit is None
        or evidence.merged_head_commit is None
    ):
        raise MergeRuntimeError("merged Project readback is incomplete")
    return MergeWriteResult(
        expected_commit=evidence.merged_head_commit,
        expected_base_commit=evidence.merged_base_commit,
        merged_commit=evidence.merged_commit,
        merged_base_commit=evidence.merged_base_commit,
        merged_head_commit=evidence.merged_head_commit,
        already_merged=True,
        recovered_by_readback=True,
    )


def _project_report(
    snapshot: ProjectMergeSnapshot,
    *,
    decision: str,
    action: str,
    reason: str,
    tested_commit: str | None = None,
    merge_commit: str | None = None,
) -> TaskMergeReport:
    state = snapshot.task_flow_state
    return TaskMergeReport(
        request_id=snapshot.request.request_id,
        issue_number=snapshot.settings.parent_issue_number,
        decision=decision,
        action=action,
        reason=reason,
        pr_url=None if state is None else state.pr_url,
        tested_commit=tested_commit,
        project_id=snapshot.project.project_id,
        repository=snapshot.project.repository,
        merge_commit=merge_commit,
    )


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
    "ProjectMergeSnapshot",
    "ProjectMergeStore",
    "ProjectMergeTask",
    "ProjectSnapshotGitHub",
    "TaskDatabaseProjectMergeStore",
    "TaskFlowSnapshotLike",
    "TaskMergeReport",
    "load_project_merge_tasks",
    "run_merge_tasks",
    "run_project_merge_tasks",
]
