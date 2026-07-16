from __future__ import annotations

import json
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

CLI_SOURCE = '''def unrelated_call(self):
    schedule(
        task_id=self.session_id,
    )

def process(self, agent_message, message, stream_callback):
    result = self.agent.run_conversation(
        user_message=agent_message,
        conversation_history=self.conversation_history[:-1],
        stream_callback=stream_callback,
        task_id=self.session_id,
        persist_user_message=message,
        moa_config=None,
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

    cli = type(
        "CLI",
        (),
        {
            "agent": Agent(),
            "conversation_history": ["current"],
            "session_id": "session-1",
        },
    )()
    result = namespace["process"](cli, "request", "request", None)

    assert result["choices"] == choices
    assert "- Chat" in result["final_response"]
    assert "- Task" in result["final_response"]


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
