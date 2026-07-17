from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path

import pytest

from forge.ops import subscription_runner
from forge.ops.subscription_runtime import (
    ExitClass,
    RuntimeKind,
    classify_claude_stream,
)
from forge.ops.subscription_runner import SubscriptionRunResult


class FakeRunner:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.worker_requests: list[object] = []
        self.codex_requests: list[object] = []
        self.claude_requests: list[object] = []

    def _result(self) -> SubscriptionRunResult:
        return SubscriptionRunResult(self.returncode, RuntimeKind.CODEX, None)

    def run_worker(self, request: object) -> SubscriptionRunResult:
        self.worker_requests.append(request)
        return self._result()

    def run_codex_skill(self, request: object) -> SubscriptionRunResult:
        self.codex_requests.append(request)
        return self._result()

    def run_claude_skill(self, request: object) -> SubscriptionRunResult:
        self.claude_requests.append(request)
        return self._result()


def test_cli_preserves_every_worker_argument_after_separator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRunner(23)
    workspace = "C:/작업 공간"
    monkeypatch.setattr(subscription_runner, "_build_runner", lambda env: fake)
    monkeypatch.setattr(
        subscription_runner.os,
        "environ",
        {
            "HERMES_KANBAN_TASK": "task-42",
            "HERMES_KANBAN_RUN_ID": "run-42",
            "HERMES_KANBAN_WORKSPACE": workspace,
            "HERMES_KANBAN_BRANCH": "wt/task-42",
        },
    )

    code = subscription_runner.main(
        ["worker", "--workspace", workspace, "--", "hermes", "chat", "--", "x y", ""]
    )

    assert code == 23
    request = fake.worker_requests[0]
    assert request.original_argv == ("hermes", "chat", "--", "x y", "")
    assert request.workspace == workspace


@pytest.mark.parametrize("mode", ["codex-skill", "claude-skill"])
def test_cli_reads_utf8_prompt_file_for_skill_modes(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRunner()
    prompt_file = tmp_path / "지시문.txt"
    prompt_file.write_text("부분 변경을 이어서 완료", encoding="utf-8")
    monkeypatch.setattr(subscription_runner, "_build_runner", lambda env: fake)

    code = subscription_runner.main(
        [mode, "--workspace", "C:/작업", "--prompt-file", str(prompt_file)]
    )

    assert code == 0
    requests = fake.codex_requests if mode == "codex-skill" else fake.claude_requests
    assert requests[0].prompt == "부분 변경을 이어서 완료"


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["worker", "--workspace", "C:/work"],
        ["codex-skill", "--workspace", "C:/work"],
        ["claude-skill", "--workspace", "C:/work", "--prompt-file", "missing"],
    ],
)
def test_cli_returns_78_for_invalid_or_missing_required_input(argv: list[str]) -> None:
    assert subscription_runner.main(argv) == 78


def test_cli_help_returns_success() -> None:
    assert subscription_runner.main(["--help"]) == 0


def test_cli_rejects_workspace_that_differs_from_worker_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subscription_runner.os,
        "environ",
        {"HERMES_KANBAN_WORKSPACE": "C:/actual"},
    )

    assert (
        subscription_runner.main(
            ["worker", "--workspace", "C:/other", "--", "hermes", "chat"]
        )
        == 78
    )


def test_stable_script_uses_repository_env_when_run_outside_repo(tmp_path: Path) -> None:
    script = Path("forge/scripts/subscription-runner.py").resolve()
    environment = dict(os.environ)
    environment["INFINITY_FORGE_REPO"] = str(Path.cwd())

    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 78
    assert "ModuleNotFoundError" not in completed.stderr


def test_stable_script_help_returns_success_with_usage(tmp_path: Path) -> None:
    script = Path("forge/scripts/subscription-runner.py").resolve()
    environment = dict(os.environ)
    environment["INFINITY_FORGE_REPO"] = str(Path.cwd())

    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=False,
    )

    assert completed.returncode == 0
    assert "usage: subscription-runner.py" in completed.stdout


