# Hermes 혼합형 대화·Task 제어와 범용 Project 실행 설계

## 목적

Hermes의 사용자 대화 창구는 Hermes 메인 에이전트로 유지한다. 사용자는 같은 대화에서
일반 질문을 하고, 실행 중인 Task의 상태를 묻고, 추가 설명을 전달할 수 있다. 다만
`mode`, Project, `task_flow`, `merge_mode`, Confirm과 명확한 중단 명령은 LLM의 해석이나
응답 성공 여부에 의존하지 않고 Infinity Forge 시스템이 직접 처리한다.

Task 관리는 `immortal0900/INFINITY_FORGE`에 중앙화하되 실제 코드 변경, commit, push,
PR은 Task마다 사용자가 선택한 GitHub Project에서 수행한다. Cognet9를 포함한 특정
저장소 이름은 코드나 배포 설정에 넣지 않는다.

이 설계는 사용자가 확정한 혼합형 구조를 구현한다.

```text
사용자 채팅
  → 시스템 제어 확인
     ├─ 시작 선택·Confirm·명확한 중단 명령 → 시스템이 직접 처리
     └─ 그 밖의 대화 → Hermes 메인 에이전트
                          ├─ 일반 답변
                          ├─ Task 상태 조회
                          └─ 실행 중 Task에 메시지 전달
```

## 변경하지 않는 공식 이름

기존 공개 이름과 값은 그대로 유지한다.

```yaml
mode: chat | task
task_flow: build | build_review | build_review_deep_check
merge_mode: manual | safe_auto | full_auto
role: builder | reviewer | deep_checker | fix
```

`interaction_mode`, `assurance_policy`, `merge_policy`, `P1`, `P2`, `P3`,
`direct`, `reviewed`, `adversarial`을 다시 활성 이름으로 만들지 않는다.

새 화면 용어는 다음처럼 동작이 바로 드러나는 단어를 사용한다.

| 화면 용어 | 뜻 |
|---|---|
| `Projects` | 실제 코드를 변경하고 PR을 만드는 저장소 목록 |
| `Management` | 중앙 Issue와 전체 상태를 보관하는 Infinity Forge |
| `Send to Task` | 메인 에이전트가 사용자 메시지를 실행 중 Task에 전달 |
| `Stop Task` | 재배차를 막고 작업자를 종료한 뒤 Task를 취소 |

## 검토한 구조

### 1. 시스템 명령만 사용

모든 Task 조작을 버튼이나 고정 명령으로만 수행한다. 잘못된 해석은 적지만 사용자가
대화 중 요구사항을 설명하거나 상태를 묻기가 불편하다.

### 2. 메인 에이전트가 모든 입력을 판단

가장 자연스럽지만 LLM이 멈추거나 잘못 해석하면 중단 명령까지 실패한다. 같은 문장을
다른 Task로 보내거나 설명 질문을 중단 명령으로 오인할 수도 있다.

### 3. 시스템 제어와 메인 대화를 분리하는 혼합형 — 확정

선택·Confirm·명확한 중단은 시스템이 처리한다. 나머지 입력은 메인 에이전트가 받고,
구조화된 Task 도구로 상태를 조회하거나 메시지를 전달한다. 자연스러운 대화와 확실한
제어를 함께 유지할 수 있다.

## 사용자 입력의 실제 수신자

모든 입력은 기술적으로 `pre_user_turn` 시스템 확인을 먼저 지난다. 이 확인은 대화하는
에이전트가 아니라 짧은 제어 분류기다. 다음 입력만 `handled`로 끝내고 모델을 호출하지
않는다.

- 새 대화의 `mode` 선택
- `task`의 Project, `task_flow`, `merge_mode` 선택
- Task 미리보기의 Confirm 또는 Cancel
- `forge stop`, `forge stop #21`, `#21 실행 중단`처럼 완전한 중단 명령
- 중단 대상이 여러 개일 때 표시되는 Task 선택

그 밖의 일반 입력은 `continue` 또는 `replace`로 현재 Hermes 메인 에이전트에 전달한다.
따라서 사용자가 느끼는 대화 상대는 Hermes 메인 에이전트다.

제어 확인은 키워드 포함 여부로 판단하지 않는다. 전체 문장이 허용된 명령 문법과
일치할 때만 중단한다. 다음 질문은 일반 대화로 보내야 한다.

- `중단 기능은 어떻게 동작해?`
- `Task를 중단하지 말고 원인만 설명해.`
- `forge stop이라는 명령이 있어?`

질문형, 부정형, 따옴표 안의 인용, Markdown 코드 블록 안의 명령은 제어 입력으로
인식하지 않는다. Desktop, TUI, CLI, Slack은 같은 정규화 규칙과 같은 명령 문법을
사용한다.

## 새 대화와 Task 생성 흐름

새 대화의 첫 사용자 입력을 잠시 보관하고 다음 순서로 진행한다.

```text
첫 사용자 입력
  → mode
     ├─ chat
     │   → 보관한 입력을 Hermes 메인 에이전트에 그대로 전달
     │   → GitHub Issue와 Kanban 카드 생성 0회
     │
     └─ task
         → Projects 선택
         → task_flow 선택
         → merge_mode 선택
         → 여러 Project + full_auto이면 merge_order 선택
         → Task 내용 입력 또는 보관한 첫 입력 사용
         → 전체 미리보기
         → Confirm Task
         → 중앙 parent Issue와 Project별 실행 항목 생성
         → 현재 대화를 chat으로 전환
```

`task`에서는 Projects, `task_flow`, `merge_mode` 중 하나라도 빠지면 미리보기나 실행으로
넘어가지 않는다. 여러 Project의 `full_auto`에서는 `merge_order`도 반드시 직접 선택한다.
이전 Task의 값을 불러오거나 기본값을 자동 선택하지 않는다.

## 범용 Project 발견과 선택

### 발견 순서

1. Hermes surface가 해당 사용자 turn의 실제 `working_directory`를 hook에 전달한다.
2. 현재 위치가 Git 저장소 안이면 정확한 Git root를 첫 후보로 표시한다.
3. 현재 위치가 여러 저장소의 상위 폴더이면 허용된 workspace root 안에서 제한된
   깊이와 개수만 탐색한다.
4. 각 후보의 Git remote를 GitHub의 `OWNER/REPO` 형식으로 정규화한다.
5. 사용자가 하나 이상의 Project를 선택하고 `Done`으로 확정한다.
6. 후보에 원하는 Project가 없으면 허용 root 안의 절대경로 또는 현재 위치 기준
   상대경로를 명시적으로 추가할 수 있다.

