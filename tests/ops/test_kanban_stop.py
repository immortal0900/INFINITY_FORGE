from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

import forge.ops.kanban_stop as kanban_stop_module
from forge.ops.kanban_stop import (
    KanbanStopError,
    KanbanStopResult,
    archive_matching_cards as _archive_matching_cards,
)
from forge.ops.process_identity import (
    ProcessBinding,
    ProcessIdentity,
    ProcessMemberIdentity,
    ProcessScopeKind,
)


REQUEST_ID = str(uuid4())
OTHER_REQUEST_ID = str(uuid4())
HOST_ID = str(uuid4())
PROJECT_IDS = tuple(f"{number:064x}" for number in range(1, 12))
TASK_SETTINGS_HASH = "f" * 64
OTHER_TASK_SETTINGS_HASH = "e" * 64
_REAL_HERMES_DISPATCH_LOCK_PROBE = (
    kanban_stop_module._installed_hermes_dispatch_lock_probe
)
NONTERMINAL = (
    "triage",
    "todo",
    "scheduled",
    "ready",
    "running",
    "blocked",
    "review",
)


def _create_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA journal_mode = WAL;
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL,
            priority INTEGER DEFAULT 0,
            created_at INTEGER NOT NULL,
            claim_lock TEXT,
            claim_expires INTEGER,
            worker_pid INTEGER,
            current_run_id INTEGER,
            idempotency_key TEXT
        );
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            profile TEXT,
            status TEXT NOT NULL,
            claim_lock TEXT,
            claim_expires INTEGER,
            worker_pid INTEGER,
            started_at INTEGER NOT NULL,
            ended_at INTEGER,
            outcome TEXT,
            summary TEXT,
            metadata TEXT,
            error TEXT
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            run_id INTEGER,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        );
        """
    )
    return connection


def _root_key(request_id: str, project_id: str) -> str:
    return f"forge-task-v2:{request_id}:{project_id}:build"


def _insert_card(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    status: str,
    request_id: str = REQUEST_ID,
    project_id: str = PROJECT_IDS[0],
    worker_pid: int | None = None,
    current_run_id: int | None = None,
    claim_lock: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO tasks (
            id, title, status, created_at, claim_lock, claim_expires,
            worker_pid, current_run_id, idempotency_key
        ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            task_id,
            status,
            claim_lock,
            999 if claim_lock else None,
            worker_pid,
            current_run_id,
            _root_key(request_id, project_id),
        ),
    )


def _identity(binding: ProcessBinding, pid: int) -> ProcessIdentity:
    member = ProcessMemberIdentity(pid=pid, start_identity="boot:123")
    return ProcessIdentity(
        binding=binding,
        platform="posix",
        pid=pid,
        start_identity=member.start_identity,
        scope_kind=ProcessScopeKind.CGROUP,
        scope_id="/forge/test",
        control_group_id=None,
        members=(member,),
    )


def _statuses(path: Path) -> dict[str, str]:
    with sqlite3.connect(path) as connection:
        return dict(connection.execute("SELECT id, status FROM tasks ORDER BY id"))


def _hermes_style_dispatch_probe(database_path: Path) -> bool:
    """Return Hermes' acquired flag for its board lock path."""

    lock_path = database_path.with_name(database_path.name + ".dispatch.lock")
    handle = lock_path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return False
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return True
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            return False
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return True
    finally:
        handle.close()


@pytest.fixture(autouse=True)
def _compatible_installed_hermes_dispatch_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        kanban_stop_module,
        "_installed_hermes_dispatch_lock_probe",
        _hermes_style_dispatch_probe,
    )


def archive_matching_cards(
    database_path: str | Path,
    **kwargs: object,
) -> KanbanStopResult:
    kwargs.setdefault("task_settings_hash", TASK_SETTINGS_HASH)
    kwargs.setdefault("dispatcher_database_path", database_path)
    return _archive_matching_cards(database_path, **kwargs)  # type: ignore[arg-type]


