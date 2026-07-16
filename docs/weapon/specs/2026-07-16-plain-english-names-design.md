# INFINITY_FORGE Plain English 명칭 설계

## 목적

INFINITY_FORGE가 직접 만든 설정 이름, 상태 이름, JSON 필드, 파일명, 사용자 메시지를 설명 없이도 기능을 짐작할 수 있는 영어로 통일한다. Git·GitHub·Hermes처럼 외부 제품이나 표준이 정한 이름은 유지하고, 처음 등장할 때 한국어 뜻을 함께 적는다.

이번 변경은 호환 이름을 덧붙이는 점진 전환이 아니다. 활성 코드와 활성 문서를 한 배포에서 새 이름으로 바꾸는 clean break(기존 이름을 함께 지원하지 않는 일괄 전환)다. 전환 뒤에는 이전 설정 키, JSON 필드, Forge 라벨, 명령 별칭을 읽거나 쓰지 않는다.

## 확정 결정

1. `chat`은 일반 대화만 수행하며 GitHub 이슈나 Kanban 카드를 만들지 않는다.
2. `task`는 작업마다 `task_flow`와 `merge_mode`를 모두 새로 선택한다.
3. `task_flow`와 `merge_mode`의 9개 조합을 전부 허용한다.
4. INFINITY_FORGE가 만든 이름은 화면, 문서, 설정, JSON, CLI, Slack 메시지, 함수, 클래스, 파일명까지 같은 단어를 사용한다.
5. 기존 이름을 위한 alias(별칭), 이중 읽기, 이중 쓰기, 자동 변환 fallback은 두지 않는다.
6. 외부 표준 이름과 현재 운영을 보호하는 명시적 안전 예외만 유지한다.

> RISK(breaking): 설정 키, JSON 형식, 라벨, 명령 계약이 한 번에 바뀐다. 이전 이름을 사용하는 호출자는 명확한 오류로 중단되며 조용히 새 의미로 변환되지 않는다.

## 1. 이름 작성 원칙

### 1.1 구체적인 동작과 결과를 이름에 쓴다

- 추상적인 품질 수준보다 실제 실행 순서를 쓴다.
- 은유보다 프로그램이 하는 일을 직접 쓴다.
- `artifact`처럼 내용이 드러나지 않는 단어는 실제 내용에 따라 `restore_package`, `release_package`, `test_report`처럼 구체화한다.
- boolean은 `enabled`, `required`, `expires_at`처럼 참일 때의 의미가 분명한 긍정형을 사용한다.
- 같은 개념에 둘 이상의 영단어를 쓰지 않는다.

### 1.2 표기 형식

| 위치 | 형식 | 예시 |
|---|---|---|
| 설정·JSON·Python | `snake_case` | `merge_mode`, `tested_commit` |
| CLI 선택값 | 소문자 `snake_case` | `safe_auto` |
| 화면 표시 | 짧은 Title Case | `Safe Auto Merge` |
| Slack·문서 | 영어 이름 뒤 즉시 한국어 설명 | `tested commit(검증한 commit)` |
| 오류 접두사 | 대문자 `SNAKE_CASE` | `CHECK_ERROR:` |

## 2. 대화와 작업 설정의 공식 이름

다음 표가 대화 시작 화면, 설정 저장소, JSON, 로그, 테스트에서 사용하는 유일한 이름이다.

| 이전 이름 | 공식 이름 | 허용 값 | 화면 의미 |
|---|---|---|---|
| `interaction_mode` | `mode` | `chat`, `task` | 일반 대화 또는 실제 작업 |
| `assurance_policy` | `task_flow` | 아래 표 참조 | 작업이 거치는 실행·검토 순서 |
| `merge_policy` | `merge_mode` | 아래 표 참조 | 검증 뒤 병합 방식 |

### 2.1 `task_flow`

