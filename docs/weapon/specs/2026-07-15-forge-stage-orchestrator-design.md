# Forge 단계 오케스트레이터 설계

## 목적

GitHub 이슈에서 생성된 executor 카드가 완료된 뒤, 같은 결과를 다시 처리해도 중복 없이 reviewer와 critic을 새 Hermes 세션으로 연결한다. reviewer 반려와 critic 결함 발견은 새 executor 재작업 카드로 되돌리고, critic이 변경한 최신 PR HEAD의 필수 CI가 모두 성공했을 때만 `forge:mergeable`로 투영한다.

GitHub `main`에는 PR 필수와 CI 성공 필수 ruleset을 적용한다. 자동 머지는 이번 범위에 포함하지 않으며 P1 사람 머지를 유지한다.

## 확정 설계

### 1. 상태 소유권

- Hermes Kanban은 루트 카드, 역할별 단계 카드, run 결과의 운영 원장이다.
- GitHub는 이슈·PR·CI 상태의 원장이다.
- `label-mirror.py`만 `forge:*` 상태 라벨을 쓴다.
- `stage-reconciler.py`는 Hermes 카드를 생성하지만 GitHub 라벨을 쓰지 않는다.
- GitHub Actions에는 LLM을 실행하지 않는다.

기존 “이슈와 카드 1:1” 불변식은 다음처럼 구체화한다.

```text
GitHub 이슈당 github-issue:* 루트 카드 정확히 1개
상위 단계 결과 영수증당 forge-stage:* 자식 카드 정확히 1개
```

### 2. 단계와 결과

```text
executor 완료 run + 유효 handoff + open/non-draft PR + 현재 HEAD의 eval=success
  → reviewer 생성

reviewer approve + 검토 HEAD 불변
  → critic 생성

reviewer reject + 비어 있지 않은 reflection
  → executor-rework 생성

critic pass + 추가 테스트 커밋이 현재 HEAD에 포함 + 현재 HEAD의 eval=success
  → forge:mergeable

critic defect_found + 비어 있지 않은 reflection
  → executor-rework 생성
```

API 오류, JSON 오류, PR HEAD 불일치, 필수 check 누락·중복·pending·failure는 성공으로 대체하지 않는다. 컨트롤러는 해당 파이프라인을 그대로 두고 exit 2로 실패한다.

### 3. 전이 영수증과 멱등키

현재 저장소에는 계획 문서의 보호된 `forge-guard` completion receipt 구현이 없다. 이번 구현은 Hermes의 완료 run을 엄격히 파싱하고 다음 canonical evidence를 SHA-256으로 해시해 전이 영수증으로 사용한다.

```json
{
  "task_id": "t_...",
  "run_id": 12,
  "stage": "executor|reviewer|critic",
  "summary": {},
  "metadata": {},
  "pr_url": "https://github.com/OWNER/REPO/pull/N",
  "head_sha": "40자리 SHA"
}
```

다음 카드의 멱등키는 상위 전이 영수증으로 결정한다.

```text
forge-stage:<OWNER/REPO>#<ISSUE>:reviewer:<digest16>
forge-stage:<OWNER/REPO>#<ISSUE>:critic:<digest16>
forge-stage:<OWNER/REPO>#<ISSUE>:executor-rework:<digest16>
```

Hermes `kanban create --idempotency-key`가 중복 생성을 막는다. 카드 생성 성공 뒤 프로세스가 중단돼도 다음 실행은 같은 key를 재사용한다.

> RISK(race): GitHub와 Hermes는 하나의 트랜잭션으로 묶이지 않는다. 모든 외부 생성은 결정적 멱등키로 재시도하고, 불완전한 조회는 전이하지 않는다.

향후 보호된 `forge-guard` receipt가 구현되면 evidence 공급자만 교체하고 단계 판정과 멱등키 인터페이스는 유지한다.

### 4. 역할별 엄격한 결과 계약

executor는 기존 handoff를 유지하되 다음 필드를 필수로 검증한다.

- `pr_url`: 같은 repository의 PR URL
- `changed_files`: 문자열 배열
- `implemented`: 비어 있지 않은 문자열 배열
- `not_implemented`: 배열
- `verified_by`: 비어 있지 않은 객체

reviewer 결과는 다음 필드를 갖는다.

- `schema_version = forge-reviewer-result/v1`
- `verdict = approve|reject`
- `source_digest`
- `pr_url`
- `head_sha`
- `delta_check`
- `spec_check`
- reject이면 비어 있지 않은 `reflection`

critic 결과는 다음 필드를 갖는다.

- `schema_version = forge-critic-result/v1`
- `outcome = pass|defect_found`
- `source_digest`
- `pr_url`
- `reviewed_head_sha`
- `result_head_sha`
- `added_tests`
- `scenarios`
- defect_found이면 비어 있지 않은 `reflection`

critic의 품질 반려는 정상적인 `defect_found` 완료 결과다. 인프라 장애나 protocol violation에 사용하는 Hermes `blocked`와 혼합하지 않는다.

