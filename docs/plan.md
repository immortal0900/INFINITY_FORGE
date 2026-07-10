# INFINITY_FORGE 로컬 hermes 운용 기획서 v1.3

> spec 여러 개를 던지면, hermes Kanban이 분해·배차·재시도를 맡고, 야간 노동은 전량 OpenAI 쪽(codex exec + GPT-5.5)이 세션 분리로 수행·검증하며, 사람은 아침에 GitHub에서 코멘트·머지만 하고, 그 아침 검수대에 대화형 Claude Code가 부조종사로 선다.
> hermes는 코어 무수정(표층 오버레이). 문서화된 약점 전부에 외부 극복 장치를 얹는다.
>
> 작성: 2026-07-09 (v1: 07-07 / v1.1·v1.2: 07-08) / 전제 문서: INFINITY_FORGE SoT, memex_knowledge_system_design_v3.md, 2026-07-07 hermes 재조사 로그, 2026-07-08 Anthropic 정책·VPS 실사용 조사
> 상태: 실행 단계 진입. 이 문서는 신규 세션 인수인계용으로 자족적이다. 신규 세션은 0절(제약) → 1절(결정) → 2.0절(환경) → 14절 Phase 0 런북 → 17절(시작 지침) 순서로 읽고 실행할 것.

## v1.2 → v1.3 변경 요약

1. **실행 환경 확정 (D18)**: OVH VPS 1대(RAM 8GB 사용자 확인)에 hermes + Kanban + 워커 + MEMEX 동거(배치 1안). 근거: 로컬 Windows의 WSL2 24/7 취약성 소거 + MEMEX outbox의 localhost 배달. 기존 Phase 3의 "VM 이전" 항목 소멸(Phase 0에 흡수).
2. **MEMEX MCP 실가동 = Wave 0 탈출 (D19)**: save_memex 동작 확인(2026-07-08). 바인딩을 127.0.0.1로 하향(공개 표면 0), 원격 접근은 SSH 터널만. search_* soft 조회 조기 개방, outbox flush의 실배달 개시.
3. **백업 승격 (D20) + 보안 하드닝 세트 (D21)**: Litestream 연속 복제(Phase 2 승격), Telegram polling(인바운드 0), UFW·키온리 SSH·fail2ban·대시보드 localhost.
4. Phase 0을 명령 단위 런북으로 교체, 인수인계 절(2.0절 환경 정보, 17절 시작 지침) 신설. 이 문서 단독으로 다른 세션이 실행 가능하도록 자족화.
5. (07-09 추가) **게이트웨이 Slack 전환 (D22)**: Telegram 사용 불가에 따라 Slack Socket Mode로 교체. 인바운드 0 원칙 불변, THE_FORGE의 검증된 패턴(네임스페이스·명령 어휘·셋업 문서) 재사용, 알림은 hermes 우회 직발송으로 이원화.
6. (07-09 추가) 슬래시 명령은 hermes 기본셋으로 확정(D23, THE_FORGE 어휘 이식 취소), 멀티레포 제품 워크스페이스 규약(D24): 보드 = 제품당 1개, 교차 작업은 이슈 1장·PR 3개·전부 green일 때만 mergeable·제공자 먼저 머지.

## v1.1 → v1.2 변경 요약

1. **완료 판정 체계 신설 (D16·D17)**: "구현 완료"의 판정 주체를 모델의 산문에서 기계 술어로 이전. spec-coverage 감사(기획서 항목 ↔ 이슈 대조)로 "기획 100% 구현"을 숫자로 정의하고, 핸드오프에 implemented / not_implemented / verified_by 3필드를 강제하며, 잔여 물질화 게이트(not_implemented 항목은 이슈 ID 없이는 종료 불가)로 고아 작업(어느 큐에도 등록되지 않은 잔여 일감)을 원천 봉쇄.
2. **수용 기준(AC) 고정 원칙**: 카드 생성 시 확정, 워커는 이슈 본문 수정 금지(코멘트만), 변경은 forge:adr 경유. 본문 편집 이벤트는 drift-audit 감시 대상.
3. 배경: Claude Code 실사용에서 관찰된 두 실패 모드의 구조적 방어. (a) 과대 완료 선언(검증 없는 "완료했습니다"), (b) 자의적 phase 분해 후 후반 phase를 산문에만 남기고 종료(분해권·완료정의권·종료권의 한 세션 독점).

## v1 → v1.1 변경 요약

1. **야간 아키텍처 전환 (D15)**: Anthropic 정책 조사 결과(D11), Claude를 무인 야간 워커로 쓰는 합법 정액 경로가 없음 → 야간 노동 전량 OpenAI, Claude는 아침 부조종사로 이동. 교차 벤더 검증은 폐지가 아니라 밤→아침 시점 이동.
2. 라벨 접두사 `forge:` 확정 (D12), 재시도 3회 (D13), 알림 전부 즉시 (D14).
3. 머지 정책 P1/P2/P3 + 태스크 오버라이드 (D8), 게이트 fail-closed 배치 원칙 (D9), 실패 에스컬레이션 사다리 (D10) 신설.
4. MEMEX에 진행상태 read-only 단방향 미러 허용 (D4 개정).
5. 컴플라이언스 절 전면 개정: 1차 출처 기반.

---

## 0. 목적, 범위, 하드 제약

**목적**: AI 위임 업무 품질을 POC 수준으로. DDD → SDD → TDD 믹싱 전제. 야간 무인 실행, 아침 인간 검토는 GitHub 코멘트·머지만으로 완결.

**범위**: OVH VPS 1대(hermes + Kanban + 워커 + MEMEX 동거)에서의 운용. 로컬 Windows 노트북은 아침 부조종사(대화형 Claude Code)와 SSH 관제석 역할만. MEMEX 자체 구현은 별도 기획서(v3).

**하드 제약 (우선순위 순)**
1. 구독 컴플라이언스: Anthropic 구독 OAuth를 hermes 프로바이더에 직결 금지(공식 문서 확정). Agent SDK 크레딧 미사용 방침에 따라 야간 파이프라인에서 Claude 프로그래매틱 경로(`claude -p`) 제외. tmux 대화형 우회 봉인(계정 리스크). hermes 자체 LLM과 codex exec는 OpenAI 구독(서드파티 허용 확인).
2. 성능 최우선: 야간 처리량은 OpenAI 쿼터가 상한. Claude는 인간 경계에서 품질 기여.
3. hermes 코어 무수정: 스킬·설정·주변 스크립트만. 버전 핀 + 월 1회 업그레이드 창.
4. 데이터 2층 분리: 진행상태(저장 실패 = 밤 유실)는 로컬 원자 쓰기, 지식(저장 실패 = 지연)은 파일 → MEMEX 비동기.

