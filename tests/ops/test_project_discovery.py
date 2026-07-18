from __future__ import annotations

import os
import shutil
import subprocess
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from forge.ops.project_discovery import (
    DiscoveryLimits,
    GitHubRepositoryMetadata,
    ProjectDiscoveryError,
    discover_projects,
    validate_task_project,
)
from forge.ops.task_projects import TaskProject


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
    if isinstance(value, subprocess.CompletedProcess):
        return (
            *_contained_text(value.args),
            *_contained_text(value.stdout),
            *_contained_text(value.stderr),
        )
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


def _library_traceback_local_text(error: BaseException) -> tuple[str, ...]:
    texts: list[str] = []
    for current in _exception_graph(error):
        trace = current.__traceback__
        while trace is not None:
            module_name = trace.tb_frame.f_globals.get("__name__", "")
            if isinstance(module_name, str) and module_name.startswith("forge.ops"):
                for value in trace.tb_frame.f_locals.values():
                    texts.extend(_contained_text(value))
            trace = trace.tb_next
    return tuple(texts)


@dataclass(frozen=True)
class RepoFixture:
    root: Path
    repository: str
    remote: str
    branch: str = "main"
    commit: str = "a" * 40
    remote_name: str = "origin"
    common_dir: Path | None = None
    push_remote: str | None = None
    remote_names: tuple[str, ...] | None = None
    fetch_urls: tuple[str, ...] | None = None
    push_urls: tuple[str, ...] | None = None
    remote_urls: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] | None = None
    git_dir_stdout: str | None = None
    common_dir_stdout: str | None = None


class FakeGitRunner:
    def __init__(self, repositories: list[RepoFixture]) -> None:
        self.repositories = repositories
        self.timeouts: list[float] = []
        self.environments: list[dict[str, str]] = []
        self.commands: list[list[str]] = []
        self.keyword_arguments: list[dict[str, Any]] = []
        self.raise_timeout = False

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.timeouts.append(kwargs["timeout"])
        self.environments.append(kwargs["env"])
        self.commands.append(command)
        self.keyword_arguments.append(kwargs)
        if self.raise_timeout:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        root_argument = Path(command[command.index("-C") + 1]).resolve()
        fixture = self._fixture_for(root_argument)
        arguments = command[command.index("-C") + 2 :]
        if arguments == ["rev-parse", "--show-toplevel"]:
            if fixture is None:
                return self._result(command, 128, "", "not a git repository")
            return self._result(command, 0, f"{fixture.root.resolve()}\n", "")
        if fixture is None or root_argument != fixture.root.resolve():
            return self._result(command, 128, "", "invalid git root")
        if arguments == ["rev-parse", "--path-format=absolute", "--git-common-dir"]:
            common = fixture.common_dir or fixture.root / ".git"
            stdout = fixture.common_dir_stdout or f"{common.resolve()}\n"
            return self._result(command, 0, stdout, "")
        if arguments == ["rev-parse", "--path-format=absolute", "--git-dir"]:
            stdout = fixture.git_dir_stdout or f"{(fixture.root / '.git').resolve()}\n"
            return self._result(command, 0, stdout, "")
        if arguments == ["remote"]:
            remotes = fixture.remote_names
            if remotes is None:
                remotes = (
                    tuple(fixture.remote_urls)
                    if fixture.remote_urls is not None
                    else (fixture.remote_name,)
                )
            return self._result(command, 0, "".join(f"{item}\n" for item in remotes), "")
        if arguments[:3] == ["remote", "get-url", "--all"]:
            remote_name = arguments[-1]
            urls = (
                fixture.remote_urls[remote_name][0]
                if fixture.remote_urls is not None
                else fixture.fetch_urls
            )
            if urls is None:
                urls = (fixture.remote,)
            return self._result(command, 0, "".join(f"{item}\n" for item in urls), "")
        if arguments[:4] == ["remote", "get-url", "--push", "--all"]:
            remote_name = arguments[-1]
            urls = (
                fixture.remote_urls[remote_name][1]
                if fixture.remote_urls is not None
                else fixture.push_urls
            )
            if urls is None:
                urls = (fixture.push_remote or fixture.remote,)
            return self._result(command, 0, "".join(f"{item}\n" for item in urls), "")
        if arguments[:2] == ["rev-parse", "--verify"]:
            return self._result(command, 0, f"{fixture.commit}\n", "")
        raise AssertionError(f"unexpected git command: {arguments!r}")

    def _fixture_for(self, path: Path) -> RepoFixture | None:
        matches = [
            fixture
            for fixture in self.repositories
            if path == fixture.root.resolve() or fixture.root.resolve() in path.parents
        ]
        return max(matches, key=lambda fixture: len(fixture.root.parts), default=None)

    @staticmethod
    def _result(
        command: list[str],
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)


