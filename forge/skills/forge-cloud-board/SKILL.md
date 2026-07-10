---
name: forge-cloud-board
description: "로컬(Forge-Local) 전용: 작업을 로컬에서 할지 클라우드로 보낼지 라우팅하고, 로컬 작업의 진행상황을 GitHub 이슈 코멘트로 공유한다. 카드/보드/작업 투입 요청 시 항상 적용."
version: 2.0.0
author: INFINITY_FORGE
platforms: [windows]
metadata:
  hermes:
    tags: [Forge, Kanban, Routing, Sharing]
    related_skills: [forge-ops]
---

# forge-cloud-board v2 (작업 라우팅 + 진행 공유)

## 원칙
- 장부는 머신마다 따로다(로컬 %LOCALAPPDATA%\hermes\kanban.db, 클라우드 VPS kanban.db). **진행상황의 공유 창은 GitHub 이슈다**(코멘트·PR).
- 사용자가 "로컬에서 해"라고 하면 로컬에서, 지정이 없고 무인 실행이 적합하면 클라우드로 보낸다. 강제 규칙이 아니라 사용자의 선택이 우선이다.

## 라우팅
| 지시 | 실행 |
|---|---|
| "로컬에서 작업해" | **로컬 보드에 카드 생성** 후 로컬에서 실행. GitHub 이슈가 있으면 멱등키 `github-issue:OWNER/REPO#N` 필수(공유·중복 방지의 열쇠) |
| "클라우드로 보내" 또는 무인 실행 적합 | GitHub 이슈에 AC 작성 + `forge:need-execution` 라벨 (2분 내 클라우드 미러가 수입) |
| 클라우드 보드 조회 | `ssh -i C:\Users\황화인HwainHwang\.ssh\id_ed25519 ubuntu@51.222.27.48 "export PATH=~/.hermes/node/bin:~/.local/bin:`$PATH; hermes kanban list"` |

## 진행 공유 규약 (로컬 작업 시)
- 로컬 카드가 GitHub 이슈와 연결돼 있으면(멱등키), local-sync가 상태 전이를 **이슈 코멘트**로 자동 보고한다 — 네가 따로 할 일은 멱등키를 정확히 넣는 것뿐.
- **forge:* 라벨은 절대 만지지 않는다** — 라벨의 단일 작성자는 클라우드 미러다(이중 작성자 = 드리프트). 로컬의 보고 수단은 코멘트다.
- 같은 이슈를 클라우드가 이미 작업 중인지 먼저 확인한다(이슈 라벨이 forge:in-progress면 클라우드가 진행 중 — 중복 착수 금지, 사용자에게 알린다).

## 완료 판정 (로컬에서도 동일)
- kanban_complete 핸드오프 3필드(implemented / not_implemented=JSON 배열 / verified_by). "완료했습니다" 산문 금지.
