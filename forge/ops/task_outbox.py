"""Durably retain an exact confirmed Task until GitHub delivery succeeds."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from .task_options import MergeMode, TaskFlow
from .task_settings import TaskContent, TaskSettings, TaskSettingsError

if TYPE_CHECKING:
    from .task_service import TaskCreationRequest


TASK_OUTBOX_FORMAT = "forge-task-outbox/v1"
TASK_OUTBOX_SCHEMA_VERSION = 1
_REQUEST_LOCK_TIMEOUT_SECONDS = 30.0
_REQUEST_LOCK_RETRY_SECONDS = 0.01

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_FIELDS = {
    "format_version",
    "request_id",
    "repository",
    "content",
    "task_flow",
    "merge_mode",
    "confirmed_by",
    "confirmed_at",
}
_CONTENT_FIELDS = {"title", "description", "acceptance_criteria"}

# RISK(breaking): this exact v1 schema rejects drift; changing it requires an
# explicit migration instead of silently accepting older or extra columns.
_TASK_OUTBOX_TABLE_SQL = """
CREATE TABLE task_outbox (
    request_id TEXT PRIMARY KEY,
    format_version TEXT NOT NULL
        CHECK (format_version = 'forge-task-outbox/v1'),
    request_json TEXT NOT NULL,
    request_hash TEXT NOT NULL
        CHECK (
            length(request_hash) = 64
            AND request_hash NOT GLOB '*[^0-9a-f]*'
        ),
    state TEXT NOT NULL CHECK (state IN ('pending', 'completed', 'ended')),
    issue_number INTEGER,
    terminal_status TEXT CHECK (
        terminal_status IN ('cancelled', 'expired', 'merged')
    ),
    CHECK (
        (
            state = 'pending'
            AND issue_number IS NULL
            AND terminal_status IS NULL
        )
        OR
        (
            state = 'completed'
            AND issue_number IS NOT NULL
            AND issue_number > 0
            AND terminal_status IS NULL
        )
        OR
        (
            state = 'ended'
            AND issue_number IS NOT NULL
            AND issue_number > 0
            AND terminal_status IS NOT NULL
        )
    )
)
"""


class TaskOutboxError(RuntimeError):
    """Raised when a confirmed Task cannot be durably stored or replayed."""


@dataclass(frozen=True, slots=True)
class _OutboxEntry:
    request: TaskCreationRequest
    state: str
    issue_number: int | None
    terminal_status: str | None


class TaskOutboxClaim:
    """One SQLite-serialized delivery claim for a stored Task request."""

    def __init__(
        self,
        outbox: TaskOutbox,
        entry: _OutboxEntry,
    ) -> None:
        self._outbox = outbox
        self.request = entry.request
        self.already_completed = entry.state == "completed"
        self.already_ended = entry.state == "ended"
        self.issue_number = entry.issue_number
        self.terminal_status = entry.terminal_status

    def complete(self, issue_number: int) -> None:
        """Mark this delivery complete only after Task creation fully succeeds."""

        if type(issue_number) is not int or issue_number <= 0:
            raise TaskOutboxError("issue_number must be a positive integer")
        if self.already_ended:
            raise TaskOutboxError("ended Task cannot be completed")
        if self.already_completed:
            if self.issue_number != issue_number:
                raise TaskOutboxError("completed Task issue_number is immutable")
            return
        self._outbox._finish(
            self.request.request_id,
            state="completed",
            issue_number=issue_number,
            terminal_status=None,
        )
        self.already_completed = True
        self.issue_number = issue_number

    def finish_terminal(self, issue_number: int, terminal_status: str) -> None:
        """Retire a pending delivery whose Task lifecycle already ended."""

        if type(issue_number) is not int or issue_number <= 0:
            raise TaskOutboxError("issue_number must be a positive integer")
        if terminal_status not in {"cancelled", "expired", "merged"}:
            raise TaskOutboxError("terminal_status is invalid")
        if self.already_completed:
            raise TaskOutboxError("completed Task cannot be marked ended")
        if self.already_ended:
            if (
                self.issue_number != issue_number
                or self.terminal_status != terminal_status
            ):
                raise TaskOutboxError("ended Task result is immutable")
            return
        self._outbox._finish(
            self.request.request_id,
            state="ended",
            issue_number=issue_number,
            terminal_status=terminal_status,
        )
        self.already_ended = True
        self.issue_number = issue_number
        self.terminal_status = terminal_status


class TaskOutbox:
    """SQLite outbox for exact confirmed Task requests."""

    def __init__(self, database_path: str | Path) -> None:
        self._initialized = False
        self.database_path = _prepare_database_path(database_path)
        self._lock_directory = _prepare_lock_directory(self.database_path)
        self._initialize()
        self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        _assert_safe_database_path(
            self.database_path,
            require_exists=self._initialized,
        )
        open_mode = "rw" if self._initialized else "rwc"
        try:
            connection = sqlite3.connect(
                f"{self.database_path.as_uri()}?mode={open_mode}",
                timeout=5,
                uri=True,
            )
        except sqlite3.Error as error:
            raise TaskOutboxError(
                "Task outbox database path could not be opened safely"
            ) from error
        try:
            _assert_safe_database_path(
                self.database_path,
                require_exists=True,
            )
        except TaskOutboxError:
            connection.close()
            raise
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with (
            _normalize_database_errors(),
            closing(self._connect()) as connection,
        ):
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            objects = _load_schema_objects(connection)
            if objects:
                if version != TASK_OUTBOX_SCHEMA_VERSION:
                    raise TaskOutboxError(
                        "Task outbox schema version "
                        f"{version} is not supported; expected "
                        f"{TASK_OUTBOX_SCHEMA_VERSION}"
                    )
                _validate_schema(connection)
                return
            if version != 0:
                raise TaskOutboxError(
                    "Task outbox schema version "
                    f"{version} is not supported; expected "
                    f"{TASK_OUTBOX_SCHEMA_VERSION}"
                )

            # Only a brand-new database needs a write lock. Existing outboxes
            # remain readable while another process holds a delivery claim.
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                objects = _load_schema_objects(connection)
                if not objects:
                    if version != 0:
                        raise TaskOutboxError(
                            "Task outbox schema version "
                            f"{version} is not supported; expected "
                            f"{TASK_OUTBOX_SCHEMA_VERSION}"
                        )
                    connection.execute(_TASK_OUTBOX_TABLE_SQL)
                    connection.execute(
                        f"PRAGMA user_version = {TASK_OUTBOX_SCHEMA_VERSION}"
                    )
                elif version != TASK_OUTBOX_SCHEMA_VERSION:
                    raise TaskOutboxError(
                        "Task outbox schema version "
                        f"{version} is not supported; expected "
                        f"{TASK_OUTBOX_SCHEMA_VERSION}"
                    )
                _validate_schema(connection)

    def save(self, request: TaskCreationRequest) -> TaskCreationRequest:
        """Commit an exact request before any external GitHub write starts."""

        normalized, request_json, request_hash = _encode_request(request)
        with (
            _normalize_database_errors(),
            closing(self._connect()) as connection,
            connection,
        ):
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO task_outbox (
                    request_id,
                    format_version,
                    request_json,
                    request_hash,
                    state,
                    issue_number,
                    terminal_status
                ) VALUES (?, ?, ?, ?, 'pending', NULL, NULL)
                ON CONFLICT(request_id) DO NOTHING
                """,
                (
                    normalized.request_id,
                    TASK_OUTBOX_FORMAT,
                    request_json,
                    request_hash,
                ),
            )
            entry = _load_entry(connection, normalized.request_id)
            if (
                entry.request != normalized
                or _request_json(entry.request) != request_json
            ):
                raise TaskOutboxError(
                    "request_id already exists with a different confirmed Task"
                )
            return entry.request

    def load(self, request_id: str) -> TaskCreationRequest | None:
        """Load a stored request, including its completed tombstone."""

        with (
            _normalize_database_errors(),
            closing(self._connect()) as connection,
        ):
            row = connection.execute(
                "SELECT * FROM task_outbox WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            return None if row is None else _entry_from_row(row).request

    def load_pending(self, request_id: str) -> TaskCreationRequest | None:
        """Load a request only while it still needs delivery."""

        with (
            _normalize_database_errors(),
            closing(self._connect()) as connection,
        ):
            row = connection.execute(
                "SELECT * FROM task_outbox WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                return None
            entry = _entry_from_row(row)
            return entry.request if entry.state == "pending" else None

    def load_pending_for_user(
        self,
        repository: str,
        confirmed_by: str,
    ) -> TaskCreationRequest | None:
        """Find the one unfinished Task that a restarted Hermes user can retry."""

        if not isinstance(repository, str) or not repository.strip():
            raise TaskOutboxError("repository must be non-empty text")
        if not isinstance(confirmed_by, str) or not confirmed_by.strip():
            raise TaskOutboxError("confirmed_by must be non-empty text")
        with (
            _normalize_database_errors(),
            closing(self._connect()) as connection,
        ):
            rows = connection.execute(
                "SELECT * FROM task_outbox WHERE state = 'pending' ORDER BY rowid"
            ).fetchall()
            matches = [
                entry.request
                for entry in (_entry_from_row(row) for row in rows)
                if entry.request.repository == repository
                and entry.request.confirmed_by == confirmed_by
            ]
        if len(matches) > 1:
            raise TaskOutboxError(
                "more than one pending Task exists for this repository and user"
            )
        return matches[0] if matches else None

    @contextmanager
    def claim(self, request_id: str) -> Iterator[TaskOutboxClaim]:
        """Serialize one request; a crash releases its lock and keeps it pending."""

        # RISK(race): an OS request lock survives threads and processes while
        # releasing automatically on crash. SQLite writes stay short, so one
        # slow GitHub request cannot block saving a different confirmed Task.
        with _request_lock(self._lock_directory, request_id):
            with (
                _normalize_database_errors(),
                closing(self._connect()) as connection,
            ):
                entry = _load_entry(connection, request_id)
            claim = TaskOutboxClaim(self, entry)
            try:
                yield claim
            except BaseException:
                raise
            if not claim.already_completed and not claim.already_ended:
                raise TaskOutboxError(
                    "Task outbox claim ended without successful completion"
                )

    def _finish(
        self,
        request_id: str,
        *,
        state: str,
        issue_number: int,
        terminal_status: str | None,
    ) -> None:
        with (
            _normalize_database_errors(),
            closing(self._connect()) as connection,
            connection,
        ):
            connection.execute("BEGIN IMMEDIATE")
            current = _load_entry(connection, request_id)
            if current.state != "pending":
                if (
                    current.state == state
                    and current.issue_number == issue_number
                    and current.terminal_status == terminal_status
                ):
                    return
                raise TaskOutboxError("finished Task result is immutable")
            cursor = connection.execute(
                """
                UPDATE task_outbox
                SET state = ?, issue_number = ?, terminal_status = ?
                WHERE request_id = ? AND state = 'pending'
                """,
                (state, issue_number, terminal_status, request_id),
            )
            if cursor.rowcount != 1:
                raise TaskOutboxError(
                    "pending Task claim changed during completion"
                )


def task_outbox_path(settings_database_path: str | Path) -> Path:
    """Return a same-directory sidecar path for the settings database."""

    try:
        settings_path = Path(settings_database_path)
        if not settings_path.name:
            raise ValueError("settings database has no file name")
        return settings_path.with_name(
            f"{settings_path.name}.task-outbox.db"
        )
    except (TypeError, ValueError, OSError) as error:
        raise TaskOutboxError(
            "settings database path cannot produce a safe Task outbox sidecar"
        ) from error


def _request_payload(request: TaskCreationRequest) -> dict[str, object]:
    return {
        "format_version": TASK_OUTBOX_FORMAT,
        "request_id": request.request_id,
        "repository": request.repository,
        "content": {
            "title": request.content.title,
            "description": request.content.description,
            "acceptance_criteria": list(request.content.acceptance_criteria),
        },
        "task_flow": request.task_flow.value,
        "merge_mode": request.merge_mode.value,
        "confirmed_by": request.confirmed_by,
        "confirmed_at": _format_timestamp(request.confirmed_at),
    }


def _request_json(request: TaskCreationRequest) -> str:
    return json.dumps(
        _request_payload(request),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _encode_request(
    request: TaskCreationRequest,
) -> tuple[TaskCreationRequest, str, str]:
    from .task_service import TaskCreationRequest

    if not isinstance(request, TaskCreationRequest):
        raise TaskOutboxError("request must be a TaskCreationRequest")
    try:
        TaskSettings.create(
            request_id=request.request_id,
            repository=request.repository,
            task_content=request.content,
            task_flow=request.task_flow,
            merge_mode=request.merge_mode,
            confirmed_by=request.confirmed_by,
            confirmed_at=request.confirmed_at,
        )
        normalized = TaskCreationRequest(
            request_id=request.request_id,
            repository=request.repository,
            content=request.content,
            task_flow=request.task_flow,
            merge_mode=request.merge_mode,
            confirmed_by=request.confirmed_by,
            confirmed_at=request.confirmed_at.astimezone(UTC),
        )
    except (TaskSettingsError, AttributeError, ValueError) as error:
        raise TaskOutboxError("confirmed Task request is invalid") from error
    encoded = _request_json(normalized)
    return (
        normalized,
        encoded,
        hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    )


def _decode_request(request_json: object, request_hash: object) -> TaskCreationRequest:
    from .task_service import TaskCreationRequest

    if not isinstance(request_json, str):
        raise TaskOutboxError("stored Task request JSON must be text")
    if (
        not isinstance(request_hash, str)
        or _SHA256_PATTERN.fullmatch(request_hash) is None
    ):
        raise TaskOutboxError("stored Task request hash is invalid")
    actual_hash = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
    if actual_hash != request_hash:
        raise TaskOutboxError("stored Task request hash does not match JSON")
    try:
        payload = json.loads(request_json)
    except json.JSONDecodeError as error:
        raise TaskOutboxError("stored Task request JSON is invalid") from error
    if not isinstance(payload, dict) or set(payload) != _REQUEST_FIELDS:
        raise TaskOutboxError("stored Task request has invalid fields")
    if payload.get("format_version") != TASK_OUTBOX_FORMAT:
        raise TaskOutboxError("stored Task request format is unknown")
    content = payload.get("content")
    if not isinstance(content, dict) or set(content) != _CONTENT_FIELDS:
        raise TaskOutboxError("stored Task content has invalid fields")
    criteria = content.get("acceptance_criteria")
    if not isinstance(criteria, list) or any(
        not isinstance(item, str) for item in criteria
    ):
        raise TaskOutboxError("stored acceptance criteria are invalid")
    try:
        request = TaskCreationRequest(
            request_id=payload["request_id"],
            repository=payload["repository"],
            content=TaskContent(
                title=content["title"],
                description=content["description"],
                acceptance_criteria=tuple(criteria),
            ),
            task_flow=TaskFlow(payload["task_flow"]),
            merge_mode=MergeMode(payload["merge_mode"]),
            confirmed_by=payload["confirmed_by"],
            confirmed_at=_parse_timestamp(payload["confirmed_at"]),
        )
        normalized, canonical, _ = _encode_request(request)
    except (KeyError, TypeError, ValueError, TaskSettingsError) as error:
        raise TaskOutboxError("stored Task request values are invalid") from error
    if canonical != request_json:
        raise TaskOutboxError("stored Task request JSON is not canonical")
    return normalized


def _load_entry(connection: sqlite3.Connection, request_id: str) -> _OutboxEntry:
    row = connection.execute(
        "SELECT * FROM task_outbox WHERE request_id = ?",
        (request_id,),
    ).fetchone()
    if row is None:
        raise TaskOutboxError("Task outbox request was not found")
    return _entry_from_row(row)


def _entry_from_row(row: sqlite3.Row) -> _OutboxEntry:
    if row["format_version"] != TASK_OUTBOX_FORMAT:
        raise TaskOutboxError("stored Task outbox format is unknown")
    request = _decode_request(row["request_json"], row["request_hash"])
    if row["request_id"] != request.request_id:
        raise TaskOutboxError("stored Task request_id does not match JSON")
    state = row["state"]
    issue_number = row["issue_number"]
    terminal_status = row["terminal_status"]
    if state not in {"pending", "completed", "ended"}:
        raise TaskOutboxError("stored Task outbox state is invalid")
    if state == "pending" and (
        issue_number is not None or terminal_status is not None
    ):
        raise TaskOutboxError("pending Task has result data")
    if state == "completed" and (
        type(issue_number) is not int
        or issue_number <= 0
        or terminal_status is not None
    ):
        raise TaskOutboxError("completed Task has invalid completion data")
    if state == "ended" and (
        type(issue_number) is not int
        or issue_number <= 0
        or terminal_status not in {"cancelled", "expired", "merged"}
    ):
        raise TaskOutboxError("ended Task has invalid lifecycle data")
    return _OutboxEntry(
        request=request,
        state=state,
        issue_number=issue_number,
        terminal_status=terminal_status,
    )


def _format_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TaskOutboxError("confirmed_at must be timezone-aware")
    if value.utcoffset() is None:
        raise TaskOutboxError("confirmed_at must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise TaskOutboxError("stored confirmed_at must be RFC 3339 text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise TaskOutboxError("stored confirmed_at is not RFC 3339") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TaskOutboxError("stored confirmed_at must include a timezone")
    normalized = parsed.astimezone(UTC)
    if _format_timestamp(normalized) != value:
        raise TaskOutboxError("stored confirmed_at is not canonical RFC 3339")
    return normalized


def _prepare_database_path(database_path: str | Path) -> Path:
    try:
        candidate = Path(database_path).expanduser().absolute()
        _assert_no_symlink_components(candidate)
        resolved = candidate.resolve(strict=False)
    except TaskOutboxError:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise TaskOutboxError(
            "database_path must be a valid filesystem path"
        ) from error
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError) as error:
        raise TaskOutboxError(
            "database parent directory could not be created safely"
        ) from error
    if not resolved.parent.is_dir():
        raise TaskOutboxError("database parent directory must be a directory")
    _assert_safe_database_path(resolved, require_exists=False)
    return resolved


def _assert_no_symlink_components(path: Path) -> None:
    try:
        if any(component.is_symlink() for component in (path, *path.parents)):
            raise TaskOutboxError(
                "database_path and its parents must not be a symbolic link"
            )
    except TaskOutboxError:
        raise
    except (OSError, ValueError) as error:
        raise TaskOutboxError(
            "database_path could not be checked safely"
        ) from error


def _assert_safe_database_path(path: Path, *, require_exists: bool) -> None:
    _assert_no_symlink_components(path)
    try:
        if not path.parent.is_dir():
            raise TaskOutboxError(
                "database parent directory must be a directory"
            )
        if require_exists and not path.exists():
            raise TaskOutboxError("Task outbox database is missing")
        if path.exists() and not path.is_file():
            raise TaskOutboxError("database_path must be a regular file")
    except TaskOutboxError:
        raise
    except (OSError, ValueError) as error:
        raise TaskOutboxError(
            "database_path could not be checked safely"
        ) from error


def _prepare_lock_directory(database_path: Path) -> Path:
    directory = database_path.with_name(f"{database_path.name}.locks")
    _assert_no_symlink_components(directory)
    try:
        directory.mkdir(mode=0o700, exist_ok=True)
    except (OSError, ValueError) as error:
        raise TaskOutboxError(
            "Task outbox lock directory could not be created safely"
        ) from error
    _assert_safe_lock_directory(directory)
    return directory


def _assert_safe_lock_directory(directory: Path) -> None:
    _assert_no_symlink_components(directory)
    try:
        if not directory.is_dir():
            raise TaskOutboxError(
                "Task outbox lock path must be a directory"
            )
    except TaskOutboxError:
        raise
    except (OSError, ValueError) as error:
        raise TaskOutboxError(
            "Task outbox lock directory could not be checked safely"
        ) from error


@contextmanager
def _request_lock(directory: Path, request_id: str) -> Iterator[None]:
    try:
        parsed = UUID(request_id)
    except (AttributeError, TypeError, ValueError) as error:
        raise TaskOutboxError("request_id must be a canonical UUID") from error
    if str(parsed) != request_id:
        raise TaskOutboxError("request_id must be a canonical UUID")
    _assert_safe_lock_directory(directory)
    lock_path = directory / f"{request_id}.lock"
    if lock_path.is_symlink():
        raise TaskOutboxError("Task outbox lock file must not be a symbolic link")
    flags = os.O_CREAT | os.O_RDWR
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOINHERIT", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise TaskOutboxError(
            "Task outbox request lock could not be opened safely"
        ) from error
    try:
        try:
            descriptor_stat = os.fstat(descriptor)
            path_stat = os.lstat(lock_path)
            if not stat.S_ISREG(descriptor_stat.st_mode):
                raise TaskOutboxError(
                    "Task outbox request lock must be a regular file"
                )
            if stat.S_ISLNK(path_stat.st_mode) or (
                descriptor_stat.st_dev,
                descriptor_stat.st_ino,
            ) != (path_stat.st_dev, path_stat.st_ino):
                raise TaskOutboxError("Task outbox request lock path changed")
            if descriptor_stat.st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            _acquire_file_lock(descriptor)
            _assert_safe_lock_directory(directory)
        except TaskOutboxError:
            raise
        except OSError as error:
            raise TaskOutboxError("Task outbox request lock failed") from error

        try:
            yield
        finally:
            _release_file_lock(descriptor)
    finally:
        os.close(descriptor)


def _acquire_file_lock(descriptor: int) -> None:
    deadline = time.monotonic() + _REQUEST_LOCK_TIMEOUT_SECONDS
    last_error: OSError | None = None
    while True:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as error:
            last_error = error
            if time.monotonic() >= deadline:
                raise TaskOutboxError(
                    "Task outbox request lock timed out"
                ) from last_error
            time.sleep(_REQUEST_LOCK_RETRY_SECONDS)


def _release_file_lock(descriptor: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError as error:
        raise TaskOutboxError(
            "Task outbox request lock could not be released"
        ) from error


@contextmanager
def _normalize_database_errors() -> Iterator[None]:
    try:
        yield
    except TaskOutboxError:
        raise
    except sqlite3.Error as error:
        raise TaskOutboxError("Task outbox database operation failed") from error


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
        (row["type"], row["name"]): row["sql"]
        for row in rows
        if row["sql"] is not None
    }


def _validate_schema(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version != TASK_OUTBOX_SCHEMA_VERSION:
        raise TaskOutboxError(
            "Task outbox schema version "
            f"{version} is not supported; expected {TASK_OUTBOX_SCHEMA_VERSION}"
        )
    actual = {
        key: _normalize_schema_sql(value)
        for key, value in _load_schema_objects(connection).items()
    }
    expected = {
        ("table", "task_outbox"): _normalize_schema_sql(_TASK_OUTBOX_TABLE_SQL)
    }
    if actual != expected:
        raise TaskOutboxError(
            "Task outbox database schema does not match the clean-break format"
        )


__all__ = [
    "TASK_OUTBOX_FORMAT",
    "TaskOutbox",
    "TaskOutboxClaim",
    "TaskOutboxError",
    "task_outbox_path",
]
