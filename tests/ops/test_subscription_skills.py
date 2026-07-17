from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SKILLS = {
    "codex": "codex-skill",
    "claude-code": "claude-skill",
}
FORBIDDEN = (
    "openai_api_key",
    "anthropic_api_key",
    "api key",
    "provider call",
    "tmux",
    "pty",
    "background",
    "retry",
    "codex exec",
    "claude -p",
    "--api-key",
    "--mcp-config",
    "--permission-mode",
)


@pytest.mark.parametrize(("skill_name", "mode"), SKILLS.items())
def test_managed_subscription_skill_has_local_override_contract(
    skill_name: str, mode: str
) -> None:
    skill_path = ROOT / "forge" / "skills" / skill_name / "SKILL.md"

    assert skill_path.is_file(), f"missing repo-managed skill: {skill_path}"
    content = skill_path.read_text(encoding="utf-8")
    windows_call = (
        "& $env:INFINITY_FORGE_SUBSCRIPTION_PYTHON "
        f"$env:INFINITY_FORGE_SUBSCRIPTION_RUNNER {mode} "
        "--workspace $workspace --prompt-file $promptFile"
    )
    linux_call = (
        '"$INFINITY_FORGE_SUBSCRIPTION_PYTHON" '
        f'"$INFINITY_FORGE_SUBSCRIPTION_RUNNER" {mode} '
        '--workspace "$workspace" --prompt-file "$prompt_file"'
    )

    assert f"name: {skill_name}" in content
    assert "platforms: [windows, linux, macos]" in content
    assert "구독" in content
    assert windows_call in content
    assert linux_call in content
    assert "INFINITY_FORGE_SUBSCRIPTION_PYTHON" in content
    assert "INFINITY_FORGE_SUBSCRIPTION_RUNNER" in content
    assert "Test-Path -LiteralPath $workspace -PathType Container" in content
    assert '[[ "$workspace" = /* && -d "$workspace" ]]' in content
    assert "[System.IO.Path]::IsPathRooted($workspace)" in content
    assert "[System.Text.UTF8Encoding]::new($false)" in content
    assert "GetRandomFileName" in content
    assert "mktemp" in content
    assert "icacls" in content
    assert "chmod 600" in content
    assert "finally" in content
    assert "trap" in content
    assert "Remove-Item -LiteralPath $promptFile -Force" in content
    assert 'rm -f "$prompt_file"' in content
    assert "$exitCode = $LASTEXITCODE" in content
    assert "exit $exitCode" in content
    assert "exit_code=$?" in content
    assert 'exit "$exit_code"' in content
    assert "선택될 때에만" in content
    assert "일반 Hermes chat" in content
    assert "구독 CLI 로그인" in content
    assert "runner가 인증·한도 분류를 담당" in content
    assert "작업 프롬프트를 명령 인수로 전달하지 않는다" in content
    assert all(forbidden not in content.lower() for forbidden in FORBIDDEN)


def test_codex_skill_reports_the_runner_final_runtime_and_fallback() -> None:
    content = (ROOT / "forge" / "skills" / "codex" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "최종 런타임" in content
    assert "fallback" in content


def test_claude_code_skill_never_mentions_or_routes_to_codex_skill() -> None:
    content = (ROOT / "forge" / "skills" / "claude-code" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "codex-skill" not in content
    assert "다른 런타임으로 라우팅하지 않는다" in content
