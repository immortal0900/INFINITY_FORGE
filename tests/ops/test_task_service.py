from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, BrokenBarrierError, Event, Lock
from time import sleep
from uuid import uuid4

import pytest

from forge.ops.task_database import TaskDatabase
from forge.ops.task_options import MergeMode, TaskFlow
from forge.ops.task_outbox import TaskOutbox
from forge.ops.task_projects import TaskProject
from forge.ops.task_service import (
    READY_TO_BUILD_LABEL,
    V2_PROGRESS_END,
    V2_PROGRESS_START,
    CreatedTaskV2,
    ProjectExecutionItem,
    ProjectItemClient,
    TaskParentIssue,
    TaskCreationRequest,
    TaskIssue,
    TaskIssueClient,
    TaskService,
    TaskServiceV2,
    TaskServiceError,
    build_task_issue_body,
    build_task_issue_body_v2,
    read_task_marker,
    read_task_marker_v2,
    replace_task_progress_v2,
    root_project_item_key,
    verify_task_issue_v2_content,
)
from forge.ops.task_settings_v2 import TaskRequestV2, TaskSettingsV2
from forge.ops.task_settings import (
    TaskContent,
    TaskSettings,
    TaskSettingsStatus,
    TaskSettingsStore,
)


NOW = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)
OWNER_HOST = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
V2_REQUEST_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class FakeTaskIssues:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.issues: dict[int, TaskIssue] = {}
        self._next_number = 1
        self._lock = Lock()
        self.fail_create_after_write = False
        self.fail_update = False
        self.fail_label = False

    def find_issue(self, repository: str, request_id: str) -> TaskIssue | None:
        self.calls.append(("find", repository, request_id))
        matches = [
            issue
            for issue in self.issues.values()
            if read_task_marker(issue.body)["request_id"] == request_id
        ]
        if len(matches) > 1:
            raise TaskServiceError("request_id matched more than one GitHub issue")
        return matches[0] if matches else None

    def create_issue(self, repository: str, title: str, body: str) -> TaskIssue:
        with self._lock:
            self.calls.append(("create", repository, title, body))
            issue = TaskIssue(
                number=self._next_number,
                title=title,
                body=body,
                labels=(),
            )
            self._next_number += 1
            self.issues[issue.number] = issue
            if self.fail_create_after_write:
                self.fail_create_after_write = False
                raise OSError("response was lost after GitHub created the issue")
            return issue

    def update_issue(
        self,
        repository: str,
        issue_number: int,
        *,
        title: str,
        body: str,
    ) -> TaskIssue:
        self.calls.append(("update", repository, issue_number, title, body))
        if self.fail_update:
            raise OSError("GitHub update failed")
        current = self.issues[issue_number]
        updated = replace(current, title=title, body=body)
        self.issues[issue_number] = updated
        return updated

    def get_issue(self, repository: str, issue_number: int) -> TaskIssue:
        self.calls.append(("get", repository, issue_number))
        return self.issues[issue_number]

    def add_label(
        self,
        repository: str,
        issue_number: int,
        label: str,
    ) -> TaskIssue:
        self.calls.append(("label", repository, issue_number, label))
        if self.fail_label:
            self.fail_label = False
            raise OSError("GitHub label failed")
        current = self.issues[issue_number]
        labels = tuple(sorted(set((*current.labels, label))))
        updated = replace(current, labels=labels)
        self.issues[issue_number] = updated
        return updated


def _request(request_id: str | None = None) -> TaskCreationRequest:
    return TaskCreationRequest(
        request_id=request_id or str(uuid4()),
        repository="openai/infinity-forge",
        content=TaskContent(
            title="Add the Hermes task chooser",
            description="Choose Chat or Task before the first model request.",
            acceptance_criteria=(
                "Chat creates no GitHub issue.",
                "Task requires a fresh flow and merge mode.",
            ),
        ),
        task_flow=TaskFlow.BUILD_REVIEW,
        merge_mode=MergeMode.SAFE_AUTO,
        confirmed_by="hermes-user-7",
        confirmed_at=NOW,
    )


def _service(tmp_path: Path, github: FakeTaskIssues) -> tuple[TaskService, TaskSettingsStore]:
    store = TaskSettingsStore(tmp_path / "task-settings.db")
    return TaskService(store, github), store


def _request_v2(
    tmp_path: Path,
    *,
    request_id: str = V2_REQUEST_ID,
    description: str = "Build the selected Projects from one managed Task.",
    replaces_request_id: str | None = None,
    merge_mode: MergeMode = MergeMode.SAFE_AUTO,
) -> TaskRequestV2:
    projects = []
    for index, repository in enumerate(("owner/project-b", "owner/project-a"), start=1):
        workspace = tmp_path / f"project-{index}"
        workspace.mkdir(parents=True, exist_ok=True)
        projects.append(
            TaskProject.create(
                repository=repository,
                workspace=str(workspace.resolve()),
                remote_name="origin",
                base_branch="main",
                base_commit=str(index) * 40,
                host_id=OWNER_HOST,
            )
        )
    return TaskRequestV2.create(
        request_id=request_id,
        management_repository="immortal0900/INFINITY_FORGE",
        task_content=TaskContent(
            title="Run a centrally managed Task",
            description=description,
            acceptance_criteria=("Each selected Project receives one Build root.",),
        ),
        task_flow=TaskFlow.BUILD_REVIEW,
        merge_mode=merge_mode,
        merge_order=(
            tuple(project.project_id for project in projects)
            if merge_mode is MergeMode.FULL_AUTO
            else None
        ),
        projects=tuple(projects),
        task_owner_host=OWNER_HOST,
        confirmed_by="hermes-user-7",
        confirmed_at=NOW,
        replaces_request_id=replaces_request_id,
    )


class FakeParentIssuesV2:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.issues: dict[int, tuple[str, TaskParentIssue]] = {}
        self.fail_create_after_write = False
        self.fail_update_after_write = False
        self._next_number = 1
        self._lock = Lock()

    def find_issue(
        self,
        repository: str,
        request_id: str,
    ) -> TaskParentIssue | None:
        self.calls.append(("find", repository, request_id))
        matches = [
            issue
            for stored_repository, issue in self.issues.values()
            if stored_repository == repository
            and read_task_marker_v2(issue.body)["request_id"] == request_id
        ]
        if len(matches) > 1:
            raise TaskServiceError("request_id matched more than one parent issue")
        return matches[0] if matches else None

    def create_issue(
        self,
        repository: str,
        title: str,
        body: str,
    ) -> TaskParentIssue:
        with self._lock:
            self.calls.append(("create", repository, title, body))
            issue = TaskParentIssue(
                number=self._next_number,
                title=title,
                body=body,
                state="open",
            )
            self._next_number += 1
            self.issues[issue.number] = (repository, issue)
            if self.fail_create_after_write:
                self.fail_create_after_write = False
                raise OSError("parent issue response was lost")
            return issue

    def get_issue(
        self,
        repository: str,
        issue_number: int,
    ) -> TaskParentIssue:
        self.calls.append(("get", repository, issue_number))
        stored_repository, issue = self.issues[issue_number]
        assert stored_repository == repository
        return issue

    def update_issue(
        self,
        repository: str,
        issue_number: int,
        *,
        title: str,
        body: str,
    ) -> TaskParentIssue:
        self.calls.append(("update", repository, issue_number, title, body))
        stored_repository, current = self.issues[issue_number]
        assert stored_repository == repository
        updated = replace(current, title=title, body=body)
        self.issues[issue_number] = (repository, updated)
        if self.fail_update_after_write:
            self.fail_update_after_write = False
            raise OSError("parent progress response was lost")
        return updated