| 값 | 화면 표시 | 실제 순서 |
|---|---|---|
| `build` | `Build` | builder만 실행 |
| `build_review` | `Build + Review` | builder → reviewer |
| `build_review_deep_check` | `Build + Review + Deep Check` | builder → reviewer → deep_checker |

### 2.2 `merge_mode`

| 값 | 화면 표시 | 실제 동작 |
|---|---|---|
| `manual` | `Manual Merge` | 사람이 모든 PR을 병합 |
| `safe_auto` | `Safe Files Auto-Merge` | 결정론적 저위험 규칙을 통과한 PR만 자동 병합 |
| `full_auto` | `All Validated PRs Auto-Merge` | 선택한 `task_flow`와 공통 안전 검사를 통과한 모든 PR을 자동 병합 |

`P1`, `P2`, `P3`, `direct`, `reviewed`, `adversarial`은 활성 설정, 화면, 명령, JSON에서 사용하지 않는다. 과거 결정을 설명하는 변경이력에서만 이전 이름임을 명시할 수 있다.

## 3. 운영·검증 이름의 공식 대응표

| 이전 이름 | 공식 이름 | 의미 |
|---|---|---|
| `preimage_hash` | `before_file_hash` | 변경 전 파일 hash |
| `postimage_hash` | `after_file_hash` | 변경 후 파일 hash |
| `rollback_artifact` | `restore_package` | 이전 버전으로 되돌릴 때 쓰는 파일 묶음 |
| `policy_ledger` | `task_settings_log` | 사용자가 확인한 Task 설정 기록 |
| `policy_digest` | `task_settings_hash` | Task 설정 전체의 SHA-256 |
| `scope_digest` | `task_content_hash` | 제목·본문·수용 기준의 SHA-256 |
| `reconciler` | `worker` | 저장된 상태를 읽고 다음 동작을 실행하는 프로그램 |
| `canary` | `system_check` | 실제 작업 전 알려진 입력으로 수행하는 자체 점검 |
| `shadow_mode` | `preview_only` | 결과만 계산하고 외부 변경은 하지 않는 실행 |
| `fail_closed` | `stop_on_error` | 오류가 있으면 진행하지 않는 규칙 |
| `exact_head` | `tested_commit` | 실제로 검사한 PR commit |
| `receipt` | `proof_record` | 단계 완료와 검증 결과를 묶은 기록 |
| `projection` | `displayed_status` | 내부 상태에서 계산한 사용자 표시 상태 |
| `frontier` | `current_step` | 현재 작업 흐름의 마지막 활성 단계 |
| `gate` | `check` | 다음 단계로 넘어가기 전에 수행하는 검사 |
| `executor` | `builder` | 코드를 만들고 검증하는 작업 역할 |
| `critic` | `deep_checker` | 정상 검토 뒤에도 남을 수 있는 결함을 깊게 찾는 역할 |
| `executor_rework` | `fix` | 발견된 문제를 같은 PR에서 고치는 단계 |

문맥에 따라 의미가 달라지는 단어는 단순 치환하지 않고 실제 대상을 이름에 넣는다.

| 금지하는 포괄어 | 대상별 공식 이름 예시 |
|---|---|
| `artifact` | `restore_package`, `release_package`, `test_report` |
| `digest` | `task_settings_hash`, `task_content_hash`, `source_result_hash` |
| `snapshot` | `saved_database`, `saved_labels`, `saved_service_state` |
| `auth` | `approval_record`, `github_token`, `slack_token` |

### 3.1 공식 오류 이름

| 이전 이름 | 공식 이름 | 의미 |
|---|---|---|
| `GATE_ERROR:` | `CHECK_ERROR:` | 검사 프로그램 또는 외부 장치 오류 |
| `CANARY_FAIL:` | `SYSTEM_CHECK_FAILED:` | 사전 자체 점검 실패 |
| `DRIFT:` | `STATE_MISMATCH:` | 두 상태가 서로 다름 |

테스트 실패와 장치 오류는 계속 구분한다. 테스트가 재현한 제품 결함은 `TESTS_FAILED:`로, 검사 장치 자체의 오류는 `CHECK_ERROR:`로 기록한다.

