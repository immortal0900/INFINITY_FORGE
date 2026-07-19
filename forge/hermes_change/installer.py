"""Safely add the generic ``pre_user_turn`` hook to a Hermes checkout."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


_MANIFEST_NAME = "installed-files-list.json"
_RELEASE_DIR = "release_files"
_RESTORE_DIR = "restore_files"
_STATE_NAME = ".infinity-forge-change-state.json"
_HOOK_MARKER = "INFINITY_FORGE_PRE_USER_TURN_V1"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class InstallError(RuntimeError):
    """Raised when a source hash, package file, or source anchor is unsafe."""


def _load_change_targets() -> tuple[str, ...]:
    """Load the deploy-visible target manifest shared with platform scripts."""

    try:
        payload = json.loads(Path(__file__).with_name("targets.json").read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InstallError("Hermes change target manifest is missing or invalid") from error
    if not isinstance(payload, list) or not payload:
        raise InstallError("Hermes change target manifest must be a non-empty array")
    targets: list[str] = []
    for target in payload:
        if not isinstance(target, str) or not target or "\\" in target:
            raise InstallError("Hermes change target must be a POSIX relative path")
        path = Path(target)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise InstallError("Hermes change target must stay below the Hermes root")
        targets.append(target)
    if len(targets) != len(set(targets)):
        raise InstallError("Hermes change target manifest contains duplicates")
    return tuple(targets)


@dataclass(frozen=True)
class ChangedSourceFile:
    path: str
    before_file_hash: str
    after_file_hash: str
    release_file: str
    restore_file: str


@dataclass(frozen=True)
class ChangeManifest:
    source_version: str
    files: tuple[ChangedSourceFile, ...]


def file_hash(path: Path) -> str:
    """Return the SHA-256 of one file without following directory inputs."""

    if path.is_symlink() or not path.is_file():
        raise InstallError(f"file is missing: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def change_plugins_source(source: str) -> str:
    """Register the generic hook in Hermes' public plugin hook set."""

    if _HOOK_MARKER in source or '"pre_user_turn"' in source:
        raise InstallError("pre_user_turn is already installed")
    newline = "\r\n" if "\r\n" in source else "\n"
    anchor = f'    "pre_gateway_dispatch",{newline}'
    if source.count(anchor) != 1:
        raise InstallError("plugins.py pre_gateway_dispatch anchor is not unique")
    addition = (
        anchor
        + f"    # {_HOOK_MARKER}: generic user-input decision before the model.{newline}"
        + f'    "pre_user_turn",{newline}'
    )
    return source.replace(anchor, addition, 1)


_CONVERSATION_HOOK = fr'''
    # RISK(security): these names belong to the authenticated transport. A
    # model/user envelope with the same names must never become authorization.
    _forge_trusted_field_names = (
        "owner_host", "subject_id", "user_id", "session_id", "surface",
        "source_event_id", "working_directory", "cwd",
    )
    _forge_trusted_context = {{}}
    if is_user_turn and isinstance(trusted_turn_context, dict):
        for _forge_field in (
            "owner_host", "subject_id", "session_id", "surface",
            "source_event_id", "working_directory",
        ):
            _forge_value = trusted_turn_context.get(_forge_field)
            if isinstance(_forge_value, str) and _forge_value:
                _forge_trusted_context[_forge_field] = _forge_value
            elif _forge_field == "working_directory" and _forge_value is None:
                _forge_trusted_context[_forge_field] = None
    if isinstance(user_message, dict):
        user_message = {{
            _forge_key: _forge_value
            for _forge_key, _forge_value in user_message.items()
            if _forge_key not in _forge_trusted_field_names
        }}
    # Reset on every run so an internal/background turn cannot inherit an
    # authenticated identity from the preceding external user turn.
    setattr(agent, "_infinity_forge_trusted_turn_context", {{}})

    if is_user_turn:
        # {_HOOK_MARKER}: only a caller that received a real user turn opts in.
        # Internal build, review, delegate, and batch calls keep the safe default.
        def _valid_pre_user_turn_choice_prompt(_prompt):
            _required_prompt_fields = (
                "choice_prompt_id",
                "choice_mode",
                "min_choices",
                "max_choices",
                "submit_label",
                "expires_at",
                "choices",
            )
            if not isinstance(_prompt, dict) or any(
                _field not in _prompt for _field in _required_prompt_fields
            ):
                return False
            _prompt_id = _prompt.get("choice_prompt_id")
            _expires_text = _prompt.get("expires_at")
            if not isinstance(_prompt_id, str) or not isinstance(_expires_text, str):
                return False
            try:
                import re as _choice_re
                from datetime import datetime as _choice_datetime
                from datetime import timezone as _choice_timezone
                from uuid import UUID as _choice_uuid

                if str(_choice_uuid(_prompt_id)) != _prompt_id:
                    return False
                if not _choice_re.fullmatch(
                    r"\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}:\d{{2}}:\d{{2}}"
                    r"(?:\.\d+)?(?:Z|[+-]\d{{2}}:\d{{2}})",
                    _expires_text,
                ):
                    return False
                _expires_at = _choice_datetime.fromisoformat(
                    _expires_text[:-1] + "+00:00"
                    if _expires_text.endswith("Z")
                    else _expires_text
                )
            except (TypeError, ValueError):
                return False
            if (
                _expires_at.tzinfo is None
                or _expires_at.utcoffset() is None
                or _expires_at <= _choice_datetime.now(_choice_timezone.utc)
            ):
                return False
            _choices = _prompt.get("choices")
            if not isinstance(_choices, list) or not _choices:
                return False
            if not all(
                isinstance(_choice, dict)
                and isinstance(_choice.get("id"), str)
                and bool(_choice["id"].strip())
                and isinstance(_choice.get("label"), str)
                and bool(_choice["label"].strip())
                and isinstance(_choice.get("description"), str)
                and bool(_choice["description"].strip())
                for _choice in _choices
            ):
                return False
            _choice_ids = [_choice["id"] for _choice in _choices]
            _choice_labels = [_choice["label"] for _choice in _choices]
            if (
                len(set(_choice_ids)) != len(_choice_ids)
                or len(set(_choice_labels)) != len(_choice_labels)
            ):
                return False
            _choice_mode = _prompt.get("choice_mode")
            _min_choices = _prompt.get("min_choices")
            _max_choices = _prompt.get("max_choices")
            if (
                not isinstance(_min_choices, int)
                or isinstance(_min_choices, bool)
                or _min_choices < 1
                or _min_choices > len(_choices)
            ):
                return False
            if _choice_mode == "single":
                if (
                    not isinstance(_max_choices, int)
                    or isinstance(_max_choices, bool)
                    or _min_choices != 1
                    or _max_choices != 1
                ):
                    return False
            elif _choice_mode == "multiple":
                if _max_choices is not None and (
                    not isinstance(_max_choices, int)
                    or isinstance(_max_choices, bool)
                    or _max_choices < _min_choices
                    or _max_choices > len(_choices)
                ):
                    return False
            else:
                return False
            return (
                isinstance(_prompt.get("submit_label"), str)
                and bool(_prompt["submit_label"].strip())
            )

        _is_structured_choice_submission = isinstance(user_message, dict) and (
            "choice_prompt_id" in user_message
            or "selected_choice_ids" in user_message
        )

        def _finish_pre_user_turn(_response, _prompt=None):
            _handled_messages = list(conversation_history or [])
            _handled_user_message = (
                persist_user_message
                if isinstance(persist_user_message, str)
                else (
                    user_message.get("text", "")
                    if isinstance(user_message, dict)
                    else user_message
                )
            )
            if (
                isinstance(_handled_user_message, str)
                and _handled_user_message.strip()
            ):
                _handled_messages.append(
                    {{"role": "user", "content": _handled_user_message}}
                )
            _handled_messages.append({{"role": "assistant", "content": _response}})
            _handled_payload = {{
                "final_response": _response,
                "messages": _handled_messages,
                "api_calls": 0,
                "completed": True,
                "handled": True,
                "choices": list(
                    _prompt.get("choices", [])
                    if isinstance(_prompt, dict)
                    else []
                ),
            }}
            if isinstance(_prompt, dict):
                for _prompt_field in (
                    "choice_prompt_id",
                    "choice_mode",
                    "min_choices",
                    "max_choices",
                    "submit_label",
                    "expires_at",
                    "choice_prompt_paused",
                ):
                    if _prompt_field in _prompt:
                        _handled_payload[_prompt_field] = _prompt[_prompt_field]
            return _handled_payload

        try:
            from hermes_cli.plugins import has_hook as _has_pre_user_turn
            from hermes_cli.plugins import invoke_hook as _invoke_pre_user_turn

            _pre_user_turn_registered = _has_pre_user_turn("pre_user_turn")
            # RISK(breaking): stable chooser IDs are authoritative input fields;
            # label text never substitutes for this structured envelope.
            _pre_user_turn_values = {{
                "agent": agent,
                "text": (
                    user_message.get("text", "")
                    if isinstance(user_message, dict)
                    else user_message
                ),
                "task_id": task_id,
                "session_id": str(
                    _forge_trusted_context.get("session_id", "")
                    or getattr(agent, "_gateway_session_key", "")
                    or task_id
                    or "local-session"
                ),
                "user_id": str(
                    _forge_trusted_context.get("subject_id", "")
                    or getattr(agent, "_user_id", "")
                    or getattr(agent, "_gateway_user_id", "")
                    or getattr(agent, "user_id", "")
                    or os.environ.get("HERMES_USER_ID", "")
                    or "local-user"
                ),
                "surface": str(
                    _forge_trusted_context.get("surface", "")
                    or getattr(agent, "platform", "")
                    or os.environ.get("HERMES_SESSION_SOURCE", "cli")
                ),
                # Trusted transport metadata is carried separately from the
                # user-controlled message/envelope and is never inferred here.
                "working_directory": _forge_trusted_context.get(
                    "working_directory", working_directory
                ),
                "owner_host": str(
                    _forge_trusted_context.get("owner_host", "")
                ),
                "subject_id": str(
                    _forge_trusted_context.get("subject_id", "")
                ),
                "source_event_id": str(
                    _forge_trusted_context.get("source_event_id", "")
                ),
                "is_new_session": (
                    False
                    if isinstance(user_message, dict)
                    and (
                        "choice_prompt_id" in user_message
                        or "selected_choice_ids" in user_message
                    )
                    else not bool(conversation_history)
                ),
            }}
            _forge_trusted_context = {{
                "owner_host": _pre_user_turn_values["owner_host"],
                "subject_id": _pre_user_turn_values["user_id"],
                "session_id": _pre_user_turn_values["session_id"],
                "surface": _pre_user_turn_values["surface"],
                "source_event_id": _pre_user_turn_values["source_event_id"],
                "working_directory": _pre_user_turn_values["working_directory"],
            }}
            setattr(
                agent,
                "_infinity_forge_trusted_turn_context",
                dict(_forge_trusted_context),
            )
            if isinstance(user_message, dict):
                for _submission_field in (
                    "choice_prompt_id",
                    "selected_choice_ids",
                ):
                    if _submission_field in user_message:
                        _pre_user_turn_values[_submission_field] = user_message[
                            _submission_field
                        ]
            _pre_user_turn_results = (
                _invoke_pre_user_turn(
                    "pre_user_turn",
                    **_pre_user_turn_values,
                )
                if _pre_user_turn_registered
                else []
            )
        except Exception as _pre_user_turn_error:
            logger.warning("pre_user_turn invocation failed: %s", _pre_user_turn_error)
            return _finish_pre_user_turn(
                "Hermes could not run the user-turn chooser. No model request was made."
            )

        _handled_pre_user_turn_results = []
        _replace_pre_user_turn_results = []
        _invalid_pre_user_turn_result = bool(
            _pre_user_turn_registered and not _pre_user_turn_results
        )
        for _pre_user_turn_result in _pre_user_turn_results:
            if not isinstance(_pre_user_turn_result, dict):
                _invalid_pre_user_turn_result = True
                continue
            _pre_user_turn_action = _pre_user_turn_result.get("action")
            if _pre_user_turn_action == "handled":
                _handled_pre_user_turn_results.append(_pre_user_turn_result)
            elif _pre_user_turn_action == "replace":
                _replace_pre_user_turn_results.append(_pre_user_turn_result)
            elif _pre_user_turn_action != "continue":
                _invalid_pre_user_turn_result = True

        # RISK(security): a policy result may veto a model request. Conflicting
        # callbacks therefore stop closed instead of depending on plugin order.
        if (
            _invalid_pre_user_turn_result
            or len(_handled_pre_user_turn_results) > 1
            or len(_replace_pre_user_turn_results) > 1
            or (
                _handled_pre_user_turn_results
                and _replace_pre_user_turn_results
            )
            or (
                _is_structured_choice_submission
                and not _handled_pre_user_turn_results
                and (
                    len(_replace_pre_user_turn_results) != 1
                    or user_message.get("selected_choice_ids") != ["chat"]
                )
            )
        ):
            return _finish_pre_user_turn(
                "Hermes user-turn plugin results conflict. No model request was made."
            )

        if _handled_pre_user_turn_results:
            _handled_result = _handled_pre_user_turn_results[0]
            _response = _handled_result.get("text")
            _choices = _handled_result.get("choices", [])
            _choice_prompt_fields = (
                "choice_prompt_id",
                "choice_mode",
                "min_choices",
                "max_choices",
                "submit_label",
                "expires_at",
            )
            _has_choice_prompt = any(
                _field in _handled_result for _field in _choice_prompt_fields
            )
            # RISK(breaking): every user surface relies on stable text IDs and
            # labels; malformed or ambiguous choice objects must stop closed.
            _valid_choices = (
                isinstance(_choices, list)
                and all(
                    isinstance(_choice, dict)
                    and isinstance(_choice.get("id"), str)
                    and bool(_choice["id"].strip())
                    and isinstance(_choice.get("label"), str)
                    and bool(_choice["label"].strip())
                    and (
                        not _has_choice_prompt
                        or (
                            isinstance(_choice.get("description"), str)
                            and bool(_choice["description"].strip())
                        )
                    )
                    for _choice in _choices
                )
            )
            _choice_ids = (
                [_choice["id"] for _choice in _choices]
                if _valid_choices
                else []
            )
            _choice_labels = (
                [_choice["label"] for _choice in _choices]
                if _valid_choices
                else []
            )
            _valid_choice_prompt = (
                not _has_choice_prompt
                or _valid_pre_user_turn_choice_prompt(_handled_result)
            )
            _choice_prompt_paused = _handled_result.get(
                "choice_prompt_paused", False
            )
            if (
                not isinstance(_response, str)
                or not _valid_choices
                or not _valid_choice_prompt
                or not isinstance(_choice_prompt_paused, bool)
                or (_choice_prompt_paused and not _has_choice_prompt)
                or len(set(_choice_ids)) != len(_choice_ids)
                or len(set(_choice_labels)) != len(_choice_labels)
            ):
                return _finish_pre_user_turn(
                    "Hermes user-turn plugin results conflict. No model request was made."
                )
            return _finish_pre_user_turn(_response, _handled_result)

        if _replace_pre_user_turn_results:
            _replacement = _replace_pre_user_turn_results[0].get("text")
            if not isinstance(_replacement, str):
                return _finish_pre_user_turn(
                    "Hermes user-turn plugin results conflict. No model request was made."
                )
            user_message = _replacement
            persist_user_message = _replacement
'''


def change_conversation_source(source: str) -> str:
    """Insert generic hook handling at the start of ``run_conversation``."""

    if _HOOK_MARKER in source:
        raise InstallError("pre_user_turn is already installed")
    function_start = source.find("def run_conversation(")
    if function_start < 0:
        raise InstallError("conversation_loop.py run_conversation was not found")
    newline = "\r\n" if "\r\n" in source else "\n"
    signature_anchor = (
        f"    moa_config: Optional[dict[str, Any]] = None,{newline}"
        f") -> Dict[str, Any]:{newline}"
    )
    signature_position = source.find(signature_anchor, function_start)
    if signature_position < 0:
        raise InstallError("conversation_loop.py user-turn signature anchor was not found")
    signature_replacement = (
        f"    moa_config: Optional[dict[str, Any]] = None,{newline}"
        f"    is_user_turn: bool = False,{newline}"
        f"    working_directory: Optional[str] = None,{newline}"
        f"    trusted_turn_context: Optional[dict[str, Any]] = None,{newline}"
        f") -> Dict[str, Any]:{newline}"
    )
    source = (
        source[:signature_position]
        + signature_replacement
        + source[signature_position + len(signature_anchor) :]
    )
    anchor = f"{newline}    if moa_config is None:{newline}"
    insertion = source.find(anchor, function_start)
    if insertion < 0:
        raise InstallError("conversation_loop.py prologue anchor was not found")
    hook = _CONVERSATION_HOOK.replace("\n", newline)
    return source[:insertion] + newline + hook + source[insertion:]


def _insert_after_unique_line(
    source: str, expected: str, additions: tuple[str, ...], *, label: str
) -> str:
    lines = source.splitlines(keepends=True)
    matches = [index for index, line in enumerate(lines) if line.strip() == expected]
    if len(matches) != 1:
        raise InstallError(f"{label} anchor is not unique")
    index = matches[0]
    original = lines[index]
    newline = "\r\n" if original.endswith("\r\n") else "\n"
    indent = original[: len(original) - len(original.lstrip())]
    lines[index + 1 : index + 1] = [
        f"{indent}{addition}{newline}" for addition in additions
    ]
    return "".join(lines)


def _insert_before_unique_line(
    source: str, expected: str, additions: tuple[str, ...], *, label: str
) -> str:
    lines = source.splitlines(keepends=True)
    matches = [index for index, line in enumerate(lines) if line.strip() == expected]
    if len(matches) != 1:
        raise InstallError(f"{label} anchor is not unique")
    index = matches[0]
    original = lines[index]
    newline = "\r\n" if original.endswith("\r\n") else "\n"
    indent = original[: len(original) - len(original.lstrip())]
    lines[index:index] = [f"{indent}{addition}{newline}" for addition in additions]
    return "".join(lines)


def _replace_unique_line(source: str, expected: str, replacement: str, *, label: str) -> str:
    lines = source.splitlines(keepends=True)
    matches = [index for index, line in enumerate(lines) if line.strip() == expected]
    if len(matches) != 1:
        raise InstallError(f"{label} anchor is not unique")
    index = matches[0]
    original = lines[index]
    newline = "\r\n" if original.endswith("\r\n") else "\n"
    indent = original[: len(original) - len(original.lstrip())]
    lines[index] = f"{indent}{replacement}{newline}"
    return "".join(lines)


def _insert_in_unique_sequence(
    source: str,
    sequence: tuple[str, ...],
    *,
    after: int,
    addition: str,
    label: str,
) -> str:
    lines = source.splitlines(keepends=True)
    stripped = [line.strip() for line in lines]
    matches = [
        index
        for index in range(len(lines) - len(sequence) + 1)
        if tuple(stripped[index : index + len(sequence)]) == sequence
    ]
    if len(matches) != 1:
        raise InstallError(f"{label} anchor is not unique")
    index = matches[0] + after
    original = lines[index]
    newline = "\r\n" if original.endswith("\r\n") else "\n"
    indent = original[: len(original) - len(original.lstrip())]
    lines.insert(index + 1, f"{indent}{addition}{newline}")
    return "".join(lines)


def _replace_unique_sequence(
    source: str,
    sequence: tuple[str, ...],
    replacement: tuple[str, ...],
    *,
    label: str,
) -> str:
    lines = source.splitlines(keepends=True)
    stripped = [line.strip() for line in lines]
    matches = [
        index
        for index in range(len(lines) - len(sequence) + 1)
        if tuple(stripped[index : index + len(sequence)]) == sequence
    ]
    if len(matches) != 1:
        raise InstallError(f"{label} anchor is not unique")
    index = matches[0]
    original = lines[index]
    newline = "\r\n" if original.endswith("\r\n") else "\n"
    indent = original[: len(original) - len(original.lstrip())]
    lines[index : index + len(sequence)] = [
        f"{indent}{item}{newline}" for item in replacement
    ]
    return "".join(lines)


def _insert_before_function_return_after_anchor(
    source: str,
    *,
    anchor: str,
    additions: tuple[str, ...],
    max_lines: int,
    label: str,
) -> str:
    lines = source.splitlines(keepends=True)
    anchors = [index for index, line in enumerate(lines) if line.strip() == anchor]
    if len(anchors) != 1:
        raise InstallError(f"{label} anchor is not unique")
    anchor_index = anchors[0]
    anchor_line = lines[anchor_index]
    indent = anchor_line[: len(anchor_line) - len(anchor_line.lstrip())]
    matches = [
        index
        for index in range(
            anchor_index + 1,
            min(len(lines), anchor_index + max_lines),
        )
        if lines[index].startswith(indent)
        and not lines[index].startswith(indent + " ")
        and lines[index].strip() in {"return response", "return result"}
    ]
    if len(matches) != 1:
        raise InstallError(f"{label} return seam is not unique")
    return_index = matches[0]
    original = lines[return_index]
    newline = "\r\n" if original.endswith("\r\n") else "\n"
    lines[return_index:return_index] = [
        f"{indent}{addition}{newline}" for addition in additions
    ]
    return "".join(lines)


def _insert_after_line_in_unique_block(
    source: str,
    *,
    block_start: str,
    expected: str,
    addition: str | tuple[str, ...],
    max_lines: int,
    label: str,
) -> str:
    lines = source.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.strip() == block_start]
    if len(starts) != 1:
        raise InstallError(f"{label} block is not unique")
    start = starts[0]
    matches = [
        index
        for index in range(start, min(len(lines), start + max_lines))
        if lines[index].strip() == expected
    ]
    if len(matches) != 1:
        raise InstallError(f"{label} anchor is not unique")
    index = matches[0]
    original = lines[index]
    newline = "\r\n" if original.endswith("\r\n") else "\n"
    indent = original[: len(original) - len(original.lstrip())]
    additions = (addition,) if isinstance(addition, str) else addition
    lines[index + 1 : index + 1] = [
        f"{indent}{item}{newline}" for item in additions
    ]
    return "".join(lines)


