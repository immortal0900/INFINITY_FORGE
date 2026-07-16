# Hermes 대화·작업 흐름 및 자동 병합 설계

## 목적

Hermes의 일반 대화와 실제 구현 작업을 처음부터 분리한다. 새 대화의 첫 사용자 입력 전에 `mode`를 선택하고, `task`를 선택한 경우에만 GitHub 이슈와 Hermes Kanban 작업 흐름을 만든다. 각 Task는 `task_flow`와 `merge_mode`를 매번 새로 선택하며 이전 Task의 선택을 재사용하지 않는다.

자동 병합은 선택한 작업 흐름, 현재 PR commit의 CI, Task별 설정 기록을 다시 확인한 뒤에만 실행한다. `full_auto`는 검사를 생략한다는 뜻이 아니다. 사용자가 선택한 `task_flow`까지만 수행하고 그 결과가 현재 commit과 정확히 연결됐을 때 병합을 자동으로 실행한다는 뜻이다.

이 설계는 다음 결과를 고정한다.

- `chat`은 일반 대화만 하며 GitHub 이슈와 Kanban 카드를 만들지 않는다.
- `task`는 `task_flow`와 `merge_mode`를 매번 모두 선택한다.
- `task_flow × merge_mode`의 9개 조합을 전부 허용한다.
- `safe_auto`는 코드 모델이 아닌 고정 규칙으로 저위험 변경만 자동 병합한다.
- `full_auto`도 Task 설정, 작업 흐름 결과, 현재 commit, CI, GitHub 보호 규칙을 모두 확인한다.
- 오류, 누락, 모호한 상태는 성공으로 바꾸지 않는다.

## 쉬운 영문 이름 규칙

Infinity Forge가 새로 만드는 공개 이름에는 다음 값만 사용한다.

```yaml
mode: chat | task
task_flow: build | build_review | build_review_deep_check
role: builder | reviewer | deep_checker | fix
merge_mode: manual | safe_auto | full_auto
```

화면 표시는 다음과 같다.

| 설정값 | 화면 표시 | 실제 동작 |
|---|---|---|
| `chat` | Chat | 일반 질문과 설계 대화만 수행 |
| `task` | Task | 구현 Task 생성 절차 시작 |
| `build` | Build | builder만 실행 |
| `build_review` | Build + Review | builder 다음 reviewer 실행 |
| `build_review_deep_check` | Build + Review + Deep Check | builder, reviewer, `deep_checker` 순서로 실행 |
| `manual` | Manual merge | 사람이 최종 병합 |
| `safe_auto` | Safe files auto-merge | 고정 저위험 규칙을 통과한 PR만 자동 병합 |
| `full_auto` | All validated PRs auto-merge | 선택한 작업 흐름과 공통 안전 검사를 통과한 PR 자동 병합 |

역할 ID는 다음 네 개로 고정한다.

| 역할 ID | 책임 |
|---|---|
| `builder` | 요구사항 구현과 PR 생성 또는 수정 |
| `reviewer` | 구현 내용과 Acceptance Criteria 대조 |
| `deep_checker` | 예외 상황 테스트 추가와 최종 품질 확인 |
| `fix` | review 또는 Deep Check가 찾은 문제 수정 |

새 구현의 Forge 소유 이름에는 `assurance`, `adversarial`, `orchestrator`, `reconciler`, `projection`, `frontier`, `ledger`, `receipt`, `digest`, `handoff`, `canary`, `drift`, `outbox`, `gate`를 사용하지 않는다. 각각 `task_flow`, `deep_check`, `task_flow_worker`, `worker`, `displayed_status`, `current_step`, `activity_log`, `proof_record`, 대상이 드러나는 `*_hash`, `work_result`, `system_check`, `state_mismatch`, `pending_messages`, `check`를 사용한다.

GitHub, Hermes, Slack, PR, CI, commit, branch, merge, SHA-256, JSON, SQLite와 같이 외부 제품 또는 표준이 정한 이름은 유지한다. GitHub API의 `head_sha`, Hermes의 `idempotency_key`, MCP의 `jsonrpc` 같은 원본 필드는 Forge가 바꾸지 않는다. 사용자 화면에서는 각각 `current commit`, `duplicate-prevention key`, `MCP message format`처럼 바로 이해되는 설명을 함께 표시한다.

## 전체 구조

```text
Hermes host
  └─ generic pre_user_turn hook
       └─ Infinity Forge plugin
            ├─ 시작 선택과 Task 초안 상태
            ├─ Task 미리보기와 사용자 확인
            └─ Forge task service 호출

Forge task service
  ├─ Task 설정 저장소
  ├─ GitHub 이슈 생성
  └─ forge:ready-to-build 라벨 부여

Forge workers
  ├─ issue status sync
  ├─ task flow worker
  ├─ safe files check
  └─ merge worker
```

