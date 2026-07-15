# Forge 단계 오케스트레이터 구현 계획

> **Agentic worker(Agent 실행자)용:** REQUIRED SUB-SKILL: 이 plan을 task 단위로 구현하려면 `weapon:subagent-driven-development`(권장) 또는 `weapon:executing-plans`를 사용한다. 진행 추적에는 checkbox(`- [ ]`) 문법을 쓴다.

**Goal:** 검증된 executor 결과부터 reviewer·critic을 새 Hermes 카드로 자동 연결하고, 반려는 새 executor 재작업으로 되돌리며, 최신 PR HEAD의 `eval` CI 성공에서만 `forge:mergeable`을 투영한다.

**Architecture:** stdlib-only Python core가 Hermes SQLite와 GitHub CLI adapter에서 받은 snapshot을 순수 상태 전이 함수로 평가한다. 후속 카드는 상위 결과 digest를 포함한 Hermes idempotency key로 JIT 생성한다. label mirror는 같은 snapshot을 읽되 GitHub 라벨을 쓰는 유일한 process로 남고, GitHub ruleset은 PR과 `eval` 성공을 서버에서 강제한다.

**Tech Stack:** Python 3.11+, pytest 8.x, stdlib dataclass/enum/json/hashlib/sqlite3/subprocess/pathlib, Hermes Agent v0.18.2 CLI, GitHub CLI, GitHub Actions, systemd user timer

## Global Constraints

1. Hermes core는 수정하지 않는다.
2. GitHub Actions에서 LLM을 실행하지 않는다.
3. GitHub 라벨은 `label-mirror.py`만 쓴다.
4. API·DB·JSON·CI 판정 불능을 성공이나 빈 목록으로 대체하지 않는다.
5. reviewer·critic·rework-executor는 상위 결과 digest당 정확히 한 카드만 생성한다.
6. reviewer와 critic은 executor와 다른 Hermes task/session이어야 한다.
7. reviewer 전에는 executor PR 현재 HEAD의 `eval=success`를 요구한다.
8. `forge:mergeable` 전에는 critic 결과 HEAD와 live HEAD가 같고 그 SHA의 `eval=success`를 요구한다.
9. reviewer reject와 critic defect_found는 새 executor-rework를 만들고 최대 3개 rework 뒤 `forge:failed`로 끝낸다.
10. 자동 merge는 구현하지 않고 P1 사람 merge를 유지한다.
11. 현재 check context `eval`은 ruleset 활성화 뒤 이름을 바꾸지 않는다.
12. public repository `main` ruleset은 bypass 없음, PR 필수, approvals 0, strict `eval` 필수, force-push·deletion 차단이다.

## 파일 책임 지도

```text
forge/
  __init__.py
  ops/
    __init__.py
    contracts.py             strict result parser와 snapshot/action 값 객체
    stage_reconciler.py      순수 상태 전이와 stage card spec
    hermes.py                read-only SQLite + Hermes create argv/실행
    github.py                PR URL/HEAD/check-runs 조회
    label_projection.py      pipeline frontier → forge label
  schemas/
    reviewer-result-v1.schema.json
    critic-result-v1.schema.json
  scripts/
    stage-reconciler.py      one-shot controller entrypoint
    label-mirror.py          root import + single label writer
tests/
  ops/
    test_contracts.py
    test_stage_reconciler.py
    test_adapters.py
    test_label_projection.py
    test_stage_cli.py
```

---

### Task 1: 역할 결과 계약과 전이 digest

**Files:**
- Create: `forge/__init__.py`
- Create: `forge/ops/__init__.py`
- Create: `forge/ops/contracts.py`
- Create: `forge/schemas/reviewer-result-v1.schema.json`
- Create: `forge/schemas/critic-result-v1.schema.json`
- Create: `tests/ops/test_contracts.py`

