---
name: kanban-codex-delegate
description: "executor 전용: kanban 카드를 받으면 tmux로 codex exec를 스폰해 실제 구현을 위임하고, 하트비트를 유지하며, 정확한 5필드 PR 핸드오프로만 종료한다. kanban 태스크를 배정받았을 때 항상 적용."
version: 0.2.0
author: INFINITY_FORGE
platforms: [linux]
metadata:
  hermes:
    tags: [Forge, Kanban, Codex, Executor, Delegate]
    related_skills: [forge-ops, memex]
---

# kanban-codex-delegate (executor 래퍼 절차)

너는 직접 코드를 짜지 않는다. 구현은 codex exec에 위임하고, 너의 임무는 **절차 준수와 핸드오프 품질**이다.

## 절차 (순서 엄수)

1. **카드 파악**: `kanban_show`로 카드 본문·수용 기준(AC)·이전 시도(runs)·코멘트(반성문 포함)를 전부 읽는다.
   - 이전 시도가 있으면: 실패 원인과 반성문을 codex 프롬프트에 반드시 포함한다.
   - 카드 idempotency key의 단계가 `executor-rework`이면 카드 본문의 canonical JSON 영수증에서 `source_digest`, `pr_url`, `bound_head_sha`, `reflection`을 읽는다. 이 reflection은 직전 reviewer/critic이 남긴 실패 원인이므로 누락하거나 요약하지 않는다.
   - reflection이 `required check 'eval' concluded ...` 형식이면 reviewer/critic 품질 반려가 아니라 exact `bound_head_sha`에서 발생한 CI failure 재작업이다. GitHub Actions 로그와 같은 PR의 실패 HEAD를 함께 읽는다.
2. **작업 지시서 작성**: 카드 AC를 그대로 인용한 지시문을 만든다. AC를 재해석·축소하지 않는다 (본문 수정 금지 — 코멘트만 허용).
   - `executor-rework`에서는 영수증의 reflection, PR URL, bound HEAD를 codex exec 지시문에 반드시 포함한다. 같은 PR 브랜치에서 reflection의 실패를 먼저 재현하고, 수정 뒤 기존 테스트와 해당 회귀 테스트를 실행하도록 지시한다.
   - CI failure 재작업이면 먼저 로컬에서 같은 check를 재현한다. 외부 서비스의 일시 장애처럼 코드로 재현되지 않고 수정할 파일도 없으면 **의미 없는 commit**으로 게이트를 속이지 말고 로그 증거를 코멘트한 뒤 `kanban_block`한다.
   - rework 지시문에서 새 PR을 만들도록 지시하지 않는다. 영수증의 `pr_url`이 가리키는 기존 PR을 계속 사용한다.
3. **재작업 전용 worktree 고정 또는 검증된 재개** — `executor-rework`이면 공유 repo checkout에서 바로 수정하지 않는다. 먼저 PR branch와 카드 receipt HEAD를 읽고 카드별 worktree를 만들거나, 같은 카드의 기존 worktree만 검증해 재개한다. `<저장소루트>`와 `<카드ID>`는 실제 값으로 바꾼다.
   ```bash
   PR_JSON="$(gh pr view "$PR_URL" --json url,state,isDraft,headRefName,headRefOid)"
   PR_HEAD_BRANCH="$(printf '%s' "$PR_JSON" | jq -r .headRefName)"
   PR_HEAD_SHA="$(printf '%s' "$PR_JSON" | jq -r .headRefOid)"
   test "$(printf '%s' "$PR_JSON" | jq -r .state)" = OPEN
   test "$(printf '%s' "$PR_JSON" | jq -r .isDraft)" = false
   TASK_WORKTREE="$HOME/.hermes/worktrees/<카드ID>"
   git -C <저장소루트> fetch origin "$PR_HEAD_BRANCH"
   if git -C "$TASK_WORKTREE" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
     git -C <저장소루트> worktree list --porcelain | grep -Fx -- "worktree $TASK_WORKTREE"
     LOCAL_HEAD="$(git -C "$TASK_WORKTREE" rev-parse HEAD)"
     git -C "$TASK_WORKTREE" merge-base --is-ancestor "$BOUND_HEAD_SHA" "$LOCAL_HEAD"
     test "$PR_HEAD_SHA" = "$BOUND_HEAD_SHA" || test "$PR_HEAD_SHA" = "$LOCAL_HEAD"
   else
     test ! -e "$TASK_WORKTREE"
     test "$PR_HEAD_SHA" = "$BOUND_HEAD_SHA"
     (cd <저장소루트> && git worktree add --detach "$TASK_WORKTREE" "$BOUND_HEAD_SHA")
     test -z "$(git -C "$TASK_WORKTREE" status --porcelain)"
     test "$(git -C "$TASK_WORKTREE" rev-parse HEAD)" = "$BOUND_HEAD_SHA"
   fi
   export WORKSPACE="$TASK_WORKTREE"
   ```
   기존 worktree 재개는 (a) 로컬 HEAD가 receipt HEAD의 후손이고 (b) live PR HEAD가 receipt HEAD(아직 push 전) 또는 그 로컬 HEAD(이미 push 완료)일 때만 허용한다. 다른 SHA로 움직인 PR은 자동 덮어쓰지 않는다. 카드 하나에서 두 워커를 동시에 실행하지 않는다. 하나라도 실패하면 파일을 수정하지 말고 protocol violation으로 `kanban_block`한다. 최초 executor는 카드에 지정된 repo workspace를 `export WORKSPACE=<워크스페이스>`로 설정한다.