Hermes host에는 Forge 전용 정책을 넣지 않는다. host는 LLM으로 사용자 입력을 보내기 전에 plugin을 호출할 수 있는 범용 `pre_user_turn` hook만 제공한다. Forge plugin이 `chat`, `task`, GitHub, Kanban, 자동 병합을 해석한다.

`pre_user_turn` hook 결과는 다음 세 가지다.

| 결과 | host 동작 |
|---|---|
| `continue` | 원래 사용자 입력을 LLM으로 전달 |
| `replace` | plugin이 반환한 입력으로 교체한 뒤 LLM으로 전달 |
| `handled` | plugin이 이미 응답했으므로 이 입력에서는 LLM을 호출하지 않음 |

hook은 선택 항목의 ID, 화면 표시문, session ID, user ID, 입력 text만 다루는 범용 계약이다. Forge 전용 enum이나 GitHub 코드를 Hermes core에 넣지 않는다. hook은 system prompt, tool 목록, 모델 설정을 대화 중간에 바꾸지 않는다. 따라서 Hermes prompt cache와 일반 대화 동작은 기존과 같은 byte 단위 입력을 유지한다.

생산 환경의 Task 설정 저장소와 merge worker는 같은 Forge host에서 실행한다. Slack의 Forge plugin은 같은 host의 task service를 직접 호출한다. Desktop, TUI, CLI의 plugin은 인증된 Forge task service endpoint로 확인된 구조화 요청만 전달한다. 클라이언트가 GitHub 이슈 본문을 직접 작성해 자동 병합 권한을 만들 수는 없다.

## 대화 시작 흐름

새 대화의 첫 사용자 입력은 바로 LLM으로 보내지 않고 plugin이 잠시 보관한다.

```text
첫 사용자 입력
  → mode 선택
     ├─ chat
     │   → 보관한 입력을 그대로 LLM에 전달
     │   → 일반 Hermes 대화
     │   → GitHub/Kanban write 0회
     │
     └─ task
         → task_flow 선택
         → merge_mode 선택
         → Task 내용 입력 또는 첫 입력을 Task 설명으로 사용
         → 미리보기
         → 사용자 확인
         → GitHub 이슈와 Task 설정 생성
         → chat 상태로 복귀
```

Slack은 대화창을 열었다는 별도 이벤트가 없으므로 첫 메시지 또는 `/new` 다음 첫 메시지에서 선택을 표시한다. Desktop, TUI, CLI도 첫 입력 시점에 같은 선택을 표시한다. 모든 surface에서 선택 ID와 상태 전이는 동일하고, 표현만 버튼 또는 번호 목록으로 달라진다.

동작 규칙은 다음과 같다.

- `chat`을 고르면 보관한 첫 입력을 잃지 않고 일반 대화로 전달한다.
- `task`를 고르면 `task_flow`와 `merge_mode`를 생략할 수 없다.
- `/task`는 현재 대화에서 새 Task 절차를 시작하지만 이전 선택을 불러오지 않는다.
- `/cancel`은 외부 write 없이 초안을 버리고 `chat`으로 돌아간다.
- Task 초안이 30분 동안 입력 없이 남으면 자동 폐기한다.
- Task 생성이 끝나면 현재 대화는 `chat`으로 돌아간다.
- 기본 `full_auto`, 이전 선택 기억, “다음에도 같은 설정” 기능은 제공하지 않는다.

mode 선택이나 Forge plugin 자체에 오류가 나면 Task는 시작하지 않는다. 사용자는 `Retry` 또는 명시적인 `Continue in Chat`만 선택할 수 있다. 오류를 이유로 Task 설정을 자동 추정하지 않는다.

## Task 미리보기와 생성

Task 설명에서 제목과 Acceptance Criteria를 구조화한 미리보기를 만든다. 구조화 helper가 모델을 사용하는 경우에도 이 단계에서는 GitHub 이슈나 Kanban 카드를 만들지 않는다. 모델 결과가 형식 검사에 실패하면 사용자에게 수정을 요청하고 임의의 기본값으로 진행하지 않는다.

미리보기에는 다음 항목을 한 화면에 표시한다.

```text
Repository: OWNER/REPO
Title: Hermes 시작 모드 및 자동 병합 구현

Acceptance Criteria:
1. 새 대화에서 mode를 선택한다.
2. Task는 task_flow와 merge_mode를 매번 선택한다.
3. 9개 조합을 모두 지원한다.

Task flow: build_review
실행 경로: Build → Review → current commit CI

Merge mode: full_auto
결과: 위 경로와 공통 안전 검사를 통과하면 자동 병합

Auto-merge permission until: 2026-07-17 07:30 KST
```

현재 workspace가 정확히 하나의 GitHub repository를 가리키면 그 repository를 사용한다. repository가 없거나 두 개 이상이면 사용자에게 대상 `OWNER/REPO`를 명시적으로 선택하게 한다. repository는 Task 확인 뒤 바꿀 수 없다.

사용자가 미리보기를 수정하면 제목, 설명, Acceptance Criteria로 `task_content_hash`를 다시 계산하고 확인을 다시 받는다. 확인 전에는 외부 write가 없다.

