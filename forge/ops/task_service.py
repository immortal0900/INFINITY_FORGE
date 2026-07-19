"""Create one confirmed Forge Task in a replay-safe, fail-closed order."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock
from typing import Protocol
from uuid import UUID

from .task_database import TaskDatabase, TaskDatabaseError
from .task_outbox import TaskOutbox
from .task_options import MergeMode, TaskFlow
from .task_projects import TaskProject
from .task_settings import (
    TaskContent,
    TaskSettings,
    TaskSettingsStatus,
    TaskSettingsStore,
    task_content_hash,
)
from .task_settings_v2 import (
    TASK_REQUEST_V2_FORMAT,
    TaskRequestV2,
    TaskSettingsV2,
    TaskSettingsV2Error,
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

V2_PROGRESS_START = "<!-- forge-v2-progress:start -->"
V2_PROGRESS_END = "<!-- forge-v2-progress:end -->"
_V2_MARKER_START = "<!-- forge-v2-task-request"
_V2_MARKER_PATTERN = re.compile(
    r"<!-- forge-v2-task-request\n(?P<json>\{[^\n]+\})\n-->",
)
_V2_BODY_LIMIT_BYTES = 65_536
_V2_PROGRESS_LABELS = {
    "blocked": "Blocked",
    "ready": "Ready",
}
_V2_ROOT_KEY_PATTERN = re.compile(
    r"^forge-task-v2:"
    r"(?P<request_id>[0-9a-f-]{36}):"
    r"(?P<project_id>[0-9a-f]{64}):build$"
)
_V2_REPOSITORY_PATTERN = re.compile(r"^[^/\s]+/[^/\s]+$")
_V2_LIFECYCLE_BARRIER_EVENTS = (
    "revision_requested",
    "stop_requested",
    "changing",
    "stopping",
    "cancelled",
    "expired",
    "merged",
    "replaced",
    "partially_merged",
)


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
class TaskParentIssue:
    """Strict v2 snapshot of the central Management issue."""

    number: int
    title: str
    body: str
    state: str

    def __post_init__(self) -> None:
        if type(self.number) is not int or self.number <= 0:
            raise TaskServiceError("GitHub parent issue number must be positive")
        if not isinstance(self.title, str) or not self.title.strip():
            raise TaskServiceError("GitHub parent issue title must be non-empty text")
        if not isinstance(self.body, str):
            raise TaskServiceError("GitHub parent issue body must be text")
        if type(self.state) is not str or self.state not in {"open", "closed"}:
            raise TaskServiceError("GitHub parent issue state is invalid")


@dataclass(frozen=True, slots=True)
class CreatedTask:
    settings: TaskSettings
    issue: TaskIssue


def root_project_item_key(request_id: str, project_id: str) -> str:
    """Return the exact request + Project + build idempotency key."""

    if not isinstance(request_id, str):
        raise TaskServiceError("Project item request_id is invalid")
    try:
        parsed_request_id = UUID(request_id)
    except ValueError as error:
        raise TaskServiceError("Project item request_id is invalid") from error
    if str(parsed_request_id) != request_id:
        raise TaskServiceError("Project item request_id is invalid")
    if not isinstance(project_id, str) or _SHA256_PATTERN.fullmatch(project_id) is None:
        raise TaskServiceError("Project item project_id is invalid")
    return f"forge-task-v2:{request_id}:{project_id}:build"


@dataclass(frozen=True, slots=True)
class ProjectExecutionItem:
    """Strict external snapshot for one blocked or released Build root."""

    item_id: str
    idempotency_key: str
    parent_issue_number: int
    project_repository: str
    state: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.item_id, str)
            or not self.item_id.strip()
            or self.item_id != self.item_id.strip()
            or len(self.item_id) > 512
        ):
            raise TaskServiceError("Project item ID is invalid")
        key_match = (
            None
            if not isinstance(self.idempotency_key, str)
            else _V2_ROOT_KEY_PATTERN.fullmatch(self.idempotency_key)
        )
        if key_match is None:
            raise TaskServiceError("Project item idempotency key is invalid")
        try:
            key_request_id = str(UUID(key_match.group("request_id")))
        except ValueError as error:
            raise TaskServiceError("Project item idempotency key is invalid") from error
        if key_request_id != key_match.group("request_id"):
            raise TaskServiceError("Project item idempotency key is invalid")
        if type(self.parent_issue_number) is not int or self.parent_issue_number <= 0:
            raise TaskServiceError("Project item parent issue number is invalid")
        if (
            not isinstance(self.project_repository, str)
            or _V2_REPOSITORY_PATTERN.fullmatch(self.project_repository) is None
        ):
            raise TaskServiceError("Project item repository is invalid")
        if type(self.state) is not str or self.state not in {"blocked", "ready"}:
            raise TaskServiceError("Project item state is invalid")


@dataclass(frozen=True, slots=True)
class CreatedTaskV2:
    request: TaskRequestV2
    settings: TaskSettingsV2
    parent_issue: TaskParentIssue
    project_items: tuple[ProjectExecutionItem, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.request, TaskRequestV2):
            raise TaskServiceError("created v2 request is invalid")
        if not isinstance(self.settings, TaskSettingsV2):
            raise TaskServiceError("created v2 settings are invalid")
        if not isinstance(self.parent_issue, TaskParentIssue):
            raise TaskServiceError("created v2 parent issue is invalid")
        if not isinstance(self.project_items, tuple):
            raise TaskServiceError("created v2 Project items are invalid")
        if len(self.project_items) != len(self.request.projects):
            raise TaskServiceError("created v2 Project item count does not match")
        for project, item in zip(self.request.projects, self.project_items, strict=True):
            if (
                item.idempotency_key
                != root_project_item_key(self.request.request_id, project.project_id)
                or item.parent_issue_number != self.parent_issue.number
                or item.project_repository != project.repository
                or item.state != "ready"
            ):
                raise TaskServiceError("created v2 Project item does not match")


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


class TaskIssueClientV2(Protocol):
    def find_issue(
        self,
        repository: str,
        request_id: str,
    ) -> TaskParentIssue | None: ...

    def create_issue(
        self,
        repository: str,
        title: str,
        body: str,
    ) -> TaskParentIssue: ...

    def get_issue(
        self,
        repository: str,
        issue_number: int,
    ) -> TaskParentIssue: ...

    def update_issue(
        self,
        repository: str,
        issue_number: int,
        *,
        title: str,
        body: str,
    ) -> TaskParentIssue: ...


class ProjectItemClient(Protocol):
    def find_items(
        self,
        management_repository: str,
        idempotency_key: str,
    ) -> tuple[ProjectExecutionItem, ...]: ...

    def create_item(
        self,
        management_repository: str,
        parent_issue_number: int,
        project_repository: str,
        idempotency_key: str,
        *,
        state: str,
    ) -> ProjectExecutionItem: ...

    def get_item(
        self,
        management_repository: str,
        item_id: str,
    ) -> ProjectExecutionItem: ...

    def release_item(
        self,
        management_repository: str,
        item_id: str,
    ) -> ProjectExecutionItem: ...


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


def _v2_marker(request: TaskRequestV2) -> str:
    payload = {
        "format_version": TASK_REQUEST_V2_FORMAT,
        "request_hash": request.request_hash,
        "request_id": request.request_id,
        "task_content_hash": request.task_content_hash,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{_V2_MARKER_START}\n{encoded}\n-->"


def read_task_marker_v2(body: str) -> dict[str, str]:
    """Read one v2 marker without accepting the v1 marker namespace."""

    if not isinstance(body, str):
        raise TaskServiceError("GitHub parent issue body must be text")
    matches = tuple(_V2_MARKER_PATTERN.finditer(body))
    if len(matches) != 1 or body.count(_V2_MARKER_START) != 1:
        raise TaskServiceError("GitHub parent issue must contain exactly one Task marker")
    try:
        value = json.loads(
            matches[0].group("json"),
            object_pairs_hook=_task_marker_object,
        )
    except json.JSONDecodeError as error:
        raise TaskServiceError("GitHub parent issue Task marker is invalid JSON") from error
    required = {
        "format_version",
        "request_id",
        "request_hash",
        "task_content_hash",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise TaskServiceError("GitHub parent issue Task marker has invalid fields")
    if value.get("format_version") != TASK_REQUEST_V2_FORMAT:
        raise TaskServiceError("GitHub parent issue Task marker has an unknown format")
    request_id = value.get("request_id")
    if not isinstance(request_id, str):
        raise TaskServiceError("GitHub parent issue Task marker request_id is invalid")
    try:
        parsed_request_id = UUID(request_id)
    except ValueError as error:
        raise TaskServiceError(
            "GitHub parent issue Task marker request_id is invalid"
        ) from error
    if str(parsed_request_id) != request_id:
        raise TaskServiceError("GitHub parent issue Task marker request_id is invalid")
    for field in ("request_hash", "task_content_hash"):
        if (
            not isinstance(value.get(field), str)
            or _SHA256_PATTERN.fullmatch(value[field]) is None
        ):
            raise TaskServiceError(
                f"GitHub parent issue Task marker {field} is invalid"
            )
    return {str(key): str(item) for key, item in value.items()}


def _render_v2_progress(
    request: TaskRequestV2,
    project_states: Mapping[str, str],
) -> str:
    if not isinstance(project_states, Mapping):
        raise TaskServiceError("Project progress must be a mapping")
    expected_ids = {project.project_id for project in request.projects}
    if set(project_states) != expected_ids:
        raise TaskServiceError("Project progress must cover every exact Project")
    lines = ("| Project | Build |", "| --- | --- |")
    rows: list[str] = []
    for project in request.projects:
        state = project_states[project.project_id]
        if type(state) is not str or state not in _V2_PROGRESS_LABELS:
            raise TaskServiceError("Project progress state is invalid")
        rows.append(f"| `{project.repository}` | {_V2_PROGRESS_LABELS[state]} |")
    return "\n".join((*lines, *rows))


def _require_v2_body_size(body: str) -> None:
    try:
        size = len(body.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise TaskServiceError("GitHub parent issue body is not valid UTF-8") from error
    if size > _V2_BODY_LIMIT_BYTES:
        raise TaskServiceError("GitHub parent issue body is too large")


def _split_v2_progress(body: str) -> tuple[str, str, str]:
    if body.count(V2_PROGRESS_START) != 1 or body.count(V2_PROGRESS_END) != 1:
        raise TaskServiceError(
            "GitHub parent issue progress delimiters must appear exactly once"
        )
    start = body.index(V2_PROGRESS_START) + len(V2_PROGRESS_START)
    end = body.index(V2_PROGRESS_END)
    if end <= start or body[start : start + 1] != "\n" or body[end - 1 : end] != "\n":
        raise TaskServiceError("GitHub parent issue progress delimiters are malformed")
    return body[:start], body[start:end], body[end:]


def build_task_issue_body_v2(
    request: TaskRequestV2,
    project_states: Mapping[str, str],
) -> str:
    """Build immutable v2 content around one Forge-owned progress section."""

    if not isinstance(request, TaskRequestV2):
        raise TaskServiceError("request must be TaskRequestV2")
    reserved = (
        _MARKER_START,
        _V2_MARKER_START,
        V2_PROGRESS_START,
        V2_PROGRESS_END,
    )
    content_values = (
        request.task_content.description,
        *request.task_content.acceptance_criteria,
    )
    if any(token in value for token in reserved for value in content_values):
        raise TaskServiceError("Task content contains a reserved v2 marker")
    criteria = "\n".join(
        f"{number}. {criterion}"
        for number, criterion in enumerate(
            request.task_content.acceptance_criteria,
            start=1,
        )
    )
    expiry = (
        "manual"
        if request.auto_merge_expires_at is None
        else request.auto_merge_expires_at.isoformat().replace("+00:00", "Z")
    )
    settings_lines = [
        f"- Task flow: `{request.task_flow.value}`",
        f"- Merge mode: `{request.merge_mode.value}`",
        f"- Auto-merge permission until: `{expiry}`",
        "- Projects:",
        *(f"  - `{project.repository}`" for project in request.projects),
    ]
    if request.merge_order is not None:
        repositories = {
            project.project_id: project.repository for project in request.projects
        }
        settings_lines.extend(
            (
                "- Merge order:",
                *(
                    f"  {number}. `{repositories[project_id]}`"
                    for number, project_id in enumerate(
                        request.merge_order,
                        start=1,
                    )
                ),
            )
        )
    progress = _render_v2_progress(request, project_states)
    body = "\n\n".join(
        (
            request.task_content.description,
            "## Acceptance Criteria\n\n" + criteria,
            "## Task Settings\n\n" + "\n".join(settings_lines),
            (
                "## Project Progress\n\n"
                f"{V2_PROGRESS_START}\n{progress}\n{V2_PROGRESS_END}"
            ),
            _v2_marker(request),
        )
    )
    _require_v2_body_size(body)
    return body


def _verify_v2_body(body: str, request: TaskRequestV2) -> None:
    _require_v2_body_size(body)
    marker = read_task_marker_v2(body)
    expected_marker = {
        "format_version": TASK_REQUEST_V2_FORMAT,
        "request_id": request.request_id,
        "request_hash": request.request_hash,
        "task_content_hash": request.task_content_hash,
    }
    for field, expected in expected_marker.items():
        if marker.get(field) != expected:
            raise TaskServiceError(
                f"GitHub parent issue Task marker {field} does not match"
            )
    expected_body = build_task_issue_body_v2(
        request,
        {project.project_id: "blocked" for project in request.projects},
    )
    prefix, _progress, suffix = _split_v2_progress(body)
    expected_prefix, _expected_progress, expected_suffix = _split_v2_progress(
        expected_body
    )
    if prefix != expected_prefix or suffix != expected_suffix:
        raise TaskServiceError("GitHub parent issue immutable content changed")


def verify_task_issue_v2_content(
    issue: TaskParentIssue,
    request: TaskRequestV2,
) -> None:
    """Verify the parent title, immutable body, marker, and open state."""

    if not isinstance(issue, TaskParentIssue):
        raise TaskServiceError("issue must be TaskParentIssue")
    if not isinstance(request, TaskRequestV2):
        raise TaskServiceError("request must be TaskRequestV2")
    if issue.state != "open":
        raise TaskServiceError("GitHub parent issue is closed")
    if issue.title != request.task_content.title:
        raise TaskServiceError("GitHub parent issue title does not match")
    _verify_v2_body(issue.body, request)


def replace_task_progress_v2(
    body: str,
    request: TaskRequestV2,
    project_states: Mapping[str, str],
) -> str:
    """Replace only the exact Forge-owned v2 progress interior."""

    if not isinstance(request, TaskRequestV2):
        raise TaskServiceError("request must be TaskRequestV2")
    _verify_v2_body(body, request)
    prefix, _progress, suffix = _split_v2_progress(body)
    updated = prefix + "\n" + _render_v2_progress(request, project_states) + "\n" + suffix
    _require_v2_body_size(updated)
    return updated


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


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _v2_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TaskServiceError("v2 event time must include a timezone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _require_v2_event_time(value: object) -> str:
    if type(value) is not str or not value.endswith("Z"):
        raise TaskServiceError("Task event time is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise TaskServiceError("Task event time is not canonical UTC") from error
    if _v2_timestamp(parsed) != value:
        raise TaskServiceError("Task event time is not canonical UTC")
    return value


def _is_v2_item_id(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and value == value.strip()
        and len(value) <= 512
    )


def _v2_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _request_project_json(request: TaskRequestV2) -> dict[str, str]:
    payload = json.loads(request.to_json())
    raw_projects = payload.get("projects")
    if not isinstance(raw_projects, list) or len(raw_projects) != len(request.projects):
        raise TaskServiceError("Task request Project payload is invalid")
    result: dict[str, str] = {}
    for project, raw_project in zip(request.projects, raw_projects, strict=True):
        if not isinstance(raw_project, dict) or raw_project.get("project_id") != project.project_id:
            raise TaskServiceError("Task request Project order is invalid")
        result[project.project_id] = _v2_json(raw_project)
    return result


class _TaskRegistryV2:
    """Keep all v2 SQLite comparisons exact and transaction-scoped."""

    def __init__(self, database: TaskDatabase) -> None:
        self._database = database

    @contextmanager
    def guard(self, request: TaskRequestV2) -> Iterator[sqlite3.Connection]:
        with self._database.transaction() as connection:
            self._require_request(connection, request)
            self._require_projects(connection, request)
            yield connection

    @staticmethod
    def require_no_lifecycle_barrier(
        connection: sqlite3.Connection,
        request: TaskRequestV2,
    ) -> None:
        placeholders = ",".join("?" for _ in _V2_LIFECYCLE_BARRIER_EVENTS)
        barrier = connection.execute(
            f"""
            SELECT event_type
            FROM task_events
            WHERE request_id = ? AND event_type IN ({placeholders})
            ORDER BY event_id
            LIMIT 1
            """,
            (request.request_id, *_V2_LIFECYCLE_BARRIER_EVENTS),
        ).fetchone()
        if barrier is not None:
            raise TaskServiceError(
                f"Task external write is blocked by lifecycle barrier {barrier[0]}"
            )

    def prepare(self, request: TaskRequestV2) -> None:
        request_payload = json.loads(request.to_json())
        project_json = _request_project_json(request)
        confirmed_at = str(request_payload["confirmed_at"])
        expected_request = (
            request.request_id,
            TASK_REQUEST_V2_FORMAT,
            request.to_json(),
            request.request_hash,
            request.management_repository,
            request.task_owner_host,
            request.confirmed_by,
            confirmed_at,
            request.replaces_request_id,
        )
        with self._database.transaction() as connection:
            existing = connection.execute(
                """
                SELECT request_id, format_version, request_json, request_hash,
                       management_repository, task_owner_host, confirmed_by,
                       confirmed_at, replaces_request_id
                FROM task_requests
                WHERE request_id = ?
                """,
                (request.request_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO task_requests (
                        request_id, format_version, request_json, request_hash,
                        management_repository, task_owner_host, confirmed_by,
                        confirmed_at, replaces_request_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    expected_request,
                )
                for project in request.projects:
                    connection.execute(
                        """
                        INSERT INTO task_projects (
                            request_id, project_id, task_settings_hash,
                            project_json, state, root_card_id, branch_name,
                            worktree_path, pr_url, head_commit, merge_commit,
                            updated_at
                        ) VALUES (?, ?, NULL, ?, 'prepared', NULL, NULL, NULL,
                                  NULL, NULL, NULL, ?)
                        """,
                        (
                            request.request_id,
                            project.project_id,
                            project_json[project.project_id],
                            confirmed_at,
                        ),
                    )
            elif tuple(existing) != expected_request:
                raise TaskServiceError(
                    "request_id is already bound to a different confirmed request"
                )
            self._require_request(connection, request)
            self._require_projects(connection, request)
            self._ensure_event(
                connection,
                request=request,
                event_key="request_prepared",
                event_type="request_prepared",
                event_json=_v2_json({"request_hash": request.request_hash}),
                occurred_at=confirmed_at,
            )
            self._require_request_prepared_event(connection, request)

    def parent_issue_number(
        self,
        connection: sqlite3.Connection,
        request: TaskRequestV2,
    ) -> int | None:
        rows = connection.execute(
            """
            SELECT event_key, task_settings_hash, project_id, event_json
            FROM task_events
            WHERE request_id = ? AND event_type = 'parent_issue_bound'
            """,
            (request.request_id,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise TaskServiceError("Task has duplicate parent issue bindings")
        row = rows[0]
        try:
            payload = json.loads(row[3])
        except json.JSONDecodeError as error:
            raise TaskServiceError("parent issue binding is invalid") from error
        if (
            row[0] != "parent_issue_bound"
            or row[1] is not None
            or row[2] is not None
            or not isinstance(payload, dict)
            or set(payload) != {"parent_issue_number", "request_hash"}
            or payload.get("request_hash") != request.request_hash
            or type(payload.get("parent_issue_number")) is not int
            or payload["parent_issue_number"] <= 0
        ):
            raise TaskServiceError("parent issue binding does not match request")
        if row[3] != _v2_json(
            {
                "parent_issue_number": payload["parent_issue_number"],
                "request_hash": request.request_hash,
            }
        ):
            raise TaskServiceError("parent issue binding is not canonical")
        return payload["parent_issue_number"]

    def bind_parent(
        self,
        connection: sqlite3.Connection,
        request: TaskRequestV2,
        issue_number: int,
        occurred_at: str,
    ) -> None:
        if type(issue_number) is not int or issue_number <= 0:
            raise TaskServiceError("parent issue number is invalid")
        self._ensure_event(
            connection,
            request=request,
            event_key="parent_issue_bound",
            event_type="parent_issue_bound",
            event_json=_v2_json(
                {
                    "parent_issue_number": issue_number,
                    "request_hash": request.request_hash,
                }
            ),
            occurred_at=occurred_at,
        )
        if self.parent_issue_number(connection, request) != issue_number:
            raise TaskServiceError("parent issue binding readback failed")

    def project_binding(
        self,
        connection: sqlite3.Connection,
        request: TaskRequestV2,
        project_id: str,
    ) -> tuple[str, str | None, str | None]:
        row = connection.execute(
            """
            SELECT state, root_card_id, task_settings_hash
            FROM task_projects
            WHERE request_id = ? AND project_id = ?
            """,
            (request.request_id, project_id),
        ).fetchone()
        if row is None:
            raise TaskServiceError("Task Project registry row is missing")
        state, root_card_id, settings_hash = tuple(row)
        if state == "prepared":
            if root_card_id is not None or settings_hash is not None:
                raise TaskServiceError("prepared Project registry row is inconsistent")
        elif state == "bound":
            if not _is_v2_item_id(root_card_id):
                raise TaskServiceError("stored Project root card ID is invalid")
            if settings_hash is not None:
                raise TaskServiceError("bound Project registry row is inconsistent")
        else:
            if not _is_v2_item_id(root_card_id):
                raise TaskServiceError("stored Project root card ID is invalid")
            if not isinstance(settings_hash, str):
                raise TaskServiceError("active Project registry row is inconsistent")
        return state, root_card_id, settings_hash

    def bind_project_item(
        self,
        connection: sqlite3.Connection,
        request: TaskRequestV2,
        project_id: str,
        item: ProjectExecutionItem,
        occurred_at: str,
    ) -> None:
        state, root_card_id, settings_hash = self.project_binding(
            connection,
            request,
            project_id,
        )
        if state == "prepared":
            updated = connection.execute(
                """
                UPDATE task_projects
                SET state = 'bound', root_card_id = ?, updated_at = ?
                WHERE request_id = ? AND project_id = ?
                  AND state = 'prepared' AND root_card_id IS NULL
                  AND task_settings_hash IS NULL
                """,
                (
                    item.item_id,
                    occurred_at,
                    request.request_id,
                    project_id,
                ),
            )
            if updated.rowcount != 1:
                raise TaskServiceError("Project item binding update failed")
        elif root_card_id != item.item_id:
            raise TaskServiceError("Project item does not match bound root card")
        if settings_hash is not None and state == "bound":
            raise TaskServiceError("bound Project unexpectedly has active settings")
        key = root_project_item_key(request.request_id, project_id)
        self._ensure_event(
            connection,
            request=request,
            event_key=f"project_item_bound:{project_id}",
            event_type="project_item_bound",
            event_json=_v2_json(
                {"idempotency_key": key, "root_card_id": item.item_id}
            ),
            occurred_at=occurred_at,
            project_id=project_id,
        )
        self._require_project_binding_event(
            connection,
            request,
            project_id,
            item.item_id,
        )

    def activate(
        self,
        request: TaskRequestV2,
        occurred_at: str,
    ) -> TaskSettingsV2:
        with self._database.transaction() as connection:
            self._require_request(connection, request)
            self._require_projects(connection, request)
            parent_issue_number = self.parent_issue_number(connection, request)
            if parent_issue_number is None:
                raise TaskServiceError("parent issue is not bound")
            root_ids: list[str] = []
            for project in request.projects:
                state, root_card_id, settings_hash = self.project_binding(
                    connection,
                    request,
                    project.project_id,
                )
                if state not in {"bound", "ready"} or root_card_id is None:
                    raise TaskServiceError("not every Project root is bound")
                if state == "bound" and settings_hash is not None:
                    raise TaskServiceError("bound Project has unexpected settings")
                root_ids.append(root_card_id)
                self._require_project_binding_event(
                    connection,
                    request,
                    project.project_id,
                    root_card_id,
                )
            if len(root_ids) != len(set(root_ids)):
                raise TaskServiceError("Project roots contain duplicate card IDs")
            self.require_no_lifecycle_barrier(connection, request)
            expected = TaskSettingsV2.create(
                request=request,
                parent_issue_number=parent_issue_number,
            )
            existing = connection.execute(
                """
                SELECT task_settings_hash, request_id, request_hash,
                       format_version, settings_json, management_repository,
                       parent_issue_number, task_owner_host, confirmed_at
                FROM task_settings_v2
                WHERE request_id = ?
                """,
                (request.request_id,),
            ).fetchone()
            if existing is None:
                payload = json.loads(expected.to_json())
                connection.execute(
                    """
                    INSERT INTO task_settings_v2 (
                        task_settings_hash, request_id, request_hash,
                        format_version, settings_json, management_repository,
                        parent_issue_number, task_owner_host, confirmed_at
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
                      AND task_settings_hash IS NULL
                    """,
                    (
                        expected.task_settings_hash,
                        occurred_at,
                        request.request_id,
                    ),
                )
                if updated.rowcount != len(request.projects):
                    raise TaskServiceError("not every Project became ready atomically")
                event_payload = _v2_json(
                    {"task_settings_hash": expected.task_settings_hash}
                )
                for event_type in ("settings_activated", "active"):
                    self._ensure_event(
                        connection,
                        request=request,
                        event_key=event_type,
                        event_type=event_type,
                        event_json=event_payload,
                        occurred_at=occurred_at,
                        task_settings_hash=expected.task_settings_hash,
                    )
                self._ensure_event(
                    connection,
                    request=request,
                    event_key="dispatch_ready",
                    event_type="dispatch_ready",
                    event_json=_v2_json(
                        {
                            "project_ids": [
                                project.project_id for project in request.projects
                            ],
                            "task_settings_hash": expected.task_settings_hash,
                        }
                    ),
                    occurred_at=occurred_at,
                    task_settings_hash=expected.task_settings_hash,
                )
            else:
                self._require_settings_row(existing, expected, request)
                for project in request.projects:
                    state, _root_id, settings_hash = self.project_binding(
                        connection,
                        request,
                        project.project_id,
                    )
                    if state == "bound" or settings_hash != expected.task_settings_hash:
                        raise TaskServiceError("active Project registry does not match settings")
                self._require_activation_events(connection, request, expected)
            self._require_settings_readback(connection, request, expected)
            self._require_activation_events(connection, request, expected)
            return expected

    def require_active_external_write(self, request: TaskRequestV2) -> None:
        """Fail closed unless the exact active registry still permits writes."""

        with self.guard(request) as connection:
            self.require_no_lifecycle_barrier(connection, request)
            parent_issue_number = self.parent_issue_number(connection, request)
            if parent_issue_number is None:
                raise TaskServiceError("parent issue is not bound")
            expected = TaskSettingsV2.create(
                request=request,
                parent_issue_number=parent_issue_number,
            )
            self._require_settings_readback(connection, request, expected)
            root_ids: list[str] = []
            for project in request.projects:
                state, root_card_id, settings_hash = self.project_binding(
                    connection,
                    request,
                    project.project_id,
                )
                if (
                    state != "ready"
                    or root_card_id is None
                    or settings_hash != expected.task_settings_hash
                ):
                    raise TaskServiceError(
                        "Project registry is not active for external write"
                    )
                self._require_project_binding_event(
                    connection,
                    request,
                    project.project_id,
                    root_card_id,
                )
                root_ids.append(root_card_id)
            if len(root_ids) != len(set(root_ids)):
                raise TaskServiceError("Project roots contain duplicate card IDs")
            self._require_activation_events(connection, request, expected)

    def root_card_ids(self, request: TaskRequestV2) -> tuple[str, ...]:
        with self._database.read() as connection:
            self._require_request(connection, request)
            self._require_projects(connection, request)
            root_ids: list[str] = []
            for project in request.projects:
                _state, root_card_id, _settings_hash = self.project_binding(
                    connection,
                    request,
                    project.project_id,
                )
                if root_card_id is None:
                    raise TaskServiceError("Project root card is not bound")
                root_ids.append(root_card_id)
            if len(root_ids) != len(set(root_ids)):
                raise TaskServiceError("Project roots contain duplicate card IDs")
            return tuple(root_ids)

    def _require_request(
        self,
        connection: sqlite3.Connection,
        request: TaskRequestV2,
    ) -> None:
        payload = json.loads(request.to_json())
        expected = (
            request.request_id,
            TASK_REQUEST_V2_FORMAT,
            request.to_json(),
            request.request_hash,
            request.management_repository,
            request.task_owner_host,
            request.confirmed_by,
            payload["confirmed_at"],
            request.replaces_request_id,
        )
        row = connection.execute(
            """
            SELECT request_id, format_version, request_json, request_hash,
                   management_repository, task_owner_host, confirmed_by,
                   confirmed_at, replaces_request_id
            FROM task_requests
            WHERE request_id = ?
            """,
            (request.request_id,),
        ).fetchone()
        if row is None or tuple(row) != expected:
            raise TaskServiceError(
                "request_id is already bound to a different confirmed request"
            )
        for event_row in connection.execute(
            "SELECT occurred_at FROM task_events WHERE request_id = ?",
            (request.request_id,),
        ):
            _require_v2_event_time(event_row[0])

    def _require_projects(
        self,
        connection: sqlite3.Connection,
        request: TaskRequestV2,
    ) -> None:
        expected = _request_project_json(request)
        rows = connection.execute(
            """
            SELECT project_id, project_json
            FROM task_projects
            WHERE request_id = ?
            """,
            (request.request_id,),
        ).fetchall()
        actual = {str(row[0]): str(row[1]) for row in rows}
        if len(actual) != len(rows) or actual != expected:
            raise TaskServiceError("Task Project registry does not match request")

    @staticmethod
    def _ensure_event(
        connection: sqlite3.Connection,
        *,
        request: TaskRequestV2,
        event_key: str,
        event_type: str,
        event_json: str,
        occurred_at: str,
        task_settings_hash: str | None = None,
        project_id: str | None = None,
    ) -> None:
        row = connection.execute(
            """
            SELECT task_settings_hash, project_id, event_type, event_json,
                   occurred_at
            FROM task_events
            WHERE request_id = ? AND event_key = ?
            """,
            (request.request_id, event_key),
        ).fetchone()
        expected = (task_settings_hash, project_id, event_type, event_json)
        if row is None:
            connection.execute(
                """
                INSERT INTO task_events (
                    request_id, task_settings_hash, project_id, event_type,
                    event_key, event_json, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.request_id,
                    task_settings_hash,
                    project_id,
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
                (request.request_id, event_key),
            ).fetchone()
        if (
            row is None
            or tuple(row[:4]) != expected
        ):
            raise TaskServiceError(f"Task event {event_key} does not match")
        _require_v2_event_time(row[4])

    def _require_project_binding_event(
        self,
        connection: sqlite3.Connection,
        request: TaskRequestV2,
        project_id: str,
        root_card_id: str,
    ) -> None:
        rows = connection.execute(
            """
            SELECT event_key, task_settings_hash, project_id, event_json
            FROM task_events
            WHERE request_id = ? AND event_type = 'project_item_bound'
              AND project_id = ?
            """,
            (request.request_id, project_id),
        ).fetchall()
        expected_json = _v2_json(
            {
                "idempotency_key": root_project_item_key(
                    request.request_id,
                    project_id,
                ),
                "root_card_id": root_card_id,
            }
        )
        if (
            len(rows) != 1
            or tuple(rows[0])
            != (
                f"project_item_bound:{project_id}",
                None,
                project_id,
                expected_json,
            )
        ):
            raise TaskServiceError("Project item binding event does not match")

    @staticmethod
    def _require_request_prepared_event(
        connection: sqlite3.Connection,
        request: TaskRequestV2,
    ) -> None:
        rows = connection.execute(
            """
            SELECT event_key, task_settings_hash, project_id, event_json
            FROM task_events
            WHERE request_id = ? AND event_type = 'request_prepared'
            """,
            (request.request_id,),
        ).fetchall()
        if (
            len(rows) != 1
            or tuple(rows[0])
            != (
                "request_prepared",
                None,
                None,
                _v2_json({"request_hash": request.request_hash}),
            )
        ):
            raise TaskServiceError("request_prepared event does not match")

    @staticmethod
    def _require_settings_row(
        row: sqlite3.Row,
        expected: TaskSettingsV2,
        request: TaskRequestV2,
    ) -> None:
        try:
            parsed = TaskSettingsV2.from_json(row[4], request=request)
        except TaskSettingsV2Error as error:
            raise TaskServiceError("stored v2 Task settings are invalid") from error
        payload = json.loads(expected.to_json())
        expected_row = (
            expected.task_settings_hash,
            expected.request_id,
            expected.request_hash,
            expected.format_version,
            expected.to_json(),
            expected.management_repository,
            expected.parent_issue_number,
            expected.task_owner_host,
            payload["confirmed_at"],
        )
        if parsed != expected or tuple(row) != expected_row:
            raise TaskServiceError("stored v2 Task settings do not match request")

    def _require_settings_readback(
        self,
        connection: sqlite3.Connection,
        request: TaskRequestV2,
        expected: TaskSettingsV2,
    ) -> None:
        row = connection.execute(
            """
            SELECT task_settings_hash, request_id, request_hash,
                   format_version, settings_json, management_repository,
                   parent_issue_number, task_owner_host, confirmed_at
            FROM task_settings_v2
            WHERE request_id = ?
            """,
            (request.request_id,),
        ).fetchone()
        if row is None:
            raise TaskServiceError("v2 Task settings readback is missing")
        self._require_settings_row(row, expected, request)

    def _require_activation_events(
        self,
        connection: sqlite3.Connection,
        request: TaskRequestV2,
        settings: TaskSettingsV2,
    ) -> None:
        rows = connection.execute(
            """
            SELECT event_type, event_key, task_settings_hash, project_id,
                   event_json
            FROM task_events
            WHERE request_id = ?
              AND event_type IN ('settings_activated', 'active', 'dispatch_ready')
            ORDER BY event_id
            """,
            (request.request_id,),
        ).fetchall()
        common_json = _v2_json(
            {"task_settings_hash": settings.task_settings_hash}
        )
        expected = (
            (
                "settings_activated",
                "settings_activated",
                settings.task_settings_hash,
                None,
                common_json,
            ),
            (
                "active",
                "active",
                settings.task_settings_hash,
                None,
                common_json,
            ),
            (
                "dispatch_ready",
                "dispatch_ready",
                settings.task_settings_hash,
                None,
                _v2_json(
                    {
                        "project_ids": [
                            project.project_id for project in request.projects
                        ],
                        "task_settings_hash": settings.task_settings_hash,
                    }
                ),
            ),
        )
        if tuple(tuple(row) for row in rows) != expected:
            raise TaskServiceError("v2 Task activation events do not match")


class TaskServiceV2:
    """Create one central parent and blocked Build root per selected Project."""

    def __init__(
        self,
        database: TaskDatabase,
        issues: TaskIssueClientV2,
        project_items: ProjectItemClient,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(database, TaskDatabase):
            raise TaskServiceError("database must be TaskDatabase")
        self._registry = _TaskRegistryV2(database)
        self._issues = issues
        self._project_items = project_items
        self._clock = clock or _utc_now

    def create_task(self, request: TaskRequestV2) -> CreatedTaskV2:
        if not isinstance(request, TaskRequestV2):
            raise TaskServiceError("request must be TaskRequestV2")
        if request.replaces_request_id is not None:
            raise TaskServiceError(
                "replacement requests are unavailable until Task 12"
            )
        if len(request.task_content.title) > 256:
            raise TaskServiceError("Task title is longer than GitHub allows")
        blocked_states = {
            project.project_id: "blocked" for project in request.projects
        }
        initial_body = build_task_issue_body_v2(request, blocked_states)
        try:
            self._registry.prepare(request)
        except TaskDatabaseError as error:
            raise TaskServiceError("Task request preparation failed") from error
        parent = self._ensure_parent(request, initial_body)
        for project in request.projects:
            self._ensure_project_item(request, parent, project)
        try:
            settings = self._registry.activate(request, self._now())
        except TaskDatabaseError as error:
            raise TaskServiceError("Task activation failed") from error
        root_ids = self._registry.root_card_ids(request)
        released = tuple(
            self._release_project_item(request, parent, project, root_card_id)
            for project, root_card_id in zip(
                request.projects,
                root_ids,
                strict=True,
            )
        )
        projected_parent = self._project_parent_progress(request, parent)
        return CreatedTaskV2(
            request=request,
            settings=settings,
            parent_issue=projected_parent,
            project_items=released,
        )

    def _ensure_parent(
        self,
        request: TaskRequestV2,
        initial_body: str,
    ) -> TaskParentIssue:
        try:
            with self._registry.guard(request) as connection:
                issue_number = self._registry.parent_issue_number(
                    connection,
                    request,
                )
                if issue_number is None:
                    issue = self._find_parent(request)
                    if issue is None:
                        self._registry.require_no_lifecycle_barrier(
                            connection,
                            request,
                        )
                        issue = self._create_parent(request, initial_body)
                    self._require_parent(issue, request)
                    if issue.body != initial_body:
                        raise TaskServiceError(
                            "unbound parent issue progress does not match"
                        )
                    readback = self._get_parent(request, issue.number)
                    self._require_parent(readback, request)
                    if readback != issue or readback.body != initial_body:
                        raise TaskServiceError("parent issue readback does not match")
                    self._registry.bind_parent(
                        connection,
                        request,
                        readback.number,
                        self._now(),
                    )
                    return readback
                issue = self._get_parent(request, issue_number)
                self._require_parent(issue, request)
                return issue
        except TaskDatabaseError as error:
            raise TaskServiceError("parent issue binding failed") from error

    def _ensure_project_item(
        self,
        request: TaskRequestV2,
        parent: TaskParentIssue,
        project: TaskProject,
    ) -> ProjectExecutionItem:
        key = root_project_item_key(request.request_id, project.project_id)
        try:
            with self._registry.guard(request) as connection:
                bound_parent = self._registry.parent_issue_number(connection, request)
                if bound_parent != parent.number:
                    raise TaskServiceError("Project item parent binding changed")
                state, root_card_id, _settings_hash = self._registry.project_binding(
                    connection,
                    request,
                    project.project_id,
                )
                if root_card_id is None:
                    matches = self._find_project_items(request, key)
                    if len(matches) > 1:
                        raise TaskServiceError(
                            "Project item key matched more than one root"
                        )
                    item = (
                        matches[0]
                        if matches
                        else self._create_project_item_after_guard(
                            connection,
                            request,
                            parent,
                            project.repository,
                            key,
                        )
                    )
                    self._require_project_item(
                        item,
                        parent,
                        project.repository,
                        key,
                        allowed_states={"blocked"},
                    )
                    readback = self._get_project_item(request, item.item_id)
                    self._require_project_item(
                        readback,
                        parent,
                        project.repository,
                        key,
                        allowed_states={"blocked"},
                    )
                    if readback != item:
                        raise TaskServiceError("Project item readback does not match")
                    self._registry.bind_project_item(
                        connection,
                        request,
                        project.project_id,
                        readback,
                        self._now(),
                    )
                    return readback
                item = self._get_project_item(request, root_card_id)
                if state == "bound" and item.state != "blocked":
                    raise TaskServiceError(
                        "Project item was released before activation"
                    )
                self._require_project_item(
                    item,
                    parent,
                    project.repository,
                    key,
                    allowed_states=(
                        {"blocked"}
                        if state == "bound"
                        else {"blocked", "ready"}
                    ),
                )
                if state == "prepared":
                    raise TaskServiceError("prepared Project unexpectedly has a root")
                self._registry.bind_project_item(
                    connection,
                    request,
                    project.project_id,
                    item,
                    self._now(),
                )
                return item
        except TaskDatabaseError as error:
            raise TaskServiceError("Project item binding failed") from error

    def _release_project_item(
        self,
        request: TaskRequestV2,
        parent: TaskParentIssue,
        project: TaskProject,
        root_card_id: str,
    ) -> ProjectExecutionItem:
        key = root_project_item_key(request.request_id, project.project_id)
        item = self._get_project_item(request, root_card_id)
        self._require_project_item(
            item,
            parent,
            project.repository,
            key,
            allowed_states={"blocked", "ready"},
        )
        if item.state == "blocked":
            self._registry.require_active_external_write(request)
            try:
                item = self._project_items.release_item(
                    request.management_repository,
                    item.item_id,
                )
            except Exception as error:
                raise TaskServiceError("Project item release failed") from error
            self._require_project_item(
                item,
                parent,
                project.repository,
                key,
                allowed_states={"ready"},
            )
            item = self._get_project_item(request, item.item_id)
        self._registry.require_active_external_write(request)
        self._require_project_item(
            item,
            parent,
            project.repository,
            key,
            allowed_states={"ready"},
        )
        return item

    def _project_parent_progress(
        self,
        request: TaskRequestV2,
        parent: TaskParentIssue,
    ) -> TaskParentIssue:
        issue = self._get_parent(request, parent.number)
        self._require_parent(issue, request)
        expected_body = replace_task_progress_v2(
            issue.body,
            request,
            {project.project_id: "ready" for project in request.projects},
        )
        if issue.body != expected_body:
            self._registry.require_active_external_write(request)
            try:
                updated = self._issues.update_issue(
                    request.management_repository,
                    issue.number,
                    title=request.task_content.title,
                    body=expected_body,
                )
            except Exception as error:
                raise TaskServiceError("parent progress update failed") from error
            self._require_parent(updated, request)
            issue = self._get_parent(request, issue.number)
        self._require_parent(issue, request)
        if issue.body != expected_body:
            raise TaskServiceError("parent progress readback does not match")
        self._registry.require_active_external_write(request)
        return issue

    def _find_parent(self, request: TaskRequestV2) -> TaskParentIssue | None:
        try:
            issue = self._issues.find_issue(
                request.management_repository,
                request.request_id,
            )
        except Exception as error:
            raise TaskServiceError("parent issue lookup failed") from error
        if issue is not None and not isinstance(issue, TaskParentIssue):
            raise TaskServiceError("parent issue lookup returned an invalid value")
        return issue

    def _create_parent(
        self,
        request: TaskRequestV2,
        body: str,
    ) -> TaskParentIssue:
        try:
            issue = self._issues.create_issue(
                request.management_repository,
                request.task_content.title,
                body,
            )
        except Exception as error:
            raise TaskServiceError("parent issue creation failed") from error
        if not isinstance(issue, TaskParentIssue):
            raise TaskServiceError("parent issue creation returned an invalid value")
        return issue

    def _get_parent(
        self,
        request: TaskRequestV2,
        issue_number: int,
    ) -> TaskParentIssue:
        try:
            issue = self._issues.get_issue(
                request.management_repository,
                issue_number,
            )
        except Exception as error:
            raise TaskServiceError("parent issue readback failed") from error
        if not isinstance(issue, TaskParentIssue):
            raise TaskServiceError("parent issue readback returned an invalid value")
        return issue

    def _find_project_items(
        self,
        request: TaskRequestV2,
        key: str,
    ) -> tuple[ProjectExecutionItem, ...]:
        try:
            items = self._project_items.find_items(
                request.management_repository,
                key,
            )
        except Exception as error:
            raise TaskServiceError("Project item lookup failed") from error
        if not isinstance(items, tuple) or any(
            not isinstance(item, ProjectExecutionItem) for item in items
        ):
            raise TaskServiceError("Project item lookup returned an invalid value")
        return items

    def _create_project_item(
        self,
        request: TaskRequestV2,
        parent: TaskParentIssue,
        project_repository: str,
        key: str,
    ) -> ProjectExecutionItem:
        try:
            item = self._project_items.create_item(
                request.management_repository,
                parent.number,
                project_repository,
                key,
                state="blocked",
            )
        except Exception as error:
            raise TaskServiceError("Project item creation failed") from error
        if not isinstance(item, ProjectExecutionItem):
            raise TaskServiceError("Project item creation returned an invalid value")
        return item

    def _create_project_item_after_guard(
        self,
        connection: sqlite3.Connection,
        request: TaskRequestV2,
        parent: TaskParentIssue,
        project_repository: str,
        key: str,
    ) -> ProjectExecutionItem:
        self._registry.require_no_lifecycle_barrier(connection, request)
        return self._create_project_item(
            request,
            parent,
            project_repository,
            key,
        )

    def _get_project_item(
        self,
        request: TaskRequestV2,
        item_id: str,
    ) -> ProjectExecutionItem:
        try:
            item = self._project_items.get_item(
                request.management_repository,
                item_id,
            )
        except Exception as error:
            raise TaskServiceError("Project item readback failed") from error
        if not isinstance(item, ProjectExecutionItem):
            raise TaskServiceError("Project item readback returned an invalid value")
        return item

    @staticmethod
    def _require_parent(
        issue: TaskParentIssue,
        request: TaskRequestV2,
    ) -> None:
        verify_task_issue_v2_content(issue, request)

    @staticmethod
    def _require_project_item(
        item: ProjectExecutionItem,
        parent: TaskParentIssue,
        project_repository: str,
        key: str,
        *,
        allowed_states: set[str],
    ) -> None:
        if (
            not isinstance(item, ProjectExecutionItem)
            or item.idempotency_key != key
            or item.parent_issue_number != parent.number
            or item.project_repository != project_repository
            or item.state not in allowed_states
        ):
            raise TaskServiceError("Project item does not match exact root binding")

    def _now(self) -> str:
        return _v2_timestamp(self._clock())
