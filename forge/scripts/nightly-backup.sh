#!/bin/bash
# INFINITY_FORGE nightly-backup (D20 Phase 0~1 임시판. Phase 2에 Litestream 승격)
trap 'exit 2' ERR
TS=$(date +%Y%m%d)
DEST=~/backups/hermes/$TS
mkdir -p "$DEST"
for DB in ~/.hermes/kanban.db ~/.hermes/state.db; do
  NAME=$(basename "$DB")
  sqlite3 "$DB" ".backup '$DEST/$NAME'"
  CHECK=$(sqlite3 "$DEST/$NAME" "PRAGMA integrity_check;")
  if [ "$CHECK" != "ok" ]; then echo "GATE_ERROR: integrity_check failed for $NAME: $CHECK" >&2; exit 2; fi
  # 크기 0 방지
  [ -s "$DEST/$NAME" ] || { echo "GATE_ERROR: empty backup $NAME" >&2; exit 2; }
done
cp ~/.hermes/config.yaml "$DEST/" 2>/dev/null || true
# 7일 초과 보관분 삭제
find ~/backups/hermes -maxdepth 1 -type d -mtime +7 -exec rm -rf {} \;
echo "backup ok: $DEST ($(ls -la $DEST | tail -n +2 | wc -l) files)"
