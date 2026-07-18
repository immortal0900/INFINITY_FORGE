from __future__ import annotations

import hashlib
import json
import traceback
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from uuid import uuid4

import pytest

from forge.ops.task_projects import (
    TaskProject,
    TaskProjectError,
    normalize_github_remote,
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
        return tuple(text for item in value for text in _contained_text(item))
    return ()


def _task_project_traceback_local_text(error: BaseException) -> tuple[str, ...]:
    texts: list[str] = []
    for current in _exception_graph(error):
        trace = current.__traceback__
        while trace is not None:
            if trace.tb_frame.f_globals.get("__name__") == "forge.ops.task_projects":
                for value in trace.tb_frame.f_locals.values():
                    texts.extend(_contained_text(value))
            trace = trace.tb_next
    return tuple(texts)


def _binding(workspace: Path) -> dict[str, str]:
    return {
        "repository": "Arbitrary-Owner/unrelated.repo",
        "workspace": str(workspace.resolve()),
        "remote_name": "upstream",
        "base_branch": "release/next",
        "base_commit": "a" * 40,
        "host_id": str(uuid4()),
    }


def test_task_project_has_exact_immutable_public_fields(tmp_path: Path) -> None:
    project = TaskProject.create(**_binding(tmp_path))

    assert tuple(field.name for field in fields(project)) == (
        "project_id",
        "repository",
        "workspace",
        "remote_name",
        "base_branch",
        "base_commit",
        "host_id",
    )
    with pytest.raises(FrozenInstanceError):
        project.repository = "other/repository"  # type: ignore[misc]


def test_task_project_id_is_compact_key_sorted_utf8_json_sha256(
    tmp_path: Path,
) -> None:
    unicode_workspace = tmp_path / "한글-workspace"
    unicode_workspace.mkdir()
    binding = _binding(unicode_workspace)

    project = TaskProject.create(**binding)

    encoded = json.dumps(
        binding,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert project.project_id == hashlib.sha256(encoded).hexdigest()
    assert len(project.project_id) == 64
    assert project.project_id == project.project_id.lower()


def test_task_project_rejects_project_id_not_bound_to_six_fields(
    tmp_path: Path,
) -> None:
    with pytest.raises(TaskProjectError, match="project_id"):
        TaskProject(project_id="0" * 64, **_binding(tmp_path))


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("https://github.com/Some-Owner/arbitrary.repo.git", "Some-Owner/arbitrary.repo"),
        ("git@github.com:Some-Owner/arbitrary.repo.git", "Some-Owner/arbitrary.repo"),
        ("ssh://git@github.com/Some-Owner/arbitrary.repo.git", "Some-Owner/arbitrary.repo"),
    ],
)
def test_github_remote_forms_normalize_to_same_repository(
    remote: str,
    expected: str,
) -> None:
    assert normalize_github_remote(remote) == expected


@pytest.mark.parametrize(
    "remote",
    [
        "https://token@example.com/owner/repo.git",
        "https://token@github.com/owner/repo.git",
        "https://github.com/owner/repo.git?token=secret",
        "https://github.com/owner/repo.git#secret",
        "https://github.com/owner/%72epo.git",
        "http://github.com/owner/repo.git",
        "git@example.com:owner/repo.git",
        "ssh://user@github.com/owner/repo.git",
        "ssh://git@github.com:22/owner/repo.git",
        "HTTPS://github.com/owner/repo.git",
        "https://GITHUB.com/owner/repo.git",
        "https://github.com./owner/repo.git",
        "https://github.com.evil/owner/repo.git",
        "https://www.github.com/owner/repo.git",
        "https://github。com/owner/repo.git",
        "https://user%3Atoken@github.com/owner/repo.git",
        "https://github.com/owner/repo.GIT",
        "https://github.com/owner/repo.git.git",
        "git@github.com:owner/repo.git:extra",
        "git@github.com:owner/%2frepo.git",
        "ssh://git@github.com/owner/%2erepo.git",
        "file:///private/owner/repo.git",
        "C:/private/owner/repo",
        r"\\server\private\repo",
        "../private/repo",
    ],
)
def test_github_remote_rejects_ambiguous_or_non_github_locations(remote: str) -> None:
    with pytest.raises(TaskProjectError) as caught:
        normalize_github_remote(remote)
    assert "token" not in str(caught.value)
    assert "private" not in str(caught.value)
    assert remote not in str(caught.value)
    assert caught.value.__cause__ is None


