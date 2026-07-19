from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from forge.ops.surface_events import SurfaceEventStore, TrustedTurnContext
from forge.ops.task_database import TaskDatabase
from forge.ops.task_messages import (
    MAX_MESSAGE_BYTES,
    MAX_REVISION_BYTES,
    MAX_REVISION_MESSAGES,
    TaskMessageConflictError,
    TaskMessageError,
    TaskMessageStore,
)
from forge.ops.task_options import MergeMode, TaskFlow
from forge.ops.task_projects import TaskProject
from forge.ops.task_settings import TaskContent
from forge.ops.task_settings_v2 import TaskRequestV2, TaskSettingsV2


HOST_ID = "d6f70d5d-6482-45f5-80d2-219ec2ad4d19"
NOW = datetime(2026, 7, 19, 3, 4, 5, 123456, tzinfo=UTC)


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _project(tmp_path: Path, name: str = "workspace") -> TaskProject:
    workspace = (tmp_path / name).resolve()
    workspace.mkdir()
    return TaskProject.create(
        repository=f"example/{name}",
        workspace=str(workspace),
        remote_name="origin",
        base_branch="main",
        base_commit="a" * 40,
        host_id=HOST_ID,
    )


def _request(
    tmp_path: Path,
    *,
    request_id: str = "4485be21-2a8f-41b8-a2a2-e25722df284e",
    project_name: str = "workspace",
) -> TaskRequestV2:
    return TaskRequestV2.create(
        request_id=request_id,
        management_repository="example/infinity-forge",
        task_content=TaskContent(
            title="Base Task",
            description="Initial confirmed work.",
            acceptance_criteria=("Keep the contract exact.",),
        ),
        task_flow=TaskFlow.BUILD,
        merge_mode=MergeMode.MANUAL,
        merge_order=None,
        projects=(_project(tmp_path, project_name),),
        task_owner_host=HOST_ID,
        confirmed_by="local-user",
        confirmed_at=NOW,
    )


def _insert_active(
    database: TaskDatabase,
    request: TaskRequestV2,
    *,
    issue_number: int = 21,
    grant_access: bool = True,
) -> TaskSettingsV2:
    settings = TaskSettingsV2.create(
        request=request,
        parent_issue_number=issue_number,
    )
    payload = json.loads(request.to_json())
    project_payload = payload["projects"][0]
    occurred_at = payload["confirmed_at"]
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO task_requests (
                request_id, format_version, request_json, request_hash,
                management_repository, task_owner_host, confirmed_by,
                confirmed_at, replaces_request_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request.request_id,
                request.format_version,
                request.to_json(),
                request.request_hash,
                request.management_repository,
                request.task_owner_host,
                request.confirmed_by,
                occurred_at,
                request.replaces_request_id,
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
                occurred_at,
            ),
        )
        project = request.projects[0]
        connection.execute(
            """
            INSERT INTO task_projects (
                request_id, project_id, task_settings_hash, project_json,
                state, root_card_id, updated_at
            ) VALUES (?, ?, ?, ?, 'ready', 'root-card-1', ?)
            """,
            (
                request.request_id,
                project.project_id,
                settings.task_settings_hash,
                _canonical(project_payload),
                occurred_at,
            ),
        )
        common = _canonical({"task_settings_hash": settings.task_settings_hash})
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
                    common,
                    occurred_at,
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
                _canonical(
                    {
                        "project_ids": [project.project_id],
                        "task_settings_hash": settings.task_settings_hash,
                    }
                ),
                occurred_at,
            ),
        )
        if grant_access:
            connection.execute(
                """
                INSERT INTO task_access (
                    request_id, surface, subject_id, role, granted_by,
                    granted_at, revoked_at
                ) VALUES (?, 'desktop', ?, 'owner', ?, ?, NULL)
                """,
                (
                    request.request_id,
                    request.confirmed_by,
                    request.confirmed_by,
                    occurred_at,
                ),
            )
    return settings


def _active_task(
    tmp_path: Path,
) -> tuple[TaskDatabase, TaskRequestV2, TaskSettingsV2]:
    database = TaskDatabase(tmp_path / "task.db")
    request = _request(tmp_path)
    settings = _insert_active(database, request)
    return database, request, settings


def _context(index: int) -> TrustedTurnContext:
    return TrustedTurnContext(
        owner_host=HOST_ID,
        subject_id="local-user",
        session_id="session-1",
        surface="desktop",
        source_event_id=f"desktop:event-{index}",
        working_directory=None,
    )


def _receive(
    database: TaskDatabase,
    context: TrustedTurnContext,
    text: str,
    *,
    at: datetime,
) -> None:
    SurfaceEventStore(database).receive(context, text, at=at)


