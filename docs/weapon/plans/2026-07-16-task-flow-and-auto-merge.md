# Hermes Task 선택과 자동 병합 구현 계획

> **Agentic worker(Agent 실행자)용:** REQUIRED SUB-SKILL: 이 plan을 task 단위로 구현하려면 `weapon:subagent-driven-development`(권장) 또는 `weapon:executing-plans`를 사용한다. 진행 추적에는 checkbox(`- [ ]`) 문법을 쓴다.

**Goal:** 새 Hermes 대화에서 Chat 또는 Task를 고르고, Task마다 세 가지 작업 흐름과 `manual|safe_auto|full_auto`를 모두 선택하며, 검증한 현재 commit만 정책에 맞게 병합한다.

**Architecture:** Forge 소유 정책은 stdlib-only Python 모듈과 Hermes 사용자 plugin에 둔다. Hermes에는 모든 surface가 공유하는 `pre_user_turn` plugin hook과 실제 사용자 입력 표시를 연결하는 6개 대상(`hermes_cli/plugins.py`, `agent/conversation_loop.py`, `run_agent.py`, `cli.py`, `tui_gateway/server.py`, `gateway/run.py`)의 carried change를 설치한다. Task 설정은 별도 SQLite에 불변 기록하고, 작업 흐름과 자동 병합은 같은 설정 hash와 현재 GitHub commit을 다시 검증한다.

**Tech Stack:** Python 3.11+, pytest 9.x, SQLite, GitHub CLI, Hermes Agent v0.18.2 plugin API, Bash/PowerShell, systemd user timers

## Global Constraints

1. 공식 값은 `mode=chat|task`, `task_flow=build|build_review|build_review_deep_check`, `merge_mode=manual|safe_auto|full_auto`뿐이다.
2. 화면 역할은 Build·Review·Deep Check·Fix이며 모든 9개 설정 조합을 허용한다. 내부 카드 role ID는 각각 `builder|reviewer|deep_checker|fix`다.
3. 이전 Forge 설정 키, 카드 key, JSON schema, 라벨을 읽는 alias나 fallback을 만들지 않는다.
4. Chat은 GitHub, Kanban, Task 설정 저장소에 write하지 않는다.
5. Task는 `task_flow`와 `merge_mode`를 매번 새로 선택하고 확인 전에는 외부 write가 없다.
6. `safe_auto|full_auto` 권한은 최대 12시간이며 만료 뒤 사람 병합으로 축소한다.
7. 자동 병합은 Task 설정, Task 내용, 선택 흐름 완료, 현재 PR commit, `eval` CI, PR 상태를 병합 직전에 다시 확인한다.
8. `safe_auto`는 LLM을 사용하지 않고 문서·root README/CHANGELOG·새 테스트 파일만 허용한다.
9. `full_auto`는 파일 위험 분류만 건너뛰며 공통 병합 검사는 건너뛰지 않는다.
10. 병합은 expected commit 조건을 사용하고 `--admin`이나 GitHub ruleset 우회를 사용하지 않는다.
11. live 외부 식별자 `eval`, `protect-main`, 기존 `forge-*.service|timer` unit 이름은 유지한다.
12. Hermes DB schema, system prompt, tool 목록을 변경하지 않는다.
13. API·DB·JSON·pagination 오류를 성공이나 빈 목록으로 대체하지 않는다.
14. public schema, 외부 write, concurrency, restore code에는 `RISK(...)` 근거를 남긴다.
15. 기존 사용자 수정 `README.md`, `docs/setup/desktop-guide.md`, `forge/skills/memex/SKILL.md`, `.codex/`, `docs/setup/fetch-ec2-dashboard-token.ps1`를 보존한다.

## 파일 책임 지도

```text
forge/ops/
  task_options.py          공식 enum과 9개 조합
  task_settings.py         hash, 불변 record, SQLite store
  task_setup.py            시작 선택과 30분 Task 초안 상태
  task_flow.py             build/review/deep_check/fix 전이
  displayed_status.py      current step을 Forge label로 표시
  safe_files.py            safe_auto 결정 규칙
  merge_decision.py        외부 write 없는 공통 병합 판단
  github.py                PR·CI·파일·Review 전체 읽기
  github_merge.py          예상 base/head commit 병합과 branch 갱신 쓰기
forge/hermes_plugin/infinity_forge/
  plugin.yaml, __init__.py  pre_user_turn 선택 UI와 Task service 연결
forge/hermes_change/
  installer.py, files/...  Hermes 6개 대상 carried change와 restore package
forge/scripts/
  task-flow-worker.py, issue-status-sync.py, merge-worker.py
  system-check.sh, state-mismatch-check.sh, activity-log-writer.py
  send-pending-messages.py
```

