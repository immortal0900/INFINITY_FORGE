#!/bin/bash
# INFINITY_FORGE morning-report — 아침 07:30 KST 집계 (LLM 0)
# 상단 고정: spec 커버리지 → 사람 결정·실패·리뷰 대기 → 24h Task 집계
# + MEMEX 일별 진행상태 메시지 1건 적재
set -u
DB=~/.hermes/kanban.db
SINCE=$(( $(date +%s) - 86400 ))

COV=$(~/forge/spec-coverage.sh 2>/dev/null || echo "커버리지 n/a")
DONE=$(sqlite3 "file:$DB?mode=ro" "SELECT count(*) FROM tasks WHERE status='done' AND completed_at > $SINCE")
FAILED=$(sqlite3 "file:$DB?mode=ro" "SELECT count(*) FROM tasks WHERE status='failed'")
BLOCKED=$(sqlite3 "file:$DB?mode=ro" "SELECT count(*) FROM tasks WHERE status='blocked'")
RUNNING=$(sqlite3 "file:$DB?mode=ro" "SELECT count(*) FROM tasks WHERE status='running'")
PRS=$(/usr/bin/gh pr list --repo immortal0900/INFINITY_FORGE --state open --json number,title \
  --jq '.[] | "#\(.number) \(.title)"' 2>/dev/null | head -5)
DECISIONS=$(/usr/bin/gh api "repos/immortal0900/INFINITY_FORGE/issues?state=open&labels=forge:needs-decision&per_page=10" \
  --jq '.[] | "#\(.number) \(.title)"' 2>/dev/null | head -5)
BK=$(tail -1 ~/backups/backup.log 2>/dev/null | cut -c1-60)

TEXT="☀️ [INFINITY_FORGE] 아침 리포트 $(TZ=Asia/Seoul date +%m/%d)
■ $COV
■ 24h: done $DONE · running $RUNNING · blocked $BLOCKED · failed(누적) $FAILED
■ 리뷰 대기 PR:
${PRS:-  (없음)}
■ 사람 결정 대기:
${DECISIONS:-  (없음)}
■ 백업: ${BK:-기록 없음}"

TOKEN=$(grep '^SLACK_BOT_TOKEN=' ~/.hermes/.env 2>/dev/null | cut -d= -f2)
[ -n "$TOKEN" ] && curl -s -m 10 -X POST https://slack.com/api/chat.postMessage \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "$(python3 -c "import json,sys; print(json.dumps({'channel':'#forge-cloud','text':sys.stdin.read()}))" <<< "$TEXT")" > /dev/null

# MEMEX 일별 진행상태 메시지 (send-pending-messages가 배달)
cat > ~/forge/outbox/$(date +%s)-daily-status.md << ENTRY
## [insight] 일별 운영 현황 $(TZ=Asia/Seoul date +%Y-%m-%d)
project:: INFINITY_FORGE
tags:: daily, status, mirror
recorded_at:: $(TZ=Asia/Seoul date +%Y-%m-%d)

$COV. 최근 24h 카드: done $DONE, running $RUNNING, blocked $BLOCKED, failed(누적) $FAILED.
리뷰 대기 PR: ${PRS:-없음}. 사람 결정 대기: ${DECISIONS:-없음}. 백업: ${BK:-기록 없음}.
(진행상태 read-only 미러 — 재개·복구는 항상 kanban 원본 기준, D4)
ENTRY
echo "morning report sent"