def _insert_after_guard_in_unique_block(
    source: str,
    *,
    block_start: str,
    guard: str,
    terminal: str,
    addition: tuple[str, ...],
    max_lines: int,
    label: str,
) -> str:
    """Insert at the guard's scope, after its one-line nested terminal."""

    lines = source.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.strip() == block_start]
    if len(starts) != 1:
        raise InstallError(f"{label} block is not unique")
    start = starts[0]
    end = min(len(lines) - 1, start + max_lines)
    matches = [
        index
        for index in range(start, end)
        if lines[index].strip() == guard and lines[index + 1].strip() == terminal
    ]
    if len(matches) != 1:
        raise InstallError(f"{label} guard is not unique")
    guard_index = matches[0]
    guard_line = lines[guard_index]
    terminal_line = lines[guard_index + 1]
    indent = guard_line[: len(guard_line) - len(guard_line.lstrip())]
    terminal_indent = terminal_line[
        : len(terminal_line) - len(terminal_line.lstrip())
    ]
    if len(terminal_indent) <= len(indent):
        raise InstallError(f"{label} terminal is not nested under its guard")
    newline = "\r\n" if terminal_line.endswith("\r\n") else "\n"
    lines[guard_index + 2 : guard_index + 2] = [
        f"{indent}{item}{newline}" for item in addition
    ]
    return "".join(lines)


def change_run_agent_source(source: str) -> str:
    """Forward trusted user-turn metadata while defaulting internal calls off."""

    if _HOOK_MARKER in source:
        raise InstallError("run_agent.py user-turn forwarding is already installed")
    source = _insert_after_unique_line(
        source,
        "moa_config: Optional[dict[str, Any]] = None,",
        (
            f"# {_HOOK_MARKER}: internal calls remain False unless a user surface opts in.",
            "# RISK(breaking): this optional public argument defaults off for every existing caller.",
            "is_user_turn: bool = False,",
            "working_directory: Optional[str] = None,",
            "trusted_turn_context: Optional[dict[str, Any]] = None,",
        ),
        label="run_agent.py signature",
    )
    return _insert_after_unique_line(
        source,
        "moa_config=moa_config,",
        (
            "is_user_turn=is_user_turn,",
            "working_directory=working_directory,",
            "trusted_turn_context=trusted_turn_context,",
        ),
        label="run_agent.py forwarding",
    )


_TOOL_EXECUTOR_TRUST_HELPERS = r'''
# INFINITY_FORGE_PRE_USER_TURN_V1: authenticated Forge tool context.
_FORGE_TRUSTED_TURN_FIELDS = frozenset({
    "owner_host", "subject_id", "user_id", "session_id", "surface",
    "source_event_id", "working_directory", "cwd",
})
_FORGE_MUTATING_TOOLS = frozenset({"send_to_task", "stop_task"})


def _forge_bind_trusted_tool_context(agent, function_name: str, function_args: dict):
    # RISK(security): model-generated identity keys are discarded before any
    # middleware, guardrail, approval, hook, or tool handler can observe them.
    sanitized = {
        key: value
        for key, value in function_args.items()
        if key not in _FORGE_TRUSTED_TURN_FIELDS
    }
    raw_context = getattr(agent, "_infinity_forge_trusted_turn_context", {})
    trusted_context = {
        key: raw_context.get(key)
        for key in (
            "owner_host", "subject_id", "session_id", "surface",
            "source_event_id", "working_directory",
        )
        if isinstance(raw_context, dict)
        and (isinstance(raw_context.get(key), str) or raw_context.get(key) is None)
    }
    source_event_id = trusted_context.get("source_event_id")
    block_message = None
    if function_name in _FORGE_MUTATING_TOOLS and (
        not isinstance(source_event_id, str) or not source_event_id.strip()
    ):
        block_message = (
            "Authenticated source event ID is unavailable; retry this user turn "
            "before changing a Forge Task."
        )
    return sanitized, trusted_context, block_message
'''


def change_tool_executor_source(source: str) -> str:
    """Bind authenticated turn metadata before both Hermes tool dispatch paths."""

    if _HOOK_MARKER in source:
        raise InstallError("tool_executor.py trusted turn handling is already installed")
    source = _insert_before_unique_line(
        source,
        "def _apply_tool_request_middleware_for_agent(",
        tuple(_TOOL_EXECUTOR_TRUST_HELPERS.strip("\n").splitlines()) + ("",),
        label="tool executor trusted context helpers",
    )
    source = _insert_after_line_in_unique_block(
        source,
        block_start="def _apply_tool_request_middleware_for_agent(",
        expected="try:",
        addition=(
            "    function_args, _forge_trusted_context, _forge_context_block = (",
            "        _forge_bind_trusted_tool_context(agent, function_name, function_args)",
            "    )",
        ),
        max_lines=14,
        label="tool executor trusted context binding",
    )
    source = _insert_after_line_in_unique_block(
        source,
        block_start="result = apply_tool_request_middleware(",
        expected='api_request_id=getattr(agent, "_current_api_request_id", "") or "",',
        addition="trusted_turn_context=_forge_trusted_context,",
        max_lines=14,
        label="tool request trusted middleware context",
    )
    if source.count("return payload, list(result.trace)") != 1:
        raise InstallError("tool executor middleware success return is not unique")
    source = source.replace(
        "return payload, list(result.trace)",
        "return payload, list(result.trace), _forge_context_block",
        1,
    )
    if source.count("return function_args, []") != 1:
        raise InstallError("tool executor middleware failure return is not unique")
    source = source.replace(
        "return function_args, []",
        "return function_args, [], _forge_context_block",
        1,
    )
    assignment = (
        "function_args, middleware_trace = "
        "_apply_tool_request_middleware_for_agent("
    )
    if source.count(assignment) != 2:
        raise InstallError("tool executor middleware call paths are not exact")
    source = source.replace(
        assignment,
        "function_args, middleware_trace, _forge_context_block = "
        "_apply_tool_request_middleware_for_agent(",
    )

    sequential_markers = (
        "def execute_tool_calls_sequential(",
        "def sequential(",
    )
    sequential_positions = [source.find(marker) for marker in sequential_markers]
    sequential_positions = [position for position in sequential_positions if position >= 0]
    if len(sequential_positions) != 1:
        raise InstallError("tool executor sequential path is not unique")
    split_at = sequential_positions[0]
    concurrent, sequential = source[:split_at], source[split_at:]
    concurrent = _insert_after_line_in_unique_block(
        concurrent,
        block_start="function_args, middleware_trace, _forge_context_block = _apply_tool_request_middleware_for_agent(",
        expected=")",
        addition=(
            "if _forge_context_block is not None:",
            "    _ts_scope_block = json.dumps({\"error\": _forge_context_block}, ensure_ascii=False)",
        ),
        max_lines=12,
        label="concurrent Forge source event block",
    )
    sequential = _insert_after_line_in_unique_block(
        sequential,
        block_start="function_args, middleware_trace, _forge_context_block = _apply_tool_request_middleware_for_agent(",
        expected=")",
        addition=(
            "if _forge_context_block is not None:",
            "    _ts_scope_block = _forge_context_block",
        ),
        max_lines=12,
        label="sequential Forge source event block",
    )
    return concurrent + sequential


_CLI_CHOICE_METHODS = r'''
# RISK(breaking): classic CLI submissions carry stable IDs, never display labels.
def _is_valid_choice_prompt(self, prompt: dict) -> bool:
    """Reject malformed, ambiguous, or expired chooser metadata."""
    required = (
        "choice_prompt_id",
        "choice_mode",
        "min_choices",
        "max_choices",
        "submit_label",
        "expires_at",
        "choices",
    )
    if not isinstance(prompt, dict) or any(field not in prompt for field in required):
        return False
    prompt_id = prompt.get("choice_prompt_id")
    expires_text = prompt.get("expires_at")
    if not isinstance(prompt_id, str) or not isinstance(expires_text, str):
        return False
    try:
        import re as _choice_re
        from datetime import datetime as _choice_datetime
        from datetime import timezone as _choice_timezone
        from uuid import UUID as _choice_uuid

        if str(_choice_uuid(prompt_id)) != prompt_id:
            return False
        if not _choice_re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
            r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
            expires_text,
        ):
            return False
        expires_at = _choice_datetime.fromisoformat(
            expires_text[:-1] + "+00:00"
            if expires_text.endswith("Z")
            else expires_text
        )
    except (TypeError, ValueError):
        return False
    if (
        expires_at.tzinfo is None
        or expires_at.utcoffset() is None
        or expires_at <= _choice_datetime.now(_choice_timezone.utc)
    ):
        return False
    choices = prompt.get("choices")
    if not isinstance(choices, list) or not choices:
        return False
    if not all(
        isinstance(choice, dict)
        and isinstance(choice.get("id"), str)
        and bool(choice["id"].strip())
        and isinstance(choice.get("label"), str)
        and bool(choice["label"].strip())
        and isinstance(choice.get("description"), str)
        and bool(choice["description"].strip())
        for choice in choices
    ):
        return False
    choice_ids = [choice["id"] for choice in choices]
    choice_labels = [choice["label"] for choice in choices]
    if len(set(choice_ids)) != len(choice_ids) or len(set(choice_labels)) != len(
        choice_labels
    ):
        return False
    choice_mode = prompt.get("choice_mode")
    min_choices = prompt.get("min_choices")
    max_choices = prompt.get("max_choices")
    if (
        not isinstance(min_choices, int)
        or isinstance(min_choices, bool)
        or min_choices < 1
        or min_choices > len(choices)
    ):
        return False
    if choice_mode == "single":
        if (
            not isinstance(max_choices, int)
            or isinstance(max_choices, bool)
            or min_choices != 1
            or max_choices != 1
        ):
            return False
    elif choice_mode == "multiple":
        if max_choices is not None and (
            not isinstance(max_choices, int)
            or isinstance(max_choices, bool)
            or max_choices < min_choices
            or max_choices > len(choices)
        ):
            return False
    else:
        return False
    return isinstance(prompt.get("submit_label"), str) and bool(
        prompt["submit_label"].strip()
    )

def _prompt_choice_modal(self, prompt: dict, timeout: float = 120) -> dict | None:
    """Return one structured chooser submission, or ``None`` on cancel/timeout."""
    if not self._is_valid_choice_prompt(prompt):
        return None
    choice_mode = prompt.get("choice_mode")
    choices = prompt.get("choices")
    if not getattr(self, "_app", None):
        return None
    if not getattr(sys.stdin, "isatty", lambda: False)():
        return None
    if not getattr(sys.stdout, "isatty", lambda: False)():
        return None
    modal_choices = [
        (choice["id"], choice["label"], choice["description"])
        for choice in choices
    ]
    min_choices = prompt.get("min_choices")
    max_choices = prompt.get("max_choices")
    initial_selected = -1 if choice_mode == "single" else 0
    # RISK(race): the existing app-loop handoff remains the sole writer of the
    # shared modal state while the process_loop thread waits on its queue.
    selected = self._prompt_text_input_modal(
        title=prompt.get("submit_label") or "Choose",
        detail=prompt.get("final_response") or "",
        choices=modal_choices,
        timeout=timeout,
        _choice_modal_state={
            "structured_choice_modal": True,
            "choice_mode": choice_mode,
            "min_choices": min_choices,
            "max_choices": max_choices,
            "submit_label": prompt.get("submit_label") or "Done",
            "selected": initial_selected,
            "selected_ids": set(),
        },
    )
    if selected is None:
        return None
    if not self._is_valid_choice_prompt(prompt):
        return None
    selected_ids = [selected] if isinstance(selected, str) else list(selected)
    allowed_ids = {choice[0] for choice in modal_choices}
    if (
        len(set(selected_ids)) != len(selected_ids)
        or any(choice_id not in allowed_ids for choice_id in selected_ids)
        or len(selected_ids) < min_choices
        or (max_choices is not None and len(selected_ids) > max_choices)
    ):
        return None
    return {
        "choice_prompt_id": prompt["choice_prompt_id"],
        "selected_choice_ids": selected_ids,
    }

def _toggle_choice_modal_selection(self) -> bool:
    state = self._slash_confirm_state
    if not state or state.get("choice_mode") != "multiple":
        return False
    choices = state.get("choices") or []
    selected = state.get("selected", 0)
    if not 0 <= selected < len(choices):
        return False
    selected_ids = state.setdefault("selected_ids", set())
    choice_id = choices[selected][0]
    if choice_id in selected_ids:
        selected_ids.remove(choice_id)
    else:
        max_choices = state.get("max_choices")
        if max_choices is not None and len(selected_ids) >= max_choices:
            state["validation_error"] = f"Choose at most {max_choices}."
            self._invalidate()
            return False
        selected_ids.add(choice_id)
    state.pop("validation_error", None)
    self._invalidate()
    return True

def _submit_choice_modal_selection(self) -> bool:
    state = self._slash_confirm_state
    if not state or not state.get("structured_choice_modal"):
        return False
    choices = state.get("choices") or []
    if state.get("choice_mode") == "multiple":
        selected_ids = state.get("selected_ids") or set()
        ordered_ids = [choice[0] for choice in choices if choice[0] in selected_ids]
    else:
        selected = state.get("selected", -1)
        ordered_ids = [choices[selected][0]] if 0 <= selected < len(choices) else []
    min_choices = state.get("min_choices", 1)
    max_choices = state.get("max_choices")
    if len(ordered_ids) < min_choices:
        state["validation_error"] = f"Choose at least {min_choices}."
        self._invalidate()
        return False
    if max_choices is not None and len(ordered_ids) > max_choices:
        state["validation_error"] = f"Choose at most {max_choices}."
        self._invalidate()
        return False
    value = ordered_ids if state.get("choice_mode") == "multiple" else ordered_ids[0]
    self._submit_slash_confirm_response(value)
    return True

def _choice_modal_instructions(self, state: dict | None = None) -> str:
    state = state or self._slash_confirm_state or {}
    if not state.get("structured_choice_modal"):
        return "Type 1/2/3 or use ↑/↓ then Enter. ESC/Ctrl+C cancels."
    if state.get("choice_mode") == "multiple":
        label = state.get("submit_label") or "Done"
        return f"Use ↑/↓, Space to toggle, Enter for {label}. ESC/Ctrl+C cancels."
    return "Use ↑/↓ then Enter, or press a number. ESC/Ctrl+C cancels."

def _continue_choice_modal_result(
    self,
    result: dict | None,
    *,
    conversation_history,
    stream_callback,
    task_id,
    moa_config,
    working_directory=None,
    trusted_turn_context=None,
):
    """Resolve bounded handled choosers through the same user-turn hook path."""
    # A direct Project can add remote and branch choosers before the existing
    # Projects, flow, merge, 256 rank, and final Confirm submissions.
    _max_consecutive_chooser_turns = 264
    for _choice_turn in range(_max_consecutive_chooser_turns):
        if isinstance(result, dict) and result.get("choice_prompt_paused") is True:
            return result
        if (
            not isinstance(result, dict)
            or not result.get("handled")
            or not self._is_valid_choice_prompt(result)
        ):
            return result
        submission = self._prompt_choice_modal(result)
        if submission is None:
            return result
        result = self.agent.run_conversation(
            user_message=submission,
            conversation_history=conversation_history,
            stream_callback=stream_callback,
            task_id=task_id,
            is_user_turn=True,
            working_directory=working_directory,
            trusted_turn_context=trusted_turn_context,
            persist_user_message=None,
            moa_config=moa_config,
        )
    if isinstance(result, dict) and result.get("choice_prompt_paused") is True:
        return result
    if (
        not isinstance(result, dict)
        or not result.get("handled")
        or not self._is_valid_choice_prompt(result)
    ):
        return result
    return {
        "final_response": "Hermes chooser stopped after too many consecutive prompts.",
        "messages": result.get("messages", conversation_history),
        "api_calls": 0,
        "completed": True,
        "handled": True,
        "choices": [],
    }
'''


def _choice_display_lines(result_name: str, response_name: str) -> tuple[str, ...]:
    """Return fail-closed source lines that make structured choices visible."""

    return (
        "# RISK(breaking): structured chooser options must survive this user-surface boundary.",
        (
            f'_pre_user_turn_choices = {result_name}.get("choices", []) '
            f"if isinstance({result_name}, dict) else []"
        ),
        (
            f'_choice_prompt_id = {result_name}.get("choice_prompt_id") '
            f"if isinstance({result_name}, dict) else None"
        ),
        "if _pre_user_turn_choices:",
        "    _pre_user_turn_entries = [",
        "        (",
        '            _choice["id"],',
        '            _choice["label"],',
        '            _choice.get("description", ""),',
        "        )",
        "        for _choice in _pre_user_turn_choices",
        (
            "        if isinstance(_choice, dict) "
            'and isinstance(_choice.get("id"), str) '
            'and isinstance(_choice.get("label"), str) '
            'and isinstance(_choice.get("description", ""), str)'
        ),
        "    ]",
        "    if len(_pre_user_turn_entries) != len(_pre_user_turn_choices):",
        "        _pre_user_turn_choices = []",
        f'        {response_name} = "Hermes user-turn plugin results conflict. No model request was made."',
        f'        {result_name}["choices"] = []',
        f'        {result_name}["final_response"] = {response_name}',
        (
            "    elif any("
            f'f"[id: {{_choice_id}}]" not in {response_name} '
            "for _choice_id, _label, _description in _pre_user_turn_entries"
            "):"
        ),
        (
            f'        {response_name} = f"{{{response_name}}}\\n\\nAvailable choices:\\n" '
            '+ "\\n".join('
            'f"- {_label} [id: {_choice_id}]" '
            '+ (f" — {_description}" if _description else "") '
            "for _choice_id, _label, _description in _pre_user_turn_entries"
            ")"
        ),
        "        if isinstance(_choice_prompt_id, str):",
        (
            f'            {response_name} = f"{{{response_name}}}\\nReply with: '
            'choose {_choice_prompt_id} <choice_id[,choice_id...]>"'
        ),
        "        else:",
        f'            {response_name} = f"{{{response_name}}}\\nReply with exact ID."',
        f'        {result_name}["final_response"] = {response_name}',
    )


