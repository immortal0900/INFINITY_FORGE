from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from forge.ops.surface_events import SurfaceEventStore, TrustedTurnContext
from forge.ops.task_database import TaskDatabase
from forge.ops.task_messages import TaskMessageStore
from forge.ops.task_options import MergeMode, TaskFlow
from forge.ops.task_projects import TaskProject
from forge.ops.task_revisions import TaskRevisionService
from forge.ops.task_settings import TaskContent
from forge.ops.task_settings_v2 import TaskRequestV2, TaskSettingsV2
from forge.ops.task_stop import (
    TaskStopAccessDenied,
    TaskStopError,
    TaskStopOwnerHostMismatch,
    TaskStopService,
    TaskStopUnsupported,
)


NOW = datetime(2026, 7, 19, 3, 0, tzinfo=UTC)
NOW_TEXT = "2026-07-19T03:00:00Z"
OWNER_HOST = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER_HOST = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _request(
    tmp_path: Path,
    *,
    request_id: str | None = None,
    owner_host: str = OWNER_HOST,
    replaces_request_id: str | None = None,
    repository: str = "owner/project",
) -> TaskRequestV2:
    workspace = tmp_path / f"project-{request_id or uuid4()}"
    workspace.mkdir(parents=True, exist_ok=True)
    project = TaskProject.create(
        repository=repository,
        workspace=str(workspace.resolve()),
        remote_name="origin",
        base_branch="main",
        base_commit="a" * 40,
        host_id=owner_host,
    )
    return TaskRequestV2.create(
        request_id=request_id or str(uuid4()),
        management_repository="management/forge",
        task_content=TaskContent(
            title=f"Task for {repository}",
            description="Implement the selected change.",
            acceptance_criteria=("The requested change is verified.",),
        ),
        task_flow=TaskFlow.BUILD_REVIEW,
        merge_mode=MergeMode.SAFE_AUTO,
        merge_order=None,
        projects=(project,),
        task_owner_host=owner_host,
        confirmed_by="user-1",
        confirmed_at=NOW,
        replaces_request_id=replaces_request_id,
    )


def _insert_event(
    connection,
    request_id: str,
    event_type: str,
    event_key: str,
    payload: object,
    *,
    settings_hash: str | None = None,
    project_id: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO task_events (
            request_id, task_settings_hash, project_id, event_type,
            event_key, event_json, occurred_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            request_id,
            settings_hash,
            project_id,
            event_type,
            event_key,
            _json(payload),
            NOW_TEXT,
        ),
    )


def _seed_request(
    database: TaskDatabase,
    request: TaskRequestV2,
    *,
    issue_number: int | None = None,
    active: bool = False,
) -> TaskSettingsV2 | None:
    raw_request = json.loads(request.to_json())
    raw_project = raw_request["projects"][0]
    settings = (
        TaskSettingsV2.create(request=request, parent_issue_number=issue_number)
        if active and issue_number is not None
        else None
    )
    project_state = "ready" if settings is not None else (
        "bound" if issue_number is not None else "prepared"
    )
    root_card_id = f"root-{request.request_id}" if issue_number is not None else None
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
                raw_request["confirmed_at"],
                request.replaces_request_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO task_projects (
                request_id, project_id, task_settings_hash, project_json,
                state, root_card_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request.request_id,
                request.projects[0].project_id,
                None,
                _json(raw_project),
                project_state,
                root_card_id,
                NOW_TEXT,
            ),
        )
        _insert_event(
            connection,
            request.request_id,
            "request_prepared",
            "request_prepared",
            {"request_hash": request.request_hash},
        )
        if issue_number is not None:
            _insert_event(
                connection,
                request.request_id,
                "parent_issue_bound",
                "parent_issue_bound",
                {
                    "parent_issue_number": issue_number,
                    "request_hash": request.request_hash,
                },
            )
            _insert_event(
                connection,
                request.request_id,
                "project_item_bound",
                f"project_item_bound:{request.projects[0].project_id}",
                {
                    "idempotency_key": (
                        f"forge-task-v2:{request.request_id}:"
                        f"{request.projects[0].project_id}:build"
                    ),
                    "root_card_id": root_card_id,
                },
            )
        if settings is not None:
            raw_settings = json.loads(settings.to_json())
            connection.execute(
                """
                INSERT INTO task_settings_v2 (
                    task_settings_hash, request_id, request_hash,
                    format_version, settings_json, management_repository,
                    parent_issue_number, task_owner_host, confirmed_at
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
                    raw_settings["confirmed_at"],
                ),
            )
            connection.execute(
                """
                UPDATE task_projects
                SET task_settings_hash = ?
                WHERE request_id = ? AND project_id = ?
                """,
                (
                    settings.task_settings_hash,
                    request.request_id,
                    request.projects[0].project_id,
                ),
            )
            common = {"task_settings_hash": settings.task_settings_hash}
            _insert_event(
                connection,
                request.request_id,
                "settings_activated",
                "settings_activated",
                common,
                settings_hash=settings.task_settings_hash,
            )
            _insert_event(
                connection,
                request.request_id,
                "active",
                "active",
                common,
                settings_hash=settings.task_settings_hash,
            )
            _insert_event(
                connection,
                request.request_id,
                "dispatch_ready",
                "dispatch_ready",
                {
                    "project_ids": [request.projects[0].project_id],
                    "task_settings_hash": settings.task_settings_hash,
                },
                settings_hash=settings.task_settings_hash,
            )
    return settings


def _grant(
    database: TaskDatabase,
    request_id: str,
    *,
    subject: str = "user-1",
    surface: str = "cli",
) -> None:
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO task_access (
                request_id, surface, subject_id, role, granted_by,
                granted_at, revoked_at
            ) VALUES (?, ?, ?, 'owner', ?, ?, NULL)
            """,
            (request_id, surface, subject, subject, NOW_TEXT),
        )


