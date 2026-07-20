# Task 4 구현 보고서 — 범용 Project 모델·발견·검증

## 결과

- `TaskProject`를 정확한 7개 공개 필드의 frozen/slots dataclass로 추가했다.
- 나머지 6개 binding field의 key-sorted compact UTF-8 JSON SHA-256으로
  `project_id`를 계산하고, 저장 record readback에서도 hash와 exact field set을 다시 검증한다.
- HTTPS, `git@github.com` SCP, `ssh://git@github.com` 세 GitHub remote 형식만
  credential 없이 허용한다.
- cwd Git root 우선 + canonical allowed roots scan을 단일 5초 deadline 아래 수행한다.
- workspace별 여러 remote를 deterministic 후보로 만들고, fetch/push URL은 remote마다
  정확히 1개이면서 같은 canonical repository여야 한다.
- GitHub `full_name`의 casing-only canonicalization은 허용하지만 rename/redirect는 거부한다.
- Git root, Git dir, common dir, remote, branch, local remote-tracking commit, GitHub commit을
  probe 전후에 다시 결합 검증한다.
- Windows locale과 무관하게 Git stdout을 UTF-8 strict로 읽어 한글 workspace를 실제 Git으로
  발견하며, decode 또는 형식 실패는 원문을 남기지 않고 닫힌 실패로 처리한다.
- 같은 workspace의 여러 remote가 같은 canonical repository를 가리켜도 `remote_name`별
  선택 후보를 유지한다. 다른 workspace의 clone과 linked worktree 중복은 계속 거부한다.
- credential-bearing remote 원문과 정규화 예외를 격리 helper 안에서 소거하여 외부 오류의
  cause/context와 라이브러리 traceback frame locals에 token이 남지 않게 했다.
- public `normalize_github_remote()` 직접 호출도 raw 인자를 지운 뒤 generic 오류를 만들며,
  private parser frame과 credential이 외부 traceback에 남지 않는다.
- custom Git runner가 `None`을 반환해도 assertion이 아니라 제어된 invalid-result 오류로
  닫힌 실패한다.
- Git 특수 이름 `HEAD`는 branch로 거부하고 일반 소문자 `head`는 허용한다.

## TDD 증거

### RED

명령:

```text
uv run --with pytest python -m pytest tests/ops/test_task_projects.py tests/ops/test_project_discovery.py -q
```

최초 결과:

```text
ERROR tests/ops/test_task_projects.py
ModuleNotFoundError: No module named 'forge.ops.task_projects'
ERROR tests/ops/test_project_discovery.py
ModuleNotFoundError: No module named 'forge.ops.project_discovery'
2 errors in 0.58s
```

self-review 중 workspace file, invalid Git ref component, final deadline, Git environment
override, unresolved reparse alias, failed cwd Git probe, external exception context, invalid
host UUID에도 각각 failing regression test를 먼저 확인한 뒤 구현을 보강했다.

### GREEN

```text
uv run --with pytest python -m pytest tests/ops/test_task_projects.py tests/ops/test_project_discovery.py -q
115 passed in 4.62s
```

```text
uv run --with pytest python -m pytest tests/ops/test_task_setup.py tests/ops/test_task_settings.py tests/ops/test_safe_files.py tests/ops/test_task_options.py -q
168 passed in 8.59s
```

### 독립 리뷰 수정 RED → GREEN

다음 회귀를 제품 코드보다 먼저 추가했다.

- `HEAD` 거부와 소문자 `head` 허용
- 같은 workspace의 `origin`·`upstream` same-repository 후보 유지
- subprocess `encoding="utf-8", errors="strict"` 계약과 실제 한글 경로 Git 저장소 발견
- credential remote 실패의 cause/context와 traceback frame locals token 비노출

수정 전 선택 회귀 실행 결과:

```text
5 failed, 20 passed, 1 warning in 2.96s
```

실패 원인은 각각 `HEAD` 허용, same-workspace alias duplicate 거부, 예외 context/token 보존,
encoding kwargs 누락, 실제 Windows CP949 decode 실패였다. 수정 후 같은 선택 회귀는 다음처럼
통과했다.

```text
25 passed in 2.40s
```

### 독립 리뷰 2차 수정 RED → GREEN

public remote parser의 credential traceback-local 비노출과 custom runner `None` 반환의 제어
오류 회귀를 먼저 추가했다. 수정 전에는 각각 token이 `forge.ops.task_projects` frame local에
남고 `AssertionError`가 발생했다.

```text
2 failed in 0.55s
```

private non-throwing parser와 public error boundary를 분리하고 `_run_git`의 assert를
invalid-result 검사로 대체한 뒤 같은 회귀가 통과했다.

```text
2 passed in 0.20s
```

실제 한글 경로 Git 저장소 검증도 별도로 다시 실행했다.

```text
1 passed in 3.05s
```

## 공격적 음수 경계

- arbitrary repository names, HTTPS/SCP/SSH canonical equivalence
- credential, userinfo, percent encoding, query, fragment, port, evil/trailing/Unicode host,
  uppercase scheme/host, `.GIT`, double suffix, extra colon
- bool/invalid type, UUID, commit, branch, extra/missing record field
- depth 0/3 exact boundary, hard depth 8/project 256 configuration, project 64/65 boundary
- single deadline, zero remaining time, subprocess timeout, API timeout 전달
- permission ambiguity, symlink/junction/reparse injection, probe 후 path change
- Git dir/common dir relative·nonexistent·multiline·outside escape
- linked worktree common-dir duplicate, separate clone duplicate, multi-remote same-repo alias
- missing remote, multi fetch/push URL, fetch/push mismatch
- GitHub full_name/default branch/selected branch/SHA mismatch
- explicit non-default branch와 local remote-tracking commit exact binding
- argv-only subprocess, prompt disabled, Git environment override 제거, secret exception 비노출

## 전체 ops 회귀의 범위 밖 실패

수정 후 전체 `tests/ops -q` 결과는 `605 passed, 3 skipped, 4 failed`였다. 다음 네 건은 Task 4
변경 파일과 무관하며 범위 밖 파일을 수정하지 않았다.

- `test_linux_deploy_lock.py::test_second_deploy_is_blocked_before_git_or_systemd` — WSL
  `wslpath`가 Windows에서 exit `4294967295`
- `test_linux_deploy_lock.py::test_matching_unlocked_fd_marker_acquires_lock_before_reentry` —
  같은 WSL 환경 실패
- `test_plain_names.py::test_pending_message_fields_have_plain_defaults` — 선택 의존성
  `yaml` 미설치
- `test_task_outbox.py::test_store_rejects_directory_and_invalid_path` — 기존 Windows invalid
  path error message가 test 기대와 불일치

## 위험 경계

- `RISK(breaking)`: 7개 공개 필드와 6-field hash preimage를 바꾸면 durable binding ID가 바뀐다.
- `RISK(security)`: validator는 저장 path/remote가 이후 Git write 권한으로 승격되기 전의
  재검증 경계다.
- 실제 GitHub/network 호출은 수행하지 않았고 모든 GitHub metadata는 injected fake로
  검증했다.
