"""Recognize only the five user-approved full Task Stop commands."""

from __future__ import annotations

import re
from dataclasses import dataclass


_MAX_ISSUE_NUMBER = (1 << 63) - 1
_OUTER_HORIZONTAL_SPACE = re.compile(r"^[ \t]*(?P<command>.*?)[ \t]*$")
_NUMBERED_COMMANDS = (
    re.compile(r"^forge stop #(?P<issue>[1-9][0-9]*)$"),
    re.compile(r"^#(?P<issue>[1-9][0-9]*) 실행 중단$"),
    re.compile(r"^#(?P<issue>[1-9][0-9]*) 작업 중단해$"),
)
_UNNUMBERED_COMMANDS = frozenset({"forge stop", "현재 Task 멈춰"})


@dataclass(frozen=True, slots=True)
class StopCommand:
    """A deterministic Stop request, optionally naming a parent Issue."""

    issue_number: int | None


def parse_stop_command(value: object) -> StopCommand | None:
    """Return a command only for an exact full-string grammar match."""

    if not isinstance(value, str):
        return None
    outer = _OUTER_HORIZONTAL_SPACE.fullmatch(value)
    if outer is None:
        return None
    command = outer.group("command")
    if "\n" in command or "\r" in command:
        return None
    if command in _UNNUMBERED_COMMANDS:
        return StopCommand(issue_number=None)
    for pattern in _NUMBERED_COMMANDS:
        matched = pattern.fullmatch(command)
        if matched is None:
            continue
        issue_number = int(matched.group("issue"))
        if issue_number > _MAX_ISSUE_NUMBER:
            return None
        return StopCommand(issue_number=issue_number)
    return None


__all__ = ["StopCommand", "parse_stop_command"]
