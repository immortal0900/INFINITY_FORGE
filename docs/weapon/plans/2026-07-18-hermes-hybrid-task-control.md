# Hermes 혼합형 Task 제어·범용 Project 실행 구현 계획

> **Agentic worker(Agent 실행자)용:** REQUIRED SUB-SKILL: 이 plan을 task 단위로 구현하려면
> `weapon:subagent-driven-development`를 사용하고, wave 사이 통합 검증에는
> `weapon:verification-before-completion`을 사용한다. 진행 추적에는 checkbox(`- [ ]`)를 쓴다.

**Goal:** Hermes 시작 선택을 키보드 chooser로 제공하고, Infinity Forge에서 중앙 관리하면서
사용자가 고른 임의의 GitHub Project에서 실제 Build·Review·Deep Check·commit·push·PR·merge를
수행하며, 메인 대화에서 Task 전달과 확실한 중단까지 제어한다.

**Architecture:** 시스템 `pre_user_turn`은 선택·Confirm·명확한 Stop만 모델 호출 없이 처리하고
그 밖의 입력은 Hermes 메인 에이전트로 보낸다. 기존 v1 데이터와 API는 그대로 보존하고 같은
owner-host SQLite에 v2 request, settings, Project, revision, stop, access, runtime event를
나란히 저장한다. 하나의 `forge-dispatcher`가 모든 claim을 소유하고 Project별 worktree와
검증된 worker adapter로 실행한다.

**Tech Stack:** Python 3.11, SQLite, pytest, Hermes Agent v0.18.2 carried changes,
prompt_toolkit, React/TypeScript TUI·Desktop, Slack Block Kit, GitHub CLI/API, systemd user units,
PowerShell

**Design SoT:**
`docs/weapon/specs/2026-07-18-hermes-hybrid-task-control-design.md`

## Global Constraints

- 공개 이름 `mode`, `task_flow`, `merge_mode`와 각 값은 바꾸지 않는다.
- Cognet9 등 Project 이름이나 checkout 경로를 코드·서비스 unit에 하드코딩하지 않는다.
- v1 `repository`, hash, row, sidecar outbox, public API 의미를 변경하거나 v2로 재저장하지 않는다.
- 모든 새 schema는 한 migration owner인 `task_database.py`가 한 transaction에서 설치한다.
- 선택·Confirm·명확한 Stop은 `handled`, 모델 API 호출 0회여야 한다.
- UI는 label이 아닌 stable ID와 prompt ID를 제출한다. 첫 항목 자동 선택은 금지한다.
- mutable 작업은 인증된 owner host, subject, session, source event와 exact settings hash가 없으면
  fail closed한다.
- Project별 원본 checkout의 dirty state를 건드리지 않고 전용 branch·worktree만 사용한다.
- Stop, revision, dispatcher, merge 경계에는 설계에 지정한 `RISK(...)` 주석을 남긴다.
- 새 runtime은 start·stop·wait·result read-back 검증 전까지 광고하거나 조용히 fallback하지
  않는다.
- 기능 flag는 `chooser → v2 manual → Stop → safe_auto → multi full_auto` 순으로만 연다.
- 각 Task는 RED test를 먼저 확인한 뒤 최소 구현, 관련 test, 회귀 test, 작은 commit 순서로
  끝낸다.

## 실행 DAG와 장기 결과

```text
Wave 1  chooser contract → CLI → TUI/Desktop/Slack
                         ↘ Project discovery → Task setup
Wave 2  v2 records → one DB migration → Management service → Project runtime → multi merge
Wave 3  trusted events → revision inbox ─┬→ Forge tools
         stop barrier → process stop ────┤
         worker adapter ─────────────────┴→ exclusive dispatcher
Wave 4  dual-reader rollout → manual smoke → Stop smoke → safe_auto → multi full_auto
```

6개월 뒤 새 Project는 허용 workspace root에 checkout하는 것만으로 후보가 된다. Hermes 모델이나
worker runtime을 바꿔도 chooser, revision, Stop, merge barrier는 시스템 계약으로 유지된다.
실패 시 새 writer를 먼저 멈춘 뒤 Release A dual-reader로 돌아갈 수 있으며, migration 이전
release까지 되돌릴 때만 SQLite backup 복원이 필요하다.

---

### Task 1: transport-neutral chooser 계약과 Task setup 연결

**Files:**
- Create: `forge/ops/choice_prompt.py`
- Modify: `forge/ops/task_setup.py`
- Modify: `forge/hermes_plugin/infinity_forge/__init__.py`
- Create: `tests/ops/test_choice_prompt.py`
- Modify: `tests/ops/test_task_setup.py`
- Modify: `tests/hermes_plugin/test_infinity_forge_plugin.py`

