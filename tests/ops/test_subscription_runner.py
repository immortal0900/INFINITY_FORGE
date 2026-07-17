from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from forge.ops.subscription_runner import (
    CompletedAttempt,
    GitContext,
    SkillRequest,
    SubscriptionRunner,
    WorkerRequest,
    build_claude_continuation_prompt,
)
from forge.ops.subscription_runtime import (
    CodexSubscriptionSnapshot,
    ExitClass,
    RuntimeKind,
)


VALID_AUTH = {
    "loggedIn": True,
    "authMethod": "claude.ai",
    "apiProvider": "firstParty",
    "subscriptionType": "max",
}
TERMINAL = {"done", "blocked", "triage", "archived"}


def available_snapshot() -> CodexSubscriptionSnapshot:
    return CodexSubscriptionSnapshot("chatgpt", "plus", None, False)


def quota_snapshot() -> CodexSubscriptionSnapshot:
    return CodexSubscriptionSnapshot("chatgpt", "plus", "primary", False)


def result(
    returncode: int, events: Sequence[Mapping[str, object]] = ()
) -> CompletedAttempt:
    return CompletedAttempt(
        returncode,
        tuple(events),
        "2026-07-17T01:00:00Z",
        "2026-07-17T01:00:01Z",
    )


class SequenceProcess:
    def __init__(self, results: Sequence[CompletedAttempt]) -> None:
        self.results = list(results)
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        argv: Sequence[str],
        cwd: str,
        env: Mapping[str, str],
        stdin_text: str | None,
        stdout_path: Path | None,
    ) -> CompletedAttempt:
        self.calls.append(
            {
                "argv": list(argv),
                "cwd": cwd,
                "env": dict(env),
                "stdin_text": stdin_text,
                "stdout_path": stdout_path,
            }
        )
        return self.results.pop(0)


class SequenceProbe:
    def __init__(self, results: Sequence[object]) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def probe(
        self, codex_bin: str, env: Mapping[str, str], timeout: float = 10.0
    ) -> CodexSubscriptionSnapshot:
        self.calls.append((codex_bin, dict(env)))
        value = self.results.pop(0)
        if isinstance(value, Exception):
            raise value
        assert isinstance(value, CodexSubscriptionSnapshot)
        return value


class FakeKanban:
    def __init__(self, status: str = "done", block_code: int = 0) -> None:
        self.current_status = status
        self.block_code = block_code
        self.status_calls: list[tuple[str, dict[str, str]]] = []
        self.block_calls: list[tuple[str, str, dict[str, str]]] = []

    def status(self, task_id: str, env: Mapping[str, str]) -> str:
        self.status_calls.append((task_id, dict(env)))
        return self.current_status

    def block(self, task_id: str, reason: str, env: Mapping[str, str]) -> int:
        self.block_calls.append((task_id, reason, dict(env)))
        return self.block_code


def worker_request(
    *, workspace: str = "C:/work/한글 작업", secret: str = "do-not-leak"
) -> WorkerRequest:
    return WorkerRequest(
        workspace=workspace,
        original_argv=("C:/Hermes/hermes.exe", "chat", "--resume", "abc -- x"),
        env={
            "PATH": "kept",
            "OPENAI_API_KEY": secret,
            "ANTHROPIC_API_KEY": secret,
            "HERMES_KANBAN_TASK": "task-42",
            "HERMES_KANBAN_RUN_ID": "run-42",
            "HERMES_KANBAN_WORKSPACE": workspace,
            "HERMES_KANBAN_BRANCH": "wt/task-42",
        },
    )


