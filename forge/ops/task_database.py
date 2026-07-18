"""Single owner for the additive Infinity Forge Task SQLite schema."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
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


class TaskDatabaseError(RuntimeError):
    """Raised when the shared Task database cannot be trusted."""


@dataclass(frozen=True, slots=True)
class _RestorePreimage:
    artifact: Path
    file_hash: str
    content_hash: str

    @property
    def rollback_artifact(self) -> Path:
        return self.artifact


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


class TaskDatabase:
    """Open, migrate, validate, back up, and restore one Task SQLite file."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = _prepare_database_path(database_path)
        _ensure_secure_database_file(self.database_path)
        self._operation_lock_path = _prepare_operation_lock_path(self.database_path)
        self._initialize()
        _apply_owner_only_permissions(self.database_path)
        if not self.verify_owner_only_permissions():
            raise TaskDatabaseError("Task database permissions are not owner-only")

    def connect(self) -> sqlite3.Connection:
        """Open one validated-path connection with foreign keys enabled."""

        _assert_safe_file(self.database_path, required=True)
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
        """Serialize a mutable operation with the shared BEGIN IMMEDIATE lock."""

        with (
            _database_operation_lock(self._operation_lock_path),
            closing(self.connect()) as connection,
        ):
            try:
                _begin_immediate(connection)
                yield connection
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
        with _database_operation_lock(self._operation_lock_path):
            return self._backup_locked(destination)

    def _backup_locked(self, destination: Path) -> Path:
        temporary = _new_secure_temporary_file(destination.parent, "backup")
        try:
            with (
                closing(self.connect()) as source,
                closing(_connect_database_file(temporary, mode="rw")) as target,
            ):
                source.backup(target)
                target.commit()
                _quick_check(target)
                _validate_supported_schema(target)
            _sync_file(temporary)
            _assert_safe_file(destination, required=False)
            os.replace(temporary, destination)
            _apply_owner_only_permissions(destination)
            if not _verify_owner_only_permissions(destination):
                raise TaskDatabaseError("Task database backup permissions are unsafe")
            return destination
        except TaskDatabaseError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise TaskDatabaseError("Task database backup failed") from error
        finally:
            _remove_temporary_file(temporary)

    def restore(self, backup_path: str | Path) -> None:
        """Validate a staged backup before atomically replacing the live file."""

        backup = _prepare_existing_input_path(backup_path)
        _require_distinct_paths(self.database_path, backup)
        with _database_operation_lock(
            self._operation_lock_path,
            exclusive=True,
            timeout_seconds=0.0,
            busy_message=_RESTORE_OFFLINE_MESSAGE,
        ):
            self._restore_locked(backup)

    def _restore_locked(self, backup: Path) -> None:
        temporary = _new_secure_temporary_file(self.database_path.parent, "restore")
        preimage: _RestorePreimage | None = None
        published = False
        keep_rollback_artifact = False
        try:
            with (
                closing(_connect_database_file(backup, mode="ro")) as source,
                closing(_connect_database_file(temporary, mode="rw")) as target,
            ):
                source.backup(target)
                target.commit()
                _quick_check(target)
                _validate_supported_schema(target)
                _initialize_database_connection(target)
                _quick_check(target)
                _validate_exact_schema(
                    target,
                    _EXPECTED_SCHEMA,
                    TASK_DATABASE_SCHEMA_VERSION,
                )
            _sync_file(temporary)
            with _offline_database_gate(self.database_path) as live:
                _quick_check(live)
                _validate_supported_schema(live)
                preimage = _capture_restore_preimage(
                    live,
                    self.database_path,
                )

            # RISK(data-loss): restore is an explicit offline replacement boundary.
            # A checked standalone SQLite preimage remains beside the live file until
            # every post-publish check succeeds or its logical data is restored.
            _remove_sqlite_sidecars(self.database_path)
            os.replace(temporary, self.database_path)
            published = True
            _apply_owner_only_permissions(self.database_path)
            self._initialize(_operation_locked=True)
            self.quick_check(_operation_locked=True)
            if not self.verify_owner_only_permissions():
                raise TaskDatabaseError(
                    "restored Task database permissions are unsafe"
                )
        except BaseException as error:
            if published and preimage is not None:
                try:
                    _restore_preimage(self.database_path, preimage)
                except BaseException as rollback_error:
                    keep_rollback_artifact = True
                    raise TaskDatabaseError(
                        "Task database restore failed and rollback failed; "
                        f"rollback artifact: {preimage.rollback_artifact}"
                    ) from rollback_error
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            if (
                not published
                and isinstance(error, TaskDatabaseError)
                and str(error) == _RESTORE_OFFLINE_MESSAGE
            ):
                raise
            raise TaskDatabaseError(
                "Task database backup could not be restored safely"
            ) from error
        finally:
            _remove_temporary_file(temporary)
            if preimage is not None and not keep_rollback_artifact:
                _remove_restore_preimage(preimage)

    def _initialize(self, *, _operation_locked: bool = False) -> None:
        if not _operation_locked:
            with _database_operation_lock(self._operation_lock_path):
                self._initialize(_operation_locked=True)
            return
        with closing(self.connect()) as connection:
            _initialize_database_connection(connection)


def _initialize_database_connection(connection: sqlite3.Connection) -> None:
    try:
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
        _quick_check(connection)
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise TaskDatabaseError("Task database foreign key check failed")
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


