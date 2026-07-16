from __future__ import annotations

from itertools import product

import pytest

from forge.ops.task_options import (
    MergeMode,
    Mode,
    TaskFlow,
    TaskOptionError,
    TaskRole,
    TaskSelection,
    all_task_selections,
    parse_task_selection,
)


def test_public_option_values_use_plain_names() -> None:
    assert [value.value for value in Mode] == ["chat", "task"]
    assert [value.value for value in TaskFlow] == [
        "build",
        "build_review",
        "build_review_deep_check",
    ]
    assert [value.value for value in MergeMode] == [
        "manual",
        "safe_auto",
        "full_auto",
    ]
    assert [value.value for value in TaskRole] == [
        "builder",
        "reviewer",
        "deep_checker",
        "fix",
    ]


def test_all_nine_task_combinations_are_valid() -> None:
    expected = set(
        product(
            ("build", "build_review", "build_review_deep_check"),
            ("manual", "safe_auto", "full_auto"),
        )
    )

    assert {
        (value.task_flow.value, value.merge_mode.value)
        for value in all_task_selections()
    } == expected


def test_parse_task_selection_requires_exact_fields() -> None:
    parsed = parse_task_selection(
        {
            "mode": "task",
            "task_flow": "build_review",
            "merge_mode": "safe_auto",
        }
    )

    assert parsed.mode is Mode.TASK
    assert parsed.task_flow is TaskFlow.BUILD_REVIEW
    assert parsed.merge_mode is MergeMode.SAFE_AUTO


@pytest.mark.parametrize(
    "value",
    [
        {"mode": "task", "task_flow": "build"},
        {
            "mode": "task",
            "task_flow": "build",
            "merge_mode": "manual",
            "remember": True,
        },
        {
            "interaction_mode": "task",
            "assurance_policy": "direct",
            "merge_policy": "P1",
        },
        {
            "mode": "task",
            "task_flow": "direct",
            "merge_mode": "manual",
        },
    ],
)
def test_invalid_or_old_task_choices_are_rejected(value: dict[str, object]) -> None:
    with pytest.raises(TaskOptionError):
        parse_task_selection(value)


def test_chat_is_not_a_task_selection() -> None:
    with pytest.raises(TaskOptionError, match="mode must be 'task'"):
        parse_task_selection(
            {"mode": "chat", "task_flow": "build", "merge_mode": "manual"}
        )


def test_direct_task_selection_rejects_chat_mode() -> None:
    with pytest.raises(TaskOptionError, match="mode must be 'task'"):
        TaskSelection(Mode.CHAT, TaskFlow.BUILD, MergeMode.MANUAL)
