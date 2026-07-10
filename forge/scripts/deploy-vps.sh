#!/bin/bash
# INFINITY_FORGE — VPS 쪽 배포 스크립트 (git pull → hermes 자산 반영)
# 실행 위치: VPS의 레포 clone(~/work/INFINITY_FORGE). 크론/수동 모두 가능.
set -euo pipefail
REPO_DIR="${FORGE_REPO_DIR:-$HOME/work/INFINITY_FORGE}"
cd "$REPO_DIR"

echo "[deploy] git pull..."
git pull --rebase --autostash

echo "[deploy] skills → hermes 프로필..."
# forge-ops·memex 공용 스킬: 게이트웨이(기본) + 워커 4프로필
for S in forge-ops memex; do
  [ -d "forge/skills/$S" ] || continue
  cp -r "forge/skills/$S" ~/.hermes/skills/
  for P in issuefinder executor reviewer critic; do
    cp -r "forge/skills/$S" ~/.hermes/profiles/$P/skills/
  done
done
# executor 전용
if [ -d forge/skills/kanban-codex-delegate ]; then
  cp -r forge/skills/kanban-codex-delegate ~/.hermes/profiles/executor/skills/
fi

echo "[deploy] hooks·scripts → ~/forge..."
mkdir -p ~/forge/hooks
[ -f forge/hooks/codex-stop-gate.sh ] && install -m 755 forge/hooks/codex-stop-gate.sh ~/forge/hooks/
[ -f forge/scripts/flush-outbox.py ] && install -m 755 forge/scripts/flush-outbox.py ~/forge/
[ -f forge/scripts/nightly-backup.sh ] && install -m 755 forge/scripts/nightly-backup.sh ~/backups/

echo "[deploy] 게이트웨이 스킬 리로드..."
systemctl --user restart hermes-gateway
echo "[deploy] done: $(git rev-parse --short HEAD)"
