from __future__ import annotations

import asyncio
import json
import os
import queue
import sys
import threading
import time
import types
import typing
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


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _isolate_surface_event_outbox(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(
        "INFINITY_FORGE_SOURCE_EVENT_OUTBOX",
        str(tmp_path / "surface-events.json"),
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

TOOL_EXECUTOR_SOURCE = '''from typing import Any

def _apply_tool_request_middleware_for_agent(
    agent,
    *,
    function_name: str,
    function_args: dict,
    effective_task_id: str,
    tool_call_id: str,
) -> tuple[dict, list[dict[str, Any]]]:
    try:
        from hermes_cli.middleware import apply_tool_request_middleware
        result = apply_tool_request_middleware(
            function_name,
            function_args,
            task_id=effective_task_id or "",
            session_id=getattr(agent, "session_id", "") or "",
            tool_call_id=tool_call_id or "",
            turn_id=getattr(agent, "_current_turn_id", "") or "",
            api_request_id=getattr(agent, "_current_api_request_id", "") or "",
        )
        payload = result.payload if isinstance(result.payload, dict) else function_args
        return payload, list(result.trace)
    except Exception:
        return function_args, []

def concurrent(agent, function_name, function_args, effective_task_id, tool_call):
    function_args, middleware_trace = _apply_tool_request_middleware_for_agent(
        agent,
        function_name=function_name,
        function_args=function_args,
        effective_task_id=effective_task_id,
        tool_call_id=getattr(tool_call, "id", "") or "",
    )
    block_result = None
    if _ts_scope_block is not None:
        block_result = _ts_scope_block
    else:
        dispatch(function_args)

def sequential(agent, function_name, function_args, effective_task_id, tool_call):
    function_args, middleware_trace = _apply_tool_request_middleware_for_agent(
        agent,
        function_name=function_name,
        function_args=function_args,
        effective_task_id=effective_task_id,
        tool_call_id=getattr(tool_call, "id", "") or "",
    )
    _block_msg = None
    _block_error_type = "plugin_block"
    if _ts_scope_block is not None:
        _block_msg = _ts_scope_block
        _block_error_type = "tool_scope_block"
    else:
        dispatch(function_args)
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
            """ESC cancels active secret/sudo prompts."""
            if self._secret_state:
                self._cancel_secret_capture()
                event.app.current_buffer.reset()
                event.app.invalidate()
                return
            if self._sudo_state:
                self._sudo_state["response_queue"].put("")
                self._sudo_state = None
                event.app.invalidate()
                return
            if self._slash_confirm_state:
                self._submit_slash_confirm_response("cancel")
                event.app.current_buffer.reset()
                event.app.invalidate()
                return

        @kb.add('c-z')
        def handle_ctrl_z(event):
            event.app.invalidate()

        @kb.add('c-c')
        def handle_ctrl_c(event):
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

TUI_GATEWAY_SOURCE = '''import copy
import sys
import threading
import time
import uuid
from datetime import datetime
from typing import Any

_METHODS = {}

def method(name):
    def register(function):
        _METHODS[name] = function
        return function
    return register

@method("prompt.submit")
def prompt_submit(rid, params):
    session, err = _sess_nowait(params, rid)
    if err:
        return err
    with session["history_lock"]:
        session["running"] = True
    return None

def process(agent, history, _stream, session, sid, text, raw, status):
    run_kwargs = {
        "conversation_history": list(history),
        "stream_callback": _stream,
    }
    goal_followup = None
    try:
        result = agent.run_conversation(text, **run_kwargs)
        payload = {"text": raw, "usage": _get_usage(agent), "status": status}
        with session["history_lock"]:
            _clear_inflight_turn(session)
        _emit("message.complete", sid, payload)
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
    except Exception as error:
        _emit("error", sid, {"message": str(error)})
    finally:
        with session["history_lock"]:
            session["running"] = False
            session["last_active"] = time.time()
            _clear_inflight_turn(session)
        _emit("session.info", sid, _session_info(agent, session))
'''

GATEWAY_SOURCE = '''class Gateway:
    async def handle_event(self, event, source, session_id, session_key, history, persist_user_message, persist_user_timestamp):
        agent_result = await self._run_agent(
                "message", "context", history, source, session_id,
                session_key=session_key,
                persist_user_message=persist_user_message,
                persist_user_timestamp=persist_user_timestamp,
            )
        try:
            response = agent_result.get("final_response") or ""
            return response

        except Exception as e:
            return str(e)

    def handle(self, event, source):
        _agent_result = self._handle_message_with_agent(event, source)
        _final_text = str(_agent_result.get("final_response") or "")
        if _final_text.strip():
            self._post_turn_goal_continuation()
        return _agent_result

    async def _run_agent(
        self,
        message,
        context_prompt,
        history,
        source,
        session_id,
        session_key=None,
        persist_user_message=None,
        persist_user_timestamp: Optional[float] = None,
    ):
        if not self.multiplex_profiles:
            return await self._run_agent_inner(
                message, context_prompt, history, source, session_id,
                session_key=session_key,
                persist_user_message=persist_user_message,
                persist_user_timestamp=persist_user_timestamp,
            )
        with self.profile_scope():
            return await self._run_agent_inner(
                message, context_prompt, history, source, session_id,
                session_key=session_key,
                persist_user_message=persist_user_message,
                persist_user_timestamp=persist_user_timestamp,
            )

    async def _run_agent_inner(
        self,
        message,
        context_prompt,
        history,
        source,
        session_id,
        session_key=None,
        persist_user_message=None,
        persist_user_timestamp: Optional[float] = None,
    ):
        if self._get_proxy_url():
            return await self._run_agent_via_proxy(message)
        _run_message = message
        _api_run_message = _wrap_current_message_with_observed_context(
            _run_message,
            {},
        )
        _conversation_kwargs = {
            "conversation_history": history,
            "task_id": session_id,
        }
        result = self.agent.run_conversation(_api_run_message, **_conversation_kwargs)
        result_holder = [result]
        final_response = result.get("final_response", "")
        if final_response and self._session_db:
            maybe_auto_title()
        return {
            "final_response": final_response,
            "last_reasoning": result.get("last_reasoning"),
            "messages": result_holder[0].get("messages", []) if result_holder[0] else [],
        }
'''

TUI_GATEWAY_TYPES_SOURCE = """export interface PromptSubmitResponse {
  ok?: boolean
}

export type GatewayEvent =
  | {
      payload?: { reasoning?: string; rendered?: string; text?: string; usage?: Usage }
      session_id?: string
      type: 'message.complete'
    }
"""

TUI_SUBMISSION_SOURCE = """export function submitPrompt(text: string, deps: any): void {
  const sid = getUiState().sid
  const startSubmit = (displayText: string, submitText: string) => {
    const liveSid = getUiState().sid
    deps.gw.request<PromptSubmitResponse>('prompt.submit', { session_id: liveSid, text: submitText }).catch((e: Error) => {
      deps.sys(`error: ${e.message}`)
    })
  }
  startSubmit(text, text)
}
"""

TUI_EVENT_HANDLER_SOURCE = """import type { GatewayEvent } from '../gatewayTypes.js'
import { getOverlayState, patchOverlayState } from './overlayStore.js'
import { getUiState } from './uiStore.js'

export function createGatewayEventHandler(ctx: any): (ev: GatewayEvent) => void {
  return (ev: GatewayEvent) => {
    const sid = getUiState().sid

    if (ev.session_id && sid && ev.session_id !== sid && !ev.type.startsWith('gateway.')) {
      return
    }

    switch (ev.type) {
      case 'message.complete': {
        record(ev.payload ?? {})
        return
      }
    }
  }
}
"""

TUI_OVERLAY_STORE_SOURCE = """import { atom, computed } from 'nanostores'
import type { OverlayState } from './interfaces.js'

export const $overlayState = atom<OverlayState>({} as OverlayState)
export const $isBlocked = computed($overlayState, overlay => Boolean(overlay.clarify))
export const getOverlayState = () => $overlayState.get()
export const patchOverlayState = (next: Partial<OverlayState>) => $overlayState.set({ ...$overlayState.get(), ...next })
export const resetOverlayState = () => $overlayState.set({} as OverlayState)
"""

TUI_PROMPTS_SOURCE = """import { Box, Text, useInput } from '@hermes/ink'
import { useState } from 'react'
import type { Theme } from '../theme.js'

export function ExistingPrompt({ t }: { t: Theme }) {
  return <Box><Text color={t.color.text}>existing</Text></Box>
}
"""

TUI_APP_OVERLAYS_SOURCE = """import { Box } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { $overlayState, patchOverlayState } from '../app/overlayStore.js'
import { $uiSessionId, $uiTheme } from '../app/uiStore.js'
import { ApprovalPrompt } from './prompts.js'

export function PromptZone({ cols, onApprovalChoice }: any) {
  const overlay = useStore($overlayState)
  const theme = useStore($uiTheme)

  if (overlay.approval) {
    return <ApprovalPrompt cols={cols} onChoice={onApprovalChoice} req={overlay.approval} t={theme} />
  }

  return null
}
"""

DESKTOP_CHAT_MESSAGES_SOURCE = """export type GatewayEventPayload = {
  text?: string
  request_id?: string
  question?: string
  choices?: string[] | null
}
"""

DESKTOP_PROMPTS_STORE_SOURCE = """import { atom, computed, type ReadableAtom } from 'nanostores'
import { $clarifyRequest } from './clarify'
import { $activeSessionId } from './session'

const keyFor = (sessionId: string | null | undefined): string => sessionId ?? ''
interface KeyedPrompt { sessionId: string | null }
function keyedPromptStore<T extends KeyedPrompt>() {
  const idOf = (value: T): string | undefined => (value as { requestId?: string }).requestId
  return {} as any
}
export interface ApprovalRequest extends KeyedPrompt {
  command: string
  description: string
}
const approval = keyedPromptStore<ApprovalRequest>()
const sudo = keyedPromptStore<ApprovalRequest>()
const secret = keyedPromptStore<ApprovalRequest>()
export const $approvalRequest = approval.$active
export const $activeSessionAwaitingInput = computed(
  [$clarifyRequest, $approvalRequest, $sudoRequest, $secretRequest],
  (clarify, approval, sudo, secret) => Boolean(clarify || approval || sudo || secret)
)
export function clearAllPrompts(sessionId?: string | null): void {
  if (sessionId === undefined) {
    approval.reset()
    sudo.reset()
    secret.reset()
    return
  }
  approval.clear(sessionId)
  sudo.clear(sessionId)
  secret.clear(sessionId)
}
"""

DESKTOP_GATEWAY_EVENT_SOURCE = """import { clearAllPrompts, setApprovalRequest, setSecretRequest, setSudoRequest } from '@/store/prompts'

export function onGatewayEvent(event: any, sessionId: string | null) {
  const payload = event.payload
  if (event.type === 'message.delta') {
    return
  } else if (event.type === 'message.start') {
    return
  } else if (event.type === 'message.complete') {
    if (!sessionId) return
    clearAllPrompts(sessionId)
    const finalText = payload?.text || ''
    completeAssistantMessage(sessionId, finalText)
  }
}
"""

DESKTOP_PROMPT_OVERLAYS_SOURCE = """'use client'
import { useStore } from '@nanostores/react'
import { type FormEvent, useCallback, useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { $gateway } from '@/store/gateway'
import { $secretRequest, $sudoRequest, clearSecretRequest, clearSudoRequest } from '@/store/prompts'

export function PromptOverlays() {
  return (
    <>
      <SudoDialog />
      <SecretDialog />
    </>
  )
}
"""

DESKTOP_SUBMIT_SOURCE = """export async function submitPrompt(
  requestGateway: any,
  sessionId: string,
  text: string,
  recoveredId: string | null,
): Promise<void> {
  await withSessionBusyRetry(() =>
    requestGateway('prompt.submit', { session_id: sessionId, text }, PROMPT_SUBMIT_REQUEST_TIMEOUT_MS)
  )
  if (recoveredId) {
    await withSessionBusyRetry(() =>
      requestGateway('prompt.submit', { session_id: recoveredId, text }, PROMPT_SUBMIT_REQUEST_TIMEOUT_MS)
    )
  }
}
"""

SLACK_ADAPTER_SOURCE = """class SlackAdapter(BasePlatformAdapter):
    def __init__(self, config):
        super().__init__(config, Platform.SLACK)
        self._approval_resolved = {}

    async def connect(self):
        for _action_id in (
            "hermes_approve_once",
            "hermes_deny",
        ):
            self._app.action(_action_id)(self._handle_approval_action)

        # Register Block Kit action handlers for slash-confirm buttons

    async def _handle_slack_message(self, event):
        text = event.get("text", "")
        user_id = event.get("user", "")
        channel_id = event.get("channel", "")
        thread_ts = event.get("thread_ts")
        source = self.build_source(chat_id=channel_id, user_id=user_id, thread_id=thread_ts)
        msg_event = MessageEvent(
            text=text,
            source=source,
            raw_message=event,
        )
        await self.handle_message(msg_event)

    async def send_exec_approval(
        self, chat_id, command, session_key
    ):
        return SendResult(success=True)
"""

TUI_PROMPT_TEST_SOURCE = """import { describe, expect, it } from 'vitest'

import { composerPromptText } from '../lib/prompt.js'

describe('composerPromptText', () => {
  it('returns a prompt', () => expect(composerPromptText('>', 'default')).toBe('>'))
})
"""

DESKTOP_PROMPTS_TEST_SOURCE = """import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { clearClarifyRequest, setClarifyRequest } from './clarify'
import {
  $activeSessionAwaitingInput,
  clearAllPrompts,
  setApprovalRequest,
  setSecretRequest,
  setSudoRequest
} from './prompts'
import { $activeSessionId } from './session'

describe('existing prompts', () => {
  it('keeps the fixture imports used', () => expect(clearAllPrompts).toBeDefined())
})
"""

SLACK_APPROVAL_TEST_SOURCE = """import pytest


def test_existing_slack_approval_surface():
    assert True
"""


def _hermes_tree(root: Path) -> None:
    (root / "hermes_cli").mkdir(parents=True)
    (root / "agent").mkdir(parents=True)
    (root / "hermes_cli" / "plugins.py").write_text(
        PLUGIN_SOURCE, encoding="utf-8"
    )
    (root / "agent" / "conversation_loop.py").write_text(
        CONVERSATION_SOURCE, encoding="utf-8"
    )
    (root / "agent" / "tool_executor.py").write_text(
        TOOL_EXECUTOR_SOURCE, encoding="utf-8"
    )
    (root / "run_agent.py").write_text(RUN_AGENT_SOURCE, encoding="utf-8")
    (root / "cli.py").write_text(CLI_SOURCE, encoding="utf-8")
    (root / "tui_gateway").mkdir(parents=True)
    (root / "tui_gateway" / "server.py").write_text(
        TUI_GATEWAY_SOURCE, encoding="utf-8"
    )
    (root / "gateway").mkdir(parents=True)
    (root / "gateway" / "run.py").write_text(GATEWAY_SOURCE, encoding="utf-8")
    (root / "ui-tui" / "src" / "app").mkdir(parents=True)
    (root / "ui-tui" / "src" / "components").mkdir(parents=True)
    (root / "ui-tui" / "src" / "gatewayTypes.ts").write_text(
        TUI_GATEWAY_TYPES_SOURCE, encoding="utf-8"
    )
    (root / "ui-tui" / "src" / "app" / "createGatewayEventHandler.ts").write_text(
        TUI_EVENT_HANDLER_SOURCE, encoding="utf-8"
    )
    (root / "ui-tui" / "src" / "app" / "overlayStore.ts").write_text(
        TUI_OVERLAY_STORE_SOURCE, encoding="utf-8"
    )
    (root / "ui-tui" / "src" / "app" / "submissionCore.ts").write_text(
        TUI_SUBMISSION_SOURCE, encoding="utf-8"
    )
    (root / "ui-tui" / "src" / "components" / "prompts.tsx").write_text(
        TUI_PROMPTS_SOURCE, encoding="utf-8"
    )
    (root / "ui-tui" / "src" / "components" / "appOverlays.tsx").write_text(
        TUI_APP_OVERLAYS_SOURCE, encoding="utf-8"
    )
    (root / "apps" / "desktop" / "src" / "lib").mkdir(parents=True)
    (root / "apps" / "desktop" / "src" / "store").mkdir(parents=True)
    (root / "apps" / "desktop" / "src" / "app" / "session" / "hooks" / "use-message-stream").mkdir(parents=True)
    (root / "apps" / "desktop" / "src" / "components").mkdir(parents=True)
    (root / "apps" / "desktop" / "src" / "lib" / "chat-messages.ts").write_text(
        DESKTOP_CHAT_MESSAGES_SOURCE, encoding="utf-8"
    )
    (root / "apps" / "desktop" / "src" / "store" / "prompts.ts").write_text(
        DESKTOP_PROMPTS_STORE_SOURCE, encoding="utf-8"
    )
    (root / "apps" / "desktop" / "src" / "app" / "session" / "hooks" / "use-message-stream" / "gateway-event.ts").write_text(
        DESKTOP_GATEWAY_EVENT_SOURCE, encoding="utf-8"
    )
    (root / "apps" / "desktop" / "src" / "components" / "prompt-overlays.tsx").write_text(
        DESKTOP_PROMPT_OVERLAYS_SOURCE, encoding="utf-8"
    )
    (root / "apps" / "desktop" / "src" / "app" / "session" / "hooks" / "use-prompt-actions").mkdir(parents=True)
    (root / "apps" / "desktop" / "src" / "app" / "session" / "hooks" / "use-prompt-actions" / "submit.ts").write_text(
        DESKTOP_SUBMIT_SOURCE, encoding="utf-8"
    )
    (root / "plugins" / "platforms" / "slack").mkdir(parents=True)
    (root / "plugins" / "platforms" / "slack" / "adapter.py").write_text(
        SLACK_ADAPTER_SOURCE, encoding="utf-8"
    )
    (root / "ui-tui" / "src" / "__tests__").mkdir(parents=True, exist_ok=True)
    (root / "ui-tui" / "src" / "__tests__" / "prompt.test.ts").write_text(
        TUI_PROMPT_TEST_SOURCE, encoding="utf-8"
    )
    (root / "apps" / "desktop" / "src" / "store" / "prompts.test.ts").write_text(
        DESKTOP_PROMPTS_TEST_SOURCE, encoding="utf-8"
    )
    (root / "tests" / "gateway").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "gateway" / "test_slack_approval_buttons.py").write_text(
        SLACK_APPROVAL_TEST_SOURCE, encoding="utf-8"
    )


def test_carried_change_targets_user_surfaces_and_forwarder(tmp_path: Path) -> None:
    root = tmp_path / "hermes"
    package = tmp_path / "change-package"
    _hermes_tree(root)

    manifest = build_change_package(root, package, source_version="0.18.2-test")

    assert {item.path for item in manifest.files} == {
        "hermes_cli/plugins.py",
        "agent/conversation_loop.py",
        "agent/tool_executor.py",
        "run_agent.py",
        "cli.py",
        "tui_gateway/server.py",
        "ui-tui/src/gatewayTypes.ts",
        "ui-tui/src/app/createGatewayEventHandler.ts",
        "ui-tui/src/app/overlayStore.ts",
        "ui-tui/src/app/submissionCore.ts",
        "ui-tui/src/components/prompts.tsx",
        "ui-tui/src/components/appOverlays.tsx",
        "apps/desktop/src/lib/chat-messages.ts",
        "apps/desktop/src/store/prompts.ts",
        "apps/desktop/src/app/session/hooks/use-message-stream/gateway-event.ts",
        "apps/desktop/src/components/prompt-overlays.tsx",
        "apps/desktop/src/app/session/hooks/use-prompt-actions/submit.ts",
        "plugins/platforms/slack/adapter.py",
        "gateway/run.py",
        "ui-tui/src/__tests__/prompt.test.ts",
        "apps/desktop/src/store/prompts.test.ts",
        "tests/gateway/test_slack_approval_buttons.py",
    }
    target_manifest = json.loads(
        (ROOT / "forge" / "hermes_change" / "targets.json").read_text(encoding="utf-8")
    )
    assert [item.path for item in manifest.files] == target_manifest


def test_tui_chooser_payload_is_session_keyed_and_never_becomes_prompt_text() -> None:
    changed_types = installer.change_tui_gateway_types_source(
        TUI_GATEWAY_TYPES_SOURCE
    )
    changed_handler = installer.change_tui_event_handler_source(
        TUI_EVENT_HANDLER_SOURCE
    )
    changed_store = installer.change_tui_overlay_store_source(
        TUI_OVERLAY_STORE_SOURCE
    )
    changed_prompts = installer.change_tui_prompts_source(TUI_PROMPTS_SOURCE)
    changed_overlays = installer.change_tui_app_overlays_source(
        TUI_APP_OVERLAYS_SOURCE
    )

    assert "choice_prompt_id" in changed_types
    assert "selected_choice_ids" in changed_types
    assert "setChoicePrompt(ev.session_id" in changed_handler
    assert changed_handler.index("setChoicePrompt") < changed_handler.index(
        "ev.session_id !== sid"
    )
    assert "Record<string, GatewayChoicePrompt>" in changed_store
    assert "choiceAction" in changed_prompts
    assert "choice.submit" in changed_prompts
    assert "prompt.submit" not in changed_prompts
    assert "ChoicePrompt" in changed_overlays


def test_tui_blocked_store_supports_current_hermes_crlf_source() -> None:
    source = '''import { atom, computed } from 'nanostores'

import type { OverlayState } from './interfaces.js'

export const $overlayState = atom<OverlayState>({} as OverlayState)

export const $isBlocked = computed(
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
)

export const getOverlayState = () => $overlayState.get()
'''.replace("\n", "\r\n")

    changed = installer.change_tui_overlay_store_source(source)

    assert "[$overlayState, $choicePrompts, $uiSessionId]" in changed
    assert "(sessionId && choices[sessionId])" in changed
    assert changed.count("\r\n") == changed.count("\n")


def test_desktop_chooser_has_separate_keyed_store_and_accessible_controls() -> None:
    changed_payload = installer.change_desktop_chat_messages_source(
        DESKTOP_CHAT_MESSAGES_SOURCE
    )
    changed_store = installer.change_desktop_prompts_store_source(
        DESKTOP_PROMPTS_STORE_SOURCE
    )
    changed_handler = installer.change_desktop_gateway_event_source(
        DESKTOP_GATEWAY_EVENT_SOURCE
    )
    changed_overlays = installer.change_desktop_prompt_overlays_source(
        DESKTOP_PROMPT_OVERLAYS_SOURCE
    )

    assert "ChoicePromptPayload" in changed_payload
    assert "keyedPromptStore<ChoiceRequest>" in changed_store
    assert "choicePromptId" in changed_store
    assert (
        "const idOf = (value: T): string | undefined => "
        "(value as { choicePromptId?: string; requestId?: string }).requestId ?? "
        "(value as { choicePromptId?: string }).choicePromptId"
    ) in changed_store
    assert "setChoiceRequest" in changed_handler
    assert "clearAllPrompts(sessionId)" in changed_handler
    assert changed_handler.index("clearAllPrompts(sessionId)") < changed_handler.index(
        "if (choiceRequest) setChoiceRequest"
    )
    assert 'role="radiogroup"' in changed_overlays
    assert 'type="checkbox"' in changed_overlays
    assert "aria-live" in changed_overlays
    assert "choice.submit" in changed_overlays
    assert "prompt.submit" not in changed_overlays


def test_chooser_submit_errors_preserve_retryable_prompts() -> None:
    changed_tui = installer.change_tui_prompts_source(TUI_PROMPTS_SOURCE)
    changed_desktop = installer.change_desktop_prompt_overlays_source(
        DESKTOP_PROMPT_OVERLAYS_SOURCE
    )

    for changed in (changed_tui, changed_desktop):
        assert "choiceSubmitErrorDisposition" in changed
        assert "session busy" in changed
        assert "clearPrompt" in changed
        catch_block = changed.split("catch", 1)[1]
        assert "if (failure.clearPrompt)" in catch_block


def test_desktop_chooser_supports_roving_keyboard_and_aria_without_default() -> None:
    changed = installer.change_desktop_prompt_overlays_source(
        DESKTOP_PROMPT_OVERLAYS_SOURCE
    )

    assert "export function choiceKeyboardAction" in changed
    assert "ArrowDown" in changed and "ArrowUp" in changed
    assert "key === ' '" in changed
    assert "key === 'Enter'" in changed
    assert "event.key" in changed
    assert "tabIndex={" in changed
    assert "aria-checked={" in changed
    assert "useRef" in changed


def test_desktop_prompt_store_missing_production_awaiting_input_anchor_fails() -> None:
    broken = DESKTOP_PROMPTS_STORE_SOURCE.replace(
        "  (clarify, approval, sudo, secret) => Boolean(clarify || approval || sudo || secret)",
        "  () => false",
    )

    with pytest.raises(InstallError, match="awaiting input"):
        installer.change_desktop_prompts_store_source(broken)


@pytest.mark.parametrize(
    "identity_line",
    (
        "",
        "const promptIdentityOf = (value: T): string | undefined => "
        "(value as { requestId?: string }).requestId",
        "const idOf = (value: T): string | undefined => "
        "(value as { requestId?: string }).requestId || undefined",
    ),
    ids=("missing", "renamed", "request-id-only-variant"),
)
def test_desktop_prompt_store_requires_exact_choice_identity_seam(
    identity_line: str,
) -> None:
    broken = DESKTOP_PROMPTS_STORE_SOURCE.replace(
        "const idOf = (value: T): string | undefined => "
        "(value as { requestId?: string }).requestId",
        identity_line,
    )

    with pytest.raises(InstallError, match="stale clear identity"):
        installer.change_desktop_prompts_store_source(broken)


def test_desktop_prompt_store_requires_exact_global_clear_seam() -> None:
    broken = DESKTOP_PROMPTS_STORE_SOURCE.replace(
        "    secret.reset()",
        "    for (const store of [approval, sudo, secret]) store.reset()",
    )

    with pytest.raises(InstallError, match="global clear"):
        installer.change_desktop_prompts_store_source(broken)


def test_desktop_prompt_store_requires_exact_session_clear_seam() -> None:
    broken = DESKTOP_PROMPTS_STORE_SOURCE.replace(
        "  secret.clear(sessionId)",
        "  for (const store of [approval, sudo, secret]) store.clear(sessionId)",
    )

    with pytest.raises(InstallError, match="session clear"):
        installer.change_desktop_prompts_store_source(broken)


def test_desktop_gateway_missing_production_import_seam_fails() -> None:
    broken = DESKTOP_GATEWAY_EVENT_SOURCE.replace(
        "clearAllPrompts, setApprovalRequest, setSecretRequest, setSudoRequest",
        "clearAllPrompts",
    )

    with pytest.raises(InstallError, match="import anchor"):
        installer.change_desktop_gateway_event_source(broken)


def test_tui_gateway_claims_structured_choice_once_and_rejects_invalid_ids() -> None:
    changed = installer.change_tui_gateway_source(TUI_GATEWAY_SOURCE)

    assert '@method("choice.submit")' in changed
    assert '"choice_prompt_id": prompt_id' in changed
    assert '"selected_choice_ids": selected_ids' in changed
    assert 'session.pop("_choice_prompt", None)' in changed
    assert 'agent.run_conversation(text, **run_kwargs)' in changed
    assert 'params.get("text")' not in changed.split('@method("choice.submit")', 1)[1]


def test_tui_gateway_rejects_cross_transport_claim_and_normal_turn_revokes_prompt() -> None:
    owner_transport = object()
    attacker_transport = object()
    session = {
        "_choice_prompt": {
            **_valid_cli_prompt(),
            "_owner_transport": owner_transport,
        },
        "history_lock": threading.RLock(),
        "running": False,
    }
    namespace: dict[str, object] = {
        "_err": lambda rid, code, message: {"error": {"code": code, "message": message}},
        "_sess_nowait": lambda params, rid: (session, None),
        "_stdio_transport": object(),
        "current_transport": lambda: attacker_transport,
        "evaluate_goal": lambda: None,
        "maybe_auto_title": lambda: None,
        "_get_usage": lambda agent: {},
    }
    exec(installer.change_tui_gateway_source(TUI_GATEWAY_SOURCE), namespace)
    params = {
        "session_id": "session-1",
        "choice_prompt_id": session["_choice_prompt"]["choice_prompt_id"],
        "selected_choice_ids": ["chat"],
    }

    rejected = namespace["_METHODS"]["choice.submit"]("request-1", params)

    assert rejected["error"]["code"] == 4008
    assert session["_choice_prompt"]["_owner_transport"] is owner_transport

    namespace["_METHODS"]["prompt.submit"](
        "request-2", {"session_id": "session-1", "text": "new turn"}
    )
    assert "_choice_prompt" not in session


def test_tui_gateway_publishes_after_finalization_to_the_captured_owner_transport() -> None:
    changed = installer.change_tui_gateway_source(TUI_GATEWAY_SOURCE)
    namespace: dict[str, object] = {
        "_clear_inflight_turn": lambda session: None,
        "_err": lambda rid, code, message: {"error": {"code": code, "message": message}},
        "_sess_nowait": lambda params, rid: (None, {"error": "unused"}),
        "_stdio_transport": object(),
        "evaluate_goal": lambda: None,
        "maybe_auto_title": lambda: None,
        "_get_usage": lambda agent: {},
    }
    exec(changed, namespace)

    rebound_frames: list[dict] = []
    rebound_transport = types.SimpleNamespace(write=lambda frame: rebound_frames.append(frame))
    observed: dict[str, object] = {}
    session = {
        "history_lock": threading.RLock(),
        "running": True,
    }

    class OwnerTransport:
        def write(self, frame: dict) -> bool:
            observed["running_at_delivery"] = session["running"]
            observed["owner_at_delivery"] = session["_choice_prompt"]["_owner_transport"]
            acquired_during_write = threading.Event()

            def rebind() -> None:
                with session["history_lock"]:
                    acquired_during_write.set()
                    session["transport"] = rebound_transport

            resume_thread = threading.Thread(target=rebind)
            resume_thread.start()
            assert not acquired_during_write.wait(0.05)
            observed["resume_thread"] = resume_thread
            observed["acquired_during_write"] = acquired_during_write
            observed["frame"] = frame
            return True

    owner_transport = OwnerTransport()
    session["transport"] = owner_transport
    payload = {**_valid_cli_prompt(), "status": "complete", "text": "Choose"}

    namespace["_finalize_gateway_choice_turn"]("session-1", session, payload)
    observed["resume_thread"].join(timeout=1)

    assert observed["acquired_during_write"].is_set()
    assert observed["running_at_delivery"] is False
    assert observed["owner_at_delivery"] is owner_transport
    assert observed["frame"]["params"]["type"] == "message.complete"
    assert session["transport"] is rebound_transport
    assert session["_choice_prompt"]["_owner_transport"] is owner_transport
    assert rebound_frames == []


def test_tui_gateway_failed_choice_delivery_cleans_prompt_and_keeps_finalization() -> None:
    changed = installer.change_tui_gateway_source(TUI_GATEWAY_SOURCE)
    namespace: dict[str, object] = {
        "_clear_inflight_turn": lambda session: None,
        "_err": lambda rid, code, message: {"error": {"code": code, "message": message}},
        "_sess_nowait": lambda params, rid: (None, {"error": "unused"}),
        "_stdio_transport": object(),
        "evaluate_goal": lambda: None,
        "maybe_auto_title": lambda: None,
        "_get_usage": lambda agent: {},
    }
    exec(changed, namespace)
    session = {
        "history_lock": threading.RLock(),
        "running": True,
        "transport": types.SimpleNamespace(write=lambda frame: (_ for _ in ()).throw(OSError("closed"))),
    }

    namespace["_finalize_gateway_choice_turn"](
        "session-1", session, {**_valid_cli_prompt(), "status": "complete", "text": "Choose"}
    )

    assert session["running"] is False
    assert "_choice_prompt" not in session


def test_tui_gateway_defers_only_choice_completion_and_initializes_error_path() -> None:
    changed = installer.change_tui_gateway_source(TUI_GATEWAY_SOURCE)

    assert "_choice_completion_payload = None" in changed
    assert 'if _choice_completion_payload is None:\n            _emit("message.complete"' in changed
    assert changed.index("_finalize_gateway_choice_turn(") < changed.index(
        '_emit("session.info"'
    )


def test_slack_chooser_uses_signed_action_context_and_exact_id_fallback() -> None:
    changed = installer.change_slack_adapter_source(SLACK_ADAPTER_SOURCE)
    changed_gateway = installer.change_gateway_source(GATEWAY_SOURCE)

    assert "forge_choice_button" in changed
    assert "forge_choice_select" in changed
    assert "forge_choice_multi" in changed
    assert "forge_choice_submit" in changed
    assert "_is_interactive_user_authorized" in changed
    assert "structured_user_message" in changed
    assert "selected_choice_ids" in changed
    assert "_choice_reply_prompts" in changed
    assert "Reply with: choose" in changed_gateway
    assert "Reply with exact ID." in changed_gateway
    assert "structured_user_message" in changed_gateway


def test_gateway_missing_production_structured_runner_seam_fails() -> None:
    broken = GATEWAY_SOURCE.replace("async def _run_agent(", "async def _run_agent_missing(")

    with pytest.raises(InstallError, match="runner seam"):
        installer.change_gateway_source(broken)


def test_slack_expired_and_superseded_prompts_do_not_consume_normal_messages() -> None:
    changed = installer.change_slack_adapter_source(SLACK_ADAPTER_SOURCE)
    namespace: dict[str, object] = {
        "Any": object,
        "BasePlatformAdapter": type("BasePlatformAdapter", (), {}),
        "Dict": dict,
        "List": list,
        "MessageEvent": type("MessageEvent", (), {}),
        "MessageType": type("MessageType", (), {"TEXT": "text"}),
        "Optional": typing.Optional,
        "Platform": type("Platform", (), {"SLACK": "slack"}),
        "PlatformConfig": object,
        "SendResult": type("SendResult", (), {}),
        "Tuple": tuple,
        "datetime": __import__("datetime").datetime,
        "logger": types.SimpleNamespace(error=lambda *args, **kwargs: None),
    }
    exec(changed, namespace)
    adapter = namespace["SlackAdapter"].__new__(namespace["SlackAdapter"])
    key = ("channel", "thread", "user")
    expired = {
        **_valid_cli_prompt(),
        "expires_at": "2000-01-01T00:00:00Z",
    }
    state = {"context_key": key, "prompt": expired}
    adapter._choice_prompts = {"old-message": state}
    adapter._choice_reply_prompts = {key: "old-message"}

    handled, envelope = adapter._consume_choice_reply(*key, "ordinary conversation")

    assert (handled, envelope) == (False, None)
    assert adapter._choice_prompts == {}
    assert adapter._choice_reply_prompts == {}

    adapter._choice_prompts = {"stale-message": state}
    adapter._choice_reply_prompts = {key: "new-message"}
    assert adapter._claim_choice_state("stale-message", ["chat"]) is None
    assert "stale-message" not in adapter._choice_prompts
    assert adapter._choice_reply_prompts[key] == "new-message"


def test_slack_paginates_256_project_fallback_without_losing_tail_ids() -> None:
    changed = installer.change_slack_adapter_source(SLACK_ADAPTER_SOURCE)

    class SendResult:
        def __init__(self, **values: object) -> None:
            self.__dict__.update(values)

    namespace: dict[str, object] = {
        "Any": object,
        "BasePlatformAdapter": type("BasePlatformAdapter", (), {}),
        "Dict": dict,
        "List": list,
        "MessageEvent": type("MessageEvent", (), {}),
        "MessageType": type("MessageType", (), {"TEXT": "text"}),
        "Optional": typing.Optional,
        "Platform": type("Platform", (), {"SLACK": "slack"}),
        "PlatformConfig": object,
        "SendResult": SendResult,
        "Tuple": tuple,
        "datetime": __import__("datetime").datetime,
        "logger": types.SimpleNamespace(error=lambda *args, **kwargs: None),
    }
    exec(changed, namespace)
    messages: list[dict[str, object]] = []

    class Client:
        async def chat_postMessage(self, **kwargs: object) -> dict[str, str]:
            messages.append(dict(kwargs))
            return {"ts": str(len(messages))}

    adapter = namespace["SlackAdapter"].__new__(namespace["SlackAdapter"])
    adapter._app = object()
    adapter._choice_prompts = {}
    adapter._choice_reply_prompts = {}
    adapter._resolve_thread_ts = lambda _event, _metadata: "root"
    adapter._get_client = lambda _chat_id: Client()
    prompt = {
        **_valid_cli_prompt(),
        "choice_mode": "multiple",
        "max_choices": None,
        "submit_label": "Choose Projects",
        "choices": [
            {
                "id": f"{index:064x}",
                "label": f"owner/project-{index}-" + "x" * 220,
                "description": "Choose this Project.",
            }
            for index in range(256)
        ],
    }

    result = asyncio.run(
        adapter.send_choice_prompt(
            "C1",
            "Choose Projects.",
            prompt,
            "session-1",
            "U1",
        )
    )

    assert result.success is True
    assert len(messages) > 1
    assert all(len(str(message["text"])) <= 30_000 for message in messages)
    assert all("blocks" not in message for message in messages[:-1])
    assert "blocks" in messages[-1]
    delivered = "\n".join(str(message["text"]) for message in messages)
    assert all(choice["id"] in delivered for choice in prompt["choices"])
    assert prompt["choices"][-1]["id"] in str(messages[-1]["text"])
    assert (
        f"choose {prompt['choice_prompt_id']} <choice_id[,choice_id...]>"
        in str(messages[-1]["text"])
    )
    assert adapter._choice_reply_prompts[("C1", "root", "U1")] == str(
        len(messages)
    )


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


def test_cli_outbox_reuses_source_event_after_failed_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outbox_path = tmp_path / "cli-source-events.json"
    monkeypatch.setenv("INFINITY_FORGE_SOURCE_EVENT_OUTBOX", str(outbox_path))
    monkeypatch.setenv(
        "INFINITY_FORGE_HOST_ID", "d6f70d5d-6482-45f5-80d2-219ec2ad4d19"
    )
    namespace: dict[str, object] = {
        "queue": queue,
        "maybe_auto_title": lambda: None,
    }
    exec(installer.change_cli_source(CLI_SOURCE), namespace)
    source_event_ids: list[str] = []

    class Agent:
        def __init__(self, fail: bool) -> None:
            self.fail = fail

        def run_conversation(self, **kwargs):
            source_event_ids.append(
                kwargs["trusted_turn_context"]["source_event_id"]
            )
            if self.fail:
                raise RuntimeError("response lost")
            return {"final_response": "ok", "messages": [], "api_calls": 1}

    first = namespace["ModalShell"]()
    first.agent = Agent(True)
    first.session_id = "session-1"
    first.conversation_history = [{"role": "user", "content": "hello"}]
    with pytest.raises(RuntimeError, match="response lost"):
        namespace["process"](first, "hello", "hello", None)

    restarted = namespace["ModalShell"]()
    restarted.agent = Agent(False)
    restarted.session_id = "session-1"
    restarted.conversation_history = [{"role": "user", "content": "hello"}]
    namespace["process"](restarted, "hello", "hello", None)

    assert len(source_event_ids) == 2
    assert source_event_ids[0] == source_event_ids[1]
    raw = json.loads(outbox_path.read_text(encoding="utf-8"))
    assert raw["pending"] == {}


def test_clients_reuse_one_persisted_source_event_id_for_transport_retry() -> None:
    changed_tui = installer.change_tui_submission_source(TUI_SUBMISSION_SOURCE)
    changed_desktop = installer.change_desktop_submit_source(DESKTOP_SUBMIT_SOURCE)

    assert "forge-surface-event/v1" in changed_tui
    assert "source_event_id: sourceEvent.id" in changed_tui
    assert changed_tui.count("source_event_id: sourceEvent.id") == 1
    assert "localStorage" in changed_desktop
    assert changed_desktop.count("source_event_id: sourceEvent.id") == 2
    assert "acknowledgeSourceEvent" in changed_desktop


def test_gateway_and_slack_carry_authenticated_source_event_identity() -> None:
    changed_tui = installer.change_tui_gateway_source(TUI_GATEWAY_SOURCE)
    changed_gateway = installer.change_gateway_source(GATEWAY_SOURCE)
    changed_slack = installer.change_slack_adapter_source(SLACK_ADAPTER_SOURCE)

    assert 'params.get("source_event_id")' in changed_tui
    assert "if _forge_source_event_id is None:" in changed_tui
    assert '_forge_source_event_id = ""' in changed_tui
    assert '"trusted_turn_context"' in changed_tui
    assert '"source_event_id"' in changed_gateway
    assert "trusted_turn_context=" in changed_gateway
    assert 'event.get("client_msg_id")' in changed_slack
    assert 'event.get("event_ts")' in changed_slack
    assert 'choice_metadata["source_event_id"] = _forge_source_event_id' in changed_slack


def test_gateway_trusted_context_attaches_to_the_runner_class() -> None:
    source = GATEWAY_SOURCE.replace(
        "class Gateway:",
        "class GatewayRunner(GatewayAuthorizationMixin, GatewaySlashCommandsMixin):",
    )

    changed = installer.change_gateway_source(source)

    runner = changed.split("class GatewayRunner", 1)[1]
    assert "def _forge_trusted_turn_context" in runner
    assert runner.index("def _forge_trusted_turn_context") < runner.index(
        "async def _run_agent("
    )


def test_tui_busy_queue_keeps_one_authenticated_source_event() -> None:
    busy_helpers = '''
def _enqueue_prompt(session: dict, text: Any, transport: Any) -> None:
    existing = session.get("queued_prompt")
    if existing and isinstance(existing.get("text"), str) and isinstance(text, str):
        prev = existing["text"]
        text = f"{prev}\\n\\n{text}" if prev and text else (prev or text)
    session["queued_prompt"] = {"text": text, "transport": transport}

def _handle_busy_submit(rid, sid: str, session: dict, text: Any, transport: Any) -> dict:
    _enqueue_prompt(session, text, transport)
    return _ok(rid, {"status": "queued"})

def _drain_queued_prompt(rid, sid: str, session: dict) -> bool:
    queued = session["queued_prompt"]
    _run_prompt_submit(rid, sid, session, queued["text"])
    return True

def _run_prompt_submit(rid, sid: str, session: dict, text: Any) -> None:
    pass

def run_after_agent_ready():
    _run_prompt_submit(rid, sid, session, text)

run_thread = threading.Thread(target=run_after_agent_ready)
'''
    source = TUI_GATEWAY_SOURCE.replace(
        "def process(agent, history, _stream, session, sid, text, raw, status):",
        (
            f"{busy_helpers}\n"
            "def process(agent, history, _stream, session, sid, text, raw, status):"
        ),
    ).replace(
        '    with session["history_lock"]:',
        (
            '    if session.get("running"):\n'
            '        return _handle_busy_submit(rid, sid, session, text, '
            't or session.get("transport"))\n'
            '    with session["history_lock"]:'
        ),
        1,
    )

    changed = installer.change_tui_gateway_source(source)

    assert "source_event_id = None" in changed
    assert (
        'session["queued_prompt"] = {"text": text, "transport": transport, '
        '"source_event_id": source_event_id}'
    ) in changed
    assert 'queued.get("source_event_id")' in changed


def test_tool_executor_strips_forged_identity_and_blocks_mutation_without_event() -> None:
    changed = installer.change_tool_executor_source(TOOL_EXECUTOR_SOURCE)

    assert "RISK(security)" in changed
    assert "_FORGE_TRUSTED_TURN_FIELDS" in changed
    assert "trusted_turn_context=_forge_trusted_context" in changed
    assert "send_to_task" in changed and "stop_task" in changed
    assert changed.count("_forge_context_block") >= 4
    compile(changed, "<changed Hermes tool executor>", "exec")


def test_run_agent_forwards_trusted_context_without_exposing_schema_fields() -> None:
    changed = installer.change_run_agent_source(RUN_AGENT_SOURCE)

    assert "trusted_turn_context: Optional[dict[str, Any]] = None" in changed
    assert "trusted_turn_context=trusted_turn_context" in changed


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


def test_legacy_slash_display_does_not_require_the_structured_modal_helper() -> None:
    namespace: dict[str, object] = {
        "queue": queue,
        "_append_panel_line": lambda *args: None,
        "box_width": 80,
    }
    exec(installer.change_cli_source(CLI_SOURCE), namespace)
    legacy_shell = types.SimpleNamespace(
        _slash_confirm_state={
            "title": "Confirm",
            "detail": "Legacy slash confirmation.",
            "choices": [("once", "Approve Once", "Proceed once.")],
            "selected": 0,
        }
    )

    fragments = namespace["ModalShell"]._get_slash_confirm_display_fragments(
        legacy_shell
    )

    assert fragments == []


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


@pytest.mark.parametrize(
    ("path", "sentinel"),
    [
        pytest.param("ctrl-c", "cancel", id="ctrl-c"),
        pytest.param("timeout", None, id="timeout"),
        pytest.param("cancel", None, id="cancel"),
    ],
)
def test_choice_modal_cancel_sentinels_never_submit_a_stable_id(path, sentinel) -> None:
    tty = type("TTY", (), {"isatty": lambda self: True})()
    fake_sys = type("Sys", (), {"stdin": tty, "stdout": tty})()
    namespace: dict[str, object] = {"queue": queue, "sys": fake_sys}
    exec(installer.change_cli_source(CLI_SOURCE), namespace)
    cli = namespace["ModalShell"]()
    cli._app = object()
    cli._prompt_text_input_modal = lambda **kwargs: sentinel

    assert cli._prompt_choice_modal(_valid_cli_prompt()) is None, path


def test_structured_ctrl_c_cannot_submit_a_choice_id_named_cancel() -> None:
    class KeyBindings:
        def __init__(self) -> None:
            self.handlers: dict[str, object] = {}

        def add(self, key, **_kwargs):
            def register(handler):
                self.handlers[key] = handler
                return handler

            return register

    class Buffer:
        def reset(self) -> None:
            pass

    class App:
        current_buffer = Buffer()

        def invalidate(self) -> None:
            pass

    namespace: dict[str, object] = {"queue": queue, "time": time}
    exec(installer.change_cli_source(CLI_SOURCE), namespace)
    cli = namespace["ModalShell"]()
    cli._voice_lock = threading.Lock()
    cli._voice_recording = False
    cli._voice_recorder = None
    namespace["cli_ref"] = cli
    kb = KeyBindings()
    cli.run(kb, lambda predicate: predicate)
    event = types.SimpleNamespace(app=App())

    structured_responses = queue.Queue()
    cli._slash_confirm_state = {
        "structured_choice_modal": True,
        "choices": [("cancel", "Cancel", "Discard the request.")],
        "response_queue": structured_responses,
    }
    cli._slash_confirm_deadline = 123
    kb.handlers["c-c"](event)

    assert structured_responses.get_nowait() is None
    assert cli._slash_confirm_state is None

    legacy_responses = queue.Queue()
    cli._slash_confirm_state = {
        "choices": [("once", "Approve once", "Proceed once.")],
        "response_queue": legacy_responses,
    }
    cli._slash_confirm_deadline = 123
    kb.handlers["c-c"](event)

    assert legacy_responses.get_nowait() == "cancel"


def test_structured_ctrl_c_does_not_reenter_the_user_turn_hook() -> None:
    class KeyBindings:
        def __init__(self) -> None:
            self.handlers: dict[str, object] = {}

        def add(self, key, **_kwargs):
            def register(handler):
                self.handlers[key] = handler
                return handler

            return register

    class Buffer:
        def reset(self) -> None:
            pass

    class App:
        current_buffer = Buffer()

        def invalidate(self) -> None:
            pass

    tty = type("TTY", (), {"isatty": lambda self: True})()
    fake_sys = type("Sys", (), {"stdin": tty, "stdout": tty})()
    namespace: dict[str, object] = {
        "queue": queue,
        "sys": fake_sys,
        "time": time,
        "maybe_auto_title": lambda: None,
    }
    exec(installer.change_cli_source(CLI_SOURCE), namespace)
    calls = 0
    prompt = {
        **_valid_cli_prompt(),
        "choices": [{"id": "cancel", "label": "Cancel", "description": "Stop."}],
    }

    class Agent:
        @staticmethod
        def run_conversation(**_kwargs):
            nonlocal calls
            calls += 1
            return (
                prompt
                if calls == 1
                else {
                    "final_response": "Unexpected reentry.",
                    "messages": [],
                    "api_calls": 0,
                    "handled": True,
                    "choices": [],
                }
            )

    cli = namespace["ModalShell"]()
    cli.agent = Agent()
    cli._app = object()
    cli._capture_modal_input_snapshot = lambda: None
    cli._voice_lock = threading.Lock()
    cli._voice_recording = False
    cli._voice_recorder = None
    cli._slash_confirm_state = None
    cli.conversation_history = ["current"]
    cli.session_id = "session-1"
    namespace["cli_ref"] = cli
    kb = KeyBindings()
    cli.run(kb, lambda predicate: predicate)
    result: dict[str, object] = {}

    def run_process() -> None:
        result["value"] = namespace["process"](cli, "first input", "first input", None)

    worker = threading.Thread(target=run_process)
    worker.start()
    deadline = time.monotonic() + 1
    while cli._slash_confirm_state is None and time.monotonic() < deadline:
        time.sleep(0.001)
    assert cli._slash_confirm_state is not None
    kb.handlers["c-c"](types.SimpleNamespace(app=App()))
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert calls == 1
    assert result["value"] is prompt


def test_choice_modal_rechecks_expiry_after_waiting_before_submitting() -> None:
    tty = type("TTY", (), {"isatty": lambda self: True})()
    fake_sys = type("Sys", (), {"stdin": tty, "stdout": tty})()
    namespace: dict[str, object] = {"queue": queue, "sys": fake_sys}
    exec(installer.change_cli_source(CLI_SOURCE), namespace)
    cli = namespace["ModalShell"]()
    cli._app = object()
    prompt = _valid_cli_prompt()

    def select_after_expiry(**_kwargs):
        prompt["expires_at"] = "2000-01-01T00:00:00Z"
        return "chat"

    cli._prompt_text_input_modal = select_after_expiry

    assert cli._prompt_choice_modal(prompt) is None


def test_expired_modal_selection_does_not_reenter_the_user_turn_hook() -> None:
    tty = type("TTY", (), {"isatty": lambda self: True})()
    fake_sys = type("Sys", (), {"stdin": tty, "stdout": tty})()
    namespace: dict[str, object] = {
        "queue": queue,
        "sys": fake_sys,
        "maybe_auto_title": lambda: None,
    }
    exec(installer.change_cli_source(CLI_SOURCE), namespace)
    prompt = _valid_cli_prompt()
    calls = 0

    class Agent:
        @staticmethod
        def run_conversation(**_kwargs):
            nonlocal calls
            calls += 1
            return prompt

    cli = namespace["ModalShell"]()
    cli.agent = Agent()
    cli._app = object()
    cli.conversation_history = ["current"]
    cli.session_id = "session-1"

    def select_after_expiry(**_kwargs):
        prompt["expires_at"] = "2000-01-01T00:00:00Z"
        return "chat"

    cli._prompt_text_input_modal = select_after_expiry

    result = namespace["process"](cli, "first input", "first input", None)

    assert calls == 1
    assert result is prompt


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
                    "expires_at": "2099-07-18T03:00:00Z",
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


def test_run_agent_carries_working_directory_to_the_conversation_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changed = installer.change_run_agent_source(RUN_AGENT_SOURCE)
    captured: dict[str, object] = {}
    agent_package = types.ModuleType("agent")
    conversation_loop = types.ModuleType("agent.conversation_loop")

    def run_conversation(*args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return {"ok": True}

    conversation_loop.run_conversation = run_conversation
    monkeypatch.setitem(sys.modules, "agent", agent_package)
    monkeypatch.setitem(sys.modules, "agent.conversation_loop", conversation_loop)
    namespace: dict[str, object] = {}
    exec(changed, namespace)

    result = namespace["AIAgent"]().run_conversation(
        "question", working_directory="C:/trusted"
    )

    assert result == {"ok": True}
    assert captured["working_directory"] == "C:/trusted"

    captured.clear()
    result = namespace["AIAgent"]().run_conversation("internal question")

    assert result == {"ok": True}
    assert captured["working_directory"] is None


def test_cli_carries_its_initial_working_directory_across_choice_reentry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    namespace: dict[str, object] = {"queue": queue, "maybe_auto_title": lambda: None}
    exec(installer.change_cli_source(CLI_SOURCE), namespace)
    first_directory = tmp_path / "first"
    changed_directory = tmp_path / "changed"
    first_directory.mkdir()
    changed_directory.mkdir()
    monkeypatch.chdir(first_directory)
    prompt = _valid_cli_prompt()
    calls: list[dict[str, object]] = []

    class Agent:
        @staticmethod
        def run_conversation(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                monkeypatch.chdir(changed_directory)
                return dict(prompt)
            return {"final_response": "done", "api_calls": 0, "handled": True}

    cli = namespace["ModalShell"]()
    cli.agent = Agent()
    cli.conversation_history = ["current"]
    cli.session_id = "session-1"
    cli._prompt_choice_modal = lambda _prompt: {
        "choice_prompt_id": prompt["choice_prompt_id"],
        "selected_choice_ids": ["task"],
    }

    namespace["process"](cli, "first input", "first input", None)

    assert [call["working_directory"] for call in calls] == [
        str(first_directory),
        str(first_directory),
    ]


def test_cli_uses_none_when_its_initial_working_directory_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace: dict[str, object] = {"queue": queue, "maybe_auto_title": lambda: None}
    exec(installer.change_cli_source(CLI_SOURCE), namespace)

    def raise_missing_working_directory() -> str:
        raise OSError("gone")

    monkeypatch.setattr(os, "getcwd", raise_missing_working_directory)
    calls: list[dict[str, object]] = []

    class Agent:
        @staticmethod
        def run_conversation(**kwargs):
            calls.append(kwargs)
            return {"final_response": "done", "api_calls": 1}

    cli = namespace["ModalShell"]()
    cli.agent = Agent()
    cli.conversation_history = ["current"]
    cli.session_id = "session-1"

    result = namespace["process"](cli, "first input", "first input", None)

    assert result["final_response"] == "done"
    assert calls[0]["working_directory"] is None


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
                "expires_at": "2099-07-18T03:00:00Z",
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


def _valid_cli_prompt() -> dict[str, object]:
    return {
        "final_response": "Choose mode.",
        "messages": [{"role": "assistant", "content": "intermediate chooser"}],
        "api_calls": 0,
        "handled": True,
        "choice_prompt_id": "79df97c7-ff3d-4415-8b2e-dbe93bd10590",
        "choice_mode": "single",
        "min_choices": 1,
        "max_choices": 1,
        "submit_label": "Choose mode",
        "expires_at": "2099-07-18T03:00:00Z",
        "choices": [
            {"id": "chat", "label": "Chat", "description": "Chat."},
            {"id": "task", "label": "Task", "description": "Task."},
        ],
    }


def test_two_hundred_sixty_fourth_reentry_returns_a_nonchooser_result_unchanged() -> None:
    namespace: dict[str, object] = {"queue": queue}
    exec(installer.change_cli_source(CLI_SOURCE), namespace)
    prompt = _valid_cli_prompt()
    final_result = {
        "final_response": "Model answer",
        "messages": [{"role": "assistant", "content": "Model answer"}],
        "api_calls": 1,
        "completed": True,
    }
    calls = 0

    class Agent:
        @staticmethod
        def run_conversation(**kwargs):
            nonlocal calls
            calls += 1
            return final_result if calls == 264 else dict(prompt)

    cli = namespace["ModalShell"]()
    cli.agent = Agent()
    cli._prompt_choice_modal = lambda _prompt: {
        "choice_prompt_id": prompt["choice_prompt_id"],
        "selected_choice_ids": ["chat"],
    }

    result = cli._continue_choice_modal_result(
        dict(prompt),
        conversation_history=[{"role": "assistant", "content": "base"}],
        stream_callback=None,
        task_id="session-1",
        moa_config=None,
    )

    assert calls == 264
    assert result is final_result
    assert result["api_calls"] == 1
    assert result["final_response"] == "Model answer"


def test_two_hundred_sixty_fifth_prompt_stops_after_264_reentries() -> None:
    namespace: dict[str, object] = {"queue": queue}
    exec(installer.change_cli_source(CLI_SOURCE), namespace)
    prompt = _valid_cli_prompt()
    calls = 0

    class Agent:
        @staticmethod
        def run_conversation(**kwargs):
            nonlocal calls
            calls += 1
            return dict(prompt)

    cli = namespace["ModalShell"]()
    cli.agent = Agent()
    cli._prompt_choice_modal = lambda _prompt: {
        "choice_prompt_id": prompt["choice_prompt_id"],
        "selected_choice_ids": ["chat"],
    }

    result = cli._continue_choice_modal_result(
        dict(prompt),
        conversation_history=[{"role": "assistant", "content": "base"}],
        stream_callback=None,
        task_id="session-1",
        moa_config=None,
    )

    assert calls == 264
    assert result["handled"] is True
    assert result["api_calls"] == 0
    assert "too many consecutive prompts" in result["final_response"]


def test_two_hundred_sixty_fourth_reentry_may_pause_the_confirm_chooser() -> None:
    namespace: dict[str, object] = {"queue": queue}
    exec(installer.change_cli_source(CLI_SOURCE), namespace)
    prompt = _valid_cli_prompt()
    paused = {
        **prompt,
        "final_response": "Task validated, but v2 Task creation is not enabled yet.",
        "messages": [{"role": "assistant", "content": "validated"}],
        "api_calls": 0,
        "completed": True,
        "handled": True,
        "choice_prompt_paused": True,
    }
    calls = 0

    class Agent:
        @staticmethod
        def run_conversation(**kwargs):
            nonlocal calls
            calls += 1
            return paused if calls == 264 else dict(prompt)

    cli = namespace["ModalShell"]()
    cli.agent = Agent()
    cli._prompt_choice_modal = lambda _prompt: {
        "choice_prompt_id": prompt["choice_prompt_id"],
        "selected_choice_ids": ["chat"],
    }

    result = cli._continue_choice_modal_result(
        dict(prompt),
        conversation_history=[{"role": "assistant", "content": "base"}],
        stream_callback=None,
        task_id="session-1",
        moa_config=None,
    )

    assert calls == 264
    assert result is paused
    assert result["choice_prompt_paused"] is True


def test_cli_reentries_keep_only_base_history_until_chat_reaches_the_model(
    monkeypatch,
) -> None:
    prompt_one = _valid_cli_prompt()
    prompt_two = {
        **_valid_cli_prompt(),
        "choice_prompt_id": "483ad83b-2972-46fc-a839-b348b1487710",
        "final_response": "Choose again.",
    }
    hook_calls = 0
    plugins = types.ModuleType("hermes_cli.plugins")

    def invoke_hook(name, **values):
        nonlocal hook_calls
        hook_calls += 1
        if hook_calls == 1:
            return [{"action": "handled", "text": "Choose mode.", **prompt_one}]
        if hook_calls == 2:
            return [{"action": "handled", "text": "Choose again.", **prompt_two}]
        return [{"action": "replace", "text": "first input"}]

    plugins.has_hook = lambda name: True
    plugins.invoke_hook = invoke_hook
    package = types.ModuleType("hermes_cli")
    package.plugins = plugins
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)
    conversation_source = CONVERSATION_SOURCE.replace(
        'return {"seen": user_message}',
        "model_context = list(conversation_history or [])\n"
        '        model_context.append({"role": "user", "content": user_message})\n'
        "        model_calls.append(model_context)\n"
        '        return {"final_response": "Model answer", "messages": model_context, "api_calls": 1}',
    )
    model_calls: list[list[dict[str, object]]] = []
    conversation_namespace: dict[str, object] = {"model_calls": model_calls}
    exec(installer.change_conversation_source(conversation_source), conversation_namespace)

    class Agent:
        @staticmethod
        def run_conversation(**kwargs):
            return conversation_namespace["run_conversation"](
                types.SimpleNamespace(platform="cli", _gateway_session_key="session-1"),
                **kwargs,
            )

    cli_namespace: dict[str, object] = {
        "queue": queue,
        "maybe_auto_title": lambda: None,
    }
    exec(installer.change_cli_source(CLI_SOURCE), cli_namespace)
    cli = cli_namespace["ModalShell"]()
    cli.agent = Agent()
    cli.conversation_history = [
        {"role": "assistant", "content": "base"},
        {"role": "user", "content": "first input"},
    ]
    cli.session_id = "session-1"
    selections = iter(
        [
            {
                "choice_prompt_id": prompt_one["choice_prompt_id"],
                "selected_choice_ids": ["task"],
            },
            {
                "choice_prompt_id": prompt_two["choice_prompt_id"],
                "selected_choice_ids": ["chat"],
            },
        ]
    )
    cli._prompt_choice_modal = lambda _prompt: next(selections)

    result = cli_namespace["process"](cli, "first input", "first input", None)

    assert result["api_calls"] == 1
    assert len(model_calls) == 1
    assert model_calls[0] == [
        {"role": "assistant", "content": "base"},
        {"role": "user", "content": "first input"},
    ]
    assert all(message["content"] for message in result["messages"])


def test_tui_transports_choice_objects_in_message_payload() -> None:
    namespace: dict[str, object] = {
        "_clear_inflight_turn": lambda session: None,
        "_emit": lambda *args: None,
        "evaluate_goal": lambda: None,
        "maybe_auto_title": lambda: None,
        "_get_usage": lambda agent: {},
        "_session_info": lambda agent, session: {},
    }
    exec(installer.change_tui_gateway_source(TUI_GATEWAY_SOURCE), namespace)
    prompt = {
        **_valid_cli_prompt(),
        "choices": [
            {"id": "build", "label": "Build", "description": "Build only."},
            {
                "id": "build_review",
                "label": "Build + Review",
                "description": "Build and review.",
            },
        ],
    }

    class Agent:
        @staticmethod
        def run_conversation(text, **kwargs):
            assert kwargs["is_user_turn"] is True
            return prompt

    payload = namespace["process"](
        Agent(),
        [],
        None,
        {
            "history_lock": threading.RLock(),
            "running": True,
            "transport": types.SimpleNamespace(write=lambda frame: True),
        },
        "session-1",
        "request",
        "Choose checks.",
        "handled",
    )

    assert payload["choices"] == prompt["choices"]


def test_gateway_displays_choice_labels_without_changing_stable_ids() -> None:
    changed = installer.change_gateway_source(GATEWAY_SOURCE)

    assert "Available choices:" in changed
    assert "[id: {_choice_id}]" in changed
    assert "choose {_choice_prompt_id}" in changed
    assert '_choice["id"]' in changed
    assert '_choice["label"]' in changed


def test_choice_display_keeps_legacy_exact_id_fallback_without_none_prompt() -> None:
    namespace = {
        "result": {
            "choices": [
                {"id": "retry", "label": "Retry", "description": "Try again."}
            ]
        },
        "response": "Choose what to do.",
    }

    exec("\n".join(installer._choice_display_lines("result", "response")), namespace)

    assert "Reply with exact ID." in namespace["response"]
    assert "choose None" not in namespace["response"]


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
