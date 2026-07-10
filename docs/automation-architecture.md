# 자동화가 "무엇으로" 돌아가는가 — 세션인가 설정인가

> 질문: Phase 1 e2e·Phase 2 무인 운영·24/7 즉시알림·이중 보드 공유가 지금 어떻게 가능한가? AI 세션을 계속 띄워둔 건가, 코드·설정으로 박아둔 건가?
> 답: **거의 전부 OS 설정(systemd 타이머·시작프로그램)으로 박아뒀고, AI 세션은 "작업 1건이 실행될 때만 잠깐" 뜬다.** 대화창을 켜둘 필요가 없다.

## 1. 큰 구분: 항상 도는 것 vs 그때만 뜨는 것

| 구분 | 정체 | 켜져 있는 방식 | AI인가 |
|---|---|---|---|
| **상주 프로세스** | hermes 게이트웨이, 대시보드 | OS가 부팅 때 자동 실행(항상 떠 있음) | 아니오 — 메시지를 기다리는 대기 프로그램 |
| **주기 실행** | 미러·백업·카나리아 등 8종 | OS 타이머가 정해진 시각에 깨움 | 아니오 — 순수 스크립트(LLM 0) |
| **일회성 AI 세션** | executor·reviewer·critic 워커 | 카드가 배정될 때 hermes가 스폰, 끝나면 소멸 | 예 — 하지만 소모품(작업당 새로 뜨고 죽음) |

핵심: **"AI가 계속 지켜보는" 부분은 없다.** 감시·알림·백업은 전부 시각에 맞춰 깨어나는 스크립트고, AI(gpt-5.5·codex)는 실제 코드 작업 1건을 처리하는 그 몇 분만 존재한다.

## 2. 무엇이 무엇으로 구현됐나 (실측 매핑)

### VPS 쪽 — systemd 사용자 타이머 (`~/.config/systemd/user/`)

`systemctl --user list-timers`로 직접 확인 가능한 실제 타이머 7종 + 상주 2종:

| 기능 | 구현체 | 트리거 | 종류 |
|---|---|---|---|
| 게이트웨이(Slack 수신·워커 스폰) | `hermes-gateway.service` | 부팅 시 자동(linger로 로그아웃에도 생존) | 상주 |
| 대시보드(Desktop 원격용) | `hermes-dashboard.service` | 부팅 시 자동 | 상주 |
| 이슈↔카드↔라벨 동기화 + 즉시알림 | `forge-mirror` → `label-mirror.py` | 2분마다 | 타이머 |
| 이벤트 원장 기록 | `forge-ledger` → `ledger-emit.py` | 10분마다 | 타이머 |
| 지식 배달(outbox→MEMEX) | `forge-flush` → `flush-outbox.py` | 10분마다 | 타이머 |
| 검문소 자가점검 | `forge-canary` → `canary.sh` | 6시간마다(00/06/12/18 KST) | 타이머 |
| 불변식·백업 신선도 감시 | `forge-drift` → `drift-audit.sh` | 매시 | 타이머 |
| 아침 리포트 + 일별 지식 미러 | `forge-morning` → `morning-report.sh` | 매일 07:30 KST | 타이머 |
| DB·MEMEX 백업 | `hermes-backup` → `nightly-backup.sh` | 매일 04:30 KST | 타이머 |

이 유닛들은 배포 스크립트(`deploy-vps.sh`)가 **자동으로 생성·enable**한다. 즉 설정이 코드(레포)에 있고, 배포하면 OS에 박힌다.

### 로컬(노트북) 쪽 — Windows 시작프로그램 (`셸:startup`)

회사 정책으로 작업 스케줄러(schtasks) 등록이 막혀 있어, **시작프로그램 폴더의 .vbs**(로그온 시 창 없이 실행)로 상주시킨다:

| 기능 | 구현체 | 트리거 |
|---|---|---|
| 로컬 게이트웨이(@forgelocal) | `Hermes_Gateway.vbs` | 로그온 시 |
| VPS 백업을 노트북으로 pull | `ForgeBackupPull.vbs` → `pull-backup.ps1` | 로그온 시 1회 |
| 로컬 장부 진행 공유 + 일일 백업 push | `ForgeLocalSync.vbs` → `local-sync.py --loop` | 로그온 후 5분 루프 |

## 3. 그래서 "24/7"과 "즉시알림"이 어떻게 성립하나 — 단계 분해

**24/7 처리**:
1. VPS는 24시간 켜진 임대 서버라 게이트웨이가 항상 떠 있음(1번 표)
2. 사람이 아무 때나 GitHub 이슈에 `forge:need-execution` 라벨을 닮
3. 2분 뒤 `label-mirror`(타이머)가 라벨을 보고 카드 생성
4. 60초 내 게이트웨이 디스패처가 카드를 집어 워커(AI) 스폰 → 작업 → PR
→ 사람의 밤낮과 무관하게 라벨만 달리면 굴러간다. "밤 공장"은 습관이지 제약이 아니다.

**즉시알림**:
1. `label-mirror`가 매 2분 실행될 때, 카드 상태를 직전 스냅샷(`mirror-state.json`)과 비교
2. done/blocked/failed로 **전이된 순간**을 감지하면 그 자리에서 Slack `chat.postMessage` 발송
→ AI가 판단해서 보내는 게 아니라, 스크립트가 상태 변화를 감지해 쏘는 것. 그래서 AI 세션이 없어도 알림이 온다.

**이중 보드 공유**:
- 로컬은 `local-sync.py`(5분 루프)가 로컬 장부의 전이를 감지 → GitHub 이슈에 코멘트 → 클라우드·사람이 그 이슈에서 로컬 진행을 봄. 역시 스크립트 소관.

## 4. 확인해 보는 법 (본인 환경에서)

- VPS 타이머: `ssh ubuntu@51.222.27.48 "systemctl --user list-timers"`
- VPS 상주: `ssh ubuntu@51.222.27.48 "systemctl --user status hermes-gateway"`
- 로컬 상주: 실행창(Win+R)에 `shell:startup` → .vbs 3개 확인
- 스크립트 원본(SoT): 이 레포의 `forge/scripts/`, `forge/hooks/`

**한마디 요약**: 자동화의 뼈대는 AI 세션이 아니라 **OS에 박힌 타이머·시작프로그램 + 순수 스크립트**다. AI는 실제 코드 한 건을 만들 때만 잠깐 소환되는 소모품이고, 그래서 대화창을 꺼도 시스템은 계속 돈다.
