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


_CONVERSATION_HOOK = f'''
    if is_user_turn:
        # {_HOOK_MARKER}: only a caller that received a real user turn opts in.
        # Internal build, review, delegate, and batch calls keep the safe default.
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
                    getattr(agent, "_gateway_session_key", "")
                    or task_id
                    or "local-session"
                ),
                "user_id": str(
                    getattr(agent, "_user_id", "")
                    or getattr(agent, "_gateway_user_id", "")
                    or getattr(agent, "user_id", "")
                    or os.environ.get("HERMES_USER_ID", "")
                    or "local-user"
                ),
                "surface": str(
                    getattr(agent, "platform", "")
                    or os.environ.get("HERMES_SESSION_SOURCE", "cli")
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
            _choice_mode = _handled_result.get("choice_mode")
            _min_choices = _handled_result.get("min_choices")
            _max_choices = _handled_result.get("max_choices")
            _valid_choice_prompt = (
                not _has_choice_prompt
                or (
                    all(
                        _field in _handled_result
                        for _field in _choice_prompt_fields
                    )
                    and isinstance(_handled_result.get("choice_prompt_id"), str)
                    and bool(_handled_result["choice_prompt_id"].strip())
                    and _choice_mode in ("single", "multiple")
                    and isinstance(_min_choices, int)
                    and not isinstance(_min_choices, bool)
                    and _min_choices >= 1
                    and (
                        _max_choices is None
                        or (
                            isinstance(_max_choices, int)
                            and not isinstance(_max_choices, bool)
                            and _max_choices >= _min_choices
                        )
                    )
                    and (
                        _choice_mode != "single"
                        or (_min_choices == 1 and _max_choices == 1)
                    )
                    and isinstance(_handled_result.get("submit_label"), str)
                    and bool(_handled_result["submit_label"].strip())
                    and isinstance(_handled_result.get("expires_at"), str)
                    and bool(_handled_result["expires_at"].strip())
                    and _min_choices <= len(_choices)
                    and (
                        _max_choices is None
                        or _max_choices <= len(_choices)
                    )
                )
            )
            if (
                not isinstance(_response, str)
                or not _valid_choices
                or not _valid_choice_prompt
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


def change_run_agent_source(source: str) -> str:
    """Forward the explicit user-transport flag while defaulting internal calls off."""

    if _HOOK_MARKER in source:
        raise InstallError("run_agent.py user-turn forwarding is already installed")
    source = _insert_after_unique_line(
        source,
        "moa_config: Optional[dict[str, Any]] = None,",
        (
            f"# {_HOOK_MARKER}: internal calls remain False unless a user surface opts in.",
            "# RISK(breaking): this optional public argument defaults off for every existing caller.",
            "is_user_turn: bool = False,",
        ),
        label="run_agent.py signature",
    )
    return _insert_after_unique_line(
        source,
        "moa_config=moa_config,",
        ("is_user_turn=is_user_turn,",),
        label="run_agent.py forwarding",
    )


_CLI_CHOICE_METHODS = '''
# RISK(breaking): classic CLI submissions carry stable IDs, never display labels.
def _prompt_choice_modal(self, prompt: dict, timeout: float = 120) -> dict | None:
    """Return one structured chooser submission, or ``None`` on cancel/timeout."""
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
        return None
    choice_mode = prompt.get("choice_mode")
    choices = prompt.get("choices")
    if choice_mode not in ("single", "multiple") or not isinstance(choices, list):
        return None
    if not getattr(self, "_app", None):
        return None
    if not getattr(sys.stdin, "isatty", lambda: False)():
        return None
    if not getattr(sys.stdout, "isatty", lambda: False)():
        return None
    modal_choices = [
        (choice["id"], choice["label"], choice.get("description", ""))
        for choice in choices
        if isinstance(choice, dict)
        and isinstance(choice.get("id"), str)
        and isinstance(choice.get("label"), str)
        and isinstance(choice.get("description", ""), str)
    ]
    if len(modal_choices) != len(choices) or not modal_choices:
        return None
    min_choices = prompt.get("min_choices")
    max_choices = prompt.get("max_choices")
    if (
        not isinstance(min_choices, int)
        or isinstance(min_choices, bool)
        or min_choices < 1
        or (
            max_choices is not None
            and (
                not isinstance(max_choices, int)
                or isinstance(max_choices, bool)
                or max_choices < min_choices
            )
        )
    ):
        return None
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
):
    """Resolve bounded handled choosers through the same user-turn hook path."""
    prompt_fields = (
        "choice_prompt_id",
        "choice_mode",
        "min_choices",
        "max_choices",
        "submit_label",
        "expires_at",
        "choices",
    )
    for _choice_turn in range(16):
        if (
            not isinstance(result, dict)
            or not result.get("handled")
            or any(field not in result for field in prompt_fields)
        ):
            return result
        submission = self._prompt_choice_modal(result)
        if submission is None:
            return result
        result = self.agent.run_conversation(
            user_message=submission,
            conversation_history=result.get("messages", conversation_history),
            stream_callback=stream_callback,
            task_id=task_id,
            is_user_turn=True,
            persist_user_message=None,
            moa_config=moa_config,
        )
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
        f'        {result_name}["final_response"] = {response_name}',
    )


