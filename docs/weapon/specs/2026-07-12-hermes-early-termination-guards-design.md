# Hermes 조기 종료 방지 설계

- 상태: 설계 방향 승인 완료, 작성본 사용자 검토 대기
- 작성일: 2026-07-12
- 적용 대상: Windows 로컬, 일반 Linux, Ubuntu VPS 실운영
- 기준 문서: `docs/plan.md`의 D9, D10, D13, D16, D17, D24
- 선택안: 공통 검증기 + Codex Stop hook + 재검증 receipt + Hermes 완료 불변식 + PR CI

## 1. 목적

Hermes executor가 작업을 덜 끝낸 상태에서 성공을 선언하거나, Codex가 테스트·잔여 작업·검증 증거 없이 종료해도 다음 단계로 전파되지 않게 한다.

완료는 모델의 문장이 아니라 다음 조건이 모두 참일 때만 인정한다.

1. 카드의 수용 기준이 `implemented`와 `not_implemented`로 빠짐없이 분할됐다.
2. 모든 `implemented` 항목에 실제 검증 증거가 있다.
3. 모든 `not_implemented` 항목이 기존 또는 신규 GitHub issue, 혹은 `forge:adr` issue로 물질화됐다.
4. 기준 SHA 이후의 실제 변경이 있고, handoff 파일만 바뀐 작업이 아니다.
5. 명시된 테스트·lint가 성공했다.
6. 태스크 예산과 최대 4개의 고유 Codex 세션 제한을 지켰다.
7. 동일 증거를 Codex Stop hook, 종료 후 runner, Hermes 완료 경계, PR CI가 검증했다.
8. 검증된 receipt가 현재 task, run, handoff, repository tree와 정확히 일치한다.

## 2. 현재 문제와 실측 근거

현재 `forge/hooks/codex-stop-gate.sh`는 배포되지만 실제 `codex exec` 종료 경로에 등록되지 않았다. canary만 이 파일을 직접 호출한다.

현재 gate에는 다음 fail-open 경로가 있다.

- handoff 파일이 없어도 성공한다.
- 필드 이름만 확인하고 빈 값, 타입, 수용 기준 전체 분할을 확인하지 않는다.
- 존재하지 않는 `card_id`를 허용하고 GitHub issue/ADR 검증도 불완전하다.
- 현재 dirty tree만 보므로 작업을 commit한 clean tree를 `empty diff`로 오판한다.
- 기존 dirty 파일이나 handoff 파일 하나만 있어도 구현 변경으로 오인할 수 있다.
- lint와 태스크 예산 cap을 실제로 검사하지 않는다.

운영 상태에서도 같은 단절이 확인됐다.

- VPS의 기존 카드 5개가 모두 `goal_mode=0`이다.
- 기존 완료 handoff 중 `not_implemented`가 배열이 아닌 문자열인 사례가 있다.
- 기존 PR #2와 #4에는 status check가 없었다.
- canary와 drift는 핵심 종료 경로를 검사하지 않은 채 green을 보고한다.
- 현재 private/free GitHub 저장소에서는 branch protection과 ruleset API가 403을 반환한다.

Hermes v0.18.2 자체의 보호도 충분하지 않다.

- `protocol_violation`은 아무 완료·차단 호출 없이 종료한 worker만 감지한다. 성급한 `kanban_complete`는 막지 못한다.
- `goal_mode` judge는 보조 모델 부재나 오류 때 fail-open한다.
- worker tool, CLI, Dashboard 완료 경로는 모두 `complete_task()`로 모이지만 현재 handoff/receipt를 요구하지 않는다.
- 사후 quarantine만 쓰면 `done` 전이 뒤 자식 카드가 먼저 ready가 되는 경쟁 조건이 생긴다.
- `--max-retries N`은 재시도 횟수가 아니라 N번째 연속 실패에서 차단한다.
- `status=failed`는 Hermes의 유효 상태가 아니다. 실패는 `blocked`와 `gave_up`/`retry_exhausted` event로 판정해야 한다.

## 3. 검토한 접근

