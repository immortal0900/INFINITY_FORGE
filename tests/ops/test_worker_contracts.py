"""Executable contracts for the Forge role-worker instructions."""

from __future__ import annotations

import json
import re
from pathlib import Path

from forge.ops.contracts import (
    CRITIC_RESULT_OPTIONAL_FIELDS,
    CRITIC_RESULT_REQUIRED_FIELDS,
    REVIEWER_RESULT_OPTIONAL_FIELDS,
    REVIEWER_RESULT_REQUIRED_FIELDS,
    PipelineStage,
    parse_stage_result,
)


ROOT = Path(__file__).resolve().parents[2]


def _skill(name: str) -> str:
    return (ROOT / "forge" / "skills" / name / "SKILL.md").read_text(
        encoding="utf-8"
    )


def _result_examples(skill: str, schema_version: str) -> list[dict[str, object]]:
    examples: list[dict[str, object]] = []
    for block in re.findall(r"```json\s*(\{.*?\})\s*```", skill, flags=re.DOTALL):
        payload = json.loads(block)
        if payload.get("schema_version") == schema_version:
            examples.append(payload)
    return examples


def test_reviewer_examples_match_parser_exactly() -> None:
    skill = _skill("reviewer-verdict")
    examples = _result_examples(skill, "forge-reviewer-result/v1")

    assert {example["verdict"] for example in examples} == {"approve", "reject"}
    for example in examples:
        expected = set(REVIEWER_RESULT_REQUIRED_FIELDS)
        if example["verdict"] == "reject":
            expected |= set(REVIEWER_RESULT_OPTIONAL_FIELDS)
        assert set(example) == expected
        parse_stage_result(PipelineStage.REVIEWER, example, {})


def test_reviewer_reject_is_a_completed_quality_result() -> None:
    skill = _skill("reviewer-verdict")

    assert "| `reject` | `kanban_complete` |" in skill
    assert "blocked는 인프라 장애나 protocol violation" in skill


def test_reviewer_copies_card_bindings_without_substitution() -> None:
    skill = _skill("reviewer-verdict")

    for field in ("source_digest", "pr_url", "bound_head_sha"):
        assert field in skill
    assert "예시 값을 그대로 쓰지" in skill


def test_critic_examples_match_parser_exactly() -> None:
    skill = _skill("critic-adversarial")
    examples = _result_examples(skill, "forge-critic-result/v1")

    assert {example["outcome"] for example in examples} == {
        "pass",
        "defect_found",
    }
    for example in examples:
        expected = set(CRITIC_RESULT_REQUIRED_FIELDS)
        if example["outcome"] == "defect_found":
            expected |= set(CRITIC_RESULT_OPTIONAL_FIELDS)
        assert set(example) == expected
        parse_stage_result(PipelineStage.CRITIC, example, {})


def test_critic_defect_is_a_completed_quality_result() -> None:
    skill = _skill("critic-adversarial")

    assert "| `defect_found` | `kanban_complete` |" in skill
    assert "blocked는 인프라 장애나 protocol violation" in skill


def test_critic_binds_reviewed_and_result_heads() -> None:
    skill = _skill("critic-adversarial")

    for field in (
        "source_digest",
        "pr_url",
        "bound_head_sha",
        "reviewed_head_sha",
        "result_head_sha",
    ):
        assert field in skill
    assert "예시 값을 그대로 쓰지" in skill


def test_rework_executor_injects_parent_reflection_into_codex_instruction() -> None:
    skill = _skill("kanban-codex-delegate")

    assert "executor-rework" in skill
    assert re.search(
        r"reflection.*codex.*지시문.*반드시 포함",
        skill,
        flags=re.IGNORECASE | re.DOTALL,
    )
