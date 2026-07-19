from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from forge.ops.surface_events import SurfaceEventStore, TrustedTurnContext
from forge.ops.task_database import TaskDatabase
from forge.ops.task_messages import TaskMessageError, TaskMessageStore
from forge.ops.task_options import MergeMode, TaskFlow
from forge.ops.task_projects import TaskProject
from forge.ops.task_revisions import TaskRevisionError, TaskRevisionService
from forge.ops.task_settings import TaskContent
from forge.ops.task_settings_v2 import TaskRequestV2, TaskSettingsV2


HOST_ID = "d6f70d5d-6482-45f5-80d2-219ec2ad4d19"
NOW = datetime(2026, 7, 19, 4, 5, 6, 123456, tzinfo=UTC)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _project(tmp_path: Path) -> TaskProject:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    return TaskProject.create(
        repository="example/project",
        workspace=str(workspace),
        remote_name="origin",
        base_branch="main",
        base_commit="a" * 40,
        host_id=HOST_ID,
    )


def _new_request(
    project: TaskProject,
    *,
    request_id: str,
    description: str,
    confirmed_at: datetime,
    replaces_request_id: str | None = None,
) -> TaskRequestV2:
    return TaskRequestV2.create(
        request_id=request_id,
        management_repository="example/infinity-forge",
        task_content=TaskContent(
            title="Task",
            description=description,
            acceptance_criteria=("Pass.",),
        ),
        task_flow=TaskFlow.BUILD,
        merge_mode=MergeMode.MANUAL,
        merge_order=None,
        projects=(project,),
        task_owner_host=HOST_ID,
        confirmed_by="local-user",
        confirmed_at=confirmed_at,
        replaces_request_id=replaces_request_id,
    )


