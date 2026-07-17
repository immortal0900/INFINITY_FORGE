# 구독 기반 Codex 우선·Claude 전환 구현 계획

> **Agentic worker(Agent 실행자)용:** REQUIRED SUB-SKILL: 이 plan을 task 단위로 구현하려면 `weapon:subagent-driven-development`(권장) 또는 `weapon:executing-plans`를 사용한다. 코드 작성에는 `code-design-principles`, 동작 변경에는 test-first 원칙을 적용한다. 진행 추적에는 checkbox(`- [ ]`) 문법을 쓴다.

**Goal:** Windows 로컬, EC2, VPS에서 ChatGPT 구독 Codex를 기본 코딩 runtime으로 사용하고, Forge 코딩 Task 또는 `codex` 스킬이 **구조화된 구독 한도 소진**을 확인한 경우에만 같은 작업·workspace·run을 Claude Max의 Claude Code CLI로 한 번 이어서 실행한다.

**Architecture:** Forge 소유 `subscription runner`가 Codex와 Claude 시도의 순서, 환경 정리, 판별, 영수증 기록을 담당한다. Codex quota 판별은 Hermes가 제공하는 `CodexAppServerClient`로 `account/read`와 `account/rateLimits/read`를 조회하며, worker의 기존 Hermes 종료 코드 `75`만으로는 전환하지 않는다. Hermes v0.18.2 carried change에는 Forge Task worker spawn 경계만 감싸는 일곱 번째 버전 고정 패치를 추가한다. 일반 Hermes 대화와 비-Forge Task는 이 경로를 통과하지 않는다. 세 머신의 App Server·MCP·스킬·서비스 설정은 같은 configure script로 적용하고, 머신별 배포 script는 설치와 서비스 생명주기만 담당한다.

**Tech Stack:** Python 3.11+ stdlib, pytest 9.x, Hermes Agent 0.18.2, Codex CLI/App Server JSON-RPC, Claude Code CLI stream-json, PowerShell, Bash, systemd user services

**Approved design:** `docs/weapon/specs/2026-07-17-subscription-runtime-fallback-design.md`

## Global Constraints

1. 종량제 OpenAI API 및 Anthropic API를 호출하거나 fallback으로 구성하지 않는다.
2. 자동 경로는 Codex 최대 1회, Claude 최대 1회이며 Claude에서 Codex로 돌아가지 않는다.
3. 자동 전환 범위는 `forge-task:` 또는 `forge-step:` idempotency key를 가진 Forge Task와 관리형 `codex` 스킬뿐이다.
4. 관리형 `claude-code` 스킬은 Claude를 직접 한 번 실행하고 Codex를 선행·후행 호출하지 않는다.
5. 일반 Hermes 대화와 비-Forge Kanban Task의 provider/runtime 동작은 바꾸지 않는다.
6. Codex 전환 조건은 ChatGPT 계정이면서 App Server 응답의 `rateLimitReachedType`가 비어 있지 않거나 `spendControlReached=true`인 경우뿐이다. `usedPercent == 100`, 사람이 읽는 오류 문구, Hermes 종료 코드 `75` 단독 값은 근거가 아니다.
7. 인증·네트워크·MCP·도구·timeout·취소·crash·알 수 없는 오류에는 Claude를 자동 호출하지 않는다.
8. Claude 실행 전 `claude auth status`가 `loggedIn=true`, `authMethod=claude.ai`, `apiProvider=firstParty`, `subscriptionType=max`인지 확인한다.
9. 자식 환경에서 `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY`를 제거한다. 사용자 원본 환경과 secret 파일은 수정하지 않는다.
10. 인증 파일, OAuth token, API key, 전체 prompt, 전체 도구 출력은 저장소·attempt 영수증·서비스 로그에 기록하지 않는다.
11. 같은 Task ID, `HERMES_KANBAN_RUN_ID`, workspace, branch를 유지하며 fallback용 Task를 새로 만들지 않는다.
12. App Server 설정 적용은 Hermes/Codex 설정을 먼저 백업하고, 설정값·프로세스 handshake·MCP 목록의 사후 검증이 하나라도 실패하면 원복한다.
13. Claude CLI에는 `--bare`를 쓰지 않는다. `--bare`는 OAuth 인증을 건너뛰므로 구독 전용 보장을 깨뜨린다.
14. Hermes bundled skill을 직접 수정하지 않고 Forge 관리형 local skill로 우선순위를 덮는다.
15. 기존 사용자 수정 `README.md`, `docs/setup/desktop-guide.md`, `forge/skills/memex/SKILL.md`, `.codex/`, `docs/setup/fetch-ec2-dashboard-token.ps1`를 보존한다.
16. 실제 원격 배포는 로컬 clean `main == origin/main`이라는 기존 `deploy.ps1` gate를 우회하지 않는다.
17. VPS의 Claude Max 대화형 로그인 전에는 준비 완료로 보고하지 않으며 해당 머신의 runtime 설정을 부분 적용하지 않는다.
18. public 계약, 외부 프로세스 실행, 원자적 설정 복원, Hermes carried change에는 이유와 rollback을 설명하는 `RISK(...)` 주석을 남긴다.

