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
109 passed in 1.29s
```

```text
uv run --with pytest python -m pytest tests/ops/test_task_setup.py tests/ops/test_task_settings.py tests/ops/test_safe_files.py tests/ops/test_task_options.py -q
168 passed in 8.03s
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

전체 `tests/ops -q` 결과는 `598 passed, 3 skipped, 4 failed`였다. 다음 네 건은 Task 4
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
