"""Read and set the durable v2 Task Stop barrier."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Iterable
from uuid import NAMESPACE_URL, UUID, uuid5

from .surface_events import TrustedTurnContext
from .task_database import TaskDatabase, TaskDatabaseError
from .task_settings_v2 import (
    TaskRequestV2,
    TaskSettingsV2,
    TaskSettingsV2Error,
)
from .task_revisions import TaskRevisionError, TaskRevisionService


_TERMINAL_EVENTS = frozenset(
    {"cancelled", "expired", "merged", "replaced", "partially_merged"}
)
_STOP_EVENTS = frozenset({"stop_requested", "stopping"})
_STOPPABLE_STATES = frozenset(
    {"prepared", "bound", "active", "changing", "stopping"}
)


class TaskStopError(RuntimeError):
    """Raised when a Task cannot be stopped without weakening a guard."""


class TaskStopAccessDenied(TaskStopError):
    """Raised without disclosing Task metadata to an unauthorized subject."""


class TaskStopOwnerHostMismatch(TaskStopError):
    """Raised only after access succeeds and the owner host differs."""

    def __init__(self, owner_host: str) -> None:
        super().__init__(f"Task belongs to owner host {owner_host}")
        self.owner_host = owner_host


class TaskStopUnsupported(TaskStopError):
    """Raised for a v1 Task that lacks the v2 identity and access boundary."""


@dataclass(frozen=True, slots=True)
class StoppableTask:
    """One parent Task aggregate that may receive a Stop barrier."""

    request_id: str
    management_repository: str
    parent_issue_number: int | None
    task_owner_host: str
    task_settings_hash: str | None
    state: str
    title: str

    def __post_init__(self) -> None:
        _canonical_uuid(self.request_id, "request_id")
        _canonical_uuid(self.task_owner_host, "task_owner_host")
        if self.state not in _STOPPABLE_STATES:
            raise TaskStopError("Task state is not stoppable")


@dataclass(frozen=True, slots=True)
class StopReceipt:
    """Committed receipt for one durable Stop request."""

    stop_request_id: str
    request_id: str
    management_repository: str
    parent_issue_number: int | None
    task_settings_hash: str | None
    state: str


@dataclass(frozen=True, slots=True)
class _RequestRecord:
    request: TaskRequestV2
    parent_issue_number: int | None
    settings: TaskSettingsV2 | None
    event_types: tuple[str, ...]
    terminal_event: str | None


@dataclass(frozen=True, slots=True)
class _Aggregate:
    task: StoppableTask
    request_ids: frozenset[str]
    pending_replacement_ids: frozenset[str]


class TaskStopService:
    """Resolve accessible parent aggregates and commit the first Stop barrier."""

    def __init__(self, database: TaskDatabase) -> None:
        if not isinstance(database, TaskDatabase):
            raise TaskStopError("database must be TaskDatabase")
        self._database = database

    def get_stoppable(
        self,
        context: TrustedTurnContext,
        issue_number: int | None = None,
    ) -> tuple[StoppableTask, ...]:
        """Return candidates without opening a write transaction."""

        _require_context(context)
        _require_optional_issue_number(issue_number)
        try:
            with self._database.read() as connection:
                aggregates = self._accessible_aggregates(connection, context)
                if issue_number is not None:
                    aggregates = tuple(
                        aggregate
                        for aggregate in aggregates
                        if aggregate.task.parent_issue_number == issue_number
                    )
                    if not aggregates:
                        self._raise_missing_explicit_task(connection, issue_number)
                else:
                    bound_ids = {
                        str(row[0])
                        for row in connection.execute(
                            """
                            SELECT request_id
                            FROM task_session_bindings
                            WHERE surface = ? AND subject_id = ? AND session_id = ?
                            """,
                            (context.surface, context.subject_id, context.session_id),
                        )
                    }
                    session_aggregates = tuple(
                        aggregate
                        for aggregate in aggregates
                        if aggregate.request_ids & bound_ids
                    )
                    if session_aggregates:
                        aggregates = session_aggregates
                self._require_owner_host(aggregates, context)
                return tuple(aggregate.task for aggregate in aggregates)
        except TaskStopError:
            raise
        except TaskDatabaseError as error:
            raise TaskStopError("Task Stop candidates could not be read") from error

    def request_stop(
        self,
        request_id: str,
        context: TrustedTurnContext,
        *,
        payload_hash: str,
        at: datetime | None = None,
    ) -> StopReceipt:
        """Commit the durable Stop request and every local lifecycle barrier."""

        request_id = _canonical_uuid(request_id, "request_id")
        _require_context(context)
        _require_hash(payload_hash, "payload_hash")
        occurred_at = _timestamp(at if at is not None else datetime.now(UTC))
        try:
            with self._database.transaction() as connection:
                aggregates = self._accessible_aggregates(connection, context)
                aggregate = next(
                    (
                        item
                        for item in aggregates
                        if request_id in item.request_ids
                        or request_id == item.task.request_id
                    ),
                    None,
                )
                if aggregate is None:
                    raise TaskStopAccessDenied(
                        "Task is unavailable or access is denied"
                    )
                self._require_owner_host((aggregate,), context)
                self._require_source_event(
                    connection,
                    context,
                    payload_hash=payload_hash,
                )
                source_row = connection.execute(
                    """
                    SELECT stop_request_id, request_id
                    FROM task_stop_requests
                    WHERE source_event_id = ?
                    """,
                    (context.source_event_id,),
                ).fetchone()
                if source_row is not None:
                    if str(source_row[1]) != aggregate.task.request_id:
                        raise TaskStopError(
                            "source event is already bound to another Task Stop"
                        )
                    return self._receipt(connection, str(source_row[0]), aggregate)

                existing = self._existing_stop(connection, aggregate.request_ids)
                if existing is not None:
                    return self._receipt(connection, existing, aggregate)

                stop_request_id = str(
                    uuid5(
                        NAMESPACE_URL,
                        "forge-task-stop/v1\0"
                        f"{aggregate.task.request_id}\0{context.source_event_id}",
                    )
                )
                details = _json(
                    {
                        "management_repository": aggregate.task.management_repository,
                        "parent_issue_number": aggregate.task.parent_issue_number,
                        "request_ids": sorted(aggregate.request_ids),
                    }
                )
                connection.execute(
                    """
                    INSERT INTO task_stop_requests (
                        stop_request_id, request_id, task_settings_hash,
                        source_event_id, state, result, details_json,
                        requested_at, updated_at, completed_at
                    ) VALUES (?, ?, ?, ?, 'requested', NULL, ?, ?, ?, NULL)
                    """,
                    (
                        stop_request_id,
                        aggregate.task.request_id,
                        aggregate.task.task_settings_hash,
                        context.source_event_id,
                        details,
                        occurred_at,
                        occurred_at,
                    ),
                )
                # RISK(race): this transaction owns the same TaskDatabase write
                # permit as revision, dispatch, GitHub, and merge state writers.
                # No new mutable work may cross the first stop_requested event.
                for member_id in sorted(aggregate.request_ids):
                    self._ensure_task_event(
                        connection,
                        request_id=member_id,
                        settings_hash=self._settings_hash_for(connection, member_id),
                        event_type="stop_requested",
                        event_key=f"stop_requested:{stop_request_id}",
                        payload={"stop_request_id": stop_request_id},
                        occurred_at=occurred_at,
                    )
                self._cancel_pending_updates(
                    connection,
                    aggregate,
                    occurred_at=occurred_at,
                )
                self._ensure_task_event(
                    connection,
                    request_id=aggregate.task.request_id,
                    settings_hash=aggregate.task.task_settings_hash,
                    event_type="stopping",
                    event_key=f"stopping:{stop_request_id}",
                    payload={"stop_request_id": stop_request_id},
                    occurred_at=occurred_at,
                )
                updated = connection.execute(
                    """
                    UPDATE task_stop_requests
                    SET state = 'stopping', updated_at = ?
                    WHERE stop_request_id = ? AND state = 'requested'
                    """,
                    (occurred_at, stop_request_id),
                )
                if updated.rowcount != 1:
                    raise TaskStopError("Task Stop state transition failed")
                return self._receipt(connection, stop_request_id, aggregate)
        except TaskStopError:
            raise
        except TaskDatabaseError as error:
            raise TaskStopError("Task Stop transaction failed") from error
        except sqlite3.Error as error:
            raise TaskStopError("Task Stop transaction failed") from error

    def resolve_stoppable(
        self,
        request_id: str,
        context: TrustedTurnContext,
    ) -> StoppableTask:
        """Reauthorize one chooser selection before recording its source event."""

        request_id = _canonical_uuid(request_id, "request_id")
        _require_context(context)
        try:
            with self._database.read() as connection:
                aggregate = next(
                    (
                        item
                        for item in self._accessible_aggregates(connection, context)
                        if request_id in item.request_ids
                        or request_id == item.task.request_id
                    ),
                    None,
                )
                if aggregate is None:
                    raise TaskStopAccessDenied(
                        "Task is unavailable or access is denied"
                    )
                self._require_owner_host((aggregate,), context)
                return aggregate.task
        except TaskStopError:
            raise
        except TaskDatabaseError as error:
            raise TaskStopError("Task Stop selection could not be read") from error

    @staticmethod
    def _require_source_event(
        connection: sqlite3.Connection,
        context: TrustedTurnContext,
        *,
        payload_hash: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT subject_id, session_id, surface, payload_hash, state
            FROM surface_events
            WHERE source_event_id = ?
            """,
            (context.source_event_id,),
        ).fetchone()
        if row is None:
            raise TaskStopError("source event was not durably recorded")
        if tuple(row[:4]) != (
            context.subject_id,
            context.session_id,
            context.surface,
            payload_hash,
        ):
            raise TaskStopError("source event identity or payload changed")
        if row[4] not in {"received", "handled", "responded"}:
            raise TaskStopError("source event is no longer usable")

    def _accessible_aggregates(
        self,
        connection: sqlite3.Connection,
        context: TrustedTurnContext,
    ) -> tuple[_Aggregate, ...]:
        accessible_ids = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT request_id
                FROM task_access
                WHERE surface = ? AND subject_id = ? AND revoked_at IS NULL
                """,
                (context.surface, context.subject_id),
            )
        }
        if not accessible_ids:
            return ()
        rows = connection.execute(
            """
            SELECT request_id, format_version, request_json, request_hash,
                   management_repository, task_owner_host, confirmed_by,
                   confirmed_at, replaces_request_id
            FROM task_requests
            """
        ).fetchall()
        requests: dict[str, TaskRequestV2] = {}
        raw_rows: dict[str, tuple[object, ...]] = {}
        for row in rows:
            row_tuple = tuple(row)
            request_id = str(row_tuple[0])
            raw_rows[request_id] = row_tuple
        related = _related_request_ids(raw_rows, accessible_ids)
        for request_id in related:
            row = raw_rows.get(request_id)
            if row is None:
                raise TaskStopError("Task replacement chain is incomplete")
            try:
                request = TaskRequestV2.from_json(row[2])
            except TaskSettingsV2Error as error:
                raise TaskStopError("stored Task request is invalid") from error
            payload = json.loads(request.to_json())
            expected = (
                request.request_id,
                request.format_version,
                request.to_json(),
                request.request_hash,
                request.management_repository,
                request.task_owner_host,
                request.confirmed_by,
                payload["confirmed_at"],
                request.replaces_request_id,
            )
            if row != expected:
                raise TaskStopError("stored Task request does not match exact JSON")
            requests[request_id] = request

        components = _request_components(requests)
        aggregates: list[_Aggregate] = []
        for component in components:
            if not component & accessible_ids:
                continue
            aggregate = self._load_aggregate(connection, requests, component)
            if aggregate is not None:
                aggregates.append(aggregate)
        return tuple(
            sorted(
                aggregates,
                key=lambda item: (
                    item.task.parent_issue_number is None,
                    item.task.parent_issue_number or 0,
                    item.task.request_id,
                ),
            )
        )

    def _load_aggregate(
        self,
        connection: sqlite3.Connection,
        requests: dict[str, TaskRequestV2],
        component: frozenset[str],
    ) -> _Aggregate | None:
        members = [requests[request_id] for request_id in sorted(component)]
        repositories = {request.management_repository for request in members}
        owner_hosts = {request.task_owner_host for request in members}
        if len(repositories) != 1 or len(owner_hosts) != 1:
            raise TaskStopError("Task replacement chain changed its owner binding")
        _validate_replacement_graph(members)
        records = {
            request.request_id: self._load_request_record(connection, request)
            for request in members
        }
        parent_numbers = {
            record.parent_issue_number
            for record in records.values()
            if record.parent_issue_number is not None
        }
        if len(parent_numbers) > 1:
            raise TaskStopError("Task replacement chain changed parent Issue")
        parent_issue_number = next(iter(parent_numbers), None)

        stop_id = self._existing_stop(connection, component)
        stop_barrier_seen = any(
            _STOP_EVENTS.intersection(record.event_types)
            for record in records.values()
        )
        if stop_id is None and stop_barrier_seen:
            raise TaskStopError(
                "Task Stop barrier exists without a durable Stop request"
            )
        if stop_id is not None and not stop_barrier_seen:
            raise TaskStopError(
                "durable Task Stop request is missing its lifecycle barrier"
            )
        settings_records = [
            record
            for record in records.values()
            if record.settings is not None and record.terminal_event != "replaced"
        ]
        if len(settings_records) > 1:
            raise TaskStopError("Task replacement chain has multiple current settings")
        settings_record = settings_records[0] if settings_records else None
        if stop_id is not None:
            stop_row = connection.execute(
                "SELECT request_id, task_settings_hash FROM task_stop_requests WHERE stop_request_id = ?",
                (stop_id,),
            ).fetchone()
            if stop_row is None or str(stop_row[0]) not in component:
                raise TaskStopError("Task Stop request does not match its aggregate")
            control_id = str(stop_row[0])
            settings_hash = None if stop_row[1] is None else str(stop_row[1])
            state = "stopping"
        elif settings_record is not None:
            control_id = settings_record.request.request_id
            settings_hash = settings_record.settings.task_settings_hash
            revision = connection.execute(
                """
                SELECT 1 FROM task_revision_requests
                WHERE request_id = ? AND state IN ('requested', 'confirmed')
                LIMIT 1
                """,
                (control_id,),
            ).fetchone()
            state = "changing" if revision is not None else "active"
        else:
            nonterminal = [
                record
                for record in records.values()
                if record.terminal_event is None
            ]
            if not nonterminal:
                return None
            leaves = [
                record
                for record in nonterminal
                if not any(
                    candidate.request.replaces_request_id
                    == record.request.request_id
                    and candidate.terminal_event is None
                    for candidate in nonterminal
                )
            ]
            if len(leaves) != 1:
                raise TaskStopError("Task replacement chain has no unique current request")
            current = leaves[0]
            control_id = current.request.request_id
            settings_hash = None
            state = "bound" if parent_issue_number is not None else "prepared"

        control = requests[control_id]
        pending_replacements = frozenset(
            request.request_id
            for request in members
            if request.request_id != control_id
            and records[request.request_id].terminal_event is None
            and records[request.request_id].settings is None
        )
        return _Aggregate(
            task=StoppableTask(
                request_id=control_id,
                management_repository=control.management_repository,
                parent_issue_number=parent_issue_number,
                task_owner_host=control.task_owner_host,
                task_settings_hash=settings_hash,
                state=state,
                title=control.task_content.title,
            ),
            request_ids=component,
            pending_replacement_ids=pending_replacements,
        )

    def _load_request_record(
        self,
        connection: sqlite3.Connection,
        request: TaskRequestV2,
    ) -> _RequestRecord:
        event_rows = connection.execute(
            """
            SELECT task_settings_hash, project_id, event_type, event_key,
                   event_json, occurred_at
            FROM task_events
            WHERE request_id = ?
            ORDER BY event_id
            """,
            (request.request_id,),
        ).fetchall()
        terminal_events: list[str] = []
        parent_issue_numbers: set[int] = set()
        event_types: list[str] = []
        for row in event_rows:
            event_type = str(row[2])
            event_key = str(row[3])
            if not event_key or event_key != event_key.strip():
                raise TaskStopError("stored Task event key is invalid")
            _canonical_json(str(row[4]), "Task event")
            _parse_timestamp(row[5], "Task event time")
            event_types.append(event_type)
            if event_type in _TERMINAL_EVENTS:
                terminal_events.append(event_type)
            if event_type == "parent_issue_bound":
                if event_key != "parent_issue_bound":
                    raise TaskStopError("parent Issue event key is invalid")
                payload = json.loads(str(row[4]))
                if (
                    set(payload) != {"parent_issue_number", "request_hash"}
                    or type(payload["parent_issue_number"]) is not int
                    or payload["parent_issue_number"] <= 0
                    or payload["request_hash"] != request.request_hash
                ):
                    raise TaskStopError("parent Issue event is invalid")
                parent_issue_numbers.add(payload["parent_issue_number"])
        if len(terminal_events) > 1:
            raise TaskStopError("Task request has multiple terminal events")
        if len(parent_issue_numbers) > 1:
            raise TaskStopError("Task request has multiple parent Issues")

        settings_rows = connection.execute(
            """
            SELECT task_settings_hash, request_id, request_hash,
                   format_version, settings_json, management_repository,
                   parent_issue_number, task_owner_host, confirmed_at
            FROM task_settings_v2
            WHERE request_id = ?
            """,
            (request.request_id,),
        ).fetchall()
        if len(settings_rows) > 1:
            raise TaskStopError("Task request has duplicate settings")
        settings: TaskSettingsV2 | None = None
        if settings_rows:
            row = tuple(settings_rows[0])
            try:
                settings = TaskSettingsV2.from_json(row[4], request=request)
            except TaskSettingsV2Error as error:
                raise TaskStopError("stored Task settings are invalid") from error
            payload = json.loads(settings.to_json())
            expected = (
                settings.task_settings_hash,
                settings.request_id,
                settings.request_hash,
                settings.format_version,
                settings.to_json(),
                settings.management_repository,
                settings.parent_issue_number,
                settings.task_owner_host,
                payload["confirmed_at"],
            )
            if row != expected:
                raise TaskStopError("stored Task settings do not match exact JSON")
            parent_issue_numbers.add(settings.parent_issue_number)
        if len(parent_issue_numbers) > 1:
            raise TaskStopError("Task settings and parent Issue binding differ")
        return _RequestRecord(
            request=request,
            parent_issue_number=next(iter(parent_issue_numbers), None),
            settings=settings,
            event_types=tuple(event_types),
            terminal_event=terminal_events[0] if terminal_events else None,
        )

    @staticmethod
    def _require_owner_host(
        aggregates: Iterable[_Aggregate],
        context: TrustedTurnContext,
    ) -> None:
        for aggregate in aggregates:
            if aggregate.task.task_owner_host != context.owner_host:
                raise TaskStopOwnerHostMismatch(aggregate.task.task_owner_host)

    @staticmethod
    def _raise_missing_explicit_task(
        connection: sqlite3.Connection,
        issue_number: int,
    ) -> None:
        v1 = connection.execute(
            """
            SELECT 1
            FROM task_settings_events
            WHERE event_type = 'issue_bound' AND issue_number = ?
            LIMIT 1
            """,
            (issue_number,),
        ).fetchone()
        if v1 is not None:
            raise TaskStopUnsupported(
                "v1 Task Stop is unsupported because it has no trusted access binding"
            )
        raise TaskStopAccessDenied("Task is unavailable or access is denied")

    @staticmethod
    def _existing_stop(
        connection: sqlite3.Connection,
        request_ids: Iterable[str],
    ) -> str | None:
        ids = tuple(sorted(request_ids))
        if not ids:
            return None
        placeholders = ",".join("?" for _ in ids)
        rows = connection.execute(
            f"""
            SELECT stop_request_id
            FROM task_stop_requests
            WHERE request_id IN ({placeholders})
            ORDER BY requested_at, stop_request_id
            """,
            ids,
        ).fetchall()
        if len(rows) > 1:
            raise TaskStopError("Task aggregate has duplicate Stop requests")
        return None if not rows else str(rows[0][0])

    @staticmethod
    def _settings_hash_for(
        connection: sqlite3.Connection,
        request_id: str,
    ) -> str | None:
        row = connection.execute(
            "SELECT task_settings_hash FROM task_settings_v2 WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        return None if row is None else str(row[0])

    @staticmethod
    def _ensure_task_event(
        connection: sqlite3.Connection,
        *,
        request_id: str,
        settings_hash: str | None,
        event_type: str,
        event_key: str,
        payload: object,
        occurred_at: str,
    ) -> None:
        event_json = _json(payload)
        row = connection.execute(
            """
            SELECT task_settings_hash, project_id, event_type, event_json,
                   occurred_at
            FROM task_events
            WHERE request_id = ? AND event_key = ?
            """,
            (request_id, event_key),
        ).fetchone()
        expected = (settings_hash, None, event_type, event_json, occurred_at)
        if row is None:
            connection.execute(
                """
                INSERT INTO task_events (
                    request_id, task_settings_hash, project_id, event_type,
                    event_key, event_json, occurred_at
                ) VALUES (?, ?, NULL, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    settings_hash,
                    event_type,
                    event_key,
                    event_json,
                    occurred_at,
                ),
            )
            row = connection.execute(
                """
                SELECT task_settings_hash, project_id, event_type, event_json,
                       occurred_at
                FROM task_events
                WHERE request_id = ? AND event_key = ?
                """,
                (request_id, event_key),
            ).fetchone()
        if row is None or tuple(row) != expected:
            raise TaskStopError(f"Task event {event_key} does not match")

    def _cancel_pending_updates(
        self,
        connection: sqlite3.Connection,
        aggregate: _Aggregate,
        *,
        occurred_at: str,
    ) -> None:
        revisions = connection.execute(
            """
            SELECT revision.revision_request_id, revision.replacement_request_id
            FROM task_revision_requests AS revision
            WHERE revision.request_id IN ({})
              AND revision.state IN ('requested', 'confirmed')
              AND NOT EXISTS (
                  SELECT 1
                  FROM task_settings_v2 AS active_replacement
                  WHERE active_replacement.request_id = revision.replacement_request_id
              )
            ORDER BY created_at, revision_request_id
            """.format(
                ",".join("?" for _ in aggregate.request_ids)
            ),
            tuple(sorted(aggregate.request_ids)),
        ).fetchall()
        handled_replacements: set[str] = set()
        revision_service = TaskRevisionService(self._database)
        stopped_at = _parse_timestamp(occurred_at, "Stop event time")
        for row in revisions:
            revision_id = str(row[0])
            replacement_id = None if row[1] is None else str(row[1])
            try:
                revision_service.cancel_for_stop_on_connection(
                    connection,
                    revision_id,
                    reason="Task stop requested",
                    at=stopped_at,
                )
            except TaskRevisionError as error:
                raise TaskStopError(
                    "pending Task revision cancellation failed"
                ) from error
            if replacement_id is not None:
                handled_replacements.add(replacement_id)
        unbound_replacements = (
            aggregate.pending_replacement_ids - handled_replacements
        )
        if unbound_replacements:
            raise TaskStopError(
                "pending replacement is not bound to a durable revision"
            )

    @staticmethod
    def _receipt(
        connection: sqlite3.Connection,
        stop_request_id: str,
        aggregate: _Aggregate,
    ) -> StopReceipt:
        row = connection.execute(
            """
            SELECT request_id, task_settings_hash, state
            FROM task_stop_requests
            WHERE stop_request_id = ?
            """,
            (stop_request_id,),
        ).fetchone()
        if row is None or str(row[0]) != aggregate.task.request_id:
            raise TaskStopError("Task Stop readback does not match")
        return StopReceipt(
            stop_request_id=stop_request_id,
            request_id=str(row[0]),
            management_repository=aggregate.task.management_repository,
            parent_issue_number=aggregate.task.parent_issue_number,
            task_settings_hash=None if row[1] is None else str(row[1]),
            state=str(row[2]),
        )


