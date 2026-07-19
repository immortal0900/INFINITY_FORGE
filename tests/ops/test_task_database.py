from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import forge.ops.task_database as task_database_module
from forge.ops.task_database import (
    TASK_DATABASE_SCHEMA_VERSION,
    TASK_DATABASE_TABLES,
    TaskDatabase,
    TaskDatabaseError,
)
from forge.ops.task_settings import TaskSettingsStatus, TaskSettingsStore


REQUEST_ID = "9f7453ce-36ec-4e8e-9dfa-bb159b58c19b"
CONTENT_HASH = "5d5bd28e8ff50a52de088e27c00e1a6f96e3b77b5c9b0d1211e92946cebc034b"
SETTINGS_HASH = "ce04fe1beb53b5b1e7c1a6ed5321a4bb025aa93cd3b1ce3f18ef8c2a90f0c2f1"

EXPECTED_TABLES = {
    "task_settings",
    "task_settings_events",
    "task_branch_refresh_intents",
    "task_requests",
    "task_settings_v2",
    "task_events",
    "task_projects",
    "task_messages",
    "task_message_events",
    "task_revision_requests",
    "task_stop_requests",
    "task_session_bindings",
    "task_access",
    "surface_events",
    "task_runtime_runs",
}


def _create_seeded_v1_database(
    path: Path,
    *,
    stored_format_version: str = "forge-task-settings/v1",
) -> None:
    with sqlite3.connect(path) as connection:
        schema = (
            """
            CREATE TABLE task_settings (
                request_id TEXT PRIMARY KEY,
                format_version TEXT NOT NULL
                    CHECK (format_version = 'forge-task-settings/v1'),
                repository TEXT NOT NULL,
                mode TEXT NOT NULL CHECK (mode = 'task'),
                task_content_hash TEXT NOT NULL,
                task_flow TEXT NOT NULL CHECK (
                    task_flow IN (
                        'build',
                        'build_review',
                        'build_review_deep_check'
                    )
                ),
                merge_mode TEXT NOT NULL CHECK (
                    merge_mode IN ('manual', 'safe_auto', 'full_auto')
                ),
                confirmed_by TEXT NOT NULL,
                confirmed_at TEXT NOT NULL,
                auto_merge_expires_at TEXT
            );
            CREATE TABLE task_settings_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL
                    REFERENCES task_settings(request_id),
                event_type TEXT NOT NULL CHECK (
                    event_type IN (
                        'prepared',
                        'issue_bound',
                        'active',
                        'cancelled',
                        'expired',
                        'merged'
                    )
                ),
                occurred_at TEXT NOT NULL,
                issue_number INTEGER,
                task_settings_hash TEXT,
                UNIQUE (request_id, event_type),
                CHECK (
                    (
                        event_type = 'issue_bound'
                        AND issue_number IS NOT NULL
                        AND task_settings_hash IS NOT NULL
                    )
                    OR
                    (
                        event_type <> 'issue_bound'
                        AND issue_number IS NULL
                        AND task_settings_hash IS NULL
                    )
                )
            );
            CREATE TABLE task_branch_refresh_intents (
                request_id TEXT NOT NULL
                    REFERENCES task_settings(request_id),
                refresh_number INTEGER NOT NULL
                    CHECK (refresh_number BETWEEN 1 AND 3),
                pr_url TEXT NOT NULL,
                expected_base_commit TEXT NOT NULL,
                expected_head_commit TEXT NOT NULL,
                created_at TEXT NOT NULL,
                current_base_commit TEXT,
                current_head_commit TEXT,
                completed_at TEXT,
                PRIMARY KEY (request_id, refresh_number),
                UNIQUE (request_id, expected_base_commit, expected_head_commit),
                CHECK (
                    (
                        current_base_commit IS NULL
                        AND current_head_commit IS NULL
                        AND completed_at IS NULL
                    )
                    OR
                    (
                        current_base_commit IS NOT NULL
                        AND current_head_commit IS NOT NULL
                        AND completed_at IS NOT NULL
                    )
                ),
                CHECK (
                    current_base_commit IS NULL
                    OR current_base_commit <> expected_base_commit
                    OR current_head_commit <> expected_head_commit
                )
            );
            CREATE UNIQUE INDEX task_settings_one_terminal_event
                ON task_settings_events (request_id)
                WHERE event_type IN ('cancelled', 'expired', 'merged');
            PRAGMA user_version = 1;
            """
        )
        schema = schema.replace(
            "'forge-task-settings/v1'",
            f"'{stored_format_version}'",
        )
        connection.executescript(schema)
        connection.execute(
            """
            INSERT INTO task_settings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                REQUEST_ID,
                stored_format_version,
                "openai/infinity-forge",
                "task",
                CONTENT_HASH,
                "build_review",
                "safe_auto",
                "hermes-user-7",
                "2026-07-16T09:30:00Z",
                "2026-07-16T21:30:00Z",
            ),
        )
        connection.executemany(
            """
            INSERT INTO task_settings_events (
                request_id,
                event_type,
                occurred_at,
                issue_number,
                task_settings_hash
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                (REQUEST_ID, "prepared", "2026-07-16T09:30:00Z", None, None),
                (
                    REQUEST_ID,
                    "issue_bound",
                    "2026-07-16T09:31:00Z",
                    42,
                    SETTINGS_HASH,
                ),
                (REQUEST_ID, "active", "2026-07-16T09:32:00Z", None, None),
            ),
        )


