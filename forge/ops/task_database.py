"""Single owner for the additive Infinity Forge Task SQLite schema."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path


TASK_DATABASE_SCHEMA_VERSION = 2
_OPERATION_LOCK_TIMEOUT_SECONDS = 5.0
_OPERATION_LOCK_RETRY_SECONDS = 0.01
_WINDOWS_OPERATION_LOCK_BYTES = 256
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_RESTORE_OFFLINE_MESSAGE = "Task database restore requires offline access"

_V1_TASK_SETTINGS_SQL = """
CREATE TABLE task_settings (
    request_id TEXT PRIMARY KEY,
    format_version TEXT NOT NULL
        CHECK (format_version = 'forge-task-settings/v1'),
    repository TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode = 'task'),
    task_content_hash TEXT NOT NULL,
    task_flow TEXT NOT NULL CHECK (
        task_flow IN (
            'build',
            'build_review',
            'build_review_deep_check'
        )
    ),
    merge_mode TEXT NOT NULL CHECK (
        merge_mode IN ('manual', 'safe_auto', 'full_auto')
    ),
    confirmed_by TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    auto_merge_expires_at TEXT
)
"""

_V1_TASK_SETTINGS_EVENTS_SQL = """
CREATE TABLE task_settings_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL
        REFERENCES task_settings(request_id),
    event_type TEXT NOT NULL CHECK (
        event_type IN (
            'prepared',
            'issue_bound',
            'active',
            'cancelled',
            'expired',
            'merged'
        )
    ),
    occurred_at TEXT NOT NULL,
    issue_number INTEGER,
    task_settings_hash TEXT,
    UNIQUE (request_id, event_type),
    CHECK (
        (
            event_type = 'issue_bound'
            AND issue_number IS NOT NULL
            AND task_settings_hash IS NOT NULL
        )
        OR
        (
            event_type <> 'issue_bound'
            AND issue_number IS NULL
            AND task_settings_hash IS NULL
        )
    )
)
"""

_V1_BRANCH_REFRESH_INTENTS_SQL = """
CREATE TABLE task_branch_refresh_intents (
    request_id TEXT NOT NULL
        REFERENCES task_settings(request_id),
    refresh_number INTEGER NOT NULL
        CHECK (refresh_number BETWEEN 1 AND 3),
    pr_url TEXT NOT NULL,
    expected_base_commit TEXT NOT NULL,
    expected_head_commit TEXT NOT NULL,
    created_at TEXT NOT NULL,
    current_base_commit TEXT,
    current_head_commit TEXT,
    completed_at TEXT,
    PRIMARY KEY (request_id, refresh_number),
    UNIQUE (request_id, expected_base_commit, expected_head_commit),
    CHECK (
        (
            current_base_commit IS NULL
            AND current_head_commit IS NULL
            AND completed_at IS NULL
        )
        OR
        (
            current_base_commit IS NOT NULL
            AND current_head_commit IS NOT NULL
            AND completed_at IS NOT NULL
        )
    ),
    CHECK (
        current_base_commit IS NULL
        OR current_base_commit <> expected_base_commit
        OR current_head_commit <> expected_head_commit
    )
)
"""

_V1_TERMINAL_EVENT_INDEX_SQL = """
CREATE UNIQUE INDEX task_settings_one_terminal_event
    ON task_settings_events (request_id)
    WHERE event_type IN ('cancelled', 'expired', 'merged')
