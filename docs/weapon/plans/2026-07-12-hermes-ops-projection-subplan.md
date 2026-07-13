# Hermes 운영·투영·배포 구현 계획

> **Agentic worker(Agent 실행자)용:** REQUIRED SUB-SKILL: 이 plan을 task 단위로 구현하려면 `weapon:subagent-driven-development`(권장) 또는 `weapon:executing-plans`를 사용한다. 진행 추적에는 checkbox(`- [ ]`) 문법을 쓴다.

**Goal:** receipt가 확인된 Hermes 결과만 GitHub/spec에 투영하고, canary·drift가 dispatcher만 fail-closed로 제어하며, 동일한 immutable build를 Windows·일반 Linux·Ubuntu VPS에 설치·롤백할 수 있는 운영층을 만든다.

**Architecture:** `forge.ops`의 순수 Python core가 Hermes/GitHub/process adapter와 분리되어 label projection, spec coverage, supervisor, canary, drift를 결정한다. 설치 파일은 checked-in systemd/Windows Task 정의를 사용하고 각 repository에 Codex Stop hook을 설치한다. build manifest는 재현 가능한 정적 사실만 담고, host별 시각·이전 release·결과는 별도 deployment receipt에 기록한다.

**Tech Stack:** Python 3.11+, pytest 8.x, stdlib dataclass/enum/json/subprocess/sqlite3/pathlib, Hermes Agent v0.18.2 CLI, GitHub CLI, systemd user units, PowerShell 7 Scheduled Tasks

## Global Constraints

1. 선행 의존성은 umbrella plan Task 2~9의 `forge-guard`, versioned schema, receipt ledger, Hermes `completion_policy=forge-v1` patch다.
2. executor card는 `tenant=forge`, `goal=true`, `goal_max_turns=20`, `max_runtime=60m`, `max_retries=4`, `completion_policy=forge-v1`을 고정한다.
3. Hermes v0.18.2 create 문법은 positional title과 `--body`, `--assignee`, `--project` 또는 `--workspace`, `--idempotency-key`, `--skill`을 사용한다.
4. delegate는 반드시 `forge-guard prepare`가 완료된 blocked card만 실행하며, `forge-guard run`의 `complete-ready` 뒤에만 `kanban_complete`를 호출한다.
5. raw `status=done`은 완료 증거가 아니다. `protected=true AND valid=true AND consumed=true`인 receipt가 있어야 완료 라벨·coverage 후보가 된다.
6. GitHub 조회·pagination·SQLite·service inspection 오류를 0건이나 빈 목록으로 바꾸지 않는다. 판정 불능은 `GATE_ERROR`와 exit 2다.
7. canary marker는 Windows current-user-only ACL 또는 POSIX mode 600이며, marker가 없거나 stale이거나 SHA가 다르면 dispatcher child를 실행하지 않는다.
8. gateway embedded dispatcher를 먼저 비활성화하고 gateway에 적용된 사실을 확인한 뒤 active task/tmux를 drain한다.
9. Stop hook installer는 target deployment input의 모든 task repository에 실행하고 설치 후 hook JSON과 release SHA-256을 재검증한다.
10. POSIX Forge runtime SSoT는 `/usr/bin/python3`이다. systemd Python service는 immutable current release를 `WorkingDirectory`와 `PYTHONPATH`로 함께 고정하고 scripts는 mode 0755로 설치한다. user service는 linger와 enable 상태를 확인하고 controlled reboot 뒤 다시 검증한다.
11. Windows Scheduled Task는 Startup VBS로 fallback하지 않는다. 등록 실패는 target deployment 실패와 rollback이다.
12. build는 full 40자리 Git SHA와 green named checks를 요구한다. remote `git pull`, 자동 commit, 자동 push를 금지한다.
13. immutable build manifest에는 시간·target·previous release를 넣지 않는다. 이 동적 값은 host deployment receipt에만 기록한다.
14. runtime state는 Windows `%LOCALAPPDATA%\InfinityForge\state`, Linux/VPS `${XDG_STATE_HOME:-~/.local/state}/infinity-forge`에 둔다.
15. 배포 순서는 Windows local → 일반 Linux staging → Ubuntu VPS이며, 각 target rollback/forward가 green이어야 다음 target으로 진행한다.
16. Hermes patch/core는 `guard/current.json`의 exact nested contract `{"schema_version":"forge-completion-manifest/v1","policies":{"forge-v1":{"python":ABSOLUTE_PATH,"artifact":ABSOLUTE_PATH,"artifact_sha256":SHA256,"timeout_seconds":INTEGER}}}`를 검증한 뒤에만 실행한다. source SHA는 artifact absolute path의 immutable release directory와 build manifest를 대조한다. 이 파일은 atomic replace하고 배포 전 snapshot과 rollback 대상에 포함한다.
17. Linux/VPS의 clean Hermes bootstrap은 승인된 upstream base commit `4281151ae859241351ba14d8c7682dc67ff4c126`을 checkout identity로 고정하고 CLI `Hermes Agent v0.18.2 (2026.7.7.2)`를 확인한다. `v2026.7.7.2`는 version label일 뿐 checkout 기준이 아니다. ambient `uv`를 사용하지 않고 checked-in wheel hash lock으로 만든 bootstrap venv의 `uv 0.11.24`만 사용한다.

---

## Rollout target paths

| Target | Project source | Immutable release root | Current pointer | Guard current manifest | Runtime state | Hermes checkout |
|---|---|---|---|---|---|---|
| Windows local | `C:\01.project\INFINITY_FORGE` | `C:\Users\황화인HwainHwang\AppData\Local\InfinityForge\guard\releases` | `C:\Users\황화인HwainHwang\AppData\Local\InfinityForge\current` | `C:\Users\황화인HwainHwang\AppData\Local\InfinityForge\guard\current.json` | `C:\Users\황화인HwainHwang\AppData\Local\InfinityForge\state` | `C:\Users\황화인HwainHwang\AppData\Local\hermes\hermes-agent` |
| Linux staging (WSL Ubuntu) | `/home/immortal0900/work/INFINITY_FORGE/<source-sha>` (Linux FS 독립 clone) | `/home/immortal0900/.local/share/infinity-forge/guard/releases` | `/home/immortal0900/.local/share/infinity-forge/current` | `/home/immortal0900/.local/share/infinity-forge/guard/current.json` | `/home/immortal0900/.local/state/infinity-forge` | `/home/immortal0900/.hermes/hermes-agent` |
| Ubuntu VPS | `/home/ubuntu/work/INFINITY_FORGE` | `/home/ubuntu/.local/share/infinity-forge/guard/releases` | `/home/ubuntu/.local/share/infinity-forge/current` | `/home/ubuntu/.local/share/infinity-forge/guard/current.json` | `/home/ubuntu/.local/state/infinity-forge` | `/home/ubuntu/.hermes/hermes-agent` |

각 immutable release는 해당 release root 아래 full 40자리 source SHA 이름의 child directory로 설치한다. `current`는 검증된 child directory를 가리키는 atomic symlink 또는 Windows directory junction이다.

WSL repository hook은 Linux filesystem의 source-SHA별 clone에만 설치한다. `/mnt/c/01.project/INFINITY_FORGE`는 Windows repository이므로 WSL adapter가 hook install/verify 대상으로 받으면 preflight에서 exit 2다. Windows `.codex/hooks.json` digest는 WSL apply 전후 exact-equal이어야 한다.

## 3~5수 앞 검토

| 현재 결정 | 다음 효과 | 장기 효과 | 실패 복구 |
|---|---|---|---|
| blocked card에서 contract 준비 | worker가 immutable contract보다 먼저 실행되지 않음 | import 재시작에도 같은 idempotency key와 task contract 재사용 | prepare 실패 card를 blocked로 유지하고 alert |
| receipt-aware projection | raw done이나 직접 SQL 결과가 GitHub 완료로 전파되지 않음 | consumer가 늘어도 CompletionStatus 한 계약을 재사용 | projection state를 이전 atomic JSON으로 복원 |
| embedded dispatcher 선중단 | drain 중 신규 worker 생성이 없음 | gateway와 dispatcher lifecycle 독립 유지 | config snapshot 복원 후 gateway graceful restart |
| build/receipt 분리 | artifact hash가 host마다 달라지지 않음 | 동일 build의 다중 host 감사 가능 | previous release pointer와 host receipt로 target별 rollback |

## 파일 책임 지도

```text
forge/
  hermes/
    uv-bootstrap-linux.lock     Linux x86_64/aarch64 uv wheel hash lock
  ops/
    __init__.py                 공통 export
    contracts.py                운영 dataclass와 enum
    executor_bridge.py          Hermes create argv, prepare/unblock
    github.py                   paginated gh adapter와 label patch
    hermes.py                   task/event/receipt read adapter
    label_mirror.py             import와 receipt-aware projection
    spec_coverage.py            SPEC 전체 술어와 structured report
    dispatcher_supervisor.py    canary marker에 따른 child lifecycle
    canary.py                   marker close, 결정론 check, reopen
    drift_audit.py              운영 불변식 집계와 GATE_ERROR
    deployment.py               build manifest, host receipt, deploy transaction
  schemas/
    spec-registry.schema.json
    build-manifest.schema.json
    guard-current.schema.json
    deployment-receipt.schema.json
  scripts/
    label-mirror.py
    spec-coverage.py
    spec-coverage.sh
    dispatcher-supervisor.py
    canary.py
    canary.sh
    drift-audit.py
    drift-audit.sh
    install-linux.sh
    verify-linux-install.sh
    install-windows.ps1
    build-guard-release.py
    deploy.ps1
    deploy-vps.sh
    rollback.ps1
    rollback-linux.sh
    rollback-vps.sh
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
forge/skills/kanban-codex-delegate/SKILL.md
tests/ops/
  test_executor_bridge.py
  test_label_mirror.py
  test_spec_coverage.py
  test_dispatcher_supervisor.py
  test_canary.py
  test_drift_audit.py
  test_install_contracts.py
  test_deployment.py
```

### Task 1: executor card 생성과 guard runner 연결

**Files:**
- Create: `forge/ops/__init__.py`
- Create: `forge/ops/contracts.py`
- Create: `forge/ops/executor_bridge.py`
- Create: `tests/ops/test_executor_bridge.py`
- Modify: `forge/skills/kanban-codex-delegate/SKILL.md`

**Consumes:**
- `forge-guard prepare --request -`: stdin의 canonical request를 trusted state에 저장하고 exit 0을 반환한다.
- `forge-guard run --task-id TASK_ID`: stdout에 단일 JSON document를 쓰며 완료 가능할 때 `{"status":"complete-ready","task_id":"TASK_ID"}`를 반환한다.
- Hermes v0.18.2 `kanban create`, `kanban unblock`, worker의 `kanban_complete` tool.

**Produces:**
- `ExecutorCardSpec`
- `build_executor_create_argv(hermes: Path, spec: ExecutorCardSpec) -> Sequence[str]`
- `prepare_and_unblock(task_id: str, current_status: str, request: Mapping[str, object], guard: JsonCommand, hermes: JsonCommand) -> None`
- delegate 순서 `forge-guard run → complete-ready 확인 → kanban_complete`

**Interfaces:**

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

JsonCommand = Callable[[Sequence[str], bytes | None], Mapping[str, object]]

@dataclass(frozen=True)
class ExecutorCardSpec:
    title: str
    body: str
    repo: str
    issue_number: int
    project: str | None
    workspace: Path | None

    @property
    def idempotency_key(self) -> str:
        return f"github-issue:{self.repo}#{self.issue_number}"
```

- [ ] **Step 1: 실제 Hermes CLI argv를 고정하는 RED test 작성**

```python
import ast
from pathlib import Path

from forge.ops.contracts import ExecutorCardSpec
from forge.ops.executor_bridge import build_executor_create_argv


def test_executor_create_uses_hermes_0182_cli_contract() -> None:
    spec = ExecutorCardSpec(
        title="[mirror] Guard task",
        body="GitHub issue: https://github.com/acme/widget/issues/7",
        repo="acme/widget",
        issue_number=7,
        project="widget",
        workspace=None,
    )

    argv = build_executor_create_argv(Path("/home/ops/.local/bin/hermes"), spec)

    assert argv == (
        "/home/ops/.local/bin/hermes",
        "kanban",
        "create",
        "[mirror] Guard task",
        "--body",
        "GitHub issue: https://github.com/acme/widget/issues/7",
        "--assignee",
        "executor",
        "--project",
        "widget",
        "--tenant",
        "forge",
        "--idempotency-key",
        "github-issue:acme/widget#7",
        "--max-runtime",
        "60m",
        "--max-retries",
        "4",
        "--goal",
        "--goal-max-turns",
        "20",
        "--completion-policy",
        "forge-v1",
        "--skill",
        "kanban-codex-delegate",
        "--initial-status",
        "blocked",
        "--json",
    )
```

- [ ] **Step 2: prepare와 unblock 순서를 고정하는 RED test 작성**

```python
import json
from dataclasses import replace

import pytest
from dataclasses import replace
from typing import Mapping

from forge.ops.executor_bridge import prepare_and_unblock


def test_contract_is_prepared_before_card_unblock() -> None:
    calls: list[tuple[str, Sequence[str], bytes | None]] = []

    def guard(argv: Sequence[str], stdin: bytes | None) -> Mapping[str, object]:
        calls.append(("guard", argv, stdin))
        return {"status": "prepared", "task_id": "t_guard7"}

    def hermes(argv: Sequence[str], stdin: bytes | None) -> Mapping[str, object]:
        calls.append(("hermes", argv, stdin))
        return {"id": "t_guard7", "status": "todo"}

    request = {"schema_version": "task-contract-v1", "task_id": "t_guard7"}
    prepare_and_unblock("t_guard7", "blocked", request, guard, hermes)

    assert calls[0][0:2] == ("guard", ("forge-guard", "prepare", "--request", "-"))
    assert json.loads(calls[0][2].decode("utf-8")) == request
    assert calls[1] == (
        "hermes",
        (
            "hermes",
            "kanban",
            "unblock",
            "--reason",
            "forge contract prepared",
            "t_guard7",
        ),
        None,
    )
```

- [ ] **Step 3: idempotent existing card RED test 작성**

```python
from typing import Mapping, Sequence

from forge.ops.executor_bridge import prepare_and_unblock


def test_existing_unblocked_card_is_not_unblocked_again() -> None:
    calls: list[str] = []

    def guard(argv: Sequence[str], stdin: bytes | None) -> Mapping[str, object]:
        calls.append("prepare")
        return {"status": "prepared", "task_id": "t_existing"}

    def hermes(argv: Sequence[str], stdin: bytes | None) -> Mapping[str, object]:
        calls.append("unblock")
        return {"id": "t_existing", "status": "todo"}

    prepare_and_unblock(
        "t_existing",
        "todo",
        {"schema_version": "task-contract-v1", "task_id": "t_existing"},
        guard,
        hermes,
    )

    assert calls == ["prepare"]
```

- [ ] **Step 4: delegate skill ordering RED test 작성**

```python
from pathlib import Path


def test_delegate_runs_guard_before_kanban_complete() -> None:
    skill = (
        Path(__file__).resolve().parents[2]
        / "forge"
        / "skills"
        / "kanban-codex-delegate"
        / "SKILL.md"
    ).read_text(encoding="utf-8")

    run_at = skill.index('forge-guard run --task-id "$HERMES_KANBAN_TASK_ID"')
    ready_at = skill.index("complete-ready", run_at)
    complete_at = skill.index("그 뒤에만 `kanban_complete`", ready_at)

    assert run_at < ready_at < complete_at
```

- [ ] **Step 5: RED 확인**

Run: `.\.venv\Scripts\python.exe -m pytest tests/ops/test_executor_bridge.py -q`

Expected: `ModuleNotFoundError: No module named 'forge.ops.executor_bridge'`로 FAIL.

- [ ] **Step 6: argv builder와 prepare/unblock 최소 구현**

```python
import json
from pathlib import Path
from typing import Mapping, Sequence

from forge.ops.contracts import ExecutorCardSpec, JsonCommand


def build_executor_create_argv(
    hermes: Path, spec: ExecutorCardSpec
) -> Sequence[str]:
    if (spec.project is None) == (spec.workspace is None):
        raise ValueError("exactly one of project or workspace is required")
    target = (
        ("--project", spec.project)
        if spec.project is not None
        else ("--workspace", f"dir:{spec.workspace}")
    )
    return (
        str(hermes),
        "kanban",
        "create",
        spec.title,
        "--body",
        spec.body,
        "--assignee",
        "executor",
        target[0],
        str(target[1]),
        "--tenant",
        "forge",
        "--idempotency-key",
        spec.idempotency_key,
        "--max-runtime",
        "60m",
        "--max-retries",
        "4",
        "--goal",
        "--goal-max-turns",
        "20",
        "--completion-policy",
        "forge-v1",
        "--skill",
        "kanban-codex-delegate",
        "--initial-status",
        "blocked",
        "--json",
    )