---

### Task 1: 공식 이름과 Task 선택 계약

**Files:**
- Create: `forge/ops/task_options.py`
- Create: `tests/ops/test_task_options.py`

**Interfaces:**
- Produces: `Mode`, `TaskFlow`, `MergeMode`, `TaskRole`, `TaskSelection`, `parse_task_selection(value: Mapping[str, object]) -> TaskSelection`.
- Rejects: 이전 키·값, 빠진 필드, 추가 필드, 잘못된 enum.

- [ ] **Step 1: RED test 작성**

```python
def test_all_nine_task_combinations_are_valid() -> None:
    assert {
        (value.task_flow.value, value.merge_mode.value)
        for value in all_task_selections()
    } == set(product(
        ("build", "build_review", "build_review_deep_check"),
        ("manual", "safe_auto", "full_auto"),
    ))

def test_old_policy_keys_are_rejected() -> None:
    with pytest.raises(TaskOptionError, match="unexpected fields"):
        parse_task_selection({"quality_level": "standard", "merge_choice": "manual"})
```

- [ ] **Step 2: RED 확인**

Run: `& "$env:LOCALAPPDATA\InfinityForge\dev-venv\Scripts\python.exe" -m pytest tests/ops/test_task_options.py -q`

Expected: `ModuleNotFoundError: forge.ops.task_options`로 FAIL.

- [ ] **Step 3: 최소 구현**

```python
class TaskFlow(str, Enum):
    BUILD = "build"
    BUILD_REVIEW = "build_review"
    BUILD_REVIEW_DEEP_CHECK = "build_review_deep_check"

class MergeMode(str, Enum):
    MANUAL = "manual"
    SAFE_AUTO = "safe_auto"
    FULL_AUTO = "full_auto"
```

- [ ] **Step 4: GREEN 확인과 commit**

Run: `& "$env:LOCALAPPDATA\InfinityForge\dev-venv\Scripts\python.exe" -m pytest tests/ops/test_task_options.py -q`

Expected: 모든 test PASS.

Commit: `git commit -m "feat: define task choices with plain names"`

### Task 2: Task 설정 hash와 SQLite 기록

**Files:**
- Create: `forge/ops/task_settings.py`
- Create: `tests/ops/test_task_settings.py`

**Interfaces:**
- Consumes: Task 1 enum.
- Produces: `TaskContent`, `TaskSettings`, `task_content_hash()`, `task_settings_hash()`, `TaskSettingsStore.prepare()`, `bind_issue()`, `activate()`, `get_active()`.

- [ ] **Step 1: RED test 작성**

```python
def test_safe_auto_expires_no_later_than_twelve_hours() -> None:
    settings = make_settings(merge_mode=MergeMode.SAFE_AUTO)
    assert settings.auto_merge_expires_at == settings.confirmed_at + timedelta(hours=12)

def test_active_settings_cannot_be_changed(tmp_path: Path) -> None:
    store = TaskSettingsStore(tmp_path / "task-settings.db")
    active = activate_one(store)
    with pytest.raises(TaskSettingsError, match="immutable"):
        store.replace(active.request_id, merge_mode=MergeMode.FULL_AUTO)
```

- [ ] **Step 2: RED 확인**

Run: `& "$env:LOCALAPPDATA\InfinityForge\dev-venv\Scripts\python.exe" -m pytest tests/ops/test_task_settings.py -q`

Expected: import 실패.

- [ ] **Step 3: canonical JSON hash와 transaction 구현**

```python
def _sha256(value: Mapping[str, object]) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()

# RISK(breaking): active record의 설정 열은 UPDATE하지 않고 lifecycle event만 append한다.
```

- [ ] **Step 4: crash/replay test와 GREEN 확인**

Run: `& "$env:LOCALAPPDATA\InfinityForge\dev-venv\Scripts\python.exe" -m pytest tests/ops/test_task_settings.py -q`

Expected: hash, 만료 경계, 중복 request, prepare→bind→active test PASS.

Commit: `git commit -m "feat: store immutable task settings"`

### Task 3: 시작 선택 상태와 Hermes plugin

