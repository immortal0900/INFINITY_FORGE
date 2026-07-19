"""Exact, task-bound process tree identity and termination adapters."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import math
import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol
from uuid import UUID


_FORMAT_VERSION = "forge-process-identity/v1"
_BINDING_TOKEN_FORMAT = "forge-process-binding-token/v1"
_SHA256_LENGTH = 64
_TASK_CGROUP_SEGMENT = "infinity-forge"


class ProcessIdentityError(RuntimeError):
    """Raised when a process tree cannot be identified or stopped safely."""


class ProcessIdentityMismatch(ProcessIdentityError):
    """Raised before signaling when a durable identity no longer matches."""


class ProcessScopeKind(str, Enum):
    """Supported OS-owned worker tree boundaries."""

    PROCESS_GROUP = "process_group"
    CGROUP = "cgroup"
    WINDOWS_JOB = "windows_job"


@dataclass(frozen=True, slots=True)
class ProcessBinding:
    """Task fields that must all match before process control is allowed."""

    request_id: str
    task_settings_hash: str
    project_id: str
    task_id: str
    run_id: str
    host_id: str

    def __post_init__(self) -> None:
        _canonical_uuid(self.request_id, "request_id")
        _lower_sha256(self.task_settings_hash, "task_settings_hash")
        _lower_sha256(self.project_id, "project_id")
        _bounded_text(self.task_id, "task_id", maximum=512)
        _bounded_text(self.run_id, "run_id", maximum=512)
        _canonical_uuid(self.host_id, "host_id")


def process_binding_token(binding: ProcessBinding) -> str:
    """Return the versioned canonical hash that binds one exact worker run."""

    if not isinstance(binding, ProcessBinding):
        raise TypeError("binding must be ProcessBinding")
    canonical = json.dumps(
        {
            "format_version": _BINDING_TOKEN_FORMAT,
            "host_id": binding.host_id,
            "project_id": binding.project_id,
            "request_id": binding.request_id,
            "run_id": binding.run_id,
            "task_id": binding.task_id,
            "task_settings_hash": binding.task_settings_hash,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def task_cgroup_scope_id(
    binding: ProcessBinding,
    *,
    delegated_parent_scope: str,
) -> str:
    """Build the exact Task child beneath the dispatcher's delegated cgroup."""

    parent = _canonical_cgroup_scope(
        delegated_parent_scope,
        "delegated_parent_scope",
    )
    scope_id = f"{parent}/{_TASK_CGROUP_SEGMENT}/{process_binding_token(binding)}"
    return _bounded_text(scope_id, "Task cgroup scope_id", maximum=512)


def task_job_name(
    binding: ProcessBinding,
    *,
    namespace: str = "Local",
) -> str:
    """Build the exact Windows Job name for one ProcessBinding."""

    if namespace not in {"Local", "Global"}:
        raise ValueError("Windows Job namespace must be Local or Global")
    return f"{namespace}\\InfinityForge-{process_binding_token(binding)}"


