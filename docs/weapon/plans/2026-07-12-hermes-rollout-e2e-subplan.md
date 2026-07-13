# Hermes 조기 종료 방지 실운영 배포 구현 계획

> **Agentic worker:** REQUIRED SUB-SKILL: `weapon:executing-plans`, `weapon:verification-before-completion`, `weapon:risk-annotation`으로 실행한다. 한 target의 실패를 다음 target에서 보정하지 말고 즉시 rollback과 release convergence loop를 수행한다.

- 상위 계획: `docs/weapon/plans/2026-07-12-hermes-early-termination-guards.md`
- 승인 spec: `docs/weapon/specs/2026-07-12-hermes-early-termination-guards-design.md`
- 순서: candidate seal → Windows → WSL Ubuntu Linux staging → Ubuntu VPS → live E2E
- Windows root: `C:\01.project\INFINITY_FORGE`
- WSL distro/user: `Ubuntu` / `immortal0900`
- VPS SSH/repo: `ubuntu@51.222.27.48` / `/home/ubuntu/work/INFINITY_FORGE`

## Goal

두 platform PR checks가 green인 하나의 immutable SHA를 Windows, 일반 Linux staging, Ubuntu VPS에 순서대로 배포하고, rollback/forward와 음성·양성 E2E를 실제 프로세스·DB·GitHub 증거로 확인한다. rollout 중 Git tree는 변경하지 않는다.

## Architecture

Windows orchestrator가 candidate SHA와 immutable build manifest를 확정한다. 각 target adapter는 먼저 control/guard/producer pre-state를 staging snapshot에 수집하고 producers/embedded dispatcher를 멈춰 active work를 drain한 뒤 DB/Hermes bytes를 추가해 snapshot을 봉인한다. 이어 candidate immutable release를 stage/verify하고 그 release의 installer로 clean-only runtime bootstrap, hook/guard/patch/current/service/gateway/canary audit, durable receipt, closed-marker supervisor ready, marker open, producer pre-state restore를 수행한다. host별 동적 상태는 build manifest가 아니라 deployment receipt에 기록한다. 검증 evidence는 OS state root와 bootstrap issue comment에 digest로 남긴다.

## Tech Stack

- PowerShell 7, WSL2 Ubuntu, Bash, systemd user services
- Git/GitHub CLI, SSH/SCP, Python 3.11+ trusted release
- Hermes v0.18.2, SQLite

## Global Constraints

1. `git status --porcelain`이 비고 40자리 SHA가 두 named PR checks에서 success일 때만 candidate를 seal한다.
2. `.codex/ralph-loop.local.json`은 구현 단계의 `.gitignore` 적용으로 status에서 제외돼야 한다. 다른 dirty file은 허용하지 않는다.
3. immutable build manifest에는 exact nine fields `schema_version|source_sha|archive_sha256|guard_sha256|requirements_lock_sha256|python_requires|schema_hashes|hermes_patch_manifest_sha256|hermes_patch_sha256`만 둔다. target/timestamp/previous release는 deployment receipt에 둔다.
4. deploy 시작 뒤 tracked file, commit, branch, PR head를 바꾸지 않는다.
5. Windows Hermes 사용자 checkout의 unrelated dirty files는 snapshot 전후 path/status가 같아야 한다. target file 외 stage/commit을 금지한다.
6. control/guard/producer pre-state를 먼저 수집한 뒤 producer와 embedded dispatcher를 중단하고 active task/tmux를 drain한다. drain timeout은 15분이며 timeout이면 미완성 snapshot을 성공 receipt로 사용하지 않고 rollback한다.
7. Windows Scheduled Task `AccessDenied`는 hard failure다. Startup VBS fallback을 자동 사용하지 않는다.
8. POSIX DB/state는 0600, Windows는 current-user-only ACL을 요구한다.
9. canary failure는 dispatcher를 닫고 gateway health를 보존한다. gateway가 함께 내려가면 rollback한다.
10. candidate defect가 나오면 세 host를 previous common release로 되돌리고 새 SHA로 CI부터 반복한다.
11. evidence에는 secret 값, environment dump, raw authentication header를 기록하지 않는다.

## Phase 0: Windows GitHub credential prerequisite

현재 read-only 확인상 Windows `gh auth status`는 미로그인이고 repository는 private이다. rollout operator가 다음 interactive login을 직접 완료해야 하며, WSL credential/token을 Windows로 복사하는 fallback은 금지한다.

```powershell
gh auth login --web --git-protocol https --scopes repo,workflow,read:org
gh auth status
Push-Location C:\01.project\INFINITY_FORGE
try {
  $CanonicalRepo = (gh repo view --json nameWithOwner --jq .nameWithOwner).Trim()
  gh api "repos/$CanonicalRepo/actions/workflows"
  gh variable list --repo $CanonicalRepo
  gh issue list --repo $CanonicalRepo --limit 1
} finally {
  Pop-Location
}
```

Expected: 모든 command exit 0. token/header는 출력·evidence 저장하지 않는다. 실패하면 GitHub write, artifact deploy, service/Task/Hermes mutation 0회로 중단한다.

## Phase 0B: 세 target Slack alert credential prerequisite

현재 read-only 실측은 Windows `C:\Users\황화인HwainHwang\.codex\secrets\codex-work-report.env`만 존재하고 ACL inheritance와 sandbox group read가 켜져 있으며, WSL `/home/immortal0900/.codex/secrets/codex-work-report.env`와 VPS `/home/ubuntu/.codex/secrets/codex-work-report.env`는 absent다. 승인 spec의 canary 즉시 Slack alert를 세 target에서 실제 보장하려면 서비스 설치 전에 이 prerequisite를 먼저 수렴시킨다.

```powershell
$SlackEnvFile = 'C:\Users\황화인HwainHwang\.codex\secrets\codex-work-report.env'
pwsh -NoProfile -File C:\01.project\INFINITY_FORGE\forge\scripts\provision-slack-alert-secret.ps1 `
  -SourceEnvFile $SlackEnvFile `
  -Targets Windows,Linux,Vps `
  -WslDistribution Ubuntu `
  -WslUser immortal0900 `
  -VpsHost ubuntu@51.222.27.48 `
  -RepairWindowsAcl
```

Provisioner는 source env의 exact allowlist `CODEX_WORK_REPORT_SLACK_APP_NAME|CODEX_WORK_REPORT_SLACK_APP_ID|CODEX_WORK_REPORT_SLACK_CHANNEL|CODEX_WORK_REPORT_SLACK_BOT_TOKEN`과 app `codex work report`/App ID `A0BEQAZ1MS5`/channel `C0BES16KE1J`을 strict parse한다. Windows는 content를 바꾸지 않고 current user, SYSTEM, Administrators만 허용하는 protected ACL을 적용해 exact read-back한다. WSL/VPS에는 canonical LF bytes를 child stdin으로만 전달해 parent dir 0700/file 0600/file+directory fsync/atomic replace를 수행한다. secret은 argv/environment/temp artifact/stdout/stderr/evidence에 넣지 않는다. 각 host는 SHA-256/mode, `auth.test`의 pinned token principal `team_id=T0AU5RA7XND`, `user_id=U0BEG5Y5CCB`, `bot_id=B0BELD3V84E`, `user=codex_work_report`, response scope exact set `chat:write,chat:write.public`을 반환한다. 현재 scope에 없는 `bots.info`/`conversations.info`는 호출하지 않는다. App ID는 strict local metadata+pinned principal의 `locally-pinned-principal` 보증으로 기록한다. 이어 target/env digest/spec ID에서 만든 deterministic `client_msg_id`로 host-local preflight message를 exact-once post하고 response가 `channel=C0BES16KE1J`, non-empty `ts`인지 검증한다. local/remote digest가 다르면 새 file을 제거하고 실패한다. 기존 다른 digest는 `-Rotate` 없이는 덮어쓰지 않으며 이 최초 rollout은 `-Rotate`를 금지한다.

