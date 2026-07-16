"""Immutable, task-scoped settings and their append-only SQLite history."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Never
from uuid import UUID

from forge.ops.task_options import MergeMode, Mode, TaskFlow


TASK_SETTINGS_FORMAT = "forge-task-settings/v1"
TASK_SETTINGS_SCHEMA_VERSION = 1
MAX_AUTO_MERGE_DURATION = timedelta(hours=12)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY_PATTERN = re.compile(r"^[^/\s]+/[^/\s]+$")
_AUTO_EXPIRY_UNSET = object()

_TASK_SETTINGS_TABLE_SQL = """
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

_TASK_SETTINGS_EVENTS_TABLE_SQL = """
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

_TERMINAL_EVENT_INDEX_SQL = """
CREATE UNIQUE INDEX task_settings_one_terminal_event
    ON task_settings_events (request_id)
    WHERE event_type IN ('cancelled', 'expired', 'merged')
"""

_EXPECTED_SCHEMA_OBJECTS = {
    ("table", "task_settings"): _TASK_SETTINGS_TABLE_SQL,
    ("table", "task_settings_events"): _TASK_SETTINGS_EVENTS_TABLE_SQL,
    ("index", "task_settings_one_terminal_event"): _TERMINAL_EVENT_INDEX_SQL,
}


class TaskSettingsError(ValueError):
    """Raised when Task settings are invalid or an immutable value would change."""


class TaskSettingsStatus(str, Enum):
    PREPARED = "prepared"
    ACTIVE = "active"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    MERGED = "merged"


class TaskSettingsEventType(str, Enum):
    PREPARED = "prepared"
    ISSUE_BOUND = "issue_bound"
    ACTIVE = "active"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    MERGED = "merged"


_TERMINAL_STATUSES = frozenset(
    {
        TaskSettingsStatus.CANCELLED,
        TaskSettingsStatus.EXPIRED,
        TaskSettingsStatus.MERGED,
    }
)
_STATUS_TO_EVENT = {
    TaskSettingsStatus.CANCELLED: TaskSettingsEventType.CANCELLED,
    TaskSettingsStatus.EXPIRED: TaskSettingsEventType.EXPIRED,
    TaskSettingsStatus.MERGED: TaskSettingsEventType.MERGED,
}
_EVENT_TO_STATUS = {
    TaskSettingsEventType.ACTIVE: TaskSettingsStatus.ACTIVE,
    TaskSettingsEventType.CANCELLED: TaskSettingsStatus.CANCELLED,
    TaskSettingsEventType.EXPIRED: TaskSettingsStatus.EXPIRED,
    TaskSettingsEventType.MERGED: TaskSettingsStatus.MERGED,
}


def _canonical_sha256(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_datetime(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TaskSettingsError(f"{field_name} must be a timezone-aware datetime")
    if value.utcoffset() is None:
        raise TaskSettingsError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _format_timestamp(value: datetime) -> str:
    normalized = _normalize_datetime(value, "timestamp")
    return normalized.isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise TaskSettingsError(f"stored {field_name} must be an RFC 3339 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise TaskSettingsError(f"stored {field_name} is not RFC 3339") from error
    normalized = _normalize_datetime(parsed, field_name)
    if _format_timestamp(normalized) != value:
        raise TaskSettingsError(f"stored {field_name} is not canonical RFC 3339")
    return normalized


@dataclass(frozen=True, slots=True)
class TaskContent:
    title: str
    description: str
    acceptance_criteria: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.title, str) or not self.title.strip():
            raise TaskSettingsError("title must be a non-empty string")
        if not isinstance(self.description, str):
            raise TaskSettingsError("description must be a string")
        if not isinstance(self.acceptance_criteria, tuple):
            raise TaskSettingsError("acceptance_criteria must be a tuple")
        if not self.acceptance_criteria or any(
            not isinstance(item, str) or not item.strip()
            for item in self.acceptance_criteria
        ):
            raise TaskSettingsError(
                "acceptance_criteria must contain non-empty strings"
            )


def task_content_hash(content: TaskContent) -> str:
    """Hash only the confirmed title, description, and acceptance criteria."""

    if not isinstance(content, TaskContent):
        raise TaskSettingsError("content must be a TaskContent")
    return _canonical_sha256(
        {
            "title": content.title,
            "description": content.description,
            "acceptance_criteria": list(content.acceptance_criteria),
        }
    )


@dataclass(frozen=True, slots=True)
class TaskSettings:
    format_version: str
    request_id: str
    repository: str
    issue_number: int | None
    mode: Mode
    task_content_hash: str
    task_flow: TaskFlow
    merge_mode: MergeMode
    confirmed_by: str
    confirmed_at: datetime
    auto_merge_expires_at: datetime | None
    status: TaskSettingsStatus
    task_settings_hash: str | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        if self.format_version != TASK_SETTINGS_FORMAT:
            raise TaskSettingsError(f"format_version must be {TASK_SETTINGS_FORMAT!r}")
        if not isinstance(self.request_id, str):
            raise TaskSettingsError("request_id must be a canonical UUID string")
        try:
            parsed_request_id = UUID(self.request_id)
        except ValueError as error:
            raise TaskSettingsError(
                "request_id must be a canonical UUID string"
            ) from error
        if str(parsed_request_id) != self.request_id:
            raise TaskSettingsError("request_id must be a canonical UUID string")
        if not isinstance(self.repository, str) or not _REPOSITORY_PATTERN.fullmatch(
            self.repository
        ):
            raise TaskSettingsError("repository must use OWNER/REPO format")
        if self.issue_number is not None and (
            type(self.issue_number) is not int or self.issue_number <= 0
        ):
            raise TaskSettingsError("issue_number must be a positive integer or null")
        if self.mode is not Mode.TASK:
            raise TaskSettingsError("mode must be Mode.TASK")
        if not isinstance(self.task_content_hash, str) or not _SHA256_PATTERN.fullmatch(
            self.task_content_hash
        ):
            raise TaskSettingsError("task_content_hash must be a lowercase SHA-256")
        if not isinstance(self.task_flow, TaskFlow):
            raise TaskSettingsError("task_flow must be a TaskFlow")
        if not isinstance(self.merge_mode, MergeMode):
            raise TaskSettingsError("merge_mode must be a MergeMode")
        if not isinstance(self.confirmed_by, str) or not self.confirmed_by.strip():
            raise TaskSettingsError("confirmed_by must be a non-empty string")
        if not isinstance(self.status, TaskSettingsStatus):
            raise TaskSettingsError("status must be a TaskSettingsStatus")

        confirmed_at = _normalize_datetime(self.confirmed_at, "confirmed_at")
        object.__setattr__(self, "confirmed_at", confirmed_at)
        expires_at = self.auto_merge_expires_at
        if expires_at is not None:
            expires_at = _normalize_datetime(
                expires_at,
                "auto_merge_expires_at",
            )
            object.__setattr__(self, "auto_merge_expires_at", expires_at)

        if self.merge_mode is MergeMode.MANUAL:
            if expires_at is not None:
                raise TaskSettingsError(
                    "manual merge_mode requires auto_merge_expires_at to be null"
                )
        else:
            if expires_at is None:
                raise TaskSettingsError(
                    "automatic merge_mode requires auto_merge_expires_at"
                )
            if expires_at <= confirmed_at:
                raise TaskSettingsError(
                    "auto_merge_expires_at must be after confirmed_at"
                )
            if expires_at > confirmed_at + MAX_AUTO_MERGE_DURATION:
                raise TaskSettingsError(
                    "auto_merge_expires_at must be no later than 12 hours after "
                    "confirmed_at"
                )

        if self.issue_number is None:
            if self.status is not TaskSettingsStatus.PREPARED:
                raise TaskSettingsError(
                    "only prepared settings may exist before issue binding"
                )
            object.__setattr__(self, "task_settings_hash", None)
            return

        object.__setattr__(
            self,
            "task_settings_hash",
            _canonical_sha256(_task_settings_payload(self)),
        )

    @classmethod
    def create(
        cls,
        *,
        request_id: str,
        repository: str,
        task_content: TaskContent,
        task_flow: TaskFlow,
        merge_mode: MergeMode,
        confirmed_by: str,
        confirmed_at: datetime,
        auto_merge_expires_at: datetime | None | object = _AUTO_EXPIRY_UNSET,
    ) -> TaskSettings:
        """Create one unbound prepared record without defaults from older formats."""

        if not isinstance(task_content, TaskContent):
            raise TaskSettingsError("task_content must be a TaskContent")
        if not isinstance(task_flow, TaskFlow):
            raise TaskSettingsError("task_flow must be a TaskFlow")
        if not isinstance(merge_mode, MergeMode):
            raise TaskSettingsError("merge_mode must be a MergeMode")
        normalized_confirmed_at = _normalize_datetime(confirmed_at, "confirmed_at")
        if auto_merge_expires_at is _AUTO_EXPIRY_UNSET:
            expires_at: datetime | None = (
                None
                if merge_mode is MergeMode.MANUAL
                else normalized_confirmed_at + MAX_AUTO_MERGE_DURATION
            )
        else:
            expires_at = auto_merge_expires_at  # type: ignore[assignment]
        return cls(
            format_version=TASK_SETTINGS_FORMAT,
            request_id=request_id,
            repository=repository,
            issue_number=None,
            mode=Mode.TASK,
            task_content_hash=task_content_hash(task_content),
            task_flow=task_flow,
            merge_mode=merge_mode,
            confirmed_by=confirmed_by,
            confirmed_at=normalized_confirmed_at,
            auto_merge_expires_at=expires_at,
            status=TaskSettingsStatus.PREPARED,
        )


def _task_settings_payload(settings: TaskSettings) -> dict[str, object]:
    if settings.issue_number is None:
        raise TaskSettingsError("issue must be bound before task_settings_hash")
    return {
        "format_version": settings.format_version,
        "request_id": settings.request_id,
        "repository": settings.repository,
        "issue_number": settings.issue_number,
        "mode": settings.mode.value,
        "task_content_hash": settings.task_content_hash,
        "task_flow": settings.task_flow.value,
        "merge_mode": settings.merge_mode.value,
        "confirmed_by": settings.confirmed_by,
        "confirmed_at": _format_timestamp(settings.confirmed_at),
        "auto_merge_expires_at": (
            None
            if settings.auto_merge_expires_at is None
            else _format_timestamp(settings.auto_merge_expires_at)
        ),
    }


def task_settings_hash(settings: TaskSettings) -> str:
    """Hash immutable Task settings while excluding the hash and lifecycle status."""

    if not isinstance(settings, TaskSettings):
        raise TaskSettingsError("settings must be a TaskSettings")
    return _canonical_sha256(_task_settings_payload(settings))


@dataclass(frozen=True, slots=True)
class TaskSettingsEvent:
    event_type: TaskSettingsEventType
    occurred_at: datetime
    issue_number: int | None = None
    task_settings_hash: str | None = None


class TaskSettingsStore:
    """Persist immutable settings plus append-only lifecycle events."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = _prepare_database_path(database_path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self.database_path, timeout=5)
        except sqlite3.Error as error:
            raise TaskSettingsError(
                "database path could not be opened safely"
            ) from error
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with (
            _normalize_database_errors(),
            closing(self._connect()) as connection,
            connection,
        ):
            # RISK(breaking/data-loss): schema v1 is a clean break. Any other
            # version or object shape must stop before settings are read or written.
            _begin_write(connection)
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            schema_objects = _load_schema_objects(connection)
            if not schema_objects:
                if version != 0:
                    raise TaskSettingsError(
                        "Task settings schema version "
                        f"{version} is not supported; expected "
                        f"{TASK_SETTINGS_SCHEMA_VERSION}"
                    )
                connection.execute(_TASK_SETTINGS_TABLE_SQL)
                connection.execute(_TASK_SETTINGS_EVENTS_TABLE_SQL)
                connection.execute(_TERMINAL_EVENT_INDEX_SQL)
                connection.execute(
                    f"PRAGMA user_version = {TASK_SETTINGS_SCHEMA_VERSION}"
                )
            elif version != TASK_SETTINGS_SCHEMA_VERSION:
                raise TaskSettingsError(
                    "Task settings schema version "
                    f"{version} is not supported; expected "
                    f"{TASK_SETTINGS_SCHEMA_VERSION}"
                )
            _validate_schema(connection)

    def prepare(self, settings: TaskSettings) -> TaskSettings:
        if not isinstance(settings, TaskSettings):
            raise TaskSettingsError("settings must be a TaskSettings")
        if (
            settings.status is not TaskSettingsStatus.PREPARED
            or settings.issue_number is not None
        ):
            raise TaskSettingsError("prepare requires unbound prepared settings")

        with (
            _normalize_database_errors(),
            closing(self._connect()) as connection,
            connection,
        ):
            _begin_write(connection)
            cursor = connection.execute(
                """
                INSERT INTO task_settings (
                    request_id,
                    format_version,
                    repository,
                    mode,
                    task_content_hash,
                    task_flow,
                    merge_mode,
                    confirmed_by,
                    confirmed_at,
                    auto_merge_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO NOTHING
                """,
                _base_row_values(settings),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    """
                    INSERT INTO task_settings_events (
                        request_id, event_type, occurred_at
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        settings.request_id,
                        TaskSettingsEventType.PREPARED.value,
                        _format_timestamp(settings.confirmed_at),
                    ),
                )
            current = self._load_settings(connection, settings.request_id)
            if _base_identity(current) != _base_identity(settings):
                raise TaskSettingsError(
                    "request_id already exists with different settings"
                )
            return current

    def bind_issue(
        self,
        request_id: str,
        issue_number: int,
        *,
        occurred_at: datetime | None = None,
    ) -> TaskSettings:
        if type(issue_number) is not int or issue_number <= 0:
            raise TaskSettingsError("issue_number must be a positive integer")
        event_time = _event_time(occurred_at)
        with (
            _normalize_database_errors(),
            closing(self._connect()) as connection,
            connection,
        ):
            _begin_write(connection)
            current = self._load_settings(connection, request_id)
            if current.issue_number is not None:
                if current.issue_number != issue_number:
                    raise TaskSettingsError("bound issue is immutable")
                return current
            if current.status is not TaskSettingsStatus.PREPARED:
                raise TaskSettingsError("Task settings are immutable after activation")

            bound = replace(current, issue_number=issue_number)
            connection.execute(
                """
                INSERT INTO task_settings_events (
                    request_id,
                    event_type,
                    occurred_at,
                    issue_number,
                    task_settings_hash
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    TaskSettingsEventType.ISSUE_BOUND.value,
                    _format_timestamp(event_time),
                    issue_number,
                    bound.task_settings_hash,
                ),
            )
            return self._load_settings(connection, request_id)

    def activate(
        self,
        request_id: str,
        *,
        occurred_at: datetime | None = None,
    ) -> TaskSettings:
        event_time = _event_time(occurred_at)
        with (
            _normalize_database_errors(),
            closing(self._connect()) as connection,
            connection,
        ):
            _begin_write(connection)
            current = self._load_settings(connection, request_id)
            if current.status is TaskSettingsStatus.ACTIVE:
                return current
            if current.status in _TERMINAL_STATUSES:
                raise TaskSettingsError(
                    "Task settings are immutable after lifecycle end"
                )
            if current.issue_number is None:
                raise TaskSettingsError("issue must be bound before activation")

            connection.execute(
                """
                INSERT INTO task_settings_events (
                    request_id, event_type, occurred_at
                ) VALUES (?, ?, ?)
                """,
                (
                    request_id,
                    TaskSettingsEventType.ACTIVE.value,
                    _format_timestamp(event_time),
                ),
            )
            return self._load_settings(connection, request_id)

    def append_lifecycle_event(
        self,
        request_id: str,
        status: TaskSettingsStatus,
        *,
        occurred_at: datetime | None = None,
    ) -> TaskSettings:
        if (
            not isinstance(status, TaskSettingsStatus)
            or status not in _TERMINAL_STATUSES
        ):
            raise TaskSettingsError(
                "lifecycle status must be cancelled, expired, or merged"
            )
        event_time = _event_time(occurred_at)
        with (
            _normalize_database_errors(),
            closing(self._connect()) as connection,
            connection,
        ):
            _begin_write(connection)
            current = self._load_settings(connection, request_id)
            if current.status is status:
                return current
            if current.status in _TERMINAL_STATUSES:
                raise TaskSettingsError("lifecycle status is immutable")
            if current.status is not TaskSettingsStatus.ACTIVE:
                raise TaskSettingsError(
                    "Task settings must be active before a lifecycle end event"
                )
            connection.execute(
                """
                INSERT INTO task_settings_events (
                    request_id, event_type, occurred_at
                ) VALUES (?, ?, ?)
                """,
                (
                    request_id,
                    _STATUS_TO_EVENT[status].value,
                    _format_timestamp(event_time),
                ),
            )
            return self._load_settings(connection, request_id)

    def get_active(self, request_id: str) -> TaskSettings | None:
        with (
            _normalize_database_errors(),
            closing(self._connect()) as connection,
            connection,
        ):
            exists = connection.execute(
                "SELECT 1 FROM task_settings WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if exists is None:
                return None
            settings = self._load_settings(connection, request_id)
        return settings if settings.status is TaskSettingsStatus.ACTIVE else None

    def list_events(self, request_id: str) -> tuple[TaskSettingsEvent, ...]:
        with (
            _normalize_database_errors(),
            closing(self._connect()) as connection,
            connection,
        ):
            self._load_settings(connection, request_id)
            return self._load_events(connection, request_id)

    def replace(self, request_id: str, **changes: object) -> Never:
        del request_id, changes
        raise TaskSettingsError(
            "Task settings are immutable; create a new request_id instead"
        )

    def _load_settings(
        self,
        connection: sqlite3.Connection,
        request_id: str,
    ) -> TaskSettings:
        row = connection.execute(
            "SELECT * FROM task_settings WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            raise TaskSettingsError("Task settings not found")

        events = self._load_events(connection, request_id)
        if not events or events[0].event_type is not TaskSettingsEventType.PREPARED:
            raise TaskSettingsError("stored Task settings have no prepared event")
        issue_number: int | None = None
        stored_hash: str | None = None
        status = TaskSettingsStatus.PREPARED
        for event in events:
            if event.event_type is TaskSettingsEventType.ISSUE_BOUND:
                issue_number = event.issue_number
                stored_hash = event.task_settings_hash
            elif event.event_type in _EVENT_TO_STATUS:
                status = _EVENT_TO_STATUS[event.event_type]

        try:
            settings = TaskSettings(
                format_version=row["format_version"],
                request_id=row["request_id"],
                repository=row["repository"],
                issue_number=issue_number,
                mode=Mode(row["mode"]),
                task_content_hash=row["task_content_hash"],
                task_flow=TaskFlow(row["task_flow"]),
                merge_mode=MergeMode(row["merge_mode"]),
                confirmed_by=row["confirmed_by"],
                confirmed_at=_parse_timestamp(row["confirmed_at"], "confirmed_at"),
                auto_merge_expires_at=(
                    None
                    if row["auto_merge_expires_at"] is None
                    else _parse_timestamp(
                        row["auto_merge_expires_at"],
                        "auto_merge_expires_at",
                    )
                ),
                status=status,
            )
        except TaskSettingsError:
            raise
        except ValueError as error:
            raise TaskSettingsError(
                "stored Task settings use an unknown value"
            ) from error
        if stored_hash is not None and settings.task_settings_hash != stored_hash:
            raise TaskSettingsError("stored task_settings_hash does not match settings")
        return settings

    def _load_events(
        self,
        connection: sqlite3.Connection,
        request_id: str,
    ) -> tuple[TaskSettingsEvent, ...]:
        rows = connection.execute(
            """
            SELECT event_type, occurred_at, issue_number, task_settings_hash
            FROM task_settings_events
            WHERE request_id = ?
            ORDER BY event_id
            """,
            (request_id,),
        ).fetchall()
        events: list[TaskSettingsEvent] = []
        for row in rows:
            try:
                event_type = TaskSettingsEventType(row["event_type"])
            except ValueError as error:
                raise TaskSettingsError(
                    "stored Task settings event uses an unknown value"
                ) from error
            events.append(
                TaskSettingsEvent(
                    event_type=event_type,
                    occurred_at=_parse_timestamp(row["occurred_at"], "occurred_at"),
                    issue_number=row["issue_number"],
                    task_settings_hash=row["task_settings_hash"],
                )
            )
        return tuple(events)


def _base_row_values(settings: TaskSettings) -> tuple[object, ...]:
    return (
        settings.request_id,
        settings.format_version,
        settings.repository,
        settings.mode.value,
        settings.task_content_hash,
        settings.task_flow.value,
        settings.merge_mode.value,
        settings.confirmed_by,
        _format_timestamp(settings.confirmed_at),
        (
            None
            if settings.auto_merge_expires_at is None
            else _format_timestamp(settings.auto_merge_expires_at)
        ),
    )


def _base_identity(settings: TaskSettings) -> tuple[object, ...]:
    return (
        settings.format_version,
        settings.request_id,
        settings.repository,
        settings.mode,
        settings.task_content_hash,
        settings.task_flow,
        settings.merge_mode,
        settings.confirmed_by,
        settings.confirmed_at,
        settings.auto_merge_expires_at,
    )


def _event_time(value: datetime | None) -> datetime:
    return (
        datetime.now(UTC)
        if value is None
        else _normalize_datetime(value, "occurred_at")
    )


def _prepare_database_path(database_path: str | Path) -> Path:
    try:
        candidate = Path(database_path).expanduser()
        if candidate.exists() and candidate.is_symlink():
            raise TaskSettingsError("database_path must not be a symbolic link")
        resolved = candidate.resolve(strict=False)
    except TaskSettingsError:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise TaskSettingsError(
            "database_path must be a valid filesystem path"
        ) from error

    parent = resolved.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise TaskSettingsError(
            "database parent directory could not be created safely"
        ) from error
    if not parent.is_dir():
        raise TaskSettingsError("database parent directory must be a directory")
    if resolved.exists() and (resolved.is_symlink() or not resolved.is_file()):
        raise TaskSettingsError("database_path must be a regular file")
    return resolved


def _begin_write(connection: sqlite3.Connection) -> None:
    # RISK(race): the lock must be acquired before reading lifecycle state, or two
    # writers can both validate the same old state and append conflicting events.
    connection.execute("BEGIN IMMEDIATE")


@contextmanager
def _normalize_database_errors() -> Iterator[None]:
    try:
        yield
    except TaskSettingsError:
        raise
    except sqlite3.Error as error:
        raise TaskSettingsError("Task settings database operation failed") from error


def _normalize_schema_sql(value: str) -> str:
    return " ".join(value.strip().rstrip(";").split()).casefold()


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
        (row["type"], row["name"]): row["sql"] for row in rows if row["sql"] is not None
    }


def _validate_schema(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version != TASK_SETTINGS_SCHEMA_VERSION:
        raise TaskSettingsError(
            "Task settings schema version "
            f"{version} is not supported; expected {TASK_SETTINGS_SCHEMA_VERSION}"
        )

    actual = {
        key: _normalize_schema_sql(value)
        for key, value in _load_schema_objects(connection).items()
    }
    expected = {
        key: _normalize_schema_sql(value)
        for key, value in _EXPECTED_SCHEMA_OBJECTS.items()
    }
    if actual != expected:
        raise TaskSettingsError(
            "Task settings database schema does not match the clean-break format"
        )