## 파일 책임 지도

```text
forge/ops/
  subscription_runtime.py       공통 enum, 분류 정책, 자식 환경 정리, receipt
  codex_subscription_probe.py   App Server account/rate-limit 구조화 조회
  subscription_runner.py        worker·codex-skill·claude-skill 실행 오케스트레이션
  subscription_setup.py         App Server·MCP·구독 로그인 설정 적용/검증/원복
forge/scripts/
  subscription-runner.py        공통 runner의 얇은 CLI
  configure-subscription-runtime.py
                                한 머신의 apply/verify/rollback CLI
forge/hermes_change/installer.py
                                Hermes worker spawn의 Forge 전용 일곱 번째 patch
forge/skills/codex/SKILL.md      Codex 우선·Claude 1회 전환 스킬
forge/skills/claude-code/SKILL.md
                                Claude Max 직접 실행 스킬
tests/ops/                       정책, probe, runner, setup, 배포 계약 test
tests/hermes/                    일곱 파일 patch build/install/restore test
docs/setup/subscription-runtime.md
                                운영·인증·검증·rollback runbook
```

## Public Interfaces and Exit Contract

```python
class RuntimeKind(str, Enum):
    CODEX = "codex"
    CLAUDE = "claude"

class ExitClass(str, Enum):
    SUCCESS = "success"
    SUBSCRIPTION_QUOTA = "subscription_quota"
    BILLING = "billing"
    AUTH = "auth"
    NETWORK = "network"
    TOOL = "tool"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

@dataclass(frozen=True)
class CodexSubscriptionSnapshot:
    account_type: str | None
    plan_type: str | None
    rate_limit_reached_type: str | None
    spend_control_reached: bool

@dataclass(frozen=True)
class AttemptResult:
    runtime: RuntimeKind
    returncode: int
    exit_class: ExitClass
    started_at: str
    ended_at: str

@dataclass(frozen=True)
class RunReceipt:
    mode: str
    task_id: str | None
    run_id: str | None
    primary_runtime: RuntimeKind
    final_runtime: RuntimeKind | None
    fallback_reason: str | None
    attempts: tuple[AttemptResult, ...]
```

CLI 계약:

```text
subscription-runner.py worker --workspace PATH -- ORIGINAL_HERMES_ARGV...
subscription-runner.py codex-skill --workspace PATH --prompt-file PATH
subscription-runner.py claude-skill --workspace PATH --prompt-file PATH
configure-subscription-runtime.py apply --forge-root PATH --hermes-root PATH
configure-subscription-runtime.py verify --forge-root PATH --hermes-root PATH
configure-subscription-runtime.py rollback --hermes-root PATH
```

Exit code 계약:

| Code | 의미 |
|---:|---|
| `0` | 최종 runtime 성공 및 Task terminal protocol 충족 |
| `75` | Codex의 transient rate-limit/billing 신호이지만 App Server quota 확정 실패; 기존 Hermes cooldown/requeue 유지 |
| 원본 nonzero | auth/network/tool/unknown 등 자동 전환 금지 오류; 원본 정책 유지 |
| `78` | 구독 인증·관리 설정이 요구 계약과 다름 |
| `70` | runner 자체 계약 위반 또는 Claude 성공 종료 뒤 Task terminal 전이 누락 |

---

### Task 1: 공통 구독 정책과 영수증

**Files:**
- Create: `forge/ops/subscription_runtime.py`
- Create: `tests/ops/test_subscription_runtime.py`

**Interfaces:**
- Produces: 위 enum/dataclass, `scrub_subscription_environment()`, `classify_codex_snapshot()`, `classify_claude_stream()`, `write_run_receipt()`.
- Consumes: plain mapping과 JSON object만 사용하며 CLI process를 직접 시작하지 않는다.

- [ ] **Step 1: RED test 작성**

```python
def test_codex_quota_requires_chatgpt_and_backend_reached_type() -> None:
    snapshot = CodexSubscriptionSnapshot("chatgpt", "plus", "primary", False)
    assert classify_codex_snapshot(snapshot) is ExitClass.SUBSCRIPTION_QUOTA

def test_used_percent_and_message_are_not_quota_inputs() -> None:
    snapshot = CodexSubscriptionSnapshot("chatgpt", "plus", None, False)
    assert classify_codex_snapshot(snapshot) is ExitClass.SUCCESS

def test_child_environment_removes_every_payg_switch() -> None:
    cleaned = scrub_subscription_environment({
        "OPENAI_API_KEY": "secret", "ANTHROPIC_API_KEY": "secret",
        "CLAUDE_CODE_USE_BEDROCK": "1", "PATH": "kept",
    })
    assert cleaned == {"PATH": "kept"}
```

- [ ] **Step 2: RED 확인**

Run: `python -m pytest tests/ops/test_subscription_runtime.py -q`

Expected: `ModuleNotFoundError: forge.ops.subscription_runtime`로 FAIL.

- [ ] **Step 3: 최소 구현**

