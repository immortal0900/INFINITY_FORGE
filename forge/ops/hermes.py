"""Read-only Hermes Task evidence access and deterministic create commands."""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .contracts import RunRecord, TaskRecord
from .task_flow import TaskCardSpec, TaskStep, role_for_step
from .task_options import TaskRole


class GateError(RuntimeError):
    """Raised when external evidence is incomplete, ambiguous, or malformed."""


class TaskCardKind(str, Enum):
    TASK = "task"
    STEP = "step"


@dataclass(frozen=True)
class TaskCardIdentity:
    """Parsed clean-break identity for one root Task or child step card."""

    kind: TaskCardKind
    repository: str
    issue_number: int
    hash_prefix: str
    step: TaskStep | None = None


@dataclass(frozen=True)
class ProjectTaskCardIdentity:
    """Parsed v2 identity bound to one request, Project, and flow step."""

    request_id: str
    project_id: str
    step: TaskStep
    hash_prefix: str | None = None


@dataclass(frozen=True)
class ProjectTaskCardSpec:
    """Exact Hermes card data for one selected Project worktree."""

    step: TaskStep
    title: str
    body: str
    idempotency_key: str
    parent_id: str | None
    skill: str

    def __post_init__(self) -> None:
        if not isinstance(self.step, TaskStep):
            raise GateError("Project card step must be a TaskStep")
        for field in ("title", "body", "idempotency_key", "skill"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise GateError(f"{field} must be a non-empty string")
        if self.parent_id is not None and (
            not isinstance(self.parent_id, str) or not self.parent_id.strip()
        ):
            raise GateError("parent_id must be null or non-empty text")
        identity = parse_project_task_card_key(self.idempotency_key)
        if identity.step is not self.step:
            raise GateError("Project card key step does not match spec")
        if self.step is TaskStep.BUILD and identity.hash_prefix is None:
            if self.parent_id is not None:
                raise GateError("Project root Build card must not have a parent")
        elif self.parent_id is None:
            raise GateError("Project step card must have a parent")

    @property
    def role(self) -> TaskRole:
        return role_for_step(self.step)


@dataclass(frozen=True)
class RootTaskCardSpec:
    """Exact data for the one builder card that starts a confirmed Task."""

    title: str
    body: str
    idempotency_key: str
    skill: str = "build-task"

    def __post_init__(self) -> None:
        for field in ("title", "body", "idempotency_key", "skill"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise GateError(f"{field} must be a non-empty string")
        identity = parse_task_card_key(self.idempotency_key)
        if identity.kind is not TaskCardKind.TASK:
            raise GateError("root card key must use forge-task")
        if self.skill != "build-task":
            raise GateError("root card skill must be build-task")

    @property
    def role(self) -> TaskRole:
        return TaskRole.BUILDER


@dataclass(frozen=True)
class HermesTaskCard:
    """Strict runtime view of one Forge-owned Hermes card."""

    task_id: str
    title: str
    status: str
    body: str
    parent_id: str | None
    idempotency_key: str
    assignee: str
    skills: tuple[str, ...]


_TASK_KEY_RE = re.compile(
    r"^forge-task:(?P<repository>[^/#:\s]+/[^/#:\s]+)#"
    r"(?P<issue>[1-9][0-9]*):(?P<hash>[0-9a-f]{16})$"
)
_STEP_KEY_RE = re.compile(
    r"^forge-step:(?P<repository>[^/#:\s]+/[^/#:\s]+)#"
    r"(?P<issue>[1-9][0-9]*):"
    r"(?P<step>build|review|deep_check|fix):(?P<hash>[0-9a-f]{16})$"
)
_PROJECT_TASK_KEY_RE = re.compile(
    r"^forge-task-v2:(?P<request_id>[0-9a-f-]{36}):"
    r"(?P<project_id>[0-9a-f]{64}):build$"
)
_PROJECT_STEP_KEY_RE = re.compile(
    r"^forge-step-v2:(?P<request_id>[0-9a-f-]{36}):"
    r"(?P<project_id>[0-9a-f]{64}):"
    r"(?P<step>build|review|deep_check|fix):(?P<hash>[0-9a-f]{16})$"
)
_REPOSITORY_RE = re.compile(r"^[^/#:\s]+/[^/#:\s]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GateError(f"{label} must be a non-empty string")
    return value


def _require_repository(repository: str) -> None:
    if (
        not isinstance(repository, str)
        or _REPOSITORY_RE.fullmatch(repository) is None
    ):
        raise GateError("repository must use OWNER/REPO")


def _require_issue_number(issue_number: int) -> None:
    if (
        not isinstance(issue_number, int)
        or isinstance(issue_number, bool)
        or issue_number < 1
    ):
        raise GateError("issue_number must be a positive integer")


def _require_full_hash(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise GateError(f"{label} must be a lowercase SHA-256")


def task_card_key(
    repository: str, issue_number: int, task_settings_hash: str
) -> str:
    """Build the only accepted root Task idempotency key."""

    _require_repository(repository)
    _require_issue_number(issue_number)
    _require_full_hash(task_settings_hash, "task_settings_hash")
    return f"forge-task:{repository}#{issue_number}:{task_settings_hash[:16]}"


def step_card_key(
    repository: str,
    issue_number: int,
    step: TaskStep,
    source_result_hash: str,
) -> str:
    """Build the only accepted child step idempotency key."""

    _require_repository(repository)
    _require_issue_number(issue_number)
    if not isinstance(step, TaskStep):
        raise GateError("step must be build, review, deep_check, or fix")
    _require_full_hash(source_result_hash, "source_result_hash")
    return (
        f"forge-step:{repository}#{issue_number}:{step.value}:"
        f"{source_result_hash[:16]}"
    )


def parse_task_card_key(key: str) -> TaskCardIdentity:
    """Parse only clean-break forge-task and forge-step keys."""

    key = _require_text(key, "idempotency key")
    task_match = _TASK_KEY_RE.fullmatch(key)
    if task_match is not None:
        return TaskCardIdentity(
            kind=TaskCardKind.TASK,
            repository=task_match.group("repository"),
            issue_number=int(task_match.group("issue")),
            hash_prefix=task_match.group("hash"),
        )
    step_match = _STEP_KEY_RE.fullmatch(key)
    if step_match is not None:
        return TaskCardIdentity(
            kind=TaskCardKind.STEP,
            repository=step_match.group("repository"),
            issue_number=int(step_match.group("issue")),
            hash_prefix=step_match.group("hash"),
            step=TaskStep(step_match.group("step")),
        )
    raise GateError("idempotency key must use the new forge-task or forge-step format")


def _require_request_id(request_id: str) -> str:
    from uuid import UUID

    if not isinstance(request_id, str):
        raise GateError("request_id must be a canonical UUID")
    try:
        parsed = UUID(request_id)
    except ValueError as error:
        raise GateError("request_id must be a canonical UUID") from error
    if str(parsed) != request_id:
        raise GateError("request_id must be a canonical UUID")
    return request_id


def _require_project_id(project_id: str) -> str:
    _require_full_hash(project_id, "project_id")
    return project_id


def project_task_card_key(request_id: str, project_id: str) -> str:
    """Return the Task 8/9 root key for one selected Project Build."""

    return (
        f"forge-task-v2:{_require_request_id(request_id)}:"
        f"{_require_project_id(project_id)}:build"
    )


def project_step_card_key(
    request_id: str,
    project_id: str,
    step: TaskStep,
    source_result_hash: str,
) -> str:
    """Return a v2 child key bound to request, Project, step, and proof."""

    _require_request_id(request_id)
    _require_project_id(project_id)
    if not isinstance(step, TaskStep):
        raise GateError("step must be build, review, deep_check, or fix")
    _require_full_hash(source_result_hash, "source_result_hash")
    return (
        f"forge-step-v2:{request_id}:{project_id}:{step.value}:"
        f"{source_result_hash[:16]}"
    )


def parse_project_task_card_key(key: str) -> ProjectTaskCardIdentity:
    """Parse only the separate v2 Project card namespace."""

    key = _require_text(key, "idempotency key")
    match = _PROJECT_TASK_KEY_RE.fullmatch(key)
    if match is not None:
        request_id = _require_request_id(match.group("request_id"))
        return ProjectTaskCardIdentity(
            request_id=request_id,
            project_id=_require_project_id(match.group("project_id")),
            step=TaskStep.BUILD,
        )
    match = _PROJECT_STEP_KEY_RE.fullmatch(key)
    if match is not None:
        request_id = _require_request_id(match.group("request_id"))
        return ProjectTaskCardIdentity(
            request_id=request_id,
            project_id=_require_project_id(match.group("project_id")),
            step=TaskStep(match.group("step")),
            hash_prefix=match.group("hash"),
        )
    raise GateError("idempotency key must use forge-task-v2 or forge-step-v2")


def _parse_json_object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, str):
        raise GateError(f"{label} must be a JSON object")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise GateError(f"{label} has duplicate JSON field: {key}")
            result[key] = item
        return result

    try:
        parsed = json.loads(value, object_pairs_hook=unique_object)
    except json.JSONDecodeError as error:
        raise GateError(f"{label} must be valid JSON") from error
    if not isinstance(parsed, dict):
        raise GateError(f"{label} must be a JSON object")
    return parsed


def _parse_run_metadata(value: object) -> Mapping[str, object]:
    # Hermes stores absent metadata as SQL NULL; that is the official empty
    # representation, not a fallback from malformed JSON.
    if value is None:
        return {}
    return _parse_json_object(value, "run metadata")


class HermesStore:
    """Query new Task records without granting this process SQLite writes."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    def _connect(self) -> sqlite3.Connection:
        uri = f"{self._db_path.as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def list_task_cards(self) -> Sequence[TaskRecord]:
        """Read only clean-break root and step cards."""

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
                    WHERE t.idempotency_key LIKE 'forge-task:%'
                       OR t.idempotency_key LIKE 'forge-step:%'
                    ORDER BY t.id
                    """
                ).fetchall()
        except sqlite3.Error as error:
            raise GateError("Hermes Task card query failed") from error

        records: dict[str, TaskRecord] = {}
        keys: dict[str, str] = {}
        parents: dict[str, set[str]] = {}
        for row in rows:
            task_id = _require_text(row["id"], "task id")
            key = _require_text(row["idempotency_key"], "idempotency key")
            try:
                parse_task_card_key(key)
            except GateError as error:
                raise GateError(f"malformed Task card identity key: {key}") from error
            existing_task = keys.get(key)
            if existing_task is not None and existing_task != task_id:
                raise GateError(f"duplicate Task card idempotency key: {key}")
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

        result: list[TaskRecord] = []
        for task_id in sorted(records):
            task_parents = parents.get(task_id, set())
            if len(task_parents) > 1:
                raise GateError(f"Task card has multiple parents: {task_id}")
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

    def list_runtime_cards(self) -> tuple[HermesTaskCard, ...]:
        """Read Forge cards with the exact role and skill runtime needs."""

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
                        t.assignee,
                        t.skills,
                        l.parent_id
                    FROM tasks AS t
                    LEFT JOIN task_links AS l ON l.child_id = t.id
                    WHERE t.idempotency_key LIKE 'forge-task:%'
                       OR t.idempotency_key LIKE 'forge-step:%'
                    ORDER BY t.id
                    """
                ).fetchall()
        except sqlite3.Error as error:
            raise GateError("Hermes runtime Task card query failed") from error
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(_require_text(row["id"], "task id"), []).append(row)
        result: list[HermesTaskCard] = []
        seen_keys: dict[str, str] = {}
        for task_id in sorted(grouped):
            task_rows = grouped[task_id]
            first = task_rows[0]
            key = _require_text(first["idempotency_key"], "idempotency key")
            parse_task_card_key(key)
            if key in seen_keys and seen_keys[key] != task_id:
                raise GateError(f"duplicate Task card idempotency key: {key}")
            seen_keys[key] = task_id
            parents = {
                _require_text(row["parent_id"], "parent id")
                for row in task_rows
                if row["parent_id"] is not None
            }
            if len(parents) > 1:
                raise GateError(f"Task card has multiple parents: {task_id}")
            body = first["body"]
            if not isinstance(body, str) or not body:
                raise GateError("Hermes runtime card body must be non-empty text")
            raw_skills = first["skills"]
            if not isinstance(raw_skills, str):
                raise GateError("Hermes runtime card skills must be JSON text")
            try:
                skills = json.loads(raw_skills)
            except json.JSONDecodeError as error:
                raise GateError("Hermes runtime card skills are invalid JSON") from error
            if (
                not isinstance(skills, list)
                or not skills
                or any(not isinstance(skill, str) or not skill.strip() for skill in skills)
                or len(skills) != len(set(skills))
            ):
                raise GateError("Hermes runtime card skills are invalid")
            result.append(
                HermesTaskCard(
                    task_id=task_id,
                    title=_require_text(first["title"], "task title"),
                    status=_require_text(first["status"], "task status"),
                    body=body,
                    parent_id=next(iter(parents), None),
                    idempotency_key=key,
                    assignee=_require_text(first["assignee"], "task assignee"),
                    skills=tuple(skills),
                )
            )
        return tuple(result)

    def list_project_runtime_cards(self) -> tuple[HermesTaskCard, ...]:
        """Read only v2 Project cards from their separate key namespace."""

        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    """
                    SELECT
                        t.id, t.title, t.status, t.body, t.idempotency_key,
                        t.assignee, t.skills, l.parent_id
                    FROM tasks AS t
                    LEFT JOIN task_links AS l ON l.child_id = t.id
                    WHERE t.idempotency_key LIKE 'forge-task-v2:%'
                       OR t.idempotency_key LIKE 'forge-step-v2:%'
                    ORDER BY t.id
                    """
                ).fetchall()
        except sqlite3.Error as error:
            raise GateError("Hermes Project runtime card query failed") from error
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(_require_text(row["id"], "task id"), []).append(row)
        result: list[HermesTaskCard] = []
        seen_keys: dict[str, str] = {}
        for task_id in sorted(grouped):
            task_rows = grouped[task_id]
            first = task_rows[0]
            key = _require_text(first["idempotency_key"], "idempotency key")
            parse_project_task_card_key(key)
            if key in seen_keys and seen_keys[key] != task_id:
                raise GateError(f"duplicate Project card idempotency key: {key}")
            seen_keys[key] = task_id
            parents = {
                _require_text(row["parent_id"], "parent id")
                for row in task_rows
                if row["parent_id"] is not None
            }
            if len(parents) > 1:
                raise GateError(f"Project card has multiple parents: {task_id}")
            body = first["body"]
            if not isinstance(body, str) or not body:
                raise GateError("Hermes Project card body must be non-empty text")
            raw_skills = first["skills"]
            if not isinstance(raw_skills, str):
                raise GateError("Hermes Project card skills must be JSON text")
            try:
                skills = json.loads(raw_skills)
            except json.JSONDecodeError as error:
                raise GateError("Hermes Project card skills are invalid JSON") from error
            if (
                not isinstance(skills, list)
                or not skills
                or any(not isinstance(skill, str) or not skill.strip() for skill in skills)
                or len(skills) != len(set(skills))
            ):
                raise GateError("Hermes Project card skills are invalid")
            result.append(
                HermesTaskCard(
                    task_id=task_id,
                    title=_require_text(first["title"], "task title"),
                    status=_require_text(first["status"], "task status"),
                    body=body,
                    parent_id=next(iter(parents), None),
                    idempotency_key=key,
                    assignee=_require_text(first["assignee"], "task assignee"),
                    skills=tuple(skills),
                )
            )
        return tuple(result)

    def completed_runs(self, task_id: str) -> tuple[RunRecord, ...]:
        """Return every successful completed run in stable order."""

        _require_text(task_id, "task id")
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    """
                    SELECT id, task_id, status, outcome, summary, metadata
                    FROM task_runs
                    WHERE task_id = ?
                      AND status IN ('done', 'completed')
                      AND outcome = 'completed'
                    ORDER BY id
                    """,
                    (task_id,),
                ).fetchall()
        except sqlite3.Error as error:
            raise GateError("Hermes completed run query failed") from error
        result: list[RunRecord] = []
        for row in rows:
            run_id = row["id"]
            if not isinstance(run_id, int) or isinstance(run_id, bool):
                raise GateError("run id must be an integer")
            result.append(
                RunRecord(
                    run_id=run_id,
                    task_id=_require_text(row["task_id"], "run task id"),
                    status="completed",
                    outcome="success",
                    summary=_parse_json_object(row["summary"], "run summary"),
                    metadata=_parse_run_metadata(row["metadata"]),
                )
            )
        return tuple(result)

    def has_idempotency_key(self, key: str) -> bool:
        parse_task_card_key(key)
        try:
            with closing(self._connect()) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE idempotency_key = ?",
                    (key,),
                ).fetchone()[0]
        except sqlite3.Error as error:
            raise GateError("Hermes idempotency query failed") from error
        if count > 1:
            raise GateError(f"duplicate Task card idempotency key: {key}")
        return count == 1

    def has_project_idempotency_key(self, key: str) -> bool:
        """Check a v2 Project card without accepting it as a v1 identity."""

        parse_project_task_card_key(key)
        try:
            with closing(self._connect()) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE idempotency_key = ?",
                    (key,),
                ).fetchone()[0]
        except sqlite3.Error as error:
            raise GateError("Hermes Project card idempotency query failed") from error
        if count > 1:
            raise GateError(f"duplicate Project card idempotency key: {key}")
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
                      AND status IN ('done', 'completed')
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
            metadata=_parse_run_metadata(row["metadata"]),
        )


def build_create_argv(spec: TaskCardSpec, workspace: str) -> tuple[str, ...]:
    """Build stable Hermes arguments for one official Task step card."""

    if not isinstance(spec, TaskCardSpec):
        raise TypeError("spec must be a TaskCardSpec")
    identity = parse_task_card_key(spec.idempotency_key)
    if identity.kind is not TaskCardKind.STEP or identity.step is not spec.step:
        raise GateError("step card key does not match TaskCardSpec.step")
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
        spec.role.value,
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


def build_root_create_argv(
    spec: RootTaskCardSpec,
    workspace: str,
) -> tuple[str, ...]:
    """Build stable Hermes arguments for the parentless builder root card."""

    if not isinstance(spec, RootTaskCardSpec):
        raise TypeError("spec must be a RootTaskCardSpec")
    if not isinstance(workspace, str) or not workspace.startswith("dir:"):
        raise ValueError("workspace must use a non-empty dir: path")
    if not workspace.removeprefix("dir:").strip():
        raise ValueError("workspace must use a non-empty dir: path")
    return (
        "kanban",
        "create",
        spec.title,
        "--body",
        spec.body,
        "--assignee",
        spec.role.value,
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


def build_project_create_argv(
    spec: ProjectTaskCardSpec,
    worktree: str | Path,
) -> tuple[str, ...]:
    """Build a v2 Hermes command scoped to the Project's isolated worktree."""

    if not isinstance(spec, ProjectTaskCardSpec):
        raise TypeError("spec must be a ProjectTaskCardSpec")
    path = Path(worktree)
    path_text = path.as_posix()
    if not path_text or path_text == ".":
        raise ValueError("worktree must be a non-empty path")
    argv = [
        "kanban",
        "create",
        spec.title,
        "--body",
        spec.body,
        "--assignee",
        spec.role.value,
    ]
    if spec.parent_id is not None:
        argv.extend(("--parent", spec.parent_id))
    argv.extend(
        (
            "--workspace",
            f"dir:{path_text}",
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
    )
    return tuple(argv)


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
