---
name: fix-task
description: "fix 전용: Review 또는 Deep Check의 fix_notes를 같은 PR에서 수정하고 새 Build를 시작할 step proof를 제출한다."
version: 1.0.0
author: INFINITY_FORGE
platforms: [linux]
metadata:
  hermes:
    tags: [Forge, Fix, Codex, Kanban]
    related_skills: [forge-ops, code-design-principles]
---

# Fix Task

fix는 카드에 기록된 문제만 같은 PR에서 고친다. 이전 Review·Deep Check 결과는 재사용하지 않으며 수정 뒤 Build부터 다시 검증한다.

## 순서

1. 카드의 `task_settings_hash`, `source_result_hash`, `tested_commit`, `pr_url`, `fix_notes`를 읽는다.
2. GitHub의 현재 PR commit이 `tested_commit`과 같은지 확인하고 카드 전용 worktree를 만든다.
3. 먼저 `fix_notes`의 실패를 재현하고 최소 수정과 회귀 테스트를 작성한다.
4. 같은 PR branch에 push하고 새 현재 commit을 다시 읽는다.
5. `~/forge/hooks/codex-work-check.sh "$WORKSPACE"`로 실제 변경과 저장소 테스트가 통과하는지 확인한다.
6. `kanban_complete` summary에 정확히 다음 step proof 하나만 제출한다.

```json
{
  "format_version": "forge-step-proof/v1",
  "tested_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "pr_url": "https://github.com/example/project/pull/1",
  "fix_notes": "재현한 문제와 적용한 수정",
  "source_result_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "source_run_id": 12,
  "source_task_id": "t_example",
  "task_settings_hash": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
}
```

> RISK(side-effect): 기존 PR branch에만 push하며 새 PR을 만들지 않는다.