def _bind(
    database: TaskDatabase,
    request_id: str,
    issue_number: int,
    *,
    subject: str = "user-1",
    session: str = "session-1",
    surface: str = "cli",
) -> None:
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO task_session_bindings (
                surface, subject_id, session_id, request_id,
                parent_issue_number, bound_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (surface, subject, session, request_id, issue_number, NOW_TEXT),
        )


def _bind_replacement(
    database: TaskDatabase,
    request: TaskRequestV2,
    *,
    issue_number: int = 21,
) -> None:
    project = request.projects[0]
    root_card_id = f"replacement-{request.request_id}"
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE task_projects
            SET state = 'bound', root_card_id = ?
            WHERE request_id = ? AND project_id = ? AND state = 'prepared'
              AND root_card_id IS NULL AND task_settings_hash IS NULL
            """,
            (root_card_id, request.request_id, project.project_id),
        )
        _insert_event(
            connection,
            request.request_id,
            "parent_issue_bound",
            "parent_issue_bound",
            {
                "parent_issue_number": issue_number,
                "request_hash": request.request_hash,
            },
        )
        _insert_event(
            connection,
            request.request_id,
            "project_item_bound",
            f"project_item_bound:{project.project_id}",
            {
                "idempotency_key": (
                    f"forge-task-v2:{request.request_id}:"
                    f"{project.project_id}:build"
                ),
                "root_card_id": root_card_id,
            },
            project_id=project.project_id,
        )


def _context(
    *,
    host: str = OWNER_HOST,
    subject: str = "user-1",
    session: str = "session-1",
    surface: str = "cli",
    source_event_id: str = "cli:stop-event-1",
) -> TrustedTurnContext:
    return TrustedTurnContext(
        owner_host=host,
        subject_id=subject,
        session_id=session,
        surface=surface,
        source_event_id=source_event_id,
        working_directory=None,
    )


def _row_counts(database: TaskDatabase) -> dict[str, int]:
    tables = (
        "surface_events",
        "task_events",
        "task_stop_requests",
        "task_revision_requests",
        "task_message_events",
    )
    with database.read() as connection:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }


def test_get_stoppable_is_read_only_and_prefers_exact_session_binding(
    tmp_path: Path,
) -> None:
    database = TaskDatabase(tmp_path / "tasks.sqlite3")
    first = _request(tmp_path, repository="owner/first")
    second = _request(tmp_path, repository="owner/second")
    _seed_request(database, first, issue_number=21, active=True)
    _seed_request(database, second, issue_number=22, active=True)
    _grant(database, first.request_id)
    _grant(database, second.request_id)
    _bind(database, second.request_id, 22)
    before = _row_counts(database)

    tasks = TaskStopService(database).get_stoppable(_context())

    assert [(task.request_id, task.parent_issue_number, task.state) for task in tasks] == [
        (second.request_id, 22, "active")
    ]
    assert _row_counts(database) == before


def test_get_stoppable_without_binding_returns_every_accessible_aggregate(
    tmp_path: Path,
) -> None:
    database = TaskDatabase(tmp_path / "tasks.sqlite3")
    first = _request(tmp_path, repository="owner/first")
    second = _request(tmp_path, repository="owner/second")
    _seed_request(database, first, issue_number=21, active=True)
    _seed_request(database, second, issue_number=22)
    _grant(database, first.request_id)
    _grant(database, second.request_id)

    tasks = TaskStopService(database).get_stoppable(_context())

    assert [(task.parent_issue_number, task.state) for task in tasks] == [
        (21, "active"),
        (22, "bound"),
    ]


def test_access_is_checked_before_owner_host_is_disclosed(tmp_path: Path) -> None:
    database = TaskDatabase(tmp_path / "tasks.sqlite3")
    request = _request(tmp_path, owner_host=OTHER_HOST)
    _seed_request(database, request, issue_number=21, active=True)
    service = TaskStopService(database)

    with pytest.raises(TaskStopAccessDenied) as hidden:
        service.get_stoppable(_context(), issue_number=21)
    assert OTHER_HOST not in str(hidden.value)

    _grant(database, request.request_id)
    with pytest.raises(TaskStopOwnerHostMismatch) as disclosed:
        service.get_stoppable(_context(), issue_number=21)
    assert disclosed.value.owner_host == OTHER_HOST
    assert OTHER_HOST in str(disclosed.value)


def test_request_stop_atomically_sets_barrier_and_cancels_pending_update(
    tmp_path: Path,
) -> None:
    database = TaskDatabase(tmp_path / "tasks.sqlite3")
    base = _request(tmp_path, repository="owner/base")
    settings = _seed_request(database, base, issue_number=21, active=True)
    assert settings is not None
    replacement = _request(
        tmp_path,
        replaces_request_id=base.request_id,
        repository="owner/base",
    )
    _seed_request(database, replacement, issue_number=21)
    _grant(database, base.request_id)
    _bind(database, base.request_id, 21)

    message_context = _context(source_event_id="cli:message-event")
    stop_context = _context(source_event_id="cli:stop-event")
    events = SurfaceEventStore(database, clock=lambda: NOW)
    events.receive(message_context, "change requirement", at=NOW)
    stop_event = events.receive(stop_context, "forge stop #21", at=NOW)
    message_receipt = TaskMessageStore(database).send(
        base.request_id,
        message_context,
        "change requirement",
        at=NOW,
    )
    revision_id = message_receipt.revision_request_id
    message_id = message_receipt.message.message_id
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE task_revision_requests
            SET replacement_request_id = ?
            WHERE revision_request_id = ? AND state = 'requested'
            """,
            (replacement.request_id, revision_id),
        )

    receipt = TaskStopService(database).request_stop(
        base.request_id,
        stop_context,
        payload_hash=stop_event.payload_hash,
        at=NOW,
    )

    assert receipt.request_id == base.request_id
    assert receipt.parent_issue_number == 21
    assert receipt.state == "stopping"
    with database.read() as connection:
        stop_row = connection.execute(
            "SELECT request_id, task_settings_hash, state FROM task_stop_requests"
        ).fetchone()
        assert tuple(stop_row) == (base.request_id, settings.task_settings_hash, "stopping")
        event_types = [
            row[0]
            for row in connection.execute(
                "SELECT event_type FROM task_events WHERE request_id = ? ORDER BY event_id",
                (base.request_id,),
            )
        ]
        assert event_types[-1] == "stopping"
        assert event_types.index("stop_requested") < event_types.index("stopping")
        assert connection.execute(
            "SELECT state FROM task_revision_requests WHERE revision_request_id = ?",
            (revision_id,),
        ).fetchone()[0] == "cancelled"
        assert connection.execute(
            "SELECT event_type FROM task_events WHERE request_id = ? ORDER BY event_id DESC LIMIT 1",
            (replacement.request_id,),
        ).fetchone()[0] == "cancelled"
        rejected = connection.execute(
            "SELECT event_type, reason FROM task_message_events WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        assert tuple(rejected) == ("rejected", "Task stop requested")


def test_local_plugin_backend_records_source_event_before_stop(
    tmp_path: Path,
) -> None:
    from forge.hermes_plugin.infinity_forge import _LocalStopBackend

    database = TaskDatabase(tmp_path / "tasks.sqlite3")
    request = _request(tmp_path)
    _seed_request(database, request, issue_number=21, active=True)
    _grant(database, request.request_id)
    _bind(database, request.request_id, 21)
    context = _context(source_event_id="cli:local-plugin-stop")
    backend = _LocalStopBackend(str(database.database_path))

    candidates = backend.get_stoppable(context)
    receipt = backend.request_stop(
        candidates[0].request_id,
        context,
        "forge stop",
        at=NOW,
    )

    assert receipt.request_id == request.request_id
    assert receipt.state == "stopping"
    with database.read() as connection:
        source = connection.execute(
            "SELECT state FROM surface_events WHERE source_event_id = ?",
            (context.source_event_id,),
        ).fetchone()
        assert source is not None
        assert source[0] == "received"


def test_activated_revision_wins_before_stop_and_new_settings_are_stopped(
    tmp_path: Path,
) -> None:
    database = TaskDatabase(tmp_path / "tasks.sqlite3")
    base = _request(tmp_path, repository="owner/base")
    base_settings = _seed_request(database, base, issue_number=21, active=True)
    assert base_settings is not None
    _grant(database, base.request_id)
    _bind(database, base.request_id, 21)
    message_context = _context(source_event_id="cli:activation-message")
    SurfaceEventStore(database).receive(
        message_context,
        "activate replacement",
        at=NOW + timedelta(seconds=1),
    )
    message = TaskMessageStore(database).send(
        base.request_id,
        message_context,
        "activate replacement",
        at=NOW + timedelta(seconds=1),
    )
    replacement = _request(
        tmp_path,
        request_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        replaces_request_id=base.request_id,
        repository="owner/base",
    )
    _seed_request(database, replacement)
    revisions = TaskRevisionService(database)
    preview = revisions.prepare_preview(
        message.revision_request_id,
        replacement,
    )
    revisions.confirm(
        message.revision_request_id,
        replacement,
        preview_hash=preview.preview_hash,
        at=NOW + timedelta(seconds=2),
    )
    _bind_replacement(database, replacement)
    replacement_settings = TaskSettingsV2.create(
        request=replacement,
        parent_issue_number=21,
    )
    revisions.activate_confirmed(
        message.revision_request_id,
        replacement_settings,
        at=NOW + timedelta(seconds=4),
    )
    stop_context = _context(source_event_id="cli:stop-activated-replacement")
    stop_event = SurfaceEventStore(database).receive(
        stop_context,
        "forge stop #21",
        at=NOW + timedelta(seconds=5),
    )

    candidates = TaskStopService(database).get_stoppable(
        stop_context,
        issue_number=21,
    )
    receipt = TaskStopService(database).request_stop(
        candidates[0].request_id,
        stop_context,
        payload_hash=stop_event.payload_hash,
        at=NOW + timedelta(seconds=5),
    )

    assert candidates[0].request_id == replacement.request_id
    assert candidates[0].task_settings_hash == replacement_settings.task_settings_hash
    assert receipt.request_id == replacement.request_id
    assert receipt.task_settings_hash == replacement_settings.task_settings_hash
    with database.read() as connection:
        revision_state = connection.execute(
            """
            SELECT state FROM task_revision_requests
            WHERE revision_request_id = ?
            """,
            (message.revision_request_id,),
        ).fetchone()[0]
    assert revision_state == "confirmed"


def test_stop_retry_is_idempotent_and_source_event_cannot_move_tasks(
    tmp_path: Path,
) -> None:
    database = TaskDatabase(tmp_path / "tasks.sqlite3")
    first = _request(tmp_path, repository="owner/first")
    second = _request(tmp_path, repository="owner/second")
    _seed_request(database, first, issue_number=21, active=True)
    _seed_request(database, second, issue_number=22, active=True)
    _grant(database, first.request_id)
    _grant(database, second.request_id)
    context = _context(source_event_id="cli:one-stop-source")
    source = SurfaceEventStore(database, clock=lambda: NOW).receive(
        context,
        "forge stop #21",
        at=NOW,
    )
    service = TaskStopService(database)

    first_receipt = service.request_stop(
        first.request_id,
        context,
        payload_hash=source.payload_hash,
        at=NOW,
    )
    replayed = service.request_stop(
        first.request_id,
        context,
        payload_hash=source.payload_hash,
        at=NOW,
    )

    assert replayed == first_receipt
    with database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM task_stop_requests").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM task_events WHERE event_type = 'stop_requested'"
        ).fetchone()[0] == 1
    with pytest.raises(TaskStopError, match="source event"):
        service.request_stop(
            second.request_id,
            context,
            payload_hash=source.payload_hash,
            at=NOW,
        )


