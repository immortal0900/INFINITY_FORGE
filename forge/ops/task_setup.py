"""In-memory Chat and Task chooser used before a Hermes user turn."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace as update
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from threading import RLock
from uuid import UUID, uuid4

from .choice_prompt import Choice, ChoiceMode, ChoicePrompt, ChoicePromptError, ChoiceSubmission
from .project_discovery import HARD_MAX_PROJECTS, ProjectPathProbe
from .task_options import MergeMode, Mode, TaskFlow, TaskSelection
from .task_projects import TaskProject, TaskProjectError, normalize_github_remote
from .task_service import TaskCreationRequest
from .task_settings import TaskContent, TaskSettings
from .task_settings_v2 import TaskRequestV2


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


def _safe_display_text(value: str) -> str:
    """Escape terminal controls without changing the trusted stored value."""

    escaped: list[str] = []
    named = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
    for character in value:
        if character in named:
            escaped.append(named[character])
        elif character.isprintable():
            escaped.append(character)
        else:
            codepoint = ord(character)
            escaped.append(
                f"\\u{codepoint:04x}"
                if codepoint <= 0xFFFF
                else f"\\U{codepoint:08x}"
            )
    return "".join(escaped)


class SetupStep(str, Enum):
    MODE = "mode"
    PROJECTS = "projects"
    PROJECT_PATH = "project_path"
    PROJECT_REMOTE = "project_remote"
    PROJECT_BRANCH = "project_branch"
    PROJECT_BRANCH_NAME = "project_branch_name"
    TASK_FLOW = "task_flow"
    MERGE_MODE = "merge_mode"
    MERGE_ORDER = "merge_order"
    TASK_CONTENT = "task_content"
    CONFIRM = "confirm"


ProjectDiscoverer = Callable[[str | None], tuple[TaskProject, ...]]
ProjectValidator = Callable[[tuple[TaskProject, ...]], tuple[TaskProject, ...]]
ProjectPathProber = Callable[[str], ProjectPathProbe]
ProjectBinder = Callable[[ProjectPathProbe, str, str], TaskProject]


@dataclass(frozen=True)
class TaskSetupContext:
    """Trusted, non-model context used only by the v2 Project setup path."""

    working_directory: str | None
    management_repository: str
    task_owner_host: str
    discover_projects: ProjectDiscoverer
    validate_projects: ProjectValidator
    probe_project_path: ProjectPathProber | None = None
    bind_project: ProjectBinder | None = None

    def __post_init__(self) -> None:
        if self.working_directory is not None and (
            not isinstance(self.working_directory, str)
            or not self.working_directory.strip()
        ):
            raise ValueError("working_directory must be a non-empty string or None")
        if self.working_directory is not None:
            try:
                working_path = Path(self.working_directory)
                if not working_path.is_absolute():
                    raise ValueError
                resolved = working_path.resolve(strict=True)
            except (OSError, RuntimeError, ValueError):
                raise ValueError(
                    "working_directory must be a canonical absolute directory"
                ) from None
            if not resolved.is_dir() or str(resolved) != self.working_directory:
                raise ValueError(
                    "working_directory must be a canonical absolute directory"
                )
        if not isinstance(self.management_repository, str):
            raise ValueError(
                "management_repository must use canonical OWNER/REPO format"
            )
        try:
            normalized_repository = normalize_github_remote(
                f"https://github.com/{self.management_repository}"
            )
        except TaskProjectError:
            raise ValueError(
                "management_repository must use canonical OWNER/REPO format"
            ) from None
        if normalized_repository != self.management_repository:
            raise ValueError(
                "management_repository must use canonical OWNER/REPO format"
            )
        if not isinstance(self.task_owner_host, str):
            raise ValueError("task_owner_host must be a canonical UUID")
        try:
            parsed_host = UUID(self.task_owner_host)
        except ValueError:
            raise ValueError("task_owner_host must be a canonical UUID") from None
        if str(parsed_host) != self.task_owner_host:
            raise ValueError("task_owner_host must be a canonical UUID")
        if not callable(self.discover_projects) or not callable(self.validate_projects):
            raise ValueError("Project discovery and validation must be callable")
        direct_callbacks = (self.probe_project_path, self.bind_project)
        if any(callback is None for callback in direct_callbacks):
            if any(callback is not None for callback in direct_callbacks):
                raise ValueError("Direct Project callbacks must be configured together")
        elif any(not callable(callback) for callback in direct_callbacks):
            raise ValueError("Direct Project callbacks must be callable")


@dataclass(frozen=True)
class SetupDraft:
    """One session's unconfirmed Task choices and stashed first input."""

    step: SetupStep
    first_input: str | None
    project_candidates: tuple[TaskProject, ...]
    projects: tuple[TaskProject, ...]
    project_probe: ProjectPathProbe | None
    project_remote_name: str | None
    task_flow: TaskFlow | None
    merge_mode: MergeMode | None
    merge_order: tuple[str, ...] | None
    management_repository: str | None
    task_owner_host: str | None
    task_text: str | None
    task_request: TaskCreationRequest | None
    v2_request_id: str | None
    task_request_v2: TaskRequestV2 | None
    expires_at: datetime
    choice_prompt: ChoicePrompt | None
    generation: int
    operation_token: str | None


@dataclass(frozen=True)
class _SetupWork:
    key: SessionKey
    context: TaskSetupContext
    token: str
    generation: int
    step: SetupStep
    expires_at: datetime
    prompt_id: str | None
    selected_ids: tuple[str, ...]


@dataclass(frozen=True)
class _DiscoveryWork(_SetupWork):
    prefix: str | None = None


@dataclass(frozen=True)
class _ValidationWork(_SetupWork):
    projects: tuple[TaskProject, ...] = ()


@dataclass(frozen=True)
class _ProjectProbeWork(_SetupWork):
    raw_path: str = ""


