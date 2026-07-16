# INFINITY_FORGE 로컬 hermes 운용 기획서 v1.3

> spec 여러 개를 던지면, hermes Kanban이 분해·배차·재시도를 맡고, 야간 노동은 전량 OpenAI 쪽(codex exec + GPT-5.5)이 세션 분리로 수행·검증하며, 사람은 아침에 GitHub에서 코멘트·머지만 하고, 그 아침 검수대에 대화형 Claude Code가 부조종사로 선다.
> Forge 기능은 대부분 Hermes 밖에 두고, Chat/Task 선택에 필요한 Hermes v0.18.2 입력 연결 6개만 확인 가능한 설치 묶음으로 적용·복원한다.
>
> 작성: 2026-07-09 (v1: 07-07 / v1.1·v1.2: 07-08) / 전제 문서: INFINITY_FORGE SoT, memex_knowledge_system_design_v3.md, 2026-07-07 hermes 재조사 로그, 2026-07-08 Anthropic 정책·VPS 실사용 조사
> 상태: 실행 단계 진입. 이 문서는 신규 세션 인수인계용으로 자족적이다. 신규 세션은 0절(제약) → 1절(결정) → 2.0절(환경) → 14절 Phase 0 런북 → 17절(시작 지침) 순서로 읽고 실행할 것.

## v1.2 → v1.3 변경 요약

1. **실행 환경 확정 (D18)**: OVH VPS 1대(RAM 8GB 사용자 확인)에 hermes + Kanban + 워커 + MEMEX 동거(배치 1안). 근거: 로컬 Windows의 WSL2 24/7 취약성 소거 + MEMEX pending messages의 localhost 배달. 기존 Phase 3의 "VM 이전" 항목 소멸(Phase 0에 흡수).
2. **MEMEX MCP 실가동 = Wave 0 탈출 (D19)**: save_memex 동작 확인(2026-07-08). 바인딩을 127.0.0.1로 하향(공개 표면 0), 원격 접근은 SSH 터널만. search_* soft 조회 조기 개방, pending message 실배달 개시.
3. **백업 승격 (D20) + 보안 하드닝 세트 (D21)**: Litestream 연속 복제(Phase 2 승격), Telegram polling(인바운드 0), UFW·키온리 SSH·fail2ban·대시보드 localhost.
4. Phase 0을 명령 단위 런북으로 교체, 인수인계 절(2.0절 환경 정보, 17절 시작 지침) 신설. 이 문서 단독으로 다른 세션이 실행 가능하도록 자족화.
5. (07-09 추가) **게이트웨이 Slack 전환 (D22)**: Telegram 사용 불가에 따라 Slack Socket Mode로 교체. 인바운드 0 원칙 불변, THE_FORGE의 검증된 패턴(네임스페이스·명령 어휘·셋업 문서) 재사용, 알림은 hermes 우회 직발송으로 이원화.
6. (07-09 추가) 슬래시 명령은 hermes 기본셋으로 확정(D23, THE_FORGE 어휘 이식 취소), 멀티레포 제품 워크스페이스 규약(D24): 보드 = 제품당 1개, 교차 작업은 이슈 1장·PR 3개·전부 green일 때만 ready-to-merge·제공자 먼저 머지.

## v1.1 → v1.2 변경 요약

1. **완료 판정 체계 신설 (D16·D17)**: "구현 완료"의 판정 주체를 모델의 산문에서 기계 조건으로 이전. spec coverage check(기획서 항목 ↔ 이슈 대조)로 "기획 100% 구현"을 숫자로 정의하고, Build 결과에 completed_items / remaining_items / checks_by_item을 강제해 남은 작업이 기록 없이 사라지는 일을 막는다.
2. **수용 기준(AC) 고정 원칙**: 카드 생성 시 확정, 워커는 이슈 본문 수정 금지(코멘트만), 변경은 forge:needs-decision 경유. 본문 편집 이벤트는 state-mismatch-check 감시 대상.
3. 배경: Claude Code 실사용에서 관찰된 두 실패 모드의 구조적 방어. (a) 과대 완료 선언(검증 없는 "완료했습니다"), (b) 자의적 phase 분해 후 후반 phase를 산문에만 남기고 종료(분해권·완료정의권·종료권의 한 세션 독점).

## v1 → v1.1 변경 요약

1. **야간 아키텍처 전환 (D15)**: Anthropic 정책 조사 결과(D11), Claude를 무인 야간 워커로 쓰는 합법 정액 경로가 없음 → 야간 노동 전량 OpenAI, Claude는 아침 부조종사로 이동. 교차 벤더 검증은 폐지가 아니라 밤→아침 시점 이동.
2. 라벨 접두사 `forge:` 확정 (D12), 재시도 3회 (D13), 알림 전부 즉시 (D14).
3. 머지 정책 manual/safe_auto/full_auto + 태스크 오버라이드 (D8), 검사 stop_on_error 배치 원칙 (D9), 실패 에스컬레이션 사다리 (D10) 신설.
4. MEMEX에 진행상태 read-only 단방향 미러 허용 (D4 개정).
5. 컴플라이언스 절 전면 개정: 1차 출처 기반.

---

## 0. 목적, 범위, 하드 제약

**목적**: AI 위임 업무 품질을 POC 수준으로. DDD → SDD → TDD 믹싱 전제. 야간 무인 실행, 아침 인간 검토는 GitHub 코멘트·머지만으로 완결.

**범위**: OVH VPS 1대(hermes + Kanban + 워커 + MEMEX 동거)에서의 운용. 로컬 Windows 노트북은 아침 부조종사(대화형 Claude Code)와 SSH 관제석 역할만. MEMEX 자체 구현은 별도 기획서(v3).

**하드 제약 (우선순위 순)**
1. 구독 컴플라이언스: Anthropic 구독 OAuth를 hermes 프로바이더에 직결 금지(공식 문서 확정). Agent SDK 크레딧 미사용 방침에 따라 야간 파이프라인에서 Claude 프로그래매틱 경로(`claude -p`) 제외. tmux 대화형 우회 봉인(계정 리스크). hermes 자체 LLM과 codex exec는 OpenAI 구독(서드파티 허용 확인).
2. 성능 최우선: 야간 처리량은 OpenAI 쿼터가 상한. Claude는 인간 경계에서 품질 기여.
3. Hermes 변경 최소화: 스킬·설정·worker가 기본이고, 공통 입력 연결 6개는 지원 버전·파일 내용 식별값 확인 뒤에만 설치한다. 버전 핀 + 월 1회 업그레이드 창.
4. 데이터 2층 분리: 진행상태(저장 실패 = 밤 유실)는 로컬 원자 쓰기, 지식(저장 실패 = 지연)은 파일 → MEMEX 비동기.

---

## 1. 확정 결정 요약 (D1~D26 누적)

