from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

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

KANBAN_DB_SOURCE = '''from __future__ import annotations

import os
from pathlib import Path

def _default_spawn(task, workspace, *, board=None):
    """Minimal Hermes v0.18.2 worker-spawn anchor fixture."""
    import subprocess
    if not task.assignee:
        raise ValueError(f"task {task.id} has no assignee")

    profile_arg = normalize_profile_name(task.assignee)
    prompt = f"work kanban task {task.id}"
    env = dict(os.environ)
    if task.tenant:
        env["HERMES_TENANT"] = task.tenant
    env["HERMES_KANBAN_TASK"] = task.id
    env["HERMES_KANBAN_WORKSPACE"] = workspace
    if task.branch_name:
        env["HERMES_KANBAN_BRANCH"] = task.branch_name
    if task.current_run_id is not None:
        env["HERMES_KANBAN_RUN_ID"] = str(task.current_run_id)
    if task.claim_lock:
        env["HERMES_KANBAN_CLAIM_LOCK"] = task.claim_lock
    env["HERMES_PROFILE"] = profile_arg

    cmd = [
        *_resolve_hermes_argv(),
        "-p", profile_arg,
        "--accept-hooks",
    ]
    cmd.extend([
        "chat",
        "-q", prompt,
    ])
    log_dir = worker_logs_dir(board=board)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task.id}.log"
    log_f = open(log_path, "ab")
    proc = subprocess.Popen(
        cmd,
        cwd=workspace if os.path.isdir(workspace) else None,
        stdin=subprocess.DEVNULL,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
        creationflags=0,
    )
    return proc.pid
'''


def _task(idempotency_key: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        id="task-42",
        assignee="builder",
        tenant="tenant-1",
        branch_name="codex/task-42",
        current_run_id=7,
        claim_lock="claim-1",
        idempotency_key=idempotency_key,
    )


def _run_spawn(
    source: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    task: SimpleNamespace,
    original_argv: list[str],
) -> dict[str, object]:
    calls: list[dict[str, object]] = []

    class Process:
        pid = 4321

    def capture(argv, **kwargs):
        calls.append({"argv": list(argv), **kwargs})
        kwargs["stdout"].close()
        return Process()

    monkeypatch.setattr(subprocess, "Popen", capture)
    namespace: dict[str, object] = {
        "normalize_profile_name": lambda value: value,
        "_resolve_hermes_argv": lambda: list(original_argv),
        "worker_logs_dir": lambda *, board=None: tmp_path / "logs",
    }
    exec(source, namespace)
    workspace = str(tmp_path / "작업 공간")
    assert namespace["_default_spawn"](task, workspace) == 4321
    assert len(calls) == 1
    return calls[0]


def _set_worker_config(
    monkeypatch: pytest.MonkeyPatch,
    *,
    python_bin: Path,
    runner: Path,
    hermes_bin: Path | None,
    fallback_hermes_bin: Path | None = None,
) -> None:
    monkeypatch.setenv("INFINITY_FORGE_SUBSCRIPTION_ROUTING", "1")
    monkeypatch.setenv("INFINITY_FORGE_SUBSCRIPTION_PYTHON", str(python_bin))
    monkeypatch.setenv("INFINITY_FORGE_SUBSCRIPTION_RUNNER", str(runner))
    if hermes_bin is None:
        monkeypatch.delenv("INFINITY_FORGE_HERMES_BIN", raising=False)
    else:
        monkeypatch.setenv("INFINITY_FORGE_HERMES_BIN", str(hermes_bin))
    if fallback_hermes_bin is None:
        monkeypatch.delenv("HERMES_BIN", raising=False)
    else:
        monkeypatch.setenv("HERMES_BIN", str(fallback_hermes_bin))


def _native_file(tmp_path: Path, name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    path = (tmp_path / f"{name}{suffix}").resolve()
    path.write_bytes(b"native executable fixture")
    if os.name != "nt":
        path.chmod(0o755)
    return path


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
    (root / "hermes_cli" / "kanban_db.py").write_text(
        KANBAN_DB_SOURCE, encoding="utf-8"
    )


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
        "hermes_cli/kanban_db.py",
    }