## 4. Forge 상태 라벨

GitHub의 `forge:*` 라벨은 INFINITY_FORGE가 소유한 사용자 화면 계약이므로 clean break에 포함한다.

| 이전 라벨 | 공식 라벨 | 사용자 의미 |
|---|---|---|
| `forge:spec-draft` | `forge:needs-details` | 작업 설명 보완 필요 |
| `forge:adr` | `forge:needs-decision` | 사람의 설계 결정 필요 |
| `forge:need-execution` | `forge:ready-to-build` | builder 실행 승인 완료 |
| `forge:in-progress` | `forge:building` | builder 실행 중 |
| `forge:need-review` | `forge:reviewing` | reviewer 실행 대기 또는 실행 중 |
| `forge:need-critic` | `forge:deep-checking` | deep_checker 실행 대기 또는 실행 중 |
| `forge:mergeable` | `forge:ready-to-merge` | 선택한 검증 경로 완료 |
| `forge:blocked` | `forge:waiting-for-help` | 사람 입력 또는 외부 문제로 정지 |
| `forge:failed` | `forge:failed` | 정해진 재시도 한도 소진 |

라벨 전환 중 두 이름을 동시에 허용하지 않는다. 작업 배차를 멈춘 상태에서 라벨 이름, 코드 상수, 문서, 테스트를 함께 바꾸고 바로 검증한다.

> RISK(race): 라벨 코드와 GitHub의 실제 라벨을 서로 다른 시점에 바꾸면 작업이 누락되거나 중복 수입될 수 있다. 전환 중에는 Task 배차와 자동 병합을 모두 정지한다.

## 5. 적용 범위

### 5.1 반드시 바꾸는 활성 범위

- `README.md`
- `docs/plan.md`
- `docs/easy_guide.md`
- `docs/user-runbook.md`
- `docs/automation-architecture.md`
- `docs/ops-guide.md`
- `docs/backup-guide.md`
- `docs/setup/`의 현재 사용 안내서
- `forge/ops/`, `forge/scripts/`, `forge/hooks/`, `forge/skills/`
- `forge/schemas/`와 모든 테스트 fixture
- `.github/`의 사용자 메시지, 환경 변수, 입력·출력 필드
- Hermes 공통 선택 gate와 INFINITY_FORGE 플러그인의 화면·상태·저장 필드
- Slack 알림, CLI 출력, 로그 접두사, systemd의 설명 문구

함수명, 클래스명, enum 값, 파일명, JSON key, 설정 key, 환경 변수, GitHub 라벨, 테스트 이름까지 같은 공식 이름을 사용한다.

### 5.2 바꾸지 않는 기록

- Git commit 기록
- 닫힌 GitHub 이슈·PR의 과거 본문과 코멘트
- 이전 배포 로그와 백업 파일 내부 문자열
- 과거 결정을 그대로 보존하는 날짜 기반 `docs/weapon/plans/` 및 `docs/weapon/specs/` 문서

과거 문서는 현재 사용법의 근거로 사용하지 않는다. 활성 문서는 이 설계와 현재 코드만 가리킨다.

## 6. 외부 표준 이름

다음 이름은 INFINITY_FORGE가 만들지 않았으므로 유지한다.

- Git·GitHub: issue, PR, merge, commit, branch, HEAD, SHA, diff, worktree, CI, GitHub Actions, ruleset
- Hermes: Hermes, Kanban, Gateway, Desktop, session
- Slack: Slack, channel, app, bot
- 데이터·실행 환경: API, JSON, YAML, SQLite, CLI, VPS, SSH, systemd, Docker, PowerShell, tmux, pytest
- 제품명: Codex, Claude Code, MEMEX

사용자 문서에서는 첫 등장 시 `HEAD(PR의 최신 commit)`, `ruleset(GitHub branch 보호 규칙)`처럼 한국어 설명을 즉시 붙인다. 외부 명령이 반환하는 status·conclusion 값은 원문을 보존하고 옆에 한국어 의미를 표시한다.

