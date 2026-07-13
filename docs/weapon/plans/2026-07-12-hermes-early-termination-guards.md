# Hermes 조기 종료 방지 구현 계획

> **Agentic worker:** REQUIRED SUB-SKILL: `weapon:subagent-driven-development` 또는 `weapon:executing-plans`로 이 계획을 실행한다. 각 Task의 RED 증거를 먼저 확인하고, 최소 구현으로 GREEN을 만든 뒤 다음 Task로 이동한다.

- 상태: 승인된 spec을 실행 계획으로 변환 완료, 실행 방식 선택 대기
- 승인 spec: `docs/weapon/specs/2026-07-12-hermes-early-termination-guards-design.md`
- 기준 문서: `docs/plan.md`
- 대상: Windows 로컬, 일반 Linux clean install, Ubuntu VPS 실운영
- 선택안: 공통 검증기 + Codex Stop hook + 종료 후 재검증 + Hermes 완료 불변식 + PR CI + canary/drift

## Goal

Hermes/Codex가 테스트 실패, 빈 구현, 불완전 handoff, 미물질화 잔여, 예산 초과, 검증 장치 오류가 있는 상태를 `done`으로 전파하지 못하게 한다. 보호 태스크는 유효한 최종 receipt가 현재 task/run/repository evidence와 일치할 때만 완료되고, Windows·Linux·VPS에 동일한 Git SHA와 artifact hash로 배포된다.

## Architecture

Python 3.11+ 공통 검증 코어가 task contract, handoff, git baseline, GitHub issue/ADR/PR, 검증 명령, Codex session ledger를 하나의 판정으로 결합한다. Codex Stop hook, post-exit runner, Hermes `complete_task()`, PR CI는 이 코어와 동일한 JSON 계약을 사용한다. Hermes v0.18.2에는 `completion_policy=forge-v1` 보호 태스크에만 작동하는 최소 carried patch를 적용해 receipt 소비, 감사 이벤트, `done` 전이를 한 SQLite transaction으로 묶는다. 운영 supervisor는 fresh canary marker와 배포 SHA가 맞을 때만 dispatcher를 실행하며 gateway와 분리한다.

## Tech Stack

- Python 3.11+, stdlib, `jsonschema>=4.23,<5`, `pytest>=8,<9`
- Git CLI, GitHub CLI `gh`, Codex CLI 0.144.1 계약
- SQLite, Hermes Agent v0.18.2, systemd user units, Windows Scheduled Tasks
- GitHub Actions `ubuntu-latest`, `windows-latest`
- PowerShell 7+, Bash

## 실행 subplan

이 문서는 전체 DAG와 수용 기준의 SoT다. 구현자는 다음 네 subplan을 순서대로 실행하며, 세부 interface/test body/expected failure/minimal GREEN이 충돌하면 승인 spec→이 umbrella→subplan 순으로 해석한다.

1. `docs/weapon/plans/2026-07-12-hermes-guard-core-ci-subplan.md`
2. `docs/weapon/plans/2026-07-12-hermes-completion-policy-subplan.md`
3. `docs/weapon/plans/2026-07-12-hermes-ops-projection-subplan.md`
4. `docs/weapon/plans/2026-07-12-hermes-rollout-e2e-subplan.md`

Task dependency는 `core/CI → Hermes patch → ops/projection → release candidate CI → Windows → Linux staging → VPS → live E2E`다. 독립 unit 구현은 subagent로 병렬화할 수 있지만 shared schema와 live rollout은 이 순서를 바꾸지 않는다.

### 최초 enforcement PR bootstrap

Task 0의 CI-onboarding PR은 두 stable check를 default branch에 먼저 설치하는 repository prerequisite이며 아직 Forge protected task가 아니다. 이 onboarding merge 직후, feature worktree를 만들기 전에 GitHub에 spec 전용 bootstrap issue를 만들고 20개 AC를 stable ID로 기록하며 현재 plan commit SHA를 baseline으로 OS state root의 `bootstrap-request.json`에 원자 저장한다. 이후 첫 protected feature PR부터는 evidence 예외가 없다. Task 2에서 parser가 생기면 request를 정식 `TaskContract`로 변환한다. Task 17에서 branch의 새 runner가 PR별 evidence comment를 게시하고, comment보다 먼저 실패한 pull_request run만 `gh run rerun <run-id>`로 재실행한다. 따라서 protected enforcement를 껐다 켜는 compatibility flag나 특정 feature SHA 예외는 없다.

## Global Constraints

1. 작업 시작 시 `weapon:using-git-worktrees`, 구현 중 `weapon:test-driven-development`, 완료 전 `weapon:verification-before-completion`을 적용한다.
2. 현재 Windows Hermes 설치 checkout에는 사용자 소유의 대량 변경이 있다. 해당 checkout에서 전체 add, clean, reset, checkout을 금지하며 패치는 격리 worktree/clone에서 제작한다.
3. 프로젝트 runtime state와 evidence는 worktree 밖 OS 고정 root에 저장한다. POSIX 파일은 mode 600, Windows 파일은 현재 사용자만 읽기/쓰기가 가능한 ACL을 적용한다.
   - Windows: `%LOCALAPPDATA%\InfinityForge\state`
   - Linux/VPS: `${XDG_STATE_HOME:-~/.local/state}/infinity-forge`
4. trusted release는 task worktree 밖에 설치하며 manifest의 절대 path와 SHA-256을 모두 확인한다.
   - Windows: `%LOCALAPPDATA%\InfinityForge\guard\releases\<git-sha>`
   - Linux/VPS: `${XDG_DATA_HOME:-~/.local/share}/infinity-forge/guard/releases/<git-sha>`
