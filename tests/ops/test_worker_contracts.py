"""Executable contracts for the Forge role-worker instructions."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

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


def _json_examples(skill: str) -> list[dict[str, object]]:
    return [
        json.loads(block)
        for block in re.findall(r"```json\s*(\{.*?\})\s*```", skill, flags=re.DOTALL)
    ]


def test_executor_example_matches_parser_exactly() -> None:
    skill = _skill("kanban-codex-delegate")
    expected = {
        "pr_url",
        "changed_files",
        "implemented",
        "not_implemented",
        "verified_by",
    }
    examples = [value for value in _json_examples(skill) if set(value) == expected]

    assert len(examples) == 1
    parse_stage_result(PipelineStage.EXECUTOR, examples[0], {})
    assert "PR URL 또는 null" not in skill
    assert "JSON 앞뒤 산문" in skill


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


def test_fresh_reviewer_reads_ancestral_executor_handoff_and_critic_tests() -> None:
    skill = _skill("reviewer-verdict")

    assert "가장 가까운 executor" in skill
    assert "exact 5-field" in skill
    assert "가장 가까운 critic 조상" in skill
    assert "executor-rework 과정" in skill
    assert "added_tests" in skill
    assert "현재 HEAD에 모두 남아" in skill


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


def test_ci_failure_rework_uses_exact_failed_head_and_same_pr() -> None:
    skill = _skill("kanban-codex-delegate")

    assert "required check" in skill
    assert "CI failure" in skill
    assert "bound_head_sha" in skill
    assert "같은 PR" in skill
    assert "의미 없는 commit" in skill


@pytest.mark.parametrize(
    "skill_name",
    ["critic-adversarial", "kanban-codex-delegate"],
)
def test_mutating_stage_uses_bound_head_task_worktree(skill_name: str) -> None:
    skill = _skill(skill_name)

    for required in (
        "headRefName",
        "headRefOid",
        "worktree add",
        "status --porcelain",
        "merge-base --is-ancestor",
        "result_head_sha",
    ):
        assert required in skill
    assert "HEAD:<PR_HEAD_BRANCH>" in skill


@pytest.mark.parametrize(
    "skill_name",
    ["critic-adversarial", "kanban-codex-delegate"],
)
def test_mutating_stage_can_resume_only_its_verified_card_worktree(
    skill_name: str,
) -> None:
    skill = _skill(skill_name)

    for required in (
        "rev-parse --is-inside-work-tree",
        'test "$PR_HEAD_SHA" = "$BOUND_HEAD_SHA" || test "$PR_HEAD_SHA" = "$LOCAL_HEAD"',
        "재개",
    ):
        assert required in skill


@pytest.mark.parametrize(
    "skill_name, workspace",
    [
        ("critic-adversarial", "$TASK_WORKTREE"),
        ("kanban-codex-delegate", "$WORKSPACE"),
    ],
)
def test_mutating_stage_requires_a_new_pushed_commit(
    skill_name: str, workspace: str
) -> None:
    skill = _skill(skill_name)

    assert 'test "$LOCAL_HEAD" != "$BOUND_HEAD_SHA"' in skill
    assert 'test "$LOCAL_HEAD" = "$LIVE_HEAD"' in skill
    assert f'git -C "{workspace}" merge-base --is-ancestor' in skill


def test_executor_gate_runs_against_the_selected_workspace() -> None:
    skill = _skill("kanban-codex-delegate")

    assert 'codex-stop-gate.sh "$WORKSPACE"' in skill
    assert "codex-stop-gate.sh <워크스페이스>" not in skill


def test_executor_passes_prompt_file_through_stdin_without_shell_reparse() -> None:
    skill = _skill("kanban-codex-delegate")

    assert "codex exec --skip-git-repo-check -" in skill
    assert "printf -v TMUX_COMMAND" in skill
    assert "< %q > %q 2>&1" in skill
    assert '"$PROMPT_FILE" "$LOG_FILE"' in skill
    assert 'LOG_FILE="/home/ubuntu/.hermes/kanban/logs/' in skill
    assert 'codex exec --skip-git-repo-check "<지시문>"' not in skill
    assert "지시문 본문을 tmux 명령 문자열에 삽입하지 않는다" in skill


def test_critic_proves_every_reported_test_was_added_after_bound_head() -> None:
    skill = _skill("critic-adversarial")

    assert 'diff --name-only "$BOUND_HEAD_SHA" "$LOCAL_HEAD"' in skill
    assert "added_tests 배열의 각 경로" in skill
    assert 'grep -Fx -- "$path"' in skill


def test_rework_records_base_only_after_bound_checkout() -> None:
    skill = _skill("kanban-codex-delegate")

    checkout = skill.index("worktree add")
    clean_check = skill.index("status --porcelain", checkout)
    base_record = skill.index(".forge-base-sha", clean_check)
    assert checkout < clean_check < base_record
