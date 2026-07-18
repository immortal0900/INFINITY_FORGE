"""In-memory Chat and Task chooser used before a Hermes user turn."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace as update
from datetime import datetime, timedelta, timezone
from enum import Enum
from threading import RLock
from uuid import UUID, uuid4

from .choice_prompt import Choice, ChoiceMode, ChoicePrompt, ChoicePromptError, ChoiceSubmission
from .task_options import MergeMode, Mode, TaskFlow, TaskSelection
from .task_service import TaskCreationRequest
from .task_settings import TaskContent, TaskSettings


SETUP_TIMEOUT = timedelta(minutes=30)
DEFAULT_MAX_TRACKED_SESSIONS = 1024
DEFAULT_SURFACE = "unknown"

TASK_CONTENT_TEMPLATE = """[SPEC-NNN] <대상>을 <원하는 결과>로 변경

## 목적
이 작업으로 사용자가 얻어야 하는 결과를 한두 문장으로 작성한다.

## 문제
현재 상태: 현재 발생하는 문제나 부족한 동작을 작성한다.
완료 상태: 작업 후 관찰할 수 있어야 하는 상태를 작성한다.

## SoT 근거
근거: `docs/spec.md:42` 또는 관련 이슈·문서 URL
근거가 없는 신규 요구라면 `신규 요구사항`이라고 작성한다.

## 작업 범위
포함: 변경할 기능과 동작을 작성한다.
대상 모듈: 관련 모듈이나 디렉터리를 작성한다.

## 수용 기준 (AC)
1. [AC-01] `<조건 또는 입력>`일 때 `<관찰 가능한 결과>`가 발생한다.
2. [AC-02] `<오류 또는 경계 조건>`일 때 `<오류 코드·메시지·상태>`를 반환한다.
3. [AC-03] 위 동작을 재현하는 테스트가 추가되고 `<정확한 테스트 명령>`이 통과한다.
4. [AC-04] `<구체적인 기존 기능>`이 유지되고 `<검증 방법>`으로 확인된다.

## 범위 제외
제외: 이번 작업에서 변경하지 않을 내용을 작성한다.

