# INFINITY_FORGE — 검증된 main 커밋을 EC2와 VPS에 반영합니다.
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
  $RemotePrepareScript | ssh $HostName bash -s -- $RemoteRepo $Commit
  if ($LASTEXITCODE -ne 0) { throw "$Name 저장소를 요청한 main commit으로 이동하지 못했습니다." }

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
  $VerificationScript | ssh $HostName bash -s -- $RemoteRepo $Commit
  if ($LASTEXITCODE -ne 0) {
    throw "$Name의 배포 후 실행 상태 검증이 실패했습니다."
  }
  Write-Host "[deploy] $Name 확인 완료: $($Commit.Substring(0, 8))"
}

if (-not $SkipEC2) {
  Invoke-ForgeServerDeploy -Name "EC2" -HostName "My-EC2" -RemoteRepo "/home/ec2-user/work/INFINITY_FORGE"
}
if (-not $SkipVPS) {
  Invoke-ForgeServerDeploy -Name "VPS" -HostName "ubuntu@51.222.27.48" -RemoteRepo "/home/ubuntu/work/INFINITY_FORGE"
}

if (-not $SkipLocal) {
  Write-Host "[deploy] local Hermes skills..."
  $LocalSkills = "$env:LOCALAPPDATA\hermes\skills"
  foreach ($S in @(
    "forge-ops",
    "memex",
    "code-design-principles",
    "forge-labels",
    "easy-answer",
    "code-problem-doc"
  )) {
    if (Test-Path "$Repo\forge\skills\$S") {
      Copy-Item -Recurse -Force "$Repo\forge\skills\$S" $LocalSkills
    }
  }

  $RoleMap = @{
    "builder"      = @("build-task")
    "reviewer"     = @("review-task", "code-problem-doc")
    "deep_checker" = @("deep-check")
    "fix"          = @("fix-task")
  }
  foreach ($Profile in $RoleMap.Keys) {
    $ProfileSkills = "$env:LOCALAPPDATA\hermes\profiles\$Profile\skills"
    if (-not (Test-Path $ProfileSkills)) { continue }
    foreach ($Skill in (@(
      "forge-ops",
      "memex",
      "code-design-principles",
      "forge-labels"
    ) + $RoleMap[$Profile])) {
      if (Test-Path "$Repo\forge\skills\$Skill") {
        Copy-Item -Recurse -Force "$Repo\forge\skills\$Skill" $ProfileSkills
      }
    }
  }
}

Write-Host "[deploy] 모든 대상 확인 완료: $($Commit.Substring(0, 8))"
