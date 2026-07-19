"""Atomically archive one v2 Task's Hermes cards and capture active runs."""

from __future__ import annotations

import contextlib
import importlib
import json
import math
import os
import socket
import sqlite3
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from .hermes import GateError, parse_project_task_card_key
from .process_identity import ProcessBinding, ProcessIdentity


_NONTERMINAL_STATUSES = frozenset(
    {"triage", "todo", "scheduled", "ready", "running", "blocked", "review"}
)
_TERMINAL_STATUSES = frozenset({"done", "archived"})
_KNOWN_STATUSES = _NONTERMINAL_STATUSES | _TERMINAL_STATUSES


class KanbanStopError(RuntimeError):
    """Raised when card or process identity cannot be stopped safely."""


@dataclass(frozen=True, slots=True)
class CapturedCardRun:
    """Active Hermes run captured before its card becomes terminal."""

    request_id: str
    task_settings_hash: str
    project_id: str
    card_id: str
    run_id: int
    worker_pid: int | None
    claim_lock: str | None
    process_identity: ProcessIdentity | None


@dataclass(frozen=True, slots=True)
class KanbanStopResult:
    """Committed card stop result and the process identities to terminate."""

    request_id: str
    archived_card_ids: tuple[str, ...]
    preserved_card_ids: tuple[str, ...]
    captured_runs: tuple[CapturedCardRun, ...]
    all_cards_terminal: bool


ProcessIdentityLookup = Callable[[ProcessBinding, int], ProcessIdentity]


@dataclass(frozen=True, slots=True)
class _Card:
    card_id: str
    key: str
    project_id: str
    status: str
    claim_lock: str | None
    claim_expires: int | None
    worker_pid: int | None
    current_run_id: int | None


def archive_matching_cards(
    database_path: str | Path,
    *,
    request_id: str,
    task_settings_hash: str,
    owner_host: str,
    current_host: str,
    dispatcher_database_path: str | Path,
    reason: str,
    occurred_at: int | None = None,
    identity_lookup: ProcessIdentityLookup | None = None,
    claimer_host_name: str | None = None,
    lock_timeout_seconds: float = 30.0,
) -> KanbanStopResult:
    """Archive exact v2 cards under the same lock used by Hermes dispatch.

    The dispatch-file guard covers the gap where Hermes has committed a claim
    but has not yet persisted the spawned PID. The SQLite IMMEDIATE transaction
    then captures every active run and archives every matching nonterminal card
    as one indivisible database change.
    """

    supplied_path = Path(database_path).expanduser().absolute()
    dispatcher_path = Path(dispatcher_database_path).expanduser().absolute()
    request_id = _canonical_uuid(request_id, "request_id")
    task_settings_hash = _lower_sha256(task_settings_hash, "task_settings_hash")
    owner_host = _canonical_uuid(owner_host, "owner_host")
    current_host = _canonical_uuid(current_host, "current_host")
    if owner_host != current_host:
        raise KanbanStopError("Task owner host does not match the current host")
    reason = _bounded_text(reason, "reason", maximum_bytes=4096)
    if claimer_host_name is None:
        claimer_host_name = socket.gethostname()
    claimer_host_name = _bounded_text(
        claimer_host_name, "claimer_host_name", maximum_bytes=255
    )
    if occurred_at is None:
        occurred_at = int(time.time())
    if (
        not isinstance(occurred_at, int)
        or isinstance(occurred_at, bool)
        or occurred_at < 0
    ):
        raise ValueError("occurred_at must be a non-negative integer")
    lock_timeout_seconds = _finite_positive_number(
        lock_timeout_seconds, "lock_timeout_seconds"
    )
    if not supplied_path.is_file():
        raise KanbanStopError("Hermes Kanban database does not exist")
    if not dispatcher_path.is_file():
        raise KanbanStopError("Hermes dispatcher database does not exist")
    try:
        path = supplied_path.resolve(strict=True)
        if not os.path.samefile(path, dispatcher_path):
            raise KanbanStopError(
                "Hermes dispatcher database identity does not match the Stop database"
            )
    except OSError as error:
        raise KanbanStopError(
            "Hermes dispatcher database identity could not be verified"
        ) from error

    # RISK(race): the file guard is the same board-scoped byte lock held for a
    # complete Hermes dispatcher tick, including its claim -> spawn -> PID gap.
    with _board_dispatch_guard(
        (path, dispatcher_path),
        dispatcher_database_path=dispatcher_path,
        timeout_seconds=lock_timeout_seconds,
    ):
        try:
            if not os.path.samefile(path, dispatcher_path):
                raise KanbanStopError(
                    "Hermes dispatcher database identity changed under the lock"
                )
        except OSError as error:
            raise KanbanStopError(
                "Hermes dispatcher database identity changed under the lock"
            ) from error
        connection = _connect_existing(path, timeout_seconds=lock_timeout_seconds)
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = _archive_in_transaction(
                    connection,
                    request_id=request_id,
                    task_settings_hash=task_settings_hash,
                    owner_host=owner_host,
                    claimer_host_name=claimer_host_name,
                    reason=reason,
                    occurred_at=occurred_at,
                    identity_lookup=identity_lookup,
                )
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    try:
                        connection.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise
        except KanbanStopError:
            raise
        except sqlite3.Error as error:
            raise KanbanStopError("Kanban stop transaction failed") from error
        finally:
            connection.close()
    return result


