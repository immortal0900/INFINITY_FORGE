from __future__ import annotations

import importlib
import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from forge.ops.process_identity import (
    ProcessBinding,
    ProcessIdentity,
    ProcessMemberIdentity,
    ProcessScopeKind,
    task_cgroup_scope_id,
)
from forge.ops.task_database import TaskDatabase
from forge.ops.task_options import MergeMode, TaskFlow
from forge.ops.task_projects import TaskProject
from forge.ops.task_settings import TaskContent
from forge.ops.task_settings_v2 import TaskRequestV2, TaskSettingsV2


HOST_ID = "d6f70d5d-6482-45f5-80d2-219ec2ad4d19"
REQUEST_ID = "4485be21-2a8f-41b8-a2a2-e25722df284e"
NOW = datetime(2026, 7, 19, 6, 7, 8, 123456, tzinfo=UTC)
HERMES_RUN_ID = 41
HERMES_CLAIM_LOCK = "host-id:claim-token"


def _runtime_module():
    return importlib.import_module("forge.ops.worker_runtime")


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hermes_contract(runtime, tmp_path: Path) -> dict[str, object]:
    database_path = (tmp_path / "hermes-kanban.db").resolve()
    with sqlite3.connect(database_path):
        pass
    return {
        "hermes_board": "forge-board",
        "hermes_database_path": str(database_path),
        "hermes_database_identity": runtime.hermes_database_identity(database_path),
        "hermes_run_id": HERMES_RUN_ID,
        "hermes_claim_lock": HERMES_CLAIM_LOCK,
    }


def _seed_active(
    tmp_path: Path,
) -> tuple[TaskDatabase, TaskRequestV2, TaskSettingsV2, TaskProject]:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    project = TaskProject.create(
        repository="example/project",
        workspace=str(workspace),
        remote_name="origin",
        base_branch="main",
        base_commit="a" * 40,
        host_id=HOST_ID,
    )
    request = TaskRequestV2.create(
        request_id=REQUEST_ID,
        management_repository="example/infinity-forge",
        task_content=TaskContent(
            title="Run one verified worker",
            description="Keep the selected Project binding exact.",
            acceptance_criteria=("Record the exact worker result.",),
        ),
        task_flow=TaskFlow.BUILD,
        merge_mode=MergeMode.MANUAL,
        merge_order=None,
        projects=(project,),
        task_owner_host=HOST_ID,
        confirmed_by="local-user",
        confirmed_at=NOW,
    )
    settings = TaskSettingsV2.create(request=request, parent_issue_number=21)
    database = TaskDatabase(tmp_path / "task.db")
    confirmed_at = json.loads(request.to_json())["confirmed_at"]
    project_json = _canonical(json.loads(request.to_json())["projects"][0])
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO task_requests (
                request_id, format_version, request_json, request_hash,
                management_repository, task_owner_host, confirmed_by,
                confirmed_at, replaces_request_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                request.request_id,
                request.format_version,
                request.to_json(),
                request.request_hash,
                request.management_repository,
                request.task_owner_host,
                request.confirmed_by,
                confirmed_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO task_settings_v2 (
                task_settings_hash, request_id, request_hash, format_version,
                settings_json, management_repository, parent_issue_number,
                task_owner_host, confirmed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                settings.task_settings_hash,
                settings.request_id,
                settings.request_hash,
                settings.format_version,
                settings.to_json(),
                settings.management_repository,
                settings.parent_issue_number,
                settings.task_owner_host,
                confirmed_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO task_projects (
                request_id, project_id, task_settings_hash, project_json,
                state, root_card_id, updated_at
            ) VALUES (?, ?, ?, ?, 'ready', 'root-card', ?)
            """,
            (
                request.request_id,
                project.project_id,
                settings.task_settings_hash,
                project_json,
                confirmed_at,
            ),
        )
        event_json = _canonical({"task_settings_hash": settings.task_settings_hash})
        for event_type in ("settings_activated", "active"):
            connection.execute(
                """
                INSERT INTO task_events (
                    request_id, task_settings_hash, event_type, event_key,
                    event_json, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    request.request_id,
                    settings.task_settings_hash,
                    event_type,
                    event_type,
                    event_json,
                    confirmed_at,
                ),
            )
    return database, request, settings, project


class _Clock:
    def __init__(self) -> None:
        self.current = NOW + timedelta(minutes=1)

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=1)
        return value


