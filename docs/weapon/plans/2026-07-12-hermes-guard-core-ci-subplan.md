# Hermes Guard Core 및 PR CI 구현 계획

> **Agentic worker(Agent 실행자)용:** REQUIRED SUB-SKILL: 이 plan을 task 단위로 구현하려면 `weapon:subagent-driven-development`(권장) 또는 `weapon:executing-plans`를 사용한다. 진행 추적에는 checkbox(`- [ ]`) 문법을 쓴다.

**Goal:** Hermes 보호 태스크가 사용하는 공통 계약, 다중 저장소 증거, Codex Stop hook, post-exit runner, phase별 receipt, PR evidence transport, Windows·Ubuntu CI를 구현해 검증되지 않은 종료가 다음 완료 단계로 넘어가지 못하게 한다.

**Architecture:** Python 3.11 공통 코어가 contract·handoff·repository·GitHub reference·command evidence를 검증하고 phase별 receipt를 발급한다. Stop hook은 즉시 판정만 담당하고, runner는 worktree 밖 원자 state에서 Codex session과 PR check를 관리한다. PR evidence는 Git tree에 넣지 않고 각 repository의 PR comment에 동일 bundle을 게시하며, GitHub Actions는 event 종류와 strict host flag에 따라 PR evidence, merge-group, regression, canonical-host-only ops audit 중 하나를 실행한다.

**Tech Stack:** Python 3.11+, `jsonschema>=4.23,<5`, `pytest>=8,<9`, Git CLI, GitHub CLI `gh`, Codex CLI 0.144.1, GitHub Actions `ubuntu-latest`·`windows-latest`, PowerShell 7+, Bash.

## Global Constraints

1. 실행 전 `weapon:using-git-worktrees`, 각 Task에서 `weapon:test-driven-development`, 완료 주장 전 `weapon:verification-before-completion`을 적용한다.
2. Python minimum은 3.11이다. runtime dependency는 `requirements.lock`에 transitive package와 SHA-256 hash까지 고정하고 `pip --require-hashes`로 설치한다.
3. production state root는 Windows `%LOCALAPPDATA%\InfinityForge\state`, Linux `${XDG_STATE_HOME:-~/.local/state}/infinity-forge`다. production CLI에는 state root override를 노출하지 않는다.
4. POSIX state/evidence 파일은 mode `0600`, Windows는 current-user-only ACL을 적용한다. worker가 쓰는 repository 안 파일을 trusted state로 사용하지 않는다.
5. verification command는 non-empty `Sequence[str]`인 `argv`만 허용한다. 모든 subprocess는 `shell=False`다.
6. 모델 산출 handoff schema/작업 결과 불충족은 `TESTS_FAILED`다. immutable task contract schema/config/I/O/lock/command missing·timeout/`gh` auth·403·429·5xx·invalid JSON은 `GATE_ERROR`다. 예상하지 못한 exception도 최상위 CLI에서 `GATE_ERROR`로 변환한다.
7. 기본 budget은 고유 Codex thread 4개, task runtime 3,600초, 누적 200,000 token, command당 900초다. preflight 오류는 예약 전 실패하고 Popen 자체 오류는 `launching` 예약을 해제한다. Popen 성공 뒤 thread ID를 받지 못한 `unknown_started` slot만 1개로 센다.
8. `GATE_ERROR` recovery는 이미 `unknown_started|started`인 slot을 되돌리지 않고 같은 slot reverify/resume만 허용하며 새 slot을 추가 할당하지 않는다.
9. receipt phase 정책은 정확히 다음과 같다.
   - `stop`: `issued_at + 3,900초`, non-consumable.
   - `post-exit`: `issued_at + 86,400초`, non-consumable.
   - `ci`: `issued_at + 7,200초`, non-consumable.
   - `hermes`: `issued_at + 900초`, single-use consumable.
10. 모든 receipt는 `issued_at_s`와 integer `expires_at_s`를 가진다. wall-clock expiry 전이라도 task/run/contract/handoff/repository/issue/head/evidence digest가 바뀌면 즉시 stale이다. 완료 권한은 900초 `hermes` receipt만 가진다.
11. D24 다중 repository task는 repository마다 baseline, head, PR number를 가진다. runner는 모든 PR에 전체 multi-repo tuple을 포함한 evidence comment를 하나씩 게시한다. GitHub Actions의 `github.token`은 event의 current repository slice만 API로 재검증하고 secondary repository API를 호출하지 않는다. CI 밖 rollout runner가 명시적 repository credential/context로 모든 repository의 live PR/head/check를 aggregate한 뒤에만 complete-ready를 만든다.
12. `prepare`는 baseline을 잡기 전에 각 repository default branch에 Forge guard workflow와 두 stable named checks가 실제 존재하는지 GitHub API로 확인한다. 하나라도 미온보딩이면 task를 만들거나 실행하지 않고 `REPO_GUARD_CI_NOT_ONBOARDED` GATE_ERROR로 보류한다.
13. evidence comment는 UTF-8 60,000 bytes 이하이며 marker `forge-evidence-v1`을 사용한다. 같은 task/run의 comment가 PR 하나에 둘 이상이면 `GATE_ERROR`다.
14. workflow event 분기는 다음으로 고정한다.
   - `pull_request` opened/synchronize/reopened/ready_for_review: umbrella Task 0의 repository별 onboarding-only PR merge와 repository variable provisioning이 먼저 완료된 뒤 생성하는 첫 protected 구현 PR부터 comment의 full multi-repo tuple 무결성과 current repository slice를 예외 없이 검증한다. 다른 repository의 live 상태는 Actions job이 조회하지 않는다.
   - `push` main은 regression, `merge_group`은 associated PR/head tuple과 prior named checks를 검증한다. `schedule`/`workflow_dispatch`는 strict repository variable `FORGE_OPS_HOST=true`인 canonical host repository에서만 latest deployed evidence의 read-only canary/drift `ops_audit`로 분기한다. `FORGE_OPS_HOST=false`인 secondary repository에서는 `regression`으로 분기하고 ops audit step·bootstrap issue API를 실행하지 않는다. 어떤 비-PR event도 `pull_request.*` field를 직접 요구하지 않는다.