4. **base SHA 기록 + codex exec 스폰 (tmux)** — 위 checkout·HEAD 검증이 끝난 뒤에만 작업 시작 SHA를 기록한다(게이트가 "이번 작업의 커밋"을 판정하는 기준). 재작업 재개 때는 기존 기준을 덮어쓰지 않고 receipt HEAD와 같은지 검증한다:
   ```bash
   PIPELINE_STAGE="<카드 idempotency key에서 읽은 executor 또는 executor-rework>"
   if test "$PIPELINE_STAGE" = executor-rework; then
     if test -f "$WORKSPACE/.forge-base-sha"; then
       test "$(cat "$WORKSPACE/.forge-base-sha")" = "$BOUND_HEAD_SHA"
     else
       printf '%s\n' "$BOUND_HEAD_SHA" > "$WORKSPACE/.forge-base-sha"
     fi
   else
     git -C "$WORKSPACE" rev-parse HEAD > "$WORKSPACE/.forge-base-sha"
   fi
   ```
   지시문에는 다음을 반드시 포함한다: "작업 종료 전에 워크스페이스 루트에 `handoff.json`을 작성하라 — 정확히 5필드 pr_url/changed_files/implemented/not_implemented/verified_by만 사용하고, pr_url은 생성·수정한 GitHub PR URL이어야 한다."
   `executor-rework` 지시문에는 `git push origin HEAD:<PR_HEAD_BRANCH>`로 기존 PR branch에만 push하고 새 PR을 만들지 말라는 규칙도 포함한다.
   반드시 OPENAI/CODEX 계열 env를 제거하고 스폰한다(hermes가 주입한 env가 있으면 codex가 ChatGPT 로그인 대신 API키 모드로 빠져 401이 난다):
   ```bash
   tmux new-session -d -s task-<카드ID> 'cd "$WORKSPACE" && env -u OPENAI_API_KEY -u OPENAI_BASE_URL -u OPENAI_ORG_ID -u CODEX_API_KEY codex exec --skip-git-repo-check "<지시문>" > ~/.hermes/kanban/logs/<카드ID>-codex.log 2>&1'
   ```
   스폰 전 `codex login status`가 "Logged in using ChatGPT"인지 확인하고, 401이 나면 로그의 인증 관련 줄을 comment로 남겨라.
   참고: VPS의 ~/.codex/config.toml에 `sandbox_mode = "danger-full-access"`가 설정되어 있어(2026-07-10 결정) 1차 시도부터 파일 쓰기가 된다. 샌드박스 오류가 다시 보이면 bypass 재시도로 토큰을 태우지 말고 오류 줄을 comment로 보고하라.
5. **하트비트 루프**: codex 실행 중 60~120초마다 `kanban_heartbeat` 호출 + tmux 세션 생존 확인.
   - codex가 60분 넘게 무출력이면: tmux 로그 확인 후 `kanban_comment`로 상황 기록.
