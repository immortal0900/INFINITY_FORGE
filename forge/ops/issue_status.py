"""Replace only Forge's displayed GitHub issue status and verify readback."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from urllib.parse import quote

from .displayed_status import FORGE_STATUS_LABELS
from .hermes import GateError


_REPOSITORY_RE = re.compile(r"^[^/#:\s]+/[^/#:\s]+$")


class GitHubIssueStatusClient:
    """Own the single-writer status-label operation through ``gh api``."""

    def __init__(
        self,
        gh_path: str | Path,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._gh_path = str(Path(gh_path).expanduser())
        self._runner = runner

    def _json(self, arguments: Sequence[str], label: str) -> object:
        result = self._runner(
            [self._gh_path, "api", *arguments],
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
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise GateError(f"GitHub {label} response is not valid JSON") from error

    @staticmethod
    def _labels(value: object, issue_number: int) -> tuple[str, ...]:
        if not isinstance(value, dict) or value.get("number") != issue_number:
            raise GateError("GitHub issue status response is invalid")
        raw = value.get("labels")
        if not isinstance(raw, list):
            raise GateError("GitHub issue status labels are invalid")
        labels: list[str] = []
        for item in raw:
            if not isinstance(item, dict):
                raise GateError("GitHub issue status label is invalid")
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                raise GateError("GitHub issue status label is invalid")
            labels.append(name)
        if len(labels) != len(set(labels)):
            raise GateError("GitHub issue status labels contain duplicates")
        return tuple(sorted(labels))

    def replace_status(
        self,
        repository: str,
        issue_number: int,
        target: str,
    ) -> tuple[str, ...]:
        if not isinstance(repository, str) or _REPOSITORY_RE.fullmatch(repository) is None:
            raise GateError("GitHub repository must use OWNER/REPO")
        if type(issue_number) is not int or issue_number <= 0:
            raise GateError("GitHub issue number must be positive")
        if target not in FORGE_STATUS_LABELS:
            raise GateError("target must be an official Forge status label")
        endpoint = f"repos/{repository}/issues/{issue_number}"
        current = self._labels(self._json((endpoint,), "issue status read"), issue_number)
        current_official = tuple(
            label for label in current if label in FORGE_STATUS_LABELS
        )
        if current_official == (target,):
            return current
        # GitHub's whole-issue PATCH replaces the complete label set and can
        # erase an unrelated label added after our read. Change only labels
        # owned by Forge so concurrent human or app labels remain untouched.
        for label in current_official:
            if label == target:
                continue
            label_endpoint = f"{endpoint}/labels/{quote(label, safe='')}"
            self._json(
                ("-X", "DELETE", label_endpoint),
                "issue status remove",
            )
        if target not in current_official:
            self._json(
                (
                    "-X",
                    "POST",
                    f"{endpoint}/labels",
                    "-f",
                    f"labels[]={target}",
                ),
                "issue status add",
            )
        readback = self._labels(
            self._json((endpoint,), "issue status readback"),
            issue_number,
        )
        official = tuple(label for label in readback if label in FORGE_STATUS_LABELS)
        if official != (target,):
            raise RuntimeError(
                "GitHub issue must contain exactly one requested Forge status"
            )
        original_unrelated = {
            label for label in current if label not in FORGE_STATUS_LABELS
        }
        if not original_unrelated.issubset(readback):
            raise RuntimeError("GitHub issue status update lost an unrelated label")
        return readback
