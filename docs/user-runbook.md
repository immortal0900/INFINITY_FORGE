# INFINITY_FORGE 실제 사용 가이드

> 기준일: 2026-07-16
>
> 검증 기준: 이 저장소의 추적된 코드, Windows에 설치된 Hermes Agent v0.18.2, 현재 VPS와 GitHub의 읽기 전용 실측 결과
> 대상: INFINITY_FORGE를 직접 켜고, 작업을 넣고, 승인하고, 결과를 병합하려는 운영자

## 0. 먼저 결론

저장소에는 실제 Task 실행 경로가 연결돼 있다. 다만 저장소 구현과 production 배포는 별도 상태이므로 서버 반영 여부는 13.2절처럼 commit과 service를 직접 확인한다.

1. 대화 시작 시 **Chat** 또는 **Task**를 고른다.
2. Task라면 **Build / Build + Review / Build + Review + Deep Check**와 **Manual Merge / Safe Files Auto-Merge / All Validated PRs Auto-Merge**를 매번 고른다.
3. 작업 내용과 선택값을 최종 확인하면 시스템이 GitHub 이슈와 변경할 수 없는 Task 설정을 만든다.
4. 선택한 검사가 끝나 원본 이슈가 `forge:ready-to-merge`가 될 때까지 기다린다.
5. Manual이면 사람이 PR diff·현재 base/head·CI를 확인해 병합한다. 자동 방식은 추가 안전 조건을 모두 통과했을 때만 Merge Worker가 병합한다.

Task Flow Worker, Issue Status Sync, Merge Worker는 Task 설정 DB·확인된 Task 기록·Hermes DB·GitHub를 실제로 읽고 쓴다. 서로 연결된 요청 ID, 내용 식별값, 설정 식별값, 이슈, 카드, PR commit이 하나라도 다르면 해당 실행은 코드 `2`로 끝나고 다음 외부 쓰기를 하지 않는다.

> RISK(service interruption): 자동 병합은 기본값이 꺼져 있다. 두 자동 방식을 실제로 쓰려면 Task에서 해당 방식을 선택한 것과 별개로 서버에 정확히 `AUTO_MERGE_ENABLED=true`가 있어야 한다. 이 값을 켜기 전에는 Manual 시험 Task로 서버의 실제 DB 경로·GitHub 권한·Hermes 실행 경로를 확인한다.

## 1. 지금 실제로 되는 것과 안 되는 것

| 기능 | 현재 상태 | 코드 근거 또는 실측 |
|---|---|---|
| VPS Gateway와 Dashboard 상주 | 가동 중 | `hermes-gateway.service`, `hermes-dashboard.service`가 active |
| 확인된 Task의 GitHub 이슈 연결 | 저장소 구현 완료 | Task Service가 요청 ID와 두 식별값을 이슈에 기록하고 완료된 요청을 다시 읽을 수 있음 |
| Build·Review·Deep Check·Fix 카드 자동 실행 | 저장소 구현 완료 | `task-flow-worker.py`가 실제 Hermes 카드·완료 결과·PR commit을 재구성해 없는 다음 카드만 1개 생성 |
| Codex 작업 완료 검사 | 구현 | 빈 diff와 저장소별 테스트를 `forge/hooks/codex-work-check.sh`가 검사하고, Task 결과 JSON은 strict parser가 별도로 확인 |
| Manual 사람 병합 | 구현 | `forge:ready-to-merge` 뒤 사람이 GitHub에서 최종 병합 |
| Review·Deep Check 결과 | 저장소 구현 완료 | 정해진 JSON 결과와 바로 앞 단계 결과·현재 PR base/head를 함께 확인 |
| 선택한 검사 자동 연결 | 저장소 구현 완료 | 선택하지 않은 단계는 건너뛰고, 필요한 단계만 서로 다른 카드·세션으로 생성 |
| Review/Deep Check 반려 재작업 | 저장소 구현 완료 | Fix 카드 생성, 최대 3회, 이후 `forge:failed` |
| `forge:deep-checking`·`forge:ready-to-merge` 자동 전이 | 저장소 구현 완료 | `issue-status-sync.py`가 실제 Task 상태를 읽고 GitHub 공식 라벨 하나로 교체한 뒤 다시 읽어 확인 |
| Safe Files Auto-Merge | 저장소 구현 완료, 기본 off | 안전 파일 규칙과 공통 병합 조건을 모두 검사하고 `AUTO_MERGE_ENABLED=true`에서만 쓰기 |
| All Validated PRs Auto-Merge | 저장소 구현 완료, 기본 off | 파일 분류만 생략하고 공통 병합 조건은 그대로 검사하며 같은 환경 opt-in 필요 |
| `forge:needs-decision` 사람 결정 | 수동 재개 | 결정 근거를 기록한 뒤 막힌 카드를 사람이 다시 시작 |
| GitHub main 보호 | **적용 완료** | `protect-main` ruleset ID `18974841`: PR 필수, approvals 0, strict `eval`, bypass 없음 |

여기서 중요한 구분은 다음과 같다.

- `docs/plan.md`는 목표 설계를 포함한다.
- 이 문서는 **현재 실행 코드로 가능한 동작**만 정상 사용법으로 적는다.
- 설계에만 있는 명령을 추정해 만들어 내지 않는다.

## 2. 어떤 방식으로 사용할지 고르기

사용 경로는 세 가지다. 결과적으로 같은 VPS를 사용하지만, 작업을 시작하고 확인하는 화면이 다르다.

| 방식 | 시작 위치 | 적합한 상황 | 제약 |
|---|---|---|---|
| A. GitHub 웹 | 브라우저 | 평소 작업 투입·PR 검토·머지 | 워커 내부 로그는 별도 터미널 필요 |
| B. PowerShell + GitHub CLI | Windows Terminal | 반복 작업, 상태 조회, 정확한 명령 기록 | 최초 `gh auth login` 필요 |
| C. Hermes Desktop | 바탕화면 앱 | 보드·세션을 큰 화면에서 관제, 대화형 운영 | 클라우드 연결용 SSH 터널과 session token 필요 |

**추천은 A를 기본으로 하고 C를 관제 화면으로 함께 쓰는 방식이다.** 이유는 GitHub 이슈 본문이 수용 기준의 원본이고, 현재 자동 수입 코드가 GitHub 라벨을 유일한 정상 투입 신호로 사용하기 때문이다. B는 장애 확인과 반복 명령에 가장 정확하다.

Slack의 `#forge-cloud`와 `#forge-local`은 대화·알림 창구다. 작업의 최종 수용 기준과 승인은 GitHub에 남긴다.

## 3. 최초 1회 준비

### 3.1 PowerShell에서 저장소로 이동

```powershell
Set-Location C:\01.project\INFINITY_FORGE
```

### 3.2 설치와 로그인 상태 확인

```powershell
hermes --version
codex login status
gh auth status
```

2026-07-14 실측 상태는 다음과 같다.

- Windows Hermes: v0.18.2
- Windows Codex: `Logged in using ChatGPT`
- Windows GitHub CLI: **미로그인**
- VPS Hermes: v0.18.2
- VPS Codex와 GitHub CLI: 로그인 상태

따라서 Windows에서 `gh` 명령을 처음 사용하기 전에 다음을 실행한다.