| 접근 | 장점 | 한계 | 판정 |
|---|---|---|---|
| Stop hook만 연결 | 변경이 작고 같은 Codex 세션에서 즉시 수정 가능 | hook 신뢰 누락, 직접 complete, CLI·Dashboard 우회를 막지 못함 | 기각 |
| 공통 검증기 + hook + runner + receipt + 완료 불변식 | 모든 정상 완료 경로를 한 계약으로 묶고 Windows/Linux/VPS에서 같은 동작을 재사용 | 작은 Hermes carried patch와 배포 관리가 필요 | 채택 |
| 별도 verifier 카드가 executor를 승인 | executor와 검증자의 세션 분리가 명확함 | 카드·의존성·운영 복잡도가 커지고 raw executor `done` 경쟁 조건은 별도 해결 필요 | 후속 확장 |

채택안은 사용자가 승인한 2번 방어 구조다. 조사 중 확인된 `done → child ready` 경쟁 조건을 제거하기 위해 receipt 검증 위치를 Hermes의 `complete_task()` 직전으로 정한다. 이는 fail-open Hermes hook이 아니라 보호 태스크의 원자적 completion invariant다.

## 4. 보장 범위와 정직한 경계

### 보장한다

- `completion_policy=forge-v1`인 태스크는 유효한 최종 검증 없이 지원 API를 통해 `done`이 될 수 없다.
- tool, CLI, Dashboard처럼 `complete_task()`를 쓰는 정상 완료 경로가 같은 정책을 적용받는다.
- 검증기 누락, timeout, 잘못된 JSON, GitHub 조회 실패는 모두 fail-closed다.
- `TESTS_FAILED`는 같은 Codex 세션의 수정 또는 다음 고유 세션으로 이어진다.
- `GATE_ERROR`는 새 Codex 세션을 소비하지 않고 해당 태스크 완료만 보류한다.
- 네 번째 고유 Codex 세션까지 실패하면 더 실행하지 않고 `retry_exhausted`로 차단한다.
- receipt 없는 완료는 GitHub 라벨, issue close, spec coverage 완료로 투영되지 않는다.

### 보장하지 않는다

- DB 파일에 직접 SQL을 쓰는 관리자 또는 손상된 프로세스까지 애플리케이션 정책으로 막지는 않는다. DB 파일은 mode 600으로 제한하고 직접 SQL 완료를 비지원 운영으로 정한다.
- private/free GitHub 저장소에서 사람이 red check를 무시하고 UI로 merge하는 행위는 플랫폼 기능상 차단할 수 없다. 자동화는 green check 없이는 절대 merge하지 않고, P1 인간 승인 정책을 유지한다. 플랫폼 수준 required check는 GitHub Pro 또는 public 전환 뒤 별도 활성화한다.
- `goal_mode` judge의 판정을 결정론적 증거로 사용하지 않는다. goal mode는 같은 Hermes 세션의 지속성을 높이는 보조층이다.
- 사용자가 2026-07-12에 명시한 대로 이번 작업에서 노출 credential 회전은 수행하지 않는다. 값은 문서·로그·테스트 fixture에 기록하지 않는다.

## 5. 전체 구조

```text
GitHub issue/card
  -> task contract + immutable baseline manifest
  -> Hermes executor(goal mode)
  -> cross-platform Codex task runner
       -> Codex Stop hook
       -> shared verifier
       -> same-session continuation(L0)
       -> fresh-session retry ledger(L1, max 4 unique sessions)
  -> final verifier receipt
  -> Hermes complete_task() completion-policy gate
  -> done + receipt-consumed event in one DB transaction
  -> label mirror / PR CI / spec coverage

canary failure
  -> healthy marker removal
  -> independent dispatcher stop
  -> Slack gateway remains alive
```

## 6. 구성 요소

### 6.1 공통 검증기

Python 3.11 이상의 단일 검증 코어를 둔다. 경로와 subprocess 처리는 `pathlib`과 인자 배열을 사용해 Windows와 POSIX에서 동일한 규칙으로 동작하게 한다.

예정 경계는 다음과 같다.

- `forge/guard/contract.py`: task contract, handoff, receipt 타입과 순수 검증
- `forge/guard/git_state.py`: 다중 repository baseline·tree·working diff 계산
- `forge/guard/references.py`: GitHub issue/ADR 실존·상태·label 검증
- `forge/guard/commands.py`: test/lint 명령 실행, timeout, 결과 분류
- `forge/guard/cli.py`: Stop hook, runner, CI, Hermes core가 공유하는 CLI
- `forge/schemas/*.schema.json`: 외부 JSON 계약

