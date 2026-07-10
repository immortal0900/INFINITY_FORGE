---
name: forge-cloud-board
description: "로컬(Forge-Local) 전용: kanban 카드 생성·조회·조작은 로컬 보드가 아니라 클라우드 정본 보드에 SSH로 한다. 사용자가 카드/보드/작업 목록/작업 투입을 요청하면 항상 적용."
version: 1.0.0
author: INFINITY_FORGE
platforms: [windows]
metadata:
  hermes:
    tags: [Forge, Kanban, Remote, SSoT]
    related_skills: [forge-ops]
---

# forge-cloud-board (정본 보드 원격 조작)

## 원칙
- **kanban 정본(SoT)은 클라우드(VPS) 보드 하나다.** 로컬 kanban.db는 사용하지 않는다 — 두 보드에 나눠 쓰면 작업 내역이 갈라진다.
- SQLite는 원격 공유가 불가능하므로(파일락 미보장 → 손상), 공유는 "같은 파일"이 아니라 "같은 보드에 SSH로 명령"으로 달성한다.

## 조작 방법 (terminal 도구로 실행)
클라우드 보드 명령은 전부 이 형태로 실행한다:
```powershell
ssh -i C:\Users\황화인HwainHwang\.ssh\id_ed25519 ubuntu@51.222.27.48 "export PATH=~/.hermes/node/bin:~/.local/bin:`$PATH; hermes kanban <명령>"
```
- 목록: `hermes kanban list`
- 상세: `hermes kanban show <카드ID>`
- 생성: `hermes kanban create "<제목>" --body "<본문>" --assignee executor --workspace dir:/home/ubuntu/work/<레포> --idempotency-key <키> --max-retries 3`
- 코멘트: `hermes kanban comment <카드ID> "<내용>"`

## 작업 투입의 더 좋은 경로
- 코드 작업 투입은 카드 직접 생성보다 **GitHub 이슈 + forge:need-execution 라벨**이 우선이다(2분 내 미러가 카드 생성, 라벨·알림 자동).
- 카드 직접 생성은 GitHub에 남기지 않을 운영성 작업에만.

## 금지
- 로컬 kanban.db에 카드 생성 금지(정본 분열). 클라우드 카드 본문(AC) 수정 금지 — 코멘트만.
