"""Adversarial contracts for SPEC-003 Korean PR guidance."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs" / "user-runbook.md"
SPEC_REGISTRY = ROOT / "forge" / "spec-registry.md"


def test_spec_003_registry_entry_is_exactly_once_and_not_reused() -> None:
    registry = SPEC_REGISTRY.read_text(encoding="utf-8")

    assert registry.count("SPEC-003 | PR 제목과 본문 한국어 작성 원칙") == 1
    assert re.findall(r"(?m)^SPEC-003\s*\|", registry) == ["SPEC-003 |"]
    assert "SPEC-004 | PR 제목과 본문 한국어 작성 원칙" not in registry


def test_runbook_requires_both_pr_title_and_body_to_be_korean() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")

    guideline = re.search(r"(?m)^자동 워커나 사람이 생성하는 PR 제목과 본문은 .+$", runbook)
    assert guideline is not None
    line = guideline.group(0)
    assert "제목과 본문" in line
    assert "한국어" in line
    assert "기본적으로" in line
    assert "이슈" not in line


def test_korean_pr_guideline_is_normative_text_not_a_fenced_example() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")

    guideline = re.search(r"(?m)^자동 워커나 사람이 생성하는 PR 제목과 본문은 .+$", runbook)
    assert guideline is not None
    preceding_text = runbook[: guideline.start()]
    assert preceding_text.count("```") % 2 == 0


def test_original_notation_exception_is_limited_to_tokens_not_whole_prs() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")

    guideline = re.search(r"(?m)^자동 워커나 사람이 생성하는 PR 제목과 본문은 .+$", runbook)
    assert guideline is not None
    line = guideline.group(0)
    for allowed_term in ("코드 식별자", "명령어", "로그", "고유 제품명"):
        assert allowed_term in line
    assert "원문 표기" in line
    assert "유지할 수 있다" in line
    assert "영어" not in line
    assert "본문 전체" not in line


def test_github_submission_step_keeps_label_gate_after_language_rule() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")

    label_gate = runbook.index("라벨을 붙이는 8단계가 실제 작업 투입 승인이다")
    guideline = runbook.index("자동 워커나 사람이 생성하는 PR 제목과 본문은")
    next_section = runbook.index("### 6.2 좋은 수용 기준 예시")
    assert label_gate < guideline < next_section