"""

_TASK_REQUESTS_SQL = """
CREATE TABLE task_requests (
    request_id TEXT PRIMARY KEY,
    format_version TEXT NOT NULL
        CHECK (format_version = 'forge-task-request/v2'),
    request_json TEXT NOT NULL,
    request_hash TEXT NOT NULL UNIQUE
        CHECK (
            length(request_hash) = 64
            AND request_hash NOT GLOB '*[^0-9a-f]*'
        ),
    management_repository TEXT NOT NULL,
    task_owner_host TEXT NOT NULL,
    confirmed_by TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    replaces_request_id TEXT
        REFERENCES task_requests(request_id),
    UNIQUE (request_id, request_hash)
)
"""

_TASK_SETTINGS_V2_SQL = """
CREATE TABLE task_settings_v2 (
    task_settings_hash TEXT PRIMARY KEY
        CHECK (
            length(task_settings_hash) = 64
            AND task_settings_hash NOT GLOB '*[^0-9a-f]*'
        ),
    request_id TEXT NOT NULL UNIQUE
        REFERENCES task_requests(request_id),
    request_hash TEXT NOT NULL,
    format_version TEXT NOT NULL
        CHECK (format_version = 'forge-task-settings/v2'),
    settings_json TEXT NOT NULL,
    management_repository TEXT NOT NULL,
    parent_issue_number INTEGER NOT NULL
        CHECK (parent_issue_number > 0),
    task_owner_host TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    FOREIGN KEY (request_id, request_hash)
        REFERENCES task_requests(request_id, request_hash)
)
"""

_TASK_EVENTS_SQL = """
CREATE TABLE task_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL
        REFERENCES task_requests(request_id),
    task_settings_hash TEXT
        REFERENCES task_settings_v2(task_settings_hash),
    project_id TEXT,
    event_type TEXT NOT NULL CHECK (
        event_type IN (
            'request_prepared',
            'parent_issue_bound',
            'project_item_bound',
            'settings_activated',
            'dispatch_ready',
            'revision_requested',
            'revision_cancelled',
            'revision_resumed',
            'stop_requested',
            'active',
            'changing',
            'stopping',
            'cancelled',
            'expired',
            'merged',
            'replaced',
            'partially_merged'
        )
    ),
    event_key TEXT NOT NULL,
    event_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    UNIQUE (request_id, event_key)
)
"""

_TASK_PROJECTS_SQL = """
CREATE TABLE task_projects (
    request_id TEXT NOT NULL
        REFERENCES task_requests(request_id),
    project_id TEXT NOT NULL
        CHECK (
            length(project_id) = 64
            AND project_id NOT GLOB '*[^0-9a-f]*'
        ),
    task_settings_hash TEXT
        REFERENCES task_settings_v2(task_settings_hash),
    project_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN (
            'prepared',
            'bound',
            'ready',
            'running',
            'reviewing',
            'waiting_for_help',
            'failed',
            'merged',
            'cancelled'
        )
    ),
    root_card_id TEXT,
    branch_name TEXT,
    worktree_path TEXT,
    pr_url TEXT,
    head_commit TEXT,
    merge_commit TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (request_id, project_id),
    UNIQUE (task_settings_hash, project_id)
)
"""

_SURFACE_EVENTS_SQL = """
CREATE TABLE surface_events (
    source_event_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    surface TEXT NOT NULL,
    payload_hash TEXT NOT NULL
        CHECK (
            length(payload_hash) = 64
            AND payload_hash NOT GLOB '*[^0-9a-f]*'
        ),
    state TEXT NOT NULL CHECK (
        state IN ('received', 'handled', 'responded', 'expired')
    ),
    received_at TEXT NOT NULL,
    response_hash TEXT,
    responded_at TEXT,
    retention_until TEXT NOT NULL,
    CHECK (
        (response_hash IS NULL AND responded_at IS NULL)
        OR
        (response_hash IS NOT NULL AND responded_at IS NOT NULL)
    )
)
"""

_TASK_MESSAGES_SQL = """
CREATE TABLE task_messages (
    message_id TEXT PRIMARY KEY,
    format_version TEXT NOT NULL
        CHECK (format_version = 'forge-task-message/v1'),
    request_id TEXT NOT NULL
        REFERENCES task_requests(request_id),
    parent_issue_number INTEGER NOT NULL
        CHECK (parent_issue_number > 0),
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL UNIQUE
        REFERENCES surface_events(source_event_id),
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    message_hash TEXT NOT NULL
        CHECK (
            length(message_hash) = 64
            AND message_hash NOT GLOB '*[^0-9a-f]*'
        )
)
"""

_TASK_MESSAGE_EVENTS_SQL = """
CREATE TABLE task_message_events (
    message_event_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL
        REFERENCES task_messages(message_id),
    task_settings_hash TEXT NOT NULL
        REFERENCES task_settings_v2(task_settings_hash),
    worker_task_id TEXT,
    run_id TEXT,
    event_type TEXT NOT NULL CHECK (
        event_type IN ('included', 'applied', 'rejected')
    ),
    reason TEXT,
    occurred_at TEXT NOT NULL,
    UNIQUE (message_id, task_settings_hash, run_id, event_type)
)
"""

_TASK_REVISION_REQUESTS_SQL = """
CREATE TABLE task_revision_requests (
    revision_request_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL
        REFERENCES task_requests(request_id),
    base_task_settings_hash TEXT NOT NULL
        REFERENCES task_settings_v2(task_settings_hash),
    replacement_request_id TEXT
        REFERENCES task_requests(request_id),
    source_event_id TEXT NOT NULL UNIQUE
        REFERENCES surface_events(source_event_id),
    state TEXT NOT NULL CHECK (
        state IN ('requested', 'confirmed', 'cancelled', 'resumed')
    ),
    preview_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_TASK_STOP_REQUESTS_SQL = """
CREATE TABLE task_stop_requests (
    stop_request_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL
        REFERENCES task_requests(request_id),
    task_settings_hash TEXT
        REFERENCES task_settings_v2(task_settings_hash),
    source_event_id TEXT NOT NULL UNIQUE
        REFERENCES surface_events(source_event_id),
    state TEXT NOT NULL CHECK (
        state IN ('requested', 'stopping', 'completed', 'cleanup_incomplete')
    ),
    result TEXT CHECK (
        result IN (
            'cancelled',
            'completed_before_stop',
            'completed_with_partial_merge'
        )
    ),
    details_json TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    CHECK (
        (state = 'completed' AND result IS NOT NULL AND completed_at IS NOT NULL)
        OR
        (state <> 'completed' AND result IS NULL AND completed_at IS NULL)
    )
)
"""

_TASK_SESSION_BINDINGS_SQL = """
CREATE TABLE task_session_bindings (
    surface TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    request_id TEXT NOT NULL
        REFERENCES task_requests(request_id),
    parent_issue_number INTEGER NOT NULL
        CHECK (parent_issue_number > 0),
    bound_at TEXT NOT NULL,
    PRIMARY KEY (surface, subject_id, session_id, request_id)
)
"""

_TASK_ACCESS_SQL = """
CREATE TABLE task_access (
    request_id TEXT NOT NULL
        REFERENCES task_requests(request_id),
    surface TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('owner', 'operator')),
    granted_by TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    revoked_at TEXT,
    PRIMARY KEY (request_id, surface, subject_id)
)
"""

_TASK_RUNTIME_RUNS_SQL = """
CREATE TABLE task_runtime_runs (
    run_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL
        REFERENCES task_requests(request_id),
    task_settings_hash TEXT NOT NULL
        REFERENCES task_settings_v2(task_settings_hash),
    project_id TEXT NOT NULL,
    host_id TEXT NOT NULL,
    worker_task_id TEXT NOT NULL,
    runtime_name TEXT NOT NULL,
    process_identity_json TEXT NOT NULL,
    message_packet_hash TEXT NOT NULL
        CHECK (
            length(message_packet_hash) = 64
            AND message_packet_hash NOT GLOB '*[^0-9a-f]*'
        ),
    state TEXT NOT NULL CHECK (
        state IN ('starting', 'running', 'stopping', 'completed', 'failed', 'stopped')
    ),
    result_hash TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    FOREIGN KEY (request_id, project_id)
        REFERENCES task_projects(request_id, project_id),
    CHECK (
        (state IN ('starting', 'running', 'stopping') AND ended_at IS NULL)
        OR
        (state IN ('completed', 'failed', 'stopped') AND ended_at IS NOT NULL)
    )
)
"""

_V1_SCHEMA_STATEMENTS = (
    _V1_TASK_SETTINGS_SQL,
    _V1_TASK_SETTINGS_EVENTS_SQL,
    _V1_BRANCH_REFRESH_INTENTS_SQL,
    _V1_TERMINAL_EVENT_INDEX_SQL,
)

_V2_SCHEMA_STATEMENTS = (
    _TASK_REQUESTS_SQL,
    _TASK_SETTINGS_V2_SQL,
    _TASK_EVENTS_SQL,
    _TASK_PROJECTS_SQL,
    _SURFACE_EVENTS_SQL,
    _TASK_MESSAGES_SQL,
    _TASK_MESSAGE_EVENTS_SQL,
    _TASK_REVISION_REQUESTS_SQL,
    _TASK_STOP_REQUESTS_SQL,
    _TASK_SESSION_BINDINGS_SQL,
    _TASK_ACCESS_SQL,
    _TASK_RUNTIME_RUNS_SQL,
)

_V1_EXPECTED_SCHEMA = {
    ("table", "task_settings"): _V1_TASK_SETTINGS_SQL,
    ("table", "task_settings_events"): _V1_TASK_SETTINGS_EVENTS_SQL,
    ("table", "task_branch_refresh_intents"): _V1_BRANCH_REFRESH_INTENTS_SQL,
    ("index", "task_settings_one_terminal_event"): _V1_TERMINAL_EVENT_INDEX_SQL,
}

_V2_EXPECTED_SCHEMA = {
    ("table", "task_requests"): _TASK_REQUESTS_SQL,
    ("table", "task_settings_v2"): _TASK_SETTINGS_V2_SQL,
    ("table", "task_events"): _TASK_EVENTS_SQL,
    ("table", "task_projects"): _TASK_PROJECTS_SQL,
    ("table", "surface_events"): _SURFACE_EVENTS_SQL,
    ("table", "task_messages"): _TASK_MESSAGES_SQL,
    ("table", "task_message_events"): _TASK_MESSAGE_EVENTS_SQL,
    ("table", "task_revision_requests"): _TASK_REVISION_REQUESTS_SQL,
    ("table", "task_stop_requests"): _TASK_STOP_REQUESTS_SQL,
    ("table", "task_session_bindings"): _TASK_SESSION_BINDINGS_SQL,
    ("table", "task_access"): _TASK_ACCESS_SQL,
    ("table", "task_runtime_runs"): _TASK_RUNTIME_RUNS_SQL,
}

_EXPECTED_SCHEMA = {**_V1_EXPECTED_SCHEMA, **_V2_EXPECTED_SCHEMA}
TASK_DATABASE_TABLES = frozenset(
    name for (object_type, name) in _EXPECTED_SCHEMA if object_type == "table"
)
_AUTOINCREMENT_PRIMARY_KEYS = {
    "task_events": "event_id",
    "task_settings_events": "event_id",
}
_AUTOINCREMENT_TABLES = frozenset(_AUTOINCREMENT_PRIMARY_KEYS)


class TaskDatabaseError(RuntimeError):
    """Raised when the shared Task database cannot be trusted."""


class TaskDatabaseCommittedError(TaskDatabaseError):
    """Report a successful commit whose retained artifact needs manual cleanup."""

    committed = True

    def __init__(
        self,
        *,
        operation: str,
        retained_artifact: Path,
        detail: str,
    ) -> None:
        self.operation = operation
        self.retained_artifact = retained_artifact
        super().__init__(
            f"Task database {operation} committed but {detail}; "
            f"retained artifact: {retained_artifact}"
        )


@dataclass(frozen=True, slots=True)
class _DatabaseArtifact:
    artifact: Path
    file_hash: str
    content_hash: str

    @property
    def rollback_artifact(self) -> Path:
        return self.artifact


@dataclass(frozen=True, slots=True)
class _PreparedSidecar:
    path: Path
    device: int
    inode: int


@dataclass(slots=True)
class _OfflineRestoreGate:
    write_connection: sqlite3.Connection
    snapshot_connection: sqlite3.Connection | None

    def close_snapshot(self) -> None:
        if self.snapshot_connection is None:
            return
        if self.snapshot_connection is self.write_connection:
            self.snapshot_connection = None
            return
        try:
            self.snapshot_connection.rollback()
        finally:
            self.snapshot_connection.close()
            self.snapshot_connection = None


@dataclass(frozen=True, slots=True)
class _HeldOperationFileLock:
    descriptor: int
    reader_slot: int | None


@dataclass(slots=True)
class _OperationBarrierState:
    condition: threading.Condition = field(
        default_factory=lambda: threading.Condition(threading.RLock())
    )
    shared_count: int = 0
    shared_file_lock: _HeldOperationFileLock | None = None
    exclusive_pending: bool = False
    exclusive_owner: int | None = None
    exclusive_depth: int = 0
    exclusive_file_lock: _HeldOperationFileLock | None = None


_OPERATION_BARRIERS_LOCK = threading.Lock()
_OPERATION_BARRIERS: dict[str, _OperationBarrierState] = {}
_ACTIVE_TRANSACTION_PATHS = threading.local()


class TaskDatabase:
    """Open, migrate, validate, back up, and restore one Task SQLite file."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = _prepare_database_path(database_path)
        _ensure_secure_database_file(self.database_path)
        self._operation_lock_path = _prepare_operation_lock_path(self.database_path)
        self._write_lock_path = _prepare_write_lock_path(self.database_path)
        self._initialize()
        _apply_owner_only_permissions(self.database_path)
        if not self.verify_owner_only_permissions():
            raise TaskDatabaseError("Task database permissions are not owner-only")

    def connect(self) -> sqlite3.Connection:
        """Open one validated-path connection with foreign keys enabled."""

        _assert_safe_file(self.database_path, required=True)
        _assert_sqlite_connection_preconditions(self.database_path)
        try:
            connection = sqlite3.connect(
                f"{self.database_path.as_uri()}?mode=rw",
                timeout=5,
                uri=True,
            )
        except sqlite3.Error as error:
            raise TaskDatabaseError(
                "Task database path could not be opened safely"
            ) from error
        try:
            _assert_safe_file(self.database_path, required=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Keep restore excluded for the lifetime of one read connection."""

        with (
            _database_operation_lock(self._operation_lock_path),
            closing(self.connect()) as connection,
        ):
            yield connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialize one mutable operation without excluding readers."""

        with (
            _database_transaction_scope(self.database_path),
            _database_operation_lock(self._operation_lock_path),
            _database_operation_lock(self._write_lock_path, exclusive=True),
            closing(self.connect()) as connection,
        ):
            prepared_sidecars: tuple[_PreparedSidecar, ...] = ()
            try:
                _assert_owner_only_sqlite_sidecars(self.database_path)
                prepared_sidecars = _prepare_secure_sqlite_write_sidecars(
                    connection,
                    self.database_path,
                )
                _begin_immediate(connection)
                yield connection
                _secure_existing_sqlite_sidecars(self.database_path)
                connection.commit()
            except BaseException as error:
                try:
                    connection.rollback()
                except sqlite3.Error as rollback_error:
                    raise TaskDatabaseError(
                        "Task database operation failed during rollback"
                    ) from rollback_error
                if isinstance(error, sqlite3.Error):
                    raise TaskDatabaseError(
                        "Task database operation failed"
                    ) from error
                raise
            finally:
                _remove_empty_precreated_sidecars(prepared_sidecars)

    def quick_check(self, *, _operation_locked: bool = False) -> str:
        """Return ``ok`` only when SQLite verifies every database page."""

        if not _operation_locked:
            with _database_operation_lock(self._operation_lock_path):
                return self.quick_check(_operation_locked=True)
        with closing(self.connect()) as connection:
            return _quick_check(connection)

    def verify_owner_only_permissions(self) -> bool:
        """Read back the host permission boundary for the Task database."""

        return _verify_owner_only_permissions(self.database_path)

    def backup(self, destination_path: str | Path) -> Path:
        """Create a checked SQLite backup and atomically publish it."""

        destination = _prepare_output_path(destination_path)
        _require_distinct_paths(self.database_path, destination)
        _assert_owner_only_sqlite_sidecars(self.database_path)
        with _database_operation_lock(self._operation_lock_path):
            return self._backup_locked(destination)

    def _backup_locked(self, destination: Path) -> Path:
        artifact: _DatabaseArtifact | None = None
        published = False
        keep_artifact = False
        operation_error: BaseException | None = None
        try:
            with closing(self.connect()) as source:
                source.execute("BEGIN")
                source.execute("SELECT COUNT(*) FROM sqlite_schema").fetchone()
                artifact = _capture_database_artifact(
                    source,
                    destination.parent,
                    purpose="backup",
                )
            destination_lock = _prepare_operation_lock_path(destination)
            with _database_operation_lock(
                destination_lock,
                exclusive=True,
                timeout_seconds=0.0,
                busy_message="Task database backup destination requires offline access",
            ):
                _publish_database_artifact(artifact, destination)
            published = True
        except BaseException as error:
            operation_error = error
            keep_artifact = isinstance(error, TaskDatabaseCommittedError)

        if artifact is not None and not keep_artifact:
            try:
                _remove_database_artifact(artifact)
            except BaseException as cleanup_error:
                if published:
                    raise TaskDatabaseCommittedError(
                        operation="backup",
                        retained_artifact=artifact.artifact,
                        detail="artifact cleanup failed",
                    ) from cleanup_error
                if operation_error is not None:
                    raise TaskDatabaseError(
                        "Task database backup failed and its staging artifact "
                        f"could not be removed: {artifact.artifact}"
                    ) from operation_error
                raise

        if operation_error is not None:
            if isinstance(operation_error, (KeyboardInterrupt, SystemExit)):
                raise operation_error
            if isinstance(operation_error, TaskDatabaseError):
                raise operation_error
            if isinstance(operation_error, (OSError, sqlite3.Error)):
                raise TaskDatabaseError("Task database backup failed") from operation_error
            raise operation_error
        return destination

    def restore(self, backup_path: str | Path) -> None:
        """Hold the live write gate while validating and restoring one backup."""

        backup = _prepare_existing_input_path(backup_path)
        _require_distinct_paths(self.database_path, backup)
        _assert_owner_only_sqlite_sidecars(backup)
        committed_preimage: _DatabaseArtifact | None = None
        try:
            with _database_operation_lock(
                self._operation_lock_path,
                exclusive=True,
                timeout_seconds=0.0,
                busy_message=_RESTORE_OFFLINE_MESSAGE,
            ):
                with _offline_database_gate(self.database_path) as gate:
                    committed_preimage = self._restore_locked(backup, gate)
        except BaseException as error:
            if committed_preimage is not None:
                raise TaskDatabaseCommittedError(
                    operation="restore",
                    retained_artifact=committed_preimage.rollback_artifact,
                    detail="offline gate teardown failed",
                ) from error
            raise

        if committed_preimage is None:
            raise TaskDatabaseError("Task database restore completion is missing")
        try:
            _remove_database_artifact(committed_preimage)
        except BaseException as cleanup_error:
            raise TaskDatabaseCommittedError(
                operation="restore",
                retained_artifact=committed_preimage.rollback_artifact,
                detail="preimage cleanup failed",
            ) from cleanup_error

    def _restore_locked(
        self,
        backup: Path,
        gate: _OfflineRestoreGate,
    ) -> _DatabaseArtifact:
        temporary = _new_secure_temporary_file(self.database_path.parent, "restore")
        preimage: _DatabaseArtifact | None = None
        expected_content_hash: str | None = None
        committed = False
        keep_rollback_artifact = False
        operation_error: BaseException | None = None
        try:
            with (
                closing(_connect_database_file(backup, mode="ro")) as source,
                closing(_connect_database_file(temporary, mode="rw")) as target,
            ):
                prepared_sidecars = _prepare_secure_sqlite_write_sidecars(
                    target,
                    temporary,
                )
                try:
                    source.backup(target)
                    target.commit()
                finally:
                    _remove_empty_precreated_sidecars(prepared_sidecars)
                _validate_supported_schema(target)
                _initialize_database_connection(target)
                _require_standalone_journal(target, "restore staging database")
                expected_content_hash = _validate_exact_database(target)
            _finalize_database_file(
                temporary,
                expected_content_hash=expected_content_hash,
                description="restore staging database",
            )
            if gate.snapshot_connection is None:
                raise TaskDatabaseError("restore snapshot connection is missing")
            preimage = _capture_restore_preimage(
                gate.snapshot_connection,
                self.database_path,
            )
            gate.close_snapshot()
            _replace_database_content(
                gate.write_connection,
                temporary,
            )
            _validate_restore_candidate(
                gate.write_connection,
                expected_content_hash=expected_content_hash,
            )
            _secure_existing_sqlite_sidecars(self.database_path)
            _apply_owner_only_permissions(self.database_path)
            if not self.verify_owner_only_permissions():
                raise TaskDatabaseError("restored Task database permissions are unsafe")
            _sync_file(self.database_path)
            gate.write_connection.commit()
            committed = True
            _detach_database_if_attached(gate.write_connection, "restore_source")
            _sync_directory(self.database_path.parent)
        except BaseException as error:
            if preimage is not None and not committed:
                try:
                    gate.write_connection.rollback()
                    _detach_database_if_attached(
                        gate.write_connection,
                        "restore_source",
                    )
                    _assert_database_matches_artifact(
                        self.database_path,
                        preimage,
                    )
                except BaseException as rollback_error:
                    keep_rollback_artifact = True
                    raise TaskDatabaseError(
                        "Task database restore failed and rollback failed; "
                        f"rollback artifact: {preimage.rollback_artifact}"
                    ) from rollback_error
                operation_error = error
            elif preimage is not None and committed:
                keep_rollback_artifact = True
                committed_error = TaskDatabaseCommittedError(
                    operation="restore",
                    retained_artifact=preimage.rollback_artifact,
                    detail="finalization failed",
                )
                committed_error.__cause__ = error
                operation_error = committed_error
            else:
                operation_error = error

        try:
            _remove_restore_artifact(temporary)
        except BaseException as cleanup_error:
            if committed:
                keep_rollback_artifact = True
                retained = (
                    preimage.rollback_artifact if preimage is not None else temporary
                )
                raise TaskDatabaseCommittedError(
                    operation="restore",
                    retained_artifact=retained,
                    detail=f"staging cleanup failed; staging artifact: {temporary}",
                ) from cleanup_error
            if operation_error is not None:
                raise TaskDatabaseError(
                    "Task database restore failed and its staging artifact "
                    f"could not be removed: {temporary}"
                ) from operation_error
            raise

        if preimage is not None and not keep_rollback_artifact and not committed:
            try:
                _remove_database_artifact(preimage)
            except BaseException as cleanup_error:
                if committed:
                    raise TaskDatabaseCommittedError(
                        operation="restore",
                        retained_artifact=preimage.rollback_artifact,
                        detail="preimage cleanup failed",
                    ) from cleanup_error
                raise

        if operation_error is not None:
            if isinstance(operation_error, TaskDatabaseCommittedError):
                raise operation_error
            if isinstance(operation_error, (KeyboardInterrupt, SystemExit)):
                raise operation_error
            if _is_sqlite_busy_error(operation_error):
                raise TaskDatabaseError(_RESTORE_OFFLINE_MESSAGE) from operation_error
            raise TaskDatabaseError(
                "Task database backup could not be restored safely"
            ) from operation_error
        if not committed or preimage is None:
            raise TaskDatabaseError("Task database restore completion is missing")
        return preimage

    def _initialize(self, *, _operation_locked: bool = False) -> None:
        if not _operation_locked:
            with _database_operation_lock(
                self._operation_lock_path,
                exclusive=True,
            ):
                _assert_owner_only_sqlite_sidecars(self.database_path)
                self._initialize(_operation_locked=True)
            return
        with closing(self.connect()) as connection:
            _initialize_database_connection(connection)


def _initialize_database_connection(connection: sqlite3.Connection) -> None:
    database_path = _database_path_for_connection(connection)
    prepared_sidecars: tuple[_PreparedSidecar, ...] = ()
    try:
        _require_standalone_journal(connection, "Task database")
        prepared_sidecars = _prepare_secure_sqlite_write_sidecars(
            connection,
            database_path,
        )
        _begin_immediate(connection)
        version = _user_version(connection)
        objects = _load_schema_objects(connection)
        if not objects:
            if version != 0:
                raise TaskDatabaseError(
                    f"Task database schema version {version} is not supported"
                )
            for statement in _V1_SCHEMA_STATEMENTS:
                connection.execute(statement)
            for statement in _V2_SCHEMA_STATEMENTS:
                connection.execute(statement)
            connection.execute(
                f"PRAGMA user_version = {TASK_DATABASE_SCHEMA_VERSION}"
            )
        elif version == 1:
            _validate_exact_schema(connection, _V1_EXPECTED_SCHEMA, 1)
            # RISK(breaking): v1 objects and rows are immutable. Every v2 object
            # and the version marker are added in this one transaction.
            for statement in _V2_SCHEMA_STATEMENTS:
                connection.execute(statement)
            connection.execute(
                f"PRAGMA user_version = {TASK_DATABASE_SCHEMA_VERSION}"
            )
        elif version != TASK_DATABASE_SCHEMA_VERSION:
            raise TaskDatabaseError(
                "Task database schema version "
                f"{version} is not supported; expected "
                f"{TASK_DATABASE_SCHEMA_VERSION}"
            )

        _validate_exact_schema(
            connection,
            _EXPECTED_SCHEMA,
            TASK_DATABASE_SCHEMA_VERSION,
        )
        _ensure_sqlite_sequence_rows(connection)
        _quick_check(connection)
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise TaskDatabaseError("Task database foreign key check failed")
        _validate_sqlite_sequence(connection)
        _secure_existing_sqlite_sidecars(database_path)
        connection.commit()
    except BaseException as error:
        try:
            connection.rollback()
        except sqlite3.Error as rollback_error:
            raise TaskDatabaseError(
                "Task database operation failed during rollback"
            ) from rollback_error
        if isinstance(error, TaskDatabaseError):
            raise
        if isinstance(error, sqlite3.Error):
            raise TaskDatabaseError("Task database operation failed") from error
        raise
    finally:
        _remove_empty_precreated_sidecars(prepared_sidecars)


def _begin_immediate(connection: sqlite3.Connection) -> None:
    # RISK(race): every Task writer acquires the same lock before reading state.
    connection.execute("BEGIN IMMEDIATE")


def _is_sqlite_busy_error(error: BaseException) -> bool:
    if not isinstance(error, sqlite3.Error):
        return False
    error_code = getattr(error, "sqlite_errorcode", None)
    if not isinstance(error_code, int):
        return False
    return error_code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}


def _user_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def _quick_check(connection: sqlite3.Connection) -> str:
    try:
        rows = connection.execute("PRAGMA quick_check").fetchall()
    except sqlite3.Error as error:
        raise TaskDatabaseError("Task database quick_check failed") from error
    values = [str(row[0]) for row in rows]
    if values != ["ok"]:
        raise TaskDatabaseError("Task database quick_check did not return ok")
    return "ok"


def _normalize_schema_sql(value: str) -> str:
    normalized: list[str] = []
    pending_space = False
    quote: str | None = None
    index = 0
    while index < len(value):
        character = value[index]
        if quote is not None:
            normalized.append(character)
            closing_quote = "]" if quote == "[" else quote
            if character == closing_quote:
                if index + 1 < len(value) and value[index + 1] == closing_quote:
                    normalized.append(value[index + 1])
                    index += 2
                    continue
                quote = None
            index += 1
            continue

        if character.isspace():
            pending_space = bool(normalized)
            index += 1
            continue
        if character in {"'", '"', "`", "["}:
            if pending_space:
                normalized.append(" ")
                pending_space = False
            quote = character
            normalized.append(character)
            index += 1
            continue
        if character == ";" and not value[index + 1 :].strip():
            break
        if pending_space:
            normalized.append(" ")
            pending_space = False
        normalized.append(
            chr(ord(character) + 32) if "A" <= character <= "Z" else character
        )
        index += 1

    if quote is not None:
        raise TaskDatabaseError("Task database schema SQL contains an open quote")
    return "".join(normalized).strip()


def _load_schema_objects(
    connection: sqlite3.Connection,
) -> dict[tuple[str, str], str]:
    rows = connection.execute(
        """
        SELECT type, name, sql
        FROM sqlite_master
        WHERE type IN ('table', 'index', 'trigger', 'view')
          AND name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    ).fetchall()
    return {
        (str(row[0]), str(row[1])): str(row[2])
        for row in rows
        if row[2] is not None
    }


