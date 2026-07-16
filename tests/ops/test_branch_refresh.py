from __future__ import annotations

import json
import subprocess

import pytest

from forge.ops.github_merge import (
    MANUAL_MERGE_REQUIRED,
    RESTART_FLOW,
    GitHubMergeClient,
)
from forge.ops.hermes import GateError


PR_URL = "https://github.com/owner/repo/pull/17"
HEAD = "a" * 40
NEW_HEAD = "b" * 40
BASE = "c" * 40
NEW_BASE = "d" * 40


def _state(head: str, *, base: str = BASE) -> dict[str, object]:
    return {
        "number": 17,
        "html_url": PR_URL,
        "state": "open",
        "merged": False,
        "merge_commit_sha": None,
        "head": {"sha": head},
        "base": {"sha": base, "ref": "main"},
    }


class BranchRunner:
    def __init__(
        self,
        states: list[dict[str, object]],
        *,
        update_returncode: int = 0,
        update_stdout: str | None = None,
    ) -> None:
        self.states = list(states)
        self.update_returncode = update_returncode
        self.update_stdout = update_stdout or json.dumps({"message": "Updating pull request branch."})
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        call = list(argv)
        self.calls.append(call)
        if call[1] == "api" and "-X" not in call:
            return subprocess.CompletedProcess(
                call,
                0,
                json.dumps(self.states.pop(0)),
                "",
            )
        if call[1:4] == ["api", "-X", "PUT"]:
            return subprocess.CompletedProcess(
                call,
                self.update_returncode,
                self.update_stdout,
                "failure",
            )
        raise AssertionError(f"unexpected GitHub command: {call}")


def test_branch_refresh_uses_expected_head_and_restarts_build() -> None:
    runner = BranchRunner([_state(HEAD), _state(NEW_HEAD)])

    result = GitHubMergeClient(
        "gh", runner=runner, refresh_delays=(0,)
    ).refresh_branch(
        PR_URL,
        expected_commit=HEAD,
        expected_base_commit=BASE,
        branch_refresh_count=0,
    )

    update_call = next(call for call in runner.calls if "PUT" in call)
    assert update_call == [
        "gh",
        "api",
        "-X",
        "PUT",
        "repos/owner/repo/pulls/17/update-branch",
        "-f",
        f"expected_head_sha={HEAD}",
    ]
    assert result.code == RESTART_FLOW
    assert result.current_commit == NEW_HEAD
    assert result.current_base_commit == BASE
    assert result.branch_refresh_count == 1
    assert result.next_step == "build"
    assert result.invalidate_existing_proofs is True
    assert result.flow_completed is False
    assert result.final_tested_commit is None


def test_fourth_branch_refresh_requires_a_person_without_a_write() -> None:
    runner = BranchRunner([])

    result = GitHubMergeClient("gh", runner=runner).refresh_branch(
        PR_URL,
        expected_commit=HEAD,
        expected_base_commit=BASE,
        branch_refresh_count=3,
    )

    assert result.code == MANUAL_MERGE_REQUIRED
    assert result.branch_refresh_count == 3
    assert result.next_step is None
    assert result.invalidate_existing_proofs is False
    assert runner.calls == []


def test_update_branch_reads_new_commit_after_ambiguous_write() -> None:
    runner = BranchRunner(
        [_state(HEAD), _state(NEW_HEAD)],
        update_returncode=1,
        update_stdout="request timed out",
    )

    result = GitHubMergeClient(
        "gh", runner=runner, refresh_delays=(0,)
    ).refresh_branch(
        PR_URL,
        expected_commit=HEAD,
        expected_base_commit=BASE,
        branch_refresh_count=1,
    )

    assert result.code == RESTART_FLOW
    assert result.current_commit == NEW_HEAD
    assert result.branch_refresh_count == 2
    assert len([call for call in runner.calls if "PUT" in call]) == 1


def test_unchanged_head_after_update_is_a_check_error() -> None:
    runner = BranchRunner([_state(HEAD), _state(HEAD)])

    with pytest.raises(GateError, match="did not change"):
        GitHubMergeClient(
            "gh", runner=runner, refresh_delays=(0,)
        ).refresh_branch(
            PR_URL,
            expected_commit=HEAD,
            expected_base_commit=BASE,
            branch_refresh_count=0,
        )


def test_changed_head_before_update_stops_without_a_write() -> None:
    runner = BranchRunner([_state(NEW_HEAD)])

    with pytest.raises(GateError, match="expected commit"):
        GitHubMergeClient(
            "gh", runner=runner, refresh_delays=(0,)
        ).refresh_branch(
            PR_URL,
            expected_commit=HEAD,
            expected_base_commit=BASE,
            branch_refresh_count=0,
        )

    assert all("PUT" not in call for call in runner.calls)


@pytest.mark.parametrize("count", [-1, True, "1"])
def test_invalid_branch_refresh_count_fails_before_github(
    count: object,
) -> None:
    runner = BranchRunner([])

    with pytest.raises(GateError, match="refresh count"):
        GitHubMergeClient("gh", runner=runner).refresh_branch(
            PR_URL,
            expected_commit=HEAD,
            expected_base_commit=BASE,
            branch_refresh_count=count,  # type: ignore[arg-type]
        )

    assert runner.calls == []


def test_update_branch_polls_until_github_publishes_the_new_commit() -> None:
    runner = BranchRunner(
        [_state(HEAD), _state(HEAD), _state(HEAD), _state(NEW_HEAD)]
    )
    delays: list[float] = []

    result = GitHubMergeClient(
        "gh",
        runner=runner,
        sleeper=delays.append,
        refresh_delays=(0, 0.5, 2.0),
    ).refresh_branch(
        PR_URL,
        expected_commit=HEAD,
        expected_base_commit=BASE,
        branch_refresh_count=0,
    )

    assert result.current_commit == NEW_HEAD
    assert delays == [0.5, 2.0]
    assert len([call for call in runner.calls if "PUT" in call]) == 1


def test_changed_base_before_branch_refresh_stops_without_a_write() -> None:
    runner = BranchRunner([_state(HEAD, base=NEW_BASE)])

    with pytest.raises(GateError, match="expected base"):
        GitHubMergeClient(
            "gh", runner=runner, refresh_delays=(0,)
        ).refresh_branch(
            PR_URL,
            expected_commit=HEAD,
            expected_base_commit=BASE,
            branch_refresh_count=0,
        )

    assert all("PUT" not in call for call in runner.calls)
