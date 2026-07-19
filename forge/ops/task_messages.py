"""Durable Task inbox, confirmed worker packets, and message acknowledgements."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from .surface_events import (
    SurfaceEventError,
    TrustedTurnContext,
    surface_event_payload_hash,
)
from .task_database import TaskDatabase, TaskDatabaseError
from .task_projects import TaskProject, TaskProjectError
from .task_settings_v2 import TaskRequestV2, TaskSettingsV2, TaskSettingsV2Error


TASK_MESSAGE_FORMAT = "forge-task-message/v1"
TASK_MESSAGE_PACKET_FORMAT = "forge-task-message-packet/v1"
MAX_MESSAGE_BYTES = 64 * 1024
MAX_REVISION_MESSAGES = 100
MAX_REVISION_BYTES = 1024 * 1024

_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_LIFECYCLE_EVENTS = (
    "active",
    "revision_requested",
    "changing",
    "revision_cancelled",
    "revision_resumed",
    "stop_requested",
    "stopping",
    "cancelled",
    "expired",
    "merged",
    "replaced",
    "partially_merged",
)
_ACTIVE_EVENTS = frozenset({"active", "revision_resumed"})
_STOP_EVENTS = frozenset({"stop_requested", "stopping"})
_TERMINAL_EVENTS = frozenset(
    {"cancelled", "expired", "merged", "replaced", "partially_merged"}
)


class TaskMessageError(RuntimeError):
    """Raised when a Task inbox operation cannot preserve its exact contract."""


class TaskMessageConflictError(TaskMessageError):
    """Raised when one immutable source event is reused with different input."""


@dataclass(frozen=True, slots=True)
class TaskMessage:
    format_version: str
    message_id: str
    request_id: str
    parent_issue_number: int
    user_id: str
    session_id: str
    source_event_id: str
    text: str
    created_at: datetime
    message_hash: str


@dataclass(frozen=True, slots=True)
class TaskMessageReceipt:
    message: TaskMessage
    revision_request_id: str
    base_task_settings_hash: str
    created: bool


@dataclass(frozen=True, slots=True)
class TaskPacketMessage:
    format_version: str
    message_id: str
    message_hash: str
    created_at: datetime
    role: str
    text: str

    def payload(self) -> dict[str, object]:
        return {
            "created_at": _format_time(self.created_at),
            "format_version": self.format_version,
            "message_hash": self.message_hash,
            "message_id": self.message_id,
            "role": self.role,
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class TaskMessagePacket:
    format_version: str
    request_id: str
    task_settings_hash: str
    messages: tuple[TaskPacketMessage, ...]
    packet_hash: str

    def _payload(self) -> dict[str, object]:
        return {
            "format_version": self.format_version,
            "messages": [message.payload() for message in self.messages],
            "request_id": self.request_id,
            "task_settings_hash": self.task_settings_hash,
        }

    def to_json(self) -> str:
        """Return runtime-neutral canonical UTF-8 JSON without a self hash."""

        return _canonical_json(self._payload())


@dataclass(frozen=True, slots=True)
class _RuntimeRunWindow:
    state: str
    started_at: datetime
    ended_at: datetime | None

    def upper_bound(self, current_at: datetime) -> datetime:
        return self.ended_at if self.ended_at is not None else current_at


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bound_hash(label: str, *values: str) -> str:
    encoded = "\0".join((label, *values)).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _utc(value: datetime, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise TaskMessageError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _format_time(value: datetime) -> str:
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _parse_time(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise TaskMessageError(f"stored {field_name} is invalid")
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError:
        raise TaskMessageError(f"stored {field_name} is invalid") from None
    if _format_time(parsed) != value:
        raise TaskMessageError(f"stored {field_name} is not canonical")
    return parsed


def _require_id(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 512
        or "\x00" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise TaskMessageError(f"{field_name} is invalid")
    return value


def _require_hash(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise TaskMessageError(f"{field_name} must be a lowercase SHA-256")
    return value


def _require_reason(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise TaskMessageError("worker acknowledgement reason is not canonical")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise TaskMessageError(
            "worker acknowledgement reason is not canonical"
        ) from None
    if len(encoded) > 4096:
        raise TaskMessageError("worker acknowledgement reason is not canonical")
    return value


def _message_id(
    request_id: str,
    session_id: str,
    source_event_id: str,
    message_hash: str,
) -> str:
    return _bound_hash(
        TASK_MESSAGE_FORMAT,
        request_id,
        session_id,
        source_event_id,
        message_hash,
    )


def _revision_id(
    request_id: str,
    task_settings_hash: str,
    source_event_id: str,
) -> str:
    return _bound_hash(
        "forge-task-revision-request/v1",
        request_id,
        task_settings_hash,
        source_event_id,
    )


def _event_id(
    message_id: str,
    task_settings_hash: str,
    run_id: str | None,
    event_type: str,
) -> str:
    return _bound_hash(
        "forge-task-message-event/v1",
        message_id,
        task_settings_hash,
        run_id or "",
        event_type,
    )


def _latest_lifecycle_event(
    connection: sqlite3.Connection,
    request_id: str,
) -> tuple[int, str, str | None] | None:
    placeholders = ",".join("?" for _ in _LIFECYCLE_EVENTS)
    rows = connection.execute(
        f"""
        SELECT event_id, event_type, task_settings_hash
        FROM task_events
        WHERE request_id = ? AND event_type IN ({placeholders})
        ORDER BY event_id DESC
        LIMIT 1
        """,
        (request_id, *_LIFECYCLE_EVENTS),
    ).fetchall()
    if not rows:
        return None
    row = rows[0]
    if not isinstance(row[0], int) or not isinstance(row[1], str):
        raise TaskMessageError("stored Task lifecycle event is invalid")
    if row[2] is not None and not isinstance(row[2], str):
        raise TaskMessageError("stored Task lifecycle settings hash is invalid")
    return int(row[0]), str(row[1]), row[2]


def _require_active_on_connection(
    connection: sqlite3.Connection,
    request_id: str,
    task_settings_hash: str,
) -> None:
    latest = _latest_lifecycle_event(connection, request_id)
    if latest is None or latest[1] not in _ACTIVE_EVENTS:
        state = "not active" if latest is None else latest[1]
        raise TaskMessageError(f"Task is {state}")
    if latest[2] != task_settings_hash:
        raise TaskMessageError("Task settings changed before the safe point")
    row = connection.execute(
        """
        SELECT task_settings_hash FROM task_settings_v2
        WHERE request_id = ?
        """,
        (request_id,),
    ).fetchone()
    if row is None or row[0] != task_settings_hash:
        raise TaskMessageError("Task settings are not the exact active record")
    event = connection.execute(
        """
        SELECT event_key, event_json, project_id
        FROM task_events WHERE event_id = ?
        """,
        (latest[0],),
    ).fetchone()
    if event is None:
        raise TaskMessageError("Task lifecycle event disappeared")
    if latest[1] == "active":
        expected = _canonical_json({"task_settings_hash": task_settings_hash})
        if tuple(event) != ("active", expected, None):
            raise TaskMessageError("Task active event changed")
        historic_barrier = connection.execute(
            """
            SELECT 1 FROM task_events
            WHERE request_id = ? AND event_type IN (
                'revision_requested', 'changing', 'revision_cancelled',
                'revision_resumed', 'stop_requested', 'stopping', 'cancelled',
                'expired', 'merged', 'replaced', 'partially_merged'
            ) LIMIT 1
            """,
            (request_id,),
        ).fetchone()
        if historic_barrier is not None:
            raise TaskMessageError("Task active event follows an unresolved barrier")
        return

    revision_id = str(event[0]).removeprefix("revision_resumed:")
    if (
        event[0] != f"revision_resumed:{revision_id}"
        or event[1] != _canonical_json({"revision_request_id": revision_id})
        or event[2] is not None
    ):
        raise TaskMessageError("Task Resume event changed")
    revision = connection.execute(
        """
        SELECT request_id, base_task_settings_hash, state, created_at, updated_at
        FROM task_revision_requests WHERE revision_request_id = ?
        """,
        (revision_id,),
    ).fetchone()
    if revision is None or tuple(revision[:3]) != (
        request_id,
        task_settings_hash,
        "resumed",
    ):
        raise TaskMessageError("Task Resume does not match one revision")
    invalid_message = connection.execute(
        """
        SELECT 1 FROM task_messages AS m
        WHERE m.request_id = ? AND m.created_at >= ? AND m.created_at <= ?
          AND (
              (SELECT count(*) FROM task_message_events AS rejected_event
               WHERE rejected_event.message_id = m.message_id
                 AND rejected_event.task_settings_hash = ?
                 AND rejected_event.event_type = 'rejected'
                 AND rejected_event.worker_task_id IS NULL
                 AND rejected_event.run_id IS NULL) != 1
              OR EXISTS (
                  SELECT 1 FROM task_message_events AS applied_event
                  WHERE applied_event.message_id = m.message_id
                    AND applied_event.event_type = 'applied'
              )
          )
        LIMIT 1
        """,
        (request_id, revision[3], revision[4], task_settings_hash),
    ).fetchone()
    if invalid_message is not None:
        raise TaskMessageError("Task Resume has an un-rejected revision message")
    other_open = connection.execute(
        """
        SELECT 1 FROM task_revision_requests
        WHERE request_id = ? AND state IN ('requested', 'confirmed') LIMIT 1
        """,
        (request_id,),
    ).fetchone()
    if other_open is not None:
        raise TaskMessageError("Task Resume has another pending revision")


def _require_message_access(
    connection: sqlite3.Connection,
    request_id: str,
    context: TrustedTurnContext,
) -> None:
    denied = TaskMessageError("Task is unavailable or access is denied")
    request = connection.execute(
        "SELECT task_owner_host FROM task_requests WHERE request_id = ?",
        (request_id,),
    ).fetchone()
    if request is None or request[0] != context.owner_host:
        raise denied

    access = connection.execute(
        """
        SELECT role, revoked_at
        FROM task_access
        WHERE request_id = ? AND surface = ? AND subject_id = ?
        """,
        (request_id, context.surface, context.subject_id),
    ).fetchone()
    if access is not None:
        if access[0] not in {"owner", "operator"} or access[1] is not None:
            raise denied
        return

    bindings = connection.execute(
        """
        SELECT binding.parent_issue_number, settings.parent_issue_number
        FROM task_session_bindings AS binding
        JOIN task_settings_v2 AS settings
          ON settings.request_id = binding.request_id
         AND settings.parent_issue_number = binding.parent_issue_number
        WHERE binding.request_id = ? AND binding.surface = ?
          AND binding.subject_id = ? AND binding.session_id = ?
        """,
        (
            request_id,
            context.surface,
            context.subject_id,
            context.session_id,
        ),
    ).fetchall()
    if len(bindings) != 1:
        raise denied
    binding = bindings[0]
    if (
        not isinstance(binding[0], int)
        or binding[0] <= 0
        or binding[0] != binding[1]
    ):
        raise denied


def _message_from_row(row: sqlite3.Row) -> TaskMessage:
    try:
        message = TaskMessage(
            format_version=str(row["format_version"]),
            message_id=str(row["message_id"]),
            request_id=str(row["request_id"]),
            parent_issue_number=int(row["parent_issue_number"]),
            user_id=str(row["user_id"]),
            session_id=str(row["session_id"]),
            source_event_id=str(row["source_event_id"]),
            text=str(row["text"]),
            created_at=_parse_time(row["created_at"], "message created_at"),
            message_hash=str(row["message_hash"]),
        )
    except (IndexError, KeyError, TypeError, ValueError) as error:
        raise TaskMessageError("stored Task message is invalid") from error
    if (
        message.format_version != TASK_MESSAGE_FORMAT
        or message.parent_issue_number <= 0
        or _hash_text(message.text) != message.message_hash
        or _message_id(
            message.request_id,
            message.session_id,
            message.source_event_id,
            message.message_hash,
        )
        != message.message_id
    ):
        raise TaskMessageError("stored Task message changed")
    return message


class TaskMessageStore:
    """Append immutable user messages and expose only confirmed packets."""

    def __init__(
        self,
        database: TaskDatabase,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(database, TaskDatabase):
            raise TaskMessageError("database must be a TaskDatabase")
        self._database = database
        self._clock = clock or (lambda: datetime.now(UTC))

    def send(
        self,
        request_id: str,
        context: TrustedTurnContext,
        text: str,
        *,
        at: datetime | None = None,
    ) -> TaskMessageReceipt:
        """Append a message and the first revision barrier in one transaction."""

        _require_id(request_id, "request_id")
        if not isinstance(context, TrustedTurnContext):
            raise TaskMessageError("context must be a TrustedTurnContext")
        if not isinstance(text, str):
            raise TaskMessageError("message text must be UTF-8 text")
        try:
            encoded = text.encode("utf-8")
        except UnicodeEncodeError:
            raise TaskMessageError("message text must be UTF-8 text") from None
        if not encoded:
            raise TaskMessageError("message text must not be empty")
        if len(encoded) > MAX_MESSAGE_BYTES:
            raise TaskMessageError("message exceeds the UTF-8 64 KiB limit")
        timestamp = _utc(at if at is not None else self._clock(), "message time")
        try:
            with self._database.transaction() as connection:
                return self._send_on_connection(
                    connection,
                    request_id,
                    context,
                    text,
                    encoded_size=len(encoded),
                    timestamp=timestamp,
                )
        except TaskDatabaseError as error:
            raise TaskMessageError("Task message transaction failed") from error

    def _send_on_connection(
        self,
        connection: sqlite3.Connection,
        request_id: str,
        context: TrustedTurnContext,
        text: str,
        *,
        encoded_size: int,
        timestamp: datetime,
    ) -> TaskMessageReceipt:
        _require_message_access(connection, request_id, context)

        event = connection.execute(
            """
            SELECT subject_id, session_id, surface, payload_hash, state
            FROM surface_events WHERE source_event_id = ?
            """,
            (context.source_event_id,),
        ).fetchone()
        if event is None:
            raise TaskMessageError("trusted source event was not recorded")
        if tuple(event[:3]) != (
            context.subject_id,
            context.session_id,
            context.surface,
        ):
            raise TaskMessageConflictError("source event identity changed")
        try:
            expected_payload_hash = surface_event_payload_hash(context, text)
        except SurfaceEventError as error:
            raise TaskMessageError("trusted source event payload is invalid") from error
        if event[3] != expected_payload_hash:
            raise TaskMessageConflictError("source event payload changed")
        if event[4] == "expired":
            raise TaskMessageError("trusted source event has expired")

        existing_row = connection.execute(
            "SELECT * FROM task_messages WHERE source_event_id = ?",
            (context.source_event_id,),
        ).fetchone()
        if existing_row is not None:
            existing = _message_from_row(existing_row)
            expected_hash = _hash_text(text)
            if (
                existing.request_id != request_id
                or existing.user_id != context.subject_id
                or existing.session_id != context.session_id
                or existing.text != text
                or existing.message_hash != expected_hash
            ):
                raise TaskMessageConflictError(
                    "source event is already bound to a different Task message"
                )
            revision_id, base_hash = self._revision_for_message(
                connection,
                existing,
            )
            return TaskMessageReceipt(existing, revision_id, base_hash, False)

        settings = connection.execute(
            """
            SELECT task_settings_hash, parent_issue_number
            FROM task_settings_v2 WHERE request_id = ?
            """,
            (request_id,),
        ).fetchone()
        if settings is None:
            raise TaskMessageError("Task has no active settings")
        base_hash = _require_hash(settings[0], "task_settings_hash")
        parent_issue_number = settings[1]
        if not isinstance(parent_issue_number, int) or parent_issue_number <= 0:
            raise TaskMessageError("Task parent issue number is invalid")
        latest = _latest_lifecycle_event(connection, request_id)
        if latest is None:
            raise TaskMessageError("Task is not messageable")
        if latest[1] in _STOP_EVENTS or latest[1] in _TERMINAL_EVENTS:
            raise TaskMessageError("Task is not messageable in its current state")

        pending_rows = connection.execute(
            """
            SELECT revision_request_id, base_task_settings_hash,
                   replacement_request_id, source_event_id, state, created_at,
                   updated_at, preview_hash
            FROM task_revision_requests
            WHERE request_id = ? AND state IN ('requested', 'confirmed')
            ORDER BY created_at, revision_request_id
            """,
            (request_id,),
        ).fetchall()
        revision_id: str
        revision_created_at: datetime
        if latest[1] in _ACTIVE_EVENTS:
            if latest[2] != base_hash or pending_rows:
                raise TaskMessageError("Task active state and revision state disagree")
            previous = connection.execute(
                """
                SELECT updated_at FROM task_revision_requests
                WHERE request_id = ? ORDER BY updated_at DESC, revision_request_id DESC
                LIMIT 1
                """,
                (request_id,),
            ).fetchone()
            if previous is not None and timestamp <= _parse_time(
                previous[0], "revision updated_at"
            ):
                raise TaskMessageError(
                    "message time does not follow the prior revision"
                )
            revision_id = _revision_id(
                request_id,
                base_hash,
                context.source_event_id,
            )
            revision_created_at = timestamp
        elif latest[1] in {"revision_requested", "changing"}:
            if len(pending_rows) != 1 or pending_rows[0]["state"] != "requested":
                raise TaskMessageError(
                    "Task changing state has no exact pending revision"
                )
            pending = pending_rows[0]
            revision_id = str(pending["revision_request_id"])
            if pending["base_task_settings_hash"] != base_hash:
                raise TaskMessageError("pending revision settings changed")
            revision_created_at = _parse_time(
                pending["created_at"], "revision created_at"
            )
            if timestamp < _parse_time(pending["updated_at"], "revision updated_at"):
                raise TaskMessageError(
                    "message time moved backwards within the revision"
                )
        else:
            raise TaskMessageError(
                "Task is not messageable while changing is unresolved"
            )

        current_messages = self._pending_rows_for_revision(
            connection,
            request_id=request_id,
            revision_created_at=revision_created_at,
        )
        if len(current_messages) >= MAX_REVISION_MESSAGES:
            raise TaskMessageError("revision already contains 100 messages")
        total_bytes = sum(len(row["text"].encode("utf-8")) for row in current_messages)
        if total_bytes + encoded_size > MAX_REVISION_BYTES:
            raise TaskMessageError("revision exceeds the UTF-8 1 MiB limit")

        message_hash = _hash_text(text)
        message_id = _message_id(
            request_id,
            context.session_id,
            context.source_event_id,
            message_hash,
        )
        created_at = _format_time(timestamp)
        connection.execute(
            """
            INSERT INTO task_messages (
                message_id, format_version, request_id, parent_issue_number,
                user_id, session_id, source_event_id, text, created_at,
                message_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                TASK_MESSAGE_FORMAT,
                request_id,
                parent_issue_number,
                context.subject_id,
                context.session_id,
                context.source_event_id,
                text,
                created_at,
                message_hash,
            ),
        )
        if latest[1] in _ACTIVE_EVENTS:
            connection.execute(
                """
                INSERT INTO task_revision_requests (
                    revision_request_id, request_id, base_task_settings_hash,
                    replacement_request_id, source_event_id, state,
                    preview_hash, created_at, updated_at
                ) VALUES (?, ?, ?, NULL, ?, 'requested', NULL, ?, ?)
                """,
                (
                    revision_id,
                    request_id,
                    base_hash,
                    context.source_event_id,
                    created_at,
                    created_at,
                ),
            )
            payload = _canonical_json(
                {
                    "base_task_settings_hash": base_hash,
                    "revision_request_id": revision_id,
                }
            )
            # RISK(race): the immutable message and both lifecycle barriers are
            # committed together, so dispatch/result/GitHub/merge guards cannot
            # observe the message without also observing ``changing``.
            for event_type in ("revision_requested", "changing"):
                connection.execute(
                    """
                    INSERT INTO task_events (
                        request_id, task_settings_hash, event_type, event_key,
                        event_json, occurred_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        base_hash,
                        event_type,
                        f"{event_type}:{revision_id}",
                        payload,
                        created_at,
                    ),
                )
        else:
            replacement_id = pending_rows[0]["replacement_request_id"]
            if replacement_id is not None:
                if (
                    connection.execute(
                        "SELECT 1 FROM task_settings_v2 WHERE request_id = ?",
                        (replacement_id,),
                    ).fetchone()
                    is not None
                ):
                    raise TaskMessageError(
                        "confirmed replacement cannot accept another message"
                    )
                rows = connection.execute(
                    """
                    SELECT state, root_card_id, task_settings_hash
                    FROM task_projects WHERE request_id = ?
                    """,
                    (replacement_id,),
                ).fetchall()
                if not rows or any(
                    tuple(row) != ("prepared", None, None) for row in rows
                ):
                    raise TaskMessageError(
                        "staged replacement changed before preview invalidation"
                    )
                connection.execute(
                    """
                    UPDATE task_projects SET state = 'cancelled', updated_at = ?
                    WHERE request_id = ? AND state = 'prepared'
                      AND root_card_id IS NULL AND task_settings_hash IS NULL
                    """,
                    (created_at, replacement_id),
                )
                connection.execute(
                    """
                    INSERT INTO task_events (
                        request_id, event_type, event_key, event_json,
                        occurred_at
                    ) VALUES (?, 'cancelled', ?, ?, ?)
                    """,
                    (
                        replacement_id,
                        f"cancelled:preview-invalidated:{revision_id}",
                        _canonical_json(
                            {
                                "reason": "revision preview invalidated",
                                "revision_request_id": revision_id,
                            }
                        ),
                        created_at,
                    ),
                )
            updated = connection.execute(
                """
                UPDATE task_revision_requests
                SET replacement_request_id = NULL, preview_hash = NULL,
                    updated_at = ?
                WHERE revision_request_id = ? AND state = 'requested'
                  AND base_task_settings_hash = ?
                """,
                (created_at, revision_id, base_hash),
            )
            if updated.rowcount != 1:
                raise TaskMessageError("pending revision changed during append")
        row = connection.execute(
            "SELECT * FROM task_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        return TaskMessageReceipt(
            _message_from_row(row),
            revision_id,
            base_hash,
            True,
        )

    def _revision_for_message(
        self,
        connection: sqlite3.Connection,
        message: TaskMessage,
    ) -> tuple[str, str]:
        rows = connection.execute(
            """
            SELECT revision_request_id, base_task_settings_hash
            FROM task_revision_requests
            WHERE request_id = ? AND created_at <= ? AND updated_at >= ?
            ORDER BY created_at DESC, revision_request_id DESC
            """,
            (
                message.request_id,
                _format_time(message.created_at),
                _format_time(message.created_at),
            ),
        ).fetchall()
        if len(rows) != 1:
            raise TaskMessageError("message does not belong to one exact revision")
        return str(rows[0][0]), _require_hash(rows[0][1], "base_task_settings_hash")

    @staticmethod
    def _pending_rows_for_revision(
        connection: sqlite3.Connection,
        *,
        request_id: str,
        revision_created_at: datetime,
        revision_updated_at: datetime | None = None,
    ) -> tuple[sqlite3.Row, ...]:
        parameters: list[object] = [request_id, _format_time(revision_created_at)]
        upper = ""
        if revision_updated_at is not None:
            upper = "AND m.created_at <= ?"
            parameters.append(_format_time(revision_updated_at))
        return tuple(
            connection.execute(
                f"""
                SELECT m.* FROM task_messages AS m
                WHERE m.request_id = ? AND m.created_at >= ? {upper}
                  AND NOT EXISTS (
                      SELECT 1 FROM task_message_events AS terminal_event
                      WHERE terminal_event.message_id = m.message_id
                        AND terminal_event.event_type IN ('applied', 'rejected')
                  )
                ORDER BY m.created_at, m.message_id
                """,
                tuple(parameters),
            ).fetchall()
        )

    def pending_for_revision(
        self,
        revision_request_id: str,
    ) -> tuple[TaskMessage, ...]:
        _require_id(revision_request_id, "revision_request_id")
        with self._database.read() as connection:
            row = connection.execute(
                """
                SELECT request_id, created_at, updated_at
                FROM task_revision_requests WHERE revision_request_id = ?
                """,
                (revision_request_id,),
            ).fetchone()
            if row is None:
                raise TaskMessageError("revision request does not exist")
            rows = self._pending_rows_for_revision(
                connection,
                request_id=str(row[0]),
                revision_created_at=_parse_time(row[1], "revision created_at"),
                revision_updated_at=_parse_time(row[2], "revision updated_at"),
            )
            return tuple(_message_from_row(item) for item in rows)

    def build_packet(
        self,
        request_id: str,
        task_settings_hash: str,
    ) -> TaskMessagePacket:
        _require_id(request_id, "request_id")
        _require_hash(task_settings_hash, "task_settings_hash")
        with self._database.read() as connection:
            _require_active_on_connection(connection, request_id, task_settings_hash)
            revisions = connection.execute(
                """
                SELECT revision_request_id, request_id, created_at, updated_at
                FROM task_revision_requests
                WHERE replacement_request_id = ? AND state = 'confirmed'
                ORDER BY created_at, revision_request_id
                """,
                (request_id,),
            ).fetchall()
            if len(revisions) > 1:
                raise TaskMessageError("replacement belongs to multiple revisions")
            messages: tuple[TaskMessage, ...] = ()
            if revisions:
                revision = revisions[0]
                rows = self._pending_rows_for_revision(
                    connection,
                    request_id=str(revision[1]),
                    revision_created_at=_parse_time(revision[2], "revision created_at"),
                    revision_updated_at=_parse_time(revision[3], "revision updated_at"),
                )
                messages = tuple(_message_from_row(row) for row in rows)
            packet_messages = tuple(
                TaskPacketMessage(
                    format_version=message.format_version,
                    message_id=message.message_id,
                    message_hash=message.message_hash,
                    created_at=message.created_at,
                    role="user",
                    text=message.text,
                )
                for message in messages
            )
            payload = {
                "format_version": TASK_MESSAGE_PACKET_FORMAT,
                "messages": [message.payload() for message in packet_messages],
                "request_id": request_id,
                "task_settings_hash": task_settings_hash,
            }
            rendered = _canonical_json(payload)
            return TaskMessagePacket(
                format_version=TASK_MESSAGE_PACKET_FORMAT,
                request_id=request_id,
                task_settings_hash=task_settings_hash,
                messages=packet_messages,
                packet_hash=hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
            )

    def record_included(
        self,
        packet: TaskMessagePacket,
        *,
        worker_task_id: str,
        run_id: str,
        at: datetime | None = None,
    ) -> None:
        current_at = _utc(self._clock(), "included check time")
        timestamp = _utc(at if at is not None else current_at, "included time")
        worker_task_id = _require_id(worker_task_id, "worker_task_id")
        run_id = _require_id(run_id, "run_id")
        with self._database.transaction() as connection:
            self._validate_packet(connection, packet)
            run_window = self._require_exact_run_packet(
                connection,
                packet,
                worker_task_id=worker_task_id,
                run_id=run_id,
                current_at=current_at,
            )
            for message in packet.messages:
                self._ensure_message_event(
                    connection,
                    message_id=message.message_id,
                    task_settings_hash=packet.task_settings_hash,
                    worker_task_id=worker_task_id,
                    run_id=run_id,
                    event_type="included",
                    reason=None,
                    occurred_at=timestamp,
                    run_window=run_window,
                    current_at=current_at,
                    allow_insert=run_window.state == "running",
                )

    def record_ack(
        self,
        packet: TaskMessagePacket,
        *,
        message_id: str,
        outcome: str,
        worker_task_id: str,
        run_id: str,
        reason: str,
        at: datetime | None = None,
    ) -> None:
        if outcome not in {"applied", "rejected"}:
            raise TaskMessageError("worker outcome must be applied or rejected")
        _require_id(message_id, "message_id")
        worker_task_id = _require_id(worker_task_id, "worker_task_id")
        run_id = _require_id(run_id, "run_id")
        reason = _require_reason(reason)
        current_at = _utc(self._clock(), "ack check time")
        timestamp = _utc(at if at is not None else current_at, "ack time")
        with self._database.transaction() as connection:
            self._validate_packet(connection, packet)
            run_window = self._require_exact_run_packet(
                connection,
                packet,
                worker_task_id=worker_task_id,
                run_id=run_id,
                current_at=current_at,
            )
            if message_id not in {message.message_id for message in packet.messages}:
                raise TaskMessageError(
                    "worker acknowledged a message outside its packet"
                )
            terminal = connection.execute(
                """
                SELECT event_type, reason
                FROM task_message_events
                WHERE message_id = ? AND task_settings_hash = ? AND run_id = ?
                  AND event_type IN ('applied', 'rejected')
                """,
                (message_id, packet.task_settings_hash, run_id),
            ).fetchall()
            if not terminal and run_window.state != "running":
                raise TaskMessageError(
                    "completed runtime run cannot add message acknowledgements"
                )
            included_at = self._require_message_event(
                connection,
                message_id=message_id,
                task_settings_hash=packet.task_settings_hash,
                worker_task_id=worker_task_id,
                run_id=run_id,
                event_type="included",
                run_window=run_window,
                current_at=current_at,
            )
            if terminal:
                if len(terminal) != 1:
                    raise TaskMessageError("worker acknowledgement is ambiguous")
                stored_outcome = str(terminal[0][0])
                stored_reason = _require_reason(terminal[0][1])
                self._require_message_event(
                    connection,
                    message_id=message_id,
                    task_settings_hash=packet.task_settings_hash,
                    worker_task_id=worker_task_id,
                    run_id=run_id,
                    event_type=stored_outcome,
                    run_window=run_window,
                    current_at=current_at,
                    not_before=included_at,
                )
                if (stored_outcome, stored_reason) != (outcome, reason):
                    raise TaskMessageError("worker acknowledgement is immutable")
                return
            self._ensure_message_event(
                connection,
                message_id=message_id,
                task_settings_hash=packet.task_settings_hash,
                worker_task_id=worker_task_id,
                run_id=run_id,
                event_type=outcome,
                reason=reason,
                occurred_at=timestamp,
                run_window=run_window,
                current_at=current_at,
                not_before=included_at,
            )

    def require_result_acknowledged(
        self,
        packet: TaskMessagePacket,
        *,
        worker_task_id: str,
        run_id: str,
    ) -> None:
        worker_task_id = _require_id(worker_task_id, "worker_task_id")
        run_id = _require_id(run_id, "run_id")
        current_at = _utc(self._clock(), "result check time")
        with self._database.transaction() as connection:
            self._validate_packet(connection, packet)
            run_window = self._require_exact_run_packet(
                connection,
                packet,
                worker_task_id=worker_task_id,
                run_id=run_id,
                current_at=current_at,
            )
            for message in packet.messages:
                included_at = self._require_message_event(
                    connection,
                    message_id=message.message_id,
                    task_settings_hash=packet.task_settings_hash,
                    worker_task_id=worker_task_id,
                    run_id=run_id,
                    event_type="included",
                    run_window=run_window,
                    current_at=current_at,
                )
                rows = connection.execute(
                    """
                    SELECT event_type, reason
                    FROM task_message_events
                    WHERE message_id = ? AND task_settings_hash = ? AND run_id = ?
                      AND event_type IN ('applied', 'rejected')
                    """,
                    (message.message_id, packet.task_settings_hash, run_id),
                ).fetchall()
                if len(rows) != 1:
                    raise TaskMessageError(
                        f"pending message blocks result acceptance: {message.message_id}"
                    )
                outcome = str(rows[0][0])
                _require_reason(rows[0][1])
                self._require_message_event(
                    connection,
                    message_id=message.message_id,
                    task_settings_hash=packet.task_settings_hash,
                    worker_task_id=worker_task_id,
                    run_id=run_id,
                    event_type=outcome,
                    run_window=run_window,
                    current_at=current_at,
                    not_before=included_at,
                )

    def _validate_packet(
        self,
        connection: sqlite3.Connection,
        packet: TaskMessagePacket,
    ) -> None:
        if not isinstance(packet, TaskMessagePacket):
            raise TaskMessageError("packet must be a TaskMessagePacket")
        if packet.format_version != TASK_MESSAGE_PACKET_FORMAT:
            raise TaskMessageError("message packet format changed")
        expected_hash = hashlib.sha256(packet.to_json().encode("utf-8")).hexdigest()
        if packet.packet_hash != expected_hash:
            raise TaskMessageError("message packet hash changed")
        _require_active_on_connection(
            connection,
            packet.request_id,
            packet.task_settings_hash,
        )
        if tuple(packet.messages) != tuple(
            sorted(packet.messages, key=lambda item: (item.created_at, item.message_id))
        ):
            raise TaskMessageError("message packet order changed")
        for item in packet.messages:
            row = connection.execute(
                "SELECT * FROM task_messages WHERE message_id = ?",
                (item.message_id,),
            ).fetchone()
            if row is None:
                raise TaskMessageError("message packet references a missing message")
            stored = _message_from_row(row)
            if item != TaskPacketMessage(
                format_version=stored.format_version,
                message_id=stored.message_id,
                message_hash=stored.message_hash,
                created_at=stored.created_at,
                role="user",
                text=stored.text,
            ):
                raise TaskMessageError("message packet content changed")
        revision = connection.execute(
            """
            SELECT request_id, created_at, updated_at
            FROM task_revision_requests
            WHERE replacement_request_id = ? AND state = 'confirmed'
            """,
            (packet.request_id,),
        ).fetchall()
        if len(revision) > 1:
            raise TaskMessageError("message packet replacement is ambiguous")
        if packet.messages and not revision:
            raise TaskMessageError("message packet has no confirmed revision")
        if revision:
            base_request_id, created_at, updated_at = tuple(revision[0])
            for item in packet.messages:
                row = connection.execute(
                    """
                    SELECT 1 FROM task_messages
                    WHERE message_id = ? AND request_id = ?
                      AND created_at >= ? AND created_at <= ?
                    """,
                    (item.message_id, base_request_id, created_at, updated_at),
                ).fetchone()
                if row is None:
                    raise TaskMessageError(
                        "message packet crosses its confirmed revision boundary"
                    )

    @staticmethod
    def _require_exact_run_packet(
        connection: sqlite3.Connection,
        packet: TaskMessagePacket,
        *,
        worker_task_id: str,
        run_id: str,
        current_at: datetime,
    ) -> _RuntimeRunWindow:
        run = connection.execute(
            """
            SELECT runtime.request_id AS runtime_request_id,
                   runtime.task_settings_hash AS runtime_settings_hash,
                   runtime.project_id AS runtime_project_id,
                   runtime.host_id AS runtime_host_id,
                   runtime.worker_task_id AS runtime_worker_task_id,
                   runtime.message_packet_hash AS message_packet_hash,
                   runtime.state AS runtime_state,
                   runtime.result_hash AS result_hash,
                   runtime.started_at AS started_at,
                   runtime.ended_at AS ended_at,
                   request.request_json AS request_json,
                   request.task_owner_host AS request_host,
                   settings.settings_json AS settings_json,
                   settings.task_owner_host AS settings_host,
                   project.task_settings_hash AS project_settings_hash,
                   project.project_json AS project_json
            FROM task_runtime_runs AS runtime
            JOIN task_requests AS request
              ON request.request_id = runtime.request_id
            JOIN task_settings_v2 AS settings
              ON settings.request_id = runtime.request_id
             AND settings.task_settings_hash = runtime.task_settings_hash
            JOIN task_projects AS project
              ON project.request_id = runtime.request_id
             AND project.project_id = runtime.project_id
             AND project.task_settings_hash = runtime.task_settings_hash
            WHERE runtime.run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if run is None:
            raise TaskMessageError("runtime run is unavailable or does not match")
        if (
            run["runtime_request_id"] != packet.request_id
            or run["runtime_settings_hash"] != packet.task_settings_hash
        ):
            raise TaskMessageError("runtime run Task or settings do not match packet")
        if run["runtime_worker_task_id"] != worker_task_id:
            raise TaskMessageError("runtime run worker does not match")
        if run["message_packet_hash"] != packet.packet_hash:
            raise TaskMessageError("runtime run packet hash does not match")

        try:
            request = TaskRequestV2.from_json(run["request_json"])
            settings = TaskSettingsV2.from_json(
                run["settings_json"],
                request=request,
            )
            project_payload = json.loads(run["project_json"])
            project = TaskProject.from_mapping(project_payload)
        except (
            json.JSONDecodeError,
            TaskProjectError,
            TaskSettingsV2Error,
            TypeError,
        ) as error:
            raise TaskMessageError("runtime run Project record is invalid") from error
        canonical_project = _canonical_json(
            {
                "base_branch": project.base_branch,
                "base_commit": project.base_commit,
                "host_id": project.host_id,
                "project_id": project.project_id,
                "remote_name": project.remote_name,
                "repository": project.repository,
                "workspace": project.workspace,
            }
        )
        if (
            run["request_json"] != request.to_json()
            or run["settings_json"] != settings.to_json()
            or run["project_json"] != canonical_project
            or request.request_id != packet.request_id
            or settings.request_id != packet.request_id
            or settings.task_settings_hash != packet.task_settings_hash
            or run["runtime_project_id"] != project.project_id
            or run["project_settings_hash"] != packet.task_settings_hash
            or tuple(item for item in request.projects if item == project) != (project,)
            or tuple(item for item in settings.projects if item == project) != (project,)
            or run["request_host"] != run["settings_host"]
            or run["runtime_host_id"] != run["request_host"]
            or project.host_id != run["request_host"]
        ):
            raise TaskMessageError("runtime run Project or host binding changed")

        current_at = _utc(current_at, "runtime check time")
        started_at = _parse_time(run["started_at"], "runtime started_at")
        if started_at > current_at:
            raise TaskMessageError("runtime run time is in the future")
        run_state = run["runtime_state"]
        if run_state not in {"running", "completed"}:
            raise TaskMessageError(
                f"runtime run state {run_state} does not permit message events"
            )
        if run_state == "running":
            if run["result_hash"] is not None or run["ended_at"] is not None:
                raise TaskMessageError("running runtime run result binding changed")
            ended_at = None
        else:
            if (
                not isinstance(run["result_hash"], str)
                or _SHA256.fullmatch(run["result_hash"]) is None
            ):
                raise TaskMessageError("completed runtime run result binding changed")
            ended_at = _parse_time(run["ended_at"], "runtime ended_at")
            if ended_at < started_at or ended_at > current_at:
                raise TaskMessageError("runtime run chronology is invalid")
        run_window = _RuntimeRunWindow(str(run_state), started_at, ended_at)
        revision = connection.execute(
            """
            SELECT request_id, created_at, updated_at
            FROM task_revision_requests
            WHERE replacement_request_id = ? AND state = 'confirmed'
            """,
            (packet.request_id,),
        ).fetchall()
        if not revision:
            if packet.messages:
                raise TaskMessageError("run packet has no confirmed revision")
            return run_window
        if len(revision) != 1:
            raise TaskMessageError("run packet revision is ambiguous")
        rows = connection.execute(
            """
            SELECT m.message_id
            FROM task_messages AS m
            WHERE m.request_id = ? AND m.created_at >= ? AND m.created_at <= ?
              AND (
                  NOT EXISTS (
                      SELECT 1 FROM task_message_events AS terminal_event
                      WHERE terminal_event.message_id = m.message_id
                        AND terminal_event.event_type IN ('applied', 'rejected')
                  )
                  OR EXISTS (
                      SELECT 1 FROM task_message_events AS this_run_event
                      WHERE this_run_event.message_id = m.message_id
                        AND this_run_event.task_settings_hash = ?
                        AND this_run_event.run_id = ?
                        AND this_run_event.event_type IN ('applied', 'rejected')
                  )
              )
            ORDER BY m.created_at, m.message_id
            """,
            (
                revision[0][0],
                revision[0][1],
                revision[0][2],
                packet.task_settings_hash,
                run_id,
            ),
        ).fetchall()
        expected_ids = tuple(str(row[0]) for row in rows)
        actual_ids = tuple(message.message_id for message in packet.messages)
        if actual_ids != expected_ids:
            raise TaskMessageError("run packet omits or adds a revision message")
        return run_window

    @staticmethod
    def _require_message_event(
        connection: sqlite3.Connection,
        *,
        message_id: str,
        task_settings_hash: str,
        worker_task_id: str,
        run_id: str,
        event_type: str,
        run_window: _RuntimeRunWindow,
        current_at: datetime,
        not_before: datetime | None = None,
    ) -> datetime:
        rows = connection.execute(
            """
            SELECT message_event_id, worker_task_id, reason, occurred_at
            FROM task_message_events
            WHERE message_id = ? AND task_settings_hash = ? AND run_id = ?
              AND event_type = ?
            """,
            (message_id, task_settings_hash, run_id, event_type),
        ).fetchall()
        if len(rows) != 1:
            if event_type == "included":
                raise TaskMessageError("worker did not include this message in the run")
            raise TaskMessageError("worker acknowledgement event is unavailable")
        row = rows[0]
        expected_event_id = _event_id(
            message_id,
            task_settings_hash,
            run_id,
            event_type,
        )
        if row[0] != expected_event_id or row[1] != worker_task_id:
            raise TaskMessageError("Task message event identity changed")
        if event_type == "included":
            if row[2] is not None:
                raise TaskMessageError("included event reason changed")
        else:
            _require_reason(row[2])
        occurred_at = _parse_time(row[3], "message event occurred_at")
        upper_bound = run_window.upper_bound(current_at)
        if occurred_at < run_window.started_at or occurred_at > upper_bound:
            raise TaskMessageError("Task message event time is outside its runtime run")
        if not_before is not None and occurred_at < not_before:
            raise TaskMessageError("Task message event chronology is invalid")
        return occurred_at

    @staticmethod
    def _ensure_message_event(
        connection: sqlite3.Connection,
        *,
        message_id: str,
        task_settings_hash: str,
        worker_task_id: str | None,
        run_id: str | None,
        event_type: str,
        reason: str | None,
        occurred_at: datetime,
        run_window: _RuntimeRunWindow | None = None,
        current_at: datetime | None = None,
        not_before: datetime | None = None,
        allow_insert: bool = True,
    ) -> None:
        event_id = _event_id(message_id, task_settings_hash, run_id, event_type)
        if run_id is None:
            if (
                worker_task_id is not None
                or event_type != "rejected"
                or run_window is not None
                or current_at is not None
                or not_before is not None
            ):
                raise TaskMessageError("non-runtime message event binding is invalid")
            reason = _require_reason(reason)
            expected = (
                message_id,
                task_settings_hash,
                None,
                None,
                event_type,
                reason,
                _format_time(occurred_at),
            )
            existing = connection.execute(
                """
                SELECT message_id, task_settings_hash, worker_task_id, run_id,
                       event_type, reason, occurred_at
                FROM task_message_events WHERE message_event_id = ?
                """,
                (event_id,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != expected:
                    raise TaskMessageError("Task message event is immutable")
                return
            if not allow_insert:
                raise TaskMessageError("Task message event is unavailable")
            connection.execute(
                """
                INSERT INTO task_message_events (
                    message_event_id, message_id, task_settings_hash,
                    worker_task_id, run_id, event_type, reason, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (event_id, *expected),
            )
            return
        if worker_task_id is None or run_window is None or current_at is None:
            raise TaskMessageError("runtime message event binding is incomplete")
        existing = connection.execute(
            """
            SELECT reason, occurred_at
            FROM task_message_events
            WHERE message_id = ? AND task_settings_hash = ? AND run_id = ?
              AND event_type = ?
            """,
            (message_id, task_settings_hash, run_id, event_type),
        ).fetchone()
        if existing is not None:
            if existing[0] != reason:
                raise TaskMessageError("Task message event is immutable")
            TaskMessageStore._require_message_event(
                connection,
                message_id=message_id,
                task_settings_hash=task_settings_hash,
                worker_task_id=worker_task_id,
                run_id=run_id,
                event_type=event_type,
                run_window=run_window,
                current_at=current_at,
                not_before=not_before,
            )
            return
        if not allow_insert:
            raise TaskMessageError("completed runtime run cannot add message events")
        upper_bound = run_window.upper_bound(current_at)
        if occurred_at < run_window.started_at or occurred_at > upper_bound:
            raise TaskMessageError("Task message event time is outside its runtime run")
        if not_before is not None and occurred_at < not_before:
            raise TaskMessageError("Task message event chronology is invalid")
        connection.execute(
            """
            INSERT INTO task_message_events (
                message_event_id, message_id, task_settings_hash,
                worker_task_id, run_id, event_type, reason, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                message_id,
                task_settings_hash,
                worker_task_id,
                run_id,
                event_type,
                reason,
                _format_time(occurred_at),
            ),
        )
        TaskMessageStore._require_message_event(
            connection,
            message_id=message_id,
            task_settings_hash=task_settings_hash,
            worker_task_id=worker_task_id,
            run_id=run_id,
            event_type=event_type,
            run_window=run_window,
            current_at=current_at,
            not_before=not_before,
        )


__all__ = [
    "MAX_MESSAGE_BYTES",
    "MAX_REVISION_BYTES",
    "MAX_REVISION_MESSAGES",
    "TASK_MESSAGE_FORMAT",
    "TASK_MESSAGE_PACKET_FORMAT",
    "TaskMessage",
    "TaskMessageConflictError",
    "TaskMessageError",
    "TaskMessagePacket",
    "TaskMessageReceipt",
    "TaskMessageStore",
    "TaskPacketMessage",
]
