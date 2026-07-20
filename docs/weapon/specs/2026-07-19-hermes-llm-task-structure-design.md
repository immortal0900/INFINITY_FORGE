# Hermes 최상위 LLM Task 구조화 설계

## 목적

Hermes가 첫 사용자 입력을 Task로 전환할 때 Markdown 목록 기호만으로
`Acceptance Criteria`를 만들지 않는다. Hermes의 공식 Plugin LLM Access를 통해 전용
최상위 모델이 원문의 의미를 분류하고, Infinity Forge의 결정적 검증기가 모델 결과를
검사한 뒤, 사용자가 미리보기에서 수정하거나 Confirm한 내용만 Task 계약으로 고정한다.

현재 결함에서는 긴 인수인계 문서의 하이픈 목록 139개와 숫자 목록 27개 중 Markdown
섹션 제목 8개를 제외한 158개가 모두 `Acceptance Criteria`로 평탄화됐다. 상태, 제약,
완료 기록, 설계 대안, 테스트 증거, 다음 작업이 서로 구분되지 않았다. 이 숫자에는
도메인 근거가 없고 다음 정규식의 문법 일치만 근거로 사용됐다.

```text
139 + 27 - 8 = 158
```

새 설계는 코드가 목록 후보를 찾는 것과 목록의 의미를 결정하는 것을 분리한다. 코드는
원문 위치와 후보 ID만 만든다. 의미 분류와 완료 조건 초안은 최상위 LLM이 담당한다.
코드는 LLM 결과의 형식, 전체 후보 coverage, 중복, 모순, 모델 identity를 검증한다.

## 확정된 사용자 결정

1. 자유 형식 Task 설명은 가장 높은 등급의 LLM이 구조화한다.
2. LLM이 최종 Task 계약을 임의로 확정하지 않는다.
3. 처리 순서는 `LLM 구조화 → 코드 검증 → 사용자 수정·확정`이다.
4. 현재 전용 모델은 `openai-codex`의 `gpt-5.6-sol`로 고정한다.
5. 모델을 자동으로 “가장 최신”이라고 추정하지 않는다. 모델 교체는 배포 설정의
   allowlist와 검증 기대값을 명시적으로 갱신한다.
6. 구조화 실패 시 기존 정규식이나 원문 전체를 Acceptance Criteria로 사용하는 fallback을
   금지한다.
7. Chat 경로, `task_flow`, `merge_mode`, merge 안전 판정에는 새 LLM 분류를 사용하지 않는다.

## 검토한 접근

### 1. LLM이 Acceptance Criteria만 반환

출력과 구현이 작지만 원문 목록 중 무엇을 제외했는지 검증할 수 없다. 중요한 요구사항이
누락돼도 코드가 알 수 없으므로 기각한다.

### 2. LLM이 원문 전체를 새 명세 문서로 재작성

읽기는 편하지만 원문과 결과의 정확한 대응이 사라진다. 모델이 상태나 제약을 삭제하거나
새 요구를 추가했는지 판정하기 어렵고, 큰 출력 때문에 비용과 실패 표면도 커져 기각한다.

### 3. 목록 후보 전수 분류 + Acceptance Criteria 합성 — 채택

코드가 원문의 목록 후보에 `I001`, `I002` 같은 안정 ID를 붙인다. LLM은 전체 원문과 후보
목록을 함께 보고 각 후보를 정확히 한 의미 범주에 배정한다. 완료 조건은 관련 후보 ID와
원문 줄 번호를 근거로 별도 합성한다. 코드가 후보 집합의 exact coverage를 검증할 수 있고,
원문은 byte 단위로 보존된다.

## 공식 Hermes 확장 경계

공급자 SDK나 인증 토큰을 Infinity Forge가 직접 다루지 않는다. Hermes v0.18.2의 공식
[`Plugin LLM Access`](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/plugin-llm-access.md)를
사용한다.

```python
ctx.llm.complete_structured(
    instructions=...,
    input=[{"type": "text", "text": numbered_source}],
    json_schema=TASK_STRUCTURE_SCHEMA,
    schema_name="forge.task-structure.v1",
    provider="openai-codex",
    model="gpt-5.6-sol",
    temperature=0.0,
    max_tokens=8192,
    timeout=300,
    purpose="infinity-forge.task-structure.v1",
)
```

