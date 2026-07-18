# Task 6 구현 보고서 — v2 request·settings 순수 데이터 계약

## 결과

- `TaskRequestV2`와 `TaskSettingsV2`를 각각 정확한 17개 field의 frozen/slots dataclass로
  추가했다.
- `create`, class `from_json`, `to_json`과 strict parser wrapper, canonical hash helper를
  제공한다.
- request hash는 `request_hash`와 `status`, settings hash는 `task_settings_hash`와 `status`를
  제외한 key-sorted compact UTF-8 JSON의 SHA-256이다.
- host별 absolute workspace와 무관한 고정 preimage로 v2 request/settings digest literal을
  각각 고정했고, 기존 v1 content/settings/outbox digest도 literal 회귀로 고정했다.
- 모든 nested object의 duplicate JSON key와 root/nested missing·extra field를 거부한다.
- UUID, lowercase SHA-256, canonical `OWNER/REPO`, RFC 3339 UTC `Z`, positive parent Issue를
  exact type으로 검증한다.
- Project는 `(repository, workspace, base_branch)`로 정렬해 생성하며 parser는 정렬되지 않은
  record를 거부한다. 빈 목록, case-insensitive 중복 repository, owner-host 불일치도 거부한다.
- multi-Project `full_auto`에서만 모든 `project_id`의 exact permutation을 요구하고 나머지
  조합은 `merge_order=null`만 허용한다.
- v1과 같은 자동 merge 만료 규칙(null/필수, confirmed 이후, 최대 12시간)을 적용한다.
- settings 생성과 parsing 모두 request와 공유하는 13개 field를 exact 비교한다.
- v1 `TaskCreationRequest`, `TaskSettings`, DB, service, outbox에는 연결하거나 수정하지 않았다.

## TDD 증거

### RED

지정 venv로 선행 v2 test를 실행해 production module 부재를 확인했다.

```text
C:\01.project\.codex-venvs\infinity-forge-tests\Scripts\python.exe -m pytest tests/ops/test_task_settings_v2.py -q
ModuleNotFoundError: No module named 'forge.ops.task_settings_v2'
1 error in 0.28s
```

### GREEN

```text
C:\01.project\.codex-venvs\infinity-forge-tests\Scripts\python.exe -m pytest tests/ops/test_task_settings_v2.py -q
51개 v2 contract test 통과
```

v1 보존과 인접 service/outbox 회귀:

```text
C:\01.project\.codex-venvs\infinity-forge-tests\Scripts\python.exe -m pytest tests/ops/test_plain_names.py tests/ops/test_task_settings.py tests/ops/test_task_service.py tests/ops/test_task_outbox.py -q
92 passed, 3 skipped in 14.43s
```

TaskProject binding 회귀를 함께 실행한 최종 commit gate:

```text
107 passed in 1.15s
```

## 품질 검증

- 지정 venv `py_compile`: 통과
- `uv run --with ruff ruff check`: 통과
- `git diff --check`: 통과

지정 pytest venv에는 Ruff module이 없어 Ruff만 격리된 `uv` 실행기로 검증했다.

## 위험 경계

- `RISK(breaking)`: 두 record의 17-field schema, 정렬 key 또는 hash preimage를 바꾸면 durable
  v2 identity가 바뀐다.
- 이 Task는 순수 data contract만 추가한다. v2 persistence와 service wiring은 뒤 Task에서
  별도 migration과 함께 수행해야 한다.
