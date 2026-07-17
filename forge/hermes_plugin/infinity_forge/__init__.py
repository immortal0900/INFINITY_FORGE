"""Infinity Forge user-turn chooser plugin for Hermes."""

from __future__ import annotations

# ruff: noqa: E402  # Managed release activation must precede forge imports.

import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any


_RELEASE_SHA = re.compile(r"[0-9a-f]{40}")


def _managed_release_root(plugin_file: Path) -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data).resolve() / "InfinityForge" / "releases"
    return plugin_file.resolve().parent.parent.parent / "infinity-forge" / "releases"


def _activate_managed_release(
    plugin_file: Path = Path(__file__),
) -> Path | None:
    pointer = plugin_file.resolve().parent / "release-path.txt"
    if not pointer.exists():
        return None
    try:
        raw = pointer.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(
            "Infinity Forge managed release pointer cannot be read"
        ) from exc

    candidate = Path(raw)
    if not raw or not candidate.is_absolute():
        raise RuntimeError("invalid Infinity Forge managed release pointer")

    # RISK(security): 이 경로가 사용자 관리 release root를 벗어나면
    # plugin import를 통해 임의 Python 코드를 실행할 수 있다.
    release_root = _managed_release_root(plugin_file)
    resolved = candidate.resolve()
    if resolved.parent != release_root:
        raise RuntimeError(
            "Infinity Forge managed release is outside managed release root"
        )
    if _RELEASE_SHA.fullmatch(resolved.name) is None:
        raise RuntimeError(
            "Infinity Forge managed release must use a "
            "40-character lowercase Git SHA"
        )
    for required in (
        resolved / "forge" / "__init__.py",
        resolved / "forge" / "ops" / "task_setup.py",
    ):
        if not required.is_file():
            raise RuntimeError("Infinity Forge managed release is incomplete")

    release_path = str(resolved)
    sys.path[:] = [entry for entry in sys.path if entry != release_path]
    sys.path.insert(0, release_path)
    return resolved


_MANAGED_RELEASE = _activate_managed_release()

from forge.ops.github import GitHubTaskIssueClient
from forge.ops.task_outbox import TaskOutbox, task_outbox_path
from forge.ops.task_service import (
    TaskCreationRequest,
    TaskService as LocalTaskService,
)
from forge.ops.task_settings import TaskSettingsStore
from forge.ops.task_setup import (
    DEFAULT_SURFACE,
    SETUP_TIMEOUT,
    TaskSetup,
    TurnResult,
)


_CHOICE_LABELS = {
    "chat": "Chat",
    "task": "Task",
    "build": "Build",
    "build_review": "Build + Review",
    "build_review_deep_check": "Build + Review + Deep Check",
    "manual": "Manual Merge",
    "safe_auto": "Safe Files Auto-Merge",
    "full_auto": "All Validated PRs Auto-Merge",
    "confirm": "Confirm Task",
    "cancel": "Cancel",
    "retry": "Retry",
    "continue_chat": "Continue in Chat",
}
_MAX_FAILED_INPUTS = 256
_MAX_PENDING_TASKS = 256
_MISSING_SESSION = "<missing-session>"
_MISSING_USER = "<missing-user>"
_REPOSITORY_ENV = "INFINITY_FORGE_REPOSITORY"
_TASK_SETTINGS_DB_ENV = "INFINITY_FORGE_TASK_SETTINGS_DB"
_GH_PATH_ENV = "INFINITY_FORGE_GH_PATH"

TaskServiceCallback = Callable[[TaskCreationRequest], str]
StateKey = tuple[str, str, str]


@dataclass(frozen=True)
class _FailedInput:
    text: str
    event: dict[str, object]
    expires_at: datetime


@dataclass(frozen=True)
class _PendingTask:
    request: TaskCreationRequest
    in_flight: bool = False


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{name} is required")
    return value


def _default_task_service(request: TaskCreationRequest) -> str:
    repository = _required_environment(_REPOSITORY_ENV)
    database_path = _required_environment(_TASK_SETTINGS_DB_ENV)
    gh_path = _required_environment(_GH_PATH_ENV)
    if repository != request.repository:
        raise RuntimeError(
            f"{_REPOSITORY_ENV} changed after Task confirmation"
        )

    store = TaskSettingsStore(database_path)
    outbox = TaskOutbox(task_outbox_path(store.database_path))
    pending = outbox.load_pending_for_user(
        request.repository,
        request.confirmed_by,
    )
    durable_request = pending if pending is not None else request
    github = GitHubTaskIssueClient(gh_path)
    created = LocalTaskService(store, github).create_task_durable(
        durable_request,
        outbox,
    )
    issue_number = created.issue.number
    return (
        f"Task #{issue_number} created: "
        f"https://github.com/{durable_request.repository}/issues/{issue_number}"
    )