```powershell
gh auth login --web --git-protocol https
gh auth setup-git
gh auth status
gh repo view immortal0900/INFINITY_FORGE
```

브라우저가 열리면 본인 GitHub 계정으로 로그인하고 표시된 기기 인증을 승인한다. 토큰을 명령줄이나 문서에 직접 붙여 넣지 않는다.

### 3.3 클라우드가 살아 있는지 확인

```powershell
ssh ubuntu@51.222.27.48 'export PATH="$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH"; hermes gateway status; hermes kanban stats'
```

정상이면 Gateway가 running이고 보드 상태 집계가 출력된다. Gateway가 내려가 있을 때만 다음을 실행한다.

```powershell
ssh ubuntu@51.222.27.48 'export PATH="$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH"; hermes gateway start'
```

## 4. 터미널에서 켜고 사용하는 방법

### 4.1 로컬 Windows Hermes 열기

```powershell
Set-Location C:\01.project\INFINITY_FORGE
hermes --tui
```

`--tui`는 TUI(터미널 사용자 인터페이스, 터미널 안에서 메뉴와 대화를 표시하는 화면)를 연다. 이 명령은 **Windows 로컬 Hermes와 로컬 보드**를 사용한다. VPS 클라우드 보드를 직접 여는 명령이 아니다.

로컬 Slack Gateway까지 사용할 때는 별도로 시작한다.

```powershell
hermes gateway start
hermes gateway status
```

Hermes Desktop은 자체 로컬 backend를 시작하므로, Desktop만 보는 경우 로컬 Slack Gateway가 꺼져 있어도 앱 자체는 열 수 있다.

### 4.2 터미널에서 VPS Hermes 열기

```powershell
ssh -t ubuntu@51.222.27.48 'export PATH="$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH"; cd "$HOME/work/INFINITY_FORGE"; hermes --tui'
```

이 화면에서 하는 대화와 명령은 VPS 파일·VPS Kanban을 대상으로 한다. 단순 상태 조회만 필요하면 대화 화면을 열지 말고 다음 명령을 사용한다.

```powershell
ssh ubuntu@51.222.27.48 'export PATH="$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH"; hermes kanban list --sort created-desc'
ssh ubuntu@51.222.27.48 'export PATH="$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH"; hermes kanban stats'
```

카드 ID가 `t_12345678`이라고 가정하면 상세·시도 기록·워커 로그는 다음과 같이 확인한다.

```powershell
ssh ubuntu@51.222.27.48 'export PATH="$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH"; hermes kanban show t_12345678'
ssh ubuntu@51.222.27.48 'export PATH="$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH"; hermes kanban runs t_12345678'
ssh ubuntu@51.222.27.48 'export PATH="$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH"; hermes kanban log t_12345678 --tail 12000'
```

### 4.3 터미널 명령 실행 승인 방법

Hermes가 위험 가능성이 있는 명령을 실행하려고 하면 다음 선택지가 표시된다.

| 표시 | 키 | 범위 | 권장 사용 |
|---|---:|---|---|
| `Allow once` | `1` | 이번 명령 1회 | 기본 선택 |
| `Allow this session` | `2` | 현재 세션 동안 같은 패턴 | 반복되는 읽기 전용 조회 |
| `Always allow` | `3` | `~/.hermes/config.yaml`에 영구 허용 | 충분히 좁고 안전한 조회 명령에만 사용 |
| `Deny` | `4` 또는 `Esc` | 실행 거부 | 삭제·머지·배포 대상이 예상과 다를 때 |

선택 전에는 명령 전문, 현재 작업 폴더, 대상 저장소·브랜치를 확인한다.

- `git push`, `gh pr merge`, 파일 삭제, 배포 명령은 `Allow once`를 사용한다.
- `Always allow`는 이후 세션에도 남는다.
- `hermes --yolo`와 세션 안의 `/yolo`는 위험 명령 승인 절차를 건너뛰므로 평상시 운영에 사용하지 않는다.

## 5. Hermes Desktop에서 켜고 사용하는 방법

### 5.1 로컬 보기

1. 바탕화면의 **Hermes Desktop**을 더블클릭한다.
2. 기본값인 **Local gateway**를 사용한다.
3. 필요한 프로젝트 폴더로 `C:\01.project\INFINITY_FORGE`를 선택한다.

이 모드는 Windows 로컬 파일과 로컬 Kanban을 본다.

### 5.2 클라우드 VPS 보기

1. 바탕화면의 **VPS 터널 (클라우드 보기용)**을 더블클릭한다.
2. 열린 검은 창을 유지한다. 창을 닫으면 터널이 끊어진다.
3. session token이 없거나 VPS Dashboard가 재시작됐다면 PowerShell에서 다음을 실행한다.

```powershell
Set-Location C:\01.project\INFINITY_FORGE
pwsh -File .\docs\setup\fetch-dashboard-token.ps1
notepad "$env:USERPROFILE\forge-backups\vps-dashboard-cred.txt"
```

4. `session-token:` 뒤의 값만 복사한다. 채팅·문서·스크린샷에 남기지 않는다.
5. Hermes Desktop에서 **Settings → Gateway**로 이동한다.
6. 적용 대상을 확인하고 **Remote gateway**를 선택한다.
7. **Remote URL**에 `http://127.0.0.1:9119`를 입력한다.
8. **Session token**에 복사한 값을 붙여 넣는다.
9. **Test remote**로 연결을 확인한다.
10. **Save and reconnect**를 누른다.

현재 VPS Dashboard는 `auth_required: false`인 self-hosted session-token 방식이다. 기존 `docs/setup/desktop-guide.md`의 사용자명·비밀번호 안내보다 `fetch-dashboard-token.ps1`과 현재 Desktop UI의 **Session token** 경로가 실제 구현과 일치한다.

### 5.3 Desktop에서 명령 승인

명령을 실행하려는 도구 행 아래에 승인 막대가 나타난다.

- **Run** 또는 `Ctrl+Enter`: 이번 1회 실행
- 아래 화살표 → **Allow this session**: 현재 세션 허용
- 아래 화살표 → **Always allow…**: 영구 허용 확인창 표시
- **Reject** 또는 `Esc`: 거부
- **Command**: 잘린 명령 전문 펼치기

`Always allow…`는 확인 후 영구 allowlist에 기록된다. GitHub 머지·배포·삭제 명령에는 사용하지 않는다.

### 5.4 Desktop에서 Task를 시작해도 되는가

가능하다. 공통 `pre_user_turn` 연결을 사용하는 화면이라면 Chat/Task 선택과 Task Service가 같은 순서로 실행된다. 단, Hermes v0.18.2 입력 연결 6개가 설치됐는지 먼저 확인한다. 사용자가 확인하기 전에 GitHub 이슈나 Kanban 카드를 직접 만들면 Task 설정과 연결 정보가 빠지므로 정상 Task 경로로 인정하지 않는다.

## 6. 대화에서 Task를 넣는 전체 절차

### 6.1 Chat과 Task 선택

1. 새 대화를 시작한다.
2. 일반 질문이나 설계 논의면 **Chat**을 고른다. 이 선택은 GitHub 이슈와 Kanban 카드를 만들지 않는다.
3. 실제 구현이면 **Task**를 고른다.
4. 실행 단계를 고른다.
   - **Build**
   - **Build + Review**
   - **Build + Review + Deep Check**