**Interfaces:**
- Produces: `PipelineStage`, `StageOutcome`, `TaskRecord`, `RunRecord`, `PullRequestSnapshot`, `CheckRun`, `ExecutorResult`, `ReviewerResult`, `CriticResult`, `parse_stage_result(stage: PipelineStage, summary: Mapping[str, object], metadata: Mapping[str, object]) -> StageResult`, `transition_digest(task_id: str, run_id: int, stage: PipelineStage, summary: Mapping[str, object], metadata: Mapping[str, object], pr_url: str, head_sha: str) -> str`.
- Raises: `ContractError` for malformed or unbound stage results.

- [ ] **Step 1: strict parser RED tests 작성**

```python
def test_reviewer_reject_requires_reflection() -> None:
    with pytest.raises(ContractError, match="reflection"):
        parse_stage_result(
            PipelineStage.REVIEWER,
            {"schema_version": "forge-reviewer-result/v1", "verdict": "reject"},
            {},
        )


def test_critic_pass_requires_added_tests_and_result_head() -> None:
    with pytest.raises(ContractError, match="added_tests"):
        parse_stage_result(
            PipelineStage.CRITIC,
            {"schema_version": "forge-critic-result/v1", "outcome": "pass"},
            {},
        )
```

- [ ] **Step 2: RED 확인**

Run: `%LOCALAPPDATA%\InfinityForge\dev-venv\Scripts\python.exe -m pytest tests/ops/test_contracts.py -q`

Expected: `ModuleNotFoundError: No module named 'forge.ops'`로 FAIL.

- [ ] **Step 3: dataclass·enum·strict parser 최소 구현**

```python
class PipelineStage(str, Enum):
    EXECUTOR = "executor"
    REVIEWER = "reviewer"
    CRITIC = "critic"
    EXECUTOR_REWORK = "executor-rework"


def transition_digest(*, task_id: str, run_id: int, stage: PipelineStage,
                      summary: Mapping[str, object], metadata: Mapping[str, object],
                      pr_url: str, head_sha: str) -> str:
    payload = {"task_id": task_id, "run_id": run_id, "stage": stage.value,
               "summary": summary, "metadata": metadata,
               "pr_url": pr_url, "head_sha": head_sha}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
```

- [ ] **Step 4: GREEN 확인**

Run: `%LOCALAPPDATA%\InfinityForge\dev-venv\Scripts\python.exe -m pytest tests/ops/test_contracts.py -q`

Expected: 모든 contract test PASS.

- [ ] **Step 5: schema와 parser field set 일치 검사 추가 후 commit**

```powershell
git add forge/__init__.py forge/ops forge/schemas tests/ops/test_contracts.py
git commit -m "feat: define Forge stage result contracts"
```

### Task 2: 순수 stage reconciler와 멱등 카드 spec

**Files:**
- Create: `forge/ops/stage_reconciler.py`
- Create: `tests/ops/test_stage_reconciler.py`

**Interfaces:**
- Consumes: Task 1의 stage/result/snapshot types.
- Produces: `ActionKind`, `PipelineSnapshot`, `StageAction`, `StageCardSpec`, `decide_next_action(snapshot)`, `build_stage_card_spec(snapshot, action)`.

- [ ] **Step 1: happy/reject/stale HEAD/retry RED tests 작성**

```python
def test_green_executor_creates_reviewer_once() -> None:
    action = decide_next_action(executor_snapshot(check_conclusion="success"))
    assert action.kind is ActionKind.CREATE_REVIEWER


def test_reviewer_reject_creates_rework_and_never_critic() -> None:
    action = decide_next_action(reviewer_snapshot(verdict="reject", reflection="AC2 누락"))
    assert action.kind is ActionKind.CREATE_REWORK


def test_stale_reviewer_head_is_gate_error() -> None:
    action = decide_next_action(reviewer_snapshot(bound_head="a" * 40, live_head="b" * 40))
    assert action.kind is ActionKind.GATE_ERROR


def test_critic_pass_needs_green_result_head() -> None:
    action = decide_next_action(critic_snapshot(check_conclusion="pending"))
    assert action.kind is ActionKind.WAIT
```

- [ ] **Step 2: RED 확인**

Run: `%LOCALAPPDATA%\InfinityForge\dev-venv\Scripts\python.exe -m pytest tests/ops/test_stage_reconciler.py -q`

