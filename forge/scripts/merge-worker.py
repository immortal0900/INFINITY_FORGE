#!/usr/bin/env python3
"""Read completed Forge Tasks and safely merge their exact pull-request commit."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from forge.ops.github import GitHubClient  # noqa: E402
from forge.ops.github_merge import BranchRefreshResult, GitHubMergeClient  # noqa: E402
from forge.ops.merge_runtime import (  # noqa: E402
    BranchRefreshRecorder,
    MergeEvidenceReader,
    MergeRunReport,
    MergeWriter,
    TaskFlowSnapshotLike,
    run_merge_tasks,
)
from forge.ops.task_settings import TaskSettingsStore  # noqa: E402


AUTO_MERGE_ENABLED_DEFAULT = False


class AutoMergeDisabledError(RuntimeError):
    """Raised before a GitHub write while automatic merging is disabled."""


def require_auto_merge_enabled(environment: Mapping[str, str]) -> None:
    """Require one exact lower-case value; missing always means disabled."""

    if environment.get("AUTO_MERGE_ENABLED") != "true":
        raise AutoMergeDisabledError("automatic merge is disabled")


class LiveMergeRuntime:
    def __init__(
        self,
        *,
        snapshots: tuple[TaskFlowSnapshotLike, ...],
        evidence_reader: MergeEvidenceReader,
        merge_writer: MergeWriter,
        settings_store: TaskSettingsStore,
        flow_updates: BranchRefreshRecorder,
        required_check: str,
        environment: Mapping[str, str],
        clock: Callable[[], datetime],
    ) -> None:
        self.snapshots = snapshots
        self.evidence_reader = evidence_reader
        self.merge_writer = merge_writer
        self.settings_store = settings_store
        self.flow_updates = flow_updates
        self.required_check = required_check
        self.environment = environment
        self.clock = clock

    def run_once(self) -> MergeRunReport:
        return run_merge_tasks(
            self.snapshots,
            evidence_reader=self.evidence_reader,
            merge_writer=self.merge_writer,
            settings_store=self.settings_store,
            flow_updates=self.flow_updates,
            required_check=self.required_check,
            environment=self.environment,
            clock=self.clock,
        )


class _LiveBranchRefreshRecorder:
    """Adapt the public two-argument Task runtime callback to the protocol."""

    def __init__(
        self,
        record: Callable[[TaskFlowSnapshotLike, BranchRefreshResult], object],
    ) -> None:
        self._record = record

    def record_branch_refresh(
        self,
        snapshot: TaskFlowSnapshotLike,
        result: BranchRefreshResult,
    ) -> object:
        return self._record(snapshot, result)


def build_runtime(args: argparse.Namespace) -> LiveMergeRuntime:
    """Late-bind the shared Task loader so importing this script has no writes."""

    # The Task runtime is developed independently and intentionally imported
    # here: importing merge-worker must remain safe for health and policy tests.
    from forge.ops.task_runtime import (  # noqa: PLC0415
        GitHubTaskRuntimeClient,
        build_branch_refresh_recorder,
        load_ready_to_merge_snapshots,
    )

    github = GitHubClient(args.gh)
    snapshots = load_ready_to_merge_snapshots(
        settings_db=args.settings_db,
        outbox_db=args.outbox,
        hermes_db=args.hermes_db,
        github=GitHubTaskRuntimeClient(args.gh),
        repository=args.repo,
    )
    if not isinstance(snapshots, tuple):
        raise TypeError("Task flow snapshot loader must return a tuple")
    updates = _LiveBranchRefreshRecorder(
        build_branch_refresh_recorder(
            hermes_db=args.hermes_db,
            hermes_path=args.hermes,
            workspace=args.workspace,
        )
    )
    return LiveMergeRuntime(
        snapshots=snapshots,
        evidence_reader=github,
        merge_writer=GitHubMergeClient(args.gh),
        settings_store=TaskSettingsStore(args.settings_db),
        flow_updates=updates,
        required_check=args.required_check,
        environment=os.environ,
        clock=lambda: datetime.now(UTC),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings-db", required=True, type=Path)
    parser.add_argument("--outbox", required=True, type=Path)
    parser.add_argument("--hermes-db", required=True, type=Path)
    parser.add_argument("--gh", default="/usr/bin/gh")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--required-check", default="eval")
    parser.add_argument("--hermes", default="/usr/local/bin/hermes")
    parser.add_argument(
        "--workspace",
        default=f"dir:{REPOSITORY_ROOT}",
        help="Hermes dir: workspace used only after a branch refresh",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runtime_builder: Callable[[argparse.Namespace], Any] = build_runtime,
) -> int:
    args = _parser().parse_args(argv)
    try:
        report = runtime_builder(args).run_once()
        if not isinstance(report, MergeRunReport):
            raise TypeError("merge runtime must return a MergeRunReport")
        payload = report.to_dict()
        exit_code = 0 if report.ok else 2
    except Exception as error:
        payload = {
            "ok": False,
            "tasks": [],
            "error": str(error) or "unexpected merge worker error",
        }
        exit_code = 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