class _FakeDriver:
    def __init__(
        self,
        runtime,
        runtime_name: str,
        availability,
        *,
        database: TaskDatabase | None = None,
        scope_kind: ProcessScopeKind = ProcessScopeKind.CGROUP,
        result_packet_hash: str | None = None,
        stop_before_result: bool = False,
        wait_statuses: tuple[tuple[bool, int | None], ...] = ((True, 0),),
        result_hermes_run_id: int | None = None,
    ) -> None:
        self.runtime = runtime
        self.runtime_name = runtime_name
        self._availability = availability
        self.database = database
        self.scope_kind = scope_kind
        self.result_packet_hash = result_packet_hash
        self.stop_before_result = stop_before_result
        self.wait_statuses = list(wait_statuses)
        self.result_hermes_run_id = result_hermes_run_id
        self.launches: list[object] = []
        self.order: list[str] = []
        self.stop_calls = 0
        self.identity: ProcessIdentity | None = None

    def availability(self):
        return self._availability

    def start(self, launch):
        self.order.append("start")
        self.launches.append(launch)
        binding = ProcessBinding(
            request_id=launch.request_id,
            task_settings_hash=launch.task_settings_hash,
            project_id=launch.project_id,
            task_id=launch.worker_task_id,
            run_id=launch.run_id,
            host_id=launch.host_id,
        )
        scope_id = (
            "701"
            if self.scope_kind is ProcessScopeKind.PROCESS_GROUP
            else task_cgroup_scope_id(
                binding,
                delegated_parent_scope="/forge-dispatcher.service",
            )
        )
        self.identity = ProcessIdentity(
            binding=binding,
            platform="posix",
            pid=701,
            start_identity="boot-id:701",
            scope_kind=self.scope_kind,
            scope_id=scope_id,
            control_group_id=None,
            members=(ProcessMemberIdentity(pid=701, start_identity="boot-id:701"),),
        )
        return self.runtime.WorkerHandle(
            runtime_name=self.runtime_name,
            handle_id=f"handle:{launch.run_id}",
            root_pid=701,
            gate_closed=True,
        )

    def process_identity(self, handle):
        del handle
        self.order.append("identity")
        assert self.identity is not None
        return self.identity

    def activate(self, handle) -> None:
        del handle
        self.order.append("activate")
        if self.database is not None:
            launch = self.launches[-1]
            with self.database.read() as connection:
                row = connection.execute(
                    """
                    SELECT state, process_identity_json FROM task_runtime_runs
                    WHERE run_id = ?
                    """,
                    (launch.run_id,),
                ).fetchone()
            assert row is not None
            assert tuple(row) == ("running", self.identity.to_json())

    def wait(self, handle):
        del handle
        self.order.append("wait")
        if self.stop_before_result:
            assert self.database is not None
            launch = self.launches[-1]
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO task_events (
                        request_id, task_settings_hash, event_type, event_key,
                        event_json, occurred_at
                    ) VALUES (?, ?, 'stop_requested', 'stop:during-worker', '{}', ?)
                    """,
                    (
                        launch.request_id,
                        launch.task_settings_hash,
                        (NOW + timedelta(minutes=2))
                        .isoformat(timespec="microseconds")
                        .replace("+00:00", "Z"),
                    ),
                )
        if not self.wait_statuses:
            raise AssertionError("wait called more often than configured")
        exited, exit_code = self.wait_statuses.pop(0)
        return self.runtime.WorkerWaitStatus(exited=exited, exit_code=exit_code)

    def result(self, handle):
        del handle
        self.order.append("result")
        launch = self.launches[-1]
        return self.runtime.WorkerRuntimeResult(
            packet_hash=self.result_packet_hash or launch.prompt.packet_hash,
            task_settings_hash=launch.prompt.task_settings_hash,
            message_ids=launch.prompt.message_ids,
            acknowledgements=(),
            output_bytes=b"verified worker output",
            hermes_run_id=self.result_hermes_run_id or launch.hermes_run_id,
            hermes_claim_lock_hash=hashlib.sha256(
                launch.hermes_claim_lock.encode("utf-8")
            ).hexdigest(),
        )

    def stop(self, handle):
        del handle
        self.order.append("stop")
        self.stop_calls += 1
        assert self.identity is not None
        return self.runtime.WorkerStopReceipt(
            identity=self.identity,
            stopped=True,
            read_back_verified=True,
        )


def _verified_availability(runtime):
    return runtime.RuntimeAvailability(
        installed=True,
        authenticated=True,
        start_gate_verified=True,
        os_boundary_verified=True,
        identity_readback_verified=True,
        stop_readback_verified=True,
        result_validation_verified=True,
        implemented=True,
    )


def test_native_and_codex_adapters_receive_the_same_exact_packet_bytes(
    tmp_path: Path,
) -> None:
    runtime = _runtime_module()
    from forge.ops.task_messages import TaskMessageStore
    from forge.ops.worker_prompt import build_worker_prompt

    database, request, settings, project = _seed_active(tmp_path)
    packet = TaskMessageStore(database).build_packet(
        request.request_id,
        settings.task_settings_hash,
    )
    prompt = build_worker_prompt(packet, instructions="Run the confirmed Task.")
    launch = runtime.WorkerLaunch(
        run_id="same-packet-run",
        request_id=request.request_id,
        task_settings_hash=settings.task_settings_hash,
        project_id=project.project_id,
        host_id=HOST_ID,
        worker_task_id="worker-1",
        worktree_path=project.workspace,
        runtime_name=runtime.NATIVE_HERMES_RUNTIME,
        prompt=prompt,
        **_hermes_contract(runtime, tmp_path),
    )
    native_driver = _FakeDriver(
        runtime,
        runtime.NATIVE_HERMES_RUNTIME,
        _verified_availability(runtime),
    )
    codex_driver = _FakeDriver(
        runtime,
        runtime.CODEX_APP_SERVER_RUNTIME,
        _verified_availability(runtime),
    )
    native = runtime.NativeHermesAdapter(native_driver)
    codex = runtime.CodexAppServerAdapter(codex_driver)

    native.start(launch)
    codex.start(replace(launch, runtime_name=runtime.CODEX_APP_SERVER_RUNTIME))

    assert native_driver.launches[0].prompt.packet_bytes == packet.to_json().encode("utf-8")
    assert (
        codex_driver.launches[0].prompt.packet_bytes
        == native_driver.launches[0].prompt.packet_bytes
    )


@pytest.mark.parametrize(
    "missing_proof",
    (
        "installed",
        "authenticated",
        "start_gate_verified",
        "os_boundary_verified",
        "identity_readback_verified",
        "stop_readback_verified",
        "result_validation_verified",
    ),
)
def test_registry_hides_any_runtime_missing_one_required_proof(
    missing_proof: str,
) -> None:
    runtime = _runtime_module()
    unavailable = replace(_verified_availability(runtime), **{missing_proof: False})
    adapter = runtime.NativeHermesAdapter(
        _FakeDriver(runtime, runtime.NATIVE_HERMES_RUNTIME, unavailable)
    )
    registry = runtime.WorkerRuntimeRegistry((adapter,))

    assert registry.available_names == ()
    with pytest.raises(runtime.WorkerRuntimeUnavailable, match="verified"):
        registry.select(runtime.NATIVE_HERMES_RUNTIME)


def test_codex_never_silently_falls_back_without_exact_configured_order() -> None:
    runtime = _runtime_module()
    unavailable_codex = replace(
        _verified_availability(runtime),
        authenticated=False,
    )
    native = runtime.NativeHermesAdapter(
        _FakeDriver(
            runtime,
            runtime.NATIVE_HERMES_RUNTIME,
            _verified_availability(runtime),
        )
    )
    codex = runtime.CodexAppServerAdapter(
        _FakeDriver(runtime, runtime.CODEX_APP_SERVER_RUNTIME, unavailable_codex)
    )
    registry = runtime.WorkerRuntimeRegistry(
        (native, codex),
        fallback_order={
            runtime.CODEX_APP_SERVER_RUNTIME: (runtime.NATIVE_HERMES_RUNTIME,)
        },
    )

    with pytest.raises(runtime.WorkerRuntimeUnavailable, match="fallback"):
        registry.select(runtime.CODEX_APP_SERVER_RUNTIME)
    assert (
        registry.select(
            runtime.CODEX_APP_SERVER_RUNTIME,
            fallbacks=(runtime.NATIVE_HERMES_RUNTIME,),
        )
        is native
    )
    with pytest.raises(runtime.WorkerRuntimeUnavailable, match="order"):
        registry.select(
            runtime.CODEX_APP_SERVER_RUNTIME,
            fallbacks=(
                runtime.NATIVE_HERMES_RUNTIME,
                runtime.CODEX_APP_SERVER_RUNTIME,
            ),
        )


def test_standalone_claude_is_hidden_and_fails_closed() -> None:
    runtime = _runtime_module()
    claude = runtime.ClaudeStandaloneAdapter()
    registry = runtime.WorkerRuntimeRegistry((claude,))

    assert claude.availability().implemented is False
    assert runtime.CLAUDE_STANDALONE_RUNTIME not in registry.available_names
    with pytest.raises(runtime.WorkerRuntimeUnavailable, match="verified"):
        registry.select(runtime.CLAUDE_STANDALONE_RUNTIME)


def test_gated_start_records_exact_identity_before_prompt_activation(
    tmp_path: Path,
) -> None:
    runtime = _runtime_module()
    database, request, settings, project = _seed_active(tmp_path)
    driver = _FakeDriver(
        runtime,
        runtime.NATIVE_HERMES_RUNTIME,
        _verified_availability(runtime),
        database=database,
    )
    registry = runtime.WorkerRuntimeRegistry(
        (runtime.NativeHermesAdapter(driver),)
    )
    worktree = (tmp_path / "task-worktree").resolve()
    worktree.mkdir()
    service = runtime.WorkerRuntimeService(
        database,
        registry,
        clock=_Clock(),
        run_id_factory=lambda: "run-1",
    )

    receipt = service.run(
        request_id=request.request_id,
        task_settings_hash=settings.task_settings_hash,
        project_id=project.project_id,
        host_id=HOST_ID,
        worker_task_id="worker-1",
        worktree_path=str(worktree),
        instructions="Run the selected Project Task.",
        runtime_name=runtime.NATIVE_HERMES_RUNTIME,
        **_hermes_contract(runtime, tmp_path),
    )

    assert driver.order == ["start", "identity", "activate", "wait", "result"]
    assert receipt.run_id == "run-1"
    assert receipt.root_pid == 701
    assert receipt.process_identity == driver.identity
    assert receipt.result_hash == receipt.result.result_hash
    with database.read() as connection:
        row = connection.execute(
            """
            SELECT request_id, task_settings_hash, project_id, host_id,
                   worker_task_id, runtime_name, process_identity_json,
                   message_packet_hash, state, result_hash, ended_at
            FROM task_runtime_runs WHERE run_id = 'run-1'
            """
        ).fetchone()
    assert tuple(row[:9]) == (
        request.request_id,
        settings.task_settings_hash,
        project.project_id,
        HOST_ID,
        "worker-1",
        runtime.NATIVE_HERMES_RUNTIME,
        driver.identity.to_json(),
        driver.launches[0].prompt.packet_hash,
        "completed",
    )
    assert row[9] == receipt.result_hash
    assert row[10] is not None


def test_dispatch_spawner_returns_plain_pid_then_reaps_result_on_later_ticks(
    tmp_path: Path,
) -> None:
    runtime = _runtime_module()
    database, request, settings, project = _seed_active(tmp_path)
    driver = _FakeDriver(
        runtime,
        runtime.NATIVE_HERMES_RUNTIME,
        _verified_availability(runtime),
        database=database,
        wait_statuses=((False, None), (True, 0)),
    )
    service = runtime.WorkerRuntimeService(
        database,
        runtime.WorkerRuntimeRegistry((runtime.NativeHermesAdapter(driver),)),
        clock=_Clock(),
        run_id_factory=lambda: "dispatch-run",
    )
    spawner = runtime.ForgeWorkerSpawner(service)
    task = runtime.ForgeWorkerTask(
        request_id=request.request_id,
        task_settings_hash=settings.task_settings_hash,
        project_id=project.project_id,
        host_id=HOST_ID,
        worker_task_id="worker-card-1",
        instructions="Run only the confirmed Project Task.",
        runtime_name=runtime.NATIVE_HERMES_RUNTIME,
        **_hermes_contract(runtime, tmp_path),
    )

    pid = spawner(task, project.workspace, "forge-board")

    assert type(pid) is int
    assert pid == 701
    assert spawner.active_run_ids == ("dispatch-run",)
    assert spawner.finish_active() == ()
    assert spawner.active_run_ids == ("dispatch-run",)
    receipts = spawner.finish_active()
    assert tuple(receipt.run_id for receipt in receipts) == ("dispatch-run",)
    assert spawner.active_run_ids == ()


def test_dispatch_spawner_fails_closed_after_restart_with_an_unreconciled_run(
    tmp_path: Path,
) -> None:
    runtime = _runtime_module()
    database, request, settings, project = _seed_active(tmp_path)
    driver = _FakeDriver(
        runtime,
        runtime.NATIVE_HERMES_RUNTIME,
        _verified_availability(runtime),
        database=database,
        wait_statuses=((False, None),),
    )
    service = runtime.WorkerRuntimeService(
        database,
        runtime.WorkerRuntimeRegistry((runtime.NativeHermesAdapter(driver),)),
        clock=_Clock(),
        run_id_factory=lambda: "orphaned-run",
    )
    first = runtime.ForgeWorkerSpawner(service)
    task = runtime.ForgeWorkerTask(
        request_id=request.request_id,
        task_settings_hash=settings.task_settings_hash,
        project_id=project.project_id,
        host_id=HOST_ID,
        worker_task_id="worker-card-1",
        instructions="Run only the confirmed Project Task.",
        runtime_name=runtime.NATIVE_HERMES_RUNTIME,
        **_hermes_contract(runtime, tmp_path),
    )
    assert first(task, project.workspace, "forge-board") == 701

    restarted = runtime.ForgeWorkerSpawner(service)

    assert restarted.unreconciled_run_ids == ("orphaned-run",)
    with pytest.raises(runtime.WorkerRuntimeUnavailable, match="reconciliation"):
        restarted(task, project.workspace, "forge-board")


def test_process_group_identity_is_never_accepted_as_a_verified_boundary(
    tmp_path: Path,
) -> None:
    runtime = _runtime_module()
    database, request, settings, project = _seed_active(tmp_path)
    driver = _FakeDriver(
        runtime,
        runtime.NATIVE_HERMES_RUNTIME,
        _verified_availability(runtime),
        database=database,
        scope_kind=ProcessScopeKind.PROCESS_GROUP,
    )
    service = runtime.WorkerRuntimeService(
        database,
        runtime.WorkerRuntimeRegistry((runtime.NativeHermesAdapter(driver),)),
        clock=_Clock(),
        run_id_factory=lambda: "unsafe-run",
    )

    with pytest.raises(runtime.WorkerRuntimeError, match="cgroup|Windows Job"):
        service.run(
            request_id=request.request_id,
            task_settings_hash=settings.task_settings_hash,
            project_id=project.project_id,
            host_id=HOST_ID,
            worker_task_id="worker-1",
            worktree_path=project.workspace,
            instructions="Run the Task.",
            runtime_name=runtime.NATIVE_HERMES_RUNTIME,
            **_hermes_contract(runtime, tmp_path),
        )

    assert "activate" not in driver.order
    assert driver.stop_calls == 1


@pytest.mark.parametrize(
    "failure",
    ("result_packet", "result_hermes_run", "stop_before_result"),
)
def test_result_acceptance_rechecks_packet_and_active_stop_guard(
    tmp_path: Path,
    failure: str,
) -> None:
    runtime = _runtime_module()
    database, request, settings, project = _seed_active(tmp_path)
    driver = _FakeDriver(
        runtime,
        runtime.NATIVE_HERMES_RUNTIME,
        _verified_availability(runtime),
        database=database,
        result_packet_hash=("f" * 64 if failure == "result_packet" else None),
        result_hermes_run_id=(42 if failure == "result_hermes_run" else None),
        stop_before_result=failure == "stop_before_result",
    )
    service = runtime.WorkerRuntimeService(
        database,
        runtime.WorkerRuntimeRegistry((runtime.NativeHermesAdapter(driver),)),
        clock=_Clock(),
        run_id_factory=lambda: "rejected-result-run",
    )

    with pytest.raises(runtime.WorkerRuntimeError, match="packet|active|stop"):
        service.run(
            request_id=request.request_id,
            task_settings_hash=settings.task_settings_hash,
            project_id=project.project_id,
            host_id=HOST_ID,
            worker_task_id="worker-1",
            worktree_path=project.workspace,
            instructions="Run the Task.",
            runtime_name=runtime.NATIVE_HERMES_RUNTIME,
            **_hermes_contract(runtime, tmp_path),
        )

    assert driver.stop_calls == 1
    with database.read() as connection:
        row = connection.execute(
            """
            SELECT state, result_hash, ended_at
            FROM task_runtime_runs WHERE run_id = 'rejected-result-run'
            """
        ).fetchone()
    assert row[0] == "failed"
    assert row[1] is None
    assert row[2] is not None
