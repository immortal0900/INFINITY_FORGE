# INFINITY_FORGE

INFINITY_FORGE는 밤새 AI 작업자들이 GitHub 작업을 구현·검증하고, 아침에 사람이 결과를 검토해 병합하는 자동 개발 운영 시스템입니다.

## 문서

- [운용 기획서](docs/plan.md)
- [쉬운 설명서](docs/easy_guide.md)

## 아키텍처 개요

INFINITY_FORGE는 VPS와 로컬 환경을 나눠 쓰는 이중 hermes 게이트웨이 구조를 전제로 합니다. VPS 쪽 hermes는 Kanban 장부, 워커 실행, 재시도, 백업, MEMEX 연동 같은 야간 운영을 담당하고, 로컬 쪽 hermes 관제 환경은 사람이 아침에 상태를 확인하고 의사결정을 내리는 창구로 사용합니다.

야간 흐름은 OpenAI 계열 워커가 담당합니다. hermes가 Kanban 카드를 클레임하면 Codex 실행 세션이 구현을 수행하고, 별도 OpenAI 세션이 리뷰와 critic 검증을 맡습니다. 아침 흐름에서는 사람이 GitHub의 PR과 이슈를 확인하고, 대화형 Claude Code를 부조종사로 사용해 mergeable PR, ADR, 실패 작업을 검토합니다.

핵심 원칙은 진행 상태를 Kanban에 남기고, GitHub는 사람이 보는 투영 및 승인 창구로 쓰며, MEMEX는 작업을 멈추지 않는 비동기 지식 저장소로만 연결하는 것입니다.

## `forge/` 디렉토리 구조

| 경로 | 역할 |
|---|---|
| `forge/` | INFINITY_FORGE 운영 스크립트, 훅, 스킬을 담는 루트 디렉토리 |
| `forge/hooks/` | Codex 종료 게이트처럼 작업 종료 조건을 강제하는 훅 스크립트 |
| `forge/hooks/codex-stop-gate.sh` | 테스트, 빈 diff, 잔여 작업 물질화 같은 종료 전 검사를 수행하는 Codex stop gate |
| `forge/scripts/` | 배포, 백업, outbox flush 등 운영 자동화 스크립트 |
| `forge/scripts/deploy-vps.sh` | VPS 배포용 셸 스크립트 |
| `forge/scripts/deploy.ps1` | 로컬 Windows 환경에서 사용하는 배포 보조 PowerShell 스크립트 |
| `forge/scripts/flush-outbox.py` | 비동기 outbox 항목을 MEMEX 등 외부 저장소로 방출하는 스크립트 |
| `forge/scripts/nightly-backup.sh` | 야간 백업 실행 스크립트 |
| `forge/skills/` | hermes와 Codex가 역할별 작업 규칙을 읽는 스킬 모음 |
| `forge/skills/code-design-principles/` | 코드 설계 원칙을 정리한 스킬 |
| `forge/skills/code-problem-doc/` | 코드 문제 보고서 작성 스킬과 참고 템플릿 |
| `forge/skills/code-problem-doc/references/` | 문제 보고서 템플릿과 예시 문서 |
| `forge/skills/critic-adversarial/` | critic 역할의 적대적 검토 스킬 |
| `forge/skills/easy-answer/` | 쉬운 설명 형식으로 답변하는 스킬 |
| `forge/skills/forge-labels/` | `forge:` GitHub 라벨 체계와 상태 전이 규칙 |
| `forge/skills/forge-ops/` | 운영 절차와 런북 성격의 스킬 |
| `forge/skills/issue-finder-sot/` | SoT 문서를 근거로 이슈 후보를 찾는 스킬 |
| `forge/skills/kanban-codex-delegate/` | Kanban 카드를 Codex 실행으로 위임하는 executor 스킬 |
| `forge/skills/memex/` | MEMEX 지식 저장 연동 스킬 |
| `forge/skills/reviewer-verdict/` | reviewer 판정 형식과 검토 기준을 정의하는 스킬 |

## 보안

이 README에는 토큰, API 키, 비밀번호, IP 주소, 호스트명, 개인 계정 식별자 같은 비밀값을 포함하지 않습니다. 운영 자격증명은 레포에 커밋하지 않고 각 실행 환경의 제한된 권한 설정 파일에서 관리합니다.