def _archive_in_transaction(
    connection: sqlite3.Connection,
    *,
    request_id: str,
    task_settings_hash: str,
    owner_host: str,
    claimer_host_name: str,
    reason: str,
    occurred_at: int,
    identity_lookup: ProcessIdentityLookup | None,
) -> KanbanStopResult:
    cards = _matching_cards(connection, request_id)
    captured = tuple(
        run
        for card in cards
        for run in _capture_card_runs(
            connection,
            card=card,
            request_id=request_id,
            task_settings_hash=task_settings_hash,
            owner_host=owner_host,
            claimer_host_name=claimer_host_name,
            identity_lookup=identity_lookup,
        )
    )

    archived: list[str] = []
    preserved: list[str] = []
    captured_by_card = {run.card_id: run for run in captured}
    for card in cards:
        run = captured_by_card.get(card.card_id)
        if card.status in _TERMINAL_STATUSES:
            if run is not None:
                cleared = connection.execute(
                    """
                    UPDATE tasks
                    SET claim_lock = NULL, claim_expires = NULL,
                        worker_pid = NULL, current_run_id = NULL
                    WHERE id = ? AND status = ? AND idempotency_key = ?
                      AND current_run_id = ?
                    """,
                    (card.card_id, card.status, card.key, run.run_id),
                )
                if cleared.rowcount != 1:
                    raise KanbanStopError(
                        "terminal card changed during the Stop transaction"
                    )
                _end_active_run(
                    connection,
                    run=run,
                    occurred_at=occurred_at,
                )
                _insert_stop_event(
                    connection,
                    card=card,
                    run=run,
                    request_id=request_id,
                    reason=reason,
                    occurred_at=occurred_at,
                    kind="stop_runtime_reclaimed",
                )
            preserved.append(card.card_id)
            continue
        updated = connection.execute(
            """
            UPDATE tasks
            SET status = 'archived', claim_lock = NULL, claim_expires = NULL,
                worker_pid = NULL, current_run_id = NULL
            WHERE id = ? AND status = ? AND idempotency_key = ?
            """,
            (card.card_id, card.status, card.key),
        )
        if updated.rowcount != 1:
            raise KanbanStopError("matching card changed during the Stop transaction")
        if run is not None:
            _end_active_run(
                connection,
                run=run,
                occurred_at=occurred_at,
            )
        _insert_stop_event(
            connection,
            card=card,
            run=run,
            request_id=request_id,
            reason=reason,
            occurred_at=occurred_at,
            kind="archived",
        )
        archived.append(card.card_id)

    terminal = _all_cards_terminal(connection, cards)
    if not terminal:
        raise KanbanStopError("matching cards did not reach a terminal state")
    return KanbanStopResult(
        request_id=request_id,
        archived_card_ids=tuple(archived),
        preserved_card_ids=tuple(preserved),
        captured_runs=captured,
        all_cards_terminal=True,
    )


