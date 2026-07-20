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

## 독립 리뷰 수정

- `TaskSettingsV2`의 17개 저장 field는 그대로 유지하고 mandatory
  `InitVar[TaskRequestV2]`를 추가했다. 직접 constructor, `create`, `from_json`,
  `dataclasses.replace`가 모두 exact request identity를 확인한다. InitVar는 `fields`, slots,
  repr, JSON, hash preimage에 포함되지 않는다.
- Task content의 title·description·acceptance criteria와 `confirmed_by`에 lone UTF-16
  surrogate가 있으면 create/parser 모두 `TaskSettingsV2Error`로 거부한다. 유효한 한글,
  emoji, NFC, NFD, 명시 U+FFFD는 변환 없이 UTF-8 JSON roundtrip한다.
- `TaskProject.from_mapping`은 exact 7-field schema, canonical absolute normalized path text,
  six-field `project_id` binding을 확인하되 저장 후 삭제된 workspace도 읽는다. 반면
  `TaskProject.create`, 직접 constructor, `TaskRequestV2.create`, 직접 request constructor는
  workspace 존재를 다시 확인한다. 따라서 복구 읽기는 가능하지만 새 Task 생성 우회는
  불가능하다.
- request/settings JSON parser는 expected data-format error를 내부 result로 바꾼 뒤 public
  boundary에서 raw/payload/request 참조를 지우고 새 sanitized error를 발생시킨다. 직접 class
  parser와 compatibility wrapper 모두 duplicate key, credential repository, content/project
  오류에서 cause/context와 traceback frame local에 원문 secret을 남기지 않는다. 예상하지
  못한 loader/nested parser 예외는 변환하지 않고 전파한다.

### 리뷰 RED

```text
python -m pytest tests/ops/test_task_settings_v2.py tests/ops/test_task_projects.py -q
19 failed, 111 passed
```

### 리뷰 GREEN

```text
python -m pytest tests/ops/test_task_settings_v2.py tests/ops/test_task_projects.py tests/ops/test_project_discovery.py -q
197 passed in 3.34s

python -m pytest tests/ops/test_plain_names.py tests/ops/test_task_settings.py tests/ops/test_task_service.py tests/ops/test_task_outbox.py -q
92 passed, 3 skipped in 10.16s
```

## 두 번째 독립 리뷰 수정

- Windows의 durable workspace text도 live `Path.resolve(strict=True)` 결과가 만들 수 있는
  표기만 허용한다. 로컬 drive letter는 ASCII 대문자만 허용하고, 경로 component의 C0
  control, `< > : " | ? *`, 끝의 점·공백, Win32 예약 device basename을 거부한다.
  `CONIN$`·`CONOUT$`, `CON.txt`, `COM1`~`COM9`, `LPT1`~`LPT9`도 포함한다.
- 위 규칙은 `os.name == "nt"`에서만 적용한다. U+007F와 U+FFFD는 Windows에서도 허용하고,
  POSIX에서는 실제 생성 가능한 newline component를 live create와 삭제 후 stored parse로
  보존한다. durable validator는 `resolve(strict=False)`로 사라진 경로를 추측하지 않는다.
- JSON decoder에 safe `parse_int`를 연결했다. 정상 integer는 그대로 `int`가 되고 Python의
  digit limit을 넘는 integer는 입력 예외를 parser 밖으로 내보내지 않는 private sentinel이
  된다. iterative scan이 sentinel을 찾으면 request/settings의 class parser와 compatibility
  wrapper 모두 sanitized `TaskSettingsV2Error`를 반환한다. monkeypatched `json.loads`의 예상
  밖 `ValueError`는 계속 원형 전파한다.
- `TaskSettingsV2.create`는 positive issue number가 현재 Python JSON integer로 표현 가능한지
  hash 생성 전에 확인한다. `10**5000` 같은 값은 native `ValueError`나 원문 integer를
  traceback에 남기지 않고 `TaskSettingsV2Error`로 거부한다.

### 두 번째 리뷰 RED

```text
Windows impossible workspace attack: 38 failed, 1 passed, 1 skipped
5000-digit JSON/create attack: 5 failed
```

### 두 번째 리뷰 GREEN

```text
python -m pytest tests/ops/test_task_settings_v2.py tests/ops/test_task_projects.py tests/ops/test_project_discovery.py -q
241 passed, 1 skipped in 3.26s

python -m pytest tests/ops/test_plain_names.py tests/ops/test_task_settings.py tests/ops/test_task_service.py tests/ops/test_task_outbox.py -q
92 passed, 3 skipped in 10.49s
```

## 세 번째 독립 리뷰 수정

- `TaskSettingsV2`의 generated dataclass initializer가 직접 constructor에 전달된
  `10**5000` 값을 실패 traceback frame에 붙잡던 문제를 없앴다. 사용자 정의 initializer가
  값을 어떤 field에도 저장하기 전에 JSON 표현 가능 여부를 확인하고, 실패 전에 frame local도
  지운다.
- initializer만 명시적으로 구현하고 dataclass의 정확한 17개 저장 field, `frozen`, `slots`,
  repr/equality, mandatory `InitVar request`, JSON/hash preimage는 유지했다. 공개 constructor의
  18개 positional-or-keyword parameter 순서와 `dataclasses.replace(..., request=request)`도
  Python 3.11과 3.13에서 회귀 고정했다. `replace(settings)`의 버전별 예외 차이는
  `(TypeError, ValueError)` 계약으로 표현한다.
- Win32가 ASCII 숫자 장치명과 동일하게 예약하는 superscript 변형 `COM¹`~`COM³`,
  `LPT¹`~`LPT³`을 기존 lexical validator에 추가했다. filesystem 조회나
  `PureWindowsPath.is_reserved()`는 사용하지 않는다. `COM⁴`, `LPT⁴`, `COM10`, `LPT0`,
  `CONSOLE`과 기존 U+007F/U+FFFD/POSIX 허용 계약도 보존한다.

### 세 번째 리뷰 RED

```text
direct TaskSettingsV2 constructor traceback identity: 1 failed
Windows superscript reserved-device attacks: 6 failed, 43 passed, 64 deselected
```

### 세 번째 리뷰 GREEN

Python 3.13.14 (`uv run --python 3.13 --with pytest`):

```text
python -m pytest tests/ops/test_task_settings_v2.py tests/ops/test_task_projects.py tests/ops/test_project_discovery.py -q
253 passed, 1 skipped in 3.16s
```

Python 3.11.15:

```text
python -m pytest tests/ops/test_task_settings_v2.py tests/ops/test_task_projects.py tests/ops/test_project_discovery.py -q
253 passed, 1 skipped in 3.83s

python -m pytest tests/ops/test_plain_names.py tests/ops/test_task_settings.py tests/ops/test_task_service.py tests/ops/test_task_outbox.py -q
92 passed, 3 skipped in 10.73s
```

Python 3.13의 v1 gate는 PyYAML dependency를 포함하면 `91 passed, 3 skipped`이고,
`TaskOutbox("bad\\0path")` 오류 문구 assertion 1개만 실패한다. detached parent worktree
`3d61a8d`에서도 같은 test가 같은 `Task outbox lock directory could not be created safely`
문구로 실패함을 재현했다. 이번 diff는 v1 outbox 코드/test를 수정하지 않으며, Task 6 범위 밖
Python 3.13 `pathlib` 기준선 호환성 문제로 기록한다.

최종 Python 3.13 Ruff, `py_compile`, `git diff --check`와 parent 대비 v1 outbox zero-diff
검증도 통과했다.
