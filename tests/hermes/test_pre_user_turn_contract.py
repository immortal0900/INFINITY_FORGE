from __future__ import annotations

import sys
import types

from forge.hermes_change.installer import (
    change_conversation_source,
    change_plugins_source,
)

from .test_installer import CONVERSATION_SOURCE, PLUGIN_SOURCE


def _run_changed_source(
    monkeypatch,
    hook_result: dict[str, object] | list[dict[str, object]],
    *,
    agent=None,
    is_user_turn: bool = True,
):
    plugins = types.ModuleType("hermes_cli.plugins")
    hook_results = hook_result if isinstance(hook_result, list) else [hook_result]
    plugins.has_hook = lambda name: True
    plugins.invoke_hook = lambda name, **kwargs: hook_results
    package = types.ModuleType("hermes_cli")
    package.plugins = plugins
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)
    namespace: dict[str, object] = {}
    exec(change_conversation_source(CONVERSATION_SOURCE), namespace)
    return namespace["run_conversation"](
        agent
        or types.SimpleNamespace(platform="tui", _gateway_session_key="s1"),
        "first question",
        conversation_history=[],
        is_user_turn=is_user_turn,
    )


def test_plugin_registry_includes_pre_user_turn_once() -> None:
    changed = change_plugins_source(PLUGIN_SOURCE)

    assert changed.count('"pre_user_turn"') == 1


def test_handled_returns_without_running_the_original_turn(monkeypatch) -> None:
    result = _run_changed_source(
        monkeypatch,
        {"action": "handled", "text": "Choose Chat or Task"},
    )

    assert result["final_response"] == "Choose Chat or Task"
    assert result["api_calls"] == 0
    assert result["handled"] is True


def test_handled_preserves_valid_structured_choices(monkeypatch) -> None:
    choices = [
        {"id": "chat", "label": "Chat"},
        {"id": "task", "label": "Task"},
    ]

    result = _run_changed_source(
        monkeypatch,
        {"action": "handled", "text": "Choose one", "choices": choices},
    )

    assert result["choices"] == choices


def test_handled_preserves_the_complete_choice_prompt_envelope(monkeypatch) -> None:
    prompt_id = "79df97c7-ff3d-4415-8b2e-dbe93bd10590"
    prompt = {
        "choice_prompt_id": prompt_id,
        "choice_mode": "multiple",
        "min_choices": 1,
        "max_choices": None,
        "submit_label": "Done",
        "expires_at": "2026-07-18T03:00:00Z",
        "choices": [
            {"id": "lint", "label": "Lint", "description": "Run lint."},
            {"id": "tests", "label": "Tests", "description": "Run tests."},
        ],
    }

    result = _run_changed_source(
        monkeypatch,
        {"action": "handled", "text": "Choose checks.", **prompt},
    )

    for key, value in prompt.items():
        assert result[key] == value
    assert result["api_calls"] == 0


def test_structured_selection_reenters_hook_without_a_model_call(monkeypatch) -> None:
    prompt_id = "79df97c7-ff3d-4415-8b2e-dbe93bd10590"
    captured: dict[str, object] = {}
    plugins = types.ModuleType("hermes_cli.plugins")

    def invoke_hook(name: str, **values: object):
        captured["name"] = name
        captured.update(values)
        return [{"action": "handled", "text": "Choose task flow."}]

    plugins.has_hook = lambda name: True
    plugins.invoke_hook = invoke_hook
    package = types.ModuleType("hermes_cli")
    package.plugins = plugins
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)
    namespace: dict[str, object] = {}
    exec(change_conversation_source(CONVERSATION_SOURCE), namespace)

    result = namespace["run_conversation"](
        types.SimpleNamespace(platform="cli", _gateway_session_key="s1"),
        {
            "choice_prompt_id": prompt_id,
            "selected_choice_ids": ["task"],
        },
        conversation_history=[{"role": "assistant", "content": "Choose mode."}],
        is_user_turn=True,
    )

    assert captured["name"] == "pre_user_turn"
    assert captured["text"] == ""
    assert captured["choice_prompt_id"] == prompt_id
    assert captured["selected_choice_ids"] == ["task"]
    assert captured["is_new_session"] is False
    assert result["api_calls"] == 0
    assert result["handled"] is True