---

## 1. 확정 결정 요약 (D1~D15 누적)

| # | 결정 | 근거 | 일자 |
|---|---|---|---|
| D1 (개정) | 야간 노동 전량 OpenAI: codex exec = 구현·터미널, GPT-5.5 = 오케스트레이션·리뷰·밤 critic (전부 세션 분리). Claude = 인간 경계 전담: spec 작성 동행, ADR 논의, 아침 mergeable 리뷰 부조종사 (전부 대화형 = 구독 정액 풀 = 통상 사용) | D11 정책 제약 + 자기채점 2층 방어(세션 분리는 컨텍스트 오염만, 모델 맹점은 교차로) | 07-08 |
| D2 | hermes Kanban = 진행상태 원장. 자작 디스패처 계획 축소(수십 줄) | Kanban이 원장·원자 클레임·재시도 이력·하트비트·크래시 회수·서킷 브레이커를 코어 1급 제공 | 07-07 |
| D3 | 운영 규율 4종을 채택 조건으로 | kanban.db도 SQLite라 state.db와 같은 계급의 실패 실증 | 07-07 |
| D4 (개정) | MEMEX = 지식 증폭기, 비동기 전용. 진행상태의 1차 저장소는 Kanban(로컬)이며, MEMEX엔 단방향 read-only 미러 허용. 재개·복구는 항상 Kanban 원본 기준. 역방향 쓰기 금지(다음 미러 사이클에 덮어써짐) | 쓰기 보장 순환 문제(복구 정보가 장애 때문에 저장 안 되는 구조) 회피 + 그래프 질의 편익 확보 | 07-08 |
| D5 | memex 사용 스킬 이름 = `memex` | 사용자 확정. MCP 서버명과 동일 무방(레지스트리 상이) | 07-07 |
| D6 | GitHub 층 = 투영 + 인간 창구 (토폴로지 B). GitHub Issues를 내부 큐로 삼지 않음 | upstream RFC #19932 동일 경계 독립 수렴, 인간 피드백 분산 고통 실증 #47423 | 07-07 |
| D7 | 라벨 = 게시판. 기계 전이는 미러 스크립트 단독 작성. 클레임은 Kanban 원자 트랜잭션 | 라벨 API CAS 부재, TOCTOU 원천 제거 | 07-07 |
| D8 | 머지: 기본 사람 승인(P1). 세션 시작 시 P1/P2/P3 선택 = 명시적 auth 행위. 태스크별 `forge:automerge-ok` 오버라이드. 미선언 시 P1로 fail-safe. P2 위험도는 파일 경로·diff 패턴 스크립트 판정(LLM 0) | 검증(기계)과 결정(사람)의 분리 + "요구 오해" 클래스는 검증이 못 잡음 | 07-08 |
| D9 | 결정론 차단은 hermes 훅에 두지 않는다: hermes 훅은 잘못된 JSON·non-zero exit·타임아웃 시 경고만 남기고 루프를 계속(fail-open). 차단은 워커 CLI 훅(exit 2, fail-closed) + GitHub Actions. hermes 훅은 관찰·로깅·컨텍스트 주입·memex 미러 전용 | hermes 훅 계약 문서 확인 | 07-08 |
| D10 | 실패 에스컬레이션 사다리 L0~L3 + 야간 시작 카나리아 + TESTS_FAILED/GATE_ERROR 신호 구분 + 게이트 스크립트 규율(모든 에러 경로 → exit 2) | fail-closed의 단위는 태스크 1개, 전체 정지 방지 | 07-08 |
| D11 | Anthropic 정책(1차 출처 확정): 구독 OAuth의 서드파티 직결 = 금지(2월 약관, 4/4 시행). 공식 `claude` CLI 스폰 = 허용(4월 중순 확인). 단 6/15부터 `claude -p`·Agent SDK는 월 $200 크레딧에서 API 정가 차감. 본 시스템은 크레딧 미사용 방침 → 야간에서 Claude 프로그래매틱 제외. 대화형(터미널·웹·Cowork)만 구독 정액 잔존 | 2026-07-08 조사 (code.claude.com/docs/en/legal-and-compliance + 4~6월 보도 종합) | 07-08 |
| D12 | 라벨 접두사 = `forge:` | 사용자 확정 | 07-08 |
| D13 | 재시도 N = 3: 최초 시도 1 + 새 세션 이어받기 최대 3 = 태스크당 최대 4세션 후 서킷 브레이커. GATE_ERROR는 카운트 제외 | 사용자 확정 | 07-08 |
| D14 | 알림 전부 즉시: 인간 액션 대상 전이(forge:adr / forge:failed / forge:mergeable 신규) + 시스템 이상(카나리아 실패, GATE_ERROR 임계, 백업 무결성, outbox 적체). 기계 전이는 제외. 아침 07:30 집계 리포트 병행 | 사용자 확정 (취침 중 무음 운용) | 07-08 |
| D15 | 야간 아키텍처 = 1안: 야간 OpenAI 단독 + 아침 Claude 부조종사. 자기채점 절충 흡수: 밤 reviewer·critic = 같은 벤더 세션 분리, 교차 벤더 층 = 아침 Claude | 사용자 확정 | 07-08 |
| D16 | spec 커버리지 감사: 기획서 체크리스트 항목 ↔ 대응 이슈(멱등키) 존재·close 여부를 스크립트(LLM 0)로 대조, 미대응 항목은 issue-finder에 재투입, 아침 리포트에 "커버리지 N/M" 고정 표기. **구현 완료의 기계적 정의 = 커버리지 M/M ∧ 전 이슈 close ∧ 전 게이트 초록.** 핸드오프에 implemented / not_implemented(빈 배열도 명시) / verified_by 3필드 필수, 이슈 템플릿에 수용 기준 필수 | LLM의 "완료"는 사실 보고가 아니라 생성 문장. 태스크 게이트만으로는 태스크화되지 않은 spec 항목이 사각지대 | 07-08 |
| D17 | 잔여 물질화 게이트: not_implemented 각 항목은 (a) 기존 이슈 ID 참조, (b) 신규 후속 이슈 생성 후 ID 기입, (c) forge:adr 에스컬레이션 중 하나를 가져야 종료 허용. ID 부재 시 exit 2. 수용 기준은 카드 생성 시 고정, 워커의 이슈 본문 수정 금지(코멘트만), 변경은 adr 경유, 본문 편집은 감사 대상 | 분해권·완료정의권·종료권을 한 세션이 쥐면 범위 축소 후 합법적 조기 종료 가능. 잔여가 산문에만 남으면 고아화되어 증발 | 07-08 |
| D18 | 실행 환경 = OVH VPS 1대(vps-aee0e707.vps.ovh.ca / 51.222.27.48, RAM 8GB 확인)에 hermes+Kanban+워커+MEMEX 동거(배치 1안). 로컬 Windows는 아침 부조종사·SSH 관제석 | 로컬 WSL2는 절전·강제 재부팅으로 24/7 부적합. VPS 실사용 후기의 지뢰 5종(볼륨 미마운트 스킬 유실, 재시작 정책 부재, 저사양 OOM, 대시보드 노출, wedged 게이트웨이) 대응책 내장 | 07-09 |
| D19 | MEMEX MCP = 127.0.0.1 바인딩(공개 표면 0). 원격은 SSH 터널만. HTTP 평문 + Bearer의 공개 노출 금지. save_memex 실가동 확인 = Wave 0 탈출: search_* soft 조회 조기 개방 + outbox flush 실배달 | HTTP 위의 Bearer는 열쇠를 평문으로 왕복시킴. 동거(D18)로 localhost 호출이 가능해져 공개 노출의 필요 자체가 소멸 | 07-09 |
| D20 | 백업 승격: Phase 0~1은 nightly .backup 임시, Phase 2부터 Litestream 연속 복제(OVH Object Storage, S3 호환) + 복제 지표 감시 + 주간 복원 리허설 + OVH 스냅샷 주 1회 | 단일 박스 동거로 폭발반경 확대. Litestream은 침묵 동기화 실패 이력 버전대가 있어 지표 감시 필수 | 07-09 |
| D21 | 보안 하드닝 세트: Telegram polling(인바운드 0), UFW deny + 22만 허용, SSH 키온리(새 터미널 검증 후 비밀번호 폐쇄), fail2ban, 대시보드 127.0.0.1 유지 + 터널, .env 600, 키 원문 기재 금지 | 노출 게이트웨이 하이재킹 사고 클래스 방어. OVH 하드웨어 안티 DDoS 위에 호스트 방어 적층 | 07-09 |
| D22 | 게이트웨이 = Slack (Telegram 사용 불가로 D14·D21의 채널 부분 대체). hermes Slack 게이트웨이는 Socket Mode(아웃바운드 WebSocket, 공개 엔드포인트 불필요)가 기본이라 인바운드 0 원칙 유지. 토큰 2종(xoxb 봇 + xapp 앱레벨) ~/.hermes/.env(600). 알림 이원화: 대화·지시 = hermes 게이트웨이, D14 즉시 알림·아침 리포트 = 스크립트가 Slack Web API 직발송(xoxb, hermes 우회: hermes가 죽어도 부고 도착). 채널 = 레포당 1개 + #forge-ops(시스템·홈채널). THE_FORGE의 `프로젝트명::동작` 네임스페이스·명령 어휘 재사용, 상세 셋업은 THE_FORGE 레포 문서 참조. 승인(adr 해제·머지)은 여전히 GitHub만(D7 단일 작성자) | 사용자 환경 Telegram 불가. THE_FORGE가 동일 패턴(Socket Mode HITL) 검증 완료. 주의: 같은 xapp 토큰으로 소켓을 두 프로세스가 열면 이벤트가 예측 불가하게 분산되므로, THE_FORGE 리스너 병행 시 hermes용 Slack 앱 분리 필수(발신용 xoxb 재사용은 무충돌) | 07-09 |
| D23 | Slack 슬래시 명령 = hermes 기본셋(/stop, /model 등 매니페스트 기본) 사용. THE_FORGE 어휘(/resume·/skip·/revise) 이식 취소(D22의 해당 부분 대체). 네임스페이스 접두사 `프로젝트명::동작`은 유지 | 사용자 확정. 승인·반려의 본선은 GitHub 라벨이므로 Slack 명령은 보조 어휘로 충분 | 07-09 |
| D24 | 멀티레포 제품 워크스페이스: 대상 3레포(front-end·backend·workflow-engine)를 ~/work/<제품>/ 아래 나란히 클론, 워커 cwd = 부모 폴더, 레포 간 관계·빌드·교차 규칙은 워크스페이스 AGENTS.md에 기술. PAT에 3레포 전부 등록 + `gh auth setup-git`. 보드 = 제품당 1개(카드에 대상 레포 필드). 교차 작업 규약: 이슈 1장(계약 소유 주 레포)·카드 1장·PR 3개 상호 링크·mergeable 판정은 연결 PR 전부 green일 때만·제공자 레포 먼저 머지 + 확장-수축(expand-contract) 작성·교차 레포 변경은 P2 위험 분류상 자동 머지 금지 | 세 레포는 함께 배포되고 함께 깨지는 한 제품 = 한 격리 단위. git은 레포별 독립 remote·push가 기본이라 기술 장벽 없음. 찢어진 카드 3장은 원자성 수작업 의존이라 기각 | 07-09 |

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
| kanban.db | 운영 SoT: 카드·의존·런 이력·핸드오프 | 로컬 SQLite (ext4 + NVMe 강제) |
| 워커 프로필 4종 | issue-finder / executor / reviewer / critic (전부 OpenAI 쪽) | 디스패처가 태스크마다 스폰 |
| codex exec 서브프로세스 | 실제 구현·수정·터미널 작업 (Codex 구독) | executor 래퍼가 tmux로 스폰 |
| 대화형 Claude Code | 아침 부조종사: mergeable 리뷰, ADR 논의, spec 작성 동행 | 로컬 Windows에서 사람이 직접 켬 (SSH로 VPS에서도 가능·정책 허용, 자동 스폰 금지) |
| GitHub (Issues·Labels·PR·Actions) | 인간 창구, 상태 투영, 결정론 CI, 인테이크 | 원격 |
| 미러·방출·감사 스크립트 | LLM 0 순수 스크립트 (12절) | systemd timer / cron |
| repo 파일층 | docs/adr/, reflections/, skills/, ledger.jsonl, outbox/ | git 커밋 대상 |
| MEMEX | 지식 증폭기 + 진행상태 read-only 미러 수신 | 같은 VPS 동거, 127.0.0.1:8080/mcp, soft 의존 불변 |

