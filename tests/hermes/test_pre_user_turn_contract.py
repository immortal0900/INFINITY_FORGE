from __future__ import annotations

import queue
import sys
import types

import pytest

import forge.hermes_change.installer as installer
from forge.hermes_change.installer import (
    change_conversation_source,
    change_plugins_source,
)

from .test_installer import CLI_SOURCE, CONVERSATION_SOURCE, PLUGIN_SOURCE


PROMPT_ID = "79df97c7-ff3d-4415-8b2e-dbe93bd10590"
FUTURE_EXPIRY = "2099-07-18T03:00:00Z"


def _choice_prompt(**overrides: object) -> dict[str, object]:
    prompt: dict[str, object] = {
        "choice_prompt_id": PROMPT_ID,
        "choice_mode": "multiple",
        "min_choices": 1,
        "max_choices": None,
        "submit_label": "Done",
        "expires_at": FUTURE_EXPIRY,
        "choices": [
            {"id": "lint", "label": "Lint", "description": "Run lint."},
            {"id": "tests", "label": "Tests", "description": "Run tests."},
        ],
    }
    prompt.update(overrides)
    return prompt


def _run_changed_source(
    monkeypatch,
    hook_result: dict[str, object] | list[dict[str, object]],
    *,
    agent=None,
    is_user_turn: bool = True,
    user_message: str | dict[str, object] = "first question",
    conversation_history: list[dict[str, object]] | None = None,
    working_directory: str | None = None,
    trusted_turn_context: dict[str, object] | None = None,
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
        user_message,
        conversation_history=conversation_history or [],
        is_user_turn=is_user_turn,
        working_directory=working_directory,
        trusted_turn_context=trusted_turn_context,
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
    prompt = {**_choice_prompt(), "choice_prompt_paused": True}

    result = _run_changed_source(
        monkeypatch,
        {"action": "handled", "text": "Choose checks.", **prompt},
    )

    for key, value in prompt.items():
        assert result[key] == value
    assert result["api_calls"] == 0


def test_structured_selection_reenters_hook_without_a_model_call(monkeypatch) -> None:
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
            "choice_prompt_id": PROMPT_ID,
            "selected_choice_ids": ["task"],
        },
        conversation_history=[{"role": "assistant", "content": "Choose mode."}],
        is_user_turn=True,
    )

    assert captured["name"] == "pre_user_turn"
    assert captured["text"] == ""
    assert captured["choice_prompt_id"] == PROMPT_ID
    assert captured["selected_choice_ids"] == ["task"]
    assert captured["is_new_session"] is False
    assert result["api_calls"] == 0
    assert result["handled"] is True
    assert {"role": "user", "content": ""} not in result["messages"]


@pytest.mark.parametrize(
    ("hook_result", "selected_choice_ids"),
    [
        pytest.param({"action": "continue"}, ["task"], id="continue"),
        pytest.param(
            {"action": "replace", "text": "must not reach the model"},
            ["task"],
            id="non-chat-replace",
        ),
    ],
)
def test_structured_submission_fails_closed_before_model_input(
    monkeypatch, hook_result, selected_choice_ids
) -> None:
    submission = {
        "choice_prompt_id": PROMPT_ID,
        "selected_choice_ids": selected_choice_ids,
    }

    result = _run_changed_source(
        monkeypatch,
        hook_result,
        user_message=submission,
        conversation_history=[{"role": "assistant", "content": "Choose mode."}],
    )

    assert result["handled"] is True
    assert result["api_calls"] == 0
    assert result.get("seen") is None
    assert submission not in [message.get("content") for message in result["messages"]]
    assert all(message.get("content") != "" for message in result["messages"])


