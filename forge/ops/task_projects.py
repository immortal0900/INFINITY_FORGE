"""Strict immutable bindings between one Task and one GitHub workspace."""

from __future__ import annotations

import hashlib
import json
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


def _validate_workspace(workspace: object) -> str:
    if type(workspace) is not str or not workspace:
        raise TaskProjectError("workspace must be a canonical absolute path")
    try:
        path = Path(workspace)
        if not path.is_absolute():
            raise TaskProjectError("workspace must be a canonical absolute path")
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
    if type(base_branch) is not str or not base_branch:
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
    except (TypeError, ValueError, AttributeError):
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
        repository = _validate_repository(self.repository)
        workspace = _validate_workspace(self.workspace)
        remote_name = _validate_remote_name(self.remote_name)
        base_branch = _validate_branch(self.base_branch)
        base_commit = _validate_commit(self.base_commit)
        host_id = _validate_host_id(self.host_id)
        expected_id = _project_id(
            _binding_payload(
                repository=repository,
                workspace=workspace,
                remote_name=remote_name,
                base_branch=base_branch,
                base_commit=base_commit,
                host_id=host_id,
            )
        )
        if (
            type(self.project_id) is not str
            or _SHA256_PATTERN.fullmatch(self.project_id) is None
            or self.project_id != expected_id
        ):
            raise TaskProjectError("project_id does not match the six binding fields")

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

        canonical_repository = _validate_repository(repository)
        canonical_workspace = _validate_workspace(workspace)
        canonical_remote_name = _validate_remote_name(remote_name)
        canonical_branch = _validate_branch(base_branch)
        canonical_commit = _validate_commit(base_commit)
        canonical_host_id = _validate_host_id(host_id)
        payload = _binding_payload(
            repository=canonical_repository,
            workspace=canonical_workspace,
            remote_name=canonical_remote_name,
            base_branch=canonical_branch,
            base_commit=canonical_commit,
            host_id=canonical_host_id,
        )
        return cls(project_id=_project_id(payload), **payload)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> TaskProject:
        """Read an exact stored record without accepting unknown fields."""

        if not isinstance(payload, Mapping) or set(payload) != _TASK_PROJECT_FIELDS:
            raise TaskProjectError("TaskProject fields must match the exact public schema")
        try:
            return cls(
                project_id=payload["project_id"],
                repository=payload["repository"],
                workspace=payload["workspace"],
                remote_name=payload["remote_name"],
                base_branch=payload["base_branch"],
                base_commit=payload["base_commit"],
                host_id=payload["host_id"],
            )
        except TypeError:
            raise TaskProjectError("TaskProject fields have invalid types") from None