def test_active_task_settings_hash_is_captured_in_the_process_binding(
    tmp_path: Path,
) -> None:
    path = tmp_path / "kanban.db"
    connection = _create_database(path)
    _insert_card(
        connection,
        task_id="worker-card",
        status="running",
        worker_pid=101,
        current_run_id=17,
        claim_lock="owner-machine:42",
    )
    connection.execute(
        """
        INSERT INTO task_runs (
            id, task_id, status, claim_lock, claim_expires, worker_pid, started_at
        ) VALUES (17, 'worker-card', 'running', 'owner-machine:42', 999, 101, 50)
        """
    )
    connection.commit()
    connection.close()
    seen: list[ProcessBinding] = []

    def lookup(binding: ProcessBinding, pid: int) -> ProcessIdentity:
        seen.append(binding)
        return _identity(binding, pid)

    archive_matching_cards(
        path,
        request_id=REQUEST_ID,
        task_settings_hash=TASK_SETTINGS_HASH,
        owner_host=HOST_ID,
        current_host=HOST_ID,
        reason="stop",
        identity_lookup=lookup,
        claimer_host_name="owner-machine",
        dispatcher_database_path=path,
    )

    assert seen[0].task_settings_hash == TASK_SETTINGS_HASH


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), 10**1000])
def test_dispatch_lock_timeout_must_be_finite_and_positive(
    tmp_path: Path,
    timeout: float,
) -> None:
    path = tmp_path / "kanban.db"
    connection = _create_database(path)
    _insert_card(connection, task_id="ready", status="ready")
    connection.commit()
    connection.close()

    with pytest.raises(ValueError, match="finite positive"):
        archive_matching_cards(
            path,
            request_id=REQUEST_ID,
            task_settings_hash=TASK_SETTINGS_HASH,
            owner_host=HOST_ID,
            current_host=HOST_ID,
            reason="stop",
            lock_timeout_seconds=timeout,
            dispatcher_database_path=path,
        )

    assert _statuses(path)["ready"] == "ready"


def test_dispatch_lock_covers_a_different_symlink_alias_used_by_hermes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "real" / "kanban.db"
    path.parent.mkdir()
    connection = _create_database(path)
    _insert_card(connection, task_id="ready", status="ready")
    connection.commit()
    connection.close()
    alias = tmp_path / "alias" / "board.db"
    alias.parent.mkdir()
    try:
        alias.symlink_to(path)
    except OSError:
        os.link(path, alias)
    probed: list[Path] = []

    def probe(database_path: Path) -> bool:
        probed.append(database_path)
        return _hermes_style_dispatch_probe(database_path)

    monkeypatch.setattr(
        kanban_stop_module,
        "_installed_hermes_dispatch_lock_probe",
        probe,
    )

    result = archive_matching_cards(
        path,
        request_id=REQUEST_ID,
        task_settings_hash=TASK_SETTINGS_HASH,
        owner_host=HOST_ID,
        current_host=HOST_ID,
        reason="stop",
        dispatcher_database_path=alias,
    )

    assert result.archived_card_ids == ("ready",)
    assert probed == [alias.absolute()]


def test_dispatcher_lock_noop_or_wrong_database_fails_before_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "kanban.db"
    other = tmp_path / "other.db"
    connection = _create_database(path)
    _insert_card(connection, task_id="ready", status="ready")
    connection.commit()
    connection.close()
    _create_database(other).close()

    monkeypatch.setattr(
        kanban_stop_module,
        "_installed_hermes_dispatch_lock_probe",
        lambda _path: True,
    )
    with pytest.raises(KanbanStopError, match="lock compatibility"):
        archive_matching_cards(
            path,
            request_id=REQUEST_ID,
            task_settings_hash=TASK_SETTINGS_HASH,
            owner_host=HOST_ID,
            current_host=HOST_ID,
            reason="stop",
            dispatcher_database_path=path,
        )
    monkeypatch.setattr(
        kanban_stop_module,
        "_installed_hermes_dispatch_lock_probe",
        _hermes_style_dispatch_probe,
    )
    with pytest.raises(KanbanStopError, match="database identity"):
        archive_matching_cards(
            path,
            request_id=REQUEST_ID,
            task_settings_hash=TASK_SETTINGS_HASH,
            owner_host=HOST_ID,
            current_host=HOST_ID,
            reason="stop",
            dispatcher_database_path=other,
        )

    assert _statuses(path)["ready"] == "ready"


def test_installed_hermes_dispatch_probe_invokes_the_actual_lock_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "kanban.db"
    path.touch()
    seen: list[Path] = []

    @contextlib.contextmanager
    def guard(database_path: Path):  # type: ignore[no-untyped-def]
        seen.append(database_path)
        yield False

    def load(name: str) -> SimpleNamespace:
        assert name == "hermes_cli.kanban_db"
        return SimpleNamespace(_dispatch_tick_lock=guard)

    monkeypatch.setattr(kanban_stop_module.importlib, "import_module", load)

    assert _REAL_HERMES_DISPATCH_LOCK_PROBE(path) is False
    assert seen == [path]


