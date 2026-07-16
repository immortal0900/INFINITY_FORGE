from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, BrokenBarrierError, Event, Lock
from time import sleep
from uuid import uuid4

import pytest

from forge.ops.task_options import MergeMode, TaskFlow
from forge.ops.task_outbox import TaskOutbox
from forge.ops.task_service import (
    READY_TO_BUILD_LABEL,
    TaskCreationRequest,
    TaskIssue,
    TaskService,
    TaskServiceError,
    read_task_marker,
)
from forge.ops.task_settings import (
    TaskContent,
    TaskSettingsStatus,
    TaskSettingsStore,
)


NOW = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)


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
