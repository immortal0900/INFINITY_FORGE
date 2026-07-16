# 자동화가 "무엇으로" 돌아가는가 — 세션인가 설정인가

> 질문: Phase 1 e2e·Phase 2 무인 운영·24/7 즉시알림·이중 보드 공유가 지금 어떻게 가능한가? AI 세션을 계속 띄워둔 건가, 코드·설정으로 박아둔 건가?
> 답: **거의 전부 OS 설정(systemd 타이머·시작프로그램)으로 박아뒀고, AI 세션은 "작업 1건이 실행될 때만 잠깐" 뜬다.** 대화창을 켜둘 필요가 없다.

## 1. 큰 구분: 항상 도는 것 vs 그때만 뜨는 것

| 구분 | 정체 | 켜져 있는 방식 | AI인가 |
|---|---|---|---|
| **상주 프로세스** | hermes 게이트웨이, 대시보드 | OS가 부팅 때 자동 실행(항상 떠 있음) | 아니오 — 메시지를 기다리는 대기 프로그램 |
| **주기 실행** | 상태 동기화·백업·시스템 점검 등 | OS 타이머가 정해진 시각에 깨움 | 아니오 — 순수 스크립트(LLM 0) |
| **일회성 AI 세션** | Build·Review·Deep Check·Fix 작업 | 카드가 배정될 때 Hermes가 시작하고, 끝나면 종료 | 예 — 작업마다 새 세션 사용 |

핵심: **"AI가 계속 지켜보는" 부분은 없다.** 감시·알림·백업은 전부 시각에 맞춰 깨어나는 스크립트고, AI(gpt-5.5·codex)는 실제 코드 작업 1건을 처리하는 그 몇 분만 존재한다.

## 2. 저장소에서 무엇이 무엇으로 구현됐나

### VPS 쪽 — systemd 사용자 타이머 (`~/.config/systemd/user/`)

아래 표는 `deploy-vps.sh`가 설치하는 구성을 보여 준다. 저장소에 구현됐다는 뜻이며, 특정 서버에 배포됐다는 뜻은 아니다. 실제 반영 여부는 서버에서 `systemctl --user list-timers`와 현재 Git commit을 함께 확인한다.

| 기능 | 구현체 | 트리거 | 종류 |
|---|---|---|---|
| 게이트웨이(Slack 수신·워커 스폰) | `hermes-gateway.service` | 부팅 시 자동(linger로 로그아웃에도 생존) | 상주 |
| 대시보드(Desktop 원격용) | `hermes-dashboard.service` | 부팅 시 자동 | 상주 |
| 이슈 상태를 공식 라벨 1개로 동기화 | `forge-mirror` → `issue-status-sync.py` | 2분마다 | 타이머 |
| 작업 활동 기록 | `forge-ledger` → `activity-log-writer.py` | 10분마다 | 타이머 |
| 보류 메시지 배달(MEMEX) | `forge-flush` → `send-pending-messages.py` | 10분마다 | 타이머 |
| 시스템 자체 점검 | `forge-canary` → `system-check.sh` | 6시간마다(00/06/12/18 KST) | 타이머 |
| 상태 불일치·백업 신선도 확인 | `forge-drift` → `state-mismatch-check.sh` | 매시 | 타이머 |
| 아침 리포트 + 일별 지식 미러 | `forge-morning` → `morning-report.sh` | 매일 07:30 KST | 타이머 |
| DB·MEMEX 백업 | `hermes-backup` → `nightly-backup.sh` | 매일 04:30 KST | 타이머 |

`forge-ledger`, `forge-canary`, `forge-drift`는 기존 설치와의 연결을 유지하기 위한 systemd 유닛 ID다. 화면과 문서에서는 각각 **Activity Log**, **System Check**, **State Mismatch Check**로 표시한다. 이 유닛들은 배포 스크립트(`deploy-vps.sh`)가 생성하고 활성화한다.

### 로컬(노트북) 쪽 — Windows 시작프로그램 (`셸:startup`)

회사 정책으로 작업 스케줄러(schtasks) 등록이 막혀 있어, **시작프로그램 폴더의 .vbs**(로그온 시 창 없이 실행)로 상주시킨다:

| 기능 | 구현체 | 트리거 |
|---|---|---|
| 로컬 게이트웨이(@forgelocal) | `Hermes_Gateway.vbs` | 로그온 시 |
| VPS 백업을 노트북으로 pull | `ForgeBackupPull.vbs` → `pull-backup.ps1` | 로그온 시 1회 |
| 로컬 장부 진행 공유 + 일일 백업 push | `ForgeLocalSync.vbs` → `local-sync.py --loop` | 로그온 후 5분 루프 |

## 3. 그래서 "24/7" 작업 처리가 어떻게 성립하나 — 단계 분해

**24/7 처리**:
1. VPS는 24시간 켜진 임대 서버라 게이트웨이가 항상 떠 있음(1번 표)
2. 사용자가 대화에서 **Task**를 선택하고, **Build / Build + Review / Build + Review + Deep Check** 중 하나와 병합 방식을 매번 고른다.
3. 확인된 Task는 GitHub 이슈, 변경할 수 없는 Task 설정, Hermes 루트 카드를 같은 요청 ID로 연결한다.
4. `task-flow-worker.py`가 현재 GitHub commit과 완료 결과를 다시 읽고 다음 Build·Review·Deep Check·Fix 카드가 없을 때만 한 장 만든다.
5. `issue-status-sync.py`가 같은 증거를 읽어 GitHub 이슈의 Forge 상태 라벨을 정확히 1개로 맞춘다.
→ 최종 확인된 Task가 있으면 사람의 밤낮과 무관하게 다음 단계가 진행된다. "밤 공장"은 습관이지 제약이 아니다.

**자동 병합**:
1. 기본 병합 방식은 **Manual**이고 `merge-worker.py`는 GitHub에 병합 쓰기를 하지 않는다.
2. **Safe Files Auto-Merge** 또는 **All Validated PRs Auto-Merge**를 Task에서 고른 경우에도 `AUTO_MERGE_ENABLED=true`가 명시돼야 쓰기가 열린다.
3. worker는 선택한 검사, 현재 base/head commit, `eval`, 미해결 Review, branch 보호 설정, Task 설정 만료를 다시 확인한다.
4. 하나라도 다르거나 읽지 못하면 그 Task만 오류로 끝내고 병합하지 않는다.

**이중 보드 공유**:
- 로컬은 `local-sync.py`(5분 루프)가 로컬 장부의 전이를 감지 → GitHub 이슈에 코멘트 → 클라우드·사람이 그 이슈에서 로컬 진행을 봄. 역시 스크립트 소관.

## 4. 확인해 보는 법 (본인 환경에서)

- VPS 타이머: `ssh ubuntu@51.222.27.48 "systemctl --user list-timers"`
- VPS 상주: `ssh ubuntu@51.222.27.48 "systemctl --user status hermes-gateway"`
- 로컬 상주: 실행창(Win+R)에 `shell:startup` → .vbs 3개 확인
- 스크립트 원본(SoT): 이 레포의 `forge/scripts/`, `forge/hooks/`

**한마디 요약**: 자동화의 뼈대는 **OS 타이머·시작프로그램 + 증거를 다시 확인하는 스크립트**다. AI는 Build·Review·Deep Check·Fix가 필요할 때만 실행되며, 자동 병합은 기본적으로 꺼져 있다.