def _validate_exact_schema(
    connection: sqlite3.Connection,
    expected_schema: dict[tuple[str, str], str],
    expected_version: int,
) -> None:
    if _user_version(connection) != expected_version:
        raise TaskDatabaseError(
            "Task database schema version does not match the expected version"
        )
    actual = {
        key: _normalize_schema_sql(value)
        for key, value in _load_schema_objects(connection).items()
    }
    expected = {
        key: _normalize_schema_sql(value)
        for key, value in expected_schema.items()
    }
    if actual != expected:
        raise TaskDatabaseError(
            "Task database schema does not match the exact supported format"
        )


def _validate_supported_schema(connection: sqlite3.Connection) -> None:
    version = _user_version(connection)
    if version == 1:
        _validate_exact_schema(connection, _V1_EXPECTED_SCHEMA, 1)
    elif version == TASK_DATABASE_SCHEMA_VERSION:
        _validate_exact_schema(
            connection,
            _EXPECTED_SCHEMA,
            TASK_DATABASE_SCHEMA_VERSION,
        )
    else:
        raise TaskDatabaseError(
            f"Task database schema version {version} is not supported"
        )


def _prepare_database_path(database_path: str | Path) -> Path:
    try:
        candidate = Path(database_path).expanduser().absolute()
        _assert_no_symlink_components(candidate)
    except TaskDatabaseError:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise TaskDatabaseError(
            "database_path must be a valid filesystem path"
        ) from error
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise TaskDatabaseError(
            "database parent directory could not be created safely"
        ) from error
    try:
        _assert_no_symlink_components(candidate)
        resolved = candidate.resolve(strict=False)
    except TaskDatabaseError:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise TaskDatabaseError(
            "database_path must be a valid filesystem path"
        ) from error
    if not resolved.parent.is_dir():
        raise TaskDatabaseError("database parent directory must be a directory")
    _assert_safe_file(resolved, required=False)
    return resolved