검증 결과는 세 종류만 허용한다.

| 결과 | 의미 | 외부 신호 |
|---|---|---|
| PASS | 모든 계약과 증거가 일치 | exit 0 + receipt JSON |
| TESTS_FAILED | 작업 결과나 handoff가 수용 기준을 만족하지 않음 | exit 2 + `TESTS_FAILED:` |
| GATE_ERROR | 검증 장치, 명령 구성, GitHub, 파일 I/O가 신뢰 가능한 판정을 만들지 못함 | exit 2 + `GATE_ERROR:` |

모든 예외는 최상위에서 `GATE_ERROR`로 변환한다. 알 수 없는 오류를 PASS로 바꾸는 fallback은 금지한다.

### 6.2 Codex Stop hook adapter

tracked hook template와 설치 script로 각 task repository의 `.codex/hooks.json`에 공식 `Stop` event를 등록한다. 설치 script는 현재 platform의 Python 3.11+ absolute path와 배포된 adapter absolute path를 기록하므로 `python`/`python3` 명령 이름 차이에 의존하지 않는다. `.codex/ralph-loop.local.json` 같은 runtime state는 계속 git에서 제외한다.

- `TESTS_FAILED`이면 Codex hook 계약의 block 응답을 반환해 같은 thread에서 수정하게 한다.
- `GATE_ERROR`이면 유효한 `continue:false` hook 응답과 machine state를 남겨 모델의 불필요한 수정을 멈추고 runner가 장치 복구를 담당한다.
- `stop_hook_active`를 확인해 재귀 loop를 방지한다.
- stdout에는 hook 계약 JSON만 쓰고 진단은 stderr와 task state log로 분리한다.
- 자동화된 Hermes 실행은 배포된 hook SHA를 먼저 검증한 뒤 Codex의 hook trust 우회 option을 명시적으로 사용한다.

hook이 등록되지 않았거나 신뢰되지 않아 실행을 건너뛰어도 runner의 post-exit 검증이 같은 실패를 잡는다.

### 6.3 Codex task runner

runner는 한 태스크의 유일한 세션 예산 소유자다.

- 태스크 시작 전에 immutable contract와 repository별 baseline을 원자적으로 기록한다.
- Codex thread를 만들기 전에 session slot을 예약해 crash 후 중복 실행을 막는다.
- 고유 thread ID를 최대 4개까지 기록한다.
- worker가 다시 spawn돼도 task ID 기반 state를 재사용하므로 최대 세션 수가 곱해지지 않는다.
- Stop hook이 차단한 동안의 turn은 같은 thread이므로 L0이며 새 session으로 세지 않는다.
- `GATE_ERROR` 뒤에는 같은 thread를 `codex exec resume`으로 재개하거나 외부 검증만 다시 실행한다.
- 실제 task failure로 현재 thread가 끝났을 때만 다음 고유 thread를 만든다.
- 네 번째 thread 실패 뒤에는 `retry_exhausted`를 기록하고 종료한다.

runner state는 temp 파일 작성 후 `fsync`와 atomic replace로 갱신한다. Windows와 POSIX의 file lock 구현 차이는 작은 adapter로 격리한다.

기본 state root는 Windows `%LOCALAPPDATA%\InfinityForge\state`, Linux/VPS `${XDG_STATE_HOME:-~/.local/state}/infinity-forge`다. task contract에 명시된 경로만 사용할 수 있고 모델이 환경 변수로 다른 root를 주입할 수 없다.

기본 예산은 다음과 같다.

- 고유 Codex session: 4
- 전체 task runtime: 60분
- 누적 Codex token: 200,000
- 단일 test/lint command timeout: 15분

예산은 task contract에서 더 낮게 조정할 수 있지만 실행 중 모델이 늘릴 수 없다. Codex JSONL에 token usage가 없거나 파싱할 수 없으면 token cap을 통과한 것으로 간주하지 않고 `GATE_ERROR`로 처리한다.

### 6.4 Hermes completion-policy invariant

Hermes v0.18.2에 최소 carried patch를 적용한다.