## 확정된 제약
호환성: 유지해야 할 API, 데이터 형식 또는 실행 환경을 작성한다.
보안·성능: 지켜야 할 구체적인 제한을 작성한다.
미결정 사항: 없음"""

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
    choice_prompt: ChoicePrompt | None


@dataclass(frozen=True)
class TurnResult:
    """A transport-neutral result for Hermes' ``pre_user_turn`` hook."""

    action: str
    text: str | None = None
    choices: tuple[str, ...] = ()
    choice_prompt: ChoicePrompt | None = None
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
        choice_prompt: ChoicePrompt | None = None,
        next_step: SetupStep | None = None,
        selection: TaskSelection | None = None,
        task_text: str | None = None,
        task_request: TaskCreationRequest | None = None,
    ) -> "TurnResult":
        return cls(
            action="handled",
            text=text,
            choices=choices,
            choice_prompt=choice_prompt,
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

    def handle_submission(
        self,
        session_id: str,
        user_id: str,
        submission: ChoiceSubmission,
        now: datetime | None = None,
        *,
        surface: str = DEFAULT_SURFACE,
        is_new_session: bool = False,
        repository: str | None = None,
    ) -> TurnResult:
        """Apply a structured submission to its pending prompt.

        A new session invalidates every prompt from its predecessor before the
        submission is inspected, so a stale confirmation cannot create a Task.
        """

        current_time = now or self._clock()
        key = (surface, session_id, user_id)
        with self._lock:
            if is_new_session:
                # RISK(breaking): structured callers must send the current
                # session flag or a stale confirmation is deliberately rejected.
                self._discard(key)
                return TurnResult.handled("No pending chooser is available.")
            draft = self._drafts.get(key)
            if draft is None or draft.choice_prompt is None:
                return TurnResult.handled("No pending chooser is available.")
            try:
                selected = draft.choice_prompt.validate_submission(submission, current_time)
            except ChoicePromptError as error:
                return self._same_prompt_result(draft, str(error))
            return self._handle_selected_choice(
                key, draft, selected[0], current_time, repository
            )

    def _handle_locked(
        self,
        key: SessionKey,
        text: str,
        current_time: datetime,
        repository: str | None,
    ) -> TurnResult:
        choice = text.strip()

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
                draft = self._new_mode_draft(text, current_time)
                self._store_draft(key, draft, current_time)
                return self._mode_prompt(draft)

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
        draft = self._new_task_draft(now)
        self._store_draft(key, draft, now)
        return self._task_flow_prompt(draft)

    def _new_task_draft(self, now: datetime) -> SetupDraft:
        expires_at = self._deadline(now)
        return SetupDraft(
            step=SetupStep.TASK_FLOW,
            first_input=None,
            task_flow=None,
            merge_mode=None,
            task_text=None,
            task_request=None,
            expires_at=expires_at,
            choice_prompt=self._choice_prompt(SetupStep.TASK_FLOW, expires_at),
        )

    def _new_mode_draft(self, first_input: str, now: datetime) -> SetupDraft:
        expires_at = self._deadline(now)
        return SetupDraft(
            step=SetupStep.MODE,
            first_input=first_input,
            task_flow=None,
            merge_mode=None,
            task_text=None,
            task_request=None,
            expires_at=expires_at,
            choice_prompt=self._choice_prompt(SetupStep.MODE, expires_at),
        )

    @staticmethod
    def _deadline(now: datetime) -> datetime:
        return now + SETUP_TIMEOUT

    def _refresh(self, draft: SetupDraft, now: datetime, **changes: object) -> SetupDraft:
        expires_at = self._deadline(now)
        step = changes.get("step", draft.step)
        choice_prompt = (
            self._choice_prompt(step, expires_at)
            if isinstance(step, SetupStep) and self._step_has_choices(step)
            else None
        )
        return update(
            draft,
            expires_at=expires_at,
            choice_prompt=choice_prompt,
            **changes,
        )

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
            return self._task_flow_prompt(task_draft)

        refreshed = self._refresh(draft, now)
        self._store_draft(key, refreshed, now)
        return self._mode_prompt(refreshed, "Choose either chat or task.")

    def _handle_task_flow(
        self,
        key: SessionKey,
        draft: SetupDraft,
        choice: str,
        now: datetime,
    ) -> TurnResult:
        task_flow = self._selected_value(choice, _FLOW_LABELS)
        if task_flow is None:
            refreshed = self._refresh(draft, now)
            self._store_draft(key, refreshed, now)
            return self._task_flow_prompt(refreshed, "Choose one listed Task flow.")

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
        return self._merge_mode_prompt(selected)

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
            refreshed = self._refresh(draft, now)
            self._store_draft(key, refreshed, now)
            return self._merge_mode_prompt(refreshed, "Choose one listed merge mode.")

        selected = self._refresh(
            draft,
            now,
            merge_mode=merge_mode,
            task_request=None,
        )
        if selected.first_input is None:
            self._store_draft(
                key,
                update(selected, step=SetupStep.TASK_CONTENT, choice_prompt=None),
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
    def _selected_value(choice: str, values: dict[Enum, str]) -> Enum | None:
        """Match only the stable ID; labels remain presentation-only text."""

        normalized = choice.strip()
        for value in values:
            if normalized == str(value.value):
                return value
        return None

    def _handle_selected_choice(
        self,
        key: SessionKey,
        draft: SetupDraft,
        choice_id: str,
        now: datetime,
        repository: str | None,
    ) -> TurnResult:
        if draft.step is SetupStep.MODE:
            return self._handle_mode(key, draft, choice_id, now)
        if draft.step is SetupStep.TASK_FLOW:
            return self._handle_task_flow(key, draft, choice_id, now)
        if draft.step is SetupStep.MERGE_MODE:
            return self._handle_merge_mode(key, draft, choice_id, now, repository)
        if draft.step is SetupStep.CONFIRM:
            return self._handle_confirm(key, draft, choice_id, now)
        return TurnResult.handled("This Task step requires text input.")

    def _same_prompt_result(self, draft: SetupDraft, error: str) -> TurnResult:
        prefix = f"Choice was not applied: {error}."
        if draft.step is SetupStep.MODE:
            return self._mode_prompt(draft, prefix)
        if draft.step is SetupStep.TASK_FLOW:
            return self._task_flow_prompt(draft, prefix)
        if draft.step is SetupStep.MERGE_MODE:
            return self._merge_mode_prompt(draft, prefix)
        if draft.step is SetupStep.CONFIRM:
            return self._preview_prompt(draft, prefix)
        return TurnResult.handled(prefix)

    @staticmethod
    def _step_has_choices(step: SetupStep) -> bool:
        return step in {
            SetupStep.MODE,
            SetupStep.TASK_FLOW,
            SetupStep.MERGE_MODE,
            SetupStep.CONFIRM,
        }

    @staticmethod
    def _choice_prompt(step: SetupStep, expires_at: datetime) -> ChoicePrompt:
        if step is SetupStep.MODE:
            choices = tuple(
                Choice(value.value, _MODE_LABELS[value], _MODE_DETAILS[value])
                for value in Mode
            )
            submit_label = "Choose mode"
        elif step is SetupStep.TASK_FLOW:
            choices = tuple(
                Choice(value.value, _FLOW_LABELS[value], _FLOW_DETAILS[value])
                for value in TaskFlow
            )
            submit_label = "Choose checks"
        elif step is SetupStep.MERGE_MODE:
            choices = tuple(
                Choice(value.value, _MERGE_LABELS[value], _MERGE_DETAILS[value])
                for value in MergeMode
            )
            submit_label = "Choose merge mode"
        elif step is SetupStep.CONFIRM:
            choices = (
                Choice("confirm", "Confirm Task", "Create the reviewed Task"),
                Choice("cancel", "Cancel", "Discard the reviewed Task"),
            )
            submit_label = "Confirm Task"
        else:
            raise RuntimeError("Task step does not have choices")
        return ChoicePrompt(
            choice_prompt_id=str(uuid4()),
            choice_mode=ChoiceMode.SINGLE,
            min_choices=1,
            max_choices=1,
            submit_label=submit_label,
            expires_at=expires_at,
            choices=choices,
        )

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
    def _mode_prompt(draft: SetupDraft, prefix: str | None = None) -> TurnResult:
        text = TaskSetup._prompt_text(
            prefix or "Choose Chat for a normal conversation or Task for implementation.",
            _MODE_LABELS,
            _MODE_DETAILS,
        )
        return TurnResult.handled(
            text,
            choices=TaskSetup._prompt_choice_ids(draft),
            choice_prompt=draft.choice_prompt,
            next_step=SetupStep.MODE,
        )

    @staticmethod
    def _task_flow_prompt(draft: SetupDraft, prefix: str | None = None) -> TurnResult:
        text = TaskSetup._prompt_text(
            prefix or "Choose the checks for this Task.",
            _FLOW_LABELS,
            _FLOW_DETAILS,
        )
        return TurnResult.handled(
            text,
            choices=TaskSetup._prompt_choice_ids(draft),
            choice_prompt=draft.choice_prompt,
            next_step=SetupStep.TASK_FLOW,
        )

    @staticmethod
    def _merge_mode_prompt(draft: SetupDraft, prefix: str | None = None) -> TurnResult:
        text = TaskSetup._prompt_text(
            prefix or "Choose how a validated pull request may be merged.",
            _MERGE_LABELS,
            _MERGE_DETAILS,
        )
        return TurnResult.handled(
            text,
            choices=TaskSetup._prompt_choice_ids(draft),
            choice_prompt=draft.choice_prompt,
            next_step=SetupStep.MERGE_MODE,
        )

    @staticmethod
    def _task_content_prompt(prefix: str | None = None) -> TurnResult:
        intro = prefix or "Enter the Task content."
        return TurnResult.handled(
            f"{intro}\n\nUse this format:\n\n{TASK_CONTENT_TEMPLATE}",
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
            choices=TaskSetup._prompt_choice_ids(draft),
            choice_prompt=draft.choice_prompt,
            next_step=SetupStep.CONFIRM,
            task_request=request,
        )

    @staticmethod
    def _prompt_choice_ids(draft: SetupDraft) -> tuple[str, ...]:
        if draft.choice_prompt is None:
            raise RuntimeError("Task choice prompt is missing")
        return tuple(choice.id for choice in draft.choice_prompt.choices)

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
