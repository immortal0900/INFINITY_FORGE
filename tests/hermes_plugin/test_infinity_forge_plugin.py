from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from threading import Barrier, Lock
from time import sleep
from typing import Any

import pytest

import forge.hermes_plugin.infinity_forge as plugin
from forge.ops.task_options import MergeMode, TaskFlow
from forge.ops.task_projects import TaskProject
from forge.ops.task_service import TaskCreationRequest
from forge.ops.task_setup import TaskSetupContext
from forge.ops.task_settings import TaskContent


REPOSITORY = "owner/repo"


@pytest.fixture(autouse=True)
def _reset_plugin_state(monkeypatch) -> None:
    monkeypatch.setattr(plugin, "_task_setup", plugin.TaskSetup())
    monkeypatch.setattr(
        plugin,
        "_task_context_factory",
        lambda _working_directory: None,
        raising=False,
    )
    plugin._failed_inputs.clear()
    pending = getattr(plugin, "_pending_tasks", None)
    if pending is not None:
        pending.clear()
    plugin.set_task_service(None)


OWNER_HOST = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
MANAGEMENT_REPOSITORY = "management/forge"


def _project(root: Path, repository: str, commit: str = "a" * 40) -> TaskProject:
    root.mkdir(parents=True, exist_ok=True)
    return TaskProject.create(
        repository=repository,
        workspace=str(root.resolve()),
        remote_name="origin",
        base_branch="main",
        base_commit=commit,
        host_id=OWNER_HOST,
    )


def _v2_context(
    working_directory: str | None,
    projects: tuple[TaskProject, ...],
    *,
    discover=None,
    validate=None,
) -> TaskSetupContext:
    return TaskSetupContext(
        working_directory=working_directory,
        management_repository=MANAGEMENT_REPOSITORY,
        task_owner_host=OWNER_HOST,
        discover_projects=discover or (lambda _working: projects),
        validate_projects=validate or (lambda selected: selected),
    )


@dataclass
class FakePluginContext:
    hooks: dict[str, Any] = field(default_factory=dict)
    commands: dict[str, dict[str, Any]] = field(default_factory=dict)

    def register_hook(self, name: str, handler: Any) -> None:
        self.hooks[name] = handler

    def register_command(
        self,
        name: str,
        handler: Any,
        description: str = "",
        args_hint: str = "",
    ) -> None:
        self.commands[name] = {
            "handler": handler,
            "description": description,
            "args_hint": args_hint,
        }


def test_registers_hook_and_slash_commands_with_hermes_signature() -> None:
    context = FakePluginContext()

    plugin.register(context)

    assert context.hooks["pre_user_turn"] is plugin.before_user_turn
    assert set(context.commands) == {"task", "cancel"}
    assert context.commands["task"]["args_hint"] == ""
    assert context.commands["cancel"]["args_hint"] == ""
    assert "session context" in context.commands["task"]["handler"]("").lower()
    assert "session context" in context.commands["cancel"]["handler"]("").lower()


def test_hook_serializes_handled_replace_and_continue_results(monkeypatch) -> None:
    setup = plugin.TaskSetup()
    monkeypatch.setattr(plugin, "_task_setup", setup)
    plugin._failed_inputs.clear()

    handled = plugin.before_user_turn(
        {"session_id": "s1", "user_id": "u1", "text": "원문"}
    )
    replaced = plugin.before_user_turn(
        session_id="s1", user_id="u1", text="chat"
    )
    continued = plugin.before_user_turn(
        session_id="s1", user_id="u1", text="다음 질문"
    )

    assert handled["action"] == "handled"
    assert [choice["id"] for choice in handled["choices"]] == ["chat", "task"]
    assert replaced == {"action": "replace", "text": "원문"}
    assert continued == {"action": "continue"}


def test_hook_emits_and_fail_closed_validates_structured_chooser_metadata(monkeypatch) -> None:
    calls: list[object] = []
    plugin.set_task_service(lambda request: calls.append(request) or "created")

    shown = plugin.before_user_turn(
        session_id="s1", user_id="u1", text="original request"
    )

    assert shown["choice_mode"] == "single"
    assert shown["min_choices"] == 1
    assert shown["max_choices"] == 1
    assert isinstance(shown["choice_prompt_id"], str)
    assert isinstance(shown["expires_at"], str)
    assert [choice["id"] for choice in shown["choices"]] == ["chat", "task"]

    malformed = plugin.before_user_turn(
        session_id="s1",
        user_id="u1",
        text="ignored label",
        choice_prompt_id=shown["choice_prompt_id"],
        selected_choice_ids=["task", "task"],
    )
    assert malformed["action"] == "handled"
    assert malformed["choice_prompt_id"] == shown["choice_prompt_id"]
    assert calls == []

    selected = plugin.before_user_turn(
        session_id="s1",
        user_id="u1",
        text="not authoritative",
        choice_prompt_id=shown["choice_prompt_id"],
        selected_choice_ids=["task"],
    )
    assert selected["action"] == "handled"
    assert selected["choice_mode"] == "single"
    assert calls == []


def test_plugin_error_offers_retry_or_explicit_chat_without_silent_task(
    monkeypatch,
) -> None:
    class BrokenSetup:
        def handle(self, *args, **kwargs):
            raise RuntimeError("selector unavailable")

        def enter_chat(self, *args, **kwargs):
            raise AssertionError("not used in this assertion")

    monkeypatch.setattr(plugin, "_task_setup", BrokenSetup())
    plugin._failed_inputs.clear()

    result = plugin.before_user_turn(
        session_id="s1", user_id="u1", text="작업 시작"
    )

    assert result["action"] == "handled"
    assert [choice["id"] for choice in result["choices"]] == [
        "retry",
        "continue_chat",
    ]
    assert result["action"] not in {"continue", "replace"}
    assert "selector unavailable" in result["text"]