def change_cli_source(source: str) -> str:
    """Mark classic CLI input as a user turn and skip handled-turn title calls."""

    if _HOOK_MARKER in source:
        raise InstallError("cli.py user-turn handling is already installed")
    source = _insert_after_unique_line(
        source,
        "_moa_cfg = None",
        (
            "# Capture once for this real CLI turn; chooser re-entry carries it unchanged.",
            "try:",
            "    _forge_working_directory = __import__(\"os\").getcwd()",
            "except OSError:",
            "    _forge_working_directory = None",
            "_forge_source_outbox = None",
            "_forge_source_event_id = \"\"",
            "try:",
            "    from forge.ops.surface_events import LocalSurfaceOutbox as _ForgeSurfaceOutbox",
            "    _forge_outbox_path = __import__(\"os\").environ.get(\"INFINITY_FORGE_SOURCE_EVENT_OUTBOX\")",
            "    if not _forge_outbox_path:",
            "        _forge_outbox_path = str(__import__(\"pathlib\").Path.home() / \".hermes\" / \"infinity-forge\" / \"surface-events.json\")",
            "    _forge_source_outbox = _ForgeSurfaceOutbox(_forge_outbox_path)",
            "    _forge_source_event_id = _forge_source_outbox.prepare(",
            "        surface=\"cli\", session_id=str(self.session_id), payload=message",
            "    )",
            "except Exception as _forge_outbox_error:",
            "    __import__(\"logging\").getLogger(__name__).warning(",
            "        \"Infinity Forge source-event outbox unavailable: %s\", _forge_outbox_error",
            "    )",
            "_forge_trusted_turn_context = {",
            "    \"owner_host\": __import__(\"os\").environ.get(\"INFINITY_FORGE_HOST_ID\", \"\"),",
            "    \"subject_id\": __import__(\"os\").environ.get(\"HERMES_USER_ID\", \"local-user\"),",
            "    \"session_id\": str(self.session_id),",
            "    \"surface\": \"cli\",",
            "    \"source_event_id\": _forge_source_event_id,",
            "    \"working_directory\": _forge_working_directory,",
            "}",
        ),
        label="cli.py initial working directory capture",
    )
    source = _insert_after_line_in_unique_block(
        source,
        block_start="result = self.agent.run_conversation(",
        expected="task_id=self.session_id,",
        addition=f"is_user_turn=True,  # {_HOOK_MARKER}: interactive CLI user turn.",
        max_lines=12,
        label="cli.py user-turn call",
    )
    source = _insert_after_line_in_unique_block(
        source,
        block_start="result = self.agent.run_conversation(",
        expected=f"is_user_turn=True,  # {_HOOK_MARKER}: interactive CLI user turn.",
        addition="working_directory=_forge_working_directory,",
        max_lines=14,
        label="cli.py trusted working directory",
    )
    source = _insert_after_line_in_unique_block(
        source,
        block_start="result = self.agent.run_conversation(",
        expected="working_directory=_forge_working_directory,",
        addition="trusted_turn_context=_forge_trusted_turn_context,",
        max_lines=16,
        label="cli.py trusted source event context",
    )
    source = _insert_after_line_in_unique_block(
        source,
        block_start="result = self.agent.run_conversation(",
        expected=")",
        addition=(
            "result = self._continue_choice_modal_result(",
            "    result,",
            "    conversation_history=self.conversation_history[:-1],",
            "    stream_callback=stream_callback,",
            "    task_id=self.session_id,",
            "    moa_config=_moa_cfg,",
            "    working_directory=_forge_working_directory,",
            "    trusted_turn_context=_forge_trusted_turn_context,",
            ")",
        ),
        max_lines=14,
        label="cli.py chooser continuation",
    )
    source = _insert_before_unique_line(
        source,
        "def _prompt_text_input_modal(",
        tuple(_CLI_CHOICE_METHODS.strip("\n").splitlines()) + ("",),
        label="cli.py generic chooser methods",
    )
    source = _insert_before_function_return_after_anchor(
        source,
        anchor='response = result.get("final_response", "") if result else ""',
        additions=(
            "if (",
            "    _forge_source_outbox is not None",
            "    and _forge_source_event_id",
            "    and isinstance(result, dict)",
            "    and not result.get(\"failed\")",
            "    and not result.get(\"partial\")",
            "):",
            "    _forge_source_outbox.acknowledge(_forge_source_event_id)",
        ),
        max_lines=260,
        label="cli.py source event acknowledgement",
    )
    source = _insert_after_line_in_unique_block(
        source,
        block_start="def _prompt_text_input_modal(",
        expected="timeout: float = 120,",
        addition="_choice_modal_state: dict | None = None,",
        max_lines=12,
        label="cli.py modal state argument",
    )
    source = _insert_after_line_in_unique_block(
        source,
        block_start="def _prompt_text_input_modal(",
        expected="response_queue = queue.Queue()",
        addition="_choice_modal_state = dict(_choice_modal_state or {})",
        max_lines=90,
        label="cli.py modal state preparation",
    )
    source = _insert_after_line_in_unique_block(
        source,
        block_start="def _setup_modal() -> None:",
        expected='"response_queue": response_queue,',
        addition="**_choice_modal_state,",
        max_lines=18,
        label="cli.py modal state forwarding",
    )
    source = _insert_after_line_in_unique_block(
        source,
        block_start="# --- Slash-command confirmation: submit typed or highlighted choice ---",
        expected="if self._slash_confirm_state:",
        addition=(
            '    if self._slash_confirm_state.get("structured_choice_modal"):',
            "        submitted = self._submit_choice_modal_selection()",
            "        if submitted:",
            "            event.app.current_buffer.reset()",
            "        event.app.invalidate()",
            "        return",
        ),
        max_lines=3,
        label="cli.py chooser Enter handler",
    )
    source = _insert_before_unique_line(
        source,
        "# --- Slash-command confirmation: arrow-key navigation ---",
        (
            "# --- Generic multiple chooser: Space toggles the highlighted ID. ---",
            "@kb.add(' ', filter=Condition(lambda: bool(self._slash_confirm_state) and self._slash_confirm_state.get(\"choice_mode\") == \"multiple\"))",
            "def choice_modal_toggle(event):",
            "    self._toggle_choice_modal_selection()",
            "    event.app.invalidate()",
            "",
        ),
        label="cli.py chooser Space handler",
    )
    source = _insert_after_line_in_unique_block(
        source,
        block_start="def _make_slash_confirm_number_handler(idx):",
        expected="def handler(event):",
        addition=(
            '    if self._slash_confirm_state and self._slash_confirm_state.get("structured_choice_modal"):',
            '        if self._slash_confirm_state.get("choice_mode") != "single":',
            "            return",
            '        if idx < len(self._slash_confirm_state.get("choices") or []):',
            '            self._slash_confirm_state["selected"] = idx',
            "            self._submit_choice_modal_selection()",
            "            event.app.current_buffer.reset()",
            "            event.app.invalidate()",
            "        return",
        ),
        max_lines=8,
        label="cli.py chooser number handler",
    )
    source = _insert_after_line_in_unique_block(
        source,
        block_start="def handle_escape_modal(event):",
        expected="if self._slash_confirm_state:",
        addition=(
            '    if self._slash_confirm_state.get("structured_choice_modal"):',
            "        self._submit_slash_confirm_response(None)",
            "        event.app.current_buffer.reset()",
            "        event.app.invalidate()",
            "        return",
        ),
        max_lines=22,
        label="cli.py chooser Escape handler",
    )
    source = _insert_after_line_in_unique_block(
        source,
        block_start="def handle_ctrl_c(event):",
        expected="if self._slash_confirm_state:",
        addition=(
            '    # RISK(breaking): upstream Ctrl+C uses "cancel", which can be a stable choice ID.',
            '    if self._slash_confirm_state.get("structured_choice_modal"):',
            "        self._submit_slash_confirm_response(None)",
            "        event.app.current_buffer.reset()",
            "        event.app.invalidate()",
            "        return",
        ),
        max_lines=90,
        label="cli.py chooser Ctrl+C handler",
    )
    source = _insert_after_line_in_unique_block(
        source,
        block_start="def _get_slash_confirm_display_fragments(self):",
        expected='selected = state.get("selected", 0)',
        addition=(
            'choice_mode = state.get("choice_mode", "single")',
            'selected_ids = state.get("selected_ids") or set()',
            "instructions = (",
            "    self._choice_modal_instructions(state)",
            '    if state.get("structured_choice_modal")',
            '    else "Type 1/2/3 or use ↑/↓ then Enter. ESC/Ctrl+C cancels."',
            ")",
        ),
        max_lines=18,
        label="cli.py chooser display state",
    )
    marker_line = '            marker = "❯" if idx == selected else " "'
    if source.count(marker_line) != 2:
        raise InstallError("cli.py chooser marker anchors are not unique")
    source = source.replace(
        marker_line,
        '            marker = (("❯" if idx == selected else " ") + (" [x]" if _value in selected_ids else " [ ]")) if choice_mode == "multiple" else ("❯" if idx == selected else " ")',
    )
    source = _replace_unique_line(
        source,
        'preview_lines.append("Type 1/2/3 or use ↑/↓ then Enter. ESC/Ctrl+C cancels.")',
        "preview_lines.append(instructions)",
        label="cli.py chooser preview instructions",
    )
    source = _replace_unique_line(
        source,
        "_append_panel_line(lines, 'class:approval-border', 'class:approval-cmd', 'Type 1/2/3 or use ↑/↓ then Enter. ESC/Ctrl+C cancels.', box_width)",
        "_append_panel_line(lines, 'class:approval-border', 'class:approval-cmd', instructions, box_width)",
        label="cli.py chooser footer instructions",
    )
    source = _replace_unique_line(
        source,
        'if response and result and not result.get("failed") and not result.get("partial"):',
        'if response and result and not result.get("failed") and not result.get("partial") and not result.get("handled"):',
        label="cli.py handled title guard",
    )
    return _insert_after_unique_line(
        source,
        'response = result.get("final_response", "") if result else ""',
        _choice_display_lines("result", "response"),
        label="cli.py chooser display",
    )


def change_tui_gateway_source(source: str) -> str:
    """Mark Desktop/TUI input and suppress handled-turn goal and title models."""

    if _HOOK_MARKER in source:
        raise InstallError("tui_gateway/server.py user-turn handling is already installed")
    source = _insert_before_unique_line(
        source,
        '@method("prompt.submit")',
        (
            "def _forge_source_session_id(session: dict, sid: str) -> str:",
            "    # RISK(security): only the server DB may collapse a compression tip to its lineage root.",
            "    session_key = str(session.get(\"session_key\") or sid)",
            "    try:",
            "        database = _get_db()",
            "        row = database.get_session(session_key) if database is not None else None",
            "    except (AttributeError, NameError, TypeError):",
            "        row = None",
            "    lineage_root = row.get(\"_lineage_root_id\") if isinstance(row, dict) else None",
            "    return str(lineage_root or session_key)",
            "",
            "",
        ),
        label="TUI server-authenticated source session identity",
    )
    source = _insert_after_guard_in_unique_block(
        source,
        block_start='@method("prompt.submit")',
        guard="if err:",
        terminal="return err",
        addition=(
            '_forge_source_event_id = params.get("source_event_id")',
            "if _forge_source_event_id is None:",
            '    _forge_source_event_id = ""',
            "elif (",
            "    not isinstance(_forge_source_event_id, str)",
            "    or not _forge_source_event_id.strip()",
            "    or _forge_source_event_id != _forge_source_event_id.strip()",
            "    or any(ord(_forge_character) < 32 for _forge_character in _forge_source_event_id)",
            "    or len(_forge_source_event_id) > 512",
            "):",
            "    return _err(rid, 4004, \"source_event_id is required\")",
            'session["_infinity_forge_source_event_id"] = _forge_source_event_id',
        ),
        max_lines=12,
        label="TUI source event validation",
    )
    if "def _run_prompt_submit(" in source:
        source = _replace_unique_line(
            source,
            "def _run_prompt_submit(rid, sid: str, session: dict, text: Any) -> None:",
            "def _run_prompt_submit(rid, sid: str, session: dict, text: Any, source_event_id: str | None = None) -> None:",
            label="TUI source event turn parameter",
        )
        source = _replace_unique_line(
            source,
            "def _enqueue_prompt(session: dict, text: Any, transport: Any) -> None:",
            "def _enqueue_prompt(session: dict, text: Any, transport: Any, source_event_id: str | None = None) -> None:",
            label="TUI queued source event parameter",
        )
        source = _insert_after_line_in_unique_block(
            source,
            block_start=(
                "def _enqueue_prompt(session: dict, text: Any, transport: Any, "
                "source_event_id: str | None = None) -> None:"
            ),
            expected='text = f"{prev}\\n\\n{text}" if prev and text else (prev or text)',
            addition=(
                "# Multiple platform events merged into one prompt have no single",
                "# authenticated event identity, so mutating Forge tools fail closed.",
                "source_event_id = None",
            ),
            max_lines=24,
            label="TUI merged event fail closed",
        )
        source = _replace_unique_line(
            source,
            'session["queued_prompt"] = {"text": text, "transport": transport}',
            'session["queued_prompt"] = {"text": text, "transport": transport, "source_event_id": source_event_id}',
            label="TUI queued source event storage",
        )
        source = _replace_unique_line(
            source,
            "def _handle_busy_submit(rid, sid: str, session: dict, text: Any, transport: Any) -> dict:",
            "def _handle_busy_submit(rid, sid: str, session: dict, text: Any, transport: Any, source_event_id: str | None = None) -> dict:",
            label="TUI busy source event parameter",
        )
        source = _insert_after_line_in_unique_block(
            source,
            block_start="def _handle_busy_submit(rid, sid: str, session: dict, text: Any, transport: Any, source_event_id: str | None = None) -> dict:",
            expected="if agent.steer(text):",
            addition=(
                "    # A successful steer becomes the authenticated input for the",
                "    # next model iteration; never retain the preceding event ID.",
                "    _forge_steer_context = getattr(agent, \"_infinity_forge_trusted_turn_context\", {})",
                "    _forge_steer_context = dict(_forge_steer_context) if isinstance(_forge_steer_context, dict) else {}",
                "    _forge_steer_context[\"source_event_id\"] = source_event_id or \"\"",
                "    setattr(agent, \"_infinity_forge_trusted_turn_context\", _forge_steer_context)",
                "    session[\"_infinity_forge_source_event_id\"] = source_event_id or \"\"",
            ),
            max_lines=28,
            label="TUI steered source event binding",
        )
        source = _replace_unique_line(
            source,
            "_enqueue_prompt(session, text, transport)",
            "_enqueue_prompt(session, text, transport, source_event_id)",
            label="TUI busy source event queue",
        )
        source = _replace_unique_line(
            source,
            '_run_prompt_submit(rid, sid, session, queued["text"])',
            '_run_prompt_submit(rid, sid, session, queued["text"], queued.get("source_event_id"))',
            label="TUI queued source event dispatch",
        )
        source = _replace_unique_line(
            source,
            "return _handle_busy_submit(rid, sid, session, text, t or session.get(\"transport\"))",
            "return _handle_busy_submit(rid, sid, session, text, t or session.get(\"transport\"), _forge_source_event_id)",
            label="TUI busy source event capture",
        )
        run_after_start = source.find("def run_after_agent_ready()")
        run_after_end = source.find("run_thread = threading.Thread", run_after_start)
        if run_after_start < 0 or run_after_end < 0:
            raise InstallError("TUI prompt submit runner block was not found")
        runner_block = source[run_after_start:run_after_end]
        expected_call = "_run_prompt_submit(rid, sid, session, text)"
        if runner_block.count(expected_call) != 1:
            raise InstallError("TUI prompt submit source event call is not unique")
        runner_block = runner_block.replace(
            expected_call,
            "_run_prompt_submit(rid, sid, session, text, _forge_source_event_id)",
            1,
        )
        source = source[:run_after_start] + runner_block + source[run_after_end:]
    source = _insert_after_unique_line(
        source,
        '"stream_callback": _stream,',
        (
            f"# {_HOOK_MARKER}: Desktop/TUI submitted this user turn.",
            '"is_user_turn": True,',
        ),
        label="tui gateway user-turn call",
    )
    source = _insert_after_unique_line(
        source,
        '"is_user_turn": True,',
        (
            '"working_directory": session.get("cwd"),',
            '"trusted_turn_context": {',
            '    "owner_host": __import__("os").environ.get("INFINITY_FORGE_HOST_ID", ""),',
            '    "subject_id": __import__("os").environ.get("HERMES_USER_ID", "local-user"),',
            '    "session_id": _forge_source_session_id(session, sid),',
            '    "surface": "tui",',
            '    "source_event_id": (source_event_id if "source_event_id" in locals() else session.get("_infinity_forge_source_event_id", "")),',
            '    "working_directory": session.get("cwd"),',
            "},",
        ),
        label="TUI trusted source event context",
    )
    source = _replace_unique_line(
        source,
        'if status == "complete" and isinstance(raw, str) and raw.strip():',
        'if status == "complete" and isinstance(raw, str) and raw.strip() and not (isinstance(result, dict) and result.get("handled")):',
        label="tui gateway handled goal guard",
    )
    source = _insert_in_unique_sequence(
        source,
        (
            "if (",
            'status == "complete"',
            "and isinstance(raw, str)",
            "and raw.strip()",
            "and isinstance(text, str)",
            "and text.strip()",
            "):",
        ),
        after=1,
        addition='and not (isinstance(result, dict) and result.get("handled"))',
        label="tui gateway handled title guard",
    )
    source = _insert_after_unique_line(
        source,
        "goal_followup = None  # set by the post-turn goal hook below"
        if "goal_followup = None  # set by the post-turn goal hook below" in source
        else "goal_followup = None",
        ("_choice_completion_payload = None",),
        label="tui gateway deferred chooser payload",
    )
    source = _insert_after_unique_line(
        source,
        'payload = {"text": raw, "usage": _get_usage(agent), "status": status}',
        (
            "# RISK(breaking): chooser clients require the complete prompt contract, not labels alone.",
            "_choice_prompt_fields = (",
            '    "choice_prompt_id", "choice_mode", "min_choices", "max_choices",',
            '    "submit_label", "expires_at", "choices",',
            ")",
            "if isinstance(result, dict) and _is_valid_gateway_choice_prompt(result):",
            "    for _choice_field in _choice_prompt_fields:",
            "        payload[_choice_field] = copy.deepcopy(result[_choice_field])",
        ),
        label="tui gateway chooser payload",
    )
    source = _insert_after_unique_line(
        source,
        'payload = {"text": raw, "usage": _get_usage(agent), "status": status}',
        (
            '_forge_completed_source_event_id = (source_event_id if "source_event_id" in locals() else session.get("_infinity_forge_source_event_id", ""))',
            "if _forge_completed_source_event_id:",
            '    payload["source_event_id"] = _forge_completed_source_event_id',
        ),
        label="TUI source event acknowledgement payload",
    )
    source = _insert_before_unique_line(
        source,
        '_emit("message.complete", sid, payload)',
        (
            "if _is_valid_gateway_choice_prompt(payload):",
            "    _choice_completion_payload = copy.deepcopy(payload)",
            "if _choice_completion_payload is None:",
        ),
        label="tui gateway normal completion guard",
    )
    source = _replace_unique_line(
        source,
        '_emit("message.complete", sid, payload)',
        '    _emit("message.complete", sid, payload)',
        label="tui gateway deferred chooser completion",
    )
    source = _replace_unique_sequence(
        source,
        (
            'with session["history_lock"]:',
            'session["running"] = False',
            'session["last_active"] = time.time()',
            "_clear_inflight_turn(session)",
            '_emit("session.info", sid, _session_info(agent, session))',
        ),
        (
            "_finalize_gateway_choice_turn(sid, session, _choice_completion_payload)",
            '_emit("session.info", sid, _session_info(agent, session))',
        ),
        label="tui gateway atomic chooser publication",
    )
    source = _insert_after_line_in_unique_block(
        source,
        block_start='@method("prompt.submit")',
        expected='with session["history_lock"]:',
        addition=(
            "    # A normal turn invalidates any chooser the prior turn published.",
            '    session.pop("_choice_prompt", None)',
        ),
        max_lines=48,
        label="tui gateway normal-turn chooser invalidation",
    )

    rpc_anchor = '@method("prompt.submit")'
    if source.count(rpc_anchor) != 1:
        raise InstallError("tui_gateway/server.py prompt.submit anchor is not unique")
    chooser_rpc = '''def _is_valid_gateway_choice_prompt(prompt: Any) -> bool:
    if not isinstance(prompt, dict):
        return False
    required = {
        "choice_prompt_id", "choice_mode", "min_choices", "max_choices",
        "submit_label", "expires_at", "choices",
    }
    if not required.issubset(prompt):
        return False
    try:
        prompt_id = str(uuid.UUID(prompt["choice_prompt_id"]))
    except (ValueError, TypeError, AttributeError):
        return False
    if prompt_id != prompt["choice_prompt_id"] or prompt["choice_mode"] not in {"single", "multiple"}:
        return False
    choices = prompt["choices"]
    if not isinstance(choices, list) or not choices:
        return False
    if any(
        not isinstance(choice, dict)
        or set(choice) != {"id", "label", "description"}
        or any(not isinstance(choice[key], str) or not choice[key].strip() for key in choice)
        for choice in choices
    ):
        return False
    ids = [choice["id"] for choice in choices]
    if len(ids) != len(set(ids)):
        return False
    minimum = prompt["min_choices"]
    maximum = prompt["max_choices"]
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 1:
        return False
    if maximum is not None and (
        not isinstance(maximum, int)
        or isinstance(maximum, bool)
        or maximum < minimum
        or maximum > len(choices)
    ):
        return False
    if prompt["choice_mode"] == "single" and (minimum != 1 or maximum != 1):
        return False
    if not isinstance(prompt["submit_label"], str) or not prompt["submit_label"].strip():
        return False
    try:
        expires = datetime.fromisoformat(prompt["expires_at"].replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        return False
    return expires.tzinfo is not None


def _claim_gateway_choice_submission(session: dict, params: dict) -> tuple[dict | None, str | None]:
    prompt = session.get("_choice_prompt")
    if not _is_valid_gateway_choice_prompt(prompt):
        return None, "no pending choice prompt"
    if set(params) != {"session_id", "choice_prompt_id", "selected_choice_ids"}:
        return None, "choice submission has invalid fields"
    prompt_id = params.get("choice_prompt_id")
    selected_ids = params.get("selected_choice_ids")
    if prompt_id != prompt["choice_prompt_id"]:
        return None, "choice prompt is stale"
    if not isinstance(selected_ids, list) or any(
        not isinstance(choice_id, str) or not choice_id.strip() for choice_id in selected_ids
    ):
        return None, "selected_choice_ids must be an array of IDs"
    if len(selected_ids) != len(set(selected_ids)):
        return None, "selected_choice_ids contains duplicates"
    allowed = {choice["id"] for choice in prompt["choices"]}
    if any(choice_id not in allowed for choice_id in selected_ids):
        return None, "selected_choice_ids contains an unknown ID"
    minimum = prompt["min_choices"]
    maximum = prompt["max_choices"]
    if len(selected_ids) < minimum or (maximum is not None and len(selected_ids) > maximum):
        return None, "selected_choice_ids violates prompt bounds"
    expires = datetime.fromisoformat(prompt["expires_at"].replace("Z", "+00:00"))
    if datetime.now(expires.tzinfo) >= expires:
        session.pop("_choice_prompt", None)
        return None, "choice prompt expired"
    # RISK(race): caller holds history_lock; pop makes duplicate clicks fail closed.
    session.pop("_choice_prompt", None)
    return {
        "choice_prompt_id": prompt_id,
        "selected_choice_ids": selected_ids,
    }, None


def _finalize_gateway_choice_turn(sid: str, session: dict, payload: dict | None) -> None:
    with session["history_lock"]:
        session["running"] = False
        session["last_active"] = time.time()
        _clear_inflight_turn(session)
        if payload is None:
            return
        fields = (
            "choice_prompt_id", "choice_mode", "min_choices", "max_choices",
            "submit_label", "expires_at", "choices",
        )
        delivery_transport = session.get("transport") or _stdio_transport
        prompt = {field: copy.deepcopy(payload[field]) for field in fields}
        # RISK(race): owner, prompt generation, and synchronous delivery are one
        # lock-protected publication; prompt.submit cannot invalidate between them.
        prompt["_owner_transport"] = delivery_transport
        session["_choice_prompt"] = prompt
        frame = {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {"type": "message.complete", "session_id": sid, "payload": payload},
        }
        try:
            delivered = delivery_transport.write(frame)
        except Exception as error:
            delivered = False
            print(f"[tui_gateway] chooser publication failed: {error}", file=sys.stderr)
        if delivered is False:
            current = session.get("_choice_prompt")
            if current is prompt:
                session.pop("_choice_prompt", None)


@method("choice.submit")
def _(rid, params: dict) -> dict:
    sid = params.get("session_id", "")
    session, err = _sess_nowait(params, rid)
    if err:
        return err
    request_transport = current_transport() or _stdio_transport
    with session["history_lock"]:
        prompt = session.get("_choice_prompt")
        if not isinstance(prompt, dict) or prompt.get("_owner_transport") is not request_transport:
            return _err(rid, 4008, "choice prompt belongs to another connection")
        if session.get("running"):
            return _err(rid, 4009, "session busy")
        submission, claim_error = _claim_gateway_choice_submission(session, params)
        if claim_error:
            return _err(rid, 4008, claim_error)
        # Rebind only after the transport-bound prompt has been claimed.
        session["transport"] = request_transport
        session["running"] = True
        session["_turn_cancel_requested"] = False
        session["last_active"] = time.time()
        _start_inflight_turn(session, submission)
    _ensure_session_db_row(session)
    _persist_branch_seed(session)
    _start_agent_build(sid, session)

    def run_after_agent_ready() -> None:
        wait_error = _wait_agent(session, rid)
        if wait_error:
            _emit("error", sid, {"message": wait_error.get("error", {}).get("message", "agent initialization failed")})
            with session["history_lock"]:
                session["running"] = False
                _clear_inflight_turn(session)
            return
        _run_prompt_submit(rid, sid, session, submission)

    run_thread = threading.Thread(target=run_after_agent_ready, daemon=True)
    session["_run_thread"] = run_thread
    run_thread.start()
    return _ok(rid, {"status": "streaming"})


'''
    live_session_anchor = (
        '"session_key": _session_lookup_key(session, fallback=sid),'
    )
    if live_session_anchor in source:
        source = _insert_after_unique_line(
            source,
            live_session_anchor,
            ('"source_event_session_id": _forge_source_session_id(session, sid),',),
            label="TUI live source session response",
        )
    elif "def _live_session_payload(" in source:
        raise InstallError("TUI live source session payload anchor is missing")
    return source.replace(rpc_anchor, chooser_rpc + rpc_anchor)