Expected: import 또는 missing symbol로 FAIL.

- [ ] **Step 3: 순수 전이 함수 최소 구현**

`decide_next_action`은 외부 호출을 하지 않는다. missing/duplicate required check는 `GATE_ERROR`, pending은 `WAIT`, failed는 `WAIT`, exact success만 다음 단계다. rework count가 3이면 `MARK_FAILED`다.

- [ ] **Step 4: idempotency key·parent·body RED/GREEN tests**

card key는 `forge-stage:{repo}#{issue}:{target-stage}:{source_digest[:16]}`이고 body에 source task/run/digest, PR URL, bound HEAD, reflection을 canonical JSON block으로 포함한다.

- [ ] **Step 5: GREEN 확인 후 commit**

```powershell
git add forge/ops/stage_reconciler.py tests/ops/test_stage_reconciler.py
git commit -m "feat: decide idempotent Forge stage transitions"
```

### Task 3: Hermes·GitHub adapter와 one-shot controller

**Files:**
- Create: `forge/ops/hermes.py`
- Create: `forge/ops/github.py`
- Create: `forge/scripts/stage-reconciler.py`
- Create: `tests/ops/test_adapters.py`
- Create: `tests/ops/test_stage_cli.py`

**Interfaces:**
- Consumes: Task 2 `StageAction`과 `StageCardSpec`.
- Produces: `HermesStore.list_pipeline_tasks() -> Sequence[TaskRecord]`, `HermesStore.latest_completed_run(task_id: str) -> RunRecord`, `build_create_argv(spec: StageCardSpec) -> Sequence[str]`, `GitHubClient.get_pr_snapshot(pr_url: str, required_check_names: Sequence[str]) -> PullRequestSnapshot`, `reconcile_once(store: HermesStore, github: GitHubClient, create: CreateCommand, config: ReconcileConfig) -> ReconcileReport`.

- [ ] **Step 1: SQLite fixture와 Hermes argv RED tests**

```python
def test_stage_create_argv_binds_parent_skill_and_idempotency() -> None:
    argv = build_create_argv(card_spec())
    assert "--parent" in argv
    assert "--skill" in argv
    assert "--idempotency-key" in argv
```

SQLite fixture는 live v0.18.2의 `tasks`, `task_runs`, `task_links` 최소 column을 만든다. query는 read-only URI를 사용하고 duplicate root/stage key를 `GateError`로 처리한다.

- [ ] **Step 2: GitHub check exact-set RED tests**

같은 `eval` check가 0개 또는 2개이면 GateError, 1개 success이면 green, queued/in_progress이면 pending으로 정규화한다. check는 반드시 requested HEAD SHA endpoint에서 조회한다.

- [ ] **Step 3: RED 확인**

Run: `%LOCALAPPDATA%\InfinityForge\dev-venv\Scripts\python.exe -m pytest tests/ops/test_adapters.py tests/ops/test_stage_cli.py -q`

Expected: missing module/symbol로 FAIL.

- [ ] **Step 4: adapter와 CLI 최소 구현**

CLI 기본값:

```text
--db ~/.hermes/kanban.db
--hermes ~/.local/bin/hermes
--gh /usr/bin/gh
--repo immortal0900/INFINITY_FORGE
--required-check eval
--max-reworks 3
```

stdout에는 JSON report 한 개, 성공 exit 0, 판정 불능 exit 2를 출력한다. token·raw environment·전체 stderr는 report에 넣지 않는다.

- [ ] **Step 5: 같은 snapshot 두 번 reconcile 회귀 test**

첫 실행은 create argv 1회, 두 번째 실행은 existing idempotency key를 보고 create 0회여야 한다.

- [ ] **Step 6: GREEN 확인 후 commit**

```powershell
git add forge/ops/hermes.py forge/ops/github.py forge/scripts/stage-reconciler.py tests/ops/test_adapters.py tests/ops/test_stage_cli.py
git commit -m "feat: reconcile Hermes stages from GitHub evidence"
```

### Task 4: stage-aware label projection과 기존 mirror 통합

