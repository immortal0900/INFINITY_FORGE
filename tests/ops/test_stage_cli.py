from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Sequence

from forge.ops.contracts import (
    CheckRun,
    PipelineStage,
    PullRequestSnapshot,
    RunRecord,
    TaskRecord,
    transition_digest,
)


REPOSITORY = "owner/repo"
ISSUE_NUMBER = 7
PR_NUMBER = 17
PR_URL = f"https://github.com/{REPOSITORY}/pull/{PR_NUMBER}"
HEAD_SHA = "a" * 40
BOUND_DIGEST = "b" * 64


def _load_cli() -> ModuleType:
    path = Path(__file__).parents[2] / "forge" / "scripts" / "stage-reconciler.py"
    spec = importlib.util.spec_from_file_location("forge_stage_cli", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _executor_summary() -> dict[str, object]:
    return {
        "pr_url": PR_URL,
        "changed_files": ["forge/ops/hermes.py"],
        "implemented": ["AC1"],
        "not_implemented": [],
        "verified_by": {"AC1": "tests/ops/test_stage_cli.py"},
    }


def _pr_snapshot() -> PullRequestSnapshot:
    return PullRequestSnapshot(
        pr_url=PR_URL,
        repository=REPOSITORY,
        pr_number=PR_NUMBER,
        head_sha=HEAD_SHA,
        is_open=True,
        is_draft=False,
        checks=(
            CheckRun(
                name="eval",
                status="completed",
                conclusion="success",
                head_sha=HEAD_SHA,
            ),
        ),
    )


class _Store:
    def __init__(self, tasks: list[TaskRecord], runs: dict[str, RunRecord]) -> None:
        self.tasks = tasks
        self.runs = runs
        self.lookups: list[str] = []

    def list_pipeline_tasks(self) -> Sequence[TaskRecord]:
        return tuple(self.tasks)

    def latest_completed_run(self, task_id: str) -> RunRecord:
        return self.runs[task_id]

    def has_idempotency_key(self, key: str) -> bool:
        self.lookups.append(key)
        return any(task.idempotency_key == key for task in self.tasks)


class _GitHub:
    def get_pr_snapshot(
        self, pr_url: str, required_check_names: Sequence[str]
    ) -> PullRequestSnapshot:
        assert pr_url == PR_URL
        assert tuple(required_check_names) == ("eval",)
        return _pr_snapshot()


class _Create:
    def __init__(self, store: _Store) -> None:
        self.store = store
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str]) -> None:
        call = tuple(argv)
        self.calls.append(call)
        key = call[call.index("--idempotency-key") + 1]
        parent = call[call.index("--parent") + 1]
        self.store.tasks.append(
            TaskRecord(
                task_id="child",
                title=call[2],
                status="todo",
                parent_id=parent,
                idempotency_key=key,
            )
        )


class _UnexpectedGitHub:
    def get_pr_snapshot(self, pr_url, required_check_names):
        raise AssertionError("completed ancestor must not be re-evaluated")


def test_same_executor_receipt_creates_reviewer_only_once() -> None:
    cli = _load_cli()
    task = TaskRecord(
        task_id="root",
        title="root executor",
        status="done",
        idempotency_key=f"github-issue:{REPOSITORY}#{ISSUE_NUMBER}",
    )
    run = RunRecord(
        run_id=12,
        task_id="root",
        status="completed",
        outcome="success",
        summary=_executor_summary(),
        metadata={"worker_session_id": "executor-session"},
    )
    store = _Store([task], {"root": run})
    create = _Create(store)
    config = cli.ReconcileConfig(repository=REPOSITORY)

    first = cli.reconcile_once(store, _GitHub(), create, config)
    second = cli.reconcile_once(store, _UnexpectedGitHub(), create, config)

    expected_digest = transition_digest(
        task_id="root",
        run_id=12,
        stage=PipelineStage.EXECUTOR,
        summary=run.summary,
        metadata=run.metadata,
        pr_url=PR_URL,
        head_sha=HEAD_SHA,
    )
    assert first.created == 1
    assert second.created == 0
    assert len(create.calls) == 1
    created_key = create.calls[0][create.calls[0].index("--idempotency-key") + 1]
    assert created_key == (
        f"forge-stage:{REPOSITORY}#{ISSUE_NUMBER}:reviewer:"
        f"{expected_digest[:16]}"
    )
    assert f"#{PR_NUMBER}:reviewer:" not in created_key
    assert store.lookups == [created_key]


