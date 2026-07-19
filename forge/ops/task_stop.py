"""Read and set the durable v2 Task Stop barrier."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Iterable, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from .github import (
    PullRequestWriteState,
    TaskStopIssueState,
    parse_pull_request_url,
)
from .displayed_status import FORGE_STATUS_LABELS
from .hermes import GateError
from .kanban_stop import (
    KanbanStopResult,
    ProcessIdentityLookup,
    archive_matching_cards,
)
from .process_identity import (
    ProcessIdentity,
    ProcessScopeBackend,
    ProcessStopResult,
    terminate_exact_process_tree,
)
from .surface_events import TrustedTurnContext
from .task_database import TaskDatabase, TaskDatabaseError
from .task_service import TaskServiceError, read_task_marker_v2
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


class StopCleanupOperation(str, Enum):
    """The complete write authority granted to one exact Stop cleanup."""

    RECONCILE_FORGE_STATUS = "reconcile_forge_status"
    COMMENT_RESULT = "comment_result"
    CLOSE_NOT_PLANNED = "close_not_planned"


_STOP_CLEANUP_OPERATIONS = frozenset(StopCleanupOperation)


@dataclass(frozen=True, slots=True)
class StopCleanupAuthority:
    """Verified, narrow authority for one unfinished durable Stop request."""

    stop_request_id: str
    request_id: str
    task_settings_hash: str | None
    management_repository: str
    parent_issue_number: int | None
    task_owner_host: str
    state: str
    allowed_operations: frozenset[StopCleanupOperation] = _STOP_CLEANUP_OPERATIONS

    def __post_init__(self) -> None:
        _canonical_uuid(self.stop_request_id, "stop_request_id")
        _canonical_uuid(self.request_id, "request_id")
        if self.task_settings_hash is not None:
            _require_hash(self.task_settings_hash, "task_settings_hash")
        _canonical_uuid(self.task_owner_host, "task_owner_host")
        _require_optional_issue_number(self.parent_issue_number)
        if self.state not in {"stopping", "cleanup_incomplete"}:
            raise TaskStopError("Stop cleanup authority requires unfinished state")
        if self.allowed_operations != _STOP_CLEANUP_OPERATIONS:
            raise TaskStopError("Stop cleanup authority operations changed")

    def require(self, operation: StopCleanupOperation | str) -> None:
        """Reject every external write outside the three cleanup operations."""

        try:
            parsed = StopCleanupOperation(operation)
        except (TypeError, ValueError):
            raise TaskStopError(
                "requested operation is not allowed for Stop cleanup"
            ) from None
        if parsed not in self.allowed_operations:
            raise TaskStopError("requested operation is not allowed for Stop cleanup")


@dataclass(frozen=True, slots=True)
class StopProjectArtifact:
    """Preserved recovery locations and remote identifiers for one Project."""

    project_id: str
    repository: str
    state: str
    branch_name: str | None
    worktree_path: str | None
    pr_url: str | None
    head_commit: str | None
    local_merge_commit: str | None


@dataclass(frozen=True, slots=True)
class StopReconcileReceipt:
    """Durable current result of one Stop reconciliation attempt."""

    stop_request_id: str
    request_id: str
    state: str
    result: str | None
    details_json: str

    @property
    def details(self) -> Mapping[str, object]:
        value = _canonical_json(self.details_json, "Task Stop details")
        if not isinstance(value, dict):
            raise TaskStopError("Task Stop details must be an object")
        return value


class StopIssueClient(Protocol):
    def find_stop_issue(
        self, repository: str, request_id: str
    ) -> TaskStopIssueState | None: ...

    def get_stop_issue(
        self, repository: str, issue_number: int
    ) -> TaskStopIssueState: ...

    def reconcile_stop_status(
        self, repository: str, issue_number: int, *, target: str | None
    ) -> TaskStopIssueState: ...

    def ensure_stop_comment(
        self,
        repository: str,
        issue_number: int,
        stop_request_id: str,
        body: str,
    ) -> str: ...

    def close_stop_issue_not_planned(
        self, repository: str, issue_number: int
    ) -> TaskStopIssueState: ...


class StopPullRequestReader(Protocol):
    def get_pr_write_state(self, pr_url: str) -> PullRequestWriteState: ...

    def find_pr_write_state(
        self, repository: str, branch_name: str
    ) -> PullRequestWriteState | None: ...


class StopKanbanController(Protocol):
    def archive(self, authority: StopCleanupAuthority) -> KanbanStopResult: ...


class StopProcessController(Protocol):
    def stop(
        self, identity: ProcessIdentity, *, current_host: str
    ) -> ProcessStopResult: ...


class HermesKanbanStopper:
    """Bind Task Stop orchestration to Task14's atomic Hermes card archive."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        dispatcher_database_path: str | Path,
        current_host: str,
        identity_lookup: ProcessIdentityLookup | None = None,
        claimer_host_name: str | None = None,
    ) -> None:
        self._database_path = Path(database_path)
        self._dispatcher_database_path = Path(dispatcher_database_path)
        self._current_host = _canonical_uuid(current_host, "current_host")
        self._identity_lookup = identity_lookup
        self._claimer_host_name = claimer_host_name

    def archive(self, authority: StopCleanupAuthority) -> KanbanStopResult:
        if not isinstance(authority, StopCleanupAuthority):
            raise TaskStopError("authority must be StopCleanupAuthority")
        if authority.task_settings_hash is None:
            raise TaskStopError("prepared Task has no Kanban cleanup authority")
        return archive_matching_cards(
            self._database_path,
            request_id=authority.request_id,
            task_settings_hash=authority.task_settings_hash,
            owner_host=authority.task_owner_host,
            current_host=self._current_host,
            dispatcher_database_path=self._dispatcher_database_path,
            reason=f"Task Stop {authority.stop_request_id}",
            identity_lookup=self._identity_lookup,
            claimer_host_name=self._claimer_host_name,
        )