def test_continue_in_chat_after_plugin_error_is_explicit(monkeypatch) -> None:
    class FailsOnceSetup(plugin.TaskSetup):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        def handle(self, *args, **kwargs):
            if not self.failed:
                self.failed = True
                raise RuntimeError("temporary failure")
            return super().handle(*args, **kwargs)

    monkeypatch.setattr(plugin, "_task_setup", FailsOnceSetup())
    plugin._failed_inputs.clear()
    plugin.before_user_turn(session_id="s1", user_id="u1", text="첫 입력")

    rejected = plugin.before_user_turn(session_id="s1", user_id="u1", text="task")
    result = plugin.before_user_turn(session_id="s1", user_id="u1", text="continue_chat")
    next_turn = plugin.before_user_turn(
        session_id="s1", user_id="u1", text="대화 질문"
    )

    assert rejected["action"] == "handled"
    assert [choice["id"] for choice in rejected["choices"]] == [
        "retry",
        "continue_chat",
    ]
    assert result == {"action": "replace", "text": "첫 입력"}
    assert next_turn == {"action": "continue"}


def test_retry_replays_the_failed_input(monkeypatch) -> None:
    class FailsOnceSetup(plugin.TaskSetup):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        def handle(self, *args, **kwargs):
            if not self.failed:
                self.failed = True
                raise RuntimeError("temporary failure")
            return super().handle(*args, **kwargs)

    monkeypatch.setattr(plugin, "_task_setup", FailsOnceSetup())
    plugin._failed_inputs.clear()
    plugin.before_user_turn(session_id="s1", user_id="u1", text="원래 요청")

    result = plugin.before_user_turn(session_id="s1", user_id="u1", text="retry")
    replay = plugin.before_user_turn(session_id="s1", user_id="u1", text="chat")

    assert result["action"] == "handled"
    assert replay == {"action": "replace", "text": "원래 요청"}


def _complete_task_until_confirmation(monkeypatch) -> dict[str, object]:
    monkeypatch.setenv("INFINITY_FORGE_REPOSITORY", REPOSITORY)
    common = {"session_id": "task-session", "user_id": "u1", "surface": "tui"}
    plugin.before_user_turn(text="고칠 내용", is_new_session=True, **common)
    plugin.before_user_turn(text="task", is_new_session=False, **common)
    plugin.before_user_turn(text="build_review", is_new_session=False, **common)
    return plugin.before_user_turn(text="safe_auto", is_new_session=False, **common)


def test_task_preview_requires_confirm_then_calls_service_exactly_once(monkeypatch) -> None:
    calls: list[object] = []

    def create_task(request) -> str:
        calls.append(request)
        return "Task #42 created."

    plugin.set_task_service(create_task)
    preview = _complete_task_until_confirmation(monkeypatch)

    assert preview["action"] == "handled"
    assert [choice["id"] for choice in preview["choices"]] == ["confirm", "cancel"]
    assert calls == []

    confirmed = plugin.before_user_turn(
        session_id="task-session",
        user_id="u1",
        surface="tui",
        is_new_session=False,
        text="confirm",
    )
    duplicate = plugin.before_user_turn(
        session_id="task-session",
        user_id="u1",
        surface="tui",
        is_new_session=False,
        text="confirm",
    )

    assert confirmed == {"action": "handled", "text": "Task #42 created."}
    assert len(calls) == 1
    assert calls[0].repository == REPOSITORY
    assert calls[0].confirmed_by == "u1"
    assert calls[0].confirmed_at.utcoffset().total_seconds() == 0
    assert calls[0].content.description == "고칠 내용"
    assert duplicate == {"action": "continue"}


def test_default_task_service_missing_config_fails_before_external_write(monkeypatch) -> None:
    monkeypatch.delenv("INFINITY_FORGE_TASK_SETTINGS_DB", raising=False)
    monkeypatch.delenv("INFINITY_FORGE_GH_PATH", raising=False)

    class MustNotConstruct:
        def __init__(self, *args, **kwargs):
            raise AssertionError("external dependency must not be constructed")

    monkeypatch.setattr(plugin, "TaskSettingsStore", MustNotConstruct, raising=False)
    monkeypatch.setattr(plugin, "GitHubTaskIssueClient", MustNotConstruct, raising=False)
    plugin.set_task_service(None)
    _complete_task_until_confirmation(monkeypatch)

    result = plugin.before_user_turn(
        session_id="task-session",
        user_id="u1",
        surface="tui",
        is_new_session=False,
        text="confirm",
    )

    assert result["action"] == "handled"
    assert "INFINITY_FORGE_TASK_SETTINGS_DB" in str(result["text"])
    assert [choice["id"] for choice in result["choices"]] == ["retry", "cancel"]
    assert result["action"] not in {"continue", "replace"}


def test_task_service_error_retries_the_same_frozen_request(monkeypatch) -> None:
    requests: list[object] = []

    def flaky_task_service(request) -> str:
        requests.append(request)
        if len(requests) == 1:
            raise RuntimeError("service offline")
        return "Task #9 created."

    plugin.set_task_service(flaky_task_service)
    _complete_task_until_confirmation(monkeypatch)

    failed = plugin.before_user_turn(
        session_id="task-session",
        user_id="u1",
        surface="tui",
        is_new_session=False,
        text="confirm",
    )
    blocked = plugin.before_user_turn(
        session_id="task-session",
        user_id="u1",
        surface="tui",
        is_new_session=False,
        text="새 요청으로 바꾸기",
    )
    retried = plugin.before_user_turn(
        session_id="task-session",
        user_id="u1",
        surface="tui",
        is_new_session=False,
        text="retry",
    )

    assert failed["action"] == "handled"
    assert [choice["id"] for choice in failed["choices"]] == ["retry", "cancel"]
    assert [choice["id"] for choice in blocked["choices"]] == ["retry", "cancel"]
    assert retried == {"action": "handled", "text": "Task #9 created."}
    assert len(requests) == 2
    assert requests[0] is requests[1]


def test_pending_retry_rejects_malformed_structured_envelope_before_service_call(
    monkeypatch,
) -> None:
    calls: list[object] = []

    def failing_task_service(request) -> str:
        calls.append(request)
        raise RuntimeError("service offline")

    plugin.set_task_service(failing_task_service)
    _complete_task_until_confirmation(monkeypatch)
    plugin.before_user_turn(
        session_id="task-session",
        user_id="u1",
        surface="tui",
        is_new_session=False,
        text="confirm",
    )

    rejected = plugin.before_user_turn(
        session_id="task-session",
        user_id="u1",
        surface="tui",
        is_new_session=False,
        text="retry",
        choice_prompt_id="not-a-uuid",
        selected_choice_ids=["retry"],
    )

    assert rejected["action"] == "handled"
    assert "choice_prompt_id" in str(rejected["text"])
    assert len(calls) == 1


