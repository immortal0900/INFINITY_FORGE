# INFINITY_FORGE — VPS 백업을 로컬 노트북으로 pull (오프박스 사본, 추가 비용 0)
# 등록: 로그온 시 자동 실행(작업 스케줄러). VPS가 죽어도 노트북에 최근 백업이 남는다.
# 보관: 로컬 14일 롤링. 로그: %USERPROFILE%\forge-backups\pull.log
$ErrorActionPreference = "Stop"
$Dest = "$env:USERPROFILE\forge-backups"
$Log = "$Dest\pull.log"
New-Item -ItemType Directory -Force -Path $Dest | Out-Null

function Log($m) { Add-Content -Path $Log -Value "$(Get-Date -Format o) $m" }

try {
  # VPS의 최신 백업 날짜 폴더 확인
  $latest = ssh ubuntu@51.222.27.48 "ls -td ~/backups/hermes/*/ | head -1 | xargs basename" 2>$null
  if (-not $latest) { Log "FAIL: VPS 백업 폴더 없음"; exit 1 }
  $target = "$Dest\$latest"
  if (Test-Path "$target\.complete") { Log "SKIP: $latest 이미 수집됨"; exit 0 }
  New-Item -ItemType Directory -Force -Path $target | Out-Null
  scp -q -r "ubuntu@51.222.27.48:~/backups/hermes/$latest/*" $target
  if ($LASTEXITCODE -ne 0) { Log "FAIL: scp 실패 ($latest)"; exit 1 }
  New-Item -ItemType File -Force -Path "$target\.complete" | Out-Null
  # 14일 초과 로컬 사본 정리
  Get-ChildItem $Dest -Directory | Where-Object { $_.CreationTime -lt (Get-Date).AddDays(-14) } |
    Remove-Item -Recurse -Force -Confirm:$false
  Log "OK: $latest 수집 ($((Get-ChildItem $target | Measure-Object -Sum Length).Sum) bytes)"
} catch {
  Log "FAIL: $($_.Exception.Message)"
  exit 1
}
