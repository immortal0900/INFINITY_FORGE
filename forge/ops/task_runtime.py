"""Rebuild and advance confirmed Task flows from durable external evidence."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from .contracts import (
    RunRecord,
    parse_step_proof,
    parse_task_result,
    source_result_hash,
)
from .displayed_status import displayed_label
from .github import (
    GitHubClient,
    GitHubTaskIssueClient,
    PullRequestWriteState,
    parse_pull_request_url,
)
from .hermes import (
    GateError,
    HermesCreateCommand,
    HermesStore,
    HermesTaskCard,
    ProjectTaskCardSpec,
    RootTaskCardSpec,
    build_create_argv,
    build_project_create_argv,
    build_root_create_argv,
    parse_project_task_card_key,
    parse_task_card_key,
    project_task_card_key,
    project_step_card_key,
    step_card_key,
    task_card_key,
)
from .task_flow import (
    TaskCardSpec,
    TaskFlowState,
    TaskFlowStatus,
    TaskStep,
    mark_task_step_running,
    next_task_action,
    observe_current_commit,
    record_fix_proof,
    record_task_result,
    start_task_flow,
)
from .task_outbox import TaskOutbox
from .task_database import TaskDatabase, TaskDatabaseError
from .task_projects import TaskProject
from .task_revisions import task_lifecycle_is_active
from .task_service import (
    TaskCreationRequest,
    TaskIssue,
    TaskServiceError,
    verify_task_issue_content,
)
from .task_settings import (
    BranchRefreshIntent,
    TaskSettings,
    TaskSettingsStore,
    task_content_hash,
)
from .task_settings_v2 import (
    TASK_REQUEST_V2_FORMAT,
    TASK_SETTINGS_V2_FORMAT,
    TaskRequestV2,
    TaskSettingsV2,
    TaskSettingsV2Error,
)
from .task_worktrees import TaskWorktree, TaskWorktreeError, TaskWorktreeManager


ROOT_CARD_FORMAT = "forge-task-card/v1"
STEP_CARD_FORMAT = "forge-step-card/v1"
PROJECT_CARD_FORMAT = "forge-project-card/v2"
_REPOSITORY_RE = re.compile(r"^[^/#:\s]+/[^/#:\s]+$")
_ROOT_FIELDS = frozenset(
    {
        "format_version",
        "request_id",
        "repository",
        "issue_number",
        "task_content_hash",
        "task_settings_hash",
        "task_flow",
        "merge_mode",
        "title",
        "description",
        "acceptance_criteria",
    }
)
_STEP_FIELDS = frozenset(
    {
        "format_version",
        "request_id",
        "repository",
        "issue_number",
        "task_content_hash",
        "task_settings_hash",
        "task_flow",
        "step",
        "source_kind",
        "source_hash",
        "source_task_id",
        "source_run_id",
        "source_summary",
        "pr_url",
        "base_commit",
        "head_commit",
        "fix_count",
        "branch_refresh_count",
        "fix_notes",
        "acceptance_criteria",
    }
)


class TaskRuntimeGitHub(Protocol):
    def get_issue(self, repository: str, issue_number: int) -> TaskIssue: ...

    def get_pr_write_state(self, pr_url: str) -> PullRequestWriteState: ...


class GitHubTaskRuntimeClient:
    """Combine existing issue and pull-request readers for Task replay."""

    def __init__(self, gh_path: str | Path) -> None:
        self._pull_requests = GitHubClient(gh_path)
        self._issues = GitHubTaskIssueClient(gh_path)

    def get_issue(self, repository: str, issue_number: int) -> TaskIssue:
        return self._issues.get_issue(repository, issue_number)

    def get_pr_write_state(self, pr_url: str) -> PullRequestWriteState:
        return self._pull_requests.get_pr_write_state(pr_url)


@dataclass(frozen=True, slots=True)
class TaskFlowSnapshot:
    """One active Task joined to its exact request, issue, cards, and PR."""

    request: TaskCreationRequest
    settings: TaskSettings
    issue: TaskIssue
    root_task_id: str | None
    pr: PullRequestWriteState | None
    state: TaskFlowState | None
    branch_refresh_count: int = 0
    current_card_id: str | None = None
    current_card_status: str | None = None
    last_task_id: str | None = None
    last_run: RunRecord | None = None
    last_summary: Mapping[str, object] | None = None
    pending_source_kind: str | None = None

    @property
    def issue_number(self) -> int:
        return self.issue.number


ReadyTaskFlowSnapshot = TaskFlowSnapshot
CardSpec = RootTaskCardSpec | TaskCardSpec


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_hash(value: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _root_payload(
    request: TaskCreationRequest,
    settings: TaskSettings,
    issue: TaskIssue,
) -> dict[str, object]:
    assert settings.task_settings_hash is not None
    return {
        "format_version": ROOT_CARD_FORMAT,
        "request_id": request.request_id,
        "repository": request.repository,
        "issue_number": issue.number,
        "task_content_hash": settings.task_content_hash,
        "task_settings_hash": settings.task_settings_hash,
        "task_flow": settings.task_flow.value,
        "merge_mode": settings.merge_mode.value,
        "title": request.content.title,
        "description": request.content.description,
        "acceptance_criteria": list(request.content.acceptance_criteria),
    }


def _parse_root_body(
    body: str | None,
    request: TaskCreationRequest,
    settings: TaskSettings,
    issue: TaskIssue,
) -> None:
    if not isinstance(body, str):
        raise GateError("Hermes root card body must be exact JSON text")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise GateError("Hermes root card body is not valid JSON") from error
    if not isinstance(payload, dict) or set(payload) != _ROOT_FIELDS:
        raise GateError("Hermes root card body fields do not match")
    expected = _root_payload(request, settings, issue)
    if payload != expected or body != _canonical_json(expected):
        raise GateError("Hermes root card body does not match the confirmed Task")


def _require_existing_file(path: str | Path, label: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise GateError(f"{label} database does not exist")
    return resolved


def _completed_outbox_rows(
    database_path: str | Path,
) -> tuple[tuple[str, int], ...]:
    path = _require_existing_file(database_path, "Task outbox")
    # Constructing the public store validates its exact schema before the
    # read-only scan discovers which completed tombstones need replay.
    TaskOutbox(path)
    try:
        with closing(
            sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        ) as connection:
            rows = connection.execute(
                """
                SELECT request_id, issue_number
                FROM task_outbox
                WHERE state = 'completed'
                ORDER BY rowid
                """
            ).fetchall()
    except sqlite3.Error as error:
        raise GateError("Task outbox completed scan failed") from error
    result: list[tuple[str, int]] = []
    for request_id, issue_number in rows:
        if not isinstance(request_id, str):
            raise GateError("Task outbox request_id is invalid")
        if type(issue_number) is not int or issue_number <= 0:
            raise GateError("Task outbox issue number is invalid")
        result.append((request_id, issue_number))
    return tuple(result)


def _validate_join(
    request: TaskCreationRequest,
    settings: TaskSettings,
    issue: TaskIssue,
    outbox_issue_number: int,
) -> None:
    if settings.task_settings_hash is None:
        raise GateError("active Task settings have no settings hash")
    if (
        request.request_id != settings.request_id
        or request.repository != settings.repository
        or request.task_flow is not settings.task_flow
        or request.merge_mode is not settings.merge_mode
        or request.confirmed_by != settings.confirmed_by
        or request.confirmed_at != settings.confirmed_at
    ):
        raise GateError("Task outbox request does not match active settings")
    if task_content_hash(request.content) != settings.task_content_hash:
        raise GateError("Task content hash does not match active settings")
    if (
        settings.issue_number != outbox_issue_number
        or issue.number != outbox_issue_number
    ):
        raise GateError("Task issue number does not match durable state")
    try:
        verify_task_issue_content(issue, request, settings)
    except TaskServiceError as error:
        raise GateError(
            f"GitHub issue does not match the confirmed Task: {error}"
        ) from error


def _matching_cards(
    all_cards: Sequence[HermesTaskCard],
    *,
    repository: str,
    issue_number: int,
    settings_hash: str,
) -> tuple[HermesTaskCard, ...]:
    cards = []
    for card in all_cards:
        identity = parse_task_card_key(card.idempotency_key)
        if identity.repository != repository or identity.issue_number != issue_number:
            continue
        if identity.kind.value == "task" and identity.hash_prefix != settings_hash[:16]:
            raise GateError("Hermes root card settings hash does not match")
        cards.append(card)
    return tuple(cards)


def _validate_cross_task_links(cards: Sequence[HermesTaskCard]) -> None:
    by_id = {card.task_id: card for card in cards}
    for child in cards:
        if child.parent_id is None or child.parent_id not in by_id:
            continue
        parent = by_id[child.parent_id]
        child_identity = parse_task_card_key(child.idempotency_key)
        parent_identity = parse_task_card_key(parent.idempotency_key)
        if (
            child_identity.repository != parent_identity.repository
            or child_identity.issue_number != parent_identity.issue_number
        ):
            raise GateError("Hermes card links two different Tasks")


def _one_completed_run(
    store: HermesStore,
    task_id: str,
    status: str,
) -> RunRecord | None:
    runs = store.completed_runs(task_id)
    if len(runs) > 1:
        raise GateError("Hermes card has more than one completed run")
    if not runs:
        if status == "done":
            raise GateError("done Hermes card has no completed result")
        return None
    run = runs[0]
    if status != "done":
        raise GateError("Hermes card has a completed result but is not done")
    return run


def _expected_role_and_skill(step: TaskStep | None) -> tuple[str, str]:
    if step is None:
        return "builder", "build-task"
    return {
        TaskStep.BUILD: ("builder", "build-task"),
        TaskStep.REVIEW: ("reviewer", "review-task"),
        TaskStep.DEEP_CHECK: ("deep_checker", "deep-check"),
        TaskStep.FIX: ("fix", "fix-task"),
    }[step]


def _validate_role_and_skill(card: HermesTaskCard, step: TaskStep | None) -> None:
    role, skill = _expected_role_and_skill(step)
    if card.assignee != role or card.skills != (skill,):
        raise GateError("Hermes card role or skill does not match its Task step")


def _parse_body_object(body: str, label: str) -> dict[str, object]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise GateError(f"{label} is not valid JSON") from error
    if not isinstance(payload, dict):
        raise GateError(f"{label} must be a JSON object")
    if body != _canonical_json(payload):
        raise GateError(f"{label} must use canonical JSON")
    return payload


def _source_hash(summary: Mapping[str, object]) -> tuple[str, str]:
    if summary.get("format_version") == "forge-step-proof/v1":
        parse_step_proof(summary)
        return "fix_proof", _canonical_hash(summary)
    result = parse_task_result(_summary_step(summary), summary)
    return "result", source_result_hash(result)


def _normalize_pr_for_replay(
    pr: PullRequestWriteState,
) -> PullRequestWriteState:
    if not pr.is_merged:
        return pr
    if pr.merged_base_commit is None or pr.merged_head_commit is None:
        raise GateError("merged pull request has no historical base and head")
    return replace(
        pr,
        base_commit=pr.merged_base_commit,
        head_commit=pr.merged_head_commit,
    )


def _parse_step_body(
    card: HermesTaskCard,
    *,
    request: TaskCreationRequest,
    settings: TaskSettings,
    issue: TaskIssue,
    expected_step: TaskStep,
    state: TaskFlowState,
    last_run: RunRecord,
    last_summary: Mapping[str, object],
    branch_refresh_count: int,
) -> tuple[TaskFlowState, int]:
    payload = _parse_body_object(card.body, "Hermes step card body")
    if set(payload) != _STEP_FIELDS:
        raise GateError("Hermes step card body fields do not match")
    assert settings.task_settings_hash is not None
    fixed = {
        "format_version": STEP_CARD_FORMAT,
        "request_id": request.request_id,
        "repository": request.repository,
        "issue_number": issue.number,
        "task_content_hash": settings.task_content_hash,
        "task_settings_hash": settings.task_settings_hash,
        "task_flow": settings.task_flow.value,
        "step": expected_step.value,
        "source_task_id": last_run.task_id,
        "source_run_id": last_run.run_id,
        "pr_url": state.pr_url,
        "acceptance_criteria": list(request.content.acceptance_criteria),
    }
    if any(payload.get(key) != value for key, value in fixed.items()):
        raise GateError("Hermes step card body does not match Task evidence")
    source_kind = payload.get("source_kind")
    source_summary = payload.get("source_summary")
    if not isinstance(source_summary, dict):
        raise GateError("Hermes step card body source_summary must be an object")
    next_state = state
    next_refresh_count = branch_refresh_count
    if source_kind in {"branch_refresh", "commit_change"}:
        from .github_merge import MAX_BRANCH_REFRESH_COUNT

        expected_refresh_fields = {
            "format_version",
            "pr_url",
            "base_commit",
            "head_commit",
            "branch_refresh_count",
        }
        if set(source_summary) != expected_refresh_fields:
            raise GateError("Hermes step card body commit change fields do not match")
        if expected_step is not TaskStep.BUILD:
            raise GateError("commit change can only create a Build card")
        count = source_summary.get("branch_refresh_count")
        expected_count = (
            branch_refresh_count + 1
            if source_kind == "branch_refresh"
            else branch_refresh_count
        )
        expected_format = (
            "forge-branch-refresh/v1"
            if source_kind == "branch_refresh"
            else "forge-commit-change/v1"
        )
        if (
            type(count) is not int
            or count != expected_count
            or source_summary.get("format_version") != expected_format
        ):
            raise GateError("Hermes commit change count or format does not match")
        if count > MAX_BRANCH_REFRESH_COUNT:
            raise GateError("Hermes branch refresh count exceeds its limit")
        if payload.get("branch_refresh_count") != count:
            raise GateError("Hermes step card body branch refresh count does not match")
        for key in ("pr_url", "base_commit", "head_commit"):
            if payload.get(key) != source_summary.get(key):
                raise GateError("Hermes commit change proof does not match card")
        try:
            next_state = observe_current_commit(
                state,
                source_summary["head_commit"],
                current_base_commit=source_summary["base_commit"],
            )
        except Exception as error:
            raise GateError("Hermes commit change commits are invalid") from error
        if (
            next_state.current_step is not TaskStep.BUILD
            or next_state.status is not TaskFlowStatus.RUNNING
        ):
            raise GateError("Hermes commit change did not restart Build")
        expected_hash = _canonical_hash(source_summary)
        next_refresh_count = count
    elif source_kind in {"result", "fix_proof"}:
        if source_summary != dict(last_summary):
            raise GateError("Hermes step card body source summary does not match parent")
        try:
            expected_kind, expected_hash = _source_hash(source_summary)
        except Exception as error:
            raise GateError("Hermes step card body source summary is invalid") from error
        if source_kind != expected_kind:
            raise GateError("Hermes step card body source kind does not match parent")
        if payload.get("base_commit") != state.current_base_commit:
            raise GateError("Hermes step card body base commit does not match flow")
        if payload.get("head_commit") != state.current_commit:
            raise GateError("Hermes step card body head commit does not match flow")
        if payload.get("branch_refresh_count") != branch_refresh_count:
            raise GateError("Hermes step card body branch refresh count changed")
    else:
        raise GateError("Hermes step card body source kind is invalid")
    if payload.get("source_hash") != expected_hash:
        raise GateError("Hermes step card body source hash does not match")
    if payload.get("fix_count") != next_state.fix_count:
        raise GateError("Hermes step card body fix count does not match")
    if payload.get("fix_notes") != next_state.fix_notes:
        raise GateError("Hermes step card body fix notes do not match")
    expected_key = step_card_key(
        request.repository,
        issue.number,
        expected_step,
        expected_hash,
    )
    if card.idempotency_key != expected_key:
        raise GateError("Hermes step card key does not match its source proof")
    return next_state, next_refresh_count


def _recover_branch_refresh_intent(
    intent: BranchRefreshIntent,
    *,
    settings: TaskSettings,
    state: TaskFlowState,
    pr: PullRequestWriteState,
    settings_store: TaskSettingsStore,
) -> BranchRefreshIntent | None:
    """Turn durable refresh readback into the missing Build-card source."""

    if (
        intent.pr_url != state.pr_url
        or intent.expected_base_commit != state.current_base_commit
        or intent.expected_head_commit != state.current_commit
    ):
        raise GateError("durable branch refresh intent does not match Task proof")
    remote_changed = (
        pr.base_commit != intent.expected_base_commit
        or pr.head_commit != intent.expected_head_commit
    )
    if not remote_changed:
        if intent.completed:
            raise GateError("completed branch refresh is missing from GitHub")
        return None
    if pr.is_merged or not pr.is_open:
        raise GateError("pull request closed during branch refresh recovery")
    if intent.completed:
        if (
            intent.current_base_commit != pr.base_commit
            or intent.current_head_commit != pr.head_commit
        ):
            raise GateError("GitHub branch refresh readback changed again")
        return intent
    try:
        with settings_store.guard_active(settings) as guard:
            return guard.complete_branch_refresh(
                intent,
                current_base_commit=pr.base_commit,
                current_head_commit=pr.head_commit,
            )
    except Exception as error:
        raise GateError("branch refresh readback could not be persisted") from error


def _replay_cards(
    *,
    request: TaskCreationRequest,
    settings: TaskSettings,
    issue: TaskIssue,
    cards: Sequence[HermesTaskCard],
    hermes: HermesStore,
    github: TaskRuntimeGitHub,
    settings_store: TaskSettingsStore,
) -> TaskFlowSnapshot:
    assert settings.task_settings_hash is not None
    roots = [
        card
        for card in cards
        if parse_task_card_key(card.idempotency_key).kind.value == "task"
    ]
    if len(roots) > 1:
        raise GateError("more than one Hermes root card exists for Task")
    if not roots:
        if cards:
            raise GateError("Hermes step card exists without its root card")
        return TaskFlowSnapshot(request, settings, issue, None, None, None)

    root = roots[0]
    if root.parent_id is not None:
        raise GateError("Hermes root card must not have a parent")
    _validate_role_and_skill(root, None)
    expected_root_key = task_card_key(
        request.repository,
        issue.number,
        settings.task_settings_hash,
    )
    if root.idempotency_key != expected_root_key:
        raise GateError("Hermes root card key does not match Task settings")
    _parse_root_body(root.body, request, settings, issue)
    children_by_parent: dict[str, list[HermesTaskCard]] = {}
    for card in cards:
        if card is root:
            continue
        if card.parent_id is None:
            raise GateError("Hermes step card must have exactly one parent")
        children_by_parent.setdefault(card.parent_id, []).append(card)
    if any(len(children) != 1 for children in children_by_parent.values()):
        raise GateError("Hermes Task step chain contains a branch")

    root_run = _one_completed_run(hermes, root.task_id, root.status)
    if root_run is None:
        if children_by_parent:
            raise GateError("Hermes child exists before root Build completed")
        return TaskFlowSnapshot(
            request,
            settings,
            issue,
            root.task_id,
            None,
            None,
            current_card_id=root.task_id,
            current_card_status=root.status,
        )

    try:
        build = parse_task_result(TaskStep.BUILD.value, root_run.summary)
    except Exception as error:
        raise GateError("Hermes root Build result is invalid") from error
    pr = _normalize_pr_for_replay(github.get_pr_write_state(build.pr_url))
    if pr.repository != request.repository or pr.pr_url != build.pr_url:
        raise GateError("Build result pull request does not match Task repository")
    first_children = children_by_parent.get(root.task_id, [])
    state = start_task_flow(
        settings.task_flow,
        task_settings_hash=settings.task_settings_hash,
        pr_url=build.pr_url,
        current_base_commit=build.built_base_commit,
        current_commit=build.built_commit,
    )
    try:
        state = record_task_result(state, build, current_commit=build.built_commit)
    except Exception as error:
        raise GateError("Hermes root Build result cannot be replayed") from error

    last_task_id = root.task_id
    last_run = root_run
    last_summary: Mapping[str, object] = root_run.summary
    branch_refresh_count = 0
    visited = {root.task_id}
    child_list = first_children
    while child_list:
        child = child_list[0]
        if child.task_id in visited:
            raise GateError("Hermes Task step chain contains a cycle")
        visited.add(child.task_id)
        identity = parse_task_card_key(child.idempotency_key)
        expected_step = next_task_action(state)
        if expected_step is None and identity.step is TaskStep.BUILD:
            candidate_body = _parse_body_object(
                child.body,
                "Hermes step card body",
            )
            if candidate_body.get("source_kind") in {
                "branch_refresh",
                "commit_change",
            }:
                expected_step = TaskStep.BUILD
        if identity.step is not expected_step:
            raise GateError("Hermes step card is out of selected flow order")
        assert expected_step is not None
        _validate_role_and_skill(child, expected_step)
        state, branch_refresh_count = _parse_step_body(
            child,
            request=request,
            settings=settings,
            issue=issue,
            expected_step=expected_step,
            state=state,
            last_run=last_run,
            last_summary=last_summary,
            branch_refresh_count=branch_refresh_count,
        )
        run = _one_completed_run(hermes, child.task_id, child.status)
        if run is None:
            state = mark_task_step_running(state)
            last_task_id = child.task_id
            if children_by_parent.get(child.task_id):
                raise GateError("Hermes child exists before parent step completed")
            break
        try:
            if expected_step is TaskStep.FIX:
                proof = parse_step_proof(run.summary)
                if (
                    proof.source_task_id != last_run.task_id
                    or proof.source_run_id != last_run.run_id
                ):
                    raise GateError("Fix proof source run does not match parent result")
                state = record_fix_proof(
                    state,
                    proof,
                    current_commit=proof.tested_commit,
                )
            else:
                result = parse_task_result(expected_step.value, run.summary)
                commit = (
                    result.built_commit
                    if expected_step is TaskStep.BUILD
                    else result.reviewed_commit
                    if expected_step is TaskStep.REVIEW
                    else result.tested_commit
                )
                state = record_task_result(state, result, current_commit=commit)
        except Exception as error:
            raise GateError("Hermes step result cannot be replayed") from error
        last_task_id = child.task_id
        last_run = run
        last_summary = run.summary
        child_list = children_by_parent.get(child.task_id, [])

    if len(visited) != len(cards):
        raise GateError("Hermes Task contains an orphan step card")
    pending_source_kind = None
    refresh_intent = settings_store.get_branch_refresh_replay(
        request.request_id,
        applied_refresh_count=branch_refresh_count,
    )
    if refresh_intent is not None and state.step_running:
        raise GateError("durable branch refresh intent conflicts with a running step")
    if not state.step_running:
        observed = observe_current_commit(
            state,
            pr.head_commit,
            current_base_commit=pr.base_commit,
        )
        if refresh_intent is not None:
            refresh_intent = _recover_branch_refresh_intent(
                refresh_intent,
                settings=settings,
                state=state,
                pr=pr,
                settings_store=settings_store,
            )
            if refresh_intent is not None:
                pending_source_kind = "branch_refresh"
                branch_refresh_count = refresh_intent.refresh_number
        elif observed != state and observed.current_step is TaskStep.BUILD:
            pending_source_kind = "commit_change"
        state = observed
    return TaskFlowSnapshot(
        request=request,
        settings=settings,
        issue=issue,
        root_task_id=root.task_id,
        pr=pr,
        state=state,
        branch_refresh_count=branch_refresh_count,
        current_card_id=(last_task_id if state.step_running else None),
        current_card_status=(
            next(card.status for card in cards if card.task_id == last_task_id)
            if state.step_running
            else None
        ),
        last_task_id=last_task_id,
        last_run=last_run,
        last_summary=last_summary,
        pending_source_kind=pending_source_kind,
    )


def load_task_flow_snapshots(
    *,
    settings_db: str | Path,
    outbox_db: str | Path,
    hermes_db: str | Path,
    github: TaskRuntimeGitHub,
    repository: str,
) -> tuple[TaskFlowSnapshot, ...]:
    """Join every active Task and replay its strict Hermes result chain."""

    if (
        not isinstance(repository, str)
        or _REPOSITORY_RE.fullmatch(repository) is None
    ):
        raise GateError("repository must use OWNER/REPO")
    settings_path = _require_existing_file(settings_db, "Task settings")
    hermes_path = _require_existing_file(hermes_db, "Hermes")
    settings_store = TaskSettingsStore(settings_path)
    outbox = TaskOutbox(_require_existing_file(outbox_db, "Task outbox"))
    hermes = HermesStore(hermes_path)
    all_cards = hermes.list_runtime_cards()
    _validate_cross_task_links(all_cards)
    snapshots: list[TaskFlowSnapshot] = []
    for request_id, issue_number in _completed_outbox_rows(outbox_db):
        request = outbox.load(request_id)
        if request is None:
            raise GateError("completed Task outbox request was not found")
        if request.repository != repository:
            continue
        settings = settings_store.get_active(request_id)
        if settings is None:
            continue
        issue = github.get_issue(repository, issue_number)
        _validate_join(request, settings, issue, issue_number)
        assert settings.task_settings_hash is not None
        cards = _matching_cards(
            all_cards,
            repository=repository,
            issue_number=issue_number,
            settings_hash=settings.task_settings_hash,
        )
        # Use the already validated snapshot list so one scan cannot observe a
        # different Hermes database generation midway through this Task.
        snapshots.append(
            _replay_cards(
                request=request,
                settings=settings,
                issue=issue,
                cards=cards,
                hermes=hermes,
                github=github,
                settings_store=settings_store,
            )
        )
    return tuple(snapshots)


def load_ready_to_merge_snapshots(**kwargs: object) -> tuple[ReadyTaskFlowSnapshot, ...]:
    """Return only snapshots with complete current PR proof."""

    result = []
    for snapshot in load_task_flow_snapshots(**kwargs):
        if snapshot.state is None or snapshot.pr is None:
            continue
        if snapshot.state.status is not TaskFlowStatus.READY_TO_MERGE:
            continue
        if (
            snapshot.state.current_commit != snapshot.pr.head_commit
            or snapshot.state.current_base_commit != snapshot.pr.base_commit
        ):
            raise GateError("ready Task state does not match current pull request")
        result.append(snapshot)
    return tuple(result)


def _root_spec(snapshot: TaskFlowSnapshot) -> RootTaskCardSpec:
    assert snapshot.settings.task_settings_hash is not None
    return RootTaskCardSpec(
        title=f"Build Task: {snapshot.request.repository}#{snapshot.issue.number}",
        body=_canonical_json(
            _root_payload(snapshot.request, snapshot.settings, snapshot.issue)
        ),
        idempotency_key=task_card_key(
            snapshot.request.repository,
            snapshot.issue.number,
            snapshot.settings.task_settings_hash,
        ),
    )


def _step_source(
    snapshot: TaskFlowSnapshot,
) -> tuple[str, str, Mapping[str, object]]:
    if snapshot.state is None or snapshot.pr is None:
        raise GateError("Task has no current flow state")
    if snapshot.pending_source_kind not in {"branch_refresh", "commit_change"}:
        if snapshot.last_summary is None:
            raise GateError("Task step has no completed source result")
        summary = dict(snapshot.last_summary)
        if summary.get("format_version") == "forge-step-proof/v1":
            proof = parse_step_proof(summary)
            if proof.tested_commit != snapshot.state.current_commit:
                raise GateError("Fix proof no longer matches current commit")
            return "fix_proof", _canonical_hash(summary), summary
        result = parse_task_result(
            _summary_step(summary),
            summary,
        )
        if (
            snapshot.state.current_step is TaskStep.BUILD
            and snapshot.state.current_commit != _summary_commit(summary)
        ):
            raise GateError("completed source result no longer matches current commit")
        return "result", source_result_hash(result), summary
    source_kind = snapshot.pending_source_kind
    assert source_kind in {"branch_refresh", "commit_change"}
    refresh = {
        "format_version": (
            "forge-branch-refresh/v1"
            if source_kind == "branch_refresh"
            else "forge-commit-change/v1"
        ),
        "pr_url": snapshot.pr.pr_url,
        "base_commit": snapshot.pr.base_commit,
        "head_commit": snapshot.pr.head_commit,
        "branch_refresh_count": snapshot.branch_refresh_count,
    }
    return source_kind, _canonical_hash(refresh), refresh


def _summary_step(summary: Mapping[str, object]) -> str:
    version = summary.get("format_version")
    return {
        "forge-build-result/v1": "build",
        "forge-review-result/v1": "review",
        "forge-deep-check-result/v1": "deep_check",
    }[version]


def _summary_commit(summary: Mapping[str, object]) -> object:
    version = summary.get("format_version")
    if version == "forge-build-result/v1":
        return summary.get("built_commit")
    if version == "forge-review-result/v1":
        return summary.get("reviewed_commit")
    if version == "forge-deep-check-result/v1":
        return summary.get("tested_commit")
    if version == "forge-step-proof/v1":
        return summary.get("tested_commit")
    return None


def _step_spec(snapshot: TaskFlowSnapshot) -> TaskCardSpec:
    assert snapshot.state is not None
    assert snapshot.pr is not None
    assert snapshot.settings.task_settings_hash is not None
    step = next_task_action(snapshot.state)
    if step is None or snapshot.state.step_running:
        raise GateError("Task has no new step card to create")
    source_kind, source_hash, source_summary = _step_source(snapshot)
    if snapshot.last_task_id is None:
        raise GateError("Task step has no completed parent card")
    payload = {
        "format_version": STEP_CARD_FORMAT,
        "request_id": snapshot.request.request_id,
        "repository": snapshot.request.repository,
        "issue_number": snapshot.issue.number,
        "task_content_hash": snapshot.settings.task_content_hash,
        "task_settings_hash": snapshot.settings.task_settings_hash,
        "task_flow": snapshot.settings.task_flow.value,
        "step": step.value,
        "source_kind": source_kind,
        "source_hash": source_hash,
        "source_task_id": (
            None if snapshot.last_run is None else snapshot.last_run.task_id
        ),
        "source_run_id": (
            None if snapshot.last_run is None else snapshot.last_run.run_id
        ),
        "source_summary": source_summary,
        "pr_url": snapshot.pr.pr_url,
        "base_commit": snapshot.pr.base_commit,
        "head_commit": snapshot.pr.head_commit,
        "fix_count": snapshot.state.fix_count,
        "branch_refresh_count": snapshot.branch_refresh_count,
        "fix_notes": snapshot.state.fix_notes,
        "acceptance_criteria": list(snapshot.request.content.acceptance_criteria),
    }
    return TaskCardSpec(
        step=step,
        title=(
            f"{step.value.replace('_', ' ').title()} Task: "
            f"{snapshot.request.repository}#{snapshot.issue.number}"
        ),
        body=_canonical_json(payload),
        parent_id=snapshot.last_task_id,
        skill={
            TaskStep.BUILD: "build-task",
            TaskStep.REVIEW: "review-task",
            TaskStep.DEEP_CHECK: "deep-check",
            TaskStep.FIX: "fix-task",
        }[step],
        idempotency_key=step_card_key(
            snapshot.request.repository,
            snapshot.issue.number,
            step,
            source_hash,
        ),
    )


def next_card_spec(snapshot: TaskFlowSnapshot) -> CardSpec | None:
    """Return the one missing root/step card, or null while waiting/terminal."""

    if not isinstance(snapshot, TaskFlowSnapshot):
        raise TypeError("snapshot must be a TaskFlowSnapshot")
    if snapshot.root_task_id is None:
        return _root_spec(snapshot)
    if snapshot.state is None or snapshot.state.step_running:
        return None
    if snapshot.state.status is not TaskFlowStatus.RUNNING:
        return None
    return _step_spec(snapshot)


def record_branch_refresh_result(
    snapshot: TaskFlowSnapshot,
    result: object,
    *,
    hermes_store: HermesStore,
    create_card: Callable[[Sequence[str]], None],
    workspace: str,
) -> str:
    """Persist one successful branch refresh as the next Build card."""

    from .github_merge import (
        MAX_BRANCH_REFRESH_COUNT,
        BranchRefreshResult,
        RESTART_FLOW,
    )

    if not isinstance(snapshot, TaskFlowSnapshot):
        raise TypeError("snapshot must be a TaskFlowSnapshot")
    if not isinstance(result, BranchRefreshResult):
        raise TypeError("result must be a BranchRefreshResult")
    if snapshot.state is None or snapshot.pr is None:
        raise GateError("branch refresh requires a current Task pull request")
    if snapshot.pr.is_merged or not snapshot.pr.is_open:
        raise GateError("branch refresh requires an open pull request")
    if not isinstance(hermes_store, HermesStore):
        raise TypeError("hermes_store must be a HermesStore")
    if result.code != RESTART_FLOW:
        raise GateError("branch refresh result must restart the Task flow")
    if (
        result.next_step != "build"
        or not result.invalidate_existing_proofs
        or result.flow_completed
        or result.final_tested_commit is not None
    ):
        raise GateError("branch refresh result does not require an exact Build restart")
    if result.branch_refresh_count != snapshot.branch_refresh_count + 1:
        raise GateError("branch refresh count must increment exactly once")
    if result.branch_refresh_count > MAX_BRANCH_REFRESH_COUNT:
        raise GateError("branch refresh limit was reached")
    if (
        result.current_commit == snapshot.pr.head_commit
        and result.current_base_commit == snapshot.pr.base_commit
    ):
        raise GateError("branch refresh did not change the pull request")
    try:
        restarted = observe_current_commit(
            snapshot.state,
            result.current_commit,
            current_base_commit=result.current_base_commit,
        )
    except Exception as error:
        raise GateError("branch refresh commits are invalid") from error
    if (
        restarted.status is not TaskFlowStatus.RUNNING
        or restarted.current_step is not TaskStep.BUILD
        or restarted.step_running
    ):
        raise GateError("branch refresh did not invalidate existing proof")
    updated = replace(
        snapshot,
        pr=replace(
            snapshot.pr,
            base_commit=result.current_base_commit,
            head_commit=result.current_commit,
        ),
        state=restarted,
        branch_refresh_count=result.branch_refresh_count,
        current_card_id=None,
        current_card_status=None,
        pending_source_kind="branch_refresh",
    )
    spec = _step_spec(updated)
    if spec.step is not TaskStep.BUILD:
        raise GateError("branch refresh must create a Build card")
    if not hermes_store.has_idempotency_key(spec.idempotency_key):
        create_card(build_create_argv(spec, workspace))
    return spec.idempotency_key


def build_branch_refresh_recorder(
    *,
    hermes_db: str | Path,
    hermes_path: str | Path,
    workspace: str,
) -> Callable[[TaskFlowSnapshot, object], str]:
    """Build the merge-worker callback that records branch refresh proof."""

    store = HermesStore(hermes_db)
    create = HermesCreateCommand(hermes_path)

    def record(snapshot: TaskFlowSnapshot, result: object) -> str:
        return record_branch_refresh_result(
            snapshot,
            result,
            hermes_store=store,
            create_card=create,
            workspace=workspace,
        )

    return record


def label_for_snapshot(snapshot: TaskFlowSnapshot) -> str:
    """Map active runtime evidence to exactly one official issue label."""

    if snapshot.state is not None:
        if snapshot.current_card_status == "blocked":
            return "forge:waiting-for-help"
        if snapshot.current_card_status == "failed":
            return "forge:failed"
        return displayed_label(snapshot.state)
    if snapshot.root_task_id is None:
        return "forge:ready-to-build"
    if snapshot.current_card_status == "blocked":
        return "forge:waiting-for-help"
    if snapshot.current_card_status == "failed":
        return "forge:failed"
    return "forge:building"


@dataclass(frozen=True, slots=True)
class WorkerReport:
    request_id: str
    issue_number: int
    status: str
    card_key: str | None


def run_task_flow_worker(
    *,
    settings_db: str | Path,
    outbox_db: str | Path,
    hermes_db: str | Path,
    hermes_path: str | Path,
    github: TaskRuntimeGitHub,
    repository: str,
    workspace: str,
    dry_run: bool = False,
) -> tuple[WorkerReport, ...]:
    """Scan active Tasks and create at most one missing card for each."""

    store = HermesStore(hermes_db)
    settings_store = TaskSettingsStore(settings_db)
    create = HermesCreateCommand(hermes_path)
    reports: list[WorkerReport] = []
    for snapshot in load_task_flow_snapshots(
        settings_db=settings_db,
        outbox_db=outbox_db,
        hermes_db=hermes_db,
        github=github,
        repository=repository,
    ):
        spec = next_card_spec(snapshot)
        if spec is None:
            status = (
                snapshot.state.status.value
                if snapshot.state is not None
                and snapshot.state.status is not TaskFlowStatus.RUNNING
                else "waiting"
            )
            reports.append(
                WorkerReport(
                    snapshot.request.request_id,
                    snapshot.issue.number,
                    status,
                    None,
                )
            )
            continue
        key = spec.idempotency_key
        if not store.has_idempotency_key(key) and not dry_run:
            # RISK(race): cancellation and card creation must have one order.
            # The database write lock is held until the Hermes command returns.
            with settings_store.guard_active(snapshot.settings):
                if not store.has_idempotency_key(key):
                    argv = (
                        build_root_create_argv(spec, workspace)
                        if isinstance(spec, RootTaskCardSpec)
                        else build_create_argv(spec, workspace)
                    )
                    create(argv)
        reports.append(
            WorkerReport(
                snapshot.request.request_id,
                snapshot.issue.number,
                "planned" if dry_run else "created",
                key,
            )
        )
    return tuple(reports)


@dataclass(frozen=True, slots=True)
class ProjectRuntimeSnapshot:
    """Exact active v2 settings joined to one immutable Project row."""

    request: TaskRequestV2
    settings: TaskSettingsV2
    project: TaskProject
    project_state: str
    branch_name: str | None
    worktree_path: str | None


@dataclass(frozen=True, slots=True)
class ProjectWorkerReport:
    request_id: str
    project_id: str
    repository: str
    parent_issue_number: int
    status: str
    card_key: str | None


def _project_payload(project: TaskProject) -> dict[str, str]:
    return {
        "project_id": project.project_id,
        "repository": project.repository,
        "workspace": project.workspace,
        "remote_name": project.remote_name,
        "base_branch": project.base_branch,
        "base_commit": project.base_commit,
        "host_id": project.host_id,
    }


def _project_json(project: TaskProject) -> str:
    return _canonical_json(_project_payload(project))


class _ProjectRuntimeRegistry:
    """Read and guard the Task 8 registry without inventing fallback state."""

    _RUNNABLE_STATES = frozenset({"ready", "running", "reviewing"})

    def __init__(self, path: str | Path, *, read_only: bool = False) -> None:
        self._read_only = read_only
        self._database: TaskDatabase | None = None
        if read_only:
            self._path = _require_existing_file(path, "v2 Task")
            return
        try:
            self._database = TaskDatabase(path)
        except TaskDatabaseError as error:
            raise GateError("v2 Task database could not be opened") from error
        self._path = self._database.database_path

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        if self._database is not None:
            with self._database.read() as connection:
                yield connection
            return
        try:
            connection = sqlite3.connect(
                f"{self._path.as_uri()}?mode=ro&immutable=1",
                uri=True,
            )
            connection.row_factory = sqlite3.Row
        except sqlite3.Error as error:
            raise GateError("v2 Task database could not be opened read-only") from error
        with closing(connection):
            yield connection

    def list_active(self) -> tuple[ProjectRuntimeSnapshot, ...]:
        try:
            with self._read() as connection:
                rows = connection.execute(
                    """
                    SELECT r.request_json, s.settings_json, p.project_id,
                           p.project_json, p.state, p.branch_name,
                           p.worktree_path
                    FROM task_settings_v2 AS s
                    JOIN task_requests AS r ON r.request_id = s.request_id
                    JOIN task_projects AS p
                      ON p.request_id = s.request_id
                     AND p.task_settings_hash = s.task_settings_hash
                    WHERE (
                        SELECT lifecycle.event_type
                        FROM task_events AS lifecycle
                        WHERE lifecycle.request_id = s.request_id
                          AND lifecycle.event_type IN (
                              'active', 'revision_requested', 'changing',
                              'revision_cancelled', 'revision_resumed',
                              'stop_requested', 'stopping', 'cancelled',
                              'expired', 'merged', 'replaced',
                              'partially_merged'
                          )
                        ORDER BY lifecycle.event_id DESC LIMIT 1
                    ) IN ('active', 'revision_resumed')
                      AND (
                        SELECT lifecycle.task_settings_hash
                        FROM task_events AS lifecycle
                        WHERE lifecycle.request_id = s.request_id
                          AND lifecycle.event_type IN (
                              'active', 'revision_requested', 'changing',
                              'revision_cancelled', 'revision_resumed',
                              'stop_requested', 'stopping', 'cancelled',
                              'expired', 'merged', 'replaced',
                              'partially_merged'
                          )
                        ORDER BY lifecycle.event_id DESC LIMIT 1
                      ) = s.task_settings_hash
                    ORDER BY r.request_id, p.project_id
                    """
                ).fetchall()
                snapshots = tuple(
                    self._snapshot_from_row(connection, row) for row in rows
                )
                self._require_complete_project_sets(connection, snapshots)
        except (sqlite3.Error, TaskDatabaseError) as error:
            raise GateError("v2 Project registry scan failed") from error
        return tuple(
            snapshot
            for snapshot in snapshots
            if snapshot.project_state in self._RUNNABLE_STATES
        )

    def get_active(
        self,
        *,
        request_id: str,
        task_settings_hash: str,
        project_id: str,
    ) -> ProjectRuntimeSnapshot:
        """Return one exact dispatch-ready Project or fail closed."""

        matches = tuple(
            snapshot
            for snapshot in self.list_active()
            if snapshot.request.request_id == request_id
            and snapshot.settings.task_settings_hash == task_settings_hash
            and snapshot.project.project_id == project_id
        )
        if len(matches) != 1:
            raise GateError("exact active Project runtime binding is unavailable")
        return matches[0]

    def _require_complete_project_sets(
        self,
        connection: sqlite3.Connection,
        snapshots: tuple[ProjectRuntimeSnapshot, ...],
    ) -> None:
        active_rows = connection.execute(
            """
            SELECT r.request_json, s.settings_json
            FROM task_settings_v2 AS s
            JOIN task_requests AS r ON r.request_id = s.request_id
            WHERE (
                SELECT lifecycle.event_type
                FROM task_events AS lifecycle
                WHERE lifecycle.request_id = s.request_id
                  AND lifecycle.event_type IN (
                      'active', 'revision_requested', 'changing',
                      'revision_cancelled', 'revision_resumed',
                      'stop_requested', 'stopping', 'cancelled',
                      'expired', 'merged', 'replaced',
                      'partially_merged'
                  )
                ORDER BY lifecycle.event_id DESC LIMIT 1
            ) IN ('active', 'revision_resumed')
              AND (
                SELECT lifecycle.task_settings_hash
                FROM task_events AS lifecycle
                WHERE lifecycle.request_id = s.request_id
                  AND lifecycle.event_type IN (
                      'active', 'revision_requested', 'changing',
                      'revision_cancelled', 'revision_resumed',
                      'stop_requested', 'stopping', 'cancelled',
                      'expired', 'merged', 'replaced',
                      'partially_merged'
                  )
                ORDER BY lifecycle.event_id DESC LIMIT 1
              ) = s.task_settings_hash
            ORDER BY r.request_id
            """
        ).fetchall()
        actual: dict[str, set[str]] = {}
        for snapshot in snapshots:
            actual.setdefault(snapshot.request.request_id, set()).add(
                snapshot.project.project_id
            )
        expected_request_ids: set[str] = set()
        for row in active_rows:
            try:
                request = TaskRequestV2.from_json(row[0])
                settings = TaskSettingsV2.from_json(row[1], request=request)
            except TaskSettingsV2Error as error:
                raise GateError("stored v2 Task settings are invalid") from error
            self._require_exact_record_rows(connection, request, settings)
            self._require_active_events(connection, request, settings)
            expected_ids = {project.project_id for project in settings.projects}
            if actual.get(request.request_id, set()) != expected_ids:
                raise GateError(
                    "v2 Task registry does not contain the complete Project set"
                )
            expected_request_ids.add(request.request_id)
        if set(actual) != expected_request_ids:
            raise GateError("v2 Task registry contains an unexpected Project set")

    @contextmanager
    def guard(self, expected: ProjectRuntimeSnapshot) -> Iterator[sqlite3.Connection]:
        if self._database is None:
            try:
                with self._read() as connection:
                    current = self._guard_snapshot(connection, expected)
                    if current != expected:
                        raise GateError(
                            "v2 Project or settings changed before a safe point"
                        )
                    yield connection
            except sqlite3.Error as error:
                raise GateError("v2 Project read-only guard failed") from error
            return
        try:
            with self._database.transaction() as connection:
                current = self._guard_snapshot(connection, expected)
                if current != expected:
                    raise GateError("v2 Project or settings changed before a safe point")
                yield connection
        except TaskDatabaseError as error:
            raise GateError("v2 Project safe-point guard failed") from error

    def _guard_snapshot(
        self,
        connection: sqlite3.Connection,
        expected: ProjectRuntimeSnapshot,
    ) -> ProjectRuntimeSnapshot:
        row = connection.execute(
            """
            SELECT r.request_json, s.settings_json, p.project_id,
                   p.project_json, p.state, p.branch_name,
                   p.worktree_path
            FROM task_settings_v2 AS s
            JOIN task_requests AS r ON r.request_id = s.request_id
            JOIN task_projects AS p
              ON p.request_id = s.request_id
             AND p.task_settings_hash = s.task_settings_hash
            WHERE s.request_id = ? AND p.project_id = ?
            """,
            (expected.request.request_id, expected.project.project_id),
        ).fetchone()
        if row is None:
            raise GateError("v2 Project disappeared before a safe point")
        return self._snapshot_from_row(connection, row)

    def record_worktree(
        self,
        connection: sqlite3.Connection,
        snapshot: ProjectRuntimeSnapshot,
        worktree: TaskWorktree,
    ) -> ProjectRuntimeSnapshot:
        if snapshot.branch_name is not None and snapshot.branch_name != worktree.branch_name:
            raise GateError("stored Project branch does not match deterministic branch")
        worktree_text = worktree.worktree_path.as_posix()
        if snapshot.worktree_path is not None and snapshot.worktree_path != worktree_text:
            raise GateError("stored Project worktree does not match deterministic path")
        updated = connection.execute(
            """
            UPDATE task_projects
            SET branch_name = ?, worktree_path = ?, state = 'running'
            WHERE request_id = ? AND project_id = ?
              AND task_settings_hash = ?
              AND state IN ('ready', 'running', 'reviewing')
              AND (branch_name IS NULL OR branch_name = ?)
              AND (worktree_path IS NULL OR worktree_path = ?)
            """,
            (
                worktree.branch_name,
                worktree_text,
                snapshot.request.request_id,
                snapshot.project.project_id,
                snapshot.settings.task_settings_hash,
                worktree.branch_name,
                worktree_text,
            ),
        )
        if updated.rowcount != 1:
            raise GateError("Project worktree registry update did not match exact settings")
        return replace(
            snapshot,
            project_state="running",
            branch_name=worktree.branch_name,
            worktree_path=worktree_text,
        )

    def _snapshot_from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> ProjectRuntimeSnapshot:
        try:
            request = TaskRequestV2.from_json(row[0])
            settings = TaskSettingsV2.from_json(row[1], request=request)
        except TaskSettingsV2Error as error:
            raise GateError("stored v2 Task settings are invalid") from error
        if request.format_version != TASK_REQUEST_V2_FORMAT:
            raise GateError("stored v2 Task request version changed")
        if settings.format_version != TASK_SETTINGS_V2_FORMAT:
            raise GateError("stored v2 Task settings version changed")
        self._require_exact_record_rows(connection, request, settings)
        project_id = row[2]
        matches = tuple(
            project for project in settings.projects if project.project_id == project_id
        )
        if len(matches) != 1:
            raise GateError("Project registry does not match active settings")
        project = matches[0]
        if row[3] != _project_json(project):
            raise GateError("Project registry snapshot does not match active settings")
        state = row[4]
        if type(state) is not str:
            raise GateError("Project registry state is invalid")
        for value, label in ((row[5], "branch"), (row[6], "worktree")):
            if value is not None and (type(value) is not str or not value):
                raise GateError(f"Project registry {label} is invalid")
        self._require_active_events(connection, request, settings)
        return ProjectRuntimeSnapshot(
            request=request,
            settings=settings,
            project=project,
            project_state=state,
            branch_name=row[5],
            worktree_path=row[6],
        )

    @staticmethod
    def _require_exact_record_rows(
        connection: sqlite3.Connection,
        request: TaskRequestV2,
        settings: TaskSettingsV2,
    ) -> None:
        request_payload = json.loads(request.to_json())
        request_row = connection.execute(
            """
            SELECT request_id, format_version, request_json, request_hash,
                   management_repository, task_owner_host, confirmed_by,
                   confirmed_at, replaces_request_id
            FROM task_requests WHERE request_id = ?
            """,
            (request.request_id,),
        ).fetchone()
        expected_request = (
            request.request_id,
            request.format_version,
            request.to_json(),
            request.request_hash,
            request.management_repository,
            request.task_owner_host,
            request.confirmed_by,
            request_payload["confirmed_at"],
            request.replaces_request_id,
        )
        if request_row is None or tuple(request_row) != expected_request:
            raise GateError("stored v2 Task request columns do not match JSON")
        settings_payload = json.loads(settings.to_json())
        settings_row = connection.execute(
            """
            SELECT task_settings_hash, request_id, request_hash,
                   format_version, settings_json, management_repository,
                   parent_issue_number, task_owner_host, confirmed_at
            FROM task_settings_v2 WHERE request_id = ?
            """,
            (request.request_id,),
        ).fetchone()
        expected_settings = (
            settings.task_settings_hash,
            settings.request_id,
            settings.request_hash,
            settings.format_version,
            settings.to_json(),
            settings.management_repository,
            settings.parent_issue_number,
            settings.task_owner_host,
            settings_payload["confirmed_at"],
        )
        if settings_row is None or tuple(settings_row) != expected_settings:
            raise GateError("stored v2 Task settings columns do not match JSON")

    def _require_active_events(
        self,
        connection: sqlite3.Connection,
        request: TaskRequestV2,
        settings: TaskSettingsV2,
    ) -> None:
        rows = connection.execute(
            """
            SELECT event_type, task_settings_hash, project_id, event_json
            FROM task_events WHERE request_id = ? ORDER BY event_id
            """,
            (request.request_id,),
        ).fetchall()
        if not task_lifecycle_is_active(
            connection,
            request.request_id,
            settings.task_settings_hash,
        ):
            raise GateError("v2 Task is blocked by its latest lifecycle event")
        common = _canonical_json(
            {"task_settings_hash": settings.task_settings_hash}
        )
        for event_type in ("settings_activated", "active"):
            matches = [row for row in rows if row[0] == event_type]
            if len(matches) != 1 or tuple(matches[0][1:]) != (
                settings.task_settings_hash,
                None,
                common,
            ):
                raise GateError(f"v2 Task {event_type} event does not match settings")
        dispatch = [row for row in rows if row[0] == "dispatch_ready"]
        expected_dispatch = _canonical_json(
            {
                "project_ids": [project.project_id for project in request.projects],
                "task_settings_hash": settings.task_settings_hash,
            }
        )
        if len(dispatch) != 1 or tuple(dispatch[0][1:]) != (
            settings.task_settings_hash,
            None,
            expected_dispatch,
        ):
            raise GateError("v2 Task dispatch event does not match every Project")


# Public dispatcher seam.  Keep the private alias used by existing flow code so
# this change does not broaden or alter the established runtime path.
ProjectRuntimeRegistry = _ProjectRuntimeRegistry


def _project_root_payload(snapshot: ProjectRuntimeSnapshot) -> dict[str, object]:
    if snapshot.branch_name is None or snapshot.worktree_path is None:
        raise GateError("Project worktree is not recorded")
    return {
        "format_version": PROJECT_CARD_FORMAT,
        "request_id": snapshot.request.request_id,
        "task_settings_hash": snapshot.settings.task_settings_hash,
        "management_repository": snapshot.settings.management_repository,
        "parent_issue_number": snapshot.settings.parent_issue_number,
        "project": _project_payload(snapshot.project),
        "task_flow": snapshot.settings.task_flow.value,
        "merge_mode": snapshot.settings.merge_mode.value,
        "step": TaskStep.BUILD.value,
        "branch_name": snapshot.branch_name,
        "worktree_path": snapshot.worktree_path,
        "title": snapshot.request.task_content.title,
        "description": snapshot.request.task_content.description,
        "acceptance_criteria": list(
            snapshot.request.task_content.acceptance_criteria
        ),
    }


def _project_root_spec(snapshot: ProjectRuntimeSnapshot) -> ProjectTaskCardSpec:
    return ProjectTaskCardSpec(
        step=TaskStep.BUILD,
        title=f"Build Project: {snapshot.project.repository}",
        body=_canonical_json(_project_root_payload(snapshot)),
        idempotency_key=project_task_card_key(
            snapshot.request.request_id,
            snapshot.project.project_id,
        ),
        parent_id=None,
        skill="build-task",
    )


def _project_step_spec(
    snapshot: ProjectRuntimeSnapshot,
    state: TaskFlowState,
    *,
    parent_id: str,
    source_run: RunRecord,
    source_summary: Mapping[str, object],
    source_hash: str,
) -> ProjectTaskCardSpec:
    step = next_task_action(state)
    if step is None or state.step_running:
        raise GateError("Project flow has no step card to create")
    payload = {
        **_project_root_payload(snapshot),
        "step": step.value,
        "source_task_id": source_run.task_id,
        "source_run_id": source_run.run_id,
        "source_hash": source_hash,
        "source_summary": source_summary,
        "pr_url": state.pr_url,
        "base_commit": state.current_base_commit,
        "head_commit": state.current_commit,
        "fix_count": state.fix_count,
        "fix_notes": state.fix_notes,
    }
    return ProjectTaskCardSpec(
        step=step,
        title=f"{step.value.replace('_', ' ').title()} Project: {snapshot.project.repository}",
        body=_canonical_json(payload),
        idempotency_key=project_step_card_key(
            snapshot.request.request_id,
            snapshot.project.project_id,
            step,
            source_hash,
        ),
        parent_id=parent_id,
        skill={
            TaskStep.BUILD: "build-task",
            TaskStep.REVIEW: "review-task",
            TaskStep.DEEP_CHECK: "deep-check",
            TaskStep.FIX: "fix-task",
        }[step],
    )


@dataclass(frozen=True, slots=True)
class _ProjectReplay:
    status: str
    next_spec: ProjectTaskCardSpec | None
    expected_head_commit: str | None


@dataclass(frozen=True, slots=True)
class _ProjectPreflight:
    original: ProjectRuntimeSnapshot
    snapshot: ProjectRuntimeSnapshot
    worktree: TaskWorktree
    replay: _ProjectReplay


def _project_cards(
    cards: Sequence[HermesTaskCard],
    snapshot: ProjectRuntimeSnapshot,
) -> tuple[HermesTaskCard, ...]:
    matching = []
    for card in cards:
        identity = parse_project_task_card_key(card.idempotency_key)
        if identity.request_id != snapshot.request.request_id:
            continue
        if identity.project_id != snapshot.project.project_id:
            continue
        matching.append(card)
    by_id = {card.task_id: card for card in cards}
    for card in matching:
        if card.parent_id is None:
            continue
        parent = by_id.get(card.parent_id)
        if parent is None:
            raise GateError("Project step card parent is missing")
        parent_identity = parse_project_task_card_key(parent.idempotency_key)
        if (
            parent_identity.request_id != snapshot.request.request_id
            or parent_identity.project_id != snapshot.project.project_id
        ):
            raise GateError("Hermes card links two different Projects")
    return tuple(matching)


def _require_project_pr(
    snapshot: ProjectRuntimeSnapshot,
    github: TaskRuntimeGitHub,
    pr_url: str,
) -> PullRequestWriteState:
    pr_repository, _number = parse_pull_request_url(pr_url)
    if pr_repository != snapshot.project.repository:
        raise GateError("pull request does not match Project repository")
    try:
        pr = github.get_pr_write_state(pr_url)
    except Exception as error:
        raise GateError("Project pull request readback failed") from error
    if (
        pr.pr_url != pr_url
        or pr.repository != snapshot.project.repository
        or pr.base_ref != snapshot.project.base_branch
        or pr.is_merged
        or not pr.is_open
    ):
        raise GateError("pull request evidence does not match Project settings")
    return pr


def _require_project_pr_at_state(
    snapshot: ProjectRuntimeSnapshot,
    github: TaskRuntimeGitHub,
    state: TaskFlowState,
) -> None:
    """Require the live PR to match the final replayed safe point exactly."""

    pr = _require_project_pr(snapshot, github, state.pr_url)
    if (
        pr.base_commit != state.current_base_commit
        or pr.head_commit != state.current_commit
    ):
        raise GateError("Project pull request changed during flow replay")


def _replay_project_flow(
    snapshot: ProjectRuntimeSnapshot,
    cards: Sequence[HermesTaskCard],
    *,
    hermes: HermesStore,
    github: TaskRuntimeGitHub,
) -> _ProjectReplay:
    root_key = project_task_card_key(
        snapshot.request.request_id,
        snapshot.project.project_id,
    )
    roots = [card for card in cards if card.idempotency_key == root_key]
    if not roots:
        if cards:
            raise GateError("Project step card exists without its Build root")
        return _ProjectReplay(
            "missing",
            _project_root_spec(snapshot),
            snapshot.project.base_commit,
        )
    if len(roots) != 1:
        raise GateError("more than one Project Build root exists")
    root = roots[0]
    expected = _project_root_spec(snapshot)
    if (
        root.parent_id is not None
        or root.title != expected.title
        or root.body != expected.body
        or root.assignee != expected.role.value
        or root.skills != (expected.skill,)
    ):
        raise GateError("Hermes Project root does not match exact settings")
    runs = hermes.completed_runs(root.task_id)
    if len(runs) > 1:
        raise GateError("Hermes Project card has more than one completed run")
    if not runs:
        if root.status == "done":
            raise GateError("done Hermes Project card has no completed result")
        if len(cards) != 1:
            raise GateError("Project child exists before its parent completed")
        return _ProjectReplay("waiting", None, None)
    if root.status != "done":
        raise GateError("Hermes Project card has a result but is not done")
    try:
        result = parse_task_result(TaskStep.BUILD.value, runs[0].summary)
    except Exception as error:
        raise GateError("Hermes Project Build result is invalid") from error
    _require_project_pr(snapshot, github, result.pr_url)
    if result.built_base_commit != snapshot.project.base_commit:
        raise GateError("Project Build base does not match confirmed base")
    state = start_task_flow(
        snapshot.settings.task_flow,
        task_settings_hash=snapshot.settings.task_settings_hash,
        pr_url=result.pr_url,
        current_base_commit=result.built_base_commit,
        current_commit=result.built_commit,
    )
    try:
        state = record_task_result(
            state,
            result,
            current_commit=result.built_commit,
        )
    except Exception as error:
        raise GateError("Project Build result does not match current settings") from error
    last_task_id = root.task_id
    last_run = runs[0]
    last_summary = runs[0].summary
    last_source_hash = source_result_hash(result)
    visited = {root.task_id}
    by_parent: dict[str, list[HermesTaskCard]] = {}
    for card in cards:
        if card.parent_id is not None:
            by_parent.setdefault(card.parent_id, []).append(card)
    while state.status is TaskFlowStatus.RUNNING:
        expected_spec = _project_step_spec(
            snapshot,
            state,
            parent_id=last_task_id,
            source_run=last_run,
            source_summary=last_summary,
            source_hash=last_source_hash,
        )
        children = by_parent.get(last_task_id, [])
        if not children:
            if len(visited) != len(cards):
                raise GateError("Hermes Project flow contains an orphan card")
            _require_project_pr_at_state(snapshot, github, state)
            return _ProjectReplay("running", expected_spec, state.current_commit)
        if len(children) != 1:
            raise GateError("Hermes Project card has more than one child")
        child = children[0]
        if child.task_id in visited:
            raise GateError("Hermes Project card chain contains a cycle")
        if (
            child.title != expected_spec.title
            or child.body != expected_spec.body
            or child.idempotency_key != expected_spec.idempotency_key
            or child.assignee != expected_spec.role.value
            or child.skills != (expected_spec.skill,)
        ):
            raise GateError("Hermes Project step does not match exact settings")
        child_runs = hermes.completed_runs(child.task_id)
        if len(child_runs) > 1:
            raise GateError("Hermes Project card has more than one completed run")
        visited.add(child.task_id)
        if not child_runs:
            if child.status == "done":
                raise GateError("done Hermes Project card has no completed result")
            if len(visited) != len(cards):
                raise GateError("Hermes Project flow contains a child after a running card")
            return _ProjectReplay("waiting", None, None)
        if child.status != "done":
            raise GateError("Hermes Project card has a result but is not done")
        child_run = child_runs[0]
        step = next_task_action(state)
        assert step is not None
        try:
            if step is TaskStep.FIX:
                proof = parse_step_proof(child_run.summary)
                if (
                    proof.source_task_id != last_run.task_id
                    or proof.source_run_id != last_run.run_id
                ):
                    raise GateError(
                        "Project Fix proof source does not match parent result"
                    )
                state = record_fix_proof(
                    state,
                    proof,
                    current_commit=proof.tested_commit,
                )
                child_source_hash = _canonical_hash(child_run.summary)
            else:
                child_result = parse_task_result(step.value, child_run.summary)
                child_repository, _number = parse_pull_request_url(child_result.pr_url)
                if child_repository != snapshot.project.repository:
                    raise GateError("pull request does not match Project repository")
                if step is TaskStep.BUILD:
                    result_commit = child_result.built_commit
                elif step is TaskStep.REVIEW:
                    result_commit = child_result.reviewed_commit
                else:
                    result_commit = child_result.tested_commit
                state = record_task_result(
                    state,
                    child_result,
                    current_commit=result_commit,
                )
                child_source_hash = source_result_hash(child_result)
        except GateError:
            raise
        except Exception as error:
            raise GateError("Project step result does not match exact settings") from error
        last_task_id = child.task_id
        last_run = child_run
        last_summary = child_run.summary
        last_source_hash = child_source_hash
    if len(visited) != len(cards):
        raise GateError("Hermes Project flow has cards after terminal state")
    _require_project_pr_at_state(snapshot, github, state)
    return _ProjectReplay(state.status.value, None, state.current_commit)


def load_project_runtime_snapshots(
    settings_db: str | Path,
) -> tuple[ProjectRuntimeSnapshot, ...]:
    """Enumerate every active Project from the v2 DB registry."""

    return _ProjectRuntimeRegistry(settings_db).list_active()


def _read_project_worktree(
    manager: TaskWorktreeManager,
    request_id: str,
    project: TaskProject,
    *,
    expected_head_commit: str | None,
    dry_run: bool,
) -> TaskWorktree:
    """Translate worktree identity failures into the worker gate contract."""

    try:
        if dry_run:
            if expected_head_commit is None:
                return manager.inspect_active(request_id, project)
            return manager.inspect(
                request_id,
                project,
                expected_head_commit=expected_head_commit,
            )
        if expected_head_commit is None:
            raise GateError("active Project worktree has no safe-point HEAD")
        return manager.prepare(
            request_id,
            project,
            expected_head_commit=expected_head_commit,
        )
    except TaskWorktreeError as error:
        raise GateError(str(error)) from error


def _plan_project_worktree(
    manager: TaskWorktreeManager,
    request_id: str,
    project: TaskProject,
) -> TaskWorktree:
    try:
        return manager.plan(request_id, project)
    except TaskWorktreeError as error:
        raise GateError(str(error)) from error


def run_project_task_flow_worker(
    *,
    settings_db: str | Path,
    hermes_db: str | Path,
    hermes_path: str | Path,
    github: TaskRuntimeGitHub,
    worktree_root: str | Path,
    dry_run: bool = False,
    create_card: Callable[[Sequence[str]], None] | None = None,
    remote_repository: Callable[[Path, str], str] | None = None,
) -> tuple[ProjectWorkerReport, ...]:
    """Prepare and dispatch every active v2 Project independently."""

    read_registry = _ProjectRuntimeRegistry(settings_db, read_only=True)
    snapshots = read_registry.list_active()
    store = HermesStore(_require_existing_file(hermes_db, "Hermes"))
    all_cards = store.list_project_runtime_cards()
    create = create_card or HermesCreateCommand(hermes_path)
    worktrees = TaskWorktreeManager(
        worktree_root,
        remote_repository=remote_repository,
    )

    preflight: list[_ProjectPreflight] = []
    for original in snapshots:
        planned = _plan_project_worktree(
            worktrees,
            original.request.request_id,
            original.project,
        )
        branch_missing = original.branch_name is None
        worktree_missing = original.worktree_path is None
        if branch_missing != worktree_missing:
            raise GateError("Project worktree registry binding is incomplete")
        snapshot = original
        if branch_missing:
            snapshot = replace(
                original,
                branch_name=planned.branch_name,
                worktree_path=planned.worktree_path.as_posix(),
            )
        elif (
            original.branch_name != planned.branch_name
            or original.worktree_path != planned.worktree_path.as_posix()
        ):
            raise GateError("Project worktree registry is not deterministic")
        project_cards = _project_cards(all_cards, snapshot)
        with read_registry.guard(original):
            replay = _replay_project_flow(
                snapshot,
                project_cards,
                hermes=store,
                github=github,
            )
        if replay.expected_head_commit is None and branch_missing:
            raise GateError("active Project card has no recorded worktree")
        with read_registry.guard(original):
            verified = _read_project_worktree(
                worktrees,
                snapshot.request.request_id,
                snapshot.project,
                expected_head_commit=replay.expected_head_commit,
                dry_run=True,
            )
        if verified != planned:
            raise GateError("Project worktree readback changed")
        preflight.append(_ProjectPreflight(original, snapshot, verified, replay))

    registry: _ProjectRuntimeRegistry | None = None
    materialized: list[
        tuple[ProjectRuntimeSnapshot, TaskWorktree, _ProjectReplay]
    ] = []
    if dry_run:
        materialized.extend(
            (item.snapshot, item.worktree, item.replay) for item in preflight
        )
    else:
        registry = _ProjectRuntimeRegistry(settings_db)
        if registry.list_active() != snapshots:
            raise GateError("v2 Project registry changed after preflight")
        for item in preflight:
            snapshot = item.snapshot
            prepared = item.worktree
            if item.original.branch_name is None:
                with registry.guard(item.original) as connection:
                    prepared = _read_project_worktree(
                        worktrees,
                        snapshot.request.request_id,
                        snapshot.project,
                        expected_head_commit=item.replay.expected_head_commit,
                        dry_run=False,
                    )
                    if prepared != item.worktree:
                        raise GateError("Project worktree changed after preflight")
                    snapshot = registry.record_worktree(
                        connection,
                        item.original,
                        prepared,
                    )
            else:
                with registry.guard(item.original):
                    prepared = _read_project_worktree(
                        worktrees,
                        snapshot.request.request_id,
                        snapshot.project,
                        expected_head_commit=item.replay.expected_head_commit,
                        dry_run=True,
                    )
                if prepared != item.worktree:
                    raise GateError("Project worktree changed after preflight")
            materialized.append((snapshot, prepared, item.replay))

    reports: list[ProjectWorkerReport] = []
    for snapshot, prepared, replay in materialized:
        spec = replay.next_spec
        if spec is not None and not dry_run:
            assert registry is not None
            with registry.guard(snapshot):
                if not store.has_project_idempotency_key(spec.idempotency_key):
                    create(build_project_create_argv(spec, prepared.worktree_path))
        reports.append(
            ProjectWorkerReport(
                request_id=snapshot.request.request_id,
                project_id=snapshot.project.project_id,
                repository=snapshot.project.repository,
                parent_issue_number=snapshot.settings.parent_issue_number,
                status=(
                    replay.status
                    if spec is None
                    else ("planned" if dry_run else "created")
                ),
                card_key=None if spec is None else spec.idempotency_key,
            )
        )
    return tuple(reports)
