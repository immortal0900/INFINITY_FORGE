from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from forge.ops.subscription_runtime import (
    AttemptResult,
    CodexSubscriptionSnapshot,
    ExitClass,
    RunReceipt,
    RuntimeKind,
    classify_claude_stream,
    classify_codex_snapshot,
    scrub_subscription_environment,
    write_run_receipt,
)


def test_codex_quota_requires_chatgpt_and_backend_reached_type() -> None:
    snapshot = CodexSubscriptionSnapshot("chatgpt", "plus", "primary", False)

    assert classify_codex_snapshot(snapshot) is ExitClass.SUBSCRIPTION_QUOTA


def test_codex_spend_control_requires_chatgpt_account() -> None:
    snapshot = CodexSubscriptionSnapshot("chatgpt", "plus", None, True)

    assert classify_codex_snapshot(snapshot) is ExitClass.SUBSCRIPTION_QUOTA


def test_used_percent_and_message_are_not_quota_inputs() -> None:
    snapshot = CodexSubscriptionSnapshot("chatgpt", "plus", None, False)

    assert classify_codex_snapshot(snapshot) is ExitClass.SUCCESS


def test_non_chatgpt_codex_account_is_not_accepted_as_subscription_auth() -> None:
    snapshot = CodexSubscriptionSnapshot("api", "enterprise", "primary", True)

    assert classify_codex_snapshot(snapshot) is ExitClass.AUTH


def test_child_environment_removes_every_payg_switch_without_mutating_input() -> None:
    environment = {
        "OPENAI_API_KEY": "secret",
        "ANTHROPIC_API_KEY": "secret",
        "ANTHROPIC_AUTH_TOKEN": "secret",
        "CLAUDE_CODE_OAUTH_TOKEN": "secret",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "CLAUDE_CODE_USE_FOUNDRY": "1",
        "PATH": "kept",
    }

    assert scrub_subscription_environment(environment) == {"PATH": "kept"}
    assert environment["OPENAI_API_KEY"] == "secret"


def valid_claude_auth() -> dict[str, object]:
    return {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "subscriptionType": "max",
    }


def test_claude_rate_limit_requires_exact_max_first_party_auth() -> None:
    events = ({"type": "system", "subtype": "api_retry", "error": "rate_limit"},)

    assert classify_claude_stream(events, valid_claude_auth()) is ExitClass.SUBSCRIPTION_QUOTA


@pytest.mark.parametrize(
    "field,value",
    [
        ("loggedIn", False),
        ("authMethod", "api_key"),
        ("apiProvider", "bedrock"),
        ("subscriptionType", "pro"),
    ],
)
def test_claude_auth_requires_all_four_exact_subscription_fields(
    field: str, value: object
) -> None:
    auth = valid_claude_auth()
    auth[field] = value
    events = ({"type": "system", "subtype": "api_retry", "error": "rate_limit"},)

    assert classify_claude_stream(events, auth) is ExitClass.AUTH


def test_claude_billing_error_is_not_mislabeled_as_quota() -> None:
    events = ({"type": "system", "subtype": "api_retry", "error": "billing_error"},)

    assert classify_claude_stream(events, valid_claude_auth()) is ExitClass.BILLING


@pytest.mark.parametrize(
    "event",
    [
        {"type": "system", "subtype": "api_retry", "error": "Rate Limit"},
        {"type": "system", "subtype": "api_retry"},
        {"type": "result", "is_error": True},
    ],
)
def test_malformed_or_unstructured_claude_errors_are_unknown(
    event: dict[str, object]
) -> None:
    assert classify_claude_stream((event,), valid_claude_auth()) is ExitClass.UNKNOWN


def test_claude_stream_without_error_event_is_success() -> None:
    events = ({"type": "assistant", "message": "rate limit in prose"},)

    assert classify_claude_stream(events, valid_claude_auth()) is ExitClass.SUCCESS


def receipt() -> RunReceipt:
    return RunReceipt(
        mode="worker",
        task_id="task-42",
        run_id="run-42",
        primary_runtime=RuntimeKind.CODEX,
        final_runtime=RuntimeKind.CLAUDE,
        fallback_reason="subscription_quota_exhausted",
        attempts=(
            AttemptResult(
                runtime=RuntimeKind.CODEX,
                returncode=75,
                exit_class=ExitClass.SUBSCRIPTION_QUOTA,
                started_at="2026-07-17T01:00:00Z",
                ended_at="2026-07-17T01:00:01Z",
            ),
            AttemptResult(
                runtime=RuntimeKind.CLAUDE,
                returncode=0,
                exit_class=ExitClass.SUCCESS,
                started_at="2026-07-17T01:00:01Z",
                ended_at="2026-07-17T01:00:02Z",
            ),
        ),
    )


def test_write_run_receipt_uses_atomic_replace_and_redacted_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replaced: list[tuple[Path, Path]] = []
    original_replace = os.replace

    def recording_replace(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        assert source_path.exists()
        replaced.append((source_path, destination_path))
        original_replace(source_path, destination_path)

    monkeypatch.setattr("forge.ops.subscription_runtime.os.replace", recording_replace)
    result = write_run_receipt(
        receipt(),
        receipt_root=tmp_path,
        now=datetime(2026, 7, 17, 1, 2, 3, tzinfo=timezone.utc),
    )

    assert result == tmp_path / "run-42-20260717T010203Z.json"
    assert replaced == [(replaced[0][0], result)]
    assert not list(tmp_path.glob("*.tmp"))
    assert json.loads(result.read_text(encoding="utf-8")) == {
        "mode": "worker",
        "task_id": "task-42",
        "run_id": "run-42",
        "primary_runtime": "codex",
        "final_runtime": "claude",
        "fallback_reason": "subscription_quota_exhausted",
        "attempts": [
            {
                "runtime": "codex",
                "returncode": 75,
                "exit_class": "subscription_quota",
                "started_at": "2026-07-17T01:00:00Z",
                "ended_at": "2026-07-17T01:00:01Z",
            },
            {
                "runtime": "claude",
                "returncode": 0,
                "exit_class": "success",
                "started_at": "2026-07-17T01:00:01Z",
                "ended_at": "2026-07-17T01:00:02Z",
            },
        ],
    }
    if os.name != "nt":
        assert stat.S_IMODE(result.stat().st_mode) == 0o600


def test_write_run_receipt_uses_safe_filename_without_task_body(tmp_path: Path) -> None:
    unsafe = RunReceipt(
        mode="worker",
        task_id="task body must not become a filename",
        run_id="run/42",
        primary_runtime=RuntimeKind.CODEX,
        final_runtime=None,
        fallback_reason=None,
        attempts=(),
    )

    result = write_run_receipt(
        unsafe,
        receipt_root=tmp_path,
        now=datetime(2026, 7, 17, tzinfo=timezone.utc),
    )

    assert result.parent == tmp_path
    assert "task body" not in result.name
    assert set(result.name) <= set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.Z")


def test_write_run_receipt_rejects_naive_timestamps(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="UTC-aware"):
        write_run_receipt(receipt(), receipt_root=tmp_path, now=datetime(2026, 7, 17))
