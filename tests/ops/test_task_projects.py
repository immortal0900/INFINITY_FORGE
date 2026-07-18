from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from uuid import uuid4

import pytest

from forge.ops.task_projects import (
    TaskProject,
    TaskProjectError,
    normalize_github_remote,
)


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