**Interfaces:**
- Produces: `ChoicePrompt`, `Choice`, `ChoiceSubmission`
- Produces: `choice_prompt_id`, `choice_mode`, bounds, `expires_at`, stable choices
- Preserves: current exact-ID text fallback and `TurnResult.choices`

- [x] **Step 1: single·multiple·stale·expiry 계약 test 작성**

  single은 정확히 1개, multiple은 최소 1개를 요구하고 unknown·duplicate ID, stale prompt,
  만료된 submission이 외부 state를 바꾸지 않는 test를 먼저 작성한다.

- [x] **Step 2: RED 확인**

  Run: `python -m pytest tests/ops/test_choice_prompt.py tests/ops/test_task_setup.py -q`

  Expected: `ChoicePrompt`와 structured submission API가 없어 FAIL

- [x] **Step 3: immutable chooser value와 setup pending prompt 구현**

  setup draft의 실제 30분 만료와 `expires_at`을 같게 만들고, Cancel·Esc 의미는 선택 적용 0회로
  고정한다. 선택을 처리한 `TurnResult`는 항상 `handled`다.

- [x] **Step 4: plugin이 전체 chooser metadata를 fail-closed하게 전달**

  malformed metadata나 ID·label 중복은 choice를 버리고 모델을 호출하지 않는 오류를 반환한다.

- [x] **Step 5: 관련 test 통과 후 commit**

  Run: `python -m pytest tests/ops/test_choice_prompt.py tests/ops/test_task_setup.py tests/hermes_plugin/test_infinity_forge_plugin.py -q`

  Expected: PASS

  Commit: `feat: add structured Hermes chooser contract`

### Task 2: Classic CLI 방향키·복수 선택 모달

**Files:**
- Modify: `forge/hermes_change/installer.py`
- Modify: `tests/hermes/test_installer.py`
- Modify: `tests/hermes/test_pre_user_turn_contract.py`
- Modify: `tests/ops/test_workflow_contract.py`

**Interfaces:**
- Carries to Hermes: generic `_prompt_choice_modal`
- Single keys: `Up`, `Down`, `Enter`, numeric shortcut, `Esc`
- Multiple keys: `Up`, `Down`, `Space`, `Enter/Done`, `Esc`

- [x] **Step 1: 현재 text 목록만 붙이는 regression test를 방향키 modal 계약으로 확장**

  기존 `_prompt_text_input_modal`의 app-loop handoff와 draft restore를 재사용하며 curses/stdin reader를
  호출하지 않는 source transform test를 작성한다.

- [x] **Step 2: RED 확인**

  Run: `python -m pytest tests/hermes/test_installer.py tests/hermes/test_pre_user_turn_contract.py -q`

  Expected: installer가 `_choice_display_lines`만 생성해 FAIL

- [x] **Step 3: generic modal carried patch 구현**

  `RISK(breaking)` 주석과 함께 기존 slash-confirm wrapper는 유지한다. 빈 multiple의 Enter,
  non-TTY, modal 자체 timeout, Esc는 어떠한 stable ID도 제출하지 않게 한다. UI timeout은 server
  setup expiry를 변경하지 않는다.

- [x] **Step 4: stable structured submission을 동일 user-turn 경로로 재진입**

  선택은 label text가 아니라 `choice_prompt_id + selected_choice_ids`로 전달하며 handled result의
  API calls가 0인지 검증한다.

- [x] **Step 5: Forge와 Hermes fixture test 통과 후 commit**

  Run: `python -m pytest tests/hermes/ tests/ops/test_workflow_contract.py -q`

  Expected: PASS

  Commit: `feat: add keyboard chooser to Hermes CLI`

### Task 3: TUI·Desktop·Slack chooser surface

**Files:**
- Modify: `forge/hermes_change/installer.py`
- Modify: `tests/hermes/test_installer.py`
- Modify: `tests/ops/test_workflow_contract.py`
- Carried Hermes targets: `ui-tui/src/gatewayTypes.ts`
- Carried Hermes targets: `ui-tui/src/app/createGatewayEventHandler.ts`
- Carried Hermes targets: `ui-tui/src/components/prompts.tsx`
- Carried Hermes targets: `apps/desktop/src/lib/chat-messages.ts`
- Carried Hermes targets: `apps/desktop/src/app/session/hooks/use-message-stream/gateway-event.ts`
- Carried Hermes targets: new session chooser store/component
- Carried Hermes targets: Slack gateway action handler

