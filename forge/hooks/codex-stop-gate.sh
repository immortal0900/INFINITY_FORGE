#!/bin/bash
# INFINITY_FORGE — Codex Stop 훅 게이트 v0.2 (plan.md 17절 제작물 2 / D16·D17)
# 역할: codex exec가 "끝"이라고 할 때 검문. 통과 못 하면 exit 2 (fail-closed).
#
# v0.2 변경 (2026-07-13, 실측된 fail-open 구멍 봉쇄):
#   - 핸드오프 파일 기본 필수화 (없으면 TESTS_FAILED — FORGE_REQUIRE_HANDOFF=0으로만 해제)
#   - implemented/verified_by 빈 값·타입 검증, verified_by가 implemented 전체를 덮는지 확인
#   - not_implemented의 card_id 실존을 kanban DB에서 확인 (기존: 미검사)
#   - 빈 diff 판정을 FORGE_BASE_SHA(작업 시작 SHA) 기준 커밋 diff까지 포함해 계산
#     (기존: dirty tree만 봐서 커밋한 정상 작업을 empty diff로 오판)
#   - 핸드오프·메타 파일만 바뀐 작업(handoff-only)은 구현 변경으로 세지 않음
#
# 입력:
#   $1                       WORKDIR (기본 .)
#   env HANDOFF_FILE         핸드오프 경로 (기본 WORKDIR/handoff.json)
#   env FORGE_BASE_SHA       작업 시작 시점 커밋 SHA. 없으면 WORKDIR/.forge-base-sha 파일을 읽음
#   env FORGE_REQUIRE_HANDOFF 기본 1(필수). 0이면 핸드오프 부재를 허용(부분 검사·레거시 전용)
#   env FORGE_KANBAN_DB      card_id 실존 확인용 DB (기본 ~/.hermes/kanban.db)
#   env FORGE_TEST_CMD/FORGE_PY 테스트 명령/인터프리터 오버라이드
#
# stderr 접두사 규약:
#   TESTS_FAILED: 판정 실패 (재시도 카운트 O)
#   GATE_ERROR:   검문소 자체 고장 (카운트 X, 즉시 알림 대상)
# 모든 예기치 못한 에러 경로 → exit 2 (fail-closed 원칙)
set -u
trap 'echo "GATE_ERROR: gate script crashed at line $LINENO" >&2; exit 2' ERR

WORKDIR="${1:-.}"
cd "$WORKDIR" || { echo "GATE_ERROR: workdir not found: $WORKDIR" >&2; exit 2; }

HANDOFF="${HANDOFF_FILE:-handoff.json}"
REQUIRE_HANDOFF="${FORGE_REQUIRE_HANDOFF:-1}"
BASE_SHA="${FORGE_BASE_SHA:-}"
if [ -z "$BASE_SHA" ] && [ -f .forge-base-sha ]; then
  BASE_SHA=$(tr -d '[:space:]' < .forge-base-sha)
fi

# ── 1. 빈 diff 차단 ─────────────────────────────────────
git rev-parse --git-dir >/dev/null 2>&1 || { echo "GATE_ERROR: not a git repository: $WORKDIR" >&2; exit 2; }
# dirty(작업트리/스테이지/미추적) + base SHA 이후 커밋된 변경을 합산.
# git 오류는 빈 목록으로 축소 → 결과적으로 차단(fail-closed).
CHANGED=$({ git diff --name-only; git diff --cached --name-only; git ls-files --others --exclude-standard; } 2>/dev/null | sort -u)
if [ -n "$BASE_SHA" ]; then
  if git rev-parse --verify -q "${BASE_SHA}^{commit}" >/dev/null 2>&1; then
    COMMITTED=$(git diff --name-only "$BASE_SHA" HEAD 2>/dev/null || true)
    CHANGED=$(printf '%s\n%s\n' "$CHANGED" "$COMMITTED" | sort -u)
  else
    echo "GATE_ERROR: FORGE_BASE_SHA is not a commit in this repo: $BASE_SHA" >&2; exit 2
  fi
fi
# 핸드오프·게이트 메타 파일은 "구현 변경"으로 인정하지 않는다 (handoff-only 종료 차단)
HANDOFF_BASE=$(basename "$HANDOFF")
REAL_CHANGES=$(printf '%s\n' "$CHANGED" | grep -v -e '^$' -e "^${HANDOFF_BASE}\$" -e '^\.forge-base-sha$' || true)
if [ -z "$REAL_CHANGES" ]; then
  echo "TESTS_FAILED: empty diff — no implementation changes (핸드오프·메타 파일 제외, base=${BASE_SHA:-none})" >&2
  exit 2
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