def _prepare_operation_lock_path(database_path: Path) -> Path:
    return _prepare_database_lock_path(database_path, purpose="operation")


def _prepare_write_lock_path(database_path: Path) -> Path:
    return _prepare_database_lock_path(database_path, purpose="write")


def _prepare_database_lock_path(database_path: Path, *, purpose: str) -> Path:
    lock_path = database_path.with_name(f"{database_path.name}.{purpose}.lock")
    _assert_no_symlink_components(lock_path)
    _ensure_secure_database_file(lock_path)
    if os.name == "nt":
        descriptor = _open_operation_lock_file(lock_path)
        try:
            if os.fstat(descriptor).st_size < _WINDOWS_OPERATION_LOCK_BYTES:
                os.ftruncate(descriptor, _WINDOWS_OPERATION_LOCK_BYTES)
                os.fsync(descriptor)
        except OSError as error:
            raise TaskDatabaseError(
                "Task database operation lock could not be initialized"
            ) from error
        finally:
            os.close(descriptor)
    return lock_path


@contextmanager
def _database_operation_lock(
    lock_path: Path,
    *,
    exclusive: bool = False,
    timeout_seconds: float = _OPERATION_LOCK_TIMEOUT_SECONDS,
    busy_message: str = "Task database operation lock timed out",
) -> Iterator[None]:
    state = _operation_barrier_state(lock_path)
    if exclusive:
        _enter_exclusive_operation(
            state,
            lock_path,
            timeout_seconds=timeout_seconds,
            busy_message=busy_message,
        )
        try:
            yield
        finally:
            _leave_exclusive_operation(state)
        return

    nested_exclusive = _enter_shared_operation(
        state,
        lock_path,
        timeout_seconds=timeout_seconds,
        busy_message=busy_message,
    )
    try:
        yield
    finally:
        if not nested_exclusive:
            _leave_shared_operation(state)


@contextmanager
def _database_transaction_scope(database_path: Path) -> Iterator[None]:
    key = os.path.normcase(str(database_path))
    active_paths = getattr(_ACTIVE_TRANSACTION_PATHS, "paths", None)
    if active_paths is None:
        active_paths = set()
        _ACTIVE_TRANSACTION_PATHS.paths = active_paths
    if key in active_paths:
        raise TaskDatabaseError(
            "Nested Task database transactions are not supported"
        )
    active_paths.add(key)
    try:
        yield
    finally:
        active_paths.remove(key)
        if not active_paths:
            del _ACTIVE_TRANSACTION_PATHS.paths


def _operation_barrier_state(lock_path: Path) -> _OperationBarrierState:
    key = os.path.normcase(str(lock_path))
    with _OPERATION_BARRIERS_LOCK:
        return _OPERATION_BARRIERS.setdefault(key, _OperationBarrierState())


def _enter_shared_operation(
    state: _OperationBarrierState,
    lock_path: Path,
    *,
    timeout_seconds: float,
    busy_message: str,
) -> bool:
    thread_id = threading.get_ident()
    deadline = time.monotonic() + timeout_seconds
    with state.condition:
        if state.exclusive_owner == thread_id:
            return True
        while state.exclusive_owner is not None or state.exclusive_pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TaskDatabaseError(busy_message)
            state.condition.wait(min(remaining, _OPERATION_LOCK_RETRY_SECONDS))
        if state.shared_count == 0:
            state.shared_file_lock = _acquire_operation_file_lock(
                lock_path,
                exclusive=False,
                timeout_seconds=max(0.0, deadline - time.monotonic()),
                busy_message=busy_message,
            )
        state.shared_count += 1
    return False


def _leave_shared_operation(state: _OperationBarrierState) -> None:
    held_lock: _HeldOperationFileLock | None = None
    with state.condition:
        if state.shared_count <= 0:
            raise TaskDatabaseError("Task database shared lock state is invalid")
        state.shared_count -= 1
        if state.shared_count == 0:
            held_lock = state.shared_file_lock
            state.shared_file_lock = None
    if held_lock is not None:
        try:
            _release_operation_file_lock(held_lock, exclusive=False)
        finally:
            with state.condition:
                state.condition.notify_all()


