# INFINITY_FORGE — hermes update 후 Zscaler CA 재적용
# 배경: hermes update가 venv를 재생성하면 certifi에 추가했던 Zscaler 체인이 사라져
#       모든 HTTPS(인증·Slack·github MCP)가 CERTIFICATE_VERIFY_FAILED로 죽는다.
#       %LOCALAPPDATA%\hermes\ca-bundle.pem(독립 사본)은 살아남으므로 여기서 복원한다.
# 사용: hermes update 직후 이 스크립트 실행. (증상: hermes가 SSL 에러를 뱉을 때)
$bundle = "$env:LOCALAPPDATA\hermes\ca-bundle.pem"
$cacert = "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Lib\site-packages\certifi\cacert.pem"
if (-not (Test-Path $bundle)) { Write-Error "ca-bundle.pem 없음 — Zscaler 체인을 다시 추출해야 함 (openssl s_client -showcerts)"; exit 1 }
if (-not (Test-Path $cacert)) { Write-Error "certifi cacert.pem 없음 — hermes venv 경로 확인"; exit 1 }
if ((Get-Content $cacert -Raw) -match "Zscaler") { Write-Output "certifi에 Zscaler 이미 존재 — 재적용 불필요"; exit 0 }
# ca-bundle.pem에서 Zscaler 주석 이후 부분(우리가 추가한 체인)을 그대로 이어붙임
$marker = (Select-String -Path $bundle -Pattern "Zscaler").LineNumber | Select-Object -First 1
if (-not $marker) { Write-Error "ca-bundle.pem에 Zscaler 항목 없음"; exit 1 }
$lines = Get-Content $bundle
$chain = $lines[($marker - 2)..($lines.Count - 1)] -join "`n"
Add-Content -Path $cacert -Value "`n$chain"
Write-Output "certifi에 Zscaler 체인 재적용 완료"
