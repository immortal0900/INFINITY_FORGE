# INFINITY_FORGE — 검증된 main 커밋을 Windows, EC2, VPS에 반영합니다.
# 이 스크립트는 파일을 stage하거나 commit하지 않습니다.
param(
  [switch]$SkipPush,
  [switch]$SkipEC2,
  [switch]$SkipVPS,
  [switch]$SkipLocal
)
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $Repo

$Branch = (git symbolic-ref --short HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $Branch -ne "main") {
  throw "서버 배포는 로컬 main 브랜치에서만 실행할 수 있습니다."
}
$LocalChanges = @(git status --porcelain=v1 --untracked-files=all)
if ($LASTEXITCODE -ne 0) { throw "로컬 작업 트리를 확인할 수 없습니다." }
if ($LocalChanges.Count -ne 0) {
  throw "로컬 저장소가 깨끗하지 않습니다. 변경을 commit하거나 manual named stash로 보관하세요."
}

$Commit = (git rev-parse HEAD).Trim()
if ($Commit -notmatch '^[0-9a-f]{40}$') {
  throw "현재 Git commit을 확인할 수 없습니다."
}

if (-not $SkipPush) {
  git push origin main
  if ($LASTEXITCODE -ne 0) { throw "git push가 실패했습니다." }
}

git fetch origin main --quiet
if ($LASTEXITCODE -ne 0) { throw "origin/main을 확인할 수 없습니다." }
$ProductionCommit = (git rev-parse origin/main).Trim()
if ($Commit -ne $ProductionCommit) {
  throw "서버 배포는 origin/main의 검증된 commit만 허용합니다. PR을 병합한 뒤 다시 실행하세요."
}

$GhCommand = Get-Command gh.exe -ErrorAction Stop
$Repository = (& $GhCommand.Source repo view --json nameWithOwner --jq .nameWithOwner).Trim()
if ($LASTEXITCODE -ne 0 -or $Repository -notmatch '^[^/]+/[^/]+$') {
  throw "배포 대상 GitHub 저장소를 확인할 수 없습니다."
}
$WindowsAdapter = Join-Path $PSScriptRoot "deploy-windows.ps1"
if (-not (Test-Path -LiteralPath $WindowsAdapter)) {
  throw "Windows 배포 adapter를 찾을 수 없습니다."
}

$StartedAtUtc = [DateTimeOffset]::UtcNow.ToString("o")
$ReportRoot = Join-Path $env:LOCALAPPDATA "InfinityForge\state"
$ReportPath = Join-Path $ReportRoot "deployment-report.json"
$TargetResults = [ordered]@{}
foreach ($Target in @("EC2", "VPS", "Windows")) {
  $TargetResults[$Target] = [ordered]@{
    status = "pending"
    preflight = "pending"
    apply = "pending"
    verify = "pending"
    commit = $null
    runtime = [ordered]@{}
    error = $null
  }
}

function Set-TargetSkipped {
  param([Parameter(Mandatory = $true)][string]$Name)
  $TargetResults[$Name].status = "skipped"
  $TargetResults[$Name].preflight = "skipped"
  $TargetResults[$Name].apply = "skipped"
  $TargetResults[$Name].verify = "skipped"
}

function Set-TargetPhase {
  param(
    [Parameter(Mandatory = $true)][string]$Name,
    [Parameter(Mandatory = $true)][ValidateSet("preflight", "apply", "verify")][string]$Phase,
    [Parameter(Mandatory = $true)][string]$Status
  )
  $TargetResults[$Name][$Phase] = $Status
}

function Get-SafeDeploymentError {
  param([Parameter(Mandatory = $true)][string]$Message)
  # RISK(security): report에는 환경 dump나 전체 command가 아니라 짧은 오류만 남긴다.
  $safe = ($Message -replace '[\r\n]+', ' ').Trim()
  if ($safe.Length -gt 500) { return $safe.Substring(0, 500) }
  return $safe
}

