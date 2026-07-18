from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, fields
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
        task_content=_content(),
        task_flow=TaskFlow.BUILD_REVIEW,
        merge_mode=merge_mode,
        merge_order=order,  # type: ignore[arg-type]
        projects=selected,
        task_owner_host=task_owner_host,
        confirmed_by="local-user",
        confirmed_at=confirmed_at,
        auto_merge_expires_at=expires_at,  # type: ignore[arg-type]
        replaces_request_id=replaces_request_id,
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
