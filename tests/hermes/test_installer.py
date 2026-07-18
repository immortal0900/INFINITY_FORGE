from __future__ import annotations

import json
import queue
from pathlib import Path

import pytest

import forge.hermes_change.installer as installer
from forge.hermes_change.installer import (
    InstallError,
    build_change_package,
    file_hash,
    install_change,
    restore_change,
)


PLUGIN_SOURCE = '''VALID_HOOKS: set[str] = {
    "pre_gateway_dispatch",
}
'''

CONVERSATION_SOURCE = '''from typing import Any, Dict, List, Optional
import logging
import os
logger = logging.getLogger(__name__)

def run_conversation(
    agent,
    user_message: str,
    system_message: str = None,
    conversation_history: List[Dict[str, Any]] = None,
    task_id: str = None,
    stream_callback=None,
    persist_user_message: Optional[str] = None,
    persist_user_timestamp: Optional[float] = None,
    moa_config: Optional[dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run one turn."""
    if moa_config is None:
        return {"seen": user_message}
'''

RUN_AGENT_SOURCE = '''from typing import Any, Dict, List, Optional

class AIAgent:
    def run_conversation(
        self,
        user_message: str,
        system_message: str = None,
        conversation_history: List[Dict[str, Any]] = None,
        task_id: str = None,
        stream_callback: Optional[callable] = None,
        persist_user_message: Optional[str] = None,
        persist_user_timestamp: Optional[float] = None,
        moa_config: Optional[dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        from agent.conversation_loop import run_conversation
        return run_conversation(
            self,
            user_message,
            system_message,
            conversation_history,
            task_id,
            stream_callback,
            persist_user_message,
            persist_user_timestamp=persist_user_timestamp,
            moa_config=moa_config,
        )
'''

