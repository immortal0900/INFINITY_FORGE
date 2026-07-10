---
name: kanban-codex-delegate
description: "executor 전용: kanban 카드를 받으면 tmux로 codex exec를 스폰해 실제 구현을 위임하고, 하트비트를 유지하며, 핸드오프 3필드(implemented/not_implemented/verified_by)로만 종료한다. kanban 태스크를 배정받았을 때 항상 적용."
version: 0.1.0
author: INFINITY_FORGE
platforms: [linux]
metadata:
  hermes:
    tags: [Forge, Kanban, Codex, Executor, Delegate]
    related_skills: [forge-ops, memex]
---

# kanban-codex-delegate (executor 래퍼 절차)

너는 직접 코드를 짜지 않는다. 구현은 codex exec에 위임하고, 너의 임무는 **절차 준수와 핸드오프 품질**이다.

## 절차 (순서 엄수)

1. **카드 파악**: `kanban_show`로 카드 본문·수용 기준(AC)·이전 시도(runs)·코멘트(반성문 포함)를 전부 읽는다.
   - 이전 시도가 있으면: 실패 원인과 반성문을 codex 프롬프트에 반드시 포함한다.
2. **작업 지시서 작성**: 카드 AC를 그대로 인용한 지시문을 만든다. AC를 재해석·축소하지 않는다 (본문 수정 금지 — 코멘트만 허용).
3. **codex exec 스폰 (tmux)** — 반드시 OPENAI/CODEX 계열 env를 제거하고 스폰한다(hermes가 주입한 env가 있으면 codex가 ChatGPT 로그인 대신 API키 모드로 빠져 401이 난다):
   ```bash
   tmux new-session -d -s task-<카드ID> 'cd <워크스페이스> && env -u OPENAI_API_KEY -u OPENAI_BASE_URL -u OPENAI_ORG_ID -u CODEX_API_KEY codex exec --skip-git-repo-check "<지시문>" > ~/.hermes/kanban/logs/<카드ID>-codex.log 2>&1'
   ```
   스폰 전 `codex login status`가 "Logged in using ChatGPT"인지 확인하고, 401이 나면 로그의 인증 관련 줄을 comment로 남겨라.
   참고: VPS의 ~/.codex/config.toml에 `sandbox_mode = "danger-full-access"`가 설정되어 있어(2026-07-10 결정) 1차 시도부터 파일 쓰기가 된다. 샌드박스 오류가 다시 보이면 bypass 재시도로 토큰을 태우지 말고 오류 줄을 comment로 보고하라.
4. **하트비트 루프**: codex 실행 중 60~120초마다 `kanban_heartbeat` 호출 + tmux 세션 생존 확인.
   - codex가 60분 넘게 무출력이면: tmux 로그 확인 후 `kanban_comment`로 상황 기록.
5. **결과 수확**: codex 종료 후 diff·테스트 결과·로그를 확인한다. Stop 훅 게이트(codex-stop-gate.sh)가 exit 2를 반환하면 stderr 사유를 읽고 codex에 재지시(같은 tmux 세션, L0 자기수정).
6. **핸드오프 작성 후 종료** — 아래 3필드를 kanban_complete의 summary에 JSON으로 기입:
   ```json
   {
     "pr_url": "<PR URL 또는 null>",
     "changed_files": ["..."],
     "implemented": ["<AC 항목별로>"],
     "not_implemented": [],
     "verified_by": {"<구현 항목>": "<테스트 파일 경로/검증 수단>"}
   }
   ```
   - **not_implemented는 빈 배열이라도 반드시 명시**한다. 타입은 반드시 JSON 배열 — 문자열("없음", "none" 등) 금지. 항목이 있으면 각각 `{"title": "...", "issue_id": "#N" 또는 "card_id": "t_..."}` 객체로.
   - not_implemented 항목이 있으면 각각에 후속 카드를 먼저 생성(`kanban_create`)하고 그 ID를 기입한다. ID 없는 잔여 항목으로는 종료 불가(D17).

## 금지

- exit 0 단순 종료 (protocol_violation → 자동 block). 반드시 `kanban_complete` 또는 `kanban_block`으로 끝낸다.
- "완료했습니다" 산문 선언으로 구현 사실을 대체하는 것. 완료 = 검증 통과 + 3필드.
- 카드가 과대하다고 판단될 때 조용히 범위를 줄이는 것. 합법 수순: 쪼개서 후속 카드 / `kanban_block`으로 인간 결정 요청 / 계속 진행 — 셋 중 하나만.
- 카드 본문(AC) 수정. 변경이 필요하면 block + 코멘트로 사유 기록.