@dataclass(frozen=True, slots=True, order=True)
class ProcessMemberIdentity:
    """One PID plus its OS start token, which detects PID reuse."""

    pid: int
    start_identity: str

    def __post_init__(self) -> None:
        _positive_int(self.pid, "member pid")
        _bounded_text(self.start_identity, "member start_identity", maximum=512)


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """Durable worker identity; exact Stop permits only cgroup or Job scopes."""

    binding: ProcessBinding
    platform: str
    pid: int
    start_identity: str
    scope_kind: ProcessScopeKind
    scope_id: str
    control_group_id: int | None
    members: tuple[ProcessMemberIdentity, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.binding, ProcessBinding):
            raise TypeError("binding must be ProcessBinding")
        if self.platform not in {"posix", "windows"}:
            raise ValueError("platform must be posix or windows")
        _positive_int(self.pid, "pid")
        _bounded_text(self.start_identity, "start_identity", maximum=512)
        if not isinstance(self.scope_kind, ProcessScopeKind):
            raise TypeError("scope_kind must be ProcessScopeKind")
        _bounded_text(self.scope_id, "scope_id", maximum=512)
        if not isinstance(self.members, tuple) or not self.members:
            raise ValueError("members must contain the root PID")
        if any(
            not isinstance(member, ProcessMemberIdentity) for member in self.members
        ):
            raise TypeError("members must contain ProcessMemberIdentity values")
        if tuple(sorted(self.members)) != self.members:
            raise ValueError("members must be sorted by PID")
        if len({member.pid for member in self.members}) != len(self.members):
            raise ValueError("members must not repeat a PID")
        root = next((member for member in self.members if member.pid == self.pid), None)
        if root is None:
            raise ValueError("members must contain the root PID")
        if root.start_identity != self.start_identity:
            raise ValueError("root PID start identity does not match")
        if self.scope_kind is ProcessScopeKind.WINDOWS_JOB:
            if self.platform != "windows":
                raise ValueError("Windows Job identity must use windows platform")
            _positive_int(self.control_group_id, "control_group_id")
            if self.control_group_id != self.pid:
                raise ValueError("Windows control group must be led by the root PID")
            _validate_job_name(self.scope_id)
            if self.scope_id not in {
                task_job_name(self.binding, namespace="Local"),
                task_job_name(self.binding, namespace="Global"),
            }:
                raise ValueError("Windows Job name does not match its ProcessBinding")
        elif self.control_group_id is not None:
            raise ValueError("control_group_id is only valid for Windows Jobs")
        if self.scope_kind in {ProcessScopeKind.PROCESS_GROUP, ProcessScopeKind.CGROUP}:
            if self.platform != "posix":
                raise ValueError("POSIX scope must use posix platform")
        if self.scope_kind is ProcessScopeKind.PROCESS_GROUP:
            if not self.scope_id.isascii() or not self.scope_id.isdigit():
                raise ValueError("process-group identity must be a positive integer")
            if int(self.scope_id) != self.pid:
                raise ValueError("process group must be led by the root PID")
        if self.scope_kind is ProcessScopeKind.CGROUP:
            _task_cgroup_parent_scope(self.binding, self.scope_id)

    def to_json(self) -> str:
        """Serialize the exact durable runtime value."""

        value = {
            "format_version": _FORMAT_VERSION,
            "binding": {
                "request_id": self.binding.request_id,
                "task_settings_hash": self.binding.task_settings_hash,
                "project_id": self.binding.project_id,
                "task_id": self.binding.task_id,
                "run_id": self.binding.run_id,
                "host_id": self.binding.host_id,
            },
            "platform": self.platform,
            "pid": self.pid,
            "start_identity": self.start_identity,
            "scope_kind": self.scope_kind.value,
            "scope_id": self.scope_id,
            "control_group_id": self.control_group_id,
            "members": [
                {"pid": member.pid, "start_identity": member.start_identity}
                for member in self.members
            ],
        }
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    @classmethod
    def from_json(cls, raw: str) -> ProcessIdentity:
        """Parse only the exact v1 identity shape."""

        value = _json_object(raw)
        _exact_fields(
            value,
            {
                "format_version",
                "binding",
                "platform",
                "pid",
                "start_identity",
                "scope_kind",
                "scope_id",
                "control_group_id",
                "members",
            },
            "process identity",
        )
        if value["format_version"] != _FORMAT_VERSION:
            raise ValueError("unsupported process identity format")
        binding_value = value["binding"]
        if not isinstance(binding_value, dict):
            raise ValueError("binding must be an object")
        _exact_fields(
            binding_value,
            {
                "request_id",
                "task_settings_hash",
                "project_id",
                "task_id",
                "run_id",
                "host_id",
            },
            "process binding",
        )
        member_values = value["members"]
        if not isinstance(member_values, list):
            raise ValueError("members must be an array")
        members: list[ProcessMemberIdentity] = []
        for member_value in member_values:
            if not isinstance(member_value, dict):
                raise ValueError("process member must be an object")
            _exact_fields(member_value, {"pid", "start_identity"}, "process member")
            members.append(
                ProcessMemberIdentity(
                    pid=member_value["pid"],
                    start_identity=member_value["start_identity"],
                )
            )
        try:
            scope_kind = ProcessScopeKind(value["scope_kind"])
        except (TypeError, ValueError) as error:
            raise ValueError("unsupported process scope kind") from error
        return cls(
            binding=ProcessBinding(
                request_id=binding_value["request_id"],
                task_settings_hash=binding_value["task_settings_hash"],
                project_id=binding_value["project_id"],
                task_id=binding_value["task_id"],
                run_id=binding_value["run_id"],
                host_id=binding_value["host_id"],
            ),
            platform=value["platform"],
            pid=value["pid"],
            start_identity=value["start_identity"],
            scope_kind=scope_kind,
            scope_id=value["scope_id"],
            control_group_id=value["control_group_id"],
            members=tuple(members),
        )


@dataclass(frozen=True, slots=True)
class ProcessStopResult:
    """Read-back result; complete means the recorded scope has zero members."""

    identity: ProcessIdentity
    term_sent: bool
    forced: bool
    already_stopped: bool
    remaining_members: tuple[ProcessMemberIdentity, ...]

    @property
    def completed(self) -> bool:
        return not self.remaining_members


class ProcessScopeBackend(Protocol):
    """Minimal boundary used by exact tree termination."""

    def supports_graceful(self, identity: ProcessIdentity) -> bool: ...

    def scope_members(
        self, identity: ProcessIdentity
    ) -> tuple[ProcessMemberIdentity, ...]: ...

    def signal_scope(self, identity: ProcessIdentity, *, force: bool) -> None: ...