def _related_request_ids(
    rows: dict[str, tuple[object, ...]],
    seeds: set[str],
) -> frozenset[str]:
    related = set(seeds)
    changed = True
    while changed:
        changed = False
        for request_id, row in rows.items():
            parent = row[8]
            if request_id in related or parent in related:
                before = len(related)
                related.add(request_id)
                if isinstance(parent, str):
                    related.add(parent)
                changed = changed or len(related) != before
    return frozenset(related)


def _request_components(
    requests: dict[str, TaskRequestV2],
) -> tuple[frozenset[str], ...]:
    remaining = set(requests)
    components: list[frozenset[str]] = []
    while remaining:
        pending = [remaining.pop()]
        component: set[str] = set()
        while pending:
            request_id = pending.pop()
            if request_id in component:
                continue
            component.add(request_id)
            request = requests[request_id]
            neighbors = {
                candidate.request_id
                for candidate in requests.values()
                if candidate.replaces_request_id == request_id
                or request.replaces_request_id == candidate.request_id
            }
            for neighbor in neighbors:
                if neighbor in remaining:
                    remaining.remove(neighbor)
                pending.append(neighbor)
        components.append(frozenset(component))
    return tuple(components)


def _validate_replacement_graph(requests: list[TaskRequestV2]) -> None:
    by_id = {request.request_id: request for request in requests}
    ids = set(by_id)
    children: dict[str, list[str]] = {request_id: [] for request_id in ids}
    for request in requests:
        parent = request.replaces_request_id
        if parent is None:
            continue
        if parent not in ids:
            raise TaskStopError("Task replacement chain is incomplete")
        children[parent].append(request.request_id)
    if any(len(values) > 1 for values in children.values()):
        raise TaskStopError("Task replacement chain forks")
    for request in requests:
        seen: set[str] = set()
        current: str | None = request.request_id
        while current is not None:
            if current in seen:
                raise TaskStopError("Task replacement chain contains a cycle")
            seen.add(current)
            current = by_id[current].replaces_request_id


