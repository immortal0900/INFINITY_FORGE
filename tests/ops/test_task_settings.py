from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

import pytest

import forge.ops.task_database as task_database_module
from forge.ops.task_options import MergeMode, TaskFlow
from forge.ops.task_settings import (
    TASK_SETTINGS_FORMAT,
    BranchRefreshIntent,
    TaskContent,
    TaskSettings,
    TaskSettingsError,
    TaskSettingsEventType,
    TaskSettingsStatus,
    TaskSettingsStore,
    task_content_hash,
    task_settings_hash,
)


CONFIRMED_AT = datetime(2026, 7, 16, 9, 30, tzinfo=UTC)


def _content() -> TaskContent:
    return TaskContent(
        title="Task 설정 저장",
        description="확인한 선택을 바꾸지 않고 저장한다.",
        acceptance_criteria=(
            "같은 요청은 한 번만 준비한다.",
            "활성 설정은 수정하지 않는다.",
        ),
    )


def _prepared(
    *,
    request_id: str | None = None,
    merge_mode: MergeMode = MergeMode.SAFE_AUTO,
    confirmed_at: datetime = CONFIRMED_AT,
    auto_merge_expires_at: datetime | None | object = ...,
) -> TaskSettings:
    values: dict[str, object] = {}
    if auto_merge_expires_at is not ...:
        values["auto_merge_expires_at"] = auto_merge_expires_at
    return TaskSettings.create(
        request_id=request_id or str(uuid4()),
        repository="openai/infinity-forge",
        task_content=_content(),
        task_flow=TaskFlow.BUILD_REVIEW,
        merge_mode=merge_mode,
        confirmed_by="hermes-user-7",
        confirmed_at=confirmed_at,
        **values,
    )


def _activate_one(store: TaskSettingsStore) -> TaskSettings:
    prepared = store.prepare(_prepared())
    store.bind_issue(
        prepared.request_id,
        42,
        occurred_at=CONFIRMED_AT + timedelta(minutes=1),
    )
    return store.activate(
        prepared.request_id,
        occurred_at=CONFIRMED_AT + timedelta(minutes=2),
    )


def _run_together(
    *callbacks: Callable[[], TaskSettings],
) -> tuple[TaskSettings | BaseException, ...]:
    barrier = threading.Barrier(len(callbacks))

    def invoke(callback: Callable[[], TaskSettings]) -> TaskSettings | BaseException:
        barrier.wait(timeout=5)
        try:
            return callback()
        except BaseException as error:  # noqa: BLE001 - 결과를 함께 비교한다.
            return error

    with ThreadPoolExecutor(max_workers=len(callbacks)) as pool:
        return tuple(pool.map(invoke, callbacks))