def _end_active_run(
    connection: sqlite3.Connection,
    *,
    run: CapturedCardRun,
    occurred_at: int,
) -> None:
    ended = connection.execute(
        """
        UPDATE task_runs
        SET status = 'reclaimed', outcome = 'reclaimed',
            summary = COALESCE(summary, 'Task stopped by Infinity Forge'),
            ended_at = ?, claim_lock = NULL, claim_expires = NULL,
            worker_pid = NULL
        WHERE id = ? AND task_id = ? AND ended_at IS NULL
        """,
        (occurred_at, run.run_id, run.card_id),
    )
    if ended.rowcount != 1:
        raise KanbanStopError("active run changed during the Stop transaction")


def _insert_stop_event(
    connection: sqlite3.Connection,
    *,
    card: _Card,
    run: CapturedCardRun | None,
    request_id: str,
    reason: str,
    occurred_at: int,
    kind: str,
) -> None:
    payload = json.dumps(
        {
            "previous_status": card.status,
            "project_id": card.project_id,
            "reason": reason,
            "request_id": request_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    connection.execute(
        """
        INSERT INTO task_events (task_id, run_id, kind, payload, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            card.card_id,
            None if run is None else run.run_id,
            kind,
            payload,
            occurred_at,
        ),
    )


def _matching_cards(
    connection: sqlite3.Connection,
    request_id: str,
) -> tuple[_Card, ...]:
    task_prefix = f"forge-task-v2:{request_id}:%"
    step_prefix = f"forge-step-v2:{request_id}:%"
    rows = connection.execute(
        """
        SELECT id, status, idempotency_key, claim_lock, claim_expires,
               worker_pid, current_run_id
        FROM tasks
        WHERE idempotency_key LIKE ? OR idempotency_key LIKE ?
        ORDER BY id
        """,
        (task_prefix, step_prefix),
    ).fetchall()
    cards: list[_Card] = []
    seen_keys: set[str] = set()
    for row in rows:
        key = row["idempotency_key"]
        if not isinstance(key, str):
            raise KanbanStopError("matching card has no identity key")
        try:
            identity = parse_project_task_card_key(key)
        except GateError as error:
            raise KanbanStopError("malformed matching v2 card identity") from error
        if identity.request_id != request_id:
            raise KanbanStopError("matching card changed request identity")
        if key in seen_keys:
            raise KanbanStopError("duplicate matching v2 card identity")
        seen_keys.add(key)
        status = row["status"]
        if not isinstance(status, str) or status not in _KNOWN_STATUSES:
            raise KanbanStopError("matching card has an unsupported status")
        card_id = _bounded_text(row["id"], "card id", maximum_bytes=512)
        cards.append(
            _Card(
                card_id=card_id,
                key=key,
                project_id=identity.project_id,
                status=status,
                claim_lock=_optional_text(row["claim_lock"], "claim_lock"),
                claim_expires=_optional_int(row["claim_expires"], "claim_expires"),
                worker_pid=_optional_positive_int(row["worker_pid"], "worker_pid"),
                current_run_id=_optional_positive_int(
                    row["current_run_id"], "current_run_id"
                ),
            )
        )
    return tuple(cards)


def _capture_card_runs(
    connection: sqlite3.Connection,
    *,
    card: _Card,
    request_id: str,
    task_settings_hash: str,
    owner_host: str,
    claimer_host_name: str,
    identity_lookup: ProcessIdentityLookup | None,
) -> tuple[CapturedCardRun, ...]:
    rows = connection.execute(
        """
        SELECT id, task_id, status, claim_lock, claim_expires, worker_pid,
               started_at, ended_at
        FROM task_runs
        WHERE task_id = ? AND ended_at IS NULL
        ORDER BY id
        """,
        (card.card_id,),
    ).fetchall()
    if len(rows) > 1:
        raise KanbanStopError("matching card has multiple active runs")
    if not rows:
        if any(
            value is not None
            for value in (
                card.claim_lock,
                card.claim_expires,
                card.worker_pid,
                card.current_run_id,
            )
        ):
            raise KanbanStopError("matching card runtime pointers have no active run")
        return ()

    row = rows[0]
    run_id = _positive_int(row["id"], "run id")
    if row["status"] != "running":
        raise KanbanStopError("active run is not in running status")
    if card.current_run_id != run_id:
        raise KanbanStopError("matching card points to another active run")
    if row["task_id"] != card.card_id:
        raise KanbanStopError("active run belongs to another card")
    run_claim = _optional_text(row["claim_lock"], "run claim_lock")
    run_expiry = _optional_int(row["claim_expires"], "run claim_expires")
    run_pid = _optional_positive_int(row["worker_pid"], "run worker_pid")
    if (run_claim, run_expiry, run_pid) != (
        card.claim_lock,
        card.claim_expires,
        card.worker_pid,
    ):
        raise KanbanStopError("card and active run identities do not match")
    if run_claim is None:
        raise KanbanStopError("active run has no owner-host claim identity")
    claim_host, separator, claim_pid = run_claim.rpartition(":")
    if (
        separator != ":"
        or claim_host != claimer_host_name
        or not claim_pid.isascii()
        or not claim_pid.isdigit()
        or int(claim_pid) <= 0
    ):
        raise KanbanStopError("active run belongs to another claimer host")
    process_identity: ProcessIdentity | None = None
    if run_pid is not None:
        if identity_lookup is None:
            raise KanbanStopError("active worker has no exact process identity lookup")
        binding = ProcessBinding(
            request_id=request_id,
            task_settings_hash=task_settings_hash,
            project_id=card.project_id,
            task_id=card.card_id,
            run_id=str(run_id),
            host_id=owner_host,
        )
        try:
            process_identity = identity_lookup(binding, run_pid)
        except Exception as error:
            raise KanbanStopError(
                "active worker process identity lookup failed"
            ) from error
        if (
            not isinstance(process_identity, ProcessIdentity)
            or process_identity.binding != binding
            or process_identity.pid != run_pid
        ):
            raise KanbanStopError(
                "active worker process identity does not match its Task"
            )
    return (
        CapturedCardRun(
            request_id=request_id,
            task_settings_hash=task_settings_hash,
            project_id=card.project_id,
            card_id=card.card_id,
            run_id=run_id,
            worker_pid=run_pid,
            claim_lock=run_claim,
            process_identity=process_identity,
        ),
    )


def _all_cards_terminal(
    connection: sqlite3.Connection,
    cards: tuple[_Card, ...],
) -> bool:
    if not cards:
        return True
    placeholders = ",".join("?" for _ in cards)
    rows = connection.execute(
        f"SELECT id, status FROM tasks WHERE id IN ({placeholders})",  # noqa: S608
        tuple(card.card_id for card in cards),
    ).fetchall()
    return len(rows) == len(cards) and all(
        row["status"] in _TERMINAL_STATUSES for row in rows
    )


def _connect_existing(path: Path, *, timeout_seconds: float) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=rw",
            uri=True,
            isolation_level=None,
            timeout=timeout_seconds,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {int(timeout_seconds * 1000)}")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection
    except sqlite3.Error as error:
        raise KanbanStopError("Hermes Kanban database could not be opened") from error


@contextlib.contextmanager
def _board_dispatch_guard(
    database_paths: tuple[Path, ...],
    *,
    dispatcher_database_path: Path,
    timeout_seconds: float,
) -> Iterator[None]:
    lock_paths = _unique_dispatch_lock_paths(database_paths)
    handles: list[object] = []
    deadline = time.monotonic() + timeout_seconds
    try:
        for lock_path in lock_paths:
            if lock_path.is_symlink():
                raise KanbanStopError("Hermes dispatcher lock path is a symbolic link")
            try:
                handle = lock_path.open("a+b")
            except OSError as error:
                raise KanbanStopError(
                    "Hermes dispatcher lock could not be opened"
                ) from error
            try:
                _acquire_dispatch_lock(handle, deadline=deadline)
            except Exception:
                handle.close()
                raise
            handles.append(handle)

        try:
            hermes_acquired = _installed_hermes_dispatch_lock_probe(
                dispatcher_database_path
            )
        except Exception as error:
            raise KanbanStopError(
                "Hermes dispatcher lock compatibility could not be verified"
            ) from error
        if type(hermes_acquired) is not bool or hermes_acquired:
            raise KanbanStopError(
                "Hermes dispatcher lock compatibility evidence is unsafe"
            )
        yield
    finally:
        for handle in reversed(handles):
            try:
                try:
                    _release_dispatch_lock(handle)
                except OSError:
                    # Closing the handle below releases the OS lock; do not
                    # hide a committed Stop result with a secondary unlock error.
                    pass
            finally:
                handle.close()


def _unique_dispatch_lock_paths(database_paths: tuple[Path, ...]) -> tuple[Path, ...]:
    unique: dict[str, Path] = {}
    for database_path in database_paths:
        lock_path = database_path.with_name(database_path.name + ".dispatch.lock")
        key = os.path.normcase(os.path.abspath(lock_path))
        unique[key] = lock_path
    return tuple(unique[key] for key in sorted(unique))


def _acquire_dispatch_lock(handle: object, *, deadline: float) -> None:
    while True:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)  # type: ignore[attr-defined]
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(
                    handle.fileno(),  # type: ignore[attr-defined]
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            return
        except (BlockingIOError, OSError):
            if time.monotonic() >= deadline:
                raise KanbanStopError("Hermes dispatcher is still running a tick")
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def _release_dispatch_lock(handle: object) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)  # type: ignore[attr-defined]
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]


def _installed_hermes_dispatch_lock_probe(database_path: Path) -> bool:
    try:
        module = importlib.import_module("hermes_cli.kanban_db")
        guard = getattr(module, "_dispatch_tick_lock")
    except (ImportError, AttributeError) as error:
        raise KanbanStopError(
            "installed Hermes dispatcher lock contract is unavailable"
        ) from error
    with guard(database_path) as acquired:
        if type(acquired) is not bool:
            raise KanbanStopError(
                "installed Hermes dispatcher lock contract returned invalid evidence"
            )
        return acquired


def _canonical_uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise ValueError(f"{label} must be a canonical UUID") from error
    if str(parsed) != value:
        raise ValueError(f"{label} must be a canonical UUID")
    return value


def _bounded_text(value: object, label: str, *, maximum_bytes: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > maximum_bytes
    ):
        raise ValueError(f"{label} must be non-empty bounded UTF-8 text")
    return value


def _lower_sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _finite_positive_number(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{label} must be a finite positive number")
    try:
        number = float(value)
    except OverflowError as error:
        raise ValueError(f"{label} must be a finite positive number") from error
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} must be a finite positive number")
    return number


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, label, maximum_bytes=512)


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise KanbanStopError(f"{label} must be a positive integer")
    return value


def _optional_positive_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, label)


def _optional_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise KanbanStopError(f"{label} must be an integer")
    return value


__all__ = [
    "CapturedCardRun",
    "KanbanStopError",
    "KanbanStopResult",
    "ProcessIdentityLookup",
    "archive_matching_cards",
]