function Write-DeploymentReport {
  New-Item -ItemType Directory -Force -Path $ReportRoot | Out-Null
  $ReportTemp = Join-Path $ReportRoot ".deployment-report-$PID.json"
  $SkippedTargets = @(
    $TargetResults.Keys | Where-Object {
      $TargetResults[$_].status -eq "skipped"
    }
  )
  [ordered]@{
    formatVersion = 1
    requestedCommit = $Commit
    startedAtUtc = $StartedAtUtc
    finishedAtUtc = [DateTimeOffset]::UtcNow.ToString("o")
    user = [Environment]::UserName
    targets = $TargetResults
    skipped = $SkippedTargets
  } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ReportTemp `
    -Encoding utf8NoBOM
  Move-Item -Force -LiteralPath $ReportTemp -Destination $ReportPath
}

function Invoke-RemoteBashScript {
  param(
    [Parameter(Mandatory = $true)][string]$HostName,
    [Parameter(Mandatory = $true)][string]$Script,
    [Parameter(Mandatory = $true)][string[]]$Arguments,
    [Parameter(Mandatory = $true)][string]$FailureMessage
  )

  foreach ($Argument in $Arguments) {
    if ($Argument -notmatch '^[A-Za-z0-9_./:@+-]+$') {
      throw "원격 Bash 인자에 허용되지 않은 문자가 있습니다."
    }
  }
  # PowerShell native pipeline은 Windows에서 마지막 줄에 CRLF를 덧붙일 수
  # 있다. UTF-8/LF bytes를 base64로 전달해 Bash가 동일한 script를 받게 한다.
  $NormalizedScript = $Script.Replace("`r`n", "`n").Replace("`r", "`n")
  $Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
  $EncodedScript = [Convert]::ToBase64String(
    $Utf8NoBom.GetBytes($NormalizedScript)
  )
  $QuotedArguments = @($Arguments | ForEach-Object { "'$_'" })
  $RemoteCommand = (
    "printf '%s' '$EncodedScript' | base64 --decode | bash -s -- " +
    ($QuotedArguments -join " ")
  )
  ssh $HostName $RemoteCommand
  if ($LASTEXITCODE -ne 0) { throw $FailureMessage }
}

function Test-ForgeServerPreflight {
  param(
    [Parameter(Mandatory = $true)][string]$Name,
    [Parameter(Mandatory = $true)][string]$HostName,
    [Parameter(Mandatory = $true)][string]$RemoteRepo
  )

  Write-Host "[preflight] $Name 확인..."
  $RemoteBranch = (
    ssh $HostName git -C $RemoteRepo symbolic-ref --short HEAD
  ).Trim()
  if ($LASTEXITCODE -ne 0 -or $RemoteBranch -ne "main") {
    throw "$Name 저장소가 main 브랜치가 아닙니다."
  }
  $RemoteChanges = @(
    ssh $HostName git -C $RemoteRepo status --porcelain=v1 --untracked-files=all
  )
  if ($LASTEXITCODE -ne 0) { throw "$Name 작업 트리를 확인할 수 없습니다." }
  if ($RemoteChanges.Count -ne 0) {
    throw "$Name 저장소가 깨끗하지 않습니다. 서버에서 manual named stash를 만든 뒤 다시 실행하세요."
  }
  $RemoteCommit = (ssh $HostName git -C $RemoteRepo rev-parse HEAD).Trim()
  if ($LASTEXITCODE -ne 0 -or $RemoteCommit -notmatch '^[0-9a-f]{40}$') {
    throw "$Name 현재 commit을 확인할 수 없습니다."
  }
  $RemoteMainLine = (
    ssh $HostName git -C $RemoteRepo ls-remote origin refs/heads/main
  ).Trim()
  if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($RemoteMainLine)) {
    throw "$Name origin/main을 읽을 수 없습니다."
  }
  $RemoteMainCommit = ($RemoteMainLine -split '\s+')[0]
  if ($RemoteMainCommit -ne $Commit) {
    throw "$Name origin/main이 요청 commit과 다릅니다."
  }
  git -C $Repo merge-base --is-ancestor $RemoteCommit $Commit
  if ($LASTEXITCODE -ne 0) {
    throw "$Name 저장소는 요청 commit으로 fast-forward할 수 없습니다."
  }
  Write-Host "[preflight] $Name 확인 완료"
}

