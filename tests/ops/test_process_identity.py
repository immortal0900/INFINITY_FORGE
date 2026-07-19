from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from forge.ops.process_identity import (
    PosixProcessBackend,
    ProcessBinding,
    ProcessIdentity,
    ProcessIdentityMismatch,
    ProcessMemberIdentity,
    ProcessScopeKind,
    WindowsJobBackend,
    terminate_exact_process_tree,
)


REQUEST_ID = str(uuid4())
HOST_ID = str(uuid4())
OTHER_HOST_ID = str(uuid4())
PROJECT_ID = "a" * 64


def _binding(**changes: object) -> ProcessBinding:
    values: dict[str, object] = {
        "request_id": REQUEST_ID,
        "project_id": PROJECT_ID,
        "task_id": "t_build",
        "run_id": "17",
        "host_id": HOST_ID,
    }
    values.update(changes)
    return ProcessBinding(**values)  # type: ignore[arg-type]


def _identity(**changes: object) -> ProcessIdentity:
    values: dict[str, object] = {
        "binding": _binding(),
        "platform": "posix",
        "pid": 101,
        "start_identity": "boot-id:9001",
        "scope_kind": ProcessScopeKind.PROCESS_GROUP,
        "scope_id": "101",
        "control_group_id": None,
        "members": (
            ProcessMemberIdentity(pid=101, start_identity="boot-id:9001"),
            ProcessMemberIdentity(pid=102, start_identity="boot-id:9002"),
        ),
    }
    values.update(changes)
    return ProcessIdentity(**values)  # type: ignore[arg-type]


class _FakeBackend:
    def __init__(
        self,
        snapshots: list[tuple[ProcessMemberIdentity, ...]],
    ) -> None:
        self._snapshots = list(snapshots)
        self.signals: list[bool] = []

    def scope_members(
        self, identity: ProcessIdentity
    ) -> tuple[ProcessMemberIdentity, ...]:
        del identity
        if len(self._snapshots) > 1:
            return self._snapshots.pop(0)
        return self._snapshots[0]

    def signal_scope(self, identity: ProcessIdentity, *, force: bool) -> None:
        del identity
        self.signals.append(force)


def test_process_identity_round_trips_exact_json() -> None:
    identity = _identity()

    restored = ProcessIdentity.from_json(identity.to_json())

    assert restored == identity


@pytest.mark.parametrize(
    "expected,current_host",
    [
        (_binding(task_id="t_other"), HOST_ID),
        (_binding(), OTHER_HOST_ID),
        (_binding(run_id="18"), HOST_ID),
    ],
)
def test_wrong_task_run_or_host_fails_before_any_process_signal(
    expected: ProcessBinding,
    current_host: str,
) -> None:
    backend = _FakeBackend([_identity().members])

    with pytest.raises(ProcessIdentityMismatch):
        terminate_exact_process_tree(
            _identity(),
            expected=expected,
            current_host=current_host,
            backend=backend,
            term_timeout_seconds=0,
            force_timeout_seconds=0,
        )

    assert backend.signals == []


def test_pid_reuse_fails_before_any_process_signal() -> None:
    reused = (ProcessMemberIdentity(pid=101, start_identity="boot-id:DIFFERENT"),)
    backend = _FakeBackend([reused])

    with pytest.raises(ProcessIdentityMismatch, match="start identity"):
        terminate_exact_process_tree(
            _identity(),
            expected=_binding(),
            current_host=HOST_ID,
            backend=backend,
            term_timeout_seconds=0,
            force_timeout_seconds=0,
        )

    assert backend.signals == []


def test_unknown_member_fails_closed_instead_of_killing_a_reused_group() -> None:
    unknown = (ProcessMemberIdentity(pid=999, start_identity="boot-id:9999"),)
    backend = _FakeBackend([unknown])

    with pytest.raises(ProcessIdentityMismatch, match="unrecorded member"):
        terminate_exact_process_tree(
            _identity(),
            expected=_binding(),
            current_host=HOST_ID,
            backend=backend,
            term_timeout_seconds=0,
            force_timeout_seconds=0,
        )

    assert backend.signals == []


def test_new_descendant_is_authorized_only_while_exact_root_is_still_present() -> None:
    root = _identity().members[0]
    new_descendant = ProcessMemberIdentity(pid=103, start_identity="boot-id:9003")
    backend = _FakeBackend([(root, new_descendant), ()])

    result = terminate_exact_process_tree(
        replace(_identity(), members=(root,)),
        expected=_binding(),
        current_host=HOST_ID,
        backend=backend,
        term_timeout_seconds=0,
        force_timeout_seconds=0,
    )

    assert result.completed is True
    assert backend.signals == [False]


def test_term_then_exact_force_requires_descendant_zero_readback() -> None:
    members = _identity().members
    survivor = (members[1],)
    backend = _FakeBackend([members, survivor, ()])

    result = terminate_exact_process_tree(
        _identity(),
        expected=_binding(),
        current_host=HOST_ID,
        backend=backend,
        term_timeout_seconds=0,
        force_timeout_seconds=0,
    )

    assert backend.signals == [False, True]
    assert result.term_sent is True
    assert result.forced is True
    assert result.completed is True
    assert result.remaining_members == ()