| # | 결정 | 근거 | 일자 |
|---|---|---|---|
| D1 (개정) | 야간 노동 전량 OpenAI: codex exec = Build·Fix·터미널, GPT-5.5 = 작업 연결·Review, Codex 별도 세션 = Deep Check. Claude = 사람 경계 전담: spec 작성 동행, 설계 논의, 아침 ready-to-merge 검토 보조 | D11 정책 제약 + 자기채점 2층 방어(세션 분리는 컨텍스트 오염만, 모델 맹점은 교차로) | 07-08 |
| D2 | hermes Kanban = 진행상태 원장. 자작 디스패처 계획 축소(수십 줄) | Kanban이 원장·원자 클레임·재시도 이력·하트비트·크래시 회수·서킷 브레이커를 코어 1급 제공 | 07-07 |
| D3 | 운영 규율 4종을 채택 조건으로 | kanban.db도 SQLite라 state.db와 같은 계급의 실패 실증 | 07-07 |
| D4 (개정) | MEMEX = 지식 증폭기, 비동기 전용. 진행상태의 1차 저장소는 Kanban(로컬)이며, MEMEX엔 단방향 read-only 미러 허용. 재개·복구는 항상 Kanban 원본 기준. 역방향 쓰기 금지(다음 미러 사이클에 덮어써짐) | 쓰기 보장 순환 문제(복구 정보가 장애 때문에 저장 안 되는 구조) 회피 + 그래프 질의 편익 확보 | 07-08 |
| D5 | memex 사용 스킬 이름 = `memex` | 사용자 확정. MCP 서버명과 동일 무방(레지스트리 상이) | 07-07 |
| D6 | GitHub 층 = 표시 상태 + 인간 창구 (토폴로지 B). GitHub Issues를 내부 큐로 삼지 않음 | upstream RFC #19932 동일 경계 독립 수렴, 인간 피드백 분산 고통 실증 #47423 | 07-07 |
| D7 | 라벨 = 게시판. 자동 전이는 `issue-status-sync` 단독 작성. 클레임은 Kanban 원자 트랜잭션 | 라벨 API CAS 부재, TOCTOU 원천 제거 | 07-07 |
| D8 | 병합 방식은 Task마다 **Manual Merge / Safe Files Auto-Merge / All Validated PRs Auto-Merge** 중 하나를 반드시 고른다. 기본은 Manual Merge이고, 자동 방식은 최대 12시간의 Task 승인과 서버의 `AUTO_MERGE_ENABLED=true`가 모두 있어야 한다. Safe Files 방식은 파일 경로·변경 패턴을 코드 규칙으로 판정한다. | 검증(기계)과 결정(사람)의 분리 + 자동 권한의 장기 잔존 방지 | 07-08 |
| D9 | 결정론 차단은 hermes 훅에 두지 않는다: hermes 훅은 잘못된 JSON·non-zero exit·타임아웃 시 경고만 남기고 루프를 계속(fail-open). 차단은 워커 CLI 훅(exit 2, stop_on_error) + GitHub Actions. hermes 훅은 관찰·로깅·컨텍스트 주입·memex 미러 전용 | hermes 훅 계약 문서 확인 | 07-08 |
| D10 | 실패 에스컬레이션 사다리 L0~L3 + 야간 시작 system check + TESTS_FAILED/CHECK_ERROR 신호 구분 + check 스크립트 규율(모든 에러 경로 → exit 2) | stop_on_error의 단위는 태스크 1개, 전체 정지 방지 | 07-08 |
| D11 | Anthropic 정책(1차 출처 확정): 구독 OAuth의 서드파티 직결 = 금지(2월 약관, 4/4 시행). 공식 `claude` CLI 스폰 = 허용(4월 중순 확인). 단 6/15부터 `claude -p`·Agent SDK는 월 $200 크레딧에서 API 정가 차감. 본 시스템은 크레딧 미사용 방침 → 야간에서 Claude 프로그래매틱 제외. 대화형(터미널·웹·Cowork)만 구독 정액 잔존 | 2026-07-08 조사 (code.claude.com/docs/en/legal-and-compliance + 4~6월 보도 종합) | 07-08 |
| D12 | 라벨 접두사 = `forge:` | 사용자 확정 | 07-08 |
| D13 | 재시도 N = 3: 최초 시도 1 + 새 세션 이어받기 최대 3 = 태스크당 최대 4세션 후 서킷 브레이커. CHECK_ERROR는 카운트 제외 | 사용자 확정 | 07-08 |
| D14 | 알림 전부 즉시: 인간 액션 대상 전이(forge:needs-decision / forge:failed / forge:ready-to-merge 신규) + 시스템 이상(system check 실패, CHECK_ERROR 임계, 백업 무결성, pending messages 적체). 기계 전이는 제외. 아침 07:30 집계 리포트 병행 | 사용자 확정 (취침 중 무음 운용) | 07-08 |
| D15 | 야간 아키텍처 = 1안: 야간 OpenAI 단독 + 아침 Claude 검토 보조. 자기채점 절충 흡수: 밤 Review·Deep Check = 같은 벤더의 서로 다른 세션, 교차 벤더 층 = 아침 Claude | 사용자 확정 | 07-08 |
| D16 | spec coverage check: 기획서 체크리스트 항목 ↔ 대응 이슈 존재·close 여부를 스크립트(LLM 0)로 대조하고, 미대응 항목은 `task-service`로 재투입한다. 아침 리포트에는 "coverage N/M"을 고정 표기한다. **구현 완료 = coverage M/M ∧ 전 이슈 close ∧ 전 check 통과.** Build 결과에는 `completed_items`, `remaining_items`, `checks_by_item`을 필수로 둔다. | LLM의 "완료"는 사실 보고가 아니라 생성 문장이므로 항목별 결과와 검증을 기계적으로 묶는다. | 07-08 |
| D17 | 잔여 작업 등록 검사: `remaining_items`가 있으면 현재 Build 결과를 완료로 인정하지 않고 별도 Task로 명시적으로 등록한다. 수용 기준은 Task 생성 시 고정하고, 워커는 이슈 본문을 수정하지 않는다. 변경이 필요하면 `forge:needs-decision`을 거친다. | 분해권·완료정의권·종료권을 한 세션이 쥐면 범위 축소 후 조기 종료할 수 있으므로 잔여 작업을 눈에 보이게 만든다. | 07-08 |
| D18 | 실행 환경 = OVH VPS 1대(vps-aee0e707.vps.ovh.ca / 51.222.27.48, RAM 8GB 확인)에 hermes+Kanban+워커+MEMEX 동거(배치 1안). 로컬 Windows는 아침 부조종사·SSH 관제석 | 로컬 WSL2는 절전·강제 재부팅으로 24/7 부적합. VPS 실사용 후기의 지뢰 5종(볼륨 미마운트 스킬 유실, 재시작 정책 부재, 저사양 OOM, 대시보드 노출, wedged 게이트웨이) 대응책 내장 | 07-09 |
| D19 | MEMEX MCP = 127.0.0.1 바인딩(공개 표면 0). 원격은 SSH 터널만. HTTP 평문 + Bearer의 공개 노출 금지. save_memex 실가동 확인 = Wave 0 탈출: search_* soft 조회 조기 개방 + pending message 실배달 | HTTP 위의 Bearer는 열쇠를 평문으로 왕복시킴. 동거(D18)로 localhost 호출이 가능해져 공개 노출의 필요 자체가 소멸 | 07-09 |
| D20 | 백업 승격: Phase 0~1은 nightly .backup 임시, Phase 2부터 Litestream 연속 복제(OVH Object Storage, S3 호환) + 복제 지표 감시 + 주간 복원 리허설 + OVH 스냅샷 주 1회 | 단일 박스 동거로 폭발반경 확대. Litestream은 침묵 동기화 실패 이력 버전대가 있어 지표 감시 필수 | 07-09 |
| D21 | 보안 하드닝 세트: Telegram polling(인바운드 0), UFW deny + 22만 허용, SSH 키온리(새 터미널 검증 후 비밀번호 폐쇄), fail2ban, 대시보드 127.0.0.1 유지 + 터널, .env 600, 키 원문 기재 금지 | 노출 게이트웨이 하이재킹 사고 클래스 방어. OVH 하드웨어 안티 DDoS 위에 호스트 방어 적층 | 07-09 |
| D22 | 게이트웨이 = Slack (Telegram 사용 불가로 D14·D21의 채널 부분 대체). hermes Slack 게이트웨이는 Socket Mode(아웃바운드 WebSocket, 공개 엔드포인트 불필요)가 기본이라 인바운드 0 원칙 유지. 토큰 2종(xoxb 봇 + xapp 앱레벨) ~/.hermes/.env(600). 알림 이원화: 대화·지시 = hermes 게이트웨이, D14 즉시 알림·아침 리포트 = 스크립트가 Slack Web API 직발송(xoxb, hermes 우회: hermes가 죽어도 부고 도착). 채널 = 레포당 1개 + #forge-ops(시스템·홈채널). THE_FORGE의 `프로젝트명::동작` 네임스페이스·명령 어휘 재사용, 상세 셋업은 THE_FORGE 레포 문서 참조. 승인(`needs-decision` 해결·머지)은 여전히 GitHub만(D7 단일 작성자) | 사용자 환경 Telegram 불가. THE_FORGE가 동일 패턴(Socket Mode HITL) 검증 완료. 주의: 같은 xapp 토큰으로 소켓을 두 프로세스가 열면 이벤트가 예측 불가하게 분산되므로, THE_FORGE 리스너 병행 시 hermes용 Slack 앱 분리 필수(발신용 xoxb 재사용은 무충돌) | 07-09 |
| D23 | Slack 슬래시 명령 = hermes 기본셋(/stop, /model 등 매니페스트 기본) 사용. THE_FORGE 어휘(/resume·/skip·/revise) 이식 취소(D22의 해당 부분 대체). 네임스페이스 접두사 `프로젝트명::동작`은 유지 | 사용자 확정. 승인·반려의 본선은 GitHub 라벨이므로 Slack 명령은 보조 어휘로 충분 | 07-09 |
| D24 | 멀티레포 제품 워크스페이스: 대상 3레포(front-end·backend·workflow-engine)를 ~/work/<제품>/ 아래 나란히 클론, 워커 cwd = 부모 폴더, 레포 간 관계·빌드·교차 규칙은 워크스페이스 AGENTS.md에 기술. PAT에 3레포 전부 등록 + `gh auth setup-git`. 보드 = 제품당 1개(카드에 대상 레포 필드). 교차 작업 규약: 이슈 1장(계약 소유 주 레포)·카드 1장·PR 3개 상호 링크·ready-to-merge 판정은 연결 PR 전부 green일 때만·제공자 레포 먼저 머지 + 확장-수축(expand-contract) 작성·교차 레포 변경은 safe_auto 위험 분류상 자동 머지 금지 | 세 레포는 함께 배포되고 함께 깨지는 한 제품 = 한 격리 단위. git은 레포별 독립 remote·push가 기본이라 기술 장벽 없음. 찢어진 카드 3장은 원자성 수작업 의존이라 기각 | 07-09 |
| D25 | Build 완료 결과는 `forge-build-result/v1` JSON으로 고정한다. Task 설정 식별값, PR URL, 현재 commit, 변경 파일, 완료·잔여 항목, 항목별 검사를 정확히 기록하고 추가 산문을 금지한다. | Task Flow Worker가 결과를 엄격히 읽으므로 Work Check·worker 지시·결과 계약이 한 집합이어야 함 | 07-15 |
| D26 | `main`은 PR 필수 + GitHub Actions `eval` 필수 + 최신 branch strict ruleset으로 보호한다. 선택한 단계 완료 뒤 PR base/head가 바뀌면 이전 결과를 폐기하고 Build부터 다시 실행한다. Merge Worker는 `eval` 대기 중이면 기다리고, 성공 외 결론·누락·중복은 CHECK_ERROR로 병합하지 않는다. ruleset이 Forge 이슈 라벨을 강제하지 못하므로 Manual 사용자는 원본 이슈 `forge:ready-to-merge`와 `eval`을 모두 확인한다. | 실제 병합 대상 commit에서 Task 결과와 GitHub 조건을 각각 확인한다. | 07-15 |

---

## 2. 아키텍처 개요

### 2.0 실행 환경 정보 (인수인계용 사실 목록)

| 항목 | 값 | 비고 |
|---|---|---|
| VPS | vps-aee0e707.vps.ovh.ca (51.222.27.48), OVHcloud | **실측 2026-07-09**: vCPU 4, RAM 7.6GiB(≈8GB, 여유 3.9)+Swap 4GB, 디스크 ext4 `/dev/sda1` 75GB(24% 사용, ROTA=1=QEMU 가상디스크 표기·백엔드 NVMe), Ubuntu 24.04.4 LTS(커널 6.8.0-134). NFS 없음(규율1 충족), sudo NOPASSWD |
| 로컬 | Windows 노트북 (프로젝트 루트 C:\01.project\) | WSL2 상주 부적합 판정이 VPS 배치의 근거(D18) |
| SSH | ed25519 공개키 등록(주석 memex, 해당 노트북 한정) | 새 터미널 키 접속 검증 후 PasswordAuthentication no 전환. 셀프 감금 방지: 기존 세션 유지한 채 검증 |
| MEMEX 스택 | 같은 VPS의 docker (Neo4j + MCP 서버), vault = /data/vault 볼륨 | save_memex 실가동 확인(2026-07-08) = Wave 0 탈출 |
| MEMEX MCP | http://127.0.0.1:8080/mcp + Bearer 인증 (D19로 localhost 바인딩) | **API 키 원문은 어떤 문서·채팅·레포에도 기재 금지.** 서버 .env(chmod 600)만. HTTP 평문으로 공개 왕복한 이력이 있으면 키 로테이션 |
| 대상 워크스페이스 | front-end·backend·workflow-engine 3레포 (제품 워크스페이스, D24) | **회사 자산 여부 확인 필요**: 회사 코드면 개인 VPS 상주·OpenAI 전송에 대한 정책 승인이 선행. MEMEX 레포는 후속 후보 |
| 게이트웨이 | Slack, Socket Mode (D22) | 인바운드 포트 0 유지. 토큰 2종(xoxb·xapp)은 ~/.hermes/.env(600). SLACK_ALLOWED_USERS 필수(미설정 시 전체 거부가 기본). 상세 셋업은 THE_FORGE 레포 문서 참조 |