**Interfaces:**
- TUI: Arrow/Space/Enter overlay
- Desktop: button·checkbox with keyboard/ARIA
- Slack: Block Kit button or multi-select; exact-ID fallback

- [x] **Step 1: message.complete chooser payload가 현재 각 UI에서 무시되는 RED fixture 작성**

  chooser가 session별로 격리되고 stale click, duplicate click, unknown ID를 제출하지 않는 test를
  포함한다.

- [x] **Step 2: TUI overlay와 Desktop chooser store/component source transforms 구현**

  normal `prompt.submit`에 label을 넣지 않고 chooser submission envelope를 보낸다. 일반 clarify
  request ID와 Forge chooser prompt ID를 섞지 않는다.

- [x] **Step 3: Slack structured control과 fallback 구현**

  방향키를 사용할 수 없는 Slack은 button/static select를 사용한다. action 검증이 불가능한
  client는 `ID — Label` 표시와 exact-ID 답장만 제공한다.

- [x] **Step 4: staged Hermes checkout에서 TypeScript test 실행**

  Run: `npm --prefix ui-tui test -- --runInBand`

  Run: `npm --prefix apps/desktop test -- --runInBand`

  Expected: chooser component와 기존 clarify test PASS

- [x] **Step 5: Forge carried-change test 후 commit**

  Run: `python -m pytest tests/hermes/ tests/ops/test_workflow_contract.py -q`

  Expected: PASS

  Commit: `feat: carry structured chooser across Hermes surfaces`

### Task 4: 범용 Project 모델·발견·검증

**Files:**
- Create: `forge/ops/task_projects.py`
- Create: `forge/ops/project_discovery.py`
- Create: `tests/ops/test_task_projects.py`
- Create: `tests/ops/test_project_discovery.py`

**Interfaces:**
- Produces: immutable `TaskProject`
- Produces: `discover_projects(working_directory, allowed_roots, limits)`
- Produces: canonical `OWNER/REPO` and 64-hex `project_id`

- [x] **Step 1: 임의 이름 저장소·Git root·상위 폴더 발견 test 작성**

  SSH/HTTPS remote 동일화, depth 3/count 64/time 5초, hard limit 8/256을 포함한다.

- [x] **Step 2: 탈출·중복·remote 불일치 RED test 작성**

  symlink/junction/root 탈출, 같은 repo의 다른 worktree 중복, credential remote, missing remote,
  GitHub canonical repo/base branch/base commit 불일치를 거부한다.

- [x] **Step 3: RED 확인 후 순수 discovery와 validator 구현**

  Run: `python -m pytest tests/ops/test_task_projects.py tests/ops/test_project_discovery.py -q`

  Expected before implementation: FAIL; after implementation: PASS

- [x] **Step 4: deterministic project ID와 timeout 실패 표시 검증 후 commit**

  Commit: `feat: discover and validate generic task projects`

### Task 5: Projects·merge order를 Task setup에 추가

**Files:**
- Modify: `forge/ops/task_setup.py`
- Modify: `forge/hermes_plugin/infinity_forge/__init__.py`
- Modify: `tests/ops/test_task_setup.py`
- Modify: `tests/hermes_plugin/test_infinity_forge_plugin.py`

**Interfaces:**
- Flow: `mode → Projects → task_flow → merge_mode → merge_order? → content → Confirm`
- Consumes: trusted `working_directory`, Project discovery results

- [x] **Step 1: exact flow와 필수 선택 RED test 작성**

  Projects 0개, multi `full_auto` merge order 누락·중복, 이전 Task 값 자동 불러오기를 거부한다.

- [x] **Step 2: multiple chooser와 동적 stable project IDs 연결**

  Project path나 label을 선택 authority로 쓰지 않고 Confirm 직전에 binding을 다시 검증한다.

- [x] **Step 3: preview가 Management와 각 Project를 구분하는지 검증**

  Run: `python -m pytest tests/ops/test_task_setup.py tests/hermes_plugin/test_infinity_forge_plugin.py -q`

  Expected: PASS, handled turns의 model calls 0

- [x] **Step 4: commit**

  Commit: `feat: select projects in Hermes task setup`

### Task 6: v2 request·settings 순수 데이터 계약

**Files:**
- Create: `forge/ops/task_settings_v2.py`
- Modify: `forge/ops/task_service.py`
- Create: `tests/ops/test_task_settings_v2.py`
- Modify: `tests/ops/test_plain_names.py`

**Interfaces:**
- Produces: `TaskRequestV2`, `TaskSettingsV2`
- Preserves: `TaskCreationRequest`, `TaskSettings` v1