## 7. 운영 안전 예외

clean break는 Forge가 자유롭게 바꿀 수 있는 이름에 적용한다. 다음 식별자는 현재 서비스와 GitHub 보호 규칙이 직접 참조하므로 이번 명칭 변경에서 이름을 바꾸지 않는다. 이들은 이전 이름을 지원하는 호환 계층이 아니라 계속 사용하는 단일 공식 식별자다.

### 7.1 CI와 GitHub ruleset

- 필수 GitHub Actions check 이름 `eval`을 유지한다.
- 현재 workflow 파일 `.github/workflows/capability-eval.yml`의 live check 연결을 유지한다.
- GitHub ruleset `protect-main`을 삭제하거나 새 ruleset으로 교체하지 않는다.
- 배포 전후에 ruleset을 다시 읽어 PR 필수, 최신 branch 필수, `eval` 필수, bypass 없음이 동일한지 확인한다.
- ruleset ID는 문서나 코드에 새 상수로 복사하지 않고 GitHub API의 현재 값을 읽는다.

필수 check나 ruleset 이름 변경은 먼저 default branch에서 새 check가 성공하는 것을 증명한 뒤 ruleset을 별도 전환해야 하므로 이 작업과 분리한다.

### 7.2 systemd 식별자

- `hermes-gateway.service`, `hermes-dashboard.service`를 유지한다.
- 현재 배포된 `forge-*.service`, `forge-*.timer` unit 이름을 유지한다.
- unit의 `Description`과 사용자 출력은 새 공식 이름으로 바꿀 수 있다.
- 같은 역할의 새 unit을 나란히 설치하지 않는다.
- `systemctl --user daemon-reload` 뒤 기존 unit이 정확히 한 개씩 enabled·active인지 확인한다.

> RISK(side-effect): live CI, ruleset, systemd unit 이름을 동시에 바꾸면 모든 PR 또는 모든 자동 작업이 멈출 수 있다. 이 세 경계는 이번 clean break에서 유지하고 표시 문구만 정리한다.

## 8. 데이터 계약

Task 설정은 `task_settings_log`에 다음 공식 필드로 저장한다.

```json
{
  "request_id": "UUID",
  "repository": "OWNER/REPO",
  "issue_number": 123,
  "mode": "task",
  "task_flow": "build_review",
  "merge_mode": "safe_auto",
  "task_content_hash": "64자리 SHA-256",
  "task_settings_hash": "64자리 SHA-256",
  "confirmed_by": "사용자 식별자",
  "confirmed_at": "RFC 3339 시각",
  "auto_merge_expires_at": "RFC 3339 시각 또는 null",
  "status": "prepared 또는 active 또는 cancelled"
}
```

- `chat`은 Task 설정 레코드를 만들지 않는다.
- `task`는 `task_flow`와 `merge_mode`가 없으면 생성되지 않는다.
- `manual`은 `auto_merge_expires_at=null`이다.
- `safe_auto`와 `full_auto`는 확인 후 최대 12시간만 유효하다.
- 이전 필드가 하나라도 들어오면 명시적인 data-format 오류를 반환한다.
- 오류 시 이전 필드로 다시 읽는 fallback은 없다.

단계 결과의 `proof_record`는 `task_settings_hash`, 이전 결과의 구체적인 `source_result_hash`, `tested_commit`을 사용한다. 모든 자동 병합은 `tested_commit`과 현재 PR commit이 같을 때만 실행한다.

> RISK(data-loss): 저장 필드와 라벨을 바꾸는 동안 활성 Task가 있으면 진행 상태를 잃을 수 있다. 전환 시작 조건은 running·ready 자동화 카드 0개와 자동 병합 정지다.

## 9. clean break 전환 절차