### 2.1 컴포넌트

| 컴포넌트 | 역할 | 상주 형태 |
|---|---|---|
| hermes gateway + Kanban 디스패처 | 카드 원자 클레임, 워커 스폰, 스테일 회수, 재시도, 서킷 브레이커 | systemd 상주 (OVH VPS 리눅스, D18) |
| kanban.db | 운영 SoT: 카드·의존·실행 이력·단계 결과 | 로컬 SQLite (ext4 + NVMe 강제) |
| 작업 프로필 4종 | Build / Review / Deep Check / Fix (전부 OpenAI 쪽) | Hermes가 Task마다 별도 세션 시작 |
| codex exec 서브프로세스 | 실제 구현·수정·터미널 작업 (Codex 구독) | Build·Fix 프로필이 tmux로 시작 |
| 대화형 Claude Code | 아침 부조종사: ready-to-merge 리뷰, ADR 논의, spec 작성 동행 | 로컬 Windows에서 사람이 직접 켬 (SSH로 VPS에서도 가능·정책 허용, 자동 스폰 금지) |
| GitHub (Issues·Labels·PR·Actions) | 인간 창구, 표시 상태 계산, 결정론 CI, 인테이크 | 원격 |
| 상태 동기화·기록·점검 스크립트 | LLM 0 순수 스크립트 (12절) | systemd timer / cron |
| repo 파일층 | docs/adr/, fix_notes/, skills/, activity.jsonl, pending-messages/ | git 커밋 대상 |
| MEMEX | 지식 증폭기 + 진행상태 read-only 미러 수신 | 같은 VPS 동거, 127.0.0.1:8080/mcp, soft 의존 불변 |

### 2.2 흐름 (1 spec의 일생)

```
대화 시작 → Chat 또는 Task 선택
  → Chat: 외부 작업 생성 없이 일반 대화
  → Task: 실행 단계와 병합 방식을 매번 선택하고 최종 확인
  → GitHub 이슈 + 변경할 수 없는 Task 설정 + Kanban 루트 카드 연결
  → Hermes 원자 클레임 → Build [forge:building]
      └ tmux로 codex exec 스폰 (tdd-cycle·wiki-check = AGENTS.md + Codex 훅)
      └ Codex work check(exit 2): 빈 diff·저장소별 테스트 통과 시에만 종료
      └ kanban_heartbeat 유지 → kanban_complete(`forge-build-result/v1` Build 결과)
  → PR [forge:reviewing]
  → 선택한 경우 Review(새 세션) → 통과 또는 fix_notes와 같은 PR Fix
  → 선택한 경우 Deep Check(별도 세션) → 엣지 테스트 추가
      └ problems_found → 같은 PR Fix
  → 선택한 단계가 현재 PR base/head에서 완료 [forge:ready-to-merge]
      └ main 전진 후 branch 갱신 → 이전 결과 폐기 → Build부터 재검증
  → Manual: 사람이 원본 이슈·base/head·CI를 확인해 병합
     Safe Files Auto-Merge / All Validated PRs Auto-Merge: 만료되지 않은 Task 승인과 AUTO_MERGE_ENABLED=true가 모두 있을 때만 자동 병합
  → 기록: activity.jsonl 원자 기록 + MEMEX pending message + PR이 자동으로 닫지 않은 이슈는 사람이 close
아침 → 사람 + 대화형 Claude 부조종사: ready-to-merge 리뷰(교차 벤더 층), needs-decision 해결, failed 힌트
매밤·매아침 → spec coverage check(D16): 기획서 ↔ 이슈 대조, 미대응 항목은 task-service 재투입
```

---

## 3. 상태 소유권 (SSoT 필드 분할)

제시해주신 내용은 **AI 에이전트 자동화 시스템(Kanban 워커)과 GitHub, 그리고 인간 개발자가 협업할 때 데이터가 꼬이지 않도록 각 데이터의 '주인(소유권)'을 엄격하게 정의한 설계서**입니다.

소프트웨어 아키텍처에서 시스템이 복잡해질 때 가장 중요한 것이 "이 데이터는 오직 한 놈만 수정할 수 있다"는 원칙(SSoT, Single Source of Truth)을 세우는 것입니다. 이를 어기면 AI와 인간이 동시에 같은 값을 수정하다가 데이터가 깨지는 '경쟁 상태(Race Condition)'가 발생하기 때문입니다.

항목별 핵심 의미를 명쾌하게 해석해 드립니다.

---

### 1. 필드별 상태 소유권 해석

#### 📋 작업 흐름 및 라벨 제어 권한

* **태스크 큐 상태 (Kanban 보드 상태):**
* **소유자:** 칸반 디스패처(스케줄러)와 워커(실제 일하는 AI)만 이 상태를 바꿀 수 있습니다.
* **의미:** 앞서 설명한 '미러'는 이 칸반 상태를 눈으로 **읽기만** 해야 하며, 멋대로 칸반 카드의 위치를 옮기면 안 됩니다.


* **GitHub 상태 라벨 (`forge:*`):**
* **소유자:** `issue-status-sync`가 단독으로 관리합니다.
* **의미:** GitHub 웹상에 붙는 라벨은 이 동기화 프로그램이 전담해서 붙이고 뗍니다. 단, 아래의 **인간 전용 예외 2가지**가 존재합니다.


* **인간 전용 전이 (인간의 치트키):**
* **행위:** 사람이 직접 1) `forge:needs-decision` 라벨을 손으로 지우거나, 2) PR을 최종 머지(병합)하는 행위입니다.
* **의미:** AI가 판단을 못 해 `needs-decision`에서 멈춰 있을 때 사람이 결정을 남기면, `issue-status-sync`가 카드를 다시 진행시키고 결정 코멘트를 가져옵니다.



#### 💬 소통 및 깃허브 인프라 권한

* **이슈·PR 코멘트 (댓글):**
* **소유자:** 사람과 AI 워커 모두 작성 가능합니다.
* **의미:** 댓글은 서로 대화하는 수단일 뿐, 시스템의 '상태'를 바꾸는 데이터가 아닙니다. 단, AI가 밖으로 댓글을 달 때 실수로 API 키 같은 비밀번호를 유출하지 않도록 필터(`아웃바운드 시크릿 리댁션 필터`)를 거칩니다.


* **PR·CI·merge 상태:**
* **소유자:** GitHub 시스템 자체.
* **의미:** 빌드가 성공했는지(CI Green), 머지가 되었는지는 GitHub이 관리하며, 칸반 시스템은 이 상태를 읽어서 참고만 합니다.



#### 💾 문서 및 데이터 기록 권한

* **ADR·SoT 파일 (`docs/adr/`):**
* **소유자:** 코드를 실행하는 주체(Builder). 단, **인간의 승인이 떨어진 후에만** Git에 커밋(기록)할 수 있습니다.
* **의미:** "의사결정은 결국 Git 기록으로 남긴다"는 원칙을 실현하는 부분입니다.


* **이슈 본문 (요구사항 고정):**
* **소유자:** `task-service`로 Task 생성을 확인한 사람 또는 처음 이슈를 만든 사람.
* **의미:** **한 번 정해진 요구사항 본문은 AI 워커가 절대 수정할 수 없습니다.** 수정하고 싶다면 무조건 인간 보류 라벨(`forge:needs-decision`)을 거쳐야 합니다. 만약 몰래 본문을 편집하면 감사 시스템(`state-mismatch-check`)에 걸리게 됩니다.


* **`activity.jsonl` (활동 로그):**
* **소유자:** `activity-log-writer` 프로그램.
* **의미:** 시스템에서 일어난 모든 일은 수정 불가능하고 뒤에 붙이기만 가능한(Append-only) 로그 파일에 기록됩니다. 비행기 블랙박스 같은 역할입니다.


* **지식 entry + 진행상태 읽기 전용 복사본 (`pending-messages/`):**
* **소유자:** `memex` 스킬(AI의 메모리 시스템) 및 `activity-log-writer` 확장.
* **의미:** MEMEX(지식 저장소) 쪽에 저장된 데이터는 읽기 전용 복사본(Read Replica)이므로, 역방향으로 데이터를 수정하려고 시도하는 것은 금지됩니다.

---

## 2. 불변식 2개 (감사 대상)

시스템이 정상적으로 작동하고 있는지 감시(Audit)할 때 무조건 지켜져야 하는 **절대 규칙 2가지**입니다. 하나라도 깨지면 시스템에 에러가 발생합니다.

1. **열린 이슈에는 `forge:*` 상태 라벨이 정확히 1개만 붙어 있어야 한다.**
* (예: 한 이슈에 `forge:building`와 `forge:reviewing`가 동시에 붙어 있으면 안 됨. 상태가 꼬인 것이므로 즉시 경고)


2. **이슈별 루트 카드 1개와 단계 완료 증거별 자식 카드 1개가 있어야 한다.**
* GitHub 이슈와 확정된 Task 설정당 `forge-task:<OWNER/REPO>#<ISSUE>:<TASK_SETTINGS_HASH16>` 루트 카드는 정확히 1개다.
* 완료된 상위 단계 결과당 `forge-step:<OWNER/REPO>#<ISSUE>:<BUILD|REVIEW|DEEP_CHECK|FIX>:<RESULT_HASH16>` 자식 카드는 정확히 1개다.
* Review·Deep Check·Fix는 서로 다른 Hermes Task/session이므로, 이슈 하나에 단계별 자식 카드가 생기는 것은 정상이다. 같은 완료 결과로 같은 단계 카드가 둘 생기거나 부모 연결이 갈라지는 것이 오류다.



---

### 💡 한 줄 요약

> **"인간과 AI 워커가 동시에 작업할 때 발생할 수 있는 충돌을 방지하기 위해, 각 데이터(라벨, 본문, 로그, 상태)의 수정 권한을 칼같이 나누고 감시 규칙(불변식)을 세워둔 설계도입니다."**

