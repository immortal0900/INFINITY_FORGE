#!/usr/bin/env python3
"""Append new Hermes Task events to the Forge activity log."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path


def write_new_events(database: Path, activity_log: Path, state_file: Path) -> int:
    """Append each new event once and atomically advance the saved event ID."""

    last_event_id = 0
    if state_file.exists():
        try:
            last_event_id = int(state_file.read_text(encoding="utf-8").strip())
        except ValueError as error:
            raise RuntimeError("saved activity event ID is invalid") from error
    with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT e.id, e.task_id, e.run_id, e.kind, e.payload, e.created_at, "
            "t.title, t.assignee, t.status "
            "FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id "
            "WHERE e.id > ? ORDER BY e.id",
            (last_event_id,),
        ).fetchall()
    if not rows:
        return 0
    activity_log.parent.mkdir(parents=True, exist_ok=True)
    with activity_log.open("a", encoding="utf-8") as output:
        for row in rows:
            output.write(
                json.dumps(
                    {
                        "event_id": row[0],
                        "task_id": row[1],
                        "run_id": row[2],
                        "kind": row[3],
                        "payload": row[4],
                        "timestamp": datetime.fromtimestamp(row[5]).isoformat(),
                        "title": row[6],
                        "assignee": row[7],
                        "task_status": row[8],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        output.flush()
        os.fsync(output.fileno())
    new_event_id = rows[-1][0]
    if not isinstance(new_event_id, int) or new_event_id <= last_event_id:
        raise RuntimeError("activity event IDs did not increase")
    state_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_file.with_suffix(state_file.suffix + ".tmp")
    temporary.write_text(str(new_event_id), encoding="utf-8")
    # RISK(data-loss): replace the saved cursor only after the activity log is fsynced.
    os.replace(temporary, state_file)
    return len(rows)


def _parser() -> argparse.ArgumentParser:
    home = Path.home()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=home / ".hermes" / "kanban.db")
    parser.add_argument("--activity-log", type=Path, default=home / "forge" / "activity.jsonl")
    parser.add_argument("--state-file", type=Path, default=home / "forge" / "activity.state")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        count = write_new_events(args.database, args.activity_log, args.state_file)
    except Exception as error:
        print(f"CHECK_ERROR: {error}", file=sys.stderr)
        return 2
    print(f"wrote {count} activity events")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
