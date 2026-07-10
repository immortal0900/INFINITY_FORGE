#!/bin/bash
# INFINITY_FORGE canary — 밤 시작(21:00 KST) 검문소 자체 점검 (LLM 0)
# 게이트가 "차단해야 할 것을 차단하고, 통과시켜야 할 것을 통과"시키는지 정답이 알려진 더미로 검사.
# 실패 시 Slack 🚨 (배차 중단 대신 알림 — 디스패처는 게이트웨이 내장이라 v0.1은 알림까지)
set -u
FAIL_MSGS=""

slack() {
  TOKEN=$(grep '^SLACK_BOT_TOKEN=' ~/.hermes/.env 2>/dev/null | cut -d= -f2)
  [ -n "$TOKEN" ] && curl -s -m 10 -X POST https://slack.com/api/chat.postMessage \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"channel\":\"#forge-cloud\",\"text\":\"$1\"}" > /dev/null
}

# 1. 게이트: 빈 diff는 차단(exit 2)해야 정상
T=$(mktemp -d); (cd "$T" && git init -q)
if ~/forge/hooks/codex-stop-gate.sh "$T" 2>/dev/null; then
  FAIL_MSGS="$FAIL_MSGS [게이트가 빈 diff를 통과시킴]"
fi
# 2. 게이트: 정상 변경은 통과(exit 0)해야 정상
echo canary > "$T/file.txt"
if ! ~/forge/hooks/codex-stop-gate.sh "$T" 2>/dev/null; then
  FAIL_MSGS="$FAIL_MSGS [게이트가 정상 변경을 차단함]"
fi
rm -rf "$T"
# 3. 게이트웨이 생존
systemctl --user is-active hermes-gateway > /dev/null || FAIL_MSGS="$FAIL_MSGS [게이트웨이 다운]"
# 4. codex 인증
PATH="$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH" codex login status 2>/dev/null | grep -q "Logged in" \
  || FAIL_MSGS="$FAIL_MSGS [codex 미인증]"

if [ -n "$FAIL_MSGS" ]; then
  slack "🚨 [INFINITY_FORGE] 카나리아 실패:$FAIL_MSGS — 오늘 밤 배차 신뢰 불가, 확인 필요"
  echo "CANARY_FAIL:$FAIL_MSGS" >&2
  exit 2
fi
slack "🐤 [INFINITY_FORGE] 카나리아 통과 — 게이트·게이트웨이·codex 인증 정상, 야간 준비 완료 ($(~/forge/spec-coverage.sh 2>/dev/null || echo '커버리지 n/a'))"
echo "CANARY_OK"