---

## 4. 라벨 체계 (스킬: `forge-labels`)

상태 라벨 9종, 상호 배타. `triage`는 Hermes 카드 상태이며 GitHub에서는 `forge:needs-details`로 투영한다. merged/closed는 GitHub 네이티브 상태를 사용하고 유형 라벨(Bugfix 등)은 별도 차원이다.


| **라벨명 (forge:)**     | **의미 (실제 역할)**                              | **미러/작동 방식**                                                                              |
| -------------------- | ------------------------------------------- | ----------------------------------------------------------------------------------------- |
| **`needs-details`** | **작업 설명 보완 필요** | 작업 범위나 수용 기준이 부족해 사람이 내용을 보완해야 합니다. |
| **`needs-decision`** | **사람의 설계 결정 필요** | 자동으로 정할 수 없는 선택이 있어 이 작업만 멈추고 사람의 결정을 기다립니다. |
| **`ready-to-build`** | **구현 시작 대기** | Task 설정이 확정되어 Build를 시작할 수 있습니다. |
| **`building`** | **구현 또는 수정 중** | Build 또는 Fix가 코드를 작성하고 테스트합니다. |
| **`reviewing`** | **코드 검토 중** | Review가 결과와 실제 변경을 수용 기준에 맞춰 확인합니다. |
| **`deep-checking`** | **심층 검사 중** | Deep Check가 경계 조건과 실패 사례를 추가 테스트로 확인합니다. |
| **`ready-to-merge`** | **선택한 단계 완료** | 병합 전 현재 commit의 `eval`과 GitHub 조건은 사람 또는 Merge Worker가 별도 확인합니다. |
| **`waiting-for-help`** | **사람 도움 대기** | 외부 의존성이나 사람 입력이 필요해 작업이 멈춰 있습니다. |
| **`failed`** | **재시도 한도 초과** | 최대 재시도 횟수를 넘겨 사람이 원인을 확인해야 합니다. |

- **기본 흐름(Happy Path):**

    설명 보완(`needs-details`) ➡️ (사람 결정이 필요하면 `needs-decision`) ➡️ 구현 대기(`ready-to-build`) ➡️ 구현 중(`building`) ➡️ 코드 검토(`reviewing`) ➡️ 심층 검사(`deep-checking`) ➡️ 병합 준비 완료(`ready-to-merge`) ➡️ 완료 및 닫기(`close`). 선택한 Task 흐름에 없는 검토 단계는 건너뜁니다.

- **반려 시나리오:**

    코드 검토나 심층 검사에서 수정이 필요하면 구체적인 `fix_notes`와 함께 `building`으로 돌아갑니다. 수정 뒤에는 기존 검토 결과를 재사용하지 않고 선택한 흐름을 다시 확인합니다.

- **예외 흐름 (Anywhere ➡️ Blocked/Failed):**

    어느 단계에서든 사람 입력이나 외부 의존성이 필요하면 `waiting-for-help`, 재시도 한도를 넘기면 `failed` 상태로 이동합니다.


설정 SSoT는 `forge-labels` 스킬 파일 + 미러 설정. 사람이 직접 라벨 달아 투입하는 경로는 미러가 흡수(원안 시나리오 보존).

---

## 5. 작업 단계 정의 (전부 OpenAI 쪽)

디스패처 스폰: `hermes -p <profile> chat -q "work kanban task <id>"`. 파이프라인 = 부모 이슈 카드 + 역할별 자식 카드(의존 승격이 순서 보장).
이 시스템은 작업을 자동 분배하는 스케줄러(`hermes`)가 각 역할에 맞는 프로필을 실행하여 AI 워커들을 구동합니다.

|**화면 표시 이름**|**엔진 (AI 모델)**|**주요 임무**|**완료 조건**|
|---|---|---|---|
| **Build** (`builder`) | Codex exec | 수용 기준을 구현하고 PR과 Build 결과를 제출합니다. | Work Check, 저장소 테스트, Build 결과의 base/head commit 일치 |
| **Review** (`reviewer`) | GPT-5.5, Build와 다른 세션 | Build 결과를 실제 변경·테스트·수용 기준과 대조합니다. | 엄격한 Review 결과 형식과 현재 PR commit 일치 |
| **Deep Check** (`deep_checker`) | Codex exec, 별도 세션 | 경계 조건과 실패 사례를 찾고 필요한 방어 테스트를 같은 PR에 추가합니다. | 추가 테스트를 포함한 현재 PR commit의 검사 통과 |
| **Fix** (`fix`) | Codex exec | Review 또는 Deep Check의 `fix_notes`를 같은 PR에서 재현하고 최소 변경으로 고칩니다. | 수정 뒤 이전 결과를 폐기하고 Build부터 다시 확인 |

> 👨‍✈️ **아침 부조종사 (인간 개발자 + Claude Code):**
>
> 밤새 OpenAI 워커들이 자동화 파이프라인을 돌려놓으면, 아침에 출근한 인간 개발자가 대화형 AI인 Claude Code를 켜서 최종 승인 단계(`forge:ready-to-merge`)의 코드들을 교차 검증(Cross-vendor)하며 함께 리뷰하고 다음 기획을 동행합니다. **안전성을 위해 이 과정은 시스템이 자동 스폰하지 않고 오직 사람이 직접 켜야 합니다.**


**세션 분리 규율**: Review와 Deep Check는 Build와 반드시 다른 세션을 사용한다. 모델 공통 맹점은 아침의 대화형 Claude 검토가 보완한다.

**최신 HEAD 재검증 규율(D26)**: strict ruleset 때문에 PR branch가 갱신되면 이전 Deep Check 결과를 새 HEAD에 넘기지 않는다. 새 Build부터 선택한 검사를 다시 실행하고, 기존 `added_tests`가 보존됐는지도 확인한다.

**Build 결과 형식 (D16·D17·D25)**: `kanban_complete` summary는 `forge-build-result/v1` JSON이다. Task 설정 식별값, PR URL, 확인한 base/head commit, 변경 파일, 완료·잔여 항목, 항목별 검사를 기록한다. "완료했습니다" 한 문장만으로는 완료로 처리하지 않는다.

## 2. AI의 과대 완료 선언을 막는 Build 결과 (D16·D17)

Build가 완료 신호(`kanban_complete`)를 보낼 때, 말로만 "다 했습니다"라고 하는 것을 방지하기 위해 **세 필드를 정해진 JSON 형식으로 제출**하도록 강제합니다. 여기에 PR URL, `built_base_commit`, `built_commit`, 변경 파일도 함께 들어가며 두 commit은 현재 PR base/head와 각각 같아야 합니다.

1. **`completed_items` (완료 목록):** 이번에 완료한 수용 기준을 나열합니다. Review가 실제 변경과 대조합니다.

2. **`remaining_items` (남은 목록):** 완료하지 못한 수용 기준을 나열합니다. Build 완료 결과에서는 빈 배열이어야 하며, 남은 항목은 별도 Task로 등록합니다.

3. **`checks_by_item` (항목별 검증):** 완료된 각 항목을 **어떤 테스트나 명령으로 확인했는지** 기록합니다. 검증이 없는 항목은 완료로 인정하지 않습니다.


> ⚠️ **범위 축소 불가능 규칙:**
>
> 일을 하다 보니 카드가 너무 크고 복잡할 때 AI 개발자가 조용히 범위를 줄여서 완료 처리하는 꼼수는 허용되지 않습니다. 합법적인 선택지는 **1) 쪼개서 다음 카드로 넘기기, 2) 인간에게 헬프 요청(`forge:needs-decision`), 3) 묵묵히 다 만들기**의 3가지뿐입니다.

**래퍼 오버헤드(정직 명시)**: Kanban 워커는 항상 LLM 에이전트라 태스크당 래퍼 토큰이 든다. 완화: toolset을 kanban + terminal로 제한, 스킬 간결화, 저가 모델 핀. exit 0 단순 종료는 protocol_violation + 자동 block(코어 내장 fail-loud)이므로 kanban_complete/kanban_block 호출을 스킬에 명시.

**장시간 작업**: codex exec를 tmux 실행 + 폴링 사이 kanban_heartbeat. 스테일 회수 타임아웃(`kanban.dispatch_stale_timeout_seconds`, 기본 4h)은 야간 1~2h로 하향 검토.

**스킬 목록**: forge-labels, build-task(builder 래퍼), review-task, deep-check, fix-task, task-service, memex, tdd-cycle(Codex 쪽: AGENTS.md + 훅), wiki-check(SoT diff 감지 시 decision 기록 강제, Codex 훅).

---

## 6. 머지 정책 (D8)
 **AI 에이전트가 코드를 완성했을 때, 이를 실제 메인 소스 코드에 합치는 작업(Merge, 머지)을 얼마나 자동화할 것인가**에 대한 정책 설계서입니다.

개발자가 일일이 코드를 확인하고 합치는 리소스를 줄이되, 시스템이 망가지는 치명적인 사고를 방지하기 위해 **위험도에 따라 병합 권한을 3단계로 나누고 안전장치를 마련한 아키텍처**입니다.

항목별 핵심 의미를 명쾌하게 해석해 드립니다.

---

### 1. 세 가지 병합 방식

기본값은 **Manual**이다. Task를 시작할 때 **Manual / Safe Files Auto-Merge / All Validated PRs Auto-Merge** 중 하나를 매번 고르고 최종 확인한다.

> **구현 경계(2026-07-16):** Task Flow Worker, Issue Status Sync, Merge Worker는 Task 설정·Hermes 카드·GitHub 이슈와 PR을 실제로 읽고 쓴다. 입력 누락, 내용 불일치, API 오류가 있으면 해당 실행은 코드 `2`로 끝나며 다음 외부 쓰기를 하지 않는다. production 서버 반영 여부는 별도로 확인한다.

* **Manual:**
* **자동 머지:** 없음 (0%)
* **사람이 확인하는 것:** 모든 의사결정 문서(ADR)와 **모든 코드 머지 건**을 사람이 직접 검토하고 버튼을 눌러 승인해야 합니다.


* **Safe Files Auto-Merge:**
* **자동 머지:** "되돌리기 쉬운 가벼운 작업"만 AI가 알아서 합칩니다.
* **사람이 확인하는 것:** 중요 의사결정(ADR) 및 "되돌리기 힘든 위험한 작업"의 머지 건만 사람이 직접 검토합니다.


