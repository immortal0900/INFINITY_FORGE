from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from forge.ops.contracts import PipelineStage
from forge.ops.github import GitHubClient
from forge.ops.hermes import GateError, HermesStore, build_create_argv
from forge.ops.stage_reconciler import StageCardSpec


PR_URL = "https://github.com/owner/repo/pull/17"
HEAD_SHA = "a" * 40


def _create_live_schema(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL,
            workspace_kind TEXT,
            workspace_path TEXT,
            project_id TEXT,
            result TEXT,
            idempotency_key TEXT,
            skills TEXT,
            max_retries INTEGER,
            goal TEXT
        );
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            profile TEXT,
            status TEXT NOT NULL,
            started_at TEXT,
            ended_at TEXT,
            outcome TEXT,
            summary TEXT,
            metadata TEXT,
            error TEXT
        );
        CREATE TABLE task_links (
            parent_id TEXT NOT NULL,
            child_id TEXT NOT NULL
        );
        """
    )
    return connection


def _insert_task(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    key: str,
    status: str = "done",
    body: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO tasks (id, title, body, status, idempotency_key)
        VALUES (?, ?, ?, ?, ?)
        """,
        (task_id, f"task {task_id}", body, status, key),
    )


def test_store_reads_live_schema_and_resolves_parent(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    connection = _create_live_schema(db_path)
    _insert_task(
        connection,
        task_id="root",
        key="github-issue:owner/repo#7",
    )
    _insert_task(
        connection,
        task_id="reviewer",
        key=f"forge-stage:owner/repo#7:reviewer:{'b' * 16}",
    )
    connection.execute(
        "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
        ("root", "reviewer"),
    )
    connection.commit()
    connection.close()

    tasks = HermesStore(db_path).list_pipeline_tasks()

    assert [task.task_id for task in tasks] == ["reviewer", "root"]
    reviewer = next(task for task in tasks if task.task_id == "reviewer")
    assert reviewer.parent_id == "root"


def test_store_opens_sqlite_with_read_only_uri(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "kanban.db"
    connection = _create_live_schema(db_path)
    connection.close()
    original_connect = sqlite3.connect
    calls: list[tuple[object, bool]] = []

    def recording_connect(database: object, *args: object, **kwargs: object):
        calls.append((database, kwargs.get("uri") is True))
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr("forge.ops.hermes.sqlite3.connect", recording_connect)

    HermesStore(db_path).list_pipeline_tasks()

    assert calls
    assert str(calls[0][0]).endswith("?mode=ro")
    assert calls[0][1] is True


def test_store_rejects_duplicate_pipeline_idempotency_key(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    connection = _create_live_schema(db_path)
    key = "github-issue:owner/repo#7"
    _insert_task(connection, task_id="first", key=key)
    _insert_task(connection, task_id="second", key=key)
    connection.commit()
    connection.close()

    with pytest.raises(GateError, match="duplicate.*idempotency"):
        HermesStore(db_path).list_pipeline_tasks()


def test_store_ignores_only_known_spec_002_legacy_stage_keys(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    connection = _create_live_schema(db_path)
    _insert_task(
        connection,
        task_id="root",
        key="github-issue:owner/repo#7",
    )
    for suffix in ("exec", "review", "critic"):
        _insert_task(
            connection,
            task_id=f"legacy-{suffix}",
            key=f"github-issue:owner/repo#8-{suffix}",
        )
    connection.commit()
    connection.close()

    store = HermesStore(db_path)
    tasks = store.list_pipeline_tasks()

    assert [task.task_id for task in tasks] == ["root"]
    assert store.ignored_legacy_count == 3


def test_store_rejects_unknown_malformed_pipeline_key(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    connection = _create_live_schema(db_path)
    _insert_task(
        connection,
        task_id="malformed",
        key="github-issue:owner/repo#7-other",
    )
    connection.commit()
    connection.close()

    with pytest.raises(GateError, match="malformed.*identity"):
        HermesStore(db_path).list_pipeline_tasks()


def test_latest_completed_run_uses_newest_run_and_parses_json(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    connection = _create_live_schema(db_path)
    _insert_task(
        connection,
        task_id="root",
        key="github-issue:owner/repo#7",
    )
    for run_id, status, outcome in (
        (1, "done", "completed"),
        (2, "running", None),
        (3, "done", "completed"),
        (4, "blocked", "blocked"),
    ):
        connection.execute(
            """
            INSERT INTO task_runs
                (id, task_id, status, outcome, summary, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                "root",
                status,
                outcome,
                json.dumps({"run": run_id}),
                json.dumps({"worker_session_id": f"session-{run_id}"}),
            ),
        )
    connection.commit()
    connection.close()

    run = HermesStore(db_path).latest_completed_run("root")

    assert run.run_id == 3
    assert run.status == "completed"
    assert run.outcome == "success"
    assert run.summary == {"run": 3}
    assert run.metadata == {"worker_session_id": "session-3"}


def test_latest_completed_run_rejects_non_object_json(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    connection = _create_live_schema(db_path)
    _insert_task(
        connection,
        task_id="root",
        key="github-issue:owner/repo#7",
    )
    connection.execute(
        """
        INSERT INTO task_runs (id, task_id, status, outcome, summary, metadata)
        VALUES (1, 'root', 'done', 'completed', '[]', '{}')
        """
    )
    connection.commit()
    connection.close()

    with pytest.raises(GateError, match="summary.*object"):
        HermesStore(db_path).latest_completed_run("root")


def test_stage_create_argv_binds_parent_skill_and_idempotency() -> None:
    spec = StageCardSpec(
        target_stage=PipelineStage.REVIEWER,
        title="Forge reviewer: owner/repo#7",
        body="```json\n{}\n```",
        parent_id="root",
        assignee="reviewer",
        skill="reviewer-verdict",
        idempotency_key=f"forge-stage:owner/repo#7:reviewer:{'b' * 16}",
    )

    argv = build_create_argv(spec, "dir:/home/user/work/repo")

    assert argv[:3] == ("kanban", "create", spec.title)
    assert argv[argv.index("--parent") + 1] == "root"
    assert argv[argv.index("--skill") + 1] == "reviewer-verdict"
    assert argv[argv.index("--idempotency-key") + 1] == spec.idempotency_key
    assert argv[argv.index("--max-retries") + 1] == "4"


def test_stage_create_argv_uses_explicit_repository_workspace() -> None:
    spec = StageCardSpec(
        target_stage=PipelineStage.CRITIC,
        title="Forge critic: owner/repo#7",
        body="```json\n{}\n```",
        parent_id="reviewer",
        assignee="critic",
        skill="critic-adversarial",
        idempotency_key=f"forge-stage:owner/repo#7:critic:{'c' * 16}",
    )

    argv = build_create_argv(spec, workspace="dir:/home/user/work/repo")

    assert argv[argv.index("--workspace") + 1] == "dir:/home/user/work/repo"


class _GitHubRunner:
    def __init__(self, *, checks: list[dict[str, object]]) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._checks = checks

    def __call__(
        self,
        argv: list[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(argv))
        endpoint = argv[-1]
        if endpoint == "repos/owner/repo/pulls/17":
            payload = {
                "html_url": PR_URL,
                "number": 17,
                "state": "open",
                "draft": False,
                "head": {"sha": HEAD_SHA},
            }
        else:
            payload = {"check_runs": self._checks}
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")


def _api_check(
    *, status: str = "completed", conclusion: str | None = "success"
) -> dict[str, object]:
    return {
        "name": "eval",
        "status": status,
        "conclusion": conclusion,
        "head_sha": HEAD_SHA,
    }


def test_github_reads_checks_from_requested_current_head() -> None:
    runner = _GitHubRunner(checks=[_api_check()])

    snapshot = GitHubClient("/usr/bin/gh", runner=runner).get_pr_snapshot(
        PR_URL, ("eval",)
    )

    assert snapshot.head_sha == HEAD_SHA
    assert snapshot.pr_number == 17
    assert snapshot.checks[0].conclusion == "success"
    assert runner.calls[1][-1] == (
        f"repos/owner/repo/commits/{HEAD_SHA}/check-runs?per_page=100"
    )


@pytest.mark.parametrize("count", [0, 2])
def test_github_requires_exactly_one_named_check(count: int) -> None:
    runner = _GitHubRunner(checks=[_api_check() for _ in range(count)])

    with pytest.raises(GateError, match="eval.*exactly one"):
        GitHubClient("gh", runner=runner).get_pr_snapshot(PR_URL, ("eval",))


@pytest.mark.parametrize("status", ["queued", "in_progress"])
def test_github_preserves_pending_check_as_not_successful(status: str) -> None:
    runner = _GitHubRunner(checks=[_api_check(status=status, conclusion=None)])

    snapshot = GitHubClient("gh", runner=runner).get_pr_snapshot(PR_URL, ("eval",))

    assert snapshot.checks[0].status == status
    assert snapshot.checks[0].conclusion is None