class FakeProjectItems:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.items: dict[str, tuple[str, ProjectExecutionItem]] = {}
        self.fail_create_before_key: str | None = None
        self.fail_create_after_key: str | None = None
        self.fail_release_after_item: str | None = None
        self._next_id = 1
        self._lock = Lock()

    def find_items(
        self,
        management_repository: str,
        idempotency_key: str,
    ) -> tuple[ProjectExecutionItem, ...]:
        self.calls.append(("find", management_repository, idempotency_key))
        return tuple(
            item
            for stored_repository, item in self.items.values()
            if stored_repository == management_repository
            and item.idempotency_key == idempotency_key
        )

    def create_item(
        self,
        management_repository: str,
        parent_issue_number: int,
        project_repository: str,
        idempotency_key: str,
        *,
        state: str,
    ) -> ProjectExecutionItem:
        with self._lock:
            self.calls.append(
                (
                    "create",
                    management_repository,
                    parent_issue_number,
                    project_repository,
                    idempotency_key,
                    state,
                )
            )
            if self.fail_create_before_key == idempotency_key:
                self.fail_create_before_key = None
                raise OSError("Project item creation failed before write")
            item = ProjectExecutionItem(
                item_id=f"root-{self._next_id}",
                idempotency_key=idempotency_key,
                parent_issue_number=parent_issue_number,
                project_repository=project_repository,
                state=state,
            )
            self._next_id += 1
            self.items[item.item_id] = (management_repository, item)
            if self.fail_create_after_key == idempotency_key:
                self.fail_create_after_key = None
                raise OSError("Project item response was lost")
            return item

    def get_item(
        self,
        management_repository: str,
        item_id: str,
    ) -> ProjectExecutionItem:
        self.calls.append(("get", management_repository, item_id))
        stored_repository, item = self.items[item_id]
        assert stored_repository == management_repository
        return item

    def release_item(
        self,
        management_repository: str,
        item_id: str,
    ) -> ProjectExecutionItem:
        self.calls.append(("release", management_repository, item_id))
        stored_repository, item = self.items[item_id]
        assert stored_repository == management_repository
        released = replace(item, state="ready")
        self.items[item_id] = (management_repository, released)
        if self.fail_release_after_item == item_id:
            self.fail_release_after_item = None
            raise OSError("Project item release response was lost")
        return released


def _service_v2(
    tmp_path: Path,
    issues: FakeParentIssuesV2,
    items: FakeProjectItems,
) -> tuple[TaskServiceV2, TaskDatabase]:
    database = TaskDatabase(tmp_path / "task-v2.db")
    return TaskServiceV2(database, issues, items, clock=lambda: NOW), database


def _event_types(database: TaskDatabase, request_id: str) -> tuple[str, ...]:
    with database.read() as connection:
        return tuple(
            row[0]
            for row in connection.execute(
                """
                SELECT event_type
                FROM task_events
                WHERE request_id = ?
                ORDER BY event_id
                """,
                (request_id,),
            )
        )


