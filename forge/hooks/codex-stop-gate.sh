#!/bin/bash
# INFINITY_FORGE — Codex Stop 훅 게이트 v0.1 (plan.md 17절 제작물 2 / D16·D17)
# 역할: codex exec가 "끝"이라고 할 때 검문. 통과 못 하면 exit 2 (fail-closed).
# stderr 접두사 규약:
#   TESTS_FAILED: 판정 실패 (재시도 카운트 O)
#   GATE_ERROR:   검문소 자체 고장 (카운트 X, 즉시 알림 대상)
# 모든 예기치 못한 에러 경로 → exit 2 (fail-closed 원칙)
set -u
trap 'echo "GATE_ERROR: gate script crashed at line $LINENO" >&2; exit 2' ERR

WORKDIR="${1:-.}"
cd "$WORKDIR" || { echo "GATE_ERROR: workdir not found: $WORKDIR" >&2; exit 2; }

# ── 1. 빈 diff 차단 ─────────────────────────────────────
if git rev-parse --git-dir >/dev/null 2>&1; then
  if git diff --quiet && git diff --cached --quiet && [ -z "$(git ls-files --others --exclude-standard)" ]; then
    echo "TESTS_FAILED: empty diff — no changes were made" >&2
    exit 2
  fi
else
  echo "GATE_ERROR: not a git repository: $WORKDIR" >&2; exit 2
fi

# ── 2. 테스트 (레포별 명령은 워크스페이스 AGENTS.md 규약으로 오버라이드) ──
# 우선순위: FORGE_TEST_CMD env > 레포 유형 자동 감지
# 파이썬 인터프리터는 암묵 python3 금지 — pytest가 있는 인터프리터를 명시적으로 고른다
# (실측: 시스템 python3엔 pytest 없음, hermes venv에 9.1.1 존재 → 워커·게이트 결과 불일치 방지)
PY="${FORGE_PY:-}"
if [ -z "$PY" ]; then
  if [ -x "$HOME/.hermes/hermes-agent/venv/bin/python" ]; then PY="$HOME/.hermes/hermes-agent/venv/bin/python"
  else PY="python3"; fi
fi
TEST_CMD="${FORGE_TEST_CMD:-}"
if [ -z "$TEST_CMD" ]; then
  if   [ -f package.json ] && grep -q '"test"' package.json; then TEST_CMD="npm test --silent"
  elif [ -f pyproject.toml ] || [ -f pytest.ini ] || [ -d tests ]; then TEST_CMD="$PY -m pytest tests/ -q"
  elif [ -f go.mod ];                                         then TEST_CMD="go test ./..."
  else TEST_CMD=""; fi
fi
if [ -n "$TEST_CMD" ]; then
  if ! bash -c "$TEST_CMD" > /tmp/gate-test-output.log 2>&1; then
    echo "TESTS_FAILED: test command failed ($TEST_CMD) — tail:" >&2
    tail -5 /tmp/gate-test-output.log >&2
    exit 2
  fi
fi

# ── 3. 잔여 물질화 게이트 (D17) ──────────────────────────
# 핸드오프 JSON(HANDOFF_FILE env 또는 ./handoff.json)의 not_implemented 각 항목에
# 실존하는 이슈/카드 ID가 있어야 통과. ID 부재 = exit 2.
HANDOFF="${HANDOFF_FILE:-handoff.json}"
if [ -f "$HANDOFF" ]; then
  python3 - "$HANDOFF" << 'PY' >&2 || exit 2
import json, sys, subprocess
try:
    h = json.load(open(sys.argv[1]))
except Exception as e:
    print(f"GATE_ERROR: handoff.json parse failed: {e}"); sys.exit(2)
for key in ("implemented", "not_implemented", "verified_by"):
    if key not in h:
        print(f"TESTS_FAILED: handoff missing required field: {key}"); sys.exit(2)
for item in h["not_implemented"]:
    ref = item.get("issue_id") or item.get("card_id") or ""
    if not ref:
        print(f"TESTS_FAILED: not_implemented item without issue/card ID: {item.get('title', item)}"); sys.exit(2)
    if str(ref).startswith("#"):  # GitHub 이슈면 실존 확인 (결정론, gh api)
        r = subprocess.run(["gh", "api", f"repos/{item.get('repo','')}/issues/{str(ref)[1:]}"],
                           capture_output=True)
        if r.returncode != 0:
            print(f"TESTS_FAILED: referenced issue does not exist: {item.get('repo','')}{ref}"); sys.exit(2)
sys.exit(0)
PY
fi
# handoff.json이 없는 단계(Phase 1 초기)는 1·2번 게이트만 적용 — Phase 1 e2e에서 필수화

exit 0
