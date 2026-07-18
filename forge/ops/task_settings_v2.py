"""Exact immutable JSON contracts for v2 Forge Task requests and settings."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import InitVar, dataclass
from datetime import UTC, datetime
from uuid import UUID

from .task_options import MergeMode, Mode, TaskFlow
from .task_projects import (
    TaskProject,
    TaskProjectError,
    _validate_task_project_live,
    _validate_repository,
)
from .task_settings import (
    MAX_AUTO_MERGE_DURATION,
    TaskContent,
    TaskSettingsError,
    task_content_hash,
)


TASK_REQUEST_V2_FORMAT = "forge-task-request/v2"
TASK_SETTINGS_V2_FORMAT = "forge-task-settings/v2"

_REQUEST_STATUS = "prepared"
_SETTINGS_STATUS = "active"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_AUTO_EXPIRY_UNSET = object()
_INVALID_JSON_INTEGER = object()

_REQUEST_FIELDS = frozenset(
    {
        "format_version",
        "request_id",
        "management_repository",
        "mode",
        "task_content",
        "task_content_hash",
        "task_flow",
        "merge_mode",
        "merge_order",
        "projects",
        "task_owner_host",
        "confirmed_by",
        "confirmed_at",
        "auto_merge_expires_at",
        "replaces_request_id",
        "request_hash",
        "status",
    }
)
_SETTINGS_FIELDS = frozenset(
    {
        "format_version",
        "request_id",
        "request_hash",
        "management_repository",
        "parent_issue_number",
        "mode",
        "task_content_hash",
        "task_flow",
        "merge_mode",
        "merge_order",
        "projects",
        "task_owner_host",
        "confirmed_by",
        "confirmed_at",
        "auto_merge_expires_at",
        "task_settings_hash",
        "status",
    }
)
_TASK_CONTENT_FIELDS = frozenset(
    {"title", "description", "acceptance_criteria"}
)


class TaskSettingsV2Error(ValueError):
    """Raised when a v2 request or settings record is not exact and canonical."""


@dataclass(frozen=True, slots=True)
class _ParseFailure:
    message: str


def _try_utf8_encode(value: str) -> bytes | None:
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError:
        return None


def _canonical_hash(payload: Mapping[str, object]) -> str:
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    encoded = _try_utf8_encode(rendered)
    if encoded is None:
        payload = {}
        rendered = ""
        raise TaskSettingsV2Error("record text must be valid UTF-8") from None
    return hashlib.sha256(encoded).hexdigest()


def _canonical_json(payload: Mapping[str, object]) -> str:
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    encoded = _try_utf8_encode(rendered)
    if encoded is None:
        payload = {}
        rendered = ""
        raise TaskSettingsV2Error("record text must be valid UTF-8") from None
    return encoded.decode("utf-8")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise TaskSettingsV2Error("JSON contains a duplicate object key")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> object:
    del value
    raise TaskSettingsV2Error("JSON contains a non-standard number")


def _parse_json_integer(value: str) -> object:
    parsed: int | None = None
    try:
        parsed = int(value)
    except ValueError:
        pass
    if parsed is None:
        value = ""
        return _INVALID_JSON_INTEGER
    return parsed


def _contains_invalid_json_integer(value: object) -> bool:
    pending = [value]
    while pending:
        item = pending.pop()
        if item is _INVALID_JSON_INTEGER:
            return True
        if type(item) is dict:
            pending.extend(item.values())
        elif type(item) is list:
            pending.extend(item)
    return False


def _load_json_object(raw: object, label: str) -> dict[str, object]:
    if type(raw) is not str:
        raise TaskSettingsV2Error(f"{label} JSON must be text")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
            parse_int=_parse_json_integer,
        )
    except TaskSettingsV2Error:
        raise
    except (json.JSONDecodeError, RecursionError):
        raise TaskSettingsV2Error(f"{label} JSON is invalid") from None
    if _contains_invalid_json_integer(value):
        raw = None
        value = None
        raise TaskSettingsV2Error(f"{label} JSON contains an invalid integer")
    if type(value) is not dict:
        raise TaskSettingsV2Error(f"{label} JSON must be an object")
    return value


def _require_fields(
    payload: Mapping[str, object],
    expected: frozenset[str],
    label: str,
) -> None:
    if set(payload) != expected:
        raise TaskSettingsV2Error(f"{label} fields must match the exact schema")


def _validate_uuid(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise TaskSettingsV2Error(f"{field_name} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except ValueError:
        raise TaskSettingsV2Error(
            f"{field_name} must be a canonical UUID"
        ) from None
    if str(parsed) != value:
        raise TaskSettingsV2Error(f"{field_name} must be a canonical UUID")
    return value


def _validate_optional_uuid(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _validate_uuid(value, field_name)


def _validate_hash(value: object, field_name: str) -> str:
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise TaskSettingsV2Error(
            f"{field_name} must be a lowercase SHA-256"
        )
    return value


def _is_json_renderable_positive_integer(value: object) -> bool:
    if type(value) is not int or value <= 0:
        return False
    try:
        str(value)
    except ValueError:
        return False
    return True


def _validate_utf8_text(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise TaskSettingsV2Error(f"{field_name} must be valid UTF-8 text")
    if _try_utf8_encode(value) is None:
        value = None
        raise TaskSettingsV2Error(
            f"{field_name} must be valid UTF-8 text"
        ) from None
    return value


def _validate_repository_field(value: object, field_name: str) -> str:
    repository: str | None = None
    try:
        repository = _validate_repository(value)
    except TaskProjectError:
        pass
    if repository is None:
        value = None
        raise TaskSettingsV2Error(
            f"{field_name} must use canonical OWNER/REPO format"
        ) from None
    return repository


def _normalize_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TaskSettingsV2Error(
            f"{field_name} must be a timezone-aware datetime"
        )
    if value.utcoffset() is None:
        raise TaskSettingsV2Error(
            f"{field_name} must be a timezone-aware datetime"
        )
    return value.astimezone(UTC)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object, field_name: str) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise TaskSettingsV2Error(
            f"{field_name} must be canonical RFC 3339 UTC Z"
        )
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError:
        raise TaskSettingsV2Error(
            f"{field_name} must be canonical RFC 3339 UTC Z"
        ) from None
    normalized = _normalize_datetime(parsed, field_name)
    if _format_timestamp(normalized) != value:
        raise TaskSettingsV2Error(
            f"{field_name} must be canonical RFC 3339 UTC Z"
        )
    return normalized


def _validate_confirmed_by(value: object) -> str:
    value = _validate_utf8_text(value, "confirmed_by")
    if not value.strip():
        raise TaskSettingsV2Error("confirmed_by must be non-empty text")
    return value


def _validate_mode(value: object) -> Mode:
    if value is not Mode.TASK:
        raise TaskSettingsV2Error("mode must be task")
    return Mode.TASK


def _parse_mode(value: object) -> Mode:
    if value != Mode.TASK.value or type(value) is not str:
        raise TaskSettingsV2Error("mode must be task")
    return Mode.TASK


def _validate_task_flow(value: object) -> TaskFlow:
    if not isinstance(value, TaskFlow):
        raise TaskSettingsV2Error("task_flow must be a TaskFlow")
    return value


def _parse_task_flow(value: object) -> TaskFlow:
    if type(value) is not str:
        raise TaskSettingsV2Error("task_flow is invalid")
    try:
        return TaskFlow(value)
    except ValueError:
        raise TaskSettingsV2Error("task_flow is invalid") from None


def _validate_merge_mode(value: object) -> MergeMode:
    if not isinstance(value, MergeMode):
        raise TaskSettingsV2Error("merge_mode must be a MergeMode")
    return value


def _parse_merge_mode(value: object) -> MergeMode:
    if type(value) is not str:
        raise TaskSettingsV2Error("merge_mode is invalid")
    try:
        return MergeMode(value)
    except ValueError:
        raise TaskSettingsV2Error("merge_mode is invalid") from None


def _task_content_payload(content: TaskContent) -> dict[str, object]:
    return {
        "title": content.title,
        "description": content.description,
        "acceptance_criteria": list(content.acceptance_criteria),
    }


def _validate_task_content(content: object) -> TaskContent:
    if not isinstance(content, TaskContent):
        raise TaskSettingsV2Error("task_content must be TaskContent")
    _validate_utf8_text(content.title, "task_content.title")
    _validate_utf8_text(content.description, "task_content.description")
    for criterion in content.acceptance_criteria:
        _validate_utf8_text(criterion, "task_content.acceptance_criteria")
    return content


def _parse_task_content(value: object) -> TaskContent:
    if type(value) is not dict:
        raise TaskSettingsV2Error("task_content must be an object")
    _require_fields(value, _TASK_CONTENT_FIELDS, "task_content")
    criteria = value["acceptance_criteria"]
    if type(criteria) is not list:
        raise TaskSettingsV2Error("acceptance_criteria must be an array")
    content: TaskContent | None = None
    try:
        content = TaskContent(
            title=value["title"],
            description=value["description"],
            acceptance_criteria=tuple(criteria),
        )
    except TaskSettingsError:
        pass
    if content is None:
        value = None
        criteria = None
        raise TaskSettingsV2Error("task_content is invalid") from None
    return _validate_task_content(content)


def _project_payload(project: TaskProject) -> dict[str, str]:
    return {
        "project_id": project.project_id,
        "repository": project.repository,
        "workspace": project.workspace,
        "remote_name": project.remote_name,
        "base_branch": project.base_branch,
        "base_commit": project.base_commit,
        "host_id": project.host_id,
    }


def _parse_projects(value: object) -> tuple[TaskProject, ...]:
    if type(value) is not list:
        raise TaskSettingsV2Error("projects must be an array")
    projects: list[TaskProject] = []
    for item in value:
        if type(item) is not dict:
            raise TaskSettingsV2Error("each project must be an object")
        try:
            projects.append(TaskProject.from_mapping(item))
        except TaskProjectError:
            raise TaskSettingsV2Error("project binding is invalid") from None
    return tuple(projects)


def _project_sort_key(project: TaskProject) -> tuple[str, str, str]:
    return (project.repository, project.workspace, project.base_branch)


def _validate_projects(
    projects: object,
    task_owner_host: str,
    *,
    require_canonical_order: bool,
    require_live_workspace: bool = False,
) -> tuple[TaskProject, ...]:
    if type(projects) is not tuple or not projects:
        raise TaskSettingsV2Error("projects must contain at least one TaskProject")
    if any(not isinstance(project, TaskProject) for project in projects):
        raise TaskSettingsV2Error("projects must contain TaskProject values")
    typed_projects = projects
    canonical = tuple(sorted(typed_projects, key=_project_sort_key))
    if require_canonical_order and typed_projects != canonical:
        raise TaskSettingsV2Error("projects are not in canonical order")
    repositories: set[str] = set()
    for project in canonical:
        if require_live_workspace:
            project_is_live = True
            try:
                _validate_task_project_live(project)
            except TaskProjectError:
                project_is_live = False
            if not project_is_live:
                project = None
                raise TaskSettingsV2Error(
                    "project binding workspace is not live"
                ) from None
        repository_key = project.repository.casefold()
        if repository_key in repositories:
            raise TaskSettingsV2Error("projects contain duplicate repositories")
        repositories.add(repository_key)
        if project.host_id != task_owner_host:
            raise TaskSettingsV2Error("project does not match the Task owner host")
    return canonical


def _parse_merge_order(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    if type(value) is not list or any(type(item) is not str for item in value):
        raise TaskSettingsV2Error("merge_order must be an array of project IDs or null")
    return tuple(value)


def _validate_merge_order(
    value: object,
    projects: tuple[TaskProject, ...],
    merge_mode: MergeMode,
) -> tuple[str, ...] | None:
    needs_order = len(projects) > 1 and merge_mode is MergeMode.FULL_AUTO
    if not needs_order:
        if value is not None:
            raise TaskSettingsV2Error("merge_order must be null for this Task")
        return None
    if type(value) is not tuple or any(type(item) is not str for item in value):
        raise TaskSettingsV2Error("merge_order must be the exact project permutation")
    project_ids = tuple(project.project_id for project in projects)
    if len(value) != len(project_ids) or set(value) != set(project_ids):
        raise TaskSettingsV2Error("merge_order must be the exact project permutation")
    return value


def _validate_expiry(
    merge_mode: MergeMode,
    confirmed_at: datetime,
    expires_at: object,
) -> datetime | None:
    if expires_at is None:
        normalized_expiry = None
    else:
        normalized_expiry = _normalize_datetime(
            expires_at,
            "auto_merge_expires_at",
        )
    if merge_mode is MergeMode.MANUAL:
        if normalized_expiry is not None:
            raise TaskSettingsV2Error(
                "manual merge_mode requires auto_merge_expires_at to be null"
            )
        return None
    if normalized_expiry is None:
        raise TaskSettingsV2Error(
            "automatic merge_mode requires auto_merge_expires_at"
        )
    if normalized_expiry <= confirmed_at:
        raise TaskSettingsV2Error(
            "auto_merge_expires_at must be after confirmed_at"
        )
    if normalized_expiry > confirmed_at + MAX_AUTO_MERGE_DURATION:
        raise TaskSettingsV2Error(
            "auto_merge_expires_at must be no later than 12 hours after confirmed_at"
        )
    return normalized_expiry


def _request_payload(request: TaskRequestV2) -> dict[str, object]:
    return {
        "format_version": request.format_version,
        "request_id": request.request_id,
        "management_repository": request.management_repository,
        "mode": request.mode.value,
        "task_content": _task_content_payload(request.task_content),
        "task_content_hash": request.task_content_hash,
        "task_flow": request.task_flow.value,
        "merge_mode": request.merge_mode.value,
        "merge_order": None if request.merge_order is None else list(request.merge_order),
        "projects": [_project_payload(project) for project in request.projects],
        "task_owner_host": request.task_owner_host,
        "confirmed_by": request.confirmed_by,
        "confirmed_at": _format_timestamp(request.confirmed_at),
        "auto_merge_expires_at": (
            None
            if request.auto_merge_expires_at is None
            else _format_timestamp(request.auto_merge_expires_at)
        ),
        "replaces_request_id": request.replaces_request_id,
        "request_hash": request.request_hash,
        "status": request.status,
    }


def _request_hash_from_payload(payload: Mapping[str, object]) -> str:
    return _canonical_hash(
        {
            key: value
            for key, value in payload.items()
            if key not in {"request_hash", "status"}
        }
    )


# RISK(breaking): These exact seventeen fields and their canonical hash are the
# durable public forge-task-request/v2 record. Never reinterpret a v1 field here.
@dataclass(frozen=True, slots=True)
class TaskRequestV2:
    format_version: str
    request_id: str
    management_repository: str
    mode: Mode
    task_content: TaskContent
    task_content_hash: str
    task_flow: TaskFlow
    merge_mode: MergeMode
    merge_order: tuple[str, ...] | None
    projects: tuple[TaskProject, ...]
    task_owner_host: str
    confirmed_by: str
    confirmed_at: datetime
    auto_merge_expires_at: datetime | None
    replaces_request_id: str | None
    request_hash: str
    status: str

    def __post_init__(self) -> None:
        self._validate_record(require_live_projects=True)

    def _validate_record(self, *, require_live_projects: bool) -> None:
        if self.format_version != TASK_REQUEST_V2_FORMAT:
            raise TaskSettingsV2Error(
                f"format_version must be {TASK_REQUEST_V2_FORMAT}"
            )
        _validate_uuid(self.request_id, "request_id")
        _validate_repository_field(
            self.management_repository,
            "management_repository",
        )
        _validate_mode(self.mode)
        _validate_task_content(self.task_content)
        _validate_hash(self.task_content_hash, "task_content_hash")
        try:
            expected_content_hash = task_content_hash(self.task_content)
        except TaskSettingsError:
            raise TaskSettingsV2Error("task_content is invalid") from None
        if self.task_content_hash != expected_content_hash:
            raise TaskSettingsV2Error("task_content_hash does not match task_content")
        _validate_task_flow(self.task_flow)
        merge_mode = _validate_merge_mode(self.merge_mode)
        owner_host = _validate_uuid(self.task_owner_host, "task_owner_host")
        projects = _validate_projects(
            self.projects,
            owner_host,
            require_canonical_order=True,
            require_live_workspace=require_live_projects,
        )
        _validate_merge_order(self.merge_order, projects, merge_mode)
        _validate_confirmed_by(self.confirmed_by)
        confirmed_at = _normalize_datetime(self.confirmed_at, "confirmed_at")
        expires_at = _validate_expiry(
            merge_mode,
            confirmed_at,
            self.auto_merge_expires_at,
        )
        object.__setattr__(self, "confirmed_at", confirmed_at)
        object.__setattr__(self, "auto_merge_expires_at", expires_at)
        _validate_optional_uuid(self.replaces_request_id, "replaces_request_id")
        _validate_hash(self.request_hash, "request_hash")
        if self.status != _REQUEST_STATUS or type(self.status) is not str:
            raise TaskSettingsV2Error("status must be prepared")
        expected_hash = _request_hash_from_payload(_request_payload(self))
        if self.request_hash != expected_hash:
            raise TaskSettingsV2Error("request_hash does not match request fields")

    @classmethod
    def create(
        cls,
        *,
        request_id: str,
        management_repository: str,
        task_content: TaskContent,
        task_flow: TaskFlow,
        merge_mode: MergeMode,
        merge_order: tuple[str, ...] | None,
        projects: tuple[TaskProject, ...],
        task_owner_host: str,
        confirmed_by: str,
        confirmed_at: datetime,
        auto_merge_expires_at: datetime | None | object = _AUTO_EXPIRY_UNSET,
        replaces_request_id: str | None = None,
    ) -> TaskRequestV2:
        """Create one canonical prepared request, sorting Project bindings."""

        canonical_request_id = _validate_uuid(request_id, "request_id")
        canonical_repository = _validate_repository_field(
            management_repository,
            "management_repository",
        )
        if not isinstance(task_content, TaskContent):
            raise TaskSettingsV2Error("task_content must be TaskContent")
        canonical_flow = _validate_task_flow(task_flow)
        canonical_mode = _validate_merge_mode(merge_mode)
        canonical_host = _validate_uuid(task_owner_host, "task_owner_host")
        canonical_projects = _validate_projects(
            projects,
            canonical_host,
            require_canonical_order=False,
            require_live_workspace=True,
        )
        canonical_order = _validate_merge_order(
            merge_order,
            canonical_projects,
            canonical_mode,
        )
        canonical_confirmed_by = _validate_confirmed_by(confirmed_by)
        canonical_confirmed_at = _normalize_datetime(confirmed_at, "confirmed_at")
        if auto_merge_expires_at is _AUTO_EXPIRY_UNSET:
            expiry_value: object = (
                None
                if canonical_mode is MergeMode.MANUAL
                else canonical_confirmed_at + MAX_AUTO_MERGE_DURATION
            )
        else:
            expiry_value = auto_merge_expires_at
        canonical_expiry = _validate_expiry(
            canonical_mode,
            canonical_confirmed_at,
            expiry_value,
        )
        canonical_replaces = _validate_optional_uuid(
            replaces_request_id,
            "replaces_request_id",
        )
        canonical_content = _validate_task_content(task_content)
        try:
            content_hash = task_content_hash(canonical_content)
        except (TaskSettingsError, UnicodeEncodeError):
            raise TaskSettingsV2Error("task_content is invalid") from None
        payload: dict[str, object] = {
            "format_version": TASK_REQUEST_V2_FORMAT,
            "request_id": canonical_request_id,
            "management_repository": canonical_repository,
            "mode": Mode.TASK.value,
            "task_content": _task_content_payload(canonical_content),
            "task_content_hash": content_hash,
            "task_flow": canonical_flow.value,
            "merge_mode": canonical_mode.value,
            "merge_order": (
                None if canonical_order is None else list(canonical_order)
            ),
            "projects": [
                _project_payload(project) for project in canonical_projects
            ],
            "task_owner_host": canonical_host,
            "confirmed_by": canonical_confirmed_by,
            "confirmed_at": _format_timestamp(canonical_confirmed_at),
            "auto_merge_expires_at": (
                None
                if canonical_expiry is None
                else _format_timestamp(canonical_expiry)
            ),
            "replaces_request_id": canonical_replaces,
        }
        return cls(
            format_version=TASK_REQUEST_V2_FORMAT,
            request_id=canonical_request_id,
            management_repository=canonical_repository,
            mode=Mode.TASK,
            task_content=canonical_content,
            task_content_hash=content_hash,
            task_flow=canonical_flow,
            merge_mode=canonical_mode,
            merge_order=canonical_order,
            projects=canonical_projects,
            task_owner_host=canonical_host,
            confirmed_by=canonical_confirmed_by,
            confirmed_at=canonical_confirmed_at,
            auto_merge_expires_at=canonical_expiry,
            replaces_request_id=canonical_replaces,
            request_hash=_canonical_hash(payload),
            status=_REQUEST_STATUS,
        )

    @classmethod
    def from_json(cls, raw: object) -> TaskRequestV2:
        """Parse one exact request without aliases, defaults, or unknown fields."""

        outcome = _parse_task_request_result(cls, raw)
        raw = None
        return _unwrap_task_request_result(outcome)

    def to_json(self) -> str:
        """Return the one compact key-sorted UTF-8 JSON representation."""

        return _canonical_json(_request_payload(self))


def _stored_task_request(
    cls: type[TaskRequestV2],
    payload: Mapping[str, object],
) -> TaskRequestV2:
    request = object.__new__(cls)
    values: dict[str, object] = {
        "format_version": payload["format_version"],
        "request_id": payload["request_id"],
        "management_repository": payload["management_repository"],
        "mode": _parse_mode(payload["mode"]),
        "task_content": _parse_task_content(payload["task_content"]),
        "task_content_hash": payload["task_content_hash"],
        "task_flow": _parse_task_flow(payload["task_flow"]),
        "merge_mode": _parse_merge_mode(payload["merge_mode"]),
        "merge_order": _parse_merge_order(payload["merge_order"]),
        "projects": _parse_projects(payload["projects"]),
        "task_owner_host": payload["task_owner_host"],
        "confirmed_by": payload["confirmed_by"],
        "confirmed_at": _parse_timestamp(payload["confirmed_at"], "confirmed_at"),
        "auto_merge_expires_at": (
            None
            if payload["auto_merge_expires_at"] is None
            else _parse_timestamp(
                payload["auto_merge_expires_at"],
                "auto_merge_expires_at",
            )
        ),
        "replaces_request_id": payload["replaces_request_id"],
        "request_hash": payload["request_hash"],
        "status": payload["status"],
    }
    for field_name, value in values.items():
        object.__setattr__(request, field_name, value)
    request._validate_record(require_live_projects=False)
    return request


def _parse_task_request_result(
    cls: type[TaskRequestV2],
    raw: object,
) -> TaskRequestV2 | _ParseFailure:
    payload: dict[str, object] | None = None
    try:
        payload = _load_json_object(raw, "Task request")
        _require_fields(payload, _REQUEST_FIELDS, "Task request")
        return _stored_task_request(cls, payload)
    except TaskSettingsV2Error as error:
        message = str(error)
    raw = None
    payload = None
    return _ParseFailure(message)


def _unwrap_task_request_result(
    outcome: TaskRequestV2 | _ParseFailure,
) -> TaskRequestV2:
    if isinstance(outcome, _ParseFailure):
        raise TaskSettingsV2Error(outcome.message) from None
    return outcome


def task_request_v2_hash(request: TaskRequestV2) -> str:
    """Recalculate the canonical request hash, excluding hash and status."""

    if not isinstance(request, TaskRequestV2):
        raise TaskSettingsV2Error("request must be TaskRequestV2")
    return _request_hash_from_payload(_request_payload(request))


def parse_task_request_v2(raw: object) -> TaskRequestV2:
    """Compatibility function for the strict TaskRequestV2 parser."""

    outcome = _parse_task_request_result(TaskRequestV2, raw)
    raw = None
    return _unwrap_task_request_result(outcome)


def _settings_payload(settings: TaskSettingsV2) -> dict[str, object]:
    return {
        "format_version": settings.format_version,
        "request_id": settings.request_id,
        "request_hash": settings.request_hash,
        "management_repository": settings.management_repository,
        "parent_issue_number": settings.parent_issue_number,
        "mode": settings.mode.value,
        "task_content_hash": settings.task_content_hash,
        "task_flow": settings.task_flow.value,
        "merge_mode": settings.merge_mode.value,
        "merge_order": (
            None if settings.merge_order is None else list(settings.merge_order)
        ),
        "projects": [_project_payload(project) for project in settings.projects],
        "task_owner_host": settings.task_owner_host,
        "confirmed_by": settings.confirmed_by,
        "confirmed_at": _format_timestamp(settings.confirmed_at),
        "auto_merge_expires_at": (
            None
            if settings.auto_merge_expires_at is None
            else _format_timestamp(settings.auto_merge_expires_at)
        ),
        "task_settings_hash": settings.task_settings_hash,
        "status": settings.status,
    }


def _settings_hash_from_payload(payload: Mapping[str, object]) -> str:
    return _canonical_hash(
        {
            key: value
            for key, value in payload.items()
            if key not in {"task_settings_hash", "status"}
        }
    )


def _shared_request_identity(request: TaskRequestV2) -> tuple[object, ...]:
    return (
        request.request_id,
        request.request_hash,
        request.management_repository,
        request.mode,
        request.task_content_hash,
        request.task_flow,
        request.merge_mode,
        request.merge_order,
        request.projects,
        request.task_owner_host,
        request.confirmed_by,
        request.confirmed_at,
        request.auto_merge_expires_at,
    )


def _shared_settings_identity(settings: TaskSettingsV2) -> tuple[object, ...]:
    return (
        settings.request_id,
        settings.request_hash,
        settings.management_repository,
        settings.mode,
        settings.task_content_hash,
        settings.task_flow,
        settings.merge_mode,
        settings.merge_order,
        settings.projects,
        settings.task_owner_host,
        settings.confirmed_by,
        settings.confirmed_at,
        settings.auto_merge_expires_at,
    )


def _verify_settings_request(
    settings: TaskSettingsV2,
    request: object,
) -> None:
    if not isinstance(request, TaskRequestV2):
        raise TaskSettingsV2Error("request must be TaskRequestV2")
    if _shared_settings_identity(settings) != _shared_request_identity(request):
        raise TaskSettingsV2Error("Task settings does not match request")


# RISK(breaking): These exact seventeen fields and their canonical hash are the
# durable public forge-task-settings/v2 record. v1 storage remains independent.
@dataclass(frozen=True, slots=True, init=False)
class TaskSettingsV2:
    format_version: str
    request_id: str
    request_hash: str
    management_repository: str
    parent_issue_number: int
    mode: Mode
    task_content_hash: str
    task_flow: TaskFlow
    merge_mode: MergeMode
    merge_order: tuple[str, ...] | None
    projects: tuple[TaskProject, ...]
    task_owner_host: str
    confirmed_by: str
    confirmed_at: datetime
    auto_merge_expires_at: datetime | None
    task_settings_hash: str
    status: str
    request: InitVar[TaskRequestV2]

    def __init__(
        self,
        format_version: str,
        request_id: str,
        request_hash: str,
        management_repository: str,
        parent_issue_number: int,
        mode: Mode,
        task_content_hash: str,
        task_flow: TaskFlow,
        merge_mode: MergeMode,
        merge_order: tuple[str, ...] | None,
        projects: tuple[TaskProject, ...],
        task_owner_host: str,
        confirmed_by: str,
        confirmed_at: datetime,
        auto_merge_expires_at: datetime | None,
        task_settings_hash: str,
        status: str,
        request: TaskRequestV2,
    ) -> None:
        # Validate before retaining the value on ``self``.  A generated
        # dataclass initializer keeps every argument alive in its traceback,
        # which can pin an attacker-controlled, non-renderable integer.
        if not _is_json_renderable_positive_integer(parent_issue_number):
            parent_issue_number = None  # type: ignore[assignment]
            raise TaskSettingsV2Error(
                "parent_issue_number must be a positive integer"
            )

        object.__setattr__(self, "format_version", format_version)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "request_hash", request_hash)
        object.__setattr__(self, "management_repository", management_repository)
        object.__setattr__(self, "parent_issue_number", parent_issue_number)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "task_content_hash", task_content_hash)
        object.__setattr__(self, "task_flow", task_flow)
        object.__setattr__(self, "merge_mode", merge_mode)
        object.__setattr__(self, "merge_order", merge_order)
        object.__setattr__(self, "projects", projects)
        object.__setattr__(self, "task_owner_host", task_owner_host)
        object.__setattr__(self, "confirmed_by", confirmed_by)
        object.__setattr__(self, "confirmed_at", confirmed_at)
        object.__setattr__(self, "auto_merge_expires_at", auto_merge_expires_at)
        object.__setattr__(self, "task_settings_hash", task_settings_hash)
        object.__setattr__(self, "status", status)
        self.__post_init__(request)

    def __post_init__(self, request: TaskRequestV2) -> None:
        if self.format_version != TASK_SETTINGS_V2_FORMAT:
            raise TaskSettingsV2Error(
                f"format_version must be {TASK_SETTINGS_V2_FORMAT}"
            )
        _validate_uuid(self.request_id, "request_id")
        _validate_hash(self.request_hash, "request_hash")
        _validate_repository_field(
            self.management_repository,
            "management_repository",
        )
        if not _is_json_renderable_positive_integer(self.parent_issue_number):
            raise TaskSettingsV2Error("parent_issue_number must be a positive integer")
        _validate_mode(self.mode)
        _validate_hash(self.task_content_hash, "task_content_hash")
        _validate_task_flow(self.task_flow)
        merge_mode = _validate_merge_mode(self.merge_mode)
        owner_host = _validate_uuid(self.task_owner_host, "task_owner_host")
        projects = _validate_projects(
            self.projects,
            owner_host,
            require_canonical_order=True,
        )
        _validate_merge_order(self.merge_order, projects, merge_mode)
        _validate_confirmed_by(self.confirmed_by)
        confirmed_at = _normalize_datetime(self.confirmed_at, "confirmed_at")
        expires_at = _validate_expiry(
            merge_mode,
            confirmed_at,
            self.auto_merge_expires_at,
        )
        object.__setattr__(self, "confirmed_at", confirmed_at)
        object.__setattr__(self, "auto_merge_expires_at", expires_at)
        _validate_hash(self.task_settings_hash, "task_settings_hash")
        if self.status != _SETTINGS_STATUS or type(self.status) is not str:
            raise TaskSettingsV2Error("status must be active")
        expected_hash = _settings_hash_from_payload(_settings_payload(self))
        if self.task_settings_hash != expected_hash:
            raise TaskSettingsV2Error(
                "task_settings_hash does not match settings fields"
            )
        _verify_settings_request(self, request)

    @classmethod
    def create(
        cls,
        *,
        request: TaskRequestV2,
        parent_issue_number: int,
    ) -> TaskSettingsV2:
        """Create active settings bound to every exact shared request field."""

        if not isinstance(request, TaskRequestV2):
            raise TaskSettingsV2Error("request must be TaskRequestV2")
        if not _is_json_renderable_positive_integer(parent_issue_number):
            parent_issue_number = None  # type: ignore[assignment]
            raise TaskSettingsV2Error("parent_issue_number must be a positive integer")
        payload: dict[str, object] = {
            "format_version": TASK_SETTINGS_V2_FORMAT,
            "request_id": request.request_id,
            "request_hash": request.request_hash,
            "management_repository": request.management_repository,
            "parent_issue_number": parent_issue_number,
            "mode": request.mode.value,
            "task_content_hash": request.task_content_hash,
            "task_flow": request.task_flow.value,
            "merge_mode": request.merge_mode.value,
            "merge_order": (
                None if request.merge_order is None else list(request.merge_order)
            ),
            "projects": [
                _project_payload(project) for project in request.projects
            ],
            "task_owner_host": request.task_owner_host,
            "confirmed_by": request.confirmed_by,
            "confirmed_at": _format_timestamp(request.confirmed_at),
            "auto_merge_expires_at": (
                None
                if request.auto_merge_expires_at is None
                else _format_timestamp(request.auto_merge_expires_at)
            ),
        }
        settings = cls(
            format_version=TASK_SETTINGS_V2_FORMAT,
            request_id=request.request_id,
            request_hash=request.request_hash,
            management_repository=request.management_repository,
            parent_issue_number=parent_issue_number,
            mode=request.mode,
            task_content_hash=request.task_content_hash,
            task_flow=request.task_flow,
            merge_mode=request.merge_mode,
            merge_order=request.merge_order,
            projects=request.projects,
            task_owner_host=request.task_owner_host,
            confirmed_by=request.confirmed_by,
            confirmed_at=request.confirmed_at,
            auto_merge_expires_at=request.auto_merge_expires_at,
            task_settings_hash=_canonical_hash(payload),
            status=_SETTINGS_STATUS,
            request=request,
        )
        return settings

    @classmethod
    def from_json(
        cls,
        raw: object,
        *,
        request: TaskRequestV2,
    ) -> TaskSettingsV2:
        """Parse settings and verify every shared field against its request."""

        outcome = _parse_task_settings_result(cls, raw, request)
        raw = None
        request = None  # type: ignore[assignment]
        return _unwrap_task_settings_result(outcome)

    def to_json(self) -> str:
        """Return the one compact key-sorted UTF-8 JSON representation."""

        return _canonical_json(_settings_payload(self))


def _parse_task_settings_result(
    cls: type[TaskSettingsV2],
    raw: object,
    request: object,
) -> TaskSettingsV2 | _ParseFailure:
    payload: dict[str, object] | None = None
    try:
        payload = _load_json_object(raw, "Task settings")
        _require_fields(payload, _SETTINGS_FIELDS, "Task settings")
        return cls(
            format_version=payload["format_version"],
            request_id=payload["request_id"],
            request_hash=payload["request_hash"],
            management_repository=payload["management_repository"],
            parent_issue_number=payload["parent_issue_number"],
            mode=_parse_mode(payload["mode"]),
            task_content_hash=payload["task_content_hash"],
            task_flow=_parse_task_flow(payload["task_flow"]),
            merge_mode=_parse_merge_mode(payload["merge_mode"]),
            merge_order=_parse_merge_order(payload["merge_order"]),
            projects=_parse_projects(payload["projects"]),
            task_owner_host=payload["task_owner_host"],
            confirmed_by=payload["confirmed_by"],
            confirmed_at=_parse_timestamp(payload["confirmed_at"], "confirmed_at"),
            auto_merge_expires_at=(
                None
                if payload["auto_merge_expires_at"] is None
                else _parse_timestamp(
                    payload["auto_merge_expires_at"],
                    "auto_merge_expires_at",
                )
            ),
            task_settings_hash=payload["task_settings_hash"],
            status=payload["status"],
            request=request,
        )
    except TaskSettingsV2Error as error:
        message = str(error)
    raw = None
    request = None
    payload = None
    return _ParseFailure(message)


def _unwrap_task_settings_result(
    outcome: TaskSettingsV2 | _ParseFailure,
) -> TaskSettingsV2:
    if isinstance(outcome, _ParseFailure):
        raise TaskSettingsV2Error(outcome.message) from None
    return outcome


def task_settings_v2_hash(settings: TaskSettingsV2) -> str:
    """Recalculate the canonical settings hash, excluding hash and status."""

    if not isinstance(settings, TaskSettingsV2):
        raise TaskSettingsV2Error("settings must be TaskSettingsV2")
    return _settings_hash_from_payload(_settings_payload(settings))


def parse_task_settings_v2(
    raw: object,
    *,
    request: TaskRequestV2,
) -> TaskSettingsV2:
    """Compatibility function for the strict TaskSettingsV2 parser."""

    outcome = _parse_task_settings_result(TaskSettingsV2, raw, request)
    raw = None
    request = None  # type: ignore[assignment]
    return _unwrap_task_settings_result(outcome)