배포 설정에는 저장소 목록이 아니라 허용된 상위 경로만 둔다.

```text
INFINITY_FORGE_MANAGEMENT_REPOSITORY=immortal0900/INFINITY_FORGE
INFINITY_FORGE_WORKSPACE_ROOTS=/home/ec2-user/work
```

기존 `INFINITY_FORGE_REPOSITORY`는 v1 단일 저장소 Task의 기존 의미로만 읽고, v2의
Management 저장소 의미로 재해석하지 않는다.

Windows, EC2, VPS는 각 환경에 맞는 workspace root를 별도로 갖는다. 서비스 프로세스의
현재 폴더를 사용자 workspace로 추측하지 않는다. 실제 turn의 폴더를 받을 수 없으면
허용 root의 후보 목록만 표시한다.

기본 탐색 제한은 workspace root 기준 깊이 3, 후보 64개, 5초다. 운영 설정으로 줄일 수
있지만 hard limit인 깊이 8과 후보 256개를 넘길 수 없다. 시간이나 개수 제한에 걸리면
일부 목록을 전체인 것처럼 표시하지 않고 탐색 실패로 알린다.

### Project 안전 확인

Confirm 직전, 작업 배차 직전, PR 확인 직전, merge 직전에 다음을 다시 확인한다.

- 정규화된 실제 경로가 허용된 workspace root 안에 있음
- symlink 또는 junction이 허용 root 밖으로 빠져나가지 않음
- Git root와 선택한 workspace가 정확히 일치함
- remote에 자격 증명이나 로컬 파일 경로가 포함되지 않음
- SSH와 HTTPS remote가 같은 `OWNER/REPO`로 정규화됨
- GitHub에서 조회한 실제 저장소와 remote가 일치함
- base branch와 Confirm 당시 base commit이 기록됨
- 같은 저장소의 다른 worktree를 중복 Project로 선택하지 않음

remote가 여러 개면 사용자가 실제 push에 사용할 `remote_name`을 선택한다. 선택한 remote의
`OWNER/REPO`와 PR 대상 repository는 정확히 같아야 한다. fork remote에서 upstream으로
PR을 만드는 형태는 첫 구현 범위에서 지원하지 않는다. `base_branch`는 GitHub default
branch를 먼저 표시하되 다른 branch를 선택하면 Confirm 화면에 명시하고 존재와 현재
base commit을 다시 확인한다.

Project 이름과 경로는 코드에 하드코딩하지 않는다. 새 저장소를 허용 root에 추가하면
다음 Task 선택에서 자동으로 후보가 된다.

## 중앙 관리와 실제 코드 작업 분리

기존 v1의 `TaskCreationRequest.repository`와 Task 설정 `repository`는 중앙 Issue와
실제 코드 저장소가 같은 단일 저장소를 뜻한다. v1 필드의 의미를 중앙 저장소로 몰래
바꾸지 않는다. 새 v2 계약에서 `management_repository`와 `projects`를 명시적으로
분리한다. `mode`, `task_flow`, `merge_mode`의 이름과 값은 그대로 유지한다.

```text
TaskCreationRequest
  request_id
  management_repository  # 중앙 Management 저장소
  projects           # 하나 이상의 TaskProject
  content
  task_flow
  merge_mode
  merge_order        # 여러 Project + full_auto일 때 확인한 project_id 순서
  confirmed_by
  confirmed_at

TaskProject
  project_id         # 아래 고정 값에서 시스템이 계산
  repository         # 실제 commit, push, PR 저장소
  workspace          # 정규화된 절대 Git root
  remote_name
  base_branch
  base_commit
  host_id
```

`projects`는 repository, workspace, base branch 순으로 정렬한 뒤 Task 설정 hash에
포함한다. 빈 목록, 중복 저장소, 허용 root 밖 경로, 알 수 없는 host는 거부한다.
Confirm 후 Project를 바꾸려면 새 Task 설정을 다시 확인해야 한다.

`merge_order`는 여러 Project와 `full_auto`를 함께 선택했을 때만 모든 `project_id`를 정확히
한 번 포함한다. 이외 조합에서는 null이다. 화면에서 Project 순서를 직접 정하고 전체
미리보기에서 다시 확인한다.

`host_id`는 hostname이나 IP가 아니라 설치 시 한 번 만든 UUID다. Task를 Confirm한 host를
`task_owner_host`로 저장하며 한 Task의 모든 Project는 이 host에 있어야 한다. Windows,
EC2, VPS는 각각 독립된 Task DB와 owner host를 갖는다. Desktop, TUI, CLI, Slack이 같은
Gateway에 연결된 경우에는 같은 owner host로 취급한다. 다른 host에서 들어온 Send 또는
Stop은 전달을 추측하지 않고 owner host를 알려 주며 외부 write 0회로 거부한다. 첫
구현에서는 cross-host Task 실행과 command forwarding을 지원하지 않는다.

`project_id`는 다음 여섯 필드의 key-sorted UTF-8 JSON을 SHA-256으로 계산한 64자리
소문자 hex다: `host_id`, `repository`, `workspace`, `remote_name`, `base_branch`,
`base_commit`. 경로나 remote가 바뀌면 같은 Project로 간주하지 않는다.

각 Project는 전용 branch와 전용 worktree를 사용한다. 원본 checkout의 미커밋 변경은
읽거나 덮어쓰지 않는다. 실제 commit, push, PR은 선택한 Project 저장소에서만 수행한다.

## 중앙 Issue와 실행 항목

중앙 Management 저장소에는 다음 구조를 만든다.

```text
parent Issue: 전체 Task 설정과 Project 진행표
  ├─ Project A 실행 항목 → Project A worktree → Project A PR
  ├─ Project B 실행 항목 → Project B worktree → Project B PR
  └─ Project C 실행 항목 → Project C worktree → Project C PR
```

- 사용자에게 표시하는 Task 번호는 parent Issue 번호다.
- Project별 실행 상태는 중앙 DB와 parent Issue의 Project 진행표에 기록한다.
- Project별 Build, Review, Deep Check, Fix는 같은 Project와 같은 PR만 이어받는다.
- Project A의 결과나 PR URL을 Project B가 사용할 수 없다.
- parent 상태는 모든 Project 상태에서 계산하며 한 Project 완료만으로 전체 완료 처리하지
  않는다.