- [x] **Step 1: exact JSON field·type·canonical hash RED test 작성**

  sorted Projects, exact `merge_order` permutation, owner host UUID, UTC Z, unknown/extra field 거부와
  v1 hash 불변 fixture를 포함한다.

- [x] **Step 2: immutable v2 records와 parser 구현**

  공개 schema 경계에 `RISK(breaking)`을 남기고 v1 class나 format marker를 수정하지 않는다.

- [x] **Step 3: test 통과 후 commit**

  Run: `python -m pytest tests/ops/test_task_settings_v2.py tests/ops/test_plain_names.py tests/ops/test_task_settings.py -q`

  Expected: PASS

  Commit: `feat: add exact v2 task records`

### Task 7: 단일 owner의 SQLite v2 migration

**Files:**
- Create: `forge/ops/task_database.py`
- Modify: `forge/ops/task_settings.py`
- Create: `tests/ops/test_task_database.py`
- Modify: `tests/ops/test_task_settings.py`

**Interfaces:**
- Migrates: exact v1 schema → additive v2 schema, `user_version=2`
- Owns tables: requests/settings/events/projects/messages/message_events/revisions/stops/session_bindings/
  access/surface_events/runtime_runs
- Provides: shared `BEGIN IMMEDIATE` transaction facade

- [ ] **Step 1: seeded v1 DB readback·hash 불변 test 작성**

  migration 전후 v1 row, lifecycle event, public get API byte-equivalent 결과를 확인한다.

- [ ] **Step 2: DDL 중간 실패 rollback·재실행 RED test 작성**

  object exact set 검증, permissions 0600/owner ACL, SQLite quick_check와 backup restore도 포함한다.

- [ ] **Step 3: one-transaction migration 구현**

  `RISK(breaking)`과 `RISK(data-loss)` 주석을 migration/restore 경계에 둔다. v1
  `task_outbox.py`는 손대지 않는다.

- [ ] **Step 4: test 통과 후 commit**

  Run: `python -m pytest tests/ops/test_task_database.py tests/ops/test_task_settings.py tests/ops/test_task_outbox.py -q`

  Expected: PASS

  Commit: `feat: add atomic v2 task database migration`

### Task 8: Management parent 생성과 Project 실행 항목 준비

**Files:**
- Modify: `forge/ops/task_service.py`
- Modify: `forge/ops/github.py`
- Modify: `tests/ops/test_task_service.py`
- Modify: `tests/ops/test_task_issue_adapter.py`

**Interfaces:**
- Sequence: `prepared → parent_issue_bound → settings_activated`
- Idempotency key: `request_id + project_id + step`
- Produces: central parent plus Project execution registry

- [ ] **Step 1: Management repo와 Project repo가 다른 RED test 작성**

  partial Issue/card failure 뒤 재시도해 parent·Project item이 중복되지 않는 test를 포함한다.

- [ ] **Step 2: immutable marker와 mutable progress section을 분리**

  v1 전체 body 검증은 보존하고 v2만 Forge-owned progress section을 exact 재생성한다.

- [ ] **Step 3: bound와 active settings를 원자 활성화**

  모든 Project root card ID가 bind된 뒤에만 `dispatch_ready`를 한 transaction으로 연다.

- [ ] **Step 4: 관련 test와 commit**

  Run: `python -m pytest tests/ops/test_task_service.py tests/ops/test_task_issue_adapter.py -q`

  Expected: PASS

  Commit: `feat: create centrally managed project tasks`

### Task 9: Project별 worktree·카드·PR 실행

**Files:**
- Create: `forge/ops/task_worktrees.py`
- Modify: `forge/ops/hermes.py`
- Modify: `forge/ops/task_runtime.py`
- Modify: `forge/scripts/task-flow-worker.py`
- Create: `tests/ops/test_task_worktrees.py`
- Modify: `tests/ops/test_adapters.py`
- Modify: `tests/ops/test_task_runtime.py`
- Modify: `tests/ops/test_task_worker_cli.py`

**Interfaces:**
- v2 card key includes `request_id + project_id + step`
- v2 snapshot carries exact `TaskProject`
- PR repository must equal `project.repository`

- [ ] **Step 1: arbitrary Project worktree/branch RED test 작성**

  dirty original checkout 보존, deterministic branch, collision, stale base commit, wrong remote/PR repo를
  검증한다.

- [ ] **Step 2: v2 card format과 runtime enumeration 구현**

  v1 card format과 `--repo/--workspace` drain 경로는 그대로 둔다. v2 worker는 DB registry에서
  활성 Projects를 동적으로 열거한다.

- [ ] **Step 3: 모든 safe point에서 exact Project/settings guard 확인**

  Project A 결과가 B에 들어가거나 한 Project만 완료되어 parent가 완료되는 것을 거부한다.

