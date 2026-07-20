"""Authenticated Hermes tools for reading and controlling managed Tasks."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import re
from typing import Protocol

from .surface_events import (
    SurfaceEventError,
    SurfaceEventStore,
    TrustedTurnContext,
    surface_event_payload_hash,
)
from .task_database import TaskDatabase, TaskDatabaseError
from .task_messages import TaskMessageError, TaskMessageStore
from .task_projects import TaskProject, TaskProjectError
from .task_stop import (
    StopReceipt,
    StoppableTask,
    TaskStopAccessDenied,
    TaskStopError,
    TaskStopOwnerHostMismatch,
    TaskStopService,
    TaskStopUnsupported,
)


FORGE_TOOL_NAMES = (
    "list_tasks",
    "task_status",
    "send_to_task",
    "stop_task",
)
FORGE_MUTATING_TOOLS = frozenset({"send_to_task", "stop_task"})
FORGE_RESERVED_ARGUMENTS = frozenset(
    {
        "_forge_trusted_context",
        "cwd",
        "owner_host",
        "session_id",
        "source_event_id",
        "source_payload",
        "source_payload_hash",
        "subject_id",
        "surface",
        "trusted_turn_context",
        "user_id",
        "working_directory",
    }
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_MESSAGEABLE_STATES = frozenset({"active", "changing"})
_MESSAGEABLE_EVENTS = frozenset(
    {"active", "revision_requested", "changing", "revision_resumed"}
)
_TERMINAL_EVENTS = frozenset(
    {"cancelled", "expired", "merged", "replaced", "partially_merged"}
)
_LIFECYCLE_EVENTS = tuple(
    sorted(
        _MESSAGEABLE_EVENTS
        | _TERMINAL_EVENTS
        | {"stop_requested", "stopping"}
    )
)
_ACCESS_DENIED = "Task is unavailable or access is denied"


class ForgeToolError(RuntimeError):
    """Raised when one Forge tool cannot preserve its durable contract."""


class ForgeToolAccessDenied(ForgeToolError):
    """Deny a Task without disclosing whether it exists or belongs elsewhere."""


class ForgeStopBackend(Protocol):
    def get_stoppable(
        self,
        context: TrustedTurnContext,
        issue_number: int | None = None,
    ) -> tuple[StoppableTask, ...]: ...

    def request_stop(
        self,
        request_id: str,
        context: TrustedTurnContext,
        payload: str,
    ) -> StopReceipt: ...


@dataclass(frozen=True, slots=True)
class TrustedToolEnvelope:
    """Host-owned turn data carried outside all model-controlled arguments."""

    context: TrustedTurnContext
    source_payload: str | None = None
    source_payload_hash: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.context, TrustedTurnContext):
            raise ForgeToolError("authenticated Task context is unavailable")
        if self.source_payload is not None and not isinstance(self.source_payload, str):
            raise ForgeToolError("trusted user turn is invalid")
        if self.source_payload_hash is not None and (
            not isinstance(self.source_payload_hash, str)
            or _SHA256.fullmatch(self.source_payload_hash) is None
        ):
            raise ForgeToolError("trusted user turn hash is invalid")

    @classmethod
    def from_mapping(cls, value: object) -> TrustedToolEnvelope:
        """Parse only the system middleware mapping, never a tool argument."""

        if not isinstance(value, Mapping):
            raise ForgeToolError("authenticated Task context is unavailable")
        try:
            context = TrustedTurnContext(
                owner_host=value["owner_host"],
                subject_id=value["subject_id"],
                session_id=value["session_id"],
                surface=value["surface"],
                source_event_id=value["source_event_id"],
                working_directory=value.get("working_directory"),
            )
        except (KeyError, SurfaceEventError, TypeError, ValueError) as error:
            raise ForgeToolError("authenticated Task context is unavailable") from error
        return cls(
            context=context,
            source_payload=value.get("source_payload"),
            source_payload_hash=value.get("source_payload_hash"),
        )

    def require_mutation_payload(self) -> str:
        """Return the exact raw turn only when its hidden digest still matches."""

        if self.source_payload is None or self.source_payload_hash is None:
            raise ForgeToolError("trusted user turn is unavailable")
        try:
            expected = surface_event_payload_hash(self.context, self.source_payload)
        except SurfaceEventError as error:
            raise ForgeToolError("trusted user turn is invalid") from error
        if expected != self.source_payload_hash:
            raise ForgeToolError("trusted user turn hash does not match")
        return self.source_payload


class _DatabaseStopBackend:
    """Default Stop barrier backend for non-plugin callers."""

    def __init__(self, database: TaskDatabase) -> None:
        self._events = SurfaceEventStore(database)
        self._stops = TaskStopService(database)

    def get_stoppable(
        self,
        context: TrustedTurnContext,
        issue_number: int | None = None,
    ) -> tuple[StoppableTask, ...]:
        return self._stops.get_stoppable(context, issue_number)

    def request_stop(
        self,
        request_id: str,
        context: TrustedTurnContext,
        payload: str,
    ) -> StopReceipt:
        self._stops.resolve_stoppable(request_id, context)
        event = self._events.receive(context, payload)
        return self._stops.request_stop(
            request_id,
            context,
            payload_hash=event.payload_hash,
        )


class ForgeToolService:
    """Pure Task selection and tool behavior with injected mutable seams."""

    def __init__(
        self,
        database: TaskDatabase,
        *,
        message_store: TaskMessageStore | None = None,
        stop_backend: ForgeStopBackend | None = None,
        reconcile_trigger: Callable[[StopReceipt], None] | None = None,
    ) -> None:
        if not isinstance(database, TaskDatabase):
            raise ForgeToolError("database must be a TaskDatabase")
        self._database = database
        self._messages = message_store or TaskMessageStore(database)
        self._stops = stop_backend or _DatabaseStopBackend(database)
        self._reconcile_trigger = reconcile_trigger

    def list_tasks(self, envelope: TrustedToolEnvelope) -> dict[str, object]:
        tasks = self._get_tasks(envelope.context)
        return {
            "status": "ok",
            "tasks": [self._task_summary(task) for task in tasks],
        }

    def task_status(
        self,
        envelope: TrustedToolEnvelope,
        *,
        task_number: int | None = None,
    ) -> dict[str, object]:
        task_number = _optional_task_number(task_number)
        tasks = self._get_tasks(envelope.context, task_number)
        if task_number is not None and len(tasks) != 1:
            raise ForgeToolAccessDenied(_ACCESS_DENIED)
        detailed = [self._task_detail(task) for task in tasks]
        if len(detailed) == 1:
            return {"status": "ok", "task": detailed[0]}
        return {"status": "ok", "tasks": detailed}

    def send_to_task(
        self,
        envelope: TrustedToolEnvelope,
        *,
        task_number: int | None = None,
        message_hint: str | None = None,
    ) -> dict[str, object]:
        del message_hint  # Model text is never the mutation payload.
        task_number = _optional_task_number(task_number)
        selected = self._select_task(
            envelope.context,
            task_number=task_number,
            messageable_only=True,
        )
        if isinstance(selected, dict):
            return selected
        payload = envelope.require_mutation_payload()
        try:
            SurfaceEventStore(self._database).receive(envelope.context, payload)
            receipt = self._messages.send(
                selected.request_id,
                envelope.context,
                payload,
            )
        except TaskMessageError as error:
            if str(error) == _ACCESS_DENIED:
                raise ForgeToolAccessDenied(_ACCESS_DENIED) from error
            raise ForgeToolError(str(error)) from error
        except (SurfaceEventError, TaskDatabaseError) as error:
            raise ForgeToolError("Task message could not be recorded") from error
        return {
            "created": receipt.created,
            "message_id": receipt.message.message_id,
            "revision_request_id": receipt.revision_request_id,
            "status": "sent",
            "task_number": receipt.message.parent_issue_number,
        }

    def stop_task(
        self,
        envelope: TrustedToolEnvelope,
        *,
        task_number: int | None = None,
    ) -> dict[str, object]:
        task_number = _optional_task_number(task_number)
        selected = self._select_task(
            envelope.context,
            task_number=task_number,
            messageable_only=False,
        )
        if isinstance(selected, dict):
            return selected
        payload = envelope.require_mutation_payload()
        try:
            receipt = self._stops.request_stop(
                selected.request_id,
                envelope.context,
                payload,
            )
        except (TaskStopAccessDenied, TaskStopOwnerHostMismatch, TaskStopUnsupported) as error:
            raise ForgeToolAccessDenied(_ACCESS_DENIED) from error
        except (TaskStopError, TaskDatabaseError, SurfaceEventError) as error:
            raise ForgeToolError(str(error)) from error
        reconcile_triggered = False
        if self._reconcile_trigger is not None:
            try:
                self._reconcile_trigger(receipt)
                reconcile_triggered = True
            except Exception:
                # The Stop barrier is already durable. The periodic Task15
                # reconciler remains the retry path, so never report it undone.
                reconcile_triggered = False
        return {
            "reconcile_triggered": reconcile_triggered,
            "status": "stop_requested",
            "stop_request_id": receipt.stop_request_id,
            "task_number": receipt.parent_issue_number,
        }

    def _select_task(
        self,
        context: TrustedTurnContext,
        *,
        task_number: int | None,
        messageable_only: bool,
    ) -> StoppableTask | dict[str, object]:
        if task_number is not None:
            tasks = self._get_tasks(context, task_number)
            if len(tasks) != 1:
                raise ForgeToolAccessDenied(_ACCESS_DENIED)
            task = tasks[0]
            if messageable_only and not self._is_messageable(task):
                raise ForgeToolError("Task is not messageable in its current state")
            return task

        visible = self._get_tasks(context)
        bound_ids, bound_numbers = self._session_bindings(context)
        bound = tuple(
            task
            for task in visible
            if task.request_id in bound_ids
            or task.parent_issue_number in bound_numbers
        )
        if messageable_only:
            candidates = tuple(
                task for task in bound if self._is_messageable(task)
            )
            if not candidates:
                candidates = tuple(
                    task
                    for task in (
                        self._all_direct_tasks(context)
                        if bound_ids or bound_numbers
                        else visible
                    )
                    if self._is_messageable(task)
                )
        else:
            candidates = bound or visible

        candidates = _unique_tasks(candidates)
        if not candidates:
            raise ForgeToolError("No accessible Task is available")
        if len(candidates) == 1:
            return candidates[0]
        return {
            "status": "choose_task",
            "choices": [self._task_summary(task) for task in candidates],
        }

    def _get_tasks(
        self,
        context: TrustedTurnContext,
        task_number: int | None = None,
    ) -> tuple[StoppableTask, ...]:
        try:
            tasks = self._stops.get_stoppable(context, task_number)
        except (TaskStopAccessDenied, TaskStopOwnerHostMismatch, TaskStopUnsupported) as error:
            raise ForgeToolAccessDenied(_ACCESS_DENIED) from error
        except TaskStopError as error:
            raise ForgeToolError(str(error)) from error
        return tuple(task for task in tasks if not self._is_terminal(task.request_id))

    def _latest_lifecycle_event(self, request_id: str) -> str | None:
        placeholders = ",".join("?" for _ in _LIFECYCLE_EVENTS)
        try:
            with self._database.read() as connection:
                row = connection.execute(
                    f"""
                    SELECT event_type FROM task_events
                    WHERE request_id = ? AND event_type IN ({placeholders})
                    ORDER BY event_id DESC LIMIT 1
                    """,
                    (request_id, *_LIFECYCLE_EVENTS),
                ).fetchone()
        except TaskDatabaseError as error:
            raise ForgeToolError("Task lifecycle could not be read") from error
        if row is None:
            return None
        event_type = row[0]
        if not isinstance(event_type, str) or event_type not in _LIFECYCLE_EVENTS:
            raise ForgeToolError("stored Task lifecycle is invalid")
        return event_type

    def _is_terminal(self, request_id: str) -> bool:
        return self._latest_lifecycle_event(request_id) in _TERMINAL_EVENTS

    def _is_messageable(self, task: StoppableTask) -> bool:
        return (
            task.state in _MESSAGEABLE_STATES
            and self._latest_lifecycle_event(task.request_id) in _MESSAGEABLE_EVENTS
        )

    def _all_direct_tasks(
        self,
        context: TrustedTurnContext,
    ) -> tuple[StoppableTask, ...]:
        try:
            with self._database.read() as connection:
                rows = connection.execute(
                    """
                    SELECT DISTINCT settings.parent_issue_number
                    FROM task_access AS access
                    JOIN task_requests AS request
                      ON request.request_id = access.request_id
                    JOIN task_settings_v2 AS settings
                      ON settings.request_id = access.request_id
                    WHERE access.surface = ? AND access.subject_id = ?
                      AND access.role IN ('owner', 'operator')
                      AND access.revoked_at IS NULL
                      AND request.task_owner_host = ?
                    ORDER BY settings.parent_issue_number
                    """,
                    (context.surface, context.subject_id, context.owner_host),
                ).fetchall()
        except TaskDatabaseError as error:
            raise ForgeToolError("Task candidates could not be read") from error
        tasks: list[StoppableTask] = []
        for row in rows:
            number = row[0]
            if type(number) is not int or number <= 0:
                raise ForgeToolError("stored Task number is invalid")
            tasks.extend(self._get_tasks(context, number))
        return _unique_tasks(tuple(tasks))

    def _session_bindings(
        self,
        context: TrustedTurnContext,
    ) -> tuple[frozenset[str], frozenset[int]]:
        try:
            with self._database.read() as connection:
                rows = connection.execute(
                    """
                    SELECT request_id, parent_issue_number
                    FROM task_session_bindings
                    WHERE surface = ? AND subject_id = ? AND session_id = ?
                    """,
                    (context.surface, context.subject_id, context.session_id),
                ).fetchall()
        except TaskDatabaseError as error:
            raise ForgeToolError("Task session binding could not be read") from error
        ids: set[str] = set()
        numbers: set[int] = set()
        for row in rows:
            if not isinstance(row[0], str) or not row[0]:
                raise ForgeToolError("stored Task session binding is invalid")
            if type(row[1]) is not int or row[1] <= 0:
                raise ForgeToolError("stored Task session binding is invalid")
            ids.add(str(row[0]))
            numbers.add(int(row[1]))
        return frozenset(ids), frozenset(numbers)

    def _task_summary(self, task: StoppableTask) -> dict[str, object]:
        return {
            "state": task.state,
            "task_number": task.parent_issue_number,
            "title": task.title,
        }

    def _task_detail(self, task: StoppableTask) -> dict[str, object]:
        return {
            **self._task_summary(task),
            "projects": self._projects_for(task),
        }

    def _projects_for(self, task: StoppableTask) -> list[dict[str, object]]:
        try:
            with self._database.read() as connection:
                rows = connection.execute(
                    """
                    SELECT project_id, project_json, state, pr_url
                    FROM task_projects
                    WHERE request_id = ?
                    ORDER BY project_id
                    """,
                    (task.request_id,),
                ).fetchall()
        except TaskDatabaseError as error:
            raise ForgeToolError("Task project status could not be read") from error
        projects: list[dict[str, object]] = []
        for row in rows:
            try:
                raw = json.loads(str(row[1]))
                project = TaskProject.from_mapping(raw)
            except (json.JSONDecodeError, TaskProjectError, TypeError) as error:
                raise ForgeToolError("stored Task project is invalid") from error
            if str(row[0]) != project.project_id:
                raise ForgeToolError("stored Task project ID changed")
            state = str(row[2])
            pr_url = row[3]
            if pr_url is not None and not isinstance(pr_url, str):
                raise ForgeToolError("stored Task PR URL is invalid")
            projects.append(
                {
                    "pr_url": pr_url,
                    "project_id": project.project_id,
                    "repository": project.repository,
                    "state": state,
                    "waiting_reason": (
                        "Task needs operator help"
                        if state == "waiting_for_help"
                        else None
                    ),
                }
            )
        return projects


def _optional_task_number(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise ForgeToolError("task_number must be a positive integer")
    return value


def _unique_tasks(tasks: tuple[StoppableTask, ...]) -> tuple[StoppableTask, ...]:
    unique = {task.request_id: task for task in tasks}
    return tuple(
        sorted(
            unique.values(),
            key=lambda task: (
                task.parent_issue_number is None,
                task.parent_issue_number or 0,
                task.request_id,
            ),
        )
    )


__all__ = [
    "FORGE_MUTATING_TOOLS",
    "FORGE_RESERVED_ARGUMENTS",
    "FORGE_TOOL_NAMES",
    "ForgeStopBackend",
    "ForgeToolAccessDenied",
    "ForgeToolError",
    "ForgeToolService",
    "TrustedToolEnvelope",
]