- Kanban의 실행 의존성용 `parent` 연결을 관리 계층에 오용하지 않는다. Project별 실행
  chain은 독립적으로 배차하고 중앙 parent가 집계한다.

중앙 Issue 생성과 Project 실행 항목 생성은 하나의 외부 transaction으로 묶을 수 없다.
각 항목은 `request_id + project_id + step` 기반의 duplicate-prevention key를 사용한다.
중간 실패 후 재실행하면 빠진 항목만 만들고 이미 생성한 Issue, 카드, PR을 중복 생성하지
않는다.

## Hermes 메인 에이전트와 Task 연결

메인 에이전트에는 Hermes `PluginContext.register_tool()`로 `toolset="forge"`인 구조화된
Task 기능만 등록한다. 메인 profile과 각 enable surface의 `platform_toolsets`에서만 `forge`
toolset을 명시적으로 enable하고 모델이 DB나 프로세스를 직접 조작하지 못하게 한다.
Desktop, TUI, CLI, Slack마다 Tool 목록 smoke를 실행해 실제 노출을 확인한다.

| 기능 | 동작 |
|---|---|
| `List Tasks` | 현재 사용자와 session에서 접근 가능한 진행 중 Task 목록 조회 |
| `Task Status` | parent와 Project별 현재 단계, PR, 기다리는 이유 조회 |
| `Send to Task` | 사용자 메시지를 durable Task inbox에 저장하고 revision 확인 시작 |
| `Stop Task` | 시스템 중단 서비스 호출. 명확한 중단 문장은 이 기능보다 앞에서 직접 처리 |

도구가 받는 Task 번호와 text는 모델이 제안할 수 있지만 사용자와 session 신원은 hook의
인증된 context에서만 가져온다. Hermes tool handler가 제공하는 session context와
`task_session_bindings`를 함께 확인하며 모델이 임의의 `user_id`나 권한을 인자로 만들 수
없다.

Task 생성자를 owner로 기록하고 명시적으로 등록한 operator만 다른 사용자의 Task를
조회·전송·중단할 수 있다. surface별 인증 ID는 `task_access`에 연결하며 같은 표시 이름,
email, LLM 주장만으로 서로 같은 사람이라고 추측하지 않는다. cross-surface 연결은 owner의
명시적 승인 기록이 있어야 한다.

기본 메인 profile만 Task 조회·전송·중단 Tool과 `pre_user_turn` 제어 확인을 등록한다.
`builder`, `reviewer`, `deep_checker`, `fix` profile에는 이 Tool과 시작 선택기를 노출하지
않는다.

현재 구조에서는 일반 대화 메인 에이전트만 Hermes native conversation runtime을 사용하고,
Forge background worker만 Codex App Server 또는 Claude 구독 runtime을 사용할 수 있다.
Codex App Server는 Hermes plugin Tool을 그대로 노출하지 않으므로 메인 대화를 그 runtime으로
전환하는 배포는 금지한다. 나중에 전환하려면 같은 Forge Tool을 Codex tool bridge에 먼저
연결하고 smoke test를 통과해야 한다. 명확한 Stop 명령은 어느 모델 runtime과도 무관하게
계속 `pre_user_turn`에서 처리한다.

### Task 선택 규칙

- 문장에 `#21`처럼 parent Issue가 있으면 해당 Task를 사용한다.
- 명시 번호가 없으면 현재 대화에 durable하게 연결된 messageable Task를 사용한다.
- 현재 대화 연결도 없고 접근 가능한 messageable Task가 정확히 하나면 그 Task를 사용한다.
- messageable Task가 여러 개면 변경 없이 Task 선택 화면을 표시한다.
- 현재 session에 없던 Task는 명시 번호와 접근 권한을 모두 확인해야 한다.
- 종료된 Task에 새 메시지를 보내지 않는다.

`active`와 `changing`만 messageable이다. `active`에 처음 보내면 새 revision 장벽을 만들고,
`changing`에 추가로 보내면 같은 revision에 append한 뒤 기존 미리보기를 무효화한다.
`stopping`과 terminal Task는 전송을 거부한다.

### durable Task inbox

메인 에이전트가 전달한 메시지는 메모리나 대화 기록만 믿지 않고 owner host의 Task
SQLite에 append한다.

```text
TaskMessage
  format_version
  message_id
  request_id
  parent_issue_number
  user_id
  session_id
  source_event_id
  text
  created_at
  message_hash
```

메시지는 수정하지 않는다. `message_id`는 request ID, 인증된 session ID, source event ID,
text hash로 계산한다. 같은 source event의 재시도는 한 번만 저장한다. 작업자는 다음 안전 지점에서
아직 확인하지 않은 메시지를 순서대로 읽고 `message_id`별 `included`, `applied`, `rejected`
확인 기록을 append한다.

`source_event_id`는 모델 입력이나 conversation 실행 때 만든 난수가 아니다. Desktop, TUI,
CLI는 입력을 Hermes에 제출하기 전에 ID를 durable local outbox에 저장하고 응답 유실·재연결
뒤 같은 입력을 재전송할 때 같은 ID를 사용한다. Slack과 다른 gateway는 인증된 platform
event ID를 사용한다.

carried change가 이 ID를 `pre_user_turn`과 Tool dispatch의 같은 request context에 전달한다.
plugin의 `tool_request` middleware는 모델 인자에 같은 이름이 있더라도 버리고 신뢰된 ID로
덮어쓴다. Tool schema에는 `source_event_id`, `session_id`, `user_id`를 노출하지 않는다.
외부 user turn에 신뢰된 event ID가 없으면 mutating Task Tool은 임의 UUID로 진행하지 않고
재시도 가능한 오류로 중단한다.

메인 에이전트는 DB 저장 영수증을 받은 뒤에만 사용자에게 `Task update saved`라고 답한다.
아직 설정 재확인이 끝나지 않았다면 `Sent to worker`라고 말하지 않는다.
전체 대화 원문을 저장하지 않고 사용자가 Task에 보내기로 한 내용만 저장한다. GitHub
Issue와 Activity Log에는 원문을 복제하지 않고 message ID, 보낸 시각, 처리 상태만
표시한다. 메시지는 `user message` 경계 안에 넣으며 system prompt나 개발자 지시로
승격하지 않는다.

안전 지점은 다음과 같다.

1. Build 또는 Fix 시작 전
2. Review 또는 Deep Check 시작 전
3. 한 작업자 결과를 받아들이기 전
4. 다음 카드를 만들기 전
5. PR merge 판단 전

