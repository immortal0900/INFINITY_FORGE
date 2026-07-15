# INFINITY_FORGE 실제 사용 가이드

> 기준일: 2026-07-15
>
> 검증 기준: 이 저장소의 추적된 코드, Windows에 설치된 Hermes Agent v0.18.2, 현재 VPS와 GitHub의 읽기 전용 실측 결과
> 대상: INFINITY_FORGE를 직접 켜고, 작업을 넣고, 승인하고, 결과를 병합하려는 운영자

## 0. 먼저 결론

**클라우드 자동화는 이미 VPS에서 24시간 실행 중이므로, 매번 서버 프로그램을 새로 켤 필요가 없다.** 평소에는 다음 다섯 단계만 수행한다.

1. GitHub에 수용 기준이 있는 이슈를 만든다.
2. 검토가 끝난 이슈에 `forge:need-execution` 라벨을 붙인다.
3. 시스템이 executor→reviewer→critic을 실행해 **원본 이슈가 `forge:mergeable`이 될 때까지** 기다린다.
4. 원본 이슈의 `forge:mergeable`, PR diff, 현재 HEAD와 CI를 직접 확인한다.
5. 이상이 없으면 사람이 PR을 병합하고, 자동으로 닫히지 않은 이슈를 닫는다.

배포 후 자동 경로는 `executor → reviewer → critic → forge:mergeable`까지 진행한다. reviewer 반려, critic의 결함 발견, 재현 가능한 `eval` 실패는 같은 PR의 새 executor-rework 카드로 되돌아간다. strict ruleset 때문에 PR branch를 갱신하면 새 HEAD에서 reviewer→critic을 다시 실행한다. **실제 PR 병합은 여전히 P1 사람 승인이다.** P2/P3 자동 머지는 구현 범위가 아니므로 이를 켜는 명령은 없다.

> 이 문서의 stage-orchestrator 절차는 이 변경 PR이 `main`에 병합되고 `forge/scripts/deploy-vps.sh`를 실행한 뒤 활성화된다. 병합 전 production VPS는 이전 동작을 유지한다.

## 1. 지금 실제로 되는 것과 안 되는 것

| 기능 | 현재 상태 | 코드 근거 또는 실측 |
|---|---|---|
| VPS Gateway와 Dashboard 상주 | 가동 중 | `hermes-gateway.service`, `hermes-dashboard.service`가 active |
| GitHub 이슈 자동 수입 | 구현·가동 | 현재 production은 2분 주기, 이 변경 배포 뒤 mirror는 매분 `:30`에 조회 |
| executor 워커 자동 실행 | 구현·가동 | 미러가 executor 카드 생성, Gateway 디스패처가 기본 60초마다 배차 |
| Codex 종료 검문 | 구현 | 빈 diff, 테스트, exact 5-field `handoff.json`을 `forge/hooks/codex-stop-gate.sh`가 검사 |
| P1 사람 머지 | 구현 | `forge:mergeable` 뒤 사람이 GitHub에서 최종 병합 |
| reviewer·critic 역할 | 구현 | strict JSON 결과와 부모 receipt·PR HEAD binding 사용 |
| executor→reviewer→critic 자동 연결 | **구현, 배포 대기** | `stage-reconciler.py`가 유일한 frontier leaf에서 다음 카드를 멱등 생성 |
| reviewer/critic 반려 재작업 | **구현, 배포 대기** | 새 executor-rework 카드 생성, 최대 3개 뒤 `forge:failed` |
| `forge:need-critic`·`forge:mergeable` 자동 전이 | **구현, 배포 대기** | critic pass + live 결과 HEAD의 정확한 `eval=success`에서만 mergeable; 갱신된 HEAD는 재검증 |
| P2 위험도 분류·부분 자동 머지 | **설계만 있음** | 실행 스크립트, 설정 저장소, 머지 워커가 없음 |
| P3 전면 자동 머지 | **설계만 있음** | 자동 머지 호출 코드가 없음 |
| `forge:automerge-ok` 태스크 예외 | **미구현** | 현재 GitHub 라벨 목록과 실행 코드에 없음 |
| `forge:adr` 자동 왕복 | **부분 구현** | 라벨 정의는 있으나 결정 후 카드를 자동 재개하는 연결 코드 없음 |
| GitHub main 보호 | **이번 작업에서 적용** | `protect-main`: PR 필수, approvals 0, strict `eval`, bypass 없음; 실제 ID는 13장에 기록 |

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

### 5.4 Desktop에서 작업을 바로 만들어도 되는가

가능하더라도 클라우드 제품 작업의 기본 경로로 권장하지 않는다. GitHub 이슈를 거치지 않은 카드는 다음 정보가 빠질 수 있다.

- GitHub 이슈 URL
- 수용 기준의 원본
- `github-issue:OWNER/REPO#N` 멱등키
- 이슈 라벨과 카드 상태의 자동 연결

따라서 Desktop은 보드·세션 관제와 대화에 사용하고, 클라우드 작업 투입은 다음 GitHub 절차를 사용한다.