def _v1_rows(path: Path) -> bytes:
    with sqlite3.connect(path) as connection:
        payload = {
            table: connection.execute(
                f"SELECT * FROM {table} ORDER BY rowid"  # noqa: S608 - fixed table set
            ).fetchall()
            for table in (
                "task_settings",
                "task_settings_events",
                "task_branch_refresh_intents",
            )
        }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _schema_objects(path: Path) -> set[tuple[str, str]]:
    with sqlite3.connect(path) as connection:
        return {
            (object_type, name)
            for object_type, name in connection.execute(
                """
                SELECT type, name
                FROM sqlite_master
                WHERE type IN ('table', 'index', 'trigger', 'view')
                  AND name NOT LIKE 'sqlite_%'
                """
            )
        }


def _save_surface_event(database: TaskDatabase, event_id: str) -> None:
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO surface_events (
                source_event_id,
                subject_id,
                session_id,
                surface,
                payload_hash,
                state,
                received_at,
                retention_until
            ) VALUES (?, 'user-1', 'session-1', 'cli', ?, 'received', 'now', 'later')
            """,
            (event_id, "a" * 64),
        )


def _replace_surface_events(database: TaskDatabase, event_id: str) -> None:
    with database.transaction() as connection:
        connection.execute("DELETE FROM surface_events")
    _save_surface_event(database, event_id)


def _surface_event_ids(database: TaskDatabase) -> list[str]:
    with closing(database.connect()) as connection:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT source_event_id FROM surface_events ORDER BY source_event_id"
            )
        ]


def _subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    repository_root = str(Path(__file__).resolve().parents[2])
    existing_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        f"{repository_root}{os.pathsep}{existing_path}"
        if existing_path
        else repository_root
    )
    return environment


def _leave_committed_wal(database_path: Path, event_id: str) -> None:
    script = '''
import os
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
connection.execute("PRAGMA wal_autocheckpoint = 0")
connection.execute("BEGIN IMMEDIATE")
connection.execute("DELETE FROM surface_events")
connection.execute(
    """
    INSERT INTO surface_events (
        source_event_id,
        subject_id,
        session_id,
        surface,
        payload_hash,
        state,
        received_at,
        retention_until
    ) VALUES (?, 'user-1', 'session-1', 'cli', ?, 'received', 'now', 'later')
    """,
    (sys.argv[2], "b" * 64),
)
connection.commit()
os._exit(0)
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(database_path), event_id],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        env=_subprocess_environment(),
    )
    assert result.returncode == 0, result.stderr
    wal_path = Path(f"{database_path}-wal")
    assert wal_path.is_file()
    assert wal_path.stat().st_size > 32
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{database_path}{suffix}")
        if sidecar.exists():
            task_database_module._apply_owner_only_permissions(sidecar)
            assert task_database_module._verify_owner_only_permissions(sidecar)


def _database_with_abrupt_wal(
    tmp_path: Path,
) -> tuple[TaskDatabase, Path]:
    database = TaskDatabase(tmp_path / "task-settings.db")
    _save_surface_event(database, "archived-event")
    backup_path = database.backup(tmp_path / "backup.db")
    _leave_committed_wal(database.database_path, "live-wal-event")
    return database, backup_path


def test_seeded_v1_migration_preserves_rows_events_and_public_readback(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "task-settings.db"
    _create_seeded_v1_database(database_path)
    before = _v1_rows(database_path)

    TaskDatabase(database_path)

    assert _v1_rows(database_path) == before
    active = TaskSettingsStore(database_path).get_active(REQUEST_ID)
    assert active is not None
    assert active.status is TaskSettingsStatus.ACTIVE
    assert active.issue_number == 42
    assert active.task_settings_hash == SETTINGS_HASH
    events = TaskSettingsStore(database_path).list_events(REQUEST_ID)
    assert [event.event_type.value for event in events] == [
        "prepared",
        "issue_bound",
        "active",
    ]


def test_migration_installs_the_exact_v2_object_set(tmp_path: Path) -> None:
    database_path = tmp_path / "task-settings.db"
    database = TaskDatabase(database_path)

    with sqlite3.connect(database_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert version == TASK_DATABASE_SCHEMA_VERSION == 2
    assert TASK_DATABASE_TABLES == frozenset(EXPECTED_TABLES)
    assert _schema_objects(database_path) == {
        *(('table', table) for table in EXPECTED_TABLES),
        ("index", "task_settings_one_terminal_event"),
    }
    assert foreign_key_errors == []
    assert database.quick_check() == "ok"


def test_migration_rejects_literal_case_drift_in_the_v1_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "task-settings.db"
    _create_seeded_v1_database(
        database_path,
        stored_format_version="FORGE-TASK-SETTINGS/V1",
    )

    with pytest.raises(TaskDatabaseError, match="schema does not match"):
        TaskDatabase(database_path)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_mid_ddl_failure_rolls_back_and_retry_migrates_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "task-settings.db"
    _create_seeded_v1_database(database_path)
    before = _v1_rows(database_path)
    v1_objects = _schema_objects(database_path)
    original = task_database_module._V2_SCHEMA_STATEMENTS
    monkeypatch.setattr(
        task_database_module,
        "_V2_SCHEMA_STATEMENTS",
        (
            original[0],
            "CREATE TABLE migration_must_roll_back (value TEXT)",
            "forced invalid migration statement",
            *original[1:],
        ),
    )

    with pytest.raises(TaskDatabaseError, match="database operation failed"):
        TaskDatabase(database_path)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    assert _schema_objects(database_path) == v1_objects
    assert _v1_rows(database_path) == before

    monkeypatch.setattr(task_database_module, "_V2_SCHEMA_STATEMENTS", original)
    TaskDatabase(database_path)
    TaskDatabase(database_path)

    assert _schema_objects(database_path) == {
        *(('table', table) for table in EXPECTED_TABLES),
        ("index", "task_settings_one_terminal_event"),
    }
    assert _v1_rows(database_path) == before


def test_shared_transaction_facade_commits_and_rolls_back(tmp_path: Path) -> None:
    database = TaskDatabase(tmp_path / "task-settings.db")

    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO surface_events (
                source_event_id,
                subject_id,
                session_id,
                surface,
                payload_hash,
                state,
                received_at,
                retention_until
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "event-1",
                "user-1",
                "session-1",
                "cli",
                "a" * 64,
                "received",
                "now",
                "later",
            ),
        )

    with pytest.raises(RuntimeError, match="rollback"):
        with database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO surface_events (
                    source_event_id,
                    subject_id,
                    session_id,
                    surface,
                    payload_hash,
                    state,
                    received_at,
                    retention_until
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "event-2",
                    "user-1",
                    "session-1",
                    "cli",
                    "b" * 64,
                    "received",
                    "now",
                    "later",
                ),
            )
            raise RuntimeError("rollback")

    with closing(database.connect()) as connection:
        stored = connection.execute(
            "SELECT source_event_id FROM surface_events ORDER BY source_event_id"
        ).fetchall()
    assert [tuple(row) for row in stored] == [("event-1",)]


def test_transaction_allows_same_thread_nested_quick_check(tmp_path: Path) -> None:
    database = TaskDatabase(tmp_path / "task-settings.db")

    with database.transaction():
        assert database.quick_check() == "ok"


def test_database_permissions_are_owner_only(tmp_path: Path) -> None:
    database = TaskDatabase(tmp_path / "task-settings.db")

    assert database.verify_owner_only_permissions()
    if os.name != "nt":
        assert stat.S_IMODE(database.database_path.stat().st_mode) == 0o600


def test_live_rollback_journal_is_owner_only_before_abrupt_exit(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "task-settings.db"
    result_path = tmp_path / "journal-acl-result"
    TaskDatabase(database_path)
    script = '''
import os
import sys
from pathlib import Path

from forge.ops.task_database import TaskDatabase, _verify_owner_only_permissions

database_path, result_path = map(Path, sys.argv[1:])
database = TaskDatabase(database_path)
with database.transaction() as connection:
    connection.execute(
        """
        INSERT INTO surface_events (
            source_event_id, subject_id, session_id, surface, payload_hash,
            state, received_at, retention_until
        ) VALUES (
            'crash-event', 'user-1', 'session-1', 'cli', ?,
            'received', 'now', 'later'
        )
        """,
        ("d" * 64,),
    )
    journal = Path(f"{database_path}-journal")
    result_path.write_text(
        "owner-only" if journal.is_file() and _verify_owner_only_permissions(journal)
        else "unsafe",
        encoding="utf-8",
    )
    os._exit(0)
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(database_path), str(result_path)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        env=_subprocess_environment(),
    )

    assert result.returncode == 0, result.stderr
    assert result_path.read_text(encoding="utf-8") == "owner-only"


def test_backup_restore_is_checked_and_does_not_replace_live_db_on_failure(
    tmp_path: Path,
) -> None:
    database = TaskDatabase(tmp_path / "task-settings.db")
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO surface_events (
                source_event_id,
                subject_id,
                session_id,
                surface,
                payload_hash,
                state,
                received_at,
                retention_until
            ) VALUES (
                'event-1', 'user-1', 'session-1', 'cli', ?,
                'received', 'now', 'later'
            )
            """,
            ("a" * 64,),
        )
    backup_path = database.backup(tmp_path / "backups" / "task-settings.db")

    with database.transaction() as connection:
        connection.execute("DELETE FROM surface_events")
    database.restore(backup_path)

    with closing(database.connect()) as connection:
        count = connection.execute("SELECT COUNT(*) FROM surface_events").fetchone()[0]
    assert count == 1
    assert database.quick_check() == "ok"
    assert TaskDatabase(backup_path).quick_check() == "ok"

    invalid_backup = tmp_path / "invalid.db"
    invalid_backup.write_bytes(b"not a sqlite database")
    with pytest.raises(TaskDatabaseError, match="backup could not be restored safely"):
        database.restore(invalid_backup)

    with closing(database.connect()) as connection:
        count_after_failure = connection.execute(
            "SELECT COUNT(*) FROM surface_events"
        ).fetchone()[0]
    assert count_after_failure == 1