def change_tui_gateway_types_source(source: str) -> str:
    """Carry the complete chooser event and structured submission types."""

    source = _insert_after_line_in_unique_block(
        source,
        block_start="export interface SessionCreateResponse {",
        expected="session_id: string",
        addition="stored_session_id?: string",
        max_lines=5,
        label="TUI durable session response type",
    )
    if "export interface SessionActivateResponse {" in source:
        source = _insert_after_line_in_unique_block(
            source,
            block_start="export interface SessionActivateResponse {",
            expected="session_id: string",
            addition="source_event_session_id?: string",
            max_lines=11,
            label="TUI active lineage response type",
        )
    source = _insert_before_unique_line(
        source,
        "export interface PromptSubmitResponse {",
        (
            "export interface GatewayChoice {",
            "  description: string",
            "  id: string",
            "  label: string",
            "}",
            "",
            "export interface GatewayChoicePrompt {",
            "  choice_mode: 'multiple' | 'single'",
            "  choice_prompt_id: string",
            "  choices: GatewayChoice[]",
            "  expires_at: string",
            "  max_choices: null | number",
            "  min_choices: number",
            "  submit_label: string",
            "}",
            "",
            "export interface ChoiceSubmitResponse {",
            "  ok?: boolean",
            "  status?: string",
            "}",
            "",
            "export interface ChoiceSubmitParams {",
            "  choice_prompt_id: string",
            "  selected_choice_ids: string[]",
            "  session_id: string",
            "}",
            "",
        ),
        label="TUI chooser gateway types",
    )
    old = "payload?: { reasoning?: string; rendered?: string; text?: string; usage?: Usage }"
    new = "payload?: Partial<GatewayChoicePrompt> & { reasoning?: string; rendered?: string; source_event_id?: string; text?: string; usage?: Usage }"
    return _replace_unique_line(source, old, new, label="TUI chooser message.complete type")


def change_tui_overlay_store_source(source: str) -> str:
    """Add a session-keyed chooser store without conflating clarify state."""

    source = _insert_after_unique_line(
        source,
        "import type { OverlayState } from './interfaces.js'",
        (
            "import type { GatewayChoicePrompt } from '../gatewayTypes.js'",
            "import { $uiSessionId } from './uiStore.js'",
            "",
            "const CHOICE_PROMPT_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/",
            "",
            "export const $choicePrompts = atom<Record<string, GatewayChoicePrompt>>({})",
            "",
            "export function isGatewayChoicePrompt(value: unknown): value is GatewayChoicePrompt {",
            "  if (!value || typeof value !== 'object' || Array.isArray(value)) return false",
            "  const prompt = value as Partial<GatewayChoicePrompt>",
            "  const choices = prompt.choices",
            "  const minimum = prompt.min_choices",
            "  const maximum = prompt.max_choices",
            "  if (!CHOICE_PROMPT_ID_RE.test(prompt.choice_prompt_id ?? '') || !Array.isArray(choices) || !choices.length) return false",
            "  if (prompt.choice_mode !== 'single' && prompt.choice_mode !== 'multiple') return false",
            "  if (typeof minimum !== 'number' || !Number.isInteger(minimum) || minimum < 1) return false",
            "  if (maximum !== null && (typeof maximum !== 'number' || !Number.isInteger(maximum) || maximum < minimum)) return false",
            "  if (prompt.choice_mode === 'single' && (minimum !== 1 || maximum !== 1)) return false",
            "  if (typeof prompt.submit_label !== 'string' || !prompt.submit_label.trim() || !Number.isFinite(Date.parse(prompt.expires_at ?? ''))) return false",
            "  const ids = new Set<string>()",
            "  for (const choice of choices) {",
            "    if (!choice || typeof choice.id !== 'string' || !choice.id.trim() || typeof choice.label !== 'string' || !choice.label.trim() || typeof choice.description !== 'string' || !choice.description.trim() || ids.has(choice.id)) return false",
            "    ids.add(choice.id)",
            "  }",
            "  return (maximum === null || maximum <= choices.length) && minimum <= choices.length",
            "}",
            "",
            "export function setChoicePrompt(sessionId: string, value: unknown): boolean {",
            "  if (!sessionId || !isGatewayChoicePrompt(value) || Date.parse(value.expires_at) <= Date.now()) return false",
            "  $choicePrompts.set({ ...$choicePrompts.get(), [sessionId]: value })",
            "  return true",
            "}",
            "",
            "export function clearChoicePrompt(sessionId: null | string | undefined, promptId?: string): void {",
            "  if (!sessionId) return",
            "  const current = $choicePrompts.get()[sessionId]",
            "  if (!current || (promptId && current.choice_prompt_id !== promptId)) return",
            "  const next = { ...$choicePrompts.get() }",
            "  delete next[sessionId]",
            "  $choicePrompts.set(next)",
            "}",
            "",
            "export const resetChoicePrompts = () => $choicePrompts.set({})",
        ),
        label="TUI session chooser store",
    )
    actual = '''export const $isBlocked = computed(
  $overlayState,
  ({
    agents,
    approval,
    billing,
    clarify,
    confirm,
    journey,
    modelPicker,
    pager,
    petPicker,
    pluginsHub,
    secret,
    sessions,
    skillsHub,
    sudo
  }) =>
    Boolean(
      agents ||
      approval ||
      billing ||
      clarify ||
      confirm ||
      journey ||
      modelPicker ||
      pager ||
      petPicker ||
      pluginsHub ||
      secret ||
      sessions ||
      skillsHub ||
      sudo
    )
)'''
    replacement = '''export const $isBlocked = computed(
  [$overlayState, $choicePrompts, $uiSessionId],
  (overlay, choices, sessionId) =>
    Boolean(
      (sessionId && choices[sessionId]) ||
      overlay.agents || overlay.approval || overlay.billing || overlay.clarify ||
      overlay.confirm || overlay.journey || overlay.modelPicker || overlay.pager ||
      overlay.petPicker || overlay.pluginsHub || overlay.secret || overlay.sessions ||
      overlay.skillsHub || overlay.sudo
    )
)'''
    fixture = "export const $isBlocked = computed($overlayState, overlay => Boolean(overlay.clarify))"
    newline = "\r\n" if "\r\n" in source else "\n"
    normalized = source.replace("\r\n", "\n")
    if normalized.count(actual) == 1:
        native_actual = actual.replace("\n", newline)
        native_replacement = replacement.replace("\n", newline)
        return source.replace(native_actual, native_replacement, 1)
    if source.count(fixture) == 1:
        return source.replace(fixture, replacement)
    raise InstallError("TUI blocked-state chooser anchor is not unique")


def change_tui_event_handler_source(source: str) -> str:
    source = _replace_unique_line(
        source,
        "import { getOverlayState, patchOverlayState } from './overlayStore.js'",
        "import { clearChoicePrompt, getOverlayState, patchOverlayState, setChoicePrompt } from './overlayStore.js'",
        label="TUI chooser event store import",
    )
    source = _insert_after_unique_line(
        source,
        "import { clearChoicePrompt, getOverlayState, patchOverlayState, setChoicePrompt } from './overlayStore.js'",
        ("import { acknowledgeSourceEvent } from './submissionCore.js'",),
        label="TUI source event acknowledgement import",
    )
    return _insert_after_unique_line(
        source,
        "const sid = getUiState().sid",
        (
            "",
            "// Capture background-session prompts before the active-session event gate.",
            "if (ev.type === 'message.start' && ev.session_id) clearChoicePrompt(ev.session_id)",
            "if (ev.type === 'message.complete' && typeof ev.payload?.source_event_id === 'string') acknowledgeSourceEvent(ev.payload.source_event_id)",
            "if (ev.type === 'message.complete' && ev.session_id) setChoicePrompt(ev.session_id, ev.payload)",
        ),
        label="TUI chooser event capture",
    )


def change_tui_prompts_source(source: str) -> str:
    source = _replace_unique_line(
        source,
        "import { useState } from 'react'",
        "import { useEffect, useState } from 'react'",
        label="TUI chooser expiry hook import",
    )
    source = _insert_after_unique_line(
        source,
        "import { useEffect, useState } from 'react'",
        (
            "import { useGateway } from '../app/gatewayContext.js'",
            "import { clearChoicePrompt } from '../app/overlayStore.js'",
            "import type { ChoiceSubmitResponse, GatewayChoicePrompt } from '../gatewayTypes.js'",
        ),
        label="TUI chooser prompt imports",
    )
    return source.rstrip() + '''


type ChoiceKey = { downArrow?: boolean; escape?: boolean; return?: boolean; upArrow?: boolean }
export type ChoiceAction =
  | { kind: 'cancel' }
  | { cursor: number; kind: 'state'; selected: string[] }
  | { error?: string; ids?: string[]; kind: 'submit' }
  | { kind: 'noop' }

export function choiceSubmitErrorDisposition(reason: unknown): { clearPrompt: boolean; message: string } {
  const error = reason as { code?: unknown; message?: unknown } | null
  const code = typeof error?.code === 'number' ? error.code : null
  const message = typeof error?.message === 'string' ? error.message : String(reason)
  if (code === 4009 || message === 'session busy') return { clearPrompt: false, message }
  const terminal = ['belongs to another connection', 'no pending choice prompt', 'choice prompt is stale', 'choice prompt expired']
  return { clearPrompt: terminal.some(fragment => message.includes(fragment)), message }
}

export function choiceAction(
  prompt: GatewayChoicePrompt,
  cursor: null | number,
  selected: readonly string[],
  ch: string,
  key: ChoiceKey
): ChoiceAction {
  if (key.escape) return { kind: 'cancel' }
  if (key.upArrow || key.downArrow) {
    const delta = key.upArrow ? -1 : 1
    const start = cursor === null ? (delta > 0 ? 0 : prompt.choices.length - 1) : cursor + delta
    return { cursor: Math.max(0, Math.min(prompt.choices.length - 1, start)), kind: 'state', selected: [...selected] }
  }
  if (ch === ' ' && prompt.choice_mode === 'multiple' && cursor !== null) {
    const id = prompt.choices[cursor]?.id
    if (!id) return { kind: 'noop' }
    const next = selected.includes(id) ? selected.filter(value => value !== id) : [...selected, id]
    if (prompt.max_choices !== null && next.length > prompt.max_choices) return { error: `Choose at most ${prompt.max_choices}.`, kind: 'submit' }
    return { cursor, kind: 'state', selected: next }
  }
  if (!key.return) return { kind: 'noop' }
  const ids = prompt.choice_mode === 'single' && cursor !== null ? [prompt.choices[cursor]!.id] : [...selected]
  if (ids.length < prompt.min_choices) return { error: `Choose at least ${prompt.min_choices}.`, kind: 'submit' }
  if (prompt.max_choices !== null && ids.length > prompt.max_choices) return { error: `Choose at most ${prompt.max_choices}.`, kind: 'submit' }
  return { ids, kind: 'submit' }
}

export function ChoicePrompt({ prompt, sessionId, t }: { prompt: GatewayChoicePrompt; sessionId: string; t: Theme }) {
  const { gw } = useGateway()
  const [cursor, setCursor] = useState<null | number>(null)
  const [selected, setSelected] = useState<string[]>([])
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    const delay = Date.parse(prompt.expires_at) - Date.now()
    if (delay <= 0) {
      clearChoicePrompt(sessionId, prompt.choice_prompt_id)
      return
    }
    const timer = setTimeout(() => clearChoicePrompt(sessionId, prompt.choice_prompt_id), delay)
    return () => clearTimeout(timer)
  }, [prompt.choice_prompt_id, prompt.expires_at, sessionId])

  useInput((ch, key) => {
    if (submitting) return
    const action = choiceAction(prompt, cursor, selected, ch, key)
    if (action.kind === 'cancel') {
      clearChoicePrompt(sessionId, prompt.choice_prompt_id)
    } else if (action.kind === 'state') {
      setCursor(action.cursor)
      setSelected(action.selected)
      setError('')
    } else if (action.kind === 'submit') {
      if (!action.ids) {
        setError(action.error ?? 'Choose an option.')
        return
      }
      setSubmitting(true)
      gw.request<ChoiceSubmitResponse>('choice.submit', {
        choice_prompt_id: prompt.choice_prompt_id,
        selected_choice_ids: action.ids,
        session_id: sessionId
      }).then(() => clearChoicePrompt(sessionId, prompt.choice_prompt_id)).catch(reason => {
        const failure = choiceSubmitErrorDisposition(reason)
        setError(failure.message)
        if (failure.clearPrompt) clearChoicePrompt(sessionId, prompt.choice_prompt_id)
        setSubmitting(false)
      })
    }
  })

  return (
    <Box borderColor={t.color.accent} borderStyle="double" flexDirection="column" paddingX={1}>
      {prompt.choices.map((choice, index) => {
        const checked = selected.includes(choice.id)
        return <Text key={choice.id} color={cursor === index ? t.color.label : t.color.muted}>
          {cursor === index ? '▸ ' : '  '}{prompt.choice_mode === 'multiple' ? (checked ? '[x] ' : '[ ] ') : ''}{choice.label} — {choice.description}
        </Text>
      })}
      {error ? <Text color={t.color.error}>{error}</Text> : null}
      <Text color={t.color.muted}>{prompt.choice_mode === 'multiple' ? '↑/↓ move · Space toggle · Enter submit' : '↑/↓ select · Enter submit'} · Esc cancel</Text>
    </Box>
  )
}
'''


def change_tui_app_overlays_source(source: str) -> str:
    source = _replace_unique_line(
        source,
        "import { $overlayState, patchOverlayState } from '../app/overlayStore.js'",
        "import { $choicePrompts, $overlayState, patchOverlayState } from '../app/overlayStore.js'",
        label="TUI chooser overlay store import",
    )
    source = _replace_unique_line(
        source,
        "import { ApprovalPrompt, ClarifyPrompt, ConfirmPrompt } from './prompts.js'"
        if "ClarifyPrompt" in source
        else "import { ApprovalPrompt } from './prompts.js'",
        "import { ApprovalPrompt, ChoicePrompt, ClarifyPrompt, ConfirmPrompt } from './prompts.js'"
        if "ClarifyPrompt" in source
        else "import { ApprovalPrompt, ChoicePrompt } from './prompts.js'",
        label="TUI chooser component import",
    )
    start = "export function PromptZone({"
    end = "export function FloatingOverlays({"
    if source.count(start) != 1:
        raise InstallError("TUI chooser PromptZone anchor is not unique")
    start_index = source.index(start)
    end_index = source.find(end, start_index)
    if end_index < 0:
        end_index = len(source)
    block = source[start_index:end_index]
    block_lines = block.splitlines(keepends=True)
    matches = [index for index, line in enumerate(block_lines) if line.strip() == "const theme = useStore($uiTheme)"]
    if len(matches) != 1:
        raise InstallError("TUI chooser theme anchor is not unique inside PromptZone")
    theme_line = block_lines[matches[0]]
    newline = "\r\n" if theme_line.endswith("\r\n") else "\n"
    addition = """  const sessionId = useStore($uiSessionId)
  const choicePrompts = useStore($choicePrompts)
  const choicePrompt = sessionId ? choicePrompts[sessionId] : undefined

  if (sessionId && choicePrompt) {
    return <Box flexDirection=\"column\" flexShrink={0} paddingX={1} paddingY={1}><ChoicePrompt key={choicePrompt.choice_prompt_id} prompt={choicePrompt} sessionId={sessionId} t={theme} /></Box>
  }
"""
    block_lines[matches[0] + 1 : matches[0] + 1] = [addition.replace("\n", newline)]
    block = "".join(block_lines)
    source = source[:start_index] + block + source[end_index:]
    return source