5. 병합 방식을 고른다.
   - **Manual Merge**
   - **Safe Files Auto-Merge**
   - **All Validated PRs Auto-Merge**
6. 제목, 목적, 문제, SoT 근거, 작업 범위, 수용 기준, 범위 제외, 확정된 제약을 입력한다.
7. 미리보기에서 모든 선택과 내용을 다시 읽고 최종 확인한다.

Task를 새로 시작할 때마다 4단계와 5단계를 다시 고른다. 지난 Task의 선택은 다음 Task에 자동으로 이어지지 않는다. 3×3의 모든 조합을 사용할 수 있다.

자동 워커나 사람이 생성하는 PR 제목과 본문은 기본적으로 한국어로 작성한다. 다만 코드 식별자, 명령어, 로그, 고유 제품명은 정확성을 위해 원문 표기를 유지할 수 있다.

### 6.2 좋은 수용 기준 예시

Task 내용 입력 화면은 다음 양식을 함께 표시한다. 사용하지 않는 항목은 빈칸으로 남기지 말고 삭제한다. 목록 형식은 수용 기준에서만 사용한다. 현재 Task 파서는 첫 번째 내용 줄을 제목으로 사용하고 모든 목록 줄을 수용 기준으로 수집하기 때문이다.

```markdown
[SPEC-NNN] <대상>을 <원하는 결과>로 변경

## 목적
이 작업으로 사용자가 얻어야 하는 결과를 한두 문장으로 작성한다.

## 문제
현재 상태: 현재 발생하는 문제나 부족한 동작을 작성한다.
완료 상태: 작업 후 관찰할 수 있어야 하는 상태를 작성한다.

## SoT 근거
근거: `docs/spec.md:42` 또는 관련 이슈·문서 URL
근거가 없는 신규 요구라면 `신규 요구사항`이라고 작성한다.

## 작업 범위
포함: 변경할 기능과 동작을 작성한다.
대상 모듈: 관련 모듈이나 디렉터리를 작성한다.

## 수용 기준 (AC)
1. [AC-01] `<조건 또는 입력>`일 때 `<관찰 가능한 결과>`가 발생한다.
2. [AC-02] `<오류 또는 경계 조건>`일 때 `<오류 코드·메시지·상태>`를 반환한다.
3. [AC-03] 위 동작을 재현하는 테스트가 추가되고 `<정확한 테스트 명령>`이 통과한다.
4. [AC-04] `<구체적인 기존 기능>`이 유지되고 `<검증 방법>`으로 확인된다.

## 범위 제외
제외: 이번 작업에서 변경하지 않을 내용을 작성한다.

## 확정된 제약
호환성: 유지해야 할 API, 데이터 형식 또는 실행 환경을 작성한다.
보안·성능: 지켜야 할 구체적인 제한을 작성한다.
미결정 사항: 없음
```

`잘 동작한다`, `깔끔하게 만든다`, `적절히 처리한다`처럼 판정 방법이 없는 문장은 AC로 사용하지 않는다. 아직 결정되지 않은 내용이 있으면 Task에 넣지 않고 Chat에서 먼저 결정한다. AC의 `[AC-01]` 같은 고정 ID는 Build 결과의 `completed_items`와 `checks_by_item`, 이후 Review가 같은 항목을 가리키는 데 사용한다.

### 6.3 최종 확인 뒤 만들어지는 것

최종 확인이 성공하면 다음 기록이 같은 요청 ID로 묶인다.

- GitHub Task 이슈
- 변경할 수 없는 Task 설정: 실행 단계, 병합 방식, 확인한 사용자와 시각, 최대 12시간의 자동 병합 만료 시각
- 확인된 Task 기록: 재시작 뒤에도 같은 요청을 한 번만 이어서 처리하기 위한 저장소
- Hermes 루트 카드와 이후 단계 카드

`task-service.py`는 GitHub 이슈에 Task 내용 식별값과 설정 식별값을 기록하고 다시 읽어 일치하는지 확인한다. 중간에 프로세스가 종료돼도 같은 요청 ID로 상태를 이어가며 이슈를 중복 생성하지 않는다.

GitHub 웹이나 `gh issue create`로 이슈만 직접 만들고 `forge:ready-to-build` 라벨을 붙이는 방식은 위 설정 기록이 없으므로 새 Task 실행 경로가 아니다.

## 7. 투입 후 무슨 일이 일어나는가

다음은 저장소에 연결된 실제 순서다. production에서 실행 중인지는 13.2절의 서버 commit과 service 확인으로 별도 판정한다.

1. 최종 확인된 Task가 GitHub 이슈와 변경할 수 없는 Task 설정으로 저장된다.
2. `task-flow-worker.py`가 확인된 Task 기록, 설정 DB, GitHub 이슈, Hermes DB를 읽고 세 기록이 같은 요청을 가리키는지 확인한다.
3. 루트 **Build** 카드가 없을 때만 정확히 한 장 만든다. 같은 실행을 반복해도 중복 카드를 만들지 않는다.
4. `issue-status-sync.py`는 같은 상태를 읽어 이슈의 공식 Forge 라벨을 정확히 하나로 맞춘 뒤 다시 읽어 확인한다.
5. Hermes Gateway의 다음 배차 시점에 Build가 실행된다.
6. Build는 Codex로 변경·테스트·PR과 Build 결과를 만든다.
7. Work Check와 결과 형식 검사가 다음을 확인한다.
   - 실제 구현 파일 변경이 있는가
   - 저장소 유형에 맞는 테스트가 통과하는가
   - `completed_items`가 수용 기준과 일치하는가
   - `remaining_items`가 비어 있는가
   - 모든 완료 항목이 `checks_by_item`으로 검증되는가
   - `built_base_commit`과 `built_commit`이 현재 PR base/head와 각각 같은가
8. 통과하면 Build 카드가 done이 된다.
9. Task Flow Worker가 현재 PR base/head가 Build 결과와 같은지 확인하고, 사용자가 선택한 다음 단계 카드를 정확히 1개 만든다.
10. **Review**를 선택했다면 별도 Hermes Task/session에서 Build 결과와 실제 diff·수용 기준을 대조한다.
11. Review가 승인하면 선택에 따라 Deep Check로 가거나 병합 준비가 된다. 수정이 필요하면 `fix_notes`를 포함한 **Fix** 카드가 생긴다.
12. **Deep Check**를 선택했다면 별도 worktree에서 심층 테스트를 추가하고 같은 PR branch에 push한다.
13. 선택한 모든 단계가 현재 PR commit에서 끝나면 이슈가 `forge:ready-to-merge`가 된다. 이 라벨 자체는 `eval` 성공을 뜻하지 않는다.
14. Review·Deep Check 반려 또는 수정 가능한 `eval` 실패는 같은 PR의 Fix로 돌아간다. Fix는 최대 3회다. 외부 일시 장애가 재현되지 않으면 의미 없는 commit을 만들지 않고 사람 도움 대기로 멈춘다.
15. `forge:ready-to-merge` 뒤 main이 전진해 branch를 갱신하면 이전 단계 결과를 폐기하고 새 base/head에서 Build부터 선택한 검사를 다시 실행한다.
16. Manual이면 사람이 diff·CI·base/head를 확인해 병합한다. 자동 방식을 선택했다면 Merge Worker가 공통 안전 조건과 Task별 조건을 모두 다시 확인한다.

