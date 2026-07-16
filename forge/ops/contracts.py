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
    "PullRequestSnapshot",
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
_GITHUB_REPOSITORY_RE = re.compile(r"^[^/\s]+/[^/\s]+$")

# RISK(public-api): These exact fields are the clean-break worker contract.
# Accepting aliases can attach proof to the wrong Task or pull request.
BUILD_RESULT_REQUIRED_FIELDS = frozenset(
    {
        "format_version",
        "task_settings_hash",
        "pr_url",
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

# Isolated compatibility data for the old modules pending their Task 8 removal.
REVIEWER_RESULT_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "verdict",
        "source_digest",
        "pr_url",
        "head_sha",
        "delta_check",
        "spec_check",
    }
)
REVIEWER_RESULT_OPTIONAL_FIELDS = frozenset({"reflection"})
CRITIC_RESULT_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "outcome",
        "source_digest",
        "pr_url",
        "reviewed_head_sha",
        "result_head_sha",
        "added_tests",
        "scenarios",
    }
)
CRITIC_RESULT_OPTIONAL_FIELDS = frozenset({"reflection"})


class ContractError(ValueError):
    """Raised when Task evidence does not satisfy its declared contract."""


class ReviewDecision(str, Enum):
    APPROVE = "approve"
    CHANGES_NEEDED = "changes_needed"


class DeepCheckDecision(str, Enum):
    PASS = "pass"
    PROBLEMS_FOUND = "problems_found"


class PipelineStage(str, Enum):
    EXECUTOR = "executor"
    REVIEWER = "reviewer"
    CRITIC = "critic"
    EXECUTOR_REWORK = "executor-rework"


class StageOutcome(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    PASS = "pass"
    DEFECT_FOUND = "defect_found"


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
class PullRequestSnapshot:
    pr_url: str
    repository: str
    pr_number: int
    head_sha: str
    is_open: bool
    is_draft: bool
    checks: tuple[CheckRun, ...]


@dataclass(frozen=True)
class BuildResult:
    format_version: str
    task_settings_hash: str
    pr_url: str
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


@dataclass(frozen=True)
class ExecutorResult:
    pr_url: str
    changed_files: tuple[str, ...]
    implemented: tuple[str, ...]
    not_implemented: tuple[object, ...]
    verified_by: Mapping[str, object]


@dataclass(frozen=True)
class ReviewerResult:
    schema_version: str
    verdict: StageOutcome
    source_digest: str
    pr_url: str
    head_sha: str
    delta_check: Mapping[str, tuple[str, ...]]
    spec_check: Mapping[str, tuple[str, ...]]
    reflection: str | None = None


@dataclass(frozen=True)
class CriticResult:
    schema_version: str
    outcome: StageOutcome
    source_digest: str
    pr_url: str
    reviewed_head_sha: str
    result_head_sha: str
    added_tests: tuple[str, ...]
    scenarios: tuple[str, ...]
    reflection: str | None = None


StageResult: TypeAlias = ExecutorResult | ReviewerResult | CriticResult


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


def _optional_reflection(value: Mapping[str, object]) -> str | None:
    if "reflection" not in value:
        return None
    return _require_string(value, "reflection")


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
    current_commit: str,
) -> None:
    """Bind result proof to the exact Task settings, PR, and current commit."""

    if _SHA256_RE.fullmatch(expected_task_settings_hash) is None:
        raise ContractError("expected_task_settings_hash has an invalid format")
    if _GITHUB_PR_RE.fullmatch(expected_pr_url) is None:
        raise ContractError("expected_pr_url has an invalid format")
    if _GIT_SHA_RE.fullmatch(current_commit) is None:
        raise ContractError("current_commit has an invalid format")
    if not isinstance(result, (BuildResult, ReviewResult, DeepCheckResult)):
        raise ContractError("unsupported Task result binding")
    if result.task_settings_hash != expected_task_settings_hash:
        raise ContractError("task_settings_hash does not match Task settings")
    if result.pr_url != expected_pr_url:
        raise ContractError("pr_url does not match the Task PR")

    if isinstance(result, BuildResult):
        result_commit = result.built_commit
    elif isinstance(result, ReviewResult):
        result_commit = result.reviewed_commit
    else:
        result_commit = result.tested_commit
    if result_commit != current_commit:
        raise ContractError("result commit does not match current commit")