`classify_codex_snapshot()`는 `account_type == "chatgpt"`를 먼저 확인하고, backend reached field만 quota로 분류한다. `classify_claude_stream()`는 먼저 Max first-party auth를 확인한 뒤 structured `system/api_retry` event의 정규화된 error code를 분류한다. `rate_limit`만 `SUBSCRIPTION_QUOTA`, `billing_error`는 `BILLING`으로 구분해 “결제 오류=한도 소진”으로 오인하지 않는다. receipt는 `~/.hermes/infinity-forge/runtime-attempts/<run-id>-<timestamp>.json`에 임시 파일과 `os.replace()`로 원자 기록하고 파일명에 task 본문을 포함하지 않는다.

- [ ] **Step 4: GREEN 및 안전 test**

Run: `python -m pytest tests/ops/test_subscription_runtime.py -q`

Expected: quota/auth/billing/unknown, secret 제거, atomic receipt test 전부 PASS.

- [ ] **Step 5: commit**

Commit: `git commit -m "feat: define subscription runtime policy"`

### Task 2: Codex App Server 구독 상태 probe

**Files:**
- Create: `forge/ops/codex_subscription_probe.py`
- Create: `tests/ops/test_codex_subscription_probe.py`

**Interfaces:**
- Produces: `ProbeError`, `CodexAppServerProbe.probe(codex_bin: str, env: Mapping[str, str], timeout: float = 10.0) -> CodexSubscriptionSnapshot`.
- Adapter: Hermes v0.18.2 `agent.transports.codex_app_server.CodexAppServerClient`를 lazy import하고 test에서는 `client_factory`를 주입한다.

- [ ] **Step 1: RED fixtures와 client double 작성**

```python
def test_probe_reads_account_and_rate_limits() -> None:
    client = FakeClient(results={
        "account/read": {"account": {"type": "chatgpt", "planType": "plus"}},
        "account/rateLimits/read": {
            "rateLimits": {"rateLimitReachedType": "primary"},
        },
    })
    snapshot = CodexAppServerProbe(lambda **_: client).probe("codex", {}, 1)
    assert snapshot.rate_limit_reached_type == "primary"
    assert client.methods == ["initialize", "account/read", "account/rateLimits/read"]
```

- [ ] **Step 2: RED 확인**

Run: `python -m pytest tests/ops/test_codex_subscription_probe.py -q`

Expected: module import FAIL.

- [ ] **Step 3: Hermes client 규격 그대로 구현**

```python
with self._client_factory(codex_bin=codex_bin, env=dict(env)) as client:
    client.initialize(
        client_name="infinity_forge_subscription_probe",
        client_title="Infinity Forge Subscription Probe",
        client_version="1.0",
        timeout=timeout,
    )
    account = client.request("account/read", {"refreshToken": False}, timeout=timeout)
    limits = client.request("account/rateLimits/read", {}, timeout=timeout)
```

응답의 field가 빠지거나 type이 다르면 `ProbeError`이며 quota로 분류하지 않는다. exception message를 quota 문자열로 해석하지 않는다.

- [ ] **Step 4: GREEN 및 version-bound contract test**

Run: `python -m pytest tests/ops/test_codex_subscription_probe.py -q`

Expected: handshake 순서, missing field, timeout, malformed type, close test PASS.

- [ ] **Step 5: commit**

Commit: `git commit -m "feat: probe codex subscription limits"`

### Task 3: 단일 run subscription runner

**Files:**
- Create: `forge/ops/subscription_runner.py`
- Create: `forge/scripts/subscription-runner.py`
- Create: `tests/ops/test_subscription_runner.py`
- Create: `tests/ops/test_subscription_runner_cli.py`

**Interfaces:**
- Produces: `SubscriptionRunner.run_worker()`, `run_codex_skill()`, `run_claude_skill()`, `build_claude_continuation_prompt()`, `main(argv) -> int`.
- Process seam: `process_runner(argv, cwd, env, stdin_text, stdout_path) -> CompletedAttempt`를 주입해 실제 CLI 없이 순서를 검증한다.

- [ ] **Step 1: RED state-machine test 작성**

```python
def test_worker_falls_back_once_only_after_confirmed_quota() -> None:
    process = SequenceProcess([result(75), result(0)])
    probe = SequenceProbe([available_snapshot(), quota_snapshot()])
    result = runner(process, probe).run_worker(worker_request())
    assert [call.runtime for call in process.calls] == ["codex", "claude"]
    assert result.final_runtime is RuntimeKind.CLAUDE

def test_transient_exit_75_is_returned_without_claude() -> None:
    process = SequenceProcess([result(75)])
    result = runner(process, SequenceProbe([available_snapshot(), available_snapshot()])).run_worker(worker_request())
    assert result.returncode == 75
    assert process.calls == [codex_call()]
```

추가 RED cases: Codex 성공, preflight quota로 Codex skip, auth/network/tool/unknown nonzero, Codex quota+Claude quota, Claude direct mode, 한 runtime 두 번 호출 방지, prompt/receipt secret 비포함, Unicode workspace, `--` argv 보존.