def test_task_content_hash_uses_canonical_utf8_json() -> None:
    content = _content()
    canonical = json.dumps(
        {
            "acceptance_criteria": list(content.acceptance_criteria),
            "description": content.description,
            "title": content.title,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    assert task_content_hash(content) == hashlib.sha256(canonical).hexdigest()
    assert (
        task_content_hash(content)
        == "5d5bd28e8ff50a52de088e27c00e1a6f96e3b77b5c9b0d1211e92946cebc034b"
    )


def test_task_settings_hash_is_canonical_and_excludes_status() -> None:
    prepared = _prepared(request_id="9f7453ce-36ec-4e8e-9dfa-bb159b58c19b")
    bound = replace(prepared, issue_number=42)
    expected = hashlib.sha256(
        json.dumps(
            {
                "auto_merge_expires_at": "2026-07-16T21:30:00Z",
                "confirmed_at": "2026-07-16T09:30:00Z",
                "confirmed_by": "hermes-user-7",
                "format_version": TASK_SETTINGS_FORMAT,
                "issue_number": 42,
                "merge_mode": "safe_auto",
                "mode": "task",
                "repository": "openai/infinity-forge",
                "request_id": "9f7453ce-36ec-4e8e-9dfa-bb159b58c19b",
                "task_content_hash": task_content_hash(_content()),
                "task_flow": "build_review",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    assert task_settings_hash(bound) == expected
    assert expected == (
        "ce04fe1beb53b5b1e7c1a6ed5321a4bb025aa93cd3b1ce3f18ef8c2a90f0c2f1"
    )
    assert (
        task_settings_hash(replace(bound, status=TaskSettingsStatus.ACTIVE)) == expected
    )


def test_auto_merge_expiry_is_exactly_twelve_hours_by_default() -> None:
    for merge_mode in (MergeMode.SAFE_AUTO, MergeMode.FULL_AUTO):
        settings = _prepared(merge_mode=merge_mode)
        assert settings.auto_merge_expires_at == CONFIRMED_AT + timedelta(hours=12)


def test_manual_has_no_auto_merge_expiry() -> None:
    settings = _prepared(merge_mode=MergeMode.MANUAL)

    assert settings.auto_merge_expires_at is None


def test_auto_merge_expiry_may_be_shorter_but_never_longer_than_twelve_hours() -> None:
    shorter = _prepared(
        auto_merge_expires_at=CONFIRMED_AT + timedelta(hours=2),
    )
    assert shorter.auto_merge_expires_at == CONFIRMED_AT + timedelta(hours=2)

    with pytest.raises(TaskSettingsError, match="no later than 12 hours"):
        _prepared(auto_merge_expires_at=CONFIRMED_AT + timedelta(hours=12, seconds=1))


def test_manual_rejects_auto_merge_expiry() -> None:
    with pytest.raises(TaskSettingsError, match="manual merge_mode requires"):
        _prepared(
            merge_mode=MergeMode.MANUAL,
            auto_merge_expires_at=CONFIRMED_AT + timedelta(hours=1),
        )


@pytest.mark.parametrize("merge_mode", [MergeMode.SAFE_AUTO, MergeMode.FULL_AUTO])
def test_automatic_merge_requires_future_expiry(merge_mode: MergeMode) -> None:
    with pytest.raises(TaskSettingsError, match="requires auto_merge_expires_at"):
        _prepared(merge_mode=merge_mode, auto_merge_expires_at=None)
    with pytest.raises(TaskSettingsError, match="must be after confirmed_at"):
        _prepared(
            merge_mode=merge_mode,
            auto_merge_expires_at=CONFIRMED_AT,
        )


def test_auto_merge_expiry_is_normalized_to_utc() -> None:
    kst = timezone(timedelta(hours=9))
    settings = _prepared(
        auto_merge_expires_at=(CONFIRMED_AT + timedelta(hours=2)).astimezone(kst),
    )

    assert settings.auto_merge_expires_at == CONFIRMED_AT + timedelta(hours=2)
    assert settings.auto_merge_expires_at.tzinfo is UTC


def test_task_settings_are_frozen_and_reject_old_or_untyped_values() -> None:
    settings = _prepared()
    with pytest.raises(FrozenInstanceError):
        settings.repository = "other/repository"  # type: ignore[misc]
    with pytest.raises(TaskSettingsError, match="format_version"):
        replace(settings, format_version="forge-policy/v1")
    with pytest.raises(TaskSettingsError, match="task_flow must be a TaskFlow"):
        replace(settings, task_flow="reviewed")  # type: ignore[arg-type]


def test_task_content_hash_rejects_wrong_type_with_domain_error() -> None:
    settings = _prepared()

    with pytest.raises(TaskSettingsError, match="task_content_hash"):
        replace(settings, task_content_hash=None)  # type: ignore[arg-type]


def test_same_instant_uses_one_utc_hash_representation() -> None:
    kst = timezone(timedelta(hours=9))
    first = _prepared(
        request_id="9f7453ce-36ec-4e8e-9dfa-bb159b58c19b",
        confirmed_at=CONFIRMED_AT,
    )
    second = _prepared(
        request_id=first.request_id,
        confirmed_at=CONFIRMED_AT.astimezone(kst),
    )

    assert first.confirmed_at == second.confirmed_at
    assert second.confirmed_at.tzinfo is UTC
    assert (
        replace(first, issue_number=7).task_settings_hash
        == replace(second, issue_number=7).task_settings_hash
    )


def test_store_requires_prepare_then_bind_then_activate(tmp_path: Path) -> None:
    store = TaskSettingsStore(tmp_path / "task-settings.db")
    prepared = store.prepare(_prepared())

    with pytest.raises(TaskSettingsError, match="issue must be bound"):
        store.activate(prepared.request_id)

    bound = store.bind_issue(prepared.request_id, 42)
    active = store.activate(prepared.request_id)

    assert bound.issue_number == 42
    assert bound.status is TaskSettingsStatus.PREPARED
    assert bound.task_settings_hash is not None
    assert active.status is TaskSettingsStatus.ACTIVE
    assert active.task_settings_hash == bound.task_settings_hash
    assert store.get_active(prepared.request_id) == active
    assert [event.event_type for event in store.list_events(prepared.request_id)] == [
        TaskSettingsEventType.PREPARED,
        TaskSettingsEventType.ISSUE_BOUND,
        TaskSettingsEventType.ACTIVE,
    ]


def test_duplicate_request_replay_returns_current_state_without_new_events(
    tmp_path: Path,
) -> None:
    store = TaskSettingsStore(tmp_path / "task-settings.db")
    original = _prepared(request_id="9f7453ce-36ec-4e8e-9dfa-bb159b58c19b")
    store.prepare(original)
    store.bind_issue(original.request_id, 42)
    active = store.activate(original.request_id)

    replay = store.prepare(original)

    assert replay == active
    assert len(store.list_events(original.request_id)) == 3


def test_duplicate_request_with_changed_settings_is_rejected(tmp_path: Path) -> None:
    store = TaskSettingsStore(tmp_path / "task-settings.db")
    original = _prepared(request_id="9f7453ce-36ec-4e8e-9dfa-bb159b58c19b")
    store.prepare(original)

    with pytest.raises(TaskSettingsError, match="different settings"):
        store.prepare(replace(original, merge_mode=MergeMode.FULL_AUTO))


def test_issue_binding_replay_is_safe_but_a_different_issue_is_immutable(
    tmp_path: Path,
) -> None:
    store = TaskSettingsStore(tmp_path / "task-settings.db")
    prepared = store.prepare(_prepared())
    first = store.bind_issue(prepared.request_id, 42)

    assert store.bind_issue(prepared.request_id, 42) == first
    with pytest.raises(TaskSettingsError, match="immutable"):
        store.bind_issue(prepared.request_id, 43)
    assert len(store.list_events(prepared.request_id)) == 2


def test_concurrent_issue_binding_replay_returns_one_bound_value(
    tmp_path: Path,
) -> None:
    store = TaskSettingsStore(tmp_path / "task-settings.db")
    prepared = store.prepare(_prepared())

    outcomes = _run_together(
        *(lambda: store.bind_issue(prepared.request_id, 42) for _ in range(4))
    )

    assert all(isinstance(outcome, TaskSettings) for outcome in outcomes), outcomes
    assert {
        outcome.issue_number
        for outcome in outcomes
        if isinstance(outcome, TaskSettings)
    } == {42}
    assert len(store.list_events(prepared.request_id)) == 2


def test_concurrent_activation_replay_returns_one_active_value(tmp_path: Path) -> None:
    store = TaskSettingsStore(tmp_path / "task-settings.db")
    prepared = store.prepare(_prepared())
    store.bind_issue(prepared.request_id, 42)

    outcomes = _run_together(
        *(lambda: store.activate(prepared.request_id) for _ in range(4))
    )

    assert all(isinstance(outcome, TaskSettings) for outcome in outcomes), outcomes
    assert {
        outcome.status for outcome in outcomes if isinstance(outcome, TaskSettings)
    } == {TaskSettingsStatus.ACTIVE}
    assert len(store.list_events(prepared.request_id)) == 3


def test_concurrent_same_lifecycle_replay_returns_terminal_value(
    tmp_path: Path,
) -> None:
    store = TaskSettingsStore(tmp_path / "task-settings.db")
    active = _activate_one(store)

    outcomes = _run_together(
        *(
            lambda: store.append_lifecycle_event(
                active.request_id,
                TaskSettingsStatus.MERGED,
            )
            for _ in range(4)
        )
    )

    assert all(isinstance(outcome, TaskSettings) for outcome in outcomes), outcomes
    assert {
        outcome.status for outcome in outcomes if isinstance(outcome, TaskSettings)
    } == {TaskSettingsStatus.MERGED}
    assert len(store.list_events(active.request_id)) == 4


def test_concurrent_conflicting_lifecycle_events_commit_exactly_one(
    tmp_path: Path,
) -> None:
    store = TaskSettingsStore(tmp_path / "task-settings.db")
    active = _activate_one(store)

    outcomes = _run_together(
        lambda: store.append_lifecycle_event(
            active.request_id,
            TaskSettingsStatus.CANCELLED,
        ),
        lambda: store.append_lifecycle_event(
            active.request_id,
            TaskSettingsStatus.MERGED,
        ),
    )

    successes = [outcome for outcome in outcomes if isinstance(outcome, TaskSettings)]
    errors = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert len(successes) == 1, outcomes
    assert len(errors) == 1, outcomes
    assert isinstance(errors[0], TaskSettingsError)
    assert "lifecycle status is immutable" in str(errors[0])
    terminal_events = [
        event
        for event in store.list_events(active.request_id)
        if event.event_type
        in {
            TaskSettingsEventType.CANCELLED,
            TaskSettingsEventType.EXPIRED,
            TaskSettingsEventType.MERGED,
        }
    ]
    assert len(terminal_events) == 1


def test_active_guard_serializes_external_write_with_lifecycle_end(
    tmp_path: Path,
) -> None:
    store = TaskSettingsStore(tmp_path / "task-settings.db")
    active = _activate_one(store)
    cancel_started = threading.Event()

    def cancel() -> TaskSettings | BaseException:
        cancel_started.set()
        try:
            return store.append_lifecycle_event(
                active.request_id,
                TaskSettingsStatus.CANCELLED,
            )
        except BaseException as error:  # noqa: BLE001 - compare the race result.
            return error

    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.guard_active(active) as guard:
            pending_cancel = pool.submit(cancel)
            assert cancel_started.wait(timeout=1)
            with pytest.raises(FutureTimeout):
                pending_cancel.result(timeout=0.05)
            merged = guard.finish(TaskSettingsStatus.MERGED)
        cancel_result = pending_cancel.result(timeout=1)

    assert merged.status is TaskSettingsStatus.MERGED
    assert isinstance(cancel_result, TaskSettingsError)
    assert "lifecycle status is immutable" in str(cancel_result)
    assert store.get_active(active.request_id) is None


def test_active_guard_rejects_a_task_that_already_ended(tmp_path: Path) -> None:
    store = TaskSettingsStore(tmp_path / "task-settings.db")
    active = _activate_one(store)
    store.append_lifecycle_event(
        active.request_id,
        TaskSettingsStatus.CANCELLED,
    )

    with pytest.raises(TaskSettingsError, match="no longer active"):
        with store.guard_active(active):
            raise AssertionError("guard must not open")


def test_branch_refresh_intent_is_durable_and_exact_replay_is_idempotent(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "task-settings.db"
    store = TaskSettingsStore(database_path)
    active = _activate_one(store)
    expected_base = "a" * 40
    expected_head = "b" * 40

    reserved = store.reserve_branch_refresh(
        active.request_id,
        pr_url="https://github.com/openai/infinity-forge/pull/42",
        expected_base_commit=expected_base,
        expected_head_commit=expected_head,
        applied_refresh_count=0,
        occurred_at=CONFIRMED_AT + timedelta(minutes=3),
    )
    replayed = TaskSettingsStore(database_path).reserve_branch_refresh(
        active.request_id,
        pr_url=reserved.pr_url,
        expected_base_commit=expected_base,
        expected_head_commit=expected_head,
        applied_refresh_count=0,
        occurred_at=CONFIRMED_AT + timedelta(hours=1),
    )

    assert isinstance(reserved, BranchRefreshIntent)
    assert reserved.refresh_number == 1
    assert reserved.completed is False
    assert replayed == reserved
    assert (
        store.get_branch_refresh_replay(active.request_id, applied_refresh_count=0)
        == reserved
    )


def test_branch_refresh_intent_completion_is_durable_and_exactly_replayable(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "task-settings.db"
    store = TaskSettingsStore(database_path)
    active = _activate_one(store)
    intent = store.reserve_branch_refresh(
        active.request_id,
        pr_url="https://github.com/openai/infinity-forge/pull/42",
        expected_base_commit="a" * 40,
        expected_head_commit="b" * 40,
        applied_refresh_count=0,
    )

    completed = store.complete_branch_refresh(
        intent,
        current_base_commit="c" * 40,
        current_head_commit="d" * 40,
        occurred_at=CONFIRMED_AT + timedelta(minutes=4),
    )
    replayed = TaskSettingsStore(database_path).complete_branch_refresh(
        intent,
        current_base_commit="c" * 40,
        current_head_commit="d" * 40,
        occurred_at=CONFIRMED_AT + timedelta(hours=1),
    )

    assert completed.completed is True
    assert completed.current_base_commit == "c" * 40
    assert completed.current_head_commit == "d" * 40
    assert replayed == completed
    assert (
        store.get_branch_refresh_replay(active.request_id, applied_refresh_count=0)
        == completed
    )
    assert store.get_branch_refresh_replay(
        active.request_id,
        applied_refresh_count=1,
    ) is None


def test_active_guard_completes_refresh_in_its_transaction(tmp_path: Path) -> None:
    store = TaskSettingsStore(tmp_path / "task-settings.db")
    active = _activate_one(store)
    intent = store.reserve_branch_refresh(
        active.request_id,
        pr_url="https://github.com/openai/infinity-forge/pull/42",
        expected_base_commit="a" * 40,
        expected_head_commit="b" * 40,
        applied_refresh_count=0,
    )

    with pytest.raises(RuntimeError, match="simulate crash"):
        with store.guard_active(active) as guard:
            guard.complete_branch_refresh(
                intent,
                current_base_commit="c" * 40,
                current_head_commit="d" * 40,
            )
            raise RuntimeError("simulate crash")

    assert store.get_branch_refresh_replay(
        active.request_id,
        applied_refresh_count=0,
    ) == intent

    with store.guard_active(active) as guard:
        completed = guard.complete_branch_refresh(
            intent,
            current_base_commit="c" * 40,
            current_head_commit="d" * 40,
        )

    assert completed.completed is True
    assert store.get_branch_refresh_replay(
        active.request_id,
        applied_refresh_count=0,
    ) == completed


def test_branch_refresh_intent_rejects_conflict_and_unproven_count(
    tmp_path: Path,
) -> None:
    store = TaskSettingsStore(tmp_path / "task-settings.db")
    active = _activate_one(store)
    intent = store.reserve_branch_refresh(
        active.request_id,
        pr_url="https://github.com/openai/infinity-forge/pull/42",
        expected_base_commit="a" * 40,
        expected_head_commit="b" * 40,
        applied_refresh_count=0,
    )

    with pytest.raises(TaskSettingsError, match="different pull request state"):
        store.reserve_branch_refresh(
            active.request_id,
            pr_url=intent.pr_url,
            expected_base_commit="a" * 40,
            expected_head_commit="e" * 40,
            applied_refresh_count=0,
        )
    with pytest.raises(TaskSettingsError, match="has no durable proof"):
        store.get_branch_refresh_replay(
            active.request_id,
            applied_refresh_count=2,
        )
    with pytest.raises(TaskSettingsError, match="different result"):
        completed = store.complete_branch_refresh(
            intent,
            current_base_commit="c" * 40,
            current_head_commit="d" * 40,
        )
        store.complete_branch_refresh(
            completed,
            current_base_commit="c" * 40,
            current_head_commit="e" * 40,
        )


def test_branch_refresh_intent_cap_cannot_be_bypassed_by_replay(
    tmp_path: Path,
) -> None:
    store = TaskSettingsStore(tmp_path / "task-settings.db")
    active = _activate_one(store)
    for applied_count, head in enumerate(("b", "c", "d")):
        intent = store.reserve_branch_refresh(
            active.request_id,
            pr_url="https://github.com/openai/infinity-forge/pull/42",
            expected_base_commit="a" * 40,
            expected_head_commit=head * 40,
            applied_refresh_count=applied_count,
        )
        store.complete_branch_refresh(
            intent,
            current_base_commit="a" * 40,
            current_head_commit=chr(ord(head) + 1) * 40,
        )

    with pytest.raises(TaskSettingsError, match="limit"):
        store.reserve_branch_refresh(
            active.request_id,
            pr_url="https://github.com/openai/infinity-forge/pull/42",
            expected_base_commit="a" * 40,
            expected_head_commit="e" * 40,
            applied_refresh_count=3,
        )


def test_lifecycle_changes_append_events_without_changing_settings(
    tmp_path: Path,
) -> None:
    store = TaskSettingsStore(tmp_path / "task-settings.db")
    active = _activate_one(store)

    cancelled = store.append_lifecycle_event(
        active.request_id,
        TaskSettingsStatus.CANCELLED,
        occurred_at=CONFIRMED_AT + timedelta(minutes=3),
    )

    assert cancelled.status is TaskSettingsStatus.CANCELLED
    assert cancelled.task_settings_hash == active.task_settings_hash
    assert cancelled.repository == active.repository
    assert store.get_active(active.request_id) is None
    event_types = [event.event_type for event in store.list_events(active.request_id)]
    assert event_types[-1] is TaskSettingsEventType.CANCELLED

    with sqlite3.connect(tmp_path / "task-settings.db") as connection:
        stored_rows = connection.execute(
            "SELECT COUNT(*) FROM task_settings"
        ).fetchone()
    assert stored_rows == (1,)


def test_replace_api_explicitly_refuses_setting_mutation(tmp_path: Path) -> None:
    store = TaskSettingsStore(tmp_path / "task-settings.db")
    active = _activate_one(store)

    with pytest.raises(TaskSettingsError, match="immutable"):
        store.replace(active.request_id, merge_mode=MergeMode.FULL_AUTO)


def test_noncanonical_request_id_and_naive_time_are_rejected() -> None:
    with pytest.raises(TaskSettingsError, match="canonical UUID"):
        _prepared(request_id="9F7453CE-36EC-4E8E-9DFA-BB159B58C19B")
    with pytest.raises(TaskSettingsError, match="timezone-aware"):
        _prepared(confirmed_at=datetime(2026, 7, 16, 9, 30))


def test_store_resolves_relative_path_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(first)
    store = TaskSettingsStore("task-settings.db")
    expected_path = (first / "task-settings.db").resolve()

    monkeypatch.chdir(second)
    prepared = store.prepare(_prepared())

    assert store.database_path == expected_path
    assert expected_path.is_file()
    assert not (second / "task-settings.db").exists()
    assert prepared.status is TaskSettingsStatus.PREPARED


def test_store_creates_missing_parent_directory(tmp_path: Path) -> None:
    database_path = tmp_path / "missing" / "nested" / "task-settings.db"

    TaskSettingsStore(database_path)

    assert database_path.is_file()


def test_store_rejects_directory_as_database_path(tmp_path: Path) -> None:
    database_path = tmp_path / "task-settings.db"
    database_path.mkdir()

    with pytest.raises(TaskSettingsError, match="regular file"):
        TaskSettingsStore(database_path)


def test_store_rejects_symbolic_link_database_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_path = tmp_path / "target.db"
    TaskSettingsStore(target_path)
    link_path = tmp_path / "linked.db"
    real_exists = Path.exists
    real_is_symlink = Path.is_symlink
    real_resolve = Path.resolve

    def fake_exists(path: Path) -> bool:
        return True if path == link_path else real_exists(path)

    def fake_is_symlink(path: Path) -> bool:
        return True if path == link_path else real_is_symlink(path)

    def fake_resolve(path: Path, strict: bool = False) -> Path:
        return target_path if path == link_path else real_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "exists", fake_exists)
    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)
    monkeypatch.setattr(Path, "resolve", fake_resolve)

    with pytest.raises(TaskSettingsError, match="symbolic link"):
        TaskSettingsStore(link_path)


def test_store_rejects_non_directory_parent(tmp_path: Path) -> None:
    parent = tmp_path / "not-a-directory"
    parent.write_text("file", encoding="utf-8")

    with pytest.raises(TaskSettingsError, match="parent directory"):
        TaskSettingsStore(parent / "task-settings.db")


def test_new_database_has_exact_schema_version_and_terminal_index(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "task-settings.db"
    TaskSettingsStore(database_path)

    with sqlite3.connect(database_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        index_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            ("task_settings_one_terminal_event",),
        ).fetchone()

    assert version == 2
    assert index_sql is not None
    normalized_index_sql = " ".join(index_sql[0].split()).lower()
    assert "unique index" in normalized_index_sql
    assert (
        "where event_type in ('cancelled', 'expired', 'merged')" in normalized_index_sql
    )


def test_store_rejects_unknown_schema_version(tmp_path: Path) -> None:
    database_path = tmp_path / "task-settings.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA user_version = 99")

    with pytest.raises(TaskSettingsError, match="schema version"):
        TaskSettingsStore(database_path)


def test_store_rejects_expected_columns_without_required_constraints(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "task-settings.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE task_settings (
                request_id TEXT PRIMARY KEY,
                format_version TEXT NOT NULL,
                repository TEXT NOT NULL,
                mode TEXT NOT NULL,
                task_content_hash TEXT NOT NULL,
                task_flow TEXT NOT NULL,
                merge_mode TEXT NOT NULL,
                confirmed_by TEXT NOT NULL,
                confirmed_at TEXT NOT NULL,
                auto_merge_expires_at TEXT
            );
            CREATE TABLE task_settings_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                issue_number INTEGER,
                task_settings_hash TEXT
            );
            CREATE UNIQUE INDEX task_settings_one_terminal_event
                ON task_settings_events (request_id)
                WHERE event_type IN ('cancelled', 'expired', 'merged');
            PRAGMA user_version = 1;
            """
        )

    with pytest.raises(TaskSettingsError, match="schema does not match"):
        TaskSettingsStore(database_path)


def test_store_rejects_extra_legacy_column(tmp_path: Path) -> None:
    database_path = tmp_path / "task-settings.db"
    TaskSettingsStore(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("ALTER TABLE task_settings ADD COLUMN assurance_policy TEXT")

    with pytest.raises(TaskSettingsError, match="schema does not match"):
        TaskSettingsStore(database_path)


def test_store_normalizes_locked_database_timeout(tmp_path: Path) -> None:
    database_path = tmp_path / "task-settings.db"
    store = TaskSettingsStore(database_path)
    with sqlite3.connect(database_path) as lock_connection:
        lock_connection.execute("BEGIN IMMEDIATE")

        with pytest.raises(TaskSettingsError, match="database operation failed"):
            store.prepare(_prepared())


def test_store_normalizes_corrupt_database_schema_error(tmp_path: Path) -> None:
    database_path = tmp_path / "task-settings.db"
    database_path.write_bytes(b"not a sqlite database")

    with pytest.raises(TaskSettingsError, match="database operation failed"):
        TaskSettingsStore(database_path)


def test_store_normalizes_commit_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "task-settings.db"
    TaskSettingsStore(database_path)
    real_connect = sqlite3.connect

    class CommitFailingConnection(sqlite3.Connection):
        def commit(self) -> None:
            raise sqlite3.OperationalError("forced commit failure")

    def connect_with_commit_failure(
        *args: object, **kwargs: object
    ) -> sqlite3.Connection:
        kwargs["factory"] = CommitFailingConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(
        task_database_module.sqlite3,
        "connect",
        connect_with_commit_failure,
    )

    with pytest.raises(TaskSettingsError, match="database operation failed"):
        TaskSettingsStore(database_path)


def test_store_normalizes_rollback_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "task-settings.db"
    store = TaskSettingsStore(database_path)
    original = _prepared(request_id="9f7453ce-36ec-4e8e-9dfa-bb159b58c19b")
    store.prepare(original)
    real_connect = sqlite3.connect

    class RollbackFailingConnection(sqlite3.Connection):
        def rollback(self) -> None:
            raise sqlite3.OperationalError("forced rollback failure")

    def connect_with_rollback_failure(
        *args: object, **kwargs: object
    ) -> sqlite3.Connection:
        kwargs["factory"] = RollbackFailingConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(
        task_database_module.sqlite3,
        "connect",
        connect_with_rollback_failure,
    )

    with pytest.raises(TaskSettingsError, match="database operation failed"):
        store.prepare(replace(original, merge_mode=MergeMode.FULL_AUTO))
