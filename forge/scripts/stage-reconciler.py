#!/usr/bin/env python3
"""Advance completed Forge stage cards exactly once."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from forge.ops.contracts import (  # noqa: E402
    ContractError,
    PipelineStage,
    PullRequestSnapshot,
    RunRecord,
    TaskRecord,
    parse_stage_result,
    transition_digest,
)
from forge.ops.github import GitHubClient  # noqa: E402
from forge.ops.hermes import (  # noqa: E402
    GateError,
    HermesCreateCommand,
    HermesStore,
    build_create_argv,
)
from forge.ops.stage_reconciler import (  # noqa: E402
    ActionKind,
    PipelineSnapshot,
    build_stage_card_spec,
    decide_next_action,
    validate_stage_child_transition,
)


_ROOT_KEY_RE = re.compile(
    r"^github-issue:(?P<repository>[^/#:\s]+/[^/#:\s]+)#"
    r"(?P<issue>[1-9][0-9]*)$"
)
_STAGE_KEY_RE = re.compile(
    r"^forge-stage:(?P<repository>[^/#:\s]+/[^/#:\s]+)#"
    r"(?P<issue>[1-9][0-9]*):"
    r"(?P<stage>reviewer|critic|executor-rework):"
    r"(?P<digest>[0-9a-f]{16})$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DEFAULT_WORKSPACE = f"dir:{Path('~/work/INFINITY_FORGE').expanduser()}"


class PipelineStore(Protocol):
    ignored_legacy_count: int
    topology_blocked_parent_ids: frozenset[str]

    def list_pipeline_tasks(self) -> Sequence[TaskRecord]: ...

    def latest_completed_run(self, task_id: str) -> RunRecord: ...

    def has_idempotency_key(self, key: str) -> bool: ...


class PullRequestReader(Protocol):
    def get_pr_snapshot(
        self, pr_url: str, required_check_names: Sequence[str]
    ) -> PullRequestSnapshot: ...


class CreateCommand(Protocol):
    def __call__(self, argv: Sequence[str]) -> None: ...


@dataclass(frozen=True)
class ReconcileConfig:
    repository: str
    required_check: str = "eval"
    max_reworks: int = 3
    workspace: str = _DEFAULT_WORKSPACE
    dry_run: bool = False

    def __post_init__(self) -> None:
        if re.fullmatch(r"[^/#:\s]+/[^/#:\s]+", self.repository) is None:
            raise ValueError("repository must have OWNER/REPO format")
        if not isinstance(self.required_check, str) or not self.required_check.strip():
            raise ValueError("required_check must be non-empty")
        if (
            not isinstance(self.workspace, str)
            or not self.workspace.startswith("dir:")
            or not self.workspace.removeprefix("dir:").strip()
        ):
            raise ValueError("workspace must use a non-empty dir: path")
        if (
            not isinstance(self.max_reworks, int)
            or isinstance(self.max_reworks, bool)
            or not 1 <= self.max_reworks <= 3
        ):
            raise ValueError("max_reworks must be from 1 through 3")


@dataclass(frozen=True)
class PipelineIdentity:
    repository: str
    issue_number: int
    stage: PipelineStage
    receipt_prefix: str | None = None


@dataclass
class ReconcileReport:
    scanned: int = 0
    ignored_legacy: int = 0
    created: int = 0
    planned: int = 0
    skipped: int = 0
    terminal: int = 0
    errors: list[str] = field(default_factory=list)
    events: list[dict[str, object]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "scanned": self.scanned,
            "ignored_legacy": self.ignored_legacy,
            "created": self.created,
            "planned": self.planned,
            "skipped": self.skipped,
            "terminal": self.terminal,
            "errors": list(self.errors),
            "events": list(self.events),
        }


def _parse_identity(task: TaskRecord) -> PipelineIdentity:
    key = task.idempotency_key
    if not isinstance(key, str):
        raise GateError(f"pipeline task {task.task_id} has no identity key")
    root_match = _ROOT_KEY_RE.fullmatch(key)
    if root_match is not None:
        return PipelineIdentity(
            repository=root_match.group("repository"),
            issue_number=int(root_match.group("issue")),
            stage=PipelineStage.EXECUTOR,
        )
    stage_match = _STAGE_KEY_RE.fullmatch(key)
    if stage_match is None:
        raise GateError(f"pipeline task {task.task_id} has malformed identity key")
    return PipelineIdentity(
        repository=stage_match.group("repository"),
        issue_number=int(stage_match.group("issue")),
        stage=PipelineStage(stage_match.group("stage")),
        receipt_prefix=stage_match.group("digest"),
    )


def _parse_card_receipt(task: TaskRecord) -> dict[str, object]:
    body = task.body
    if not isinstance(body, str):
        raise GateError(f"stage task {task.task_id} has no receipt body")
    lines = body.splitlines()
    if len(lines) != 3 or lines[0] != "```json" or lines[2] != "```":
        raise GateError(f"stage task {task.task_id} has malformed receipt body")
    try:
        payload = json.loads(lines[1])
    except json.JSONDecodeError as error:
        raise GateError(
            f"stage task {task.task_id} receipt is not valid JSON"
        ) from error
    required = {
        "bound_head_sha",
        "pr_url",
        "reflection",
        "source_digest",
        "source_run_id",
        "source_task_id",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise GateError(f"stage task {task.task_id} receipt fields are invalid")
    if (
        not isinstance(payload["source_digest"], str)
        or _SHA256_RE.fullmatch(payload["source_digest"]) is None
    ):
        raise GateError(f"stage task {task.task_id} source digest is invalid")
    if (
        not isinstance(payload["bound_head_sha"], str)
        or _GIT_SHA_RE.fullmatch(payload["bound_head_sha"]) is None
    ):
        raise GateError(f"stage task {task.task_id} bound HEAD is invalid")
    if not isinstance(payload["pr_url"], str) or not payload["pr_url"].strip():
        raise GateError(f"stage task {task.task_id} PR URL is invalid")
    if not isinstance(payload["source_task_id"], str) or not payload[
        "source_task_id"
    ].strip():
        raise GateError(f"stage task {task.task_id} source task is invalid")
    if not isinstance(payload["source_run_id"], int) or isinstance(
        payload["source_run_id"], bool
    ):
        raise GateError(f"stage task {task.task_id} source run is invalid")
    reflection = payload["reflection"]
    if reflection is not None and (
        not isinstance(reflection, str) or not reflection.strip()
    ):
        raise GateError(f"stage task {task.task_id} reflection is invalid")
    return payload


def _event(
    report: ReconcileReport,
    task: TaskRecord,
    action: str,
    detail: str,
) -> None:
    report.events.append(
        {"task_id": task.task_id, "action": action, "detail": detail}
    )


def _error(report: ReconcileReport, task_id: str, reason: str) -> None:
    message = f"{task_id}: {reason}"
    report.errors.append(message)
    report.events.append(
        {"task_id": task_id, "action": "gate-error", "detail": reason}
    )


def _validate_pipeline_graph(
    tasks: Sequence[TaskRecord],
    identities: dict[str, PipelineIdentity],
    topology_blocked_parent_ids: frozenset[str],
) -> None:
    task_by_id = {task.task_id: task for task in tasks}
    if len(task_by_id) != len(tasks):
        raise GateError("duplicate pipeline task id")
    blocked = sorted(topology_blocked_parent_ids.intersection(task_by_id))
    if blocked:
        raise GateError(
            f"canonical parent {blocked[0]} has an ignored legacy child"
        )
    roots: dict[tuple[str, int], int] = {}
    allowed_children = {
        PipelineStage.EXECUTOR: {
            PipelineStage.REVIEWER,
            PipelineStage.EXECUTOR_REWORK,
        },
        PipelineStage.EXECUTOR_REWORK: {
            PipelineStage.REVIEWER,
            PipelineStage.EXECUTOR_REWORK,
        },
        PipelineStage.REVIEWER: {
            PipelineStage.CRITIC,
            PipelineStage.EXECUTOR_REWORK,
        },
        PipelineStage.CRITIC: {
            PipelineStage.REVIEWER,
            PipelineStage.EXECUTOR_REWORK,
        },
    }
    for task in tasks:
        identity = identities[task.task_id]
        pipeline = (identity.repository, identity.issue_number)
        if identity.stage is PipelineStage.EXECUTOR:
            roots[pipeline] = roots.get(pipeline, 0) + 1
            if task.parent_id is not None:
                raise GateError(f"root task {task.task_id} must not have a parent")
            continue
        if task.parent_id is None or task.parent_id not in task_by_id:
            raise GateError(f"stage task {task.task_id} has no pipeline parent")
        parent_identity = identities[task.parent_id]
        if (
            parent_identity.repository != identity.repository
            or parent_identity.issue_number != identity.issue_number
        ):
            raise GateError(f"stage task {task.task_id} crosses pipeline identity")
        if identity.stage not in allowed_children[parent_identity.stage]:
            raise GateError(
                f"stage transition {parent_identity.stage.value} -> "
                f"{identity.stage.value} is not allowed"
            )
        receipt = _parse_card_receipt(task)
        if receipt["source_task_id"] != task.parent_id:
            raise GateError(f"stage task {task.task_id} receipt parent does not match")
        if identity.receipt_prefix != str(receipt["source_digest"])[:16]:
            raise GateError(f"stage task {task.task_id} receipt key does not match")
    for pipeline, count in roots.items():
        if count != 1:
            raise GateError(
                f"pipeline {pipeline[0]}#{pipeline[1]} has {count} root tasks"
            )
    referenced_pipelines = {
        (identity.repository, identity.issue_number) for identity in identities.values()
    }
    missing_roots = referenced_pipelines - set(roots)
    if missing_roots:
        repository, issue = sorted(missing_roots)[0]
        raise GateError(f"pipeline {repository}#{issue} has no root task")
    for task in tasks:
        seen: set[str] = set()
        cursor = task
        while cursor.parent_id is not None:
            if cursor.task_id in seen:
                raise GateError(f"pipeline graph contains a cycle at {cursor.task_id}")
            seen.add(cursor.task_id)
            cursor = task_by_id[cursor.parent_id]
        if identities[cursor.task_id].stage is not PipelineStage.EXECUTOR:
            raise GateError(f"stage task {task.task_id} is disconnected from its root")
    parent_ids = {task.parent_id for task in tasks if task.parent_id is not None}
    leaves_by_pipeline: dict[tuple[str, int], int] = {}
    for task in tasks:
        if task.task_id in parent_ids:
            continue
        identity = identities[task.task_id]
        pipeline = (identity.repository, identity.issue_number)
        leaves_by_pipeline[pipeline] = leaves_by_pipeline.get(pipeline, 0) + 1
    ambiguous = [pipeline for pipeline, count in leaves_by_pipeline.items() if count != 1]
    if ambiguous:
        repository, issue = sorted(ambiguous)[0]
        raise GateError(f"multiple leaves for {repository}#{issue}")


def _validate_stage_receipts(
    tasks: Sequence[TaskRecord],
    identities: dict[str, PipelineIdentity],
    store: PipelineStore,
    required_check_name: str,
) -> None:
    task_by_id = {task.task_id: task for task in tasks}
    parent_runs: dict[str, RunRecord] = {}
    for task in tasks:
        identity = identities[task.task_id]
        if identity.stage is PipelineStage.EXECUTOR:
            continue
        receipt = _parse_card_receipt(task)
        parent_id = task.parent_id
        if parent_id is None:
            raise GateError(f"stage task {task.task_id} has no pipeline parent")
        parent = task_by_id[parent_id]
        if parent.status != "done":
            raise GateError(
                f"stage task {task.task_id} parent is not completed"
            )
        if parent_id not in parent_runs:
            parent_runs[parent_id] = store.latest_completed_run(parent_id)
        parent_run = parent_runs[parent_id]
        if (
            parent_run.task_id != parent_id
            or parent_run.status != "completed"
            or parent_run.outcome != "success"
        ):
            raise GateError(
                f"stage task {task.task_id} parent run is not completed with success"
            )
        if receipt["source_run_id"] != parent_run.run_id:
            raise GateError(
                f"stage task {task.task_id} parent run receipt id does not match"
            )
        parent_identity = identities[parent_id]
        parent_result = parse_stage_result(
            parent_identity.stage,
            parent_run.summary,
            parent_run.metadata,
        )
        validate_stage_child_transition(
            parent_stage=parent_identity.stage,
            parent_result=parent_result,
            child_stage=identity.stage,
            pr_url=str(receipt["pr_url"]),
            bound_head_sha=str(receipt["bound_head_sha"]),
            reflection=receipt["reflection"],
            required_check_name=required_check_name,
        )
        expected_digest = transition_digest(
            task_id=parent_id,
            run_id=parent_run.run_id,
            stage=parent_identity.stage,
            summary=parent_run.summary,
            metadata=parent_run.metadata,
            pr_url=str(receipt["pr_url"]),
            head_sha=str(receipt["bound_head_sha"]),
        )
        if receipt["source_digest"] != expected_digest:
            raise GateError(
                f"stage task {task.task_id} parent run receipt digest does not match"
            )


def reconcile_once(
    store: PipelineStore,
    github: PullRequestReader,
    create: CreateCommand,
    config: ReconcileConfig,
) -> ReconcileReport:
    """Evaluate every completed pipeline leaf and perform at most one transition."""

    report = ReconcileReport()
    try:
        all_tasks = tuple(store.list_pipeline_tasks())
        report.ignored_legacy = int(getattr(store, "ignored_legacy_count", 0))
        parsed = {task.task_id: _parse_identity(task) for task in all_tasks}
        raw_blockers = getattr(store, "topology_blocked_parent_ids", frozenset())
        if not isinstance(raw_blockers, (set, frozenset)) or any(
            not isinstance(task_id, str) or not task_id.strip()
            for task_id in raw_blockers
        ):
            raise GateError("legacy topology blockers are malformed")
        topology_blockers = frozenset(raw_blockers)
        _validate_pipeline_graph(all_tasks, parsed, topology_blockers)
        _validate_stage_receipts(
            all_tasks,
            parsed,
            store,
            config.required_check,
        )
        tasks = tuple(
            task
            for task in all_tasks
            if parsed[task.task_id].repository == config.repository
        )
        identities = {task.task_id: parsed[task.task_id] for task in tasks}
    except (GateError, ContractError, ValueError, KeyError) as error:
        _error(report, "pipeline", str(error))
        return report

    report.scanned = len(tasks)
    parent_ids = {task.parent_id for task in tasks if task.parent_id is not None}
    leaves = [task for task in tasks if task.task_id not in parent_ids]
    leaves_by_pipeline: dict[tuple[str, int], list[TaskRecord]] = {}
    for task in leaves:
        identity = identities[task.task_id]
        leaves_by_pipeline.setdefault(
            (identity.repository, identity.issue_number), []
        ).append(task)
    ambiguous = [key for key, values in leaves_by_pipeline.items() if len(values) != 1]
    if ambiguous:
        repository, issue = sorted(ambiguous)[0]
        _error(report, "pipeline", f"multiple leaves for {repository}#{issue}")
        return report

    rework_counts: dict[tuple[str, int], int] = {}
    for identity in identities.values():
        if identity.stage is PipelineStage.EXECUTOR_REWORK:
            pipeline = (identity.repository, identity.issue_number)
            rework_counts[pipeline] = rework_counts.get(pipeline, 0) + 1

    for task in sorted(leaves, key=lambda item: item.task_id):
        identity = identities[task.task_id]
        if task.status != "done":
            report.skipped += 1
            _event(report, task, "wait", f"task status is {task.status}")
            continue
        try:
            run = store.latest_completed_run(task.task_id)
            if run.status != "completed" or run.outcome != "success":
                raise GateError("source run is not completed with success")
            result = parse_stage_result(identity.stage, run.summary, run.metadata)
            pr = github.get_pr_snapshot(
                result.pr_url,
                (config.required_check,),
            )
            if pr.repository != identity.repository:
                raise GateError("PR repository does not match pipeline identity")
            source_digest = transition_digest(
                task_id=task.task_id,
                run_id=run.run_id,
                stage=identity.stage,
                summary=run.summary,
                metadata=run.metadata,
                pr_url=pr.pr_url,
                head_sha=pr.head_sha,
            )
            bound_source_digest = None
            bound_pr_url = None
            bound_head_sha = None
            if identity.stage is not PipelineStage.EXECUTOR:
                receipt = _parse_card_receipt(task)
                bound_source_digest = str(receipt["source_digest"])
                bound_pr_url = str(receipt["pr_url"])
                bound_head_sha = str(receipt["bound_head_sha"])
            snapshot = PipelineSnapshot(
                stage=identity.stage,
                issue_number=identity.issue_number,
                source_task=task,
                source_run=run,
                result=result,
                source_digest=source_digest,
                pull_request=pr,
                bound_source_digest=bound_source_digest,
                bound_pr_url=bound_pr_url,
                bound_head_sha=bound_head_sha,
                rework_count=rework_counts.get(
                    (identity.repository, identity.issue_number), 0
                ),
                max_reworks=config.max_reworks,
                required_check_name=config.required_check,
            )
            action = decide_next_action(snapshot)
            if action.kind in {
                ActionKind.CREATE_REVIEWER,
                ActionKind.CREATE_FRESH_REVIEWER,
                ActionKind.CREATE_CRITIC,
                ActionKind.CREATE_REWORK,
            }:
                spec = build_stage_card_spec(snapshot, action)
                if store.has_idempotency_key(spec.idempotency_key):
                    report.skipped += 1
                    _event(report, task, "exists", spec.idempotency_key)
                    continue
                if config.dry_run:
                    report.planned += 1
                    _event(report, task, "planned", spec.idempotency_key)
                else:
                    create(build_create_argv(spec, config.workspace))
                    report.created += 1
                    _event(report, task, action.kind.value, spec.idempotency_key)
                continue
            if action.kind in {ActionKind.MARK_MERGEABLE, ActionKind.MARK_FAILED}:
                report.terminal += 1
                _event(report, task, action.kind.value, action.reason)
                continue
            if action.kind is ActionKind.WAIT:
                report.skipped += 1
                _event(report, task, "wait", action.reason)
                continue
            _error(report, task.task_id, action.reason or action.kind.value)
        except (GateError, ContractError, ValueError, KeyError) as error:
            _error(report, task.task_id, str(error))
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="~/.hermes/kanban.db")
    parser.add_argument("--hermes", default="~/.local/bin/hermes")
    parser.add_argument("--gh", default="/usr/bin/gh")
    parser.add_argument("--repo", default="immortal0900/INFINITY_FORGE")
    parser.add_argument("--required-check", default="eval")
    parser.add_argument("--max-reworks", type=int, default=3)
    parser.add_argument("--workspace", default=_DEFAULT_WORKSPACE)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def build_runtime(
    args: argparse.Namespace,
) -> tuple[HermesStore, GitHubClient, HermesCreateCommand]:
    return (
        HermesStore(args.db),
        GitHubClient(args.gh),
        HermesCreateCommand(args.hermes),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = ReconcileConfig(
            repository=args.repo,
            required_check=args.required_check,
            max_reworks=args.max_reworks,
            workspace=args.workspace,
            dry_run=args.dry_run,
        )
        store, github, create = build_runtime(args)
        report = reconcile_once(store, github, create, config)
    except (GateError, ContractError, ValueError) as error:
        report = ReconcileReport(errors=[f"runtime: {error}"])
    except Exception:
        report = ReconcileReport(errors=["runtime: unexpected internal error"])
    print(
        json.dumps(
            report.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0 if report.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