def test_kanban_transform_wraps_both_forge_idempotency_prefixes_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changed = installer.change_kanban_db_source(KANBAN_DB_SOURCE)
    python_bin = Path(sys.executable).resolve()
    runner = (tmp_path / "subscription-runner.py").resolve()
    runner.write_text("# runner\n", encoding="utf-8")
    hermes_bin = python_bin
    _set_worker_config(
        monkeypatch,
        python_bin=python_bin,
        runner=runner,
        hermes_bin=hermes_bin,
    )

    for prefix in ("forge-task:", "forge-step:"):
        original_head = [str(hermes_bin), "", "--", "한글"]
        call = _run_spawn(
            changed,
            monkeypatch,
            tmp_path,
            task=_task(prefix + "abc"),
            original_argv=original_head,
        )
        original_cmd = [
            *original_head,
            "-p",
            "builder",
            "--accept-hooks",
            "chat",
            "-q",
            "work kanban task task-42",
        ]
        assert call["argv"] == [
            str(python_bin),
            str(runner),
            "worker",
            "--workspace",
            str(tmp_path / "작업 공간"),
            "--",
            *original_cmd,
        ]
        assert call["env"]["INFINITY_FORGE_HERMES_BIN"] == str(hermes_bin)
        assert call["env"]["HERMES_KANBAN_RUN_ID"] == "7"
        assert call.get("shell", False) is False


@pytest.mark.parametrize(
    ("routing", "key"),
    (
        ("0", "forge-task:abc"),
        ("", "forge-step:abc"),
        ("1", "prefix-forge-task:abc"),
        ("1", None),
    ),
)
def test_kanban_transform_leaves_disabled_and_nonforge_spawn_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    routing: str,
    key: str | None,
) -> None:
    monkeypatch.setenv("INFINITY_FORGE_SUBSCRIPTION_ROUTING", routing)
    monkeypatch.setenv("INFINITY_FORGE_SUBSCRIPTION_PYTHON", "relative-python")
    original_argv = ["PATH-hermes", "", "한글"]
    parent_before = dict(os.environ)

    original = _run_spawn(
        KANBAN_DB_SOURCE,
        monkeypatch,
        tmp_path,
        task=_task(key),
        original_argv=original_argv,
    )
    changed = _run_spawn(
        installer.change_kanban_db_source(KANBAN_DB_SOURCE),
        monkeypatch,
        tmp_path,
        task=_task(key),
        original_argv=original_argv,
    )

    assert changed["argv"] == original["argv"]
    assert changed["env"] == original["env"]
    assert dict(os.environ) == parent_before


@pytest.mark.parametrize(
    "missing_key",
    (
        "INFINITY_FORGE_SUBSCRIPTION_PYTHON",
        "INFINITY_FORGE_SUBSCRIPTION_RUNNER",
        "INFINITY_FORGE_HERMES_BIN",
    ),
)
def test_kanban_transform_rejects_missing_configuration_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_key: str,
) -> None:
    python_bin = Path(sys.executable).resolve()
    runner = (tmp_path / "runner.py").resolve()
    runner.write_text("# runner\n", encoding="utf-8")
    _set_worker_config(
        monkeypatch,
        python_bin=python_bin,
        runner=runner,
        hermes_bin=python_bin,
    )
    monkeypatch.delenv(missing_key)

    with pytest.raises(RuntimeError, match=missing_key):
        _run_spawn(
            installer.change_kanban_db_source(KANBAN_DB_SOURCE),
            monkeypatch,
            tmp_path,
            task=_task("forge-task:abc"),
            original_argv=[str(python_bin)],
        )


