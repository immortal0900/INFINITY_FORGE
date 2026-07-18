"""Immutable, task-scoped settings and their append-only SQLite history."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Never
from uuid import UUID

from forge.ops.task_database import (
    TaskDatabase,
    TaskDatabaseError,
)
from forge.ops.task_options import MergeMode, Mode, TaskFlow


TASK_SETTINGS_FORMAT = "forge-task-settings/v1"
MAX_AUTO_MERGE_DURATION = timedelta(hours=12)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_PATTERN = re.compile(r"^[^/\s]+/[^/\s]+$")
_PULL_REQUEST_URL_PATTERN = re.compile(
    r"^https://github\.com/(?P<repository>[^/\s]+/[^/\s]+)/pull/[1-9][0-9]*$"
)
_AUTO_EXPIRY_UNSET = object()

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


@dataclass(frozen=True, slots=True)
class BranchRefreshIntent:
    """One durable, replayable permission to refresh an exact PR state."""

    request_id: str
    refresh_number: int
    pr_url: str
    expected_base_commit: str
    expected_head_commit: str
    created_at: datetime
    current_base_commit: str | None
    current_head_commit: str | None
    completed_at: datetime | None

    def __post_init__(self) -> None:
        try:
            parsed_request_id = UUID(self.request_id)
        except (AttributeError, ValueError) as error:
            raise TaskSettingsError(
                "branch refresh request_id must be a canonical UUID string"
            ) from error
        if str(parsed_request_id) != self.request_id:
            raise TaskSettingsError(
                "branch refresh request_id must be a canonical UUID string"
            )
        if type(self.refresh_number) is not int or not 1 <= self.refresh_number <= 3:
            raise TaskSettingsError("branch refresh number must be between 1 and 3")
        if not isinstance(self.pr_url, str) or not _PULL_REQUEST_URL_PATTERN.fullmatch(
            self.pr_url
        ):
            raise TaskSettingsError("branch refresh pull request URL is invalid")
        for field_name, value in (
            ("expected_base_commit", self.expected_base_commit),
            ("expected_head_commit", self.expected_head_commit),
        ):
            if not isinstance(value, str) or not _COMMIT_PATTERN.fullmatch(value):
                raise TaskSettingsError(f"branch refresh {field_name} is invalid")
        object.__setattr__(
            self,
            "created_at",
            _normalize_datetime(self.created_at, "created_at"),
        )
        result_values = (
            self.current_base_commit,
            self.current_head_commit,
            self.completed_at,
        )
        if all(value is None for value in result_values):
            return
        if any(value is None for value in result_values):
            raise TaskSettingsError("branch refresh result must be complete")
        assert self.current_base_commit is not None
        assert self.current_head_commit is not None
        assert self.completed_at is not None
        if not _COMMIT_PATTERN.fullmatch(self.current_base_commit):
            raise TaskSettingsError("branch refresh current_base_commit is invalid")
        if not _COMMIT_PATTERN.fullmatch(self.current_head_commit):
            raise TaskSettingsError("branch refresh current_head_commit is invalid")
        if (
            self.current_base_commit == self.expected_base_commit
            and self.current_head_commit == self.expected_head_commit
        ):
            raise TaskSettingsError("branch refresh result did not change the pull request")
        object.__setattr__(
            self,
            "completed_at",
            _normalize_datetime(self.completed_at, "completed_at"),
        )

    @property
    def completed(self) -> bool:
        return self.completed_at is not None


class ActiveTaskGuard:
    """Hold one Task active while an external write is completed."""

    def __init__(
        self,
        store: TaskSettingsStore,
        connection: sqlite3.Connection,
        settings: TaskSettings,
    ) -> None:
        self._store = store
        self._connection = connection
        self.settings = settings

    def finish(
        self,
        status: TaskSettingsStatus,
        *,
        occurred_at: datetime | None = None,
    ) -> TaskSettings:
        """End the guarded Task in the same transaction as the active check."""

        if (
            not isinstance(status, TaskSettingsStatus)
            or status not in _TERMINAL_STATUSES
        ):
            raise TaskSettingsError(
                "lifecycle status must be cancelled, expired, or merged"
            )
        current = self._store._load_settings(
            self._connection,
            self.settings.request_id,
        )
        if current.status is status:
            self.settings = current
            return current
        if current.status in _TERMINAL_STATUSES:
            raise TaskSettingsError("lifecycle status is immutable")
        if current != self.settings or current.status is not TaskSettingsStatus.ACTIVE:
            raise TaskSettingsError("guarded Task settings changed")
        self._connection.execute(
            """
            INSERT INTO task_settings_events (
                request_id, event_type, occurred_at
            ) VALUES (?, ?, ?)
            """,
            (
                current.request_id,
                _STATUS_TO_EVENT[status].value,
                _format_timestamp(_event_time(occurred_at)),
            ),
        )
        self.settings = self._store._load_settings(
            self._connection,
            current.request_id,
        )
        return self.settings

    def complete_branch_refresh(
        self,
        intent: BranchRefreshIntent,
        *,
        current_base_commit: str,
        current_head_commit: str,
        occurred_at: datetime | None = None,
    ) -> BranchRefreshIntent:
        """Persist refresh readback without opening a second DB connection."""

        if intent.request_id != self.settings.request_id:
            raise TaskSettingsError("branch refresh intent does not match guarded Task")
        return self._store._complete_branch_refresh_on_connection(
            self._connection,
            intent,
            current_base_commit=current_base_commit,
            current_head_commit=current_head_commit,
            occurred_at=occurred_at,
        )


class TaskSettingsStore:
    """Persist immutable settings plus append-only lifecycle events."""

    def __init__(self, database_path: str | Path) -> None:
        try:
            self._database = TaskDatabase(database_path)
        except TaskDatabaseError as error:
            raise TaskSettingsError(str(error)) from error
        self.database_path = self._database.database_path

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
            self._database.transaction() as connection,
        ):
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
            self._database.transaction() as connection,
        ):
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
            self._database.transaction() as connection,
        ):
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
            self._database.transaction() as connection,
        ):
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
            self._database.read() as connection,
        ):
            exists = connection.execute(
                "SELECT 1 FROM task_settings WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if exists is None:
                return None
            settings = self._load_settings(connection, request_id)
        return settings if settings.status is TaskSettingsStatus.ACTIVE else None

    @contextmanager
    def guard_active(self, expected: TaskSettings) -> Iterator[ActiveTaskGuard]:
        """Serialize an external write against cancel, expiry, and merge."""

        if not isinstance(expected, TaskSettings):
            raise TaskSettingsError("expected settings must be TaskSettings")
        if expected.status is not TaskSettingsStatus.ACTIVE:
            raise TaskSettingsError("expected Task settings must be active")
        with (
            _normalize_database_errors(),
            self._database.transaction() as connection,
        ):
            current = self._load_settings(connection, expected.request_id)
            if current.status is not TaskSettingsStatus.ACTIVE:
                raise TaskSettingsError("Task settings are no longer active")
            if current != expected:
                raise TaskSettingsError("Task settings changed before external write")
            yield ActiveTaskGuard(self, connection, current)

    def list_events(self, request_id: str) -> tuple[TaskSettingsEvent, ...]:
        with (
            _normalize_database_errors(),
            self._database.read() as connection,
        ):
            self._load_settings(connection, request_id)
            return self._load_events(connection, request_id)

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
        """Spend the next refresh number before any GitHub write."""

        if type(applied_refresh_count) is not int or applied_refresh_count < 0:
            raise TaskSettingsError("applied branch refresh count is invalid")
        event_time = _event_time(occurred_at)
        with (
            _normalize_database_errors(),
            self._database.transaction() as connection,
        ):
            settings = self._load_settings(connection, request_id)
            _require_active_refresh_settings(settings, pr_url)
            intents = self._load_branch_refresh_intents(connection, request_id)
            replay = _branch_refresh_replay(intents, applied_refresh_count)
            if replay is not None:
                if _branch_refresh_expected_identity(replay) != (
                    request_id,
                    pr_url,
                    expected_base_commit,
                    expected_head_commit,
                ):
                    raise TaskSettingsError(
                        "reserved branch refresh has a different pull request state"
                    )
                return replay
            if applied_refresh_count >= 3:
                raise TaskSettingsError("branch refresh limit was reached")

            intent = BranchRefreshIntent(
                request_id=request_id,
                refresh_number=applied_refresh_count + 1,
                pr_url=pr_url,
                expected_base_commit=expected_base_commit,
                expected_head_commit=expected_head_commit,
                created_at=event_time,
                current_base_commit=None,
                current_head_commit=None,
                completed_at=None,
            )
            connection.execute(
                """
                INSERT INTO task_branch_refresh_intents (
                    request_id,
                    refresh_number,
                    pr_url,
                    expected_base_commit,
                    expected_head_commit,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    intent.request_id,
                    intent.refresh_number,
                    intent.pr_url,
                    intent.expected_base_commit,
                    intent.expected_head_commit,
                    _format_timestamp(intent.created_at),
                ),
            )
            return self._load_branch_refresh_intents(connection, request_id)[-1]

    def get_branch_refresh_replay(
        self,
        request_id: str,
        *,
        applied_refresh_count: int,
    ) -> BranchRefreshIntent | None:
        """Return the one intent not yet represented in the Hermes proof chain."""

        if type(applied_refresh_count) is not int or applied_refresh_count < 0:
            raise TaskSettingsError("applied branch refresh count is invalid")
        with (
            _normalize_database_errors(),
            self._database.read() as connection,
        ):
            settings = self._load_settings(connection, request_id)
            if settings.status is not TaskSettingsStatus.ACTIVE:
                raise TaskSettingsError("Task settings are no longer active")
            intents = self._load_branch_refresh_intents(connection, request_id)
            return _branch_refresh_replay(intents, applied_refresh_count)

    def complete_branch_refresh(
        self,
        intent: BranchRefreshIntent,
        *,
        current_base_commit: str,
        current_head_commit: str,
        occurred_at: datetime | None = None,
    ) -> BranchRefreshIntent:
        """Persist the exact readback for one previously reserved refresh."""

        with (
            _normalize_database_errors(),
            self._database.transaction() as connection,
        ):
            return self._complete_branch_refresh_on_connection(
                connection,
                intent,
                current_base_commit=current_base_commit,
                current_head_commit=current_head_commit,
                occurred_at=occurred_at,
            )

    def _complete_branch_refresh_on_connection(
        self,
        connection: sqlite3.Connection,
        intent: BranchRefreshIntent,
        *,
        current_base_commit: str,
        current_head_commit: str,
        occurred_at: datetime | None,
    ) -> BranchRefreshIntent:
        if not isinstance(intent, BranchRefreshIntent):
            raise TaskSettingsError("intent must be a BranchRefreshIntent")
        event_time = _event_time(occurred_at)
        candidate = replace(
            intent,
            current_base_commit=current_base_commit,
            current_head_commit=current_head_commit,
            completed_at=event_time,
        )
        settings = self._load_settings(connection, intent.request_id)
        _require_active_refresh_settings(settings, intent.pr_url)
        intents = self._load_branch_refresh_intents(
            connection,
            intent.request_id,
        )
        if intent.refresh_number > len(intents):
            raise TaskSettingsError("branch refresh intent was not reserved")
        stored = intents[intent.refresh_number - 1]
        if _branch_refresh_expected_identity(stored) != (
            intent.request_id,
            intent.pr_url,
            intent.expected_base_commit,
            intent.expected_head_commit,
        ):
            raise TaskSettingsError("stored branch refresh intent does not match")
        if stored.completed:
            if (
                stored.current_base_commit != current_base_commit
                or stored.current_head_commit != current_head_commit
            ):
                raise TaskSettingsError(
                    "completed branch refresh has a different result"
                )
            return stored
        connection.execute(
            """
            UPDATE task_branch_refresh_intents
            SET current_base_commit = ?,
                current_head_commit = ?,
                completed_at = ?
            WHERE request_id = ?
              AND refresh_number = ?
              AND completed_at IS NULL
            """,
            (
                candidate.current_base_commit,
                candidate.current_head_commit,
                _format_timestamp(event_time),
                candidate.request_id,
                candidate.refresh_number,
            ),
        )
        return self._load_branch_refresh_intents(
            connection,
            intent.request_id,
        )[intent.refresh_number - 1]

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

    def _load_branch_refresh_intents(
        self,
        connection: sqlite3.Connection,
        request_id: str,
    ) -> tuple[BranchRefreshIntent, ...]:
        rows = connection.execute(
            """
            SELECT
                request_id,
                refresh_number,
                pr_url,
                expected_base_commit,
                expected_head_commit,
                created_at,
                current_base_commit,
                current_head_commit,
                completed_at
            FROM task_branch_refresh_intents
            WHERE request_id = ?
            ORDER BY refresh_number
            """,
            (request_id,),
        ).fetchall()
        intents = tuple(
            BranchRefreshIntent(
                request_id=row["request_id"],
                refresh_number=row["refresh_number"],
                pr_url=row["pr_url"],
                expected_base_commit=row["expected_base_commit"],
                expected_head_commit=row["expected_head_commit"],
                created_at=_parse_timestamp(row["created_at"], "created_at"),
                current_base_commit=row["current_base_commit"],
                current_head_commit=row["current_head_commit"],
                completed_at=(
                    None
                    if row["completed_at"] is None
                    else _parse_timestamp(row["completed_at"], "completed_at")
                ),
            )
            for row in rows
        )
        if tuple(intent.refresh_number for intent in intents) != tuple(
            range(1, len(intents) + 1)
        ):
            raise TaskSettingsError("stored branch refresh sequence is not contiguous")
        return intents