각 자동 전이는 `forge-stage`, `forge-mirror` timer와 Hermes Gateway 배차 간격 때문에 수 분 걸릴 수 있다. 실제 시간은 작업 크기, 선택한 검사, Fix 횟수에 따라 달라진다.

Task Flow Worker의 모듈 연결만 확인하려면 다음 명령을 쓴다. 이 검사는 실제 DB·GitHub·카드 쓰기를 검증하지 않는다.

```powershell
ssh ubuntu@51.222.27.48 'cd "$HOME/work/INFINITY_FORGE" && PYTHONPATH="$PWD" python3 forge/scripts/task-flow-worker.py --check-port'
```

실제 실행은 배포 스크립트가 설정한 DB, Hermes, GitHub CLI, 저장소, workspace 인자를 모두 요구한다. 인자가 빠지거나 상태가 맞지 않으면 코드 `2`로 끝난다. 운영 확인은 인자를 생략한 직접 실행 대신 `systemctl --user start forge-stage.service`와 service 로그를 사용한다.

## 8. 상태를 확인하는 방법

### 8.1 GitHub 라벨 해석

| 라벨 | 지금 무엇을 뜻하는가 | 사용자가 할 일 |
|---|---|---|
| `forge:needs-details` | 스펙 보완 대기 | 목적·근거·AC 보완 |
| `forge:needs-decision` | 사람의 설계 결정 대기 | 이슈 코멘트로 결정 기록 후 아래 10장 절차 수행 |
| `forge:ready-to-build` | 아직 수입 전이거나 ready/todo | 3분 이상 지속되면 미러·Gateway 점검 |
| `forge:building` | Build 또는 Fix 실행 중 | 보통 기다림, 필요 시 카드 로그 확인 |
| `forge:reviewing` | Review 실행 또는 대기 | 보통 기다림. 오래 지속되면 Task Flow Worker와 Review 로그 확인 |
| `forge:deep-checking` | Deep Check 실행 또는 최신 HEAD CI 대기 | 보통 기다림. 수정 가능한 CI 실패는 Fix로 보내고, 시스템 오류는 사람이 로그 확인 |
| `forge:ready-to-merge` | 선택한 Task 단계 완료. GitHub 병합 조건은 별도 확인 필요 | Manual이면 사람이 `eval`·diff·base/head 확인, 자동 방식이면 Merge Worker 결과 확인 |
| `forge:waiting-for-help` | 입력·의존성·장치 문제로 정지 | 사유 확인 후 카드 unblock |
| `forge:failed` | 재시도 한도 소진 | 로그 확인 후 새 후속 이슈 권장 |

정상 Task에서는 사람이 실행 라벨을 직접 붙이지 않는다. 최종 확인 뒤 Task Service가 최초 상태를 만들고, 카드가 만들어진 뒤의 `forge:*` 라벨은 Issue Status Sync가 실제 Task 상태를 기준으로 다시 쓰므로 임의로 바꿔도 다음 주기에 되돌아갈 수 있다.

### 8.2 GitHub CLI로 보기

```powershell
gh issue list `
  --repo immortal0900/INFINITY_FORGE `
  --state open `
  --search "label:forge:building OR label:forge:reviewing OR label:forge:waiting-for-help OR label:forge:failed"

gh pr list --repo immortal0900/INFINITY_FORGE --state open
```

### 8.3 VPS 서비스와 타이머 보기

```powershell
ssh ubuntu@51.222.27.48 'systemctl --user status hermes-gateway --no-pager'
ssh ubuntu@51.222.27.48 'systemctl --user list-timers --all --no-pager | grep -E "forge-|hermes-"'
```

미러가 의심되면 최근 실행 로그를 본다.

```powershell
ssh ubuntu@51.222.27.48 'systemctl --user status forge-mirror.service --no-pager'
ssh ubuntu@51.222.27.48 'journalctl --user -u forge-mirror.service -n 100 --no-pager'
ssh ubuntu@51.222.27.48 'systemctl --user status forge-stage.service --no-pager'
ssh ubuntu@51.222.27.48 'journalctl --user -u forge-stage.service -n 100 --no-pager'
```

## 9. PR을 검토하고 승인하는 방법

### 9.1 Manual에서 승인이라는 말의 정확한 의미

현재 worker와 사람이 같은 GitHub 계정을 사용해 PR을 만들 수 있다. GitHub는 PR 작성자가 자신의 PR에 공식 `Approve` Review를 남기는 것을 허용하지 않는다. 따라서 Manual에서 사람 승인은 다음 행위의 조합이다.

1. **원본 이슈**의 현재 라벨이 `forge:ready-to-merge`인지 확인한다.
2. 사람이 `Files changed`, 현재 HEAD와 CI를 확인한다.
3. 문제가 있으면 코멘트하고 병합하지 않는다.
4. 문제가 없으면 사람이 직접 **Merge pull request**를 누른다.

`forge:ready-to-merge`은 PR 라벨이 아니라 원본 이슈 라벨이다. GitHub ruleset은 PR 경유와 `eval` 성공을 강제하지만 이 Forge 라벨까지 검사하지는 않는다. 원본 이슈가 `forge:reviewing` 또는 `forge:deep-checking`이면 GitHub의 merge 버튼이 활성화돼 있어도 병합하지 않는다.

독립된 공식 Approve를 플랫폼에서 강제하려면 별도 리뷰 계정 또는 협업자가 필요하다. 현재 `protect-main` ruleset은 단일 계정 운용 때문에 required approving reviews를 0으로 두되, **PR 경유와 최신 branch의 `eval` 성공은 GitHub가 강제**하도록 설계했다. bypass actor는 두지 않는다.

### 9.2 GitHub 웹에서 검토·머지

1. 저장소의 **Pull requests**를 열고 대상 PR을 선택한다.
2. **Conversation**에서 원본 이슈 링크를 연다.
3. 원본 이슈의 현재 라벨이 `forge:ready-to-merge`인지 확인한다. `forge:reviewing`·`forge:deep-checking`이면 기다린다.
4. PR에 **Update branch**가 표시되면 한 번 누르고 병합하지 않는다. 새 base/head에서 Build부터 선택한 검사가 끝나 원본 이슈가 다시 `forge:ready-to-merge`가 될 때까지 기다린다.
5. **Files changed**에서 파일별 diff를 확인한다.
6. 필수 check 이름 `eval`이 green인지 확인한다.
7. 다음 항목 중 하나라도 있으면 병합하지 않는다.
   - red 또는 pending check
   - 이슈 AC와 맞지 않는 변경
   - 설명되지 않은 파일 변경
   - migration, 공개 API, 의존성, 배포·보안 설정 변경인데 복구 계획 없음
8. 이상이 없으면 **Merge pull request → Confirm merge**를 누른다.
9. PR이 이슈를 자동으로 닫지 않았다면 원래 이슈를 직접 닫는다.

`protect-main` 적용 뒤에는 red/pending `eval`이나 최신 main을 반영하지 않은 branch를 GitHub가 병합 불가로 표시한다. **Update branch**는 검증 대상 코드를 바꾸는 행위이므로 이전 `forge:ready-to-merge`을 재사용하지 않는다. main이 다시 전진하면 같은 갱신·재검증을 반복하며 `--admin` 또는 ruleset bypass를 사용하지 않는다.

### 9.3 PowerShell에서 검토·머지

원본 이슈 번호가 `12`, PR 번호가 `7`이라고 가정한다. 먼저 원본 이슈 라벨을 확인한다.

```powershell
$labels = gh issue view 12 `
  --repo immortal0900/INFINITY_FORGE `
  --json labels `
  --jq '.labels[].name'

if ($labels -notcontains 'forge:ready-to-merge') {
  throw '원본 이슈가 forge:ready-to-merge이 아니므로 병합하지 않습니다.'
}

gh pr view 7 --repo immortal0900/INFINITY_FORGE --web
gh pr diff 7 --repo immortal0900/INFINITY_FORGE
gh pr checks 7 --repo immortal0900/INFINITY_FORGE --watch
```

