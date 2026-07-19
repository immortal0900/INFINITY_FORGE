from __future__ import annotations

import json
import runpy
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from forge.ops.github import (
    GitHubClient,
    GitHubTaskIssueClientV2,
    PullRequestWriteState,
    TaskStopIssueState,
)
from forge.ops.kanban_stop import KanbanStopResult
from forge.ops.process_identity import (
    PosixProcessBackend,
    ProcessBinding,
    ProcessIdentity,
    ProcessIdentityError,
    ProcessMemberIdentity,
    ProcessScopeKind,
)
from forge.ops.surface_events import SurfaceEventStore, TrustedTurnContext
from forge.ops.task_database import TaskDatabase
from forge.ops.task_options import MergeMode, TaskFlow
from forge.ops.task_projects import TaskProject
from forge.ops.task_settings import TaskContent
from forge.ops.task_settings_v2 import TaskRequestV2, TaskSettingsV2
from forge.ops.task_stop import (
    ProcessTreeStopper,
    StopReconcileReceipt,
    TaskStopOwnerHostMismatch,
    TaskStopReconciler,
    TaskStopService,
)


REQUEST_ID = "9f7453ce-36ec-4e8e-9dfa-bb159b58c19b"
STOP_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OWNER_HOST = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
NOW = datetime(2026, 7, 19, 3, 0, tzinfo=UTC)
NOW_TEXT = "2026-07-19T03:00:00Z"
V2_BODY = (
    "Task body\n\n"
    "<!-- forge-v2-progress:start -->\n"
    "progress\n"
    "<!-- forge-v2-progress:end -->\n\n"
    "<!-- forge-v2-task-request\n"
    + json.dumps(
        {
            "format_version": "forge-task-request/v2",
            "request_id": REQUEST_ID,
            "request_hash": "c" * 64,
            "task_content_hash": "a" * 64,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    + "\n-->"
)


def _issue(
    *,
    state: str = "open",
    state_reason: str | None = None,
    labels: tuple[str, ...] = ("bug", "forge:building"),
) -> dict[str, object]:
    return {
        "number": 21,
        "title": "Task title",
        "body": V2_BODY,
        "state": state,
        "state_reason": state_reason,
        "labels": [{"name": label} for label in labels],
    }


class Runner:
    def __init__(self, outputs: list[object]) -> None:
        self.outputs = outputs
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        output = self.outputs.pop(0)
        if isinstance(output, tuple):
            returncode, text = output
        else:
            returncode, text = 0, json.dumps(output)
        return subprocess.CompletedProcess(argv, returncode, text, "failure")


def test_stop_issue_client_removes_only_forge_status_and_preserves_other_labels() -> (
    None
):
    runner = Runner(
        [
            _issue(labels=("bug", "forge:building", "urgent")),
            (0, ""),
            _issue(labels=("bug", "urgent")),
        ]
    )
    client = GitHubTaskIssueClientV2("gh", runner=runner)

    state = client.reconcile_stop_status("owner/forge", 21, target=None)

    assert state.labels == ("bug", "urgent")
    assert any("labels/forge%3Abuilding" in argument for argument in runner.calls[1])
    assert not any(
        "bug" in argument or "urgent" in argument for argument in runner.calls[1]
    )


def test_stop_issue_client_adds_only_needs_decision_and_closes_not_planned() -> None:
    runner = Runner(
        [
            _issue(labels=("bug", "forge:reviewing")),
            {},
            {},
            _issue(labels=("bug", "forge:needs-decision")),
            _issue(labels=("bug", "forge:needs-decision")),
            _issue(
                state="closed",
                state_reason="not_planned",
                labels=("bug", "forge:needs-decision"),
            ),
            _issue(
                state="closed",
                state_reason="not_planned",
                labels=("bug", "forge:needs-decision"),
            ),
        ]
    )
    client = GitHubTaskIssueClientV2("gh", runner=runner)

    status = client.reconcile_stop_status(
        "owner/forge",
        21,
        target="forge:needs-decision",
    )
    closed = client.close_stop_issue_not_planned("owner/forge", 21)

    assert status.labels == ("bug", "forge:needs-decision")
    assert closed.state == "closed"
    assert closed.state_reason == "not_planned"
    assert any("state_reason=not_planned" in argument for argument in runner.calls[5])


def test_stop_comment_is_marker_idempotent_after_a_lost_write_response() -> None:
    body = f"Task Stop result\n\n<!-- forge-task-stop:{STOP_ID} -->"
    comment = {"id": 7, "body": body}
    runner = Runner([[[comment]], [[comment]]])
    client = GitHubTaskIssueClientV2("gh", runner=runner)

    first = client.ensure_stop_comment("owner/forge", 21, STOP_ID, body)
    replay = client.ensure_stop_comment("owner/forge", 21, STOP_ID, body)

    assert first == replay == body
    assert all("--paginate" in call and "--slurp" in call for call in runner.calls)
    assert all("POST" not in call for call in runner.calls)


def test_stop_comment_updates_only_its_exact_marker_after_outcome_changes() -> None:
    marker = f"<!-- forge-task-stop:{STOP_ID} -->"
    old_body = f"Task Stop result: cancelled\n\n{marker}"
    new_body = f"Task Stop result: completed_before_stop\n\n{marker}"
    unrelated = {"id": 8, "body": "human note"}
    runner = Runner(
        [
            [[unrelated, {"id": 7, "body": old_body}]],
            {},
            [[unrelated, {"id": 7, "body": new_body}]],
        ]
    )

    result = GitHubTaskIssueClientV2("gh", runner=runner).ensure_stop_comment(
        "owner/forge",
        21,
        STOP_ID,
        new_body,
    )

    assert result == new_body
    patch_calls = [call for call in runner.calls if "PATCH" in call]
    assert len(patch_calls) == 1
    assert "repos/owner/forge/issues/comments/7" in patch_calls[0]
    assert all("comments/8" not in argument for call in runner.calls for argument in call)


def test_pr_reader_recovers_exact_branch_after_ambiguous_create() -> None:
    url = "https://github.com/owner/project/pull/17"
    runner = Runner(
        [
            [
                [
                    {
                        "number": 17,
                        "html_url": url,
                        "head": {
                            "ref": "forge/task/one",
                            "repo": {"full_name": "owner/project"},
                        },
                    }
                ]
            ],
            {
                "number": 17,
                "html_url": url,
                "state": "open",
                "merged": False,
                "merge_commit_sha": None,
                "head": {"sha": "d" * 40},
                "base": {"sha": "a" * 40, "ref": "main"},
            },
        ]
    )

    state = GitHubClient("gh", runner=runner).find_pr_write_state(
        "owner/project",
        "forge/task/one",
    )

    assert state is not None and state.pr_url == url
    assert "--paginate" in runner.calls[0]
    assert "--slurp" in runner.calls[0]
    assert "head=owner:forge%2Ftask%2Fone" in runner.calls[0][-1]


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _task_body(request: TaskRequestV2) -> str:
    raw = json.loads(request.to_json())
    marker = {
        "format_version": request.format_version,
        "request_hash": request.request_hash,
        "request_id": request.request_id,
        "task_content_hash": raw["task_content_hash"],
    }
    return f"Task body\n\n<!-- forge-v2-task-request\n{_canonical(marker)}\n-->"


def _seed_stop(
    tmp_path: Path,
    *,
    project_count: int = 1,
    prepared: bool = False,
) -> tuple[TaskDatabase, TaskRequestV2, str]:
    projects: list[TaskProject] = []
    for index in range(project_count):
        workspace = (tmp_path / f"project-{index}").resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        projects.append(
            TaskProject.create(
                repository=f"owner/project-{index}",
                workspace=str(workspace),
                remote_name="origin",
                base_branch="main",
                base_commit=chr(ord("a") + index) * 40,
                host_id=OWNER_HOST,
            )
        )
    request = TaskRequestV2.create(
        request_id=str(uuid4()),
        management_repository="owner/forge",
        task_content=TaskContent(
            title="Stop saga",
            description="Verify exact cleanup.",
            acceptance_criteria=("Remote truth is preserved.",),
        ),
        task_flow=TaskFlow.BUILD_REVIEW,
        merge_mode=MergeMode.FULL_AUTO if project_count > 1 else MergeMode.SAFE_AUTO,
        merge_order=tuple(project.project_id for project in projects)
        if project_count > 1
        else None,
        projects=tuple(projects),
        task_owner_host=OWNER_HOST,
        confirmed_by="user-1",
        confirmed_at=NOW,
    )
    settings = (
        None
        if prepared
        else TaskSettingsV2.create(
            request=request,
            parent_issue_number=21,
        )
    )
    database = TaskDatabase(tmp_path / "tasks.sqlite3")
    raw_request = json.loads(request.to_json())
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO task_requests (
                request_id, format_version, request_json, request_hash,
                management_repository, task_owner_host, confirmed_by,
                confirmed_at, replaces_request_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                request.request_id,
                request.format_version,
                request.to_json(),
                request.request_hash,
                request.management_repository,
                request.task_owner_host,
                request.confirmed_by,
                raw_request["confirmed_at"],
            ),
        )
        connection.execute(
            """
            INSERT INTO task_events (
                request_id, task_settings_hash, project_id, event_type,
                event_key, event_json, occurred_at
            ) VALUES (?, NULL, NULL, 'request_prepared', 'request_prepared', ?, ?)
            """,
            (
                request.request_id,
                _canonical({"request_hash": request.request_hash}),
                NOW_TEXT,
            ),
        )
        if settings is not None:
            raw_settings = json.loads(settings.to_json())
            connection.execute(
                """
                INSERT INTO task_settings_v2 (
                    task_settings_hash, request_id, request_hash, format_version,
                    settings_json, management_repository, parent_issue_number,
                    task_owner_host, confirmed_at
                ) VALUES (?, ?, ?, ?, ?, ?, 21, ?, ?)
                """,
                (
                    settings.task_settings_hash,
                    request.request_id,
                    request.request_hash,
                    settings.format_version,
                    settings.to_json(),
                    settings.management_repository,
                    settings.task_owner_host,
                    raw_settings["confirmed_at"],
                ),
            )
            connection.execute(
                """
                INSERT INTO task_events (
                    request_id, task_settings_hash, project_id, event_type,
                    event_key, event_json, occurred_at
                ) VALUES (?, NULL, NULL, 'parent_issue_bound',
                          'parent_issue_bound', ?, ?)
                """,
                (
                    request.request_id,
                    _canonical(
                        {
                            "parent_issue_number": 21,
                            "request_hash": request.request_hash,
                        }
                    ),
                    NOW_TEXT,
                ),
            )
            for event_type in ("settings_activated", "active"):
                connection.execute(
                    """
                    INSERT INTO task_events (
                        request_id, task_settings_hash, project_id, event_type,
                        event_key, event_json, occurred_at
                    ) VALUES (?, ?, NULL, ?, ?, ?, ?)
                    """,
                    (
                        request.request_id,
                        settings.task_settings_hash,
                        event_type,
                        event_type,
                        _canonical({"task_settings_hash": settings.task_settings_hash}),
                        NOW_TEXT,
                    ),
                )
        for project, raw_project in zip(projects, raw_request["projects"], strict=True):
            connection.execute(
                """
                INSERT INTO task_projects (
                    request_id, project_id, task_settings_hash, project_json,
                    state, root_card_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.request_id,
                    project.project_id,
                    None if settings is None else settings.task_settings_hash,
                    _canonical(raw_project),
                    "prepared" if settings is None else "ready",
                    None if settings is None else f"root-{project.project_id}",
                    NOW_TEXT,
                ),
            )
        connection.execute(
            """
            INSERT INTO task_access (
                request_id, surface, subject_id, role, granted_by,
                granted_at, revoked_at
            ) VALUES (?, 'cli', 'user-1', 'owner', 'user-1', ?, NULL)
            """,
            (request.request_id, NOW_TEXT),
        )
    context = TrustedTurnContext(
        owner_host=OWNER_HOST,
        subject_id="user-1",
        session_id="session-1",
        surface="cli",
        source_event_id=f"cli:stop:{request.request_id}",
        working_directory=None,
    )
    source = SurfaceEventStore(database, clock=lambda: NOW).receive(
        context,
        "forge stop",
        at=NOW,
    )
    receipt = TaskStopService(database).request_stop(
        request.request_id,
        context,
        payload_hash=source.payload_hash,
        at=NOW,
    )
    return database, request, receipt.stop_request_id


class FakeCards:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def archive(self, authority):
        self.calls.append(authority.stop_request_id)
        return KanbanStopResult(
            request_id=authority.request_id,
            archived_card_ids=("root",),
            preserved_card_ids=(),
            captured_runs=(),
            all_cards_terminal=True,
        )


class FakeProcesses:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def stop(self, identity, *, current_host: str):
        self.calls.append(identity.to_json())
        raise AssertionError("no process identity was expected")


class FakePullRequests:
    def __init__(self) -> None:
        self.states: dict[str, PullRequestWriteState] = {}
        self.recovered: dict[tuple[str, str], PullRequestWriteState | None] = {}

    def get_pr_write_state(self, pr_url: str) -> PullRequestWriteState:
        return self.states[pr_url]

    def find_pr_write_state(self, repository: str, branch_name: str):
        return self.recovered.get((repository, branch_name))


class SnapshotPullRequests(FakePullRequests):
    def __init__(self) -> None:
        super().__init__()
        self.snapshots: list[dict[str, PullRequestWriteState]] = []
        self.read_count = 0

    def get_pr_write_state(self, pr_url: str) -> PullRequestWriteState:
        if not self.snapshots:
            return super().get_pr_write_state(pr_url)
        width = len(self.snapshots[0])
        snapshot_index = min(self.read_count // width, len(self.snapshots) - 1)
        self.read_count += 1
        return self.snapshots[snapshot_index][pr_url]


class FakeIssues:
    def __init__(self, request: TaskRequestV2, *, exists: bool = True) -> None:
        self.issue = (
            TaskStopIssueState(
                number=21,
                title="Task title",
                body=_task_body(request),
                state="open",
                state_reason=None,
                labels=("bug", "forge:building"),
            )
            if exists
            else None
        )
        self.comments: dict[str, str] = {}
        self.fail_comment = False
        self.calls: list[str] = []

    def find_stop_issue(self, repository: str, request_id: str):
        self.calls.append("find")
        return self.issue

    def get_stop_issue(self, repository: str, issue_number: int):
        self.calls.append("get")
        assert self.issue is not None
        return self.issue

    def reconcile_stop_status(self, repository: str, issue_number: int, *, target):
        self.calls.append(f"status:{target}")
        assert self.issue is not None
        unrelated = tuple(
            label for label in self.issue.labels if not label.startswith("forge:")
        )
        self.issue = TaskStopIssueState(
            number=self.issue.number,
            title=self.issue.title,
            body=self.issue.body,
            state=self.issue.state,
            state_reason=self.issue.state_reason,
            labels=tuple(sorted((*unrelated, *((target,) if target else ())))),
        )
        return self.issue

    def ensure_stop_comment(
        self,
        repository: str,
        issue_number: int,
        stop_request_id: str,
        body: str,
    ) -> str:
        self.calls.append("comment")
        if self.fail_comment:
            raise RuntimeError("injected comment failure")
        previous = self.comments.setdefault(stop_request_id, body)
        assert previous == body
        return previous

    def close_stop_issue_not_planned(self, repository: str, issue_number: int):
        self.calls.append("close")
        assert self.issue is not None
        self.issue = TaskStopIssueState(
            number=self.issue.number,
            title=self.issue.title,
            body=self.issue.body,
            state="closed",
            state_reason="not_planned",
            labels=self.issue.labels,
        )
        return self.issue


def _set_pr(
    database: TaskDatabase,
    request: TaskRequestV2,
    project_index: int,
    *,
    merged: bool,
    reader: FakePullRequests,
) -> str:
    project = request.projects[project_index]
    url = f"https://github.com/{project.repository}/pull/{project_index + 1}"
    head = chr(ord("d") + project_index) * 40
    merge = chr(ord("f") - project_index) * 40 if merged else None
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE task_projects
            SET state = 'reviewing', branch_name = ?, worktree_path = ?,
                pr_url = ?, head_commit = ?, updated_at = ?
            WHERE request_id = ? AND project_id = ?
            """,
            (
                f"forge/{request.request_id}/{project_index}",
                project.workspace,
                url,
                head,
                NOW_TEXT,
                request.request_id,
                project.project_id,
            ),
        )
    reader.states[url] = PullRequestWriteState(
        pr_url=url,
        repository=project.repository,
        pr_number=project_index + 1,
        base_commit=project.base_commit,
        base_ref=project.base_branch,
        head_commit=head,
        is_open=not merged,
        is_merged=merged,
        merged_commit=merge,
        merged_base_commit=project.base_commit if merged else None,
        merged_head_commit=head if merged else None,
    )
    return url


def _reconciler(
    database: TaskDatabase,
    issues: FakeIssues,
    prs: FakePullRequests,
    cards: FakeCards,
) -> TaskStopReconciler:
    return TaskStopReconciler(
        database,
        issue_client=issues,
        pull_request_reader=prs,
        kanban_stopper=cards,
        process_stopper=FakeProcesses(),
        current_host=OWNER_HOST,
        clock=lambda: NOW,
    )


def test_reconcile_zero_merges_cancels_closes_and_never_replays_completed(
    tmp_path: Path,
) -> None:
    database, request, stop_id = _seed_stop(tmp_path)
    prs = FakePullRequests()
    _set_pr(database, request, 0, merged=False, reader=prs)
    issues = FakeIssues(request)
    cards = FakeCards()
    reconciler = _reconciler(database, issues, prs, cards)

    result = reconciler.reconcile(stop_id)
    replay = reconciler.reconcile(stop_id)

    assert result == replay
    assert result.state == "completed"
    assert result.result == "cancelled"
    assert cards.calls == [stop_id]
    assert issues.issue is not None
    assert issues.issue.state == "closed"
    assert issues.issue.state_reason == "not_planned"
    assert issues.issue.labels == ("bug",)
    assert len(issues.comments) == 1
    with database.read() as connection:
        assert (
            connection.execute(
                "SELECT event_type FROM task_events ORDER BY event_id DESC LIMIT 1"
            ).fetchone()[0]
            == "cancelled"
        )
        assert (
            connection.execute(
                "SELECT state FROM task_projects WHERE request_id = ?",
                (request.request_id,),
            ).fetchone()[0]
            == "cancelled"
        )


def test_reconcile_all_merges_records_actual_commits_without_closing_parent(
    tmp_path: Path,
) -> None:
    database, request, stop_id = _seed_stop(tmp_path)
    prs = FakePullRequests()
    url = _set_pr(database, request, 0, merged=True, reader=prs)
    issues = FakeIssues(request)
    cards = FakeCards()

    result = _reconciler(database, issues, prs, cards).reconcile(stop_id)

    assert result.result == "completed_before_stop"
    assert issues.issue is not None and issues.issue.state == "open"
    assert "close" not in issues.calls
    with database.read() as connection:
        project = connection.execute(
            "SELECT state, pr_url, merge_commit FROM task_projects WHERE request_id = ?",
            (request.request_id,),
        ).fetchone()
        assert tuple(project) == ("merged", url, prs.states[url].merged_commit)
        assert (
            connection.execute(
                "SELECT event_type FROM task_events ORDER BY event_id DESC LIMIT 1"
            ).fetchone()[0]
            == "merged"
        )


def test_reconcile_recovers_pr_created_before_a_lost_local_write(
    tmp_path: Path,
) -> None:
    database, request, stop_id = _seed_stop(tmp_path)
    project = request.projects[0]
    branch = f"forge/{request.request_id}/lost-response"
    url = f"https://github.com/{project.repository}/pull/17"
    head = "d" * 40
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE task_projects
            SET state = 'reviewing', branch_name = ?, worktree_path = ?,
                updated_at = ?
            WHERE request_id = ? AND project_id = ?
            """,
            (
                branch,
                project.workspace,
                NOW_TEXT,
                request.request_id,
                project.project_id,
            ),
        )
    prs = FakePullRequests()
    prs.recovered[(project.repository, branch)] = PullRequestWriteState(
        pr_url=url,
        repository=project.repository,
        pr_number=17,
        base_commit=project.base_commit,
        base_ref=project.base_branch,
        head_commit=head,
        is_open=True,
        is_merged=False,
        merged_commit=None,
        merged_base_commit=None,
        merged_head_commit=None,
    )
    issues = FakeIssues(request)

    result = _reconciler(database, issues, prs, FakeCards()).reconcile(stop_id)

    assert result.result == "cancelled"
    with database.read() as connection:
        assert tuple(
            connection.execute(
                "SELECT pr_url, head_commit FROM task_projects WHERE request_id = ?",
                (request.request_id,),
            ).fetchone()
        ) == (url, head)


def test_reconcile_partial_merge_keeps_parent_open_needs_decision(
    tmp_path: Path,
) -> None:
    database, request, stop_id = _seed_stop(tmp_path, project_count=2)
    prs = FakePullRequests()
    _set_pr(database, request, 0, merged=True, reader=prs)
    _set_pr(database, request, 1, merged=False, reader=prs)
    issues = FakeIssues(request)
    cards = FakeCards()

    result = _reconciler(database, issues, prs, cards).reconcile(stop_id)

    assert result.result == "completed_with_partial_merge"
    assert issues.issue is not None
    assert issues.issue.state == "open"
    assert "forge:needs-decision" in issues.issue.labels
    assert "close" not in issues.calls
    with database.read() as connection:
        assert (
            connection.execute(
                "SELECT event_type FROM task_events ORDER BY event_id DESC LIMIT 1"
            ).fetchone()[0]
            == "partially_merged"
        )


@pytest.mark.parametrize(
    ("project_count", "expected_result"),
    (
        (1, "completed_before_stop"),
        (2, "completed_with_partial_merge"),
    ),
)
def test_reconcile_stabilizes_zero_to_merged_before_any_outcome_write(
    tmp_path: Path,
    project_count: int,
    expected_result: str,
) -> None:
    database, request, stop_id = _seed_stop(tmp_path, project_count=project_count)
    prs = SnapshotPullRequests()
    urls = tuple(
        _set_pr(database, request, index, merged=False, reader=prs)
        for index in range(project_count)
    )
    before = dict(prs.states)
    after = dict(before)
    first = before[urls[0]]
    after[urls[0]] = replace(
        first,
        is_open=False,
        is_merged=True,
        merged_commit="f" * 40,
        merged_base_commit=first.base_commit,
        merged_head_commit=first.head_commit,
    )
    prs.snapshots = [before, after]
    issues = FakeIssues(request)

    result = _reconciler(database, issues, prs, FakeCards()).reconcile(stop_id)

    assert result.state == "completed"
    assert result.result == expected_result
    assert issues.issue is not None and issues.issue.state == "open"
    assert "close" not in issues.calls
    assert len(issues.comments) == 1
    assert expected_result in issues.comments[stop_id]


def test_cleanup_failure_is_the_only_incomplete_state_and_retry_converges(
    tmp_path: Path,
) -> None:
    database, request, stop_id = _seed_stop(tmp_path)
    prs = FakePullRequests()
    _set_pr(database, request, 0, merged=False, reader=prs)
    issues = FakeIssues(request)
    issues.fail_comment = True
    cards = FakeCards()
    reconciler = _reconciler(database, issues, prs, cards)

    failed = reconciler.reconcile(stop_id)
    issues.fail_comment = False
    completed = reconciler.reconcile(stop_id)

    assert failed.state == "cleanup_incomplete"
    assert failed.result is None
    assert completed.result == "cancelled"
    with database.read() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM task_events WHERE event_type = 'cancelled'"
            ).fetchone()[0]
            == 1
        )


def test_concurrent_reconcile_attempts_commit_one_terminal_result(
    tmp_path: Path,
) -> None:
    database, request, stop_id = _seed_stop(tmp_path)
    prs = FakePullRequests()
    _set_pr(database, request, 0, merged=False, reader=prs)
    issues = FakeIssues(request)
    reconciler = _reconciler(database, issues, prs, FakeCards())

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = tuple(executor.map(reconciler.reconcile, [stop_id] * 4))

    assert {result.state for result in results} == {"completed"}
    assert {result.result for result in results} == {"cancelled"}
    assert len(issues.comments) == 1
    with database.read() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM task_events WHERE event_type = 'cancelled'"
            ).fetchone()[0]
            == 1
        )


def test_late_worker_crossing_barrier_keeps_stop_cleanup_incomplete(
    tmp_path: Path,
) -> None:
    database, request, stop_id = _seed_stop(tmp_path)
    prs = FakePullRequests()
    _set_pr(database, request, 0, merged=False, reader=prs)

    class LateWorkerIssues(FakeIssues):
        def ensure_stop_comment(
            self,
            repository: str,
            issue_number: int,
            stop_request_id: str,
            body: str,
        ) -> str:
            value = super().ensure_stop_comment(
                repository,
                issue_number,
                stop_request_id,
                body,
            )
            with database.transaction() as connection:
                settings_hash = connection.execute(
                    "SELECT task_settings_hash FROM task_settings_v2 WHERE request_id = ?",
                    (request.request_id,),
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT OR IGNORE INTO task_runtime_runs (
                        run_id, request_id, task_settings_hash, project_id,
                        host_id, worker_task_id, runtime_name,
                        process_identity_json, message_packet_hash, state,
                        result_hash, started_at, ended_at
                    ) VALUES ('late-run', ?, ?, ?, ?, 'late-card', 'test', '{}',
                              ?, 'running', NULL, ?, NULL)
                    """,
                    (
                        request.request_id,
                        settings_hash,
                        request.projects[0].project_id,
                        OWNER_HOST,
                        "e" * 64,
                        NOW_TEXT,
                    ),
                )
            return value

    result = _reconciler(
        database,
        LateWorkerIssues(request),
        prs,
        FakeCards(),
    ).reconcile(stop_id)

    assert result.state == "cleanup_incomplete"
    with database.read() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM task_events WHERE event_type = 'cancelled'"
            ).fetchone()[0]
            == 0
        )