이 API는 Hermes가 provider resolution, OAuth·credential pool, JSON 요청, schema validation,
감사 로그를 소유하게 한다. Infinity Forge는 prompt, schema, 결과의 추가 검증과 오류 처리를
소유한다. Hermes의 선택적 `jsonschema` 설치 여부에 의존하지 않고 Forge 검증기가 같은
불변식을 다시 검사한다.

공식 API가 provider fallback을 수행할 수 있으므로 반환된 `result.provider`와
`result.model`이 각각 `openai-codex`, `gpt-5.6-sol`과 일치하지 않으면 결과를 폐기한다.
낮은 모델의 fallback 결과를 Task 계약으로 사용하지 않는다.

## 데이터 계약

### 입력 후보

원문은 수정하지 않고 줄 번호를 붙여 LLM에 전달한다. 기존 목록 정규식은 Acceptance
Criteria 생성기가 아니라 후보 열거기로 역할을 축소한다.

```text
I001 | line 7 | indent 0 | - Hermes 실행 직후 빈 화면에서는 chooser가 뜨지 않음
I002 | line 8 | indent 0 | - 첫 메시지를 보내면 ...
I003 | line 12 | indent 2 |   - Projects
```

후보에는 다음을 보존한다.

- 안정 후보 ID
- 원문 줄 번호
- 들여쓰기 깊이
- 원문 text
- 가장 가까운 상위 Markdown heading

heading과 전체 원문도 LLM 입력에 포함하되, heading 자체를 목록 후보로 세지 않는다.

### 구조화 출력

출력 format은 `forge-task-structure/v1`로 고정한다.

```json
{
  "format_version": "forge-task-structure/v1",
  "title": "Task 제목",
  "acceptance_criteria": [
    {
      "id": "AC-01",
      "text": "첫 메시지를 보존한 채 Chat 또는 Task를 선택할 수 있다.",
      "verification": "CLI/TUI/Desktop/Slack chooser smoke가 통과한다.",
      "source_item_ids": ["I001", "I002"],
      "source_lines": [7, 8]
    }
  ],
  "classification": {
    "acceptance_source_items": ["I001", "I002"],
    "constraints": [],
    "current_state": [],
    "completed_work": [],
    "remaining_work": [],
    "alternatives": [],
    "verification_evidence": [],
    "context": []
  },
  "conflicts": []
}
```

분류 범주의 의미는 다음과 같다.

| 범주 | 포함 내용 |
|---|---|
| `acceptance_source_items` | 최종 결과로 관찰·검증해야 하는 요구사항의 근거 |
| `constraints` | 금지사항, 호환성, 유지 조건 |
| `current_state` | 브랜치, commit, 배포, 실행 상태 |
| `completed_work` | 이미 구현·검토된 내용 |
| `remaining_work` | 앞으로 수행할 작업과 순서 |
| `alternatives` | 선택되지 않았거나 서로 배타적인 설계안 |
| `verification_evidence` | 이미 실행한 test와 관측 증거 |
| `context` | 파일 경로, 환경, 설명용 참고 정보 |

### 결정적 검증 불변식

1. `format_version`은 exact `forge-task-structure/v1`이다.
2. title은 공백이 아니며 UTF-8 기준 256자 이하이다.
3. Acceptance Criteria는 1개 이상 64개 이하이다.
4. AC ID는 `AC-01`부터 중복 없이 연속된다.
5. AC text와 verification은 공백이 아니며 동일 text가 중복되지 않는다.
6. 모든 후보 ID는 정확히 한 분류 배열에 존재한다.
7. 분류에 알 수 없는 ID, 중복 ID, 누락 ID가 없다.
8. `acceptance_source_items`는 모든 AC의 `source_item_ids` 합집합과 exact-equal이다.
9. 모든 `source_lines`는 실제 원문 범위 안이며 해당 후보의 원문 줄과 일치한다.
10. `conflicts`가 비어 있지 않으면 Confirm 화면으로 진행하지 않는다.
11. 반환 provider/model이 고정한 최상위 모델과 exact-equal이다.
12. 원문 description은 어떤 LLM 출력으로도 교체·요약하지 않는다.

