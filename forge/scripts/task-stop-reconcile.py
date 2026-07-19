#!/usr/bin/env python3
"""Retry unfinished Task Stops until local and GitHub truth converge."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Callable, Sequence
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from forge.ops.github import GitHubClient, GitHubTaskIssueClientV2  # noqa: E402
from forge.ops.process_identity import (  # noqa: E402
    PosixProcessBackend,
    ProcessBinding,
    ProcessIdentity,
    ProcessIdentityError,
    WindowsJobBackend,
)
from forge.ops.task_database import TaskDatabase  # noqa: E402
from forge.ops.task_stop import (  # noqa: E402
    HermesKanbanStopper,
    ProcessTreeStopper,
    StopReconcileReceipt,
    TaskStopReconciler,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings-db", required=True, type=Path)
    parser.add_argument("--kanban-db", required=True, type=Path)
    parser.add_argument("--dispatcher-db", required=True, type=Path)
    parser.add_argument("--owner-host", required=True)
    parser.add_argument("--gh", default="/usr/bin/gh")
    parser.add_argument(
        "--stop-request-id",
        action="append",
        dest="stop_request_ids",
        help="reconcile one exact Stop; repeat for more than one",
    )
    return parser


def _stored_identity_lookup(
    database: TaskDatabase,
    backend: PosixProcessBackend | WindowsJobBackend,
) -> Callable[[ProcessBinding, int], ProcessIdentity]:
    def lookup(binding: ProcessBinding, pid: int) -> ProcessIdentity:
        with database.read() as connection:
            rows = connection.execute(
                """
                SELECT run_id, worker_task_id, process_identity_json
                FROM task_runtime_runs
                WHERE request_id = ? AND task_settings_hash = ?
                  AND project_id = ? AND host_id = ?
                  AND state IN ('starting', 'running', 'stopping')
                ORDER BY run_id
                """,
                (
                    binding.request_id,
                    binding.task_settings_hash,
                    binding.project_id,
                    binding.host_id,
                ),
            ).fetchall()
        matches: list[ProcessIdentity] = []
        for row in rows:
            try:
                identity = ProcessIdentity.from_json(str(row[2]))
            except (TypeError, ValueError) as error:
                raise ProcessIdentityError(
                    "durable exact process identity is malformed"
                ) from error
            stored_binding = identity.binding
            if (
                stored_binding.request_id != binding.request_id
                or stored_binding.task_settings_hash != binding.task_settings_hash
                or stored_binding.project_id != binding.project_id
                or stored_binding.host_id != binding.host_id
                or stored_binding.run_id != row[0]
                or stored_binding.task_id != row[1]
            ):
                raise ProcessIdentityError(
                    "durable exact process identity does not match its runtime row"
                )
            if stored_binding == binding and identity.pid == pid:
                matches.append(identity)
        if len(matches) != 1:
            raise ProcessIdentityError(
                "durable exact process identity is missing or ambiguous"
            )
        return matches[0]

    return lookup


def build_reconciler(args: argparse.Namespace) -> TaskStopReconciler:
    database = TaskDatabase(args.settings_db)
    backend = WindowsJobBackend() if os.name == "nt" else PosixProcessBackend()
    return TaskStopReconciler(
        database,
        issue_client=GitHubTaskIssueClientV2(args.gh),
        pull_request_reader=GitHubClient(args.gh),
        kanban_stopper=HermesKanbanStopper(
            args.kanban_db,
            dispatcher_database_path=args.dispatcher_db,
            current_host=args.owner_host,
            identity_lookup=_stored_identity_lookup(database, backend),
        ),
        process_stopper=ProcessTreeStopper(backend),
        current_host=args.owner_host,
    )


def _report(receipt: StopReconcileReceipt) -> dict[str, object]:
    return {
        "request_id": receipt.request_id,
        "result": receipt.result,
        "state": receipt.state,
        "stop_request_id": receipt.stop_request_id,
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    reconciler_builder: Callable[
        [argparse.Namespace], TaskStopReconciler
    ] = build_reconciler,
) -> int:
    args = _parser().parse_args(argv)
    try:
        reconciler = reconciler_builder(args)
        stop_ids = (
            tuple(args.stop_request_ids)
            if args.stop_request_ids
            else reconciler.list_reconcilable()
        )
        receipts = tuple(reconciler.reconcile(stop_id) for stop_id in stop_ids)
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as error:
        print(
            json.dumps(
                {"error": str(error), "status": "error"},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    reports = [_report(receipt) for receipt in receipts]
    complete = all(receipt.state == "completed" for receipt in receipts)
    print(
        json.dumps(
            {"status": "ok" if complete else "cleanup_incomplete", "stops": reports},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
