from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
WINDOWS_DEPLOY = ROOT / "forge" / "scripts" / "deploy-windows.ps1"


def _script() -> str:
    return WINDOWS_DEPLOY.read_text(encoding="utf-8")


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def _new_hermes_change_package_source() -> str:
    script = _script()
    return script[
        script.index("function New-HermesChangePackage") : script.index(
            "function Get-PreviousHermesPackage"
        )
    ]


def _run_reused_package_harness(
    powershell: str,
    function_source: str,
    sandbox: Path,
) -> subprocess.CompletedProcess[str]:
    sandbox.mkdir()
    harness = sandbox / "reused-package-harness.ps1"
    package_root = sandbox / "packages"
    harness.write_text(
        """param([Parameter(Mandatory = $true)][string]$PackageRoot)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$Commit = "1111111111111111111111111111111111111111"

function Get-HermesRuntimeFingerprint {
  param([Parameter(Mandatory = $true)][pscustomobject]$Paths)
  return "runtime"
}

"""
        + function_source
        + """
$targets = @("alpha.ts", "beta.ts", "gamma.ts")
$paths = [pscustomobject]@{
  PackageRoot = $PackageRoot
  HermesChangeTargets = $targets
}
$packageVersion = "$Commit-runtime"
$packagePath = Join-Path $PackageRoot $packageVersion
$manifestPath = Join-Path $packagePath "installed-files-list.json"
New-Item -ItemType Directory -Force -Path $packagePath | Out-Null

function Write-TestManifest {
  param([Parameter(Mandatory = $true)][string[]]$Targets)
  $files = @($Targets | ForEach-Object { [pscustomobject]@{ path = $_ } })
  [pscustomobject]@{
    source_version = $packageVersion
    files = $files
  } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $manifestPath -Encoding utf8
}

Write-TestManifest -Targets @($targets[2], $targets[1], $targets[0])
$rejected = $false
try {
  $null = New-HermesChangePackage -Paths $paths -ReleasePath $PackageRoot
} catch {
  if ($_.Exception.Message -ne "Existing Hermes change package has unexpected target paths.") {
    throw
  }
  $rejected = $true
}
if (-not $rejected) { throw "REORDERED_MANIFEST_WAS_ACCEPTED" }

Write-TestManifest -Targets $targets
$result = New-HermesChangePackage -Paths $paths -ReleasePath $PackageRoot
if ($result.Version -ne $packageVersion) { throw "ORDERED_VERSION_MISMATCH" }
if ([IO.Path]::GetFullPath([string]$result.Path) -ne [IO.Path]::GetFullPath($packagePath)) {
  throw "ORDERED_PATH_MISMATCH"
}
"BEHAVIOR_OK"
""",
        encoding="utf-8-sig",
    )
    return subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(harness),
            "-PackageRoot",
            str(package_root),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )


def test_windows_adapter_parses_as_powershell() -> None:
    pwsh = _powershell()
    if pwsh is None:
        pytest.skip("PowerShell is unavailable")
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

    target_manifest = (
        ROOT / "forge" / "hermes_change" / "targets.json"
    )
    targets = json.loads(target_manifest.read_text(encoding="utf-8"))

    assert len(targets) == 19
    assert len(targets) == len(set(targets))
    assert "$HermesChangeTargets = @(" not in script
    assert '"$Commit`:forge/hermes_change/targets.json"' in script
    assert "$Paths.HermesChangeTargets" in script
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


def test_windows_reused_package_rejects_reordered_target_manifest() -> None:
    package_function = _new_hermes_change_package_source()

    assert "Compare-Object" not in package_function
    assert (
        "$manifestTargets.Count -ne $Paths.HermesChangeTargets.Count"
        in package_function
    )
    assert "[StringComparison]::Ordinal" in package_function
    assert "$manifestTargets[$index]" in package_function
    assert "$Paths.HermesChangeTargets[$index]" in package_function


def test_windows_reused_package_order_check_executes_actual_function(
    tmp_path: Path,
) -> None:
    powershell = _powershell()
    if powershell is None:
        pytest.skip("PowerShell is unavailable")
    function_source = _new_hermes_change_package_source()

    actual = _run_reused_package_harness(
        powershell,
        function_source,
        tmp_path / "actual",
    )
    assert actual.returncode == 0, actual.stderr
    assert "BEHAVIOR_OK" in actual.stdout

    mutation_anchor = "if (-not [string]::Equals("
    assert function_source.count(mutation_anchor) == 1
    mutated_source = function_source.replace(
        mutation_anchor,
        "if ($false -and -not [string]::Equals(",
        1,
    )
    mutated = _run_reused_package_harness(
        powershell,
        mutated_source,
        tmp_path / "mutated",
    )
    assert mutated.returncode != 0
    assert "REORDERED_MANIFEST_WAS_ACCEPTED" in (mutated.stdout + mutated.stderr)


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
    assert "${LOCALAPPDATA}\\hermes\\infinity-forge\\task-settings.db" in script
    assert "from dotenv import dotenv_values" in script
    assert 'environment["INFINITY_FORGE_TASK_SETTINGS_DB"]' in script
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


def test_windows_plugin_verification_reads_exact_enabled_config() -> None:
    script = _script()

    assert "from hermes_cli.config import load_config" in script
    assert '"infinity-forge" in enabled_plugins' in script
    assert "plugins list --enabled --user --plain" not in script


def test_windows_profile_provisioning_preserves_existing_profiles() -> None:
    script = _script()
    start = script.index("function Install-InfinityForgeProfilesAndSkills")
    end = script.index("function Enable-InfinityForgePlugin", start)
    provisioning = script[start:end]

    assert '"create", "builder", "--clone-from", "reviewer"' in provisioning
    assert '"create", "deep_checker", "--clone-from", "reviewer"' in provisioning
    assert '"create", "fix", "--clone-from", "builder"' in provisioning
    assert '"rename"' not in provisioning
    assert '"delete"' not in provisioning
    assert "issuefinder" not in provisioning
    assert "RISK(data-loss)" not in provisioning


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
        "from hermes_cli.config import load_config",
        "from dotenv import dotenv_values",
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