## 구성요소와 책임

### `forge/ops/task_content_structurer.py`

- 후보 목록과 heading context를 결정적으로 생성한다.
- `TaskContentStructurer` protocol과 구조화 결과 dataclass를 정의한다.
- JSON Schema와 prompt version을 단일 진실 공급원으로 둔다.
- LLM 결과를 검증하고 기존 `TaskContent`와 preview classification으로 변환한다.
- provider/model identity mismatch와 schema·coverage 오류를 명시적 예외로 반환한다.

### `forge/ops/task_setup.py`

- `TaskContentStructurer`를 constructor dependency로 받는다.
- 네트워크 모델 호출을 `RLock` 밖에서 실행하는 `_TaskStructuringWork`로 만든다.
- operation token, generation, prompt ID, expiry를 재검증해 취소·만료된 결과를 적용하지 않는다.
- 성공한 `TaskContent`를 draft에 한 번만 저장하고 preview와 Confirm이 같은 객체를 사용한다.
- LLM을 Confirm 시 다시 호출하지 않는다.
- 구조화 실패·모순·수정 선택을 위한 state와 chooser를 소유한다.

### `forge/hermes_plugin/infinity_forge/__init__.py`

- `register(ctx)`에서 공식 `ctx.llm`을 받아 production structurer를 조립한다.
- main/default profile에만 structurer를 설치한다.
- test에서는 fake structurer를 주입할 수 있고 실제 네트워크를 호출하지 않는다.
- 기존 Chat·Stop·Forge Tool 경로는 변경하지 않는다.

### `forge/ops/hermes_llm_policy.py`

- Hermes의 `load_config()`와 `save_config()`로 plugin LLM trust policy를 적용한다.
- main profile에만 exact provider/model override allowlist를 설치한다.
- worker profile에는 Infinity Forge LLM override가 없음을 검증한다.
- config backup, rollback, readback을 제공한다.

## Hermes trust policy

plugin manifest의 이름 `infinity-forge`를 config key로 사용한다.

```yaml
plugins:
  entries:
    infinity-forge:
      llm:
        allow_provider_override: true
        allowed_providers:
          - openai-codex
        allow_model_override: true
        allowed_models:
          - gpt-5.6-sol
        allow_agent_id_override: false
        allow_profile_override: false
```

배포는 Windows, EC2, VPS에서 같은 설정을 설치하고 Hermes API로 다시 읽어 exact-equal을
검증한다. 기존 operator 설정의 관련 없는 key는 보존한다. apply 중 하나라도 실패하면
backup으로 원복하고 readback까지 확인한다.

## 사용자 흐름

정상 흐름은 다음과 같다.

```text
첫 입력 보존
  → Chat / Task
  → Projects
  → task_flow
  → merge_mode
  → 필요할 때 merge_order
  → 최상위 LLM 구조화(외부 write 0회)
  → Forge schema·coverage·model 검증
  → 분류된 미리보기
     ├─ Confirm Task
     ├─ Revise Task
     └─ Cancel
```

미리보기에는 다음을 표시한다.

- 고정 모델과 schema version
- 원문 title과 description
- Acceptance Criteria
- 범주별 항목 수와 원문 항목
- 발견된 conflict
- 선택한 checks와 merge mode

`Revise Task`는 새 Task text 입력 단계로 이동해 LLM 구조화를 다시 실행한다. 이전 구조화
결과와 Confirm prompt는 폐기한다. `Cancel`은 Task draft를 폐기한다.

## 실패 처리

| 실패 | 동작 |
|---|---|
| provider 인증 없음, 429·5xx, timeout | Task 생성 0회, `Retry` 또는 `Continue in Chat` 표시 |
| JSON parse·schema 실패 | 원문·raw 응답을 복제하지 않고 오류 종류와 audit ID만 기록 |
| 후보 coverage 누락·중복 | Confirm 차단, Retry 또는 Revise 표시 |
| 낮은 모델 fallback | 결과 폐기, 최상위 모델로 Retry |
| conflict 존재 | Confirm 차단, 원문과 conflict를 표시하고 Revise 요구 |
| 사용자가 구조화 중 Cancel | 늦게 도착한 결과를 token/generation mismatch로 폐기 |
| chooser timeout | 결과와 draft 폐기, 외부 write 0회 |

