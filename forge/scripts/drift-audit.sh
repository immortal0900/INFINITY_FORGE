#!/bin/bash
# INFINITY_FORGE drift-audit — 불변식·체류·신선도 감시 (LLM 0, 60분 주기)
# 위반 시에만 Slack 알림 (정상이면 침묵)
set -u
V=""

# 1. 게이트웨이·타이머 생존
systemctl --user is-active hermes-gateway > /dev/null || V="$V [게이트웨이 다운]"
# 2. 백업 신선도 (<26h)
LATEST=$(ls -td ~/backups/hermes/*/ 2>/dev/null | head -1)
if [ -z "$LATEST" ] || [ $(( $(date +%s) - $(stat -c %Y "$LATEST") )) -gt 93600 ]; then
  V="$V [백업 26h 초과]"
fi
# 3. ready/todo 장기 체류 (2h+, D 노브: 스테일 회수 하향 기준)
STUCK=$(sqlite3 "file:$HOME/.hermes/kanban.db?mode=ro" \
  "SELECT count(*) FROM tasks WHERE status IN ('ready','todo') AND created_at < strftime('%s','now') - 7200" 2>/dev/null || echo 0)
[ "${STUCK:-0}" -gt 0 ] && V="$V [ready/todo 2h+ 체류 ${STUCK}건]"
# 4. 디스크
DUSE=$(df --output=pcent / | tail -1 | tr -dc '0-9')
[ "${DUSE:-0}" -gt 85 ] && V="$V [디스크 ${DUSE}%]"
# 5. outbox 적체 (>20)
OB=$(ls ~/forge/outbox/*.md 2>/dev/null | wc -l)
[ "${OB:-0}" -gt 20 ] && V="$V [outbox 적체 ${OB}건]"
# 6. 라벨 불변식: open 이슈의 forge:* 라벨은 정확히 0 또는 1개 (0 = 미투입 이슈라 허용)
DUP=$(/usr/bin/gh api "repos/immortal0900/INFINITY_FORGE/issues?state=open&per_page=50" \
  --jq '[.[] | select(has("pull_request") | not) | [.labels[].name | select(startswith("forge:"))] | select(length > 1)] | length' 2>/dev/null || echo 0)
[ "${DUP:-0}" -gt 0 ] && V="$V [forge 라벨 2개+ 이슈 ${DUP}건]"

if [ -n "$V" ]; then
  TOKEN=$(grep '^SLACK_BOT_TOKEN=' ~/.hermes/.env 2>/dev/null | cut -d= -f2)
  [ -n "$TOKEN" ] && curl -s -m 10 -X POST https://slack.com/api/chat.postMessage \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"channel\":\"#forge-cloud\",\"text\":\"⚠️ [INFINITY_FORGE] drift-audit 위반:$V\"}" > /dev/null
  echo "DRIFT:$V" >&2
  exit 1
fi
echo "drift-audit ok"
