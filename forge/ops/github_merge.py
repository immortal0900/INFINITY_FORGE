"""Expected-commit GitHub writes and branch-refresh restart instructions."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .github import (
    GitHubClient,
    PullRequestWriteState,
    parse_pull_request_url,
    validate_commit_sha,
)
from .hermes import GateError
from .merge_decision import MANUAL_MERGE_REQUIRED, RESTART_FLOW


MAX_BRANCH_REFRESH_COUNT = 3
BUILD_STEP = "build"


@dataclass(frozen=True, slots=True)
class MergeWriteResult:
    expected_commit: str
    expected_base_commit: str
    merged_commit: str
    merged_base_commit: str
    merged_head_commit: str
    already_merged: bool
    recovered_by_readback: bool


@dataclass(frozen=True, slots=True)
class BranchRefreshResult:
    code: str
    reason: str
    current_commit: str
    current_base_commit: str
    branch_refresh_count: int
    next_step: str | None
    invalidate_existing_proofs: bool
    flow_completed: bool
    final_tested_commit: str | None


class GitHubMergeClient:
    """Perform no GitHub write without binding it to the current PR commit."""

    def __init__(
        self,
        gh_path: str | Path,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        sleeper: Callable[[float], None] = time.sleep,
        refresh_delays: tuple[float, ...] = (0.0, 0.5, 2.0, 5.0),
    ) -> None:
        self._gh_path = str(Path(gh_path).expanduser())
        self._runner = runner
        self._reader = GitHubClient(self._gh_path, runner=runner)
        self._sleeper = sleeper
        if not refresh_delays or any(
            not isinstance(delay, (int, float))
            or isinstance(delay, bool)
            or delay < 0
            for delay in refresh_delays
        ):
            raise ValueError("refresh_delays must contain non-negative numbers")
        self._refresh_delays = tuple(float(delay) for delay in refresh_delays)

    def _run_write(
        self,
        argv: list[str],
    ) -> subprocess.CompletedProcess[str] | None:
        try:
            return self._runner(
                argv,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            # A process error does not prove that the remote write did not happen.
            return None

    def _read_after_write(
        self,
        pr_url: str,
        operation: str,
    ) -> PullRequestWriteState:
        try:
            return self._reader.get_pr_write_state(pr_url)
        except Exception as error:
            raise GateError(
                f"GitHub {operation} result is ambiguous and readback failed"
            ) from error

    def merge_expected_commit(
        self,
        pr_url: str,
        expected_commit: str,
        *,
        expected_base_commit: str,
    ) -> MergeWriteResult:
        """Immediately merge only one exact validated base/head pair."""

        expected_commit = validate_commit_sha(expected_commit, "expected commit")
        expected_base_commit = validate_commit_sha(
            expected_base_commit,
            "expected base commit",
        )
        repository, pr_number = parse_pull_request_url(pr_url)
        before = self._reader.get_pr_write_state(pr_url)
        if before.is_merged:
            if (
                before.merged_commit is None
                or before.merged_base_commit != expected_base_commit
                or before.merged_head_commit != expected_commit
            ):
                raise GateError(
                    "GitHub merged PR does not match the expected base and head"
                )
            return MergeWriteResult(
                expected_commit=expected_commit,
                expected_base_commit=expected_base_commit,
                merged_commit=before.merged_commit,
                merged_base_commit=before.merged_base_commit,
                merged_head_commit=before.merged_head_commit,
                already_merged=True,
                recovered_by_readback=False,
            )
        if before.head_commit != expected_commit:
            raise GateError("GitHub PR does not match the expected commit")
        if before.base_commit != expected_base_commit:
            raise GateError("GitHub PR does not match the expected base commit")
        if not before.is_open:
            raise GateError("GitHub PR is not open for merging")

        # RISK(race): this exact SHA is the commit approved by the pure decision.
        # RISK(side-effect): use the immediate REST merge endpoint. ``gh pr
        # merge`` can silently enable deferred auto-merge on merge queues even
        # without ``--auto``, which would outlive this validation decision.
        write = self._run_write(
            [
                self._gh_path,
                "api",
                "-X",
                "PUT",
                f"repos/{repository}/pulls/{pr_number}/merge",
                "-f",
                f"sha={expected_commit}",
                "-f",
                "merge_method=merge",
            ]
        )
        after = self._read_after_write(pr_url, "merge")
        if not after.is_merged:
            if after.head_commit != expected_commit:
                raise GateError("GitHub PR commit changed during merge")
            if after.is_open:
                raise GateError("GitHub PR remains open after merge request")
            raise GateError("GitHub PR closed without a verified merge")
        if (
            after.merged_commit is None
            or after.merged_base_commit != expected_base_commit
            or after.merged_head_commit != expected_commit
        ):
            raise GateError(
                "GitHub merge result does not match the expected base and head"
            )
        return MergeWriteResult(
            expected_commit=expected_commit,
            expected_base_commit=expected_base_commit,
            merged_commit=after.merged_commit,
            merged_base_commit=after.merged_base_commit,
            merged_head_commit=after.merged_head_commit,
            already_merged=False,
            recovered_by_readback=write is None or write.returncode != 0,
        )

    def update_branch(
        self,
        pr_url: str,
        *,
        expected_commit: str,
        expected_base_commit: str,
    ) -> PullRequestWriteState:
        """Update the base only when the PR still has the expected current commit."""

        expected_commit = validate_commit_sha(expected_commit, "expected commit")
        expected_base_commit = validate_commit_sha(
            expected_base_commit,
            "expected base commit",
        )
        repository, pr_number = parse_pull_request_url(pr_url)
        before = self._reader.get_pr_write_state(pr_url)
        if before.head_commit != expected_commit:
            raise GateError("GitHub PR does not match the expected commit")
        if before.base_commit != expected_base_commit:
            raise GateError("GitHub PR does not match the expected base commit")
        if before.is_merged or not before.is_open:
            raise GateError("GitHub PR is not open for branch refresh")

        write = self._run_write(
            [
                self._gh_path,
                "api",
                "-X",
                "PUT",
                f"repos/{repository}/pulls/{pr_number}/update-branch",
                "-f",
                f"expected_head_sha={expected_commit}",
            ]
        )
        # GitHub accepts update-branch asynchronously. Poll a small bounded
        # schedule and never issue a second write after an ambiguous response.
        last_error: Exception | None = None
        for delay in self._refresh_delays:
            if delay:
                self._sleeper(delay)
            try:
                after = self._read_after_write(pr_url, "branch refresh")
            except GateError as error:
                last_error = error
                continue
            if after.is_merged or not after.is_open:
                raise GateError("GitHub PR closed during branch refresh")
            if after.head_commit != expected_commit:
                return after
        if last_error is not None:
            raise GateError("GitHub branch refresh readback never completed") from last_error
        raise GateError("GitHub branch refresh did not change the current commit")

    def refresh_branch(
        self,
        pr_url: str,
        *,
        expected_commit: str,
        expected_base_commit: str,
        branch_refresh_count: int,
    ) -> BranchRefreshResult:
        """Refresh at most three times, then restart validation from Build."""

        if type(branch_refresh_count) is not int or branch_refresh_count < 0:
            raise GateError("GitHub branch refresh count is invalid")
        expected_commit = validate_commit_sha(expected_commit, "expected commit")
        expected_base_commit = validate_commit_sha(
            expected_base_commit,
            "expected base commit",
        )
        parse_pull_request_url(pr_url)
        if branch_refresh_count >= MAX_BRANCH_REFRESH_COUNT:
            return BranchRefreshResult(
                code=MANUAL_MERGE_REQUIRED,
                reason="branch refresh limit was reached",
                current_commit=expected_commit,
                current_base_commit=expected_base_commit,
                branch_refresh_count=branch_refresh_count,
                next_step=None,
                invalidate_existing_proofs=False,
                flow_completed=True,
                final_tested_commit=expected_commit,
            )

        updated = self.update_branch(
            pr_url,
            expected_commit=expected_commit,
            expected_base_commit=expected_base_commit,
        )
        return BranchRefreshResult(
            code=RESTART_FLOW,
            reason="branch was refreshed; restart validation from Build",
            current_commit=updated.head_commit,
            current_base_commit=updated.base_commit,
            branch_refresh_count=branch_refresh_count + 1,
            next_step=BUILD_STEP,
            invalidate_existing_proofs=True,
            flow_completed=False,
            final_tested_commit=None,
        )


__all__ = [
    "BUILD_STEP",
    "MANUAL_MERGE_REQUIRED",
    "MAX_BRANCH_REFRESH_COUNT",
    "RESTART_FLOW",
    "BranchRefreshResult",
    "GitHubMergeClient",
    "MergeWriteResult",
]