def test_repeated_backup_does_not_replay_existing_destination_wal(
    tmp_path: Path,
) -> None:
    database = TaskDatabase(tmp_path / "task-settings.db")
    _save_surface_event(database, "first-source-event")
    destination = database.backup(tmp_path / "backup.db")
    _leave_committed_wal(destination, "stale-destination-event")
    _replace_surface_events(database, "second-source-event")

    database.backup(destination)

    assert not Path(f"{destination}-wal").exists()
    assert not Path(f"{destination}-shm").exists()
    assert not Path(f"{destination}-journal").exists()
    destination_database = TaskDatabase(destination)
    assert _surface_event_ids(destination_database) == ["second-source-event"]
    with destination_database.read() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_backup_rejects_foreign_key_invalid_live_database(tmp_path: Path) -> None:
    database = TaskDatabase(tmp_path / "task-settings.db")
    destination = tmp_path / "backup.db"
    with sqlite3.connect(database.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            """
            INSERT INTO task_events (
                request_id,
                event_type,
                event_key,
                event_json,
                occurred_at
            ) VALUES ('missing-request', 'active', 'orphan', '{}', 'now')
            """
        )

    with pytest.raises(TaskDatabaseError, match="foreign key"):
        database.backup(destination)

    assert not destination.exists()


