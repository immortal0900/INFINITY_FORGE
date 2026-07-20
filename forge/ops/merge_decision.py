"""Pure, fail-closed decision for manual, safe, and full PR merging."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .safe_files import (
    AUTO_MERGE_ALLOWED as SAFE_FILES_ALLOWED,
    CHECK_ERROR as SAFE_FILES_ERROR,
    MANUAL_MERGE_REQUIRED as SAFE_FILES_MANUAL,
    SafeFilesEvidence,
    SafeFilesResult,
)
from .task_options import MergeMode
from .task_flow import TaskFlowState, TaskFlowStatus, required_steps
from .task_settings import TaskSettings, TaskSettingsStatus
from .task_settings_v2 import TaskSettingsV2


AUTO_MERGE_ALLOWED = "AUTO_MERGE_ALLOWED"
MANUAL_MERGE_REQUIRED = "MANUAL_MERGE_REQUIRED"
REFRESH_BRANCH = "REFRESH_BRANCH"
RESTART_FLOW = "RESTART_FLOW"
WAIT = "WAIT"
CHECK_ERROR = "CHECK_ERROR"

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_PR_URL_PATTERN = re.compile(
    r"^https://github\.com/(?P<repository>[^/\s]+/[^/\s]+)/pull/[1-9][0-9]*$"
)
_BLOCKED_STATUSES = frozenset(
    {
        "forge:waiting-for-help",
        "forge:failed",
        "forge:needs-decision",
    }
)


@dataclass(frozen=True, slots=True)
class MergePullRequest:
    pr_url: str
    repository: str
    # RISK(breaking): adapters must provide both current and merged base commits.
    base_commit: str
    head_commit: str
    is_open: bool
    is_draft: bool
    is_merged: bool
    merged_commit: str | None
    merged_base_commit: str | None
    merged_head_commit: str | None
    has_conflict: bool
    base_is_current: bool
    rules_allow_merge: bool
    unresolved_review_threads: int
    eval_status: str
    eval_commit: str
    eval_check_count: int


# RISK(breaking): loose completion/hash fields were intentionally replaced by
# one complete TaskFlowState, so old MergeContext constructors must be updated.
@dataclass(frozen=True, slots=True)
class MergeContext:
    settings: TaskSettings
    repository: str
    issue_number: int | None
    task_content_hash: str
    task_flow_state: TaskFlowState
    pull_request: MergePullRequest
    displayed_status: str
    safe_files: SafeFilesEvidence | None
    now: datetime
    branch_refresh_count: int


@dataclass(frozen=True, slots=True)
class MergeDecision:
    code: str
    reason: str
    tested_commit: str | None = None
    already_merged: bool = False


@dataclass(frozen=True, slots=True)
class ProjectMergeProof:
    """One Project's immutable result before the parent merge barrier opens."""

    project_id: str
    repository: str
    decision: str
    expected_head_commit: str
    already_merged: bool = False


@dataclass(frozen=True, slots=True)
class ProjectGroupDecision:
    """Parent-level decision containing only a confirmed full-auto order."""

    code: str
    reason: str
    ordered_project_ids: tuple[str, ...] = ()


def decide_project_group(
    settings: TaskSettingsV2,
    proofs: tuple[ProjectMergeProof, ...],
) -> ProjectGroupDecision:
    """Open a multi-Project barrier only after every exact proof is current."""

    if not isinstance(settings, TaskSettingsV2):
        return ProjectGroupDecision(CHECK_ERROR, "v2 Task settings are invalid")
    if not isinstance(proofs, tuple) or any(
        not isinstance(proof, ProjectMergeProof) for proof in proofs
    ):
        return ProjectGroupDecision(CHECK_ERROR, "Project proofs must be a tuple")
    expected = {project.project_id: project for project in settings.projects}
    if len(proofs) != len(expected) or {proof.project_id for proof in proofs} != set(
        expected
    ):
        return ProjectGroupDecision(
            WAIT,
            "every Project must have one current merge proof",
        )
    if len({proof.project_id for proof in proofs}) != len(proofs):
        return ProjectGroupDecision(CHECK_ERROR, "Project merge proofs are duplicated")
    for proof in proofs:
        project = expected[proof.project_id]
        if (
            proof.repository != project.repository
            or _COMMIT_PATTERN.fullmatch(proof.expected_head_commit) is None
            or type(proof.already_merged) is not bool
        ):
            return ProjectGroupDecision(
                CHECK_ERROR,
                "Project merge proof does not match exact settings",
            )
    if settings.merge_mode is MergeMode.MANUAL:
        return ProjectGroupDecision(
            MANUAL_MERGE_REQUIRED,
            "this Task requires a person to merge every Project",
        )
    if len(settings.projects) > 1 and settings.merge_mode is MergeMode.SAFE_AUTO:
        return ProjectGroupDecision(
            MANUAL_MERGE_REQUIRED,
            "multi-Project safe_auto requires a person to merge",
        )
    blocked = tuple(proof for proof in proofs if proof.decision != AUTO_MERGE_ALLOWED)
    if blocked:
        return ProjectGroupDecision(
            WAIT,
            "every Project must pass current merge checks before any merge",
        )
    if len(settings.projects) == 1:
        return ProjectGroupDecision(
            AUTO_MERGE_ALLOWED,
            "the Project passed every current merge check",
            (settings.projects[0].project_id,),
        )
    order = settings.merge_order
    if order is None or set(order) != set(expected) or len(order) != len(expected):
        return ProjectGroupDecision(
            MANUAL_MERGE_REQUIRED,
            "multi-Project full_auto has no exact confirmed merge order",
        )
    return ProjectGroupDecision(
        AUTO_MERGE_ALLOWED,
        "every Project passed; use the confirmed dependency order",
        order,
    )


