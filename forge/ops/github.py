"""GitHub CLI adapter for exact-HEAD pull-request evidence."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from .contracts import CheckRun, PullRequestSnapshot
from .hermes import GateError


_PR_URL_RE = re.compile(
    r"^https://github\.com/(?P<repository>[^/\s]+/[^/\s]+)/pull/"
    r"(?P<number>[1-9][0-9]*)$"
)
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_CHECK_STATUSES = frozenset({"queued", "in_progress", "completed"})
_CHECK_CONCLUSIONS = frozenset(
    {
        "action_required",
        "cancelled",
        "failure",
        "neutral",
        "pending",
        "skipped",
        "stale",
        "startup_failure",
        "success",
        "timed_out",
    }
)


def _require_object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise GateError(f"GitHub {label} must be an object")
    return value


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GateError(f"GitHub {label} must be a non-empty string")
    return value


class GitHubClient:
    """Read a PR and required checks through authenticated ``gh api`` calls."""

    def __init__(
        self,
        gh_path: str | Path,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._gh_path = str(Path(gh_path).expanduser())
        self._runner = runner

    def _get_json(self, endpoint: str, label: str) -> Mapping[str, object]:
        result = self._runner(
            [self._gh_path, "api", endpoint],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise GateError(
                f"GitHub {label} request failed with exit code {result.returncode}"
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise GateError(f"GitHub {label} response is not valid JSON") from error
        return _require_object(payload, f"{label} response")

    def get_pr_snapshot(
        self,
        pr_url: str,
        required_check_names: Sequence[str],
    ) -> PullRequestSnapshot:
        match = _PR_URL_RE.fullmatch(pr_url)
        if match is None:
            raise GateError("GitHub PR URL has an invalid format")
        repository = match.group("repository")
        pr_number = int(match.group("number"))
        required_names = tuple(required_check_names)
        if (
            not required_names
            or any(not isinstance(name, str) or not name.strip() for name in required_names)
            or len(set(required_names)) != len(required_names)
        ):
            raise GateError("required check names must be unique non-empty strings")

        pr_payload = self._get_json(
            f"repos/{repository}/pulls/{pr_number}",
            "pull request",
        )
        api_pr_number = pr_payload.get("number")
        if (
            not isinstance(api_pr_number, int)
            or isinstance(api_pr_number, bool)
            or api_pr_number != pr_number
        ):
            raise GateError("GitHub PR number does not match requested URL")
        if pr_payload.get("html_url") != pr_url:
            raise GateError("GitHub PR URL does not match requested URL")
        state = pr_payload.get("state")
        if state not in {"open", "closed"}:
            raise GateError("GitHub PR state is invalid")
        draft = pr_payload.get("draft")
        if not isinstance(draft, bool):
            raise GateError("GitHub PR draft flag is invalid")
        head = _require_object(pr_payload.get("head"), "PR head")
        head_sha = _require_text(head.get("sha"), "PR head SHA")
        if _GIT_SHA_RE.fullmatch(head_sha) is None:
            raise GateError("GitHub PR head SHA has an invalid format")

        check_payload = self._get_json(
            f"repos/{repository}/commits/{head_sha}/check-runs?per_page=100",
            "check-runs",
        )
        raw_checks = check_payload.get("check_runs")
        if not isinstance(raw_checks, list):
            raise GateError("GitHub check-runs must be an array")
        total_count = check_payload.get("total_count")
        if (
            not isinstance(total_count, int)
            or isinstance(total_count, bool)
            or total_count != len(raw_checks)
        ):
            raise GateError(
                "GitHub check-runs total_count does not match the complete payload"
            )

        checks: list[CheckRun] = []
        for required_name in required_names:
            matches = [
                raw
                for raw in raw_checks
                if isinstance(raw, dict) and raw.get("name") == required_name
            ]
            if len(matches) != 1:
                raise GateError(
                    f"required check {required_name} must appear exactly one time"
                )
            raw = matches[0]
            status = raw.get("status")
            if status not in _CHECK_STATUSES:
                raise GateError(f"required check {required_name} has invalid status")
            conclusion = raw.get("conclusion")
            if status == "completed":
                if conclusion not in _CHECK_CONCLUSIONS:
                    raise GateError(
                        f"required check {required_name} has invalid conclusion"
                    )
            elif conclusion is not None:
                raise GateError(
                    f"pending required check {required_name} has a conclusion"
                )
            check_head_sha = raw.get("head_sha")
            if check_head_sha != head_sha:
                raise GateError(
                    f"required check {required_name} is not bound to current HEAD"
                )
            checks.append(
                CheckRun(
                    name=required_name,
                    status=status,
                    conclusion=conclusion,
                    head_sha=head_sha,
                )
            )

        return PullRequestSnapshot(
            pr_url=pr_url,
            repository=repository,
            pr_number=pr_number,
            head_sha=head_sha,
            is_open=state == "open",
            is_draft=draft,
            checks=tuple(checks),
        )