확인 뒤 생성 순서는 다음과 같다.

1. `request_id`로 Task 준비 기록을 만든다.
2. GitHub 이슈를 생성한다.
3. 생성된 issue number와 이슈 내용 hash를 Task 설정에 연결한다.
4. 이슈 본문에 사람이 읽을 수 있는 Task 설정 블록을 기록한다.
5. Task 설정을 `active`로 바꾼다.
6. 마지막에만 `forge:ready-to-build` 라벨을 붙인다.
7. issue status sync가 활성 Task 설정과 이슈 내용 hash를 확인한 뒤 루트 카드를 만든다.

GitHub와 SQLite는 하나의 transaction으로 묶을 수 없다. `request_id`는 모든 재시도에 동일하게 사용한다. 이슈 생성 뒤 프로세스가 중단되면 다음 실행은 `request_id`가 들어간 기존 이슈를 찾아 3단계부터 이어서 수행한다. Task 설정이 `active`가 되기 전에는 `forge:ready-to-build`를 붙이지 않으므로 불완전한 Task가 실행 흐름에 들어가지 않는다.

GitHub 이슈의 제목, 설명, Acceptance Criteria가 확인 뒤 바뀌면 `task_content_hash`가 달라진다. 작업은 계속 조사할 수 있지만 기존 자동 병합 권한은 즉시 무효가 된다. 변경된 내용으로 자동화를 계속하려면 같은 이슈를 다시 불러와 `task_flow`와 `merge_mode`를 모두 새로 선택하고 미리보기를 확인한다. 이때 새 `request_id`의 Task 설정을 append하며 기존 설정은 수정하지 않는다.

## Task 설정 저장소

Task 설정 저장소는 SQLite를 사용하며 Forge task service와 merge worker만 write할 수 있다. GitHub 이슈의 설정 블록은 표시용이고 자동 병합 권한의 원본은 Task 설정 저장소다.

활성 Task 설정은 다음 필드를 갖는다.

```json
{
  "format_version": "forge-task-settings/v1",
  "request_id": "UUID",
  "repository": "OWNER/REPO",
  "issue_number": 12,
  "mode": "task",
  "task_content_hash": "64자리 SHA-256",
  "task_flow": "build_review",
  "merge_mode": "full_auto",
  "confirmed_by": "Hermes user ID",
  "confirmed_at": "RFC 3339 timestamp",
  "auto_merge_expires_at": "RFC 3339 timestamp or null",
  "task_settings_hash": "64자리 SHA-256",
  "status": "active"
}
```

Task 설정의 공개 필드는 위 13개로 고정한다. `status`의 허용 값은 `prepared|active|cancelled|expired|merged`이며, 위 JSON은 이 중 활성 상태를 보여 준다. 새 필드나 이전 형식 필드가 추가되면 data-format 오류로 중단한다.

`task_settings_hash`는 `task_settings_hash` 자신과 lifecycle `status`를 제외한 위 필드를 key 정렬한 UTF-8 JSON으로 만든 SHA-256이다. `task_content_hash`는 이슈의 제목, 설명, Acceptance Criteria만 key 정렬한 UTF-8 JSON의 SHA-256이다. 표시용 설정 블록은 두 hash 계산에서 제외한다.

설정의 repository, issue number, Task 내용, `task_flow`, `merge_mode`, 확인 사용자, 확인 시각, 자동 병합 만료 시각은 활성화 뒤 수정할 수 없다. 취소, 만료, 병합 성공 같은 lifecycle 변화는 별도 event 행으로 append한다. 설정 값을 바꾸려면 새 `request_id`로 전체 선택과 미리보기 확인을 다시 수행한다.

- `manual`은 `auto_merge_expires_at = null`이다.
- `safe_auto`와 `full_auto`는 사용자 확인 시점부터 최대 12시간 동안만 자동 병합할 수 있다.
- 12시간이 지나도 선택한 작업 흐름은 계속할 수 있다.
- 만료 뒤에는 자동 병합만 중단하고 `forge:ready-to-merge`에서 사람을 기다린다.
- 자동 연장, 영구 자동 병합, repository 전체 기본 권한은 제공하지 않는다.

모든 step proof와 worker 결과는 같은 `task_settings_hash`를 포함한다. repository, issue, PR, Task 내용, `task_settings_hash` 중 하나라도 맞지 않으면 새 카드 생성과 병합을 모두 중단한다.

## 새 카드와 결과 형식

clean break 이후 새 코드는 다음 key만 읽는다.

```text
forge-task:<OWNER/REPO>#<ISSUE>:<task_settings_hash 앞 16자리>
forge-step:<OWNER/REPO>#<ISSUE>:<build|review|deep_check|fix>:<source_result_hash 앞 16자리>
```

자식 카드 본문의 step proof는 다음 exact JSON이다.