def test_new_session_rejects_stale_structured_confirm_without_task_service_call(
    monkeypatch,
) -> None:
    calls: list[object] = []
    plugin.set_task_service(lambda request: calls.append(request) or "created")
    preview = _complete_task_until_confirmation(monkeypatch)

    rejected = plugin.before_user_turn(
        session_id="task-session",
        user_id="u1",
        surface="tui",
        is_new_session=True,
        text="ignored",
        choice_prompt_id=preview["choice_prompt_id"],
        selected_choice_ids=["confirm"],
    )

    assert rejected["action"] == "handled"
    assert calls == []


def test_structured_confirmation_capacity_uses_selected_id_and_preserves_draft(
    monkeypatch,
) -> None:
    calls: list[object] = []
    plugin.set_task_service(lambda request: calls.append(request) or "created")
    preview = _complete_task_until_confirmation(monkeypatch)
    monkeypatch.setattr(plugin, "_MAX_PENDING_TASKS", 1)
    plugin._pending_tasks[("tui", "other", "user")] = object()

    blocked = plugin.before_user_turn(
        session_id="task-session",
        user_id="u1",
        surface="tui",
        is_new_session=False,
        text="not-confirm",
        choice_prompt_id=preview["choice_prompt_id"],
        selected_choice_ids=["confirm"],
    )
    plugin._pending_tasks.clear()
    confirmed = plugin.before_user_turn(
        session_id="task-session",
        user_id="u1",
        surface="tui",
        is_new_session=False,
        text="not-confirm",
        choice_prompt_id=preview["choice_prompt_id"],
        selected_choice_ids=["confirm"],
    )

    assert blocked["action"] == "handled"
    assert "temporarily full" in str(blocked["text"])
    assert confirmed == {"action": "handled", "text": "created"}
    assert len(calls) == 1


def test_capacity_rejection_replays_the_same_structured_confirmation_prompt(
    monkeypatch,
) -> None:
    plugin.set_task_service(lambda request: "created")
    preview = _complete_task_until_confirmation(monkeypatch)
    monkeypatch.setattr(plugin, "_MAX_PENDING_TASKS", 1)
    plugin._pending_tasks[("tui", "other", "user")] = object()

    blocked = plugin.before_user_turn(
        session_id="task-session",
        user_id="u1",
        surface="tui",
        is_new_session=False,
        text="not-confirm",
        choice_prompt_id=preview["choice_prompt_id"],
        selected_choice_ids=["confirm"],
    )

    metadata_keys = (
        "choice_prompt_id",
        "choice_mode",
        "min_choices",
        "max_choices",
        "submit_label",
        "expires_at",
        "choices",
    )
    preview_metadata = {key: preview[key] for key in metadata_keys}
    blocked_metadata = {key: blocked[key] for key in metadata_keys}
    assert json.dumps(blocked_metadata, separators=(",", ":")) == json.dumps(
        preview_metadata, separators=(",", ":")
    )


def test_expired_structured_confirmation_beats_capacity_without_losing_prompt(
    monkeypatch,
) -> None:
    preview = _complete_task_until_confirmation(monkeypatch)
    prompt = plugin._task_setup.pending_choice_prompt(
        "task-session", "u1", surface="tui"
    )
    assert prompt is not None
    monkeypatch.setattr(plugin, "_MAX_PENDING_TASKS", 1)
    plugin._pending_tasks[("tui", "other", "user")] = object()

    rejected = plugin.before_user_turn(
        session_id="task-session",
        user_id="u1",
        surface="tui",
        is_new_session=False,
        now=prompt.expires_at,
        text="ignored",
        choice_prompt_id=preview["choice_prompt_id"],
        selected_choice_ids=["confirm"],
    )
    preserved = plugin._task_setup.pending_choice_prompt(
        "task-session", "u1", prompt.expires_at, surface="tui"
    )

    assert rejected["action"] == "handled"
    assert "choice prompt expired" in str(rejected["text"])
    assert rejected["choice_prompt_id"] == preview["choice_prompt_id"]
    assert rejected["expires_at"] == preview["expires_at"]
    assert preserved == prompt


def test_capacity_does_not_replay_a_previous_session_prompt(monkeypatch) -> None:
    plugin.set_task_service(lambda request: "created")
    preview = _complete_task_until_confirmation(monkeypatch)
    monkeypatch.setattr(plugin, "_MAX_PENDING_TASKS", 1)
    plugin._pending_tasks[("tui", "other", "user")] = object()

    rejected = plugin.before_user_turn(
        session_id="task-session",
        user_id="u1",
        surface="tui",
        is_new_session=True,
        text="ignored",
        choice_prompt_id=preview["choice_prompt_id"],
        selected_choice_ids=["confirm"],
    )

    assert rejected["action"] == "handled"
    assert rejected["text"] == "No pending chooser is available."


def test_structured_cancel_is_not_blocked_by_confirm_text_when_pending_is_full(
    monkeypatch,
) -> None:
    preview = _complete_task_until_confirmation(monkeypatch)
    monkeypatch.setattr(plugin, "_MAX_PENDING_TASKS", 1)
    plugin._pending_tasks[("tui", "other", "user")] = object()

    cancelled = plugin.before_user_turn(
        session_id="task-session",
        user_id="u1",
        surface="tui",
        is_new_session=False,
        text="confirm",
        choice_prompt_id=preview["choice_prompt_id"],
        selected_choice_ids=["cancel"],
    )

    assert cancelled["action"] == "handled"
    assert "cancelled" in str(cancelled["text"]).lower()


