"""Run one subscription-only Codex attempt with at most one Claude fallback."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .codex_subscription_probe import CodexAppServerProbe
from .subscription_runtime import (
    AttemptResult,
    ExitClass,
    RunReceipt,
    RuntimeKind,
    classify_claude_stream,
    classify_codex_snapshot,
    scrub_subscription_environment,
    write_run_receipt,
)


EXIT_CONFIGURATION = 78
EXIT_CONTRACT = 70
EXIT_TRANSIENT = 75
_TERMINAL_STATUSES = frozenset({"done", "blocked", "triage", "archived"})
_PROCESS_WAIT_TIMEOUT_SECONDS = 6 * 60 * 60.0
_PROCESS_POLL_SECONDS = 0.1
_PROCESS_TERMINATE_WAIT_SECONDS = 0.5
_THREAD_JOIN_TIMEOUT_SECONDS = 0.5
_MAX_CLASSIFIED_EVENTS = 64
_MAX_DECISIVE_EVENTS = 3
_MAX_CLASSIFICATION_STRING_CHARS = 256
_CLOSING_STREAM_IDS: set[int] = set()
_CLOSING_STREAM_LOCK = threading.Lock()


class ConfigurationError(RuntimeError):
    """Raised when a required managed runtime path is absent or unsafe."""


@dataclass(frozen=True)
class CompletedAttempt:
    returncode: int
    events: tuple[Mapping[str, object], ...] = ()
    started_at: str = ""
    ended_at: str = ""
    failure_class: ExitClass | None = None


@dataclass(frozen=True)
class GitContext:
    status: str = ""
    diff_stat: str = ""
    error: str | None = None


@dataclass(frozen=True)
class WorkerRequest:
    workspace: str
    original_argv: tuple[str, ...]
    env: Mapping[str, str]


@dataclass(frozen=True)
class SkillRequest:
    workspace: str
    prompt: str
    env: Mapping[str, str]


@dataclass(frozen=True)
class SubscriptionRunResult:
    returncode: int
    final_runtime: RuntimeKind | None
    receipt: RunReceipt | None


class Probe(Protocol):
    def probe(
        self, codex_bin: str, env: Mapping[str, str], timeout: float = 10.0
    ) -> object: ...


class Kanban(Protocol):
    def status(self, task_id: str, env: Mapping[str, str]) -> str: ...

    def block(self, task_id: str, reason: str, env: Mapping[str, str]) -> int: ...


ProcessRunner = Callable[
    [Sequence[str], str, Mapping[str, str], str | None, Path | None],
    CompletedAttempt,
]
GitContextReader = Callable[[str, Mapping[str, str]], GitContext]
ClaudeAuthReader = Callable[[str, Mapping[str, str]], Mapping[str, object]]
ReceiptWriter = Callable[[RunReceipt], object]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def default_process_runner(
    argv: Sequence[str],
    cwd: str,
    env: Mapping[str, str],
    stdin_text: str | None,
    stdout_path: Path | None,
) -> CompletedAttempt:
    """Stream one runtime and retain only quota-classification event fields."""

    del stdout_path
    started_at = _utc_now()
    fold = _ClassificationFold()
    process: subprocess.Popen[str] | None = None
    writer: threading.Thread | None = None
    reader: threading.Thread | None = None
    writer_failure = threading.Event()
    reader_failure = threading.Event()
    try:
        # RISK(side-effect): this is the single external runtime process boundary.
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="strict",
            shell=False,
        )
        assert process.stdin is not None
        assert process.stdout is not None

        writer = threading.Thread(
            target=_write_stdin,
            args=(process.stdin, stdin_text or "", writer_failure),
            name="subscription-runtime-stdin",
            daemon=True,
        )
        reader = threading.Thread(
            target=_read_stdout,
            args=(process.stdout, fold, reader_failure),
            name="subscription-runtime-stdout",
            daemon=True,
        )
        writer.start()
        reader.start()

        returncode, failure_class = _wait_for_process(process)
        if failure_class is not None:
            _terminate_process(process)
            _force_close_stream(process.stdin)
            _force_close_stream(process.stdout)

        _join_thread(writer)
        _join_thread(reader)
        if writer.is_alive():
            writer_failure.set()
            _force_close_stream(process.stdin)
        if reader.is_alive():
            reader_failure.set()
            _force_close_stream(process.stdout)
        if writer.is_alive() or reader.is_alive():
            _join_thread(writer)
            _join_thread(reader)

        events = fold.finish()
        if reader_failure.is_set():
            events = ()
        if failure_class is not None:
            returncode = EXIT_CONTRACT
        elif returncode == 0 and (writer_failure.is_set() or reader_failure.is_set()):
            returncode = EXIT_CONTRACT
            failure_class = ExitClass.UNKNOWN
        return CompletedAttempt(
            returncode,
            events,
            started_at,
            _utc_now(),
            failure_class,
        )
    except OSError:
        _terminate_process(process)
        if process is not None:
            _force_close_stream(process.stdin)
            _force_close_stream(process.stdout)
        raise
    finally:
        if process is not None:
            _force_close_stream(process.stdin)
            _force_close_stream(process.stdout)
        _join_thread(writer)
        _join_thread(reader)


class _ClassificationFold:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sequence = 0
        self._ordinary: list[tuple[int, Mapping[str, object]]] = []
        self._decisive: list[tuple[int, Mapping[str, object]]] = []
        self._decisive_keys: set[tuple[str, object]] = set()
        self._frozen = False
        self._finished_events: tuple[Mapping[str, object], ...] = ()

    def add(self, value: object) -> None:
        if not isinstance(value, Mapping):
            return
        with self._lock:
            if self._frozen:
                return
            projected = _classification_projection(value)
            if not projected:
                return
            sequence = self._sequence
            self._sequence += 1
            decisive_key = _decisive_key(projected)
            if decisive_key is not None:
                if (
                    decisive_key not in self._decisive_keys
                    and len(self._decisive) < _MAX_DECISIVE_EVENTS
                ):
                    self._decisive_keys.add(decisive_key)
                    self._decisive.append((sequence, projected))
                return
            ordinary_limit = _MAX_CLASSIFIED_EVENTS - _MAX_DECISIVE_EVENTS
            if len(self._ordinary) < ordinary_limit:
                self._ordinary.append((sequence, projected))

    def finish(self) -> tuple[Mapping[str, object], ...]:
        with self._lock:
            if not self._frozen:
                retained = sorted(
                    (*self._ordinary, *self._decisive), key=lambda item: item[0]
                )
                self._finished_events = tuple(event for _, event in retained)
                self._frozen = True
            return self._finished_events


def _classification_projection(value: Mapping[str, object]) -> dict[str, object]:
    projected: dict[str, object] = {}
    for key in ("type", "subtype", "error"):
        candidate = value.get(key)
        if isinstance(candidate, str):
            if len(candidate) > _MAX_CLASSIFICATION_STRING_CHARS:
                return {"type": "result", "is_error": True}
            projected[key] = candidate
    if isinstance(value.get("is_error"), bool):
        projected["is_error"] = value["is_error"]
    return projected


def _decisive_key(event: Mapping[str, object]) -> tuple[str, object] | None:
    if event.get("type") == "system" and event.get("subtype") == "api_retry":
        error = event.get("error")
        if error in {"rate_limit", "billing_error"}:
            return ("api_retry", error)
    if event.get("type") == "result" and event.get("is_error") is True:
        return ("result", True)
    return None


def _write_stdin(
    stream: object, text: str, writer_failure: threading.Event
) -> None:
    try:
        stream.write(text)  # type: ignore[attr-defined]
        stream.flush()  # type: ignore[attr-defined]
    except (BrokenPipeError, OSError, UnicodeError, ValueError):
        writer_failure.set()
    finally:
        _close_stream(stream)


def _read_stdout(
    stream: object,
    fold: _ClassificationFold,
    reader_failure: threading.Event,
) -> None:
    try:
        for line in stream:  # type: ignore[union-attr]
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            fold.add(value)
    except (OSError, UnicodeError, ValueError):
        reader_failure.set()
    finally:
        _close_stream(stream)


def _wait_for_process(
    process: subprocess.Popen[str],
) -> tuple[int, ExitClass | None]:
    deadline = time.monotonic() + _PROCESS_WAIT_TIMEOUT_SECONDS
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return EXIT_CONTRACT, ExitClass.TIMEOUT
            try:
                return process.wait(timeout=min(_PROCESS_POLL_SECONDS, remaining)), None
            except subprocess.TimeoutExpired:
                continue
    except KeyboardInterrupt:
        return EXIT_CONTRACT, ExitClass.CANCELLED


def _join_thread(thread: threading.Thread | None) -> None:
    if thread is not None and thread.is_alive():
        thread.join(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)


def _close_stream(stream: object | None) -> None:
    if stream is None:
        return
    try:
        stream.close()  # type: ignore[attr-defined]
    except (OSError, ValueError):
        return


def _force_close_stream(stream: object | None) -> None:
    if stream is None or getattr(stream, "closed", False):
        return
    stream_id = id(stream)
    with _CLOSING_STREAM_LOCK:
        if stream_id in _CLOSING_STREAM_IDS:
            return
        _CLOSING_STREAM_IDS.add(stream_id)

    def close() -> None:
        try:
            _close_stream(stream)
        finally:
            with _CLOSING_STREAM_LOCK:
                _CLOSING_STREAM_IDS.discard(stream_id)

    threading.Thread(
        target=close,
        name="subscription-runtime-pipe-close",
        daemon=True,
    ).start()


def _terminate_process(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        try:
            process.wait(timeout=_PROCESS_TERMINATE_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    except OSError:
        return


def default_claude_auth_status(
    claude_bin: str, env: Mapping[str, str]
) -> Mapping[str, object]:
    # RISK(security): query subscription auth without exposing stdout or credentials.
    try:
        completed = subprocess.run(
            [claude_bin, "auth", "status", "--json"],
            env=dict(env),
            text=True,
            encoding="utf-8",
            errors="strict",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            check=False,
        )
    except (OSError, UnicodeError):
        return {}
    if completed.returncode != 0:
        return {}
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, Mapping) else {}


def default_git_context(workspace: str, env: Mapping[str, str]) -> GitContext:
    outputs: list[str] = []
    errors: list[str] = []
    for label, argv in (
        ("git status --short", ["git", "status", "--short"]),
        ("git diff --stat", ["git", "diff", "--stat"]),
    ):
        # RISK(side-effect): no shell is used; Git commands are read-only context probes.
        try:
            completed = subprocess.run(
                argv,
                cwd=workspace,
                env=dict(env),
                text=True,
                encoding="utf-8",
                errors="strict",
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                shell=False,
                check=False,
            )
        except (OSError, UnicodeError):
            outputs.append("")
            errors.append(f"{label} could not start")
            continue
        outputs.append(completed.stdout.rstrip())
        if completed.returncode != 0:
            errors.append(f"{label} failed with code {completed.returncode}")
    return GitContext(outputs[0], outputs[1], "; ".join(errors) or None)


class SubprocessKanban:
    """Read and transition Kanban state through an explicitly configured CLI."""

    def status(self, task_id: str, env: Mapping[str, str]) -> str:
        hermes_bin = self._hermes_bin(env)
        child_env = _kanban_child_env(env)
        # RISK(side-effect): status is a read-only subprocess with fixed argument order.
        completed = subprocess.run(
            [hermes_bin, "kanban", "show", task_id, "--json"],
            env=child_env,
            text=True,
            encoding="utf-8",
            errors="strict",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("Hermes Kanban status command failed")
        try:
            payload = json.loads(completed.stdout)
            task = payload["task"]
            status = task["status"]
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise RuntimeError("Hermes Kanban status response is invalid") from error
        if not isinstance(status, str):
            raise RuntimeError("Hermes Kanban status response is invalid")
        return status

    def block(self, task_id: str, reason: str, env: Mapping[str, str]) -> int:
        hermes_bin = self._hermes_bin(env)
        child_env = _kanban_child_env(env)
        # RISK(side-effect): this command deliberately writes the task terminal state.
        completed = subprocess.run(
            [
                hermes_bin,
                "kanban",
                "block",
                "--kind",
                "capability",
                task_id,
                reason,
            ],
            env=child_env,
            text=True,
            encoding="utf-8",
            errors="strict",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            check=False,
        )
        return completed.returncode

    @staticmethod
    def _hermes_bin(env: Mapping[str, str]) -> str:
        return str(_configured_hermes_binary(env))


def _kanban_child_env(env: Mapping[str, str]) -> dict[str, str]:
    child_env = dict(env)
    child_env["PYTHONUTF8"] = "1"
    return child_env


def _configured_hermes_binary(env: Mapping[str, str]) -> Path:
    value = (
        env.get("INFINITY_FORGE_HERMES_BIN") or env.get("HERMES_BIN") or ""
    ).strip()
    supplied = Path(value)
    if not value or not supplied.is_absolute():
        raise ConfigurationError("Hermes executable configuration is invalid")
    try:
        # RISK(security): only a strict-resolved native command may cross Popen.
        resolved = supplied.resolve(strict=True)
    except OSError as error:
        raise ConfigurationError("Hermes executable configuration is invalid") from error
    if not resolved.is_file():
        raise ConfigurationError("Hermes executable configuration is invalid")
    if os.name == "nt":
        if resolved.suffix.lower() != ".exe":
            raise ConfigurationError("Hermes executable configuration is invalid")
    elif not os.access(resolved, os.X_OK):
        raise ConfigurationError("Hermes executable configuration is invalid")
    return resolved


def build_claude_continuation_prompt(
    *,
    original_prompt: str | None,
    task_id: str | None,
    run_id: str | None,
    workspace: str,
    branch: str | None,
    git_context: GitContext,
) -> str:
    """Build the bounded continuation instruction without copying process env."""

    lines = [
        "Codex 구독 한도 소진이 구조화된 상태로 확인되어 Claude로 1회 전환합니다.",
        f"workspace: {workspace}",
    ]
    if task_id is not None:
        lines.extend(
            [
                f"원본 Task ID: {task_id}",
                f"동일 run ID: {run_id or '(missing)'}",
                f"동일 branch: {branch or '(missing)'}",
                "Kanban에서 원본 Task의 지시와 현재 상태를 읽고 그 Task만 계속 수행하세요.",
            ]
        )
    elif original_prompt is not None:
        lines.extend(["원본 지시:", original_prompt])
    lines.extend(
        [
            "현재 git status --short:",
            git_context.status or "(empty)",
            "현재 git diff --stat:",
            git_context.diff_stat or "(empty)",
        ]
    )
    if git_context.error:
        lines.append(f"Git context error: {git_context.error}")
    lines.extend(
        [
            "이미 존재하는 부분 변경을 보존하고, 덮어쓰거나 처음부터 다시 만들지 마세요.",
            "남은 작업을 완료하고 검증하세요.",
        ]
    )
    if task_id is not None:
        lines.append(
            "마지막에 같은 Task에 kanban_complete 또는 kanban_block 중 정확히 한 번만 호출하세요."
        )
    return "\n".join(lines) + "\n"


class SubscriptionRunner:
    def __init__(
        self,
        *,
        process_runner: ProcessRunner = default_process_runner,
        probe: Probe | None = None,
        kanban: Kanban | None = None,
        git_context: GitContextReader = default_git_context,
        claude_auth_status: ClaudeAuthReader = default_claude_auth_status,
        receipt_writer: ReceiptWriter = write_run_receipt,
        codex_bin: str = "codex",
        claude_bin: str = "claude",
        claude_mcp_config: str | None = None,
    ) -> None:
        self._process_runner = process_runner
        self._probe = probe or CodexAppServerProbe()
        self._kanban = kanban or SubprocessKanban()
        self._git_context = git_context
        self._claude_auth_status = claude_auth_status
        self._receipt_writer = receipt_writer
        self._codex_bin = codex_bin
        self._claude_bin = claude_bin
        self._claude_mcp_config = claude_mcp_config

    def run_worker(self, request: WorkerRequest) -> SubscriptionRunResult:
        child_env = scrub_subscription_environment(request.env)
        context = self._worker_context(request)
        if context is None:
            return self._finish("worker", None, None, RuntimeKind.CODEX, None, None, (), EXIT_CONFIGURATION)
        task_id, run_id, branch = context
        try:
            worker_argv = self._validated_worker_argv(request, child_env)
        except ConfigurationError:
            return self._finish(
                "worker",
                task_id,
                run_id,
                RuntimeKind.CODEX,
                None,
                None,
                (),
                EXIT_CONFIGURATION,
            )

        preflight_quota = self._codex_quota(child_env)
        if preflight_quota:
            return self._run_worker_claude(
                request, child_env, task_id, run_id, branch, ()
            )

        completed = self._invoke_process(
            worker_argv, request.workspace, child_env, None, None
        )
        codex_class = ExitClass.SUCCESS if completed.returncode == 0 else ExitClass.UNKNOWN
        codex_attempt = self._attempt(RuntimeKind.CODEX, completed, codex_class)
        attempts = (codex_attempt,)
        if completed.returncode == 0:
            return self._finish_worker_terminal(
                request, child_env, attempts, RuntimeKind.CODEX
            )
        if completed.returncode != EXIT_TRANSIENT:
            return self._finish(
                "worker",
                task_id,
                run_id,
                RuntimeKind.CODEX,
                RuntimeKind.CODEX,
                None,
                attempts,
                completed.returncode,
            )
        if not self._codex_quota(child_env):
            return self._finish(
                "worker",
                task_id,
                run_id,
                RuntimeKind.CODEX,
                RuntimeKind.CODEX,
                None,
                attempts,
                EXIT_TRANSIENT,
            )
        attempts = (replace(codex_attempt, exit_class=ExitClass.SUBSCRIPTION_QUOTA),)
        return self._run_worker_claude(
            request, child_env, task_id, run_id, branch, attempts
        )

    def run_codex_skill(self, request: SkillRequest) -> SubscriptionRunResult:
        child_env = scrub_subscription_environment(request.env)
        completed = self._invoke_process(
            self._codex_skill_argv(request.workspace),
            request.workspace,
            child_env,
            request.prompt,
            None,
        )
        exit_class = ExitClass.SUCCESS if completed.returncode == 0 else ExitClass.UNKNOWN
        codex_attempt = self._attempt(RuntimeKind.CODEX, completed, exit_class)
        if completed.returncode == 0:
            return self._finish(
                "codex-skill", None, None, RuntimeKind.CODEX, RuntimeKind.CODEX, None, (codex_attempt,), 0
            )
        if completed.failure_class is not None or not self._codex_quota(child_env):
            return self._finish(
                "codex-skill",
                None,
                None,
                RuntimeKind.CODEX,
                RuntimeKind.CODEX,
                None,
                (codex_attempt,),
                completed.returncode,
            )
        codex_attempt = replace(codex_attempt, exit_class=ExitClass.SUBSCRIPTION_QUOTA)
        prompt = build_claude_continuation_prompt(
            original_prompt=request.prompt,
            task_id=None,
            run_id=None,
            workspace=request.workspace,
            branch=None,
            git_context=self._git_context(request.workspace, child_env),
        )
        return self._run_skill_claude(
            "codex-skill", request, child_env, prompt, (codex_attempt,), RuntimeKind.CODEX
        )

    def run_claude_skill(self, request: SkillRequest) -> SubscriptionRunResult:
        child_env = scrub_subscription_environment(request.env)
        return self._run_skill_claude(
            "claude-skill", request, child_env, request.prompt, (), RuntimeKind.CLAUDE
        )

    def _run_worker_claude(
        self,
        request: WorkerRequest,
        child_env: Mapping[str, str],
        task_id: str,
        run_id: str,
        branch: str,
        attempts: tuple[AttemptResult, ...],
    ) -> SubscriptionRunResult:
        auth = self._validated_claude_auth(child_env)
        if auth is None:
            return self._finish(
                "worker",
                task_id,
                run_id,
                RuntimeKind.CODEX,
                attempts[-1].runtime if attempts else None,
                "subscription_quota_exhausted",
                attempts,
                EXIT_CONFIGURATION,
            )
        prompt = build_claude_continuation_prompt(
            original_prompt=None,
            task_id=task_id,
            run_id=run_id,
            workspace=request.workspace,
            branch=branch,
            git_context=self._git_context(request.workspace, child_env),
        )
        # RISK(security): bypassPermissions is fixed by the approved worker contract.
        completed = self._invoke_process(
            self._claude_argv(), request.workspace, child_env, prompt, None
        )
        exit_class = self._claude_exit_class(completed, auth)
        claude_attempt = self._attempt(RuntimeKind.CLAUDE, completed, exit_class)
        all_attempts = (*attempts, claude_attempt)
        if exit_class is ExitClass.SUBSCRIPTION_QUOTA:
            returncode = self._block(
                task_id, "Codex와 Claude 구독 한도 소진", child_env, quota=True
            )
            return self._finish(
                "worker",
                task_id,
                run_id,
                RuntimeKind.CODEX,
                RuntimeKind.CLAUDE,
                "subscription_quota_exhausted",
                all_attempts,
                returncode,
            )
        if completed.returncode != 0:
            return self._finish(
                "worker",
                task_id,
                run_id,
                RuntimeKind.CODEX,
                RuntimeKind.CLAUDE,
                "subscription_quota_exhausted",
                all_attempts,
                completed.returncode,
            )
        return self._finish_worker_terminal(
            request,
            child_env,
            all_attempts,
            RuntimeKind.CLAUDE,
            fallback_reason="subscription_quota_exhausted",
        )

    def _run_skill_claude(
        self,
        mode: str,
        request: SkillRequest,
        child_env: Mapping[str, str],
        prompt: str,
        attempts: tuple[AttemptResult, ...],
        primary_runtime: RuntimeKind,
    ) -> SubscriptionRunResult:
        auth = self._validated_claude_auth(child_env)
        if auth is None:
            return self._finish(
                mode,
                None,
                None,
                primary_runtime,
                attempts[-1].runtime if attempts else None,
                "subscription_quota_exhausted" if attempts else None,
                attempts,
                EXIT_CONFIGURATION,
            )
        # RISK(security): bypassPermissions is fixed by the approved skill contract.
        completed = self._invoke_process(
            self._claude_argv(), request.workspace, child_env, prompt, None
        )
        exit_class = self._claude_exit_class(completed, auth)
        claude_attempt = self._attempt(RuntimeKind.CLAUDE, completed, exit_class)
        returncode = EXIT_TRANSIENT if exit_class is ExitClass.SUBSCRIPTION_QUOTA else completed.returncode
        return self._finish(
            mode,
            None,
            None,
            primary_runtime,
            RuntimeKind.CLAUDE,
            "subscription_quota_exhausted" if attempts else None,
            (*attempts, claude_attempt),
            returncode,
        )

    def _finish_worker_terminal(
        self,
        request: WorkerRequest,
        child_env: Mapping[str, str],
        attempts: tuple[AttemptResult, ...],
        final_runtime: RuntimeKind,
        fallback_reason: str | None = None,
    ) -> SubscriptionRunResult:
        task_id = request.env["HERMES_KANBAN_TASK"]
        run_id = request.env["HERMES_KANBAN_RUN_ID"]
        try:
            status = self._kanban.status(task_id, child_env)
        except Exception:
            self._block(
                task_id,
                "runtime exited successfully but Task terminal status could not be verified",
                child_env,
                quota=False,
            )
            returncode = EXIT_CONTRACT
        else:
            if status in _TERMINAL_STATUSES:
                returncode = 0
            else:
                self._block(
                    task_id,
                    "runtime exited successfully but Task did not reach a terminal state",
                    child_env,
                    quota=False,
                )
                returncode = EXIT_CONTRACT
        return self._finish(
            "worker",
            task_id,
            run_id,
            RuntimeKind.CODEX,
            final_runtime,
            fallback_reason,
            attempts,
            returncode,
        )

    def _block(
        self,
        task_id: str,
        reason: str,
        env: Mapping[str, str],
        *,
        quota: bool,
    ) -> int:
        try:
            code = self._kanban.block(task_id, reason, env)
        except ConfigurationError:
            return EXIT_CONFIGURATION
        except Exception:
            return EXIT_CONTRACT
        if quota and code == 0:
            return 0
        return EXIT_CONTRACT

    def _codex_quota(self, env: Mapping[str, str]) -> bool:
        try:
            snapshot = self._probe.probe(self._codex_bin, env, 10.0)
            return classify_codex_snapshot(snapshot) is ExitClass.SUBSCRIPTION_QUOTA  # type: ignore[arg-type]
        except Exception:
            return False

    def _validated_claude_auth(
        self, env: Mapping[str, str]
    ) -> Mapping[str, object] | None:
        if not self._claude_mcp_config:
            return None
        try:
            auth = self._claude_auth_status(self._claude_bin, env)
        except Exception:
            return None
        valid = (
            auth.get("loggedIn") is True
            and auth.get("authMethod") == "claude.ai"
            and auth.get("apiProvider") == "firstParty"
            and auth.get("subscriptionType") == "max"
        )
        return auth if valid else None

    def _claude_exit_class(
        self,
        completed: CompletedAttempt,
        auth: Mapping[str, object],
    ) -> ExitClass:
        if completed.failure_class is not None:
            return completed.failure_class
        classified = classify_claude_stream(completed.events, auth)
        if classified is ExitClass.SUCCESS and completed.returncode != 0:
            return ExitClass.UNKNOWN
        return classified

    def _attempt(
        self, runtime: RuntimeKind, completed: CompletedAttempt, exit_class: ExitClass
    ) -> AttemptResult:
        now = _utc_now()
        return AttemptResult(
            runtime,
            completed.returncode,
            completed.failure_class or exit_class,
            completed.started_at or now,
            completed.ended_at or now,
        )

    def _finish(
        self,
        mode: str,
        task_id: str | None,
        run_id: str | None,
        primary_runtime: RuntimeKind,
        final_runtime: RuntimeKind | None,
        fallback_reason: str | None,
        attempts: tuple[AttemptResult, ...],
        returncode: int,
    ) -> SubscriptionRunResult:
        receipt = RunReceipt(
            mode,
            task_id,
            run_id,
            primary_runtime,
            final_runtime,
            fallback_reason,
            attempts,
        )
        self._receipt_writer(receipt)
        return SubscriptionRunResult(returncode, final_runtime, receipt)

    def _worker_context(
        self, request: WorkerRequest
    ) -> tuple[str, str, str] | None:
        values = tuple(
            request.env.get(key, "").strip()
            for key in (
                "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_RUN_ID",
                "HERMES_KANBAN_WORKSPACE",
                "HERMES_KANBAN_BRANCH",
            )
        )
        task_id, run_id, workspace, branch = values
        if not all(values) or workspace != request.workspace:
            return None
        return task_id, run_id, branch

    def _validated_worker_argv(
        self, request: WorkerRequest, env: Mapping[str, str]
    ) -> tuple[str, ...]:
        configured = _configured_hermes_binary(env)
        if not request.original_argv:
            raise ConfigurationError("worker executable configuration is invalid")
        original = Path(request.original_argv[0])
        if not original.is_absolute():
            raise ConfigurationError("worker executable configuration is invalid")
        try:
            resolved = original.resolve(strict=True)
        except OSError as error:
            raise ConfigurationError(
                "worker executable configuration is invalid"
            ) from error
        if resolved != configured or not resolved.is_file():
            raise ConfigurationError("worker executable configuration is invalid")
        return (str(configured), *request.original_argv[1:])

    def _invoke_process(
        self,
        argv: Sequence[str],
        cwd: str,
        env: Mapping[str, str],
        stdin_text: str | None,
        stdout_path: Path | None,
    ) -> CompletedAttempt:
        started_at = _utc_now()
        try:
            return self._process_runner(argv, cwd, env, stdin_text, stdout_path)
        except subprocess.TimeoutExpired:
            return CompletedAttempt(
                EXIT_CONTRACT, (), started_at, _utc_now(), ExitClass.TIMEOUT
            )
        except KeyboardInterrupt:
            return CompletedAttempt(
                EXIT_CONTRACT, (), started_at, _utc_now(), ExitClass.CANCELLED
            )
        except OSError:
            return CompletedAttempt(
                EXIT_CONTRACT, (), started_at, _utc_now(), ExitClass.UNKNOWN
            )

    def _codex_skill_argv(self, workspace: str) -> list[str]:
        return [
            self._codex_bin,
            "exec",
            "--json",
            "--sandbox",
            "workspace-write",
            "--ephemeral",
            "-C",
            workspace,
            "-",
        ]

    def _claude_argv(self) -> list[str]:
        assert self._claude_mcp_config is not None
        return [
            self._claude_bin,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--max-turns",
            "20",
            "--permission-mode",
            "bypassPermissions",
            "--mcp-config",
            self._claude_mcp_config,
            "--strict-mcp-config",
        ]


def _build_runner(env: Mapping[str, str]) -> SubscriptionRunner:
    return SubscriptionRunner(
        codex_bin=env.get("INFINITY_FORGE_CODEX_BIN", "codex"),
        claude_bin=env.get("INFINITY_FORGE_CLAUDE_BIN", "claude"),
        claude_mcp_config=env.get("INFINITY_FORGE_CLAUDE_MCP_CONFIG"),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="subscription-runner.py")
    modes = parser.add_subparsers(dest="mode", required=True)
    worker = modes.add_parser("worker")
    worker.add_argument("--workspace", required=True)
    worker.add_argument("original_argv", nargs=argparse.REMAINDER)
    for mode in ("codex-skill", "claude-skill"):
        skill = modes.add_parser(mode)
        skill.add_argument("--workspace", required=True)
        skill.add_argument("--prompt-file", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(list(argv) if argv is not None else None)
    except SystemExit as error:
        return 0 if error.code == 0 else EXIT_CONFIGURATION
    env = dict(os.environ)
    runner = _build_runner(env)
    if args.mode == "worker":
        original = list(args.original_argv)
        if original[:1] == ["--"]:
            original = original[1:]
        if not original or env.get("HERMES_KANBAN_WORKSPACE") != args.workspace:
            return EXIT_CONFIGURATION
        request = WorkerRequest(args.workspace, tuple(original), env)
        return runner.run_worker(request).returncode
    try:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return EXIT_CONFIGURATION
    request = SkillRequest(args.workspace, prompt, env)
    if args.mode == "codex-skill":
        return runner.run_codex_skill(request).returncode
    return runner.run_claude_skill(request).returncode


__all__ = [
    "CompletedAttempt",
    "ConfigurationError",
    "GitContext",
    "SkillRequest",
    "SubscriptionRunResult",
    "SubscriptionRunner",
    "SubprocessKanban",
    "WorkerRequest",
    "build_claude_continuation_prompt",
    "main",
]
