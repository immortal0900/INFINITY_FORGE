---
name: reviewer-verdict
description: "reviewer 전용: executor의 핸드오프 델타 표를 diff·테스트와 대조하고 PR을 스펙과 대조해 verdict JSON을 산출한다. 리뷰 태스크를 배정받았을 때 항상 적용."
version: 0.2.0
author: INFINITY_FORGE
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Forge, Review, Verdict, Kanban]
    related_skills: [forge-ops, code-design-principles]
---

# reviewer-verdict (리뷰 절차)

너는 **새 세션**이다. executor의 대화 컨텍스트를 물려받지 않고 카드 영수증, PR diff, 테스트 파일만 근거로 판단한다. 산문 완료 선언 대신 `kanban_complete` summary에 정확한 reviewer result JSON 하나를 제출한다.

## 절차 (순서 엄수)

1. **영수증과 HEAD 고정**: `kanban_show`로 카드 본문의 canonical JSON 영수증을 읽는다.
   - `source_digest`, `pr_url`, `bound_head_sha`를 별도로 기록한다.
   - `gh pr view <pr_url> --json url,headRefOid,state,isDraft`로 PR이 open/non-draft이며 현재 HEAD가 `bound_head_sha`와 같은지 확인한다.
   - URL 또는 HEAD가 다르거나 영수증 필드가 없으면 품질 반려가 아니라 protocol violation이다. 사유를 남기고 `kanban_block`한다.
2. **1차 임무 — 델타 대조**: 상위 executor의 핸드오프를 읽고 실제 diff(`gh pr diff <pr_url>` 또는 고정된 HEAD의 git diff)와 대조한다.
   - implemented 항목이 diff에 실제로 존재하는가?
   - verified_by의 테스트 파일이 실존하고 해당 항목을 실제로 검증하는가?
   - diff에는 있는데 핸드오프에 없는 변경이 있는가?
   - 카드 AC 중 implemented와 not_implemented 양쪽에 모두 없는 항목은 반려 사유다.
3. **2차 임무 — 스펙 대조**: PR diff를 카드의 수용 기준(AC)과 항목별로 대조한다.
4. **바인딩 복사**: 카드의 `source_digest`와 `pr_url`을 결과의 같은 이름 필드에 그대로 복사하고, 카드의 `bound_head_sha`를 결과의 `head_sha`로 복사한다. 아래 예시 값을 그대로 쓰지 말고 카드 값을 한 글자도 바꾸지 않는다.
5. **결과 제출**: summary에는 아래 exact field set 외의 필드를 넣지 않는다. `reject`도 정상적인 품질 판정이므로 `kanban_block`이 아니라 `kanban_complete`로 제출한다.

| verdict | 종료 호출 |
|---|---|
| `approve` | `kanban_complete` |
| `reject` | `kanban_complete` |

`approve` summary 예시:

```json
{
  "schema_version": "forge-reviewer-result/v1",
  "verdict": "approve",
  "source_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "pr_url": "https://github.com/example/project/pull/1",
  "head_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "delta_check": {
    "implemented_verified": ["AC1 구현과 테스트가 diff에 존재"],
    "discrepancies": []
  },
  "spec_check": {
    "met": ["AC1"],
    "unmet": []
  }
}
```

`reject` summary 예시:

```json
{
  "schema_version": "forge-reviewer-result/v1",
  "verdict": "reject",
  "source_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "pr_url": "https://github.com/example/project/pull/1",
  "head_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "delta_check": {
    "implemented_verified": [],
    "discrepancies": ["AC2 테스트가 handoff에는 있으나 diff에 없음"]
  },
  "spec_check": {
    "met": ["AC1"],
    "unmet": ["AC2"]
  },
  "reflection": "AC2 회귀 테스트를 먼저 추가하고 실패를 확인한 뒤 구현을 보완해야 한다."
}
```

6. **반려 기록**: reject의 `reflection`은 비어 있지 않아야 하며 PR 코멘트로도 남긴다(`gh pr comment <pr_url> --body <reflection>`). 다음 executor-rework 카드가 같은 reflection을 전달받는다.

Hermes blocked는 인프라 장애나 protocol violation에만 사용한다. 스펙 미충족, 미신고 변경, 테스트 누락은 `reject` 완료 결과다.

## 금지

- 카드 영수증과 다른 `source_digest`, `pr_url`, `head_sha` 제출.
- summary에 schema 밖 필드나 JSON 앞뒤 산문 추가.
- 카드 본문 또는 AC 수정, verdict 없이 단순 exit 0.
- "대체로 좋음" 같은 모호 판정. approve 아니면 reject다.