@pytest.mark.parametrize("invalid_kind", ("relative", "missing", "non_native"))
def test_kanban_transform_rejects_invalid_native_executables_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_kind: str,
) -> None:
    valid = Path(sys.executable).resolve()
    runner = (tmp_path / "runner.py").resolve()
    runner.write_text("# runner\n", encoding="utf-8")
    if invalid_kind == "relative":
        invalid = Path("relative-python")
    elif invalid_kind == "missing":
        invalid = (tmp_path / ("missing.exe" if os.name == "nt" else "missing")).resolve()
    else:
        invalid = (tmp_path / ("python.cmd" if os.name == "nt" else "python")).resolve()
        invalid.write_bytes(b"not a native executable")
        if os.name != "nt":
            invalid.chmod(0o644)
    _set_worker_config(
        monkeypatch,
        python_bin=invalid,
        runner=runner,
        hermes_bin=valid,
    )

    with pytest.raises(RuntimeError, match="INFINITY_FORGE_SUBSCRIPTION_PYTHON"):
        _run_spawn(
            installer.change_kanban_db_source(KANBAN_DB_SOURCE),
            monkeypatch,
            tmp_path,
            task=_task("forge-step:abc"),
            original_argv=[str(valid)],
        )


@pytest.mark.parametrize(
    ("configured_name", "invalid_kind"),
    (
        ("runner", "relative"),
        ("runner", "missing"),
        ("runner", "directory"),
        ("hermes", "relative"),
        ("hermes", "missing"),
        ("hermes", "non_native"),
    ),
)
def test_kanban_transform_validates_runner_and_hermes_files_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured_name: str,
    invalid_kind: str,
) -> None:
    native = Path(sys.executable).resolve()
    runner = (tmp_path / "runner.py").resolve()
    runner.write_text("# runner\n", encoding="utf-8")
    if invalid_kind == "relative":
        invalid = Path("relative-command")
    elif invalid_kind == "missing":
        invalid = (tmp_path / ("missing.exe" if os.name == "nt" else "missing")).resolve()
    elif invalid_kind == "directory":
        invalid = (tmp_path / "runner-directory").resolve()
        invalid.mkdir()
    else:
        invalid = (tmp_path / ("hermes.cmd" if os.name == "nt" else "hermes")).resolve()
        invalid.write_bytes(b"not native")
        if os.name != "nt":
            invalid.chmod(0o644)
    _set_worker_config(
        monkeypatch,
        python_bin=native,
        runner=invalid if configured_name == "runner" else runner,
        hermes_bin=invalid if configured_name == "hermes" else native,
    )
    expected = (
        "INFINITY_FORGE_SUBSCRIPTION_RUNNER"
        if configured_name == "runner"
        else "INFINITY_FORGE_HERMES_BIN"
    )

    with pytest.raises(RuntimeError, match=expected):
        _run_spawn(
            installer.change_kanban_db_source(KANBAN_DB_SOURCE),
            monkeypatch,
            tmp_path,
            task=_task("forge-task:abc"),
            original_argv=[str(native)],
        )


@pytest.mark.parametrize("invalid_kind", ("relative", "missing", "non_native"))
def test_kanban_transform_rejects_invalid_original_hermes_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_kind: str,
) -> None:
    native = Path(sys.executable).resolve()
    runner = (tmp_path / "runner.py").resolve()
    runner.write_text("# runner\n", encoding="utf-8")
    if invalid_kind == "relative":
        original = "hermes"
    elif invalid_kind == "missing":
        original = str(
            (tmp_path / ("missing.exe" if os.name == "nt" else "missing")).resolve()
        )
    else:
        non_native = (
            tmp_path / ("hermes.ps1" if os.name == "nt" else "hermes")
        ).resolve()
        non_native.write_bytes(b"not native")
        if os.name != "nt":
            non_native.chmod(0o644)
        original = str(non_native)
    _set_worker_config(
        monkeypatch,
        python_bin=native,
        runner=runner,
        hermes_bin=native,
    )

    with pytest.raises(RuntimeError, match="original Hermes command"):
        _run_spawn(
            installer.change_kanban_db_source(KANBAN_DB_SOURCE),
            monkeypatch,
            tmp_path,
            task=_task("forge-step:abc"),
            original_argv=[original],
        )


