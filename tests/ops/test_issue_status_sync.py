from __future__ import annotations

import json
import subprocess

import pytest

from forge.ops.issue_status import GitHubIssueStatusClient


class Runner:
    def __init__(self, outputs: list[object]) -> None:
        self.outputs = outputs
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        value = self.outputs.pop(0)
        return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")


def _issue(labels: list[str]) -> dict[str, object]:
    return {
        "number": 7,
        "title": "Task",
        "body": "Task body",
        "labels": [{"name": label} for label in labels],
    }


def test_status_writer_preserves_other_labels_and_keeps_exactly_one_forge_status() -> None:
    runner = Runner(
        [
            _issue(["bug", "forge:building", "forge:failed"]),
            [],
            [],
            [],
            _issue(["bug", "urgent", "forge:reviewing"]),
        ]
    )
    client = GitHubIssueStatusClient("gh", runner=runner)

    labels = client.replace_status("owner/repo", 7, "forge:reviewing")

    assert labels == ("bug", "forge:reviewing", "urgent")
    assert runner.calls[1][1:4] == ["api", "-X", "DELETE"]
    assert runner.calls[1][4].endswith("/labels/forge%3Abuilding")
    assert runner.calls[2][4].endswith("/labels/forge%3Afailed")
    assert runner.calls[3][1:5] == [
        "api",
        "-X",
        "POST",
        "repos/owner/repo/issues/7/labels",
    ]
    assert "labels[]=forge:reviewing" in runner.calls[3]
    assert all("labels[]=bug" not in call for call in runner.calls)
    assert all("PATCH" not in call for call in runner.calls)


def test_status_writer_rejects_readback_without_exact_target() -> None:
    runner = Runner(
        [
            _issue(["forge:building"]),
            [],
            [],
            _issue(["forge:reviewing", "forge:failed"]),
        ]
    )
    client = GitHubIssueStatusClient("gh", runner=runner)

    with pytest.raises(RuntimeError, match="exactly one"):
        client.replace_status("owner/repo", 7, "forge:reviewing")


def test_status_writer_skips_remote_write_when_label_is_already_exact() -> None:
    runner = Runner([_issue(["bug", "forge:reviewing"])])
    client = GitHubIssueStatusClient("gh", runner=runner)

    labels = client.replace_status("owner/repo", 7, "forge:reviewing")

    assert labels == ("bug", "forge:reviewing")
    assert len(runner.calls) == 1
