---
name: review-task
description: "reviewer 전용: Build 결과를 실제 diff와 Acceptance Criteria에 대조해 Review 결과를 제출한다."
version: 1.0.0
author: INFINITY_FORGE
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Forge, Reviewer, Review, Kanban]
    related_skills: [forge-ops, code-design-principles]
---

# Review Task

reviewer는 새 세션에서 카드의 step proof, Build 결과, 실제 PR diff와 테스트만 근거로 판단한다.

## 순서

1. 카드의 `task_settings_hash`, `source_result_hash`, `tested_commit`, `pr_url`을 읽는다.
2. GitHub에서 PR이 open·non-draft이고 현재 commit이 `tested_commit`과 같은지 확인한다.
3. Build의 `completed_items`, `changed_files`, `checks_by_item`을 diff·테스트와 대조한다.
4. Acceptance Criteria 누락과 신고되지 않은 변경을 확인한다.
5. 승인하면 `approve`, 수정이 필요하면 구체적인 `fix_notes`와 함께 `changes_needed`를 제출한다.

```json
{
  "format_version": "forge-review-result/v1",
  "task_settings_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "result": "approve",
  "source_result_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "pr_url": "https://github.com/example/project/pull/1",
  "reviewed_commit": "cccccccccccccccccccccccccccccccccccccccc",
  "change_check": {"confirmed_work": ["AC1"], "problems": []},
  "requirements_check": {"completed": ["AC1"], "missing": []},
  "fix_notes": null
}
```

`changes_needed`도 정상 품질 결과이므로 `kanban_complete`로 제출한다. 외부 장애나 형식 오류만 `kanban_block`한다.