def test_pending_task_can_only_be_cancelled_without_another_service_call(monkeypatch) -> None:
    requests: list[object] = []

    def fail_task_service(request) -> str:
        requests.append(request)
        raise RuntimeError("service offline")

    plugin.set_task_service(fail_task_service)
    _complete_task_until_confirmation(monkeypatch)
    plugin.before_user_turn(
        session_id="task-session",
        user_id="u1",
        surface="tui",
        is_new_session=False,
        text="confirm",
    )

    cancelled = plugin.before_user_turn(
        session_id="task-session",
        user_id="u1",
        surface="tui",
        is_new_session=False,
        text="cancel",
    )

    assert cancelled["action"] == "handled"
    assert "cancelled" in str(cancelled["text"]).lower()
    assert len(requests) == 1


def test_concurrent_pending_retries_call_service_once(monkeypatch) -> None:
    first_request: list[object] = []

    def initial_failure(request) -> str:
        first_request.append(request)
        raise RuntimeError("temporary")

    plugin.set_task_service(initial_failure)
    _complete_task_until_confirmation(monkeypatch)
    plugin.before_user_turn(
        session_id="task-session",
        user_id="u1",
        surface="tui",
        is_new_session=False,
        text="confirm",
    )
    successful_calls: list[object] = []

    def succeeds(request) -> str:
        successful_calls.append(request)
        sleep(0.05)
        return "Task #10 created."

    plugin.set_task_service(succeeds)
    start = Barrier(3)

    def retry() -> dict[str, object]:
        start.wait()
        return plugin.before_user_turn(
            session_id="task-session",
            user_id="u1",
            surface="tui",
            is_new_session=False,
            text="retry",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(retry) for _ in range(2)]
        start.wait()
        results = [future.result(timeout=2) for future in futures]

    assert len(successful_calls) == 1
    assert successful_calls[0] is first_request[0]
    assert results.count({"action": "handled", "text": "Task #10 created."}) == 1


def test_task_service_calls_for_different_sessions_can_run_concurrently(
    monkeypatch,
) -> None:
    monkeypatch.setenv("INFINITY_FORGE_REPOSITORY", REPOSITORY)
    for session_id, user_id in (("task-a", "u-a"), ("task-b", "u-b")):
        common = {
            "session_id": session_id,
            "user_id": user_id,
            "surface": "tui",
        }
        plugin.before_user_turn(text="고칠 내용", is_new_session=True, **common)
        plugin.before_user_turn(text="task", is_new_session=False, **common)
        plugin.before_user_turn(text="build", is_new_session=False, **common)
        plugin.before_user_turn(text="manual", is_new_session=False, **common)

    tracker = Lock()
    active = 0
    max_active = 0

    def create_task(request) -> str:
        nonlocal active, max_active
        with tracker:
            active += 1
            max_active = max(max_active, active)
        try:
            sleep(0.1)
            return f"Task for {request.confirmed_by} created."
        finally:
            with tracker:
                active -= 1

    plugin.set_task_service(create_task)
    start = Barrier(3)

    def confirm(session_id: str, user_id: str) -> dict[str, object]:
        start.wait()
        return plugin.before_user_turn(
            session_id=session_id,
            user_id=user_id,
            surface="tui",
            is_new_session=False,
            text="confirm",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(confirm, "task-a", "u-a"),
            pool.submit(confirm, "task-b", "u-b"),
        ]
        start.wait()
        results = [future.result(timeout=2) for future in futures]

    assert max_active == 2
    assert {str(result["text"]) for result in results} == {
        "Task for u-a created.",
        "Task for u-b created.",
    }