- [ ] **Step 4: test와 commit**

  Run: `python -m pytest tests/ops/test_task_worktrees.py tests/ops/test_adapters.py tests/ops/test_task_runtime.py tests/ops/test_task_worker_cli.py -q`

  Expected: PASS

  Commit: `feat: run task flow in selected project worktrees`

### Task 10: parent 집계와 multi-Project merge

**Files:**
- Modify: `forge/ops/merge_decision.py`
- Modify: `forge/ops/merge_runtime.py`
- Modify: `forge/scripts/merge-worker.py`
- Modify: `forge/scripts/issue-status-sync.py`
- Modify: `tests/ops/test_merge_decision.py`
- Modify: `tests/ops/test_merge_worker.py`
- Modify: `tests/ops/test_task_worker_cli.py`

**Interfaces:**
- Aggregates all Project readiness before any multi full-auto write
- Terminal: all merged → `merged`; some merged → `partially_merged`

- [ ] **Step 1: manual·multi safe_auto write-0 RED tests 작성**

  full_auto도 exact merge order와 모든 Project current evidence가 없으면 merge 0회를 검증한다.

- [ ] **Step 2: aggregate barrier와 ordered expected-head merge 구현**

  `RISK(race)` 주석과 함께 한 Project merge 직전/직후 실패 순서를 모두 test한다.

- [ ] **Step 3: partial merge readback과 parent needs-decision 투영**

  자동 rollback 없이 남은 merge를 차단하고 merge된/실패한/남은 Project를 표시한다.

- [ ] **Step 4: test와 commit**

  Run: `python -m pytest tests/ops/test_merge_decision.py tests/ops/test_merge_worker.py tests/ops/test_task_worker_cli.py -q`

  Expected: PASS

  Commit: `feat: coordinate multi-project merge barriers`

### Task 11: durable source event와 인증된 turn context

**Files:**
- Create: `forge/ops/surface_events.py`
- Modify: `forge/hermes_change/installer.py`
- Modify: `tests/ops/test_surface_events.py`
- Modify: `tests/hermes/test_pre_user_turn_contract.py`
- Modify: `tests/hermes/test_installer.py`
- Carried Hermes targets: CLI local outbox, TUI persisted submission ID, gateway platform event ID,
  `agent/conversation_loop.py`, `agent/tool_executor.py`

**Interfaces:**
- Trusted context: owner host, subject ID, session ID, surface, source event ID, cwd
- Mutating tool schema does not expose trusted fields

- [ ] **Step 1: resend·response-loss·restart dedupe RED tests 작성**

  Desktop/TUI persisted client ID, CLI durable local outbox, Slack platform message/update ID를 검증한다.

- [ ] **Step 2: source event store와 carried propagation 구현**

  `RISK(security)` 주석과 함께 모델 args의 같은 이름을 버리고 trusted context로 덮어쓴다.
  event ID가 없으면 mutating Forge Tool은 write 0회로 거부한다.

- [ ] **Step 3: DB permissions와 retention metadata 검증**

  Run: `python -m pytest tests/ops/test_surface_events.py tests/hermes/test_pre_user_turn_contract.py tests/hermes/test_installer.py -q`

  Expected: PASS

- [ ] **Step 4: commit**

  Commit: `feat: bind trusted source events to Hermes turns`

### Task 12: durable Task inbox와 revision 재확인

**Files:**
- Create: `forge/ops/task_messages.py`
- Create: `forge/ops/task_revisions.py`
- Create: `tests/ops/test_task_messages.py`
- Create: `tests/ops/test_task_revisions.py`
- Modify: `forge/ops/task_runtime.py`
- Modify: `forge/ops/merge_runtime.py`

**Interfaces:**
- Atomic: append message + `revision_requested`
- Produces: runtime-neutral message packet, hash, included/applied/rejected events

- [ ] **Step 1: idempotency·limits·ordering RED tests 작성**

  64 KiB/message, 100 messages/revision, 1 MiB/revision, same source event dedupe, terminal rejection을
  포함한다.

- [ ] **Step 2: active→changing barrier transaction 구현**

  `RISK(race)` 주석과 함께 새 배차, result accept, GitHub write, merge를 즉시 막고 Confirm 전에는
  worker prompt에 메시지를 노출하지 않는다.

- [ ] **Step 3: Confirm/Cancel/Resume와 worker ack event 구현**

  stale result, pending message, revision Confirm과 Stop 경쟁 test를 추가한다.