15. “최초 구현 PR도 compatibility/enforcement 예외가 없다”는 umbrella Task 0의 별도 onboarding-only PR merge와 repository variable provisioning **이후 첫 protected 구현 PR**부터 적용한다는 뜻이다. onboarding PR 자체가 아직 default branch에 없는 workflow로 자신을 검증하는 순환 의존은 만들지 않는다. 구현 task 실행 전에 bootstrap GitHub issue를 만들고, 그 issue에서 local `TaskContract`를 준비하고, branch runner가 각 구현 PR에 evidence comment를 게시한다. comment 없이 먼저 실패한 current-head run은 comment 게시 뒤 정확히 한 번 rerun한다.
16. PR CI job 안에서는 현재 실행 중인 자기 check의 green 여부를 묻지 않는다. rollout runner가 CI 밖에서 repository를 명시한 API client로 모든 repository의 `guard-contract (ubuntu-latest)`와 `guard-contract (windows-latest)` 존재/current-head success를 aggregate한다.
17. source, evidence comment, release file list, CI artifact metadata, Slack message fixture에 secret scan을 적용한다. finding에는 path와 rule ID만 남기고 matched value는 출력하지 않는다.
18. hook 성공 stdout도 JSON 하나만 출력한다. `TESTS_FAILED`는 `decision:block`, `GATE_ERROR`와 recursive hook은 `continue:false`다. 계약은 [Codex Hooks](https://learn.chatgpt.com/docs/hooks)를 기준으로 한다.
19. 각 Task는 RED command와 예상 failure를 기록한 뒤 최소 GREEN을 만들고 관련 test, 전체 guard regression, `git diff --check`를 순서대로 실행한다.
20. 아래 checkbox 한 개는 2~5분짜리 단일 행동으로 취급한다. 한 checkbox에서 두 interface를 동시에 구현하지 않고, 중간 실패가 생기면 해당 checkbox 안에서 test를 다시 RED로 고정한다.

---

## 범위와 의존성

이 subplan은 umbrella plan의 Task 2~8과 Task 14 중 CI 부분만 구현한다. Hermes SQLite carried patch, label/spec projection, canary/drift, systemd, Scheduled Task, live deployment는 별도 subplan의 책임이다.

```text
Task 1 contracts
  ├─ Task 2 git evidence
  └─ Task 3 GitHub/command evidence
       └─ Task 4 verifier/phase receipts
            └─ Task 5 persistent runner
                 ├─ Task 6 Stop hook/CLI
                 └─ Task 7 trusted release
                      └─ Task 8 secret scanner
                           └─ Task 9 multi-repo PR evidence
                                └─ Task 10 workflow/full PR enforcement
```

| Core/CI 수용 기준 | Task | Fresh evidence |
|---|---:|---|
| strict AC partition·빈 residual 허용·issue/ADR-only residual | 1, 3 | contract/reference tests |
| committed clean diff와 D24 multi-repository equality | 2 | temporary Git repository tests |
| command nonzero와 verifier 장애 분리 | 3, 4 | typed failure tests |
| phase별 receipt expiry와 digest 변경 즉시 stale | 4, 9 | 3,900/86,400/7,200/900초 경계 test |
| 고유 thread 4개와 reserve-before-spawn | 5 | crash/respawn state test |
| GATE_ERROR recovery가 새 slot을 만들지 않음 | 5 | reservation identity assertion |
| Codex Stop TESTS_FAILED same-thread continuation | 6 | hook JSON unit test와 opt-in live test |
| trusted worktree 밖 deterministic artifact | 7 | two-build byte/hash equality |
| credential value 비노출 | 8, 10 | scanner output test와 matrix scan |
| 모든 repository PR에 exactly-one evidence comment | 9 | two-repository fake GitHub test |
| Windows·Ubuntu named checks | 10 | workflow contract와 실제 PR checks |
| onboarding merge·repository variable provisioning 후 첫 protected 구현 PR부터 full evidence enforcement | 9, 10 | umbrella Task 0 onboarding prerequisite와 bootstrap issue/local contract/comment integration test |
| non-PR event가 PR payload 없이 merge/regression/host-only audit 분기 | 10 | host·secondary event table test |

## 파일 지도

| 파일 | 책임 |
|---|---|
| `pyproject.toml` | package metadata, Python floor, console script, test dependency |
| `requirements.lock` | self-contained trusted release용 hashed runtime lock |
| `forge/__init__.py` | Forge Python package marker |
| `forge/guard/errors.py` | `TESTS_FAILED`와 `GATE_ERROR` typed exception |
| `forge/guard/contract.py` | frozen contract/handoff/receipt types, strict schema parsing, canonical JSON |
| `forge/guard/git_state.py` | repository baseline, clean committed diff, changed-file equality |
| `forge/guard/references.py` | paginated GitHub issue/ADR/PR 조회와 HTTP failure 분류 |
| `forge/guard/commands.py` | argv-only command execution, environment scrub, output digest |
| `forge/guard/verifier.py` | phase-independent verification pipeline과 phase receipt issuance |
| `forge/guard/state.py` | fixed state root, lock, atomic JSON write, permissions |
| `forge/guard/runner.py` | Codex reservation/thread/token ledger, post-exit verify, PR check wait |
| `forge/guard/stop_hook.py` | Codex Stop payload parsing과 JSON response rendering |
| `forge/guard/evidence_bundle.py` | immutable bundle, per-PR comment upsert, CI revalidation input |
| `forge/guard/secret_scan.py` | secret value 비노출 scanner |
| `forge/guard/ci_event.py` | GitHub event별 `pr_evidence|regression` route |
| `forge/guard/cli.py` | `prepare`, `run`, `verify`, `stop-hook`, `verify-ci`, `ci-route` |
| `forge/guard/__main__.py` | `python -m forge.guard` entrypoint |
| `forge/schemas/task-contract-v1.schema.json` | strict task contract schema |
| `forge/schemas/handoff-v1.schema.json` | strict handoff schema |
| `forge/schemas/receipt-v1.schema.json` | integer expiry와 phase policy를 가진 receipt schema |
| `forge/schemas/runner-state-v1.schema.json` | reservation/thread/token state schema |
| `forge/schemas/evidence-bundle-v1.schema.json` | PR comment transport bundle schema |
| `forge/hooks/codex-hooks.template.json` | installer가 두 exact sentinel을 치환하는 Stop hook template |
| `forge/hooks/codex-stop-gate.sh` | trusted Python CLI를 exec하는 POSIX compatibility shim |
| `forge/scripts/__init__.py` | import 가능한 release build module package marker |
| `forge/scripts/build_guard_release.py` | deterministic archive library |
| `forge/scripts/install-codex-hook.py` | trusted manifest 검증과 `.codex/hooks.json` atomic install |
| `forge/scripts/build-guard-release.py` | deterministic self-contained zipapp와 manifest build |
| `.github/workflows/capability-eval.yml` | Windows·Ubuntu named checks와 event routing |
| `tests/guard/conftest.py` | immutable contract/handoff builders |
| `tests/guard/test_contract.py` | schema와 acceptance partition tests |
| `tests/guard/test_git_state.py` | multi-repo baseline/diff tests |
| `tests/guard/test_references_commands.py` | GitHub failure와 command evidence tests |
| `tests/guard/test_verifier.py` | phase receipt와 full pipeline tests |
| `tests/guard/test_runner.py` | reservation, respawn, GATE_ERROR, post-exit tests |
| `tests/guard/test_stop_hook.py` | hook payload/response tests |
| `tests/guard/test_release.py` | deterministic release와 install tests |
| `tests/guard/test_secret_scan.py` | value non-disclosure tests |
| `tests/guard/test_evidence_bundle.py` | multi-repo comment upsert tests |
| `tests/guard/test_ci_event.py` | PR full evidence와 non-PR regression routing tests |
| `tests/integration/test_ci_evidence.py` | queue-safe evidence revalidation tests |
| `tests/integration/test_codex_stop_hook.py` | opt-in live Codex same-thread smoke |
| `tests/test_workflow_contract.py` | triggers, matrix names, conditional steps contract |

## 고정 public 타입

```python
class FailureKind(str, Enum):
    PASS = "PASS"
    TESTS_FAILED = "TESTS_FAILED"
    GATE_ERROR = "GATE_ERROR"

class ReceiptPhase(str, Enum):
    STOP = "stop"
    POST_EXIT = "post-exit"
    CI = "ci"
    HERMES = "hermes"

@dataclass(frozen=True)
class Budget:
    max_sessions: int
    max_runtime_s: int
    max_tokens: int
    command_timeout_s: int

@dataclass(frozen=True)
class RepositoryContract:
    name: str
    path: Path
    remote: str
    branch: str
    baseline_sha: str

@dataclass(frozen=True)
class AcceptanceCriterion:
    acceptance_id: str
    text_hash: str

@dataclass(frozen=True)
class CommandSpec:
    command_id: str
    repository: str
    argv: Sequence[str]
    timeout_s: int

@dataclass(frozen=True)
class TaskContract:
    schema_version: str
    task_id: str
    run_id: int
    source_issue_repo: str
    source_issue_number: int
    source_body_hash: str
    acceptance_criteria: Sequence[AcceptanceCriterion]
    repositories: Sequence[RepositoryContract]
    commands: Sequence[CommandSpec]
    budget: Budget
    completion_policy: str
    verifier_sha: str

@dataclass(frozen=True)
class PullRequestClaim:
    repository: str
    url: str
    head_sha: str

@dataclass(frozen=True)
class ImplementedCriterion:
    acceptance_id: str
    summary: str

@dataclass(frozen=True)
class ResidualMaterialization:
    kind: Literal["issue", "adr"]
    repository: str
    number: int

@dataclass(frozen=True)
class ResidualCriterion:
    acceptance_id: str
    reason: str
    materialization: ResidualMaterialization

@dataclass(frozen=True)
class VerificationClaim:
    acceptance_id: str
    command_id: str
    evidence_path: str

@dataclass(frozen=True)
class ChangedFile:
    repository: str
    path: str

@dataclass(frozen=True)
class Handoff:
    schema_version: str
    task_id: str
    run_id: int
    pull_requests: Sequence[PullRequestClaim]
    changed_files: Sequence[ChangedFile]
    implemented: Sequence[ImplementedCriterion]
    not_implemented: Sequence[ResidualCriterion]
    verified_by: Sequence[VerificationClaim]

@dataclass(frozen=True)
class SessionUsage:
    thread_ids: Sequence[str]
    total_input_tokens: int
    total_output_tokens: int
    runtime_s: int

@dataclass(frozen=True)
class VerificationContext:
    phase: ReceiptPhase
    deployed_sha: str
    state_dir: Path
    session_usage: SessionUsage
    now_s: int

@dataclass(frozen=True)
class VerifiedEvidence:
    task_id: str
    run_id: int
    contract_digest: str
    handoff_digest: str
    repository_digest: str
    command_digest: str
    reference_digest: str
    session_digest: str
    deployed_sha: str

@dataclass(frozen=True)
class Receipt:
    schema_version: str
    phase: ReceiptPhase
    issued_at_s: int
    expires_at_s: int
    task_id: str
    run_id: int
    contract_digest: str
    handoff_digest: str
    repository_digest: str
    command_digest: str
    reference_digest: str
    session_digest: str
    deployed_sha: str

@dataclass(frozen=True)
class VerificationResult:
    kind: FailureKind
    code: str
    message: str
    receipt: Receipt | None
```

## Task 1: strict contract와 schema를 만든다

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

**Consumes:** 승인 spec의 TaskContract/Handoff/Receipt 필드, `FailureKind`, budget exact values.

**Produces:** 뒤 Task가 import하는 frozen types와 strict parser. Schema는 `additionalProperties:false`이며 public parser만 schema validation exception을 typed guard failure로 변환한다.

**Interfaces:**

```text
parse_task_contract(data: Mapping[str, object]) -> TaskContract
parse_handoff(data: Mapping[str, object], contract: TaskContract) -> Handoff
validate_acceptance_partition(expected: Sequence[str], implemented: Sequence[str], residual: Sequence[str]) -> None
canonical_json_bytes(value: object) -> bytes
sha256_json(value: object) -> str
load_schema(name: Literal["task-contract-v1", "handoff-v1", "receipt-v1", "runner-state-v1"]) -> Mapping[str, object]
```

- [ ] **Step 1: package metadata와 ignore를 추가한다.**

`pyproject.toml`은 `requires-python = ">=3.11"`, runtime `jsonschema>=4.23,<5`, test extra `pytest>=8,<9`, console script `forge-guard = "forge.guard.cli:main"`을 선언한다. `.gitignore`에는 `.venv/`, `.codex/*`, `.pytest_cache/`를 추가한다.

- [ ] **Step 2: acceptance partition RED test를 작성한다.**

```python
import pytest

from forge.guard.contract import validate_acceptance_partition
from forge.guard.errors import TestsFailed


def test_acceptance_partition_rejects_overlap_and_missing_ids() -> None:
    with pytest.raises(TestsFailed) as overlap:
        validate_acceptance_partition(
            expected=("ac-build", "ac-test"),
            implemented=("ac-build",),
            residual=("ac-build",),
        )
    assert overlap.value.code == "AC_PARTITION_OVERLAP"

    with pytest.raises(TestsFailed) as missing:
        validate_acceptance_partition(
            expected=("ac-build", "ac-test"),
            implemented=("ac-build",),
            residual=(),
        )
    assert missing.value.code == "AC_PARTITION_MISMATCH"
```

```python
from copy import deepcopy

import pytest

from forge.guard.contract import parse_handoff, parse_task_contract
from forge.guard.errors import GateError, TestsFailed


def test_contract_and_handoff_schema_errors_have_distinct_classification(
    valid_contract_data, valid_handoff_data
) -> None:
    bad_contract = deepcopy(valid_contract_data)
    bad_contract["budget"]["max_sessions"] = "4"
    with pytest.raises(GateError) as contract_error:
        parse_task_contract(bad_contract)
    assert contract_error.value.code == "TASK_CONTRACT_SCHEMA"

    contract = parse_task_contract(valid_contract_data)
    bad_handoff = deepcopy(valid_handoff_data)
    bad_handoff["not_implemented"] = "none"
    with pytest.raises(TestsFailed) as handoff_error:
        parse_handoff(bad_handoff, contract)
    assert handoff_error.value.code == "HANDOFF_SCHEMA"
```

- [ ] **Step 3: RED를 실행한다.**

Run:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.lock
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
.\.venv\Scripts\python.exe -m pytest tests/guard/test_contract.py -q
```

Expected: `ModuleNotFoundError: No module named 'forge.guard.contract'`로 FAIL.

- [ ] **Step 4: typed error와 partition 최소 GREEN을 구현한다.**

```python
from collections.abc import Sequence


class GuardFailure(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TestsFailed(GuardFailure):
    pass


class GateError(GuardFailure):
    pass


def validate_acceptance_partition(
    expected: Sequence[str],
    implemented: Sequence[str],
    residual: Sequence[str],
) -> None:
    expected_set = set(expected)
    implemented_set = set(implemented)
    residual_set = set(residual)
    overlap = implemented_set & residual_set
    if overlap:
        names = ",".join(sorted(overlap))
        raise TestsFailed("AC_PARTITION_OVERLAP", names)
    actual = implemented_set | residual_set
    if actual != expected_set:
        missing = ",".join(sorted(expected_set - actual))
        extra = ",".join(sorted(actual - expected_set))
        raise TestsFailed("AC_PARTITION_MISMATCH", f"missing={missing};extra={extra}")
```

- [ ] **Step 5: canonical JSON과 strict schema를 추가한다.**

`canonical_json_bytes`는 `json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")`만 사용한다. 네 schema의 `$id`는 `https://infinity-forge.local/schemas/task-contract-v1.schema.json`, `https://infinity-forge.local/schemas/handoff-v1.schema.json`, `https://infinity-forge.local/schemas/receipt-v1.schema.json`, `https://infinity-forge.local/schemas/runner-state-v1.schema.json`으로 고정하고, object node마다 `additionalProperties:false`를 둔다. `parse_task_contract`의 `ValidationError`는 `GateError("TASK_CONTRACT_SCHEMA")`, `parse_handoff`의 `ValidationError`는 `TestsFailed("HANDOFF_SCHEMA")`로 변환한다. receipt `expires_at_s` type은 `integer`이고 phase enum은 네 값만 허용한다.

- [ ] **Step 6: GREEN과 guard regression을 실행한다.**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_contract.py -q
.\.venv\Scripts\python.exe -m pytest tests/guard -q
git diff --check
```

Expected: contract tests PASS, guard tests PASS, `git diff --check` exit 0.

- [ ] **Step 7: commit한다.**

```powershell
git add .gitignore pyproject.toml requirements.lock forge/__init__.py forge/guard/__init__.py forge/guard/errors.py forge/guard/contract.py forge/schemas/task-contract-v1.schema.json forge/schemas/handoff-v1.schema.json forge/schemas/receipt-v1.schema.json forge/schemas/runner-state-v1.schema.json tests/guard/conftest.py tests/guard/test_contract.py
git commit -m "feat: define Forge guard contracts"
```

## Task 2: clean committed multi-repository evidence를 만든다

**Files:**
- Create: `forge/guard/git_state.py`
- Create: `forge/guard/repo_capability.py`
- Create: `tests/guard/test_git_state.py`
- Create: `tests/guard/test_repo_capability.py`
- Modify: `forge/guard/contract.py`

**Consumes:** `RepositoryContract`, `ChangedFile`, `TestsFailed`, fixed state root exclusion rules.

**Produces:** repository별 baseline/head/tree/changed-file evidence. 모든 repository가 clean이고 handoff claim과 exact set-equal일 때만 반환한다.

**Interfaces:**

```text
capture_baseline(path: Path) -> RepositoryBaseline
inspect_repository(contract: RepositoryContract, require_clean: bool = True) -> RepositoryState
parse_name_status_z(payload: bytes) -> Sequence[ChangedFileStatus]
validate_changed_files(states: Sequence[RepositoryState], claims: Sequence[ChangedFile]) -> None
repository_digest(states: Sequence[RepositoryState]) -> str
verify_guard_ci_onboarding(repositories: Sequence[RepositoryContract], probe: RepoCiProbe) -> None
```

- [ ] **Step 1: multi-repository set equality RED test를 작성한다.**

```python
import pytest

from forge.guard.contract import ChangedFile
from forge.guard.errors import TestsFailed
from forge.guard.git_state import RepositoryState, validate_changed_files


def test_multi_repo_changed_files_must_be_exactly_equal() -> None:
    states = (
        RepositoryState(
            name="api",
            baseline_sha="a" * 40,
            head_sha="b" * 40,
            tree_sha="c" * 40,
            branch="codex/guard",
            remote="https://github.com/example/api.git",
            clean=True,
            changed_paths=("src/api.py",),
        ),
        RepositoryState(
            name="web",
            baseline_sha="d" * 40,
            head_sha="e" * 40,
            tree_sha="f" * 40,
            branch="codex/guard",
            remote="https://github.com/example/web.git",
            clean=True,
            changed_paths=("src/web.ts",),
        ),
    )
    claims = (
        ChangedFile(repository="api", path="src/api.py"),
        ChangedFile(repository="web", path="src/web.ts"),
    )
    validate_changed_files(states, claims)

    with pytest.raises(TestsFailed) as error:
        validate_changed_files(states, claims[:1])
    assert error.value.code == "CHANGED_FILES_MISMATCH"
```

- [ ] **Step 1a: guard workflow가 없는 secondary repo를 prepare 전에 거절하는 RED test를 작성한다.**

```python
import pytest

from forge.guard.errors import GateError
from forge.guard.repo_capability import verify_guard_ci_onboarding


class FakeProbe:
    def __init__(self) -> None:
        self.workflows = {"example/api": True, "example/web": False}

    def has_guard_workflow(self, repository: str) -> bool:
        return self.workflows[repository]

    def stable_check_names(self, repository: str) -> frozenset[str]:
        if repository == "example/api":
            return frozenset(
                {
                    "guard-contract (ubuntu-latest)",
                    "guard-contract (windows-latest)",
                }
            )
        return frozenset()


def test_prepare_rejects_a_repository_without_guard_ci(
    multi_repo_contract,
) -> None:
    with pytest.raises(GateError) as error:
        verify_guard_ci_onboarding(multi_repo_contract.repositories, FakeProbe())
    assert error.value.code == "REPO_GUARD_CI_NOT_ONBOARDED"
    assert "example/web" in error.value.message
```

- [ ] **Step 2: RED를 실행한다.**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_git_state.py tests/guard/test_repo_capability.py -q
```

Expected: `ModuleNotFoundError: No module named 'forge.guard.git_state'`로 FAIL.

- [ ] **Step 3: exact set 비교 최소 GREEN을 구현한다.**

```python
from collections.abc import Sequence
from dataclasses import dataclass

from forge.guard.contract import ChangedFile
from forge.guard.errors import TestsFailed


@dataclass(frozen=True)
class RepositoryState:
    name: str
    baseline_sha: str
    head_sha: str
    tree_sha: str
    branch: str
    remote: str
    clean: bool
    changed_paths: Sequence[str]


def validate_changed_files(
    states: Sequence[RepositoryState], claims: Sequence[ChangedFile]
) -> None:
    actual = {
        (state.name, path)
        for state in states
        for path in state.changed_paths
    }
    claimed = {(item.repository, item.path) for item in claims}
    if actual != claimed:
        missing = sorted(actual - claimed)
        extra = sorted(claimed - actual)
        raise TestsFailed(
            "CHANGED_FILES_MISMATCH",
            f"missing={missing};extra={extra}",
        )
```

- [ ] **Step 4: Git argv와 baseline 검사를 구현한다.**

`capture_baseline`은 `git status --porcelain=v1 -z`, `git rev-parse HEAD`, `git rev-parse --abbrev-ref HEAD`, `git remote get-url origin`을 `shell=False`로 호출한다. `inspect_repository`는 `git merge-base --is-ancestor`, `git rev-parse HEAD^{tree}`, `git diff --name-status -z baseline..HEAD`, `git status --porcelain=v1 -z`를 실행한다. dirty tree, empty implementation diff, handoff/evidence-only diff, baseline 비조상, remote/branch drift, absolute path, `..`, symlink/junction escape는 `TestsFailed`다.

- [ ] **Step 4a: repository CI onboarding hard prerequisite를 구현한다.**

`verify_guard_ci_onboarding`은 각 repository default branch의 `.github/workflows/capability-eval.yml` 존재와 최근 probe commit의 두 stable check name을 GitHub API adapter로 확인한다. workflow/check 하나라도 없으면 baseline capture, card unblock, Codex spawn 전에 `GateError("REPO_GUARD_CI_NOT_ONBOARDED", repository)`를 낸다. onboarding은 별도 repository PR로 먼저 수행하며 현재 task가 secondary repo workflow를 몰래 추가하지 않는다.

```python
from collections.abc import Sequence

from forge.guard.contract import RepositoryContract
from forge.guard.errors import GateError


_REQUIRED_CHECKS = frozenset(
    {
        "guard-contract (ubuntu-latest)",
        "guard-contract (windows-latest)",
    }
)


def verify_guard_ci_onboarding(
    repositories: Sequence[RepositoryContract], probe: RepoCiProbe
) -> None:
    for repository in repositories:
        repo = repository.github_repository
        checks = probe.stable_check_names(repo)
        if not probe.has_guard_workflow(repo) or not _REQUIRED_CHECKS <= checks:
            raise GateError("REPO_GUARD_CI_NOT_ONBOARDED", repo)
```

- [ ] **Step 5: GREEN과 regression을 실행한다.**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_git_state.py tests/guard/test_repo_capability.py -q
.\.venv\Scripts\python.exe -m pytest tests/guard -q
git diff --check
```

Expected: multi-repository tests PASS, guard tests PASS, diff check exit 0.

- [ ] **Step 6: commit한다.**

```powershell
git add forge/guard/contract.py forge/guard/git_state.py forge/guard/repo_capability.py tests/guard/test_git_state.py tests/guard/test_repo_capability.py
git commit -m "feat: bind guard evidence to repository heads"
```

## Task 3: GitHub reference와 command evidence를 fail-loud로 만든다

**Files:**
- Create: `forge/guard/references.py`
- Create: `forge/guard/commands.py`
- Create: `tests/guard/test_references_commands.py`

**Consumes:** `TaskContract`, `Handoff`, repository paths, `TestsFailed`, `GateError`.

**Produces:** paginated issue/ADR/PR evidence와 scrubbed argv command evidence. API나 subprocess 오류를 빈 evidence로 바꾸지 않는다.

**Interfaces:**

```text
GhCliClient.get_issue(repo: str, number: int) -> IssueRecord
GhCliClient.get_pull(repo: str, number: int) -> PullRecord
GhCliClient.list_issue_comments(repo: str, number: int) -> Sequence[CommentRecord]
classify_gh_failure(status: int, stderr: str) -> NoReturn
verify_references(contract: TaskContract, handoff: Handoff, gh: GhCliClient) -> ReferenceEvidence
build_child_env(source: Mapping[str, str], allowed_names: AbstractSet[str]) -> dict[str, str]
run_verification_commands(contract: TaskContract, state_dir: Path) -> Sequence[CommandEvidence]
```

- [ ] **Step 1: HTTP classification과 environment scrub RED tests를 작성한다.**

```python
import pytest

from forge.guard.commands import build_child_env
from forge.guard.errors import GateError, TestsFailed
from forge.guard.references import classify_gh_failure


def test_github_status_and_child_environment_fail_loudly() -> None:
    with pytest.raises(TestsFailed) as missing:
        classify_gh_failure(404, "not found")
    assert missing.value.code == "GITHUB_REFERENCE_NOT_FOUND"

    with pytest.raises(GateError) as throttled:
        classify_gh_failure(429, "rate limit")
    assert throttled.value.code == "GITHUB_RATE_LIMIT"

    child = build_child_env(
        source={
            "PATH": "C:\\Windows\\System32",
            "LANG": "ko_KR.UTF-8",
            "GITHUB_TOKEN": "fake-secret-value",
        },
        allowed_names=frozenset({"PATH", "LANG"}),
    )
    assert child == {
        "LANG": "ko_KR.UTF-8",
        "PATH": "C:\\Windows\\System32",
    }
```

- [ ] **Step 2: RED를 실행한다.**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_references_commands.py::test_github_status_and_child_environment_fail_loudly -q
```

Expected: `ModuleNotFoundError: No module named 'forge.guard.commands'`로 FAIL.

- [ ] **Step 3: failure mapping과 environment scrub 최소 GREEN을 구현한다.**

```python
from collections.abc import AbstractSet, Mapping
from typing import NoReturn

from forge.guard.errors import GateError, TestsFailed


def classify_gh_failure(status: int, stderr: str) -> NoReturn:
    if status == 404:
        raise TestsFailed("GITHUB_REFERENCE_NOT_FOUND", "referenced object does not exist")
    if status == 429:
        raise GateError("GITHUB_RATE_LIMIT", "GitHub API rate limited the verifier")
    if status in {401, 403}:
        raise GateError("GITHUB_AUTH", "GitHub authentication or authorization failed")
    if status >= 500:
        raise GateError("GITHUB_SERVER", f"GitHub API returned HTTP {status}")
    raise GateError("GITHUB_CLI", f"GitHub CLI failed with HTTP {status}: {stderr[:200]}")


def build_child_env(
    source: Mapping[str, str], allowed_names: AbstractSet[str]
) -> dict[str, str]:
    return {
        name: source[name]
        for name in sorted(allowed_names)
        if name in source
    }
```

- [ ] **Step 4: paginated GitHub client를 구현한다.**

모든 `gh api` call은 `--paginate --slurp`와 명시 repo path를 사용한다. source issue body hash와 AC text hash를 재계산한다. residual issue는 open, ADR은 open과 `forge:adr`, PR은 open·non-draft·expected repository·expected head SHA를 요구한다. invalid JSON, page 누락, stderr status 미분류는 `GateError`다.

- [ ] **Step 5: command evidence를 구현한다.**

command runner는 `subprocess.run(list(spec.argv), cwd=repo.path, env=scrubbed, shell=False, timeout=spec.timeout_s, check=False, capture_output=True)`를 사용한다. nonzero exit는 `TestsFailed("COMMAND_FAILED", command_id)`, missing binary와 timeout은 `GateError`다. trusted state에는 command ID, argv digest, exit code, stdout/stderr digest, redacted 2,000자 tail만 atomic write한다.

- [ ] **Step 6: GREEN과 regression을 실행한다.**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_references_commands.py -q
.\.venv\Scripts\python.exe -m pytest tests/guard -q
git diff --check
```

Expected: reference/command tests PASS, guard tests PASS, diff check exit 0.

- [ ] **Step 7: commit한다.**

```powershell
git add forge/guard/references.py forge/guard/commands.py tests/guard/test_references_commands.py
git commit -m "feat: verify GitHub and command evidence"
```

## Task 4: phase별 receipt와 공통 verifier를 구현한다

**Files:**
- Create: `forge/guard/verifier.py`
- Create: `forge/schemas/hermes-completion-result-v1.schema.json`
- Create: `tests/guard/test_verifier.py`
- Modify: `forge/guard/contract.py`
- Modify: `forge/schemas/receipt-v1.schema.json`

**Consumes:** Task 1~3의 typed contract, repository, reference, command evidence.

**Produces:** `stop=3,900초`, `post-exit=86,400초`, `ci=7,200초`, `hermes=900초` phase receipt와 single-use eligibility 판정.

**Interfaces:**

```text
phase_expiry(phase: ReceiptPhase, issued_at_s: int) -> int
issue_receipt(phase: ReceiptPhase, issued_at_s: int, evidence: VerifiedEvidence) -> Receipt
receipt_is_consumable(receipt: Receipt, now_s: int) -> bool
verify_receipt_bindings(receipt: Receipt, evidence: VerifiedEvidence, now_s: int) -> None
verify(contract: TaskContract, handoff: Handoff, context: VerificationContext) -> VerificationResult
render_hermes_result(request: Mapping[str, object], receipt: Receipt, verifier_sha256: str) -> Mapping[str, object]
render_hermes_denial(request: Mapping[str, object], result: VerificationResult) -> Mapping[str, object]
```

- [ ] **Step 1: queue-safe phase policy RED test를 작성한다.**

```python
from forge.guard.contract import Receipt, ReceiptPhase
from forge.guard.verifier import phase_expiry, receipt_is_consumable


def _receipt(phase: ReceiptPhase, issued_at_s: int) -> Receipt:
    return Receipt(
        schema_version="forge-receipt/v1",
        phase=phase,
        issued_at_s=issued_at_s,
        expires_at_s=phase_expiry(phase, issued_at_s),
        task_id="t_guard_core",
        run_id=7,
        contract_digest="a" * 64,
        handoff_digest="b" * 64,
        repository_digest="c" * 64,
        command_digest="d" * 64,
        reference_digest="e" * 64,
        session_digest="f" * 64,
        deployed_sha="1" * 40,
    )


def test_phase_expiry_matches_approved_ttl_and_hermes_is_single_use() -> None:
    issued = 1_700_000_000
    assert phase_expiry(ReceiptPhase.STOP, issued) == issued + 3_900
    assert phase_expiry(ReceiptPhase.POST_EXIT, issued) == issued + 86_400
    assert phase_expiry(ReceiptPhase.CI, issued) == issued + 7_200
    assert phase_expiry(ReceiptPhase.HERMES, issued) == issued + 900

    assert not receipt_is_consumable(_receipt(ReceiptPhase.POST_EXIT, issued), issued + 86_400)
    assert receipt_is_consumable(_receipt(ReceiptPhase.HERMES, issued), issued + 899)
    assert not receipt_is_consumable(_receipt(ReceiptPhase.HERMES, issued), issued + 900)
```

- [ ] **Step 1a: Hermes consumer response adapter RED test를 작성한다.**

```python
from forge.guard.contract import ReceiptPhase, sha256_json
from forge.guard.errors import FailureKind, VerificationResult
from forge.guard.verifier import render_hermes_denial, render_hermes_result


def test_hermes_result_matches_core_consumer_contract() -> None:
    issued = 1_700_000_000
    receipt = _receipt(ReceiptPhase.HERMES, issued)
    request = {
        "schema_version": "forge-completion-request/v1",
        "phase": "hermes",
        "policy": "forge-v1",
        "task_id": receipt.task_id,
        "run_id": receipt.run_id,
        "board": "default",
        "workspace_path": "C:/work/repo",
    }
    verifier_sha256 = "9" * 64
    result = render_hermes_result(request, receipt, verifier_sha256)

    assert result["schema_version"] == "forge-completion-result/v1"
    assert result["decision"] == "allow"
    assert result["classification"] == "PASS"
    assert result["receipt_version"] == "forge-receipt/v1"
    assert result["repository_state_digest"] == receipt.repository_digest
    assert result["verifier_sha256"] == verifier_sha256
    digest_input = dict(result)
    digest = digest_input.pop("receipt_digest")
    assert digest == sha256_json(digest_input)


def test_hermes_denial_preserves_typed_classification() -> None:
    request = {
        "phase": "hermes",
        "policy": "forge-v1",
        "task_id": "t_guard_core",
        "run_id": 7,
    }
    failure = VerificationResult(
        kind=FailureKind.TESTS_FAILED,
        code="HANDOFF_INCOMPLETE",
        message="acceptance criterion ac-test is missing",
        receipt=None,
    )
    result = render_hermes_denial(request, failure)
    assert result == {
        "schema_version": "forge-completion-result/v1",
        "phase": "hermes",
        "decision": "deny",
        "classification": "TESTS_FAILED",
        "policy": "forge-v1",
        "task_id": "t_guard_core",
        "run_id": 7,
        "reason": "HANDOFF_INCOMPLETE: acceptance criterion ac-test is missing",
    }
```

- [ ] **Step 2: RED를 실행한다.**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_verifier.py -q
```

Expected: `ModuleNotFoundError: No module named 'forge.guard.verifier'`로 FAIL.

- [ ] **Step 3: phase policy 최소 GREEN을 구현한다.**

```python
from forge.guard.contract import Receipt, ReceiptPhase


_PHASE_TTL_SECONDS: dict[ReceiptPhase, int] = {
    ReceiptPhase.STOP: 3_900,
    ReceiptPhase.POST_EXIT: 86_400,
    ReceiptPhase.CI: 7_200,
    ReceiptPhase.HERMES: 900,
}


def phase_expiry(phase: ReceiptPhase, issued_at_s: int) -> int:
    return issued_at_s + _PHASE_TTL_SECONDS[phase]


def receipt_is_consumable(receipt: Receipt, now_s: int) -> bool:
    if receipt.phase is not ReceiptPhase.HERMES:
        return False
    return now_s < receipt.expires_at_s
```

- [ ] **Step 3a: Hermes result adapter 최소 GREEN을 구현한다.**

```python
from collections.abc import Mapping

from forge.guard.contract import Receipt, sha256_json
from forge.guard.errors import FailureKind, VerificationResult


def render_hermes_result(
    request: Mapping[str, object], receipt: Receipt, verifier_sha256: str
) -> Mapping[str, object]:
    body: dict[str, object] = {
        "schema_version": "forge-completion-result/v1",
        "phase": "hermes",
        "decision": "allow",
        "classification": "PASS",
        "policy": request["policy"],
        "task_id": receipt.task_id,
        "run_id": receipt.run_id,
        "receipt_version": "forge-receipt/v1",
        "contract_digest": receipt.contract_digest,
        "handoff_digest": receipt.handoff_digest,
        "repository_state_digest": receipt.repository_digest,
        "verifier_sha256": verifier_sha256,
        "issued_at": receipt.issued_at_s,
        "expires_at": receipt.expires_at_s,
    }
    body["receipt_digest"] = sha256_json(body)
    return body


def render_hermes_denial(
    request: Mapping[str, object], result: VerificationResult
) -> Mapping[str, object]:
    if result.kind is FailureKind.PASS:
        raise ValueError("PASS cannot be rendered as a denial")
    return {
        "schema_version": "forge-completion-result/v1",
        "phase": "hermes",
        "decision": "deny",
        "classification": result.kind.value,
        "policy": request["policy"],
        "task_id": request["task_id"],
        "run_id": request["run_id"],
        "reason": f"{result.code}: {result.message}",
    }
```

- [ ] **Step 4: binding과 pipeline을 구현한다.**

검증 순서를 contract→budget→handoff partition→git→GitHub references/PR→commands→session usage→receipt로 고정한다. non-consumable receipt도 task/run/contract/handoff/repository head/command/reference/session/deployed SHA digest가 한 필드라도 달라지면 거절한다. `ci` phase는 current check rollup을 조회하지 않는다. PASS만 receipt를 가지며 실패 result의 `receipt`는 `None`이다.

`forge-guard verify --phase hermes`는 stdin의 `forge-completion-request/v1`을 strict parse해 OS state root의 task/run/contract/handoff/runner state를 조회한다. PASS는 `render_hermes_result` JSON 한 개와 exit 0, TESTS_FAILED/GATE_ERROR는 `render_hermes_denial` JSON 한 개와 exit 2를 출력한다. `verifier_sha256`은 실행 중인 trusted zipapp bytes의 SHA-256이며 deployment manifest의 artifact hash와 같아야 한다. 이 CLI integration을 `tests/integration/test_ci_evidence.py`의 subprocess test로 고정한다.

- [ ] **Step 5: queue 지연 integration test를 추가한다.**

`tests/integration/test_ci_evidence.py`에서 stop은 3,899초 PASS/3,900초 stale, post-exit은 86,399초 PASS/86,400초 stale, CI는 7,199초 PASS/7,200초 stale, Hermes는 899초 consumable/900초 stale을 실제 body로 작성한다. 네 phase 모두 expiry 전이라도 repository head, source issue body hash, AC hash 중 한 값이 달라지면 즉시 `TESTS_FAILED`인지 확인한다.

- [ ] **Step 6: GREEN과 regression을 실행한다.**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_verifier.py tests/integration/test_ci_evidence.py -q
.\.venv\Scripts\python.exe -m pytest tests/guard tests/integration -q
git diff --check
```

Expected: verifier/integration tests PASS, 전체 guard/integration PASS, diff check exit 0.

- [ ] **Step 7: commit한다.**

```powershell
git add forge/guard/verifier.py forge/guard/contract.py forge/schemas/receipt-v1.schema.json forge/schemas/hermes-completion-result-v1.schema.json tests/guard/test_verifier.py tests/integration/test_ci_evidence.py
git commit -m "feat: issue phase-aware guard receipts"
```

## Task 5: persistent Codex reservation과 post-exit runner를 구현한다

**Files:**
- Create: `forge/guard/state.py`
- Create: `forge/guard/runner.py`
- Create: `tests/guard/test_runner.py`
- Modify: `forge/schemas/runner-state-v1.schema.json`

**Consumes:** budget, phase verifier, trusted state root, Codex JSONL `thread.started`와 `turn.completed.usage`.

**Produces:** crash-safe reservation ledger와 `spawn|resume|reverify|retry_exhausted|ready_for_ci` decision. post-exit verifier는 hook 실행 여부와 무관하게 항상 실행된다.

**Interfaces:**

```text
state_path(task_id: str) -> Path
atomic_write_json(path: Path, value: Mapping[str, object]) -> None
load_runner_state(task_id: str, run_id: int) -> RunnerState
preflight_launch(contract: TaskContract) -> None
reserve_launching_slot(state: RunnerState) -> tuple[RunnerState, int]
release_launching_slot(state: RunnerState, slot: int) -> RunnerState
mark_process_started_without_thread(state: RunnerState, slot: int) -> RunnerState
attach_thread_id(state: RunnerState, slot: int, thread_id: str) -> RunnerState
record_usage(state: RunnerState, slot: int, input_tokens: int, output_tokens: int) -> RunnerState
recover_gate_error(state: RunnerState, slot: int) -> RunnerDecision
run_task(contract: TaskContract) -> RunnerResult
```

- [ ] **Step 1: reservation/GATE_ERROR RED test를 작성한다.**

```python
from dataclasses import replace
from pathlib import Path

import pytest

from forge.guard.errors import GateError
from forge.guard.runner import (
    ReservationStatus,
    RunnerAction,
    RunnerState,
    mark_process_started_without_thread,
    recover_gate_error,
    release_launching_slot,
    reserve_launching_slot,
)


def test_gate_error_reuses_reserved_slot_without_allocating_a_new_one() -> None:
    state = RunnerState(
        task_id="t_guard_core",
        run_id=7,
        reservations=(),
        total_tokens=0,
        runtime_s=0,
    )
    state, slot = reserve_launching_slot(state)
    assert slot == 1
    assert len(state.reservations) == 1
    assert state.reservations[0].status is ReservationStatus.LAUNCHING

    state = mark_process_started_without_thread(state, slot)
    assert state.reservations[0].status is ReservationStatus.UNKNOWN_STARTED

    decision = recover_gate_error(state, slot)
    assert decision.action is RunnerAction.REVERIFY
    assert decision.state == state
    assert len(decision.state.reservations) == 1



def test_popen_error_releases_launching_slot() -> None:
    state = RunnerState("t_guard_core", 7, (), 0, 0)
    state, slot = reserve_launching_slot(state)
    released = release_launching_slot(state, slot)
    assert released.reservations == ()


def test_preflight_error_occurs_before_any_reservation(
    valid_contract, tmp_path: Path
) -> None:
    missing = replace(valid_contract.repositories[0], path=tmp_path / "missing")
    broken = replace(valid_contract, repositories=(missing,))
    state = RunnerState("t_guard_core", 7, (), 0, 0)
    with pytest.raises(GateError, match="repository path does not exist"):
        preflight_launch(broken)
    assert state.reservations == ()
```

- [ ] **Step 2: RED를 실행한다.**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_runner.py::test_gate_error_reuses_reserved_slot_without_allocating_a_new_one -q
```

Expected: `ModuleNotFoundError: No module named 'forge.guard.runner'`로 FAIL.

- [ ] **Step 3: reservation 최소 GREEN을 구현한다.**

```python
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from forge.guard.contract import TaskContract
from forge.guard.errors import GateError


def preflight_launch(contract: TaskContract) -> None:
    for repository in contract.repositories:
        if not repository.path.exists():
            raise GateError(
                "REPOSITORY_MISSING", "repository path does not exist"
            )


class RetryExhausted(RuntimeError):
    pass


class RunnerAction(str, Enum):
    REVERIFY = "reverify"


class ReservationStatus(str, Enum):
    LAUNCHING = "launching"
    UNKNOWN_STARTED = "unknown_started"
    STARTED = "started"


@dataclass(frozen=True)
class SessionReservation:
    slot: int
    status: ReservationStatus
    thread_id: str | None


@dataclass(frozen=True)
class RunnerState:
    task_id: str
    run_id: int
    reservations: Sequence[SessionReservation]
    total_tokens: int
    runtime_s: int


@dataclass(frozen=True)
class RunnerDecision:
    action: RunnerAction
    state: RunnerState
    slot: int


def reserve_launching_slot(state: RunnerState) -> tuple[RunnerState, int]:
    if len(state.reservations) >= 4:
        raise RetryExhausted("four Codex session reservations are already consumed")
    slot = len(state.reservations) + 1
    reservation = SessionReservation(slot, ReservationStatus.LAUNCHING, None)
    updated = RunnerState(
        task_id=state.task_id,
        run_id=state.run_id,
        reservations=tuple(state.reservations) + (reservation,),
        total_tokens=state.total_tokens,
        runtime_s=state.runtime_s,
    )
    return updated, slot


def release_launching_slot(state: RunnerState, slot: int) -> RunnerState:
    reservation = state.reservations[slot - 1]
    if reservation.status is not ReservationStatus.LAUNCHING:
        raise ValueError("only a launching reservation can be released")
    kept = tuple(item for item in state.reservations if item.slot != slot)
    return RunnerState(state.task_id, state.run_id, kept, state.total_tokens, state.runtime_s)


def mark_process_started_without_thread(state: RunnerState, slot: int) -> RunnerState:
    items = list(state.reservations)
    current = items[slot - 1]
    items[slot - 1] = SessionReservation(
        current.slot, ReservationStatus.UNKNOWN_STARTED, None
    )
    return RunnerState(
        state.task_id, state.run_id, tuple(items), state.total_tokens, state.runtime_s
    )


def recover_gate_error(state: RunnerState, slot: int) -> RunnerDecision:
    if slot < 1 or slot > len(state.reservations):
        raise ValueError("slot is not reserved")
    return RunnerDecision(action=RunnerAction.REVERIFY, state=state, slot=slot)
```

- [ ] **Step 4: atomic state writer를 구현한다.**

POSIX는 same-directory temp→file flush→`os.fsync(file)`→`os.replace`→directory fsync를 사용한다. Windows는 same-directory temp→flush→`os.fsync(file)`→`os.replace` 뒤 current-user-only ACL을 재검증한다. lock은 POSIX `fcntl.flock`, Windows `msvcrt.locking` adapter로 나누며 lock timeout은 `GATE_ERROR`다.

- [ ] **Step 5: Codex JSONL과 post-exit state machine을 구현한다.**

immutable contract, repo path, Codex binary, hook manifest를 `preflight_launch()`에서 먼저 검증한다. 그 뒤 `LAUNCHING` reservation을 atomic write한다. `subprocess.Popen` 자체가 `OSError`를 내면 해당 launching slot을 해제한다. Popen이 process handle을 반환하면 즉시 `UNKNOWN_STARTED`로 바꾸며, 이후 `thread.started.thread_id`를 같은 slot에 결합해 `STARTED`로 만든다. `turn.completed.usage.input_tokens/output_tokens`를 누적한다. usage missing/invalid는 `GATE_ERROR`지만 기존 slot은 유지한다. process가 끝나면 hook 결과와 관계없이 phase `POST_EXIT`로 `verify`를 호출한다. `TESTS_FAILED`만 새 slot을 허용하고, `GATE_ERROR`는 current slot reverify 또는 known thread resume만 허용한다.

- [ ] **Step 6: GREEN과 regression을 실행한다.**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_runner.py -q
.\.venv\Scripts\python.exe -m pytest tests/guard -q
git diff --check
```

Expected: runner tests PASS, guard tests PASS, diff check exit 0.

- [ ] **Step 7: commit한다.**

```powershell
git add forge/guard/state.py forge/guard/runner.py forge/schemas/runner-state-v1.schema.json tests/guard/test_runner.py
git commit -m "feat: persist Codex guard sessions"
```

## Task 6: Codex Stop hook adapter와 CLI를 구현한다

**Files:**
- Create: `forge/guard/stop_hook.py`
- Create: `forge/guard/cli.py`
- Create: `forge/guard/__main__.py`
- Create: `forge/hooks/codex-hooks.template.json`
- Create: `tests/guard/test_stop_hook.py`
- Create: `tests/integration/test_codex_stop_hook.py`
- Modify: `forge/hooks/codex-stop-gate.sh`

**Consumes:** Task 4 verifier, Task 5 state, Codex Stop payload fields `session_id`, `turn_id`, `cwd`, `stop_hook_active`, `last_assistant_message`.

**Produces:** stdout JSON 한 문서와 exit code 0인 Stop command. live smoke는 `FORGE_LIVE_CODEX_TEST=1`일 때만 실행하고 default CI에는 외부 Codex 호출을 넣지 않는다.

**Interfaces:**

```text
parse_stop_payload(data: Mapping[str, object]) -> StopPayload
render_stop_response(result: VerificationResult, stop_hook_active: bool) -> Mapping[str, object]
handle_stop(stdin: TextIO, stdout: TextIO, environ: Mapping[str, str]) -> int
main(argv: Sequence[str] | None = None) -> int
```

- [ ] **Step 1: hook response RED test를 작성한다.**

```python
from forge.guard.contract import FailureKind, VerificationResult
from forge.guard.stop_hook import render_stop_response


def test_stop_hook_blocks_tests_and_stops_gate_errors() -> None:
    tests_failed = VerificationResult(
        kind=FailureKind.TESTS_FAILED,
        code="COMMAND_FAILED",
        message="pytest failed",
        receipt=None,
    )
    assert render_stop_response(tests_failed, stop_hook_active=False) == {
        "decision": "block",
        "reason": "TESTS_FAILED: COMMAND_FAILED: pytest failed",
    }

    gate_error = VerificationResult(
        kind=FailureKind.GATE_ERROR,
        code="GITHUB_RATE_LIMIT",
        message="GitHub API unavailable",
        receipt=None,
    )
    assert render_stop_response(gate_error, stop_hook_active=False) == {
        "continue": False,
        "stopReason": "GATE_ERROR: GITHUB_RATE_LIMIT: GitHub API unavailable",
    }
    assert render_stop_response(tests_failed, stop_hook_active=True) == {
        "continue": False,
        "stopReason": "GATE_ERROR: STOP_HOOK_RECURSION: recursive Stop hook blocked",
    }
```

- [ ] **Step 2: RED를 실행한다.**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_stop_hook.py::test_stop_hook_blocks_tests_and_stops_gate_errors -q
```

Expected: `ModuleNotFoundError: No module named 'forge.guard.stop_hook'`로 FAIL.

- [ ] **Step 3: response renderer 최소 GREEN을 구현한다.**

```python
from collections.abc import Mapping

from forge.guard.contract import FailureKind, VerificationResult


def render_stop_response(
    result: VerificationResult, stop_hook_active: bool
) -> Mapping[str, object]:
    if stop_hook_active:
        return {
            "continue": False,
            "stopReason": "GATE_ERROR: STOP_HOOK_RECURSION: recursive Stop hook blocked",
        }
    if result.kind is FailureKind.PASS:
        return {}
    if result.kind is FailureKind.TESTS_FAILED:
        return {
            "decision": "block",
            "reason": f"TESTS_FAILED: {result.code}: {result.message}",
        }
    return {
        "continue": False,
        "stopReason": f"GATE_ERROR: {result.code}: {result.message}",
    }
```

- [ ] **Step 4: payload parser와 CLI를 구현한다.**

payload는 다섯 필드의 type을 엄격 검사한다. `FORGE_TASK_ID`는 `^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`와 stored task/run/cwd를 다시 대조한다. `last_assistant_message` JSON을 handoff schema로 parse해 trusted state에 atomic write하고 `phase=stop` verifier를 호출한다. traceback과 logs는 stderr가 아니라 trusted log에 기록하며 stdout에는 `json.dumps(response, separators=(",", ":")) + "\n"` 하나만 쓴다.

- [ ] **Step 5: hook template과 shim을 작성한다.**

template은 exact sentinel `__FORGE_GUARD_POSIX_COMMAND__`, `__FORGE_GUARD_WINDOWS_COMMAND__` 두 개와 timeout `3660`만 가진다. installer는 sentinel을 trusted manifest의 absolute launcher command로 치환하고 unresolved sentinel이 남으면 실패한다. `codex-stop-gate.sh`는 independent validation을 삭제하고 trusted launcher를 `exec`한다.

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "__FORGE_GUARD_POSIX_COMMAND__",
            "commandWindows": "__FORGE_GUARD_WINDOWS_COMMAND__",
            "timeout": 3660
          }
        ]
      }
    ]
  }
}
```

- [ ] **Step 6: GREEN과 opt-in live test 분리를 확인한다.**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_stop_hook.py -q
.\.venv\Scripts\python.exe -m pytest tests/integration/test_codex_stop_hook.py -q
git diff --check
```