Forge의 runtime-neutral worker prompt builder가 native Hermes, Codex App Server, Claude 중
어느 runtime을 시작하든 그 직전에 현재 `request_id`와 현재 `task_settings_hash`에 확인된
미확인 메시지 packet을 만든다. packet은 별도 untrusted user-message block이며 hash와
message ID 목록을 run record에 연결한다. worker runtime이 prompt를 받아 시작된 뒤
`included` event를 기록하지만 이를 모델이 적용했다는 증거로 사용하지 않는다.

메시지는 worker 결과가 `applied` 또는 `rejected`를 명시할 때까지 모든 재시도 prompt에
계속 포함한다. 이미 실행 중인 모델 호출에 메시지를 강제로 끼워 넣지 않는다. 실행 중 새
revision이 Confirm되면 현재 결과 수락을 차단하고 새 settings와 message packet으로 worker를
다시 시작한다. 정확성은 Hermes `llm_request` middleware에 의존하지 않으므로 해당
middleware를 우회하는 Codex App Server에서도 같은 동작을 유지한다.

### Forge dispatcher와 runtime adapter

Hermes 기본 Kanban daemon의 고정 spawn은 message packet과 Forge runtime binding을 알지
못한다. Forge host에서는 외부 기본 daemon을 disable하고 Gateway 설정
`kanban.dispatch_in_gateway=false`와 환경변수 `HERMES_KANBAN_DISPATCH_IN_GATEWAY=0`을 함께
고정한다. 정확히 하나의 `forge-dispatcher`가 process lifetime 전체에 걸친 OS singleton
lock을 보유한 채 `dispatch_once(spawn_fn=route_spawn)`으로 모든 claim을 소유한다. 짧은
board tick lock만 단독성의 근거로 사용하지 않는다. 배포와 system check는 다른 dispatcher
process와 Gateway 내부 dispatch가 모두 꺼졌는지 확인한다.

`route_spawn`은 다음 세 분기로 fail-closed하게 처리한다.

- Forge 표식과 registry binding이 모두 정확함 → `forge_spawn`
- Forge 표식과 registry binding이 모두 없음 → Hermes 기존 default spawn
- Forge 표식이 일부 있거나 binding이 불일치함 → 카드를 block하고 spawn 0회

따라서 일반 Hermes Kanban 사용을 없애지 않으면서 손상된 Forge 카드가 일반 worker로
실행되는 것을 막는다. Forge dispatcher가 실패하거나 singleton lock을 잃으면 다른 daemon이
대신 claim하지 않고 모든 새 배차를 멈춘다.

`forge_spawn`은 다음을 하나의 run record에 묶는다.

- exact Task settings와 stop/revision barrier 재확인
- confirmed message packet과 packet hash 생성
- Project 전용 worktree와 worker prompt 생성
- 선택한 native Hermes, Codex App Server, Claude `WorkerRuntimeAdapter` 시작
- host, PID start identity, process group/cgroup 또는 Windows Job 기록
- runtime 결과와 message `applied|rejected` 결과 수집

세 runtime adapter는 start, stop, wait, result, process identity의 같은 interface를
구현한다. adapter가 설치·인증·stop read-back 검증을 통과하지 않은 runtime은 선택하거나
fallback할 수 없다. standalone Claude adapter가 구현되기 전에는 Claude 지원을 표시하지
않는다.

Task inbox 메시지를 LLM이 단순 설명인지 범위 변경인지 판단하게 하지 않는다. `Send to
Task`는 메시지 append와 `revision_requested` 장벽을 하나의 DB transaction으로 기록한다.
이 장벽은 새 배차, 현재 worker 결과 수락, 새 GitHub write, 자동 merge를 모두 막으며,
메시지는 아직 worker prompt builder에 노출되지 않는다.

시스템은 메시지를 포함한 새 Task 내용을 만들고 새 `request_id`로 `mode=task`, Projects,
`task_flow`, `merge_mode`, 필요한 `merge_order`와 전체 미리보기를 다시 확인한다. Confirm이
성공하면 메시지를 새 `task_settings_hash`에 연결해 worker에게 공개하고 이전 설정은
`replaced`로 끝낸다. Cancel하면 메시지는 `rejected`로 끝내고 기존 Task를 재개할지 별도
선택하게 한다. 따라서 확인되지 않은 문장이 기존 자동 merge 권한으로 코드에 반영될 수
없다. 상태 질문은 inbox에 저장하지 않고 `Task Status` 읽기만 수행한다.

메시지 하나는 UTF-8 64 KiB, 한 revision의 메시지는 100개와 합계 1 MiB를 넘을 수 없다.
초과 입력은 잘라 저장하지 않고 거부한다. terminal Task의 원문은 기본 90일 뒤 삭제하되
message hash와 event는 감사 기록으로 유지한다. DB 파일은 소유자만 읽고 쓸 수 있게
Windows ACL 또는 Linux mode `0600`을 적용하고 검증된 SQLite backup에 포함한다.

## 확실한 Task 중단

### 사용자 명령

다음처럼 전체 문장이 명확한 중단 명령일 때 시스템 제어 확인이 LLM보다 먼저 처리한다.

```text
forge stop
forge stop #21
#21 실행 중단
#21 작업 중단해
현재 Task 멈춰
```

번호가 없고 현재 session의 중단 가능한 Task가 하나면 즉시 그 Task를 중단한다. 여러 개면
`Stop Task` 대상 선택을 표시하며 선택 전에는 아무 상태도 바꾸지 않는다. 대상이 없으면
`실행 중인 Task가 없습니다`를 반환한다.

중단 대상 조회는 worker용 `get_active`를 사용하지 않고 별도 `get_stoppable`을 사용한다.
같은 parent Task의 request 또는 settings가 `prepared`, `bound`, `active`, `changing`,
`stopping` 중 하나면 중단 대상으로 찾는다. `stopping`은 기존 stop request의 현재 결과를
반환한다. revision 확인 중에는 이전 settings, pending revision request, pending message를
한 parent aggregate로 선택한다.

Stop과 revision Confirm 또는 Resume가 경쟁하면 같은 Task DB transaction lock으로
직렬화한다. Stop 장벽이 먼저면 pending revision과 새 request를 취소하고 이전 settings를
`stopping`으로 만든 뒤 Confirm과 Resume를 거부한다. revision Confirm이 먼저면 새 settings를
active로 만들고 이전 settings를 replaced로 끝낸 뒤 Stop이 새 settings를 중단한다. 어느
순서에서도 Task 없음이나 worker 재개로 빠지지 않는다.