def test_default_task_service_lazily_wires_store_github_and_service(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeStore:
        def __init__(self, path: str) -> None:
            captured["database"] = path
            self.database_path = Path(path)

    class FakeOutbox:
        def __init__(self, path: Path) -> None:
            captured["outbox_path"] = path

        def load_pending_for_user(self, repository: str, confirmed_by: str):
            captured["pending_lookup"] = (repository, confirmed_by)
            return None

    class FakeGitHub:
        def __init__(self, path: str) -> None:
            captured["gh_path"] = path

    class FakeTaskService:
        def __init__(self, store, github) -> None:
            captured["store"] = store
            captured["github"] = github

        def create_task_durable(self, request, outbox):
            captured["request"] = request
            captured["outbox"] = outbox
            return SimpleNamespace(issue=SimpleNamespace(number=17))

    monkeypatch.setattr(plugin, "TaskSettingsStore", FakeStore, raising=False)
    monkeypatch.setattr(plugin, "TaskOutbox", FakeOutbox, raising=False)
    monkeypatch.setattr(plugin, "GitHubTaskIssueClient", FakeGitHub, raising=False)
    monkeypatch.setattr(plugin, "LocalTaskService", FakeTaskService, raising=False)
    monkeypatch.setenv("INFINITY_FORGE_TASK_SETTINGS_DB", "C:/state/tasks.db")
    monkeypatch.setenv("INFINITY_FORGE_GH_PATH", "C:/tools/gh.exe")
    plugin.set_task_service(None)
    _complete_task_until_confirmation(monkeypatch)

    result = plugin.before_user_turn(
        session_id="task-session",
        user_id="u1",
        surface="tui",
        is_new_session=False,
        text="confirm",
    )

    assert result == {
        "action": "handled",
        "text": "Task #17 created: https://github.com/owner/repo/issues/17",
    }
    assert captured["database"] == "C:/state/tasks.db"
    assert captured["outbox_path"] == Path("C:/state/tasks.db.task-outbox.db")
    assert captured["pending_lookup"] == (REPOSITORY, "u1")
    assert captured["gh_path"] == "C:/tools/gh.exe"
    assert captured["request"].repository == REPOSITORY


def test_default_service_replays_same_users_pending_request_after_restart(
    monkeypatch,
) -> None:
    pending = TaskCreationRequest(
        request_id="9f7453ce-36ec-4e8e-9dfa-bb159b58c19b",
        repository=REPOSITORY,
        content=TaskContent(
            title="Pending Task",
            description="Original confirmed work",
            acceptance_criteria=("Resume the exact request.",),
        ),
        task_flow=TaskFlow.BUILD_REVIEW,
        merge_mode=MergeMode.SAFE_AUTO,
        confirmed_by="u1",
        confirmed_at=datetime(2026, 7, 16, 10, 0, tzinfo=UTC),
    )
    replacement = TaskCreationRequest(
        request_id="59e70c97-fdd2-4bf1-bf2a-71df20e68d57",
        repository=REPOSITORY,
        content=TaskContent(
            title="New Task",
            description="A new chooser confirmation after restart",
            acceptance_criteria=("Do not replace an unfinished request.",),
        ),
        task_flow=TaskFlow.BUILD,
        merge_mode=MergeMode.MANUAL,
        confirmed_by="u1",
        confirmed_at=datetime(2026, 7, 16, 11, 0, tzinfo=UTC),
    )
    captured: dict[str, object] = {}

    class FakeStore:
        def __init__(self, path: str) -> None:
            self.database_path = Path(path)

    class FakeOutbox:
        def __init__(self, path: Path) -> None:
            captured["outbox_path"] = path

        def load_pending_for_user(self, repository: str, confirmed_by: str):
            assert (repository, confirmed_by) == (REPOSITORY, "u1")
            return pending

    class FakeGitHub:
        def __init__(self, path: str) -> None:
            del path

    class FakeTaskService:
        def __init__(self, store, github) -> None:
            del store, github

        def create_task_durable(self, request, outbox):
            captured["request"] = request
            captured["outbox"] = outbox
            return SimpleNamespace(issue=SimpleNamespace(number=23))

    monkeypatch.setattr(plugin, "TaskSettingsStore", FakeStore, raising=False)
    monkeypatch.setattr(plugin, "TaskOutbox", FakeOutbox, raising=False)
    monkeypatch.setattr(plugin, "GitHubTaskIssueClient", FakeGitHub, raising=False)
    monkeypatch.setattr(plugin, "LocalTaskService", FakeTaskService, raising=False)
    monkeypatch.setenv("INFINITY_FORGE_REPOSITORY", REPOSITORY)
    monkeypatch.setenv("INFINITY_FORGE_TASK_SETTINGS_DB", "C:/state/tasks.db")
    monkeypatch.setenv("INFINITY_FORGE_GH_PATH", "C:/tools/gh.exe")

    message = plugin._default_task_service(replacement)

    assert message == "Task #23 created: https://github.com/owner/repo/issues/23"
    assert captured["request"] is pending


def test_chat_never_calls_task_service_store_or_github(monkeypatch) -> None:
    calls = 0

    def task_service(request) -> str:
        nonlocal calls
        calls += 1
        return "unexpected"

    class MustNotConstruct:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Chat must not construct Task dependencies")

    plugin.set_task_service(task_service)
    monkeypatch.setattr(plugin, "TaskSettingsStore", MustNotConstruct, raising=False)
    monkeypatch.setattr(plugin, "GitHubTaskIssueClient", MustNotConstruct, raising=False)
    plugin.before_user_turn(
        session_id="chat", user_id="u1", surface="tui", is_new_session=True, text="질문"
    )
    replay = plugin.before_user_turn(
        session_id="chat", user_id="u1", surface="tui", is_new_session=False, text="chat"
    )

    assert replay == {"action": "replace", "text": "질문"}
    assert calls == 0


def test_hook_uses_surface_and_new_session_to_reset_state(monkeypatch) -> None:
    monkeypatch.setattr(plugin, "_task_setup", plugin.TaskSetup())
    plugin._failed_inputs.clear()

    plugin.before_user_turn(
        session_id="same", user_id="u1", surface="tui", is_new_session=True, text="첫 입력"
    )
    plugin.before_user_turn(
        session_id="same", user_id="u1", surface="tui", is_new_session=False, text="chat"
    )
    slack = plugin.before_user_turn(
        session_id="same", user_id="u1", surface="slack", is_new_session=False, text="Slack 입력"
    )
    restarted = plugin.before_user_turn(
        session_id="same", user_id="u1", surface="tui", is_new_session=True, text="새 입력"
    )

    assert [choice["id"] for choice in slack["choices"]] == ["chat", "task"]
    assert [choice["id"] for choice in restarted["choices"]] == ["chat", "task"]


def test_field_error_uses_stable_fallback_key_for_continue_chat(monkeypatch) -> None:
    monkeypatch.setattr(plugin, "_task_setup", plugin.TaskSetup())
    plugin._failed_inputs.clear()

    failed = plugin.before_user_turn(
        user_id="u1", surface="tui", is_new_session=True, text="원래 입력"
    )
    continued = plugin.before_user_turn(
        user_id="u1", surface="tui", is_new_session=False, text="continue_chat"
    )

    assert failed["action"] == "handled"
    assert continued == {"action": "replace", "text": "원래 입력"}


def test_failed_field_retry_keeps_original_input_for_continue_chat(monkeypatch) -> None:
    monkeypatch.setattr(plugin, "_task_setup", plugin.TaskSetup())
    plugin._failed_inputs.clear()
    plugin.before_user_turn(
        user_id="u1", surface="tui", is_new_session=True, text="보존할 원문"
    )

    retried = plugin.before_user_turn(
        user_id="u1", surface="tui", is_new_session=False, text="retry"
    )
    continued = plugin.before_user_turn(
        user_id="u1", surface="tui", is_new_session=False, text="continue_chat"
    )

    assert retried["action"] == "handled"
    assert continued == {"action": "replace", "text": "보존할 원문"}


def test_failed_input_retry_rejects_a_malformed_saved_structured_envelope(
    monkeypatch,
) -> None:
    class MustNotHandle:
        def handle(self, *args, **kwargs):
            raise AssertionError("malformed replay must not use raw text authority")

    now = datetime(2026, 7, 18, tzinfo=UTC)
    key = ("tui", "s1", "u1")
    monkeypatch.setattr(plugin, "_task_setup", MustNotHandle())
    plugin._failed_inputs[key] = plugin._FailedInput(
        text="task",
        event={
            "session_id": "s1",
            "user_id": "u1",
            "surface": "tui",
            "is_new_session": False,
            "text": "task",
            "choice_prompt_id": "not-a-uuid",
            "selected_choice_ids": ["task"],
        },
        expires_at=now + timedelta(minutes=1),
    )

    rejected = plugin.before_user_turn(
        session_id="s1",
        user_id="u1",
        surface="tui",
        is_new_session=False,
        text="retry",
        now=now,
    )

    assert rejected["action"] == "handled"
    assert "choice_prompt_id" in str(rejected["text"])


def test_failed_structured_merge_retry_preserves_selected_id_over_accompanying_text(
    monkeypatch,
) -> None:
    class FailsOnceStructuredMerge(plugin.TaskSetup):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        def handle_submission(self, *args, **kwargs):
            submission = args[2]
            if submission.selected_choice_ids == ("safe_auto",) and not self.failed:
                self.failed = True
                raise RuntimeError("temporary structured merge failure")
            return super().handle_submission(*args, **kwargs)

    monkeypatch.setattr(plugin, "_task_setup", FailsOnceStructuredMerge())
    monkeypatch.setenv("INFINITY_FORGE_REPOSITORY", REPOSITORY)
    common = {"session_id": "s1", "user_id": "u1", "surface": "tui"}
    plugin.before_user_turn(text="원래 요청", is_new_session=True, **common)
    plugin.before_user_turn(text="task", is_new_session=False, **common)
    merge = plugin.before_user_turn(text="build_review", is_new_session=False, **common)

    failed = plugin.before_user_turn(
        text="full_auto",
        is_new_session=False,
        choice_prompt_id=merge["choice_prompt_id"],
        selected_choice_ids=["safe_auto"],
        **common,
    )
    replayed = plugin.before_user_turn(text="retry", is_new_session=False, **common)

    assert failed["action"] == "handled"
    assert "temporary structured merge failure" in str(failed["text"])
    assert "Merge choice: Safe Files Auto-Merge" in str(replayed["text"])


def test_failed_structured_confirm_retry_preserves_selected_id_over_accompanying_text(
    monkeypatch,
) -> None:
    class FailsOnceStructuredConfirm(plugin.TaskSetup):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        def handle_submission(self, *args, **kwargs):
            submission = args[2]
            if submission.selected_choice_ids == ("confirm",) and not self.failed:
                self.failed = True
                raise RuntimeError("temporary structured confirm failure")
            return super().handle_submission(*args, **kwargs)

    calls: list[object] = []
    plugin.set_task_service(lambda request: calls.append(request) or "created")
    monkeypatch.setattr(plugin, "_task_setup", FailsOnceStructuredConfirm())
    preview = _complete_task_until_confirmation(monkeypatch)

    failed = plugin.before_user_turn(
        session_id="task-session",
        user_id="u1",
        surface="tui",
        is_new_session=False,
        text="cancel",
        choice_prompt_id=preview["choice_prompt_id"],
        selected_choice_ids=["confirm"],
    )
    replayed = plugin.before_user_turn(
        session_id="task-session",
        user_id="u1",
        surface="tui",
        is_new_session=False,
        text="retry",
    )

    assert failed["action"] == "handled"
    assert "temporary structured confirm failure" in str(failed["text"])
    assert replayed == {"action": "handled", "text": "created"}
    assert len(calls) == 1


def test_plugin_lock_keeps_legacy_task_setup_admission_serialized(monkeypatch) -> None:
    tracker = Lock()
    start = Barrier(3)
    active = 0
    max_active = 0

    class SlowSetup:
        def handle(self, *args, **kwargs):
            nonlocal active, max_active
            with tracker:
                active += 1
                max_active = max(max_active, active)
            try:
                sleep(0.05)
                return plugin.TurnResult.continue_original()
            finally:
                with tracker:
                    active -= 1

    monkeypatch.setattr(plugin, "_task_setup", SlowSetup())
    plugin._failed_inputs.clear()

    def invoke(index: int) -> dict[str, object]:
        start.wait()
        return plugin.before_user_turn(
            session_id=f"s{index}",
            user_id="u1",
            surface="tui",
            is_new_session=False,
            text="질문",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(invoke, index) for index in range(2)]
        start.wait()
        assert [future.result(timeout=2) for future in futures] == [
            {"action": "continue"},
            {"action": "continue"},
        ]

    assert max_active == 1


def test_v2_confirmation_is_gated_without_any_v1_write(
    monkeypatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path / "project", "owner/project")
    context = _v2_context(str(tmp_path.resolve()), (project,))
    monkeypatch.setattr(plugin, "_task_context_factory", lambda _cwd: context)
    calls: list[object] = []
    plugin.set_task_service(lambda request: calls.append(request) or "created")

    class MustNotConstruct:
        def __init__(self, *args, **kwargs):
            raise AssertionError("v1 writer must stay disconnected")

    monkeypatch.setattr(plugin, "TaskSettingsStore", MustNotConstruct)
    monkeypatch.setattr(plugin, "TaskOutbox", MustNotConstruct)
    monkeypatch.setattr(plugin, "GitHubTaskIssueClient", MustNotConstruct)
    common = {
        "session_id": "v2",
        "user_id": "alice",
        "surface": "tui",
        "working_directory": str(tmp_path.resolve()),
    }
    mode = plugin.before_user_turn(text="고칠 내용", is_new_session=True, **common)
    projects = plugin.before_user_turn(
        text="ignored",
        is_new_session=False,
        choice_prompt_id=mode["choice_prompt_id"],
        selected_choice_ids=["task"],
        **common,
    )
    flow = plugin.before_user_turn(
        text="ignored",
        is_new_session=False,
        choice_prompt_id=projects["choice_prompt_id"],
        selected_choice_ids=[project.project_id],
        **common,
    )
    merge = plugin.before_user_turn(
        text="ignored",
        is_new_session=False,
        choice_prompt_id=flow["choice_prompt_id"],
        selected_choice_ids=["build"],
        **common,
    )
    preview = plugin.before_user_turn(
        text="ignored",
        is_new_session=False,
        choice_prompt_id=merge["choice_prompt_id"],
        selected_choice_ids=["manual"],
        **common,
    )
    gated = plugin.before_user_turn(
        text="ignored",
        is_new_session=False,
        choice_prompt_id=preview["choice_prompt_id"],
        selected_choice_ids=["confirm"],
        **common,
    )

    assert gated["action"] == "handled"
    assert "not enabled" in str(gated["text"]).lower()
    assert gated["choice_prompt_id"] == preview["choice_prompt_id"]
    assert calls == []
    assert plugin._pending_tasks == {}


def test_task_entry_carries_working_directory_to_project_discovery(
    monkeypatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path / "project", "owner/project")
    seen: list[str | None] = []
    working_directory = str(tmp_path.resolve())
    context = _v2_context(
        working_directory,
        (project,),
        discover=lambda cwd: seen.append(cwd) or (project,),
    )
    monkeypatch.setattr(plugin, "_task_context_factory", lambda cwd: context)
    plugin.before_user_turn(
        session_id="s1",
        user_id="u1",
        text="요청",
        working_directory=working_directory,
    )
    projects = plugin.before_user_turn(
        session_id="s1",
        user_id="u1",
        text="task",
        working_directory=working_directory,
    )

    assert projects["choice_mode"] == "multiple"
    assert seen == [working_directory]


def test_blocked_plugin_discovery_does_not_block_another_session(
    monkeypatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path / "project", "owner/project")
    started = Barrier(2)
    release = Barrier(2)

    def discover(_cwd: str | None) -> tuple[TaskProject, ...]:
        started.wait()
        release.wait()
        return (project,)

    context = _v2_context(str(tmp_path.resolve()), (project,), discover=discover)
    monkeypatch.setattr(plugin, "_task_context_factory", lambda _cwd: context)
    common = {
        "user_id": "u1",
        "working_directory": str(tmp_path.resolve()),
    }
    plugin.before_user_turn(session_id="blocked", text="요청", **common)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            plugin.before_user_turn,
            session_id="blocked",
            text="task",
            **common,
        )
        started.wait()
        unrelated = plugin.before_user_turn(
            session_id="other",
            text="다른 요청",
            **common,
        )
        release.wait()
        pending.result(timeout=1)

    assert unrelated["action"] == "handled"
    assert [choice["id"] for choice in unrelated["choices"]] == ["chat", "task"]