Expected: default run은 unit tests PASS, live test는 `FORGE_LIVE_CODEX_TEST` 미설정으로 SKIPPED, diff check exit 0.

- [ ] **Step 7: commit한다.**

```powershell
git add forge/guard/stop_hook.py forge/guard/cli.py forge/guard/__main__.py forge/hooks/codex-hooks.template.json forge/hooks/codex-stop-gate.sh tests/guard/test_stop_hook.py tests/integration/test_codex_stop_hook.py
git commit -m "feat: connect Codex Stop hook to guard verifier"
```

## Task 7: self-contained trusted release를 deterministic build한다

**Files:**
- Create: `forge/scripts/__init__.py`
- Create: `forge/scripts/build_guard_release.py`
- Create: `forge/scripts/build-guard-release.py`
- Create: `forge/scripts/install_codex_hook.py`
- Create: `forge/scripts/install-codex-hook.py`
- Create: `forge/schemas/build-manifest.schema.json`
- Create: `tests/guard/test_release.py`
- Modify: `requirements.lock`

**Consumes:** package source, schemas, hashed lock, hook template, exact 40자리 source SHA.

**Produces:** byte-reproducible `forge-guard.pyz`와 `GuardBuildComponent`, shared `build-manifest-v1` schema, atomic installed release와 generated `.codex/hooks.json`. 별도 competing release manifest는 만들지 않는다.

