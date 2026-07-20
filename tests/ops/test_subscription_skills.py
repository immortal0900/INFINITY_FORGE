from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SKILLS = {
    "codex": "codex-skill",
    "claude-code": "claude-skill",
}
PAYG_CREDENTIALS = (
    "openai_api_key",
    "anthropic_api_key",
    "api key",
    "api-key",
)


def _frontmatter(content: str) -> str:
    match = re.match(r"\A---\r?\n(?P<frontmatter>.*?)\r?\n---\r?\n", content, re.DOTALL)
    assert match, "frontmatter must be the opening delimited block"
    return match.group("frontmatter")


def _code_blocks(content: str) -> list[str]:
    blocks = re.findall(r"```(?:powershell|bash)\n(.*?)```", content, re.DOTALL)
    assert blocks, "skill must contain executable OS code blocks"
    return blocks


def _block_for(blocks: list[str], token: str) -> str:
    return next(block for block in blocks if token in block)


def _is_direct_provider_cli(line: str) -> bool:
    command = line.strip()
    if not command or command.startswith("#"):
        return False
    command = re.sub(r"^(?:&\s*|command\s+)", "", command)
    match = re.match(r'''(?:"([^"]+)"|'([^']+)'|(\S+))''', command)
    if match is None:
        return False
    executable = next(part for part in match.groups() if part is not None)
    basename = executable.replace("\\", "/").rsplit("/", maxsplit=1)[-1].lower()
    return basename in {"codex", "codex.exe", "claude", "claude.exe"}


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

    frontmatter = _frontmatter(content)
    blocks = _code_blocks(content)
    windows = _block_for(blocks, "INFINITY_FORGE_SUBSCRIPTION_PYTHON")
    posix = _block_for(blocks, 'prompt_file="$(mktemp')

    assert f"name: {skill_name}" in frontmatter.splitlines()
    assert "platforms: [windows, linux, macos]" in frontmatter.splitlines()
    assert "구독" in content
    assert "정확히 하나의 OS 절만" in content
    assert "bash가 필요" in content
    assert windows_call in windows
    assert linux_call in posix
    assert "Test-Path -LiteralPath $workspace -PathType Container" in windows
    assert "Test-FullyQualifiedWindowsPath" in windows
    assert "$path -like '\\\\*'" in windows
    assert "$path -match '^[A-Za-z]:\\\\'" in windows
    assert "FileMode]::CreateNew" in windows
    assert "FileShare]::Read" in windows
    assert "$attempt -lt 10" in windows
    assert "WindowsIdentity]::GetCurrent().Name" in windows
    assert "Get-Command icacls.exe -CommandType Application -ErrorAction Stop" in windows
    assert "$expectedIcacls" in windows
    assert "[System.StringComparison]::OrdinalIgnoreCase" in windows
    assert "required Windows ACL utility is unavailable" in windows
    assert "& $icaclsPath $promptFile /inheritance:r /grant:r \"${identity}:(R,W)\"" in windows
    assert "# RISK(security):" in windows
    assert windows.index("$icacls = Get-Command") < windows.index("$writer.Write($prompt)")
    assert windows.index("& $icaclsPath $promptFile") < windows.index("$writer.Write($prompt)")
    assert windows.index("$writer.Dispose()") < windows.index("$promptStream.Dispose()")
    assert windows.index("$promptStream.Dispose()") < windows.index("Remove-Item -LiteralPath $promptFile -Force")
    assert "$exitCode = $LASTEXITCODE" in windows
    assert "exit $exitCode" in windows
    assert "prompt_file=''" in posix
    assert "trap cleanup EXIT" in posix
    assert posix.index("trap cleanup EXIT") < posix.index('prompt_file="$(mktemp')
    assert 'mktemp "${TMPDIR:-/tmp}/infinity-forge.XXXXXX"' in posix
    assert "|| exit 70" in posix
    assert "chmod 600 \"$prompt_file\" || exit 70" in posix
    assert "printf '%s' \"$prompt\" > \"$prompt_file\" || exit 70" in posix
    assert 'rm -f "$prompt_file"' in posix
    assert "exit_code=$?" in posix
    assert 'exit "$exit_code"' in posix
    assert "선택될 때에만" in content
    assert "일반 Hermes chat" in content
    assert "구독 CLI 로그인" in content
    assert "runner가 인증·한도 분류를 담당" in content
    assert "작업 프롬프트를 명령 인수로 전달하지 않는다" in content
    assert all(credential not in content.lower() for credential in PAYG_CREDENTIALS)
    assert content.count(windows_call) == 1
    assert content.count(linux_call) == 1
    assert not any(
        _is_direct_provider_cli(line)
        for block in blocks
        for line in block.splitlines()
    )


@pytest.mark.parametrize(
    "line",
    (
        '& "codex" exec',
        '&"codex" exec',
        "& '.\\claude.exe' -p",
        "&'C:\\Tools\\claude.exe' -p",
        '& "C:\\Tools\\codex.exe" exec',
        "command claude",
    ),
)
def test_direct_provider_cli_detector_rejects_quoted_and_path_commands(line: str) -> None:
    assert _is_direct_provider_cli(line)


@pytest.mark.parametrize(
    "line",
    (
        "codex-skill --workspace workspace",
        "claude-skill --workspace workspace",
        "& $env:INFINITY_FORGE_SUBSCRIPTION_PYTHON $env:INFINITY_FORGE_SUBSCRIPTION_RUNNER",
    ),
)
def test_direct_provider_cli_detector_allows_runner_modes(line: str) -> None:
    assert not _is_direct_provider_cli(line)


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