def test_prepared_request_without_remote_issue_cancels_without_local_worker_calls(
    tmp_path: Path,
) -> None:
    database, request, stop_id = _seed_stop(tmp_path, prepared=True)
    prs = FakePullRequests()
    issues = FakeIssues(request, exists=False)
    cards = FakeCards()

    result = _reconciler(database, issues, prs, cards).reconcile(stop_id)

    assert result.result == "cancelled"
    assert cards.calls == []
    assert issues.calls == ["find"]


def test_reconciler_wrong_host_has_zero_external_writes(tmp_path: Path) -> None:
    database, request, stop_id = _seed_stop(tmp_path, prepared=True)
    issues = FakeIssues(request, exists=False)
    reconciler = TaskStopReconciler(
        database,
        issue_client=issues,
        pull_request_reader=FakePullRequests(),
        kanban_stopper=FakeCards(),
        process_stopper=FakeProcesses(),
        current_host="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        clock=lambda: NOW,
    )

    with pytest.raises(TaskStopOwnerHostMismatch) as mismatch:
        reconciler.reconcile(stop_id)

    assert mismatch.value.owner_host == OWNER_HOST
    assert issues.calls == []
    with database.read() as connection:
        assert (
            connection.execute(
                "SELECT state FROM task_stop_requests WHERE stop_request_id = ?",
                (stop_id,),
            ).fetchone()[0]
            == "stopping"
        )