def make_runner(
    process: SequenceProcess,
    probe: SequenceProbe,
    *,
    kanban: FakeKanban | None = None,
    auth: Mapping[str, object] = VALID_AUTH,
    receipts: list[object] | None = None,
) -> SubscriptionRunner:
    receipt_sink = receipts if receipts is not None else []
    return SubscriptionRunner(
        process_runner=process,
        probe=probe,
        kanban=kanban or FakeKanban(),
        git_context=lambda workspace, env: GitContext(" M partial.py", " 1 file changed"),
        claude_auth_status=lambda claude_bin, env: auth,
        receipt_writer=receipt_sink.append,
        codex_bin="codex",
        claude_bin="claude",
        claude_mcp_config="C:/config/hermes-tools.json",
    )


def test_worker_falls_back_once_only_after_confirmed_quota() -> None:
    process = SequenceProcess([result(75), result(0)])
    probe = SequenceProbe([available_snapshot(), quota_snapshot()])

    outcome = make_runner(process, probe).run_worker(worker_request())

    assert [Path(call["argv"][0]).name for call in process.calls] == [
        "hermes.exe",
        "claude",
    ]
    assert outcome.returncode == 0
    assert outcome.final_runtime is RuntimeKind.CLAUDE


def test_transient_exit_75_is_returned_without_claude() -> None:
    process = SequenceProcess([result(75)])
    probe = SequenceProbe([available_snapshot(), available_snapshot()])

    outcome = make_runner(process, probe).run_worker(worker_request())

    assert outcome.returncode == 75
    assert len(process.calls) == 1
    assert process.calls[0]["argv"] == list(worker_request().original_argv)


def test_worker_success_requires_a_terminal_task() -> None:
    process = SequenceProcess([result(0)])
    kanban = FakeKanban("running")

    outcome = make_runner(
        process, SequenceProbe([available_snapshot()]), kanban=kanban
    ).run_worker(worker_request())

    assert outcome.returncode == 70
    assert len(kanban.block_calls) == 1
    assert "terminal" in kanban.block_calls[0][1]


@pytest.mark.parametrize("status", sorted(TERMINAL))
def test_worker_success_accepts_only_canonical_terminal_states(status: str) -> None:
    process = SequenceProcess([result(0)])
    outcome = make_runner(
        process, SequenceProbe([available_snapshot()]), kanban=FakeKanban(status)
    ).run_worker(worker_request())

    assert outcome.returncode == 0
    assert outcome.final_runtime is RuntimeKind.CODEX


@pytest.mark.parametrize("status", ["running", "ready", "todo", "scheduled"])
def test_worker_success_rejects_nonterminal_states(status: str) -> None:
    process = SequenceProcess([result(0)])
    kanban = FakeKanban(status)
    outcome = make_runner(
        process, SequenceProbe([available_snapshot()]), kanban=kanban
    ).run_worker(worker_request())

    assert outcome.returncode == 70
    assert len(kanban.block_calls) == 1


def test_preflight_quota_skips_codex_and_runs_claude_once() -> None:
    process = SequenceProcess([result(0)])

    outcome = make_runner(process, SequenceProbe([quota_snapshot()])).run_worker(
        worker_request()
    )

    assert len(process.calls) == 1
    assert process.calls[0]["argv"] == [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--max-turns",
        "20",
        "--permission-mode",
        "bypassPermissions",
        "--mcp-config",
        "C:/config/hermes-tools.json",
        "--strict-mcp-config",
    ]
    assert outcome.final_runtime is RuntimeKind.CLAUDE


def test_probe_error_is_not_quota_and_codex_still_runs() -> None:
    process = SequenceProcess([result(23)])

    outcome = make_runner(process, SequenceProbe([RuntimeError("probe failed")])).run_worker(
        worker_request()
    )

    assert outcome.returncode == 23
    assert len(process.calls) == 1


@pytest.mark.parametrize("returncode", [1, 2, 23, 78, 137])
def test_unknown_auth_network_and_tool_codes_do_not_fallback(returncode: int) -> None:
    process = SequenceProcess([result(returncode)])

    outcome = make_runner(process, SequenceProbe([available_snapshot()])).run_worker(
        worker_request()
    )

    assert outcome.returncode == returncode
    assert len(process.calls) == 1


