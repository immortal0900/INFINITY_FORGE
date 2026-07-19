"""Infinity Forge user-turn chooser plugin for Hermes."""

from __future__ import annotations

# ruff: noqa: E402  # Managed release activation must precede forge imports.

import os
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from threading import RLock
from typing import Any, Protocol
from urllib.parse import quote
from uuid import UUID, uuid4


_RELEASE_SHA = re.compile(r"[0-9a-f]{40}")
_PLAIN_CHOICE = re.compile(
    r"^choose (?P<prompt_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}) (?P<choice_ids>\S(?:.*\S)?)$"
)


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
from forge.ops.choice_prompt import (
    Choice,
    ChoiceMode,
    ChoicePrompt,
    ChoicePromptError,
    ChoiceSubmission,
)
from forge.ops.project_discovery import (
    DEFAULT_TIMEOUT_SECONDS,
    GitHubRepositoryMetadata,
    ProjectPathProbe,
    bind_task_project,
    discover_projects,
    probe_project_path,
    validate_task_project,
)
from forge.ops.task_projects import TaskProject, normalize_github_remote
from forge.ops.stop_command import StopCommand, parse_stop_command
from forge.ops.surface_events import (
    SurfaceEventStore,
    TrustedTurnContext,
)
from forge.ops.task_database import TaskDatabase
from forge.ops.task_setup import (
    DEFAULT_SURFACE,
    SETUP_TIMEOUT,
    TaskSetup,
    TaskSetupContext,
    TurnResult,
)
from forge.ops.task_stop import StopReceipt, StoppableTask, TaskStopService


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
_MANAGEMENT_REPOSITORY_ENV = "INFINITY_FORGE_MANAGEMENT_REPOSITORY"
_WORKSPACE_ROOTS_ENV = "INFINITY_FORGE_WORKSPACE_ROOTS"
_HOST_ID_ENV = "INFINITY_FORGE_HOST_ID"
_TASK_SETTINGS_DB_ENV = "INFINITY_FORGE_TASK_SETTINGS_DB"
_GH_PATH_ENV = "INFINITY_FORGE_GH_PATH"
_MAX_GITHUB_RESPONSE_BYTES = 1_000_000

TaskServiceCallback = Callable[[TaskCreationRequest], str]
StateKey = tuple[str, str, str]


class TaskStopBackend(Protocol):
    def get_stoppable(
        self,
        context: TrustedTurnContext,
        issue_number: int | None = None,
    ) -> tuple[StoppableTask, ...]: ...

    def request_stop(
        self,
        request_id: str,
        context: TrustedTurnContext,
        payload: str,
        *,
        at: datetime | None = None,
    ) -> StopReceipt: ...


@dataclass(frozen=True)
class _FailedInput:
    text: str
    event: dict[str, object]
    expires_at: datetime
    working_directory: str | None = None


@dataclass(frozen=True)
class _PendingTask:
    request: TaskCreationRequest
    in_flight: bool = False


@dataclass(frozen=True)
class _PendingStop:
    prompt: ChoicePrompt
    request_ids: frozenset[str]


class _LocalStopBackend:
    """Bind the trusted source receipt immediately before the Stop transaction."""

    def __init__(self, database_path: str) -> None:
        database = TaskDatabase(database_path)
        self._events = SurfaceEventStore(database)
        self._stops = TaskStopService(database)

    def get_stoppable(
        self,
        context: TrustedTurnContext,
        issue_number: int | None = None,
    ) -> tuple[StoppableTask, ...]:
        return self._stops.get_stoppable(context, issue_number)

    def request_stop(
        self,
        request_id: str,
        context: TrustedTurnContext,
        payload: str,
        *,
        at: datetime | None = None,
    ) -> StopReceipt:
        # Reauthorize before the receipt write so revoked access cannot create
        # even a local Stop-side effect or disclose the owner host.
        self._stops.resolve_stoppable(request_id, context)
        event = self._events.receive(context, payload, at=at)
        return self._stops.request_stop(
            request_id,
            context,
            payload_hash=event.payload_hash,
            at=at,
        )


