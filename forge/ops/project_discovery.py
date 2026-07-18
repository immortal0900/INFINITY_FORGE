"""Bounded, deterministic discovery and validation of local GitHub projects."""

from __future__ import annotations

import os
import stat
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Protocol

from .task_projects import (
    TaskProject,
    TaskProjectError,
    _validate_branch,
    _validate_commit,
    _validate_host_id,
    _validate_remote_name,
    _validate_repository,
    normalize_github_remote,
)


DEFAULT_MAX_DEPTH = 3
DEFAULT_MAX_PROJECTS = 64
DEFAULT_TIMEOUT_SECONDS = 5.0
HARD_MAX_DEPTH = 8
HARD_MAX_PROJECTS = 256

_GIT_ENVIRONMENT_OVERRIDES = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_ASKPASS",
        "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_PARAMETERS",
        "GIT_DIR",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_EXEC_PATH",
        "GIT_INDEX_FILE",
        "GIT_GRAFT_FILE",
        "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_OPTIONAL_LOCKS",
        "GIT_PROTOCOL_FROM_USER",
        "GIT_QUARANTINE_PATH",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_WORK_TREE",
        "SSH_ASKPASS",
    }
)
_GIT_ENVIRONMENT_PREFIXES = (
    "GIT_CONFIG_KEY_",
    "GIT_CONFIG_VALUE_",
    "GIT_TRACE",
)


class ProjectDiscoveryError(ValueError):
    """Raised when complete, unambiguous project discovery is impossible."""


@dataclass(frozen=True, slots=True)
class DiscoveryLimits:
    max_depth: int = DEFAULT_MAX_DEPTH
    max_projects: int = DEFAULT_MAX_PROJECTS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class GitHubRepositoryMetadata:
    full_name: str
    default_branch: str
    branch: str
    commit_sha: str

    def __post_init__(self) -> None:
        _validate_repository(self.full_name)
        _validate_branch(self.default_branch)
        _validate_branch(self.branch)
        _validate_commit(self.commit_sha)


class GitHubMetadataReader(Protocol):
    def __call__(
        self,
        repository: str,
        branch: str | None,
        timeout: float,
    ) -> GitHubRepositoryMetadata | Mapping[str, object]: ...


GitRunner = Callable[..., subprocess.CompletedProcess[str]]
Monotonic = Callable[[], float]


@dataclass(frozen=True, slots=True)
class _GitEvidence:
    workspace: Path
    git_dir: Path
    common_dir: Path


@dataclass(frozen=True, slots=True)
class _RemoteEvidence:
    git: _GitEvidence
    remote_name: str
    repository: str


def _validate_limits(limits: object) -> DiscoveryLimits:
    if not isinstance(limits, DiscoveryLimits):
        raise ProjectDiscoveryError("discovery limits must use DiscoveryLimits")
    if (
        type(limits.max_depth) is not int
        or not 0 <= limits.max_depth <= HARD_MAX_DEPTH
        or type(limits.max_projects) is not int
        or not 1 <= limits.max_projects <= HARD_MAX_PROJECTS
        or isinstance(limits.timeout_seconds, bool)
        or not isinstance(limits.timeout_seconds, (int, float))
        or not isfinite(limits.timeout_seconds)
        or limits.timeout_seconds <= 0
    ):
        raise ProjectDiscoveryError("discovery limits are outside the safe boundary")
    return limits


def _resolve_path(path: Path) -> Path:
    """Resolve an existing path; kept small so TOCTOU behavior is testable."""

    return path.resolve(strict=True)