def test_default_context_uses_separate_v2_config_and_one_batch_deadline(
    monkeypatch,
    tmp_path: Path,
) -> None:
    first = _project(tmp_path / "first", "owner/first", "a" * 40)
    second = _project(tmp_path / "second", "owner/second", "b" * 40)
    monkeypatch.setenv("INFINITY_FORGE_REPOSITORY", "legacy/only")
    monkeypatch.setenv("INFINITY_FORGE_MANAGEMENT_REPOSITORY", MANAGEMENT_REPOSITORY)
    monkeypatch.setenv("INFINITY_FORGE_WORKSPACE_ROOTS", str(tmp_path.resolve()))
    monkeypatch.setenv("INFINITY_FORGE_HOST_ID", OWNER_HOST)
    monkeypatch.setenv("INFINITY_FORGE_GH_PATH", "gh")
    time_values = iter((0.0, 1.0, 2.0))
    timeouts: list[float] = []

    def validate(project, **kwargs):
        timeouts.append(kwargs["timeout_seconds"])
        return project

    monkeypatch.setattr(plugin, "validate_task_project", validate, raising=False)
    context = plugin._default_task_context(
        None,
        monotonic=lambda: next(time_values),
    )
    validated = context.validate_projects((first, second))

    assert context.management_repository == MANAGEMENT_REPOSITORY
    assert context.working_directory is None
    assert validated == (first, second)
    assert timeouts == [4.0, 3.0]