Expected: 세 target JSON result가 exact same env SHA-256, pinned principal/scope, exact channel/ts preflight sent receipt, Windows protected ACL 또는 POSIX mode 0600을 보고한다. 같은 명령 재실행은 durable sent receipt를 재사용해 추가 visible message를 만들지 않는다. 실패하면 candidate seal, GitHub write, artifact deploy, service/Task/Hermes mutation 0회로 중단한다. credential은 deployment rollback 소유물이 아니므로 receipt에는 digest만 기록하고 rollback이 삭제하지 않는다.

## 공통 PowerShell 변수

각 Task 시작 시 같은 세션에서 다음을 실행한다.

```powershell
$ErrorActionPreference = 'Stop'
$Repo = 'C:\01.project\INFINITY_FORGE'
$Sha = (git -C $Repo rev-parse HEAD).Trim()
if ($Sha -notmatch '^[0-9a-f]{40}$') { throw "invalid candidate SHA: $Sha" }
$BuildRoot = Join-Path $env:LOCALAPPDATA "InfinityForge\builds\$Sha"
$Manifest = Join-Path $BuildRoot 'build-manifest.json'
$Artifact = Join-Path $BuildRoot 'infinity-forge.tar'
$ArtifactHash = if (Test-Path -LiteralPath $Artifact) { (Get-FileHash -Algorithm SHA256 -LiteralPath $Artifact).Hash.ToLowerInvariant() } else { '' }
$GuardRelease = Join-Path $env:LOCALAPPDATA "InfinityForge\guard\releases\$Sha"
$WindowsReceipt = Join-Path $env:LOCALAPPDATA 'InfinityForge\state\deployment-receipt-v1.json'
$EvidenceRoot = Join-Path $env:LOCALAPPDATA 'InfinityForge\state\evidence'
New-Item -ItemType Directory -Force -Path $EvidenceRoot | Out-Null
$CiEvidenceRoot = Join-Path $EvidenceRoot "ci-$Sha"
$LinuxReceipt = Join-Path $EvidenceRoot 'linux-deployment-receipt.json'
$VpsReceipt = Join-Path $EvidenceRoot 'vps-deployment-receipt.json'
$BootstrapRepository = ''
Push-Location $Repo
try {
  $BootstrapRepository = (gh repo view --json nameWithOwner --jq .nameWithOwner).Trim()
} finally {
  Pop-Location
}
if ($BootstrapRepository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') {
  throw "invalid bootstrap repository: $BootstrapRepository"
}
$OpsHost = (gh variable get FORGE_OPS_HOST --repo $BootstrapRepository --json value --jq .value).Trim()
$ConfiguredBootstrapRepository = (gh variable get FORGE_BOOTSTRAP_REPOSITORY --repo $BootstrapRepository --json value --jq .value).Trim()
if ($OpsHost -ne 'true' -or $ConfiguredBootstrapRepository -ne $BootstrapRepository) {
  throw 'canonical repository is not provisioned as the ops host'
}
$BootstrapIssue = [int](gh variable get FORGE_BOOTSTRAP_ISSUE --repo $BootstrapRepository --json value --jq .value)
if ($LASTEXITCODE -ne 0 -or $BootstrapIssue -le 0) { throw 'FORGE_BOOTSTRAP_ISSUE is not configured' }
$LinuxRepo = "/home/immortal0900/work/INFINITY_FORGE/$Sha"
$VpsRepo = '/home/ubuntu/work/INFINITY_FORGE'
```

## Rollout Task 1: candidate SHA와 artifact를 seal한다

**Consumes:** clean Git worktree, bootstrap PR, two named checks.

**Produces:** `$Artifact`, `$Manifest`, local verification JSON. Git tree는 불변이다.

**Steps:**

- [ ] branch/dirty 상태와 SHA를 확인한다.

```powershell
Set-Location $Repo
$Status = @(git status --porcelain)
if ($Status.Count -ne 0) { throw "candidate worktree is dirty: $($Status -join ', ')" }
if ((git branch --show-current) -eq 'main') { throw 'rollout requires reviewed feature branch before merge' }
git show --no-patch --format='%H %s' $Sha
```

Expected: 한 줄의 `$Sha <commit subject>`, exit 0.

- [ ] bootstrap PR의 head와 두 check를 API에서 검증한다.

```powershell
$Pr = gh pr view --json number,headRefOid,statusCheckRollup | ConvertFrom-Json
if ($Pr.headRefOid -ne $Sha) { throw "PR head $($Pr.headRefOid) != candidate $Sha" }
$ExpectedChecks = @('guard-contract (ubuntu-latest)', 'guard-contract (windows-latest)')
foreach ($Name in $ExpectedChecks) {
  $Matches = @($Pr.statusCheckRollup | Where-Object { $_.name -eq $Name })
  if ($Matches.Count -ne 1 -or $Matches[0].conclusion -ne 'SUCCESS') {
    throw "required check not green exactly once: $Name"
  }
}
```

Expected: no output, exit 0. Missing, duplicate, pending, skipped, neutral, failure는 hard failure다.