GitHub가 branch 갱신을 요구하면 웹의 **Update branch**를 누르거나 다음 명령으로 main을 반영한 뒤, 이슈가 다시 `forge:ready-to-merge`이 될 때까지 기다린다.

```powershell
gh pr update-branch 7 --repo immortal0900/INFINITY_FORGE
```

CI와 선택한 검사가 모두 통과한 뒤 이슈 라벨을 다시 조회하고, 검토한 head SHA와 실제 병합 대상을 고정한다.

```powershell
$labels = gh issue view 12 `
  --repo immortal0900/INFINITY_FORGE `
  --json labels `
  --jq '.labels[].name'

if ($labels -notcontains 'forge:ready-to-merge') {
  throw '재검증이 끝나지 않았으므로 병합하지 않습니다.'
}

$head = gh pr view 7 `
  --repo immortal0900/INFINITY_FORGE `
  --json headRefOid `
  --jq .headRefOid

gh pr merge 7 `
  --repo immortal0900/INFINITY_FORGE `
  --merge `
  --match-head-commit $head `
  --delete-branch
```

`--match-head-commit`은 검토 후 새 커밋이 추가된 PR을 실수로 병합하지 않게 한다. `--admin`으로 검사를 우회하지 않는다.

### 9.4 반려할 때

Review는 `reject`와 비어 있지 않은 `fix_notes`를 결과로 제출한다. Task Flow Worker는 다음 Deep Check를 만들지 않고 같은 PR에 연결된 Fix 카드를 만든다. Deep Check가 제품 결함을 찾으면 `problems_found`로 기록하고 같은 Fix 경로를 사용한다.

- 사용자가 같은 이슈에 `forge:ready-to-build`을 다시 붙이거나 완료 카드를 삭제할 필요가 없다.
- Fix는 `fix_notes`를 그대로 받고 기존 PR branch에서 수정한다. 새 PR을 만들면 계약 위반이다.
- Fix 카드가 3개인 상태에서 다시 반려되면 새 카드를 만들지 않고 `forge:failed`가 된다.
- 범위 자체가 바뀌었거나 원래 AC가 틀렸다면 자동 재작업 대신 새 후속 이슈로 분리한다.

## 10. 설계 결정과 blocked 작업을 승인하는 방법

### 10.1 실행 전 ADR 결정

아직 카드가 생성되지 않은 이슈가 `forge:needs-decision` 상태라면 다음 순서로 처리한다.

1. 가능한 접근을 최소 2~3개 비교한다.
2. 선택한 접근, 선택 이유, 버린 접근, 복구 조건을 이슈 코멘트에 기록한다.
3. `forge:needs-decision`를 제거하면서 `forge:ready-to-build`을 붙인다.

```powershell
gh issue comment 12 `
  --repo immortal0900/INFINITY_FORGE `
  --body "결정: 접근 B. 이유: 공개 API를 유지하고 롤백이 파일 복원 1회로 끝남. 접근 A는 스키마 변경 때문에 제외. 실패 시 기존 구현으로 revert."

gh issue edit 12 `
  --repo immortal0900/INFINITY_FORGE `
  --remove-label "forge:needs-decision" `
  --add-label "forge:ready-to-build"
```

`forge:needs-decision` 제거만으로는 현재 미러가 카드를 만들지 않는다. 반드시 실행할 준비가 끝났다면 `forge:ready-to-build`도 붙인다.

### 10.2 실행 중 blocked 카드 재개

작업 중 입력이 필요해 카드가 blocked가 되면 GitHub 라벨은 `forge:waiting-for-help`가 된다.

1. 이슈와 카드 로그에서 질문을 확인한다.
2. 이슈 코멘트에 결정을 기록한다.
3. 클라우드 카드 ID를 확인한다.
4. 카드 자체를 unblock한다.

```powershell
ssh ubuntu@51.222.27.48 'export PATH="$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH"; hermes kanban list --status blocked'
```

카드 ID가 `t_12345678`이면 다음을 실행한다.

```powershell
ssh ubuntu@51.222.27.48 'export PATH="$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH"; hermes kanban unblock t_12345678 --reason "GitHub #12 결정 반영: 접근 B 사용"'
```

unblock 뒤 카드는 ready로 돌아가며 다음 디스패처 틱에서 다시 실행된다. 미러는 ready 상태를 `forge:ready-to-build`으로 투영한다.

## 11. 세 병합 방식은 구체적으로 어떻게 사용하는가

### 11.1 Task마다 매번 선택

| 화면 선택 | 내부 설정값 | 동작 |
|---|---|---|
| **Manual Merge** | `manual` | Merge Worker의 GitHub 쓰기 0. 사람이 최종 병합 |
| **Safe Files Auto-Merge** | `safe_auto` | 안전 파일 규칙과 공통 조건을 모두 통과한 PR만 자동 병합 |
| **All Validated PRs Auto-Merge** | `full_auto` | 파일 분류만 생략하고 공통 조건을 모두 통과한 PR 자동 병합 |

이 선택은 Kanban `priority`와 무관하다. 각 Task의 확인된 설정에 저장되며 다음 Task로 이어지지 않는다. 자동 방식의 권한은 최대 12시간 뒤 만료되고 Manual로 처리된다.

### 11.2 Manual 병합

대화에서 Manual을 선택하고, 원본 이슈가 `forge:ready-to-merge`인지 9.3절대로 확인한 뒤 사람이 병합한다.

```powershell
$head = gh pr view <PR_NUMBER> `
  --repo immortal0900/INFINITY_FORGE `
  --json headRefOid `
  --jq .headRefOid

gh pr merge <PR_NUMBER> `
  --repo immortal0900/INFINITY_FORGE `
  --merge `
  --match-head-commit $head `
  --delete-branch
```

다음 명령은 Forge의 두 자동 병합 방식을 대신하지 않는다.

```powershell
gh pr merge <PR_NUMBER> --auto
```

GitHub의 `--auto`는 GitHub 조건만 보고 병합한다. Forge의 Task별 선택, 선택한 검사, 파일 규칙, base/head 결합, 만료 시각을 확인하지 않는다. 또한 GitHub merge queue에서는 `gh pr merge`가 의도치 않게 자동 병합을 켤 수 있으므로 Merge Worker는 현재 head를 지정한 REST 병합 요청을 사용한다.