def test_default_context_rejects_non_uuid_host(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("INFINITY_FORGE_MANAGEMENT_REPOSITORY", MANAGEMENT_REPOSITORY)
    monkeypatch.setenv("INFINITY_FORGE_WORKSPACE_ROOTS", str(tmp_path.resolve()))
    monkeypatch.setenv("INFINITY_FORGE_HOST_ID", "hostname-derived")
    monkeypatch.setenv("INFINITY_FORGE_GH_PATH", "gh")

    with pytest.raises(RuntimeError, match="INFINITY_FORGE_HOST_ID"):
        plugin._default_task_context(None)


def test_github_metadata_adapter_uses_one_deadline_and_encoded_branch(
    monkeypatch,
) -> None:
    monkeypatch.setenv("GH_HOST", "attacker.example")
    commands: list[list[str]] = []
    timeouts: list[float] = []
    time_values = iter((10.0, 10.5, 11.0, 11.5, 12.0))
    outputs = iter(
        (
            json.dumps({"full_name": "owner/repo", "default_branch": "main"}),
            json.dumps({"name": "feature/x", "commit": {"sha": "a" * 40}}),
        )
    )

    def runner(command, **kwargs):
        commands.append(command)
        timeouts.append(kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0, next(outputs), "")

    adapter = plugin._GitHubMetadataAdapter(
        "gh",
        runner=runner,
        monotonic=lambda: next(time_values),
    )
    metadata = adapter("owner/repo", "feature/x", 5.0)

    assert metadata.full_name == "owner/repo"
    assert metadata.branch == "feature/x"
    assert commands == [
        [
            "gh",
            "api",
            "--hostname",
            "github.com",
            "--method",
            "GET",
            "repos/owner/repo",
        ],
        [
            "gh",
            "api",
            "--hostname",
            "github.com",
            "--method",
            "GET",
            "repos/owner/repo/branches/feature%2Fx",
        ],
    ]
    assert timeouts == [4.5, 3.5]


def test_github_metadata_deadline_stops_before_second_process() -> None:
    commands: list[list[str]] = []
    time_values = iter((0.0, 0.0, 1.0, 5.0))

    def runner(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"full_name": "owner/repo", "default_branch": "main"}),
            "",
        )

    adapter = plugin._GitHubMetadataAdapter(
        "gh",
        runner=runner,
        monotonic=lambda: next(time_values),
    )

    with pytest.raises(RuntimeError, match="timed out"):
        adapter("owner/repo", None, 5.0)

    assert len(commands) == 1


