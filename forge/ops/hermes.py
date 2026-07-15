"""Read-only Hermes evidence access and deterministic create commands."""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from pathlib import Path

from .contracts import RunRecord, TaskRecord
from .stage_reconciler import StageCardSpec


class GateError(RuntimeError):
    """Raised when external evidence is incomplete, ambiguous, or malformed."""


_ROOT_KEY_RE = re.compile(
    r"^github-issue:[^/#:\s]+/[^/#:\s]+#[1-9][0-9]*$"
)
_LEGACY_STAGE_KEY_RE = re.compile(
    r"^github-issue:[^/#:\s]+/[^/#:\s]+#[1-9][0-9]*-"
    r"(?:exec|review|critic)$"
)
_STAGE_KEY_RE = re.compile(
    r"^forge-stage:[^/#:\s]+/[^/#:\s]+#[1-9][0-9]*:"
    r"(?:reviewer|critic|executor-rework):[0-9a-f]{16}$"
)


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GateError(f"{label} must be a non-empty string")
    return value


def _parse_json_object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, str):
        raise GateError(f"{label} must be a JSON object")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise GateError(f"{label} must be valid JSON") from error
    if not isinstance(parsed, dict):
        raise GateError(f"{label} must be a JSON object")
    return parsed


class HermesStore:
    """Query pipeline records without granting this process SQLite writes."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()
        self.ignored_legacy_count = 0

    def _connect(self) -> sqlite3.Connection:
        uri = f"{self._db_path.as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def list_pipeline_tasks(self) -> Sequence[TaskRecord]:
        self.ignored_legacy_count = 0
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    """
                    SELECT
                        t.id,
                        t.title,
                        t.status,
                        t.body,
                        t.idempotency_key,
                        l.parent_id
                    FROM tasks AS t
                    LEFT JOIN task_links AS l ON l.child_id = t.id
                    WHERE t.idempotency_key LIKE 'github-issue:%'
                       OR t.idempotency_key LIKE 'forge-stage:%'
                    ORDER BY t.id
                    """
                ).fetchall()
        except sqlite3.Error as error:
            raise GateError("Hermes pipeline task query failed") from error

        records: dict[str, TaskRecord] = {}
        keys: dict[str, str] = {}
        parents: dict[str, set[str]] = {}
        ignored_legacy_ids: set[str] = set()
        for row in rows:
            task_id = _require_text(row["id"], "task id")
            key = _require_text(row["idempotency_key"], "idempotency key")
            if _LEGACY_STAGE_KEY_RE.fullmatch(key) is not None:
                ignored_legacy_ids.add(task_id)
                continue
            if (
                _ROOT_KEY_RE.fullmatch(key) is None
                and _STAGE_KEY_RE.fullmatch(key) is None
            ):
                raise GateError(f"malformed pipeline identity key: {key}")
            existing_task = keys.get(key)
            if existing_task is not None and existing_task != task_id:
                raise GateError(f"duplicate pipeline idempotency key: {key}")
            keys[key] = task_id

            parent = row["parent_id"]
            if parent is not None:
                parents.setdefault(task_id, set()).add(
                    _require_text(parent, "parent id")
                )
            if task_id not in records:
                body = row["body"]
                if body is not None and not isinstance(body, str):
                    raise GateError("task body must be text or null")
                records[task_id] = TaskRecord(
                    task_id=task_id,
                    title=_require_text(row["title"], "task title"),
                    status=_require_text(row["status"], "task status"),
                    body=body,
                    idempotency_key=key,
                )

        self.ignored_legacy_count = len(ignored_legacy_ids)
        result: list[TaskRecord] = []
        for task_id in sorted(records):
            task_parents = parents.get(task_id, set())
            if len(task_parents) > 1:
                raise GateError(f"pipeline task has multiple parents: {task_id}")
            record = records[task_id]
            result.append(
                TaskRecord(
                    task_id=record.task_id,
                    title=record.title,
                    status=record.status,
                    body=record.body,
                    parent_id=next(iter(task_parents), None),
                    idempotency_key=record.idempotency_key,
                )
            )
        return tuple(result)

    def has_idempotency_key(self, key: str) -> bool:
        _require_text(key, "idempotency key")
        try:
            with closing(self._connect()) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE idempotency_key = ?",
                    (key,),
                ).fetchone()[0]
        except sqlite3.Error as error:
            raise GateError("Hermes idempotency query failed") from error
        if count > 1:
            raise GateError(f"duplicate pipeline idempotency key: {key}")
        return count == 1

    def latest_completed_run(self, task_id: str) -> RunRecord:
        _require_text(task_id, "task id")
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    """
                    SELECT id, task_id, status, outcome, summary, metadata
                    FROM task_runs
                    WHERE task_id = ?
                      AND status = 'done'
                      AND outcome = 'completed'
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (task_id,),
                ).fetchone()
        except sqlite3.Error as error:
            raise GateError("Hermes completed run query failed") from error
        if row is None:
            raise GateError(f"completed run not found for task: {task_id}")
        run_id = row["id"]
        if not isinstance(run_id, int) or isinstance(run_id, bool):
            raise GateError("run id must be an integer")
        outcome = row["outcome"]
        if outcome is not None and not isinstance(outcome, str):
            raise GateError("run outcome must be text or null")
        return RunRecord(
            run_id=run_id,
            task_id=_require_text(row["task_id"], "run task id"),
            status="completed",
            outcome="success",
            summary=_parse_json_object(row["summary"], "run summary"),
            metadata=_parse_json_object(row["metadata"], "run metadata"),
        )


def build_create_argv(spec: StageCardSpec, workspace: str) -> tuple[str, ...]:
    """Build stable Hermes arguments; the caller supplies the binary path."""

    if not isinstance(spec, StageCardSpec):
        raise TypeError("spec must be a StageCardSpec")
    if not isinstance(workspace, str) or not workspace.startswith("dir:"):
        raise ValueError("workspace must use a non-empty dir: path")
    workspace_path = workspace.removeprefix("dir:")
    if not workspace_path.strip():
        raise ValueError("workspace must use a non-empty dir: path")
    return (
        "kanban",
        "create",
        spec.title,
        "--body",
        spec.body,
        "--assignee",
        spec.assignee,
        "--parent",
        spec.parent_id,
        "--workspace",
        workspace,
        "--idempotency-key",
        spec.idempotency_key,
        "--max-retries",
        "4",
        "--skill",
        spec.skill,
        "--goal",
        "--goal-max-turns",
        "20",
    )


class HermesCreateCommand:
    """Perform the only Hermes write through its public CLI."""

    def __init__(
        self,
        hermes_path: str | Path,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._hermes_path = str(Path(hermes_path).expanduser())
        self._runner = runner

    def __call__(self, argv: Sequence[str]) -> None:
        result = self._runner(
            [self._hermes_path, *argv],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            raise GateError(
                f"Hermes create failed with exit code {result.returncode}"
            )
