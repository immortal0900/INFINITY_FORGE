#!/usr/bin/env python3
"""Replay active Task evidence and create each missing Hermes card once."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from forge.ops.contracts import parse_step_proof, parse_task_result  # noqa: E402
from forge.ops.task_flow import (  # noqa: E402
    TaskFlowState,
    TaskStep,
    next_task_action,
    record_fix_proof,
    record_task_result,
)
from forge.ops.task_runtime import (  # noqa: E402
    GitHubTaskRuntimeClient,
    run_project_task_flow_worker,
    run_task_flow_worker,
)


def apply_completed_summary(
    state: TaskFlowState,
    summary: Mapping[str, object],
    *,
    current_commit: str,
) -> TaskFlowState:
    """Apply one strict Build, Review, Deep Check, or Fix summary."""

    step = next_task_action(state)
    if step is None:
        raise ValueError("Task flow has no current step")
    if step is TaskStep.FIX:
        return record_fix_proof(
            state,
            parse_step_proof(summary),
            current_commit=current_commit,
        )
    return record_task_result(
        state,
        parse_task_result(step.value, summary),
        current_commit=current_commit,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-port",
        action="store_true",
        help="confirm that the Task flow API port imports successfully",
    )
    parser.add_argument("--db", help="Hermes Kanban SQLite database")
    parser.add_argument("--hermes", help="Hermes executable")
    parser.add_argument("--gh", help="GitHub CLI executable")
    parser.add_argument("--settings-db", help="immutable Task settings database")
    parser.add_argument("--outbox", help="confirmed Task outbox database")
    parser.add_argument("--repo", help="GitHub repository as OWNER/REPO")
    parser.add_argument("--workspace", help="repository workspace path")
    parser.add_argument(
        "--worktree-root",
        help="root directory for v2 Project worktrees",
    )
    parser.add_argument("--dry-run", action="store_true", help="report without creating cards")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.check_port:
        print(json.dumps({"status": "ready", "writer": "task-flow-worker"}))
        return 0
    common_required = {
        "--db": args.db,
        "--hermes": args.hermes,
        "--gh": args.gh,
        "--settings-db": args.settings_db,
    }
    missing = [name for name, value in common_required.items() if not value]
    if missing:
        _parser().error(f"required arguments: {', '.join(missing)}")
    v1_values = (args.outbox, args.repo, args.workspace)
    use_v1 = any(v1_values)
    if use_v1 and not all(v1_values):
        _parser().error("--repo, --workspace, and --outbox must be provided together")
    if not use_v1 and not args.worktree_root:
        _parser().error("required arguments: --worktree-root")
    try:
        github = GitHubTaskRuntimeClient(args.gh)
        if use_v1:
            workspace = args.workspace
            if not workspace.startswith("dir:"):
                workspace = f"dir:{workspace}"
            reports = run_task_flow_worker(
                settings_db=args.settings_db,
                outbox_db=args.outbox,
                hermes_db=args.db,
                hermes_path=args.hermes,
                github=github,
                repository=args.repo,
                workspace=workspace,
                dry_run=args.dry_run,
            )
        else:
            reports = run_project_task_flow_worker(
                settings_db=args.settings_db,
                hermes_db=args.db,
                hermes_path=args.hermes,
                github=github,
                worktree_root=args.worktree_root,
                dry_run=args.dry_run,
            )
    except Exception as error:
        print(
            json.dumps({"status": "error", "error": str(error)}),
            file=sys.stderr,
        )
        return 2
    print(json.dumps({"status": "ok", "tasks": [asdict(item) for item in reports]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