def change_cli_source(source: str) -> str:
    """Mark classic CLI input as a user turn and skip handled-turn title calls."""

    if _HOOK_MARKER in source:
        raise InstallError("cli.py user-turn handling is already installed")
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
        expected=")",
        addition=(
            "result = self._continue_choice_modal_result(",
            "    result,",
            "    conversation_history=self.conversation_history[:-1],",
            "    stream_callback=stream_callback,",
            "    task_id=self.session_id,",
            "    moa_config=_moa_cfg,",
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
        block_start="def _get_slash_confirm_display_fragments(self):",
        expected='selected = state.get("selected", 0)',
        addition=(
            'choice_mode = state.get("choice_mode", "single")',
            'selected_ids = state.get("selected_ids") or set()',
            "instructions = self._choice_modal_instructions(state)",
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
    source = _insert_after_unique_line(
        source,
        '"stream_callback": _stream,',
        (
            f"# {_HOOK_MARKER}: Desktop/TUI submitted this user turn.",
            '"is_user_turn": True,',
        ),
        label="tui gateway user-turn call",
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
    return _insert_after_unique_line(
        source,
        'payload = {"text": raw, "usage": _get_usage(agent), "status": status}',
        (
            "# RISK(breaking): Desktop choice buttons depend on this additive payload field.",
            (
                '_pre_user_turn_choices = result.get("choices", []) '
                "if isinstance(result, dict) else []"
            ),
            "if _pre_user_turn_choices:",
            '    payload["choices"] = list(_pre_user_turn_choices)',
        ),
        label="tui gateway chooser payload",
    )


def change_gateway_source(source: str) -> str:
    """Mark messaging input and suppress handled-turn title and goal models."""

    if _HOOK_MARKER in source:
        raise InstallError("gateway/run.py user-turn handling is already installed")
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
        ),
        label="gateway handled result forwarding",
    )
    source = _replace_unique_line(
        source,
        "if _final_text.strip():",
        'if _final_text.strip() and not (isinstance(_agent_result, dict) and _agent_result.get("handled")):',
        label="gateway handled goal guard",
    )
    return _insert_after_unique_line(
        source,
        'response = agent_result.get("final_response") or ""',
        _choice_display_lines("agent_result", "response"),
        label="gateway chooser display",
    )


_CHANGES: dict[str, Callable[[str], str]] = {
    "hermes_cli/plugins.py": change_plugins_source,
    "agent/conversation_loop.py": change_conversation_source,
    "run_agent.py": change_run_agent_source,
    "cli.py": change_cli_source,
    "tui_gateway/server.py": change_tui_gateway_source,
    "gateway/run.py": change_gateway_source,
}


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
    if len(files) != len(_CHANGES) or {item.path for item in files} != set(_CHANGES):
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