### 11.3 두 자동 방식의 공통 조건

Merge Worker는 병합 직전에 다음을 다시 읽는다.

1. 원본 Task 설정이 여전히 active이고 설정 식별값이 같은가
2. 자동 권한 만료 시각 전인가
3. 선택한 Build·Review·Deep Check 결과가 현재 base/head commit에 묶여 있는가
4. 원본 이슈 상태가 `forge:ready-to-merge`인가
5. PR이 열려 있고 draft·충돌 상태가 아니며 branch 보호 결과가 `clean`인가
6. `protect-main`이 최신 branch 검사, `eval`, 대화 해결을 요구하는가
7. 현재 head에 이름이 `eval`인 check가 정확히 1개 있고 성공했는가
8. 해결되지 않은 Review 대화가 0개인가

하나라도 다르거나 GitHub 응답을 읽지 못하면 병합하지 않는다. base가 전진했다면 branch를 갱신하고 기존 결과를 무효화한 뒤 Build부터 다시 시작한다.

### 11.4 서버 전체 opt-in

두 자동 방식을 선택해도 서버 환경에 정확히 아래 값이 없으면 Merge Worker는 `disabled` 결과만 남기고 병합하지 않는다.

```text
AUTO_MERGE_ENABLED=true
```

대소문자가 다른 `TRUE`, 빈 값, 미설정은 모두 꺼짐이다. production 첫 적용은 Manual 시험 Task로 실제 경로를 확인한 뒤, Safe Files Auto-Merge의 문서 전용 시험 PR부터 별도로 활성화한다.

## 12. 워커 예상 시나리오와 대응

### 시나리오 A: 정상 완료

1. 이슈에 `forge:ready-to-build`을 붙인다.
2. 약 3분 안에 `forge:building`가 된다.
3. Build가 Codex를 실행하고 PR을 만든다.
4. Stop check가 통과한다.
5. 현재 PR base/head가 Build 결과와 같으면 선택에 따라 Review 카드가 자동 생성된다.
6. Review 승인 뒤 선택에 따라 Deep Check 카드가 자동 생성된다.
7. Deep Check는 Task worktree에서 엣지 테스트를 추가하고 같은 PR에 push한다.
8. 선택한 단계 결과가 현재 PR HEAD와 같으면 원본 이슈가 `forge:ready-to-merge`가 된다.
9. 사람이 원본 이슈 라벨·PR·CI·HEAD를 검토하고 Manual로 병합한다.
10. PR이 이슈를 자동으로 닫지 않으면 사람이 이슈를 닫는다.

### 시나리오 B: 테스트 또는 Work result 실패

Stop check가 `TESTS_FAILED:`를 반환한다.

- 예: 테스트 실패, 빈 diff, Work result 누락, 잘못된 JSON 타입
- Build는 오류를 Codex에 다시 넣고 수정한다.
- 미러 카드에는 `--max-retries 4`가 설정되어 있어 4번째 연속 실패에서 차단된다.
- 최종 실패 시 `forge:failed` 또는 `forge:waiting-for-help`를 확인하고 로그를 읽는다.

```powershell
ssh ubuntu@51.222.27.48 'export PATH="$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH"; hermes kanban runs <CARD_ID>'
ssh ubuntu@51.222.27.48 'export PATH="$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH"; hermes kanban log <CARD_ID> --tail 12000'
```

### 시나리오 C: 검문소 자체 오류

Stop check가 `CHECK_ERROR:`를 반환한다.

- 예: GitHub API 장애, Kanban DB 접근 실패, base SHA 손상
- 같은 구현을 반복해도 해결되지 않을 수 있으므로 Codex 재시도 대신 카드가 block된다.
- 사람이 장치 상태를 고친 뒤 `hermes kanban unblock`한다.

### 시나리오 D: 사람의 설계 입력이 필요함

- 실행 전이면 `forge:needs-decision` 이슈 코멘트로 결정하고 `forge:ready-to-build`으로 바꾼다.
- 실행 중이면 카드가 `forge:waiting-for-help`가 된다.
- GitHub에 결정 근거를 남기고 카드 ID를 `hermes kanban unblock`한다.

현재 `forge:needs-decision` 라벨 제거만 감지해 카드를 자동 재개하는 코드는 없다.

### 시나리오 E: 같은 라벨을 두 번 붙임

issue status sync는 `forge-task:OWNER/REPO#N:TASK_SETTINGS_HASH16` 중복 방지 키를 사용한다. 같은 Task 설정이 반복 조회돼도 새 카드가 계속 생기지 않는다.

완료된 루트 카드를 reopen하지 않는다. 반려가 발생하면 부모의 `source_result_hash`가 포함된 새 `forge-step:*:fix:*` 카드가 정확히 하나 생긴다. 같은 timer 입력을 반복해도 동일한 idempotency key 때문에 중복 생성되지 않는다.

### 시나리오 F: GitHub API 또는 미러 장애

- 이슈는 GitHub에 남아 있고 카드 생성이 지연된다.
- 배포 전 production은 다음 2분 주기에, 이 변경 배포 뒤에는 다음 매분 `:30` 주기에 다시 실행한다.
- 3분 이상 `forge:ready-to-build`에 머물면 `forge-mirror.service` 로그와 VPS `gh auth status`를 확인한다.

```powershell
ssh ubuntu@51.222.27.48 'gh auth status'
ssh ubuntu@51.222.27.48 'journalctl --user -u forge-mirror.service -n 100 --no-pager'
```

### 시나리오 G: Gateway가 내려감

- 미러가 카드를 만들 수는 있어도 워커가 배차되지 않아 ready에 머문다.
- `hermes gateway status`를 확인하고 내려가 있을 때만 start한다.

```powershell
ssh ubuntu@51.222.27.48 'export PATH="$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH"; hermes gateway status'
ssh ubuntu@51.222.27.48 'export PATH="$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH"; hermes gateway start'
```

### 시나리오 H: MEMEX가 내려감

- 보내지 못한 MEMEX 메시지 파일은 `pending-messages/`에 그대로 남는다.
- `forge-flush.timer`가 10분마다 재시도한다.
- 작업·PR·Kanban 진행은 MEMEX 때문에 멈추지 않는다.

2026-07-14 VPS Gateway 로그에는 MEMEX MCP 재연결 실패가 관찰됐다. 현재 코드는 이 장애를 비동기 지식 배달 지연으로 처리하므로 Build 작업의 직접 차단 사유는 아니다.

### 시나리오 I: Review 또는 Deep Check가 반려함

- Review `reject`: Deep Check를 만들지 않고 `fix_notes`를 그대로 가진 Fix를 만든다.
- Deep Check `problems_found`: 제품 결함을 정상 결과로 기록하고 Fix를 만든다.
- Fix는 확인된 결과의 HEAD에 고정한 Task worktree에서 기존 PR만 수정한다.
- 수정 뒤 Build부터 선택한 검사를 다시 시작한다. 세 번째 Fix 뒤 또 반려되면 `forge:failed`다.

### 시나리오 J: `eval`이 실패하거나 비정상 종료됨

