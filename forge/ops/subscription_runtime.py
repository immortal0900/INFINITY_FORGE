"""Pure policy and receipt handling for subscription-only coding runtimes."""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path


class RuntimeKind(str, Enum):
    CODEX = "codex"
    CLAUDE = "claude"


class ExitClass(str, Enum):
    SUCCESS = "success"
    SUBSCRIPTION_QUOTA = "subscription_quota"
    BILLING = "billing"
    AUTH = "auth"
    NETWORK = "network"
    TOOL = "tool"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CodexSubscriptionSnapshot:
    account_type: str | None
    plan_type: str | None
    rate_limit_reached_type: str | None
    spend_control_reached: bool


@dataclass(frozen=True)
class AttemptResult:
    runtime: RuntimeKind
    returncode: int
    exit_class: ExitClass
    started_at: str
    ended_at: str


@dataclass(frozen=True)
class RunReceipt:
    mode: str
    task_id: str | None
    run_id: str | None
    primary_runtime: RuntimeKind
    final_runtime: RuntimeKind | None
    fallback_reason: str | None
    attempts: tuple[AttemptResult, ...]


_PAYG_ENVIRONMENT_KEYS = frozenset(
    {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    }
)
_SAFE_FILENAME_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")


def scrub_subscription_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Return a child-only environment without API billing credentials or switches."""

    # RISK(security): only the copied child environment is scrubbed; never mutate user state.
    return {
        key: value
        for key, value in environment.items()
        if key not in _PAYG_ENVIRONMENT_KEYS
    }


def classify_codex_snapshot(snapshot: CodexSubscriptionSnapshot) -> ExitClass:
    """Classify only backend-confirmed ChatGPT subscription limits as quota."""

    if snapshot.account_type != "chatgpt":
        return ExitClass.AUTH
    if snapshot.rate_limit_reached_type or snapshot.spend_control_reached:
        return ExitClass.SUBSCRIPTION_QUOTA
    return ExitClass.SUCCESS


def is_claude_subscription_auth(auth_status: Mapping[str, object]) -> bool:
    """Accept only Claude.ai first-party login without unstable plan metadata."""

    return (
        auth_status.get("loggedIn") is True
        and auth_status.get("authMethod") == "claude.ai"
        and auth_status.get("apiProvider") == "firstParty"
    )


def classify_claude_stream(
    events: Iterable[Mapping[str, object]], auth_status: Mapping[str, object]
) -> ExitClass:
    """Classify the canonical Claude stream error events after subscription auth checks."""

    if not is_claude_subscription_auth(auth_status):
        return ExitClass.AUTH

    for event in events:
        if not isinstance(event, Mapping):
            return ExitClass.UNKNOWN
        if event.get("type") == "system" and event.get("subtype") == "api_retry":
            error = event.get("error")
            if error == "rate_limit":
                return ExitClass.SUBSCRIPTION_QUOTA
            if error == "billing_error":
                return ExitClass.BILLING
            return ExitClass.UNKNOWN
        if event.get("type") == "result" and event.get("is_error") is True:
            return ExitClass.UNKNOWN
    return ExitClass.SUCCESS


def write_run_receipt(
    receipt: RunReceipt,
    *,
    receipt_root: Path | None = None,
    now: datetime | None = None,
) -> Path:
    """Atomically persist the deliberately redacted run receipt and return its path."""

    timestamp = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
        raise ValueError("now must be UTC-aware")

    root = receipt_root or (
        Path.home() / ".hermes" / "infinity-forge" / "runtime-attempts"
    )
    root.mkdir(parents=True, exist_ok=True)
    filename = _safe_run_id(receipt.run_id) + "-" + timestamp.strftime("%Y%m%dT%H%M%SZ")
    destination = root / f"{filename}.json"
    temporary = root / f".{filename}.{uuid.uuid4().hex}.tmp"
    encoded = (json.dumps(_receipt_payload(receipt), ensure_ascii=False) + "\n").encode(
        "utf-8"
    )

    # RISK(security): create a private new temporary file before persisting run metadata.
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as receipt_file:
            receipt_file.write(encoded)
            receipt_file.flush()
            os.fsync(receipt_file.fileno())
        # RISK(data-loss): same-directory replacement keeps readers from observing partial JSON.
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def _safe_run_id(run_id: str | None) -> str:
    component = _SAFE_FILENAME_COMPONENT.sub("-", run_id or "skill").strip(".-")
    return (component or "skill")[:80]


def _receipt_payload(receipt: RunReceipt) -> dict[str, object]:
    return {
        "mode": receipt.mode,
        "task_id": receipt.task_id,
        "run_id": receipt.run_id,
        "primary_runtime": receipt.primary_runtime.value,
        "final_runtime": (
            receipt.final_runtime.value if receipt.final_runtime is not None else None
        ),
        "fallback_reason": receipt.fallback_reason,
        "attempts": [
            {
                "runtime": attempt.runtime.value,
                "returncode": attempt.returncode,
                "exit_class": attempt.exit_class.value,
                "started_at": attempt.started_at,
                "ended_at": attempt.ended_at,
            }
            for attempt in receipt.attempts
        ],
    }
