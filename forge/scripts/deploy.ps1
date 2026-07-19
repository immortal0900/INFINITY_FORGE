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

  # 저장소 fast-forward와 배포 전체를 같은 FD 9 lock 아래에서 실행한다.
  # 이전 deploy-vps.sh가 새 script로 exec해도 FD와 marker가 그대로 상속된다.
  $DeployBootstrapScript = @'
set -euo pipefail
REPO_DIR="$1"
EXPECTED_COMMIT="$2"
DEPLOY_LOCK_ROOT="$HOME/.hermes/infinity-forge"
DEPLOY_LOCK_FILE="$DEPLOY_LOCK_ROOT/deploy.lock"
if [ ! -x /usr/bin/flock ]; then
  echo "[deploy] /usr/bin/flock is required for deployment locking" >&2
  exit 1
fi
mkdir -p "$DEPLOY_LOCK_ROOT"
exec 9>"$DEPLOY_LOCK_FILE"
if ! /usr/bin/flock --nonblock 9; then
  echo "[deploy] another Infinity Forge deployment is already running" >&2
  exec 9>&-
  exit 1
fi
export INFINITY_FORGE_DEPLOY_LOCK_FD9="$DEPLOY_LOCK_FILE"
env \
  FORGE_EXPECTED_COMMIT="$EXPECTED_COMMIT" \
  FORGE_REPO_DIR="$REPO_DIR" \
  bash "$REPO_DIR/forge/scripts/deploy-vps.sh"
'@
  Invoke-RemoteBashScript `
    -HostName $HostName `
    -Script $DeployBootstrapScript `
    -Arguments @($RemoteRepo, $Commit) `
    -FailureMessage "$Name 배포 잠금을 얻지 못했거나 배포가 실패했습니다."

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

TOOLSET_PROFILE_ARGS=(
  --worker-home "$HOME/.hermes/profiles/builder"
  --worker-home "$HOME/.hermes/profiles/reviewer"
  --worker-home "$HOME/.hermes/profiles/deep_checker"
  --worker-home "$HOME/.hermes/profiles/fix"
)
PYTHONPATH="$REPO_DIR" "$HERMES_PY" -m forge.ops.hermes_toolsets \
  verify --main-home "$HOME/.hermes" "${TOOLSET_PROFILE_ARGS[@]}"

CHOOSER_EXPECTED_COMMIT="$EXPECTED_COMMIT"
CHOOSER_HERMES_ROOT="$HERMES_ROOT"
CHOOSER_EXPECTED_REPOSITORY="$REPOSITORY"
CHOOSER_EXPECTED_TASK_SETTINGS_DB="$TASK_SETTINGS_DB"
CHOOSER_EXPECTED_GH_PATH="$GH_BIN"
# INFINITY_FORGE_CHOOSER_SMOKE_BEGIN
(
  CHOOSER_SMOKE_CWD="$(mktemp -d "${TMPDIR:-/tmp}/infinity-forge-chooser-smoke.XXXXXX")"
  chmod 700 "$CHOOSER_SMOKE_CWD"
  trap 'rmdir -- "$CHOOSER_SMOKE_CWD" 2>/dev/null || true' EXIT
  cd "$CHOOSER_SMOKE_CWD"
  env \
    -u PYTHONPATH \
    -u PYTHONHOME \
    -u PYTHONOPTIMIZE \
    -u INFINITY_FORGE_REPOSITORY \
    -u INFINITY_FORGE_TASK_SETTINGS_DB \
    -u INFINITY_FORGE_GH_PATH \
    HERMES_HOME="$HOME/.hermes" \
    PYTHONDONTWRITEBYTECODE=1 \
    CHOOSER_EXPECTED_COMMIT="$CHOOSER_EXPECTED_COMMIT" \
    CHOOSER_HERMES_ROOT="$CHOOSER_HERMES_ROOT" \
    CHOOSER_EXPECTED_REPOSITORY="$CHOOSER_EXPECTED_REPOSITORY" \
    CHOOSER_EXPECTED_TASK_SETTINGS_DB="$CHOOSER_EXPECTED_TASK_SETTINGS_DB" \
    CHOOSER_EXPECTED_GH_PATH="$CHOOSER_EXPECTED_GH_PATH" \
    "$HERMES_PY" - <<'PY'
import os
from pathlib import Path

from hermes_cli.env_loader import load_hermes_dotenv

hermes_root = Path(os.environ["HERMES_HOME"]).resolve()
hermes_project_root = Path(os.environ["CHOOSER_HERMES_ROOT"]).resolve()
expected_commit = os.environ["CHOOSER_EXPECTED_COMMIT"]
load_hermes_dotenv(project_env=hermes_project_root / ".env")
assert (
    os.environ["INFINITY_FORGE_REPOSITORY"]
    == os.environ["CHOOSER_EXPECTED_REPOSITORY"]
)
assert (
    os.environ["INFINITY_FORGE_TASK_SETTINGS_DB"]
    == os.environ["CHOOSER_EXPECTED_TASK_SETTINGS_DB"]
)
assert (
    os.environ["INFINITY_FORGE_GH_PATH"]
    == os.environ["CHOOSER_EXPECTED_GH_PATH"]
)

from hermes_cli.plugins import discover_plugins
from hermes_cli.plugins import get_plugin_manager
from hermes_cli.plugins import has_hook

discover_plugins(force=True)
manager = get_plugin_manager()
loaded = manager._plugins["infinity-forge"]
assert loaded.enabled is True
assert loaded.error is None
assert loaded.module is not None
assert loaded.manifest.path is not None
assert "pre_user_turn" in loaded.hooks_registered
assert has_hook("pre_user_turn")

module = loaded.module
plugin_path = Path(loaded.manifest.path).resolve()
expected_plugin_path = (hermes_root / "plugins" / "infinity-forge").resolve()
assert plugin_path == expected_plugin_path
module_file = getattr(module, "__file__", None)
assert module_file is not None
assert Path(module_file).resolve() == (plugin_path / "__init__.py").resolve()

managed_release = getattr(module, "_MANAGED_RELEASE", None)
assert managed_release is not None
expected_release = (
    hermes_root / "infinity-forge" / "releases" / expected_commit
).resolve()
assert Path(managed_release).resolve() == expected_release
assert expected_release.name == expected_commit


def forbid_task_service(_request):
    raise AssertionError("Task service must not run during chooser smoke")


module.set_task_service(forbid_task_service)
result = module.before_user_turn(
    session_id=f"chooser-smoke-{expected_commit}",
    user_id="deploy-verifier",
    surface="cli",
    text="diagnostic",
    is_new_session=True,
)
assert result["action"] == "handled"
assert [choice["id"] for choice in result["choices"]] == ["chat", "task"]
PY
)
# INFINITY_FORGE_CHOOSER_SMOKE_END

for MarkerFile in \
  hermes_cli/plugins.py \
  agent/conversation_loop.py \
  run_agent.py \
  cli.py \
  tui_gateway/server.py \
  gateway/run.py; do
  grep -Fq "INFINITY_FORGE_PRE_USER_TURN_V1" "$HERMES_ROOT/$MarkerFile"
done
grep -Fq "INFINITY_FORGE_SUBSCRIPTION_WORKER_V1" "$HERMES_ROOT/hermes_cli/kanban_db.py"

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