**Interfaces:**

```text
normalize_zip_entries(entries: Mapping[str, bytes], output: Path) -> None
build_guard_component(source_root: Path, source_sha: str, output_dir: Path) -> GuardBuildComponent
verify_release(release_dir: Path, build_manifest_path: Path) -> Mapping[str, object]
install_hook(repo_root: Path, release_dir: Path, manifest_path: Path, verify: bool = False) -> Path
install-codex-hook.py --release RELEASE --manifest BUILD_MANIFEST --repo REPOSITORY [--verify]
```

- [ ] **Step 1: deterministic archive RED test를 작성한다.**

```python
import hashlib

from forge.scripts.build_guard_release import normalize_zip_entries


def test_normalized_zip_entries_are_byte_reproducible(tmp_path) -> None:
    entries = {
        "__main__.py": b"print('guard')\n",
        "forge/schemas/receipt-v1.schema.json": b"{}\n",
    }
    first = tmp_path / "first.pyz"
    second = tmp_path / "second.pyz"
    normalize_zip_entries(entries, first)
    normalize_zip_entries(dict(reversed(tuple(entries.items()))), second)
    assert first.read_bytes() == second.read_bytes()
    assert hashlib.sha256(first.read_bytes()).hexdigest() == hashlib.sha256(
        second.read_bytes()
    ).hexdigest()


def test_hook_installer_parser_has_one_exact_cross_platform_contract() -> None:
    from forge.scripts.install_codex_hook import build_parser

    parsed = build_parser().parse_args(
        [
            "--release",
            "C:/trusted/release",
            "--manifest",
            "C:/trusted/build-manifest.json",
            "--repo",
            "C:/work/repository",
            "--verify",
        ]
    )
    assert parsed.release == "C:/trusted/release"
    assert parsed.manifest == "C:/trusted/build-manifest.json"
    assert parsed.repo == "C:/work/repository"
    assert parsed.verify is True
```