```json
{
  "format_version": "forge-step-proof/v1",
  "tested_commit": "40자리 Git commit",
  "pr_url": "https://github.com/OWNER/REPO/pull/N",
  "fix_notes": null,
  "source_result_hash": "64자리 SHA-256",
  "source_run_id": 12,
  "source_task_id": "t_...",
  "task_settings_hash": "64자리 SHA-256"
}
```

worker 결과 형식도 새 이름만 사용한다.

### Build 결과

```json
{
  "format_version": "forge-build-result/v1",
  "task_settings_hash": "64자리 SHA-256",
  "pr_url": "https://github.com/OWNER/REPO/pull/N",
  "built_commit": "40자리 Git commit",
  "changed_files": ["src/example.py"],
  "completed_items": ["AC1"],
  "remaining_items": [],
  "checks_by_item": {"AC1": "tests/test_example.py::test_ac1"}
}
```

### Review 결과

```json
{
  "format_version": "forge-review-result/v1",
  "task_settings_hash": "64자리 SHA-256",
  "result": "approve",
  "source_result_hash": "64자리 SHA-256",
  "pr_url": "https://github.com/OWNER/REPO/pull/N",
  "reviewed_commit": "40자리 Git commit",
  "change_check": {"confirmed_work": ["AC1"], "problems": []},
  "requirements_check": {"completed": ["AC1"], "missing": []},
  "fix_notes": null
}
```

`result`는 `approve|changes_needed`다. `changes_needed`에는 비어 있지 않은 `fix_notes`가 필요하다.

### Deep Check 결과

```json
{
  "format_version": "forge-deep-check-result/v1",
  "task_settings_hash": "64자리 SHA-256",
  "result": "pass",
  "source_result_hash": "64자리 SHA-256",
  "pr_url": "https://github.com/OWNER/REPO/pull/N",
  "reviewed_commit": "40자리 Git commit",
  "tested_commit": "40자리 Git commit",
  "added_tests": ["tests/test_edge.py"],
  "tested_cases": ["빈 입력"],
  "fix_notes": null
}
```

`result`는 `pass|problems_found`다. `problems_found`에는 비어 있지 않은 `fix_notes`가 필요하다. `deep_checker`가 테스트를 추가해 commit이 바뀌면 `tested_commit`이 최종 검증 대상이다.

모든 JSON은 명시된 field만 허용한다. 누락 field, 추가 field, 잘못된 type, 알 수 없는 enum은 check error다.

## Task 작업 흐름

`task_flow`는 필요한 품질 확인 단계만 정한다. `merge_mode`는 그 단계가 끝난 뒤 병합 주체만 정한다. 둘은 서로 독립이다.

### `build`

```text
builder 결과
  + open, non-draft PR
  + built_commit = current PR commit
  + current PR commit의 required CI success
  → 작업 흐름 완료
```

### `build_review`

```text
builder 결과
  → reviewer가 built_commit 검토
  + review result approve
  + reviewed_commit = current PR commit
  + current PR commit의 required CI success
  → 작업 흐름 완료
```

### `build_review_deep_check`

```text
builder 결과
  → reviewer가 built_commit 검토
  → deep_checker가 reviewed_commit에서 추가 테스트 실행
  + deep check result pass
  + tested_commit = current PR commit
  + current PR commit의 required CI success
  → 작업 흐름 완료
```

reviewer가 `changes_needed`를 반환하거나 `deep_checker`가 `problems_found`를 반환하면 같은 PR의 새 `fix` 카드를 만든다. 수정 뒤에는 이전 review 또는 deep check 결과를 재사용하지 않고 선택한 흐름을 `build`부터 다시 실행한다. 제품 결함 수정은 Task당 최대 3회다. 네 번째 반려에서는 새 fix 카드를 만들지 않고 `forge:failed`로 끝낸다.

선택하지 않은 단계를 자동 추가하지 않는다. 특히 `full_auto`를 선택했다고 deep check를 추가하지 않는다.

## 9개 조합

| `task_flow` | `manual` | `safe_auto` | `full_auto` |
|---|---|---|---|
| `build` | Build + current commit CI 뒤 사람이 병합 | Build + current commit CI + safe files check 뒤 자동 병합, 그 밖에는 사람 병합 | Build + current commit CI + 공통 병합 검사 뒤 자동 병합 |
| `build_review` | Build + Review + current commit CI 뒤 사람이 병합 | Build + Review + current commit CI + safe files check 뒤 자동 병합, 그 밖에는 사람 병합 | Build + Review + current commit CI + 공통 병합 검사 뒤 자동 병합 |
| `build_review_deep_check` | Build + Review + Deep Check + current commit CI 뒤 사람이 병합 | Build + Review + Deep Check + current commit CI + safe files check 뒤 자동 병합, 그 밖에는 사람 병합 | Build + Review + Deep Check + current commit CI + 공통 병합 검사 뒤 자동 병합 |

모든 조합을 UI와 API에서 허용한다. `build + full_auto`처럼 위험도가 높은 조합도 막지 않지만 미리보기에 실제 실행 경로와 자동 병합 결과를 그대로 표시하고 사용자의 최종 확인을 요구한다.