def prepare_and_unblock(
    task_id: str,
    current_status: str,
    request: Mapping[str, object],
    guard: JsonCommand,
    hermes: JsonCommand,
) -> None:
    payload = json.dumps(
        request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    prepared = guard(("forge-guard", "prepare", "--request", "-"), payload)
    if prepared.get("status") != "prepared" or prepared.get("task_id") != task_id:
        raise RuntimeError("guard prepare did not confirm the task")
    if current_status != "blocked":
        return
    unblocked = hermes(
        (
            "hermes",
            "kanban",
            "unblock",
            "--reason",
            "forge contract prepared",
            task_id,
        ),
        None,
    )
    if unblocked.get("id") != task_id:
        raise RuntimeError("Hermes unblock did not confirm the task")
```

- [ ] **Step 7: delegate skill의 실행 순서를 정확히 교체**

```markdown
1. `kanban_show`로 task ID, source issue, AC, repository 목록을 읽는다.
2. `forge-guard run --task-id "$HERMES_KANBAN_TASK_ID"`를 실행하고 stdout JSON을 `$HOME/.hermes/kanban/logs/$HERMES_KANBAN_TASK_ID-forge-result.json`에 저장한다.
3. JSON의 `status`가 `complete-ready`이고 `task_id`가 `$HERMES_KANBAN_TASK_ID`와 같을 때만 다음 단계로 이동한다.
4. 그 뒤에만 `kanban_complete` tool을 호출한다. `TESTS_FAILED`, `GATE_ERROR`, JSON parse 오류에서는 `kanban_complete`를 호출하지 않는다.
```

- [ ] **Step 8: GREEN과 skill ordering 확인**

Run: `.\.venv\Scripts\python.exe -m pytest tests/ops/test_executor_bridge.py -q`

Expected: `4 passed`.

Run: `rg -n "forge-guard run|complete-ready|kanban_complete" forge/skills/kanban-codex-delegate/SKILL.md`

Expected: 세 문자열이 실행 순서대로 출력된다.

- [ ] **Step 9: commit**

```powershell
git add forge/ops/__init__.py forge/ops/contracts.py forge/ops/executor_bridge.py tests/ops/test_executor_bridge.py forge/skills/kanban-codex-delegate/SKILL.md
git commit -m "feat: route executor cards through Forge guard runner"
```

### Task 2: receipt-aware label mirror

**Files:**
- Modify: `forge/ops/contracts.py`
- Create: `forge/ops/github.py`
- Create: `forge/ops/hermes.py`
- Create: `forge/ops/label_mirror.py`
- Create: `tests/ops/test_label_mirror.py`
- Create: `tests/ops/test_hermes_cli.py`
- Modify: `forge/scripts/label-mirror.py`

**Consumes:**
- Task 1의 `ExecutorCardSpec`, `build_executor_create_argv`, `prepare_and_unblock`.
- `forge-guard completion-status --task-id TASK_ID --json`.
- paginated GitHub issue 목록과 Hermes event/receipt 조회.

**Produces:**
- `CompletionStatus`
- `ProjectionCard`
- `decide_projection(card: ProjectionCard, completion: CompletionStatus) -> ProjectionDecision`
- `python -m forge.ops.hermes verify-db --path DB_PATH --acl current-user|posix-0600`
- `python -m forge.ops.hermes gateway-health --expect healthy|stopped`
- thin entrypoint `forge/scripts/label-mirror.py`.

**Interfaces:**

```python
from dataclasses import dataclass
from enum import Enum
from typing import Sequence

class PipelineStage(str, Enum):
    EXECUTOR = "executor"
    REVIEWER = "reviewer"
    CRITIC = "critic"

@dataclass(frozen=True)
class CompletionStatus:
    protected: bool
    valid: bool
    consumed: bool
    digest: str | None

@dataclass(frozen=True)
class ProjectionCard:
    task_id: str
    status: str
    stage: PipelineStage
    retry_exhausted: bool
    sticky_protocol_violation: bool
    gave_up: bool

@dataclass(frozen=True)
class ProjectionDecision:
    label: str | None
    reason: str
    exit_code: int
```

- [ ] **Step 1: raw done 차단과 stage 전이를 고정하는 RED tests 작성**

```python
from forge.ops.contracts import (
    CompletionStatus,
    PipelineStage,
    ProjectionCard,
)
from forge.ops.label_mirror import decide_projection


def test_receiptless_done_is_not_projected() -> None:
    card = ProjectionCard(
        task_id="t_done1",
        status="done",
        stage=PipelineStage.EXECUTOR,
        retry_exhausted=False,
        sticky_protocol_violation=False,
        gave_up=False,
    )
    completion = CompletionStatus(
        protected=True,
        valid=False,
        consumed=False,
        digest=None,
    )

    decision = decide_projection(card, completion)

    assert decision.label is None
    assert decision.reason == "completion receipt is not valid and consumed"
    assert decision.exit_code == 2


def test_consumed_executor_and_critic_use_distinct_labels() -> None:
    completion = CompletionStatus(
        protected=True,
        valid=True,
        consumed=True,
        digest="sha256:receipt-7",
    )
    executor = ProjectionCard(
        task_id="t_exec",
        status="done",
        stage=PipelineStage.EXECUTOR,
        retry_exhausted=False,
        sticky_protocol_violation=False,
        gave_up=False,
    )
    critic = ProjectionCard(
        task_id="t_critic",
        status="done",
        stage=PipelineStage.CRITIC,
        retry_exhausted=False,
        sticky_protocol_violation=False,
        gave_up=False,
    )

    assert decide_projection(executor, completion).label == "forge:need-review"
    assert decide_projection(critic, completion).label == "forge:mergeable"
```

- [ ] **Step 2: RED 확인**

Run: `.\.venv\Scripts\python.exe -m pytest tests/ops/test_label_mirror.py -q`

Expected: `ImportError: cannot import name 'decide_projection'`로 FAIL.

- [ ] **Step 2a: rollout에서 사용하는 Hermes 운영 CLI parser RED test 작성**

```python
from forge.ops.hermes import build_parser


def test_hermes_ops_cli_contract() -> None:
    verify_db = build_parser().parse_args(
        [
            "verify-db",
            "--path",
            "C:/Users/operator/AppData/Local/hermes/kanban.db",
            "--acl",
            "current-user",
        ]
    )
    assert verify_db.command == "verify-db"
    assert verify_db.acl == "current-user"

    gateway = build_parser().parse_args(
        ["gateway-health", "--expect", "healthy"]
    )
    assert gateway.command == "gateway-health"
    assert gateway.expect == "healthy"
```

- [ ] **Step 3: pure projection 최소 구현**

```python
from forge.ops.contracts import (
    CompletionStatus,
    PipelineStage,
    ProjectionCard,
    ProjectionDecision,
)


def decide_projection(
    card: ProjectionCard, completion: CompletionStatus
) -> ProjectionDecision:
    failed = (
        card.retry_exhausted
        or card.sticky_protocol_violation
        or card.gave_up
    )
    if failed:
        return ProjectionDecision("forge:failed", "terminal failure event", 0)
    if card.status == "blocked":
        return ProjectionDecision("forge:blocked", "Hermes task is blocked", 0)
    if card.status != "done":
        labels = {
            "triage": "forge:spec-draft",
            "todo": "forge:need-execution",
            "ready": "forge:need-execution",
            "running": "forge:in-progress",
        }
        return ProjectionDecision(labels.get(card.status), "non-terminal projection", 0)
    if not (
        completion.protected
        and completion.valid
        and completion.consumed
        and completion.digest
    ):
        return ProjectionDecision(
            None,
            "completion receipt is not valid and consumed",
            2,
        )
    if card.stage is PipelineStage.EXECUTOR:
        return ProjectionDecision("forge:need-review", "executor receipt consumed", 0)
    if card.stage is PipelineStage.REVIEWER:
        return ProjectionDecision("forge:need-critic", "reviewer receipt consumed", 0)
    return ProjectionDecision("forge:mergeable", "critic receipt consumed", 0)
```

- [ ] **Step 4: adapters와 entrypoint 연결**

`forge/ops/github.py`는 `gh api --paginate`의 각 page를 JSON으로 파싱하고 nonzero, invalid JSON, 403, 429, 5xx를 `GateError`로 올린다. `forge/ops/hermes.py`는 query-only SQLite로 task/event를 읽고 completion 상태는 trusted `forge-guard completion-status`만 사용한다. 같은 module의 `build_parser()/main()`은 `verify-db --path --acl`과 `gateway-health --expect` 두 subcommand를 소유하며 JSON 한 개와 exit 0/2만 출력한다. `verify-db`는 SQLite quick_check와 Windows current-user-only ACL 또는 POSIX mode 0600을 확인하고, `gateway-health`는 Hermes gateway의 실제 health probe 결과를 expected 상태와 비교한다. label entrypoint는 blocked card 생성→contract prepare→unblock을 수행하고, projection 시 `ProjectionDecision.exit_code == 2`이면 label을 유지하고 `completion_rejected` 감사 기록을 남긴다.

- [ ] **Step 5: GREEN과 회귀 확인**

Run: `.\.venv\Scripts\python.exe -m pytest tests/ops/test_executor_bridge.py tests/ops/test_label_mirror.py tests/ops/test_hermes_cli.py -q`

Expected: `7 passed`.

- [ ] **Step 6: commit**

```powershell
git add forge/ops/github.py forge/ops/hermes.py forge/ops/label_mirror.py forge/scripts/label-mirror.py tests/ops/test_label_mirror.py tests/ops/test_hermes_cli.py
git commit -m "feat: project only receipt-backed Hermes states"
```

### Task 3: structured spec registry와 M/M coverage

**Files:**
- Modify: `forge/ops/contracts.py`
- Create: `forge/spec-registry.json`
- Create: `forge/schemas/spec-registry.schema.json`
- Create: `forge/ops/spec_coverage.py`
- Create: `forge/scripts/spec-coverage.py`
- Create: `tests/ops/test_spec_coverage.py`
- Modify: `forge/spec-registry.md`
- Modify: `forge/scripts/spec-coverage.sh`

**Consumes:**
- Task 2의 receipt-aware `CompletionStatus`.
- GitHub canonical issue, body/AC hash, linked PR merge 상태, named check rollup.
- registry schema `spec-registry-v1`.

**Produces:**
- `SpecEvidence`
- `CoverageItem`
- `CoverageReport`
- `evaluate_spec(spec: SpecEvidence) -> CoverageItem`
- `build_report(items: Sequence[CoverageItem]) -> CoverageReport`
- JSON stdout의 `covered`, `total`, predicate별 실패 목록.
- `python -m forge.ops.spec_coverage --format json --require-complete`

**Interfaces:**

```python
from dataclasses import dataclass
from typing import Sequence

@dataclass(frozen=True)
class SpecEvidence:
    spec_id: str
    canonical_issue_count: int
    source_hash_matches: bool
    acceptance_hash_matches: bool
    issue_closed: bool
    all_prs_merged: bool
    all_required_checks_green: bool
    receipt_consumed: bool

@dataclass(frozen=True)
class CoverageItem:
    spec_id: str
    complete: bool
    failed_predicates: Sequence[str]

@dataclass(frozen=True)
class CoverageReport:
    covered: int
    total: int
    incomplete: Sequence[CoverageItem]
```

- [ ] **Step 1: 전체 술어 conjunction을 고정하는 RED test 작성**

```python
from forge.ops.spec_coverage import (
    SpecEvidence,
    build_report,
    evaluate_spec,
)


def test_spec_is_complete_only_when_every_predicate_is_true() -> None:
    complete = SpecEvidence(
        spec_id="SPEC-001",
        canonical_issue_count=1,
        source_hash_matches=True,
        acceptance_hash_matches=True,
        issue_closed=True,
        all_prs_merged=True,
        all_required_checks_green=True,
        receipt_consumed=True,
    )
    missing_receipt = SpecEvidence(
        spec_id="SPEC-002",
        canonical_issue_count=1,
        source_hash_matches=True,
        acceptance_hash_matches=True,
        issue_closed=True,
        all_prs_merged=True,
        all_required_checks_green=True,
        receipt_consumed=False,
    )

    items = (evaluate_spec(complete), evaluate_spec(missing_receipt))
    report = build_report(items)

    assert items[0].complete is True
    assert items[1].failed_predicates == ("receipt_consumed",)
    assert report.covered == 1
    assert report.total == 2
    assert tuple(item.spec_id for item in report.incomplete) == ("SPEC-002",)
```

- [ ] **Step 2: canonical issue 중복과 API error를 분리하는 RED test 작성**

```python
import pytest

from forge.guard.errors import GateError
from forge.ops.spec_coverage import (
    SpecEvidence,
    build_parser,
    evaluate_spec,
    parse_issue_pages,
)


def test_duplicate_issue_is_incomplete_and_bad_page_is_gate_error() -> None:
    duplicate = SpecEvidence(
        spec_id="SPEC-003",
        canonical_issue_count=2,
        source_hash_matches=True,
        acceptance_hash_matches=True,
        issue_closed=True,
        all_prs_merged=True,
        all_required_checks_green=True,
        receipt_consumed=True,
    )
    assert evaluate_spec(duplicate).failed_predicates == (
        "canonical_issue_exactly_one",
    )

    with pytest.raises(GateError, match="invalid GitHub page JSON"):
        parse_issue_pages((b"{broken",))

    parsed = build_parser().parse_args(["--format", "json", "--require-complete"])
    assert parsed.format == "json"
    assert parsed.require_complete is True
```

- [ ] **Step 3: RED 확인**

Run: `.\.venv\Scripts\python.exe -m pytest tests/ops/test_spec_coverage.py -q`

Expected: `ModuleNotFoundError: No module named 'forge.ops.spec_coverage'`로 FAIL.

- [ ] **Step 4: coverage pure core 최소 구현**

```python
import json
from collections.abc import Sequence

from forge.guard.errors import GateError
from forge.ops.contracts import CoverageItem, CoverageReport, SpecEvidence


def evaluate_spec(spec: SpecEvidence) -> CoverageItem:
    checks = (
        ("canonical_issue_exactly_one", spec.canonical_issue_count == 1),
        ("source_hash_matches", spec.source_hash_matches),
        ("acceptance_hash_matches", spec.acceptance_hash_matches),
        ("issue_closed", spec.issue_closed),
        ("all_prs_merged", spec.all_prs_merged),
        ("all_required_checks_green", spec.all_required_checks_green),
        ("receipt_consumed", spec.receipt_consumed),
    )
    failed = tuple(name for name, ok in checks if not ok)
    return CoverageItem(spec.spec_id, not failed, failed)


def build_report(items: Sequence[CoverageItem]) -> CoverageReport:
    ordered = tuple(sorted(items, key=lambda item: item.spec_id))
    incomplete = tuple(item for item in ordered if not item.complete)
    return CoverageReport(len(ordered) - len(incomplete), len(ordered), incomplete)


def parse_issue_pages(pages: Sequence[bytes]) -> Sequence[dict[str, object]]:
    issues: list[dict[str, object]] = []
    for raw in pages:
        try:
            page = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GateError("invalid GitHub page JSON") from exc
        if not isinstance(page, list):
            raise GateError("GitHub page must be a JSON array")
        for value in page:
            if not isinstance(value, dict):
                raise GateError("GitHub issue must be a JSON object")
            issues.append(value)
    return tuple(issues)
```

- [ ] **Step 5: registry와 entrypoint 연결**

`forge/spec-registry.json`은 `spec_id`, source path/text SHA-256, owner repo, canonical issue number/body hash, AC ID/text hash 목록, PR URL 목록, required check 이름을 필수로 한다. `forge/spec-registry.md`는 JSON에서 매 실행 생성한다. missing spec은 `spec-gap:SPEC-NNN` idempotency key로 blocked issue-finder card를 만들고 contract prepare 후 unblock한다. `build_parser()/main()`은 rollout exact `--format json --require-complete`를 받고 incomplete면 JSON report와 exit 2, complete면 exit 0을 반환한다. shell 파일은 candidate release cwd/PYTHONPATH에서 `/usr/bin/python3 "$release/forge/scripts/spec-coverage.py" "$@"`만 실행한다.

- [ ] **Step 6: GREEN 확인**

Run: `.\.venv\Scripts\python.exe -m pytest tests/ops/test_spec_coverage.py -q`

Expected: `2 passed`.

- [ ] **Step 7: commit**

```powershell
git add forge/spec-registry.json forge/spec-registry.md forge/schemas/spec-registry.schema.json forge/ops/spec_coverage.py forge/scripts/spec-coverage.py forge/scripts/spec-coverage.sh tests/ops/test_spec_coverage.py
git commit -m "feat: compute receipt-aware spec coverage"
```

### Task 4: dispatcher supervisor와 fail-closed canary

**Files:**
- Modify: `forge/ops/contracts.py`
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

**Consumes:**
- deployment manifest의 exact project/guard SHA.
- canary check 결과: hook block, post-exit block, committed-clean PASS, invalid handoff, receiptless completion, artifact SHA, DB quick_check/permission, gateway/Codex auth.
- `hermes kanban daemon --interval 60 --failure-limit 4 --pidfile STATE/dispatcher.pid --verbose`.
- 실행 사용자 홈의 `.codex/secrets/codex-work-report.env`. 현재 실측은 Windows 파일만 존재하고 WSL/VPS 파일은 absent다. rollout Phase 0에서 승인된 Windows source file의 exact allowlisted 4개 key를 Linux/VPS에 stdin-only로 provision하고 mode/digest/identity를 read-back해야 한다.

**Produces:**
- `CanaryMarker`
- `SupervisorDecision`
- `reconcile_dispatcher(marker: CanaryMarker | None, now_epoch_s: int, ttl_s: int, expected_project_sha: str, expected_guard_sha256: str, child_running: bool, start_child: Callable[[], None], stop_child: Callable[[int], None]) -> SupervisorDecision`
- `run_canary(probes: Sequence[CanaryProbe], close_marker: Callable[[], None], stop_dispatcher: Callable[[], None], write_marker: Callable[[], None], alert: Callable[[Sequence[str]], bool]) -> CanaryResult`
- `send_canary_alert(attempt_id: str, target: str, source_sha: str, failed_checks: Sequence[str], env_file: Path, state_root: Path, transport: SlackTransport) -> AlertReceipt`
- `post_scanned_slack_request(request: SlackRequest, env_file: Path, receipt_file: Path, transport: SlackTransport) -> SlackReceipt`
- `python -m forge.ops.canary --mode run|verify|force-stale --target windows|linux|vps (--sha SHA40|--sha-from-build-manifest FILE)`
- `post-canary-alert.py --attempt-id ID --target windows|linux|vps --sha SHA40 --failed-check NAME [--failed-check NAME ...] --env-file FILE --state-root DIR`
- `provision-slack-alert-secret.ps1 -SourceEnvFile FILE -Targets Windows,Linux,Vps -WslDistribution Ubuntu -WslUser immortal0900 -VpsHost ubuntu@51.222.27.48 -RepairWindowsAcl`
- `python -m forge.ops.dispatcher_supervisor status --target windows|linux|vps --expect running|stopped --within-seconds INTEGER`
- marker close 뒤 5초 안에 dispatcher child 종료.
- 실패 alert는 app `codex work report`/App ID `A0BEQAZ1MS5`/channel `C0BES16KE1J`에만 전송한다. alert 전송 실패도 canary red/exit 2이며 marker를 열지 않는다.

**Interfaces:**

```python
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Sequence

class SupervisorAction(str, Enum):
    STARTED = "started"
    KEPT_RUNNING = "kept-running"
    STOPPED = "stopped"
    KEPT_STOPPED = "kept-stopped"

@dataclass(frozen=True)
class CanaryMarker:
    project_sha: str
    guard_sha256: str
    succeeded_at_epoch_s: int

@dataclass(frozen=True)
class SupervisorDecision:
    action: SupervisorAction
    reason: str

@dataclass(frozen=True)
class CanaryCheck:
    name: str
    passed: bool
    detail: str

@dataclass(frozen=True)
class CanaryProbe:
    name: str
    run: Callable[[], CanaryCheck]

@dataclass(frozen=True)
class CanaryResult:
    passed: bool
    failed_checks: Sequence[str]
    alert_delivered: bool | None
```

- [ ] **Step 1: stale marker가 dispatcher만 중단하는 RED test 작성**

```python
from forge.ops.dispatcher_supervisor import (
    CanaryMarker,
    SupervisorAction,
    reconcile_dispatcher,
)


def test_stale_marker_stops_dispatcher_without_touching_gateway() -> None:
    calls: list[str] = []
    marker = CanaryMarker(
        project_sha="a" * 40,
        guard_sha256="b" * 64,
        succeeded_at_epoch_s=1_000,
    )

    decision = reconcile_dispatcher(
        marker=marker,
        now_epoch_s=25_000,
        ttl_s=23_400,
        expected_project_sha="a" * 40,
        expected_guard_sha256="b" * 64,
        child_running=True,
        start_child=lambda: calls.append("start-dispatcher"),
        stop_child=lambda timeout_s: calls.append(f"stop-dispatcher:{timeout_s}"),
    )

    assert decision.action is SupervisorAction.STOPPED
    assert decision.reason == "canary marker is stale"
    assert calls == ["stop-dispatcher:5"]
```

- [ ] **Step 2: canary 실패의 close→stop→alert 순서를 고정하는 RED test 작성**

```python
from forge.ops.canary import CanaryCheck, CanaryProbe, run_canary


def test_failed_canary_never_reopens_marker() -> None:
    calls: list[str] = []
    probes = (
        CanaryProbe(
            "gateway-health",
            lambda: (
                calls.append("probe:gateway-health")
                or CanaryCheck("gateway-health", True, "healthy")
            ),
        ),
        CanaryProbe(
            "receiptless-complete",
            lambda: (
                calls.append("probe:receiptless-complete")
                or CanaryCheck(
                    "receiptless-complete",
                    False,
                    "completion was accepted",
                )
            ),
        ),
    )

    result = run_canary(
        probes=probes,
        close_marker=lambda: calls.append("close-marker"),
        stop_dispatcher=lambda: calls.append("stop-dispatcher"),
        write_marker=lambda: calls.append("write-marker"),
        alert=lambda names: (
            calls.append("scan-and-alert:" + ",".join(names)) or True
        ),
    )

    assert result.passed is False
    assert result.failed_checks == ("receiptless-complete",)
    assert result.alert_delivered is True
    assert calls == [
        "close-marker",
        "stop-dispatcher",
        "probe:gateway-health",
        "probe:receiptless-complete",
        "scan-and-alert:receiptless-complete",
    ]


def test_slack_delivery_failure_cannot_turn_red_canary_green() -> None:
    calls: list[str] = []
    result = run_canary(
        probes=(
            CanaryProbe(
                "gateway-health",
                lambda: CanaryCheck("gateway-health", False, "unhealthy"),
            ),
        ),
        close_marker=lambda: calls.append("close-marker"),
        stop_dispatcher=lambda: calls.append("stop-dispatcher"),
        write_marker=lambda: calls.append("write-marker"),
        alert=lambda _names: False,
    )

    assert result == CanaryResult(
        passed=False,
        failed_checks=("gateway-health",),
        alert_delivered=False,
    )
    assert calls == ["close-marker", "stop-dispatcher"]


def test_rollout_operational_cli_contracts() -> None:
    from forge.ops.canary import build_parser as build_canary_parser
    from forge.ops.dispatcher_supervisor import (
        build_parser as build_supervisor_parser,
    )

    for mode in ("run", "verify", "force-stale"):
        parsed = build_canary_parser().parse_args(
            ["--mode", mode, "--target", "linux", "--sha", "1" * 40]
        )
        assert parsed.mode == mode
        assert parsed.target == "linux"

    scheduled = build_canary_parser().parse_args(
        [
            "--mode",
            "run",
            "--target",
            "vps",
            "--sha-from-build-manifest",
            "/home/ops/.local/share/infinity-forge/current/build-manifest.json",
        ]
    )
    assert scheduled.sha is None
    assert scheduled.sha_from_build_manifest.endswith("build-manifest.json")

    status = build_supervisor_parser().parse_args(
        [
            "status",
            "--target",
            "linux",
            "--expect",
            "stopped",
            "--within-seconds",
            "5",
        ]
    )
    assert status.command == "status"
    assert status.expect == "stopped"
    assert status.within_seconds == 5
```

- [ ] **Step 3: RED 확인**

Run: `.\.venv\Scripts\python.exe -m pytest tests/ops/test_dispatcher_supervisor.py tests/ops/test_canary.py -q`

Expected: 두 module import가 실패한다.

- [ ] **Step 4: supervisor core 최소 구현**

```python
from typing import Callable

from forge.ops.contracts import (
    CanaryMarker,
    SupervisorAction,
    SupervisorDecision,
)


def reconcile_dispatcher(
    *,
    marker: CanaryMarker | None,
    now_epoch_s: int,
    ttl_s: int,
    expected_project_sha: str,
    expected_guard_sha256: str,
    child_running: bool,
    start_child: Callable[[], None],
    stop_child: Callable[[int], None],
) -> SupervisorDecision:
    reason = ""
    if marker is None:
        reason = "canary marker is missing"
    elif now_epoch_s - marker.succeeded_at_epoch_s > ttl_s:
        reason = "canary marker is stale"
    elif marker.project_sha != expected_project_sha:
        reason = "canary project SHA does not match deployment"
    elif marker.guard_sha256 != expected_guard_sha256:
        reason = "canary guard SHA does not match deployment"
    if reason:
        if child_running:
            stop_child(5)
            return SupervisorDecision(SupervisorAction.STOPPED, reason)
        return SupervisorDecision(SupervisorAction.KEPT_STOPPED, reason)
    if child_running:
        return SupervisorDecision(
            SupervisorAction.KEPT_RUNNING,
            "fresh marker matches deployment",
        )
    start_child()
    return SupervisorDecision(
        SupervisorAction.STARTED,
        "fresh marker matches deployment",
    )
```

- [ ] **Step 5: canary core 최소 구현**

```python
from collections.abc import Callable, Sequence

from forge.ops.contracts import CanaryCheck, CanaryProbe, CanaryResult


def run_canary(
    *,
    probes: Sequence[CanaryProbe],
    close_marker: Callable[[], None],
    stop_dispatcher: Callable[[], None],
    write_marker: Callable[[], None],
    alert: Callable[[Sequence[str]], bool],
) -> CanaryResult:
    close_marker()
    stop_dispatcher()
    checks = []
    for probe in probes:
        try:
            check = probe.run()
            if not isinstance(check, CanaryCheck) or check.name != probe.name:
                check = CanaryCheck(probe.name, False, "invalid-probe-result")
        except Exception:
            check = CanaryCheck(probe.name, False, "probe-error")
        checks.append(check)
    failed = tuple(check.name for check in checks if not check.passed)
    if failed:
        delivered = alert(failed)
        return CanaryResult(False, failed, delivered)
    write_marker()
    return CanaryResult(True, (), None)
```

- [ ] **Step 6: platform adapters 구현**

Supervisor entrypoint는 singleton lock을 얻고 marker를 5초마다 읽는다. POSIX child는 SIGTERM→5초 wait→SIGKILL, Windows child는 terminate→5초 wait→kill 순서다. 어떤 분기에서도 gateway PID나 gateway service를 stop하지 않는다. `build_parser()/main()`의 `status`는 target별 child PID/Task 상태를 poll해 expected 상태를 `within-seconds` 안에 확인하고 JSON 한 개와 exit 0/2를 반환한다. canary entrypoint는 시작 즉시 marker를 atomic remove하고 dispatcher stopped를 확인한 뒤 첫 probe로 gateway health를 검사하고 나머지 probe를 실행한다. 전부 green일 때만 temp→flush→fsync→replace로 marker를 쓴다. 실패하면 marker closed/dispatcher stopped를 다시 확인한 상태에서 아래 공통 Slack transport로 즉시 alert를 시도하고, alert 성공 여부와 무관하게 JSON red와 exit 2를 반환한다. canary `build_parser()/main()`은 `run`, read-only `verify`, negative-test 전용 `force-stale` mode를 exact target/SHA에 결합하고 JSON 한 개와 exit 0/2를 반환한다.

`forge.ops.slack_transport`는 Task 16의 최종 work report도 재사용하는 유일한 Slack 전송 primitive다. 실행 사용자 홈의 `.codex/secrets/codex-work-report.env`를 기본 경로로 계산하고 override는 절대경로만 허용한다. env file은 `CODEX_WORK_REPORT_SLACK_BOT_TOKEN`, `CODEX_WORK_REPORT_SLACK_APP_NAME=codex work report`, `CODEX_WORK_REPORT_SLACK_APP_ID=A0BEQAZ1MS5`, `CODEX_WORK_REPORT_SLACK_CHANNEL=C0BES16KE1J` exact 값만 허용한다. 현재 token은 `bots.info`/`conversations.info`가 `missing_scope`이고 response scope가 exact `chat:write,chat:write.public`이므로 그 API를 요구하지 않는다. 대신 `auth.test`의 exact pinned token principal `team_id=T0AU5RA7XND`, `user_id=U0BEG5Y5CCB`, `bot_id=B0BELD3V84E`, `user=codex_work_report`와 response scope set을 검증한다. env app name의 공백 표기와 auth user의 underscore 표기를 서로 equal 비교하지 않는다. App ID는 독립 API 증명이 아니라 strict local metadata와 pinned token principal의 결합임을 receipt에 `identity_assurance=locally-pinned-principal`로 명시한다. channel 권한은 Phase 0의 host별 deterministic preflight `chat.postMessage` response가 exact `channel=C0BES16KE1J`와 non-empty `ts`를 반환하는 것으로 증명한다. token/header/raw response는 result, exception, stdout/stderr, receipt에 넣지 않는다. canonical `chat.postMessage` request bytes와 모든 non-empty secret value를 `scan_bytes`로 검사한 뒤에만 post한다.

각 `run`은 probe 전에 `canary-attempt-v1` journal을 state root에 원자 생성한다. `attempt_id`는 target/source SHA/start timestamp/random nonce를 결합한 불투명 ID이고, alert의 deterministic `client_msg_id`는 attempt ID와 canonical request digest로 만든다. 각 probe exception, timeout, name/type mismatch는 exception text를 노출하지 않고 `probe-error|probe-timeout|invalid-probe-result` failure로 변환해 alert까지 계속한다. post 직전 `pending` receipt를 flush/fsync/replace하고 성공한 exact channel/ts만 `sent`로 전이한다. API accept 직후 crash하면 marker는 계속 닫혀 있고 다음 invocation은 새 attempt를 만들기 전에 pending attempt를 같은 `client_msg_id`로 재개한다. Slack duplicate suppression 결과를 read-back해 visible alert 하나로 수렴한 뒤에만 prior attempt를 terminal로 만든다. 새로운 정기 canary attempt는 새 ID라서 같은 장애가 지속되어도 다음 주기의 새 alert를 억제하지 않는다. SIGINT/SIGTERM은 signal handler가 marker/dispatcher closed를 재확인하고 attempt를 `interrupted`로 fsync한 뒤 `canary-interrupted` pending alert를 bounded 전송하며, SIGKILL 뒤 next invocation은 같은 journal을 재개한다. `KeyboardInterrupt`/`SystemExit`를 일반 probe error로 삼키지 않는다. scan/identity/API/receipt 오류는 sanitized error code만 journal에 기록하고 canary red를 유지한다.

현재 실측상 Windows에는 source env가 있지만 ACL inheritance가 켜져 있고 sandbox group read가 상속되며, WSL `/home/immortal0900/.codex/secrets/codex-work-report.env`와 VPS `/home/ubuntu/.codex/secrets/codex-work-report.env`는 없다. `provision-slack-alert-secret.ps1`는 Windows source를 strict parse해 위 4개 key 외 항목, duplicate, blank token, CR/NUL을 거절하고 canonical LF bytes를 만든다. `-RepairWindowsAcl`은 source content를 바꾸지 않고 inheritance를 끈 protected ACL로 원자 교체해 current user, `SYSTEM`, local Administrators만 허용하고 exact rule set을 read-back한다. 이 switch 없이는 현재 ACL을 hard fail한다. Linux/VPS target은 credential bytes를 command line, environment, temp artifact, stdout/stderr에 넣지 않고 `wsl.exe`/`ssh` child의 stdin으로만 전달한다. remote helper는 parent directory 0700, temp file 0600, file+directory fsync, atomic replace를 수행하고 SHA-256/mode만 반환한다. local digest와 exact 일치하지 않으면 target file을 제거하고 실패한다. overwrite 전 기존 file이 있으면 bytes를 읽지 않고 digest만 비교하며 다른 digest는 explicit `-Rotate` 없이는 거절한다. rollout의 최초 provision은 absent-only이고 `-Rotate`를 사용하지 않는다.

Windows/Linux/VPS installer의 Services preflight는 서비스/Task mutation 전에 위 기본 env file의 존재, 일반 파일 여부, Windows protected exact ACL 또는 POSIX mode 0600, canonical digest, pinned `auth.test` identity/scope와 Phase 0 host-local preflight-post sent receipt를 해당 host에서 확인한다. credential 파일이 없거나 잘못되면 서비스/Task mutation 0회로 실패한다. Scheduled Task와 systemd unit은 동일 canary entrypoint를 호출하므로 별도 비보호 alert 경로를 만들지 않는다. credential lifecycle은 deployment receipt의 typed `slack_alert_env_sha256`에 digest만 기록하고 rollback은 preexisting credential을 삭제하거나 복원하지 않는다.

- [ ] **Step 7: Slack transport·crash convergence RED tests 작성**

`tests/ops/test_slack_transport.py`는 fake scanner/transport/filesystem clock으로 close→stop→gateway probe→request scan→`chat.postMessage` 순서를 고정한다. `test_probe_exception_timeout_or_invalid_result_still_alerts_and_stays_red`는 세 오류가 sanitized failure로 바뀌고 alert가 호출되는지 검증하며, signal journal/SIGKILL recovery도 별도 subprocess case로 고정한다. env/pinned auth identity/exact scope mismatch, missing/mode-or-ACL-invalid env, scan finding, `auth.test` failure, preflight-post wrong channel/empty ts, API error는 post 0회 또는 red를 보장한다. `bots.info`와 `conversations.info`가 호출되지 않는 것도 assert한다. API accept 직후 crash→same attempt retry는 같은 `client_msg_id`, visible message 1개, durable sent channel/ts를 검증한다. alert delivery 실패 뒤 marker write 0회와 dispatcher stopped, 새 scheduled attempt의 distinct alert ID도 검증한다. 모든 captured stdout/stderr/receipt에 raw secret이 없음을 assert한다. `tests/ops/test_slack_secret_provision.py`는 secret이 child stdin에만 존재하고 argv/env/log에는 없으며 absent-only install, mismatched existing refusal, mode 0600/dir 0700, digest mismatch cleanup, partial-write crash recovery, Windows protected exact ACL repair/read-back, 각 host의 deterministic preflight post exact-once를 fake WSL/SSH process로 고정한다.

- [ ] **Step 8: GREEN 확인**

Run: `.\.venv\Scripts\python.exe -m pytest tests/ops/test_dispatcher_supervisor.py tests/ops/test_canary.py tests/ops/test_slack_transport.py tests/ops/test_slack_secret_provision.py -q`

Expected: canary order, fail-closed alert, crash retry, secret non-disclosure cases가 모두 pass한다.

- [ ] **Step 9: commit**

```powershell
git add forge/ops/dispatcher_supervisor.py forge/ops/canary.py forge/ops/slack_transport.py forge/ops/canary_alert.py forge/ops/slack_secret_provision.py forge/scripts/dispatcher-supervisor.py forge/scripts/canary.py forge/scripts/post-canary-alert.py forge/scripts/provision-slack-alert-secret.ps1 forge/scripts/canary.sh tests/ops/test_dispatcher_supervisor.py tests/ops/test_canary.py tests/ops/test_slack_transport.py tests/ops/test_slack_secret_provision.py
git commit -m "feat: stop dispatcher when canary evidence is invalid"
```

### Task 5: fail-loud drift audit

**Files:**
- Modify: `forge/ops/contracts.py`
- Create: `forge/ops/drift_audit.py`
- Create: `forge/ops/drift_alert.py`
- Create: `forge/ops/ops_evidence.py`
- Modify: `forge/ops/slack_transport.py`
- Create: `forge/scripts/drift-audit.py`
- Create: `tests/ops/test_drift_audit.py`
- Create: `tests/ops/test_ops_evidence.py`
- Modify: `forge/scripts/drift-audit.sh`

**Consumes:**
- issue:card 1:1, forge label cardinality, protected card fields, receipt ledger, protocol/retry/GATE_ERROR events.
- canary/supervisor heartbeat, service/Task inventory, build/deployment hashes, DB permission/quick_check, backup/outbox/disk.

**Produces:**
- `InvariantObservation`
- `DriftReport`
- `audit_invariants(observations: Sequence[InvariantObservation]) -> DriftReport`
- `python -m forge.ops.drift_audit --target windows|linux|vps|all (--sha SHA40|--sha-from-build-manifest FILE) [--publish-ops-evidence --bootstrap-repository OWNER/REPO --bootstrap-issue-from-repo-variable]`
- `build_ops_evidence(INPUTS) -> OpsEvidence`, `publish_current_ops_evidence(INPUTS) -> CommentRecord`.
- `send_drift_alert(target: str, source_sha: str, report: DriftReport, state_root: Path, env_file: Path, transport: SlackTransport) -> AlertReceipt`.
- `python -m forge.ops.ops_evidence preflight-publisher --bootstrap-repository OWNER/REPO --bootstrap-issue-from-repo-variable --require-wsl-target linux --require-vps-target vps --output FILE`.
- PASS exit 0, invariant violation exit 1, inspection `GATE_ERROR` exit 2.

**Interfaces:**

```python
from dataclasses import dataclass
from enum import Enum
from typing import Sequence

class ObservationKind(str, Enum):
    PASS = "PASS"
    VIOLATION = "VIOLATION"
    GATE_ERROR = "GATE_ERROR"

@dataclass(frozen=True)
class InvariantObservation:
    name: str
    kind: ObservationKind
    detail: str

@dataclass(frozen=True)
class DriftReport:
    kind: ObservationKind
    exit_code: int
    violations: Sequence[str]
    gate_errors: Sequence[str]
```

- [ ] **Step 1: inspection 오류가 green으로 축소되지 않는 RED test 작성**

```python
from forge.ops.drift_audit import (
    InvariantObservation,
    ObservationKind,
    audit_invariants,
    build_parser,
)


def test_gate_error_has_precedence_over_counted_violations() -> None:
    report = audit_invariants(
        (
            InvariantObservation(
                "receipt-ledger",
                ObservationKind.VIOLATION,
                "one done task has no consumed receipt",
            ),
            InvariantObservation(
                "github-pagination",
                ObservationKind.GATE_ERROR,
                "page 2 returned HTTP 502",
            ),
        )
    )

    assert report.kind is ObservationKind.GATE_ERROR
    assert report.exit_code == 2
    assert report.violations == ("receipt-ledger",)
    assert report.gate_errors == ("github-pagination",)
    parsed = build_parser().parse_args(
        ["--target", "all", "--sha", "1" * 40]
    )
    assert parsed.target == "all"
    assert parsed.sha == "1" * 40


def test_scheduled_parser_derives_sha_and_enables_windows_publisher() -> None:
    parsed = build_parser().parse_args(
        [
            "--target",
            "all",
            "--sha-from-build-manifest",
            "C:/release/build-manifest.json",
            "--publish-ops-evidence",
            "--bootstrap-repository",
            "example/infinity-forge",
            "--bootstrap-issue-from-repo-variable",
        ]
    )
    assert parsed.sha is None
    assert parsed.publish_ops_evidence is True
    assert parsed.bootstrap_repository == "example/infinity-forge"
```

`tests/ops/test_ops_evidence.py`는 fake Windows/WSL/VPS transport와 paginated GitHub client로 exact-three-target happy path를 검증하고, `deployment_lock_held`, `marker_closed`, `mixed_sha`, `stale_canary`, `stale_drift`, `missing_credential`, `missing_bootstrap_repository`, `ops_host_false`, `configured_repository_mismatch`, `duplicate_marker`, `scan_finding`, `transport_error`, `deployment_started_between_read_and_publish`를 각각 주입한다. 모든 failure에서 comment create/update call count가 0이고 local drift state만 atomic GATE_ERROR로 갱신되는지 assert한다. same-SHA 두 hourly cycle은 comment 1개를 유지하며 timestamp/evidence digest만 갱신한다. fake GitHub client는 모든 variable/issue/comment 호출이 parser에서 canonicalize한 exact `OWNER/REPO`를 명시적으로 받는지 assert하며 ambient `.git`, current directory, `GH_REPO`에는 의존하지 않는다.

`tests/ops/test_drift_audit.py`에는 `test_first_violation_or_gate_error_posts_crash_safe_slack_alert_on_each_target`, `test_same_open_incident_and_process_retry_do_not_duplicate_alert`, `test_pass_closes_incident_and_recurrence_posts_new_alert`, `test_alert_transport_failure_escalates_violation_to_gate_error_without_green`을 추가한다. fake shared transport는 실제 canonical request scan 뒤에만 호출돼야 하며 Windows/Linux/VPS target을 parametrized한다.

- [ ] **Step 2: RED 확인**

Run: `.\.venv\Scripts\python.exe -m pytest tests/ops/test_drift_audit.py -q`

Expected: `ModuleNotFoundError: No module named 'forge.ops.drift_audit'`로 FAIL.

- [ ] **Step 3: audit aggregator 최소 구현**

```python
from collections.abc import Sequence

from forge.ops.contracts import (
    DriftReport,
    InvariantObservation,
    ObservationKind,
)


def audit_invariants(
    observations: Sequence[InvariantObservation],
) -> DriftReport:
    violations = tuple(
        item.name
        for item in observations
        if item.kind is ObservationKind.VIOLATION
    )
    gate_errors = tuple(
        item.name
        for item in observations
        if item.kind is ObservationKind.GATE_ERROR
    )
    if gate_errors:
        return DriftReport(
            ObservationKind.GATE_ERROR,
            2,
            violations,
            gate_errors,
        )
    if violations:
        return DriftReport(
            ObservationKind.VIOLATION,
            1,
            violations,
            (),
        )
    return DriftReport(ObservationKind.PASS, 0, (), ())
```

- [ ] **Step 4: 모든 required provider 연결**

`forge/scripts/drift-audit.py`는 각 provider를 독립 호출해 observation을 만든다. module `build_parser()/main()`은 manual rollout의 `--target TARGET --sha SHA40`과 scheduled exact `--target TARGET --sha-from-build-manifest FILE`을 mutually exclusive required input으로 받고 target별 current receipt와 service inventory가 SHA에 결합됐는지 검사한다. publisher mode에서는 canonical bootstrap host repository인 exact `--bootstrap-repository OWNER/REPO`도 required이며 owner/repo를 GitHub 규칙으로 검증한다. SQLite는 URI `mode=ro`와 `PRAGMA query_only=ON`으로 읽고 expected schema가 없으면 GATE_ERROR다. GitHub는 전 page를 읽는다. service inspection은 Linux에서 `systemctl --user show`, Windows에서 `Get-ScheduledTask` JSON bridge를 사용한다. 최근 60분 GATE_ERROR가 3건 이상이거나 최소 5회 중 20% 초과면 violation이다.

모든 target은 audit 결과가 `VIOLATION` 또는 `GATE_ERROR`이면 Task 4의 동일 `forge.ops.slack_transport`로 즉시 Slack alert를 보낸다. incident key는 target+source SHA+kind+sorted violation/gate-error name canonical digest이고 `drift-alert-incident-v1` journal을 state root에 원자 기록한다. open incident의 process/API-accept crash retry는 같은 deterministic `client_msg_id`와 pending/sent receipt를 재사용해 visible alert 하나로 수렴한다. 동일 incident가 계속되는 동안 재알림을 만들지 않고 PASS가 terminal-resolved journal을 쓴 뒤 같은 문제가 재발하면 generation을 올려 새 alert를 보낸다. detail/exception/secret은 request나 receipt에 넣지 않고 check name과 sanitized kind만 보낸다. alert scan/identity/transport/receipt 실패는 기존 VIOLATION을 `GATE_ERROR` exit 2로 승격하고, 기존 GATE_ERROR도 exit 2를 유지하며 stale PASS를 쓰지 않는다. Windows의 GitHub evidence publish와 Slack alert는 독립 side effect다. Slack 실패 때문에 GitHub 상태를 green으로 만들거나 POSIX local report를 누락하지 않는다.

Windows hourly scheduled invocation만 `--target all --publish-ops-evidence --bootstrap-repository OWNER/REPO --bootstrap-issue-from-repo-variable`를 추가한다. `OWNER/REPO`는 bootstrap issue가 실제 존재하는 단일 canonical host repository이며 installer preflight에서 확정해 immutable Task argv에 고정한다. publisher는 deployment lock을 nonblocking/fail-loud acquire하고 marker open, 세 target receipt/activation same SHA/build digest, canary 7시간(25200초) 이내, drift 2시간 이내를 확인한다. lock held, marker closed, mixed SHA, missing `FORGE_BOOTSTRAP_ISSUE`, missing/invalid bootstrap repository, `gh auth status` 실패, duplicate same-SHA marker, scan finding, transport 오류면 GitHub write 0회·exit 2다. 모든 `gh variable`과 `gh api` 호출은 `--repo OWNER/REPO` 또는 `repos/{owner}/{repo}` API path를 명시하고 모든 issue comment page를 읽는다. canonical `forge-ops-evidence-v1` request를 실제 credential value와 함께 scan한 뒤 exact-one create/update한다. publish 직전과 직후에도 deployment lock/marker/SHA를 재검증해 deploy/rollback과 race하지 않는다. POSIX scheduled drift는 local target만 audit하고 publish flag를 사용하지 않는다.

`preflight-publisher`는 `gh auth status`, `gh repo view OWNER/REPO --json nameWithOwner`, `gh variable get FORGE_OPS_HOST --repo OWNER/REPO`, `gh variable get FORGE_BOOTSTRAP_REPOSITORY --repo OWNER/REPO`, `gh variable get FORGE_BOOTSTRAP_ISSUE --repo OWNER/REPO`, `gh api repos/{owner}/{repo}/issues/{number}`를 exact repository context로 실행한다. ops host는 exact lowercase `true`, configured bootstrap repository는 argv `OWNER/REPO`와 exact-equal이어야 한다. 이어 configured WSL/VPS transport에 read-only receipt/activation probe를 수행한다. 모든 remote/read-only check가 성공하기 전에는 output parent를 만들거나 쓰지 않는다. stdout/stderr와 output JSON에는 token 또는 credential helper 응답을 넣지 않는다. 성공 output은 exact `schema_version,bootstrap_repository,bootstrap_issue,checked_at_utc,targets`만 가진 canonical JSON이며 installer는 이를 temp→flush→fsync→replace→directory fsync로 `%LOCALAPPDATA%\InfinityForge\state\publisher-config.json`에 둔다. Task argv의 repository와 config repository가 다르면 publisher는 GitHub write 없이 exit 2다.

- [ ] **Step 5: GREEN과 전체 ops 회귀 확인**

Run: `.\.venv\Scripts\python.exe -m pytest tests/ops/test_drift_audit.py tests/ops/test_ops_evidence.py tests/ops/test_dispatcher_supervisor.py tests/ops/test_canary.py tests/ops/test_slack_transport.py -q`

Expected: `3 passed`.

- [ ] **Step 6: commit**

```powershell
git add forge/ops/drift_audit.py forge/ops/drift_alert.py forge/ops/ops_evidence.py forge/ops/slack_transport.py forge/scripts/drift-audit.py forge/scripts/drift-audit.sh tests/ops/test_drift_audit.py tests/ops/test_ops_evidence.py
git commit -m "feat: report Forge operational drift without silent fallback"
```

### Task 6: checked-in systemd·Windows Task와 repository hook installer

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
- Create: `forge/hermes/uv-bootstrap-linux.lock`
- Create: `tests/ops/test_install_contracts.py`
- Create: `tests/ops/test_hermes_bootstrap.py`

**Consumes:**
- Task 2~5의 checked-in Python/shell entrypoints.
- umbrella Task 7의 `forge/scripts/install-codex-hook.py`와 trusted `forge-guard.pyz`.
- installer CLI에 반복 전달된 repository absolute path 목록.
- umbrella/completion/rollout이 승인한 Hermes upstream base commit `4281151ae859241351ba14d8c7682dc67ff4c126`과 expected CLI version `Hermes Agent v0.18.2 (2026.7.7.2)`.

**Produces:**
- systemd user services/timers with explicit interpreter.
- Linux installer interface:
  `install-linux.sh --phase hermes|hooks|services --target linux|vps --release PATH --manifest PATH [repeatable --repo PATH] [--snapshot-index PATH --snapshot-sha256 HASH]`.
- Linux verifier interface:
  `verify-linux-install.sh --target linux|vps --release PATH --manifest PATH [repeatable --repo PATH]`.
- Windows installer interface:
  `install-windows.ps1 -Phase Hooks|Services -ReleasePath PATH -Manifest PATH -RepoPaths PATHS -PythonPath PATH [-BootstrapRepository OWNER/REPO] [-PlanOnly]` (`Services`에서는 `-BootstrapRepository` required, `Hooks`에서는 금지).
- Hermes bootstrap/ref interface:
  `hermes-bootstrap.py recover|begin|advance|complete --record RECORD COMMAND_ARGS`와 `hermes-bootstrap.py ensure-approved-base --root ROOT --commit SHA40 --remote origin`; `begin`은 authorization digest/source/root/target을 고정하고, `advance`는 expected stage CAS로만 전이하며, `recover`는 미완료 journal이 소유한 경로만 제거한다. `ensure-approved-base`는 pinned object를 exact fetch할 수 있지만 HEAD/index/target을 바꾸지 않고 ref를 zero-OID create 또는 exact reuse만 한다.
- 각 repo의 `.codex/hooks.json`, enabled service/Task inventory, linger/reboot evidence.
- ambient `uv`와 분리된 trusted bootstrap venv, `<hermes-root>/venv`, `~/.hermes/kanban.db` mode 0600.

**Interfaces:**

- Linux `hermes` phase는 기존 설치에서 HEAD/version/DB를 read-only 검증하고, 설치가 없을 때만 finalized snapshot의 `hermes_installation=absent` authorization을 확인한 뒤 bootstrap `uv 0.11.24`, locked sync, `hermes kanban init`, DB mode 0600을 생성한다. clean bootstrap은 producer pause·drain·snapshot finalization 뒤, patch 전 단계에서만 실행한다.
- Linux `hooks` phase exit 0은 전달된 모든 repo hook install/verify를, `services` phase exit 0은 target별 exact unit inventory·scripts 0755·`Linger=yes`·enable/start를 뜻한다. 서로 다른 phase의 mutation을 섞지 않는다.
- Windows `Hooks` phase exit 0은 모든 repo hook install/verify를, `Services` phase exit 0은 current-user-only ACL, canonical bootstrap repository preflight, 세 Scheduled Task 등록을 뜻한다. 서로 다른 phase의 mutation을 섞지 않는다.
- `-PlanOnly`는 filesystem, Task Scheduler, hook file을 변경하지 않고 planned task/action/repo JSON만 stdout에 쓴다.

**Service command map:**

| Service | Exact ExecStart |
|---|---|
| dispatcher | `/usr/bin/python3 %h/.local/share/infinity-forge/current/forge/scripts/dispatcher-supervisor.py` |
| canary | `/usr/bin/python3 %h/.local/share/infinity-forge/current/forge/scripts/canary.py --mode run --target ${FORGE_TARGET} --sha-from-build-manifest %h/.local/share/infinity-forge/current/build-manifest.json` |
| drift | `/usr/bin/python3 %h/.local/share/infinity-forge/current/forge/scripts/drift-audit.py --target ${FORGE_TARGET} --sha-from-build-manifest %h/.local/share/infinity-forge/current/build-manifest.json` |
| mirror | `/usr/bin/python3 %h/.local/share/infinity-forge/current/forge/scripts/label-mirror.py` |
| spec-coverage | `/usr/bin/python3 %h/.local/share/infinity-forge/current/forge/scripts/spec-coverage.py` |
| ledger | `/usr/bin/python3 %h/.local/share/infinity-forge/current/forge/scripts/ledger-emit.py` |
| flush-outbox | `/usr/bin/python3 %h/.local/share/infinity-forge/current/forge/scripts/flush-outbox.py` |
| morning-report | `/usr/bin/bash %h/.local/share/infinity-forge/current/forge/scripts/morning-report.sh` |
| backup | `/usr/bin/bash %h/.local/share/infinity-forge/current/forge/scripts/nightly-backup.sh` |

- [ ] **Step 1: missing-uv bootstrap, interpreter, linger, hook, Windows Task 계약 RED test 작성**

```python
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_linux_units_use_interpreters_and_installer_enforces_linger_hooks() -> None:
    expected = {
        "forge-dispatcher.service": "/usr/bin/python3 ",
        "forge-canary.service": "/usr/bin/python3 ",
        "forge-drift.service": "/usr/bin/python3 ",
        "forge-mirror.service": "/usr/bin/python3 ",
        "forge-spec-coverage.service": "/usr/bin/python3 ",
        "forge-ledger.service": "/usr/bin/python3 ",
        "forge-flush-outbox.service": "/usr/bin/python3 ",
        "forge-morning-report.service": "/usr/bin/bash ",
        "forge-backup.service": "/usr/bin/bash ",
    }
    for name, interpreter in expected.items():
        text = (ROOT / "forge" / "systemd" / name).read_text(encoding="utf-8")
        assert f"ExecStart={interpreter}" in text
        assert "/current/forge/scripts/" in text
        assert "WorkingDirectory=%h/.local/share/infinity-forge/current" in text
        if interpreter == "/usr/bin/python3 ":
            assert "Environment=PYTHONPATH=%h/.local/share/infinity-forge/current" in text

    canary_unit = (ROOT / "forge/systemd/forge-canary.service").read_text(encoding="utf-8")
    drift_unit = (ROOT / "forge/systemd/forge-drift.service").read_text(encoding="utf-8")
    for text in (canary_unit, drift_unit):
        assert "EnvironmentFile=%h/.config/infinity-forge/target.env" in text
        assert "--target ${FORGE_TARGET}" in text
        assert "--sha-from-build-manifest %h/.local/share/infinity-forge/current/build-manifest.json" in text

    installer = (
        ROOT / "forge" / "scripts" / "install-linux.sh"
    ).read_text(encoding="utf-8")
    assert '--phase) phase="$2"' in installer
    assert '--target) target="$2"' in installer
    assert '^(hermes|hooks|services)$' in installer
    assert '"$target" == "linux" || "$target" == "vps"' in installer
    assert 'chmod 0755 "$release/forge/scripts/"*.py' in installer
    assert 'chmod 0755 "$release/forge/scripts/"*.sh' in installer
    assert 'sudo -n loginctl enable-linger "$USER"' in installer
    assert '"$release/forge/scripts/install-codex-hook.py"' in installer
    assert 'PYTHONPATH="$release" /usr/bin/python3' in installer
    assert 'for repo in "${repos[@]}"' in installer
    assert 'if [[ "$phase" == "hooks" ]]' in installer
    assert 'if [[ "$phase" == "hermes" ]]' in installer
    assert "authorize-clean-hermes-bootstrap" in installer
    assert 'if [[ -d "$hermes_root/.git" ]]' in installer
    assert '"$release/forge/scripts/hermes-patch.py" status' in installer
    assert '"$release/forge/scripts/hermes-patch.py" recover' in installer
    assert 'snapshot authorization is valid only for hermes phase' in installer
    assert '--snapshot-sha256 "$snapshot_sha256"' in installer
    assert 'if [[ "$target" == "vps" ]]' in installer
    assert 'vps_only_units=(' in installer
    assert 'printf "FORGE_TARGET=%s\\n" "$target"' in installer
    assert 'target_env="$HOME/.config/infinity-forge/target.env"' in installer
    assert 'chmod 0600 "$target_env"' in installer
    assert 'load_state="$(systemctl --user show "$unit"' in installer
    assert 'loaded) systemctl --user disable --now "$unit"' in installer
    assert "|| true" not in installer


def test_linux_installer_bootstraps_hermes_without_ambient_uv() -> None:
    installer = (
        ROOT / "forge" / "scripts" / "install-linux.sh"
    ).read_text(encoding="utf-8")
    lock = (
        ROOT / "forge" / "hermes" / "uv-bootstrap-linux.lock"
    ).read_text(encoding="utf-8")
    bootstrap = (
        ROOT / "forge" / "scripts" / "hermes-bootstrap.py"
    ).read_text(encoding="utf-8")

    assert lock.splitlines()[0].startswith("uv==0.11.24")
    assert lock.count("--hash=sha256:") == 3
    assert set(re.findall(r"sha256:([0-9a-f]{64})", lock)) == {
        "e7e78c18686202c8b8715bebb83bfaf58f82d7fb848b6a5ae4e925a9fac3de4c",
        "6ecdad43e870f88d3772d9d37e877259ae35ec374d51589805cdcf6196205829",
        "48a6123f71b801e0e0b8a38520b011632ad81e0a043445044ce5b1a7b1cec7b6",
    }
    assert 'if [[ ! -x "$uv_bin" ]]' in installer
    assert "/usr/bin/python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'" in installer
    assert '/usr/bin/python3 -m venv "$uv_bootstrap"' in installer
    assert '"$uv_bootstrap/bin/python" -m pip install' in installer
    assert "--require-hashes" in installer
    assert "--only-binary=:all:" in installer
    assert "command -v uv" not in installer
    assert 'UV_PROJECT_ENVIRONMENT="$hermes_root/venv"' in installer
    assert 'sync --extra all --locked' in installer
    assert '"$hermes_root/venv/bin/hermes" kanban init' in installer
    assert "4281151ae859241351ba14d8c7682dc67ff4c126" in installer
    assert 'git -C "$hermes_root" fetch --depth 1 origin "$hermes_base_commit"' in installer
    assert 'git -C "$hermes_root" checkout --detach FETCH_HEAD' in installer
    assert '"$release/forge/scripts/hermes-bootstrap.py" ensure-approved-base' in installer
    assert "refs/infinity-forge/approved-base" in bootstrap
    assert "0000000000000000000000000000000000000000" in bootstrap
    assert "Hermes Agent v0.18.2 (2026.7.7.2)" in installer
    assert installer.count(
        '[[ "$hermes_version" == "Hermes Agent v0.18.2 (2026.7.7.2)"* ]]'
    ) == 2
    assert 'chmod 0600 "$kanban_db"' in installer
    assert 'stat -c "%a" "$kanban_db"' in installer


def test_windows_installer_registers_tasks_and_installs_every_repo_hook() -> None:
    text = (
        ROOT / "forge" / "scripts" / "install-windows.ps1"
    ).read_text(encoding="utf-8")
    assert "[string[]]$RepoPaths" in text
    assert "[string]$PythonPath" in text
    assert '[ValidateSet("Hooks","Services")]' in text
    assert '[string]$Phase' in text
    assert 'if ($Phase -eq "Hooks")' in text
    assert 'if ($Phase -ne "Services")' in text
    assert "foreach ($RepoPath in $RepoPaths)" in text
    assert "install-codex-hook.py" in text
    assert "$env:PYTHONPATH = $ReleasePath" in text
    assert "-WorkingDirectory $ReleasePath" in text
    assert "runpy.run_path" in text
    assert "sys.path.insert(0,sys.argv[1])" in text
    assert "sys.version_info < (3, 11)" in text
    assert '$TaskPath = "\\INFINITY_FORGE\\"' in text
    assert 'Name = "Dispatcher"' in text
    assert 'Name = "Canary"' in text
    assert 'Name = "Drift"' in text
    assert '[string]$BootstrapRepository' in text
    assert '"--mode", "run", "--target", "windows"' in text
    assert '"--target", "all", "--sha-from-build-manifest"' in text
    assert '"--publish-ops-evidence", "--bootstrap-repository", $BootstrapRepository' in text
    assert '"--bootstrap-issue-from-repo-variable"' in text
    assert "sys.argv=[sys.argv[2],*sys.argv[3:]]" in text
    assert "Arguments = @()" in text
    assert "preflight-publisher" in text
    assert text.rindex("$env:PYTHONPATH = $ReleasePath") < text.index(
        "-m forge.ops.ops_evidence preflight-publisher"
    )
    assert "finally {\n  $env:PYTHONPATH = $PreviousPythonPath" in text
    assert "IgnoreNew" in text
    assert "-LogonType Interactive" in text
    assert "Startup VBS" not in text
```

`tests/ops/test_hermes_bootstrap.py`의 RED suite는 `begin` 직후, clone 직후, `uv sync` 직후, DB 생성 직후 process failure를 각각 주입한다. 별도 subprocess에는 SIGINT/SIGTERM을 주입해 각각 exit 130/143이고 recovery 뒤 다음 stage mutation 호출이 0회인지 검증한다. `recover`가 journal의 `owned_paths` exact set만 역순 제거하고 unrelated sibling, preexisting parent, symlink/reparse point를 건드리지 않는지 검사한다. `stage=complete` record는 자동 삭제하지 않고 exact target/root/source/snapshot digest가 맞을 때만 receipt binding에 사용할 수 있어야 한다. record byte tamper, stage CAS mismatch, 다른 source SHA 재사용은 exit 2다. clean rollback 뒤 `stage=rolled-back` immutable archive를 남긴 상태에서 새 source의 `begin`은 새 active record를 원자 생성할 수 있어야 한다.

같은 suite는 disposable Windows HEAD `540f90190f50f9518bf36632a724e0e58877a10b`와 VPS HEAD `73b611ad19720d70308dad6b0fb64648aaadc216` fixture에서 approved ref가 absent인 상태를 만든다. `ensure-approved-base` 후 ref만 pinned SHA로 생기고 HEAD/index bytes/mtime/target/porcelain이 불변이며 completion check가 각각 manifest의 supported blob을 선택해야 한다. 두 HEAD 모두 approved commit ancestry가 아니어도 통과한다. existing ref가 다른 SHA면 fetch/ref/HEAD/index/target mutation 0회로 실패하고, rollback fixture는 pre-state absent ref를 제거한다.

`tests/ops/test_install_contracts.py`는 linger command runner fake로 두 경계를 추가한다. `Linger=yes`/passwordless sudo 없음에서는 sudo 호출 0회로 services phase가 통과하고, `Linger=no`/`sudo -n` 거절에서는 unit copy·daemon-reload·enable/start 호출 0회로 exit 2여야 한다. Windows preflight test는 cwd를 empty temp directory, ambient `PYTHONPATH`를 empty로 두어도 release path를 명시적으로 주입해 `forge.ops.ops_evidence`가 import되고, 종료 후 원래 environment가 복원되는지 검증한다.

- [ ] **Step 2: RED 확인**

Run: `.\.venv\Scripts\python.exe -m pytest tests/ops/test_install_contracts.py tests/ops/test_hermes_bootstrap.py -q`

Expected: 첫 missing systemd file에서 `FileNotFoundError`로 FAIL.

- [ ] **Step 3: checked-in service와 timer 작성**

`forge-dispatcher.service`의 전체 내용은 다음과 같다. 일반 service는 위 command map의 interpreter/entrypoint를 exact하게 사용하고 oneshot job은 `Type=oneshot`, supervisor는 `Restart=always`와 `RestartSec=5`를 사용한다.

```ini
[Unit]
Description=INFINITY_FORGE independent Hermes dispatcher supervisor
After=network-online.target hermes-gateway.service
Wants=network-online.target

[Service]
Type=simple
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH=%h/.local/share/infinity-forge/current
WorkingDirectory=%h/.local/share/infinity-forge/current
ExecStart=/usr/bin/python3 %h/.local/share/infinity-forge/current/forge/scripts/dispatcher-supervisor.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

`forge-canary.service`와 `forge-drift.service`는 parser-required argv가 빠지지 않도록 다음 `[Service]` 계약을 각각 그대로 사용한다. `${FORGE_TARGET}`은 shell 확장이 아니라 systemd environment substitution이며 installer가 만든 target-specific file에서만 온다.

```ini
# forge-canary.service
[Service]
Type=oneshot
Environment=PYTHONPATH=%h/.local/share/infinity-forge/current
EnvironmentFile=%h/.config/infinity-forge/target.env
WorkingDirectory=%h/.local/share/infinity-forge/current
ExecStart=/usr/bin/python3 %h/.local/share/infinity-forge/current/forge/scripts/canary.py --mode run --target ${FORGE_TARGET} --sha-from-build-manifest %h/.local/share/infinity-forge/current/build-manifest.json
```

```ini
# forge-drift.service
[Service]
Type=oneshot
Environment=PYTHONPATH=%h/.local/share/infinity-forge/current
EnvironmentFile=%h/.config/infinity-forge/target.env
WorkingDirectory=%h/.local/share/infinity-forge/current
ExecStart=/usr/bin/python3 %h/.local/share/infinity-forge/current/forge/scripts/drift-audit.py --target ${FORGE_TARGET} --sha-from-build-manifest %h/.local/share/infinity-forge/current/build-manifest.json
```

Timer schedule은 canary `OnUnitActiveSec=6h`와 `OnCalendar=*-*-* 21:00:00 Asia/Seoul`, drift `OnCalendar=hourly`, mirror `OnUnitActiveSec=2m`, spec coverage `OnCalendar=*-*-* 21:02:00 Asia/Seoul` 및 `OnCalendar=*-*-* 07:30:00 Asia/Seoul`, ledger `OnUnitActiveSec=10m`, morning `OnCalendar=*-*-* 07:30:00 Asia/Seoul`, backup `OnCalendar=*-*-* 04:30:00 Asia/Seoul`로 고정한다.

- [ ] **Step 4: Linux installer의 hash-locked Hermes bootstrap·executable·linger·hook loop 구현**

`forge/hermes/uv-bootstrap-linux.lock`은 Linux x86_64/aarch64 wheel만 허용하며 exact content는 다음과 같다. sdist fallback은 installer의 `--only-binary=:all:`로 차단한다.

```text
uv==0.11.24 \
    --hash=sha256:e7e78c18686202c8b8715bebb83bfaf58f82d7fb848b6a5ae4e925a9fac3de4c \
    --hash=sha256:6ecdad43e870f88d3772d9d37e877259ae35ec374d51589805cdcf6196205829 \
    --hash=sha256:48a6123f71b801e0e0b8a38520b011632ad81e0a043445044ce5b1a7b1cec7b6
```

```bash
#!/usr/bin/env bash
set -euo pipefail

phase=""
target=""
release=""
manifest=""
snapshot_index=""
snapshot_sha256=""
repos=()
while (($#)); do
  case "$1" in
    --phase) phase="$2"; shift 2 ;;
    --target) target="$2"; shift 2 ;;
    --release) release="$2"; shift 2 ;;
    --manifest) manifest="$2"; shift 2 ;;
    --snapshot-index) snapshot_index="$2"; shift 2 ;;
    --snapshot-sha256) snapshot_sha256="$2"; shift 2 ;;
    --repo) repos+=("$2"); shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ "$phase" =~ ^(hermes|hooks|services)$ ]] || {
  echo "--phase must be hermes, hooks, or services" >&2; exit 2;
}
[[ "$target" == "linux" || "$target" == "vps" ]] || {
  echo "--target must be linux or vps" >&2; exit 2;
}
[[ -d "$release" && -f "$manifest" ]] || {
  echo "release and manifest are required" >&2; exit 2;
}
if [[ "$phase" == "hooks" && ${#repos[@]} -eq 0 ]]; then
  echo "hooks phase requires at least one --repo" >&2; exit 2
fi
if [[ "$phase" != "hooks" && ${#repos[@]} -ne 0 ]]; then
  echo "--repo is valid only for hooks phase" >&2; exit 2
fi
if [[ "$phase" != "hermes" && ( -n "$snapshot_index" || -n "$snapshot_sha256" ) ]]; then
  echo "snapshot authorization is valid only for hermes phase" >&2; exit 2
fi

hermes_repo="https://github.com/NousResearch/hermes-agent.git"
hermes_base_commit="4281151ae859241351ba14d8c7682dc67ff4c126"
hermes_root="$HOME/.hermes/hermes-agent"
kanban_db="$HOME/.hermes/kanban.db"
uv_lock="$release/forge/hermes/uv-bootstrap-linux.lock"
uv_bootstrap="${XDG_DATA_HOME:-$HOME/.local/share}/infinity-forge/bootstrap/uv-0.11.24"
uv_bin="$uv_bootstrap/bin/uv"
bootstrap_record="${XDG_STATE_HOME:-$HOME/.local/state}/infinity-forge/deployments/hermes-bootstrap-$target.json"

/usr/bin/python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' || {
  echo "Python 3.11+ is required" >&2
  exit 2
}

if [[ "$phase" == "hermes" ]]; then
  PYTHONPATH="$release" /usr/bin/python3 \
    "$release/forge/scripts/hermes-bootstrap.py" recover \
    --record "$bootstrap_record"
  if [[ -d "$hermes_root/.git" ]]; then
    PYTHONPATH="$release" /usr/bin/python3 \
      "$release/forge/scripts/hermes-bootstrap.py" ensure-approved-base \
      --root "$hermes_root" --commit "$hermes_base_commit" --remote origin
    patch_manifest="$release/forge/patches/hermes/0.18.2/manifest.json"
    patch_record="${XDG_STATE_HOME:-$HOME/.local/state}/infinity-forge/deployments/hermes-patch-$target.json"
    PYTHONPATH="$release" /usr/bin/python3 \
      "$release/forge/scripts/hermes-patch.py" recover \
      --root "$hermes_root" --manifest "$patch_manifest" --record "$patch_record"
    PYTHONPATH="$release" /usr/bin/python3 \
      "$release/forge/scripts/hermes-patch.py" status \
      --root "$hermes_root" --manifest "$patch_manifest" --record "$patch_record"
    [[ -x "$hermes_root/venv/bin/hermes" ]] || {
      echo "existing Hermes runtime is incomplete" >&2; exit 2;
    }
    hermes_version="$("$hermes_root/venv/bin/hermes" --version | head -n 1)"
    [[ "$hermes_version" == "Hermes Agent v0.18.2 (2026.7.7.2)"* ]] || {
      echo "Hermes CLI version mismatch" >&2; exit 2;
    }
    [[ -f "$kanban_db" && "$(stat -c "%a" "$kanban_db")" == "600" ]] || {
      echo "existing kanban DB or mode mismatch" >&2; exit 2;
    }
    /usr/bin/python3 - "$kanban_db" <<'PY'
import sqlite3
import sys

with sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True) as connection:
    if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
        raise SystemExit(2)
PY
    exit 0
  fi
  [[ -f "${snapshot_index:-}" && "$snapshot_sha256" =~ ^[0-9a-f]{64}$ ]] || {
    echo "clean Hermes bootstrap requires snapshot path and digest" >&2; exit 2;
  }
  PYTHONPATH="$release" /usr/bin/python3 -m forge.ops.deployment \
    authorize-clean-hermes-bootstrap --target "$target" \
    --snapshot-index "$snapshot_index" --snapshot-sha256 "$snapshot_sha256" \
    --hermes-root "$hermes_root"
  source_sha="$(/usr/bin/python3 - "$manifest" <<'PY'
import json
import re
import sys

value = json.load(open(sys.argv[1], encoding="utf-8"))["source_sha"]
if re.fullmatch(r"[0-9a-f]{40}", value) is None:
    raise SystemExit(2)
print(value)
PY
)"
  owned_args=(--owned-path "$hermes_root")
  [[ -e "$uv_bootstrap" ]] || owned_args+=(--owned-path "$uv_bootstrap")
  [[ -e "$kanban_db" ]] || owned_args+=(--owned-path "$kanban_db")
  PYTHONPATH="$release" /usr/bin/python3 \
    "$release/forge/scripts/hermes-bootstrap.py" begin \
    --record "$bootstrap_record" --target "$target" --root "$hermes_root" \
    --snapshot-index "$snapshot_index" --snapshot-sha256 "$snapshot_sha256" \
    --source-sha "$source_sha" "${owned_args[@]}"
  bootstrap_recover() {
    PYTHONPATH="$release" /usr/bin/python3 \
      "$release/forge/scripts/hermes-bootstrap.py" recover \
      --record "$bootstrap_record"
  }
  bootstrap_fail() {
    local rc=$?
    trap - ERR INT TERM
    bootstrap_recover
    exit "$rc"
  }
  bootstrap_interrupt() {
    trap - ERR INT TERM
    bootstrap_recover
    exit 130
  }
  bootstrap_terminate() {
    trap - ERR INT TERM
    bootstrap_recover
    exit 143
  }
  trap bootstrap_fail ERR
  trap bootstrap_interrupt INT
  trap bootstrap_terminate TERM
  if [[ ! -x "$uv_bin" ]]; then
    install -d -m 0755 "$(dirname "$uv_bootstrap")"
    /usr/bin/python3 -m venv "$uv_bootstrap"
    "$uv_bootstrap/bin/python" -m pip install --require-hashes \
      --only-binary=:all: -r "$uv_lock"
  fi
  [[ "$("$uv_bin" --version)" == "uv 0.11.24"* ]] || {
    echo "trusted uv version mismatch" >&2; exit 2;
  }
  install -d -m 0700 "$(dirname "$hermes_root")"
  git clone --filter=blob:none --no-checkout "$hermes_repo" "$hermes_root"
  git -C "$hermes_root" fetch --depth 1 origin "$hermes_base_commit"
  git -C "$hermes_root" checkout --detach FETCH_HEAD
  PYTHONPATH="$release" /usr/bin/python3 \
    "$release/forge/scripts/hermes-bootstrap.py" ensure-approved-base \
    --root "$hermes_root" --commit "$hermes_base_commit" --remote origin
  [[ "$(git -C "$hermes_root" rev-parse HEAD)" == "$hermes_base_commit" ]] || {
    echo "Hermes checkout commit mismatch" >&2; exit 2;
  }
  PYTHONPATH="$release" /usr/bin/python3 \
    "$release/forge/scripts/hermes-bootstrap.py" advance \
    --record "$bootstrap_record" --expected-stage authorized --next-stage checkout
  (
    cd "$hermes_root"
    UV_PROJECT_ENVIRONMENT="$hermes_root/venv" \
      "$uv_bin" sync --extra all --locked
  )
  PYTHONPATH="$release" /usr/bin/python3 \
    "$release/forge/scripts/hermes-bootstrap.py" advance \
    --record "$bootstrap_record" --expected-stage checkout --next-stage runtime
  hermes_version="$("$hermes_root/venv/bin/hermes" --version | head -n 1)"
  [[ "$hermes_version" == "Hermes Agent v0.18.2 (2026.7.7.2)"* ]] || {
    echo "Hermes CLI version mismatch" >&2; exit 2;
  }
  "$hermes_root/venv/bin/hermes" kanban init
  [[ -f "$kanban_db" ]] || { echo "kanban DB was not created" >&2; exit 2; }
  chmod 0600 "$kanban_db"
  [[ "$(stat -c "%a" "$kanban_db")" == "600" ]] || {
    echo "kanban DB mode mismatch" >&2; exit 2;
  }
  PYTHONPATH="$release" /usr/bin/python3 \
    "$release/forge/scripts/hermes-bootstrap.py" advance \
    --record "$bootstrap_record" --expected-stage runtime --next-stage database
  PYTHONPATH="$release" /usr/bin/python3 \
    "$release/forge/scripts/hermes-bootstrap.py" complete \
    --record "$bootstrap_record" --expected-stage database
  trap - ERR INT TERM
  exit 0
fi

if [[ "$phase" == "hooks" ]]; then
  for repo in "${repos[@]}"; do
    (
      cd "$release"
      PYTHONPATH="$release" /usr/bin/python3 \
        "$release/forge/scripts/install-codex-hook.py" \
        --release "$release" --manifest "$manifest" --repo "$repo"
      PYTHONPATH="$release" /usr/bin/python3 \
        "$release/forge/scripts/install-codex-hook.py" \
        --release "$release" --manifest "$manifest" --repo "$repo" --verify
    )
  done
  exit 0
fi

linger_state="$(loginctl show-user "$USER" -p Linger --value)"
case "$linger_state" in
  yes) ;;
  no)
    sudo -n true
    sudo -n loginctl enable-linger "$USER"
    ;;
  *) echo "unexpected linger state: $linger_state" >&2; exit 2 ;;
esac
[[ "$(loginctl show-user "$USER" -p Linger --value)" == "yes" ]] || {
  echo "linger is not enabled" >&2; exit 2;
}

chmod 0755 "$release/forge/scripts/"*.py
chmod 0755 "$release/forge/scripts/"*.sh
unit_dir="$HOME/.config/systemd/user"
install -d -m 0755 "$unit_dir"
target_env="$HOME/.config/infinity-forge/target.env"
install -d -m 0700 "$(dirname "$target_env")"
target_env_tmp="$(dirname "$target_env")/.target.env.$$.${RANDOM}.tmp"
(umask 077; set -o noclobber; : > "$target_env_tmp")
trap 'rm -f "$target_env_tmp"' EXIT
printf "FORGE_TARGET=%s\n" "$target" > "$target_env_tmp"
chmod 0600 "$target_env_tmp"
/usr/bin/python3 - "$target_env_tmp" <<'PY'
import os
import sys

with open(sys.argv[1], "rb") as stream:
    os.fsync(stream.fileno())
PY
mv -f "$target_env_tmp" "$target_env"
/usr/bin/python3 - "$(dirname "$target_env")" <<'PY'
import os
import sys

descriptor = os.open(sys.argv[1], os.O_RDONLY)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
trap - EXIT
common_units=(
  forge-dispatcher.service forge-canary.service forge-canary.timer
  forge-drift.service forge-drift.timer
)
vps_only_units=(
  forge-mirror.service forge-mirror.timer
  forge-spec-coverage.service forge-spec-coverage.timer
  forge-ledger.service forge-ledger.timer
  forge-flush-outbox.service forge-flush-outbox.timer
  forge-morning-report.service forge-morning-report.timer
  forge-backup.service forge-backup.timer
)
selected_units=("${common_units[@]}")
if [[ "$target" == "vps" ]]; then
  selected_units+=("${vps_only_units[@]}")
else
  for unit in "${vps_only_units[@]}"; do
    load_state="$(systemctl --user show "$unit" -p LoadState --value)"
    case "$load_state" in
      not-found) ;;
      loaded) systemctl --user disable --now "$unit" ;;
      *) echo "unexpected unit load state: $unit=$load_state" >&2; exit 2 ;;
    esac
    rm -f "$unit_dir/$unit"
  done
fi
for unit in "${selected_units[@]}"; do
  install -m 0644 "$release/forge/systemd/$unit" "$unit_dir/$unit"
done
systemctl --user daemon-reload
enable_units=(forge-dispatcher.service forge-canary.timer forge-drift.timer)
if [[ "$target" == "vps" ]]; then
  enable_units+=(
    forge-mirror.timer forge-spec-coverage.timer forge-ledger.timer
    forge-flush-outbox.timer forge-morning-report.timer forge-backup.timer
  )
fi
systemctl --user enable --now "${enable_units[@]}"
```

`forge/ops/hermes_bootstrap.py`는 exact schema `schema_version,target,root,snapshot_index,snapshot_sha256,source_sha,stage,owned_paths`만 허용한다. `schema_version`은 `hermes-bootstrap-journal/v1`, target은 `linux|vps`, stage는 `authorized|checkout|runtime|database|complete|rolled-back`이다. `begin`은 authorization 직후 filesystem mutation 전에 canonical absolute root/snapshot/owned path, snapshot canonical bytes digest, source SHA를 temp→flush→fsync→replace→parent-directory fsync로 기록한다. 같은 active record의 exact-equal retry만 idempotent하고 다른 source/root/snapshot은 fail closed다. `advance`와 `complete`는 `--expected-stage` compare-and-swap 뒤 같은 내구 순서로 전이한다.

`recover`는 active record가 없으면 no-op, `stage=complete|rolled-back`이면 read-only 검증 후 no-op다. 그 외에는 record와 현재 filesystem을 다시 검증하고, 모든 owned path가 승인된 Hermes root/bootstrap venv/kanban DB exact set이며 parent chain에 symlink 또는 Windows reparse point가 없고 pre-snapshot absent였음을 확인한 후에만 deepest-first로 삭제한다. parent directory나 unrelated sibling은 삭제하지 않는다. 삭제와 directory fsync가 모두 끝난 뒤 active record bytes를 content-addressed `hermes-bootstrap-history/<sha256>.json`에 보존하고, active `hermes-bootstrap-<target>.json`의 sibling `hermes-bootstrap-<target>.rolled-back.json`에 history digest/source/root와 `stage=rolled-back`인 tombstone pointer를 원자 기록한 뒤 active record를 guarded delete/fsync한다. 새 `begin`은 tombstone을 read-only 검증하고 새 active record를 생성하므로 same/new source forward가 가능하다. 삭제 전 검증 실패는 아무것도 변경하지 않고 exit 2다. completed journal은 deployment receipt가 digest로 결합할 증거이므로 installer 성공 후에도 유지한다.

`ensure-approved-base`는 `git cat-file -e SHA^{commit}`이 실패할 때만 `git fetch --no-write-fetch-head --depth 1 origin SHA`로 pinned object를 가져오고 다시 exact object를 확인한다. `refs/infinity-forge/approved-base`가 absent면 old value zero OID로만 생성하고, present면 SHA exact-equal일 때만 reuse한다. 다른 값 rewrite, moving remote ref, `merge-base --is-ancestor`, checkout/reset은 사용하지 않는다. Windows carried HEAD `540f90190f50f9518bf36632a724e0e58877a10b`와 VPS carried HEAD `73b611ad19720d70308dad6b0fb64648aaadc216`는 approved commit의 descendants가 아니므로 ancestry를 신뢰 경계로 쓰지 않고 completion manifest의 exact supported target blob/7개 AST preimage/target preimage로 판정한다. outer deployment snapshot은 ref pre-state를 기록해 rollback에서 absent였으면 delete, present였으면 exact restore한다.

- [ ] **Step 5: Linux post-install/reboot verifier 구현**

`verify-linux-install.sh --target linux|vps`은 manifest hash, current symlink, script mode 0755, `Linger=yes`, dispatcher/canary/drift의 exact common inventory와 `is-enabled`, dispatcher `is-active`, 각 repo hook `--verify`, gateway health, canary marker/deployed SHA를 검사한다. `$HOME/.config/infinity-forge/target.env`가 mode 0600과 exact single line `FORGE_TARGET=<target>`을 갖는지, canary/drift unit이 그 file과 exact required `--target`/`--sha-from-build-manifest` argv를 쓰는지도 검사한다. `linux` target에서는 mirror/spec/ledger/flush/morning/backup unit이 absent 또는 disabled임을, `vps`에서는 그 exact VPS-only inventory가 모두 installed/enabled임을 hard fail로 검사한다. 또한 ambient PATH와 무관하게 trusted bootstrap `uv`가 존재하는 경우 exact version을, Hermes checkout full approved base/patch HEAD, `<hermes-root>/venv/bin/hermes --version`, `~/.hermes/kanban.db` 존재·read-only SQLite `quick_check`·mode 0600을 검사한다. Linux staging과 VPS에서 설치 직후 한 번, controlled reboot 뒤 SSH가 복구된 후 한 번 실행한다. 두 실행 결과와 boot ID를 deployment evidence에 기록한다.

Linux mutation preflight는 `loginctl show-user "$USER" -p Linger --value`가 `yes`이거나 `sudo -n true`와 `sudo -n loginctl enable-linger "$USER"`가 가능한 경우만 통과한다. 현재 WSL `Ubuntu/immortal0900`처럼 passwordless sudo가 없고 Linger가 `no`이면 rollout operator가 mutation 시작 전에 Windows 관리자 경계에서 정확히 `wsl.exe -d Ubuntu -u root -- loginctl enable-linger immortal0900`을 한 번 실행하고, 일반 사용자로 `loginctl show-user immortal0900 -p Linger --value`가 `yes`인지 read-back한다. 이 명시적 prerequisite가 실패하면 deploy는 시작하지 않으며 user service를 Startup VBS나 foreground process로 대체하지 않는다. 이미 `yes`이면 installer는 sudo를 호출하지 않는다.

- [ ] **Step 6: Windows installer 구현**

`install-windows.ps1`은 current-user-only ACL을 release/state에 설정하고, `$RepoPaths` 각각에 hook installer install/verify를 실행한다. `Register-ScheduledTask` action은 확인된 Python 3.11 absolute path와 current release의 Python entrypoint 및 parser-required argv 전체를 사용한다. Dispatcher는 logon+startup trigger, Canary는 6시간+21:00 trigger, Drift는 hourly trigger를 사용하고 `MultipleInstances=IgnoreNew`를 고정한다. Services phase는 Task 생성 전 canonical bootstrap repository를 확정하고 same-user GitHub credential, explicit repository variable/issue access, WSL/VPS read-only transport를 preflight해 state config로 원자 기록한다. 등록·ACL·hook verify 중 하나라도 실패하면 등록한 새 Task를 제거하고 nonzero로 종료한다. 핵심 등록 코드는 다음 계약을 그대로 사용한다.

```powershell
param(
  [Parameter(Mandatory=$true)]
  [ValidateSet("Hooks","Services")]
  [string]$Phase,
  [Parameter(Mandatory=$true)][string]$ReleasePath,
  [Parameter(Mandatory=$true)][string]$Manifest,
  [Parameter(Mandatory=$true)][string[]]$RepoPaths,
  [Parameter(Mandatory=$true)][string]$PythonPath,
  [string]$BootstrapRepository,
  [switch]$PlanOnly
)
$ErrorActionPreference = "Stop"
if ($Phase -eq "Hooks" -and $RepoPaths.Count -eq 0) {
  throw "Hooks phase requires at least one repository"
}
if ($Phase -eq "Services" -and $BootstrapRepository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') {
  throw "Services phase requires -BootstrapRepository OWNER/REPO"
}
if ($Phase -eq "Hooks" -and $BootstrapRepository) {
  throw "BootstrapRepository is valid only for Services phase"
}
$TaskPath = "\INFINITY_FORGE\"
$StatePath = Join-Path $env:LOCALAPPDATA "InfinityForge\state"
$BuildManifestPath = Join-Path $ReleasePath "build-manifest.json"
$Specs = @(
  [pscustomobject]@{
    Name = "Dispatcher"
    Script = "dispatcher-supervisor.py"
    Arguments = @()
    Triggers = @(
      (New-ScheduledTaskTrigger -AtStartup),
      (New-ScheduledTaskTrigger -AtLogOn)
    )
  },
  [pscustomobject]@{
    Name = "Canary"
    Script = "canary.py"
    Arguments = @(
      "--mode", "run", "--target", "windows",
      "--sha-from-build-manifest", $BuildManifestPath
    )
    Triggers = @(
      (New-ScheduledTaskTrigger -Daily -At "21:00"),
      (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Hours 6) `
        -RepetitionDuration (New-TimeSpan -Days 3650))
    )
  },
  [pscustomobject]@{
    Name = "Drift"
    Script = "drift-audit.py"
    Arguments = @(
      "--target", "all", "--sha-from-build-manifest", $BuildManifestPath,
      "--publish-ops-evidence", "--bootstrap-repository", $BootstrapRepository,
      "--bootstrap-issue-from-repo-variable"
    )
    Triggers = @(
      (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Hours 1) `
        -RepetitionDuration (New-TimeSpan -Days 3650))
    )
  }
)
if ($PlanOnly) {
  if ($Phase -eq "Hooks") {
    @($RepoPaths) | ForEach-Object {
      [pscustomobject]@{ Phase = $Phase; Repository = $_ }
    } | ConvertTo-Json -Compress
    exit 0
  }
  $Specs | ForEach-Object {
    [pscustomobject]@{
      Phase = $Phase
      Name = $_.Name
      Script = $_.Script
      Arguments = @($_.Arguments)
      WorkingDirectory = $ReleasePath
      PythonPath = $PythonPath
    }
  } | ConvertTo-Json -Compress
  exit 0
}

& $PythonPath -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'
if ($LASTEXITCODE -ne 0) { throw "Python 3.11+ is required: $PythonPath" }

$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$PreviousPythonPath = $env:PYTHONPATH
if ($Phase -eq "Hooks") {
  New-Item -ItemType Directory -Force -Path $StatePath | Out-Null
  icacls $ReleasePath /inheritance:r /grant:r "${User}:(OI)(CI)F" | Out-Null
  icacls $StatePath /inheritance:r /grant:r "${User}:(OI)(CI)F" | Out-Null
  try {
    $env:PYTHONPATH = $ReleasePath
    foreach ($RepoPath in $RepoPaths) {
      & $PythonPath "$ReleasePath\forge\scripts\install-codex-hook.py" `
        --release $ReleasePath --manifest $Manifest --repo $RepoPath
      if ($LASTEXITCODE -ne 0) { throw "hook install failed: $RepoPath" }
      & $PythonPath "$ReleasePath\forge\scripts\install-codex-hook.py" `
        --release $ReleasePath --manifest $Manifest --repo $RepoPath --verify
      if ($LASTEXITCODE -ne 0) { throw "hook verify failed: $RepoPath" }
    }
  } finally {
    $env:PYTHONPATH = $PreviousPythonPath
  }
  exit 0
}
if ($Phase -ne "Services") {
  throw "unsupported installer phase: $Phase"
}

$ResolvedManifest = (Resolve-Path -LiteralPath $Manifest).Path
$ResolvedBuildManifest = (Resolve-Path -LiteralPath $BuildManifestPath).Path
if ($ResolvedManifest -ne $ResolvedBuildManifest) {
  throw "Services manifest must be the immutable release build-manifest.json"
}
$PublisherConfig = Join-Path $StatePath "publisher-config.json"
try {
  $env:PYTHONPATH = $ReleasePath
  & $PythonPath -m forge.ops.ops_evidence preflight-publisher `
    --bootstrap-repository $BootstrapRepository `
    --bootstrap-issue-from-repo-variable `
    --require-wsl-target linux --require-vps-target vps `
    --output $PublisherConfig
  if ($LASTEXITCODE -ne 0) { throw "publisher preflight failed" }
} finally {
  $env:PYTHONPATH = $PreviousPythonPath
}
New-Item -ItemType Directory -Force -Path $StatePath | Out-Null
icacls $ReleasePath /inheritance:r /grant:r "${User}:(OI)(CI)F" | Out-Null
icacls $StatePath /inheritance:r /grant:r "${User}:(OI)(CI)F" | Out-Null

$Settings = New-ScheduledTaskSettingsSet `
  -MultipleInstances IgnoreNew `
  -RestartCount 3 `
  -RestartInterval (New-TimeSpan -Minutes 1)
$Principal = New-ScheduledTaskPrincipal `
  -UserId $User -LogonType Interactive -RunLevel Highest
foreach ($Spec in $Specs) {
  $FullName = "\INFINITY_FORGE\$($Spec.Name)"
  $ScriptPath = Join-Path $ReleasePath "forge\scripts\$($Spec.Script)"
  $Bootstrap = "import runpy,sys;sys.path.insert(0,sys.argv[1]);sys.argv=[sys.argv[2],*sys.argv[3:]];runpy.run_path(sys.argv[0],run_name='__main__')"
  function ConvertTo-TaskArgument([string]$Value) {
    return '"' + $Value.Replace('"', '\"') + '"'
  }
  $ActionParts = @(
    '-c',
    (ConvertTo-TaskArgument $Bootstrap),
    (ConvertTo-TaskArgument $ReleasePath),
    (ConvertTo-TaskArgument $ScriptPath)
  )
  $ActionParts += @($Spec.Arguments | ForEach-Object { ConvertTo-TaskArgument $_ })
  $ActionArguments = $ActionParts -join ' '
  $Action = New-ScheduledTaskAction -Execute $PythonPath `
    -Argument $ActionArguments -WorkingDirectory $ReleasePath
  Register-ScheduledTask -TaskPath $TaskPath -TaskName $Spec.Name `
    -Action $Action -Trigger $Spec.Triggers -Settings $Settings `
    -Principal $Principal -Force | Out-Null
  if (-not (Get-ScheduledTask -TaskPath $TaskPath -TaskName $Spec.Name)) {
    throw "Scheduled Task missing after registration: $FullName"
  }
}
```

- [ ] **Step 7: GREEN과 platform syntax 확인**

Run: `.\.venv\Scripts\python.exe -m pytest tests/ops/test_install_contracts.py tests/ops/test_hermes_bootstrap.py -q`

Expected: 모든 install contract와 bootstrap crash/signal recovery case PASS.

Run: `wsl.exe bash -lc 'cd /mnt/c/01.project/INFINITY_FORGE && bash -n forge/scripts/install-linux.sh forge/scripts/verify-linux-install.sh && systemd-analyze verify forge/systemd/*.service forge/systemd/*.timer'`

Expected: exit 0.

Run: `wsl.exe --terminate Ubuntu; wsl.exe -d Ubuntu -- bash -lc 'release="$HOME/.local/share/infinity-forge/current"; sha="$(PYTHONPATH="$release" /usr/bin/python3 -c "import json; print(json.load(open(\"$release/build-manifest.json\"))[\"source_sha\"])")"; "$release/forge/scripts/verify-linux-install.sh" --target linux --release "$release" --manifest "$release/build-manifest.json" --repo "$HOME/work/INFINITY_FORGE/$sha"'`

Expected: 새 WSL boot ID에서 exit 0이며 dispatcher service가 active다.

Run: `pwsh -NoProfile -Command "[scriptblock]::Create((Get-Content -Raw forge/scripts/install-windows.ps1)) | Out-Null"`

Expected: exit 0.

- [ ] **Step 8: commit**

```powershell
git add forge/systemd forge/hermes/uv-bootstrap-linux.lock forge/ops/hermes_bootstrap.py forge/scripts/hermes-bootstrap.py forge/scripts/install-linux.sh forge/scripts/verify-linux-install.sh forge/scripts/install-windows.ps1 tests/ops/test_install_contracts.py tests/ops/test_hermes_bootstrap.py
git commit -m "feat: install Forge services and hooks on Linux and Windows"
```

### Task 7: immutable build manifest와 host deployment receipt

**Files:**
- Modify: `forge/ops/contracts.py`
- Modify: `forge/schemas/build-manifest.schema.json`
- Create: `forge/schemas/guard-current.schema.json`
- Create: `forge/schemas/deployment-receipt.schema.json`
- Create: `forge/ops/deployment.py`
- Modify: `forge/scripts/build-guard-release.py`
- Create: `tests/ops/test_deployment.py`

**Consumes:**
- clean full 40자리 source SHA.
- named checks `guard-contract (ubuntu-latest)`, `guard-contract (windows-latest)`.
- reproducible project archive, self-contained guard zipapp, schema hashes, Hermes patch hashes.

**Produces:**
- `BuildManifest`: source/artifact 정적 사실만 포함한 canonical JSON.
- `GuardCurrentManifest`: Hermes가 직접 소비하는 exact nested policy runtime binding.
- `DeploymentReceipt`: target, installed_at, previous/current release, result를 포함한 host state.
- `build_manifest_bytes(manifest: BuildManifest) -> bytes`.
- `write_guard_current(path: Path, manifest: GuardCurrentManifest) -> None`.
- `verify_guard_current(path: Path, *, expected_source_sha: str) -> GuardCurrentManifest`.
- `deployment_receipt_bytes(receipt: DeploymentReceipt) -> bytes`.
- `build_parser() -> argparse.ArgumentParser` with seven fixed deployment subcommands.

**Interfaces:**

```python
from dataclasses import dataclass
from typing import Sequence

@dataclass(frozen=True)
class BuildManifest:
    schema_version: str
    source_sha: str
    archive_sha256: str
    guard_sha256: str
    requirements_lock_sha256: str
    python_requires: str
    schema_hashes: Sequence[tuple[str, str]]
    hermes_patch_manifest_sha256: str
    hermes_patch_sha256: str

@dataclass(frozen=True)
class ForgePolicyRuntime:
    python: str
    artifact: str
    artifact_sha256: str
    timeout_seconds: int

@dataclass(frozen=True)
class GuardCurrentManifest:
    schema_version: str
    policies: dict[str, ForgePolicyRuntime]

@dataclass(frozen=True)
class DeploymentReceipt:
    schema_version: str
    build_manifest_sha256: str
    target: str
    installed_at_utc: str
    previous_release: str | None
    current_release: str | None
    guard_current_sha256: str | None
    snapshot_bundle_path: str
    snapshot_bundle_sha256: str
    hermes_patch_install_record_path: str
    hermes_patch_install_record_sha256: str
    hermes_bootstrap_record_path: str | None
    hermes_bootstrap_record_sha256: str | None
    slack_alert_env_sha256: str
    repository_hook_hashes: Sequence[tuple[str, str]]
    result: str
```

- [ ] **Step 1: build determinism과 동적 필드 분리를 고정하는 RED test 작성**

```python
import json

from forge.ops.deployment import (
    BuildManifest,
    DeploymentReceipt,
    build_manifest_bytes,
    deployment_receipt_bytes,
)


def test_build_manifest_is_reproducible_and_host_receipt_is_separate() -> None:
    manifest = BuildManifest(
        schema_version="build-manifest-v1",
        source_sha="1" * 40,
        archive_sha256="2" * 64,
        guard_sha256="3" * 64,
        requirements_lock_sha256="a" * 64,
        python_requires=">=3.11",
        schema_hashes=(
            ("handoff-v1.schema.json", "4" * 64),
            ("receipt-v1.schema.json", "5" * 64),
        ),
        hermes_patch_manifest_sha256="6" * 64,
        hermes_patch_sha256="7" * 64,
    )
    first = build_manifest_bytes(manifest)
    second = build_manifest_bytes(manifest)
    parsed = json.loads(first)

    assert first == second
    assert "installed_at_utc" not in parsed
    assert "target" not in parsed
    assert "previous_release" not in parsed
    assert parsed["python_requires"] == ">=3.11"

    receipt = DeploymentReceipt(
        schema_version="deployment-receipt-v1",
        build_manifest_sha256="8" * 64,
        target="vps",
        installed_at_utc="2026-07-12T05:00:00Z",
        previous_release="0" * 40,
        current_release="1" * 40,
        guard_current_sha256="9" * 64,
        snapshot_bundle_path="/home/ops/.local/state/infinity-forge/snapshots/deploy-1",
        snapshot_bundle_sha256="b" * 64,
        hermes_patch_install_record_path="/home/ops/.local/state/infinity-forge/deployments/hermes-patch-vps.json",
        hermes_patch_install_record_sha256="c" * 64,
        hermes_bootstrap_record_path=None,
        hermes_bootstrap_record_sha256=None,
        slack_alert_env_sha256="e" * 64,
        repository_hook_hashes=(
            ("/home/ops/work/widget", "a" * 64),
        ),
        result="success",
    )
    receipt_json = json.loads(deployment_receipt_bytes(receipt))
    assert receipt_json["target"] == "vps"
    assert receipt_json["installed_at_utc"] == "2026-07-12T05:00:00Z"
    assert receipt_json["guard_current_sha256"] == "9" * 64
    assert receipt_json["snapshot_bundle_sha256"] == "b" * 64
    assert receipt_json["hermes_patch_install_record_sha256"] == "c" * 64
    assert receipt_json["slack_alert_env_sha256"] == "e" * 64
    assert receipt_json["repository_hook_hashes"] == [
        ["/home/ops/work/widget", "a" * 64]
    ]
    clean_rollback = json.loads(
        deployment_receipt_bytes(
            replace(
                receipt,
                previous_release=None,
                current_release=None,
                guard_current_sha256=None,
                result="rolled-back",
            )
        )
    )
    assert clean_rollback["current_release"] is None
    assert clean_rollback["guard_current_sha256"] is None
    with pytest.raises(ValueError, match="release and guard digest must be paired"):
        deployment_receipt_bytes(
            replace(
                receipt,
                current_release=receipt.previous_release,
                guard_current_sha256=None,
                result="rolled-back",
            )
        )
    with pytest.raises(ValueError, match="bootstrap record path and digest must be paired"):
        deployment_receipt_bytes(
            replace(
                receipt,
                hermes_bootstrap_record_path="/state/hermes-bootstrap-vps.json",
                hermes_bootstrap_record_sha256=None,
            )
        )
    with pytest.raises(ValueError, match="Slack alert environment digest"):
        deployment_receipt_bytes(
            replace(receipt, slack_alert_env_sha256="not-a-sha256")
        )
    clean_bootstrap = json.loads(
        deployment_receipt_bytes(
            replace(
                receipt,
                hermes_bootstrap_record_path="/state/hermes-bootstrap-vps.json",
                hermes_bootstrap_record_sha256="d" * 64,
            )
        )
    )
    assert clean_bootstrap["hermes_bootstrap_record_sha256"] == "d" * 64
```

- [ ] **Step 2: guard current.json atomic write와 tamper 검증 RED test 작성**

```python
import hashlib
import json
import sys
from pathlib import Path

import pytest

from forge.ops.deployment import (
    ForgePolicyRuntime,
    GuardCurrentManifest,
    verify_guard_current,
    write_guard_current,
)


def test_guard_current_is_atomic_and_binds_exact_runtime(tmp_path: Path) -> None:
    source_sha = "1" * 40
    guard = (
        tmp_path / "guard" / "releases" / source_sha / "forge-guard.pyz"
    ).resolve()
    guard.parent.mkdir(parents=True)
    guard.write_bytes(b"trusted-guard")
    digest = hashlib.sha256(guard.read_bytes()).hexdigest()
    manifest = GuardCurrentManifest(
        schema_version="forge-completion-manifest/v1",
        policies={
            "forge-v1": ForgePolicyRuntime(
                python=str(Path(sys.executable).resolve()),
                artifact=str(guard),
                artifact_sha256=digest,
                timeout_seconds=900,
            )
        },
    )
    current = tmp_path / "guard" / "current.json"

    write_guard_current(current, manifest)

    assert current.exists()
    assert not current.with_name("current.json.tmp").exists()
    assert verify_guard_current(current, expected_source_sha=source_sha) == manifest
    assert set(json.loads(current.read_text(encoding="utf-8"))) == {
        "schema_version",
        "policies",
    }
    raw = json.loads(current.read_text(encoding="utf-8"))
    assert set(raw["policies"]) == {"forge-v1"}
    assert set(raw["policies"]["forge-v1"]) == {
        "python",
        "artifact",
        "artifact_sha256",
        "timeout_seconds",
    }

    raw["policies"]["forge-v1"]["timeout_seconds"] = 901
    current.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="between 1 and 900"):
        verify_guard_current(current, expected_source_sha=source_sha)
    write_guard_current(current, manifest)

    guard.write_bytes(b"tampered-guard")
    with pytest.raises(ValueError, match="forge-guard.pyz SHA-256 mismatch"):
        verify_guard_current(current, expected_source_sha=source_sha)