- [ ] **Step 2: RED를 실행한다.**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_release.py::test_normalized_zip_entries_are_byte_reproducible -q
```

Expected: `ModuleNotFoundError: No module named 'forge.scripts.build_guard_release'`로 FAIL. 실행용 script filename은 hyphen이므로 같은 로직을 import 가능한 `forge/scripts/build_guard_release.py`에 두고 `build-guard-release.py`가 thin entrypoint로 호출한다.

- [ ] **Step 3: deterministic zip 최소 GREEN을 구현한다.**

```python
from collections.abc import Mapping
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


def normalize_zip_entries(entries: Mapping[str, bytes], output: Path) -> None:
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(entries):
            info = ZipInfo(filename=name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, entries[name])
```

- [ ] **Step 4: hashed dependency staging을 구현한다.**

build script는 `output_dir / ".staging"` empty directory를 만들고 `python -m pip install --require-hashes --no-compile --target output_dir/.staging -r requirements.lock` 의미의 argv를 `shell=False`로 실행한다. package와 schema를 staging에 복사한 뒤 `.pyc`, cache, metadata timestamp를 제거하고 archive entry를 정규화한다. build 두 번의 SHA-256이 다르면 실패한다.

- [ ] **Step 5: manifest verify와 hook install을 구현한다.**

`forge/schemas/build-manifest.schema.json`은 final manifest의 exact nine fields `schema_version|source_sha|archive_sha256|guard_sha256|requirements_lock_sha256|python_requires|schema_hashes|hermes_patch_manifest_sha256|hermes_patch_sha256`를 고정한다. 이 Task의 builder는 source SHA, guard zipapp SHA, schema SHA map, lock SHA, Python floor를 `GuardBuildComponent`로 반환하고, ops build Task가 archive/Hermes hash를 결합해 유일한 `build-manifest-v1`을 쓴다. 별도 `manifest.json`은 금지한다. import 가능한 `install_codex_hook.py`가 parser와 설치 로직을 소유하고 hyphenated `install-codex-hook.py`는 thin entrypoint다. installer는 exact `--release RELEASE --manifest BUILD_MANIFEST --repo REPOSITORY [--verify]`만 받고 shared schema를 parse해 release path가 OS trusted root 아래인지, `source_sha`, `guard_sha256`, `requirements_lock_sha256`, `python_requires>=3.11`, schema hash가 모두 일치하는지 확인한다. `--verify`는 파일을 쓰지 않고 기존 `.codex/hooks.json`의 canonical bytes와 interpreter/zipapp hash를 재검증한다. install mode는 temp→fsync→replace로 설치하고 project trust가 없더라도 automation argv가 `--dangerously-bypass-hook-trust`를 사용한다는 경계를 문서화한다.

- [ ] **Step 6: GREEN과 regression을 실행한다.**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_release.py -q
.\.venv\Scripts\python.exe -m pytest tests/guard -q
git diff --check
```

Expected: release tests PASS, guard tests PASS, diff check exit 0.

- [ ] **Step 7: commit한다.**

```powershell
git add requirements.lock forge/schemas/build-manifest.schema.json forge/scripts/__init__.py forge/scripts/build_guard_release.py forge/scripts/build-guard-release.py forge/scripts/install_codex_hook.py forge/scripts/install-codex-hook.py tests/guard/test_release.py
git commit -m "feat: build deterministic trusted guard release"
```

## Task 8: matched value를 노출하지 않는 secret scanner를 구현한다

**Files:**
- Create: `forge/guard/secret_scan.py`
- Create: `tests/guard/test_secret_scan.py`
- Modify: `forge/guard/cli.py`

**Consumes:** tracked source paths, Git object bytes, bundle bytes, release manifest/file list, 실제 Slack request body, current secret environment의 non-empty byte values.

**Produces:** `SecretFinding(path, rule_id)`만 포함하는 result. raw secret, matched substring, line content는 result와 logs에 포함하지 않는다.

**Interfaces:**

```text
scan_bytes(path: str, payload: bytes, secret_values: Sequence[bytes]) -> Sequence[SecretFinding]
scan_paths(paths: Sequence[Path], secret_values: Sequence[bytes]) -> ScanResult
scan_git_objects(repository: Path, secret_values: Sequence[bytes]) -> ScanResult
scan_cli(argv: Sequence[str]) -> int
secret_scan_ci_main(argv: Sequence[str], secret_values: Sequence[bytes]) -> int
python -m forge.guard secret-scan --paths PATHS [--git-repository REPOSITORY] [repeatable --payload-file LABEL=FILE]
python -m forge.guard secret-scan-ci --event-path FILE --report-output FILE
```

- [ ] **Step 1: value non-disclosure RED test를 작성한다.**

```python
from pathlib import Path

import subprocess
import pytest

from forge.guard.secret_scan import scan_bytes, scan_git_objects, secret_scan_ci_main


def test_secret_scan_reports_rule_without_echoing_matched_value() -> None:
    fake_value = "gh" + "p_" + "A" * 36
    findings = scan_bytes(
        path="evidence/comment.json",
        payload=f'{{"token":"{fake_value}"}}'.encode("utf-8"),
        secret_values=(),
    )
    assert [(item.path, item.rule_id) for item in findings] == [
        ("evidence/comment.json", "known-token-full-value")
    ]
    rendered = repr(findings)
    assert fake_value not in rendered
    assert "A" * 36 not in rendered


def test_scanner_source_is_not_a_false_positive_and_embedded_secret_is_found() -> None:
    source = Path("forge/guard/secret_scan.py").read_bytes()
    assert scan_bytes("forge/guard/secret_scan.py", source, ()) == ()

    secret = ("runtime" + "-secret-" + "Z" * 24).encode("utf-8")
    payload = b'{"callback":"https://example.invalid/?token=' + secret + b'"}'
    findings = scan_bytes("artifact/payload.json", payload, (secret,))
    assert [(item.path, item.rule_id) for item in findings] == [
        ("artifact/payload.json", "configured-secret-value")
    ]


def test_git_object_scan_finds_deleted_secret_without_disclosing(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "guard@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Guard Test"],
        check=True,
    )
    fake_value = "gh" + "p_" + "B" * 36
    secret_file = repository / "deleted-secret.txt"
    secret_file.write_text(fake_value, encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "deleted-secret.txt"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "add fixture"], check=True)
    secret_file.unlink()
    subprocess.run(["git", "-C", str(repository), "add", "-u"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "remove fixture"], check=True)

    result = scan_git_objects(repository, ())

    assert any(item.rule_id == "known-token-full-value" for item in result.findings)
    assert fake_value not in repr(result)


def test_artifact_comment_and_actual_slack_request_use_same_scanner() -> None:
    secret = ("configured" + "-secret-" + "Q" * 24).encode("utf-8")
    payloads = {
        "ci/artifact-metadata.json": b'{"digest":"ok","debug":"' + secret + b'"}',
        "github/pr-evidence-comment.json": b'{"body":"' + secret + b'"}',
        "slack/chat.postMessage.request.json": b'{"channel":"C0BES16KE1J","text":"' + secret + b'"}',
    }
    for path, payload in payloads.items():
        findings = scan_bytes(path, payload, (secret,))
        assert [(item.path, item.rule_id) for item in findings] == [
            (path, "configured-secret-value")
        ]


def test_secret_scan_cli_accepts_git_objects_and_labeled_payloads() -> None:
    from forge.guard.cli import build_parser

    parsed = build_parser().parse_args(
        [
            "secret-scan",
            "--paths",
            "C:/work/repository",
            "C:/evidence/build",
            "--git-repository",
            "C:/work/repository",
            "--payload-file",
            "ci/artifact-metadata.json=C:/evidence/ci-artifacts.json",
            "--payload-file",
            "github/pr-comment.json=C:/evidence/pr-comment.json",
            "--payload-file",
            "slack/chat.postMessage.request.json=C:/evidence/slack-request.json",
        ]
    )
    assert parsed.command == "secret-scan"
    assert parsed.git_repository == ["C:/work/repository"]
    assert len(parsed.payload_file) == 3

    ci = build_parser().parse_args(
        [
            "secret-scan-ci",
            "--event-path",
            "C:/runner/event.json",
            "--report-output",
            "C:/runner/.forge-ci/secret-scan.json",
        ]
    )
    assert ci.command == "secret-scan-ci"
    assert ci.event_path == "C:/runner/event.json"


def test_secret_scan_ci_scans_git_objects_writes_atomic_report_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "guard@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Guard Test"],
        check=True,
    )
    secret = "gh" + "p_" + "R" * 36
    deleted = repository / "deleted.txt"
    deleted.write_text(secret, encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "deleted.txt"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "fixture"], check=True)
    deleted.unlink()
    subprocess.run(["git", "-C", str(repository), "add", "-u"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "remove"], check=True)
    event = repository / "event.json"
    event.write_text('{"event":"push"}\n', encoding="utf-8")
    metadata = repository / ".forge-ci" / "verified-evidence.json"
    metadata.parent.mkdir()
    metadata.write_text('{"status":"ok"}\n', encoding="utf-8")
    report = repository / ".forge-ci" / "secret-scan.json"
    monkeypatch.chdir(repository)

    assert secret_scan_ci_main(
        ["--event-path", str(event), "--report-output", str(report)],
        secret_values=(),
    ) == 2
    report_bytes = report.read_bytes()
    assert b"known-token-full-value" in report_bytes
    assert secret.encode("utf-8") not in report_bytes
    assert list(report.parent.glob(".secret-scan.json.*.tmp")) == []
```

- [ ] **Step 2: RED를 실행한다.**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_secret_scan.py::test_secret_scan_reports_rule_without_echoing_matched_value -q
```

Expected: `ModuleNotFoundError: No module named 'forge.guard.secret_scan'`로 FAIL.

- [ ] **Step 3: byte scanner 최소 GREEN을 구현한다.**

```python
import re
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class SecretFinding:
    path: str
    rule_id: str


def scan_bytes(
    path: str,
    payload: bytes,
    secret_values: Sequence[bytes],
) -> Sequence[SecretFinding]:
    rule_ids: set[str] = set()
    patterns = (
        re.compile(rb"(?<![A-Za-z0-9])" + bytes.fromhex("6768705f") + rb"[A-Za-z0-9]{36,255}(?![A-Za-z0-9])"),
        re.compile(rb"(?<![A-Za-z0-9_])" + bytes.fromhex("6769746875625f7061745f") + rb"[A-Za-z0-9_]{20,255}(?![A-Za-z0-9_])"),
        re.compile(rb"(?<![A-Za-z0-9_-])" + bytes.fromhex("736b2d") + rb"[A-Za-z0-9_-]{20,255}(?![A-Za-z0-9_-])"),
    )
    if any(pattern.search(payload) is not None for pattern in patterns):
        rule_ids.add("known-token-full-value")
    private_begin = bytes.fromhex("2d2d2d2d2d424547494e20")
    private_end = bytes.fromhex("2050524956415445204b45592d2d2d2d2d")
    if private_begin in payload and private_end in payload:
        rule_ids.add("private-key-header")
    for secret in secret_values:
        if len(secret) >= 8 and secret in payload:
            rule_ids.add("configured-secret-value")
    return tuple(SecretFinding(path=path, rule_id=rule) for rule in sorted(rule_ids))
```

- [ ] **Step 4: path scanner와 CLI를 구현한다.**

symlink/junction과 repository root escape를 거절한다. binary file도 bytes로 scan한다. `scan_git_objects`는 `git rev-list --objects --all`의 object ID를 `git cat-file --batch`로 읽어 현재 tree뿐 아니라 branch/tag history를 검사한다. `build_parser()`의 `secret-scan`은 `--paths` 1개 이상, repeated `--git-repository`, repeated `--payload-file LABEL=FILE`을 exact interface로 받고 모든 입력의 합집합을 검사한다. `secret-scan-ci`는 checkout tracked/untracked source, 전체 Git object, event payload, `.forge-ci`의 기존 verify/merge/ops metadata, artifact upload file list, canonical PR comment request를 수집하되 자신의 report temp/final path는 제외한다. 현재 process의 non-empty secret environment bytes도 같은 scanner에 넣고, matched value 없는 `schema_version|scanned_inputs|findings(path,rule_id)` exact JSON을 `--report-output`에 temp→flush→fsync→replace→directory fsync로 원자 기록한다. CI artifact와 PR evidence comment는 게시/업로드 직전 canonical request bytes를, Slack delivery adapter는 `chat.postMessage` transport 호출 직전 실제 request JSON을 같은 `scan_bytes`에 통과시킨다. finding 또는 read 오류가 있으면 transport를 호출하지 않는다. CLI exit는 finding 0개면 0, finding이 있거나 file/Git object read 오류가 있으면 2다. stdout JSON에는 `path`, `rule_id`만 쓰며 stderr에도 content를 쓰지 않는다.

- [ ] **Step 5: GREEN과 regression을 실행한다.**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_secret_scan.py -q
.\.venv\Scripts\python.exe -m pytest tests/guard -q
git diff --check
```

Expected: secret scan tests PASS, guard tests PASS, diff check exit 0.

- [ ] **Step 6: commit한다.**

```powershell
git add forge/guard/secret_scan.py forge/guard/cli.py tests/guard/test_secret_scan.py
git commit -m "feat: scan guard evidence without exposing secrets"
```

## Task 9: 모든 repository PR에 immutable evidence comment를 게시한다

**Files:**
- Create: `forge/guard/evidence_bundle.py`
- Create: `forge/schemas/evidence-bundle-v1.schema.json`
- Create: `tests/guard/test_evidence_bundle.py`
- Modify: `forge/guard/runner.py`
- Modify: `forge/guard/cli.py`

**Consumes:** `post-exit` receipt, canonical contract/handoff, repository별 PR/head, secret scanner, paginated comment client.

**Produces:** 동일 bundle을 각 PR에 exactly-one comment로 upsert하고 current-head check run을 runner가 기다릴 수 있는 `PublishedEvidence`를 반환한다.

**Interfaces:**

```text
build_evidence_bundle(contract: TaskContract, handoff: Handoff, receipt: Receipt, evidence_files: Sequence[Path]) -> EvidenceBundle
render_evidence_comment(bundle: EvidenceBundle, target: PullTarget) -> str
CommentClient.list_comments(repo: str, pr_number: int) -> Sequence[CommentRecord]
CommentClient.create_comment(repo: str, pr_number: int, body: str) -> CommentRecord
CommentClient.update_comment(repo: str, comment_id: int, body: str) -> CommentRecord
publish_evidence_to_all_prs(bundle: EvidenceBundle, client: CommentClient, published_at_s: int, secret_values: Sequence[bytes]) -> PublishedEvidence
ActionsClient.list_runs_for_head(repo: str, head_sha: str) -> Sequence[WorkflowRun]
ActionsClient.rerun_failed_jobs(repo: str, run_id: int) -> None
reconcile_checks_after_publish(published: PublishedEvidence, state: RunnerState, client: ActionsClient) -> RunnerState
extract_evidence_comment(comments: Sequence[CommentRecord], task_id: str, run_id: int, head_sha: str) -> EvidenceBundle
```