CLI_SOURCE = '''class ModalShell:
    def _prompt_text_input_modal(
        self,
        *,
        title: str,
        detail: str,
        choices: list[tuple[str, str, str]],
        timeout: float = 120,
    ) -> str | None:
        if not choices:
            return None
        response_queue = queue.Queue()

        def _setup_modal() -> None:
            self._capture_modal_input_snapshot()
            self._slash_confirm_state = {
                "title": title,
                "detail": detail,
                "choices": choices,
                "selected": 0,
                "response_queue": response_queue,
            }
            self._slash_confirm_deadline = timeout

        _setup_modal()
        return response_queue.get()

    def _submit_slash_confirm_response(self, value: str | None) -> None:
        state = self._slash_confirm_state
        if not state:
            return
        state["response_queue"].put(value)
        self._slash_confirm_state = None
        self._slash_confirm_deadline = 0

    def _get_slash_confirm_display_fragments(self):
        state = self._slash_confirm_state
        if not state:
            return []
        choices = state.get("choices") or []
        selected = state.get("selected", 0)
        preview_lines = []
        for idx, (_value, label, desc) in enumerate(choices):
            marker = "❯" if idx == selected else " "
            preview_lines.append(f"{marker} [{idx + 1}] {label} — {desc}")
        choice_wrapped = []
        for idx, (_value, label, desc) in enumerate(choices):
            marker = "❯" if idx == selected else " "
            choice_wrapped.append((idx, f"{marker} [{idx + 1}] {label} — {desc}"))
        preview_lines.append("Type 1/2/3 or use ↑/↓ then Enter. ESC/Ctrl+C cancels.")
        lines = []
        _append_panel_line(lines, 'class:approval-border', 'class:approval-cmd', 'Type 1/2/3 or use ↑/↓ then Enter. ESC/Ctrl+C cancels.', box_width)
        return lines

    def run(self, kb, Condition):
        def handle_enter(event):
            # --- Slash-command confirmation: submit typed or highlighted choice ---
            if self._slash_confirm_state:
                text = event.app.current_buffer.text.strip()
                choices = self._slash_confirm_state.get("choices") or []
                choice = self._normalize_slash_confirm_choice(text, choices) if text else None
                if choice is None:
                    selected = self._slash_confirm_state.get("selected", 0)
                    if 0 <= selected < len(choices):
                        choice = choices[selected][0]
                self._submit_slash_confirm_response(choice or "cancel")
                event.app.current_buffer.reset()
                event.app.invalidate()
                return

        # --- Slash-command confirmation: arrow-key navigation ---
        @kb.add('up', filter=Condition(lambda: bool(self._slash_confirm_state)))
        def slash_confirm_up(event):
            if self._slash_confirm_state:
                self._slash_confirm_state["selected"] = max(0, self._slash_confirm_state.get("selected", 0) - 1)
                event.app.invalidate()

        @kb.add('down', filter=Condition(lambda: bool(self._slash_confirm_state)))
        def slash_confirm_down(event):
            if self._slash_confirm_state:
                max_idx = len(self._slash_confirm_state.get("choices") or []) - 1
                self._slash_confirm_state["selected"] = min(max_idx, self._slash_confirm_state.get("selected", 0) + 1)
                event.app.invalidate()

        def _make_slash_confirm_number_handler(idx):
            def handler(event):
                if self._slash_confirm_state and idx < len(self._slash_confirm_state.get("choices") or []):
                    choice = self._slash_confirm_state["choices"][idx][0]
                    self._submit_slash_confirm_response(choice)
                    event.app.current_buffer.reset()
                    event.app.invalidate()
            return handler

        _modal_prompt_active = Condition(
            lambda: bool(self._secret_state or self._sudo_state or self._slash_confirm_state)
        )

        @kb.add('escape', filter=_modal_prompt_active, eager=True)
        def handle_escape_modal(event):
            if self._slash_confirm_state:
                self._submit_slash_confirm_response("cancel")
                event.app.current_buffer.reset()
                event.app.invalidate()
                return


def unrelated_call(self):
    schedule(
        task_id=self.session_id,
    )

def process(self, agent_message, message, stream_callback):
    _moa_cfg = None
    result = self.agent.run_conversation(
        user_message=agent_message,
        conversation_history=self.conversation_history[:-1],
        stream_callback=stream_callback,
        task_id=self.session_id,
        persist_user_message=message,
        moa_config=_moa_cfg,
    )
    response = result.get("final_response", "") if result else ""
    if response and result and not result.get("failed") and not result.get("partial"):
        maybe_auto_title()
    return result
'''

TUI_GATEWAY_SOURCE = '''def process(agent, history, _stream, session, text, raw, status):
    run_kwargs = {
        "conversation_history": list(history),
        "stream_callback": _stream,
    }
    result = agent.run_conversation(text, **run_kwargs)
    payload = {"text": raw, "usage": _get_usage(agent), "status": status}
    if status == "complete" and isinstance(raw, str) and raw.strip():
        evaluate_goal()
    if (
        status == "complete"
        and isinstance(raw, str)
        and raw.strip()
        and isinstance(text, str)
        and text.strip()
    ):
        maybe_auto_title()
    return payload
'''

GATEWAY_SOURCE = '''def deliver(agent_result):
    response = agent_result.get("final_response") or ""
    return response

def handle(self, event, source):
    _agent_result = self._handle_message_with_agent(event, source)
    _final_text = str(_agent_result.get("final_response") or "")
    if _final_text.strip():
        self._post_turn_goal_continuation()
    return _agent_result

def run(self, agent, agent_history, session_id, final_response):
    _conversation_kwargs = {
        "conversation_history": agent_history,
        "task_id": session_id,
    }
    result = agent.run_conversation("message", **_conversation_kwargs)
    result_holder = [result]
    if final_response and self._session_db:
        maybe_auto_title()
    return {
        "final_response": final_response,
        "last_reasoning": result.get("last_reasoning"),
        "messages": result_holder[0].get("messages", []) if result_holder[0] else [],
    }
'''