def test_all_matching_nonterminal_cards_archive_in_one_transaction(
    tmp_path: Path,
) -> None:
    path = tmp_path / "kanban.db"
    connection = _create_database(path)
    for index, status in enumerate((*NONTERMINAL, "done", "archived")):
        _insert_card(
            connection,
            task_id=f"target-{status}",
            status=status,
            project_id=PROJECT_IDS[index],
        )
    _insert_card(
        connection,
        task_id="other-ready",
        status="ready",
        request_id=OTHER_REQUEST_ID,
        project_id=PROJECT_IDS[10],
    )
    connection.commit()
    connection.close()

    result = archive_matching_cards(
        path,
        request_id=REQUEST_ID,
        owner_host=HOST_ID,
        current_host=HOST_ID,
        reason="사용자 중단",
        occurred_at=100,
    )

    statuses = _statuses(path)
    assert all(statuses[f"target-{status}"] == "archived" for status in NONTERMINAL)
    assert statuses["target-done"] == "done"
    assert statuses["target-archived"] == "archived"
    assert statuses["other-ready"] == "ready"
    assert set(result.archived_card_ids) == {
        f"target-{status}" for status in NONTERMINAL
    }
    assert set(result.preserved_card_ids) == {"target-done", "target-archived"}
    assert result.all_cards_terminal is True
    with sqlite3.connect(path) as check:
        events = check.execute(
            "SELECT task_id, kind, payload FROM task_events ORDER BY task_id"
        ).fetchall()
    assert len(events) == len(NONTERMINAL)
    assert all(kind == "archived" for _, kind, _ in events)
    assert all(
        json.loads(payload)["reason"] == "사용자 중단" for _, _, payload in events
    )


def test_failure_rolls_back_every_card_and_event(tmp_path: Path) -> None:
    path = tmp_path / "kanban.db"
    connection = _create_database(path)
    _insert_card(connection, task_id="one", status="ready", project_id=PROJECT_IDS[0])
    _insert_card(connection, task_id="two", status="blocked", project_id=PROJECT_IDS[1])
    connection.executescript(
        """
        CREATE TRIGGER fail_second_archive
        BEFORE INSERT ON task_events
        WHEN NEW.task_id = 'two' AND NEW.kind = 'archived'
        BEGIN
            SELECT RAISE(ABORT, 'forced archive failure');
        END;
        """
    )
    connection.commit()
    connection.close()

    with pytest.raises(KanbanStopError, match="transaction"):
        archive_matching_cards(
            path,
            request_id=REQUEST_ID,
            owner_host=HOST_ID,
            current_host=HOST_ID,
            reason="stop",
            occurred_at=100,
        )

    assert _statuses(path) == {"one": "ready", "two": "blocked"}
    with sqlite3.connect(path) as check:
        assert check.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 0


def test_active_run_and_exact_process_identity_are_captured_before_archive(
    tmp_path: Path,
) -> None:
    path = tmp_path / "kanban.db"
    connection = _create_database(path)
    _insert_card(
        connection,
        task_id="worker-card",
        status="running",
        worker_pid=101,
        current_run_id=17,
        claim_lock="owner-machine:42",
    )
    connection.execute(
        """
        INSERT INTO task_runs (
            id, task_id, status, claim_lock, claim_expires, worker_pid,
            started_at, ended_at, outcome
        ) VALUES (17, 'worker-card', 'running', 'owner-machine:42', 999,
                  101, 50, NULL, NULL)
        """
    )
    connection.commit()
    connection.close()
    seen: list[ProcessBinding] = []

    def lookup(binding: ProcessBinding, pid: int) -> ProcessIdentity:
        seen.append(binding)
        return _identity(binding, pid)

    result = archive_matching_cards(
        path,
        request_id=REQUEST_ID,
        owner_host=HOST_ID,
        current_host=HOST_ID,
        reason="stop",
        occurred_at=100,
        identity_lookup=lookup,
        claimer_host_name="owner-machine",
    )

    assert seen == [
        ProcessBinding(
            request_id=REQUEST_ID,
            task_settings_hash=TASK_SETTINGS_HASH,
            project_id=PROJECT_IDS[0],
            task_id="worker-card",
            run_id="17",
            host_id=HOST_ID,
        )
    ]
    assert result.captured_runs[0].process_identity == _identity(seen[0], 101)
    with sqlite3.connect(path) as check:
        task = check.execute(
            """
            SELECT status, claim_lock, claim_expires, worker_pid, current_run_id
            FROM tasks WHERE id = 'worker-card'
            """
        ).fetchone()
        run = check.execute(
            """
            SELECT status, outcome, ended_at, claim_lock, claim_expires, worker_pid
            FROM task_runs WHERE id = 17
            """
        ).fetchone()
    assert task == ("archived", None, None, None, None)
    assert run == ("reclaimed", "reclaimed", 100, None, None, None)


