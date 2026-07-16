from __future__ import annotations

import base64
import json
import subprocess

import pytest

from forge.ops.github import GitHubClient
from forge.ops.github_merge import GitHubMergeClient
from forge.ops.hermes import GateError
from forge.ops.safe_files import CHECK_ERROR as SAFE_FILES_ERROR


PR_URL = "https://github.com/owner/repo/pull/17"
BASE = "b" * 40
HEAD = "a" * 40
MERGE_COMMIT = "c" * 40
BLOB = "d" * 40
NEW_HEAD = "e" * 40


def _completed(
    argv: list[str],
    payload: object,
    *,
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    stdout = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.CompletedProcess(argv, returncode, stdout, "failure")


class EvidenceRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.changed_file_count = 1
        self.file_has_patch = True
        self.tree_truncated = False
        self.check_total = 1
        self.review_has_next_page = False
        self.mergeable_state = "clean"
        self.mergeable: bool | None = True
        self.merged = False
        self.head_commit = HEAD
        self.strict_base = True
        self.conversations_must_be_resolved = True
        self.required_checks = ("eval",)
        self.review_errors = False
        self.invalid_json_endpoint: str | None = None

    def __call__(self, argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        call = list(argv)
        self.calls.append(call)
        if call[1:3] == ["api", "graphql"]:
            page: dict[str, object] = {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": [],
                                    "pageInfo": {
                                        "hasNextPage": self.review_has_next_page,
                                        "endCursor": (
                                            "cursor-1"
                                            if self.review_has_next_page
                                            else None
                                        ),
                                    },
                                }
                            }
                        }
                    }
                }
            if self.review_errors:
                page["errors"] = [{"message": "partial review data"}]
            payload: object = [page]
            return _completed(call, payload)

        endpoint = next(
            (part for part in call if part.startswith("repos/owner/repo/")),
            "",
        )
        if endpoint == self.invalid_json_endpoint:
            return _completed(call, "not-json")
        if endpoint == "repos/owner/repo/pulls/17":
            return _completed(
                call,
                {
                    "number": 17,
                    "html_url": PR_URL,
                    "state": "closed" if self.merged else "open",
                    "draft": False,
                    "merged": self.merged,
                    "merge_commit_sha": MERGE_COMMIT if self.merged else None,
                    "mergeable": self.mergeable,
                    "mergeable_state": self.mergeable_state,
                    "changed_files": self.changed_file_count,
                    "head": {"sha": self.head_commit},
                    "base": {"sha": BASE, "ref": "main"},
                },
            )
        if endpoint == "repos/owner/repo/branches/main/protection":
            return _completed(
                call,
                {
                    "required_status_checks": {
                        "strict": self.strict_base,
                        "contexts": list(self.required_checks),
                        "checks": [
                            {"context": name, "app_id": 1}
                            for name in self.required_checks
                        ],
                    },
                    "required_conversation_resolution": {
                        "enabled": self.conversations_must_be_resolved,
                    },
                },
            )
        if endpoint == f"repos/owner/repo/git/commits/{MERGE_COMMIT}":
            return _completed(
                call,
                {
                    "sha": MERGE_COMMIT,
                    "parents": [
                        {"sha": BASE},
                        {"sha": HEAD},
                    ],
                },
            )
        if endpoint == "repos/owner/repo/pulls/17/files?per_page=100":
            changed = {
                "filename": "docs/guide.md",
                "status": "added",
                "sha": BLOB,
            }
            if self.file_has_patch:
                changed["patch"] = "@@ -0,0 +1 @@\n+# Guide"
            return _completed(call, [[changed]])
        if endpoint in {
            f"repos/owner/repo/git/trees/{BASE}?recursive=1",
            f"repos/owner/repo/git/trees/{HEAD}?recursive=1",
        }:
            entries = (
                []
                if endpoint.endswith(f"{BASE}?recursive=1")
                else [
                    {
                        "path": "docs/guide.md",
                        "mode": "100644",
                        "type": "blob",
                        "sha": BLOB,
                    }
                ]
            )
            return _completed(
                call,
                {
                    "sha": "e" * 40,
                    "truncated": self.tree_truncated,
                    "tree": entries,
                },
            )
        if endpoint == f"repos/owner/repo/git/blobs/{BLOB}":
            raw = b"# Guide\n"
            return _completed(
                call,
                {
                    "sha": BLOB,
                    "encoding": "base64",
                    "size": len(raw),
                    "content": base64.b64encode(raw).decode("ascii"),
                },
            )
        if endpoint == f"repos/owner/repo/commits/{HEAD}/check-runs?per_page=100":
            return _completed(
                call,
                [
                    {
                        "total_count": self.check_total,
                        "check_runs": [
                            {
                                "name": "eval",
                                "status": "completed",
                                "conclusion": "success",
                                "head_sha": HEAD,
                            }
                        ],
                    }
                ],
            )
        if endpoint == f"repos/owner/repo/compare/{BASE}...{HEAD}":
            return _completed(call, {"status": "ahead"})
        raise AssertionError(f"unexpected GitHub command: {call}")


