from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from forge.ops.choice_prompt import (
    Choice,
    ChoiceMode,
    ChoicePrompt,
    ChoicePromptError,
    ChoiceSubmission,
)


NOW = datetime(2026, 7, 18, 9, 0, tzinfo=UTC)


def _prompt(*, mode: ChoiceMode = ChoiceMode.SINGLE, max_choices: int | None = 1) -> ChoicePrompt:
    return ChoicePrompt(
        choice_prompt_id=str(uuid4()),
        choice_mode=mode,
        min_choices=1,
        max_choices=max_choices,
        submit_label="Done",
        expires_at=NOW + timedelta(minutes=30),
        choices=(
            Choice("chat", "Chat", "Continue the conversation"),
            Choice("task", "Task", "Start implementation"),
        ),
    )


def test_single_prompt_requires_exactly_one_known_unique_choice() -> None:
    prompt = _prompt()

    selected = prompt.validate_submission(
        ChoiceSubmission(prompt.choice_prompt_id, ("task",)), NOW
    )

    assert selected == ("task",)
    with pytest.raises(ChoicePromptError, match="exactly 1"):
        prompt.validate_submission(ChoiceSubmission(prompt.choice_prompt_id, ()), NOW)
    with pytest.raises(ChoicePromptError, match="unknown"):
        prompt.validate_submission(
            ChoiceSubmission(prompt.choice_prompt_id, ("missing",)), NOW
        )
    with pytest.raises(ChoicePromptError, match="duplicate"):
        prompt.validate_submission(
            ChoiceSubmission(prompt.choice_prompt_id, ("task", "task")), NOW
        )


def test_multiple_prompt_requires_at_least_its_minimum_and_allows_no_maximum() -> None:
    prompt = _prompt(mode=ChoiceMode.MULTIPLE, max_choices=None)

    assert prompt.validate_submission(
        ChoiceSubmission(prompt.choice_prompt_id, ("chat", "task")), NOW
    ) == ("chat", "task")
    with pytest.raises(ChoicePromptError, match="at least 1"):
        prompt.validate_submission(ChoiceSubmission(prompt.choice_prompt_id, ()), NOW)


def test_stale_or_expired_submission_is_rejected() -> None:
    prompt = _prompt()

    with pytest.raises(ChoicePromptError, match="does not match"):
        prompt.validate_submission(ChoiceSubmission(str(uuid4()), ("task",)), NOW)
    with pytest.raises(ChoicePromptError, match="expired"):
        prompt.validate_submission(
            ChoiceSubmission(prompt.choice_prompt_id, ("task",)),
            NOW + timedelta(minutes=30),
        )


def test_choice_prompt_rejects_duplicate_ids_and_labels() -> None:
    common = dict(
        choice_prompt_id=str(uuid4()),
        choice_mode=ChoiceMode.MULTIPLE,
        min_choices=1,
        max_choices=None,
        submit_label="Done",
        expires_at=NOW + timedelta(minutes=30),
    )

    with pytest.raises(ChoicePromptError, match="duplicate choice ID"):
        ChoicePrompt(
            **common,
            choices=(Choice("task", "Task", "A"), Choice("task", "Other", "B")),
        )
    with pytest.raises(ChoicePromptError, match="duplicate choice label"):
        ChoicePrompt(
            **common,
            choices=(Choice("task", "Task", "A"), Choice("chat", "Task", "B")),
        )
