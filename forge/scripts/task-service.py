#!/usr/bin/env python3
"""Create one confirmed Task through the strict Task service API."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from forge.ops.github import GitHubTaskIssueClient  # noqa: E402
from forge.ops.task_options import MergeMode, TaskFlow  # noqa: E402
from forge.ops.task_service import (  # noqa: E402
    TaskCreationRequest,
    TaskService,
)
from forge.ops.task_settings import TaskContent, TaskSettingsStore  # noqa: E402


REQUEST_FIELDS = frozenset(
    {
        "request_id",
        "repository",
        "title",
        "description",
        "acceptance_criteria",
        "task_flow",
        "merge_mode",
        "confirmed_by",
        "confirmed_at",
    }
)


class TaskServiceInputError(ValueError):
    """Raised when the CLI request does not match the public Task format."""


def _text(value: object, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        suffix = "text" if allow_empty else "non-empty text"
        raise TaskServiceInputError(f"{field} must be {suffix}")
    return value


def parse_creation_request(value: Mapping[str, object]) -> TaskCreationRequest:
    """Parse one exact Task request without defaults or old field names."""

    if not isinstance(value, Mapping):
        raise TaskServiceInputError("Task request must be a JSON object")
    fields = set(value)
    if fields != REQUEST_FIELDS:
        missing = sorted(REQUEST_FIELDS - fields)
        extra = sorted(fields - REQUEST_FIELDS)
        raise TaskServiceInputError(
            f"Task request fields do not match; missing={missing}, extra={extra}"
        )
    raw_criteria = value["acceptance_criteria"]
    if not isinstance(raw_criteria, list):
        raise TaskServiceInputError("acceptance_criteria must be a JSON list")
    criteria = tuple(_text(item, "acceptance_criteria item") for item in raw_criteria)
    raw_confirmed_at = _text(value["confirmed_at"], "confirmed_at")
    try:
        confirmed_at = datetime.fromisoformat(raw_confirmed_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise TaskServiceInputError("confirmed_at must be RFC 3339") from error
    try:
        task_flow = TaskFlow(_text(value["task_flow"], "task_flow"))
        merge_mode = MergeMode(_text(value["merge_mode"], "merge_mode"))
    except ValueError as error:
        raise TaskServiceInputError("Task flow or merge mode is unknown") from error
    return TaskCreationRequest(
        request_id=_text(value["request_id"], "request_id"),
        repository=_text(value["repository"], "repository"),
        content=TaskContent(
            title=_text(value["title"], "title"),
            description=_text(value["description"], "description", allow_empty=True),
            acceptance_criteria=criteria,
        ),
        task_flow=task_flow,
        merge_mode=merge_mode,
        confirmed_by=_text(value["confirmed_by"], "confirmed_by"),
        confirmed_at=confirmed_at,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-json", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--gh", default="/usr/bin/gh")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        raw = json.loads(args.request_json.read_text(encoding="utf-8"))
        request = parse_creation_request(raw)
        service = TaskService(
            TaskSettingsStore(args.database),
            GitHubTaskIssueClient(args.gh),
        )
        # RISK(side-effect): confirmation is the only path that may create or
        # update a GitHub issue; TaskService performs exact readback checks.
        created = service.create_task(request)
    except Exception as error:
        print(f"CHECK_ERROR: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "issue_number": created.issue.number,
                "request_id": created.settings.request_id,
                "task_settings_hash": created.settings.task_settings_hash,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