### 2.2 흐름 (1 spec의 일생)

```
spec 투입(사람 or issue-finder)
  → GitHub 이슈 [forge:spec-draft]
  → 미러: Kanban triage 카드 (멱등키 github-issue:OWNER/REPO#N)
  → 승격: SoT 근거 인용 가능 → [forge:need-execution]
          신규 스코프 → 카드 block(adr) [forge:adr] (즉시 알림, 그 건만 정지)
  → 디스패처 원자 클레임 → executor 래퍼 [forge:in-progress]
      └ tmux로 codex exec 스폰 (tdd-cycle·wiki-gate = AGENTS.md + Codex 훅)
      └ Codex Stop 훅(exit 2): 테스트·린트·빈 diff·태스크 예산 + 잔여 물질화(D17) 통과 시에만 종료
      └ 래퍼: kanban_heartbeat 유지 → kanban_complete(핸드오프: PR URL, changed_files, decisions,
         implemented / not_implemented(+이슈 ID) / verified_by)
  → PR + Actions CI(결정론만) [forge:need-review]
  → reviewer(GPT-5.5 새 세션) → 통과: [forge:need-critic] / 반려: 반성문 코멘트 + 재큐(D13: 최대 3회)
  → critic(codex exec 적대 모드, 별도 세션) → 엣지 테스트 추가 → CI green [forge:mergeable] (즉시 알림)
  → 머지: P1 사람 / P2 위험도 분기 / P3 자동 (세션 시작 시 선택)
  → 방출: ledger.jsonl 원자 기록 + memex outbox entry + 이슈 close(GitHub 네이티브)
아침 → 사람 + 대화형 Claude 부조종사: mergeable 리뷰(교차 벤더 층), adr 결정, failed 힌트
매밤·매아침 → spec-coverage 감사(D16): 기획서 ↔ 이슈 대조, 미대응 항목은 issue-finder 재투입
```