function Invoke-ForgeServerDeploy {
  param(
    [Parameter(Mandatory = $true)][string]$Name,
    [Parameter(Mandatory = $true)][string]$HostName,
    [Parameter(Mandatory = $true)][string]$RemoteRepo
  )

  Write-Host "[deploy] $Name 반영..."

  $RemoteBranch = (ssh $HostName git -C $RemoteRepo symbolic-ref --short HEAD).Trim()
  if ($LASTEXITCODE -ne 0 -or $RemoteBranch -ne "main") {
    throw "$Name 저장소가 main 브랜치가 아닙니다."
  }
  $RemoteChanges = @(ssh $HostName git -C $RemoteRepo status --porcelain=v1 --untracked-files=all)
  if ($LASTEXITCODE -ne 0) { throw "$Name 작업 트리를 확인할 수 없습니다." }
  if ($RemoteChanges.Count -ne 0) {
    throw "$Name 저장소가 깨끗하지 않습니다. 서버에서 manual named stash를 만든 뒤 다시 실행하세요."
  }

  # 서버에 설치된 배포 스크립트가 이전 버전이어도 정확한 커밋만 먼저 적용한다.
  $RemotePrepareScript = @'
set -euo pipefail
REPO_DIR="$1"
EXPECTED_COMMIT="$2"
cd "$REPO_DIR"
test "$(git symbolic-ref --short HEAD)" = "main"
test -z "$(git status --porcelain=v1 --untracked-files=all)"
git fetch origin main --quiet
test "$(git rev-parse origin/main)" = "$EXPECTED_COMMIT"
git merge-base --is-ancestor HEAD "$EXPECTED_COMMIT"
git merge --ff-only "$EXPECTED_COMMIT"
test "$(git rev-parse HEAD)" = "$EXPECTED_COMMIT"
test -z "$(git status --porcelain=v1 --untracked-files=all)"
'@
  Invoke-RemoteBashScript `
    -HostName $HostName `
    -Script $RemotePrepareScript `
    -Arguments @($RemoteRepo, $Commit) `
    -FailureMessage "$Name 저장소를 요청한 main commit으로 이동하지 못했습니다."

  ssh $HostName env "FORGE_EXPECTED_COMMIT=$Commit" "FORGE_REPO_DIR=$RemoteRepo" bash "$RemoteRepo/forge/scripts/deploy-vps.sh" --post-update
  if ($LASTEXITCODE -ne 0) { throw "$Name 배포가 실패했습니다." }

  $VerificationScript = @'
set -euo pipefail
REPO_DIR="$1"
EXPECTED_COMMIT="$2"
HERMES_ROOT="$HOME/.hermes/hermes-agent"
HERMES_PY="$HERMES_ROOT/venv/bin/python"
HERMES_BIN="$HERMES_ROOT/venv/bin/hermes"
GH_BIN="/usr/bin/gh"
TASK_SETTINGS_DB="$HOME/.hermes/infinity-forge/task-settings.db"
CONFIRMED_TASKS_DB="$TASK_SETTINGS_DB.task-outbox.db"
HERMES_DB="$HOME/.hermes/kanban.db"
REPOSITORY="$(cd "$REPO_DIR" && "$GH_BIN" repo view --json nameWithOwner --jq .nameWithOwner)"

test "$(git -C "$REPO_DIR" rev-parse HEAD)" = "$EXPECTED_COMMIT"
test "$(git -C "$REPO_DIR" symbolic-ref --short HEAD)" = "main"
test -z "$(git -C "$REPO_DIR" status --porcelain=v1 --untracked-files=all)"
systemctl --user is-active --quiet hermes-gateway

test -f "$HOME/.hermes/plugins/infinity-forge/plugin.yaml"
test -f "$HOME/.hermes/plugins/infinity-forge/__init__.py"
PLUGIN_LIST="$("$HERMES_PY" -m hermes_cli.main plugins list --enabled --user --plain)"
case "$PLUGIN_LIST" in
  *infinity-forge*) ;;
  *) echo "[verify] infinity-forge plugin is not enabled" >&2; exit 1 ;;
esac

for MarkerFile in \
  hermes_cli/plugins.py \
  agent/conversation_loop.py \
  run_agent.py \
  cli.py \
  tui_gateway/server.py \
  gateway/run.py; do
  grep -Fq "INFINITY_FORGE_PRE_USER_TURN_V1" "$HERMES_ROOT/$MarkerFile"
done

for Profile in builder reviewer deep_checker fix; do
  test -d "$HOME/.hermes/profiles/$Profile"
