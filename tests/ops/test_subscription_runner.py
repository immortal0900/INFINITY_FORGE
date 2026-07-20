from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from forge.ops.subscription_runner import (
    CompletedAttempt,
    GitContext,
    SkillRequest,
    SubprocessKanban,
    SubscriptionRunner,
    WorkerRequest,
    build_claude_continuation_prompt,
    default_process_runner,
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
    "subscriptionType": None,
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


class FailingStatusKanban(FakeKanban):
    def status(self, task_id: str, env: Mapping[str, str]) -> str:
        self.status_calls.append((task_id, dict(env)))
        raise RuntimeError("malformed show response")


def worker_request(
    *, workspace: str = "C:/work/한글 작업", secret: str = "do-not-leak"
) -> WorkerRequest:
    hermes_bin = str(Path(sys.executable).resolve())
    return WorkerRequest(
        workspace=workspace,
        original_argv=(hermes_bin, "chat", "--resume", "abc -- x"),
        env={
            "PATH": "kept",
            "OPENAI_API_KEY": secret,
            "ANTHROPIC_API_KEY": secret,
            "HERMES_KANBAN_TASK": "task-42",
            "HERMES_KANBAN_RUN_ID": "run-42",
            "HERMES_KANBAN_WORKSPACE": workspace,
            "HERMES_KANBAN_BRANCH": "wt/task-42",
            "INFINITY_FORGE_HERMES_BIN": hermes_bin,
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
        Path(sys.executable).resolve().name,
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


def test_worker_status_exception_attempts_one_block_with_same_scrubbed_env() -> None:
    process = SequenceProcess([result(0)])
    kanban = FailingStatusKanban()

    outcome = make_runner(
        process, SequenceProbe([available_snapshot()]), kanban=kanban
    ).run_worker(worker_request())

    assert outcome.returncode == 70
    assert len(kanban.status_calls) == 1
    assert len(kanban.block_calls) == 1
    task_id, reason, block_env = kanban.block_calls[0]
    assert task_id == "task-42"
    assert "terminal" in reason
    assert block_env == kanban.status_calls[0][1]
    assert "OPENAI_API_KEY" not in block_env


def test_worker_status_exception_remains_70_when_block_fails() -> None:
    process = SequenceProcess([result(0)])
    kanban = FailingStatusKanban(block_code=9)

    outcome = make_runner(
        process, SequenceProbe([available_snapshot()]), kanban=kanban
    ).run_worker(worker_request())

    assert outcome.returncode == 70
    assert len(kanban.block_calls) == 1


def test_worker_malformed_default_show_blocks_once_with_utf8_child_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = SequenceProcess([result(0)])
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        stdout = '{"task":{"title":"깨짐 🧪𐐷"}}' if "show" in argv else ""
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr("forge.ops.subscription_runner.subprocess.run", run)

    outcome = make_runner(
        process,
        SequenceProbe([available_snapshot()]),
        kanban=SubprocessKanban(),  # type: ignore[arg-type]
    ).run_worker(worker_request())

    assert outcome.returncode == 70
    assert ["show" in call[0] for call in calls] == [True, False]
    assert calls[1][0][2:6] == ["block", "--kind", "capability", "task-42"]
    assert calls[0][1]["env"] == calls[1][1]["env"]
    assert calls[0][1]["env"]["PYTHONUTF8"] == "1"
    assert "OPENAI_API_KEY" not in calls[0][1]["env"]


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


def test_claude_rejects_api_auth_before_invocation() -> None:
    process = SequenceProcess([])
    invalid_auth = dict(VALID_AUTH, authMethod="api_key")

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


@pytest.mark.parametrize("argv0", ["hermes", "relative/hermes"])
def test_worker_rejects_relative_or_path_lookup_executable_before_runtime(
    argv0: str,
) -> None:
    request = worker_request()
    request = WorkerRequest(request.workspace, (argv0, *request.original_argv[1:]), request.env)
    process = SequenceProcess([])
    receipts: list[object] = []

    outcome = make_runner(
        process, SequenceProbe([]), receipts=receipts
    ).run_worker(request)

    assert outcome.returncode == 78
    assert outcome.final_runtime is None
    assert outcome.receipt is receipts[0]
    assert outcome.receipt.attempts == ()
    assert process.calls == []
    assert argv0 not in json.dumps(outcome.receipt, default=str)


def test_worker_rejects_nonexistent_original_executable_before_runtime(
    tmp_path: Path,
) -> None:
    request = worker_request()
    missing = tmp_path / ("missing.exe" if os.name == "nt" else "missing")
    request = WorkerRequest(
        request.workspace, (str(missing), *request.original_argv[1:]), request.env
    )
    process = SequenceProcess([])

    outcome = make_runner(process, SequenceProbe([])).run_worker(request)

    assert outcome.returncode == 78
    assert outcome.final_runtime is None
    assert outcome.receipt.attempts == ()
    assert process.calls == []
    assert str(missing) not in json.dumps(outcome.receipt, default=str)


def test_worker_rejects_missing_configured_executable_before_runtime() -> None:
    request = worker_request()
    env = dict(request.env)
    env.pop("INFINITY_FORGE_HERMES_BIN")
    process = SequenceProcess([])

    outcome = make_runner(process, SequenceProbe([])).run_worker(
        WorkerRequest(request.workspace, request.original_argv, env)
    )

    assert outcome.returncode == 78
    assert outcome.final_runtime is None
    assert outcome.receipt.attempts == ()
    assert process.calls == []


def test_worker_rejects_missing_original_executable_and_keeps_receipt_identity() -> None:
    request = worker_request()
    process = SequenceProcess([])

    outcome = make_runner(process, SequenceProbe([])).run_worker(
        WorkerRequest(request.workspace, (), request.env)
    )

    assert outcome.returncode == 78
    assert outcome.final_runtime is None
    assert outcome.receipt.task_id == "task-42"
    assert outcome.receipt.run_id == "run-42"
    assert outcome.receipt.attempts == ()
    assert process.calls == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable-bit contract")
def test_worker_rejects_non_executable_posix_binary(tmp_path: Path) -> None:
    binary = tmp_path / "hermes"
    binary.write_bytes(b"not executable")
    binary.chmod(0o644)
    request = worker_request()
    env = dict(request.env, INFINITY_FORGE_HERMES_BIN=str(binary))
    process = SequenceProcess([])

    outcome = make_runner(process, SequenceProbe([])).run_worker(
        WorkerRequest(request.workspace, (str(binary), "chat"), env)
    )

    assert outcome.returncode == 78
    assert outcome.final_runtime is None
    assert outcome.receipt.attempts == ()
    assert process.calls == []


@pytest.mark.skipif(os.name != "nt", reason="Windows native executable contract")
def test_worker_rejects_windows_script_shim_before_runtime(tmp_path: Path) -> None:
    shim = tmp_path / "hermes.cmd"
    shim.touch()
    request = worker_request()
    env = dict(request.env, INFINITY_FORGE_HERMES_BIN=str(shim))
    request = WorkerRequest(request.workspace, (str(shim), "chat"), env)
    process = SequenceProcess([])

    outcome = make_runner(process, SequenceProbe([])).run_worker(request)

    assert outcome.returncode == 78
    assert outcome.final_runtime is None
    assert outcome.receipt.attempts == ()
    assert process.calls == []


def test_worker_rejects_configured_executable_mismatch_before_runtime(
    tmp_path: Path,
) -> None:
    suffix = ".exe" if os.name == "nt" else ""
    other = tmp_path / f"other-hermes{suffix}"
    other.write_bytes(b"not executed")
    if os.name != "nt":
        other.chmod(0o755)
    request = worker_request()
    env = dict(request.env, INFINITY_FORGE_HERMES_BIN=str(other))
    request = WorkerRequest(request.workspace, request.original_argv, env)
    process = SequenceProcess([])

    outcome = make_runner(process, SequenceProbe([])).run_worker(request)

    assert outcome.returncode == 78
    assert outcome.final_runtime is None
    assert outcome.receipt.attempts == ()
    assert process.calls == []


def test_worker_runs_resolved_configured_executable_and_preserves_tail() -> None:
    request = worker_request()
    process = SequenceProcess([result(23)])

    outcome = make_runner(
        process, SequenceProbe([available_snapshot()])
    ).run_worker(request)

    assert outcome.returncode == 23
    assert process.calls[0]["argv"][0] == str(Path(sys.executable).resolve())
    assert process.calls[0]["argv"][1:] == list(request.original_argv[1:])


def test_worker_uses_hermes_bin_only_when_primary_config_is_absent() -> None:
    request = worker_request()
    env = dict(request.env)
    hermes_bin = env.pop("INFINITY_FORGE_HERMES_BIN")
    env["HERMES_BIN"] = hermes_bin
    process = SequenceProcess([result(23)])

    outcome = make_runner(
        process, SequenceProbe([available_snapshot()])
    ).run_worker(WorkerRequest(request.workspace, request.original_argv, env))

    assert outcome.returncode == 23
    assert process.calls[0]["argv"][0] == hermes_bin


def test_worker_does_not_fall_through_invalid_primary_config(tmp_path: Path) -> None:
    request = worker_request()
    missing = tmp_path / ("missing.exe" if os.name == "nt" else "missing")
    env = dict(
        request.env,
        INFINITY_FORGE_HERMES_BIN=str(missing),
        HERMES_BIN=request.original_argv[0],
    )
    process = SequenceProcess([])

    outcome = make_runner(process, SequenceProbe([])).run_worker(
        WorkerRequest(request.workspace, request.original_argv, env)
    )

    assert outcome.returncode == 78
    assert outcome.receipt.attempts == ()
    assert process.calls == []


def test_worker_spawn_oserror_becomes_unknown_contract_attempt() -> None:
    class SpawnFailure(SequenceProcess):
        def __call__(self, *args: object, **kwargs: object) -> CompletedAttempt:
            raise OSError("raw path must not escape")

    process = SpawnFailure([])

    outcome = make_runner(
        process, SequenceProbe([available_snapshot()])
    ).run_worker(worker_request())

    assert outcome.returncode == 70
    assert outcome.final_runtime is RuntimeKind.CODEX
    assert len(outcome.receipt.attempts) == 1
    assert outcome.receipt.attempts[0].exit_class is ExitClass.UNKNOWN
    assert "raw path" not in json.dumps(outcome.receipt, default=str)


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


@pytest.mark.parametrize(
    ("error", "returncode", "exit_class"),
    [
        (OSError("spawn failed"), 70, ExitClass.UNKNOWN),
        (
            subprocess.TimeoutExpired(["codex"], 1),
            70,
            ExitClass.TIMEOUT,
        ),
        (KeyboardInterrupt(), 70, ExitClass.CANCELLED),
    ],
)
def test_codex_skill_process_boundary_failures_never_probe_or_fallback(
    error: BaseException, returncode: int, exit_class: ExitClass
) -> None:
    class FailingProcess(SequenceProcess):
        def __call__(self, *args: object, **kwargs: object) -> CompletedAttempt:
            raise error

    process = FailingProcess([])
    probe = SequenceProbe([])

    outcome = make_runner(process, probe).run_codex_skill(
        SkillRequest("C:/work", "finish", {})
    )

    assert outcome.returncode == returncode
    assert outcome.receipt.attempts[0].exit_class is exit_class
    assert probe.calls == []


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


@pytest.mark.parametrize("returncode", [23, 75])
def test_codex_skill_preserves_actual_early_child_code_and_post_probes(
    returncode: int,
) -> None:
    class ActualEarlyExitProcess(SequenceProcess):
        def __call__(
            self,
            argv: Sequence[str],
            cwd: str,
            env: Mapping[str, str],
            stdin_text: str | None,
            stdout_path: Path | None,
        ) -> CompletedAttempt:
            self.calls.append({"argv": list(argv)})
            return default_process_runner(
                [sys.executable, "-c", f"raise SystemExit({returncode})"],
                str(Path.cwd()),
                dict(os.environ, PYTHONUTF8="1"),
                stdin_text,
                stdout_path,
            )

    process = ActualEarlyExitProcess([])
    probe = SequenceProbe([available_snapshot()])

    outcome = make_runner(process, probe).run_codex_skill(
        SkillRequest("C:/work", "대용량 🧪𐐷" * 200_000, {})
    )

    assert outcome.returncode == returncode
    assert outcome.receipt.attempts[0].returncode == returncode
    assert len(probe.calls) == 1


def test_claude_quota_event_survives_writer_broken_pipe_on_nonzero_child() -> None:
    class ActualQuotaExitProcess(SequenceProcess):
        def __call__(
            self,
            argv: Sequence[str],
            cwd: str,
            env: Mapping[str, str],
            stdin_text: str | None,
            stdout_path: Path | None,
        ) -> CompletedAttempt:
            child = (
                "import json; print(json.dumps({'type':'system',"
                "'subtype':'api_retry','error':'rate_limit'}), flush=True); "
                "raise SystemExit(9)"
            )
            return default_process_runner(
                [sys.executable, "-c", child],
                str(Path.cwd()),
                dict(os.environ, PYTHONUTF8="1"),
                stdin_text,
                stdout_path,
            )

    outcome = make_runner(ActualQuotaExitProcess([]), SequenceProbe([])).run_claude_skill(
        SkillRequest("C:/work", "대용량 🧪𐐷" * 200_000, {})
    )

    assert outcome.returncode == 75
    assert outcome.receipt.attempts[0].exit_class is ExitClass.SUBSCRIPTION_QUOTA


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