def test_backup_fails_fast_with_active_destination_reader(tmp_path: Path) -> None:
    database = TaskDatabase(tmp_path / "task-settings.db")
    _save_surface_event(database, "new-source-event")
    destination = database.backup(tmp_path / "backup.db")
    _replace_surface_events(database, "replacement-source-event")
    ready_path = tmp_path / "reader-ready"
    release_path = tmp_path / "reader-release"
    reader_script = '''
import sqlite3
import sys
import time
from pathlib import Path

database_path, ready_path, release_path = map(Path, sys.argv[1:])
connection = sqlite3.connect(database_path)
connection.execute("PRAGMA journal_mode = DELETE")
connection.execute("BEGIN")
connection.execute("SELECT COUNT(*) FROM surface_events").fetchone()
ready_path.write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 10
while not release_path.exists():
    if time.monotonic() >= deadline:
        raise RuntimeError("reader release timed out")
    time.sleep(0.01)
connection.rollback()
connection.close()
'''
    backup_script = '''
import sys
from pathlib import Path

from forge.ops.task_database import TaskDatabase, TaskDatabaseError

source, destination = map(Path, sys.argv[1:])
try:
    TaskDatabase(source).backup(destination)
except TaskDatabaseError as error:
    if "offline" not in str(error):
        raise
else:
    raise RuntimeError("backup unexpectedly succeeded")
'''
    reader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            reader_script,
            str(destination),
            str(ready_path),
            str(release_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_subprocess_environment(),
    )
    try:
        deadline = time.monotonic() + 5
        while not ready_path.exists() and reader.poll() is None:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert ready_path.exists()
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                backup_script,
                str(database.database_path),
                str(destination),
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
            env=_subprocess_environment(),
        )
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    finally:
        release_path.touch()
    stdout, stderr = reader.communicate(timeout=10)
    assert reader.returncode == 0, f"{stdout}\n{stderr}"