class ProcessBackend:
    def __init__(self, members: tuple[ProcessMemberIdentity, ...]) -> None:
        self.members = members
        self.signals: list[bool] = []

    def scope_members(self, identity: ProcessIdentity):
        return self.members

    def signal_scope(self, identity: ProcessIdentity, *, force: bool) -> None:
        self.signals.append(force)
        if force:
            self.members = ()


def _process_identity() -> ProcessIdentity:
    member = ProcessMemberIdentity(pid=901, start_identity="boot:10")
    return ProcessIdentity(
        binding=ProcessBinding(
            request_id=REQUEST_ID,
            task_settings_hash="b" * 64,
            project_id="c" * 64,
            task_id="card-1",
            run_id="run-1",
            host_id=OWNER_HOST,
        ),
        platform="posix",
        pid=901,
        start_identity=member.start_identity,
        scope_kind=ProcessScopeKind.PROCESS_GROUP,
        scope_id="901",
        control_group_id=None,
        members=(member,),
    )


def test_process_tree_stopper_accepts_already_dead_and_forces_term_ignoring_worker() -> (
    None
):
    identity = _process_identity()
    already_dead = ProcessBackend(())
    dead_result = ProcessTreeStopper(already_dead).stop(
        identity,
        current_host=OWNER_HOST,
    )
    ignoring = ProcessBackend(identity.members)
    forced_result = ProcessTreeStopper(
        ignoring,
        term_timeout_seconds=0.001,
        force_timeout_seconds=0.001,
        poll_interval_seconds=0.001,
    ).stop(identity, current_host=OWNER_HOST)

    assert dead_result.already_stopped is True
    assert already_dead.signals == []
    assert forced_result.completed is True
    assert forced_result.forced is True
    assert ignoring.signals == [False, True]