def _safe_resolve(path: object, label: str) -> Path:
    if isinstance(path, bool) or not isinstance(path, (str, os.PathLike)):
        raise ProjectDiscoveryError(f"{label} path boundary is invalid")
    try:
        unresolved = Path(path)
        if not unresolved.is_absolute():
            unresolved = Path.cwd() / unresolved
        unresolved = Path(os.path.abspath(unresolved))
        _assert_unresolved_no_reparse(unresolved)
        resolved = _resolve_path(unresolved)
        if not resolved.is_dir():
            raise ProjectDiscoveryError(f"{label} path boundary is invalid")
    except ProjectDiscoveryError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise ProjectDiscoveryError(f"{label} path boundary is invalid") from None
    return resolved


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _containing_root(path: Path, roots: Sequence[Path]) -> Path | None:
    containing = [root for root in roots if _is_within(path, root)]
    if not containing:
        return None
    return max(containing, key=lambda root: len(root.parts))


def _is_reparse(path: Path) -> bool:
    try:
        information = os.lstat(path)
    except OSError:
        raise ProjectDiscoveryError("project path boundary cannot be inspected") from None
    attributes = getattr(information, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(information.st_mode) or bool(attributes & reparse_flag)


def _assert_no_reparse_components(path: Path, root: Path) -> None:
    if not _is_within(path, root):
        raise ProjectDiscoveryError("project path boundary escaped allowed roots")
    current = root
    if _is_reparse(current):
        raise ProjectDiscoveryError("project path boundary contains a reparse point")
    for component in path.relative_to(root).parts:
        current /= component
        if _is_reparse(current):
            raise ProjectDiscoveryError("project path boundary contains a reparse point")


def _assert_unresolved_no_reparse(path: Path) -> None:
    """Reject a lexical path containing a symlink, junction, or reparse alias."""

    if not path.is_absolute():
        raise ProjectDiscoveryError("project path boundary is invalid")
    anchor = Path(path.anchor)
    current = anchor
    anchor_parts = len(anchor.parts)
    for component in path.parts[anchor_parts:]:
        current /= component
        if _is_reparse(current):
            raise ProjectDiscoveryError("project path boundary contains a reparse point")


def _normalize_roots(allowed_roots: object) -> tuple[Path, ...]:
    if (
        isinstance(allowed_roots, (str, bytes, os.PathLike))
        or not isinstance(allowed_roots, Sequence)
        or not allowed_roots
    ):
        raise ProjectDiscoveryError("allowed root path boundary is invalid")
    resolved_roots = tuple(
        _safe_resolve(root, "allowed root") for root in allowed_roots
    )
    unique: dict[str, Path] = {}
    for root in resolved_roots:
        _assert_no_reparse_components(root, root)
        unique.setdefault(_path_key(root), root)
    roots: list[Path] = []
    for root in sorted(
        unique.values(),
        key=lambda path: (len(path.parts), _path_key(path), str(path)),
    ):
        if any(_is_within(root, parent) for parent in roots):
            continue
        roots.append(root)
    return tuple(sorted(roots, key=lambda path: (_path_key(path), str(path))))


def _remaining(deadline: float, monotonic: Monotonic) -> float:
    try:
        now = monotonic()
    except Exception:
        raise ProjectDiscoveryError("monotonic deadline probe failed") from None
    if (
        isinstance(now, bool)
        or not isinstance(now, (int, float))
        or not isfinite(now)
    ):
        raise ProjectDiscoveryError("monotonic deadline probe failed")
    remaining = deadline - float(now)
    if remaining <= 0:
        raise ProjectDiscoveryError("project discovery timed out")
    return remaining


def _start_deadline(limits: DiscoveryLimits, monotonic: Monotonic) -> float:
    try:
        started_at = monotonic()
    except Exception:
        raise ProjectDiscoveryError("monotonic deadline probe failed") from None
    if (
        isinstance(started_at, bool)
        or not isinstance(started_at, (int, float))
        or not isfinite(started_at)
    ):
        raise ProjectDiscoveryError("monotonic deadline probe failed")
    return float(started_at) + float(limits.timeout_seconds)


def _run_git(
    directory: Path,
    arguments: Sequence[str],
    *,
    deadline: float,
    runner: GitRunner,
    monotonic: Monotonic,
    allow_not_repository: bool = False,
) -> subprocess.CompletedProcess[str] | None:
    timeout = _remaining(deadline, monotonic)
    environment = os.environ.copy()
    for key in tuple(environment):
        if key in _GIT_ENVIRONMENT_OVERRIDES or key.startswith(
            _GIT_ENVIRONMENT_PREFIXES
        ):
            environment.pop(key, None)
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "GIT_ASKPASS": "",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_PAGER": "",
            "SSH_ASKPASS": "",
        }
    )
    command = ["git", "-C", str(directory), *arguments]
    failure: str | None = None
    result: subprocess.CompletedProcess[str] | None = None
    try:
        result = runner(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        failure = "timeout"
    except (OSError, ValueError, TypeError):
        failure = "git"
    if failure == "timeout":
        raise ProjectDiscoveryError("project discovery timed out")
    if failure == "git":
        raise ProjectDiscoveryError("Git probe failed")
    _remaining(deadline, monotonic)
    result_is_invalid = (
        not isinstance(result, subprocess.CompletedProcess)
        or type(result.returncode) is not int
        or not isinstance(result.stdout, str)
    )
    if result_is_invalid:
        result = None
        raise ProjectDiscoveryError("Git probe returned an invalid result")
    if result.returncode != 0:
        result = None
        if allow_not_repository:
            return None
        raise ProjectDiscoveryError("Git probe failed")
    return result


def _one_line(stdout: str, label: str) -> str:
    lines = stdout.splitlines()
    if len(lines) != 1 or not lines[0]:
        raise ProjectDiscoveryError(f"{label} is ambiguous")
    return lines[0]


def _path_from_git(stdout: str, label: str) -> Path:
    raw_path = _one_line(stdout, label)
    try:
        path = Path(raw_path)
        if not path.is_absolute():
            raise ProjectDiscoveryError(f"{label} is invalid")
        _assert_unresolved_no_reparse(Path(os.path.abspath(path)))
        return _resolve_path(path)
    except ProjectDiscoveryError:
        raise ProjectDiscoveryError(f"{label} is invalid") from None
    except (OSError, RuntimeError, ValueError):
        raise ProjectDiscoveryError(f"{label} is invalid") from None


def _has_git_marker(path: Path) -> bool:
    marker = path / ".git"
    try:
        information = os.lstat(marker)
    except FileNotFoundError:
        return False
    except OSError:
        raise ProjectDiscoveryError("Git metadata permission is ambiguous") from None
    attributes = getattr(information, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(information.st_mode) or attributes & reparse_flag:
        raise ProjectDiscoveryError("Git metadata path boundary is invalid")
    if not (stat.S_ISDIR(information.st_mode) or stat.S_ISREG(information.st_mode)):
        raise ProjectDiscoveryError("Git metadata path boundary is invalid")
    return True


def _scan_roots(
    roots: Sequence[Path],
    limits: DiscoveryLimits,
    *,
    deadline: float,
    monotonic: Monotonic,
) -> tuple[Path, ...]:
    candidates: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        pending: list[tuple[Path, int]] = [(root, 0)]
        while pending:
            _remaining(deadline, monotonic)
            current, depth = pending.pop(0)
            resolved_before = _resolve_path(current)
            containing = _containing_root(resolved_before, roots)
            if containing is None:
                raise ProjectDiscoveryError("project path boundary escaped allowed roots")
            _assert_no_reparse_components(resolved_before, containing)
            key = _path_key(resolved_before)
            if key in seen:
                continue
            seen.add(key)
            if _has_git_marker(resolved_before):
                candidates.append(resolved_before)
                if len(candidates) > limits.max_projects:
                    raise ProjectDiscoveryError("project count limit exceeded")
                continue
            if depth >= limits.max_depth:
                continue
            try:
                with os.scandir(resolved_before) as entries:
                    ordered = sorted(entries, key=lambda entry: (entry.name.casefold(), entry.name))
            except PermissionError:
                raise ProjectDiscoveryError("project scan permission is ambiguous") from None
            except OSError:
                raise ProjectDiscoveryError("project scan failed") from None
            children: list[Path] = []
            for entry in ordered:
                _remaining(deadline, monotonic)
                try:
                    information = entry.stat(follow_symlinks=False)
                    attributes = getattr(information, "st_file_attributes", 0)
                    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                    if entry.is_symlink() or attributes & reparse_flag:
                        raise ProjectDiscoveryError(
                            "project path boundary contains a reparse point"
                        )
                    if stat.S_ISDIR(information.st_mode):
                        children.append(Path(entry.path))
                except ProjectDiscoveryError:
                    raise
                except OSError:
                    raise ProjectDiscoveryError(
                        "project scan permission is ambiguous"
                    ) from None
            pending[0:0] = [(child, depth + 1) for child in children]
    return tuple(candidates)


def _working_git_root(
    working_directory: Path,
    roots: Sequence[Path],
    *,
    deadline: float,
    runner: GitRunner,
    monotonic: Monotonic,
) -> Path | None:
    result = _run_git(
        working_directory,
        ("rev-parse", "--show-toplevel"),
        deadline=deadline,
        runner=runner,
        monotonic=monotonic,
        allow_not_repository=True,
    )
    if result is None:
        containing = _containing_root(working_directory, roots)
        if containing is not None:
            current = working_directory
            while True:
                if _has_git_marker(current):
                    raise ProjectDiscoveryError("working Git probe failed")
                if current == containing:
                    break
                current = current.parent
        return None
    root = _path_from_git(result.stdout, "working Git root")
    containing = _containing_root(root, roots)
    if containing is None:
        raise ProjectDiscoveryError("working Git root escaped allowed roots")
    _assert_no_reparse_components(root, containing)
    return root


def _remote_bindings(
    evidence: _GitEvidence,
    *,
    deadline: float,
    runner: GitRunner,
    monotonic: Monotonic,
) -> tuple[_RemoteEvidence, ...]:
    remote_result = _run_git(
        evidence.workspace,
        ("remote",),
        deadline=deadline,
        runner=runner,
        monotonic=monotonic,
    )
    assert remote_result is not None
    remote_names = remote_result.stdout.splitlines()
    if (
        not remote_names
        or any(not remote_name for remote_name in remote_names)
        or len(set(remote_names)) != len(remote_names)
    ):
        raise ProjectDiscoveryError("project remote selection is ambiguous")
    bindings: list[_RemoteEvidence] = []
    for raw_remote_name in sorted(remote_names, key=lambda value: (value.casefold(), value)):
        try:
            remote_name = _validate_remote_name(raw_remote_name)
        except TaskProjectError:
            raise ProjectDiscoveryError("project remote binding is invalid") from None
        fetch_repository = _remote_repository_from_git(
            evidence.workspace,
            ("remote", "get-url", "--all", remote_name),
            "fetch remote",
            deadline=deadline,
            runner=runner,
            monotonic=monotonic,
        )
        push_repository = _remote_repository_from_git(
            evidence.workspace,
            ("remote", "get-url", "--push", "--all", remote_name),
            "push remote",
            deadline=deadline,
            runner=runner,
            monotonic=monotonic,
        )
        if (
            fetch_repository is None
            or push_repository is None
            or fetch_repository.casefold() != push_repository.casefold()
        ):
            raise ProjectDiscoveryError("project remote binding is invalid")
        bindings.append(
            _RemoteEvidence(
                git=evidence,
                remote_name=remote_name,
                repository=fetch_repository,
            )
        )
    return tuple(bindings)


def _remote_repository_from_git(
    workspace: Path,
    arguments: Sequence[str],
    label: str,
    *,
    deadline: float,
    runner: GitRunner,
    monotonic: Monotonic,
) -> str | None:
    """Normalize one remote without retaining its credential-bearing raw output."""

    result = _run_git(
        workspace,
        arguments,
        deadline=deadline,
        runner=runner,
        monotonic=monotonic,
    )
    assert result is not None
    raw_output: str | None = result.stdout
    result = None
    repository: str | None = None
    try:
        repository = normalize_github_remote(_one_line(raw_output, label))
    except (TaskProjectError, ProjectDiscoveryError):
        repository = None
    raw_output = None
    return repository


def _git_evidence(
    workspace: Path,
    roots: Sequence[Path],
    *,
    deadline: float,
    runner: GitRunner,
    monotonic: Monotonic,
) -> _GitEvidence:
    before = _resolve_path(workspace)
    containing = _containing_root(before, roots)
    if containing is None:
        raise ProjectDiscoveryError("project path boundary escaped allowed roots")
    _assert_no_reparse_components(before, containing)
    top_result = _run_git(
        before,
        ("rev-parse", "--show-toplevel"),
        deadline=deadline,
        runner=runner,
        monotonic=monotonic,
    )
    git_dir_result = _run_git(
        before,
        ("rev-parse", "--path-format=absolute", "--git-dir"),
        deadline=deadline,
        runner=runner,
        monotonic=monotonic,
    )
    common_result = _run_git(
        before,
        ("rev-parse", "--path-format=absolute", "--git-common-dir"),
        deadline=deadline,
        runner=runner,
        monotonic=monotonic,
    )
    assert top_result is not None and git_dir_result is not None and common_result is not None
    top = _path_from_git(top_result.stdout, "Git workspace")
    git_dir = _path_from_git(git_dir_result.stdout, "Git metadata")
    common_dir = _path_from_git(common_result.stdout, "Git metadata")
    if top != before:
        raise ProjectDiscoveryError("Git workspace path boundary is invalid")
    for metadata_path in (git_dir, common_dir):
        metadata_root = _containing_root(metadata_path, roots)
        if metadata_root is None:
            raise ProjectDiscoveryError("Git metadata escaped allowed roots")
        _assert_no_reparse_components(metadata_path, metadata_root)
    after = _resolve_path(workspace)
    if after != before or _containing_root(after, roots) is None:
        raise ProjectDiscoveryError("project path boundary changed during probes")
    _assert_no_reparse_components(after, containing)
    return _GitEvidence(
        workspace=after,
        git_dir=git_dir,
        common_dir=common_dir,
    )


def _recheck_git_evidence(evidence: _GitEvidence, roots: Sequence[Path]) -> None:
    for original, label in (
        (evidence.workspace, "project path boundary"),
        (evidence.git_dir, "Git metadata"),
        (evidence.common_dir, "Git metadata"),
    ):
        try:
            current = _resolve_path(original)
        except (OSError, RuntimeError, ValueError):
            raise ProjectDiscoveryError(f"{label} changed during probes") from None
        if current != original:
            raise ProjectDiscoveryError(f"{label} changed during probes")
        root = _containing_root(current, roots)
        if root is None:
            raise ProjectDiscoveryError(f"{label} escaped allowed roots")
        _assert_no_reparse_components(current, root)


def _read_metadata(
    reader: GitHubMetadataReader,
    repository: str,
    branch: str | None,
    *,
    deadline: float,
    monotonic: Monotonic,
) -> GitHubRepositoryMetadata:
    timeout = _remaining(deadline, monotonic)
    failed = False
    raw: object = None
    try:
        raw = reader(repository, branch, timeout)
    except Exception:
        failed = True
    if failed:
        raise ProjectDiscoveryError("GitHub metadata probe failed")
    _remaining(deadline, monotonic)
    if isinstance(raw, GitHubRepositoryMetadata):
        return raw
    if not isinstance(raw, Mapping) or set(raw) != {
        "full_name",
        "default_branch",
        "branch",
        "commit_sha",
    }:
        raise ProjectDiscoveryError("GitHub metadata response is invalid")
    try:
        return GitHubRepositoryMetadata(
            full_name=raw["full_name"],
            default_branch=raw["default_branch"],
            branch=raw["branch"],
            commit_sha=raw["commit_sha"],
        )
    except (TaskProjectError, TypeError):
        raise ProjectDiscoveryError("GitHub metadata response is invalid") from None


def _local_branch_commit(
    evidence: _RemoteEvidence,
    branch: str,
    *,
    deadline: float,
    runner: GitRunner,
    monotonic: Monotonic,
) -> str:
    try:
        safe_branch = _validate_branch(branch)
    except TaskProjectError:
        raise ProjectDiscoveryError("GitHub metadata response is invalid") from None
    reference = f"refs/remotes/{evidence.remote_name}/{safe_branch}^{{commit}}"
    result = _run_git(
        evidence.git.workspace,
        ("rev-parse", "--verify", reference),
        deadline=deadline,
        runner=runner,
        monotonic=monotonic,
    )
    assert result is not None
    commit = _one_line(result.stdout, "local Git binding")
    try:
        return _validate_commit(commit)
    except TaskProjectError:
        raise ProjectDiscoveryError("local Git binding is invalid") from None


def _validate_evidence_binding(
    evidence: _RemoteEvidence,
    *,
    branch: str | None,
    expected_commit: str | None,
    expected_repository: str | None,
    deadline: float,
    runner: GitRunner,
    monotonic: Monotonic,
    github_metadata_reader: GitHubMetadataReader,
) -> tuple[str, str, str]:
    metadata = _read_metadata(
        github_metadata_reader,
        evidence.repository,
        branch,
        deadline=deadline,
        monotonic=monotonic,
    )
    selected_branch = metadata.default_branch if branch is None else branch
    if (
        metadata.full_name.casefold() != evidence.repository.casefold()
        or (
            expected_repository is not None
            and metadata.full_name != expected_repository
        )
        or metadata.branch != selected_branch
        or (branch is None and metadata.default_branch != metadata.branch)
    ):
        raise ProjectDiscoveryError("GitHub binding does not match local project")
    local_commit = _local_branch_commit(
        evidence,
        selected_branch,
        deadline=deadline,
        runner=runner,
        monotonic=monotonic,
    )
    if metadata.commit_sha != local_commit:
        raise ProjectDiscoveryError("GitHub binding does not match local project")
    if expected_commit is not None and expected_commit != local_commit:
        raise ProjectDiscoveryError("local Git binding does not match TaskProject")
    return metadata.full_name, selected_branch, local_commit


# RISK(security): This validator is the authorization boundary between stored
# TaskProject paths/remotes and later Git writes. Every binding is re-read exactly.
def validate_task_project(
    project: TaskProject,
    *,
    allowed_roots: Sequence[str | os.PathLike[str]],
    runner: GitRunner = subprocess.run,
    github_metadata_reader: GitHubMetadataReader,
    monotonic: Monotonic = time.monotonic,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> TaskProject:
    """Revalidate one explicit default or non-default Task Project binding."""

    if not isinstance(project, TaskProject):
        raise ProjectDiscoveryError("project must be a TaskProject")
    limits = _validate_limits(DiscoveryLimits(timeout_seconds=timeout_seconds))
    deadline = _start_deadline(limits, monotonic)
    _remaining(deadline, monotonic)
    roots = _normalize_roots(allowed_roots)
    _remaining(deadline, monotonic)
    workspace = _safe_resolve(project.workspace, "project")
    _remaining(deadline, monotonic)
    evidence = _git_evidence(
        workspace,
        roots,
        deadline=deadline,
        runner=runner,
        monotonic=monotonic,
    )
    remotes = _remote_bindings(
        evidence,
        deadline=deadline,
        runner=runner,
        monotonic=monotonic,
    )
    matches = [remote for remote in remotes if remote.remote_name == project.remote_name]
    if len(matches) != 1:
        raise ProjectDiscoveryError("project remote binding changed")
    _validate_evidence_binding(
        matches[0],
        branch=project.base_branch,
        expected_commit=project.base_commit,
        expected_repository=project.repository,
        deadline=deadline,
        runner=runner,
        monotonic=monotonic,
        github_metadata_reader=github_metadata_reader,
    )
    _recheck_git_evidence(evidence, roots)
    _remaining(deadline, monotonic)
    return project


def discover_projects(
    working_directory: str | os.PathLike[str],
    allowed_roots: Sequence[str | os.PathLike[str]],
    limits: DiscoveryLimits | None = None,
    *,
    host_id: str,
    runner: GitRunner = subprocess.run,
    github_metadata_reader: GitHubMetadataReader,
    monotonic: Monotonic = time.monotonic,
) -> tuple[TaskProject, ...]:
    """Discover every valid project or fail without returning a partial list."""

    checked_limits = _validate_limits(limits if limits is not None else DiscoveryLimits())
    try:
        checked_host_id = _validate_host_id(host_id)
    except TaskProjectError:
        raise ProjectDiscoveryError("host_id must be a canonical UUID string") from None
    deadline = _start_deadline(checked_limits, monotonic)
    _remaining(deadline, monotonic)
    roots = _normalize_roots(allowed_roots)
    _remaining(deadline, monotonic)
    working = _safe_resolve(working_directory, "working directory")
    _remaining(deadline, monotonic)
    working_root = _working_git_root(
        working,
        roots,
        deadline=deadline,
        runner=runner,
        monotonic=monotonic,
    )
    scanned = _scan_roots(
        roots,
        checked_limits,
        deadline=deadline,
        monotonic=monotonic,
    )
    ordered_paths: list[Path] = []
    if working_root is not None:
        ordered_paths.append(working_root)
    ordered_paths.extend(
        path
        for path in sorted(scanned, key=lambda item: (_path_key(item), str(item)))
        if working_root is None or path != working_root
    )
    if len(ordered_paths) > checked_limits.max_projects:
        raise ProjectDiscoveryError("project count limit exceeded")
    projects: list[TaskProject] = []
    common_dirs: set[str] = set()
    repository_workspaces: dict[str, str] = {}
    workspaces: set[str] = set()
    for workspace in ordered_paths:
        evidence = _git_evidence(
            workspace,
            roots,
            deadline=deadline,
            runner=runner,
            monotonic=monotonic,
        )
        workspace_key = _path_key(evidence.workspace)
        common_key = _path_key(evidence.common_dir)
        if (
            workspace_key in workspaces
            or common_key in common_dirs
        ):
            raise ProjectDiscoveryError("duplicate project binding detected")
        workspaces.add(workspace_key)
        common_dirs.add(common_key)
        remotes = _remote_bindings(
            evidence,
            deadline=deadline,
            runner=runner,
            monotonic=monotonic,
        )
        for remote in remotes:
            if len(projects) >= checked_limits.max_projects:
                raise ProjectDiscoveryError("project count limit exceeded")
            repository, branch, commit = _validate_evidence_binding(
                remote,
                branch=None,
                expected_commit=None,
                expected_repository=None,
                deadline=deadline,
                runner=runner,
                monotonic=monotonic,
                github_metadata_reader=github_metadata_reader,
            )
            repository_key = repository.casefold()
            bound_workspace = repository_workspaces.get(repository_key)
            if bound_workspace is not None and bound_workspace != workspace_key:
                raise ProjectDiscoveryError("duplicate project binding detected")
            repository_workspaces.setdefault(repository_key, workspace_key)
            _recheck_git_evidence(evidence, roots)
            try:
                projects.append(
                    TaskProject.create(
                        repository=repository,
                        workspace=str(evidence.workspace),
                        remote_name=remote.remote_name,
                        base_branch=branch,
                        base_commit=commit,
                        host_id=checked_host_id,
                    )
                )
            except TaskProjectError:
                raise ProjectDiscoveryError("project binding is invalid") from None
    _remaining(deadline, monotonic)
    return tuple(projects)
