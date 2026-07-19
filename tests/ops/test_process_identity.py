from __future__ import annotations

import ctypes
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
import forge.ops.process_identity as process_identity_module

from forge.ops.process_identity import (
    PosixProcessBackend,
    ProcessBinding,
    ProcessIdentity,
    ProcessIdentityError,
    ProcessIdentityMismatch,
    ProcessMemberIdentity,
    ProcessScopeKind,
    WindowsJobBackend,
    _CtypesWindowsJobApi,
    terminate_exact_process_tree,
)


REQUEST_ID = str(uuid4())
HOST_ID = str(uuid4())
OTHER_HOST_ID = str(uuid4())
PROJECT_ID = "a" * 64
TASK_SETTINGS_HASH = "b" * 64
OTHER_TASK_SETTINGS_HASH = "c" * 64
DELEGATED_PARENT_SCOPE = "/system.slice/forge-dispatcher.service"
DISPATCHER_PID = 900


def _binding(**changes: object) -> ProcessBinding:
    values: dict[str, object] = {
        "request_id": REQUEST_ID,
        "task_settings_hash": TASK_SETTINGS_HASH,
        "project_id": PROJECT_ID,
        "task_id": "t_build",
        "run_id": "17",
        "host_id": HOST_ID,
    }
    values.update(changes)
    return ProcessBinding(**values)  # type: ignore[arg-type]