def test_mismatched_process_binding_rolls_back_without_archiving(
    tmp_path: Path,
) -> None:
    path = tmp_path / "kanban.db"
    connection = _create_database(path)
    _insert_card(
        connection,
        task_id="worker-card",
        status="running",
        worker_pid=101,
        current_run_id=17,
        claim_lock="owner-machine:42",
    )
    connection.execute(
        """
        INSERT INTO task_runs (
            id, task_id, status, claim_lock, claim_expires, worker_pid, started_at
        ) VALUES (17, 'worker-card', 'running', 'owner-machine:42', 999, 101, 50)
        """
    )
    connection.commit()
    connection.close()

    def wrong_lookup(binding: ProcessBinding, pid: int) -> ProcessIdentity:
        return _identity(
            ProcessBinding(
                request_id=binding.request_id,
                task_settings_hash=binding.task_settings_hash,
                project_id=binding.project_id,
                task_id="another-card",
                run_id=binding.run_id,
                host_id=binding.host_id,
            ),
            pid,
        )

    with pytest.raises(KanbanStopError, match="process identity"):
        archive_matching_cards(
            path,
            request_id=REQUEST_ID,
            owner_host=HOST_ID,
            current_host=HOST_ID,
            reason="stop",
            occurred_at=100,
            identity_lookup=wrong_lookup,
            claimer_host_name="owner-machine",
        )

    assert _statuses(path)["worker-card"] == "running"


def test_archive_transaction_blocks_dispatcher_write_until_cards_are_terminal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "kanban.db"
    connection = _create_database(path)
    _insert_card(
        connection,
        task_id="worker-card",
        status="running",
        worker_pid=101,
        current_run_id=17,
        claim_lock="owner-machine:42",
    )
    connection.execute(
        """
        INSERT INTO task_runs (
            id, task_id, status, claim_lock, claim_expires, worker_pid, started_at
        ) VALUES (17, 'worker-card', 'running', 'owner-machine:42', 999, 101, 50)
        """
    )
    connection.commit()
    connection.close()
    transaction_open = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def lookup(binding: ProcessBinding, pid: int) -> ProcessIdentity:
        transaction_open.set()
        assert release.wait(timeout=5)
        return _identity(binding, pid)

    def stop() -> None:
        try:
            archive_matching_cards(
                path,
                request_id=REQUEST_ID,
                owner_host=HOST_ID,
                current_host=HOST_ID,
                reason="stop",
                occurred_at=100,
                identity_lookup=lookup,
                claimer_host_name="owner-machine",
            )
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    thread = threading.Thread(target=stop)
    thread.start()
    assert transaction_open.wait(timeout=5)
    dispatcher = sqlite3.connect(path, timeout=0.05, isolation_level=None)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        dispatcher.execute("BEGIN IMMEDIATE")
    dispatcher.close()
    release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []
    assert _statuses(path)["worker-card"] == "archived"


def test_wrong_owner_host_and_malformed_target_key_write_nothing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "kanban.db"
    connection = _create_database(path)
    _insert_card(connection, task_id="ready", status="ready")
    connection.execute(
        """
        INSERT INTO tasks (id, title, status, created_at, idempotency_key)
        VALUES ('bad', 'bad', 'ready', 1, ?)
        """,
        (f"forge-step-v2:{REQUEST_ID}:not-a-project:build:abc",),
    )
    connection.commit()
    connection.close()

    with pytest.raises(KanbanStopError, match="owner host"):
        archive_matching_cards(
            path,
            request_id=REQUEST_ID,
            owner_host=HOST_ID,
            current_host=str(uuid4()),
            reason="stop",
        )
    assert _statuses(path) == {"bad": "ready", "ready": "ready"}

    with pytest.raises(KanbanStopError, match="malformed"):
        archive_matching_cards(
            path,
            request_id=REQUEST_ID,
            owner_host=HOST_ID,
            current_host=HOST_ID,
            reason="stop",
        )
    assert _statuses(path) == {"bad": "ready", "ready": "ready"}