1. 자동 병합 worker, Task 수입, 단계 worker를 정지한다.
2. Hermes에서 running·ready 자동화 카드가 0개인지 확인한다. 0개가 아니면 전환을 시작하지 않는다.
3. GitHub에서 open PR, Forge 라벨이 붙은 open issue, 현재 ruleset을 저장한다.
4. Hermes SQLite DB를 `.backup`으로 저장하고 `PRAGMA integrity_check`가 `ok`인지 확인한다.
5. 현재 배포 파일과 systemd 상태를 `restore_package`로 만든다.
6. 활성 코드, 데이터 형식, 라벨 상수, 메시지, 테스트, 활성 문서를 하나의 release commit으로 바꾼다.
7. GitHub 라벨 이름을 공식 라벨로 바꾸고 open issue마다 Forge 상태 라벨이 정확히 하나인지 확인한다.
8. Windows, Linux staging, VPS에 같은 release commit을 배포한다.
9. 전체 검증 기준을 실행한다.
10. 검증이 모두 성공한 뒤 Task 수입과 단계 worker를 켠다.
11. `safe_auto` 시험 Task가 성공한 뒤에만 자동 병합 worker를 켠다.

전환 뒤 기존 이름을 읽는 코드, 환경 변수, 명령 별칭, 임시 변환 스크립트는 배포 대상에 남기지 않는다.

## 10. 검증 기준

### 10.1 정적 검사

- 활성 범위에서 이전 공식 이름을 검색했을 때 허용된 변경이력 설명과 외부 안전 식별자를 제외하고 0건이어야 한다.
- `interaction_mode`, `assurance_policy`, `merge_policy`, `P1`, `P2`, `P3`, `policy_ledger`, `policy_digest`, `scope_digest`, `preimage_hash`, `postimage_hash`, `rollback_artifact`, `receipt`, `projection`, `frontier`, `GATE_ERROR`, `CANARY_FAIL`이 활성 설정·코드·메시지에 없어야 한다.
- Markdown 내부 링크와 로컬 파일 링크가 모두 유효해야 한다.
- 새 파일명과 Python import가 Windows와 Linux에서 모두 대소문자까지 일치해야 한다.

### 10.2 데이터와 단위 테스트

- `mode=chat`은 GitHub 이슈와 Kanban 카드를 0개 생성한다.
- `mode=task`는 `task_flow`와 `merge_mode`가 없으면 실패한다.
- `task_flow × merge_mode` 9개 조합이 모두 serialize·parse된다.
- 이전 설정 키와 JSON 필드는 모두 거절된다.
- `task_content_hash`, `task_settings_hash`, `source_result_hash`는 같은 입력에 항상 같은 SHA-256을 만든다.
- `tested_commit`이 현재 PR commit과 다르면 자동 병합이 거절된다.
- 공식 Forge 라벨 9개만 상태 라벨로 인식한다.
- `CHECK_ERROR:`, `SYSTEM_CHECK_FAILED:`, `STATE_MISMATCH:`가 문서와 실제 출력에서 일치한다.

### 10.3 통합 테스트

- Chat 선택 → 첫 질문 전달 → 일반 대화 응답 → 외부 쓰기 0건을 확인한다.
- Task 선택 → `task_flow` → `merge_mode` → 작업 내용 → 미리보기 → 확인 → GitHub 이슈·라벨·Kanban 카드 생성 순서를 확인한다.
- `build + manual`, `build_review + safe_auto`, `build_review_deep_check + full_auto` 대표 경로가 공식 이름만으로 끝까지 진행한다.
- Slack, Desktop, TUI, CLI가 같은 선택값과 같은 설명을 표시한다.
- `preview_only`는 계산 결과를 출력하지만 GitHub, Hermes DB, Slack에 쓰지 않는다.
- 같은 입력을 두 번 처리해도 GitHub 이슈와 Kanban 카드가 중복 생성되지 않는다.

### 10.4 live 운영 확인

