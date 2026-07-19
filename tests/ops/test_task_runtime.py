from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import forge.ops.task_runtime as task_runtime_module
from forge.ops.github import PullRequestWriteState
from forge.ops.contracts import (
    parse_build_result,
    parse_review_result,
    source_result_hash,
)
from forge.ops.github_merge import BranchRefreshResult
from forge.ops.hermes import GateError, task_card_key
from forge.ops.task_options import MergeMode, TaskFlow
from forge.ops.task_database import TaskDatabase
from forge.ops.task_projects import TaskProject
from forge.ops.task_outbox import TaskOutbox
from forge.ops.task_runtime import (
    TaskFlowSnapshot,
    label_for_snapshot,
    load_ready_to_merge_snapshots,
    load_task_flow_snapshots,
    next_card_spec,
    record_branch_refresh_result,
    run_project_task_flow_worker,
    run_task_flow_worker,
)
from forge.ops.task_service import (
    TaskCreationRequest,
    TaskIssue,
    TaskService,
    read_task_marker,
)
from forge.ops.task_settings import TaskContent, TaskSettingsStore
from forge.ops.task_settings_v2 import TaskRequestV2, TaskSettingsV2
from forge.ops.task_flow import TaskStep
from forge.ops.task_worktrees import task_branch_name


NOW = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)
REPOSITORY = "openai/infinity-forge"
BASE = "a" * 40
HEAD = "b" * 40
NEW_HEAD = "c" * 40


class FakeGitHub:
    def __init__(self) -> None:
        self.issues: dict[int, TaskIssue] = {}
        self.prs: dict[str, PullRequestWriteState] = {}
        self._next_issue = 1

    def find_issue(self, repository: str, request_id: str) -> TaskIssue | None:
        matches = [
            issue
            for issue in self.issues.values()
            if read_task_marker(issue.body)["request_id"] == request_id
        ]
        return matches[0] if matches else None

    def create_issue(self, repository: str, title: str, body: str) -> TaskIssue:
        issue = TaskIssue(self._next_issue, title, body, ())
        self.issues[issue.number] = issue
        self._next_issue += 1
        return issue

    def update_issue(
        self,
        repository: str,
        issue_number: int,
        *,
        title: str,
        body: str,
    ) -> TaskIssue:
        issue = replace(self.issues[issue_number], title=title, body=body)
        self.issues[issue_number] = issue
        return issue

    def get_issue(self, repository: str, issue_number: int) -> TaskIssue:
        return self.issues[issue_number]

    def add_label(
        self,
        repository: str,
        issue_number: int,
        label: str,
    ) -> TaskIssue:
        issue = self.issues[issue_number]
        updated = replace(issue, labels=tuple(sorted(set((*issue.labels, label)))))
        self.issues[issue_number] = updated
        return updated

    def get_pr_write_state(self, pr_url: str) -> PullRequestWriteState:
        return self.prs[pr_url]


def _request(flow: TaskFlow = TaskFlow.BUILD_REVIEW) -> TaskCreationRequest:
    return TaskCreationRequest(
        request_id="9f7453ce-36ec-4e8e-9dfa-bb159b58c19b",
        repository=REPOSITORY,
        content=TaskContent(
            title="Build the selected Task",
            description="Implement the confirmed work.",
            acceptance_criteria=("The selected flow runs exactly once.",),
        ),
        task_flow=flow,
        merge_mode=MergeMode.SAFE_AUTO,
        confirmed_by="user-7",
        confirmed_at=NOW,
    )