- [ ] **Step 4: test와 commit**

  Run: `python -m pytest tests/ops/test_task_messages.py tests/ops/test_task_revisions.py tests/ops/test_task_runtime.py tests/ops/test_merge_worker.py -q`

  Expected: PASS

  Commit: `feat: add durable task revisions and inbox`

### Task 13: deterministic Stop 명령과 stoppable barrier

**Files:**
- Create: `forge/ops/stop_command.py`
- Create: `forge/ops/task_stop.py`
- Create: `tests/ops/test_stop_command.py`
- Create: `tests/ops/test_task_stop.py`
- Modify: `forge/ops/task_setup.py`

**Interfaces:**
- Parses only exact full commands
- `get_stoppable`: prepared, bound, active, changing, stopping
- Atomic: durable stop request + `stop_requested` barrier

- [ ] **Step 1: command grammar positive/negative RED matrix 작성**

  `forge stop #21`, `#21 실행 중단`은 처리하고 질문·부정·인용·code block·substring은 일반
  대화로 보낸다.

- [ ] **Step 2: stoppable selection과 owner-host guard 구현**

  대상 여러 개면 chooser 전 state write 0회, 다른 host면 owner host 표시와 external write 0회다.

- [ ] **Step 3: revision/Resume/merge 경쟁 barrier 구현**

  `RISK(race)` 주석과 transaction serialization으로 어느 순서에서도 worker 재개를 막는다.

- [ ] **Step 4: test와 commit**

  Run: `python -m pytest tests/ops/test_stop_command.py tests/ops/test_task_stop.py tests/ops/test_task_setup.py -q`

  Expected: PASS

  Commit: `feat: add deterministic task stop barrier`

### Task 14: Kanban 원자 중단과 정확한 프로세스 종료

**Files:**
- Create: `forge/ops/kanban_stop.py`
- Create: `forge/ops/process_identity.py`
- Create: `tests/ops/test_kanban_stop.py`
- Create: `tests/ops/test_process_identity.py`

**Interfaces:**
- Atomic cards: nonterminal matching cards → archived, PID/run capture
- Process: Linux process group/cgroup or Windows Job + start identity
- Completion: descendants 0 and all cards terminal

- [ ] **Step 1: 카드 상태·dispatcher race RED tests 작성**

  triage/todo/scheduled/ready/running/blocked/review를 한 transaction으로 archive하고 done/archived는
  보존한다.

- [ ] **Step 2: PID reuse·wrong host·wrong task security RED tests 작성**

  process name 검색이나 전역 kill은 test에서 금지한다.

- [ ] **Step 3: platform adapters 구현**

  process termination에 `RISK(side-effect)`, start identity 검증에 `RISK(security)`를 남긴다.
  TERM timeout 뒤 exact group/job만 강제 종료하고 read-back한다.

- [ ] **Step 4: test와 commit**

  Run: `python -m pytest tests/ops/test_kanban_stop.py tests/ops/test_process_identity.py -q`

  Expected: PASS

  Commit: `feat: stop exact task cards and worker trees`

### Task 15: Stop saga remote readback·정리·복구

**Files:**
- Modify: `forge/ops/task_stop.py`
- Modify: `forge/ops/github.py`
- Create: `forge/scripts/task-stop-reconcile.py`
- Modify: `tests/ops/test_task_stop.py`
- Create: `tests/ops/test_task_stop_reconcile.py`

**Interfaces:**
- Guard: `guard_stop_cleanup(stop_request_id)`
- Results: cancelled, completed_before_stop, completed_with_partial_merge, cleanup_incomplete

- [ ] **Step 1: 각 saga 단계 crash injection RED tests 작성**

  concurrent Stop N회, already-dead PID, TERM ignore, prepared request, Issue 번호 없음, late worker
  result를 포함한다.

- [ ] **Step 2: PR/merge remote readback과 terminal convergence 구현**

  merge 0/all/some을 정확히 구분하며 Stop cleanup 권한은 label 제거, comment, parent close에만
  제한한다.

- [ ] **Step 3: reconciler와 idempotent event/comment 구현**

  cleanup 실패만 `cleanup_incomplete`; branch/worktree/PR은 삭제하지 않고 위치를 기록한다.

- [ ] **Step 4: test와 commit**

  Run: `python -m pytest tests/ops/test_task_stop.py tests/ops/test_task_stop_reconcile.py -q`

  Expected: PASS

  Commit: `feat: reconcile task stop to remote truth`

### Task 16: Hermes 메인 에이전트 Forge Tool

**Files:**
- Create: `forge/ops/forge_tools.py`
- Modify: `forge/hermes_plugin/infinity_forge/__init__.py`
- Modify: `forge/hermes_plugin/infinity_forge/plugin.yaml`
- Create: `tests/hermes_plugin/test_forge_tools.py`
- Modify: `tests/hermes_plugin/test_infinity_forge_plugin.py`
- Modify: `tests/ops/test_workflow_contract.py`