## 6. GitHub에서 작업을 넣는 전체 절차

### 6.1 방법 A: GitHub 웹에서 만들기

1. [INFINITY_FORGE Issues](https://github.com/immortal0900/INFINITY_FORGE/issues)를 연다.
2. **New issue**를 누른다.
3. **Forge 스펙 이슈** 옆의 **Get started**를 누른다.
4. 제목을 `[SPEC-NNN] 구체적인 작업명` 형식으로 쓴다.
5. 본문의 네 영역을 채운다.
   - **목적**: 왜 필요한지 1~3문장
   - **SoT 근거**: 기준 문서의 파일명과 절, 필요한 원문
   - **수용 기준(AC)**: 명령이나 테스트로 참·거짓을 판정할 수 있는 체크리스트
   - **범위 제외**: 이번 작업에서 하지 않는 내용
6. **Submit new issue**를 누른다.
7. 다시 읽고 범위와 AC가 맞는지 확인한다.
8. 오른쪽 **Labels**에서 `forge:need-execution`을 붙인다.

라벨을 붙이는 8단계가 실제 작업 투입 승인이다. 그 전까지는 워커가 실행되지 않는다.

### 6.2 좋은 수용 기준 예시

```markdown
## 목적
사용자가 잘못된 설정 파일을 넣었을 때 조용히 기본값으로 진행하지 않고 원인을 알 수 있어야 한다.

## SoT 근거
docs/spec.md 4.2절 "설정 오류는 즉시 실패로 반환한다."

## 수용 기준 (AC)
- [ ] 잘못된 YAML 입력 시 프로세스가 exit code 2를 반환한다.
- [ ] stderr가 `GATE_ERROR:`로 시작한다.
- [ ] 해당 동작을 재현하는 pytest가 추가되고 `pytest tests/ -q`가 통과한다.

## 범위 제외
- 설정 파일 형식 변경은 하지 않는다.
```

`잘 동작한다`, `깔끔하게 만든다`, `적절히 처리한다`처럼 판정 방법이 없는 문장은 AC로 사용하지 않는다.

### 6.3 방법 B: PowerShell과 `gh`로 만들기

```powershell
$body = @'
## 목적
작업 목적을 적는다.

## SoT 근거
기준 문서의 파일명과 절을 적는다.

## 수용 기준 (AC)
- [ ] 검증 가능한 조건 1
- [ ] 검증 가능한 조건 2

## 범위 제외
- 이번에 하지 않는 것
'@

gh issue create `
  --repo immortal0900/INFINITY_FORGE `
  --title "[SPEC-NNN] 구체적인 작업명" `
  --body $body
```

출력된 URL의 이슈 번호가 `12`라면 내용을 먼저 확인한다.

```powershell
gh issue view 12 --repo immortal0900/INFINITY_FORGE --web
```

확정 후 투입한다.

```powershell
gh issue edit 12 `
  --repo immortal0900/INFINITY_FORGE `
  --add-label "forge:need-execution"
```

이슈 생성과 투입을 두 명령으로 나누는 이유는 AC를 잘못 쓴 이슈가 생성 즉시 실행되는 것을 막기 위해서다.

## 7. 투입 후 무슨 일이 일어나는가

정상 시 예상 순서는 다음과 같다.

1. `forge:need-execution` 라벨이 붙는다.
2. 배포 뒤 최대 약 1분 내 `label-mirror.py`가 이슈를 발견한다. 배포 전 production 주기는 2분이다.
3. 다음 정보를 가진 executor 카드가 생성된다.
   - 제목: `[mirror] <이슈 제목>`
   - assignee: `executor`
   - workspace: `/home/ubuntu/work/INFINITY_FORGE`
   - 멱등키: `github-issue:immortal0900/INFINITY_FORGE#<번호>`
   - 최대 연속 실패 한도: 4번째 실패에서 차단
   - goal loop: 최대 20턴
4. Gateway의 다음 디스패처 틱에서 카드가 실행된다. 기본 간격은 60초다.
5. executor가 작업 시작 SHA를 기록하고 tmux에서 `codex exec`를 실행한다.
6. Codex가 변경·테스트·PR·`handoff.json`을 만든다.
7. Stop gate가 다음을 검사한다.
   - 실제 구현 파일 변경이 있는가
   - 저장소 유형에 맞는 테스트가 통과하는가
   - `implemented`가 비어 있지 않은가
   - `not_implemented`가 JSON 배열인가
   - 모든 구현 항목이 `verified_by`로 덮이는가
   - 남은 작업이 있으면 실제 후속 이슈·카드 ID가 있는가
8. 통과하면 executor 카드가 done이 된다.
9. `forge-stage.timer`가 현재 PR HEAD의 `eval=success`를 확인하고 reviewer 자식 카드를 정확히 1개 만든다.
10. reviewer는 별도 Hermes task/session에서 executor 보고서와 실제 diff·AC를 대조한다.
11. reviewer approve이면 critic 자식 카드가 생긴다. reject이면 reflection을 포함한 executor-rework 카드가 생기며 critic은 만들지 않는다.
12. critic은 별도 task 전용 worktree에서 적대적 테스트를 추가하고 같은 PR branch에 push한다.
13. critic pass이고 `result_head_sha == live PR HEAD`이며 그 SHA의 `eval` check가 정확히 1개·success이면 이슈가 `forge:mergeable`이 된다.
14. critic `defect_found` 또는 `eval=failure|timed_out`이면 같은 PR의 새 executor-rework로 돌아간다. 재작업 카드는 최대 3개다. 외부 일시 장애라 재현되지 않으면 의미 없는 커밋을 만들지 않고 block한다. `startup_failure` 등 자동 수정 의미가 불명확한 결론은 GATE_ERROR다.
15. `forge:mergeable` 뒤 main이 전진해 **Update branch**로 PR HEAD가 바뀌면 mergeable을 제거하고, 새 HEAD의 `eval` 성공 뒤 fresh reviewer→critic을 다시 실행한다.
16. 원본 이슈가 다시 `forge:mergeable`일 때 사람이 diff·CI·HEAD를 확인하고 P1로 병합한다.

이론상 각 자동 전이는 mirror/stage timer와 Gateway 배차 간격 때문에 수 분 걸릴 수 있다. 실제 코드 작업 시간은 작업 크기, reviewer/critic 결과, 재작업 횟수에 따라 달라진다.

오케스트레이터를 읽기 전용으로 한 번 점검하려면 다음 명령을 쓴다.

```powershell
ssh ubuntu@51.222.27.48 'cd "$HOME/work/INFINITY_FORGE" && PYTHONPATH="$PWD" python3 forge/scripts/stage-reconciler.py --dry-run'
```

즉시 한 번 실행하려면 production 배포 뒤 다음을 사용한다. 카드 생성 가능성이 있으므로 명령과 대상 저장소를 확인하고 승인한다.

```powershell
ssh ubuntu@51.222.27.48 'systemctl --user start forge-stage.service'
```

## 8. 상태를 확인하는 방법

### 8.1 GitHub 라벨 해석

| 라벨 | 지금 무엇을 뜻하는가 | 사용자가 할 일 |
|---|---|---|
| `forge:spec-draft` | 스펙 보완 대기 | 목적·근거·AC 보완 |
| `forge:adr` | 사람의 설계 결정 대기 | 이슈 코멘트로 결정 기록 후 아래 10장 절차 수행 |
| `forge:need-execution` | 아직 수입 전이거나 ready/todo | 3분 이상 지속되면 미러·Gateway 점검 |
| `forge:in-progress` | executor가 실행 중 | 보통 기다림, 필요 시 카드 로그 확인 |
| `forge:need-review` | reviewer 카드가 ready/running이거나 executor 완료 뒤 reviewer 생성 대기 | 보통 기다림. 오래 지속되면 stage timer와 reviewer log 확인 |
| `forge:need-critic` | reviewer approve 뒤 critic 실행 중, 또는 critic pass 뒤 최신 HEAD CI 대기 | 보통 기다림. actionable red CI는 자동 rework, 그 밖의 비정상 conclusion은 gate error로 사람이 로그 확인 |
| `forge:mergeable` | critic pass + critic 결과 HEAD가 live HEAD + 그 SHA의 `eval=success` | 원본 이슈의 이 라벨을 확인한 뒤 사람이 P1 최종 검토·병합 |
| `forge:blocked` | 입력·의존성·장치 문제로 정지 | 사유 확인 후 카드 unblock |
| `forge:failed` | 재시도 한도 소진 | 로그 확인 후 새 후속 이슈 권장 |

사람이 직접 붙이는 정상 실행 라벨은 최초의 `forge:need-execution`이다. 카드가 만들어진 뒤의 `forge:*` 라벨은 미러가 카드 상태를 기준으로 다시 쓰므로 임의로 바꿔도 다음 주기에 되돌아갈 수 있다.

### 8.2 GitHub CLI로 보기

```powershell
gh issue list `
  --repo immortal0900/INFINITY_FORGE `
  --state open `
  --search "label:forge:in-progress OR label:forge:need-review OR label:forge:blocked OR label:forge:failed"

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

### 9.1 현재 P1에서 승인이라는 말의 정확한 의미

현재 워커와 사람이 같은 GitHub 계정을 사용해 PR을 만들 수 있다. GitHub는 PR 작성자가 자신의 PR에 공식 `Approve` 리뷰를 남기는 것을 허용하지 않는다. 따라서 현재 P1에서 사람 승인은 다음 행위의 조합이다.

1. **원본 이슈**의 현재 라벨이 `forge:mergeable`인지 확인한다.
2. 사람이 `Files changed`, 현재 HEAD와 CI를 확인한다.
3. 문제가 있으면 코멘트하고 병합하지 않는다.
4. 문제가 없으면 사람이 직접 **Merge pull request**를 누른다.

`forge:mergeable`은 PR 라벨이 아니라 원본 이슈 라벨이다. GitHub ruleset은 PR 경유와 `eval` 성공을 강제하지만 이 Forge 라벨까지 검사하지는 않는다. 원본 이슈가 `forge:need-review` 또는 `forge:need-critic`이면 GitHub의 merge 버튼이 활성화돼 있어도 병합하지 않는다.

독립된 공식 Approve를 플랫폼에서 강제하려면 별도 리뷰 계정 또는 협업자가 필요하다. 현재 `protect-main` ruleset은 단일 계정 운용 때문에 required approving reviews를 0으로 두되, **PR 경유와 최신 branch의 `eval` 성공은 GitHub가 강제**하도록 설계했다. bypass actor는 두지 않는다.

### 9.2 GitHub 웹에서 검토·머지

1. 저장소의 **Pull requests**를 열고 대상 PR을 선택한다.
2. **Conversation**에서 원본 이슈 링크를 연다.
3. 원본 이슈의 현재 라벨이 `forge:mergeable`인지 확인한다. `forge:need-review`·`forge:need-critic`이면 기다린다.
4. PR에 **Update branch**가 표시되면 한 번 누르고 병합하지 않는다. 새 HEAD의 `eval`→fresh reviewer→critic이 끝나 원본 이슈가 다시 `forge:mergeable`이 될 때까지 기다린다.
5. **Files changed**에서 파일별 diff를 확인한다.
6. 필수 check 이름 `eval`이 green인지 확인한다.
7. 다음 항목 중 하나라도 있으면 병합하지 않는다.
   - red 또는 pending check
   - 이슈 AC와 맞지 않는 변경
   - 설명되지 않은 파일 변경
   - migration, 공개 API, 의존성, 배포·보안 설정 변경인데 복구 계획 없음
8. 이상이 없으면 **Merge pull request → Confirm merge**를 누른다.
9. PR이 이슈를 자동으로 닫지 않았다면 원래 이슈를 직접 닫는다.

`protect-main` 적용 뒤에는 red/pending `eval`이나 최신 main을 반영하지 않은 branch를 GitHub가 병합 불가로 표시한다. **Update branch**는 검증 대상 코드를 바꾸는 행위이므로 이전 `forge:mergeable`을 재사용하지 않는다. main이 다시 전진하면 같은 갱신·재검증을 반복하며 `--admin` 또는 ruleset bypass를 사용하지 않는다.

### 9.3 PowerShell에서 검토·머지

원본 이슈 번호가 `12`, PR 번호가 `7`이라고 가정한다. 먼저 원본 이슈 라벨을 확인한다.

```powershell
$labels = gh issue view 12 `
  --repo immortal0900/INFINITY_FORGE `
  --json labels `
  --jq '.labels[].name'

if ($labels -notcontains 'forge:mergeable') {
  throw '원본 이슈가 forge:mergeable이 아니므로 병합하지 않습니다.'
}

gh pr view 7 --repo immortal0900/INFINITY_FORGE --web
gh pr diff 7 --repo immortal0900/INFINITY_FORGE
gh pr checks 7 --repo immortal0900/INFINITY_FORGE --watch
```

GitHub가 branch 갱신을 요구하면 웹의 **Update branch**를 누르거나 다음 명령으로 main을 반영한 뒤, 이슈가 다시 `forge:mergeable`이 될 때까지 기다린다.

```powershell
gh pr update-branch 7 --repo immortal0900/INFINITY_FORGE
```

CI와 자동 reviewer·critic이 모두 통과한 뒤 이슈 라벨을 다시 조회하고, 검토한 head SHA와 실제 병합 대상을 고정한다.

```powershell
$labels = gh issue view 12 `
  --repo immortal0900/INFINITY_FORGE `
  --json labels `
  --jq '.labels[].name'

if ($labels -notcontains 'forge:mergeable') {
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

reviewer는 `reject`와 비어 있지 않은 reflection을 정상 완료 결과로 제출한다. 오케스트레이터는 critic을 만들지 않고 같은 PR에 연결된 새 executor-rework 카드를 만든다. critic도 제품 결함을 찾으면 `blocked`가 아니라 `defect_found`로 완료하고 같은 재작업 경로를 사용한다.

- 사용자가 같은 이슈에 `forge:need-execution`을 다시 붙이거나 완료 카드를 삭제할 필요가 없다.
- rework worker는 reflection을 그대로 받고 기존 PR branch에서 수정한다. 새 PR을 만들면 계약 위반이다.
- rework 카드가 3개인 상태에서 다시 반려되면 새 카드를 만들지 않고 `forge:failed`가 된다.
- 범위 자체가 바뀌었거나 원래 AC가 틀렸다면 자동 재작업 대신 새 후속 이슈로 분리한다.

## 10. 설계 결정과 blocked 작업을 승인하는 방법

### 10.1 실행 전 ADR 결정

아직 카드가 생성되지 않은 이슈가 `forge:adr` 상태라면 다음 순서로 처리한다.

1. 가능한 접근을 최소 2~3개 비교한다.
2. 선택한 접근, 선택 이유, 버린 접근, 복구 조건을 이슈 코멘트에 기록한다.
3. `forge:adr`를 제거하면서 `forge:need-execution`을 붙인다.

```powershell
gh issue comment 12 `
  --repo immortal0900/INFINITY_FORGE `
  --body "결정: 접근 B. 이유: 공개 API를 유지하고 롤백이 파일 복원 1회로 끝남. 접근 A는 스키마 변경 때문에 제외. 실패 시 기존 구현으로 revert."

gh issue edit 12 `
  --repo immortal0900/INFINITY_FORGE `
  --remove-label "forge:adr" `
  --add-label "forge:need-execution"
```

`forge:adr` 제거만으로는 현재 미러가 카드를 만들지 않는다. 반드시 실행할 준비가 끝났다면 `forge:need-execution`도 붙인다.

### 10.2 실행 중 blocked 카드 재개

작업 중 입력이 필요해 카드가 blocked가 되면 GitHub 라벨은 `forge:blocked`가 된다.

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

unblock 뒤 카드는 ready로 돌아가며 다음 디스패처 틱에서 다시 실행된다. 미러는 ready 상태를 `forge:need-execution`으로 투영한다.

## 11. P1·P2·P3는 구체적으로 어떻게 사용하는가

### 11.1 P1~P3와 Kanban priority는 다른 개념

- P1/P2/P3: **PR 머지 권한 정책**
- `hermes kanban create --priority <숫자>`: 여러 카드 중 먼저 배차할 작업을 정하는 숫자

`--priority P1`처럼 입력하는 명령은 없다. GitHub 미러가 만드는 카드에는 `--priority`를 전달하지 않으므로 현재 자동 투입 카드의 priority는 기본값 `0`이다.

세 정책의 운영상 차이는 다음과 같다.

| 정책 | 장점 | 단점·6개월 뒤 실패 모습 | 현재 판단 |
|---|---|---|---|
| P1 사람 머지 | 요구 오해와 고위험 변경을 마지막에 사람이 막을 수 있고 복구가 가장 쉽다 | PR마다 검토 시간이 들고 처리량이 가장 낮다 | **현재 유일한 구현 경로이자 추천** |
| P2 저위험만 자동 머지 | 처리량과 안전성의 균형이 좋다 | 위험 규칙 누락 시 위험 PR이 자동 통과하며 분류기·만료 auth 유지보수가 필요하다 | 목표 정책, 아직 미구현 |
| P3 전면 자동 머지 | 처리량이 가장 높고 사람 대기가 없다 | 요구 오해·권한 사고의 폭발 반경이 가장 크며 사후 revert 비용이 높다 | 목표 정책, 현재 사용 금지 |

현재 최선은 P1이다. 향후 P2를 구현하더라도 만료되는 승인과 PR별 opt-in부터 도입하고, P3는 충분한 운영 데이터와 독립 승인 주체가 생기기 전에는 열지 않는 편이 `plan.md`의 fail-safe 의도에 맞다.

### 11.2 지금 실행할 수 있는 명령

| 정책 | 현재 사용법 | 실제 결과 |
|---|---|---|
| P1 | 별도 모드 전환 명령 없음. 기본값으로 운영 후 사람이 `gh pr merge` | **정상 사용 가능** |
| P2 | 유효한 Forge 명령 없음 | 위험도 분류와 자동 머지가 실행되지 않음 |
| P3 | 유효한 Forge 명령 없음 | 전면 자동 머지가 실행되지 않음 |

P1의 실제 명령은 다음이다.

```powershell
# 작업 투입
gh issue edit <ISSUE_NUMBER> `
  --repo immortal0900/INFINITY_FORGE `
  --add-label "forge:need-execution"

# 원본 이슈가 forge:mergeable인지 9.3절대로 확인하고, 사람이 diff와 CI를 확인한 뒤 최종 병합
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

다음 명령은 P2/P3의 대체가 아니다.

```powershell
gh pr merge <PR_NUMBER> --auto
```

GitHub의 `--auto`는 GitHub 병합 조건이 충족되면 병합하는 기능일 뿐이다. Forge의 P2 파일 위험도 분류, critic 검증, 세션별 사람 auth를 수행하지 않는다.

### 11.3 P2/P3를 실제로 구현할 때 가능한 세 접근

현재 없는 명령을 문서만으로 만들 수는 없다. 구현을 추가한다면 다음 세 방향이 본질적으로 다르다.

| 접근 | 동작 | 3~5수 뒤 결과 |
|---|---|---|
| 세션 한정 정책 명령 | 예: 정책 레코드에 P2/P3, 승인자, 시작·만료 시각 기록 | 시간이 지나면 자동 P1 복귀, 감사 가능. 구현량은 가장 큼 |
| GitHub repository variable | 저장소 전역 값을 P1/P2/P3로 설정 | 구현은 단순하지만 6개월 뒤 켜진 P3를 잊을 위험이 큼 |
| PR·이슈별 opt-in 라벨 | 특정 작업만 자동 머지 허용 | 폭발 범위는 작지만 밤 전체를 P2/P3로 전환하는 기능은 아님 |

추천은 **만료되는 세션 정책 + PR별 opt-in을 함께 사용하는 방식**이다.

1. 사람이 P2/P3를 승인하면 서명된 정책 레코드가 만들어진다.
2. 레코드에는 최대 한 밤 같은 짧은 만료 시간이 들어간다.
3. P2는 결정론적 위험 분류기가 안전하다고 판정한 PR만 통과시킨다.
4. 머지 워커는 CI green, critic 증거, 현재 head SHA, 미만료 auth를 모두 확인한다.
5. 다음 날 자동으로 P1로 돌아간다.

repository variable 하나만 영구 변경하면 초기에는 편하지만, 담당자가 바뀌거나 운영을 몇 달 쉬었다 재개할 때 과거 P3가 조용히 살아나는 실패 시나리오가 생긴다. 되돌리기 어려운 머지 권한은 만료와 감사 기록이 필요하다.

## 12. 워커 예상 시나리오와 대응

### 시나리오 A: 정상 완료

1. 이슈에 `forge:need-execution`을 붙인다.
2. 약 3분 안에 `forge:in-progress`가 된다.
3. executor가 Codex를 실행하고 PR을 만든다.
4. Stop gate가 통과한다.
5. 현재 HEAD의 `eval`이 green이면 reviewer 카드가 자동 생성된다.
6. reviewer approve 뒤 critic 카드가 자동 생성된다.
7. critic이 task worktree에서 엣지 테스트를 추가하고 같은 PR에 push한다.
8. critic 결과 HEAD의 `eval`이 green이면 원본 이슈가 `forge:mergeable`이 된다.
9. 사람이 원본 이슈 라벨·PR·CI·HEAD를 검토하고 P1로 병합한다.
10. PR이 이슈를 자동으로 닫지 않으면 사람이 이슈를 닫는다.

### 시나리오 B: 테스트 또는 handoff 실패

Stop gate가 `TESTS_FAILED:`를 반환한다.

- 예: 테스트 실패, 빈 diff, `handoff.json` 누락, 잘못된 JSON 타입
- executor는 오류를 Codex에 다시 넣고 수정한다.
- 미러 카드에는 `--max-retries 4`가 설정되어 있어 4번째 연속 실패에서 차단된다.
- 최종 실패 시 `forge:failed` 또는 `forge:blocked`를 확인하고 로그를 읽는다.

```powershell
ssh ubuntu@51.222.27.48 'export PATH="$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH"; hermes kanban runs <CARD_ID>'
ssh ubuntu@51.222.27.48 'export PATH="$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH"; hermes kanban log <CARD_ID> --tail 12000'
```

### 시나리오 C: 검문소 자체 오류

Stop gate가 `GATE_ERROR:`를 반환한다.

- 예: GitHub API 장애, Kanban DB 접근 실패, base SHA 손상
- 같은 구현을 반복해도 해결되지 않을 수 있으므로 Codex 재시도 대신 카드가 block된다.
- 사람이 장치 상태를 고친 뒤 `hermes kanban unblock`한다.

### 시나리오 D: 사람의 설계 입력이 필요함

- 실행 전이면 `forge:adr` 이슈 코멘트로 결정하고 `forge:need-execution`으로 바꾼다.
- 실행 중이면 카드가 `forge:blocked`가 된다.
- GitHub에 결정 근거를 남기고 카드 ID를 `hermes kanban unblock`한다.

현재 `forge:adr` 라벨 제거만 감지해 카드를 자동 재개하는 코드는 없다.

### 시나리오 E: 같은 라벨을 두 번 붙임

미러는 `github-issue:OWNER/REPO#N` 멱등키를 사용한다. 같은 이슈가 반복 조회돼도 새 카드가 계속 생기지 않는다.

완료된 루트 카드를 reopen하지 않는다. 반려가 발생하면 부모 결과 digest가 포함된 새 `forge-stage:*:executor-rework:*` 카드가 정확히 하나 생긴다. 같은 timer 입력을 반복해도 동일한 idempotency key 때문에 중복 생성되지 않는다.

### 시나리오 F: GitHub API 또는 미러 장애

- 이슈는 GitHub에 남아 있고 카드 생성이 지연된다.
- 배포 전 production은 다음 2분 주기에, 이 변경 배포 뒤에는 다음 매분 `:30` 주기에 다시 실행한다.
- 3분 이상 `forge:need-execution`에 머물면 `forge-mirror.service` 로그와 VPS `gh auth status`를 확인한다.

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

- 지식 저장 outbox 파일은 그대로 남는다.
- `forge-flush.timer`가 10분마다 재시도한다.
- 작업·PR·Kanban 진행은 MEMEX 때문에 멈추지 않는다.

2026-07-14 VPS Gateway 로그에는 MEMEX MCP 재연결 실패가 관찰됐다. 현재 코드는 이 장애를 비동기 지식 배달 지연으로 처리하므로 executor 작업의 직접 차단 사유는 아니다.

### 시나리오 I: reviewer 또는 critic이 반려함

- reviewer `reject`: critic을 만들지 않고 reflection을 그대로 가진 executor-rework를 만든다.
- critic `defect_found`: 제품 결함을 정상 완료 결과로 기록하고 executor-rework를 만든다.
- rework worker는 receipt HEAD에 고정한 task worktree에서 기존 PR만 수정한다.
- 수정 뒤 다시 reviewer부터 시작한다. 세 번째 rework 뒤 또 반려되면 `forge:failed`다.

### 시나리오 J: `eval`이 실패하거나 비정상 종료됨

- `failure`, `timed_out`: exact failed HEAD와 결정적 reflection을 가진 같은-PR executor-rework를 만든다.
- rework worker는 먼저 실패한 check를 재현하고 제품 코드나 테스트로 고칠 수 있을 때만 커밋한다.
- 외부 서비스의 일시 장애라 이미 재현되지 않으면 통과용 빈·무의미 커밋을 만들지 않고 카드를 block한다.
- `startup_failure`, `cancelled`, `action_required`, `neutral`, `skipped`, `stale`처럼 자동 수정 의미가 불명확한 completed conclusion은 영구 대기하지 않고 `GATE_ERROR`로 fail-closed한다. 사람이 Actions와 stage log를 확인한다.
- CI 재작업도 전체 rework 최대 3개에 포함되며 한도를 넘으면 `forge:failed`다.

### 시나리오 K: main 전진으로 PR branch를 갱신함

1. 원본 이슈가 `forge:mergeable`이어도 GitHub가 branch를 최신 main으로 갱신하라고 하면 아직 병합하지 않는다.
2. **Update branch** 또는 `gh pr update-branch <PR_NUMBER>`를 한 번 실행한다.
3. PR HEAD가 바뀌면 이전 critic pass는 merge 증거로 재사용되지 않고 라벨이 `forge:need-review`/`forge:need-critic`으로 돌아간다.
4. 새 HEAD의 `eval` 성공 뒤 fresh reviewer가 생성되고, 이전 critic이 추가한 테스트가 여전히 남았는지도 확인한다.
5. 새 critic과 최신 HEAD `eval`까지 통과해 원본 이슈가 다시 `forge:mergeable`일 때만 P1 병합한다.
6. 기다리는 동안 main이 또 전진하면 같은 과정을 반복한다. 이 반복 비용이 strict ruleset의 통합 안전성 비용이다.

### 시나리오 L: critic 또는 rework worker가 중간에 죽음

- push 전 종료: 같은 카드의 task worktree와 receipt HEAD를 다시 검증하고 기존 작업을 재개한다.
- push 후·완료 기록 전 종료: live PR HEAD와 local HEAD가 일치하면 그 커밋을 재사용해 완료하며 새 커밋을 중복 생성하지 않는다.
- live PR HEAD가 receipt HEAD나 기존 local HEAD와 다르면 외부 변경으로 간주해 자동 덮어쓰기·force-push하지 않고 block한다.
- reviewer/critic 실행 도중 외부 커밋이 추가돼 bound HEAD가 낡아도 결과를 사용하지 않고 gate error로 막는다.
- critic이 실제 새 테스트 커밋을 만들지 않았거나 `added_tests` 파일이 diff/HEAD에 없으면 완료를 거부한다.

## 13. 이 변경을 활성화하는 1회 절차

### 13.1 GitHub `protect-main` ruleset

main이 자주 전진할 때 선택지는 세 가지다.

| 방법 | 장점 | 단점·3~5수 뒤 결과 | 채택 여부 |
|---|---|---|---|
| strict 끄기 | 구현과 CI 반복이 가장 적다 | 이전 base에서만 green인 PR이 main과 합쳐져 깨질 수 있어 `plan.md`의 최신 통합 검증 의도가 약해진다 | 기각 |
| strict + updated HEAD fresh reviewer | exact-HEAD 증거와 최신 main 통합 안전성을 함께 유지한다 | main이 계속 전진하면 reviewer·critic 비용이 반복된다 | **현재 채택** |
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

approvals가 0인 이유는 현재 워커와 사람이 같은 GitHub 계정을 써서 PR 작성자가 자기 PR에 공식 Approve를 남길 수 없기 때문이다. 별도 reviewer 계정이 생기면 1 이상으로 강화한다. strict를 켜도 12장 시나리오 K처럼 갱신된 HEAD를 자동으로 fresh reviewer→critic에 되돌리므로 검증 고착이 생기지 않는다.

CLI로 적용 결과를 읽을 때는 다음 명령을 쓴다.

```powershell
gh api repos/immortal0900/INFINITY_FORGE/rulesets `
  --jq '.[] | {id,name,enforcement,target}'

gh api repos/immortal0900/INFINITY_FORGE/rulesets/<RULESET_ID>
```

이 ruleset은 PR 경유·최신 main·`eval` 성공만 강제한다. **원본 이슈의 `forge:mergeable`은 사람이 별도로 확인해야 한다.** `--admin`이나 새 bypass actor로 우회하지 않는다.

### 13.2 P1 병합 뒤 VPS production 배포

이 변경 PR을 사람이 main에 병합한 뒤에만 다음을 실행한다. feature branch나 로컬 파일을 production에 직접 복사하지 않는다.

```powershell
ssh ubuntu@51.222.27.48 'cd "$HOME/work/INFINITY_FORGE" && test "$(git branch --show-current)" = main && test -z "$(git status --porcelain)" && git fetch origin main && git pull --ff-only origin main && bash forge/scripts/deploy-vps.sh'
```

배포 결과를 순서대로 읽어 확인한다.

```powershell
ssh ubuntu@51.222.27.48 'systemctl --user is-active forge-stage.timer forge-mirror.timer'
ssh ubuntu@51.222.27.48 'systemctl --user list-timers --all --no-pager | grep -E "forge-(stage|mirror)"'
ssh ubuntu@51.222.27.48 'cd "$HOME/work/INFINITY_FORGE" && PYTHONPATH="$PWD" python3 forge/scripts/stage-reconciler.py --dry-run'
```

정상 기준은 두 timer 모두 `active`, stage 다음 실행이 매분 `:00`, mirror 다음 실행이 매분 `:30`, dry-run exit 0이다. dry-run이 exit 2이면 카드를 생성해 덮지 말고 출력된 `GATE_ERROR`의 pipeline부터 고친다.

## 14. 현재 환경 점검표

다음 표는 2026-07-14 실측이며 시간이 지나면 바뀔 수 있다.

| 항목 | 실측 상태 | 의미 |
|---|---|---|
| VPS Gateway | active | 클라우드 작업 배차 가능 |
| VPS Dashboard | active | SSH 터널을 통한 Desktop 원격 연결 가능 |
| VPS Forge 타이머 | mirror·flush·ledger·drift·canary·morning·backup 가동 | 자동 감시·동기화·백업 실행 중 |
| VPS Kanban | active 0, done 5 | 현재 실행 중 작업 없음 |
| Windows Codex | ChatGPT 로그인 | 로컬 Codex 사용 가능 |
| Windows GitHub CLI | 미로그인 | 3.2절 로그인 필요 |
| Windows local-sync | 실행 중 | 로컬 상태 공유·백업 루프 실행 중 |
| Windows local Gateway | 프로세스 미검출 | `#forge-local` 사용 전 `hermes gateway start` 필요 |
| GitHub 저장소 | public, main 보호 없음, ruleset 없음 | red check 머지를 플랫폼이 막지 않음 |
| 열린 PR | 0개 | 현재 검토 대상 없음 |
| MEMEX MCP | Gateway 재연결 실패 경고 | 지식 배달 지연, 코드 작업은 계속 가능 |

## 15. 매일 사용하는 최소 체크리스트

### 작업을 넣을 때

- [ ] 이슈 목적과 SoT 근거가 있는가
- [ ] 모든 AC가 명령·테스트로 판정 가능한가
- [ ] 범위 제외가 적혀 있는가
- [ ] 내용을 다시 읽은 뒤 `forge:need-execution`을 붙였는가

### 알림을 받았을 때

- [ ] `forge:blocked`: 질문에 답하고 카드도 unblock했는가
- [ ] `forge:failed`: runs와 log에서 마지막 실패를 확인했는가
- [ ] `forge:mergeable`: 원본 이슈 라벨, PR 링크, diff, CI, 이슈 AC를 대조했는가

### 병합할 때

- [ ] 원본 이슈의 현재 라벨이 `forge:mergeable`인가
- [ ] CI가 모두 green인가
- [ ] Update branch가 필요하면 갱신 후 새 reviewer·critic이 끝날 때까지 기다렸는가
- [ ] 검토한 head SHA와 병합할 head SHA가 같은가
- [ ] 공개 API·DB·의존성·배포·보안 변경의 복구 방법이 있는가
- [ ] `--admin`이나 red check 무시를 사용하지 않았는가
- [ ] 병합 후 원래 이슈가 닫혔는가

## 16. 구현 근거

- GitHub 수입·상태 투영: `forge/scripts/label-mirror.py`
- 단계 결정·CI 실패 복구: `forge/ops/stage_reconciler.py`
- 단계 receipt·카드 생성: `forge/scripts/stage-reconciler.py`
- 단일 라벨 projection: `forge/ops/label_projection.py`
- executor 위임·재시도·handoff: `forge/skills/kanban-codex-delegate/SKILL.md`
- 종료 검문: `forge/hooks/codex-stop-gate.sh`
- reviewer 규약: `forge/skills/reviewer-verdict/SKILL.md`
- critic 규약: `forge/skills/critic-adversarial/SKILL.md`
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

**한마디 요약**: 배포 후에는 GitHub 이슈에 `forge:need-execution`을 붙이면 executor→reviewer→critic이 서로 다른 카드·세션으로 자동 진행하고, 최신 critic 결과 HEAD의 `eval`이 성공할 때만 원본 이슈가 `forge:mergeable`이 된다. branch를 갱신하면 새 HEAD를 다시 검증한다. 실제 merge는 사람이 수행하는 P1이며 P2/P3 자동 merge 명령은 아직 없다.