def test_kanban_transform_rejects_hermes_mismatch_and_module_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    python_bin = Path(sys.executable).resolve()
    runner = (tmp_path / "runner.py").resolve()
    runner.write_text("# runner\n", encoding="utf-8")
    configured_hermes = _native_file(tmp_path, "hermes")
    _set_worker_config(
        monkeypatch,
        python_bin=python_bin,
        runner=runner,
        hermes_bin=configured_hermes,
    )
    changed = installer.change_kanban_db_source(KANBAN_DB_SOURCE)

    with pytest.raises(RuntimeError, match="does not match"):
        _run_spawn(
            changed,
            monkeypatch,
            tmp_path,
            task=_task("forge-task:abc"),
            original_argv=[str(python_bin)],
        )

    _set_worker_config(
        monkeypatch,
        python_bin=python_bin,
        runner=runner,
        hermes_bin=python_bin,
    )
    with pytest.raises(RuntimeError, match="module-form"):
        _run_spawn(
            changed,
            monkeypatch,
            tmp_path,
            task=_task("forge-task:abc"),
            original_argv=[str(python_bin), "-m", "hermes_cli"],
        )


def test_kanban_transform_uses_hermes_fallback_and_does_not_mutate_parent_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes_bin = Path(sys.executable).resolve()
    runner = (tmp_path / "runner.py").resolve()
    runner.write_text("# runner\n", encoding="utf-8")
    _set_worker_config(
        monkeypatch,
        python_bin=hermes_bin,
        runner=runner,
        hermes_bin=None,
        fallback_hermes_bin=hermes_bin,
    )
    parent_before = dict(os.environ)

    call = _run_spawn(
        installer.change_kanban_db_source(KANBAN_DB_SOURCE),
        monkeypatch,
        tmp_path,
        task=_task("forge-task:abc"),
        original_argv=[str(hermes_bin)],
    )

    assert call["env"]["INFINITY_FORGE_HERMES_BIN"] == str(hermes_bin)
    assert dict(os.environ) == parent_before


def test_kanban_transform_does_not_bypass_invalid_primary_hermes_with_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = Path(sys.executable).resolve()
    runner = (tmp_path / "runner.py").resolve()
    runner.write_text("# runner\n", encoding="utf-8")
    _set_worker_config(
        monkeypatch,
        python_bin=native,
        runner=runner,
        hermes_bin=Path("relative-primary"),
        fallback_hermes_bin=native,
    )

    with pytest.raises(RuntimeError, match="INFINITY_FORGE_HERMES_BIN"):
        _run_spawn(
            installer.change_kanban_db_source(KANBAN_DB_SOURCE),
            monkeypatch,
            tmp_path,
            task=_task("forge-task:abc"),
            original_argv=[str(native)],
        )


def test_kanban_transform_rejects_marker_reinstall_and_anchor_drift() -> None:
    changed = installer.change_kanban_db_source(KANBAN_DB_SOURCE)
    assert changed.count("INFINITY_FORGE_SUBSCRIPTION_WORKER_V1") == 1
    assert changed.index("_infinity_forge_subscription_worker_argv(task, cmd, env)") < changed.index(
        "log_dir = worker_logs_dir(board=board)"
    )

    with pytest.raises(InstallError, match="already installed"):
        installer.change_kanban_db_source(changed)
    partial = changed.replace(
        "    # INFINITY_FORGE_SUBSCRIPTION_WORKER_V1\n", "", 1
    )
    with pytest.raises(InstallError, match="partial"):
        installer.change_kanban_db_source(partial)
    with pytest.raises(InstallError, match="anchor"):
        installer.change_kanban_db_source(
            KANBAN_DB_SOURCE.replace('        "chat",', '        "speak",')
        )
    with pytest.raises(InstallError, match="not unique"):
        installer.change_kanban_db_source(KANBAN_DB_SOURCE + KANBAN_DB_SOURCE)


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
    manifest = build_change_package(root, package, source_version="0.18.2-test")
    before = {item.path: item.before_file_hash for item in manifest.files}
    install_change(root, package)

    for item in manifest.files:
        assert file_hash(root / item.path) == item.after_file_hash

    restore_change(root, package)

    assert {item.path: file_hash(root / item.path) for item in manifest.files} == before


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