* **All Validated PRs Auto-Merge:**
* **자동 머지:** 선택한 Task 흐름과 Deep Check, 테스트(CI), 현재 commit 검사가 모두 통과하면 **검증된 PR을 자동으로 머지**합니다.
* **사람이 확인하는 것:** 오직 핵심 의사결정 문서(ADR)만 최종 검토합니다.



> Merge Worker의 자동 쓰기는 기본적으로 꺼져 있다. 두 자동 방식은 Task의 만료되지 않은 승인에 더해 운영 환경에 정확히 `AUTO_MERGE_ENABLED=true`가 있어야 한다. Manual에서는 branch 갱신, 병합, lifecycle 변경을 포함한 GitHub 쓰기가 0이다.

---

## 2. Safe Files Auto-Merge의 파일 분류 기준

Safe Files Auto-Merge는 자동 병합 가능한 변경인지 코드 규칙으로 판단한다. **LLM의 주관적 판단을 쓰지 않고**, 파일 경로·변경 종류·파일 내용 조건을 모두 확인한다.

| 🚨 되돌리기 어려움 (사람 승인 필수) | ✅ 되돌리기 쉬움 (자동 머지 가능) |
| --- | --- |
| **데이터베이스 스키마 변경 (`migrations/`)**<br>

<br>→ DB 구조를 바꾸는 것은 롤백 시 데이터 유실 위험이 큼 | **문서 및 리드미 수정 (`docs/`, `README.md`)**<br>

<br>→ 단순 문서 수정은 시스템 동작에 영향이 없음 |
| **공개 API 시그니처 변경**<br>

<br>→ 다른 시스템이나 사용자가 쓰고 있는 인터페이스를 바꾸면 장애가 발생함 | **테스트 코드 추가**<br>

<br>→ 프로덕션 코드에 영향을 주지 않는 순수 테스트 코드 추가 |
| **파일 삭제 포함**<br>

<br>→ 기존 소스코드를 지우는 행위는 사이드 이펙트 추적이 어려움 | **새 파일 추가**<br>

<br>→ 기존 소스를 건드리지 않고 새로운 모듈/파일을 붙이는 행위 |
| **의존성 파일 변경 (`package.json`, `requirements.txt` 등)**<br>

<br>→ 외부 라이브러리 버전이 바뀌면 다른 코드들이 깨질 수 있음 | **내부 리팩토링**<br>

<br>→ 외부 인터페이스(API)를 건드리지 않고 내부 로직만 깔끔하게 다듬는 것 |
| **CI/CD 배포 및 보안 시크릿 설정**<br>

<br>→ 배포 파이프라인이나 환경 변수를 건드리는 것은 보안/인프라적 핵심 영역임 | **주석 및 포맷 수정**<br>

<br>→ 코드 설명(Comment)을 달거나 줄 바꿈, 들여쓰기 정돈 |

---

### 💡 한 줄 요약

> **"AI가 코드를 다 짰을 때, 단순 문서나 테스트 추가 같은 '안전한 작업'은 자동으로 합쳐서 생산성을 극대화하고, DB 변경이나 라이브러리 추가 같은 '위험한 작업'은 반드시 사람이 승인하도록 걸러내는 스마트한 필터링 정책입니다."**

안전망: auto-merge분도 전부 PR로 남으므로 revert 1커밋으로 원복(되돌리기 쉬운 것만 자동이므로 자기일관).

---

## 7. 종료조건 ①~⑥ 배치

제시해주신 내용은 **AI 자동화 개발 시스템이 특정 작업(이슈/PR)을 성공적으로 마쳤는지, 그리고 프로젝트 전체가 완벽히 끝났는지를 판정하는 '종료 조건(Definition of Done)' 배치도**입니다.

가장 중요한 아키텍처 원칙은 "CI(지속적 통합) 서버에는 LLM(인공지능)을 올리지 않는다"는 점입니다. 인공지능의 주관적이고 확률적인 판단(LLM) 대신, 100% 명확한 규칙과 코드로만 작동하는 결정론적 순수성(Deterministic)과 보안/규정(컴플라이언스)을 지키겠다는 의지입니다.

---

### 1. 6가지 종료 조건(①~⑥) 배치 해석

#### 1단계: 코드 및 파일 검증 (기계적 검사)

* **① Verifier output (검증기 출력):**
* **구현 위치:** GitHub Actions 테스트 잡 (LLM 사용 안 함 ❌)
* **의미:** 작성된 코드가 빌드 오류가 없는지, 테스트 코드를 통과하는지 기계적으로 검사합니다.


* **② Diff (코드 변경점):**
* **구현 위치:** Pull Request 자체 + 코덱스 훅(Codex Hook)
* **의미:** 실제로 바뀐 코드 내용(Diff)이 있는지 확인합니다. AI가 아무것도 안 바꾸고 빈(Empty) 결과물을 냈을 때 이를 감지하고 차단합니다.


* **③ Deterministic check (결정론적 체크):**
* **구현 위치:** 이중 잠금 (로컬 Codex Stop 훅 + 원격 GitHub Actions)
* **의미:** 입력값에 따라 결과가 항상 100% 똑같이 떨어지는 기계적 규칙(정적 분석, 문법 검사 등)을 로컬과 서버 양쪽에서 엄격하게 2중으로 검증합니다.



#### 2단계: 주관적 맥락 및 시스템 모니터링

* **④ LLM judge 보조 (LLM 판사 보조):**
* **구현 위치:** Review와 Deep Check
* **의미:** 기계적 검사(③번)를 **완벽히 통과한 건에 한해서만** 실행됩니다. 가독성이 좋은지, 기획 의도에 맞는지 같은 '주관적인 맥락'만 LLM 에이전트가 검토하며, 최종 결정은 아침에 로그인한 인간 개발자(Claude 부조종사를 곁들인)가 내립니다.


* **⑤ Capability eval + 회귀 + 감시 (역량 평가 및 모니터링):**
* **구현 위치:** 주간 Actions 스케줄 잡 + 상시 작동하는 `state-mismatch-check`
* **의미:** 일주일마다 정기적으로 시스템 전체 성능과 회귀 테스트(과거엔 잘 되다 지금 안 되는 것이 있는지)를 수행하고, 시스템 데이터가 오염되지 않았는지 상시 감시합니다.



#### 3단계: 기록 및 영구 보존

* **⑥ 기록 동시 저장:**
* **구현 위치:** 머지 이벤트 발생 시 `activity-log-writer`가 작동
* **의미:** 작업 결과, 걸린 시간, 비용, 대화 기록을 활동 로그(`activity.jsonl`)에 한 줄씩 안전하게 추가합니다. 대화 원본은 로컬 아카이브에 두고 활동 로그에는 경로만 남기며, MEMEX에는 요약본만 보냅니다.



---

### 2. 구현 완료의 기계적 정의 (D16)

이 시스템은 AI 모델이 스스로 *"저 다 만들었어요!"*라고 선언하는 것을 **절대 믿지 않습니다.** 오직 아래 명시된 **수학적/기계적 술어(조건)가 참(True)이 되어야만** 완료된 것으로 인정합니다.

$$\text{spec 커버리지 M/M} \wedge \text{전 이슈 close} \wedge \text{전 게이트 초록}$$

* **spec 커버리지 M/M:** 기획서에 명시된 요구사항 대비 테스트 코드가 빈틈없이 100% 매칭되어 커버되는가?
* **전 이슈 close:** 등록된 모든 깃허브 이슈(작업 티켓)가 해결되어 닫혔는가?
* **전 게이트 초록:** 모든 빌드, 테스트, CI 파이프라인(게이트)이 통과(Green light)했는가?

이 세 가지 조건이 모두 동시에 충족(`AND, ∧`)되기 전까지 시스템은 스스로를 언제나 "미완성" 상태로 인지하고 루프를 돕니다.

---

#### 💡 한 줄 요약

> **"AI의 주관적인 판단은 기계적 검증을 통과한 뒤에만 보조적으로 사용하고, 프로젝트의 최종 완료 여부는 테스트 통과율 및 이슈 해결률 같은 100% 완벽한 기계적 데이터(술어)로만 판정하는 엄격한 종료 통제 시스템입니다."**

---

## 8. 실패 에스컬레이션 (D10 + D13)
제시해주신 내용은 AI 에이전트가 코드를 작성하다가 실패했을 때, 시스템이 뻗지 않고 단계적으로 문제를 해결하거나 사람에게 도움을 요청하도록 설계한 '예외 처리 및 실패 대응 체계(Failure Escalation)'입니다.

시스템의 핵심 철학은 "작업 하나가 실패해도 전체 파이프라인은 멈추지 않고 계속 돌아간다"는 점입니다. AI가 스스로 고칠 기회를 주되(L0~L1), 안 되면 안전장치(L2)를 작동시켜 사람을 부르고, 그 와중에 다른 일은 계속 처리(L3)합니다.

---

### 1. 단계별 실패 대응 에스컬레이션 (L0 ~ L3)

| 단계 (Layer) | 동작 방식 및 핵심 메커니즘 |
| --- | --- |
| **L0: 자기 수정**<br>

<br>(Self-Correction) | **같은 작업 세션 내에서 스스로 해결 시도**<br>

<br>로컬 검증 단계에서 실패 코드(`exit 2`)가 떨어지면, 발생한 에러 메시지(`stderr`)를 AI 모델의 프롬프트에 곧바로 다시 입력(Feedback Loop)하여 스스로 코드를 고치게 만듭니다. |
| **L1: 세션 이어받기**<br>

<br>(Retry with Context) | **새로운 세션을 열어 다시 시도 (최대 3회)**<br>

<br>L0에서 해결이 안 되면 세션을 새로 만들되, 기존 작업 브랜치와 소스 코드를 그대로 유지합니다. 이때 이전 시도의 코멘트들과 **"왜 실패했는지에 대한 분석(반성문)"**을 칸반 도구를 통해 새 AI 에이전트에게 주입하여 처음부터 다시 풀게 합니다. |
| **L2: 서킷 브레이커**<br>

<br>(Circuit Breaker) | **3회 모두 실패 시 즉시 차단 및 사람 호출 (`forge:failed`)**<br>

<br>재시도 횟수를 모두 소진하면 작업을 중단하고 즉시 담당자에게 알림을 보냅니다. 사람이 확인 후 해결 힌트를 댓글로 달고 라벨을 원복해주면, **사람이 쓴 피드백 코멘트가 다음 시도의 컨텍스트로 주입**되어 재시작합니다. |
| **L3: 작업 분리**<br>

