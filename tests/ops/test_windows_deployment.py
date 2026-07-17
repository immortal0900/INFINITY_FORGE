from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
WINDOWS_DEPLOY = ROOT / "forge" / "scripts" / "deploy-windows.ps1"


def _script() -> str:
    return WINDOWS_DEPLOY.read_text(encoding="utf-8")


def test_windows_adapter_parses_as_powershell() -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is unavailable")
    quoted_path = str(WINDOWS_DEPLOY).replace("'", "''")

    result = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-Command",
            "[void][scriptblock]::Create((Get-Content -Raw -LiteralPath "
            f"'{quoted_path}'))",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_windows_adapter_has_separate_read_only_and_write_phases() -> None:
    script = _script()

    assert '[ValidateSet("Preflight", "Apply", "Verify")]' in script
    assert "function Test-ForgeWindowsPreflight" in script
    assert "function Install-ForgeWindowsRelease" in script
    assert "function Test-ForgeWindowsRuntime" in script
    assert script.index('"Preflight" {') < script.index('"Apply" {')
    assert script.index('"Apply" {') < script.index('"Verify" {')


def test_preflight_rejects_missing_tools_and_active_legacy_tasks() -> None:
    script = _script()

    for contract in (
        "venv\\Scripts\\python.exe",
        "venv\\Scripts\\hermes.exe",
        "Get-Command gh.exe",
        "git -C $Repo rev-parse $Commit",
        "Get-HermesRuntimeFingerprint -Paths $paths",
        "LEGACY_ACTIVE",
        "github-issue:%",
        "forge-stage:%",
        "executor",
        "critic",
        "issuefinder",
    ):
        assert contract in script


def test_windows_release_is_archive_based_and_atomically_promoted() -> None:
    script = _script()

    assert "git -C $Repo archive --format=zip" in script
    assert 'Join-Path $paths.ReleaseRoot $Commit' in script
    assert "deployment-source.json" in script
    assert "Move-Item -LiteralPath $releaseTemp -Destination $releasePath" in script
    assert "Copy-Item -Recurse -Force $Repo" not in script
    assert "Remove-Item -Recurse -Force $paths.ReleaseRoot" not in script


def test_windows_hermes_package_uses_live_runtime_snapshot() -> None:
    script = _script()

    assert "$HermesChangeTargets = @(" in script
    assert "function Get-HermesRuntimeFingerprint" in script
    assert (
        "Copy-Item -LiteralPath (Join-Path $Paths.HermesRoot $target)"
        in script
    )
    assert "git -C $paths.HermesRoot archive --format=zip" not in script
    assert "install-hermes-change.py" in script
    assert '"build"' in script
    assert '"install"' in script
    assert '"restore"' in script
    assert "installed-files-list.json" in script
    assert '$packageVersion = "$Commit-$hermesRuntimeVersion"' in script


def test_previous_package_is_restored_before_new_runtime_snapshot() -> None:
    script = _script()
    start = script.index("function Invoke-ForgeWindowsApply")
    apply = script[start:]

    previous = apply.index("$oldPackage = Get-PreviousHermesPackage")
    restore = apply.index('-Action "restore" -Package $oldPackage')
    snapshot = apply.index("$package = New-HermesChangePackage")

    assert previous < restore < snapshot
    previous_function = script[
        script.index("function Get-PreviousHermesPackage") : start
    ]
    assert "$Paths.StateFile" in previous_function
    assert "$state.packagePath" in previous_function


def test_windows_hermes_package_temp_names_do_not_repeat_full_shas() -> None:
    script = _script()

    for long_name in (
        '".$packageVersion.source-$suffix"',
        '".$packageVersion.build-$suffix"',
        '".$packageVersion.archive-$suffix.zip"',
    ):
        assert long_name not in script
    for short_name in ('"._s-$suffix"', '"._b-$suffix"'):
        assert short_name in script
    assert '"._a-$suffix.zip"' not in script


def test_windows_env_updates_are_narrow_and_do_not_print_env() -> None:
    script = _script()

    assert "from hermes_cli.config import save_env_value" in script
    for key in (
        "INFINITY_FORGE_REPOSITORY",
        "INFINITY_FORGE_TASK_SETTINGS_DB",
        "INFINITY_FORGE_GH_PATH",
    ):
        assert key in script
    assert "Get-Content $paths.EnvFile" not in script
    assert "Write-Host $env:" not in script
    assert "RISK(security)" in script


def test_windows_plugin_is_built_as_sibling_then_enabled_noninteractively() -> None:
    script = _script()

    assert "release-path.txt" in script
    assert '"$($paths.PluginDir).staging-' in script
    assert "Move-Item -LiteralPath $pluginTemp -Destination $paths.PluginDir" in script
    assert "plugins enable infinity-forge --no-allow-tool-override" in script
    assert "RISK(data-loss)" in script


def test_windows_gateway_running_state_is_preserved_on_success_and_failure() -> None:
    script = _script()

    assert "$gatewayWasRunning = Test-HermesGatewayRunning" in script
    assert "Stop-HermesGateway" in script
    assert "Start-HermesGateway" in script
    assert "Restore-WindowsDeploymentTransaction" in script
    assert "expectedGatewayRunning" in script
    assert "RISK(side-effect)" in script


def test_windows_profiles_and_skills_match_linux_roles() -> None:
    script = _script()

    for profile in ("builder", "reviewer", "deep_checker", "fix"):
        assert profile in script
    for skill in (
        "forge-ops",
        "memex",
        "code-design-principles",
        "forge-labels",
        "easy-answer",
        "code-problem-doc",
        "build-task",
        "review-task",
        "deep-check",
        "fix-task",
    ):
        assert skill in script


def test_windows_verification_checks_runtime_contracts_without_creating_task() -> None:
    script = _script()

    for contract in (
        "plugins list --enabled --user --plain",
        "INFINITY_FORGE_PRE_USER_TURN_V1",
        "TASK_CONTENT_TEMPLATE",
        "[SPEC-NNN]",
        "[AC-01]",
        "## 확정된 제약",
        "PRAGMA quick_check",
        "after_file_hash",
        "expectedGatewayRunning",
    ):
        assert contract in script
    assert "create_task" not in script
    assert "gh issue create" not in script


def test_windows_adapter_persists_non_secret_state_for_separate_verify() -> None:
    script = _script()

    assert "windows-deployment-state.json" in script
    assert "ConvertTo-Json" in script
    assert "commit = $Commit" in script
    assert "gatewayWasRunning = $gatewayWasRunning" in script
    assert "token" not in script.lower()
    assert "secret" not in script.lower()
