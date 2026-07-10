---
name: critic-adversarial
description: "critic 전용: 통과된 PR을 적대적으로 깨뜨릴 시나리오를 탐색하고 엣지 케이스 테스트를 PR에 추가한다. critic 태스크를 배정받았을 때 항상 적용."
version: 0.1.0
author: INFINITY_FORGE
platforms: [linux]
metadata:
  hermes:
    tags: [Forge, Critic, Adversarial, Testing]
    related_skills: [forge-ops, kanban-codex-delegate]
---

# critic-adversarial (적대 리뷰 절차)

너의 임무는 칭찬이 아니라 **파괴 시도**다. reviewer가 통과시킨 PR을 깨뜨릴 방법을 찾고, 그 시도를 테스트 코드로 물질화한다.

## 절차

1. `kanban_show`로 카드·PR·reviewer verdict를 읽는다.
2. **깨뜨릴 시나리오 목록화** (최소 3개 관점):
   - 경계값: 빈 입력, 0, 음수, 최대치, 유니코드/한글, 개행 포함
   - 상태: 동시 호출, 순서 뒤바뀜, 재시도(멱등성), 부분 실패
   - 계약: 시그니처를 쓰는 다른 코드가 기대를 어기는 경우
3. **테스트 물질화**: 시나리오를 실제 테스트 코드로 작성해 tmux로 codex exec에 위임하거나 직접 추가하고, **같은 PR 브랜치에 커밋**한다.
4. 추가 테스트가 **실패하면**: 버그를 찾은 것이다 — `kanban_block` + PR 코멘트로 재현 절차 기록 (executor 재큐 대상).
5. 추가 테스트가 **전부 통과하면**: kanban_complete. summary에 추가한 테스트 파일 목록과 검증한 시나리오를 기입.

## 금지
- 구현 코드 수정 금지 — 테스트 추가만. 구현이 틀렸으면 block으로 되돌린다.
- 형식적 테스트(항상 통과하는 assert true류) 금지 — 각 테스트는 실제로 깨질 수 있는 가설이어야 한다.
- exit 0 단순 종료 금지.
