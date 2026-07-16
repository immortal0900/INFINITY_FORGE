"""In-memory Chat and Task chooser used before a Hermes user turn."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace as update
from datetime import datetime, timedelta, timezone
from enum import Enum
from threading import RLock
from uuid import UUID, uuid4

from .task_options import MergeMode, Mode, TaskFlow, TaskSelection
from .task_service import TaskCreationRequest
from .task_settings import TaskContent, TaskSettings


SETUP_TIMEOUT = timedelta(minutes=30)
DEFAULT_MAX_TRACKED_SESSIONS = 1024
DEFAULT_SURFACE = "unknown"

_ACCEPTANCE_CRITERION = re.compile(
    r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(?P<text>\S.*)$"
)
_FLOW_PATHS = {
    TaskFlow.BUILD: "Build → Automated Tests",
    TaskFlow.BUILD_REVIEW: "Build → Review → Automated Tests",
    TaskFlow.BUILD_REVIEW_DEEP_CHECK: (
        "Build → Review → Deep Check → Automated Tests"
    ),
}
_MERGE_RESULTS = {
    MergeMode.MANUAL: "Human merges after all checks pass",
    MergeMode.SAFE_AUTO: (
        "System merges safe-file changes after all checks pass"
    ),
    MergeMode.FULL_AUTO: (
        "System merges any pull request after all checks pass"
    ),
}
_MODE_LABELS = {
    Mode.CHAT: "Chat",
    Mode.TASK: "Task",
}
_FLOW_LABELS = {
    TaskFlow.BUILD: "Build",
    TaskFlow.BUILD_REVIEW: "Build + Review",
    TaskFlow.BUILD_REVIEW_DEEP_CHECK: "Build + Review + Deep Check",
}
_MERGE_LABELS = {
    MergeMode.MANUAL: "Manual Merge",
    MergeMode.SAFE_AUTO: "Safe Files Auto-Merge",
    MergeMode.FULL_AUTO: "All Validated PRs Auto-Merge",
}
_MODE_DETAILS = {
    Mode.CHAT: "normal questions and design discussion; creates no work item",
    Mode.TASK: "implementation; asks for checks and a merge choice before creating work",
}
_FLOW_DETAILS = {
    TaskFlow.BUILD: "build the change and run automated tests",
    TaskFlow.BUILD_REVIEW: "build, run automated tests, and add a separate review",
    TaskFlow.BUILD_REVIEW_DEEP_CHECK: (
        "build, review, and test additional failure and edge cases"
    ),
}
_MERGE_DETAILS = {
    MergeMode.MANUAL: "a person merges after every required check passes",
    MergeMode.SAFE_AUTO: (
        "the system merges only low-risk file changes after every required check passes"
    ),
    MergeMode.FULL_AUTO: (
        "the system merges any pull request after every required check passes"
    ),
}


class SetupStep(str, Enum):
    MODE = "mode"
    TASK_FLOW = "task_flow"
    MERGE_MODE = "merge_mode"
    TASK_CONTENT = "task_content"
    CONFIRM = "confirm"


@dataclass(frozen=True)
class SetupDraft:
    """One session's unconfirmed Task choices and stashed first input."""

    step: SetupStep
    first_input: str | None
    task_flow: TaskFlow | None
    merge_mode: MergeMode | None
    task_text: str | None
    task_request: TaskCreationRequest | None
    expires_at: datetime


@dataclass(frozen=True)
class TurnResult:
    """A transport-neutral result for Hermes' ``pre_user_turn`` hook."""

    action: str
    text: str | None = None
    choices: tuple[str, ...] = ()
    next_step: SetupStep | None = None
    selection: TaskSelection | None = None
    task_text: str | None = None
    task_request: TaskCreationRequest | None = None

    @classmethod
    def continue_original(cls) -> "TurnResult":
        return cls(action="continue")

    @classmethod
    def replace(cls, text: str) -> "TurnResult":
        return cls(action="replace", text=text)

    @classmethod
    def handled(
        cls,
        text: str,
        *,
        choices: tuple[str, ...] = (),
        next_step: SetupStep | None = None,
        selection: TaskSelection | None = None,
        task_text: str | None = None,
        task_request: TaskCreationRequest | None = None,
    ) -> "TurnResult":
        return cls(
            action="handled",
            text=text,
            choices=choices,
            next_step=next_step,
            selection=selection,
            task_text=task_text,
            task_request=task_request,
        )