6. **게이트 실행 (필수 — 생략 시 완료 선언 무효)**: codex 종료 후 반드시 Stop 훅 게이트를 직접 실행한다. 게이트 rc=0 없이는 완료 단계로 진행할 수 없다:
   ```bash
   HANDOFF_FILE=handoff.json ~/forge/hooks/codex-stop-gate.sh "$WORKSPACE" 2> /tmp/gate-<카드ID>.err; echo "gate rc=$?"
   ```
   (base SHA는 3단계에서 기록한 `.forge-base-sha`를 게이트가 자동으로 읽는다.)
   - **rc=0**: 6단계로 진행.
   - **rc=2 + stderr `TESTS_FAILED:`**: 사유 전문을 codex에 재주입해 같은 tmux 세션에서 재지시(L0 자기수정). 수정 후 게이트 재실행. 반복 실패로 예산이 소진되면 `kanban_block`(사유: 재시도 소진).
   - **rc=2 + stderr `GATE_ERROR:`**: 검문소 자체 고장이다. codex에 재지시하지 말고 `kanban_comment`로 stderr를 기록한 뒤 `kanban_block`(인간 조치 필요).
7. **재작업 push 증거 확인** — `executor-rework`이면 완료 전에 다음을 모두 확인한다. `result_head_sha`에 해당하는 로컬 commit과 live PR HEAD가 다르거나, 시작 HEAD가 결과의 조상이 아니면 block한다.
   ```bash
   LOCAL_HEAD="$(git -C "$WORKSPACE" rev-parse HEAD)"
   LIVE_HEAD="$(gh pr view "$PR_URL" --json headRefOid --jq .headRefOid)"
   test "$LOCAL_HEAD" != "$BOUND_HEAD_SHA"
   test "$LOCAL_HEAD" = "$LIVE_HEAD"
   git -C "$WORKSPACE" merge-base --is-ancestor "$BOUND_HEAD_SHA" "$LOCAL_HEAD"
   ```
8. **핸드오프 제출 후 종료** — 게이트를 통과한 `handoff.json`의 내용을 그대로 kanban_complete의 summary에 **JSON 객체 하나만** 기입한다. JSON 앞뒤 산문과 schema 밖 추가 필드는 금지한다.
   ```json
   {
     "pr_url": "https://github.com/example/project/pull/1",
     "changed_files": ["src/example.py", "tests/test_example.py"],
     "implemented": ["AC1: 잘못된 입력을 명시적 오류로 반환"],
     "not_implemented": [],
     "verified_by": {"AC1: 잘못된 입력을 명시적 오류로 반환": "tests/test_example.py::test_invalid_input"}
   }
   ```
   - `pr_url`은 `https://github.com/<OWNER>/<REPO>/pull/<NUMBER>` 형식의 비어 있지 않은 문자열이다. `null`, 이슈 URL, 로컬 경로는 금지한다.
   - 필드 집합은 위 5개와 정확히 같아야 한다. `schema_version`, `reflection`, 임의 메모 필드를 추가하지 않는다.
   - **not_implemented는 빈 배열이라도 반드시 명시**한다. 타입은 반드시 JSON 배열 — 문자열("없음", "none" 등) 금지. 항목이 있으면 각각 `{"title": "...", "issue_id": "#N" 또는 "card_id": "t_..."}` 객체로.
   - not_implemented 항목이 있으면 각각에 후속 카드를 먼저 생성(`kanban_create`)하고 그 ID를 기입한다. ID 없는 잔여 항목으로는 종료 불가(D17).

## 금지

- **게이트 rc=0 없이 `kanban_complete` 호출.** 완료의 증거는 게이트 통과이지 너의 판단이 아니다. 게이트를 실행하지 않았거나 rc=2인 상태의 complete는 성급한 완료 선언이다.
- exit 0 단순 종료 (protocol_violation → 자동 block). 반드시 `kanban_complete` 또는 `kanban_block`으로 끝낸다.
- "완료했습니다" 산문 선언으로 구현 사실을 대체하는 것. 완료 = 검증 통과 + 정확한 5필드 JSON.
- `executor-rework`에서 공유 repo checkout을 직접 수정하거나, receipt HEAD 확인 전에 `.forge-base-sha`를 기록하거나, `HEAD:<PR_HEAD_BRANCH>`가 아닌 branch로 push하는 것.
- 카드가 과대하다고 판단될 때 조용히 범위를 줄이는 것. 합법 수순: 쪼개서 후속 카드 / `kanban_block`으로 인간 결정 요청 / 계속 진행 — 셋 중 하나만.
- 카드 본문(AC) 수정. 변경이 필요하면 block + 코멘트로 사유 기록.