- [ ] **Step 2: RED 확인**

Run: `python -m pytest tests/ops/test_subscription_runner.py tests/ops/test_subscription_runner_cli.py -q`

Expected: module/entrypoint import FAIL.

- [ ] **Step 3: worker 실행 구현**

1. scrubbed child env와 task/run/workspace 정보를 만든다.
2. App Server preflight가 quota이면 Codex worker를 시작하지 않고 Claude로 간다. probe 자체 오류면 Codex를 정상 시작한다.
3. 원본 Hermes argv를 그대로 한 번 실행한다.
4. return code `0`은 완료, `75`는 App Server를 다시 probe한다. quota 확정이면 Claude, 아니면 `75`를 반환한다.
5. 나머지 nonzero는 그대로 반환한다.
6. Claude fallback에는 원본 Task ID, 같은 run/workspace/branch, 현재 `git status --short`, `git diff --stat`, 부분 변경 보존, `kanban_complete` 또는 `kanban_block` 정확히 한 번 호출 규칙을 prompt에 넣는다.
7. Claude가 `0`으로 끝나도 Task가 terminal state가 아니면 실제 Hermes CLI의 `kanban block`을 호출하고 `70`을 반환한다.
8. Claude structured quota이면 `Codex와 Claude 구독 한도 소진` 사유로 같은 Task를 block한다.

Claude argv:

```python
[
    claude_bin, "-p", "--output-format", "stream-json", "--verbose",
    "--max-turns", "20", "--permission-mode", "bypassPermissions",
    "--mcp-config", claude_mcp_config, "--strict-mcp-config",
]
```

Codex skill argv:

```python
[
    codex_bin, "exec", "--json", "--sandbox", "workspace-write",
    "--ephemeral", "-C", workspace, "-",
]
```

prompt는 argv가 아니라 stdin으로 전달한다.

- [ ] **Step 4: CLI와 receipt 구현**

CLI는 mode별 필수 인자를 검증하고 `worker`의 `--` 뒤 argv를 손실 없이 넘긴다. receipt에는 runtime·분류·시각·code만 기록하고 prompt와 raw stream-json은 기록하지 않는다.

- [ ] **Step 5: GREEN 확인**

Run: `python -m pytest tests/ops/test_subscription_runner.py tests/ops/test_subscription_runner_cli.py -q`

Expected: 모든 state transition과 process 호출 횟수 test PASS.

- [ ] **Step 6: commit**

Commit: `git commit -m "feat: run codex with claude subscription fallback"`

### Task 4: Hermes Forge worker spawn 경계 patch

**Files:**
- Modify: `forge/hermes_change/installer.py`
- Modify: `tests/hermes/test_installer.py`
- Modify: `tests/hermes/test_installer_cli.py`
- Modify: `tests/ops/test_plain_names.py`
- Modify: `tests/ops/test_workflow_contract.py`

**Interfaces:**
- Add transform: `change_kanban_db_source(source: str) -> str`.
- Add marker: `INFINITY_FORGE_SUBSCRIPTION_WORKER_V1`.
- Add target: `hermes_cli/kanban_db.py`; package target 수는 6에서 7로 바뀐다.

- [ ] **Step 1: RED patch tests**

Test fixture의 `_default_spawn()`에 현재 Hermes v0.18.2 anchor를 넣고 다음을 검증한다.

- `INFINITY_FORGE_SUBSCRIPTION_ROUTING=1`이고 `task.idempotency_key`가 `forge-task:` 또는 `forge-step:`로 시작할 때만 wrapper argv가 붙는다.
- 일반 대화 경로와 다른 Kanban task는 원본 cmd다.
- enabled Forge task에서 runner/python 경로가 없거나 상대 경로·없는 파일이면 `RuntimeError`로 spawn 전 중단한다.
- original Hermes argv의 순서와 task env가 유지된다.
- build/install/restore가 일곱 파일 모두에서 atomic round trip한다.

Run: `python -m pytest tests/hermes/test_installer.py tests/hermes/test_installer_cli.py tests/ops/test_plain_names.py tests/ops/test_workflow_contract.py -q`

Expected: 일곱 번째 target 부재로 FAIL.

- [ ] **Step 2: 최소 patch transform 구현**

삽입 helper의 계약:

```python
def _infinity_forge_subscription_worker_argv(task, cmd, env):
    # INFINITY_FORGE_SUBSCRIPTION_WORKER_V1
    if env.get("INFINITY_FORGE_SUBSCRIPTION_ROUTING") != "1":
        return cmd
    key = task.idempotency_key or ""
    if not key.startswith(("forge-task:", "forge-step:")):
        return cmd
    python_bin = _required_absolute_file(env, "INFINITY_FORGE_SUBSCRIPTION_PYTHON")
    runner = _required_absolute_file(env, "INFINITY_FORGE_SUBSCRIPTION_RUNNER")
    return [python_bin, runner, "worker", "--workspace", env["HERMES_KANBAN_WORKSPACE"], "--", *cmd]
```