- 보호 태스크 생성 시 변경 불가능한 `completion_policy=forge-v1`을 기록한다.
- `complete_task()`는 상태 변경 전에 배포된 trusted verifier를 subprocess로 호출한다.
- verifier 경로와 SHA는 deployment manifest로 고정하고 task worktree의 verifier를 신뢰하지 않는다.
- verifier가 현재 task ID, current run ID, contract hash, handoff digest, repository state digest, receipt version을 다시 확인한다.
- timeout, missing verifier, malformed response, mismatch는 `CompletionPolicyError`로 거절한다.
- 성공 시 core가 trusted verifier 응답으로 최종 receipt digest를 만들고, 같은 DB transaction에서 task metadata와 audit event에 `consumed` 상태로 기록하면서 `done`으로 전이한다.
- 실패 시 task, run, child dependency 상태를 바꾸지 않는다.
- 보호된 완료 결과의 handoff/proof 필드는 사후 편집할 수 없다. 변경하려면 새 검증 run과 receipt가 필요하다.
- raw SQL은 비지원이며 DB mode 600과 운영 계정 분리로 제한한다.

carried patch는 exact upstream commit과 preimage hash가 맞을 때만 적용한다. Windows·Linux·VPS 설치본 각각에 patch check, targeted Hermes test, rollback 절차를 둔다.

trusted verifier release는 task worktree 밖에 설치한다. Windows는 `%LOCALAPPDATA%\InfinityForge\guard\releases\<sha>`, Linux/VPS는 `${XDG_DATA_HOME:-~/.local/share}/infinity-forge/guard/releases/<sha>`를 사용한다. core는 deployment manifest의 path와 artifact hash가 모두 맞을 때만 실행하며, worker가 수정 가능한 worktree copy를 완료 근거로 사용하지 않는다.

### 6.5 Hermes goal mode와 retry backstop

새 executor 카드는 다음 운영 속성을 가진다.

- `tenant=forge`
- `goal_mode=true`
- `goal_max_turns=20`
- `max_runtime=60m`
- `max_retries=4`
- `completion_policy=forge-v1`

`max_retries=4`는 Hermes worker process의 spawn/crash backstop이고 Codex 세션 수의 SoT가 아니다. runner의 persistent session ledger가 전체 worker respawn을 통틀어 고유 Codex thread를 4개로 제한한다.

프로젝트 completion-policy patch는 보호 태스크의 `protocol_violation`을 첫 발생에 sticky `blocked`로 만들고, `recompute_ready`가 이를 자동 재승격하지 못하게 한다.

goal mode task의 합법적인 block은 Hermes 제약에 맞춰 다음처럼 고정한다.

- 외부 의존 대기: `dependency`
- 4회 소진, 장기 GATE_ERROR, 사람 결정 필요: `needs_input`

`capability`, `transient`, 무타입 block 지시는 executor skill에서 제거한다.

### 6.6 완료 투영과 실패 분류

`label-mirror`와 모든 완료 consumer는 raw `status=done`만 신뢰하지 않는다.

- `done`과 유효·소비된 receipt가 함께 있어야 `forge:mergeable` 또는 issue close 후보가 된다.
- receipt가 없거나 불일치하면 projection을 중단하고 `completion_rejected` 감사 이벤트를 만든다.
- Hermes에 `failed` status가 없으므로 `forge:failed`는 `retry_exhausted`, sticky protocol violation, 또는 breaker의 `gave_up` event로 판정한다.
- 같은 event를 다시 처리해도 결과가 변하지 않도록 idempotency key를 사용한다.

### 6.7 독립 dispatcher와 canary

gateway의 embedded dispatcher를 끄고 별도 dispatcher supervisor를 운영한다. 그래야 canary 실패 시 배차만 멈추고 Slack gateway는 계속 살아 있다.

- Linux/VPS: systemd user service가 `hermes kanban daemon`을 소유한다.
- Windows: Scheduled Task로 시작되는 Python supervisor가 daemon child와 PID를 소유한다.
- supervisor는 신선한 canary success marker와 배포 SHA가 일치할 때만 daemon을 실행한다.
- canary 실패는 marker 삭제, dispatcher 정지, 즉시 Slack 알림을 한 동작으로 처리한다.
- 기존 ready 카드도 더 배차되지 않아야 성공으로 본다.

canary는 6시간 주기에 더해 매일 21:00 KST 야간 배차 직전에 반드시 실행한다.

### 6.8 PR CI

같은 verifier와 contract tests를 GitHub Actions에서 실행한다.