def test_default_kanban_uses_exact_show_and_block_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "hermes.exe"
    executable.touch()
    calls: list[tuple[list[str], dict[str, object]]] = []
    parent_env = {"INFINITY_FORGE_HERMES_BIN": str(executable), "PYTHONUTF8": "0"}

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        stdout = (
            '{"task":{"status":"done","title":"🧪𐐷"}}'
            if "show" in argv
            else ""
        )
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(subscription_runner.subprocess, "run", run)
    kanban = subscription_runner.SubprocessKanban()
    env = parent_env

    assert kanban.status("task-42", env) == "done"
    assert kanban.block("task-42", "reason", env) == 0
    assert [call[0] for call in calls] == [
        [str(executable), "kanban", "show", "task-42", "--json"],
        [
            str(executable),
            "kanban",
            "block",
            "--kind",
            "capability",
            "task-42",
            "reason",
        ],
    ]
    assert all(call[1]["encoding"] == "utf-8" for call in calls)
    assert all(call[1]["errors"] == "strict" for call in calls)
    assert all(call[1]["env"]["PYTHONUTF8"] == "1" for call in calls)
    assert parent_env["PYTHONUTF8"] == "0"


def test_default_kanban_fails_closed_without_explicit_native_binary() -> None:
    kanban = subscription_runner.SubprocessKanban()

    with pytest.raises(subscription_runner.ConfigurationError):
        kanban.status("task-42", {})


def test_default_git_context_reports_missing_git_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subscription_runner.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )

    context = subscription_runner.default_git_context("C:/work", {})

    assert context.status == ""
    assert context.diff_stat == ""
    assert "git status --short could not start" in str(context.error)
    assert "git diff --stat could not start" in str(context.error)