`cmd`가 완성된 직후, log file을 열기 전에 이 helper를 호출한다. Hermes upstream 전체 fallback/provider 코드는 수정하지 않는다.

- [ ] **Step 3: GREEN, package, restore 검증**

Run: `python -m pytest tests/hermes/test_installer.py tests/hermes/test_installer_cli.py tests/ops/test_plain_names.py tests/ops/test_workflow_contract.py -q`

Expected: 일곱 target, source-hash refusal, interrupted install/restore, marker test 전부 PASS.

- [ ] **Step 4: commit**

Commit: `git commit -m "feat: route forge workers through subscription runner"`

### Task 5: 관리형 Codex·Claude Code 스킬

**Files:**
- Create: `forge/skills/codex/SKILL.md`
- Create: `forge/skills/claude-code/SKILL.md`
- Create: `tests/ops/test_subscription_skills.py`

**Interfaces:**
- `codex` skill: 공통 runner의 `codex-skill`만 호출한다.
- `claude-code` skill: 공통 runner의 `claude-skill`만 호출한다.
- OS별 env 표기만 분기하고 정책을 skill 본문에서 재구현하지 않는다.

- [ ] **Step 1: RED contract test**

각 SKILL에 mode별 정확한 runner 호출, prompt-file 사용, workspace 전달, API key 직접 사용 금지, 일반 chat 자동 fallback 금지가 있는지 검사한다. `claude-code`에 `codex-skill`이 없어야 한다.

- [ ] **Step 2: 최소 skill 작성**

Windows 호출:

```powershell
& $env:INFINITY_FORGE_SUBSCRIPTION_PYTHON $env:INFINITY_FORGE_SUBSCRIPTION_RUNNER codex-skill --workspace $workspace --prompt-file $promptFile
```

Linux 호출:

```bash
"$INFINITY_FORGE_SUBSCRIPTION_PYTHON" "$INFINITY_FORGE_SUBSCRIPTION_RUNNER" codex-skill --workspace "$workspace" --prompt-file "$prompt_file"
```

Claude skill은 마지막 mode만 `claude-skill`로 고정한다.

- [ ] **Step 3: GREEN 확인과 commit**

Run: `python -m pytest tests/ops/test_subscription_skills.py -q`

Expected: skill 경계와 local override 계약 PASS.

Commit: `git commit -m "feat: add managed subscription cli skills"`

### Task 6: App Server·Claude MCP 설정 apply/verify/rollback

**Files:**
- Create: `forge/ops/subscription_setup.py`
- Create: `forge/scripts/configure-subscription-runtime.py`
- Create: `tests/ops/test_subscription_setup.py`
- Create: `tests/ops/test_subscription_setup_cli.py`

**Interfaces:**
- Produces: `SubscriptionRuntimeSetup.apply()`, `verify()`, `rollback()`, `SubscriptionReadiness`.
- Hermes runtime switch: `hermes_cli.codex_runtime_switch.apply(config, "codex_app_server", persist_callback=save_config)`.
- Claude MCP: `hermes-tools` 하나만 있는 managed JSON을 원자 생성한다.

- [ ] **Step 1: RED setup tests**

Cases: clean apply, 재apply 멱등성, Codex binary 누락, App Server handshake 실패, MCP migration post-check 실패, Claude Max가 아닌 로그인, backup 복원, Linux mode `0600`, 사용자 Codex 설정의 managed block 밖 보존.

- [ ] **Step 2: RED 확인**

Run: `python -m pytest tests/ops/test_subscription_setup.py tests/ops/test_subscription_setup_cli.py -q`

Expected: module/entrypoint import FAIL.

- [ ] **Step 3: Claude Max와 Codex ChatGPT readiness 구현**

`claude auth status` JSON에서 Max first-party 네 필드를 모두 확인한다. `CodexAppServerProbe`의 `account_type == "chatgpt"`를 확인한다. 이메일·조직·token 값은 error와 stdout에 내보내지 않는다.

- [ ] **Step 4: App Server와 MCP 설정 적용**

1. Hermes config와 Codex config를 timestamp backup한다.
2. Hermes runtime switch helper를 호출한다.
3. `model.openai_runtime == "codex_app_server"`를 다시 읽어 검증한다.
4. Codex MCP에 `hermes-tools`와 마이그레이션 대상 서버가 존재하는지 확인한다.
5. Claude MCP JSON에는 Hermes venv Python의 절대 경로와 `-m agent.transports.hermes_tools_mcp_server`만 기록한다. 외부 server의 token/header를 복사하지 않는다.
6. Codex account/rate-limit probe와 Claude `mcp list` 또는 JSON config parse를 수행한다.
7. 실패 시 두 설정을 backup에서 원자 복원하고 runtime readiness를 false로 반환한다.

- [ ] **Step 5: GREEN 및 rollback 검증**

Run: `python -m pytest tests/ops/test_subscription_setup.py tests/ops/test_subscription_setup_cli.py -q`

Expected: apply/verify/reapply/rollback와 secret non-copy test PASS.

- [ ] **Step 6: commit**