def test_malformed_choice_objects_fail_closed(monkeypatch) -> None:
    result = _run_changed_source(
        monkeypatch,
        {
            "action": "handled",
            "text": "Choose one",
            "choices": [{"id": "chat"}],
        },
    )

    assert result["handled"] is True
    assert result["api_calls"] == 0
    assert result["choices"] == []
    assert "conflict" in result["final_response"].lower()


def test_replace_passes_the_replacement_to_the_original_turn(monkeypatch) -> None:
    result = _run_changed_source(
        monkeypatch,
        {"action": "replace", "text": "stashed first question"},
    )

    assert result == {"seen": "stashed first question"}


def test_continue_preserves_the_original_text(monkeypatch) -> None:
    result = _run_changed_source(monkeypatch, {"action": "continue"})

    assert result == {"seen": "first question"}


def test_internal_agent_run_does_not_invoke_user_turn_hooks(monkeypatch) -> None:
    calls = 0
    plugins = types.ModuleType("hermes_cli.plugins")

    def invoke_hook(name: str, **values: object):
        nonlocal calls
        calls += 1
        return [{"action": "handled", "text": "Choose Chat or Task"}]

    plugins.has_hook = lambda name: True
    plugins.invoke_hook = invoke_hook
    package = types.ModuleType("hermes_cli")
    package.plugins = plugins
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)
    namespace: dict[str, object] = {}
    exec(change_conversation_source(CONVERSATION_SOURCE), namespace)

    result = namespace["run_conversation"](
        types.SimpleNamespace(platform="cli"),
        "internal builder prompt",
        task_id="kanban-worker",
        conversation_history=[],
    )

    assert calls == 0
    assert result == {"seen": "internal builder prompt"}


def test_handled_veto_is_checked_after_an_earlier_continue(monkeypatch) -> None:
    result = _run_changed_source(
        monkeypatch,
        [
            {"action": "continue"},
            {"action": "handled", "text": "Choose Chat or Task"},
        ],
    )

    assert result["handled"] is True
    assert result["final_response"] == "Choose Chat or Task"


def test_conflicting_handled_and_replace_results_fail_closed(monkeypatch) -> None:
    result = _run_changed_source(
        monkeypatch,
        [
            {"action": "replace", "text": "changed"},
            {"action": "handled", "text": "Choose Chat or Task"},
        ],
    )

    assert result["handled"] is True
    assert result["api_calls"] == 0
    assert "conflict" in result["final_response"].lower()


def test_hook_receives_transport_neutral_user_turn_fields(monkeypatch) -> None:
    captured: dict[str, object] = {}
    plugins = types.ModuleType("hermes_cli.plugins")

    def invoke_hook(name: str, **values: object):
        captured["name"] = name
        captured.update(values)
        return [{"action": "continue"}]

    plugins.has_hook = lambda name: True
    plugins.invoke_hook = invoke_hook
    package = types.ModuleType("hermes_cli")
    package.plugins = plugins
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)
    namespace: dict[str, object] = {}
    exec(change_conversation_source(CONVERSATION_SOURCE), namespace)

    namespace["run_conversation"](
        types.SimpleNamespace(
            platform="tui", _gateway_session_key="s1", _user_id="actual-user"
        ),
        "first question",
        conversation_history=[],
        is_user_turn=True,
    )

    assert captured["name"] == "pre_user_turn"
    assert captured["text"] == "first question"
    assert captured["session_id"] == "s1"
    assert captured["user_id"] == "actual-user"
    assert captured["surface"] == "tui"
    assert captured["is_new_session"] is True
