"""Durable, privacy-minimal identities for authenticated user submissions."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import stat
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator
from uuid import UUID, uuid4

from .task_database import TaskDatabase


DEFAULT_SURFACE_EVENT_RETENTION = timedelta(days=30)
_OUTBOX_FORMAT = "forge-surface-outbox/v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_ID_LENGTH = 512
_OUTBOX_LOCK_TIMEOUT_SECONDS = 10.0
_OUTBOX_LOCK_RETRY_SECONDS = 0.01
_OUTBOX_THREAD_LOCKS: dict[str, threading.RLock] = {}
_OUTBOX_THREAD_LOCKS_GUARD = threading.Lock()


class SurfaceEventError(RuntimeError):
    """Raised when a source event cannot be trusted or durably recorded."""


class SurfaceEventConflictError(SurfaceEventError):
    """Raised when an immutable event ID is reused with different content."""


@dataclass(frozen=True, slots=True)
class TrustedTurnContext:
    """Transport-authenticated identity carried beside model-controlled text."""

    owner_host: str
    subject_id: str
    session_id: str
    surface: str
    source_event_id: str
    working_directory: str | None

    def __post_init__(self) -> None:
        try:
            owner_host = str(UUID(self.owner_host))
        except (AttributeError, TypeError, ValueError):
            raise SurfaceEventError("owner_host must be a canonical UUID") from None
        if owner_host != self.owner_host:
            raise SurfaceEventError("owner_host must be a canonical UUID")
        for field_name in ("subject_id", "session_id", "surface", "source_event_id"):
            _require_identity(getattr(self, field_name), field_name)
        if self.working_directory is not None:
            _require_identity(
                self.working_directory,
                "working_directory",
                allow_whitespace=True,
            )

    def as_mapping(self) -> dict[str, str | None]:
        """Return a fresh mapping safe to give to trusted middleware only."""

        return {
            "owner_host": self.owner_host,
            "subject_id": self.subject_id,
            "session_id": self.session_id,
            "surface": self.surface,
            "source_event_id": self.source_event_id,
            "working_directory": self.working_directory,
        }


@dataclass(frozen=True, slots=True)
class SurfaceEvent:
    source_event_id: str
    subject_id: str
    session_id: str
    surface: str
    payload_hash: str
    state: str
    received_at: datetime
    response_hash: str | None
    responded_at: datetime | None
    retention_until: datetime


class SurfaceEventStore:
    """Store source-event receipts in the shared owner-only Task database."""

    def __init__(
        self,
        database: TaskDatabase,
        *,
        retention: timedelta = DEFAULT_SURFACE_EVENT_RETENTION,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(database, TaskDatabase):
            raise SurfaceEventError("database must be a TaskDatabase")
        if not isinstance(retention, timedelta) or retention <= timedelta(0):
            raise SurfaceEventError("retention must be a positive duration")
        self._database = database
        self._retention = retention
        self._clock = clock or (lambda: datetime.now(UTC))

    def receive(
        self,
        context: TrustedTurnContext,
        payload: str | bytes,
        *,
        at: datetime | None = None,
    ) -> SurfaceEvent:
        """Insert before dispatch, or return the exact prior receipt on retry."""

        if not isinstance(context, TrustedTurnContext):
            raise SurfaceEventError("context must be a TrustedTurnContext")
        payload_hash = surface_event_payload_hash(context, payload)
        received_at = _utc(at if at is not None else self._clock(), "received_at")
        retention_until = received_at + self._retention
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM surface_events WHERE source_event_id = ?",
                (context.source_event_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO surface_events (
                        source_event_id, subject_id, session_id, surface,
                        payload_hash, state, received_at, retention_until
                    ) VALUES (?, ?, ?, ?, ?, 'received', ?, ?)
                    """,
                    (
                        context.source_event_id,
                        context.subject_id,
                        context.session_id,
                        context.surface,
                        payload_hash,
                        _format_time(received_at),
                        _format_time(retention_until),
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM surface_events WHERE source_event_id = ?",
                    (context.source_event_id,),
                ).fetchone()
            event = _event_from_row(row)
            if (
                event.subject_id != context.subject_id
                or event.session_id != context.session_id
                or event.surface != context.surface
                or event.payload_hash != payload_hash
            ):
                # RISK(security): a platform/client ID is an immutable trust
                # boundary. Rebinding it could authorize a different user turn.
                raise SurfaceEventConflictError(
                    "source event identity and payload are immutable"
                )
            return event

    def get(self, source_event_id: str) -> SurfaceEvent:
        _require_identity(source_event_id, "source_event_id")
        with self._database.read() as connection:
            row = connection.execute(
                "SELECT * FROM surface_events WHERE source_event_id = ?",
                (source_event_id,),
            ).fetchone()
        if row is None:
            raise SurfaceEventError("source event was not recorded")
        return _event_from_row(row)

    def mark_handled(self, source_event_id: str) -> SurfaceEvent:
        _require_identity(source_event_id, "source_event_id")
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM surface_events WHERE source_event_id = ?",
                (source_event_id,),
            ).fetchone()
            if row is None:
                raise SurfaceEventError("source event was not recorded")
            event = _event_from_row(row)
            if event.state == "expired":
                raise SurfaceEventError("expired source event cannot be handled")
            if event.state == "received":
                connection.execute(
                    "UPDATE surface_events SET state = 'handled' WHERE source_event_id = ?",
                    (source_event_id,),
                )
                row = connection.execute(
                    "SELECT * FROM surface_events WHERE source_event_id = ?",
                    (source_event_id,),
                ).fetchone()
            return _event_from_row(row)

    def mark_responded(
        self,
        source_event_id: str,
        response: str | bytes,
        *,
        at: datetime | None = None,
    ) -> SurfaceEvent:
        _require_identity(source_event_id, "source_event_id")
        response_hash = _content_hash(response, "response")
        responded_at = _utc(at if at is not None else self._clock(), "responded_at")
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM surface_events WHERE source_event_id = ?",
                (source_event_id,),
            ).fetchone()
            if row is None:
                raise SurfaceEventError("source event was not recorded")
            event = _event_from_row(row)
            if event.state == "expired":
                raise SurfaceEventError("expired source event cannot receive a response")
            if not event.received_at <= responded_at < event.retention_until:
                raise SurfaceEventError(
                    "source event response time must be within its retention period"
                )
            if event.response_hash is not None:
                if event.response_hash != response_hash:
                    raise SurfaceEventConflictError(
                        "source event response is immutable"
                    )
                return event
            connection.execute(
                """
                UPDATE surface_events
                SET state = 'responded', response_hash = ?, responded_at = ?
                WHERE source_event_id = ?
                """,
                (response_hash, _format_time(responded_at), source_event_id),
            )
            row = connection.execute(
                "SELECT * FROM surface_events WHERE source_event_id = ?",
                (source_event_id,),
            ).fetchone()
            return _event_from_row(row)

    def expire_due(self, *, at: datetime | None = None) -> int:
        """Mark retained receipts expired without deleting their audit identity."""

        deadline = _utc(at if at is not None else self._clock(), "expiry time")
        with self._database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE surface_events
                SET state = 'expired'
                WHERE state != 'expired' AND retention_until <= ?
                """,
                (_format_time(deadline),),
            )
            return int(cursor.rowcount)


class LocalSurfaceOutbox:
    """Small owner-only client outbox used before a local CLI submits text."""

    def __init__(self, path: str | Path) -> None:
        self.path = _prepare_outbox_path(path)
        self._lock_path = self.path.with_name(f".{self.path.name}.lock")
        _ensure_owner_only_lock_file(self._lock_path)
        if self.path.exists():
            if not _verify_owner_only_permissions(self.path):
                _apply_owner_only_permissions(self.path)
                if not _verify_owner_only_permissions(self.path):
                    raise SurfaceEventError(
                        "local surface outbox permissions are not owner-only"
                    )

    def verify_owner_only_permissions(self) -> bool:
        """Read back the local file permission boundary on this host."""

        return self.path.is_file() and _verify_owner_only_permissions(self.path)

    def prepare(self, *, surface: str, session_id: str, payload: str | bytes) -> str:
        _require_identity(surface, "surface")
        _require_identity(session_id, "session_id")
        payload_hash = _content_hash(payload, "payload")
        with _outbox_operation_lock(self._lock_path):
            data = self._read()
            pending = data["pending"]
            key = _outbox_key(surface, session_id, payload_hash)
            current = pending.get(key)
            if current is not None:
                if (
                    current.get("surface") != surface
                    or current.get("session_id") != session_id
                    or current.get("payload_hash") != payload_hash
                ):
                    raise SurfaceEventError(
                        "local surface outbox entry is inconsistent"
                    )
                return str(current["source_event_id"])

            source_event_id = f"{surface}:{uuid4()}"
            pending[key] = {
                "surface": surface,
                "session_id": session_id,
                "source_event_id": source_event_id,
                "payload_hash": payload_hash,
            }
            self._write(data)
            return source_event_id

    def acknowledge(self, source_event_id: str) -> None:
        _require_identity(source_event_id, "source_event_id")
        with _outbox_operation_lock(self._lock_path):
            data = self._read()
            matches = [
                key
                for key, value in data["pending"].items()
                if value.get("source_event_id") == source_event_id
            ]
            if len(matches) != 1:
                raise SurfaceEventError(
                    "source event is not pending in the local outbox"
                )
            del data["pending"][matches[0]]
            self._write(data)

    def _read(self) -> dict[str, object]:
        _assert_no_reparse_components(self.path)
        if not self.path.exists():
            return {"format_version": _OUTBOX_FORMAT, "pending": {}}
        if self.path.is_symlink() or not self.path.is_file():
            raise SurfaceEventError("local surface outbox path is unsafe")
        try:
            descriptor = _open_safe_existing_file(self.path)
            with os.fdopen(descriptor, "r", encoding="utf-8") as source:
                raw = json.load(source)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise SurfaceEventError("local surface outbox is unreadable") from error
        if (
            not isinstance(raw, dict)
            or set(raw) != {"format_version", "pending"}
            or raw.get("format_version") != _OUTBOX_FORMAT
            or not isinstance(raw.get("pending"), dict)
        ):
            raise SurfaceEventError("local surface outbox format is invalid")
        for key, value in raw["pending"].items():
            if (
                not isinstance(key, str)
                or not isinstance(value, dict)
                or set(value)
                != {"surface", "session_id", "source_event_id", "payload_hash"}
                or _outbox_key(
                    value.get("surface"),
                    value.get("session_id"),
                    value.get("payload_hash"),
                )
                != key
                or not isinstance(value.get("source_event_id"), str)
                or not _SHA256.fullmatch(str(value.get("payload_hash", "")))
            ):
                raise SurfaceEventError("local surface outbox entry is invalid")
        return raw

    def _write(self, data: dict[str, object]) -> None:
        _assert_no_reparse_components(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _assert_no_reparse_components(self.path)
        if self.path.is_symlink():
            raise SurfaceEventError("local surface outbox path is unsafe")
        encoded = json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        descriptor, temporary = _new_owner_only_temporary_file(
            self.path.parent,
            prefix=f".{self.path.name}.",
        )
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            if not _verify_owner_only_permissions(temporary):
                raise SurfaceEventError(
                    "local surface outbox temporary file is not owner-only"
                )
            os.replace(temporary, self.path)
            if not _verify_owner_only_permissions(self.path):
                raise SurfaceEventError(
                    "local surface outbox permissions are not owner-only"
                )
            if os.name != "nt":
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except OSError as error:
            raise SurfaceEventError("local surface outbox could not be written") from error
        finally:
            temporary.unlink(missing_ok=True)


def _require_identity(
    value: object,
    field_name: str,
    *,
    allow_whitespace: bool = False,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_ID_LENGTH
        or "\x00" in value
        or any(ord(character) < 32 for character in value)
        or (not allow_whitespace and value != value.strip())
    ):
        raise SurfaceEventError(f"{field_name} is invalid")
    return value


def _content_hash(value: str | bytes, field_name: str) -> str:
    if isinstance(value, str):
        encoded = value.encode("utf-8")
    elif isinstance(value, bytes):
        encoded = value
    else:
        raise SurfaceEventError(f"{field_name} must be text or bytes")
    return hashlib.sha256(encoded).hexdigest()


def _bound_payload_hash(
    owner_host: str,
    working_directory: str | None,
    payload: str | bytes,
) -> str:
    """Bind payload bytes to the host and exact trusted routing directory."""

    payload_digest = _content_hash(payload, "payload")
    directory_bytes = (
        b"\x00"
        if working_directory is None
        else b"\x01" + working_directory.encode("utf-8")
    )
    directory_digest = hashlib.sha256(directory_bytes).hexdigest()
    encoded = (
        "forge-surface-event-payload/v1\0"
        f"{owner_host}\0{directory_digest}\0{payload_digest}"
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def surface_event_payload_hash(
    context: TrustedTurnContext,
    payload: str | bytes,
) -> str:
    """Return the exact host/directory-bound digest used by a source receipt."""

    if not isinstance(context, TrustedTurnContext):
        raise SurfaceEventError("context must be a TrustedTurnContext")
    return _bound_payload_hash(
        context.owner_host,
        context.working_directory,
        payload,
    )


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SurfaceEventError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_time(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise SurfaceEventError(f"stored {field_name} is invalid")
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError:
        raise SurfaceEventError(f"stored {field_name} is invalid") from None
    if _format_time(parsed) != value:
        raise SurfaceEventError(f"stored {field_name} is not canonical")
    return parsed


def _event_from_row(row: object) -> SurfaceEvent:
    if row is None:
        raise SurfaceEventError("source event was not recorded")
    try:
        response_hash = row["response_hash"]
        event = SurfaceEvent(
            source_event_id=str(row["source_event_id"]),
            subject_id=str(row["subject_id"]),
            session_id=str(row["session_id"]),
            surface=str(row["surface"]),
            payload_hash=str(row["payload_hash"]),
            state=str(row["state"]),
            received_at=_parse_time(row["received_at"], "received_at"),
            response_hash=None if response_hash is None else str(response_hash),
            responded_at=(
                None
                if row["responded_at"] is None
                else _parse_time(row["responded_at"], "responded_at")
            ),
            retention_until=_parse_time(row["retention_until"], "retention_until"),
        )
    except (IndexError, KeyError, TypeError) as error:
        raise SurfaceEventError("stored source event is invalid") from error
    if not _SHA256.fullmatch(event.payload_hash) or (
        event.response_hash is not None and not _SHA256.fullmatch(event.response_hash)
    ):
        raise SurfaceEventError("stored source event hash is invalid")
    if event.state not in {"received", "handled", "responded", "expired"}:
        raise SurfaceEventError("stored source event state is invalid")
    if event.retention_until <= event.received_at:
        raise SurfaceEventError("stored source event retention is invalid")
    if (event.response_hash is None) != (event.responded_at is None):
        raise SurfaceEventError("stored source event response is inconsistent")
    if event.state in {"received", "handled"} and event.response_hash is not None:
        raise SurfaceEventError("stored source event response state is inconsistent")
    if event.state == "responded" and event.response_hash is None:
        raise SurfaceEventError("stored source event response state is inconsistent")
    if event.responded_at is not None and not (
        event.received_at <= event.responded_at < event.retention_until
    ):
        raise SurfaceEventError("stored source event response time is invalid")
    return event


def _outbox_key(surface: object, session_id: object, payload_hash: object) -> str:
    surface_text = _require_identity(surface, "surface")
    session_text = _require_identity(session_id, "session_id")
    if not isinstance(payload_hash, str) or not _SHA256.fullmatch(payload_hash):
        raise SurfaceEventError("payload_hash is invalid")
    return hashlib.sha256(
        f"{surface_text}\0{session_text}\0{payload_hash}".encode("utf-8")
    ).hexdigest()


def _prepare_outbox_path(path: str | Path) -> Path:
    try:
        candidate = Path(path).expanduser().absolute()
        _assert_no_reparse_components(candidate)
        candidate.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _assert_no_reparse_components(candidate)
    except SurfaceEventError:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise SurfaceEventError("local surface outbox path is unsafe") from error
    if not candidate.parent.is_dir():
        raise SurfaceEventError("local surface outbox parent is not a directory")
    if candidate.exists() and not candidate.is_file():
        raise SurfaceEventError("local surface outbox path is unsafe")
    return candidate


def _assert_no_reparse_components(path: Path) -> None:
    try:
        for component in (path, *path.parents):
            if not os.path.lexists(component):
                continue
            file_stat = os.lstat(component)
            is_reparse = bool(
                os.name == "nt"
                and getattr(file_stat, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            )
            is_junction = bool(
                getattr(component, "is_junction", lambda: False)()
            )
            if stat.S_ISLNK(file_stat.st_mode) or is_reparse or is_junction:
                raise SurfaceEventError(
                    "local surface outbox path and parents cannot be symlinks or reparse points"
                )
    except SurfaceEventError:
        raise
    except (OSError, ValueError) as error:
        raise SurfaceEventError("local surface outbox path is unsafe") from error


def _ensure_owner_only_lock_file(path: Path) -> None:
    _assert_no_reparse_components(path)
    descriptor: int | None = None
    if not path.exists():
        try:
            descriptor = _create_owner_only_file(path)
        except FileExistsError:
            descriptor = None
    if descriptor is not None:
        try:
            os.ftruncate(descriptor, 1)
            os.fsync(descriptor)
        except OSError as error:
            raise SurfaceEventError(
                "local surface outbox lock could not be initialized"
            ) from error
        finally:
            os.close(descriptor)
    _assert_no_reparse_components(path)
    if not path.is_file():
        raise SurfaceEventError("local surface outbox lock is unsafe")
    if not _verify_owner_only_permissions(path):
        _apply_owner_only_permissions(path)
        if not _verify_owner_only_permissions(path):
            raise SurfaceEventError(
                "local surface outbox lock permissions are not owner-only"
            )


def _open_safe_existing_file(path: Path, *, writable: bool = False) -> int:
    _assert_no_reparse_components(path)
    flags = (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOINHERIT", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise SurfaceEventError("local surface outbox path is unsafe") from error
    try:
        descriptor_stat = os.fstat(descriptor)
        path_stat = os.lstat(path)
        if (
            not stat.S_ISREG(descriptor_stat.st_mode)
            or stat.S_ISLNK(path_stat.st_mode)
            or (descriptor_stat.st_dev, descriptor_stat.st_ino)
            != (path_stat.st_dev, path_stat.st_ino)
            or not _verify_owner_only_permissions(path)
        ):
            raise SurfaceEventError("local surface outbox path is unsafe")
        _assert_no_reparse_components(path)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _outbox_thread_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path))
    with _OUTBOX_THREAD_LOCKS_GUARD:
        return _OUTBOX_THREAD_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _outbox_operation_lock(path: Path) -> Iterator[None]:
    thread_lock = _outbox_thread_lock(path)
    with thread_lock:
        descriptor = _open_safe_existing_file(path, writable=True)
        deadline = time.monotonic() + _OUTBOX_LOCK_TIMEOUT_SECONDS
        acquired = False
        try:
            while not acquired:
                try:
                    if os.name == "nt":
                        import msvcrt

                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except (BlockingIOError, OSError) as error:
                    if time.monotonic() >= deadline:
                        raise SurfaceEventError(
                            "local surface outbox lock timed out"
                        ) from error
                    time.sleep(_OUTBOX_LOCK_RETRY_SECONDS)
            yield
        finally:
            try:
                if acquired:
                    if os.name == "nt":
                        import msvcrt

                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError as error:
                raise SurfaceEventError(
                    "local surface outbox lock could not be released"
                ) from error
            finally:
                os.close(descriptor)


def _new_owner_only_temporary_file(
    directory: Path,
    *,
    prefix: str,
) -> tuple[int, Path]:
    for _attempt in range(128):
        path = directory / f"{prefix}{uuid4().hex}.tmp"
        try:
            return _create_owner_only_file(path), path
        except FileExistsError:
            continue
    raise SurfaceEventError("local surface outbox temporary file could not be reserved")


def _create_owner_only_file(path: Path) -> int:
    if os.name == "nt":
        return _windows_create_owner_only_file(path)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags, 0o600)


def _windows_create_owner_only_file(path: Path) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _, sid = _windows_identity()
    security_descriptor = wintypes.LPVOID()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    local_free = kernel32.LocalFree
    local_free.argtypes = (wintypes.LPVOID,)
    local_free.restype = wintypes.LPVOID
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.ULONG),
    )
    convert.restype = wintypes.BOOL
    if not convert(
        f"O:{sid}D:P(A;;FA;;;{sid})",
        1,
        ctypes.byref(security_descriptor),
        None,
    ):
        raise OSError(
            ctypes.get_last_error(),
            "owner-only security descriptor could not be built",
        )

    class SecurityAttributes(ctypes.Structure):
        _fields_ = (
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        )

    attributes = SecurityAttributes(
        ctypes.sizeof(SecurityAttributes), security_descriptor, False
    )
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(SecurityAttributes),
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    try:
        handle = create_file(
            str(path),
            0x80000000 | 0x40000000,
            0x00000001 | 0x00000002 | 0x00000004,
            ctypes.byref(attributes),
            1,
            0x00000080,
            None,
        )
        error_code = ctypes.get_last_error()
    finally:
        local_free(security_descriptor)
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        if error_code in {80, 183}:
            raise FileExistsError(error_code, "file already exists", str(path))
        raise OSError(error_code, "owner-only file could not be created", str(path))
    try:
        return msvcrt.open_osfhandle(
            int(handle),
            os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0),
        )
    except OSError:
        close_handle(handle)
        raise


def _apply_owner_only_permissions(path: Path) -> None:
    if os.name != "nt":
        try:
            os.chmod(path, 0o600, follow_symlinks=False)
        except (NotImplementedError, OSError) as error:
            raise SurfaceEventError("local surface outbox mode could not be restricted") from error
        return
    _, sid = _windows_identity()
    try:
        result = subprocess.run(
            [
                "icacls.exe",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"*{sid}:(F)",
                "/Q",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise SurfaceEventError("local surface outbox ACL could not be restricted") from error
    if result.returncode != 0:
        raise SurfaceEventError("local surface outbox ACL could not be restricted")


def _verify_owner_only_permissions(path: Path) -> bool:
    try:
        if os.name != "nt":
            file_stat = path.stat()
            expected_uid = getattr(os, "geteuid", lambda: file_stat.st_uid)()
            return stat.S_IMODE(file_stat.st_mode) == 0o600 and file_stat.st_uid == expected_uid
        sddl = _windows_security_descriptor_sddl(path)
        owner_match = re.search(r"O:(S-1(?:-\d+)+)", sddl, re.IGNORECASE)
        dacl_match = re.search(r"D:([^\(]*)(.*)$", sddl, re.IGNORECASE)
        if owner_match is None or dacl_match is None:
            return False
        owner_sid = owner_match.group(1).casefold()
        dacl_flags, ace_text = dacl_match.groups()
        aces = re.findall(r"\(([^\)]*)\)", ace_text)
        return "P" in dacl_flags.upper() and bool(aces) and all(
            len(parts := ace.split(";")) == 6
            and parts[0].upper() == "A"
            and "ID" not in parts[1].upper()
            and parts[2].upper() in {"FA", "0X1F01FF"}
            and parts[5].casefold() == owner_sid
            for ace in aces
        )
    except (OSError, SurfaceEventError, ValueError):
        return False


def _windows_identity() -> tuple[str, str]:
    try:
        result = subprocess.run(
            ["whoami.exe", "/user", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise SurfaceEventError("current Windows identity could not be read") from error
    if result.returncode != 0:
        raise SurfaceEventError("current Windows identity could not be read")
    rows = list(csv.reader(result.stdout.splitlines()))
    if len(rows) != 1 or len(rows[0]) != 2:
        raise SurfaceEventError("current Windows identity output is invalid")
    account, sid = (value.strip() for value in rows[0])
    if not account or re.fullmatch(r"S-1(?:-\d+)+", sid, re.IGNORECASE) is None:
        raise SurfaceEventError("current Windows identity output is invalid")
    return account, sid


def _windows_security_descriptor_sddl(path: Path) -> str:
    import ctypes
    from ctypes import wintypes

    security_descriptor = wintypes.LPVOID()
    get_named_security_info = ctypes.windll.advapi32.GetNamedSecurityInfoW
    get_named_security_info.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPVOID),
    )
    get_named_security_info.restype = wintypes.DWORD
    result = get_named_security_info(
        str(path),
        1,
        0x00000001 | 0x00000004,
        None,
        None,
        None,
        None,
        ctypes.byref(security_descriptor),
    )
    if result != 0 or not security_descriptor:
        raise SurfaceEventError("local surface outbox ACL could not be verified")
    sddl_pointer = wintypes.LPWSTR()
    convert = ctypes.windll.advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
    convert.argtypes = (
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.DWORD),
    )
    convert.restype = wintypes.BOOL
    try:
        if not convert(
            security_descriptor,
            1,
            0x00000001 | 0x00000004,
            ctypes.byref(sddl_pointer),
            None,
        ):
            raise SurfaceEventError("local surface outbox ACL could not be verified")
        return str(sddl_pointer.value)
    finally:
        if sddl_pointer:
            ctypes.windll.kernel32.LocalFree(sddl_pointer)
        ctypes.windll.kernel32.LocalFree(security_descriptor)