단순 substring이나 자유로운 LLM 분류로 시스템 중단을 실행하지 않는다. 일반 대화에서
메인 에이전트가 중단 의도를 파악한 경우에도 같은 구조화된 `Stop Task` 기능을 호출한다.

### 중단 서비스의 수렴 순서

중단은 `reclaim`과 `block` 명령을 연속 입력하는 방식으로 구현하지 않는다. 두 동작
사이에 dispatcher가 다시 작업을 가져갈 수 있기 때문이다. 하나의 멱등적인
`TaskStopService`가 다음 상태로 수렴시킨다.

1. `stop_request_id`를 durable하게 기록한다.
2. 같은 transaction에서 Task에 `stop_requested` 장벽을 세워 새 배차, 새 카드, 새
   GitHub write, 새 merge를 차단한다. 이 시점에는 아직 중단 완료라고 답하지 않는다.
   `get_active`와 모든 `guard_active`는 이 장벽을 확인하고 해당 Task를 active로 반환하거나
   외부 write 권한을 주지 않는다.
3. Project별 matching 카드 중 기존 `done`, `archived`는 보존한다. 그 밖의 `triage`,
   `todo`, `scheduled`, `ready`, `running`, `blocked`, `review`는 하나의 Kanban DB
   transaction에서 stop reason이 있는 `archived`로 바꾸고 새 claim을 금지한다.
4. 기록된 실행 PID와 run을 정상 종료 신호로 중단하고 제한 시간 뒤 남아 있으면 강제
   종료한다.
5. Project별 후속 카드 생성, prepared activation, outbox replay, branch refresh 예약,
   상태 동기화, merge worker를 포함해 settings DB를 직접 쓰는 모든 경로가
   `stop_requested` 장벽을 다시 확인하고 일반 작업을 만들지 않게 한다.
6. 이미 시작됐을 수 있는 GitHub Issue, PR, merge 상태를 remote에서 다시 읽는다.
7. merge가 먼저 완료됐다면 Task를 `merged`로 기록하고 `중단 전에 완료됨`이라고
   보고한다. merge되지 않았다면 lifecycle에 `cancelled`를 append한다. 일부 Project만
   merge됐다면 Task를 `partially_merged`로 기록하고 남은 자동 작업을 중단한다. 정리
   동작 자체가 실패한 경우에만 stop request를 `cleanup_incomplete`로 둔다.
8. 중앙 parent Issue의 활성 Forge 상태를 제거하고 실제 결과와 중단된 Project 목록을
   기록한다. 취소로 끝난 경우에만 `not planned`로 닫는다. 이 write는 일반 worker
   `guard_active`가 아니라 해당 `stop_request_id`만 허용하는 `guard_stop_cleanup`을
   통과해야 한다. 이 권한은 label 제거, stop 결과 comment, Issue close만 허용하며 PR
   write나 merge는 허용하지 않는다.
9. 모든 read-back과 정리가 끝난 경우에만 중단 요청을 완료 상태로 기록한다.

모든 중단 결과는 관련 process group 또는 Windows Job의 descendant 0개, 관련 카드 전부
`done|archived`, 후속 카드 0개를 공통 완료조건으로 사용한다. 그 뒤 실제 remote 결과에
따라 다음처럼 끝낸다.

- merge 0개: 활성 Forge label 0개, parent Issue `closed/not planned`, Task `cancelled`
- 모든 Project merge: 실제 merge commit 전체 확인, Task `merged`, 결과 `completed_before_stop`
- 일부 Project merge: merge된 commit과 남은 PR 전체 확인, parent Issue open +
  `forge:needs-decision`, Task `partially_merged`, 결과 `completed_with_partial_merge`

다른 제품이나 사용자가 붙인 GitHub label은 제거하지 않는다. 해당 분기의 완료조건 중
하나라도 확인하지 못하면 장벽을 유지한 채 `cleanup_incomplete`로 남긴다.

중간에 프로세스가 죽으면 reconciliation worker가 완료되지 않은 중단 요청을 다시 읽어
같은 최종 상태로 만든다. 같은 명령을 반복해도 추가 lifecycle event, 중복 댓글, 중복
프로세스 종료를 만들지 않고 이미 중단됐다는 결과를 반환한다.

작업자는 시작 직후, 모델 호출 직전, 모델 결과 저장 직전, PR write 직전, merge 직전에
Task가 여전히 `active`인지 확인한다. 취소 뒤 늦게 도착한 작업자 결과는 성공으로
받아들이지 않는다.

worker, run, 카드, PID는 `request_id`, `task_settings_hash`, `project_id`, `host_id`,
process start identity, Linux process group/cgroup 또는 Windows Job identity에 연결해
추적한다. 종료 직전에 PID의 start identity를 다시 확인하고 일치하는 group/job 전체를
종료한 뒤 descendant 0개를 read-back한다. 프로세스 이름 검색이나 Hermes 전역 stop으로
다른 Task 또는 일반 background process를 종료하지 않는다. 중단은 branch, commit,
worktree, PR을 자동 삭제하지 않고 사용자가 복구할 위치를 기록한다.

현재 Hermes의 `reclaim`, `block`, `archive`를 조합하지 않고 하나의 새 Kanban 중단
기능을 추가한다. 이 기능은 matching 카드를 먼저 하나의 DB transaction에서 terminal로
바꾸고 각 카드의 PID와 run을 캡처한 뒤 해당 프로세스만 종료하고 read-back한다.

Confirm 복구 중 아직 `prepared`이거나 Issue 번호가 만들어지지 않은 요청도 중단할 수
있어야 한다. 이 경우 outbox 재생을 먼저 막고 외부 항목이 실제로 생성됐는지 read-back한
뒤 준비 기록을 취소한다. owner host와 다른 host에서 받은 중단 요청은 실행하지 않으며
owner host를 표시한다.

## Task 설정 형식과 이전 Task

새 Task는 `forge-task-settings/v2`로 저장한다. v2는 `repository`를 재해석하지 않고
`management_repository`와 정렬된 `projects`를 명시한다. `task_settings_hash`는 Project
binding 전체를 포함한다.

### exact v2 record

Confirm 직후에는 외부 Issue 연결과 분리된 다음 exact `forge-task-request/v2`를 먼저
저장한다.