def test_send_requires_exact_task_access_without_revealing_task_existence(
    tmp_path: Path,
) -> None:
    database = TaskDatabase(tmp_path / "task.db")
    request = _request(tmp_path)
    _insert_active(database, request, grant_access=False)
    context = TrustedTurnContext(
        owner_host=HOST_ID,
        subject_id="other-user",
        session_id="other-session",
        surface="desktop",
        source_event_id="desktop:unauthorized",
        working_directory=None,
    )
    _receive(database, context, "unauthorized", at=NOW + timedelta(seconds=1))
    store = TaskMessageStore(database)

    with pytest.raises(TaskMessageError) as known:
        store.send(request.request_id, context, "unauthorized")
    with pytest.raises(TaskMessageError) as missing:
        store.send("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", context, "unauthorized")

    assert str(known.value) == "Task is unavailable or access is denied"
    assert str(missing.value) == str(known.value)
    with database.read() as connection:
        assert connection.execute("SELECT count(*) FROM task_messages").fetchone()[0] == 0
        assert (
            connection.execute("SELECT count(*) FROM task_revision_requests").fetchone()[0]
            == 0
        )


def test_exact_session_binding_can_authorize_task_message(
    tmp_path: Path,
) -> None:
    database = TaskDatabase(tmp_path / "task.db")
    request = _request(tmp_path)
    _insert_active(database, request, grant_access=False)
    context = _context(1)
    _receive(database, context, "bound update", at=NOW + timedelta(seconds=1))
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO task_session_bindings (
                surface, subject_id, session_id, request_id,
                parent_issue_number, bound_at
            ) VALUES (?, ?, ?, ?, 22, ?)
            """,
            (
                context.surface,
                context.subject_id,
                context.session_id,
                request.request_id,
                NOW.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            ),
        )
    with pytest.raises(
        TaskMessageError,
        match="Task is unavailable or access is denied",
    ):
        TaskMessageStore(database).send(
            request.request_id,
            context,
            "bound update",
            at=NOW + timedelta(seconds=2),
        )
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE task_session_bindings SET parent_issue_number = 21
            WHERE request_id = ? AND surface = ? AND subject_id = ?
              AND session_id = ?
            """,
            (
                request.request_id,
                context.surface,
                context.subject_id,
                context.session_id,
            ),
        )

    receipt = TaskMessageStore(database).send(
        request.request_id,
        context,
        "bound update",
        at=NOW + timedelta(seconds=2),
    )

    assert receipt.message.request_id == request.request_id


def test_send_atomically_appends_message_and_one_changing_barrier(
    tmp_path: Path,
) -> None:
    database, request, settings = _active_task(tmp_path)
    context = _context(1)
    _receive(database, context, "Apply this update.", at=NOW + timedelta(seconds=1))

    receipt = TaskMessageStore(database).send(
        request.request_id,
        context,
        "Apply this update.",
        at=NOW + timedelta(seconds=2),
    )

    assert receipt.created is True
    assert receipt.base_task_settings_hash == settings.task_settings_hash
    assert receipt.message.text == "Apply this update."
    assert (
        receipt.message.message_hash
        == hashlib.sha256(b"Apply this update.").hexdigest()
    )
    with database.read() as connection:
        assert (
            connection.execute("SELECT count(*) FROM task_messages").fetchone()[0] == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM task_revision_requests WHERE state = 'requested'"
            ).fetchone()[0]
            == 1
        )
        events = connection.execute(
            """
            SELECT event_type FROM task_events
            WHERE request_id = ? AND event_type IN ('revision_requested', 'changing')
            ORDER BY event_id
            """,
            (request.request_id,),
        ).fetchall()
    assert [row[0] for row in events] == ["revision_requested", "changing"]


def test_same_source_retry_is_exact_but_other_text_or_request_conflicts(
    tmp_path: Path,
) -> None:
    database, request, _settings = _active_task(tmp_path)
    context = _context(1)
    _receive(database, context, "first", at=NOW + timedelta(seconds=1))
    store = TaskMessageStore(database)
    first = store.send(
        request.request_id, context, "first", at=NOW + timedelta(seconds=2)
    )

    retry = store.send(request.request_id, context, "first", at=NOW + timedelta(days=1))
    assert retry.message == first.message
    assert retry.revision_request_id == first.revision_request_id
    assert retry.base_task_settings_hash == first.base_task_settings_hash
    assert retry.created is False
    with pytest.raises(TaskMessageConflictError, match="source event"):
        store.send(request.request_id, context, "different")

    other = _request(
        tmp_path,
        request_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        project_name="other-workspace",
    )
    _insert_active(database, other, issue_number=22)
    with pytest.raises(TaskMessageConflictError, match="source event"):
        store.send(other.request_id, context, "first")


