from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from forge.ops.hermes import (
    GateError,
    HermesStore,
    ProjectTaskCardSpec,
    RootTaskCardSpec,
    build_create_argv,
    build_project_create_argv,
    build_root_create_argv,
    parse_project_task_card_key,
    parse_task_card_key,
    project_step_card_key,
    project_task_card_key,
    step_card_key,
    task_card_key,
)
from forge.ops.task_flow import TaskCardSpec, TaskStep


TASK_SETTINGS_HASH = "a" * 64
SOURCE_RESULT_HASH = "b" * 64
ROOT_KEY = f"forge-task:owner/repo#7:{TASK_SETTINGS_HASH[:16]}"


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
        key=ROOT_KEY,
    )
    _insert_task(
        connection,
        task_id="review",
        key=f"forge-step:owner/repo#7:review:{SOURCE_RESULT_HASH[:16]}",
    )
    connection.execute(
        "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
        ("root", "review"),
    )
    connection.commit()
    connection.close()

    tasks = HermesStore(db_path).list_task_cards()

    assert [task.task_id for task in tasks] == ["review", "root"]
    review = next(task for task in tasks if task.task_id == "review")
    assert review.parent_id == "root"


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

    HermesStore(db_path).list_task_cards()

    assert calls
    assert str(calls[0][0]).endswith("?mode=ro")
    assert calls[0][1] is True