**Interfaces:**
- Tools: `list_tasks`, `task_status`, `send_to_task`, `stop_task`
- Toolset: `forge`, main profile only

- [ ] **Step 1: 실제 tool registry exposure RED tests 작성**

  CLI, TUI, Desktop, Slack main profile에는 4개가 있고 builder/reviewer/deep_checker/fix에는 0개인지
  검증한다.

- [ ] **Step 2: authenticated context middleware와 handlers 구현**

  List/Status는 read-only, Send는 Task 12 revision, Stop은 Task 15 service만 호출한다. 모델이 만든
  subject/session/source event 값은 무시한다.

- [ ] **Step 3: Task 선택 규칙과 cross-host/access tests 구현**

  Task 여러 개면 chooser 전 write 0회, 종료 Task Send 거부, owner/operator permission을 검증한다.

- [ ] **Step 4: test와 commit**

  Run: `python -m pytest tests/hermes_plugin/test_forge_tools.py tests/hermes_plugin/test_infinity_forge_plugin.py tests/ops/test_workflow_contract.py -q`

  Expected: PASS

  Commit: `feat: connect Hermes main agent to managed tasks`

### Task 17: runtime-neutral worker packet과 adapter

**Files:**
- Create: `forge/ops/worker_prompt.py`
- Create: `forge/ops/worker_runtime.py`
- Create: `tests/ops/test_worker_prompt.py`
- Create: `tests/ops/test_worker_runtime.py`
- Modify: `forge/ops/task_runtime.py`

**Interfaces:**
- Adapter: `start`, `stop`, `wait`, `result`, `process_identity`
- Packet: exact settings hash + confirmed message IDs/hash as untrusted user block

- [ ] **Step 1: canonical packet·prompt injection boundary RED tests 작성**

  message가 system/developer prompt로 승격되지 않고 native/Codex/Claude가 같은 bytes/hash를
  받는지 검증한다.

- [ ] **Step 2: native Hermes adapter와 runtime registry 구현**

  start 직전 active/revision/stop guard, run identity 기록, result 전 guard를 수행한다.

- [ ] **Step 3: Codex App Server·Claude unavailable/fail-closed tests 작성**

  install/auth/stop/result 검증 전에는 선택·fallback되지 않는다. 기존 subscription fallback의
  `_default_spawn` wrapper는 superseded 처리한다.

- [ ] **Step 4: test와 commit**

  Run: `python -m pytest tests/ops/test_worker_prompt.py tests/ops/test_worker_runtime.py tests/ops/test_task_runtime.py -q`

  Expected: PASS

  Commit: `feat: add verified Forge worker runtime adapters`

### Task 18: 단일 forge-dispatcher와 3-way routing

**Files:**
- Create: `forge/ops/forge_dispatcher.py`
- Create: `forge/scripts/forge-dispatcher.py`
- Create: `tests/ops/test_dispatch_routing.py`
- Create: `tests/ops/test_dispatcher_singleton.py`
- Modify: `tests/ops/test_workflow_contract.py`

**Interfaces:**
- Calls: `hermes_cli.kanban_db.dispatch_once(..., spawn_fn=route_spawn)`
- Route: exact Forge → forge spawn; no Forge → default spawn; partial/mismatch → block/no spawn
- Owns process-lifetime OS singleton lock

- [ ] **Step 1: three-way routing RED matrix 작성**

  marker/binding/settings/project/host 조합을 모두 test하고 partial Forge marker는 default spawn으로
  빠지지 않게 한다.

- [ ] **Step 2: singleton loss·double dispatcher RED tests 작성**

  `RISK(race)` 주석과 함께 lock 획득/유지 실패 시 claim 0회를 검증한다.

- [ ] **Step 3: dispatcher loop와 default spawn adapter 구현**

  `dispatch_ready`, exact settings hash, revision/stop barrier, runtime availability를 claim 직후 다시
  확인한다.

- [ ] **Step 4: test와 commit**

  Run: `python -m pytest tests/ops/test_dispatch_routing.py tests/ops/test_dispatcher_singleton.py tests/ops/test_workflow_contract.py -q`

  Expected: PASS

  Commit: `feat: add exclusive Forge task dispatcher`

### Task 19: dual-reader 배포·서비스·rollback 계약

