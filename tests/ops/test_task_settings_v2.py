from __future__ import annotations

import hashlib
import json
import shutil
import traceback
from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

import forge.ops.task_settings_v2 as task_settings_v2_module
from forge.ops.task_options import MergeMode, TaskFlow
from forge.ops.task_projects import TaskProject
from forge.ops.task_settings import TaskContent, task_content_hash
from forge.ops.task_settings_v2 import (
    TASK_REQUEST_V2_FORMAT,
    TASK_SETTINGS_V2_FORMAT,
    TaskRequestV2,
    TaskSettingsV2,
    TaskSettingsV2Error,
    parse_task_request_v2,
    parse_task_settings_v2,
    task_request_v2_hash,
    task_settings_v2_hash,
)


REQUEST_ID = "12345678-1234-4234-8234-123456789abc"
OWNER_HOST = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CONFIRMED_AT = datetime(2026, 7, 18, 3, 0, tzinfo=UTC)


def _content() -> TaskContent:
    return TaskContent(
        title="여러 저장소 작업",
        description="중앙 관리와 실제 코드 작업을 분리한다.",
        acceptance_criteria=("모든 Project binding을 hash에 포함한다.",),
    )


def _project(
    root: Path,
    *,
    repository: str,
    branch: str,
    commit: str,
    remote_name: str = "origin",
    host_id: str = OWNER_HOST,
) -> TaskProject:
    workspace = root / repository.replace("/", "-") / remote_name
    workspace.mkdir(parents=True)
    return TaskProject.create(
        repository=repository,
        workspace=str(workspace.resolve()),
        remote_name=remote_name,
        base_branch=branch,
        base_commit=commit,
        host_id=host_id,
    )


def _projects(tmp_path: Path) -> tuple[TaskProject, TaskProject]:
    return (
        _project(
            tmp_path,
            repository="Zulu/code-two",
            branch="release/next",
            commit="b" * 40,
        ),
        _project(
            tmp_path,
            repository="Alpha/code-one",
            branch="main",
            commit="a" * 40,
        ),
    )


def _request(
    tmp_path: Path,
    *,
    projects: tuple[TaskProject, ...] | None = None,
    merge_mode: MergeMode = MergeMode.FULL_AUTO,
    merge_order: tuple[str, ...] | None | object = ...,
    request_id: str = REQUEST_ID,
    task_owner_host: str = OWNER_HOST,
    confirmed_at: datetime = CONFIRMED_AT,
    auto_merge_expires_at: datetime | None | object = ...,
    replaces_request_id: str | None = None,
    task_content: TaskContent | None = None,
    confirmed_by: str = "local-user",
) -> TaskRequestV2:
    selected = projects if projects is not None else _projects(tmp_path)
    if merge_order is ...:
        order = (
            tuple(project.project_id for project in reversed(selected))
            if len(selected) > 1 and merge_mode is MergeMode.FULL_AUTO
            else None
        )
    else:
        order = merge_order
    if auto_merge_expires_at is ...:
        expires_at = (
            None
            if merge_mode is MergeMode.MANUAL
            else confirmed_at + timedelta(hours=12)
        )
    else:
        expires_at = auto_merge_expires_at
    return TaskRequestV2.create(
        request_id=request_id,
        management_repository="immortal0900/INFINITY_FORGE",
        task_content=_content() if task_content is None else task_content,
        task_flow=TaskFlow.BUILD_REVIEW,
        merge_mode=merge_mode,
        merge_order=order,  # type: ignore[arg-type]
        projects=selected,
        task_owner_host=task_owner_host,
        confirmed_by=confirmed_by,
        confirmed_at=confirmed_at,
        auto_merge_expires_at=expires_at,  # type: ignore[arg-type]
        replaces_request_id=replaces_request_id,
    )


def _exception_graph(error: BaseException) -> tuple[BaseException, ...]:
    pending = [error]
    seen: set[int] = set()
    found: list[BaseException] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        found.append(current)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return tuple(found)


