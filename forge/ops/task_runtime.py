"""Rebuild and advance confirmed Task flows from durable external evidence."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
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
from .github import GitHubClient, GitHubTaskIssueClient, PullRequestWriteState
from .hermes import (
    GateError,
    HermesCreateCommand,
    HermesStore,
    HermesTaskCard,
    RootTaskCardSpec,
    build_create_argv,
    build_root_create_argv,
    parse_task_card_key,
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


ROOT_CARD_FORMAT = "forge-task-card/v1"
STEP_CARD_FORMAT = "forge-step-card/v1"
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