<br>(Non-blocking Dispatch) | **장애 격리 및 정상 작업 계속 진행**<br>

<br>하나의 태스크가 실패해서 묶여 있더라도, 스케줄러(디스패처)는 대기 중인 다른 독립적인 태스크들을 계속 배차하여 일하게 만듭니다. 오직 실패한 태스크의 결과를 기다려야 하는 자식 태스크들만 대기 상태로 묶어둡니다. |

---

### 2. 정밀한 예외 처리를 위한 설계 규칙들

#### 🚨 신호 구분 (엄격한 원인 분석)

에러가 났을 때 그것이 **AI가 짠 코드의 문제**인지, **서버나 시스템 자체의 문제**인지를 명확히 구분하여 대응합니다.

* `TESTS_FAILED:` (코드 오류) ➡️ AI의 잘못이므로 재시도 카운트를 차단(깎음)하고 다시 시도하게 유도합니다.
* `CHECK_ERROR:` (장치 고장) ➡️ 서버 장애나 네트워크 끊김 등 인프라 에러이므로 **AI 카운트를 깎지 않고 즉시 사람에게 알림**을 보냅니다.

#### 시스템 자체 점검 (System Check)

* 자동 배차를 시작하기 전에, 이미 정답을 알고 있는 입력으로 시스템 검사를 실행합니다.
* 이 검사가 실패하면 검증 시스템 자체에 문제가 생긴 것이므로, 전체 배차를 즉시 중단하고 사람에게 알립니다.

#### ⚙️ Check 스크립트 규율 (단순함 유지)

* 테스트 스크립트 내부에서 어설프게 복잡한 에러 처리를 하지 않고, 어떤 에러 경로를 만나든 **무조건 시스템 종료 코드 `exit 2`로 치환**되도록 규격화합니다.
* 이를 통해 검증 시스템의 복잡도를 낮추고 빠르고 명확하게 AI에게 실패 신호를 전달합니다.

---

### 3. 세션 수 정정 (2026-07-13 실측 피드백)

> 💡 **핵심 내용:** 설정 값과 실제 작동 횟수의 간극을 발견하여 수정한 기록입니다.

* **발견된 버그:** 원래 설계 사상인 D13 규칙은 "최초 1회 실행 + 실패 시 이어받기 3회 = 총 4회 실행"이 목적이었습니다.
* 그런데 실제로 구동하는 실행기(`hermes`)의 `--max-retries N` 옵션을 실측해 보니, "추가 재시도 횟수"가 아니라 "연속 실패 허용 횟수(총 실행 횟수)"로 작동하고 있었습니다.
* 즉, 기존에 라벨 미러링에서 주었던 `--max-retries 3` 설정 때문에 실제로는 총 3번만 돌고 에러로 뻗어버려 설계(총 4회)보다 1회가 부족했던 것입니다.
* **해결:** 이를 일치시키기 위해 카드 생성 시 매개변수를 `--max-retries 4`로 상향 수정하였습니다. 과거 문서(D13)를 직접 뜯어고치지 않고, 변경 이력을 아카이빙하는 규칙에 따라 본 문서에 소급 수정 내용을 주석으로 남겨두었습니다.

---

#### 💡 한 줄 요약

> **"AI가 에러를 내면 스스로 디버깅할 기회를 여러 번(총 4회 세션) 주되, 해결 불가능한 단계에 도달하면 다른 작업에 영향이 가지 않도록 격리시킨 후 개발자에게 즉시 SOS를 요청하도록 설계된 견고한 안전장치입니다."**
---

## 9. hermes 약점 → 극복 장치 매핑