def test_worker_blocks_after_both_subscriptions_reach_quota() -> None:
    quota_event = {"type": "system", "subtype": "api_retry", "error": "rate_limit"}
    process = SequenceProcess([result(75), result(1, [quota_event])])
    kanban = FakeKanban("running")

    outcome = make_runner(
        process,
        SequenceProbe([available_snapshot(), quota_snapshot()]),
        kanban=kanban,
    ).run_worker(worker_request())

    assert outcome.returncode == 0
    assert len(kanban.block_calls) == 1
    assert "Codex와 Claude 구독 한도 소진" in kanban.block_calls[0][1]


def test_worker_returns_contract_error_when_quota_block_fails() -> None:
    quota_event = {"type": "system", "subtype": "api_retry", "error": "rate_limit"}
    process = SequenceProcess([result(75), result(1, [quota_event])])
    kanban = FakeKanban("running", block_code=3)

    outcome = make_runner(
        process,
        SequenceProbe([available_snapshot(), quota_snapshot()]),
        kanban=kanban,
    ).run_worker(worker_request())

    assert outcome.returncode == 70


def test_claude_requires_exact_max_first_party_auth_before_invocation() -> None:
    process = SequenceProcess([])
    invalid_auth = dict(VALID_AUTH, subscriptionType="pro")

    outcome = make_runner(
        process, SequenceProbe([quota_snapshot()]), auth=invalid_auth
    ).run_worker(worker_request())

    assert outcome.returncode == 78
    assert process.calls == []


def test_worker_preserves_original_argv_unicode_workspace_and_scrubs_child_env() -> None:
    request = worker_request()
    process = SequenceProcess([result(23)])

    make_runner(process, SequenceProbe([available_snapshot()])).run_worker(request)

    assert process.calls[0]["argv"] == list(request.original_argv)
    assert process.calls[0]["cwd"] == request.workspace
    assert process.calls[0]["env"]["HERMES_KANBAN_TASK"] == "task-42"
    assert "OPENAI_API_KEY" not in process.calls[0]["env"]
    assert "ANTHROPIC_API_KEY" not in process.calls[0]["env"]
    assert request.env["OPENAI_API_KEY"] == "do-not-leak"


def test_claude_prompt_contains_continuation_context_without_environment_secret() -> None:
    process = SequenceProcess([result(0)])
    receipts: list[object] = []

    outcome = make_runner(
        process, SequenceProbe([quota_snapshot()]), receipts=receipts
    ).run_worker(worker_request())

    prompt = str(process.calls[0]["stdin_text"])
    assert "task-42" in prompt
    assert "run-42" in prompt
    assert "C:/work/한글 작업" in prompt
    assert "wt/task-42" in prompt
    assert "Codex 구독 한도" in prompt
    assert " M partial.py" in prompt
    assert "1 file changed" in prompt
    assert "부분 변경" in prompt
    assert "kanban_complete" in prompt
    assert "kanban_block" in prompt
    assert "정확히 한 번" in prompt
    assert "do-not-leak" not in prompt
    assert outcome.receipt == receipts[0]
    assert "do-not-leak" not in json.dumps(outcome.receipt, default=str)


def test_git_context_failure_is_explicit_in_prompt_and_does_not_change_fallback() -> None:
    process = SequenceProcess([result(0)])
    runner = SubscriptionRunner(
        process_runner=process,
        probe=SequenceProbe([quota_snapshot()]),
        kanban=FakeKanban(),
        git_context=lambda workspace, env: GitContext(error="git unavailable"),
        claude_auth_status=lambda claude_bin, env: VALID_AUTH,
        receipt_writer=lambda receipt: None,
        codex_bin="codex",
        claude_bin="claude",
        claude_mcp_config="mcp.json",
    )

    outcome = runner.run_worker(worker_request())

    assert outcome.returncode == 0
    assert "git unavailable" in str(process.calls[0]["stdin_text"])