def test_complete_merge_evidence_reads_every_required_source() -> None:
    runner = EvidenceRunner()

    evidence = GitHubClient("gh", runner=runner).get_merge_evidence(
        PR_URL,
        ("eval",),
    )

    assert evidence.head_commit == HEAD
    assert evidence.base_commit == BASE
    assert evidence.unresolved_review_threads == 0
    assert evidence.base_is_current is True
    assert evidence.rules_allow_merge is True
    assert evidence.server_requires_current_base is True
    assert evidence.files_pagination_complete is True
    assert evidence.safe_files is not None
    assert evidence.safe_files.result.allowed is True
    assert len(evidence.changed_files) == 1
    changed = evidence.changed_files[0]
    assert changed.path == "docs/guide.md"
    assert changed.is_text is True
    assert changed.file_type == "file"
    assert changed.data_complete is True
    assert changed.patch_complete is True
    assert changed.tree_entry_complete is True

    paginated_calls = [
        call for call in runner.calls if "--paginate" in call or "--slurp" in call
    ]
    assert any("pulls/17/files?per_page=100" in call[-1] for call in paginated_calls)
    assert any("check-runs?per_page=100" in call[-1] for call in paginated_calls)
    assert any(call[1:3] == ["api", "graphql"] for call in paginated_calls)
    assert any(f"git/trees/{BASE}?recursive=1" in call[-1] for call in runner.calls)
    assert any(f"git/trees/{HEAD}?recursive=1" in call[-1] for call in runner.calls)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (("changed_file_count", 2), "changed file count"),
        (("tree_truncated", True), "tree.*truncated"),
        (("check_total", 2), "check.*total"),
        (("review_has_next_page", True), "review thread pagination"),
        (("mergeable_state", "mystery"), "mergeable state"),
    ],
)
def test_incomplete_or_unknown_github_evidence_fails_closed(
    change: tuple[str, object],
    message: str,
) -> None:
    runner = EvidenceRunner()
    setattr(runner, *change)

    with pytest.raises(GateError, match=message):
        GitHubClient("gh", runner=runner).get_merge_evidence(PR_URL, ("eval",))


def test_missing_patch_is_supplied_to_safe_files_as_incomplete() -> None:
    runner = EvidenceRunner()
    runner.file_has_patch = False

    evidence = GitHubClient("gh", runner=runner).get_merge_evidence(
        PR_URL,
        ("eval",),
    )

    assert evidence.changed_files[0].patch_complete is False
    assert evidence.safe_files is not None
    assert evidence.safe_files.result.code == SAFE_FILES_ERROR