def terminate_exact_process_tree(
    identity: ProcessIdentity,
    *,
    expected: ProcessBinding,
    current_host: str,
    backend: ProcessScopeBackend,
    term_timeout_seconds: float = 5.0,
    force_timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.1,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> ProcessStopResult:
    """Stop only the recorded tree, then prove the descendant count is zero."""

    if not isinstance(identity, ProcessIdentity):
        raise TypeError("identity must be ProcessIdentity")
    if not isinstance(expected, ProcessBinding):
        raise TypeError("expected must be ProcessBinding")
    _canonical_uuid(current_host, "current_host")
    term_timeout_seconds = _finite_positive_number(
        term_timeout_seconds, "term_timeout_seconds"
    )
    force_timeout_seconds = _finite_positive_number(
        force_timeout_seconds, "force_timeout_seconds"
    )
    poll_interval_seconds = _finite_positive_number(
        poll_interval_seconds, "poll_interval_seconds"
    )
    current, authorized_members = _validate_exact_process_tree(
        identity,
        expected=expected,
        current_host=current_host,
        backend=backend,
    )
    if not current:
        return ProcessStopResult(
            identity=identity,
            term_sent=False,
            forced=False,
            already_stopped=True,
            remaining_members=(),
        )

    graceful = backend.supports_graceful(identity)
    if not isinstance(graceful, bool):
        raise ProcessIdentityError("process backend graceful capability is invalid")
    if not graceful:
        # RISK(side-effect): Windows and Linux runtimes without pidfd support
        # use only the exact Job/cgroup kernel handle. No PID-number fallback
        # is permitted.
        forced = _signal_or_gone(identity, backend, force=True)
        remaining = _wait_for_zero(
            identity,
            backend,
            authorized_members=authorized_members,
            timeout_seconds=force_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            monotonic=monotonic,
            sleep=sleep,
        )
        return ProcessStopResult(
            identity=identity,
            term_sent=False,
            forced=forced,
            already_stopped=False,
            remaining_members=remaining,
        )

    # RISK(side-effect): graceful signaling is allowed only after every live
    # PID/start pair was proven inside the durable Task cgroup.
    term_sent = _signal_or_gone(identity, backend, force=False)
    remaining = _wait_for_zero(
        identity,
        backend,
        authorized_members=authorized_members,
        timeout_seconds=term_timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        monotonic=monotonic,
        sleep=sleep,
    )
    forced = False
    if remaining:
        # RISK(side-effect): the force path addresses only the same recorded
        # cgroup/Job after a fresh start-identity read-back.
        forced = _signal_or_gone(identity, backend, force=True)
        remaining = _wait_for_zero(
            identity,
            backend,
            authorized_members=authorized_members,
            timeout_seconds=force_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            monotonic=monotonic,
            sleep=sleep,
        )
    return ProcessStopResult(
        identity=identity,
        term_sent=term_sent,
        forced=forced,
        already_stopped=False,
        remaining_members=remaining,
    )


def validate_exact_process_tree(
    identity: ProcessIdentity,
    *,
    expected: ProcessBinding,
    current_host: str,
    backend: ProcessScopeBackend,
) -> tuple[ProcessMemberIdentity, ...]:
    """Prove exact Task authority and OS boundary without sending a signal."""

    current, _authorized = _validate_exact_process_tree(
        identity,
        expected=expected,
        current_host=current_host,
        backend=backend,
    )
    return current


def _validate_exact_process_tree(
    identity: ProcessIdentity,
    *,
    expected: ProcessBinding,
    current_host: str,
    backend: ProcessScopeBackend,
) -> tuple[tuple[ProcessMemberIdentity, ...], dict[int, str]]:
    if not isinstance(identity, ProcessIdentity):
        raise TypeError("identity must be ProcessIdentity")
    if not isinstance(expected, ProcessBinding):
        raise TypeError("expected must be ProcessBinding")
    _canonical_uuid(current_host, "current_host")
    if identity.binding != expected:
        raise ProcessIdentityMismatch("recorded process belongs to another Task or run")
    if identity.binding.host_id != current_host:
        raise ProcessIdentityMismatch("recorded process belongs to another owner host")
    if identity.scope_kind is ProcessScopeKind.PROCESS_GROUP:
        raise ProcessIdentityError(
            "exact worker stop requires a Linux cgroup or Windows Job boundary"
        )
    return _initial_scope_members(identity, backend)


def _signal_or_gone(
    identity: ProcessIdentity,
    backend: ProcessScopeBackend,
    *,
    force: bool,
) -> bool:
    try:
        backend.signal_scope(identity, force=force)
    except ProcessLookupError:
        return False
    except OSError as error:
        if not force:
            return False
        raise ProcessIdentityError("exact process scope signal failed") from error
    return True


def _wait_for_zero(
    identity: ProcessIdentity,
    backend: ProcessScopeBackend,
    *,
    authorized_members: dict[int, str],
    timeout_seconds: float,
    poll_interval_seconds: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> tuple[ProcessMemberIdentity, ...]:
    deadline = monotonic() + timeout_seconds
    while True:
        members = _trusted_scope_members(identity, backend, authorized_members)
        if not members or monotonic() >= deadline:
            return members
        sleep(min(poll_interval_seconds, max(0.0, deadline - monotonic())))


def _initial_scope_members(
    identity: ProcessIdentity,
    backend: ProcessScopeBackend,
) -> tuple[tuple[ProcessMemberIdentity, ...], dict[int, str]]:
    try:
        current = tuple(sorted(backend.scope_members(identity)))
    except ProcessLookupError:
        current = ()
    durable = {member.pid: member.start_identity for member in identity.members}
    root = next((member for member in current if member.pid == identity.pid), None)
    if root is not None and root.start_identity != identity.start_identity:
        raise ProcessIdentityMismatch(f"PID {identity.pid} start identity changed")
    for member in current:
        expected_start = durable.get(member.pid)
        if expected_start is None:
            if root is None:
                raise ProcessIdentityMismatch(
                    f"recorded process scope contains unrecorded member PID {member.pid}"
                )
            continue
        # RISK(security): a PID alone is never authority because the OS may
        # recycle it after the worker exits.
        if expected_start != member.start_identity:
            raise ProcessIdentityMismatch(f"PID {member.pid} start identity changed")
    authorized = dict(durable)
    authorized.update({member.pid: member.start_identity for member in current})
    return current, authorized


def _trusted_scope_members(
    identity: ProcessIdentity,
    backend: ProcessScopeBackend,
    authorized_members: dict[int, str],
) -> tuple[ProcessMemberIdentity, ...]:
    try:
        current = tuple(sorted(backend.scope_members(identity)))
    except ProcessLookupError:
        return ()
    for member in current:
        expected_start = authorized_members.get(member.pid)
        if expected_start != member.start_identity:
            # RISK(security): after the initial root/start proof, cgroup and Job
            # membership is the kernel-owned authority. Descendants may spawn,
            # exit, and reuse a PID while TERM is pending; they remain inside
            # the exact boundary and must be included in force/readback.
            if identity.scope_kind not in {
                ProcessScopeKind.CGROUP,
                ProcessScopeKind.WINDOWS_JOB,
            }:
                raise ProcessIdentityMismatch(
                    f"recorded process scope contains unrecorded member PID {member.pid}"
                )
            authorized_members[member.pid] = member.start_identity
    return current


class PosixProcessBackend:
    """Linux /proc adapter; exact signaling requires a cgroup v2 boundary."""

    def __init__(
        self,
        *,
        proc_root: str | Path = "/proc",
        cgroup_root: str | Path = "/sys/fs/cgroup",
        boot_id: str | None = None,
        dispatcher_pid: int | None = None,
        kill_group: Callable[[int, int], None] | None = None,
        kill_process: Callable[[int, int], None] | None = None,
    ) -> None:
        self._proc_root = Path(proc_root)
        self._cgroup_root = Path(cgroup_root)
        self._boot_id = (
            _bounded_text(boot_id, "boot_id", maximum=512)
            if boot_id is not None
            else self._read_boot_id()
        )
        self._kill_group = kill_group if kill_group is not None else _posix_kill_group
        self._kill_process = (
            kill_process if kill_process is not None else _posix_kill_process
        )
        self._pidfd_open = getattr(os, "pidfd_open", None)
        self._pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
        self._close_fd = os.close
        self._dispatcher_pid = (
            os.getpid()
            if dispatcher_pid is None
            else _positive_int(dispatcher_pid, "dispatcher_pid")
        )

    def supports_graceful(self, identity: ProcessIdentity) -> bool:
        if identity.scope_kind is ProcessScopeKind.PROCESS_GROUP:
            return False
        if identity.scope_kind is not ProcessScopeKind.CGROUP:
            raise ProcessIdentityError("POSIX backend received a non-POSIX scope")
        return self._pidfd_open is not None and self._pidfd_send_signal is not None

    def capture_process_group(
        self,
        binding: ProcessBinding,
        *,
        pid: int,
    ) -> ProcessIdentity:
        """Capture a new-session worker whose PID is its process-group ID."""

        _positive_int(pid, "pid")
        root = self._snapshot(pid)
        if root is None:
            raise ProcessIdentityError("worker PID is not running")
        _parent, group_id, start_identity = root
        if group_id != pid:
            raise ProcessIdentityError("worker PID is not its process-group leader")
        members = self._group_members(group_id)
        return ProcessIdentity(
            binding=binding,
            platform="posix",
            pid=pid,
            start_identity=start_identity,
            scope_kind=ProcessScopeKind.PROCESS_GROUP,
            scope_id=str(group_id),
            control_group_id=None,
            members=members,
        )

    def capture_cgroup(
        self,
        binding: ProcessBinding,
        *,
        pid: int,
        delegated_parent_scope: str,
        scope_id: str,
        binding_token: str,
    ) -> ProcessIdentity:
        """Capture the worker's exact Linux cgroup v2 boundary."""

        _positive_int(pid, "pid")
        expected_token = process_binding_token(binding)
        if _lower_sha256(binding_token, "binding_token") != expected_token:
            raise ProcessIdentityError("cgroup binding token does not match its Task")
        try:
            expected_scope = task_cgroup_scope_id(
                binding,
                delegated_parent_scope=delegated_parent_scope,
            )
        except (TypeError, ValueError) as error:
            raise ProcessIdentityError("delegated Task cgroup scope is invalid") from error
        if scope_id != expected_scope:
            raise ProcessIdentityError("scope_id is not the exact Task cgroup")
        self._require_delegated_parent(delegated_parent_scope)
        root = self._snapshot(pid)
        if root is None:
            raise ProcessIdentityError("worker PID is not running")
        current_cgroup = self._process_cgroup(pid)
        if current_cgroup is None:
            raise ProcessIdentityError("worker has no cgroup v2 identity")
        if current_cgroup != expected_scope:
            raise ProcessIdentityMismatch("worker PID is outside the exact Task cgroup")
        self._require_task_cgroup_kill(expected_scope)
        members = self._cgroup_members(expected_scope)
        return ProcessIdentity(
            binding=binding,
            platform="posix",
            pid=pid,
            start_identity=root[2],
            scope_kind=ProcessScopeKind.CGROUP,
            scope_id=expected_scope,
            control_group_id=None,
            members=members,
        )

    def scope_members(
        self,
        identity: ProcessIdentity,
    ) -> tuple[ProcessMemberIdentity, ...]:
        if identity.scope_kind is ProcessScopeKind.PROCESS_GROUP:
            try:
                group_id = int(identity.scope_id)
            except ValueError as error:
                raise ProcessIdentityError("invalid process-group identity") from error
            return self._group_members(group_id)
        if identity.scope_kind is ProcessScopeKind.CGROUP:
            self._require_exact_task_cgroup(identity)
            return self._cgroup_members(identity.scope_id)
        raise ProcessIdentityError("POSIX backend received a non-POSIX scope")

    def signal_scope(self, identity: ProcessIdentity, *, force: bool) -> None:
        if identity.scope_kind is ProcessScopeKind.PROCESS_GROUP:
            raise ProcessIdentityError(
                "exact POSIX worker signaling requires a cgroup boundary"
            )
        if identity.scope_kind is not ProcessScopeKind.CGROUP:
            raise ProcessIdentityError("POSIX backend received a non-POSIX scope")
        self._require_exact_task_cgroup(identity)
        if force:
            control = self._cgroup_control(identity.scope_id, "cgroup.kill")
            # RISK(side-effect): cgroup.kill is used only for the already
            # validated cgroup path and never for a parent/global cgroup.
            control.write_text("1\n", encoding="ascii")
            return
        if self._pidfd_open is None or self._pidfd_send_signal is None:
            raise ProcessIdentityError(
                "Linux pidfd signaling is required for exact cgroup TERM"
            )
        for member in self.scope_members(identity):
            self._signal_cgroup_member(identity, member)

    def _require_exact_task_cgroup(self, identity: ProcessIdentity) -> None:
        try:
            delegated_parent = _task_cgroup_parent_scope(
                identity.binding,
                identity.scope_id,
            )
        except (TypeError, ValueError) as error:
            raise ProcessIdentityError("recorded Task cgroup scope is invalid") from error
        self._require_delegated_parent(delegated_parent)
        self._require_task_cgroup_kill(identity.scope_id)

    def _require_task_cgroup_kill(self, scope_id: str) -> None:
        task_directory = self._cgroup_directory(scope_id)
        kill_control = self._checked_cgroup_file(task_directory, "cgroup.kill")
        if not os.access(kill_control, os.W_OK):
            raise ProcessIdentityError("exact Task cgroup.kill is not writable")

    def _require_delegated_parent(self, delegated_parent_scope: str) -> None:
        try:
            parent_scope = _canonical_cgroup_scope(
                delegated_parent_scope,
                "delegated_parent_scope",
            )
        except (TypeError, ValueError) as error:
            raise ProcessIdentityError("delegated cgroup parent is invalid") from error
        dispatcher_scope = self._process_cgroup(self._dispatcher_pid)
        if dispatcher_scope != parent_scope:
            raise ProcessIdentityMismatch(
                "delegated parent does not match the dispatcher cgroup"
            )
        parent = self._cgroup_directory(parent_scope)
        if not os.access(parent, os.W_OK | os.X_OK):
            raise ProcessIdentityError("dispatcher cgroup is not delegated for child control")
        direct_members = self._read_cgroup_process_ids(parent)
        if self._dispatcher_pid not in direct_members:
            raise ProcessIdentityMismatch(
                "dispatcher PID is not in the delegated parent cgroup"
            )
        for filename in ("cgroup.controllers", "cgroup.subtree_control"):
            control = self._checked_cgroup_file(parent, filename)
            try:
                control.read_text(encoding="ascii")
            except (OSError, UnicodeError) as error:
                raise ProcessIdentityError(
                    "dispatcher cgroup delegation controls are unavailable"
                ) from error

    def _signal_cgroup_member(
        self,
        identity: ProcessIdentity,
        member: ProcessMemberIdentity,
    ) -> None:
        assert self._pidfd_open is not None
        assert self._pidfd_send_signal is not None
        try:
            descriptor = self._pidfd_open(member.pid, 0)
        except OSError as error:
            if error.errno == errno.ESRCH:
                return
            raise ProcessIdentityError(
                f"cannot open pidfd for cgroup PID {member.pid}"
            ) from error
        if (
            not isinstance(descriptor, int)
            or isinstance(descriptor, bool)
            or descriptor < 0
        ):
            raise ProcessIdentityError("pidfd_open returned an invalid descriptor")
        try:
            snapshot = self._snapshot(member.pid)
            if snapshot is None:
                return
            if snapshot[2] != member.start_identity:
                raise ProcessIdentityMismatch(
                    f"PID {member.pid} start identity changed before pidfd signal"
                )
            current_cgroup = self._process_cgroup(member.pid)
            if current_cgroup is None:
                raise ProcessIdentityMismatch(
                    f"PID {member.pid} cgroup cannot be proven before pidfd signal"
                )
            if not _is_same_or_descendant_cgroup(
                current_cgroup,
                identity.scope_id,
            ):
                raise ProcessIdentityMismatch(
                    f"PID {member.pid} left the exact cgroup before pidfd signal"
                )
            try:
                self._pidfd_send_signal(descriptor, signal.SIGTERM)
            except OSError as error:
                if error.errno == errno.ESRCH:
                    return
                raise ProcessIdentityError(
                    f"pidfd signal failed for cgroup PID {member.pid}"
                ) from error
        finally:
            self._close_fd(descriptor)

    def _read_boot_id(self) -> str:
        try:
            value = (
                (self._proc_root / "sys/kernel/random/boot_id")
                .read_text(encoding="ascii")
                .strip()
            )
        except OSError as error:
            raise ProcessIdentityError("Linux boot identity is unavailable") from error
        return _bounded_text(value, "boot_id", maximum=512)

    def _snapshot(self, pid: int) -> tuple[int, int, str] | None:
        try:
            raw = (self._proc_root / str(pid) / "stat").read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as error:
            raise ProcessIdentityError(f"cannot inspect PID {pid}") from error
        closing = raw.rfind(")")
        if closing < 1:
            raise ProcessIdentityError(f"PID {pid} stat record is malformed")
        fields = raw[closing + 1 :].strip().split()
        if len(fields) < 20:
            raise ProcessIdentityError(f"PID {pid} stat record is incomplete")
        try:
            parent_pid = int(fields[1])
            group_id = int(fields[2])
            start_ticks = int(fields[19])
        except ValueError as error:
            raise ProcessIdentityError(
                f"PID {pid} stat identity is malformed"
            ) from error
        return parent_pid, group_id, f"{self._boot_id}:{start_ticks}"

    def _all_process_ids(self) -> tuple[int, ...]:
        try:
            return tuple(
                sorted(
                    int(entry.name)
                    for entry in self._proc_root.iterdir()
                    if entry.is_dir() and entry.name.isascii() and entry.name.isdigit()
                )
            )
        except OSError as error:
            raise ProcessIdentityError("cannot enumerate Linux processes") from error

    def _group_members(self, group_id: int) -> tuple[ProcessMemberIdentity, ...]:
        members: list[ProcessMemberIdentity] = []
        for pid in self._all_process_ids():
            snapshot = self._snapshot(pid)
            if snapshot is not None and snapshot[1] == group_id:
                members.append(
                    ProcessMemberIdentity(pid=pid, start_identity=snapshot[2])
                )
        return tuple(members)

    def _process_cgroup(self, pid: int) -> str | None:
        try:
            lines = (
                (self._proc_root / str(pid) / "cgroup")
                .read_text(encoding="utf-8")
                .splitlines()
            )
        except FileNotFoundError:
            return None
        except OSError as error:
            raise ProcessIdentityError(f"cannot inspect PID {pid} cgroup") from error
        matches = [line[3:] for line in lines if line.startswith("0::/")]
        if len(matches) != 1:
            return None
        value = "/" + matches[0].lstrip("/")
        if "\x00" in value or any(
            part in {"", ".", ".."} for part in value[1:].split("/")
        ):
            return None
        return value

    def _cgroup_members(self, cgroup: str) -> tuple[ProcessMemberIdentity, ...]:
        root = self._cgroup_directory(cgroup)
        directories = self._cgroup_descendants(root)
        process_ids: set[int] = set()
        for directory in directories:
            try:
                process_ids.update(self._read_cgroup_process_ids(directory))
            except ProcessLookupError:
                if directory == root:
                    raise
                continue

        members: list[ProcessMemberIdentity] = []
        for pid in sorted(process_ids):
            # A process may move or exit after cgroup.procs is read. Only a
            # fresh read that is still inside the exact subtree is accepted.
            current_cgroup = self._process_cgroup(pid)
            if current_cgroup is None or not _is_same_or_descendant_cgroup(
                current_cgroup, cgroup
            ):
                continue
            snapshot = self._snapshot(pid)
            if snapshot is not None:
                members.append(
                    ProcessMemberIdentity(pid=pid, start_identity=snapshot[2])
                )

        populated = self._cgroup_populated(root)
        if populated and not members:
            raise ProcessIdentityError(
                "cgroup is populated but descendant membership is not proven"
            )
        if not populated and members:
            raise ProcessIdentityError(
                "cgroup populated readback conflicts with descendant membership"
            )
        return tuple(members)

    def _read_cgroup_process_ids(self, directory: Path) -> tuple[int, ...]:
        control = self._checked_cgroup_file(directory, "cgroup.procs")
        try:
            lines = control.read_text(encoding="ascii").splitlines()
        except FileNotFoundError:
            raise ProcessLookupError(str(directory)) from None
        except (OSError, UnicodeError) as error:
            raise ProcessIdentityError("cannot read exact cgroup membership") from error
        process_ids: set[int] = set()
        for raw_pid in lines:
            if (
                not raw_pid
                or not raw_pid.isascii()
                or not raw_pid.isdigit()
                or int(raw_pid) <= 0
            ):
                raise ProcessIdentityError("cgroup.procs contains an invalid PID")
            process_ids.add(int(raw_pid))
        return tuple(sorted(process_ids))

    def _cgroup_descendants(self, root: Path) -> tuple[Path, ...]:
        directories: list[Path] = []
        pending = [root]
        while pending:
            directory = pending.pop()
            directories.append(directory)
            try:
                entries = tuple(directory.iterdir())
            except FileNotFoundError:
                if directory == root:
                    raise ProcessLookupError(str(root)) from None
                continue
            except OSError as error:
                raise ProcessIdentityError(
                    "cannot enumerate exact cgroup subtree"
                ) from error
            children: list[Path] = []
            for entry in entries:
                if entry.is_symlink():
                    raise ProcessIdentityError(
                        "cgroup subtree contains a symbolic link"
                    )
                try:
                    if entry.is_dir():
                        children.append(entry)
                except OSError as error:
                    raise ProcessIdentityError(
                        "cannot inspect exact cgroup subtree"
                    ) from error
            pending.extend(sorted(children, reverse=True))
        return tuple(sorted(directories))

    def _cgroup_populated(self, root: Path) -> bool:
        control = self._checked_cgroup_file(root, "cgroup.events")
        try:
            lines = control.read_text(encoding="ascii").splitlines()
        except FileNotFoundError:
            raise ProcessLookupError(str(root)) from None
        except (OSError, UnicodeError) as error:
            raise ProcessIdentityError("cannot read cgroup populated state") from error
        values = [line.split() for line in lines]
        populated = [
            parts[1] for parts in values if len(parts) == 2 and parts[0] == "populated"
        ]
        if len(populated) != 1 or populated[0] not in {"0", "1"}:
            raise ProcessIdentityError("cgroup.events has no exact populated state")
        return populated[0] == "1"

    def _cgroup_directory(self, cgroup: str) -> Path:
        if (
            not isinstance(cgroup, str)
            or not cgroup.startswith("/")
            or cgroup == "/"
            or "\x00" in cgroup
            or any(part in {"", ".", ".."} for part in cgroup[1:].split("/"))
        ):
            raise ProcessIdentityError(
                "cgroup identity must be an absolute non-root cgroup path"
            )
        root = self._cgroup_root.resolve()
        try:
            candidate = (root / cgroup.lstrip("/")).resolve(strict=True)
        except FileNotFoundError:
            raise ProcessLookupError(cgroup) from None
        if not candidate.is_relative_to(root):
            raise ProcessIdentityError("cgroup identity escapes the worker boundary")
        if candidate.is_symlink() or not candidate.is_dir():
            raise ProcessIdentityError("cgroup identity is not an exact directory")
        return candidate

    @staticmethod
    def _checked_cgroup_file(directory: Path, filename: str) -> Path:
        if filename not in {
            "cgroup.controllers",
            "cgroup.events",
            "cgroup.kill",
            "cgroup.procs",
            "cgroup.subtree_control",
        }:
            raise ProcessIdentityError("unsupported cgroup control file")
        candidate = directory / filename
        if candidate.is_symlink():
            raise ProcessIdentityError("cgroup control file is a symbolic link")
        try:
            if not candidate.is_file():
                raise ProcessIdentityError(f"{filename} cgroup control is unavailable")
        except OSError as error:
            raise ProcessIdentityError(
                f"cannot inspect {filename} cgroup control"
            ) from error
        return candidate

    def _cgroup_control(self, cgroup: str, filename: str) -> Path:
        return self._checked_cgroup_file(self._cgroup_directory(cgroup), filename)


class WindowsJobApi(Protocol):
    def process_start_identity(self, pid: int) -> str: ...

    def job_process_ids(self, job_name: str) -> tuple[int, ...]: ...

    def job_breakaway_flags(self, job_name: str) -> int: ...

    def terminate_job(self, job_name: str) -> None: ...


class WindowsJobBackend:
    """Named Windows Job adapter with force-only exact-handle termination."""

    def __init__(self, *, api: WindowsJobApi | None = None) -> None:
        self._api = api if api is not None else _CtypesWindowsJobApi()

    def supports_graceful(self, identity: ProcessIdentity) -> bool:
        if identity.scope_kind is not ProcessScopeKind.WINDOWS_JOB:
            raise ProcessIdentityError("Windows backend received a non-Job scope")
        return False

    def capture_job(
        self,
        binding: ProcessBinding,
        *,
        pid: int,
        job_name: str,
        control_group_id: int,
    ) -> ProcessIdentity:
        _positive_int(pid, "pid")
        _positive_int(control_group_id, "control_group_id")
        _validate_job_name(job_name)
        if job_name not in {
            task_job_name(binding, namespace="Local"),
            task_job_name(binding, namespace="Global"),
        }:
            raise ProcessIdentityError("Windows Job name does not match its Task")
        self._require_contained_job(job_name)
        members = self._job_members(job_name)
        root = next((member for member in members if member.pid == pid), None)
        if root is None:
            raise ProcessIdentityError("worker PID is not a member of the Windows Job")
        return ProcessIdentity(
            binding=binding,
            platform="windows",
            pid=pid,
            start_identity=root.start_identity,
            scope_kind=ProcessScopeKind.WINDOWS_JOB,
            scope_id=job_name,
            control_group_id=control_group_id,
            members=members,
        )

    def scope_members(
        self, identity: ProcessIdentity
    ) -> tuple[ProcessMemberIdentity, ...]:
        if identity.scope_kind is not ProcessScopeKind.WINDOWS_JOB:
            raise ProcessIdentityError("Windows backend received a non-Job scope")
        self._require_contained_job(identity.scope_id)
        return self._job_members(identity.scope_id)

    def signal_scope(self, identity: ProcessIdentity, *, force: bool) -> None:
        if identity.scope_kind is not ProcessScopeKind.WINDOWS_JOB:
            raise ProcessIdentityError("Windows backend received a non-Job scope")
        self._require_contained_job(identity.scope_id)
        if not force:
            raise ProcessIdentityError(
                "Windows Job has no exact graceful signal; force is required"
            )
        # RISK(side-effect): TerminateJobObject is restricted to the exact
        # binding-derived named Job whose membership was just checked.
        self._api.terminate_job(identity.scope_id)

    def _job_members(self, job_name: str) -> tuple[ProcessMemberIdentity, ...]:
        _validate_job_name(job_name)
        members: list[ProcessMemberIdentity] = []
        for pid in sorted(self._api.job_process_ids(job_name)):
            try:
                start_identity = self._api.process_start_identity(pid)
            except ProcessLookupError:
                continue
            members.append(
                ProcessMemberIdentity(pid=pid, start_identity=start_identity)
            )
        return tuple(members)

    def _require_contained_job(self, job_name: str) -> None:
        flags = self._api.job_breakaway_flags(job_name)
        if not isinstance(flags, int) or isinstance(flags, bool) or flags < 0:
            raise ProcessIdentityError("Windows Job breakaway policy is invalid")
        if flags:
            raise ProcessIdentityError(
                "Windows Job allows child breakaway from the exact Task boundary"
            )


class _CtypesWindowsJobApi:
    """Small ctypes wrapper; it never searches by process name."""

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _JOB_OBJECT_QUERY = 0x0004
    _JOB_OBJECT_TERMINATE = 0x0008
    _JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
    _JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
    _BREAKAWAY_MASK = (
        _JOB_OBJECT_LIMIT_BREAKAWAY_OK | _JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
    )
    _MAX_JOB_QUERY_ATTEMPTS = 8
    _MAX_JOB_PROCESS_CAPACITY = 65_536

    def __init__(self) -> None:
        if os.name != "nt":
            raise ProcessIdentityError("Windows Job API is unavailable on this host")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        from ctypes import wintypes

        self._kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        self._kernel32.GetProcessTimes.restype = wintypes.BOOL
        self._kernel32.OpenJobObjectW.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        self._kernel32.OpenJobObjectW.restype = wintypes.HANDLE
        self._kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        self._kernel32.TerminateJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.UINT,
        ]
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

    def process_start_identity(self, pid: int) -> str:
        from ctypes import wintypes

        handle = self._kernel32.OpenProcess(
            self._PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:  # ERROR_INVALID_PARAMETER: PID is already gone.
                raise ProcessLookupError(pid)
            raise OSError(error, "OpenProcess failed")
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        try:
            ok = self._kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            )
            if not ok:
                raise OSError(ctypes.get_last_error(), "GetProcessTimes failed")
            value = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return f"windows-filetime:{value}"
        finally:
            self._kernel32.CloseHandle(handle)

    def job_process_ids(self, job_name: str) -> tuple[int, ...]:
        handle = self._open_job(job_name, self._JOB_OBJECT_QUERY)
        try:
            capacity = 64
            previous_complete: tuple[int, ...] | None = None
            for _attempt in range(self._MAX_JOB_QUERY_ATTEMPTS):
                header_size = ctypes.sizeof(ctypes.c_ulong) * 2
                buffer = ctypes.create_string_buffer(
                    header_size + capacity * ctypes.sizeof(ctypes.c_size_t)
                )
                ok = self._kernel32.QueryInformationJobObject(
                    handle,
                    self._JOB_OBJECT_BASIC_PROCESS_ID_LIST,
                    buffer,
                    len(buffer),
                    None,
                )
                if not ok and ctypes.get_last_error() != 234:  # ERROR_MORE_DATA
                    raise OSError(
                        ctypes.get_last_error(), "QueryInformationJobObject failed"
                    )
                assigned = ctypes.c_ulong.from_buffer(buffer, 0).value
                listed = ctypes.c_ulong.from_buffer(
                    buffer, ctypes.sizeof(ctypes.c_ulong)
                ).value
                if listed > assigned or listed > capacity:
                    raise ProcessIdentityError(
                        "Windows Job process membership is inconsistent"
                    )
                if ok and listed == assigned:
                    array_type = ctypes.c_size_t * listed
                    values = array_type.from_buffer(buffer, header_size)
                    current = tuple(sorted(int(value) for value in values))
                    if any(pid <= 0 for pid in current) or len(set(current)) != len(
                        current
                    ):
                        raise ProcessIdentityError(
                            "Windows Job process membership is inconsistent"
                        )
                    if previous_complete == current:
                        return current
                    previous_complete = current
                    continue
                previous_complete = None
                if listed < assigned:
                    capacity = max(capacity * 2, int(assigned))
                    if capacity > self._MAX_JOB_PROCESS_CAPACITY:
                        break
                    continue
                capacity *= 2
                if capacity > self._MAX_JOB_PROCESS_CAPACITY:
                    break
            raise ProcessIdentityError(
                "Windows Job process membership did not reach a stable snapshot"
            )
        finally:
            self._kernel32.CloseHandle(handle)

    def job_breakaway_flags(self, job_name: str) -> int:
        from ctypes import wintypes

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        handle = self._open_job(job_name, self._JOB_OBJECT_QUERY)
        try:
            information = _ExtendedLimitInformation()
            ok = self._kernel32.QueryInformationJobObject(
                handle,
                self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(information),
                ctypes.sizeof(information),
                None,
            )
            if not ok:
                raise OSError(
                    ctypes.get_last_error(),
                    "QueryInformationJobObject limits failed",
                )
            return (
                int(information.BasicLimitInformation.LimitFlags) & self._BREAKAWAY_MASK
            )
        finally:
            self._kernel32.CloseHandle(handle)

    def terminate_job(self, job_name: str) -> None:
        handle = self._open_job(job_name, self._JOB_OBJECT_TERMINATE)
        try:
            if not self._kernel32.TerminateJobObject(handle, 1):
                raise OSError(ctypes.get_last_error(), "TerminateJobObject failed")
        finally:
            self._kernel32.CloseHandle(handle)

    def _open_job(self, job_name: str, access: int) -> int:
        handle = self._kernel32.OpenJobObjectW(access, False, job_name)
        if not handle:
            error = ctypes.get_last_error()
            if error in {2, 6}:
                raise ProcessLookupError(job_name)
            raise OSError(error, "OpenJobObjectW failed")
        return int(handle)


def _validate_job_name(value: object) -> str:
    job_name = _bounded_text(value, "job_name", maximum=256)
    prefix = next(
        (
            candidate
            for candidate in ("Local\\InfinityForge-", "Global\\InfinityForge-")
            if job_name.startswith(candidate)
        ),
        None,
    )
    suffix = "" if prefix is None else job_name[len(prefix) :]
    if (
        prefix is None
        or not suffix
        or "\\" in suffix
        or "/" in suffix
        or any(ord(character) < 32 or ord(character) == 127 for character in suffix)
    ):
        raise ValueError("job_name must use an InfinityForge Job namespace")
    return job_name


def _canonical_cgroup_scope(value: object, label: str) -> str:
    scope = _bounded_text(value, label, maximum=512)
    if (
        not scope.startswith("/")
        or scope == "/"
        or scope.endswith("/")
        or "\x00" in scope
        or "\\" in scope
        or any(ord(character) < 32 or ord(character) == 127 for character in scope)
        or any(part in {"", ".", ".."} for part in scope[1:].split("/"))
    ):
        raise ValueError(f"{label} must be a canonical absolute non-root cgroup path")
    return scope


def _task_cgroup_parent_scope(binding: ProcessBinding, scope_id: str) -> str:
    scope = _canonical_cgroup_scope(scope_id, "scope_id")
    suffix = f"/{_TASK_CGROUP_SEGMENT}/{process_binding_token(binding)}"
    if not scope.endswith(suffix):
        raise ValueError("Task cgroup scope does not match its ProcessBinding")
    parent = scope[: -len(suffix)]
    if not parent:
        raise ValueError("Task cgroup requires a delegated non-root parent")
    parent = _canonical_cgroup_scope(parent, "delegated parent scope")
    if task_cgroup_scope_id(
        binding,
        delegated_parent_scope=parent,
    ) != scope:
        raise ValueError("Task cgroup scope is not canonical")
    return parent


def _is_same_or_descendant_cgroup(candidate: str, root: str) -> bool:
    return candidate == root or candidate.startswith(root.rstrip("/") + "/")


def _posix_kill_group(group_id: int, signal_number: int) -> None:
    kill_group = getattr(os, "killpg", None)
    if kill_group is None:
        raise ProcessIdentityError("POSIX process groups are unavailable on this host")
    kill_group(group_id, signal_number)


def _posix_kill_process(pid: int, signal_number: int) -> None:
    if os.name == "nt":
        raise ProcessIdentityError("POSIX process signals are unavailable on this host")
    os.kill(pid, signal_number)


def _canonical_uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise ValueError(f"{label} must be a canonical UUID") from error
    if str(parsed) != value:
        raise ValueError(f"{label} must be a canonical UUID")
    return value


def _lower_sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _bounded_text(value: object, label: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{label} must be non-empty bounded UTF-8 text")
    return value


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _finite_positive_number(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{label} must be a finite positive number")
    try:
        number = float(value)
    except OverflowError as error:
        raise ValueError(f"{label} must be a finite positive number") from error
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} must be a finite positive number")
    return number


def _json_object(raw: str) -> dict[str, object]:
    if not isinstance(raw, str):
        raise TypeError("process identity JSON must be text")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except json.JSONDecodeError as error:
        raise ValueError("process identity JSON is invalid") from error
    if not isinstance(value, dict):
        raise ValueError("process identity must be an object")
    return value


def _exact_fields(value: dict[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{label} fields do not match the exact contract")


__all__ = [
    "PosixProcessBackend",
    "ProcessBinding",
    "ProcessIdentity",
    "ProcessIdentityError",
    "ProcessIdentityMismatch",
    "ProcessMemberIdentity",
    "ProcessScopeBackend",
    "ProcessScopeKind",
    "ProcessStopResult",
    "WindowsJobBackend",
    "process_binding_token",
    "task_cgroup_scope_id",
    "task_job_name",
    "terminate_exact_process_tree",
    "validate_exact_process_tree",
]
