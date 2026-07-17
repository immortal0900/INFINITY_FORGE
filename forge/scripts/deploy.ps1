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
STABLE_RUNNER="$HOME/.hermes/infinity-forge/bin/subscription-runner.py"
CLAUDE_BIN="$(command -v claude)"
CLAUDE_MCP_CONFIG="$HOME/.hermes/infinity-forge/subscription-runtime/claude-mcp.json"

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
grep -Fq "INFINITY_FORGE_SUBSCRIPTION_WORKER_V1" "$HERMES_ROOT/hermes_cli/kanban_db.py"
test -f "$STABLE_RUNNER"

for Skill in codex claude-code; do
  test -f "$HOME/.hermes/skills/$Skill/SKILL.md"
done

for Profile in builder reviewer deep_checker fix; do
  test -d "$HOME/.hermes/profiles/$Profile"
  for Skill in codex claude-code; do
    test -f "$HOME/.hermes/profiles/$Profile/skills/$Skill/SKILL.md"
  done
  test "$(readlink "$HOME/.hermes/profiles/$Profile/home/.codex")" = "$HOME/.codex"
  test "$(readlink "$HOME/.hermes/profiles/$Profile/home/.claude")" = "$HOME/.claude"
  test "$(readlink "$HOME/.hermes/profiles/$Profile/home/.claude.json")" = "$HOME/.claude.json"
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

GATEWAY_ENVIRONMENT="$(systemctl --user show hermes-gateway --property=Environment --value)"
for ExpectedEnvironment in \
  "INFINITY_FORGE_SUBSCRIPTION_ROUTING=1" \
  "INFINITY_FORGE_SUBSCRIPTION_PYTHON=$HERMES_PY" \
  "INFINITY_FORGE_SUBSCRIPTION_RUNNER=$STABLE_RUNNER" \
  "INFINITY_FORGE_CLAUDE_BIN=$CLAUDE_BIN" \
  "INFINITY_FORGE_CLAUDE_MCP_CONFIG=$CLAUDE_MCP_CONFIG" \
  "INFINITY_FORGE_REPO=$REPO_DIR"; do
  case "$GATEWAY_ENVIRONMENT" in
    *"$ExpectedEnvironment"*) ;;
    *) echo "[verify] subscription gateway environment is incomplete" >&2; exit 1 ;;
  esac