done
for OldProfile in executor critic issuefinder; do
  test ! -e "$HOME/.hermes/profiles/$OldProfile"
done

for Database in "$TASK_SETTINGS_DB" "$CONFIRMED_TASKS_DB" "$HERMES_DB"; do
  test -f "$Database"
done
TASK_SETTINGS_DB="$TASK_SETTINGS_DB" CONFIRMED_TASKS_DB="$CONFIRMED_TASKS_DB" HERMES_DB="$HERMES_DB" "$HERMES_PY" -c 'import os, sqlite3; paths=(os.environ["TASK_SETTINGS_DB"], os.environ["CONFIRMED_TASKS_DB"], os.environ["HERMES_DB"]); assert all(sqlite3.connect(f"file:{path}?mode=ro", uri=True).execute("PRAGMA quick_check").fetchone()[0] == "ok" for path in paths)'

for Timer in ledger stage mirror canary drift morning merge flush; do
  systemctl --user is-active --quiet "forge-$Timer.timer"
done
assert_python_exec() {
  Service="$1"
  Script="$2"
  ExecStart="$(systemctl --user show "$Service" --property=ExecStart --value)"
  case "$ExecStart" in
    *"$HERMES_PY"*"$Script"*) ;;
    *) echo "[verify] unexpected ExecStart for $Service" >&2; exit 1 ;;
  esac
}
assert_python_exec forge-ledger.service activity-log-writer.py
assert_python_exec forge-stage.service task-flow-worker.py
assert_python_exec forge-mirror.service issue-status-sync.py
assert_python_exec forge-merge.service merge-worker.py
assert_python_exec forge-flush.service send-pending-messages.py
MERGE_ENVIRONMENT="$(systemctl --user show forge-merge.service --property=Environment --value)"
case "$MERGE_ENVIRONMENT" in
  *AUTO_MERGE_ENABLED=false*) ;;
  *) echo "[verify] automatic merge safety switch is not disabled" >&2; exit 1 ;;
esac

PYTHONPATH="$REPO_DIR" "$HERMES_PY" "$REPO_DIR/forge/scripts/task-flow-worker.py" --check-port
PYTHONPATH="$REPO_DIR" "$HERMES_PY" "$REPO_DIR/forge/scripts/issue-status-sync.py" --check-port
PYTHONPATH="$REPO_DIR" "$HERMES_PY" "$REPO_DIR/forge/scripts/merge-worker.py" --help >/dev/null
PYTHONPATH="$REPO_DIR" "$HERMES_PY" "$REPO_DIR/forge/scripts/task-flow-worker.py" --db "$HERMES_DB" --hermes "$HERMES_BIN" --gh "$GH_BIN" --settings-db "$TASK_SETTINGS_DB" --outbox "$CONFIRMED_TASKS_DB" --repo "$REPOSITORY" --workspace "dir:$REPO_DIR" --dry-run
PYTHONPATH="$REPO_DIR" "$HERMES_PY" "$REPO_DIR/forge/scripts/issue-status-sync.py" --db "$HERMES_DB" --gh "$GH_BIN" --settings-db "$TASK_SETTINGS_DB" --outbox "$CONFIRMED_TASKS_DB" --repo "$REPOSITORY" --dry-run
'@
  Invoke-RemoteBashScript `
    -HostName $HostName `
    -Script $VerificationScript `
    -Arguments @($RemoteRepo, $Commit) `
    -FailureMessage "$Name의 배포 후 실행 상태 검증이 실패했습니다."
  Write-Host "[deploy] $Name 확인 완료: $($Commit.Substring(0, 8))"
}

if ($SkipEC2) { Set-TargetSkipped -Name "EC2" }
if ($SkipVPS) { Set-TargetSkipped -Name "VPS" }
if ($SkipLocal) { Set-TargetSkipped -Name "Windows" }