def test_structured_chat_replace_calls_model_once_with_stashed_text(monkeypatch) -> None:
    plugins = types.ModuleType("hermes_cli.plugins")
    plugins.has_hook = lambda name: True
    plugins.invoke_hook = lambda name, **kwargs: [
        {"action": "replace", "text": "stashed first question"}
    ]
    package = types.ModuleType("hermes_cli")
    package.plugins = plugins
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)
    source = CONVERSATION_SOURCE.replace(
        'return {"seen": user_message}',
        'model_calls.append(user_message)\n        return {"seen": user_message}',
    )
    model_calls: list[str] = []
    namespace: dict[str, object] = {"model_calls": model_calls}
    exec(change_conversation_source(source), namespace)

    result = namespace["run_conversation"](
        types.SimpleNamespace(platform="cli", _gateway_session_key="s1"),
        {
            "choice_prompt_id": PROMPT_ID,
            "selected_choice_ids": ["chat"],
        },
        conversation_history=[],
        is_user_turn=True,
    )

    assert result == {"seen": "stashed first question"}
    assert model_calls == ["stashed first question"]


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"choice_prompt_id": PROMPT_ID.upper()}, id="noncanonical-uuid"),
        pytest.param({"expires_at": "2099-07-18T03:00:00"}, id="naive-expiry"),
        pytest.param({"expires_at": "not-rfc3339"}, id="unparseable-expiry"),
        pytest.param({"expires_at": "2000-01-01T00:00:00Z"}, id="expired"),
        pytest.param({"submit_label": "  "}, id="empty-submit-label"),
        pytest.param(
            {
                "choices": [
                    {"id": "lint", "label": "Lint", "description": ""},
                    {"id": "tests", "label": "Tests", "description": "Run tests."},
                ]
            },
            id="empty-description",
        ),
        pytest.param(
            {
                "choices": [
                    {"id": "same", "label": "Lint", "description": "Run lint."},
                    {"id": "same", "label": "Tests", "description": "Run tests."},
                ]
            },
            id="duplicate-ids",
        ),
        pytest.param(
            {
                "choices": [
                    {"id": "lint", "label": "Same", "description": "Run lint."},
                    {"id": "tests", "label": "Same", "description": "Run tests."},
                ]
            },
            id="duplicate-labels",
        ),
        pytest.param(
            {"choice_mode": "single", "min_choices": 1, "max_choices": None},
            id="single-not-exactly-one",
        ),
        pytest.param(
            {"choice_mode": "single", "max_choices": True},
            id="single-max-bool",
        ),
        pytest.param(
            {"choice_mode": "single", "max_choices": 1.0},
            id="single-max-float",
        ),
        pytest.param(
            {"choice_mode": "single", "max_choices": "1"},
            id="single-max-string",
        ),
        pytest.param({"min_choices": 0}, id="multiple-min-below-one"),
        pytest.param({"min_choices": 2, "max_choices": 1}, id="multiple-max-below-min"),
        pytest.param({"max_choices": 3}, id="multiple-max-above-choices"),
        pytest.param(
            {"choice_prompt_paused": "yes"},
            id="paused-flag-not-bool",
        ),
    ],
)
def test_conversation_and_modal_reject_the_same_malformed_prompts(
    monkeypatch, overrides
) -> None:
    prompt = _choice_prompt(**overrides)
    result = _run_changed_source(
        monkeypatch,
        {"action": "handled", "text": "Choose checks.", **prompt},
    )
    assert result["handled"] is True
    assert result["api_calls"] == 0
    assert result["choices"] == []

    namespace: dict[str, object] = {"queue": queue, "sys": sys}
    exec(installer.change_cli_source(CLI_SOURCE), namespace)
    cli = namespace["ModalShell"]()
    cli._app = object()
    modal_calls = 0

    def select_nothing(**kwargs):
        nonlocal modal_calls
        modal_calls += 1
        return None

    cli._prompt_text_input_modal = select_nothing
    modal_prompt = {"handled": True, "final_response": "Choose checks.", **prompt}

    assert cli._prompt_choice_modal(modal_prompt) is None
    assert modal_calls == 0


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


def test_hook_uses_only_carried_working_directory_not_user_envelope(monkeypatch) -> None:
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

    result = namespace["run_conversation"](
        types.SimpleNamespace(platform="cli", _gateway_session_key="s1"),
        {"text": "first question", "working_directory": "C:/untrusted"},
        conversation_history=[],
        is_user_turn=True,
        working_directory="C:/trusted",
    )

    assert result == {"seen": {"text": "first question"}}
    assert captured["name"] == "pre_user_turn"
    assert captured["text"] == "first question"
    assert captured["working_directory"] == "C:/trusted"


def test_trusted_turn_context_overrides_same_named_model_input(monkeypatch) -> None:
    captured: dict[str, object] = {}
    plugins = types.ModuleType("hermes_cli.plugins")

    def invoke_hook(name: str, **values: object):
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
    agent = types.SimpleNamespace(platform="forged", _gateway_session_key="forged")
    trusted = {
        "owner_host": "d6f70d5d-6482-45f5-80d2-219ec2ad4d19",
        "subject_id": "trusted-user",
        "session_id": "trusted-session",
        "surface": "desktop",
        "source_event_id": "desktop:01JZABC",
        "working_directory": "C:/trusted",
    }

    result = namespace["run_conversation"](
        agent,
        {
            "text": "update the Task",
            "owner_host": "forged-host",
            "subject_id": "forged-subject",
            "user_id": "forged-user",
            "session_id": "forged-session",
            "surface": "forged-surface",
            "source_event_id": "forged-event",
            "working_directory": "C:/forged",
        },
        conversation_history=[],
        is_user_turn=True,
        trusted_turn_context=trusted,
    )

    assert result == {"seen": {"text": "update the Task"}}
    assert captured["owner_host"] == trusted["owner_host"]
    assert captured["subject_id"] == "trusted-user"
    assert captured["user_id"] == "trusted-user"
    assert captured["session_id"] == "trusted-session"
    assert captured["surface"] == "desktop"
    assert captured["source_event_id"] == "desktop:01JZABC"
    assert captured["working_directory"] == "C:/trusted"
    assert agent._infinity_forge_trusted_turn_context == trusted


def test_internal_turn_clears_a_prior_trusted_context(monkeypatch) -> None:
    plugins = types.ModuleType("hermes_cli.plugins")
    plugins.has_hook = lambda name: True
    plugins.invoke_hook = lambda name, **values: [{"action": "continue"}]
    package = types.ModuleType("hermes_cli")
    package.plugins = plugins
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)
    namespace: dict[str, object] = {}
    exec(change_conversation_source(CONVERSATION_SOURCE), namespace)
    agent = types.SimpleNamespace(
        _infinity_forge_trusted_turn_context={"source_event_id": "stale"}
    )

    namespace["run_conversation"](
        agent,
        "internal prompt",
        conversation_history=[],
        is_user_turn=False,
    )

    assert agent._infinity_forge_trusted_turn_context == {}
