"""Create one deterministic, isolated Git worktree for each confirmed Project."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from .task_projects import TaskProject, TaskProjectError, normalize_github_remote


_PROJECT_ID = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_BRANCH = re.compile(r"^forge/task-[0-9a-f]{64}$", re.ASCII)
_COMMIT = re.compile(r"^[0-9a-f]{40}$", re.ASCII)


class TaskWorktreeError(RuntimeError):
    """Raised before a Project binding could be changed or confused."""


@dataclass(frozen=True, slots=True)
class TaskWorktree:
    """Exact branch and worktree prepared for one confirmed Project."""

    branch_name: str
    worktree_path: Path
    base_commit: str


def task_branch_name(request_id: str, project_id: str) -> str:
    """Return the stable branch name owned by one request and Project."""

    try:
        parsed = UUID(request_id)
    except (TypeError, ValueError) as error:
        raise TaskWorktreeError("request_id must be a canonical UUID") from error
    if str(parsed) != request_id:
        raise TaskWorktreeError("request_id must be a canonical UUID")
    if not isinstance(project_id, str) or _PROJECT_ID.fullmatch(project_id) is None:
        raise TaskWorktreeError("project_id must be a lowercase SHA-256")
    identity = hashlib.sha256(
        f"{request_id}\0{project_id}".encode("ascii")
    ).hexdigest()
    return f"forge/task-{identity}"


class TaskWorktreeManager:
    """Validate immutable Git evidence, then prepare an idempotent worktree."""

    def __init__(
        self,
        worktree_root: str | Path,
        *,
        remote_repository: Callable[[Path, str], str] | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        root = Path(worktree_root).expanduser().resolve()
        if root.exists() and not root.is_dir():
            raise TaskWorktreeError("worktree root must be a directory")
        self._root = root
        self._remote_repository = remote_repository or self._read_remote_repository
        self._runner = runner

    def worktree_path(self, request_id: str, project: TaskProject) -> Path:
        if not isinstance(project, TaskProject):
            raise TypeError("project must be a TaskProject")
        branch = task_branch_name(request_id, project.project_id)
        identity = branch.removeprefix("forge/task-")
        destination = self._root / f"task-{identity[:24]}"
        self._require_path_within_root(destination)
        return destination

    def prepare(
        self,
        request_id: str,
        project: TaskProject,
        *,
        expected_head_commit: str | None = None,
    ) -> TaskWorktree:
        """Create or replay one worktree without touching original checkout files."""

        planned = self.inspect(
            request_id,
            project,
            expected_head_commit=expected_head_commit,
        )
        workspace = Path(project.workspace)
        if planned.worktree_path.is_dir():
            return planned
        before = self._git(workspace, "status", "--porcelain=v1", "--untracked-files=all")
        try:
            existing_branch = self._branch_commit(workspace, planned.branch_name)
            self._root.mkdir(parents=True, exist_ok=True)
            if existing_branch is None:
                self._git_write(
                    workspace,
                    "worktree",
                    "add",
                    "-b",
                    planned.branch_name,
                    str(planned.worktree_path),
                    project.base_commit,
                )
            else:
                self._git_write(
                    workspace,
                    "worktree",
                    "add",
                    str(planned.worktree_path),
                    planned.branch_name,
                )
            self._require_exact_worktree(
                workspace,
                planned.worktree_path,
                planned.branch_name,
                project.base_commit,
                expected_head_commit or project.base_commit,
            )
        finally:
            after = self._git(
                workspace,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            )
            if after != before:
                raise TaskWorktreeError("original checkout state changed")
        return planned

    def inspect(
        self,
        request_id: str,
        project: TaskProject,
        *,
        expected_head_commit: str | None = None,
    ) -> TaskWorktree:
        """Validate and plan a worktree without creating a branch or directory."""

        if not isinstance(project, TaskProject):
            raise TypeError("project must be a TaskProject")
        workspace = Path(project.workspace)
        if expected_head_commit is not None and (
            not isinstance(expected_head_commit, str)
            or _COMMIT.fullmatch(expected_head_commit) is None
        ):
            raise TaskWorktreeError(
                "expected_head_commit must be a lowercase 40-hex commit"
            )
        branch = task_branch_name(request_id, project.project_id)
        if _BRANCH.fullmatch(branch) is None:
            raise TaskWorktreeError("task branch name is invalid")
        destination = self.worktree_path(request_id, project)
        before = self._git(workspace, "status", "--porcelain=v1", "--untracked-files=all")
        try:
            self._require_workspace(workspace)
            self._require_remote(project, workspace)
            self._require_confirmed_base(project, workspace)
            legacy_branch = (
                f"forge/task-{request_id[:8]}-{project.project_id[:12]}"
            )
            legacy_path = self._root / (
                f"{project.repository.rsplit('/', 1)[1]}-"
                f"{request_id[:8]}-{project.project_id[:12]}"
            )
            if (
                self._branch_commit(workspace, legacy_branch) is not None
                or self._registered_branch_path(workspace, legacy_branch) is not None
                or legacy_path.exists()
            ):
                raise TaskWorktreeError("legacy task identity collision")
            existing_branch = self._branch_commit(workspace, branch)
            registered = self._registered_branch_path(workspace, branch)
            if registered is not None and registered != destination:
                raise TaskWorktreeError("task branch collision is registered elsewhere")
            if (
                existing_branch is not None
                and existing_branch != project.base_commit
                and registered is None
            ):
                raise TaskWorktreeError("task branch collision has a different commit")
            if registered is None and destination.exists():
                raise TaskWorktreeError("task worktree path collision")
            if (
                registered is None
                and expected_head_commit is not None
                and expected_head_commit != project.base_commit
            ):
                raise TaskWorktreeError(
                    "recorded result HEAD has no registered task worktree"
                )
            if registered is not None:
                self._require_exact_worktree(
                    workspace,
                    destination,
                    branch,
                    project.base_commit,
                    expected_head_commit or project.base_commit,
                )
        finally:
            after = self._git(
                workspace,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            )
            if after != before:
                raise TaskWorktreeError("original checkout state changed")
        return TaskWorktree(branch, destination, project.base_commit)

    def _require_workspace(self, workspace: Path) -> None:
        if not workspace.is_dir():
            raise TaskWorktreeError("Project workspace does not exist")
        root = Path(self._git(workspace, "rev-parse", "--show-toplevel")).resolve()
        if root != workspace.resolve():
            raise TaskWorktreeError("Project workspace is not the exact Git root")

    def _require_remote(self, project: TaskProject, workspace: Path) -> None:
        try:
            repository = self._remote_repository(workspace, project.remote_name)
        except (TaskProjectError, subprocess.SubprocessError, OSError) as error:
            raise TaskWorktreeError("Project remote repository could not be verified") from error
        if repository != project.repository:
            raise TaskWorktreeError("Project remote repository does not match settings")

    def _require_confirmed_base(self, project: TaskProject, workspace: Path) -> None:
        output = self._git(
            workspace,
            "ls-remote",
            "--exit-code",
            project.remote_name,
            f"refs/heads/{project.base_branch}",
        )
        rows = [line.split("\t", 1) for line in output.splitlines() if line]
        if len(rows) != 1 or len(rows[0]) != 2:
            raise TaskWorktreeError("Project base branch remote readback is ambiguous")
        remote_commit, remote_ref = rows[0]
        if remote_ref != f"refs/heads/{project.base_branch}":
            raise TaskWorktreeError("Project base branch remote readback changed")
        if remote_commit != project.base_commit:
            raise TaskWorktreeError("Project base commit changed after confirmation")
        self._git(workspace, "cat-file", "-e", f"{project.base_commit}^{{commit}}")

    def _branch_commit(self, workspace: Path, branch: str) -> str | None:
        result = self._run(
            workspace,
            "show-ref",
            "--verify",
            "--hash",
            f"refs/heads/{branch}",
            check=False,
        )
        if result.returncode != 0 and not result.stdout:
            return None
        if result.returncode != 0:
            raise TaskWorktreeError("task branch readback failed")
        return result.stdout.strip()

    def _registered_branch_path(self, workspace: Path, branch: str) -> Path | None:
        output = self._git(workspace, "worktree", "list", "--porcelain")
        current_path: Path | None = None
        found: list[Path] = []
        for line in (*output.splitlines(), ""):
            if line.startswith("worktree "):
                raw_path = Path(line.removeprefix("worktree "))
                if not raw_path.is_absolute():
                    raise TaskWorktreeError("Git worktree metadata path is not absolute")
                current_path = raw_path.absolute()
            elif line == f"branch refs/heads/{branch}" and current_path is not None:
                found.append(current_path)
            elif not line:
                current_path = None
        if len(found) > 1:
            raise TaskWorktreeError("task branch is registered in multiple worktrees")
        return found[0] if found else None

    def _require_exact_worktree(
        self,
        source_workspace: Path,
        destination: Path,
        branch: str,
        base_commit: str,
        expected_head_commit: str,
    ) -> None:
        self._require_path_within_root(destination)
        if not destination.is_dir():
            raise TaskWorktreeError("task worktree was not created")
        root = Path(self._git(destination, "rev-parse", "--show-toplevel")).resolve()
        current_branch = self._git(destination, "symbolic-ref", "--short", "HEAD")
        current_commit = self._git(destination, "rev-parse", "HEAD")
        status = self._git(destination, "status", "--porcelain=v1", "--untracked-files=all")
        ancestry = self._run(
            destination,
            "merge-base",
            "--is-ancestor",
            base_commit,
            "HEAD",
            check=False,
        )
        source_common = self._git_path(
            source_workspace,
            self._git(source_workspace, "rev-parse", "--git-common-dir"),
        )
        destination_common = self._git_path(
            destination,
            self._git(destination, "rev-parse", "--git-common-dir"),
        )
        if (
            root != destination.resolve()
            or current_branch != branch
            or ancestry.returncode != 0
            or current_commit != expected_head_commit
            or status
            or source_common != destination_common
        ):
            if current_commit != expected_head_commit:
                raise TaskWorktreeError(
                    "task worktree does not match recorded result HEAD"
                )
            raise TaskWorktreeError("task worktree readback does not match Project")

    def _require_path_within_root(self, destination: Path) -> None:
        absolute = destination.absolute()
        resolved = destination.resolve(strict=False)
        if not resolved.is_relative_to(self._root):
            raise TaskWorktreeError(
                "task worktree escapes the configured worktree root"
            )
        if destination.exists() and resolved != absolute:
            raise TaskWorktreeError(
                "task worktree link escapes the configured worktree root"
            )

    @staticmethod
    def _git_path(workspace: Path, value: str) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = workspace / path
        return path.resolve()

    def _read_remote_repository(self, workspace: Path, remote_name: str) -> str:
        return normalize_github_remote(
            self._git(workspace, "remote", "get-url", "--push", remote_name)
        )

    def _git(self, workspace: Path, *args: str) -> str:
        result = self._run(workspace, *args, check=True)
        return result.stdout.strip()

    def _git_write(self, workspace: Path, *args: str) -> None:
        self._run(workspace, *args, check=True)

    def _run(
        self,
        workspace: Path,
        *args: str,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return self._runner(
                ["git", "-C", str(workspace), *args],
                capture_output=True,
                check=check,
                text=True,
                encoding="utf-8",
                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
                timeout=30,
            )
        except (subprocess.SubprocessError, OSError) as error:
            raise TaskWorktreeError(f"Git command failed: {' '.join(args)}") from error
