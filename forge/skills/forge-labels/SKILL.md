---
name: forge-labels
description: "forge:* 상태 라벨 9종의 정의와 전이 규칙(SSoT). 라벨을 읽거나 상태 전이를 판단할 때 참조한다. 워커는 라벨을 직접 조작하지 않는다."
version: 0.1.0
author: INFINITY_FORGE
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Forge, Labels, GitHub, State]
    related_skills: [forge-ops]
---

# forge-labels (상태 라벨 SSoT)

상태 라벨 9종, 상호 배타(열린 이슈에 정확히 1개). merged/closed는 GitHub 네이티브 상태 사용.

| 라벨 | 의미 | 즉시 알림 |
|---|---|---|
| forge:spec-draft | triage 대기 | |
| forge:adr | 인간 결정 대기 (그 건만 정지) | O |
| forge:need-execution | 실행 대기 | |
| forge:in-progress | 클레임됨 | |
| forge:need-review | PR 오픈, 리뷰 대기 | |
| forge:need-critic | 적대 리뷰 대기 | |
| forge:mergeable | critic + CI green | O |
| forge:blocked | 의존·장애 (adr 외) | |
| forge:failed | 재시도 3회 소진 | O |

## 전이
spec-draft → (adr ↔) need-execution → in-progress → need-review → need-critic → mergeable → close.
반려: need-review → need-execution (반성문 동반). 어디서든 → blocked/failed.

## 작성자 규칙 (위반 = drift-audit 감지 대상)
- 기계 전이는 **미러 스크립트 단독** 작성. 워커·게이트웨이는 라벨을 바꾸지 않는다(코멘트만).
- 인간 전용 전이 2건: forge:adr 라벨 **제거**(결정 완료 신호), PR 머지.
- 불변식: ① 열린 이슈에 forge:* 상태 라벨 정확히 1개 ② 이슈:카드 멱등키 1:1 (github-issue:OWNER/REPO#N).