_TUI_SOURCE_EVENT_OUTBOX = r'''import { execFileSync } from 'node:child_process'
import { createHash, randomUUID } from 'node:crypto'
import { chmodSync, closeSync, constants, existsSync, fsyncSync, fstatSync, lstatSync, mkdirSync, openSync, readFileSync, readdirSync, realpathSync, renameSync, rmdirSync, statSync, unlinkSync, writeSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join, parse, resolve, sep } from 'node:path'

type ForgePendingSourceEvent = {
  id: string
  payloadHash: string
  sessionId: string
}

type ForgeSourceEventOutbox = {
  format: 'forge-surface-event/v1'
  pending: ForgePendingSourceEvent[]
}

const forgeSourceEventHome = process.env.HERMES_HOME || join(homedir(), '.hermes')
const forgeSourceEventPath = join(
  forgeSourceEventHome,
  'infinity-forge',
  'tui-source-events.json',
)
const forgeSourceEventLockPath = join(forgeSourceEventHome, '.infinity-forge-source-events.lock')
const forgeSourceEventReclaimPath = `${forgeSourceEventLockPath}.reclaim`
const forgeSourceEventLockOwnerName = 'owner.json'
const forgeSourceEventLockTimeoutMs = 30_000

type ForgeSourceEventLockOwner = {
  format: 'forge-source-event-lock/v1'
  nonce: string
  pid: number
  processStartIdentity: string
}

const sleepForSourceEventLock = (): void => {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 10)
}

const pathParts = (path: string): string[] => {
  const absolute = resolve(path)
  const root = parse(absolute).root
  const tail = absolute.slice(root.length).split(sep).filter(Boolean)
  const parts = [root]
  let current = root
  for (const part of tail) {
    current = join(current, part)
    parts.push(current)
  }
  return parts
}

const comparablePath = (path: string): string => {
  const normalized = resolve(path).replace(/^\\\\\?\\/, '')
  return process.platform === 'win32' ? normalized.toLocaleLowerCase('en-US') : normalized
}

const assertSafeSourceEventPath = (path: string): void => {
  for (const part of pathParts(path)) {
    let info
    try {
      info = lstatSync(part)
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') continue
      throw error
    }
    if (info.isSymbolicLink()) {
      throw new Error('Infinity Forge TUI source-event path cannot contain a symbolic link')
    }
    let realPath
    try {
      realPath = realpathSync.native(part)
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') continue
      throw error
    }
    if (comparablePath(realPath) !== comparablePath(part)) {
      throw new Error('Infinity Forge TUI source-event path cannot contain a reparse point')
    }
  }
}

const verifyOwnerOnlyWindows = (path: string): void => {
  const script = [
    '$item = if ($env:FORGE_ACL_DIRECTORY -eq "1") { New-Object System.IO.DirectoryInfo($env:FORGE_ACL_PATH) } else { New-Object System.IO.FileInfo($env:FORGE_ACL_PATH) }',
    '$acl = $item.GetAccessControl()',
    '$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value',
    '$owner = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value',
    '$rules = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))',
    '$full = [System.Security.AccessControl.FileSystemRights]::FullControl',
    '$ok = $acl.AreAccessRulesProtected -and $owner -eq $sid -and $rules.Count -gt 0',
    'foreach ($rule in $rules) { $ok = $ok -and -not $rule.IsInherited -and $rule.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Allow -and $rule.IdentityReference.Value -eq $sid -and (($rule.FileSystemRights -band $full) -eq $full) }',
    'if (-not $ok) { exit 3 }',
  ].join('\n')
  execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], { env: { ...process.env, FORGE_ACL_DIRECTORY: statSync(path).isDirectory() ? '1' : '0', FORGE_ACL_PATH: path }, stdio: 'ignore', windowsHide: true })
}

const restrictOwnerOnlyWindows = (path: string): void => {
  const script = [
    '$path = $env:FORGE_ACL_PATH',
    '$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User',
    '$isDirectory = $env:FORGE_ACL_DIRECTORY -eq "1"',
    '$item = if ($isDirectory) { New-Object System.IO.DirectoryInfo($path) } else { New-Object System.IO.FileInfo($path) }',
    '$acl = $item.GetAccessControl()',
    '$rights = [System.Security.AccessControl.FileSystemRights]::FullControl',
    '$allow = [System.Security.AccessControl.AccessControlType]::Allow',
    '$acl.SetAccessRuleProtection($true, $false)',
    'foreach ($existing in @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))) { [void]$acl.RemoveAccessRuleAll($existing) }',
    'if ($isDirectory) {',
    '  $inheritance = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit',
    '  $rule = [System.Security.AccessControl.FileSystemAccessRule]::new($sid, $rights, $inheritance, [System.Security.AccessControl.PropagationFlags]::None, $allow)',
    '} else {',
    '  $rule = [System.Security.AccessControl.FileSystemAccessRule]::new($sid, $rights, $allow)',
    '}',
    '[void]$acl.AddAccessRule($rule)',
    '$item.SetAccessControl($acl)',
  ].join('\n')
  execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], { env: { ...process.env, FORGE_ACL_DIRECTORY: statSync(path).isDirectory() ? '1' : '0', FORGE_ACL_PATH: path }, stdio: 'ignore', windowsHide: true })
}

const ensureOwnerOnlyPath = (path: string, mode: number): void => {
  let lastError: unknown = null
  for (let attempt = 0; attempt < 20; attempt += 1) {
    try {
      verifyOwnerOnlyPath(path, mode)
      return
    } catch (error) {
      lastError = error
    }
    try {
      if (process.platform === 'win32') restrictOwnerOnlyWindows(path)
      else chmodSync(path, mode)
    } catch (error) {
      lastError = error
    }
    sleepForSourceEventLock()
  }
  throw new Error('Infinity Forge TUI source-event ACL is not owner-only', { cause: lastError })
}

const verifyOwnerOnlyPath = (path: string, mode: number): void => {
  if (process.platform === 'win32') {
    verifyOwnerOnlyWindows(path)
    return
  }
  const info = statSync(path)
  const expectedUid = typeof process.getuid === 'function' ? process.getuid() : info.uid
  if ((info.mode & 0o777) !== mode || info.uid !== expectedUid) {
    throw new Error('Infinity Forge TUI source-event permissions are not owner-only')
  }
}

const ensureSourceEventDirectory = (): void => {
  const directory = dirname(forgeSourceEventPath)
  assertSafeSourceEventPath(directory)
  mkdirSync(directory, { mode: 0o700, recursive: true })
  assertSafeSourceEventPath(directory)
  ensureOwnerOnlyPath(directory, 0o700)
}

const fsyncSourceEventDirectory = (directory = dirname(forgeSourceEventPath)): void => {
  if (process.platform === 'win32') return
  const descriptor = openSync(directory, constants.O_RDONLY)
  try {
    fsyncSync(descriptor)
  } finally {
    closeSync(descriptor)
  }
}

const readProcessStartIdentity = (pid: number): string | null => {
  if (!Number.isSafeInteger(pid) || pid <= 0) {
    throw new Error('Infinity Forge TUI source-event lock PID is invalid')
  }
  if (process.platform === 'win32') {
    const script = [
      `$process = Get-Process -Id ${pid} -ErrorAction SilentlyContinue`,
      "if ($null -eq $process) { Write-Output 'MISSING'; exit 0 }",
      "try { Write-Output ('win:' + $process.StartTime.ToUniversalTime().Ticks) } catch { exit 4 }",
    ].join('\n')
    let output: string
    try {
      output = execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], { encoding: 'utf8', windowsHide: true }).trim()
    } catch (error) {
      throw new Error('Infinity Forge TUI source-event process identity is unavailable', { cause: error })
    }
    if (output === 'MISSING') return null
    if (!/^win:\d+$/.test(output)) throw new Error('Infinity Forge TUI source-event process identity is invalid')
    return output
  }
  if (process.platform === 'linux') {
    let value: string
    try {
      value = readFileSync(`/proc/${pid}/stat`, 'utf8')
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return null
      throw new Error('Infinity Forge TUI source-event process identity is unavailable', { cause: error })
    }
    const closing = value.lastIndexOf(')')
    const fields = closing >= 0 ? value.slice(closing + 2).trim().split(/\s+/) : []
    if (!/^\d+$/.test(fields[19] ?? '')) throw new Error('Infinity Forge TUI source-event process identity is invalid')
    const bootId = readFileSync('/proc/sys/kernel/random/boot_id', 'utf8').trim()
    if (!/^[0-9a-f-]{36}$/i.test(bootId)) throw new Error('Infinity Forge TUI source-event boot identity is invalid')
    return `linux:${bootId}:${fields[19]}`
  }
  try {
    const started = execFileSync('ps', ['-o', 'lstart=', '-p', String(pid)], { encoding: 'utf8' }).trim()
    if (!started || started.length > 128 || [...started].some(character => character.charCodeAt(0) < 32)) {
      throw new Error('invalid process start output')
    }
    return `posix:${started}`
  } catch (error) {
    try {
      process.kill(pid, 0)
    } catch (probeError) {
      if ((probeError as NodeJS.ErrnoException).code === 'ESRCH') return null
    }
    throw new Error('Infinity Forge TUI source-event process identity is unavailable', { cause: error })
  }
}

const forgeSourceEventProcessStartIdentity = readProcessStartIdentity(process.pid)
if (!forgeSourceEventProcessStartIdentity) throw new Error('Infinity Forge TUI source-event process identity is missing')

const sourceEventLockOwner = (): ForgeSourceEventLockOwner => ({
  format: 'forge-source-event-lock/v1',
  nonce: randomUUID(),
  pid: process.pid,
  processStartIdentity: forgeSourceEventProcessStartIdentity,
})

const sameSourceEventLockOwner = (left: ForgeSourceEventLockOwner, right: ForgeSourceEventLockOwner): boolean => (
  left.format === right.format && left.nonce === right.nonce && left.pid === right.pid && left.processStartIdentity === right.processStartIdentity
)

const readSourceEventLockOwner = (directory: string): ForgeSourceEventLockOwner => {
  assertSafeSourceEventPath(directory)
  verifyOwnerOnlyPath(directory, 0o700)
  const entries = readdirSync(directory)
  if (entries.length !== 1 || entries[0] !== forgeSourceEventLockOwnerName) {
    throw new Error('Infinity Forge TUI source-event lock metadata is corrupt')
  }
  const ownerPath = join(directory, forgeSourceEventLockOwnerName)
  assertSafeSourceEventPath(ownerPath)
  verifyOwnerOnlyPath(ownerPath, 0o600)
  const descriptor = openSync(ownerPath, constants.O_RDONLY | (constants.O_NOFOLLOW || 0))
  let value: unknown
  try {
    const opened = fstatSync(descriptor)
    const named = lstatSync(ownerPath)
    if (!opened.isFile() || named.isSymbolicLink() || opened.dev !== named.dev || opened.ino !== named.ino) {
      throw new Error('Infinity Forge TUI source-event lock metadata path changed')
    }
    value = JSON.parse(readFileSync(descriptor, 'utf8'))
  } finally {
    closeSync(descriptor)
  }
  const fields = value && typeof value === 'object' ? Object.keys(value).sort().join(',') : ''
  const owner = value as Partial<ForgeSourceEventLockOwner>
  if (fields !== 'format,nonce,pid,processStartIdentity' || owner.format !== 'forge-source-event-lock/v1' || typeof owner.nonce !== 'string' || !/^[0-9a-f-]{36}$/i.test(owner.nonce) || !Number.isSafeInteger(owner.pid) || (owner.pid ?? 0) <= 0 || typeof owner.processStartIdentity !== 'string' || !owner.processStartIdentity || owner.processStartIdentity.length > 512 || [...owner.processStartIdentity].some(character => character.charCodeAt(0) < 32)) {
    throw new Error('Infinity Forge TUI source-event lock metadata is corrupt')
  }
  return owner as ForgeSourceEventLockOwner
}

const removeSourceEventOwnerDirectory = (directory: string, owner: ForgeSourceEventLockOwner, label: string): void => {
  const confirmed = readSourceEventLockOwner(directory)
  if (!sameSourceEventLockOwner(confirmed, owner)) {
    throw new Error(`Infinity Forge TUI source-event ${label} owner changed`)
  }
  const quarantine = `${directory}.${label}.${randomUUID()}`
  renameSync(directory, quarantine)
  const moved = readSourceEventLockOwner(quarantine)
  if (!sameSourceEventLockOwner(moved, owner)) {
    if (!existsSync(directory)) renameSync(quarantine, directory)
    throw new Error(`Infinity Forge TUI source-event ${label} owner changed`)
  }
  unlinkSync(join(quarantine, forgeSourceEventLockOwnerName))
  rmdirSync(quarantine)
  fsyncSourceEventDirectory(forgeSourceEventHome)
}

const publishSourceEventOwnerDirectory = (directory: string, owner: ForgeSourceEventLockOwner): boolean => {
  const candidate = `${directory}.candidate.${owner.nonce}`
  assertSafeSourceEventPath(candidate)
  mkdirSync(candidate, { mode: 0o700 })
  try {
    ensureOwnerOnlyPath(candidate, 0o700)
    const ownerPath = join(candidate, forgeSourceEventLockOwnerName)
    const descriptor = openSync(ownerPath, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | (constants.O_NOFOLLOW || 0), 0o600)
    try {
      writeSync(descriptor, JSON.stringify(owner), undefined, 'utf8')
      fsyncSync(descriptor)
    } finally {
      closeSync(descriptor)
    }
    ensureOwnerOnlyPath(ownerPath, 0o600)
    fsyncSourceEventDirectory(candidate)
    try {
      renameSync(candidate, directory)
    } catch (error) {
      if (!existsSync(directory)) throw error
      return false
    }
    fsyncSourceEventDirectory(forgeSourceEventHome)
    return true
  } finally {
    if (existsSync(candidate)) {
      const candidateOwner = readSourceEventLockOwner(candidate)
      if (!sameSourceEventLockOwner(candidateOwner, owner)) throw new Error('Infinity Forge TUI source-event lock candidate changed')
      unlinkSync(join(candidate, forgeSourceEventLockOwnerName))
      rmdirSync(candidate)
    }
  }
}

const waitForSourceEventLock = (deadline: number): void => {
  if (Date.now() >= deadline) throw new Error('Infinity Forge TUI source-event lock timed out')
  sleepForSourceEventLock()
}

const clearDeadSourceEventReclaim = (): void => {
  const reclaimOwner = readSourceEventLockOwner(forgeSourceEventReclaimPath)
  const currentIdentity = readProcessStartIdentity(reclaimOwner.pid)
  if (currentIdentity === reclaimOwner.processStartIdentity) return
  removeSourceEventOwnerDirectory(forgeSourceEventReclaimPath, reclaimOwner, 'dead-reclaim')
}

const acquireSourceEventLock = (): ForgeSourceEventLockOwner => {
  const deadline = Date.now() + forgeSourceEventLockTimeoutMs
  while (true) {
    assertSafeSourceEventPath(forgeSourceEventLockPath)
    assertSafeSourceEventPath(forgeSourceEventReclaimPath)
    if (existsSync(forgeSourceEventReclaimPath)) {
      clearDeadSourceEventReclaim()
      if (existsSync(forgeSourceEventReclaimPath)) waitForSourceEventLock(deadline)
      continue
    }
    const owner = sourceEventLockOwner()
    if (publishSourceEventOwnerDirectory(forgeSourceEventLockPath, owner)) return owner

    const observed = readSourceEventLockOwner(forgeSourceEventLockPath)
    const currentIdentity = readProcessStartIdentity(observed.pid)
    if (currentIdentity === observed.processStartIdentity) {
      waitForSourceEventLock(deadline)
      continue
    }

    const reclaimOwner = sourceEventLockOwner()
    if (!publishSourceEventOwnerDirectory(forgeSourceEventReclaimPath, reclaimOwner)) {
      waitForSourceEventLock(deadline)
      continue
    }
    try {
      const confirmed = readSourceEventLockOwner(forgeSourceEventLockPath)
      const confirmedIdentity = readProcessStartIdentity(confirmed.pid)
      if (sameSourceEventLockOwner(confirmed, observed) && confirmedIdentity !== confirmed.processStartIdentity) {
        removeSourceEventOwnerDirectory(forgeSourceEventLockPath, confirmed, 'dead-lock')
      }
    } finally {
      removeSourceEventOwnerDirectory(forgeSourceEventReclaimPath, reclaimOwner, 'reclaim-release')
    }
  }
}

const withSourceEventLock = <T>(action: () => T): T => {
  assertSafeSourceEventPath(forgeSourceEventHome)
  mkdirSync(forgeSourceEventHome, { mode: 0o700, recursive: true })
  assertSafeSourceEventPath(forgeSourceEventHome)
  ensureOwnerOnlyPath(forgeSourceEventHome, 0o700)
  const owner = acquireSourceEventLock()
  try {
    ensureSourceEventDirectory()
    return action()
  } finally {
    removeSourceEventOwnerDirectory(forgeSourceEventLockPath, owner, 'lock-release')
  }
}

const readSourceEventSessionId = (): string | null => {
  const activeSessionPath = process.env.HERMES_TUI_ACTIVE_SESSION_FILE
  if (!activeSessionPath) return null
  try {
    const value = JSON.parse(readFileSync(activeSessionPath, 'utf8')) as { session_id?: unknown }
    const sessionId = value.session_id
    if (typeof sessionId !== 'string' || !sessionId || sessionId.length > 512 || [...sessionId].some(character => character.charCodeAt(0) < 32)) return null
    return sessionId
  } catch {
    return null
  }
}

const readSourceEvents = (): ForgeSourceEventOutbox => {
  if (!existsSync(forgeSourceEventPath)) return { format: 'forge-surface-event/v1', pending: [] }
  assertSafeSourceEventPath(forgeSourceEventPath)
  ensureOwnerOnlyPath(forgeSourceEventPath, 0o600)
  const flags = constants.O_RDONLY | (constants.O_NOFOLLOW || 0)
  const descriptor = openSync(forgeSourceEventPath, flags)
  let value: ForgeSourceEventOutbox
  try {
    const opened = fstatSync(descriptor)
    const named = lstatSync(forgeSourceEventPath)
    if (!opened.isFile() || named.isSymbolicLink() || opened.dev !== named.dev || opened.ino !== named.ino) {
      throw new Error('Infinity Forge TUI source-event outbox path changed')
    }
    value = JSON.parse(readFileSync(descriptor, 'utf8')) as ForgeSourceEventOutbox
  } finally {
    closeSync(descriptor)
  }
  if (value.format !== 'forge-surface-event/v1' || !Array.isArray(value.pending)) {
    throw new Error('Infinity Forge TUI source-event outbox is invalid')
  }
  const keys = new Set<string>()
  for (const event of value.pending) {
    const fields = event && typeof event === 'object' ? Object.keys(event).sort().join(',') : ''
    const key = event && typeof event.sessionId === 'string' && typeof event.payloadHash === 'string' ? `${event.sessionId}\0${event.payloadHash}` : ''
    if (fields !== 'id,payloadHash,sessionId' || typeof event.id !== 'string' || !/^tui:[0-9a-f-]{36}$/i.test(event.id) || typeof event.sessionId !== 'string' || !event.sessionId || event.sessionId.length > 512 || [...event.sessionId].some(character => character.charCodeAt(0) < 32) || !/^[0-9a-f]{64}$/.test(event.payloadHash) || keys.has(key)) {
      throw new Error('Infinity Forge TUI source-event outbox entry is invalid')
    }
    keys.add(key)
  }
  return value
}

const writeSourceEvents = (value: ForgeSourceEventOutbox): void => {
  ensureSourceEventDirectory()
  assertSafeSourceEventPath(forgeSourceEventPath)
  const temporary = `${forgeSourceEventPath}.${process.pid}.${randomUUID()}.tmp`
  const flags = constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | (constants.O_NOFOLLOW || 0)
  let descriptor: number | null = null
  try {
    descriptor = openSync(temporary, flags, 0o600)
    writeSync(descriptor, JSON.stringify(value), undefined, 'utf8')
    fsyncSync(descriptor)
    closeSync(descriptor)
    descriptor = null
    ensureOwnerOnlyPath(temporary, 0o600)
    assertSafeSourceEventPath(temporary)
    renameSync(temporary, forgeSourceEventPath)
    assertSafeSourceEventPath(forgeSourceEventPath)
    ensureOwnerOnlyPath(forgeSourceEventPath, 0o600)
    fsyncSourceEventDirectory()
  } finally {
    if (descriptor !== null) closeSync(descriptor)
    if (existsSync(temporary)) {
      assertSafeSourceEventPath(temporary)
      unlinkSync(temporary)
    }
  }
}

export const prepareSourceEvent = (sessionId: string, payload: string): ForgePendingSourceEvent => withSourceEventLock(() => {
  const payloadHash = createHash('sha256').update(payload, 'utf8').digest('hex')
  const outbox = readSourceEvents()
  const prior = [...outbox.pending].reverse().find(
    event => event.sessionId === sessionId && event.payloadHash === payloadHash,
  )
  if (prior) return prior
  const event = { id: `tui:${randomUUID()}`, payloadHash, sessionId }
  outbox.pending.push(event)
  writeSourceEvents(outbox)
  return event
})

export const acknowledgeSourceEvent = (sourceEventId: string): void => {
  withSourceEventLock(() => {
    const outbox = readSourceEvents()
    const pending = outbox.pending.filter(event => event.id !== sourceEventId)
    if (pending.length === outbox.pending.length) return
    writeSourceEvents({ ...outbox, pending })
  })
}
'''


def change_tui_submission_source(source: str) -> str:
    """Persist one TUI submission ID before the gateway request is sent."""

    if "forge-surface-event/v1" in source:
        raise InstallError("TUI source-event outbox is already installed")
    newline = "\r\n" if "\r\n" in source else "\n"
    source = _TUI_SOURCE_EVENT_OUTBOX.replace("\n", newline) + newline + source
    request_line = (
        "deps.gw.request<PromptSubmitResponse>('prompt.submit', "
        "{ session_id: liveSid, text: submitText }).catch((e: Error) => {"
    )
    source = _insert_before_unique_line(
        source,
        request_line,
        (
            "const sourceEventSessionId = readSourceEventSessionId()",
            "const sourceEvent = sourceEventSessionId ? prepareSourceEvent(sourceEventSessionId, submitText) : null",
        ),
        label="TUI durable source event preparation",
    )
    return _replace_unique_line(
        source,
        request_line,
        "deps.gw.request<PromptSubmitResponse>('prompt.submit', { session_id: liveSid, text: submitText, ...(sourceEvent ? { source_event_id: sourceEvent.id } : {}) }).catch((e: Error) => {",
        label="TUI source event submission",
    )


def change_tui_session_lifecycle_source(source: str) -> str:
    """Persist the server-authenticated durable key, never the ephemeral sid."""

    source = _replace_unique_line(
        source,
        "writeActiveSessionFile(r.session_id)",
        "writeActiveSessionFile(r.stored_session_id ?? r.session_id)",
        label="TUI durable active session identity",
    )
    active_anchor = "writeActiveSessionFile(r.session_key ?? r.session_id)"
    if active_anchor in source:
        source = _replace_unique_line(
            source,
            active_anchor,
            "writeActiveSessionFile(r.source_event_session_id ?? r.session_key ?? r.session_id)",
            label="TUI active lineage identity",
        )
    resume_anchor = "writeActiveSessionFile(r.resumed ?? r.session_id)"
    if resume_anchor in source:
        source = _replace_unique_line(
            source,
            resume_anchor,
            "writeActiveSessionFile(id)",
            label="TUI resumed lineage identity",
        )
    return source


_DESKTOP_SOURCE_EVENT_OUTBOX = r'''type ForgeDesktopSourceEvent = {
  id: string
  liveSessionId: string
  payloadHash: string
  sessionId: string
}

const forgeDesktopSourceEventKey = 'forge-surface-event/v1'
const forgeDesktopSourceEventLockName = 'infinity-forge:desktop-source-events:v1'

const withDesktopSourceEventLock = async <T>(action: () => Promise<T> | T): Promise<T> => {
  const locks = globalThis.navigator?.locks
  if (!locks || typeof locks.request !== 'function') {
    throw new Error('Infinity Forge Desktop source-event lock is unavailable')
  }
  return locks.request(
    forgeDesktopSourceEventLockName,
    { mode: 'exclusive' },
    async lock => {
      if (!lock) throw new Error('Infinity Forge Desktop source-event lock was not acquired')
      return action()
    },
  )
}

const readDesktopSourceEvents = (): ForgeDesktopSourceEvent[] => {
  const raw = globalThis.localStorage.getItem(forgeDesktopSourceEventKey)
  if (!raw) return []
  const value = JSON.parse(raw) as unknown
  if (!Array.isArray(value)) throw new Error('Infinity Forge Desktop source-event outbox is invalid')
  if (value.some(event => !event || typeof event !== 'object' || typeof event.id !== 'string' || typeof event.liveSessionId !== 'string' || typeof event.payloadHash !== 'string' || typeof event.sessionId !== 'string')) {
    throw new Error('Infinity Forge Desktop source-event outbox entry is invalid')
  }
  return value as ForgeDesktopSourceEvent[]
}

const payloadHash = async (payload: string): Promise<string> => {
  const bytes = new TextEncoder().encode(payload)
  const digest = await globalThis.crypto.subtle.digest('SHA-256', bytes)
  return [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, '0')).join('')
}

const prepareSourceEvent = async (sessionId: string, liveSessionId: string, payload: string): Promise<ForgeDesktopSourceEvent> => {
  const hash = await payloadHash(payload)
  return withDesktopSourceEventLock(() => {
    const pending = readDesktopSourceEvents()
    const prior = [...pending].reverse().find(event => event.sessionId === sessionId && event.payloadHash === hash)
    if (prior) return prior
    const event = { id: `desktop:${globalThis.crypto.randomUUID()}`, liveSessionId, payloadHash: hash, sessionId }
    globalThis.localStorage.setItem(forgeDesktopSourceEventKey, JSON.stringify([...pending, event]))
    return event
  })
}

const rebindSourceEvent = async (sourceEventId: string, liveSessionId: string): Promise<void> => withDesktopSourceEventLock(() => {
    const pending = readDesktopSourceEvents()
    const index = pending.findIndex(event => event.id === sourceEventId)
    if (index < 0) throw new Error('Infinity Forge Desktop source event is not pending')
    pending[index] = { ...pending[index]!, liveSessionId }
    globalThis.localStorage.setItem(forgeDesktopSourceEventKey, JSON.stringify(pending))
  })

export const acknowledgeSourceEvent = async (sourceEventId: string): Promise<void> => withDesktopSourceEventLock(() => {
    const pending = readDesktopSourceEvents()
    const retained = pending.filter(event => event.id !== sourceEventId)
    if (retained.length !== pending.length) {
      globalThis.localStorage.setItem(forgeDesktopSourceEventKey, JSON.stringify(retained))
    }
  })
'''