def _append_v2_lifecycle_event(
    database: TaskDatabase,
    request: TaskRequestV2,
    event_type: str,
) -> None:
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO task_events (
                request_id, task_settings_hash, project_id, event_type,
                event_key, event_json, occurred_at
            ) VALUES (?, NULL, NULL, ?, ?, ?, ?)
            """,
            (
                request.request_id,
                event_type,
                f"{event_type}:{uuid4()}",
                json.dumps({"source": "test"}, separators=(",", ":"), sort_keys=True),
                "2026-07-16T10:00:00Z",
            ),
        )


@pytest.mark.parametrize(
    "marker_json",
    (
        (
            '{"format_version":"forge-task-request/v1",'
            '"format_version":"forge-task-request/v1",'
            '"request_id":"9f7453ce-36ec-4e8e-9dfa-bb159b58c19b",'
            '"task_content_hash":"' + "a" * 64 + '"}'
        ),
        (
            '{"format_version":"forge-task-request/v1",'
            '"request_id":"9f7453ce-36ec-4e8e-9dfa-bb159b58c19b",'
            '"task_content_hash":{"value":"first","value":"second"}}'
        ),
    ),
)
def test_task_marker_rejects_duplicate_keys_at_every_object_level(
    marker_json: str,
) -> None:
    body = f"<!-- forge-task-request\n{marker_json}\n-->"

    with pytest.raises(TaskServiceError, match="duplicate fields"):
        read_task_marker(body)


def test_confirmed_task_is_activated_before_ready_label(tmp_path: Path) -> None:
    github = FakeTaskIssues()
    service, store = _service(tmp_path, github)

    created = service.create_task(_request("9f7453ce-36ec-4e8e-9dfa-bb159b58c19b"))

    assert created.settings.status is TaskSettingsStatus.ACTIVE
    assert created.settings.issue_number == created.issue.number == 1
    assert created.settings.task_settings_hash is not None
    assert created.issue.labels == (READY_TO_BUILD_LABEL,)
    assert github.calls[-1][:1] == ("label",)
    assert store.get_active(created.settings.request_id) == created.settings
    marker = read_task_marker(created.issue.body)
    assert marker == {
        "format_version": "forge-task-request/v1",
        "request_id": created.settings.request_id,
        "task_content_hash": created.settings.task_content_hash,
        "task_settings_hash": created.settings.task_settings_hash,
    }


def test_lost_create_response_resumes_by_request_id_without_duplicate_issue(
    tmp_path: Path,
) -> None:
    github = FakeTaskIssues()
    github.fail_create_after_write = True
    service, store = _service(tmp_path, github)
    request = _request("9f7453ce-36ec-4e8e-9dfa-bb159b58c19b")

    with pytest.raises(TaskServiceError, match="GitHub issue creation failed"):
        service.create_task(request)
    created = service.create_task(request)

    assert len(github.issues) == 1
    assert created.settings.status is TaskSettingsStatus.ACTIVE
    assert store.get_active(request.request_id) is not None


def test_durable_restart_replays_lost_create_response_without_duplicate_issue(
    tmp_path: Path,
) -> None:
    github = FakeTaskIssues()
    github.fail_create_after_write = True
    settings_database = tmp_path / "task-settings.db"
    outbox_database = tmp_path / "task-outbox.db"
    request = _request("9f7453ce-36ec-4e8e-9dfa-bb159b58c19b")

    first_service = TaskService(TaskSettingsStore(settings_database), github)
    with pytest.raises(TaskServiceError, match="GitHub issue creation failed"):
        first_service.create_task_durable(request, TaskOutbox(outbox_database))

    assert TaskOutbox(outbox_database).load_pending(request.request_id) == request
    restarted_service = TaskService(TaskSettingsStore(settings_database), github)
    created = restarted_service.create_task_durable(
        request,
        TaskOutbox(outbox_database),
    )

    assert created.issue.number == 1
    assert len(github.issues) == 1
    assert TaskOutbox(outbox_database).load_pending(request.request_id) is None


def test_durable_request_is_committed_before_first_github_write(
    tmp_path: Path,
) -> None:
    outbox_database = tmp_path / "task-outbox.db"
    request = _request()

    class ChecksOutboxBeforeWrite(FakeTaskIssues):
        def create_issue(self, repository: str, title: str, body: str) -> TaskIssue:
            with sqlite3.connect(outbox_database) as connection:
                request_json, state = connection.execute(
                    """
                    SELECT request_json, state
                    FROM task_outbox
                    WHERE request_id = ?
                    """,
                    (request.request_id,),
                ).fetchone()
            assert json.loads(request_json)["request_id"] == request.request_id
            assert state == "pending"
            return super().create_issue(repository, title, body)

    github = ChecksOutboxBeforeWrite()
    service, _ = _service(tmp_path, github)

    service.create_task_durable(request, TaskOutbox(outbox_database))

    assert len(github.issues) == 1


def test_completed_durable_task_replay_reads_without_github_write(
    tmp_path: Path,
) -> None:
    github = FakeTaskIssues()
    service, _ = _service(tmp_path, github)
    outbox = TaskOutbox(tmp_path / "task-outbox.db")
    request = _request()
    first = service.create_task_durable(request, outbox)
    github.calls.clear()

    replayed = TaskService(
        TaskSettingsStore(tmp_path / "task-settings.db"),
        github,
    ).create_task_durable(request, TaskOutbox(tmp_path / "task-outbox.db"))

    assert replayed == first
    assert [call[0] for call in github.calls] == ["get"]


@pytest.mark.parametrize(
    "terminal_status",
    (
        TaskSettingsStatus.CANCELLED,
        TaskSettingsStatus.EXPIRED,
        TaskSettingsStatus.MERGED,
    ),
)
def test_completed_durable_terminal_task_stops_before_github_write(
    tmp_path: Path,
    terminal_status: TaskSettingsStatus,
) -> None:
    github = FakeTaskIssues()
    service, store = _service(tmp_path, github)
    outbox = TaskOutbox(tmp_path / "task-outbox.db")
    request = _request()
    service.create_task_durable(request, outbox)
    store.append_lifecycle_event(request.request_id, terminal_status)
    github.calls.clear()

    with pytest.raises(TaskServiceError, match="lifecycle ended"):
        service.create_task_durable(request, outbox)

    assert github.calls == []


def test_terminal_pending_delivery_is_retired_without_github_write(
    tmp_path: Path,
) -> None:
    github = FakeTaskIssues()
    service, store = _service(tmp_path, github)
    outbox = TaskOutbox(tmp_path / "task-outbox.db")
    request = _request()
    service.create_task(request)
    outbox.save(request)
    store.append_lifecycle_event(
        request.request_id,
        TaskSettingsStatus.CANCELLED,
    )
    github.calls.clear()

    with pytest.raises(TaskServiceError, match="lifecycle ended as cancelled"):
        service.create_task_durable(request, outbox)

    assert github.calls == []
    assert outbox.load_pending(request.request_id) is None
    replacement = _request()
    outbox.save(replacement)
    assert (
        outbox.load_pending_for_user(
            replacement.repository,
            replacement.confirmed_by,
        )
        == replacement
    )


def test_terminal_transition_waits_until_ready_label_write_finishes(
    tmp_path: Path,
) -> None:
    class BlockingReadIssues(FakeTaskIssues):
        def __init__(self) -> None:
            super().__init__()
            self.block_reads = False
            self.read_started = Event()
            self.release_read = Event()

        def get_issue(self, repository: str, issue_number: int) -> TaskIssue:
            if self.block_reads:
                self.read_started.set()
                assert self.release_read.wait(timeout=2)
            return super().get_issue(repository, issue_number)

    github = BlockingReadIssues()
    service, store = _service(tmp_path, github)
    outbox = TaskOutbox(tmp_path / "task-outbox.db")
    request = _request()
    service.create_task(request)
    outbox.save(request)
    github.calls.clear()
    github.block_reads = True
    terminal_started = Event()

    def end_task() -> object:
        terminal_started.set()
        return store.append_lifecycle_event(
            request.request_id,
            TaskSettingsStatus.CANCELLED,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        delivery = pool.submit(service.create_task_durable, request, outbox)
        assert github.read_started.wait(timeout=2)
        terminal = pool.submit(end_task)
        assert terminal_started.wait(timeout=2)
        sleep(0.05)
        assert not terminal.done()
        github.release_read.set()
        created = delivery.result(timeout=2)
        ended = terminal.result(timeout=2)

    assert created.issue.number == ended.issue_number == 1
    assert ended.status is TaskSettingsStatus.CANCELLED
    assert [call[0] for call in github.calls] == ["find", "get", "label"]


def test_update_failure_leaves_prepared_task_without_ready_label(tmp_path: Path) -> None:
    github = FakeTaskIssues()
    github.fail_update = True
    service, store = _service(tmp_path, github)
    request = _request()

    with pytest.raises(TaskServiceError, match="GitHub issue update failed"):
        service.create_task(request)

    assert store.get_active(request.request_id) is None
    assert READY_TO_BUILD_LABEL not in github.issues[1].labels
    assert all(call[0] != "label" for call in github.calls)


def test_label_failure_replays_without_creating_or_rebinding(tmp_path: Path) -> None:
    github = FakeTaskIssues()
    github.fail_label = True
    service, store = _service(tmp_path, github)
    request = _request()

    with pytest.raises(TaskServiceError, match="ready label failed"):
        service.create_task(request)
    assert store.get_active(request.request_id) is not None

    created = service.create_task(request)

    assert len(github.issues) == 1
    assert created.issue.labels == (READY_TO_BUILD_LABEL,)
    assert sum(call[0] == "create" for call in github.calls) == 1


def test_active_issue_content_change_is_not_silently_overwritten(tmp_path: Path) -> None:
    github = FakeTaskIssues()
    service, _ = _service(tmp_path, github)
    request = _request()
    created = service.create_task(request)
    github.issues[created.issue.number] = replace(
        created.issue,
        body=created.issue.body.replace(
            "Chat creates no GitHub issue.",
            "Chat may create a GitHub issue.",
        ),
    )

    with pytest.raises(TaskServiceError, match="active GitHub issue content changed"):
        service.create_task(request)


@pytest.mark.parametrize(
    "terminal_status",
    (
        TaskSettingsStatus.CANCELLED,
        TaskSettingsStatus.EXPIRED,
        TaskSettingsStatus.MERGED,
    ),
)
def test_terminal_task_replay_stops_before_any_github_write(
    tmp_path: Path,
    terminal_status: TaskSettingsStatus,
) -> None:
    github = FakeTaskIssues()
    service, store = _service(tmp_path, github)
    request = _request()
    service.create_task(request)
    store.append_lifecycle_event(request.request_id, terminal_status)
    github.calls.clear()

    with pytest.raises(TaskServiceError, match="lifecycle ended"):
        service.create_task(request)

    assert github.calls == []


def test_concurrent_replay_creates_exactly_one_issue(tmp_path: Path) -> None:
    github = FakeTaskIssues()
    service, _ = _service(tmp_path, github)
    request = _request("9f7453ce-36ec-4e8e-9dfa-bb159b58c19b")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: service.create_task(request), range(2)))

    assert len(github.issues) == 1
    assert {result.issue.number for result in results} == {1}
    assert sum(call[0] == "create" for call in github.calls) == 1


def test_concurrent_replay_across_service_instances_creates_one_issue(
    tmp_path: Path,
) -> None:
    class SynchronizedFindIssues(FakeTaskIssues):
        def __init__(self) -> None:
            super().__init__()
            self.find_barrier = Barrier(2)

        def find_issue(
            self,
            repository: str,
            request_id: str,
        ) -> TaskIssue | None:
            found = super().find_issue(repository, request_id)
            try:
                self.find_barrier.wait(timeout=0.2)
            except BrokenBarrierError:
                pass
            return found

    github = SynchronizedFindIssues()
    database = tmp_path / "task-settings.db"
    services = (
        TaskService(TaskSettingsStore(database), github),
        TaskService(TaskSettingsStore(database), github),
    )
    request = _request("9f7453ce-36ec-4e8e-9dfa-bb159b58c19b")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(service.create_task, request)
            for service in services
        ]
        results = tuple(future.result(timeout=2) for future in futures)

    assert len(github.issues) == 1
    assert {result.issue.number for result in results} == {1}
    assert sum(call[0] == "create" for call in github.calls) == 1


def test_v1_issue_body_keeps_its_exact_golden_bytes() -> None:
    request = _request("9f7453ce-36ec-4e8e-9dfa-bb159b58c19b")
    settings = TaskSettings.create(
        request_id=request.request_id,
        repository=request.repository,
        task_content=request.content,
        task_flow=request.task_flow,
        merge_mode=request.merge_mode,
        confirmed_by=request.confirmed_by,
        confirmed_at=request.confirmed_at,
    )

    assert build_task_issue_body(request.content, settings) == (
        "Choose Chat or Task before the first model request.\n\n"
        "## Acceptance Criteria\n\n"
        "1. Chat creates no GitHub issue.\n"
        "2. Task requires a fresh flow and merge mode.\n\n"
        "<!-- forge-task-request\n"
        '{"format_version":"forge-task-request/v1",'
        '"request_id":"9f7453ce-36ec-4e8e-9dfa-bb159b58c19b",'
        f'"task_content_hash":"{settings.task_content_hash}"}}\n'
        "-->"
    )


def test_v1_issue_client_protocol_keeps_its_exact_methods() -> None:
    methods = {
        name
        for name, value in TaskIssueClient.__dict__.items()
        if not name.startswith("_") and callable(value)
    }

    assert methods == {
        "find_issue",
        "create_issue",
        "update_issue",
        "get_issue",
        "add_label",
    }
    assert not hasattr(ProjectItemClient, "get_issue")
    assert not hasattr(ProjectItemClient, "add_label")


def test_v2_issue_body_separates_immutable_marker_from_exact_progress(
    tmp_path: Path,
) -> None:
    request = _request_v2(tmp_path)

    body = build_task_issue_body_v2(
        request,
        {project.project_id: "blocked" for project in request.projects},
    )

    assert "<!-- forge-task-request" not in body
    assert body.count("<!-- forge-v2-task-request") == 1
    assert body.count(V2_PROGRESS_START) == 1
    assert body.count(V2_PROGRESS_END) == 1
    assert request.task_owner_host not in body
    assert request.confirmed_by not in body
    assert all(project.workspace not in body for project in request.projects)
    assert "Task flow: `build_review`" in body
    assert "Merge mode: `safe_auto`" in body
    assert "Auto-merge permission until: `2026-07-16T22:00:00Z`" in body
    assert all(f"- `{project.repository}`" in body for project in request.projects)
    assert read_task_marker_v2(body) == {
        "format_version": "forge-task-request/v2",
        "request_id": request.request_id,
        "request_hash": request.request_hash,
        "task_content_hash": request.task_content_hash,
    }


def test_v2_parent_body_shows_confirmed_full_auto_merge_order(tmp_path: Path) -> None:
    request = _request_v2(tmp_path, merge_mode=MergeMode.FULL_AUTO)

    body = build_task_issue_body_v2(
        request,
        {project.project_id: "blocked" for project in request.projects},
    )

    assert "Merge mode: `full_auto`" in body
    assert request.merge_order is not None
    repositories = {
        project.project_id: project.repository for project in request.projects
    }
    positions = [
        body.index(f"{number}. `{repositories[project_id]}`")
        for number, project_id in enumerate(request.merge_order, start=1)
    ]
    assert positions == sorted(positions)


def test_v2_progress_replacement_changes_only_the_owned_interior(
    tmp_path: Path,
) -> None:
    request = _request_v2(tmp_path)
    blocked = build_task_issue_body_v2(
        request,
        {project.project_id: "blocked" for project in request.projects},
    )

    ready = replace_task_progress_v2(
        blocked,
        request,
        {project.project_id: "ready" for project in request.projects},
    )

    blocked_prefix, blocked_tail = blocked.split(V2_PROGRESS_START, 1)
    blocked_interior, blocked_suffix = blocked_tail.split(V2_PROGRESS_END, 1)
    ready_prefix, ready_tail = ready.split(V2_PROGRESS_START, 1)
    ready_interior, ready_suffix = ready_tail.split(V2_PROGRESS_END, 1)
    assert ready_prefix == blocked_prefix
    assert ready_suffix == blocked_suffix
    assert ready_interior != blocked_interior
    assert "Ready" in ready_interior


@pytest.mark.parametrize(
    "mutate, message",
    (
        (lambda body: "changed\n" + body, "immutable"),
        (lambda body: body + "\nchanged", "immutable"),
        (
            lambda body: body.replace(V2_PROGRESS_END, "", 1),
            "progress delimiters",
        ),
        (
            lambda body: body.replace(
                V2_PROGRESS_END,
                V2_PROGRESS_START + "\n" + V2_PROGRESS_END,
                1,
            ),
            "progress delimiters",
        ),
        (
            lambda body: body.replace(
                V2_PROGRESS_START,
                "<!-- forge-v2-task-request\n{}\n-->\n" + V2_PROGRESS_START,
                1,
            ),
            "Task marker",
        ),
    ),
)
def test_v2_progress_update_rejects_immutable_or_delimiter_injection(
    tmp_path: Path,
    mutate,
    message: str,
) -> None:
    request = _request_v2(tmp_path)
    body = build_task_issue_body_v2(
        request,
        {project.project_id: "blocked" for project in request.projects},
    )

    with pytest.raises(TaskServiceError, match=message):
        replace_task_progress_v2(
            mutate(body),
            request,
            {project.project_id: "ready" for project in request.projects},
        )


def test_v2_issue_verification_rejects_title_or_marker_mismatch(
    tmp_path: Path,
) -> None:
    request = _request_v2(tmp_path)
    body = build_task_issue_body_v2(
        request,
        {project.project_id: "blocked" for project in request.projects},
    )

    with pytest.raises(TaskServiceError, match="title"):
        verify_task_issue_v2_content(
            TaskParentIssue(1, "Changed title", body, "open"),
            request,
        )
    with pytest.raises(TaskServiceError, match="request_hash"):
        verify_task_issue_v2_content(
            TaskParentIssue(
                1,
                request.task_content.title,
                body.replace(request.request_hash, "f" * 64),
                "open",
            ),
            request,
        )


def test_v2_creates_parent_only_in_management_and_roots_for_each_project(
    tmp_path: Path,
) -> None:
    issues = FakeParentIssuesV2()
    items = FakeProjectItems()
    service, database = _service_v2(tmp_path, issues, items)
    request = _request_v2(tmp_path)

    created = service.create_task(request)

    assert isinstance(created, CreatedTaskV2)
    assert isinstance(created.settings, TaskSettingsV2)
    assert created.settings.status == "active"
    assert created.parent_issue.number == created.settings.parent_issue_number == 1
    assert {call[1] for call in issues.calls if call[0] in {"find", "create"}} == {
        request.management_repository
    }
    assert all(
        call[1] == request.management_repository
        for call in items.calls
        if call[0] in {"find", "create", "get", "release"}
    )
    assert tuple(item.project_repository for item in created.project_items) == tuple(
        project.repository for project in request.projects
    )
    assert all(item.state == "ready" for item in created.project_items)
    assert "Ready" in created.parent_issue.body
    assert _event_types(database, request.request_id) == (
        "request_prepared",
        "parent_issue_bound",
        "project_item_bound",
        "project_item_bound",
        "settings_activated",
        "active",
        "dispatch_ready",
    )
    with database.read() as connection:
        project_rows = connection.execute(
            """
            SELECT project_id, task_settings_hash, state, root_card_id
            FROM task_projects
            WHERE request_id = ?
            ORDER BY project_json
            """,
            (request.request_id,),
        ).fetchall()
    assert {row[0] for row in project_rows} == {
        project.project_id for project in request.projects
    }
    assert all(row[1] == created.settings.task_settings_hash for row in project_rows)
    assert all(row[2] == "ready" and row[3] for row in project_rows)


def test_v2_partial_project_create_retries_without_duplicates_or_early_activation(
    tmp_path: Path,
) -> None:
    issues = FakeParentIssuesV2()
    items = FakeProjectItems()
    service, database = _service_v2(tmp_path, issues, items)
    request = _request_v2(tmp_path)
    second_key = root_project_item_key(
        request.request_id,
        request.projects[1].project_id,
    )
    items.fail_create_before_key = second_key

    with pytest.raises(TaskServiceError, match="Project item creation failed"):
        service.create_task(request)

    with database.read() as connection:
        assert connection.execute("SELECT count(*) FROM task_settings_v2").fetchone()[0] == 0
        states = tuple(
            row[0]
            for row in connection.execute(
                """
                SELECT state
                FROM task_projects
                WHERE request_id = ?
                ORDER BY project_json
                """,
                (request.request_id,),
            )
        )
    assert states.count("bound") == 1
    assert states.count("prepared") == 1
    assert not {
        "settings_activated",
        "active",
        "dispatch_ready",
    } & set(_event_types(database, request.request_id))

    created = service.create_task(request)

    assert len(issues.issues) == 1
    assert len(items.items) == 2
    assert created.settings.status == "active"
    assert sum(call[0] == "create" for call in issues.calls) == 1
    assert sum(call[0] == "create" for call in items.calls) == 3


def test_v2_parent_create_response_loss_recovers_by_lookup(
    tmp_path: Path,
) -> None:
    issues = FakeParentIssuesV2()
    issues.fail_create_after_write = True
    items = FakeProjectItems()
    service, database = _service_v2(tmp_path, issues, items)
    request = _request_v2(tmp_path)

    with pytest.raises(TaskServiceError, match="parent issue creation failed"):
        service.create_task(request)
    created = service.create_task(request)

    assert len(issues.issues) == 1
    assert sum(call[0] == "create" for call in issues.calls) == 1
    assert created.settings.status == "active"
    assert "parent_issue_bound" in _event_types(database, request.request_id)


def test_v2_project_item_create_response_loss_recovers_by_exact_key(
    tmp_path: Path,
) -> None:
    issues = FakeParentIssuesV2()
    items = FakeProjectItems()
    service, _database = _service_v2(tmp_path, issues, items)
    request = _request_v2(tmp_path)
    first_key = root_project_item_key(
        request.request_id,
        request.projects[0].project_id,
    )
    items.fail_create_after_key = first_key

    with pytest.raises(TaskServiceError, match="Project item creation failed"):
        service.create_task(request)
    created = service.create_task(request)

    assert len(items.items) == 2
    assert sum(call[0] == "create" for call in items.calls) == 2
    assert created.settings.status == "active"


def test_v2_activation_failure_rolls_back_settings_ready_and_activation_events(
    tmp_path: Path,
) -> None:
    issues = FakeParentIssuesV2()
    items = FakeProjectItems()
    service, database = _service_v2(tmp_path, issues, items)
    request = _request_v2(tmp_path)
    with database.transaction() as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_v2_activation
            BEFORE INSERT ON task_events
            WHEN NEW.event_type = 'settings_activated'
            BEGIN
                SELECT RAISE(ABORT, 'injected activation failure');
            END
            """
        )

    with pytest.raises(TaskServiceError, match="activation failed"):
        service.create_task(request)

    with database.read() as connection:
        assert connection.execute("SELECT count(*) FROM task_settings_v2").fetchone()[0] == 0
        states = {
            row[0]
            for row in connection.execute(
                "SELECT state FROM task_projects WHERE request_id = ?",
                (request.request_id,),
            )
        }
    assert states == {"bound"}
    assert not {
        "settings_activated",
        "active",
        "dispatch_ready",
    } & set(_event_types(database, request.request_id))

    with database.transaction() as connection:
        connection.execute("DROP TRIGGER fail_v2_activation")
    created = service.create_task(request)

    assert created.settings.status == "active"
    assert len(issues.issues) == 1
    assert len(items.items) == 2