class FakeGitHubReader:
    def __init__(self, fixtures: list[RepoFixture]) -> None:
        self.fixtures = {fixture.repository.casefold(): fixture for fixture in fixtures}
        self.calls: list[tuple[str, str | None, float]] = []
        self.overrides: dict[str, GitHubRepositoryMetadata] = {}

    def __call__(
        self,
        repository: str,
        branch: str | None,
        timeout: float,
    ) -> GitHubRepositoryMetadata:
        self.calls.append((repository, branch, timeout))
        override = self.overrides.get(repository) or self.overrides.get(repository.casefold())
        if override is not None:
            return override
        fixture = self.fixtures[repository.casefold()]
        selected_branch = branch or fixture.branch
        return GitHubRepositoryMetadata(
            full_name=fixture.repository,
            default_branch=fixture.branch,
            branch=selected_branch,
            commit_sha=fixture.commit,
        )


def _make_repo(
    parent: Path,
    name: str,
    repository: str,
    *,
    remote_form: str = "https",
    **overrides: Any,
) -> RepoFixture:
    root = parent / name
    root.mkdir(parents=True)
    (root / ".git").mkdir()
    if remote_form == "https":
        remote = f"https://github.com/{repository}.git"
    elif remote_form == "scp":
        remote = f"git@github.com:{repository}.git"
    else:
        remote = f"ssh://git@github.com/{repository}.git"
    return RepoFixture(root=root, repository=repository, remote=remote, **overrides)


def _discover(
    working_directory: Path,
    allowed_roots: tuple[Path, ...],
    fixtures: list[RepoFixture],
    **kwargs: Any,
) -> tuple[TaskProject, ...]:
    runner = kwargs.pop("runner", FakeGitRunner(fixtures))
    reader = kwargs.pop("github_metadata_reader", FakeGitHubReader(fixtures))
    return discover_projects(
        working_directory,
        allowed_roots,
        kwargs.pop("limits", DiscoveryLimits()),
        host_id=kwargs.pop("host_id", str(uuid4())),
        runner=runner,
        github_metadata_reader=reader,
        **kwargs,
    )


def test_discovers_working_directory_git_root_before_sorted_allowed_roots(
    tmp_path: Path,
) -> None:
    alpha = _make_repo(tmp_path, "alpha", "people/alpha")
    current = _make_repo(tmp_path, "z-current", "people/current", remote_form="scp")
    nested_cwd = current.root / "src" / "package"
    nested_cwd.mkdir(parents=True)

    projects = _discover(nested_cwd, (tmp_path,), [alpha, current])

    assert [project.repository for project in projects] == [
        "people/current",
        "people/alpha",
    ]
    assert projects[0].workspace == str(current.root.resolve())


def test_discovers_arbitrary_repository_names_at_exact_depth_boundary(
    tmp_path: Path,
) -> None:
    at_depth_three = _make_repo(
        tmp_path / "one" / "two",
        "three",
        "random-owner/not-a-forge-name",
        remote_form="ssh",
    )
    at_depth_four = _make_repo(
        tmp_path / "a" / "b" / "c",
        "d",
        "random-owner/too-deep",
    )

    projects = _discover(
        tmp_path,
        (tmp_path,),
        [at_depth_three, at_depth_four],
        limits=DiscoveryLimits(max_depth=3),
    )

    assert [project.repository for project in projects] == [
        "random-owner/not-a-forge-name"
    ]