def test_public_complete_file_and_review_reads_keep_pagination_proof() -> None:
    files = GitHubClient("gh", runner=EvidenceRunner()).get_all_changed_files(PR_URL)
    review = GitHubClient("gh", runner=EvidenceRunner()).get_review_state(PR_URL)

    assert files.pagination_complete is True
    assert files.head_commit == HEAD
    assert files.base_commit == BASE
    assert files.files[0].tree_entry_complete is True
    assert review.pagination_complete is True
    assert review.unresolved_threads == 0


def test_partial_graphql_errors_never_become_complete_review_state() -> None:
    runner = EvidenceRunner()
    runner.review_errors = True

    with pytest.raises(GateError, match="review.*error"):
        GitHubClient("gh", runner=runner).get_review_state(PR_URL)


def test_unstable_github_state_never_allows_an_automatic_merge() -> None:
    runner = EvidenceRunner()
    runner.mergeable_state = "unstable"

    evidence = GitHubClient("gh", runner=runner).get_merge_evidence(
        PR_URL,
        ("eval",),
    )

    assert evidence.rules_allow_merge is False


@pytest.mark.parametrize(
    "change",
    (
        ("strict_base", False),
        ("conversations_must_be_resolved", False),
        ("required_checks", ("other-check",)),
    ),
)
def test_server_rules_must_atomically_protect_base_checks_and_conversations(
    change: tuple[str, object],
) -> None:
    runner = EvidenceRunner()
    setattr(runner, *change)

    evidence = GitHubClient("gh", runner=runner).get_merge_evidence(
        PR_URL,
        ("eval",),
    )

    assert evidence.server_requires_current_base is False
    assert evidence.rules_allow_merge is False


def test_full_auto_common_evidence_skips_safe_only_tree_and_blob_reads() -> None:
    runner = EvidenceRunner()
    runner.tree_truncated = True

    evidence = GitHubClient("gh", runner=runner).get_merge_evidence(
        PR_URL,
        ("eval",),
        include_safe_files=False,
    )

    assert evidence.safe_files is None
    assert evidence.changed_files == ()
    assert evidence.files_pagination_complete is None
    assert all("/files?" not in " ".join(call) for call in runner.calls)
    assert all("/git/trees/" not in " ".join(call) for call in runner.calls)


def test_already_merged_read_keeps_result_commit_and_merged_head_separate() -> None:
    runner = EvidenceRunner()
    runner.merged = True
    runner.mergeable = None
    runner.mergeable_state = "unknown"
    runner.head_commit = NEW_HEAD

    evidence = GitHubClient("gh", runner=runner).get_merge_evidence(
        PR_URL,
        ("eval",),
    )

    assert evidence.merged_commit == MERGE_COMMIT
    assert evidence.merged_head_commit == HEAD
    assert evidence.merged_base_commit == BASE
    assert evidence.head_commit == HEAD
    assert evidence.base_commit == BASE


def test_invalid_json_never_becomes_empty_or_successful_evidence() -> None:
    runner = EvidenceRunner()
    runner.invalid_json_endpoint = f"repos/owner/repo/compare/{BASE}...{HEAD}"

    with pytest.raises(GateError, match="JSON"):
        GitHubClient("gh", runner=runner).get_merge_evidence(PR_URL, ("eval",))


def _write_state(
    *,
    head: str = HEAD,
    base: str = BASE,
    merged: bool = False,
) -> dict[str, object]:
    return {
        "number": 17,
        "html_url": PR_URL,
        "state": "closed" if merged else "open",
        "merged": merged,
        "merge_commit_sha": MERGE_COMMIT if merged else None,
        "head": {"sha": head},
        "base": {"sha": base, "ref": "main"},
    }


