"""Pure, fail-closed decision for manual, safe, and full PR merging."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .safe_files import (
    AUTO_MERGE_ALLOWED as SAFE_FILES_ALLOWED,
    CHECK_ERROR as SAFE_FILES_ERROR,
    MANUAL_MERGE_REQUIRED as SAFE_FILES_MANUAL,
    SafeFilesResult,
)
from .task_options import MergeMode
from .task_settings import TaskSettings, TaskSettingsStatus


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
    head_commit: str
    is_open: bool
    is_draft: bool
    is_merged: bool
    merged_commit: str | None
    has_conflict: bool
    base_is_current: bool
    rules_allow_merge: bool
    unresolved_review_threads: int
    eval_status: str
    eval_commit: str
    eval_check_count: int


@dataclass(frozen=True, slots=True)
class MergeContext:
    settings: TaskSettings
    repository: str
    issue_number: int | None
    task_content_hash: str
    proof_settings_hashes: tuple[str | None, ...]
    flow_completed: bool
    final_tested_commit: str
    pull_request: MergePullRequest
    displayed_status: str
    safe_files: SafeFilesResult | None
    now: datetime
    branch_refresh_count: int


@dataclass(frozen=True, slots=True)
class MergeDecision:
    code: str
    reason: str
    tested_commit: str | None = None
    already_merged: bool = False


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
    if not context.proof_settings_hashes or any(
        value != settings.task_settings_hash
        for value in context.proof_settings_hashes
    ):
        return _check_error("step proof settings hash does not match")
    if not isinstance(context.flow_completed, bool) or not context.flow_completed:
        return _check_error("selected Task flow is not complete")
    if (
        not isinstance(context.final_tested_commit, str)
        or _COMMIT_PATTERN.fullmatch(context.final_tested_commit) is None
    ):
        return _check_error("final tested commit is invalid")
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
    if pr.is_merged and pr.is_open:
        return _check_error("pull request open and merged flags conflict")
    if not pr.is_merged and pr.merged_commit is not None:
        return _check_error("unmerged pull request has a merged commit")
    pr_match = (
        _PR_URL_PATTERN.fullmatch(pr.pr_url)
        if isinstance(pr.pr_url, str)
        else None
    )
    if pr_match is None or pr_match.group("repository") != context.repository:
        return _check_error("pull request URL does not match repository")
    if pr.repository != context.repository:
        return _check_error("pull request repository does not match")
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

    if pr.head_commit != context.final_tested_commit:
        return _decision(
            RESTART_FLOW,
            "pull request commit changed after the selected Task flow",
            commit=pr.head_commit,
        )

    if pr.is_merged:
        if pr.merged_commit != context.final_tested_commit:
            return _check_error("pull request merged commit does not match tested commit")
        return _decision(
            AUTO_MERGE_ALLOWED,
            "the tested pull request commit is already merged",
            commit=context.final_tested_commit,
            already_merged=True,
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
        result = context.safe_files
        if not isinstance(result, SafeFilesResult):
            return _check_error("safe files result is unavailable")
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
