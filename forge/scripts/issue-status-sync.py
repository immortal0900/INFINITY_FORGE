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
from forge.ops.task_database import TaskDatabase  # noqa: E402
from forge.ops.task_settings_v2 import (  # noqa: E402
    TaskRequestV2,
    TaskSettingsV2,
)
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


class V2ParentStatus:
    __slots__ = (
        "request_id",
        "management_repository",
        "parent_issue_number",
        "label",
    )

    def __init__(
        self,
        *,
        request_id: str,
        management_repository: str,
        parent_issue_number: int,
        label: str,
    ) -> None:
        self.request_id = request_id
        self.management_repository = management_repository
        self.parent_issue_number = parent_issue_number
        self.label = label


def load_v2_parent_statuses(path: str | Path) -> tuple[V2ParentStatus, ...]:
    """Load only durable partial-merge parents that require a decision."""

    database = TaskDatabase(path)
    with database.read() as connection:
        rows = connection.execute(
            """
            SELECT r.request_json, s.settings_json, e.task_settings_hash,
                   e.project_id, e.event_key, e.event_json,
                   e.occurred_at
            FROM task_events AS e
            JOIN task_requests AS r ON r.request_id = e.request_id
            JOIN task_settings_v2 AS s
              ON s.request_id = e.request_id
             AND s.task_settings_hash = e.task_settings_hash
            WHERE e.event_type = 'partially_merged'
            ORDER BY e.request_id
            """
        ).fetchall()
        statuses: list[V2ParentStatus] = []
        for row in rows:
            request = TaskRequestV2.from_json(row[0])
            settings = TaskSettingsV2.from_json(row[1], request=request)
            try:
                payload = json.loads(row[5])
            except (TypeError, json.JSONDecodeError) as error:
                raise RuntimeError("partial merge event is invalid") from error
            if (
                row[2] != settings.task_settings_hash
                or row[3] is not None
                or row[4] != "partially_merged"
                or not isinstance(payload, dict)
                or set(payload)
                != {
                    "failed_project_id",
                    "merged_project_ids",
                    "reason",
                    "remaining_project_ids",
                    "task_settings_hash",
                }
                or payload.get("task_settings_hash") != settings.task_settings_hash
                or not isinstance(payload.get("failed_project_id"), str)
                or not isinstance(payload.get("reason"), str)
                or not payload["reason"].strip()
                or not isinstance(payload.get("merged_project_ids"), list)
                or not payload["merged_project_ids"]
                or not isinstance(payload.get("remaining_project_ids"), list)
            ):
                raise RuntimeError("partial merge event does not match settings")
            project_rows = connection.execute(
                """
                SELECT project_id, state, merge_commit FROM task_projects
                WHERE request_id = ? ORDER BY project_id
                """,
                (request.request_id,),
            ).fetchall()
            states = {item[0]: (item[1], item[2]) for item in project_rows}
            merged_ids = tuple(payload["merged_project_ids"])
            remaining_ids = tuple(payload["remaining_project_ids"])
            failed_id = payload["failed_project_id"]
            supplied = {*merged_ids, failed_id, *remaining_ids}
            if (
                supplied != {project.project_id for project in settings.projects}
                or len(supplied) != len(merged_ids) + 1 + len(remaining_ids)
                or any(
                    states.get(item, (None, None))[0] != "merged"
                    or states.get(item, (None, None))[1] is None
                    for item in merged_ids
                )
                or states.get(failed_id, (None, None))[0] != "failed"
                or any(states.get(item, ("merged", None))[0] == "merged" for item in remaining_ids)
            ):
                raise RuntimeError("partial merge Project readback does not match event")
            statuses.append(
                V2ParentStatus(
                    request_id=request.request_id,
                    management_repository=settings.management_repository,
                    parent_issue_number=settings.parent_issue_number,
                    label="forge:needs-decision",
                )
            )
    return tuple(statuses)


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
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        _parser().error(f"required arguments: {', '.join(missing)}")
    if (args.repo is None) != (args.outbox is None):
        _parser().error("--repo and --outbox must be provided together for v1")
    try:
        writer = GitHubIssueStatusClient(args.gh)
        if args.repo is None:
            statuses = load_v2_parent_statuses(args.settings_db)
            reports = []
            for status in statuses:
                if not args.dry_run:
                    writer.replace_status(
                        status.management_repository,
                        status.parent_issue_number,
                        status.label,
                    )
                reports.append(
                    {
                        "request_id": status.request_id,
                        "issue_number": status.parent_issue_number,
                        "label": status.label,
                    }
                )
            print(json.dumps({"status": "ok", "tasks": reports}))
            return 0
        assert args.outbox is not None
        snapshots = load_task_flow_snapshots(
            settings_db=args.settings_db,
            outbox_db=args.outbox,
            hermes_db=args.db,
            github=GitHubTaskRuntimeClient(args.gh),
            repository=args.repo,
        )
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