def test_existing_backup_rolls_back_bytes_when_precommit_sync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = TaskDatabase(tmp_path / "task-settings.db")
    _save_surface_event(database, "old-backup-event")
    destination = database.backup(tmp_path / "backup.db")
    old_bytes = destination.read_bytes()
    _replace_surface_events(database, "new-source-event")
    real_sync = task_database_module._sync_file

    def fail_destination_sync(path: Path) -> None:
        if path == destination:
            raise TaskDatabaseError("forced precommit sync failure")
        real_sync(path)

    monkeypatch.setattr(task_database_module, "_sync_file", fail_destination_sync)

    with pytest.raises(TaskDatabaseError, match="sync failure"):
        database.backup(destination)

    assert destination.read_bytes() == old_bytes
    assert _surface_event_ids(TaskDatabase(destination)) == ["old-backup-event"]


def test_existing_backup_rolls_back_bytes_when_precommit_acl_check_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = TaskDatabase(tmp_path / "task-settings.db")
    _save_surface_event(database, "old-backup-event")
    destination = database.backup(tmp_path / "backup.db")
    old_bytes = destination.read_bytes()
    _replace_surface_events(database, "new-source-event")
    real_verify = task_database_module._verify_owner_only_permissions
    destination_checks = 0

    def fail_candidate_acl(path: Path) -> bool:
        nonlocal destination_checks
        if path == destination:
            destination_checks += 1
            if destination_checks == 2:
                return False
        return real_verify(path)

    monkeypatch.setattr(
        task_database_module,
        "_verify_owner_only_permissions",
        fail_candidate_acl,
    )

    with pytest.raises(TaskDatabaseError, match="permissions are unsafe"):
        database.backup(destination)

    assert destination.read_bytes() == old_bytes
    assert _surface_event_ids(TaskDatabase(destination)) == ["old-backup-event"]


def test_existing_backup_rolls_back_bytes_when_precommit_readback_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = TaskDatabase(tmp_path / "task-settings.db")
    _save_surface_event(database, "old-backup-event")
    destination = database.backup(tmp_path / "backup.db")
    old_bytes = destination.read_bytes()
    _replace_surface_events(database, "new-source-event")
    real_validate = task_database_module._validate_exact_database
    destination_validations = 0

    def fail_candidate_readback(
        connection: sqlite3.Connection,
        *,
        expected_content_hash: str | None = None,
    ) -> str:
        nonlocal destination_validations
        if task_database_module._database_path_for_connection(connection) == destination:
            destination_validations += 1
            if destination_validations == 3:
                raise TaskDatabaseError("forced precommit readback failure")
        return real_validate(
            connection,
            expected_content_hash=expected_content_hash,
        )

    monkeypatch.setattr(
        task_database_module,
        "_validate_exact_database",
        fail_candidate_readback,
    )

    with pytest.raises(TaskDatabaseError, match="readback failure"):
        database.backup(destination)

    assert destination.read_bytes() == old_bytes
    assert _surface_event_ids(TaskDatabase(destination)) == ["old-backup-event"]


def test_new_backup_removes_partial_publication_when_link_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = TaskDatabase(tmp_path / "task-settings.db")
    _save_surface_event(database, "source-event")
    destination = tmp_path / "backup.db"

    def fail_after_link(artifact: Path, published_path: Path) -> None:
        os.link(artifact, published_path)
        raise TaskDatabaseError("forced link finalization failure")

    monkeypatch.setattr(
        task_database_module,
        "_link_new_backup_artifact",
        fail_after_link,
    )

    with pytest.raises(TaskDatabaseError, match="link finalization failure"):
        database.backup(destination)

    assert not destination.exists()
    assert not Path(f"{destination}-journal").exists()
    assert not Path(f"{destination}-wal").exists()
    assert not Path(f"{destination}-shm").exists()


def test_backup_reports_committed_cleanup_failure_with_retained_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = TaskDatabase(tmp_path / "task-settings.db")
    _save_surface_event(database, "source-event")
    destination = tmp_path / "backup.db"
    retained: list[object] = []
    real_remove = task_database_module._remove_database_artifact

    def fail_cleanup(artifact: object) -> None:
        retained.append(artifact)
        raise TaskDatabaseError("forced backup cleanup failure")

    monkeypatch.setattr(
        task_database_module,
        "_remove_database_artifact",
        fail_cleanup,
    )

    with pytest.raises(task_database_module.TaskDatabaseCommittedError) as captured:
        database.backup(destination)

    error = captured.value
    assert error.committed is True
    assert error.operation == "backup"
    assert error.retained_artifact.is_file()
    assert _surface_event_ids(TaskDatabase(destination)) == ["source-event"]
    assert retained
    real_remove(retained[0])