def _canonical_uuid(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise TaskStopError(f"{field_name} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except ValueError:
        raise TaskStopError(f"{field_name} must be a canonical UUID") from None
    if str(parsed) != value:
        raise TaskStopError(f"{field_name} must be a canonical UUID")
    return value


def _require_context(context: TrustedTurnContext) -> None:
    if not isinstance(context, TrustedTurnContext):
        raise TaskStopError("context must be a trusted turn context")


def _require_optional_issue_number(value: int | None) -> None:
    if value is not None and (type(value) is not int or value <= 0 or value > (1 << 63) - 1):
        raise TaskStopError("issue_number must be a positive 64-bit integer")


def _require_hash(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise TaskStopError(f"{field_name} must be a lowercase SHA-256")
    return value


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TaskStopError("Stop time must include a timezone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise TaskStopError(f"{field_name} is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise TaskStopError(f"{field_name} is invalid") from None
    canonical_values = {
        _timestamp(parsed),
        parsed.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
    }
    if value not in canonical_values:
        raise TaskStopError(f"{field_name} is not canonical")
    return parsed


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_json(value: str, label: str) -> object:
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, RecursionError):
        raise TaskStopError(f"{label} JSON is invalid") from None
    if _json(parsed) != value:
        raise TaskStopError(f"{label} JSON is not canonical")
    return parsed


__all__ = [
    "StopReceipt",
    "StoppableTask",
    "TaskStopAccessDenied",
    "TaskStopError",
    "TaskStopOwnerHostMismatch",
    "TaskStopService",
    "TaskStopUnsupported",
]