done
"$HERMES_PY" "$REPO_DIR/forge/scripts/configure-subscription-runtime.py" verify \
  --forge-root "$REPO_DIR" --hermes-root "$HOME/.hermes" >/dev/null

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
  Write-Host "[deploy] local subscription runtime..."
  $HermesDataRoot = Join-Path $env:LOCALAPPDATA "hermes"
  $HermesRoot = Join-Path $env:LOCALAPPDATA "hermes\hermes-agent"
  $HermesPython = Join-Path $HermesRoot "venv\Scripts\python.exe"
  $HermesExe = Join-Path $HermesRoot "venv\Scripts\hermes.exe"
  $StableRunner = Join-Path $env:LOCALAPPDATA "InfinityForge\subscription-runtime\subscription-runner.py"
  $ClaudeMcpConfig = Join-Path $HermesDataRoot "infinity-forge\subscription-runtime\claude-mcp.json"
  $ConfigureScript = Join-Path $Repo "forge\scripts\configure-subscription-runtime.py"
  $ChangeInstaller = Join-Path $Repo "forge\scripts\install-hermes-change.py"

  foreach ($RequiredFile in @($HermesPython, $HermesExe, $ConfigureScript, $ChangeInstaller)) {
    if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
      throw "Windows Hermes runtime is incomplete."
    }
  }
  $ClaudeCommand = Get-Command claude -CommandType Application -ErrorAction Stop
  $ClaudeBin = $ClaudeCommand.Source
  $CodexCommand = Get-Command codex.exe -CommandType Application -ErrorAction Stop
  $CodexBin = $CodexCommand.Source

  # RISK(security): parse the private auth response in memory and never print its raw fields.
  $ClaudeAuthText = & $ClaudeBin auth status --json 2>$null
  if ($LASTEXITCODE -ne 0) {
    throw "Claude Max login required; run: claude auth login"
  }
  try { $ClaudeAuth = $ClaudeAuthText | ConvertFrom-Json -ErrorAction Stop } catch {
    throw "Claude Max login required; run: claude auth login"
  }
  if (
    $ClaudeAuth.loggedIn -ne $true -or
    $ClaudeAuth.authMethod -cne "claude.ai" -or
    $ClaudeAuth.apiProvider -cne "firstParty" -or
    $ClaudeAuth.subscriptionType -cne "max"
  ) {
    throw "Claude Max login required; run: claude auth login"
  }
  $CodexProbe = 'import os,sys; sys.path.insert(0,sys.argv[1]); from forge.ops.codex_subscription_probe import CodexAppServerProbe; from forge.ops.subscription_runtime import scrub_subscription_environment; snapshot=CodexAppServerProbe().probe(sys.argv[2],scrub_subscription_environment(os.environ),timeout=10.0); raise SystemExit(0 if snapshot.account_type == "chatgpt" else 1)'
  & $HermesPython -c $CodexProbe $Repo $CodexBin
  if ($LASTEXITCODE -ne 0) { throw "Codex ChatGPT subscription preflight failed." }
  & $HermesExe gateway status | Out-Null
  $GatewayWasActive = $LASTEXITCODE -eq 0

  foreach ($AuthSource in @(
    (Join-Path $env:USERPROFILE ".codex"),
    (Join-Path $env:USERPROFILE ".claude"),
    (Join-Path $env:USERPROFILE ".claude.json")
  )) {
    $AuthItem = Get-Item -LiteralPath $AuthSource -Force -ErrorAction Stop
    if (($AuthItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
      throw "A real local login source is required."
    }
  }

  $SubscriptionEnvironment = [ordered]@{
    "INFINITY_FORGE_SUBSCRIPTION_ROUTING" = "1"
    "INFINITY_FORGE_SUBSCRIPTION_PYTHON" = $HermesPython
    "INFINITY_FORGE_SUBSCRIPTION_RUNNER" = $StableRunner
    "INFINITY_FORGE_CLAUDE_BIN" = $ClaudeBin
    "INFINITY_FORGE_CLAUDE_MCP_CONFIG" = $ClaudeMcpConfig
    "INFINITY_FORGE_REPO" = $Repo
  }
  $PreviousUserEnvironment = @{}
  $PreviousProcessEnvironment = @{}
  $EnvironmentChanged = $false
  $ConfigureApplied = $false
  $PackageChanged = $false
  $Package = $null
  $DeploymentRoot = Split-Path -Parent $StableRunner
  New-Item -ItemType Directory -Force -Path $DeploymentRoot | Out-Null
  $DeploymentBackup = Join-Path $DeploymentRoot (".deploy-backup-" + [guid]::NewGuid().ToString("N"))
  New-Item -ItemType Directory -Path $DeploymentBackup | Out-Null
  $ManagedBackups = [System.Collections.Generic.List[object]]::new()
  $ProfileLinks = [System.Collections.Generic.List[object]]::new()

  function Backup-LocalManagedPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $Backup = Join-Path $DeploymentBackup ([string]$ManagedBackups.Count)
    $Existed = Test-Path -LiteralPath $Path
    if ($Existed) { Copy-Item -LiteralPath $Path -Destination $Backup -Recurse -Force }
    $ManagedBackups.Add([pscustomobject]@{ Path = $Path; Backup = $Backup; Existed = $Existed })
  }

  try {
    $HermesSourceVersion = (git -C $HermesRoot rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $HermesSourceVersion -notmatch '^[0-9a-f]{40}$') {
      throw "Windows Hermes source version is invalid."
    }
    $ChangePackageRoot = Join-Path $DeploymentRoot "hermes-changes"
    New-Item -ItemType Directory -Force -Path $ChangePackageRoot | Out-Null
    $Package = Join-Path $ChangePackageRoot ("$Commit-$HermesSourceVersion")
    if (-not (Test-Path -LiteralPath (Join-Path $Package "installed-files-list.json") -PathType Leaf)) {
      if (Test-Path -LiteralPath $Package) { throw "Incomplete Hermes change package exists." }
      $SourceTemp = Join-Path $ChangePackageRoot (".source-" + [guid]::NewGuid().ToString("N"))
      $PackageTemp = Join-Path $ChangePackageRoot (".build-" + [guid]::NewGuid().ToString("N"))
      $Archive = Join-Path $ChangePackageRoot (".archive-" + [guid]::NewGuid().ToString("N") + ".tar")
      try {
        New-Item -ItemType Directory -Path $SourceTemp | Out-Null
        git -C $HermesRoot archive --format=tar --output=$Archive $HermesSourceVersion
        if ($LASTEXITCODE -ne 0) { throw "Hermes source archive failed." }
        tar -xf $Archive -C $SourceTemp
        if ($LASTEXITCODE -ne 0) { throw "Hermes source extraction failed." }
        & $HermesPython $ChangeInstaller build --hermes-root $SourceTemp --package $PackageTemp --source-version "$Commit-$HermesSourceVersion"
        if ($LASTEXITCODE -ne 0) { throw "Hermes change package build failed." }
        Move-Item -LiteralPath $PackageTemp -Destination $Package
      } finally {
        foreach ($TemporaryPath in @($Archive, $SourceTemp, $PackageTemp)) {
          if ($TemporaryPath -and (Test-Path -LiteralPath $TemporaryPath)) {
            Remove-Item -LiteralPath $TemporaryPath -Recurse -Force
          }
        }
      }
    }

    $WorkerTarget = Join-Path $HermesRoot "hermes_cli\kanban_db.py"
    $PackageChanged = -not (Select-String -LiteralPath $WorkerTarget -SimpleMatch "INFINITY_FORGE_SUBSCRIPTION_WORKER_V1" -Quiet)
    & $HermesPython $ChangeInstaller install --hermes-root $HermesRoot --package $Package
    if ($LASTEXITCODE -ne 0) { throw "Hermes carried change installation failed." }
    foreach ($MarkerTarget in @(
      "hermes_cli\plugins.py",
      "agent\conversation_loop.py",
      "run_agent.py",
      "cli.py",
      "tui_gateway\server.py",
      "gateway\run.py"
    )) {
      if (-not (Select-String -LiteralPath (Join-Path $HermesRoot $MarkerTarget) -SimpleMatch "INFINITY_FORGE_PRE_USER_TURN_V1" -Quiet)) {
        throw "Hermes user-turn carried change verification failed."
      }
    }
    if (-not (Select-String -LiteralPath $WorkerTarget -SimpleMatch "INFINITY_FORGE_SUBSCRIPTION_WORKER_V1" -Quiet)) {
      throw "Hermes subscription worker carried change verification failed."
    }

    $LocalSkills = Join-Path $HermesDataRoot "skills"
    New-Item -ItemType Directory -Force -Path $LocalSkills | Out-Null
    foreach ($S in @("forge-ops", "memex", "code-design-principles", "forge-labels", "easy-answer", "code-problem-doc", "codex", "claude-code")) {
      $SkillSource = Join-Path $Repo "forge\skills\$S"
      if (Test-Path -LiteralPath $SkillSource -PathType Container) {
        $SkillDestination = Join-Path $LocalSkills $S
        Backup-LocalManagedPath $SkillDestination
        Copy-Item -LiteralPath $SkillSource -Destination $LocalSkills -Recurse -Force
      }
    }

    $RoleMap = @{
      "builder"      = @("build-task")
      "reviewer"     = @("review-task", "code-problem-doc")
      "deep_checker" = @("deep-check")
      "fix"          = @("fix-task")
    }
    foreach ($Profile in $RoleMap.Keys) {
      $ProfileRoot = Join-Path $HermesDataRoot "profiles\$Profile"
      $ProfileSkills = Join-Path $ProfileRoot "skills"
      if (-not (Test-Path -LiteralPath $ProfileSkills -PathType Container)) { continue }
      foreach ($Skill in (@("forge-ops", "memex", "code-design-principles", "forge-labels", "codex", "claude-code") + $RoleMap[$Profile])) {
        $SkillSource = Join-Path $Repo "forge\skills\$Skill"
        if (Test-Path -LiteralPath $SkillSource -PathType Container) {
          $SkillDestination = Join-Path $ProfileSkills $Skill
          Backup-LocalManagedPath $SkillDestination
          Copy-Item -LiteralPath $SkillSource -Destination $ProfileSkills -Recurse -Force
        }
      }

      $ProfileHome = Join-Path $ProfileRoot "home"
      New-Item -ItemType Directory -Force -Path $ProfileHome | Out-Null
      foreach ($AuthName in @(".codex", ".claude", ".claude.json")) {
        $Source = Join-Path $env:USERPROFILE $AuthName
        $Destination = Join-Path $ProfileHome $AuthName
        $Backup = $null
        if (Test-Path -LiteralPath $Destination) {
          $Backup = "$Destination.bak.$([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')).$PID"
          # RISK(security): preserve the exact profile credential item before linking it.
          Move-Item -LiteralPath $Destination -Destination $Backup
        }
        $ProfileLinks.Add([pscustomobject]@{ Path = $Destination; Backup = $Backup })
        New-Item -ItemType SymbolicLink -Path $Destination -Target $Source | Out-Null
      }
    }

    Backup-LocalManagedPath $StableRunner
    Copy-Item -LiteralPath (Join-Path $Repo "forge\scripts\subscription-runner.py") -Destination $StableRunner -Force

    foreach ($Name in $SubscriptionEnvironment.Keys) {
      $PreviousUserEnvironment[$Name] = [Environment]::GetEnvironmentVariable($Name, "User")
      $PreviousProcessEnvironment[$Name] = [Environment]::GetEnvironmentVariable($Name, "Process")
    }
    $EnvironmentChanged = $true
    foreach ($Name in $SubscriptionEnvironment.Keys) {
      $Value = [string]$SubscriptionEnvironment[$Name]
      # RISK(side-effect): gateway launches inherit these current-user persistent values.
      [Environment]::SetEnvironmentVariable($Name, $Value, "User")
      Set-Item -Path "Env:$Name" -Value $Value
    }
    $ConfigureApplied = $true
    & $HermesPython $ConfigureScript apply --forge-root $Repo --hermes-root $HermesDataRoot
    if ($LASTEXITCODE -ne 0) { throw "Local subscription configuration apply failed." }
    & $HermesPython $ConfigureScript verify --forge-root $Repo --hermes-root $HermesDataRoot
    if ($LASTEXITCODE -ne 0) { throw "Local subscription configuration verify failed." }

    & $HermesExe gateway restart
    if ($LASTEXITCODE -ne 0) { throw "Local Hermes gateway restart failed." }
    & $HermesExe gateway status | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Local Hermes gateway is not active." }
  } catch {
    if ($ConfigureApplied) {
      & $HermesPython $ConfigureScript rollback --hermes-root $HermesDataRoot | Out-Null
    }
    if ($EnvironmentChanged) {
      foreach ($Name in $SubscriptionEnvironment.Keys) {
        $PreviousValue = $PreviousUserEnvironment[$Name]
        [Environment]::SetEnvironmentVariable($Name, $PreviousValue, "User")
        $PreviousProcessValue = $PreviousProcessEnvironment[$Name]
        if ($null -eq $PreviousProcessValue) { Remove-Item -Path "Env:$Name" -ErrorAction SilentlyContinue } else { Set-Item -Path "Env:$Name" -Value $PreviousProcessValue }
      }
    }
    for ($Index = $ProfileLinks.Count - 1; $Index -ge 0; $Index--) {
      $Link = $ProfileLinks[$Index]
      if (Test-Path -LiteralPath $Link.Path) { Remove-Item -LiteralPath $Link.Path -Force }
      if ($Link.Backup -and (Test-Path -LiteralPath $Link.Backup)) { Move-Item -LiteralPath $Link.Backup -Destination $Link.Path }
    }
    for ($Index = $ManagedBackups.Count - 1; $Index -ge 0; $Index--) {
      $Record = $ManagedBackups[$Index]
      if (Test-Path -LiteralPath $Record.Path) { Remove-Item -LiteralPath $Record.Path -Recurse -Force }
      if ($Record.Existed) { Copy-Item -LiteralPath $Record.Backup -Destination $Record.Path -Recurse -Force }
    }
    if ($PackageChanged -and $Package) {
      & $HermesPython $ChangeInstaller restore --hermes-root $HermesRoot --package $Package | Out-Null
    }
    if ($GatewayWasActive) {
      & $HermesExe gateway restart | Out-Null
    } else {
      & $HermesExe gateway stop | Out-Null
    }
    throw
  } finally {
    if (Test-Path -LiteralPath $DeploymentBackup) { Remove-Item -LiteralPath $DeploymentBackup -Recurse -Force }
  }
}

Write-Host "[deploy] 모든 대상 확인 완료: $($Commit.Substring(0, 8))"