def test_root_itself_is_depth_zero(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    root_repo = RepoFixture(
        root=tmp_path,
        repository="owner/root",
        remote="https://github.com/owner/root.git",
    )

    projects = _discover(tmp_path, (tmp_path,), [root_repo], limits=DiscoveryLimits(max_depth=0))

    assert [project.repository for project in projects] == ["owner/root"]


@pytest.mark.parametrize(
    "limits",
    [
        DiscoveryLimits(max_depth=9),
        DiscoveryLimits(max_projects=257),
        DiscoveryLimits(max_depth=True),
        DiscoveryLimits(max_projects=True),
        DiscoveryLimits(timeout_seconds=True),
        DiscoveryLimits(timeout_seconds=float("nan")),
        DiscoveryLimits(timeout_seconds=float("inf")),
        DiscoveryLimits(timeout_seconds=float("-inf")),
    ],
)
def test_discovery_limits_reject_hard_limit_and_bool_ambiguity(
    tmp_path: Path,
    limits: DiscoveryLimits,
) -> None:
    with pytest.raises(ProjectDiscoveryError, match="limits"):
        _discover(tmp_path, (tmp_path,), [], limits=limits)


def test_project_count_limit_fails_without_partial_result(tmp_path: Path) -> None:
    first = _make_repo(tmp_path, "one", "owner/one")
    second = _make_repo(tmp_path, "two", "owner/two")

    with pytest.raises(ProjectDiscoveryError, match="limit"):
        _discover(
            tmp_path,
            (tmp_path,),
            [first, second],
            limits=DiscoveryLimits(max_projects=1),
        )


@pytest.mark.parametrize("host_id", [True, "not-a-uuid"])
def test_invalid_host_id_fails_before_git_or_github_probe(
    tmp_path: Path,
    host_id: object,
) -> None:
    fixture = _make_repo(tmp_path, "repo", "owner/repo")
    runner = FakeGitRunner([fixture])
    reader = FakeGitHubReader([fixture])

    with pytest.raises(ProjectDiscoveryError, match="host_id"):
        _discover(
            tmp_path,
            (tmp_path,),
            [fixture],
            runner=runner,
            github_metadata_reader=reader,
            host_id=host_id,
        )
    assert runner.commands == []
    assert reader.calls == []


def test_project_count_accepts_exact_64_and_rejects_65_without_partial_result(
    tmp_path: Path,
) -> None:
    fixture = _make_repo(tmp_path, "repo", "owner/repo-00")
    remote_urls = {
        f"remote-{index:02d}": (
            (f"https://github.com/owner/repo-{index:02d}.git",),
            (f"git@github.com:owner/repo-{index:02d}.git",),
        )
        for index in range(65)
    }
    exact = RepoFixture(
        **{**fixture.__dict__, "remote_urls": dict(list(remote_urls.items())[:64])}
    )
    reader = FakeGitHubReader([exact])
    for index in range(64):
        repository = f"owner/repo-{index:02d}"
        reader.overrides[repository] = GitHubRepositoryMetadata(
            full_name=repository,
            default_branch=fixture.branch,
            branch=fixture.branch,
            commit_sha=fixture.commit,
        )

    projects = _discover(
        tmp_path,
        (tmp_path,),
        [exact],
        limits=DiscoveryLimits(max_projects=64),
        github_metadata_reader=reader,
    )
    assert len(projects) == 64

    overflow = RepoFixture(**{**fixture.__dict__, "remote_urls": remote_urls})
    reader.overrides["owner/repo-64"] = GitHubRepositoryMetadata(
        full_name="owner/repo-64",
        default_branch=fixture.branch,
        branch=fixture.branch,
        commit_sha=fixture.commit,
    )
    with pytest.raises(ProjectDiscoveryError, match="limit"):
        _discover(
            tmp_path,
            (tmp_path,),
            [overflow],
            limits=DiscoveryLimits(max_projects=64),
            github_metadata_reader=reader,
        )


def test_hard_depth_and_project_configuration_boundary_is_accepted(
    tmp_path: Path,
) -> None:
    assert _discover(
        tmp_path,
        (tmp_path,),
        [],
        limits=DiscoveryLimits(max_depth=8, max_projects=256),
    ) == ()


def test_single_deadline_remaining_time_reaches_git_and_github_probes(
    tmp_path: Path,
) -> None:
    fixture = _make_repo(tmp_path, "repo", "owner/repo")
    runner = FakeGitRunner([fixture])
    reader = FakeGitHubReader([fixture])
    moments = iter([10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9,
                    11.0, 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 11.8, 11.9,
                    12.0, 12.1, 12.2, 12.3, 12.4, 12.5, 12.6, 12.7, 12.8, 12.9])

    _discover(
        tmp_path,
        (tmp_path,),
        [fixture],
        runner=runner,
        github_metadata_reader=reader,
        monotonic=lambda: next(moments),
    )

    assert runner.timeouts
    assert reader.calls
    all_timeouts = runner.timeouts + [call[2] for call in reader.calls]
    assert all(0 < value <= 5.0 for value in all_timeouts)
    assert all(
        later < earlier
        for earlier, later in zip(runner.timeouts, runner.timeouts[1:])
    )
    assert all(env["GIT_TERMINAL_PROMPT"] == "0" for env in runner.environments)
    assert all(env["GCM_INTERACTIVE"] == "Never" for env in runner.environments)


def test_timeout_and_subprocess_timeout_are_explicit_and_sanitized(
    tmp_path: Path,
) -> None:
    fixture = _make_repo(tmp_path, "private-project", "owner/private-project")
    runner = FakeGitRunner([fixture])
    runner.raise_timeout = True

    with pytest.raises(ProjectDiscoveryError, match="timed out") as caught:
        _discover(tmp_path, (tmp_path,), [fixture], runner=runner)
    assert "private-project" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None

    moments = iter([0.0, 6.0])
    zero_call_runner = FakeGitRunner([fixture])
    with pytest.raises(ProjectDiscoveryError, match="timed out"):
        _discover(
            tmp_path,
            (tmp_path,),
            [fixture],
            runner=zero_call_runner,
            monotonic=lambda: next(moments),
        )
    assert zero_call_runner.commands == []


def test_injected_reparse_escape_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path.parent / f"outside-{uuid4()}"
    outside.mkdir()
    escape = tmp_path / "escape"
    escape.mkdir()
    import forge.ops.project_discovery as module

    real_is_reparse = module._is_reparse
    monkeypatch.setattr(
        module,
        "_is_reparse",
        lambda path: path == escape or real_is_reparse(path),
    )
    try:
        with pytest.raises(ProjectDiscoveryError, match="path boundary"):
            _discover(tmp_path, (tmp_path,), [])
    finally:
        escape.rmdir()
        outside.rmdir()


def test_duplicate_and_nested_allowed_roots_are_scanned_once(tmp_path: Path) -> None:
    fixture = _make_repo(tmp_path, "nested", "owner/repo")
    runner = FakeGitRunner([fixture])

    projects = _discover(
        tmp_path,
        (tmp_path, tmp_path / "." / "nested", tmp_path / "nested" / ".."),
        [fixture],
        runner=runner,
    )

    assert [project.repository for project in projects] == ["owner/repo"]
    common_dir_calls = [
        timeout
        for timeout, environment in zip(runner.timeouts, runner.environments)
        if environment["GIT_TERMINAL_PROMPT"] == "0"
    ]
    assert common_dir_calls


def test_disjoint_roots_remain_supported(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    sibling = tmp_path.parent / f"sibling-{uuid4()}"
    sibling.mkdir()
    try:
        assert _discover(tmp_path, (tmp_path, sibling), []) == ()
    finally:
        sibling.rmdir()


def test_resolution_change_after_probe_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _make_repo(tmp_path, "repo", "owner/repo")
    import forge.ops.project_discovery as module

    real_resolve = module._resolve_path
    calls = 0

    def changing_resolve(path: Path) -> Path:
        nonlocal calls
        calls += 1
        resolved = real_resolve(path)
        if path == fixture.root and calls > 4:
            return tmp_path.parent.resolve()
        return resolved

    monkeypatch.setattr(module, "_resolve_path", changing_resolve)
    with pytest.raises(ProjectDiscoveryError, match="path boundary"):
        _discover(tmp_path, (tmp_path,), [fixture])


def test_reparse_point_appearing_after_git_and_github_probes_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _make_repo(tmp_path, "repo", "owner/repo")
    inner = FakeGitRunner([fixture])
    state = {"verified": False}

    def changing_runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        result = inner(command, **kwargs)
        if "--verify" in command:
            state["verified"] = True
        return result

    import forge.ops.project_discovery as module

    real_is_reparse = module._is_reparse
    monkeypatch.setattr(
        module,
        "_is_reparse",
        lambda path: (
            state["verified"] and path == fixture.root.resolve()
        )
        or real_is_reparse(path),
    )

    with pytest.raises(ProjectDiscoveryError, match="reparse"):
        _discover(tmp_path, (tmp_path,), [fixture], runner=changing_runner)


def test_git_common_dir_outside_allowed_roots_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"private-common-{uuid4()}"
    outside.mkdir()
    fixture = _make_repo(
        tmp_path,
        "linked",
        "owner/repo",
        common_dir=outside,
    )
    try:
        with pytest.raises(ProjectDiscoveryError, match="Git metadata") as caught:
            _discover(tmp_path, (tmp_path,), [fixture])
        assert "private-common" not in str(caught.value)
    finally:
        outside.rmdir()


def test_linked_worktrees_with_same_common_dir_are_rejected(tmp_path: Path) -> None:
    shared = tmp_path / "shared-git"
    shared.mkdir()
    first = _make_repo(tmp_path, "worktree-a", "owner/repo-a", common_dir=shared)
    second = _make_repo(tmp_path, "worktree-b", "owner/repo-b", common_dir=shared)

    with pytest.raises(ProjectDiscoveryError, match="duplicate"):
        _discover(tmp_path, (tmp_path,), [first, second])


def test_separate_clones_of_same_repository_are_rejected(tmp_path: Path) -> None:
    first = _make_repo(tmp_path, "clone-a", "owner/same")
    second = _make_repo(tmp_path, "clone-b", "owner/same", remote_form="scp")

    with pytest.raises(ProjectDiscoveryError, match="duplicate"):
        _discover(tmp_path, (tmp_path,), [first, second])


@pytest.mark.parametrize(
    "fixture_changes",
    [
        {"remote_names": ()},
        {"fetch_urls": (
            "https://github.com/owner/repo.git",
            "git@github.com:owner/repo.git",
        )},
        {"push_urls": (
            "https://github.com/owner/repo.git",
            "git@github.com:owner/repo.git",
        )},
        {"push_remote": "git@github.com:owner/other.git"},
        {"remote": "https://secret@github.com/owner/repo.git"},
    ],
)
def test_remote_missing_multiple_credential_and_fetch_push_mismatch_are_rejected(
    tmp_path: Path,
    fixture_changes: dict[str, object],
) -> None:
    fixture = _make_repo(tmp_path, "repo", "owner/repo")
    fixture = RepoFixture(**{**fixture.__dict__, **fixture_changes})

    with pytest.raises(ProjectDiscoveryError, match="remote") as caught:
        _discover(tmp_path, (tmp_path,), [fixture])
    assert "secret" not in str(caught.value)


def test_multiple_valid_remotes_become_deterministic_candidates(tmp_path: Path) -> None:
    fixture = _make_repo(tmp_path, "repo", "owner/one")
    fixture = RepoFixture(
        **{
            **fixture.__dict__,
            "remote_urls": {
                "upstream": (
                    ("git@github.com:Owner/two.git",),
                    ("https://github.com/Owner/two.git",),
                ),
                "origin": ((fixture.remote,), (fixture.remote,)),
            },
        }
    )
    reader = FakeGitHubReader([fixture])
    reader.overrides["Owner/two"] = GitHubRepositoryMetadata(
        full_name="Owner/two",
        default_branch=fixture.branch,
        branch=fixture.branch,
        commit_sha=fixture.commit,
    )

    projects = _discover(
        tmp_path,
        (tmp_path,),
        [fixture],
        github_metadata_reader=reader,
    )

    assert [(project.remote_name, project.repository) for project in projects] == [
        ("origin", "owner/one"),
        ("upstream", "Owner/two"),
    ]


def test_multiple_remote_aliases_for_same_repository_remain_distinct_choices(
    tmp_path: Path,
) -> None:
    fixture = _make_repo(tmp_path, "repo", "owner/repo")
    fixture = RepoFixture(
        **{
            **fixture.__dict__,
            "remote_urls": {
                "origin": ((fixture.remote,), (fixture.remote,)),
                "upstream": (
                    ("git@github.com:OWNER/REPO.git",),
                    ("ssh://git@github.com/OWNER/REPO.git",),
                ),
            },
        }
    )
    reader = FakeGitHubReader([fixture])
    reader.overrides["OWNER/REPO"] = GitHubRepositoryMetadata(
        full_name="owner/repo",
        default_branch=fixture.branch,
        branch=fixture.branch,
        commit_sha=fixture.commit,
    )

    projects = _discover(
        tmp_path,
        (tmp_path,),
        [fixture],
        github_metadata_reader=reader,
    )

    assert [
        (project.remote_name, project.repository) for project in projects
    ] == [
        ("origin", "owner/repo"),
        ("upstream", "owner/repo"),
    ]
    assert len({project.project_id for project in projects}) == 2


def test_fetch_and_push_casing_forms_share_api_canonical_repository(
    tmp_path: Path,
) -> None:
    fixture = _make_repo(tmp_path, "repo", "owner/repo")
    fixture = RepoFixture(
        **{
            **fixture.__dict__,
            "fetch_urls": ("https://github.com/owner/repo.git",),
            "push_urls": ("git@github.com:OWNER/REPO.git",),
        }
    )
    reader = FakeGitHubReader([fixture])
    reader.overrides[fixture.repository] = GitHubRepositoryMetadata(
        full_name="Owner/Repo",
        default_branch=fixture.branch,
        branch=fixture.branch,
        commit_sha=fixture.commit,
    )

    projects = _discover(
        tmp_path,
        (tmp_path,),
        [fixture],
        github_metadata_reader=reader,
    )
    assert projects[0].repository == "Owner/Repo"


def test_github_casing_is_canonicalized_but_redirect_is_rejected(tmp_path: Path) -> None:
    fixture = _make_repo(tmp_path, "repo", "owner/repo")
    reader = FakeGitHubReader([fixture])
    reader.overrides[fixture.repository] = GitHubRepositoryMetadata(
        full_name="Owner/Repo",
        default_branch=fixture.branch,
        branch=fixture.branch,
        commit_sha=fixture.commit,
    )

    projects = _discover(
        tmp_path,
        (tmp_path,),
        [fixture],
        github_metadata_reader=reader,
    )
    assert projects[0].repository == "Owner/Repo"

    reader.overrides[fixture.repository] = GitHubRepositoryMetadata(
        full_name="Owner/Renamed",
        default_branch=fixture.branch,
        branch=fixture.branch,
        commit_sha=fixture.commit,
    )
    with pytest.raises(ProjectDiscoveryError, match="GitHub binding"):
        _discover(
            tmp_path,
            (tmp_path,),
            [fixture],
            github_metadata_reader=reader,
        )


def test_invalid_github_default_branch_never_reaches_git_argv(tmp_path: Path) -> None:
    fixture = _make_repo(tmp_path, "repo", "owner/repo")
    runner = FakeGitRunner([fixture])

    with pytest.raises(ProjectDiscoveryError, match="GitHub metadata"):
        _discover(
            tmp_path,
            (tmp_path,),
            [fixture],
            runner=runner,
            github_metadata_reader=lambda repository, branch, timeout: {
                "full_name": repository,
                "default_branch": "main\n--upload-pack=evil",
                "branch": "main\n--upload-pack=evil",
                "commit_sha": fixture.commit,
            },
        )
    assert not any(command[-2:-1] == ["--verify"] for command in runner.commands)


def test_github_reader_exception_does_not_retain_secret_context(tmp_path: Path) -> None:
    fixture = _make_repo(tmp_path, "repo", "owner/repo")

    def failing_reader(
        repository: str,
        branch: str | None,
        timeout: float,
    ) -> GitHubRepositoryMetadata:
        raise RuntimeError("private-api-token")

    with pytest.raises(ProjectDiscoveryError, match="GitHub metadata") as caught:
        _discover(
            tmp_path,
            (tmp_path,),
            [fixture],
            github_metadata_reader=failing_reader,
        )
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    rendered = "".join(
        traceback.format_exception(caught.type, caught.value, caught.tb)
    )
    assert "private-api-token" not in rendered


def test_git_runner_uses_argv_without_shell_and_sanitizes_secret_traceback(
    tmp_path: Path,
) -> None:
    fixture = _make_repo(tmp_path, "repo", "owner/repo")
    fixture = RepoFixture(
        **{
            **fixture.__dict__,
            "remote": "https://secret-token@github.com/owner/repo.git",
        }
    )
    runner = FakeGitRunner([fixture])

    with pytest.raises(ProjectDiscoveryError) as caught:
        _discover(tmp_path, (tmp_path,), [fixture], runner=runner)
    rendered = "".join(traceback.format_exception(caught.value))
    assert "secret-token" not in str(caught.value)
    assert "secret-token" not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert all(
        error.__cause__ is None and error.__context__ is None
        for error in _exception_graph(caught.value)
    )
    assert not any(
        "secret-token" in text
        for text in _library_traceback_local_text(caught.value)
    )
    assert all(isinstance(command, list) for command in runner.commands)
    assert all("shell" not in kwargs for kwargs in runner.keyword_arguments)


def test_git_runner_requests_strict_utf8_decoding(tmp_path: Path) -> None:
    fixture = _make_repo(tmp_path, "repo", "owner/repo")
    runner = FakeGitRunner([fixture])

    _discover(tmp_path, (tmp_path,), [fixture], runner=runner)

    assert runner.keyword_arguments
    assert all(
        kwargs["encoding"] == "utf-8" and kwargs["errors"] == "strict"
        for kwargs in runner.keyword_arguments
    )


def test_git_runner_none_result_is_a_controlled_invalid_result(
    tmp_path: Path,
) -> None:
    fixture = _make_repo(tmp_path, "repo", "owner/repo")

    def none_runner(command: list[str], **kwargs: Any) -> None:
        return None

    with pytest.raises(ProjectDiscoveryError, match="invalid result") as caught:
        _discover(tmp_path, (tmp_path,), [fixture], runner=none_runner)

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.skipif(shutil.which("git") is None, reason="Git executable is required")
def test_actual_git_discovers_workspace_with_korean_path(tmp_path: Path) -> None:
    workspace = tmp_path / "한글 저장소"
    workspace.mkdir()
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Forge Test",
        "GIT_AUTHOR_EMAIL": "forge@example.invalid",
        "GIT_COMMITTER_NAME": "Forge Test",
        "GIT_COMMITTER_EMAIL": "forge@example.invalid",
    }

    def run_git(*arguments: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", "-C", str(workspace), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            env=environment,
        )

    run_git("init", "--initial-branch=main")
    (workspace / "README.md").write_text("테스트\n", encoding="utf-8")
    run_git("add", "README.md")
    run_git("commit", "-m", "initial")
    run_git("remote", "add", "origin", "https://github.com/owner/korean-path.git")
    run_git("update-ref", "refs/remotes/origin/main", "HEAD")
    commit = run_git("rev-parse", "HEAD").stdout.decode("ascii").strip()
    fixture = RepoFixture(
        root=workspace,
        repository="owner/korean-path",
        remote="https://github.com/owner/korean-path.git",
        commit=commit,
    )

    projects = discover_projects(
        workspace,
        (tmp_path,),
        host_id=str(uuid4()),
        github_metadata_reader=FakeGitHubReader([fixture]),
    )

    assert [project.workspace for project in projects] == [str(workspace.resolve())]


def test_git_runner_removes_environment_overrides_that_can_escape_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _make_repo(tmp_path, "repo", "owner/repo")
    runner = FakeGitRunner([fixture])
    monkeypatch.setenv("GIT_DIR", "C:/private-git-dir")
    monkeypatch.setenv("GIT_WORK_TREE", "C:/private-worktree")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "remote.origin.url")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://secret@github.com/owner/repo")
    monkeypatch.setenv("GIT_SSH_COMMAND", "private-command")

    _discover(tmp_path, (tmp_path,), [fixture], runner=runner)

    forbidden = {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_SSH_COMMAND",
    }
    assert all(forbidden.isdisjoint(environment) for environment in runner.environments)
    assert all(
        environment["GIT_CONFIG_GLOBAL"] == os.devnull
        and environment["GIT_CONFIG_SYSTEM"] == os.devnull
        for environment in runner.environments
    )


