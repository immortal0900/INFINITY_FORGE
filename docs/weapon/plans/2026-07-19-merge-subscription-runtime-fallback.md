# Subscription Runtime Fallback 병합 구현 계획

> **Agentic worker(Agent 실행자)용:** REQUIRED SUB-SKILL: 이 plan을 task 단위로 구현하려면 `weapon:subagent-driven-development`(권장) 또는 `weapon:executing-plans`를 사용한다. 진행 추적에는 checkbox(`- [ ]`) 문법을 쓴다.

**Goal:** `codex/subscription-runtime-fallback`의 커밋된 구현 전체를 현재 브랜치에 통합하고, Claude Code `2.1.215`의 `subscriptionType: null`을 안전한 Claude.ai 구독 인증으로 허용한다.

**Architecture:** 커밋 `9084dcf`를 merge commit으로 통합해 기존 runner·setup·배포·테스트 이력을 보존한다. 인증 판단은 `forge.ops.subscription_runtime.is_claude_subscription_auth()` 한 곳으로 모으고 runner·setup·stream 분류가 같은 계약을 사용한다. 실제 배포와 gateway 재시작은 수행하지 않는다.

**Tech Stack:** Python 3.12, pytest 9.1.1, PowerShell, Bash, Git, Hermes Agent 0.18.2, Claude Code 2.1.215

## Global Constraints

- 병합 대상은 `codex/subscription-runtime-fallback`의 커밋된 tip `9084dcf`다.
- 원본 subscription worktree의 미커밋 변경은 병합하지 않는다.
- 대상 worktree의 기존 `forge/ops/worker_runtime.py` 변경은 보존한다.
- Claude 자동 호출은 `loggedIn=true`, `authMethod=claude.ai`, `apiProvider=firstParty`일 때만 허용한다.
- `subscriptionType`은 누락·`null`·기타 값이어도 인증 허용 여부에 사용하지 않는다.
- API·cloud credential과 routing switch는 Claude 자식 프로세스 환경에서 제거한다.
- Codex quota 이외의 실패에는 Claude로 fallback하지 않는다.
- Windows·EC2·VPS live deploy, persistent 환경변수 쓰기, Hermes gateway 재시작은 금지한다.

---

### Task 1: 커밋된 subscription runtime 브랜치 전체 병합

**Files:**
- Merge: `codex/subscription-runtime-fallback` at `9084dcf`
- Resolve: `forge/hermes_change/installer.py`
- Resolve: `forge/scripts/deploy-vps.sh`
- Resolve: `forge/scripts/deploy.ps1`
- Resolve: `tests/hermes/test_installer.py`
- Resolve: `tests/ops/test_plain_names.py`
- Resolve: `tests/ops/test_workflow_contract.py`

**Interfaces:**
- Consumes: 현재 `codex/hybrid-task-control`의 TUI·Desktop·배포 안전성 변경과 subscription 브랜치의 runner·setup·carried patch
- Produces: 두 계열 변경을 모두 포함하는 merge commit과 conflict marker가 없는 작업트리

- [ ] **Step 1: 병합 전 상태와 exact tip 검증**

Run: `git rev-parse codex/subscription-runtime-fallback && git status --short`
Expected: tip이 `9084dcf...`, 기존 사용자 변경은 `forge/ops/worker_runtime.py` 하나

- [ ] **Step 2: merge commit 시작**

Run: `git merge --no-ff codex/subscription-runtime-fallback -m "merge: integrate subscription runtime fallback"`
Expected: 위 여섯 파일만 content conflict

- [ ] **Step 3: 충돌을 합성 해결**

`forge/hermes_change/installer.py`와 `tests/hermes/test_installer.py`에는 현재 TUI·Desktop target과 subscription의 `hermes_cli/kanban_db.py` target을 모두 유지한다. 두 deploy script에는 현재 rollback/finalize 절차를 유지하면서 subscription configure/apply/verify와 환경 설치를 동일한 성공 경로에 넣는다. 두 contract test에는 양쪽 marker 검증을 모두 유지한다.

- [ ] **Step 4: 병합 구조 검증**

Run: `git diff --check && rg -n "^(<<<<<<<|=======|>>>>>>>)" forge tests`
Expected: exit 0, conflict marker 0개

### Task 2: Claude.ai first-party 인증 계약을 TDD로 단일화

**Files:**
- Modify: `forge/ops/subscription_runtime.py`
- Modify: `forge/ops/subscription_runner.py`
- Modify: `forge/ops/subscription_setup.py`
- Test: `tests/ops/test_subscription_runtime.py`
- Test: `tests/ops/test_subscription_runner.py`
- Test: `tests/ops/test_subscription_setup.py`