def test_v2_exact_replay_reuses_objects_events_and_progress_projection(
    tmp_path: Path,
) -> None:
    issues = FakeParentIssuesV2()
    items = FakeProjectItems()
    service, database = _service_v2(tmp_path, issues, items)
    request = _request_v2(tmp_path)
    first = service.create_task(request)
    writes_after_first = (
        sum(call[0] in {"create", "update"} for call in issues.calls),
        sum(call[0] in {"create", "release"} for call in items.calls),
    )

    replayed = service.create_task(request)

    assert replayed == first
    assert (
        sum(call[0] in {"create", "update"} for call in issues.calls),
        sum(call[0] in {"create", "release"} for call in items.calls),
    ) == writes_after_first
    events = _event_types(database, request.request_id)
    assert len(events) == len(set(events)) + 1
    assert events.count("project_item_bound") == 2


def test_v2_concurrent_replay_across_services_creates_one_parent_and_roots(
    tmp_path: Path,
) -> None:
    issues = FakeParentIssuesV2()
    items = FakeProjectItems()
    database_path = tmp_path / "task-v2.db"
    services = (
        TaskServiceV2(
            TaskDatabase(database_path),
            issues,
            items,
            clock=lambda: NOW,
        ),
        TaskServiceV2(
            TaskDatabase(database_path),
            issues,
            items,
            clock=lambda: NOW,
        ),
    )
    request = _request_v2(tmp_path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            future.result(timeout=5)
            for future in (
                pool.submit(services[0].create_task, request),
                pool.submit(services[1].create_task, request),
            )
        )

    assert results[0] == results[1]
    assert len(issues.issues) == 1
    assert len(items.items) == 2
    assert sum(call[0] == "create" for call in issues.calls) == 1
    assert sum(call[0] == "create" for call in items.calls) == 2
    events = _event_types(TaskDatabase(database_path), request.request_id)
    assert events.count("settings_activated") == 1
    assert events.count("active") == 1
    assert events.count("dispatch_ready") == 1


