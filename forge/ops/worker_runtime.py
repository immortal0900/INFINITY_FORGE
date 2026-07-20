"""Verified worker runtime adapters and durable Forge run lifecycle."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from .process_identity import (
    ProcessBinding,
    ProcessIdentity,
    ProcessScopeKind,
)
from .task_database import TaskDatabase, TaskDatabaseError
from .task_messages import TaskMessagePacket, TaskMessageStore
from .task_projects import TaskProject, TaskProjectError
from .task_revisions import TaskRevisionError, TaskRevisionService, task_lifecycle_is_active
from .task_settings_v2 import TaskRequestV2, TaskSettingsV2, TaskSettingsV2Error
from .worker_prompt import WorkerPrompt, build_worker_prompt


NATIVE_HERMES_RUNTIME = "native-hermes"
CODEX_APP_SERVER_RUNTIME = "codex-app-server"
CLAUDE_STANDALONE_RUNTIME = "claude-standalone"
WORKER_RESULT_FORMAT = "forge-worker-result/v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_RUNTIME_NAMES = frozenset(
    {NATIVE_HERMES_RUNTIME, CODEX_APP_SERVER_RUNTIME, CLAUDE_STANDALONE_RUNTIME}
)


class WorkerRuntimeError(RuntimeError):
    """Raised when a worker cannot preserve the verified runtime contract."""


class WorkerRuntimeUnavailable(WorkerRuntimeError):
    """Raised when a runtime or explicit fallback lacks all required proof."""


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _require_text(value: object, field_name: str, *, maximum: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or "\x00" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise WorkerRuntimeError(f"{field_name} is invalid")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise WorkerRuntimeError(f"{field_name} is invalid") from None
    return value


def _require_hash(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise WorkerRuntimeError(f"{field_name} must be a lowercase SHA-256")
    return value


def _require_uuid(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise WorkerRuntimeError(f"{field_name} must be a canonical UUID")
    try:
        parsed = str(UUID(value))
    except ValueError:
        raise WorkerRuntimeError(f"{field_name} must be a canonical UUID") from None
    if parsed != value:
        raise WorkerRuntimeError(f"{field_name} must be a canonical UUID")
    return value


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise WorkerRuntimeError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def hermes_database_identity(path: str | Path) -> str:
    """Capture a path-and-file identity that detects Kanban DB replacement."""

    try:
        resolved = Path(path).resolve(strict=True)
        stat = resolved.stat()
    except (OSError, RuntimeError, ValueError) as error:
        raise WorkerRuntimeError("Hermes Kanban database is unavailable") from error
    if not resolved.is_file() or stat.st_ino <= 0 or stat.st_dev < 0:
        raise WorkerRuntimeError("Hermes Kanban database identity is unavailable")
    payload = {
        "device": stat.st_dev,
        "format_version": "forge-hermes-database-identity/v1",
        "inode": stat.st_ino,
        "path": str(resolved),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _claim_lock_hash(claim_lock: str) -> str:
    return hashlib.sha256(claim_lock.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RuntimeAvailability:
    installed: bool
    authenticated: bool
    start_gate_verified: bool
    os_boundary_verified: bool
    identity_readback_verified: bool
    stop_readback_verified: bool
    result_validation_verified: bool
    implemented: bool = True

    def __post_init__(self) -> None:
        if any(type(value) is not bool for value in self.__dict_values()):
            raise WorkerRuntimeError("runtime availability proofs must be boolean")

    def __dict_values(self) -> tuple[bool, ...]:
        return (
            self.installed,
            self.authenticated,
            self.start_gate_verified,
            self.os_boundary_verified,
            self.identity_readback_verified,
            self.stop_readback_verified,
            self.result_validation_verified,
            self.implemented,
        )

    @property
    def verified(self) -> bool:
        return all(self.__dict_values())


@dataclass(frozen=True, slots=True)
class WorkerHandle:
    runtime_name: str
    handle_id: str
    root_pid: int
    gate_closed: bool

    def __post_init__(self) -> None:
        _require_text(self.runtime_name, "runtime_name", maximum=128)
        _require_text(self.handle_id, "handle_id", maximum=512)
        if type(self.root_pid) is not int or self.root_pid <= 0:
            raise WorkerRuntimeError("worker root PID must be positive")
        if self.gate_closed is not True:
            raise WorkerRuntimeError("worker start handle must keep the prompt gate closed")


@dataclass(frozen=True, slots=True)
class WorkerLaunch:
    run_id: str
    request_id: str
    task_settings_hash: str
    project_id: str
    host_id: str
    worker_task_id: str
    worktree_path: str
    runtime_name: str
    prompt: WorkerPrompt
    hermes_board: str
    hermes_database_path: str
    hermes_database_identity: str
    hermes_run_id: int
    hermes_claim_lock: str

    def __post_init__(self) -> None:
        _require_text(self.run_id, "run_id", maximum=512)
        _require_uuid(self.request_id, "request_id")
        _require_hash(self.task_settings_hash, "task_settings_hash")
        _require_hash(self.project_id, "project_id")
        _require_uuid(self.host_id, "host_id")
        _require_text(self.worker_task_id, "worker_task_id", maximum=512)
        _require_text(self.runtime_name, "runtime_name", maximum=128)
        if self.runtime_name not in _RUNTIME_NAMES:
            raise WorkerRuntimeError("runtime_name is unsupported")
        if not isinstance(self.prompt, WorkerPrompt):
            raise WorkerRuntimeError("prompt must be WorkerPrompt")
        _require_text(self.hermes_board, "hermes_board", maximum=128)
        _require_hash(
            self.hermes_database_identity,
            "hermes_database_identity",
        )
        if type(self.hermes_run_id) is not int or self.hermes_run_id <= 0:
            raise WorkerRuntimeError("hermes_run_id must be positive")
        _require_text(self.hermes_claim_lock, "hermes_claim_lock", maximum=1024)
        try:
            database_path = Path(self.hermes_database_path)
            if (
                not database_path.is_absolute()
                or str(database_path.resolve(strict=True)) != self.hermes_database_path
                or not database_path.is_file()
                or hermes_database_identity(database_path)
                != self.hermes_database_identity
            ):
                raise WorkerRuntimeError("Hermes Kanban database binding changed")
        except (OSError, RuntimeError, ValueError):
            raise WorkerRuntimeError(
                "Hermes Kanban database binding changed"
            ) from None
        if self.prompt.task_settings_hash != self.task_settings_hash:
            raise WorkerRuntimeError("worker launch settings do not match prompt")
        try:
            packet = json.loads(self.prompt.packet_bytes)
        except (json.JSONDecodeError, TypeError):
            raise WorkerRuntimeError("worker launch packet is invalid") from None
        if (
            not isinstance(packet, dict)
            or packet.get("request_id") != self.request_id
            or packet.get("task_settings_hash") != self.task_settings_hash
        ):
            raise WorkerRuntimeError("worker launch packet binding changed")
        try:
            path = Path(self.worktree_path)
            if not path.is_absolute() or not path.is_dir():
                raise WorkerRuntimeError("worktree_path must be an existing absolute directory")
        except (OSError, RuntimeError, ValueError):
            raise WorkerRuntimeError(
                "worktree_path must be an existing absolute directory"
            ) from None


@dataclass(frozen=True, slots=True)
class WorkerMessageAcknowledgement:
    message_id: str
    outcome: str
    reason: str

    def __post_init__(self) -> None:
        _require_text(self.message_id, "message_id", maximum=512)
        if self.outcome not in {"applied", "rejected"}:
            raise WorkerRuntimeError("message outcome must be applied or rejected")
        _require_text(self.reason, "message acknowledgement reason", maximum=4096)


@dataclass(frozen=True, slots=True)
class WorkerRuntimeResult:
    packet_hash: str
    task_settings_hash: str
    message_ids: tuple[str, ...]
    acknowledgements: tuple[WorkerMessageAcknowledgement, ...]
    output_bytes: bytes
    hermes_run_id: int
    hermes_claim_lock_hash: str
    format_version: str = WORKER_RESULT_FORMAT

    def __post_init__(self) -> None:
        if self.format_version != WORKER_RESULT_FORMAT:
            raise WorkerRuntimeError("worker result format changed")
        _require_hash(self.packet_hash, "result packet_hash")
        _require_hash(self.task_settings_hash, "result task_settings_hash")
        if (
            not isinstance(self.message_ids, tuple)
            or len(set(self.message_ids)) != len(self.message_ids)
            or any(not isinstance(item, str) or not item for item in self.message_ids)
        ):
            raise WorkerRuntimeError("worker result message IDs are invalid")
        if (
            not isinstance(self.acknowledgements, tuple)
            or any(
                not isinstance(item, WorkerMessageAcknowledgement)
                for item in self.acknowledgements
            )
            or tuple(item.message_id for item in self.acknowledgements)
            != self.message_ids
        ):
            raise WorkerRuntimeError("worker result must acknowledge every packet message")
        if not isinstance(self.output_bytes, bytes):
            raise WorkerRuntimeError("worker result output must be bytes")
        if type(self.hermes_run_id) is not int or self.hermes_run_id <= 0:
            raise WorkerRuntimeError("worker result Hermes run ID is invalid")
        _require_hash(
            self.hermes_claim_lock_hash,
            "worker result Hermes claim proof",
        )

    def to_bytes(self) -> bytes:
        payload = {
            "acknowledgements": [
                {
                    "message_id": item.message_id,
                    "outcome": item.outcome,
                    "reason": item.reason,
                }
                for item in self.acknowledgements
            ],
            "format_version": self.format_version,
            "message_ids": list(self.message_ids),
            "output_base64": base64.b64encode(self.output_bytes).decode("ascii"),
            "packet_hash": self.packet_hash,
            "task_settings_hash": self.task_settings_hash,
            "hermes_run_id": self.hermes_run_id,
            "hermes_claim_lock_hash": self.hermes_claim_lock_hash,
        }
        return _canonical_json(payload).encode("utf-8")

    @property
    def result_hash(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    def require_launch(self, launch: WorkerLaunch) -> None:
        if (
            not isinstance(launch, WorkerLaunch)
            or self.packet_hash != launch.prompt.packet_hash
            or self.task_settings_hash != launch.prompt.task_settings_hash
            or self.message_ids != launch.prompt.message_ids
            or self.hermes_run_id != launch.hermes_run_id
            or self.hermes_claim_lock_hash
            != _claim_lock_hash(launch.hermes_claim_lock)
        ):
            raise WorkerRuntimeError("worker result run or packet acknowledgement changed")


@dataclass(frozen=True, slots=True)
class WorkerWaitStatus:
    exited: bool
    exit_code: int | None

    def __post_init__(self) -> None:
        if type(self.exited) is not bool:
            raise WorkerRuntimeError("worker wait status is invalid")
        if self.exited:
            if type(self.exit_code) is not int:
                raise WorkerRuntimeError("exited worker must have an exit code")
        elif self.exit_code is not None:
            raise WorkerRuntimeError("running worker cannot have an exit code")


@dataclass(frozen=True, slots=True)
class WorkerStopReceipt:
    identity: ProcessIdentity
    stopped: bool
    read_back_verified: bool

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ProcessIdentity):
            raise WorkerRuntimeError("worker stop identity is invalid")
        if type(self.stopped) is not bool or type(self.read_back_verified) is not bool:
            raise WorkerRuntimeError("worker stop readback flags are invalid")


class WorkerRuntimeAdapter(Protocol):
    runtime_name: str

    def availability(self) -> RuntimeAvailability: ...

    def start(self, launch: WorkerLaunch) -> WorkerHandle: ...

    def activate(self, handle: WorkerHandle) -> None: ...

    def stop(self, handle: WorkerHandle) -> WorkerStopReceipt: ...

    def wait(self, handle: WorkerHandle) -> WorkerWaitStatus: ...

    def result(self, handle: WorkerHandle) -> WorkerRuntimeResult: ...

    def process_identity(self, handle: WorkerHandle) -> ProcessIdentity: ...


class WorkerRuntimeDriver(Protocol):
    def availability(self) -> RuntimeAvailability: ...

    def start(self, launch: WorkerLaunch) -> WorkerHandle: ...

    def activate(self, handle: WorkerHandle) -> None: ...

    def stop(self, handle: WorkerHandle) -> WorkerStopReceipt: ...

    def wait(self, handle: WorkerHandle) -> WorkerWaitStatus: ...

    def result(self, handle: WorkerHandle) -> WorkerRuntimeResult: ...

    def process_identity(self, handle: WorkerHandle) -> ProcessIdentity: ...


class _DelegatingAdapter:
    runtime_name: str

    def __init__(self, driver: WorkerRuntimeDriver) -> None:
        self._driver = driver
        self._activated: set[str] = set()

    def availability(self) -> RuntimeAvailability:
        value = self._driver.availability()
        if not isinstance(value, RuntimeAvailability):
            raise WorkerRuntimeUnavailable("runtime availability proof is invalid")
        return value

    def _require_available(self) -> None:
        if not self.availability().verified:
            raise WorkerRuntimeUnavailable(
                f"runtime {self.runtime_name} is not fully verified"
            )

    def _require_handle(self, handle: WorkerHandle) -> None:
        if not isinstance(handle, WorkerHandle) or handle.runtime_name != self.runtime_name:
            raise WorkerRuntimeError("worker handle belongs to another runtime")

    def start(self, launch: WorkerLaunch) -> WorkerHandle:
        self._require_available()
        if not isinstance(launch, WorkerLaunch) or launch.runtime_name != self.runtime_name:
            raise WorkerRuntimeError("worker launch belongs to another runtime")
        handle = self._driver.start(launch)
        self._require_handle(handle)
        if handle.gate_closed is not True:
            raise WorkerRuntimeError("runtime opened the prompt gate during start")
        return handle

    def process_identity(self, handle: WorkerHandle) -> ProcessIdentity:
        self._require_handle(handle)
        identity = self._driver.process_identity(handle)
        if not isinstance(identity, ProcessIdentity) or identity.pid != handle.root_pid:
            raise WorkerRuntimeError("runtime process identity readback changed")
        return identity

    def activate(self, handle: WorkerHandle) -> None:
        self._require_handle(handle)
        if handle.handle_id in self._activated:
            raise WorkerRuntimeError("worker prompt gate was already activated")
        self._driver.activate(handle)
        self._activated.add(handle.handle_id)

    def stop(self, handle: WorkerHandle) -> WorkerStopReceipt:
        self._require_handle(handle)
        receipt = self._driver.stop(handle)
        if not isinstance(receipt, WorkerStopReceipt):
            raise WorkerRuntimeError("runtime stop readback is invalid")
        return receipt

    def wait(self, handle: WorkerHandle) -> WorkerWaitStatus:
        self._require_handle(handle)
        if handle.handle_id not in self._activated:
            raise WorkerRuntimeError("worker wait cannot run before prompt activation")
        value = self._driver.wait(handle)
        if not isinstance(value, WorkerWaitStatus):
            raise WorkerRuntimeError("runtime wait readback is invalid")
        return value

    def result(self, handle: WorkerHandle) -> WorkerRuntimeResult:
        self._require_handle(handle)
        if handle.handle_id not in self._activated:
            raise WorkerRuntimeError("worker result cannot run before prompt activation")
        value = self._driver.result(handle)
        if not isinstance(value, WorkerRuntimeResult):
            raise WorkerRuntimeError("runtime result readback is invalid")
        return value


class NativeHermesAdapter(_DelegatingAdapter):
    runtime_name = NATIVE_HERMES_RUNTIME


class CodexAppServerAdapter(_DelegatingAdapter):
    runtime_name = CODEX_APP_SERVER_RUNTIME


class ClaudeStandaloneAdapter:
    runtime_name = CLAUDE_STANDALONE_RUNTIME

    @staticmethod
    def availability() -> RuntimeAvailability:
        return RuntimeAvailability(False, False, False, False, False, False, False, False)

    @staticmethod
    def _unavailable() -> None:
        raise WorkerRuntimeUnavailable("standalone Claude runtime is not implemented")

    def start(self, launch: WorkerLaunch) -> WorkerHandle:
        del launch
        self._unavailable()

    def activate(self, handle: WorkerHandle) -> None:
        del handle
        self._unavailable()

    def stop(self, handle: WorkerHandle) -> WorkerStopReceipt:
        del handle
        self._unavailable()

    def wait(self, handle: WorkerHandle) -> WorkerWaitStatus:
        del handle
        self._unavailable()

    def result(self, handle: WorkerHandle) -> WorkerRuntimeResult:
        del handle
        self._unavailable()

    def process_identity(self, handle: WorkerHandle) -> ProcessIdentity:
        del handle
        self._unavailable()


class WorkerRuntimeRegistry:
    """Select only fully verified runtimes and exact configured fallbacks."""

    def __init__(
        self,
        adapters: Sequence[WorkerRuntimeAdapter],
        *,
        fallback_order: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        values: dict[str, WorkerRuntimeAdapter] = {}
        for adapter in adapters:
            name = getattr(adapter, "runtime_name", None)
            _require_text(name, "adapter runtime_name", maximum=128)
            if name in values:
                raise WorkerRuntimeError("runtime registry has a duplicate adapter")
            values[name] = adapter
        self._adapters = values
        self._fallback_order = dict(fallback_order or {})
        for primary, order in self._fallback_order.items():
            _require_text(primary, "fallback primary runtime", maximum=128)
            if (
                not isinstance(order, tuple)
                or not order
                or len(set(order)) != len(order)
                or primary in order
            ):
                raise WorkerRuntimeError("runtime fallback order is invalid")
            for name in order:
                _require_text(name, "fallback runtime", maximum=128)

    @staticmethod
    def _verified(adapter: WorkerRuntimeAdapter | None) -> bool:
        if adapter is None:
            return False
        try:
            availability = adapter.availability()
        except Exception:
            return False
        return isinstance(availability, RuntimeAvailability) and availability.verified

    @property
    def available_names(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                name
                for name, adapter in self._adapters.items()
                if self._verified(adapter)
            )
        )

    def select(
        self,
        primary: str,
        *,
        fallbacks: tuple[str, ...] = (),
    ) -> WorkerRuntimeAdapter:
        _require_text(primary, "primary runtime", maximum=128)
        if not isinstance(fallbacks, tuple):
            raise WorkerRuntimeUnavailable("runtime fallback order must be explicit")
        primary_adapter = self._adapters.get(primary)
        if self._verified(primary_adapter):
            return primary_adapter  # type: ignore[return-value]
        if not fallbacks:
            raise WorkerRuntimeUnavailable(
                f"runtime {primary} is not verified and no fallback was requested"
            )
        configured = self._fallback_order.get(primary)
        if configured != fallbacks:
            raise WorkerRuntimeUnavailable("runtime fallback order is not exactly configured")
        for name in fallbacks:
            adapter = self._adapters.get(name)
            if self._verified(adapter):
                return adapter  # type: ignore[return-value]
        raise WorkerRuntimeUnavailable("no runtime in the explicit fallback order is verified")


@dataclass(frozen=True, slots=True)
class ActiveWorkerRun:
    run_id: str
    root_pid: int
    process_identity: ProcessIdentity
    launch: WorkerLaunch
    adapter: WorkerRuntimeAdapter
    handle: WorkerHandle
    packet: TaskMessagePacket


@dataclass(frozen=True, slots=True)
class WorkerRunReceipt:
    run_id: str
    root_pid: int
    process_identity: ProcessIdentity
    result: WorkerRuntimeResult
    result_hash: str


class WorkerRuntimeService:
    """Start behind a verified gate, then accept only exact guarded results."""

    def __init__(
        self,
        database: TaskDatabase,
        registry: WorkerRuntimeRegistry,
        *,
        clock: Callable[[], datetime] | None = None,
        run_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(database, TaskDatabase):
            raise WorkerRuntimeError("database must be TaskDatabase")
        if not isinstance(registry, WorkerRuntimeRegistry):
            raise WorkerRuntimeError("registry must be WorkerRuntimeRegistry")
        self.database = database
        self._registry = registry
        self._clock = clock or (lambda: datetime.now(UTC))
        self._run_id_factory = run_id_factory or (lambda: str(uuid4()))
        self._messages = TaskMessageStore(database, clock=self._clock)
        self._active_guard = TaskRevisionService(database)

    def _now(self, field_name: str) -> datetime:
        return _utc(self._clock(), field_name)

    def _current_packet(
        self,
        request_id: str,
        task_settings_hash: str,
    ) -> TaskMessagePacket:
        try:
            self._active_guard.require_active(request_id, task_settings_hash)
            return self._messages.build_packet(request_id, task_settings_hash)
        except (TaskRevisionError, Exception) as error:
            if isinstance(error, WorkerRuntimeError):
                raise
            raise WorkerRuntimeError(f"Task active/stop/revision guard failed: {error}") from error

    @staticmethod
    def _same_packet(left: TaskMessagePacket, right: TaskMessagePacket) -> bool:
        return (
            left == right
            and left.packet_hash == right.packet_hash
            and left.to_json().encode("utf-8") == right.to_json().encode("utf-8")
        )

    @staticmethod
    def _project_on_connection(
        connection: sqlite3.Connection,
        *,
        request_id: str,
        task_settings_hash: str,
        project_id: str,
        host_id: str,
    ) -> TaskProject:
        row = connection.execute(
            """
            SELECT request.request_json, settings.settings_json,
                   project.project_json, project.state,
                   request.task_owner_host, settings.task_owner_host,
                   project.task_settings_hash
            FROM task_requests AS request
            JOIN task_settings_v2 AS settings
              ON settings.request_id = request.request_id
            JOIN task_projects AS project
              ON project.request_id = request.request_id
             AND project.project_id = ?
            WHERE request.request_id = ?
              AND settings.task_settings_hash = ?
            """,
            (project_id, request_id, task_settings_hash),
        ).fetchone()
        if row is None:
            raise WorkerRuntimeError("exact Project runtime binding is unavailable")
        try:
            request = TaskRequestV2.from_json(row[0])
            settings = TaskSettingsV2.from_json(row[1], request=request)
            payload = json.loads(row[2])
            project = TaskProject.from_mapping(payload)
        except (
            json.JSONDecodeError,
            TaskProjectError,
            TaskSettingsV2Error,
            TypeError,
        ) as error:
            raise WorkerRuntimeError("exact Project runtime binding is invalid") from error
        canonical_project = _canonical_json(
            {
                "base_branch": project.base_branch,
                "base_commit": project.base_commit,
                "host_id": project.host_id,
                "project_id": project.project_id,
                "remote_name": project.remote_name,
                "repository": project.repository,
                "workspace": project.workspace,
            }
        )
        if (
            row[0] != request.to_json()
            or row[1] != settings.to_json()
            or row[2] != canonical_project
            or row[3] not in {"ready", "running", "reviewing"}
            or row[4] != host_id
            or row[5] != host_id
            or row[6] != task_settings_hash
            or request.request_id != request_id
            or settings.task_settings_hash != task_settings_hash
            or project.project_id != project_id
            or project.host_id != host_id
            or project not in request.projects
            or project not in settings.projects
        ):
            raise WorkerRuntimeError("exact Project runtime binding changed")
        return project

    @staticmethod
    def _require_identity(launch: WorkerLaunch, identity: ProcessIdentity) -> None:
        expected = ProcessBinding(
            request_id=launch.request_id,
            task_settings_hash=launch.task_settings_hash,
            project_id=launch.project_id,
            task_id=launch.worker_task_id,
            run_id=launch.run_id,
            host_id=launch.host_id,
        )
        if identity.binding != expected:
            raise WorkerRuntimeError("worker process identity binding changed")
        if identity.scope_kind not in {
            ProcessScopeKind.CGROUP,
            ProcessScopeKind.WINDOWS_JOB,
        }:
            raise WorkerRuntimeError(
                "verified worker identity requires a cgroup or Windows Job boundary"
            )

    def _record_running(
        self,
        launch: WorkerLaunch,
        packet: TaskMessagePacket,
        identity: ProcessIdentity,
        started_at: datetime,
    ) -> None:
        identity_json = identity.to_json()
        started = _format_time(started_at)
        try:
            with self.database.transaction() as connection:
                if not task_lifecycle_is_active(
                    connection,
                    launch.request_id,
                    launch.task_settings_hash,
                ):
                    raise WorkerRuntimeError("Task active/stop/revision guard failed")
                self._project_on_connection(
                    connection,
                    request_id=launch.request_id,
                    task_settings_hash=launch.task_settings_hash,
                    project_id=launch.project_id,
                    host_id=launch.host_id,
                )
                self._messages._validate_packet(connection, packet)
                connection.execute(
                    """
                    INSERT INTO task_runtime_runs (
                        run_id, request_id, task_settings_hash, project_id,
                        host_id, worker_task_id, runtime_name,
                        process_identity_json, message_packet_hash, state,
                        result_hash, started_at, ended_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'starting', NULL, ?, NULL)
                    """,
                    (
                        launch.run_id,
                        launch.request_id,
                        launch.task_settings_hash,
                        launch.project_id,
                        launch.host_id,
                        launch.worker_task_id,
                        launch.runtime_name,
                        identity_json,
                        packet.packet_hash,
                        started,
                    ),
                )
                starting = connection.execute(
                    """
                    SELECT state, process_identity_json, result_hash, ended_at
                    FROM task_runtime_runs WHERE run_id = ?
                    """,
                    (launch.run_id,),
                ).fetchone()
                if starting is None or tuple(starting) != (
                    "starting",
                    identity_json,
                    None,
                    None,
                ):
                    raise WorkerRuntimeError("starting worker run readback changed")
                updated = connection.execute(
                    """
                    UPDATE task_runtime_runs SET state = 'running'
                    WHERE run_id = ? AND state = 'starting'
                      AND process_identity_json = ?
                      AND result_hash IS NULL AND ended_at IS NULL
                    """,
                    (launch.run_id, identity_json),
                )
                if updated.rowcount != 1:
                    raise WorkerRuntimeError("worker run did not enter running exactly")
                running = connection.execute(
                    """
                    SELECT state, process_identity_json, result_hash, ended_at
                    FROM task_runtime_runs WHERE run_id = ?
                    """,
                    (launch.run_id,),
                ).fetchone()
                if running is None or tuple(running) != (
                    "running",
                    identity_json,
                    None,
                    None,
                ):
                    raise WorkerRuntimeError("running worker run readback changed")
        except TaskDatabaseError as error:
            raise WorkerRuntimeError("worker runtime run transaction failed") from error

    def _mark_failed(self, run_id: str) -> None:
        ended_at = _format_time(self._now("worker failure time"))
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE task_runtime_runs
                    SET state = 'failed', result_hash = NULL, ended_at = ?
                    WHERE run_id = ? AND state IN ('starting', 'running', 'stopping')
                      AND result_hash IS NULL AND ended_at IS NULL
                    """,
                    (ended_at, run_id),
                )
        except TaskDatabaseError as error:
            raise WorkerRuntimeError("failed worker run could not be recorded") from error

    def _stop_failed(
        self,
        *,
        adapter: WorkerRuntimeAdapter,
        handle: WorkerHandle,
        identity: ProcessIdentity,
        run_id: str,
        cause: Exception,
    ) -> None:
        stop_error: Exception | None = None
        try:
            receipt = adapter.stop(handle)
            if (
                receipt.identity != identity
                or not receipt.stopped
                or not receipt.read_back_verified
            ):
                raise WorkerRuntimeError("worker stop readback did not match identity")
        except Exception as error:
            stop_error = error
        self._mark_failed(run_id)
        message = str(cause) or type(cause).__name__
        if stop_error is not None:
            message = f"{message}; worker stop readback failed: {stop_error}"
        raise WorkerRuntimeError(message) from cause

    def start(
        self,
        *,
        request_id: str,
        task_settings_hash: str,
        project_id: str,
        host_id: str,
        worker_task_id: str,
        worktree_path: str,
        instructions: str,
        runtime_name: str,
        hermes_board: str,
        hermes_database_path: str,
        hermes_database_identity: str,
        hermes_run_id: int,
        hermes_claim_lock: str,
        fallbacks: tuple[str, ...] = (),
    ) -> ActiveWorkerRun:
        adapter = self._registry.select(runtime_name, fallbacks=fallbacks)
        packet = self._current_packet(request_id, task_settings_hash)
        prompt = build_worker_prompt(packet, instructions=instructions)
        run_id = _require_text(self._run_id_factory(), "run_id", maximum=512)
        launch = WorkerLaunch(
            run_id=run_id,
            request_id=request_id,
            task_settings_hash=task_settings_hash,
            project_id=project_id,
            host_id=host_id,
            worker_task_id=worker_task_id,
            worktree_path=worktree_path,
            runtime_name=adapter.runtime_name,
            prompt=prompt,
            hermes_board=hermes_board,
            hermes_database_path=hermes_database_path,
            hermes_database_identity=hermes_database_identity,
            hermes_run_id=hermes_run_id,
            hermes_claim_lock=hermes_claim_lock,
        )
        with self.database.read() as connection:
            self._project_on_connection(
                connection,
                request_id=request_id,
                task_settings_hash=task_settings_hash,
                project_id=project_id,
                host_id=host_id,
            )
        current = self._current_packet(request_id, task_settings_hash)
        if not self._same_packet(packet, current):
            raise WorkerRuntimeError("worker message packet changed before start")
        started_at = self._now("worker start time")
        handle: WorkerHandle | None = None
        identity: ProcessIdentity | None = None
        try:
            handle = adapter.start(launch)
            identity = adapter.process_identity(handle)
            if identity.pid != handle.root_pid:
                raise WorkerRuntimeError("worker root PID identity changed")
            self._require_identity(launch, identity)
            self._record_running(launch, packet, identity, started_at)
            adapter.activate(handle)
            included_at = self._now("worker packet included time")
            self._messages.record_included(
                packet,
                worker_task_id=worker_task_id,
                run_id=run_id,
                at=included_at,
            )
            return ActiveWorkerRun(
                run_id=run_id,
                root_pid=identity.pid,
                process_identity=identity,
                launch=launch,
                adapter=adapter,
                handle=handle,
                packet=packet,
            )
        except Exception as error:
            if handle is not None and identity is not None:
                self._stop_failed(
                    adapter=adapter,
                    handle=handle,
                    identity=identity,
                    run_id=run_id,
                    cause=error,
                )
            raise WorkerRuntimeError(str(error) or type(error).__name__) from error

    def finish(self, active: ActiveWorkerRun) -> WorkerRunReceipt | None:
        if not isinstance(active, ActiveWorkerRun):
            raise WorkerRuntimeError("active run must be ActiveWorkerRun")
        try:
            wait_status = active.adapter.wait(active.handle)
            if not wait_status.exited:
                return None
            if wait_status.exit_code != 0:
                raise WorkerRuntimeError(
                    f"worker exited unsuccessfully with code {wait_status.exit_code}"
                )
            result = active.adapter.result(active.handle)
            result.require_launch(active.launch)
            current = self._current_packet(
                active.launch.request_id,
                active.launch.task_settings_hash,
            )
            if not self._same_packet(active.packet, current):
                raise WorkerRuntimeError("worker result packet changed before acceptance")
            for acknowledgement in result.acknowledgements:
                self._messages.record_ack(
                    active.packet,
                    message_id=acknowledgement.message_id,
                    outcome=acknowledgement.outcome,
                    worker_task_id=active.launch.worker_task_id,
                    run_id=active.run_id,
                    reason=acknowledgement.reason,
                    at=self._now("worker acknowledgement time"),
                )
            self._messages.require_result_acknowledged(
                active.packet,
                worker_task_id=active.launch.worker_task_id,
                run_id=active.run_id,
            )
            result_hash = result.result_hash
            ended_at = _format_time(self._now("worker result time"))
            with self.database.transaction() as connection:
                if not task_lifecycle_is_active(
                    connection,
                    active.launch.request_id,
                    active.launch.task_settings_hash,
                ):
                    raise WorkerRuntimeError(
                        "Task active/stop/revision guard failed before result commit"
                    )
                self._messages._validate_packet(connection, active.packet)
                updated = connection.execute(
                    """
                    UPDATE task_runtime_runs
                    SET state = 'completed', result_hash = ?, ended_at = ?
                    WHERE run_id = ? AND state = 'running'
                      AND process_identity_json = ?
                      AND message_packet_hash = ?
                      AND result_hash IS NULL AND ended_at IS NULL
                    """,
                    (
                        result_hash,
                        ended_at,
                        active.run_id,
                        active.process_identity.to_json(),
                        active.packet.packet_hash,
                    ),
                )
                if updated.rowcount != 1:
                    raise WorkerRuntimeError("worker result run binding changed")
                row = connection.execute(
                    """
                    SELECT state, result_hash, ended_at
                    FROM task_runtime_runs WHERE run_id = ?
                    """,
                    (active.run_id,),
                ).fetchone()
                if row is None or tuple(row) != ("completed", result_hash, ended_at):
                    raise WorkerRuntimeError("worker result readback changed")
            return WorkerRunReceipt(
                run_id=active.run_id,
                root_pid=active.root_pid,
                process_identity=active.process_identity,
                result=result,
                result_hash=result_hash,
            )
        except Exception as error:
            self._stop_failed(
                adapter=active.adapter,
                handle=active.handle,
                identity=active.process_identity,
                run_id=active.run_id,
                cause=error,
            )

    def run(self, **kwargs: object) -> WorkerRunReceipt:
        active = self.start(**kwargs)  # type: ignore[arg-type]
        result = self.finish(active)
        if result is None:
            raise WorkerRuntimeError("worker is still running; use finish on a later tick")
        return result