- `failure`, `timed_out`: exact failed HEAD와 결정적 fix_notes을 가진 같은-PR fix를 만든다.
- fix worker는 먼저 실패한 check를 재현하고 제품 코드나 테스트로 고칠 수 있을 때만 커밋한다.
- 외부 서비스의 일시 장애라 이미 재현되지 않으면 통과용 빈·무의미 커밋을 만들지 않고 카드를 block한다.
- `startup_failure`, `cancelled`, `action_required`, `neutral`, `skipped`, `stale`처럼 자동 수정 의미가 불명확한 completed conclusion은 영구 대기하지 않고 `CHECK_ERROR`로 stop_on_error한다. 사람이 Actions와 stage log를 확인한다.
- CI 재작업도 전체 fix 최대 3개에 포함되며 한도를 넘으면 `forge:failed`다.

### 시나리오 K: main 전진으로 PR branch를 갱신함

1. 원본 이슈가 `forge:ready-to-merge`이어도 GitHub가 branch를 최신 main으로 갱신하라고 하면 아직 병합하지 않는다.
2. **Update branch** 또는 `gh pr update-branch <PR_NUMBER>`를 한 번 실행한다.
3. PR HEAD가 바뀌면 이전 검사 결과는 병합 근거로 재사용되지 않고 라벨이 현재 단계로 돌아간다.
4. 새 base/head에서 Build부터 선택한 검사가 실행되고, 이전 Deep Check가 추가한 테스트가 남았는지도 확인한다.
5. 선택한 검사와 최신 HEAD `eval`까지 통과해 원본 이슈가 다시 `forge:ready-to-merge`일 때만 Manual 병합한다.
6. 기다리는 동안 main이 또 전진하면 같은 과정을 반복한다. 이 반복 비용이 strict ruleset의 통합 안전성 비용이다.

### 시나리오 L: Deep Check 또는 Fix가 중간에 종료됨

- push 전 종료: 같은 카드의 Task worktree와 확인된 HEAD를 다시 검사하고 기존 작업을 재개한다.
- push 후·완료 기록 전 종료: live PR HEAD와 local HEAD가 일치하면 그 커밋을 재사용해 완료하며 새 커밋을 중복 생성하지 않는다.
- live PR HEAD가 확인된 HEAD나 기존 local HEAD와 다르면 외부 변경으로 간주해 자동 덮어쓰기·force-push하지 않고 멈춘다.
- Review/Deep Check 실행 도중 외부 commit이 추가돼 tested commit이 낡아도 결과를 사용하지 않고 CHECK_ERROR로 멈춘다.
- Deep Check가 실제 새 테스트 commit을 만들지 않았거나 `added_tests` 파일이 diff/HEAD에 없으면 완료를 거부한다.

## 13. 이 변경을 활성화하는 1회 절차

### 13.1 GitHub `protect-main` ruleset

main이 자주 전진할 때 선택지는 세 가지다.

| 방법 | 장점 | 단점·3~5수 뒤 결과 | 채택 여부 |
|---|---|---|---|
| strict 끄기 | 구현과 CI 반복이 가장 적다 | 이전 base에서만 green인 PR이 main과 합쳐져 깨질 수 있어 `plan.md`의 최신 통합 검증 의도가 약해진다 | 기각 |
| strict + 갱신된 HEAD 재검사 | exact-HEAD 결과와 최신 main 통합 안전성을 함께 유지한다 | main이 계속 전진하면 선택한 검사 비용이 반복된다 | **현재 채택** |
| GitHub merge queue | 동시 PR이 많을 때 최신 base merge group을 GitHub가 관리한다 | 현재 individual-owned public repo에서는 사용할 수 없고, 조직 이전과 `merge_group` CI 배선이 필요하다 | 장기 후보 |

따라서 현재 최선은 strict를 유지하고 **Update branch가 만든 HEAD를 새 검증 세대**로 취급하는 방식이다. 나중에 조직 저장소로 옮기고 동시 PR 수가 많아지면 merge queue가 더 나은 다음 단계다.

저장소 관리자 권한으로 GitHub 웹에서 다음 순서로 만든다.

1. 저장소 **Settings → Rules → Rulesets → New branch ruleset**을 연다.
2. 이름을 `protect-main`, Enforcement status를 **Active**로 둔다.
3. Target branches에서 **Include default branch**를 선택한다.
4. bypass list는 비워 둔다.
5. **Restrict deletions**와 **Block force pushes**를 켠다.
6. **Require a pull request before merging**를 켜고 required approvals는 `0`으로 둔다.
7. **Require status checks to pass**를 켜고 GitHub Actions가 만든 `eval`을 추가한다.
8. **Require branches to be up to date before merging**도 켠다.
9. 저장한 뒤 ruleset이 Active이고 target이 `~DEFAULT_BRANCH`, bypass가 빈 배열인지 다시 읽는다.

approvals가 0인 이유는 현재 worker와 사람이 같은 GitHub 계정을 써서 PR 작성자가 자기 PR에 공식 Approve를 남길 수 없기 때문이다. 별도 Review 계정이 생기면 1 이상으로 강화한다. strict를 켜도 branch가 갱신되면 Build부터 선택한 검사를 새 HEAD에서 다시 실행한다.

2026-07-15 실제 적용 결과는 ruleset ID `18974841`, enforcement `active`, `current_user_can_bypass=never`다. effective `main` rules API에서도 deletion, non-fast-forward, pull-request, strict required-status-check 네 규칙이 같은 ID로 확인됐다. CI run을 재실행한 검증에서는 `eval=queued`일 때 PR #6이 `BLOCKED`, success 뒤 다시 `MERGEABLE`이 됐다.

CLI로 적용 결과를 읽을 때는 다음 명령을 쓴다.

```powershell
gh api repos/immortal0900/INFINITY_FORGE/rulesets `
  --jq '.[] | {id,name,enforcement,target}'

$rulesetId = 18974841
gh api "repos/immortal0900/INFINITY_FORGE/rulesets/$rulesetId"
```

이 ruleset은 PR 경유·최신 main·`eval` 성공만 강제한다. **원본 이슈의 `forge:ready-to-merge`은 사람이 별도로 확인해야 한다.** `--admin`이나 새 bypass actor로 우회하지 않는다.

### 13.2 production 전환과 EC2·VPS 반영 확인

전체 자동 테스트가 통과한 변경을 사람이 `main`에 병합한 뒤에만 배포한다. feature branch나 로컬 파일을 production에 직접 복사하지 않는다. 원격 저장소가 수정된 상태라면 배포를 중단하고 차이를 먼저 보존·검토한다.

```powershell
$expected = gh api repos/immortal0900/INFINITY_FORGE/commits/main --jq .sha

ssh My-EC2 'cd "$HOME/work/INFINITY_FORGE" && test "$(git branch --show-current)" = main && test -z "$(git status --porcelain)" && git fetch origin main && git pull --ff-only origin main && bash forge/scripts/deploy-vps.sh'
ssh ubuntu@51.222.27.48 'cd "$HOME/work/INFINITY_FORGE" && test "$(git branch --show-current)" = main && test -z "$(git status --porcelain)" && git fetch origin main && git pull --ff-only origin main && bash forge/scripts/deploy-vps.sh'
```

배포 결과를 순서대로 읽어 확인한다.

```powershell
ssh My-EC2 'cd "$HOME/work/INFINITY_FORGE" && git rev-parse HEAD && systemctl --user is-active forge-stage.timer forge-mirror.timer forge-merge.timer'
ssh ubuntu@51.222.27.48 'cd "$HOME/work/INFINITY_FORGE" && git rev-parse HEAD && systemctl --user is-active forge-stage.timer forge-mirror.timer forge-merge.timer'

