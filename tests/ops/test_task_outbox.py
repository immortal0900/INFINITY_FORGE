from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Lock
from time import sleep
from uuid import uuid4

import pytest

import forge.ops.task_outbox as task_outbox_module
from forge.ops.task_options import MergeMode, TaskFlow
from forge.ops.task_outbox import TaskOutbox, TaskOutboxError
from forge.ops.task_service import TaskCreationRequest
from forge.ops.task_settings import TaskContent


NOW = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)


def _request(request_id: str | None = None) -> TaskCreationRequest:
    return TaskCreationRequest(
        request_id=request_id or str(uuid4()),
        repository="openai/infinity-forge",
        content=TaskContent(
            title="Keep confirmed Tasks after a crash",
            description="Save the exact confirmed Task before GitHub is called.",
            acceptance_criteria=(
                "A restart can load the same request.",
                "Only one concurrent worker can deliver it.",
            ),
        ),
        task_flow=TaskFlow.BUILD_REVIEW_DEEP_CHECK,
        merge_mode=MergeMode.FULL_AUTO,
        confirmed_by="hermes-user-7",
        confirmed_at=NOW,
    )


def test_restart_loads_the_exact_canonical_request_and_hash(tmp_path: Path) -> None:
    database = tmp_path / "task-outbox.db"
    request = _request("9f7453ce-36ec-4e8e-9dfa-bb159b58c19b")
    TaskOutbox(database).save(request)

    loaded = TaskOutbox(database).load_pending(request.request_id)

    assert loaded == request
    with sqlite3.connect(database) as connection:
        request_json, request_hash, state = connection.execute(
            "SELECT request_json, request_hash, state FROM task_outbox"
        ).fetchone()
    assert request_json == json.dumps(
        {
            "confirmed_at": "2026-07-16T10:00:00Z",
            "confirmed_by": "hermes-user-7",
            "content": {
                "acceptance_criteria": [
                    "A restart can load the same request.",
                    "Only one concurrent worker can deliver it.",
                ],
                "description": "Save the exact confirmed Task before GitHub is called.",
                "title": "Keep confirmed Tasks after a crash",
            },
            "format_version": "forge-task-outbox/v1",
            "merge_mode": "full_auto",
            "repository": "openai/infinity-forge",
            "request_id": "9f7453ce-36ec-4e8e-9dfa-bb159b58c19b",
            "task_flow": "build_review_deep_check",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert request_hash == hashlib.sha256(request_json.encode("utf-8")).hexdigest()
    assert state == "pending"


def test_failed_delivery_keeps_request_pending_for_restart(tmp_path: Path) -> None:
    database = tmp_path / "task-outbox.db"
    request = _request()
    outbox = TaskOutbox(database)
    outbox.save(request)

    with pytest.raises(OSError, match="GitHub unavailable"):
        with outbox.claim(request.request_id) as claim:
            assert not claim.already_completed
            raise OSError("GitHub unavailable")

    assert TaskOutbox(database).load_pending(request.request_id) == request


def test_process_exit_during_claim_keeps_request_pending(tmp_path: Path) -> None:
    database = tmp_path / "task-outbox.db"
    request_id = "9f7453ce-36ec-4e8e-9dfa-bb159b58c19b"
    script = """
import os
import sys
from datetime import UTC, datetime

from forge.ops.task_options import MergeMode, TaskFlow
from forge.ops.task_outbox import TaskOutbox
from forge.ops.task_service import TaskCreationRequest
from forge.ops.task_settings import TaskContent

request = TaskCreationRequest(
    request_id="9f7453ce-36ec-4e8e-9dfa-bb159b58c19b",
    repository="openai/infinity-forge",
    content=TaskContent(
        title="Keep confirmed Tasks after a crash",
        description="Save the exact confirmed Task before GitHub is called.",
        acceptance_criteria=(
            "A restart can load the same request.",
            "Only one concurrent worker can deliver it.",
        ),
    ),
    task_flow=TaskFlow.BUILD_REVIEW_DEEP_CHECK,
    merge_mode=MergeMode.FULL_AUTO,
    confirmed_by="hermes-user-7",
    confirmed_at=datetime(2026, 7, 16, 10, 0, tzinfo=UTC),
)
outbox = TaskOutbox(sys.argv[1])
outbox.save(request)
with outbox.claim(request.request_id):
    os._exit(7)
"""

    child = subprocess.run(
        [sys.executable, "-c", script, str(database)],
        cwd=Path(__file__).resolve().parents[2],
        check=False,
        timeout=10,
    )

    assert child.returncode == 7
    assert TaskOutbox(database).load_pending(request_id) == _request(request_id)


def test_success_marks_complete_and_removes_request_from_pending(
    tmp_path: Path,
) -> None:
    database = tmp_path / "task-outbox.db"
    request = _request()
    outbox = TaskOutbox(database)
    outbox.save(request)

    with outbox.claim(request.request_id) as claim:
        assert claim.request == request
        assert not claim.already_completed
        claim.complete(41)

    assert TaskOutbox(database).load_pending(request.request_id) is None
    assert TaskOutbox(database).load(request.request_id) == request
    with TaskOutbox(database).claim(request.request_id) as completed:
        assert completed.already_completed
        assert completed.issue_number == 41


def test_two_concurrent_claims_deliver_only_once(tmp_path: Path) -> None:
    database = tmp_path / "task-outbox.db"
    request = _request()
    TaskOutbox(database).save(request)
    start = Barrier(3)
    counter_lock = Lock()
    deliveries = 0

    def deliver() -> bool:
        nonlocal deliveries
        outbox = TaskOutbox(database)
        start.wait()
        with outbox.claim(request.request_id) as claim:
            if claim.already_completed:
                return False
            with counter_lock:
                deliveries += 1
            sleep(0.05)
            claim.complete(17)
            return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(deliver) for _ in range(2)]
        start.wait()
        results = [future.result(timeout=2) for future in futures]

    assert results.count(True) == 1
    assert results.count(False) == 1
    assert deliveries == 1


def test_existing_outbox_can_be_opened_for_read_while_delivery_is_claimed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "task-outbox.db"
    request = _request()
    outbox = TaskOutbox(database)
    outbox.save(request)
    real_connect = sqlite3.connect

    def connect_without_wait(*args: object, **kwargs: object) -> sqlite3.Connection:
        kwargs["timeout"] = 0
        return real_connect(*args, **kwargs)

    with outbox.claim(request.request_id) as claim:
        monkeypatch.setattr(
            task_outbox_module.sqlite3,
            "connect",
            connect_without_wait,
        )
        reopened = TaskOutbox(database)

        assert reopened.load_pending(request.request_id) == request
        claim.complete(19)


def test_claim_for_one_request_does_not_block_saving_another_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "task-outbox.db"
    first = _request()
    second = TaskCreationRequest(
        request_id=str(uuid4()),
        repository=first.repository,
        content=first.content,
        task_flow=first.task_flow,
        merge_mode=first.merge_mode,
        confirmed_by="another-user",
        confirmed_at=first.confirmed_at,
    )
    outbox = TaskOutbox(database)
    outbox.save(first)
    real_connect = sqlite3.connect

    def connect_without_wait(*args: object, **kwargs: object) -> sqlite3.Connection:
        kwargs["timeout"] = 0
        return real_connect(*args, **kwargs)

    with outbox.claim(first.request_id) as claim:
        monkeypatch.setattr(
            task_outbox_module.sqlite3,
            "connect",
            connect_without_wait,
        )

        assert TaskOutbox(database).save(second) == second
        claim.complete(20)


def test_same_request_id_cannot_change_confirmed_content(tmp_path: Path) -> None:
    outbox = TaskOutbox(tmp_path / "task-outbox.db")
    request = _request()
    outbox.save(request)
    changed = TaskCreationRequest(
        request_id=request.request_id,
        repository=request.repository,
        content=TaskContent(
            title="Changed title",
            description=request.content.description,
            acceptance_criteria=request.content.acceptance_criteria,
        ),
        task_flow=request.task_flow,
        merge_mode=request.merge_mode,
        confirmed_by=request.confirmed_by,
        confirmed_at=request.confirmed_at,
    )

    with pytest.raises(TaskOutboxError, match="different confirmed Task"):
        outbox.save(changed)


def test_unencodable_confirmed_content_fails_closed(tmp_path: Path) -> None:
    base = _request()
    malformed = TaskCreationRequest(
        request_id=base.request_id,
        repository=base.repository,
        content=TaskContent(
            title=base.content.title,
            description="invalid surrogate: \ud800",
            acceptance_criteria=base.content.acceptance_criteria,
        ),
        task_flow=base.task_flow,
        merge_mode=base.merge_mode,
        confirmed_by=base.confirmed_by,
        confirmed_at=base.confirmed_at,
    )

    with pytest.raises(TaskOutboxError, match="invalid"):
        TaskOutbox(tmp_path / "task-outbox.db").save(malformed)


def test_pending_request_is_found_only_for_the_same_repository_and_user(
    tmp_path: Path,
) -> None:
    outbox = TaskOutbox(tmp_path / "task-outbox.db")
    request = _request()
    outbox.save(request)

    assert (
        outbox.load_pending_for_user(request.repository, request.confirmed_by)
        == request
    )
    assert outbox.load_pending_for_user("other/repo", request.confirmed_by) is None
    assert outbox.load_pending_for_user(request.repository, "other-user") is None


def test_multiple_pending_requests_for_one_user_fail_closed(tmp_path: Path) -> None:
    outbox = TaskOutbox(tmp_path / "task-outbox.db")
    first = _request()
    second = TaskCreationRequest(
        request_id=str(uuid4()),
        repository=first.repository,
        content=first.content,
        task_flow=first.task_flow,
        merge_mode=first.merge_mode,
        confirmed_by=first.confirmed_by,
        confirmed_at=first.confirmed_at,
    )
    outbox.save(first)
    outbox.save(second)

    with pytest.raises(TaskOutboxError, match="more than one pending Task"):
        outbox.load_pending_for_user(first.repository, first.confirmed_by)


def test_tampered_json_or_hash_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "task-outbox.db"
    request = _request()
    TaskOutbox(database).save(request)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE task_outbox SET request_json = replace(request_json, ?, ?)",
            ("full_auto", "manual"),
        )

    with pytest.raises(TaskOutboxError, match="hash does not match"):
        TaskOutbox(database).load(request.request_id)


