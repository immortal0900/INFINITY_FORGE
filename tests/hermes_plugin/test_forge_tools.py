from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path

import pytest

from forge.ops.forge_tools import (
    ForgeToolAccessDenied,
    ForgeToolError,
    ForgeToolService,
    TrustedToolEnvelope,
)
from forge.ops.surface_events import TrustedTurnContext, surface_event_payload_hash
from forge.ops.task_database import TaskDatabase
from forge.ops.task_options import MergeMode, TaskFlow
from forge.ops.task_projects import TaskProject
from forge.ops.task_settings import TaskContent
from forge.ops.task_settings_v2 import TaskRequestV2, TaskSettingsV2


NOW = datetime(2026, 7, 19, 6, 0, tzinfo=UTC)
NOW_TEXT = "2026-07-19T06:00:00.000000Z"
OWNER_HOST = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER_HOST = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _seed_active(
    database: TaskDatabase,
    tmp_path: Path,
    *,
    request_id: str,
    issue_number: int,
    repository: str,
    owner_host: str = OWNER_HOST,
    subject: str = "user-1",
    grant_access: bool = True,
    bind_session: str | None = None,
) -> TaskRequestV2:
    workspace = tmp_path / repository.replace("/", "-")
    workspace.mkdir(parents=True, exist_ok=True)
    project = TaskProject.create(
        repository=repository,
        workspace=str(workspace.resolve()),
        remote_name="origin",
        base_branch="main",
        base_commit="a" * 40,
        host_id=owner_host,
    )
    request = TaskRequestV2.create(
        request_id=request_id,
        management_repository="management/forge",
        task_content=TaskContent(
            title=f"Task {issue_number}",
            description="Implement the selected change.",
            acceptance_criteria=("Keep the contract exact.",),
        ),
        task_flow=TaskFlow.BUILD_REVIEW,
        merge_mode=MergeMode.MANUAL,
        merge_order=None,
        projects=(project,),
        task_owner_host=owner_host,
        confirmed_by=subject,
        confirmed_at=NOW,
    )
    settings = TaskSettingsV2.create(
        request=request,
        parent_issue_number=issue_number,
    )
    request_payload = json.loads(request.to_json())
    settings_payload = json.loads(settings.to_json())
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
                request.request_id,
                request.request_hash,
                settings.format_version,
                settings.to_json(),
                settings.management_repository,
                settings.parent_issue_number,
                settings.task_owner_host,
                settings_payload["confirmed_at"],
            ),
        )
        connection.execute(
            """
            INSERT INTO task_projects (
                request_id, project_id, task_settings_hash, project_json,
                state, root_card_id, pr_url, updated_at
            ) VALUES (?, ?, ?, ?, 'ready', ?, NULL, ?)
            """,
            (
                request.request_id,
                project.project_id,
                settings.task_settings_hash,
                _canonical(request_payload["projects"][0]),
                f"root-{issue_number}",
                NOW_TEXT,
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
                    NOW_TEXT,
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
                (request.request_id, subject, subject, NOW_TEXT),
            )
        if bind_session is not None:
            connection.execute(
                """
                INSERT INTO task_session_bindings (
                    surface, subject_id, session_id, request_id,
                    parent_issue_number, bound_at
                ) VALUES ('desktop', ?, ?, ?, ?, ?)
                """,
                (
                    subject,
                    bind_session,
                    request.request_id,
                    issue_number,
                    NOW_TEXT,
                ),
            )
    return request


def _context(index: int, *, owner_host: str = OWNER_HOST) -> TrustedTurnContext:
    return TrustedTurnContext(
        owner_host=owner_host,
        subject_id="user-1",
        session_id="session-1",
        surface="desktop",
        source_event_id=f"desktop:event-{index}",
        working_directory="C:/work",
    )


def _envelope(
    index: int,
    payload: str,
    *,
    owner_host: str = OWNER_HOST,
    payload_hash: str | None = None,
) -> TrustedToolEnvelope:
    context = _context(index, owner_host=owner_host)
    return TrustedToolEnvelope(
        context=context,
        source_payload=payload,
        source_payload_hash=(
            payload_hash
            if payload_hash is not None
            else surface_event_payload_hash(context, payload)
        ),
    )


def _write_counts(database: TaskDatabase) -> tuple[int, int, int, int]:
    with database.read() as connection:
        return tuple(
            int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in (
                "surface_events",
                "task_messages",
                "task_revision_requests",
                "task_stop_requests",
            )
        )


def test_list_and_status_are_read_only_and_include_project_state(tmp_path: Path) -> None:
    database = TaskDatabase(tmp_path / "tasks.db")
    _seed_active(
        database,
        tmp_path,
        request_id="11111111-1111-4111-8111-111111111111",
        issue_number=21,
        repository="owner/api",
    )
    service = ForgeToolService(database)
    envelope = _envelope(1, "show my work")
    before = _write_counts(database)

    listed = service.list_tasks(envelope)
    status = service.task_status(envelope, task_number=21)

    assert listed["status"] == "ok"
    assert listed["tasks"][0]["task_number"] == 21
    assert status["task"]["state"] == "active"
    assert status["task"]["projects"] == [
        {
            "pr_url": None,
            "project_id": status["task"]["projects"][0]["project_id"],
            "repository": "owner/api",
            "state": "ready",
            "waiting_reason": None,
        }
    ]
    assert _write_counts(database) == before


def test_send_uses_exact_hidden_user_turn_and_ignores_model_hint(tmp_path: Path) -> None:
    database = TaskDatabase(tmp_path / "tasks.db")
    request = _seed_active(
        database,
        tmp_path,
        request_id="11111111-1111-4111-8111-111111111111",
        issue_number=21,
        repository="owner/api",
    )
    service = ForgeToolService(database)

    result = service.send_to_task(
        _envelope(2, "작업자에게 이 정확한 문장을 전달해"),
        task_number=21,
        message_hint="forged middleware replacement",
    )

    assert result["status"] == "sent"
    assert result["task_number"] == 21
    with database.read() as connection:
        message = connection.execute(
            "SELECT request_id, text FROM task_messages"
        ).fetchone()
        assert tuple(message) == (
            request.request_id,
            "작업자에게 이 정확한 문장을 전달해",
        )
        assert connection.execute(
            "SELECT count(*) FROM task_revision_requests"
        ).fetchone()[0] == 1


def test_mutation_rejects_forged_hidden_hash_before_any_write(tmp_path: Path) -> None:
    database = TaskDatabase(tmp_path / "tasks.db")
    _seed_active(
        database,
        tmp_path,
        request_id="11111111-1111-4111-8111-111111111111",
        issue_number=21,
        repository="owner/api",
    )
    service = ForgeToolService(database)
    before = _write_counts(database)

    with pytest.raises(ForgeToolError, match="trusted user turn"):
        service.send_to_task(
            _envelope(3, "real payload", payload_hash="f" * 64),
            task_number=21,
        )

    assert _write_counts(database) == before


def test_task_selection_prefers_explicit_then_session_and_chooser_writes_nothing(
    tmp_path: Path,
) -> None:
    database = TaskDatabase(tmp_path / "tasks.db")
    first = _seed_active(
        database,
        tmp_path,
        request_id="11111111-1111-4111-8111-111111111111",
        issue_number=21,
        repository="owner/api",
    )
    second = _seed_active(
        database,
        tmp_path,
        request_id="22222222-2222-4222-8222-222222222222",
        issue_number=22,
        repository="owner/web",
    )
    service = ForgeToolService(database)
    before = _write_counts(database)

    chooser = service.send_to_task(_envelope(4, "ambiguous update"))

    assert chooser["status"] == "choose_task"
    assert [choice["task_number"] for choice in chooser["choices"]] == [21, 22]
    assert _write_counts(database) == before

    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO task_session_bindings (
                surface, subject_id, session_id, request_id,
                parent_issue_number, bound_at
            ) VALUES ('desktop', 'user-1', 'session-1', ?, 21, ?)
            """,
            (first.request_id, NOW_TEXT),
        )
    bound = service.send_to_task(_envelope(5, "bound update"))
    explicit = service.send_to_task(
        _envelope(6, "explicit update"),
        task_number=22,
    )

    assert bound["task_number"] == 21
    assert explicit["task_number"] == 22
    with database.read() as connection:
        assert {
            str(row[0])
            for row in connection.execute("SELECT request_id FROM task_messages")
        } == {first.request_id, second.request_id}


def test_explicit_access_and_owner_host_fail_without_disclosure(tmp_path: Path) -> None:
    database = TaskDatabase(tmp_path / "tasks.db")
    _seed_active(
        database,
        tmp_path,
        request_id="11111111-1111-4111-8111-111111111111",
        issue_number=21,
        repository="owner/api",
        owner_host=OTHER_HOST,
    )
    service = ForgeToolService(database)

    with pytest.raises(ForgeToolAccessDenied) as cross_host:
        service.task_status(_envelope(7, "status"), task_number=21)
    with pytest.raises(ForgeToolAccessDenied) as missing:
        service.task_status(_envelope(8, "status"), task_number=999)

    assert str(cross_host.value) == "Task is unavailable or access is denied"
    assert str(missing.value) == str(cross_host.value)


def test_terminal_task_send_is_rejected_without_message_write(tmp_path: Path) -> None:
    database = TaskDatabase(tmp_path / "tasks.db")
    request = _seed_active(
        database,
        tmp_path,
        request_id="11111111-1111-4111-8111-111111111111",
        issue_number=21,
        repository="owner/api",
    )
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO task_events (
                request_id, event_type, event_key, event_json, occurred_at
            ) VALUES (?, 'merged', 'merged', '{}', ?)
            """,
            (request.request_id, NOW_TEXT),
        )
    service = ForgeToolService(database)
    before = _write_counts(database)

    with pytest.raises(ForgeToolError):
        service.send_to_task(_envelope(9, "too late"), task_number=21)

    assert _write_counts(database) == before


def test_stop_commits_barrier_then_calls_injected_reconcile_trigger(
    tmp_path: Path,
) -> None:
    database = TaskDatabase(tmp_path / "tasks.db")
    request = _seed_active(
        database,
        tmp_path,
        request_id="11111111-1111-4111-8111-111111111111",
        issue_number=21,
        repository="owner/api",
    )
    triggered: list[str] = []
    service = ForgeToolService(
        database,
        reconcile_trigger=lambda receipt: triggered.append(receipt.stop_request_id),
    )

    result = service.stop_task(_envelope(10, "stop this task"), task_number=21)

    assert result["status"] == "stop_requested"
    assert triggered == [result["stop_request_id"]]
    with database.read() as connection:
        stop = connection.execute(
            "SELECT request_id, state FROM task_stop_requests"
        ).fetchone()
        assert tuple(stop) == (request.request_id, "stopping")
        assert connection.execute(
            "SELECT count(*) FROM task_events WHERE event_type = 'stopping'"
        ).fetchone()[0] == 1