def _test_binding_token(binding: ProcessBinding | None = None) -> str:
    if binding is None:
        binding = _binding()
    canonical = json.dumps(
        {
            "format_version": "forge-process-binding-token/v1",
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


def _task_scope(binding: ProcessBinding | None = None) -> str:
    return f"{DELEGATED_PARENT_SCOPE}/infinity-forge/{_test_binding_token(binding)}"


def _task_job_name(binding: ProcessBinding | None = None) -> str:
    return f"Local\\InfinityForge-{_test_binding_token(binding)}"


def _identity(**changes: object) -> ProcessIdentity:
    values: dict[str, object] = {
        "binding": _binding(),
        "platform": "posix",
        "pid": 101,
        "start_identity": "boot-id:9001",
        "scope_kind": ProcessScopeKind.CGROUP,
        "scope_id": _task_scope(),
        "control_group_id": None,
        "members": (
            ProcessMemberIdentity(pid=101, start_identity="boot-id:9001"),
            ProcessMemberIdentity(pid=102, start_identity="boot-id:9002"),
        ),
    }
    values.update(changes)
    return ProcessIdentity(**values)  # type: ignore[arg-type]


def _kernel_boundary_identity(scope_kind: ProcessScopeKind) -> ProcessIdentity:
    if scope_kind is ProcessScopeKind.CGROUP:
        return replace(
            _identity(),
            scope_kind=scope_kind,
            scope_id=_task_scope(),
        )
    return replace(
        _identity(),
        platform="windows",
        scope_kind=ProcessScopeKind.WINDOWS_JOB,
        scope_id=_task_job_name(),
        control_group_id=101,
    )


class _FakeBackend:
    def __init__(
        self,
        snapshots: list[tuple[ProcessMemberIdentity, ...]],
    ) -> None:
        self._snapshots = list(snapshots)
        self.signals: list[bool] = []

    def supports_graceful(self, identity: ProcessIdentity) -> bool:
        return identity.scope_kind is not ProcessScopeKind.WINDOWS_JOB

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


def test_process_identity_json_binds_the_active_task_settings_hash() -> None:
    binding = ProcessBinding(
        request_id=REQUEST_ID,
        task_settings_hash=TASK_SETTINGS_HASH,
        project_id=PROJECT_ID,
        task_id="t_build",
        run_id="17",
        host_id=HOST_ID,
    )
    identity = replace(_identity(), binding=binding)

    restored = ProcessIdentity.from_json(identity.to_json())

    assert restored.binding.task_settings_hash == TASK_SETTINGS_HASH


def test_task_cgroup_scope_uses_the_canonical_process_binding_hash() -> None:
    binding = _binding()
    expected_token = _test_binding_token(binding)

    assert process_identity_module.process_binding_token(binding) == expected_token
    assert (
        process_identity_module.task_cgroup_scope_id(
            binding,
            delegated_parent_scope=DELEGATED_PARENT_SCOPE,
        )
        == f"{DELEGATED_PARENT_SCOPE}/infinity-forge/{expected_token}"
    )
    assert process_identity_module.task_job_name(binding) == (
        f"Local\\InfinityForge-{expected_token}"
    )


@pytest.mark.parametrize(
    "scope_id",
    (
        "/shared",
        f"/infinity-forge/{_test_binding_token()}",
        DELEGATED_PARENT_SCOPE,
        f"{DELEGATED_PARENT_SCOPE}/infinity-forge/" + "f" * 64,
        f"{_task_scope()}/child",
        f"{DELEGATED_PARENT_SCOPE}\n/infinity-forge/{_test_binding_token()}",
        f"{DELEGATED_PARENT_SCOPE}\\alias/infinity-forge/{_test_binding_token()}",
    ),
)
def test_cgroup_identity_rejects_a_scope_not_derived_from_its_binding(
    scope_id: str,
) -> None:
    with pytest.raises(ValueError, match="binding|Task cgroup|canonical"):
        replace(_identity(), scope_id=scope_id)


def test_active_revision_mismatch_fails_before_any_process_signal() -> None:
    recorded = ProcessBinding(
        request_id=REQUEST_ID,
        task_settings_hash=TASK_SETTINGS_HASH,
        project_id=PROJECT_ID,
        task_id="t_build",
        run_id="17",
        host_id=HOST_ID,
    )
    expected = replace(recorded, task_settings_hash=OTHER_TASK_SETTINGS_HASH)
    identity = replace(_identity(), binding=recorded)
    backend = _FakeBackend([identity.members])

    with pytest.raises(ProcessIdentityMismatch, match="another Task or run"):
        terminate_exact_process_tree(
            identity,
            expected=expected,
            current_host=HOST_ID,
            backend=backend,
        )

    assert backend.signals == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("term_timeout_seconds", 0),
        ("term_timeout_seconds", float("nan")),
        ("term_timeout_seconds", 10**1000),
        ("force_timeout_seconds", float("inf")),
        ("poll_interval_seconds", float("nan")),
    ],
)
def test_process_stop_timeouts_must_be_finite_and_positive(
    field: str,
    value: float,
) -> None:
    backend = _FakeBackend([()])

    with pytest.raises(ValueError, match="finite positive"):
        terminate_exact_process_tree(
            _identity(),
            expected=_binding(),
            current_host=HOST_ID,
            backend=backend,
            **{field: value},
        )


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
            term_timeout_seconds=0.000001,
            force_timeout_seconds=0.000001,
        )

    assert backend.signals == []


def test_exact_worker_stop_rejects_a_process_group_boundary_before_signal() -> None:
    identity = replace(
        _identity(),
        scope_kind=ProcessScopeKind.PROCESS_GROUP,
        scope_id="101",
    )
    backend = _FakeBackend([identity.members])

    with pytest.raises(ProcessIdentityError, match="cgroup|Windows Job"):
        terminate_exact_process_tree(
            identity,
            expected=identity.binding,
            current_host=HOST_ID,
            backend=backend,
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
            term_timeout_seconds=0.000001,
            force_timeout_seconds=0.000001,
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
            term_timeout_seconds=0.000001,
            force_timeout_seconds=0.000001,
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
        term_timeout_seconds=0.000001,
        force_timeout_seconds=0.000001,
    )

    assert result.completed is True
    assert backend.signals == [False]


def test_term_then_exact_force_requires_descendant_zero_readback() -> None:
    members = _identity().members
    survivor = (members[1],)
    backend = _FakeBackend([members, survivor, ()])
    clock = iter(range(0, 100, 2))

    result = terminate_exact_process_tree(
        _identity(),
        expected=_binding(),
        current_host=HOST_ID,
        backend=backend,
        term_timeout_seconds=0.000001,
        force_timeout_seconds=0.000001,
        monotonic=lambda: float(next(clock)),
    )

    assert backend.signals == [False, True]
    assert result.term_sent is True
    assert result.forced is True
    assert result.completed is True
    assert result.remaining_members == ()