class _GitHubMetadataAdapter:
    """Read exact repository and branch metadata through the configured gh CLI."""

    def __init__(
        self,
        gh_path: str,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(gh_path, str) or not gh_path.strip():
            raise RuntimeError(f"{_GH_PATH_ENV} is required")
        self._gh_path = gh_path
        self._runner = runner
        self._monotonic = monotonic

    def _time(self) -> float:
        try:
            value = self._monotonic()
        except Exception:
            raise RuntimeError("GitHub metadata deadline failed") from None
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
        ):
            raise RuntimeError("GitHub metadata deadline failed")
        return float(value)

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._time()
        if remaining <= 0:
            raise RuntimeError("GitHub metadata request timed out")
        return remaining

    def _api(self, endpoint: str, deadline: float) -> Mapping[str, object]:
        timeout = self._remaining(deadline)
        command = [
            self._gh_path,
            "api",
            "--hostname",
            "github.com",
            "--method",
            "GET",
            endpoint,
        ]
        try:
            result = self._runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=timeout,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("GitHub metadata request timed out") from None
        except (OSError, TypeError, ValueError):
            raise RuntimeError("GitHub metadata request failed") from None
        self._remaining(deadline)
        try:
            response_size = (
                len(result.stdout.encode("utf-8"))
                if isinstance(result, subprocess.CompletedProcess)
                and isinstance(result.stdout, str)
                else None
            )
        except UnicodeEncodeError:
            response_size = None
        if (
            not isinstance(result, subprocess.CompletedProcess)
            or type(result.returncode) is not int
            or result.returncode != 0
            or not isinstance(result.stdout, str)
            or response_size is None
            or response_size > _MAX_GITHUB_RESPONSE_BYTES
        ):
            raise RuntimeError("GitHub metadata request failed")
        try:
            payload = json.loads(result.stdout)
        except (json.JSONDecodeError, RecursionError):
            raise RuntimeError("GitHub metadata response is invalid") from None
        if not isinstance(payload, Mapping):
            raise RuntimeError("GitHub metadata response is invalid")
        return payload

    def __call__(
        self,
        repository: str,
        branch: str | None,
        timeout: float,
    ) -> GitHubRepositoryMetadata:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not isfinite(timeout)
            or timeout <= 0
        ):
            raise RuntimeError("GitHub metadata timeout is invalid")
        deadline = self._time() + float(timeout)
        repository_payload = self._api(f"repos/{repository}", deadline)
        full_name = repository_payload.get("full_name")
        default_branch = repository_payload.get("default_branch")
        selected_branch = default_branch if branch is None else branch
        if (
            not isinstance(full_name, str)
            or not isinstance(default_branch, str)
            or not isinstance(selected_branch, str)
        ):
            raise RuntimeError("GitHub metadata response is invalid")
        branch_payload = self._api(
            f"repos/{repository}/branches/{quote(selected_branch, safe='')}",
            deadline,
        )
        commit = branch_payload.get("commit")
        if (
            branch_payload.get("name") != selected_branch
            or not isinstance(commit, Mapping)
            or not isinstance(commit.get("sha"), str)
        ):
            raise RuntimeError("GitHub metadata response is invalid")
        try:
            return GitHubRepositoryMetadata(
                full_name=full_name,
                default_branch=default_branch,
                branch=selected_branch,
                commit_sha=commit["sha"],
            )
        except Exception:
            raise RuntimeError("GitHub metadata response is invalid") from None


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{name} is required")
    return value


def _canonical_repository_environment(name: str) -> str:
    value = _required_environment(name).strip()
    try:
        canonical = normalize_github_remote(f"https://github.com/{value}")
    except Exception:
        raise RuntimeError(f"{name} must use canonical OWNER/REPO format") from None
    if canonical != value:
        raise RuntimeError(f"{name} must use canonical OWNER/REPO format")
    return value


def _workspace_roots_from_environment() -> tuple[str, ...]:
    raw = _required_environment(_WORKSPACE_ROOTS_ENV)
    values = raw.split(os.pathsep)
    if not values or any(not value.strip() for value in values):
        raise RuntimeError(
            f"{_WORKSPACE_ROOTS_ENV} must contain canonical absolute directories"
        )
    roots: list[str] = []
    for value in values:
        candidate = value.strip()
        try:
            path = Path(candidate)
            if not path.is_absolute():
                raise ValueError
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            raise RuntimeError(
                f"{_WORKSPACE_ROOTS_ENV} must contain canonical absolute directories"
            ) from None
        if not resolved.is_dir() or str(resolved) != candidate:
            raise RuntimeError(
                f"{_WORKSPACE_ROOTS_ENV} must contain canonical absolute directories"
            )
        if candidate not in roots:
            roots.append(candidate)
    return tuple(roots)