- [ ] **Step 1: two-repository exactly-one comment RED test를 작성한다.**

```python
from collections.abc import Sequence
from dataclasses import dataclass

import pytest

from forge.guard.errors import GateError
from forge.guard.evidence_bundle import (
    CommentRecord,
    EvidenceBundle,
    PullTarget,
    publish_evidence_to_all_prs,
)


@dataclass
class FakeCommentClient:
    store: dict[tuple[str, int], list[CommentRecord]]
    next_id: int = 1

    def list_comments(self, repo: str, pr_number: int) -> Sequence[CommentRecord]:
        return tuple(self.store.get((repo, pr_number), []))

    def create_comment(self, repo: str, pr_number: int, body: str) -> CommentRecord:
        record = CommentRecord(comment_id=self.next_id, body=body)
        self.next_id += 1
        self.store.setdefault((repo, pr_number), []).append(record)
        return record

    def update_comment(self, repo: str, comment_id: int, body: str) -> CommentRecord:
        for key, records in self.store.items():
            for index, record in enumerate(records):
                if record.comment_id == comment_id:
                    updated = CommentRecord(comment_id=comment_id, body=body)
                    records[index] = updated
                    return updated
        raise AssertionError(f"comment {comment_id} not found in {repo}")


def test_multi_repo_bundle_is_upserted_once_per_pull_request() -> None:
    bundle = EvidenceBundle(
        task_id="t_guard_core",
        run_id=7,
        bundle_sha256="a" * 64,
        payload_json=b'{"schema_version":"forge-evidence-v1"}',
        pull_targets=(
            PullTarget(repo="example/api", pr_number=11, head_sha="b" * 40),
            PullTarget(repo="example/web", pr_number=22, head_sha="c" * 40),
        ),
    )
    client = FakeCommentClient(store={})
    publish_evidence_to_all_prs(
        bundle, client, published_at_s=1_700_000_000, secret_values=()
    )
    publish_evidence_to_all_prs(
        bundle, client, published_at_s=1_700_000_001, secret_values=()
    )

    assert len(client.store[("example/api", 11)]) == 1
    assert len(client.store[("example/web", 22)]) == 1
    assert "head_sha=" + "b" * 40 in client.store[("example/api", 11)][0].body
    assert "head_sha=" + "c" * 40 in client.store[("example/web", 22)][0].body


def test_comment_transport_is_not_called_when_rendered_body_contains_secret() -> None:
    secret = ("outgoing-secret-" + "Q" * 24).encode("utf-8")
    bundle = EvidenceBundle(
        task_id="t_secret",
        run_id=8,
        bundle_sha256="d" * 64,
        payload_json=b'{"debug":"' + secret + b'"}',
        pull_targets=(
            PullTarget(repo="example/api", pr_number=11, head_sha="e" * 40),
        ),
    )
    client = FakeCommentClient(store={})

    with pytest.raises(GateError, match="SECRET_SCAN_FAILED"):
        publish_evidence_to_all_prs(
            bundle,
            client,
            published_at_s=1_700_000_000,
            secret_values=(secret,),
        )

    assert client.store == {}
    assert client.next_id == 1
```

- [ ] **Step 2: RED를 실행한다.**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_evidence_bundle.py::test_multi_repo_bundle_is_upserted_once_per_pull_request -q
```

Expected: `ModuleNotFoundError: No module named 'forge.guard.evidence_bundle'`로 FAIL.

- [ ] **Step 3: comment upsert 최소 GREEN을 구현한다.**

```python
from collections.abc import Sequence
from dataclasses import dataclass

from forge.guard.errors import GateError
from forge.guard.secret_scan import scan_bytes


@dataclass(frozen=True)
class PullTarget:
    repo: str
    pr_number: int
    head_sha: str


@dataclass(frozen=True)
class CommentRecord:
    comment_id: int
    body: str


@dataclass(frozen=True)
class EvidenceBundle:
    task_id: str
    run_id: int
    bundle_sha256: str
    payload_json: bytes
    pull_targets: Sequence[PullTarget]


@dataclass(frozen=True)
class PublishedEvidence:
    bundle_sha256: str
    published_at_s: int
    pull_targets: Sequence[PullTarget]
    comments: Sequence[CommentRecord]


def _marker_prefix(bundle: EvidenceBundle) -> str:
    return f"<!-- forge-evidence-v1 task_id={bundle.task_id} run_id={bundle.run_id} "


def render_evidence_comment(bundle: EvidenceBundle, target: PullTarget) -> str:
    marker = (
        _marker_prefix(bundle)
        + f"head_sha={target.head_sha} bundle_sha256={bundle.bundle_sha256} -->"
    )
    body = marker + "\n~~~json\n" + bundle.payload_json.decode("utf-8") + "\n~~~\n"
    if len(body.encode("utf-8")) > 60_000:
        raise GateError("EVIDENCE_COMMENT_TOO_LARGE", "evidence comment exceeds 60000 bytes")
    return body


def publish_evidence_to_all_prs(
    bundle: EvidenceBundle,
    client,
    published_at_s: int,
    secret_values: Sequence[bytes],
) -> PublishedEvidence:
    published: list[CommentRecord] = []
    prefix = _marker_prefix(bundle)
    for target in sorted(bundle.pull_targets, key=lambda item: (item.repo, item.pr_number)):
        matches = [
            comment
            for comment in client.list_comments(target.repo, target.pr_number)
            if comment.body.startswith(prefix)
        ]
        if len(matches) > 1:
            raise GateError("DUPLICATE_EVIDENCE_COMMENT", target.repo)
        body = render_evidence_comment(bundle, target)
        findings = scan_bytes(
            f"github/{target.repo}/pull/{target.pr_number}/comment-request.txt",
            body.encode("utf-8"),
            secret_values,
        )
        if findings:
            raise GateError("SECRET_SCAN_FAILED", target.repo)
        if matches:
            record = client.update_comment(target.repo, matches[0].comment_id, body)
        else:
            record = client.create_comment(target.repo, target.pr_number, body)
        published.append(record)
    return PublishedEvidence(
        bundle_sha256=bundle.bundle_sha256,
        published_at_s=published_at_s,
        pull_targets=tuple(bundle.pull_targets),
        comments=tuple(published),
    )
```

- [ ] **Step 4: bundle binding과 secret scan을 구현한다.**

bundle은 canonical contract, handoff, `post-exit` receipt, relative evidence descriptors와 digest, 모든 `repo/pr/head` tuple을 포함한다. absolute path, environment, raw stdout/stderr, secret finding은 hard fail이다. bundle target set이 contract repository set과 exact-equal이 아니면 `TESTS_FAILED`다.

- [ ] **Step 5: comment-before-check race를 수렴시킨다.**

PR job의 evidence step은 comment를 최대 300초, 10초 간격으로 기다린다. runner가 comment를 게시했는데 current-head workflow run이 그 게시 시각 전에 `failure`로 끝났다면 `reconcile_checks_after_publish`가 해당 Actions run의 failed jobs를 head당 정확히 한 번 rerun하고 `ci_rerun_requested_heads`에 atomic 기록한다. 게시 뒤 시작된 failure는 자동 rerun하지 않고 `GATE_ERROR`로 보류한다.

- [ ] **Step 6: runner complete-ready gate를 연결한다.**

모든 repository에서 `guard-contract (ubuntu-latest)`와 `guard-contract (windows-latest)`가 current head에 존재하고 success일 때만 `ready_for_completion`을 반환한다. missing, pending timeout, red, stale-head check는 `GATE_ERROR`다. check poll은 새 Codex slot을 할당하지 않는다.

- [ ] **Step 7: GREEN과 regression을 실행한다.**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_evidence_bundle.py tests/integration/test_ci_evidence.py -q
.\.venv\Scripts\python.exe -m pytest tests/guard tests/integration -q
git diff --check
```

Expected: evidence tests PASS, guard/integration tests PASS, diff check exit 0.

- [ ] **Step 8: commit한다.**

```powershell
git add forge/guard/evidence_bundle.py forge/guard/runner.py forge/guard/cli.py forge/schemas/evidence-bundle-v1.schema.json tests/guard/test_evidence_bundle.py tests/integration/test_ci_evidence.py
git commit -m "feat: publish guard evidence to every pull request"
```

## Task 10: event별 workflow와 onboarding 이후 첫 protected PR full evidence를 구현한다

**Files:**
- Create: `forge/guard/ci_event.py`
- Create: `tests/guard/test_ci_event.py`
- Create: `tests/test_workflow_contract.py`
- Modify: `forge/guard/cli.py`
- Modify: `.github/workflows/capability-eval.yml`

**Consumes:** GitHub event JSON의 explicit `repository.full_name`, umbrella Task 0이 provision한 strict repository variable `FORGE_OPS_HOST`, bootstrap issue에서 만든 local `TaskContract`, full multi-repo tuple을 운반하는 PR evidence comment verifier, current-repository-scoped GitHub client, secret scanner.

**Produces:** `pr_evidence|merge_group|regression|ops_audit` route와 stable named Windows·Ubuntu checks. umbrella Task 0 onboarding merge·repository variable provisioning 이후 첫 protected 구현 PR부터 evidence 예외가 없고 Actions 결과는 current repository slice의 live 검증만 증명한다. schedule/manual route는 strict `FORGE_OPS_HOST` repository variable로 fail-closed 분기하며 canonical host만 `ops_audit`, secondary repository는 API 없는 `regression`을 수행한다.

**Interfaces:**

```text
parse_ops_host(value: str | None) -> bool
route_ci_event(event_name: str, payload: Mapping[str, object], ops_host_value: str | None) -> CiRoute
write_github_output(route: CiRoute, output_path: Path) -> None
verify_ci_event(event_name: str, event_path: Path, wait_seconds: int, poll_seconds: int) -> VerificationResult
verify_merge_group(event_path: Path, evidence_output: Path) -> VerificationResult
verify_ops_audit(event_path: Path, evidence_output: Path) -> VerificationResult
verify_ops_audit_main(argv: Sequence[str]) -> int
verify_merge_group_with_client(current_repo: str, payload: Mapping[str, object], client: MergeGroupClient) -> Mapping[str, object]
verify_ops_audit_with_client(current_repo: str, deployed_sha: str, bootstrap_issue: int, now_s: int, client: OpsEvidenceClient) -> Mapping[str, object]
render_ops_evidence_comment(bundle: Mapping[str, object]) -> str
```

- [ ] **Step 1: onboarding 이후 첫 protected PR과 host-only schedule route RED test를 작성한다.**