**Files:**
- Modify: `forge/scripts/deploy-vps.sh`
- Modify: `forge/scripts/deploy-windows.ps1`
- Modify: `forge/scripts/deploy.ps1`
- Modify: `forge/scripts/system-check.sh`
- Modify: `tests/ops/test_linux_deploy_lock.py`
- Modify: `tests/ops/test_windows_deployment.py`
- Modify: `tests/ops/test_workflow_contract.py`

**Interfaces:**
- Release A: schema v2 dual-reader, v2 creation/dispatch off
- Release B: chooser + v2 manual on
- Services: one dispatcher and one Stop reconciler per host

- [ ] **Step 1: old daemon/Gateway/timer exclusion RED tests 작성**

  external `hermes kanban daemon` off, `kanban.dispatch_in_gateway=false`,
  `HERMES_KANBAN_DISPATCH_IN_GATEWAY=0`, old stage/merge timer disabled를 검사한다.

- [ ] **Step 2: backup·restore·host UUID·workspace roots 계약 구현**

  SQLite/WAL 일관 backup과 실제 restore/quick_check, release pointer rollback, Project 이름 없는
  allowed roots 설정을 검증한다.

- [ ] **Step 3: feature flags와 health/readback smoke 구현**

  dispatcher PID/lock, Gateway dispatch off, plugin release commit, DB schema, Tool exposure, chooser
  modal을 system check에 추가한다.

- [ ] **Step 4: test와 shell syntax 후 commit**

  Run: `python -m pytest tests/ops/test_linux_deploy_lock.py tests/ops/test_windows_deployment.py tests/ops/test_workflow_contract.py -q`

  Run: `& 'C:\Program Files\Git\bin\bash.exe' -n forge/scripts/deploy-vps.sh`

  Expected: PASS

  Commit: `feat: deploy hybrid task control services safely`

### Task 20: 전체 검증·push·EC2/VPS/Windows 단계 활성화

**Files:**
- Modify if needed: `docs/weapon/specs/2026-07-18-hermes-hybrid-task-control-design.md`
- Modify if needed: `docs/weapon/plans/2026-07-18-hermes-hybrid-task-control.md`
- Deployment evidence only: no Project source changes unless smoke Task selects it

**Interfaces:**
- Produces: test evidence, release commit readback, real chooser screenshots/logs, smoke Task IDs/PRs

- [ ] **Step 1: focused suites와 전체 suite fresh 실행**

  Run: `python -m pytest tests/hermes/ tests/hermes_plugin/ tests/ops/ -q`

  Run: `python -m pytest tests/ -q`

  Expected: all PASS

- [ ] **Step 2: code review subagent와 risk audit 수행**

  설계 AC 1~52, chooser 추가 AC 6-A~6-C, schema/Stop/dispatcher race, v1 compatibility를 독립 검토하고
  지적을 수정한 뒤 fresh test를 다시 실행한다.

- [ ] **Step 3: branch push와 Release A 배포**

  Push: `codex/hybrid-task-control`

  Windows, EC2, VPS에 dual-reader/v2-off로 배포하고 commit·plugin·schema·backup restore를
  read-back한다.

- [ ] **Step 4: Release B chooser·manual smoke**

  EC2의 임의 외부 cwd에서 `hermes`를 시작해 `↑/↓ Enter`, Projects `Space`, task_flow,
  merge_mode, Confirm을 수행한다. Management Issue는 Infinity Forge, commit/push/PR은 선택한
  Project인지 확인한다.

- [ ] **Step 5: Stop·safe_auto·multi full_auto 순차 smoke**

  실행 중 `forge stop #N`이 모델 호출 없이 descendants 0/card terminal/remote readback으로
  끝나는지 확인한다. 그 뒤 single safe_auto, 마지막으로 dependency order가 있는 multi
  full_auto를 각각 별도 Task로 검증한다.

- [ ] **Step 6: 배포 결과와 rollback 위치 기록 후 final commit**

  EC2/VPS/Windows의 release commit, owner host, service 상태, Task/PR URL, 남은 flag를 변경이력에
  기록한다. 실패한 환경은 성공으로 표시하지 않고 이전 release와 backup 위치를 함께 보고한다.

  Commit: `docs: record hybrid task control rollout evidence`

---

## 변경이력

- 2026-07-18 | 사용자 진행 및 방향키 chooser 확정 | 변경: CLI/TUI 방향키, 복수 Project Space
  선택부터 v2 중앙 관리, revision, Stop saga, worker adapter, 단일 dispatcher, 세 환경 rollout까지
  20개 TDD Task로 분해 | 검증: Infinity Forge v1 결합점과 Hermes v0.18.2의 CLI modal,
  PluginContext Tool, middleware, TUI/Desktop/gateway 전달 경로를 세 독립 조사로 대조