def test_v2_parent_find_create_bind_guard_blocks_another_process(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "task-v2.db"
    ready_path = tmp_path / "contender-ready"
    enter_path = tmp_path / "contender-enter"
    entered_path = tmp_path / "contender-entered"
    database = TaskDatabase(database_path)
    script = (
        "from pathlib import Path\n"
        "from time import sleep\n"
        "from forge.ops.task_database import TaskDatabase\n"
        f"database = TaskDatabase({str(database_path)!r})\n"
        f"ready = Path({str(ready_path)!r})\n"
        f"enter = Path({str(enter_path)!r})\n"
        f"entered = Path({str(entered_path)!r})\n"
        "ready.write_text('ready', encoding='utf-8')\n"
        "while not enter.exists():\n"
        "    sleep(0.01)\n"
        "with database.transaction():\n"
        "    entered.write_text('entered', encoding='utf-8')\n"
    )
    contender = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    for _ in range(100):
        if ready_path.exists() or contender.poll() is not None:
            break
        sleep(0.05)
    assert ready_path.exists()
    assert contender.poll() is None

    class CrossProcessProbeIssues(FakeParentIssuesV2):
        def find_issue(
            self,
            repository: str,
            request_id: str,
        ) -> TaskParentIssue | None:
            enter_path.write_text("enter", encoding="utf-8")
            sleep(0.1)
            assert not entered_path.exists()
            assert contender.poll() is None
            return super().find_issue(repository, request_id)

    issues = CrossProcessProbeIssues()
    items = FakeProjectItems()
    service = TaskServiceV2(
        database,
        issues,
        items,
        clock=lambda: NOW,
    )

    service.create_task(_request_v2(tmp_path))

    stdout, stderr = contender.communicate(timeout=10)
    assert contender.returncode == 0, f"{stdout}\n{stderr}"
    assert entered_path.read_text(encoding="utf-8") == "entered"


def test_v2_same_request_id_with_different_hash_stops_before_external_calls(
    tmp_path: Path,
) -> None:
    issues = FakeParentIssuesV2()
    items = FakeProjectItems()
    service, _database = _service_v2(tmp_path, issues, items)
    service.create_task(_request_v2(tmp_path))
    calls_before = (len(issues.calls), len(items.calls))

    with pytest.raises(TaskServiceError, match="request_id.*different"):
        service.create_task(
            _request_v2(tmp_path, description="A different confirmed Task."),
        )

    assert (len(issues.calls), len(items.calls)) == calls_before


def test_v2_replacement_request_is_rejected_before_database_or_external_write(
    tmp_path: Path,
) -> None:
    issues = FakeParentIssuesV2()
    items = FakeProjectItems()
    service, database = _service_v2(tmp_path, issues, items)
    request = _request_v2(
        tmp_path,
        replaces_request_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    )

    with pytest.raises(TaskServiceError, match="replacement.*Task 12"):
        service.create_task(request)

    with database.read() as connection:
        assert connection.execute("SELECT count(*) FROM task_requests").fetchone()[0] == 0
    assert issues.calls == []
    assert items.calls == []


def test_v2_oversized_parent_body_stops_before_external_calls(tmp_path: Path) -> None:
    issues = FakeParentIssuesV2()
    items = FakeProjectItems()
    service, _database = _service_v2(tmp_path, issues, items)
    request = _request_v2(tmp_path, description="가" * 22_000)

    with pytest.raises(TaskServiceError, match="body is too large"):
        service.create_task(request)

    assert issues.calls == []
    assert items.calls == []


def test_v2_project_item_key_requires_canonical_request_project_and_step() -> None:
    key = root_project_item_key(V2_REQUEST_ID, "a" * 64)

    assert key == f"forge-task-v2:{V2_REQUEST_ID}:{'a' * 64}:build"
    with pytest.raises(TaskServiceError, match="idempotency key"):
        ProjectExecutionItem(
            item_id="root-1",
            idempotency_key=f"forge-task-v2:{'a' * 36}:{'b' * 64}:build",
            parent_issue_number=1,
            project_repository="owner/project",
            state="blocked",
        )


def test_v2_duplicate_or_mismatched_project_item_key_fails_closed(
    tmp_path: Path,
) -> None:
    request = _request_v2(tmp_path)
    key = root_project_item_key(request.request_id, request.projects[0].project_id)

    class DuplicateItems(FakeProjectItems):
        def find_items(
            self,
            management_repository: str,
            idempotency_key: str,
        ) -> tuple[ProjectExecutionItem, ...]:
            if idempotency_key != key:
                return super().find_items(management_repository, idempotency_key)
            return (
                ProjectExecutionItem(
                    "duplicate-1",
                    key,
                    1,
                    request.projects[0].repository,
                    "blocked",
                ),
                ProjectExecutionItem(
                    "duplicate-2",
                    key,
                    1,
                    request.projects[0].repository,
                    "blocked",
                ),
            )

    issues = FakeParentIssuesV2()
    service, database = _service_v2(tmp_path, issues, DuplicateItems())

    with pytest.raises(TaskServiceError, match="more than one root"):
        service.create_task(request)

    with database.read() as connection:
        assert connection.execute("SELECT count(*) FROM task_settings_v2").fetchone()[0] == 0

    class MismatchedItems(FakeProjectItems):
        def find_items(
            self,
            management_repository: str,
            idempotency_key: str,
        ) -> tuple[ProjectExecutionItem, ...]:
            if idempotency_key != key:
                return super().find_items(management_repository, idempotency_key)
            wrong_key = root_project_item_key(
                request.request_id,
                request.projects[1].project_id,
            )
            return (
                ProjectExecutionItem(
                    "wrong-key",
                    wrong_key,
                    1,
                    request.projects[0].repository,
                    "blocked",
                ),
            )

    other_root = tmp_path / "mismatch"
    other_root.mkdir()
    service = TaskServiceV2(
        TaskDatabase(other_root / "task-v2.db"),
        FakeParentIssuesV2(),
        MismatchedItems(),
        clock=lambda: NOW,
    )
    with pytest.raises(TaskServiceError, match="exact root binding"):
        service.create_task(request)


def test_v2_duplicate_root_ids_block_atomic_activation(tmp_path: Path) -> None:
    request = _request_v2(tmp_path)

    class DuplicateRootItems(FakeProjectItems):
        def create_item(
            self,
            management_repository: str,
            parent_issue_number: int,
            project_repository: str,
            idempotency_key: str,
            *,
            state: str,
        ) -> ProjectExecutionItem:
            item = ProjectExecutionItem(
                "same-root",
                idempotency_key,
                parent_issue_number,
                project_repository,
                state,
            )
            self.calls.append(
                (
                    "create",
                    management_repository,
                    parent_issue_number,
                    project_repository,
                    idempotency_key,
                    state,
                )
            )
            self.items[f"{idempotency_key}"] = (management_repository, item)
            return item

        def get_item(
            self,
            management_repository: str,
            item_id: str,
        ) -> ProjectExecutionItem:
            matches = [
                item
                for stored_repository, item in self.items.values()
                if stored_repository == management_repository and item.item_id == item_id
            ]
            return matches[-1]

    issues = FakeParentIssuesV2()
    items = DuplicateRootItems()
    service, database = _service_v2(tmp_path, issues, items)

    with pytest.raises(TaskServiceError, match="duplicate card IDs"):
        service.create_task(request)

    with database.read() as connection:
        assert connection.execute("SELECT count(*) FROM task_settings_v2").fetchone()[0] == 0
    assert not {
        "settings_activated",
        "active",
        "dispatch_ready",
    } & set(_event_types(database, request.request_id))


def test_v2_release_response_loss_replays_without_duplicate_roots(
    tmp_path: Path,
) -> None:
    issues = FakeParentIssuesV2()
    items = FakeProjectItems()
    service, _database = _service_v2(tmp_path, issues, items)
    request = _request_v2(tmp_path)
    items.fail_release_after_item = "root-1"

    with pytest.raises(TaskServiceError, match="Project item release failed"):
        service.create_task(request)
    created = service.create_task(request)

    assert created.settings.status == "active"
    assert len(issues.issues) == 1
    assert len(items.items) == 2
    assert sum(call[0] == "create" for call in items.calls) == 2


def test_v2_progress_response_loss_replays_without_external_duplicates(
    tmp_path: Path,
) -> None:
    issues = FakeParentIssuesV2()
    issues.fail_update_after_write = True
    items = FakeProjectItems()
    service, _database = _service_v2(tmp_path, issues, items)
    request = _request_v2(tmp_path)

    with pytest.raises(TaskServiceError, match="parent progress update failed"):
        service.create_task(request)
    created = service.create_task(request)

    assert "Ready" in created.parent_issue.body
    assert len(issues.issues) == 1
    assert len(items.items) == 2
    assert sum(call[0] == "create" for call in issues.calls) == 1
    assert sum(call[0] == "create" for call in items.calls) == 2


def test_v2_project_registry_tamper_stops_before_external_replay(
    tmp_path: Path,
) -> None:
    issues = FakeParentIssuesV2()
    items = FakeProjectItems()
    service, database = _service_v2(tmp_path, issues, items)
    request = _request_v2(tmp_path)
    service.create_task(request)
    calls_before = (len(issues.calls), len(items.calls))
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE task_projects
            SET project_json = ?
            WHERE request_id = ? AND project_id = ?
            """,
            (
                '{"project_id":"tampered"}',
                request.request_id,
                request.projects[0].project_id,
            ),
        )

    with pytest.raises(TaskServiceError, match="Project registry"):
        service.create_task(request)

    assert (len(issues.calls), len(items.calls)) == calls_before


def test_v2_bound_project_rejects_external_release_before_activation(
    tmp_path: Path,
) -> None:
    issues = FakeParentIssuesV2()
    items = FakeProjectItems()
    service, database = _service_v2(tmp_path, issues, items)
    request = _request_v2(tmp_path)
    second_key = root_project_item_key(
        request.request_id,
        request.projects[1].project_id,
    )
    items.fail_create_before_key = second_key
    with pytest.raises(TaskServiceError, match="Project item creation failed"):
        service.create_task(request)
    first_item_id = next(iter(items.items))
    repository, first_item = items.items[first_item_id]
    items.items[first_item_id] = (repository, replace(first_item, state="ready"))

    with pytest.raises(TaskServiceError, match="released before activation"):
        service.create_task(request)

    with database.read() as connection:
        assert connection.execute("SELECT count(*) FROM task_settings_v2").fetchone()[0] == 0


@pytest.mark.parametrize("root_card_id", ["", "x" * 513])
def test_v2_stored_root_card_id_must_be_bounded_nonempty_text(
    tmp_path: Path,
    root_card_id: str,
) -> None:
    issues = FakeParentIssuesV2()
    items = FakeProjectItems()
    service, database = _service_v2(tmp_path, issues, items)
    request = _request_v2(tmp_path)
    second_key = root_project_item_key(
        request.request_id,
        request.projects[1].project_id,
    )
    items.fail_create_before_key = second_key
    with pytest.raises(TaskServiceError, match="Project item creation failed"):
        service.create_task(request)
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE task_projects
            SET root_card_id = ?
            WHERE request_id = ? AND state = 'bound'
            """,
            (root_card_id, request.request_id),
        )

    with pytest.raises(TaskServiceError, match="root card ID"):
        service.create_task(request)