def _contained_text(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, bytes):
        return (value.decode("utf-8", errors="replace"),)
    if isinstance(value, dict):
        return tuple(
            text
            for item in value.items()
            for part in item
            for text in _contained_text(part)
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(
            text for item in value for text in _contained_text(item)
        )
    try:
        return (repr(value),)
    except Exception:
        return ()


def _forge_traceback_local_text(error: BaseException) -> tuple[str, ...]:
    texts: list[str] = []
    for current in _exception_graph(error):
        trace = current.__traceback__
        while trace is not None:
            module_name = trace.tb_frame.f_globals.get("__name__", "")
            if module_name.startswith("forge.ops"):
                for value in trace.tb_frame.f_locals.values():
                    texts.extend(_contained_text(value))
            trace = trace.tb_next
    return tuple(texts)


def _forge_traceback_contains_identity(
    error: BaseException,
    target: object,
) -> bool:
    for current in _exception_graph(error):
        trace = current.__traceback__
        while trace is not None:
            module_name = trace.tb_frame.f_globals.get("__name__", "")
            if module_name.startswith("forge.ops") and any(
                value is target for value in trace.tb_frame.f_locals.values()
            ):
                return True
            trace = trace.tb_next
    return False


def _assert_sanitized_v2_error(
    operation: object,
    secret: str,
) -> None:
    with pytest.raises(TaskSettingsV2Error) as caught:
        operation()  # type: ignore[operator]
    assert all(
        error.__cause__ is None and error.__context__ is None
        for error in _exception_graph(caught.value)
    )
    rendered = "".join(traceback.format_exception(caught.value))
    assert secret not in rendered
    assert not any(
        secret in text for text in _forge_traceback_local_text(caught.value)
    )


def _project_payload(project: TaskProject) -> dict[str, object]:
    return {field.name: getattr(project, field.name) for field in fields(project)}


def _request_hash(payload: dict[str, object]) -> str:
    hash_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"request_hash", "status"}
    }
    canonical = json.dumps(
        hash_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _settings_hash(payload: dict[str, object]) -> str:
    hash_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"task_settings_hash", "status"}
    }
    canonical = json.dumps(
        hash_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def test_v2_records_have_exact_frozen_fields_and_canonical_roundtrip(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    settings = TaskSettingsV2.create(request=request, parent_issue_number=21)

    assert tuple(field.name for field in fields(request)) == (
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
    )
    assert tuple(field.name for field in fields(settings)) == (
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
    )
    assert request.format_version == TASK_REQUEST_V2_FORMAT
    assert request.status == "prepared"
    assert settings.format_version == TASK_SETTINGS_V2_FORMAT
    assert settings.status == "active"
    assert request.projects == tuple(
        sorted(
            request.projects,
            key=lambda item: (
                item.repository,
                item.workspace,
                item.base_branch,
                item.project_id,
            ),
        )
    )
    assert parse_task_request_v2(request.to_json()) == request
    assert parse_task_settings_v2(settings.to_json(), request=request) == settings
    assert TaskRequestV2.from_json(request.to_json()) == request
    assert TaskSettingsV2.from_json(settings.to_json(), request=request) == settings
    with pytest.raises(FrozenInstanceError):
        request.status = "bound"  # type: ignore[misc]


def test_every_settings_constructor_requires_the_exact_request(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    settings = TaskSettingsV2.create(request=request, parent_issue_number=21)
    constructor_fields = {
        field.name: getattr(settings, field.name) for field in fields(settings)
    }

    assert "request" not in TaskSettingsV2.__slots__
    assert "request=" not in repr(settings)
    with pytest.raises(TypeError):
        TaskSettingsV2(**constructor_fields)  # type: ignore[arg-type]
    assert TaskSettingsV2(**constructor_fields, request=request) == settings

    with pytest.raises(ValueError, match="request"):
        replace(settings)
    assert replace(settings, request=request) == settings

    changed_payload = json.loads(settings.to_json())
    changed_payload["confirmed_by"] = "different-user"
    changed_hash = _settings_hash(changed_payload)
    with pytest.raises(TaskSettingsV2Error, match="does not match request"):
        replace(
            settings,
            confirmed_by="different-user",
            task_settings_hash=changed_hash,
            request=request,
        )


def test_v2_hashes_use_compact_key_sorted_utf8_and_exclude_hash_and_status(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    request_payload = json.loads(request.to_json())
    settings = TaskSettingsV2.create(request=request, parent_issue_number=21)
    settings_payload = json.loads(settings.to_json())

    assert request.task_content_hash == task_content_hash(_content())
    assert request.request_hash == _request_hash(request_payload)
    assert request.request_hash == task_request_v2_hash(request)
    assert settings.task_settings_hash == _settings_hash(settings_payload)
    assert settings.task_settings_hash == task_settings_v2_hash(settings)
    assert request.to_json() == json.dumps(
        request_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def test_v2_request_and_settings_canonical_hash_have_hardcoded_fixtures() -> None:
    project = {
        "project_id": "0" * 64,
        "repository": "Owner/Repo",
        "workspace": "/fixed/work",
        "remote_name": "origin",
        "base_branch": "main",
        "base_commit": "a" * 40,
        "host_id": OWNER_HOST,
    }
    request_preimage = {
        "format_version": TASK_REQUEST_V2_FORMAT,
        "request_id": REQUEST_ID,
        "management_repository": "Owner/Management",
        "mode": "task",
        "task_content": {
            "title": "고정 Task",
            "description": "digest fixture",
            "acceptance_criteria": ["AC-01"],
        },
        "task_content_hash": "1" * 64,
        "task_flow": "build_review",
        "merge_mode": "manual",
        "merge_order": None,
        "projects": [project],
        "task_owner_host": OWNER_HOST,
        "confirmed_by": "subject-1",
        "confirmed_at": "2026-07-18T03:00:00Z",
        "auto_merge_expires_at": None,
        "replaces_request_id": None,
    }
    request_digest = task_settings_v2_module._canonical_hash(request_preimage)
    assert request_digest == (
        "ded963b6b821ff95aa5e04d9e19fbd0bebe06ba1b5fcc009a6bb85aaa0d377c1"
    )

    settings_preimage = {
        "format_version": TASK_SETTINGS_V2_FORMAT,
        "request_id": REQUEST_ID,
        "request_hash": request_digest,
        "management_repository": "Owner/Management",
        "parent_issue_number": 21,
        "mode": "task",
        "task_content_hash": "1" * 64,
        "task_flow": "build_review",
        "merge_mode": "manual",
        "merge_order": None,
        "projects": [project],
        "task_owner_host": OWNER_HOST,
        "confirmed_by": "subject-1",
        "confirmed_at": "2026-07-18T03:00:00Z",
        "auto_merge_expires_at": None,
    }
    assert task_settings_v2_module._canonical_hash(settings_preimage) == (
        "783dc2c1405ac4c5e6726afde1401520efde8b05f88ad7b318701c027fcc05fd"
    )


@pytest.mark.parametrize("duplicate_level", ["root", "content", "project"])
def test_parser_rejects_duplicate_json_keys_at_every_level(
    tmp_path: Path,
    duplicate_level: str,
) -> None:
    request = _request(tmp_path)
    raw = request.to_json()
    if duplicate_level == "root":
        raw = raw.replace(
            '"format_version":"forge-task-request/v2"',
            '"format_version":"forge-task-request/v2",'
            '"format_version":"forge-task-request/v2"',
            1,
        )
    elif duplicate_level == "content":
        raw = raw.replace(
            '"title":"여러 저장소 작업"',
            '"title":"여러 저장소 작업","title":"중복"',
            1,
        )
    else:
        raw = raw.replace(
            '"base_branch":"main"',
            '"base_branch":"main","base_branch":"other"',
            1,
        )

    with pytest.raises(TaskSettingsV2Error, match="duplicate"):
        parse_task_request_v2(raw)


def test_settings_parser_rejects_nested_duplicate_project_key(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    settings = TaskSettingsV2.create(request=request, parent_issue_number=21)
    raw = settings.to_json().replace(
        '"base_branch":"main"',
        '"base_branch":"main","base_branch":"other"',
        1,
    )

    with pytest.raises(TaskSettingsV2Error, match="duplicate"):
        TaskSettingsV2.from_json(raw, request=request)


@pytest.mark.parametrize("record", ["request", "settings"])
@pytest.mark.parametrize("change", ["missing", "extra"])
def test_root_schema_rejects_missing_and_extra_fields(
    tmp_path: Path,
    record: str,
    change: str,
) -> None:
    request = _request(tmp_path)
    if record == "request":
        payload = json.loads(request.to_json())
        parser = TaskRequestV2.from_json
        if change == "missing":
            payload.pop("confirmed_by")
        else:
            payload["unexpected"] = "value"
        raw = json.dumps(payload)
        arguments: dict[str, object] = {}
    else:
        settings = TaskSettingsV2.create(request=request, parent_issue_number=21)
        payload = json.loads(settings.to_json())
        parser = TaskSettingsV2.from_json
        if change == "missing":
            payload.pop("confirmed_by")
        else:
            payload["unexpected"] = "value"
        raw = json.dumps(payload)
        arguments = {"request": request}

    with pytest.raises(TaskSettingsV2Error, match="fields"):
        parser(raw, **arguments)  # type: ignore[operator]


@pytest.mark.parametrize("raw", [None, b"{}", "[]", "null", "{broken"])
def test_request_parser_rejects_non_string_non_object_and_invalid_json(
    raw: object,
) -> None:
    with pytest.raises(TaskSettingsV2Error, match="JSON|object|text"):
        TaskRequestV2.from_json(raw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "content",
    [
        TaskContent(
            title="bad\ud800title",
            description="description",
            acceptance_criteria=("criterion",),
        ),
        TaskContent(
            title="title",
            description="bad\ud800description",
            acceptance_criteria=("criterion",),
        ),
        TaskContent(
            title="title",
            description="description",
            acceptance_criteria=("bad\ud800criterion",),
        ),
    ],
)
def test_request_create_rejects_unencodable_task_content(
    tmp_path: Path,
    content: TaskContent,
) -> None:
    with pytest.raises(TaskSettingsV2Error) as caught:
        _request(tmp_path, task_content=content)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_request_create_rejects_unencodable_confirmed_by(tmp_path: Path) -> None:
    with pytest.raises(TaskSettingsV2Error) as caught:
        _request(tmp_path, confirmed_by="bad\ud800subject")
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    ("field_path", "bad_value"),
    [
        (("task_content", "title"), "bad\ud800title"),
        (("task_content", "description"), "bad\ud800description"),
        (("task_content", "acceptance_criteria", 0), "bad\ud800criterion"),
        (("confirmed_by",), "bad\ud800subject"),
    ],
)
def test_request_parser_rejects_unencodable_text(
    tmp_path: Path,
    field_path: tuple[object, ...],
    bad_value: str,
) -> None:
    payload = json.loads(_request(tmp_path).to_json())
    target: object = payload
    for part in field_path[:-1]:
        target = target[part]  # type: ignore[index]
    target[field_path[-1]] = bad_value  # type: ignore[index]
    payload["request_hash"] = "0" * 64
    raw = json.dumps(payload, ensure_ascii=True)

    with pytest.raises(TaskSettingsV2Error) as caught:
        TaskRequestV2.from_json(raw)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_canonical_json_is_always_utf8_encodable(tmp_path: Path) -> None:
    request = _request(tmp_path)
    settings = TaskSettingsV2.create(request=request, parent_issue_number=21)

    assert request.to_json().encode("utf-8").decode("utf-8") == request.to_json()
    assert settings.to_json().encode("utf-8").decode("utf-8") == settings.to_json()


def test_valid_unicode_scalars_roundtrip_without_replacement(tmp_path: Path) -> None:
    content = TaskContent(
        title="한글 😀 \ufffd",
        description="NFC é / NFD e\u0301",
        acceptance_criteria=("원문 😀 \ufffd e\u0301 유지",),
    )
    request = _request(
        tmp_path,
        task_content=content,
        confirmed_by="사용자-😀-\ufffd-e\u0301",
    )
    raw = request.to_json()

    assert TaskRequestV2.from_json(raw) == request
    assert json.loads(raw)["task_content"]["description"] == content.description
    assert json.loads(raw)["confirmed_by"] == request.confirmed_by


@pytest.mark.parametrize(
    ("field_name", "value", "error"),
    [
        ("request_id", REQUEST_ID.upper(), "request_id"),
        ("task_owner_host", "not-a-uuid", "task_owner_host"),
        ("replaces_request_id", "not-a-uuid", "replaces_request_id"),
        ("management_repository", "missing-slash", "management_repository"),
        ("mode", "chat", "mode"),
        ("task_flow", "reviewed", "task_flow"),
        ("merge_mode", "P2", "merge_mode"),
        ("confirmed_by", "   ", "confirmed_by"),
        ("status", "bound", "status"),
        ("format_version", "forge-task-request/v1", "format_version"),
        ("task_content_hash", "A" * 64, "task_content_hash"),
        ("request_hash", "A" * 64, "request_hash"),
    ],
)
def test_request_parser_rejects_noncanonical_scalar_fields(
    tmp_path: Path,
    field_name: str,
    value: object,
    error: str,
) -> None:
    payload = json.loads(_request(tmp_path).to_json())
    payload[field_name] = value
    if field_name != "request_hash":
        payload["request_hash"] = _request_hash(payload)

    with pytest.raises(TaskSettingsV2Error, match=error):
        TaskRequestV2.from_json(json.dumps(payload))


@pytest.mark.parametrize(
    "field_name",
    ["confirmed_at", "auto_merge_expires_at"],
)
def test_parser_requires_canonical_utc_z_timestamps(
    tmp_path: Path,
    field_name: str,
) -> None:
    payload = json.loads(_request(tmp_path).to_json())
    payload[field_name] = "2026-07-18T03:00:00+00:00"
    payload["request_hash"] = _request_hash(payload)

    with pytest.raises(TaskSettingsV2Error, match="UTC|RFC 3339|canonical"):
        TaskRequestV2.from_json(json.dumps(payload))


def test_create_normalizes_aware_timestamps_to_utc_z(tmp_path: Path) -> None:
    kst = timezone(timedelta(hours=9))
    confirmed_at = CONFIRMED_AT.astimezone(kst)
    request = _request(
        tmp_path,
        confirmed_at=confirmed_at,
        auto_merge_expires_at=confirmed_at + timedelta(hours=2),
    )
    payload = json.loads(request.to_json())

    assert request.confirmed_at.tzinfo is UTC
    assert request.auto_merge_expires_at is not None
    assert request.auto_merge_expires_at.tzinfo is UTC
    assert payload["confirmed_at"].endswith("Z")
    assert payload["auto_merge_expires_at"].endswith("Z")


@pytest.mark.parametrize(
    ("merge_mode", "expiry_kind", "valid"),
    [
        (MergeMode.MANUAL, "null", True),
        (MergeMode.MANUAL, "later", False),
        (MergeMode.SAFE_AUTO, "null", False),
        (MergeMode.SAFE_AUTO, "equal", False),
        (MergeMode.SAFE_AUTO, "later", True),
        (MergeMode.FULL_AUTO, "too_late", False),
        (MergeMode.FULL_AUTO, "limit", True),
    ],
)
def test_v2_preserves_v1_auto_merge_expiry_rules(
    tmp_path: Path,
    merge_mode: MergeMode,
    expiry_kind: str,
    valid: bool,
) -> None:
    expiry = {
        "null": None,
        "equal": CONFIRMED_AT,
        "later": CONFIRMED_AT + timedelta(hours=1),
        "limit": CONFIRMED_AT + timedelta(hours=12),
        "too_late": CONFIRMED_AT + timedelta(hours=12, microseconds=1),
    }[expiry_kind]

    if valid:
        assert _request(
            tmp_path,
            merge_mode=merge_mode,
            auto_merge_expires_at=expiry,
        ).auto_merge_expires_at == expiry
    else:
        with pytest.raises(TaskSettingsV2Error, match="auto_merge_expires_at"):
            _request(
                tmp_path,
                merge_mode=merge_mode,
                auto_merge_expires_at=expiry,
            )


def test_projects_must_be_nonempty_sorted_unique_and_on_owner_host(
    tmp_path: Path,
) -> None:
    first, second = _projects(tmp_path)
    request = _request(tmp_path, projects=(first, second))
    payload = json.loads(request.to_json())
    payload["projects"].reverse()
    payload["request_hash"] = _request_hash(payload)
    unsorted = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    with pytest.raises(TaskSettingsV2Error, match="canonical order"):
        parse_task_request_v2(unsorted)
    with pytest.raises(TaskSettingsV2Error, match="at least one"):
        _request(tmp_path, projects=(), merge_order=None)

    alias = _project(
        tmp_path,
        repository=first.repository.lower(),
        branch="other",
        commit="c" * 40,
        remote_name="upstream",
    )
    with pytest.raises(TaskSettingsV2Error, match="duplicate repositories"):
        _request(tmp_path, projects=(first, alias))

    other_host = _project(
        tmp_path,
        repository="Other/repository",
        branch="main",
        commit="d" * 40,
        host_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    )
    with pytest.raises(TaskSettingsV2Error, match="owner host"):
        _request(tmp_path, projects=(first, other_host))


@pytest.mark.parametrize(
    ("project_count", "merge_mode", "merge_order_kind", "valid"),
    [
        (1, MergeMode.MANUAL, "null", True),
        (1, MergeMode.SAFE_AUTO, "null", True),
        (1, MergeMode.FULL_AUTO, "null", True),
        (1, MergeMode.FULL_AUTO, "all", False),
        (2, MergeMode.MANUAL, "null", True),
        (2, MergeMode.SAFE_AUTO, "null", True),
        (2, MergeMode.FULL_AUTO, "null", False),
        (2, MergeMode.FULL_AUTO, "all", True),
        (2, MergeMode.FULL_AUTO, "duplicate", False),
        (2, MergeMode.FULL_AUTO, "partial", False),
    ],
)
def test_merge_order_truth_table(
    tmp_path: Path,
    project_count: int,
    merge_mode: MergeMode,
    merge_order_kind: str,
    valid: bool,
) -> None:
    projects = _projects(tmp_path)[:project_count]
    if merge_order_kind == "null":
        merge_order = None
    elif merge_order_kind == "all":
        merge_order = tuple(project.project_id for project in projects)
    elif merge_order_kind == "duplicate":
        merge_order = (projects[0].project_id,) * 2
    else:
        merge_order = (projects[0].project_id,)

    if valid:
        assert _request(
            tmp_path,
            projects=projects,
            merge_mode=merge_mode,
            merge_order=merge_order,
        ).merge_order == merge_order
    else:
        with pytest.raises(TaskSettingsV2Error, match="merge_order"):
            _request(
                tmp_path,
                projects=projects,
                merge_mode=merge_mode,
                merge_order=merge_order,
            )


def test_settings_parser_binds_every_shared_field_to_exact_request(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    settings = TaskSettingsV2.create(request=request, parent_issue_number=21)
    payload = json.loads(settings.to_json())
    payload["confirmed_by"] = "different-user"
    payload["task_settings_hash"] = _settings_hash(payload)
    changed = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    with pytest.raises(TaskSettingsV2Error, match="does not match request"):
        parse_task_settings_v2(changed, request=request)


def test_settings_rejects_bool_parent_issue_number(tmp_path: Path) -> None:
    request = _request(tmp_path)

    with pytest.raises(TaskSettingsV2Error, match="positive integer"):
        TaskSettingsV2.create(request=request, parent_issue_number=True)


def test_stored_v2_records_survive_deleted_workspace(tmp_path: Path) -> None:
    request = _request(tmp_path)
    settings = TaskSettingsV2.create(request=request, parent_issue_number=21)
    request_raw = request.to_json()
    settings_raw = settings.to_json()
    shutil.rmtree(tmp_path)

    recovered_request = TaskRequestV2.from_json(request_raw)
    recovered_settings = TaskSettingsV2.from_json(
        settings_raw,
        request=recovered_request,
    )

    assert recovered_request == request
    assert recovered_settings == settings


def test_request_create_revalidates_stored_project_workspace_live(
    tmp_path: Path,
) -> None:
    project = _projects(tmp_path)[0]
    request = _request(
        tmp_path,
        projects=(project,),
        merge_mode=MergeMode.MANUAL,
        merge_order=None,
    )
    project_payload = _project_payload(request.projects[0])
    shutil.rmtree(Path(request.projects[0].workspace))
    stored_project = TaskProject.from_mapping(project_payload)

    with pytest.raises(TaskSettingsV2Error, match="project binding"):
        TaskRequestV2.create(
            request_id=REQUEST_ID,
            management_repository="immortal0900/INFINITY_FORGE",
            task_content=_content(),
            task_flow=TaskFlow.BUILD_REVIEW,
            merge_mode=MergeMode.MANUAL,
            merge_order=None,
            projects=(stored_project,),
            task_owner_host=OWNER_HOST,
            confirmed_by="local-user",
            confirmed_at=CONFIRMED_AT,
            auto_merge_expires_at=None,
        )

    direct_fields = {
        field.name: getattr(request, field.name) for field in fields(request)
    }
    direct_fields["projects"] = (stored_project,)
    with pytest.raises(TaskSettingsV2Error, match="project binding"):
        TaskRequestV2(**direct_fields)  # type: ignore[arg-type]


@pytest.mark.parametrize("entrypoint", ["direct", "compatibility"])
def test_invalid_request_json_does_not_leak_raw_text_in_traceback(
    entrypoint: str,
) -> None:
    secret = "request-json-secret-token"
    raw = '{"broken":"' + secret
    parser = (
        TaskRequestV2.from_json
        if entrypoint == "direct"
        else parse_task_request_v2
    )

    _assert_sanitized_v2_error(lambda: parser(raw), secret)


@pytest.mark.parametrize("entrypoint", ["direct", "compatibility"])
def test_settings_parser_does_not_leak_raw_text_in_traceback(
    tmp_path: Path,
    entrypoint: str,
) -> None:
    request = _request(tmp_path)
    secret = "settings-json-secret-token"
    raw = '{"broken":"' + secret
    parser = (
        TaskSettingsV2.from_json
        if entrypoint == "direct"
        else parse_task_settings_v2
    )

    _assert_sanitized_v2_error(
        lambda: parser(raw, request=request),
        secret,
    )


def test_settings_parser_rejects_invalid_utf8_text(tmp_path: Path) -> None:
    request = _request(tmp_path)
    settings = TaskSettingsV2.create(request=request, parent_issue_number=21)
    payload = json.loads(settings.to_json())
    payload["confirmed_by"] = "bad\ud800subject"
    payload["task_settings_hash"] = "0" * 64
    raw = json.dumps(payload, ensure_ascii=True)

    with pytest.raises(TaskSettingsV2Error) as caught:
        TaskSettingsV2.from_json(raw, request=request)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "attack",
    ["duplicate", "management_repository", "content", "project_path"],
)
def test_expected_invalid_request_paths_do_not_leak_values_in_traceback(
    tmp_path: Path,
    attack: str,
) -> None:
    request = _request(tmp_path)
    secret = f"{attack}-secret-token"
    if attack == "duplicate":
        raw = request.to_json().replace(
            '"confirmed_by":"local-user"',
            f'"confirmed_by":"{secret}","confirmed_by":"local-user"',
            1,
        )
    else:
        payload = json.loads(request.to_json())
        if attack == "management_repository":
            payload["management_repository"] = (
                f"https://{secret}@github.com/owner/repo.git"
            )
        elif attack == "content":
            payload["task_content"]["description"] = [secret]  # type: ignore[index]
        else:
            project_payload = payload["projects"][0]  # type: ignore[index]
            project_payload["workspace"] = str(  # type: ignore[index]
                Path(project_payload["workspace"]) / ".." / secret  # type: ignore[arg-type,index]
            )
            project_binding = {
                key: project_payload[key]  # type: ignore[index]
                for key in (
                    "repository",
                    "workspace",
                    "remote_name",
                    "base_branch",
                    "base_commit",
                    "host_id",
                )
            }
            project_payload["project_id"] = hashlib.sha256(  # type: ignore[index]
                json.dumps(
                    project_binding,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        payload["request_hash"] = _request_hash(payload)
        raw = json.dumps(payload, ensure_ascii=False)

    _assert_sanitized_v2_error(lambda: TaskRequestV2.from_json(raw), secret)


def test_invalid_settings_repository_does_not_leak_credentials(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    settings = TaskSettingsV2.create(request=request, parent_issue_number=21)
    secret = "settings-repository-secret-token"
    payload = json.loads(settings.to_json())
    payload["management_repository"] = (
        f"https://{secret}@github.com/owner/repo.git"
    )
    payload["task_settings_hash"] = _settings_hash(payload)
    raw = json.dumps(payload, ensure_ascii=False)

    _assert_sanitized_v2_error(
        lambda: TaskSettingsV2.from_json(raw, request=request),
        secret,
    )


def test_unexpected_json_loader_exception_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _request(tmp_path).to_json()

    def fail_programming_error(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise ValueError("unexpected loader programming error")

    monkeypatch.setattr(task_settings_v2_module.json, "loads", fail_programming_error)

    with pytest.raises(ValueError, match="unexpected loader programming error"):
        TaskRequestV2.from_json(raw)


def test_unexpected_nested_parser_exception_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _request(tmp_path).to_json()

    def fail_programming_error(value: object) -> TaskContent:
        del value
        raise TypeError("unexpected nested parser programming error")

    monkeypatch.setattr(
        task_settings_v2_module,
        "_parse_task_content",
        fail_programming_error,
    )

    with pytest.raises(TypeError, match="unexpected nested parser programming error"):
        TaskRequestV2.from_json(raw)


@pytest.mark.parametrize("entrypoint", ["direct", "compatibility"])
def test_request_parser_sanitizes_python_digit_limit_integer(
    tmp_path: Path,
    entrypoint: str,
) -> None:
    request = _request(tmp_path)
    marker = "91827364501234567890"
    huge_integer = marker * 250
    raw = request.to_json().replace(
        f'"task_content_hash":"{request.task_content_hash}"',
        f'"task_content_hash":{huge_integer}',
        1,
    )
    parser = (
        TaskRequestV2.from_json
        if entrypoint == "direct"
        else parse_task_request_v2
    )

    _assert_sanitized_v2_error(lambda: parser(raw), marker)


@pytest.mark.parametrize("entrypoint", ["direct", "compatibility"])
def test_settings_parser_sanitizes_python_digit_limit_integer(
    tmp_path: Path,
    entrypoint: str,
) -> None:
    request = _request(tmp_path)
    settings = TaskSettingsV2.create(request=request, parent_issue_number=21)
    marker = "56473829105647382910"
    huge_integer = marker * 250
    raw = settings.to_json().replace(
        '"parent_issue_number":21',
        f'"parent_issue_number":{huge_integer}',
        1,
    )
    parser = (
        TaskSettingsV2.from_json
        if entrypoint == "direct"
        else parse_task_settings_v2
    )

    _assert_sanitized_v2_error(
        lambda: parser(raw, request=request),
        marker,
    )


def test_settings_create_rejects_unrenderable_python_issue_number(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    huge_issue_number = 10**5000

    with pytest.raises(TaskSettingsV2Error, match="parent_issue_number") as caught:
        TaskSettingsV2.create(
            request=request,
            parent_issue_number=huge_issue_number,
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert not _forge_traceback_contains_identity(
        caught.value,
        huge_issue_number,
    )