## GitHub 상태 표시

새 작업은 다음 Forge label만 사용한다.

| Label | 의미 |
|---|---|
| `forge:needs-details` | 제목과 Acceptance Criteria 보완 필요 |
| `forge:needs-decision` | 사람의 설계 결정 필요 |
| `forge:ready-to-build` | builder 실행 대기 |
| `forge:building` | builder 또는 fix 실행 중 |
| `forge:reviewing` | reviewer 실행 또는 대기 |
| `forge:deep-checking` | `deep_checker` 실행 또는 대기 |
| `forge:ready-to-merge` | 선택한 흐름 완료, 사람 병합 또는 merge worker 처리 대기 |
| `forge:waiting-for-help` | 입력, 권한, 외부 시스템 문제로 정지 |
| `forge:failed` | 제품 결함 수정 횟수 소진 |

한 이슈에는 위 상태 label이 정확히 하나만 존재한다. `issue-status-sync`만 이 label을 쓴다. merge worker는 label을 권한 근거로 사용하지 않고 Task 설정 저장소와 현재 GitHub 상태를 다시 읽는다.

## 현재 commit과 CI 확인

각 worker 결과는 자신이 확인한 commit을 기록한다. 다음 단계와 병합은 GitHub에서 다시 읽은 현재 PR commit이 그 값과 같을 때만 진행한다.

- `build`: `built_commit`이 현재 PR commit이어야 한다.
- `build_review`: `reviewed_commit`이 현재 PR commit이어야 한다.
- `build_review_deep_check`: `deep_checker` 입력은 `reviewed_commit`, 최종 현재 PR commit은 `tested_commit`이어야 한다.
- 선택한 흐름이 끝난 뒤 새 push가 있으면 완료 증거를 무효로 하고 `build`부터 다시 실행한다.
- CI는 현재 PR commit에서 required check가 정확히 한 개 존재하고 `success`일 때만 통과한다.
- 이전 commit의 성공, 다른 PR의 성공, 이름이 같은 중복 check는 사용할 수 없다.
- queued 또는 in-progress CI는 기다린다.
- 누락, 중복, API의 불완전한 page, 알 수 없는 완료 결과는 check error다.

required CI는 현재 GitHub ruleset에 연결된 외부 식별자 `eval`을 유지한다. branch ruleset은 PR 필수, 최신 base branch 반영 필수, `eval` 성공 필수, force push와 branch 삭제 차단, 우회 사용자 없음으로 유지한다. 이 이름 변경은 새 check를 먼저 성공시킨 뒤 ruleset을 별도로 전환해야 하므로 이번 clean break에서 제외한다.

## `safe_auto` 고정 저위험 규칙

`safe_auto`는 LLM 또는 점수 모델을 사용하지 않는다. merge worker가 GitHub에서 현재 base commit과 현재 PR commit의 전체 변경 목록을 모든 page로 다시 읽고 고정 규칙으로 판정한다.

모든 변경 파일이 다음 허용 규칙 중 하나에 들어가야 한다.

- `docs/**`의 text 문서 추가 또는 수정
- repository root의 `README*` 추가 또는 수정
- repository root의 `CHANGELOG*` 추가 또는 수정
- `tests/**` 또는 `test/**` 아래의 새 text 테스트 파일 추가

다음 차단 규칙이 허용 규칙보다 우선한다.

- 파일 삭제, rename, copy
- binary, symlink, submodule
- 기존 테스트 파일 수정 또는 삭제
- production source code
- dependency file과 lockfile
- migration과 DB schema
- `.github/**`, CI/CD, Docker, systemd, 배포와 infrastructure 파일
- 인증, 권한, secret 관련 파일
- `AGENTS.md`, `**/AGENTS.md`, `**/SKILL.md`, `.codex/**`, `.claude/**`, `.weapon/**`
- `forge/**`
- `docs/weapon/**`, `docs/setup/**`, `docs/plan.md`, `docs/user-runbook.md`, `docs/automation-architecture.md`
- 자동 병합 규칙과 Task 설정 규칙 자체
- 규칙에 명시되지 않은 모든 경로

GitHub PR files API의 전체 page와 base/head tree mode를 함께 확인한다. page 누락, API 오류, patch 누락, tree entry 누락처럼 변경 형식을 확정할 수 없는 경우 `CHECK_ERROR`로 병합을 차단한다. 정상 diff지만 허용 규칙 밖이면 `MANUAL_REQUIRED`로 `forge:ready-to-merge`에 남긴다. 모든 규칙을 통과한 경우에만 `AUTO_MERGE_SAFE`다.

주석·format 변경과 내부 refactor는 첫 버전의 자동 병합 대상이 아니다. 파일 경로와 diff만으로 public API 불변을 증명할 수 없기 때문이다.

## `full_auto`와 공통 병합 검사

`safe_auto`와 `full_auto`는 병합 직전에 다음 조건을 모두 다시 확인한다.