def _host_id_from_environment() -> str:
    value = _required_environment(_HOST_ID_ENV).strip()
    try:
        parsed = UUID(value)
    except ValueError:
        raise RuntimeError(f"{_HOST_ID_ENV} must be a canonical UUID") from None
    if str(parsed) != value:
        raise RuntimeError(f"{_HOST_ID_ENV} must be a canonical UUID")
    return value


def _default_task_context(
    working_directory: str | None,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    monotonic: Callable[[], float] = time.monotonic,
) -> TaskSetupContext:
    """Build trusted v2 discovery and one-deadline batch validation context."""

    management_repository = _canonical_repository_environment(
        _MANAGEMENT_REPOSITORY_ENV
    )
    allowed_roots = _workspace_roots_from_environment()
    host_id = _host_id_from_environment()
    gh_path = _required_environment(_GH_PATH_ENV).strip()
    metadata_reader = _GitHubMetadataAdapter(
        gh_path,
        runner=runner,
        monotonic=monotonic,
    )

    def discover(
        selected_working_directory: str | None,
    ) -> tuple[TaskProject, ...]:
        return discover_projects(
            selected_working_directory,
            allowed_roots,
            host_id=host_id,
            runner=runner,
            github_metadata_reader=metadata_reader,
            monotonic=monotonic,
        )

    def validate(
        projects: tuple[TaskProject, ...],
    ) -> tuple[TaskProject, ...]:
        try:
            started = monotonic()
        except Exception:
            raise RuntimeError("Project validation deadline failed") from None
        if (
            isinstance(started, bool)
            or not isinstance(started, (int, float))
            or not isfinite(started)
        ):
            raise RuntimeError("Project validation deadline failed")
        deadline = float(started) + DEFAULT_TIMEOUT_SECONDS
        validated: list[TaskProject] = []
        for project in projects:
            try:
                current = monotonic()
            except Exception:
                raise RuntimeError("Project validation deadline failed") from None
            if (
                isinstance(current, bool)
                or not isinstance(current, (int, float))
                or not isfinite(current)
            ):
                raise RuntimeError("Project validation deadline failed")
            remaining = deadline - float(current)
            if remaining <= 0:
                raise RuntimeError("Project validation timed out")
            if getattr(project, "host_id", None) != host_id:
                raise RuntimeError("Project host binding changed")
            validated.append(
                validate_task_project(
                    project,
                    allowed_roots=allowed_roots,
                    runner=runner,
                    github_metadata_reader=metadata_reader,
                    monotonic=monotonic,
                    timeout_seconds=remaining,
                )
            )
        return tuple(validated)

    def probe_path(path: str) -> ProjectPathProbe:
        return probe_project_path(
            path,
            working_directory=working_directory,
            allowed_roots=allowed_roots,
            runner=runner,
            github_metadata_reader=metadata_reader,
            monotonic=monotonic,
        )

    def bind_project(
        probe: ProjectPathProbe,
        remote_name: str,
        branch: str,
    ) -> TaskProject:
        return bind_task_project(
            probe,
            remote_name=remote_name,
            branch=branch,
            allowed_roots=allowed_roots,
            host_id=host_id,
            runner=runner,
            github_metadata_reader=metadata_reader,
            monotonic=monotonic,
        )

    return TaskSetupContext(
        working_directory=working_directory,
        management_repository=management_repository,
        task_owner_host=host_id,
        discover_projects=discover,
        validate_projects=validate,
        probe_project_path=probe_path,
        bind_project=bind_project,
    )


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
_task_context_factory: Callable[[str | None], TaskSetupContext | None] = (
    _default_task_context
)
_task_service: TaskServiceCallback = _default_task_service
_task_stop_backend: TaskStopBackend | None = None
_failed_inputs: dict[StateKey, _FailedInput] = {}
_pending_tasks: dict[StateKey, _PendingTask] = {}
_pending_stops: dict[StateKey, _PendingStop] = {}
_state_lock = RLock()