class MergeRunner:
    def __init__(
        self,
        states: list[dict[str, object]],
        *,
        merge_returncode: int = 0,
    ) -> None:
        self.states = list(states)
        self.merge_returncode = merge_returncode
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        call = list(argv)
        self.calls.append(call)
        if "repos/owner/repo/pulls/17/merge" in call:
            return _completed(
                call,
                {
                    "sha": MERGE_COMMIT,
                    "merged": self.merge_returncode == 0,
                    "message": "merge result",
                },
                returncode=self.merge_returncode,
            )
        if call[1] == "api" and call[-1] == "repos/owner/repo/pulls/17":
            return _completed(call, self.states.pop(0))
        if call[1] == "api" and call[-1] == f"repos/owner/repo/git/commits/{MERGE_COMMIT}":
            return _completed(
                call,
                {
                    "sha": MERGE_COMMIT,
                    "parents": [{"sha": BASE}, {"sha": HEAD}],
                },
            )
        raise AssertionError(f"unexpected GitHub command: {call}")


def test_merge_uses_only_the_exact_expected_commit_command() -> None:
    runner = MergeRunner([_write_state(), _write_state(merged=True)])

    result = GitHubMergeClient("gh", runner=runner).merge_expected_commit(
        PR_URL,
        HEAD,
        expected_base_commit=BASE,
    )

    merge_call = next(
        call for call in runner.calls if "repos/owner/repo/pulls/17/merge" in call
    )
    assert merge_call == [
        "gh",
        "api",
        "-X",
        "PUT",
        "repos/owner/repo/pulls/17/merge",
        "-f",
        f"sha={HEAD}",
        "-f",
        "merge_method=merge",
    ]
    assert "--admin" not in merge_call
    assert "--auto" not in merge_call
    assert result.expected_commit == HEAD
    assert result.merged_commit == MERGE_COMMIT
    assert result.merged_head_commit == HEAD
    assert result.merged_base_commit == BASE
    assert result.recovered_by_readback is False


def test_ambiguous_merge_failure_reads_back_before_deciding() -> None:
    runner = MergeRunner(
        [_write_state(), _write_state(merged=True)],
        merge_returncode=1,
    )

    result = GitHubMergeClient("gh", runner=runner).merge_expected_commit(
        PR_URL,
        HEAD,
        expected_base_commit=BASE,
    )

    assert result.recovered_by_readback is True
    assert len(
        [call for call in runner.calls if "repos/owner/repo/pulls/17/merge" in call]
    ) == 1


def test_failed_merge_still_open_is_not_retried() -> None:
    runner = MergeRunner(
        [_write_state(), _write_state()],
        merge_returncode=1,
    )

    with pytest.raises(GateError, match="remains open"):
        GitHubMergeClient("gh", runner=runner).merge_expected_commit(
            PR_URL,
            HEAD,
            expected_base_commit=BASE,
        )

    assert len(
        [call for call in runner.calls if "repos/owner/repo/pulls/17/merge" in call]
    ) == 1


def test_changed_head_stops_before_merge_write() -> None:
    runner = MergeRunner([_write_state(head="f" * 40)])

    with pytest.raises(GateError, match="expected commit"):
        GitHubMergeClient("gh", runner=runner).merge_expected_commit(
            PR_URL,
            HEAD,
            expected_base_commit=BASE,
        )

    assert all("repos/owner/repo/pulls/17/merge" not in call for call in runner.calls)


def test_changed_base_stops_before_merge_write() -> None:
    runner = MergeRunner([_write_state(base="f" * 40)])

    with pytest.raises(GateError, match="expected base"):
        GitHubMergeClient("gh", runner=runner).merge_expected_commit(
            PR_URL,
            HEAD,
            expected_base_commit=BASE,
        )

    assert all("repos/owner/repo/pulls/17/merge" not in call for call in runner.calls)


def test_merged_read_uses_merge_parents_not_a_moved_source_branch() -> None:
    runner = MergeRunner([_write_state(head=NEW_HEAD, merged=True)])

    result = GitHubMergeClient("gh", runner=runner).merge_expected_commit(
        PR_URL,
        HEAD,
        expected_base_commit=BASE,
    )

    assert result.already_merged is True
    assert result.merged_head_commit == HEAD
    assert result.merged_base_commit == BASE