**Interfaces:**
- Produces: `is_claude_subscription_auth(auth_status: Mapping[str, object]) -> bool`
- Consumes: `classify_claude_stream()`, `SubscriptionRunner._claude_auth()`, `SubscriptionRuntimeSetup` preflight

- [ ] **Step 1: 실패하는 인증 회귀 테스트 작성**

`tests/ops/test_subscription_runtime.py`에 `subscriptionType` 누락과 `None`을 각각 허용하는 test를 추가하고, `loggedIn=false`, `authMethod=api_key`, `apiProvider=bedrock`은 `ExitClass.AUTH`로 유지한다. runner test는 `subscriptionType=None` 인증으로 quota fallback이 Claude를 한 번 호출하는지 검증한다. setup test는 같은 인증으로 preflight가 ready가 되는지 검증한다.

- [ ] **Step 2: RED 검증**

Run: `uv run --with pytest --python 3.12 --managed-python python -m pytest -q tests/ops/test_subscription_runtime.py tests/ops/test_subscription_runner.py tests/ops/test_subscription_setup.py`
Expected: 기존 `subscriptionType == "max"` 검사 때문에 새 null/missing test가 FAIL

- [ ] **Step 3: 최소 제품 코드 구현**

```python
def is_claude_subscription_auth(auth_status: Mapping[str, object]) -> bool:
    return (
        auth_status.get("loggedIn") is True
        and auth_status.get("authMethod") == "claude.ai"
        and auth_status.get("apiProvider") == "firstParty"
    )
```

`classify_claude_stream()`, `SubscriptionRunner._claude_auth()`, setup preflight가 이 helper만 사용하게 하고 `_is_max_auth` 중복을 제거한다. 사용자·조직 식별자와 `subscriptionType`은 로그나 receipt에 추가하지 않는다.

- [ ] **Step 4: GREEN 검증**

Run: `uv run --with pytest --python 3.12 --managed-python python -m pytest -q tests/ops/test_subscription_runtime.py tests/ops/test_subscription_runner.py tests/ops/test_subscription_setup.py`
Expected: 모든 지정 test PASS

### Task 3: 배포 계약·문서 정합성과 전체 회귀 검증

**Files:**
- Modify if required: `forge/scripts/configure-subscription-runtime.py`
- Modify if required: `forge/scripts/deploy.ps1`
- Modify if required: `forge/scripts/deploy-vps.sh`
- Modify: `docs/weapon/plans/2026-07-17-subscription-runtime-fallback.md`
- Test: `tests/ops/test_subscription_setup_cli.py`
- Test: `tests/ops/test_subscription_deploy_contract.py`
- Test: `tests/ops/test_subscription_skills.py`
- Test: `tests/ops/test_workflow_contract.py`
- Test: `tests/hermes/test_installer.py`

**Interfaces:**
- Consumes: Task 1의 병합 결과와 Task 2의 인증 helper
- Produces: `subscriptionType == "max"` 런타임 의존이 없는 통합 코드와 fresh 검증 증거

- [ ] **Step 1: 남은 Max 전용 계약 검색과 수정**

Run: `rg -n 'subscriptionType|max subscription|Claude Max|_is_max_auth' forge tests docs/weapon/plans/2026-07-17-subscription-runtime-fallback.md`
Expected: 정책 설명 외 제품 코드·테스트의 exact Max gate를 모두 식별

제품·테스트·배포 계약은 세 필드 helper 의미로 수정하고, 공식 문서 링크와 현재 CLI `2.1.215`의 null 사례를 계획 문서에 기록한다.

- [ ] **Step 2: subscription 전체 회귀 실행**

Run: `uv run --with pytest --python 3.12 --managed-python python -m pytest -q tests/ops/test_codex_subscription_probe.py tests/ops/test_subscription_runtime.py tests/ops/test_subscription_runner.py tests/ops/test_subscription_runner_cli.py tests/ops/test_subscription_setup.py tests/ops/test_subscription_setup_cli.py tests/ops/test_subscription_skills.py tests/ops/test_subscription_deploy_contract.py tests/ops/test_workflow_contract.py tests/hermes/test_installer.py`
Expected: 모든 지정 test PASS

- [ ] **Step 3: 전체 테스트와 정적 검증**

Run: `uv run --with pytest --python 3.12 --managed-python python -m pytest -q tests`
Expected: failure 0개

Run: `git diff --check`
Expected: exit 0

Run: PowerShell AST parse of `forge/scripts/deploy.ps1` and Git Bash `bash -n forge/scripts/deploy-vps.sh`
Expected: 두 script syntax PASS

- [ ] **Step 4: 변경 커밋**

Run: `git add`에는 병합·인증·테스트·문서 파일만 포함하고 `forge/ops/worker_runtime.py`는 제외한다.

Run: `git commit -m "fix: accept verified Claude subscription auth"`
Expected: 인증 계약 수정 commit 생성, 사용자 변경은 unstaged 유지