@dataclass(frozen=True)
class _ProjectBindWork(_SetupWork):
    probe: ProjectPathProbe | None = None
    remote_name: str = ""
    branch: str = ""
    return_step: SetupStep = SetupStep.PROJECT_BRANCH


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
    task_request_v2: TaskRequestV2 | None = None
    choice_prompt_paused: bool = False

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
        task_request_v2: TaskRequestV2 | None = None,
        choice_prompt_paused: bool = False,
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
            task_request_v2=task_request_v2,
            choice_prompt_paused=choice_prompt_paused,
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
        context: TaskSetupContext | None = None,
    ) -> TurnResult:
        """Consume one trusted/internal text input.

        Production surfaces must bind chooser replies to a prompt through
        :meth:`handle_submission`; raw stable IDs remain a compatibility seam
        for direct callers and tests only.
        """

        current_time = now or self._clock()
        key = (surface, session_id, user_id)
        with self._lock:
            self._sweep(current_time)
            if is_new_session:
                self._discard(key)
            outcome = self._handle_locked(
                key,
                text,
                current_time,
                repository,
                context,
            )
        return self._resolve_work(
            outcome,
            current_time if now is not None else None,
        )

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
        context: TaskSetupContext | None = None,
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
            outcome = self._handle_selected_choices(
                key,
                draft,
                selected,
                current_time,
                repository,
                context,
            )
        return self._resolve_work(
            outcome,
            current_time if now is not None else None,
        )

    def pending_choice_prompt(
        self,
        session_id: str,
        user_id: str,
        now: datetime | None = None,
        *,
        surface: str = DEFAULT_SURFACE,
    ) -> ChoicePrompt | None:
        """Peek at the current immutable prompt without changing draft state."""

        key = (surface, session_id, user_id)
        with self._lock:
            draft = self._drafts.get(key)
            return None if draft is None else draft.choice_prompt

    def requires_task_context(
        self,
        session_id: str,
        user_id: str,
        *,
        surface: str = DEFAULT_SURFACE,
    ) -> bool:
        """Return whether the pending step needs trusted Project callbacks."""

        with self._lock:
            draft = self._drafts.get((surface, session_id, user_id))
            return draft is not None and draft.step in {
                SetupStep.PROJECTS,
                SetupStep.PROJECT_PATH,
                SetupStep.PROJECT_REMOTE,
                SetupStep.PROJECT_BRANCH,
                SetupStep.PROJECT_BRANCH_NAME,
            }

    def invalid_submission_result(
        self,
        session_id: str,
        user_id: str,
        submission: ChoiceSubmission,
        now: datetime | None = None,
        *,
        surface: str = DEFAULT_SURFACE,
    ) -> TurnResult | None:
        """Return the normal fail-closed result, or ``None`` for a valid submission.

        This is a read-only preflight for callers that must make admission
        decisions before applying a structured selection.
        """

        current_time = now or self._clock()
        key = (surface, session_id, user_id)
        with self._lock:
            draft = self._drafts.get(key)
            if draft is None or draft.choice_prompt is None:
                return TurnResult.handled("No pending chooser is available.")
            try:
                draft.choice_prompt.validate_submission(submission, current_time)
            except ChoicePromptError as error:
                return self._same_prompt_result(draft, str(error))
            return None

    def _resolve_work(
        self,
        outcome: TurnResult | _SetupWork,
        explicit_time: datetime | None,
    ) -> TurnResult:
        if isinstance(outcome, _DiscoveryWork):
            try:
                projects = outcome.context.discover_projects(
                    outcome.context.working_directory
                )
                if not isinstance(projects, tuple) or any(
                    not isinstance(project, TaskProject) for project in projects
                ):
                    raise ValueError("Project discovery returned invalid data")
                error = None
            except Exception:
                projects = ()
                error = "Project discovery failed."
            completion_time = explicit_time or self._clock()
            with self._lock:
                draft = self._matching_work_draft(outcome, completion_time)
                if draft is None:
                    return TurnResult.handled(
                        "The Project discovery result was stale and was not applied."
                    )
                if error is not None or not projects:
                    prefix = error or "No Projects were found in the configured workspace roots."
                    refreshed = self._refresh(
                        draft,
                        completion_time,
                        project_candidates=(),
                        projects=(),
                        project_probe=None,
                        project_remote_name=None,
                        management_repository=outcome.context.management_repository,
                        task_owner_host=outcome.context.task_owner_host,
                        task_flow=None,
                        merge_mode=None,
                        merge_order=None,
                        task_text=None,
                        task_request=None,
                        v2_request_id=None,
                        task_request_v2=None,
                        operation_token=None,
                    )
                    self._store_draft(outcome.key, refreshed, completion_time)
                    return self._projects_prompt(refreshed, prefix)
                refreshed = self._refresh(
                    draft,
                    completion_time,
                    project_candidates=projects,
                    projects=(),
                    project_probe=None,
                    project_remote_name=None,
                    task_flow=None,
                    merge_mode=None,
                    merge_order=None,
                    management_repository=outcome.context.management_repository,
                    task_owner_host=outcome.context.task_owner_host,
                    task_text=None,
                    task_request=None,
                    v2_request_id=None,
                    task_request_v2=None,
                    operation_token=None,
                )
                self._store_draft(outcome.key, refreshed, completion_time)
                return self._projects_prompt(refreshed, outcome.prefix)

        if isinstance(outcome, _ProjectProbeWork):
            callback = outcome.context.probe_project_path
            try:
                if callback is None:
                    raise ValueError("Direct Project probing is unavailable")
                probe = callback(outcome.raw_path)
                if not isinstance(probe, ProjectPathProbe):
                    raise ValueError("Project path probe returned invalid data")
                error = None
            except Exception:
                probe = None
                error = "Project path could not be verified."
            completion_time = explicit_time or self._clock()
            with self._lock:
                draft = self._matching_work_draft(outcome, completion_time)
                if draft is None:
                    return TurnResult.handled(
                        "The Project path result was stale and was not applied."
                    )
                if error is not None or probe is None:
                    refreshed = self._refresh(
                        draft,
                        completion_time,
                        step=SetupStep.PROJECT_PATH,
                        project_probe=None,
                        project_remote_name=None,
                        operation_token=None,
                    )
                    self._store_draft(outcome.key, refreshed, completion_time)
                    return self._project_path_prompt(refreshed, error)
                refreshed = self._refresh(
                    draft,
                    completion_time,
                    step=SetupStep.PROJECT_REMOTE,
                    project_probe=probe,
                    project_remote_name=None,
                    operation_token=None,
                )
                self._store_draft(outcome.key, refreshed, completion_time)
                return self._project_remote_prompt(refreshed)

        if isinstance(outcome, _ProjectBindWork):
            callback = outcome.context.bind_project
            try:
                if callback is None or outcome.probe is None:
                    raise ValueError("Direct Project binding is unavailable")
                project = callback(
                    outcome.probe,
                    outcome.remote_name,
                    outcome.branch,
                )
                if not isinstance(project, TaskProject):
                    raise ValueError("Project binding returned invalid data")
                error = None
            except Exception:
                project = None
                error = "Project remote and branch could not be verified."
            completion_time = explicit_time or self._clock()
            with self._lock:
                draft = self._matching_work_draft(outcome, completion_time)
                if draft is None:
                    return TurnResult.handled(
                        "The Project binding result was stale and was not applied."
                    )
                if error is not None or project is None:
                    refreshed = self._refresh(
                        draft,
                        completion_time,
                        step=outcome.return_step,
                        operation_token=None,
                    )
                    self._store_draft(outcome.key, refreshed, completion_time)
                    if outcome.return_step is SetupStep.PROJECT_BRANCH_NAME:
                        return self._project_branch_name_prompt(refreshed, error)
                    return self._project_branch_prompt(refreshed, error)
                if len(draft.project_candidates) >= HARD_MAX_PROJECTS:
                    refreshed = self._refresh(
                        draft,
                        completion_time,
                        step=SetupStep.PROJECTS,
                        project_probe=None,
                        project_remote_name=None,
                        operation_token=None,
                    )
                    self._store_draft(outcome.key, refreshed, completion_time)
                    return self._projects_prompt(
                        refreshed,
                        "The Project limit was reached; the new Project was not added.",
                    )
                duplicate = any(
                    candidate.project_id == project.project_id
                    or candidate.repository.casefold() == project.repository.casefold()
                    or Path(candidate.workspace) == Path(project.workspace)
                    for candidate in draft.project_candidates
                )
                if duplicate:
                    refreshed = self._refresh(
                        draft,
                        completion_time,
                        step=SetupStep.PROJECTS,
                        project_probe=None,
                        project_remote_name=None,
                        operation_token=None,
                    )
                    self._store_draft(outcome.key, refreshed, completion_time)
                    return self._projects_prompt(
                        refreshed,
                        "That Project is already listed.",
                    )
                refreshed = self._refresh(
                    draft,
                    completion_time,
                    step=SetupStep.PROJECTS,
                    project_candidates=(*draft.project_candidates, project),
                    projects=(),
                    project_probe=None,
                    project_remote_name=None,
                    task_flow=None,
                    merge_mode=None,
                    merge_order=None,
                    task_text=None,
                    task_request=None,
                    v2_request_id=None,
                    task_request_v2=None,
                    operation_token=None,
                )
                self._store_draft(outcome.key, refreshed, completion_time)
                return self._projects_prompt(
                    refreshed,
                    "Project added. Choose one or more Projects.",
                )

        if isinstance(outcome, _ValidationWork):
            try:
                validated = outcome.context.validate_projects(outcome.projects)
                valid = validated == outcome.projects
            except Exception:
                valid = False
            completion_time = explicit_time or self._clock()
            with self._lock:
                draft = self._matching_work_draft(outcome, completion_time)
                if draft is None:
                    return TurnResult.handled(
                        "The Project validation result was stale and was not applied."
                    )
                if not valid:
                    reset = self._refresh(
                        draft,
                        completion_time,
                        step=SetupStep.PROJECTS,
                        project_candidates=(),
                        projects=(),
                        project_probe=None,
                        project_remote_name=None,
                        task_flow=None,
                        merge_mode=None,
                        merge_order=None,
                        task_text=None,
                        task_request=None,
                        v2_request_id=None,
                        task_request_v2=None,
                        operation_token=None,
                    )
                    self._store_draft(outcome.key, reset, completion_time)
                    return self._projects_prompt(
                        reset,
                        "Project validation failed. Discover Projects again before confirming.",
                    )
                prepared = update(draft, operation_token=None)
                self._store_draft(outcome.key, prepared, completion_time)
                return self._prepared_v2_result(prepared)

        return outcome

    def _matching_work_draft(
        self,
        work: _SetupWork,
        now: datetime,
    ) -> SetupDraft | None:
        draft = self._drafts.get(work.key)
        if draft is None or now >= draft.expires_at:
            return None
        prompt_id = (
            None
            if draft.choice_prompt is None
            else draft.choice_prompt.choice_prompt_id
        )
        selected_ids = tuple(project.project_id for project in draft.projects)
        if (
            draft.operation_token != work.token
            or draft.generation != work.generation
            or draft.step is not work.step
            or draft.expires_at != work.expires_at
            or prompt_id != work.prompt_id
            or selected_ids != work.selected_ids
        ):
            return None
        return draft

    def _handle_locked(
        self,
        key: SessionKey,
        text: str,
        current_time: datetime,
        repository: str | None,
        context: TaskSetupContext | None,
    ) -> TurnResult | _SetupWork:
        choice = text.strip()

        if choice == "/task":
            return self._start_task(key, current_time, context)
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
                if context is not None:
                    return self._begin_project_discovery(
                        key,
                        draft,
                        current_time,
                        context,
                    )
            else:
                draft = self._new_mode_draft(text, current_time)
                self._store_draft(key, draft, current_time)
                return self._mode_prompt(draft)

        if draft.step is SetupStep.MODE:
            return self._handle_mode(key, draft, choice, current_time, context)
        if draft.step is SetupStep.PROJECTS:
            selected = tuple(
                item.strip() for item in choice.split(",") if item.strip()
            )
            return self._handle_projects(
                key,
                draft,
                selected,
                current_time,
                context,
            )
        if draft.step is SetupStep.PROJECT_PATH:
            return self._handle_project_path(
                key,
                draft,
                text,
                current_time,
                context,
            )
        if draft.step is SetupStep.PROJECT_REMOTE:
            return self._handle_project_remote(
                key,
                draft,
                choice,
                current_time,
            )
        if draft.step is SetupStep.PROJECT_BRANCH:
            return self._handle_project_branch(
                key,
                draft,
                choice,
                current_time,
                context,
            )
        if draft.step is SetupStep.PROJECT_BRANCH_NAME:
            return self._handle_project_branch_name(
                key,
                draft,
                text,
                current_time,
                context,
            )
        if draft.step is SetupStep.TASK_FLOW:
            return self._handle_task_flow(key, draft, choice, current_time)
        if draft.step is SetupStep.MERGE_MODE:
            return self._handle_merge_mode(
                key, draft, choice, current_time, repository
            )
        if draft.step is SetupStep.MERGE_ORDER:
            return self._handle_merge_order(
                key,
                draft,
                choice,
                current_time,
                repository,
            )
        if draft.step is SetupStep.TASK_CONTENT:
            return self._handle_task_content(
                key, draft, text, current_time, repository
            )
        return self._handle_confirm(key, draft, choice, current_time, context)

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

    def recover_in_chat(
        self,
        session_id: str,
        user_id: str,
        *,
        fallback_text: str,
        surface: str = DEFAULT_SURFACE,
        now: datetime | None = None,
    ) -> TurnResult:
        """Enter Chat and replay the stashed first input exactly once."""

        if not isinstance(fallback_text, str):
            raise ValueError("fallback_text must be a string")
        current_time = now or self._clock()
        key = (surface, session_id, user_id)
        with self._lock:
            self._sweep(current_time)
            draft = self._drafts.get(key)
            had_draft = draft is not None
            stashed_input = None if draft is None else draft.first_input
            self._store_chat(key, current_time)
            if stashed_input is not None:
                return TurnResult.replace(stashed_input)
            if not had_draft:
                return TurnResult.replace(fallback_text)
            return TurnResult.handled(
                "Task setup cancelled. Continuing in Chat."
            )

    def _enter_chat(self, key: SessionKey, now: datetime) -> TurnResult:
        self._drafts.pop(key, None)
        self._store_chat(key, now)
        return TurnResult.handled("Task setup cancelled. Continuing in Chat.")

    def _start_task(
        self,
        key: SessionKey,
        now: datetime,
        context: TaskSetupContext | None,
    ) -> TurnResult | _DiscoveryWork:
        self._chat_sessions.discard(key)
        draft = self._new_task_draft(now)
        self._store_draft(key, draft, now)
        if context is not None:
            return self._begin_project_discovery(key, draft, now, context)
        return self._task_flow_prompt(draft)

    def _new_task_draft(self, now: datetime) -> SetupDraft:
        expires_at = self._deadline(now)
        draft = SetupDraft(
            step=SetupStep.TASK_FLOW,
            first_input=None,
            project_candidates=(),
            projects=(),
            project_probe=None,
            project_remote_name=None,
            task_flow=None,
            merge_mode=None,
            merge_order=None,
            management_repository=None,
            task_owner_host=None,
            task_text=None,
            task_request=None,
            v2_request_id=None,
            task_request_v2=None,
            expires_at=expires_at,
            choice_prompt=None,
            generation=0,
            operation_token=None,
        )
        return update(draft, choice_prompt=self._choice_prompt(draft, expires_at))

    def _new_mode_draft(self, first_input: str, now: datetime) -> SetupDraft:
        expires_at = self._deadline(now)
        draft = SetupDraft(
            step=SetupStep.MODE,
            first_input=first_input,
            project_candidates=(),
            projects=(),
            project_probe=None,
            project_remote_name=None,
            task_flow=None,
            merge_mode=None,
            merge_order=None,
            management_repository=None,
            task_owner_host=None,
            task_text=None,
            task_request=None,
            v2_request_id=None,
            task_request_v2=None,
            expires_at=expires_at,
            choice_prompt=None,
            generation=0,
            operation_token=None,
        )
        return update(draft, choice_prompt=self._choice_prompt(draft, expires_at))

    @staticmethod
    def _deadline(now: datetime) -> datetime:
        return now + SETUP_TIMEOUT

    def _refresh(self, draft: SetupDraft, now: datetime, **changes: object) -> SetupDraft:
        expires_at = self._deadline(now)
        step = changes.get("step", draft.step)
        refreshed = update(
            draft,
            expires_at=expires_at,
            choice_prompt=None,
            generation=draft.generation + 1,
            **changes,
        )
        choice_prompt = (
            self._choice_prompt(refreshed, expires_at)
            if isinstance(step, SetupStep) and self._step_has_choices(step)
            else None
        )
        return update(refreshed, choice_prompt=choice_prompt)

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
        context: TaskSetupContext | None,
    ) -> TurnResult | _DiscoveryWork:
        if choice == Mode.CHAT.value:
            self._store_chat(key, now)
            return TurnResult.replace(draft.first_input or "")
        if choice == Mode.TASK.value:
            if context is not None:
                return self._begin_project_discovery(key, draft, now, context)
            task_draft = self._refresh(
                draft,
                now,
                step=SetupStep.TASK_FLOW,
                project_candidates=(),
                projects=(),
                project_probe=None,
                project_remote_name=None,
                task_flow=None,
                merge_mode=None,
                merge_order=None,
                management_repository=None,
                task_owner_host=None,
                task_text=None,
                task_request=None,
                v2_request_id=None,
                task_request_v2=None,
            )
            self._store_draft(key, task_draft, now)
            return self._task_flow_prompt(task_draft)

        refreshed = self._refresh(draft, now)
        self._store_draft(key, refreshed, now)
        return self._mode_prompt(refreshed, "Choose either chat or task.")

    def _begin_project_discovery(
        self,
        key: SessionKey,
        draft: SetupDraft,
        now: datetime,
        context: TaskSetupContext,
        prefix: str | None = None,
    ) -> _DiscoveryWork:
        token = str(uuid4())
        pending = self._refresh(
            draft,
            now,
            step=SetupStep.PROJECTS,
            project_candidates=(),
            projects=(),
            project_probe=None,
            project_remote_name=None,
            task_flow=None,
            merge_mode=None,
            merge_order=None,
            management_repository=None,
            task_owner_host=None,
            task_text=None,
            task_request=None,
            v2_request_id=None,
            task_request_v2=None,
            operation_token=token,
        )
        pending = update(pending, choice_prompt=None)
        self._store_draft(key, pending, now)
        return _DiscoveryWork(
            key=key,
            context=context,
            token=token,
            generation=pending.generation,
            step=pending.step,
            expires_at=pending.expires_at,
            prompt_id=None,
            selected_ids=(),
            prefix=prefix,
        )

    def _handle_projects(
        self,
        key: SessionKey,
        draft: SetupDraft,
        selected_ids: tuple[str, ...],
        now: datetime,
        context: TaskSetupContext | None,
    ) -> TurnResult | _DiscoveryWork:
        if draft.operation_token is not None:
            return TurnResult.handled("Project discovery is already in progress.")
        if selected_ids == ("add_project",):
            if len(draft.project_candidates) >= HARD_MAX_PROJECTS:
                return self._projects_prompt(
                    draft,
                    "The Project limit has been reached.",
                )
            if not self._direct_context_matches(draft, context):
                return self._projects_prompt(
                    draft,
                    "Trusted direct Project configuration is unavailable.",
                )
            path_draft = self._refresh(
                draft,
                now,
                step=SetupStep.PROJECT_PATH,
                projects=(),
                project_probe=None,
                project_remote_name=None,
                task_flow=None,
                merge_mode=None,
                merge_order=None,
                task_text=None,
                task_request=None,
                v2_request_id=None,
                task_request_v2=None,
                operation_token=None,
            )
            self._store_draft(key, path_draft, now)
            return self._project_path_prompt(path_draft)
        if not draft.project_candidates:
            if selected_ids == ("cancel",):
                return self._enter_chat(key, now)
            if selected_ids == ("retry",):
                if context is None:
                    return self._projects_prompt(
                        draft,
                        "Trusted Project configuration is unavailable.",
                    )
                return self._begin_project_discovery(key, draft, now, context)
            return self._projects_prompt(
                draft,
                "Choose Retry, Add Project, or Cancel.",
            )
        if "add_project" in selected_ids:
            return self._projects_prompt(
                draft,
                "Add Project must be selected by itself.",
            )
        allowed = {project.project_id: project for project in draft.project_candidates}
        if (
            not selected_ids
            or len(set(selected_ids)) != len(selected_ids)
            or any(project_id not in allowed for project_id in selected_ids)
        ):
            return self._projects_prompt(
                draft,
                "Choose one or more listed Projects by stable ID.",
            )
        selected_set = set(selected_ids)
        projects = tuple(
            project
            for project in draft.project_candidates
            if project.project_id in selected_set
        )
        repositories = [project.repository.casefold() for project in projects]
        if len(repositories) != len(set(repositories)):
            return self._projects_prompt(
                draft,
                "The same repository cannot be selected through multiple remotes.",
            )
        selected = self._refresh(
            draft,
            now,
            step=SetupStep.TASK_FLOW,
            projects=projects,
            task_flow=None,
            merge_mode=None,
            merge_order=None,
            task_text=None,
            task_request=None,
            v2_request_id=None,
            task_request_v2=None,
            operation_token=None,
        )
        self._store_draft(key, selected, now)
        return self._task_flow_prompt(selected)

    @staticmethod
    def _direct_context_matches(
        draft: SetupDraft,
        context: TaskSetupContext | None,
    ) -> bool:
        return (
            context is not None
            and context.management_repository == draft.management_repository
            and context.task_owner_host == draft.task_owner_host
            and context.probe_project_path is not None
            and context.bind_project is not None
        )

    def _handle_project_path(
        self,
        key: SessionKey,
        draft: SetupDraft,
        raw_path: str,
        now: datetime,
        context: TaskSetupContext | None,
    ) -> TurnResult | _ProjectProbeWork:
        if draft.operation_token is not None:
            return self._project_path_prompt(
                draft,
                "Project path verification is already in progress.",
            )
        if not raw_path.strip():
            return self._project_path_prompt(draft, "Project path cannot be empty.")
        if not self._direct_context_matches(draft, context):
            return self._project_path_prompt(
                draft,
                "Trusted direct Project configuration is unavailable.",
            )
        token = str(uuid4())
        pending = self._refresh(
            draft,
            now,
            operation_token=token,
        )
        self._store_draft(key, pending, now)
        return _ProjectProbeWork(
            key=key,
            context=context,
            token=token,
            generation=pending.generation,
            step=pending.step,
            expires_at=pending.expires_at,
            prompt_id=None,
            selected_ids=tuple(project.project_id for project in pending.projects),
            raw_path=raw_path.strip(),
        )

    def _handle_project_remote(
        self,
        key: SessionKey,
        draft: SetupDraft,
        remote_name: str,
        now: datetime,
    ) -> TurnResult:
        probe = draft.project_probe
        if probe is None:
            raise RuntimeError("Project path probe is missing")
        allowed = {remote.remote_name for remote in probe.remotes}
        if remote_name not in allowed:
            refreshed = self._refresh(draft, now)
            self._store_draft(key, refreshed, now)
            return self._project_remote_prompt(
                refreshed,
                "Choose one listed remote.",
            )
        selected = self._refresh(
            draft,
            now,
            step=SetupStep.PROJECT_BRANCH,
            project_remote_name=remote_name,
            operation_token=None,
        )
        self._store_draft(key, selected, now)
        return self._project_branch_prompt(selected)

    def _handle_project_branch(
        self,
        key: SessionKey,
        draft: SetupDraft,
        choice: str,
        now: datetime,
        context: TaskSetupContext | None,
    ) -> TurnResult | _ProjectBindWork:
        if draft.operation_token is not None:
            return TurnResult.handled(
                "Project binding is already in progress.",
                next_step=SetupStep.PROJECT_BRANCH,
            )
        if choice == "other_branch":
            selected = self._refresh(
                draft,
                now,
                step=SetupStep.PROJECT_BRANCH_NAME,
                operation_token=None,
            )
            self._store_draft(key, selected, now)
            return self._project_branch_name_prompt(selected)
        if choice != "default_branch":
            refreshed = self._refresh(draft, now)
            self._store_draft(key, refreshed, now)
            return self._project_branch_prompt(
                refreshed,
                "Choose the default branch or another branch.",
            )
        remote = self._selected_project_remote(draft)
        return self._begin_project_bind(
            key,
            draft,
            remote.default_branch,
            SetupStep.PROJECT_BRANCH,
            now,
            context,
        )

    def _handle_project_branch_name(
        self,
        key: SessionKey,
        draft: SetupDraft,
        branch: str,
        now: datetime,
        context: TaskSetupContext | None,
    ) -> TurnResult | _ProjectBindWork:
        if draft.operation_token is not None:
            return self._project_branch_name_prompt(
                draft,
                "Project binding is already in progress.",
            )
        if not branch.strip():
            return self._project_branch_name_prompt(
                draft,
                "Branch name cannot be empty.",
            )
        return self._begin_project_bind(
            key,
            draft,
            branch.strip(),
            SetupStep.PROJECT_BRANCH_NAME,
            now,
            context,
        )

    @staticmethod
    def _selected_project_remote(draft: SetupDraft):
        if draft.project_probe is None or draft.project_remote_name is None:
            raise RuntimeError("Project remote selection is missing")
        matches = [
            remote
            for remote in draft.project_probe.remotes
            if remote.remote_name == draft.project_remote_name
        ]
        if len(matches) != 1:
            raise RuntimeError("Project remote selection is invalid")
        return matches[0]

    def _begin_project_bind(
        self,
        key: SessionKey,
        draft: SetupDraft,
        branch: str,
        return_step: SetupStep,
        now: datetime,
        context: TaskSetupContext | None,
    ) -> TurnResult | _ProjectBindWork:
        if not self._direct_context_matches(draft, context):
            if return_step is SetupStep.PROJECT_BRANCH_NAME:
                return self._project_branch_name_prompt(
                    draft,
                    "Trusted direct Project configuration is unavailable.",
                )
            return self._project_branch_prompt(
                draft,
                "Trusted direct Project configuration is unavailable.",
            )
        remote = self._selected_project_remote(draft)
        token = str(uuid4())
        pending = self._refresh(draft, now, operation_token=token)
        pending = update(pending, choice_prompt=None)
        self._store_draft(key, pending, now)
        return _ProjectBindWork(
            key=key,
            context=context,
            token=token,
            generation=pending.generation,
            step=pending.step,
            expires_at=pending.expires_at,
            prompt_id=None,
            selected_ids=tuple(project.project_id for project in pending.projects),
            probe=pending.project_probe,
            remote_name=remote.remote_name,
            branch=branch,
            return_step=return_step,
        )

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
            merge_order=None,
            task_text=None,
            task_request=None,
            v2_request_id=None,
            task_request_v2=None,
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
            merge_order=None,
            task_request=None,
            v2_request_id=None,
            task_request_v2=None,
        )
        if merge_mode is MergeMode.FULL_AUTO and len(selected.projects) > 1:
            ordered = self._refresh(
                selected,
                now,
                step=SetupStep.MERGE_ORDER,
                merge_order=(),
            )
            self._store_draft(key, ordered, now)
            return self._merge_order_prompt(ordered)
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

    def _handle_merge_order(
        self,
        key: SessionKey,
        draft: SetupDraft,
        choice: str,
        now: datetime,
        repository: str | None,
    ) -> TurnResult:
        if draft.merge_order is None:
            raise RuntimeError("Merge order state is missing")
        project_ids = {project.project_id for project in draft.projects}
        if choice not in project_ids or choice in draft.merge_order:
            return self._merge_order_prompt(
                draft,
                "Choose one remaining Project by stable ID.",
            )
        merge_order = (*draft.merge_order, choice)
        if len(merge_order) < len(draft.projects):
            selected = self._refresh(draft, now, merge_order=merge_order)
            self._store_draft(key, selected, now)
            return self._merge_order_prompt(selected)
        selected = update(draft, merge_order=merge_order)
        if selected.first_input is None:
            content = self._refresh(
                selected,
                now,
                step=SetupStep.TASK_CONTENT,
            )
            self._store_draft(key, content, now)
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
        if draft.projects:
            if draft.management_repository is None or draft.task_owner_host is None:
                raise RuntimeError("Trusted v2 Task configuration is missing")
            preview = self._refresh(
                draft,
                now,
                step=SetupStep.CONFIRM,
                task_text=task_text,
                task_request=None,
                v2_request_id=self._new_request_id(),
                task_request_v2=None,
            )
            self._store_draft(key, preview, now)
            return self._preview_prompt(preview)
        if not isinstance(repository, str) or not repository.strip():
            raise RuntimeError("INFINITY_FORGE_REPOSITORY is required")

        request = TaskCreationRequest(
            request_id=self._new_request_id(),
            repository=repository,
            content=self._task_content(task_text),
            task_flow=draft.task_flow,
            merge_mode=draft.merge_mode,
            confirmed_by=key[2],
            confirmed_at=self._normalized_utc(now),
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
            v2_request_id=None,
            task_request_v2=None,
        )
        self._store_draft(key, preview, now)
        return self._preview_prompt(preview)

    def _handle_confirm(
        self,
        key: SessionKey,
        draft: SetupDraft,
        choice: str,
        now: datetime,
        context: TaskSetupContext | None,
    ) -> TurnResult | _ValidationWork:
        if choice == "cancel":
            return self._enter_chat(key, now)
        if draft.operation_token is not None:
            return self._preview_prompt(
                draft,
                "Project validation is already in progress.",
            )
        if choice != "confirm":
            refreshed = self._refresh(draft, now)
            self._store_draft(key, refreshed, now)
            return self._preview_prompt(
                refreshed,
                "Choose confirm to create this Task or cancel to discard it.",
            )
        if draft.task_text is None:
            raise RuntimeError("Task content is missing")
        if draft.v2_request_id is not None:
            if (
                context is None
                or context.management_repository != draft.management_repository
                or context.task_owner_host != draft.task_owner_host
            ):
                reset = self._refresh(
                    draft,
                    now,
                    step=SetupStep.PROJECTS,
                    project_candidates=(),
                    projects=(),
                    project_probe=None,
                    project_remote_name=None,
                    task_flow=None,
                    merge_mode=None,
                    merge_order=None,
                    task_text=None,
                    task_request=None,
                    v2_request_id=None,
                    task_request_v2=None,
                    operation_token=None,
                )
                self._store_draft(key, reset, now)
                return self._projects_prompt(
                    reset,
                    "Trusted Task configuration changed. Discover Projects again.",
                )
            token = str(uuid4())
            if draft.task_flow is None or draft.merge_mode is None:
                raise RuntimeError("Task choices are incomplete")
            request_v2 = TaskRequestV2.create(
                request_id=draft.v2_request_id,
                management_repository=draft.management_repository,
                task_content=self._task_content(draft.task_text),
                task_flow=draft.task_flow,
                merge_mode=draft.merge_mode,
                merge_order=draft.merge_order,
                projects=draft.projects,
                task_owner_host=draft.task_owner_host,
                confirmed_by=key[2],
                confirmed_at=self._normalized_utc(now),
            )
            pending = update(
                draft,
                task_request_v2=request_v2,
                operation_token=token,
            )
            self._store_draft(key, pending, now)
            prompt_id = (
                None
                if pending.choice_prompt is None
                else pending.choice_prompt.choice_prompt_id
            )
            return _ValidationWork(
                key=key,
                context=context,
                token=token,
                generation=pending.generation,
                step=pending.step,
                expires_at=pending.expires_at,
                prompt_id=prompt_id,
                selected_ids=tuple(
                    project.project_id for project in pending.projects
                ),
                projects=pending.projects,
            )
        if draft.task_request is None:
            raise RuntimeError("Task request is missing")
        return self._finish_task(key, draft, draft.task_text, draft.task_request, now)

    def _prepared_v2_result(self, draft: SetupDraft) -> TurnResult:
        if (
            draft.task_flow is None
            or draft.merge_mode is None
            or draft.task_text is None
            or draft.task_request_v2 is None
            or draft.choice_prompt is None
        ):
            raise RuntimeError("Prepared v2 Task data is incomplete")
        selection = TaskSelection(Mode.TASK, draft.task_flow, draft.merge_mode)
        return TurnResult.handled(
            "Task validated, but v2 Task creation is not enabled yet.",
            choice_prompt=draft.choice_prompt,
            next_step=SetupStep.CONFIRM,
            selection=selection,
            task_text=draft.task_text,
            task_request_v2=draft.task_request_v2,
            choice_prompt_paused=True,
        )

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

    def _handle_selected_choices(
        self,
        key: SessionKey,
        draft: SetupDraft,
        choice_ids: tuple[str, ...],
        now: datetime,
        repository: str | None,
        context: TaskSetupContext | None,
    ) -> TurnResult | _SetupWork:
        if draft.step is SetupStep.PROJECTS:
            return self._handle_projects(key, draft, choice_ids, now, context)
        if len(choice_ids) != 1:
            return self._same_prompt_result(
                draft,
                "this step requires exactly one choice",
            )
        choice_id = choice_ids[0]
        if draft.step is SetupStep.MODE:
            return self._handle_mode(key, draft, choice_id, now, context)
        if draft.step is SetupStep.PROJECT_REMOTE:
            return self._handle_project_remote(key, draft, choice_id, now)
        if draft.step is SetupStep.PROJECT_BRANCH:
            return self._handle_project_branch(
                key,
                draft,
                choice_id,
                now,
                context,
            )
        if draft.step is SetupStep.TASK_FLOW:
            return self._handle_task_flow(key, draft, choice_id, now)
        if draft.step is SetupStep.MERGE_MODE:
            return self._handle_merge_mode(key, draft, choice_id, now, repository)
        if draft.step is SetupStep.MERGE_ORDER:
            return self._handle_merge_order(
                key,
                draft,
                choice_id,
                now,
                repository,
            )
        if draft.step is SetupStep.CONFIRM:
            return self._handle_confirm(key, draft, choice_id, now, context)
        return TurnResult.handled("This Task step requires text input.")

    def _same_prompt_result(self, draft: SetupDraft, error: str) -> TurnResult:
        prefix = f"Choice was not applied: {error}."
        if draft.step is SetupStep.MODE:
            return self._mode_prompt(draft, prefix)
        if draft.step is SetupStep.PROJECTS:
            return self._projects_prompt(draft, prefix)
        if draft.step is SetupStep.PROJECT_REMOTE:
            return self._project_remote_prompt(draft, prefix)
        if draft.step is SetupStep.PROJECT_BRANCH:
            return self._project_branch_prompt(draft, prefix)
        if draft.step is SetupStep.TASK_FLOW:
            return self._task_flow_prompt(draft, prefix)
        if draft.step is SetupStep.MERGE_MODE:
            return self._merge_mode_prompt(draft, prefix)
        if draft.step is SetupStep.MERGE_ORDER:
            return self._merge_order_prompt(draft, prefix)
        if draft.step is SetupStep.CONFIRM:
            return self._preview_prompt(draft, prefix)
        return TurnResult.handled(prefix)

    @staticmethod
    def _step_has_choices(step: SetupStep) -> bool:
        return step in {
            SetupStep.MODE,
            SetupStep.PROJECTS,
            SetupStep.PROJECT_REMOTE,
            SetupStep.PROJECT_BRANCH,
            SetupStep.TASK_FLOW,
            SetupStep.MERGE_MODE,
            SetupStep.MERGE_ORDER,
            SetupStep.CONFIRM,
        }

    @staticmethod
    def _choice_prompt(draft: SetupDraft, expires_at: datetime) -> ChoicePrompt:
        step = draft.step
        if step is SetupStep.MODE:
            choices = tuple(
                Choice(value.value, _MODE_LABELS[value], _MODE_DETAILS[value])
                for value in Mode
            )
            submit_label = "Choose mode"
            choice_mode = ChoiceMode.SINGLE
            max_choices = 1
        elif step is SetupStep.PROJECTS:
            if draft.project_candidates:
                project_choices = tuple(
                    Choice(
                        project.project_id,
                        (
                            f"{_safe_display_text(project.repository)} "
                            f"({_safe_display_text(project.remote_name)}, "
                            f"{project.project_id[:8]})"
                        ),
                        (
                            f"{_safe_display_text(project.workspace)} — "
                            f"{_safe_display_text(project.base_branch)} "
                            f"at {project.base_commit[:12]}"
                        ),
                    )
                    for project in draft.project_candidates
                )
                choices = project_choices + (
                    (
                        Choice(
                            "add_project",
                            "Add Project",
                            "Add an allowed absolute or working-directory-relative path",
                        ),
                    )
                    if len(project_choices) < HARD_MAX_PROJECTS
                    else ()
                )
                submit_label = "Choose Projects"
                choice_mode = ChoiceMode.MULTIPLE
                max_choices = None
            else:
                choices = (
                    Choice("retry", "Retry", "Discover Projects again"),
                    Choice(
                        "add_project",
                        "Add Project",
                        "Add an allowed absolute or working-directory-relative path",
                    ),
                    Choice("cancel", "Cancel", "Return to Chat"),
                )
                submit_label = "Project discovery"
                choice_mode = ChoiceMode.SINGLE
                max_choices = 1
        elif step is SetupStep.PROJECT_REMOTE:
            if draft.project_probe is None:
                raise RuntimeError("Project path probe is missing")
            choices = tuple(
                Choice(
                    remote.remote_name,
                    (
                        f"{_safe_display_text(remote.repository)} "
                        f"({_safe_display_text(remote.remote_name)})"
                    ),
                    f"Default branch: {_safe_display_text(remote.default_branch)}",
                )
                for remote in draft.project_probe.remotes
            )
            submit_label = "Choose remote"
            choice_mode = ChoiceMode.SINGLE
            max_choices = 1
        elif step is SetupStep.PROJECT_BRANCH:
            remote = TaskSetup._selected_project_remote(draft)
            choices = (
                Choice(
                    "default_branch",
                    f"Default: {_safe_display_text(remote.default_branch)}",
                    "Use the repository default branch",
                ),
                Choice(
                    "other_branch",
                    "Another branch",
                    "Enter a different existing branch",
                ),
            )
            submit_label = "Choose branch"
            choice_mode = ChoiceMode.SINGLE
            max_choices = 1
        elif step is SetupStep.TASK_FLOW:
            choices = tuple(
                Choice(value.value, _FLOW_LABELS[value], _FLOW_DETAILS[value])
                for value in TaskFlow
            )
            submit_label = "Choose checks"
            choice_mode = ChoiceMode.SINGLE
            max_choices = 1
        elif step is SetupStep.MERGE_MODE:
            choices = tuple(
                Choice(value.value, _MERGE_LABELS[value], _MERGE_DETAILS[value])
                for value in MergeMode
            )
            submit_label = "Choose merge mode"
            choice_mode = ChoiceMode.SINGLE
            max_choices = 1
        elif step is SetupStep.MERGE_ORDER:
            completed = set(draft.merge_order or ())
            remaining = tuple(
                project
                for project in draft.projects
                if project.project_id not in completed
            )
            if not remaining:
                raise RuntimeError("Merge order has no remaining Project")
            choices = tuple(
                Choice(
                    project.project_id,
                    (
                        f"{_safe_display_text(project.repository)} "
                        f"({_safe_display_text(project.remote_name)}, "
                        f"{project.project_id[:8]})"
                    ),
                    (
                        f"Merge rank {len(completed) + 1}: "
                        f"{_safe_display_text(project.workspace)}"
                    ),
                )
                for project in remaining
            )
            submit_label = f"Choose merge rank {len(completed) + 1}"
            choice_mode = ChoiceMode.SINGLE
            max_choices = 1
        elif step is SetupStep.CONFIRM:
            choices = (
                Choice("confirm", "Confirm Task", "Create the reviewed Task"),
                Choice("cancel", "Cancel", "Discard the reviewed Task"),
            )
            submit_label = "Confirm Task"
            choice_mode = ChoiceMode.SINGLE
            max_choices = 1
        else:
            raise RuntimeError("Task step does not have choices")
        return ChoicePrompt(
            choice_prompt_id=str(uuid4()),
            choice_mode=choice_mode,
            min_choices=1,
            max_choices=max_choices,
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
    def _projects_prompt(
        draft: SetupDraft,
        prefix: str | None = None,
    ) -> TurnResult:
        if draft.choice_prompt is None:
            raise RuntimeError("Project choice prompt is missing")
        if draft.project_candidates:
            intro = prefix or (
                "Choose one or more Projects. Use the arrow keys and Space "
                "to select, then submit."
            )
            options = "\n".join(
                (
                    f"- {_safe_display_text(project.repository)} "
                    f"({_safe_display_text(project.remote_name)}) — "
                    f"{_safe_display_text(project.workspace)}"
                )
                for project in draft.project_candidates
            )
            if len(draft.project_candidates) < HARD_MAX_PROJECTS:
                options += "\n- Add Project — enter another allowed Git root"
        else:
            intro = prefix or (
                "No Projects were found. Retry discovery, add a Project, or cancel."
            )
            options = (
                "- Retry — discover Projects again\n"
                "- Add Project — enter an allowed Git root\n"
                "- Cancel — return to Chat"
            )
        return TurnResult.handled(
            f"{intro}\n\nOptions:\n{options}",
            choices=TaskSetup._prompt_choice_ids(draft),
            choice_prompt=draft.choice_prompt,
            next_step=SetupStep.PROJECTS,
        )

    @staticmethod
    def _project_path_prompt(
        draft: SetupDraft,
        prefix: str | None = None,
    ) -> TurnResult:
        intro = prefix or (
            "Enter an allowed absolute Project path or a path relative to "
            "the Task working directory."
        )
        return TurnResult.handled(intro, next_step=SetupStep.PROJECT_PATH)

    @staticmethod
    def _project_remote_prompt(
        draft: SetupDraft,
        prefix: str | None = None,
    ) -> TurnResult:
        if draft.choice_prompt is None or draft.project_probe is None:
            raise RuntimeError("Project remote prompt is missing")
        intro = prefix or "Choose the Git remote to use for push and pull requests."
        options = "\n".join(
            (
                f"- {_safe_display_text(remote.repository)} "
                f"({_safe_display_text(remote.remote_name)})"
            )
            for remote in draft.project_probe.remotes
        )
        return TurnResult.handled(
            f"{intro}\n\nOptions:\n{options}",
            choices=TaskSetup._prompt_choice_ids(draft),
            choice_prompt=draft.choice_prompt,
            next_step=SetupStep.PROJECT_REMOTE,
        )

    @staticmethod
    def _project_branch_prompt(
        draft: SetupDraft,
        prefix: str | None = None,
    ) -> TurnResult:
        if draft.choice_prompt is None:
            raise RuntimeError("Project branch prompt is missing")
        remote = TaskSetup._selected_project_remote(draft)
        intro = prefix or "Choose the Project base branch."
        return TurnResult.handled(
            (
                f"{intro}\n\n"
                f"Default branch: {_safe_display_text(remote.default_branch)}\n"
                "You may choose Another branch and enter its name."
            ),
            choices=TaskSetup._prompt_choice_ids(draft),
            choice_prompt=draft.choice_prompt,
            next_step=SetupStep.PROJECT_BRANCH,
        )

    @staticmethod
    def _project_branch_name_prompt(
        draft: SetupDraft,
        prefix: str | None = None,
    ) -> TurnResult:
        intro = prefix or "Enter the existing non-default branch name."
        return TurnResult.handled(
            intro,
            next_step=SetupStep.PROJECT_BRANCH_NAME,
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
    def _merge_order_prompt(
        draft: SetupDraft,
        prefix: str | None = None,
    ) -> TurnResult:
        if draft.choice_prompt is None:
            raise RuntimeError("Merge order choice prompt is missing")
        rank = len(draft.merge_order or ()) + 1
        intro = prefix or f"Choose the Project to merge at rank {rank}."
        remaining_ids = {choice.id for choice in draft.choice_prompt.choices}
        options = "\n".join(
            f"- {_safe_display_text(project.repository)} "
            f"({_safe_display_text(project.remote_name)}) — "
            f"{_safe_display_text(project.workspace)}"
            for project in draft.projects
            if project.project_id in remaining_ids
        )
        return TurnResult.handled(
            f"{intro}\n\nOptions:\n{options}",
            choices=TaskSetup._prompt_choice_ids(draft),
            choice_prompt=draft.choice_prompt,
            next_step=SetupStep.MERGE_ORDER,
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
        if draft.v2_request_id is not None:
            request_v2 = draft.task_request_v2
            task_content = (
                request_v2.task_content
                if request_v2 is not None
                else TaskSetup._task_content(draft.task_text or "")
            )
            projects = (
                request_v2.projects
                if request_v2 is not None
                else draft.projects
            )
            merge_order = (
                request_v2.merge_order
                if request_v2 is not None
                else draft.merge_order
            )
            task_flow = (
                request_v2.task_flow
                if request_v2 is not None
                else draft.task_flow
            )
            merge_mode = (
                request_v2.merge_mode
                if request_v2 is not None
                else draft.merge_mode
            )
            if (
                draft.management_repository is None
                or draft.task_owner_host is None
                or task_flow is None
                or merge_mode is None
            ):
                raise RuntimeError("Task preview is incomplete")
            criteria = "\n".join(
                f"{number}. {criterion}"
                for number, criterion in enumerate(
                    task_content.acceptance_criteria,
                    start=1,
                )
            )
            order_ranks = {
                project_id: rank
                for rank, project_id in enumerate(
                    merge_order or (),
                    start=1,
                )
            }
            project_details = "\n".join(
                (
                    f"Project {number}: "
                    f"{_safe_display_text(project.repository)}\n"
                    f"Workspace: {_safe_display_text(project.workspace)}\n"
                    f"Remote: {_safe_display_text(project.remote_name)}\n"
                    f"Base branch: {_safe_display_text(project.base_branch)}\n"
                    f"Base commit: {project.base_commit}\n"
                    + (
                        ""
                        if project.project_id not in order_ranks
                        else f"Merge rank: {order_ranks[project.project_id]}\n"
                    )
                    + f"Project ID: {project.project_id}"
                )
                for number, project in enumerate(projects, start=1)
            )
            if request_v2 is None:
                expiry = (
                    "not granted"
                    if merge_mode is MergeMode.MANUAL
                    else "set from Confirm Task selection time"
                )
                confirmed_by = "set when Confirm Task is selected"
                confirmed_at = "set when Confirm Task is selected"
            else:
                expiry = (
                    "not granted"
                    if request_v2.auto_merge_expires_at is None
                    else TaskSetup._format_timestamp(
                        request_v2.auto_merge_expires_at
                    )
                )
                confirmed_by = request_v2.confirmed_by
                confirmed_at = TaskSetup._format_timestamp(
                    request_v2.confirmed_at
                )
            details = (
                f"Management: {draft.management_repository}\n"
                f"Task ID: {draft.v2_request_id}\n"
                f"Title: {task_content.title}\n"
                f"Description:\n{task_content.description}\n"
                f"Acceptance criteria:\n{criteria}\n"
                f"{project_details}\n"
                f"Checks selected: {_FLOW_LABELS[task_flow]}\n"
                f"Checks: {_FLOW_PATHS[task_flow]}\n"
                f"Merge choice: {_MERGE_LABELS[merge_mode]}\n"
                f"Merge result: {_MERGE_RESULTS[merge_mode]}\n"
                f"Automatic merge permission until: {expiry}\n"
                f"Task owner host: {draft.task_owner_host}\n"
                f"Confirmed by: {confirmed_by}\n"
                f"Confirmed at: {confirmed_at}"
            )
            text = f"{prefix}\n\n{details}" if prefix else details
            return TurnResult.handled(
                text,
                choices=TaskSetup._prompt_choice_ids(draft),
                choice_prompt=draft.choice_prompt,
                next_step=SetupStep.CONFIRM,
                task_request_v2=request_v2,
            )
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