Commit: `git commit -m "feat: configure subscription app server runtime"`

### Task 7: Windows·Linux 배포 연결

**Files:**
- Modify: `forge/scripts/deploy-vps.sh`
- Modify: `forge/scripts/deploy.ps1`
- Modify: `tests/ops/test_workflow_contract.py`
- Modify: `tests/ops/test_plain_names.py`
- Create: `tests/ops/test_subscription_deploy_contract.py`

**Interfaces:**
- Stable runner: Windows `%LOCALAPPDATA%\InfinityForge\subscription-runtime\subscription-runner.py`; Linux `~/.hermes/infinity-forge/bin/subscription-runner.py`.
- Linux gateway drop-in env: `INFINITY_FORGE_SUBSCRIPTION_ROUTING`, `INFINITY_FORGE_SUBSCRIPTION_PYTHON`, `INFINITY_FORGE_SUBSCRIPTION_RUNNER`, `INFINITY_FORGE_CLAUDE_BIN`, `INFINITY_FORGE_CLAUDE_MCP_CONFIG`, `INFINITY_FORGE_REPO`.
- Windows는 같은 값을 user-scoped persistent env로 저장하고 현재 gateway process에도 전달한다.

- [ ] **Step 1: RED deploy contract test**

다음을 text/fixture test로 고정한다.

- Claude CLI 버전 `2.1.212`가 Linux에 없을 때 공식 native installer로 서비스 중지 전에 설치한다.
- Claude auth가 Max first-party가 아니면 runtime·service 설정을 적용하지 않고 `claude auth login` checkpoint로 종료한다.
- stable runner, 두 skill, 일곱 파일 Hermes package를 설치한다.
- profile `.codex`, `.claude`, `.claude.json` 연결은 기존 실제 항목을 timestamp backup한 뒤 수행한다.
- systemd drop-in과 Windows user env가 같은 여섯 변수를 가진다.
- configure `apply` 후 `verify`가 성공해야 gateway를 재시작한다.
- 현재 `clean main == origin/main` remote deploy gate는 유지한다.

- [ ] **Step 2: Linux deploy 구현**

공식 설치 command:

```bash
curl -fsSL https://claude.ai/install.sh | bash -s 2.1.212
```

설치·auth gate → 서비스 중지 → Hermes carried change 설치 → stable runner/skills/profile links/drop-in 설치 → configure apply/verify → daemon-reload/restart 순서로 고정한다. 중간 실패 trap은 기존 서비스 상태와 관리 설정을 원복한다.

- [ ] **Step 3: Windows deploy 구현**

기존 local 검증에 Claude Max/Codex ChatGPT readiness, configure apply/verify, stable runner/skill 설치, persistent user env 설정, Hermes gateway 시작/재시작, active process 검증을 추가한다. user API env 자체를 삭제하지 않는다.

- [ ] **Step 4: GREEN 확인**

Run: `python -m pytest tests/ops/test_subscription_deploy_contract.py tests/ops/test_workflow_contract.py tests/ops/test_plain_names.py -q`

Expected: 배포 순서, login gate, env, clean-main gate, rollback 계약 PASS.

- [ ] **Step 5: shell syntax 검사와 commit**

Run: `bash -n forge/scripts/deploy-vps.sh`

Expected: exit `0`.

Run: `powershell -NoProfile -Command "$errors = $null; [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path 'forge/scripts/deploy.ps1'), [ref]$null, [ref]$errors) > $null; if ($errors) { $errors; exit 1 }"`

Expected: exit `0`.

Commit: `git commit -m "feat: deploy subscription runtime on all hosts"`

### Task 8: 현재 구독 문서 정정과 운영 runbook

**Files:**
- Modify: `docs/plan.md`
- Create: `docs/setup/subscription-runtime.md`
- Create: `tests/ops/test_subscription_docs.py`

**Interfaces:**
- `docs/plan.md` D11의 “Claude Code `-p`는 별도 크레딧” 과거 결정을 현재 공식 동작으로 정정한다.
- runbook은 prepare/apply/verify/fake-quota/rollback과 사용자 checkpoint를 명령 단위로 제공한다.

- [ ] **Step 1: RED docs test**

구 문구가 사라지고 다음 공식 근거·안전 원칙·3대 환경 표가 존재하는지 검사한다.

- Anthropic Pro/Max Claude Code 사용은 plan usage에 포함된다.
- 엄격한 정액제 사용은 API credits prompt 거절, Pro/Max 로그인, Console 자격 증명 회피로 보장한다.
- Codex quota는 App Server `account/rateLimits/read`로 판별한다.
- VPS 로그인과 clean-main 배포 gate는 사람이 해제해야 한다.

- [ ] **Step 2: 문서 구현**

공식 출처:

- `https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan`
- `https://code.claude.com/docs/en/iam`
- `https://code.claude.com/docs/en/costs`
- `https://code.claude.com/docs/en/cli-reference`
- `https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md`

runbook은 secret 값을 출력하는 command를 포함하지 않는다.

- [ ] **Step 3: GREEN 확인과 commit**