trigger는 다음을 포함한다.

- `pull_request`: opened, synchronize, reopened, ready_for_review
- `push`: main
- `merge_group`
- weekly schedule
- `workflow_dispatch`

matrix는 `windows-latest`와 `ubuntu-latest`를 포함한다. Python 계약 테스트는 양쪽에서 실행하고, bash 검사는 Ubuntu, PowerShell 검사는 Windows에서 실행한다.

현재 GitHub 요금제 제약 때문에 required check 자체는 설정할 수 없다. 자동 merge 경로는 check rollup이 존재하고 모두 green일 때만 진행한다. P1 인간 merge는 red 상태에서 금지한다는 운영 규약과 감사로 보완한다.

### 6.9 spec coverage와 drift

spec registry는 `SPEC-NNN`별로 다음을 명시한다.

- plan source 위치와 immutable text hash
- owner GitHub repo와 issue number
- acceptance criteria 목록과 hash
- 관련 PR 목록
- required gate 이름

coverage는 단순 closed issue 수가 아니라 다음을 모두 계산한다.

- 모든 SPEC에 정확히 하나의 canonical issue가 존재한다.
- issue 본문 acceptance criteria hash가 바뀌지 않았다.
- issue가 closed다.
- 연결 PR이 모두 merge됐다.
- required gate와 receipt가 모두 green이다.

전체 술어가 거짓이면 시스템은 `완료`를 보고하지 않는다. 미대응 SPEC은 멱등키로 issue-finder 큐에 재투입한다. GitHub API 실패와 pagination 실패는 0건으로 축소하지 않고 `GATE_ERROR`다.

drift audit는 최소 다음을 검사한다.

- issue와 card의 1:1 매핑
- tracked issue의 forge 상태 label 정확히 1개
- 보호 카드의 goal mode, retry, completion policy
- done 카드의 유효 receipt
- protocol violation과 retry exhausted
- GATE_ERROR 횟수·비율
- issue body/acceptance criteria hash 변경
- canary와 drift 자신의 마지막 성공 시각
- dispatcher, gateway, timer/service active 상태
- 배포 SHA와 artifact hash
- DB mode 600, backup 신선도, outbox, disk 임계

결과는 원자적 JSON state로 남기며 검사 자체가 실패하면 green을 만들지 않는다.

## 7. 데이터 계약

### 7.1 Task contract

task contract는 첫 Codex session 전에 만들어지고 모델이 수정할 수 없는 위치에 저장된다.

필수 내용은 다음과 같다.

- schema version, task ID, run ID
- source issue repo/number/body hash
- stable acceptance criteria ID와 text hash
- 대상 repository 목록
- repository별 path, remote, branch, baseline SHA
- verification command와 timeout
- session/runtime/token budget
- completion policy와 verifier version

D24 멀티 repository 작업은 repository마다 독립 baseline과 PR URL을 가진다. 전체 repository의 gate가 green일 때만 하나의 task receipt가 발급된다.

### 7.2 Handoff

handoff의 필수 필드는 다음과 같다.

- `schema_version`
- `task_id`
- `run_id`
- `pr_urls`
- `changed_files`: repository와 path의 구조화 목록
- `implemented`: acceptance criteria ID와 구현 요약
- `not_implemented`: acceptance criteria ID, 사유, materialization
- `verified_by`: acceptance criteria ID, 실행 command, evidence path

불변식은 다음과 같다.

- `implemented`와 `not_implemented`는 겹치지 않는다.
- 두 목록의 합집합은 수용 기준 전체와 정확히 같다.
- `verified_by`는 모든 implemented ID를 적어도 한 번 덮는다.
- 검증 증거는 실제로 실행된 command 결과와 일치한다.
- `not_implemented`는 빈 배열을 명시할 수 있다.
- 비어 있지 않은 각 잔여는 GitHub issue 또는 `forge:adr` issue를 참조한다.
- card-only 잔여는 원격 CI가 검증할 수 없으므로 금지한다. 필요하면 canonical issue에서 child card를 파생한다.
- worker가 source issue 본문이나 acceptance criteria를 수정해 계약을 약화할 수 없다.

### 7.3 Receipt

receipt는 다음 값에 결합된다.