class RecapturingPosixBackend(PosixProcessBackend):
    def __init__(self, identity: ProcessIdentity) -> None:
        super().__init__(boot_id="test-boot")
        self.identity = identity
        self.capture_calls: list[tuple[ProcessBinding, int]] = []

    def capture_process_group(
        self,
        binding: ProcessBinding,
        *,
        pid: int,
    ) -> ProcessIdentity:
        self.capture_calls.append((binding, pid))
        return self.identity


def _durable_process_identity(
    database: TaskDatabase,
    request: TaskRequestV2,
) -> ProcessIdentity:
    with database.read() as connection:
        settings_hash = str(
            connection.execute(
                "SELECT task_settings_hash FROM task_settings_v2 WHERE request_id = ?",
                (request.request_id,),
            ).fetchone()[0]
        )
    member = ProcessMemberIdentity(pid=901, start_identity="boot:10")
    return ProcessIdentity(
        binding=ProcessBinding(
            request_id=request.request_id,
            task_settings_hash=settings_hash,
            project_id=request.projects[0].project_id,
            task_id="card-1",
            run_id="durable-run",
            host_id=OWNER_HOST,
        ),
        platform="posix",
        pid=member.pid,
        start_identity=member.start_identity,
        scope_kind=ProcessScopeKind.PROCESS_GROUP,
        scope_id=str(member.pid),
        control_group_id=None,
        members=(member,),
    )


