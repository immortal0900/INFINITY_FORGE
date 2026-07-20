from __future__ import annotations

import pytest

from forge.ops.stop_command import StopCommand, parse_stop_command


@pytest.mark.parametrize(
    ("text", "issue_number"),
    (
        ("forge stop", None),
        ("forge stop #21", 21),
        ("#21 실행 중단", 21),
        ("#21 작업 중단해", 21),
        ("현재 Task 멈춰", None),
        ("\t forge stop #21 \t", 21),
    ),
)
def test_only_the_five_exact_full_stop_commands_are_recognized(
    text: str,
    issue_number: int | None,
) -> None:
    assert parse_stop_command(text) == StopCommand(issue_number=issue_number)


@pytest.mark.parametrize(
    "text",
    (
        "",
        "forge  stop",
        "forge\tstop",
        "Forge stop",
        "FORGE STOP",
        "forge stop #0",
        "forge stop #01",
        "forge stop #-1",
        "forge stop #9223372036854775808",
        "forge stop #21 now",
        "please forge stop #21",
        "forge stop #21?",
        "forge stop이라는 명령이 있어?",
        "forge stop 하지 마",
        "#21 실행 중단하지 마",
        "'#21 실행 중단'",
        '"forge stop"',
        "`forge stop`",
        "```\nforge stop\n```",
        "> forge stop",
        "설명: #21 작업 중단해",
        "현재 task 멈춰",
        "현재 Task를 멈춰",
        "현재 Task 멈춰?",
        "forge stop\n",
        "\nforge stop",
        "forge stop\r\n",
        "현재 Task 멈춰\n다음 문장",
        "ｆｏｒｇｅ stop",
        "#２１ 실행 중단",
        "#21 실행중단",
    ),
)
def test_questions_negation_quotes_code_substrings_and_normalization_are_chat(
    text: str,
) -> None:
    assert parse_stop_command(text) is None


@pytest.mark.parametrize("value", (None, 1.5, b"forge stop", object()))
def test_non_text_is_never_a_stop_command(value: object) -> None:
    assert parse_stop_command(value) is None