def test_new_cgroup_descendant_after_term_is_included_in_force_readback() -> None:
    identity = _kernel_boundary_identity(ProcessScopeKind.CGROUP)
    new_descendant = ProcessMemberIdentity(pid=103, start_identity="new:9003")
    backend = _FakeBackend(
        [identity.members, (*identity.members, new_descendant), ()]
    )
    clock = iter(range(0, 100, 2))

    result = terminate_exact_process_tree(
        identity,
        expected=identity.binding,
        current_host=HOST_ID,
        backend=backend,
        term_timeout_seconds=0.000001,
        force_timeout_seconds=0.000001,
        monotonic=lambda: float(next(clock)),
    )

    assert backend.signals == [False, True]
    assert result.completed is True


def test_already_dead_tree_is_complete_without_signal() -> None:
    backend = _FakeBackend([()])

    result = terminate_exact_process_tree(
        _identity(),
        expected=_binding(),
        current_host=HOST_ID,
        backend=backend,
        term_timeout_seconds=0.000001,
        force_timeout_seconds=0.000001,
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
        term_timeout_seconds=0.000001,
        force_timeout_seconds=0.000001,
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
    clock = iter(range(0, 100, 2))

    result = terminate_exact_process_tree(
        _identity(),
        expected=_binding(),
        current_host=HOST_ID,
        backend=backend,
        term_timeout_seconds=0.000001,
        force_timeout_seconds=0.000001,
        monotonic=lambda: float(next(clock)),
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
    process_dir.mkdir(parents=True, exist_ok=True)
    # Linux /proc/<pid>/stat fields 3..22. The command deliberately contains
    # spaces and ')' so the parser must split at the final closing parenthesis.
    tail = ["S", str(parent), str(group), str(group)]
    tail.extend("0" for _ in range(15))
    tail.append(str(start_ticks))
    (process_dir / "stat").write_text(
        f"{pid} (worker ) name) {' '.join(tail)}\n",
        encoding="utf-8",
    )
    (process_dir / "cgroup").write_text(
        f"0::{_task_scope()}\n",
        encoding="utf-8",
    )


def _set_process_cgroup(proc_root: Path, pid: int, cgroup: str) -> None:
    (proc_root / str(pid) / "cgroup").write_text(
        f"0::{cgroup}\n",
        encoding="utf-8",
    )


def _write_cgroup(
    cgroup_root: Path,
    relative: str,
    *,
    process_ids: tuple[int, ...],
    populated: bool,
) -> Path:
    path = cgroup_root / relative
    path.mkdir(parents=True, exist_ok=True)
    (path / "cgroup.procs").write_text(
        "".join(f"{pid}\n" for pid in process_ids),
        encoding="ascii",
    )
    (path / "cgroup.events").write_text(
        f"populated {int(populated)}\n",
        encoding="ascii",
    )
    (path / "cgroup.kill").write_text("", encoding="ascii")
    return path


def _write_delegated_parent(proc_root: Path, cgroup_root: Path) -> None:
    _write_proc_stat(
        proc_root,
        pid=DISPATCHER_PID,
        parent=1,
        group=DISPATCHER_PID,
        start_ticks=8000,
    )
    _set_process_cgroup(proc_root, DISPATCHER_PID, DELEGATED_PARENT_SCOPE)
    parent = _write_cgroup(
        cgroup_root,
        DELEGATED_PARENT_SCOPE.lstrip("/"),
        process_ids=(DISPATCHER_PID,),
        populated=True,
    )
    (parent / "cgroup.controllers").write_text("cpu memory pids\n", encoding="ascii")
    (parent / "cgroup.subtree_control").write_text(
        "cpu memory pids\n",
        encoding="ascii",
    )


def _capture_task_cgroup(
    backend: PosixProcessBackend,
    *,
    pid: int = 101,
) -> ProcessIdentity:
    binding = _binding()
    return backend.capture_cgroup(
        binding,
        pid=pid,
        delegated_parent_scope=DELEGATED_PARENT_SCOPE,
        scope_id=_task_scope(binding),
        binding_token=_test_binding_token(binding),
    )


def test_posix_adapter_can_capture_but_never_signal_a_process_group_as_exact(
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
    with pytest.raises(ProcessIdentityError, match="cgroup"):
        backend.signal_scope(identity, force=False)

    assert identity.scope_id == "101"
    assert identity.members == (
        ProcessMemberIdentity(pid=101, start_identity="boot-id:9001"),
        ProcessMemberIdentity(pid=102, start_identity="boot-id:9002"),
    )
    assert signals == []


def test_cgroup_scope_includes_descendants_and_skips_unrelated_root_members(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    _write_delegated_parent(proc_root, cgroup_root)
    _write_proc_stat(proc_root, pid=101, parent=1, group=101, start_ticks=9001)
    _write_proc_stat(proc_root, pid=102, parent=101, group=101, start_ticks=9002)
    _write_proc_stat(proc_root, pid=777, parent=1, group=777, start_ticks=7000)
    _set_process_cgroup(proc_root, 102, f"{_task_scope()}/child")
    _set_process_cgroup(proc_root, 777, "/")
    target = _write_cgroup(
        cgroup_root,
        _task_scope().lstrip("/"),
        process_ids=(101,),
        populated=True,
    )
    _write_cgroup(
        cgroup_root,
        f"{_task_scope().lstrip('/')}/child",
        process_ids=(102,),
        populated=True,
    )
    _write_cgroup(
        cgroup_root,
        "unrelated",
        process_ids=(777,),
        populated=True,
    )
    backend = PosixProcessBackend(
        proc_root=proc_root,
        cgroup_root=cgroup_root,
        boot_id="boot-id",
        dispatcher_pid=DISPATCHER_PID,
    )

    identity = _capture_task_cgroup(backend)

    assert identity.members == (
        ProcessMemberIdentity(pid=101, start_identity="boot-id:9001"),
        ProcessMemberIdentity(pid=102, start_identity="boot-id:9002"),
    )

    backend.signal_scope(identity, force=True)
    assert (target / "cgroup.kill").read_text(encoding="ascii") == "1\n"
    (target / "cgroup.procs").write_text("", encoding="ascii")
    (target / "child" / "cgroup.procs").write_text("", encoding="ascii")
    (target / "child" / "cgroup.events").write_text("populated 0\n", encoding="ascii")
    (target / "cgroup.events").write_text("populated 0\n", encoding="ascii")
    assert backend.scope_members(identity) == ()


def test_capture_cgroup_never_adopts_a_shared_current_scope(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    _write_delegated_parent(proc_root, cgroup_root)
    _write_proc_stat(proc_root, pid=101, parent=1, group=101, start_ticks=9001)
    _set_process_cgroup(proc_root, 101, "/shared")
    _write_cgroup(cgroup_root, "shared", process_ids=(101,), populated=True)
    backend = PosixProcessBackend(
        proc_root=proc_root,
        cgroup_root=cgroup_root,
        boot_id="boot-id",
        dispatcher_pid=DISPATCHER_PID,
    )

    with pytest.raises(ProcessIdentityError, match="binding|exact Task cgroup"):
        backend.capture_cgroup(
            _binding(),
            pid=101,
            delegated_parent_scope=DELEGATED_PARENT_SCOPE,
            scope_id="/shared",
            binding_token="f" * 64,
        )


def test_capture_cgroup_rejects_a_parent_other_than_the_dispatcher_scope(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    _write_delegated_parent(proc_root, cgroup_root)
    wrong_parent = "/system.slice/shared.service"
    wrong_scope = f"{wrong_parent}/infinity-forge/{_test_binding_token()}"
    wrong_parent_dir = _write_cgroup(
        cgroup_root,
        wrong_parent.lstrip("/"),
        process_ids=(),
        populated=True,
    )
    (wrong_parent_dir / "cgroup.controllers").write_text("pids\n", encoding="ascii")
    (wrong_parent_dir / "cgroup.subtree_control").write_text(
        "pids\n",
        encoding="ascii",
    )
    _write_proc_stat(proc_root, pid=101, parent=1, group=101, start_ticks=9001)
    _set_process_cgroup(proc_root, 101, wrong_scope)
    _write_cgroup(
        cgroup_root,
        wrong_scope.lstrip("/"),
        process_ids=(101,),
        populated=True,
    )
    backend = PosixProcessBackend(
        proc_root=proc_root,
        cgroup_root=cgroup_root,
        boot_id="boot-id",
        dispatcher_pid=DISPATCHER_PID,
    )

    with pytest.raises(ProcessIdentityMismatch, match="dispatcher cgroup"):
        backend.capture_cgroup(
            _binding(),
            pid=101,
            delegated_parent_scope=wrong_parent,
            scope_id=wrong_scope,
            binding_token=_test_binding_token(),
        )


def test_force_signal_rechecks_the_dispatcher_delegated_parent(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    _write_delegated_parent(proc_root, cgroup_root)
    _write_proc_stat(proc_root, pid=101, parent=1, group=101, start_ticks=9001)
    target = _write_cgroup(
        cgroup_root,
        _task_scope().lstrip("/"),
        process_ids=(101,),
        populated=True,
    )
    backend = PosixProcessBackend(
        proc_root=proc_root,
        cgroup_root=cgroup_root,
        boot_id="boot-id",
        dispatcher_pid=DISPATCHER_PID,
    )
    identity = _capture_task_cgroup(backend)
    _set_process_cgroup(proc_root, DISPATCHER_PID, "/system.slice/other.service")

    with pytest.raises(ProcessIdentityMismatch, match="dispatcher cgroup"):
        backend.signal_scope(identity, force=True)

    assert (target / "cgroup.kill").read_text(encoding="ascii") == ""


def test_cgroup_populated_readback_cannot_claim_zero_without_member_proof(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    _write_delegated_parent(proc_root, cgroup_root)
    _write_proc_stat(proc_root, pid=101, parent=1, group=101, start_ticks=9001)
    target = _write_cgroup(
        cgroup_root,
        _task_scope().lstrip("/"),
        process_ids=(101,),
        populated=True,
    )
    backend = PosixProcessBackend(
        proc_root=proc_root,
        cgroup_root=cgroup_root,
        boot_id="boot-id",
        dispatcher_pid=DISPATCHER_PID,
    )
    identity = _capture_task_cgroup(backend)
    (target / "cgroup.procs").write_text("", encoding="ascii")

    with pytest.raises(ProcessIdentityError, match="populated"):
        backend.scope_members(identity)


def test_exact_cgroup_kill_control_must_exist_before_force_signal(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    _write_delegated_parent(proc_root, cgroup_root)
    _write_proc_stat(proc_root, pid=101, parent=1, group=101, start_ticks=9001)
    target = _write_cgroup(
        cgroup_root,
        _task_scope().lstrip("/"),
        process_ids=(101,),
        populated=True,
    )
    backend = PosixProcessBackend(
        proc_root=proc_root,
        cgroup_root=cgroup_root,
        boot_id="boot-id",
        dispatcher_pid=DISPATCHER_PID,
    )
    identity = _capture_task_cgroup(backend)
    (target / "cgroup.kill").unlink()

    with pytest.raises(ProcessIdentityError, match="cgroup.kill|control"):
        backend.signal_scope(identity, force=True)

    assert not (target / "cgroup.kill").exists()


def test_cgroup_membership_tolerates_kernel_documented_duplicate_pid_reads(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    _write_delegated_parent(proc_root, cgroup_root)
    _write_proc_stat(proc_root, pid=101, parent=1, group=101, start_ticks=9001)
    _write_cgroup(
        cgroup_root,
        _task_scope().lstrip("/"),
        process_ids=(101, 101),
        populated=True,
    )
    backend = PosixProcessBackend(
        proc_root=proc_root,
        cgroup_root=cgroup_root,
        boot_id="boot-id",
        dispatcher_pid=DISPATCHER_PID,
    )

    identity = _capture_task_cgroup(backend)

    assert identity.members == (
        ProcessMemberIdentity(pid=101, start_identity="boot-id:9001"),
    )


class _CgroupKillReadbackBackend(PosixProcessBackend):
    def __init__(self, *, target: Path, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._target = target

    def signal_scope(self, identity: ProcessIdentity, *, force: bool) -> None:
        super().signal_scope(identity, force=force)
        if force:
            (self._target / "cgroup.procs").write_text("", encoding="ascii")
            (self._target / "cgroup.events").write_text(
                "populated 0\n",
                encoding="ascii",
            )


def test_pidfd_unavailable_uses_exact_cgroup_kill_force_only(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    _write_delegated_parent(proc_root, cgroup_root)
    _write_proc_stat(proc_root, pid=101, parent=1, group=101, start_ticks=9001)
    target = _write_cgroup(
        cgroup_root,
        _task_scope().lstrip("/"),
        process_ids=(101,),
        populated=True,
    )
    backend = _CgroupKillReadbackBackend(
        target=target,
        proc_root=proc_root,
        cgroup_root=cgroup_root,
        boot_id="boot-id",
        dispatcher_pid=DISPATCHER_PID,
    )
    backend._pidfd_open = None
    backend._pidfd_send_signal = None
    identity = _capture_task_cgroup(backend)

    result = terminate_exact_process_tree(
        identity,
        expected=identity.binding,
        current_host=HOST_ID,
        backend=backend,
        term_timeout_seconds=0.000001,
        force_timeout_seconds=0.000001,
    )

    assert result.term_sent is False
    assert result.forced is True
    assert result.completed is True
    assert (target / "cgroup.kill").read_text(encoding="ascii") == "1\n"


class _PidReuseBeforeSignalBackend(PosixProcessBackend):
    def scope_members(
        self,
        identity: ProcessIdentity,
    ) -> tuple[ProcessMemberIdentity, ...]:
        members = super().scope_members(identity)
        _write_proc_stat(
            self._proc_root,
            pid=101,
            parent=1,
            group=777,
            start_ticks=9999,
        )
        _set_process_cgroup(self._proc_root, 101, "/unrelated")
        return members


def test_cgroup_soft_signal_revalidates_pidfd_identity_after_pid_reuse(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    _write_delegated_parent(proc_root, cgroup_root)
    _write_proc_stat(proc_root, pid=101, parent=1, group=101, start_ticks=9001)
    _write_cgroup(
        cgroup_root,
        _task_scope().lstrip("/"),
        process_ids=(101,),
        populated=True,
    )
    opened: list[int] = []
    sent: list[tuple[int, int]] = []
    closed: list[int] = []
    raw_signals: list[tuple[int, int]] = []
    backend = _PidReuseBeforeSignalBackend(
        proc_root=proc_root,
        cgroup_root=cgroup_root,
        boot_id="boot-id",
        dispatcher_pid=DISPATCHER_PID,
        kill_process=lambda pid, signal_number: raw_signals.append(
            (pid, signal_number)
        ),
    )
    backend._pidfd_open = lambda pid, _flags: opened.append(pid) or 50
    backend._pidfd_send_signal = lambda fd, sig: sent.append((fd, sig))
    backend._close_fd = closed.append
    identity = _capture_task_cgroup(backend)

    with pytest.raises(ProcessIdentityMismatch, match="start identity|cgroup"):
        backend.signal_scope(identity, force=False)

    assert opened == [101]
    assert sent == []
    assert closed == [50]
    assert raw_signals == []


class _FakeWindowsJobApi:
    def __init__(self) -> None:
        self.break_groups: list[int] = []
        self.terminated_jobs: list[str] = []

    def process_start_identity(self, pid: int) -> str:
        return {101: "creation:1", 102: "creation:2"}[pid]

    def job_process_ids(self, job_name: str) -> tuple[int, ...]:
        assert job_name == _task_job_name()
        return (101, 102)

    def job_breakaway_flags(self, job_name: str) -> int:
        assert job_name == _task_job_name()
        return 0

    def send_control_break(self, group_id: int) -> None:
        self.break_groups.append(group_id)

    def terminate_job(self, job_name: str) -> None:
        self.terminated_jobs.append(job_name)


class _StoppingWindowsJobApi(_FakeWindowsJobApi):
    def job_process_ids(self, job_name: str) -> tuple[int, ...]:
        if self.terminated_jobs:
            return ()
        return super().job_process_ids(job_name)


def test_windows_exact_stop_uses_only_the_job_handle_force_path() -> None:
    api = _StoppingWindowsJobApi()
    backend = WindowsJobBackend(api=api)
    identity = backend.capture_job(
        _binding(),
        pid=101,
        job_name=_task_job_name(),
        control_group_id=101,
    )

    result = terminate_exact_process_tree(
        identity,
        expected=identity.binding,
        current_host=HOST_ID,
        backend=backend,
        term_timeout_seconds=0.000001,
        force_timeout_seconds=0.000001,
    )

    assert api.break_groups == []
    assert api.terminated_jobs == [_task_job_name()]
    assert result.term_sent is False
    assert result.forced is True


def test_windows_adapter_targets_only_the_binding_derived_job() -> None:
    api = _FakeWindowsJobApi()
    backend = WindowsJobBackend(api=api)

    identity = backend.capture_job(
        _binding(),
        pid=101,
        job_name=_task_job_name(),
        control_group_id=101,
    )
    with pytest.raises(ProcessIdentityError, match="force"):
        backend.signal_scope(identity, force=False)
    backend.signal_scope(identity, force=True)

    assert identity.platform == "windows"
    assert identity.start_identity == "creation:1"
    assert api.break_groups == []
    assert api.terminated_jobs == [_task_job_name()]


def test_windows_job_name_rejects_a_different_process_binding() -> None:
    identity = _kernel_boundary_identity(ProcessScopeKind.WINDOWS_JOB)

    with pytest.raises(ValueError, match="ProcessBinding"):
        replace(identity, binding=_binding(run_id="18"))


@pytest.mark.parametrize(
    "job_name",
    (
        "Local\\InfinityForge-safe\x00alias",
        "Local\\InfinityForge-safe\nalias",
        "Local\\InfinityForge-safe\\alias",
        "Global\\InfinityForge-safe\\alias",
        "Local\\InfinityForge-",
    ),
)
def test_windows_job_name_rejects_control_and_namespace_aliases(
    job_name: str,
) -> None:
    members = (ProcessMemberIdentity(pid=101, start_identity="creation:1"),)

    with pytest.raises(ValueError, match="job_name"):
        ProcessIdentity(
            binding=_binding(),
            platform="windows",
            pid=101,
            start_identity="creation:1",
            scope_kind=ProcessScopeKind.WINDOWS_JOB,
            scope_id=job_name,
            control_group_id=101,
            members=members,
        )


class _BreakawayWindowsJobApi(_FakeWindowsJobApi):
    def job_breakaway_flags(self, job_name: str) -> int:
        assert job_name == _task_job_name()
        return 0x0800


def test_windows_job_with_breakaway_policy_is_rejected_before_capture() -> None:
    backend = WindowsJobBackend(api=_BreakawayWindowsJobApi())

    with pytest.raises(ProcessIdentityError, match="breakaway"):
        backend.capture_job(
            _binding(),
            pid=101,
            job_name=_task_job_name(),
            control_group_id=101,
        )


class _ChangingBreakawayWindowsJobApi(_FakeWindowsJobApi):
    def __init__(self) -> None:
        super().__init__()
        self._flags = iter((0, 0x1000))

    def job_breakaway_flags(self, job_name: str) -> int:
        assert job_name == _task_job_name()
        return next(self._flags)


def test_windows_job_breakaway_policy_is_rechecked_before_signal() -> None:
    api = _ChangingBreakawayWindowsJobApi()
    backend = WindowsJobBackend(api=api)
    identity = backend.capture_job(
        _binding(),
        pid=101,
        job_name=_task_job_name(),
        control_group_id=101,
    )

    with pytest.raises(ProcessIdentityError, match="breakaway"):
        backend.signal_scope(identity, force=True)

    assert api.terminated_jobs == []


class _FakeJobQueryKernel:
    def __init__(
        self,
        responses: list[tuple[int, tuple[int, ...]]],
    ) -> None:
        self.responses = list(responses)
        self.query_count = 0

    def QueryInformationJobObject(
        self,
        handle: int,
        information_class: int,
        buffer: object,
        buffer_length: int,
        return_length: object,
    ) -> bool:
        del handle, information_class, buffer_length, return_length
        assigned, values = self.responses[self.query_count % len(self.responses)]
        self.query_count += 1
        ctypes.c_ulong.from_buffer(buffer, 0).value = assigned
        listed_offset = ctypes.sizeof(ctypes.c_ulong)
        ctypes.c_ulong.from_buffer(buffer, listed_offset).value = len(values)
        header_size = ctypes.sizeof(ctypes.c_ulong) * 2
        identifiers = (ctypes.c_size_t * len(values)).from_buffer(buffer, header_size)
        for index, value in enumerate(values):
            identifiers[index] = value
        return True

    def CloseHandle(self, handle: int) -> bool:
        del handle
        return True


class _FailingJobQueryKernel(_FakeJobQueryKernel):
    def QueryInformationJobObject(
        self,
        handle: int,
        information_class: int,
        buffer: object,
        buffer_length: int,
        return_length: object,
    ) -> bool:
        super().QueryInformationJobObject(
            handle,
            information_class,
            buffer,
            buffer_length,
            return_length,
        )
        ctypes.set_last_error(5)
        return False


def _windows_query_api(kernel: _FakeJobQueryKernel) -> _CtypesWindowsJobApi:
    api = object.__new__(_CtypesWindowsJobApi)
    api._kernel32 = kernel  # type: ignore[attr-defined]
    api._open_job = lambda _name, _access: 1  # type: ignore[method-assign]
    return api


def test_windows_job_query_retries_when_listed_is_less_than_assigned() -> None:
    kernel = _FakeJobQueryKernel(
        [
            (3, (101, 102)),
            (3, (101, 102, 103)),
            (3, (101, 102, 103)),
        ]
    )

    result = _windows_query_api(kernel).job_process_ids("Local\\InfinityForge-test")

    assert result == (101, 102, 103)
    assert kernel.query_count >= 3


def test_windows_job_query_does_not_mask_non_buffer_api_failure() -> None:
    kernel = _FailingJobQueryKernel([(3, (101, 102))])

    with pytest.raises(OSError, match="QueryInformationJobObject"):
        _windows_query_api(kernel).job_process_ids("Local\\InfinityForge-test")

    assert kernel.query_count == 1


def test_windows_job_query_fails_closed_when_membership_never_stabilizes() -> None:
    kernel = _FakeJobQueryKernel(
        [
            (1, (101,)),
            (1, (102,)),
        ]
    )

    with pytest.raises(ProcessIdentityError, match="stable"):
        _windows_query_api(kernel).job_process_ids("Local\\InfinityForge-test")

    assert kernel.query_count > 2


def test_windows_job_query_rejects_more_listed_than_assigned() -> None:
    kernel = _FakeJobQueryKernel([(1, (101, 102))])

    with pytest.raises(ProcessIdentityError, match="inconsistent"):
        _windows_query_api(kernel).job_process_ids("Local\\InfinityForge-test")


def test_windows_job_query_treats_order_only_changes_as_one_stable_membership() -> None:
    kernel = _FakeJobQueryKernel(
        [
            (2, (102, 101)),
            (2, (101, 102)),
        ]
    )

    result = _windows_query_api(kernel).job_process_ids("Local\\InfinityForge-test")

    assert result == (101, 102)
    assert kernel.query_count == 2


def test_identity_rejects_scope_that_does_not_contain_the_recorded_pid() -> None:
    with pytest.raises(ValueError, match="root PID"):
        replace(
            _identity(),
            members=(ProcessMemberIdentity(pid=102, start_identity="boot-id:9002"),),
        )


def test_identity_rejects_a_process_group_not_led_by_the_recorded_pid() -> None:
    with pytest.raises(ValueError, match="process group"):
        replace(
            _identity(),
            scope_kind=ProcessScopeKind.PROCESS_GROUP,
            scope_id="999",
        )