- verifier schema/version과 deployed SHA
- task ID와 current run ID
- task contract digest
- handoff digest
- repository별 baseline, HEAD, tree/working diff digest
- verification command, exit code, output digest
- GitHub reference verification result
- Codex thread ID 목록과 누적 token/runtime
- issued/expiry timestamp

최종 receipt는 Hermes completion-policy gate가 현재 상태를 재검증한 실행에서만 소비된다. 오래됐거나 한 필드라도 달라진 receipt는 재사용할 수 없다.

## 8. 오류 처리 상태기계

```text
PREPARE
  -> reserve unique session slot
  -> CODEX_RUNNING

CODEX_RUNNING
  -> Stop / TESTS_FAILED: same thread continuation (L0)
  -> Stop / GATE_ERROR: runner recovery, same thread retained
  -> process failure with remaining slots: fresh thread (L1)
  -> process failure at 4 threads: RETRY_EXHAUSTED
  -> verifier PASS: RECEIPT_READY

RECEIPT_READY
  -> Hermes completion policy reverify PASS: DONE
  -> TESTS_FAILED: same/fresh thread according to state
  -> GATE_ERROR: completion held, alert, no new thread count

RETRY_EXHAUSTED
  -> sticky blocked(needs_input)
  -> forge:failed projection + immediate alert
```

GitHub 장애는 시스템 전체 성공으로 위장하지 않는다. 해당 태스크의 reference 검증은 `GATE_ERROR`로 보류하고 나머지 독립 태스크 배차는 계속한다. 따라서 기존 문서의 “GitHub 장애에도 밤 완주”는 “로컬 작업은 계속되지만 GitHub 의존 완료는 지연”으로 새 결정에서 정정한다.

## 9. 테스트 전략

모든 production 변경은 실패하는 테스트를 먼저 추가한 뒤 구현한다.

### 공통 verifier

- handoff 없음, malformed JSON, 잘못된 타입
- 빈 필수 값, 수용 기준 누락·중복·겹침
- implemented 대비 verified_by 누락
- 존재하지 않거나 잘못된 상태의 issue
- `forge:adr` label 없는 ADR 참조
- card-only 잔여 거절
- commit 후 clean worktree의 정상 변경
- 실행 전 dirty 변경만 있는 작업 거절
- handoff-only 변경 거절
- 단일·다중 repository baseline과 PR 조합
- test failure와 command-not-found/GitHub failure 신호 분리
- token/runtime/session budget

### Hook과 runner

- Stop hook의 block JSON과 재귀 방지
- hook이 실행되지 않아도 post-exit gate가 차단
- crash 전 session slot 예약의 멱등성
- Hermes worker respawn 뒤에도 고유 Codex thread 최대 4개
- GATE_ERROR가 새 thread를 만들지 않음
- 네 번째 task failure만 retry exhausted
- timeout과 resume 경로

### Hermes completion policy

- tool, CLI, Dashboard 완료가 receipt 없으면 모두 거절
- 거절 시 task/run/event/child 상태 불변
- 올바른 receipt의 1회 소비와 done 전이 원자성
- stale/mismatched/replayed receipt 거절
- proof 사후 편집 거절
- 보호되지 않은 일반 Hermes task의 기존 동작 보존
- protocol violation 첫 발생 sticky block
- carried patch exact-version check와 rollback

### 운영 자동화

- label mirror의 goal/retry/policy 생성 인자
- `gave_up`/`retry_exhausted` 기반 forge:failed 투영
- canary 실패가 Windows/Linux dispatcher를 실제 중단
- gateway와 Slack은 dispatcher 중단 중에도 active
- drift의 API/SQLite/systemd 오류 fail-loud
- spec pagination, duplicate mapping, body edit, red gate
- workflow trigger와 Windows/Ubuntu matrix 계약

## 10. 배포와 롤백

### 순서

1. Windows와 Ubuntu CI matrix에서 모든 테스트를 통과시킨다.
2. PR check rollup이 존재하고 green인 exact commit SHA를 확정한다.
3. Windows 로컬, 일반 Linux staging, VPS 순으로 같은 SHA를 설치한다.
4. 각 대상에서 기존 artifact와 Hermes install 상태를 snapshot한다.
5. verifier와 core patch를 staging 경로에서 검증한 뒤 원자 교체한다.
6. embedded dispatcher를 끄고 독립 supervisor/service를 설치한다.
7. active task와 tmux가 0인지 확인한 뒤 gateway를 graceful restart한다.
8. canary, drift, DB quick check, service/timer, deployed SHA를 확인한다.
9. 신규 격리 issue/card/PR로 정상 E2E를 실행한다.
10. invalid handoff와 receipt 없는 complete는 deterministic fixture로 live rejection을 확인한다.