---

## 3. 상태 소유권 (SSoT 필드 분할)

| 필드 | 단일 작성자 | 비고 |
|---|---|---|
| 태스크 큐 상태 (triage/todo/ready/running/blocked/done) | Kanban 디스패처 + 워커 kanban_* 도구 | 미러는 읽기만 |
| GitHub 상태 라벨 (forge:*) | 미러 스크립트 단독 | 예외: 인간 전용 전이 2건 |
| 인간 전용 전이 | 사람: forge:adr 라벨 제거(결정 완료 신호), PR 머지 | 미러가 감지해 카드 unblock + 코멘트 수입 |
| 이슈·PR 코멘트 | 사람 + 워커 (대화 매체, 상태 아님) | 아웃바운드 시크릿 리댁션 필터 |
| PR·CI·merge 상태 | GitHub | Kanban은 읽기 |
| ADR·SoT 파일 (docs/adr/) | executor (인간 승인 후 커밋) | "git으로 남긴다"의 실체 |
| 이슈 본문(수용 기준 포함) | issue-finder 또는 사람이 생성 시 고정 | 이후 워커 수정 금지(코멘트만). 변경은 forge:adr 경유. 본문 편집 이벤트는 drift-audit 감시 (D17) |
| ledger.jsonl | ledger-emit (append-only) | |
| 지식 entry + 진행상태 미러 entry (outbox/) | memex 스킬(지식) + ledger-emit 확장(진행상태 미러) | MEMEX 쪽은 read replica, 역방향 금지 |

**불변식 2개 (감사 대상)**: ① 열린 이슈에 forge:* 상태 라벨 정확히 1개, ② 이슈:카드 멱등키 1:1.

---

## 4. 라벨 체계 (스킬: `forge-labels`)

상태 라벨 9종, 상호 배타. merged/closed는 GitHub 네이티브 상태 사용. 유형 라벨(Bugfix 등)은 별도 차원.

| 라벨 | 의미 | 진입 작성자 | 즉시 알림 |
|---|---|---|---|
| forge:spec-draft | triage 대기 | 미러 | |
| forge:adr | 인간 결정 대기 (그 건만 정지) | 미러 | O |
| forge:need-execution | 실행 대기 | 미러 / 사람(adr 해제) | |
| forge:in-progress | 클레임됨 | 미러 | |
| forge:need-review | PR 오픈, 리뷰 대기 | 미러 | |
| forge:need-critic | 적대 리뷰 대기 | 미러 | |
| forge:mergeable | critic + CI green | 미러 | O |
| forge:blocked | 의존·장애 (adr 외) | 미러 / 사람 | |
| forge:failed | 재시도 3회 소진 | 미러 | O |

전이: spec-draft → (adr ↔) need-execution → in-progress → need-review → need-critic → mergeable → close. 반려는 need-review → need-execution (반성문 동반). 어디서든 → blocked/failed.

설정 SSoT는 `forge-labels` 스킬 파일 + 미러 설정. 사람이 직접 라벨 달아 투입하는 경로는 미러가 흡수(원안 시나리오 보존).

---

## 5. 워커 정의 (전부 OpenAI 쪽)

디스패처 스폰: `hermes -p <profile> chat -q "work kanban task <id>"`. 파이프라인 = 부모 이슈 카드 + 역할별 자식 카드(의존 승격이 순서 보장).

