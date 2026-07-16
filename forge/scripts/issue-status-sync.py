#!/usr/bin/env python3
"""Synchronize each active Task issue to one official Forge status label."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from forge.ops.displayed_status import displayed_label  # noqa: E402
from forge.ops.issue_status import GitHubIssueStatusClient  # noqa: E402
from forge.ops.task_flow import TaskFlowState  # noqa: E402
from forge.ops.task_settings import TaskSettingsStore  # noqa: E402
from forge.ops.task_runtime import (  # noqa: E402
    TaskFlowSnapshot,
    GitHubTaskRuntimeClient,
    label_for_snapshot,
    load_task_flow_snapshots,
)


def label_for_task(state: TaskFlowState) -> str:
    """Return the official label without writing to GitHub."""

    return displayed_label(state)


def issue_number_for_snapshot(snapshot: TaskFlowSnapshot) -> int:
    return snapshot.issue.number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-port",
        action="store_true",
        help="confirm that the displayed-status API port imports successfully",
    )
    parser.add_argument("--db", help="Hermes Kanban SQLite database")
    parser.add_argument("--gh", help="GitHub CLI executable")
    parser.add_argument("--settings-db", help="immutable Task settings database")
    parser.add_argument("--outbox", help="confirmed Task outbox database")
    parser.add_argument("--repo", help="GitHub repository as OWNER/REPO")
    parser.add_argument("--dry-run", action="store_true", help="report without changing labels")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.check_port:
        print(json.dumps({"status": "ready", "writer": "issue-status-sync"}))
        return 0
    required = {
        "--db": args.db,
        "--gh": args.gh,
        "--settings-db": args.settings_db,
        "--outbox": args.outbox,
        "--repo": args.repo,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        _parser().error(f"required arguments: {', '.join(missing)}")
    try:
        snapshots = load_task_flow_snapshots(
            settings_db=args.settings_db,
            outbox_db=args.outbox,
            hermes_db=args.db,
            github=GitHubTaskRuntimeClient(args.gh),
            repository=args.repo,
        )
        writer = GitHubIssueStatusClient(args.gh)
        settings_store = TaskSettingsStore(args.settings_db)
        reports = []
        for snapshot in snapshots:
            label = label_for_snapshot(snapshot)
            issue_number = issue_number_for_snapshot(snapshot)
            if not args.dry_run:
                # RISK(race): keep cancellation ordered with the GitHub write.
                with settings_store.guard_active(snapshot.settings):
                    writer.replace_status(args.repo, issue_number, label)
            reports.append({"issue_number": issue_number, "label": label})
    except Exception as error:
        print(
            json.dumps({"status": "error", "error": str(error)}),
            file=sys.stderr,
        )
        return 2
    print(json.dumps({"status": "ok", "tasks": reports}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