**Files:**
- Create: `forge/ops/label_projection.py`
- Modify: `forge/scripts/label-mirror.py`
- Create: `tests/ops/test_label_projection.py`
- Create: `tests/ops/test_label_mirror.py`

**Interfaces:**
- Consumes: pipeline task/run/PR snapshot.
- Produces: `ProjectionState(stage: PipelineStage, task_status: str, outcome: StageOutcome | None, current_head_green: bool, rework_count: int)`, `projected_label(snapshot: ProjectionState, max_reworks: int = 3) -> str | None`.

- [ ] **Step 1: frontier projection RED tests**

```python
def test_reviewer_ready_projects_need_review() -> None:
    state = ProjectionState(PipelineStage.REVIEWER, "ready", None, False, 0)
    assert projected_label(state) == "forge:need-review"


def test_reviewer_reject_with_rework_ready_projects_need_execution() -> None:
    state = ProjectionState(PipelineStage.EXECUTOR_REWORK, "ready", None, False, 1)
    assert projected_label(state) == "forge:need-execution"


def test_critic_running_projects_need_critic() -> None:
    state = ProjectionState(PipelineStage.CRITIC, "running", None, False, 0)
    assert projected_label(state) == "forge:need-critic"


def test_critic_pass_pending_ci_stays_need_critic() -> None:
    state = ProjectionState(PipelineStage.CRITIC, "done", StageOutcome.PASS, False, 0)
    assert projected_label(state) == "forge:need-critic"


def test_critic_pass_green_current_head_projects_mergeable() -> None:
    state = ProjectionState(PipelineStage.CRITIC, "done", StageOutcome.PASS, True, 0)
    assert projected_label(state) == "forge:mergeable"


def test_rework_limit_projects_failed() -> None:
    state = ProjectionState(PipelineStage.EXECUTOR_REWORK, "done", StageOutcome.REJECT, False, 3)
    assert projected_label(state) == "forge:failed"
```

- [ ] **Step 2: RED 확인**

Run: `%LOCALAPPDATA%\InfinityForge\dev-venv\Scripts\python.exe -m pytest tests/ops/test_label_projection.py tests/ops/test_label_mirror.py -q`

Expected: missing module 또는 기존 mirror의 stage 미지원 assertion으로 FAIL.

- [ ] **Step 3: projection core와 mirror 최소 변경**

기존 root executor import를 유지한다. `cards_by_key`는 root와 `forge-stage:*`를 함께 읽고 issue별 pipeline을 구성한다. 상태 라벨 patch는 기존 함수 하나에서만 수행한다.

- [ ] **Step 4: 기존 수입 회귀와 전체 GREEN 확인 후 commit**

```powershell
git add forge/ops/label_projection.py forge/scripts/label-mirror.py tests/ops/test_label_projection.py tests/ops/test_label_mirror.py
git commit -m "feat: project Forge pipeline frontier labels"
```

### Task 5: worker 계약, CI, VPS timer, 운영 문서

**Files:**
- Modify: `forge/skills/reviewer-verdict/SKILL.md`
- Modify: `forge/skills/critic-adversarial/SKILL.md`
- Modify: `forge/skills/kanban-codex-delegate/SKILL.md`
- Modify: `.github/workflows/capability-eval.yml`
- Modify: `forge/scripts/deploy-vps.sh`
- Modify: `docs/plan.md`
- Modify: `docs/user-runbook.md`
- Modify: `docs/weapon/specs/2026-07-15-forge-stage-orchestrator-design.md`
- Create: `tests/ops/test_worker_contracts.py`
- Create: `tests/ops/test_workflow_contract.py`

**Interfaces:**
- reviewer/critic summary는 Task 1 schema와 exact field set이 일치한다.
- `eval`은 ruleset이 요구하는 안정적인 final check context다.
- VPS `forge-stage.timer`는 매 1분 one-shot controller를 실행한다.