VPS gateway는 raw `systemctl restart`가 아니라 Hermes의 drain-aware `hermes gateway restart`를 사용한다. Windows gateway가 내려가 있으면 start, 실행 중이면 graceful restart를 선택한다.

### 롤백 조건

다음 중 하나면 배포를 실패로 판정한다.

- gateway/dispatcher가 60초 안에 active가 되지 않음
- 반복 crash 또는 protocol violation 발생
- canary, drift, contract smoke 중 하나라도 실패
- receipt 없는 완료가 accepted/projected됨
- DB quick check 실패
- 배포 SHA나 artifact hash 불일치

rollback은 dispatcher 정지 유지, gateway drain/stop, 이전 artifact와 Hermes patch 복원, daemon reload, gateway start, DB quick check, canary 통과 순으로 수행한다. 코드 rollback만으로 DB snapshot을 복원하지 않으며 DB 손상 때만 별도 복원한다.

## 11. 문서 결정 추가

구현 시 `docs/plan.md`의 기존 D번호를 소급 수정하지 않고 다음 새 결정을 추가한다.

- D25: 기존 core 무수정 선호보다 완료 원자성이 우선한다. Forge 보호 태스크의 `complete_task()`는 최소 carried patch로 receipt를 원자적으로 검증·기록한다.
- D26: runner ledger가 최대 4개 고유 Codex session의 유일한 SoT이며 Hermes retry는 process backstop이다.
- D27: 잔여의 canonical 원격 SoT는 GitHub issue 또는 `forge:adr` issue이며 card-only 잔여를 금지한다.
- D28: GitHub 장애는 해당 태스크의 `GATE_ERROR`로 완료를 지연하되 독립 태스크는 계속한다.
- D29: gateway와 dispatcher를 분리하고 canary가 dispatcher만 fail-closed로 제어한다.
- D30: private/free GitHub에서는 Actions 실행·자동 merge 차단까지만 보장하며 platform required check는 요금제 변경 뒤 활성화한다.

## 12. 완료 수용 기준

다음 항목이 모두 증거로 확인돼야 이 작업을 완료로 선언한다.

1. Windows와 Ubuntu test matrix가 green이다.
2. 실제 Codex Stop hook이 TESTS_FAILED를 같은 thread에 재주입한다.
3. hook 미실행 상태에서도 runner post-gate가 종료를 막는다.
4. handoff 없는 완료가 tool, CLI, Dashboard 경로에서 모두 거절된다.
5. 유효한 handoff와 receipt만 원자적으로 done이 된다.
6. 잔여 없는 경우 빈 배열, 잔여 있는 경우 실존 issue/ADR가 강제된다.
7. commit된 clean 변경과 다중 repository 변경을 정확히 검증한다.
8. 최대 4개의 고유 Codex thread가 worker respawn 전체에 걸쳐 유지된다.
9. GATE_ERROR가 task failure session을 소비하지 않는다.
10. goal mode와 typed block 규칙이 신규 카드에 적용된다.
11. receipt 없는 done이 GitHub/spec 완료로 투영되지 않는다.
12. PR workflow가 pull request에서 실제 check를 생성한다.
13. canary 실패 시 Windows/Linux/VPS dispatcher가 중단되고 gateway는 유지된다.
14. drift가 protocol violation, GATE_ERROR, issue edit, receipt, 배포 SHA를 검사한다.
15. spec coverage가 M/M, issue close, PR merge, gate green을 별도로 보고한다.
16. exact-SHA 배포와 rollback rehearsal이 성공한다.
17. 신규 격리 issue/card/PR E2E가 정상 완료된다.
18. 실패 fixture E2E가 완료·투영되지 않는다.
19. 로컬과 VPS의 DB 권한과 service/timer manifest가 검증된다.
20. credential 값이 새 파일, commit, CI artifact, Slack에 포함되지 않는다.

이 중 하나라도 증거가 없거나 간접적이면 Ralph Loop의 completion promise를 출력하지 않는다.
