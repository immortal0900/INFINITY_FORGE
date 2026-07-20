"""Two-phase Task revision confirmation and append-only lifecycle recovery."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from .task_database import TaskDatabase, TaskDatabaseError
from .task_messages import (
    TaskMessage,
    TaskMessageError,
    TaskMessageStore,
    _canonical_json,
    _format_time,
    _latest_lifecycle_event,
    _message_from_row,
    _parse_time,
    _require_active_on_connection,
)
from .task_settings_v2 import (
    TaskRequestV2,
    TaskSettingsV2,
    TaskSettingsV2Error,
)


TASK_REVISION_PREVIEW_FORMAT = "forge-task-revision-preview/v1"


class TaskRevisionError(RuntimeError):
    """Raised when a revision transition is not exact or no longer permitted."""


@dataclass(frozen=True, slots=True)
class TaskRevisionRequest:
    revision_request_id: str
    request_id: str
    base_task_settings_hash: str
    replacement_request_id: str | None
    source_event_id: str
    state: str
    preview_hash: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TaskRevisionPreview:
    format_version: str
    revision_request_id: str
    base_request_id: str
    base_task_settings_hash: str
    replacement_request_id: str
    replacement_request_hash: str
    messages: tuple[TaskMessage, ...]
    preview_hash: str

    def _payload(self) -> dict[str, object]:
        return {
            "base_request_id": self.base_request_id,
            "base_task_settings_hash": self.base_task_settings_hash,
            "format_version": self.format_version,
            "messages": [
                {
                    "created_at": _format_time(message.created_at),
                    "message_hash": message.message_hash,
                    "message_id": message.message_id,
                    "text": message.text,
                }
                for message in self.messages
            ],
            "replacement_request_hash": self.replacement_request_hash,
            "replacement_request_id": self.replacement_request_id,
            "revision_request_id": self.revision_request_id,
        }

    def to_json(self) -> str:
        return _canonical_json(self._payload())


def _utc(value: datetime, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise TaskRevisionError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _revision_from_row(row: sqlite3.Row) -> TaskRevisionRequest:
    try:
        revision = TaskRevisionRequest(
            revision_request_id=str(row["revision_request_id"]),
            request_id=str(row["request_id"]),
            base_task_settings_hash=str(row["base_task_settings_hash"]),
            replacement_request_id=(
                None
                if row["replacement_request_id"] is None
                else str(row["replacement_request_id"])
            ),
            source_event_id=str(row["source_event_id"]),
            state=str(row["state"]),
            preview_hash=(
                None if row["preview_hash"] is None else str(row["preview_hash"])
            ),
            created_at=_parse_time(row["created_at"], "revision created_at"),
            updated_at=_parse_time(row["updated_at"], "revision updated_at"),
        )
    except (IndexError, KeyError, TypeError) as error:
        raise TaskRevisionError("stored revision request is invalid") from error
    if (
        not revision.revision_request_id
        or not revision.request_id
        or len(revision.base_task_settings_hash) != 64
        or revision.state not in {"requested", "confirmed", "cancelled", "resumed"}
        or revision.updated_at < revision.created_at
    ):
        raise TaskRevisionError("stored revision request is invalid")
    return revision


def task_lifecycle_is_active(
    connection: sqlite3.Connection,
    request_id: str,
    task_settings_hash: str,
) -> bool:
    """Return true only for the exact latest active or resumed lifecycle event."""

    try:
        _require_active_on_connection(connection, request_id, task_settings_hash)
    except TaskMessageError:
        return False
    return True


class TaskRevisionService:
    """Confirm DB-only revisions, then activate after external roots are bound.

    ``confirm`` intentionally performs no GitHub or Kanban write.  A caller
    stages only the replacement request in SQLite, confirms the exact preview,
    then uses ``require_replacement_write_on_connection`` before each external
    Project-root write.  ``activate_confirmed`` is the final atomic switch.
    """

    def __init__(
        self,
        database: TaskDatabase,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(database, TaskDatabase):
            raise TaskRevisionError("database must be a TaskDatabase")
        self._database = database
        self._clock = clock or (lambda: datetime.now(UTC))

    def _now(self, at: datetime | None, field_name: str) -> datetime:
        value = at if at is not None else self._clock()
        return _utc(value, field_name)

    def prepare_preview(
        self,
        revision_request_id: str,
        replacement: TaskRequestV2,
    ) -> TaskRevisionPreview:
        if not isinstance(replacement, TaskRequestV2):
            raise TaskRevisionError("replacement must be a TaskRequestV2")
        try:
            with self._database.transaction() as connection:
                revision = self._require_revision(
                    connection,
                    revision_request_id,
                    allowed_states={"requested"},
                )
                self._require_no_stop(connection, revision.request_id)
                self._require_base_changing(connection, revision)
                self._require_staged_replacement(connection, revision, replacement)
                if (
                    revision.replacement_request_id is not None
                    and revision.replacement_request_id != replacement.request_id
                ):
                    raise TaskRevisionError(
                        "revision is already bound to another linear replacement"
                    )
                messages = self._pending_messages(connection, revision)
                preview = self._preview(revision, replacement, messages)
                updated = connection.execute(
                    """
                    UPDATE task_revision_requests
                    SET replacement_request_id = ?, preview_hash = ?
                    WHERE revision_request_id = ? AND state = 'requested'
                      AND (replacement_request_id IS NULL OR replacement_request_id = ?)
                    """,
                    (
                        replacement.request_id,
                        preview.preview_hash,
                        revision.revision_request_id,
                        replacement.request_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise TaskRevisionError("revision preview changed concurrently")
                return preview
        except TaskDatabaseError as error:
            raise TaskRevisionError("revision preview transaction failed") from error

    def confirm(
        self,
        revision_request_id: str,
        replacement: TaskRequestV2,
        *,
        preview_hash: str,
        at: datetime | None = None,
    ) -> TaskRevisionRequest:
        timestamp = self._now(at, "revision confirmation time")
        try:
            with self._database.transaction() as connection:
                return self.confirm_on_connection(
                    connection,
                    revision_request_id,
                    replacement,
                    preview_hash=preview_hash,
                    at=timestamp,
                )
        except TaskDatabaseError as error:
            raise TaskRevisionError(
                "revision confirmation transaction failed"
            ) from error

    def confirm_on_connection(
        self,
        connection: sqlite3.Connection,
        revision_request_id: str,
        replacement: TaskRequestV2,
        *,
        preview_hash: str,
        at: datetime | None = None,
    ) -> TaskRevisionRequest:
        """Confirm within the caller's transaction; never opens a nested one."""

        timestamp = self._now(at, "revision confirmation time")
        revision = self._require_revision(
            connection,
            revision_request_id,
            allowed_states={"requested", "confirmed"},
        )
        if revision.state == "confirmed":
            if revision.replacement_request_id != replacement.request_id:
                raise TaskRevisionError("confirmed revision replacement changed")
            stored = self._stored_request(connection, replacement.request_id)
            if stored != replacement:
                raise TaskRevisionError("confirmed replacement request changed")
            preview = self._preview(
                revision,
                replacement,
                self._all_messages(connection, revision),
            )
            if (
                revision.preview_hash is None
                or preview_hash != revision.preview_hash
                or preview_hash != preview.preview_hash
            ):
                raise TaskRevisionError("confirmed revision preview changed")
            return revision
        self._require_no_stop(connection, revision.request_id)
        self._require_base_changing(connection, revision)
        if (
            revision.replacement_request_id != replacement.request_id
            or revision.preview_hash is None
            or preview_hash != revision.preview_hash
        ):
            raise TaskRevisionError("revision preview is stale or was not prepared")
        self._require_staged_replacement(connection, revision, replacement)
        messages = self._pending_messages(connection, revision)
        preview = self._preview(revision, replacement, messages)
        if preview_hash != preview.preview_hash:
            raise TaskRevisionError("revision preview is stale or was not prepared")
        if timestamp < revision.updated_at:
            raise TaskRevisionError("revision confirmation time moved backwards")
        updated = connection.execute(
            """
            UPDATE task_revision_requests
            SET state = 'confirmed', updated_at = ?
            WHERE revision_request_id = ? AND state = 'requested'
              AND replacement_request_id = ? AND preview_hash = ?
            """,
            (
                _format_time(timestamp),
                revision.revision_request_id,
                replacement.request_id,
                preview_hash,
            ),
        )
        if updated.rowcount != 1:
            raise TaskRevisionError("revision changed during confirmation")
        return self._require_revision(
            connection,
            revision_request_id,
            allowed_states={"confirmed"},
        )

    def require_replacement_write_on_connection(
        self,
        connection: sqlite3.Connection,
        replacement_request_id: str,
    ) -> TaskRevisionRequest:
        """Authorize one post-Confirm external write for a prepared replacement."""

        rows = connection.execute(
            """
            SELECT * FROM task_revision_requests
            WHERE replacement_request_id = ? AND state = 'confirmed'
            """,
            (replacement_request_id,),
        ).fetchall()
        if len(rows) != 1:
            raise TaskRevisionError(
                "replacement external write requires one confirmed revision"
            )
        revision = _revision_from_row(rows[0])
        self._require_no_stop(connection, revision.request_id)
        self._require_base_changing(connection, revision)
        request = self._stored_request(connection, replacement_request_id)
        self._require_staged_replacement(
            connection,
            revision,
            request,
            allow_partial_bound=True,
        )
        return revision

    def activate_confirmed(
        self,
        revision_request_id: str,
        replacement_settings: TaskSettingsV2,
        *,
        at: datetime | None = None,
    ) -> TaskSettingsV2:
        timestamp = self._now(at, "revision activation time")
        try:
            with self._database.transaction() as connection:
                return self.activate_confirmed_on_connection(
                    connection,
                    revision_request_id,
                    replacement_settings,
                    at=timestamp,
                )
        except TaskDatabaseError as error:
            raise TaskRevisionError("revision activation transaction failed") from error

    def activate_confirmed_on_connection(
        self,
        connection: sqlite3.Connection,
        revision_request_id: str,
        replacement_settings: TaskSettingsV2,
        *,
        at: datetime | None = None,
    ) -> TaskSettingsV2:
        """Atomically switch old→replaced/new→active after root read-back."""

        if not isinstance(replacement_settings, TaskSettingsV2):
            raise TaskRevisionError("replacement_settings must be TaskSettingsV2")
        timestamp = self._now(at, "revision activation time")
        revision = self._require_revision(
            connection,
            revision_request_id,
            allowed_states={"confirmed"},
        )
        if revision.replacement_request_id != replacement_settings.request_id:
            raise TaskRevisionError("replacement settings target another revision")
        replacement = self._stored_request(
            connection,
            replacement_settings.request_id,
        )
        try:
            expected = TaskSettingsV2.create(
                request=replacement,
                parent_issue_number=self._base_parent_issue(connection, revision),
            )
        except TaskSettingsV2Error as error:
            raise TaskRevisionError("replacement settings are invalid") from error
        if replacement_settings != expected:
            raise TaskRevisionError(
                "replacement settings do not match the staged request"
            )
        existing = connection.execute(
            """
            SELECT task_settings_hash, request_id, request_hash, format_version,
                   settings_json, management_repository, parent_issue_number,
                   task_owner_host, confirmed_at
            FROM task_settings_v2 WHERE request_id = ?
            """,
            (replacement.request_id,),
        ).fetchone()
        if existing is not None:
            expected_payload = json.loads(expected.to_json())
            if tuple(existing) != (
                expected.task_settings_hash,
                expected.request_id,
                expected.request_hash,
                expected.format_version,
                expected.to_json(),
                expected.management_repository,
                expected.parent_issue_number,
                expected.task_owner_host,
                expected_payload["confirmed_at"],
            ):
                raise TaskRevisionError("replacement settings changed")
            self._require_completed_activation(
                connection,
                revision,
                replacement,
                expected,
            )
            return expected

        self._require_no_stop(connection, revision.request_id)
        self._require_base_changing(connection, revision)
        self._require_staged_replacement(
            connection,
            revision,
            replacement,
            require_bound=True,
        )
        if timestamp < revision.updated_at:
            raise TaskRevisionError("revision activation time moved backwards")

        payload = json.loads(expected.to_json())
        occurred_at = _format_time(timestamp)
        connection.execute(
            """
            INSERT INTO task_settings_v2 (
                task_settings_hash, request_id, request_hash, format_version,
                settings_json, management_repository, parent_issue_number,
                task_owner_host, confirmed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                expected.task_settings_hash,
                expected.request_id,
                expected.request_hash,
                expected.format_version,
                expected.to_json(),
                expected.management_repository,
                expected.parent_issue_number,
                expected.task_owner_host,
                payload["confirmed_at"],
            ),
        )
        updated = connection.execute(
            """
            UPDATE task_projects
            SET task_settings_hash = ?, state = 'ready', updated_at = ?
            WHERE request_id = ? AND state = 'bound'
              AND task_settings_hash IS NULL AND root_card_id IS NOT NULL
            """,
            (
                expected.task_settings_hash,
                occurred_at,
                replacement.request_id,
            ),
        )
        if updated.rowcount != len(replacement.projects):
            raise TaskRevisionError("not every replacement Project became ready")
        common = _canonical_json({"task_settings_hash": expected.task_settings_hash})
        for event_type in ("settings_activated", "active"):
            self._insert_event(
                connection,
                request_id=replacement.request_id,
                task_settings_hash=expected.task_settings_hash,
                event_type=event_type,
                event_key=event_type,
                event_json=common,
                occurred_at=occurred_at,
            )
        self._insert_event(
            connection,
            request_id=replacement.request_id,
            task_settings_hash=expected.task_settings_hash,
            event_type="dispatch_ready",
            event_key="dispatch_ready",
            event_json=_canonical_json(
                {
                    "project_ids": [
                        project.project_id for project in replacement.projects
                    ],
                    "task_settings_hash": expected.task_settings_hash,
                }
            ),
            occurred_at=occurred_at,
        )
        self._insert_event(
            connection,
            request_id=revision.request_id,
            task_settings_hash=revision.base_task_settings_hash,
            event_type="replaced",
            event_key=f"replaced:{replacement.request_id}",
            event_json=_canonical_json(
                {
                    "replacement_request_id": replacement.request_id,
                    "replacement_task_settings_hash": expected.task_settings_hash,
                    "revision_request_id": revision.revision_request_id,
                }
            ),
            occurred_at=occurred_at,
        )
        connection.execute(
            """
            UPDATE task_revision_requests SET updated_at = ?
            WHERE revision_request_id = ? AND state = 'confirmed'
            """,
            (occurred_at, revision.revision_request_id),
        )
        return expected

    def _require_completed_activation(
        self,
        connection: sqlite3.Connection,
        revision: TaskRevisionRequest,
        replacement: TaskRequestV2,
        settings: TaskSettingsV2,
    ) -> None:
        if not task_lifecycle_is_active(
            connection,
            replacement.request_id,
            settings.task_settings_hash,
        ):
            raise TaskRevisionError("replacement settings are not exactly active")
        latest = _latest_lifecycle_event(connection, revision.request_id)
        if (
            latest is None
            or latest[1] != "replaced"
            or latest[2] != revision.base_task_settings_hash
        ):
            raise TaskRevisionError("base Task is not exactly replaced")
        replaced = connection.execute(
            """
            SELECT event_key, event_json, project_id
            FROM task_events WHERE event_id = ?
            """,
            (latest[0],),
        ).fetchone()
        expected_replaced = (
            f"replaced:{replacement.request_id}",
            _canonical_json(
                {
                    "replacement_request_id": replacement.request_id,
                    "replacement_task_settings_hash": settings.task_settings_hash,
                    "revision_request_id": revision.revision_request_id,
                }
            ),
            None,
        )
        if replaced is None or tuple(replaced) != expected_replaced:
            raise TaskRevisionError("base replacement event changed")
        raw = json.loads(replacement.to_json())
        expected_projects = {
            project.project_id: _canonical_json(payload)
            for project, payload in zip(
                replacement.projects,
                raw["projects"],
                strict=True,
            )
        }
        rows = connection.execute(
            """
            SELECT project_id, project_json, state, root_card_id,
                   task_settings_hash
            FROM task_projects WHERE request_id = ? ORDER BY project_id
            """,
            (replacement.request_id,),
        ).fetchall()
        if len(rows) != len(expected_projects):
            raise TaskRevisionError("active replacement Project set changed")
        root_ids: list[str] = []
        for row in rows:
            if (
                row[0] not in expected_projects
                or row[1] != expected_projects[row[0]]
                or row[2]
                not in {
                    "ready",
                    "running",
                    "reviewing",
                    "waiting_for_help",
                    "failed",
                    "merged",
                }
                or not isinstance(row[3], str)
                or not row[3]
                or row[4] != settings.task_settings_hash
            ):
                raise TaskRevisionError("active replacement Project changed")
            root_ids.append(row[3])
            self._require_project_binding_event(
                connection,
                replacement.request_id,
                str(row[0]),
                str(row[3]),
            )
        if len(root_ids) != len(set(root_ids)):
            raise TaskRevisionError("active replacement Project roots are not unique")
        parent = connection.execute(
            """
            SELECT event_json FROM task_events
            WHERE request_id = ? AND event_type = 'parent_issue_bound'
            """,
            (replacement.request_id,),
        ).fetchall()
        expected_parent = _canonical_json(
            {
                "parent_issue_number": settings.parent_issue_number,
                "request_hash": replacement.request_hash,
            }
        )
        if len(parent) != 1 or parent[0][0] != expected_parent:
            raise TaskRevisionError("active replacement parent binding changed")
        common = _canonical_json({"task_settings_hash": settings.task_settings_hash})
        activation = connection.execute(
            """
            SELECT event_type, task_settings_hash, project_id, event_json
            FROM task_events
            WHERE request_id = ? AND event_type IN (
                'settings_activated', 'active', 'dispatch_ready'
            )
            ORDER BY event_id
            """,
            (replacement.request_id,),
        ).fetchall()
        expected_activation = (
            ("settings_activated", settings.task_settings_hash, None, common),
            ("active", settings.task_settings_hash, None, common),
            (
                "dispatch_ready",
                settings.task_settings_hash,
                None,
                _canonical_json(
                    {
                        "project_ids": [
                            project.project_id for project in replacement.projects
                        ],
                        "task_settings_hash": settings.task_settings_hash,
                    }
                ),
            ),
        )
        if tuple(tuple(row) for row in activation) != expected_activation:
            raise TaskRevisionError("active replacement events changed")

    def cancel(
        self,
        revision_request_id: str,
        *,
        reason: str,
        at: datetime | None = None,
    ) -> TaskRevisionRequest:
        timestamp = self._now(at, "revision cancellation time")
        try:
            with self._database.transaction() as connection:
                return self.cancel_on_connection(
                    connection,
                    revision_request_id,
                    reason=reason,
                    at=timestamp,
                )
        except TaskDatabaseError as error:
            raise TaskRevisionError(
                "revision cancellation transaction failed"
            ) from error

    def cancel_on_connection(
        self,
        connection: sqlite3.Connection,
        revision_request_id: str,
        *,
        reason: str,
        at: datetime | None = None,
    ) -> TaskRevisionRequest:
        timestamp = self._now(at, "revision cancellation time")
        if not isinstance(reason, str) or not reason.strip():
            raise TaskRevisionError("revision cancellation reason is required")
        revision = self._require_revision(
            connection,
            revision_request_id,
            allowed_states={"requested", "confirmed", "cancelled"},
        )
        self._require_no_stop(connection, revision.request_id)
        if revision.state == "cancelled":
            return revision
        self._require_base_changing(connection, revision)
        if timestamp < revision.updated_at:
            raise TaskRevisionError("revision cancellation time moved backwards")
        occurred_at = _format_time(timestamp)
        self._reject_pending_messages(connection, revision, reason, timestamp)
        self._cancel_replacement(connection, revision, reason, occurred_at)
        updated = connection.execute(
            """
            UPDATE task_revision_requests
            SET state = 'cancelled', updated_at = ?
            WHERE revision_request_id = ? AND state IN ('requested', 'confirmed')
            """,
            (occurred_at, revision.revision_request_id),
        )
        if updated.rowcount != 1:
            raise TaskRevisionError("revision changed during cancellation")
        self._insert_event(
            connection,
            request_id=revision.request_id,
            task_settings_hash=revision.base_task_settings_hash,
            event_type="revision_cancelled",
            event_key=f"revision_cancelled:{revision.revision_request_id}",
            event_json=_canonical_json(
                {
                    "reason": reason,
                    "revision_request_id": revision.revision_request_id,
                }
            ),
            occurred_at=occurred_at,
        )
        return self._require_revision(
            connection,
            revision_request_id,
            allowed_states={"cancelled"},
        )

    def cancel_for_stop_on_connection(
        self,
        connection: sqlite3.Connection,
        revision_request_id: str,
        *,
        reason: str,
        at: datetime | None = None,
    ) -> TaskRevisionRequest:
        """Cancel pending revision data without appending an event after Stop."""

        timestamp = self._now(at, "Stop revision cancellation time")
        if not isinstance(reason, str) or not reason.strip():
            raise TaskRevisionError("Stop revision cancellation reason is required")
        revision = self._require_revision(
            connection,
            revision_request_id,
            allowed_states={"requested", "confirmed", "cancelled"},
        )
        if revision.state == "cancelled":
            return revision
        if timestamp < revision.updated_at:
            raise TaskRevisionError("Stop revision cancellation time moved backwards")
        occurred_at = _format_time(timestamp)
        self._reject_pending_messages(connection, revision, reason, timestamp)
        self._cancel_replacement(connection, revision, reason, occurred_at)
        connection.execute(
            """
            UPDATE task_revision_requests
            SET state = 'cancelled', updated_at = ?
            WHERE revision_request_id = ? AND state IN ('requested', 'confirmed')
            """,
            (occurred_at, revision.revision_request_id),
        )
        return self._require_revision(
            connection,
            revision_request_id,
            allowed_states={"cancelled"},
        )

    def resume(
        self,
        revision_request_id: str,
        *,
        at: datetime | None = None,
    ) -> TaskRevisionRequest:
        timestamp = self._now(at, "revision resume time")
        try:
            with self._database.transaction() as connection:
                return self.resume_on_connection(
                    connection,
                    revision_request_id,
                    at=timestamp,
                )
        except TaskDatabaseError as error:
            raise TaskRevisionError("revision resume transaction failed") from error

    def resume_on_connection(
        self,
        connection: sqlite3.Connection,
        revision_request_id: str,
        *,
        at: datetime | None = None,
    ) -> TaskRevisionRequest:
        timestamp = self._now(at, "revision resume time")
        revision = self._require_revision(
            connection,
            revision_request_id,
            allowed_states={"requested", "confirmed", "cancelled", "resumed"},
        )
        self._require_no_stop(connection, revision.request_id)
        if revision.state == "resumed":
            return revision
        if revision.state != "cancelled":
            raise TaskRevisionError(
                f"revision state {revision.state} does not permit Resume"
            )
        if timestamp < revision.updated_at:
            raise TaskRevisionError("revision Resume time moved backwards")
        latest = _latest_lifecycle_event(connection, revision.request_id)
        if latest is None or latest[1] != "revision_cancelled":
            raise TaskRevisionError("revision is not awaiting Resume")
        occurred_at = _format_time(timestamp)
        updated = connection.execute(
            """
            UPDATE task_revision_requests SET state = 'resumed', updated_at = ?
            WHERE revision_request_id = ? AND state = 'cancelled'
            """,
            (occurred_at, revision.revision_request_id),
        )
        if updated.rowcount != 1:
            raise TaskRevisionError("revision changed during Resume")
        self._insert_event(
            connection,
            request_id=revision.request_id,
            task_settings_hash=revision.base_task_settings_hash,
            event_type="revision_resumed",
            event_key=f"revision_resumed:{revision.revision_request_id}",
            event_json=_canonical_json(
                {"revision_request_id": revision.revision_request_id}
            ),
            occurred_at=occurred_at,
        )
        return self._require_revision(
            connection,
            revision_request_id,
            allowed_states={"resumed"},
        )

    def require_active(self, request_id: str, task_settings_hash: str) -> None:
        with self._database.read() as connection:
            try:
                _require_active_on_connection(
                    connection,
                    request_id,
                    task_settings_hash,
                )
            except TaskMessageError as error:
                raise TaskRevisionError(str(error)) from error

    @staticmethod
    def _require_revision(
        connection: sqlite3.Connection,
        revision_request_id: str,
        *,
        allowed_states: set[str],
    ) -> TaskRevisionRequest:
        row = connection.execute(
            "SELECT * FROM task_revision_requests WHERE revision_request_id = ?",
            (revision_request_id,),
        ).fetchone()
        if row is None:
            raise TaskRevisionError("revision request does not exist")
        revision = _revision_from_row(row)
        if revision.state not in allowed_states:
            raise TaskRevisionError(
                f"revision state {revision.state} does not permit this operation"
            )
        return revision

    @staticmethod
    def _require_no_stop(
        connection: sqlite3.Connection,
        request_id: str,
    ) -> None:
        stopped = connection.execute(
            """
            SELECT event_type FROM task_events
            WHERE request_id = ? AND event_type IN (
                'stop_requested', 'stopping', 'cancelled', 'expired',
                'merged', 'replaced', 'partially_merged'
            )
            ORDER BY event_id DESC LIMIT 1
            """,
            (request_id,),
        ).fetchone()
        if stopped is not None:
            raise TaskRevisionError(
                f"Stop or terminal barrier {stopped[0]} blocks revision changes"
            )

    @staticmethod
    def _require_base_changing(
        connection: sqlite3.Connection,
        revision: TaskRevisionRequest,
    ) -> None:
        latest = _latest_lifecycle_event(connection, revision.request_id)
        if (
            latest is None
            or latest[1] not in {"revision_requested", "changing"}
            or latest[2] != revision.base_task_settings_hash
        ):
            raise TaskRevisionError("base Task is not changing for this revision")
        active = connection.execute(
            """
            SELECT task_settings_hash FROM task_settings_v2
            WHERE request_id = ?
            """,
            (revision.request_id,),
        ).fetchall()
        if len(active) != 1 or active[0][0] != revision.base_task_settings_hash:
            raise TaskRevisionError("base Task current settings changed")
        pending = connection.execute(
            """
            SELECT revision_request_id FROM task_revision_requests
            WHERE request_id = ? AND state IN ('requested', 'confirmed')
            """,
            (revision.request_id,),
        ).fetchall()
        if len(pending) != 1 or pending[0][0] != revision.revision_request_id:
            raise TaskRevisionError("base Task has an ambiguous pending revision")

    def _require_staged_replacement(
        self,
        connection: sqlite3.Connection,
        revision: TaskRevisionRequest,
        replacement: TaskRequestV2,
        *,
        require_bound: bool = False,
        allow_partial_bound: bool = False,
    ) -> None:
        if replacement.replaces_request_id != revision.request_id:
            raise TaskRevisionError("replacement does not continue the revision base")
        base = self._stored_request(connection, revision.request_id)
        if (
            replacement.management_repository != base.management_repository
            or replacement.task_owner_host != base.task_owner_host
        ):
            raise TaskRevisionError("replacement changed Management or owner host")
        stored = self._stored_request(connection, replacement.request_id)
        if stored != replacement:
            raise TaskRevisionError("staged replacement request changed")
        self._require_linear_chain(connection, revision, replacement)
        request_event = connection.execute(
            """
            SELECT event_json FROM task_events
            WHERE request_id = ? AND event_type = 'request_prepared'
            """,
            (replacement.request_id,),
        ).fetchall()
        if len(request_event) != 1:
            raise TaskRevisionError("replacement is not exact staged prepared data")
        self._require_request_prepared_event(
            connection,
            replacement,
            str(request_event[0][0]),
        )
        raw = json.loads(replacement.to_json())
        expected_projects = {
            project.project_id: _canonical_json(payload)
            for project, payload in zip(
                replacement.projects,
                raw["projects"],
                strict=True,
            )
        }
        rows = connection.execute(
            """
            SELECT project_id, project_json, state, root_card_id,
                   task_settings_hash
            FROM task_projects WHERE request_id = ? ORDER BY project_id
            """,
            (replacement.request_id,),
        ).fetchall()
        if len(rows) != len(expected_projects):
            raise TaskRevisionError("staged replacement Project set changed")
        root_ids: list[str] = []
        for row in rows:
            if row[0] not in expected_projects or row[1] != expected_projects[row[0]]:
                raise TaskRevisionError("staged replacement Project changed")
            if require_bound:
                if (
                    row[2] != "bound"
                    or not isinstance(row[3], str)
                    or not row[3].strip()
                    or row[4] is not None
                ):
                    raise TaskRevisionError("replacement Project root is not bound")
                root_ids.append(row[3])
                self._require_project_binding_event(
                    connection,
                    replacement.request_id,
                    str(row[0]),
                    str(row[3]),
                )
            elif allow_partial_bound and row[2] == "bound":
                if (
                    not isinstance(row[3], str)
                    or not row[3].strip()
                    or row[4] is not None
                ):
                    raise TaskRevisionError("partial replacement root binding changed")
                root_ids.append(row[3])
                self._require_project_binding_event(
                    connection,
                    replacement.request_id,
                    str(row[0]),
                    str(row[3]),
                )
            elif row[2] != "prepared" or row[3] is not None or row[4] is not None:
                raise TaskRevisionError(
                    "replacement is not staged without external roots"
                )
        if require_bound:
            if len(root_ids) != len(set(root_ids)):
                raise TaskRevisionError("replacement Project roots are not unique")
            parent = connection.execute(
                """
                SELECT event_json FROM task_events
                WHERE request_id = ? AND event_type = 'parent_issue_bound'
                """,
                (replacement.request_id,),
            ).fetchall()
            expected_parent = _canonical_json(
                {
                    "parent_issue_number": self._base_parent_issue(
                        connection,
                        revision,
                    ),
                    "request_hash": replacement.request_hash,
                }
            )
            if len(parent) != 1 or parent[0][0] != expected_parent:
                raise TaskRevisionError("replacement parent binding changed")
        elif allow_partial_bound and len(root_ids) != len(set(root_ids)):
            raise TaskRevisionError("partial replacement Project roots are not unique")
        if (
            connection.execute(
                "SELECT 1 FROM task_settings_v2 WHERE request_id = ?",
                (replacement.request_id,),
            ).fetchone()
            is not None
        ):
            raise TaskRevisionError("staged replacement is already active")

    @staticmethod
    def _require_linear_chain(
        connection: sqlite3.Connection,
        revision: TaskRevisionRequest,
        replacement: TaskRequestV2,
    ) -> None:
        children = connection.execute(
            """
            SELECT child.request_id FROM task_requests AS child
            WHERE child.replaces_request_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM task_events AS cancelled_child
                  WHERE cancelled_child.request_id = child.request_id
                    AND cancelled_child.event_type = 'cancelled'
              )
            ORDER BY child.request_id
            """,
            (revision.request_id,),
        ).fetchall()
        if len(children) != 1 or children[0][0] != replacement.request_id:
            raise TaskRevisionError(
                "replacement chain would fork instead of remaining linear"
            )
        visited = {replacement.request_id}
        current = revision.request_id
        while current is not None:
            if current in visited:
                raise TaskRevisionError("replacement chain contains a cycle")
            visited.add(current)
            row = connection.execute(
                "SELECT replaces_request_id FROM task_requests WHERE request_id = ?",
                (current,),
            ).fetchone()
            if row is None:
                raise TaskRevisionError("replacement chain contains a missing request")
            current = row[0]

    @staticmethod
    def _stored_request(
        connection: sqlite3.Connection,
        request_id: str,
    ) -> TaskRequestV2:
        row = connection.execute(
            """
            SELECT request_json, request_id, request_hash, management_repository,
                   task_owner_host, confirmed_by, confirmed_at,
                   replaces_request_id
            FROM task_requests WHERE request_id = ?
            """,
            (request_id,),
        ).fetchone()
        if row is None:
            raise TaskRevisionError("replacement is not staged prepared data")
        try:
            request = TaskRequestV2.from_json(row[0])
        except TaskSettingsV2Error as error:
            raise TaskRevisionError("stored replacement request is invalid") from error
        payload = json.loads(request.to_json())
        if tuple(row[1:]) != (
            request.request_id,
            request.request_hash,
            request.management_repository,
            request.task_owner_host,
            request.confirmed_by,
            payload["confirmed_at"],
            request.replaces_request_id,
        ):
            raise TaskRevisionError("stored replacement columns changed")
        return request

    @staticmethod
    def _require_request_prepared_event(
        connection: sqlite3.Connection,
        replacement: TaskRequestV2,
        raw_event: str,
    ) -> None:
        try:
            payload = json.loads(raw_event)
        except (json.JSONDecodeError, RecursionError):
            raise TaskRevisionError(
                "replacement request_prepared event is invalid"
            ) from None
        if not isinstance(payload, dict) or _canonical_json(payload) != raw_event:
            raise TaskRevisionError("replacement request_prepared event is invalid")
        if payload == {"request_hash": replacement.request_hash}:
            return
        if set(payload) != {
            "request_hash",
            "source_event_id",
            "source_payload_hash",
        } or payload.get("request_hash") != replacement.request_hash:
            raise TaskRevisionError("replacement request_prepared event changed")
        source_event_id = payload.get("source_event_id")
        source_payload_hash = payload.get("source_payload_hash")
        if (
            not isinstance(source_event_id, str)
            or not source_event_id
            or not isinstance(source_payload_hash, str)
            or len(source_payload_hash) != 64
            or any(character not in "0123456789abcdef" for character in source_payload_hash)
        ):
            raise TaskRevisionError("replacement creation receipt is invalid")
        source = connection.execute(
            """
            SELECT subject_id, payload_hash, state FROM surface_events
            WHERE source_event_id = ?
            """,
            (source_event_id,),
        ).fetchone()
        if (
            source is None
            or source[0] != replacement.confirmed_by
            or source[1] != source_payload_hash
            or source[2] not in {"received", "handled", "responded"}
        ):
            raise TaskRevisionError("replacement creation receipt changed")

    def _pending_messages(
        self,
        connection: sqlite3.Connection,
        revision: TaskRevisionRequest,
    ) -> tuple[TaskMessage, ...]:
        rows = TaskMessageStore._pending_rows_for_revision(
            connection,
            request_id=revision.request_id,
            revision_created_at=revision.created_at,
            revision_updated_at=revision.updated_at,
        )
        messages = tuple(_message_from_row(row) for row in rows)
        if not messages:
            raise TaskRevisionError("revision has no exact pending messages")
        return messages

    @staticmethod
    def _all_messages(
        connection: sqlite3.Connection,
        revision: TaskRevisionRequest,
    ) -> tuple[TaskMessage, ...]:
        rows = connection.execute(
            """
            SELECT * FROM task_messages
            WHERE request_id = ? AND created_at >= ? AND created_at <= ?
            ORDER BY created_at, message_id
            """,
            (
                revision.request_id,
                _format_time(revision.created_at),
                _format_time(revision.updated_at),
            ),
        ).fetchall()
        messages = tuple(_message_from_row(row) for row in rows)
        if not messages:
            raise TaskRevisionError("revision has no immutable messages")
        return messages

    @staticmethod
    def _preview(
        revision: TaskRevisionRequest,
        replacement: TaskRequestV2,
        messages: tuple[TaskMessage, ...],
    ) -> TaskRevisionPreview:
        base = {
            "base_request_id": revision.request_id,
            "base_task_settings_hash": revision.base_task_settings_hash,
            "format_version": TASK_REVISION_PREVIEW_FORMAT,
            "messages": [
                {
                    "created_at": _format_time(message.created_at),
                    "message_hash": message.message_hash,
                    "message_id": message.message_id,
                    "text": message.text,
                }
                for message in messages
            ],
            "replacement_request_hash": replacement.request_hash,
            "replacement_request_id": replacement.request_id,
            "revision_request_id": revision.revision_request_id,
        }
        rendered = _canonical_json(base)
        return TaskRevisionPreview(
            format_version=TASK_REVISION_PREVIEW_FORMAT,
            revision_request_id=revision.revision_request_id,
            base_request_id=revision.request_id,
            base_task_settings_hash=revision.base_task_settings_hash,
            replacement_request_id=replacement.request_id,
            replacement_request_hash=replacement.request_hash,
            messages=messages,
            preview_hash=hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        )

    @staticmethod
    def _base_parent_issue(
        connection: sqlite3.Connection,
        revision: TaskRevisionRequest,
    ) -> int:
        row = connection.execute(
            """
            SELECT parent_issue_number FROM task_settings_v2
            WHERE request_id = ? AND task_settings_hash = ?
            """,
            (revision.request_id, revision.base_task_settings_hash),
        ).fetchone()
        if row is None or not isinstance(row[0], int) or row[0] <= 0:
            raise TaskRevisionError("base parent Issue binding changed")
        return int(row[0])

    @staticmethod
    def _require_project_binding_event(
        connection: sqlite3.Connection,
        request_id: str,
        project_id: str,
        root_card_id: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT event_json FROM task_events
            WHERE request_id = ? AND project_id = ?
              AND event_type = 'project_item_bound'
            """,
            (request_id, project_id),
        ).fetchall()
        expected = _canonical_json(
            {
                "idempotency_key": f"forge-task-v2:{request_id}:{project_id}:build",
                "root_card_id": root_card_id,
            }
        )
        if len(row) != 1 or row[0][0] != expected:
            raise TaskRevisionError("replacement Project root binding changed")

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        request_id: str,
        task_settings_hash: str | None,
        event_type: str,
        event_key: str,
        event_json: str,
        occurred_at: str,
        project_id: str | None = None,
    ) -> None:
        existing = connection.execute(
            """
            SELECT task_settings_hash, project_id, event_type, event_json
            FROM task_events WHERE request_id = ? AND event_key = ?
            """,
            (request_id, event_key),
        ).fetchone()
        expected = (task_settings_hash, project_id, event_type, event_json)
        if existing is None:
            connection.execute(
                """
                INSERT INTO task_events (
                    request_id, task_settings_hash, project_id, event_type,
                    event_key, event_json, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    task_settings_hash,
                    project_id,
                    event_type,
                    event_key,
                    event_json,
                    occurred_at,
                ),
            )
        elif tuple(existing) != expected:
            raise TaskRevisionError(f"Task lifecycle event {event_key} changed")

    def _reject_pending_messages(
        self,
        connection: sqlite3.Connection,
        revision: TaskRevisionRequest,
        reason: str,
        timestamp: datetime,
    ) -> None:
        for message in self._pending_messages(connection, revision):
            TaskMessageStore._ensure_message_event(
                connection,
                message_id=message.message_id,
                task_settings_hash=revision.base_task_settings_hash,
                worker_task_id=None,
                run_id=None,
                event_type="rejected",
                reason=reason,
                occurred_at=timestamp,
            )

    def _cancel_replacement(
        self,
        connection: sqlite3.Connection,
        revision: TaskRevisionRequest,
        reason: str,
        occurred_at: str,
    ) -> None:
        replacement_id = revision.replacement_request_id
        if replacement_id is None:
            return
        if (
            connection.execute(
                "SELECT 1 FROM task_settings_v2 WHERE request_id = ?",
                (replacement_id,),
            ).fetchone()
            is not None
        ):
            raise TaskRevisionError(
                "active replacement cannot be cancelled as staged data"
            )
        connection.execute(
            """
            UPDATE task_projects SET state = 'cancelled', updated_at = ?
            WHERE request_id = ? AND state IN ('prepared', 'bound')
              AND task_settings_hash IS NULL
            """,
            (occurred_at, replacement_id),
        )
        self._insert_event(
            connection,
            request_id=replacement_id,
            task_settings_hash=None,
            event_type="cancelled",
            event_key=f"cancelled:revision:{revision.revision_request_id}",
            event_json=_canonical_json(
                {
                    "reason": reason,
                    "revision_request_id": revision.revision_request_id,
                }
            ),
            occurred_at=occurred_at,
        )


__all__ = [
    "TASK_REVISION_PREVIEW_FORMAT",
    "TaskRevisionError",
    "TaskRevisionPreview",
    "TaskRevisionRequest",
    "TaskRevisionService",
    "task_lifecycle_is_active",
]