@dataclass(frozen=True, slots=True)
class ForgeWorkerTask:
    request_id: str
    task_settings_hash: str
    project_id: str
    host_id: str
    worker_task_id: str
    instructions: str
    runtime_name: str
    hermes_board: str
    hermes_database_path: str
    hermes_database_identity: str
    hermes_run_id: int
    hermes_claim_lock: str
    fallbacks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_uuid(self.request_id, "request_id")
        _require_hash(self.task_settings_hash, "task_settings_hash")
        _require_hash(self.project_id, "project_id")
        _require_uuid(self.host_id, "host_id")
        _require_text(self.worker_task_id, "worker_task_id", maximum=512)
        _require_text(self.instructions, "instructions", maximum=256 * 1024)
        if self.runtime_name not in _RUNTIME_NAMES:
            raise WorkerRuntimeError("runtime_name is unsupported")
        _require_text(self.hermes_board, "hermes_board", maximum=128)
        _require_hash(self.hermes_database_identity, "hermes_database_identity")
        if type(self.hermes_run_id) is not int or self.hermes_run_id <= 0:
            raise WorkerRuntimeError("hermes_run_id must be positive")
        _require_text(self.hermes_claim_lock, "hermes_claim_lock", maximum=1024)
        if not isinstance(self.fallbacks, tuple):
            raise WorkerRuntimeError("fallbacks must be an exact tuple")
        if hermes_database_identity(self.hermes_database_path) != (
            self.hermes_database_identity
        ):
            raise WorkerRuntimeError("Hermes Kanban database binding changed")