def set_task_service(callback: TaskServiceCallback | None) -> None:
    """Install the confirmed-Task callback, or restore the local service."""

    global _task_service
    with _state_lock:
        _task_service = callback if callback is not None else _default_task_service


def set_task_stop_backend(backend: TaskStopBackend | None) -> None:
    """Install a Stop backend, or restore lazy local construction."""

    global _task_stop_backend
    with _state_lock:
        _task_stop_backend = backend


def _stop_backend() -> TaskStopBackend:
    global _task_stop_backend
    with _state_lock:
        if _task_stop_backend is None:
            _task_stop_backend = _LocalStopBackend(
                _required_environment(_TASK_SETTINGS_DB_ENV)
            )
        return _task_stop_backend


def _hook_result(result: TurnResult) -> dict[str, object]:
    payload: dict[str, object] = {"action": result.action}
    if result.text is not None:
        payload["text"] = result.text
    if result.choice_prompt is not None:
        # RISK(breaking): surfaces must receive one validated chooser envelope.
        payload.update(result.choice_prompt.metadata())
        if result.choice_prompt_paused:
            payload["choice_prompt_paused"] = True
    elif result.choices:
        payload["choices"] = [
            {"id": choice, "label": _CHOICE_LABELS[choice]}
            for choice in result.choices
        ]
    return payload


def _read_choice_submission(
    combined: Mapping[str, object],
) -> ChoiceSubmission | None:
    has_prompt_id = "choice_prompt_id" in combined
    has_selected_ids = "selected_choice_ids" in combined
    if not has_prompt_id and not has_selected_ids:
        return None
    if not has_prompt_id or not has_selected_ids:
        raise ChoicePromptError(
            "choice_prompt_id and selected_choice_ids must be provided together"
        )
    prompt_id = combined["choice_prompt_id"]
    selected_ids = combined["selected_choice_ids"]
    if not isinstance(selected_ids, (list, tuple)):
        raise ChoicePromptError("selected_choice_ids must be an array")
    return ChoiceSubmission(prompt_id, tuple(selected_ids))


def _read_plain_choice_submission(text: str) -> ChoiceSubmission | None:
    """Parse the prompt-bound fallback emitted by production surfaces."""

    matched = _PLAIN_CHOICE.fullmatch(text)
    if matched is None:
        return None
    selected_ids = tuple(
        part.strip() for part in matched.group("choice_ids").split(",")
    )
    return ChoiceSubmission(matched.group("prompt_id"), selected_ids)


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
    # The message/envelope is user controlled. Only the carried hook keyword
    # may supply trusted transport metadata.
    for field_name in (
        "owner_host",
        "subject_id",
        "source_event_id",
        "working_directory",
    ):
        combined.pop(field_name, None)
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


def _trusted_working_directory(
    combined: Mapping[str, object],
) -> str | None:
    raw = combined.get("working_directory")
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        return None
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if not resolved.is_dir() or str(resolved) != raw:
        return None
    return raw


def _read_event(
    combined: Mapping[str, object],
) -> tuple[str, str, str, str, bool, datetime | None, str | None]:
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
    working_directory = _trusted_working_directory(combined)
    return (
        session_id,
        user_id,
        surface,
        text,
        is_new_session,
        now,
        working_directory,
    )


def _trusted_stop_context(
    combined: Mapping[str, object],
    *,
    session_id: str,
    user_id: str,
    surface: str,
    working_directory: str | None,
) -> TrustedTurnContext:
    owner_host = combined.get("owner_host")
    subject_id = combined.get("subject_id")
    source_event_id = combined.get("source_event_id")
    if (
        not isinstance(owner_host, str)
        or not owner_host
        or not isinstance(subject_id, str)
        or not subject_id
        or subject_id != user_id
        or not isinstance(source_event_id, str)
        or not source_event_id
    ):
        raise RuntimeError(
            "Trusted owner, subject, and source event context is required for Stop."
        )
    return TrustedTurnContext(
        owner_host=owner_host,
        subject_id=subject_id,
        session_id=session_id,
        surface=surface,
        source_event_id=source_event_id,
        working_directory=working_directory,
    )