오류 시 다음 fallback을 금지한다.

- 모든 Markdown bullet을 Acceptance Criteria로 사용
- 원문 전체를 하나의 Acceptance Criterion으로 사용
- 빈 Acceptance Criteria로 진행
- 낮은 모델 결과를 경고 없이 수용
- 구조화 실패 뒤 GitHub Issue나 Kanban card 생성

## 저장과 hash

원문 `description`과 승인된 `title`, `acceptance_criteria`만 기존 `TaskContent`에 저장한다.
분류 결과는 확인용이며 원문의 대체 저장소가 아니다. 기존 `task_content_hash` 공식은
변경하지 않는다. 따라서 v1/v2 DB schema migration 없이 기존 worker, issue body, merge
barrier가 확정한 Task 계약을 읽을 수 있다.

Hermes의 Plugin LLM audit에는 plugin ID, purpose, provider, model, token usage가 남는다.
Forge 로그에는 request ID가 만들어지기 전이므로 session key의 digest, prompt/schema
version, 결과 hash, provider/model만 남기고 원문과 raw 모델 응답은 기본 로그에 복제하지
않는다.

## 테스트 전략

구현은 test-first로 진행한다.

### 구조화 단위 테스트

1. 158개 목록 fixture를 넣어 모든 후보가 정확히 한 범주에 배정되는지 검증한다.
2. 상태·완료 기록·경로·테스트 증거·서로 배타적인 대안이 AC가 되지 않는지 검증한다.
3. 후보 하나 누락, 후보 중복, 알 수 없는 ID를 각각 거절한다.
4. 중복 AC, 빈 verification, 65개 AC를 거절한다.
5. conflict가 있으면 Confirm 가능한 `TaskContent`를 만들지 않는다.
6. 원문 description의 byte가 구조화 전후 동일한지 검증한다.

### Task setup 상태 테스트

7. LLM 호출 동안 다른 session의 chooser lock이 막히지 않는지 검증한다.
8. Cancel·timeout 뒤 도착한 구조화 결과가 draft에 적용되지 않는지 검증한다.
9. 성공 결과를 preview와 Confirm이 공유하고 LLM 호출이 정확히 1회인지 검증한다.
10. Revise가 이전 결과와 prompt를 폐기하고 새 원문으로 정확히 1회 재호출하는지 검증한다.
11. 구조화 오류에서 GitHub·Kanban callback이 0회인지 검증한다.
12. v1과 v2 Project 흐름이 같은 structurer 계약을 사용하는지 검증한다.

### Plugin·배포 테스트

13. `register(ctx)`가 main/default에서만 fake `ctx.llm.complete_structured`를 연결한다.
14. provider/model override 인자가 official API 계약과 exact match인지 검증한다.
15. 반환 provider 또는 model이 다르면 결과를 거절한다.
16. main config의 trust allowlist apply·verify·backup·rollback을 검증한다.
17. worker profile에 override가 남으면 배포 검증을 실패시킨다.
18. Windows PowerShell과 Linux/VPS shell 배포가 같은 policy module을 호출하는지 검사한다.

실제 provider smoke는 테스트 fixture가 아니라 배포 단계에서 1회 수행한다. 원문에 실제
상태·제약·대안·증거를 섞은 Task를 보내고, preview 분류와 model audit을 확인한 뒤 Cancel해
외부 write 0회를 검증한다. 그 다음 최소 Task를 Confirm해 중앙 Issue와 선택 Project 흐름을
검증한다.

## 호환성과 범위 제외

- `TaskContent`, v1/v2 request, DB schema, hash field는 변경하지 않는다.
- `task_flow`, `merge_mode`, merge safety는 계속 결정적 코드가 판정한다.
- 일반 Chat에는 구조화 LLM을 호출하지 않는다.
- worker prompt와 worker model 선택은 이번 범위에서 변경하지 않는다.
- Hermes core나 설치 checkout을 수동 편집하지 않는다.
- LLM이 Project, branch, repository, user identity를 생성하거나 권한 근거로 사용하지 않는다.
- provider/model 교체 UI는 이번 범위에 포함하지 않는다.

