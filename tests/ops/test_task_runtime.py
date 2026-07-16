from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import forge.ops.task_runtime as task_runtime_module
from forge.ops.github import PullRequestWriteState
from forge.ops.contracts import parse_build_result, source_result_hash
from forge.ops.github_merge import BranchRefreshResult
from forge.ops.hermes import GateError, task_card_key
from forge.ops.task_options import MergeMode, TaskFlow
from forge.ops.task_outbox import TaskOutbox
from forge.ops.task_runtime import (
    TaskFlowSnapshot,
    label_for_snapshot,
    load_ready_to_merge_snapshots,
    load_task_flow_snapshots,
    next_card_spec,
    record_branch_refresh_result,
    run_task_flow_worker,
)
from forge.ops.task_service import (
    TaskCreationRequest,
    TaskIssue,
    TaskService,
    read_task_marker,
)
from forge.ops.task_settings import TaskContent, TaskSettingsStore


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