def _stop_prompt(tasks: tuple[StoppableTask, ...], now: datetime) -> ChoicePrompt:
    if len(tasks) < 2:
        raise RuntimeError("Stop chooser requires at least two Tasks")
    if len(tasks) > 256:
        raise RuntimeError("Too many Tasks are stoppable; use an exact Task number")
    choices = tuple(
        Choice(
            task.request_id,
            (
                f"#{task.parent_issue_number} — {task.title} "
                f"({task.request_id[:8]})"
                if task.parent_issue_number is not None
                else f"Prepared Task — {task.title} ({task.request_id[:8]})"
            ),
            f"{task.management_repository} — {task.state}",
        )
        for task in tasks
    ) + (Choice("cancel", "Cancel", "Keep every Task running"),)
    return ChoicePrompt(
        choice_prompt_id=str(uuid4()),
        choice_mode=ChoiceMode.SINGLE,
        min_choices=1,
        max_choices=1,
        submit_label="Stop Task",
        expires_at=now + SETUP_TIMEOUT,
        choices=choices,
    )


def _stop_receipt_result(receipt: StopReceipt) -> dict[str, object]:
    target = (
        f"Task #{receipt.parent_issue_number}"
        if receipt.parent_issue_number is not None
        else f"Task {receipt.request_id[:8]}"
    )
    return _hook_result(
        TurnResult.handled(
            f"{target} stop requested. New Task work is now blocked."
        )
    )


def _stop_prompt_result(
    pending: _PendingStop,
    text: str,
) -> dict[str, object]:
    return _hook_result(
        TurnResult.handled(text, choice_prompt=pending.prompt)
    )


def _execute_stop(
    request_id: str,
    context: TrustedTurnContext,
    text: str,
    now: datetime,
) -> dict[str, object]:
    try:
        receipt = _stop_backend().request_stop(
            request_id,
            context,
            text,
            at=now,
        )
    except Exception as error:
        return _hook_result(TurnResult.handled(f"Task was not stopped: {error}"))
    return _stop_receipt_result(receipt)


