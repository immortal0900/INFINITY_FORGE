from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Lock
from time import sleep

import pytest

from forge.ops.task_options import MergeMode, Mode, TaskFlow
from forge.ops.task_setup import SetupStep, TaskSetup, TurnResult, begin_task_setup


NOW = datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc)
REPOSITORY = "owner/repo"
REQUEST_ID = "12345678-1234-4123-8123-123456789abc"


def test_chat_replays_first_message_exactly_once_without_external_writes() -> None:
    setup = TaskSetup()
    first_input = "  설명해줘\n둘째 줄도 그대로  "

    chooser = setup.handle("s1", "u1", first_input, NOW)
    replay = setup.handle("s1", "u1", "chat", NOW + timedelta(seconds=1))
    following_turn = setup.handle("s1", "u1", "다음 질문", NOW + timedelta(seconds=2))

    assert chooser.action == "handled"
    assert chooser.next_step is SetupStep.MODE
    assert replay == TurnResult.replace(first_input)
    assert following_turn == TurnResult.continue_original()


def test_task_requires_flow_then_merge_mode_and_uses_stashed_input() -> None:
    setup = TaskSetup()

    setup.handle("s1", "u1", "버그를 고쳐줘", NOW)
    flow_prompt = setup.handle("s1", "u1", "task", NOW + timedelta(seconds=1))
    merge_prompt = setup.handle(
        "s1", "u1", "build_review", NOW + timedelta(seconds=2)
    )
    preview = setup.handle(
        "s1",
        "u1",
        "safe_auto",
        NOW + timedelta(seconds=3),
        repository=REPOSITORY,
    )
    ready = setup.handle("s1", "u1", "confirm", NOW + timedelta(seconds=4))

    assert flow_prompt.next_step is SetupStep.TASK_FLOW
    assert merge_prompt.next_step is SetupStep.MERGE_MODE
    assert preview.action == "handled"
    assert preview.next_step is SetupStep.CONFIRM
    assert "버그를 고쳐줘" in (preview.text or "")
    assert ready.action == "handled"
    assert ready.selection is not None
    assert ready.selection.mode is Mode.TASK
    assert ready.selection.task_flow is TaskFlow.BUILD_REVIEW
    assert ready.selection.merge_mode is MergeMode.SAFE_AUTO
    assert ready.task_text == "버그를 고쳐줘"
    assert setup.handle("s1", "u1", "일반 대화", NOW + timedelta(seconds=5)).action == "continue"


def test_explicit_task_command_resets_choices_and_requires_task_text() -> None:
    setup = TaskSetup()

    start = setup.handle("s1", "u1", "/task", NOW)
    setup.handle("s1", "u1", "build", NOW + timedelta(seconds=1))
    setup.handle("s1", "u1", "manual", NOW + timedelta(seconds=2))
    reset = setup.handle("s1", "u1", "/task", NOW + timedelta(seconds=3))
    merge_prompt = setup.handle(
        "s1", "u1", "build_review_deep_check", NOW + timedelta(seconds=4)
    )
    content_prompt = setup.handle(
        "s1", "u1", "full_auto", NOW + timedelta(seconds=5)
    )
    preview = setup.handle(
        "s1",
        "u1",
        "새 Task 내용",
        NOW + timedelta(seconds=6),
        repository=REPOSITORY,
    )
    ready = setup.handle("s1", "u1", "confirm", NOW + timedelta(seconds=7))

    assert start.next_step is SetupStep.TASK_FLOW
    assert reset.next_step is SetupStep.TASK_FLOW
    assert merge_prompt.next_step is SetupStep.MERGE_MODE
    assert content_prompt.next_step is SetupStep.TASK_CONTENT
    assert preview.next_step is SetupStep.CONFIRM
    assert ready.selection is not None
    assert ready.selection.task_flow is TaskFlow.BUILD_REVIEW_DEEP_CHECK
    assert ready.selection.merge_mode is MergeMode.FULL_AUTO
    assert ready.task_text == "새 Task 내용"


def test_cancel_discards_task_draft_and_returns_to_chat() -> None:
    setup = begin_task_setup()

    merge_prompt = setup.handle("s1", "u1", "build", NOW)
    cancelled = setup.handle("s1", "u1", "/cancel", NOW + timedelta(seconds=1))
    following_turn = setup.handle("s1", "u1", "대화 계속", NOW + timedelta(seconds=2))

    assert merge_prompt.next_step is SetupStep.MERGE_MODE
    assert cancelled.action == "handled"
    assert cancelled.next_step is None
    assert following_turn.action == "continue"