- [ ] full local verification을 fresh 실행한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m compileall forge
git diff --check
```

Expected: pytest exit 0, compileall exit 0, diff-check exit 0.

- [ ] immutable artifact를 두 번 별도 output에 build해 byte reproducibility를 확인한다.

```powershell
.\.venv\Scripts\python.exe -m forge.ops.deployment build --sha $Sha --output-dir $BuildRoot
$First = (Get-FileHash -Algorithm SHA256 -LiteralPath $Artifact).Hash.ToLowerInvariant()
$SecondRoot = Join-Path $env:LOCALAPPDATA "InfinityForge\builds\$Sha-second"
.\.venv\Scripts\python.exe -m forge.ops.deployment build --sha $Sha --output-dir $SecondRoot
$SecondArtifact = Join-Path $SecondRoot 'infinity-forge.tar'
$Second = (Get-FileHash -Algorithm SHA256 -LiteralPath $SecondArtifact).Hash.ToLowerInvariant()
if ($First -ne $Second) { throw "non-reproducible artifact: $First != $Second" }
```

Expected: 두 build JSON status `PASS`, 두 SHA-256 동일. 기존 output directory면 command가 삭제하지 않고 실패한다.

- [ ] manifest schema/hash와 credential scan을 검증한다.

```powershell
$ArtifactHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Artifact).Hash.ToLowerInvariant()
.\.venv\Scripts\python.exe -m forge.ops.deployment verify-build --build-manifest $Manifest --artifact $Artifact --artifact-sha256 $ArtifactHash
.\.venv\Scripts\python.exe -m forge.guard secret-scan --paths $Repo $BuildRoot --git-repository $Repo
```

Expected: 두 command exit 0. scanner는 matched value를 출력하지 않는다.

- [ ] current-head CI artifact와 실제 PR evidence comment payload를 회수해 다시 scan한다.

```powershell
$RepoName = (gh repo view --json nameWithOwner | ConvertFrom-Json).nameWithOwner
$Branch = (git -C $Repo branch --show-current).Trim()
$Runs = @(gh run list --workflow capability-eval.yml --branch $Branch --limit 20 --json databaseId,headSha,status,conclusion | ConvertFrom-Json)
$Run = @($Runs | Where-Object { $_.headSha -eq $Sha -and $_.status -eq 'completed' -and $_.conclusion -eq 'success' } | Sort-Object databaseId -Descending)[0]
if ($null -eq $Run) { throw "no successful capability-eval run for $Sha" }
if (Test-Path -LiteralPath $CiEvidenceRoot) { throw "CI evidence path already exists: $CiEvidenceRoot" }
New-Item -ItemType Directory -Path $CiEvidenceRoot | Out-Null
$ArtifactMetadataPath = Join-Path $CiEvidenceRoot 'actions-artifacts.json'
$PrCommentsPath = Join-Path $CiEvidenceRoot 'pr-comments.json'
$Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
$ArtifactPagesJson = gh api --paginate --slurp "repos/$RepoName/actions/runs/$($Run.databaseId)/artifacts?per_page=100"
if ($LASTEXITCODE -ne 0) { throw 'paginated artifact metadata read failed' }
$ArtifactPages = $ArtifactPagesJson | ConvertFrom-Json
$Artifacts = @($ArtifactPages | ForEach-Object { @($_.artifacts) } | ForEach-Object { $_ })
$ArtifactMetadata = $Artifacts | ConvertTo-Json -Depth 20 -Compress
$ExpectedArtifactNames = @(
  'guard-evidence-ubuntu-latest',
  'guard-evidence-windows-latest'
)
$GuardArtifacts = @($Artifacts | Where-Object { $_.name -like 'guard-evidence-*' })
$ActualArtifactNameSet = @($GuardArtifacts.name | Sort-Object) -join "`n"
$ExpectedArtifactNameSet = @($ExpectedArtifactNames | Sort-Object) -join "`n"
if ($ActualArtifactNameSet -ne $ExpectedArtifactNameSet) {
  throw 'guard evidence artifact set mismatch'
}
foreach ($Name in $ExpectedArtifactNames) {
  $Match = @($GuardArtifacts | Where-Object { $_.name -eq $Name -and -not $_.expired })
  if ($Match.Count -ne 1) { throw "artifact missing, duplicate, or expired: $Name" }
  gh run download $Run.databaseId --name $Name --dir (Join-Path $CiEvidenceRoot $Name)
  if ($LASTEXITCODE -ne 0) { throw "CI artifact download failed: $Name" }
  foreach ($Required in @('verified-evidence.json', 'secret-scan.json')) {
    if (-not (Test-Path -LiteralPath (Join-Path $CiEvidenceRoot "$Name\$Required") -PathType Leaf)) {
      throw "required CI evidence missing: $Name/$Required"
    }
  }
}
$PrCommentPages = gh api --paginate --slurp "repos/$RepoName/issues/$($Pr.number)/comments?per_page=100"
if ($LASTEXITCODE -ne 0) { throw 'paginated PR comment read failed' }
$AllComments = @(
  $PrCommentPages | ConvertFrom-Json | ForEach-Object { @($_) } | ForEach-Object { $_ }
)
$Verified = Get-Content -Raw -LiteralPath (Join-Path $CiEvidenceRoot 'guard-evidence-ubuntu-latest\verified-evidence.json') | ConvertFrom-Json
$MarkerPrefix = "<!-- forge-evidence-v1 task_id=$($Verified.task_id) run_id=$($Verified.run_id) head_sha=$Sha "
$MatchingComments = @($AllComments | Where-Object { [string]$_.body -like "$MarkerPrefix*" })
if ($MatchingComments.Count -ne 1) { throw 'current task/run/head evidence comment must be exact-one' }
[System.IO.File]::WriteAllText($ArtifactMetadataPath, $ArtifactMetadata, $Utf8NoBom)
[System.IO.File]::WriteAllText($PrCommentsPath, ($AllComments | ConvertTo-Json -Depth 20 -Compress), $Utf8NoBom)
.\.venv\Scripts\python.exe -m forge.guard secret-scan --paths $Repo $BuildRoot $CiEvidenceRoot --git-repository $Repo --payload-file "ci/artifact-metadata.json=$ArtifactMetadataPath" --payload-file "github/pr-comments.json=$PrCommentsPath"
```

Expected: Ubuntu/Windows `guard-evidence-*` artifact가 실제 다운로드되고 Git object, artifact content/metadata, 현재 PR comments scan이 모두 exit 0이다.

- [ ] build manifest와 PR check URLs를 candidate seal evidence로 사용하고 manifest SHA-256을 bootstrap issue comment에 기록한다. Git tree나 별도 mutable manifest를 만들지 않는다.

**Gate:** candidate seal 뒤 `git rev-parse HEAD`와 `git status --porcelain`이 각각 `$Sha`, empty가 아니면 중단한다.

## Rollout Task 2: Windows 로컬 배포와 rollback/forward를 검증한다

**Consumes:** sealed `$Sha`, `$Manifest`, `$Artifact`.

**Produces:** Windows deployment receipt, current-user ACL, Scheduled Tasks, positive canary. Git tree는 불변이다.

**Steps:**

- [ ] Windows preflight plan을 생성한다.

```powershell
pwsh -NoProfile -File forge/scripts/deploy.ps1 -Sha $Sha -Artifact $Artifact -ArtifactSha256 $ArtifactHash -BuildManifest $Manifest -Targets Windows -RepoPaths @($Repo) -BootstrapRepository $BootstrapRepository -PlanOnly
```

Expected JSON actions: stage pre-state snapshot, stop producers, close marker, stop gateway/embedded dispatcher, drain, finalize DB/Hermes snapshot, prepare runtime, patch, hook/Task install, gateway/canary/drift audit, durable receipt, supervisor ready, marker open, producer restore. Exit 0.

- [ ] 현재 Hermes unrelated dirty status와 DB를 snapshot한다.

```powershell
$HermesRoot = Join-Path $env:LOCALAPPDATA 'hermes\hermes-agent'
$BeforeStatusPath = Join-Path $EvidenceRoot 'windows-hermes-status-before.txt'
git -C $HermesRoot status --porcelain | Set-Content -LiteralPath $BeforeStatusPath -Encoding utf8
$KanbanDb = Join-Path $env:LOCALAPPDATA 'hermes\kanban.db'
if (-not (Test-Path -LiteralPath $KanbanDb)) { throw "missing DB: $KanbanDb" }
```

- [ ] apply를 실행한다.

```powershell
pwsh -NoProfile -File forge/scripts/deploy.ps1 -Sha $Sha -Artifact $Artifact -ArtifactSha256 $ArtifactHash -BuildManifest $Manifest -Targets Windows -RepoPaths @($Repo) -BootstrapRepository $BootstrapRepository -Apply
if (-not (Test-Path -LiteralPath $WindowsReceipt)) { throw "missing receipt: $WindowsReceipt" }
```

Expected: deployment JSON `status=PASS`, receipt SHA=`$Sha`, exit 0.

- [ ] target-only Hermes patch와 user changes 보존을 확인한다.

```powershell
$AfterStatusPath = Join-Path $EvidenceRoot 'windows-hermes-status-after.txt'
git -C $HermesRoot status --porcelain | Set-Content -LiteralPath $AfterStatusPath -Encoding utf8
$GuardManifest = Join-Path $env:LOCALAPPDATA 'InfinityForge\guard\current.json'
$GuardCurrent = Get-Content -Raw -LiteralPath $GuardManifest | ConvertFrom-Json
$DeployedPython = [string]$GuardCurrent.policies.'forge-v1'.python
if (-not (Test-Path -LiteralPath $DeployedPython -PathType Leaf)) { throw "missing deployed Python: $DeployedPython" }
$PreviousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = $GuardRelease
$WindowsPatchRecord = Join-Path $env:LOCALAPPDATA 'InfinityForge\state\deployments\hermes-patch-windows.json'
& $DeployedPython "$GuardRelease\forge\scripts\hermes-patch.py" verify --root $HermesRoot --manifest "$GuardRelease\forge\patches\hermes\0.18.2\manifest.json" --record $WindowsPatchRecord --current-manifest $GuardManifest --expected-source-sha $Sha
& $DeployedPython -m forge.ops.deployment compare-hermes-status --target windows --expected-sha $Sha --current-manifest $GuardManifest --hermes-root $HermesRoot
$ApprovedBase = (git -C $HermesRoot rev-parse refs/infinity-forge/approved-base).Trim()
if ($ApprovedBase -ne '4281151ae859241351ba14d8c7682dc67ff4c126') { throw 'Windows approved-base ref mismatch' }
```

Expected: patch verify PASS; manifest target path 외 status delta 0.

- [ ] hook, Scheduled Tasks, gateway/dispatcher, ACL/DB를 확인한다.

```powershell
& $DeployedPython "$GuardRelease\forge\scripts\install-codex-hook.py" --release $GuardRelease --manifest $Manifest --repo $Repo --verify
$Tasks = @(Get-ScheduledTask -TaskPath '\INFINITY_FORGE\')
if (($Tasks.TaskName | Sort-Object) -join ',' -ne 'Canary,Dispatcher,Drift') { throw 'unexpected Scheduled Task inventory' }
& $DeployedPython -m forge.ops.canary --mode verify --target windows --sha $Sha
& $DeployedPython -m forge.ops.drift_audit --target windows --sha $Sha
& $DeployedPython -m forge.ops.hermes verify-db --path $KanbanDb --acl current-user
```

Expected: all exit 0; dispatcher child active; gateway healthy.

- [ ] negative marker test 뒤 복구한다.

```powershell
& $DeployedPython -m forge.ops.canary --mode force-stale --target windows --sha $Sha
& $DeployedPython -m forge.ops.dispatcher_supervisor status --target windows --expect stopped --within-seconds 5
& $DeployedPython -m forge.ops.hermes gateway-health --expect healthy
& $DeployedPython -m forge.ops.canary --mode run --target windows --sha $Sha
```

- [ ] controlled rollback과 같은 SHA forward deploy를 수행한다.

```powershell
$WindowsBeforeRollback = Join-Path $EvidenceRoot 'windows-before-rollback.json'
Copy-Item -LiteralPath $WindowsReceipt -Destination $WindowsBeforeRollback -Force
pwsh -NoProfile -File "$GuardRelease\forge\scripts\rollback.ps1" -BeforeReceipt $WindowsBeforeRollback -BuildManifest $Manifest -RepoPaths @($Repo)
pwsh -NoProfile -File forge/scripts/deploy.ps1 -Sha $Sha -Artifact $Artifact -ArtifactSha256 $ArtifactHash -BuildManifest $Manifest -Targets Windows -RepoPaths @($Repo) -BootstrapRepository $BootstrapRepository -Apply
$env:PYTHONPATH = $PreviousPythonPath
```

Expected: rollback PASS, previous release health PASS, forward deploy PASS.

```powershell
$WindowsHookPath = Join-Path $Repo '.codex\hooks.json'
$WindowsHookHashBeforeLinux = (Get-FileHash -Algorithm SHA256 -LiteralPath $WindowsHookPath).Hash.ToLowerInvariant()
```

Expected: Windows hook digest가 WSL staging 전 evidence에 고정된다.

**Gate:** Task registration, ACL, DB quick_check, hook hash, patch hash, gateway health, dispatcher canary 중 하나라도 실패하면 Windows rollback 후 release convergence loop를 실행한다.

## Rollout Task 3: WSL Ubuntu 일반 Linux clean install을 검증한다

**Consumes:** Windows에서 검증된 동일 `$Sha` artifact.

**Produces:** WSL deployment receipt, systemd/linger/restart/rollback evidence.

**Steps:**

- [ ] WSL Linger prerequisite를 명시적 admin bootstrap으로 충족하고 일반 사용자 read-back을 확인한다.

```powershell
wsl.exe -d Ubuntu -u root -- loginctl enable-linger immortal0900
if ($LASTEXITCODE -ne 0) { throw 'failed to provision WSL linger as root' }
wsl.exe -d Ubuntu -- bash -lc 'set -euo pipefail; test "$USER" = immortal0900; test "$HOME" = /home/immortal0900; test "$(loginctl show-user immortal0900 -p Linger --value)" = yes; systemctl --user is-system-running; python3 --version; test "$(find ~/.config/systemd/user -maxdepth 1 -name "forge-*" 2>/dev/null | wc -l)" -eq 0; test ! -e ~/.hermes/hermes-agent; test ! -e ~/.hermes/kanban.db; test ! -e ~/.local/share/infinity-forge/bootstrap/uv-0.11.24'
```

Expected: `Linger=yes`, user manager `running`, initial Forge unit 0, exit 0. Startup VBS나 foreground fallback은 없다.

- [ ] Windows와 물리적으로 분리된 Linux filesystem exact-SHA clone을 준비한다.

```powershell
$LinuxCloneCommand = @"
set -euo pipefail
repo='$LinuxRepo'
source='/mnt/c/01.project/INFINITY_FORGE'
if [[ ! -e "`$repo" ]]; then
  install -d -m 0755 "`$(dirname "`$repo")"
  GIT_TERMINAL_PROMPT=0 git clone --no-local --no-checkout "`$source" "`$repo"
  git -C "`$repo" checkout --detach '$Sha'
  git -C "`$repo" remote set-url origin 'https://github.com/$BootstrapRepository.git'
fi
test -d "`$repo/.git"
test "`$(git -C "`$repo" rev-parse HEAD)" = '$Sha'
test -z "`$(git -C "`$repo" status --porcelain --untracked-files=no)"
test "`$(git -C "`$repo" remote get-url origin)" = 'https://github.com/$BootstrapRepository.git'
test ! -s "`$repo/.git/objects/info/alternates"
common="`$(readlink -f "`$repo/`$(git -C "`$repo" rev-parse --git-common-dir)")"
objects="`$(readlink -f "`$repo/`$(git -C "`$repo" rev-parse --git-path objects)")"
test "`$common" = "`$(readlink -f "`$repo/.git")"
case "`$objects" in "`$(readlink -f "`$repo/.git")"/*) ;; *) exit 2 ;; esac
case "`$(readlink -f "`$repo")" in /home/immortal0900/*) ;; *) exit 2 ;; esac
"@
wsl.exe -d Ubuntu -- bash -lc $LinuxCloneCommand
```

Expected: committed objects를 Windows repo에서 read-only 복사한 `$LinuxRepo=/home/immortal0900/work/INFINITY_FORGE/$Sha`이고 `/mnt/c` 아래가 아니며 exact candidate SHA다. fresh/reused 모두 canonical origin, repo-local common/object directory, absent alternates, clean tracked state를 재검증한다. network credential/prompt에 의존하지 않고 기존 다른-SHA directory는 덮어쓰거나 reset하지 않고 실패한다.

- [ ] Windows orchestrator로 Linux plan/apply를 실행한다.

```powershell
pwsh -NoProfile -File forge/scripts/deploy.ps1 -Sha $Sha -Artifact $Artifact -ArtifactSha256 $ArtifactHash -BuildManifest $Manifest -Targets Linux -RepoPaths @($LinuxRepo) -BootstrapRepository $BootstrapRepository -PlanOnly
pwsh -NoProfile -File forge/scripts/deploy.ps1 -Sha $Sha -Artifact $Artifact -ArtifactSha256 $ArtifactHash -BuildManifest $Manifest -Targets Linux -RepoPaths @($LinuxRepo) -BootstrapRepository $BootstrapRepository -Apply
```

Expected: artifact copied from `$BuildRoot`, remote hash match, install PASS.

Linux `Apply`의 clean-host branch는 `/home/immortal0900/.hermes/hermes-agent`가 없을 때 hash-locked `uv==0.11.24` bootstrap venv를 trusted data root에 만들고, origin에서 exact `4281151ae859241351ba14d8c7682dc67ff4c126`을 fetch/checkout한다. 그 bootstrap `uv`로 `UV_PROJECT_ENVIRONMENT=/home/immortal0900/.hermes/hermes-agent/venv uv sync --extra all --locked`를 실행한 뒤 `venv/bin/hermes kanban init`으로 mode 600 DB를 만든다. checkout 직후 immutable approved-base ref를 zero-OID create하고 version/ref/commit/DB 검증 전에 completion patch를 적용하지 않는다.

- [ ] systemd units, linger, DB mode, hook, SHA를 확인한다.

```powershell
wsl.exe -d Ubuntu -- env FORGE_LINUX_REPO=$LinuxRepo bash -lc 'set -euo pipefail; loginctl show-user immortal0900 -p Linger --value | grep -Fx yes; systemctl --user is-enabled forge-dispatcher.service forge-canary.timer forge-drift.timer; systemctl --user is-active forge-dispatcher.service forge-canary.timer forge-drift.timer; stat -c %a ~/.hermes/kanban.db | grep -Fx 600; ~/.local/share/infinity-forge/current/forge/scripts/verify-linux-install.sh --target linux --release ~/.local/share/infinity-forge/current --manifest ~/.local/share/infinity-forge/current/build-manifest.json --repo "$FORGE_LINUX_REPO"'
wsl.exe -d Ubuntu -- bash -lc 'set -euo pipefail; release="$HOME/.local/share/infinity-forge/current"; cd "$release"; PYTHONPATH="$release" /usr/bin/python3 forge/scripts/hermes-patch.py verify --root "$HOME/.hermes/hermes-agent" --manifest forge/patches/hermes/0.18.2/manifest.json --record "$HOME/.local/state/infinity-forge/deployments/hermes-patch-linux.json" --current-manifest "$HOME/.local/share/infinity-forge/guard/current.json" --expected-source-sha '"$Sha"''
wsl.exe -d Ubuntu -- bash -lc 'set -euo pipefail; python3 - <<"PY"
import hashlib
import json
from pathlib import Path

receipt = json.loads(Path.home().joinpath(".local/state/infinity-forge/deployment-receipt-v1.json").read_bytes())
path = receipt["hermes_bootstrap_record_path"]
digest = receipt["hermes_bootstrap_record_sha256"]
assert isinstance(path, str) and path
assert isinstance(digest, str) and len(digest) == 64
record_path = Path(path)
assert hashlib.sha256(record_path.read_bytes()).hexdigest() == digest
record = json.loads(record_path.read_bytes())
assert record["stage"] == "complete"
assert record["target"] == "linux"
PY'
```

Expected: all enabled/active, DB 600, verify PASS.

- [ ] WSL restart survival을 검증한다.

```powershell
wsl.exe --terminate Ubuntu
wsl.exe -d Ubuntu -- bash -lc 'set -euo pipefail; for i in $(seq 1 30); do systemctl --user is-active --quiet forge-dispatcher.service && exit 0; sleep 1; done; systemctl --user status forge-dispatcher.service; exit 1'
wsl.exe -d Ubuntu -- bash -lc 'systemctl --user is-active forge-canary.timer forge-drift.timer'
```

Expected: 30초 안에 dispatcher와 timers active.

- [ ] stale marker, gateway isolation, rollback/forward를 검증한다.

```powershell
wsl.exe -d Ubuntu -- bash -lc "set -euo pipefail; release=\"`$HOME/.local/share/infinity-forge/current\"; cd \"`$release\"; PYTHONPATH=\"`$release\" /usr/bin/python3 -m forge.ops.canary --mode force-stale --target linux --sha $Sha"
wsl.exe -d Ubuntu -- bash -lc 'set -euo pipefail; release="$HOME/.local/share/infinity-forge/current"; cd "$release"; PYTHONPATH="$release" /usr/bin/python3 -m forge.ops.dispatcher_supervisor status --target linux --expect stopped --within-seconds 5; PYTHONPATH="$release" /usr/bin/python3 -m forge.ops.hermes gateway-health --expect healthy'
wsl.exe -d Ubuntu -- env FORGE_LINUX_REPO=$LinuxRepo bash -lc 'set -euo pipefail; install -D -m 600 ~/.local/state/infinity-forge/deployment-receipt-v1.json ~/.local/state/infinity-forge/evidence/linux-before-rollback.json; ~/.local/share/infinity-forge/current/forge/scripts/rollback-linux.sh --target linux --before-receipt ~/.local/state/infinity-forge/evidence/linux-before-rollback.json --build-manifest ~/.local/share/infinity-forge/current/build-manifest.json --repo "$FORGE_LINUX_REPO"'
wsl.exe -d Ubuntu -- bash -lc 'set -euo pipefail; test ! -e ~/.hermes/hermes-agent; test ! -e ~/.hermes/kanban.db; test ! -e ~/.local/share/infinity-forge/bootstrap/uv-0.11.24; test -f ~/.local/state/infinity-forge/deployments/hermes-bootstrap-linux.rolled-back.json'
pwsh -NoProfile -File forge/scripts/deploy.ps1 -Sha $Sha -Artifact $Artifact -ArtifactSha256 $ArtifactHash -BuildManifest $Manifest -Targets Linux -RepoPaths @($LinuxRepo) -BootstrapRepository $BootstrapRepository -Apply
$WindowsHookHashAfterLinux = (Get-FileHash -Algorithm SHA256 -LiteralPath $WindowsHookPath).Hash.ToLowerInvariant()
if ($WindowsHookHashAfterLinux -ne $WindowsHookHashBeforeLinux) { throw 'WSL rollout modified Windows hook' }
```

**Gate:** restart/linger/rollback/forward까지 green이 아니면 VPS로 진행하지 않는다.

## Rollout Task 4: Ubuntu VPS 실운영을 배포한다

**Consumes:** Windows와 Linux staging에서 검증된 동일 `$Sha`.

**Produces:** VPS deployment receipt, native headless systemd/reboot evidence, same-SHA audit.

**Steps:**

- [ ] VPS path/SHA/gateway/DB preflight를 확인한다.

```powershell
ssh -o BatchMode=yes -o ConnectTimeout=10 ubuntu@51.222.27.48 'set -euo pipefail; test "$HOME" = /home/ubuntu; test -d /home/ubuntu/work/INFINITY_FORGE/.git; test -d /home/ubuntu/.hermes/hermes-agent/.git; test -f /home/ubuntu/.hermes/kanban.db; cd /home/ubuntu/work/INFINITY_FORGE; git rev-parse HEAD; systemctl --user is-active hermes-gateway.service'
```

Expected: gateway active, exit 0.

- [ ] VPS plan/apply를 실행한다. orchestrator가 artifact/manifest/bootstrap installer를 SCP하고 각각 hash를 검증해야 한다.

```powershell
pwsh -NoProfile -File forge/scripts/deploy.ps1 -Sha $Sha -Artifact $Artifact -ArtifactSha256 $ArtifactHash -BuildManifest $Manifest -Targets Vps -RepoPaths @($VpsRepo) -BootstrapRepository $BootstrapRepository -PlanOnly
pwsh -NoProfile -File forge/scripts/deploy.ps1 -Sha $Sha -Artifact $Artifact -ArtifactSha256 $ArtifactHash -BuildManifest $Manifest -Targets Vps -RepoPaths @($VpsRepo) -BootstrapRepository $BootstrapRepository -Apply
```

Expected: remote `git pull` 0회, deployment PASS.

- [ ] exact SHA, patch preservation, systemd/linger, DB, hook을 검증한다.

```powershell
ssh -o BatchMode=yes ubuntu@51.222.27.48 'set -euo pipefail; release="$HOME/.local/share/infinity-forge/current"; cd "$release"; test "$(jq -r .source_sha build-manifest.json)" = '"$Sha"'; "$release/forge/scripts/verify-linux-install.sh" --target vps --release "$release" --manifest "$release/build-manifest.json" --repo /home/ubuntu/work/INFINITY_FORGE; PYTHONPATH="$release" /usr/bin/python3 forge/scripts/hermes-patch.py verify --root "$HOME/.hermes/hermes-agent" --manifest "$release/forge/patches/hermes/0.18.2/manifest.json" --record "$HOME/.local/state/infinity-forge/deployments/hermes-patch-vps.json" --current-manifest "$HOME/.local/share/infinity-forge/guard/current.json" --expected-source-sha '"$Sha"''
```

- [ ] native headless restart survival을 검증한다. `sudo -n`이 허용되지 않으면 전체 reboot를 생략하지 말고 사용자에게 maintenance window를 요청한다.

```powershell
ssh -o BatchMode=yes ubuntu@51.222.27.48 'sudo -n systemctl reboot'
for ($i = 0; $i -lt 60; $i++) {
  Start-Sleep -Seconds 5
  ssh -o BatchMode=yes -o ConnectTimeout=5 ubuntu@51.222.27.48 'systemctl --user is-active --quiet hermes-gateway.service forge-dispatcher.service forge-canary.timer forge-drift.timer' 2>$null
  if ($LASTEXITCODE -eq 0) { break }
}
if ($LASTEXITCODE -ne 0) { throw 'VPS services did not recover within 300 seconds' }
```

- [ ] controlled rollback과 forward를 수행한다.

```powershell
ssh -o BatchMode=yes ubuntu@51.222.27.48 'set -euo pipefail; install -D -m 600 ~/.local/state/infinity-forge/deployment-receipt-v1.json ~/.local/state/infinity-forge/evidence/vps-before-rollback.json; ~/.local/share/infinity-forge/current/forge/scripts/rollback-vps.sh --before-receipt ~/.local/state/infinity-forge/evidence/vps-before-rollback.json --build-manifest ~/.local/share/infinity-forge/current/build-manifest.json --repo /home/ubuntu/work/INFINITY_FORGE'
pwsh -NoProfile -File forge/scripts/deploy.ps1 -Sha $Sha -Artifact $Artifact -ArtifactSha256 $ArtifactHash -BuildManifest $Manifest -Targets Vps -RepoPaths @($VpsRepo) -BootstrapRepository $BootstrapRepository -Apply
```

- [ ] 세 host same-SHA audit를 실행한다.

```powershell
.\.venv\Scripts\python.exe -m forge.ops.deployment audit-targets --windows-receipt $WindowsReceipt --linux-receipt $LinuxReceipt --vps-receipt $VpsReceipt
```

Expected: project/guard/patch/schema hashes가 세 host에서 모두 동일.

**Gate:** native restart, rollback/forward, same-SHA audit 중 하나라도 실패하면 E2E로 진행하지 않는다.

## Rollout Task 5: 음성·양성 live E2E와 completion audit를 실행한다

**Consumes:** 세 host same-SHA PASS, bootstrap issue/TaskContract.

**Produces:** external final acceptance JSON, bootstrap issue comment digest, cleaned test resources.

**Steps:**

- [ ] 음성 mode를 각각 고유 run ID로 실행한다.

```powershell
$Invalid = .\.venv\Scripts\python.exe forge/scripts/e2e-early-termination.py --sha $Sha --mode invalid-handoff | ConvertFrom-Json
$Receiptless = .\.venv\Scripts\python.exe forge/scripts/e2e-early-termination.py --sha $Sha --mode receiptless | ConvertFrom-Json
$HookSkipped = .\.venv\Scripts\python.exe forge/scripts/e2e-early-termination.py --sha $Sha --mode hook-skipped | ConvertFrom-Json
foreach ($Result in @($Invalid, $Receiptless, $HookSkipped)) {
  if ($Result.status -ne 'EXPECTED_REJECTION' -or $Result.completed -or $Result.projected) {
    throw "negative E2E violated completion boundary: $($Result | ConvertTo-Json -Compress)"
  }
}
```

- [ ] 실제 Stop hook same-thread와 post-exit fallback evidence를 확인한다.

```powershell
.\.venv\Scripts\python.exe -m forge.ops.e2e_driver verify-thread-continuation --run-id $Invalid.run_id --expect-same-thread
.\.venv\Scripts\python.exe -m forge.ops.e2e_driver verify-post-exit-rejection --run-id $HookSkipped.run_id
```

- [ ] positive mode를 실행한다.

```powershell
$Positive = .\.venv\Scripts\python.exe forge/scripts/e2e-early-termination.py --sha $Sha --mode positive | ConvertFrom-Json
if ($Positive.status -ne 'PASS' -or -not $Positive.receipt_consumed -or -not $Positive.projected) {
  throw "positive E2E incomplete: $($Positive | ConvertTo-Json -Compress)"
}
```

Expected journal order: issue/card → Codex commit/PR → Stop hook → post-exit → per-PR comments → Windows/Ubuntu checks → Hermes receipt consumed/done → projection.

- [ ] thread/session, typed block, residual, multi-repo, receipt replay, drift를 audit한다.

```powershell
.\.venv\Scripts\python.exe -m forge.ops.e2e_driver audit --run-id $Positive.run_id --max-threads 4 --require-empty-or-materialized-residual --require-consumed-hermes-receipt
.\.venv\Scripts\python.exe -m forge.ops.drift_audit --target all --sha $Sha
```

- [ ] 모든 E2E resource를 tag-scoped cleanup한다.

```powershell
foreach ($RunId in @($Invalid.run_id, $Receiptless.run_id, $HookSkipped.run_id, $Positive.run_id)) {
  .\.venv\Scripts\python.exe forge/scripts/e2e-early-termination.py --sha $Sha --mode cleanup --run-id $RunId
}
```

- [ ] live canary가 끝난 exact implementation PR을 merge-commit 방식으로 병합하고 deployed head ancestry를 검증한다.

```powershell
$ImplementationPr = gh pr view --repo $BootstrapRepository --json number,headRefOid,state | ConvertFrom-Json
if ($ImplementationPr.headRefOid -ne $Sha) { throw 'implementation PR no longer matches deployed candidate' }
if ($ImplementationPr.state -eq 'CLOSED') { throw 'implementation PR was closed without merge' }
$MergedPr = if ($ImplementationPr.state -eq 'MERGED') {
  gh pr view $ImplementationPr.number --repo $BootstrapRepository --json state,mergedAt,mergeCommit,headRefOid | ConvertFrom-Json
} else { $null }
if (-not $MergedPr) {
  gh pr merge $ImplementationPr.number --repo $BootstrapRepository --merge --match-head-commit $Sha
  if ($LASTEXITCODE -ne 0) { throw 'merge-commit request failed' }
  for ($Attempt = 0; $Attempt -lt 60 -and -not $MergedPr; $Attempt++) {
    Start-Sleep -Seconds 5
    $Observed = gh pr view $ImplementationPr.number --repo $BootstrapRepository --json state,mergedAt,mergeCommit,headRefOid | ConvertFrom-Json
    if ($Observed.state -eq 'MERGED') { $MergedPr = $Observed }
  }
}
if (-not $MergedPr -or $MergedPr.headRefOid -ne $Sha) { throw 'implementation PR was not merged from deployed SHA' }
$MergeSha = [string]$MergedPr.mergeCommit.oid
if ($MergeSha -notmatch '^[0-9a-f]{40}$') { throw 'invalid merge commit SHA' }
$CompareSeparator = -join @('.', '.', '.')
$Relation = gh api "repos/$BootstrapRepository/compare/$Sha$CompareSeparator$MergeSha" | ConvertFrom-Json
if ($Relation.status -notin @('ahead','identical')) { throw 'merge commit does not preserve deployed head ancestry' }
```

`--squash`와 `--rebase`는 deployed `$Sha` ancestry를 끊으므로 금지한다. merge 전 실패는 remote source history를 바꾸지 않고 세 host rollback 여부를 판단한다. merge 뒤 defect가 발견되면 main을 rewrite/revert로 숨기지 않고 hosts를 previous release로 rollback한 뒤 corrective PR/new SHA로 Task 1부터 다시 수렴한다.

- [ ] merge commit의 main push run과 두 stable checks를 exact하게 확인한다.

```powershell
$MainRunReceipt = Join-Path $EvidenceRoot 'implementation-main-run.json'
$MainRun = $null
if (Test-Path -LiteralPath $MainRunReceipt) {
  $Saved = Get-Content -Raw -LiteralPath $MainRunReceipt | ConvertFrom-Json
  if ($Saved.merge_sha -ne $MergeSha) { throw 'stale implementation main-run receipt' }
  $MainRun = [long]$Saved.run_id
} else {
  for ($Attempt = 0; $Attempt -lt 60 -and -not $MainRun; $Attempt++) {
    Start-Sleep -Seconds 5
    $Runs = @(gh run list --repo $BootstrapRepository --workflow capability-eval.yml --commit $MergeSha --event push --limit 100 --json databaseId,createdAt,headSha,event,status,conclusion | ConvertFrom-Json)
    $Successful = @($Runs | Where-Object { $_.headSha -eq $MergeSha -and $_.event -eq 'push' -and $_.conclusion -eq 'success' } | Sort-Object createdAt -Descending)
    $Candidates = @($Runs | Where-Object { $_.headSha -eq $MergeSha -and $_.event -eq 'push' } | Sort-Object createdAt -Descending)
    if ($Successful.Count -gt 0) { $MainRun = [long]$Successful[0].databaseId }
    elseif ($Candidates.Count -gt 0) { $MainRun = [long]$Candidates[0].databaseId }
  }
}
if (-not $MainRun) { throw 'main push workflow run not found' }
gh run watch $MainRun --repo $BootstrapRepository --exit-status
$Run = gh run view $MainRun --repo $BootstrapRepository --json headSha,jobs | ConvertFrom-Json
if ($Run.headSha -ne $MergeSha) { throw 'main push run SHA mismatch' }
foreach ($Name in @('guard-contract (ubuntu-latest)', 'guard-contract (windows-latest)')) {
  $Checks = @($Run.jobs | Where-Object { $_.name -eq $Name })
  if ($Checks.Count -ne 1 -or $Checks[0].conclusion -ne 'success') { throw "main check mismatch: $Name" }
}
$MainRunBytes = [Text.Encoding]::UTF8.GetBytes((@{schema_version='forge-main-run/v1';merge_sha=$MergeSha;run_id=$MainRun} | ConvertTo-Json -Compress) + "`n")
$MainRunTemp = "$MainRunReceipt.$([Guid]::NewGuid().ToString('N')).tmp"
$Stream = [IO.File]::Open($MainRunTemp, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
try { $Stream.Write($MainRunBytes, 0, $MainRunBytes.Length); $Stream.Flush($true) } finally { $Stream.Dispose() }
Move-Item -LiteralPath $MainRunTemp -Destination $MainRunReceipt -Force
```

- [ ] merged PR predicate를 포함한 coverage, credential scan, 20개 acceptance mapping을 생성한다.

```powershell
.\.venv\Scripts\python.exe -m forge.ops.spec_coverage --format json --require-complete
$FinalEvidence = Join-Path $EvidenceRoot 'hermes-guard-final-acceptance.json'
.\.venv\Scripts\python.exe -m forge.guard secret-scan --paths $Repo $BuildRoot $EvidenceRoot
.\.venv\Scripts\python.exe -m forge.ops.e2e_driver acceptance-report --spec docs/weapon/specs/2026-07-12-hermes-early-termination-guards-design.md --run-id $Positive.run_id --output $FinalEvidence --require-count 20
```

- [ ] final immutable assertions를 실행한다.

```powershell
if ((git rev-parse HEAD).Trim() -ne $Sha) { throw 'Git SHA changed during rollout' }
if (@(git status --porcelain).Count -ne 0) { throw 'Git tree changed during rollout' }
.\.venv\Scripts\python.exe -m forge.ops.deployment audit-targets --windows-receipt $WindowsReceipt --linux-receipt $LinuxReceipt --vps-receipt $VpsReceipt
.\.venv\Scripts\python.exe -m forge.guard secret-scan --paths $FinalEvidence
```

- [ ] schedule/workflow_dispatch가 읽을 current ops evidence를 최종 assertions 뒤 exact-SHA로 승격한다.

```powershell
$OpsEvidence = Join-Path $EvidenceRoot 'forge-ops-evidence-v1.json'
.\.venv\Scripts\python.exe -m forge.ops.e2e_driver build-ops-evidence --sha $Sha --build-manifest $Manifest --windows-receipt $WindowsReceipt --linux-receipt $LinuxReceipt --vps-receipt $VpsReceipt --require-current-activation --max-canary-age-seconds 25200 --max-drift-age-seconds 7200 --output $OpsEvidence
.\.venv\Scripts\python.exe -m forge.guard secret-scan --paths $OpsEvidence --git-repository $Repo
.\.venv\Scripts\python.exe -m forge.ops.e2e_driver promote-ops-evidence --repository $BootstrapRepository --issue $BootstrapIssue --evidence $OpsEvidence --marker forge-ops-evidence-v1 --upsert-exact-sha --set-deployed-sha-variable
$PromotedSha = (gh variable get FORGE_DEPLOYED_SHA --repo $BootstrapRepository --json value --jq .value).Trim()
if ($PromotedSha -ne $Sha) { throw "FORGE_DEPLOYED_SHA read-back mismatch: $PromotedSha" }
gh api "repos/$BootstrapRepository/commits/$Sha" --jq .sha | Select-String -SimpleMatch $Sha | Out-Null
```

Expected: Windows/Linux/VPS exact target set과 same source/build digest, durable success/activation-open, fresh canary/drift가 canonical comment 하나에 기록되고 canonical ops host의 `FORGE_DEPLOYED_SHA`가 exact SHA로 read-back된다. promotion adapter는 comment request bytes를 transport 전에 다시 scan하고 동일 SHA marker가 2개 이상이면 쓰지 않고 실패한다. comment/variable 중간 실패는 이전 두 상태로 보상 복구하며, 복구까지 실패하면 세 target rollback 후 corrective PR/new SHA 수렴 cycle로 간다. secondary repository schedule은 이 중앙 evidence를 읽지 않는다.

- [ ] 마지막 side effect로 Slack request JSON을 만들고 동일 bytes를 scan한 뒤 `codex work report` 앱으로 전송한다.

```powershell
$SlackRequest = Join-Path $EvidenceRoot 'codex-work-report-request.json'
$SlackReceipt = Join-Path $EvidenceRoot 'codex-work-report-receipt.json'
$SlackEnvFile = 'C:\Users\황화인HwainHwang\.codex\secrets\codex-work-report.env'
.\.venv\Scripts\python.exe -m forge.ops.work_report render --channel C0BES16KE1J --sha $Sha --evidence $FinalEvidence --output $SlackRequest
.\.venv\Scripts\python.exe -m forge.guard secret-scan --paths $FinalEvidence --git-repository $Repo --payload-file "slack/chat.postMessage.request.json=$SlackRequest"
.\.venv\Scripts\python.exe forge/scripts/post-work-report.py --request-file $SlackRequest --env-file $SlackEnvFile --receipt $SlackReceipt
```

Expected: scanner exit 0 뒤 post command가 app/channel을 확인하고 secret/header/raw response 없이 `ok`, `channel`, `ts`만 출력한다. same request digest 재시도는 durable sent receipt면 transport 0회이며, API accept 직후 crash한 pending receipt도 동일 `client_msg_id`로 수렴해 visible message가 하나다. scan 실패면 Slack transport는 0회다.

## Release convergence loop

어느 rollout Task에서든 제품 코드/설정/unit defect가 확인되면 다음 순서를 한 번의 원자적 회복 흐름으로 수행한다.

1. 세 host canary marker를 닫고 independent dispatcher를 중단한다.
2. 생성된 E2E resource를 run tag로 cleanup한다.
3. 이미 candidate가 배포된 host를 각 deployment receipt의 previous common release로 rollback한다.
4. DB quick_check와 gateway health를 확인한다. DB snapshot 복원은 integrity failure일 때만 한다.
5. 실패를 재현하는 RED test를 추가하고 최소 fix를 commit한다.
6. 새 `$Sha`로 Rollout Task 1부터 Windows→Linux→VPS→E2E를 전부 반복한다.

부분 host만 새 SHA로 유지하거나 live target에서 ad-hoc edit하는 경로는 없다.

## 변경이력

- 2026-07-12 | rollout subplan 작성 | 변경: Windows→WSL Ubuntu→VPS exact-SHA 배포, restart/rollback/forward, 음성·양성 E2E와 수렴 loop의 실제 command/gate를 고정 | 검증: 현재 환경의 WSL distro/user, VPS SSH/repo, Windows/VPS Hermes DB path를 read-only로 확인; 실행 검증은 implementation 완료 뒤 수행
- 2026-07-12 | verifier·rollback CLI 교차계약 정합화 | 변경: Hermes verify를 root/patch manifest/install record/guard current/source SHA 5인자로 고정하고 Windows·Linux·VPS rollback을 before/after receipt와 build manifest 기반 exact interface로 통일 | 이유: 설치 후 검증과 별도 프로세스 rollback이 같은 receipt를 덮어쓰거나 서로 다른 parser 인자를 사용하지 않도록 함 | 검증: 구현 전 계획 단계이며 fenced PowerShell/Bash parser와 ops/completion subplan 대조로 확인
- 2026-07-12 | private ops host·WSL 격리·deployed-SHA promotion 보강 | 변경: Windows interactive GitHub auth prerequisite, explicit bootstrap repository, host-only variables, every deploy의 repository argv, root Linger bootstrap, Linux filesystem exact-SHA clone과 Windows hook hash 불변, 7시간 canary evidence, comment+`FORGE_DEPLOYED_SHA` 보상 promotion을 rollout 명령에 반영 | 이유: 현재 Windows gh 미로그인/private repo, WSL no-sudo/shared Windows repo, PR head와 main SHA 차이에서도 승인된 exact candidate를 세 target에 안전하게 수렴시키기 위함 | 검증: 구현 전 계획 단계에서 PowerShell/Bash parser, deploy signature scan, 독립 P0/P1 review 대상으로 등록
