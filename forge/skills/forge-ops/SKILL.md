---
name: forge-ops
description: "INFINITY_FORGE 운영 규약: Slack 채널-프로젝트-보드 매핑, 카드 라우팅, 완료 판정 규율. Slack에서 작업 지시를 받거나 kanban 카드를 만들 때 항상 적용."
version: 1.1.0
author: INFINITY_FORGE
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Forge, Kanban, Slack, Routing, Operations]
    related_skills: [memex, code-design-principles]
---

# INFINITY_FORGE 운영 규약 (Claude 부재 시에도 이 규칙으로 자율 동작)

## 0. 게이트웨이 정체성
이 시스템에는 게이트웨이가 둘 있다. **자기 봇 이름에 해당하는 채널만 담당**한다:

| 봇 | 위치 | 담당 채널 |
|---|---|---|
| Forge-Cloud (@forgecloud) | OVH VPS, 24/7 | `#forge-cloud`(운영 홈) + `#forge-<제품>` 전부 |
| Forge-Local (@forgelocal) | 로컬 Windows | `#forge-local`만 |

## 1. Slack 채널 ↔ 프로젝트 매핑
- `#forge-cloud` = 시스템/운영 홈. 특정 제품에 속하지 않는 지시·질문·상태 보고.
- `#forge-<제품명>` = 제품(프로젝트)당 1개. 그 채널의 지시는 그 제품의 kanban 보드로 라우팅.
- 메시지 네임스페이스: 다중 프로젝트 맥락에서는 `프로젝트명::동작` 접두사로 대상을 명시 (예: `memex::status`).

## 2. 프로젝트 ↔ 보드 ↔ 카드 라우팅
- 보드는 제품당 1개 (`hermes kanban boards`). 프로젝트는 `hermes project`로 폴더(레포)들을 묶고 `bind-board`로 연결.
- 제품 채널에서 온 작업 지시 → 그 제품 보드에 카드 생성 (`--board <슬러그>`), 카드 본문에 대상 레포 명시(멀티레포 제품).
- **카드 본문에는 반드시 대응 GitHub 이슈 URL을 첫 줄에 명시**한다. AC의 원본(SoT)은 GitHub 이슈 본문이며, 카드 본문은 파생물이다 — triage의 auto-decomposer가 카드 제목·본문을 재작성할 수 있으므로, 리뷰·검증은 항상 **이슈의 AC 기준**으로 대조한다.
- 어느 프로젝트인지 불명확하면: 추측으로 카드를 만들지 말고 되물어 확정 후 생성.
- 교차 레포 작업: 이슈 1장(계약 소유 주 레포) + 카드 1장 + 레포별 PR 상호 링크. 연결 PR 전부 green일 때만 mergeable. 제공자(provider) 레포부터 머지.

## 3. 완료 판정 규율 (절대 규칙)
- "완료했습니다"라는 문장은 완료가 아니다. 완료 = kanban_complete 호출 + 검증 통과.
- kanban_complete 핸드오프 3필드 필수: implemented / not_implemented(**빈 배열도 명시**, 각 항목은 후속 카드·이슈 ID 필수) / verified_by.
- 카드가 과대하면 합법 수순 3개뿐: 쪼개서 후속 카드 / 인간 결정 요청(block) / 계속 진행. 조용한 범위 축소 후 완료 선언 금지.
- exit 0 단순 종료 금지: kanban_complete 또는 kanban_block으로만 종료.

## 4. 상태 라벨 (GitHub 연동 시)
- forge:* 라벨 9종은 미러 스크립트가 단독 작성자. 워커는 라벨을 직접 바꾸지 않는다(코멘트만).

## 5. 안전
- Anthropic 구독 OAuth 연결 금지. API 키·토큰 원문을 메시지/파일/코드에 남기지 않는다.
