from __future__ import annotations

import json
import multiprocessing
import os
import stat
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from forge.ops.surface_events import (
    LocalSurfaceOutbox,
    SurfaceEventConflictError,
    SurfaceEventError,
    SurfaceEventStore,
    TrustedTurnContext,
)
from forge.ops.task_database import TaskDatabase


HOST_ID = "d6f70d5d-6482-45f5-80d2-219ec2ad4d19"
NOW = datetime(2026, 7, 19, 1, 2, 3, 456789, tzinfo=UTC)


def _prepare_outbox_in_process(
    path: str,
    payload: str,
    start: object,
    output: object,
) -> None:
    start.wait()
    try:
        source_event_id = LocalSurfaceOutbox(path).prepare(
            surface="cli",
            session_id="session-1",
            payload=payload,
        )
        output.put((payload, source_event_id, None))
    except BaseException as error:  # pragma: no cover - returned to parent process
        output.put((payload, None, repr(error)))


def _run_parallel_outbox_prepares(path: Path, payloads: list[str]) -> list[tuple]:
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    output = context.Queue()
    processes = [
        context.Process(
            target=_prepare_outbox_in_process,
            args=(str(path), payload, start, output),
        )
        for payload in payloads
    ]
    for process in processes:
        process.start()
    start.set()
    results = [output.get(timeout=30) for _process in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
    return results


def _context(source_event_id: str = "desktop:01JZABC") -> TrustedTurnContext:
    return TrustedTurnContext(
        owner_host=HOST_ID,
        subject_id="local-user",
        session_id="session-1",
        surface="desktop",
        source_event_id=source_event_id,
        working_directory="C:/01.project/example",
    )


def test_receive_is_durable_and_exact_retries_return_one_event(tmp_path: Path) -> None:
    database = TaskDatabase(tmp_path / "tasks.sqlite3")
    first_store = SurfaceEventStore(database, clock=lambda: NOW)

    first = first_store.receive(_context(), "send this to the Task")
    restarted_store = SurfaceEventStore(database, clock=lambda: NOW + timedelta(minutes=1))
    retried = restarted_store.receive(_context(), "send this to the Task")

    assert retried == first
    assert first.state == "received"
    assert first.received_at == NOW
    assert first.retention_until == NOW + timedelta(days=30)
    with database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM surface_events").fetchone()[0] == 1


def test_source_event_id_cannot_be_reused_for_other_payload_or_identity(
    tmp_path: Path,
) -> None:
    database = TaskDatabase(tmp_path / "tasks.sqlite3")
    store = SurfaceEventStore(database, clock=lambda: NOW)
    store.receive(_context(), "original")

    with pytest.raises(SurfaceEventConflictError, match="immutable"):
        store.receive(_context(), "changed")
    with pytest.raises(SurfaceEventConflictError, match="immutable"):
        store.receive(replace(_context(), subject_id="different-user"), "original")

    assert store.get(_context().source_event_id).subject_id == "local-user"


def test_source_event_id_cannot_be_replayed_from_another_owner_host(
    tmp_path: Path,
) -> None:
    database = TaskDatabase(tmp_path / "tasks.sqlite3")
    store = SurfaceEventStore(database, clock=lambda: NOW)
    store.receive(_context(), "original")

    with pytest.raises(SurfaceEventConflictError, match="immutable"):
        store.receive(
            replace(
                _context(),
                owner_host="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            ),
            "original",
        )

    with database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM surface_events").fetchone()[0] == 1


def test_source_event_id_cannot_be_replayed_from_another_working_directory(
    tmp_path: Path,
) -> None:
    database = TaskDatabase(tmp_path / "tasks.sqlite3")
    store = SurfaceEventStore(database, clock=lambda: NOW)
    store.receive(_context(), "same command")

    with pytest.raises(SurfaceEventConflictError, match="immutable"):
        store.receive(
            replace(_context(), working_directory="C:/01.project/other"),
            "same command",
        )
    with pytest.raises(SurfaceEventConflictError, match="immutable"):
        store.receive(
            replace(_context(), working_directory=None),
            "same command",
        )

    with database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM surface_events").fetchone()[0] == 1


def test_handled_and_responded_transitions_are_restart_safe(tmp_path: Path) -> None:
    database = TaskDatabase(tmp_path / "tasks.sqlite3")
    store = SurfaceEventStore(database, clock=lambda: NOW)
    store.receive(_context(), b"payload")

    handled = store.mark_handled(_context().source_event_id)
    responded = store.mark_responded(
        _context().source_event_id,
        "Task update saved",
        at=NOW + timedelta(seconds=2),
    )
    retried = SurfaceEventStore(database).mark_responded(
        _context().source_event_id,
        "Task update saved",
        at=NOW + timedelta(days=1),
    )

    assert handled.state == "handled"
    assert responded.state == "responded"
    assert retried == responded
    assert retried.responded_at == NOW + timedelta(seconds=2)
    with pytest.raises(SurfaceEventConflictError, match="response"):
        store.mark_responded(_context().source_event_id, "different response")


def test_response_time_cannot_precede_receive_time(tmp_path: Path) -> None:
    database = TaskDatabase(tmp_path / "tasks.sqlite3")
    store = SurfaceEventStore(database, clock=lambda: NOW)
    store.receive(_context(), "payload")

    with pytest.raises(SurfaceEventError, match="response time"):
        store.mark_responded(
            _context().source_event_id,
            "response",
            at=NOW - timedelta(microseconds=1),
        )


@pytest.mark.parametrize(
    "update",
    [
        "retention_until = received_at",
        "state = 'handled', response_hash = '" + "a" * 64 + "', responded_at = received_at",
        "state = 'responded', response_hash = NULL, responded_at = NULL",
        "state = 'responded', response_hash = '" + "a" * 64 + "', responded_at = '2026-07-19T01:02:03.456788Z'",
    ],
)
def test_corrupt_source_event_chronology_fails_closed(
    tmp_path: Path,
    update: str,
) -> None:
    database = TaskDatabase(tmp_path / "tasks.sqlite3")
    store = SurfaceEventStore(database, clock=lambda: NOW)
    store.receive(_context(), "payload")
    with database.transaction() as connection:
        connection.execute(
            f"UPDATE surface_events SET {update} WHERE source_event_id = ?",
            (_context().source_event_id,),
        )

    with pytest.raises(SurfaceEventError, match="stored source event"):
        store.get(_context().source_event_id)


def test_retention_expiry_is_explicit_and_does_not_delete_audit_identity(
    tmp_path: Path,
) -> None:
    database = TaskDatabase(tmp_path / "tasks.sqlite3")
    store = SurfaceEventStore(database, retention=timedelta(seconds=5), clock=lambda: NOW)
    event = store.receive(_context(), "payload")

    assert store.expire_due(at=NOW + timedelta(seconds=4)) == 0
    assert store.expire_due(at=NOW + timedelta(seconds=5)) == 1
    assert store.get(event.source_event_id).state == "expired"
    assert store.expire_due(at=NOW + timedelta(days=1)) == 0


def test_task_database_and_local_outbox_are_owner_only(tmp_path: Path) -> None:
    database = TaskDatabase(tmp_path / "tasks.sqlite3")
    SurfaceEventStore(database, clock=lambda: NOW).receive(_context(), "payload")
    outbox = LocalSurfaceOutbox(tmp_path / "client-outbox.json")
    outbox.prepare(surface="cli", session_id="session-1", payload="hello")

    assert database.verify_owner_only_permissions() is True
    assert outbox.verify_owner_only_permissions() is True
    if os.name != "nt":
        assert stat.S_IMODE(database.database_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(outbox.path.stat().st_mode) == 0o600


def test_local_outbox_reuses_pending_id_after_restart_then_rotates_after_ack(
    tmp_path: Path,
) -> None:
    path = tmp_path / "client-outbox.json"
    first = LocalSurfaceOutbox(path).prepare(
        surface="cli", session_id="session-1", payload="hello"
    )
    after_restart = LocalSurfaceOutbox(path).prepare(
        surface="cli", session_id="session-1", payload="hello"
    )

    assert after_restart == first
    LocalSurfaceOutbox(path).acknowledge(first)
    second = LocalSurfaceOutbox(path).prepare(
        surface="cli", session_id="session-1", payload="hello"
    )
    assert second != first
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["format_version"] == "forge-surface-outbox/v1"
    assert [entry["source_event_id"] for entry in raw["pending"].values()] == [second]
    assert "hello" not in path.read_text(encoding="utf-8")


def test_local_outbox_keeps_independent_unacknowledged_submissions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "client-outbox.json"
    outbox = LocalSurfaceOutbox(path)
    first = outbox.prepare(surface="cli", session_id="session-1", payload="first")
    second = outbox.prepare(surface="cli", session_id="session-1", payload="second")

    restarted = LocalSurfaceOutbox(path)
    assert second != first
    assert restarted.prepare(
        surface="cli", session_id="session-1", payload="first"
    ) == first
    assert restarted.prepare(
        surface="cli", session_id="session-1", payload="second"
    ) == second


def test_local_outbox_serializes_processes_and_preserves_every_payload(
    tmp_path: Path,
) -> None:
    path = tmp_path / "client-outbox.json"
    payloads = [f"payload-{index}" for index in range(8)]

    results = _run_parallel_outbox_prepares(path, payloads)

    assert all(error is None for _payload, _source_event_id, error in results), results
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert len(raw["pending"]) == len(payloads)
    assert {entry["source_event_id"] for entry in raw["pending"].values()} == {
        source_event_id for _payload, source_event_id, _error in results
    }


def test_local_outbox_serializes_same_payload_to_one_process_safe_id(
    tmp_path: Path,
) -> None:
    path = tmp_path / "client-outbox.json"

    results = _run_parallel_outbox_prepares(path, ["same"] * 8)

    assert all(error is None for _payload, _source_event_id, error in results), results
    assert len({source_event_id for _payload, source_event_id, _error in results}) == 1
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert len(raw["pending"]) == 1


def test_local_outbox_rejects_dangling_symlink(tmp_path: Path) -> None:
    path = tmp_path / "client-outbox.json"
    try:
        path.symlink_to(tmp_path / "missing.json")
    except OSError as error:
        pytest.skip(f"symbolic links are unavailable: {error}")

    with pytest.raises(SurfaceEventError, match="symlink|unsafe"):
        LocalSurfaceOutbox(path)


def test_local_outbox_rejects_symlinked_parent(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    try:
        linked_parent.symlink_to(real_parent, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symbolic links are unavailable: {error}")

    with pytest.raises(SurfaceEventError, match="symlink|unsafe"):
        LocalSurfaceOutbox(linked_parent / "client-outbox.json")


@pytest.mark.parametrize(
    "changes",
    [
        {"owner_host": "not-a-uuid"},
        {"subject_id": ""},
        {"session_id": ""},
        {"surface": ""},
        {"source_event_id": ""},
        {"working_directory": "bad\x00path"},
    ],
)
def test_trusted_turn_context_rejects_malformed_transport_identity(changes) -> None:
    values = {
        "owner_host": HOST_ID,
        "subject_id": "local-user",
        "session_id": "session-1",
        "surface": "cli",
        "source_event_id": "cli:event-1",
        "working_directory": "C:/work",
    }
    values.update(changes)

    with pytest.raises(SurfaceEventError):
        TrustedTurnContext(**values)
