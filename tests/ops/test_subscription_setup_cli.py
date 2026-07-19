from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from forge.ops import subscription_setup
from forge.ops.subscription_setup import SubscriptionReadiness


class FakeSetup:
    calls: list[str] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def apply(self):
        self.calls.append("apply")
        return SubscriptionReadiness(
            True, "codex_app_server", "chatgpt", "claude.ai", True, False, None
        )

    def verify(self):
        self.calls.append("verify")
        return SubscriptionReadiness(
            True, "codex_app_server", "chatgpt", "claude.ai", True, False, None
        )

    def rollback(self):
        self.calls.append("rollback")
        return SubscriptionReadiness(False, None, None, None, False, False, None)


def test_cli_public_commands_emit_only_safe_json(monkeypatch, tmp_path: Path, capsys):
    FakeSetup.calls = []
    monkeypatch.setattr(subscription_setup, "SubscriptionRuntimeSetup", FakeSetup)

    for command in ("apply", "verify", "rollback"):
        argv = [command, "--hermes-root", str(tmp_path / "hermes")]
        if command != "rollback":
            argv += ["--forge-root", str(tmp_path)]
        assert subscription_setup.main(argv) == 0
        payload = json.loads(capsys.readouterr().out)
        assert set(payload) == {
            "ready",
            "runtime",
            "codex_account",
            "claude_subscription",
            "mcp",
            "rollback_required",
            "error",
        }
    assert FakeSetup.calls == ["apply", "verify", "rollback"]


def test_stable_script_help_and_invalid_command_are_bounded(tmp_path: Path):
    script = Path("forge/scripts/configure-subscription-runtime.py").resolve()
    help_result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    bad_result = subprocess.run(
        [sys.executable, str(script), "bad"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert help_result.returncode == 0
    assert bad_result.returncode == 78
    assert "Traceback" not in bad_result.stderr


def test_cli_readiness_failure_returns_78(monkeypatch, tmp_path: Path, capsys):
    class FailedSetup(FakeSetup):
        def apply(self):
            return SubscriptionReadiness(
                False, None, None, None, False, False, "preflight failed"
            )

    monkeypatch.setattr(subscription_setup, "SubscriptionRuntimeSetup", FailedSetup)

    assert (
        subscription_setup.main(
            ["apply", "--forge-root", str(tmp_path), "--hermes-root", str(tmp_path)]
        )
        == 78
    )
    assert json.loads(capsys.readouterr().out)["error"] == "preflight failed"


def test_cli_unexpected_adapter_error_is_generic_and_has_no_traceback(
    monkeypatch, tmp_path: Path, capsys
):
    def fail(**kwargs):
        raise OSError(f"private path: {tmp_path}")

    monkeypatch.setattr(subscription_setup, "SubscriptionRuntimeSetup", fail)

    assert (
        subscription_setup.main(
            ["verify", "--forge-root", str(tmp_path), "--hermes-root", str(tmp_path)]
        )
        == 78
    )
    captured = capsys.readouterr()
    assert str(tmp_path) not in captured.out + captured.err
    assert "Traceback" not in captured.err


def test_cli_keyboard_interrupt_is_generic_and_returns_78(
    monkeypatch, tmp_path: Path, capsys
):
    class InterruptedSetup(FakeSetup):
        def apply(self):
            raise KeyboardInterrupt(str(tmp_path / "private-config.yaml"))

    monkeypatch.setattr(
        subscription_setup, "SubscriptionRuntimeSetup", InterruptedSetup
    )

    assert (
        subscription_setup.main(
            ["apply", "--forge-root", str(tmp_path), "--hermes-root", str(tmp_path)]
        )
        == 78
    )
    captured = capsys.readouterr()
    assert str(tmp_path) not in captured.out + captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("exit_code", [0, "private exit code"])
def test_cli_setup_system_exit_is_generic_and_returns_78(
    monkeypatch, tmp_path: Path, capsys, exit_code
):
    class InterruptedSetup(FakeSetup):
        def apply(self):
            raise SystemExit(exit_code)

    monkeypatch.setattr(
        subscription_setup, "SubscriptionRuntimeSetup", InterruptedSetup
    )

    assert (
        subscription_setup.main(
            ["apply", "--forge-root", str(tmp_path), "--hermes-root", str(tmp_path)]
        )
        == 78
    )
    captured = capsys.readouterr()
    assert "private exit code" not in captured.out + captured.err
    assert str(tmp_path) not in captured.out + captured.err
    assert "Traceback" not in captured.err