5. shell command 문자열은 contract에서 금지하고 non-empty `array[string]` 형태의 `argv`만 허용한다. subprocess는 `shell=False`로 실행한다.
6. `TESTS_FAILED`와 `GATE_ERROR`를 섞지 않는다. 검증 대상의 불충족은 전자, 신뢰 가능한 판정을 만들 수 없는 도구/I/O/API 오류는 후자다.
7. 기본 상한은 고유 Codex thread 4개, task 3,600초, 누적 200,000 token, 명령당 900초다. 명령 timeout 합계는 task runtime 이하이며 hook timeout 3,660초보다 작아야 한다. pre-spawn 오류는 slot을 예약하지 않고, spawn 성공 가능성이 있으나 thread ID를 못 받은 `unknown_started` 예약만 보수적으로 기존 slot을 유지한다. `GATE_ERROR` recovery는 새 slot을 추가하지 않는다.
8. Stop hook 성공 stdout도 유효한 JSON을 출력한다. `TESTS_FAILED`는 `decision:block`, `GATE_ERROR`는 `continue:false`로 처리한다. 공식 계약은 [Codex Hooks](https://learn.chatgpt.com/docs/hooks)를 기준으로 한다.
9. private/free GitHub에서는 platform required check 설정을 보장하지 않는다. Actions 누락/실패 시 자동 merge와 Forge 완료 투영을 차단하는 것으로 D30을 충족한다.
10. GitHub 장애는 해당 task만 `GATE_ERROR`로 보류하고 독립 task dispatcher는 계속한다.
11. DB 직접 SQL 완료는 비지원 운영이다. DB mode 600, 전용 운영 계정, drift 검사를 방어선으로 둔다.
12. 기존 secret 값은 출력·복사·회전하지 않는다. 사용자가 요청한 범위는 조기 종료 방지 변경이며 credential rotation은 제외한다.
13. 모든 public CLI/schema/DB/concurrency 변경에 `RISK(breaking|race|security|data-loss|side-effect)` 주석 또는 인접 risk note를 남긴다.
14. 각 Task는 관련 테스트만 먼저 실행한 뒤 전체 테스트를 실행한다. 실패 출력을 숨기는 `|| true`, 빈 결과를 성공으로 바꾸는 fallback을 금지한다.
15. 배포는 정확한 40자리 Git SHA, green CI, immutable archive hash를 요구한다. `git pull` 기반 mutable deploy를 금지한다.
16. receipt expiry는 phase별로 고정한다: stop 65분, post-exit 24시간, ci 2시간, hermes 15분. source issue, AC, PR head, repo digest가 바뀌면 시간과 무관하게 즉시 stale이다.
17. candidate SHA가 바뀌면 이전 host deployment를 rollback하고 `2-platform CI → artifact rebuild → Windows → Linux staging → VPS → E2E`를 처음부터 반복한다. 일부 target만 새 SHA로 유지하지 않는다.

## 3~5수 앞 시뮬레이션

| 현재 선택 | 다음 단계 | 6개월 뒤 결과 | 실패 시 회복 |
|---|---|---|---|
| 공통 verifier 계약 | hook/runner/core/CI가 같은 fixture 사용 | 완료 규칙 drift를 schema/version diff로 탐지 | 이전 trusted release와 manifest로 atomic pointer rollback |
| Hermes 최소 carried patch | 모든 정상 complete 경로가 한 경계로 수렴 | upstream upgrade 때 함수별 preimage 차이를 명시적으로 검토 | patch commit revert, additive DB schema는 유지 |
| persistent session ledger | worker respawn이 thread 상한을 늘리지 못함 | 비용·retry 정책의 단일 SoT 유지 | task를 `needs_input`으로 보류 후 ledger audit |
| dispatcher/gateway 분리 | canary가 작업 수신만 중지 | 장애 조사 중 gateway API와 상태 조회 유지 | marker reopen 후 supervisor 재기동 |
| exact-SHA staged deploy | Windows→Linux→VPS 순차 검증 | 어느 호스트가 어떤 코드인지 재현 가능 | 이전 release pointer, config, unit, Hermes patch snapshot 복원 |

닫히는 옵션은 세 가지다. raw `status=done` 신뢰, card-only 잔여, mutable-main 배포는 더 이상 지원하지 않는다. 회복 비용이 가장 큰 실패는 DB migration/receipt ledger 손상과 Hermes patch 오적용이므로, additive migration과 target-file-only rollback을 사용하고 DB snapshot 복원은 integrity 손상 때만 수행한다.

## 요구사항 추적표

| Spec 수용 기준 | 구현 Task | 주 검증 |
|---|---:|---|
| 1. Windows/Ubuntu matrix green | 14, 17 | named Actions check 두 개의 실제 success |
| 2. 실제 Stop hook이 같은 thread 계속 | 7, 21 | Codex JSONL thread ID 동일 |
| 3. hook 미실행도 post-exit 차단 | 6, 21 | hook 제거 fixture |
| 4. handoff 없는 tool/CLI/Dashboard 완료 거절 | 9, 21 | upstream targeted tests + live negative E2E |
| 5. 유효 handoff/receipt만 원자 done | 5, 9, 21 | transaction snapshot equality + receipt ledger event |
| 6. 빈 잔여 배열 또는 실존 issue/ADR 강제 | 2, 4, 21 | partition/reference tests + live handoff |
| 7. committed-clean 및 multi-repo 검증 | 3, 8, 21 | baseline/diff/PR head tests |
| 8. worker respawn 전체 최대 4 thread | 6, 21 | persistent ledger test |
| 9. `GATE_ERROR` recovery가 새 failure session 미할당 | 6, 21 | pre-spawn release/unknown-started/retained-slot state test |
| 10. goal mode와 typed block 적용 | 1, 9, 10, 21 | create argv, sticky protocol, live card |
| 11. receipt 없는 done 비투영 | 10, 11, 21 | label/spec projection tests |
| 12. PR workflow 실제 check 생성 | 8, 14, 17 | PR event의 두 named checks |
| 13. canary가 dispatcher만 중단 | 12, 18, 19, 20 | process inspection + gateway health |
| 14. drift가 protocol/GATE_ERROR/issue/receipt/SHA 검사 | 13, 21 | invariant별 parametrized/live test |
| 15. spec coverage가 M/M/issue/PR/gate 별도 보고 | 11, 20, 21 | structured coverage output |
| 16. exact-SHA 배포와 rollback rehearsal | 15, 18, 19, 20 | deployment manifest/hash audit |
| 17. 양성 issue/card/PR E2E | 16, 21 | unique run ID evidence journal |
| 18. 음성 fixture 미완료/미투영 | 16, 21 | invalid handoff + receiptless complete |
| 19. 로컬/VPS DB 권한과 service/timer 검증 | 12, 14, 18, 19, 20 | ACL/mode, quick_check, systemd/Task inventory |
| 20. credential 값 비누출 | 4, 7, 8, 14, 17, 21 | scrub tests, secret scan, artifact/Slack payload audit |

## 최종 파일 구조

```text
pyproject.toml
requirements.lock
forge/
  __init__.py
  guard/
    __init__.py
    __main__.py
    errors.py
    contract.py
    git_state.py
    repo_capability.py
    references.py
    commands.py
    state.py
    verifier.py
    stop_hook.py
    runner.py
    evidence_bundle.py
    secret_scan.py
    cli.py
  hooks/
    codex-hooks.template.json
    codex-stop-gate.sh
  ops/
    __init__.py
    state.py
    github.py
    hermes.py
    label_mirror.py
    spec_coverage.py
    dispatcher_supervisor.py
    canary.py
    drift_audit.py
    deployment.py
    e2e_driver.py
  schemas/
    task-contract-v1.schema.json
    handoff-v1.schema.json
    receipt-v1.schema.json
    runner-state-v1.schema.json
    hermes-completion-result-v1.schema.json
    evidence-bundle-v1.schema.json
    spec-registry.schema.json
    build-manifest.schema.json
    deployment-receipt.schema.json
  patches/hermes/0.18.2/
    completion-policy.patch
    manifest.json
  scripts/
    build-guard-release.py
    install-codex-hook.py
    hermes-patch.py
    label-mirror.py
    spec-coverage.py
    canary.py
    drift-audit.py
    dispatcher-supervisor.py
    install-linux.sh
    install-windows.ps1
    deploy.ps1
    deploy-vps.sh
    rollback.ps1
    rollback-linux.sh
    rollback-vps.sh
    e2e-early-termination.py
  systemd/
    forge-dispatcher.service
    forge-canary.service
    forge-canary.timer
    forge-drift.service
    forge-drift.timer
    forge-mirror.service
    forge-mirror.timer
    forge-spec-coverage.service
    forge-spec-coverage.timer
    forge-ledger.service
    forge-ledger.timer
    forge-flush-outbox.service
    forge-flush-outbox.timer
    forge-morning-report.service
    forge-morning-report.timer
    forge-backup.service
    forge-backup.timer
forge/spec-registry.json
forge/spec-registry.md
tests/
  fixtures/
    build-manifest-v1.json
  guard/
  ops/
  hermes/
  integration/
  test_workflow_contract.py
.github/workflows/capability-eval.yml
docs/plan.md
docs/ops-guide.md
docs/automation-architecture.md
docs/weapon/evidence/hermes-completion-policy-patch-rehearsal.md
docs/weapon/plans/2026-07-12-hermes-guard-core-ci-subplan.md
docs/weapon/plans/2026-07-12-hermes-completion-policy-subplan.md
docs/weapon/plans/2026-07-12-hermes-ops-projection-subplan.md
docs/weapon/plans/2026-07-12-hermes-rollout-e2e-subplan.md
docs/weapon/specs/2026-07-12-hermes-early-termination-guards-design.md
```

## 고정 인터페이스

### 판정과 오류

```python
class FailureKind(str, Enum):
    PASS = "PASS"
    TESTS_FAILED = "TESTS_FAILED"
    GATE_ERROR = "GATE_ERROR"

@dataclass(frozen=True)
class VerificationResult:
    kind: FailureKind
    code: str
    message: str
    receipt: Receipt | None
```

- immutable task contract/config/schema/I/O/lock/`gh` auth·403·429·5xx·invalid JSON/command missing·timeout은 `GATE_ERROR`다.
- 모델 산출 handoff schema/AC partition, 빈 diff, changed-files mismatch, test/lint nonzero, 명시적 GitHub 404는 `TESTS_FAILED`다.
- 예상하지 못한 exception은 최상위 CLI에서 `GATE_ERROR`로 변환하고 traceback은 trusted log에만 기록한다.

### Contract/Handoff/Receipt

```text
parse_task_contract(data: Mapping[str, object]) -> TaskContract
parse_handoff(data: Mapping[str, object], contract: TaskContract) -> Handoff
canonical_json_bytes(value: object) -> bytes
sha256_json(value: object) -> str
verify(contract: TaskContract, handoff: Handoff, context: VerificationContext) -> VerificationResult
```

- `TaskContract`: source issue body hash, stable AC ID/text hash, repo path/remote/branch/baseline, argv commands, budgets, policy, verifier SHA.
- `Handoff`: PR URL, repo/path changed files, implemented, not_implemented(issue 또는 `forge:adr` issue), verified_by(command ID/evidence path).
- `Receipt`: phase(`stop|post-exit|ci|hermes`), task/run/contract/handoff/repo/command/reference/session/deployed SHA digest를 가진다. expiry는 stop 65분, post-exit 24시간, ci 2시간, hermes 15분이며 `phase=hermes`만 소비 가능하다.
- schema는 `additionalProperties:false`를 사용하고 receipt replay 방지를 위해 task/run에 결합한다.

### CLI

```text
forge-guard prepare --request <json>
forge-guard run --task-id <id>
forge-guard verify --task-id <id> --phase stop|post-exit|hermes|ci
forge-guard stop-hook
forge-guard completion-status --task-id <id> --json
forge-guard verify --phase ci --bundle <downloaded-pr-comment-json>
```

운영 CLI는 임의 `--state-dir`을 받지 않는다. Stop hook은 `FORGE_TASK_ID`를 읽되 task ID regex, hook cwd, stored contract task/run을 다시 대조한다.

### Codex 실행

새 thread는 다음 argv 계약을 사용한다.

```text
codex exec --json --output-schema <trusted>/handoff-v1.schema.json \
  -o <task-state>/codex-final.json --dangerously-bypass-hook-trust \
  --strict-config --sandbox danger-full-access -C <primary-repo> \
  --add-dir <secondary-repo> -
```

secondary repository가 둘 이상이면 `--add-dir <secondary-repo>` 쌍을 repository마다 반복한다.

resume은 원 session 설정을 계승하고 primary repo를 subprocess cwd로 사용한다.

```text
codex exec resume <thread-id> --json --output-schema <trusted>/handoff-v1.schema.json \
  -o <task-state>/codex-final.json --dangerously-bypass-hook-trust --strict-config -
```

JSONL의 `thread.started`와 `turn.completed.usage`만 thread/token SoT로 사용한다. usage 누락/파싱 오류는 `GATE_ERROR`다.

## Task 0: stable CI check를 별도 onboarding PR로 먼저 default branch에 올린다

**Files:**
- Modify: `.github/workflows/capability-eval.yml`
- Create: `tests/test_workflow_onboarding_contract.py`

**Boundary:** 이 Task는 아직 Forge protected task/runner를 만들 수 없는 유일한 repository onboarding 단계다. completion 예외가 아니라, 이후 첫 protected task가 요구할 두 named check를 default branch에 선행 설치하는 별도 PR이다. 제품 guard/Hermes/운영 코드는 포함하지 않는다.

**Steps:**

- [ ] 현재 Windows `gh auth status`가 미로그인이므로 operator가 interactive `gh auth login --web --git-protocol https --scopes repo,workflow,read:org`를 수행한다. WSL token/env를 복사하거나 plaintext token을 입력 파일에 남기지 않는다. 이어 plain `gh auth status`, canonical private repo view, workflow/variable/issue GET을 explicit repository context로 검증한다. 이 단계가 끝나기 전에는 GitHub write나 host deployment mutation을 0회로 유지한다.
- [ ] canonical remote default tip을 API/fetch로 exact base SHA로 고정하고 repository 밖 `%LOCALAPPDATA%\InfinityForge\worktrees\ci-onboarding-<base-sha>` worktree에서만 `codex/ci-onboarding` branch를 만든다. 현재 local `main`의 spec/plan commits를 base로 사용하지 않는다. pull_request/merge_group/push main/workflow_dispatch event와 Python 3.11의 exact matrix check 이름 `guard-contract (ubuntu-latest)`, `guard-contract (windows-latest)`만 추가한다.
- [ ] 두 job은 checkout SHA를 출력·검증하고 baseline repository의 existing test/compile/diff check만 실행한다. 아직 존재하지 않는 Forge package, bootstrap issue, evidence comment를 요구하지 않는다.
- [ ] `tests/test_workflow_onboarding_contract.py`가 event set, OS matrix, stable check 이름, `permissions: contents: read`를 검증하고 missing/skipped platform을 green으로 취급하지 않는지 확인한다.
- [ ] local `base...head`와 paginated remote PR files가 exact `{.github/workflows/capability-eval.yml, tests/test_workflow_onboarding_contract.py}`이고 PR `baseRefName=main`, `baseRefOid=<captured base>`, `headRefOid=<captured head>`인지 merge 직전에 재검증한다. 별도 PR의 두 check가 실제 success인 뒤 operator가 merge한다.
- [ ] merge SHA가 이후 current default tip의 ancestor이고 current tip의 workflow blob이 onboarding head와 exact same인지 검증한다. default branch가 더 전진했으면 정상으로 받아들이되 current tip에 `workflow_dispatch`를 실행해 동일 두 check success를 새 receipt에 결합한다.
- [ ] current default tip에서 새 implementation worktree/feature branch를 만들고, Task 0 시작 전에 캡처한 local planning-only commits가 spec+5 plans exact allowlist만 바꾸는지 확인한 뒤 순서대로 cherry-pick한다. onboarding PR 자체를 Forge guarded task로 소급 포장하지 않으며 이 clean feature branch에서만 Task 1 bootstrap request/첫 protected task를 시작한다.

Task 0 편집 전에 다음 setup만 실행한다. 기존 path/branch가 있으면 exact base binding을 검증하고 임의 삭제·reset하지 않는다.

```powershell
$PlanningRepo = 'C:\01.project\INFINITY_FORGE'
$TrustedPython = 'C:\01.project\INFINITY_FORGE\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $TrustedPython -PathType Leaf)) { throw 'trusted Python is missing' }
$StateRoot = Join-Path $env:LOCALAPPDATA 'InfinityForge\state'
$SetupReceiptPath = Join-Path $StateRoot 'ci-onboarding-setup.json'
Push-Location $PlanningRepo
try {
  $CanonicalRepo = (gh repo view --json nameWithOwner --jq .nameWithOwner).Trim()
} finally {
  Pop-Location
}
$ExpectedPlanningPaths = @(
  'docs/weapon/specs/2026-07-12-hermes-early-termination-guards-design.md',
  'docs/weapon/plans/2026-07-12-hermes-early-termination-guards.md',
  'docs/weapon/plans/2026-07-12-hermes-guard-core-ci-subplan.md',
  'docs/weapon/plans/2026-07-12-hermes-completion-policy-subplan.md',
  'docs/weapon/plans/2026-07-12-hermes-ops-projection-subplan.md',
  'docs/weapon/plans/2026-07-12-hermes-rollout-e2e-subplan.md'
)
$OnboardingBranch = 'codex/ci-onboarding'
if (Test-Path -LiteralPath $SetupReceiptPath) {
  $Setup = Get-Content -Raw -LiteralPath $SetupReceiptPath | ConvertFrom-Json
  if ($Setup.schema_version -ne 'forge-ci-onboarding-setup/v1' -or $Setup.repository -ne $CanonicalRepo) { throw 'onboarding setup receipt identity mismatch' }
  $OnboardingBase = [string]$Setup.onboarding_base_sha
  $LocalPlanningHead = [string]$Setup.planning_head_sha
  $PlanningCommits = @($Setup.planning_commits | ForEach-Object { [string]$_ })
  $OnboardingRoot = [string]$Setup.onboarding_root
  if ($Setup.onboarding_branch -ne $OnboardingBranch) { throw 'onboarding setup branch mismatch' }
  if ((git -C $PlanningRepo rev-parse HEAD).Trim() -ne $LocalPlanningHead) { throw 'planning head changed after setup receipt' }
  $CurrentApiMain = (gh api "repos/$CanonicalRepo/commits/main" --jq .sha).Trim()
  $Status = (gh api "repos/$CanonicalRepo/compare/$OnboardingBase...$CurrentApiMain" --jq .status).Trim()
  if ($Status -notin @('identical','ahead')) { throw 'captured onboarding base is no longer on main history' }
} else {
  Push-Location $PlanningRepo
  try {
    $LocalPlanningHead = (git rev-parse HEAD).Trim()
    git fetch --no-tags origin main
    if ($LASTEXITCODE -ne 0) { throw 'origin/main fetch failed' }
    $OnboardingBase = (git rev-parse refs/remotes/origin/main).Trim()
  } finally {
    Pop-Location
  }
  $ApiBase = (gh api "repos/$CanonicalRepo/commits/main" --jq .sha).Trim()
  if ($ApiBase -ne $OnboardingBase) { throw 'fetched origin/main does not match API default tip' }
  $PlanningPaths = @(git -C $PlanningRepo diff --name-only "$OnboardingBase..$LocalPlanningHead")
  if ((@($PlanningPaths | Sort-Object) -join "`n") -ne (@($ExpectedPlanningPaths | Sort-Object) -join "`n")) { throw 'local commits ahead of origin/main are not the approved planning-only path set' }
  $PlanningCommits = @(git -C $PlanningRepo rev-list --reverse "$OnboardingBase..$LocalPlanningHead")
  if ($PlanningCommits.Count -lt 1) { throw 'approved planning commits are missing' }
  $OnboardingRoot = Join-Path $env:LOCALAPPDATA "InfinityForge\worktrees\ci-onboarding-$OnboardingBase"
  New-Item -ItemType Directory -Force -Path $StateRoot | Out-Null
  $SetupValue = [ordered]@{schema_version='forge-ci-onboarding-setup/v1';repository=$CanonicalRepo;onboarding_base_sha=$OnboardingBase;planning_head_sha=$LocalPlanningHead;planning_commits=$PlanningCommits;onboarding_branch=$OnboardingBranch;onboarding_root=$OnboardingRoot}
  $SetupBytes = [Text.Encoding]::UTF8.GetBytes(($SetupValue | ConvertTo-Json -Compress) + "`n")
  $SetupTemp = "$SetupReceiptPath.$([Guid]::NewGuid().ToString('N')).tmp"
  $SetupStream = [IO.File]::Open($SetupTemp, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
  try { $SetupStream.Write($SetupBytes, 0, $SetupBytes.Length); $SetupStream.Flush($true) } finally { $SetupStream.Dispose() }
  Move-Item -LiteralPath $SetupTemp -Destination $SetupReceiptPath
}
$PlanningPaths = @(git -C $PlanningRepo diff --name-only "$OnboardingBase..$LocalPlanningHead")
if ((@($PlanningPaths | Sort-Object) -join "`n") -ne (@($ExpectedPlanningPaths | Sort-Object) -join "`n")) { throw 'setup receipt planning path allowlist mismatch' }
if ((@(git -C $PlanningRepo rev-list --reverse "$OnboardingBase..$LocalPlanningHead") -join "`n") -ne (@($PlanningCommits) -join "`n")) { throw 'setup receipt planning commit sequence mismatch' }
if (-not (Test-Path -LiteralPath $OnboardingRoot)) {
  $null = git -C $PlanningRepo show-ref --verify --quiet "refs/heads/$OnboardingBranch"
  if ($LASTEXITCODE -eq 0) {
    git -C $PlanningRepo worktree add $OnboardingRoot $OnboardingBranch
  } else {
    git -C $PlanningRepo worktree add -b $OnboardingBranch $OnboardingRoot $OnboardingBase
  }
  if ($LASTEXITCODE -ne 0) { throw 'isolated onboarding worktree creation failed' }
}
$GitRoot = [IO.Path]::GetFullPath((git -C $OnboardingRoot rev-parse --show-toplevel).Trim()).TrimEnd('\')
$ExpectedRoot = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $OnboardingRoot).Path).TrimEnd('\')
if (-not $GitRoot.Equals($ExpectedRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'onboarding worktree root mismatch' }
$OnboardingMergeBase = (git -C $OnboardingRoot merge-base $OnboardingBase HEAD).Trim()
if ($OnboardingMergeBase -ne $OnboardingBase) { throw 'onboarding branch is not based on captured default tip' }
```

이제 `$OnboardingRoot`에서 exact 두 파일만 편집·commit한다. 그 뒤 아래 검증/PR/merge/dispatch block을 새 PowerShell 세션에서 실행한다.

```powershell
$PlanningRepo = 'C:\01.project\INFINITY_FORGE'
$TrustedPython = 'C:\01.project\INFINITY_FORGE\.venv\Scripts\python.exe'
$OnboardingBranch = 'codex/ci-onboarding'
$StateRoot = Join-Path $env:LOCALAPPDATA 'InfinityForge\state'
$SetupReceiptPath = Join-Path $StateRoot 'ci-onboarding-setup.json'
if (-not (Test-Path -LiteralPath $SetupReceiptPath -PathType Leaf)) { throw 'onboarding setup receipt is missing' }
$Setup = Get-Content -Raw -LiteralPath $SetupReceiptPath | ConvertFrom-Json
Push-Location $PlanningRepo
try {
  $CanonicalRepo = (gh repo view --json nameWithOwner --jq .nameWithOwner).Trim()
} finally {
  Pop-Location
}
if ($Setup.schema_version -ne 'forge-ci-onboarding-setup/v1' -or $Setup.repository -ne $CanonicalRepo -or $Setup.onboarding_branch -ne $OnboardingBranch) { throw 'onboarding setup receipt identity mismatch' }
$LocalPlanningHead = [string]$Setup.planning_head_sha
$OnboardingBase = [string]$Setup.onboarding_base_sha
$PlanningCommits = @($Setup.planning_commits | ForEach-Object { [string]$_ })
$RepoRoot = [string]$Setup.onboarding_root
if ((git -C $PlanningRepo rev-parse HEAD).Trim() -ne $LocalPlanningHead) { throw 'planning head changed after setup' }
Push-Location $RepoRoot
try { & $TrustedPython -m pytest tests/test_workflow_onboarding_contract.py -q } finally { Pop-Location }
if ((git -C $RepoRoot branch --show-current).Trim() -ne $OnboardingBranch) { throw 'wrong onboarding branch' }
if (@(git -C $RepoRoot status --porcelain).Count -ne 0) { throw 'onboarding worktree is dirty' }
$OnboardingHead = (git -C $RepoRoot rev-parse HEAD).Trim()
$AllowedOnboardingPaths = @('.github/workflows/capability-eval.yml','tests/test_workflow_onboarding_contract.py')
$LocalPaths = @(git -C $RepoRoot diff --name-only "$OnboardingBase...$OnboardingHead")
if ((@($LocalPaths | Sort-Object) -join "`n") -ne (@($AllowedOnboardingPaths | Sort-Object) -join "`n")) { throw 'onboarding local diff allowlist mismatch' }
$OnboardingWorkflowBlob = (git -C $RepoRoot rev-parse "$OnboardingHead`:.github/workflows/capability-eval.yml").Trim()
git -C $RepoRoot push --set-upstream origin $OnboardingBranch
$Candidates = @(gh pr list --repo $CanonicalRepo --head $OnboardingBranch --state all --limit 100 --json number,headRefOid,state | ConvertFrom-Json)
$SameHead = @($Candidates | Where-Object { $_.headRefOid -eq $OnboardingHead })
if ($SameHead.Count -gt 1) { throw 'duplicate onboarding PR for exact head' }
if ($SameHead.Count -eq 0) {
  $PrUrl = gh pr create --repo $CanonicalRepo --base main --head $OnboardingBranch --title 'ci: onboard stable guard checks' --body 'Installs the two stable cross-platform check names before the first protected Forge task.'
  $OnboardingPr = [int](gh pr view $PrUrl --repo $CanonicalRepo --json number --jq .number)
} else {
  $OnboardingPr = [int]$SameHead[0].number
}
$Onboarding = gh pr view $OnboardingPr --repo $CanonicalRepo --json state,baseRefName,baseRefOid,headRefOid,statusCheckRollup,mergeCommit | ConvertFrom-Json
if ($Onboarding.headRefOid -ne $OnboardingHead) { throw 'onboarding PR head mismatch' }
$CurrentPrBase = (gh api "repos/$CanonicalRepo/commits/main" --jq .sha).Trim()
$BaseAdvance = (gh api "repos/$CanonicalRepo/compare/$OnboardingBase...$CurrentPrBase" --jq .status).Trim()
if ($BaseAdvance -notin @('identical','ahead')) { throw 'current PR base is not descended from isolated onboarding base' }
if ($Onboarding.baseRefName -ne 'main' -or $Onboarding.baseRefOid -ne $CurrentPrBase) { throw 'onboarding PR base read-back raced; retry before merge' }
if ($Onboarding.state -eq 'CLOSED') { throw 'onboarding PR was closed without merge' }
$FilePages = gh api --paginate --slurp "repos/$CanonicalRepo/pulls/$OnboardingPr/files?per_page=100" | ConvertFrom-Json
$RemotePaths = @($FilePages | ForEach-Object { @($_) } | ForEach-Object { $_ } | ForEach-Object { [string]$_.filename })
if ((@($RemotePaths | Sort-Object) -join "`n") -ne (@($AllowedOnboardingPaths | Sort-Object) -join "`n")) { throw 'onboarding remote PR file allowlist mismatch' }
$Expected = @('guard-contract (ubuntu-latest)', 'guard-contract (windows-latest)')
foreach ($Name in $Expected) {
  $Matches = @($Onboarding.statusCheckRollup | Where-Object { $_.name -eq $Name })
  if ($Matches.Count -ne 1 -or $Matches[0].conclusion -ne 'SUCCESS') { throw "onboarding check mismatch: $Name" }
}
$Merged = if ($Onboarding.state -eq 'MERGED') { $Onboarding } else { $null }
if (-not $Merged) {
  gh pr merge $OnboardingPr --repo $CanonicalRepo --merge --match-head-commit $OnboardingHead
  if ($LASTEXITCODE -ne 0) { throw 'onboarding merge failed' }
  for ($Attempt = 0; $Attempt -lt 60 -and -not $Merged; $Attempt++) {
    Start-Sleep -Seconds 5
    $Observed = gh pr view $OnboardingPr --repo $CanonicalRepo --json state,mergeCommit,headRefOid | ConvertFrom-Json
    if ($Observed.state -eq 'MERGED') { $Merged = $Observed }
  }
}
if ($Merged.state -ne 'MERGED' -or $Merged.headRefOid -ne $OnboardingHead) { throw 'onboarding PR merge read-back failed' }
$MainSha = [string]$Merged.mergeCommit.oid
$CurrentMainSha = (gh api "repos/$CanonicalRepo/commits/main" --jq .sha).Trim()
$CompareStatus = (gh api "repos/$CanonicalRepo/compare/$MainSha...$CurrentMainSha" --jq .status).Trim()
if ($CompareStatus -notin @('identical','ahead')) { throw 'onboarding merge is not an ancestor of current main' }
$CurrentWorkflowBlob = (gh api "repos/$CanonicalRepo/contents/.github/workflows/capability-eval.yml?ref=$CurrentMainSha" --jq .sha).Trim()
if ($CurrentWorkflowBlob -ne $OnboardingWorkflowBlob) { throw 'current main workflow blob differs from reviewed onboarding head' }
$StateRoot = Join-Path $env:LOCALAPPDATA 'InfinityForge\state'
$RunReceiptPath = Join-Path $StateRoot 'ci-onboarding-run.json'
$OnboardingRun = $null
if (Test-Path -LiteralPath $RunReceiptPath) {
  $SavedRun = Get-Content -Raw -LiteralPath $RunReceiptPath | ConvertFrom-Json
  if (
    $SavedRun.schema_version -ne 'forge-ci-onboarding-run/v3' -or
    $SavedRun.repository -ne $CanonicalRepo -or
    $SavedRun.onboarding_base_sha -ne $OnboardingBase -or
    $SavedRun.onboarding_head_sha -ne $OnboardingHead -or
    $SavedRun.onboarding_merge_sha -ne $MainSha -or
    $SavedRun.workflow_blob_sha -ne $OnboardingWorkflowBlob
  ) { throw 'onboarding receipt identity mismatch' }
  if ($SavedRun.dispatch_head_sha -eq $CurrentMainSha) {
    $OnboardingRun = [long]$SavedRun.run_id
  }
}
if (-not $OnboardingRun) {
  $Runs = @(gh run list --repo $CanonicalRepo --workflow capability-eval.yml --branch main --event workflow_dispatch --limit 100 --json databaseId,createdAt,headSha,event,status,conclusion | ConvertFrom-Json)
  $Reusable = @($Runs | Where-Object { $_.headSha -eq $CurrentMainSha -and $_.event -eq 'workflow_dispatch' -and $_.conclusion -eq 'success' } | Sort-Object createdAt -Descending)
  if ($Reusable.Count -gt 0) {
    $OnboardingRun = [long]$Reusable[0].databaseId
  } else {
    $DispatchStarted = [DateTimeOffset]::UtcNow.AddSeconds(-2)
    gh workflow run capability-eval.yml --repo $CanonicalRepo --ref main
    for ($Attempt = 0; $Attempt -lt 30 -and -not $OnboardingRun; $Attempt++) {
      Start-Sleep -Seconds 2
      $Runs = @(gh run list --repo $CanonicalRepo --workflow capability-eval.yml --branch main --event workflow_dispatch --limit 100 --json databaseId,createdAt,headSha,event | ConvertFrom-Json)
      $NewRuns = @($Runs | Where-Object { $_.headSha -eq $CurrentMainSha -and $_.event -eq 'workflow_dispatch' -and [DateTimeOffset]$_.createdAt -ge $DispatchStarted } | Sort-Object createdAt -Descending)
      if ($NewRuns.Count -gt 0) { $OnboardingRun = [long]$NewRuns[0].databaseId }
    }
  }
}
if (-not $OnboardingRun) { throw 'new onboarding workflow_dispatch run was not found' }
gh run watch $OnboardingRun --repo $CanonicalRepo --exit-status
$Run = gh run view $OnboardingRun --repo $CanonicalRepo --json headSha,jobs | ConvertFrom-Json
if ($Run.headSha -ne $CurrentMainSha) { throw 'onboarding dispatch current-main SHA mismatch; rerun after main settles' }
foreach ($Name in $Expected) {
  $Matches = @($Run.jobs | Where-Object { $_.name -eq $Name })
  if ($Matches.Count -ne 1 -or $Matches[0].conclusion -ne 'success') { throw "onboarding dispatch check mismatch: $Name" }
}
New-Item -ItemType Directory -Force -Path $StateRoot | Out-Null
$RunReceiptValue = [ordered]@{
  schema_version = 'forge-ci-onboarding-run/v3'
  stage = 'dispatch_verified'
  repository = $CanonicalRepo
  onboarding_base_sha = $OnboardingBase
  onboarding_head_sha = $OnboardingHead
  onboarding_pr = $OnboardingPr
  onboarding_merge_sha = $MainSha
  workflow_blob_sha = $OnboardingWorkflowBlob
  dispatch_head_sha = $CurrentMainSha
  run_id = $OnboardingRun
  implementation_base_sha = $null
  implementation_head_sha = $null
  implementation_branch = $null
  implementation_root = $null
  planning_commits = $PlanningCommits
}
$RunReceipt = [Text.Encoding]::UTF8.GetBytes(($RunReceiptValue | ConvertTo-Json -Compress) + "`n")
$RunReceiptTemp = "$RunReceiptPath.$([Guid]::NewGuid().ToString('N')).tmp"
$Stream = [IO.File]::Open($RunReceiptTemp, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
try { $Stream.Write($RunReceipt, 0, $RunReceipt.Length); $Stream.Flush($true) } finally { $Stream.Dispose() }
Move-Item -LiteralPath $RunReceiptTemp -Destination $RunReceiptPath -Force

function Write-AtomicJsonFile([string]$Path, [object]$Value) {
  $Bytes = [Text.Encoding]::UTF8.GetBytes(($Value | ConvertTo-Json -Depth 20 -Compress) + "`n")
  $Temp = "$Path.$([Guid]::NewGuid().ToString('N')).tmp"
  $File = [IO.File]::Open($Temp, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
  try { $File.Write($Bytes, 0, $Bytes.Length); $File.Flush($true) } finally { $File.Dispose() }
  Move-Item -LiteralPath $Temp -Destination $Path -Force
}

$FeatureBranch = 'codex/hermes-early-termination-guards'
$FeatureSetupPath = Join-Path $StateRoot 'hermes-guards-feature-setup.json'
if (Test-Path -LiteralPath $FeatureSetupPath) {
  $SavedFeature = Get-Content -Raw -LiteralPath $FeatureSetupPath | ConvertFrom-Json
  if (
    $SavedFeature.schema_version -ne 'forge-feature-setup/v1' -or
    $SavedFeature.repository -ne $CanonicalRepo -or
    $SavedFeature.planning_head_sha -ne $LocalPlanningHead -or
    $SavedFeature.branch -ne $FeatureBranch
  ) { throw 'feature setup receipt identity mismatch' }
  $FeatureBase = [string]$SavedFeature.feature_base_sha
  $FeatureRoot = [string]$SavedFeature.feature_root
} else {
  $FeatureBase = $CurrentMainSha
  $FeatureRoot = Join-Path $env:LOCALAPPDATA "InfinityForge\worktrees\hermes-guards-$FeatureBase"
  $PreparedFeature = [ordered]@{schema_version='forge-feature-setup/v1';stage='prepared';repository=$CanonicalRepo;feature_base_sha=$FeatureBase;planning_head_sha=$LocalPlanningHead;planning_commits=$PlanningCommits;applied_source_commits=@();branch=$FeatureBranch;feature_root=$FeatureRoot;feature_head_sha=$null}
  Write-AtomicJsonFile $FeatureSetupPath $PreparedFeature
}
if ((@($PlanningCommits) -join "`n") -ne (@($Setup.planning_commits | ForEach-Object { [string]$_ }) -join "`n")) { throw 'feature/setup planning commit sequence mismatch' }
if (-not (Test-Path -LiteralPath $FeatureRoot)) {
  $null = git -C $PlanningRepo show-ref --verify --quiet "refs/heads/$FeatureBranch"
  if ($LASTEXITCODE -eq 0) {
    git -C $PlanningRepo worktree add $FeatureRoot $FeatureBranch
  } else {
    git -C $PlanningRepo worktree add -b $FeatureBranch $FeatureRoot $FeatureBase
  }
  if ($LASTEXITCODE -ne 0) { throw 'implementation worktree creation failed' }
}
$FeatureGitRoot = [IO.Path]::GetFullPath((git -C $FeatureRoot rev-parse --show-toplevel).Trim()).TrimEnd('\')
$FeatureExpectedRoot = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $FeatureRoot).Path).TrimEnd('\')
if (-not $FeatureGitRoot.Equals($FeatureExpectedRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'implementation worktree root mismatch' }
if ((git -C $FeatureRoot branch --show-current).Trim() -ne $FeatureBranch) { throw 'implementation branch mismatch' }
if ((git -C $FeatureRoot merge-base $FeatureBase HEAD).Trim() -ne $FeatureBase) { throw 'implementation branch base mismatch' }
$CherryPickHead = (git -C $FeatureRoot rev-parse --git-path CHERRY_PICK_HEAD).Trim()
if (-not [IO.Path]::IsPathRooted($CherryPickHead)) { $CherryPickHead = Join-Path $FeatureRoot $CherryPickHead }
if (Test-Path -LiteralPath $CherryPickHead) {
  git -C $FeatureRoot cherry-pick --abort
  if ($LASTEXITCODE -ne 0) { throw 'owned interrupted planning cherry-pick recovery failed' }
}
if (@(git -C $FeatureRoot status --porcelain).Count -ne 0) { throw 'implementation worktree has non-journaled changes' }
$FeatureCommits = @(git -C $FeatureRoot rev-list --reverse "$FeatureBase..HEAD")
$AppliedFromGit = @()
foreach ($FeatureCommit in $FeatureCommits) {
  $Message = (git -C $FeatureRoot show -s --format=%B $FeatureCommit) -join "`n"
  $SourceMarkers = [regex]::Matches($Message, '\(cherry picked from commit ([0-9a-f]{40})\)')
  if ($SourceMarkers.Count -ne 1) { throw 'implementation branch contains a non-journaled commit' }
  $AppliedFromGit += $SourceMarkers[0].Groups[1].Value
}
if ($AppliedFromGit.Count -gt $PlanningCommits.Count) { throw 'too many implementation planning commits' }
for ($Index = 0; $Index -lt $AppliedFromGit.Count; $Index++) {
  if ($AppliedFromGit[$Index] -ne $PlanningCommits[$Index]) { throw 'planning cherry-pick sequence is not an exact prefix' }
}
for ($Index = $AppliedFromGit.Count; $Index -lt $PlanningCommits.Count; $Index++) {
  git -C $FeatureRoot cherry-pick -x $PlanningCommits[$Index]
  if ($LASTEXITCODE -ne 0) { throw "planning cherry-pick failed: $($PlanningCommits[$Index])" }
  $AppliedFromGit += $PlanningCommits[$Index]
  $PartialFeature = [ordered]@{schema_version='forge-feature-setup/v1';stage='cherry_picking';repository=$CanonicalRepo;feature_base_sha=$FeatureBase;planning_head_sha=$LocalPlanningHead;planning_commits=$PlanningCommits;applied_source_commits=$AppliedFromGit;branch=$FeatureBranch;feature_root=$FeatureRoot;feature_head_sha=(git -C $FeatureRoot rev-parse HEAD).Trim()}
  Write-AtomicJsonFile $FeatureSetupPath $PartialFeature
}
$FeatureHead = (git -C $FeatureRoot rev-parse HEAD).Trim()
$FeaturePaths = @(git -C $FeatureRoot diff --name-only "$FeatureBase...$FeatureHead")
if ((@($FeaturePaths | Sort-Object) -join "`n") -ne (@($ExpectedPlanningPaths | Sort-Object) -join "`n")) { throw 'implementation planning path allowlist mismatch' }
foreach ($Path in $ExpectedPlanningPaths) {
  $PlanningBlob = (git -C $PlanningRepo rev-parse "$LocalPlanningHead`:$Path").Trim()
  $FeatureBlob = (git -C $FeatureRoot rev-parse "$FeatureHead`:$Path").Trim()
  if ($FeatureBlob -ne $PlanningBlob) { throw "implementation planning blob mismatch: $Path" }
}
if (@(git -C $FeatureRoot status --porcelain).Count -ne 0) { throw 'implementation worktree is not clean after planning transfer' }
$CompleteFeature = [ordered]@{schema_version='forge-feature-setup/v1';stage='complete';repository=$CanonicalRepo;feature_base_sha=$FeatureBase;planning_head_sha=$LocalPlanningHead;planning_commits=$PlanningCommits;applied_source_commits=$AppliedFromGit;branch=$FeatureBranch;feature_root=$FeatureRoot;feature_head_sha=$FeatureHead}
Write-AtomicJsonFile $FeatureSetupPath $CompleteFeature
$FeatureSetupSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $FeatureSetupPath).Hash.ToLowerInvariant()
$RunReceiptValue['stage'] = 'complete'
$RunReceiptValue['implementation_base_sha'] = $FeatureBase
$RunReceiptValue['implementation_head_sha'] = $FeatureHead
$RunReceiptValue['implementation_branch'] = $FeatureBranch
$RunReceiptValue['implementation_root'] = $FeatureRoot
$RunReceiptValue['implementation_setup_sha256'] = $FeatureSetupSha256
Write-AtomicJsonFile $RunReceiptPath $RunReceiptValue
```

**Gate:** `ci-onboarding-run/v3 stage=complete`와 그 안에 결합된 feature setup digest, current-main dispatch의 두 stable check 실제 URL/success, external implementation worktree의 exact base/6 planning blobs/clean status가 모두 맞지 않으면 Task 1 이하를 시작하지 않는다. 첫 protected feature PR 생성 전에는 Task 1이 canonical host의 `FORGE_OPS_HOST|FORGE_BOOTSTRAP_REPOSITORY|FORGE_BOOTSTRAP_ISSUE`와 secondary `FORGE_OPS_HOST=false`를 set/read-back해야 하며, Task 0 check와 Task 1 variable provisioning을 모두 만족해야 bootstrap 완료다.

## Task 1: 승인 결정과 문서 상태를 고정한다

**Files:**
- Verify: `docs/weapon/specs/2026-07-12-hermes-early-termination-guards-design.md`
- Modify: `docs/plan.md`
- Modify: `forge/skills/kanban-codex-delegate/SKILL.md`

**Interfaces:**

```text
docs/plan.md decision IDs: D25, D26, D27, D28, D29, D30
executor card CLI invariants: tenant=forge, goal=true, goal_max_turns=20,
  max_runtime=60m, max_retries=4, completion_policy=forge-v1
residual materialization kinds: issue | adr_issue
bootstrap-request.json: ops_host_repository, bootstrap_issue, repositories(unique canonical OWNER/REPO array)
```

**Steps:**

- [x] spec 상태를 `사용자 승인 완료`로 바꾸고 승인일과 선택안 2를 기록했다. 이 변경은 계획 문서와 함께 commit한다.
- [ ] implementation worktree 생성 전에 canonical `bootstrap-request.json`을 secret scan하고, marker `forge-hermes-early-termination-bootstrap-v1`을 가진 issue를 모든 page에서 exact-one 조회해 없으면 생성하고 하나면 재사용하며 둘 이상이면 중단한다. issue body에는 D25~D30과 stable AC 20개를 기록한다.
- [ ] bootstrap issue가 존재하는 canonical INFINITY_FORGE repository 하나를 ops host로 확정한다. 그 repo에만 `FORGE_OPS_HOST=true`, `FORGE_BOOTSTRAP_REPOSITORY=OWNER/REPO`, `FORGE_BOOTSTRAP_ISSUE=ISSUE_NUMBER`를 설정하고 explicit `--repo OWNER/REPO` read-back을 수행한다. secondary repository에는 `FORGE_OPS_HOST=false`만 설정하고 중앙 issue 번호를 복사하지 않는다. ops host provisioning 실패면 issue에 blocked comment를 남기고 producer/CI/rollout을 시작하지 않는다. 재실행은 같은 marker issue/repository를 재사용한다. `FORGE_DEPLOYED_SHA`는 세 target 배포+evidence publish가 끝나는 Task 16/rollout에서만 설정한다.

```powershell
$BootstrapRequestPath = Join-Path $env:LOCALAPPDATA 'InfinityForge\state\bootstrap-request.json'
$Bootstrap = Get-Content -Raw -LiteralPath $BootstrapRequestPath | ConvertFrom-Json
$OpsHostRepository = [string]$Bootstrap.ops_host_repository
$BootstrapIssue = [int]$Bootstrap.bootstrap_issue
$ContractRepositories = @($Bootstrap.repositories | Sort-Object -Unique)
if ($ContractRepositories.Count -ne @($Bootstrap.repositories).Count) { throw 'duplicate contract repository' }
if ($ContractRepositories -notcontains $OpsHostRepository) { throw 'ops host is not in bootstrap contract' }
foreach ($Repository in $ContractRepositories) {
  if ($Repository -eq $OpsHostRepository) {
    gh variable set FORGE_OPS_HOST --repo $Repository --body true
    gh variable set FORGE_BOOTSTRAP_REPOSITORY --repo $Repository --body $OpsHostRepository
    gh variable set FORGE_BOOTSTRAP_ISSUE --repo $Repository --body ([string]$BootstrapIssue)
    if ((gh variable get FORGE_OPS_HOST --repo $Repository --json value --jq .value).Trim() -ne 'true') { throw 'ops host flag read-back failed' }
    if ((gh variable get FORGE_BOOTSTRAP_REPOSITORY --repo $Repository --json value --jq .value).Trim() -ne $OpsHostRepository) { throw 'bootstrap repository read-back failed' }
    if ([int](gh variable get FORGE_BOOTSTRAP_ISSUE --repo $Repository --json value --jq .value) -ne $BootstrapIssue) { throw 'bootstrap issue read-back failed' }
  } else {
    gh variable set FORGE_OPS_HOST --repo $Repository --body false
    $Names = @(gh variable list --repo $Repository --json name --jq '.[].name')
    foreach ($Name in @('FORGE_BOOTSTRAP_REPOSITORY','FORGE_BOOTSTRAP_ISSUE','FORGE_DEPLOYED_SHA')) {
      if ($Names -contains $Name) { gh variable delete $Name --repo $Repository }
    }
    if ((gh variable get FORGE_OPS_HOST --repo $Repository --json value --jq .value).Trim() -ne 'false') { throw "secondary ops flag read-back failed: $Repository" }
  }
  if ($LASTEXITCODE -ne 0) { throw "repository variable provisioning failed: $Repository" }
}
```
- [ ] `docs/plan.md`의 기존 D1~D24를 수정하지 않고 D25~D30을 새 결정으로 추가한다.
- [ ] D25 원자 completion receipt, D26 runner ledger, D27 issue/ADR residual, D28 task-local GitHub GATE_ERROR, D29 gateway/dispatcher split, D30 private/free GitHub 경계를 정확히 옮긴다.
- [ ] 17절의 `--max-retries 3`, card-only 잔여, bash-only Stop gate 문구가 새 결정으로 대체됐음을 새 단락에서 명시한다.
- [ ] delegate skill의 생성 계약을 `tenant=forge`, `goal=true`, `goal_max_turns=20`, `max_runtime=60m`, `max_retries=4`, `completion_policy=forge-v1`로 수정한다.
- [ ] residual은 GitHub issue/`forge:adr` issue만 허용하고 completion receipt 없이는 complete를 호출하지 않도록 명시한다.
- [ ] delegate skill의 실행 경로를 raw `codex exec`에서 `forge-guard prepare --request <request> → forge-guard run --task-id <id> → outcome=complete_ready 확인 → kanban_complete`로 교체한다. `TESTS_FAILED`, `GATE_ERROR`, `retry_exhausted` outcome에서는 `kanban_complete`를 호출하지 않는다.
- [ ] 문서 diff를 검토한다.

```powershell
rg -n "D25|D26|D27|D28|D29|D30|completion_policy|goal_max_turns|max_retries" docs forge/skills/kanban-codex-delegate/SKILL.md
git diff --check
```

예상: D25~D30은 각각 한 번 정의되고 기존 D1~D24 문장은 유지된다.

- [ ] Commit:

```powershell
git add docs/plan.md forge/skills/kanban-codex-delegate/SKILL.md
git commit -m "docs: approve Hermes completion guard decisions"
```

## Task 2: JSON 계약과 Python 패키지 경계를 만든다

**Files:**
- Create: `pyproject.toml`
- Create: `requirements.lock`
- Create: `forge/__init__.py`
- Create: `forge/guard/__init__.py`
- Create: `forge/guard/errors.py`
- Create: `forge/guard/contract.py`
- Create: `forge/schemas/task-contract-v1.schema.json`
- Create: `forge/schemas/handoff-v1.schema.json`
- Create: `forge/schemas/receipt-v1.schema.json`
- Create: `forge/schemas/runner-state-v1.schema.json`
- Create: `tests/guard/conftest.py`
- Create: `tests/guard/test_contract.py`
- Modify: `.gitignore`

**Interfaces:**

```text
Budget(max_sessions: int, max_runtime_s: int, max_tokens: int)
parse_task_contract(data: Mapping[str, object]) -> TaskContract
parse_handoff(data: Mapping[str, object], contract: TaskContract) -> Handoff
canonical_json_bytes(value: object) -> bytes
sha256_json(value: object) -> str
```

**Steps:**

- [ ] `pyproject.toml`에 Python `>=3.11`, runtime `jsonschema>=4.23,<5`, test `pytest>=8,<9`를 선언하고 console script `forge-guard = forge.guard.cli:main`을 둔다. `requirements.lock`에는 runtime transitive dependency까지 exact version과 package hash를 기록한다.
- [ ] `.gitignore`에 `.venv/`, `.codex/*`를 추가한다. Hermes development clone과 Task 0 onboarding worktree는 모두 repository 밖에 두며 `.worktrees/` scanner exclude나 ignore로 숨기지 않는다. tracked hook template은 `forge/hooks/`에 두므로 예외가 필요 없다.
- [ ] 먼저 다음 RED tests를 작성한다.

Test inventory:

- `test_missing_handoff_is_tests_failed`
- `test_wrong_handoff_field_type_is_tests_failed`
- `test_wrong_immutable_contract_field_type_is_gate_error`
- `test_acceptance_partition_is_exact_and_disjoint`
- `test_verified_by_covers_every_implemented_criterion`
- `test_empty_not_implemented_is_valid`
- `test_card_only_materialization_is_rejected`
- `test_command_timeout_sum_must_fit_task_and_hook_budget`
- `test_local_runtime_state_is_gitignored_and_no_nested_worktree_root_exists`

대표 test body는 다음 인터페이스를 고정한다.

```python
def test_empty_not_implemented_is_valid(valid_contract_data, valid_handoff_data):
    contract = parse_task_contract(valid_contract_data)
    valid_handoff_data["not_implemented"] = []
    handoff = parse_handoff(valid_handoff_data, contract)
    assert handoff.not_implemented == ()
```

- [ ] 별도 repo `.venv`를 만들고 RED를 확인한다.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest tests/guard/test_contract.py -q
```

예상 RED: `forge.guard.contract` import 또는 schema 파일 부재로 실패한다.

- [ ] `FailureKind`, `TestsFailed`, `GateError`, frozen dataclass와 canonical JSON SHA-256 함수를 구현한다.
- [ ] 네 schema를 `additionalProperties:false`로 작성하고 `$id`에 version을 포함한다.
- [ ] AC ID union=전체, intersection=empty, `verified_by` coverage, issue/ADR-only residual, argv-only, budget 상한을 구현한다.
- [ ] receipt에는 phase, verifier SHA, task/run, contract/handoff/repo/command/reference/session digest, issued/expires를 필수로 둔다. `phase=hermes` 외 receipt는 DB 소비 입력으로 거절한다.
- [ ] GREEN과 전체 회귀를 확인한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_contract.py -q
.\.venv\Scripts\python.exe -m pytest tests -q
```

- [ ] Commit:

```powershell
git add .gitignore pyproject.toml requirements.lock forge/__init__.py forge/guard forge/schemas tests/guard
git commit -m "feat: define Forge completion contracts"
```

## Task 3: 다중 repository baseline과 diff 증거를 구현한다

**Files:**
- Create: `forge/guard/git_state.py`
- Create: `forge/guard/repo_capability.py`
- Create: `tests/guard/test_git_state.py`
- Create: `tests/guard/test_repo_capability.py`
- Modify: `forge/guard/contract.py`

**Interfaces:**

```text
capture_baseline(path: Path) -> RepositoryContract
inspect_repository(repo: RepositoryContract, require_clean: bool = True) -> RepositoryState
validate_changed_files(repositories: Sequence[RepositoryState], handoff: Handoff) -> None
```

**Steps:**

- [ ] temp Git repo fixture와 다음 RED tests를 작성한다.

Test inventory:

- `test_prepare_rejects_preexisting_dirty_tree`
- `test_committed_clean_change_since_baseline_passes`
- `test_empty_change_fails`
- `test_handoff_file_only_change_fails`
- `test_changed_files_are_set_equal_to_git_diff`
- `test_every_multi_repo_entry_has_baseline_and_matching_pr`
- `test_remote_branch_and_baseline_ancestry_cannot_drift`
- `test_prepare_rejects_repository_without_guard_ci_workflow_and_named_checks`

- [ ] RED를 확인한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_git_state.py -q
```

예상 RED: `capture_baseline`과 `inspect_repository`가 없다.

- [ ] `capture_baseline(path)`가 clean tree, branch, remote, HEAD SHA를 기록하도록 구현한다.
- [ ] `inspect_repository(repo, require_clean=True)`가 baseline object 존재, ancestor 관계, remote/branch 고정, non-empty committed diff를 검증하도록 구현한다.
- [ ] `git diff --name-status -z <baseline>..<HEAD>`를 파싱하고 handoff changed files와 repo/path set-equal을 검사한다.
- [ ] path traversal, symlink escape, state-root evidence만 바뀐 경우를 거절한다.
- [ ] baseline 전에 모든 repository default branch의 guard workflow와 두 stable named checks를 확인하고 미온보딩 repo를 `REPO_GUARD_CI_NOT_ONBOARDED` GATE_ERROR로 보류한다.
- [ ] GREEN과 전체 회귀를 확인한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_git_state.py -q
.\.venv\Scripts\python.exe -m pytest tests/guard -q
```

- [ ] Commit:

```powershell
git add forge/guard/contract.py forge/guard/git_state.py forge/guard/repo_capability.py tests/guard/test_git_state.py tests/guard/test_repo_capability.py
git commit -m "feat: bind completion to repository baselines"
```

## Task 4: GitHub reference와 명령 evidence를 fail-loud로 만든다

**Files:**
- Create: `forge/guard/references.py`
- Create: `forge/guard/commands.py`
- Create: `tests/guard/test_references.py`
- Create: `tests/guard/test_commands.py`

**Interfaces:**

```text
GhCliClient.get_issue(repo: str, number: int) -> IssueRecord
GhCliClient.get_pull(repo: str, number: int) -> PullRecord
verify_references(contract: TaskContract, handoff: Handoff, gh: GhClient) -> ReferenceEvidence
run_verification_commands(contract: TaskContract, state_dir: Path) -> Sequence[CommandEvidence]
```

**Steps:**

- [ ] injectable fake `GhCliClient`와 subprocess fixture를 사용해 RED tests를 작성한다.

Test inventory:

- `test_open_issue_materialization_passes`
- `test_adr_requires_open_state_and_forge_adr_label`
- `test_explicit_404_is_tests_failed`
- `test_auth_rate_limit_server_and_invalid_json_are_gate_error`
- `test_pr_repo_head_sha_open_and_non_draft_must_match`
- `test_command_argv_runs_without_shell_and_writes_mode_600_evidence`
- `test_nonzero_test_is_tests_failed`
- `test_missing_binary_and_timeout_are_gate_error`
- `test_secret_environment_is_removed_from_child`

- [ ] RED를 확인한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_references.py tests/guard/test_commands.py -q
```

- [ ] `GhCliClient.get_issue`, `get_pull`, paginated list helper를 argv 기반으로 구현하고 HTTP status를 분류한다.
- [ ] source issue body hash와 AC text hash를 fetched JSON에서 재계산한다.
- [ ] residual issue는 open, ADR은 open+`forge:adr`, PR은 repo/head SHA/open/non-draft로 고정한다.
- [ ] command runner는 repo cwd, scrubbed env, timeout, stdout/stderr digest와 redacted tail을 trusted state에 mode 600으로 기록한다.
- [ ] GitHub/command 오류에서 빈 목록이나 0건을 반환하는 fallback이 없음을 검증한다.
- [ ] GREEN과 전체 회귀를 확인한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_references.py tests/guard/test_commands.py -q
.\.venv\Scripts\python.exe -m pytest tests/guard -q
```

- [ ] Commit:

```powershell
git add forge/guard/references.py forge/guard/commands.py tests/guard/test_references.py tests/guard/test_commands.py
git commit -m "feat: verify GitHub and command evidence"
```

## Task 5: 공통 verifier와 replay-safe receipt를 구현한다

**Files:**
- Create: `forge/guard/verifier.py`
- Create: `forge/schemas/hermes-completion-result-v1.schema.json`
- Create: `tests/guard/test_verifier.py`
- Modify: `forge/guard/contract.py`
- Modify: `forge/schemas/receipt-v1.schema.json`

**Interfaces:**

```text
VerificationContext(
  phase: Literal["stop", "post-exit", "ci", "hermes"],
  deployed_sha: str,
  state_dir: Path,
  session_usage: SessionUsage
)
verify(contract: TaskContract, handoff: Handoff, context: VerificationContext) -> VerificationResult
```

**Steps:**

- [ ] 다음 RED tests를 작성한다.

Test inventory:

- `test_valid_contract_handoff_repos_refs_commands_issue_receipt`
- `test_receipt_binds_task_run_contract_handoff_repo_and_deployed_sha`
- `test_receipt_expiry_is_phase_specific`
- `test_only_hermes_phase_receipt_is_consumable`
- `test_hermes_cli_result_matches_forge_completion_result_v1`
- `test_stale_mismatched_cross_task_and_replayed_receipts_fail`
- `test_unexpected_exception_becomes_gate_error`
- `test_failure_never_returns_partial_receipt`

- [ ] RED를 확인한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_verifier.py -q
```

- [ ] 검증 순서를 contract→budget→handoff→git→references/PR→commands→receipt로 고정한다.
- [ ] 모든 digest는 canonical JSON으로 만들고 repo별 baseline/HEAD/tree/empty-working digest를 포함한다.
- [ ] command ID가 immutable contract argv를 참조하도록 하고 handoff의 mutable command 문자열 중복을 금지한다.
- [ ] `phase=hermes` CLI는 `forge-completion-request/v1`을 받아 exact `forge-completion-result/v1` allow/deny JSON을 만들고 receipt digest, version, repository-state digest, running artifact SHA를 Hermes consumer 이름으로 변환한다.
- [ ] PASS일 때만 phase별 expiry receipt를 발급하고 다른 결과의 `receipt`는 `None`으로 유지한다. stop/post-exit/ci receipt는 evidence이며 Hermes phase의 15분 receipt만 final consumable이다.
- [ ] RISK 주석을 추가한다.

```python
# RISK(race): verifier preflight와 Hermes DB commit 사이 외부 repo 변경은
# short expiry, dedicated workspace, expected run CAS로 완화한다.
```

- [ ] GREEN과 전체 회귀를 확인한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_verifier.py -q
.\.venv\Scripts\python.exe -m pytest tests/guard -q
```

- [ ] Commit:

```powershell
git add forge/guard/verifier.py forge/guard/contract.py forge/schemas/receipt-v1.schema.json forge/schemas/hermes-completion-result-v1.schema.json tests/guard/test_verifier.py
git commit -m "feat: issue completion receipts from shared verifier"
```

## Task 6: 원자 state와 Codex session runner를 구현한다

**Files:**
- Create: `forge/guard/state.py`
- Create: `forge/guard/runner.py`
- Create: `tests/guard/test_state.py`
- Create: `tests/guard/test_runner.py`

**Interfaces:**

```text
atomic_write_json(path: Path, data: Mapping[str, object], permission: FilePermission) -> None
task_lock(task_id: str) -> ContextManager[None]
reserve_session_slot(task_id: str) -> SessionReservation
record_thread_started(task_id: str, reservation_id: str, thread_id: str) -> RunnerState
record_usage(task_id: str, thread_id: str, usage: TokenUsage) -> RunnerState
TaskRunner.run(task_id: str) -> RunnerOutcome
```

**Steps:**

- [ ] Windows `msvcrt`, POSIX `fcntl`을 테스트 가능한 adapter로 감싸고 다음 RED tests를 작성한다.

Test inventory:

- `test_atomic_json_write_survives_interrupted_temp_file`
- `test_session_slot_is_reserved_before_codex_spawn`
- `test_crash_after_reservation_conservatively_consumes_one_slot`
- `test_pre_spawn_gate_error_does_not_reserve_a_slot`
- `test_popen_oserror_releases_launching_reservation`
- `test_unknown_started_gate_error_retains_existing_slot_but_adds_none`
- `test_worker_respawn_reuses_task_ledger_and_never_exceeds_four_threads`
- `test_tests_failed_may_use_fresh_thread_but_gate_error_does_not`
- `test_fourth_failed_thread_becomes_retry_exhausted`
- `test_missing_or_malformed_usage_is_gate_error`
- `test_hook_skipped_is_caught_by_post_exit_verify`
- `test_resume_keeps_the_same_thread_id`
- `test_runner_waits_for_both_platform_checks_before_ready_to_complete`

- [ ] RED를 확인한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_state.py tests/guard/test_runner.py -q
```

- [ ] `atomic_write_json`을 temp→flush→fsync→replace→parent fsync 순서로 구현한다.
- [ ] task ID만으로 OS 고정 state root를 계산하고 production CLI에서 root override를 금지한다.
- [ ] immutable contract/tool prerequisites를 먼저 검증한 뒤 `launching` slot을 예약한다. `Popen` 자체가 실패하면 예약을 해제하고, OS process 생성 뒤 `thread.started` 전에 끊기면 `unknown_started`로 보수적으로 유지한다. `thread.started`, `turn.completed.usage`를 기존 slot에 원자 반영한다.
- [ ] Stop hook 종료와 무관하게 Codex process 후 항상 verifier를 재실행한다.
- [ ] post-exit PASS 뒤 Task 8 bundle comment를 게시하고 두 named platform check가 current PR head에서 green이 될 때까지 poll한다. missing/red/timeout은 `GATE_ERROR`이며 complete-ready를 반환하지 않는다.
- [ ] `TESTS_FAILED`만 남은 slot에서 새 thread를 허용하고 `GATE_ERROR`는 hold/reverify 또는 같은 thread resume만 허용한다.
- [ ] RISK 주석을 추가한다.

```python
# RISK(race): Popen 성공 뒤 thread.started 전 crash는 중복 thread 생성을 막기 위해
# unknown_started slot을 유지한다. pre-spawn/Popen 실패는 slot을 해제한다.
```

- [ ] GREEN과 회귀를 확인한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_state.py tests/guard/test_runner.py -q
.\.venv\Scripts\python.exe -m pytest tests/guard -q
```

- [ ] Commit:

```powershell
git add forge/guard/state.py forge/guard/runner.py tests/guard/test_state.py tests/guard/test_runner.py
git commit -m "feat: persist Codex task sessions across respawns"
```

## Task 7: 공식 Codex Stop hook과 trusted release를 연결한다

**Files:**
- Create: `forge/guard/stop_hook.py`
- Create: `forge/guard/cli.py`
- Create: `forge/guard/__main__.py`
- Create: `forge/hooks/codex-hooks.template.json`
- Create: `forge/schemas/build-manifest.schema.json`
- Create: `forge/scripts/install_codex_hook.py`
- Create: `forge/scripts/install-codex-hook.py`
- Create: `forge/scripts/build-guard-release.py`
- Create: `tests/guard/test_stop_hook.py`
- Create: `tests/guard/test_cli.py`
- Create: `tests/integration/test_codex_stop_hook.py`
- Modify: `forge/hooks/codex-stop-gate.sh`

**Interfaces:**

```text
handle_stop(payload: Mapping[str, object], env: Mapping[str, str]) -> Mapping[str, object]
cli_main(argv: Sequence[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int
build_guard_component(source_root: Path, source_sha: str, output_dir: Path) -> GuardBuildComponent
install_codex_hook(repo: Path, release: Path, build_manifest: Path, verify: bool = False) -> Path
install-codex-hook.py --release RELEASE --manifest BUILD_MANIFEST --repo REPOSITORY [--verify]
```

**Steps:**

- [ ] Stop payload와 CLI RED tests를 작성한다.

Test inventory:

- `test_pass_emits_valid_empty_json_object`
- `test_tests_failed_emits_decision_block_with_prefixed_reason`
- `test_active_stop_hook_ends_recursive_continuation`
- `test_gate_error_emits_continue_false_and_stop_reason`
- `test_stdout_contains_only_one_hook_json_document`
- `test_hook_uses_env_task_id_but_revalidates_stored_contract`
- `test_template_has_stop_event_windows_override_and_3660_timeout`
- `test_real_codex_hook_keeps_thread_id_on_block`
- `test_guard_zipapp_build_is_byte_reproducible_and_contains_locked_runtime_dependencies`

- [ ] RED를 확인한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_stop_hook.py tests/guard/test_cli.py -q
```

- [ ] `session_id`, `turn_id`, `cwd`, `stop_hook_active`, `last_assistant_message`를 엄격 파싱한다.
- [ ] `last_assistant_message`의 structured handoff를 trusted state에 원자 저장한 뒤 공통 verifier를 호출한다.
- [ ] PASS는 `{}`, TESTS_FAILED는 `{"decision":"block","reason":"TESTS_FAILED: <code>: <message>"}`, GATE_ERROR/재귀는 `{"continue":false,"stopReason":"GATE_ERROR: <code>: <message>"}` 계약으로 만든다. `<code>`와 `<message>`는 실제 판정 값으로 치환하며 literal angle-bracket 문자열을 출력하지 않는다.
- [ ] template의 `command`와 `commandWindows`는 trusted release의 절대 interpreter/zipapp을 installer가 채우고 `FORGE_TASK_ID`는 runner environment로 공급한다.
- [ ] `build-guard-release.py`가 `requirements.lock`을 `--require-hashes`로 staging directory에 설치하고 Forge package/schema를 더해 self-contained `forge-guard.pyz`와 `GuardBuildComponent` metadata를 재현 가능하게 생성하도록 한다. final nine-field BuildManifest는 만들지 않는다. archive entry order, timestamp, permission을 정규화해 같은 source SHA의 2회 build가 byte-identical해야 한다.
- [ ] 기존 bash gate는 Python CLI를 exec하는 호환 shim만 남기고 독립 판정 로직을 제거한다.
- [ ] 단위 GREEN 후 실제 Codex CLI smoke를 실행해 같은 `thread.started` ID에서 continuation이 발생하는지 확인한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_stop_hook.py tests/guard/test_cli.py -q
.\.venv\Scripts\python.exe -m pytest tests/integration/test_codex_stop_hook.py -q
```

- [ ] Commit:

```powershell
git add forge/guard forge/hooks forge/schemas/build-manifest.schema.json forge/scripts/install_codex_hook.py forge/scripts/install-codex-hook.py forge/scripts/build-guard-release.py tests/guard tests/integration/test_codex_stop_hook.py
git commit -m "feat: connect Codex Stop hook to completion verifier"
```

## Task 8: PR CI용 immutable evidence bundle을 만든다

**Files:**
- Create: `forge/guard/evidence_bundle.py`
- Create: `forge/guard/secret_scan.py`
- Create: `forge/schemas/evidence-bundle-v1.schema.json`
- Create: `tests/guard/test_evidence_bundle.py`
- Create: `tests/integration/test_ci_evidence.py`
- Modify: `forge/guard/cli.py`

**Interfaces:**

```text
build_bundle(contract: TaskContract, handoff: Handoff, receipt: Receipt) -> EvidenceBundle
upsert_pr_comment(repo: str, pr_number: int, bundle: EvidenceBundle, gh: GhClient) -> int
load_ci_bundle(event: PullRequestEvent, gh: GhClient) -> EvidenceBundle
verify_ci_bundle(bundle: EvidenceBundle, event: PullRequestEvent, gh: GhClient) -> VerificationResult
scan_paths(paths: Sequence[Path], secret_values: Sequence[bytes]) -> SecretScanResult
scan_git_objects(repository: Path, secret_values: Sequence[bytes]) -> SecretScanResult
```

**Steps:**

- [ ] 단위 테스트만 실행하고 “같은 증거를 검증했다”고 과장하지 않도록, Actions에 전달할 bundle 계약부터 RED로 작성한다.

Test inventory:

- `test_bundle_contains_contract_handoff_receipt_and_content_digests`
- `test_bundle_rejects_absolute_workspace_paths_and_secret_fields`
- `test_secret_scan_rejects_known_token_prefixes_private_keys_and_secret_env_values`
- `test_secret_scan_reports_path_and_rule_without_echoing_secret_value`
- `test_ci_rederives_base_and_head_from_github_event_not_bundle_claims`
- `test_ci_rejects_changed_issue_body_or_acceptance_hash`
- `test_ci_rejects_stale_receipt_or_head_sha`
- `test_ci_accepts_bundle_only_when_all_rederived_evidence_matches`
- `test_each_repository_pr_gets_exactly_one_current_head_comment`
- `test_runner_reruns_evidence_missing_pull_request_check_after_comment_upsert`
- `test_non_pull_request_events_take_explicit_contract_only_branch`

- [ ] RED를 확인한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_evidence_bundle.py tests/integration/test_ci_evidence.py -q
```

- [ ] bundle에 canonical contract, handoff, `phase=post-exit` receipt, relative evidence descriptors와 각 SHA-256만 포함한다. OS 절대 path, environment, raw stdout/stderr, secret pattern은 거절한다.
- [ ] `scan_paths`는 full credential token pattern, private-key header, current secret env byte value의 embedded occurrence를 탐지하고 `scan_git_objects`는 reachable Git objects를 검사한다. scanner 자체의 encoded rule source는 false positive가 아니어야 하며 결과에는 path/rule만 기록하고 matched value를 출력하지 않는다.
- [ ] runner가 multi-repo contract의 repository별 bundle을 worktree 밖 trusted state에 만든 뒤 각 repository PR issue comment에 한 건씩 게시/갱신한다. marker는 `forge-evidence-v1`, task ID, run ID, 전체 `(repo, baseline, head, PR)` tuple digest와 해당 PR head SHA를 포함한다. comment 게시가 Git HEAD를 바꾸지 않으므로 receipt self-reference를 만들지 않는다.
- [ ] CI는 paginated PR comments에서 현재 task/run/head에 맞는 marker가 정확히 한 건인지 최대 300초 poll하고 JSON을 추출한다. comment 내용은 transport일 뿐 신뢰하지 않고 schema와 digest를 전부 재검증한다.
- [ ] comment upsert가 PR workflow보다 늦어 `EVIDENCE_MISSING`으로 끝난 경우 runner가 해당 head의 pull_request run ID를 조회해 `gh run rerun <run-id>`를 한 번 호출한다. 그 외 실패는 자동 rerun하지 않는다.
- [ ] 각 PR CI는 `pull_request.base.sha`, `pull_request.head.sha`, current repository, PR number를 event/API에서 재도출하고 comment의 전체 multi-repo tuple 무결성을 검증하되, scoped `github.token`으로 live 재조회하는 범위는 current repository PR/check slice뿐이다. secondary repository live aggregate는 CI 밖 runner가 각 repo에 명시한 credential/context로 검증한다.
- [ ] pull_request mode는 current pinned repo checkout의 검증 명령과 source issue/AC/current PR 상태만 다시 조회한다. merge_group도 current repo associated PR pagination, 이전 named checks, head tuple만 live 검증한다. 외부 rollout runner가 모든 repository slice를 aggregate해 tuple 전체를 승인한다. push main은 contract/full test와 secret scan만 실행한다. schedule/workflow_dispatch는 `FORGE_OPS_HOST=true`인 canonical host repository에서만 `FORGE_DEPLOYED_SHA`와 중앙 issue evidence의 read-only canary/drift audit을 실행하고, secondary repo(`FORGE_OPS_HOST=false`)는 regression/contract/full test와 secret scan만 실행한다.
- [ ] 현재 head에 matching comment가 없거나 둘 이상이거나 body가 size limit/encoding/schema를 위반하면 `GATE_ERROR`로 실패한다.
- [ ] 두 platform CI check가 green이 된 뒤 runner가 다음 단계로 진행하며, Hermes `complete_task()`가 `phase=hermes` verifier를 호출해 final receipt를 발급·소비한다. CI가 자신의 미완료 check를 요구하는 순환은 만들지 않는다.
- [ ] GREEN과 회귀를 확인한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_evidence_bundle.py tests/integration/test_ci_evidence.py -q
.\.venv\Scripts\python.exe -m pytest tests/guard tests/integration -q
```

- [ ] Commit:

```powershell
git add forge/guard/evidence_bundle.py forge/guard/secret_scan.py forge/guard/cli.py forge/schemas/evidence-bundle-v1.schema.json tests/guard/test_evidence_bundle.py tests/integration/test_ci_evidence.py
git commit -m "feat: carry completion evidence into pull request CI"
```

## Task 9: Hermes v0.18.2 완료 불변식 carried patch를 만든다

**Files:**
- Create: `forge/patches/hermes/0.18.2/completion-policy.patch`
- Create: `forge/patches/hermes/0.18.2/manifest.json`
- Create: `forge/scripts/hermes-patch.py`
- Create: `tests/hermes/test_patch_manifest.py`
- Create: `tests/hermes/test_patch_apply.py`
- External isolated Hermes checkout only:
  - Create: `hermes_cli/kanban_completion_policy.py`
  - Modify: `hermes_cli/kanban_db.py`
  - Modify: `hermes_cli/kanban.py`
  - Modify: `plugins/kanban/dashboard/plugin_api.py`
  - Create: `tests/hermes_cli/test_kanban_completion_policy.py`

**Interfaces:**

```text
verify_completion_policy(request: Mapping[str, object]) -> CompletionPolicyDecision
KanbanDB.create_task(existing parameters unchanged; completion_policy: str | None = None) -> Task
KanbanDB.complete_task(task_id: str, result: str; existing optional parameters unchanged) -> Task
KanbanDB._recompute_ready_in_txn(conn: sqlite3.Connection, failure_limit: int | None = None) -> int
KanbanDB.recompute_ready(failure_limit: int | None = None) -> int
KanbanDB._record_task_failure(existing parameters unchanged; sticky_block: bool = False) -> FailureOutcome
```

`recompute_ready()`만 새 `write_txn()`을 열고 `_recompute_ready_in_txn()`은 caller가 연 transaction을 재사용한다. 보호 `complete_task()`는 receipt insert/done/event와 같은 transaction에서 in-txn helper를 호출한다.

**Pinned facts:**

- Hermes version: `0.18.2`
- upstream base: `4281151ae859241351ba14d8c7682dc67ff4c126`
- supported `kanban_db.py` base blobs: Windows `518e74eb0647786a0361105b76bfbaeb1bad3e19`, VPS `6150b141537b947a2a89d19b13be4fbad2330711`
- whole-file blob은 서로 다르지만 다음 핵심 AST preimage는 동일하다.
  - Task `37dbff1faa5f92afa3b63e3d80a1c041e36a0a5fcebd2dc9585bb8c824656137`
  - `_migrate_add_optional_columns` `e8d018507072b7aa7a9d875bde98b389446bb9fb5c61efdfd4e0b1a09fd82583`
  - `create_task` `d95d2c6f0bd66eb3419ce2ee3ad49faa4f211b28624e3cd36e1efbbd8bd265aa`
  - `recompute_ready` `d6e8a2840b92a4c38a9d41e358f49c35c90d386f14834d91a1abe4ff682249e8`
  - `complete_task` `a10e062b91aeef9e8c097997c39840b3bf1b0d0552764681613038505b286bf2`
  - `edit_completed_task_result` `bcf22376052004ea28747d65a95260edcc30781b7e53f7b8ebfa8de72e82e2e2`
  - `detect_crashed_workers` `d7dca0d5a3943b21108e1fb36fca5bb98e13b68b95001b72bf79b5024df9235a`

manifest에는 이 full SHA-1/SHA-256을 그대로 기록한다.

**Steps:**

- [ ] `weapon:using-git-worktrees`로 pinned Hermes source를 `%LOCALAPPDATA%\InfinityForge\worktrees\hermes-guard-4281151a`에 별도 clone/worktree로 준비한다. Windows 사용자 설치 checkout에는 edit를 하지 않는다.
- [ ] project-side manifest tests를 먼저 작성한다.

Test inventory:

- `test_manifest_uses_full_upstream_blob_hunk_and_patch_hashes`
- `test_windows_and_vps_supported_preimages_are_explicit`
- `test_unknown_blob_or_changed_function_preimage_fails_check`
- `test_installer_stages_only_declared_target_paths`

- [ ] isolated Hermes checkout에 upstream RED tests를 먼저 작성한다.

Test inventory:

- `test_protected_create_persists_immutable_policy`
- `test_idempotency_key_cannot_reuse_task_with_different_policy`
- `test_unprotected_completion_remains_backward_compatible`
- `test_missing_bad_hash_timeout_nonzero_malformed_verifier_fail_closed`
- `test_rejected_completion_mutates_no_task_run_event_or_child_state`
- `test_allowed_receipt_done_and_consumed_event_commit_atomically`
- `test_complete_calls_in_txn_ready_helper_without_nested_transaction`
- `test_child_never_becomes_ready_when_receipt_transaction_rolls_back`
- `test_stale_run_cross_task_and_same_task_replay_are_rejected`
- `test_protected_result_edit_is_rejected_but_unprotected_edit_remains`
- `test_protocol_violation_blocks_on_first_hit_even_with_max_retries_four`
- `test_explicit_unblock_is_required_before_recompute_ready`
- `test_cli_policy_flag_json_and_exit_two_contract`
- `test_dashboard_and_tool_complete_surface_policy_rejection`

- [ ] RED를 pinned Hermes venv에서 확인한다.

```powershell
$HermesPython = Join-Path $env:LOCALAPPDATA 'InfinityForge\worktrees\hermes-guard-4281151a\venv\Scripts\python.exe'
& $HermesPython -m pytest tests/hermes_cli/test_kanban_completion_policy.py -q
```

- [ ] `kanban_completion_policy.py`를 구현한다.
  - manifest fixed path를 읽는다.
  - zipapp SHA-256을 확인한다.
  - verifier argv-list subprocess에 JSON request를 stdin으로 전달한다.
  - allow/deny response schema, task/run/policy/digest를 재대조한다.
  - missing/hash/timeout/nonzero/malformed/mismatch를 `CompletionPolicyError`의 GATE_ERROR로 변환한다.
- [ ] additive schema를 구현한다.

```sql
ALTER TABLE tasks ADD COLUMN completion_policy TEXT;
ALTER TABLE tasks ADD COLUMN completion_receipt_digest TEXT;
CREATE TABLE IF NOT EXISTS completion_receipts (
  digest TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  run_id INTEGER NOT NULL,
  policy TEXT NOT NULL,
  payload TEXT NOT NULL,
  consumed_at INTEGER NOT NULL,
  UNIQUE(task_id, run_id)
);
```

- [ ] `create_task()`에 create-only `completion_policy`와 valid set `{"forge-v1"}`를 추가하고 idempotent existing row의 정책 mismatch를 거절한다.
- [ ] `complete_task()`에서 task/status/current_run을 읽고 보호 task만 `phase=hermes` trusted verifier preflight를 실행한다. stop/post-exit/ci receipt가 입력되면 거절한다.
- [ ] 기존 `recompute_ready()` body를 `_recompute_ready_in_txn(conn, failure_limit=None)`로 분리하고 public wrapper만 `write_txn()`을 연다. write transaction에서 `id + status + current_run_id` CAS, receipt digest update, run end, ledger insert, `completion_receipt_consumed`, completed event, done, in-txn child readiness를 묶는다. transaction 안에서 public wrapper를 재호출하지 않는다.
- [ ] duplicate receipt 또는 CAS 0 row는 transaction 전체를 rollback한다.
- [ ] protected `edit_completed_task_result()`를 거절하고 새 run/reverify를 요구한다.
- [ ] `_record_task_failure`에 keyword-only parameter `sticky_block: bool = False`를 추가하고 protocol_violation caller만 `sticky_block=True`를 전달한다. 첫 발생을 `needs_input` block과 audit event로 남기고 `recompute_ready`가 명시 unblock 전 재승격하지 못하게 한다.
- [ ] CLI에 `--completion-policy forge-v1`, task JSON 노출, exit 2를 추가하고 dashboard는 HTTP 409/bulk per-id error로 변환한다. 기존 tool의 `ValueError` 경로가 bypass하지 않는지 확인한다.
- [ ] RISK 주석을 schema, CAS transaction, external verifier boundary, sticky block에 붙인다.
- [ ] upstream targeted GREEN을 확인한다.

```powershell
$HermesPython = Join-Path $env:LOCALAPPDATA 'InfinityForge\worktrees\hermes-guard-4281151a\venv\Scripts\python.exe'
& $HermesPython -m pytest tests/hermes_cli/test_kanban_completion_policy.py -q
& $HermesPython -m pytest tests/hermes_cli -q
```

- [ ] isolated diff에서 patch를 만들고 manifest에 full upstream SHA, 두 supported blob, 함수별 full preimage, patch SHA-256, expected postimage SHA를 기록한다.
- [ ] `hermes-patch.py check|install|verify|rollback`을 구현한다. install은 drain→DB/대상 파일 backup→preimage check→apply→targeted tests→target-path-only commit→deployment receipt 순서다.
- [ ] rollback은 dispatcher stop→patch commit revert, 충돌 시 verified target-file backup atomic restore→targeted tests 순서다. additive DB column/table은 구버전이 무시하므로 integrity 손상이 없으면 DB snapshot을 되돌리지 않는다.
- [ ] 두 base fixture에 patch check/apply/rollback을 검증한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/hermes/test_patch_manifest.py tests/hermes/test_patch_apply.py -q
```

- [ ] Commit은 INFINITY_FORGE artifact만 포함한다. 외부 사용자 checkout을 stage하지 않는다.

```powershell
git add forge/patches/hermes/0.18.2 forge/scripts/hermes-patch.py tests/hermes
git commit -m "feat: enforce receipts at Hermes completion boundary"
```

## Task 10: label projection을 receipt와 이벤트 기반으로 바꾼다

**Files:**
- Create: `forge/ops/__init__.py`
- Create: `forge/ops/state.py`
- Create: `forge/ops/github.py`
- Create: `forge/ops/hermes.py`
- Create: `forge/ops/label_mirror.py`
- Create: `tests/ops/test_label_mirror.py`
- Modify: `forge/scripts/label-mirror.py`

**Interfaces:**

```text
CompletionInspector.status(task_id: str) -> CompletionStatus
build_executor_create_argv(issue: IssueRecord, workspace: WorkspaceRecord) -> Sequence[str]
reconcile_issue(issue: IssueRecord, state: MirrorState) -> ProjectionResult
```

**Steps:**

- [ ] 기존 script를 import 가능한 Python core로 옮기기 전에 RED tests를 작성한다.

Test inventory:

- `test_executor_create_argv_has_tenant_goal_turn_runtime_retry_and_policy`
- `test_issue_root_card_mapping_is_idempotent_and_exactly_one`
- `test_raw_done_without_consumed_receipt_is_not_projected`
- `test_valid_consumed_receipt_may_project_mergeable`
- `test_failed_label_comes_only_from_retry_exhausted_gave_up_or_sticky_event`
- `test_github_pagination_or_api_error_is_gate_error`

- [ ] RED를 확인한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ops/test_label_mirror.py -q
```

- [ ] canonical key를 `github-issue:<repo>#<number>`로 고정하고 reviewer/critic child가 필요하면 `github-stage:<repo>#<number>:<stage>:<receipt>`를 사용한다.
- [ ] 신규 executor argv를 정확히 다음과 같이 만든다.

```text
hermes kanban create <title> --body <body> --assignee executor \
  --workspace <canonical-workspace-path> --idempotency-key <github-issue-key> \
  --skill kanban-codex-delegate --tenant forge --goal --goal-max-turns 20 \
  --max-runtime 60m --max-retries 4 --completion-policy forge-v1
```

`<title>`, `<body>`, `<canonical-workspace-path>`, `<github-issue-key>`는 source issue/registry에서 계산한 argv element 한 개로 전달하며 literal angle-bracket 문자열을 사용하지 않는다. workspace 대신 board가 project ID를 요구하는 설치에서는 contract에 고정된 `--project <project-id>`를 사용하고 두 flag를 동시에 보내지 않는다.

- [ ] `CompletionInspector.status(task_id)`가 trusted `forge-guard completion-status`를 호출해 protected/valid/consumed/digest를 얻도록 한다. raw done만 조회하는 코드를 금지한다.
- [ ] receipt 누락/불일치는 label을 유지하고 `completion_rejected` event+exit 2로 기록한다.
- [ ] valid consumed receipt일 때만 `forge:mergeable`/close 후보를 만든다. 실패 투영은 event를 기준으로 한다.
- [ ] 기존 script는 thin entrypoint만 남긴다.
- [ ] GREEN과 전체 ops 회귀를 확인한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ops/test_label_mirror.py -q
```

- [ ] Commit:

```powershell
git add forge/ops forge/scripts/label-mirror.py tests/ops/test_label_mirror.py
git commit -m "feat: require consumed receipts for label projection"
```

## Task 11: spec registry와 coverage를 기계적 SoT로 바꾼다

**Files:**
- Create: `forge/spec-registry.json`
- Create: `forge/schemas/spec-registry.schema.json`
- Create: `forge/ops/spec_coverage.py`
- Create: `forge/scripts/spec-coverage.py`
- Create: `tests/ops/test_spec_coverage.py`
- Modify: `forge/spec-registry.md`
- Modify: `forge/scripts/spec-coverage.sh`

**Interfaces:**

```text
load_registry(path: Path) -> SpecRegistry
evaluate_spec(spec: SpecRecord, gh: GhClient, completion: CompletionInspector) -> SpecCoverage
evaluate_coverage(registry: SpecRegistry, gh: GhClient) -> CoverageReport
render_registry_markdown(registry: SpecRegistry) -> str
```

**Steps:**

- [ ] registry schema와 coverage RED tests를 작성한다.

Test inventory:

- `test_every_spec_has_source_hash_issue_ac_hashes_prs_and_required_gates`
- `test_canonical_issue_mapping_must_be_exactly_one`
- `test_body_edit_open_issue_unmerged_pr_missing_or_red_gate_fail`
- `test_invalid_or_missing_receipt_fails_coverage`
- `test_paginated_api_error_is_gate_error_not_zero_items`
- `test_full_m_of_m_requires_every_predicate`
- `test_missing_spec_requeues_idempotent_spec_gap_card`

- [ ] RED를 확인한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ops/test_spec_coverage.py -q
```

- [ ] JSON SSoT에 `spec_id`, source path/text SHA, repo, issue/body/AC hashes, AC list, PR URLs, required gates를 기록한다.
- [ ] Markdown registry는 JSON에서 생성한 human view로 바꾸고 수동 SoT로 사용하지 않는다.
- [ ] coverage 술어를 exactly-one issue ∧ body/AC hash ∧ issue closed ∧ PR merged ∧ expected checks present+green ∧ receipt consumed로 구현한다.
- [ ] missing SPEC는 `spec-gap:<SPEC-ID>` key로 issue-finder 큐에 멱등 재투입한다.
- [ ] shell script는 Python entrypoint shim만 남긴다.
- [ ] GREEN과 회귀를 확인한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ops/test_spec_coverage.py -q
```

- [ ] Commit:

```powershell
git add forge/spec-registry.json forge/spec-registry.md forge/schemas/spec-registry.schema.json forge/ops/spec_coverage.py forge/scripts/spec-coverage.py forge/scripts/spec-coverage.sh tests/ops/test_spec_coverage.py
git commit -m "feat: make spec coverage evidence complete"
```

## Task 12: dispatcher supervisor와 fail-closed canary를 구현한다

**Files:**
- Create: `forge/ops/dispatcher_supervisor.py`
- Create: `forge/ops/canary.py`
- Create: `forge/ops/slack_transport.py`
- Create: `forge/ops/canary_alert.py`
- Create: `forge/ops/slack_secret_provision.py`
- Create: `forge/scripts/dispatcher-supervisor.py`
- Create: `forge/scripts/canary.py`
- Create: `forge/scripts/post-canary-alert.py`
- Create: `forge/scripts/provision-slack-alert-secret.ps1`
- Create: `tests/ops/test_dispatcher_supervisor.py`
- Create: `tests/ops/test_canary.py`
- Create: `tests/ops/test_slack_transport.py`
- Create: `tests/ops/test_slack_secret_provision.py`
- Modify: `forge/scripts/canary.sh`

**Interfaces:**

```text
read_marker(path: Path, now: datetime, deployed_sha: str) -> CanaryMarkerStatus
run_supervisor(config: SupervisorConfig, child: ProcessFactory) -> SupervisorExit
run_canary(config: CanaryConfig, probes: Sequence[CanaryProbe]) -> CanaryResult
send_canary_alert(attempt_id: str, target: str, source_sha: str, failed_checks: Sequence[str], env_file: Path, state_root: Path, transport: SlackTransport) -> AlertReceipt
post_scanned_slack_request(request: SlackRequest, env_file: Path, receipt_file: Path, transport: SlackTransport) -> SlackReceipt
provision-slack-alert-secret.ps1 -SourceEnvFile FILE -Targets Windows,Linux,Vps -WslDistribution Ubuntu -WslUser immortal0900 -VpsHost ubuntu@51.222.27.48 -RepairWindowsAcl
```

**Steps:**

- [ ] fake Hermes child/gateway와 clock을 사용해 RED tests를 작성한다.

Test inventory:

- `test_matching_fresh_marker_and_deployed_sha_spawn_dispatcher`
- `test_missing_stale_or_mismatched_marker_never_spawns`
- `test_marker_removal_terminates_child_within_five_seconds`
- `test_supervisor_is_singleton_and_never_controls_gateway`
- `test_canary_closes_marker_before_first_check`
- `test_only_all_pass_reopens_marker_mode_600`
- `test_each_failure_keeps_dispatcher_stopped_and_gateway_healthy`
- `test_slack_delivery_failure_cannot_turn_red_canary_green`
- `test_failed_canary_orders_close_stop_gateway_probe_scan_and_post`
- `test_slack_accept_crash_retry_has_one_visible_alert`
- `test_new_canary_attempt_uses_distinct_alert_id`
- `test_probe_exception_timeout_or_invalid_result_still_alerts_and_stays_red`
- `test_signal_or_kill_reuses_interrupted_canary_attempt_journal`
- `test_secret_provision_uses_stdin_only_and_readbacks_mode_digest_identity`
- `test_missing_linux_or_vps_alert_env_blocks_service_mutation`

- [ ] RED를 확인한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ops/test_dispatcher_supervisor.py tests/ops/test_canary.py tests/ops/test_slack_transport.py tests/ops/test_slack_secret_provision.py -q
```

- [ ] supervisor가 manifest SHA와 TTL 6시간 30분 canary marker가 맞을 때만 아래 child를 소유하도록 한다.

```text
hermes kanban daemon --interval 60 --failure-limit 4 --pidfile <state>/dispatcher.pid --verbose
```

- [ ] marker 삭제/만료/mismatch 시 SIGTERM/Windows terminate 후 5초, 필요 시 kill하고 heartbeat/status JSON을 atomic write한다.
- [ ] canary는 시작 즉시 marker를 삭제하고 dispatcher stopped를 확인한 뒤 gateway health를 첫 probe로, 이어 hook block, post-gate, committed-clean PASS, invalid handoff, receiptless completion, artifact SHA, DB quick_check/mode, Codex auth를 검사한다. 이미 계산된 check 목록을 받지 않고 marker close 뒤 probe를 호출한다.
- [ ] 전부 PASS한 뒤에만 marker를 POSIX mode 600 또는 Windows current-user-only ACL로 atomic write하고 supervisor child 시작을 확인한다.
- [ ] 실패 시 marker closed, dispatcher stopped, gateway health 상태 수집, canonical request secret scan, `codex work report` App ID `A0BEQAZ1MS5`의 channel `C0BES16KE1J` Slack alert, exit 2를 순서대로 확인한다. delivery 실패도 red이며 marker는 열지 않는다.
- [ ] Task 16도 재사용할 `forge.ops.slack_transport`가 env metadata와 pinned `auth.test` principal(`T0AU5RA7XND/U0BEG5Y5CCB/B0BELD3V84E/codex_work_report`), exact `chat:write,chat:write.public` scope를 read-back하고 실제 request bytes를 secret scan한 뒤에만 전송하도록 한다. 현재 scope에 없는 `bots.info`/`conversations.info`는 호출하지 않는다. App ID 보증 수준은 `locally-pinned-principal`로 명시하고, channel 쓰기는 각 host의 idempotent preflight post가 exact channel/ts를 반환하는 것으로 증명한다. token/header/raw response는 결과·예외·log·receipt에 넣지 않는다.
- [ ] canary attempt journal과 deterministic `client_msg_id`를 사용해 API accept 직후 crash retry는 visible alert 하나로 수렴시킨다. 새 정기 attempt는 새 ID를 사용한다.
- [ ] probe exception/timeout/invalid return은 sanitized failure로 변환해 Slack alert를 반드시 시도한다. SIGINT/SIGTERM/SIGKILL은 interrupted journal을 fsync하고 다음 invocation이 같은 attempt를 재개하며, `KeyboardInterrupt`/`SystemExit`를 일반 exception으로 삼키지 않는다.
- [ ] 현재 Windows에만 존재하고 ACL inheritance/sandbox read가 실측된 `C:\Users\황화인HwainHwang\.codex\secrets\codex-work-report.env`를 `-RepairWindowsAcl`로 current user, SYSTEM, Administrators exact protected ACL로 바꾼 뒤 content digest를 보존한다. exact allowlisted 4개 key를 WSL `/home/immortal0900/.codex/secrets/codex-work-report.env`와 VPS `/home/ubuntu/.codex/secrets/codex-work-report.env`로 stdin-only atomic install한다. argv/env/log/artifact에는 credential을 넣지 않고 dir 0700/file 0600/digest/host-side identity를 read-back한다. 기존 다른 digest는 `-Rotate` 없이는 거절한다.
- [ ] 각 installer Services preflight는 host-local alert env의 ACL/mode, digest, app/channel identity와 Slack API read 권한을 검증한다. missing/invalid이면 service/Task mutation 0회로 실패하고 rollback은 이 external prerequisite를 삭제하지 않는다.
- [ ] shell script는 thin shim으로 바꾼다.
- [ ] GREEN과 회귀를 확인한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ops/test_dispatcher_supervisor.py tests/ops/test_canary.py tests/ops/test_slack_transport.py tests/ops/test_slack_secret_provision.py -q
```

- [ ] Commit:

```powershell
git add forge/ops/dispatcher_supervisor.py forge/ops/canary.py forge/ops/slack_transport.py forge/ops/canary_alert.py forge/ops/slack_secret_provision.py forge/scripts/dispatcher-supervisor.py forge/scripts/canary.py forge/scripts/post-canary-alert.py forge/scripts/provision-slack-alert-secret.ps1 forge/scripts/canary.sh tests/ops/test_dispatcher_supervisor.py tests/ops/test_canary.py tests/ops/test_slack_transport.py tests/ops/test_slack_secret_provision.py
git commit -m "feat: gate dispatcher startup on canary evidence"
```

## Task 13: drift audit를 모든 완료·운영 불변식에 연결한다

**Files:**
- Create: `forge/ops/drift_audit.py`
- Create: `forge/ops/drift_alert.py`
- Create: `forge/ops/ops_evidence.py`
- Modify: `forge/ops/slack_transport.py`
- Create: `forge/scripts/drift-audit.py`
- Create: `tests/ops/test_drift_audit.py`
- Create: `tests/ops/test_ops_evidence.py`
- Modify: `forge/scripts/drift-audit.sh`

**Interfaces:**

```text
audit_invariants(context: AuditContext, checks: Sequence[InvariantCheck]) -> AuditReport
classify_gate_error_rate(events: Sequence[AuditEvent], window: timedelta) -> GateErrorRate
write_audit_state(path: Path, report: AuditReport) -> None
build_ops_evidence(source_sha: str, build_manifest: Path, receipts: Mapping[str, Path], canary: Mapping[str, CanaryEvidence], drift: Mapping[str, DriftEvidence]) -> OpsEvidence
upsert_ops_evidence_comment(issue: int, evidence: OpsEvidence, client: GitHubCommentClient, secret_values: Sequence[bytes]) -> CommentRecord
publish_current_ops_evidence(context: AuditContext, issue: int, client: GitHubCommentClient) -> CommentRecord
send_drift_alert(target: str, source_sha: str, report: DriftReport, state_root: Path, env_file: Path, transport: SlackTransport) -> AlertReceipt
```

**Steps:**

- [ ] 각 invariant를 독립 fixture로 깨뜨리는 RED tests를 작성한다.

Test inventory:

- `test_each_drift_invariant_fails_loudly`, parametrized with `issue_root_card_1_to_1`, `protected_card_policy`, `consumed_receipt`, `protocol_violation`, `retry_exhausted`, `gate_error_rate`, `issue_body_ac_hash`, `canary_heartbeat`, `dispatcher_gateway_split`, `service_timers`, `deployed_sha`, `artifact_hash`, `db_mode_quick_check`, `backup_outbox_disk`
- `test_first_violation_or_gate_error_posts_crash_safe_slack_alert_on_each_target`
- `test_same_open_incident_and_process_retry_do_not_duplicate_alert`
- `test_pass_closes_incident_and_recurrence_posts_new_alert`
- `test_alert_transport_failure_escalates_violation_to_gate_error_without_green`
- `test_sqlite_github_or_service_inspection_error_is_gate_error`
- `test_result_state_is_atomic_and_never_empty_success`
- `test_ops_evidence_requires_exact_three_target_same_sha_and_build_digest`
- `test_ops_evidence_comment_paginates_and_rejects_duplicate_sha_marker`
- `test_ops_evidence_scans_canonical_request_before_comment_transport`
- `test_windows_hourly_drift_refreshes_same_sha_comment_without_duplicate`

- [ ] RED를 확인한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ops/test_drift_audit.py -q
```

- [ ] SQLite는 query-only, expected schema check 후 읽고 raw done 대신 receipt ledger를 검사한다.
- [ ] protected card 속성은 goal=1, turns=20, runtime=3600, retries=4, policy=`forge-v1`을 모두 검사한다.
- [ ] 기본 GATE_ERROR 경보는 최근 60분 3건 또는 최소 5회 중 20% 초과로 고정하고 raw samples를 evidence에 남긴다.
- [ ] issue edit, protocol violation, retry exhausted, deployment/canary/drift SHA, DB permission/quick_check, service/timer, backup/outbox/disk를 검사한다.
- [ ] 모든 target의 첫 VIOLATION/GATE_ERROR를 Task 12 공통 Slack transport에 연결한다. target+source SHA+kind+sorted check-name digest의 durable open-incident receipt로 accept-crash retry를 exact-once 수렴시키고, PASS가 incident를 닫은 뒤 재발하면 새 generation alert를 보낸다. alert 실패는 green으로 축소하지 않고 GATE_ERROR exit 2다.
- [ ] GitHub pagination, DB, system service 검사 실패를 0건으로 축소하지 않고 exit 2로 처리한다.
- [ ] Windows hourly Drift 실행은 local Windows와 read-only WSL/VPS transport의 receipt/activation/canary/drift를 모아 `forge-ops-evidence-v1` exact target/source/build schema를 만든다. immutable Task argv의 canonical `--bootstrap-repository OWNER/REPO`에 모든 `gh variable`/issue API context를 명시하고 `FORGE_BOOTSTRAP_ISSUE`, `FORGE_DEPLOYED_SHA`를 읽어 같은 deployed SHA marker comment를 pagination/exact-one upsert한다. canary freshness는 25200초(7시간), drift는 7200초로 고정한다. request canonical bytes와 OS credential provider가 반환한 non-empty GitHub token을 `scan_bytes`로 검사한 뒤에만 transport를 호출한다. duplicate marker, remote read, credential, scan, transport 실패는 local drift report에 `GATE_ERROR`로 원자 기록하고 exit 2이며 stale success를 재게시하지 않는다. POSIX Drift는 local audit만 하고 publish하지 않는다.
- [ ] Windows Scheduled Task는 동일 interactive user의 OS-protected `gh` credential store를 사용하고 `gh auth status`, exact bootstrap repository/variable/issue read-back, 3-host reachability를 install preflight에서 확인한다. Windows `gh`가 미로그인이면 배포 mutation 0회로 중단하고 WSL credential을 복사하지 않는다. token/header/raw response는 stdout/stderr/evidence에 넣지 않는다. hourly cycle이 current same-SHA comment의 `published_at`과 canary/drift evidence를 갱신하므로 weekly CI audit의 freshness가 지속된다.
- [ ] shell script는 thin shim으로 바꾼다.
- [ ] GREEN과 회귀를 확인한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ops/test_drift_audit.py -q
.\.venv\Scripts\python.exe -m pytest tests/ops -q
```

- [ ] Commit:

```powershell
git add forge/ops/drift_audit.py forge/ops/drift_alert.py forge/ops/ops_evidence.py forge/ops/slack_transport.py forge/scripts/drift-audit.py forge/scripts/drift-audit.sh tests/ops/test_drift_audit.py tests/ops/test_ops_evidence.py
git commit -m "feat: audit completion and deployment drift"
```

## Task 14: clean-install unit, Scheduled Task, workflow 계약을 만든다

**Files:**
- Create: `forge/systemd/forge-dispatcher.service`
- Create: `forge/systemd/forge-canary.service`
- Create: `forge/systemd/forge-canary.timer`
- Create: `forge/systemd/forge-drift.service`
- Create: `forge/systemd/forge-drift.timer`
- Create: `forge/systemd/forge-mirror.service`
- Create: `forge/systemd/forge-mirror.timer`
- Create: `forge/systemd/forge-spec-coverage.service`
- Create: `forge/systemd/forge-spec-coverage.timer`
- Create: `forge/systemd/forge-ledger.service`
- Create: `forge/systemd/forge-ledger.timer`
- Create: `forge/systemd/forge-flush-outbox.service`
- Create: `forge/systemd/forge-flush-outbox.timer`
- Create: `forge/systemd/forge-morning-report.service`
- Create: `forge/systemd/forge-morning-report.timer`
- Create: `forge/systemd/forge-backup.service`
- Create: `forge/systemd/forge-backup.timer`
- Create: `forge/scripts/install-linux.sh`
- Create: `forge/scripts/verify-linux-install.sh`
- Create: `forge/scripts/install-windows.ps1`
- Create: `forge/ops/hermes_bootstrap.py`
- Create: `forge/scripts/hermes-bootstrap.py`
- Create: `tests/ops/test_install_contracts.py`
- Create: `tests/ops/test_hermes_bootstrap.py`
- Create: `tests/fixtures/build-manifest-v1.json`
- Create: `tests/test_workflow_contract.py`
- Modify: `.github/workflows/capability-eval.yml`

**Interfaces:**

```text
install-linux.sh --phase hermes|hooks|services --target linux|vps --release PATH --manifest PATH [repeatable --repo PATH] [--snapshot-index PATH --snapshot-sha256 HASH]
verify-linux-install.sh --target linux|vps --release PATH --manifest PATH [repeatable --repo PATH]
install-windows.ps1 -Phase Hooks|Services -ReleasePath PATH -Manifest PATH -RepoPaths PATHS -PythonPath PATH [-BootstrapRepository OWNER/REPO] [-PlanOnly]
hermes-bootstrap.py recover|begin|advance|complete --record RECORD COMMAND_ARGS
install result JSON: {status, target, actions, previous_state_digest, applied_state_digest}
workflow checks: guard-contract (ubuntu-latest), guard-contract (windows-latest)
```

**Steps:**

- [ ] checked-in unit/Task plan과 workflow RED tests를 작성한다.

Test inventory:

- `test_linux_units_use_current_release_and_never_runtime_heredoc`
- `test_dispatcher_canary_drift_mirror_coverage_and_existing_jobs_have_units`
- `test_timer_calendars_match_approved_operating_schedule`
- `test_windows_plan_has_dispatcher_canary_drift_tasks_and_ignore_new`
- `test_windows_access_denied_is_hard_failure_without_hidden_vbs_fallback`
- `test_installer_sets_dispatch_in_gateway_false_atomically`
- `test_workflow_has_pr_push_main_merge_group_schedule_and_dispatch`
- `test_workflow_has_python_311_windows_and_ubuntu_stable_check_names`
- `test_missing_platform_check_is_not_treated_as_green`
- `test_workflow_scans_source_bundle_artifacts_and_slack_payload_for_credentials`

- [ ] RED를 확인한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ops/test_install_contracts.py tests/test_workflow_contract.py -q
```

- [ ] POSIX runtime SSoT는 `/usr/bin/python3`으로 고정한다. systemd user unit은 `WorkingDirectory=%h/.local/share/infinity-forge/current`, `Environment=PYTHONPATH=%h/.local/share/infinity-forge/current`, `ExecStart=/usr/bin/python3 %h/.local/share/infinity-forge/current/forge/scripts/<entrypoint>.py`를 함께 사용하고 `<entrypoint>`를 각 checked-in filename으로 치환한다. 수동·rollout·rollback 명령도 candidate immutable release를 cwd/PYTHONPATH로 사용한다. Bash entrypoint는 `/usr/bin/bash <absolute-script>`로 지정한다.
- [ ] canary는 6시간 간격+21:00 Asia/Seoul, drift hourly, mirror VPS-only 2분, coverage 21:02+07:30으로 설정한다.
- [ ] 기존 ledger/flush/morning/backup job도 heredoc이 아닌 checked-in template로 옮긴다.
- [ ] Windows installer는 `-Phase Hooks|Services -ReleasePath -Manifest -RepoPaths -PythonPath [-BootstrapRepository OWNER/REPO] [-PlanOnly]`를 제공한다. Hooks는 모든 repository install/verify만, Services는 bootstrap repository를 required로 받아 `\INFINITY_FORGE\Dispatcher`, Canary, Drift Scheduled Task 등록만 수행한다. Canary/Drift action은 각각 parser-required target/build-manifest argv 전체를 고정하고 Drift는 publisher/repository/issue-variable flags를 포함한다. `MultipleInstances=IgnoreNew`, restart settings, immutable release interpreter/working-directory/PYTHONPATH를 고정한다.
- [ ] Windows Services phase는 Drift Task 등록 전에 release `PYTHONPATH`로 same-user `gh auth status`, canonical bootstrap repository/`FORGE_BOOTSTRAP_ISSUE` read-back, WSL/VPS read-only reachability를 확인한다. Drift Task는 hourly local audit 뒤 `publish_current_ops_evidence`를 실행하며 publish 실패를 성공으로 숨기지 않는다.
- [ ] Windows Task 등록 `AccessDenied`는 hard fail로 rollback한다. 현재 Startup VBS를 조용한 fallback으로 사용하지 않는다.
- [ ] Windows/Linux/VPS config의 `kanban.dispatch_in_gateway=false`를 backup+atomic replace하고 exact false 확인 전 gateway restart를 금지한다.
- [ ] Linux installer는 Hermes(existing read-only verify 또는 finalized absent snapshot 기반 clean bootstrap)→Hooks(all repos)→Services 순으로 phase를 분리한다. clean bootstrap은 durable `hermes-bootstrap-journal/v1`의 authorized→checkout→runtime→database→complete CAS stage와 ERR/INT/TERM recovery를 사용하고 receipt가 completed record digest를 결합한다. Services는 Linger read-back이 이미 yes이면 sudo 0회, no이면 `sudo -n loginctl enable-linger <user>` 후 read-back하고, target.env mode 0600과 exact target-specific canary/drift argv를 설치한다. Linux staging은 dispatcher/canary/drift만 허용하고 VPS만 mirror/spec/ledger/flush/morning/backup을 허용한다. WSL hook repo는 `/home/immortal0900/work/INFINITY_FORGE/<SHA>` 독립 clone만 허용하고 `/mnt/c`는 거절한다. command 권한 실패는 hard failure다.
- [ ] workflow check 이름을 `guard-contract (ubuntu-latest)`, `guard-contract (windows-latest)`로 고정한다.
- [ ] 양쪽 matrix에서 package install/full pytest/compileall을 실행하고 Ubuntu는 bash+systemd verify, Windows는 PowerShell parser+PlanOnly를 실행한다.
- [ ] 양쪽 matrix에서 `forge.guard.secret_scan`으로 tracked source, generated evidence bundle, release manifest/artifact file list, Slack message fixture를 검사하고 match 시 value를 출력하지 않은 채 실패한다.
- [ ] GREEN과 정적 검증을 확인한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ops/test_install_contracts.py tests/test_workflow_contract.py -q
.\.venv\Scripts\python.exe -m compileall forge
pwsh -NoProfile -File forge/scripts/install-windows.ps1 -Phase Services -ReleasePath C:\staged -Manifest C:\staged\build-manifest.json -RepoPaths @('C:\01.project\INFINITY_FORGE') -PythonPath .\.venv\Scripts\python.exe -BootstrapRepository OWNER/REPO -PlanOnly
```

```bash
bash -n forge/scripts/*.sh
systemd-analyze verify forge/systemd/*.service forge/systemd/*.timer
```

- [ ] Commit:

```powershell
git add forge/systemd forge/ops/hermes_bootstrap.py forge/scripts/hermes-bootstrap.py forge/scripts/install-linux.sh forge/scripts/verify-linux-install.sh forge/scripts/install-windows.ps1 tests/fixtures/build-manifest-v1.json tests/ops/test_install_contracts.py tests/ops/test_hermes_bootstrap.py tests/test_workflow_contract.py .github/workflows/capability-eval.yml
git commit -m "ci: verify guard contracts on Windows and Linux"
```

## Task 15: exact-SHA build, deploy, rollback 도구를 구현한다

**Files:**
- Create: `forge/ops/deployment.py`
- Modify: `forge/schemas/build-manifest.schema.json`
- Create: `forge/schemas/deployment-receipt.schema.json`
- Create: `forge/scripts/rollback.ps1`
- Create: `forge/scripts/rollback-linux.sh`
- Create: `forge/scripts/rollback-vps.sh`
- Create: `tests/ops/test_deployment.py`
- Modify: `forge/scripts/deploy.ps1`
- Modify: `forge/scripts/deploy-vps.sh`
- Modify: `docs/ops-guide.md`
- Modify: `docs/automation-architecture.md`

**Interfaces:**

```text
deploy_target(ops: DeploymentOperations) -> DeploymentReceipt
rollback_linux_target(target: str, before_receipt: Path, build_manifest: Path, repositories: Sequence[Path]) -> DeploymentReceipt
python -m forge.ops.deployment build --sha SHA40 --output-dir DIR
python -m forge.ops.deployment verify-build --build-manifest FILE --artifact FILE --artifact-sha256 SHA256
deploy.ps1 -Sha SHA40 -Artifact FILE -ArtifactSha256 SHA256 -BuildManifest FILE -Targets Windows,Linux,Vps -RepoPaths PATHS -BootstrapRepository OWNER/REPO [-PlanOnly|-Apply]
deploy-vps.sh --sha SHA40 --artifact FILE --artifact-sha256 SHA256 --build-manifest FILE --repo PATH [--repo PATH]
rollback.ps1 -BeforeReceipt FILE -BuildManifest FILE -RepoPaths PATHS
rollback-linux.sh --target linux|vps --before-receipt FILE --build-manifest FILE --repo PATH [--repo PATH]
```

**Steps:**

- [ ] mutable deploy를 거절하는 RED tests를 작성한다.

Test inventory:

- `test_deploy_requires_clean_source_and_full_forty_hex_sha`
- `test_deploy_requires_both_named_ci_checks_present_and_successful`
- `test_archive_hash_and_manifest_are_reproducible`
- `test_vps_deploy_never_runs_git_pull_or_generates_units_with_heredoc`
- `test_each_target_closes_canary_and_drains_before_mutation`
- `test_failure_restores_previous_release_config_units_and_patch`
- `test_db_snapshot_restore_occurs_only_after_integrity_failure`
- `test_same_sha_forward_reuses_exact_existing_release_and_rejects_tamper_or_extra_file`
- `test_clean_bootstrap_receipt_binds_complete_ownership_journal_and_signal_recovery`
- `test_first_forge_release_with_preexisting_vps_hermes_db_preserves_both_on_rollback`

- [ ] RED를 확인한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ops/test_deployment.py -q
```

- [ ] `deploy.ps1 -Sha SHA40 -Artifact FILE -ArtifactSha256 SHA256 -BuildManifest FILE -Targets Windows,Linux,Vps -RepoPaths PATHS -BootstrapRepository OWNER/REPO [-PlanOnly|-Apply]`로 인터페이스를 교체한다. 자동 `git add/commit/push`를 삭제한다.
- [ ] clean source, object 존재, expected CI check presence/success를 확인한 뒤 core `build_guard_component`, `git archive <sha>`, locked runtime, schemas, Hermes patch를 두 번 byte-reproducible build하고 ops builder만 final BuildManifest를 쓴다.
- [ ] shared `build-manifest-v1` exact nine fields는 `schema_version`, `source_sha`, `archive_sha256`, `guard_sha256`, `requirements_lock_sha256`, `python_requires`, `schema_hashes`, `hermes_patch_manifest_sha256`, `hermes_patch_sha256`이며 모든 object는 `additionalProperties:false`다. 경쟁 manifest producer를 만들지 않는다. host별 timestamp, target, previous/current pointer, snapshot ID는 별도 deployment receipt에 기록한다.
- [ ] per-target transaction은 lock→control/guard/producers pre-state staging snapshot→producer timers/mirror/outbox pause→marker close→independent dispatcher stop→existing gateway embedded dispatcher off→active task/tmux 0 drain→SQLite backup API+Hermes target bytes/HEAD/approved-base-ref pre-state snapshot→canonical snapshot finalize→candidate immutable release stage/verify→clean-only Hermes runtime bootstrap 또는 existing checkout의 create-only approved ref 초기화→embedded dispatcher false 재검증→tests/hooks/guard manifest/Hermes patch/current switch/services/gateway/DB/canary/drift/coverage/SHA audit→success receipt fsync→supervisor closed-marker ready→marker open→producer pre-state restore 순서다. approved ref는 pinned object identity만 증명하고 Windows/VPS carried HEAD에 ancestry를 요구하지 않으며 exact supported target blob/AST/preimage를 별도로 검증한다. 중간 실패와 activation 실패 모두 verified snapshot rollback으로 fixed receipt를 `rolled-back`으로 바꾼다. gateway가 이미 down이면 stop은 멱등 no-op다.
- [ ] same-SHA release directory가 이미 있으면 candidate manifest와 전체 relative path/type/mode/file digest inventory가 exact-equal일 때만 read-only reuse한다. absent면 sibling temp extract/verify/fsync/atomic publish하고, tamper/missing/extra/symlink이면 기존 directory나 current pointer를 변경하지 않고 실패한다.
- [ ] gateway가 running이면 `hermes gateway restart`, down이면 start를 사용하고 60초 health timeout을 둔다. raw `systemctl restart`를 삭제한다.
- [ ] VPS script는 `--sha`, `--artifact`, `--artifact-sha256`, `--build-manifest`, repeated `--repo`만 받고 remote `git pull`을 하지 않는다.
- [ ] 각 target의 contract repository마다 `install-codex-hook.py --release RELEASE --manifest BUILD_MANIFEST --repo REPOSITORY`를 실행하고 같은 명령에 `--verify`를 추가해 `.codex/hooks.json`의 adapter/interpreter hash를 배포 후 다시 검증한다.
- [ ] rollback은 before receipt/build/snapshot/actual backup bytes 선검증→current producers pause→marker closed/dispatcher stopped→gateway graceful stop→previous guard/current/Hermes target/config/unit/DB/producer pre-state restore→daemon reload/Task restore→gateway pre-state/quick_check→old canary 또는 clean absence 확인→rolled-back receipt fsync→old dispatcher closed-marker activation→producer pre-state restore 순서다. `previous_release`는 old guard/dispatcher 축만 제어하고 Hermes/DB/service/hook는 snapshot entry를 독립 복원한다. 진짜 clean bootstrap만 receipt의 completed ownership journal pair를 요구하고 그 record가 소유한 root/DB/optional uv subset만 제거한다. previous release와 gateway state는 verified context에서만 읽는다.
- [ ] 운영 문서에 상태기계, GATE_ERROR recovery, manual unblock, canary reopen, exact-SHA deploy/rollback, hook 설치, unsupported raw SQL 경계를 구현과 같은 commit에 반영한다.
- [ ] RISK 주석을 current pointer switch, config replace, Hermes restore, DB restore 분기에 붙인다.
- [ ] GREEN과 static scan을 확인한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ops/test_deployment.py -q
rg -n "git pull|git add -A|systemctl restart|cat >.*service" forge/scripts/deploy.ps1 forge/scripts/deploy-vps.sh forge/ops/deployment.py
```

예상: 금지 패턴은 0건이며 테스트는 통과한다.

- [ ] Commit:

```powershell
git add forge/ops/deployment.py forge/schemas/build-manifest.schema.json forge/schemas/deployment-receipt.schema.json forge/scripts/deploy.ps1 forge/scripts/deploy-vps.sh forge/scripts/rollback.ps1 forge/scripts/rollback-linux.sh forge/scripts/rollback-vps.sh tests/ops/test_deployment.py docs/ops-guide.md docs/automation-architecture.md
git commit -m "feat: deploy Forge guards by exact artifact SHA"
```

## Task 16: 격리 E2E driver와 cleanup 경계를 구현한다

**Files:**
- Create: `forge/ops/e2e_driver.py`
- Create: `forge/ops/work_report.py`
- Modify: `forge/ops/slack_transport.py`
- Create: `forge/scripts/e2e-early-termination.py`
- Create: `forge/scripts/post-work-report.py`
- Create: `tests/integration/test_e2e_driver.py`
- Create: `tests/ops/test_work_report.py`

**Interfaces:**

```text
E2EMode = positive | invalid-handoff | receiptless | hook-skipped | cleanup
run_e2e(sha: str, mode: E2EMode, run_id: str | None = None) -> E2EResult
cleanup_run(run_id: str, expected_resources: Sequence[ResourceRef]) -> CleanupResult
python -m forge.ops.e2e_driver verify-thread-continuation --run-id RUN_ID --expect-same-thread
python -m forge.ops.e2e_driver verify-post-exit-rejection --run-id RUN_ID
python -m forge.ops.e2e_driver audit --run-id RUN_ID --max-threads 4 --require-empty-or-materialized-residual --require-consumed-hermes-receipt
python -m forge.ops.e2e_driver acceptance-report --spec SPEC --run-id RUN_ID --output FILE --require-count 20
python -m forge.ops.e2e_driver build-ops-evidence --sha SHA40 --build-manifest FILE --windows-receipt FILE --linux-receipt FILE --vps-receipt FILE --require-current-activation --max-canary-age-seconds 25200 --max-drift-age-seconds 7200 --output FILE
python -m forge.ops.e2e_driver promote-ops-evidence --repository OWNER/REPO --issue NUMBER --evidence FILE --marker forge-ops-evidence-v1 --upsert-exact-sha --set-deployed-sha-variable
python -m forge.ops.work_report render --channel CHANNEL --sha SHA40 --evidence FILE --output REQUEST_FILE
post-work-report.py --request-file REQUEST_FILE --env-file CODEX_WORK_REPORT_ENV --receipt RECEIPT_FILE
```

**Steps:**

- [ ] cleanup 범위와 mode state machine의 RED tests를 작성한다.

Test inventory:

- `test_e2e_resources_have_unique_run_id_and_exact_cleanup_scope`
- `test_negative_modes_never_accept_or_project_completion`
- `test_positive_mode_requires_hook_runner_receipt_done_pr_checks`
- `test_cleanup_refuses_resource_without_matching_run_tag`
- `test_cleanup_is_idempotent_after_partial_failure`
- `test_driver_rejects_non_deployed_or_non_green_sha`
- `test_e2e_driver_parser_matches_rollout_commands`
- `test_build_ops_evidence_rejects_target_sha_or_build_digest_mismatch`
- `test_promote_ops_evidence_paginates_scans_sets_deployed_sha_and_compensates_partial_failure`
- `test_work_report_scans_actual_request_before_transport`
- `test_work_report_never_serializes_or_logs_bot_token`
- `test_work_report_retry_reuses_client_msg_id_and_durable_receipt_without_duplicate_post`

Parser RED test는 rollout의 네 command를 그대로 parse한다.

```python
from forge.ops.e2e_driver import build_parser


def test_e2e_driver_parser_matches_rollout_commands() -> None:
    parser = build_parser()
    cases = (
        ["verify-thread-continuation", "--run-id", "run-1", "--expect-same-thread"],
        ["verify-post-exit-rejection", "--run-id", "run-1"],
        [
            "audit",
            "--run-id",
            "run-1",
            "--max-threads",
            "4",
            "--require-empty-or-materialized-residual",
            "--require-consumed-hermes-receipt",
        ],
        [
            "acceptance-report",
            "--spec",
            "docs/weapon/specs/2026-07-12-hermes-early-termination-guards-design.md",
            "--run-id",
            "run-1",
            "--output",
            "C:/evidence/final.json",
            "--require-count",
            "20",
        ],
        [
            "build-ops-evidence",
            "--sha",
            "a" * 40,
            "--build-manifest",
            "build-manifest.json",
            "--windows-receipt",
            "windows.json",
            "--linux-receipt",
            "linux.json",
            "--vps-receipt",
            "vps.json",
            "--require-current-activation",
            "--max-canary-age-seconds",
            "25200",
            "--max-drift-age-seconds",
            "7200",
            "--output",
            "ops-evidence.json",
        ],
        [
            "promote-ops-evidence",
            "--repository",
            "example/infinity-forge",
            "--issue",
            "77",
            "--evidence",
            "ops-evidence.json",
            "--marker",
            "forge-ops-evidence-v1",
            "--upsert-exact-sha",
            "--set-deployed-sha-variable",
        ],
    )
    for argv in cases:
        assert parser.parse_args(argv).command == argv[0]
```

- [ ] RED를 확인한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_e2e_driver.py -q
```

예상 RED: `forge.ops.e2e_driver` import가 실패한다.

- [ ] `run_e2e`가 40자리 deployed SHA와 green check를 preflight하고, 모든 생성 resource에 `forge-e2e:<run-id>` tag를 넣도록 구현한다.
- [ ] negative mode는 expected rejection과 snapshot immutability를 검증하고 accepted/projected 상태가 보이면 즉시 실패한다.
- [ ] positive mode는 issue→card→Codex→hook→post-exit→PR comment evidence→CI→Hermes receipt→projection 단계별 resource/evidence ID를 journal에 atomic write한다.
- [ ] cleanup은 journal과 remote tag가 모두 일치하는 resource만 닫거나 삭제하고, 제품 resource와 tag 없는 resource는 hard fail로 건너뛴다.
- [ ] driver는 production resource mutation 전에 `--mode`와 run ID를 log하고 secret-bearing environment를 evidence에서 제거한다.
- [ ] `build_parser()/main()`이 위 여섯 audit/report/evidence command를 소유한다. `build-ops-evidence`는 Task 13 library로 exact three-target/current activation/fresh canary+drift/same source+build canonical JSON을 원자 기록한다. `promote-ops-evidence`는 explicit canonical repository의 모든 issue comment page와 기존 `FORGE_DEPLOYED_SHA`를 읽어 same-SHA marker 0/1개만 create/update한 뒤 host repo variable을 같은 SHA로 set/read-back한다. 2개 이상이면 write 0회로 실패하고, 실제 request bytes를 secret scan한 뒤에만 transport를 호출한다. comment 성공 뒤 variable update/read-back 실패 시 이전 comment/variable을 exact 복원하는 compensating transaction을 실행하며, 복원 실패는 `GATE_ERROR`와 release rollback 대상이다. secondary repo variable은 건드리지 않는다. 나머지 command는 journal의 same-thread, post-exit rejection, thread/residual/receipt, spec 20개 mapping을 각각 재검증해 JSON 한 개와 exit 0/2만 출력한다.
- [ ] `forge.ops.work_report render`는 evidence digest/channel/source SHA에서 deterministic UUID `client_msg_id`와 `report_id`를 만들고 `chat.postMessage`에 그대로 전달할 canonical `{channel,text,client_msg_id}` JSON을 원자 기록한다. `post-work-report.py`는 Task 12의 유일한 `forge.ops.slack_transport.post_scanned_slack_request`를 재사용해 지정된 env file의 app/channel identity를 host-side read-back하고 실제 request bytes와 모든 non-empty secret value를 `scan_bytes(label, request_bytes, secret_values)`에 먼저 통과시킨 뒤에만 Slack Web API transport를 호출한다. 전송 전 `schema_version,request_sha256,client_msg_id,state=pending` receipt를 flush/fsync/replace하고, 성공 response의 exact channel/ts를 read-back한 뒤 `state=sent`로 원자 전이한다. same request digest retry는 sent receipt면 transport 0회, pending이면 같은 `client_msg_id`로 재호출해 Slack duplicate suppression 결과를 수렴시키며 새 ID를 만들지 않는다. fake transport는 API accept 직후 process crash를 주입해 재시도 뒤 visible post count 1과 durable channel/ts receipt를 검증한다. finding/error면 transport 0회, exit 2이며 token/header/raw response를 출력하지 않는다. canary와 work report가 서로 다른 Slack HTTP/receipt 구현을 가지면 contract test를 실패시킨다.
- [ ] GREEN과 전체 integration 회귀를 확인한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_e2e_driver.py -q
.\.venv\Scripts\python.exe -m pytest tests/integration -q
```

- [ ] Commit:

```powershell
git add forge/ops/e2e_driver.py forge/ops/work_report.py forge/ops/slack_transport.py forge/scripts/e2e-early-termination.py forge/scripts/post-work-report.py tests/integration/test_e2e_driver.py tests/ops/test_work_report.py
git commit -m "test: add isolated Hermes guard E2E driver"
```

## Task 17: 전 로컬 테스트와 PR CI를 통과시킨다

**Files:**
- External evidence: `%LOCALAPPDATA%\InfinityForge\state\evidence\hermes-guard-local-verification.json`

No implementation file is modified in this Task. A defect starts a new TDD commit and the release convergence loop from Task 17.

**Consumes / Produces:**

```text
Consumes: clean candidate SHA40, bootstrap TaskContract, per-repo PRs and evidence comments
Produces: two green named checks, immutable build manifest, local verification evidence
Failure: no deployable artifact; candidate SHA is rejected
```

**Steps:**

- [ ] `weapon:requesting-code-review`로 spec 대비 코드 리뷰를 수행하고 P0/P1을 먼저 수정한다.
- [ ] Windows 개발 venv에서 전체 suite를 fresh 실행한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m compileall forge
git diff --check
```

- [ ] bash와 PowerShell 계약을 fresh 실행한다.

```powershell
pwsh -NoProfile -File forge/scripts/install-windows.ps1 -Phase Services -ReleasePath C:\staged -Manifest C:\staged\build-manifest.json -RepoPaths @('C:\01.project\INFINITY_FORGE') -PythonPath .\.venv\Scripts\python.exe -BootstrapRepository OWNER/REPO -PlanOnly
```

```bash
bash -n forge/scripts/*.sh
systemd-analyze verify forge/systemd/*.service forge/systemd/*.timer
```

- [ ] isolated Hermes checkout의 targeted/full tests와 두 supported-base patch apply/rollback을 fresh 실행한다.
- [ ] `RISK(` 목록을 검토해 각 위험에 test 또는 rollback 단계가 있는지 매핑한다.

```powershell
rg -n "RISK\(" forge
```

- [ ] branch를 push하고 PR을 만든다. Actions의 두 named checks가 실제로 생성되고 success인지 `gh` API로 확인한다.
- [ ] PR evidence bundle이 base/head/source issue/AC/command/receipt를 다시 검증했는지 log에서 확인한다.
- [ ] external verification evidence에 정확한 명령, exit code, check URL, commit SHA, artifact SHA를 원자 기록하고 PR evidence comment에 digest만 게시한다. secret 또는 raw token은 기록하지 않는다.

**Gate:** 두 platform check가 없거나 red면 Task 18로 진행하지 않는다.

## Task 18: Windows 로컬에 동일 SHA를 배포한다

**Files:**
- External evidence: `%LOCALAPPDATA%\InfinityForge\state\evidence\hermes-guard-windows-deployment.json`
- External operational state: Windows guard release, Hermes target files, config, Scheduled Tasks

**Command interface:**

```powershell
$Repo = 'C:\01.project\INFINITY_FORGE'
$Sha = (git rev-parse HEAD).Trim()
$BuildRoot = Join-Path $env:LOCALAPPDATA "InfinityForge\builds\$Sha"
$Manifest = Join-Path $BuildRoot 'build-manifest.json'
$Artifact = Join-Path $BuildRoot 'infinity-forge.tar'
$ArtifactHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Artifact).Hash.ToLowerInvariant()
$WindowsReceipt = Join-Path $env:LOCALAPPDATA 'InfinityForge\state\deployment-receipt-v1.json'
$BeforeReceipt = Join-Path $env:LOCALAPPDATA 'InfinityForge\state\evidence\windows-before-rollback.json'
$BootstrapRepository = (gh repo view --json nameWithOwner --jq .nameWithOwner).Trim()
pwsh -NoProfile -File forge/scripts/deploy.ps1 -Sha $Sha -Artifact $Artifact -ArtifactSha256 $ArtifactHash -BuildManifest $Manifest -Targets Windows -RepoPaths @($Repo) -BootstrapRepository $BootstrapRepository -Apply
Copy-Item -LiteralPath $WindowsReceipt -Destination $BeforeReceipt -Force
pwsh -NoProfile -File forge/scripts/rollback.ps1 -BeforeReceipt $BeforeReceipt -BuildManifest $Manifest -RepoPaths @($Repo)
```

**Steps:**

- [ ] 현재 gateway down, Scheduled Task 0개, Startup VBS 3개, Hermes dirty checkout 상태를 배포 preflight evidence로 기록한다.
- [ ] active Hermes tasks/tmux 0을 확인하고 canary marker를 닫은 뒤 dispatcher가 없거나 stopped임을 확인한다.
- [ ] DB `.backup`, Hermes target files, config, Task inventory, current release pointer를 snapshot한다.
- [ ] Task 17에서 확정한 project SHA/artifact SHA를 staged release에 설치한다.
- [ ] Hermes patch installer가 Windows supported blob+함수 preimage를 확인하고 대상 파일만 stage/commit하는지 확인한다. 사용자 unrelated dirty 파일은 전후 status diff가 같아야 한다.
- [ ] config를 atomic update해 `dispatch_in_gateway=false`를 확인한다.
- [ ] `\INFINITY_FORGE\Dispatcher`, Canary, Drift Scheduled Task를 등록한다. `AccessDenied`면 Startup VBS로 우회하지 말고 배포를 rollback하고 작업을 중단한다.
- [ ] gateway가 down이므로 `hermes gateway start` 후 60초 health check를 수행한다.
- [ ] DB quick_check와 Windows current-user-only ACL, targeted Hermes tests, local Forge tests, canary, supervisor child, drift, deployed SHA를 검증한다.
- [ ] stale marker negative canary로 dispatcher만 stopped, gateway healthy를 확인한 뒤 positive canary로 복구한다.
- [ ] external evidence에 Task XML/상태, process IDs, hashes, DB/service 결과를 기록한다. candidate Git tree는 변경하지 않는다.

**Gate:** Scheduled Task 등록 또는 live canary가 실패하면 rollback하고 release convergence loop를 실행한다. Linux/VPS로 진행하지 않는다.

## Task 19: 일반 Linux clean install과 rollback을 WSL Ubuntu staging에서 검증한다

**Files:**
- External evidence: `%LOCALAPPDATA%\InfinityForge\state\evidence\hermes-guard-linux-staging.json` 및 WSL `${XDG_STATE_HOME:-~/.local/state}/infinity-forge/evidence/linux-staging.json`

No implementation file is modified in this Task. A defect triggers rollback and the release convergence loop.

**Command interface:**

```powershell
$Repo = 'C:\01.project\INFINITY_FORGE'
$Sha = (git rev-parse HEAD).Trim()
$BuildRoot = Join-Path $env:LOCALAPPDATA "InfinityForge\builds\$Sha"
$Manifest = Join-Path $BuildRoot 'build-manifest.json'
$Artifact = Join-Path $BuildRoot 'infinity-forge.tar'
$ArtifactHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Artifact).Hash.ToLowerInvariant()
$BootstrapRepository = (gh repo view --json nameWithOwner --jq .nameWithOwner).Trim()
$LinuxRepo = "/home/immortal0900/work/INFINITY_FORGE/$Sha"
wsl.exe -d Ubuntu -u root -- loginctl enable-linger immortal0900
pwsh -NoProfile -File forge/scripts/deploy.ps1 -Sha $Sha -Artifact $Artifact -ArtifactSha256 $ArtifactHash -BuildManifest $Manifest -Targets Linux -RepoPaths @($LinuxRepo) -BootstrapRepository $BootstrapRepository -Apply
wsl.exe -d Ubuntu -- bash -lc 'systemctl --user is-active forge-dispatcher.service && systemctl --user list-timers --all'
wsl.exe -d Ubuntu -- env FORGE_LINUX_REPO=$LinuxRepo bash -lc 'set -euo pipefail; install -D -m 600 ~/.local/state/infinity-forge/deployment-receipt-v1.json ~/.local/state/infinity-forge/evidence/linux-before-rollback.json; ~/.local/share/infinity-forge/current/forge/scripts/rollback-linux.sh --target linux --before-receipt ~/.local/state/infinity-forge/evidence/linux-before-rollback.json --build-manifest ~/.local/share/infinity-forge/current/build-manifest.json --repo "$FORGE_LINUX_REPO"'
```

**Steps:**

- [ ] WSL distro `Ubuntu`, user `immortal0900`, systemd running, Python 3.12.3 상태와 기존 Forge/Hermes unit 0개를 기록한다. passwordless sudo가 없으므로 deployment mutation 전에 root boundary에서 Linger를 한 번 enable하고 일반 사용자 read-back을 요구한다.
- [ ] Windows repo의 committed object를 `git clone --no-local --no-checkout`으로 Linux filesystem `$LinuxRepo`에 복사해 exact `$Sha` detached checkout을 만들고 hook은 그 경로에만 설치한다. `/mnt/c` hook target은 거절하며 Windows `.codex/hooks.json` digest가 WSL 전후 불변인지 확인한다.
- [ ] Task 18과 동일한 exact artifact/SHA로 Hermes v0.18.2와 trusted guard release를 clean install한다.
- [ ] `kanban.dispatch_in_gateway=false`, POSIX DB mode 600, checked-in systemd units/timers, linger, current release pointer와 hashes를 검증한다.
- [ ] `systemd-analyze verify`, targeted Hermes tests, full Forge tests를 WSL interpreter에서 실행한다.
- [ ] canary positive를 실행해 marker→dispatcher child를 확인하고 gateway health가 독립임을 확인한다.
- [ ] marker를 의도적으로 stale/mismatch로 바꿔 5초 내 dispatcher stop, gateway 유지, drift alert를 확인한다.
- [ ] invalid handoff와 receiptless complete fixture가 tool/CLI/Dashboard 경로에서 거절되고 child가 ready 되지 않는지 확인한다.
- [ ] `wsl.exe --terminate Ubuntu` 후 재시작해 systemd user units/timers와 linger 동작을 확인한다.
- [ ] rollback을 실행해 이전 release/config/unit/Hermes target을 복원하고 DB quick_check를 확인한다.
- [ ] 같은 SHA를 forward redeploy해 멱등성과 canary reopen을 확인한다.
- [ ] external evidence에 명령, exit, PID/service state, SHA, restart, rollback/forward 결과를 기록하고 Windows copy에는 digest만 동기화한다.
- [ ] defect가 있으면 WSL을 rollback하고 새 TDD commit/SHA로 Task 17→18→19를 전부 다시 실행한다.

**Gate:** Linux clean install/restart/rollback/forward가 모두 green이 아니면 VPS로 진행하지 않는다.

## Task 20: Ubuntu VPS 실운영에 동일 SHA를 배포한다

**Files:**
- External evidence: VPS `${XDG_STATE_HOME:-~/.local/state}/infinity-forge/evidence/vps-deployment.json` 및 로컬 digest copy
- External operational state: VPS release, Hermes target files, config, systemd user units/timers

**Command interface:**

```powershell
$Repo = 'C:\01.project\INFINITY_FORGE'
$Sha = (git rev-parse HEAD).Trim()
$BuildRoot = Join-Path $env:LOCALAPPDATA "InfinityForge\builds\$Sha"
$Manifest = Join-Path $BuildRoot 'build-manifest.json'
$Artifact = Join-Path $BuildRoot 'infinity-forge.tar'
$ArtifactHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Artifact).Hash.ToLowerInvariant()
$BootstrapRepository = (gh repo view --json nameWithOwner --jq .nameWithOwner).Trim()
pwsh -NoProfile -File forge/scripts/deploy.ps1 -Sha $Sha -Artifact $Artifact -ArtifactSha256 $ArtifactHash -BuildManifest $Manifest -Targets Vps -RepoPaths @('/home/ubuntu/work/INFINITY_FORGE') -BootstrapRepository $BootstrapRepository -Apply
ssh -o BatchMode=yes ubuntu@51.222.27.48 'set -euo pipefail; install -D -m 600 ~/.local/state/infinity-forge/deployment-receipt-v1.json ~/.local/state/infinity-forge/evidence/vps-before-rollback.json; ~/.local/share/infinity-forge/current/forge/scripts/rollback-vps.sh --before-receipt ~/.local/state/infinity-forge/evidence/vps-before-rollback.json --build-manifest ~/.local/share/infinity-forge/current/build-manifest.json --repo /home/ubuntu/work/INFINITY_FORGE'
```

**Steps:**

- [ ] VPS current project/Hermes SHA, active gateway, embedded dispatcher default, unit inventory, active tasks/tmux, DB mode/quick_check를 preflight 기록한다.
- [ ] exact artifact를 SCP하고 remote SHA-256이 manifest와 같은지 확인한다. remote `git pull`을 실행하지 않는다.
- [ ] marker close, independent dispatcher stop, active task drain 후 DB/config/unit/current/Hermes target snapshot을 만든다.
- [ ] Hermes patch installer가 VPS supported blob `6150b141537b947a2a89d19b13be4fbad2330711`와 full hunk hashes를 확인하고 기존 unrelated carried change를 보존하는지 확인한다.
- [ ] project release와 checked-in units를 stage하고 current pointer를 atomic switch한다.
- [ ] `dispatch_in_gateway=false`를 확인한 뒤 active gateway에 `hermes gateway restart`를 사용한다.
- [ ] DB quick_check/mode, targeted Hermes tests, systemd verify, canary, dispatcher, drift, mirror, coverage, deployed SHA를 순서대로 확인한다.
- [ ] VPS에서 controlled rollback→health/old canary→동일 SHA forward deploy를 한 번 리허설한다.
- [ ] marker stale negative를 실행해 dispatcher만 중단되고 gateway/독립 task 상태 조회가 유지되는지 확인한 뒤 복구한다.
- [ ] external evidence에 정확한 명령/exit, unit states, PID, hashes, rollback/forward, DB 결과를 기록하고 로컬에는 digest만 동기화한다.
- [ ] defect가 있으면 VPS를 rollback하고 Windows/Linux도 previous common release로 되돌린 뒤 새 TDD commit/SHA로 Task 17부터 다시 실행한다.

**Gate:** Windows, WSL, VPS deployment manifest의 project/guard/patch SHA가 모두 동일하지 않으면 E2E를 실행하지 않는다.

## Task 21: 격리된 음성·양성 live E2E와 최종 수용 감사를 수행한다

**Files:**
- External evidence: `%LOCALAPPDATA%\InfinityForge\state\evidence\hermes-guard-final-acceptance.json`
- External evidence: bootstrap GitHub issue/PR evidence comment

**Command interface:**

```powershell
$Sha = (git rev-parse HEAD).Trim()
$Runs = @()
foreach ($Mode in @('invalid-handoff','receiptless','hook-skipped','positive')) {
  $Runs += (.\.venv\Scripts\python.exe forge/scripts/e2e-early-termination.py --sha $Sha --mode $Mode | ConvertFrom-Json)
}
foreach ($Run in $Runs) {
  .\.venv\Scripts\python.exe forge/scripts/e2e-early-termination.py --sha $Sha --mode cleanup --run-id $Run.run_id
}
```

**Steps:**

- [ ] Task 16에서 CI를 통과하고 세 target에 배포된 E2E driver의 `--sha SHA --mode positive|invalid-handoff|receiptless|hook-skipped`와 cleanup 전용 `--sha SHA --mode cleanup --run-id RUN_ID` 인터페이스만 사용한다. parser RED test는 cleanup의 missing/blank run-id를 exit 2로, 다른 mode의 `--run-id`를 invalid로 고정한다. live 검증 중 driver 코드를 수정하지 않는다.
- [ ] 모든 issue/branch/card/PR에 unique run ID tag가 붙고 cleanup이 같은 tag가 있는 resource만 닫거나 삭제하는지 journal과 remote를 대조한다.
- [ ] Windows에서 실제 Codex Stop hook `TESTS_FAILED` fixture를 실행하고 JSONL의 thread ID가 continuation 전후 동일한지 확인한다.
- [ ] hook 파일을 격리 fixture에서 제거한 `hook-skipped` 실행이 runner post-exit에서 거절되는지 확인한다.
- [ ] invalid handoff와 receiptless complete를 tool, CLI, Dashboard로 시도해 모두 거절, task/run/event/child snapshot 무변경, GitHub/spec 미투영을 확인한다.
- [ ] worker crash/respawn fixture로 persistent ledger가 최대 4 thread를 넘지 않고 GATE_ERROR recovery가 기존 예약 외 새 slot을 추가하지 않는지 live 확인한다.
- [ ] positive issue를 생성하고 canonical card→Codex commit/PR→hook→post-exit verifier→repo별 evidence comment→Windows/Ubuntu checks green→Hermes phase final receipt/atomic done→projection 전체를 완료한다.
- [ ] source issue body/AC를 격리 test issue에서 수정해 drift가 탐지하고 완료를 보류하는지 확인한 뒤 원복/cleanup한다.
- [ ] 세 target canary와 live E2E가 green인 exact implementation PR head `$Sha`를 `gh pr merge --merge --match-head-commit $Sha`로 병합한다. squash/rebase를 금지하고 merge commit이 deployed head의 descendant인지 API compare로 확인한 뒤 merge commit의 main push run에서 두 stable check가 exact-one success인지 read-back한다.
- [ ] merge 전 실패는 source history 변경 없이 중단한다. merge 뒤 defect는 main rewrite로 숨기지 않고 세 host rollback→corrective PR/new SHA→Task 17 전체 수렴으로 복구한다.
- [ ] 최종 20개 spec 수용 기준을 evidence path와 실제 명령/결과에 1:1 매핑한다.
- [ ] source tree, Git commits, CI artifact manifest, PR evidence comment, 생성할 Slack 완료 payload에 credential scanner를 실행하고 0건인지 확인한다. scanner output에는 matched value가 없어야 한다.
- [ ] Task 15에서 미리 반영한 운영 문서가 live 결과와 일치하는지 검증한다. 불일치는 제품 defect로 처리해 release convergence loop를 실행한다.
- [ ] 모든 runtime test resource를 run ID 기반 cleanup하고 제품 issue/card/PR에는 손대지 않았음을 확인한다.
- [ ] live E2E에서 제품 defect가 확인되면 세 host marker를 닫고 dispatcher를 중단한 뒤 test resource cleanup→세 host previous common release rollback→TDD fix/new SHA→Task 17부터 전체 release cycle을 반복한다. live host에서 ad-hoc patch하지 않는다.
- [ ] fresh final verification을 실행한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m compileall forge
git diff --check
```

```bash
bash -n forge/scripts/*.sh
systemd-analyze verify forge/systemd/*.service forge/systemd/*.timer
```

- [ ] GitHub checks, Windows Scheduled Tasks, WSL/VPS systemd units, DB quick_check/mode, canary/drift heartbeat, deployment manifest SHA를 마지막으로 다시 읽는다.
- [ ] exact three-target/current activation/7시간 canary/2시간 drift ops evidence를 만들고 `promote-ops-evidence --repository $BootstrapRepository --issue $BootstrapIssue --evidence $OpsEvidence --marker forge-ops-evidence-v1 --upsert-exact-sha --set-deployed-sha-variable`로 중앙 comment와 host-only `FORGE_DEPLOYED_SHA`를 보상 가능한 transaction으로 승격한 뒤 exact read-back한다.
- [ ] `weapon:verification-before-completion`과 `weapon:requesting-code-review`를 적용하고 P0/P1이 0개인지 확인한다.
- [ ] merge/main checks/coverage/final immutable assertions 뒤에만 final evidence와 host-only `FORGE_DEPLOYED_SHA`를 bootstrap issue에 보상 가능한 transaction으로 승격한다. candidate local Git tree는 rollout 시작 SHA에서 변하지 않아야 한다.
- [ ] ops evidence 승격까지 성공한 뒤에만 `codex work report` Slack 완료 알림을 마지막 side effect로 전송한다.

- [ ] 다음 술어가 모두 참일 때만 Ralph completion promise를 출력한다.

```text
all_spec_acceptance_evidenced
AND windows_ubuntu_checks_green
AND windows_linux_vps_same_sha
AND negative_e2e_not_completed_or_projected
AND positive_e2e_receipt_consumed
AND rollback_rehearsed
AND no_p0_p1_review_findings
```

완료 문자열: `HERMES_EARLY_TERMINATION_GUARDS_COMPLETE`

## Risk notes

| Category | 위험 | 방어/복구 |
|---|---|---|
| `breaking` | Hermes CLI/schema와 Forge JSON 계약 변경 | protected policy opt-in, additive migration, versioned schema |
| `race` | verifier preflight 후 repo 변경, 중복 session, receipt replay | dedicated workspace, short expiry, run CAS, reserve-before-spawn, unique ledger |
| `data-loss` | DB/file/config 배포 중 중단 | drain, `.backup`, target snapshot, atomic replace; DB restore는 integrity failure만 |
| `security` | worker가 verifier/state/manifest를 변조 | worktree 밖 trusted release, fixed roots, artifact SHA, mode 600 |
| `side-effect` | canary가 전체 gateway를 중단 | marker가 dispatcher child만 제어, gateway health assertion |
| `side-effect` | Windows Task 정책 차단을 fallback으로 숨김 | AccessDenied hard fail+rollback, Startup VBS 자동 우회 금지 |
| `breaking` | private/free GitHub required checks 부재 | named Actions 존재/success를 deploy와 projection이 직접 강제 |

## 자체 검토 체크리스트

- [ ] 승인 spec의 20개 수용 기준이 요구사항 추적표와 Task 21 evidence에 모두 매핑돼 있다.
- [ ] `TaskContract`, `Handoff`, `Receipt`, `VerificationResult`, `CompletionInspector` 이름이 전 Task에서 일치한다.
- [ ] `completion_policy=forge-v1`, thread 4, runtime 3600, token 200000, command 900, hook 3660, phase expiry 65분/24시간/2시간/15분 값이 모순 없이 일치한다.
- [ ] Windows/Linux/VPS 모두 build/install/canary/drift/rollback/live E2E 경로가 있다.
- [ ] PR CI evidence transport가 있으며 unit test만으로 동일 증거를 검증했다고 주장하지 않는다.
- [ ] Hermes whole-file hash 차이를 숨기지 않고 두 supported blob+함수 preimage를 검증한다.
- [ ] preflight↔DB의 외부 repo TOCTOU를 완전 원자라고 표현하지 않고 완화책과 경계를 기록한다.
- [ ] raw SQL, private/free GitHub, Windows Scheduled Task policy 경계를 명시했다.
- [ ] placeholder, 미정 파일, 무근거 fallback, destructive git 명령이 없다.

## 변경이력

- 2026-07-12 | 구현 계획 작성 | 변경: 승인된 방어안 2를 guard core, Hermes carried patch, projection, CI, Windows/Linux/VPS exact-SHA 배포·롤백·E2E의 21개 Task로 분해 | 이유: 사용자의 spec 승인과 실운영 적용 요청 | 검증: spec 20개 수용 기준 추적표 작성, 세 병렬 조사 결과 반영; 코드/실운영 검증은 실행 단계에서 수행
- 2026-07-12 | 배포 CLI 교차계약 정합화 | 변경: umbrella의 build/deploy/rollback 예시를 ops·rollout subplan의 artifact/hash/manifest/repository/before-receipt exact interface로 통일 | 이유: 상위 계획의 축약 명령이 구현 parser와 달라지는 것을 방지 | 검증: 구현 전 계획 단계이며 세 문서의 명령 문자열 대조와 fenced PowerShell parser로 확인
- 2026-07-12 | 승인 spec 기반 최초 CI·host-only ops·3-target 수렴 계약 보강 | 변경: 별도 CI onboarding PR, Windows GitHub 로그인 prerequisite, current-repo CI slice, canonical ops host 변수, `FORGE_DEPLOYED_SHA` evidence promotion, 7시간 canary, WSL 독립 clone/Linger, scheduled argv, clean-bootstrap ownership journal, same-SHA release reuse, approved-base create-only object identity와 pre-state rollback을 상위 Task에 반영 | 이유: 최초 PR 순환, private repo token 범위, shared hook overwrite, 실제 Windows/VPS divergent Hermes HEAD, 부분 rollback 위험을 제거 | 검증: 구현 전 계획 단계에서 Python/PowerShell/Bash/YAML/JSON fenced parser, exact CLI/금지 패턴 scan, 독립 P0/P1 review 대상으로 등록