def _enter_exclusive_operation(
    state: _OperationBarrierState,
    lock_path: Path,
    *,
    timeout_seconds: float,
    busy_message: str,
) -> None:
    thread_id = threading.get_ident()
    with state.condition:
        if state.exclusive_owner == thread_id:
            state.exclusive_depth += 1
            return
        deadline = time.monotonic() + timeout_seconds
        while (
            state.shared_count > 0
            or state.exclusive_owner is not None
            or state.exclusive_pending
        ):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TaskDatabaseError(busy_message)
            state.condition.wait(min(remaining, _OPERATION_LOCK_RETRY_SECONDS))
        state.exclusive_pending = True
        try:
            held_lock = _acquire_operation_file_lock(
                lock_path,
                exclusive=True,
                timeout_seconds=max(0.0, deadline - time.monotonic()),
                busy_message=busy_message,
            )
        except BaseException:
            state.exclusive_pending = False
            state.condition.notify_all()
            raise
        state.exclusive_pending = False
        state.exclusive_owner = thread_id
        state.exclusive_depth = 1
        state.exclusive_file_lock = held_lock
        state.condition.notify_all()


def _leave_exclusive_operation(state: _OperationBarrierState) -> None:
    held_lock: _HeldOperationFileLock | None = None
    with state.condition:
        if (
            state.exclusive_owner != threading.get_ident()
            or state.exclusive_depth <= 0
        ):
            raise TaskDatabaseError("Task database exclusive lock state is invalid")
        state.exclusive_depth -= 1
        if state.exclusive_depth == 0:
            held_lock = state.exclusive_file_lock
            state.exclusive_file_lock = None
            state.exclusive_owner = None
    if held_lock is not None:
        try:
            _release_operation_file_lock(held_lock, exclusive=True)
        finally:
            with state.condition:
                state.condition.notify_all()


def _open_operation_lock_file(lock_path: Path) -> int:
    _assert_safe_file(lock_path, required=True)
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOINHERIT", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags)
    except OSError as error:
        raise TaskDatabaseError("Task database operation lock is unsafe") from error
    try:
        descriptor_stat = os.fstat(descriptor)
        path_stat = os.lstat(lock_path)
        if not stat.S_ISREG(descriptor_stat.st_mode):
            raise TaskDatabaseError("Task database operation lock is not a file")
        if stat.S_ISLNK(path_stat.st_mode) or (
            descriptor_stat.st_dev,
            descriptor_stat.st_ino,
        ) != (path_stat.st_dev, path_stat.st_ino):
            raise TaskDatabaseError("Task database operation lock path changed")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _acquire_operation_file_lock(
    lock_path: Path,
    *,
    exclusive: bool,
    timeout_seconds: float,
    busy_message: str,
) -> _HeldOperationFileLock:
    descriptor = _open_operation_lock_file(lock_path)
    deadline = time.monotonic() + timeout_seconds
    try:
        if os.name == "nt":
            import msvcrt

            while True:
                last_error: OSError | None = None
                if exclusive:
                    try:
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(
                            descriptor,
                            msvcrt.LK_NBLCK,
                            _WINDOWS_OPERATION_LOCK_BYTES,
                        )
                        return _HeldOperationFileLock(descriptor, None)
                    except OSError as error:
                        last_error = error
                else:
                    first_slot = os.getpid() % _WINDOWS_OPERATION_LOCK_BYTES
                    for offset in range(_WINDOWS_OPERATION_LOCK_BYTES):
                        slot = (
                            first_slot + offset
                        ) % _WINDOWS_OPERATION_LOCK_BYTES
                        try:
                            os.lseek(descriptor, slot, os.SEEK_SET)
                            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                            return _HeldOperationFileLock(descriptor, slot)
                        except OSError as error:
                            last_error = error
                if time.monotonic() >= deadline:
                    raise TaskDatabaseError(busy_message) from last_error
                time.sleep(_OPERATION_LOCK_RETRY_SECONDS)

        import fcntl

        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        while True:
            try:
                fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
                return _HeldOperationFileLock(descriptor, None)
            except OSError as error:
                if time.monotonic() >= deadline:
                    raise TaskDatabaseError(busy_message) from error
                time.sleep(_OPERATION_LOCK_RETRY_SECONDS)
    except BaseException:
        os.close(descriptor)
        raise


def _release_operation_file_lock(
    held_lock: _HeldOperationFileLock,
    *,
    exclusive: bool,
) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            offset = 0 if exclusive else held_lock.reader_slot
            if offset is None:
                raise TaskDatabaseError(
                    "Task database shared lock slot is missing"
                )
            length = _WINDOWS_OPERATION_LOCK_BYTES if exclusive else 1
            os.lseek(held_lock.descriptor, offset, os.SEEK_SET)
            msvcrt.locking(held_lock.descriptor, msvcrt.LK_UNLCK, length)
        else:
            import fcntl

            fcntl.flock(held_lock.descriptor, fcntl.LOCK_UN)
    except OSError as error:
        raise TaskDatabaseError(
            "Task database operation lock could not be released"
        ) from error
    finally:
        os.close(held_lock.descriptor)


def _prepare_output_path(path: str | Path) -> Path:
    try:
        candidate = Path(path).expanduser().absolute()
        _assert_no_symlink_components(candidate)
        candidate.parent.mkdir(parents=True, exist_ok=True)
        _assert_no_symlink_components(candidate)
        resolved = candidate.resolve(strict=False)
    except TaskDatabaseError:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise TaskDatabaseError("backup path is invalid") from error
    if not resolved.parent.is_dir():
        raise TaskDatabaseError("backup parent must be a directory")
    _assert_safe_file(resolved, required=False)
    return resolved


def _prepare_existing_input_path(path: str | Path) -> Path:
    try:
        candidate = Path(path).expanduser().absolute()
        _assert_no_symlink_components(candidate)
        resolved = candidate.resolve(strict=True)
    except TaskDatabaseError:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise TaskDatabaseError("backup path is invalid") from error
    _assert_safe_file(resolved, required=True)
    return resolved


def _assert_no_symlink_components(path: Path) -> None:
    try:
        if any(component.is_symlink() for component in (path, *path.parents)):
            raise TaskDatabaseError(
                "database path and its parents must not be symbolic links"
            )
    except TaskDatabaseError:
        raise
    except (OSError, ValueError) as error:
        raise TaskDatabaseError("database path could not be checked safely") from error


def _assert_safe_file(path: Path, *, required: bool) -> None:
    _assert_no_symlink_components(path)
    try:
        if not path.parent.is_dir():
            raise TaskDatabaseError("database parent directory must be a directory")
        if required and not path.exists():
            raise TaskDatabaseError("Task database file is missing")
        if path.exists() and not path.is_file():
            raise TaskDatabaseError("database_path must be a regular file")
    except TaskDatabaseError:
        raise
    except (OSError, ValueError) as error:
        raise TaskDatabaseError("database path could not be checked safely") from error


def _create_owner_only_file(path: Path) -> int:
    if os.name == "nt":
        return _windows_create_owner_only_file(path)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags, 0o600)


def _windows_create_owner_only_file(path: Path) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _, sid = _windows_identity()
    security_descriptor = wintypes.LPVOID()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    local_free = kernel32.LocalFree
    local_free.argtypes = (wintypes.LPVOID,)
    local_free.restype = wintypes.LPVOID
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.ULONG),
    )
    convert.restype = wintypes.BOOL
    sddl = f"O:{sid}D:P(A;;FA;;;{sid})"
    if not convert(sddl, 1, ctypes.byref(security_descriptor), None):
        error_code = ctypes.get_last_error()
        raise OSError(error_code, "owner-only security descriptor could not be built")

    class SecurityAttributes(ctypes.Structure):
        _fields_ = (
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        )

    attributes = SecurityAttributes(
        ctypes.sizeof(SecurityAttributes),
        security_descriptor,
        False,
    )
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(SecurityAttributes),
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    try:
        handle = create_file(
            str(path),
            0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
            0x00000001 | 0x00000002 | 0x00000004,  # share read/write/delete
            ctypes.byref(attributes),
            1,  # CREATE_NEW
            0x00000080,  # FILE_ATTRIBUTE_NORMAL
            None,
        )
        error_code = ctypes.get_last_error()
    finally:
        local_free(security_descriptor)

    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        if error_code in {80, 183}:  # ERROR_FILE_EXISTS / ERROR_ALREADY_EXISTS
            raise FileExistsError(error_code, "file already exists", str(path))
        raise OSError(error_code, "owner-only file could not be created", str(path))
    try:
        return msvcrt.open_osfhandle(
            int(handle),
            os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0),
        )
    except OSError:
        close_handle(handle)
        raise


def _ensure_secure_database_file(path: Path) -> None:
    try:
        descriptor = _create_owner_only_file(path)
    except FileExistsError:
        _assert_safe_file(path, required=True)
        _ensure_owner_only_permissions(path)
        return
    except OSError as error:
        raise TaskDatabaseError(
            "Task database file could not be created safely"
        ) from error
    try:
        descriptor_stat = os.fstat(descriptor)
        path_stat = os.lstat(path)
        if not stat.S_ISREG(descriptor_stat.st_mode):
            raise TaskDatabaseError("Task database must be a regular file")
        if stat.S_ISLNK(path_stat.st_mode) or (
            descriptor_stat.st_dev,
            descriptor_stat.st_ino,
        ) != (path_stat.st_dev, path_stat.st_ino):
            raise TaskDatabaseError("Task database path changed during creation")
    finally:
        os.close(descriptor)
    _ensure_owner_only_permissions(path)


def _assert_sqlite_connection_preconditions(path: Path) -> None:
    uses_wal = _database_header_uses_wal(path)
    _assert_owner_only_sqlite_sidecars(path)
    if not uses_wal:
        return
    required_sidecars = (Path(f"{path}-wal"), Path(f"{path}-shm"))
    if any(not sidecar.is_file() for sidecar in required_sidecars):
        raise TaskDatabaseError(
            "WAL database requires existing owner-only sidecars before opening"
        )