def _insert_runtime_identity(
    database: TaskDatabase,
    identity: ProcessIdentity,
    *,
    row_run_id: str,
    raw_identity: str,
) -> None:
    binding = identity.binding
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO task_runtime_runs (
                run_id, request_id, task_settings_hash, project_id, host_id,
                worker_task_id, runtime_name, process_identity_json,
                message_packet_hash, state, result_hash, started_at, ended_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'test', ?, ?, 'running', NULL, ?, NULL)
            """,
            (
                row_run_id,
                binding.request_id,
                binding.task_settings_hash,
                binding.project_id,
                binding.host_id,
                binding.task_id,
                raw_identity,
                "e" * 64,
                NOW_TEXT,
            ),
        )


@pytest.mark.parametrize("stored", ("missing", "malformed", "ambiguous"))
def test_reconcile_identity_lookup_never_recaptures_a_live_posix_pid(
    tmp_path: Path,
    stored: str,
) -> None:
    database, request, _stop_id = _seed_stop(tmp_path)
    identity = _durable_process_identity(database, request)
    if stored == "malformed":
        _insert_runtime_identity(
            database,
            identity,
            row_run_id="malformed-row",
            raw_identity="{}",
        )
    elif stored == "ambiguous":
        for index in range(2):
            _insert_runtime_identity(
                database,
                identity,
                row_run_id=f"ambiguous-row-{index}",
                raw_identity=identity.to_json(),
            )
    backend = RecapturingPosixBackend(identity)
    script = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "forge"
            / "scripts"
            / "task-stop-reconcile.py"
        )
    )
    lookup = script["_stored_identity_lookup"](database, backend)

    with pytest.raises(ProcessIdentityError, match="durable exact"):
        lookup(identity.binding, identity.pid)

    assert backend.capture_calls == []


def test_reconciler_lists_only_unfinished_stop_requests(tmp_path: Path) -> None:
    first_db, first, first_stop = _seed_stop(tmp_path / "first")
    prs = FakePullRequests()
    _set_pr(first_db, first, 0, merged=False, reader=prs)
    issues = FakeIssues(first)
    cards = FakeCards()
    reconciler = _reconciler(first_db, issues, prs, cards)

    assert reconciler.list_reconcilable() == (first_stop,)
    reconciler.reconcile(first_stop)
    assert reconciler.list_reconcilable() == ()


def test_reconcile_script_accepts_multiple_exact_stops_in_one_run(
    tmp_path: Path,
    capsys,
) -> None:
    script = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "forge"
            / "scripts"
            / "task-stop-reconcile.py"
        )
    )
    stop_ids = (
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
    )

    class FakeReconciler:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def list_reconcilable(self):
            raise AssertionError("explicit Stop IDs must win")

        def reconcile(self, stop_id: str):
            self.calls.append(stop_id)
            return StopReconcileReceipt(
                stop_request_id=stop_id,
                request_id=REQUEST_ID,
                state="completed",
                result="cancelled",
                details_json="{}",
            )

    reconciler = FakeReconciler()
    code = script["main"](
        [
            "--settings-db",
            str(tmp_path / "settings.sqlite3"),
            "--kanban-db",
            str(tmp_path / "kanban.sqlite3"),
            "--dispatcher-db",
            str(tmp_path / "kanban.sqlite3"),
            "--owner-host",
            OWNER_HOST,
            "--stop-request-id",
            stop_ids[0],
            "--stop-request-id",
            stop_ids[1],
        ],
        reconciler_builder=lambda _args: reconciler,
    )

    assert code == 0
    assert reconciler.calls == list(stop_ids)
    output = json.loads(capsys.readouterr().out)
    assert [item["stop_request_id"] for item in output["stops"]] == list(stop_ids)