### 5. 재작업 루프

완료된 Hermes 카드는 reopen하지 않는다. 반려될 때마다 상위 reviewer 또는 critic 카드 아래 새 executor-rework 자식 카드를 만든다. 이 방식은 다음 이력을 DAG로 보존한다.

```text
executor-0
  → reviewer-0 reject
    → executor-rework-1
      → reviewer-1 approve
        → critic-1 defect_found
          → executor-rework-2
            → reviewer-2 approve
              → critic-2 pass
```

재작업 단계는 파이프라인당 최대 3개다. 네 번째 반려 결과에서는 새 executor를 만들지 않고 `forge:failed`로 투영한다. 각 개별 Hermes 카드는 현재 규약대로 최대 4개 worker session을 사용할 수 있다.

### 6. PR HEAD와 CI 바인딩

reviewer 카드는 생성 시점의 executor PR HEAD에 묶인다. reviewer 완료 시 live HEAD가 달라졌으면 verdict를 사용하지 않는다.

critic은 같은 PR branch에 테스트를 추가하므로 HEAD가 바뀔 수 있다. `forge:mergeable` 판정은 critic 결과의 `result_head_sha`와 live HEAD가 같고, 바로 그 SHA에서 required check `eval`이 정확히 하나 존재하며 `success`일 때만 가능하다.

이전 HEAD의 green 결과는 새 HEAD에 재사용하지 않는다.

### 7. GitHub ruleset

현재 public repository에 다음 active ruleset을 적용한다.

- 이름: `protect-main`
- 대상: default branch (`main`)
- bypass: 없음
- pull request 필수
- required approving reviews: 0
- required status check: `eval`, source GitHub Actions
- branch up-to-date 필수(strict)
- force push 차단
- branch deletion 차단

단일 GitHub 계정 환경이므로 approval 1개는 요구하지 않는다. 독립 collaborator 또는 GitHub App reviewer가 준비된 뒤 별도 변경으로 강화한다.

> RISK(security): bypass actor를 추가하거나 required check source를 `any source`로 완화하면 red CI 차단을 우회할 수 있다. 기본 설정은 bypass 없음과 GitHub Actions source 고정이다.

### 8. CI 이름 안정성

현재 live check 이름 `eval`을 ruleset의 안정적인 최종 gate 이름으로 유지한다. 내부 Linux·Windows 검사가 늘어나면 `eval`을 aggregator job으로 바꾸고 모든 선행 job 성공에만 success가 되게 한다. ruleset 활성화 뒤 check 이름을 먼저 바꾸지 않는다.

### 9. 파일 책임

- `forge/ops/contracts.py`: 단계·run·PR snapshot·action 값 객체와 strict parser
- `forge/ops/stage_reconciler.py`: side effect 없는 `decide_next_action`과 카드 spec 생성
- `forge/ops/hermes.py`: read-only SQLite 조회와 Hermes create adapter
- `forge/ops/github.py`: PR HEAD와 check-runs 조회 adapter
- `forge/ops/label_projection.py`: 전체 pipeline frontier를 단일 라벨로 변환
- `forge/scripts/stage-reconciler.py`: 한 번 실행하는 CLI entrypoint
- `forge/scripts/label-mirror.py`: 이슈 수입과 단일 라벨 writer
- `forge/schemas/*`: reviewer·critic 결과 JSON Schema
- `tests/ops/*`: pure state machine, parser, adapter argv, crash/retry 회귀 테스트

### 10. 검증 시나리오

1. executor evidence와 현재 HEAD `eval=success`에서 reviewer가 정확히 1개 생성된다.
2. 같은 입력을 다시 처리해도 reviewer가 중복 생성되지 않는다.
3. reviewer approve에서만 critic이 생성된다.
4. reviewer reject는 critic을 만들지 않고 reflection이 포함된 executor-rework를 만든다.
5. critic defect_found는 executor-rework를 만든다.
6. critic pass라도 이전 HEAD가 green이면 mergeable이 되지 않는다.
7. critic 결과 HEAD의 `eval=success`에서만 mergeable이 된다.
8. malformed result, missing check, duplicate check, API 실패는 exit 2다.
9. ruleset 적용 뒤 red/pending CI PR은 merge 불가이고 green PR만 merge 가능하다.
10. VPS timer가 새 entrypoint를 실행하고 기존 executor 수입 경로가 회귀하지 않는다.

## 범위 제외

- P2/P3 자동 머지
- GitHub required approving review 1개 이상
- Hermes core 수정
- GitHub Actions에서 LLM 실행
- 전체 `forge-guard` completion-policy 구현
- 멀티 repository 동시 PR 원자 머지

## 변경이력

- 2026-07-15 | 설계 확정 | 변경: executor→reviewer→critic JIT 전이, reject 재작업, exact-HEAD CI gate, main ruleset 계약을 정의 | 검증: `docs/plan.md`, 현재 코드, VPS Hermes v0.18.2 CLI·SQLite, GitHub live check/ruleset 상태 대조