```

- [ ] **Step 3: deployment Python CLI subcommands RED test 작성**

```python
import pytest

from forge.ops.deployment import build_parser


@pytest.mark.parametrize(
    ("argv", "command"),
    (
        (
            ["build", "--sha", "1" * 40, "--output-dir", "C:\\forge-build"],
            "build",
        ),
        (
            [
                "verify-build",
                "--build-manifest",
                "build-manifest.json",
                "--artifact",
                "forge-release.zip",
                "--artifact-sha256",
                "2" * 64,
            ],
            "verify-build",
        ),
        (
            [
                "compare-hermes-status",
                "--target",
                "windows",
                "--expected-sha",
                "1" * 40,
                "--current-manifest",
                "current.json",
                "--hermes-root",
                "C:\\Users\\ops\\AppData\\Local\\hermes\\hermes-agent",
            ],
            "compare-hermes-status",
        ),
        (
            [
                "authorize-clean-hermes-bootstrap",
                "--target",
                "linux",
                "--snapshot-index",
                "/tmp/snapshot-index.json",
                "--snapshot-sha256",
                "a" * 64,
                "--hermes-root",
                "/home/ops/.hermes/hermes-agent",
            ],
            "authorize-clean-hermes-bootstrap",
        ),
        (
            [
                "verify-rollback",
                "--target",
                "linux",
                "--before-receipt",
                "before.json",
                "--after-receipt",
                "after.json",
                "--current-manifest",
                "current.json",
            ],
            "verify-rollback",
        ),
        (
            [
                "record-evidence",
                "--target",
                "vps",
                "--receipt",
                "receipt.json",
                "--output",
                "evidence.json",
            ],
            "record-evidence",
        ),
        (
            [
                "audit-targets",
                "--windows-receipt",
                "windows.json",
                "--linux-receipt",
                "linux.json",
                "--vps-receipt",
                "vps.json",
            ],
            "audit-targets",
        ),
    ),
)
def test_deployment_cli_has_fixed_subcommands(
    argv: list[str], command: str
) -> None:
    parsed = build_parser().parse_args(argv)
    assert parsed.command == command
