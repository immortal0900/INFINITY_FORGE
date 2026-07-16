from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

import forge.ops as forge_ops
import forge.ops.contracts as contracts
from forge.ops.contracts import (
    BUILD_RESULT_REQUIRED_FIELDS,
    DEEP_CHECK_RESULT_REQUIRED_FIELDS,
    REVIEW_RESULT_REQUIRED_FIELDS,
    STEP_PROOF_REQUIRED_FIELDS,
    BuildResult,
    ContractError,
    DeepCheckDecision,
    DeepCheckResult,
    ReviewDecision,
    ReviewResult,
    StepProof,
    parse_build_result,
    parse_deep_check_result,
    parse_review_result,
    parse_step_proof,
    parse_task_result,
    source_result_hash,
    task_result_payload,
    validate_task_result_binding,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SETTINGS_HASH = "a" * 64
SOURCE_HASH = "b" * 64
PR_URL = "https://github.com/owner/repo/pull/17"
BUILT_COMMIT = "c" * 40
TESTED_COMMIT = "d" * 40


def build_summary(**overrides: object) -> dict[str, object]:
    summary: dict[str, object] = {
        "format_version": "forge-build-result/v1",
        "task_settings_hash": SETTINGS_HASH,
        "pr_url": PR_URL,
        "built_commit": BUILT_COMMIT,
        "changed_files": ["forge/ops/task_flow.py"],
        "completed_items": ["AC1"],
        "remaining_items": [],
        "checks_by_item": {"AC1": "tests/ops/test_task_flow.py::test_ac1"},
    }
    summary.update(overrides)
    return summary


def review_summary(**overrides: object) -> dict[str, object]:
    summary: dict[str, object] = {
        "format_version": "forge-review-result/v1",
        "task_settings_hash": SETTINGS_HASH,
        "result": "approve",
        "source_result_hash": SOURCE_HASH,
        "pr_url": PR_URL,
        "reviewed_commit": BUILT_COMMIT,
        "change_check": {"confirmed_work": ["AC1"], "problems": []},
        "requirements_check": {"completed": ["AC1"], "missing": []},
        "fix_notes": None,
    }
    summary.update(overrides)
    return summary


def deep_check_summary(**overrides: object) -> dict[str, object]:
    summary: dict[str, object] = {
        "format_version": "forge-deep-check-result/v1",
        "task_settings_hash": SETTINGS_HASH,
        "result": "pass",
        "source_result_hash": SOURCE_HASH,
        "pr_url": PR_URL,
        "reviewed_commit": BUILT_COMMIT,
        "tested_commit": TESTED_COMMIT,
        "added_tests": ["tests/ops/test_task_flow.py"],
        "tested_cases": ["empty input"],
        "fix_notes": None,
    }
    summary.update(overrides)
    return summary


def step_proof_summary(**overrides: object) -> dict[str, object]:
    summary: dict[str, object] = {
        "format_version": "forge-step-proof/v1",
        "tested_commit": TESTED_COMMIT,
        "pr_url": PR_URL,
        "fix_notes": None,
        "source_result_hash": SOURCE_HASH,
        "source_run_id": 12,
        "source_task_id": "t_build_12",
        "task_settings_hash": SETTINGS_HASH,
    }
    summary.update(overrides)
    return summary


def test_parsers_accept_only_the_new_exact_result_shapes() -> None:
    build = parse_build_result(build_summary())
    review = parse_review_result(review_summary())
    deep_check = parse_deep_check_result(deep_check_summary())
    proof = parse_step_proof(step_proof_summary())

    assert isinstance(build, BuildResult)
    assert build.built_commit == BUILT_COMMIT
    assert isinstance(review, ReviewResult)
    assert review.result is ReviewDecision.APPROVE
    assert isinstance(deep_check, DeepCheckResult)
    assert deep_check.result is DeepCheckDecision.PASS
    assert isinstance(proof, StepProof)
    assert proof.source_run_id == 12


def test_old_stage_symbols_are_not_part_of_any_public_export() -> None:
    old_symbols = {
        "PipelineStage",
        "StageOutcome",
        "StageResult",
        "ExecutorResult",
        "ReviewerResult",
        "CriticResult",
        "parse_stage_result",
        "validate_stage_result_binding",
    }

    assert old_symbols.isdisjoint(contracts.__all__)
    assert old_symbols.isdisjoint(forge_ops.__all__)


@pytest.mark.parametrize(
    ("parser", "summary"),
    [
        (parse_build_result, build_summary(extra="old")),
        (parse_review_result, review_summary(verdict="approve")),
        (parse_deep_check_result, deep_check_summary(outcome="pass")),
        (parse_step_proof, step_proof_summary(receipt="old")),
    ],
)
def test_parsers_reject_extra_or_old_fields(parser: object, summary: object) -> None:
    with pytest.raises(ContractError, match="unexpected fields"):
        parser(summary)  # type: ignore[operator]


@pytest.mark.parametrize(
    ("parser", "summary", "field"),
    [
        (parse_build_result, build_summary(), "built_commit"),
        (parse_review_result, review_summary(), "reviewed_commit"),
        (parse_deep_check_result, deep_check_summary(), "tested_commit"),
        (parse_step_proof, step_proof_summary(), "source_run_id"),
    ],
)
def test_parsers_reject_missing_fields(
    parser: object, summary: dict[str, object], field: str
) -> None:
    del summary[field]

    with pytest.raises(ContractError, match="missing required field"):
        parser(summary)  # type: ignore[operator]


@pytest.mark.parametrize("old_step", ["executor", "reviewer", "critic", "stage"])
def test_task_result_dispatch_rejects_old_step_names(old_step: str) -> None:
    with pytest.raises(ContractError, match="step must be"):
        parse_task_result(old_step, build_summary())


def test_task_result_dispatches_only_new_result_names() -> None:
    assert isinstance(parse_task_result("build", build_summary()), BuildResult)
    assert isinstance(parse_task_result("review", review_summary()), ReviewResult)
    assert isinstance(
        parse_task_result("deep_check", deep_check_summary()), DeepCheckResult
    )

    with pytest.raises(ContractError, match="step must be"):
        parse_task_result("fix", step_proof_summary())


@pytest.mark.parametrize(
    ("parser", "summary"),
    [
        (
            parse_review_result,
            review_summary(result="changes_needed", fix_notes=None),
        ),
        (
            parse_review_result,
            review_summary(result="approve", fix_notes="change it"),
        ),
        (
            parse_deep_check_result,
            deep_check_summary(result="problems_found", fix_notes="  "),
        ),
        (
            parse_deep_check_result,
            deep_check_summary(result="pass", fix_notes="change it"),
        ),
    ],
)
def test_fix_notes_must_match_the_result(parser: object, summary: object) -> None:
    with pytest.raises(ContractError, match="fix_notes"):
        parser(summary)  # type: ignore[operator]


@pytest.mark.parametrize("source_run_id", [0, -1, True, "12"])
def test_step_proof_requires_a_positive_integer_run_id(source_run_id: object) -> None:
    with pytest.raises(ContractError, match="source_run_id"):
        parse_step_proof(step_proof_summary(source_run_id=source_run_id))


def test_source_result_hash_is_canonical_and_result_bound() -> None:
    parsed = parse_review_result(review_summary())
    first = source_result_hash(parsed)
    payload = task_result_payload(parsed)
    reordered = dict(reversed(list(payload.items())))

    assert source_result_hash(parse_review_result(reordered)) == first
    assert len(first) == 64
    changed = deepcopy(payload)
    changed["result"] = "changes_needed"
    changed["fix_notes"] = "missing AC2"
    assert source_result_hash(parse_review_result(changed)) != first


@pytest.mark.parametrize(
    ("result", "current_commit"),
    [
        (parse_build_result(build_summary()), BUILT_COMMIT),
        (parse_review_result(review_summary()), BUILT_COMMIT),
        (parse_deep_check_result(deep_check_summary()), TESTED_COMMIT),
    ],
)
def test_result_binding_accepts_exact_settings_pr_and_current_commit(
    result: BuildResult | ReviewResult | DeepCheckResult,
    current_commit: str,
) -> None:
    validate_task_result_binding(
        result,
        expected_task_settings_hash=SETTINGS_HASH,
        expected_pr_url=PR_URL,
        current_commit=current_commit,
    )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("expected_task_settings_hash", "e" * 64, "task_settings_hash"),
        (
            "expected_pr_url",
            "https://github.com/owner/repo/pull/18",
            "pr_url",
        ),
        ("current_commit", "e" * 40, "current commit"),
    ],
)
def test_result_binding_rejects_any_binding_mismatch(
    field: str, value: str, match: str
) -> None:
    arguments = {
        "expected_task_settings_hash": SETTINGS_HASH,
        "expected_pr_url": PR_URL,
        "current_commit": BUILT_COMMIT,
    }
    arguments[field] = value

    with pytest.raises(ContractError, match=match):
        validate_task_result_binding(
            parse_build_result(build_summary()),
            **arguments,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("schema_name", "required_fields"),
    [
        ("build-result-v1.schema.json", BUILD_RESULT_REQUIRED_FIELDS),
        ("review-result-v1.schema.json", REVIEW_RESULT_REQUIRED_FIELDS),
        ("deep-check-result-v1.schema.json", DEEP_CHECK_RESULT_REQUIRED_FIELDS),
        ("step-proof-v1.schema.json", STEP_PROOF_REQUIRED_FIELDS),
    ],
)
def test_json_schema_field_sets_match_parser_field_sets(
    schema_name: str, required_fields: frozenset[str]
) -> None:
    schema = json.loads(
        (REPO_ROOT / "forge" / "schemas" / schema_name).read_text(encoding="utf-8")
    )

    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == required_fields
    assert set(schema["required"]) == required_fields