def test_already_dead_tree_is_complete_without_signal() -> None:
    backend = _FakeBackend([()])

    result = terminate_exact_process_tree(
        _identity(),
        expected=_binding(),
        current_host=HOST_ID,
        backend=backend,
        term_timeout_seconds=0,
        force_timeout_seconds=0,
    )

    assert result.completed is True
    assert result.already_stopped is True
    assert backend.signals == []


class _MissingScopeBackend(_FakeBackend):
    def scope_members(
        self, identity: ProcessIdentity
    ) -> tuple[ProcessMemberIdentity, ...]:
        del identity
        raise ProcessLookupError("scope is already gone")


def test_destroyed_job_or_group_is_already_stopped() -> None:
    backend = _MissingScopeBackend([()])

    result = terminate_exact_process_tree(
        _identity(),
        expected=_binding(),
        current_host=HOST_ID,
        backend=backend,
        term_timeout_seconds=0,
        force_timeout_seconds=0,
    )

    assert result.completed is True
    assert result.already_stopped is True
    assert backend.signals == []


class _TermFailureBackend(_FakeBackend):
    def signal_scope(self, identity: ProcessIdentity, *, force: bool) -> None:
        if not force:
            self.signals.append(force)
            raise OSError("CTRL_BREAK unavailable")
        super().signal_scope(identity, force=force)


def test_graceful_signal_failure_still_uses_the_exact_force_boundary() -> None:
    members = _identity().members
    backend = _TermFailureBackend([members, members, ()])

    result = terminate_exact_process_tree(
        _identity(),
        expected=_binding(),
        current_host=HOST_ID,
        backend=backend,
        term_timeout_seconds=0,
        force_timeout_seconds=0,
    )

    assert backend.signals == [False, True]
    assert result.term_sent is False
    assert result.forced is True
    assert result.completed is True


def _write_proc_stat(
    proc_root: Path,
    *,
    pid: int,
    parent: int,
    group: int,
    start_ticks: int,
) -> None:
    process_dir = proc_root / str(pid)
    process_dir.mkdir(parents=True)
    # Linux /proc/<pid>/stat fields 3..22. The command deliberately contains
    # spaces and ')' so the parser must split at the final closing parenthesis.
    tail = ["S", str(parent), str(group), str(group)]
    tail.extend("0" for _ in range(15))
    tail.append(str(start_ticks))
    (process_dir / "stat").write_text(
        f"{pid} (worker ) name) {' '.join(tail)}\n",
        encoding="utf-8",
    )
    (process_dir / "cgroup").write_text("0::/forge/test\n", encoding="utf-8")


def test_posix_adapter_captures_group_and_start_identity_without_name_search(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    _write_proc_stat(proc_root, pid=101, parent=1, group=101, start_ticks=9001)
    _write_proc_stat(proc_root, pid=102, parent=101, group=101, start_ticks=9002)
    _write_proc_stat(proc_root, pid=777, parent=1, group=777, start_ticks=7000)
    signals: list[tuple[int, int]] = []
    backend = PosixProcessBackend(
        proc_root=proc_root,
        boot_id="boot-id",
        kill_group=lambda group, signal_number: signals.append((group, signal_number)),
    )

    identity = backend.capture_process_group(_binding(), pid=101)
    backend.signal_scope(identity, force=False)

    assert identity.scope_id == "101"
    assert identity.members == (
        ProcessMemberIdentity(pid=101, start_identity="boot-id:9001"),
        ProcessMemberIdentity(pid=102, start_identity="boot-id:9002"),
    )
    assert signals and signals[0][0] == 101


class _FakeWindowsJobApi:
    def __init__(self) -> None:
        self.break_groups: list[int] = []
        self.terminated_jobs: list[str] = []

    def process_start_identity(self, pid: int) -> str:
        return {101: "creation:1", 102: "creation:2"}[pid]

    def job_process_ids(self, job_name: str) -> tuple[int, ...]:
        assert job_name == "Local\\InfinityForge-test"
        return (101, 102)

    def send_control_break(self, group_id: int) -> None:
        self.break_groups.append(group_id)

    def terminate_job(self, job_name: str) -> None:
        self.terminated_jobs.append(job_name)


def test_windows_adapter_targets_only_recorded_job_and_control_group() -> None:
    api = _FakeWindowsJobApi()
    backend = WindowsJobBackend(api=api)

    identity = backend.capture_job(
        _binding(),
        pid=101,
        job_name="Local\\InfinityForge-test",
        control_group_id=101,
    )
    backend.signal_scope(identity, force=False)
    backend.signal_scope(identity, force=True)

    assert identity.platform == "windows"
    assert identity.start_identity == "creation:1"
    assert api.break_groups == [101]
    assert api.terminated_jobs == ["Local\\InfinityForge-test"]


def test_identity_rejects_scope_that_does_not_contain_the_recorded_pid() -> None:
    with pytest.raises(ValueError, match="root PID"):
        replace(
            _identity(),
            members=(ProcessMemberIdentity(pid=102, start_identity="boot-id:9002"),),
        )


def test_identity_rejects_a_process_group_not_led_by_the_recorded_pid() -> None:
    with pytest.raises(ValueError, match="process group"):
        replace(_identity(), scope_id="999")
