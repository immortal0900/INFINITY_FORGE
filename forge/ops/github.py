"""GitHub CLI adapter for exact-HEAD pull-request evidence."""

from __future__ import annotations

import base64
import binascii
import json
import re
import subprocess
from urllib.parse import quote
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID

from .contracts import CheckRun
from .displayed_status import FORGE_STATUS_LABELS
from .hermes import GateError
from .safe_files import (
    ChangedFile,
    SafeFilesEvidence,
    check_safe_files,
)
from .task_service import (
    TaskIssue,
    TaskParentIssue,
    TaskServiceError,
    read_task_marker,
    read_task_marker_v2,
)


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
_REPOSITORY_RE = re.compile(r"^[^/\s]+/[^/\s]+$")
_REQUEST_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _is_canonical_v2_request_id(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = UUID(value)
    except ValueError:
        return False
    return str(parsed) == value


_FILE_STATUSES = frozenset(
    {"added", "modified", "removed", "renamed", "copied", "changed", "unchanged"}
)
_MERGEABLE_STATES = frozenset(
    {"clean", "dirty", "unstable", "blocked", "behind", "draft", "has_hooks"}
)
_MERGEABLE_ALLOWED_STATES = frozenset(
    {"clean"}
)
_COMPARE_STATUSES = frozenset({"ahead", "behind", "diverged", "identical"})

_REVIEW_THREADS_QUERY = """
query(
  $owner: String!,
  $name: String!,
  $number: Int!,
  $endCursor: String
) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $endCursor) {
        nodes { id isResolved }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()


@dataclass(frozen=True, slots=True)
class PullRequestWriteState:
    pr_url: str
    repository: str
    pr_number: int
    base_commit: str
    base_ref: str
    head_commit: str
    is_open: bool
    is_merged: bool
    merged_commit: str | None
    merged_base_commit: str | None
    merged_head_commit: str | None


@dataclass(frozen=True, slots=True)
class TaskStopIssueState:
    """Minimal authoritative parent Issue state used by Stop cleanup."""

    number: int
    title: str
    body: str
    state: str
    state_reason: str | None
    labels: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CompleteChangedFiles:
    files: tuple[ChangedFile, ...]
    pagination_complete: bool
    base_commit: str
    head_commit: str


@dataclass(frozen=True, slots=True)
class ReviewState:
    unresolved_threads: int
    pagination_complete: bool


@dataclass(frozen=True, slots=True)
class GitHubMergeEvidence:
    pr_url: str
    repository: str
    pr_number: int
    head_commit: str
    base_commit: str
    is_open: bool
    is_draft: bool
    is_merged: bool
    merged_commit: str | None
    merged_base_commit: str | None
    merged_head_commit: str | None
    has_conflict: bool
    base_is_current: bool
    rules_allow_merge: bool
    server_requires_current_base: bool
    unresolved_review_threads: int
    checks: tuple[CheckRun, ...]
    changed_files: tuple[ChangedFile, ...]
    files_pagination_complete: bool | None
    safe_files: SafeFilesEvidence | None


def _require_object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise GateError(f"GitHub {label} must be an object")
    return value


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GateError(f"GitHub {label} must be a non-empty string")
    return value


def parse_pull_request_url(pr_url: str) -> tuple[str, int]:
    """Return the exact repository and number from a canonical GitHub PR URL."""

    match = _PR_URL_RE.fullmatch(pr_url) if isinstance(pr_url, str) else None
    if match is None:
        raise GateError("GitHub PR URL has an invalid format")
    return match.group("repository"), int(match.group("number"))


def validate_commit_sha(value: object, label: str = "commit") -> str:
    """Return one full lowercase Git commit SHA or fail closed."""

    if not isinstance(value, str) or _GIT_SHA_RE.fullmatch(value) is None:
        raise GateError(f"GitHub {label} has an invalid format")
    return value


def _require_sha(value: object, label: str) -> str:
    return validate_commit_sha(_require_text(value, label), label)


def _require_integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise GateError(f"GitHub {label} must be an integer of at least {minimum}")
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

    def _run_json(self, arguments: Sequence[str], label: str) -> object:
        argv = [self._gh_path, "api", *arguments]
        try:
            result = self._runner(
                argv,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise GateError(f"GitHub {label} request could not be completed") from error
        if result.returncode != 0:
            raise GateError(
                f"GitHub {label} request failed with exit code {result.returncode}"
            )
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise GateError(f"GitHub {label} response is not valid JSON") from error

    def _get_json(self, endpoint: str, label: str) -> Mapping[str, object]:
        payload = self._run_json((endpoint,), label)
        return _require_object(payload, f"{label} response")

    def _get_pages(self, arguments: Sequence[str], label: str) -> list[object]:
        if not arguments:
            raise GateError(f"GitHub {label} endpoint is missing")
        if arguments[0] == "graphql":
            paginated_arguments = (
                "graphql",
                "--paginate",
                "--slurp",
                *arguments[1:],
            )
        else:
            paginated_arguments = ("--paginate", "--slurp", *arguments)
        payload = self._run_json(paginated_arguments, label)
        if not isinstance(payload, list) or not payload:
            raise GateError(f"GitHub {label} pagination response is invalid")
        return payload

    def _read_pull_request(
        self,
        pr_url: str,
    ) -> tuple[str, int, Mapping[str, object]]:
        repository, pr_number = parse_pull_request_url(pr_url)
        payload = self._get_json(
            f"repos/{repository}/pulls/{pr_number}",
            "pull request",
        )
        api_number = payload.get("number")
        if type(api_number) is not int or api_number != pr_number:
            raise GateError("GitHub PR number does not match requested URL")
        if payload.get("html_url") != pr_url:
            raise GateError("GitHub PR URL does not match requested URL")
        return repository, pr_number, payload

    @staticmethod
    def _pull_request_state(
        pr_url: str,
        repository: str,
        pr_number: int,
        payload: Mapping[str, object],
    ) -> PullRequestWriteState:
        state = payload.get("state")
        if state not in {"open", "closed"}:
            raise GateError("GitHub PR state is invalid")
        merged = payload.get("merged")
        if not isinstance(merged, bool):
            raise GateError("GitHub PR merged flag is invalid")
        if merged and state != "closed":
            raise GateError("GitHub merged PR must be closed")
        head = _require_object(payload.get("head"), "PR head")
        head_commit = _require_sha(head.get("sha"), "PR head SHA")
        base = _require_object(payload.get("base"), "PR base")
        base_commit = _require_sha(base.get("sha"), "PR base SHA")
        base_ref = _require_text(base.get("ref"), "PR base ref")
        merged_commit = None
        if merged:
            merged_commit = _require_sha(
                payload.get("merge_commit_sha"),
                "PR merged commit",
            )
        return PullRequestWriteState(
            pr_url=pr_url,
            repository=repository,
            pr_number=pr_number,
            base_commit=base_commit,
            base_ref=base_ref,
            head_commit=head_commit,
            is_open=state == "open",
            is_merged=merged,
            merged_commit=merged_commit,
            merged_base_commit=None,
            merged_head_commit=None,
        )

    def get_pr_write_state(self, pr_url: str) -> PullRequestWriteState:
        """Read the minimal authoritative PR state used after a write."""

        repository, pr_number, payload = self._read_pull_request(pr_url)
        state = self._pull_request_state(
            pr_url,
            repository,
            pr_number,
            payload,
        )
        if not state.is_merged:
            return state
        assert state.merged_commit is not None
        merged_base, merged_head = self._read_merge_parents(
            repository,
            state.merged_commit,
        )
        return PullRequestWriteState(
            pr_url=state.pr_url,
            repository=state.repository,
            pr_number=state.pr_number,
            base_commit=state.base_commit,
            base_ref=state.base_ref,
            head_commit=state.head_commit,
            is_open=state.is_open,
            is_merged=state.is_merged,
            merged_commit=state.merged_commit,
            merged_base_commit=merged_base,
            merged_head_commit=merged_head,
        )

    def find_pr_write_state(
        self,
        repository: str,
        branch_name: str,
    ) -> PullRequestWriteState | None:
        """Find an exact same-repository PR after an ambiguous create response."""

        if (
            not isinstance(repository, str)
            or _REPOSITORY_RE.fullmatch(repository) is None
        ):
            raise GateError("GitHub repository must use OWNER/REPO format")
        if (
            not isinstance(branch_name, str)
            or not branch_name
            or branch_name != branch_name.strip()
            or any(
                character.isspace() or ord(character) < 32 for character in branch_name
            )
        ):
            raise GateError("GitHub PR branch name is invalid")
        owner = repository.split("/", 1)[0]
        head_filter = quote(f"{owner}:{branch_name}", safe=":")
        pages = self._get_pages(
            (f"repos/{repository}/pulls?state=all&head={head_filter}&per_page=100",),
            "pull request recovery list",
        )
        matches: list[str] = []
        for page in pages:
            if not isinstance(page, list):
                raise GateError("GitHub PR recovery page is invalid")
            for item in page:
                raw = _require_object(item, "PR recovery item")
                head = _require_object(raw.get("head"), "PR recovery head")
                head_repository = _require_object(
                    head.get("repo"), "PR recovery head repository"
                )
                if (
                    head.get("ref") != branch_name
                    or head_repository.get("full_name") != repository
                ):
                    continue
                number = raw.get("number")
                if type(number) is not int or number <= 0:
                    raise GateError("GitHub PR recovery number is invalid")
                url = f"https://github.com/{repository}/pull/{number}"
                if raw.get("html_url") != url:
                    raise GateError("GitHub PR recovery URL is invalid")
                matches.append(url)
        if len(matches) > 1:
            raise GateError("GitHub branch matched more than one pull request")
        return None if not matches else self.get_pr_write_state(matches[0])

    def _read_merge_parents(
        self,
        repository: str,
        merge_commit: str,
    ) -> tuple[str, str]:
        """Return the historical base and head of one merge commit."""

        payload = self._get_json(
            f"repos/{repository}/git/commits/{merge_commit}",
            "merged commit",
        )
        if payload.get("sha") != merge_commit:
            raise GateError("GitHub merged commit identity does not match")
        raw_parents = payload.get("parents")
        if not isinstance(raw_parents, list) or len(raw_parents) != 2:
            raise GateError(
                "GitHub merged commit is not a two-parent merge commit"
            )
        parents = tuple(
            _require_sha(
                _require_object(parent, "merged commit parent").get("sha"),
                "merged commit parent SHA",
            )
            for parent in raw_parents
        )
        return parents[0], parents[1]

    def _read_tree(
        self,
        repository: str,
        commit: str,
        label: str,
    ) -> dict[str, Mapping[str, object]]:
        payload = self._get_json(
            f"repos/{repository}/git/trees/{commit}?recursive=1",
            label,
        )
        if payload.get("truncated") is not False:
            raise GateError(f"GitHub {label} tree is truncated")
        raw_entries = payload.get("tree")
        if not isinstance(raw_entries, list):
            raise GateError(f"GitHub {label} tree entries must be an array")
        entries: dict[str, Mapping[str, object]] = {}
        for raw_entry in raw_entries:
            entry = _require_object(raw_entry, f"{label} tree entry")
            path = _require_text(entry.get("path"), f"{label} tree path")
            mode = _require_text(entry.get("mode"), f"{label} tree mode")
            entry_type = entry.get("type")
            if entry_type not in {"blob", "tree", "commit"}:
                raise GateError(f"GitHub {label} tree entry type is unknown")
            _require_sha(entry.get("sha"), f"{label} tree entry SHA")
            if mode not in {"040000", "100644", "100755", "120000", "160000"}:
                raise GateError(f"GitHub {label} tree entry mode is unknown")
            if path in entries:
                raise GateError(f"GitHub {label} tree contains a duplicate path")
            entries[path] = entry
        return entries

    def _read_blob(
        self,
        repository: str,
        blob_sha: str,
    ) -> bytes:
        payload = self._get_json(
            f"repos/{repository}/git/blobs/{blob_sha}",
            "file data",
        )
        if payload.get("sha") != blob_sha or payload.get("encoding") != "base64":
            raise GateError("GitHub file data identity or encoding is invalid")
        size = _require_integer(payload.get("size"), "file data size")
        content = payload.get("content")
        if not isinstance(content, str):
            raise GateError("GitHub file data content must be base64 text")
        encoded = "".join(content.split())
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise GateError("GitHub file data content is invalid base64") from error
        if len(decoded) != size:
            raise GateError("GitHub file data size does not match content")
        return decoded

    @staticmethod
    def _is_text_file(content: bytes) -> bool:
        if b"\x00" in content:
            return False
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return True

    def _changed_file(
        self,
        repository: str,
        raw_file: object,
        *,
        base_tree: Mapping[str, Mapping[str, object]],
        head_tree: Mapping[str, Mapping[str, object]],
    ) -> ChangedFile:
        item = _require_object(raw_file, "changed file")
        path = _require_text(item.get("filename"), "changed file path")
        status = item.get("status")
        if status not in _FILE_STATUSES:
            raise GateError("GitHub changed file status is unknown")
        file_sha = _require_sha(item.get("sha"), "changed file SHA")
        patch_complete = isinstance(item.get("patch"), str)
        source_tree = base_tree if status == "removed" else head_tree
        entry = source_tree.get(path)
        if entry is None:
            return ChangedFile(
                path=path,
                status=status,
                is_text=None,
                file_type=None,
                data_complete=False,
                patch_complete=patch_complete,
                tree_entry_complete=False,
            )

        entry_sha = _require_sha(entry.get("sha"), "changed tree entry SHA")
        if entry_sha != file_sha:
            raise GateError("GitHub changed file SHA does not match its tree entry")
        mode = entry.get("mode")
        entry_type = entry.get("type")
        if entry_type == "commit" and mode == "160000":
            file_type = "submodule"
            is_text = False
        elif entry_type == "blob" and mode == "120000":
            self._read_blob(repository, file_sha)
            file_type = "symlink"
            is_text = False
        elif entry_type == "blob" and mode in {"100644", "100755"}:
            content = self._read_blob(repository, file_sha)
            file_type = "file"
            is_text = self._is_text_file(content)
        else:
            raise GateError("GitHub changed file tree entry is unknown")
        return ChangedFile(
            path=path,
            status=status,
            is_text=is_text,
            file_type=file_type,
            data_complete=True,
            patch_complete=patch_complete,
            tree_entry_complete=True,
        )

    def _read_changed_files(
        self,
        repository: str,
        pr_number: int,
        expected_count: int,
        *,
        base_commit: str,
        head_commit: str,
    ) -> tuple[ChangedFile, ...]:
        pages = self._get_pages(
            (f"repos/{repository}/pulls/{pr_number}/files?per_page=100",),
            "changed files",
        )
        if any(not isinstance(page, list) for page in pages):
            raise GateError("GitHub changed file pagination pages are invalid")
        raw_files = [item for page in pages for item in page]
        if len(raw_files) != expected_count:
            raise GateError("GitHub changed file count does not match complete pagination")
        base_tree = self._read_tree(repository, base_commit, "base")
        head_tree = self._read_tree(repository, head_commit, "head")
        files = tuple(
            self._changed_file(
                repository,
                raw_file,
                base_tree=base_tree,
                head_tree=head_tree,
            )
            for raw_file in raw_files
        )
        paths = tuple(item.path for item in files)
        if len(set(paths)) != len(paths):
            raise GateError("GitHub changed file pagination contains duplicate paths")
        return files

    def get_all_changed_files(self, pr_url: str) -> CompleteChangedFiles:
        """Read every changed file plus complete base/head tree evidence."""

        repository, pr_number, payload = self._read_pull_request(pr_url)
        head = _require_object(payload.get("head"), "PR head")
        base = _require_object(payload.get("base"), "PR base")
        head_commit = _require_sha(head.get("sha"), "PR head SHA")
        base_commit = _require_sha(base.get("sha"), "PR base SHA")
        expected_count = _require_integer(
            payload.get("changed_files"),
            "changed file count",
        )
        files = self._read_changed_files(
            repository,
            pr_number,
            expected_count,
            base_commit=base_commit,
            head_commit=head_commit,
        )
        return CompleteChangedFiles(
            files=files,
            pagination_complete=True,
            base_commit=base_commit,
            head_commit=head_commit,
        )

    def _read_checks(
        self,
        repository: str,
        head_commit: str,
        required_check_names: Sequence[str],
    ) -> tuple[CheckRun, ...]:
        required_names = tuple(required_check_names)
        if (
            not required_names
            or any(
                not isinstance(name, str) or not name.strip()
                for name in required_names
            )
            or len(set(required_names)) != len(required_names)
        ):
            raise GateError("required check names must be unique non-empty strings")
        pages = self._get_pages(
            (
                f"repos/{repository}/commits/{head_commit}/"
                "check-runs?per_page=100",
            ),
            "check-runs",
        )
        page_objects = [
            _require_object(page, "check-runs page")
            for page in pages
        ]
        totals = tuple(page.get("total_count") for page in page_objects)
        if any(type(total) is not int or total < 0 for total in totals):
            raise GateError("GitHub check total is invalid")
        raw_checks: list[Mapping[str, object]] = []
        for page in page_objects:
            raw_page_checks = page.get("check_runs")
            if not isinstance(raw_page_checks, list):
                raise GateError("GitHub check-runs page must contain an array")
            raw_checks.extend(
                _require_object(raw, "check-run") for raw in raw_page_checks
            )
        if len(set(totals)) != 1 or totals[0] != len(raw_checks):
            raise GateError("GitHub check total does not match complete pagination")

        parsed: list[CheckRun] = []
        for raw in raw_checks:
            name = _require_text(raw.get("name"), "check-run name")
            status = raw.get("status")
            if status not in _CHECK_STATUSES:
                raise GateError(f"GitHub check-run {name} has invalid status")
            conclusion = raw.get("conclusion")
            if status == "completed":
                if conclusion not in _CHECK_CONCLUSIONS:
                    raise GateError(
                        f"GitHub check-run {name} has invalid conclusion"
                    )
            elif conclusion is not None:
                raise GateError(f"GitHub pending check-run {name} has a conclusion")
            check_commit = _require_sha(raw.get("head_sha"), "check-run head SHA")
            if check_commit != head_commit:
                raise GateError(f"GitHub check-run {name} is not for current HEAD")
            parsed.append(
                CheckRun(
                    name=name,
                    status=status,
                    conclusion=conclusion,
                    head_sha=check_commit,
                )
            )

        required: list[CheckRun] = []
        for name in required_names:
            matches = [check for check in parsed if check.name == name]
            if len(matches) != 1:
                raise GateError(f"required check {name} must appear exactly one time")
            required.append(matches[0])
        return tuple(required)

    def _read_unresolved_review_threads(
        self,
        repository: str,
        pr_number: int,
    ) -> int:
        owner, name = repository.split("/", 1)
        pages = self._get_pages(
            (
                "graphql",
                "-f",
                f"query={_REVIEW_THREADS_QUERY}",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={name}",
                "-F",
                f"number={pr_number}",
            ),
            "review threads",
        )
        seen_ids: set[str] = set()
        unresolved = 0
        for index, raw_page in enumerate(pages):
            page = _require_object(raw_page, "review thread page")
            if "errors" in page:
                raise GateError("GitHub review thread response contains errors")
            data = _require_object(page.get("data"), "review thread data")
            raw_repository = _require_object(
                data.get("repository"),
                "review thread repository",
            )
            pull_request = _require_object(
                raw_repository.get("pullRequest"),
                "review thread pull request",
            )
            threads = _require_object(
                pull_request.get("reviewThreads"),
                "review threads",
            )
            nodes = threads.get("nodes")
            if not isinstance(nodes, list):
                raise GateError("GitHub review thread nodes must be an array")
            page_info = _require_object(
                threads.get("pageInfo"),
                "review thread page info",
            )
            has_next_page = page_info.get("hasNextPage")
            if not isinstance(has_next_page, bool):
                raise GateError("GitHub review thread pagination flag is invalid")
            expected_next_page = index < len(pages) - 1
            if has_next_page != expected_next_page:
                raise GateError("GitHub review thread pagination is incomplete")
            if has_next_page:
                _require_text(
                    page_info.get("endCursor"),
                    "review thread end cursor",
                )
            for raw_node in nodes:
                node = _require_object(raw_node, "review thread")
                thread_id = _require_text(node.get("id"), "review thread ID")
                is_resolved = node.get("isResolved")
                if not isinstance(is_resolved, bool):
                    raise GateError("GitHub review thread resolved flag is invalid")
                if thread_id in seen_ids:
                    raise GateError("GitHub review thread pagination has duplicates")
                seen_ids.add(thread_id)
                unresolved += int(not is_resolved)
        return unresolved

    def get_review_state(self, pr_url: str) -> ReviewState:
        """Read every review thread and prove pagination reached its last page."""

        repository, pr_number, _payload = self._read_pull_request(pr_url)
        return ReviewState(
            unresolved_threads=self._read_unresolved_review_threads(
                repository,
                pr_number,
            ),
            pagination_complete=True,
        )

    def _base_is_current(
        self,
        repository: str,
        base_commit: str,
        head_commit: str,
    ) -> bool:
        payload = self._get_json(
            f"repos/{repository}/compare/{base_commit}...{head_commit}",
            "base comparison",
        )
        status = payload.get("status")
        if status not in _COMPARE_STATUSES:
            raise GateError("GitHub base comparison status is unknown")
        return status in {"ahead", "identical"}

    def _server_protects_merge_snapshot(
        self,
        repository: str,
        base_ref: str,
        required_check_names: Sequence[str],
    ) -> bool:
        """Require GitHub to close the base/review TOCTOU at merge time."""

        encoded_ref = quote(base_ref, safe="")
        payload = self._get_json(
            f"repos/{repository}/branches/{encoded_ref}/protection",
            "base branch protection",
        )
        raw_checks = payload.get("required_status_checks")
        raw_conversations = payload.get("required_conversation_resolution")
        if not isinstance(raw_checks, Mapping) or not isinstance(
            raw_conversations, Mapping
        ):
            return False
        if raw_checks.get("strict") is not True:
            return False
        if raw_conversations.get("enabled") is not True:
            return False

        names: set[str] = set()
        contexts = raw_checks.get("contexts")
        if isinstance(contexts, list):
            for context in contexts:
                if not isinstance(context, str) or not context.strip():
                    raise GateError(
                        "GitHub required status check context is invalid"
                    )
                names.add(context)
        checks = raw_checks.get("checks")
        if isinstance(checks, list):
            for raw_check in checks:
                check = _require_object(
                    raw_check,
                    "required status check",
                )
                names.add(
                    _require_text(
                        check.get("context"),
                        "required status check context",
                    )
                )
        return set(required_check_names).issubset(names)

    def get_merge_evidence(
        self,
        pr_url: str,
        required_check_names: Sequence[str],
        *,
        include_safe_files: bool = True,
    ) -> GitHubMergeEvidence:
        """Read all current-HEAD evidence needed by safe/full merge decisions."""

        if not isinstance(include_safe_files, bool):
            raise GateError("include_safe_files must be true or false")

        repository, pr_number, payload = self._read_pull_request(pr_url)
        write_state = self._pull_request_state(
            pr_url,
            repository,
            pr_number,
            payload,
        )
        if write_state.is_merged:
            assert write_state.merged_commit is not None
            merged_base, merged_head = self._read_merge_parents(
                repository,
                write_state.merged_commit,
            )
            write_state = replace(
                write_state,
                merged_base_commit=merged_base,
                merged_head_commit=merged_head,
            )
        draft = payload.get("draft")
        if not isinstance(draft, bool):
            raise GateError("GitHub PR draft flag is invalid")
        base_commit = (
            write_state.merged_base_commit
            if write_state.is_merged
            else write_state.base_commit
        )
        head_commit = (
            write_state.merged_head_commit
            if write_state.is_merged
            else write_state.head_commit
        )
        assert base_commit is not None
        assert head_commit is not None
        changed_file_count = _require_integer(
            payload.get("changed_files"),
            "changed file count",
        )
        mergeable = payload.get("mergeable")
        mergeable_state = payload.get("mergeable_state")
        if write_state.is_merged:
            has_conflict = False
            rules_allow_merge = False
        else:
            if not isinstance(mergeable, bool):
                raise GateError("GitHub PR mergeable flag is unknown")
            if mergeable_state not in _MERGEABLE_STATES:
                raise GateError("GitHub PR mergeable state is unknown")
            if mergeable_state == "dirty" and mergeable is not False:
                raise GateError("GitHub PR conflict state is inconsistent")
            if (
                mergeable_state in _MERGEABLE_ALLOWED_STATES
                and mergeable is not True
            ):
                raise GateError("GitHub PR mergeable state is inconsistent")
            has_conflict = mergeable_state == "dirty"
            rules_allow_merge = mergeable_state in _MERGEABLE_ALLOWED_STATES

        changed_files: tuple[ChangedFile, ...] = ()
        safe_files: SafeFilesEvidence | None = None
        files_pagination_complete: bool | None = None
        if include_safe_files:
            changed_files = self._read_changed_files(
                repository,
                pr_number,
                changed_file_count,
                base_commit=base_commit,
                head_commit=head_commit,
            )
            safe_files = SafeFilesEvidence(
                base_commit=base_commit,
                head_commit=head_commit,
                result=check_safe_files(
                    changed_files,
                    pagination_complete=True,
                ),
            )
            files_pagination_complete = True
        checks = self._read_checks(
            repository,
            head_commit,
            required_check_names,
        )
        unresolved_threads = self._read_unresolved_review_threads(
            repository,
            pr_number,
        )
        base_is_current = self._base_is_current(
            repository,
            base_commit,
            head_commit,
        )
        server_requires_current_base = self._server_protects_merge_snapshot(
            repository,
            write_state.base_ref,
            required_check_names,
        )
        return GitHubMergeEvidence(
            pr_url=pr_url,
            repository=repository,
            pr_number=pr_number,
            head_commit=head_commit,
            base_commit=base_commit,
            is_open=write_state.is_open,
            is_draft=draft,
            is_merged=write_state.is_merged,
            merged_commit=write_state.merged_commit,
            merged_base_commit=write_state.merged_base_commit,
            merged_head_commit=write_state.merged_head_commit,
            has_conflict=has_conflict,
            base_is_current=base_is_current,
            rules_allow_merge=rules_allow_merge and server_requires_current_base,
            server_requires_current_base=server_requires_current_base,
            unresolved_review_threads=unresolved_threads,
            checks=checks,
            changed_files=changed_files,
            files_pagination_complete=files_pagination_complete,
            safe_files=safe_files,
        )


class GitHubTaskIssueClient:
    """Create and resume confirmed Forge Task issues through ``gh api``."""

    def __init__(
        self,
        gh_path: str | Path,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._gh_path = str(Path(gh_path).expanduser())
        self._runner = runner

    def _run_json(self, arguments: Sequence[str], label: str) -> object:
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
    def _repository(value: str) -> str:
        if not isinstance(value, str) or _REPOSITORY_RE.fullmatch(value) is None:
            raise GateError("GitHub repository must use OWNER/REPO format")
        return value

    @staticmethod
    def _issue_number(value: int) -> int:
        if type(value) is not int or value <= 0:
            raise GateError("GitHub issue number must be a positive integer")
        return value

    @staticmethod
    def _parse_issue(value: object) -> TaskIssue:
        if not isinstance(value, dict) or "pull_request" in value:
            raise GateError("GitHub issue response is not an issue object")
        number = value.get("number")
        title = value.get("title")
        body = value.get("body")
        raw_labels = value.get("labels")
        if type(number) is not int or number <= 0:
            raise GateError("GitHub issue number is invalid")
        if not isinstance(title, str) or not title.strip():
            raise GateError("GitHub issue title is invalid")
        if not isinstance(body, str):
            raise GateError("GitHub issue body is invalid")
        if not isinstance(raw_labels, list):
            raise GateError("GitHub issue labels are invalid")
        labels: list[str] = []
        for raw_label in raw_labels:
            if not isinstance(raw_label, dict):
                raise GateError("GitHub issue label is invalid")
            name = raw_label.get("name")
            if not isinstance(name, str) or not name.strip():
                raise GateError("GitHub issue label is invalid")
            labels.append(name)
        if len(labels) != len(set(labels)):
            raise GateError("GitHub issue labels contain duplicates")
        return TaskIssue(
            number=number,
            title=title,
            body=body,
            labels=tuple(sorted(labels)),
        )

    def find_issue(self, repository: str, request_id: str) -> TaskIssue | None:
        repository = self._repository(repository)
        if not isinstance(request_id, str) or _REQUEST_ID_RE.fullmatch(request_id) is None:
            raise GateError("GitHub Task request_id is invalid")
        # RISK(data-loss): --paginate --slurp is required. An incomplete issue
        # list could miss a prior create and cause a duplicate issue.
        payload = self._run_json(
            (
                "--paginate",
                "--slurp",
                f"repos/{repository}/issues?state=all&per_page=100",
            ),
            "issue list",
        )
        if not isinstance(payload, list) or any(
            not isinstance(page, list) for page in payload
        ):
            raise GateError("GitHub paginated issue response is invalid")
        matches: list[TaskIssue] = []
        for page in payload:
            for raw_issue in page:
                if not isinstance(raw_issue, dict):
                    raise GateError("GitHub issue list contains an invalid item")
                if "pull_request" in raw_issue:
                    continue
                body = raw_issue.get("body")
                if body is None:
                    continue
                if not isinstance(body, str):
                    raise GateError("GitHub issue body is invalid")
                if "<!-- forge-task-request" not in body:
                    continue
                try:
                    marker = read_task_marker(body)
                except TaskServiceError as error:
                    raise GateError("GitHub issue Task marker is invalid") from error
                if marker.get("request_id") == request_id:
                    matches.append(self._parse_issue(raw_issue))
        if len(matches) > 1:
            raise GateError("GitHub request_id matched more than one issue")
        return matches[0] if matches else None

    def create_issue(self, repository: str, title: str, body: str) -> TaskIssue:
        repository = self._repository(repository)
        if not isinstance(title, str) or not title.strip():
            raise GateError("GitHub issue title must be non-empty text")
        if not isinstance(body, str):
            raise GateError("GitHub issue body must be text")
        payload = self._run_json(
            (
                "-X",
                "POST",
                f"repos/{repository}/issues",
                "-f",
                f"title={title}",
                "-f",
                f"body={body}",
            ),
            "issue create",
        )
        return self._parse_issue(payload)

    def update_issue(
        self,
        repository: str,
        issue_number: int,
        *,
        title: str,
        body: str,
    ) -> TaskIssue:
        repository = self._repository(repository)
        issue_number = self._issue_number(issue_number)
        if not isinstance(title, str) or not title.strip():
            raise GateError("GitHub issue title must be non-empty text")
        if not isinstance(body, str):
            raise GateError("GitHub issue body must be text")
        payload = self._run_json(
            (
                "-X",
                "PATCH",
                f"repos/{repository}/issues/{issue_number}",
                "-f",
                f"title={title}",
                "-f",
                f"body={body}",
            ),
            "issue update",
        )
        return self._parse_issue(payload)

    def get_issue(self, repository: str, issue_number: int) -> TaskIssue:
        repository = self._repository(repository)
        issue_number = self._issue_number(issue_number)
        payload = self._run_json(
            (f"repos/{repository}/issues/{issue_number}",),
            "issue readback",
        )
        return self._parse_issue(payload)

    def add_label(
        self,
        repository: str,
        issue_number: int,
        label: str,
    ) -> TaskIssue:
        repository = self._repository(repository)
        issue_number = self._issue_number(issue_number)
        if not isinstance(label, str) or not label.strip():
            raise GateError("GitHub issue label must be non-empty text")
        payload = self._run_json(
            (
                "-X",
                "POST",
                f"repos/{repository}/issues/{issue_number}/labels",
                "-f",
                f"labels[]={label}",
            ),
            "issue label",
        )
        if not isinstance(payload, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not item["name"].strip()
            for item in payload
        ):
            raise GateError("GitHub issue label response is invalid")
        return self.get_issue(repository, issue_number)


class GitHubTaskIssueClientV2:
    """Find and mutate only v2 central parent issues through ``gh api``."""

    def __init__(
        self,
        gh_path: str | Path,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._gh_path = str(Path(gh_path).expanduser())
        self._runner = runner

    def _run_json(self, arguments: Sequence[str], label: str) -> object:
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

    def _run_mutation(self, arguments: Sequence[str], label: str) -> object | None:
        """Accept a valid JSON response or GitHub's successful 204 empty body."""

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
        if not result.stdout.strip():
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise GateError(f"GitHub {label} response is not valid JSON") from error

    @staticmethod
    def _repository(value: str) -> str:
        if not isinstance(value, str) or _REPOSITORY_RE.fullmatch(value) is None:
            raise GateError("GitHub repository must use OWNER/REPO format")
        return value

    @staticmethod
    def _issue_number(value: int) -> int:
        if type(value) is not int or value <= 0:
            raise GateError("GitHub issue number must be a positive integer")
        return value

    @staticmethod
    def _parse_parent_issue(value: object) -> TaskParentIssue:
        if not isinstance(value, dict) or "pull_request" in value:
            raise GateError("GitHub parent issue response is not an issue object")
        number = value.get("number")
        title = value.get("title")
        body = value.get("body")
        state = value.get("state")
        if type(number) is not int or number <= 0:
            raise GateError("GitHub parent issue number is invalid")
        if not isinstance(title, str) or not title.strip():
            raise GateError("GitHub parent issue title is invalid")
        if not isinstance(body, str):
            raise GateError("GitHub parent issue body is invalid")
        if type(state) is not str or state not in {"open", "closed"}:
            raise GateError("GitHub parent issue state is invalid")
        try:
            return TaskParentIssue(
                number=number,
                title=title,
                body=body,
                state=state,
            )
        except TaskServiceError as error:
            raise GateError("GitHub parent issue response is invalid") from error

    @staticmethod
    def _parse_stop_issue(value: object) -> TaskStopIssueState:
        if not isinstance(value, dict) or "pull_request" in value:
            raise GateError("GitHub Stop parent response is not an issue object")
        number = value.get("number")
        title = value.get("title")
        body = value.get("body")
        state = value.get("state")
        state_reason = value.get("state_reason")
        raw_labels = value.get("labels")
        if type(number) is not int or number <= 0:
            raise GateError("GitHub Stop parent issue number is invalid")
        if not isinstance(title, str) or not title.strip():
            raise GateError("GitHub Stop parent issue title is invalid")
        if not isinstance(body, str):
            raise GateError("GitHub Stop parent issue body is invalid")
        if state not in {"open", "closed"}:
            raise GateError("GitHub Stop parent issue state is invalid")
        if state_reason not in {None, "completed", "not_planned", "reopened"}:
            raise GateError("GitHub Stop parent issue state reason is invalid")
        if not isinstance(raw_labels, list):
            raise GateError("GitHub Stop parent issue labels are invalid")
        labels: list[str] = []
        for raw_label in raw_labels:
            if not isinstance(raw_label, dict):
                raise GateError("GitHub Stop parent issue label is invalid")
            name = raw_label.get("name")
            if not isinstance(name, str) or not name.strip():
                raise GateError("GitHub Stop parent issue label is invalid")
            labels.append(name)
        if len(labels) != len(set(labels)):
            raise GateError("GitHub Stop parent issue labels contain duplicates")
        return TaskStopIssueState(
            number=number,
            title=title,
            body=body,
            state=state,
            state_reason=state_reason,
            labels=tuple(sorted(labels)),
        )

    def get_stop_issue(
        self,
        repository: str,
        issue_number: int,
    ) -> TaskStopIssueState:
        """Read all fields required to prove one Stop cleanup result."""

        repository = self._repository(repository)
        issue_number = self._issue_number(issue_number)
        payload = self._run_json(
            (f"repos/{repository}/issues/{issue_number}",),
            "Stop parent issue readback",
        )
        issue = self._parse_stop_issue(payload)
        if issue.number != issue_number:
            raise GateError("GitHub Stop parent issue number changed")
        return issue

    def find_stop_issue(
        self,
        repository: str,
        request_id: str,
    ) -> TaskStopIssueState | None:
        """Find an open or closed v2 parent after an ambiguous create."""

        repository = self._repository(repository)
        if not _is_canonical_v2_request_id(request_id):
            raise GateError("GitHub v2 Task request_id is invalid")
        payload = self._run_json(
            (
                "--paginate",
                "--slurp",
                f"repos/{repository}/issues?state=all&per_page=100",
            ),
            "Stop parent issue list",
        )
        if not isinstance(payload, list) or any(
            not isinstance(page, list) for page in payload
        ):
            raise GateError("GitHub paginated Stop parent response is invalid")
        matches: list[TaskStopIssueState] = []
        for page in payload:
            for raw_issue in page:
                if not isinstance(raw_issue, dict):
                    raise GateError("GitHub Stop parent list contains an invalid item")
                if "pull_request" in raw_issue:
                    continue
                body = raw_issue.get("body")
                if body is None:
                    continue
                if not isinstance(body, str):
                    raise GateError("GitHub Stop parent issue body is invalid")
                if "<!-- forge-v2-task-request" not in body:
                    continue
                try:
                    marker = read_task_marker_v2(body)
                except TaskServiceError as error:
                    raise GateError(
                        "GitHub Stop parent Task marker is invalid"
                    ) from error
                if marker.get("request_id") == request_id:
                    matches.append(self._parse_stop_issue(raw_issue))
        if len(matches) > 1:
            raise GateError("GitHub v2 request_id matched more than one Stop parent")
        return matches[0] if matches else None

    def reconcile_stop_status(
        self,
        repository: str,
        issue_number: int,
        *,
        target: str | None,
    ) -> TaskStopIssueState:
        """Remove only Forge status labels, optionally keeping needs-decision."""

        repository = self._repository(repository)
        issue_number = self._issue_number(issue_number)
        if target not in {None, "forge:needs-decision"}:
            raise GateError("Stop cleanup target must be needs-decision or empty")
        endpoint = f"repos/{repository}/issues/{issue_number}"
        before = self.get_stop_issue(repository, issue_number)
        if target is not None and before.state != "open":
            raise GateError("partial Stop parent issue must remain open")
        forge_labels = tuple(
            label for label in before.labels if label in FORGE_STATUS_LABELS
        )
        for label in forge_labels:
            if label == target:
                continue
            # RISK(side-effect): delete only an official Forge status label;
            # never replace the complete label list owned by other writers.
            self._run_mutation(
                (
                    "-X",
                    "DELETE",
                    f"{endpoint}/labels/{quote(label, safe='')}",
                ),
                "Stop parent status remove",
            )
        if target is not None and target not in forge_labels:
            self._run_json(
                (
                    "-X",
                    "POST",
                    f"{endpoint}/labels",
                    "-f",
                    f"labels[]={target}",
                ),
                "Stop parent needs-decision add",
            )
        after = self.get_stop_issue(repository, issue_number)
        expected = () if target is None else (target,)
        actual = tuple(label for label in after.labels if label in FORGE_STATUS_LABELS)
        if actual != expected:
            raise GateError("GitHub Stop parent status readback does not match")
        unrelated = {
            label for label in before.labels if label not in FORGE_STATUS_LABELS
        }
        if not unrelated.issubset(after.labels):
            raise GateError("GitHub Stop cleanup lost an unrelated label")
        return after

    def ensure_stop_comment(
        self,
        repository: str,
        issue_number: int,
        stop_request_id: str,
        body: str,
    ) -> str:
        """Create one marker-bound result comment, or verify its exact replay."""

        repository = self._repository(repository)
        issue_number = self._issue_number(issue_number)
        if not _is_canonical_v2_request_id(stop_request_id):
            raise GateError("GitHub Stop request ID is invalid")
        marker = f"<!-- forge-task-stop:{stop_request_id} -->"
        if not isinstance(body, str) or body.count(marker) != 1:
            raise GateError("GitHub Stop comment marker is invalid")

        def matching_comments() -> list[str]:
            payload = self._run_json(
                (
                    "--paginate",
                    "--slurp",
                    f"repos/{repository}/issues/{issue_number}/comments?per_page=100",
                ),
                "Stop parent comment list",
            )
            if not isinstance(payload, list) or any(
                not isinstance(page, list) for page in payload
            ):
                raise GateError("GitHub paginated Stop comment response is invalid")
            matches: list[str] = []
            for page in payload:
                for raw_comment in page:
                    if not isinstance(raw_comment, dict):
                        raise GateError("GitHub Stop comment is invalid")
                    comment_body = raw_comment.get("body")
                    if not isinstance(comment_body, str):
                        raise GateError("GitHub Stop comment body is invalid")
                    if marker in comment_body:
                        matches.append(comment_body)
            return matches

        matches = matching_comments()
        if len(matches) > 1:
            raise GateError("GitHub Stop comment marker is duplicated")
        if matches:
            if matches[0] != body:
                raise GateError("GitHub Stop comment marker body changed")
            return matches[0]
        self._run_json(
            (
                "-X",
                "POST",
                f"repos/{repository}/issues/{issue_number}/comments",
                "-f",
                f"body={body}",
            ),
            "Stop parent comment create",
        )
        matches = matching_comments()
        if matches != [body]:
            raise GateError("GitHub Stop comment readback does not match")
        return body

    def close_stop_issue_not_planned(
        self,
        repository: str,
        issue_number: int,
    ) -> TaskStopIssueState:
        """Close only a cancelled parent and prove the not-planned reason."""

        repository = self._repository(repository)
        issue_number = self._issue_number(issue_number)
        before = self.get_stop_issue(repository, issue_number)
        if before.state == "closed" and before.state_reason == "not_planned":
            return before
        self._run_json(
            (
                "-X",
                "PATCH",
                f"repos/{repository}/issues/{issue_number}",
                "-f",
                "state=closed",
                "-f",
                "state_reason=not_planned",
            ),
            "Stop parent close",
        )
        after = self.get_stop_issue(repository, issue_number)
        if after.state != "closed" or after.state_reason != "not_planned":
            raise GateError("GitHub Stop parent close readback does not match")
        return after

    def find_issue(
        self,
        repository: str,
        request_id: str,
    ) -> TaskParentIssue | None:
        repository = self._repository(repository)
        if not _is_canonical_v2_request_id(request_id):
            raise GateError("GitHub v2 Task request_id is invalid")
        # RISK(data-loss): every page and every state is required. Missing a
        # prior v2 parent after a lost create response would duplicate the Task.
        payload = self._run_json(
            (
                "--paginate",
                "--slurp",
                f"repos/{repository}/issues?state=all&per_page=100",
            ),
            "v2 parent issue list",
        )
        if not isinstance(payload, list) or any(
            not isinstance(page, list) for page in payload
        ):
            raise GateError("GitHub paginated parent issue response is invalid")
        matches: list[TaskParentIssue] = []
        for page in payload:
            for raw_issue in page:
                if not isinstance(raw_issue, dict):
                    raise GateError("GitHub parent issue list contains an invalid item")
                if "pull_request" in raw_issue:
                    continue
                body = raw_issue.get("body")
                if body is None:
                    continue
                if not isinstance(body, str):
                    raise GateError("GitHub parent issue body is invalid")
                if "<!-- forge-v2-task-request" not in body:
                    continue
                try:
                    marker = read_task_marker_v2(body)
                except TaskServiceError as error:
                    raise GateError("GitHub parent issue Task marker is invalid") from error
                if marker.get("request_id") != request_id:
                    continue
                issue = self._parse_parent_issue(raw_issue)
                if issue.state != "open":
                    raise GateError("GitHub v2 parent issue is closed")
                matches.append(issue)
        if len(matches) > 1:
            raise GateError("GitHub v2 request_id matched more than one parent issue")
        return matches[0] if matches else None

    def create_issue(
        self,
        repository: str,
        title: str,
        body: str,
    ) -> TaskParentIssue:
        repository = self._repository(repository)
        if not isinstance(title, str) or not title.strip():
            raise GateError("GitHub parent issue title must be non-empty text")
        if not isinstance(body, str):
            raise GateError("GitHub parent issue body must be text")
        payload = self._run_json(
            (
                "-X",
                "POST",
                f"repos/{repository}/issues",
                "-f",
                f"title={title}",
                "-f",
                f"body={body}",
            ),
            "v2 parent issue create",
        )
        return self._parse_parent_issue(payload)

    def update_issue(
        self,
        repository: str,
        issue_number: int,
        *,
        title: str,
        body: str,
    ) -> TaskParentIssue:
        repository = self._repository(repository)
        issue_number = self._issue_number(issue_number)
        if not isinstance(title, str) or not title.strip():
            raise GateError("GitHub parent issue title must be non-empty text")
        if not isinstance(body, str):
            raise GateError("GitHub parent issue body must be text")
        payload = self._run_json(
            (
                "-X",
                "PATCH",
                f"repos/{repository}/issues/{issue_number}",
                "-f",
                f"title={title}",
                "-f",
                f"body={body}",
            ),
            "v2 parent issue update",
        )
        return self._parse_parent_issue(payload)

    def get_issue(
        self,
        repository: str,
        issue_number: int,
    ) -> TaskParentIssue:
        repository = self._repository(repository)
        issue_number = self._issue_number(issue_number)
        payload = self._run_json(
            (f"repos/{repository}/issues/{issue_number}",),
            "v2 parent issue readback",
        )
        return self._parse_parent_issue(payload)