1. Task 설정이 `active`이며 repository와 issue가 현재 대상과 같다.
2. `task_settings_hash`와 `task_content_hash`가 저장소, 이슈, 모든 step proof에서 같다.
3. 자동 병합 만료 시각이 지나지 않았다.
4. 선택한 `task_flow`가 완료됐다.
5. 흐름의 최종 commit이 현재 PR commit과 같다.
6. PR이 open이고 draft가 아니다.
7. 현재 PR commit의 `eval`이 정확히 한 개이며 success다.
8. PR이 conflict가 없고 GitHub ruleset 기준으로 병합 가능하다.
9. 해결되지 않은 review thread가 없다.
10. Task가 `waiting-for-help`, `failed`, `needs-decision`, check error 상태가 아니다.
11. `safe_auto`이면 결과가 `AUTO_MERGE_SAFE`다.
12. 병합 요청 순간의 PR commit이 마지막으로 확인한 commit과 같다.

병합 명령은 `gh pr merge --merge --match-head-commit <CURRENT_COMMIT>`과 같은 expected-commit 조건을 사용한다. `--admin`, ruleset 우회, 확인되지 않은 지연형 auto-merge는 사용하지 않는다.

병합 요청 결과가 모호하면 즉시 다시 요청하지 않는다. 먼저 PR을 읽어 이미 병합됐는지, open인지, current commit이 무엇인지 확인한다. 이미 같은 PR이 정상 병합된 상태면 성공으로 기록한다. 다른 commit으로 바뀌었거나 상태를 확인할 수 없으면 병합을 차단한다.

## Base branch 갱신과 재시도

`safe_auto`와 `full_auto` 병합 직전에 PR branch가 base branch보다 뒤에 있으면 다음 순서로 처리한다.

1. conflict가 없는지 확인한다.
2. GitHub의 update-branch 기능으로 base branch를 반영한다.
3. 새 current commit을 읽는다.
4. 기존 작업 흐름 결과를 모두 무효로 한다.
5. 선택한 `task_flow`를 `build`부터 다시 실행한다.
6. 새 current commit의 CI와 병합 검사를 다시 수행한다.

자동 branch 갱신은 Task당 최대 3회다. conflict가 있거나 세 번의 갱신 뒤에도 base branch가 다시 앞서면 자동 병합을 중단하고 `forge:ready-to-merge`에서 사람을 기다린다. 자동 병합 만료 시각이 재검증 도중 지나도 같은 방식으로 사람 병합으로 전환한다.

재시도 규칙은 원인별로 분리한다.

- 제품 결함: fix 최대 3회, 초과하면 `forge:failed`.
- branch 갱신: 최대 3회, 초과하면 사람 병합.
- GitHub read 일시 오류: 2초, 10초, 30초 간격으로 최대 3회 읽은 뒤 `forge:waiting-for-help`.
- GitHub write: 같은 `request_id` 또는 expected commit으로 read-back 확인 뒤에만 재시도.
- conflict, 설정 불일치, Task 내용 변경, 알 수 없는 상태: 자동 재시도하지 않고 명시적으로 정지.

`manual`은 branch를 자동 갱신하지 않는다. 사람이 branch를 갱신하면 commit이 바뀌므로 선택한 작업 흐름을 다시 완료한 뒤에만 `forge:ready-to-merge`로 돌아온다.

## Clean break 전환

새 구현은 구 형식 alias를 제공하지 않는다. 다음 구 이름과 데이터 형식은 새 parser에서 명시적 오류다.

- `interaction_mode`, `assurance_policy`, `merge_policy`
- `direct`, `reviewed`, `adversarial`, `P1`, `P2`, `P3`
- `github-issue:*`, `forge-stage:*`
- `executor`, `reviewer`, `critic`, `executor-rework`를 step enum으로 사용한 새 카드
- `handoff.json`과 구 reviewer/critic 결과 schema
- 구 `forge:*` 상태 label

live required CI 식별자 `eval`은 Forge가 소유한 이전 형식이 아니므로 거절 목록에 포함하지 않는다.

전환은 다음 순서로 한 번 수행한다.

1. 구 stage와 label timer를 중지한다.
2. 구 형식을 사용하는 실행 중 또는 실행 대기 Task가 0개인지 확인한다.
3. 남아 있는 구 Task는 기존 release에서 완료하거나 사람이 중단 처리한다.
4. Hermes DB, Task 설정 DB, Forge 상태 파일을 backup한다.
5. GitHub 상태 label을 새 이름으로 전환하고, 기존 `eval` required CI와 ruleset이 그대로인지 read-back한다.
6. 새 release를 설치한다.
7. 새 코드는 `forge-task:*`, `forge-step:*`, 새 JSON schema만 읽는다.
8. 새 system check Task로 `chat`, `manual`, `safe_auto`, `full_auto` 경계를 확인한다.
9. 새 timer와 merge worker를 활성화한다.

구 historical row와 log는 감사 기록으로 보존하지만 새 worker query 대상에 포함하지 않는다. 구 parser와 새 parser를 한 process에 함께 두지 않는다. rollback이 필요하면 release 전체를 되돌리며 구·신 형식을 섞어 실행하지 않는다.

