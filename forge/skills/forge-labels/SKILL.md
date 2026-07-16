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
| forge:needs-details | 작업 설명 보완 필요 | |
| forge:needs-decision | 사람의 설계 결정 필요 | O |
| forge:ready-to-build | Build 실행 대기 | |
| forge:building | Build 또는 Fix 실행 중 | |
| forge:reviewing | Review 실행 또는 대기 | |
| forge:deep-checking | Deep Check 실행 또는 대기 | |
| forge:ready-to-merge | 선택한 Task 흐름 완료. 병합 전 GitHub 검사는 별도 확인 | O |
| forge:waiting-for-help | 사람 입력 또는 외부 문제로 정지 | |
| forge:failed | 재시도 3회 소진 | O |

## 전이
needs-details → (needs-decision ↔) ready-to-build → building → reviewing → deep-checking → ready-to-merge → close.
선택한 Task 흐름에 없는 단계는 건너뛴다. 수정 요청은 building으로 돌아가며 어디서든 waiting-for-help/failed로 정지할 수 있다.

## 작성자 규칙 (위반 = state mismatch check 대상)
- 자동 전이는 **Issue Status Sync**(`issue-status-sync.py`)만 작성한다. 다른 작업 실행기와 Hermes Gateway는 라벨을 바꾸지 않는다.
- 사람은 `forge:needs-decision`을 해결하고 최종 PR을 병합할 수 있다.
- 불변식: 열린 Task 이슈에는 Forge 상태 라벨이 정확히 1개다.

> RISK(breaking): 위 9개 라벨만 현재 상태로 인식하며 이전 라벨은 읽지 않는다.