```json
{
  "format_version": "forge-task-request/v2",
  "request_id": "UUID",
  "management_repository": "OWNER/REPO",
  "mode": "task",
  "task_content": {
    "title": "Task 제목",
    "description": "확인한 Task 설명",
    "acceptance_criteria": ["AC-01"]
  },
  "task_content_hash": "64자리 SHA-256",
  "task_flow": "build_review",
  "merge_mode": "full_auto",
  "merge_order": ["64자리 project_id"],
  "projects": [
    {
      "project_id": "64자리 SHA-256",
      "repository": "OWNER/REPO",
      "workspace": "/정규화된/절대/Git-root",
      "remote_name": "origin",
      "base_branch": "main",
      "base_commit": "40자리 Git commit",
      "host_id": "UUID"
    }
  ],
  "task_owner_host": "UUID",
  "confirmed_by": "인증된 subject ID",
  "confirmed_at": "RFC 3339 UTC",
  "auto_merge_expires_at": "RFC 3339 UTC 또는 null",
  "replaces_request_id": "UUID 또는 null",
  "request_hash": "64자리 SHA-256",
  "status": "prepared"
}
```

request field는 위 exact 목록과 type만 허용한다. `request_hash`는 자신과 `status`를 제외한
나머지 field의 canonical JSON을 SHA-256으로 계산한다. Issue 번호는 immutable request에
나중에 써넣지 않고 append-only request event에 저장한다.

```text
prepared
  ├─ parent_issue_bound(parent_issue_number) → bound
  │    ├─ settings_activated(task_settings_hash) → activated
  │    └─ stop/cancel → cancelled
  └─ stop/cancel → cancelled
```

새 parent Task는 멱등적으로 생성한 새 parent Issue 번호를 bind한다. revision request는
`replaces_request_id`로 현재 settings의 request를 가리키고 같은 parent Issue 번호를 새
`parent_issue_bound` event로 bind한다. request가 `bound`인 상태에서 crash가 발생해도
재생 시 event의 번호를 사용하며 새 Issue를 만들지 않는다. `bound → activated`와 새
settings `active` 생성은 같은 SQLite transaction에서 수행한다.

Issue가 만들어지고 번호가 확인된 뒤에만 다음 exact settings record를 만든다.

```json
{
  "format_version": "forge-task-settings/v2",
  "request_id": "UUID",
  "request_hash": "64자리 SHA-256",
  "management_repository": "OWNER/REPO",
  "parent_issue_number": 21,
  "mode": "task",
  "task_content_hash": "64자리 SHA-256",
  "task_flow": "build_review",
  "merge_mode": "full_auto",
  "merge_order": ["64자리 project_id"],
  "projects": [
    {
      "project_id": "64자리 SHA-256",
      "repository": "OWNER/REPO",
      "workspace": "/정규화된/절대/Git-root",
      "remote_name": "origin",
      "base_branch": "main",
      "base_commit": "40자리 Git commit",
      "host_id": "UUID"
    }
  ],
  "task_owner_host": "UUID",
  "confirmed_by": "인증된 subject ID",
  "confirmed_at": "RFC 3339 UTC",
  "auto_merge_expires_at": "RFC 3339 UTC 또는 null",
  "task_settings_hash": "64자리 SHA-256",
  "status": "active"
}
```

모든 settings field는 위 exact 목록과 type을 사용한다. `projects`는 canonical tuple로 정렬한다.
`task_settings_hash`는 자신과 `status`를 제외한 나머지 settings field를 key-sorted compact
UTF-8 JSON으로 만든 SHA-256이다. `request_hash`도 같은 방식으로 request 자신과 status,
`request_hash`를 제외해 계산한다. `task_content_hash`는 기존 공식 TaskContent hash 규칙을
그대로 사용한다. hash 입력의 시간은 UTC `Z`, repository는 canonical `OWNER/REPO`, 경로는
host별 canonical absolute path로 정규화한다.

### exact lifecycle

v2 settings의 current status는 append-only event에서 계산한다.

```text
active
  ├─ revision_requested → changing
  │    ├─ 새 settings active → replaced
  │    └─ 사용자가 update 취소 후 Resume 확인 → active
  ├─ stop_requested → stopping
  │    ├─ 실제 unmerged + cleanup 완료 → cancelled
  │    ├─ 실제 전체 merged → merged
  │    └─ 일부 Project merged → partially_merged
  ├─ 자동 merge 시간 만료 → expired
  └─ 정상 전체 merge 완료 → merged

prepared request
  ├─ Issue bind 성공 → bound → activated + settings active
  └─ stop/cancel → cancelled
```

`cleanup_incomplete`는 Task lifecycle status가 아니라 `task_stop_requests`의 재시도 상태다.
해당 settings는 `stopping`에 머물며 active 조회에서 제외된다. `changing`, `stopping`,
`cancelled`, `expired`, `merged`, `replaced`, `partially_merged`도 worker의 active 조회에서
제외한다. `get_active`와 `guard_active`는 status `active`, exact settings hash,
revision barrier 없음, stop barrier 없음을 모두 만족할 때만 성공한다.

`replaced`, `cancelled`, `expired`, `merged`, `partially_merged`는 terminal event이며 같은
settings에서 둘 이상을 append하지 못한다. stop과 merge가 경쟁하면 먼저 확보한 settings
guard가 끝난 뒤 remote read-back 결과로 단 하나의 terminal event만 기록한다.

기존 v1 Task는 기존 hash와 의미를 바꾸지 않고 단일 Project Task로 읽는다. v1을 다시
저장하거나 새 의미로 재계산하지 않는다. 새 Task만 v2로 생성한다. 알 수 없는 version,
필드 누락, 추가 필드, 잘못된 Project binding은 계속 명확한 data-format 오류로 중단한다.

마이그레이션은 하나의 SQLite transaction으로 수행하고 재실행 가능해야 한다. 최소 새
저장 구조는 다음과 같다.

- `task_projects`: Project binding과 Project별 현재 상태
- `task_messages`: 메인 에이전트가 전달한 immutable 메시지
- `task_message_events`: worker task ID와 run ID에 연결된 included·applied·rejected 결과
- `task_revision_requests`: settings hash별 revision 장벽과 재확인 결과
- `task_stop_requests`: 중단 요청의 durable 진행 상태
- `task_session_bindings`: session과 parent Task 연결
- `task_access`: owner와 명시적으로 승인된 operator subject
- `surface_events`: 입력 제출 전 저장한 source event ID와 응답·재전송 상태

