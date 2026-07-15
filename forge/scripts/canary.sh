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

# 게이트 검문 fixture 공통: 유효 핸드오프 (v0.2부터 핸드오프 필수 — D16)
HANDOFF_OK='{"pr_url":"https://github.com/example/project/pull/1","changed_files":["file.txt"],"implemented":["AC1"],"not_implemented":[],"verified_by":{"AC1":"canary fixture"}}'

# 1. 게이트: 빈 diff는 차단(exit 2)해야 정상
T=$(mktemp -d); (cd "$T" && git init -q)
if ~/forge/hooks/codex-stop-gate.sh "$T" 2>/dev/null; then
  FAIL_MSGS="$FAIL_MSGS [게이트가 빈 diff를 통과시킴]"
fi
# 2. 게이트: 정상 변경 + 유효 핸드오프는 통과(exit 0)해야 정상
echo canary > "$T/file.txt"
echo "$HANDOFF_OK" > "$T/handoff.json"
if ! ~/forge/hooks/codex-stop-gate.sh "$T" 2>/dev/null; then
  FAIL_MSGS="$FAIL_MSGS [게이트가 정상 변경을 차단함]"
fi
# 3. 게이트: 핸드오프 없는 종료는 차단해야 정상 (D16 — 2026-07-12 실측 구멍 회귀 감시)
rm "$T/handoff.json"
if ~/forge/hooks/codex-stop-gate.sh "$T" 2>/dev/null; then
  FAIL_MSGS="$FAIL_MSGS [게이트가 핸드오프 없는 종료를 통과시킴]"
fi
# 4. 게이트: implemented가 빈 핸드오프는 차단해야 정상 (D17)
echo '{"implemented":[],"not_implemented":[],"verified_by":{}}' > "$T/handoff.json"
if ~/forge/hooks/codex-stop-gate.sh "$T" 2>/dev/null; then
  FAIL_MSGS="$FAIL_MSGS [게이트가 빈 implemented를 통과시킴]"
fi
rm -rf "$T"
# 5. 게이트: 커밋해서 워크트리가 깨끗해도 base SHA 기준으로 통과해야 정상 (committed-clean 오판 회귀 감시)
T=$(mktemp -d); (cd "$T" && git init -q \
  && echo seed > seed.txt && git add . && git -c user.email=canary@forge -c user.name=canary commit -qm seed \
  && git rev-parse HEAD > .forge-base-sha \
  && echo work > file.txt && git add file.txt && git -c user.email=canary@forge -c user.name=canary commit -qm work)
echo "$HANDOFF_OK" > "$T/handoff.json"
if ! ~/forge/hooks/codex-stop-gate.sh "$T" 2>/dev/null; then
  FAIL_MSGS="$FAIL_MSGS [게이트가 커밋된 정상 작업을 empty diff로 차단함]"
fi
rm -rf "$T"
# 3. 게이트웨이 생존
systemctl --user is-active hermes-gateway > /dev/null || FAIL_MSGS="$FAIL_MSGS [게이트웨이 다운]"
# 4. codex 인증
# codex는 상태를 stderr로 출력하므로 2>&1 필수 (오탐 이력 있음)
PATH="$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH" codex login status 2>&1 | grep -q "Logged in" \
  || FAIL_MSGS="$FAIL_MSGS [codex 미인증]"

if [ -n "$FAIL_MSGS" ]; then
  slack "🚨 [INFINITY_FORGE] 카나리아 실패:$FAIL_MSGS — 오늘 밤 배차 신뢰 불가, 확인 필요"
  echo "CANARY_FAIL:$FAIL_MSGS" >&2
  exit 2
fi
slack "🐤 [INFINITY_FORGE] 카나리아 통과 — 게이트·게이트웨이·codex 인증 정상, 야간 준비 완료 ($(~/forge/spec-coverage.sh 2>/dev/null || echo '커버리지 n/a'))"
echo "CANARY_OK"
