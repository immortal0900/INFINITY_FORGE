"""Strict, side-effect-free contracts for Forge Task evidence."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias


__all__ = [
    "BUILD_RESULT_REQUIRED_FIELDS",
    "BuildResult",
    "CheckRun",
    "ContractError",
    "DEEP_CHECK_RESULT_REQUIRED_FIELDS",
    "DeepCheckDecision",
    "DeepCheckResult",
    "REVIEW_RESULT_REQUIRED_FIELDS",
    "ReviewDecision",
    "ReviewResult",
    "RunRecord",
    "STEP_PROOF_REQUIRED_FIELDS",
    "StepProof",
    "TaskRecord",
    "TaskResult",
    "parse_build_result",
    "parse_deep_check_result",
    "parse_review_result",
    "parse_step_proof",
    "parse_task_result",
    "source_result_hash",
    "task_result_payload",
    "validate_task_result_binding",
]


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_GITHUB_PR_RE = re.compile(
    r"^https://github\.com/[^/]+/[^/]+/pull/[1-9][0-9]*$"
)
# RISK(public-api): These exact fields are the clean-break worker contract.
# Accepting aliases can attach proof to the wrong Task or pull request.
BUILD_RESULT_REQUIRED_FIELDS = frozenset(
    {
        "format_version",
        "task_settings_hash",
        "pr_url",
        "built_base_commit",
        "built_commit",
        "changed_files",
        "completed_items",
        "remaining_items",
        "checks_by_item",
    }
)
REVIEW_RESULT_REQUIRED_FIELDS = frozenset(
    {
        "format_version",
        "task_settings_hash",
        "result",
        "source_result_hash",
        "pr_url",
        "reviewed_commit",
        "change_check",
        "requirements_check",
        "fix_notes",
    }
)
DEEP_CHECK_RESULT_REQUIRED_FIELDS = frozenset(
    {
        "format_version",
        "task_settings_hash",
        "result",
        "source_result_hash",
        "pr_url",
        "reviewed_commit",
        "tested_commit",
        "added_tests",
        "tested_cases",
        "fix_notes",
    }
)
STEP_PROOF_REQUIRED_FIELDS = frozenset(
    {
        "format_version",
        "tested_commit",
        "pr_url",
        "fix_notes",
        "source_result_hash",
        "source_run_id",
        "source_task_id",
        "task_settings_hash",
    }
)

class ContractError(ValueError):
    """Raised when Task evidence does not satisfy its declared contract."""


class ReviewDecision(str, Enum):
    APPROVE = "approve"
    CHANGES_NEEDED = "changes_needed"


class DeepCheckDecision(str, Enum):
    PASS = "pass"
    PROBLEMS_FOUND = "problems_found"


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    title: str
    status: str
    body: str | None = None
    parent_id: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class RunRecord:
    run_id: int
    task_id: str
    status: str
    outcome: str | None
    summary: Mapping[str, object]
    metadata: Mapping[str, object]


@dataclass(frozen=True)
class CheckRun:
    name: str
    status: str
    conclusion: str | None
    head_sha: str


@dataclass(frozen=True)
class BuildResult:
    format_version: str
    task_settings_hash: str
    pr_url: str
    built_base_commit: str
    built_commit: str
    changed_files: tuple[str, ...]
    completed_items: tuple[str, ...]
    remaining_items: tuple[str, ...]
    checks_by_item: Mapping[str, str]


@dataclass(frozen=True)
class ReviewResult:
    format_version: str
    task_settings_hash: str
    result: ReviewDecision
    source_result_hash: str
    pr_url: str
    reviewed_commit: str
    change_check: Mapping[str, tuple[str, ...]]
    requirements_check: Mapping[str, tuple[str, ...]]
    fix_notes: str | None


@dataclass(frozen=True)
class DeepCheckResult:
    format_version: str
    task_settings_hash: str
    result: DeepCheckDecision
    source_result_hash: str
    pr_url: str
    reviewed_commit: str
    tested_commit: str
    added_tests: tuple[str, ...]
    tested_cases: tuple[str, ...]
    fix_notes: str | None


@dataclass(frozen=True)
class StepProof:
    format_version: str
    tested_commit: str
    pr_url: str
    fix_notes: str | None
    source_result_hash: str
    source_run_id: int
    source_task_id: str
    task_settings_hash: str


TaskResult: TypeAlias = BuildResult | ReviewResult | DeepCheckResult


def _require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{label} must be an object")
    return value


def _require_exact_fields(
    value: Mapping[str, object],
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    actual = set(value)
    unexpected = actual - required - optional
    if unexpected:
        raise ContractError(
            f"unexpected fields: {', '.join(sorted(str(key) for key in unexpected))}"
        )
    missing = required - actual
    if missing:
        raise ContractError(
            f"missing required field: {', '.join(sorted(missing))}"
        )


def _require_string(
    value: Mapping[str, object], key: str, *, pattern: re.Pattern[str] | None = None
) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ContractError(f"{key} must be a non-empty string")
    if pattern is not None and pattern.fullmatch(item) is None:
        raise ContractError(f"{key} has an invalid format")
    return item


def _require_string_array(
    value: Mapping[str, object], key: str, *, non_empty: bool
) -> tuple[str, ...]:
    item = value.get(key)
    if not isinstance(item, list):
        raise ContractError(f"{key} must be an array of strings")
    if non_empty and not item:
        raise ContractError(f"{key} must be a non-empty array of strings")
    if any(not isinstance(entry, str) or not entry.strip() for entry in item):
        raise ContractError(f"{key} must contain only non-empty strings")
    return tuple(item)


def _require_check_object(
    value: Mapping[str, object], key: str, fields: set[str]
) -> Mapping[str, tuple[str, ...]]:
    item = _require_mapping(value.get(key), key)
    _require_exact_fields(item, required=fields)
    return {
        field: _require_string_array(item, field, non_empty=False)
        for field in sorted(fields)
    }


def _require_nullable_string(
    value: Mapping[str, object], key: str
) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str) or not item.strip():
        raise ContractError(f"{key} must be null or a non-empty string")
    return item


def _require_positive_integer(value: Mapping[str, object], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool) or item < 1:
        raise ContractError(f"{key} must be a positive integer")
    return item


def _require_string_mapping(
    value: Mapping[str, object], key: str
) -> Mapping[str, str]:
    item = _require_mapping(value.get(key), key)
    result: dict[str, str] = {}
    for map_key, map_value in item.items():
        if not isinstance(map_key, str) or not map_key.strip():
            raise ContractError(f"{key} keys must be non-empty strings")
        if not isinstance(map_value, str) or not map_value.strip():
            raise ContractError(f"{key} values must be non-empty strings")
        result[map_key] = map_value
    return result


def parse_build_result(summary: Mapping[str, object]) -> BuildResult:
    """Parse exact Build output without aliases or legacy fallbacks."""

    parsed = _require_mapping(summary, "build result")
    _require_exact_fields(parsed, required=BUILD_RESULT_REQUIRED_FIELDS)
    format_version = _require_string(parsed, "format_version")
    if format_version != "forge-build-result/v1":
        raise ContractError("format_version must be 'forge-build-result/v1'")
    return BuildResult(
        format_version=format_version,
        task_settings_hash=_require_string(
            parsed, "task_settings_hash", pattern=_SHA256_RE
        ),
        pr_url=_require_string(parsed, "pr_url", pattern=_GITHUB_PR_RE),
        built_base_commit=_require_string(
            parsed, "built_base_commit", pattern=_GIT_SHA_RE
        ),
        built_commit=_require_string(parsed, "built_commit", pattern=_GIT_SHA_RE),
        changed_files=_require_string_array(parsed, "changed_files", non_empty=False),
        completed_items=_require_string_array(
            parsed, "completed_items", non_empty=False
        ),
        remaining_items=_require_string_array(
            parsed, "remaining_items", non_empty=False
        ),
        checks_by_item=_require_string_mapping(parsed, "checks_by_item"),
    )


def parse_review_result(summary: Mapping[str, object]) -> ReviewResult:
    """Parse exact Review output and its conditional fix notes."""

    parsed = _require_mapping(summary, "review result")
    _require_exact_fields(parsed, required=REVIEW_RESULT_REQUIRED_FIELDS)
    format_version = _require_string(parsed, "format_version")
    if format_version != "forge-review-result/v1":
        raise ContractError("format_version must be 'forge-review-result/v1'")
    result_text = _require_string(parsed, "result")
    try:
        result = ReviewDecision(result_text)
    except ValueError as error:
        raise ContractError("result must be 'approve' or 'changes_needed'") from error
    fix_notes = _require_nullable_string(parsed, "fix_notes")
    if result is ReviewDecision.CHANGES_NEEDED and fix_notes is None:
        raise ContractError("fix_notes must explain changes_needed")
    if result is ReviewDecision.APPROVE and fix_notes is not None:
        raise ContractError("fix_notes must be null for approve")
    return ReviewResult(
        format_version=format_version,
        task_settings_hash=_require_string(
            parsed, "task_settings_hash", pattern=_SHA256_RE
        ),
        result=result,
        source_result_hash=_require_string(
            parsed, "source_result_hash", pattern=_SHA256_RE
        ),
        pr_url=_require_string(parsed, "pr_url", pattern=_GITHUB_PR_RE),
        reviewed_commit=_require_string(
            parsed, "reviewed_commit", pattern=_GIT_SHA_RE
        ),
        change_check=_require_check_object(
            parsed, "change_check", {"confirmed_work", "problems"}
        ),
        requirements_check=_require_check_object(
            parsed, "requirements_check", {"completed", "missing"}
        ),
        fix_notes=fix_notes,
    )


def parse_deep_check_result(summary: Mapping[str, object]) -> DeepCheckResult:
    """Parse exact Deep Check output and its conditional fix notes."""

    parsed = _require_mapping(summary, "deep check result")
    _require_exact_fields(parsed, required=DEEP_CHECK_RESULT_REQUIRED_FIELDS)
    format_version = _require_string(parsed, "format_version")
    if format_version != "forge-deep-check-result/v1":
        raise ContractError("format_version must be 'forge-deep-check-result/v1'")
    result_text = _require_string(parsed, "result")
    try:
        result = DeepCheckDecision(result_text)
    except ValueError as error:
        raise ContractError("result must be 'pass' or 'problems_found'") from error
    fix_notes = _require_nullable_string(parsed, "fix_notes")
    if result is DeepCheckDecision.PROBLEMS_FOUND and fix_notes is None:
        raise ContractError("fix_notes must explain problems_found")
    if result is DeepCheckDecision.PASS and fix_notes is not None:
        raise ContractError("fix_notes must be null for pass")
    return DeepCheckResult(
        format_version=format_version,
        task_settings_hash=_require_string(
            parsed, "task_settings_hash", pattern=_SHA256_RE
        ),
        result=result,
        source_result_hash=_require_string(
            parsed, "source_result_hash", pattern=_SHA256_RE
        ),
        pr_url=_require_string(parsed, "pr_url", pattern=_GITHUB_PR_RE),
        reviewed_commit=_require_string(
            parsed, "reviewed_commit", pattern=_GIT_SHA_RE
        ),
        tested_commit=_require_string(parsed, "tested_commit", pattern=_GIT_SHA_RE),
        added_tests=_require_string_array(parsed, "added_tests", non_empty=False),
        tested_cases=_require_string_array(parsed, "tested_cases", non_empty=False),
        fix_notes=fix_notes,
    )


def parse_step_proof(summary: Mapping[str, object]) -> StepProof:
    """Parse exact proof attached to a Task child card."""

    parsed = _require_mapping(summary, "step proof")
    _require_exact_fields(parsed, required=STEP_PROOF_REQUIRED_FIELDS)
    format_version = _require_string(parsed, "format_version")
    if format_version != "forge-step-proof/v1":
        raise ContractError("format_version must be 'forge-step-proof/v1'")
    return StepProof(
        format_version=format_version,
        tested_commit=_require_string(parsed, "tested_commit", pattern=_GIT_SHA_RE),
        pr_url=_require_string(parsed, "pr_url", pattern=_GITHUB_PR_RE),
        fix_notes=_require_nullable_string(parsed, "fix_notes"),
        source_result_hash=_require_string(
            parsed, "source_result_hash", pattern=_SHA256_RE
        ),
        source_run_id=_require_positive_integer(parsed, "source_run_id"),
        source_task_id=_require_string(parsed, "source_task_id"),
        task_settings_hash=_require_string(
            parsed, "task_settings_hash", pattern=_SHA256_RE
        ),
    )


def parse_task_result(step: str, summary: Mapping[str, object]) -> TaskResult:
    """Dispatch only the three result-producing Task step names."""

    if step == "build":
        return parse_build_result(summary)
    if step == "review":
        return parse_review_result(summary)
    if step == "deep_check":
        return parse_deep_check_result(summary)
    raise ContractError("step must be 'build', 'review', or 'deep_check'")


def task_result_payload(result: TaskResult) -> Mapping[str, object]:
    """Return the exact JSON-ready representation of one Task result."""

    if isinstance(result, BuildResult):
        return {
            "format_version": result.format_version,
            "task_settings_hash": result.task_settings_hash,
            "pr_url": result.pr_url,
            "built_base_commit": result.built_base_commit,
            "built_commit": result.built_commit,
            "changed_files": list(result.changed_files),
            "completed_items": list(result.completed_items),
            "remaining_items": list(result.remaining_items),
            "checks_by_item": dict(result.checks_by_item),
        }
    if isinstance(result, ReviewResult):
        return {
            "format_version": result.format_version,
            "task_settings_hash": result.task_settings_hash,
            "result": result.result.value,
            "source_result_hash": result.source_result_hash,
            "pr_url": result.pr_url,
            "reviewed_commit": result.reviewed_commit,
            "change_check": {
                key: list(items) for key, items in result.change_check.items()
            },
            "requirements_check": {
                key: list(items) for key, items in result.requirements_check.items()
            },
            "fix_notes": result.fix_notes,
        }
    if isinstance(result, DeepCheckResult):
        return {
            "format_version": result.format_version,
            "task_settings_hash": result.task_settings_hash,
            "result": result.result.value,
            "source_result_hash": result.source_result_hash,
            "pr_url": result.pr_url,
            "reviewed_commit": result.reviewed_commit,
            "tested_commit": result.tested_commit,
            "added_tests": list(result.added_tests),
            "tested_cases": list(result.tested_cases),
            "fix_notes": result.fix_notes,
        }
    raise ContractError("result must be a Build, Review, or Deep Check result")


def source_result_hash(result: TaskResult) -> str:
    """Hash one exact worker result using canonical UTF-8 JSON."""

    canonical = json.dumps(
        task_result_payload(result),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def validate_task_result_binding(
    result: TaskResult,
    *,
    expected_task_settings_hash: str,
    expected_pr_url: str,
    current_base_commit: str,
    current_commit: str,
) -> None:
    """Bind result proof to the exact Task settings, PR, and current commit."""

    if _SHA256_RE.fullmatch(expected_task_settings_hash) is None:
        raise ContractError("expected_task_settings_hash has an invalid format")
    if _GITHUB_PR_RE.fullmatch(expected_pr_url) is None:
        raise ContractError("expected_pr_url has an invalid format")
    if _GIT_SHA_RE.fullmatch(current_commit) is None:
        raise ContractError("current_commit has an invalid format")
    if _GIT_SHA_RE.fullmatch(current_base_commit) is None:
        raise ContractError("current_base_commit has an invalid format")
    if not isinstance(result, (BuildResult, ReviewResult, DeepCheckResult)):
        raise ContractError("unsupported Task result binding")
    if result.task_settings_hash != expected_task_settings_hash:
        raise ContractError("task_settings_hash does not match Task settings")
    if result.pr_url != expected_pr_url:
        raise ContractError("pr_url does not match the Task PR")

    if isinstance(result, BuildResult):
        if result.built_base_commit != current_base_commit:
            raise ContractError("built base commit does not match current base commit")
        result_commit = result.built_commit
    elif isinstance(result, ReviewResult):
        result_commit = result.reviewed_commit
    else:
        result_commit = result.tested_commit
    if result_commit != current_commit:
        raise ContractError("result commit does not match current commit")