_task_setup = TaskSetup()
_task_service: TaskServiceCallback = _default_task_service
_failed_inputs: dict[StateKey, _FailedInput] = {}
_pending_tasks: dict[StateKey, _PendingTask] = {}
_state_lock = RLock()


def set_task_service(callback: TaskServiceCallback | None) -> None:
    """Install the confirmed-Task callback, or restore the local service."""

    global _task_service
    with _state_lock:
        _task_service = callback if callback is not None else _default_task_service


def _hook_result(result: TurnResult) -> dict[str, object]:
    payload: dict[str, object] = {"action": result.action}
    if result.text is not None:
        payload["text"] = result.text
    if result.choices:
        payload["choices"] = [
            {"id": choice, "label": _CHOICE_LABELS[choice]}
            for choice in result.choices
        ]
    return payload


def _error_result(error: Exception) -> dict[str, object]:
    return {
        "action": "handled",
        "text": f"Infinity Forge could not open the chooser: {error}",
        "choices": [
            {"id": "retry", "label": _CHOICE_LABELS["retry"]},
            {
                "id": "continue_chat",
                "label": _CHOICE_LABELS["continue_chat"],
            },
        ],
    }


def _task_error_result(error: Exception) -> dict[str, object]:
    return _hook_result(
        TurnResult.handled(
            f"Task was not created: {error}",
            choices=("retry", "cancel"),
        )
    )


def _combined_event(
    event: Mapping[str, object] | None,
    values: Mapping[str, object],
) -> dict[str, object]:
    combined: dict[str, object] = dict(event or {})
    combined.update(values)
    return combined


def _nonempty_string(value: object, fallback: str) -> str:
    return value if isinstance(value, str) and value else fallback


def _fallback_key(combined: Mapping[str, object]) -> StateKey:
    return (
        _nonempty_string(combined.get("surface"), DEFAULT_SURFACE),
        _nonempty_string(combined.get("session_id"), _MISSING_SESSION),
        _nonempty_string(combined.get("user_id"), _MISSING_USER),
    )


def _cleanup_time(combined: Mapping[str, object]) -> datetime:
    now = combined.get("now")
    if isinstance(now, datetime) and now.tzinfo is not None:
        return now
    return datetime.now(timezone.utc)


def _read_event(
    combined: Mapping[str, object],
) -> tuple[str, str, str, str, bool, datetime | None]:
    session_id = combined.get("session_id")
    user_id = combined.get("user_id")
    surface = combined.get("surface", DEFAULT_SURFACE)
    text = combined.get("text")
    is_new_session = combined.get("is_new_session", False)
    now = combined.get("now")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id is required")
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("user_id is required")
    if not isinstance(surface, str) or not surface:
        raise ValueError("surface is required")
    if not isinstance(text, str):
        raise ValueError("text is required")
    if not isinstance(is_new_session, bool):
        raise ValueError("is_new_session must be a bool")
    if now is not None and not isinstance(now, datetime):
        raise ValueError("now must be a datetime")
    return session_id, user_id, surface, text, is_new_session, now


def _sweep_failed_inputs(now: datetime) -> None:
    expired = [
        key for key, failed in _failed_inputs.items() if now >= failed.expires_at
    ]
    for key in expired:
        _failed_inputs.pop(key, None)
    while len(_failed_inputs) > _MAX_FAILED_INPUTS:
        _failed_inputs.pop(next(iter(_failed_inputs)))


def _store_failed_input(
    key: StateKey,
    text: str,
    combined: Mapping[str, object],
    now: datetime,
) -> None:
    _failed_inputs[key] = _FailedInput(
        text=text,
        event=dict(combined),
        expires_at=now + SETUP_TIMEOUT,
    )
    _sweep_failed_inputs(now)


def _call_task_service(request: TaskCreationRequest) -> str:
    message = _task_service(request)
    if not isinstance(message, str) or not message.strip():
        raise RuntimeError("Task service returned no confirmation")
    return message


def _handle_pending_task(
    key: StateKey,
    choice: str,
) -> tuple[dict[str, object] | None, TaskCreationRequest | None]:
    pending = _pending_tasks.get(key)
    if pending is None:
        return None, None
    if pending.in_flight:
        return (
            _hook_result(
                TurnResult.handled("Task creation is already in progress.")
            ),
            None,
        )
    if choice in {"cancel", "/cancel"}:
        _pending_tasks.pop(key, None)
        return _hook_result(TurnResult.handled("Pending Task cancelled.")), None
    if choice != "retry":
        return (
            _task_error_result(
                ValueError("Choose Retry or Cancel for this pending Task.")
            ),
            None,
        )
    _pending_tasks[key] = _PendingTask(pending.request, in_flight=True)
    return None, pending.request


