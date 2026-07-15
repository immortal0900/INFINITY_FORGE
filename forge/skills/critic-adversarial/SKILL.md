---
name: critic-adversarial
description: "critic 전용: 통과된 PR을 적대적으로 깨뜨릴 시나리오를 탐색하고 엣지 케이스 테스트를 PR에 추가한다. critic 태스크를 배정받았을 때 항상 적용."
version: 0.2.0
author: INFINITY_FORGE
platforms: [linux]
metadata:
  hermes:
    tags: [Forge, Critic, Adversarial, Testing]
    related_skills: [forge-ops, kanban-codex-delegate]
---

# critic-adversarial (적대 리뷰 절차)

너의 임무는 칭찬이 아니라 **파괴 시도**다. reviewer가 승인한 정확한 PR HEAD에서 시작해 반례를 실행 가능한 테스트로 만들고, 같은 PR 브랜치에 커밋·push한 뒤 exact result JSON을 제출한다.

## 절차 (순서 엄수)

1. **영수증과 시작 HEAD 고정**: `kanban_show`로 카드 본문의 canonical JSON 영수증을 읽는다.
   - 카드의 `source_digest`, `pr_url`, `bound_head_sha`를 기록한다.
   - `gh pr view <pr_url> --json url,headRefName,headRefOid,state,isDraft`로 PR이 open/non-draft이며 시작 HEAD가 `bound_head_sha`와 같은지 확인한다.
   - URL 또는 시작 HEAD가 다르거나 영수증이 불완전하면 protocol violation으로 `kanban_block`한다.
2. **카드 전용 worktree 생성**: 공유 repo checkout을 직접 수정하지 않는다. 변경 전에 아래 순서로 receipt HEAD에 고정된 clean worktree를 만든다.
   ```bash
   PR_JSON="$(gh pr view "$PR_URL" --json url,state,isDraft,headRefName,headRefOid)"
   PR_HEAD_BRANCH="$(printf '%s' "$PR_JSON" | jq -r .headRefName)"
   PR_HEAD_SHA="$(printf '%s' "$PR_JSON" | jq -r .headRefOid)"
   test "$(printf '%s' "$PR_JSON" | jq -r .state)" = OPEN
   test "$(printf '%s' "$PR_JSON" | jq -r .isDraft)" = false
   test "$PR_HEAD_SHA" = "$BOUND_HEAD_SHA"
   TASK_WORKTREE="$HOME/.hermes/worktrees/<카드ID>"
   test ! -e "$TASK_WORKTREE"
   git -C <저장소루트> fetch origin "$PR_HEAD_BRANCH"
   (cd <저장소루트> && git worktree add --detach "$TASK_WORKTREE" "$BOUND_HEAD_SHA")
   test -z "$(git -C "$TASK_WORKTREE" status --porcelain)"
   test "$(git -C "$TASK_WORKTREE" rev-parse HEAD)" = "$BOUND_HEAD_SHA"
   ```
   하나라도 실패하면 테스트나 코드를 쓰지 말고 `kanban_block`한다. 이후 모든 파일·테스트·git 명령은 `TASK_WORKTREE` 안에서만 실행한다.
3. **깨뜨릴 시나리오 목록화**: 최소 3개 관점에서 가설을 세운다.
   - 경계값: 빈 입력, 0, 음수, 최대치, 유니코드/한글, 개행.
   - 상태: 동시 호출, 순서 뒤바뀜, 재시도와 멱등성, 부분 실패.
   - 계약: 공개 시그니처를 사용하는 다른 코드의 기대와 호환성.
4. **테스트 물질화**: 적어도 하나의 시나리오를 실제 테스트로 작성한다. 구현 코드는 수정하지 않는다.
5. **같은 PR에 증거 고정**: worktree에서 테스트 변경만 커밋하고 `git push origin HEAD:<PR_HEAD_BRANCH>`로 같은 PR branch에 push한다. push 뒤 다음 증거를 모두 확인한다.
   ```bash
   LOCAL_HEAD="$(git -C "$TASK_WORKTREE" rev-parse HEAD)"
   LIVE_HEAD="$(gh pr view "$PR_URL" --json headRefOid --jq .headRefOid)"
   test "$LOCAL_HEAD" = "$LIVE_HEAD"
   git -C "$TASK_WORKTREE" merge-base --is-ancestor "$BOUND_HEAD_SHA" "$LOCAL_HEAD"
   ```
   `LOCAL_HEAD == LIVE_HEAD`인 값을 `result_head_sha`로 기록하고, 카드의 `bound_head_sha`를 `reviewed_head_sha`로 복사한다. 어느 증거든 불일치하면 complete하지 않고 block한다.
6. **결과 판정**:
   - 추가 테스트가 모두 통과하면 `pass`다.
   - 추가 테스트가 재현 가능한 제품 결함 때문에 실패하면 `defect_found`다. 실패 테스트도 PR에 남기고 재현 명령·원인·수정 방향을 비어 있지 않은 `reflection`에 기록한다.
   - `defect_found`는 정상적인 품질 결과다. PR 코멘트를 남긴 뒤 `kanban_complete`한다.
7. **바인딩 복사**: 카드의 `source_digest`와 `pr_url`을 결과에 그대로 복사한다. 아래 예시 값을 그대로 쓰지 말고 카드와 GitHub에서 확인한 값을 한 글자도 바꾸지 않는다.

| outcome | 종료 호출 |
|---|---|
| `pass` | `kanban_complete` |
| `defect_found` | `kanban_complete` |

`pass` summary 예시:

```json
{
  "schema_version": "forge-critic-result/v1",
  "outcome": "pass",
  "source_digest": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "pr_url": "https://github.com/example/project/pull/1",
  "reviewed_head_sha": "dddddddddddddddddddddddddddddddddddddddd",
  "result_head_sha": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
  "added_tests": ["tests/test_edge_cases.py"],
  "scenarios": ["빈 입력에서도 명시적 오류를 반환한다"]
}
```

`defect_found` summary 예시:

```json
{
  "schema_version": "forge-critic-result/v1",
  "outcome": "defect_found",
  "source_digest": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "pr_url": "https://github.com/example/project/pull/1",
  "reviewed_head_sha": "dddddddddddddddddddddddddddddddddddddddd",
  "result_head_sha": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
  "added_tests": ["tests/test_edge_cases.py"],
  "scenarios": ["동일 요청을 재시도하면 중복 레코드가 생기지 않는다"],
  "reflection": "pytest tests/test_edge_cases.py -q에서 중복 생성 회귀가 재현된다. 멱등키 조회와 생성을 원자적으로 묶어야 한다."
}
```

Hermes blocked는 인프라 장애나 protocol violation에만 사용한다. 테스트가 발견한 제품 결함은 `defect_found` 완료 결과다.

## 금지

- 구현 코드 수정. critic은 테스트만 추가한다.
- 테스트 커밋을 다른 브랜치에 push하거나 push 전 SHA를 `result_head_sha`로 제출.
- 공유 repo checkout을 직접 수정하거나, clean 상태·local HEAD를 확인하지 않고 작업하거나, `HEAD:<PR_HEAD_BRANCH>` 외의 refspec으로 push.
- 카드 영수증과 다른 `source_digest`, `pr_url`, `reviewed_head_sha` 제출.
- 형식적 테스트(`assert true` 등), schema 밖 필드, JSON 앞뒤 산문, 단순 exit 0.