def change_desktop_submit_source(source: str) -> str:
    """Reuse one localStorage-backed Desktop ID across request retries."""

    if "forge-surface-event/v1" in source:
        raise InstallError("Desktop source-event outbox is already installed")
    newline = "\r\n" if "\r\n" in source else "\n"
    source = _DESKTOP_SOURCE_EVENT_OUTBOX.replace("\n", newline) + newline + source
    source = _insert_after_unique_line(
        source,
        "let submitErr: unknown = null",
        (
            "const sourceEvent = await prepareSourceEvent(",
            "  selectedStoredSessionIdRef.current ?? sessionId,",
            "  sessionId,",
            "  text",
            ")",
        ),
        label="Desktop durable source event preparation",
    )
    if source.count("{ session_id: sessionId, text }") != 1 or source.count(
        "{ session_id: recoveredId, text }"
    ) != 1:
        raise InstallError("Desktop prompt.submit retry payloads are not exact")
    source = source.replace(
        "{ session_id: sessionId, text }",
        "{ session_id: sessionId, text, source_event_id: sourceEvent.id }",
        1,
    )
    source = _insert_after_unique_line(
        source,
        "if (recoveredId) {",
        ("await rebindSourceEvent(sourceEvent.id, recoveredId)",),
        label="Desktop recovered source event binding",
    )
    return source.replace(
        "{ session_id: recoveredId, text }",
        "{ session_id: recoveredId, text, source_event_id: sourceEvent.id }",
        1,
    )


def change_desktop_chat_messages_source(source: str) -> str:
    """Widen only message.complete choices; clarify keeps its string contract."""

    source = _insert_before_unique_line(
        source,
        "export type GatewayEventPayload = {",
        (
            "export interface ChoiceOptionPayload {",
            "  description: string",
            "  id: string",
            "  label: string",
            "}",
            "",
            "export interface ChoicePromptPayload {",
            "  choice_mode: 'multiple' | 'single'",
            "  choice_prompt_id: string",
            "  choices: ChoiceOptionPayload[]",
            "  expires_at: string",
            "  max_choices: null | number",
            "  min_choices: number",
            "  submit_label: string",
            "}",
            "",
        ),
        label="Desktop chooser payload types",
    )
    source = _replace_unique_line(
        source,
        "choices?: string[] | null",
        "choices?: ChoiceOptionPayload[] | string[] | null",
        label="Desktop chooser choices union",
    )
    return _insert_after_unique_line(
        source,
        "choices?: ChoiceOptionPayload[] | string[] | null",
        (
            "choice_prompt_id?: string",
            "choice_mode?: 'multiple' | 'single'",
            "min_choices?: number",
            "max_choices?: null | number",
            "submit_label?: string",
            "expires_at?: string",
            "source_event_id?: string",
        ),
        label="Desktop chooser contract fields",
    )


def change_desktop_prompts_store_source(source: str) -> str:
    source = _insert_after_unique_line(
        source,
        "import { $activeSessionId } from './session'",
        ("import type { ChoicePromptPayload, GatewayEventPayload } from '@/lib/chat-messages'",),
        label="Desktop chooser store type import",
    )
    identity_anchor = (
        "const idOf = (value: T): string | undefined => "
        "(value as { requestId?: string }).requestId"
    )
    identity_postcondition = (
        "const idOf = (value: T): string | undefined => "
        "(value as { choicePromptId?: string; requestId?: string }).requestId ?? "
        "(value as { choicePromptId?: string }).choicePromptId"
    )
    source = _replace_unique_line(
        source,
        identity_anchor,
        identity_postcondition,
        label="Desktop chooser stale clear identity",
    )
    anchor = "export interface ApprovalRequest extends KeyedPrompt {"
    addition = '''export interface ChoiceRequest extends KeyedPrompt {
  choiceMode: 'multiple' | 'single'
  choicePromptId: string
  choices: ChoicePromptPayload['choices']
  expiresAt: string
  maxChoices: null | number
  minChoices: number
  submitLabel: string
}

const CHOICE_PROMPT_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

export function choiceRequestFromPayload(payload: GatewayEventPayload | undefined, sessionId: string): ChoiceRequest | null {
  if (!payload || !CHOICE_PROMPT_ID_RE.test(payload.choice_prompt_id ?? '') || !Array.isArray(payload.choices) || !payload.choices.length) return null
  if (payload.choice_mode !== 'single' && payload.choice_mode !== 'multiple') return null
  if (!Number.isInteger(payload.min_choices) || (payload.min_choices ?? 0) < 1) return null
  if (payload.max_choices !== null && (!Number.isInteger(payload.max_choices) || (payload.max_choices ?? 0) < (payload.min_choices ?? 1))) return null
  if (payload.choice_mode === 'single' && (payload.min_choices !== 1 || payload.max_choices !== 1)) return null
  if (typeof payload.submit_label !== 'string' || !payload.submit_label.trim() || !Number.isFinite(Date.parse(payload.expires_at ?? '')) || Date.parse(payload.expires_at!) <= Date.now()) return null
  const ids = new Set<string>()
  for (const value of payload.choices) {
    if (!value || typeof value !== 'object' || typeof value.id !== 'string' || !value.id.trim() || typeof value.label !== 'string' || !value.label.trim() || typeof value.description !== 'string' || !value.description.trim() || ids.has(value.id)) return null
    ids.add(value.id)
  }
  if ((payload.max_choices !== null && payload.max_choices! > payload.choices.length) || payload.min_choices! > payload.choices.length) return null
  return {
    choiceMode: payload.choice_mode,
    choicePromptId: payload.choice_prompt_id!,
    choices: payload.choices as ChoicePromptPayload['choices'],
    expiresAt: payload.expires_at!,
    maxChoices: payload.max_choices!,
    minChoices: payload.min_choices!,
    sessionId,
    submitLabel: payload.submit_label
  }
}

'''
    source = _insert_before_unique_line(source, anchor, tuple(addition.splitlines()), label="Desktop chooser request type")
    instance_anchor = "const approval = keyedPromptStore<ApprovalRequest>()"
    source = _insert_before_unique_line(
        source,
        instance_anchor,
        ("const choice = keyedPromptStore<ChoiceRequest>()",),
        label="Desktop chooser keyed store",
    )
    export_anchor = "export const $approvalRequest = approval.$active"
    source = _insert_before_unique_line(
        source,
        export_anchor,
        (
            "export const $choiceRequest = choice.$active",
            "export const setChoiceRequest = choice.set",
            "export const clearChoiceRequest = choice.clear",
            "export const resetChoiceRequests = choice.reset",
            "",
        ),
        label="Desktop chooser store exports",
    )
    source = _insert_after_unique_line(
        source,
        "secret.reset()",
        ("choice.reset()",),
        label="Desktop chooser global clear",
    )
    source = _insert_after_unique_line(
        source,
        "secret.clear(sessionId)",
        ("choice.clear(sessionId)",),
        label="Desktop chooser session clear",
    )
    source = _replace_unique_sequence(
        source,
        (
            "export const $activeSessionAwaitingInput = computed(",
            "[$clarifyRequest, $approvalRequest, $sudoRequest, $secretRequest],",
            "(clarify, approval, sudo, secret) => Boolean(clarify || approval || sudo || secret)",
            ")",
        ),
        (
            "export const $activeSessionAwaitingInput = computed(",
            "  [$clarifyRequest, $approvalRequest, $sudoRequest, $secretRequest, $choiceRequest],",
            "  (clarify, approval, sudo, secret, pendingChoice) => Boolean(clarify || approval || sudo || secret || pendingChoice)",
            ")",
        ),
        label="Desktop chooser awaiting input",
    )
    for expected, label in (
        (identity_postcondition, "Desktop chooser stale clear identity"),
        ("choice.reset()", "Desktop chooser global clear"),
        ("choice.clear(sessionId)", "Desktop chooser session clear"),
    ):
        if source.count(expected) != 1:
            raise InstallError(f"{label} postcondition failed")
    return source


def change_desktop_gateway_event_source(source: str) -> str:
    actual_import = "import { clearAllPrompts, setApprovalRequest, setSecretRequest, setSudoRequest } from '@/store/prompts'"
    new_import = "import { choiceRequestFromPayload, clearAllPrompts, clearChoiceRequest, setApprovalRequest, setChoiceRequest, setSecretRequest, setSudoRequest } from '@/store/prompts'"
    if source.count(actual_import) != 1:
        raise InstallError("Desktop gateway chooser import anchor is not unique")
    source = source.replace(actual_import, new_import)
    source = _insert_after_unique_line(
        source,
        new_import,
        ("import { acknowledgeSourceEvent } from '../use-prompt-actions/submit'",),
        label="Desktop source event acknowledgement import",
    )

    message_start = "} else if (event.type === 'message.start') {"
    source = _insert_after_unique_line(
        source,
        message_start,
        (
            "if (sessionId) clearChoiceRequest(sessionId)",
        ),
        label="Desktop chooser clear on new turn",
    )
    source = _insert_after_unique_line(
        source,
        "completeAssistantMessage(sessionId, finalText)",
        (
            "if (typeof payload?.source_event_id === 'string') void acknowledgeSourceEvent(payload.source_event_id).catch(error => console.error('Infinity Forge Desktop source-event acknowledgement failed', error))",
            "",
            "const choiceRequest = choiceRequestFromPayload(payload, sessionId)",
            "if (choiceRequest) setChoiceRequest(choiceRequest)",
        ),
        label="Desktop chooser capture after message completion",
    )
    return source


def change_desktop_prompt_overlays_source(source: str) -> str:
    import_line = "import { type FormEvent, useCallback, useEffect, useState } from 'react'"
    if source.count(import_line) != 1:
        raise InstallError("Desktop chooser React import anchor is not unique")
    source = source.replace(
        import_line,
        "import { type FormEvent, type KeyboardEvent, useCallback, useEffect, useRef, useState } from 'react'",
    )
    old = "import { $secretRequest, $sudoRequest, clearSecretRequest, clearSudoRequest } from '@/store/prompts'"
    new = "import { $choiceRequest, $secretRequest, $sudoRequest, clearChoiceRequest, clearSecretRequest, clearSudoRequest, type ChoiceRequest } from '@/store/prompts'"
    if source.count(old) != 1:
        raise InstallError("Desktop chooser prompt store import anchor is not unique")
    source = source.replace(old, new)

    component = '''export function choiceSubmitErrorDisposition(reason: unknown): { clearPrompt: boolean; message: string } {
  const error = reason as { code?: unknown; message?: unknown } | null
  const code = typeof error?.code === 'number' ? error.code : null
  const message = typeof error?.message === 'string' ? error.message : String(reason)
  if (code === 4009 || message === 'session busy') return { clearPrompt: false, message }
  const terminal = ['belongs to another connection', 'no pending choice prompt', 'choice prompt is stale', 'choice prompt expired']
  return { clearPrompt: terminal.some(fragment => message.includes(fragment)), message }
}

export type ChoiceKeyboardAction =
  | { focusedIndex: number; kind: 'state'; selected: string[] }
  | { ids: string[]; kind: 'submit' }
  | { kind: 'noop' }

export function choiceKeyboardAction(
  request: ChoiceRequest,
  focusedIndex: null | number,
  selected: readonly string[],
  key: string
): ChoiceKeyboardAction {
  if (key === 'ArrowDown' || key === 'ArrowUp') {
    const delta = key === 'ArrowDown' ? 1 : -1
    const nextIndex = focusedIndex === null
      ? (delta > 0 ? 0 : request.choices.length - 1)
      : (focusedIndex + delta + request.choices.length) % request.choices.length
    const nextSelected = request.choiceMode === 'single' ? [request.choices[nextIndex]!.id] : [...selected]
    return { focusedIndex: nextIndex, kind: 'state', selected: nextSelected }
  }
  if (key === ' ' && focusedIndex !== null) {
    const id = request.choices[focusedIndex]?.id
    if (!id) return { kind: 'noop' }
    const nextSelected = request.choiceMode === 'single'
      ? [id]
      : selected.includes(id) ? selected.filter(value => value !== id) : [...selected, id]
    return { focusedIndex, kind: 'state', selected: nextSelected }
  }
  if (key === 'Enter') return { ids: [...selected], kind: 'submit' }
  return { kind: 'noop' }
}

export function ChoiceDialog() {
  const request = useStore($choiceRequest)
  const gateway = useStore($gateway)
  const [selected, setSelected] = useState<string[]>([])
  const [focusedIndex, setFocusedIndex] = useState<null | number>(null)
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const choiceRefs = useRef<Array<HTMLButtonElement | HTMLInputElement | null>>([])

  useEffect(() => {
    setSelected([])
    setFocusedIndex(null)
    setError('')
    setSubmitting(false)
  }, [request?.choicePromptId])

  useEffect(() => {
    if (!request) return
    const delay = Date.parse(request.expiresAt) - Date.now()
    if (delay <= 0) {
      clearChoiceRequest(request.sessionId, request.choicePromptId)
      return
    }
    const timer = window.setTimeout(() => clearChoiceRequest(request.sessionId, request.choicePromptId), delay)
    return () => window.clearTimeout(timer)
  }, [request?.choicePromptId, request?.expiresAt, request?.sessionId])

  if (!request) return null

  const submit = async (ids: string[]) => {
    if (!gateway || submitting) return
    if (ids.length < request.minChoices || (request.maxChoices !== null && ids.length > request.maxChoices)) {
      setError(`Choose ${request.minChoices}${request.maxChoices === null ? '+' : `–${request.maxChoices}`} option(s).`)
      return
    }
    setSubmitting(true)
    try {
      await gateway.request('choice.submit', {
        choice_prompt_id: request.choicePromptId,
        selected_choice_ids: ids,
        session_id: request.sessionId
      })
      clearChoiceRequest(request.sessionId, request.choicePromptId)
    } catch (reason) {
      const failure = choiceSubmitErrorDisposition(reason)
      setError(failure.message)
      if (failure.clearPrompt) clearChoiceRequest(request.sessionId, request.choicePromptId)
      setSubmitting(false)
    }
  }

  const handleChoiceKeyDown = (event: KeyboardEvent) => {
    const action = choiceKeyboardAction(request, focusedIndex, selected, event.key)
    if (action.kind === 'noop') return
    event.preventDefault()
    if (action.kind === 'state') {
      setFocusedIndex(action.focusedIndex)
      setSelected(action.selected)
      setError('')
      choiceRefs.current[action.focusedIndex]?.focus()
    } else {
      void submit(action.ids)
    }
  }

  return <Dialog onOpenChange={open => { if (!open && !submitting) clearChoiceRequest(request.sessionId, request.choicePromptId) }} open>
    <DialogContent aria-describedby="forge-choice-description" showCloseButton={false}>
      <DialogHeader>
        <DialogTitle>Choose an option</DialogTitle>
        <DialogDescription id="forge-choice-description">Selections are submitted by stable ID.</DialogDescription>
      </DialogHeader>
      {request.choiceMode === 'single' ? <div aria-label="Available choices" onKeyDown={handleChoiceKeyDown} role="radiogroup">
        {request.choices.map((choice, index) => <Button aria-checked={selected.includes(choice.id)} disabled={submitting} key={choice.id} onClick={() => void submit([choice.id])} onFocus={() => setFocusedIndex(index)} ref={element => { choiceRefs.current[index] = element }} role="radio" tabIndex={focusedIndex === index || (focusedIndex === null && index === 0) ? 0 : -1} variant="outline">
          <span>{choice.label}</span><span>{choice.description}</span>
        </Button>)}
      </div> : <fieldset disabled={submitting} onKeyDown={handleChoiceKeyDown}>
        <legend>Available choices</legend>
        {request.choices.map((choice, index) => <label key={choice.id}>
          <input checked={selected.includes(choice.id)} onChange={() => setSelected(values => values.includes(choice.id) ? values.filter(id => id !== choice.id) : [...values, choice.id])} onFocus={() => setFocusedIndex(index)} ref={element => { choiceRefs.current[index] = element }} tabIndex={focusedIndex === index || (focusedIndex === null && index === 0) ? 0 : -1} type="checkbox" />
          <span>{choice.label}</span><span>{choice.description}</span>
        </label>)}
      </fieldset>}
      <p aria-live="polite">{error}</p>
      {request.choiceMode === 'multiple' ? <DialogFooter>
        <Button disabled={submitting} onClick={() => clearChoiceRequest(request.sessionId, request.choicePromptId)} type="button" variant="ghost">Cancel</Button>
        <Button disabled={submitting} onClick={() => void submit(selected)} type="button">{request.submitLabel}</Button>
      </DialogFooter> : null}
    </DialogContent>
  </Dialog>
}

'''
    source = _insert_before_unique_line(
        source,
        "export function PromptOverlays() {",
        tuple(component.splitlines()),
        label="Desktop chooser dialog",
    )
    return _insert_after_unique_line(
        source,
        "<SecretDialog />",
        ("<ChoiceDialog />",),
        label="Desktop chooser dialog mount",
    )