- GitHub의 현재 required check가 정확히 `eval`이며 성공 상태다.
- `protect-main` ruleset의 PR 필수, 최신 branch 필수, `eval` 필수, bypass 없음이 배포 전과 같다.
- `systemctl --user is-active hermes-gateway`가 `active`다.
- 모든 `forge-*.timer`가 정확히 한 개씩 enabled 상태이며 같은 역할의 중복 unit이 없다.
- Hermes DB의 `PRAGMA integrity_check`가 `ok`다.
- system check와 state mismatch check가 공식 오류 이름으로 정상 종료한다.

## 11. restore package와 복원

`restore_package`는 대상 환경별로 다음을 포함한다.

- 이전 release commit SHA
- 변경 대상 파일의 `before_file_hash` 목록
- 새 release 파일의 `after_file_hash` 목록
- 이전 release 파일 묶음
- Hermes DB의 검증된 SQLite backup
- open issue의 Forge 라벨 목록
- GitHub ruleset readback JSON
- `systemctl --user list-unit-files`와 active unit 목록
- 복원 순서와 대상 경로

secret, token, 대화 원문은 `restore_package`에 넣지 않는다.

### 11.1 즉시 복원 조건

다음 중 하나라도 발생하면 새 기능을 계속 운영하지 않고 이전 release로 복원한다.

- DB integrity check가 `ok`가 아님
- GitHub ruleset의 필수 조건이 배포 전과 다름
- `eval` required check가 사라지거나 이름이 바뀜
- Gateway 또는 필수 timer가 60초 안에 active가 되지 않음
- 같은 역할의 systemd unit이 두 개 이상 실행됨
- open issue에 Forge 상태 라벨이 0개 또는 2개 이상 존재함
- 기존 이름이 새 로그·JSON·Slack 메시지에 출력됨
- Chat이 외부 쓰기를 수행함
- 9개 설정 조합 중 하나라도 parse되지 않음
- `tested_commit`이 다른 PR commit의 자동 병합을 허용함

### 11.2 복원 순서

1. 자동 병합, Task 수입, 단계 worker를 정지한다.
2. 현재 배포 파일의 `after_file_hash`를 확인해 예상하지 않은 외부 변경이 없는지 확인한다.
3. 이전 release 파일을 복원하고 `before_file_hash`와 일치하는지 확인한다.
4. schema 또는 저장 데이터가 변경됐고 새 형식을 읽을 수 없을 때만 검증된 SQLite backup을 복원한다.
5. GitHub 라벨 이름을 저장된 이전 목록으로 되돌린다.
6. `systemctl --user daemon-reload` 후 기존 unit만 enable·start한다.
7. Gateway, timer, DB, ruleset, `eval`을 다시 확인한다.
8. 모든 복원 검사가 성공한 뒤 Task 수입을 재개한다. 자동 병합은 별도 시험 Task 성공 뒤 재개한다.

복원 성공 기준은 DB integrity `ok`, ruleset 동일, `eval` 성공, Gateway·timer active, Forge 라벨 단일성, Chat 외부 쓰기 0건이다.

## 12. 장기 결과

- 다음 기능은 새 공식 이름만 사용하므로 같은 개념의 이름이 다시 갈라지지 않는다.
- 6개월 뒤 운영자는 `task_flow`, `merge_mode`, `tested_commit`, `restore_package`만 알면 현재 설정과 복구 절차를 읽을 수 있다.
- 잘못된 이전 이름은 즉시 오류가 되어 누락된 호출자를 배포 시점에 발견한다.
- live CI·ruleset·systemd 경계는 그대로 유지해 명칭 정리 때문에 PR 보호나 24시간 자동화가 중단되는 위험을 분리한다.

## 변경이력

- 2026-07-16 | Plain English 명칭 설계 확정 | 변경: Forge 소유 이름의 공식 대응표, clean break 범위, live CI·ruleset·systemd 안전 예외, 검증·복원 기준을 정의 | 검증: 사용자 승인 결정과 현재 README·runbook·운영 스크립트의 노출 이름을 대조해 작성