**Files:**
- Create: `forge/ops/task_setup.py`
- Create: `forge/hermes_plugin/infinity_forge/plugin.yaml`
- Create: `forge/hermes_plugin/infinity_forge/__init__.py`
- Create: `tests/ops/test_task_setup.py`
- Create: `tests/hermes_plugin/test_infinity_forge_plugin.py`

**Interfaces:**
- Produces: `SetupStep`, `SetupDraft`, `TaskSetup.handle(session_id, user_id, text, now) -> TurnResult`.
- Plugin hook return: `{"action":"continue"}`, `{"action":"replace","text":...}`, `{"action":"handled","text":...,"choices":[...]}`.

- [ ] **Step 1: RED test 작성**

```python
def test_chat_replays_first_message_once_without_writes() -> None:
    setup = TaskSetup(clock=fixed_clock)
    assert setup.handle("s1", "u1", "설명해줘").action == "handled"
    result = setup.handle("s1", "u1", "chat")
    assert result == TurnResult.replace("설명해줘")

def test_task_requires_flow_and_merge_mode_each_time() -> None:
    setup = begin_task_setup()
    assert setup.handle("s1", "u1", "build").next_step is SetupStep.MERGE_MODE
```

- [ ] **Step 2: RED 확인**

Run: `& "$env:LOCALAPPDATA\InfinityForge\dev-venv\Scripts\python.exe" -m pytest tests/ops/test_task_setup.py tests/hermes_plugin/test_infinity_forge_plugin.py -q`

Expected: import 실패.

- [ ] **Step 3: state machine과 plugin 등록 구현**

```python
def register(ctx) -> None:
    ctx.register_hook("pre_user_turn", before_user_turn)
    ctx.register_command("task", handler=start_task, description="Start a new Task")
```

- [ ] **Step 4: cancel, `/task`, 30분 만료, plugin 오류 test 후 GREEN**

Run: `& "$env:LOCALAPPDATA\InfinityForge\dev-venv\Scripts\python.exe" -m pytest tests/ops/test_task_setup.py tests/hermes_plugin/test_infinity_forge_plugin.py -q`

Expected: provider 호출 없이 handled, Chat replay 1회, 외부 write 0회 PASS.

Commit: `git commit -m "feat: add Hermes chat and task chooser"`

### Task 4: Hermes 공통 `pre_user_turn` carried change

**Files:**
- Create: `forge/hermes_change/installer.py`
- Create: `forge/hermes_change/files/hermes_cli/plugins.py.patch`
- Create: `forge/hermes_change/files/agent/conversation_loop.py.patch`
- Create: `forge/hermes_change/files/run_agent.py.patch`
- Create: `forge/hermes_change/files/cli.py.patch`
- Create: `forge/hermes_change/files/tui_gateway/server.py.patch`
- Create: `forge/hermes_change/files/gateway/run.py.patch`
- Create: `forge/scripts/install-hermes-change.py`
- Create: `tests/hermes/test_installer.py`
- Create: `tests/hermes/test_pre_user_turn_contract.py`

**Interfaces:**
- Produces: `install_change(root: Path, package: Path) -> InstallResult`, `restore_change(root: Path, restore_package: Path)`.
- Changes Hermes: `VALID_HOOKS`와 `run_conversation()` 직전 hook 호출만.

- [ ] **Step 1: RED installer와 hook contract test 작성**

```python
def test_changed_source_is_refused(tmp_path: Path) -> None:
    package = fixture_package(tmp_path)
    target = tmp_path / "hermes_cli" / "plugins.py"
    target.write_text("user change", encoding="utf-8")
    with pytest.raises(InstallError, match="before_file_hash"):
        install_change(tmp_path, package)
```

- [ ] **Step 2: RED 확인**

Run: `& "$env:LOCALAPPDATA\InfinityForge\dev-venv\Scripts\python.exe" -m pytest tests/hermes -q`

Expected: installer import 실패.

- [ ] **Step 3: hash 확인, atomic replace, restore package 구현**

```python
# RISK(data-loss): 대상 6개 파일의 before_file_hash가 정확히 맞을 때만 임시 파일을 os.replace한다.
if file_hash(target) != item.before_file_hash:
    raise InstallError(f"before_file_hash mismatch: {item.path}")
```

- [ ] **Step 4: copied Hermes source에서 allow/replace/handled 회귀 확인**

Run: `& "$env:LOCALAPPDATA\InfinityForge\dev-venv\Scripts\python.exe" -m pytest tests/hermes -q`

Expected: handled는 model 호출 0회, replace는 입력 1회 전달, allow는 기존 byte 입력 유지.