def _database_header_uses_wal(path: Path) -> bool:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOINHERIT", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise TaskDatabaseError(
            "Task database header could not be checked safely"
        ) from error
    try:
        descriptor_stat = os.fstat(descriptor)
        header = os.read(descriptor, 20)
        path_stat = os.lstat(path)
        if not stat.S_ISREG(descriptor_stat.st_mode):
            raise TaskDatabaseError("Task database must be a regular file")
        if stat.S_ISLNK(path_stat.st_mode) or (
            descriptor_stat.st_dev,
            descriptor_stat.st_ino,
        ) != (path_stat.st_dev, path_stat.st_ino):
            raise TaskDatabaseError(
                "Task database path changed during WAL header check"
            )
    except TaskDatabaseError:
        raise
    except OSError as error:
        raise TaskDatabaseError(
            "Task database header could not be checked safely"
        ) from error
    finally:
        os.close(descriptor)
    return (
        header[:16] == b"SQLite format 3\x00"
        and header[18:20] == b"\x02\x02"
    )


def _connect_database_file(path: Path, *, mode: str) -> sqlite3.Connection:
    _assert_safe_file(path, required=True)
    _assert_sqlite_connection_preconditions(path)
    try:
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode={mode}",
            timeout=5,
            uri=True,
        )
    except sqlite3.Error as error:
        raise TaskDatabaseError("SQLite file could not be opened") from error
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def _offline_database_gate(path: Path) -> Iterator[_OfflineRestoreGate]:
    _assert_owner_only_sqlite_sidecars(path)
    write_gate = _connect_database_file(path, mode="rw")
    source: sqlite3.Connection | None = None
    gate: _OfflineRestoreGate | None = None
    prepared_sidecars: tuple[_PreparedSidecar, ...] = ()
    try:
        write_gate.execute("PRAGMA busy_timeout = 0")
        try:
            _require_standalone_journal(write_gate, "live Task database")
            prepared_sidecars = _prepare_secure_sqlite_write_sidecars(
                write_gate,
                path,
            )
            write_gate.execute("BEGIN IMMEDIATE")
        except (sqlite3.Error, TaskDatabaseError) as error:
            _remove_empty_precreated_sidecars(prepared_sidecars)
            raise TaskDatabaseError(_RESTORE_OFFLINE_MESSAGE) from error
        source = _connect_database_file(path, mode="rw")
        source.execute("PRAGMA busy_timeout = 0")
        source.execute("BEGIN")
        source.execute("SELECT COUNT(*) FROM sqlite_schema").fetchone()
        gate = _OfflineRestoreGate(write_gate, source)
        yield gate
    finally:
        try:
            if gate is not None:
                gate.close_snapshot()
            elif source is not None:
                source.rollback()
                source.close()
        finally:
            try:
                write_gate.rollback()
            finally:
                write_gate.close()
                _remove_empty_precreated_sidecars(prepared_sidecars)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise TaskDatabaseError("Task database file could not be hashed") from error
    return digest.hexdigest()


