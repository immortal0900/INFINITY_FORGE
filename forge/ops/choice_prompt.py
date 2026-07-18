"""Immutable, transport-neutral values for an interactive choice prompt."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from uuid import UUID


class ChoicePromptError(ValueError):
    """Raised when chooser metadata or a submitted selection is invalid."""


class ChoiceMode(str, Enum):
    SINGLE = "single"
    MULTIPLE = "multiple"


@dataclass(frozen=True)
class Choice:
    """One stable choice ID and its presentation-only text."""

    id: str
    label: str
    description: str

    def __post_init__(self) -> None:
        for field in ("id", "label", "description"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ChoicePromptError(f"choice {field} must be a non-empty string")


@dataclass(frozen=True)
class ChoiceSubmission:
    """A structured selection; labels deliberately have no authority."""

    choice_prompt_id: str
    selected_choice_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_uuid(self.choice_prompt_id, "choice_prompt_id")
        if not isinstance(self.selected_choice_ids, tuple):
            raise ChoicePromptError("selected_choice_ids must be a tuple")
        if any(
            not isinstance(choice_id, str) or not choice_id.strip()
            for choice_id in self.selected_choice_ids
        ):
            raise ChoicePromptError(
                "selected_choice_ids must contain non-empty strings"
            )


@dataclass(frozen=True)
class ChoicePrompt:
    """An immutable prompt whose IDs, bounds, and expiry govern submission."""

    choice_prompt_id: str
    choice_mode: ChoiceMode
    min_choices: int
    max_choices: int | None
    submit_label: str
    expires_at: datetime
    choices: tuple[Choice, ...]

    def __post_init__(self) -> None:
        _require_uuid(self.choice_prompt_id, "choice_prompt_id")
        if not isinstance(self.choice_mode, ChoiceMode):
            raise ChoicePromptError("choice_mode must be a ChoiceMode")
        if not isinstance(self.min_choices, int) or isinstance(self.min_choices, bool):
            raise ChoicePromptError("min_choices must be an integer")
        if self.min_choices < 1:
            raise ChoicePromptError("min_choices must be at least 1")
        if self.max_choices is not None and (
            not isinstance(self.max_choices, int) or isinstance(self.max_choices, bool)
        ):
            raise ChoicePromptError("max_choices must be an integer or null")
        if self.max_choices is not None and self.max_choices < self.min_choices:
            raise ChoicePromptError("max_choices must not be less than min_choices")
        if self.choice_mode is ChoiceMode.SINGLE and (
            self.min_choices != 1 or self.max_choices != 1
        ):
            raise ChoicePromptError("single choice prompts require exactly 1 choice")
        if not isinstance(self.submit_label, str) or not self.submit_label.strip():
            raise ChoicePromptError("submit_label must be a non-empty string")
        if not isinstance(self.expires_at, datetime) or self.expires_at.tzinfo is None:
            raise ChoicePromptError("expires_at must be a timezone-aware datetime")
        if not isinstance(self.choices, tuple) or not self.choices:
            raise ChoicePromptError("choices must be a non-empty tuple")
        if any(not isinstance(choice, Choice) for choice in self.choices):
            raise ChoicePromptError("choices must contain Choice values")
        if self.min_choices > len(self.choices):
            raise ChoicePromptError("min_choices cannot exceed the available choices")
        if self.max_choices is not None and self.max_choices > len(self.choices):
            raise ChoicePromptError("max_choices cannot exceed the available choices")
        _require_unique((choice.id for choice in self.choices), "choice ID")
        _require_unique((choice.label for choice in self.choices), "choice label")

    def validate_submission(
        self,
        submission: ChoiceSubmission,
        now: datetime,
    ) -> tuple[str, ...]:
        """Return accepted IDs without mutating prompt state."""

        if not isinstance(submission, ChoiceSubmission):
            raise ChoicePromptError("submission must be a ChoiceSubmission")
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ChoicePromptError("now must be a timezone-aware datetime")
        if submission.choice_prompt_id != self.choice_prompt_id:
            raise ChoicePromptError("choice_prompt_id does not match the pending prompt")
        if now >= self.expires_at:
            raise ChoicePromptError("choice prompt expired")
        selected = submission.selected_choice_ids
        _require_unique(selected, "selected choice ID")
        allowed = {choice.id for choice in self.choices}
        if any(choice_id not in allowed for choice_id in selected):
            raise ChoicePromptError("selected choice ID is unknown")
        if len(selected) < self.min_choices:
            if self.choice_mode is ChoiceMode.SINGLE:
                raise ChoicePromptError("single choice prompts require exactly 1 choice")
            raise ChoicePromptError(f"multiple choice prompts require at least {self.min_choices} choices")
        if self.max_choices is not None and len(selected) > self.max_choices:
            if self.choice_mode is ChoiceMode.SINGLE:
                raise ChoicePromptError("single choice prompts require exactly 1 choice")
            raise ChoicePromptError(f"multiple choice prompts allow at most {self.max_choices} choices")
        return selected

    def metadata(self) -> dict[str, object]:
        """Serialize the complete cross-surface chooser contract."""

        return {
            "choice_prompt_id": self.choice_prompt_id,
            "choice_mode": self.choice_mode.value,
            "min_choices": self.min_choices,
            "max_choices": self.max_choices,
            "submit_label": self.submit_label,
            "expires_at": self.expires_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "choices": [
                {
                    "id": choice.id,
                    "label": choice.label,
                    "description": choice.description,
                }
                for choice in self.choices
            ],
        }


def _require_uuid(value: object, field: str) -> None:
    if not isinstance(value, str):
        raise ChoicePromptError(f"{field} must be a UUID string")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise ChoicePromptError(f"{field} must be a UUID string") from error
    if str(parsed) != value:
        raise ChoicePromptError(f"{field} must be a canonical UUID string")


def _require_unique(values: object, field: str) -> None:
    seen: set[str] = set()
    for value in values:  # type: ignore[union-attr]
        if value in seen:
            raise ChoicePromptError(f"duplicate {field}: {value}")
        seen.add(value)