Commit: `git commit -m "feat: install shared Hermes user turn hook"`

### Task 5: 세 작업 흐름과 새 결과 계약

**Files:**
- Create: `forge/ops/task_flow.py`
- Create: `forge/ops/displayed_status.py`
- Create: `forge/schemas/build-result-v1.schema.json`
- Create: `forge/schemas/review-result-v1.schema.json`
- Create: `forge/schemas/deep-check-result-v1.schema.json`
- Create: `forge/schemas/step-proof-v1.schema.json`
- Create: `tests/ops/test_task_flow.py`
- Create: `tests/ops/test_displayed_status.py`
- Modify: `forge/ops/contracts.py`
- Modify: `forge/ops/hermes.py`

**Interfaces:**
- Produces: `TaskStep`, `TaskResult`, `TaskFlowState`, `next_task_action(state)`, `displayed_label(state)`.
- Every result includes `task_settings_hash` and current commit field.

- [ ] **Step 1: three-flow RED tests 작성**

```python
@pytest.mark.parametrize(("flow", "steps"), [
    (TaskFlow.BUILD, [TaskStep.BUILD]),
    (TaskFlow.BUILD_REVIEW, [TaskStep.BUILD, TaskStep.REVIEW]),
    (TaskFlow.BUILD_REVIEW_DEEP_CHECK,
     [TaskStep.BUILD, TaskStep.REVIEW, TaskStep.DEEP_CHECK]),
])
def test_each_flow_runs_only_selected_steps(flow, steps) -> None:
    assert completed_steps(run_flow(flow)) == steps
```

- [ ] **Step 2: RED 확인**

Run: `& "$env:LOCALAPPDATA\InfinityForge\dev-venv\Scripts\python.exe" -m pytest tests/ops/test_task_flow.py tests/ops/test_displayed_status.py -q`

Expected: 새 module import 실패.

- [ ] **Step 3: 최소 전이와 strict parser 구현**

```python
def required_steps(flow: TaskFlow) -> tuple[TaskStep, ...]:
    return {
        TaskFlow.BUILD: (TaskStep.BUILD,),
        TaskFlow.BUILD_REVIEW: (TaskStep.BUILD, TaskStep.REVIEW),
        TaskFlow.BUILD_REVIEW_DEEP_CHECK:
            (TaskStep.BUILD, TaskStep.REVIEW, TaskStep.DEEP_CHECK),
    }[flow]
```

- [ ] **Step 4: fix 최대 3회, commit 변경 시 build 재시작, 새 라벨 GREEN**

Run: `& "$env:LOCALAPPDATA\InfinityForge\dev-venv\Scripts\python.exe" -m pytest tests/ops/test_task_flow.py tests/ops/test_displayed_status.py tests/ops/test_contracts.py -q`

Expected: 선택하지 않은 step 0개, 새 라벨만 출력, exact current commit PASS.

Commit: `git commit -m "feat: route three task validation flows"`

### Task 6: `safe_auto` 고정 파일 검사

**Files:**
- Create: `forge/ops/safe_files.py`
- Create: `tests/ops/test_safe_files.py`

**Interfaces:**
- Produces: `ChangedFile`, `SafeFilesResult`, `check_safe_files(files) -> SafeFilesDecision`.
- Decisions: `AUTO_MERGE_ALLOWED`, `MANUAL_MERGE_REQUIRED`, `CHECK_ERROR`.

- [ ] **Step 1: allow/deny/error RED tests 작성**

```python
@pytest.mark.parametrize("path", ["docs/guide.md", "README.md", "CHANGELOG-2026.md"])
def test_text_docs_are_allowed(path: str) -> None:
    assert check_safe_files([changed(path, status="modified")]).allowed

def test_existing_test_change_requires_manual_merge() -> None:
    result = check_safe_files([changed("tests/test_api.py", status="modified")])
    assert result.code == "MANUAL_MERGE_REQUIRED"
```

- [ ] **Step 2: RED 확인**

Run: `& "$env:LOCALAPPDATA\InfinityForge\dev-venv\Scripts\python.exe" -m pytest tests/ops/test_safe_files.py -q`

Expected: import 실패.

- [ ] **Step 3: 경로 allowlist보다 차단 규칙을 먼저 적용**

```python
def check_safe_files(files: Sequence[ChangedFile]) -> SafeFilesDecision:
    if any(item.incomplete for item in files):
        return SafeFilesDecision.check_error("incomplete GitHub file data")
    if any(_blocked(item) for item in files):
        return SafeFilesDecision.manual("change is outside safe files")
    return SafeFilesDecision.allowed()
```