def _database_content_hash(connection: sqlite3.Connection) -> str:
    table_names = set(TASK_DATABASE_TABLES)
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'"
    ).fetchone():
        table_names.add("sqlite_sequence")

    digest = hashlib.sha256()
    for table_name in sorted(table_names):
        quoted_table = _quote_sqlite_identifier(table_name)
        columns = tuple(
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({quoted_table})")
        )
        if not columns:
            raise TaskDatabaseError(
                f"Task database table {table_name} has no readable columns"
            )
        encoded_rows = sorted(
            json.dumps(
                [_encode_sqlite_value(value) for value in row],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            for row in connection.execute(f"SELECT * FROM {quoted_table}")
        )
        payload = json.dumps(
            [table_name, columns, encoded_rows],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _quote_sqlite_identifier(value: str) -> str:
    escaped = value.replace('"', '""')
    return f'"{escaped}"'


def _encode_sqlite_value(value: object) -> tuple[str, str]:
    if value is None:
        return ("null", "")
    if isinstance(value, bytes):
        return ("blob", value.hex())
    if isinstance(value, int):
        return ("integer", str(value))
    if isinstance(value, float):
        return ("real", value.hex())
    if isinstance(value, str):
        return ("text", value)
    raise TaskDatabaseError("Task database contains an unsupported SQLite value")


def _validate_exact_database(
    connection: sqlite3.Connection,
    *,
    expected_content_hash: str | None = None,
) -> str:
    _quick_check(connection)
    _validate_exact_schema(
        connection,
        _EXPECTED_SCHEMA,
        TASK_DATABASE_SCHEMA_VERSION,
    )
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise TaskDatabaseError(
            "Task database foreign key check failed: "
            f"{len(foreign_key_errors)} error(s)"
        )
    _validate_sqlite_sequence(connection)
    content_hash = _database_content_hash(connection)
    if expected_content_hash is not None and content_hash != expected_content_hash:
        raise TaskDatabaseError("Task database content hash does not match")
    return content_hash


def _validate_sqlite_sequence(connection: sqlite3.Connection) -> None:
    rows = connection.execute("SELECT name, seq FROM sqlite_sequence").fetchall()
    names = [str(row[0]) for row in rows]
    unexpected = set(names) - _AUTOINCREMENT_TABLES
    if unexpected:
        raise TaskDatabaseError("Task database has an unexpected sequence table")
    if len(rows) != len(_AUTOINCREMENT_TABLES) or set(names) != _AUTOINCREMENT_TABLES:
        raise TaskDatabaseError(
            "Task database sequence rows must contain each AUTOINCREMENT table once"
        )
    for raw_name, raw_sequence in rows:
        table_name = str(raw_name)
        if not isinstance(raw_sequence, int) or raw_sequence < 0:
            raise TaskDatabaseError("Task database sequence value is invalid")
        quoted_table = _quote_sqlite_identifier(table_name)
        quoted_primary_key = _quote_sqlite_identifier(
            _AUTOINCREMENT_PRIMARY_KEYS[table_name]
        )
        maximum = connection.execute(
            f"SELECT COALESCE(MAX({quoted_primary_key}), 0) FROM {quoted_table}"
        ).fetchone()
        if maximum is None or raw_sequence < int(maximum[0]):
            raise TaskDatabaseError("Task database sequence is below its row high-water")


def _ensure_sqlite_sequence_rows(connection: sqlite3.Connection) -> None:
    for table_name, primary_key in sorted(_AUTOINCREMENT_PRIMARY_KEYS.items()):
        quoted_table = _quote_sqlite_identifier(table_name)
        quoted_primary_key = _quote_sqlite_identifier(primary_key)
        connection.execute(
            "INSERT INTO sqlite_sequence(name, seq) "
            f"SELECT ?, COALESCE((SELECT MAX({quoted_primary_key}) "
            f"FROM {quoted_table}), 0) "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM sqlite_sequence WHERE name = ?"
            ")",
            (table_name, table_name),
        )


def _validate_restore_candidate(
    connection: sqlite3.Connection,
    *,
    expected_content_hash: str,
) -> str:
    return _validate_exact_database(
        connection,
        expected_content_hash=expected_content_hash,
    )


def _require_standalone_journal(
    connection: sqlite3.Connection,
    description: str,
) -> None:
    journal_mode = connection.execute("PRAGMA journal_mode = DELETE").fetchone()
    if journal_mode is None or str(journal_mode[0]).casefold() != "delete":
        raise TaskDatabaseError(f"{description} is not a standalone SQLite database")


def _database_path_for_connection(connection: sqlite3.Connection) -> Path:
    for _, database_name, raw_path in connection.execute("PRAGMA database_list"):
        if str(database_name) == "main" and raw_path:
            return Path(str(raw_path)).resolve(strict=True)
    raise TaskDatabaseError("Task database connection has no main file")


def _assert_safe_sqlite_sidecars(database_path: Path) -> None:
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        sidecar = Path(f"{database_path}{suffix}")
        if sidecar.exists() or sidecar.is_symlink():
            _assert_safe_file(sidecar, required=True)


def _assert_owner_only_sqlite_sidecars(database_path: Path) -> None:
    _assert_safe_sqlite_sidecars(database_path)
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        sidecar = Path(f"{database_path}{suffix}")
        if sidecar.exists() or sidecar.is_symlink():
            _assert_owner_only_sidecar(sidecar)


def _assert_owner_only_sidecar(sidecar: Path) -> None:
    _assert_safe_file(sidecar, required=True)
    try:
        before = sidecar.stat()
        owner_only = _verify_owner_only_permissions(sidecar)
        after = sidecar.stat()
    except OSError as error:
        raise TaskDatabaseError("SQLite sidecar permissions are unsafe") from error
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise TaskDatabaseError("SQLite sidecar path changed during permission check")
    if not owner_only:
        raise TaskDatabaseError("SQLite sidecar permissions are unsafe")


def _secure_existing_sqlite_sidecars(database_path: Path) -> None:
    _assert_safe_sqlite_sidecars(database_path)
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        sidecar = Path(f"{database_path}{suffix}")
        if not sidecar.exists() and not sidecar.is_symlink():
            continue
        _assert_owner_only_sidecar(sidecar)


def _ensure_owner_only_permissions(path: Path) -> None:
    _assert_safe_file(path, required=True)
    try:
        before = path.stat()
        if not _verify_owner_only_permissions(path):
            _apply_owner_only_permissions(path)
        after = path.stat()
    except OSError as error:
        raise TaskDatabaseError("SQLite sidecar permissions are unsafe") from error
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise TaskDatabaseError("SQLite sidecar path changed during permission check")
    if not _verify_owner_only_permissions(path):
        raise TaskDatabaseError("SQLite sidecar permissions are unsafe")


def _prepare_secure_sqlite_write_sidecars(
    connection: sqlite3.Connection,
    database_path: Path,
) -> tuple[_PreparedSidecar, ...]:
    journal_row = connection.execute("PRAGMA main.journal_mode").fetchone()
    journal_mode = str(journal_row[0]).casefold() if journal_row else ""
    suffixes = ("-wal", "-shm") if journal_mode == "wal" else ("-journal",)
    created: list[_PreparedSidecar] = []
    try:
        for suffix in suffixes:
            sidecar = Path(f"{database_path}{suffix}")
            try:
                descriptor = _create_owner_only_file(sidecar)
            except FileExistsError:
                _assert_owner_only_sidecar(sidecar)
            except OSError as error:
                raise TaskDatabaseError(
                    "SQLite sidecar could not be created securely"
                ) from error
            else:
                try:
                    descriptor_stat = os.fstat(descriptor)
                    created.append(
                        _PreparedSidecar(
                            path=sidecar,
                            device=descriptor_stat.st_dev,
                            inode=descriptor_stat.st_ino,
                        )
                    )
                finally:
                    os.close(descriptor)
                _assert_owner_only_sidecar(sidecar)
    except BaseException as preparation_error:
        try:
            _remove_empty_precreated_sidecars(tuple(created))
        except BaseException as cleanup_error:
            combined_error = TaskDatabaseError(
                "SQLite sidecar preparation failed and cleanup also failed; "
                f"preparation error: {preparation_error}; "
                f"cleanup error: {cleanup_error}"
            )
            combined_error.add_note(
                f"Original sidecar preparation failure: {preparation_error!r}"
            )
            raise combined_error from cleanup_error
        raise
    return tuple(created)


def _remove_empty_precreated_sidecars(
    sidecars: tuple[_PreparedSidecar, ...],
) -> None:
    synced_directories: set[Path] = set()
    for prepared in sidecars:
        try:
            if not prepared.path.exists():
                continue
            path_stat = prepared.path.stat()
            if (
                path_stat.st_dev,
                path_stat.st_ino,
                path_stat.st_size,
            ) != (prepared.device, prepared.inode, 0):
                continue
            _assert_safe_file(prepared.path, required=True)
            prepared.path.unlink()
            synced_directories.add(prepared.path.parent)
        except OSError as error:
            raise TaskDatabaseError(
                "empty SQLite sidecar could not be removed"
            ) from error
    for directory in synced_directories:
        _sync_directory(directory)


def _assert_no_sqlite_sidecars(database_path: Path) -> None:
    _assert_owner_only_sqlite_sidecars(database_path)
    if any(
        Path(f"{database_path}{suffix}").exists()
        or Path(f"{database_path}{suffix}").is_symlink()
        for suffix in _SQLITE_SIDECAR_SUFFIXES
    ):
        raise TaskDatabaseError("standalone Task database has SQLite sidecars")


def _finalize_database_file(
    database_path: Path,
    *,
    expected_content_hash: str,
    description: str,
) -> str:
    _apply_owner_only_permissions(database_path)
    _sync_file(database_path)
    _sync_directory(database_path.parent)
    if not _verify_owner_only_permissions(database_path):
        raise TaskDatabaseError(f"{description} permissions are unsafe")
    _assert_no_sqlite_sidecars(database_path)
    file_hash = _file_sha256(database_path)
    with closing(_connect_database_file(database_path, mode="ro")) as readback:
        journal_mode = readback.execute("PRAGMA journal_mode").fetchone()
        if journal_mode is None or str(journal_mode[0]).casefold() != "delete":
            raise TaskDatabaseError(f"{description} journal is not standalone")
        _validate_exact_database(
            readback,
            expected_content_hash=expected_content_hash,
        )
    if _file_sha256(database_path) != file_hash:
        raise TaskDatabaseError(f"{description} changed during readback")
    _assert_no_sqlite_sidecars(database_path)
    return file_hash


def _capture_database_artifact(
    source: sqlite3.Connection,
    directory: Path,
    *,
    purpose: str,
) -> _DatabaseArtifact:
    source_content_hash = _validate_exact_database(source)
    artifact = _new_secure_temporary_file(directory, purpose)
    try:
        with closing(_connect_database_file(artifact, mode="rw")) as target:
            prepared_sidecars = _prepare_secure_sqlite_write_sidecars(
                target,
                artifact,
            )
            try:
                source.backup(target)
                target.commit()
            finally:
                _remove_empty_precreated_sidecars(prepared_sidecars)
            _require_standalone_journal(target, f"{purpose} artifact")
            _validate_exact_database(
                target,
                expected_content_hash=source_content_hash,
            )
        file_hash = _finalize_database_file(
            artifact,
            expected_content_hash=source_content_hash,
            description=f"{purpose} artifact",
        )
        return _DatabaseArtifact(
            artifact=artifact,
            file_hash=file_hash,
            content_hash=source_content_hash,
        )
    except BaseException:
        _remove_restore_artifact(artifact)
        raise


def _capture_restore_preimage(
    source: sqlite3.Connection,
    database_path: Path,
) -> _DatabaseArtifact:
    return _capture_database_artifact(
        source,
        database_path.parent,
        purpose="rollback-preimage",
    )


def _publish_database_artifact(
    artifact: _DatabaseArtifact,
    destination: Path,
) -> None:
    if _file_sha256(artifact.artifact) != artifact.file_hash:
        raise TaskDatabaseError("Task database artifact changed before publish")
    _assert_no_sqlite_sidecars(artifact.artifact)
    if destination.exists():
        _publish_existing_database_artifact(artifact, destination)
        return
    _publish_new_database_artifact(artifact, destination)


def _publish_existing_database_artifact(
    artifact: _DatabaseArtifact,
    destination: Path,
) -> None:
    _assert_safe_file(destination, required=True)
    _assert_existing_destination_is_standalone(destination)
    if not _verify_owner_only_permissions(destination):
        raise TaskDatabaseError("Task database backup permissions are unsafe")
    target = _connect_database_file(destination, mode="rw")
    prepared_sidecars: tuple[_PreparedSidecar, ...] = ()
    old_file_hash: str | None = None
    old_content_hash: str | None = None
    committed = False
    try:
        target.execute("PRAGMA busy_timeout = 0")
        try:
            journal_mode = target.execute("PRAGMA journal_mode").fetchone()
            if journal_mode is None or str(journal_mode[0]).casefold() != "delete":
                raise TaskDatabaseError(
                    "Task database backup destination must be standalone"
                )
            prepared_sidecars = _prepare_secure_sqlite_write_sidecars(
                target,
                destination,
            )
            target.execute("BEGIN EXCLUSIVE")
        except (sqlite3.Error, TaskDatabaseError) as error:
            raise TaskDatabaseError(
                "Task database backup destination requires offline access"
            ) from error
        old_file_hash = _file_sha256(destination)
        old_content_hash = _validate_exact_database(target)
        _replace_database_content(target, artifact.artifact)
        _validate_existing_backup_candidate(
            target,
            destination,
            expected_content_hash=artifact.content_hash,
        )
        target.commit()
        committed = True
    except BaseException as error:
        try:
            target.rollback()
        finally:
            target.close()
            _remove_empty_precreated_sidecars(prepared_sidecars)
        if old_file_hash is not None and old_content_hash is not None:
            _assert_existing_backup_unchanged(
                destination,
                expected_file_hash=old_file_hash,
                expected_content_hash=old_content_hash,
            )
        raise error
    else:
        try:
            target.close()
            _remove_empty_precreated_sidecars(prepared_sidecars)
        except BaseException as error:
            committed_error = TaskDatabaseCommittedError(
                operation="backup",
                retained_artifact=artifact.artifact,
                detail="SQLite handle cleanup failed",
            )
            committed_error.__cause__ = error
            raise committed_error

    if committed:
        try:
            _assert_no_sqlite_sidecars(destination)
            _sync_directory(destination.parent)
        except BaseException as error:
            committed_error = TaskDatabaseCommittedError(
                operation="backup",
                retained_artifact=artifact.artifact,
                detail="destination finalization failed",
            )
            committed_error.__cause__ = error
            raise committed_error


def _assert_existing_destination_is_standalone(destination: Path) -> None:
    _assert_no_sqlite_sidecars(destination)
    try:
        with destination.open("rb") as stream:
            header = stream.read(20)
    except OSError as error:
        raise TaskDatabaseError(
            "Task database backup destination header could not be read"
        ) from error
    if len(header) < 20 or header[18:20] != b"\x01\x01":
        raise TaskDatabaseError(
            "Task database backup destination must be standalone DELETE journal"
        )


def _validate_existing_backup_candidate(
    target: sqlite3.Connection,
    destination: Path,
    *,
    expected_content_hash: str,
) -> None:
    _validate_exact_database(
        target,
        expected_content_hash=expected_content_hash,
    )
    _secure_existing_sqlite_sidecars(destination)
    if not _verify_owner_only_permissions(destination):
        raise TaskDatabaseError("Task database backup permissions are unsafe")
    _sync_file(destination)
    _validate_exact_database(
        target,
        expected_content_hash=expected_content_hash,
    )


def _assert_existing_backup_unchanged(
    destination: Path,
    *,
    expected_file_hash: str,
    expected_content_hash: str,
) -> None:
    if _file_sha256(destination) != expected_file_hash:
        raise TaskDatabaseError(
            "Task database backup failed and destination rollback changed bytes"
        )
    with closing(_connect_database_file(destination, mode="ro")) as readback:
        _validate_exact_database(
            readback,
            expected_content_hash=expected_content_hash,
        )


def _link_new_backup_artifact(artifact: Path, destination: Path) -> None:
    try:
        os.link(artifact, destination)
    except FileExistsError as error:
        raise TaskDatabaseError(
            "Task database backup destination requires offline access"
        ) from error
    except OSError as error:
        raise TaskDatabaseError("Task database backup could not be published") from error


def _publish_new_database_artifact(
    artifact: _DatabaseArtifact,
    destination: Path,
) -> None:
    _assert_safe_sqlite_sidecars(destination)
    try:
        _link_new_backup_artifact(artifact.artifact, destination)
        _assert_safe_file(destination, required=True)
        if not os.path.samefile(artifact.artifact, destination):
            raise TaskDatabaseError("Task database backup publication changed path")
        if not _verify_owner_only_permissions(destination):
            raise TaskDatabaseError("Task database backup permissions are unsafe")
        if _file_sha256(destination) != artifact.file_hash:
            raise TaskDatabaseError("Task database backup changed during publication")
        _assert_no_sqlite_sidecars(destination)
        _sync_file(destination)
        _sync_directory(destination.parent)
    except BaseException:
        try:
            if destination.exists() and os.path.samefile(
                artifact.artifact,
                destination,
            ):
                destination.unlink()
                _sync_directory(destination.parent)
        except OSError as cleanup_error:
            raise TaskDatabaseError(
                "partial Task database backup could not be removed"
            ) from cleanup_error
        raise


def _replace_database_content(
    connection: sqlite3.Connection,
    source_path: Path,
) -> None:
    connection.execute("ATTACH DATABASE ? AS restore_source", (str(source_path),))
    connection.execute("PRAGMA defer_foreign_keys = ON")
    table_columns: dict[str, tuple[str, ...]] = {}
    for table_name in sorted(TASK_DATABASE_TABLES):
        quoted_table = _quote_sqlite_identifier(table_name)
        main_columns = tuple(
            str(row[1])
            for row in connection.execute(
                f"PRAGMA main.table_info({quoted_table})"
            )
        )
        source_columns = tuple(
            str(row[1])
            for row in connection.execute(
                f"PRAGMA restore_source.table_info({quoted_table})"
            )
        )
        if not main_columns or source_columns != main_columns:
            raise TaskDatabaseError("restore source table columns do not match")
        table_columns[table_name] = main_columns
        connection.execute(f"DELETE FROM main.{quoted_table}")

    for table_name, columns in table_columns.items():
        quoted_table = _quote_sqlite_identifier(table_name)
        column_list = ", ".join(_quote_sqlite_identifier(name) for name in columns)
        connection.execute(
            f"INSERT INTO main.{quoted_table} ({column_list}) "
            f"SELECT {column_list} FROM restore_source.{quoted_table}"
        )

    connection.execute("DELETE FROM main.sqlite_sequence")
    connection.execute(
        "INSERT INTO main.sqlite_sequence(name, seq) "
        "SELECT name, seq FROM restore_source.sqlite_sequence"
    )


def _detach_database_if_attached(
    connection: sqlite3.Connection,
    database_name: str,
) -> None:
    attached_names = {
        str(row[1]) for row in connection.execute("PRAGMA database_list").fetchall()
    }
    if database_name in attached_names:
        quoted_name = _quote_sqlite_identifier(database_name)
        connection.execute(f"DETACH DATABASE {quoted_name}")


def _assert_database_matches_artifact(
    database_path: Path,
    artifact: _DatabaseArtifact,
) -> None:
    if not _verify_owner_only_permissions(database_path):
        raise TaskDatabaseError("rolled back Task database permissions are unsafe")
    with closing(_connect_database_file(database_path, mode="rw")) as connection:
        _validate_exact_database(
            connection,
            expected_content_hash=artifact.content_hash,
        )


def _remove_database_artifact(artifact: _DatabaseArtifact) -> None:
    _remove_restore_artifact(artifact.artifact)


def _remove_restore_artifact(artifact: Path) -> None:
    _remove_sqlite_sidecars(artifact)
    _remove_temporary_file(artifact)


def _new_secure_temporary_file(directory: Path, purpose: str) -> Path:
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".task-database-{purpose}-",
            suffix=".sqlite3",
            dir=directory,
        )
        os.close(descriptor)
        path = Path(raw_path).resolve(strict=True)
        _apply_owner_only_permissions(path)
        return path
    except (OSError, ValueError) as error:
        raise TaskDatabaseError(
            f"Task database {purpose} staging file could not be created"
        ) from error


def _remove_temporary_file(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
            _sync_directory(path.parent)
    except OSError as error:
        raise TaskDatabaseError(
            "Task database staging file could not be removed"
        ) from error


def _remove_sqlite_sidecars(database_path: Path) -> None:
    removed = False
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        sidecar = Path(f"{database_path}{suffix}")
        try:
            if sidecar.exists():
                _assert_safe_file(sidecar, required=True)
                sidecar.unlink()
                removed = True
        except OSError as error:
            raise TaskDatabaseError(
                "stale SQLite sidecar could not be removed"
            ) from error
    if removed:
        _sync_directory(database_path.parent)


def _require_distinct_paths(first: Path, second: Path) -> None:
    try:
        same = first == second or (
            first.exists() and second.exists() and os.path.samefile(first, second)
        )
    except OSError as error:
        raise TaskDatabaseError(
            "database paths could not be compared safely"
        ) from error
    if same:
        raise TaskDatabaseError("backup and live database paths must differ")


def _sync_file(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise TaskDatabaseError(
            "Task database staging file could not be synced"
        ) from error


def _sync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise TaskDatabaseError(
            "Task database directory could not be synced"
        ) from error


def _apply_owner_only_permissions(path: Path) -> None:
    if os.name != "nt":
        try:
            os.chmod(path, 0o600, follow_symlinks=False)
        except (NotImplementedError, OSError) as error:
            raise TaskDatabaseError(
                "Task database mode could not be restricted to 0600"
            ) from error
        return

    account, sid = _windows_identity()
    del account
    command = [
        "icacls.exe",
        str(path),
        "/inheritance:r",
        "/grant:r",
        f"*{sid}:(F)",
        "/Q",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise TaskDatabaseError("Task database ACL could not be restricted") from error
    if result.returncode != 0:
        raise TaskDatabaseError("Task database ACL could not be restricted")


def _verify_owner_only_permissions(path: Path) -> bool:
    try:
        if os.name != "nt":
            file_stat = path.stat()
            expected_uid = getattr(os, "geteuid", lambda: file_stat.st_uid)()
            return (
                stat.S_IMODE(file_stat.st_mode) == 0o600
                and file_stat.st_uid == expected_uid
            )
        sddl = _windows_security_descriptor_sddl(path)
        owner_match = re.search(r"O:(S-1(?:-\d+)+)", sddl, re.IGNORECASE)
        dacl_match = re.search(r"D:([^\(]*)(.*)$", sddl, re.IGNORECASE)
        if owner_match is None or dacl_match is None:
            return False
        owner_sid = owner_match.group(1).casefold()
        dacl_flags, ace_text = dacl_match.groups()
        aces = re.findall(r"\(([^\)]*)\)", ace_text)
        return "P" in dacl_flags.upper() and bool(aces) and all(
            len(parts := ace.split(";")) == 6
            and parts[0].upper() == "A"
            and "ID" not in parts[1].upper()
            and parts[2].upper() in {"FA", "0X1F01FF"}
            and parts[5].casefold() == owner_sid
            for ace in aces
        )
    except (OSError, TaskDatabaseError, ValueError):
        return False


def _windows_identity() -> tuple[str, str]:
    try:
        result = subprocess.run(
            ["whoami.exe", "/user", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise TaskDatabaseError("current Windows identity could not be read") from error
    if result.returncode != 0:
        raise TaskDatabaseError("current Windows identity could not be read")
    rows = list(csv.reader(result.stdout.splitlines()))
    if len(rows) != 1 or len(rows[0]) != 2:
        raise TaskDatabaseError("current Windows identity output is invalid")
    account, sid = (value.strip() for value in rows[0])
    if not account or re.fullmatch(r"S-1(?:-\d+)+", sid, re.IGNORECASE) is None:
        raise TaskDatabaseError("current Windows identity output is invalid")
    return account, sid


def _windows_security_descriptor_sddl(path: Path) -> str:
    import ctypes
    from ctypes import wintypes

    security_descriptor = wintypes.LPVOID()
    get_named_security_info = ctypes.windll.advapi32.GetNamedSecurityInfoW
    get_named_security_info.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPVOID),
    )
    get_named_security_info.restype = wintypes.DWORD
    result = get_named_security_info(
        str(path),
        1,  # SE_FILE_OBJECT
        0x00000001 | 0x00000004,  # OWNER + DACL_SECURITY_INFORMATION
        None,
        None,
        None,
        None,
        ctypes.byref(security_descriptor),
    )
    if result != 0 or not security_descriptor:
        raise TaskDatabaseError("Task database ACL could not be verified")

    sddl_pointer = wintypes.LPWSTR()
    convert = (
        ctypes.windll.advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
    )
    convert.argtypes = (
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.DWORD),
    )
    convert.restype = wintypes.BOOL
    try:
        if not convert(
            security_descriptor,
            1,
            0x00000001 | 0x00000004,
            ctypes.byref(sddl_pointer),
            None,
        ):
            raise TaskDatabaseError("Task database ACL could not be verified")
        return str(sddl_pointer.value)
    finally:
        if sddl_pointer:
            ctypes.windll.kernel32.LocalFree(sddl_pointer)
        ctypes.windll.kernel32.LocalFree(security_descriptor)