def test_v2_stored_event_time_must_be_canonical_utc_before_external_replay(
    tmp_path: Path,
) -> None:
    issues = FakeParentIssuesV2()
    items = FakeProjectItems()
    service, database = _service_v2(tmp_path, issues, items)
    request = _request_v2(tmp_path)
    service.create_task(request)
    calls_before = (len(issues.calls), len(items.calls))
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE task_events
            SET occurred_at = '2026-07-16 10:00:00'
            WHERE request_id = ? AND event_key = 'request_prepared'
            """,
            (request.request_id,),
        )

    with pytest.raises(TaskServiceError, match="event time"):
        service.create_task(request)

    assert (len(issues.calls), len(items.calls)) == calls_before


def test_v2_duplicate_request_prepared_event_stops_before_external_replay(
    tmp_path: Path,
) -> None:
    issues = FakeParentIssuesV2()
    items = FakeProjectItems()
    service, database = _service_v2(tmp_path, issues, items)
    request = _request_v2(tmp_path)
    service.create_task(request)
    calls_before = (len(issues.calls), len(items.calls))
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO task_events (
                request_id, task_settings_hash, project_id, event_type,
                event_key, event_json, occurred_at
            ) VALUES (?, NULL, NULL, 'request_prepared', ?, ?, ?)
            """,
            (
                request.request_id,
                "duplicate_request_prepared",
                json.dumps(
                    {"request_hash": request.request_hash},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                "2026-07-16T10:00:00Z",
            ),
        )

    with pytest.raises(TaskServiceError, match="request_prepared event"):
        service.create_task(request)

    assert (len(issues.calls), len(items.calls)) == calls_before