def _deliver_confirmed_task(
    result: TurnResult,
    key: StateKey,
) -> tuple[dict[str, object] | None, TaskCreationRequest | None]:
    if result.selection is None and result.task_text is None:
        return _hook_result(result), None
    if (
        result.selection is None
        or result.task_text is None
        or result.task_request is None
    ):
        return (
            _hook_result(
                TurnResult.handled(
                    "Task was not created: confirmed Task data is incomplete."
                )
            ),
            None,
        )
    if len(_pending_tasks) >= _MAX_PENDING_TASKS and key not in _pending_tasks:
        raise RuntimeError("Pending Task capacity was exceeded")
    _pending_tasks[key] = _PendingTask(result.task_request, in_flight=True)
    return None, result.task_request


def _finish_task_service_call(
    key: StateKey,
    request: TaskCreationRequest,
) -> dict[str, object]:
    """Call the slow Task service without blocking unrelated sessions."""

    try:
        message = _call_task_service(request)
    except Exception as error:
        with _state_lock:
            pending = _pending_tasks.get(key)
            if pending is not None and pending.request is request:
                _pending_tasks[key] = _PendingTask(request, in_flight=False)
        return _task_error_result(error)

    with _state_lock:
        pending = _pending_tasks.get(key)
        if pending is not None and pending.request is request:
            _pending_tasks.pop(key, None)
    return _hook_result(TurnResult.handled(message))


def before_user_turn(
    event: Mapping[str, object] | None = None,
    **values: object,
) -> dict[str, object]:
    """Run the Forge chooser and never silently enter Task after an error."""

    combined = _combined_event(event, values)
    cleanup_time = _cleanup_time(combined)
    key = _fallback_key(combined)
    raw_text = combined.get("text")
    choice = raw_text.strip().lower() if isinstance(raw_text, str) else ""
    preserved_text = raw_text if isinstance(raw_text, str) else ""
    service_request: TaskCreationRequest | None = None

    with _state_lock:
        pending_result, service_request = _handle_pending_task(key, choice)
        if pending_result is not None:
            return pending_result
        if service_request is None:
            _sweep_failed_inputs(cleanup_time)
            if combined.get("is_new_session") is True:
                _failed_inputs.pop(key, None)
            failed = _failed_inputs.get(key)
            if failed is not None:
                if choice == "continue_chat":
                    _failed_inputs.pop(key, None)
                    surface, session_id, user_id = key
                    try:
                        _task_setup.enter_chat(
                            session_id,
                            user_id,
                            surface=surface,
                            now=cleanup_time,
                        )
                    except Exception as error:
                        _failed_inputs[key] = failed
                        return _error_result(error)
                    return _hook_result(TurnResult.replace(failed.text))
                if choice == "retry":
                    _failed_inputs.pop(key, None)
                    preserved_text = failed.text
                    combined = dict(failed.event)
                    combined.pop("now", None)
                    key = _fallback_key(combined)
                else:
                    return _error_result(
                        ValueError(
                            "Choose Retry or Continue in Chat after this error."
                        )
                    )

            try:
                session_id, user_id, surface, text, is_new_session, now = _read_event(
                    combined
                )
            except Exception as error:
                _store_failed_input(key, preserved_text, combined, cleanup_time)
                return _error_result(error)

            parsed_choice = text.strip().lower()
            if (
                parsed_choice == "confirm"
                and len(_pending_tasks) >= _MAX_PENDING_TASKS
            ):
                return _hook_result(
                    TurnResult.handled(
                        "Task confirmation is temporarily full. "
                        "Resolve or cancel a pending Task before confirming this one.",
                        choices=("confirm", "cancel"),
                    )
                )

            try:
                result = _task_setup.handle(
                    session_id,
                    user_id,
                    text,
                    now,
                    surface=surface,
                    is_new_session=is_new_session,
                    repository=os.environ.get(_REPOSITORY_ENV),
                )
            except Exception as error:
                _store_failed_input(key, preserved_text, combined, cleanup_time)
                return _error_result(error)
            _failed_inputs.pop(key, None)
            immediate_result, service_request = _deliver_confirmed_task(result, key)
            if immediate_result is not None:
                return immediate_result

    if service_request is None:
        raise RuntimeError("Task service request was not prepared")
    return _finish_task_service_call(key, service_request)


def _slash_command_without_context(raw_args: str) -> str:
    suffix = " Arguments are not supported." if raw_args.strip() else ""
    return (
        "Infinity Forge needs Hermes session context for this command. "
        "Use the Chat or Task chooser before a normal user turn."
        f"{suffix}"
    )


def register(ctx: Any) -> None:
    """Register the generic user-turn hook and discoverable slash commands."""

    ctx.register_hook("pre_user_turn", before_user_turn)
    ctx.register_command(
        "task",
        handler=_slash_command_without_context,
        description="Start a new Infinity Forge Task",
    )
    ctx.register_command(
        "cancel",
        handler=_slash_command_without_context,
        description="Cancel the current Infinity Forge Task setup",
    )


__all__ = [
    "TaskSetup",
    "before_user_turn",
    "register",
    "set_task_service",
]
