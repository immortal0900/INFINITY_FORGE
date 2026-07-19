"""Exact, task-bound process tree identity and termination adapters."""

from __future__ import annotations

import ctypes
import json
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
_SHA256_LENGTH = 64


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
    project_id: str
    task_id: str
    run_id: str
    host_id: str

    def __post_init__(self) -> None:
        _canonical_uuid(self.request_id, "request_id")
        _lower_sha256(self.project_id, "project_id")
        _bounded_text(self.task_id, "task_id", maximum=512)
        _bounded_text(self.run_id, "run_id", maximum=512)
        _canonical_uuid(self.host_id, "host_id")


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
    """Durable identity for one exact worker process group, cgroup, or Job."""

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
        if self.scope_kind is ProcessScopeKind.CGROUP and (
            not self.scope_id.startswith("/") or self.scope_id == "/"
        ):
            raise ValueError("cgroup identity must be an absolute non-root path")

    def to_json(self) -> str:
        """Serialize the exact durable runtime value."""

        value = {
            "format_version": _FORMAT_VERSION,
            "binding": {
                "request_id": self.binding.request_id,
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
            {"request_id", "project_id", "task_id", "run_id", "host_id"},
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
    _nonnegative_number(term_timeout_seconds, "term_timeout_seconds")
    _nonnegative_number(force_timeout_seconds, "force_timeout_seconds")
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be positive")
    if identity.binding != expected:
        raise ProcessIdentityMismatch("recorded process belongs to another Task or run")
    if identity.binding.host_id != current_host:
        raise ProcessIdentityMismatch("recorded process belongs to another owner host")

    current, authorized_members = _initial_scope_members(identity, backend)
    if not current:
        return ProcessStopResult(
            identity=identity,
            term_sent=False,
            forced=False,
            already_stopped=True,
            remaining_members=(),
        )

    # RISK(side-effect): signaling is allowed only after every live PID/start
    # pair was proven to be a subset of the durable Task-bound scope.
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
        # process group/cgroup/Job after a fresh start-identity read-back.
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
        if expected_start is None:
            raise ProcessIdentityMismatch(
                f"recorded process scope contains unrecorded member PID {member.pid}"
            )
        if expected_start != member.start_identity:
            raise ProcessIdentityMismatch(f"PID {member.pid} start identity changed")
    return current


class PosixProcessBackend:
    """Linux /proc adapter for an exact process group or cgroup."""

    def __init__(
        self,
        *,
        proc_root: str | Path = "/proc",
        cgroup_root: str | Path = "/sys/fs/cgroup",
        boot_id: str | None = None,
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
    ) -> ProcessIdentity:
        """Capture the worker's exact Linux cgroup v2 boundary."""

        _positive_int(pid, "pid")
        root = self._snapshot(pid)
        if root is None:
            raise ProcessIdentityError("worker PID is not running")
        cgroup = self._process_cgroup(pid)
        if cgroup is None:
            raise ProcessIdentityError("worker has no cgroup v2 identity")
        members = self._cgroup_members(cgroup)
        return ProcessIdentity(
            binding=binding,
            platform="posix",
            pid=pid,
            start_identity=root[2],
            scope_kind=ProcessScopeKind.CGROUP,
            scope_id=cgroup,
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
            return self._cgroup_members(identity.scope_id)
        raise ProcessIdentityError("POSIX backend received a non-POSIX scope")

    def signal_scope(self, identity: ProcessIdentity, *, force: bool) -> None:
        if identity.scope_kind is ProcessScopeKind.PROCESS_GROUP:
            signal_number = signal.SIGKILL if force else signal.SIGTERM
            self._kill_group(int(identity.scope_id), signal_number)
            return
        if identity.scope_kind is not ProcessScopeKind.CGROUP:
            raise ProcessIdentityError("POSIX backend received a non-POSIX scope")
        if force:
            control = self._cgroup_control(identity.scope_id, "cgroup.kill")
            # RISK(side-effect): cgroup.kill is used only for the already
            # validated cgroup path and never for a parent/global cgroup.
            control.write_text("1\n", encoding="ascii")
            return
        for member in self.scope_members(identity):
            self._kill_process(member.pid, signal.SIGTERM)

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
        self._cgroup_control(value, "cgroup.procs")
        return value

    def _cgroup_members(self, cgroup: str) -> tuple[ProcessMemberIdentity, ...]:
        members: list[ProcessMemberIdentity] = []
        for pid in self._all_process_ids():
            if self._process_cgroup(pid) != cgroup:
                continue
            snapshot = self._snapshot(pid)
            if snapshot is not None:
                members.append(
                    ProcessMemberIdentity(pid=pid, start_identity=snapshot[2])
                )
        return tuple(members)

    def _cgroup_control(self, cgroup: str, filename: str) -> Path:
        if not isinstance(cgroup, str) or not cgroup.startswith("/"):
            raise ProcessIdentityError(
                "cgroup identity must be an absolute cgroup path"
            )
        root = self._cgroup_root.resolve()
        candidate = (root / cgroup.lstrip("/") / filename).resolve()
        if not candidate.is_relative_to(root) or candidate.parent == root:
            raise ProcessIdentityError("cgroup identity escapes the worker boundary")
        return candidate


class WindowsJobApi(Protocol):
    def process_start_identity(self, pid: int) -> str: ...

    def job_process_ids(self, job_name: str) -> tuple[int, ...]: ...

    def send_control_break(self, group_id: int) -> None: ...

    def terminate_job(self, job_name: str) -> None: ...


class WindowsJobBackend:
    """Named Windows Job plus CREATE_NEW_PROCESS_GROUP adapter."""

    def __init__(self, *, api: WindowsJobApi | None = None) -> None:
        self._api = api if api is not None else _CtypesWindowsJobApi()

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
        return self._job_members(identity.scope_id)

    def signal_scope(self, identity: ProcessIdentity, *, force: bool) -> None:
        if identity.scope_kind is not ProcessScopeKind.WINDOWS_JOB:
            raise ProcessIdentityError("Windows backend received a non-Job scope")
        if force:
            # RISK(side-effect): TerminateJobObject is restricted to the exact
            # named Job whose recorded PID/start membership was just checked.
            self._api.terminate_job(identity.scope_id)
        else:
            assert identity.control_group_id is not None
            self._api.send_control_break(identity.control_group_id)

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


class _CtypesWindowsJobApi:
    """Small ctypes wrapper; it never searches by process name."""

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _JOB_OBJECT_QUERY = 0x0004
    _JOB_OBJECT_TERMINATE = 0x0008
    _JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
    _CTRL_BREAK_EVENT = 1

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
        self._kernel32.GenerateConsoleCtrlEvent.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._kernel32.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
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
            for capacity in (64, 256, 1024, 4096):
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
                assigned = ctypes.c_ulong.from_buffer(buffer, 0).value
                listed = ctypes.c_ulong.from_buffer(
                    buffer, ctypes.sizeof(ctypes.c_ulong)
                ).value
                if ok and listed <= assigned and assigned <= capacity:
                    array_type = ctypes.c_size_t * listed
                    values = array_type.from_buffer(buffer, header_size)
                    return tuple(int(value) for value in values)
                if not ok and ctypes.get_last_error() != 234:  # ERROR_MORE_DATA
                    raise OSError(
                        ctypes.get_last_error(), "QueryInformationJobObject failed"
                    )
            raise ProcessIdentityError("Windows Job has too many processes to verify")
        finally:
            self._kernel32.CloseHandle(handle)

    def send_control_break(self, group_id: int) -> None:
        if not self._kernel32.GenerateConsoleCtrlEvent(
            self._CTRL_BREAK_EVENT, group_id
        ):
            raise OSError(ctypes.get_last_error(), "GenerateConsoleCtrlEvent failed")

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
    if not job_name.startswith(("Local\\InfinityForge-", "Global\\InfinityForge-")):
        raise ValueError("job_name must use an InfinityForge Job namespace")
    return job_name


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


def _nonnegative_number(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label} must be non-negative")
    return float(value)


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
    "terminate_exact_process_tree",
]