def _decision(
    code: str,
    reason: str,
    *,
    commit: str | None = None,
    already_merged: bool = False,
) -> MergeDecision:
    return MergeDecision(
        code=code,
        reason=reason,
        tested_commit=commit,
        already_merged=already_merged,
    )


def _check_error(reason: str) -> MergeDecision:
    return _decision(CHECK_ERROR, reason)


def decide_merge(context: MergeContext) -> MergeDecision:
    """Return a deterministic result without making any external write."""

    if not isinstance(context, MergeContext):
        return _check_error("merge context has an unexpected type")
    settings = context.settings
    pr = context.pull_request
    if not isinstance(settings, TaskSettings):
        return _check_error("Task settings have an unexpected type")
    if settings.status is not TaskSettingsStatus.ACTIVE:
        return _check_error("Task settings are not active")
    if settings.task_settings_hash is None or (
        _SHA256_PATTERN.fullmatch(settings.task_settings_hash) is None
    ):
        return _check_error("Task settings hash is unavailable")
    if context.repository != settings.repository:
        return _check_error("repository does not match Task settings")
    if context.issue_number != settings.issue_number:
        return _check_error("issue does not match Task settings")
    if context.task_content_hash != settings.task_content_hash:
        return _check_error("Task content hash does not match")
    flow = context.task_flow_state
    if not isinstance(flow, TaskFlowState):
        return _check_error("Task flow state has an unexpected type")
    if flow.task_flow is not settings.task_flow:
        return _check_error("Task flow does not match Task settings")
    if flow.task_settings_hash != settings.task_settings_hash:
        return _check_error("Task flow settings hash does not match")
    if flow.status is not TaskFlowStatus.READY_TO_MERGE:
        return _check_error("selected Task flow is not ready to merge")
    if flow.completed_steps != required_steps(settings.task_flow):
        return _check_error("selected Task flow steps are not exactly complete")
    if flow.current_step is not None:
        return _check_error("selected Task flow still has a current step")
    if flow.step_running is not False:
        return _check_error("selected Task flow still has a running step")
    if (
        _COMMIT_PATTERN.fullmatch(flow.current_base_commit) is None
        or _COMMIT_PATTERN.fullmatch(flow.current_commit) is None
    ):
        return _check_error("Task flow base or head commit is invalid")
    if not isinstance(pr, MergePullRequest):
        return _check_error("pull request data has an unexpected type")
    if any(
        not isinstance(value, bool)
        for value in (
            pr.is_open,
            pr.is_draft,
            pr.is_merged,
            pr.has_conflict,
            pr.base_is_current,
            pr.rules_allow_merge,
        )
    ):
        return _check_error("pull request flags must be true or false")
    if not isinstance(pr.eval_status, str):
        return _check_error("eval check status is invalid")
    if (
        not isinstance(pr.eval_commit, str)
        or _COMMIT_PATTERN.fullmatch(pr.eval_commit) is None
    ):
        return _check_error("eval check commit is invalid")
    if pr.merged_commit is not None and (
        not isinstance(pr.merged_commit, str)
        or _COMMIT_PATTERN.fullmatch(pr.merged_commit) is None
    ):
        return _check_error("pull request merged commit is invalid")
    if pr.merged_base_commit is not None and (
        not isinstance(pr.merged_base_commit, str)
        or _COMMIT_PATTERN.fullmatch(pr.merged_base_commit) is None
    ):
        return _check_error("pull request merged base is invalid")
    if pr.merged_head_commit is not None and (
        not isinstance(pr.merged_head_commit, str)
        or _COMMIT_PATTERN.fullmatch(pr.merged_head_commit) is None
    ):
        return _check_error("pull request merged head is invalid")
    if pr.is_merged and pr.is_open:
        return _check_error("pull request open and merged flags conflict")
    if not pr.is_merged and (
        pr.merged_commit is not None
        or pr.merged_base_commit is not None
        or pr.merged_head_commit is not None
    ):
        return _check_error("unmerged pull request has merged commit data")
    pr_match = (
        _PR_URL_PATTERN.fullmatch(pr.pr_url)
        if isinstance(pr.pr_url, str)
        else None
    )
    if pr_match is None or pr_match.group("repository") != context.repository:
        return _check_error("pull request URL does not match repository")
    if flow.pr_url != pr.pr_url:
        return _check_error("Task flow pull request does not match")
    if pr.repository != context.repository:
        return _check_error("pull request repository does not match")
    if (
        not isinstance(pr.base_commit, str)
        or _COMMIT_PATTERN.fullmatch(pr.base_commit) is None
    ):
        return _check_error("pull request base commit is invalid")
    if (
        not isinstance(pr.head_commit, str)
        or _COMMIT_PATTERN.fullmatch(pr.head_commit) is None
    ):
        return _check_error("pull request commit is invalid")
    if not isinstance(context.displayed_status, str):
        return _check_error("Task displayed status is invalid")
    if context.displayed_status in _BLOCKED_STATUSES:
        return _check_error("Task status blocks merging")
    if context.displayed_status != "forge:ready-to-merge":
        return _check_error("Task is not ready to merge")
    if not isinstance(context.now, datetime) or context.now.tzinfo is None:
        return _check_error("merge decision time must include a timezone")
    if (
        type(context.branch_refresh_count) is not int
        or context.branch_refresh_count < 0
    ):
        return _check_error("branch refresh count is invalid")

    if pr.is_merged:
        if pr.merged_commit is None:
            return _check_error("merged pull request has no result commit")
        if pr.merged_base_commit != flow.current_base_commit:
            return _check_error(
                "pull request merged base does not match tested base"
            )
        if pr.merged_head_commit != flow.current_commit:
            return _check_error(
                "pull request merged head does not match tested commit"
            )
        return _decision(
            AUTO_MERGE_ALLOWED,
            "the tested pull request base and head are already merged",
            commit=flow.current_commit,
            already_merged=True,
        )

    if pr.base_commit != flow.current_base_commit:
        return _decision(
            RESTART_FLOW,
            "pull request base changed after the selected Task flow",
            commit=pr.head_commit,
        )
    if pr.head_commit != flow.current_commit:
        return _decision(
            RESTART_FLOW,
            "pull request commit changed after the selected Task flow",
            commit=pr.head_commit,
        )

    if not pr.is_open:
        return _decision(MANUAL_MERGE_REQUIRED, "pull request is not open")
    if pr.is_draft:
        return _decision(MANUAL_MERGE_REQUIRED, "pull request is still a draft")
    if type(pr.eval_check_count) is not int or pr.eval_check_count != 1:
        return _check_error("eval check must appear exactly once")
    if pr.eval_commit != pr.head_commit:
        return _check_error("eval check is not for the current pull request commit")
    if pr.eval_status in {"queued", "in_progress"}:
        return _decision(WAIT, "eval check is still running", commit=pr.head_commit)
    if pr.eval_status != "success":
        return _check_error("eval check did not succeed")
    if pr.has_conflict:
        return _decision(MANUAL_MERGE_REQUIRED, "pull request has a conflict")
    if (
        type(pr.unresolved_review_threads) is not int
        or pr.unresolved_review_threads < 0
    ):
        return _check_error("unresolved review thread count is invalid")
    if pr.unresolved_review_threads:
        return _decision(
            MANUAL_MERGE_REQUIRED,
            "pull request has unresolved review threads",
        )

    if settings.merge_mode is MergeMode.MANUAL:
        return _decision(
            MANUAL_MERGE_REQUIRED,
            "this Task requires a person to merge",
            commit=pr.head_commit,
        )
    expires_at = settings.auto_merge_expires_at
    if expires_at is None:
        return _check_error("automatic merge permission has no expiry")
    if context.now >= expires_at:
        return _decision(
            MANUAL_MERGE_REQUIRED,
            "automatic merge permission expired",
            commit=pr.head_commit,
        )
    if not pr.base_is_current:
        if context.branch_refresh_count >= 3:
            return _decision(
                MANUAL_MERGE_REQUIRED,
                "branch refresh limit was reached",
                commit=pr.head_commit,
            )
        return _decision(
            REFRESH_BRANCH,
            "pull request branch must include the current base branch",
            commit=pr.head_commit,
        )
    if not pr.rules_allow_merge:
        return _decision(
            MANUAL_MERGE_REQUIRED,
            "GitHub rules do not currently allow merging",
        )

    if settings.merge_mode is MergeMode.SAFE_AUTO:
        evidence = context.safe_files
        if not isinstance(evidence, SafeFilesEvidence):
            return _check_error("safe files evidence is unavailable")
        if (
            evidence.base_commit != pr.base_commit
            or evidence.head_commit != pr.head_commit
        ):
            return _check_error(
                "safe files evidence does not match the tested base and head"
            )
        result = evidence.result
        if not isinstance(result, SafeFilesResult):
            return _check_error("safe files result has an unexpected type")
        if result.code == SAFE_FILES_ERROR:
            return _check_error("safe files check could not be completed")
        if result.code == SAFE_FILES_MANUAL:
            return _decision(
                MANUAL_MERGE_REQUIRED,
                "changed files require a person to merge",
                commit=pr.head_commit,
            )
        if result.code != SAFE_FILES_ALLOWED:
            return _check_error("safe files result has an unknown value")

    return _decision(
        AUTO_MERGE_ALLOWED,
        "all checks allow expected-commit merging",
        commit=pr.head_commit,
    )