def change_slack_adapter_source(source: str) -> str:
    """Carry Block Kit chooser controls plus an exact-ID structured fallback."""

    if source.count("from dataclasses import dataclass, field") == 1:
        source = _insert_after_unique_line(
            source,
            "from dataclasses import dataclass, field",
            ("from datetime import datetime",),
            label="Slack chooser datetime import",
        )
    approval_state = "self._approval_resolved: Dict[str, bool] = {}" if "self._approval_resolved: Dict" in source else "self._approval_resolved = {}"
    source = _insert_after_unique_line(
        source,
        approval_state,
        (
            "# RISK(race): action handlers atomically pop these event-loop-owned maps before their first await.",
            "self._choice_prompts: Dict[str, Dict[str, Any]] = {}",
            "self._choice_reply_prompts: Dict[Tuple[str, str, str], str] = {}",
        ),
        label="Slack chooser pending state",
    )
    registration = "# Register Block Kit action handlers for slash-confirm buttons"
    source = _insert_before_unique_line(
        source,
        registration,
        (
            "# Forge chooser actions are distinct from approvals and slash-confirm IDs.",
            "for _choice_action_id in (\"forge_choice_button\", \"forge_choice_select\", \"forge_choice_multi\", \"forge_choice_submit\"):",
            "    self._app.action(_choice_action_id)(self._handle_choice_action)",
            "",
        ),
        label="Slack chooser action registration",
    )

    methods = '''    @staticmethod
    def _choice_context_key(channel_id: str, thread_ts: Optional[str], user_id: str) -> Tuple[str, str, str]:
        return str(channel_id or ""), str(thread_ts or ""), str(user_id or "")

    @staticmethod
    def _valid_choice_prompt(prompt: Any) -> bool:
        if not isinstance(prompt, dict):
            return False
        required = {"choice_prompt_id", "choice_mode", "min_choices", "max_choices", "submit_label", "expires_at", "choices"}
        if not required.issubset(prompt) or prompt.get("choice_mode") not in {"single", "multiple"}:
            return False
        choices = prompt.get("choices")
        if not isinstance(choices, list) or not choices or len(choices) > 256:
            return False
        if any(not isinstance(choice, dict) or set(choice) != {"id", "label", "description"} or any(not isinstance(choice.get(key), str) or not choice[key].strip() for key in ("id", "label", "description")) for choice in choices):
            return False
        if any(len(choice["id"]) > 512 or len(choice["label"]) > 3000 or len(choice["description"]) > 3000 for choice in choices):
            return False
        ids = [choice["id"] for choice in choices]
        minimum, maximum = prompt.get("min_choices"), prompt.get("max_choices")
        if len(ids) != len(set(ids)) or not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 1:
            return False
        if maximum is not None and (not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < minimum or maximum > len(ids)):
            return False
        if prompt["choice_mode"] == "single" and (minimum != 1 or maximum != 1):
            return False
        try:
            expires = datetime.fromisoformat(prompt["expires_at"].replace("Z", "+00:00"))
        except (TypeError, ValueError, AttributeError):
            return False
        return expires.tzinfo is not None

    @staticmethod
    def _choice_fallback_pages(content: str, prompt: Dict[str, Any]) -> List[str]:
        # Slack truncates very large text. Keep every stable ID reachable while
        # reserving room for the prompt-bound reply syntax on the final page.
        content_limit = 25000
        lines = [f"{choice['id']} — {choice['label']}" for choice in prompt["choices"]]
        chunks: List[List[str]] = []
        current: List[str] = []
        current_length = 0
        for line in lines:
            if len(line) > content_limit:
                return []
            separator = 1 if current else 0
            if current and current_length + separator + len(line) > content_limit:
                chunks.append(current)
                current = []
                current_length = 0
                separator = 0
            current.append(line)
            current_length += separator + len(line)
        if current:
            chunks.append(current)
        if not chunks:
            return []
        reply = f"Reply with: choose {prompt['choice_prompt_id']} <choice_id[,choice_id...]>"
        heading = str(content or "Choose an option")[:3000]
        pages: List[str] = []
        total = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            parts = []
            if index == 1:
                parts.extend((heading, ""))
            parts.extend((f"Choices page {index}/{total}", "\\n".join(chunk)))
            if index == total:
                parts.append(reply)
            page = "\\n".join(parts)
            if len(page) > 30000:
                return []
            pages.append(page)
        return pages

    def _claim_choice_state(self, message_ts: str, selected_ids: List[str]) -> Optional[Dict[str, Any]]:
        state = self._choice_prompts.get(message_ts)
        if not state or len(selected_ids) != len(set(selected_ids)):
            return None
        if self._choice_reply_prompts.get(state["context_key"]) != message_ts:
            self._choice_prompts.pop(message_ts, None)
            return None
        prompt = state["prompt"]
        allowed = {choice["id"] for choice in prompt["choices"]}
        minimum, maximum = prompt["min_choices"], prompt["max_choices"]
        expires = datetime.fromisoformat(prompt["expires_at"].replace("Z", "+00:00"))
        if datetime.now(expires.tzinfo) >= expires:
            self._choice_prompts.pop(message_ts, None)
            if self._choice_reply_prompts.get(state["context_key"]) == message_ts:
                self._choice_reply_prompts.pop(state["context_key"], None)
            return None
        if any(choice_id not in allowed for choice_id in selected_ids) or len(selected_ids) < minimum or (maximum is not None and len(selected_ids) > maximum):
            return None
        # Atomic before any await: stale and duplicate Slack actions fail closed.
        self._choice_prompts.pop(message_ts, None)
        self._choice_reply_prompts.pop(state["context_key"], None)
        return state

    def _consume_choice_reply(self, channel_id: str, thread_ts: Optional[str], user_id: str, text: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
        key = self._choice_context_key(channel_id, thread_ts, user_id)
        message_ts = self._choice_reply_prompts.get(key)
        if not message_ts:
            return False, None
        state = self._choice_prompts.get(message_ts)
        if not state:
            self._choice_reply_prompts.pop(key, None)
            return False, None
        expires = datetime.fromisoformat(state["prompt"]["expires_at"].replace("Z", "+00:00"))
        if datetime.now(expires.tzinfo) >= expires:
            self._choice_prompts.pop(message_ts, None)
            self._choice_reply_prompts.pop(key, None)
            return False, None
        reply_parts = str(text or "").split(" ", 2)
        if len(reply_parts) != 3 or reply_parts[0] != "choose":
            return False, None
        if reply_parts[1] != state["prompt"]["choice_prompt_id"]:
            return True, None
        selected_ids = [part.strip() for part in reply_parts[2].split(",") if part.strip()]
        state = self._claim_choice_state(message_ts, selected_ids)
        if not state:
            return True, None
        return True, {"choice_prompt_id": state["prompt"]["choice_prompt_id"], "selected_choice_ids": selected_ids}

    async def _dispatch_choice_submission(self, state: Dict[str, Any], selected_ids: List[str]) -> None:
        workspace_id = str(state.get("workspace_id") or "")
        if not workspace_id:
            raise ValueError("Slack chooser workspace identity is missing")
        source = self.build_source(
            chat_id=state["channel_id"],
            chat_name=state["channel_id"],
            chat_type=state["chat_type"],
            user_id=state["user_id"],
            user_name=state.get("user_name"),
            thread_id=state.get("thread_ts"),
        )
        envelope = {"choice_prompt_id": state["prompt"]["choice_prompt_id"], "selected_choice_ids": selected_ids}
        source_identity = __import__("json").dumps(
            [
                "forge-slack-choice-source-event/v1",
                workspace_id,
                state["channel_id"],
                str(state.get("thread_ts") or ""),
                state["user_id"],
                state["prompt"]["choice_prompt_id"],
                sorted(selected_ids),
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        source_event_id = "slack-choice:" + __import__("hashlib").sha256(source_identity).hexdigest()
        event = MessageEvent(
            text=",".join(selected_ids),
            message_type=MessageType.TEXT,
            source=source,
            raw_message={"type": "forge_choice_action"},
            metadata={
                "structured_user_message": envelope,
                "source_event_id": source_event_id,
            },
        )
        await self.handle_message(event)

    async def send_choice_prompt(
        self,
        chat_id: str,
        content: str,
        prompt: Dict[str, Any],
        session_key: str,
        user_id: str,
        user_name: Optional[str] = None,
        chat_type: str = "group",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if not self._app or not self._valid_choice_prompt(prompt) or not user_id:
            return SendResult(success=False, error="invalid chooser context")
        try:
            thread_ts = self._resolve_thread_ts(None, metadata)
            options = [{"text": {"type": "plain_text", "text": choice["label"][:75]}, "value": choice["id"]} for choice in prompt["choices"]]
            blocks: List[Dict[str, Any]] = [{"type": "section", "text": {"type": "mrkdwn", "text": str(content or "Choose an option")[:3000]}, "block_id": f"forge_choice:{prompt['choice_prompt_id']}"}]
            if prompt["choice_mode"] == "single" and len(options) <= 5:
                blocks.append({"type": "actions", "block_id": f"forge_choice:{prompt['choice_prompt_id']}", "elements": [{"type": "button", "text": option["text"], "value": option["value"], "action_id": "forge_choice_button"} for option in options]})
            elif len(options) <= 100:
                element = {"type": "multi_static_select" if prompt["choice_mode"] == "multiple" else "static_select", "action_id": "forge_choice_multi" if prompt["choice_mode"] == "multiple" else "forge_choice_select", "options": options, "placeholder": {"type": "plain_text", "text": "Select choices" if prompt["choice_mode"] == "multiple" else prompt["submit_label"][:75]}}
                elements = [element]
                if prompt["choice_mode"] == "multiple":
                    elements.append({"type": "button", "action_id": "forge_choice_submit", "text": {"type": "plain_text", "text": prompt["submit_label"][:75]}, "value": prompt["choice_prompt_id"]})
                blocks.append({"type": "actions", "block_id": f"forge_choice:{prompt['choice_prompt_id']}", "elements": elements})
            fallback_pages = self._choice_fallback_pages(str(content or "Choose an option"), prompt)
            if not fallback_pages:
                return SendResult(success=False, error="Slack chooser fallback is too large")
            client = self._get_client(chat_id)
            result: Dict[str, Any] = {}
            message_ts = ""
            for page_number, fallback in enumerate(fallback_pages, start=1):
                kwargs: Dict[str, Any] = {"channel": chat_id, "text": fallback}
                if page_number == len(fallback_pages):
                    kwargs["blocks"] = blocks
                if thread_ts:
                    kwargs["thread_ts"] = thread_ts
                result = await client.chat_postMessage(**kwargs)
                message_ts = str(result.get("ts") or "")
                if not message_ts:
                    return SendResult(success=False, error="Slack chooser message has no timestamp")
            context_key = self._choice_context_key(chat_id, thread_ts, user_id)
            workspace_id = str((metadata or {}).get("team_id") or (metadata or {}).get("workspace_id") or "")
            state = {"channel_id": chat_id, "chat_type": chat_type, "context_key": context_key, "prompt": prompt, "session_key": session_key, "thread_ts": thread_ts, "user_id": user_id, "user_name": user_name, "workspace_id": workspace_id}
            previous_message_ts = self._choice_reply_prompts.get(context_key)
            if previous_message_ts:
                self._choice_prompts.pop(previous_message_ts, None)
            self._choice_prompts[message_ts] = state
            self._choice_reply_prompts[context_key] = message_ts
            return SendResult(success=True, message_id=message_ts, raw_response=result)
        except Exception as exc:
            logger.error("[Slack] send_choice_prompt failed: %s", exc, exc_info=True)
            return SendResult(success=False, error=str(exc))

    async def _handle_choice_action(self, ack, body, action) -> None:
        await ack()
        message = body.get("message") or {}
        message_ts = str(message.get("ts") or "")
        state = self._choice_prompts.get(message_ts)
        if not state:
            return
        channel_id = str((body.get("channel") or {}).get("id") or "")
        workspace_id = str((body.get("team") or {}).get("id") or body.get("team_id") or "")
        user = body.get("user") or {}
        user_id = str(user.get("id") or "")
        expected_workspace_id = str(state.get("workspace_id") or "")
        if not workspace_id or (expected_workspace_id and workspace_id != expected_workspace_id) or channel_id != state["channel_id"] or user_id != state["user_id"] or not self._is_interactive_user_authorized(user_id, channel_id=channel_id, user_name=user.get("name")):
            return
        if str(message.get("thread_ts") or "") != str(state.get("thread_ts") or ""):
            return
        if str(action.get("block_id") or "") != f"forge_choice:{state['prompt']['choice_prompt_id']}":
            return
        action_id = action.get("action_id")
        if action_id == "forge_choice_multi":
            selected_ids = [str(option.get("value") or "") for option in action.get("selected_options") or []]
            allowed = {choice["id"] for choice in state["prompt"]["choices"]}
            maximum = state["prompt"]["max_choices"]
            if len(selected_ids) != len(set(selected_ids)) or any(choice_id not in allowed for choice_id in selected_ids) or (maximum is not None and len(selected_ids) > maximum):
                return
            state["selected_ids"] = selected_ids
            return
        elif action_id == "forge_choice_submit":
            if str(action.get("value") or "") != state["prompt"]["choice_prompt_id"]:
                return
            selected_ids = list(state.get("selected_ids") or [])
        elif action_id == "forge_choice_select":
            selected_ids = [str((action.get("selected_option") or {}).get("value") or "")]
        else:
            selected_ids = [str(action.get("value") or "")]
        claimed = self._claim_choice_state(message_ts, selected_ids)
        if not claimed:
            return
        claimed["workspace_id"] = workspace_id
        try:
            await self._get_client(channel_id).chat_update(channel=channel_id, ts=message_ts, text="Choice submitted", blocks=[{"type": "context", "elements": [{"type": "mrkdwn", "text": "Choice submitted."}]}])
        except Exception:
            logger.warning("[Slack] Failed to close chooser controls", exc_info=True)
        await self._dispatch_choice_submission(claimed, selected_ids)

'''
    source = _insert_before_unique_line(
        source,
        "async def send_exec_approval(" if "    async def send_exec_approval(" not in source else "async def send_exec_approval(",
        tuple(line[4:] if line.startswith("    ") else line for line in methods.splitlines()),
        label="Slack chooser methods",
    )

    # Convert an exact-ID reply under a pending prompt into trusted metadata;
    # invalid IDs are consumed (not forwarded to the model).
    msg_anchor = "msg_event = MessageEvent("
    prelude = (
        "choice_reply_handled, structured_choice = self._consume_choice_reply(channel_id, thread_ts, user_id, text)",
        "if choice_reply_handled and structured_choice is None:",
        "    logger.warning(\"[Slack] Ignoring invalid exact-ID chooser reply from %s\", user_id)",
        "    return",
        "_forge_platform_event_id = (event.get(\"event_ts\") if event.get(\"subtype\") == \"message_changed\" else event.get(\"client_msg_id\")) or event.get(\"event_ts\") or event.get(\"ts\") or (event.get(\"message\") or {}).get(\"client_msg_id\")",
        "_forge_workspace_id = str(event.get(\"team\") or event.get(\"team_id\") or \"\")",
        "_forge_source_identity = __import__(\"json\").dumps([\"forge-slack-source-event/v1\", _forge_workspace_id, channel_id, str(thread_ts or \"\"), str(_forge_platform_event_id or \"\")], ensure_ascii=False, separators=(\",\", \":\")).encode(\"utf-8\")",
        "_forge_source_event_id = \"slack:\" + __import__(\"hashlib\").sha256(_forge_source_identity).hexdigest() if _forge_workspace_id and _forge_platform_event_id else \"\"",
        "choice_metadata = {\"structured_user_message\": structured_choice} if structured_choice else {}",
        "if _forge_source_event_id:",
        "    choice_metadata[\"source_event_id\"] = _forge_source_event_id",
        "",
    )
    source = _insert_before_unique_line(source, msg_anchor, prelude, label="Slack exact-ID chooser fallback")
    return _insert_after_line_in_unique_block(
        source,
        block_start=msg_anchor,
        expected="raw_message=event,",
        addition="metadata=choice_metadata,",
        max_lines=18,
        label="Slack structured chooser metadata",
    )


def change_gateway_source(source: str) -> str:
    """Mark messaging input and suppress handled-turn title and goal models."""

    if _HOOK_MARKER in source:
        raise InstallError("gateway/run.py user-turn handling is already installed")
    gateway_lines = source.splitlines()
    runner_methods = [
        index
        for index, line in enumerate(gateway_lines)
        if line.strip() == "async def _run_agent("
    ]
    if len(runner_methods) != 1:
        raise InstallError("gateway structured runner seam is not unique")
    runner_classes = [
        line.strip()
        for line in gateway_lines[: runner_methods[0]]
        if line.startswith("class ") and line.rstrip().endswith(":")
    ]
    if not runner_classes:
        raise InstallError("gateway runner class was not found")
    gateway_class_anchor = runner_classes[-1]
    source = _insert_after_unique_line(
        source,
        gateway_class_anchor,
        (
            "    @staticmethod",
            "    def _forge_trusted_turn_context(event, source, session_id):",
            "        # RISK(security): source identity is adapter-authenticated; user/model text is ignored.",
            "        platform_value = str(getattr(getattr(source, \"platform\", None), \"value\", getattr(source, \"platform\", \"gateway\")) or \"gateway\")",
            "        metadata = getattr(event, \"metadata\", None)",
            "        source_event_id = metadata.get(\"source_event_id\", \"\") if isinstance(metadata, dict) else \"\"",
            "        if not source_event_id:",
            "            scope_id = str(getattr(source, \"scope_id\", None) or getattr(source, \"guild_id\", None) or \"\")",
            "            profile = str(getattr(source, \"profile\", None) or \"\")",
            "            chat_id = str(getattr(source, \"chat_id\", \"\") or \"\")",
            "            thread_id = str(getattr(source, \"thread_id\", None) or \"\")",
            "            platform_event_id = getattr(event, \"platform_update_id\", None)",
            "            event_kind = \"update\"",
            "            if platform_event_id is None:",
            "                platform_event_id = getattr(event, \"message_id\", None) or getattr(source, \"message_id\", None)",
            "                event_kind = \"message\"",
            "            if platform_event_id is not None and str(platform_event_id):",
            "                event_identity = f\"forge-gateway-source-event/v1\\0{platform_value}\\0{scope_id}\\0{profile}\\0{chat_id}\\0{thread_id}\\0{event_kind}\\0{platform_event_id}\"",
            "                event_digest = __import__(\"hashlib\").sha256(event_identity.encode(\"utf-8\")).hexdigest()",
            "                source_event_id = f\"gateway:{event_digest}\"",
            "        return {",
            "            \"owner_host\": os.environ.get(\"INFINITY_FORGE_HOST_ID\", \"\"),",
            "            \"subject_id\": str(getattr(source, \"user_id\", \"\") or \"\"),",
            "            \"session_id\": str(session_id or \"\"),",
            "            \"surface\": platform_value,",
            "            \"source_event_id\": str(source_event_id or \"\"),",
            "            \"working_directory\": getattr(source, \"working_directory\", None),",
            "        }",
            "",
        ),
        label="gateway trusted turn context builder",
    )
    source = _insert_after_unique_line(
        source,
        '"task_id": session_id,',
        (
            f"# {_HOOK_MARKER}: an authenticated gateway message created this turn.",
            '"is_user_turn": True,',
        ),
        label="gateway user-turn call",
    )
    source = _replace_unique_line(
        source,
        "if final_response and self._session_db:",
        'if final_response and self._session_db and not result.get("handled"):',
        label="gateway handled title guard",
    )
    source = _insert_after_unique_line(
        source,
        '"last_reasoning": result.get("last_reasoning"),',
        (
            '"handled": result_holder[0].get("handled", False) if result_holder[0] else False,',
            '"choices": result_holder[0].get("choices", []) if result_holder[0] else [],',
            '"choice_prompt_id": result_holder[0].get("choice_prompt_id") if result_holder[0] else None,',
            '"choice_mode": result_holder[0].get("choice_mode") if result_holder[0] else None,',
            '"min_choices": result_holder[0].get("min_choices") if result_holder[0] else None,',
            '"max_choices": result_holder[0].get("max_choices") if result_holder[0] else None,',
            '"submit_label": result_holder[0].get("submit_label") if result_holder[0] else None,',
            '"expires_at": result_holder[0].get("expires_at") if result_holder[0] else None,',
        ),
        label="gateway handled result forwarding",
    )
    source = _replace_unique_line(
        source,
        "if _final_text.strip():",
        'if _final_text.strip() and not (isinstance(_agent_result, dict) and _agent_result.get("handled")):',
        label="gateway handled goal guard",
    )
    source = _insert_after_unique_line(
        source,
        'response = agent_result.get("final_response") or ""',
        _choice_display_lines("agent_result", "response"),
        label="gateway chooser display",
    )

    # The full Hermes gateway has an inner runner seam that can accept the
    # trusted adapter metadata without serializing it into user-visible text.
    runner_seams = source.count("async def _run_agent(")
    if runner_seams == 1:
        source = _insert_after_line_in_unique_block(
            source,
            block_start="async def _run_agent(",
            expected="persist_user_timestamp: Optional[float] = None,",
            addition=(
                "structured_user_message: Optional[dict] = None,",
                "trusted_turn_context: Optional[dict] = None,",
            ),
            max_lines=30,
            label="gateway structured chooser wrapper parameter",
        )
        source = _insert_after_line_in_unique_block(
            source,
            block_start="async def _run_agent_inner(",
            expected="persist_user_timestamp: Optional[float] = None,",
            addition=(
                "structured_user_message: Optional[dict] = None,",
                "trusted_turn_context: Optional[dict] = None,",
            ),
            max_lines=30,
            label="gateway structured chooser inner parameter",
        )
        lines = source.splitlines(keepends=True)
        call_indexes = [
            index
            for index, line in enumerate(lines)
            if line.strip() == "persist_user_timestamp=persist_user_timestamp,"
        ]
        if len(call_indexes) != 3:
            raise InstallError("gateway structured chooser calls are not unique")
        for ordinal, index in reversed(list(enumerate(call_indexes))):
            original = lines[index]
            newline = "\r\n" if original.endswith("\r\n") else "\n"
            indent = original[: len(original) - len(original.lstrip())]
            value = (
                '(event.metadata or {}).get("structured_user_message") if isinstance(getattr(event, "metadata", None), dict) else None'
                if ordinal == 0
                else "structured_user_message"
            )
            trusted_value = (
                "self._forge_trusted_turn_context(event, source, session_id)"
                if ordinal == 0
                else "trusted_turn_context"
            )
            lines[index + 1 : index + 1] = [
                f"{indent}structured_user_message={value},{newline}",
                f"{indent}trusted_turn_context={trusted_value},{newline}",
            ]
        source = "".join(lines)
        source = _insert_after_unique_line(
            source,
            "if self._get_proxy_url():",
            (
                "    if structured_user_message is not None:",
                "        return {\"final_response\": \"Structured choice submission is unavailable through a proxy.\", \"failed\": True, \"handled\": True, \"messages\": []}",
            ),
            label="gateway proxy chooser fail closed",
        )
        source = _insert_before_unique_line(
            source,
            "_api_run_message = _wrap_current_message_with_observed_context(",
            (
                "if structured_user_message is not None:",
                "    _run_message = structured_user_message",
                "",
            ),
            label="gateway trusted chooser envelope",
        )
        source = _insert_after_unique_line(
            source,
            '"is_user_turn": True,',
            ('"trusted_turn_context": trusted_turn_context,',),
            label="gateway trusted source event forwarding",
        )

        final_return = re.compile(
            r"            return response\r?\n[ \t]*\r?\n        except Exception as e:"
        )
        choice_delivery = '''            _choice_fields = ("choice_prompt_id", "choice_mode", "min_choices", "max_choices", "submit_label", "expires_at", "choices")
            _choice_prompt = {field: agent_result.get(field) for field in _choice_fields}
            if all(_choice_prompt.get(field) is not None for field in _choice_fields if field != "max_choices") and isinstance(_choice_prompt.get("choices"), list):
                # Stable IDs are authoritative only when bound to this prompt.
                _choice_fallback = response + "\\n\\n" + "\\n".join(f"{choice['id']} — {choice['label']}" for choice in _choice_prompt["choices"]) + f"\\nReply with: choose {_choice_prompt['choice_prompt_id']} <choice_id[,choice_id...]>"
                _choice_adapter = self._adapter_for_source(source)
                if _choice_adapter and hasattr(_choice_adapter, "send_choice_prompt"):
                    _choice_result = await _choice_adapter.send_choice_prompt(
                        source.chat_id,
                        response,
                        _choice_prompt,
                        session_key,
                        source.user_id,
                        user_name=source.user_name,
                        chat_type=getattr(source, "chat_type", "group") or "group",
                        metadata=self._thread_metadata_for_source(source, self._reply_anchor_for_event(event)),
                    )
                    if getattr(_choice_result, "success", False):
                        return None
                response = _choice_fallback

            return response

        except Exception as e:'''
        if len(final_return.findall(source)) != 1:
            raise InstallError("gateway chooser delivery return anchor is not unique")
        source = final_return.sub(lambda _: choice_delivery, source, count=1)
    return source


def change_tui_prompt_test_source(source: str) -> str:
    source = _insert_after_unique_line(
        source,
        "import { composerPromptText } from '../lib/prompt.js'",
        (
            "import { choiceAction, choiceSubmitErrorDisposition } from '../components/prompts.js'",
            "import type { GatewayChoicePrompt } from '../gatewayTypes.js'",
        ),
        label="TUI chooser reducer test imports",
    )
    return source.rstrip() + '''


const singleChoicePrompt = {
  choice_mode: 'single',
  choice_prompt_id: '79df97c7-ff3d-4415-8b2e-dbe93bd10590',
  choices: [
    { description: 'Chat normally.', id: 'chat', label: 'Chat' },
    { description: 'Create a task.', id: 'task', label: 'Task' }
  ],
  expires_at: '2099-07-18T03:00:00Z',
  max_choices: 1,
  min_choices: 1,
  submit_label: 'Choose'
} satisfies GatewayChoicePrompt

describe('choiceAction', () => {
  it('has no initial default and submits only after navigation', () => {
    expect(choiceAction(singleChoicePrompt, null, [], '', { return: true })).toMatchObject({
      error: 'Choose at least 1.',
      kind: 'submit'
    })

    const moved = choiceAction(singleChoicePrompt, null, [], '', { downArrow: true })
    expect(moved).toEqual({ cursor: 0, kind: 'state', selected: [] })
    expect(choiceAction(singleChoicePrompt, 0, [], '', { return: true })).toEqual({
      ids: ['chat'],
      kind: 'submit'
    })
  })

  it('toggles multiple IDs with Space, enforces bounds, and cancels with Esc', () => {
    const multiple = { ...singleChoicePrompt, choice_mode: 'multiple', max_choices: 2 } satisfies GatewayChoicePrompt
    expect(choiceAction(multiple, 0, [], ' ', {})).toEqual({ cursor: 0, kind: 'state', selected: ['chat'] })
    expect(choiceAction(multiple, 0, ['chat'], '', { return: true })).toEqual({ ids: ['chat'], kind: 'submit' })
    expect(choiceAction(multiple, 0, ['chat'], '', { escape: true })).toEqual({ kind: 'cancel' })
  })

  it('keeps retryable busy failures and clears terminal prompt failures', () => {
    expect(choiceSubmitErrorDisposition(new Error('session busy')).clearPrompt).toBe(false)
    expect(choiceSubmitErrorDisposition(new Error('choice prompt is stale')).clearPrompt).toBe(true)
  })
})
'''