- [ ] **Step 4: rename/delete/binary/symlink/unknown pagination GREEN**

Run: `& "$env:LOCALAPPDATA\InfinityForge\dev-venv\Scripts\python.exe" -m pytest tests/ops/test_safe_files.py -q`

Expected: deterministic table test PASS.

Commit: `git commit -m "feat: classify safe auto merge files"`

### Task 7: GitHub 완전 조회와 병합 판단

**Files:**
- Modify: `forge/ops/github.py`
- Create: `forge/ops/merge_decision.py`
- Create: `forge/ops/github_merge.py`
- Create: `tests/ops/test_github_merge.py`
- Create: `tests/ops/test_merge_decision.py`

**Interfaces:**
- Produces: `GitHubClient.get_all_changed_files()`, `get_review_state()`, `GitHubMergeClient.merge_expected_commit()`, `decide_merge(context)`.

- [ ] **Step 1: pagination와 9-combination RED tests 작성**

```python
def test_full_auto_keeps_common_checks() -> None:
    decision = decide_merge(context(merge_mode="full_auto", ci="failure"))
    assert decision.code == "CHECK_ERROR"

def test_merge_uses_expected_commit() -> None:
    client.merge_expected_commit(PR_URL, "a" * 40)
    assert "--match-head-commit" in runner.calls[0]
```

- [ ] **Step 2: RED 확인**

Run: `& "$env:LOCALAPPDATA\InfinityForge\dev-venv\Scripts\python.exe" -m pytest tests/ops/test_github_merge.py tests/ops/test_merge_decision.py -q`

Expected: 새 symbol import 실패.

- [ ] **Step 3: read adapter와 순수 판단 구현**

```python
# RISK(race): 이 SHA는 판정 직후 expected commit 인자로 다시 전달한다.
if context.tested_commit != context.pull_request.head_sha:
    return MergeDecision.check_error("tested commit changed")
```

- [ ] **Step 4: draft/conflict/review/expiry/이미 병합됨 GREEN**

Run: `& "$env:LOCALAPPDATA\InfinityForge\dev-venv\Scripts\python.exe" -m pytest tests/ops/test_github_merge.py tests/ops/test_merge_decision.py tests/ops/test_adapters.py -q`

Expected: API 오류 때 write 0회, safe/full 공통 검사 PASS.

Commit: `git commit -m "feat: merge only the validated pull request commit"`

### Task 8: 실행 worker와 쉬운 운영 파일명 전환

**Files:**
- Create: `forge/scripts/task-flow-worker.py`
- Create: `forge/scripts/issue-status-sync.py`
- Create: `forge/scripts/merge-worker.py`
- Create: `forge/scripts/system-check.sh`
- Create: `forge/scripts/state-mismatch-check.sh`
- Create: `forge/scripts/activity-log-writer.py`
- Create: `forge/scripts/send-pending-messages.py`
- Create: `forge/hooks/codex-work-check.sh`
- Modify: `forge/scripts/deploy-vps.sh`
- Modify: `forge/scripts/deploy.ps1`
- Modify: `forge/skills/*/SKILL.md`
- Modify: active docs listed in the naming spec
- Delete: replaced old scripts, hook, schemas and Python modules after imports are zero.
- Create: `tests/ops/test_plain_names.py`
- Modify: `tests/ops/test_workflow_contract.py`

**Interfaces:**
- Existing systemd unit IDs stay; their `ExecStart` points to new entrypoints.
- All workers share one process lock; merge worker defaults `auto_merge_enabled=false` until system check passes.

- [ ] **Step 1: old-name and shared-lock RED tests 작성**

```python
def test_active_runtime_uses_only_plain_forge_names() -> None:
    matches = find_forbidden_names(ACTIVE_RUNTIME_FILES)
    assert matches == []

def test_all_writers_share_one_lock() -> None:
    deploy = Path("forge/scripts/deploy-vps.sh").read_text(encoding="utf-8")
    assert deploy.count("forge-pipeline.lock") >= 3
```

- [ ] **Step 2: RED 확인**

Run: `& "$env:LOCALAPPDATA\InfinityForge\dev-venv\Scripts\python.exe" -m pytest tests/ops/test_plain_names.py tests/ops/test_workflow_contract.py -q`

Expected: 현재 old runtime 파일명과 이름 때문에 FAIL.