def _hermes_tree(root: Path) -> None:
    (root / "hermes_cli").mkdir(parents=True)
    (root / "agent").mkdir(parents=True)
    (root / "hermes_cli" / "plugins.py").write_text(
        PLUGIN_SOURCE, encoding="utf-8"
    )
    (root / "agent" / "conversation_loop.py").write_text(
        CONVERSATION_SOURCE, encoding="utf-8"
    )
    (root / "run_agent.py").write_text(RUN_AGENT_SOURCE, encoding="utf-8")
    (root / "cli.py").write_text(CLI_SOURCE, encoding="utf-8")
    (root / "tui_gateway").mkdir(parents=True)
    (root / "tui_gateway" / "server.py").write_text(
        TUI_GATEWAY_SOURCE, encoding="utf-8"
    )
    (root / "gateway").mkdir(parents=True)
    (root / "gateway" / "run.py").write_text(GATEWAY_SOURCE, encoding="utf-8")


def test_carried_change_targets_user_surfaces_and_forwarder(tmp_path: Path) -> None:
    root = tmp_path / "hermes"
    package = tmp_path / "change-package"
    _hermes_tree(root)

    manifest = build_change_package(root, package, source_version="0.18.2-test")

    assert {item.path for item in manifest.files} == {
        "hermes_cli/plugins.py",
        "agent/conversation_loop.py",
        "run_agent.py",
        "cli.py",
        "tui_gateway/server.py",
        "gateway/run.py",
    }


def test_user_surfaces_opt_in_and_handled_turns_skip_model_followups() -> None:
    changed_forwarder = installer.change_run_agent_source(RUN_AGENT_SOURCE)
    changed_cli = installer.change_cli_source(CLI_SOURCE)
    changed_tui = installer.change_tui_gateway_source(TUI_GATEWAY_SOURCE)
    changed_gateway = installer.change_gateway_source(GATEWAY_SOURCE)

    assert "is_user_turn: bool = False" in changed_forwarder
    assert "is_user_turn=is_user_turn" in changed_forwarder
    assert "is_user_turn=True" in changed_cli
    assert '"is_user_turn": True' in changed_tui
    assert '"is_user_turn": True' in changed_gateway
    assert 'not result.get("handled")' in changed_cli
    assert changed_tui.count('result.get("handled")') >= 2
    assert changed_gateway.count('get("handled"') >= 3

    for changed in (changed_forwarder, changed_cli, changed_tui, changed_gateway):
        compile(changed, "<changed Hermes source>", "exec")


def test_cli_displays_choice_labels_without_changing_stable_ids() -> None:
    namespace: dict[str, object] = {"maybe_auto_title": lambda: None}
    exec(installer.change_cli_source(CLI_SOURCE), namespace)
    choices = [
        {"id": "chat", "label": "Chat"},
        {"id": "task", "label": "Task"},
    ]

    class Agent:
        @staticmethod
        def run_conversation(**kwargs):
            assert kwargs["is_user_turn"] is True
            return {
                "final_response": "Choose one.",
                "choices": choices,
                "handled": True,
            }

    cli = namespace["ModalShell"]()
    cli.agent = Agent()
    cli.conversation_history = ["current"]
    cli.session_id = "session-1"
    result = namespace["process"](cli, "request", "request", None)

    assert result["choices"] == choices
    assert "- Chat" in result["final_response"]
    assert "- Task" in result["final_response"]


def test_cli_carries_a_generic_keyboard_choice_modal_without_raw_readers() -> None:
    changed = installer.change_cli_source(CLI_SOURCE)

    assert "def _prompt_choice_modal(" in changed
    assert "def _toggle_choice_modal_selection(" in changed
    assert "def _submit_choice_modal_selection(" in changed
    assert "@kb.add(' '," in changed
    assert '"choice_mode": choice_mode' in changed
    assert '"selected": initial_selected' in changed
    assert "_capture_modal_input_snapshot()" in changed
    assert "_restore_modal_input_snapshot()" not in changed.split(
        "def _prompt_choice_modal(", 1
    )[1].split("def _prompt_text_input_modal(", 1)[0]
    generic = changed.split("def _prompt_choice_modal(", 1)[1].split(
        "def _prompt_text_input_modal(", 1
    )[0]
    assert "curses" not in generic
    assert "input(" not in generic
    assert "_prompt_text_input(" not in generic
    assert changed.count("def _prompt_text_input_modal(") == 1
    compile(changed, "<changed Hermes CLI>", "exec")