def test_backup_restore_preserves_self_reference_and_sequence_high_water(
    tmp_path: Path,
) -> None:
    database = TaskDatabase(tmp_path / "task-settings.db")
    base_request = "base-request"
    replacement_request = "replacement-request"
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO task_requests (
                request_id, format_version, request_json, request_hash,
                management_repository, task_owner_host, confirmed_by,
                confirmed_at, replaces_request_id
            ) VALUES (?, 'forge-task-request/v2', '{}', ?, 'owner/management',
                      'worker-1', 'user-1', 'now', NULL)
            """,
            (base_request, "1" * 64),
        )
        connection.execute(
            """
            INSERT INTO task_requests (
                request_id, format_version, request_json, request_hash,
                management_repository, task_owner_host, confirmed_by,
                confirmed_at, replaces_request_id
            ) VALUES (?, 'forge-task-request/v2', '{}', ?, 'owner/management',
                      'worker-1', 'user-1', 'later', ?)
            """,
            (replacement_request, "2" * 64, base_request),
        )
        connection.execute(
            """
            INSERT INTO task_events (
                request_id, event_type, event_key, event_json, occurred_at
            ) VALUES (?, 'request_prepared', 'prepared', '{}', 'later')
            """,
            (replacement_request,),
        )
        connection.execute(
            "UPDATE sqlite_sequence SET seq = 1000 WHERE name = 'task_events'"
        )
    backup_path = database.backup(tmp_path / "backup.db")
    with database.transaction() as connection:
        connection.execute(
            "UPDATE task_requests SET request_json = '{\"changed\":true}'"
        )

    database.restore(backup_path)

    with database.read() as connection:
        chain = connection.execute(
            """
            SELECT request_id, replaces_request_id
            FROM task_requests
            ORDER BY replaces_request_id IS NOT NULL
            """
        ).fetchall()
        sequence = connection.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'task_events'"
        ).fetchone()
        event_max = connection.execute("SELECT MAX(event_id) FROM task_events").fetchone()
    assert [tuple(row) for row in chain] == [
        (base_request, None),
        (replacement_request, base_request),
    ]
    assert sequence is not None and sequence[0] == 1000
    assert event_max is not None and sequence[0] > event_max[0]


def test_backup_rejects_unexpected_sqlite_sequence_entry(tmp_path: Path) -> None:
    database = TaskDatabase(tmp_path / "task-settings.db")
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO sqlite_sequence(name, seq) VALUES ('future_table', 999)"
        )

    with pytest.raises(TaskDatabaseError, match="unexpected sequence"):
        database.backup(tmp_path / "backup.db")


def test_backup_and_restore_sync_parent_directory_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = TaskDatabase(tmp_path / "task-settings.db")
    _save_surface_event(database, "archived-event")
    synced: list[Path] = []
    real_sync_directory = task_database_module._sync_directory

    def record_directory_sync(directory: Path) -> None:
        synced.append(directory)
        real_sync_directory(directory)

    monkeypatch.setattr(
        task_database_module,
        "_sync_directory",
        record_directory_sync,
    )

    backup_path = database.backup(tmp_path / "backup.db")
    _replace_surface_events(database, "live-event")
    database.restore(backup_path)

    assert synced.count(tmp_path) >= 2


@pytest.mark.skipif(os.name == "nt", reason="directory descriptors are POSIX-only")
def test_sync_directory_fsyncs_a_directory_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synced_modes: list[int] = []
    real_fsync = os.fsync

    def record_fsync(descriptor: int) -> None:
        synced_modes.append(os.fstat(descriptor).st_mode)
        real_fsync(descriptor)

    monkeypatch.setattr(task_database_module.os, "fsync", record_fsync)

    task_database_module._sync_directory(tmp_path)

    assert len(synced_modes) == 1
    assert stat.S_ISDIR(synced_modes[0])


def _database_with_different_live_and_backup(
    tmp_path: Path,
) -> tuple[TaskDatabase, Path]:
    database = TaskDatabase(tmp_path / "task-settings.db")
    _save_surface_event(database, "archived-event")
    backup_path = database.backup(tmp_path / "backup.db")
    _replace_surface_events(database, "live-event")
    return database, backup_path


def test_restore_accepts_a_valid_abrupt_exit_wal_database(tmp_path: Path) -> None:
    database, backup_path = _database_with_abrupt_wal(tmp_path)

    database.restore(backup_path)

    assert _surface_event_ids(database) == ["archived-event"]
    assert not Path(f"{database.database_path}-wal").exists()
    assert not Path(f"{database.database_path}-shm").exists()


def test_restore_rejects_unsafe_backup_sidecar_before_reading_live_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, backup_path = _database_with_different_live_and_backup(tmp_path)
    _leave_committed_wal(backup_path, "wal-backup-event")
    real_verify = task_database_module._verify_owner_only_permissions

    def reject_backup_sidecar(path: Path) -> bool:
        if path in {Path(f"{backup_path}-wal"), Path(f"{backup_path}-shm")}:
            return False
        return real_verify(path)

    monkeypatch.setattr(
        task_database_module,
        "_verify_owner_only_permissions",
        reject_backup_sidecar,
    )

    with pytest.raises(TaskDatabaseError, match="sidecar permissions"):
        database.restore(backup_path)

    assert _surface_event_ids(database) == ["live-event"]


def test_restore_rolls_back_abrupt_exit_wal_data_after_publish_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, backup_path = _database_with_abrupt_wal(tmp_path)
    real_apply = task_database_module._apply_owner_only_permissions
    publish_was_attempted = False

    def fail_first_live_acl(path: Path) -> None:
        nonlocal publish_was_attempted
        if path == database.database_path and not publish_was_attempted:
            publish_was_attempted = True
            raise TaskDatabaseError("forced ACL failure")
        real_apply(path)

    monkeypatch.setattr(
        task_database_module,
        "_apply_owner_only_permissions",
        fail_first_live_acl,
    )

    with pytest.raises(TaskDatabaseError, match="could not be restored safely"):
        database.restore(backup_path)

    assert publish_was_attempted
    assert _surface_event_ids(database) == ["live-wal-event"]
    assert database.quick_check() == "ok"


def test_restore_rolls_back_live_database_when_post_publish_acl_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, backup_path = _database_with_different_live_and_backup(tmp_path)
    real_apply = task_database_module._apply_owner_only_permissions
    failed = False

    def fail_first_live_acl(path: Path) -> None:
        nonlocal failed
        if path == database.database_path and not failed:
            failed = True
            raise TaskDatabaseError("forced ACL failure")
        real_apply(path)

    monkeypatch.setattr(
        task_database_module,
        "_apply_owner_only_permissions",
        fail_first_live_acl,
    )

    with pytest.raises(TaskDatabaseError, match="could not be restored safely"):
        database.restore(backup_path)

    assert _surface_event_ids(database) == ["live-event"]
    assert database.verify_owner_only_permissions()
    assert database.quick_check() == "ok"


def test_restore_preserves_live_database_when_staged_initialize_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, backup_path = _database_with_different_live_and_backup(tmp_path)

    def fail_initialize(connection: sqlite3.Connection) -> None:
        del connection
        raise TaskDatabaseError("forced initialize failure")

    monkeypatch.setattr(
        task_database_module,
        "_initialize_database_connection",
        fail_initialize,
    )

    with pytest.raises(TaskDatabaseError, match="could not be restored safely"):
        database.restore(backup_path)

    assert _surface_event_ids(database) == ["live-event"]
    assert database.verify_owner_only_permissions()


def test_restore_rolls_back_live_database_when_candidate_quick_check_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, backup_path = _database_with_different_live_and_backup(tmp_path)

    def fail_candidate_quick_check(
        connection: sqlite3.Connection,
        *,
        expected_content_hash: str,
    ) -> str:
        del connection, expected_content_hash
        raise TaskDatabaseError("forced final quick_check failure")

    monkeypatch.setattr(
        task_database_module,
        "_validate_restore_candidate",
        fail_candidate_quick_check,
    )

    with pytest.raises(TaskDatabaseError, match="could not be restored safely"):
        database.restore(backup_path)

    assert _surface_event_ids(database) == ["live-event"]
    assert database.verify_owner_only_permissions()


def test_restore_reports_committed_cleanup_failure_with_retained_preimage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, backup_path = _database_with_different_live_and_backup(tmp_path)
    retained: list[object] = []
    real_remove = task_database_module._remove_database_artifact

    def fail_cleanup(artifact: object) -> None:
        retained.append(artifact)
        raise TaskDatabaseError("forced restore cleanup failure")

    monkeypatch.setattr(
        task_database_module,
        "_remove_database_artifact",
        fail_cleanup,
    )

    with pytest.raises(task_database_module.TaskDatabaseCommittedError) as captured:
        database.restore(backup_path)

    error = captured.value
    assert error.committed is True
    assert error.operation == "restore"
    assert error.retained_artifact.is_file()
    assert _surface_event_ids(database) == ["archived-event"]
    assert retained
    real_remove(retained[0])


def test_restore_rejects_a_concurrent_write_before_publish(tmp_path: Path) -> None:
    database, backup_path = _database_with_different_live_and_backup(tmp_path)
    writer_started = threading.Event()
    release_writer = threading.Event()

    def hold_writer() -> None:
        with database.transaction():
            writer_started.set()
            assert release_writer.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(hold_writer)
        assert writer_started.wait(timeout=5)
        try:
            with pytest.raises(TaskDatabaseError, match="offline"):
                database.restore(backup_path)
        finally:
            release_writer.set()
        future.result(timeout=5)

    assert _surface_event_ids(database) == ["live-event"]


def test_restore_rejects_an_unmanaged_active_writer(tmp_path: Path) -> None:
    database, backup_path = _database_with_different_live_and_backup(tmp_path)

    with sqlite3.connect(database.database_path) as unmanaged_writer:
        unmanaged_writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(TaskDatabaseError, match="offline"):
            database.restore(backup_path)

    assert _surface_event_ids(database) == ["live-event"]


def test_restore_fails_bounded_with_active_delete_journal_reader(
    tmp_path: Path,
) -> None:
    database, backup_path = _database_with_different_live_and_backup(tmp_path)

    with sqlite3.connect(database.database_path) as reader:
        assert reader.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "delete"
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM surface_events").fetchone()
        started = time.monotonic()
        with pytest.raises(TaskDatabaseError, match="offline"):
            database.restore(backup_path)
        elapsed = time.monotonic() - started

    assert elapsed < 10
    assert _surface_event_ids(database) == ["live-event"]


def test_restore_gates_unmanaged_writer_before_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, backup_path = _database_with_different_live_and_backup(tmp_path)
    staging_ready = threading.Event()
    allow_staging = threading.Event()
    writer_attempted = threading.Event()
    writer_finished = threading.Event()
    original_validate = task_database_module._validate_supported_schema

    def pause_during_staging(connection: sqlite3.Connection) -> None:
        staging_ready.set()
        assert allow_staging.wait(timeout=5)
        original_validate(connection)

    monkeypatch.setattr(
        task_database_module,
        "_validate_supported_schema",
        pause_during_staging,
    )

    def restore_outcome() -> BaseException | None:
        try:
            database.restore(backup_path)
        except BaseException as error:  # noqa: BLE001 - assert the race outcome.
            return error
        return None

    def unmanaged_write() -> None:
        writer_attempted.set()
        with sqlite3.connect(database.database_path, timeout=5) as writer:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("DELETE FROM surface_events")
            writer.execute(
                """
                INSERT INTO surface_events (
                    source_event_id,
                    subject_id,
                    session_id,
                    surface,
                    payload_hash,
                    state,
                    received_at,
                    retention_until
                ) VALUES (
                    'concurrent-event', 'user-1', 'session-1', 'cli', ?,
                    'received', 'now', 'later'
                )
                """,
                ("c" * 64,),
            )
        writer_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        restore_future = pool.submit(restore_outcome)
        assert staging_ready.wait(timeout=5)
        writer_future = pool.submit(unmanaged_write)
        assert writer_attempted.wait(timeout=5)
        writer_finished.wait(timeout=0.25)
        allow_staging.set()
        restore_error = restore_future.result(timeout=10)
        writer_future.result(timeout=10)

    assert restore_error is None
    assert _surface_event_ids(database) == ["concurrent-event"]


def test_restore_is_excluded_by_a_restore_in_another_process(tmp_path: Path) -> None:
    database, backup_path = _database_with_different_live_and_backup(tmp_path)
    ready_path = tmp_path / "restore-ready"
    release_path = tmp_path / "restore-release"
    script = '''
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import forge.ops.task_database as module
from forge.ops.task_database import TaskDatabase

database_path, backup_path, ready_path, release_path = map(Path, sys.argv[1:])
original_gate = module._offline_database_gate

@contextmanager
def paused_gate(path):
    ready_path.write_text("ready", encoding="utf-8")
    deadline = time.monotonic() + 10
    while not release_path.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("release marker timed out")
        time.sleep(0.01)
    with original_gate(path) as connection:
        yield connection

module._offline_database_gate = paused_gate
TaskDatabase(database_path).restore(backup_path)
'''
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(database.database_path),
            str(backup_path),
            str(ready_path),
            str(release_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_subprocess_environment(),
    )
    try:
        deadline = time.monotonic() + 10
        while not ready_path.exists() and process.poll() is None:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert ready_path.exists()

        with pytest.raises(TaskDatabaseError, match="offline"):
            database.restore(backup_path)
    finally:
        release_path.touch()
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 0, f"{stdout}\n{stderr}"


def test_backup_cannot_overwrite_the_live_database(tmp_path: Path) -> None:
    database = TaskDatabase(tmp_path / "task-settings.db")

    with pytest.raises(TaskDatabaseError, match="must differ"):
        database.backup(database.database_path)