# ── 3. 핸드오프 게이트 (D16·D17) ──────────────────────────
# 정확한 5필드(pr_url/changed_files/implemented/not_implemented/verified_by) 계약 + 잔여 물질화 검증.
# 핸드오프 부재 = 차단이 기본. FORGE_REQUIRE_HANDOFF=0은 canary 부분 검사 등 명시적 예외만.
if [ ! -f "$HANDOFF" ]; then
  if [ "$REQUIRE_HANDOFF" = "1" ]; then
    echo "TESTS_FAILED: handoff file missing: $HANDOFF — 핸드오프 5필드 없이 종료 불가(D16)" >&2
    exit 2
  fi
else
  "$PY" - "$HANDOFF" << 'PYEOF' >&2 || exit 2
import json, os, re, sqlite3, subprocess, sys

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as f:
        h = json.load(f)
except Exception as e:  # 모델 산출물 불량 → 판정 실패(재시도 대상)
    print(f"TESTS_FAILED: handoff parse failed: {e}"); sys.exit(2)
if not isinstance(h, dict):
    print("TESTS_FAILED: handoff must be a JSON object"); sys.exit(2)

required = {"pr_url", "changed_files", "implemented", "not_implemented", "verified_by"}
for key in sorted(required):
    if key not in h:
        print(f"TESTS_FAILED: handoff missing required field: {key}"); sys.exit(2)
unexpected = sorted(set(h) - required)
if unexpected:
    print(f"TESTS_FAILED: handoff has unexpected fields: {unexpected}"); sys.exit(2)

pr_url = h["pr_url"]
if (not isinstance(pr_url, str)
        or re.fullmatch(r"https://github\.com/[^/\s]+/[^/\s]+/pull/[1-9][0-9]*", pr_url) is None):
    print("TESTS_FAILED: pr_url must be a non-empty GitHub pull request URL"); sys.exit(2)

changed = h["changed_files"]
if (not isinstance(changed, list)
        or not all(isinstance(x, str) and x.strip() for x in changed)):
    print("TESTS_FAILED: changed_files must be an array of non-empty strings"); sys.exit(2)

imp = h["implemented"]
if (not isinstance(imp, list) or not imp
        or not all(isinstance(x, str) and x.strip() for x in imp)):
    print("TESTS_FAILED: implemented must be a non-empty array of non-empty strings"); sys.exit(2)

ni = h["not_implemented"]
if not isinstance(ni, list):
    print("TESTS_FAILED: not_implemented must be a JSON array (빈 배열 허용, 문자열 금지)"); sys.exit(2)

vb = h["verified_by"]
if not isinstance(vb, dict) or not vb:
    print("TESTS_FAILED: verified_by must be a non-empty object {구현항목: 검증수단}"); sys.exit(2)
if not all(isinstance(v, str) and v.strip() for v in vb.values()):
    print("TESTS_FAILED: verified_by values must be non-empty strings"); sys.exit(2)
uncovered = [x for x in imp if x not in vb]
if uncovered:
    print(f"TESTS_FAILED: implemented items without verified_by entry: {uncovered}"); sys.exit(2)

db = os.environ.get("FORGE_KANBAN_DB") or os.path.expanduser("~/.hermes/kanban.db")
for item in ni:
    if not isinstance(item, dict):
        print(f"TESTS_FAILED: not_implemented item must be an object: {item!r}"); sys.exit(2)
    issue = str(item.get("issue_id") or "")
    card = str(item.get("card_id") or "")
    if not issue and not card:
        print(f"TESTS_FAILED: not_implemented item without issue/card ID: {item.get('title', item)}"); sys.exit(2)
    if issue:
        repo = str(item.get("repo") or "")
        m = re.fullmatch(r"#(\d+)", issue)
        if not m or not repo:
            print(f"TESTS_FAILED: issue_id must be '#N' with a repo field: {item}"); sys.exit(2)
        r = subprocess.run(["gh", "api", f"repos/{repo}/issues/{m.group(1)}"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            err = (r.stderr or r.stdout).strip()
            if "404" in err:  # 명시적 404 = 실존 실패(판정), 그 외 = 장치/네트워크(GATE_ERROR)
                print(f"TESTS_FAILED: referenced issue does not exist: {repo}{issue}"); sys.exit(2)
            print(f"GATE_ERROR: gh api failed for {repo}{issue}: {err[:160]}"); sys.exit(2)
    else:
        if not os.path.exists(db):
            print(f"GATE_ERROR: kanban DB not found for card check: {db}"); sys.exit(2)
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            row = con.execute("SELECT 1 FROM tasks WHERE id = ?", (card,)).fetchone()
            con.close()
        except Exception as e:
            print(f"GATE_ERROR: kanban DB query failed: {e}"); sys.exit(2)
        if row is None:
            print(f"TESTS_FAILED: referenced card does not exist: {card}"); sys.exit(2)
sys.exit(0)
PYEOF
fi

exit 0