| 프로필 | 엔진 | 하는 일 | 종료 게이트 |
|---|---|---|---|
| issue-finder | hermes 네이티브 GPT-5.5 | SoT 스캔 → SDD 스펙 골격 이슈 생성(gh) + 근거 인용 → triage 분류. 직행 금지: 근거 인용 없으면 forge:adr | 이슈 본문 스키마 체크(스크립트): 수용 기준(AC) 부재 시 반려 |
| executor | 저가 모델 핀 래퍼 → **codex exec** | tmux로 codex exec 스폰, 하트비트 루프, PR 오픈, kanban_complete(핸드오프 3필드 포함) | Codex Stop 훅(exit 2): 테스트·린트·빈 diff 차단 + 태스크 예산 캡 + 잔여 물질화(D17) |
| reviewer | hermes 네이티브 GPT-5.5 **새 세션** | 1차 임무: 핸드오프 델타 표(implemented / not_implemented / verified_by)를 diff·테스트와 대조. 이후 PR diff + 스펙 대조, verdict JSON 스키마 산출, 반려 시 반성문 코멘트 | verdict 스키마 검증(스크립트 파싱). 결정론 재확인은 Actions |
| critic | **codex exec 적대 모드**, 별도 세션 | 깨뜨릴 시나리오 탐색, 엣지 테스트를 PR에 추가 | 추가 테스트가 CI 편입되어 green |

**아침 부조종사 (워커 아님)**: 사람이 대화형 Claude Code를 직접 켜서 forge:mergeable PR들을 함께 리뷰(교차 벤더 층의 회수 지점), forge:adr 안건 논의, 다음 spec 작성 동행. 시스템이 자동 스폰하지 않는다(D11 봉인 유지).

**세션 분리 규율**: reviewer·critic은 executor와 반드시 다른 세션(컨텍스트 오염 차단). 모델 맹점 층은 아침 Claude가 담당.

**핸드오프 스키마 (D16·D17)**: kanban_complete 시 필수 3필드. "완료했습니다" 한 문장을 항목별 상태표로 강제 치환한다.

| 필드 | 내용 | 강제 장치 |
|---|---|---|
| implemented | 이번에 구현한 spec 항목 목록 | reviewer가 diff와 대조 |
| not_implemented | 미구현 항목 + 사유. **빈 배열도 명시적으로 기입**. 각 항목은 기존 이슈 ID / 신규 후속 이슈 ID / forge:adr 중 하나 필수 | Stop 훅이 ID 실존을 gh api로 확인(결정론), 부재 시 exit 2 = 잔여 물질화 게이트 |
| verified_by | 각 구현 항목의 검증 수단(테스트 파일 경로) | "검증 없는 완료" 불가 |

executor가 작업 중 카드가 과대함을 발견했을 때의 합법 수순은 3개뿐: 쪼개서 후속 카드 생성(D17 경로) / forge:adr 승격 / 계속 진행. "조용히 범위 축소 후 완료 선언"은 수순에 없다.

**래퍼 오버헤드(정직 명시)**: Kanban 워커는 항상 LLM 에이전트라 태스크당 래퍼 토큰이 든다. 완화: toolset을 kanban + terminal로 제한, 스킬 간결화, 저가 모델 핀. exit 0 단순 종료는 protocol_violation + 자동 block(코어 내장 fail-loud)이므로 kanban_complete/kanban_block 호출을 스킬에 명시.

**장시간 작업**: codex exec를 tmux 실행 + 폴링 사이 kanban_heartbeat. 스테일 회수 타임아웃(`kanban.dispatch_stale_timeout_seconds`, 기본 4h)은 야간 1~2h로 하향 검토.

**스킬 목록**: forge-labels, kanban-codex-delegate(executor 래퍼), reviewer-verdict, critic-adversarial, issue-finder-sot, memex, tdd-cycle(Codex 쪽: AGENTS.md + 훅), wiki-gate(SoT diff 감지 시 decision 기록 강제, Codex 훅).

---

## 6. 머지 정책 (D8)

**기본값 P1(전량 수동).** 세션 시작 시 선택이 곧 auth 행위. 미선언 = P1.

| 정책 | auto-merge 범위 | 사람이 보는 것 |
|---|---|---|
| P1 | 없음 | ADR + 모든 머지 |
| P2 | 되돌리기 쉬운 것만 | ADR + 되돌리기 어려운 머지 |
| P3 | critic + CI 통과 전부 | ADR만 |

태스크 오버라이드: `forge:automerge-ok` 라벨.

**P2 위험 분류 (스크립트, LLM 0)**

| 되돌리기 어려움 → 사람 | 되돌리기 쉬움 → 자동 |
|---|---|
| migrations/·스키마 변경 | docs/·README |
| 공개 API 시그니처 변경 | 테스트 추가 |
| 파일 삭제 포함 | 새 파일 추가 |
| 의존성 파일 변경 | 내부 리팩터 |
| CI·배포·시크릿 설정 | 주석·포맷 |

안전망: auto-merge분도 전부 PR로 남으므로 revert 1커밋으로 원복(되돌리기 쉬운 것만 자동이므로 자기일관).

---

## 7. 종료조건 ①~⑥ 배치

| 조건 | 구현 위치 |
|---|---|
| ① Verifier output | GitHub Actions 테스트 잡 (LLM 0) |
| ② Diff | PR 자체 + Codex 훅의 빈 diff 차단 |
| ③ Deterministic check | 이중: Codex Stop 훅(로컬) + Actions(원격) |
| ④ LLM judge 보조 | reviewer·critic. 반드시 ③ 통과 후, 주관 맥락 한정. 아침 Claude 부조종사가 최종 주관층 |
| ⑤ Capability eval + 회귀 + 감시 | 주간 Actions 스케줄 잡 + drift-audit 상시 |
| ⑥ Outcome/Latency/Cost/Transcript 동시 기록 | merge 이벤트 → ledger-emit이 ledger.jsonl 1행 원자 append(tmp+rename). Transcript 원문은 로컬 아카이브, 원장엔 경로만. MEMEX엔 요약 entry만 |

CI에 LLM을 올리지 않는다(컴플라이언스 + 결정론 순수성).

**구현 완료의 기계적 정의 (D16)**: spec 커버리지 M/M ∧ 전 이슈 close ∧ 전 게이트 초록. 이 술어가 참이 되기 전까지 시스템은 스스로를 "미완"으로 인지한다. 완료 여부는 모델의 선언이 아니라 이 술어로만 판정한다.

---

## 8. 실패 에스컬레이션 (D10 + D13)

닫힘의 단위는 태스크 1개다. 파이프라인은 계속 돈다.