def test_unknown_json_field_fails_even_with_matching_hash(tmp_path: Path) -> None:
    database = tmp_path / "task-outbox.db"
    request = _request()
    TaskOutbox(database).save(request)
    with sqlite3.connect(database) as connection:
        raw = connection.execute(
            "SELECT request_json FROM task_outbox WHERE request_id = ?",
            (request.request_id,),
        ).fetchone()[0]
        payload = json.loads(raw)
        payload["legacy_assurance"] = "reviewed"
        changed = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            "UPDATE task_outbox SET request_json = ?, request_hash = ?",
            (changed, hashlib.sha256(changed.encode("utf-8")).hexdigest()),
        )

    with pytest.raises(TaskOutboxError, match="fields"):
        TaskOutbox(database).load(request.request_id)


def test_store_rejects_non_exact_schema_and_version(tmp_path: Path) -> None:
    database = tmp_path / "task-outbox.db"
    TaskOutbox(database)
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE task_outbox ADD COLUMN legacy TEXT")

    with pytest.raises(TaskOutboxError, match="schema does not match"):
        TaskOutbox(database)

    other = tmp_path / "other.db"
    with sqlite3.connect(other) as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(TaskOutboxError, match="schema version"):
        TaskOutbox(other)


def test_store_rejects_directory_and_invalid_path(tmp_path: Path) -> None:
    with pytest.raises(TaskOutboxError, match="regular file"):
        TaskOutbox(tmp_path)

    with pytest.raises(TaskOutboxError, match="valid filesystem path"):
        TaskOutbox("bad\x00path")


