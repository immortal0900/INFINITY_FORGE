from __future__ import annotations

import subprocess
import os
from pathlib import Path

import pytest

from forge.ops.task_projects import TaskProject
from forge.ops.task_worktrees import (
    TaskWorktreeError,
    TaskWorktreeManager,
    task_branch_name,
)


HOST_ID = "d6f70d5d-6482-45f5-80d2-219ec2ad4d19"
REQUEST_ID = "4485be21-2a8f-41b8-a2a2-e25722df284e"


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        check=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _project_repo(tmp_path: Path) -> tuple[Path, Path, TaskProject]:
    remote = tmp_path / "remote.git"
    workspace = tmp_path / "unrelated-project-name"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "clone", str(remote), str(workspace)],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    _git(workspace, "config", "user.name", "Test User")
    _git(workspace, "config", "user.email", "test@example.com")
    (workspace / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(workspace, "add", "tracked.txt")
    _git(workspace, "commit", "-m", "base")
    _git(workspace, "branch", "-M", "main")
    _git(workspace, "push", "-u", "origin", "main")
    base_commit = _git(workspace, "rev-parse", "HEAD")
    # TaskProject intentionally stores the canonical GitHub identity, while the
    # test remote is local. The manager's resolver is the trusted GitHub identity
    # readback used by production discovery/gh integration.
    project = TaskProject.create(
        repository="example/arbitrary-project",
        workspace=str(workspace.resolve()),
        remote_name="origin",
        base_branch="main",
        base_commit=base_commit,
        host_id=HOST_ID,
    )
    return remote, workspace, project


def _manager(tmp_path: Path) -> TaskWorktreeManager:
    return TaskWorktreeManager(
        tmp_path / "task-worktrees",
        remote_repository=lambda _workspace, _remote: "example/arbitrary-project",
    )


def test_prepare_uses_deterministic_branch_and_preserves_dirty_checkout(
    tmp_path: Path,
) -> None:
    _remote, workspace, project = _project_repo(tmp_path)
    (workspace / "tracked.txt").write_text("local uncommitted work\n", encoding="utf-8")
    (workspace / "untracked.txt").write_text("keep me\n", encoding="utf-8")
    before = _git(workspace, "status", "--porcelain=v1", "--untracked-files=all")
    manager = _manager(tmp_path)

    first = manager.prepare(REQUEST_ID, project)
    second = manager.prepare(REQUEST_ID, project)

    assert first == second
    assert first.branch_name == task_branch_name(REQUEST_ID, project.project_id)
    assert first.worktree_path.is_dir()
    assert _git(first.worktree_path, "rev-parse", "HEAD") == project.base_commit
    assert _git(first.worktree_path, "status", "--porcelain=v1") == ""
    assert _git(workspace, "status", "--porcelain=v1", "--untracked-files=all") == before


def test_prepare_rejects_branch_name_owned_by_another_commit(tmp_path: Path) -> None:
    _remote, workspace, project = _project_repo(tmp_path)
    branch = task_branch_name(REQUEST_ID, project.project_id)
    (workspace / "collision.txt").write_text("collision\n", encoding="utf-8")
    _git(workspace, "add", "collision.txt")
    _git(workspace, "commit", "-m", "collision")
    _git(workspace, "branch", branch)

    with pytest.raises(TaskWorktreeError, match="branch.*collision"):
        _manager(tmp_path).prepare(REQUEST_ID, project)


def test_prepare_rejects_stale_confirmed_base(tmp_path: Path) -> None:
    remote, workspace, project = _project_repo(tmp_path)
    updater = tmp_path / "updater"
    subprocess.run(
        ["git", "clone", str(remote), str(updater)],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    _git(updater, "config", "user.name", "Test User")
    _git(updater, "config", "user.email", "test@example.com")
    _git(updater, "checkout", "main")
    (updater / "remote.txt").write_text("new remote base\n", encoding="utf-8")
    _git(updater, "add", "remote.txt")
    _git(updater, "commit", "-m", "advance remote")
    _git(updater, "push", "origin", "main")

    with pytest.raises(TaskWorktreeError, match="base commit.*changed"):
        _manager(tmp_path).prepare(REQUEST_ID, project)

    assert not (_manager(tmp_path).worktree_path(REQUEST_ID, project)).exists()
    assert _git(workspace, "status", "--porcelain=v1") == ""


def test_prepare_rejects_wrong_remote_repository_before_git_write(
    tmp_path: Path,
) -> None:
    _remote, _workspace, project = _project_repo(tmp_path)
    manager = TaskWorktreeManager(
        tmp_path / "task-worktrees",
        remote_repository=lambda _workspace, _remote: "other/wrong-project",
    )

    with pytest.raises(TaskWorktreeError, match="remote repository"):
        manager.prepare(REQUEST_ID, project)

    assert not manager.worktree_path(REQUEST_ID, project).exists()


def test_inspect_validates_without_creating_branch_or_worktree(tmp_path: Path) -> None:
    _remote, workspace, project = _project_repo(tmp_path)
    manager = _manager(tmp_path)

    planned = manager.inspect(REQUEST_ID, project)

    assert not planned.worktree_path.exists()
    result = subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "show-ref",
            "--verify",
            f"refs/heads/{planned.branch_name}",
        ],
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0


def test_prepare_replays_clean_descendant_commit_in_recorded_worktree(
    tmp_path: Path,
) -> None:
    _remote, _workspace, project = _project_repo(tmp_path)
    manager = _manager(tmp_path)
    prepared = manager.prepare(REQUEST_ID, project)
    _git(prepared.worktree_path, "config", "user.name", "Test User")
    _git(prepared.worktree_path, "config", "user.email", "test@example.com")
    (prepared.worktree_path / "result.txt").write_text("built\n", encoding="utf-8")
    _git(prepared.worktree_path, "add", "result.txt")
    _git(prepared.worktree_path, "commit", "-m", "build result")
    advanced = _git(prepared.worktree_path, "rev-parse", "HEAD")

    replayed = manager.prepare(
        REQUEST_ID,
        project,
        expected_head_commit=advanced,
    )

    assert replayed == prepared
    assert _git(replayed.worktree_path, "rev-parse", "HEAD") == advanced


def test_prepare_rejects_descendant_not_equal_to_recorded_result_head(
    tmp_path: Path,
) -> None:
    _remote, _workspace, project = _project_repo(tmp_path)
    manager = _manager(tmp_path)
    prepared = manager.prepare(REQUEST_ID, project)
    _git(prepared.worktree_path, "config", "user.name", "Test User")
    _git(prepared.worktree_path, "config", "user.email", "test@example.com")
    (prepared.worktree_path / "result.txt").write_text("built\n", encoding="utf-8")
    _git(prepared.worktree_path, "add", "result.txt")
    _git(prepared.worktree_path, "commit", "-m", "recorded build")
    recorded_head = _git(prepared.worktree_path, "rev-parse", "HEAD")
    (prepared.worktree_path / "injected.txt").write_text("foreign\n", encoding="utf-8")
    _git(prepared.worktree_path, "add", "injected.txt")
    _git(prepared.worktree_path, "commit", "-m", "unrecorded injection")

    with pytest.raises(TaskWorktreeError, match="recorded result HEAD"):
        manager.prepare(
            REQUEST_ID,
            project,
            expected_head_commit=recorded_head,
        )


def test_branch_identity_uses_full_request_entropy(tmp_path: Path) -> None:
    _remote, _workspace, project = _project_repo(tmp_path)
    same_prefix = "4485be21-ffff-4fff-8fff-ffffffffffff"

    first = task_branch_name(REQUEST_ID, project.project_id)
    second = task_branch_name(same_prefix, project.project_id)

    assert first != second
    assert _manager(tmp_path).worktree_path(REQUEST_ID, project) != _manager(
        tmp_path
    ).worktree_path(same_prefix, project)


def test_prepare_rejects_legacy_short_identity_branch(tmp_path: Path) -> None:
    _remote, workspace, project = _project_repo(tmp_path)
    legacy = f"forge/task-{REQUEST_ID[:8]}-{project.project_id[:12]}"
    _git(workspace, "branch", legacy, project.base_commit)

    with pytest.raises(TaskWorktreeError, match="legacy.*collision"):
        _manager(tmp_path).prepare(REQUEST_ID, project)


def test_registered_worktree_cannot_escape_configured_root_through_link(
    tmp_path: Path,
) -> None:
    _remote, workspace, project = _project_repo(tmp_path)
    manager = _manager(tmp_path)
    expected = manager.worktree_path(REQUEST_ID, project)
    outside = tmp_path / "outside-worktree"
    branch = task_branch_name(REQUEST_ID, project.project_id)
    _git(workspace, "worktree", "add", "-b", branch, str(outside), project.base_commit)
    expected.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(expected), str(outside)],
            check=True,
            capture_output=True,
        )
    else:
        os.symlink(outside, expected, target_is_directory=True)
    try:
        with pytest.raises(TaskWorktreeError, match="configured worktree root"):
            manager.prepare(REQUEST_ID, project)
    finally:
        if os.name == "nt":
            os.rmdir(expected)
        else:
            expected.unlink()
