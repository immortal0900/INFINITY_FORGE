from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from forge.ops import subscription_runner
from forge.ops.subscription_runtime import RuntimeKind
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