def _begin_immediate(connection: sqlite3.Connection) -> None:
    # RISK(race): every Task writer acquires the same lock before reading state.
    connection.execute("BEGIN IMMEDIATE")


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
    lock_path = database_path.with_name(f"{database_path.name}.operation.lock")
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
        if (
            state.shared_count > 0
            or state.exclusive_owner is not None
            or state.exclusive_pending
        ):
            raise TaskDatabaseError(busy_message)
        state.exclusive_pending = True
        try:
            held_lock = _acquire_operation_file_lock(
                lock_path,
                exclusive=True,
                timeout_seconds=timeout_seconds,
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


def _ensure_secure_database_file(path: Path) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOINHERIT", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        _assert_safe_file(path, required=True)
        _apply_owner_only_permissions(path)
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
    _apply_owner_only_permissions(path)


def _connect_database_file(path: Path, *, mode: str) -> sqlite3.Connection:
    _assert_safe_file(path, required=True)
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
def _offline_database_gate(path: Path) -> Iterator[sqlite3.Connection]:
    write_gate = _connect_database_file(path, mode="rw")
    source: sqlite3.Connection | None = None
    try:
        write_gate.execute("PRAGMA busy_timeout = 0")
        try:
            write_gate.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as error:
            raise TaskDatabaseError(_RESTORE_OFFLINE_MESSAGE) from error
        source = _connect_database_file(path, mode="rw")
        source.execute("PRAGMA busy_timeout = 0")
        source.execute("BEGIN")
        source.execute("SELECT COUNT(*) FROM sqlite_schema").fetchone()
        yield source
    finally:
        try:
            if source is not None:
                source.rollback()
                source.close()
        finally:
            try:
                write_gate.rollback()
            finally:
                write_gate.close()


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


def _capture_restore_preimage(
    source: sqlite3.Connection,
    database_path: Path,
) -> _RestorePreimage:
    artifact = _new_secure_temporary_file(
        database_path.parent,
        "rollback-preimage",
    )
    try:
        source_content_hash = _database_content_hash(source)
        with closing(_connect_database_file(artifact, mode="rw")) as target:
            source.backup(target)
            target.commit()
            journal_mode = target.execute("PRAGMA journal_mode = DELETE").fetchone()
            if journal_mode is None or str(journal_mode[0]).casefold() != "delete":
                raise TaskDatabaseError(
                    "rollback artifact is not a standalone SQLite database"
                )
            _quick_check(target)
            _validate_exact_schema(
                target,
                _EXPECTED_SCHEMA,
                TASK_DATABASE_SCHEMA_VERSION,
            )
            if connection_errors := target.execute(
                "PRAGMA foreign_key_check"
            ).fetchall():
                raise TaskDatabaseError(
                    "rollback artifact foreign key check failed: "
                    f"{len(connection_errors)} error(s)"
                )
            if _database_content_hash(target) != source_content_hash:
                raise TaskDatabaseError(
                    "rollback artifact does not match live database data"
                )
        _apply_owner_only_permissions(artifact)
        _sync_file(artifact)
        if not _verify_owner_only_permissions(artifact):
            raise TaskDatabaseError("rollback artifact permissions are unsafe")
        file_hash = _file_sha256(artifact)
        with closing(_connect_database_file(artifact, mode="ro")) as readback:
            _quick_check(readback)
            _validate_exact_schema(
                readback,
                _EXPECTED_SCHEMA,
                TASK_DATABASE_SCHEMA_VERSION,
            )
            if _database_content_hash(readback) != source_content_hash:
                raise TaskDatabaseError("rollback artifact readback does not match")
        if _file_sha256(artifact) != file_hash:
            raise TaskDatabaseError("rollback artifact changed during readback")
        return _RestorePreimage(
            artifact=artifact,
            file_hash=file_hash,
            content_hash=source_content_hash,
        )
    except BaseException:
        _remove_restore_artifact(artifact)
        raise


def _publish_preimage_file(source: Path, destination: Path) -> None:
    temporary = _new_secure_temporary_file(destination.parent, "rollback-publish")
    try:
        shutil.copyfile(source, temporary)
        _apply_owner_only_permissions(temporary)
        _sync_file(temporary)
        if _file_sha256(temporary) != _file_sha256(source):
            raise TaskDatabaseError("rollback publish copy is not exact")
        os.replace(temporary, destination)
    finally:
        _remove_temporary_file(temporary)


def _restore_preimage(
    database_path: Path,
    preimage: _RestorePreimage,
) -> None:
    _remove_sqlite_sidecars(database_path)
    _publish_preimage_file(preimage.artifact, database_path)
    if _file_sha256(database_path) != preimage.file_hash:
        raise TaskDatabaseError("rolled back Task database file is not exact")
    if not _verify_owner_only_permissions(database_path):
        raise TaskDatabaseError("rolled back Task database permissions are unsafe")
    with closing(_connect_database_file(database_path, mode="rw")) as connection:
        _quick_check(connection)
        _validate_exact_schema(
            connection,
            _EXPECTED_SCHEMA,
            TASK_DATABASE_SCHEMA_VERSION,
        )
        if _database_content_hash(connection) != preimage.content_hash:
            raise TaskDatabaseError("rolled back Task database data does not match")


def _remove_restore_preimage(preimage: _RestorePreimage) -> None:
    _remove_restore_artifact(preimage.artifact)


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
    except OSError as error:
        raise TaskDatabaseError(
            "Task database staging file could not be removed"
        ) from error


def _remove_sqlite_sidecars(database_path: Path) -> None:
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        sidecar = Path(f"{database_path}{suffix}")
        try:
            if sidecar.exists():
                _assert_safe_file(sidecar, required=True)
                sidecar.unlink()
        except OSError as error:
            raise TaskDatabaseError(
                "stale SQLite sidecar could not be removed"
            ) from error


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