Run: `python -m pytest tests/ops/test_subscription_docs.py -q`

Expected: 과거 결제 설명 부재, 공식 source, 운영 gate, rollback test PASS.

Commit: `git commit -m "docs: explain subscription-only runtime operations"`

### Task 9: 통합 회귀와 단일 run 증명

**Files:**
- Modify: `tests/ops/test_subscription_runner.py`
- Create: `tests/ops/test_subscription_integration.py`
- Create: `tests/fixtures/subscription-runtime/codex-available.json`
- Create: `tests/fixtures/subscription-runtime/codex-quota.json`
- Create: `tests/fixtures/subscription-runtime/claude-success.jsonl`
- Create: `tests/fixtures/subscription-runtime/claude-quota.jsonl`
- Create: `tests/fixtures/subscription-runtime/claude-auth-error.jsonl`

- [ ] **Step 1: fake process integration harness 작성**

임시 Kanban DB, 같은 task/run/workspace, fake Codex/Claude executable을 사용해 실제 subprocess 경계를 통과시킨다. fixture에는 secret과 실제 계정 식별자를 넣지 않는다.

- [ ] **Step 2: 승인 시나리오 실행**

1. Codex 성공 → Claude 0회, 완료 run 1개.
2. Codex quota → Claude 1회, 같은 run/workspace, 완료 run 1개.
3. Codex 일반 오류 → Claude 0회, 원본 nonzero.
4. Codex transient `75` + quota 아님 → Claude 0회, `75`.
5. Codex quota + Claude quota → Task blocked, API process 0회.
6. Codex 부분 변경 → Claude가 기존 파일을 보존해 완료.
7. 비-Forge task와 일반 Hermes chat → runner 0회.
8. Claude direct skill → Codex 0회.

Run: `python -m pytest tests/ops/test_subscription_integration.py -q`

Expected: 8개 시나리오 PASS.

- [ ] **Step 3: 전체 회귀**

Run: `python -m pytest tests/ -q`

Expected: 전체 suite PASS, warning/skip은 기존 baseline과 비교해 신규 설명 없는 항목 `0`개.

- [ ] **Step 4: 정적 검증과 commit**

Run: `git diff --check`

Expected: 출력 없음.

Run: `rg -n "OPENAI_API_KEY|ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN|CLAUDE_CODE_OAUTH_TOKEN" forge tests docs/setup/subscription-runtime.md`

Expected: scrub key 이름과 안전 설명만 나오며 값·대입·example secret은 없음.

Commit: `git commit -m "test: verify subscription fallback end to end"`

### Task 10: release gate와 Windows 적용

**Files:**
- Runtime changes only; 저장소 파일 추가 변경 없음.

- [ ] **Step 1: 배포 가능 commit 확인**

Run: `git status --short`

Expected: 구현 파일 관점에서 clean. 현재 사용자 소유 변경이 남아 있으면 stash/reset하지 않고 사용자에게 분리·정리를 요청한다.

Run: `git branch --show-current; git rev-parse HEAD; git rev-parse origin/main`

Expected: 구현 commit이 검토·병합되어 `main == origin/main == HEAD`.

- [ ] **Step 2: Windows preflight**

Run: `codex --version; claude --version; claude auth status; hermes --version`

Expected: Codex 설치, Claude `2.1.212`, Claude Max first-party 로그인, Hermes `0.18.2`. auth 출력은 사용자 응답/로그에 개인정보 없이 요약한다.

- [ ] **Step 3: Windows apply/verify**

Run: `python forge/scripts/configure-subscription-runtime.py apply --forge-root "$PWD" --hermes-root "$env:LOCALAPPDATA\hermes"`

Expected: `runtime=codex_app_server`, `codex_account=chatgpt`, `claude_subscription=max`, `mcp=ready`, `rollback_required=false`.

Run: `python forge/scripts/configure-subscription-runtime.py verify --forge-root "$PWD" --hermes-root "$env:LOCALAPPDATA\hermes"`

Expected: 모든 readiness 항목 true.

- [ ] **Step 4: Windows smoke**

- App Server initialize/account/rate-limit probe 성공.
- Codex native file/shell tool 1회와 `hermes-tools` MCP `kanban_list` read-only 호출 성공.
- fake quota shim을 통한 `Codex → Claude` 1회 전환 성공.
- 일반 Hermes 대화와 비-Forge task에서 runner 미호출.
- gateway process active.

- [ ] **Step 5: Windows 실패 시 원복**

Run: `python forge/scripts/configure-subscription-runtime.py rollback --hermes-root "$env:LOCALAPPDATA\hermes"`

Expected: 배포 전 runtime/config 복원, 구독 로그인과 CLI 설치 유지.

### Task 11: EC2 적용

**Files:**
- Runtime changes only; 저장소 파일 추가 변경 없음.

