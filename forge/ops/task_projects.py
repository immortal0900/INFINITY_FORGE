"""Strict immutable bindings between one Task and one GitHub workspace."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$", re.ASCII)
_OWNER_PATTERN = re.compile(
    r"^(?!-)(?!.*--)[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$",
    re.ASCII,
)
_REPOSITORY_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}$", re.ASCII)
_REMOTE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", re.ASCII)
_WINDOWS_FORBIDDEN_PATH_CHARACTERS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_DEVICE_BASENAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        "conin$",
        "conout$",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
        "com¹",
        "com²",
        "com³",
        "lpt¹",
        "lpt²",
        "lpt³",
    }
)
_HTTPS_REMOTE_PATTERN = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<name>[^/]+)$",
    re.ASCII,
)
_SCP_REMOTE_PATTERN = re.compile(
    r"^git@github\.com:(?P<owner>[^/]+)/(?P<name>[^/]+)$",
    re.ASCII,
)
_SSH_REMOTE_PATTERN = re.compile(
    r"^ssh://git@github\.com/(?P<owner>[^/]+)/(?P<name>[^/]+)$",
    re.ASCII,
)
_TASK_PROJECT_FIELDS = frozenset(
    {
        "project_id",
        "repository",
        "workspace",
        "remote_name",
        "base_branch",
        "base_commit",
        "host_id",
    }
)


class TaskProjectError(ValueError):
    """Raised when a project binding is ambiguous or non-canonical."""


def _is_utf8_text(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _canonical_repository(owner: object, name: object) -> str:
    if type(owner) is not str or _OWNER_PATTERN.fullmatch(owner) is None:
        raise TaskProjectError("GitHub remote is not canonical") from None
    if type(name) is not str or not name:
        raise TaskProjectError("GitHub remote is not canonical") from None
    if name.endswith(".git"):
        name = name[:-4]
    if (
        _REPOSITORY_NAME_PATTERN.fullmatch(name) is None
        or name in {".", ".."}
        or name.casefold().endswith(".git")
    ):
        raise TaskProjectError("GitHub remote is not canonical") from None
    return f"{owner}/{name}"


def _validate_repository(repository: object) -> str:
    if type(repository) is not str or repository.count("/") != 1:
        raise TaskProjectError("repository must use canonical OWNER/REPO format")
    owner, name = repository.split("/", 1)
    try:
        canonical = _canonical_repository(owner, name)
    except TaskProjectError:
        raise TaskProjectError(
            "repository must use canonical OWNER/REPO format"
        ) from None
    if canonical != repository:
        raise TaskProjectError("repository must use canonical OWNER/REPO format")
    return repository


def _try_normalize_github_remote(remote: object) -> str | None:
    if type(remote) is not str or not remote or not remote.isascii():
        return None
    if any(character.isspace() or ord(character) < 32 for character in remote):
        return None
    if "%" in remote or "?" in remote or "#" in remote:
        return None
    match = (
        _HTTPS_REMOTE_PATTERN.fullmatch(remote)
        or _SCP_REMOTE_PATTERN.fullmatch(remote)
        or _SSH_REMOTE_PATTERN.fullmatch(remote)
    )
    if match is None:
        return None
    try:
        return _canonical_repository(match.group("owner"), match.group("name"))
    except TaskProjectError:
        return None


def normalize_github_remote(remote: object) -> str:
    """Return canonical ``OWNER/REPO`` for one exact, credential-free URL."""

    repository = _try_normalize_github_remote(remote)
    remote = None
    if repository is None:
        raise TaskProjectError("GitHub remote is not canonical") from None
    return repository


def _validate_utf8_text(value: object, error_message: str) -> str:
    if type(value) is not str:
        raise TaskProjectError(error_message)
    if not _is_utf8_text(value):
        value = None
        raise TaskProjectError(error_message) from None
    return value


def _is_canonical_windows_workspace(path: Path) -> bool:
    drive = path.drive
    if (
        len(drive) == 2
        and drive[1] == ":"
        and drive[0] not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    ):
        return False
    components = path.parts[1:] if path.anchor else path.parts
    for component in components:
        if (
            any(ord(character) < 32 for character in component)
            or any(
                character in _WINDOWS_FORBIDDEN_PATH_CHARACTERS
                for character in component
            )
            or component.endswith((".", " "))
        ):
            return False
        device_basename = component.split(".", 1)[0].rstrip(" ").casefold()
        if device_basename in _WINDOWS_RESERVED_DEVICE_BASENAMES:
            return False
    return True


def _validate_workspace_text(workspace: object) -> str:
    workspace = _validate_utf8_text(
        workspace,
        "workspace must be a canonical absolute path",
    )
    if not workspace or "\x00" in workspace:
        raise TaskProjectError("workspace must be a canonical absolute path")
    try:
        path = Path(workspace)
        if not path.is_absolute():
            raise TaskProjectError("workspace must be a canonical absolute path")
        if os.path.normpath(workspace) != workspace or str(path) != workspace:
            raise TaskProjectError("workspace must be a canonical absolute path")
        if os.name == "nt" and not _is_canonical_windows_workspace(path):
            raise TaskProjectError("workspace must be a canonical absolute path")
    except (OSError, RuntimeError, ValueError):
        raise TaskProjectError("workspace must be a canonical absolute path") from None
    return workspace


def _validate_workspace(workspace: object) -> str:
    workspace = _validate_workspace_text(workspace)
    try:
        path = Path(workspace)
        resolved = path.resolve(strict=True)
        if not resolved.is_dir():
            raise TaskProjectError("workspace must be a canonical absolute path")
    except (OSError, RuntimeError, ValueError):
        raise TaskProjectError("workspace must be a canonical absolute path") from None
    if str(resolved) != workspace:
        raise TaskProjectError("workspace must be a canonical absolute path")
    return workspace


def _validate_remote_name(remote_name: object) -> str:
    if (
        type(remote_name) is not str
        or _REMOTE_NAME_PATTERN.fullmatch(remote_name) is None
        or remote_name in {".", ".."}
        or remote_name.startswith("-")
    ):
        raise TaskProjectError("remote_name must be canonical ASCII text")
    return remote_name


def _validate_branch(base_branch: object) -> str:
    base_branch = _validate_utf8_text(
        base_branch,
        "base_branch must be a canonical branch name",
    )
    if not base_branch:
        raise TaskProjectError("base_branch must be a canonical branch name")
    if base_branch.startswith(("-", "/", "refs/")) or base_branch.endswith(("/", ".")):
        raise TaskProjectError("base_branch must be a canonical branch name")
    components = base_branch.split("/")
    if (
        base_branch in {"@", "HEAD"}
        or ".." in base_branch
        or "//" in base_branch
        or "@{" in base_branch
        or base_branch.endswith(".lock")
        or any(
            component.startswith(".") or component.endswith(".lock")
            for component in components
        )
        or any(
            ord(character) < 32
            or ord(character) == 127
            or character in " ~^:?*[\\"
            for character in base_branch
        )
    ):
        raise TaskProjectError("base_branch must be a canonical branch name")
    return base_branch


def _validate_commit(base_commit: object) -> str:
    if type(base_commit) is not str or _COMMIT_PATTERN.fullmatch(base_commit) is None:
        raise TaskProjectError("base_commit must be a lowercase 40-hex commit")
    return base_commit


def _validate_host_id(host_id: object) -> str:
    if type(host_id) is not str:
        raise TaskProjectError("host_id must be a canonical UUID string")
    try:
        parsed = UUID(host_id)
    except ValueError:
        raise TaskProjectError("host_id must be a canonical UUID string") from None
    if str(parsed) != host_id:
        raise TaskProjectError("host_id must be a canonical UUID string")
    return host_id


def _binding_payload(
    *,
    repository: str,
    workspace: str,
    remote_name: str,
    base_branch: str,
    base_commit: str,
    host_id: str,
) -> dict[str, str]:
    return {
        "repository": repository,
        "workspace": workspace,
        "remote_name": remote_name,
        "base_branch": base_branch,
        "base_commit": base_commit,
        "host_id": host_id,
    }


def _project_id(payload: Mapping[str, str]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validated_binding(
    *,
    repository: object,
    workspace: object,
    remote_name: object,
    base_branch: object,
    base_commit: object,
    host_id: object,
    live_workspace: bool,
) -> dict[str, str]:
    return _binding_payload(
        repository=_validate_repository(repository),
        workspace=(
            _validate_workspace(workspace)
            if live_workspace
            else _validate_workspace_text(workspace)
        ),
        remote_name=_validate_remote_name(remote_name),
        base_branch=_validate_branch(base_branch),
        base_commit=_validate_commit(base_commit),
        host_id=_validate_host_id(host_id),
    )


def _validate_project_id(project_id: object, payload: Mapping[str, str]) -> str:
    expected_id = _project_id(payload)
    if (
        type(project_id) is not str
        or _SHA256_PATTERN.fullmatch(project_id) is None
        or project_id != expected_id
    ):
        raise TaskProjectError("project_id does not match the six binding fields")
    return project_id


def _validate_task_project_live(project: object) -> TaskProject:
    if not isinstance(project, TaskProject):
        raise TaskProjectError("project binding must be a TaskProject")
    payload = _validated_binding(
        repository=project.repository,
        workspace=project.workspace,
        remote_name=project.remote_name,
        base_branch=project.base_branch,
        base_commit=project.base_commit,
        host_id=project.host_id,
        live_workspace=True,
    )
    _validate_project_id(project.project_id, payload)
    return project


# RISK(breaking): These seven fields are the durable public Task Project record.
# Adding, removing, or silently normalizing a field changes stored binding hashes.
@dataclass(frozen=True, slots=True)
class TaskProject:
    project_id: str
    repository: str
    workspace: str
    remote_name: str
    base_branch: str
    base_commit: str
    host_id: str

    def __post_init__(self) -> None:
        payload = _validated_binding(
            repository=self.repository,
            workspace=self.workspace,
            remote_name=self.remote_name,
            base_branch=self.base_branch,
            base_commit=self.base_commit,
            host_id=self.host_id,
            live_workspace=True,
        )
        _validate_project_id(self.project_id, payload)

    @classmethod
    def create(
        cls,
        *,
        repository: str,
        workspace: str,
        remote_name: str,
        base_branch: str,
        base_commit: str,
        host_id: str,
    ) -> TaskProject:
        """Validate six fields and calculate their deterministic project ID."""

        payload = _validated_binding(
            repository=repository,
            workspace=workspace,
            remote_name=remote_name,
            base_branch=base_branch,
            base_commit=base_commit,
            host_id=host_id,
            live_workspace=True,
        )
        return cls(project_id=_project_id(payload), **payload)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> TaskProject:
        """Read an exact stored record without accepting unknown fields."""

        if not isinstance(payload, Mapping) or set(payload) != _TASK_PROJECT_FIELDS:
            raise TaskProjectError("TaskProject fields must match the exact public schema")
        binding = _validated_binding(
            repository=payload["repository"],
            workspace=payload["workspace"],
            remote_name=payload["remote_name"],
            base_branch=payload["base_branch"],
            base_commit=payload["base_commit"],
            host_id=payload["host_id"],
            live_workspace=False,
        )
        project_id = _validate_project_id(payload["project_id"], binding)
        project = object.__new__(cls)
        object.__setattr__(project, "project_id", project_id)
        for field_name, value in binding.items():
            object.__setattr__(project, field_name, value)
        return project