```

- [ ] **Step 4: RED 확인**

Run: `.\.venv\Scripts\python.exe -m pytest tests/ops/test_deployment.py -q`

Expected: `ModuleNotFoundError: No module named 'forge.ops.deployment'`로 FAIL.

- [ ] **Step 5: canonical serialization 최소 구현**

```python
import json
from dataclasses import asdict

from forge.ops.contracts import BuildManifest, DeploymentReceipt


def _canonical_bytes(value: dict[str, object]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def build_manifest_bytes(manifest: BuildManifest) -> bytes:
    if len(manifest.source_sha) != 40:
        raise ValueError("source_sha must contain 40 hexadecimal characters")
    return _canonical_bytes(asdict(manifest))


def deployment_receipt_bytes(receipt: DeploymentReceipt) -> bytes:
    if receipt.result not in {"success", "rolled-back", "failed"}:
        raise ValueError("invalid deployment result")
    if receipt.result == "success" and (
        receipt.current_release is None or receipt.guard_current_sha256 is None
    ):
        raise ValueError("successful deployment requires current release and guard digest")
    if receipt.current_release is None and not (
        receipt.result == "rolled-back" and receipt.guard_current_sha256 is None
    ):
        raise ValueError("absent release is valid only for clean-host rollback")
    if (receipt.current_release is None) != (receipt.guard_current_sha256 is None):
        raise ValueError("release and guard digest must be paired")
    if (receipt.hermes_bootstrap_record_path is None) != (
        receipt.hermes_bootstrap_record_sha256 is None
    ):
        raise ValueError("bootstrap record path and digest must be paired")
    for digest in (
        receipt.snapshot_bundle_sha256,
        receipt.hermes_patch_install_record_sha256,
        *(
            (receipt.hermes_bootstrap_record_sha256,)
            if receipt.hermes_bootstrap_record_sha256 is not None
            else ()
        ),
    ):
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError("deployment receipt digest must be 64 lowercase hex")
    if receipt.guard_current_sha256 is not None and (
        len(receipt.guard_current_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in receipt.guard_current_sha256
        )
    ):
        raise ValueError("guard current digest must be 64 lowercase hex or null")
    if not receipt.snapshot_bundle_path or not receipt.hermes_patch_install_record_path:
        raise ValueError("deployment receipt rollback locators are required")
    if receipt.hermes_bootstrap_record_path == "":
        raise ValueError("bootstrap record path must be non-empty or null")
    return _canonical_bytes(asdict(receipt))
```

- [ ] **Step 6: exact nested current.json atomic writer와 verifier 구현**

```python
import hashlib
import os
from pathlib import Path

from forge.ops.contracts import ForgePolicyRuntime, GuardCurrentManifest


def guard_current_bytes(manifest: GuardCurrentManifest) -> bytes:
    if manifest.schema_version != "forge-completion-manifest/v1":
        raise ValueError("unsupported guard current schema")
    if set(manifest.policies) != {"forge-v1"}:
        raise ValueError("guard current must contain only forge-v1")
    return _canonical_bytes(asdict(manifest))


def write_guard_current(
    path: Path, manifest: GuardCurrentManifest
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name("current.json.tmp")
    try:
        with temp.open("wb") as handle:
            handle.write(guard_current_bytes(manifest))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        path.chmod(0o600)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temp.exists():
            temp.unlink()


def verify_guard_current(
    path: Path, *, expected_source_sha: str
) -> GuardCurrentManifest:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "policies"}:
        raise ValueError("guard current top-level fields do not match schema")
    if raw["schema_version"] != "forge-completion-manifest/v1":
        raise ValueError("unsupported guard current schema")
    policies = raw["policies"]
    if not isinstance(policies, dict) or set(policies) != {"forge-v1"}:
        raise ValueError("guard current must contain only forge-v1")
    policy = policies["forge-v1"]
    required = {
        "python",
        "artifact",
        "artifact_sha256",
        "timeout_seconds",
    }
    if not isinstance(policy, dict) or set(policy) != required:
        raise ValueError("forge-v1 policy fields do not match schema")
    if not isinstance(policy["python"], str):
        raise ValueError("policy python must be a string")
    if not isinstance(policy["artifact"], str):
        raise ValueError("policy artifact must be a string")
    artifact_sha256 = policy["artifact_sha256"]
    if (
        not isinstance(artifact_sha256, str)
        or len(artifact_sha256) != 64
        or any(character not in "0123456789abcdef" for character in artifact_sha256)
    ):
        raise ValueError("policy artifact_sha256 must be 64 lowercase hex characters")
    timeout_seconds = policy["timeout_seconds"]
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 900:
        raise ValueError("policy timeout_seconds must be between 1 and 900")
    if (
        len(expected_source_sha) != 40
        or any(character not in "0123456789abcdef" for character in expected_source_sha)
    ):
        raise ValueError("expected_source_sha must be 40 lowercase hex characters")
    runtime = ForgePolicyRuntime(
        python=policy["python"],
        artifact=policy["artifact"],
        artifact_sha256=artifact_sha256,
        timeout_seconds=timeout_seconds,
    )
    interpreter = Path(runtime.python)
    artifact = Path(runtime.artifact)
    if not interpreter.is_absolute() or not interpreter.is_file():
        raise ValueError("policy python must be an existing absolute file")
    if not artifact.is_absolute() or not artifact.is_file():
        raise ValueError("policy artifact must be an existing absolute file")
    if artifact.parent.name != expected_source_sha:
        raise ValueError("policy artifact release directory does not match source SHA")
    actual_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
    if actual_sha != runtime.artifact_sha256:
        raise ValueError("forge-guard.pyz SHA-256 mismatch")
    return GuardCurrentManifest(
        schema_version="forge-completion-manifest/v1",
        policies={"forge-v1": runtime},
    )
```

- [ ] **Step 7: deployment Python CLI parser 구현**

```python
import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m forge.ops.deployment")
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build")
    build.add_argument("--sha", required=True)
    build.add_argument("--output-dir", required=True)

    verify_build = commands.add_parser("verify-build")
    verify_build.add_argument("--build-manifest", required=True)
    verify_build.add_argument("--artifact", required=True)
    verify_build.add_argument("--artifact-sha256", required=True)

    compare = commands.add_parser("compare-hermes-status")
    compare.add_argument(
        "--target", choices=("windows", "linux", "vps"), required=True
    )
    compare.add_argument("--expected-sha", required=True)
    compare.add_argument("--current-manifest", required=True)
    compare.add_argument("--hermes-root", required=True)

    authorize = commands.add_parser("authorize-clean-hermes-bootstrap")
    authorize.add_argument(
        "--target", choices=("linux", "vps"), required=True
    )
    authorize.add_argument("--snapshot-index", required=True)
    authorize.add_argument("--snapshot-sha256", required=True)
    authorize.add_argument("--hermes-root", required=True)

    rollback = commands.add_parser("verify-rollback")
    rollback.add_argument(
        "--target", choices=("windows", "linux", "vps"), required=True
    )
    rollback.add_argument("--before-receipt", required=True)
    rollback.add_argument("--after-receipt", required=True)
    rollback.add_argument("--current-manifest", required=True)

    evidence = commands.add_parser("record-evidence")
    evidence.add_argument(
        "--target", choices=("windows", "linux", "vps"), required=True
    )
    evidence.add_argument("--receipt", required=True)
    evidence.add_argument("--output", required=True)

    audit = commands.add_parser("audit-targets")
    audit.add_argument("--windows-receipt", required=True)
    audit.add_argument("--linux-receipt", required=True)
    audit.add_argument("--vps-receipt", required=True)
    return parser
```

`main()`은 각 command를 같은 이름의 implementation function에 dispatch하고 단일 JSON stdout을 쓴다. `verify-build`, `compare-hermes-status`, `verify-rollback`, `audit-targets` 불일치는 exit 2다. `verify-rollback`은 after receipt의 `current_release/guard_current_sha256`이 null이면 `--current-manifest` path가 존재하지 않고 current pointer/Forge service가 absent인지 확인하며 파일을 열지 않는다. hash 값이면 manifest가 존재하고 digest/current release가 일치해야 한다. `record-evidence`는 temp→fsync→replace로 evidence를 쓰며 secret-bearing environment를 포함하지 않는다.

- [ ] **Step 8: schema와 build script 연결**

`build-manifest-v1` schema는 shared core schema의 위 9개 field만 허용한다. `guard-current.schema.json`은 top-level `schema_version`, `policies`와 nested `forge-v1.python|artifact|artifact_sha256|timeout_seconds`만 허용한다. 두 schema 모두 모든 object에서 `additionalProperties:false`다. source SHA는 `current.json`에 중복 저장하지 않고 artifact의 직계 parent인 immutable release directory 이름이 `BuildManifest.source_sha`와 같은지 검증한다. `deployment-receipt.schema.json`은 `snapshot_bundle_sha256`, `hermes_patch_install_record_sha256`, `slack_alert_env_sha256`을 required 64자리 lowercase hex field로, `snapshot_bundle_path`, `hermes_patch_install_record_path`를 required non-empty string으로 고정한다. `slack_alert_env_sha256`은 Services preflight가 검증한 host-local canonical four-key env bytes digest에서만 만들며 success와 rolled-back receipt 모두 required다. receipt serializer, parser, `verify-rollback`, `audit-targets`, drift provider는 실제 host env digest와 exact 일치 또는 별도 authorized rotation receipt를 요구하고 credential bytes는 읽어 출력하지 않는다. `hermes_bootstrap_record_path`와 `hermes_bootstrap_record_sha256`도 required keys지만 exact pair로만 `null|null` 또는 `non-empty state-root path|64자리 lowercase hex`다. existing Hermes 배포는 null pair, clean bootstrap 성공은 non-null pair여야 하며 verifier는 record bytes digest와 `hermes-bootstrap-journal/v1`, `stage=complete`, exact target/root/source SHA/snapshot digest/owned path set을 다시 확인한다. `result=success`이면 `current_release`와 `guard_current_sha256`은 각각 40/64자리 hash다. `result=rolled-back`이며 clean-host 최초 배포 전 상태로 돌아갈 때만 두 field가 함께 `null`일 수 있고, 이때 `guard/current.json`은 실제로 없어야 한다. target adapter는 Windows/POSIX path 문법 검사를 한 뒤 state root부터 locator까지 모든 existing component를 `lstat`하고 POSIX symlink 또는 Windows `FILE_ATTRIBUTE_REPARSE_POINT`를 거절한다. 그 다음 `resolve(strict=True)`와 `os.path.commonpath`를 모두 통과한 실제 경로만 고정 state root 하위로 인정한다. build script는 clean object와 두 named check를 먼저 확인하고 core `build_guard_component`, `git archive SOURCE_SHA`, locked dependency, schema, Hermes patch를 byte-normalized staging에서 두 번 build해 hash가 같은지 비교한 뒤 유일한 final BuildManifest를 쓴다. deployment receipt는 build 과정에서 만들지 않는다.

- [ ] **Step 9: GREEN 확인**

Run: `.\.venv\Scripts\python.exe -m pytest tests/ops/test_deployment.py -q`

Expected: `8 passed`.

- [ ] **Step 10: commit**

```powershell
git add forge/schemas/build-manifest.schema.json forge/schemas/guard-current.schema.json forge/schemas/deployment-receipt.schema.json forge/ops/deployment.py forge/scripts/build-guard-release.py tests/ops/test_deployment.py
git commit -m "feat: separate immutable builds from host deployment receipts"
```

### Task 8: transactional deploy와 rollback

**Files:**
- Modify: `forge/ops/deployment.py`
- Modify: `tests/ops/test_deployment.py`
- Modify: `forge/scripts/deploy.ps1`
- Modify: `forge/scripts/deploy-vps.sh`
- Modify: `forge/scripts/hermes-patch.py`
- Create: `forge/scripts/rollback.ps1`
- Create: `forge/scripts/rollback-linux.sh`
- Create: `forge/scripts/rollback-vps.sh`

**Consumes:**
- Task 6 installer/verifier와 per-repository hook verification.
- Task 7 immutable `BuildManifest`.
- Hermes patch installer `check|install|verify|rollback`; `verify`는 `--root`, `--manifest`, `--record`, `--current-manifest`, `--expected-source-sha`를 모두 필수로 소비한다.
- target별 config/current pointer/`guard/current.json`/unit/Task/Hermes target snapshot과 DB `.backup`.

**Produces:**
- `DeploymentOperations`
- `RollbackOperations`
- `deploy_target(ops: DeploymentOperations) -> DeploymentReceipt`
- `authorize_clean_hermes_bootstrap(*, target: str, snapshot_index: Path, snapshot_sha256: str, hermes_root: Path) -> None`.
- `preserve_before_receipt(before_receipt: Path, *, state_root: Path) -> Path`.
- `verify_rollback_materials(receipt: DeploymentReceipt, *, target: str, state_root: Path, hermes_root: Path, build: BuildManifest, patch_manifest_path: Path) -> None`.
- `prepare_rollback_materials(*, target: str, before_receipt: Path, state_root: Path, hermes_root: Path, build: BuildManifest, patch_manifest_path: Path) -> VerifiedRollbackContext`.
- `rollback_target(ops: RollbackOperations) -> DeploymentReceipt`.
- `rollback_windows_target(*, before_receipt: Path, build_manifest: Path, repositories: Sequence[Path]) -> DeploymentReceipt`.
- `rollback_linux_target(*, target: str, before_receipt: Path, build_manifest: Path, repositories: Sequence[Path]) -> DeploymentReceipt`.
- Windows interface:
  `deploy.ps1 -Sha SHA -Artifact FILE -ArtifactSha256 HASH -BuildManifest FILE -Targets Windows,Linux,Vps -RepoPaths PATHS -BootstrapRepository OWNER/REPO [-PlanOnly|-Apply]`.
- Windows rollback interface:
  `rollback.ps1 -BeforeReceipt FILE -BuildManifest FILE -RepoPaths PATHS`.
- VPS interface:
  `deploy-vps.sh --sha SHA --artifact FILE --artifact-sha256 HASH --build-manifest FILE --repo PATH [--repo PATH]`.
- generic Linux rollback:
  `rollback-linux.sh --target linux|vps --before-receipt FILE --build-manifest FILE --repo PATH [--repo PATH]`.
- `rollback-vps.sh`는 target을 `vps`로 고정해 `rollback-linux.sh`를 exec하는 thin wrapper다.
- Python CLI:
  `python -m forge.ops.deployment build|verify-build|compare-hermes-status|authorize-clean-hermes-bootstrap|verify-rollback|record-evidence|audit-targets`.
- target state root의 `deployment-receipt-v1.json`.

**Interfaces:**

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from forge.ops.contracts import DeploymentReceipt

VoidAction = Callable[[], None]
BoolAction = Callable[[], bool]
ReceiptAction = Callable[[], DeploymentReceipt]

@dataclass(frozen=True)
class DeploymentOperations:
    verify_build: VoidAction
    acquire_lock: VoidAction
    snapshot_control_plane: VoidAction
    snapshot_guard_current: VoidAction
    pause_producers: VoidAction
    close_marker: VoidAction
    stop_supervisor: VoidAction
    disable_embedded_dispatch: VoidAction
    apply_gateway_config: VoidAction
    assert_embedded_dispatch_off: VoidAction
    drain_active_work: VoidAction
    backup_database: VoidAction
    snapshot_hermes_targets: VoidAction
    finalize_snapshot: VoidAction
    stage_release: VoidAction
    verify_stage: VoidAction
    prepare_hermes_runtime: BoolAction
    install_repository_hooks: VoidAction
    write_guard_current: VoidAction
    verify_guard_current: VoidAction
    install_hermes_patch: VoidAction
    verify_hermes_patch: VoidAction
    switch_current: VoidAction
    install_services: VoidAction
    start_or_restart_gateway: VoidAction
    verify_database: VoidAction
    run_canary: VoidAction
    run_drift: VoidAction
    run_coverage: VoidAction
    audit_hashes: VoidAction
    start_supervisor: VoidAction
    verify_supervisor_ready: VoidAction
    open_marker: VoidAction
    restore_producers: VoidAction
    write_success_receipt: ReceiptAction
    restore_guard_current: VoidAction
    rollback: VoidAction
    release_lock: VoidAction


@dataclass(frozen=True)
class VerifiedRollbackContext:
    preserved_before_receipt: Path
    receipt: DeploymentReceipt
    previous_release: str | None
    gateway_was_running: bool


@dataclass(frozen=True)
class RollbackOperations:
    target: str
    acquire_lock: VoidAction
    prepare_materials: Callable[[], VerifiedRollbackContext]
    pause_producers: VoidAction
    close_marker: VoidAction
    stop_supervisor: VoidAction
    stop_gateway: VoidAction
    restore_guard_current: VoidAction
    restore_snapshot: VoidAction
    reload_services: VoidAction
    start_gateway: VoidAction
    ensure_gateway_stopped: VoidAction
    verify_database_state: VoidAction
    run_previous_canary: VoidAction
    verify_absent_release_state: VoidAction
    activate_previous_dispatcher: VoidAction
    restore_producers: VoidAction
    write_after_receipt: Callable[[VerifiedRollbackContext], DeploymentReceipt]
    release_lock: VoidAction
```

- [ ] **Step 1: embedded dispatcher 선중단과 drain 순서를 고정하는 RED test 작성**

```python
from forge.ops.contracts import DeploymentReceipt
from forge.ops.deployment import DeploymentOperations, deploy_target


def test_deploy_disables_embedded_dispatch_before_drain() -> None:
    calls: list[str] = []

    def action(name: str):
        def invoke() -> None:
            calls.append(name)
        return invoke

    def receipt() -> DeploymentReceipt:
        calls.append("write-success-receipt")
        return DeploymentReceipt(
            schema_version="deployment-receipt-v1",
            build_manifest_sha256="9" * 64,
            target="vps",
            installed_at_utc="2026-07-12T05:00:00Z",
            previous_release="0" * 40,
            current_release="1" * 40,
            guard_current_sha256="9" * 64,
            snapshot_bundle_path="/home/ops/.local/state/infinity-forge/snapshots/deploy-1",
            snapshot_bundle_sha256="b" * 64,
            hermes_patch_install_record_path="/home/ops/.local/state/infinity-forge/deployments/hermes-patch-vps.json",
            hermes_patch_install_record_sha256="c" * 64,
            hermes_bootstrap_record_path=None,
            hermes_bootstrap_record_sha256=None,
            slack_alert_env_sha256="e" * 64,
            repository_hook_hashes=(
                ("/home/ops/work/widget", "a" * 64),
            ),
            result="success",
        )

    def prepare_runtime() -> bool:
        calls.append("prepare-hermes-runtime")
        return True

    names = (
        "verify-build",
        "acquire-lock",
        "snapshot-control-plane",
        "snapshot-guard-current",
        "pause-producers",
        "close-marker",
        "stop-supervisor",
        "disable-embedded-dispatch",
        "apply-gateway-config",
        "assert-embedded-dispatch-off",
        "drain-active-work",
        "backup-database",
        "snapshot-hermes-targets",
        "finalize-snapshot",
        "prepare-hermes-runtime",
        "stage-release",
        "verify-stage",
        "install-repository-hooks",
        "write-guard-current",
        "verify-guard-current",
        "install-hermes-patch",
        "verify-hermes-patch",
        "switch-current",
        "install-services",
        "start-or-restart-gateway",
        "verify-database",
        "run-canary",
        "run-drift",
        "run-coverage",
        "audit-hashes",
        "start-supervisor",
        "verify-supervisor-ready",
        "open-marker",
        "restore-producers",
    )
    callbacks = {name: action(name) for name in names}
    ops = DeploymentOperations(
        verify_build=callbacks["verify-build"],
        acquire_lock=callbacks["acquire-lock"],
        close_marker=callbacks["close-marker"],
        stop_supervisor=callbacks["stop-supervisor"],
        snapshot_control_plane=callbacks["snapshot-control-plane"],
        snapshot_guard_current=callbacks["snapshot-guard-current"],
        pause_producers=callbacks["pause-producers"],
        disable_embedded_dispatch=callbacks["disable-embedded-dispatch"],
        apply_gateway_config=callbacks["apply-gateway-config"],
        assert_embedded_dispatch_off=callbacks["assert-embedded-dispatch-off"],
        drain_active_work=callbacks["drain-active-work"],
        backup_database=callbacks["backup-database"],
        snapshot_hermes_targets=callbacks["snapshot-hermes-targets"],
        finalize_snapshot=callbacks["finalize-snapshot"],
        prepare_hermes_runtime=prepare_runtime,
        stage_release=callbacks["stage-release"],
        verify_stage=callbacks["verify-stage"],
        install_repository_hooks=callbacks["install-repository-hooks"],
        write_guard_current=callbacks["write-guard-current"],
        verify_guard_current=callbacks["verify-guard-current"],
        install_hermes_patch=callbacks["install-hermes-patch"],
        verify_hermes_patch=callbacks["verify-hermes-patch"],
        switch_current=callbacks["switch-current"],
        install_services=callbacks["install-services"],
        start_or_restart_gateway=callbacks["start-or-restart-gateway"],
        verify_database=callbacks["verify-database"],
        run_canary=callbacks["run-canary"],
        run_drift=callbacks["run-drift"],
        run_coverage=callbacks["run-coverage"],
        audit_hashes=callbacks["audit-hashes"],
        start_supervisor=callbacks["start-supervisor"],
        verify_supervisor_ready=callbacks["verify-supervisor-ready"],
        open_marker=callbacks["open-marker"],
        restore_producers=callbacks["restore-producers"],
        write_success_receipt=receipt,
        restore_guard_current=action("restore-guard-current"),
        rollback=action("rollback"),
        release_lock=action("release-lock"),
    )

    result = deploy_target(ops)

    assert result.result == "success"
    assert calls.index("snapshot-control-plane") < calls.index("close-marker")
    assert calls.index("snapshot-guard-current") < calls.index("close-marker")
    assert calls.index("snapshot-guard-current") < calls.index("pause-producers")
    assert calls.index("pause-producers") < calls.index("drain-active-work")
    assert calls.index("disable-embedded-dispatch") < calls.index(
        "drain-active-work"
    )
    assert calls.index("assert-embedded-dispatch-off") < calls.index(
        "drain-active-work"
    )
    assert calls.index("snapshot-hermes-targets") < calls.index(
        "finalize-snapshot"
    )
    assert calls.index("finalize-snapshot") < calls.index("stage-release")
    assert calls.index("verify-stage") < calls.index("prepare-hermes-runtime")
    assert calls.index("prepare-hermes-runtime") < calls.index(
        "install-hermes-patch"
    )
    assert calls.index("prepare-hermes-runtime") < max(
        index
        for index, name in enumerate(calls)
        if name == "assert-embedded-dispatch-off"
    )
    assert calls.index("install-repository-hooks") < calls.index(
        "run-canary"
    )
    assert calls.index("snapshot-guard-current") < calls.index(
        "write-guard-current"
    )
    assert calls.index("write-guard-current") < calls.index(
        "verify-guard-current"
    )
    assert calls.index("verify-guard-current") < calls.index(
        "install-hermes-patch"
    )
    assert calls.index("install-hermes-patch") < calls.index(
        "verify-hermes-patch"
    )
    assert calls[-6:] == [
        "write-success-receipt",
        "start-supervisor",
        "verify-supervisor-ready",
        "open-marker",
        "restore-producers",
        "release-lock",
    ]
```

같은 RED suite는 target adapter가 snapshot/pause/restore할 producer inventory를 exact set으로 검증한다. Windows는 `\INFINITY_FORGE\Canary`와 `\INFINITY_FORGE\Drift`를 반드시 포함하고, Linux/VPS는 `forge-canary.timer`와 `forge-drift.timer`를 반드시 포함한다. VPS는 여기에 mirror/spec/ledger/flush/morning/backup timer와 외부 outbox writer를 더한다. 이미 running인 Drift publisher를 fake lock으로 주입하면 deploy가 lock 획득 전 mutation 0회로 기다리고, lock을 획득한 뒤에는 Task/timer가 disabled/stopped인 상태에서만 marker를 닫는지 assert한다. success/rollback 모두 snapshot의 enabled/running pair를 exact하게 복원하며 원래 disabled producer를 blind enable하지 않는다.

`tests/ops/test_deployment.py`의 release staging RED는 세 경계를 고정한다. destination `<release-root>/<SHA>`가 없으면 sibling temp directory에 artifact를 extract하고 exact tree inventory/file digest/build manifest를 검증·fsync한 뒤 원자 publish한다. destination이 이미 있으면 manifest source/build digest와 전체 relative path→type→mode→SHA-256 inventory가 candidate와 exact-equal일 때 read-only reuse하고 write/rename call은 0회다. file tamper, missing file, extra file, symlink/reparse point, wrong manifest 중 하나라도 있으면 destination과 current pointer mutation 0회로 exit 2다. rollback 뒤 same-SHA forward deploy가 exact reuse 경로로 성공하는 test를 별도로 둔다.

- [ ] **Step 2: mutation 실패가 rollback하는 RED test 작성**

```python
import pytest

from forge.ops.deployment import DeploymentOperations, deploy_target


def test_failed_hermes_patch_restores_guard_current_then_rolls_back() -> None:
    calls: list[str] = []

    def ok(name: str):
        def invoke() -> None:
            calls.append(name)
        return invoke

    def fail_patch() -> None:
        calls.append("install-hermes-patch")
        raise RuntimeError("Hermes patch install failed")

    def no_receipt():
        raise AssertionError("success receipt must not be written")

    ops = DeploymentOperations(
        verify_build=ok("verify-build"),
        acquire_lock=ok("acquire-lock"),
        close_marker=ok("close-marker"),
        stop_supervisor=ok("stop-supervisor"),
        snapshot_control_plane=ok("snapshot-control-plane"),
        snapshot_guard_current=ok("snapshot-guard-current"),
        pause_producers=ok("pause-producers"),
        disable_embedded_dispatch=ok("disable-embedded-dispatch"),
        apply_gateway_config=ok("apply-gateway-config"),
        assert_embedded_dispatch_off=ok("assert-embedded-dispatch-off"),
        drain_active_work=ok("drain-active-work"),
        backup_database=ok("backup-database"),
        snapshot_hermes_targets=ok("snapshot-hermes-targets"),
        finalize_snapshot=ok("finalize-snapshot"),
        prepare_hermes_runtime=lambda: (
            calls.append("prepare-hermes-runtime") or False
        ),
        stage_release=ok("stage-release"),
        verify_stage=ok("verify-stage"),
        install_repository_hooks=ok("install-repository-hooks"),
        write_guard_current=ok("write-guard-current"),
        verify_guard_current=ok("verify-guard-current"),
        install_hermes_patch=fail_patch,
        verify_hermes_patch=ok("verify-hermes-patch"),
        switch_current=ok("switch-current"),
        install_services=ok("install-services"),
        start_or_restart_gateway=ok("start-or-restart-gateway"),
        verify_database=ok("verify-database"),
        run_canary=ok("run-canary"),
        start_supervisor=ok("start-supervisor"),
        run_drift=ok("run-drift"),
        run_coverage=ok("run-coverage"),
        audit_hashes=ok("audit-hashes"),
        verify_supervisor_ready=ok("verify-supervisor-ready"),
        open_marker=ok("open-marker"),
        restore_producers=ok("restore-producers"),
        write_success_receipt=no_receipt,
        restore_guard_current=ok("restore-guard-current"),
        rollback=ok("rollback"),
        release_lock=ok("release-lock"),
    )

    with pytest.raises(RuntimeError, match="Hermes patch install failed"):
        deploy_target(ops)

    assert calls.index("snapshot-guard-current") < calls.index(
        "write-guard-current"
    )
    assert calls[-3:] == [
        "restore-guard-current",
        "rollback",
        "release-lock",
    ]
```

- [ ] **Step 2a: 별도 프로세스 rollback material 무결성 RED test 작성**

```python
import hashlib
import json
import os
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from forge.ops.contracts import BuildManifest, DeploymentReceipt
from forge.ops.deployment import (
    RollbackOperations,
    VerifiedRollbackContext,
    authorize_clean_hermes_bootstrap,
    build_manifest_bytes,
    deployment_receipt_bytes,
    preserve_before_receipt,
    rollback_target,
    verify_rollback_materials,
)


def test_clean_hermes_bootstrap_requires_finalized_absent_snapshot(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "snapshot"
    blobs = snapshot / "blobs"
    blobs.mkdir(parents=True)
    entries: dict[str, dict[str, str]] = {}
    for name in ("service_definitions", "gateway_state"):
        payload = b"{}\n"
        path = blobs / f"{name}.json"
        path.write_bytes(payload)
        entries[name] = {
            "state": "present",
            "path": f"blobs/{name}.json",
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    for name in (
        "config",
        "current_pointer",
        "previous_deployment_receipt",
        "guard_current",
        "hermes_installation",
        "hermes_targets",
        "database_backup",
    ):
        entries[name] = {"state": "absent"}
    index_bytes = (
        json.dumps(
            {
                "entries": entries,
                "schema_version": "forge-snapshot-index/v1",
                "target": "linux",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    index = snapshot / "snapshot-index.json"
    index.write_bytes(index_bytes)
    digest = hashlib.sha256(index_bytes).hexdigest()
    hermes_root = tmp_path / "hermes-agent"

    authorize_clean_hermes_bootstrap(
        target="linux",
        snapshot_index=index,
        snapshot_sha256=digest,
        hermes_root=hermes_root,
    )
    hermes_root.mkdir()
    with pytest.raises(ValueError, match="Hermes root must be absent"):
        authorize_clean_hermes_bootstrap(
            target="linux",
            snapshot_index=index,
            snapshot_sha256=digest,
            hermes_root=hermes_root,
        )


def _valid_rollback_fixture(
    tmp_path: Path,
    target: str = "vps",
    previous_release: str | None = "0" * 40,
    preexisting_hermes: bool | None = None,
    preexisting_database: bool | None = None,
    bootstrap_owned_uv: bool = False,
) -> tuple[
    DeploymentReceipt,
    BuildManifest,
    Path,
    Path,
    Path,
    Path,
    Path,
]:
    state_root = tmp_path / "state"
    snapshot = state_root / "snapshots" / "deploy-1"
    blobs = snapshot / "blobs"
    blobs.mkdir(parents=True)
    hermes_root = tmp_path / "hermes"
    hermes_root.mkdir()
    if preexisting_hermes is None:
        preexisting_hermes = previous_release is not None
    if preexisting_database is None:
        preexisting_database = previous_release is not None
    if preexisting_hermes != preexisting_database:
        raise ValueError("fixture requires Hermes and DB pre-state to agree")
    if preexisting_hermes and bootstrap_owned_uv:
        raise ValueError("preexisting Hermes cannot own bootstrap uv")
    backup_dir = state_root / "hermes-backups" / "deploy-1"
    target_relative = Path("hermes_cli/kanban_db.py")
    target_before = b"original Hermes target\n"
    target_postimage = b"patched Hermes target\n"
    target_before_sha = hashlib.sha256(target_before).hexdigest()
    target_postimage_sha = hashlib.sha256(target_postimage).hexdigest()
    backup_target = backup_dir / target_relative
    backup_target.parent.mkdir(parents=True)
    backup_target.write_bytes(target_before)
    live_target = hermes_root / target_relative
    live_target.parent.mkdir(parents=True)
    live_target.write_bytes(target_postimage)
    bootstrap_record_path: str | None = None
    bootstrap_record_sha256: str | None = None
    previous_head = "2" * 40
    payloads = {
        "config": b"dispatch_in_gateway: true\n",
        "service_definitions": (
            json.dumps({"target": target, "units": []}, separators=(",", ":"))
            + "\n"
        ).encode("utf-8"),
        "gateway_state": b'{"was_running":true}\n',
    }
    if preexisting_hermes:
        payloads["hermes_installation"] = (
            json.dumps(
                {
                    "approved_base_ref": {"state": "absent"},
                    "head": previous_head,
                    "state": "present",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        payloads["hermes_targets"] = (
            json.dumps(
                {target_relative.as_posix(): target_before_sha},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    entries: dict[str, dict[str, str]] = {}
    for kind, payload in payloads.items():
        relative = f"blobs/{kind}.bin"
        path = snapshot / relative
        path.write_bytes(payload)
        entries[kind] = {
            "state": "present",
            "path": relative,
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    if previous_release is None:
        for kind in (
            "current_pointer",
            "previous_deployment_receipt",
            "guard_current",
        ):
            entries[kind] = {"state": "absent"}
    else:
        release_dir = (
            tmp_path / "data" / "infinity-forge" / "guard" / "releases" / previous_release
        )
        release_dir.mkdir(parents=True)
        previous_artifact = release_dir / "forge-guard.pyz"
        previous_artifact.write_bytes(b"previous guard artifact\n")
        previous_artifact_sha = hashlib.sha256(previous_artifact.read_bytes()).hexdigest()
        runtime_python = tmp_path / ("python.exe" if target == "windows" else "python3")
        runtime_python.write_bytes(b"trusted runtime placeholder\n")
        current_pointer = (
            json.dumps(
                {"release": str(release_dir.resolve()), "source_sha": previous_release},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        guard_current = (
            json.dumps(
                {
                    "policies": {
                        "forge-v1": {
                            "artifact": str(previous_artifact.resolve()),
                            "artifact_sha256": previous_artifact_sha,
                            "python": str(runtime_python.resolve()),
                            "timeout_seconds": 3660,
                        }
                    },
                    "schema_version": "forge-completion-manifest/v1",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        previous_receipt = (
            json.dumps(
                {
                    "build_manifest_sha256": "8" * 64,
                    "current_release": previous_release,
                    "guard_current_sha256": hashlib.sha256(guard_current).hexdigest(),
                    "hermes_bootstrap_record_path": None,
                    "hermes_bootstrap_record_sha256": None,
                    "hermes_patch_install_record_path": "historical-record.json",
                    "hermes_patch_install_record_sha256": "7" * 64,
                    "installed_at_utc": "2026-07-11T05:00:00Z",
                    "previous_release": None,
                    "slack_alert_env_sha256": "e" * 64,
                    "repository_hook_hashes": [],
                    "result": "success",
                    "schema_version": "deployment-receipt-v1",
                    "snapshot_bundle_path": "historical-snapshot",
                    "snapshot_bundle_sha256": "6" * 64,
                    "target": target,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        for kind, payload in {
            "current_pointer": current_pointer,
            "previous_deployment_receipt": previous_receipt,
            "guard_current": guard_current,
        }.items():
            relative = f"blobs/{kind}.bin"
            path = snapshot / relative
            path.write_bytes(payload)
            entries[kind] = {
                "state": "present",
                "path": relative,
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
    if preexisting_database:
        database_backup = blobs / "database_backup.sqlite3"
        with sqlite3.connect(database_backup) as connection:
            connection.execute("CREATE TABLE rollback_probe (value TEXT NOT NULL)")
            connection.execute("INSERT INTO rollback_probe VALUES ('ok')")
        database_payload = database_backup.read_bytes()
        entries["database_backup"] = {
            "state": "present",
            "path": "blobs/database_backup.sqlite3",
            "sha256": hashlib.sha256(database_payload).hexdigest(),
        }
    else:
        entries["hermes_installation"] = {"state": "absent"}
        entries["hermes_targets"] = {"state": "absent"}
        entries["database_backup"] = {"state": "absent"}
    index = snapshot / "snapshot-index.json"
    index_bytes = (
        json.dumps(
            {
                "schema_version": "forge-snapshot-index/v1",
                "target": target,
                "entries": entries,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    index.write_bytes(index_bytes)
    if not preexisting_hermes:
        kanban_db = tmp_path / "home" / ".hermes" / "kanban.db"
        kanban_db.parent.mkdir(parents=True)
        kanban_db.write_bytes(b"SQLite format 3\x00")
        owned_paths = [str(hermes_root.resolve()), str(kanban_db.resolve())]
        if bootstrap_owned_uv:
            uv_bootstrap = tmp_path / "data" / "infinity-forge" / "bootstrap" / "uv-0.11.24"
            uv_bootstrap.mkdir(parents=True)
            owned_paths.append(str(uv_bootstrap.resolve()))
        bootstrap_record = (
            state_root / "deployments" / f"hermes-bootstrap-{target}.json"
        )
        bootstrap_record.parent.mkdir(parents=True)
        bootstrap_record_bytes = (
            json.dumps(
                {
                    "owned_paths": sorted(owned_paths),
                    "root": str(hermes_root.resolve()),
                    "schema_version": "hermes-bootstrap-journal/v1",
                    "snapshot_index": str(index.resolve()),
                    "snapshot_sha256": hashlib.sha256(index_bytes).hexdigest(),
                    "source_sha": "1" * 40,
                    "stage": "complete",
                    "target": target,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        bootstrap_record.write_bytes(bootstrap_record_bytes)
        bootstrap_record_path = str(bootstrap_record.resolve())
        bootstrap_record_sha256 = hashlib.sha256(bootstrap_record_bytes).hexdigest()
    patch_manifest_path = tmp_path / "hermes-patch-manifest.json"
    patch_manifest_bytes = (
        json.dumps(
            {
                "schema_version": "forge-hermes-patch/v1",
                "hermes_version": "0.18.2",
                "upstream_base": "4281151ae859241351ba14d8c7682dc67ff4c126",
                "target_files": ["hermes_cli/kanban_db.py"],
                "patch_sha256": "7" * 64,
                "variants": {
                    "1" * 40: {
                        "ast_preimages": {
                            name: "d" * 64
                            for name in (
                                "Task",
                                "_migrate_add_optional_columns",
                                "create_task",
                                "recompute_ready",
                                "complete_task",
                                "edit_completed_task_result",
                                "detect_crashed_workers",
                            )
                        },
                        "target_preimage_sha256": {
                            "hermes_cli/kanban_db.py": target_before_sha
                        },
                        "target_postimage_sha256": {
                            "hermes_cli/kanban_db.py": target_postimage_sha
                        },
                    }
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    patch_manifest_path.write_bytes(patch_manifest_bytes)
    patch_manifest_sha = hashlib.sha256(patch_manifest_bytes).hexdigest()
    patch_record = state_root / "deployments" / f"hermes-patch-{target}.json"
    patch_record.parent.mkdir(parents=True)
    patch_record_bytes = (
        json.dumps(
            {
                "schema_version": "forge-hermes-install-record/v1",
                "root": str(hermes_root.resolve()),
                "base_blob": "1" * 40,
                "previous_head": previous_head,
                "patch_commit": "3" * 40,
                "manifest_sha256": patch_manifest_sha,
                "patch_sha256": "7" * 64,
                "target_files": ["hermes_cli/kanban_db.py"],
                "target_before_sha256": {
                    "hermes_cli/kanban_db.py": target_before_sha
                },
                "target_postimage_sha256": {
                    "hermes_cli/kanban_db.py": target_postimage_sha
                },
                "backup_dir": str(backup_dir.resolve()),
                "installed_at": 1_783_828_800,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    patch_record.write_bytes(patch_record_bytes)
    build = BuildManifest(
        schema_version="build-manifest-v1",
        source_sha="1" * 40,
        archive_sha256="2" * 64,
        guard_sha256="3" * 64,
        requirements_lock_sha256="a" * 64,
        python_requires=">=3.11",
        schema_hashes=(("receipt-v1.schema.json", "4" * 64),),
        hermes_patch_manifest_sha256=patch_manifest_sha,
        hermes_patch_sha256="7" * 64,
    )
    build_digest = hashlib.sha256(build_manifest_bytes(build)).hexdigest()
    receipt = DeploymentReceipt(
        schema_version="deployment-receipt-v1",
        build_manifest_sha256=build_digest,
        target=target,
        installed_at_utc="2026-07-12T05:00:00Z",
        previous_release=previous_release,
        current_release="1" * 40,
        guard_current_sha256="9" * 64,
        snapshot_bundle_path=str(snapshot),
        snapshot_bundle_sha256=hashlib.sha256(index_bytes).hexdigest(),
        hermes_patch_install_record_path=str(patch_record),
        hermes_patch_install_record_sha256=hashlib.sha256(patch_record_bytes).hexdigest(),
        hermes_bootstrap_record_path=bootstrap_record_path,
        hermes_bootstrap_record_sha256=bootstrap_record_sha256,
        slack_alert_env_sha256="e" * 64,
        repository_hook_hashes=((str(tmp_path / "repo"), "a" * 64),),
        result="success",
    )
    return (
        receipt,
        build,
        state_root,
        hermes_root,
        snapshot / "blobs/config.bin",
        patch_record,
        patch_manifest_path,
    )


def test_separate_process_rollback_refuses_tampered_snapshot_or_patch_record(
    tmp_path: Path,
) -> None:
    receipt, build, state_root, hermes_root, config, patch_record, patch_manifest_path = (
        _valid_rollback_fixture(tmp_path)
    )

    verify_rollback_materials(
        receipt,
        target="vps",
        state_root=state_root,
        hermes_root=hermes_root,
        build=build,
        patch_manifest_path=patch_manifest_path,
    )
    with pytest.raises(ValueError, match="build manifest SHA-256 mismatch"):
        verify_rollback_materials(
            receipt,
            target="vps",
            state_root=state_root,
            hermes_root=hermes_root,
            build=replace(build, source_sha="9" * 40),
            patch_manifest_path=patch_manifest_path,
        )
    index_path = Path(receipt.snapshot_bundle_path) / "snapshot-index.json"
    original_index = index_path.read_bytes()
    missing_index = json.loads(original_index)
    del missing_index["entries"]["database_backup"]
    missing_index_bytes = (
        json.dumps(missing_index, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    index_path.write_bytes(missing_index_bytes)
    missing_entry_receipt = replace(
        receipt,
        snapshot_bundle_sha256=hashlib.sha256(missing_index_bytes).hexdigest(),
    )
    with pytest.raises(ValueError, match="required snapshot entries"):
        verify_rollback_materials(
            missing_entry_receipt,
            target="vps",
            state_root=state_root,
            hermes_root=hermes_root,
            build=build,
            patch_manifest_path=patch_manifest_path,
        )
    index_path.write_bytes(original_index)
    config.write_bytes(b"tampered: true\n")
    with pytest.raises(ValueError, match="snapshot file digest mismatch"):
        verify_rollback_materials(
            receipt,
            target="vps",
            state_root=state_root,
            hermes_root=hermes_root,
            build=build,
            patch_manifest_path=patch_manifest_path,
        )
    config.write_bytes(b"dispatch_in_gateway: true\n")
    record_data = json.loads(patch_record.read_text(encoding="utf-8"))
    record_data["root"] = str((tmp_path / "other-hermes").resolve())
    rebound_record = (
        json.dumps(record_data, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    patch_record.write_bytes(rebound_record)
    rebound_receipt = replace(
        receipt,
        hermes_patch_install_record_sha256=hashlib.sha256(
            rebound_record
        ).hexdigest(),
    )
    with pytest.raises(ValueError, match="install record root mismatch"):
        verify_rollback_materials(
            rebound_receipt,
            target="vps",
            state_root=state_root,
            hermes_root=hermes_root,
            build=build,
            patch_manifest_path=patch_manifest_path,
        )
    patch_record.write_bytes(b"{}\n")
    with pytest.raises(ValueError, match="install record SHA-256 mismatch"):
        verify_rollback_materials(
            receipt,
            target="vps",
            state_root=state_root,
            hermes_root=hermes_root,
            build=build,
            patch_manifest_path=patch_manifest_path,
        )


def test_clean_rollback_binds_complete_bootstrap_journal(tmp_path: Path) -> None:
    receipt, build, state_root, hermes_root, _, _, patch_manifest_path = (
        _valid_rollback_fixture(tmp_path, previous_release=None)
    )
    assert receipt.hermes_bootstrap_record_path is not None
    record = Path(receipt.hermes_bootstrap_record_path)
    original = record.read_bytes()
    verify_rollback_materials(
        receipt,
        target="vps",
        state_root=state_root,
        hermes_root=hermes_root,
        build=build,
        patch_manifest_path=patch_manifest_path,
    )

    record.write_bytes(b"{}\n")
    with pytest.raises(ValueError, match="bootstrap record SHA-256 mismatch"):
        verify_rollback_materials(
            receipt,
            target="vps",
            state_root=state_root,
            hermes_root=hermes_root,
            build=build,
            patch_manifest_path=patch_manifest_path,
        )

    for key, value, message in (
        ("stage", "runtime", "bootstrap stage mismatch"),
        ("source_sha", "9" * 40, "bootstrap source SHA mismatch"),
        ("root", str((tmp_path / "other").resolve()), "bootstrap root mismatch"),
        (
            "owned_paths",
            [str((tmp_path / "unrelated").resolve())],
            "bootstrap owned paths mismatch",
        ),
    ):
        payload = json.loads(original)
        payload[key] = value
        changed = (
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        record.write_bytes(changed)
        rebound = replace(
            receipt,
            hermes_bootstrap_record_sha256=hashlib.sha256(changed).hexdigest(),
        )
        with pytest.raises(ValueError, match=message):
            verify_rollback_materials(
                rebound,
                target="vps",
                state_root=state_root,
                hermes_root=hermes_root,
                build=build,
                patch_manifest_path=patch_manifest_path,
            )
    record.write_bytes(original)


@pytest.mark.parametrize("bootstrap_owned_uv", [False, True])
def test_clean_rollback_accepts_only_actually_owned_uv_subset(
    tmp_path: Path,
    bootstrap_owned_uv: bool,
) -> None:
    receipt, build, state_root, hermes_root, _, _, patch_manifest_path = (
        _valid_rollback_fixture(
            tmp_path,
            previous_release=None,
            bootstrap_owned_uv=bootstrap_owned_uv,
        )
    )
    record = json.loads(Path(receipt.hermes_bootstrap_record_path).read_bytes())
    expected_count = 3 if bootstrap_owned_uv else 2
    assert len(record["owned_paths"]) == expected_count
    verify_rollback_materials(
        receipt,
        target="vps",
        state_root=state_root,
        hermes_root=hermes_root,
        build=build,
        patch_manifest_path=patch_manifest_path,
    )


def test_first_forge_release_preserves_preexisting_vps_hermes_and_database(
    tmp_path: Path,
) -> None:
    receipt, build, state_root, hermes_root, _, _, patch_manifest_path = (
        _valid_rollback_fixture(
            tmp_path,
            previous_release=None,
            preexisting_hermes=True,
            preexisting_database=True,
        )
    )
    assert receipt.previous_release is None
    assert receipt.hermes_bootstrap_record_path is None
    index = json.loads(
        (Path(receipt.snapshot_bundle_path) / "snapshot-index.json").read_bytes()
    )
    assert index["entries"]["current_pointer"]["state"] == "absent"
    assert index["entries"]["hermes_installation"]["state"] == "present"
    assert index["entries"]["database_backup"]["state"] == "present"
    verify_rollback_materials(
        receipt,
        target="vps",
        state_root=state_root,
        hermes_root=hermes_root,
        build=build,
        patch_manifest_path=patch_manifest_path,
    )


def _rebind_snapshot_entry(
    receipt: DeploymentReceipt,
    entry_name: str,
    payload: bytes,
) -> DeploymentReceipt:
    index_path = Path(receipt.snapshot_bundle_path) / "snapshot-index.json"
    index_data = json.loads(index_path.read_bytes())
    entry = index_data["entries"][entry_name]
    entry_path = Path(receipt.snapshot_bundle_path) / entry["path"]
    entry_path.write_bytes(payload)
    entry["sha256"] = hashlib.sha256(payload).hexdigest()
    index_bytes = (
        json.dumps(index_data, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    index_path.write_bytes(index_bytes)
    return replace(
        receipt,
        snapshot_bundle_sha256=hashlib.sha256(index_bytes).hexdigest(),
    )


def test_rollback_materials_bind_previous_release_and_restorable_bytes(
    tmp_path: Path,
) -> None:
    receipt, build, state_root, hermes_root, _, _, patch_manifest_path = (
        _valid_rollback_fixture(tmp_path)
    )
    rebound_pointer = (
        json.dumps(
            {"release": "/wrong/release", "source_sha": "9" * 40},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    rebound_receipt = _rebind_snapshot_entry(
        receipt, "current_pointer", rebound_pointer
    )
    with pytest.raises(ValueError, match="previous release snapshot binding"):
        verify_rollback_materials(
            rebound_receipt,
            target="vps",
            state_root=state_root,
            hermes_root=hermes_root,
            build=build,
            patch_manifest_path=patch_manifest_path,
        )

    receipt, build, state_root, hermes_root, _, _, patch_manifest_path = (
        _valid_rollback_fixture(tmp_path / "backup")
    )
    backup_target = (
        state_root / "hermes-backups" / "deploy-1" / "hermes_cli" / "kanban_db.py"
    )
    backup_target.write_bytes(b"tampered rollback source\n")
    with pytest.raises(ValueError, match="target backup digest mismatch"):
        verify_rollback_materials(
            receipt,
            target="vps",
            state_root=state_root,
            hermes_root=hermes_root,
            build=build,
            patch_manifest_path=patch_manifest_path,
        )


def test_rollback_materials_require_valid_sqlite_backup_and_current_postimage(
    tmp_path: Path,
) -> None:
    receipt, build, state_root, hermes_root, _, _, patch_manifest_path = (
        _valid_rollback_fixture(tmp_path)
    )
    invalid_db_receipt = _rebind_snapshot_entry(
        receipt, "database_backup", b"not a SQLite database\n"
    )
    with pytest.raises(ValueError, match="database backup quick_check"):
        verify_rollback_materials(
            invalid_db_receipt,
            target="vps",
            state_root=state_root,
            hermes_root=hermes_root,
            build=build,
            patch_manifest_path=patch_manifest_path,
        )

    receipt, build, state_root, hermes_root, _, _, patch_manifest_path = (
        _valid_rollback_fixture(tmp_path / "postimage")
    )
    (hermes_root / "hermes_cli" / "kanban_db.py").write_bytes(b"unexpected\n")
    with pytest.raises(ValueError, match="current target postimage mismatch"):
        verify_rollback_materials(
            receipt,
            target="vps",
            state_root=state_root,
            hermes_root=hermes_root,
            build=build,
            patch_manifest_path=patch_manifest_path,
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink test")
def test_rollback_materials_reject_posix_symlink_component(tmp_path: Path) -> None:
    receipt, build, state_root, hermes_root, _, _, patch_manifest_path = (
        _valid_rollback_fixture(tmp_path)
    )
    link = state_root / "snapshots" / "link"
    link.symlink_to(Path(receipt.snapshot_bundle_path), target_is_directory=True)
    escaped = replace(receipt, snapshot_bundle_path=str(link))
    with pytest.raises(ValueError, match="symlink or reparse point"):
        verify_rollback_materials(
            escaped,
            target="vps",
            state_root=state_root,
            hermes_root=hermes_root,
            build=build,
            patch_manifest_path=patch_manifest_path,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows junction test")
def test_rollback_materials_reject_windows_junction_component(tmp_path: Path) -> None:
    receipt, build, state_root, hermes_root, _, _, patch_manifest_path = _valid_rollback_fixture(
        tmp_path, target="windows"
    )
    link = state_root / "snapshots" / "junction"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), receipt.snapshot_bundle_path],
        check=True,
        capture_output=True,
        text=True,
    )
    escaped = replace(receipt, snapshot_bundle_path=str(link))
    with pytest.raises(ValueError, match="symlink or reparse point"):
        verify_rollback_materials(
            escaped,
            target="windows",
            state_root=state_root,
            hermes_root=hermes_root,
            build=build,
            patch_manifest_path=patch_manifest_path,
        )


def test_same_before_after_path_is_preserved_before_overwrite(tmp_path: Path) -> None:
    receipt, _, state_root, _, _, _, _ = _valid_rollback_fixture(tmp_path)
    fixed_after = state_root / "deployment-receipt-v1.json"
    original = deployment_receipt_bytes(receipt)
    fixed_after.write_bytes(original)

    preserved = preserve_before_receipt(fixed_after, state_root=state_root)
    fixed_after.write_bytes(b'{"result":"rolled-back"}\n')

    assert preserved != fixed_after
    assert preserved.parent == state_root / "rollback-inputs"
    assert preserved.read_bytes() == original


@pytest.mark.parametrize("target", ["windows", "linux", "vps"])
def test_each_target_verifies_materials_before_first_rollback_mutation(
    target: str,
    tmp_path: Path,
) -> None:
    receipt, _, _, _, _, _, _ = _valid_rollback_fixture(tmp_path, target=target)
    rolled_back = replace(
        receipt,
        current_release=receipt.previous_release,
        result="rolled-back",
    )
    calls: list[str] = []

    def action(name: str):
        def invoke() -> None:
            calls.append(name)
        return invoke

    def prepare() -> VerifiedRollbackContext:
        calls.append("prepare-materials")
        return VerifiedRollbackContext(
            preserved_before_receipt=(
                tmp_path / "state" / "rollback-inputs" / "before.json"
            ),
            receipt=receipt,
            previous_release=receipt.previous_release,
            gateway_was_running=True,
        )

    def write_receipt(context: VerifiedRollbackContext) -> DeploymentReceipt:
        assert context.previous_release == receipt.previous_release
        calls.append("write-after-receipt")
        return rolled_back

    result = rollback_target(
        RollbackOperations(
            target=target,
            acquire_lock=action("acquire-lock"),
            prepare_materials=prepare,
            pause_producers=action("pause-producers"),
            close_marker=action("close-marker"),
            stop_supervisor=action("stop-supervisor"),
            stop_gateway=action("stop-gateway"),
            restore_guard_current=action("restore-guard-current"),
            restore_snapshot=action("restore-snapshot"),
            reload_services=action("reload-services"),
            start_gateway=action("start-gateway"),
            ensure_gateway_stopped=action("ensure-gateway-stopped"),
            verify_database_state=action("verify-database-state"),
            run_previous_canary=action("run-previous-canary"),
            verify_absent_release_state=action("verify-absent-release-state"),
            activate_previous_dispatcher=action("activate-previous-dispatcher"),
            restore_producers=action("restore-producers"),
            write_after_receipt=write_receipt,
            release_lock=action("release-lock"),
        )
    )

    assert result.result == "rolled-back"
    assert calls[:4] == [
        "acquire-lock",
        "prepare-materials",
        "pause-producers",
        "close-marker",
    ]
    assert calls[-4:] == [
        "write-after-receipt",
        "activate-previous-dispatcher",
        "restore-producers",
        "release-lock",
    ]


def test_clean_host_rollback_records_absence_and_skips_old_canary(
    tmp_path: Path,
) -> None:
    receipt, build, state_root, hermes_root, _, _, patch_manifest_path = _valid_rollback_fixture(
        tmp_path, target="linux", previous_release=None
    )
    verify_rollback_materials(
        receipt,
        target="linux",
        state_root=state_root,
        hermes_root=hermes_root,
        build=build,
        patch_manifest_path=patch_manifest_path,
    )
    clean_receipt = replace(
        receipt,
        previous_release=None,
        current_release=None,
        guard_current_sha256=None,
        result="rolled-back",
    )
    calls: list[str] = []

    def action(name: str):
        def invoke() -> None:
            calls.append(name)
        return invoke

    result = rollback_target(
        RollbackOperations(
            target="linux",
            acquire_lock=action("acquire-lock"),
            prepare_materials=lambda: VerifiedRollbackContext(
                preserved_before_receipt=tmp_path / "preserved-before.json",
                receipt=receipt,
                previous_release=None,
                gateway_was_running=False,
            ),
            pause_producers=action("pause-producers"),
            close_marker=action("close-marker"),
            stop_supervisor=action("stop-supervisor"),
            stop_gateway=action("stop-gateway"),
            restore_guard_current=action("restore-guard-current"),
            restore_snapshot=action("restore-snapshot"),
            reload_services=action("reload-services"),
            start_gateway=action("start-gateway"),
            ensure_gateway_stopped=action("ensure-gateway-stopped"),
            verify_database_state=action("verify-database-state"),
            run_previous_canary=action("run-previous-canary"),
            verify_absent_release_state=action("verify-absent-release-state"),
            activate_previous_dispatcher=action("activate-previous-dispatcher"),
            restore_producers=action("restore-producers"),
            write_after_receipt=lambda context: clean_receipt,
            release_lock=action("release-lock"),
        )
    )

    assert result.current_release is None
    assert result.guard_current_sha256 is None
    assert "start-gateway" not in calls
    assert "run-previous-canary" not in calls
    assert "activate-previous-dispatcher" not in calls
    assert "ensure-gateway-stopped" in calls
    assert "verify-absent-release-state" in calls
    assert calls.index("pause-producers") < calls.index("restore-snapshot")
    assert calls.index("write-after-receipt") < calls.index("restore-producers")
```

- [ ] **Step 3: wrapper와 CLI의 exact interface를 고정하는 RED test 작성**

```python
from pathlib import Path

from forge.ops.deployment import build_parser


ROOT = Path(__file__).resolve().parents[2]


def test_deploy_and_linux_rollback_script_contracts() -> None:
    deploy = (ROOT / "forge" / "scripts" / "deploy.ps1").read_text(
        encoding="utf-8"
    )
    for declaration in (
        "[string]$Sha",
        "[string]$Artifact",
        "[string]$ArtifactSha256",
        "[string]$BuildManifest",
        "[string[]]$Targets",
        "[string[]]$RepoPaths",
        "[string]$BootstrapRepository",
        "[switch]$PlanOnly",
        "[switch]$Apply",
    ):
        assert declaration in deploy
    assert '[CmdletBinding(DefaultParameterSetName="Plan")]' in deploy
    assert '[Parameter(ParameterSetName="Plan")]' in deploy
    assert '[Parameter(ParameterSetName="Apply")]' in deploy
    assert '[ValidateSet("Windows","Linux","Vps")]' in deploy
    assert "^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$" in deploy
    bootstrap_declaration = deploy.index("[string]$BootstrapRepository")
    assert "[Parameter(Mandatory=$true)]" in deploy[
        bootstrap_declaration - 160 : bootstrap_declaration
    ]
    assert "-BootstrapRepository $BootstrapRepository" in deploy
    assert deploy.index("finalize-snapshot") < deploy.index("ensure-approved-base")
    assert deploy.index("ensure-approved-base") < deploy.index("hermes-patch.py status")
    assert "restore-approved-base-ref" in deploy
    assert 'evidence\\linux-deployment-receipt.json' in deploy
    assert 'evidence\\vps-deployment-receipt.json' in deploy
    assert "record-evidence" in deploy

    deployment = (ROOT / "forge" / "ops" / "deployment.py").read_text(
        encoding="utf-8"
    )
    for command in (
        "build",
        "verify-build",
        "compare-hermes-status",
        "authorize-clean-hermes-bootstrap",
        "verify-rollback",
        "record-evidence",
        "audit-targets",
    ):
        assert f'add_parser("{command}")' in deployment
    bootstrap_parser = build_parser().parse_args(
        [
            "authorize-clean-hermes-bootstrap",
            "--target",
            "linux",
            "--snapshot-index",
            "/tmp/snapshot/snapshot-index.json",
            "--snapshot-sha256",
            "a" * 64,
            "--hermes-root",
            "/home/test/.hermes/hermes-agent",
        ]
    )
    assert bootstrap_parser.target == "linux"
    tree = ast.parse(deployment)
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for adapter in ("rollback_windows_target", "rollback_linux_target"):
        function = functions[adapter]
        assert any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "rollback_target"
            for node in ast.walk(function)
        )
        operation_calls = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "RollbackOperations"
        ]
        assert len(operation_calls) == 1
        prepare_keywords = [
            keyword.value
            for keyword in operation_calls[0].keywords
            if keyword.arg == "prepare_materials"
        ]
        assert len(prepare_keywords) == 1
        assert any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "prepare_rollback_materials"
            for node in ast.walk(prepare_keywords[0])
        )

    hermes_patch = (
        ROOT / "forge" / "scripts" / "hermes-patch.py"
    ).read_text(encoding="utf-8")
    assert 'add_parser("verify")' in hermes_patch
    for flag in (
        "--root",
        "--manifest",
        "--record",
        "--current-manifest",
        "--expected-source-sha",
    ):
        assert f'add_argument("{flag}", required=True)' in hermes_patch

    rollback_linux = (
        ROOT / "forge" / "scripts" / "rollback-linux.sh"
    ).read_text(encoding="utf-8")
    assert "verify-rollback" in rollback_linux
    assert "guard/current.json" in rollback_linux
    assert "preserve_before_receipt" in rollback_linux
    assert 'PYTHONPATH="$tool_release" /usr/bin/python3' in rollback_linux

    rollback_vps = (
        ROOT / "forge" / "scripts" / "rollback-vps.sh"
    ).read_text(encoding="utf-8")
    assert (
        'exec "$(dirname "$0")/rollback-linux.sh" --target vps "$@"'
        in rollback_vps
    )


def test_windows_rollback_wrapper_contract() -> None:
    rollback_windows = (
        ROOT / "forge" / "scripts" / "rollback.ps1"
    ).read_text(encoding="utf-8")
    for declaration in (
        "[string]$BeforeReceipt",
        "[string]$BuildManifest",
        "[string[]]$RepoPaths",
    ):
        assert declaration in rollback_windows
    assert "rollback_windows_target" in rollback_windows
    assert "preserve_before_receipt" in rollback_windows
    assert "verify-rollback" in rollback_windows
    assert "--target windows" in rollback_windows
    assert '"deployment-receipt-v1.json"' in rollback_windows
    assert r"InfinityForge\guard\current.json" in rollback_windows
    assert "policies.'forge-v1'.python" in rollback_windows
    assert "$env:PYTHONPATH = $ProjectRoot" in rollback_windows
    assert ".venv\\Scripts\\python.exe" not in rollback_windows
```

- [ ] **Step 4: RED 확인**

Run: `.\.venv\Scripts\python.exe -m pytest tests/ops/test_deployment.py -q`

Expected: `ImportError: cannot import name 'DeploymentOperations'`로 FAIL.

- [ ] **Step 5: deploy transaction 최소 구현**

```python
from forge.ops.contracts import DeploymentReceipt


def deploy_target(ops: DeploymentOperations) -> DeploymentReceipt:
    ops.verify_build()
    ops.acquire_lock()
    mutated = False
    guard_current_mutation_started = False
    try:
        ops.snapshot_control_plane()
        ops.snapshot_guard_current()
        mutated = True
        ops.pause_producers()
        ops.close_marker()
        ops.stop_supervisor()
        ops.disable_embedded_dispatch()
        ops.apply_gateway_config()
        ops.assert_embedded_dispatch_off()
        ops.drain_active_work()
        ops.backup_database()
        ops.snapshot_hermes_targets()
        ops.finalize_snapshot()
        ops.stage_release()
        ops.verify_stage()
        clean_bootstrapped = ops.prepare_hermes_runtime()
        if clean_bootstrapped:
            ops.apply_gateway_config()
            ops.assert_embedded_dispatch_off()
        ops.install_repository_hooks()
        guard_current_mutation_started = True
        ops.write_guard_current()
        ops.verify_guard_current()
        ops.install_hermes_patch()
        ops.verify_hermes_patch()
        ops.switch_current()
        ops.install_services()
        ops.start_or_restart_gateway()
        ops.verify_database()
        ops.run_canary()
        ops.run_drift()
        ops.run_coverage()
        ops.audit_hashes()
        receipt = ops.write_success_receipt()
        ops.start_supervisor()
        ops.verify_supervisor_ready()
        ops.open_marker()
        ops.restore_producers()
        return receipt
    except BaseException:
        if guard_current_mutation_started:
            ops.restore_guard_current()
        if mutated:
            ops.rollback()
        raise
    finally:
        ops.release_lock()
```

- [ ] **Step 5a: rollback material 선검증과 복원 state machine 구현**

```python
def rollback_target(ops: RollbackOperations) -> DeploymentReceipt:
    if ops.target not in {"windows", "linux", "vps"}:
        raise ValueError(f"unsupported rollback target: {ops.target}")
    ops.acquire_lock()
    try:
        context = ops.prepare_materials()
        if context.receipt.target != ops.target:
            raise ValueError("verified rollback target mismatch")
        ops.pause_producers()
        ops.close_marker()
        ops.stop_supervisor()
        ops.stop_gateway()
        ops.restore_guard_current()
        ops.restore_snapshot()
        ops.reload_services()
        if context.gateway_was_running:
            ops.start_gateway()
        else:
            ops.ensure_gateway_stopped()
        ops.verify_database_state()
        if context.previous_release is None:
            ops.verify_absent_release_state()
        else:
            ops.run_previous_canary()
        receipt = ops.write_after_receipt(context)
        if receipt.target != ops.target or receipt.result != "rolled-back":
            raise ValueError("rollback receipt target/result mismatch")
        if receipt.current_release != context.previous_release:
            raise ValueError("rollback receipt current release mismatch")
        if context.previous_release is None and receipt.guard_current_sha256 is not None:
            raise ValueError("clean-host rollback must record absent guard manifest")
        if context.previous_release is not None:
            ops.activate_previous_dispatcher()
        ops.restore_producers()
        return receipt
    finally:
        ops.release_lock()
```

`preserve_before_receipt`는 입력 bytes를 mutation 전에 읽어 SHA-256을 계산하고 `<state-root>/rollback-inputs/<digest>.json`에 temp→flush→fsync→exclusive replace로 보존한다. 같은 digest 파일이 이미 있으면 bytes가 exact-equal일 때만 재사용한다. `prepare_rollback_materials`는 이 immutable copy를 strict schema parse한 뒤 `verify_rollback_materials`를 호출하고 preserved path, verified receipt, previous release, gateway pre-state만 담은 immutable `VerifiedRollbackContext`를 반환한다. Windows/Linux/VPS adapter는 `RollbackOperations.prepare_materials`에서 이 함수를 호출하며 direct library 호출도 우회할 수 없다. state machine과 after-receipt writer는 이 context만 소비하고 unverified duplicate scalar를 받지 않는다.

- [ ] **Step 6: host adapter와 rollback 순서 구현**

`snapshot_control_plane`과 `snapshot_guard_current`는 lock 획득 직후 marker close보다 먼저 config, current pointer, previous deployment receipt, guard current, service/Task 정의, gateway pre-state, producer enable/running pre-state를 staging snapshot에 수집하지만 아직 index를 확정하지 않는다. 이어 `pause_producers`가 Windows Canary/Drift Scheduled Task, POSIX canary/drift timer, VPS mirror/spec/ledger/flush/morning/backup timer와 외부 outbox writer를 fail-loud하게 disable/stop하고 실행 중 process 종료를 확인한 뒤 marker close→supervisor stop→embedded dispatcher disable→active work drain을 수행한다. hourly Drift publisher도 같은 deployment lock을 nonblocking으로 사용하므로 이미 publisher가 lock을 잡은 동안 deploy mutation은 0회이고, deploy가 lock을 잡은 뒤에는 publisher Task가 pause되어 새 실행이 시작되지 않는다. drain 뒤 `backup_database`는 SQLite backup API로 동일 DB의 일관된 copy를 생성하고 fsync한 뒤 read-only `PRAGMA quick_check=ok`와 digest를 반환한다. `snapshot_hermes_targets`가 installation HEAD 및 patch target 원본 bytes/digest를 수집한 다음 `finalize_snapshot`이 exact nine entry를 한 번에 canonical index로 봉인한다. audit까지 marker/supervisor/producers는 닫힌 상태를 유지한다. success receipt를 temp→flush→fsync→replace→directory fsync로 먼저 내구화한 뒤 supervisor를 closed marker 아래 시작·ready 검증하고, marker를 원자 open한 다음에만 snapshot의 producer enable/running 상태를 복원한다. 이 activation 단계가 실패하면 marker를 다시 닫고 producers/supervisor를 멈춘 상태에서 rollback하여 fixed receipt를 `rolled-back`으로 덮는다. 따라서 durable success receipt 전에는 새 work가 생성·dispatch되지 않는다. 중간 실패 rollback도 같은 producer snapshot을 복원하며 blind enable은 금지한다.

`snapshot-index.json`은 `schema_version=forge-snapshot-index/v1`, exact target, exact nine entry keys `config|current_pointer|previous_deployment_receipt|guard_current|service_definitions|gateway_state|hermes_installation|hermes_targets|database_backup`만 가진 canonical JSON이다. 각 entry는 `state=present`와 relative path/SHA-256 또는 `state=absent`만 허용하며 `service_definitions`, `gateway_state`는 항상 present다. preexisting Hermes/DB가 없던 clean host는 `hermes_installation|hermes_targets|database_backup=absent`로 기록한다. preexisting Hermes의 `hermes_installation` payload는 exact `state,head,approved_base_ref`를 가지며 ref는 `{state:absent}` 또는 `{state:present,sha:SHA40}`다. `hermes_targets` payload의 path set은 patch manifest target set과 exact-equal이어야 한다. index digest가 receipt의 `snapshot_bundle_sha256`이며 모든 path component에 대해 POSIX symlink/Windows reparse point, 절대경로, `..`, 중복, 목록 밖 파일, canonical root escape를 거절한다. `guard_current`가 배포 전 없었다면 absent entry로 기록한다. snapshot file과 index는 temp→flush→fsync→replace하고 directory를 fsync한다. finalized index 뒤 candidate immutable release를 먼저 `stage_release`/`verify_stage`한다. stage destination이 absent면 sibling temp에 extract→exact inventory/hash 검증→file/dir fsync→atomic rename→release-root fsync하고, existing이면 candidate inventory와 byte-for-byte exact-equal일 때만 read-only reuse한다. mismatch/extra/symlink는 기존 release를 덮어쓰거나 삭제하지 않고 fail closed다. `prepare_hermes_runtime`은 바로 그 verified staged/reused release를 사용한다. existing Windows/Linux/VPS Hermes면 snapshot 뒤 cross-platform `ensure-approved-base`를 호출하고 ancestry가 아닌 supported target blob/AST/preimage 기반 read-only `status`를 검증한다. absent면 Task 6 `hermes` phase에 index/digest authorization을 전달해 checkout/venv/DB를 만든다. rollback은 approved ref가 pre-state absent였으면 create된 ref를 delete하고 present였으면 exact SHA로 restore한 뒤 target/head 상태를 검증한다. `restore_guard_current`는 snapshot bytes를 같은 방식으로 원자 복원하거나 absent인 경우 새로 생긴 `current.json`을 삭제한다. `write_guard_current` 직전부터 guard mutation flag를 세우므로 write나 verify 자체가 실패해도 반드시 이 복원 callback을 거친다.

`authorize-clean-hermes-bootstrap`은 snapshot index canonical bytes의 SHA-256이 `--snapshot-sha256`과 exact-equal인지, target과 exact nine-entry schema가 맞는지, 모든 present blob digest/path containment가 맞는지 먼저 검증한다. 이어 `hermes_installation`, `hermes_targets`, `database_backup`이 모두 absent이고 requested Hermes root 자체가 존재하지 않으며 parent chain에 symlink/reparse point가 없을 때만 exit 0한다. 이 명령은 filesystem을 변경하지 않고, 실패 시 installer는 clone/venv/DB mutation 전에 exit 2한다.

`disable_embedded_dispatch`는 config를 temp→flush→fsync→atomic replace로 바꾼다. running gateway는 `hermes gateway restart`, down gateway는 다음 start 전 config를 검사한다. `assert_embedded_dispatch_off`가 성공한 뒤에만 active task/tmux 0을 기다린다. clean bootstrap이면 runtime 생성 직후 config를 다시 apply/assert하고 이 두 번째 검증이 patch/gateway 단계보다 앞서야 한다. `write_success_receipt`는 verifier가 읽은 바로 그 `current.json` bytes의 SHA-256을 `guard_current_sha256`에 기록하고, build manifest의 source SHA 및 immutable artifact directory/hash와 다시 대조한 뒤에만 receipt를 원자 기록한다. `prepare_hermes_runtime`이 clean bootstrap을 실제 수행한 경우에는 completed journal path/bytes를 다시 검증해 non-null bootstrap pair를 쓰고, preexisting Hermes의 read-only status 경로에서는 null pair를 쓴다. snapshot pre-state와 이 pair 조합이 다르면 receipt write 전 exit 2다.

전체 rollback 순서는 verified context 준비→현재 producers fail-loud pause→marker closed→supervisor stopped→gateway graceful stop→explicit `restore_guard_current`→`restore_snapshot`의 previous current/config/deployment receipt/unit/Task/producer/Hermes state 복원→daemon reload 또는 Task restore→snapshot의 gateway pre-state 복원→DB state 검증→old release canary 또는 clean-host absence 검증→rolled-back receipt 내구화→old dispatcher closed-marker activation→snapshot producer pre-state 복원이다. 별도 rollback 프로세스는 mutation 전에 original before receipt를 immutable copy로 보존하고 `sha256(build_manifest_bytes(build)) == receipt.build_manifest_sha256`를 먼저 확인한다. 이어 receipt schema/target, state-root 하위 locator와 실제 component 안전성, snapshot index/필수 entry/모든 file digest, candidate patch manifest 실제 digest, Hermes install record exact key set/root/target list/base blob/manifest hash/patch hash/backup directory binding을 대조한다. receipt의 previous release와 gateway pre-state는 이 검증이 반환한 `VerifiedRollbackContext`에서만 읽고 adapter 인자로 중복 주입하지 않는다. `previous_release`는 오직 old current pointer/previous receipt/guard manifest/old dispatcher canary 축을 제어한다. 값이 있으면 그 세 snapshot entry가 모두 present이며 같은 SHA와 guard artifact bytes를 가리켜야 하고, null이면 그 세 entry만 absent여야 한다. Hermes와 DB의 pre-state는 독립적으로 `hermes_installation|hermes_targets|database_backup` entry가 결정하며, service/Task/hook 상태는 언제나 snapshot definition을 복원한다. install record의 `previous_head`는 `hermes_installation=present`일 때 그 head와 같아야 한다. 각 target의 backup file은 backup root 하위 exact relative path에 존재하며 `target_before_sha256` 및 snapshot `hermes_targets` digest와 일치하고, 현재 target은 postimage digest와 일치해야 한다. 하나라도 없거나 다르면 복원을 시작하지 않는다. database backup은 immutable SQLite file이어야 하며 read-only `quick_check=ok`를 통과해야 한다. preexisting DB는 current quick_check 실패 때만 그 verified backup을 복원한다. Hermes/DB 세 entry가 모두 absent인 진짜 clean bootstrap에서만 receipt에 결합된 completed `hermes-bootstrap-journal/v1`이 required다. journal의 owned path exact set은 root+DB 또는 실제 bootstrap uv도 새로 만들었을 때 root+DB+uv만 허용하며, 그 실제 subset만 제거한다. 세 entry가 present인 첫 Forge 배포 VPS에서는 bootstrap pair가 null이어야 하고 기존 Hermes/DB를 snapshot bytes로 보존한다. 제거 완료와 directory fsync 뒤 journal을 content-addressed history로 보존하고 `rolled-back` tombstone을 원자 기록한다. gateway는 verified `gateway_state.was_running`일 때만 다시 시작한다. `previous_release=null`이면 old canary만 생략하고 current pointer/guard absence를 검증해 after receipt의 `current_release`와 `guard_current_sha256`을 null로 쓴다. 이 경우에도 service/Task/hooks와 Hermes/DB는 각 snapshot entry대로 복원하며 absence를 가정하지 않는다. previous release가 있으면 rolled-back receipt가 durable해진 뒤에만 previous dispatcher를 closed marker 아래 start/ready 검증하고 marker를 open한다. 그 다음에만 snapshot producer pre-state를 복원한다. `rollback_windows_target`은 Windows Task와 ACL을 포함한 snapshot을 복원하고 `%LOCALAPPDATA%\InfinityForge\state\deployment-receipt-v1.json`을 after receipt로 쓴다. `rollback_linux_target`은 `linux|vps`만 받고 같은 snapshot layout 전체를 복원한다. 두 함수 모두 generic `rollback_target`을 사용한다.

- [ ] **Step 7: wrappers에서 mutable deploy 제거**

`deploy.ps1`의 첫 executable statement는 다음 exact parameter contract다. parameter set 때문에 `-PlanOnly`와 `-Apply`는 동시에 사용할 수 없으며, 둘 다 생략하면 read-only `Plan`이 기본이다.

```powershell
[CmdletBinding(DefaultParameterSetName="Plan")]
param(
    [Parameter(Mandatory=$true)]
    [ValidatePattern('^[0-9a-f]{40}$')]
    [string]$Sha,

    [Parameter(Mandatory=$true)]
    [string]$Artifact,

    [Parameter(Mandatory=$true)]
    [ValidatePattern('^[0-9a-f]{64}$')]
    [string]$ArtifactSha256,

    [Parameter(Mandatory=$true)]
    [string]$BuildManifest,

    [Parameter(Mandatory=$true)]
    [ValidateSet("Windows","Linux","Vps")]
    [string[]]$Targets,

    [Parameter(Mandatory=$true)]
    [string[]]$RepoPaths,

    [Parameter(Mandatory=$true)]
    [ValidatePattern('^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')]
    [string]$BootstrapRepository,

    [Parameter(ParameterSetName="Plan")]
    [switch]$PlanOnly,

    [Parameter(ParameterSetName="Apply")]
    [switch]$Apply
)

$StateRoot = Join-Path $env:LOCALAPPDATA "InfinityForge\state"
$WindowsReceipt = Join-Path $StateRoot "deployment-receipt-v1.json"
$LinuxReceiptEvidence = Join-Path $StateRoot "evidence\linux-deployment-receipt.json"
$VpsReceiptEvidence = Join-Path $StateRoot "evidence\vps-deployment-receipt.json"
```

`Plan`은 경로·target·build manifest·artifact hash·Hermes status 비교 결과만 JSON으로 출력하고 host를 바꾸지 않는다. `Apply`는 첫 mutation 전에 정확히 `python -m forge.ops.deployment verify-build --build-manifest BUILD --artifact FILE --artifact-sha256 SHA256`을 호출하고, target adapter로 `DeploymentOperations`를 구성한다. 각 target 전후에는 `compare-hermes-status --target TARGET --expected-sha SHA --current-manifest CURRENT --hermes-root ROOT`, 성공 receipt 보존에는 `record-evidence --target TARGET --receipt RECEIPT --output OUTPUT`, 세 target 마지막에는 `audit-targets --windows-receipt FILE --linux-receipt FILE --vps-receipt FILE`을 사용한다. `build --sha SHA --output-dir DIR`은 immutable artifact 생성 전용이며 deploy wrapper 안에서 다시 build하지 않는다.

Windows 성공 receipt는 `%LOCALAPPDATA%\InfinityForge\state\deployment-receipt-v1.json`에 남긴다. Linux staging과 VPS adapter는 remote receipt bytes와 SHA-256을 각각 WSL transport와 SSH transport로 회수하고, remote에서 계산한 digest와 local에서 다시 계산한 digest가 같을 때만 `record-evidence`를 호출한다. local mirror exact path는 `%LOCALAPPDATA%\InfinityForge\state\evidence\linux-deployment-receipt.json`, `%LOCALAPPDATA%\InfinityForge\state\evidence\vps-deployment-receipt.json`이다. mirror도 temp→flush→fsync→replace로 쓰며 digest 불일치는 target 실패와 rollback이다.

`deploy.ps1`과 `deploy-vps.sh`는 전달된 artifact SHA-256을 local/remote에서 다시 계산하고 build manifest와 비교한다. `git add`, `git commit`, `git pull`, `git push`를 실행하지 않는다. repository 목록은 repeated `-RepoPaths` 또는 `--repo` target input에서 받고 Task 6 hook installer의 `hooks`/`Hooks` phase를 모든 repo에 실행한 뒤 path별 hook SHA-256을 deployment receipt에 기록한다. Linux exact interface는 `install-linux.sh --phase hermes|hooks|services --target linux|vps --release PATH --manifest PATH [repeatable --repo PATH] [--snapshot-index PATH --snapshot-sha256 HASH]`, verifier는 `verify-linux-install.sh --target linux|vps --release PATH --manifest PATH [repeatable --repo PATH]`다. Windows Hooks는 `install-windows.ps1 -Phase Hooks -ReleasePath PATH -Manifest PATH -RepoPaths PATHS -PythonPath PATH`, Services는 여기에 exact `-BootstrapRepository OWNER/REPO`를 추가해 호출한다. wrapper는 `gh repo view`로 canonicalized/read-back한 repository가 전달값과 exact-equal인지 확인한 뒤에만 Services phase로 간다.

Windows adapter는 finalized snapshot의 `hermes_installation.approved_base_ref`를 읽은 뒤 candidate release의 `hermes-bootstrap.py ensure-approved-base --root ROOT --commit 4281151ae859241351ba14d8c7682dc67ff4c126 --remote origin`을 실행한다. 그 exit 0 뒤에만 `hermes-patch.py status|recover|install`을 호출한다. rollback adapter의 `restore-approved-base-ref`는 snapshot이 absent면 candidate가 만든 ref만 zero-OID guarded delete하고, present면 exact previous SHA를 guarded restore한다. HEAD/index/target/porcelain과 다른 ref는 변경하지 않으며 이 Windows wiring은 `deploy.ps1` RED order assertion으로 고정한다.

Hermes patch installer는 `check|install|verify|rollback` parser를 유지하고 다음 command를 새 completion gate로 사용한다.

```text
python forge/scripts/hermes-patch.py verify --root ROOT --manifest MANIFEST --record RECORD --current-manifest CURRENT --expected-source-sha SHA
```

`verify`는 `--root`의 checkout HEAD가 승인된 upstream base인지 확인하고, `--manifest`의 preimage/postimage 계약과 `--record`의 실제 설치 hash를 대조한다. 이어 `forge.ops.deployment.verify_guard_current`를 호출해 current manifest와 expected source SHA를 검증한 뒤 patched target postimage SHA와 Hermes target test를 검사한다. 다섯 입력 중 하나가 없거나 대조가 하나라도 실패하면 stderr와 exit 2이며, `verify_hermes_patch` callback이 성공하기 전에는 current pointer, service, receipt 단계로 넘어가지 않는다.

Windows rollback wrapper는 다음 exact parser와 `verify-rollback` call을 사용한다. `rollback_windows_target`이 snapshot의 `guard/current.json`, current pointer, config, deployment receipt, Scheduled Task와 Hermes target을 복원하고 repository hook을 old release 기준으로 재검증한 뒤 fixed after receipt를 쓴다.

```powershell
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [string]$BeforeReceipt,

    [Parameter(Mandatory=$true)]
    [string]$BuildManifest,

    [Parameter(Mandatory=$true)]
    [string[]]$RepoPaths
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$StateRoot = Join-Path $env:LOCALAPPDATA "InfinityForge\state"
$AfterReceipt = Join-Path $StateRoot "deployment-receipt-v1.json"
$CurrentManifest = Join-Path $env:LOCALAPPDATA "InfinityForge\guard\current.json"
$GuardCurrent = Get-Content -Raw -LiteralPath $CurrentManifest | ConvertFrom-Json
$Python = [string]$GuardCurrent.policies.'forge-v1'.python
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "trusted deployment Python is missing: $Python"
}

$RepoJson = ConvertTo-Json -InputObject @($RepoPaths) -Compress
$RollbackCode = @'
import json
import sys
from pathlib import Path

from forge.ops.deployment import preserve_before_receipt, rollback_windows_target

state_root = Path(sys.argv[4])
preserved = preserve_before_receipt(Path(sys.argv[1]), state_root=state_root)
receipt = rollback_windows_target(
    before_receipt=preserved,
    build_manifest=Path(sys.argv[2]),
    repositories=tuple(Path(value) for value in json.loads(sys.argv[3])),
)
if receipt.target != "windows" or receipt.result != "rolled-back":
    raise SystemExit(2)
print(json.dumps({"before_receipt": str(preserved), "current_release": receipt.current_release}))
'@
$PreviousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = $ProjectRoot
    $RollbackResult = & $Python -c $RollbackCode $BeforeReceipt $BuildManifest $RepoJson $StateRoot | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) {
        throw "Windows snapshot rollback failed"
    }

    & $Python -m forge.ops.deployment verify-rollback `
        --target windows `
        --before-receipt $RollbackResult.before_receipt `
        --after-receipt $AfterReceipt `
        --current-manifest $CurrentManifest
    if ($LASTEXITCODE -ne 0) {
        throw "Windows rollback verification failed"
    }
} finally {
    $env:PYTHONPATH = $PreviousPythonPath
}
```

공용 Linux rollback wrapper는 다음 parser와 library call을 그대로 구현한다.

```bash
#!/usr/bin/env bash
set -euo pipefail

target=""
before_receipt=""
build_manifest=""
repos=()
while (($#)); do
  case "$1" in
    --target) target="$2"; shift 2 ;;
    --before-receipt) before_receipt="$2"; shift 2 ;;
    --build-manifest) build_manifest="$2"; shift 2 ;;
    --repo) repos+=("$2"); shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ "$target" != "linux" && "$target" != "vps" ]]; then
  echo "--target must be linux or vps" >&2
  exit 2
fi
if [[ ! -f "$before_receipt" || ! -f "$build_manifest" || ${#repos[@]} -eq 0 ]]; then
  echo "receipt, build manifest, and at least one repo are required" >&2
  exit 2
fi

data_root="${XDG_DATA_HOME:-$HOME/.local/share}/infinity-forge"
state_root="${XDG_STATE_HOME:-$HOME/.local/state}/infinity-forge"
current_manifest="$data_root/guard/current.json"
after_receipt="$state_root/deployment-receipt-v1.json"
tool_release="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"

rollback_output="$(PYTHONPATH="$tool_release" /usr/bin/python3 - \
  "$target" "$before_receipt" "$build_manifest" "$state_root" "${repos[@]}" <<'PY'
import sys
from pathlib import Path

from forge.ops.deployment import preserve_before_receipt, rollback_linux_target

state_root = Path(sys.argv[4])
preserved = preserve_before_receipt(Path(sys.argv[2]), state_root=state_root)
receipt = rollback_linux_target(
    target=sys.argv[1],
    before_receipt=preserved,
    build_manifest=Path(sys.argv[3]),
    repositories=tuple(Path(value) for value in sys.argv[5:]),
)
print(receipt.current_release or "")
print(preserved)
PY
)"
mapfile -t rollback_result <<<"$rollback_output"
release_sha="${rollback_result[0]}"
preserved_before="${rollback_result[1]}"

if [[ -n "$release_sha" ]]; then
  release="$data_root/guard/releases/$release_sha"
  previous_manifest="$release/build-manifest.json"
  verify_args=(--target "$target" --release "$release" --manifest "$previous_manifest")
  for repo in "${repos[@]}"; do
    verify_args+=(--repo "$repo")
  done
  "$release/forge/scripts/verify-linux-install.sh" "${verify_args[@]}"
fi

PYTHONPATH="$tool_release" /usr/bin/python3 -m forge.ops.deployment verify-rollback \
  --target "$target" \
  --before-receipt "$preserved_before" \
  --after-receipt "$after_receipt" \
  --current-manifest "$current_manifest"
```

VPS wrapper는 별도 복구 논리를 복제하지 않는다.

```bash
#!/usr/bin/env bash
set -euo pipefail
exec "$(dirname "$0")/rollback-linux.sh" --target vps "$@"
```

- [ ] **Step 8: GREEN과 금지 패턴 확인**

Run: `.\.venv\Scripts\python.exe -m pytest tests/ops/test_deployment.py -q`

Expected: `pytest --collect-only`로 고정된 전체 deployment test가 모두 PASS하고, POSIX symlink와 Windows junction 중 현재 OS가 아닌 case만 1건 SKIP한다. 숫자를 수동 상수로 축약하지 않고 CI artifact에 collected/passed/skipped count를 기록한다.

Run: `rg -n "git pull|git add -A|git commit|git push|systemctl restart" forge/scripts/deploy.ps1 forge/scripts/deploy-vps.sh forge/scripts/hermes-patch.py forge/scripts/rollback.ps1 forge/scripts/rollback-linux.sh forge/scripts/rollback-vps.sh forge/ops/deployment.py`

Expected: output 0 lines.

Run: `wsl.exe bash -lc 'cd /mnt/c/01.project/INFINITY_FORGE && bash -n forge/scripts/deploy-vps.sh forge/scripts/rollback-linux.sh forge/scripts/rollback-vps.sh'`

Expected: exit 0.

- [ ] **Step 9: Windows→Linux staging→VPS rollback/forward gate**

1. Windows local에서 install, Scheduled Task inventory, stale marker negative, rollback, forward deploy를 실행한다.
2. 같은 build manifest SHA로 Linux staging install, verify, controlled reboot, verify, rollback, forward deploy를 실행한다.
3. 같은 build manifest SHA로 VPS install, user service reboot survival, stale marker negative, rollback, forward deploy를 실행한다.
4. Windows receipt는 `%LOCALAPPDATA%\InfinityForge\state\deployment-receipt-v1.json`을 사용한다. Linux/VPS 성공 직후 `deploy.ps1`은 remote receipt를 hash 검증하고 `record-evidence`로 각각 `%LOCALAPPDATA%\InfinityForge\state\evidence\linux-deployment-receipt.json`, `%LOCALAPPDATA%\InfinityForge\state\evidence\vps-deployment-receipt.json`에 원자 mirror한다.
5. 세 host deployment receipt의 `build_manifest_sha256`, `current_release`, build manifest가 가리키는 guard/patch hash가 같고 `target`이 각각 windows/linux/vps인지 검사한다. `guard_current_sha256`은 host별 absolute `python`/`artifact` path 때문에 서로 같다고 가정하지 않고, 각 target에서 검증한 canonical `current.json` digest와 일치하는지만 검사한다.

- [ ] **Step 10: commit**

```powershell
git add forge/ops/deployment.py tests/ops/test_deployment.py forge/scripts/deploy.ps1 forge/scripts/deploy-vps.sh forge/scripts/hermes-patch.py forge/scripts/rollback.ps1 forge/scripts/rollback-linux.sh forge/scripts/rollback-vps.sh
git commit -m "feat: deploy Forge guards with transactional host receipts"
```

## 최종 검증

- [ ] 운영·투영 tests 전체:

Run: `.\.venv\Scripts\python.exe -m pytest tests/ops -q`

Expected: exit 0, failures 0.

- [ ] 전체 repository 회귀:

Run: `.\.venv\Scripts\python.exe -m pytest tests -q`

Expected: exit 0, failures 0.

- [ ] Python compile:

Run: `.\.venv\Scripts\python.exe -m compileall forge`

Expected: exit 0.

- [ ] shell/systemd:

Run: `wsl.exe bash -lc 'cd /mnt/c/01.project/INFINITY_FORGE && bash -n forge/scripts/*.sh && systemd-analyze verify forge/systemd/*.service forge/systemd/*.timer'`

Expected: exit 0.

- [ ] PowerShell parser:

Run: `pwsh -NoProfile -Command "Get-ChildItem forge/scripts/*.ps1 | ForEach-Object { [scriptblock]::Create((Get-Content -Raw $_.FullName)) | Out-Null }"`

Expected: exit 0.

- [ ] exact deployment audit:

Run: `.\.venv\Scripts\python.exe -m forge.ops.deployment audit-targets --windows-receipt "$env:LOCALAPPDATA\InfinityForge\state\deployment-receipt-v1.json" --linux-receipt "$env:LOCALAPPDATA\InfinityForge\state\evidence\linux-deployment-receipt.json" --vps-receipt "$env:LOCALAPPDATA\InfinityForge\state\evidence\vps-deployment-receipt.json"`

Expected: JSON `{"result":"PASS","targets":["windows","linux","vps"]}`와 exit 0.

## 작성자 자체 검토

- [x] label mirror, spec coverage, supervisor/canary, drift, systemd/Windows Task, install, build, deploy/rollback이 각각 독립 task와 test cycle을 가진다.
- [x] Hermes create는 positional title과 `--body`, `--assignee`, `--project` 또는 `--workspace`, `--idempotency-key`, `--skill`을 사용한다.
- [x] delegate의 `prepare → run → complete-ready → kanban_complete` 순서가 card blocked/unblock과 함께 고정됐다.
- [x] embedded dispatcher를 config+gateway에 먼저 적용한 뒤 active work를 drain한다.
- [x] Windows/Linux/VPS installer가 target input의 모든 repo에 hook installer install/verify를 실행하고 host receipt에 hash를 기록한다.
- [x] systemd interpreter, script 0755, linger, enable, controlled reboot 검증이 있다.
- [x] Linux/VPS clean host가 ambient uv 없이 hash-locked uv 0.11.24로 승인된 Hermes upstream base `4281151ae859241351ba14d8c7682dc67ff4c126`을 sync하고 kanban DB mode 0600을 검증한다.
- [x] immutable build manifest와 host deployment receipt의 field가 분리됐다.
- [x] exact nested `guard/current.json`은 Hermes patch 전에 원자 기록·검증되고 snapshot/rollback과 receipt digest에 포함된다.
- [x] deployment receipt는 snapshot bundle과 Hermes install record의 state-root locator/digest를 포함하고 별도 rollback 프로세스가 mutation 전에 재검증한다.
- [x] Windows/Linux/VPS deploy·rollback wrapper와 seven-command Python CLI, remote receipt local mirror 경로가 exact interface test로 고정됐다.
- [x] 각 code step과 test body는 실행 가능한 구체 값과 assertion을 가진다.
- [x] Windows→Linux staging→VPS rollback/forward와 동일 build manifest SHA audit가 마지막 gate다.

## 변경이력

- 2026-07-12 | Hermes 운영·투영·배포 subplan 작성 | 변경: umbrella Task 10~15의 label mirror, spec coverage, dispatcher/canary, drift, cross-platform install, immutable build, transactional deploy/rollback을 8개 TDD task로 분리 | 이유: 실제 Hermes CLI와 실운영 배포 순서를 실행 가능한 계약으로 고정하기 위해 작성 | 검증: Python code block 29개 compile 성공, PowerShell code block 9개 parser 성공, Bash code block syntax 성공, 금지 패턴 0건, `git diff --check` 통과
- 2026-07-12 | rollout 순서·경로 정정 | 변경: 승인 spec에 맞춰 Windows local→Linux staging→Ubuntu VPS 순서로 통일하고 세 target의 project/release/current/state/Hermes absolute path를 고정 | 이유: umbrella rollout 순서와 subplan 순서의 불일치 제거 | 검증: 순서 역전 표현 0건, Python/PowerShell/Bash parser 오류 0건, 금지 패턴 0건, `git diff --check` 통과
- 2026-07-12 | clean bootstrap·guard manifest·transaction interface 보강 | 변경: 승인된 Hermes upstream base와 uv 0.11.24 wheel hash lock, nested `guard/current.json` atomic write/verify/snapshot/restore, receipt digest, deploy.ps1 parameter sets, deployment CLI 집합, 공용 Linux rollback, Hermes patch verify, remote receipt local mirror 계약을 추가 | 이유: clean Linux/VPS와 부분 실패에서도 조기 종료 방지 guard가 Hermes보다 먼저 검증되고 원상 복구되도록 rollout gap 제거 | 검증: Python code block 34개 compile 성공, PowerShell code block 10개 parser 성공, Bash code block 3개 syntax 성공, 금지 패턴 0건, 순서 역전 0건, untracked diff check 통과
- 2026-07-12 | upstream base·verify·Windows rollback 교차계약 정렬 | 변경: clean checkout을 `4281151ae859241351ba14d8c7682dc67ff4c126`으로 고정하고 tag tip 의존을 제거했으며 Hermes verify의 다섯 필수 인자와 Windows rollback parser/receipt/verifier 계약을 추가 | 이유: umbrella completion patch preimage와 모든 target rollback interface를 동일 기준으로 맞추기 위해 수정 | 검증: Python code block 34개 compile 성공, PowerShell code block 11개 parser 성공, Bash code block 3개 syntax 성공, 금지 패턴 0건, 순서 역전 0건, untracked diff check 통과
- 2026-07-12 | rollback material 영속 계약 보강 | 변경: deployment receipt에 snapshot bundle과 Hermes patch install record locator/digest를 추가하고 snapshot index·모든 파일·record를 별도 프로세스가 mutation 전에 검증하는 RED test와 복원 규칙을 추가했으며 guard timeout 범위를 1..900으로 통일 | 이유: 배포 프로세스 종료 뒤에도 Windows/Linux/VPS rollback이 복원 재료를 정확히 찾고 변조를 거절하도록 이전 P1을 완전히 닫기 위해 수정 | 검증: 구현 전 계획 단계이며 Python fenced block AST와 schema/CLI 교차 계약 재검증 대상으로 등록
- 2026-07-12 | 실호스트 bootstrap·publisher·rollback 수렴 보강 | 변경: clean-bootstrap durable ownership journal/receipt binding, signal recovery, exact scheduled argv와 target env, canonical GitHub repo publisher, producer lock/pause, host-only ops freshness, WSL Linger/독립 hook clone, same-SHA immutable release reuse, preexisting VPS Hermes/DB 독립 rollback, approved-base pinned object create-only helper/ref snapshot을 추가 | 이유: Windows·clean WSL·existing VPS의 실제 상태와 rollback/forward 재시도에서 partial mutation·cross-platform hook overwrite·divergent Hermes ancestry 실패를 방지 | 검증: Python/PowerShell/Bash fenced parser와 disposable Windows/VPS variant, tamper, crash, race RED test를 구현 단계 fresh verification 대상으로 등록