def test_claude_auth_is_checked_once_before_the_runtime_attempt() -> None:
    process = SequenceProcess([result(0)])
    auth_calls: list[tuple[str, dict[str, str]]] = []

    def auth_status(
        claude_bin: str, env: Mapping[str, str]
    ) -> Mapping[str, object]:
        auth_calls.append((claude_bin, dict(env)))
        return VALID_AUTH

    runner = SubscriptionRunner(
        process_runner=process,
        probe=SequenceProbe([quota_snapshot()]),
        kanban=FakeKanban(),
        git_context=lambda workspace, env: GitContext(),
        claude_auth_status=auth_status,
        receipt_writer=lambda receipt: None,
        claude_mcp_config="mcp.json",
    )

    outcome = runner.run_worker(worker_request())

    assert outcome.returncode == 0
    assert len(auth_calls) == 1


def test_codex_skill_uses_exact_argv_and_returns_success_without_probe() -> None:
    process = SequenceProcess([result(0)])
    probe = SequenceProbe([])
    request = SkillRequest("C:/work/한글", "fix it", {"OPENAI_API_KEY": "secret"})

    outcome = make_runner(process, probe).run_codex_skill(request)

    assert process.calls[0]["argv"] == [
        "codex",
        "exec",
        "--json",
        "--sandbox",
        "workspace-write",
        "--ephemeral",
        "-C",
        "C:/work/한글",
        "-",
    ]
    assert process.calls[0]["stdin_text"] == "fix it"
    assert "OPENAI_API_KEY" not in process.calls[0]["env"]
    assert outcome.returncode == 0
    assert probe.calls == []


def test_codex_skill_falls_back_once_only_after_structured_probe_quota() -> None:
    process = SequenceProcess([result(5), result(0)])

    outcome = make_runner(process, SequenceProbe([quota_snapshot()])).run_codex_skill(
        SkillRequest("C:/work", "finish", {})
    )

    assert [call["argv"][0] for call in process.calls] == ["codex", "claude"]
    assert outcome.returncode == 0
    assert outcome.final_runtime is RuntimeKind.CLAUDE


def test_codex_skill_returns_original_failure_when_probe_errors() -> None:
    process = SequenceProcess([result(41)])

    outcome = make_runner(
        process, SequenceProbe([RuntimeError("unknown")])
    ).run_codex_skill(SkillRequest("C:/work", "finish", {}))

    assert outcome.returncode == 41
    assert len(process.calls) == 1


def test_claude_skill_never_calls_codex_and_maps_exact_quota_to_75() -> None:
    quota_event = {"type": "system", "subtype": "api_retry", "error": "rate_limit"}
    process = SequenceProcess([result(1, [quota_event])])
    probe = SequenceProbe([])

    outcome = make_runner(process, probe).run_claude_skill(
        SkillRequest("C:/work", "finish", {})
    )

    assert len(process.calls) == 1
    assert process.calls[0]["argv"][0] == "claude"
    assert outcome.returncode == 75
    assert probe.calls == []


def test_claude_skill_returns_nonquota_failure_unchanged() -> None:
    process = SequenceProcess([result(19, [{"type": "result", "is_error": True}])])

    outcome = make_runner(process, SequenceProbe([])).run_claude_skill(
        SkillRequest("C:/work", "finish", {})
    )

    assert outcome.returncode == 19


def test_build_prompt_includes_skill_instruction_but_not_environment() -> None:
    prompt = build_claude_continuation_prompt(
        original_prompt="repair the parser",
        task_id=None,
        run_id=None,
        workspace="C:/work",
        branch=None,
        git_context=GitContext("clean", "no diff"),
    )

    assert "repair the parser" in prompt
    assert "C:/work" in prompt
    assert "API_KEY" not in prompt
