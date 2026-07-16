"""Executable JSON examples for the clean-break Task worker skills."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from forge.ops.contracts import (
    BuildResult,
    DeepCheckResult,
    ReviewResult,
    StepProof,
    parse_build_result,
    parse_deep_check_result,
    parse_review_result,
    parse_step_proof,
)


ROOT = Path(__file__).resolve().parents[2]


def _only_json_example(skill_name: str) -> Mapping[str, object]:
    text = (ROOT / "forge" / "skills" / skill_name / "SKILL.md").read_text(
        encoding="utf-8"
    )
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    assert len(blocks) == 1
    payload = json.loads(blocks[0])
    assert isinstance(payload, dict)
    return payload


@pytest.mark.parametrize(
    ("skill_name", "parser", "result_type"),
    [
        ("build-task", parse_build_result, BuildResult),
        ("review-task", parse_review_result, ReviewResult),
        ("deep-check", parse_deep_check_result, DeepCheckResult),
        ("fix-task", parse_step_proof, StepProof),
    ],
)
def test_worker_skill_json_example_matches_live_parser(
    skill_name: str,
    parser: Callable[[Mapping[str, object]], object],
    result_type: type[object],
) -> None:
    assert isinstance(parser(_only_json_example(skill_name)), result_type)