| 층 | 동작 |
|---|---|
| L0 | 같은 세션 자기수정: Stop 훅 exit 2의 이유(stderr)가 모델에 재주입되어 계속 수정 |
| L1 | 새 세션 이어받기: 실패 시도 기록 → 재스폰. 워크트리·브랜치 유지, kanban_show로 이전 시도·코멘트 전체 + 반성문 주입. **최대 3회 (D13)** |
| L2 | 서킷 브레이커: 3회 소진 → forge:failed (즉시 알림). 사람이 코멘트 힌트 + 라벨 되돌리면 그 코멘트가 다음 시도 컨텍스트에 포함 |
| L3 | 디스패처는 나머지 ready 태스크 계속 배차. 실패 태스크의 자식만 의존 대기 |

**신호 구분**: 게이트 stderr 접두사 `TESTS_FAILED:`(판정 실패, 재시도 카운트 O) vs `GATE_ERROR:`(장치 고장, 카운트 X, 즉시 알림 직행).
**카나리아**: 밤 시작 시 정답이 알려진 더미 태스크 1건을 게이트에 통과시켜 검문소 자체를 점검. 실패 시 배차 시작 전 중단 + 알림.
**게이트 스크립트 규율**: 모든 에러 경로를 exit 2로 변환(bash `trap 'exit 2' ERR` / python 전체 try-except → sys.exit(2)). 게이트는 빠르고 단순하게.

---

## 9. hermes 약점 → 극복 장치 매핑