def test_direct_remote_rejection_does_not_retain_credential_in_traceback() -> None:
    token = "direct-secret-token"
    remote = f"https://{token}@github.com/owner/repo.git"

    with pytest.raises(TaskProjectError) as caught:
        normalize_github_remote(remote)

    exceptions = _exception_graph(caught.value)
    assert all(
        error.__cause__ is None and error.__context__ is None
        for error in exceptions
    )
    rendered = "".join(traceback.format_exception(caught.value))
    assert token not in rendered
    assert not any(
        token in text
        for text in _task_project_traceback_local_text(caught.value)
    )
    forge_frames: list[str] = []
    trace = caught.value.__traceback__
    while trace is not None:
        if trace.tb_frame.f_globals.get("__name__") == "forge.ops.task_projects":
            forge_frames.append(trace.tb_frame.f_code.co_name)
        trace = trace.tb_next
    assert forge_frames == ["normalize_github_remote"]


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("repository", True),
        ("workspace", True),
        ("remote_name", True),
        ("base_branch", True),
        ("base_commit", True),
        ("host_id", True),
        ("repository", "missing-slash"),
        ("repository", "owner/repo/extra"),
        ("repository", "owner/한글"),
        ("remote_name", "--upload-pack=evil"),
        ("base_branch", "../escape"),
        ("base_branch", "refs/heads/main"),
        ("base_branch", "HEAD"),
        ("base_branch", "@"),
        ("base_branch", ".hidden"),
        ("base_branch", "feature/.hidden"),
        ("base_branch", "feature.lock/next"),
        ("base_commit", "A" * 40),
        ("base_commit", "a" * 39),
        ("host_id", "not-a-uuid"),
    ],
)
def test_task_project_rejects_bool_type_and_noncanonical_values(
    tmp_path: Path,
    field_name: str,
    value: object,
) -> None:
    binding: dict[str, object] = _binding(tmp_path)
    binding[field_name] = value

    with pytest.raises(TaskProjectError):
        TaskProject.create(**binding)  # type: ignore[arg-type]


def test_task_project_rejects_noncanonical_workspace(tmp_path: Path) -> None:
    child = tmp_path / "child"
    child.mkdir()
    binding = _binding(child)
    binding["workspace"] = str(child / ".." / "child")

    with pytest.raises(TaskProjectError, match="workspace"):
        TaskProject.create(**binding)


def test_task_project_allows_lowercase_head_as_an_ordinary_branch(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    binding["base_branch"] = "head"

    assert TaskProject.create(**binding).base_branch == "head"


def test_task_project_workspace_must_be_a_directory(tmp_path: Path) -> None:
    workspace_file = tmp_path / "not-a-directory"
    workspace_file.write_text("content", encoding="utf-8")

    with pytest.raises(TaskProjectError, match="workspace"):
        TaskProject.create(**_binding(workspace_file))


def test_task_project_mapping_rejects_missing_and_extra_fields(tmp_path: Path) -> None:
    project = TaskProject.create(**_binding(tmp_path))
    payload = {field.name: getattr(project, field.name) for field in fields(project)}

    with pytest.raises(TaskProjectError, match="fields"):
        TaskProject.from_mapping({**payload, "secret": "unexpected"})
    payload.pop("host_id")
    with pytest.raises(TaskProjectError, match="fields"):
        TaskProject.from_mapping(payload)


def test_stored_task_project_mapping_survives_deleted_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "deleted-after-confirm"
    workspace.mkdir()
    project = TaskProject.create(**_binding(workspace))
    payload = {field.name: getattr(project, field.name) for field in fields(project)}
    workspace.rmdir()

    assert TaskProject.from_mapping(payload) == project


@pytest.mark.parametrize(
    "attack",
    ["relative", "dot_segment", "nul", "bad_hash"],
)
def test_stored_task_project_rejects_noncanonical_missing_workspace_or_id(
    tmp_path: Path,
    attack: str,
) -> None:
    missing = tmp_path / "missing-workspace"
    valid = _binding(missing)
    encoded = json.dumps(
        valid,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload: dict[str, object] = {
        "project_id": hashlib.sha256(encoded).hexdigest(),
        **valid,
    }
    if attack == "relative":
        payload["workspace"] = "relative/missing"
    elif attack == "dot_segment":
        payload["workspace"] = str(missing.parent / ".." / missing.parent.name / missing.name)
    elif attack == "nul":
        payload["workspace"] = f"{missing}\x00suffix"
    else:
        payload["project_id"] = "0" * 64

    if attack != "bad_hash":
        changed_binding = {
            key: payload[key]
            for key in (
                "repository",
                "workspace",
                "remote_name",
                "base_branch",
                "base_commit",
                "host_id",
            )
        }
        changed_encoded = json.dumps(
            changed_binding,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        payload["project_id"] = hashlib.sha256(changed_encoded).hexdigest()

    with pytest.raises(TaskProjectError):
        TaskProject.from_mapping(payload)


def test_direct_task_project_constructor_remains_live_strict(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "deleted-before-direct-construction"
    workspace.mkdir()
    project = TaskProject.create(**_binding(workspace))
    payload = {field.name: getattr(project, field.name) for field in fields(project)}
    workspace.rmdir()

    with pytest.raises(TaskProjectError, match="workspace"):
        TaskProject(**payload)  # type: ignore[arg-type]