def test_v2_whitespace_root_id_is_rejected_before_registry_binding(
    tmp_path: Path,
) -> None:
    class WhitespaceRootItems(FakeProjectItems):
        def create_item(
            self,
            management_repository: str,
            parent_issue_number: int,
            project_repository: str,
            idempotency_key: str,
            *,
            state: str,
        ) -> ProjectExecutionItem:
            item = ProjectExecutionItem(
                item_id=" root ",
                idempotency_key=idempotency_key,
                parent_issue_number=parent_issue_number,
                project_repository=project_repository,
                state=state,
            )
            self.items[item.item_id] = (management_repository, item)
            return item

    issues = FakeParentIssuesV2()
    items = WhitespaceRootItems()
    service, database = _service_v2(tmp_path, issues, items)
    request = _request_v2(tmp_path)

    with pytest.raises(TaskServiceError, match="Project item creation failed"):
        service.create_task(request)

    with database.read() as connection:
        rows = connection.execute(
            """
            SELECT state, root_card_id
            FROM task_projects
            WHERE request_id = ?
            """,
            (request.request_id,),
        ).fetchall()
    assert {tuple(row) for row in rows} == {("prepared", None)}


def test_v2_stop_after_root_response_loss_binds_found_root_without_new_writes(
    tmp_path: Path,
) -> None:
    issues = FakeParentIssuesV2()
    items = FakeProjectItems()
    service, database = _service_v2(tmp_path, issues, items)
    request = _request_v2(tmp_path)
    first_key = root_project_item_key(
        request.request_id,
        request.projects[0].project_id,
    )
    items.fail_create_after_key = first_key
    with pytest.raises(TaskServiceError, match="Project item creation failed"):
        service.create_task(request)
    _append_v2_lifecycle_event(database, request, "stop_requested")

    with pytest.raises(TaskServiceError, match="lifecycle barrier"):
        service.create_task(request)

    assert len(items.items) == 1
    assert sum(call[0] == "create" for call in items.calls) == 1
    assert sum(call[0] == "release" for call in items.calls) == 0
    assert {item.state for _, item in items.items.values()} == {"blocked"}
    with database.read() as connection:
        rows = connection.execute(
            """
            SELECT state, root_card_id
            FROM task_projects
            WHERE request_id = ?
            ORDER BY project_json
            """,
            (request.request_id,),
        ).fetchall()
        settings_count = connection.execute(
            "SELECT count(*) FROM task_settings_v2 WHERE request_id = ?",
            (request.request_id,),
        ).fetchone()[0]
    assert [row[0] for row in rows].count("bound") == 1
    assert [row[0] for row in rows].count("prepared") == 1
    assert [row[1] is not None for row in rows].count(True) == 1
    assert settings_count == 0


