# INFINITY_FORGE — 검증된 Forge commit을 Windows Hermes에 반영합니다.
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Repo,
  [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$Commit,
  [Parameter(Mandatory = $true)][ValidatePattern('^[^/]+/[^/]+$')][string]$Repository,
  [Parameter(Mandatory = $true)][ValidateSet("Preflight", "Apply", "Verify")][string]$Mode
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$HermesChangeTargets = @(
  "hermes_cli\plugins.py",
  "agent\conversation_loop.py",
  "run_agent.py",
  "cli.py",
  "tui_gateway\server.py",
  "gateway\run.py"
)

function Assert-ExternalCommand {
  param([Parameter(Mandatory = $true)][string]$Message)
  if ($LASTEXITCODE -ne 0) { throw $Message }
}

function Get-HermesRuntimeFingerprint {
  param([Parameter(Mandatory = $true)][pscustomobject]$Paths)
  $fingerprintScript = @'
import hashlib
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
digest = hashlib.sha256()
for relative in sys.argv[2:]:
    digest.update(relative.encode("utf-8"))
    digest.update(b"\0")
    digest.update((root / relative).read_bytes())
print(digest.hexdigest()[:40])
'@
  $fingerprint = (
    & $Paths.HermesPython -c $fingerprintScript `
      $Paths.HermesRoot @HermesChangeTargets
  ).Trim()
  Assert-ExternalCommand "Hermes runtime fingerprint cannot be computed."
  if ($fingerprint -notmatch '^[0-9a-f]{40}$') {
    throw "Hermes runtime fingerprint is invalid."
  }
  return $fingerprint
}

function Remove-DeploymentPath {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$ExpectedParent,
    [Parameter(Mandatory = $true)][string]$ExpectedPrefix
  )
  if (-not (Test-Path -LiteralPath $Path)) { return }
  $resolved = [IO.Path]::GetFullPath($Path)
  $parent = [IO.Path]::GetDirectoryName($resolved)
  $name = [IO.Path]::GetFileName($resolved)
  if ($parent -ne [IO.Path]::GetFullPath($ExpectedParent)) {
    throw "Refusing to remove a path outside its deployment parent."
  }
  if (-not $name.StartsWith($ExpectedPrefix, [StringComparison]::Ordinal)) {
    throw "Refusing to remove a path without the deployment prefix."
  }
  # RISK(data-loss): 임시·backup 경로만 exact parent와 prefix를 확인한 뒤 제거한다.
  Remove-Item -Recurse -Force -LiteralPath $resolved
}

function Invoke-WithReleasePythonPath {
  param(
    [Parameter(Mandatory = $true)][string]$ReleasePath,
    [Parameter(Mandatory = $true)][scriptblock]$Action
  )
  $previous = $env:PYTHONPATH
  $env:PYTHONPATH = $ReleasePath
  try {
    & $Action
  } finally {
    if ($null -eq $previous) {
      [Environment]::SetEnvironmentVariable("PYTHONPATH", $null, "Process")
    } else {
      $env:PYTHONPATH = $previous
    }
  }
}

function Test-ForgeWindowsPreflight {
  if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    throw "LOCALAPPDATA is unavailable."
  }
  $repoPath = [IO.Path]::GetFullPath($Repo)
  $hermesHome = [IO.Path]::GetFullPath(
    (Join-Path $env:LOCALAPPDATA "hermes")
  )
  $hermesRoot = [IO.Path]::GetFullPath(
    (Join-Path $hermesHome "hermes-agent")
  )
  $hermesPython = Join-Path $hermesRoot "venv\Scripts\python.exe"
  $hermesCli = Join-Path $hermesRoot "venv\Scripts\hermes.exe"
  $gh = (Get-Command gh.exe -ErrorAction Stop).Source
  $localRoot = [IO.Path]::GetFullPath(
    (Join-Path $env:LOCALAPPDATA "InfinityForge")
  )
  $taskData = Join-Path $hermesHome "infinity-forge"
  $paths = [pscustomobject]@{
    Repo = $repoPath
    LocalRoot = $localRoot
    ReleaseRoot = (Join-Path $localRoot "releases")
    StateRoot = (Join-Path $localRoot "state")
    StateFile = (Join-Path $localRoot "state\windows-deployment-state.json")
    HermesHome = $hermesHome
    HermesRoot = $hermesRoot
    HermesPython = $hermesPython
    HermesCli = $hermesCli
    Gh = $gh
    EnvFile = (Join-Path $hermesHome ".env")
    PluginRoot = (Join-Path $hermesHome "plugins")
    PluginDir = (Join-Path $hermesHome "plugins\infinity-forge")
    SkillsRoot = (Join-Path $hermesHome "skills")
    ProfilesRoot = (Join-Path $hermesHome "profiles")
    TaskData = $taskData
    TaskSettingsDB = (Join-Path $taskData "task-settings.db")
    TaskOutboxDB = (Join-Path $taskData "task-settings.db.task-outbox.db")
    KanbanDB = (Join-Path $hermesHome "kanban.db")
    PackageRoot = (Join-Path $taskData "hermes-user-turn-changes")
  }

  foreach ($path in @(
    $paths.Repo,
    $paths.HermesRoot,
    $paths.HermesPython,
    $paths.HermesCli,
    $paths.Gh,
    $paths.KanbanDB
  )) {
    if (-not (Test-Path -LiteralPath $path)) {
      throw "Windows preflight path is missing: $path"
    }
  }

  $repoCommit = (& git -C $Repo rev-parse $Commit).Trim()
  Assert-ExternalCommand "Requested Forge commit is unavailable locally."
  if ($repoCommit -ne $Commit) {
    throw "Requested Forge commit did not resolve exactly."
  }
  foreach ($target in $HermesChangeTargets) {
    if (-not (Test-Path -PathType Leaf -LiteralPath (
      Join-Path $paths.HermesRoot $target
    ))) {
      throw "Hermes runtime target is missing: $target"
    }
  }
  $null = Get-HermesRuntimeFingerprint -Paths $paths

  $legacyCheck = @'
import sqlite3
import sys

connection = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
try:
    count = connection.execute(
        """
        SELECT count(*) FROM tasks
        WHERE status NOT IN ('done','failed','cancelled')
          AND (
            coalesce(idempotency_key,'') LIKE 'github-issue:%'
            OR coalesce(idempotency_key,'') LIKE 'forge-stage:%'
            OR assignee IN ('executor','critic','issuefinder')
          )
        """
    ).fetchone()[0]
finally:
    connection.close()
print(count)
'@
  $LEGACY_ACTIVE = (
    & $paths.HermesPython -c $legacyCheck $paths.KanbanDB
  ).Trim()
  Assert-ExternalCommand "Active legacy Task check failed."
  if ($LEGACY_ACTIVE -notmatch '^\d+$' -or [int]$LEGACY_ACTIVE -ne 0) {
    throw "Legacy Task profiles still have active work."
  }
  return $paths
}

function Get-HermesGatewayProcesses {
  param([Parameter(Mandatory = $true)][pscustomobject]$Paths)
  $root = $Paths.HermesRoot.ToLowerInvariant()
  return @(
    Get-CimInstance Win32_Process -ErrorAction Stop |
      Where-Object {
        $_.Name -in @("python.exe", "pythonw.exe") -and
        $_.CommandLine -match 'hermes_cli\.main\s+gateway\s+run' -and
        ($_.CommandLine.ToLowerInvariant().Contains($root) -or
          $_.CommandLine -match 'pythonw?\.exe')
      }
  )
}

function Test-HermesGatewayRunning {
  param([Parameter(Mandatory = $true)][pscustomobject]$Paths)
  return @(Get-HermesGatewayProcesses -Paths $Paths).Count -gt 0
}

function Stop-HermesGateway {
  param([Parameter(Mandatory = $true)][pscustomobject]$Paths)
  if (-not (Test-HermesGatewayRunning -Paths $Paths)) { return }
  # RISK(side-effect): runtime 파일을 바꾸는 동안 새 turn이 시작되지 않게 한다.
  & $Paths.HermesCli gateway stop | Out-Host
  Assert-ExternalCommand "Windows Hermes Gateway stop failed."
  $deadline = [DateTime]::UtcNow.AddSeconds(15)
  while (Test-HermesGatewayRunning -Paths $Paths) {
    if ([DateTime]::UtcNow -ge $deadline) {
      throw "Windows Hermes Gateway did not stop."
    }
    Start-Sleep -Milliseconds 250
  }
}

function Start-HermesGateway {
  param([Parameter(Mandatory = $true)][pscustomobject]$Paths)
  if (Test-HermesGatewayRunning -Paths $Paths) { return }
  # RISK(side-effect): 배포 전에 실행 중이던 Gateway만 다시 시작한다.
  & $Paths.HermesCli gateway start | Out-Host
  Assert-ExternalCommand "Windows Hermes Gateway start failed."
  $deadline = [DateTime]::UtcNow.AddSeconds(20)
  while (-not (Test-HermesGatewayRunning -Paths $Paths)) {
    if ([DateTime]::UtcNow -ge $deadline) {
      throw "Windows Hermes Gateway did not start."
    }
    Start-Sleep -Milliseconds 250
  }
}

function Install-ForgeWindowsRelease {
  param([Parameter(Mandatory = $true)][pscustomobject]$Paths)
  New-Item -ItemType Directory -Force -Path $Paths.ReleaseRoot | Out-Null
  $releasePath = Join-Path $paths.ReleaseRoot $Commit
  $markerPath = Join-Path $releasePath "deployment-source.json"
  if (Test-Path -LiteralPath $releasePath) {
    if (-not (Test-Path -LiteralPath $markerPath)) {
      throw "Existing Windows release is incomplete."
    }
    $marker = Get-Content -Raw -LiteralPath $markerPath | ConvertFrom-Json
    if ($marker.commit -ne $Commit) {
      throw "Existing Windows release has a different commit marker."
    }
    return $releasePath
  }

  $suffix = "$PID-$([guid]::NewGuid().ToString('N'))"
  $releaseTemp = Join-Path $Paths.ReleaseRoot ".$Commit.staging-$suffix"
  $archive = Join-Path $Paths.ReleaseRoot ".$Commit.archive-$suffix.zip"
  try {
    git -C $Repo archive --format=zip "--output=$archive" $Commit
    Assert-ExternalCommand "Forge release archive failed."
    Expand-Archive -LiteralPath $archive -DestinationPath $releaseTemp
    foreach ($required in @(
      (Join-Path $releaseTemp "forge\__init__.py"),
      (Join-Path $releaseTemp "forge\ops\task_setup.py"),
      (Join-Path $releaseTemp "forge\scripts\install-hermes-change.py")
    )) {
      if (-not (Test-Path -LiteralPath $required)) {
        throw "Forge release archive is incomplete."
      }
    }
    [ordered]@{
      formatVersion = 1
      commit = $Commit
      repository = $Repository
      createdAtUtc = [DateTimeOffset]::UtcNow.ToString("o")
    } | ConvertTo-Json | Set-Content -LiteralPath (
      Join-Path $releaseTemp "deployment-source.json"
    ) -Encoding utf8NoBOM
    Move-Item -LiteralPath $releaseTemp -Destination $releasePath
  } finally {
    Remove-DeploymentPath -Path $archive -ExpectedParent $Paths.ReleaseRoot `
      -ExpectedPrefix ".$Commit.archive-"
    Remove-DeploymentPath -Path $releaseTemp -ExpectedParent $Paths.ReleaseRoot `
      -ExpectedPrefix ".$Commit.staging-"
  }
  return $releasePath
}

function New-HermesChangePackage {
  param(
    [Parameter(Mandatory = $true)][pscustomobject]$Paths,
    [Parameter(Mandatory = $true)][string]$ReleasePath
  )
  New-Item -ItemType Directory -Force -Path $Paths.PackageRoot | Out-Null
  $hermesRuntimeVersion = Get-HermesRuntimeFingerprint -Paths $Paths
  $packageVersion = "$Commit-$hermesRuntimeVersion"
  $packagePath = Join-Path $Paths.PackageRoot $packageVersion
  $manifestPath = Join-Path $packagePath "installed-files-list.json"
  if (Test-Path -LiteralPath $packagePath) {
    if (-not (Test-Path -LiteralPath $manifestPath)) {
      throw "Existing Hermes change package is incomplete."
    }
    $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
    if ($manifest.source_version -ne $packageVersion) {
      throw "Existing Hermes change package has a different version."
    }
    return [pscustomobject]@{
      Path = $packagePath
      Version = $packageVersion
      HermesVersion = $hermesRuntimeVersion
    }
  }

  $suffix = "$PID-$([guid]::NewGuid().ToString('N'))"
  # Windows의 legacy MAX_PATH 환경에서도 package 안의 중첩 파일을 만들 수
  # 있도록 두 SHA는 최종 directory에만 두고 sibling 임시 이름은 짧게 유지한다.
  $sourceTemp = Join-Path $Paths.PackageRoot "._s-$suffix"
  $packageTemp = Join-Path $Paths.PackageRoot "._b-$suffix"
  try {
    New-Item -ItemType Directory -Path $sourceTemp | Out-Null
    # Gateway가 중지된 동안 실제 Desktop runtime의 여섯 source만 snapshot한다.
    foreach ($target in $HermesChangeTargets) {
      $destination = Join-Path $sourceTemp $target
      New-Item -ItemType Directory -Force -Path (
        Split-Path -Parent $destination
      ) | Out-Null
      Copy-Item -LiteralPath (Join-Path $Paths.HermesRoot $target) `
        -Destination $destination
    }
    & $Paths.HermesPython (
      Join-Path $ReleasePath "forge\scripts\install-hermes-change.py"
    ) "build" --hermes-root $sourceTemp --package $packageTemp `
      --source-version $packageVersion | Out-Host
    Assert-ExternalCommand "Hermes change package build failed."
    Move-Item -LiteralPath $packageTemp -Destination $packagePath
  } finally {
    Remove-DeploymentPath -Path $sourceTemp -ExpectedParent $Paths.PackageRoot `
      -ExpectedPrefix "._s-"
    Remove-DeploymentPath -Path $packageTemp -ExpectedParent $Paths.PackageRoot `
      -ExpectedPrefix "._b-"
  }
  return [pscustomobject]@{
    Path = $packagePath
    Version = $packageVersion
    HermesVersion = $hermesRuntimeVersion
  }
}

function Get-PreviousHermesPackage {
  param([Parameter(Mandatory = $true)][pscustomobject]$Paths)
  if (-not (Test-Path -LiteralPath $Paths.StateFile)) { return $null }
  $state = Get-Content -Raw -LiteralPath $Paths.StateFile | ConvertFrom-Json
  if ($state.packagePath -isnot [string] -or
      [string]::IsNullOrWhiteSpace($state.packagePath)) {
    throw "Previous Windows deployment package path is invalid."
  }
  $oldPackage = [IO.Path]::GetFullPath($state.packagePath)
  if ([IO.Path]::GetDirectoryName($oldPackage) -ne
      [IO.Path]::GetFullPath($Paths.PackageRoot)) {
    throw "Previous Windows deployment package is outside package root."
  }
  if ([IO.Path]::GetFileName($oldPackage) -notmatch
      '^[0-9a-f]{40}-[0-9a-f]{40}$') {
    throw "Previous Windows deployment package name is invalid."
  }
  if (-not (Test-Path -PathType Leaf -LiteralPath (
    Join-Path $oldPackage "installed-files-list.json"
  ))) {
    throw "Previous Windows deployment package is incomplete."
  }
  return $oldPackage
}

function Invoke-HermesChange {
  param(
    [Parameter(Mandatory = $true)][pscustomobject]$Paths,
    [Parameter(Mandatory = $true)][string]$ReleasePath,
    [Parameter(Mandatory = $true)][ValidateSet("install", "restore")][string]$Action,
    [Parameter(Mandatory = $true)][string]$Package
  )
  & $Paths.HermesPython (
    Join-Path $ReleasePath "forge\scripts\install-hermes-change.py"
  ) $Action --hermes-root $Paths.HermesRoot --package $Package | Out-Host
  Assert-ExternalCommand "Hermes change $Action failed."
}

function Install-InfinityForgePlugin {
  param(
    [Parameter(Mandatory = $true)][pscustomobject]$Paths,
    [Parameter(Mandatory = $true)][string]$ReleasePath
  )
  New-Item -ItemType Directory -Force -Path $Paths.PluginRoot | Out-Null
  $suffix = "$PID-$([guid]::NewGuid().ToString('N'))"
  $pluginTemp = "$($paths.PluginDir).staging-$suffix"
  $pluginBackup = "$($paths.PluginDir).backup-$suffix"
  New-Item -ItemType Directory -Path $pluginTemp | Out-Null
  Copy-Item -LiteralPath (
    Join-Path $ReleasePath "forge\hermes_plugin\infinity_forge\plugin.yaml"
  ) -Destination (Join-Path $pluginTemp "plugin.yaml")
  Copy-Item -LiteralPath (
    Join-Path $ReleasePath "forge\hermes_plugin\infinity_forge\__init__.py"
  ) -Destination (Join-Path $pluginTemp "__init__.py")
  Set-Content -LiteralPath (Join-Path $pluginTemp "release-path.txt") `
    -Value $ReleasePath -Encoding utf8NoBOM -NoNewline
  if (Test-Path -LiteralPath $Paths.PluginDir) {
    # RISK(data-loss): 기존 plugin은 transaction backup으로 옮겨 rollback을 보장한다.
    Move-Item -LiteralPath $Paths.PluginDir -Destination $pluginBackup
  } else {
    $pluginBackup = $null
  }
  try {
    Move-Item -LiteralPath $pluginTemp -Destination $paths.PluginDir
  } catch {
    if ($null -ne $pluginBackup -and (Test-Path -LiteralPath $pluginBackup)) {
      Move-Item -LiteralPath $pluginBackup -Destination $Paths.PluginDir
    }
    throw
  }
  return $pluginBackup
}

function Set-InfinityForgeEnvironment {
  param(
    [Parameter(Mandatory = $true)][pscustomobject]$Paths,
    [Parameter(Mandatory = $true)][string]$ReleasePath
  )
  $env:HERMES_HOME = $Paths.HermesHome
  $saveEnvironment = @'
import sys
from hermes_cli.config import save_env_value

pairs = zip(sys.argv[1::2], sys.argv[2::2], strict=True)
for key, value in pairs:
    save_env_value(key, value)
'@
  # RISK(security): 기존 .env 전체를 읽거나 덮어쓰지 않고 세 경로 key만 갱신한다.
  Invoke-WithReleasePythonPath -ReleasePath $ReleasePath -Action {
    & $Paths.HermesPython -c $saveEnvironment `
      "INFINITY_FORGE_REPOSITORY" $Repository `
      "INFINITY_FORGE_TASK_SETTINGS_DB" $Paths.TaskSettingsDB `
      "INFINITY_FORGE_GH_PATH" $Paths.Gh
    Assert-ExternalCommand "Infinity Forge environment update failed."
  }
}

function Initialize-InfinityForgeDatabases {
  param(
    [Parameter(Mandatory = $true)][pscustomobject]$Paths,
    [Parameter(Mandatory = $true)][string]$ReleasePath
  )
  New-Item -ItemType Directory -Force -Path $Paths.TaskData | Out-Null
  $initialize = @'
import sys
from forge.ops.task_outbox import TaskOutbox, task_outbox_path
from forge.ops.task_settings import TaskSettingsStore

store = TaskSettingsStore(sys.argv[1])
TaskOutbox(task_outbox_path(store.database_path))
'@
  Invoke-WithReleasePythonPath -ReleasePath $ReleasePath -Action {
    & $Paths.HermesPython -c $initialize $Paths.TaskSettingsDB
    Assert-ExternalCommand "Infinity Forge database initialization failed."
  }
}

function Invoke-HermesProfile {
  param(
    [Parameter(Mandatory = $true)][pscustomobject]$Paths,
    [Parameter(Mandatory = $true)][string[]]$Arguments
  )
  & $Paths.HermesCli profile @Arguments | Out-Host
  Assert-ExternalCommand "Hermes profile command failed."
}

function Install-InfinityForgeProfilesAndSkills {
  param(
    [Parameter(Mandatory = $true)][pscustomobject]$Paths,
    [Parameter(Mandatory = $true)][string]$ReleasePath
  )
  # RISK(data-loss): active legacy Task가 0일 때만 교체된 profile ID를 이관한다.
  if ((Test-Path (Join-Path $Paths.ProfilesRoot "executor")) -and
      -not (Test-Path (Join-Path $Paths.ProfilesRoot "builder"))) {
    Invoke-HermesProfile -Paths $Paths -Arguments @("rename", "executor", "builder")
  }
  if ((Test-Path (Join-Path $Paths.ProfilesRoot "critic")) -and
      -not (Test-Path (Join-Path $Paths.ProfilesRoot "deep_checker"))) {
    Invoke-HermesProfile -Paths $Paths -Arguments @("rename", "critic", "deep_checker")
  }
  if (-not (Test-Path (Join-Path $Paths.ProfilesRoot "reviewer"))) {
    throw "The reviewer profile required for Task roles is missing."
  }
  if (-not (Test-Path (Join-Path $Paths.ProfilesRoot "builder"))) {
    Invoke-HermesProfile -Paths $Paths -Arguments @(
      "create", "builder", "--clone-from", "reviewer", "--no-alias"
    )
  }
  if (-not (Test-Path (Join-Path $Paths.ProfilesRoot "deep_checker"))) {
    Invoke-HermesProfile -Paths $Paths -Arguments @(
      "create", "deep_checker", "--clone-from", "reviewer", "--no-alias"
    )
  }
  if (-not (Test-Path (Join-Path $Paths.ProfilesRoot "fix"))) {
    Invoke-HermesProfile -Paths $Paths -Arguments @(
      "create", "fix", "--clone-from", "builder", "--no-alias"
    )
  }
  if (Test-Path (Join-Path $Paths.ProfilesRoot "issuefinder")) {
    Invoke-HermesProfile -Paths $Paths -Arguments @("delete", "issuefinder", "--yes")
  }

  New-Item -ItemType Directory -Force -Path $Paths.SkillsRoot | Out-Null
  $commonSkills = @(
    "forge-ops", "memex", "code-design-principles", "forge-labels"
  )
  $gatewaySkills = @("easy-answer", "code-problem-doc")
  $roleSkills = [ordered]@{
    builder = @("build-task")
    reviewer = @("review-task", "code-problem-doc")
    deep_checker = @("deep-check")
    fix = @("fix-task")
  }
  foreach ($skill in @($commonSkills + $gatewaySkills)) {
    $source = Join-Path $ReleasePath "forge\skills\$skill"
    if (Test-Path -LiteralPath $source) {
      Copy-Item -Recurse -Force -LiteralPath $source -Destination $Paths.SkillsRoot
    }
  }
  foreach ($profile in $roleSkills.Keys) {
    $profileSkills = Join-Path $Paths.ProfilesRoot "$profile\skills"
    New-Item -ItemType Directory -Force -Path $profileSkills | Out-Null
    foreach ($skill in @($commonSkills + $roleSkills[$profile])) {
      $source = Join-Path $ReleasePath "forge\skills\$skill"
      if (Test-Path -LiteralPath $source) {
        Copy-Item -Recurse -Force -LiteralPath $source -Destination $profileSkills
      }
    }
  }
}

function Enable-InfinityForgePlugin {
  param(
    [Parameter(Mandatory = $true)][pscustomobject]$Paths,
    [Parameter(Mandatory = $true)][string]$ReleasePath
  )
  Invoke-WithReleasePythonPath -ReleasePath $ReleasePath -Action {
    & $Paths.HermesCli plugins enable infinity-forge --no-allow-tool-override |
      Out-Host
    Assert-ExternalCommand "Infinity Forge plugin enable failed."
  }
}

function Test-ForgeWindowsRuntime {
  param(
    [Parameter(Mandatory = $true)][pscustomobject]$Paths,
    [Parameter(Mandatory = $true)][string]$ReleasePath,
    [Parameter(Mandatory = $true)][string]$PackagePath,
    [Parameter(Mandatory = $true)][bool]$expectedGatewayRunning
  )
  $pointer = Join-Path $Paths.PluginDir "release-path.txt"
  if (-not (Test-Path -LiteralPath $pointer)) {
    throw "Infinity Forge plugin release pointer is missing."
  }
  if ((Get-Content -Raw -LiteralPath $pointer).Trim() -ne $ReleasePath) {
    throw "Infinity Forge plugin release pointer is incorrect."
  }
  $marker = Get-Content -Raw -LiteralPath (
    Join-Path $ReleasePath "deployment-source.json"
  ) | ConvertFrom-Json
  if ($marker.commit -ne $Commit) {
    throw "Windows release marker does not match the requested commit."
  }

  $pluginList = (& $Paths.HermesCli plugins list --enabled --user --plain) -join "`n"
  Assert-ExternalCommand "Hermes plugin list failed."
  if ($pluginList -notmatch '(?m)^infinity-forge(?:\s|$)') {
    throw "Infinity Forge plugin is not enabled."
  }
  foreach ($markerFile in @(
    "hermes_cli\plugins.py",
    "agent\conversation_loop.py",
    "run_agent.py",
    "cli.py",
    "tui_gateway\server.py",
    "gateway\run.py"
  )) {
    $content = Get-Content -Raw -LiteralPath (Join-Path $Paths.HermesRoot $markerFile)
    if (-not $content.Contains("INFINITY_FORGE_PRE_USER_TURN_V1")) {
      throw "Hermes user-turn marker is missing: $markerFile"
    }
  }
  foreach ($profile in @("builder", "reviewer", "deep_checker", "fix")) {
    if (-not (Test-Path (Join-Path $Paths.ProfilesRoot $profile))) {
      throw "Infinity Forge Task profile is missing: $profile"
    }
  }

  $verifyPython = @'
import hashlib
import json
import pathlib
import sqlite3
import sys

release = pathlib.Path(sys.argv[1])
package = pathlib.Path(sys.argv[2])
hermes = pathlib.Path(sys.argv[3])
databases = [pathlib.Path(value) for value in sys.argv[4:]]
sys.path.insert(0, str(release))
from forge.ops.task_setup import TASK_CONTENT_TEMPLATE

for marker in ("[SPEC-NNN]", "[AC-01]", "## 확정된 제약"):
    assert marker in TASK_CONTENT_TEMPLATE
manifest = json.loads(
    (package / "installed-files-list.json").read_text(encoding="utf-8")
)
for item in manifest["files"]:
    actual = hashlib.sha256((hermes / item["path"]).read_bytes()).hexdigest()
    assert actual == item["after_file_hash"]
for database in databases:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        connection.close()
'@
  Invoke-WithReleasePythonPath -ReleasePath $ReleasePath -Action {
    & $Paths.HermesPython -c $verifyPython $ReleasePath $PackagePath `
      $Paths.HermesRoot $Paths.TaskSettingsDB $Paths.TaskOutboxDB $Paths.KanbanDB
    Assert-ExternalCommand "Windows Infinity Forge runtime verification failed."
  }
  $gatewayRunning = Test-HermesGatewayRunning -Paths $Paths
  if ($gatewayRunning -ne $expectedGatewayRunning) {
    throw "Windows Hermes Gateway running state was not preserved."
  }
}

function Restore-WindowsDeploymentTransaction {
  param(
    [Parameter(Mandatory = $true)][pscustomobject]$Paths,
    [Parameter(Mandatory = $true)][string]$ReleasePath,
    [Parameter(Mandatory = $true)][hashtable]$Transaction
  )
  try {
    if (Test-HermesGatewayRunning -Paths $Paths) {
      Stop-HermesGateway -Paths $Paths
    }
    if ($Transaction.NewPackageInstalled) {
      Invoke-HermesChange -Paths $Paths -ReleasePath $ReleasePath `
        -Action "restore" -Package $Transaction.NewPackage
    }
    if ($Transaction.OldPackageRestored) {
      Invoke-HermesChange -Paths $Paths -ReleasePath $ReleasePath `
        -Action "install" -Package $Transaction.OldPackage
    }
    if ($Transaction.PluginInstalled -and
        (Test-Path -LiteralPath $Paths.PluginDir)) {
      Remove-DeploymentPath -Path $Paths.PluginDir `
        -ExpectedParent $Paths.PluginRoot -ExpectedPrefix "infinity-forge"
    }
    if ($null -ne $Transaction.PluginBackup -and
        (Test-Path -LiteralPath $Transaction.PluginBackup)) {
      Move-Item -LiteralPath $Transaction.PluginBackup `
        -Destination $Paths.PluginDir
    }
  } catch {
    Write-Warning "Windows deployment rollback needs manual inspection: $($_.Exception.Message)"
  }
}

function Write-WindowsDeploymentState {
  param(
    [Parameter(Mandatory = $true)][pscustomobject]$Paths,
    [Parameter(Mandatory = $true)][string]$PackagePath,
    [Parameter(Mandatory = $true)][bool]$gatewayWasRunning
  )
  New-Item -ItemType Directory -Force -Path $Paths.StateRoot | Out-Null
  $stateTemp = Join-Path $Paths.StateRoot ".windows-deployment-state-$PID.json"
  [ordered]@{
    formatVersion = 1
    commit = $Commit
    packagePath = $PackagePath
    gatewayWasRunning = $gatewayWasRunning
    verifiedAtUtc = [DateTimeOffset]::UtcNow.ToString("o")
  } | ConvertTo-Json | Set-Content -LiteralPath $stateTemp -Encoding utf8NoBOM
  Move-Item -Force -LiteralPath $stateTemp -Destination $Paths.StateFile
}

function Invoke-ForgeWindowsApply {
  param([Parameter(Mandatory = $true)][pscustomobject]$Paths)
  $gatewayWasRunning = Test-HermesGatewayRunning -Paths $Paths
  $releasePath = Install-ForgeWindowsRelease -Paths $Paths
  $oldPackage = Get-PreviousHermesPackage -Paths $Paths
  $transaction = @{
    NewPackage = $null
    NewPackageInstalled = $false
    OldPackage = $oldPackage
    OldPackageRestored = $false
    PluginBackup = $null
    PluginInstalled = $false
  }
  try {
    if ($gatewayWasRunning) { Stop-HermesGateway -Paths $Paths }
    if ($null -ne $oldPackage) {
      Invoke-HermesChange -Paths $Paths -ReleasePath $releasePath `
        -Action "restore" -Package $oldPackage
      $transaction.OldPackageRestored = $true
    }
    $package = New-HermesChangePackage -Paths $Paths -ReleasePath $releasePath
    $transaction.NewPackage = $package.Path
    Invoke-HermesChange -Paths $Paths -ReleasePath $releasePath `
      -Action "install" -Package $package.Path
    $transaction.NewPackageInstalled = $true
    $transaction.PluginBackup = Install-InfinityForgePlugin `
      -Paths $Paths -ReleasePath $releasePath
    $transaction.PluginInstalled = $true
    Set-InfinityForgeEnvironment -Paths $Paths -ReleasePath $releasePath
    Initialize-InfinityForgeDatabases -Paths $Paths -ReleasePath $releasePath
    Install-InfinityForgeProfilesAndSkills -Paths $Paths -ReleasePath $releasePath
    Enable-InfinityForgePlugin -Paths $Paths -ReleasePath $releasePath
    if ($gatewayWasRunning) { Start-HermesGateway -Paths $Paths }
    Test-ForgeWindowsRuntime -Paths $Paths -ReleasePath $releasePath `
      -PackagePath $package.Path -expectedGatewayRunning $gatewayWasRunning
    Write-WindowsDeploymentState -Paths $Paths -PackagePath $package.Path `
      -gatewayWasRunning $gatewayWasRunning
    if ($null -ne $transaction.PluginBackup) {
      Remove-DeploymentPath -Path $transaction.PluginBackup `
        -ExpectedParent $Paths.PluginRoot -ExpectedPrefix "infinity-forge.backup-"
      $transaction.PluginBackup = $null
    }
  } catch {
    Restore-WindowsDeploymentTransaction -Paths $Paths `
      -ReleasePath $releasePath -Transaction $transaction
    if ($gatewayWasRunning -and -not (Test-HermesGatewayRunning -Paths $Paths)) {
      Start-HermesGateway -Paths $Paths
    }
    if (-not $gatewayWasRunning -and (Test-HermesGatewayRunning -Paths $Paths)) {
      Stop-HermesGateway -Paths $Paths
    }
    throw
  }
}

$paths = Test-ForgeWindowsPreflight
switch ($Mode) {
  "Preflight" {
    [ordered]@{
      target = "windows"
      phase = "preflight"
      status = "verified"
      commit = $Commit
      gatewayRunning = (Test-HermesGatewayRunning -Paths $paths)
    } | ConvertTo-Json -Compress
  }
  "Apply" {
    Invoke-ForgeWindowsApply -Paths $paths
    [ordered]@{
      target = "windows"
      phase = "apply"
      status = "verified"
      commit = $Commit
      gatewayRunning = (Test-HermesGatewayRunning -Paths $paths)
    } | ConvertTo-Json -Compress
  }
  "Verify" {
    if (-not (Test-Path -LiteralPath $paths.StateFile)) {
      throw "Windows deployment state is missing."
    }
    $state = Get-Content -Raw -LiteralPath $paths.StateFile | ConvertFrom-Json
    if ($state.commit -ne $Commit) {
      throw "Windows deployment state has a different commit."
    }
    $releasePath = Join-Path $paths.ReleaseRoot $Commit
    Test-ForgeWindowsRuntime -Paths $paths -ReleasePath $releasePath `
      -PackagePath $state.packagePath `
      -expectedGatewayRunning ([bool]$state.gatewayWasRunning)
    [ordered]@{
      target = "windows"
      phase = "verify"
      status = "verified"
      commit = $Commit
      gatewayRunning = (Test-HermesGatewayRunning -Paths $paths)
    } | ConvertTo-Json -Compress
  }
}