def _parse_executor(summary: Mapping[str, object]) -> ExecutorResult:
    required = {
        "pr_url",
        "changed_files",
        "implemented",
        "not_implemented",
        "verified_by",
    }
    _require_exact_fields(summary, required=required)
    not_implemented = summary.get("not_implemented")
    if not isinstance(not_implemented, list):
        raise ContractError("not_implemented must be an array")
    verified_by = _require_mapping(summary.get("verified_by"), "verified_by")
    if not verified_by:
        raise ContractError("verified_by must be a non-empty object")
    return ExecutorResult(
        pr_url=_require_string(summary, "pr_url", pattern=_GITHUB_PR_RE),
        changed_files=_require_string_array(summary, "changed_files", non_empty=False),
        implemented=_require_string_array(summary, "implemented", non_empty=True),
        not_implemented=tuple(not_implemented),
        verified_by=dict(verified_by),
    )


def _parse_reviewer(summary: Mapping[str, object]) -> ReviewerResult:
    schema_version = _require_string(summary, "schema_version")
    if schema_version != "forge-reviewer-result/v1":
        raise ContractError("schema_version must be 'forge-reviewer-result/v1'")
    verdict_text = _require_string(summary, "verdict")
    if verdict_text not in {StageOutcome.APPROVE.value, StageOutcome.REJECT.value}:
        raise ContractError("verdict must be 'approve' or 'reject'")
    if verdict_text == StageOutcome.REJECT.value:
        _require_string(summary, "reflection")

    _require_exact_fields(
        summary,
        required=REVIEWER_RESULT_REQUIRED_FIELDS,
        optional=REVIEWER_RESULT_OPTIONAL_FIELDS,
    )
    return ReviewerResult(
        schema_version=schema_version,
        verdict=StageOutcome(verdict_text),
        source_digest=_require_string(summary, "source_digest", pattern=_SHA256_RE),
        pr_url=_require_string(summary, "pr_url", pattern=_GITHUB_PR_RE),
        head_sha=_require_string(summary, "head_sha", pattern=_GIT_SHA_RE),
        delta_check=_require_check_object(
            summary,
            "delta_check",
            {"implemented_verified", "discrepancies"},
        ),
        spec_check=_require_check_object(summary, "spec_check", {"met", "unmet"}),
        reflection=_optional_reflection(summary),
    )


def _parse_critic(summary: Mapping[str, object]) -> CriticResult:
    schema_version = _require_string(summary, "schema_version")
    if schema_version != "forge-critic-result/v1":
        raise ContractError("schema_version must be 'forge-critic-result/v1'")
    outcome_text = _require_string(summary, "outcome")
    if outcome_text not in {StageOutcome.PASS.value, StageOutcome.DEFECT_FOUND.value}:
        raise ContractError("outcome must be 'pass' or 'defect_found'")

    added_tests = _require_string_array(summary, "added_tests", non_empty=True)
    if outcome_text == StageOutcome.DEFECT_FOUND.value:
        _require_string(summary, "reflection")

    _require_exact_fields(
        summary,
        required=CRITIC_RESULT_REQUIRED_FIELDS,
        optional=CRITIC_RESULT_OPTIONAL_FIELDS,
    )
    return CriticResult(
        schema_version=schema_version,
        outcome=StageOutcome(outcome_text),
        source_digest=_require_string(summary, "source_digest", pattern=_SHA256_RE),
        pr_url=_require_string(summary, "pr_url", pattern=_GITHUB_PR_RE),
        reviewed_head_sha=_require_string(
            summary, "reviewed_head_sha", pattern=_GIT_SHA_RE
        ),
        result_head_sha=_require_string(summary, "result_head_sha", pattern=_GIT_SHA_RE),
        added_tests=added_tests,
        scenarios=_require_string_array(summary, "scenarios", non_empty=True),
        reflection=_optional_reflection(summary),
    )