def test_multiple_choice_requires_space_toggle_before_done() -> None:
    namespace: dict[str, object] = {"queue": queue}
    exec(installer.change_cli_source(CLI_SOURCE), namespace)
    cli = namespace["ModalShell"]()
    response_queue = queue.Queue()
    cli._slash_confirm_state = {
        "structured_choice_modal": True,
        "choice_mode": "multiple",
        "min_choices": 1,
        "max_choices": None,
        "choices": [
            ("lint", "Lint", "Run lint."),
            ("tests", "Tests", "Run tests."),
        ],
        "selected": 0,
        "selected_ids": set(),
        "response_queue": response_queue,
    }
    cli._slash_confirm_deadline = 123
    cli._invalidate = lambda: None

    assert cli._submit_choice_modal_selection() is False
    assert response_queue.empty()
    assert cli._slash_confirm_state is not None

    cli._toggle_choice_modal_selection()

    assert cli._submit_choice_modal_selection() is True
    assert response_queue.get_nowait() == ["lint"]
    assert cli._slash_confirm_state is None


def test_cli_reenters_the_same_user_turn_path_with_stable_ids() -> None:
    namespace: dict[str, object] = {"queue": queue, "maybe_auto_title": lambda: None}
    exec(installer.change_cli_source(CLI_SOURCE), namespace)
    prompt_id = "79df97c7-ff3d-4415-8b2e-dbe93bd10590"
    calls: list[dict[str, object]] = []

    class Agent:
        @staticmethod
        def run_conversation(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return {
                    "final_response": "Choose mode.",
                    "messages": [],
                    "api_calls": 0,
                    "handled": True,
                    "choice_prompt_id": prompt_id,
                    "choice_mode": "single",
                    "min_choices": 1,
                    "max_choices": 1,
                    "submit_label": "Choose mode",
                    "expires_at": "2026-07-18T03:00:00Z",
                    "choices": [
                        {"id": "chat", "label": "Chat", "description": "Chat."},
                        {"id": "task", "label": "Task", "description": "Task."},
                    ],
                }
            assert kwargs["user_message"] == {
                "choice_prompt_id": prompt_id,
                "selected_choice_ids": ["task"],
            }
            assert kwargs["is_user_turn"] is True
            return {
                "final_response": "Choose task flow.",
                "messages": [],
                "api_calls": 0,
                "handled": True,
                "choices": [],
            }

    cli = namespace["ModalShell"]()
    cli.agent = Agent()
    cli.conversation_history = ["current"]
    cli.session_id = "session-1"
    cli._prompt_choice_modal = lambda _prompt: {
        "choice_prompt_id": prompt_id,
        "selected_choice_ids": ["task"],
    }

    result = namespace["process"](cli, "first input", "first input", None)

    assert len(calls) == 2
    assert result["api_calls"] == 0
    assert result["final_response"] == "Choose task flow."


def test_cli_modal_cancel_does_not_reenter_or_auto_select_first_choice() -> None:
    namespace: dict[str, object] = {"queue": queue, "maybe_auto_title": lambda: None}
    exec(installer.change_cli_source(CLI_SOURCE), namespace)
    prompt_id = "79df97c7-ff3d-4415-8b2e-dbe93bd10590"
    calls = 0

    class Agent:
        @staticmethod
        def run_conversation(**_kwargs):
            nonlocal calls
            calls += 1
            return {
                "final_response": "Choose mode.",
                "messages": [],
                "api_calls": 0,
                "handled": True,
                "choice_prompt_id": prompt_id,
                "choice_mode": "single",
                "min_choices": 1,
                "max_choices": 1,
                "submit_label": "Choose mode",
                "expires_at": "2026-07-18T03:00:00Z",
                "choices": [
                    {"id": "chat", "label": "Chat", "description": "Chat."},
                    {"id": "task", "label": "Task", "description": "Task."},
                ],
            }

    cli = namespace["ModalShell"]()
    cli.agent = Agent()
    cli.conversation_history = ["current"]
    cli.session_id = "session-1"
    cli._prompt_choice_modal = lambda _prompt: None

    result = namespace["process"](cli, "first input", "first input", None)

    assert calls == 1
    assert result["api_calls"] == 0
    assert result["choices"][0]["id"] == "chat"
    assert "- Chat [id: chat]" in result["final_response"]


def test_tui_transports_choice_objects_in_message_payload() -> None:
    namespace: dict[str, object] = {
        "evaluate_goal": lambda: None,
        "maybe_auto_title": lambda: None,
        "_get_usage": lambda agent: {},
    }
    exec(installer.change_tui_gateway_source(TUI_GATEWAY_SOURCE), namespace)
    choices = [
        {"id": "build", "label": "Build"},
        {"id": "build_review", "label": "Build + Review"},
    ]

    class Agent:
        @staticmethod
        def run_conversation(text, **kwargs):
            assert kwargs["is_user_turn"] is True
            return {"final_response": "Choose checks.", "choices": choices}

    payload = namespace["process"](
        Agent(), [], None, {}, "request", "Choose checks.", "handled"
    )

    assert payload["choices"] == choices


def test_gateway_displays_choice_labels_without_changing_stable_ids() -> None:
    namespace: dict[str, object] = {}
    exec(installer.change_gateway_source(GATEWAY_SOURCE), namespace)
    choices = [
        {"id": "manual", "label": "Manual Merge"},
        {"id": "safe_auto", "label": "Safe Files Auto-Merge"},
    ]

    class Agent:
        @staticmethod
        def run_conversation(message, **kwargs):
            assert kwargs["is_user_turn"] is True
            return {
                "final_response": "Choose one.",
                "choices": choices,
                "handled": True,
            }

    gateway = type("Gateway", (), {"_session_db": False})()
    result = namespace["run"](
        gateway,
        Agent(),
        [],
        "session-1",
        "Choose one.",
    )

    response = namespace["deliver"](result)

    assert result["choices"] == choices
    assert "- Manual Merge" in response
    assert "- Safe Files Auto-Merge" in response


def test_build_install_and_restore_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "hermes"
    package = tmp_path / "change-package"
    _hermes_tree(root)
    before = {
        path: file_hash(root / path)
        for path in ("hermes_cli/plugins.py", "agent/conversation_loop.py")
    }

    manifest = build_change_package(root, package, source_version="0.18.2-test")
    install_change(root, package)

    for item in manifest.files:
        assert file_hash(root / item.path) == item.after_file_hash

    restore_change(root, package)

    assert {
        path: file_hash(root / path)
        for path in ("hermes_cli/plugins.py", "agent/conversation_loop.py")
    } == before


def test_changed_source_is_refused_before_any_write(tmp_path: Path) -> None:
    root = tmp_path / "hermes"
    package = tmp_path / "change-package"
    _hermes_tree(root)
    build_change_package(root, package, source_version="0.18.2-test")
    plugin = root / "hermes_cli" / "plugins.py"
    conversation = root / "agent" / "conversation_loop.py"
    conversation_before = conversation.read_text(encoding="utf-8")
    plugin.write_text("user change", encoding="utf-8")

    with pytest.raises(InstallError, match="before_file_hash"):
        install_change(root, package)

    assert plugin.read_text(encoding="utf-8") == "user change"
    assert conversation.read_text(encoding="utf-8") == conversation_before


def test_package_manifest_uses_plain_hash_and_restore_names(tmp_path: Path) -> None:
    root = tmp_path / "hermes"
    package = tmp_path / "change-package"
    _hermes_tree(root)

    build_change_package(root, package, source_version="0.18.2-test")

    raw = json.loads((package / "installed-files-list.json").read_text("utf-8"))
    assert raw["source_version"] == "0.18.2-test"
    assert set(raw["files"][0]) == {
        "path",
        "before_file_hash",
        "after_file_hash",
        "release_file",
        "restore_file",
    }


def test_restore_refuses_an_unexpected_installed_file(tmp_path: Path) -> None:
    root = tmp_path / "hermes"
    package = tmp_path / "change-package"
    _hermes_tree(root)
    build_change_package(root, package, source_version="0.18.2-test")
    install_change(root, package)
    target = root / "agent" / "conversation_loop.py"
    target.write_text(target.read_text("utf-8") + "\n# later user edit\n", "utf-8")

    with pytest.raises(InstallError, match="after_file_hash"):
        restore_change(root, package)


def test_manifest_rejects_package_paths_outside_the_named_folders(
    tmp_path: Path,
) -> None:
    root = tmp_path / "hermes"
    package = tmp_path / "change-package"
    _hermes_tree(root)
    build_change_package(root, package, source_version="0.18.2-test")
    manifest_path = package / "installed-files-list.json"
    raw = json.loads(manifest_path.read_text("utf-8"))
    raw["files"][0]["release_file"] = "release.py"
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(InstallError, match="package path"):
        install_change(root, package)


def test_post_install_hash_failure_restores_every_original_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "hermes"
    package = tmp_path / "change-package"
    _hermes_tree(root)
    manifest = build_change_package(root, package, source_version="0.18.2-test")
    before = {item.path: file_hash(root / item.path) for item in manifest.files}
    original_write = installer._write_atomic
    release_writes = 0

    def corrupt_second_release(path: Path, content: bytes, *, mode=None) -> None:
        nonlocal release_writes
        original_write(path, content, mode=mode)
        if path.is_relative_to(root) and content.startswith(b"from typing"):
            release_writes += 1
            if release_writes == 1:
                path.write_bytes(content + b"\n# external race\n")

    monkeypatch.setattr(installer, "_write_atomic", corrupt_second_release)

    with pytest.raises(InstallError, match="restored"):
        install_change(root, package)

    assert {item.path: file_hash(root / item.path) for item in manifest.files} == before


def test_post_restore_hash_failure_reinstalls_every_release_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "hermes"
    package = tmp_path / "change-package"
    _hermes_tree(root)
    manifest = build_change_package(root, package, source_version="0.18.2-test")
    install_change(root, package)
    installed = {item.path: file_hash(root / item.path) for item in manifest.files}
    original_write = installer._write_atomic
    restore_writes = 0

    def corrupt_second_restore(path: Path, content: bytes, *, mode=None) -> None:
        nonlocal restore_writes
        original_write(path, content, mode=mode)
        if path.is_relative_to(root) and b"INFINITY_FORGE_PRE_USER_TURN_V1" not in content:
            restore_writes += 1
            if restore_writes == 2:
                path.write_bytes(content + b"\n# external race\n")

    monkeypatch.setattr(installer, "_write_atomic", corrupt_second_restore)

    with pytest.raises(InstallError, match="reinstalled"):
        restore_change(root, package)

    assert {item.path: file_hash(root / item.path) for item in manifest.files} == installed


def test_install_validates_every_restore_file_before_the_first_write(
    tmp_path: Path,
) -> None:
    root = tmp_path / "hermes"
    package = tmp_path / "change-package"
    _hermes_tree(root)
    manifest = build_change_package(root, package, source_version="0.18.2-test")
    before = {item.path: file_hash(root / item.path) for item in manifest.files}
    first = manifest.files[0]
    (package / first.restore_file).write_bytes(b"tampered restore data\n")

    with pytest.raises(InstallError, match="before_file_hash mismatch in package"):
        install_change(root, package)

    assert {item.path: file_hash(root / item.path) for item in manifest.files} == before


def test_restore_validates_every_release_file_before_the_first_write(
    tmp_path: Path,
) -> None:
    root = tmp_path / "hermes"
    package = tmp_path / "change-package"
    _hermes_tree(root)
    manifest = build_change_package(root, package, source_version="0.18.2-test")
    install_change(root, package)
    installed = {item.path: file_hash(root / item.path) for item in manifest.files}
    first = manifest.files[0]
    (package / first.release_file).write_bytes(b"tampered release data\n")

    with pytest.raises(InstallError, match="after_file_hash mismatch in package"):
        restore_change(root, package)

    assert {item.path: file_hash(root / item.path) for item in manifest.files} == installed


def test_interrupted_install_is_journaled_and_retry_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = (tmp_path / "hermes").resolve()
    package = (tmp_path / "change-package").resolve()
    _hermes_tree(root)
    manifest = build_change_package(root, package, source_version="0.18.2-test")
    target_paths = {root / item.path for item in manifest.files}
    original_write = installer._write_atomic
    target_writes = 0

    def interrupt_second_target(path: Path, content: bytes, *, mode=None) -> None:
        nonlocal target_writes
        if path in target_paths:
            target_writes += 1
            if target_writes == 2:
                raise KeyboardInterrupt("simulated process stop")
        original_write(path, content, mode=mode)

    monkeypatch.setattr(installer, "_write_atomic", interrupt_second_target)
    with pytest.raises(KeyboardInterrupt, match="simulated process stop"):
        install_change(root, package)
    monkeypatch.setattr(installer, "_write_atomic", original_write)

    journal = root / ".infinity-forge-change-state.json"
    assert journal.is_file()
    install_change(root, package)
    assert all(
        file_hash(root / item.path) == item.after_file_hash for item in manifest.files
    )
    assert not journal.exists()

    # A lost success response is safe to retry as well.
    install_change(root, package)


def test_interrupted_restore_is_journaled_and_retry_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = (tmp_path / "hermes").resolve()
    package = (tmp_path / "change-package").resolve()
    _hermes_tree(root)
    manifest = build_change_package(root, package, source_version="0.18.2-test")
    install_change(root, package)
    target_paths = {root / item.path for item in manifest.files}
    original_write = installer._write_atomic
    target_writes = 0

    def interrupt_second_target(path: Path, content: bytes, *, mode=None) -> None:
        nonlocal target_writes
        if path in target_paths:
            target_writes += 1
            if target_writes == 2:
                raise KeyboardInterrupt("simulated process stop")
        original_write(path, content, mode=mode)

    monkeypatch.setattr(installer, "_write_atomic", interrupt_second_target)
    with pytest.raises(KeyboardInterrupt, match="simulated process stop"):
        restore_change(root, package)
    monkeypatch.setattr(installer, "_write_atomic", original_write)

    journal = root / ".infinity-forge-change-state.json"
    assert journal.is_file()
    restore_change(root, package)
    assert all(
        file_hash(root / item.path) == item.before_file_hash for item in manifest.files
    )
    assert not journal.exists()

    restore_change(root, package)


def test_install_refuses_a_concurrent_change_writer(tmp_path: Path) -> None:
    root = (tmp_path / "hermes").resolve()
    package = (tmp_path / "change-package").resolve()
    _hermes_tree(root)
    build_change_package(root, package, source_version="0.18.2-test")

    with installer._change_lock(root):
        with pytest.raises(InstallError, match="already running"):
            install_change(root, package)


def test_atomic_replace_retries_a_transient_permission_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target.py"
    target.write_bytes(b"before")
    real_replace = installer.os.replace
    attempts = 0

    def fail_once(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("simulated Windows sharing violation")
        real_replace(source, destination)

    monkeypatch.setattr(installer.os, "replace", fail_once)

    installer._write_atomic(target, b"after")

    assert attempts == 2
    assert target.read_bytes() == b"after"
