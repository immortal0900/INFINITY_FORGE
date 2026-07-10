#!/bin/bash
# INFINITY_FORGE nightly-backup v2 (D20 Phase 0~1 임시판 + MEMEX 볼륨 확장)
# - hermes: kanban.db/state.db sqlite .backup + integrity_check
# - MEMEX: vault/state 볼륨 무중단 tar, neo4j는 짧은 정지 후 tar(정합성 보장)
# - 실패 시 Slack #forge-cloud 직발송 (hermes 우회 — 감시자는 감시 대상에 의존하지 않는다)
# Phase 2에서 Litestream(오프박스) 승격 예정. 백업이 같은 디스크에 있는 한계는 그때 해소.
set -u

TS=$(date +%Y%m%d)
DEST=~/backups/hermes/$TS
LOG=~/backups/backup.log

log() { echo "$(date -Is) $*" >> "$LOG"; }

slack_fail() {
  MSG="$1"
  TOKEN=$(grep '^SLACK_BOT_TOKEN=' ~/.hermes/.env 2>/dev/null | cut -d= -f2)
  [ -n "$TOKEN" ] && curl -s -m 10 -X POST https://slack.com/api/chat.postMessage \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"channel\":\"#forge-cloud\",\"text\":\"🚨 [INFINITY_FORGE] 백업 실패: $MSG\"}" > /dev/null
  log "FAIL $MSG"
}

fail() { slack_fail "$1"; exit 2; }
trap 'fail "backup script crashed at line $LINENO"' ERR

mkdir -p "$DEST"

# ── 1. hermes SQLite ────────────────────────────────────
for DB in ~/.hermes/kanban.db ~/.hermes/state.db; do
  NAME=$(basename "$DB")
  sqlite3 "$DB" ".backup '$DEST/$NAME'"
  CHECK=$(sqlite3 "$DEST/$NAME" "PRAGMA integrity_check;")
  [ "$CHECK" = "ok" ] || fail "integrity_check failed: $NAME ($CHECK)"
  [ -s "$DEST/$NAME" ] || fail "empty backup: $NAME"
done
cp ~/.hermes/config.yaml "$DEST/" 2>/dev/null || true

# ── 2. MEMEX vault·state (파일 기반, 무중단) ─────────────
sudo -n tar czf "$DEST/memex-vault.tgz" -C /var/lib/docker/volumes/deploy_vault/_data . \
  || fail "vault tar 실패"
sudo -n tar czf "$DEST/memex-state.tgz" -C /var/lib/docker/volumes/deploy_state/_data . \
  || fail "memex-state tar 실패"

# ── 3. MEMEX neo4j (정합성 위해 짧은 정지) ────────────────
sudo -n docker stop memex-neo4j > /dev/null || fail "neo4j stop 실패"
sudo -n tar czf "$DEST/memex-neo4j-data.tgz" -C /var/lib/docker/volumes/deploy_neo4j-data/_data . \
  || { sudo -n docker start memex-neo4j > /dev/null; fail "neo4j tar 실패(컨테이너는 재시작함)"; }
sudo -n docker start memex-neo4j > /dev/null || fail "neo4j start 실패 — MEMEX 다운 상태!"
# 재기동 확인 (최대 60초)
H=""
for i in $(seq 1 12); do
  H=$(sudo -n docker inspect memex-neo4j --format '{{.State.Health.Status}}' 2>/dev/null)
  [ "$H" = "healthy" ] && break
  sleep 5
done
[ "$H" = "healthy" ] || slack_fail "neo4j 재기동 후 healthy 미도달(현재: $H) — 확인 필요"

# ── 4. 크기 검증 + 7일 롤링 ─────────────────────────────
[ -s "$DEST/memex-neo4j-data.tgz" ] || fail "neo4j 백업 크기 0"
find ~/backups/hermes -maxdepth 1 -type d -mtime +7 -exec rm -rf {} \;
log "OK $DEST ($(du -sh $DEST | cut -f1), files=$(ls $DEST | wc -l))"
echo "backup ok: $DEST"