class ProcessTreeStopper:
    """Bind reconciliation to Task14's exact process-tree terminator."""

    def __init__(
        self,
        backend: ProcessScopeBackend,
        *,
        term_timeout_seconds: float = 5.0,
        force_timeout_seconds: float = 5.0,
        poll_interval_seconds: float = 0.1,
    ) -> None:
        self._backend = backend
        self._term_timeout_seconds = term_timeout_seconds
        self._force_timeout_seconds = force_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds

    def stop(
        self,
        identity: ProcessIdentity,
        *,
        current_host: str,
    ) -> ProcessStopResult:
        return terminate_exact_process_tree(
            identity,
            expected=identity.binding,
            current_host=current_host,
            backend=self._backend,
            term_timeout_seconds=self._term_timeout_seconds,
            force_timeout_seconds=self._force_timeout_seconds,
            poll_interval_seconds=self._poll_interval_seconds,
        )


@dataclass(frozen=True, slots=True)
class _RemoteProject:
    artifact: StopProjectArtifact
    is_open: bool | None
    is_merged: bool
    head_commit: str | None
    merged_commit: str | None
    merged_base_commit: str | None
    merged_head_commit: str | None


@dataclass(frozen=True, slots=True)
class _StopOutcome:
    event_type: str
    result: str
    projects: tuple[_RemoteProject, ...]


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

    def guard_stop_cleanup(self, stop_request_id: str) -> StopCleanupAuthority:
        """Grant only Issue cleanup for one exact unfinished Stop request."""

        stop_request_id = _canonical_uuid(stop_request_id, "stop_request_id")
        try:
            with self._database.read() as connection:
                return self._guard_stop_cleanup_on_connection(
                    connection,
                    stop_request_id,
                )
        except TaskStopError:
            raise
        except (TaskDatabaseError, sqlite3.Error) as error:
            raise TaskStopError(
                "Task Stop cleanup authority could not be read"
            ) from error

    @staticmethod
    def _guard_stop_cleanup_on_connection(
        connection: sqlite3.Connection,
        stop_request_id: str,
    ) -> StopCleanupAuthority:
        row = connection.execute(
            """
            SELECT stop.request_id, stop.task_settings_hash, stop.state,
                   stop.details_json, request.management_repository,
                   request.task_owner_host
            FROM task_stop_requests AS stop
            JOIN task_requests AS request ON request.request_id = stop.request_id
            WHERE stop.stop_request_id = ?
            """,
            (stop_request_id,),
        ).fetchone()
        if row is None:
            raise TaskStopError("Task Stop request does not exist")
        request_id = _canonical_uuid(str(row[0]), "request_id")
        settings_hash = (
            None if row[1] is None else _require_hash(str(row[1]), "task_settings_hash")
        )
        state = str(row[2])
        if state == "completed":
            raise TaskStopError("completed Task Stop has no cleanup write authority")
        if state not in {"stopping", "cleanup_incomplete"}:
            raise TaskStopError("Task Stop is not ready for cleanup")
        details = _canonical_json(str(row[3]), "Task Stop details")
        if not isinstance(details, dict):
            raise TaskStopError("Task Stop details must be an object")
        required = {"management_repository", "parent_issue_number", "request_ids"}
        if not required.issubset(details):
            raise TaskStopError("Task Stop details are incomplete")
        management_repository = str(row[4])
        owner_host = _canonical_uuid(str(row[5]), "task_owner_host")
        if details["management_repository"] != management_repository:
            raise TaskStopError("Task Stop details changed management repository")
        parent_issue_number = details["parent_issue_number"]
        _require_optional_issue_number(parent_issue_number)
        raw_request_ids = details["request_ids"]
        if not isinstance(raw_request_ids, list) or not raw_request_ids:
            raise TaskStopError("Task Stop details request_ids are invalid")
        request_ids = tuple(
            _canonical_uuid(value, "details request_id") for value in raw_request_ids
        )
        if (
            tuple(sorted(set(request_ids))) != request_ids
            or request_id not in request_ids
        ):
            raise TaskStopError("Task Stop details request_ids are not canonical")

        request_bindings = connection.execute(
            "SELECT request_id, management_repository, task_owner_host "
            "FROM task_requests WHERE request_id IN ({}) ORDER BY request_id".format(
                ",".join("?" for _ in request_ids)
            ),
            request_ids,
        ).fetchall()
        if len(request_bindings) != len(request_ids) or any(
            tuple(binding) != (member_id, management_repository, owner_host)
            for member_id, binding in zip(request_ids, request_bindings, strict=True)
        ):
            raise TaskStopError("Task Stop aggregate owner binding changed")
        bound_numbers: set[int] = set()
        for member_id in request_ids:
            bound_events = connection.execute(
                """
                SELECT event_json FROM task_events
                WHERE request_id = ? AND event_type = 'parent_issue_bound'
                """,
                (member_id,),
            ).fetchall()
            for bound_event in bound_events:
                payload = _canonical_json(str(bound_event[0]), "parent Issue event")
                if (
                    not isinstance(payload, dict)
                    or type(payload.get("parent_issue_number")) is not int
                    or payload["parent_issue_number"] <= 0
                ):
                    raise TaskStopError("Task Stop parent Issue binding is invalid")
                bound_numbers.add(payload["parent_issue_number"])
        expected_parent = next(iter(bound_numbers), None)
        if len(bound_numbers) > 1 or expected_parent != parent_issue_number:
            raise TaskStopError("Task Stop parent Issue authority changed")

        matching_stops = connection.execute(
            "SELECT stop_request_id FROM task_stop_requests WHERE request_id IN ({})".format(
                ",".join("?" for _ in request_ids)
            ),
            request_ids,
        ).fetchall()
        if [str(item[0]) for item in matching_stops] != [stop_request_id]:
            raise TaskStopError(
                "Task Stop cleanup does not have exact aggregate authority"
            )

        event_json = _json({"stop_request_id": stop_request_id})
        for member_id in request_ids:
            event = connection.execute(
                """
                SELECT project_id, event_type, event_json, occurred_at
                FROM task_events
                WHERE request_id = ? AND event_key = ?
                """,
                (member_id, f"stop_requested:{stop_request_id}"),
            ).fetchone()
            if (
                event is None
                or event[0] is not None
                or str(event[1]) != "stop_requested"
                or str(event[2]) != event_json
            ):
                raise TaskStopError("Task Stop cleanup barrier does not match")
            _parse_timestamp(event[3], "Stop barrier time")
        stopping = connection.execute(
            """
            SELECT project_id, event_type, event_json, occurred_at
            FROM task_events
            WHERE request_id = ? AND event_key = ?
            """,
            (request_id, f"stopping:{stop_request_id}"),
        ).fetchone()
        if (
            stopping is None
            or stopping[0] is not None
            or str(stopping[1]) != "stopping"
            or str(stopping[2]) != event_json
        ):
            raise TaskStopError("Task Stop cleanup stopping event does not match")
        _parse_timestamp(stopping[3], "Stop barrier time")

        if settings_hash is not None:
            settings_row = connection.execute(
                """
                SELECT request_id, management_repository, parent_issue_number,
                       task_owner_host
                FROM task_settings_v2 WHERE task_settings_hash = ?
                """,
                (settings_hash,),
            ).fetchone()
            expected = (
                request_id,
                management_repository,
                parent_issue_number,
                owner_host,
            )
            if settings_row is None or tuple(settings_row) != expected:
                raise TaskStopError("Task Stop cleanup settings authority changed")

        return StopCleanupAuthority(
            stop_request_id=stop_request_id,
            request_id=request_id,
            task_settings_hash=settings_hash,
            management_repository=management_repository,
            parent_issue_number=parent_issue_number,
            task_owner_host=owner_host,
            state=state,
        )

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