def _handle_stop_turn(
    *,
    combined: Mapping[str, object],
    key: StateKey,
    text: str,
    submission: ChoiceSubmission | None,
    session_id: str,
    user_id: str,
    surface: str,
    working_directory: str | None,
    now: datetime,
    is_new_session: bool,
) -> dict[str, object] | None:
    with _state_lock:
        if is_new_session:
            _pending_stops.pop(key, None)
        pending = _pending_stops.get(key)

    stop_submission = submission
    if pending is not None and stop_submission is None and text.startswith("choose "):
        try:
            stop_submission = _read_plain_choice_submission(text)
        except ChoicePromptError as error:
            return _stop_prompt_result(
                pending,
                f"That Stop chooser reply is invalid: {error}",
            )

    if pending is not None and stop_submission is not None:
        try:
            selected = pending.prompt.validate_submission(stop_submission, now)
        except ChoicePromptError as error:
            if now >= pending.prompt.expires_at:
                with _state_lock:
                    if _pending_stops.get(key) is pending:
                        _pending_stops.pop(key, None)
                return _hook_result(TurnResult.handled("Stop chooser expired."))
            return _stop_prompt_result(
                pending,
                f"That Stop chooser reply is invalid: {error}",
            )
        with _state_lock:
            if _pending_stops.get(key) is pending:
                _pending_stops.pop(key, None)
        request_id = selected[0]
        if request_id == "cancel":
            return _hook_result(TurnResult.handled("Task Stop cancelled."))
        try:
            context = _trusted_stop_context(
                combined,
                session_id=session_id,
                user_id=user_id,
                surface=surface,
                working_directory=working_directory,
            )
        except Exception as error:
            return _hook_result(TurnResult.handled(f"Task was not stopped: {error}"))
        return _execute_stop(request_id, context, text, now)

    command: StopCommand | None = parse_stop_command(text)
    if command is None:
        if pending is None:
            return None
        return _stop_prompt_result(
            pending,
            "Choose one Task to stop, or choose Cancel.",
        )
    if pending is not None:
        with _state_lock:
            if _pending_stops.get(key) is pending:
                _pending_stops.pop(key, None)
    try:
        context = _trusted_stop_context(
            combined,
            session_id=session_id,
            user_id=user_id,
            surface=surface,
            working_directory=working_directory,
        )
        tasks = _stop_backend().get_stoppable(context, command.issue_number)
    except Exception as error:
        return _hook_result(TurnResult.handled(f"Task was not stopped: {error}"))
    if not tasks:
        return _hook_result(TurnResult.handled("No running Task can be stopped."))
    if len(tasks) == 1:
        return _execute_stop(tasks[0].request_id, context, text, now)
    try:
        prompt = _stop_prompt(tasks, now)
    except Exception as error:
        return _hook_result(TurnResult.handled(f"Task was not stopped: {error}"))
    pending = _PendingStop(
        prompt=prompt,
        request_ids=frozenset(task.request_id for task in tasks),
    )
    with _state_lock:
        if len(_pending_stops) >= _MAX_PENDING_TASKS and key not in _pending_stops:
            return _hook_result(
                TurnResult.handled(
                    "Task Stop chooser is temporarily full. Try again shortly."
                )
            )
        _pending_stops[key] = pending
    return _stop_prompt_result(pending, "Choose the Task to stop.")


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
    working_directory: str | None,
    now: datetime,
) -> None:
    _failed_inputs[key] = _FailedInput(
        text=text,
        event=dict(combined),
        working_directory=working_directory,
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


def _is_confirmation_input(
    submission: ChoiceSubmission | None,
    text: str,
) -> bool:
    """Use structured selection as authority whenever its envelope is present."""

    if submission is not None:
        return submission.selected_choice_ids == ("confirm",)
    return text.strip() == "confirm"


def _deliver_confirmed_task(
    result: TurnResult,
    key: StateKey,
) -> tuple[dict[str, object] | None, TaskCreationRequest | None]:
    if result.task_request_v2 is not None:
        # Task 5 prepares and revalidates v2 only. The durable v2 writer is a
        # later task, so never downcast or leak this request into v1 state.
        return _hook_result(result), None
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
        return (
            _hook_result(
                TurnResult.handled(
                    "Task confirmation is temporarily full. "
                    "Resolve or cancel a pending Task before confirming this one."
                )
            ),
            None,
        )
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


def _recover_failed_input_in_chat(
    failed: _FailedInput,
    key: StateKey,
    now: datetime,
) -> TurnResult:
    recover = getattr(_task_setup, "recover_in_chat", None)
    if callable(recover):
        return recover(
            key[1],
            key[2],
            surface=key[0],
            fallback_text=failed.text,
            now=now,
        )
    _task_setup.enter_chat(
        key[1],
        key[2],
        surface=key[0],
        now=now,
    )
    return TurnResult.replace(failed.text)


def before_user_turn(
    event: Mapping[str, object] | None = None,
    **values: object,
) -> dict[str, object]:
    """Run the Forge chooser and never silently enter Task after an error."""

    combined = _combined_event(event, values)
    cleanup_time = _cleanup_time(combined)
    key = _fallback_key(combined)
    raw_text = combined.get("text")
    choice = raw_text.strip() if isinstance(raw_text, str) else ""
    preserved_text = raw_text if isinstance(raw_text, str) else ""
    service_request: TaskCreationRequest | None = None
    working_directory = _trusted_working_directory(combined)

    # RISK(breaking): an envelope is authoritative over text controls. Parse it
    # before retry, cancel, or confirmation can reach any external Task state.
    try:
        submission = _read_choice_submission(combined)
    except ChoicePromptError as error:
        return _error_result(error)

    try:
        (
            session_id,
            user_id,
            surface,
            text,
            is_new_session,
            now,
            working_directory,
        ) = _read_event(combined)
    except Exception as error:
        with _state_lock:
            _sweep_failed_inputs(cleanup_time)
            if submission is None:
                failed = _failed_inputs.get(key)
                if failed is not None and choice == "continue_chat":
                    _failed_inputs.pop(key, None)
                    try:
                        recovered = _recover_failed_input_in_chat(
                            failed,
                            key,
                            cleanup_time,
                        )
                    except Exception as setup_error:
                        _failed_inputs[key] = failed
                        return _error_result(setup_error)
                    return _hook_result(recovered)
                if failed is not None and choice == "retry":
                    _store_failed_input(
                        key,
                        failed.text,
                        failed.event,
                        failed.working_directory,
                        cleanup_time,
                    )
                    return _error_result(error)
            _store_failed_input(
                key,
                preserved_text,
                combined,
                working_directory,
                cleanup_time,
            )
        return _error_result(error)

    key = (surface, session_id, user_id)

    stop_result = _handle_stop_turn(
        combined=combined,
        key=key,
        text=text,
        submission=submission,
        session_id=session_id,
        user_id=user_id,
        surface=surface,
        working_directory=working_directory,
        now=cleanup_time,
        is_new_session=is_new_session,
    )
    if stop_result is not None:
        return stop_result

    with _state_lock:
        _sweep_failed_inputs(cleanup_time)
        if is_new_session:
            _failed_inputs.pop(key, None)

        # Structured controls never enter the text retry path. A new session
        # likewise cannot resume a predecessor's pending Task from its text.
        if submission is None and not is_new_session:
            pending_result, service_request = _handle_pending_task(key, choice)
            if pending_result is not None:
                return pending_result
        if service_request is None:
            if submission is None and not is_new_session:
                failed = _failed_inputs.get(key)
                if failed is not None:
                    if choice == "continue_chat":
                        _failed_inputs.pop(key, None)
                        try:
                            recovered = _recover_failed_input_in_chat(
                                failed,
                                key,
                                cleanup_time,
                            )
                        except Exception as error:
                            _failed_inputs[key] = failed
                            return _error_result(error)
                        return _hook_result(recovered)
                    if choice == "retry":
                        _failed_inputs.pop(key, None)
                        preserved_text = failed.text
                        combined = dict(failed.event)
                        combined.pop("now", None)
                        combined.pop("working_directory", None)
                        key = _fallback_key(combined)
                        try:
                            # RISK(breaking): replay must recover the saved
                            # envelope before text so selected IDs remain the
                            # sole selection authority after a retry.
                            submission = _read_choice_submission(combined)
                            (
                                session_id,
                                user_id,
                                surface,
                                text,
                                is_new_session,
                                now,
                                _ignored_working_directory,
                            ) = _read_event(combined)
                        except Exception as error:
                            _store_failed_input(
                                key,
                                preserved_text,
                                combined,
                                failed.working_directory,
                                cleanup_time,
                            )
                            return _error_result(error)
                        working_directory = failed.working_directory
                        choice = text.strip()
                        key = (surface, session_id, user_id)
                    else:
                        return _error_result(
                            ValueError(
                                "Choose Retry or Continue in Chat after this error."
                            )
                        )

    if service_request is not None:
        return _finish_task_service_call(key, service_request)

    # TaskSetup owns its own short state lock and deliberately executes Git and
    # GitHub callbacks outside it. Never wrap that call in the plugin lock.
    pending_prompt_reader = getattr(_task_setup, "pending_choice_prompt", None)
    pending_prompt = (
        None
        if not callable(pending_prompt_reader)
        else pending_prompt_reader(
            session_id,
            user_id,
            now,
            surface=surface,
        )
    )
    if submission is None and pending_prompt is not None and text.startswith("choose "):
        try:
            submission = _read_plain_choice_submission(text)
        except ChoicePromptError as error:
            return _hook_result(
                TurnResult.handled(
                    f"That chooser reply is invalid: {error}",
                    choice_prompt=pending_prompt,
                )
            )
        if submission is None:
            return _hook_result(
                TurnResult.handled(
                    "Use: choose <choice_prompt_id> <choice_id[,choice_id...]>",
                    choice_prompt=pending_prompt,
                )
            )
    invalid_submission_reader = getattr(
        _task_setup,
        "invalid_submission_result",
        None,
    )
    if (
        submission is not None
        and not is_new_session
        and callable(invalid_submission_reader)
    ):
        invalid_submission = invalid_submission_reader(
            session_id,
            user_id,
            submission,
            now,
            surface=surface,
        )
        if invalid_submission is not None:
            with _state_lock:
                _failed_inputs.pop(key, None)
            return _hook_result(invalid_submission)

    selected_ids = () if submission is None else submission.selected_choice_ids
    context_choice = choice if submission is None else ""
    pending_ids = (
        set()
        if pending_prompt is None
        else {pending_choice.id for pending_choice in pending_prompt.choices}
    )
    context_required_reader = getattr(
        _task_setup,
        "requires_task_context",
        None,
    )
    pending_step_requires_context = (
        not is_new_session
        and callable(context_required_reader)
        and context_required_reader(
            session_id,
            user_id,
            now,
            surface=surface,
        )
    )
    structured_cancel = (
        submission is not None
        and selected_ids == ("cancel",)
        and pending_prompt is not None
        and submission.choice_prompt_id == pending_prompt.choice_prompt_id
        and "cancel" in pending_ids
    )
    raw_cancel = submission is None and (
        context_choice == "/cancel"
        or (context_choice == "cancel" and "cancel" in pending_ids)
    )
    deterministic_cancel = structured_cancel or raw_cancel
    needs_task_context = not deterministic_cancel and (
        pending_step_requires_context
        or context_choice == "/task"
        or bool(
            {"task", "retry", "confirm"}
            & (
                set(selected_ids)
                if submission is not None
                else ({context_choice} if context_choice in pending_ids else set())
            )
        )
    )
    context: TaskSetupContext | None = None
    if needs_task_context:
        try:
            context = _task_context_factory(working_directory)
        except Exception as error:
            with _state_lock:
                _store_failed_input(
                    key,
                    preserved_text,
                    combined,
                    working_directory,
                    cleanup_time,
                )
            return _error_result(error)

    if (
        context is None
        and not is_new_session
        and _is_confirmation_input(submission, text)
        and pending_prompt is not None
        and "confirm" in pending_ids
        and (
            submission is None
            or submission.choice_prompt_id == pending_prompt.choice_prompt_id
        )
    ):
        with _state_lock:
            if (
                len(_pending_tasks) >= _MAX_PENDING_TASKS
                and key not in _pending_tasks
            ):
                return _hook_result(
                    TurnResult.handled(
                        "Task confirmation is temporarily full. "
                        "Resolve or cancel a pending Task before confirming this one.",
                        choice_prompt=pending_prompt,
                    )
                )

    def invoke_setup() -> TurnResult:
        if submission is None:
            return _task_setup.handle(
                session_id,
                user_id,
                text,
                now,
                surface=surface,
                is_new_session=is_new_session,
                repository=os.environ.get(_REPOSITORY_ENV),
                context=context,
            )
        return _task_setup.handle_submission(
            session_id,
            user_id,
            submission,
            now,
            surface=surface,
            is_new_session=is_new_session,
            repository=os.environ.get(_REPOSITORY_ENV),
            context=context,
        )

    if context is None:
        # Keep the established v1 admission transition atomic. It has no
        # discovery callback, and moving it outside this lock could clear the
        # draft before a concurrently exhausted pending slot is observed.
        with _state_lock:
            if (
                not is_new_session
                and _is_confirmation_input(submission, text)
                and pending_prompt is not None
                and "confirm" in pending_ids
                and len(_pending_tasks) >= _MAX_PENDING_TASKS
                and key not in _pending_tasks
            ):
                return _hook_result(
                    TurnResult.handled(
                        "Task confirmation is temporarily full. "
                        "Resolve or cancel a pending Task before confirming this one.",
                        choice_prompt=pending_prompt,
                    )
                )
            try:
                result = invoke_setup()
            except Exception as error:
                _store_failed_input(
                    key,
                    preserved_text,
                    combined,
                    working_directory,
                    cleanup_time,
                )
                return _error_result(error)
            _failed_inputs.pop(key, None)
            immediate_result, service_request = _deliver_confirmed_task(result, key)
    else:
        try:
            result = invoke_setup()
        except Exception as error:
            with _state_lock:
                _store_failed_input(
                    key,
                    preserved_text,
                    combined,
                    working_directory,
                    cleanup_time,
                )
            return _error_result(error)
        with _state_lock:
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
    "set_task_stop_backend",
]
