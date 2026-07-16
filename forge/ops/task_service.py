"""Create one confirmed Forge Task in a replay-safe, fail-closed order."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Protocol
from uuid import UUID

from .task_outbox import TaskOutbox
from .task_options import MergeMode, TaskFlow
from .task_settings import (
    TaskContent,
    TaskSettings,
    TaskSettingsStatus,
    TaskSettingsStore,
    task_content_hash,
)


TASK_REQUEST_FORMAT = "forge-task-request/v1"
READY_TO_BUILD_LABEL = "forge:ready-to-build"
_MARKER_START = "<!-- forge-task-request"
_MARKER_PATTERN = re.compile(
    r"<!-- forge-task-request\n(?P<json>\{[^\n]+\})\n-->",
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PROCESS_LOCKS_GUARD = RLock()
_PROCESS_LOCKS: dict[str, RLock] = {}


class TaskServiceError(RuntimeError):
    """Raised when confirmed Task creation cannot safely continue."""


class _TaskLifecycleEndedError(TaskServiceError):
    def __init__(self, status: TaskSettingsStatus, issue_number: int) -> None:
        super().__init__(f"Task lifecycle ended as {status.value}")
        self.status = status
        self.issue_number = issue_number


@dataclass(frozen=True, slots=True)
class TaskCreationRequest:
    request_id: str
    repository: str
    content: TaskContent
    task_flow: TaskFlow
    merge_mode: MergeMode
    confirmed_by: str
    confirmed_at: datetime


@dataclass(frozen=True, slots=True)
class TaskIssue:
    number: int
    title: str
    body: str
    labels: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.number) is not int or self.number <= 0:
            raise TaskServiceError("GitHub issue number must be a positive integer")
        if not isinstance(self.title, str) or not self.title.strip():
            raise TaskServiceError("GitHub issue title must be non-empty text")
        if not isinstance(self.body, str):
            raise TaskServiceError("GitHub issue body must be text")
        if not isinstance(self.labels, tuple) or any(
            not isinstance(label, str) or not label.strip() for label in self.labels
        ):
            raise TaskServiceError("GitHub issue labels must be a tuple of text")


@dataclass(frozen=True, slots=True)
class CreatedTask:
    settings: TaskSettings
    issue: TaskIssue


class TaskIssueClient(Protocol):
    def find_issue(self, repository: str, request_id: str) -> TaskIssue | None: ...

    def create_issue(self, repository: str, title: str, body: str) -> TaskIssue: ...

    def update_issue(
        self,
        repository: str,
        issue_number: int,
        *,
        title: str,
        body: str,
    ) -> TaskIssue: ...

    def get_issue(self, repository: str, issue_number: int) -> TaskIssue: ...

    def add_label(
        self,
        repository: str,
        issue_number: int,
        label: str,
    ) -> TaskIssue: ...


def _task_marker_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise TaskServiceError("GitHub issue Task marker has duplicate fields")
        value[key] = item
    return value


def read_task_marker(body: str) -> dict[str, str]:
    """Read the one strict request marker used to resume a GitHub issue."""

    if not isinstance(body, str):
        raise TaskServiceError("GitHub issue body must be text")
    matches = tuple(_MARKER_PATTERN.finditer(body))
    if len(matches) != 1 or body.count(_MARKER_START) != 1:
        raise TaskServiceError("GitHub issue must contain exactly one Task marker")
    try:
        value = json.loads(
            matches[0].group("json"),
            object_pairs_hook=_task_marker_object,
        )
    except json.JSONDecodeError as error:
        raise TaskServiceError("GitHub issue Task marker is invalid JSON") from error
    if not isinstance(value, dict):
        raise TaskServiceError("GitHub issue Task marker must be an object")
    required = {"format_version", "request_id", "task_content_hash"}
    allowed = required | {"task_settings_hash"}
    if not required.issubset(value) or set(value) - allowed:
        raise TaskServiceError("GitHub issue Task marker has invalid fields")
    if value.get("format_version") != TASK_REQUEST_FORMAT:
        raise TaskServiceError("GitHub issue Task marker has an unknown format")
    request_id = value.get("request_id")
    if not isinstance(request_id, str):
        raise TaskServiceError("GitHub issue Task marker request_id is invalid")
    try:
        parsed_request_id = UUID(request_id)
    except ValueError as error:
        raise TaskServiceError(
            "GitHub issue Task marker request_id is invalid"
        ) from error
    if str(parsed_request_id) != request_id:
        raise TaskServiceError("GitHub issue Task marker request_id is invalid")
    for field in ("task_content_hash", "task_settings_hash"):
        if field in value and (
            not isinstance(value[field], str)
            or _SHA256_PATTERN.fullmatch(value[field]) is None
        ):
            raise TaskServiceError(f"GitHub issue Task marker {field} is invalid")
    return {str(key): str(item) for key, item in value.items()}


def _marker(settings: TaskSettings) -> str:
    payload = {
        "format_version": TASK_REQUEST_FORMAT,
        "request_id": settings.request_id,
        "task_content_hash": settings.task_content_hash,
    }
    if settings.task_settings_hash is not None:
        payload["task_settings_hash"] = settings.task_settings_hash
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{_MARKER_START}\n{encoded}\n-->"


def build_task_issue_body(content: TaskContent, settings: TaskSettings) -> str:
    """Build the one exact GitHub issue body for confirmed Task content."""

    if not isinstance(content, TaskContent):
        raise TaskServiceError("content must be TaskContent")
    if not isinstance(settings, TaskSettings):
        raise TaskServiceError("settings must be TaskSettings")
    criteria = "\n".join(
        f"{number}. {criterion}"
        for number, criterion in enumerate(content.acceptance_criteria, start=1)
    )
    sections = [
        content.description,
        "## Acceptance Criteria\n\n" + criteria,
    ]
    if settings.task_settings_hash is not None:
        expiry = (
            "manual"
            if settings.auto_merge_expires_at is None
            else settings.auto_merge_expires_at.isoformat().replace("+00:00", "Z")
        )
        sections.append(
            "## Task Settings\n\n"
            f"- Task flow: `{settings.task_flow.value}`\n"
            f"- Merge mode: `{settings.merge_mode.value}`\n"
            f"- Auto-merge permission until: `{expiry}`\n"
            f"- Task settings hash: `{settings.task_settings_hash}`"
        )
    sections.append(_marker(settings))
    return "\n\n".join(sections)


def verify_task_issue_content(
    issue: TaskIssue,
    request: TaskCreationRequest,
    settings: TaskSettings,
) -> None:
    """Require the issue title and body to equal the confirmed Task exactly."""

    if not isinstance(issue, TaskIssue):
        raise TaskServiceError("issue must be TaskIssue")
    if not isinstance(request, TaskCreationRequest):
        raise TaskServiceError("request must be TaskCreationRequest")
    if not isinstance(settings, TaskSettings):
        raise TaskServiceError("settings must be TaskSettings")
    if (
        request.request_id != settings.request_id
        or request.repository != settings.repository
        or task_content_hash(request.content) != settings.task_content_hash
    ):
        raise TaskServiceError("Task request does not match immutable settings")

    marker = read_task_marker(issue.body)
    if marker.get("request_id") != settings.request_id:
        raise TaskServiceError("GitHub issue request_id does not match")
    if marker.get("task_content_hash") != settings.task_content_hash:
        raise TaskServiceError("GitHub issue Task content hash does not match")
    if marker.get("task_settings_hash") != settings.task_settings_hash:
        raise TaskServiceError("GitHub issue Task settings hash does not match")
    if issue.title != request.content.title:
        raise TaskServiceError(
            "GitHub issue title does not match the confirmed Task"
        )
    if issue.body != build_task_issue_body(request.content, settings):
        raise TaskServiceError(
            "GitHub issue body does not match the confirmed Task"
        )


class TaskService:
    """Own the prepare, GitHub issue, activate, then ready-label sequence."""

    def __init__(self, store: TaskSettingsStore, github: TaskIssueClient) -> None:
        self._store = store
        self._github = github
        # RISK(race): every service instance using the same settings database
        # shares one process lock. The default Hermes callback constructs a new
        # TaskService per call, so an instance-local lock would permit two
        # simultaneous find-then-create operations for one request_id.
        lock_key = str(store.database_path)
        with _PROCESS_LOCKS_GUARD:
            self._lock = _PROCESS_LOCKS.setdefault(lock_key, RLock())

    def create_task_durable(
        self,
        request: TaskCreationRequest,
        outbox: TaskOutbox,
    ) -> CreatedTask:
        """Persist, exclusively deliver, then complete one confirmed Task."""

        if not isinstance(outbox, TaskOutbox):
            raise TaskServiceError("outbox must be a TaskOutbox")
        # RISK(side-effect): save commits before create_task can issue the first
        # GitHub write. Reordering these calls would lose crash recovery.
        stored_request = outbox.save(request)
        with outbox.claim(stored_request.request_id) as claim:
            if claim.already_ended:
                raise TaskServiceError(
                    f"Task lifecycle ended as {claim.terminal_status}"
                )
            if claim.already_completed:
                assert claim.issue_number is not None
                return self._read_completed_delivery(
                    claim.request,
                    claim.issue_number,
                )
            try:
                created = self.create_task(claim.request)
            except _TaskLifecycleEndedError as error:
                claim.finish_terminal(
                    error.issue_number,
                    error.status.value,
                )
                raise
            claim.complete(created.issue.number)
            return created

    def create_task(self, request: TaskCreationRequest) -> CreatedTask:
        if not isinstance(request, TaskCreationRequest):
            raise TaskServiceError("request must be a TaskCreationRequest")
        if _MARKER_START in request.content.description or any(
            _MARKER_START in item for item in request.content.acceptance_criteria
        ):
            raise TaskServiceError("Task content contains a reserved marker")
        if len(request.content.title) > 256:
            raise TaskServiceError("Task title is longer than GitHub allows")

        with self._lock:
            settings = self._store.prepare(
                TaskSettings.create(
                    request_id=request.request_id,
                    repository=request.repository,
                    task_content=request.content,
                    task_flow=request.task_flow,
                    merge_mode=request.merge_mode,
                    confirmed_by=request.confirmed_by,
                    confirmed_at=request.confirmed_at,
                )
            )
            if settings.status in {
                TaskSettingsStatus.CANCELLED,
                TaskSettingsStatus.EXPIRED,
                TaskSettingsStatus.MERGED,
            }:
                assert settings.issue_number is not None
                raise _TaskLifecycleEndedError(
                    settings.status,
                    settings.issue_number,
                )
            issue = self._find_issue(settings)
            if issue is None:
                if settings.issue_number is not None:
                    raise TaskServiceError("bound GitHub issue could not be found")
                issue = self._create_issue(request, settings)
            if settings.status is not TaskSettingsStatus.ACTIVE:
                verify_task_issue_content(issue, request, settings)

            if settings.issue_number is None:
                settings = self._store.bind_issue(
                    settings.request_id,
                    issue.number,
                )
            elif settings.issue_number != issue.number:
                raise TaskServiceError("GitHub issue does not match the bound issue")

            expected_body = build_task_issue_body(request.content, settings)
            if settings.status is TaskSettingsStatus.ACTIVE:
                self._require_exact_issue(
                    issue,
                    request,
                    settings,
                    "active GitHub issue content changed",
                )
            else:
                issue = self._update_issue(request, settings, expected_body)
                self._require_exact_issue(
                    issue,
                    request,
                    settings,
                    "GitHub issue update did not persist exact content",
                )
                settings = self._store.activate(settings.request_id)

            # RISK(race): serialize the active-state check and final GitHub write
            # against terminal lifecycle events in the settings database.
            with self._ready_label_guard(settings):
                issue = self._get_issue(settings)
                self._require_exact_issue(
                    issue,
                    request,
                    settings,
                    "active GitHub issue content changed",
                )
                issue = self._add_ready_label(settings)
                if READY_TO_BUILD_LABEL not in issue.labels:
                    raise TaskServiceError("GitHub ready label was not persisted")
                self._require_exact_issue(
                    issue,
                    request,
                    settings,
                    "GitHub issue changed while adding the ready label",
                )
            return CreatedTask(settings=settings, issue=issue)

    @contextmanager
    def _ready_label_guard(
        self,
        settings: TaskSettings,
    ) -> Iterator[None]:
        try:
            connection = sqlite3.connect(self._store.database_path, timeout=5)
        except sqlite3.Error as error:
            raise TaskServiceError(
                "Task lifecycle guard could not open the settings database"
            ) from error
        with closing(connection):
            try:
                connection.execute("BEGIN IMMEDIATE")
                terminal = connection.execute(
                    """
                    SELECT event_type
                    FROM task_settings_events
                    WHERE request_id = ?
                      AND event_type IN ('cancelled', 'expired', 'merged')
                    ORDER BY event_id DESC
                    LIMIT 1
                    """,
                    (settings.request_id,),
                ).fetchone()
                if terminal is not None:
                    assert settings.issue_number is not None
                    raise _TaskLifecycleEndedError(
                        TaskSettingsStatus(terminal[0]),
                        settings.issue_number,
                    )
                if self._store.get_active(settings.request_id) != settings:
                    raise TaskServiceError(
                        "Task settings changed before the ready label write"
                    )
                yield
                connection.commit()
            except sqlite3.Error as error:
                connection.rollback()
                raise TaskServiceError(
                    "Task lifecycle guard database operation failed"
                ) from error
            except BaseException:
                connection.rollback()
                raise

    def _read_completed_delivery(
        self,
        request: TaskCreationRequest,
        issue_number: int,
    ) -> CreatedTask:
        """Return a completed replay without repeating any GitHub write."""

        settings = self._store.get_active(request.request_id)
        if settings is None:
            raise TaskServiceError(
                "Task lifecycle ended or durable settings are unavailable"
            )
        if settings.issue_number != issue_number:
            raise TaskServiceError(
                "completed Task issue does not match durable settings"
            )
        issue = self._get_issue(settings)
        self._require_exact_issue(
            issue,
            request,
            settings,
            "active GitHub issue content changed",
        )
        if READY_TO_BUILD_LABEL not in issue.labels:
            raise TaskServiceError("completed Task is missing the ready label")
        return CreatedTask(settings=settings, issue=issue)

    def _find_issue(self, settings: TaskSettings) -> TaskIssue | None:
        try:
            issue = self._github.find_issue(
                settings.repository,
                settings.request_id,
            )
        except TaskServiceError:
            raise
        except Exception as error:
            raise TaskServiceError("GitHub issue lookup failed") from error
        if issue is not None and not isinstance(issue, TaskIssue):
            raise TaskServiceError("GitHub issue lookup returned an invalid value")
        return issue

    def _create_issue(
        self,
        request: TaskCreationRequest,
        settings: TaskSettings,
    ) -> TaskIssue:
        try:
            issue = self._github.create_issue(
                settings.repository,
                request.content.title,
                build_task_issue_body(request.content, settings),
            )
        except Exception as error:
            raise TaskServiceError("GitHub issue creation failed") from error
        if not isinstance(issue, TaskIssue):
            raise TaskServiceError("GitHub issue creation returned an invalid value")
        return issue

    def _update_issue(
        self,
        request: TaskCreationRequest,
        settings: TaskSettings,
        body: str,
    ) -> TaskIssue:
        assert settings.issue_number is not None
        try:
            issue = self._github.update_issue(
                settings.repository,
                settings.issue_number,
                title=request.content.title,
                body=body,
            )
        except Exception as error:
            raise TaskServiceError("GitHub issue update failed") from error
        if not isinstance(issue, TaskIssue):
            raise TaskServiceError("GitHub issue update returned an invalid value")
        return issue

    def _get_issue(self, settings: TaskSettings) -> TaskIssue:
        assert settings.issue_number is not None
        try:
            issue = self._github.get_issue(settings.repository, settings.issue_number)
        except Exception as error:
            raise TaskServiceError("GitHub issue readback failed") from error
        if not isinstance(issue, TaskIssue):
            raise TaskServiceError("GitHub issue readback returned an invalid value")
        return issue

    def _add_ready_label(self, settings: TaskSettings) -> TaskIssue:
        assert settings.issue_number is not None
        try:
            issue = self._github.add_label(
                settings.repository,
                settings.issue_number,
                READY_TO_BUILD_LABEL,
            )
        except Exception as error:
            raise TaskServiceError("GitHub ready label failed") from error
        if not isinstance(issue, TaskIssue):
            raise TaskServiceError("GitHub ready label returned an invalid value")
        return issue

    @staticmethod
    def _require_exact_issue(
        issue: TaskIssue,
        request: TaskCreationRequest,
        settings: TaskSettings,
        message: str,
    ) -> None:
        try:
            verify_task_issue_content(issue, request, settings)
        except TaskServiceError as error:
            raise TaskServiceError(message) from error