def test_process_runner_streams_utf8_jsonl_and_discards_raw_fields(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    child = (
        "import json,sys; "
        "value=sys.stdin.read(); "
        "print('not-json'); "
        "print(json.dumps({'type':'system','subtype':'api_retry',"
        "'error':'rate_limit','is_error':True,'tool_output':value,"
        "'secret':'🧪𐐷'}, ensure_ascii=False)); "
        "print(json.dumps({'error':{'secret':value}}, ensure_ascii=False)); "
        "print(json.dumps(['not','an','object']))"
    )

    completed = subscription_runner.default_process_runner(
        [sys.executable, "-c", child],
        str(tmp_path),
        dict(os.environ, PYTHONUTF8="1"),
        "입력 🧪𐐷",
        None,
    )

    assert completed.returncode == 0
    assert completed.events == (
        {
            "type": "system",
            "subtype": "api_retry",
            "error": "rate_limit",
            "is_error": True,
        },
    )
    assert "tool_output" not in json.dumps(completed.events, ensure_ascii=False)
    assert "🧪𐐷" not in json.dumps(completed.events, ensure_ascii=False)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_process_runner_large_unicode_prompt_uses_no_file_and_does_not_deadlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt = "대용량 🧪𐐷" * 200_000
    child = (
        "import json,sys; value=sys.stdin.read(); "
        "assert value.startswith('대용량') and value.endswith('𐐷'); "
        "print(json.dumps({'type':'system','subtype':'api_retry',"
        "'error':'rate_limit'}, ensure_ascii=False))"
    )
    monkeypatch.setattr(
        tempfile,
        "TemporaryFile",
        lambda *args, **kwargs: pytest.fail("prompt must never touch a temporary file"),
    )

    started = time.monotonic()
    completed = subscription_runner.default_process_runner(
        [sys.executable, "-c", child],
        str(tmp_path),
        dict(os.environ, PYTHONUTF8="1"),
        prompt,
        None,
    )

    assert completed.returncode == 0
    assert completed.events[-1]["error"] == "rate_limit"
    assert time.monotonic() - started < 10


@pytest.mark.parametrize("failure", ["timeout", "cancel"])
def test_process_runner_inherited_descendant_pipe_returns_bounded(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(0.8)']); "
        "time.sleep(10)"
    )
    monkeypatch.setattr(
        subscription_runner, "_PROCESS_WAIT_TIMEOUT_SECONDS", 0.15
    )
    if failure == "cancel":
        original_wait = subprocess.Popen.wait
        interrupted = False

        def wait(
            process: subprocess.Popen[str], timeout: float | None = None
        ) -> int:
            nonlocal interrupted
            if timeout is not None and not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            return original_wait(process, timeout=timeout)

        monkeypatch.setattr(subprocess.Popen, "wait", wait)

    started = time.monotonic()
    completed = subscription_runner.default_process_runner(
        [sys.executable, "-c", child],
        str(tmp_path),
        dict(os.environ, PYTHONUTF8="1"),
        "🧪𐐷" * 100_000,
        None,
    )
    elapsed = time.monotonic() - started

    assert completed.returncode == 70
    assert completed.failure_class is (
        ExitClass.TIMEOUT if failure == "timeout" else ExitClass.CANCELLED
    )
    assert elapsed < 3


def test_process_runner_caps_events_but_preserves_late_exact_quota(
    tmp_path: Path,
) -> None:
    child = (
        "import json; "
        "[print(json.dumps({'type':'assistant','subtype':str(i)})) "
        "for i in range(1000)]; "
        "print(json.dumps({'type':'system','subtype':'api_retry',"
        "'error':'rate_limit'})); "
        "print(json.dumps({'type':'system','subtype':'api_retry',"
        "'error':'billing_error'})); "
        "print(json.dumps({'type':'result','is_error':True}))"
    )

    completed = subscription_runner.default_process_runner(
        [sys.executable, "-c", child],
        str(tmp_path),
        dict(os.environ, PYTHONUTF8="1"),
        None,
        None,
    )

    assert len(completed.events) <= subscription_runner._MAX_CLASSIFIED_EVENTS
    assert {"type": "system", "subtype": "api_retry", "error": "rate_limit"} in completed.events
    assert {"type": "system", "subtype": "api_retry", "error": "billing_error"} in completed.events
    assert {"type": "result", "is_error": True} in completed.events
    assert (
        classify_claude_stream(completed.events, {
            "loggedIn": True,
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
            "subscriptionType": "max",
        })
        is ExitClass.SUBSCRIPTION_QUOTA
    )


def test_process_runner_oversized_error_is_bounded_unknown_not_quota(
    tmp_path: Path,
) -> None:
    child = (
        "import json; print(json.dumps({'type':'system',"
        "'subtype':'api_retry','error':'rate_limit'+'x'*100000}))"
    )

    completed = subscription_runner.default_process_runner(
        [sys.executable, "-c", child],
        str(tmp_path),
        dict(os.environ, PYTHONUTF8="1"),
        None,
        None,
    )

    assert len(completed.events) == 1
    assert "error" not in completed.events[0]
    assert all(
        not isinstance(value, str)
        or len(value) <= subscription_runner._MAX_CLASSIFICATION_STRING_CHARS
        for event in completed.events
        for value in event.values()
    )
    assert (
        classify_claude_stream(completed.events, {
            "loggedIn": True,
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
            "subscriptionType": "max",
        })
        is ExitClass.UNKNOWN
    )


def test_classification_fold_finish_is_atomic_and_ignores_late_reader_add() -> None:
    fold = subscription_runner._ClassificationFold()
    fold.add({"type": "system", "subtype": "api_retry", "error": "rate_limit"})
    reader_ready = threading.Event()
    release_reader = threading.Event()

    def reader() -> None:
        for index in range(100):
            fold.add({"type": "assistant", "subtype": str(index)})
        reader_ready.set()
        release_reader.wait(timeout=2)
        fold.add(
            {"type": "system", "subtype": "api_retry", "error": "billing_error"}
        )

    thread = threading.Thread(target=reader)
    thread.start()
    assert reader_ready.wait(timeout=2)
    frozen = fold.finish()
    release_reader.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert {"type": "system", "subtype": "api_retry", "error": "rate_limit"} in frozen
    assert {"type": "system", "subtype": "api_retry", "error": "billing_error"} not in frozen
    assert fold.finish() == frozen


def test_process_runner_freezes_classification_fold_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    original_finish = subscription_runner._ClassificationFold.finish

    def finish(fold: object) -> tuple[Mapping[str, object], ...]:
        nonlocal calls
        calls += 1
        return original_finish(fold)

    monkeypatch.setattr(subscription_runner._ClassificationFold, "finish", finish)

    completed = subscription_runner.default_process_runner(
        [sys.executable, "-c", "print('{}')"],
        str(tmp_path),
        dict(os.environ, PYTHONUTF8="1"),
        "prompt",
        None,
    )

    assert completed.returncode == 0
    assert calls == 1


@pytest.mark.parametrize(
    ("child_returncode", "expected_returncode", "expected_failure"),
    [
        (0, 70, ExitClass.UNKNOWN),
        (23, 23, None),
    ],
)
def test_reader_decode_failure_discards_partial_evidence_and_preserves_nonzero(
    child_returncode: int,
    expected_returncode: int,
    expected_failure: ExitClass | None,
    tmp_path: Path,
) -> None:
    child = (
        "import sys; "
        "sys.stdout.buffer.write(b'{\"type\":\"system\",\"subtype\":"
        "\"api_retry\",\"error\":\"rate_limit\"}\\n'+bytes([255])+b'\\n'); "
        "sys.stdout.buffer.flush(); "
        f"raise SystemExit({child_returncode})"
    )

    completed = subscription_runner.default_process_runner(
        [sys.executable, "-c", child],
        str(tmp_path),
        dict(os.environ, PYTHONUTF8="1"),
        "대용량 🧪𐐷" * 200_000,
        None,
    )

    assert completed.returncode == expected_returncode
    assert completed.failure_class is expected_failure
    assert completed.events == ()


@pytest.mark.parametrize(
    ("child_returncode", "expected_returncode", "expected_failure"),
    [
        (0, 70, ExitClass.UNKNOWN),
        (23, 23, None),
    ],
)
def test_injected_reader_failure_always_discards_already_folded_event(
    child_returncode: int,
    expected_returncode: int,
    expected_failure: ExitClass | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_after_valid_event(
        stream: object,
        fold: object,
        reader_failure: threading.Event,
    ) -> None:
        fold.add(
            {"type": "system", "subtype": "api_retry", "error": "rate_limit"}
        )
        reader_failure.set()
        subscription_runner._close_stream(stream)

    monkeypatch.setattr(subscription_runner, "_read_stdout", fail_after_valid_event)

    completed = subscription_runner.default_process_runner(
        [sys.executable, "-c", f"raise SystemExit({child_returncode})"],
        str(tmp_path),
        dict(os.environ, PYTHONUTF8="1"),
        "prompt",
        None,
    )

    assert completed.returncode == expected_returncode
    assert completed.failure_class is expected_failure
    assert completed.events == ()


def test_auth_status_parses_unicode_json_with_strict_utf8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        payload = {
            "loggedIn": True,
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
            "subscriptionType": "max",
            "displayName": "사용자 🧪𐐷",
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload, ensure_ascii=False), "")

    monkeypatch.setattr(subscription_runner.subprocess, "run", run)

    auth = subscription_runner.default_claude_auth_status("claude", {})

    assert auth["displayName"] == "사용자 🧪𐐷"
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "strict"


def test_default_kanban_rejects_malformed_unicode_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "hermes.exe"
    executable.touch()
    monkeypatch.setattr(
        subscription_runner.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, '{"task":{"title":"깨짐 🧪𐐷"}}', ""
        ),
    )

    with pytest.raises(RuntimeError, match="status response is invalid"):
        subscription_runner.SubprocessKanban().status(
            "task-42", {"INFINITY_FORGE_HERMES_BIN": str(executable)}
        )
