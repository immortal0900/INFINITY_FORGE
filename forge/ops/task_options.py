"""Plain, strict choices for one Infinity Forge Task."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TypeVar


class TaskOptionError(ValueError):
    """Raised when a Task choice is missing, unknown, or uses an old name."""


class Mode(str, Enum):
    CHAT = "chat"
    TASK = "task"


class TaskFlow(str, Enum):
    BUILD = "build"
    BUILD_REVIEW = "build_review"
    BUILD_REVIEW_DEEP_CHECK = "build_review_deep_check"


class MergeMode(str, Enum):
    MANUAL = "manual"
    SAFE_AUTO = "safe_auto"
    FULL_AUTO = "full_auto"


class TaskRole(str, Enum):
    BUILDER = "builder"
    REVIEWER = "reviewer"
    DEEP_CHECKER = "deep_checker"
    FIX = "fix"


@dataclass(frozen=True)
class TaskSelection:
    mode: Mode
    task_flow: TaskFlow
    merge_mode: MergeMode

    def __post_init__(self) -> None:
        if self.mode is not Mode.TASK:
            raise TaskOptionError("mode must be 'task' for a task selection")
        if not isinstance(self.task_flow, TaskFlow):
            raise TaskOptionError("task_flow must be a TaskFlow")
        if not isinstance(self.merge_mode, MergeMode):
            raise TaskOptionError("merge_mode must be a MergeMode")


_TASK_SELECTION_FIELDS = frozenset({"mode", "task_flow", "merge_mode"})
_EnumT = TypeVar("_EnumT", bound=Enum)


def _enum_value(enum_type: type[_EnumT], value: object, field: str) -> _EnumT:
    if not isinstance(value, str):
        raise TaskOptionError(f"{field} must be a string")
    try:
        return enum_type(value)
    except ValueError as error:
        allowed = ", ".join(repr(item.value) for item in enum_type)
        raise TaskOptionError(f"{field} must be one of: {allowed}") from error


def parse_task_selection(value: Mapping[str, object]) -> TaskSelection:
    """Parse one Task choice without defaults, aliases, or extra fields."""

    if not isinstance(value, Mapping):
        raise TaskOptionError("task selection must be an object")
    fields = set(value)
    missing = _TASK_SELECTION_FIELDS - fields
    extra = fields - _TASK_SELECTION_FIELDS
    if missing:
        raise TaskOptionError(f"missing fields: {', '.join(sorted(missing))}")
    if extra:
        raise TaskOptionError(f"unexpected fields: {', '.join(sorted(extra))}")

    mode = _enum_value(Mode, value["mode"], "mode")
    if mode is not Mode.TASK:
        raise TaskOptionError("mode must be 'task' for a task selection")
    return TaskSelection(
        mode=Mode.TASK,
        task_flow=_enum_value(TaskFlow, value["task_flow"], "task_flow"),
        merge_mode=_enum_value(MergeMode, value["merge_mode"], "merge_mode"),
    )


def all_task_selections() -> tuple[TaskSelection, ...]:
    """Return the nine allowed Task flow and merge-mode combinations."""

    return tuple(
        TaskSelection(Mode.TASK, task_flow, merge_mode)
        for task_flow in TaskFlow
        for merge_mode in MergeMode
    )
