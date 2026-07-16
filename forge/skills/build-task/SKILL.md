---
name: build-task
description: "builder 전용: Task의 Acceptance Criteria를 구현하고 현재 PR commit에 묶인 Build 결과를 제출한다."
version: 1.0.0
author: INFINITY_FORGE
platforms: [linux]
metadata:
  hermes:
    tags: [Forge, Builder, Codex, Kanban]
    related_skills: [forge-ops, code-design-principles]
---

# Build Task

builder는 카드의 Task 설정과 Acceptance Criteria를 그대로 구현한다. 완료 선언은 아래 JSON과 실제 PR·테스트가 일치할 때만 유효하다.

## 순서

1. `kanban_show`로 `task_settings_hash`, repository, issue, Task 본문을 읽는다.
2. 카드에 지정된 workspace에서 PR 대상 base commit을 기록하고 별도 branch에서 작업한다.
3. Codex에 Acceptance Criteria 전체와 테스트 우선 요구를 전달한다.
4. PR을 열고 현재 PR commit에서 테스트를 실행한다.
5. `~/forge/hooks/codex-work-check.sh "$WORKSPACE"`로 실제 변경과 저장소 테스트가 통과하는지 확인한다.
6. `kanban_complete` summary에 정확히 다음 형식 하나만 제출한다.

```json
{
  "format_version": "forge-build-result/v1",
  "task_settings_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "pr_url": "https://github.com/example/project/pull/1",
  "built_base_commit": "cccccccccccccccccccccccccccccccccccccccc",
  "built_commit": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "changed_files": ["src/example.py"],
  "completed_items": ["AC1"],
  "remaining_items": [],
  "checks_by_item": {"AC1": "tests/test_example.py::test_ac1"}
}
```

## 중단 조건

- Task 설정 hash, repository, issue 또는 PR이 카드와 다르다.
- `remaining_items`가 비어 있지 않다.
- `built_base_commit` 또는 `built_commit`이 현재 PR base/head commit과 다르다.
- 테스트 또는 Codex 작업 완료 검사가 실패한다.

> RISK(side-effect): GitHub push와 PR 생성은 카드에 지정된 repository와 branch에만 수행한다.