| 약점 (근거) | 극복 장치 | 층 |
|---|---|---|
| SQLite 손상: BTRFS COW·NFS 잠금과 WAL 비호환 | fs-precheck: ext4 + 로컬 NVMe 강제, 부적합 시 설치 중단 | 규율 1 |
| 네이티브 백업의 조용한 파일 누락 | nightly-backup: sqlite3 .backup 직접 + PRAGMA integrity_check + 크기·mtime 검증, 별도 디스크 | 규율 2+3 |
| 미지 assignee 카드의 ready 영구 체류 (디스패처 직접 생성 경로, 확인 필요) | state-mismatch-check: ready 장기 체류 알람 | 규율 3 |
| session_search 취약·장기 기억 유실 | 기억을 hermes 밖으로: 지식 = MEMEX pending messages, 운영 = activity log, 세션 = 소모품 | 구조 |
| 훅 fail-open (D9: non-zero exit·타임아웃 시 경고만 남기고 계속) | 결정론 차단을 hermes 훅에 두지 않음. Codex 훅(exit 2) + Actions로 stop_on_error. hermes 훅은 관찰·주입·memex 미러 전용 | 게이트 |
| 자기채점 silent-pass | 세션 분리 Review·Deep Check + 결정론 선행 + 아침 Claude 교차 확인. Kanban 완료 결과 검사는 보완재 | 선택한 검사 |
| 인간 피드백 2면 분산·비동기화 | 인간 창구 GitHub 1면 통일. 사람은 대시보드·세션에서 결정하지 않는다 | 토폴로지 |
| 업스트림 롤러코스터 | 코어 무수정 + 버전 핀 + 월 1회 창. 미러는 upstream 스펙(#31992 멱등키, #19932 경계) 호환으로 은퇴 가능 설계 | 원칙 |
| GitHub 의존 (신규) | Kanban 로컬 SoT라 GitHub 장애 밤에도 완주, issue status sync와 pending messages가 복구 뒤 따라잡기 | 토폴로지 |

---

## 10. 컴플라이언스 (D11, 1차 출처 기반)

**정책 사실 (2026-07-08 기준)**
- 2월 약관: Free/Pro/Max OAuth는 Claude Code·Claude.ai 전용. 서드파티 도구(Agent SDK 포함 당시 문구) 사용 = 약관 위반.
- 4/4 시행: 서드파티 하네스의 구독 커버 종료(OpenClaw 등). 공식 CLI는 SSH·tmux 상주 포함 지원 유지.
- 4월 중순: `claude -p` 서브프로세스·원격 CLI 허용 확인.
- 6/15: Agent SDK 크레딧 발효. `claude -p`·Agent SDK·Claude Code GitHub Actions는 월 $200(Max 20x) 크레딧에서 API 정가 차감. 대화형 터미널·웹·Cowork만 구독 정액 잔존.
- 공식 문서는 사전 통지 없는 제재 권리를 명시. 정가 규칙이 반년에 3번 바뀐 영역.

**본 시스템의 결정**
1. hermes 프로바이더에 Anthropic 구독 OAuth 직결 금지 (API 키만 허용, 단 본 시스템은 사용 안 함).
2. 크레딧 미사용 방침 → 야간 파이프라인에서 `claude -p` 제외.
3. tmux로 대화형 Claude를 조종해 정액 풀에 남는 우회 = 봉인 (통상적·개인적 사용 전제 위반 패턴, 본계정 정지 리스크).
4. Claude의 유일한 사용처 = 사람이 직접 켜는 대화형 세션 (아침 부조종사, spec/ADR 동행) = 통상 사용.
5. OpenAI 쪽: codex exec(Codex 구독) + hermes 네이티브 GPT-5.5(OpenAI 구독). OpenAI는 서드파티 구독 사용 공식 허용 (OpenClaw의 Codex OAuth 전환이 공개 선례).
6. 재점검 주기 4~6주 유지. 점검 대상이 Anthropic에서 **OpenAI 정책 안정성**으로 이동.

---

## 11. MEMEX 연동 (`memex` 스킬 + 진행상태 미러)

**memex 스킬 (D5)**
- 설치 2곳: Codex 쪽(AGENTS.md 참조 스킬) + hermes 스킬. 계약 동일.
- 발동: spec 완료(merge) 시 배치 1회 기본 / 리뷰 반려 반성문 = [error] / ADR 확정 = [decision] / 재사용 가치 = [qa]·[insight].
- 쓰기: MEMEX entry 문법 호환 md(`## [aspect] 제목` + `project::` + `tags::` + `recorded_at::` 자체 필드). `pending-messages/`에 먼저 저장하고 `send-pending-messages.py`가 localhost MCP로 보낸 뒤 성공한 파일만 옮긴다. 실패는 작업 진행을 막지 않고 활동 로그에 남긴다.
- 읽기: MCP 실가동 확인(D19)으로 search_* soft 직결 조기 개방. 호출 대상 127.0.0.1:8080/mcp, 타임아웃 2~3초, 실패 시 로컬 vault·pending messages 검색으로 전환한다.
- 금지: Transcript 원문, 스킬 본체 파일.

**진행상태 미러 (D4 개정)**
- activity-log-writer 확장: Kanban 이벤트를 activity log에 쓸 때 같은 이벤트를 MEMEX 진행 상태 entry로도 pending messages에 적재.
- 단방향 read-only. MEMEX 쪽 진행상태 노드는 read replica, 편집해도 다음 사이클에 덮어써짐. 재개·복구는 항상 Kanban 원본.

**쿼터 규칙**: save_memex 1건 = LLM 최대 3회(Codex 구독 = 야간 노동과 같은 지갑). spec당 배치 1회로 묶음. enrichment는 PendingQueue로 주간 보류 옵션. 대량 재인제스트는 ApiKeyClient.

---

## 12. 운영 스크립트 + 알림 (D14)

전부 LLM 0. 알림 경로 이원화(D22): 대화·지시는 hermes Slack 게이트웨이, **아래 스크립트들의 알림은 Slack Web API 직발송**(curl chat.postMessage + xoxb 토큰, hermes 우회). 이유: hermes 자체가 죽었을 때도 "hermes 죽음" 알림이 도착해야 한다. 감시자가 감시 대상에 의존하면 안 됨. 메시지 접두사는 `[레포명]` + `프로젝트명::동작` 네임스페이스, Slack 레이트리밋 대비 동일 분 내 다건은 배칭.

| 스크립트 | 주기 | 역할 | 즉시 알림 조건 |
|---|---|---|---|
| fs-precheck.sh | 설치 시 | ext4·NVMe·WAL 확인, BTRFS/NFS 차단 | 부적합 = 설치 중단 |
| system-check.sh | 밤 시작 | 알려진 입력으로 work check 자체 점검 + Slack 왕복 1회(게이트웨이 응답 정지 감지) | 실패 시 배차 중단 + 알림 |
| issue-status-sync.py | 60초 (ETag) | 수입: 신규 이슈→triage 카드, needs-decision 해결·코멘트→unblock. 표시: 카드→forge:* 라벨, 시크릿 제거 | GitHub 5xx·토큰 만료 연속 N회, pending messages 적체 |
| activity-log-writer.py | 10분 | Kanban 이벤트 → activity.jsonl(원자 append) + MEMEX 진행 상태 entry | event ID 증가 위반 |
| nightly-backup.sh | 04:30 | state.db·kanban.db .backup + integrity_check (Phase 0~1 임시). Phase 2에서 Litestream 연속 복제로 승격(D20): OVH Object Storage 대상, 복제 지표 감시, 주간 복원 리허설, OVH 스냅샷 주 1회 병행 | 체크 실패·누락, 복제 지연 |
| spec-coverage.sh | 밤 시작 + 07:30 | 기획서 체크리스트(안정 ID: SPEC-NNN 부여 전제) ↔ 이슈 존재·close 대조 → 미대응 목록을 task-service 재투입 큐에 전달, 리포트에 "coverage N/M" 표기. 검증 불가능 문장은 forge:needs-decision 분류 | coverage가 전일 대비 감소 시 |
| state-mismatch-check.sh | 60분 | 불변식 2개 대조, ready 장기 체류, protocol_violation, CHECK_ERROR 비율, 백업 신선도, 이슈 본문 편집 이벤트(D17), 자기 하트비트 | 전 항목 + 임계 초과 |
| morning-report.sh | 07:30 | merged/failed/needs-decision 대기 집계 코멘트 (gh api, LLM 0) | |

**즉시 알림 대상 (D14)**: forge:needs-decision / forge:failed / forge:ready-to-merge 신규 + system check 실패 / CHECK_ERROR 임계 / 백업 무결성 실패 / pending messages 적체. 기계 전이(`building` 등)는 제외.

---

## 13. 운영 타임라인

```
저녁  사람 + 대화형 Claude: 다음 밤 spec 작성·투입, 현재 정책 manual 확인(safe_auto/full_auto는 미구현)
21:00 system check → 통과 시 spec-coverage 감사(미대응 항목 → task-service 재투입) → 배차 시작
21:05 task-service → 이슈 생성 → issue status sync → 승격/needs-decision 분기 (사람 결정 필요 시 즉시 알림)
야간  Build(codex exec) → PR → Review(GPT 새 세션) → Deep Check(codex exec 별도 세션)
      → ready-to-merge(즉시 알림) → manual 사람이 HEAD·CI·원본 이슈 라벨 확인 후 머지 → 방출
      실패 시 L0→L1(반성문, 최대 3회)→L2(forge:failed, 즉시 알림)→L3(나머지 계속)
04:30 백업 + 무결성 검증
07:30 아침 리포트 (상단 고정: spec coverage N/M + needs-decision·failed·ready-to-merge 집계)
아침  사람 + 대화형 Claude 부조종사:
      ready-to-merge PR 공동 리뷰(교차 벤더 층) → 머지
      needs-decision 안건 논의 → 코멘트 + 라벨 해제
      failed 힌트 코멘트 → 라벨 되돌림(다음 밤 재시도에 주입)
```

---

## 14. 로드맵

### Phase 0: VPS 점화 런북 (반나절. 신규 세션은 여기부터 순서대로 실행)

**STEP 1: 기초 공사 (30분)**
- [ ] SSH 접속 후 실측 → 2.0절 표에 기입: `free -h && nproc && df -T / && cat /etc/os-release && docker stats --no-stream`
- [ ] 규율 1 확인: `mount | grep -i nfs` 출력 없음, `df -T /`가 ext4
- [ ] 도구: `sudo apt update && sudo apt install -y git tmux sqlite3 ufw fail2ban`
- [ ] 방화벽(D21): `sudo ufw default deny incoming && sudo ufw allow 22/tcp && sudo ufw enable`
- [ ] MEMEX 바인딩 하향(D19): compose ports를 `"127.0.0.1:8080:8080"`으로 변경 → `docker compose up -d` → `sudo ss -tlnp | grep 8080`에서 127.0.0.1 확인
- [ ] SSH 하드닝(D21): 기존 세션 유지한 채 새 터미널에서 키 접속 검증 → 성공 시에만 sshd_config에 PasswordAuthentication no → sshd 재시작 → 재접속 재확인

**STEP 2: hermes 설치·상주 (30분)**
- [ ] 공식 설치 스크립트 실행, `hermes --version` 기록 = 버전 핀(월 1회 창에서만 상향)
- [ ] `hermes doctor` 통과
- [ ] 온보딩 선택: 프로바이더 = OpenAI(GPT-5.5)만, **Anthropic 미연결(D11)** / Slack: `hermes slack manifest --write`로 매니페스트 생성 → api.slack.com/apps에서 앱 생성(THE_FORGE 리스너 병행 시 별도 앱) → Socket Mode 켜고 xapp 토큰 + 설치 후 xoxb 토큰 → .env에 SLACK_BOT_TOKEN·SLACK_APP_TOKEN·SLACK_ALLOWED_USERS(내 Member ID)·홈채널(#forge-ops) / 대시보드 127.0.0.1:9119 기본값 유지(접근은 `ssh -L 9119:127.0.0.1:9119`)
- [ ] 채널 준비: #forge-ops + 레포당 채널(#forge-memex 등) 생성, 각 채널에 봇 /invite (미초대 시 채널 메시지 못 봄)
- [ ] 검증: 폰 Slack 앱에서 봇 DM 왕복 1회 + 채널 멘션 왕복 1회(DM만 되고 채널이 침묵하면 *:history 스코프 누락 + 재설치 필요) + systemd 상주 확인 + VPS 재부팅 후 자동 복귀 확인

**STEP 3: 워커 도구 체인 (30분)**
- [ ] Codex CLI: `npm i -g @openai/codex`, 로컬 노트북의 ~/.codex/auth.json을 scp로 이식(chmod 600), `codex exec` 스모크 1회 + 쿼터 차감 위치 확인
- [ ] `gh auth login` + `gh auth setup-git`: fine-grained PAT (**대상 3레포 전부 등록**, issues:rw / pull_requests:rw / contents:rw, 만료일 설정. 하나라도 누락 시 해당 레포만 push 403)
- [ ] 워크스페이스 배치(D24): ~/work/<제품>/ 아래 3레포 최초 clone + 워크스페이스 AGENTS.md 작성. 각 레포에 docs/adr/, fix_notes/, pending-messages/ 골격 + forge:* 라벨 9종 멱등 생성 (`gh label create "forge:$L" || true` 루프, 3레포 각각)
- [ ] (선택) Agent SDK 크레딧 상태 1회 확인, upstream GitHub 브리지 상태 재확인(#31992·#19932: 구현됐으면 자작 미러 계획 축소)

**STEP 4: Kanban 점화 (30분)**
- [ ] `hermes kanban init` → ~/.hermes/kanban.db 생성 확인
- [ ] 프로필 4종 생성: 5절 표의 description 문구 그대로 사용(디컴포저 라우팅 근거). 정확한 문법은 설치 버전의 `hermes profile --help` 기준으로 확인
- [ ] 카드 1장 수동 생성 → specify → 디스패처가 워커를 스폰하고 ~/.hermes/kanban/logs/에 로그가 쌓이는지 확인

**Phase 0 완료 판정**: 텔레그램 왕복 / 재부팅 생존 / codex exec 스모크 / `ss`에서 8080 = 127.0.0.1 / kanban 카드 1장 / 실측치가 2.0절 표에 기입됨

> **판정 결과 (2026-07-10): Phase 0 완료.** Slack 왕복(#forge-cloud 아웃바운드+인바운드, 텔레그램→Slack은 D22) ✓ / 재부팅 생존(gateway·MEMEX 4컨테이너·백업타이머 자동복귀) ✓ / codex exec 스모크(CODEX_OK, 1,822tok) ✓ / kanban 카드 1장 20초 완주(t_eb52e76a) ✓ / 실측치 2.0절 기입 ✓ / 8080은 127.0.0.1 하향 대신 공개 유지(사용자 결정, 로컬 원격 접근 유지 — 하향 재검토 트리거는 로컬 SSH 터널 전환 시).
> 추가 완료(계획 외): 로컬 Windows 동급 메인 세팅(@forgelocal, D18 개정 성격 — 로컬은 관제석이 아니라 제2 게이트웨이), MCP 3종 양쪽 이식, 규약 스킬 2종(forge-ops·memex), nightly 백업(D20 임시판), 대시보드 9119 상주(127.0.0.1)+Desktop 원격 준비, Zscaler CA 번들 해결(로컬).

### Phase 1: 파이프 수동 검증 (2~3일)
- [ ] worker 스킬 4종 + memex 스킬(pending messages 쓰기) + Codex 훅(tdd-cycle·wiki-check·work check·예산 cap·잔여 작업 등록 D17)
- [x] Build·Review·Deep Check·Fix strict JSON 결과 + 이슈 템플릿(수용 기준 필수) + Review 변경 대조 임무
- [ ] 이슈 1건 수동 투입 → Manual decompose → e2e 1왕복
- [ ] **태스크 1건당 Codex 쿼터 소모 실측** → 밤당 spec 수 역산 (재시도 3회 = 최악 4세션 반영)
- 검증: 이슈→카드→codex exec→PR→Review→Deep Check→병합 1회 완주, protocol_violation 0, Build 결과에 PR URL·base/head commit·changed_files 존재

### Phase 2: 무인화 (3~5일)
- [ ] 스크립트 8종 가동 (system check, spec-coverage 포함)
- [ ] needs-decision 왕복 리허설 (알림 → 폰 코멘트 → 라벨 해제 → 수입)
- [ ] 야간 dry-run 3회
- 검증: 중복 카드 0, state mismatch 0, 백업 integrity 통과, GitHub 차단 모의 시 밤 완주 + 아침 따라잡기, CHECK_ERROR 0, 고아 잔여 0, coverage N/M 표기 정상

### Phase 3: 확장
- [x] Auto decompose 전환 (오케스트레이터 toolset 보드 연산 제한)
- [x] capability eval 주간 잡 (VM 이전은 D18로 Phase 0에 흡수, MEMEX 조회 개방은 D19로 조기 완료)
- [ ] (옵션) Review를 codex exec로 이관 (필요 시)

> **판정 결과 (2026-07-12): Phase 3 필수 2건 반영.**
> ① Auto decompose: hermes 기본값이 이미 True라 실질 가동 중이었음(Phase 1의 unblock 시 auto-specify 관찰이 그 증거) → 암묵 기본값을 명시 설정으로 고정: `kanban.auto_decompose=true`, `auto_decompose_per_tick=3` (VPS+로컬 양쪽 config.yaml, 게이트웨이는 틱마다 재독취라 재시작 불요). "오케스트레이터 toolset 보드 연산 제한"은 구조 충족 — 디컴포저는 도구 없는 보조 LLM 호출(chat.completions, JSON 산출만)이고 보드 쓰기는 결정론 코드가 수행(도구 0 = 요구보다 강한 제약). 16절 노브의 스테일 회수 2h 하향(`dispatch_stale_timeout_seconds=7200`)도 이때 함께 명시 적용.
> ② capability eval 주간 작업: `.github/workflows/capability-eval.yml` — 매주 월 07:00 KST, 결정론만(pytest 회귀 + Bash/Python 문법 + 스킬 SKILL.md 계약 + spec-registry 형식). 7절 "CI에 LLM 금지" 준수, LLM 단계 점검은 VPS system check(6시간 주기)가 담당.
> ③ Review→codex exec 이관은 옵션 조항으로 미실행(현 GPT-5.5 Review 정상 동작, 필요 발생 시 재검토).

---

## 15. 리스크와 불확실성

| 리스크 | 추정 | 대응 |
|---|---|---|
| **단일 벤더 집중**: 밤 전체가 OpenAI 의존. Anthropic식 3단계 재가격화·Google Gemini CLI 제재 전례로 업계 방향이 조임세 | 중 | 엔진 추상화 유지(래퍼가 CLI 교체 가능), 4~6주 정책 재점검, hermes 로컬 모델(Ollama 등) 경로를 비상 탈출구로 표기 |
| **Codex 쿼터 집중**: 밤 노동 + MEMEX enrichment 같은 지갑, 재시도 3회로 최악 4세션 | 중 | Phase 1 실측 필수, spec당 memex 배치 1회, enrichment 주간 보류. 태스크 예산 캡은 향후 구현 |
| *(실측 2026-07-10, SPEC-001 e2e)* 태스크 1건 codex 소모 = **41,460 tokens** (README 작성 수준의 소형 문서 작업 기준. 코드+테스트 작업은 수배 예상. hermes 래퍼 gpt-5.5 소모는 별도. 쿼터 윈도우 규칙은 미확인이라 밤당 spec 수 역산은 코드 작업 실측 후) | - | - |
| Kanban 성숙도 (병합 2개월여) | 중 | 규율 2 엄수, 첫 2주 방출 일 2회 |
| 이중 상태 드리프트 | 중 | 단일 작성자(D7) + state-mismatch-check + 라벨 최소주의 |
| 래퍼의 verdict 오독 | 중 | verdict JSON 스키마 강제 + 스크립트 파싱 |
| 쓰레기 후속 카드 양산 (D17이 카드 생성을 강제하므로) | 중 | 후속 이슈도 AC 필수 스키마 체크, 멱등키 중복 접기, triage 필터. 원칙: 고아(안 보임)보다 쓰레기(보임)가 낫다 |
| 남은 항목 누락 | 중 | 이중 방어: Review의 spec 대비 변경 대조(Task 층) + spec coverage check(spec 층, 밤 단위 회수) |
| GitHub 장애 밤 | 저 | Kanban 로컬 지속 + pending messages 따라잡기 |
| 단일 박스 폭발반경 (hermes+MEMEX 동거) | 중 | Litestream + GitHub push + OVH 스냅샷 3중(D20), MEMEX soft-fail 불변, Neo4j에 docker 메모리 상한 |
| 미확인: 디스패처 직접 생성 카드의 미지 assignee 처리, hermes OpenAI 구독 인증 설정 상세, codex exec 쿼터 윈도우 규칙·태스크당 소모량, VPS vCPU·OS·디스크 타입, 8080 세계 개방 여부(핫스팟 curl 미실시), 대상 3레포의 회사 자산 여부(정책 확인 선행) | - | Phase 0~1 실측 (17절 목록) |

---

## 16. 운영 노브 (기본값 가동, 변경 가능)

| 노브 | 기본값 | 비고 |
|---|---|---|
| needs-decision 무응답 처리 | 강등 없음, 3일 경과 시 아침 리포트 상단 경고 고정 | 사람 큐는 사람이 비운다 |
| 보드 분할 | 제품당 보드 1개 (D24: 워크스페이스 = 보드 단위, 카드에 대상 레포 필드) | 단일 레포 제품이면 레포당 1개와 자연히 동일. Slack 채널도 #forge-<제품> 1개 |
| auto-decompose | Phase 2까지 Manual, Phase 3에 Auto | 초기엔 분해 품질 관찰 우선 |
| 미러 폴링 간격 | 60초 (ETag 조건부) | 한도 여유 크면 30초 |
| 스테일 회수 타임아웃 | 2h (기본 4h에서 하향) | 야간 기준 |

---

## 17. 신규 세션 시작 지침 (인수인계)

**읽는 순서**: 0절(하드 제약) → 1절(D1~D21 결정) → 2.0절(환경 사실) → 14절 Phase 0 런북 실행 → Phase 1.

**현재 핵심 제작물**:
1. `build-task`, `review-task`, `deep-check`, `fix-task` 스킬: 각 역할은 strict JSON 결과를 제출하고 현재 PR commit에 결과를 묶는다.
2. Codex work check: 저장소별 테스트와 빈 diff를 검사하고 모든 오류 경로를 exit 2로 끝낸다. stderr 접두사는 TESTS_FAILED(재시도 대상)와 CHECK_ERROR(시스템 문제)로 구분한다.
3. 세 실제 worker: `task-flow-worker.py`는 Task 설정·GitHub 이슈·Hermes 결과를 연결해 다음 카드를 한 장만 만들고, `issue-status-sync.py`는 공식 Forge 라벨 하나만 남기며, `merge-worker.py`는 최신 GitHub 증거와 Task 설정을 재검증한다. 누락·불일치·읽기 오류는 코드 `2`로 끝난다.

**production 전환 전 실제 서버 확인**: 작은 Task 1건을 **Build + Review + Deep Check + Manual**로 선택 → Build → Review → Deep Check → 원본 이슈 `forge:ready-to-merge` → 현재 PR HEAD의 `eval=success`와 base/head 확인 → 사람이 병합한다. Fix 요청, 실패한 CI의 병합 거부, branch 갱신, Safe Files Auto-Merge 거부 사례도 별도로 확인한다. 이 절차를 수행하기 전까지는 저장소 구현 완료와 production 반영을 같은 상태로 표현하지 않는다.

**절대 금지 3개**: hermes에 Anthropic OAuth 연결(D11) / MEMEX·대시보드의 공개 바인딩(D19·D21) / API 키·토큰 원문의 문서·레포·채팅 기재.

**미실측 잔여 (Phase 0~1에서 채울 것)**: VPS vCPU·OS·디스크 타입(→ 2.0절 표), 8080 세계 개방 여부(로컬이 아닌 망에서 curl 1회: 타임아웃이면 방화벽 정상), codex exec 쿼터 윈도우 규칙과 태스크당 소모량, hermes 프로필 생성 문법(설치 버전 --help).

**문서 갱신 규칙**: 결정 변경은 D번호 추가로만(기존 D 소급 수정 금지), 실측치는 2.0절 표에 기입, 구현 완료 판정은 7절의 기계적 정의(coverage M/M ∧ 전 이슈 close ∧ 전 check 통과)를 따른다.

## 변경 이력

- 2026-07-16 | 쉬운 운영 이름과 실제 worker 연결 | Chat/Task, 세 실행 단계, 세 병합 방식, Build·Review·Deep Check·Fix 프로필, 공통 writer lock을 연결했다. 세 worker는 실제 DB·GitHub·Hermes 상태를 읽고 쓰며 자동 병합은 기본 off다. | 검증: Task runtime, Issue Status Sync, Merge Worker의 정상·불일치·중복·오류 계약 테스트
- 2026-07-15 | Task flow 구현 반영 | 루트 카드 1개와 완료 결과별 자식 카드 구조, 선택한 검사 자동 연결, Fix, 최신 HEAD CI check와 세 병합 방식을 명시 | 검증: 상태 전이·GitHub 읽기·라벨·worker 계약 회귀 테스트 및 독립 리뷰
- 2026-07-15 | strict HEAD 복구 완성 | branch 갱신으로 결과 HEAD가 낡으면 Build부터 선택한 검사를 다시 실행하고, 수정 가능한 `eval` 실패는 같은 PR Fix로 보내며, 자동 수정 의미가 불명확한 결론은 CHECK_ERROR로 종료 | 검증: 전체 `244 passed, 2 skipped`; 독립 리뷰 PASS; Task Flow Worker·Issue Status Sync 회귀 검증
- 2026-07-15 | PR #6 Linux CI import 복구 | 전역 `pytest` entrypoint가 저장소 루트의 `forge` package를 import하지 못한 수집 오류를 `python -m pytest`로 교정하고 workflow 계약 테스트를 추가 | 검증: workflow contract 4 passed, `tests/ops` 214 passed, 전체 `244 passed, 2 skipped`
- 2026-07-15 | GitHub main ruleset 활성화 | `protect-main` ID `18974841`에 PR 필수, approvals 0, strict GitHub Actions `eval`, force-push·deletion 차단, bypass 없음 적용 | 검증: effective main rules read-back; `eval` queued에서 PR #6 `BLOCKED`, success에서 `MERGEABLE`; manual 사람 merge 전 VPS 배포 보류
- 2026-07-15 | 부트스트랩 manual 경계 명시 | Task flow worker를 처음 배포하는 PR #6은 병합 전에 자기 pipeline 라벨을 생성할 수 없어 green current HEAD·독립 리뷰·active ruleset을 일회성 manual 증거로 사용하고, 배포 후 일반 PR부터 원본 이슈 `forge:ready-to-merge`을 필수화 | 검증: 순환 의존(merge→deploy→label)을 운영 런북과 대조