def test_github_metadata_nonzero_exit_does_not_expose_output() -> None:
    secret = "credential-bearing diagnostic"
    time_values = iter((0.0, 0.0, 0.1))

    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, secret, secret)

    adapter = plugin._GitHubMetadataAdapter(
        "gh",
        runner=runner,
        monotonic=lambda: next(time_values),
    )

    with pytest.raises(RuntimeError) as captured:
        adapter("owner/repo", None, 5.0)

    assert secret not in str(captured.value)


def test_chat_choice_never_loads_v2_configuration(monkeypatch) -> None:
    calls = 0

    def forbidden(_working_directory):
        nonlocal calls
        calls += 1
        raise AssertionError("Chat must not read Task configuration")

    monkeypatch.setattr(plugin, "_task_context_factory", forbidden)
    shown = plugin.before_user_turn(
        session_id="chat",
        user_id="u1",
        text="일반 질문",
    )
    replay = plugin.before_user_turn(
        session_id="chat",
        user_id="u1",
        text="ignored",
        choice_prompt_id=shown["choice_prompt_id"],
        selected_choice_ids=["chat"],
    )

    assert replay == {"action": "replace", "text": "일반 질문"}
    assert calls == 0


def test_invalid_task_submission_is_rejected_before_loading_config(
    monkeypatch,
) -> None:
    calls = 0

    def forbidden(_working_directory):
        nonlocal calls
        calls += 1
        raise AssertionError("invalid choice must not read Task configuration")

    monkeypatch.setattr(plugin, "_task_context_factory", forbidden)
    shown = plugin.before_user_turn(
        session_id="invalid",
        user_id="u1",
        text="요청",
    )
    rejected = plugin.before_user_turn(
        session_id="invalid",
        user_id="u1",
        text="ignored",
        choice_prompt_id=shown["choice_prompt_id"],
        selected_choice_ids=["task", "task"],
    )

    assert rejected["choice_prompt_id"] == shown["choice_prompt_id"]
    assert calls == 0


def test_failed_task_entry_retry_keeps_first_trusted_working_directory(
    monkeypatch,
    tmp_path: Path,
) -> None:
    original = tmp_path / "original"
    changed = tmp_path / "changed"
    original.mkdir()
    changed.mkdir()
    project = _project(original / "project", "owner/project")
    seen: list[str | None] = []

    def factory(working_directory: str | None):
        seen.append(working_directory)
        if len(seen) == 1:
            raise RuntimeError("temporary config failure")
        return _v2_context(working_directory, (project,))

    monkeypatch.setattr(plugin, "_task_context_factory", factory)
    plugin.before_user_turn(
        session_id="s1",
        user_id="u1",
        text="요청",
        working_directory=str(original.resolve()),
    )
    failed = plugin.before_user_turn(
        session_id="s1",
        user_id="u1",
        text="task",
        working_directory=str(original.resolve()),
    )
    retried = plugin.before_user_turn(
        session_id="s1",
        user_id="u1",
        text="retry",
        working_directory=str(changed.resolve()),
    )

    assert "temporary config failure" in str(failed["text"])
    assert retried["choice_mode"] == "multiple"
    assert seen == [str(original.resolve()), str(original.resolve())]


def test_user_event_working_directory_is_not_trusted(
    monkeypatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path / "project", "owner/project")
    seen: list[str | None] = []

    def factory(working_directory: str | None):
        seen.append(working_directory)
        return _v2_context(working_directory, (project,))

    monkeypatch.setattr(plugin, "_task_context_factory", factory)
    plugin.before_user_turn(
        {
            "session_id": "s1",
            "user_id": "u1",
            "text": "요청",
            "working_directory": str(tmp_path.resolve()),
        }
    )
    plugin.before_user_turn(
        {
            "session_id": "s1",
            "user_id": "u1",
            "text": "task",
            "working_directory": str(tmp_path.resolve()),
        }
    )

    assert seen == [None]


def test_relative_working_directory_is_rejected_without_resolving(
    monkeypatch,
) -> None:
    def forbidden_resolve(*args, **kwargs):
        raise AssertionError("relative cwd must not be resolved")

    monkeypatch.setattr(plugin.Path, "resolve", forbidden_resolve)

    assert plugin._trusted_working_directory(
        {"working_directory": "relative/project"}
    ) is None


def test_confirm_uses_fresh_context_validator_not_entry_validator(
    monkeypatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path / "project", "owner/project")
    working_directory = str(tmp_path.resolve())
    entry_validations = 0
    confirm_validations = 0
    factory_calls = 0

    def entry_validate(selected: tuple[TaskProject, ...]):
        nonlocal entry_validations
        entry_validations += 1
        return selected

    def confirm_validate(selected: tuple[TaskProject, ...]):
        nonlocal confirm_validations
        confirm_validations += 1
        raise RuntimeError("current roots no longer authorize this Project")

    def factory(_working_directory: str | None):
        nonlocal factory_calls
        factory_calls += 1
        validator = entry_validate if factory_calls == 1 else confirm_validate
        return _v2_context(
            working_directory,
            (project,),
            validate=validator,
        )

    monkeypatch.setattr(plugin, "_task_context_factory", factory)
    common = {
        "session_id": "fresh-context",
        "user_id": "u1",
        "working_directory": working_directory,
    }
    mode = plugin.before_user_turn(text="내용", **common)
    projects = plugin.before_user_turn(
        text="ignored",
        choice_prompt_id=mode["choice_prompt_id"],
        selected_choice_ids=["task"],
        **common,
    )
    flow = plugin.before_user_turn(
        text="ignored",
        choice_prompt_id=projects["choice_prompt_id"],
        selected_choice_ids=[project.project_id],
        **common,
    )
    merge = plugin.before_user_turn(
        text="ignored",
        choice_prompt_id=flow["choice_prompt_id"],
        selected_choice_ids=["build"],
        **common,
    )
    preview = plugin.before_user_turn(
        text="ignored",
        choice_prompt_id=merge["choice_prompt_id"],
        selected_choice_ids=["manual"],
        **common,
    )
    failed = plugin.before_user_turn(
        text="ignored",
        choice_prompt_id=preview["choice_prompt_id"],
        selected_choice_ids=["confirm"],
        **common,
    )

    assert [choice["id"] for choice in failed["choices"]] == ["retry", "cancel"]
    assert entry_validations == 0
    assert confirm_validations == 1