class TaskStopReconciler:
    """Converge one durable Stop to local and remote read-back truth."""

    def __init__(
        self,
        database: TaskDatabase,
        *,
        issue_client: StopIssueClient,
        pull_request_reader: StopPullRequestReader,
        kanban_stopper: StopKanbanController | None,
        process_stopper: StopProcessController | None,
        current_host: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not isinstance(database, TaskDatabase):
            raise TaskStopError("database must be TaskDatabase")
        if not callable(clock):
            raise TaskStopError("clock must be callable")
        self._database = database
        self._service = TaskStopService(database)
        self._issues = issue_client
        self._pull_requests = pull_request_reader
        self._kanban = kanban_stopper
        self._processes = process_stopper
        self._current_host = _canonical_uuid(current_host, "current_host")
        self._clock = clock

    def list_reconcilable(self) -> tuple[str, ...]:
        """List only unfinished requests; completed Stops are never replayed."""

        try:
            with self._database.read() as connection:
                return tuple(
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT stop_request_id FROM task_stop_requests
                        WHERE state IN ('stopping', 'cleanup_incomplete')
                        ORDER BY requested_at, stop_request_id
                        """
                    )
                )
        except (TaskDatabaseError, sqlite3.Error) as error:
            raise TaskStopError("unfinished Task Stops could not be listed") from error

    def reconcile(self, stop_request_id: str) -> StopReconcileReceipt:
        """Run idempotent local cleanup, remote readback, and final commit."""

        stop_request_id = _canonical_uuid(stop_request_id, "stop_request_id")
        current = self._read_receipt(stop_request_id)
        if current.state == "completed":
            return current
        authority = self._service.guard_stop_cleanup(stop_request_id)
        if authority.task_owner_host != self._current_host:
            raise TaskStopOwnerHostMismatch(authority.task_owner_host)
        evidence: dict[str, object] = {}
        try:
            projects = self._load_projects(authority)
            local = self._stop_local(authority, projects)
            evidence["artifacts"] = self._artifact_details(projects)
            evidence["local_cleanup"] = local

            # A human may merge while cleanup is running. Two equal consecutive
            # remote snapshots are required before any outcome-dependent write,
            # then one more readback must still match before the terminal DB
            # transaction.
            previous_signature: str | None = None
            outcome: _StopOutcome | None = None
            issue: TaskStopIssueState | None = None
            for _attempt in range(6):
                authority = self._service.guard_stop_cleanup(stop_request_id)
                candidate = self._read_remote_projects(projects)
                signature = self._outcome_signature(candidate)
                if previous_signature != signature:
                    previous_signature = signature
                    outcome = candidate
                    continue
                outcome = candidate
                issue = self._resolve_issue(authority)
                issue = self._cleanup_issue(authority, outcome, issue)
                verified = self._read_remote_projects(projects)
                verified_signature = self._outcome_signature(verified)
                if verified_signature == signature:
                    outcome = verified
                    break
                previous_signature = verified_signature
                outcome = verified
            else:
                raise TaskStopError("remote Project state did not stabilize")
            assert outcome is not None
            self._verify_issue_result(outcome, issue)
            evidence["remote_projects"] = self._remote_details(outcome.projects)
            evidence["parent_issue"] = self._issue_details(issue)
            evidence["cleanup"] = {
                "state": "completed",
                "terminal_event": outcome.event_type,
                "result": outcome.result,
            }
            return self._complete(authority, outcome, evidence)
        except Exception as error:
            # KeyboardInterrupt/SystemExit are BaseException and deliberately
            # escape. A process crash leaves the durable barrier for the daemon.
            return self._mark_cleanup_incomplete(
                stop_request_id,
                error,
                evidence=evidence,
            )

    def _read_receipt(self, stop_request_id: str) -> StopReconcileReceipt:
        try:
            with self._database.read() as connection:
                row = connection.execute(
                    """
                    SELECT request_id, state, result, details_json
                    FROM task_stop_requests WHERE stop_request_id = ?
                    """,
                    (stop_request_id,),
                ).fetchone()
        except (TaskDatabaseError, sqlite3.Error) as error:
            raise TaskStopError("Task Stop receipt could not be read") from error
        if row is None:
            raise TaskStopError("Task Stop request does not exist")
        details_json = str(row[3])
        _canonical_json(details_json, "Task Stop details")
        return StopReconcileReceipt(
            stop_request_id=stop_request_id,
            request_id=_canonical_uuid(str(row[0]), "request_id"),
            state=str(row[1]),
            result=None if row[2] is None else str(row[2]),
            details_json=details_json,
        )

    def _load_projects(
        self,
        authority: StopCleanupAuthority,
    ) -> tuple[StopProjectArtifact, ...]:
        try:
            with self._database.read() as connection:
                request_row = connection.execute(
                    "SELECT request_json FROM task_requests WHERE request_id = ?",
                    (authority.request_id,),
                ).fetchone()
                project_rows = connection.execute(
                    """
                    SELECT project_id, task_settings_hash, project_json, state,
                           branch_name, worktree_path, pr_url, head_commit,
                           merge_commit
                    FROM task_projects WHERE request_id = ? ORDER BY project_id
                    """,
                    (authority.request_id,),
                ).fetchall()
        except (TaskDatabaseError, sqlite3.Error) as error:
            raise TaskStopError(
                "Task Stop Project registry could not be read"
            ) from error
        if request_row is None:
            raise TaskStopError("Task Stop request disappeared")
        try:
            request = TaskRequestV2.from_json(str(request_row[0]))
        except TaskSettingsV2Error as error:
            raise TaskStopError("Task Stop request JSON is invalid") from error
        if request.request_id != authority.request_id:
            raise TaskStopError("Task Stop request identity changed")
        raw_projects = {
            str(item["project_id"]): _json(item)
            for item in json.loads(request.to_json())["projects"]
        }
        if len(project_rows) != len(raw_projects):
            raise TaskStopError("Task Stop Project registry count changed")
        artifacts: list[StopProjectArtifact] = []
        for row in project_rows:
            project_id = _require_hash(str(row[0]), "project_id")
            project = next(
                (item for item in request.projects if item.project_id == project_id),
                None,
            )
            if (
                project is None
                or str(row[2]) != raw_projects.get(project_id)
                or row[1] != authority.task_settings_hash
            ):
                raise TaskStopError("Task Stop Project registry changed")
            if project.host_id != authority.task_owner_host:
                raise TaskStopError("Task Stop Project owner host changed")
            artifacts.append(
                StopProjectArtifact(
                    project_id=project_id,
                    repository=project.repository,
                    state=str(row[3]),
                    branch_name=_optional_text(row[4], "branch_name"),
                    worktree_path=_optional_text(row[5], "worktree_path"),
                    pr_url=_optional_text(row[6], "pr_url"),
                    head_commit=_optional_hash(row[7], "head_commit"),
                    local_merge_commit=_optional_hash(row[8], "merge_commit"),
                )
            )
        return tuple(artifacts)

    def _stop_local(
        self,
        authority: StopCleanupAuthority,
        projects: tuple[StopProjectArtifact, ...],
    ) -> dict[str, object]:
        if authority.task_settings_hash is None:
            if self._active_runtime_identities(authority, projects):
                raise TaskStopError("prepared Task has an unexpected active worker")
            return {
                "archived_card_ids": [],
                "preserved_card_ids": [],
                "processes": [],
            }
        if self._kanban is None:
            raise TaskStopError("active Task Stop requires a Kanban controller")
        archived: set[str] = set()
        preserved: set[str] = set()
        process_details: dict[str, dict[str, object]] = {}
        for _attempt in range(3):
            authority = self._service.guard_stop_cleanup(authority.stop_request_id)
            runtime_identities = self._active_runtime_identities(authority, projects)
            cards = self._kanban.archive(authority)
            if cards.request_id != authority.request_id or not cards.all_cards_terminal:
                raise TaskStopError("Task Stop cards did not become terminal")
            archived.update(cards.archived_card_ids)
            preserved.update(cards.preserved_card_ids)
            identities = {
                identity.to_json(): identity for identity in runtime_identities
            }
            for captured in cards.captured_runs:
                if captured.process_identity is not None:
                    captured_json = captured.process_identity.to_json()
                    if captured_json not in identities:
                        raise TaskStopError(
                            "Kanban worker has no durable Task runtime identity"
                        )
            if identities and self._processes is None:
                raise TaskStopError("active Task Stop requires a process controller")
            for identity_json, identity in sorted(identities.items()):
                if (
                    identity.binding.request_id != authority.request_id
                    or identity.binding.task_settings_hash
                    != authority.task_settings_hash
                    or identity.binding.host_id != authority.task_owner_host
                    or identity.binding.project_id
                    not in {project.project_id for project in projects}
                ):
                    raise TaskStopError("recorded process is outside Stop authority")
                assert self._processes is not None
                result = self._processes.stop(
                    identity,
                    current_host=self._current_host,
                )
                if not isinstance(result, ProcessStopResult) or not result.completed:
                    raise TaskStopError("recorded process descendants remain alive")
                process_details[identity_json] = {
                    "already_stopped": result.already_stopped,
                    "forced": result.forced,
                    "run_id": identity.binding.run_id,
                    "term_sent": result.term_sent,
                }
                self._mark_runtime_stopped(authority, identity)
            if not self._active_runtime_identities(authority, projects):
                return {
                    "archived_card_ids": sorted(archived),
                    "preserved_card_ids": sorted(preserved),
                    "processes": [
                        process_details[key] for key in sorted(process_details)
                    ],
                }
        raise TaskStopError("late worker remained after the Stop barrier")

    def _active_runtime_identities(
        self,
        authority: StopCleanupAuthority,
        projects: tuple[StopProjectArtifact, ...],
    ) -> tuple[ProcessIdentity, ...]:
        if authority.task_settings_hash is None:
            return ()
        try:
            with self._database.read() as connection:
                rows = connection.execute(
                    """
                    SELECT run_id, project_id, host_id, worker_task_id,
                           process_identity_json
                    FROM task_runtime_runs
                    WHERE request_id = ? AND task_settings_hash = ?
                      AND state IN ('starting', 'running', 'stopping')
                    ORDER BY run_id
                    """,
                    (authority.request_id, authority.task_settings_hash),
                ).fetchall()
        except (TaskDatabaseError, sqlite3.Error) as error:
            raise TaskStopError(
                "Task Stop runtime registry could not be read"
            ) from error
        project_ids = {project.project_id for project in projects}
        identities: list[ProcessIdentity] = []
        for row in rows:
            try:
                identity = ProcessIdentity.from_json(str(row[4]))
            except (TypeError, ValueError) as error:
                raise TaskStopError(
                    "Task Stop runtime process identity is invalid"
                ) from error
            expected = (
                str(row[0]),
                str(row[1]),
                str(row[2]),
                str(row[3]),
            )
            actual = (
                identity.binding.run_id,
                identity.binding.project_id,
                identity.binding.host_id,
                identity.binding.task_id,
            )
            if (
                actual != expected
                or identity.binding.request_id != authority.request_id
                or identity.binding.task_settings_hash != authority.task_settings_hash
                or identity.binding.project_id not in project_ids
            ):
                raise TaskStopError("Task Stop runtime process binding changed")
            identities.append(identity)
        return tuple(identities)

    def _mark_runtime_stopped(
        self,
        authority: StopCleanupAuthority,
        identity: ProcessIdentity,
    ) -> None:
        occurred_at = _timestamp(self._clock())
        try:
            with self._database.transaction() as connection:
                self._service._guard_stop_cleanup_on_connection(
                    connection,
                    authority.stop_request_id,
                )
                connection.execute(
                    """
                    UPDATE task_runtime_runs
                    SET state = 'stopped', ended_at = ?
                    WHERE run_id = ? AND request_id = ?
                      AND task_settings_hash = ? AND project_id = ?
                      AND host_id = ?
                      AND state IN ('starting', 'running', 'stopping')
                    """,
                    (
                        occurred_at,
                        identity.binding.run_id,
                        authority.request_id,
                        authority.task_settings_hash,
                        identity.binding.project_id,
                        authority.task_owner_host,
                    ),
                )
        except (TaskDatabaseError, sqlite3.Error) as error:
            raise TaskStopError(
                "Task Stop runtime result could not be stored"
            ) from error

    def _read_remote_projects(
        self,
        projects: tuple[StopProjectArtifact, ...],
    ) -> _StopOutcome:
        remote: list[_RemoteProject] = []
        for project in projects:
            state: PullRequestWriteState | None = None
            if project.pr_url is None and project.branch_name is not None:
                try:
                    state = self._pull_requests.find_pr_write_state(
                        project.repository,
                        project.branch_name,
                    )
                except (GateError, RuntimeError, KeyError) as error:
                    raise TaskStopError(
                        "Project PR recovery readback failed"
                    ) from error
                if state is not None:
                    project = replace(project, pr_url=state.pr_url)
            if project.pr_url is None and state is None:
                if project.local_merge_commit is not None or project.state == "merged":
                    raise TaskStopError("merged Project has no remote PR identity")
                remote.append(
                    _RemoteProject(
                        artifact=project,
                        is_open=None,
                        is_merged=False,
                        head_commit=None,
                        merged_commit=None,
                        merged_base_commit=None,
                        merged_head_commit=None,
                    )
                )
                continue
            if state is None:
                assert project.pr_url is not None
                try:
                    state = self._pull_requests.get_pr_write_state(project.pr_url)
                except (GateError, RuntimeError, KeyError) as error:
                    raise TaskStopError("Project PR remote readback failed") from error
            if not isinstance(state, PullRequestWriteState):
                raise TaskStopError("Project PR remote readback type is invalid")
            assert project.pr_url is not None
            repository, _number = parse_pull_request_url(project.pr_url)
            if (
                repository != project.repository
                or state.pr_url != project.pr_url
                or state.repository != repository
            ):
                raise TaskStopError("Project PR remote identity changed")
            _git_hash(state.base_commit, "remote base commit")
            _git_hash(state.head_commit, "remote head commit")
            if state.is_merged:
                if state.is_open:
                    raise TaskStopError("remote PR is both open and merged")
                _git_hash(state.merged_commit, "remote merge commit")
                _git_hash(state.merged_base_commit, "remote merged base")
                _git_hash(state.merged_head_commit, "remote merged head")
            elif any(
                value is not None
                for value in (
                    state.merged_commit,
                    state.merged_base_commit,
                    state.merged_head_commit,
                )
            ):
                raise TaskStopError("unmerged remote PR contains merge commits")
            if not state.is_merged and (
                project.local_merge_commit is not None or project.state == "merged"
            ):
                raise TaskStopError("local merged Project conflicts with remote PR")
            if (
                state.is_merged
                and project.local_merge_commit is not None
                and project.local_merge_commit != state.merged_commit
            ):
                raise TaskStopError("local and remote merge commits differ")
            remote.append(
                _RemoteProject(
                    artifact=project,
                    is_open=state.is_open,
                    is_merged=state.is_merged,
                    head_commit=state.head_commit,
                    merged_commit=state.merged_commit,
                    merged_base_commit=state.merged_base_commit,
                    merged_head_commit=state.merged_head_commit,
                )
            )
        merged_count = sum(project.is_merged for project in remote)
        if merged_count == 0:
            event_type, result = "cancelled", "cancelled"
        elif merged_count == len(remote):
            event_type, result = "merged", "completed_before_stop"
        else:
            event_type, result = (
                "partially_merged",
                "completed_with_partial_merge",
            )
        return _StopOutcome(event_type, result, tuple(remote))

    def _resolve_issue(
        self,
        authority: StopCleanupAuthority,
    ) -> TaskStopIssueState | None:
        try:
            if authority.parent_issue_number is None:
                issue = self._issues.find_stop_issue(
                    authority.management_repository,
                    authority.request_id,
                )
            else:
                issue = self._issues.get_stop_issue(
                    authority.management_repository,
                    authority.parent_issue_number,
                )
        except (GateError, RuntimeError) as error:
            raise TaskStopError("parent Issue remote readback failed") from error
        if issue is None:
            if authority.parent_issue_number is not None:
                raise TaskStopError("bound parent Issue is missing")
            return None
        if authority.parent_issue_number is not None and (
            issue.number != authority.parent_issue_number
        ):
            raise TaskStopError("parent Issue number changed")
        try:
            marker = read_task_marker_v2(issue.body)
        except TaskServiceError as error:
            raise TaskStopError("parent Issue Task marker is invalid") from error
        if marker.get("request_id") != authority.request_id:
            raise TaskStopError("parent Issue belongs to another Task")
        return issue

    def _cleanup_issue(
        self,
        authority: StopCleanupAuthority,
        outcome: _StopOutcome,
        issue: TaskStopIssueState | None,
    ) -> TaskStopIssueState | None:
        if issue is None:
            return None
        target = (
            "forge:needs-decision"
            if outcome.result == "completed_with_partial_merge"
            else None
        )
        authority = self._service.guard_stop_cleanup(authority.stop_request_id)
        authority.require(StopCleanupOperation.RECONCILE_FORGE_STATUS)
        try:
            issue = self._issues.reconcile_stop_status(
                authority.management_repository,
                issue.number,
                target=target,
            )
        except (GateError, RuntimeError) as error:
            raise TaskStopError("parent Issue status cleanup failed") from error
        body = self._comment_body(authority, outcome)
        authority = self._service.guard_stop_cleanup(authority.stop_request_id)
        authority.require(StopCleanupOperation.COMMENT_RESULT)
        try:
            self._issues.ensure_stop_comment(
                authority.management_repository,
                issue.number,
                authority.stop_request_id,
                body,
            )
        except (GateError, RuntimeError) as error:
            raise TaskStopError("parent Issue Stop comment failed") from error
        if outcome.result == "cancelled":
            authority = self._service.guard_stop_cleanup(authority.stop_request_id)
            authority.require(StopCleanupOperation.CLOSE_NOT_PLANNED)
            try:
                issue = self._issues.close_stop_issue_not_planned(
                    authority.management_repository,
                    issue.number,
                )
            except (GateError, RuntimeError) as error:
                raise TaskStopError("parent Issue close failed") from error
        return issue

    @staticmethod
    def _verify_issue_result(
        outcome: _StopOutcome,
        issue: TaskStopIssueState | None,
    ) -> None:
        if issue is None:
            return
        forge_labels = tuple(
            label for label in issue.labels if label in FORGE_STATUS_LABELS
        )
        if outcome.result == "cancelled":
            if (
                issue.state != "closed"
                or issue.state_reason != "not_planned"
                or forge_labels
            ):
                raise TaskStopError("cancelled parent Issue readback is incomplete")
        elif outcome.result == "completed_with_partial_merge":
            if issue.state != "open" or forge_labels != ("forge:needs-decision",):
                raise TaskStopError("partial parent Issue readback is incomplete")
        elif forge_labels:
            raise TaskStopError(
                "completed parent Issue still has an active Forge status"
            )

    def _complete(
        self,
        authority: StopCleanupAuthority,
        outcome: _StopOutcome,
        evidence: dict[str, object],
    ) -> StopReconcileReceipt:
        completed_at = _timestamp(self._clock())
        details_json = _json(
            {
                "management_repository": authority.management_repository,
                "parent_issue_number": authority.parent_issue_number,
                "request_ids": self._request_ids(authority.stop_request_id),
                **evidence,
            }
        )
        try:
            with self._database.transaction() as connection:
                authority = self._service._guard_stop_cleanup_on_connection(
                    connection,
                    authority.stop_request_id,
                )
                active_run = connection.execute(
                    """
                    SELECT 1 FROM task_runtime_runs
                    WHERE request_id = ?
                      AND state IN ('starting', 'running', 'stopping')
                    LIMIT 1
                    """,
                    (authority.request_id,),
                ).fetchone()
                if active_run is not None:
                    raise TaskStopError("late worker crossed the Stop barrier")
                for project in outcome.projects:
                    target_state = "merged" if project.is_merged else "cancelled"
                    target_merge = project.merged_commit if project.is_merged else None
                    updated = connection.execute(
                        """
                        UPDATE task_projects
                        SET state = ?, pr_url = ?, head_commit = ?,
                            merge_commit = ?, updated_at = ?
                        WHERE request_id = ? AND project_id = ?
                          AND task_settings_hash IS ?
                        """,
                        (
                            target_state,
                            project.artifact.pr_url,
                            project.head_commit,
                            target_merge,
                            completed_at,
                            authority.request_id,
                            project.artifact.project_id,
                            authority.task_settings_hash,
                        ),
                    )
                    if updated.rowcount != 1:
                        raise TaskStopError("Task Stop Project finalization changed")
                terminal_payload = {
                    "merged_projects": [
                        {
                            "merge_commit": project.merged_commit,
                            "pr_url": project.artifact.pr_url,
                            "project_id": project.artifact.project_id,
                        }
                        for project in outcome.projects
                        if project.is_merged
                    ],
                    "remaining_projects": [
                        {
                            "pr_url": project.artifact.pr_url,
                            "project_id": project.artifact.project_id,
                        }
                        for project in outcome.projects
                        if not project.is_merged
                    ],
                    "stop_request_id": authority.stop_request_id,
                }
                requested_at = connection.execute(
                    "SELECT requested_at FROM task_stop_requests WHERE stop_request_id = ?",
                    (authority.stop_request_id,),
                ).fetchone()
                if requested_at is None:
                    raise TaskStopError("Task Stop request disappeared")
                event_time = str(requested_at[0])
                _parse_timestamp(event_time, "Task Stop request time")
                existing_terminal = connection.execute(
                    """
                    SELECT event_type, event_key, event_json
                    FROM task_events
                    WHERE request_id = ? AND event_type IN (
                        'cancelled', 'expired', 'merged', 'replaced',
                        'partially_merged'
                    )
                    ORDER BY event_id
                    """,
                    (authority.request_id,),
                ).fetchall()
                expected_key = f"{outcome.event_type}:{authority.stop_request_id}"
                if existing_terminal and [tuple(row) for row in existing_terminal] != [
                    (outcome.event_type, expected_key, _json(terminal_payload))
                ]:
                    raise TaskStopError("Task terminal event won before Stop cleanup")
                self._service._ensure_task_event(
                    connection,
                    request_id=authority.request_id,
                    settings_hash=authority.task_settings_hash,
                    event_type=outcome.event_type,
                    event_key=expected_key,
                    payload=terminal_payload,
                    occurred_at=event_time,
                )
                updated = connection.execute(
                    """
                    UPDATE task_stop_requests
                    SET state = 'completed', result = ?, details_json = ?,
                        updated_at = ?, completed_at = ?
                    WHERE stop_request_id = ?
                      AND state IN ('stopping', 'cleanup_incomplete')
                      AND result IS NULL AND completed_at IS NULL
                    """,
                    (
                        outcome.result,
                        details_json,
                        completed_at,
                        completed_at,
                        authority.stop_request_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise TaskStopError("Task Stop completion did not commit")
        except TaskStopError:
            raise
        except (TaskDatabaseError, sqlite3.Error) as error:
            raise TaskStopError("Task Stop completion transaction failed") from error
        return self._read_receipt(authority.stop_request_id)

    def _mark_cleanup_incomplete(
        self,
        stop_request_id: str,
        error: Exception,
        *,
        evidence: dict[str, object],
    ) -> StopReconcileReceipt:
        current = self._read_receipt(stop_request_id)
        if current.state == "completed":
            return current
        base = dict(current.details)
        base.update(evidence)
        base["cleanup"] = {
            "error": {
                "message": str(error)[:1000] or error.__class__.__name__,
                "type": error.__class__.__name__,
            },
            "state": "incomplete",
        }
        occurred_at = _timestamp(self._clock())
        try:
            with self._database.transaction() as connection:
                row = connection.execute(
                    "SELECT state FROM task_stop_requests WHERE stop_request_id = ?",
                    (stop_request_id,),
                ).fetchone()
                if row is None:
                    raise TaskStopError("Task Stop request disappeared")
                if row[0] != "completed":
                    self._service._guard_stop_cleanup_on_connection(
                        connection,
                        stop_request_id,
                    )
                    updated = connection.execute(
                        """
                        UPDATE task_stop_requests
                        SET state = 'cleanup_incomplete', details_json = ?, updated_at = ?
                        WHERE stop_request_id = ?
                          AND state IN ('stopping', 'cleanup_incomplete')
                          AND result IS NULL AND completed_at IS NULL
                        """,
                        (_json(base), occurred_at, stop_request_id),
                    )
                    if updated.rowcount != 1:
                        raise TaskStopError("Task Stop incomplete state did not commit")
        except TaskStopError:
            raise
        except (TaskDatabaseError, sqlite3.Error) as database_error:
            raise TaskStopError(
                "Task Stop incomplete state could not be stored"
            ) from database_error
        return self._read_receipt(stop_request_id)

    def _request_ids(self, stop_request_id: str) -> list[str]:
        with self._database.read() as connection:
            row = connection.execute(
                "SELECT details_json FROM task_stop_requests WHERE stop_request_id = ?",
                (stop_request_id,),
            ).fetchone()
        if row is None:
            raise TaskStopError("Task Stop request disappeared")
        details = _canonical_json(str(row[0]), "Task Stop details")
        if not isinstance(details, dict) or not isinstance(
            details.get("request_ids"), list
        ):
            raise TaskStopError("Task Stop details request_ids are invalid")
        return list(details["request_ids"])

    @staticmethod
    def _outcome_signature(outcome: _StopOutcome) -> str:
        return _json(
            {
                "event_type": outcome.event_type,
                "projects": TaskStopReconciler._remote_details(outcome.projects),
                "result": outcome.result,
            }
        )

    @staticmethod
    def _artifact_details(
        projects: tuple[StopProjectArtifact, ...],
    ) -> list[dict[str, object]]:
        return [
            {
                "branch_name": project.branch_name,
                "project_id": project.project_id,
                "pr_url": project.pr_url,
                "repository": project.repository,
                "worktree_path": project.worktree_path,
            }
            for project in projects
        ]

    @staticmethod
    def _remote_details(
        projects: tuple[_RemoteProject, ...],
    ) -> list[dict[str, object]]:
        return [
            {
                "is_merged": project.is_merged,
                "is_open": project.is_open,
                "head_commit": project.head_commit,
                "merged_base_commit": project.merged_base_commit,
                "merged_commit": project.merged_commit,
                "merged_head_commit": project.merged_head_commit,
                "pr_url": project.artifact.pr_url,
                "project_id": project.artifact.project_id,
                "repository": project.artifact.repository,
            }
            for project in projects
        ]

    @staticmethod
    def _issue_details(issue: TaskStopIssueState | None) -> object:
        if issue is None:
            return None
        return {
            "labels": list(issue.labels),
            "number": issue.number,
            "state": issue.state,
            "state_reason": issue.state_reason,
        }

    @staticmethod
    def _comment_body(
        authority: StopCleanupAuthority,
        outcome: _StopOutcome,
    ) -> str:
        merged = [
            f"- `{project.artifact.repository}`: `{project.merged_commit}`"
            for project in outcome.projects
            if project.is_merged
        ]
        remaining = [
            f"- `{project.artifact.repository}`: "
            f"{f'[{project.artifact.pr_url}]({project.artifact.pr_url})' if project.artifact.pr_url else 'PR 없음'}"
            for project in outcome.projects
            if not project.is_merged
        ]
        lines = [
            "## Task Stop result",
            "",
            f"- result: `{outcome.result}`",
            f"- stop_request_id: `{authority.stop_request_id}`",
            "",
            "### Merged Projects",
            *(merged or ["- 없음"]),
            "",
            "### Remaining Projects",
            *(remaining or ["- 없음"]),
            "",
            f"<!-- forge-task-stop:{authority.stop_request_id} -->",
        ]
        return "\n".join(lines)


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


def _git_hash(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 40
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise TaskStopError(f"{field_name} must be a lowercase Git commit")
    return value


def _optional_hash(value: object, field_name: str) -> str | None:
    return None if value is None else _git_hash(value, field_name)


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise TaskStopError(f"{field_name} is invalid")
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
    "StopCleanupAuthority",
    "StopCleanupOperation",
    "StopIssueClient",
    "HermesKanbanStopper",
    "ProcessTreeStopper",
    "StopKanbanController",
    "StopProcessController",
    "StopProjectArtifact",
    "StopPullRequestReader",
    "StopReconcileReceipt",
    "StopReceipt",
    "StoppableTask",
    "TaskStopAccessDenied",
    "TaskStopError",
    "TaskStopOwnerHostMismatch",
    "TaskStopService",
    "TaskStopReconciler",
    "TaskStopUnsupported",
]