| 약점 (근거) | 극복 장치 | 층 |
|---|---|---|
| SQLite 손상: BTRFS COW·NFS 잠금과 WAL 비호환 | fs-precheck: ext4 + 로컬 NVMe 강제, 부적합 시 설치 중단 | 규율 1 |
| 네이티브 백업의 조용한 파일 누락 | nightly-backup: sqlite3 .backup 직접 + PRAGMA integrity_check + 크기·mtime 검증, 별도 디스크 | 규율 2+3 |
| 미지 assignee 카드의 ready 영구 체류 (디스패처 직접 생성 경로, 확인 필요) | drift-audit: ready 장기 체류 알람 | 규율 3 |
| session_search 취약·장기 기억 유실 | 기억을 hermes 밖으로: 지식 = memex outbox, 운영 = ledger 방출, 세션 = 소모품 | 구조 |
| 훅 fail-open (D9: non-zero exit·타임아웃 시 경고만 남기고 계속) | 결정론 차단을 hermes 훅에 두지 않음. Codex 훅(exit 2) + Actions로 fail-closed. hermes 훅은 관찰·주입·memex 미러 전용 | 게이트 |
| 자기채점 silent-pass | 세션 분리 reviewer·critic + 결정론 선행 + 아침 Claude 교차 층. Kanban 환각 게이트는 보완재 | 게이트 |
| 인간 피드백 2면 분산·비동기화 | 인간 창구 GitHub 1면 통일. 사람은 대시보드·세션에서 결정하지 않는다 | 토폴로지 |
| 업스트림 롤러코스터 | 코어 무수정 + 버전 핀 + 월 1회 창. 미러는 upstream 스펙(#31992 멱등키, #19932 경계) 호환으로 은퇴 가능 설계 | 원칙 |
| GitHub 의존 (신규) | Kanban 로컬 SoT라 GitHub 장애 밤에도 완주, 미러 outbox 따라잡기 | 토폴로지 |

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
- 쓰기: MEMEX entry 문법 호환 md(`## [aspect] 제목` + `project::` + `tags::` + `recorded_at::` 자체 필드). outbox/ 경유 fire-and-forget(불변). flush 스크립트가 localhost MCP로 실배달하고 성공분만 제거. 실패는 진행 안 막되 감사 로그에 남김.
- 읽기: MCP 실가동 확인(D19)으로 search_* soft 직결 조기 개방. 호출 대상 127.0.0.1:8080/mcp, 타임아웃 2~3초, 실패 시 로컬 vault·outbox grep 폴백.
- 금지: Transcript 원문, 스킬 본체 파일.

**진행상태 미러 (D4 개정)**
- ledger-emit 확장: Kanban 이벤트를 ledger.jsonl에 쓸 때 같은 이벤트를 MEMEX 진행상태 entry로도 outbox에 적재.
- 단방향 read-only. MEMEX 쪽 진행상태 노드는 read replica, 편집해도 다음 사이클에 덮어써짐. 재개·복구는 항상 Kanban 원본.

**쿼터 규칙**: save_memex 1건 = LLM 최대 3회(Codex 구독 = 야간 노동과 같은 지갑). spec당 배치 1회로 묶음. enrichment는 PendingQueue로 주간 보류 옵션. 대량 재인제스트는 ApiKeyClient.

---

## 12. 운영 스크립트 + 알림 (D14)

전부 LLM 0. 알림 경로 이원화(D22): 대화·지시는 hermes Slack 게이트웨이, **아래 스크립트들의 알림은 Slack Web API 직발송**(curl chat.postMessage + xoxb 토큰, hermes 우회). 이유: hermes 자체가 죽었을 때도 "hermes 죽음" 알림이 도착해야 한다. 감시자가 감시 대상에 의존하면 안 됨. 메시지 접두사는 `[레포명]` + `프로젝트명::동작` 네임스페이스, Slack 레이트리밋 대비 동일 분 내 다건은 배칭.

| 스크립트 | 주기 | 역할 | 즉시 알림 조건 |
|---|---|---|---|
| fs-precheck.sh | 설치 시 | ext4·NVMe·WAL 확인, BTRFS/NFS 차단 | 부적합 = 설치 중단 |
| canary.sh | 밤 시작 | 더미 태스크로 게이트 자체 점검 + Slack 왕복 1회(게이트웨이 wedged 상태 감지) | 실패 시 배차 중단 + 알림 |
| label-mirror.sh | 60초 (ETag) | 수입: 신규 이슈→triage 카드(멱등키), adr 해제·코멘트→unblock. 투영: 카드→forge:* 라벨, 시크릿 리댁션 | GitHub 5xx·토큰 만료 연속 N회, outbox 적체 |
| ledger-emit.sh | 10분 | Kanban 이벤트 → ledger.jsonl(원자 append) + MEMEX 진행상태 미러 entry | jsonl 단조 증가 위반 |
| nightly-backup.sh | 04:30 | state.db·kanban.db .backup + integrity_check (Phase 0~1 임시). Phase 2에서 Litestream 연속 복제로 승격(D20): OVH Object Storage 대상, 복제 지표 감시, 주간 복원 리허설, OVH 스냅샷 주 1회 병행 | 체크 실패·누락, 복제 지연 |
| spec-coverage.sh | 밤 시작 + 07:30 | 기획서 체크리스트(안정 ID: SPEC-NNN 부여 전제) ↔ 이슈(멱등키) 존재·close 대조 → 미대응 목록을 issue-finder 재투입 큐에 전달, 리포트에 "커버리지 N/M" 표기. 검증 불가능 문장은 forge:adr 분류 | 커버리지가 전일 대비 감소 시 |
| drift-audit.sh | 60분 | 불변식 2개 대조, ready 장기 체류, protocol_violation, GATE_ERROR 비율, 백업 신선도, 이슈 본문 편집 이벤트(D17), 자기 하트비트 | 전 항목 + 임계 초과 |
| morning-report.sh | 07:30 | merged/failed/adr 대기 집계 코멘트 (gh api, LLM 0) | |

**즉시 알림 대상 (D14)**: forge:adr / forge:failed / forge:mergeable 신규 + 카나리아 실패 / GATE_ERROR 임계 / 백업 무결성 실패 / outbox 적체. 기계 전이(in-progress 등)는 제외.

---

## 13. 운영 타임라인

```
저녁  사람 + 대화형 Claude: 다음 밤 spec 작성·투입, 머지 정책 선언(P1/P2/P3)
21:00 canary → 통과 시 spec-coverage 감사(미대응 항목 → issue-finder 재투입) → 배차 시작
21:05 issue-finder → 이슈 생성 → 미러 → 승격/adr 분기 (adr는 즉시 알림)
야간  executor(codex exec) → PR → reviewer(GPT 새 세션) → critic(codex exec 적대)
      → mergeable(즉시 알림) 또는 P2/P3 자동 머지 → 방출
      실패 시 L0→L1(반성문, 최대 3회)→L2(forge:failed, 즉시 알림)→L3(나머지 계속)
04:30 백업 + 무결성 검증
07:30 아침 리포트 (상단 고정: spec 커버리지 N/M + adr·failed·mergeable 집계)
아침  사람 + 대화형 Claude 부조종사:
      mergeable PR 공동 리뷰(교차 벤더 층) → 머지
      adr 안건 논의 → 코멘트 + 라벨 해제
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
- [ ] 워크스페이스 배치(D24): ~/work/<제품>/ 아래 3레포 최초 clone + 워크스페이스 AGENTS.md 작성. 각 레포에 docs/adr/, reflections/, outbox/ 골격 + forge:* 라벨 9종 멱등 생성 (`gh label create "forge:$L" || true` 루프, 3레포 각각)
- [ ] (선택) Agent SDK 크레딧 상태 1회 확인, upstream GitHub 브리지 상태 재확인(#31992·#19932: 구현됐으면 자작 미러 계획 축소)

**STEP 4: Kanban 점화 (30분)**
- [ ] `hermes kanban init` → ~/.hermes/kanban.db 생성 확인
- [ ] 프로필 4종 생성: 5절 표의 description 문구 그대로 사용(디컴포저 라우팅 근거). 정확한 문법은 설치 버전의 `hermes profile --help` 기준으로 확인
- [ ] 카드 1장 수동 생성 → specify → 디스패처가 워커를 스폰하고 ~/.hermes/kanban/logs/에 로그가 쌓이는지 확인

**Phase 0 완료 판정**: 텔레그램 왕복 / 재부팅 생존 / codex exec 스모크 / `ss`에서 8080 = 127.0.0.1 / kanban 카드 1장 / 실측치가 2.0절 표에 기입됨

> **판정 결과 (2026-07-10): Phase 0 완료.** Slack 왕복(#forge-cloud 아웃바운드+인바운드, 텔레그램→Slack은 D22) ✓ / 재부팅 생존(gateway·MEMEX 4컨테이너·백업타이머 자동복귀) ✓ / codex exec 스모크(CODEX_OK, 1,822tok) ✓ / kanban 카드 1장 20초 완주(t_eb52e76a) ✓ / 실측치 2.0절 기입 ✓ / 8080은 127.0.0.1 하향 대신 공개 유지(사용자 결정, 로컬 원격 접근 유지 — 하향 재검토 트리거는 로컬 SSH 터널 전환 시).
> 추가 완료(계획 외): 로컬 Windows 동급 메인 세팅(@forgelocal, D18 개정 성격 — 로컬은 관제석이 아니라 제2 게이트웨이), MCP 3종 양쪽 이식, 규약 스킬 2종(forge-ops·memex), nightly 백업(D20 임시판), 대시보드 9119 상주(127.0.0.1)+Desktop 원격 준비, Zscaler CA 번들 해결(로컬).

### Phase 1: 파이프 수동 검증 (2~3일)
- [ ] 워커 스킬 5종 + memex 스킬(outbox 쓰기) + Codex 훅(tdd-cycle·wiki-gate·게이트·예산 캡·잔여 물질화 D17)
- [ ] 핸드오프 3필드 스키마 + 이슈 템플릿(수용 기준 필수) + reviewer 델타 대조 임무 + 기획서 체크리스트 안정 ID(SPEC-NNN) 부여
- [ ] 이슈 1건 수동 투입 → Manual decompose → e2e 1왕복
- [ ] **태스크 1건당 Codex 쿼터 소모 실측** → 밤당 spec 수 역산 (재시도 3회 = 최악 4세션 반영)
- 검증: 이슈→카드→codex exec→PR→리뷰→critic→머지 1회 완주, protocol_violation 0, 핸드오프에 PR URL·changed_files 존재

### Phase 2: 무인화 (3~5일)
- [ ] 스크립트 8종 가동 (canary, spec-coverage 포함)
- [ ] adr 왕복 리허설 (알림 → 폰 코멘트 → 라벨 해제 → 수입)
- [ ] 야간 dry-run 3회
- 검증: 중복 카드 0, 드리프트 0, 백업 integrity 통과, GitHub 차단 모의 시 밤 완주 + 아침 따라잡기, GATE_ERROR 0, 고아 잔여 0(모든 not_implemented 항목이 이슈 ID 보유), 커버리지 N/M 표기 정상

### Phase 3: 확장
- [ ] Auto decompose 전환 (오케스트레이터 toolset 보드 연산 제한)
- [ ] capability eval 주간 잡 (VM 이전은 D18로 Phase 0에 흡수, MEMEX 조회 개방은 D19로 조기 완료)
- [ ] (옵션) reviewer를 codex exec로 이관 (필요 시)

---

## 15. 리스크와 불확실성

| 리스크 | 추정 | 대응 |
|---|---|---|
| **단일 벤더 집중**: 밤 전체가 OpenAI 의존. Anthropic식 3단계 재가격화·Google Gemini CLI 제재 전례로 업계 방향이 조임세 | 중 | 엔진 추상화 유지(래퍼가 CLI 교체 가능), 4~6주 정책 재점검, hermes 로컬 모델(Ollama 등) 경로를 비상 탈출구로 표기 |
| **Codex 쿼터 집중**: 밤 노동 + MEMEX enrichment 같은 지갑, 재시도 3회로 최악 4세션 | 중 | Phase 1 실측 필수, spec당 memex 배치 1회, enrichment 주간 보류, 태스크 예산 캡 |
| *(실측 2026-07-10, SPEC-001 e2e)* 태스크 1건 codex 소모 = **41,460 tokens** (README 작성 수준의 소형 문서 작업 기준. 코드+테스트 작업은 수배 예상. hermes 래퍼 gpt-5.5 소모는 별도. 쿼터 윈도우 규칙은 미확인이라 밤당 spec 수 역산은 코드 작업 실측 후) | - | - |
| Kanban 성숙도 (병합 2개월여) | 중 | 규율 2 엄수, 첫 2주 방출 일 2회 |
| 이중 상태 드리프트 | 중 | 단일 작성자(D7) + drift-audit + 라벨 최소주의 |
| 래퍼의 verdict 오독 | 중 | verdict JSON 스키마 강제 + 스크립트 파싱 |
| 쓰레기 후속 카드 양산 (D17이 카드 생성을 강제하므로) | 중 | 후속 이슈도 AC 필수 스키마 체크, 멱등키 중복 접기, triage 필터. 원칙: 고아(안 보임)보다 쓰레기(보임)가 낫다 |
| not_implemented 누락 기재 | 중 | 이중 방어: reviewer의 spec 대비 델타 대조(태스크 층) + spec-coverage 감사(spec 층, 밤 단위 회수) |
| GitHub 장애 밤 | 저 | Kanban 로컬 지속 + outbox 따라잡기 |
| 단일 박스 폭발반경 (hermes+MEMEX 동거) | 중 | Litestream + GitHub push + OVH 스냅샷 3중(D20), MEMEX soft-fail 불변, Neo4j에 docker 메모리 상한 |
| 미확인: 디스패처 직접 생성 카드의 미지 assignee 처리, hermes OpenAI 구독 인증 설정 상세, codex exec 쿼터 윈도우 규칙·태스크당 소모량, VPS vCPU·OS·디스크 타입, 8080 세계 개방 여부(핫스팟 curl 미실시), 대상 3레포의 회사 자산 여부(정책 확인 선행) | - | Phase 0~1 실측 (17절 목록) |

---

## 16. 운영 노브 (기본값 가동, 변경 가능)

| 노브 | 기본값 | 비고 |
|---|---|---|
| adr 무응답 처리 | 강등 없음, 3일 경과 시 아침 리포트 상단 경고 고정 | 사람 큐는 사람이 비운다 |
| 보드 분할 | 제품당 보드 1개 (D24: 워크스페이스 = 보드 단위, 카드에 대상 레포 필드) | 단일 레포 제품이면 레포당 1개와 자연히 동일. Slack 채널도 #forge-<제품> 1개 |
| auto-decompose | Phase 2까지 Manual, Phase 3에 Auto | 초기엔 분해 품질 관찰 우선 |
| 미러 폴링 간격 | 60초 (ETag 조건부) | 한도 여유 크면 30초 |
| 스테일 회수 타임아웃 | 2h (기본 4h에서 하향) | 야간 기준 |

---

## 17. 신규 세션 시작 지침 (인수인계)

**읽는 순서**: 0절(하드 제약) → 1절(D1~D21 결정) → 2.0절(환경 사실) → 14절 Phase 0 런북 실행 → Phase 1.

**Phase 1 최우선 제작물 2개** (이게 있어야 첫 e2e 왕복이 돈다):
1. `kanban-codex-delegate` 스킬(executor 래퍼): kanban_show로 카드·이전 시도·코멘트 읽기 → tmux로 codex exec 스폰 → 주기적 kanban_heartbeat → 핸드오프 3필드(implemented / not_implemented+이슈 ID / verified_by) 작성 → kanban_complete. exit 0 단순 종료 금지(protocol_violation).
2. Codex Stop 훅 게이트: 테스트·빈 diff·태스크 예산 캡·잔여 물질화(D17: not_implemented 항목별 이슈 ID를 gh api로 실존 확인). 모든 에러 경로를 exit 2로(bash: `trap 'exit 2' ERR`). stderr 접두사 규약: TESTS_FAILED:(재시도 카운트 O) / GATE_ERROR:(카운트 X, 즉시 알림).

**첫 e2e 절차**: 작은 이슈 1건 수동 투입(수용 기준 필수) → Manual decompose → executor → PR → 자동 reviewer 구축 전이므로 사람 + 대화형 Claude(로컬)로 수동 리뷰 → 머지 → **태스크당 codex 쿼터 소모 실측치를 이 문서 15절에 기록**. 이 수치가 재시도 3회(D13) 예산과 밤당 spec 투입량을 확정한다.

**절대 금지 3개**: hermes에 Anthropic OAuth 연결(D11) / MEMEX·대시보드의 공개 바인딩(D19·D21) / API 키·토큰 원문의 문서·레포·채팅 기재.

**미실측 잔여 (Phase 0~1에서 채울 것)**: VPS vCPU·OS·디스크 타입(→ 2.0절 표), 8080 세계 개방 여부(로컬이 아닌 망에서 curl 1회: 타임아웃이면 방화벽 정상), codex exec 쿼터 윈도우 규칙과 태스크당 소모량, hermes 프로필 생성 문법(설치 버전 --help).

**문서 갱신 규칙**: 결정 변경은 D번호 추가로만(기존 D 소급 수정 금지), 실측치는 2.0절 표에 기입, 구현 완료 판정은 7절의 기계적 정의(커버리지 M/M ∧ 전 이슈 close ∧ 전 게이트 초록)를 따른다.
