from __future__ import annotations

import json
import subprocess

import pytest

from forge.ops.github import GitHubTaskIssueClient
from forge.ops.hermes import GateError
from forge.ops.task_service import READY_TO_BUILD_LABEL


REQUEST_ID = "9f7453ce-36ec-4e8e-9dfa-bb159b58c19b"
CONTENT_HASH = "a" * 64
SETTINGS_HASH = "b" * 64
BODY = (
    "Task body\n\n"
    "<!-- forge-task-request\n"
    + json.dumps(
        {
            "format_version": "forge-task-request/v1",
            "request_id": REQUEST_ID,
            "task_content_hash": CONTENT_HASH,
            "task_settings_hash": SETTINGS_HASH,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    + "\n-->"
)


def _issue(number: int, *, body: str = BODY, labels: tuple[str, ...] = ()) -> dict:
    return {
        "number": number,
        "title": "Task title",
        "body": body,
        "labels": [{"name": label} for label in labels],
    }


class Runner:
    def __init__(self, outputs: list[object]) -> None:
        self.outputs = outputs
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        output = self.outputs.pop(0)
        if isinstance(output, tuple):
            returncode, text = output
        else:
            returncode, text = 0, json.dumps(output)
        return subprocess.CompletedProcess(argv, returncode, text, "failure")


def test_find_issue_reads_every_page_and_matches_exact_request_marker() -> None:
    runner = Runner(
        [
            [
                [_issue(1, body="ordinary issue")],
                [_issue(2)],
            ]
        ]
    )
    client = GitHubTaskIssueClient("gh", runner=runner)

    issue = client.find_issue("openai/infinity-forge", REQUEST_ID)

    assert issue is not None and issue.number == 2
    assert "--paginate" in runner.calls[0]
    assert "--slurp" in runner.calls[0]
    assert "state=all" in runner.calls[0][-1]


def test_find_issue_rejects_duplicate_or_malformed_forge_markers() -> None:
    duplicate = Runner([[[ _issue(1), _issue(2) ]]])
    client = GitHubTaskIssueClient("gh", runner=duplicate)
    with pytest.raises(GateError, match="more than one"):
        client.find_issue("openai/infinity-forge", REQUEST_ID)

    malformed = Runner([[[ _issue(1, body="<!-- forge-task-request\nbad\n-->") ]]])
    client = GitHubTaskIssueClient("gh", runner=malformed)
    with pytest.raises(GateError, match="Task marker"):
        client.find_issue("openai/infinity-forge", REQUEST_ID)


def test_issue_write_and_read_commands_return_strict_issue_snapshots() -> None:
    runner = Runner(
        [
            _issue(3),
            _issue(3, body=BODY + "\nupdated"),
            _issue(3, body=BODY + "\nupdated"),
            [{"name": READY_TO_BUILD_LABEL}],
            _issue(
                3,
                body=BODY + "\nupdated",
                labels=(READY_TO_BUILD_LABEL,),
            ),
        ]
    )
    client = GitHubTaskIssueClient("gh", runner=runner)

    created = client.create_issue("openai/infinity-forge", "Task title", BODY)
    updated = client.update_issue(
        "openai/infinity-forge",
        3,
        title="Task title",
        body=BODY + "\nupdated",
    )
    fetched = client.get_issue("openai/infinity-forge", 3)
    labeled = client.add_label(
        "openai/infinity-forge",
        3,
        READY_TO_BUILD_LABEL,
    )

    assert created.number == updated.number == fetched.number == labeled.number == 3
    assert labeled.labels == (READY_TO_BUILD_LABEL,)
    assert runner.calls[0][1:5] == ["api", "-X", "POST", "repos/openai/infinity-forge/issues"]
    assert runner.calls[1][2:5] == ["-X", "PATCH", "repos/openai/infinity-forge/issues/3"]
    assert runner.calls[2][1:] == ["api", "repos/openai/infinity-forge/issues/3"]
    assert runner.calls[3][2:5] == [
        "-X",
        "POST",
        "repos/openai/infinity-forge/issues/3/labels",
    ]
    assert f"labels[]={READY_TO_BUILD_LABEL}" in runner.calls[3]
    assert runner.calls[4][1:] == ["api", "repos/openai/infinity-forge/issues/3"]


@pytest.mark.parametrize(
    "payload",
    [
        {"number": True, "title": "Task", "body": BODY, "labels": []},
        {"number": 1, "title": "", "body": BODY, "labels": []},
        {"number": 1, "title": "Task", "body": None, "labels": []},
        {"number": 1, "title": "Task", "body": BODY, "labels": [{}]},
        {
            "number": 1,
            "title": "Task",
            "body": BODY,
            "labels": [],
            "pull_request": {},
        },
    ],
)
def test_malformed_issue_response_is_never_treated_as_success(payload: object) -> None:
    client = GitHubTaskIssueClient("gh", runner=Runner([payload]))

    with pytest.raises(GateError, match="GitHub issue"):
        client.get_issue("openai/infinity-forge", 1)


def test_github_api_failure_does_not_return_an_empty_issue() -> None:
    client = GitHubTaskIssueClient("gh", runner=Runner([(1, "denied")]))

    with pytest.raises(GateError, match="exit code 1"):
        client.find_issue("openai/infinity-forge", REQUEST_ID)