## 파일과 component 책임

새 이름 기준의 책임은 다음과 같다.

| 위치 | 책임 |
|---|---|
| Hermes `pre_user_turn` host hook | plugin 호출과 `continue|replace|handled` 처리 |
| Infinity Forge Hermes plugin | mode 선택, Task 초안, 미리보기, 사용자 확인 |
| `forge/task_settings.py` | Task 설정 형식, hash, 만료 판단 |
| `forge/task_service.py` | 준비 기록, GitHub 이슈 생성, 활성화 순서 |
| `forge/task_flow.py` | 선택한 flow의 다음 step 결정 |
| `forge/issue_status.py` | current step을 단일 GitHub label로 표시 |
| `forge/safe_files.py` | `safe_auto` 고정 파일 규칙 |
| `forge/merge_worker.py` | 공통 병합 검사, branch 갱신, expected-commit 병합 |
| `forge/github.py` | PR, commit, CI, diff, merge 상태의 read/write adapter |
| `forge/hermes.py` | Hermes Task read와 새 카드 create adapter |
| `forge/schemas/*` | Task 설정, step proof, Build/Review/Deep Check 결과 JSON Schema |

운영 entrypoint의 새 파일명은 `task-service`, `task-flow-worker`, `issue-status-sync`, `safe-files-check`, `merge-worker`, `system-check`, `state-mismatch-check`, `activity-log-writer`, `send-pending-messages`를 사용한다. 이미 배포된 `forge-*.service`와 `forge-*.timer` unit 식별자는 중복 실행을 막기 위해 유지하고 `Description`과 `ExecStart` 대상만 새 파일명으로 바꾼다.

## 테스트 기준

구현은 test-first로 진행하며 다음을 자동 검증한다.

### 시작 선택

1. Slack, Desktop, TUI, CLI가 같은 선택 ID와 상태 전이를 사용한다.
2. 첫 입력이 mode 선택 중 사라지거나 두 번 전달되지 않는다.
3. `chat`에서 GitHub, Kanban, Task 설정 write가 0회다.
4. `task`에서 `task_flow`와 `merge_mode`를 생략할 수 없다.
5. `/task`, `/cancel`, `/new`, 30분 초안 만료가 같은 규칙으로 작동한다.
6. hook이 system prompt, tool 목록, 모델 설정을 대화 중간에 바꾸지 않는다.

### Task 생성과 설정

1. 미리보기 확인 전 외부 write가 0회다.
2. 같은 `request_id` 재실행이 이슈를 중복 생성하지 않는다.
3. 중간 실패에서는 `forge:ready-to-build`가 붙지 않는다.
4. Task 내용 수정이 `task_content_hash` 불일치로 자동 병합을 막는다.
5. 12시간 직전, 정확한 만료 시각, 직후를 각각 검증한다.
6. settings field 변경과 추가 field를 거절한다.

### 작업 흐름

1. `task_flow × merge_mode` 9개 조합을 각각 실행한다.
2. `build`가 review 또는 deep check를 만들지 않는다.
3. `build_review`가 deep check를 만들지 않는다.
4. `build_review_deep_check`가 세 단계를 순서대로 실행한다.
5. fix 뒤 이전 review와 deep check 결과를 재사용하지 않는다.
6. fix 네 번째 요청에서 `forge:failed`가 된다.
7. 같은 step proof를 반복 처리해도 카드가 하나만 생긴다.

### commit, CI, 자동 병합

1. 이전 commit의 worker 결과와 CI success를 거절한다.
2. required CI 누락, 중복, incomplete page, 다른 commit 결과를 거절한다.
3. 병합 직전 commit 변경 경쟁에서 `--match-head-commit`이 병합을 막는다.
4. 이미 병합된 PR 재처리는 성공 한 번으로 끝난다.
5. unresolved review, draft, conflict, blocked, failed, decision-needed 상태를 거절한다.
6. branch 갱신 뒤 선택한 흐름을 `build`부터 다시 실행한다.
7. branch 갱신 3회 초과와 권한 만료는 사람 병합으로 전환한다.

### `safe_auto`

1. 허용 문서, root README/CHANGELOG, 새 테스트 파일 조합을 통과시킨다.
2. production code, 기존 테스트 수정, 삭제, rename, binary, symlink, submodule을 사람 병합으로 보낸다.
3. Forge 정책, Agent 지침, CI, dependency, migration, 보안 파일을 사람 병합으로 보낸다.
4. pagination과 tree 조회 오류를 `CHECK_ERROR`로 차단한다.
5. 같은 diff는 실행 횟수와 관계없이 같은 결과를 낸다.

### Clean break와 restore

1. 구 enum, key prefix, schema, label, CI 이름을 새 parser가 거절한다.
2. active 구 Task가 있으면 배포를 중단한다.
3. 새 release 실패 시 이전 release와 DB backup을 복원한다.
4. restore 뒤 이전 Hermes 일반 대화가 정상 동작한다.
5. restore 과정이 구·신 worker를 동시에 실행하지 않는다.