- [ ] **Step 1: skill/workflow/deploy contract RED tests 작성 후 실패 확인**
- [ ] **Step 2: worker skill JSON 예시와 금지 규칙 갱신**
- [ ] **Step 3: workflow의 stale private/free 주석 제거와 `eval` 안정성 주석 추가**
- [ ] **Step 4: deploy-vps가 repo-root PYTHONPATH로 stage timer를 설치하도록 변경**
- [ ] **Step 5: plan 불변식을 root 1개 + receipt별 child 1개로 정정하고 runbook의 미구현 경고를 구현 상태로 갱신**
- [ ] **Step 6: 문서 change history에 실제 검증 결과 기록**
- [ ] **Step 7: focused/full GREEN 확인 후 commit**

```powershell
%LOCALAPPDATA%\InfinityForge\dev-venv\Scripts\python.exe -m pytest tests/ops -q
%LOCALAPPDATA%\InfinityForge\dev-venv\Scripts\python.exe -m pytest -q
git add forge/skills .github/workflows/capability-eval.yml forge/scripts/deploy-vps.sh docs tests/ops
git commit -m "ops: activate Forge reviewer and critic pipeline"
```

### Task 6: GitHub ruleset 적용, VPS 배포, E2E 검증

**External state:**
- GitHub repository `immortal0900/INFINITY_FORGE`
- Ubuntu VPS `ubuntu@51.222.27.48`

- [ ] **Step 1: 배포 전 fresh local verification**

```powershell
%LOCALAPPDATA%\InfinityForge\dev-venv\Scripts\python.exe -m pytest -q
git diff --check
```

- [ ] **Step 2: branch push와 PR 생성**

Ruleset 적용 전 `eval`이 실제 PR HEAD에 생성되는지 확인한다. 사용자 소유 미커밋 파일은 staging scope에서 제외한다.

- [ ] **Step 3: active ruleset 생성**

GitHub REST/UI desired state는 `protect-main`, `~DEFAULT_BRANCH`, bypass 없음, pull_request approvals 0, required status `eval` source GitHub Actions, strict true, deletion/non-fast-forward 차단이다. 생성 뒤 REST API로 exact read-back한다.

- [ ] **Step 4: ruleset 차단 검증**

PR의 `eval`이 pending/red이면 mergeability가 blocked이고 success이면 CI 조건이 충족되는지 확인한다. 실제 merge는 P1 사람 승인 전 수행하지 않는다.

- [ ] **Step 5: PR을 사람이 merge한 뒤 VPS 배포**

```powershell
ssh ubuntu@51.222.27.48 'cd ~/work/INFINITY_FORGE && bash forge/scripts/deploy-vps.sh'
```

배포는 GitHub `main`에 병합된 commit만 사용한다. 로컬 미병합 branch를 VPS production에 직접 복사하지 않는다.

- [ ] **Step 6: VPS read-back**

```text
systemctl --user is-active forge-stage.timer
systemctl --user list-timers forge-stage.timer
python3 stage-reconciler.py --dry-run
Hermes DB quick_check
```

- [ ] **Step 7: E2E canary issue**

작은 테스트 전용 이슈로 executor→reviewer approve→critic pass→latest HEAD `eval` green→mergeable을 확인한다. 별도 fixtures로 reviewer reject와 critic defect_found가 rework를 만들고 critic을 잘못 승격하지 않는지 확인한다.

- [ ] **Step 8: 완료 알림과 change history**

실제 commit SHA, PR URL, ruleset ID, VPS timer 상태, test pass 수, E2E 카드 ID를 문서 change history와 Slack 완료 알림에 기록한다.

## 최종 검증

```powershell
$py = "$env:LOCALAPPDATA\InfinityForge\dev-venv\Scripts\python.exe"
& $py -m pytest -q
& $py -m py_compile forge/scripts/*.py forge/ops/*.py
git diff --check
```

```bash
for f in forge/scripts/*.sh forge/hooks/*.sh; do bash -n "$f"; done
```

## 변경이력

- 2026-07-15 | 최초 계획 | 변경: stage contract, reconciler, adapter, label projection, worker 계약, ruleset, VPS E2E를 6개 검증 단위로 분해 | 검증: placeholder scan 0건, task별 파일·interface·검증 명령 자체 대조