def test_store_rejects_final_symlink(tmp_path: Path) -> None:

    target = tmp_path / "target.db"
    target.touch()
    link = tmp_path / "linked.db"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable")
    with pytest.raises(TaskOutboxError, match="symbolic link"):
        TaskOutbox(link)


def test_open_store_rejects_database_replaced_by_symlink(tmp_path: Path) -> None:
    database = tmp_path / "task-outbox.db"
    outbox = TaskOutbox(database)
    request = _request()
    outbox.save(request)
    moved = tmp_path / "moved.db"
    database.rename(moved)
    try:
        os.symlink(moved, database)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(TaskOutboxError, match="symbolic link"):
        outbox.load(request.request_id)


def test_open_store_does_not_recreate_a_deleted_database(tmp_path: Path) -> None:
    database = tmp_path / "task-outbox.db"
    outbox = TaskOutbox(database)
    request = _request()
    outbox.save(request)
    database.unlink()

    with pytest.raises(TaskOutboxError, match="missing"):
        outbox.load(request.request_id)

    assert not database.exists()


def test_store_rejects_symlinked_parent(tmp_path: Path) -> None:

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    try:
        os.symlink(real_parent, linked_parent, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symbolic links are unavailable")
    with pytest.raises(TaskOutboxError, match="symbolic link"):
        TaskOutbox(linked_parent / "outbox.db")