def test_any_failure_rolls_back_stop_revision_and_message_changes(tmp_path: Path) -> None:
    database = TaskDatabase(tmp_path / "tasks.sqlite3")
    request = _request(tmp_path)
    _seed_request(database, request, issue_number=21, active=True)
    _grant(database, request.request_id)
    context = _context()
    source = SurfaceEventStore(database, clock=lambda: NOW).receive(
        context,
        "forge stop #21",
        at=NOW,
    )
    with database.transaction() as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_stopping
            BEFORE INSERT ON task_events
            WHEN NEW.event_type = 'stopping'
            BEGIN
                SELECT RAISE(ABORT, 'forced stopping failure');
            END
            """
        )

    with pytest.raises(TaskStopError):
        TaskStopService(database).request_stop(
            request.request_id,
            context,
            payload_hash=source.payload_hash,
            at=NOW,
        )

    with database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM task_stop_requests").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM task_events WHERE event_type IN ('stop_requested', 'stopping')"
        ).fetchone()[0] == 0


def test_prepared_bound_and_stopping_are_separate_stoppable_states(
    tmp_path: Path,
) -> None:
    database = TaskDatabase(tmp_path / "tasks.sqlite3")
    prepared = _request(tmp_path, repository="owner/prepared")
    bound = _request(tmp_path, repository="owner/bound")
    active = _request(tmp_path, repository="owner/active")
    _seed_request(database, prepared)
    _seed_request(database, bound, issue_number=22)
    _seed_request(database, active, issue_number=23, active=True)
    for request in (prepared, bound, active):
        _grant(database, request.request_id)
    context = _context(source_event_id="cli:active-stop")
    source = SurfaceEventStore(database, clock=lambda: NOW).receive(
        context,
        "forge stop #23",
        at=NOW,
    )
    TaskStopService(database).request_stop(
        active.request_id,
        context,
        payload_hash=source.payload_hash,
        at=NOW,
    )

    tasks = TaskStopService(database).get_stoppable(
        _context(source_event_id="cli:list")
    )

    assert {(task.parent_issue_number, task.state) for task in tasks} == {
        (None, "prepared"),
        (22, "bound"),
        (23, "stopping"),
    }


def test_v1_issue_stop_is_explicitly_unsupported_without_any_write(
    tmp_path: Path,
) -> None:
    database = TaskDatabase(tmp_path / "tasks.sqlite3")
    request_id = str(uuid4())
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO task_settings (
                request_id, format_version, repository, mode,
                task_content_hash, task_flow, merge_mode, confirmed_by,
                confirmed_at, auto_merge_expires_at
            ) VALUES (?, 'forge-task-settings/v1', 'owner/v1', 'task', ?,
                      'build', 'manual', 'user-1', ?, NULL)
            """,
            (request_id, "a" * 64, NOW_TEXT),
        )
        connection.execute(
            """
            INSERT INTO task_settings_events (
                request_id, event_type, occurred_at, issue_number,
                task_settings_hash
            ) VALUES (?, 'issue_bound', ?, 21, ?)
            """,
            (request_id, NOW_TEXT, "b" * 64),
        )
    before = _row_counts(database)

    with pytest.raises(TaskStopUnsupported, match="v1 Task Stop is unsupported"):
        TaskStopService(database).get_stoppable(_context(), issue_number=21)

    assert _row_counts(database) == before


def test_orphan_stop_barrier_fails_closed_instead_of_looking_active(
    tmp_path: Path,
) -> None:
    database = TaskDatabase(tmp_path / "tasks.sqlite3")
    request = _request(tmp_path)
    settings = _seed_request(database, request, issue_number=21, active=True)
    assert settings is not None
    _grant(database, request.request_id)
    with database.transaction() as connection:
        _insert_event(
            connection,
            request.request_id,
            "stop_requested",
            f"stop_requested:{uuid4()}",
            {"stop_request_id": str(uuid4())},
            settings_hash=settings.task_settings_hash,
        )

    with pytest.raises(TaskStopError, match="Stop barrier.*durable"):
        TaskStopService(database).get_stoppable(_context())