def test_invalid_choice_stays_on_the_same_required_step() -> None:
    setup = begin_task_setup()

    result = setup.handle("s1", "u1", "direct", NOW)

    assert result.action == "handled"
    assert result.next_step is SetupStep.TASK_FLOW
    assert result.choices == tuple(flow.value for flow in TaskFlow)


def test_inactive_task_draft_expires_after_thirty_minutes() -> None:
    setup = TaskSetup()
    setup.handle("s1", "u1", "첫 요청", NOW)
    setup.handle("s1", "u1", "task", NOW + timedelta(minutes=1))

    result = setup.handle("s1", "u1", "새 요청", NOW + timedelta(minutes=31))

    assert result.action == "handled"
    assert result.next_step is SetupStep.MODE
    assert setup.handle(
        "s1", "u1", "chat", NOW + timedelta(minutes=31, seconds=1)
    ) == TurnResult.replace("새 요청")


def test_each_new_task_requires_fresh_flow_and_merge_choices() -> None:
    setup = TaskSetup()
    setup.handle("s1", "u1", "/task", NOW)
    setup.handle("s1", "u1", "build", NOW + timedelta(seconds=1))
    setup.handle("s1", "u1", "manual", NOW + timedelta(seconds=2))
    setup.handle(
        "s1",
        "u1",
        "첫 Task",
        NOW + timedelta(seconds=3),
        repository=REPOSITORY,
    )
    setup.handle("s1", "u1", "confirm", NOW + timedelta(seconds=4))

    restarted = setup.handle("s1", "u1", "/task", NOW + timedelta(seconds=5))
    invalid_merge_as_flow = setup.handle(
        "s1", "u1", "manual", NOW + timedelta(seconds=6)
    )

    assert restarted.next_step is SetupStep.TASK_FLOW
    assert invalid_merge_as_flow.next_step is SetupStep.TASK_FLOW


def test_task_cannot_finish_without_explicit_confirmation() -> None:
    setup = begin_task_setup()

    setup.handle("s1", "u1", "build", NOW)
    setup.handle("s1", "u1", "manual", NOW + timedelta(seconds=1))
    preview = setup.handle(
        "s1",
        "u1",
        "고칠 내용",
        NOW + timedelta(seconds=2),
        repository=REPOSITORY,
    )
    rejected = setup.handle("s1", "u1", "yes", NOW + timedelta(seconds=3))

    assert preview.next_step is SetupStep.CONFIRM
    assert preview.selection is None
    assert rejected.next_step is SetupStep.CONFIRM
    assert rejected.selection is None


def test_preview_builds_exact_task_request_and_keeps_it_through_confirm() -> None:
    raw_text = "\n  인증 오류 수정  \n- 로그인은 200을 반환한다\n2. 토큰을 로그에 남기지 않는다\n설명"
    setup = begin_task_setup(request_id_factory=lambda: REQUEST_ID)
    setup.handle("s1", "alice", "build_review", NOW)
    setup.handle("s1", "alice", "safe_auto", NOW + timedelta(seconds=1))

    preview = setup.handle(
        "s1",
        "alice",
        raw_text,
        NOW + timedelta(seconds=2),
        repository=REPOSITORY,
    )
    confirmed = setup.handle(
        "s1", "alice", "confirm", NOW + timedelta(seconds=3)
    )

    request = preview.task_request
    assert request is not None
    assert request.request_id == REQUEST_ID
    assert request.repository == REPOSITORY
    assert request.confirmed_by == "alice"
    assert request.confirmed_at == NOW + timedelta(seconds=2)
    assert request.confirmed_at.utcoffset() == timedelta(0)
    assert request.content.title == "인증 오류 수정"
    assert request.content.description == raw_text
    assert request.content.acceptance_criteria == (
        "로그인은 200을 반환한다",
        "토큰을 로그에 남기지 않는다",
    )
    assert confirmed.task_request is request
    assert f"Repository: {REPOSITORY}" in (preview.text or "")
    assert f"Request ID: {REQUEST_ID}" in (preview.text or "")
    assert "Execution path: Build → Review → current commit CI" in (preview.text or "")
    assert "Merge result: Auto-merge safe files after validation" in (preview.text or "")
    assert "2026-07-16T21:00:02Z" in (preview.text or "")