def test_reviewer_binding_digest_is_separate_from_current_run_digest() -> None:
    cli = _load_cli()
    body = json.dumps(
        {
            "bound_head_sha": HEAD_SHA,
            "pr_url": PR_URL,
            "reflection": None,
            "source_digest": BOUND_DIGEST,
            "source_run_id": 12,
            "source_task_id": "root",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    task = TaskRecord(
        task_id="reviewer",
        title="reviewer",
        status="done",
        body=f"```json\n{body}\n```",
        parent_id="root",
        idempotency_key=(
            f"forge-stage:{REPOSITORY}#{ISSUE_NUMBER}:reviewer:"
            f"{BOUND_DIGEST[:16]}"
        ),
    )
    summary = {
        "schema_version": "forge-reviewer-result/v1",
        "verdict": "approve",
        "source_digest": BOUND_DIGEST,
        "pr_url": PR_URL,
        "head_sha": HEAD_SHA,
        "delta_check": {"implemented_verified": ["AC1"], "discrepancies": []},
        "spec_check": {"met": ["AC1"], "unmet": []},
    }
    run = RunRecord(
        run_id=13,
        task_id="reviewer",
        status="completed",
        outcome="success",
        summary=summary,
        metadata={"worker_session_id": "reviewer-session"},
    )
    root = TaskRecord(
        task_id="root",
        title="root executor",
        status="done",
        idempotency_key=f"github-issue:{REPOSITORY}#{ISSUE_NUMBER}",
    )
    store = _Store([root, task], {"reviewer": run})
    create = _Create(store)

    report = cli.reconcile_once(
        store,
        _GitHub(),
        create,
        cli.ReconcileConfig(repository=REPOSITORY),
    )

    current_digest = transition_digest(
        task_id="reviewer",
        run_id=13,
        stage=PipelineStage.REVIEWER,
        summary=summary,
        metadata=run.metadata,
        pr_url=PR_URL,
        head_sha=HEAD_SHA,
    )
    assert report.created == 1
    call = create.calls[0]
    key = call[call.index("--idempotency-key") + 1]
    assert key.endswith(f":critic:{current_digest[:16]}")
    card_payload = json.loads(call[call.index("--body") + 1].splitlines()[1])
    assert card_payload["source_digest"] == current_digest
    assert card_payload["source_digest"] != BOUND_DIGEST


def test_executor_rework_is_bound_to_its_parent_receipt() -> None:
    cli = _load_cli()
    root = TaskRecord(
        task_id="root",
        title="root",
        status="done",
        idempotency_key=f"github-issue:{REPOSITORY}#{ISSUE_NUMBER}",
    )
    reviewer_digest = "c" * 64
    reviewer = TaskRecord(
        task_id="reviewer",
        title="reviewer",
        status="done",
        body=_receipt_body(source_task_id="root", source_digest=reviewer_digest),
        parent_id="root",
        idempotency_key=(
            f"forge-stage:{REPOSITORY}#{ISSUE_NUMBER}:reviewer:"
            f"{reviewer_digest[:16]}"
        ),
    )
    rework_digest = "d" * 64
    rework = TaskRecord(
        task_id="rework",
        title="rework",
        status="done",
        body=_receipt_body(source_task_id="reviewer", source_digest=rework_digest),
        parent_id="reviewer",
        idempotency_key=(
            f"forge-stage:{REPOSITORY}#{ISSUE_NUMBER}:executor-rework:"
            f"{rework_digest[:16]}"
        ),
    )
    run = RunRecord(
        run_id=14,
        task_id="rework",
        status="completed",
        outcome="success",
        summary=_executor_summary(),
        metadata={"worker_session_id": "rework-session"},
    )
    store = _Store([root, reviewer, rework], {"rework": run})

    report = cli.reconcile_once(
        store,
        _GitHub(),
        _Create(store),
        cli.ReconcileConfig(repository=REPOSITORY),
    )

    assert report.ok is True
    assert report.created == 1
    assert report.events[0]["action"] == "create-reviewer"


def test_pending_pipeline_returns_exit_zero_wait_report(capsys) -> None:
    cli = _load_cli()
    task = TaskRecord(
        task_id="root",
        title="root executor",
        status="done",
        idempotency_key=f"github-issue:{REPOSITORY}#{ISSUE_NUMBER}",
    )
    run = RunRecord(
        run_id=12,
        task_id="root",
        status="completed",
        outcome="success",
        summary=_executor_summary(),
        metadata={},
    )
    store = _Store([task], {"root": run})

    class PendingGitHub(_GitHub):
        def get_pr_snapshot(self, pr_url, required_check_names):
            snapshot = super().get_pr_snapshot(pr_url, required_check_names)
            pending = replace(
                snapshot.checks[0], status="in_progress", conclusion=None
            )
            return replace(snapshot, checks=(pending,))

    cli.build_runtime = lambda args: (store, PendingGitHub(), _Create(store))

    exit_code = cli.main(["--repo", REPOSITORY])

    output = capsys.readouterr()
    report = json.loads(output.out)
    assert exit_code == 0
    assert report["ok"] is True
    assert report["errors"] == []
    assert report["skipped"] == 1
    assert report["events"][0]["action"] == "wait"
    assert output.out.count("\n") == 1
    assert output.err == ""


def test_malformed_external_evidence_returns_exit_two_json_report(capsys) -> None:
    cli = _load_cli()
    task = TaskRecord(
        task_id="root",
        title="root executor",
        status="done",
        idempotency_key=f"github-issue:{REPOSITORY}#{ISSUE_NUMBER}",
    )
    run = RunRecord(
        run_id=12,
        task_id="root",
        status="completed",
        outcome="success",
        summary=_executor_summary(),
        metadata={},
    )
    store = _Store([task], {"root": run})

    class BrokenGitHub:
        def get_pr_snapshot(self, pr_url, required_check_names):
            raise cli.GateError("check-runs response is malformed")

    cli.build_runtime = lambda args: (store, BrokenGitHub(), _Create(store))

    exit_code = cli.main(["--repo", REPOSITORY])

    output = capsys.readouterr()
    report = json.loads(output.out)
    assert exit_code == 2
    assert report["ok"] is False
    assert report["errors"]
    assert output.out.count("\n") == 1
    assert output.err == ""


def _receipt_body(*, source_task_id: str, source_digest: str) -> str:
    payload = {
        "bound_head_sha": HEAD_SHA,
        "pr_url": PR_URL,
        "reflection": None,
        "source_digest": source_digest,
        "source_run_id": 12,
        "source_task_id": source_task_id,
    }
    return f"```json\n{json.dumps(payload, sort_keys=True, separators=(',', ':'))}\n```"


def test_pipeline_graph_rejects_disallowed_root_to_critic_edge() -> None:
    cli = _load_cli()
    root = TaskRecord(
        task_id="root",
        title="root",
        status="done",
        idempotency_key=f"github-issue:{REPOSITORY}#{ISSUE_NUMBER}",
    )
    critic = TaskRecord(
        task_id="critic",
        title="critic",
        status="todo",
        body=_receipt_body(source_task_id="root", source_digest=BOUND_DIGEST),
        parent_id="root",
        idempotency_key=(
            f"forge-stage:{REPOSITORY}#{ISSUE_NUMBER}:critic:"
            f"{BOUND_DIGEST[:16]}"
        ),
    )
    store = _Store([root, critic], {})

    report = cli.reconcile_once(
        store,
        _UnexpectedGitHub(),
        _Create(store),
        cli.ReconcileConfig(repository=REPOSITORY),
    )

    assert report.ok is False
    assert "stage transition" in report.errors[0]


def test_pipeline_graph_rejects_disconnected_stage_cycle() -> None:
    cli = _load_cli()
    root = TaskRecord(
        task_id="root",
        title="root",
        status="todo",
        idempotency_key=f"github-issue:{REPOSITORY}#{ISSUE_NUMBER}",
    )
    reviewer_digest = "c" * 64
    rework_digest = "d" * 64
    reviewer = TaskRecord(
        task_id="reviewer",
        title="reviewer",
        status="todo",
        body=_receipt_body(
            source_task_id="rework", source_digest=reviewer_digest
        ),
        parent_id="rework",
        idempotency_key=(
            f"forge-stage:{REPOSITORY}#{ISSUE_NUMBER}:reviewer:"
            f"{reviewer_digest[:16]}"
        ),
    )
    rework = TaskRecord(
        task_id="rework",
        title="rework",
        status="todo",
        body=_receipt_body(source_task_id="reviewer", source_digest=rework_digest),
        parent_id="reviewer",
        idempotency_key=(
            f"forge-stage:{REPOSITORY}#{ISSUE_NUMBER}:executor-rework:"
            f"{rework_digest[:16]}"
        ),
    )
    store = _Store([root, reviewer, rework], {})

    report = cli.reconcile_once(
        store,
        _UnexpectedGitHub(),
        _Create(store),
        cli.ReconcileConfig(repository=REPOSITORY),
    )

    assert report.ok is False
    assert "cycle" in report.errors[0]