def test_utf8_limit_counts_bytes_without_truncating_hangul_or_emoji(
    tmp_path: Path,
) -> None:
    database, request, _settings = _active_task(tmp_path)
    store = TaskMessageStore(database)
    allowed = "가" * ((MAX_MESSAGE_BYTES - 4) // 3) + "😀"
    assert len(allowed.encode("utf-8")) == MAX_MESSAGE_BYTES
    first = _context(1)
    _receive(database, first, allowed, at=NOW + timedelta(seconds=1))
    assert (
        store.send(
            request.request_id,
            first,
            allowed,
            at=NOW + timedelta(seconds=1),
        ).message.text
        == allowed
    )

    too_large = allowed + "a"
    second = _context(2)
    _receive(database, second, too_large, at=NOW + timedelta(seconds=2))
    with pytest.raises(TaskMessageError, match="64 KiB"):
        store.send(request.request_id, second, too_large)
    with database.read() as connection:
        assert (
            connection.execute("SELECT count(*) FROM task_messages").fetchone()[0] == 1
        )


def test_revision_count_and_total_byte_limits_are_exact(tmp_path: Path) -> None:
    database, request, _settings = _active_task(tmp_path)
    store = TaskMessageStore(database)
    chunk = "x" * (MAX_REVISION_BYTES // MAX_REVISION_MESSAGES)
    total = 0
    for index in range(MAX_REVISION_MESSAGES):
        context = _context(index)
        _receive(database, context, chunk, at=NOW + timedelta(seconds=index + 1))
        store.send(
            request.request_id,
            context,
            chunk,
            at=NOW + timedelta(seconds=index + 1),
        )
        total += len(chunk.encode("utf-8"))
    assert total < MAX_REVISION_BYTES

    overflow = _context(MAX_REVISION_MESSAGES)
    _receive(database, overflow, "x", at=NOW + timedelta(minutes=5))
    with pytest.raises(TaskMessageError, match="100 messages"):
        store.send(request.request_id, overflow, "x", at=NOW + timedelta(minutes=5))


def test_revision_total_byte_limit_is_one_mibibyte(tmp_path: Path) -> None:
    database, request, _settings = _active_task(tmp_path)
    store = TaskMessageStore(database)
    chunk = "x" * MAX_MESSAGE_BYTES
    for index in range(MAX_REVISION_BYTES // MAX_MESSAGE_BYTES):
        context = _context(index)
        _receive(database, context, chunk, at=NOW + timedelta(seconds=index + 1))
        store.send(
            request.request_id,
            context,
            chunk,
            at=NOW + timedelta(seconds=index + 1),
        )

    overflow = _context(99)
    _receive(database, overflow, "x", at=NOW + timedelta(minutes=5))
    with pytest.raises(TaskMessageError, match="1 MiB"):
        store.send(request.request_id, overflow, "x", at=NOW + timedelta(minutes=5))


def test_two_concurrent_first_messages_create_one_pending_revision(
    tmp_path: Path,
) -> None:
    database, request, _settings = _active_task(tmp_path)
    contexts = (_context(1), _context(2))
    for index, context in enumerate(contexts):
        _receive(
            database, context, f"message-{index}", at=NOW + timedelta(seconds=index + 1)
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = tuple(
            pool.map(
                lambda pair: TaskMessageStore(database).send(
                    request.request_id,
                    pair[1],
                    f"message-{pair[0]}",
                    at=NOW + timedelta(seconds=10 + pair[0]),
                ),
                enumerate(contexts),
            )
        )

    assert len({item.revision_request_id for item in receipts}) == 1
    with database.read() as connection:
        assert (
            connection.execute("SELECT count(*) FROM task_messages").fetchone()[0] == 2
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM task_revision_requests"
            ).fetchone()[0]
            == 1
        )


def test_terminal_or_stopping_task_rejects_without_partial_message(
    tmp_path: Path,
) -> None:
    database, request, settings = _active_task(tmp_path)
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO task_events (
                request_id, task_settings_hash, event_type, event_key,
                event_json, occurred_at
            ) VALUES (?, ?, 'stop_requested', 'stop:test', '{}', ?)
            """,
            (request.request_id, settings.task_settings_hash, NOW.isoformat()),
        )
    context = _context(1)
    _receive(database, context, "late", at=NOW + timedelta(seconds=1))

    with pytest.raises(TaskMessageError, match="not messageable"):
        TaskMessageStore(database).send(request.request_id, context, "late")
    with database.read() as connection:
        assert (
            connection.execute("SELECT count(*) FROM task_messages").fetchone()[0] == 0
        )


def test_unconfirmed_revision_has_no_worker_packet(tmp_path: Path) -> None:
    database, request, settings = _active_task(tmp_path)
    context = _context(1)
    _receive(database, context, "secret until confirm", at=NOW + timedelta(seconds=1))
    store = TaskMessageStore(database)
    store.send(request.request_id, context, "secret until confirm")

    with pytest.raises(TaskMessageError, match="changing") as error:
        store.build_packet(request.request_id, settings.task_settings_hash)
    assert "secret until confirm" not in str(error.value)
