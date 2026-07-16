---
name: forge-ops
description: "INFINITY_FORGE 운영 규약: Chat/Task 선택, Slack 채널-프로젝트 매핑, Task 실행과 완료 판정. 대화에서 실제 작업을 시작하거나 Kanban 카드를 다룰 때 적용."
version: 1.1.0
author: INFINITY_FORGE
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Forge, Kanban, Slack, Routing, Operations]
    related_skills: [memex, code-design-principles]
---

# INFINITY_FORGE 운영 규약

## 0. 대화를 시작할 때

처음에는 반드시 **Chat** 또는 **Task**를 고르게 한다.

- **Chat**: 일반 질문과 설계 논의만 한다. GitHub 이슈와 Kanban 카드를 만들지 않는다.
- **Task**: 실제 구현을 시작한다. 아래 두 선택을 매번 새로 받는다.
  - 실행 단계: **Build**, **Build + Review**, **Build + Review + Deep Check**
  - 병합 방식: **Manual Merge**, **Safe Files Auto-Merge**, **All Validated PRs Auto-Merge**

사용자가 최종 확인하기 전에는 외부 작업을 만들지 않는다. 확인 뒤에는 요청 ID, Task 내용 식별값, Task 설정 식별값을 GitHub 이슈·Task 설정 DB·Hermes 카드에 동일하게 기록한다.

## 1. Hermes Gateway 담당 범위
이 시스템에는 게이트웨이가 둘 있다. **자기 봇 이름에 해당하는 채널만 담당**한다:

| 봇 | 위치 | 담당 채널 |
|---|---|---|
| Forge-Cloud (@forgecloud) | OVH VPS, 24/7 | `#forge-cloud`(운영 홈) + `#forge-<제품>` 전부 |
| Forge-Local (@forgelocal) | 로컬 Windows | `#forge-local`만 |

## 2. Slack 채널 ↔ 프로젝트 매핑
- `#forge-cloud` = 시스템/운영 홈. 특정 제품에 속하지 않는 지시·질문·상태 보고.
- `#forge-<제품명>` = 제품(프로젝트)당 1개. 그 채널의 지시는 그 제품의 kanban 보드로 라우팅.
- 메시지 네임스페이스: 다중 프로젝트 맥락에서는 `프로젝트명::동작` 접두사로 대상을 명시 (예: `memex::status`).

## 3. 프로젝트 ↔ 보드 ↔ 카드 연결
- 보드는 제품당 1개 (`hermes kanban boards`). 프로젝트는 `hermes project`로 폴더(레포)들을 묶고 `bind-board`로 연결.
- 제품 채널에서 확인된 Task는 해당 제품 보드에 연결하고 카드 본문에 대상 저장소를 명시한다.
- **카드에는 대응 GitHub 이슈 URL과 변경할 수 없는 Task 설정을 넣는다.** Acceptance Criteria(수용 기준)의 원본은 확인된 Task 내용이며, Review와 Deep Check는 그 기준으로 실제 변경을 대조한다.
- 어느 프로젝트인지 불명확하면: 추측으로 카드를 만들지 말고 되물어 확정 후 생성.
- 여러 저장소를 바꾸는 작업은 주 저장소 이슈 1개와 저장소별 PR을 서로 연결한다. 모든 PR이 검사를 통과한 뒤, 다른 저장소가 먼저 필요로 하는 변경부터 병합한다.

## 4. 완료 판정 규칙

- "완료했습니다"라는 문장은 완료 증거가 아니다. 완료는 `kanban_complete` 결과와 선택한 검사가 실제 PR 상태에 모두 맞을 때만 인정한다.
- Build 결과에는 `completed_items`, `remaining_items`, `checks_by_item`, 변경 파일, PR URL, 확인한 base/head commit을 모두 넣는다.
- `remaining_items`가 있거나 항목별 검사가 빠졌으면 완료로 처리하지 않는다. 후속 Task를 만들거나 사람 결정을 요청한다.
- Review·Deep Check·Fix 결과는 바로 앞 단계 결과와 현재 PR commit에 묶는다. commit이 바뀌면 이전 결과를 다시 쓰지 않고 Build부터 재검증한다.
- 정상 종료는 `kanban_complete`, 사람 입력이나 외부 문제로 멈출 때는 `kanban_block`을 사용한다.

## 5. 상태 라벨과 병합

- Forge 상태 라벨 9종은 **Issue Status Sync**(`issue-status-sync.py`)만 쓴다. 다른 작업 실행기는 라벨을 직접 바꾸지 않는다.
- 기본 병합 방식은 **Manual Merge**다.
- 두 자동 병합 방식은 Task에서 명시적으로 선택되고, 승인 시간이 지나지 않았으며, 운영 환경에 정확히 `AUTO_MERGE_ENABLED=true`가 있을 때만 쓸 수 있다.
- 병합 직전에 현재 base/head commit, `eval`, Review 상태, branch 보호 설정, Task 설정을 다시 읽는다. 하나라도 다르거나 읽지 못하면 병합하지 않는다.

## 6. 안전
- Anthropic 구독 OAuth 연결 금지. API 키·토큰 원문을 메시지/파일/코드에 남기지 않는다.