def _create_hermes_db(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                body TEXT,
                assignee TEXT,
                status TEXT NOT NULL,
                idempotency_key TEXT,
                skills TEXT
            );
            CREATE TABLE task_runs (
                id INTEGER PRIMARY KEY,
                task_id TEXT NOT NULL,
                status TEXT NOT NULL,
                outcome TEXT,
                summary TEXT,
                metadata TEXT
            );
            CREATE TABLE task_links (parent_id TEXT NOT NULL, child_id TEXT NOT NULL);
            """
        )


def _activated_task(
    tmp_path: Path,
    github: FakeGitHub,
    *,
    flow: TaskFlow = TaskFlow.BUILD_REVIEW,
) -> tuple[TaskCreationRequest, str, Path, Path, Path]:
    settings_db = tmp_path / "settings.db"
    outbox_db = tmp_path / "outbox.db"
    hermes_db = tmp_path / "hermes.db"
    _create_hermes_db(hermes_db)
    request = _request(flow)
    created = TaskService(
        TaskSettingsStore(settings_db), github
    ).create_task_durable(request, TaskOutbox(outbox_db))
    assert created.settings.task_settings_hash is not None
    return (
        request,
        created.settings.task_settings_hash,
        settings_db,
        outbox_db,
        hermes_db,
    )


def _load(
    github: FakeGitHub,
    settings_db: Path,
    outbox_db: Path,
    hermes_db: Path,
) -> tuple[TaskFlowSnapshot, ...]:
    return load_task_flow_snapshots(
        settings_db=settings_db,
        outbox_db=outbox_db,
        hermes_db=hermes_db,
        github=github,
        repository=REPOSITORY,
    )


def _insert_card(
    database: Path,
    *,
    task_id: str,
    title: str,
    body: str,
    assignee: str,
    key: str,
    skill: str,
    parent_id: str | None = None,
    summary: dict[str, object] | None = None,
    run_id: int = 1,
) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO tasks
                (id, title, body, assignee, status, idempotency_key, skills)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                title,
                body,
                assignee,
                "done" if summary is not None else "ready",
                key,
                json.dumps([skill]),
            ),
        )
        if parent_id is not None:
            connection.execute(
                "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (parent_id, task_id),
            )
        if summary is not None:
            connection.execute(
                """
                INSERT INTO task_runs
                    (id, task_id, status, outcome, summary, metadata)
                VALUES (?, ?, 'done', 'completed', ?, '{}')
                """,
                (run_id, task_id, json.dumps(summary)),
            )


def _build_summary(
    settings_hash: str,
    *,
    commit: str = HEAD,
    base_commit: str = BASE,
) -> dict[str, object]:
    return {
        "format_version": "forge-build-result/v1",
        "task_settings_hash": settings_hash,
        "pr_url": f"https://github.com/{REPOSITORY}/pull/7",
        "built_base_commit": base_commit,
        "built_commit": commit,
        "changed_files": ["src/task.py"],
        "completed_items": ["The selected flow runs exactly once."],
        "remaining_items": [],
        "checks_by_item": {
            "The selected flow runs exactly once.": "tests/test_task.py"
        },
    }


def _review_summary(
    settings_hash: str,
    build: dict[str, object],
) -> dict[str, object]:
    return {
        "format_version": "forge-review-result/v1",
        "task_settings_hash": settings_hash,
        "result": "approve",
        "source_result_hash": source_result_hash(parse_build_result(build)),
        "pr_url": f"https://github.com/{REPOSITORY}/pull/7",
        "reviewed_commit": HEAD,
        "change_check": {"confirmed_work": ["src/task.py"], "problems": []},
        "requirements_check": {
            "completed": ["The selected flow runs exactly once."],
            "missing": [],
        },
        "fix_notes": None,
    }


def _pr(head: str = HEAD, base: str = BASE) -> PullRequestWriteState:
    return PullRequestWriteState(
        pr_url=f"https://github.com/{REPOSITORY}/pull/7",
        repository=REPOSITORY,
        pr_number=7,
        base_commit=base,
        base_ref="main",
        head_commit=head,
        is_open=True,
        is_merged=False,
        merged_commit=None,
        merged_base_commit=None,
        merged_head_commit=None,
    )


def _merged_pr() -> PullRequestWriteState:
    return PullRequestWriteState(
        pr_url=f"https://github.com/{REPOSITORY}/pull/7",
        repository=REPOSITORY,
        pr_number=7,
        base_commit="d" * 40,
        base_ref="main",
        head_commit=NEW_HEAD,
        is_open=False,
        is_merged=True,
        merged_commit="e" * 40,
        merged_base_commit=BASE,
        merged_head_commit=HEAD,
    )


def test_missing_root_loads_exact_request_and_plans_builder_root(tmp_path: Path) -> None:
    github = FakeGitHub()
    request, settings_hash, settings_db, outbox_db, hermes_db = _activated_task(
        tmp_path, github
    )

    snapshots = _load(github, settings_db, outbox_db, hermes_db)

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.request == request
    assert snapshot.settings.task_settings_hash == settings_hash
    assert snapshot.issue.number == 1
    assert snapshot.root_task_id is None
    assert snapshot.pr is None
    assert snapshot.state is None
    assert label_for_snapshot(snapshot) == "forge:ready-to-build"
    root = next_card_spec(snapshot)
    assert root.idempotency_key == task_card_key(REPOSITORY, 1, settings_hash)
    assert root.role.value == "builder"

    assert label_for_snapshot(
        replace(snapshot, root_task_id="root", current_card_status="blocked")
    ) == "forge:waiting-for-help"
    assert label_for_snapshot(
        replace(snapshot, root_task_id="root", current_card_status="failed")
    ) == "forge:failed"

    with pytest.raises(GateError, match="OWNER/REPO"):
        load_task_flow_snapshots(
            settings_db=settings_db,
            outbox_db=outbox_db,
            hermes_db=hermes_db,
            github=github,
            repository="not-a-repository",
        )


def test_worker_creates_missing_root_once_then_waits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github = FakeGitHub()
    _, _, settings_db, outbox_db, hermes_db = _activated_task(tmp_path, github)
    calls: list[tuple[str, ...]] = []
    guarded: list[str] = []
    original_guard = TaskSettingsStore.guard_active

    @contextmanager
    def tracked_guard(store: TaskSettingsStore, expected):
        guarded.append(expected.request_id)
        with original_guard(store, expected) as guard:
            yield guard

    class Create:
        def __init__(self, executable: str) -> None:
            assert executable == "hermes"

        def __call__(self, argv: tuple[str, ...]) -> None:
            calls.append(argv)
            _insert_card(
                hermes_db,
                task_id="root-1",
                title=argv[2],
                body=argv[argv.index("--body") + 1],
                assignee=argv[argv.index("--assignee") + 1],
                key=argv[argv.index("--idempotency-key") + 1],
                skill=argv[argv.index("--skill") + 1],
            )

    monkeypatch.setattr(task_runtime_module, "HermesCreateCommand", Create)
    monkeypatch.setattr(TaskSettingsStore, "guard_active", tracked_guard)

    first = run_task_flow_worker(
        settings_db=settings_db,
        outbox_db=outbox_db,
        hermes_db=hermes_db,
        hermes_path="hermes",
        github=github,
        repository=REPOSITORY,
        workspace="dir:/workspace",
    )
    second = run_task_flow_worker(
        settings_db=settings_db,
        outbox_db=outbox_db,
        hermes_db=hermes_db,
        hermes_path="hermes",
        github=github,
        repository=REPOSITORY,
        workspace="dir:/workspace",
    )

    assert len(calls) == 1
    assert guarded == [first[0].request_id]
    assert "--parent" not in calls[0]
    assert first[0].status == "created"
    assert second[0].status == "waiting"


def test_completed_root_replays_build_and_plans_only_selected_review(
    tmp_path: Path,
) -> None:
    github = FakeGitHub()
    request, settings_hash, settings_db, outbox_db, hermes_db = _activated_task(
        tmp_path, github
    )
    initial = _load(github, settings_db, outbox_db, hermes_db)[0]
    root = next_card_spec(initial)
    build = _build_summary(settings_hash)
    _insert_card(
        hermes_db,
        task_id="root-1",
        title=root.title,
        body=root.body,
        assignee="builder",
        key=root.idempotency_key,
        skill="build-task",
        summary=build,
    )
    github.prs[_pr().pr_url] = _pr()

    snapshot = _load(github, settings_db, outbox_db, hermes_db)[0]

    assert snapshot.root_task_id == "root-1"
    assert snapshot.state is not None
    assert snapshot.state.current_step.value == "review"
    assert snapshot.state.current_base_commit == BASE
    assert snapshot.state.current_commit == HEAD
    review = next_card_spec(snapshot)
    assert review.step.value == "review"
    assert review.role.value == "reviewer"
    assert review.parent_id == "root-1"
    assert '"source_summary"' in review.body
    assert label_for_snapshot(snapshot) == "forge:reviewing"
    assert request.task_flow is TaskFlow.BUILD_REVIEW


def test_current_head_change_invalidates_completed_proofs_and_restarts_build(
    tmp_path: Path,
) -> None:
    github = FakeGitHub()
    request, settings_hash, settings_db, outbox_db, hermes_db = _activated_task(
        tmp_path, github, flow=TaskFlow.BUILD
    )
    initial = _load(github, settings_db, outbox_db, hermes_db)[0]
    root = next_card_spec(initial)
    _insert_card(
        hermes_db,
        task_id="root-1",
        title=root.title,
        body=root.body,
        assignee="builder",
        key=root.idempotency_key,
        skill="build-task",
        summary=_build_summary(settings_hash),
    )
    github.prs[_pr().pr_url] = _pr(head=NEW_HEAD)

    snapshot = _load(github, settings_db, outbox_db, hermes_db)[0]

    assert snapshot.state is not None
    assert snapshot.state.current_step.value == "build"
    assert snapshot.state.current_commit == NEW_HEAD
    assert snapshot.state.completed_steps == ()
    restarted = next_card_spec(snapshot)
    assert restarted.step.value == "build"
    _insert_card(
        hermes_db,
        task_id="external-build-1",
        title=restarted.title,
        body=restarted.body,
        assignee="builder",
        key=restarted.idempotency_key,
        skill="build-task",
        parent_id="root-1",
    )
    replayed = _load(github, settings_db, outbox_db, hermes_db)[0]
    assert replayed.branch_refresh_count == 0
    assert replayed.state is not None and replayed.state.step_running
    assert load_ready_to_merge_snapshots(
        settings_db=settings_db,
        outbox_db=outbox_db,
        hermes_db=hermes_db,
        github=github,
        repository=REPOSITORY,
    ) == ()


def test_current_base_change_invalidates_build_only_proof(tmp_path: Path) -> None:
    github = FakeGitHub()
    _, settings_hash, settings_db, outbox_db, hermes_db = _activated_task(
        tmp_path, github, flow=TaskFlow.BUILD
    )
    root = next_card_spec(_load(github, settings_db, outbox_db, hermes_db)[0])
    _insert_card(
        hermes_db,
        task_id="root-1",
        title=root.title,
        body=root.body,
        assignee="builder",
        key=root.idempotency_key,
        skill="build-task",
        summary=_build_summary(settings_hash, base_commit=BASE),
    )
    github.prs[_pr().pr_url] = _pr(base="d" * 40)

    snapshot = _load(github, settings_db, outbox_db, hermes_db)[0]

    assert snapshot.state is not None
    assert snapshot.state.current_step.value == "build"
    assert snapshot.state.completed_steps == ()
    assert load_ready_to_merge_snapshots(
        settings_db=settings_db,
        outbox_db=outbox_db,
        hermes_db=hermes_db,
        github=github,
        repository=REPOSITORY,
    ) == ()


def test_completed_refresh_intent_is_exposed_as_branch_refresh_before_projection(
    tmp_path: Path,
) -> None:
    github = FakeGitHub()
    request, settings_hash, settings_db, outbox_db, hermes_db = _activated_task(
        tmp_path, github, flow=TaskFlow.BUILD
    )
    root = next_card_spec(_load(github, settings_db, outbox_db, hermes_db)[0])
    _insert_card(
        hermes_db,
        task_id="root-1",
        title=root.title,
        body=root.body,
        assignee="builder",
        key=root.idempotency_key,
        skill="build-task",
        summary=_build_summary(settings_hash),
    )
    github.prs[_pr().pr_url] = _pr()
    store = TaskSettingsStore(settings_db)
    intent = store.reserve_branch_refresh(
        request.request_id,
        pr_url=_pr().pr_url,
        expected_base_commit=BASE,
        expected_head_commit=HEAD,
        applied_refresh_count=0,
    )
    store.complete_branch_refresh(
        intent,
        current_base_commit="d" * 40,
        current_head_commit=NEW_HEAD,
    )
    github.prs[_pr().pr_url] = _pr(head=NEW_HEAD, base="d" * 40)

    recovered = _load(github, settings_db, outbox_db, hermes_db)[0]
    spec = next_card_spec(recovered)

    assert recovered.pending_source_kind == "branch_refresh"
    assert recovered.branch_refresh_count == 1
    assert recovered.state is not None
    assert recovered.state.current_step.value == "build"
    assert spec is not None and '"source_kind":"branch_refresh"' in spec.body
    assert '"branch_refresh_count":1' in spec.body


def test_remote_refresh_readback_completes_reserved_intent_before_projection(
    tmp_path: Path,
) -> None:
    github = FakeGitHub()
    request, settings_hash, settings_db, outbox_db, hermes_db = _activated_task(
        tmp_path, github, flow=TaskFlow.BUILD
    )
    root = next_card_spec(_load(github, settings_db, outbox_db, hermes_db)[0])
    _insert_card(
        hermes_db,
        task_id="root-1",
        title=root.title,
        body=root.body,
        assignee="builder",
        key=root.idempotency_key,
        skill="build-task",
        summary=_build_summary(settings_hash),
    )
    github.prs[_pr().pr_url] = _pr()
    store = TaskSettingsStore(settings_db)
    reserved = store.reserve_branch_refresh(
        request.request_id,
        pr_url=_pr().pr_url,
        expected_base_commit=BASE,
        expected_head_commit=HEAD,
        applied_refresh_count=0,
    )
    github.prs[_pr().pr_url] = _pr(head=NEW_HEAD, base="d" * 40)

    recovered = _load(github, settings_db, outbox_db, hermes_db)[0]
    persisted = store.get_branch_refresh_replay(
        request.request_id,
        applied_refresh_count=0,
    )

    assert recovered.pending_source_kind == "branch_refresh"
    assert recovered.branch_refresh_count == 1
    assert persisted is not None and persisted.completed
    assert persisted.current_base_commit == "d" * 40
    assert persisted.current_head_commit == NEW_HEAD
    assert persisted.refresh_number == reserved.refresh_number


def test_task_timer_recovers_pending_refresh_before_creating_build_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github = FakeGitHub()
    request, settings_hash, settings_db, outbox_db, hermes_db = _activated_task(
        tmp_path, github, flow=TaskFlow.BUILD
    )
    root = next_card_spec(_load(github, settings_db, outbox_db, hermes_db)[0])
    _insert_card(
        hermes_db,
        task_id="root-1",
        title=root.title,
        body=root.body,
        assignee="builder",
        key=root.idempotency_key,
        skill="build-task",
        summary=_build_summary(settings_hash),
    )
    github.prs[_pr().pr_url] = _pr()
    settings_store = TaskSettingsStore(settings_db)
    settings_store.reserve_branch_refresh(
        request.request_id,
        pr_url=_pr().pr_url,
        expected_base_commit=BASE,
        expected_head_commit=HEAD,
        applied_refresh_count=0,
    )
    github.prs[_pr().pr_url] = _pr(head=NEW_HEAD, base="d" * 40)
    calls: list[tuple[str, ...]] = []

    class Create:
        def __init__(self, executable: str) -> None:
            assert executable == "hermes"

        def __call__(self, argv: tuple[str, ...]) -> None:
            with sqlite3.connect(settings_db) as connection:
                persisted = connection.execute(
                    """
                    SELECT completed_at
                    FROM task_branch_refresh_intents
                    WHERE request_id = ? AND refresh_number = 1
                    """,
                    (request.request_id,),
                ).fetchone()
            assert persisted is not None and persisted[0] is not None
            calls.append(argv)
            _insert_card(
                hermes_db,
                task_id="refresh-build-1",
                title=argv[2],
                body=argv[argv.index("--body") + 1],
                assignee=argv[argv.index("--assignee") + 1],
                key=argv[argv.index("--idempotency-key") + 1],
                skill=argv[argv.index("--skill") + 1],
                parent_id=argv[argv.index("--parent") + 1],
            )

    monkeypatch.setattr(task_runtime_module, "HermesCreateCommand", Create)

    reports = run_task_flow_worker(
        settings_db=settings_db,
        outbox_db=outbox_db,
        hermes_db=hermes_db,
        hermes_path="hermes",
        github=github,
        repository=REPOSITORY,
        workspace="dir:/workspace",
    )
    replayed = _load(github, settings_db, outbox_db, hermes_db)[0]

    assert reports[0].status == "created"
    assert len(calls) == 1
    assert '"source_kind":"branch_refresh"' in calls[0][calls[0].index("--body") + 1]
    assert replayed.branch_refresh_count == 1
    assert replayed.state is not None and replayed.state.step_running
    assert settings_store.get_branch_refresh_replay(
        request.request_id,
        applied_refresh_count=1,
    ) is None


def test_issue_marker_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    github = FakeGitHub()
    _, _, settings_db, outbox_db, hermes_db = _activated_task(tmp_path, github)
    issue = github.issues[1]
    github.issues[1] = replace(
        issue,
        body=issue.body.replace(
            read_task_marker(issue.body)["task_content_hash"], "f" * 64
        ),
    )

    with pytest.raises(GateError, match="content hash"):
        _load(github, settings_db, outbox_db, hermes_db)


@pytest.mark.parametrize("changed_field", ("title", "body"))
def test_issue_title_or_body_change_fails_closed(
    tmp_path: Path,
    changed_field: str,
) -> None:
    github = FakeGitHub()
    _, _, settings_db, outbox_db, hermes_db = _activated_task(tmp_path, github)
    issue = github.issues[1]
    github.issues[1] = replace(
        issue,
        **{
            changed_field: (
                issue.title + " changed"
                if changed_field == "title"
                else issue.body + "\nchanged"
            )
        },
    )

    with pytest.raises(GateError, match="confirmed Task"):
        _load(github, settings_db, outbox_db, hermes_db)


def test_build_only_current_result_is_exposed_to_merge_loader(tmp_path: Path) -> None:
    github = FakeGitHub()
    _, settings_hash, settings_db, outbox_db, hermes_db = _activated_task(
        tmp_path, github, flow=TaskFlow.BUILD
    )
    root = next_card_spec(_load(github, settings_db, outbox_db, hermes_db)[0])
    _insert_card(
        hermes_db,
        task_id="root-1",
        title=root.title,
        body=root.body,
        assignee="builder",
        key=root.idempotency_key,
        skill="build-task",
        summary=_build_summary(settings_hash),
    )
    github.prs[_pr().pr_url] = _pr()

    ready = load_ready_to_merge_snapshots(
        settings_db=settings_db,
        outbox_db=outbox_db,
        hermes_db=hermes_db,
        github=github,
        repository=REPOSITORY,
    )

    assert len(ready) == 1
    assert ready[0].state is not None
    assert ready[0].state.status.value == "ready_to_merge"


@pytest.mark.parametrize("changed_field", ("title", "body"))
def test_merge_loader_rejects_changed_task_issue_content(
    tmp_path: Path,
    changed_field: str,
) -> None:
    github = FakeGitHub()
    _, settings_hash, settings_db, outbox_db, hermes_db = _activated_task(
        tmp_path,
        github,
        flow=TaskFlow.BUILD,
    )
    root = next_card_spec(_load(github, settings_db, outbox_db, hermes_db)[0])
    _insert_card(
        hermes_db,
        task_id="root-1",
        title=root.title,
        body=root.body,
        assignee="builder",
        key=root.idempotency_key,
        skill="build-task",
        summary=_build_summary(settings_hash),
    )
    github.prs[_pr().pr_url] = _pr()
    issue = github.issues[1]
    github.issues[1] = replace(
        issue,
        **{
            changed_field: (
                issue.title + " changed"
                if changed_field == "title"
                else issue.body + "\nchanged"
            )
        },
    )

    with pytest.raises(GateError, match="confirmed Task"):
        load_ready_to_merge_snapshots(
            settings_db=settings_db,
            outbox_db=outbox_db,
            hermes_db=hermes_db,
            github=github,
            repository=REPOSITORY,
        )


def test_already_merged_pr_replays_against_historical_base_and_head(
    tmp_path: Path,
) -> None:
    github = FakeGitHub()
    _, settings_hash, settings_db, outbox_db, hermes_db = _activated_task(
        tmp_path, github, flow=TaskFlow.BUILD
    )
    root = next_card_spec(_load(github, settings_db, outbox_db, hermes_db)[0])
    _insert_card(
        hermes_db,
        task_id="root-1",
        title=root.title,
        body=root.body,
        assignee="builder",
        key=root.idempotency_key,
        skill="build-task",
        summary=_build_summary(settings_hash),
    )
    github.prs[_pr().pr_url] = _merged_pr()

    ready = load_ready_to_merge_snapshots(
        settings_db=settings_db,
        outbox_db=outbox_db,
        hermes_db=hermes_db,
        github=github,
        repository=REPOSITORY,
    )

    assert len(ready) == 1
    assert ready[0].pr is not None and ready[0].pr.is_merged
    assert ready[0].pr.base_commit == BASE
    assert ready[0].pr.head_commit == HEAD


def test_review_child_body_and_single_completed_run_are_strict(tmp_path: Path) -> None:
    github = FakeGitHub()
    _, settings_hash, settings_db, outbox_db, hermes_db = _activated_task(
        tmp_path, github
    )
    root = next_card_spec(_load(github, settings_db, outbox_db, hermes_db)[0])
    build = _build_summary(settings_hash)
    _insert_card(
        hermes_db,
        task_id="root-1",
        title=root.title,
        body=root.body,
        assignee="builder",
        key=root.idempotency_key,
        skill="build-task",
        summary=build,
    )
    github.prs[_pr().pr_url] = _pr()
    review = next_card_spec(_load(github, settings_db, outbox_db, hermes_db)[0])
    _insert_card(
        hermes_db,
        task_id="review-1",
        title=review.title,
        body=review.body,
        assignee="reviewer",
        key=review.idempotency_key,
        skill="review-task",
        parent_id="root-1",
        summary=_review_summary(settings_hash, build),
        run_id=2,
    )

    ready = _load(github, settings_db, outbox_db, hermes_db)[0]
    assert ready.state is not None and ready.state.status.value == "ready_to_merge"

    with sqlite3.connect(hermes_db) as connection:
        connection.execute(
            "UPDATE tasks SET body = replace(body, 'review', 'deep_check') "
            "WHERE id = 'review-1'"
        )
    with pytest.raises(GateError, match="step card body"):
        _load(github, settings_db, outbox_db, hermes_db)


def test_root_role_and_skill_must_be_exact(tmp_path: Path) -> None:
    github = FakeGitHub()
    _, settings_hash, settings_db, outbox_db, hermes_db = _activated_task(
        tmp_path, github, flow=TaskFlow.BUILD
    )
    root = next_card_spec(_load(github, settings_db, outbox_db, hermes_db)[0])
    _insert_card(
        hermes_db,
        task_id="root-1",
        title=root.title,
        body=root.body,
        assignee="reviewer",
        key=root.idempotency_key,
        skill="review-task",
        summary=_build_summary(settings_hash),
    )
    github.prs[_pr().pr_url] = _pr()

    with pytest.raises(GateError, match="role or skill"):
        _load(github, settings_db, outbox_db, hermes_db)


def test_foreign_forge_child_cannot_attach_to_another_task_root(
    tmp_path: Path,
) -> None:
    github = FakeGitHub()
    _, settings_hash, settings_db, outbox_db, hermes_db = _activated_task(
        tmp_path, github, flow=TaskFlow.BUILD
    )
    root = next_card_spec(_load(github, settings_db, outbox_db, hermes_db)[0])
    _insert_card(
        hermes_db,
        task_id="root-1",
        title=root.title,
        body=root.body,
        assignee="builder",
        key=root.idempotency_key,
        skill="build-task",
        summary=_build_summary(settings_hash),
    )
    _insert_card(
        hermes_db,
        task_id="foreign-review",
        title="Review Task: other/repo#2",
        body="{}",
        assignee="reviewer",
        key=f"forge-step:other/repo#2:review:{'f' * 16}",
        skill="review-task",
        parent_id="root-1",
    )
    github.prs[_pr().pr_url] = _pr()

    with pytest.raises(GateError, match="different Task"):
        _load(github, settings_db, outbox_db, hermes_db)


def test_multiple_completed_runs_for_one_card_are_ambiguous(tmp_path: Path) -> None:
    github = FakeGitHub()
    _, settings_hash, settings_db, outbox_db, hermes_db = _activated_task(
        tmp_path, github, flow=TaskFlow.BUILD
    )
    root = next_card_spec(_load(github, settings_db, outbox_db, hermes_db)[0])
    summary = _build_summary(settings_hash)
    _insert_card(
        hermes_db,
        task_id="root-1",
        title=root.title,
        body=root.body,
        assignee="builder",
        key=root.idempotency_key,
        skill="build-task",
        summary=summary,
    )
    with sqlite3.connect(hermes_db) as connection:
        connection.execute(
            """
            INSERT INTO task_runs
                (id, task_id, status, outcome, summary, metadata)
            VALUES (2, 'root-1', 'done', 'completed', ?, '{}')
            """,
            (json.dumps(summary),),
        )
    github.prs[_pr().pr_url] = _pr()

    with pytest.raises(GateError, match="more than one completed run"):
        _load(github, settings_db, outbox_db, hermes_db)


def test_branch_refresh_is_recorded_as_one_new_build_card(tmp_path: Path) -> None:
    github = FakeGitHub()
    request, settings_hash, settings_db, outbox_db, hermes_db = _activated_task(
        tmp_path, github, flow=TaskFlow.BUILD
    )
    root = next_card_spec(_load(github, settings_db, outbox_db, hermes_db)[0])
    _insert_card(
        hermes_db,
        task_id="root-1",
        title=root.title,
        body=root.body,
        assignee="builder",
        key=root.idempotency_key,
        skill="build-task",
        summary=_build_summary(settings_hash),
    )
    github.prs[_pr().pr_url] = _pr()
    snapshot = _load(github, settings_db, outbox_db, hermes_db)[0]
    calls: list[tuple[str, ...]] = []
    result = BranchRefreshResult(
        code="RESTART_FLOW",
        reason="branch was refreshed; restart validation from Build",
        current_commit=NEW_HEAD,
        current_base_commit="d" * 40,
        branch_refresh_count=1,
        next_step="build",
        invalidate_existing_proofs=True,
        flow_completed=False,
        final_tested_commit=None,
    )
    settings_store = TaskSettingsStore(settings_db)
    intent = settings_store.reserve_branch_refresh(
        request.request_id,
        pr_url=_pr().pr_url,
        expected_base_commit=BASE,
        expected_head_commit=HEAD,
        applied_refresh_count=0,
    )
    settings_store.complete_branch_refresh(
        intent,
        current_base_commit=result.current_base_commit,
        current_head_commit=result.current_commit,
    )

    key = record_branch_refresh_result(
        snapshot,
        result,
        hermes_store=__import__("forge.ops.hermes", fromlist=["HermesStore"]).HermesStore(
            hermes_db
        ),
        create_card=lambda argv: calls.append(tuple(argv)),
        workspace="dir:/workspace",
    )

    assert ":build:" in key
    assert len(calls) == 1
    assert calls[0][calls[0].index("--idempotency-key") + 1] == key
    assert '"branch_refresh_count":1' in calls[0][calls[0].index("--body") + 1]
    argv = calls[0]
    _insert_card(
        hermes_db,
        task_id="refresh-build-1",
        title=argv[2],
        body=argv[argv.index("--body") + 1],
        assignee=argv[argv.index("--assignee") + 1],
        key=key,
        skill=argv[argv.index("--skill") + 1],
        parent_id=argv[argv.index("--parent") + 1],
    )
    github.prs[_pr().pr_url] = _pr(head=NEW_HEAD, base="d" * 40)

    replayed = _load(github, settings_db, outbox_db, hermes_db)[0]

    assert replayed.branch_refresh_count == 1
    assert replayed.state is not None and replayed.state.step_running
    assert replayed.state.current_step.value == "build"

    # A replay of the same exact refresh sees the existing idempotency key.
    record_branch_refresh_result(
        snapshot,
        result,
        hermes_store=__import__("forge.ops.hermes", fromlist=["HermesStore"]).HermesStore(
            hermes_db
        ),
        create_card=lambda repeated: calls.append(tuple(repeated)),
        workspace="dir:/workspace",
    )
    assert len(calls) == 1


def test_branch_refresh_count_must_increment_exactly_once(tmp_path: Path) -> None:
    github = FakeGitHub()
    _, settings_hash, settings_db, outbox_db, hermes_db = _activated_task(
        tmp_path, github, flow=TaskFlow.BUILD
    )
    root = next_card_spec(_load(github, settings_db, outbox_db, hermes_db)[0])
    _insert_card(
        hermes_db,
        task_id="root-1",
        title=root.title,
        body=root.body,
        assignee="builder",
        key=root.idempotency_key,
        skill="build-task",
        summary=_build_summary(settings_hash),
    )
    github.prs[_pr().pr_url] = _pr()
    snapshot = _load(github, settings_db, outbox_db, hermes_db)[0]
    invalid = BranchRefreshResult(
        code="RESTART_FLOW",
        reason="bad",
        current_commit=NEW_HEAD,
        current_base_commit="d" * 40,
        branch_refresh_count=2,
        next_step="build",
        invalidate_existing_proofs=True,
        flow_completed=False,
        final_tested_commit=None,
    )

    with pytest.raises(GateError, match="count"):
        record_branch_refresh_result(
            snapshot,
            invalid,
            hermes_store=__import__(
                "forge.ops.hermes", fromlist=["HermesStore"]
            ).HermesStore(hermes_db),
            create_card=lambda argv: None,
            workspace="dir:/workspace",
        )

    over_limit = replace(invalid, branch_refresh_count=4)
    with pytest.raises(GateError, match="limit"):
        record_branch_refresh_result(
            replace(snapshot, branch_refresh_count=3),
            over_limit,
            hermes_store=__import__(
                "forge.ops.hermes", fromlist=["HermesStore"]
            ).HermesStore(hermes_db),
            create_card=lambda argv: None,
            workspace="dir:/workspace",
        )


def _git_v2(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _v2_project(tmp_path: Path, name: str, repository: str) -> TaskProject:
    remote = tmp_path / f"{name}.git"
    workspace = tmp_path / name
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(workspace)], check=True, capture_output=True)
    _git_v2(workspace, "config", "user.name", "Test User")
    _git_v2(workspace, "config", "user.email", "test@example.com")
    (workspace / "README.md").write_text(f"{name}\n", encoding="utf-8")
    _git_v2(workspace, "add", "README.md")
    _git_v2(workspace, "commit", "-m", "base")
    _git_v2(workspace, "branch", "-M", "main")
    _git_v2(workspace, "push", "-u", "origin", "main")
    return TaskProject.create(
        repository=repository,
        workspace=str(workspace.resolve()),
        remote_name="origin",
        base_branch="main",
        base_commit=_git_v2(workspace, "rev-parse", "HEAD"),
        host_id="d6f70d5d-6482-45f5-80d2-219ec2ad4d19",
    )


def _activated_v2(
    tmp_path: Path,
    *,
    flow: TaskFlow = TaskFlow.BUILD,
) -> tuple[Path, Path, TaskRequestV2, TaskSettingsV2]:
    projects = (
        _v2_project(tmp_path, "project-one", "example/project-one"),
        _v2_project(tmp_path, "project-two", "example/project-two"),
    )
    request = TaskRequestV2.create(
        request_id="4485be21-2a8f-41b8-a2a2-e25722df284e",
        management_repository="example/infinity-forge",
        task_content=TaskContent(
            title="Run in selected Projects",
            description="Change only the selected repositories.",
            acceptance_criteria=("Each Project gets its own PR.",),
        ),
        task_flow=flow,
        merge_mode=MergeMode.MANUAL,
        merge_order=None,
        projects=projects,
        task_owner_host="d6f70d5d-6482-45f5-80d2-219ec2ad4d19",
        confirmed_by="user-7",
        confirmed_at=NOW,
    )
    settings = TaskSettingsV2.create(request=request, parent_issue_number=21)
    database_path = tmp_path / "task-v2.db"
    database = TaskDatabase(database_path)
    request_payload = json.loads(request.to_json())
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
                request_payload["confirmed_at"],
            ),
        )
        connection.execute(
            """
            INSERT INTO task_settings_v2 (
                task_settings_hash, request_id, request_hash, format_version,
                settings_json, management_repository, parent_issue_number,
                task_owner_host, confirmed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                settings.task_settings_hash,
                settings.request_id,
                settings.request_hash,
                settings.format_version,
                settings.to_json(),
                settings.management_repository,
                settings.parent_issue_number,
                settings.task_owner_host,
                request_payload["confirmed_at"],
            ),
        )
        for index, project in enumerate(request.projects, start=1):
            project_json = json.dumps(
                {
                    "project_id": project.project_id,
                    "repository": project.repository,
                    "workspace": project.workspace,
                    "remote_name": project.remote_name,
                    "base_branch": project.base_branch,
                    "base_commit": project.base_commit,
                    "host_id": project.host_id,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                """
                INSERT INTO task_projects (
                    request_id, project_id, task_settings_hash, project_json,
                    state, root_card_id, updated_at
                ) VALUES (?, ?, ?, ?, 'ready', ?, ?)
                """,
                (
                    request.request_id,
                    project.project_id,
                    settings.task_settings_hash,
                    project_json,
                    f"management-root-{index}",
                    request_payload["confirmed_at"],
                ),
            )
        for event_type in ("settings_activated", "active"):
            connection.execute(
                """
                INSERT INTO task_events (
                    request_id, task_settings_hash, event_type, event_key,
                    event_json, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    request.request_id,
                    settings.task_settings_hash,
                    event_type,
                    event_type,
                    json.dumps(
                        {"task_settings_hash": settings.task_settings_hash},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    request_payload["confirmed_at"],
                ),
            )
        connection.execute(
            """
            INSERT INTO task_events (
                request_id, task_settings_hash, event_type, event_key,
                event_json, occurred_at
            ) VALUES (?, ?, 'dispatch_ready', 'dispatch_ready', ?, ?)
            """,
            (
                request.request_id,
                settings.task_settings_hash,
                json.dumps(
                    {
                        "project_ids": [project.project_id for project in projects],
                        "task_settings_hash": settings.task_settings_hash,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                request_payload["confirmed_at"],
            ),
        )
    hermes_db = tmp_path / "hermes-v2.db"
    _create_hermes_db(hermes_db)
    return database_path, hermes_db, request, settings


def test_v2_worker_enumerates_all_projects_and_uses_each_worktree(
    tmp_path: Path,
) -> None:
    settings_db, hermes_db, request, settings = _activated_v2(tmp_path)
    calls: list[tuple[str, ...]] = []

    reports = run_project_task_flow_worker(
        settings_db=settings_db,
        hermes_db=hermes_db,
        hermes_path="hermes",
        github=FakeGitHub(),
        worktree_root=tmp_path / "worktrees",
        create_card=lambda argv: calls.append(tuple(argv)),
        remote_repository=lambda workspace, _remote: f"example/{workspace.name}",
    )

    assert len(reports) == len(request.projects) == 2
    assert {report.project_id for report in reports} == {
        project.project_id for project in request.projects
    }
    assert len(calls) == 2
    calls_by_repository = {
        json.loads(call[call.index("--body") + 1])["project"]["repository"]: call
        for call in calls
    }
    for project in request.projects:
        call = calls_by_repository[project.repository]
        body = json.loads(call[call.index("--body") + 1])
        assert body["project"] == {
            "project_id": project.project_id,
            "repository": project.repository,
            "workspace": project.workspace,
            "remote_name": project.remote_name,
            "base_branch": project.base_branch,
            "base_commit": project.base_commit,
            "host_id": project.host_id,
        }
        assert body["task_settings_hash"] == settings.task_settings_hash
        assert Path(call[call.index("--workspace") + 1].removeprefix("dir:")).is_dir()
        assert project.repository in call[2]


def test_v2_worker_rejects_tampered_project_snapshot_before_card_write(
    tmp_path: Path,
) -> None:
    settings_db, hermes_db, request, _settings = _activated_v2(tmp_path)
    with sqlite3.connect(settings_db) as connection:
        connection.execute(
            "UPDATE task_projects SET project_json = '{}' WHERE project_id = ?",
            (request.projects[0].project_id,),
        )
    calls: list[tuple[str, ...]] = []

    with pytest.raises(GateError, match="Project.*snapshot"):
        run_project_task_flow_worker(
            settings_db=settings_db,
            hermes_db=hermes_db,
            hermes_path="hermes",
            github=FakeGitHub(),
            worktree_root=tmp_path / "worktrees",
            create_card=lambda argv: calls.append(tuple(argv)),
            remote_repository=lambda workspace, _remote: f"example/{workspace.name}",
        )

    assert calls == []


def test_v2_worker_dry_run_does_not_create_worktrees_or_update_registry(
    tmp_path: Path,
) -> None:
    settings_db, hermes_db, request, _settings = _activated_v2(tmp_path)
    worktree_root = tmp_path / "worktrees"
    calls: list[tuple[str, ...]] = []

    reports = run_project_task_flow_worker(
        settings_db=settings_db,
        hermes_db=hermes_db,
        hermes_path="hermes",
        github=FakeGitHub(),
        worktree_root=worktree_root,
        dry_run=True,
        create_card=lambda argv: calls.append(tuple(argv)),
        remote_repository=lambda workspace, _remote: f"example/{workspace.name}",
    )

    assert all(report.status == "planned" for report in reports)
    assert calls == []
    assert not worktree_root.exists()
    with sqlite3.connect(settings_db) as connection:
        rows = connection.execute(
            """
            SELECT state, branch_name, worktree_path FROM task_projects
            WHERE request_id = ? ORDER BY project_id
            """,
            (request.request_id,),
        ).fetchall()
    assert rows == [("ready", None, None), ("ready", None, None)]


def test_v2_worker_preflights_every_project_before_any_persistent_write(
    tmp_path: Path,
) -> None:
    settings_db, hermes_db, request, _settings = _activated_v2(tmp_path)
    ordered = sorted(request.projects, key=lambda project: project.project_id)
    rejected_workspace = Path(ordered[1].workspace)
    worktree_root = tmp_path / "worktrees"
    calls: list[tuple[str, ...]] = []
    database_before = settings_db.read_bytes()

    def remote_repository(workspace: Path, _remote: str) -> str:
        if workspace == rejected_workspace:
            return "other/wrong-project"
        return next(
            project.repository
            for project in request.projects
            if Path(project.workspace) == workspace
        )

    with pytest.raises(GateError, match="remote repository"):
        run_project_task_flow_worker(
            settings_db=settings_db,
            hermes_db=hermes_db,
            hermes_path="hermes",
            github=FakeGitHub(),
            worktree_root=worktree_root,
            create_card=lambda argv: calls.append(tuple(argv)),
            remote_repository=remote_repository,
        )

    assert calls == []
    assert not worktree_root.exists()
    assert settings_db.read_bytes() == database_before
    for suffix in ("-journal", "-wal", "-shm"):
        assert not Path(f"{settings_db}{suffix}").exists()
    with sqlite3.connect(settings_db) as connection:
        rows = connection.execute(
            """
            SELECT state, branch_name, worktree_path FROM task_projects
            WHERE request_id = ? ORDER BY project_id
            """,
            (request.request_id,),
        ).fetchall()
    assert rows == [("ready", None, None), ("ready", None, None)]
    for project in request.projects:
        branch_readback = subprocess.run(
            [
                "git",
                "-C",
                project.workspace,
                "show-ref",
                "--verify",
                f"refs/heads/{task_branch_name(request.request_id, project.project_id)}",
            ],
            capture_output=True,
            check=False,
        )
        assert branch_readback.returncode != 0


def test_v2_worker_rejects_missing_project_row_before_any_write(
    tmp_path: Path,
) -> None:
    settings_db, hermes_db, request, _settings = _activated_v2(tmp_path)
    with sqlite3.connect(settings_db) as connection:
        connection.execute(
            "DELETE FROM task_projects WHERE project_id = ?",
            (request.projects[1].project_id,),
        )
    calls: list[tuple[str, ...]] = []
    worktree_root = tmp_path / "worktrees"

    with pytest.raises(GateError, match="complete Project set"):
        run_project_task_flow_worker(
            settings_db=settings_db,
            hermes_db=hermes_db,
            hermes_path="hermes",
            github=FakeGitHub(),
            worktree_root=worktree_root,
            create_card=lambda argv: calls.append(tuple(argv)),
            remote_repository=lambda workspace, _remote: f"example/{workspace.name}",
        )

    assert calls == []
    assert not worktree_root.exists()


def test_v2_dry_run_missing_database_creates_nothing(tmp_path: Path) -> None:
    missing = tmp_path / "missing-parent" / "task.db"
    hermes_db = tmp_path / "hermes.db"
    _create_hermes_db(hermes_db)

    with pytest.raises(GateError, match="does not exist"):
        run_project_task_flow_worker(
            settings_db=missing,
            hermes_db=hermes_db,
            hermes_path="hermes",
            github=FakeGitHub(),
            worktree_root=tmp_path / "worktrees",
            dry_run=True,
            create_card=lambda _argv: None,
        )

    assert not missing.exists()
    assert not missing.parent.exists()


def _insert_v2_root(
    hermes_db: Path,
    call: tuple[str, ...],
    *,
    summary: dict[str, object] | None,
    task_id: str = "v2-root",
) -> None:
    _insert_card(
        hermes_db,
        task_id=task_id,
        title=call[2],
        body=call[call.index("--body") + 1],
        assignee=call[call.index("--assignee") + 1],
        key=call[call.index("--idempotency-key") + 1],
        skill=call[call.index("--skill") + 1],
        summary=summary,
    )


def _insert_v2_call(
    hermes_db: Path,
    call: tuple[str, ...],
    *,
    task_id: str,
    parent_id: str | None,
    summary: dict[str, object] | None,
    run_id: int = 1,
) -> None:
    _insert_card(
        hermes_db,
        task_id=task_id,
        title=call[2],
        body=call[call.index("--body") + 1],
        assignee=call[call.index("--assignee") + 1],
        key=call[call.index("--idempotency-key") + 1],
        skill=call[call.index("--skill") + 1],
        parent_id=parent_id,
        summary=summary,
        run_id=run_id,
    )


def _v2_build_summary(
    settings: TaskSettingsV2,
    project: TaskProject,
    *,
    pr_repository: str | None = None,
    built_commit: str = "f" * 40,
) -> dict[str, object]:
    repository = pr_repository or project.repository
    return {
        "format_version": "forge-build-result/v1",
        "task_settings_hash": settings.task_settings_hash,
        "pr_url": f"https://github.com/{repository}/pull/7",
        "built_base_commit": project.base_commit,
        "built_commit": built_commit,
        "changed_files": ["src/task.py"],
        "completed_items": ["Each Project gets its own PR."],
        "remaining_items": [],
        "checks_by_item": {"Each Project gets its own PR.": "tests pass"},
    }


def _commit_v2_worktree(
    root_call: tuple[str, ...],
    *,
    filename: str,
) -> str:
    worktree = Path(
        root_call[root_call.index("--workspace") + 1].removeprefix("dir:")
    )
    _git_v2(worktree, "config", "user.name", "Test User")
    _git_v2(worktree, "config", "user.email", "test@example.com")
    (worktree / filename).write_text(f"{filename}\n", encoding="utf-8")
    _git_v2(worktree, "add", filename)
    _git_v2(worktree, "commit", "-m", f"add {filename}")
    return _git_v2(worktree, "rev-parse", "HEAD")


def _v2_call_for(
    calls: list[tuple[str, ...]],
    project: TaskProject,
    step: str,
) -> tuple[str, ...]:
    return next(
        call
        for call in calls
        if (
            (body := json.loads(call[call.index("--body") + 1]))["project"]
            ["project_id"]
            == project.project_id
            and body["step"] == step
        )
    )


def _v2_pr(project: TaskProject, head_commit: str) -> PullRequestWriteState:
    return PullRequestWriteState(
        pr_url=f"https://github.com/{project.repository}/pull/7",
        repository=project.repository,
        pr_number=7,
        base_commit=project.base_commit,
        base_ref=project.base_branch,
        head_commit=head_commit,
        is_open=True,
        is_merged=False,
        merged_commit=None,
        merged_base_commit=None,
        merged_head_commit=None,
    )


@dataclass(frozen=True, slots=True)
class _V2FixReplay:
    settings_db: Path
    hermes_db: Path
    project: TaskProject
    worker_kwargs: dict[str, object]
    github: FakeGitHub
    fix_call: tuple[str, ...]
    fix_task_id: str
    fixed_head: str


def _prepare_v2_fix_replay(tmp_path: Path) -> _V2FixReplay:
    settings_db, hermes_db, request, settings = _activated_v2(
        tmp_path,
        flow=TaskFlow.BUILD_REVIEW,
    )
    worker_kwargs: dict[str, object] = {
        "settings_db": settings_db,
        "hermes_db": hermes_db,
        "hermes_path": "hermes",
        "worktree_root": tmp_path / "worktrees",
        "remote_repository": (
            lambda workspace, _remote: f"example/{workspace.name}"
        ),
    }
    initial_calls: list[tuple[str, ...]] = []
    run_project_task_flow_worker(
        **worker_kwargs,
        github=FakeGitHub(),
        create_card=lambda argv: initial_calls.append(tuple(argv)),
    )
    project = request.projects[0]
    root_call = _v2_call_for(initial_calls, project, TaskStep.BUILD.value)
    built_head = _commit_v2_worktree(root_call, filename="initial-build.txt")
    build_summary = _v2_build_summary(
        settings,
        project,
        built_commit=built_head,
    )
    root_task_id = "fix-chain-root"
    for index, call in enumerate(initial_calls, start=1):
        body = json.loads(call[call.index("--body") + 1])
        is_target = body["project"]["project_id"] == project.project_id
        _insert_v2_call(
            hermes_db,
            call,
            task_id=(root_task_id if is_target else f"waiting-root-{index}"),
            parent_id=None,
            summary=(build_summary if is_target else None),
            run_id=101,
        )

    github = FakeGitHub()
    pr_url = str(build_summary["pr_url"])
    github.prs[pr_url] = _v2_pr(project, built_head)
    review_calls: list[tuple[str, ...]] = []
    run_project_task_flow_worker(
        **worker_kwargs,
        github=github,
        create_card=lambda argv: review_calls.append(tuple(argv)),
    )
    review_call = _v2_call_for(review_calls, project, TaskStep.REVIEW.value)
    fix_notes = "Handle the missing edge case."
    review_summary: dict[str, object] = {
        "format_version": "forge-review-result/v1",
        "task_settings_hash": settings.task_settings_hash,
        "result": "changes_needed",
        "source_result_hash": source_result_hash(parse_build_result(build_summary)),
        "pr_url": pr_url,
        "reviewed_commit": built_head,
        "change_check": {
            "confirmed_work": ["initial-build.txt"],
            "problems": [fix_notes],
        },
        "requirements_check": {
            "completed": [],
            "missing": ["Each Project gets its own PR."],
        },
        "fix_notes": fix_notes,
    }
    review_task_id = "fix-chain-review"
    _insert_v2_call(
        hermes_db,
        review_call,
        task_id=review_task_id,
        parent_id=root_task_id,
        summary=review_summary,
        run_id=102,
    )

    fix_calls: list[tuple[str, ...]] = []
    run_project_task_flow_worker(
        **worker_kwargs,
        github=github,
        create_card=lambda argv: fix_calls.append(tuple(argv)),
    )
    fix_call = _v2_call_for(fix_calls, project, TaskStep.FIX.value)
    fixed_head = _commit_v2_worktree(fix_call, filename="fixed.txt")
    fix_task_id = "fix-chain-fix"
    proof = {
        "format_version": "forge-step-proof/v1",
        "tested_commit": fixed_head,
        "pr_url": pr_url,
        "fix_notes": fix_notes,
        "source_result_hash": source_result_hash(
            parse_review_result(review_summary)
        ),
        "source_run_id": 102,
        "source_task_id": review_task_id,
        "task_settings_hash": settings.task_settings_hash,
    }
    _insert_v2_call(
        hermes_db,
        fix_call,
        task_id=fix_task_id,
        parent_id=review_task_id,
        summary=proof,
        run_id=103,
    )
    github.prs[pr_url] = _v2_pr(project, fixed_head)
    return _V2FixReplay(
        settings_db=settings_db,
        hermes_db=hermes_db,
        project=project,
        worker_kwargs=worker_kwargs,
        github=github,
        fix_call=fix_call,
        fix_task_id=fix_task_id,
        fixed_head=fixed_head,
    )


def test_v2_fix_proof_replay_creates_new_build_at_proven_head(
    tmp_path: Path,
) -> None:
    chain = _prepare_v2_fix_replay(tmp_path)
    calls: list[tuple[str, ...]] = []

    run_project_task_flow_worker(
        **chain.worker_kwargs,
        github=chain.github,
        create_card=lambda argv: calls.append(tuple(argv)),
    )

    new_build = _v2_call_for(calls, chain.project, TaskStep.BUILD.value)
    body = json.loads(new_build[new_build.index("--body") + 1])
    assert new_build[new_build.index("--parent") + 1] == chain.fix_task_id
    assert body["head_commit"] == chain.fixed_head
    assert body["source_task_id"] == chain.fix_task_id
    assert (
        Path(new_build[new_build.index("--workspace") + 1].removeprefix("dir:"))
        == Path(chain.fix_call[chain.fix_call.index("--workspace") + 1].removeprefix("dir:"))
    )


def test_v2_fix_build_response_loss_replays_without_duplicate(
    tmp_path: Path,
) -> None:
    chain = _prepare_v2_fix_replay(tmp_path)
    lost_keys: list[str] = []

    def create_then_lose_response(argv: Sequence[str]) -> None:
        call = tuple(argv)
        key = call[call.index("--idempotency-key") + 1]
        lost_keys.append(key)
        _insert_v2_call(
            chain.hermes_db,
            call,
            task_id="response-lost-build",
            parent_id=chain.fix_task_id,
            summary=None,
        )
        raise RuntimeError("simulated Hermes response loss")

    with pytest.raises(RuntimeError, match="response loss"):
        run_project_task_flow_worker(
            **chain.worker_kwargs,
            github=chain.github,
            create_card=create_then_lose_response,
        )

    replay_writes: list[tuple[str, ...]] = []
    reports = run_project_task_flow_worker(
        **chain.worker_kwargs,
        github=chain.github,
        create_card=lambda argv: replay_writes.append(tuple(argv)),
    )

    assert len(lost_keys) == 1
    assert replay_writes == []
    target = next(
        report
        for report in reports
        if report.project_id == chain.project.project_id
    )
    assert target.status == "waiting"
    with sqlite3.connect(chain.hermes_db) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE idempotency_key = ?",
            (lost_keys[0],),
        ).fetchone()[0]
    assert count == 1


def test_v2_fix_replay_rejects_unrecorded_descendant_before_any_write(
    tmp_path: Path,
) -> None:
    chain = _prepare_v2_fix_replay(tmp_path)
    injected_head = _commit_v2_worktree(
        chain.fix_call,
        filename="unrecorded-after-fix.txt",
    )
    pr_url = f"https://github.com/{chain.project.repository}/pull/7"
    chain.github.prs[pr_url] = _v2_pr(chain.project, injected_head)
    database_before = chain.settings_db.read_bytes()
    writes: list[tuple[str, ...]] = []

    with pytest.raises(GateError, match="pull request"):
        run_project_task_flow_worker(
            **chain.worker_kwargs,
            github=chain.github,
            create_card=lambda argv: writes.append(tuple(argv)),
        )

    assert writes == []
    assert chain.settings_db.read_bytes() == database_before


def test_v2_worker_rejects_pull_request_for_another_project_repository(
    tmp_path: Path,
) -> None:
    settings_db, hermes_db, request, settings = _activated_v2(tmp_path)
    initial_calls: list[tuple[str, ...]] = []
    kwargs = {
        "settings_db": settings_db,
        "hermes_db": hermes_db,
        "hermes_path": "hermes",
        "worktree_root": tmp_path / "worktrees",
        "remote_repository": lambda workspace, _remote: f"example/{workspace.name}",
    }
    run_project_task_flow_worker(
        **kwargs,
        github=FakeGitHub(),
        create_card=lambda argv: initial_calls.append(tuple(argv)),
    )
    project = request.projects[0]
    call = next(item for item in initial_calls if project.repository in item[2])
    _insert_v2_root(
        hermes_db,
        call,
        summary=_v2_build_summary(
            settings,
            project,
            pr_repository=request.projects[1].repository,
        ),
    )

    with pytest.raises(GateError, match="pull request.*Project repository"):
        run_project_task_flow_worker(
            **kwargs,
            github=FakeGitHub(),
            create_card=lambda _argv: None,
        )


def test_one_v2_project_completion_does_not_complete_parent_task(
    tmp_path: Path,
) -> None:
    settings_db, hermes_db, request, settings = _activated_v2(tmp_path)
    initial_calls: list[tuple[str, ...]] = []
    kwargs = {
        "settings_db": settings_db,
        "hermes_db": hermes_db,
        "hermes_path": "hermes",
        "worktree_root": tmp_path / "worktrees",
        "remote_repository": lambda workspace, _remote: f"example/{workspace.name}",
    }
    run_project_task_flow_worker(
        **kwargs,
        github=FakeGitHub(),
        create_card=lambda argv: initial_calls.append(tuple(argv)),
    )
    project = request.projects[0]
    call = next(item for item in initial_calls if project.repository in item[2])
    built_commit = _commit_v2_worktree(call, filename="built.txt")
    summary = _v2_build_summary(
        settings,
        project,
        built_commit=built_commit,
    )
    _insert_v2_root(hermes_db, call, summary=summary)
    github = FakeGitHub()
    github.prs[summary["pr_url"]] = PullRequestWriteState(
        pr_url=summary["pr_url"],
        repository=project.repository,
        pr_number=7,
        base_commit=project.base_commit,
        base_ref=project.base_branch,
        head_commit=built_commit,
        is_open=True,
        is_merged=False,
        merged_commit=None,
        merged_base_commit=None,
        merged_head_commit=None,
    )

    reports = run_project_task_flow_worker(
        **kwargs,
        github=github,
        create_card=lambda _argv: None,
    )

    completed = next(report for report in reports if report.project_id == project.project_id)
    assert completed.status == "ready_to_merge"
    with sqlite3.connect(settings_db) as connection:
        terminal_count = connection.execute(
            """
            SELECT COUNT(*) FROM task_events
            WHERE request_id = ?
              AND event_type IN ('merged', 'partially_merged', 'cancelled')
            """,
            (request.request_id,),
        ).fetchone()[0]
    assert terminal_count == 0


def test_v2_build_completion_creates_review_for_same_project_and_worktree(
    tmp_path: Path,
) -> None:
    settings_db, hermes_db, request, settings = _activated_v2(
        tmp_path,
        flow=TaskFlow.BUILD_REVIEW,
    )
    kwargs = {
        "settings_db": settings_db,
        "hermes_db": hermes_db,
        "hermes_path": "hermes",
        "worktree_root": tmp_path / "worktrees",
        "remote_repository": lambda workspace, _remote: f"example/{workspace.name}",
    }
    roots: list[tuple[str, ...]] = []
    run_project_task_flow_worker(
        **kwargs,
        github=FakeGitHub(),
        create_card=lambda argv: roots.append(tuple(argv)),
    )
    project = request.projects[0]
    root = next(item for item in roots if project.repository in item[2])
    built_commit = _commit_v2_worktree(root, filename="built.txt")
    summary = _v2_build_summary(
        settings,
        project,
        built_commit=built_commit,
    )
    _insert_v2_root(hermes_db, root, summary=summary, task_id="project-one-root")
    github = FakeGitHub()
    github.prs[summary["pr_url"]] = PullRequestWriteState(
        pr_url=summary["pr_url"],
        repository=project.repository,
        pr_number=7,
        base_commit=project.base_commit,
        base_ref=project.base_branch,
        head_commit=built_commit,
        is_open=True,
        is_merged=False,
        merged_commit=None,
        merged_base_commit=None,
        merged_head_commit=None,
    )
    children: list[tuple[str, ...]] = []

    run_project_task_flow_worker(
        **kwargs,
        github=github,
        create_card=lambda argv: children.append(tuple(argv)),
    )

    review = next(item for item in children if ":review:" in item[item.index("--idempotency-key") + 1])
    assert review[review.index("--parent") + 1] == "project-one-root"
    assert review[review.index("--workspace") + 1] == root[root.index("--workspace") + 1]
    body = json.loads(review[review.index("--body") + 1])
    assert body["project"] == json.loads(root[root.index("--body") + 1])["project"]
    assert body["step"] == "review"


def test_v2_worker_rejects_unrecorded_descendant_before_review_card_write(
    tmp_path: Path,
) -> None:
    settings_db, hermes_db, request, settings = _activated_v2(
        tmp_path,
        flow=TaskFlow.BUILD_REVIEW,
    )
    kwargs = {
        "settings_db": settings_db,
        "hermes_db": hermes_db,
        "hermes_path": "hermes",
        "worktree_root": tmp_path / "worktrees",
        "remote_repository": lambda workspace, _remote: f"example/{workspace.name}",
    }
    roots: list[tuple[str, ...]] = []
    run_project_task_flow_worker(
        **kwargs,
        github=FakeGitHub(),
        create_card=lambda argv: roots.append(tuple(argv)),
    )
    project = request.projects[0]
    root = next(item for item in roots if project.repository in item[2])
    recorded_head = _commit_v2_worktree(root, filename="recorded.txt")
    summary = _v2_build_summary(
        settings,
        project,
        built_commit=recorded_head,
    )
    _insert_v2_root(
        hermes_db,
        root,
        summary=summary,
        task_id="project-one-root",
    )
    injected_head = _commit_v2_worktree(root, filename="injected.txt")
    assert injected_head != recorded_head
    github = FakeGitHub()
    github.prs[summary["pr_url"]] = PullRequestWriteState(
        pr_url=summary["pr_url"],
        repository=project.repository,
        pr_number=7,
        base_commit=project.base_commit,
        base_ref=project.base_branch,
        head_commit=recorded_head,
        is_open=True,
        is_merged=False,
        merged_commit=None,
        merged_base_commit=None,
        merged_head_commit=None,
    )
    writes: list[tuple[str, ...]] = []

    with pytest.raises(GateError, match="recorded result HEAD"):
        run_project_task_flow_worker(
            **kwargs,
            github=github,
            create_card=lambda argv: writes.append(tuple(argv)),
        )

    assert writes == []
