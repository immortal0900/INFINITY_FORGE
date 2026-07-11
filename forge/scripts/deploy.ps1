# INFINITY_FORGE — 로컬 배포 스크립트 (deploy-skill.ps1 패턴)
# 하는 일: ① git push ② VPS에서 deploy-vps.sh 실행 ③ 로컬 hermes에 스킬 반영
# 사용법: .\forge\scripts\deploy.ps1 [-Message "커밋메시지"] [-SkipPush]
param(
  [string]$Message = "",
  [switch]$SkipPush
)
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $Repo

if (-not $SkipPush) {
  if ([string]::IsNullOrWhiteSpace($Message)) { $Message = "forge: 자산 업데이트" }
  git add forge/ docs/ .github/
  git diff --cached --quiet
  if ($LASTEXITCODE -ne 0) { git commit -m $Message }
  git pull --rebase --autostash
  git push
}

Write-Host "[deploy] VPS 반영..."
ssh ubuntu@51.222.27.48 "bash ~/work/INFINITY_FORGE/forge/scripts/deploy-vps.sh"

Write-Host "[deploy] 로컬 hermes 반영..."
$LocalSkills = "$env:LOCALAPPDATA\hermes\skills"
# 게이트웨이(기본 프로필) 스킬
foreach ($S in @("forge-ops", "memex", "code-design-principles", "forge-labels", "easy-answer", "code-problem-doc", "forge-cloud-board")) {
  if (Test-Path "$Repo\forge\skills\$S") { Copy-Item -Recurse -Force "$Repo\forge\skills\$S" $LocalSkills }
}
# 워커 프로필 매핑 (클라우드 deploy-vps.sh와 동일 구조)
$roleMap = @{
  "issuefinder" = @("issue-finder-sot")
  "executor"    = @("kanban-codex-delegate")
  "reviewer"    = @("reviewer-verdict", "code-problem-doc")
  "critic"      = @("critic-adversarial")
}
foreach ($P in @("issuefinder", "executor", "reviewer", "critic")) {
  $PS = "$env:LOCALAPPDATA\hermes\profiles\$P\skills"
  if (-not (Test-Path $PS)) { continue }
  foreach ($S in (@("forge-ops", "memex", "code-design-principles", "forge-labels") + $roleMap[$P])) {
    if (Test-Path "$Repo\forge\skills\$S") { Copy-Item -Recurse -Force "$Repo\forge\skills\$S" $PS }
  }
}
# Stop 훅 게이트 (Git Bash로 실행 가능)
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\forge\hooks", "$env:USERPROFILE\forge\outbox\sent" | Out-Null
Copy-Item -Force "$Repo\forge\hooks\codex-stop-gate.sh" "$env:USERPROFILE\forge\hooks\"
Write-Host "[deploy] done"