Clock = Callable[[], datetime]
RequestIdFactory = Callable[[], str]
SessionKey = tuple[str, str, str]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskSetup:
    """Keep chooser state in memory and perform no GitHub or Kanban writes."""

    def __init__(
        self,
        clock: Clock | None = None,
        *,
        start_in_task: bool = False,
        max_tracked_sessions: int = DEFAULT_MAX_TRACKED_SESSIONS,
        request_id_factory: RequestIdFactory | None = None,
    ) -> None:
        if max_tracked_sessions < 1:
            raise ValueError("max_tracked_sessions must be at least 1")
        self._clock = clock or _utc_now
        self._start_in_task = start_in_task
        self._max_tracked_sessions = max_tracked_sessions
        self._request_id_factory = request_id_factory or (lambda: str(uuid4()))
        self._lock = RLock()
        self._drafts: dict[SessionKey, SetupDraft] = {}
        self._chat_sessions: set[SessionKey] = set()
        self._last_seen: dict[SessionKey, datetime] = {}

    def handle(
        self,
        session_id: str,
        user_id: str,
        text: str,
        now: datetime | None = None,
        *,
        surface: str = DEFAULT_SURFACE,
        is_new_session: bool = False,
        repository: str | None = None,
    ) -> TurnResult:
        """Consume one user input and return continue, replace, or handled."""

        current_time = now or self._clock()
        key = (surface, session_id, user_id)
        with self._lock:
            self._sweep(current_time)
            if is_new_session:
                self._discard(key)
            return self._handle_locked(key, text, current_time, repository)

    def _handle_locked(
        self,
        key: SessionKey,
        text: str,
        current_time: datetime,
        repository: str | None,
    ) -> TurnResult:
        choice = text.strip().lower()

        if choice == "/task":
            return self._start_task(key, current_time)
        if choice == "/cancel":
            return self._enter_chat(key, current_time)

        draft = self._drafts.get(key)
        if draft is not None and current_time >= draft.expires_at:
            self._discard(key)
            draft = None

        if draft is None:
            if key in self._chat_sessions:
                self._remember(key, current_time)
                return TurnResult.continue_original()
            if self._start_in_task:
                draft = self._new_task_draft(current_time)
                self._store_draft(key, draft, current_time)
            else:
                self._store_draft(
                    key,
                    SetupDraft(
                        step=SetupStep.MODE,
                        first_input=text,
                        task_flow=None,
                        merge_mode=None,
                        task_text=None,
                        task_request=None,
                        expires_at=self._deadline(current_time),
                    ),
                    current_time,
                )
                return self._mode_prompt()

        if draft.step is SetupStep.MODE:
            return self._handle_mode(key, draft, choice, current_time)
        if draft.step is SetupStep.TASK_FLOW:
            return self._handle_task_flow(key, draft, choice, current_time)
        if draft.step is SetupStep.MERGE_MODE:
            return self._handle_merge_mode(
                key, draft, choice, current_time, repository
            )
        if draft.step is SetupStep.TASK_CONTENT:
            return self._handle_task_content(
                key, draft, text, current_time, repository
            )
        return self._handle_confirm(key, draft, choice, current_time)

    def enter_chat(
        self,
        session_id: str,
        user_id: str,
        *,
        surface: str = DEFAULT_SURFACE,
        now: datetime | None = None,
    ) -> TurnResult:
        """Discard any Task draft and explicitly put the session in Chat."""

        current_time = now or self._clock()
        key = (surface, session_id, user_id)
        with self._lock:
            self._sweep(current_time)
            return self._enter_chat(key, current_time)

    def _enter_chat(self, key: SessionKey, now: datetime) -> TurnResult:
        self._drafts.pop(key, None)
        self._store_chat(key, now)
        return TurnResult.handled("Task setup cancelled. Continuing in Chat.")

    def _start_task(self, key: SessionKey, now: datetime) -> TurnResult:
        self._chat_sessions.discard(key)
        self._store_draft(key, self._new_task_draft(now), now)
        return self._task_flow_prompt()

    def _new_task_draft(self, now: datetime) -> SetupDraft:
        return SetupDraft(
            step=SetupStep.TASK_FLOW,
            first_input=None,
            task_flow=None,
            merge_mode=None,
            task_text=None,
            task_request=None,
            expires_at=self._deadline(now),
        )

    @staticmethod
    def _deadline(now: datetime) -> datetime:
        return now + SETUP_TIMEOUT

    def _refresh(self, draft: SetupDraft, now: datetime, **changes: object) -> SetupDraft:
        return update(draft, expires_at=self._deadline(now), **changes)

    def _sweep(self, now: datetime) -> None:
        expired = [
            key for key, draft in self._drafts.items() if now >= draft.expires_at
        ]
        for key in expired:
            self._discard(key)

    def _discard(self, key: SessionKey) -> None:
        self._drafts.pop(key, None)
        self._chat_sessions.discard(key)
        self._last_seen.pop(key, None)

    def _remember(self, key: SessionKey, now: datetime) -> None:
        self._last_seen[key] = now
        self._trim(protected=key)

    def _store_draft(self, key: SessionKey, draft: SetupDraft, now: datetime) -> None:
        self._chat_sessions.discard(key)
        self._drafts[key] = draft
        self._remember(key, now)

    def _store_chat(self, key: SessionKey, now: datetime) -> None:
        self._drafts.pop(key, None)
        self._chat_sessions.add(key)
        self._remember(key, now)

    def _trim(self, *, protected: SessionKey) -> None:
        tracked = set(self._drafts) | self._chat_sessions
        while len(tracked) > self._max_tracked_sessions:
            candidates = [key for key in tracked if key != protected]
            if not candidates:
                return
            oldest = min(
                candidates,
                key=lambda key: self._last_seen.get(
                    key, datetime.min.replace(tzinfo=timezone.utc)
                ),
            )
            self._discard(oldest)
            tracked.discard(oldest)

    def _handle_mode(
        self,
        key: SessionKey,
        draft: SetupDraft,
        choice: str,
        now: datetime,
    ) -> TurnResult:
        if choice == Mode.CHAT.value:
            self._store_chat(key, now)
            return TurnResult.replace(draft.first_input or "")
        if choice == Mode.TASK.value:
            task_draft = self._refresh(
                draft,
                now,
                step=SetupStep.TASK_FLOW,
                task_flow=None,
                merge_mode=None,
                task_text=None,
                task_request=None,
            )
            self._store_draft(key, task_draft, now)
            return self._task_flow_prompt()

        self._store_draft(key, self._refresh(draft, now), now)
        return self._mode_prompt("Choose either chat or task.")

    def _handle_task_flow(
        self,
        key: SessionKey,
        draft: SetupDraft,
        choice: str,
        now: datetime,
    ) -> TurnResult:
        task_flow = self._selected_value(choice, _FLOW_LABELS)
        if task_flow is None:
            self._store_draft(key, self._refresh(draft, now), now)
            return self._task_flow_prompt("Choose one listed Task flow.")

        selected = self._refresh(
            draft,
            now,
            step=SetupStep.MERGE_MODE,
            task_flow=task_flow,
            merge_mode=None,
            task_text=None,
            task_request=None,
        )
        self._store_draft(key, selected, now)
        return self._merge_mode_prompt()

    def _handle_merge_mode(
        self,
        key: SessionKey,
        draft: SetupDraft,
        choice: str,
        now: datetime,
        repository: str | None,
    ) -> TurnResult:
        merge_mode = self._selected_value(choice, _MERGE_LABELS)
        if merge_mode is None:
            self._store_draft(key, self._refresh(draft, now), now)
            return self._merge_mode_prompt("Choose one listed merge mode.")

        selected = self._refresh(
            draft,
            now,
            merge_mode=merge_mode,
            task_request=None,
        )
        if selected.first_input is None:
            self._store_draft(
                key,
                update(selected, step=SetupStep.TASK_CONTENT),
                now,
            )
            return self._task_content_prompt()
        return self._prepare_preview(
            key,
            selected,
            selected.first_input,
            now,
            repository,
        )

    def _handle_task_content(
        self,
        key: SessionKey,
        draft: SetupDraft,
        text: str,
        now: datetime,
        repository: str | None,
    ) -> TurnResult:
        if not text.strip():
            self._store_draft(key, self._refresh(draft, now), now)
            return self._task_content_prompt("Task content cannot be empty.")
        return self._prepare_preview(key, draft, text, now, repository)

    def _prepare_preview(
        self,
        key: SessionKey,
        draft: SetupDraft,
        task_text: str,
        now: datetime,
        repository: str | None,
    ) -> TurnResult:
        if draft.task_flow is None or draft.merge_mode is None:
            raise RuntimeError("Task choices are incomplete")
        if not isinstance(repository, str) or not repository.strip():
            raise RuntimeError("INFINITY_FORGE_REPOSITORY is required")

        confirmed_at = self._normalized_utc(now)
        request = TaskCreationRequest(
            request_id=self._new_request_id(),
            repository=repository,
            content=self._task_content(task_text),
            task_flow=draft.task_flow,
            merge_mode=draft.merge_mode,
            confirmed_by=key[2],
            confirmed_at=confirmed_at,
        )
        # Validate the exact immutable request and derive its automatic-merge
        # expiry before it is shown to the user.
        TaskSettings.create(
            request_id=request.request_id,
            repository=request.repository,
            task_content=request.content,
            task_flow=request.task_flow,
            merge_mode=request.merge_mode,
            confirmed_by=request.confirmed_by,
            confirmed_at=request.confirmed_at,
        )
        preview = self._refresh(
            draft,
            now,
            step=SetupStep.CONFIRM,
            task_text=task_text,
            task_request=request,
        )
        self._store_draft(key, preview, now)
        return self._preview_prompt(preview)

    def _handle_confirm(
        self,
        key: SessionKey,
        draft: SetupDraft,
        choice: str,
        now: datetime,
    ) -> TurnResult:
        if choice == "cancel":
            return self._enter_chat(key, now)
        if choice != "confirm":
            refreshed = self._refresh(draft, now)
            self._store_draft(key, refreshed, now)
            return self._preview_prompt(
                refreshed,
                "Choose confirm to create this Task or cancel to discard it.",
            )
        if draft.task_text is None:
            raise RuntimeError("Task content is missing")
        if draft.task_request is None:
            raise RuntimeError("Task request is missing")
        return self._finish_task(key, draft, draft.task_text, draft.task_request, now)

    def _finish_task(
        self,
        key: SessionKey,
        draft: SetupDraft,
        task_text: str,
        task_request: TaskCreationRequest,
        now: datetime,
    ) -> TurnResult:
        if draft.task_flow is None or draft.merge_mode is None:
            raise RuntimeError("Task choices are incomplete")
        selection = TaskSelection(Mode.TASK, draft.task_flow, draft.merge_mode)
        self._store_chat(key, now)
        return TurnResult.handled(
            "Task confirmed and ready for the Task service.",
            selection=selection,
            task_text=task_text,
            task_request=task_request,
        )

    def _new_request_id(self) -> str:
        raw_request_id = self._request_id_factory()
        try:
            request_id = str(UUID(str(raw_request_id)))
        except (AttributeError, TypeError, ValueError) as error:
            raise RuntimeError("Task request ID must be a UUID") from error
        if request_id != raw_request_id:
            raise RuntimeError("Task request ID must be a canonical UUID string")
        return request_id

    @staticmethod
    def _selected_value(choice: str, labels: dict[Enum, str]) -> Enum | None:
        """Match a visible label while retaining stable internal storage values."""

        normalized = choice.strip().casefold()
        for value, label in labels.items():
            if normalized in {label.casefold(), str(value.value).casefold()}:
                return value
        return None

    @staticmethod
    def _normalized_utc(value: datetime) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise RuntimeError("Task confirmation time must include a timezone")
        if value.utcoffset() is None:
            raise RuntimeError("Task confirmation time must include a timezone")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _task_content(raw_text: str) -> TaskContent:
        title = next(
            (line.strip() for line in raw_text.splitlines() if line.strip()),
            "",
        )[:256]
        criteria = tuple(
            match.group("text").strip()
            for line in raw_text.splitlines()
            if (match := _ACCEPTANCE_CRITERION.fullmatch(line)) is not None
        )
        return TaskContent(
            title=title,
            description=raw_text,
            acceptance_criteria=criteria or (raw_text,),
        )

    @staticmethod
    def _mode_prompt(prefix: str | None = None) -> TurnResult:
        text = TaskSetup._prompt_text(
            prefix or "Choose Chat for a normal conversation or Task for implementation.",
            _MODE_LABELS,
            _MODE_DETAILS,
        )
        return TurnResult.handled(
            text,
            choices=tuple(mode.value for mode in Mode),
            next_step=SetupStep.MODE,
        )

    @staticmethod
    def _task_flow_prompt(prefix: str | None = None) -> TurnResult:
        text = TaskSetup._prompt_text(
            prefix or "Choose the checks for this Task.",
            _FLOW_LABELS,
            _FLOW_DETAILS,
        )
        return TurnResult.handled(
            text,
            choices=tuple(flow.value for flow in TaskFlow),
            next_step=SetupStep.TASK_FLOW,
        )

    @staticmethod
    def _merge_mode_prompt(prefix: str | None = None) -> TurnResult:
        text = TaskSetup._prompt_text(
            prefix or "Choose how a validated pull request may be merged.",
            _MERGE_LABELS,
            _MERGE_DETAILS,
        )
        return TurnResult.handled(
            text,
            choices=tuple(mode.value for mode in MergeMode),
            next_step=SetupStep.MERGE_MODE,
        )

    @staticmethod
    def _task_content_prompt(prefix: str | None = None) -> TurnResult:
        return TurnResult.handled(
            prefix or "Enter the Task content.",
            next_step=SetupStep.TASK_CONTENT,
        )

    @staticmethod
    def _preview_prompt(
        draft: SetupDraft,
        prefix: str | None = None,
    ) -> TurnResult:
        if (
            draft.task_flow is None
            or draft.merge_mode is None
            or draft.task_text is None
            or draft.task_request is None
        ):
            raise RuntimeError("Task preview is incomplete")
        request = draft.task_request
        settings = TaskSettings.create(
            request_id=request.request_id,
            repository=request.repository,
            task_content=request.content,
            task_flow=request.task_flow,
            merge_mode=request.merge_mode,
            confirmed_by=request.confirmed_by,
            confirmed_at=request.confirmed_at,
        )
        criteria = "\n".join(
            f"{number}. {criterion}"
            for number, criterion in enumerate(
                request.content.acceptance_criteria,
                start=1,
            )
        )
        expiry = (
            "not granted"
            if settings.auto_merge_expires_at is None
            else TaskSetup._format_timestamp(settings.auto_merge_expires_at)
        )
        details = (
            f"Project: {request.repository}\n"
            f"Task ID: {request.request_id}\n"
            f"Title: {request.content.title}\n"
            f"Description:\n{request.content.description}\n"
            f"Acceptance criteria:\n{criteria}\n"
            f"Checks selected: {_FLOW_LABELS[request.task_flow]}\n"
            f"Checks: {_FLOW_PATHS[request.task_flow]}\n"
            f"Merge choice: {_MERGE_LABELS[request.merge_mode]}\n"
            f"Merge result: {_MERGE_RESULTS[request.merge_mode]}\n"
            f"Automatic merge permission until: {expiry}\n"
            f"Confirmed by: {request.confirmed_by}\n"
            "Confirmed at: "
            f"{TaskSetup._format_timestamp(request.confirmed_at)}"
        )
        text = f"{prefix}\n\n{details}" if prefix else details
        return TurnResult.handled(
            text,
            choices=("confirm", "cancel"),
            next_step=SetupStep.CONFIRM,
            task_request=request,
        )

    @staticmethod
    def _format_timestamp(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _prompt_text(
        intro: str,
        labels: dict[Enum, str],
        details: dict[Enum, str],
    ) -> str:
        if labels.keys() != details.keys():
            raise RuntimeError("Task prompt labels and descriptions do not match")
        options = "\n".join(
            f"- {label} — {details[value]}" for value, label in labels.items()
        )
        return f"{intro}\n\nOptions:\n{options}"


def begin_task_setup(
    clock: Clock | None = None,
    *,
    request_id_factory: RequestIdFactory | None = None,
) -> TaskSetup:
    """Return a setup controller whose first input is a Task-flow choice."""

    return TaskSetup(
        clock=clock,
        start_in_task=True,
        request_id_factory=request_id_factory,
    )
