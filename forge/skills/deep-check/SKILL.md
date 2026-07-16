---
name: deep-check
description: "deep_checker 전용: Review를 통과한 PR에 경계·상태·재시도 테스트를 추가하고 Deep Check 결과를 제출한다."
version: 1.0.0
author: INFINITY_FORGE
platforms: [linux]
metadata:
  hermes:
    tags: [Forge, DeepCheck, Testing, Kanban]
    related_skills: [forge-ops, code-design-principles]
---

# Deep Check

deep_checker는 검토된 commit에서 시작해 남아 있을 수 있는 결함을 실행 가능한 테스트로 확인한다. 제품 구현 코드는 수정하지 않는다.

## 순서

1. 카드의 `task_settings_hash`, `source_result_hash`, `tested_commit`, `pr_url`을 읽는다.
2. PR의 현재 commit이 카드의 `tested_commit`과 같은지 확인하고 카드 전용 worktree를 만든다.
3. 경계 입력, 동시 실행·재시도, 공개 계약 관점에서 최소 3개 사례를 정한다.
4. 최소 1개 새 테스트를 작성해 같은 PR branch에 commit·push한다.
5. push 뒤 현재 PR commit과 로컬 commit이 같은지 확인한다.
6. 테스트가 통과하면 `pass`, 제품 결함을 재현하면 `problems_found`와 구체적인 `fix_notes`를 제출한다.

```json
{
  "format_version": "forge-deep-check-result/v1",
  "task_settings_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "result": "pass",
  "source_result_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "pr_url": "https://github.com/example/project/pull/1",
  "reviewed_commit": "cccccccccccccccccccccccccccccccccccccccc",
  "tested_commit": "dddddddddddddddddddddddddddddddddddddddd",
  "added_tests": ["tests/test_edge.py"],
  "tested_cases": ["빈 입력"],
  "fix_notes": null
}
```

> RISK(side-effect): 같은 PR branch에 테스트 파일만 push하며, push 뒤 commit을 다시 읽어 결과에 기록한다.