$ActiveTarget = $null
$ActivePhase = $null
try {
  # 모든 선택 대상의 read-only preflight가 끝난 뒤에만 첫 apply를 시작한다.
  if (-not $SkipEC2) {
    $ActiveTarget = "EC2"
    $ActivePhase = "preflight"
    Test-ForgeServerPreflight -Name "EC2" -HostName "My-EC2" `
      -RemoteRepo "/home/ec2-user/work/INFINITY_FORGE"
    Set-TargetPhase -Name "EC2" -Phase "preflight" -Status "verified"
  }
  if (-not $SkipVPS) {
    $ActiveTarget = "VPS"
    $ActivePhase = "preflight"
    Test-ForgeServerPreflight -Name "VPS" `
      -HostName "ubuntu@51.222.27.48" `
      -RemoteRepo "/home/ubuntu/work/INFINITY_FORGE"
    Set-TargetPhase -Name "VPS" -Phase "preflight" -Status "verified"
  }
  if (-not $SkipLocal) {
    $ActiveTarget = "Windows"
    $ActivePhase = "preflight"
    & $WindowsAdapter -Repo $Repo -Commit $Commit -Repository $Repository `
      -Mode "Preflight" | Out-Host
    Set-TargetPhase -Name "Windows" -Phase "preflight" -Status "verified"
  }

  if (-not $SkipEC2) {
    $ActiveTarget = "EC2"
    $ActivePhase = "apply"
    Set-TargetPhase -Name "EC2" -Phase "apply" -Status "running"
    Invoke-ForgeServerDeploy -Name "EC2" -HostName "My-EC2" `
      -RemoteRepo "/home/ec2-user/work/INFINITY_FORGE"
    Set-TargetPhase -Name "EC2" -Phase "apply" -Status "verified"
    Set-TargetPhase -Name "EC2" -Phase "verify" -Status "verified"
    $TargetResults.EC2.status = "verified"
    $TargetResults.EC2.commit = $Commit
    $TargetResults.EC2.runtime = [ordered]@{
      gateway = "active"; timers = "active"; autoMergeEnabled = $false
    }
  }
  if (-not $SkipVPS) {
    $ActiveTarget = "VPS"
    $ActivePhase = "apply"
    Set-TargetPhase -Name "VPS" -Phase "apply" -Status "running"
    Invoke-ForgeServerDeploy -Name "VPS" `
      -HostName "ubuntu@51.222.27.48" `
      -RemoteRepo "/home/ubuntu/work/INFINITY_FORGE"
    Set-TargetPhase -Name "VPS" -Phase "apply" -Status "verified"
    Set-TargetPhase -Name "VPS" -Phase "verify" -Status "verified"
    $TargetResults.VPS.status = "verified"
    $TargetResults.VPS.commit = $Commit
    $TargetResults.VPS.runtime = [ordered]@{
      gateway = "active"; timers = "active"; autoMergeEnabled = $false
    }
  }
  if (-not $SkipLocal) {
    $ActiveTarget = "Windows"
    $ActivePhase = "apply"
    Set-TargetPhase -Name "Windows" -Phase "apply" -Status "running"
    & $WindowsAdapter -Repo $Repo -Commit $Commit -Repository $Repository `
      -Mode "Apply" | Out-Host
    Set-TargetPhase -Name "Windows" -Phase "apply" -Status "verified"
    $ActivePhase = "verify"
    & $WindowsAdapter -Repo $Repo -Commit $Commit -Repository $Repository `
      -Mode "Verify" | Out-Host
    Set-TargetPhase -Name "Windows" -Phase "verify" -Status "verified"
    $TargetResults.Windows.status = "verified"
    $TargetResults.Windows.commit = $Commit
    $TargetResults.Windows.runtime = [ordered]@{
      gateway = "preserved"; plugin = "enabled"
    }
  }
  Write-DeploymentReport
} catch {
  if ($null -ne $ActiveTarget -and
      $TargetResults[$ActiveTarget].status -ne "skipped") {
    $TargetResults[$ActiveTarget].status = "failed"
    if ($null -ne $ActivePhase) {
      $TargetResults[$ActiveTarget][$ActivePhase] = "failed"
    }
    $TargetResults[$ActiveTarget].error = Get-SafeDeploymentError `
      -Message $_.Exception.Message
  }
  Write-DeploymentReport
  throw
}

$SkippedCount = @(
  $TargetResults.Keys | Where-Object {
    $TargetResults[$_].status -eq "skipped"
  }
).Count
if ($SkippedCount -eq 0) {
  Write-Host "[deploy] 모든 대상 확인 완료: $($Commit.Substring(0, 8))"
} else {
  Write-Host "[deploy] 선택한 대상 확인 완료: $($Commit.Substring(0, 8)); 생략 $SkippedCount"
}
