# INFINITY_FORGE — Desktop용 VPS 대시보드 session token 갱신 스크립트
# 용도: VPS 대시보드가 재시작되면 토큰이 바뀐다 → 이 스크립트 실행(더블클릭용 cmd 참조)하면
#       최신 토큰을 받아 %USERPROFILE%\forge-backups\vps-dashboard-cred.txt 에 갱신해준다.
# 값은 화면에 출력하지 않는다. Desktop Settings→Gateway→Session token에 붙여넣는다.
$ErrorActionPreference = "Stop"
$cred = "$env:USERPROFILE\forge-backups\vps-dashboard-cred.txt"
# VPS 쪽에서: .env의 basic auth로 대시보드 SPA를 받아 토큰만 추출해 stdout으로
$tok = ssh ubuntu@51.222.27.48 'U=$(grep "^HERMES_DASHBOARD_BASIC_AUTH_USERNAME=" ~/.hermes/.env | cut -d= -f2); P=$(grep "^HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=" ~/.hermes/.env | cut -d= -f2); curl -s -u "$U:$P" http://127.0.0.1:9119/ | grep -oE "__HERMES_SESSION_TOKEN__=\"[^\"]+\"" | head -1 | cut -d\" -f2'
if (-not $tok) { Write-Output "토큰을 찾지 못함 — VPS 대시보드 상태 확인 필요 (systemctl --user status hermes-dashboard)"; exit 1 }
# cred 파일에서 기존 token 줄 제거 후 추가
$lines = (Get-Content $cred -ErrorAction SilentlyContinue) | Where-Object { $_ -notmatch "^session-token:" }
$lines += "session-token: $tok"
Set-Content -Path $cred -Value $lines
Write-Output "갱신 완료 — 메모장에서 $cred 열어 session-token 줄을 복사하세요 (길이: $($tok.Length)자)"
