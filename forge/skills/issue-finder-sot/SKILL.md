---
name: issue-finder-sot
description: "issuefinder 전용: SoT(기획서)를 스캔해 SDD 스펙 골격 이슈를 gh로 생성하고 근거를 인용해 triage 분류한다. 근거 인용이 불가능하면 인간 결정(forge:adr)으로 보낸다."
version: 0.1.0
author: INFINITY_FORGE
platforms: [linux]
metadata:
  hermes:
    tags: [Forge, Issue, Spec, SoT]
    related_skills: [forge-ops, memex]
---

# issue-finder-sot (스펙 이슈 생성 절차)

## 원칙
- **직행 금지**: SoT(기획서·ADR)에서 근거를 인용할 수 없는 작업은 이슈로 만들지 않는다 — 인간 결정 대기(forge:adr 라벨 또는 kanban_block)로 보낸다. 네 추론은 근거가 아니다.
- 이슈 1건 = 검증 가능한 작업 1단위. 밤 세션 하나가 끝낼 수 있는 크기로.

## 이슈 본문 스키마 (필수 — 스키마 미달 이슈는 게이트가 반려)

```markdown
## 목적
<이 작업이 왜 필요한가 — 1~3문장>

## SoT 근거
<기획서/ADR의 해당 구절 인용 + 파일·절 번호. 없으면 이 이슈를 만들지 마라>

## 수용 기준 (AC)
- [ ] <기계적으로 판정 가능한 조건 1>
- [ ] <조건 2 — "테스트 X가 통과한다" 형태 권장>

## 범위 제외
<이번에 하지 않는 것 — 후속 이슈로 분리할 것들>
```

## 절차
1. 대상 SoT 문서를 읽고 체크리스트 항목(SPEC-NNN ID가 있으면 그 단위)과 기존 이슈를 대조한다 (`gh issue list`).
2. 미대응 항목마다 위 스키마로 이슈 생성 (`gh issue create`). 제목은 `[SPEC-NNN] <요약>` 형식(ID 있을 때).
3. 수용 기준은 생성 시점에 확정한다 — 이후 워커는 본문을 수정할 수 없다(코멘트만).
4. 생성한 이슈 목록을 kanban_complete summary에 기입(이슈 번호·제목·대응 SPEC ID).

## 금지
- AC 없는 이슈 생성. 근거 없는 이슈 생성. 기존 이슈와 중복 생성(멱등키: 이슈 제목의 SPEC ID).