ssh My-EC2 'cd "$HOME/work/INFINITY_FORGE" && PYTHONPATH="$PWD" python3 forge/scripts/task-flow-worker.py --check-port && PYTHONPATH="$PWD" python3 forge/scripts/issue-status-sync.py --check-port'
ssh ubuntu@51.222.27.48 'cd "$HOME/work/INFINITY_FORGE" && PYTHONPATH="$PWD" python3 forge/scripts/task-flow-worker.py --check-port && PYTHONPATH="$PWD" python3 forge/scripts/issue-status-sync.py --check-port'

ssh My-EC2 'journalctl --user -u forge-stage.service -u forge-mirror.service -u forge-merge.service -n 100 --no-pager'
ssh ubuntu@51.222.27.48 'journalctl --user -u forge-stage.service -u forge-mirror.service -u forge-merge.service -n 100 --no-pager'
```

두 서버의 `git rev-parse HEAD`가 모두 `$expected`와 같고 세 timer가 모두 `active`이며 최근 service 로그에 인자 누락·DB 불일치·GitHub 권한 오류가 없어야 반영 완료다. `--check-port`는 import만 확인하므로 timer가 active인 것만으로 기능 완료를 선언하지 않는다. 마지막으로 작은 **Build + Review + Deep Check + Manual** Task를 실제로 실행한다. 자동 병합은 이 시험과 Safe Files 전용 시험 PR을 통과한 뒤에도 별도 승인 없이 켜지 않는다.

## 14. 현재 환경 점검표

다음 표는 2026-07-15 실측이며 시간이 지나면 바뀔 수 있다.

| 항목 | 실측 상태 | 의미 |
|---|---|---|
| VPS Gateway | active | 클라우드 작업 배차 가능 |
| VPS Dashboard | active | SSH 터널을 통한 Desktop 원격 연결 가능 |
| VPS Forge 타이머 | 2026-07-15 기존 자동화 가동 실측 | 이 변경의 세 새 worker 반영 여부는 13.2절로 다시 확인 필요 |
| VPS Kanban | active 0, done 5 | 현재 실행 중 작업 없음 |
| Windows Codex | ChatGPT 로그인 | 로컬 Codex 사용 가능 |
| Windows GitHub CLI | `immortal0900` 로그인 | 로컬에서 PR·ruleset 조회와 push 가능 |
| Windows local-sync | 실행 중 | 로컬 상태 공유·백업 루프 실행 중 |
| Windows local Gateway | 프로세스 미검출 | `#forge-local` 사용 전 `hermes gateway start` 필요 |
| GitHub 저장소 | public, `protect-main` active, ID `18974841` | PR·strict `eval`·force-push/deletion 차단, bypass 없음 |
| 열린 PR | 2026-07-15 PR #6 스냅샷 | 최신 상태는 GitHub에서 다시 확인 필요 |
| MEMEX MCP | Gateway 재연결 실패 경고 | 지식 배달 지연, 코드 작업은 계속 가능 |

## 15. 매일 사용하는 최소 체크리스트

### 작업을 넣을 때

- [ ] 이슈 목적과 SoT 근거가 있는가
- [ ] 모든 AC가 명령·테스트로 판정 가능한가
- [ ] 범위 제외가 적혀 있는가
- [ ] Task를 선택했는가
- [ ] 실행 단계와 병합 방식을 이번 Task에 맞게 새로 골랐는가
- [ ] 미리보기 내용을 다시 읽고 최종 확인했는가

### 알림을 받았을 때

- [ ] `forge:waiting-for-help`: 질문에 답하고 카드도 unblock했는가
- [ ] `forge:failed`: runs와 log에서 마지막 실패를 확인했는가
- [ ] `forge:ready-to-merge`: 원본 이슈 라벨, PR 링크, diff, CI, 이슈 AC를 대조했는가

### 병합할 때

- [ ] 원본 이슈의 현재 라벨이 `forge:ready-to-merge`인가
- [ ] CI가 모두 green인가
- [ ] branch 갱신이 필요하면 새 base/head에서 Build부터 선택한 검사가 끝날 때까지 기다렸는가
- [ ] 검토한 head SHA와 병합할 head SHA가 같은가
- [ ] 공개 API·DB·의존성·배포·보안 변경의 복구 방법이 있는가
- [ ] `--admin`이나 red check 무시를 사용하지 않았는가
- [ ] 병합 후 원래 이슈가 닫혔는가

## 16. 구현 근거

- GitHub 수입·표시 상태 계산: `forge/scripts/issue-status-sync.py`
- 단계 결정·CI 실패 복구: `forge/ops/task_flow.py`
- 단계 결과 확인·카드 생성: `forge/scripts/task-flow-worker.py`
- 단일 공식 상태 라벨 계산: `forge/ops/displayed_status.py`
- Build 위임·재시도·결과: `forge/skills/build-task/SKILL.md`
- 종료 검문: `forge/hooks/codex-work-check.sh`
- Review 규약: `forge/skills/review-task/SKILL.md`
- Deep Check 규약: `forge/skills/deep-check/SKILL.md`
- 상태 라벨 규약: `forge/skills/forge-labels/SKILL.md`
- VPS 서비스·타이머 설치: `forge/scripts/deploy-vps.sh`
- Desktop 터널: `docs/setup/vps-dashboard-tunnel.cmd`
- Desktop token 갱신: `docs/setup/fetch-dashboard-token.ps1`
- PR CI: `.github/workflows/capability-eval.yml`

GitHub 화면과 CLI의 공식 사용법은 다음 문서를 기준으로 확인했다.

- [Creating an issue - GitHub Docs](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/creating-an-issue)
- [Reviewing proposed changes in a pull request - GitHub Docs](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/reviewing-changes-in-pull-requests/reviewing-proposed-changes-in-a-pull-request)
- [Merging a pull request - GitHub Docs](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/incorporating-changes-from-a-pull-request/merging-a-pull-request)
- [GitHub CLI `gh pr merge`](https://cli.github.com/manual/gh_pr_merge)
- [Keeping a pull request in sync with the base branch](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/proposing-changes-to-your-work-with-pull-requests/keeping-your-pull-request-in-sync-with-the-base-branch)
- [Available rules for rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets)

**한마디 요약**: Chat/Task 선택, 세 실행 단계, 세 병합 방식과 실제 Task·GitHub·Hermes worker 연결은 저장소에 구현됐다. 자동 병합은 기본적으로 꺼져 있으며, production 완료는 EC2와 VPS의 commit·timer·실제 Manual 시험 Task를 모두 확인한 뒤에만 선언한다.

## 변경이력

- 2026-07-17 | Task 입력 표준 양식 반영 | 변경: Task 내용 프롬프트와 6.2절을 SPEC·AC ID 기반 양식으로 통일 | 검증: `uv run --no-project --with pytest python -m pytest tests/ops/test_task_setup.py -q`