def change_desktop_prompts_test_source(source: str) -> str:
    source = _insert_after_unique_line(
        source,
        "import { clearClarifyRequest, setClarifyRequest } from './clarify'",
        (
            "import { $choiceRequest, choiceRequestFromPayload, clearChoiceRequest, setChoiceRequest } from './prompts'",
            "import { choiceKeyboardAction, choiceSubmitErrorDisposition } from '../components/prompt-overlays'",
        ),
        label="Desktop chooser store test imports",
    )
    return source.rstrip() + '''


const chooserPayload = {
  choice_mode: 'single' as const,
  choice_prompt_id: '79df97c7-ff3d-4415-8b2e-dbe93bd10590',
  choices: [
    { description: 'Chat normally.', id: 'chat', label: 'Chat' },
    { description: 'Create a task.', id: 'task', label: 'Task' }
  ],
  expires_at: '2099-07-18T03:00:00Z',
  max_choices: 1,
  min_choices: 1,
  submit_label: 'Choose'
}

describe('choice prompt store', () => {
  it('validates the complete payload and parks it by session', () => {
    const request = choiceRequestFromPayload(chooserPayload, 's2')
    expect(request?.choicePromptId).toBe(chooserPayload.choice_prompt_id)
    setChoiceRequest(request!)

    expect($choiceRequest.get()).toBeNull()
    $activeSessionId.set('s2')
    expect($choiceRequest.get()?.choices.map(choice => choice.id)).toEqual(['chat', 'task'])
  })

  it('does not let a stale prompt id clear the current session prompt', () => {
    setChoiceRequest(choiceRequestFromPayload(chooserPayload, 's1')!)
    clearChoiceRequest('s1', '483ad83b-2972-46fc-a839-b348b1487710')
    expect($choiceRequest.get()?.choicePromptId).toBe(chooserPayload.choice_prompt_id)

    clearChoiceRequest('s1', chooserPayload.choice_prompt_id)
    expect($choiceRequest.get()).toBeNull()
  })

  it('clears chooser state with the normal per-session turn cleanup', () => {
    setChoiceRequest(choiceRequestFromPayload(chooserPayload, 's1')!)
    clearAllPrompts('s1')
    expect($choiceRequest.get()).toBeNull()
  })

  it('moves from no default, toggles with Space, and submits with Enter', () => {
    const request = choiceRequestFromPayload(chooserPayload, 's1')!
    const moved = choiceKeyboardAction(request, null, [], 'ArrowDown')
    expect(moved).toEqual({ focusedIndex: 0, kind: 'state', selected: ['chat'] })
    expect(choiceKeyboardAction(request, 0, [], ' ')).toEqual({ focusedIndex: 0, kind: 'state', selected: ['chat'] })
    expect(choiceKeyboardAction(request, 0, ['chat'], 'Enter')).toEqual({ ids: ['chat'], kind: 'submit' })
  })

  it('keeps retryable failures and clears only terminal prompt failures', () => {
    expect(choiceSubmitErrorDisposition(new Error('session busy')).clearPrompt).toBe(false)
    expect(choiceSubmitErrorDisposition(new Error('choice prompt expired')).clearPrompt).toBe(true)
    expect(choiceSubmitErrorDisposition(new Error('gateway closed')).clearPrompt).toBe(false)
  })
})
'''


def change_slack_approval_test_source(source: str) -> str:
    return source.rstrip() + '''


def _chooser_prompt(*, expires_at="2099-07-18T03:00:00Z"):
    return {
        "choice_prompt_id": "79df97c7-ff3d-4415-8b2e-dbe93bd10590",
        "choice_mode": "single",
        "min_choices": 1,
        "max_choices": 1,
        "submit_label": "Choose",
        "expires_at": expires_at,
        "choices": [
            {"id": "chat", "label": "Chat", "description": "Chat normally."},
            {"id": "task", "label": "Task", "description": "Create a task."},
        ],
    }


class TestSlackChoicePrompt:
    @pytest.mark.asyncio
    async def test_new_prompt_revokes_old_buttons_in_the_same_context(self):
        adapter = _make_adapter()
        client = adapter._team_clients["T1"]
        client.chat_postMessage = AsyncMock(side_effect=[{"ts": "1.0"}, {"ts": "2.0"}])

        for _ in range(2):
            result = await adapter.send_choice_prompt(
                "C1", "Choose one", _chooser_prompt(), "session", "U1",
                metadata={"thread_id": "root"},
            )
            assert result.success is True

        assert "1.0" not in adapter._choice_prompts
        assert "2.0" in adapter._choice_prompts
        assert adapter._choice_reply_prompts[("C1", "root", "U1")] == "2.0"

    def test_expired_prompt_is_cleaned_and_does_not_consume_normal_text(self):
        adapter = _make_adapter()
        key = ("C1", "root", "U1")
        adapter._choice_prompts["1.0"] = {
            "context_key": key,
            "prompt": _chooser_prompt(expires_at="2000-01-01T00:00:00Z"),
        }
        adapter._choice_reply_prompts[key] = "1.0"

        assert adapter._consume_choice_reply("C1", "root", "U1", "ordinary message") == (False, None)
        assert adapter._choice_prompts == {}
        assert adapter._choice_reply_prompts == {}

    @pytest.mark.asyncio
    async def test_action_requires_the_published_thread_binding(self):
        adapter = _make_adapter()
        _attach_auth_runner(adapter)
        key = ("C1", "root", "U1")
        adapter._choice_prompts["1.0"] = {
            "channel_id": "C1",
            "chat_type": "group",
            "context_key": key,
            "prompt": _chooser_prompt(),
            "session_key": "session",
            "thread_ts": "root",
            "user_id": "U1",
            "user_name": "user",
        }
        adapter._choice_reply_prompts[key] = "1.0"
        adapter._dispatch_choice_submission = AsyncMock()

        await adapter._handle_choice_action(
            AsyncMock(),
            {"channel": {"id": "C1"}, "message": {"thread_ts": "wrong", "ts": "1.0"}, "user": {"id": "U1", "name": "user"}},
            {"action_id": "forge_choice_button", "block_id": "forge_choice:79df97c7-ff3d-4415-8b2e-dbe93bd10590", "value": "chat"},
        )

        assert "1.0" in adapter._choice_prompts
        adapter._dispatch_choice_submission.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_multi_select_waits_for_the_explicit_submit_button(self):
        adapter = _make_adapter()
        _attach_auth_runner(adapter)
        key = ("C1", "root", "U1")
        prompt = {**_chooser_prompt(), "choice_mode": "multiple", "max_choices": 2}
        adapter._choice_prompts["1.0"] = {
            "channel_id": "C1",
            "chat_type": "group",
            "context_key": key,
            "prompt": prompt,
            "session_key": "session",
            "thread_ts": "root",
            "user_id": "U1",
            "user_name": "user",
        }
        adapter._choice_reply_prompts[key] = "1.0"
        adapter._dispatch_choice_submission = AsyncMock()
        body = {"channel": {"id": "C1"}, "message": {"thread_ts": "root", "ts": "1.0"}, "user": {"id": "U1", "name": "user"}}
        block_id = "forge_choice:79df97c7-ff3d-4415-8b2e-dbe93bd10590"

        await adapter._handle_choice_action(
            AsyncMock(), body,
            {"action_id": "forge_choice_multi", "block_id": block_id, "selected_options": [{"value": "chat"}, {"value": "task"}]},
        )
        assert adapter._choice_prompts["1.0"]["selected_ids"] == ["chat", "task"]
        adapter._dispatch_choice_submission.assert_not_awaited()

        await adapter._handle_choice_action(
            AsyncMock(), body,
            {"action_id": "forge_choice_submit", "block_id": block_id, "value": prompt["choice_prompt_id"]},
        )
        adapter._dispatch_choice_submission.assert_awaited_once()
        assert adapter._dispatch_choice_submission.await_args.args[1] == ["chat", "task"]
        assert "1.0" not in adapter._choice_prompts
'''


_CHANGES: dict[str, Callable[[str], str]] = {
    "hermes_cli/plugins.py": change_plugins_source,
    "agent/conversation_loop.py": change_conversation_source,
    "agent/tool_executor.py": change_tool_executor_source,
    "run_agent.py": change_run_agent_source,
    "cli.py": change_cli_source,
    "tui_gateway/server.py": change_tui_gateway_source,
    "ui-tui/src/gatewayTypes.ts": change_tui_gateway_types_source,
    "ui-tui/src/app/createGatewayEventHandler.ts": change_tui_event_handler_source,
    "ui-tui/src/app/overlayStore.ts": change_tui_overlay_store_source,
    "ui-tui/src/app/submissionCore.ts": change_tui_submission_source,
    "ui-tui/src/app/useSessionLifecycle.ts": change_tui_session_lifecycle_source,
    "ui-tui/src/components/prompts.tsx": change_tui_prompts_source,
    "ui-tui/src/components/appOverlays.tsx": change_tui_app_overlays_source,
    "apps/desktop/src/lib/chat-messages.ts": change_desktop_chat_messages_source,
    "apps/desktop/src/store/prompts.ts": change_desktop_prompts_store_source,
    "apps/desktop/src/app/session/hooks/use-message-stream/gateway-event.ts": change_desktop_gateway_event_source,
    "apps/desktop/src/components/prompt-overlays.tsx": change_desktop_prompt_overlays_source,
    "apps/desktop/src/app/session/hooks/use-prompt-actions/submit.ts": change_desktop_submit_source,
    "plugins/platforms/slack/adapter.py": change_slack_adapter_source,
    "gateway/run.py": change_gateway_source,
    "ui-tui/src/__tests__/prompt.test.ts": change_tui_prompt_test_source,
    "apps/desktop/src/store/prompts.test.ts": change_desktop_prompts_test_source,
    "tests/gateway/test_slack_approval_buttons.py": change_slack_approval_test_source,
}
_CHANGE_TARGETS = _load_change_targets()
if tuple(_CHANGES) != _CHANGE_TARGETS:
    raise InstallError("Hermes change target manifest does not match installer transforms")


def _write_atomic(path: Path, content: bytes, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        # RISK(data-loss): Windows indexers and virus scanners can briefly hold
        # a target open. Retry only PermissionError; all other replace errors
        # still stop immediately and leave the original target untouched.
        for attempt in range(3):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.05 * (attempt + 1))
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _manifest_payload(manifest: ChangeManifest) -> dict[str, object]:
    return {
        "source_version": manifest.source_version,
        "files": [asdict(item) for item in manifest.files],
    }


def _read_manifest(package: Path) -> ChangeManifest:
    manifest_path = package / _MANIFEST_NAME
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InstallError("installed files list is missing or invalid") from error
    if not isinstance(payload, dict) or set(payload) != {"source_version", "files"}:
        raise InstallError("installed files list has invalid fields")
    source_version = payload.get("source_version")
    raw_files = payload.get("files")
    if not isinstance(source_version, str) or not source_version.strip():
        raise InstallError("source_version must be a non-empty string")
    if not isinstance(raw_files, list) or not raw_files:
        raise InstallError("files must be a non-empty array")
    required = {
        "path",
        "before_file_hash",
        "after_file_hash",
        "release_file",
        "restore_file",
    }
    files: list[ChangedSourceFile] = []
    for raw in raw_files:
        if not isinstance(raw, dict) or set(raw) != required:
            raise InstallError("installed file entry has invalid fields")
        if any(not isinstance(raw[key], str) or not raw[key] for key in required):
            raise InstallError("installed file entry must contain non-empty strings")
        item = ChangedSourceFile(**raw)
        if not _SHA256_PATTERN.fullmatch(item.before_file_hash) or not (
            _SHA256_PATTERN.fullmatch(item.after_file_hash)
        ):
            raise InstallError("installed file entry has an invalid file hash")
        if item.release_file != f"{_RELEASE_DIR}/{item.path}" or (
            item.restore_file != f"{_RESTORE_DIR}/{item.path}"
        ):
            raise InstallError(f"installed file entry has an invalid package path: {item.path}")
        files.append(item)
    if tuple(item.path for item in files) != _CHANGE_TARGETS:
        raise InstallError("installed files list has unexpected target paths")
    return ChangeManifest(source_version=source_version, files=tuple(files))


def build_change_package(
    hermes_root: Path,
    package: Path,
    *,
    source_version: str,
) -> ChangeManifest:
    """Build a hash-bound release and restore package from one clean checkout."""

    hermes_root = hermes_root.resolve()
    package = package.resolve()
    if package.exists() and any(package.iterdir()):
        raise InstallError("change package directory must be empty")
    package.mkdir(parents=True, exist_ok=True)
    files: list[ChangedSourceFile] = []
    for relative_path, transform in _CHANGES.items():
        source_path = hermes_root / relative_path
        try:
            original_bytes = source_path.read_bytes()
            original = original_bytes.decode("utf-8")
        except OSError as error:
            raise InstallError(f"cannot read Hermes source: {relative_path}") from error
        changed = transform(original)
        release_path = package / _RELEASE_DIR / relative_path
        restore_path = package / _RESTORE_DIR / relative_path
        _write_atomic(release_path, changed.encode("utf-8"))
        _write_atomic(restore_path, original_bytes)
        files.append(
            ChangedSourceFile(
                path=relative_path,
                before_file_hash=file_hash(source_path),
                after_file_hash=file_hash(release_path),
                release_file=release_path.relative_to(package).as_posix(),
                restore_file=restore_path.relative_to(package).as_posix(),
            )
        )
    manifest = ChangeManifest(source_version=source_version, files=tuple(files))
    encoded = json.dumps(
        _manifest_payload(manifest),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    _write_atomic(package / _MANIFEST_NAME, encoded)
    return manifest


def _manifest_hash(manifest: ChangeManifest) -> str:
    encoded = json.dumps(
        _manifest_payload(manifest),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_change_state(hermes_root: Path, manifest: ChangeManifest) -> str | None:
    path = hermes_root / _STATE_NAME
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise InstallError("change state file is unsafe")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InstallError("change state file is invalid") from error
    if not isinstance(payload, dict) or set(payload) != {"operation", "manifest_hash"}:
        raise InstallError("change state file has invalid fields")
    operation = payload.get("operation")
    manifest_hash = payload.get("manifest_hash")
    if operation not in {"install", "restore"} or manifest_hash != _manifest_hash(manifest):
        raise InstallError("change state file does not match this package")
    return operation


def _write_change_state(
    hermes_root: Path, manifest: ChangeManifest, *, operation: str
) -> None:
    payload = json.dumps(
        {"operation": operation, "manifest_hash": _manifest_hash(manifest)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        # RISK(data-loss): this durable marker is written before the first
        # source replacement so a killed process can resume a mixed state.
        _write_atomic(hermes_root / _STATE_NAME, payload)
    except OSError as error:
        raise InstallError("cannot write durable change state") from error


def _clear_change_state(hermes_root: Path) -> None:
    path = hermes_root / _STATE_NAME
    try:
        path.unlink(missing_ok=True)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except OSError as error:
        raise InstallError("cannot clear durable change state") from error


@contextmanager
def _change_lock(hermes_root: Path) -> Iterator[None]:
    """Hold one OS lock for all source changes targeting the same Hermes root."""

    normalized = str(hermes_root.resolve())
    if os.name == "nt":
        normalized = normalized.casefold()
    identity = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    lock_path = Path(tempfile.gettempdir()) / f"infinity-forge-hermes-{identity}.lock"
    try:
        lock_file = lock_path.open("a+b")
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
            os.fsync(lock_file.fileno())
        lock_file.seek(0)
        # RISK(race): install and restore share this non-blocking OS lock so two
        # writers cannot overwrite the durable journal or source files together.
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        try:
            lock_file.close()
        except (NameError, OSError):
            pass
        raise InstallError("another Hermes source change is already running") from error
    try:
        yield
    finally:
        try:
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def _validate_all_package_files(package: Path, manifest: ChangeManifest) -> None:
    for item in manifest.files:
        for package_field, hash_field in (
            ("release_file", "after_file_hash"),
            ("restore_file", "before_file_hash"),
        ):
            packaged = package / getattr(item, package_field)
            if not packaged.resolve().is_relative_to(package) or packaged.is_symlink():
                raise InstallError(f"unsafe package path: {item.path}")
            if file_hash(packaged) != getattr(item, hash_field):
                raise InstallError(f"{hash_field} mismatch in package: {item.path}")


def _read_known_target_states(
    hermes_root: Path,
    manifest: ChangeManifest,
    *,
    required_start_hash: str,
) -> dict[str, str]:
    states: dict[str, str] = {}
    for item in manifest.files:
        target = hermes_root / item.path
        if not target.resolve().is_relative_to(hermes_root) or target.is_symlink():
            raise InstallError(f"unsafe Hermes target path: {item.path}")
        actual = file_hash(target)
        if actual == item.before_file_hash:
            states[item.path] = "before"
        elif actual == item.after_file_hash:
            states[item.path] = "after"
        else:
            raise InstallError(f"{required_start_hash} mismatch: {item.path}")
    return states


def _write_desired_files(
    hermes_root: Path,
    package: Path,
    manifest: ChangeManifest,
    *,
    states: dict[str, str],
    desired_state: str,
    package_field: str,
) -> None:
    for item in manifest.files:
        if states[item.path] == desired_state:
            continue
        target = hermes_root / item.path
        mode = target.stat().st_mode
        packaged = package / getattr(item, package_field)
        _write_atomic(target, packaged.read_bytes(), mode=mode)


def _replace_from_package(
    hermes_root: Path,
    package: Path,
    manifest: ChangeManifest,
    *,
    package_field: str,
) -> None:
    for item in manifest.files:
        target = hermes_root / item.path
        mode = target.stat().st_mode
        packaged = package / getattr(item, package_field)
        _write_atomic(target, packaged.read_bytes(), mode=mode)


def _verify_target_hashes(
    hermes_root: Path,
    manifest: ChangeManifest,
    hash_field: str,
) -> None:
    for item in manifest.files:
        if file_hash(hermes_root / item.path) != getattr(item, hash_field):
            raise InstallError(f"{hash_field} mismatch: {item.path}")


def _install_change_unlocked(hermes_root: Path, package: Path) -> ChangeManifest:
    """Install every carried source file only after all package hashes pass."""

    hermes_root = hermes_root.resolve()
    package = package.resolve()
    manifest = _read_manifest(package)
    _read_change_state(hermes_root, manifest)
    # RISK(data-loss): validate both the forward and recovery bytes before any
    # target write; an error path must never consume unverified restore data.
    _validate_all_package_files(package, manifest)
    states = _read_known_target_states(
        hermes_root, manifest, required_start_hash="before_file_hash"
    )
    if all(state == "after" for state in states.values()):
        _clear_change_state(hermes_root)
        return manifest
    _write_change_state(hermes_root, manifest, operation="install")
    try:
        _write_desired_files(
            hermes_root,
            package,
            manifest,
            states=states,
            desired_state="after",
            package_field="release_file",
        )
        _verify_target_hashes(hermes_root, manifest, "after_file_hash")
    except Exception as error:
        # RISK(data-loss): a partial install restores every target from the
        # package produced from this exact checkout before surfacing the error.
        try:
            _validate_all_package_files(package, manifest)
            _replace_from_package(
                hermes_root, package, manifest, package_field="restore_file"
            )
            _verify_target_hashes(hermes_root, manifest, "before_file_hash")
            _clear_change_state(hermes_root)
        except Exception as restore_error:
            raise InstallError(
                "Hermes source install failed and could not be restored"
            ) from restore_error
        raise InstallError("Hermes source install failed and was restored") from error
    _clear_change_state(hermes_root)
    return manifest


def _restore_change_unlocked(hermes_root: Path, package: Path) -> ChangeManifest:
    """Restore originals only when installed files still match the package."""

    hermes_root = hermes_root.resolve()
    package = package.resolve()
    manifest = _read_manifest(package)
    _read_change_state(hermes_root, manifest)
    _validate_all_package_files(package, manifest)
    states = _read_known_target_states(
        hermes_root, manifest, required_start_hash="after_file_hash"
    )
    if all(state == "before" for state in states.values()):
        _clear_change_state(hermes_root)
        return manifest
    _write_change_state(hermes_root, manifest, operation="restore")
    try:
        _write_desired_files(
            hermes_root,
            package,
            manifest,
            states=states,
            desired_state="before",
            package_field="restore_file",
        )
        _verify_target_hashes(hermes_root, manifest, "before_file_hash")
    except Exception as error:
        # RISK(data-loss): keep Hermes on one complete version if restore is
        # interrupted; reinstall the already-validated release package.
        try:
            _validate_all_package_files(package, manifest)
            _replace_from_package(
                hermes_root, package, manifest, package_field="release_file"
            )
            _verify_target_hashes(hermes_root, manifest, "after_file_hash")
            _clear_change_state(hermes_root)
        except Exception as reinstall_error:
            raise InstallError(
                "Hermes source restore failed and release could not be reinstalled"
            ) from reinstall_error
        raise InstallError(
            "Hermes source restore failed and release was reinstalled"
        ) from error
    _clear_change_state(hermes_root)
    return manifest


def install_change(hermes_root: Path, package: Path) -> ChangeManifest:
    """Install one carried change with an exclusive per-checkout writer lock."""

    resolved_root = hermes_root.resolve()
    with _change_lock(resolved_root):
        return _install_change_unlocked(resolved_root, package)


def restore_change(hermes_root: Path, package: Path) -> ChangeManifest:
    """Restore one carried change with an exclusive per-checkout writer lock."""

    resolved_root = hermes_root.resolve()
    with _change_lock(resolved_root):
        return _restore_change_unlocked(resolved_root, package)


def verify_change(hermes_root: Path, package: Path) -> ChangeManifest:
    """Verify every installed target and both package copies without writing."""

    resolved_root = hermes_root.resolve()
    resolved_package = package.resolve()
    with _change_lock(resolved_root):
        manifest = _read_manifest(resolved_package)
        if _read_change_state(resolved_root, manifest) is not None:
            raise InstallError("Hermes source change has an unfinished operation")
        _validate_all_package_files(resolved_package, manifest)
        _verify_target_hashes(resolved_root, manifest, "after_file_hash")
        return manifest