```python
import json
from pathlib import Path

import pytest

from forge.guard.ci_event import (
    CiMode,
    ci_route_main,
    route_ci_event,
    verify_merge_group_with_client,
    verify_ops_audit_main,
    verify_ops_audit_with_client,
    render_ops_evidence_comment,
)
from forge.guard.errors import GateError


class FakeCiGitHub:
    def __init__(
        self,
        *,
        allowed_repo,
        associated_pages,
        comments_by_pull,
        checks_by_head,
    ):
        self.allowed_repo = allowed_repo
        self.associated_pages = associated_pages
        self.comments_by_pull = comments_by_pull
        self.checks_by_head = checks_by_head
        self.associated_page_calls = 0
        self.api_repositories = []

    def _record_repo(self, repo: str) -> None:
        assert repo == self.allowed_repo
        self.api_repositories.append(repo)

    def list_associated_pulls(
        self,
        repo: str,
        merge_head_sha: str,
        cursor: int | None,
    ):
        self._record_repo(repo)
        index = 0 if cursor is None else cursor
        self.associated_page_calls += 1
        next_cursor = index + 1 if index + 1 < len(self.associated_pages) else None
        return self.associated_pages[index], next_cursor

    def list_comments(self, repo: str, pr_number: int):
        self._record_repo(repo)
        return self.comments_by_pull[(repo, pr_number)]

    def list_check_runs(self, repo: str, head_sha: str):
        self._record_repo(repo)
        return self.checks_by_head[(repo, head_sha)]


class FakeOpsCommentClient:
    def __init__(self, *, allowed_repo, pages, deployed_relation):
        self.allowed_repo = allowed_repo
        self.pages = pages
        self.deployed_relation = deployed_relation
        self.page_calls = 0
        self.api_repositories = []
        self.deployed_relation_calls = []

    def list_issue_comments(self, repo: str, issue: int, cursor: int | None):
        assert repo == self.allowed_repo
        self.api_repositories.append(repo)
        index = 0 if cursor is None else cursor
        self.page_calls += 1
        next_cursor = index + 1 if index + 1 < len(self.pages) else None
        return self.pages[index], next_cursor

    def get_deployed_relation(self, repo: str, deployed_sha: str):
        assert repo == self.allowed_repo
        self.api_repositories.append(repo)
        self.deployed_relation_calls.append((repo, deployed_sha))
        return self.deployed_relation


@pytest.mark.parametrize(
    ("event_name", "payload", "ops_host_value", "expected"),
    (
        (
            "pull_request",
            {
                "action": "opened",
                "pull_request": {
                    "base": {"sha": "a" * 40, "ref": "main"},
                    "head": {"sha": "b" * 40},
                },
            },
            "true",
            CiMode.PR_EVIDENCE,
        ),
        (
            "pull_request",
            {
                "action": "ready_for_review",
                "pull_request": {
                    "base": {"sha": "c" * 40, "ref": "main"},
                    "head": {"sha": "d" * 40},
                },
            },
            "false",
            CiMode.PR_EVIDENCE,
        ),
        (
            "push",
            {"ref": "refs/heads/main", "after": "e" * 40},
            "false",
            CiMode.REGRESSION,
        ),
        (
            "merge_group",
            {"merge_group": {"head_sha": "f" * 40}},
            "true",
            CiMode.MERGE_GROUP,
        ),
        ("schedule", {}, "true", CiMode.OPS_AUDIT),
        ("workflow_dispatch", {}, "true", CiMode.OPS_AUDIT),
        ("schedule", {}, "false", CiMode.REGRESSION),
        ("workflow_dispatch", {}, "false", CiMode.REGRESSION),
    ),
)
def test_event_routes_are_explicit(
    event_name: str,
    payload: dict[str, object],
    ops_host_value: str,
    expected: CiMode,
) -> None:
    assert route_ci_event(event_name, payload, ops_host_value).mode is expected


@pytest.mark.parametrize(
    ("ops_host_value", "expected_code"),
    (
        (None, "FORGE_OPS_HOST_MISSING"),
        ("", "FORGE_OPS_HOST_MISSING"),
        ("TRUE", "FORGE_OPS_HOST_INVALID"),
        ("false ", "FORGE_OPS_HOST_INVALID"),
        ("1", "FORGE_OPS_HOST_INVALID"),
    ),
)
def test_event_routes_reject_noncanonical_ops_host(
    ops_host_value: str | None,
    expected_code: str,
) -> None:
    with pytest.raises(GateError, match=expected_code):
        route_ci_event("schedule", {}, ops_host_value)


def test_ci_route_writes_exact_github_step_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "action": "opened",
                "pull_request": {
                    "base": {"sha": "a" * 40, "ref": "main"},
                    "head": {"sha": "b" * 40},
                },
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "github-output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("FORGE_OPS_HOST", "false")

    assert ci_route_main(["--event-name", "pull_request", "--event-path", str(event)]) == 0
    assert output.read_text(encoding="utf-8") == "mode=pr_evidence\n"


@pytest.mark.parametrize(
    "command",
    ("verify-merge-group", "verify-ops-audit"),
)
def test_event_specific_verifier_cli_contract(command: str) -> None:
    from forge.guard.cli import build_parser

    parsed = build_parser().parse_args(
        [
            command,
            "--event-path",
            "event.json",
            "--evidence-output",
            "evidence.json",
        ]
    )
    assert parsed.command == command


def test_ops_audit_cli_requires_exact_deployed_sha_variable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = tmp_path / "event.json"
    event.write_text("{}\n", encoding="utf-8")
    output = tmp_path / "ops.json"
    monkeypatch.setenv("FORGE_OPS_HOST", "true")
    monkeypatch.delenv("FORGE_DEPLOYED_SHA", raising=False)
    with pytest.raises(GateError, match="FORGE_DEPLOYED_SHA_MISSING"):
        verify_ops_audit_main(
            ["--event-path", str(event), "--evidence-output", str(output)]
        )
    monkeypatch.setenv("FORGE_DEPLOYED_SHA", "not-a-sha")
    with pytest.raises(GateError, match="FORGE_DEPLOYED_SHA_INVALID"):
        verify_ops_audit_main(
            ["--event-path", str(event), "--evidence-output", str(output)]
        )


def test_ops_audit_cli_does_not_interpret_host_issue_as_secondary_issue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = tmp_path / "event.json"
    event.write_text("{}\n", encoding="utf-8")
    output = tmp_path / "ops.json"
    monkeypatch.setenv("FORGE_OPS_HOST", "false")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/secondary")
    monkeypatch.setenv("FORGE_BOOTSTRAP_REPOSITORY", "example/host")
    monkeypatch.setenv("FORGE_BOOTSTRAP_ISSUE", "77")
    monkeypatch.setenv("FORGE_DEPLOYED_SHA", "a" * 40)

    with pytest.raises(GateError, match="OPS_AUDIT_NOT_HOST"):
        verify_ops_audit_main(
            ["--event-path", str(event), "--evidence-output", str(output)]
        )


def test_ops_audit_cli_requires_current_bootstrap_repository_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = tmp_path / "event.json"
    event.write_text("{}\n", encoding="utf-8")
    output = tmp_path / "ops.json"
    argv = ["--event-path", str(event), "--evidence-output", str(output)]
    monkeypatch.setenv("FORGE_OPS_HOST", "true")
    monkeypatch.setenv("FORGE_DEPLOYED_SHA", "a" * 40)
    monkeypatch.setenv("FORGE_BOOTSTRAP_ISSUE", "77")
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("FORGE_BOOTSTRAP_REPOSITORY", raising=False)

    with pytest.raises(GateError, match="GITHUB_REPOSITORY_MISSING"):
        verify_ops_audit_main(argv)

    monkeypatch.setenv("GITHUB_REPOSITORY", "example/current")
    with pytest.raises(GateError, match="BOOTSTRAP_REPOSITORY_MISSING"):
        verify_ops_audit_main(argv)

    monkeypatch.setenv("FORGE_BOOTSTRAP_REPOSITORY", "example/other")
    with pytest.raises(GateError, match="CROSS_REPOSITORY_TOKEN_SCOPE"):
        verify_ops_audit_main(argv)


def test_merge_group_reads_all_current_repo_pages_without_cross_repo_api() -> None:
    current_repo = "example/current"
    pulls = [
        {
            "repo": current_repo,
            "pr_number": index + 1,
            "head_sha": f"{index:040x}",
        }
        for index in range(101)
    ]
    full_tuples = {
        pull["pr_number"]: (
            (current_repo, pull["pr_number"], pull["head_sha"]),
            ("example/external", 10_000 + pull["pr_number"], "e" * 40),
        )
        for pull in pulls
    }
    client = FakeCiGitHub(
        allowed_repo=current_repo,
        associated_pages=(pulls[:100], pulls[100:]),
        comments_by_pull={
            (pull["repo"], pull["pr_number"]): (
                {
                    "task_id": f"task-{pull['pr_number']}",
                    "run_id": 7,
                    "head_sha": pull["head_sha"],
                    "head_tuple": full_tuples[pull["pr_number"]],
                },
            )
            for pull in pulls
        },
        checks_by_head={
            (pull["repo"], pull["head_sha"]): (
                {"name": "guard-contract (ubuntu-latest)", "conclusion": "success"},
                {"name": "guard-contract (windows-latest)", "conclusion": "success"},
            )
            for pull in pulls
        },
    )
    result = verify_merge_group_with_client(
        current_repo,
        {"merge_group": {"head_sha": "f" * 40}},
        client,
    )
    assert len(result["associated_pulls"]) == 101
    assert client.associated_page_calls == 2
    assert set(client.api_repositories) == {current_repo}
    assert all(len(item) == 2 for item in full_tuples.values())

    last = pulls[-1]
    client.checks_by_head[(last["repo"], last["head_sha"])] = (
        {"name": "guard-contract (ubuntu-latest)", "conclusion": "success"},
    )
    with pytest.raises(GateError, match="MERGE_GROUP_EVIDENCE_INVALID"):
        verify_merge_group_with_client(
            current_repo,
            {"merge_group": {"head_sha": "f" * 40}},
            client,
        )


def test_ops_audit_reads_paginated_bootstrap_comment_and_rejects_stale_canary() -> None:
    current_repo = "example/current"
    now_s = 1_783_828_800
    bundle = {
        "schema_version": "forge-ops-evidence-v1",
        "source_sha": "a" * 40,
        "build_manifest_sha256": "b" * 64,
        "targets": {
            target: {
                "receipt_source_sha": "a" * 40,
                "receipt_build_manifest_sha256": "b" * 64,
                "receipt_result": "success",
                "activation_open": True,
                "canary_status": "success",
                "canary_at_s": now_s - 300,
                "drift_findings": 0,
                "drift_at_s": now_s - 300,
            }
            for target in ("windows", "linux", "vps")
        },
    }
    client = FakeOpsCommentClient(
        allowed_repo=current_repo,
        deployed_relation={
            "kind": "pr_head",
            "pr_number": 17,
            "head_sha": "a" * 40,
        },
        pages=(
            tuple({"body": "noise"} for _ in range(100)),
            ({"body": render_ops_evidence_comment(bundle)},),
        )
    )
    result = verify_ops_audit_with_client(
        current_repo=current_repo,
        deployed_sha="a" * 40,
        bootstrap_issue=77,
        now_s=now_s,
        client=client,
    )
    assert set(result["targets"]) == {"windows", "linux", "vps"}
    assert client.page_calls == 2
    assert set(client.api_repositories) == {current_repo}
    assert client.deployed_relation_calls == [(current_repo, "a" * 40)]
    assert result["deployed_sha"] == "a" * 40
    assert result["deployed_relation"]["kind"] == "pr_head"

    client.deployed_relation = {
        "kind": "pr_head",
        "pr_number": 18,
        "head_sha": "c" * 40,
    }
    with pytest.raises(GateError, match="OPS_DEPLOYED_SHA_UNRELATED"):
        verify_ops_audit_with_client(
            current_repo=current_repo,
            deployed_sha="a" * 40,
            bootstrap_issue=77,
            now_s=now_s,
            client=client,
        )

    bundle["targets"]["vps"]["canary_at_s"] = now_s - 25_201
    client.deployed_relation = {
        "kind": "merged_ancestor",
        "ancestor_sha": "a" * 40,
        "merge_commit_sha": "d" * 40,
    }
    client.pages = (({"body": render_ops_evidence_comment(bundle)},),)
    with pytest.raises(GateError, match="OPS_EVIDENCE_STALE"):
        verify_ops_audit_with_client(
            current_repo=current_repo,
            deployed_sha="a" * 40,
            bootstrap_issue=77,
            now_s=now_s,
            client=client,
        )
```

- [ ] **Step 2: RED를 실행한다.**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard/test_ci_event.py::test_event_routes_are_explicit -q
```

Expected: `ModuleNotFoundError: No module named 'forge.guard.ci_event'`로 FAIL.

- [ ] **Step 3: 네 mode router와 GitHub output writer 최소 GREEN을 구현한다.**

```python
import json
import os
import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from forge.guard.errors import GateError


class CiMode(str, Enum):
    PR_EVIDENCE = "pr_evidence"
    MERGE_GROUP = "merge_group"
    REGRESSION = "regression"
    OPS_AUDIT = "ops_audit"


@dataclass(frozen=True)
class CiRoute:
    mode: CiMode
    base_sha: str | None
    head_sha: str | None


def parse_ops_host(value: str | None) -> bool:
    if value is None or value == "":
        raise GateError(
            "FORGE_OPS_HOST_MISSING",
            "repository variable FORGE_OPS_HOST must be true or false",
        )
    if value not in {"true", "false"}:
        raise GateError(
            "FORGE_OPS_HOST_INVALID",
            "repository variable FORGE_OPS_HOST must be exactly true or false",
        )
    return value == "true"


def route_ci_event(
    event_name: str,
    payload: Mapping[str, object],
    ops_host_value: str | None,
) -> CiRoute:
    ops_host = parse_ops_host(ops_host_value)
    if event_name == "pull_request":
        action = str(payload.get("action", ""))
        if action not in {"opened", "synchronize", "reopened", "ready_for_review"}:
            raise GateError("UNSUPPORTED_PR_ACTION", action)
        pull = payload.get("pull_request")
        if not isinstance(pull, Mapping):
            raise GateError("INVALID_EVENT", "pull_request must be an object")
        base = pull.get("base")
        head = pull.get("head")
        if not isinstance(base, Mapping) or not isinstance(head, Mapping):
            raise GateError("INVALID_EVENT", "base and head must be objects")
        return CiRoute(CiMode.PR_EVIDENCE, str(base["sha"]), str(head["sha"]))
    if event_name == "push":
        return CiRoute(CiMode.REGRESSION, None, None)
    if event_name == "merge_group":
        merge_group = payload.get("merge_group")
        if not isinstance(merge_group, Mapping):
            raise GateError("INVALID_EVENT", "merge_group must be an object")
        return CiRoute(
            CiMode.MERGE_GROUP,
            None,
            str(merge_group["head_sha"]),
        )
    if event_name in {"schedule", "workflow_dispatch"}:
        mode = CiMode.OPS_AUDIT if ops_host else CiMode.REGRESSION
        return CiRoute(mode, None, None)
    raise GateError("UNSUPPORTED_EVENT", event_name)