def _seed_active(
    tmp_path: Path,
) -> tuple[TaskDatabase, TaskRequestV2, TaskSettingsV2, TaskProject]:
    project = _project(tmp_path)
    request = _new_request(
        project,
        request_id="4485be21-2a8f-41b8-a2a2-e25722df284e",
        description="base",
        confirmed_at=NOW,
    )
    settings = TaskSettingsV2.create(request=request, parent_issue_number=21)
    database = TaskDatabase(tmp_path / "task.db")
    payload = json.loads(request.to_json())
    timestamp = payload["confirmed_at"]
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
                timestamp,
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
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO task_projects (
                request_id, project_id, task_settings_hash, project_json,
                state, root_card_id, updated_at
            ) VALUES (?, ?, ?, ?, 'ready', 'root-card', ?)
            """,
            (
                request.request_id,
                project.project_id,
                settings.task_settings_hash,
                _json(payload["projects"][0]),
                timestamp,
            ),
        )
        common = _json({"task_settings_hash": settings.task_settings_hash})
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
                    timestamp,
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
                _json(
                    {
                        "project_ids": [project.project_id],
                        "task_settings_hash": settings.task_settings_hash,
                    }
                ),
                timestamp,
            ),
        )
    return database, request, settings, project


def _context(index: int) -> TrustedTurnContext:
    return TrustedTurnContext(
        owner_host=HOST_ID,
        subject_id="local-user",
        session_id="session-1",
        surface="desktop",
        source_event_id=f"desktop:revision-{index}",
        working_directory=None,
    )


def _send(
    database: TaskDatabase,
    request: TaskRequestV2,
    index: int,
    text: str,
    *,
    at: datetime,
):
    context = _context(index)
    SurfaceEventStore(database).receive(context, text, at=at)
    return TaskMessageStore(database).send(request.request_id, context, text, at=at)


def _stage_replacement(
    database: TaskDatabase,
    request: TaskRequestV2,
) -> None:
    payload = json.loads(request.to_json())
    timestamp = payload["confirmed_at"]
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
                timestamp,
                request.replaces_request_id,
            ),
        )
        for project, raw_project in zip(
            request.projects, payload["projects"], strict=True
        ):
            connection.execute(
                """
                INSERT INTO task_projects (
                    request_id, project_id, task_settings_hash, project_json,
                    state, root_card_id, updated_at
                ) VALUES (?, ?, NULL, ?, 'prepared', NULL, ?)
                """,
                (
                    request.request_id,
                    project.project_id,
                    _json(raw_project),
                    timestamp,
                ),
            )
        connection.execute(
            """
            INSERT INTO task_events (
                request_id, event_type, event_key, event_json, occurred_at
            ) VALUES (?, 'request_prepared', 'request_prepared', ?, ?)
            """,
            (
                request.request_id,
                _json({"request_hash": request.request_hash}),
                timestamp,
            ),
        )


def _bind_replacement(
    database: TaskDatabase,
    request: TaskRequestV2,
    *,
    root_card_id: str,
    parent_issue_number: int = 21,
) -> None:
    occurred_at = (NOW + timedelta(minutes=2)).isoformat().replace("+00:00", "Z")
    project = request.projects[0]
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE task_projects
            SET state = 'bound', root_card_id = ?, updated_at = ?
            WHERE request_id = ? AND state = 'prepared'
              AND root_card_id IS NULL AND task_settings_hash IS NULL
            """,
            (root_card_id, occurred_at, request.request_id),
        )
        connection.execute(
            """
            INSERT INTO task_events (
                request_id, event_type, event_key, event_json, occurred_at
            ) VALUES (?, 'parent_issue_bound', 'parent_issue_bound', ?, ?)
            """,
            (
                request.request_id,
                _json(
                    {
                        "parent_issue_number": parent_issue_number,
                        "request_hash": request.request_hash,
                    }
                ),
                occurred_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO task_events (
                request_id, project_id, event_type, event_key,
                event_json, occurred_at
            ) VALUES (?, ?, 'project_item_bound', ?, ?, ?)
            """,
            (
                request.request_id,
                project.project_id,
                f"project_item_bound:{project.project_id}",
                _json(
                    {
                        "idempotency_key": (
                            f"forge-task-v2:{request.request_id}:"
                            f"{project.project_id}:build"
                        ),
                        "root_card_id": root_card_id,
                    }
                ),
                occurred_at,
            ),
        )


def test_preview_is_invalidated_when_another_message_is_appended(
    tmp_path: Path,
) -> None:
    database, base, _settings, project = _seed_active(tmp_path)
    receipt = _send(database, base, 1, "first", at=NOW + timedelta(seconds=1))
    replacement = _new_request(
        project,
        request_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        description="base plus first",
        confirmed_at=NOW + timedelta(minutes=1),
        replaces_request_id=base.request_id,
    )
    _stage_replacement(database, replacement)
    service = TaskRevisionService(database)
    preview = service.prepare_preview(receipt.revision_request_id, replacement)

    _send(database, base, 2, "second", at=NOW + timedelta(seconds=2))

    with pytest.raises(TaskRevisionError, match="preview"):
        service.confirm(
            receipt.revision_request_id,
            replacement,
            preview_hash=preview.preview_hash,
        )
    with database.read() as connection:
        assert (
            connection.execute(
                "SELECT preview_hash FROM task_revision_requests"
            ).fetchone()[0]
            is None
        )
        assert (
            connection.execute(
                "SELECT state FROM task_projects WHERE request_id = ?",
                (replacement.request_id,),
            ).fetchone()[0]
            == "cancelled"
        )

    refreshed = _new_request(
        project,
        request_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        description="base plus first and second",
        confirmed_at=NOW + timedelta(minutes=2),
        replaces_request_id=base.request_id,
    )
    _stage_replacement(database, refreshed)
    refreshed_preview = service.prepare_preview(
        receipt.revision_request_id,
        refreshed,
    )
    assert [message.text for message in refreshed_preview.messages] == [
        "first",
        "second",
    ]


def test_cancel_then_resume_reactivates_base_without_reusing_rejected_messages(
    tmp_path: Path,
) -> None:
    database, base, settings, _project_value = _seed_active(tmp_path)
    first = _send(database, base, 1, "cancel me", at=NOW + timedelta(seconds=1))
    service = TaskRevisionService(database)

    service.cancel(
        first.revision_request_id,
        reason="user cancelled",
        at=NOW + timedelta(seconds=2),
    )
    service.resume(first.revision_request_id, at=NOW + timedelta(seconds=3))
    # Resume clears the old append-only revision barrier for the exact base settings.
    service.require_active(base.request_id, settings.task_settings_hash)
    second = _send(database, base, 2, "new revision", at=NOW + timedelta(seconds=4))

    assert second.revision_request_id != first.revision_request_id
    pending = TaskMessageStore(database).pending_for_revision(
        second.revision_request_id
    )
    assert [message.text for message in pending] == ["new revision"]
    with database.read() as connection:
        assert (
            connection.execute(
                """
            SELECT event_type FROM task_message_events
            WHERE message_id = ? ORDER BY occurred_at, message_event_id
            """,
                (first.message.message_id,),
            ).fetchall()[0][0]
            == "rejected"
        )


def test_confirm_requires_exact_prepared_linear_replacement(tmp_path: Path) -> None:
    database, base, _settings, project = _seed_active(tmp_path)
    receipt = _send(database, base, 1, "change", at=NOW + timedelta(seconds=1))
    service = TaskRevisionService(database)
    replacement = _new_request(
        project,
        request_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        description="replacement",
        confirmed_at=NOW + timedelta(minutes=1),
        replaces_request_id=base.request_id,
    )
    preview_hash = hashlib.sha256(b"not-the-stored-preview").hexdigest()
    with pytest.raises(TaskRevisionError, match="staged|prepared"):
        service.confirm(
            receipt.revision_request_id, replacement, preview_hash=preview_hash
        )

    _stage_replacement(database, replacement)
    with database.transaction() as connection:
        source_payload_hash = connection.execute(
            "SELECT payload_hash FROM surface_events WHERE source_event_id = ?",
            (receipt.message.source_event_id,),
        ).fetchone()[0]
        connection.execute(
            """
            UPDATE task_events SET event_json = ?
            WHERE request_id = ? AND event_type = 'request_prepared'
            """,
            (
                _json(
                    {
                        "request_hash": replacement.request_hash,
                        "source_event_id": receipt.message.source_event_id,
                        "source_payload_hash": source_payload_hash,
                    }
                ),
                replacement.request_id,
            ),
        )
    preview = service.prepare_preview(receipt.revision_request_id, replacement)
    with database.transaction() as connection:
        with pytest.raises(TaskRevisionError, match="requires one confirmed"):
            service.require_replacement_write_on_connection(
                connection,
                replacement.request_id,
            )
    confirmed = service.confirm(
        receipt.revision_request_id,
        replacement,
        preview_hash=preview.preview_hash,
        at=NOW + timedelta(minutes=1, seconds=1),
    )
    assert confirmed.state == "confirmed"
    assert confirmed.replacement_request_id == replacement.request_id

    fork = _new_request(
        project,
        request_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        description="fork",
        confirmed_at=NOW + timedelta(minutes=2),
        replaces_request_id=base.request_id,
    )
    _stage_replacement(database, fork)
    with pytest.raises(TaskRevisionError, match="fork|linear|confirmed"):
        service.prepare_preview(receipt.revision_request_id, fork)


def test_confirm_before_activation_does_not_expose_packet_then_activation_does(
    tmp_path: Path,
) -> None:
    database, base, _base_settings, project = _seed_active(tmp_path)
    first = _send(database, base, 1, "first", at=NOW + timedelta(seconds=1))
    second = _send(database, base, 2, "second", at=NOW + timedelta(seconds=1))
    assert first.message.created_at == second.message.created_at
    replacement = _new_request(
        project,
        request_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        description="replacement includes both messages",
        confirmed_at=NOW + timedelta(minutes=1),
        replaces_request_id=base.request_id,
    )
    _stage_replacement(database, replacement)
    revisions = TaskRevisionService(database)
    preview = revisions.prepare_preview(first.revision_request_id, replacement)
    revisions.confirm(
        first.revision_request_id,
        replacement,
        preview_hash=preview.preview_hash,
        at=NOW + timedelta(minutes=1, seconds=1),
    )
    with database.transaction() as connection:
        assert (
            revisions.require_replacement_write_on_connection(
                connection,
                replacement.request_id,
            ).revision_request_id
            == first.revision_request_id
        )

    with pytest.raises(TaskMessageError, match="not active"):
        TaskMessageStore(database).build_packet(
            replacement.request_id,
            "0" * 64,
        )

    replacement_settings = TaskSettingsV2.create(
        request=replacement,
        parent_issue_number=21,
    )
    _bind_replacement(database, replacement, root_card_id="replacement-root")
    with database.transaction() as connection:
        assert (
            revisions.require_replacement_write_on_connection(
                connection,
                replacement.request_id,
            ).revision_request_id
            == first.revision_request_id
        )
    revisions.activate_confirmed(
        first.revision_request_id,
        replacement_settings,
        at=NOW + timedelta(minutes=2),
    )
    assert (
        revisions.activate_confirmed(
            first.revision_request_id,
            replacement_settings,
            at=NOW + timedelta(minutes=3),
        )
        == replacement_settings
    )
    assert (
        revisions.confirm(
            first.revision_request_id,
            replacement,
            preview_hash=preview.preview_hash,
            at=NOW + timedelta(minutes=4),
        ).state
        == "confirmed"
    )

    packet = TaskMessageStore(database).build_packet(
        replacement.request_id,
        replacement_settings.task_settings_hash,
    )
    assert [item.message_id for item in packet.messages] == sorted(
        [first.message.message_id, second.message.message_id],
        key=lambda message_id: next(
            (message.created_at, message.message_id)
            for message in (first.message, second.message)
            if message.message_id == message_id
        ),
    )
    assert (
        packet.packet_hash
        == hashlib.sha256(packet.to_json().encode("utf-8")).hexdigest()
    )
    assert packet.to_json() == _json(json.loads(packet.to_json()))
    assert "local-user" not in packet.to_json()
    assert "session-1" not in packet.to_json()
    forged = replace(packet, messages=packet.messages[:1])
    forged = replace(
        forged,
        packet_hash=hashlib.sha256(forged.to_json().encode("utf-8")).hexdigest(),
    )
    with pytest.raises(TaskMessageError, match="omits|adds"):
        TaskMessageStore(database).record_included(
            forged,
            worker_task_id="forged-worker",
            run_id="forged-run",
            at=NOW + timedelta(minutes=4),
        )


def test_activation_failure_rolls_back_until_every_external_binding_is_exact(
    tmp_path: Path,
) -> None:
    database, base, _base_settings, project = _seed_active(tmp_path)
    receipt = _send(database, base, 1, "change", at=NOW + timedelta(seconds=1))
    replacement = _new_request(
        project,
        request_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        description="replacement",
        confirmed_at=NOW + timedelta(minutes=1),
        replaces_request_id=base.request_id,
    )
    _stage_replacement(database, replacement)
    revisions = TaskRevisionService(database)
    preview = revisions.prepare_preview(receipt.revision_request_id, replacement)
    revisions.confirm(
        receipt.revision_request_id,
        replacement,
        preview_hash=preview.preview_hash,
        at=NOW + timedelta(minutes=1, seconds=1),
    )
    settings = TaskSettingsV2.create(request=replacement, parent_issue_number=21)

    with pytest.raises(TaskRevisionError, match="root is not bound"):
        revisions.activate_confirmed(
            receipt.revision_request_id,
            settings,
            at=NOW + timedelta(minutes=2),
        )
    with database.read() as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM task_settings_v2 WHERE request_id = ?",
                (replacement.request_id,),
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT event_type FROM task_events WHERE request_id = ? ORDER BY event_id DESC LIMIT 1",
                (base.request_id,),
            ).fetchone()[0]
            == "changing"
        )


def test_worker_included_is_not_ack_and_pending_blocks_result_acceptance(
    tmp_path: Path,
) -> None:
    database, base, _settings, project = _seed_active(tmp_path)
    receipt = _send(database, base, 1, "apply", at=NOW + timedelta(seconds=1))
    replacement = _new_request(
        project,
        request_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        description="replacement",
        confirmed_at=NOW + timedelta(minutes=1),
        replaces_request_id=base.request_id,
    )
    _stage_replacement(database, replacement)
    revisions = TaskRevisionService(database)
    preview = revisions.prepare_preview(receipt.revision_request_id, replacement)
    revisions.confirm(
        receipt.revision_request_id,
        replacement,
        preview_hash=preview.preview_hash,
        at=NOW + timedelta(minutes=1, seconds=1),
    )
    settings = TaskSettingsV2.create(request=replacement, parent_issue_number=21)
    _bind_replacement(database, replacement, root_card_id="root-2")
    revisions.activate_confirmed(
        receipt.revision_request_id, settings, at=NOW + timedelta(minutes=2)
    )
    store = TaskMessageStore(database)
    packet = store.build_packet(replacement.request_id, settings.task_settings_hash)

    store.record_included(
        packet,
        worker_task_id="worker-1",
        run_id="run-1",
        at=NOW + timedelta(minutes=3),
    )
    with pytest.raises(TaskMessageError, match="pending message"):
        store.require_result_acknowledged(
            packet, worker_task_id="worker-1", run_id="run-1"
        )
    store.record_ack(
        packet,
        message_id=receipt.message.message_id,
        outcome="applied",
        worker_task_id="worker-1",
        run_id="run-1",
        reason="implemented",
        at=NOW + timedelta(minutes=4),
    )
    store.require_result_acknowledged(packet, worker_task_id="worker-1", run_id="run-1")
    # Exact retry is idempotent, but a changed outcome is not.
    store.record_ack(
        packet,
        message_id=receipt.message.message_id,
        outcome="applied",
        worker_task_id="worker-1",
        run_id="run-1",
        reason="implemented",
        at=NOW + timedelta(minutes=4),
    )
    with pytest.raises(TaskMessageError, match="immutable"):
        store.record_ack(
            packet,
            message_id=receipt.message.message_id,
            outcome="rejected",
            worker_task_id="worker-1",
            run_id="run-1",
            reason="changed",
        )


def test_stop_barrier_wins_before_confirm_resume_and_activation(tmp_path: Path) -> None:
    database, base, settings, project = _seed_active(tmp_path)
    receipt = _send(database, base, 1, "change", at=NOW + timedelta(seconds=1))
    replacement = _new_request(
        project,
        request_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        description="replacement",
        confirmed_at=NOW + timedelta(minutes=1),
        replaces_request_id=base.request_id,
    )
    _stage_replacement(database, replacement)
    service = TaskRevisionService(database)
    preview = service.prepare_preview(receipt.revision_request_id, replacement)
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO task_events (
                request_id, task_settings_hash, event_type, event_key,
                event_json, occurred_at
            ) VALUES (?, ?, 'stop_requested', 'stop:test', '{}', ?)
            """,
            (base.request_id, settings.task_settings_hash, NOW.isoformat()),
        )
        with pytest.raises(TaskRevisionError, match="Stop"):
            service.confirm_on_connection(
                connection,
                receipt.revision_request_id,
                replacement,
                preview_hash=preview.preview_hash,
            )
        with pytest.raises(TaskRevisionError, match="Stop"):
            service.resume_on_connection(connection, receipt.revision_request_id)


def test_confirmed_revision_is_cancelled_when_stop_wins_before_activation(
    tmp_path: Path,
) -> None:
    database, base, settings, project = _seed_active(tmp_path)
    receipt = _send(database, base, 1, "change", at=NOW + timedelta(seconds=1))
    replacement = _new_request(
        project,
        request_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        description="replacement",
        confirmed_at=NOW + timedelta(minutes=1),
        replaces_request_id=base.request_id,
    )
    _stage_replacement(database, replacement)
    service = TaskRevisionService(database)
    preview = service.prepare_preview(receipt.revision_request_id, replacement)
    service.confirm(
        receipt.revision_request_id,
        replacement,
        preview_hash=preview.preview_hash,
        at=NOW + timedelta(minutes=1, seconds=1),
    )

    stopped_at = NOW + timedelta(minutes=2)
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO task_events (
                request_id, task_settings_hash, event_type, event_key,
                event_json, occurred_at
            ) VALUES (?, ?, 'stop_requested', 'stop:after-confirm', '{}', ?)
            """,
            (
                base.request_id,
                settings.task_settings_hash,
                stopped_at.isoformat().replace("+00:00", "Z"),
            ),
        )
        cancelled = service.cancel_for_stop_on_connection(
            connection,
            receipt.revision_request_id,
            reason="Task stop requested",
            at=stopped_at,
        )
        assert cancelled.state == "cancelled"

    with database.read() as connection:
        assert (
            connection.execute(
                "SELECT state FROM task_projects WHERE request_id = ?",
                (replacement.request_id,),
            ).fetchone()[0]
            == "cancelled"
        )
        assert (
            connection.execute(
                "SELECT event_type FROM task_message_events WHERE message_id = ?",
                (receipt.message.message_id,),
            ).fetchone()[0]
            == "rejected"
        )
        assert (
            connection.execute(
                "SELECT event_type FROM task_events WHERE request_id = ? ORDER BY event_id DESC LIMIT 1",
                (base.request_id,),
            ).fetchone()[0]
            == "stop_requested"
        )
    with pytest.raises(TaskRevisionError, match="state cancelled"):
        service.activate_confirmed(
            receipt.revision_request_id,
            TaskSettingsV2.create(request=replacement, parent_issue_number=21),
            at=NOW + timedelta(minutes=3),
        )