def test_v2_stop_after_partial_bind_prevents_remaining_root_creation(
    tmp_path: Path,
) -> None:
    issues = FakeParentIssuesV2()
    items = FakeProjectItems()
    service, database = _service_v2(tmp_path, issues, items)
    request = _request_v2(tmp_path)
    second_key = root_project_item_key(
        request.request_id,
        request.projects[1].project_id,
    )
    items.fail_create_before_key = second_key
    with pytest.raises(TaskServiceError, match="Project item creation failed"):
        service.create_task(request)
    creates_before_stop = sum(call[0] == "create" for call in items.calls)
    _append_v2_lifecycle_event(database, request, "stop_requested")

    with pytest.raises(TaskServiceError, match="lifecycle barrier"):
        service.create_task(request)

    assert sum(call[0] == "create" for call in items.calls) == creates_before_stop
    assert len(items.items) == 1
    assert sum(call[0] == "release" for call in items.calls) == 0


def test_v2_stop_during_first_release_blocks_remaining_releases_and_success(
    tmp_path: Path,
) -> None:
    class StopOnFirstReleaseItems(FakeProjectItems):
        stopped = False

        def release_item(
            self,
            management_repository: str,
            item_id: str,
        ) -> ProjectExecutionItem:
            released = super().release_item(management_repository, item_id)
            if not self.stopped:
                self.stopped = True
                _append_v2_lifecycle_event(database, request, "stop_requested")
            return released

    issues = FakeParentIssuesV2()
    items = StopOnFirstReleaseItems()
    service, database = _service_v2(tmp_path, issues, items)
    request = _request_v2(tmp_path)

    with pytest.raises(TaskServiceError, match="lifecycle barrier"):
        service.create_task(request)

    assert sum(call[0] == "release" for call in items.calls) == 1
    assert [item.state for _, item in items.items.values()].count("ready") == 1
    assert [item.state for _, item in items.items.values()].count("blocked") == 1
    assert sum(call[0] == "update" for call in issues.calls) == 0