def _require_active_refresh_settings(
    settings: TaskSettings,
    pr_url: str,
) -> None:
    if settings.status is not TaskSettingsStatus.ACTIVE:
        raise TaskSettingsError("Task settings are no longer active")
    match = (
        _PULL_REQUEST_URL_PATTERN.fullmatch(pr_url)
        if isinstance(pr_url, str)
        else None
    )
    if match is None or match.group("repository") != settings.repository:
        raise TaskSettingsError(
            "branch refresh pull request does not match Task settings"
        )


def _branch_refresh_expected_identity(
    intent: BranchRefreshIntent,
) -> tuple[str, str, str, str]:
    return (
        intent.request_id,
        intent.pr_url,
        intent.expected_base_commit,
        intent.expected_head_commit,
    )


def _branch_refresh_replay(
    intents: tuple[BranchRefreshIntent, ...],
    applied_refresh_count: int,
) -> BranchRefreshIntent | None:
    if applied_refresh_count > len(intents):
        raise TaskSettingsError(
            "Hermes branch refresh count has no durable proof"
        )
    if len(intents) > applied_refresh_count + 1:
        raise TaskSettingsError(
            "durable branch refresh count is more than one step ahead"
        )
    if any(not intent.completed for intent in intents[:applied_refresh_count]):
        raise TaskSettingsError(
            "Hermes branch refresh count has no completed durable proof"
        )
    if applied_refresh_count == len(intents):
        return None
    return intents[applied_refresh_count]


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


@contextmanager
def _normalize_database_errors() -> Iterator[None]:
    try:
        yield
    except TaskSettingsError:
        raise
    except TaskDatabaseError as error:
        raise TaskSettingsError(str(error)) from error
    except sqlite3.Error as error:
        raise TaskSettingsError("Task settings database operation failed") from error