## 배포와 restore

Hermes 설치본을 직접 수동 편집하지 않는다. 지원하는 Hermes commit의 별도 checkout에서 범용 hook patch를 만들고, 검증된 release package로 설치한다.

설치 package에는 다음을 포함한다.

- 변경 전 대상 파일과 `before_file_hash`
- 변경 뒤 대상 파일과 `after_file_hash`
- 원본 파일을 담은 `restore_package`
- 지원 Hermes commit
- 변경 대상의 정확한 상대 경로 목록

설치 전 현재 파일 hash가 `before_file_hash`와 다르면 중단한다. 설치 뒤 `after_file_hash`와 다르면 서비스를 시작하지 않고 원본 파일을 복원한다.

기능은 같은 release에 넣되 다음 순서로 활성화한다.

1. `interaction_choice_enabled=false`, `task_creation_enabled=false`, `auto_merge_enabled=false`로 설치한다.
2. 전체 unit, schema, DB, hook system check를 실행한다.
3. 시작 선택과 `chat`을 활성화한다.
4. Task 생성과 `manual` 흐름을 활성화한다.
5. `safe_auto`를 merge 없는 `preview_only`로 실행해 판정 결과를 확인한다.
6. 문서 전용 test PR에서 `safe_auto` 실제 병합을 확인한다.
7. 전용 test PR에서 `full_auto` 실제 병합을 확인한다.
8. production `auto_merge_enabled=true`를 적용한다.

즉시 중단 설정은 다음과 같다.

- `auto_merge_enabled=false`: 자동 병합만 정지하고 사람 병합은 유지
- `task_creation_enabled=false`: 새 Task 생성을 정지하고 기존 Task와 chat은 유지
- `interaction_choice_enabled=false`: 시작 선택을 끄고 기존 Hermes 대화로 복귀

restore는 새 Task 접수와 merge worker를 먼저 멈추고 process lock을 획득한 뒤 수행한다. Task 설정 DB와 Hermes DB를 backup하고, `restore_package`의 원본 파일 hash를 확인해 대상 파일만 복원한다. 이전 service 정의를 복원하고 Hermes gateway를 재시작한 뒤 일반 대화와 DB read를 확인한다. Task 설정 기록은 삭제하지 않고 read-only 감사 자료로 남긴다.

## 실패 처리

- 시작 선택 오류: Task를 만들지 않고 Retry 또는 Continue in Chat 제공
- Task 설정 저장 실패: GitHub write 0회
- GitHub 이슈 생성 실패: Task 설정을 active로 만들지 않음
- 설정 또는 내용 hash 불일치: 카드 생성과 병합 차단
- GitHub 일시 장애: 제한된 read 재시도 뒤 blocked
- `safe_auto` 규칙 밖 정상 변경: 실패가 아니라 사람 병합
- 자동 병합 권한 만료: 사람 병합
- branch conflict 또는 반복 갱신: 사람 병합
- 형식 오류, 불완전 API 결과, 알 수 없는 상태: check error와 blocked
- 병합 성공: repository, issue, PR, merged commit, settings hash를 activity log에 기록

대화 원문, access token, secret은 Task 설정, GitHub 이슈, activity log에 저장하지 않는다.

## 범위 제외

- repository별 기본 `task_flow` 또는 `merge_mode`
- 이전 Task 선택 자동 재사용
- LLM 기반 위험도 분류
- GitHub ruleset 또는 `--admin` 우회
- 구 카드·schema·label을 새 worker가 읽는 호환 layer
- 서로 다른 repository의 PR을 하나의 transaction으로 병합
- GitHub Actions 안에서 builder, reviewer, `deep_checker` LLM 실행

## 완료 기준

1. 새 대화 첫 입력에서 모든 Hermes surface가 `chat|task`를 선택한다.
2. `chat`은 외부 작업 생성 없이 일반 대화를 수행한다.
3. `task`는 `task_flow`와 `merge_mode`를 매번 선택하고 미리보기 확인 뒤에만 이슈를 만든다.
4. 9개 조합이 표의 경로대로 작동한다.
5. `safe_auto`는 고정 허용 규칙만 자동 병합한다.
6. `full_auto`는 선택한 흐름과 공통 병합 검사를 모두 통과한 현재 commit만 자동 병합한다.
7. branch 갱신, commit 경쟁, 만료, conflict에서 검증되지 않은 병합이 발생하지 않는다.
8. 구 형식 없이 새 이름만 사용하며 clean break 배포와 전체 release restore가 검증된다.

## 변경이력

- 2026-07-16 | 설계 확정 | 변경: Hermes 시작 선택, Task별 작업·병합 설정, 9개 조합, `safe_auto`, `full_auto`, clean break 이름 체계와 restore 절차 정의 | 검증: 사용자 승인 대화와 현재 Forge 실행 경계 대조
