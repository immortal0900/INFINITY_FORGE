#!/bin/bash
# INFINITY_FORGE — VPS 쪽 배포 스크립트 (git pull → hermes 자산 반영)
# 실행 위치: VPS의 레포 clone(~/work/INFINITY_FORGE). 크론/수동 모두 가능.
set -euo pipefail
# main 함수로 전체를 감싼다: git pull이 이 파일 자신을 덮어써도,
# bash가 함수 정의를 먼저 통째로 파싱하므로 실행 중인 버전은 안 바뀐다(자기갱신 안전).
main() {
REPO_DIR="${FORGE_REPO_DIR:-$HOME/work/INFINITY_FORGE}"
cd "$REPO_DIR"

# pull이 이 스크립트 자신을 갱신할 수 있으므로, pull 후 새 버전으로 재실행(exec).
# --post-pull 플래그가 있으면 이미 새 버전이므로 배포 단계로 직행.
if [ "${1:-}" != "--post-pull" ]; then
  echo "[deploy] git pull..."
  git pull --rebase --autostash
  exec bash "$REPO_DIR/forge/scripts/deploy-vps.sh" --post-pull
fi

echo "[deploy] skills → hermes 프로필..."
# 공용 스킬: 게이트웨이(기본) + 워커 4프로필
for S in forge-ops memex code-design-principles forge-labels; do
  [ -d "forge/skills/$S" ] || continue
  cp -r "forge/skills/$S" ~/.hermes/skills/
  for P in issuefinder executor reviewer critic; do
    cp -r "forge/skills/$S" ~/.hermes/profiles/$P/skills/
  done
done
# 게이트웨이 전용 (사용자 대화 스타일·문서화)
for S in easy-answer code-problem-doc; do
  [ -d "forge/skills/$S" ] && cp -r "forge/skills/$S" ~/.hermes/skills/
done
# reviewer 추가 (반려 리포트 문서화)
[ -d forge/skills/code-problem-doc ] && cp -r forge/skills/code-problem-doc ~/.hermes/profiles/reviewer/skills/
# 역할 전용 스킬
[ -d forge/skills/kanban-codex-delegate ] && cp -r forge/skills/kanban-codex-delegate ~/.hermes/profiles/executor/skills/
[ -d forge/skills/reviewer-verdict ]     && cp -r forge/skills/reviewer-verdict     ~/.hermes/profiles/reviewer/skills/
[ -d forge/skills/critic-adversarial ]   && cp -r forge/skills/critic-adversarial   ~/.hermes/profiles/critic/skills/
[ -d forge/skills/issue-finder-sot ]     && cp -r forge/skills/issue-finder-sot     ~/.hermes/profiles/issuefinder/skills/

echo "[deploy] hooks·scripts → ~/forge..."
mkdir -p ~/forge/hooks
[ -f forge/hooks/codex-stop-gate.sh ] && install -m 755 forge/hooks/codex-stop-gate.sh ~/forge/hooks/
[ -f forge/scripts/flush-outbox.py ] && install -m 755 forge/scripts/flush-outbox.py ~/forge/
[ -f forge/scripts/nightly-backup.sh ] && install -m 755 forge/scripts/nightly-backup.sh ~/backups/

echo "[deploy] 게이트웨이 스킬 리로드..."
systemctl --user restart hermes-gateway
echo "[deploy] done: $(git rev-parse --short HEAD)"
}
main "$@"