절대 workspace 경로는 host에 종속되므로 `host_id`가 달라지거나 경로가 사라지면 자동으로
다른 경로를 추측하지 않는다. 해당 Project를 `waiting for help`로 두고 사용자가 새 Task
설정을 확인하게 한다.

## 여러 Project의 merge

여러 GitHub 저장소의 merge는 하나의 원자적 transaction이 아니다.

- `manual`: 모든 Project PR을 사람이 merge한다.
- `safe_auto`: 단일 Project이며 safe-file 규칙을 통과한 경우에만 자동 merge한다. 여러
  Project Task는 저장소 간 영향도를 safe-file 검사만으로 판단할 수 없으므로 사람을
  기다린다.
- `full_auto`: 모든 Project가 선택한 `task_flow`, 현재 commit CI, repository와 branch
  확인을 통과하고 사용자가 Confirm한 dependency order가 있을 때만 전체 준비 barrier를
  연다. 그 전에는 어떤 PR도 merge하지 않는다.

`full_auto`의 실제 merge는 expected commit을 지정해 Confirm한 dependency order로
Project별 순서대로 수행한다. 순서가 확인되지 않은 multi-Project Task는 사람을 기다린다.
중간 실패 시 남은 merge를 즉시 중단하고 parent를 `forge:needs-decision` 상태로 둔다. 이미
merge된 PR을 자동으로 되돌리면 또 다른 코드 변경이 생기므로 자동 rollback하지 않는다.
settings는 더 이상 active로 남기지 않고 `partially_merged` terminal event를 기록한다.
운영자에게 merge된 Project, 실패한 Project, 남은 Project와 복구 선택지를 표시한다.

## 오류와 복구

| 실패 시점 | 즉시 동작 | 재시도 결과 |
|---|---|---|
| Project 발견 실패 | 외부 write 없이 선택 화면 유지 | 경로 수정 뒤 다시 발견 |
| Confirm 후 parent Issue 생성 실패 | prepared 상태 유지 | 같은 request로 재시도 |
| Project 실행 항목 일부 생성 실패 | 생성된 항목 보존, 실행 시작 보류 | 빠진 항목만 생성 |
| Task 메시지 저장 뒤 응답 유실 | 같은 message ID 재전송 | 메시지 중복 없음 |
| 중단 도중 host 종료 | stop_requested 장벽 유지 | 나머지 카드·프로세스·Issue 정리 |
| worker가 취소 뒤 결과 반환 | 결과 거부, 후속 카드 없음 | 중단 상태 유지 |
| Project remote 또는 path 변경 | 해당 Project 정지 | 새 설정 확인 전 재개 없음 |
| 여러 Project merge 중간 실패 | 남은 merge 정지 | 사람 결정 필요 |

## 구현 경계

기존 책임을 크게 섞지 않고 다음 모듈 경계를 사용한다.

- `task_setup`: 선택 state machine과 control command routing
- `project_discovery`: cwd에서 범용 Project 후보 발견
- `task_projects`: Project binding 검증과 저장
- `task_messages`: durable inbox와 확인 기록
- `task_stop`: 멱등 중단 orchestration
- `task_service`: 중앙 parent 생성과 Project 실행 항목 준비
- `task_runtime`: Project별 Build, Review, Deep Check, Fix 진행
- `merge_runtime`: Project별 증거 확인과 전체 barrier
- Hermes plugin: 인증된 session context, 동적 선택, 메인 profile의 `forge` Task Tool과
  trusted turn ID 연결
- worker prompt builder: native Hermes, Codex App Server, Claude에 같은 confirmed message
  packet 전달
- forge-dispatcher: 유일한 Kanban claim owner, Forge와 일반 카드의 spawn 경로 분기
- worker runtime adapters: native Hermes, Codex App Server, Claude의 start·stop·wait·result
  계약 통일
- Hermes carried change: 실제 `working_directory` 전달과 handled turn의 모델 호출 차단

하나의 함수나 클래스가 Project 탐색, GitHub write, Kanban write, 프로세스 종료를 함께
담지 않는다. 외부 시스템마다 adapter를 두고 orchestration은 명시적인 결과를 조합한다.

## 검증 기준

### 대화와 선택

1. `chat`을 선택하면 첫 입력과 이후 일반 대화를 Hermes 메인 에이전트가 받는다.
2. `chat`은 GitHub, Kanban, Task DB에 작업 항목을 만들지 않는다.
3. `task`는 `Projects → task_flow → merge_mode → Task content → Confirm`을 모두 거치며,
   여러 Project + `full_auto`는 `merge_order`도 거친다.
4. 모든 chooser 입력과 Confirm은 `handled`이며 모델 호출이 0회다.
5. Confirm 뒤 일반 문장은 다시 Hermes 메인 에이전트가 받는다.
6. 기존 `mode`, `task_flow`, `merge_mode` 값과 화면 문구가 바뀌지 않는다.

### Project 실행

7. Git 저장소 내부 cwd에서는 해당 Git root를 발견한다.
8. 비-Git 상위 폴더에서는 임의 이름의 여러 Git 저장소를 발견한다.
9. 특정 Cognet9 저장소 이름 없이 새 저장소가 후보에 나타난다.
10. 복수 Project를 선택할 수 있고 0개 선택은 거부한다.
11. root 탈출, symlink 탈출, missing remote, remote 불일치를 거부한다.
12. 중앙 parent는 항상 Infinity Forge에 생성된다.
13. 각 worker는 선택한 Project 전용 worktree에서만 변경한다.
14. 각 PR은 해당 Project 저장소에 생성되고 다른 Project PR은 거부한다.
15. 부분 dispatch 재시도에도 Issue, 카드, worktree, PR이 중복되지 않는다.

### 메인 에이전트 Task 연결

16. messageable Task가 하나면 메인 에이전트가 번호 없이 상태를 조회하고 메시지를 전달한다.
17. 여러 Task가 있으면 명시 번호 또는 선택 없이는 메시지를 보내지 않는다.
18. surface가 입력 제출 전에 durable source event ID를 저장하며, 프로세스 재시작·응답
    유실·재전송에도 Task 메시지는 한 번만 저장된다.
