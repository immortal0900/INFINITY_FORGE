import json
from dataclasses import fields
from pathlib import Path

import pytest

from forge.ops.contracts import (
    CheckRun,
    ContractError,
    CRITIC_RESULT_OPTIONAL_FIELDS,
    CRITIC_RESULT_REQUIRED_FIELDS,
    CriticResult,
    ExecutorResult,
    PipelineStage,
    PullRequestSnapshot,
    REVIEWER_RESULT_OPTIONAL_FIELDS,
    REVIEWER_RESULT_REQUIRED_FIELDS,
    ReviewerResult,
    RunRecord,
    StageOutcome,
    TaskRecord,
    parse_stage_result,
    transition_digest,
    validate_stage_result_binding,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
PR_URL = "https://github.com/owner/repo/pull/7"
SOURCE_DIGEST = "a" * 64
HEAD_SHA = "b" * 40
RESULT_HEAD_SHA = "c" * 40


def executor_summary(**overrides: object) -> dict[str, object]:
    summary: dict[str, object] = {
        "pr_url": PR_URL,
        "changed_files": ["forge/ops/contracts.py"],
        "implemented": ["AC1"],
        "not_implemented": [],
        "verified_by": {"AC1": "tests/ops/test_contracts.py"},
    }
    summary.update(overrides)
    return summary


def reviewer_summary(**overrides: object) -> dict[str, object]:
    summary: dict[str, object] = {
        "schema_version": "forge-reviewer-result/v1",
        "verdict": "approve",
        "source_digest": SOURCE_DIGEST,
        "pr_url": PR_URL,
        "head_sha": HEAD_SHA,
        "delta_check": {
            "implemented_verified": ["AC1"],
            "discrepancies": [],
        },
        "spec_check": {"met": ["AC1"], "unmet": []},
    }
    summary.update(overrides)
    return summary


def critic_summary(**overrides: object) -> dict[str, object]:
    summary: dict[str, object] = {
        "schema_version": "forge-critic-result/v1",
        "outcome": "pass",
        "source_digest": SOURCE_DIGEST,
        "pr_url": PR_URL,
        "reviewed_head_sha": HEAD_SHA,
        "result_head_sha": RESULT_HEAD_SHA,
        "added_tests": ["tests/test_edge_case.py"],
        "scenarios": ["empty input", "retry", "contract mismatch"],
    }
    summary.update(overrides)
    return summary


def test_public_record_types_are_dataclasses() -> None:
    assert {field.name for field in fields(TaskRecord)} >= {"task_id", "status"}
    assert {field.name for field in fields(RunRecord)} >= {
        "run_id",
        "task_id",
        "summary",
        "metadata",
    }
    assert {field.name for field in fields(PullRequestSnapshot)} >= {
        "pr_url",
        "head_sha",
        "checks",
    }
    assert {field.name for field in fields(CheckRun)} == {
        "name",
        "status",
        "conclusion",
        "head_sha",
    }


def test_pipeline_stage_and_outcome_values_are_stable() -> None:
    assert [stage.value for stage in PipelineStage] == [
        "executor",
        "reviewer",
        "critic",
        "executor-rework",
    ]
    assert {outcome.value for outcome in StageOutcome} == {
        "approve",
        "reject",
        "pass",
        "defect_found",
    }


def test_executor_result_requires_strict_handoff_fields() -> None:
    result = parse_stage_result(
        PipelineStage.EXECUTOR,
        {
            "pr_url": PR_URL,
            "changed_files": ["forge/ops/contracts.py"],
            "implemented": ["AC1"],
            "not_implemented": [],
            "verified_by": {"AC1": "tests/ops/test_contracts.py"},
        },
        {},
    )

    assert isinstance(result, ExecutorResult)
    assert result.implemented == ("AC1",)


def test_executor_rework_uses_the_executor_contract() -> None:
    with pytest.raises(ContractError, match="implemented"):
        parse_stage_result(
            PipelineStage.EXECUTOR_REWORK,
            {
                "pr_url": PR_URL,
                "changed_files": [],
                "implemented": [],
                "not_implemented": [],
                "verified_by": {"AC1": "tests/ops/test_contracts.py"},
            },
            {},
        )


def test_reviewer_reject_requires_reflection() -> None:
    with pytest.raises(ContractError, match="reflection"):
        parse_stage_result(
            PipelineStage.REVIEWER,
            {"schema_version": "forge-reviewer-result/v1", "verdict": "reject"},
            {},
        )


def test_reviewer_result_is_bound_to_source_pr_and_head() -> None:
    result = parse_stage_result(PipelineStage.REVIEWER, reviewer_summary(), {})

    assert isinstance(result, ReviewerResult)
    assert result.verdict is StageOutcome.APPROVE
    assert result.source_digest == SOURCE_DIGEST
    assert result.pr_url == PR_URL
    assert result.head_sha == HEAD_SHA
    assert result.reflection is None


def test_reviewer_reject_accepts_non_empty_reflection() -> None:
    result = parse_stage_result(
        PipelineStage.REVIEWER,
        reviewer_summary(verdict="reject", reflection="AC2 implementation missing"),
        {},
    )

    assert isinstance(result, ReviewerResult)
    assert result.verdict is StageOutcome.REJECT
    assert result.reflection == "AC2 implementation missing"


def test_reviewer_rejects_unbound_source_digest() -> None:
    summary = reviewer_summary()
    del summary["source_digest"]

    with pytest.raises(ContractError, match="source_digest"):
        parse_stage_result(PipelineStage.REVIEWER, summary, {})


def test_stage_result_rejects_unexpected_fields() -> None:
    with pytest.raises(ContractError, match="unexpected fields"):
        parse_stage_result(
            PipelineStage.REVIEWER,
            reviewer_summary(undeclared=True),
            {},
        )


def test_critic_pass_requires_added_tests_and_result_head() -> None:
    with pytest.raises(ContractError, match="added_tests"):
        parse_stage_result(
            PipelineStage.CRITIC,
            {"schema_version": "forge-critic-result/v1", "outcome": "pass"},
            {},
        )


def test_critic_defect_requires_reflection() -> None:
    with pytest.raises(ContractError, match="reflection"):
        parse_stage_result(
            PipelineStage.CRITIC,
            critic_summary(outcome="defect_found"),
            {},
        )


def test_critic_result_binds_reviewed_and_result_heads() -> None:
    result = parse_stage_result(PipelineStage.CRITIC, critic_summary(), {})

    assert isinstance(result, CriticResult)
    assert result.outcome is StageOutcome.PASS
    assert result.reviewed_head_sha == HEAD_SHA
    assert result.result_head_sha == RESULT_HEAD_SHA
    assert result.added_tests == ("tests/test_edge_case.py",)


def test_stage_schema_version_must_match_stage() -> None:
    with pytest.raises(ContractError, match="schema_version"):
        parse_stage_result(
            PipelineStage.REVIEWER,
            reviewer_summary(schema_version="forge-critic-result/v1"),
            {},
        )


@pytest.mark.parametrize(
    "stage",
    [PipelineStage.EXECUTOR, PipelineStage.EXECUTOR_REWORK],
)
def test_executor_binding_rejects_cross_repository_pr(stage: PipelineStage) -> None:
    result = parse_stage_result(
        stage,
        executor_summary(pr_url="https://github.com/attacker/repo/pull/7"),
        {},
    )

    with pytest.raises(ContractError, match="repository"):
        validate_stage_result_binding(
            result,
            expected_repository="owner/repo",
        )


@pytest.mark.parametrize(
    "stage",
    [PipelineStage.EXECUTOR, PipelineStage.EXECUTOR_REWORK],
)
def test_executor_binding_accepts_same_repository_without_exact_transition_fields(
    stage: PipelineStage,
) -> None:
    result = parse_stage_result(stage, executor_summary(), {})

    validate_stage_result_binding(
        result,
        expected_repository="owner/repo",
    )


def test_executor_binding_rejects_inapplicable_exact_transition_fields() -> None:
    result = parse_stage_result(PipelineStage.EXECUTOR, executor_summary(), {})

    with pytest.raises(ContractError, match="repository-only"):
        validate_stage_result_binding(
            result,
            expected_repository="owner/repo",
            expected_pr_url=PR_URL,
            expected_source_digest=SOURCE_DIGEST,
            expected_head_sha=HEAD_SHA,
        )


@pytest.mark.parametrize(
    ("stage", "summary"),
    [
        (
            PipelineStage.REVIEWER,
            reviewer_summary(pr_url="https://github.com/attacker/repo/pull/7"),
        ),
        (
            PipelineStage.CRITIC,
            critic_summary(pr_url="https://github.com/attacker/repo/pull/7"),
        ),
    ],
)
def test_stage_binding_rejects_cross_repository_pr(
    stage: PipelineStage,
    summary: dict[str, object],
) -> None:
    result = parse_stage_result(stage, summary, {})

    with pytest.raises(ContractError, match="repository"):
        validate_stage_result_binding(
            result,
            expected_repository="owner/repo",
            expected_pr_url=PR_URL,
            expected_source_digest=SOURCE_DIGEST,
            expected_head_sha=HEAD_SHA,
        )


@pytest.mark.parametrize(
    ("stage", "summary"),
    [
        (
            PipelineStage.REVIEWER,
            reviewer_summary(pr_url="https://github.com/owner/repo/pull/8"),
        ),
        (
            PipelineStage.CRITIC,
            critic_summary(pr_url="https://github.com/owner/repo/pull/8"),
        ),
    ],
)
def test_stage_binding_rejects_different_pr(
    stage: PipelineStage,
    summary: dict[str, object],
) -> None:
    result = parse_stage_result(stage, summary, {})

    with pytest.raises(ContractError, match="pr_url"):
        validate_stage_result_binding(
            result,
            expected_repository="owner/repo",
            expected_pr_url=PR_URL,
            expected_source_digest=SOURCE_DIGEST,
            expected_head_sha=HEAD_SHA,
        )


@pytest.mark.parametrize(
    ("stage", "summary"),
    [
        (
            PipelineStage.REVIEWER,
            reviewer_summary(source_digest="d" * 64),
        ),
        (
            PipelineStage.CRITIC,
            critic_summary(source_digest="d" * 64),
        ),
    ],
)
def test_stage_binding_rejects_different_source_digest(
    stage: PipelineStage,
    summary: dict[str, object],
) -> None:
    result = parse_stage_result(stage, summary, {})

    with pytest.raises(ContractError, match="source_digest"):
        validate_stage_result_binding(
            result,
            expected_repository="owner/repo",
            expected_pr_url=PR_URL,
            expected_source_digest=SOURCE_DIGEST,
            expected_head_sha=HEAD_SHA,
        )


@pytest.mark.parametrize(
    ("stage", "summary"),
    [
        (
            PipelineStage.REVIEWER,
            reviewer_summary(head_sha="d" * 40),
        ),
        (
            PipelineStage.CRITIC,
            critic_summary(reviewed_head_sha="d" * 40),
        ),
    ],
)
def test_stage_binding_rejects_stale_reviewed_head(
    stage: PipelineStage,
    summary: dict[str, object],
) -> None:
    result = parse_stage_result(stage, summary, {})

    with pytest.raises(ContractError, match="head"):
        validate_stage_result_binding(
            result,
            expected_repository="owner/repo",
            expected_pr_url=PR_URL,
            expected_source_digest=SOURCE_DIGEST,
            expected_head_sha=HEAD_SHA,
        )


@pytest.mark.parametrize(
    ("stage", "summary"),
    [
        (PipelineStage.REVIEWER, reviewer_summary()),
        (PipelineStage.CRITIC, critic_summary()),
    ],
)
def test_stage_binding_accepts_exact_source_pr_and_reviewed_head(
    stage: PipelineStage,
    summary: dict[str, object],
) -> None:
    result = parse_stage_result(stage, summary, {})

    validate_stage_result_binding(
        result,
        expected_repository="owner/repo",
        expected_pr_url=PR_URL,
        expected_source_digest=SOURCE_DIGEST,
        expected_head_sha=HEAD_SHA,
    )


@pytest.mark.parametrize(
    ("schema_name", "required_fields", "optional_fields"),
    [
        (
            "reviewer-result-v1.schema.json",
            REVIEWER_RESULT_REQUIRED_FIELDS,
            REVIEWER_RESULT_OPTIONAL_FIELDS,
        ),
        (
            "critic-result-v1.schema.json",
            CRITIC_RESULT_REQUIRED_FIELDS,
            CRITIC_RESULT_OPTIONAL_FIELDS,
        ),
    ],
)
def test_json_schema_field_sets_match_parser_field_sets(
    schema_name: str,
    required_fields: frozenset[str],
    optional_fields: frozenset[str],
) -> None:
    schema = json.loads(
        (REPO_ROOT / "forge" / "schemas" / schema_name).read_text(encoding="utf-8")
    )

    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == required_fields | optional_fields
    assert set(schema["required"]) == required_fields


@pytest.mark.parametrize(
    ("schema_name", "string_paths"),
    [
        (
            "reviewer-result-v1.schema.json",
            [
                ("delta_check", "implemented_verified", "items"),
                ("delta_check", "discrepancies", "items"),
                ("spec_check", "met", "items"),
                ("spec_check", "unmet", "items"),
                ("reflection",),
            ],
        ),
        (
            "critic-result-v1.schema.json",
            [
                ("added_tests", "items"),
                ("scenarios", "items"),
                ("reflection",),
            ],
        ),
    ],
)
def test_json_schema_rejects_whitespace_only_strings_where_parser_does(
    schema_name: str,
    string_paths: list[tuple[str, ...]],
) -> None:
    schema = json.loads(
        (REPO_ROOT / "forge" / "schemas" / schema_name).read_text(encoding="utf-8")
    )

    for path in string_paths:
        node = schema["properties"]
        for segment in path:
            node = node[segment]
            if segment not in {"items"} and "properties" in node:
                node = node["properties"]
        assert node.get("pattern") == r"\S", ".".join(path)


def test_transition_digest_is_canonical_and_evidence_bound() -> None:
    first = transition_digest(
        task_id="t_123",
        run_id=12,
        stage=PipelineStage.REVIEWER,
        summary={"verdict": "approve", "nested": {"b": 2, "a": 1}},
        metadata={"worker": "codex", "attempt": 1},
        pr_url=PR_URL,
        head_sha=HEAD_SHA,
    )
    reordered = transition_digest(
        task_id="t_123",
        run_id=12,
        stage=PipelineStage.REVIEWER,
        summary={"nested": {"a": 1, "b": 2}, "verdict": "approve"},
        metadata={"attempt": 1, "worker": "codex"},
        pr_url=PR_URL,
        head_sha=HEAD_SHA,
    )
    changed = transition_digest(
        task_id="t_123",
        run_id=12,
        stage=PipelineStage.REVIEWER,
        summary={"verdict": "approve", "nested": {"a": 1, "b": 2}},
        metadata={"attempt": 2, "worker": "codex"},
        pr_url=PR_URL,
        head_sha=HEAD_SHA,
    )

    assert first == reordered
    assert first == "c22bbb1b8df23be8f23f508f456b53b3a71122cf46e6a5ca729698ef18a04139"
    assert first != changed