def test_store_rejects_duplicate_pipeline_idempotency_key(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    connection = _create_live_schema(db_path)
    key = ROOT_KEY
    _insert_task(connection, task_id="first", key=key)
    _insert_task(connection, task_id="second", key=key)
    connection.commit()
    connection.close()

    with pytest.raises(GateError, match="duplicate.*idempotency"):
        HermesStore(db_path).list_task_cards()


def test_store_reads_only_new_task_and_step_keys(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    connection = _create_live_schema(db_path)
    _insert_task(
        connection,
        task_id="root",
        key=ROOT_KEY,
    )
    _insert_task(connection, task_id="old-root", key="github-issue:owner/repo#7")
    _insert_task(
        connection,
        task_id="old-stage",
        key=f"forge-stage:owner/repo#7:reviewer:{SOURCE_RESULT_HASH[:16]}",
    )
    connection.commit()
    connection.close()

    tasks = HermesStore(db_path).list_task_cards()

    assert [task.task_id for task in tasks] == ["root"]


def test_store_rejects_malformed_new_task_key(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    connection = _create_live_schema(db_path)
    _insert_task(
        connection,
        task_id="malformed",
        key="forge-task:owner/repo#7:short",
    )
    connection.commit()
    connection.close()

    with pytest.raises(GateError, match="malformed.*identity"):
        HermesStore(db_path).list_task_cards()


@pytest.mark.parametrize(
    "old_key",
    [
        "github-issue:owner/repo#7",
        f"forge-stage:owner/repo#7:reviewer:{SOURCE_RESULT_HASH[:16]}",
        f"forge-step:owner/repo#7:critic:{SOURCE_RESULT_HASH[:16]}",
    ],
)
def test_card_key_parser_explicitly_rejects_old_keys(old_key: str) -> None:
    with pytest.raises(GateError, match="new forge-task or forge-step"):
        parse_task_card_key(old_key)


def test_card_key_builders_use_full_hashes_and_exact_new_names() -> None:
    assert task_card_key("owner/repo", 7, TASK_SETTINGS_HASH) == ROOT_KEY
    assert step_card_key(
        "owner/repo", 7, TaskStep.DEEP_CHECK, SOURCE_RESULT_HASH
    ) == f"forge-step:owner/repo#7:deep_check:{SOURCE_RESULT_HASH[:16]}"


def test_latest_completed_run_uses_newest_run_and_parses_json(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    connection = _create_live_schema(db_path)
    _insert_task(
        connection,
        task_id="root",
        key=ROOT_KEY,
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


def test_completed_run_readers_accept_synthetic_manual_completion(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "kanban.db"
    connection = _create_live_schema(db_path)
    _insert_task(
        connection,
        task_id="root",
        key="forge-task:owner/repo#7:aaaaaaaaaaaaaaaa",
    )
    connection.execute(
        """
        INSERT INTO task_runs (id, task_id, status, outcome, summary, metadata)
        VALUES (1, 'root', 'completed', 'completed', '{"run": 1}', '{}')
        """
    )
    connection.commit()
    connection.close()

    store = HermesStore(db_path)

    assert [run.run_id for run in store.completed_runs("root")] == [1]
    assert store.latest_completed_run("root").run_id == 1


def test_latest_completed_run_rejects_non_object_json(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    connection = _create_live_schema(db_path)
    _insert_task(
        connection,
        task_id="root",
        key=ROOT_KEY,
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


def test_completed_run_accepts_official_null_metadata_as_empty_object(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "kanban.db"
    connection = _create_live_schema(db_path)
    _insert_task(connection, task_id="root", key=ROOT_KEY)
    connection.execute(
        """
        INSERT INTO task_runs (id, task_id, status, outcome, summary, metadata)
        VALUES (1, 'root', 'done', 'completed', '{}', NULL)
        """
    )
    connection.commit()
    connection.close()

    assert HermesStore(db_path).completed_runs("root")[0].metadata == {}


def test_completed_run_rejects_duplicate_json_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    connection = _create_live_schema(db_path)
    _insert_task(connection, task_id="root", key=ROOT_KEY)
    connection.execute(
        """
        INSERT INTO task_runs (id, task_id, status, outcome, summary, metadata)
        VALUES (1, 'root', 'done', 'completed', '{"run":1,"run":2}', NULL)
        """
    )
    connection.commit()
    connection.close()

    with pytest.raises(GateError, match="duplicate.*run"):
        HermesStore(db_path).completed_runs("root")


def test_task_create_argv_binds_parent_role_skill_and_idempotency() -> None:
    spec = TaskCardSpec(
        step=TaskStep.REVIEW,
        title="Forge review: owner/repo#7",
        body="```json\n{}\n```",
        parent_id="root",
        skill="review-result",
        idempotency_key=(
            f"forge-step:owner/repo#7:review:{SOURCE_RESULT_HASH[:16]}"
        ),
    )

    argv = build_create_argv(spec, "dir:/home/user/work/repo")

    assert argv[:3] == ("kanban", "create", spec.title)
    assert argv[argv.index("--parent") + 1] == "root"
    assert argv[argv.index("--assignee") + 1] == "reviewer"
    assert argv[argv.index("--skill") + 1] == "review-result"
    assert argv[argv.index("--idempotency-key") + 1] == spec.idempotency_key
    assert argv[argv.index("--max-retries") + 1] == "4"


def test_task_create_argv_uses_explicit_repository_workspace() -> None:
    spec = TaskCardSpec(
        step=TaskStep.DEEP_CHECK,
        title="Forge deep check: owner/repo#7",
        body="```json\n{}\n```",
        parent_id="review",
        skill="deep-check-result",
        idempotency_key=(
            f"forge-step:owner/repo#7:deep_check:{SOURCE_RESULT_HASH[:16]}"
        ),
    )

    argv = build_create_argv(spec, workspace="dir:/home/user/work/repo")

    assert argv[argv.index("--workspace") + 1] == "dir:/home/user/work/repo"


def test_root_task_create_argv_is_builder_without_parent() -> None:
    spec = RootTaskCardSpec(
        title="Build Task: owner/repo#7",
        body='{"format_version":"forge-task-card/v1"}',
        idempotency_key=ROOT_KEY,
    )

    argv = build_root_create_argv(spec, "dir:/home/user/work/repo")

    assert argv[:3] == ("kanban", "create", spec.title)
    assert argv[argv.index("--assignee") + 1] == "builder"
    assert argv[argv.index("--skill") + 1] == "build-task"
    assert argv[argv.index("--idempotency-key") + 1] == ROOT_KEY
    assert "--parent" not in argv


def test_v2_card_keys_bind_request_project_and_step_without_v1_aliasing() -> None:
    request_id = "4485be21-2a8f-41b8-a2a2-e25722df284e"
    project_id = "c" * 64

    root = project_task_card_key(request_id, project_id)
    review = project_step_card_key(
        request_id,
        project_id,
        TaskStep.REVIEW,
        SOURCE_RESULT_HASH,
    )

    assert root == f"forge-task-v2:{request_id}:{project_id}:build"
    assert review == (
        f"forge-step-v2:{request_id}:{project_id}:review:"
        f"{SOURCE_RESULT_HASH[:16]}"
    )
    assert parse_project_task_card_key(root).step is TaskStep.BUILD
    assert parse_project_task_card_key(review).project_id == project_id
    with pytest.raises(GateError, match="new forge-task or forge-step"):
        parse_task_card_key(root)


def test_v2_card_argv_uses_project_worktree_and_keeps_exact_snapshot_body() -> None:
    request_id = "4485be21-2a8f-41b8-a2a2-e25722df284e"
    project_id = "c" * 64
    body = json.dumps(
        {
            "format_version": "forge-project-card/v2",
            "project": {
                "project_id": project_id,
                "repository": "owner/actual-project",
                "workspace": "/source/actual-project",
                "remote_name": "origin",
                "base_branch": "main",
                "base_commit": "d" * 40,
                "host_id": "d6f70d5d-6482-45f5-80d2-219ec2ad4d19",
            },
            "request_id": request_id,
            "step": "build",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    spec = ProjectTaskCardSpec(
        step=TaskStep.BUILD,
        title="Build Project: owner/actual-project",
        body=body,
        idempotency_key=project_task_card_key(request_id, project_id),
        parent_id=None,
        skill="build-task",
    )

    argv = build_project_create_argv(spec, Path("C:/tasks/project-worktree"))

    assert argv[argv.index("--workspace") + 1] == "dir:C:/tasks/project-worktree"
    assert argv[argv.index("--body") + 1] == body
    assert argv[argv.index("--idempotency-key") + 1] == spec.idempotency_key
    assert "--parent" not in argv