def test_resolved_allowed_root_alias_marked_reparse_is_rejected_before_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alias = tmp_path / "alias"
    target = tmp_path / "target"
    alias.mkdir()
    target.mkdir()
    runner = FakeGitRunner([])
    import forge.ops.project_discovery as module

    real_resolve = module._resolve_path
    real_is_reparse = module._is_reparse
    monkeypatch.setattr(
        module,
        "_resolve_path",
        lambda path: target.resolve() if path == alias else real_resolve(path),
    )
    monkeypatch.setattr(
        module,
        "_is_reparse",
        lambda path: path == alias or real_is_reparse(path),
    )

    with pytest.raises(ProjectDiscoveryError, match="reparse"):
        _discover(alias, (alias,), [], runner=runner)
    assert runner.commands == []


def test_failed_working_git_probe_with_ancestor_marker_is_not_silently_skipped(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "one" / "two" / "three"
    fixture = _make_repo(parent, "four", "owner/repo")
    nested = fixture.root / "src"
    nested.mkdir()
    inner = FakeGitRunner([fixture])

    def failing_runner(
        command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        if command[-2:] == ["rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(
                command,
                128,
                "",
                "permission denied for private path",
            )
        return inner(command, **kwargs)

    with pytest.raises(ProjectDiscoveryError, match="Git probe") as caught:
        _discover(
            nested,
            (tmp_path,),
            [fixture],
            runner=failing_runner,
            limits=DiscoveryLimits(max_depth=3),
        )
    assert "private path" not in str(caught.value)


@pytest.mark.parametrize(
    ("field_name", "stdout"),
    [
        ("common_dir_stdout", ".git\n"),
        ("common_dir_stdout", "C:/missing-common-dir\n"),
        ("common_dir_stdout", "C:/one\nC:/two\n"),
        ("git_dir_stdout", ".git\n"),
        ("git_dir_stdout", "C:/missing-git-dir\n"),
        ("git_dir_stdout", "C:/one\nC:/two\n"),
    ],
)
def test_relative_nonexistent_and_multiline_git_metadata_are_rejected(
    tmp_path: Path,
    field_name: str,
    stdout: str,
) -> None:
    fixture = _make_repo(tmp_path, "repo", "owner/repo")
    fixture = RepoFixture(**{**fixture.__dict__, field_name: stdout})

    with pytest.raises(ProjectDiscoveryError, match="Git metadata"):
        _discover(tmp_path, (tmp_path,), [fixture])


@pytest.mark.parametrize("mismatch", ["repository", "branch", "commit"])
def test_github_repository_branch_and_commit_mismatch_are_rejected(
    tmp_path: Path,
    mismatch: str,
) -> None:
    fixture = _make_repo(tmp_path, "repo", "owner/repo")
    reader = FakeGitHubReader([fixture])
    values = {
        "full_name": fixture.repository,
        "default_branch": fixture.branch,
        "branch": fixture.branch,
        "commit_sha": fixture.commit,
    }
    if mismatch == "repository":
        values["full_name"] = "owner/different"
    elif mismatch == "branch":
        values["branch"] = "different"
    else:
        values["commit_sha"] = "b" * 40
    reader.overrides[fixture.repository] = GitHubRepositoryMetadata(**values)

    with pytest.raises(ProjectDiscoveryError, match="GitHub binding"):
        _discover(
            tmp_path,
            (tmp_path,),
            [fixture],
            github_metadata_reader=reader,
        )


def test_validator_accepts_verified_explicit_non_default_branch(tmp_path: Path) -> None:
    fixture = _make_repo(
        tmp_path,
        "repo",
        "owner/repo",
        branch="release/stable",
        commit="c" * 40,
    )
    project = TaskProject.create(
        repository=fixture.repository,
        workspace=str(fixture.root.resolve()),
        remote_name=fixture.remote_name,
        base_branch=fixture.branch,
        base_commit=fixture.commit,
        host_id=str(uuid4()),
    )
    reader = FakeGitHubReader([fixture])
    reader.overrides[fixture.repository] = GitHubRepositoryMetadata(
        full_name=fixture.repository,
        default_branch="main",
        branch=fixture.branch,
        commit_sha=fixture.commit,
    )

    validated = validate_task_project(
        project,
        allowed_roots=(tmp_path,),
        runner=FakeGitRunner([fixture]),
        github_metadata_reader=reader,
    )

    assert validated is project
    assert reader.calls[0][1] == "release/stable"


def test_validator_accepts_local_remote_casing_bound_to_api_canonical_name(
    tmp_path: Path,
) -> None:
    fixture = _make_repo(tmp_path, "repo", "owner/repo")
    project = TaskProject.create(
        repository="Owner/Repo",
        workspace=str(fixture.root.resolve()),
        remote_name=fixture.remote_name,
        base_branch=fixture.branch,
        base_commit=fixture.commit,
        host_id=str(uuid4()),
    )
    reader = FakeGitHubReader([fixture])
    reader.overrides[fixture.repository] = GitHubRepositoryMetadata(
        full_name=project.repository,
        default_branch=fixture.branch,
        branch=fixture.branch,
        commit_sha=fixture.commit,
    )

    assert validate_task_project(
        project,
        allowed_roots=(tmp_path,),
        runner=FakeGitRunner([fixture]),
        github_metadata_reader=reader,
    ) is project


def test_branch_reference_is_one_argv_element_and_shell_is_never_enabled(
    tmp_path: Path,
) -> None:
    fixture = _make_repo(
        tmp_path,
        "repo",
        "owner/repo",
        branch="release;echo-token",
    )
    runner = FakeGitRunner([fixture])

    _discover(tmp_path, (tmp_path,), [fixture], runner=runner)

    verify_commands = [command for command in runner.commands if "--verify" in command]
    assert [command[-1] for command in verify_commands] == [
        "refs/remotes/origin/release;echo-token^{commit}"
    ]
    assert all("shell" not in kwargs for kwargs in runner.keyword_arguments)


def test_validator_rejects_local_remote_tracking_commit_mismatch(tmp_path: Path) -> None:
    fixture = _make_repo(tmp_path, "repo", "owner/repo", commit="a" * 40)
    project = TaskProject.create(
        repository=fixture.repository,
        workspace=str(fixture.root.resolve()),
        remote_name=fixture.remote_name,
        base_branch=fixture.branch,
        base_commit="b" * 40,
        host_id=str(uuid4()),
    )

    with pytest.raises(ProjectDiscoveryError, match="local Git binding"):
        validate_task_project(
            project,
            allowed_roots=(tmp_path,),
            runner=FakeGitRunner([fixture]),
            github_metadata_reader=FakeGitHubReader([fixture]),
        )


def test_validator_checks_deadline_again_after_final_containment_probe(
    tmp_path: Path,
) -> None:
    fixture = _make_repo(tmp_path, "repo", "owner/repo")
    project = TaskProject.create(
        repository=fixture.repository,
        workspace=str(fixture.root.resolve()),
        remote_name=fixture.remote_name,
        base_branch=fixture.branch,
        base_commit=fixture.commit,
        host_id=str(uuid4()),
    )
    inner = FakeGitRunner([fixture])
    state = {"verified": False, "post_verify_checked": False}

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        result = inner(command, **kwargs)
        if "--verify" in command:
            state["verified"] = True
        return result

    def monotonic() -> float:
        if not state["verified"]:
            return 0.0
        if not state["post_verify_checked"]:
            state["post_verify_checked"] = True
            return 0.0
        return 6.0

    with pytest.raises(ProjectDiscoveryError, match="timed out"):
        validate_task_project(
            project,
            allowed_roots=(tmp_path,),
            runner=runner,
            github_metadata_reader=FakeGitHubReader([fixture]),
            monotonic=monotonic,
        )


def test_permission_ambiguity_returns_no_partial_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _make_repo(tmp_path, "visible", "owner/visible")
    real_scandir = os.scandir

    def denied(path: object) -> Any:
        if Path(path) == tmp_path.resolve():
            raise PermissionError("private-directory")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", denied)
    with pytest.raises(ProjectDiscoveryError, match="permission") as caught:
        _discover(tmp_path, (tmp_path,), [fixture])
    assert "private-directory" not in str(caught.value)