- [ ] **Step 1: 기존 deploy gate로 EC2 배포**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File forge/scripts/deploy.ps1`

Expected: clean-main gate, remote fast-forward, EC2/VPS별 preflight가 먼저 실행된다. VPS login checkpoint가 아직 남으면 EC2 성공 결과는 보존하고 전체 성공으로 표시하지 않는다.

- [ ] **Step 2: EC2 readiness**

Remote verify 결과가 Hermes `0.18.2`, ChatGPT Codex, Claude Max, App Server, `hermes-tools`, 두 managed skill, 일곱 patch target, gateway active를 모두 확인한다.

- [ ] **Step 3: EC2 smoke**

Windows와 같은 네 종류 smoke를 실행하고 receipt의 task/run/runtime field만 회수한다. OAuth/token/전체 prompt는 회수하지 않는다.

- [ ] **Step 4: EC2 실패 시 host-local rollback**

EC2의 configure rollback과 service restore만 수행한다. Windows/VPS의 성공 상태는 변경하지 않는다.

### Task 12: VPS Claude Max 로그인 checkpoint와 적용

**Files:**
- Runtime changes only; 저장소 파일 추가 변경 없음.

- [ ] **Step 1: VPS CLI 설치 gate**

deploy preflight가 Claude Code `2.1.212` native binary를 설치하고 `claude auth status`를 확인한다.

- [ ] **Step 2: 사용자 대화형 로그인 checkpoint**

로그인되지 않았으면 VPS에서 `claude auth login`을 사용자가 완료한다. Console/API credential이 아니라 Claude Max 계정을 선택하고 API credits 제안을 거절한다. 완료 전에는 apply를 실행하지 않는다.

- [ ] **Step 3: 배포 재실행과 readiness**

같은 `deploy.ps1`을 재실행해 fast-forward 상태, Claude Max, App Server, MCP, skill, patch, gateway를 검증한다.

- [ ] **Step 4: VPS smoke와 host-local rollback drill**

Windows·EC2와 같은 smoke를 실행한 뒤 managed 설정 backup 위치를 확인한다. 실제 rollback은 verify 실패 때만 수행하고, 성공 시에는 dry-run/backup inspection으로 복구 가능성을 확인한다.

### Task 13: 3대 환경 최종 승인과 운영 인계

**Files:**
- Runtime evidence only; secret 없는 요약을 작업 결과에 첨부.

- [ ] **Step 1: 환경별 승인 표 작성**

| 검증 | Windows | EC2 | VPS |
|---|---:|---:|---:|
| Codex ChatGPT 로그인 | PASS 필요 | PASS 필요 | PASS 필요 |
| Claude Max first-party | PASS 필요 | PASS 필요 | PASS 필요 |
| Codex App Server active | PASS 필요 | PASS 필요 | PASS 필요 |
| `hermes-tools` MCP | PASS 필요 | PASS 필요 | PASS 필요 |
| managed CLI skills | PASS 필요 | PASS 필요 | PASS 필요 |
| fake quota one-way fallback | PASS 필요 | PASS 필요 | PASS 필요 |
| 일반 chat/비-Forge 미영향 | PASS 필요 | PASS 필요 | PASS 필요 |
| API credential child 비전달 | PASS 필요 | PASS 필요 | PASS 필요 |
| 재배포 멱등성 | PASS 필요 | PASS 필요 | PASS 필요 |

- [ ] **Step 2: 3~5수 앞 최종 점검**

1. Codex 응답 schema가 바뀌면 unknown으로 닫혀 Claude 오호출이 없는지 확인한다.
2. Claude 로그인이 만료되면 해당 host readiness가 실패하고 재로그인만 요구하는지 확인한다.
3. Hermes upgrade 시 source hash mismatch가 서비스 변경 전에 patch install을 막는지 확인한다.
4. 같은 구독을 세 host가 동시에 쓰더라도 Task claim과 시도 1+1 제한으로 폭주하지 않는지 확인한다.
5. rollback이 사용자 로그인·CLI·Task 이력을 삭제하지 않는지 확인한다.

- [ ] **Step 3: 최종 보고 및 Slack 알림**

작업 요약, 변경 파일, test 결과, 세 host 승인 표, 남은 로그인/배포 gate를 `codex work report` 앱으로 채널 `C0BES16KE1J`에 보낸다. 비밀정보와 계정 식별자는 포함하지 않는다.

## 구현 중 중단 조건

다음 조건은 추측으로 우회하지 않고 사용자에게 보고한다.

1. Hermes/Codex 실제 버전이 계획의 version-bound source anchor와 달라 carried change package를 안전하게 만들 수 없음.
2. Codex App Server가 ChatGPT account/rate-limit 구조화 endpoint를 제공하지 않음.
3. Claude CLI가 Max first-party 인증을 확인하지 못함.
4. 현재 사용자 변경 때문에 clean-main release gate를 안전하게 만들 수 없음.
5. VPS 대화형 로그인이 완료되지 않음.

## 완료 정의

구현 commit과 test PASS만으로 완료라고 하지 않는다. Windows·EC2·VPS 모두에서 실제 App Server 활성화, Claude Max readiness, managed MCP/skill, fake quota 전환, 일반 chat 미영향, API credential 비전달을 확인하고 세 환경 승인 표가 모두 PASS일 때 완료다.