class ForgeWorkerSpawner:
    """Immediate-PID dispatcher seam plus required tick-based result reaping."""

    def __init__(self, service: WorkerRuntimeService) -> None:
        if not isinstance(service, WorkerRuntimeService):
            raise WorkerRuntimeError("service must be WorkerRuntimeService")
        self._service = service
        self._active: dict[str, ActiveWorkerRun] = {}
        with service.database.read() as connection:
            rows = connection.execute(
                """
                SELECT run_id FROM task_runtime_runs
                WHERE state IN ('starting', 'running', 'stopping')
                ORDER BY run_id
                """
            ).fetchall()
        self._unreconciled = tuple(str(row[0]) for row in rows)

    @property
    def active_run_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._active))

    @property
    def unreconciled_run_ids(self) -> tuple[str, ...]:
        return self._unreconciled

    def _require_reconciled(self) -> None:
        if self._unreconciled:
            raise WorkerRuntimeUnavailable(
                "existing runtime runs require fail-closed stop reconciliation"
            )

    def __call__(self, task: ForgeWorkerTask, workspace: str | Path, board: str) -> int:
        self._require_reconciled()
        if not isinstance(task, ForgeWorkerTask):
            raise WorkerRuntimeError("task must be ForgeWorkerTask")
        _require_text(board, "board", maximum=128)
        if board != task.hermes_board:
            raise WorkerRuntimeError("dispatcher board changed before worker start")
        active = self._service.start(
            request_id=task.request_id,
            task_settings_hash=task.task_settings_hash,
            project_id=task.project_id,
            host_id=task.host_id,
            worker_task_id=task.worker_task_id,
            worktree_path=str(workspace),
            instructions=task.instructions,
            runtime_name=task.runtime_name,
            hermes_board=task.hermes_board,
            hermes_database_path=task.hermes_database_path,
            hermes_database_identity=task.hermes_database_identity,
            hermes_run_id=task.hermes_run_id,
            hermes_claim_lock=task.hermes_claim_lock,
            fallbacks=task.fallbacks,
        )
        if active.run_id in self._active:
            raise WorkerRuntimeError("worker run ID is already active")
        self._active[active.run_id] = active
        return active.root_pid

    def finish_active(self) -> tuple[WorkerRunReceipt, ...]:
        self._require_reconciled()
        completed: list[WorkerRunReceipt] = []
        for run_id in tuple(sorted(self._active)):
            active = self._active[run_id]
            try:
                receipt = self._service.finish(active)
            except Exception:
                del self._active[run_id]
                raise
            if receipt is not None:
                del self._active[run_id]
                completed.append(receipt)
        return tuple(completed)


__all__ = [
    "CLAUDE_STANDALONE_RUNTIME",
    "CODEX_APP_SERVER_RUNTIME",
    "NATIVE_HERMES_RUNTIME",
    "ActiveWorkerRun",
    "ClaudeStandaloneAdapter",
    "CodexAppServerAdapter",
    "ForgeWorkerSpawner",
    "ForgeWorkerTask",
    "NativeHermesAdapter",
    "RuntimeAvailability",
    "WorkerHandle",
    "WorkerLaunch",
    "WorkerMessageAcknowledgement",
    "WorkerRunReceipt",
    "WorkerRuntimeAdapter",
    "WorkerRuntimeError",
    "WorkerRuntimeRegistry",
    "WorkerRuntimeResult",
    "WorkerRuntimeService",
    "WorkerRuntimeUnavailable",
    "WorkerStopReceipt",
    "WorkerWaitStatus",
    "hermes_database_identity",
]
