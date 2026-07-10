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
  git add forge/ docs/
  git diff --cached --quiet
  if ($LASTEXITCODE -ne 0) { git commit -m $Message }
  git pull --rebase --autostash
  git push
}

Write-Host "[deploy] VPS 반영..."
ssh ubuntu@51.222.27.48 "bash ~/work/INFINITY_FORGE/forge/scripts/deploy-vps.sh"

Write-Host "[deploy] 로컬 hermes 반영..."
$LocalSkills = "$env:LOCALAPPDATA\hermes\skills"
foreach ($S in @("forge-ops", "memex")) {
  if (Test-Path "$Repo\forge\skills\$S") {
    Copy-Item -Recurse -Force "$Repo\forge\skills\$S" $LocalSkills
  }
}
Write-Host "[deploy] done"