19. 작업자는 메시지를 순서대로 읽고 적용 결과를 message ID에 연결한다.
20. 확정 설정을 바꾸는 메시지는 자동 merge를 계속하지 않고 재확인을 요구한다.
21. 모델이 임의의 user ID나 session ID를 만들어 다른 Task에 접근할 수 없다.
22. Task 생성자 또는 허가된 운영자만 메시지 전송과 중단을 수행한다.
23. 메시지는 user message로 전달되며 system prompt로 승격되지 않는다.
24. 전체 대화가 아니라 명시적으로 Task에 보낸 내용만 로컬 DB에 저장된다.
25. 새 revision은 현재 run 결과를 차단하고 다음 worker run의 runtime-neutral prompt
    packet에 포함되며, 미확인 메시지가 있으면 결과 수락·후속 카드·merge가 차단된다.
26. 메인 profile에는 `forge` Task Tool이 있고 worker profile에는 시작 선택기와 중단
    Tool이 노출되지 않는다. enable된 native Hermes, Codex App Server, Claude adapter가
    같은 packet hash와 message ID를 받는다.
27. 외부 Hermes daemon과 Gateway 내부 dispatch가 모두 꺼져 있고 singleton lock을 가진
    하나의 forge-dispatcher만 claim한다. valid Forge 카드는 forge_spawn, Forge 표식이 전혀
    없는 일반 카드는 default spawn, binding이 손상된 Forge 카드는 block/no spawn으로
    정확히 한 번 분기된다.

### 중단

28. `forge stop #21`과 `#21 실행 중단`은 LLM 호출 없이 처리된다.
29. 질문·부정·인용·코드 블록의 중단 문장은 중단하지 않고 메인 에이전트가 답변한다.
30. 단일 stoppable Task의 `forge stop`은 prepared·bound·active·changing 상태에서도 해당
    parent Task를 중단한다.
31. 여러 stoppable Task에서는 대상 선택 전 상태 변경이 없다.
32. revision Confirm·Resume와 Stop이 경쟁해도 Stop 장벽 이후에는 worker가 재개되지 않는다.
33. stop barrier, Kanban 중단, worker 종료, 후속 배차 금지, remote read-back, Issue 정리가 한 요청으로
    수렴한다.
34. 중단 도중 crash 후 재시도해도 최종 상태가 같고 중복 event가 없다.
35. 취소 뒤 늦게 도착한 worker 결과와 merge 시도는 거부된다.
36. merge가 중단 요청보다 먼저 완료되면 `cancelled`가 아니라 실제 `merged`를 보고한다.
37. 일부 Project만 merge됐으면 `partially_merged`와 `completed_with_partial_merge`를
    보고하고 parent를 open `needs decision`으로 유지한다.
38. 정리 실패는 완료가 아니라 `cleanup_incomplete`로 표시하고 재시도한다.
39. 다른 Task와 일반 Hermes background process는 종료하지 않는다.
40. dispatcher와 상태 동기화를 재시작해도 중단된 Task가 다시 배차되거나 활성 label을
    되찾지 않는다.
41. Confirm 복구 중인 prepared Task와 아직 Issue 번호가 없는 요청도 outbox 재생 없이
    중단된다.
42. owner host가 아닌 surface의 Send와 Stop은 owner host를 표시하고 외부 write 0회로
    거부한다.

중단 경쟁 테스트는 카드 생성, label write, branch refresh, merge 각각에 대해 중단이 먼저인
순서와 외부 write가 먼저인 순서를 모두 실행한다. 동시 중단 N회, dispatcher tick 동시
실행, TERM을 무시하는 worker, barrier·카드 중단·GitHub 정리 각 단계의 crash도 검증한다.
ready, blocked, done, archived, PID 없음, 이미 종료된 PID는 모두 같은 최종 상태로
수렴해야 한다.

### 데이터·merge·배포

43. v1 Task는 `repository`의 기존 의미와 기존 hash로 단일 Project 실행을 계속 읽는다.
44. v2는 `management_repository`와 `projects`를 분리하고 hash에 정렬된 모든 Project
    binding을 포함한다.
45. 한 Project merge가 parent 전체를 완료 처리하지 않는다.
46. 모든 Project가 준비되기 전 `full_auto` merge가 시작되지 않는다.
47. multi-Project `safe_auto`는 사람 merge를 기다린다.
48. multi-Project `full_auto`는 Confirm한 dependency order가 없으면 시작하지 않는다.
49. multi-Project merge 중간 실패는 settings를 active로 남기지 않고 `partially_merged`로
    끝내며 남은 merge를 차단한다.
50. Windows, EC2, VPS에서 plugin과 worker가 같은 release commit이며 같은 선택 state
    machine을 사용하는지 확인한다.
51. Desktop, TUI, CLI, Slack의 실제 model Tool 목록에 main profile의 네 Forge Task Tool이
    있고 worker profile에는 없는지 확인한다.
52. EC2와 VPS smoke Task에서 Management 저장소와 실제 PR 저장소가 서로 다름을 확인한다.

## 단계별 적용과 장기 결과

1. 전환 전에 활성 v1 Task를 완료 또는 취소하고 SQLite backup과 복원 검증을 만든다.
2. session context, Project 발견, v2 저장 형식을 추가하되 새 배차는 끈 상태에서
   migration과 readback을 확인한다. 구형·신형 worker가 동시에 write하지 못하게 한다.
3. Project별 작업 흐름과 메인 에이전트 Task inbox를 켜고 `manual` smoke Task로 검증한다.
4. `Stop Task`의 crash 복구와 재배차 금지를 검증한 뒤 자연어 명령을 공개한다.
5. 단일 Project `safe_auto`를 검증한다.
6. 마지막에 multi-Project `full_auto` barrier를 켠다.

6개월 뒤에도 Management와 Project의 의미가 분리되어 새 저장소를 추가할 때 배포 코드를
고칠 필요가 없다. 메인 에이전트 모델을 교체해도 Confirm과 Stop의 안전성은 바뀌지 않는다.
Task 메시지와 중단 요청이 durable하게 남으므로 응답 중단, 프로세스 crash, host 재시작 뒤
무엇이 전달됐고 무엇이 중단됐는지 복구할 수 있다.

## 변경이력

- 2026-07-18 | 혼합형 구조 사용자 확정 | 변경: Hermes 메인 대화, deterministic control,
  durable Task inbox, 범용 Project 선택, 중앙 Management/실제 PR 분리, 멱등 Stop Task를
  하나의 설계로 고정 | 검증: 현재 Task setup, Task settings, worker, merge, Linux CLI
  chooser 경로와 EC2의 `#21 실행 중단` 기록을 대조해 작성