- [ ] **Step 3: entrypoint, profile, label, message, docs를 clean break로 변경**

```bash
# RISK(race): 기존 unit ID를 재사용해 같은 역할 timer가 둘 생기지 않게 한다.
mkunit stage "$PIPELINE_LOCK /usr/bin/python3 $REPO_DIR/forge/scripts/task-flow-worker.py" "OnCalendar=*-*-* *:*:00"
mkunit mirror "$PIPELINE_LOCK /usr/bin/python3 $REPO_DIR/forge/scripts/issue-status-sync.py" "OnCalendar=*-*-* *:*:30"
```

- [ ] **Step 4: import scan, schema, docs link, full suite GREEN**

Run: `& "$env:LOCALAPPDATA\InfinityForge\dev-venv\Scripts\python.exe" -m pytest -q`

Expected: 전체 test PASS, 허용된 과거 spec/plan을 제외한 활성 파일 old name 0건.

Commit: `git commit -m "refactor: use plain Forge runtime names"`

### Task 9: branch 갱신, release 검증과 restore 연습

**Files:**
- Modify: `forge/ops/github_merge.py`
- Modify: `forge/ops/task_flow.py`
- Create: `tests/ops/test_branch_refresh.py`
- Modify: `docs/user-runbook.md`
- Modify: `docs/ops-guide.md`
- Modify: `docs/weapon/specs/2026-07-16-hermes-task-flow-auto-merge-design.md`

**Interfaces:**
- Produces: `update_branch(expected_commit)`, 독립 `fix_count`와 `branch_refresh_count`, 최대 3회 뒤 manual fallback.

- [ ] **Step 1: branch refresh RED tests 작성**

```python
def test_branch_refresh_restarts_selected_flow_from_build() -> None:
    result = refresh(context(branch_refresh_count=0))
    assert result.next_step is TaskStep.BUILD

def test_fourth_branch_refresh_requires_manual_merge() -> None:
    assert refresh(context(branch_refresh_count=3)).code == "MANUAL_MERGE_REQUIRED"
```

- [ ] **Step 2: RED 확인**

Run: `& "$env:LOCALAPPDATA\InfinityForge\dev-venv\Scripts\python.exe" -m pytest tests/ops/test_branch_refresh.py -q`

Expected: update branch interface 부재로 FAIL.

- [ ] **Step 3: expected commit update와 evidence 무효화 구현**

```python
# RISK(race): update-branch 뒤 새 commit을 읽고 이전 proof를 전부 폐기한다.
updated = github.update_branch(pr_url, expected_commit=current_commit)
return RestartFlow(commit=updated.head_sha, next_step=TaskStep.BUILD)
```

- [ ] **Step 4: fresh 전체 검증과 변경이력 기록**

Run:

```powershell
& "$env:LOCALAPPDATA\InfinityForge\dev-venv\Scripts\python.exe" -m pytest -q
git diff --check
rg -n "TB[D]|TO[D]O|[P]LACEHOLDER" docs/weapon/specs/2026-07-16-* docs/weapon/plans/2026-07-16-*
```

Expected: test failure 0개, diff error 0개, placeholder 0개.

- [ ] **Step 5: copied Hermes tree에 install→system check→restore 실행**

Expected: 대상 6개 파일의 `after_file_hash`가 설치 package와 일치하고 restore 뒤 `before_file_hash`와 일치한다. live Hermes 설치본은 배포 승인 전 수정하지 않는다.

Commit: `git commit -m "feat: restart validation after branch refresh"`

## 변경이력

- 2026-07-16 | 쉬운 운영 이름과 실제 worker 연결 | 변경: 9개 선택 조합, Build·Review·Deep Check·Fix 프로필, 공통 writer lock, 자동 병합 기본 off, Hermes 변경 대상 6개, Task Flow Worker·Issue Status Sync·Merge Worker의 실제 DB·GitHub·Hermes 연결을 반영. 불일치와 읽기 오류는 코드 2로 중단 | 검증: Task runtime·Issue Status Sync·Merge Worker 계약 테스트, plain-name/workflow 검사, Python·Git Bash·PowerShell 문법, Work Check known-input smoke
- 2026-07-16 | 구현 계획 작성 | 변경: Plain English clean break, Hermes 공통 입력 hook, Task 설정, 3개 작업 흐름, safe/full 자동 병합, branch 갱신을 9개 TDD task로 분해 | 검증: 두 2026-07-16 설계 명세와 현재 244 passed, 2 skipped 기준선 대조