## 단계별 적용과 장기 결과

1. 구조화 schema와 검증기를 먼저 추가하고 기존 158개 재현 fixture로 RED를 확인한다.
2. TaskSetup에 외부 작업 seam과 stale-result guard를 연결하되 production plugin에는 아직
   연결하지 않는다. focused test로 동시성과 재호출 1회를 확인한다.
3. plugin의 `ctx.llm.complete_structured`를 연결하고 fake context test를 통과시킨다.
4. 세 환경의 trust policy 배포·rollback·readback을 추가한다.
5. Release A에서 policy와 코드를 배포하되 Task 생성은 꺼둔 채 구조화 Cancel smoke를 한다.
6. Release B에서 Task 생성을 켜고 minimal manual Task 뒤 기존 checks·merge 조합을 검증한다.

이 순서는 LLM 구조화가 실패해도 외부 write 전에 중단되게 한다. 6개월 뒤 모델이 교체되면
schema와 prompt version은 유지한 채 provider/model allowlist와 golden fixture 평가만
갱신한다. 모델 품질이 떨어지거나 provider가 중단돼도 Task 생성이 멈출 뿐 기존 Chat과
실행 중 Task는 계속 동작한다. 분류 결과를 DB schema에 넣지 않으므로 rollback은 plugin과
config를 이전 release로 되돌리는 것으로 끝난다.

## Acceptance Criteria

1. 긴 Task 입력의 목록 항목을 문법만으로 Acceptance Criteria로 만들지 않는다.
2. production 구조화는 Hermes 공식 `ctx.llm.complete_structured`만 사용한다.
3. provider/model은 `openai-codex`와 `gpt-5.6-sol`로 고정하고 다른 결과를 거절한다.
4. 모든 목록 후보가 정확히 한 의미 범주에 배정되지 않으면 Confirm을 차단한다.
5. LLM 결과가 invalid, incomplete, contradictory이면 외부 write가 0회이다.
6. 구조화 실패 시 정규식·원문 전체·낮은 모델로 fallback하지 않는다.
7. 사용자는 Confirm 전에 Acceptance Criteria와 범주별 분류를 볼 수 있다.
8. 사용자는 Revise로 원문을 바꾸고 구조화를 다시 실행할 수 있다.
9. 구조화 중 lock이 다른 session의 chooser를 막지 않는다.
10. Cancel·timeout·새 generation 뒤 도착한 결과를 적용하지 않는다.
11. preview와 Confirm은 한 번 구조화한 exact `TaskContent`를 공유한다.
12. 원문 description과 기존 task content hash 공식은 변경하지 않는다.
13. Chat, Stop, task_flow, merge_mode, merge safety 동작은 기존과 같다.
14. Windows, EC2, VPS의 main profile에 같은 trust allowlist가 있고 worker profile에는 없다.
15. 설정 적용 실패는 backup 복원과 readback을 마친 뒤 실패로 끝난다.
16. 158개 재현 fixture, schema·coverage·conflict·stale result·배포 policy test가 통과한다.

## 변경이력

- 2026-07-19 | 최상위 LLM Task 구조화 사용자 확정 | 변경: Markdown 목록 전역 추출을
  후보 전수 분류와 AC 합성으로 교체하고, `openai-codex/gpt-5.6-sol`, Hermes 공식
  Plugin LLM Access, deterministic coverage 검증, Revise·Confirm, fail-closed 오류 계약을
  설계로 고정 | 이유: 인수인계 문서의 목록 158개가 의미 구분 없이 Acceptance Criteria가
  된 실제 재현 문제를 제거 | 검증: 첨부 원문의 158개 산출식, 현재 `task_setup.py`의
  lock 밖 callback pattern, Hermes v0.18.2 `plugin-llm-access.md`, `PluginContext.llm`,
  `PluginLlm.complete_structured` 구현을 대조해 작성; 제품 코드는 아직 검증하지 않음