def parse_stage_result(
    stage: PipelineStage,
    summary: Mapping[str, object],
    metadata: Mapping[str, object],
) -> StageResult:
    """Parse one completed run without accepting missing or extra evidence."""

    if not isinstance(stage, PipelineStage):
        raise ContractError("stage must be a PipelineStage")
    parsed_summary = _require_mapping(summary, "summary")
    _require_mapping(metadata, "metadata")
    if stage in {PipelineStage.EXECUTOR, PipelineStage.EXECUTOR_REWORK}:
        return _parse_executor(parsed_summary)
    if stage is PipelineStage.REVIEWER:
        return _parse_reviewer(parsed_summary)
    if stage is PipelineStage.CRITIC:
        return _parse_critic(parsed_summary)
    raise ContractError(f"unsupported stage: {stage.value}")


def _pr_repository(pr_url: str, *, label: str) -> str:
    if not isinstance(pr_url, str) or _GITHUB_PR_RE.fullmatch(pr_url) is None:
        raise ContractError(f"{label} has an invalid format")
    parts = pr_url.split("/")
    return f"{parts[3]}/{parts[4]}"


def validate_stage_result_binding(
    result: StageResult,
    *,
    expected_repository: str,
    expected_pr_url: str | None = None,
    expected_source_digest: str | None = None,
    expected_head_sha: str | None = None,
) -> None:
    """Validate stage evidence against its applicable transition bindings.

    Executor and executor-rework results are repository-bound because their
    contract has no source digest or reviewed HEAD. Reviewer and critic results
    additionally require exact PR, source digest, and reviewed HEAD bindings.
    """

    if (
        not isinstance(expected_repository, str)
        or _GITHUB_REPOSITORY_RE.fullmatch(expected_repository) is None
    ):
        raise ContractError("expected_repository has an invalid format")
    if _pr_repository(result.pr_url, label="pr_url") != expected_repository:
        raise ContractError("pr_url repository does not match expected_repository")

    if isinstance(result, ExecutorResult):
        if any(
            value is not None
            for value in (expected_pr_url, expected_source_digest, expected_head_sha)
        ):
            raise ContractError(
                "executor binding is repository-only; exact transition fields "
                "are inapplicable"
            )
        return

    if not isinstance(result, (ReviewerResult, CriticResult)):
        raise ContractError("unsupported stage result binding")
    if (
        expected_pr_url is None
        or expected_source_digest is None
        or expected_head_sha is None
    ):
        raise ContractError(
            "reviewer and critic bindings require expected PR, source digest, and head"
        )

    expected_pr_repository = _pr_repository(
        expected_pr_url,
        label="expected_pr_url",
    )
    if expected_pr_repository != expected_repository:
        raise ContractError("expected_pr_url repository does not match expected_repository")
    if result.pr_url != expected_pr_url:
        raise ContractError("pr_url does not match expected_pr_url")
    if _SHA256_RE.fullmatch(expected_source_digest) is None:
        raise ContractError("expected_source_digest has an invalid format")
    if result.source_digest != expected_source_digest:
        raise ContractError("source_digest does not match expected_source_digest")
    if _GIT_SHA_RE.fullmatch(expected_head_sha) is None:
        raise ContractError("expected_head_sha has an invalid format")

    reviewed_head_sha = (
        result.head_sha
        if isinstance(result, ReviewerResult)
        else result.reviewed_head_sha
    )
    if reviewed_head_sha != expected_head_sha:
        raise ContractError("reviewed head does not match expected_head_sha")
    if (
        isinstance(result, CriticResult)
        and result.result_head_sha == result.reviewed_head_sha
    ):
        raise ContractError("critic result HEAD must differ from reviewed HEAD")


def transition_digest(
    *,
    task_id: str,
    run_id: int,
    stage: PipelineStage,
    summary: Mapping[str, object],
    metadata: Mapping[str, object],
    pr_url: str,
    head_sha: str,
) -> str:
    """Hash canonical transition evidence for replay-safe child creation."""

    payload = {
        "task_id": task_id,
        "run_id": run_id,
        "stage": stage.value,
        "summary": summary,
        "metadata": metadata,
        "pr_url": pr_url,
        "head_sha": head_sha,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