def test_terminal_card_status_is_preserved_while_a_leaked_run_is_reclaimed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "kanban.db"
    connection = _create_database(path)
    _insert_card(
        connection,
        task_id="done-card",
        status="done",
        worker_pid=101,
        current_run_id=17,
        claim_lock="owner-machine:42",
    )
    connection.execute(
        """
        INSERT INTO task_runs (
            id, task_id, status, claim_lock, claim_expires, worker_pid,
            started_at
        ) VALUES (17, 'done-card', 'running', 'owner-machine:42', 999, 101, 50)
        """
    )
    connection.commit()
    connection.close()

    result = archive_matching_cards(
        path,
        request_id=REQUEST_ID,
        owner_host=HOST_ID,
        current_host=HOST_ID,
        reason="stop",
        occurred_at=100,
        identity_lookup=lambda binding, pid: _identity(binding, pid),
        claimer_host_name="owner-machine",
    )

    assert _statuses(path)["done-card"] == "done"
    assert result.archived_card_ids == ()
    assert result.preserved_card_ids == ("done-card",)
    assert len(result.captured_runs) == 1
    with sqlite3.connect(path) as check:
        assert check.execute(
            "SELECT status, outcome, ended_at FROM task_runs WHERE id = 17"
        ).fetchone() == ("reclaimed", "reclaimed", 100)
        assert (
            check.execute(
                "SELECT kind FROM task_events WHERE task_id = 'done-card'"
            ).fetchone()[0]
            == "stop_runtime_reclaimed"
        )


def test_run_claimed_on_another_host_fails_before_process_lookup_or_write(
    tmp_path: Path,
) -> None:
    path = tmp_path / "kanban.db"
    connection = _create_database(path)
    _insert_card(
        connection,
        task_id="worker-card",
        status="running",
        worker_pid=101,
        current_run_id=17,
        claim_lock="other-machine:42",
    )
    connection.execute(
        """
        INSERT INTO task_runs (
            id, task_id, status, claim_lock, claim_expires, worker_pid,
            started_at
        ) VALUES (17, 'worker-card', 'running', 'other-machine:42', 999, 101, 50)
        """
    )
    connection.commit()
    connection.close()
    lookups: list[int] = []

    def lookup(binding: ProcessBinding, pid: int) -> ProcessIdentity:
        lookups.append(pid)
        return _identity(binding, pid)

    with pytest.raises(KanbanStopError, match="another claimer host"):
        archive_matching_cards(
            path,
            request_id=REQUEST_ID,
            owner_host=HOST_ID,
            current_host=HOST_ID,
            reason="stop",
            occurred_at=100,
            identity_lookup=lookup,
            claimer_host_name="owner-machine",
        )

    assert lookups == []
    assert _statuses(path)["worker-card"] == "running"


def test_retry_is_idempotent_and_does_not_duplicate_archive_events(
    tmp_path: Path,
) -> None:
    path = tmp_path / "kanban.db"
    connection = _create_database(path)
    _insert_card(connection, task_id="ready", status="ready")
    connection.commit()
    connection.close()

    first = archive_matching_cards(
        path,
        request_id=REQUEST_ID,
        owner_host=HOST_ID,
        current_host=HOST_ID,
        reason="stop",
        occurred_at=100,
    )
    second = archive_matching_cards(
        path,
        request_id=REQUEST_ID,
        owner_host=HOST_ID,
        current_host=HOST_ID,
        reason="stop",
        occurred_at=101,
    )

    assert first.archived_card_ids == ("ready",)
    assert second.archived_card_ids == ()
    assert second.preserved_card_ids == ("ready",)
    with sqlite3.connect(path) as check:
        assert check.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 1


def test_dispatch_tick_file_lock_blocks_stop_before_any_sqlite_write(
    tmp_path: Path,
) -> None:
    path = tmp_path / "kanban.db"
    connection = _create_database(path)
    _insert_card(connection, task_id="ready", status="ready")
    connection.commit()
    connection.close()
    lock_path = path.with_name(path.name + ".dispatch.lock")
    handle = lock_path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(KanbanStopError, match="dispatcher"):
            archive_matching_cards(
                path,
                request_id=REQUEST_ID,
                owner_host=HOST_ID,
                current_host=HOST_ID,
                reason="stop",
                lock_timeout_seconds=0.001,
            )
    finally:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()

    assert _statuses(path)["ready"] == "ready"