def write_github_output(route: CiRoute, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("ab") as handle:
        handle.write(f"mode={route.mode.value}\n".encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())


def build_ci_route_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m forge.guard ci-route")
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--event-path", required=True)
    return parser


def ci_route_main(argv: Sequence[str]) -> int:
    args = build_ci_route_parser().parse_args(argv)
    payload = json.loads(Path(args.event_path).read_text(encoding="utf-8"))
    route = route_ci_event(
        args.event_name,
        payload,
        os.environ.get("FORGE_OPS_HOST"),
    )
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        raise GateError("GITHUB_OUTPUT_MISSING", "ci-route requires GITHUB_OUTPUT")
    write_github_output(route, Path(output_path))
    return 0
```

- [ ] **Step 4: workflow contract RED test를 작성하고 실행한다.**

```python
from pathlib import Path


def test_workflow_has_stable_matrix_and_event_specific_steps() -> None:
    workflow = Path(".github/workflows/capability-eval.yml").read_text(encoding="utf-8")
    assert "pull_request:" in workflow
    assert "merge_group:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "schedule:" in workflow
    assert "guard-contract (${{ matrix.os }})" in workflow
    assert "steps.route.outputs.mode == 'pr_evidence'" in workflow
    assert "steps.route.outputs.mode == 'merge_group'" in workflow
    assert "steps.route.outputs.mode == 'ops_audit'" in workflow
    assert "--wait-seconds 300 --poll-seconds 10" in workflow
    assert "bootstrap" not in workflow
    assert "python -m forge.guard secret-scan-ci" in workflow
    assert "checks: read" in workflow
    assert "id: secret_scan" in workflow
    assert "if: always() && steps.secret_scan.outcome == 'success'" in workflow
    assert "python -m forge.guard verify-merge-group" in workflow
    assert "python -m forge.guard verify-ops-audit" in workflow
    assert "FORGE_OPS_HOST: ${{ vars.FORGE_OPS_HOST }}" in workflow
    assert "FORGE_BOOTSTRAP_ISSUE: ${{ vars.FORGE_BOOTSTRAP_ISSUE }}" in workflow
    assert "FORGE_BOOTSTRAP_REPOSITORY: ${{ vars.FORGE_BOOTSTRAP_REPOSITORY }}" in workflow
    assert "FORGE_DEPLOYED_SHA: ${{ vars.FORGE_DEPLOYED_SHA }}" in workflow
    assert workflow.count("FORGE_OPS_HOST:") == 2
    assert workflow.count("FORGE_BOOTSTRAP_ISSUE:") == 1
    assert workflow.count("FORGE_BOOTSTRAP_REPOSITORY:") == 1
    assert workflow.count("FORGE_DEPLOYED_SHA:") == 1
    assert "--evidence-output .forge-ci/verified-evidence.json" in workflow
    assert "--report-output .forge-ci/secret-scan.json" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "guard-evidence-${{ matrix.os }}" in workflow
```

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_workflow_contract.py::test_workflow_has_stable_matrix_and_event_specific_steps -q
```

Expected: current workflow에 stable matrix/evidence route가 없어 assertion FAIL.

- [ ] **Step 5: workflow 최소 GREEN을 작성한다.**

```yaml
name: capability-eval

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
  push:
    branches: [main]
  merge_group:
  schedule:
    - cron: "0 22 * * 0"
  workflow_dispatch:

permissions:
  contents: read
  checks: read
  issues: read
  pull-requests: read
  actions: read

jobs:
  guard-contract:
    name: guard-contract (${{ matrix.os }})
    runs-on: ${{ matrix.os }}
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, windows-latest]
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: python -m pip install --require-hashes -r requirements.lock
      - run: python -m pip install -e . --no-deps
      - id: route
        run: python -m forge.guard ci-route --event-name "${{ github.event_name }}" --event-path "${{ github.event_path }}"
        env:
          FORGE_OPS_HOST: ${{ vars.FORGE_OPS_HOST }}
      - run: python -m pytest tests -q
      - run: python -m compileall forge
      - if: steps.route.outputs.mode == 'pr_evidence'
        run: python -m forge.guard verify-ci --event-path "${{ github.event_path }}" --wait-seconds 300 --poll-seconds 10 --evidence-output .forge-ci/verified-evidence.json
        env:
          GH_TOKEN: ${{ github.token }}
      - if: steps.route.outputs.mode == 'merge_group'
        run: python -m forge.guard verify-merge-group --event-path "${{ github.event_path }}" --evidence-output .forge-ci/merge-group-evidence.json
        env:
          GH_TOKEN: ${{ github.token }}
      - if: steps.route.outputs.mode == 'ops_audit'
        run: python -m forge.guard verify-ops-audit --event-path "${{ github.event_path }}" --evidence-output .forge-ci/ops-audit-evidence.json
        env:
          GH_TOKEN: ${{ github.token }}
          FORGE_OPS_HOST: ${{ vars.FORGE_OPS_HOST }}
          FORGE_BOOTSTRAP_ISSUE: ${{ vars.FORGE_BOOTSTRAP_ISSUE }}
          FORGE_BOOTSTRAP_REPOSITORY: ${{ vars.FORGE_BOOTSTRAP_REPOSITORY }}
          FORGE_DEPLOYED_SHA: ${{ vars.FORGE_DEPLOYED_SHA }}
      - id: secret_scan
        if: always()
        run: python -m forge.guard secret-scan-ci --event-path "${{ github.event_path }}" --report-output .forge-ci/secret-scan.json
      - if: always() && steps.secret_scan.outcome == 'success'
        uses: actions/upload-artifact@v4
        with:
          name: guard-evidence-${{ matrix.os }}
          path: .forge-ci
          if-no-files-found: error
          retention-days: 7
      - if: runner.os == 'Linux'
        run: |
          set -euo pipefail
          for file in forge/scripts/*.sh forge/hooks/*.sh; do
            bash -n "$file"
          done
```

- [ ] **Step 6: event별 CI semantics를 고정한다.**

umbrella Task 0의 repository별 onboarding-only PR이 default branch에 merge되고 repository variable provisioning까지 완료된 뒤, 첫 protected 구현 PR을 포함한 모든 후속 pull_request는 comment의 task/run/current PR head와 전체 multi-repo head tuple·tuple digest를 strict parse한다. 이 시점부터 compatibility/enforcement 예외는 없다. `github.token`으로 하는 issue/PR/comment/check API 조회는 event의 `repository.full_name`과 exact-equal인 current repository slice에만 한정하고 current repository의 PR/head/comment/check와 pinned checkout command를 재검증한다. source issue/AC가 current repository 소유일 때만 live issue API를 조회하고, 다른 repository 소유라면 canonical digest binding만 검사한 뒤 live 검증은 외부 rollout runner에 맡긴다. secondary repository tuple은 comment transport 안에 그대로 보존하지만 이 Actions job에서 live API 조회하지 않는다. comment를 300초 기다린 뒤에도 없으면 `EVIDENCE_MISSING`으로 실패한다. runner는 comment 게시 전에 시작해 그 이유로 실패한 current-head run만 정확히 한 번 rerun하고, 게시 뒤 시작된 failure는 자동 rerun하지 않는다. push main은 contract/full test와 secret scan만 실행한다. CI 밖 rollout runner가 repository별 explicit client로 모든 tuple slice와 source issue/AC의 live PR/head/two-check 상태를 aggregate한 뒤 전체 task를 complete-ready로 만든다.

`verify-merge-group`은 `GITHUB_REPOSITORY`를 explicit `current_repo`로 받고 event의 merge head에 연결된 **같은 current repository의** associated PR을 모든 page에서 조회한다. PR마다 current head SHA, exactly-one Forge evidence comment의 전체 multi-repo tuple·digest, 그 tuple에서 선택한 current repository slice, `guard-contract (ubuntu-latest)`와 `guard-contract (windows-latest)` prior success를 검증한다. comment의 secondary repository slice는 삭제하지 않지만 `github.token`으로 secondary repository API를 호출하지 않는다. associated PR 0개, 다른 repo API request, duplicate/missing comment, stale head, missing/pending/red named check는 `MERGE_GROUP_EVIDENCE_INVALID` exit 2다. 현재 merge-group run 자체를 prior check로 세지 않는다. canonical current-repo associated PR/head/check 결과와 full tuple digest를 evidence output에 원자 기록하고, cross-repo live aggregate는 외부 rollout runner가 담당한다.

`verify-ops-audit` handler는 API client를 만들거나 bootstrap/deployment variable을 읽기 전에 exact `FORGE_OPS_HOST=true`를 다시 검증한다. `false`이면 `OPS_AUDIT_NOT_HOST`로 종료하며 bootstrap issue API를 호출하지 않는다. host에서는 current repository variable `FORGE_DEPLOYED_SHA`를 required 40자리 lowercase hex audit source로 사용한다. schedule checkout의 `GITHUB_SHA`, schedule payload, current main tip 또는 ambient `gh` context로 이 값을 대체하거나 추정하지 않는다. `GITHUB_REPOSITORY`와 repository variable `FORGE_BOOTSTRAP_REPOSITORY`를 canonical `OWNER/REPO`로 검증하고 두 repository가 exact-equal일 때만 audit하며, `FORGE_BOOTSTRAP_ISSUE`는 그 canonical host repository의 positive integer issue 번호이고 모든 API path/client call에 `current_repo`를 명시한다. 따라서 secondary repository의 중앙 issue 번호는 current-repository issue로 해석되지 않는다. current-repository API로 `FORGE_DEPLOYED_SHA`가 exact PR head로 존재하거나 그 SHA를 보존한 merged-ancestry evidence가 있는지만 main과의 관계 metadata로 검증한다. 이 관계 evidence는 배포 SHA를 main tip/merge commit SHA로 바꾸지 않으며, evidence output의 `deployed_sha`와 exact-one `forge-ops-evidence-v1` canonical comment marker/source SHA는 끝까지 `FORGE_DEPLOYED_SHA`와 exact-equal이어야 한다. 관계가 없거나 relation payload가 다른 SHA를 가리키면 `OPS_DEPLOYED_SHA_UNRELATED`다. bootstrap comment의 모든 page를 읽어 Windows/Linux/VPS deployment receipts, activation marker, canary, drift reports가 같은 deployed source SHA와 build manifest digest를 가지며 receipt/marker는 current, canary는 25,200초(7시간) 이내, drift는 7,200초(2시간) 이내이고 모두 success/zero finding인지 검증한다. schedule payload나 ambient repository에서 SHA/repo를 추정하거나 `github.token`으로 다른 repository를 조회하면 `CROSS_REPOSITORY_TOKEN_SCOPE`다. 최초 comment는 rollout Task 5가 explicit bootstrap repository에 upsert한다. 이후 Windows hourly Drift publisher도 immutable Scheduled Task argv의 `--bootstrap-repository OWNER/REPO`와 그 repository에서 읽은 issue 번호를 사용해 같은 repo/same-deployed-SHA comment만 갱신하며 ambient checkout을 사용하지 않는다. CI audit은 live host, service, issue, PR을 변경하지 않으며 repository/variable/comment가 온보딩되지 않았거나 duplicate/stale/missing이면 `OPS_EVIDENCE_MISSING`/`OPS_EVIDENCE_STALE` exit 2다. 모든 repository/host의 live aggregate는 외부 rollout runner가 수행한다.

`ci-route`는 `${{ vars.FORGE_OPS_HOST }}`를 step environment `FORGE_OPS_HOST`로 받아 모든 event에서 exact lowercase `true|false`만 허용한다. unset/empty는 `FORGE_OPS_HOST_MISSING`, 대소문자·공백·`0|1` 등 다른 값은 `FORGE_OPS_HOST_INVALID`로 fail-closed한다. `schedule`/`workflow_dispatch`에서 `true`는 `ops_audit`, `false`는 API step이 없는 `regression`이다. secondary route에서는 `FORGE_BOOTSTRAP_REPOSITORY`, `FORGE_BOOTSTRAP_ISSUE`, `FORGE_DEPLOYED_SHA`를 읽거나 `GH_TOKEN`을 ops handler에 넘기지 않는다. 이 세 variable은 `ops_audit` conditional step 한 곳에만 scoped되며, handler도 host flag를 다시 검사한다. canonical mode는 stdout에만 쓰는 대신 `$GITHUB_OUTPUT` 파일에 정확히 `mode=<mode>\n`을 append/flush/fsync해 following step condition을 활성화한다. `verify-ci`, `verify-merge-group`, `verify-ops-audit`의 `--evidence-output`과 `secret-scan-ci --report-output`은 matched value 없는 canonical JSON을 `.forge-ci`에 원자 기록하고, 각 matrix job은 이를 `guard-evidence-<os>` artifact로 항상 업로드한다. rollout은 current-head success run의 두 artifact를 실제 다운로드해 다시 scan한다.

- [ ] **Step 7: GREEN과 전체 core CI regression을 실행한다.**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/guard tests/integration tests/test_workflow_contract.py -q
.\.venv\Scripts\python.exe -m compileall forge
git diff --check
```

```bash
python3.11 -m pytest tests/guard tests/integration tests/test_workflow_contract.py -q
python3.11 -m compileall forge
bash -n forge/hooks/codex-stop-gate.sh
```

Expected: Windows/Linux tests PASS, compileall/Bash/diff check exit 0.

- [ ] **Step 8: commit한다.**

```powershell
git add forge/guard/ci_event.py forge/guard/cli.py tests/guard/test_ci_event.py tests/test_workflow_contract.py .github/workflows/capability-eval.yml
git commit -m "ci: enforce guard evidence on every pull request"
```

## 완료 게이트

- [ ] `stop=3,900`, `post-exit=86,400`, `ci=7,200`, `hermes=900`초 expiry와 Hermes-only single-use를 test가 증명한다.
- [ ] preflight/Popen 오류는 slot 미소비, Popen 성공 후 unknown thread는 기존 slot 유지, `GATE_ERROR` recovery는 새 slot 0개임을 test가 증명한다.
- [ ] 다중 repository의 모든 PR에 exactly-one current-head evidence comment가 있고 duplicate가 `GATE_ERROR`임을 test가 증명한다.
- [ ] comment 게시 전 시작된 failed check rerun은 head당 1회이고, 게시 뒤 발생한 failure는 자동 반복하지 않음을 test가 증명한다.
- [ ] umbrella Task 0 onboarding-only PR merge·repository variable provisioning 이후 첫 protected 구현 PR을 포함한 모든 pull_request가 bootstrap issue/local contract/comment를 사용하는 `pr_evidence` route이며 enforcement 예외가 없음을 test가 증명한다.
- [ ] `push`는 regression, `merge_group`은 current-repo associated PR/head/named-check 검증, `schedule`/`workflow_dispatch`는 `FORGE_OPS_HOST=true` canonical host에서만 explicit current-repo bootstrap comment의 read-only canary/drift `ops_audit`, `false` secondary repository에서는 ops API 없는 regression으로 분기한다. missing/invalid host variable과 direct secondary audit 호출은 fail-closed하고 `$GITHUB_OUTPUT`의 exact mode가 각 step을 실제 실행시킨다. comment의 full multi-repo tuple은 유지하되 `github.token` cross-repo API call은 0회이고 rollout runner만 전체 live tuple을 aggregate한다.
- [ ] Windows와 Ubuntu의 named checks가 `guard-contract (windows-latest)`, `guard-contract (ubuntu-latest)`로 실제 생성된다.
- [ ] source, 전체 Git object, bundle, release file list, CI metadata, PR comment와 실제 Slack request JSON의 secret scan 결과가 0건이고 scanner가 matched value를 출력하지 않는다.
- [ ] default suite가 외부 Codex 호출을 하지 않으며 opt-in live smoke에서 TESTS_FAILED continuation의 thread ID가 동일하다.
- [ ] 전체 test, compileall, Bash syntax, `git diff --check`, code review P0/P1 0건을 fresh evidence로 남긴다.

## 실행 handoff

이 subplan은 guard core와 CI만 완결한다. 완료 후 Hermes atomic completion subplan이 `phase=hermes` receipt 소비 API를 연결하고, 운영 rollout subplan이 이 commit의 exact SHA artifact를 Windows→Linux staging→VPS 순서로 배포한다.

## 변경이력

- 2026-07-12 | guard core/CI subplan 작성 | 변경: contract, multi-repo evidence, command/reference verifier, phase receipt, crash-safe runner, Stop hook, secret scan, PR comment CI를 10개 TDD Task로 고정 | 이유: 승인된 공통 verifier가 onboarding 이후 첫 protected PR을 포함한 모든 정상 실행 경로에서 같은 증거를 사용하도록 하기 위함 | 검증: Python 예제 AST parse, 금지 표현 scan, Markdown fence, `git diff --check`; 제품 코드와 live CI는 실행 단계에서 검증
- 2026-07-12 | Hermes adapter·다중 repo·credential P1 보강 | 변경: verifier 결과 adapter와 canonical receipt digest, secondary repo CI onboarding hard gate, full-value token pattern, configured secret substring, 전체 Git object 및 artifact/PR/실제 Slack request scan RED tests를 추가 | 이유: 공통 verifier와 Hermes 소비 계약을 연결하고 미온보딩 repo 또는 scanner 자기탐지·누락이 실행을 잘못 통과시키지 않도록 함 | 검증: 구현 전 계획 단계이며 Python fenced block AST, workflow YAML, 금지 placeholder, Git diff 검증 대상으로 등록
- 2026-07-12 | Actions current-repository CI 경계 보강 | 변경: full multi-repo tuple은 evidence comment에 유지하되 `github.token` API 검증을 current repository slice로 제한하고, same-repo 101 PR pagination RED fake, explicit bootstrap repository context, 외부 rollout live aggregate 책임, ops canary freshness 25,200초를 Task 10 계약에 추가했다. ops audit source는 current main의 `GITHUB_SHA`가 아니라 required repository variable `FORGE_DEPLOYED_SHA`로 고정하고, PR head 또는 merged ancestry는 배포 SHA를 대체하지 않는 관계 evidence로만 사용한다 | 이유: repository-scoped Actions token으로 secondary private repository를 조회하는 불가능한 계약을 제거하고 Windows publisher와 core audit의 repository·deployed SHA 대상을 일치시키기 위함 | 검증: Python fenced block AST parse, Markdown fence 짝수, exact current-repo/canary/deployed-SHA variable scan과 `git diff --check` 대상; production code와 live GitHub API는 아직 실행하지 않음
- 2026-07-12 | host-only schedule 경계 보강 | 변경: strict `FORGE_OPS_HOST=true|false` parser와 route RED cases, workflow route/audit env, canonical host-only ops audit, secondary regression/no-API 계약을 Task 10에 추가하고 “최초 구현 PR”을 umbrella Task 0 onboarding merge 이후 첫 protected 구현 PR로 한정했다 | 이유: secondary repository가 중앙 bootstrap issue 번호를 자기 issue로 오인하거나 schedule마다 ops API를 호출하는 것을 막고 onboarding workflow의 self-enforcement 순환을 제거하기 위함 | 검증: Python fenced block AST parse, host/secondary route 및 workflow variable scope scan, Markdown fence, whitespace 검사 대상; production code와 live GitHub API는 아직 실행하지 않음