def test_task_content_fallback_is_exact_raw_text_and_title_is_capped() -> None:
    raw_text = " X" * 200 + "\nplain description"
    setup = begin_task_setup(request_id_factory=lambda: REQUEST_ID)
    setup.handle("s1", "alice", "build", NOW)
    setup.handle("s1", "alice", "manual", NOW + timedelta(seconds=1))

    preview = setup.handle(
        "s1",
        "alice",
        raw_text,
        NOW + timedelta(seconds=2),
        repository=REPOSITORY,
    )

    assert preview.task_request is not None
    assert preview.task_request.content.title == raw_text.splitlines()[0].strip()[:256]
    assert preview.task_request.content.acceptance_criteria == (raw_text,)
    assert "Automatic merge permission until: not granted" in (preview.text or "")


@pytest.mark.parametrize(
    ("task_flow", "merge_mode", "expected_path", "expected_merge"),
    [
        ("build", "manual", "Build → current commit CI", "Human merge after validation"),
        (
            "build_review",
            "safe_auto",
            "Build → Review → current commit CI",
            "Auto-merge safe files after validation",
        ),
        (
            "build_review_deep_check",
            "full_auto",
            "Build → Review → Deep Check → current commit CI",
            "Auto-merge any validated pull request",
        ),
    ],
)
def test_preview_shows_actual_flow_and_merge_result(
    task_flow: str,
    merge_mode: str,
    expected_path: str,
    expected_merge: str,
) -> None:
    setup = begin_task_setup(request_id_factory=lambda: REQUEST_ID)
    setup.handle("s1", "alice", task_flow, NOW)
    setup.handle("s1", "alice", merge_mode, NOW + timedelta(seconds=1))

    preview = setup.handle(
        "s1",
        "alice",
        "작업 내용",
        NOW + timedelta(seconds=2),
        repository=REPOSITORY,
    )

    assert f"Execution path: {expected_path}" in (preview.text or "")
    assert f"Merge result: {expected_merge}" in (preview.text or "")


def test_concurrent_chat_choice_replays_first_input_exactly_once(monkeypatch) -> None:
    setup = TaskSetup()
    setup.handle("s1", "u1", "첫 입력", NOW)
    original = setup._handle_mode
    active_lock = Lock()
    active = 0
    max_active = 0

    def slowed_handle_mode(*args, **kwargs):
        nonlocal active, max_active
        with active_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            sleep(0.05)
            return original(*args, **kwargs)
        finally:
            with active_lock:
                active -= 1

    monkeypatch.setattr(setup, "_handle_mode", slowed_handle_mode)
    start = Barrier(3)

    def choose_chat() -> TurnResult:
        start.wait()
        return setup.handle("s1", "u1", "chat", NOW + timedelta(seconds=1))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(choose_chat) for _ in range(2)]
        start.wait()
        results = [future.result(timeout=2) for future in futures]

    assert max_active == 1
    assert results.count(TurnResult.replace("첫 입력")) == 1
    assert results.count(TurnResult.continue_original()) == 1


def test_surface_and_new_session_have_fresh_state() -> None:
    setup = TaskSetup()

    setup.handle("same", "user", "TUI 첫 입력", NOW, surface="tui")
    setup.handle("same", "user", "chat", NOW + timedelta(seconds=1), surface="tui")

    slack = setup.handle(
        "same", "user", "Slack 첫 입력", NOW + timedelta(seconds=2), surface="slack"
    )
    restarted = setup.handle(
        "same",
        "user",
        "새 TUI 입력",
        NOW + timedelta(seconds=3),
        surface="tui",
        is_new_session=True,
    )

    assert slack.next_step is SetupStep.MODE
    assert restarted.next_step is SetupStep.MODE
    assert setup.handle(
        "same", "user", "chat", NOW + timedelta(seconds=4), surface="tui"
    ) == TurnResult.replace("새 TUI 입력")


def test_access_sweeps_other_expired_drafts() -> None:
    setup = TaskSetup()
    setup.handle("old", "user", "오래된 입력", NOW, surface="tui")

    setup.handle(
        "new", "user", "새 입력", NOW + timedelta(minutes=31), surface="tui"
    )

    assert len(setup._drafts) == 1


def test_session_state_is_bounded_by_oldest_activity() -> None:
    setup = TaskSetup(max_tracked_sessions=2)
    for index in range(3):
        setup.handle(
            f"s{index}",
            "user",
            "/cancel",
            NOW + timedelta(seconds=index),
            surface="tui",
        )

    evicted = setup.handle(
        "s0", "user", "다시 시작", NOW + timedelta(seconds=4), surface="tui"
    )

    assert evicted.next_step is SetupStep.MODE
