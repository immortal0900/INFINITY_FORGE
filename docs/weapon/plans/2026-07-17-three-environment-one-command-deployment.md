# Windows·EC2·VPS 단일 명령 배포 구현 계획

> **Agentic worker(Agent 실행자)용:** REQUIRED SUB-SKILL: 이 plan을 task 단위로 구현하려면 `weapon:subagent-driven-development`(권장) 또는 `weapon:executing-plans`를 사용한다. 진행 추적에는 checkbox(`- [ ]`) 문법을 쓴다.

**Goal:** 검증된 `origin/main`의 정확한 한 커밋을 PowerShell 명령 한 번으로 Windows Hermes, EC2, VPS에 안전하게 설치하고 세 환경의 실행 상태를 검증한다.

**Architecture:** `forge/scripts/deploy.ps1`은 전체 대상의 사전 점검과 실행 순서, 결과 보고만 담당하고, Windows 설치 세부 사항은 새 `forge/scripts/deploy-windows.ps1`에 둔다. Linux는 기존 `deploy-vps.sh`를 재사용하며, Windows plugin은 현재 checkout 대신 `%LOCALAPPDATA%\InfinityForge\releases\<sha>`의 불변 snapshot을 import한다. 모든 대상의 사전 점검이 성공한 뒤 EC2 → VPS → Windows 순서로 적용하고, 부분 실패는 자동 전체 rollback 대신 정확한 단계와 재실행 가능한 상태로 보고한다.

**Tech Stack:** PowerShell 7, Python 3.11+, Git/Git archive, OpenSSH, Hermes CLI/plugin API, SQLite, pytest, Ruff

## Global Constraints

- 로컬 실행 저장소는 `main`이고 tracked·untracked 상태가 모두 clean이어야 한다.
- 로컬 `HEAD`, `origin/main`, 배포 요청 commit은 같은 40자리 소문자 SHA여야 한다.
- 선택된 모든 대상의 preflight가 성공하기 전에는 어느 대상도 변경하지 않는다.
- EC2와 VPS는 clean `main`에서 요청 commit으로 fast-forward할 수 있어야 한다.
- Windows release 경로는 `%LOCALAPPDATA%\InfinityForge\releases\<40자리 SHA>`로 고정한다.
- Windows `.env`는 덮어쓰지 않고 `hermes_cli.config.save_env_value`로 `INFINITY_FORGE_REPOSITORY`, `INFINITY_FORGE_TASK_SETTINGS_DB`, `INFINITY_FORGE_GH_PATH`만 갱신한다.
- 세 환경의 최종 Forge commit은 하나의 SHA로 같아야 한다.
- 자동 병합 안전 스위치는 `AUTO_MERGE_ENABLED=false`를 유지한다.
- secret 원문은 출력, 보고서, Git 저장소에 기록하지 않는다.
- Windows Gateway의 배포 전 running/stopped 상태와 로그인 시작 항목 등록 상태를 보존한다.
- 기존 사용자 작업 파일과 무관한 dirty change는 수정하거나 stage하지 않는다.
- 오래된 Windows release 자동 삭제, GitHub Actions 배포, 서버 DB rollback은 이 구현에 포함하지 않는다.

---

## 파일 책임 지도

| 파일 | 책임 |
|---|---|
| `forge/hermes_plugin/infinity_forge/__init__.py` | Forge import 전에 Windows managed release pointer를 검증하고 `sys.path`를 활성화한다. |
| `forge/scripts/deploy-windows.ps1` | Windows preflight, immutable release, Hermes patch, plugin/profile/skill, DB, Gateway 상태 보존, 검증을 담당한다. |
| `forge/scripts/deploy.ps1` | 로컬 main 검증, 전체 대상 preflight, EC2 → VPS → Windows apply/verify, JSON 보고서를 담당한다. |
| `forge/scripts/deploy-vps.sh` | 변경 없이 Linux 설치 adapter로 재사용하되 최신 main의 안전한 plugin enable 계약을 보존한다. |
| `tests/hermes_plugin/test_managed_release.py` | pointer 없음/정상/위조/손상 경로의 import bootstrap 계약을 검증한다. |
| `tests/ops/test_windows_deployment.py` | Windows adapter의 parser·preflight·release·설정·Gateway·재실행 계약을 검증한다. |
| `tests/ops/test_workflow_contract.py` | orchestrator의 전체 preflight 선행, 실행 순서, skip/report, Linux 계약을 검증한다. |
| `docs/user-runbook.md` | 운영자가 실제로 실행할 한 명령, 사전 조건, 성공/부분 실패 확인법을 설명한다. |
| `docs/weapon/specs/2026-07-17-three-environment-one-command-deployment-design.md` | 구현 완료 상태와 검증 근거를 기록한다. |

### Task 1: 최신 main 통합과 충돌 의미 보존

**Files:**
- Modify: `docs/user-runbook.md`
- Modify: `forge/ops/hermes.py`
- Modify: `forge/ops/task_setup.py`
- Modify: `forge/scripts/deploy-vps.sh`
- Modify: `forge/scripts/deploy.ps1`
- Modify: `forge/spec-registry.md`
- Modify: `tests/ops/test_adapters.py`
- Modify: `tests/ops/test_task_setup.py`
- Modify: `tests/ops/test_workflow_contract.py`
- Preserve: `tests/ops/test_spec_003_korean_pr_guideline.py`

**Interfaces:**
- Consumes: 현재 승인 커밋 `e160b2e`와 최신 `origin/main`.
- Produces: 기존 Task 표준 양식과 main의 PR 한국어 정책·완료 상태 호환·안전한 원격 Bash 전달을 모두 가진 conflict-free branch.

- [ ] **Step 1: 격리 worktree와 통합 branch 생성**

Run:

```powershell
git fetch origin main
git worktree add ..\INFINITY_FORGE-three-env -b codex/three-env-deploy e160b2e
git -C ..\INFINITY_FORGE-three-env merge --no-commit --no-ff origin/main
```

Expected: 9개 알려진 conflict가 나타나며 원본 dirty worktree는 바뀌지 않는다.

- [ ] **Step 2: 충돌을 양쪽 계약을 보존해 해소**

해소 규칙은 다음과 같이 고정한다.

```text
docs/user-runbook.md       = main의 PR 제목·본문 한국어 규칙과 label 설명 + branch의 Task 표준 양식
forge/ops/hermes.py        = task_runs.status의 done/completed 둘 다 허용
forge/ops/task_setup.py    = TASK_CONTENT_TEMPLATE과 새 content prompt 유지
forge/scripts/deploy-vps.sh= plugins enable ... --no-allow-tool-override 유지
forge/scripts/deploy.ps1   = main의 base64/LF-safe Invoke-RemoteBashScript 유지
forge/spec-registry.md     = SPEC-003 한국어 PR 원칙 유지
tests/ops/test_adapters.py = synthetic manual completion 허용 test 유지
tests/ops/test_task_setup.py = 표준 양식 regression test 유지
tests/ops/test_workflow_contract.py = noninteractive enable과 LF/base64 test 유지
```

Run:

```powershell
git status --short
git diff --check
rg -n "^(<<<<<<<|=======|>>>>>>>)" docs forge tests
```

Expected: conflict marker가 없고 `git diff --check`가 성공한다.

- [ ] **Step 3: 통합 regression 실행**

Run:

```powershell
python -m pytest tests/ops/test_task_setup.py tests/ops/test_adapters.py tests/ops/test_workflow_contract.py tests/ops/test_spec_003_korean_pr_guideline.py -q
```

Expected: 모든 test가 PASS한다.

- [ ] **Step 4: 통합 commit**

```powershell
git add docs/user-runbook.md forge/ops/hermes.py forge/ops/task_setup.py forge/scripts/deploy-vps.sh forge/scripts/deploy.ps1 forge/spec-registry.md tests/ops/test_adapters.py tests/ops/test_task_setup.py tests/ops/test_workflow_contract.py tests/ops/test_spec_003_korean_pr_guideline.py
git commit -m "merge: integrate latest main deployment contracts"
```

### Task 2: Windows managed release bootstrap

**Files:**
- Modify: `forge/hermes_plugin/infinity_forge/__init__.py:1-20`
- Create: `tests/hermes_plugin/test_managed_release.py`

**Interfaces:**
- Consumes: plugin 옆 선택 파일 `release-path.txt`, 환경값 `LOCALAPPDATA`.
- Produces: `_activate_managed_release(plugin_file: Path = Path(__file__)) -> Path | None`; pointer가 없으면 `None`, 정상이면 검증된 release `Path`, pointer가 있는데 잘못됐으면 `RuntimeError`.

- [ ] **Step 1: 실패하는 pointer 계약 test 작성**

```python
def test_valid_pointer_prepends_exact_release(tmp_path, monkeypatch):
    local = tmp_path / "Local"
    release = local / "InfinityForge" / "releases" / ("a" * 40)
    (release / "forge" / "ops").mkdir(parents=True)
    (release / "forge" / "__init__.py").write_text("", encoding="utf-8")
    (release / "forge" / "ops" / "task_setup.py").write_text("", encoding="utf-8")
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    plugin_file = plugin_dir / "__init__.py"
    plugin_file.write_text("", encoding="utf-8")
    (plugin_dir / "release-path.txt").write_text(str(release), encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(local))

    assert bootstrap._activate_managed_release(plugin_file) == release.resolve()
    assert Path(sys.path[0]) == release.resolve()


@pytest.mark.parametrize("pointer", ["relative/release", "../outside", ""])
def test_present_invalid_pointer_fails_loudly(tmp_path, monkeypatch, pointer):
    plugin_file = tmp_path / "plugin" / "__init__.py"
    plugin_file.parent.mkdir()
    plugin_file.write_text("", encoding="utf-8")
    (plugin_file.parent / "release-path.txt").write_text(pointer, encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))

    with pytest.raises(RuntimeError, match="managed release"):
        bootstrap._activate_managed_release(plugin_file)
```

추가 test는 pointer 파일이 없을 때 `None`, root 밖의 절대 경로, 대문자/39자리 SHA, 필수 파일 누락을 각각 거부하는지 확인한다.

- [ ] **Step 2: test가 현재 실패하는지 확인**

Run: `python -m pytest tests/hermes_plugin/test_managed_release.py -q`

Expected: bootstrap 함수가 없어 collection 또는 assertion 단계에서 FAIL한다.

- [ ] **Step 3: 표준 라이브러리만 사용하는 bootstrap 구현**

`forge` import보다 앞에서 다음 계약을 구현한다.

```python
import os
import re
import sys
from pathlib import Path

_RELEASE_SHA = re.compile(r"[0-9a-f]{40}")


def _activate_managed_release(
    plugin_file: Path = Path(__file__),
) -> Path | None:
    pointer = plugin_file.resolve().parent / "release-path.txt"
    if not pointer.exists():
        return None
    raw = pointer.read_text(encoding="utf-8").strip()
    candidate = Path(raw)
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if not raw or not candidate.is_absolute() or not local_app_data:
        raise RuntimeError("invalid Infinity Forge managed release pointer")
    release_root = (
        Path(local_app_data).resolve() / "InfinityForge" / "releases"
    )
    resolved = candidate.resolve()
    if resolved.parent != release_root or not _RELEASE_SHA.fullmatch(resolved.name):
        raise RuntimeError("Infinity Forge managed release is outside release root")
    for required in (
        resolved / "forge" / "__init__.py",
        resolved / "forge" / "ops" / "task_setup.py",
    ):
        if not required.is_file():
            raise RuntimeError("Infinity Forge managed release is incomplete")
    sys.path.insert(0, str(resolved))
    return resolved


_MANAGED_RELEASE = _activate_managed_release()
```

- [ ] **Step 4: plugin regression 확인**

Run:

```powershell
python -m pytest tests/hermes_plugin/test_managed_release.py tests/hermes_plugin/test_infinity_forge_plugin.py -q
```

Expected: 모든 test가 PASS하고 Linux처럼 pointer가 없는 import도 유지된다.

- [ ] **Step 5: commit**

```powershell
git add forge/hermes_plugin/infinity_forge/__init__.py tests/hermes_plugin/test_managed_release.py
git commit -m "feat: load Windows plugin from managed release"
```

### Task 3: Windows 배포 adapter

**Files:**
- Create: `forge/scripts/deploy-windows.ps1`
- Create: `tests/ops/test_windows_deployment.py`
- Modify: `tests/ops/test_plain_names.py`

**Interfaces:**
- Consumes: `-Repo [string]`, `-Commit [40자리 SHA]`, `-Repository [owner/name]`, `-Mode Preflight|Apply|Verify`.
- Produces: 성공 시 한 줄 JSON `{target, phase, status, commit, gatewayRunning}`; 실패 시 non-zero exit와 secret 없는 오류.

- [ ] **Step 1: adapter contract test 작성**

```python
def test_windows_adapter_has_separate_read_only_and_write_phases():
    script = WINDOWS_DEPLOY.read_text(encoding="utf-8")
    assert '[ValidateSet("Preflight", "Apply", "Verify")]' in script
    assert "function Test-ForgeWindowsPreflight" in script
    assert "function Install-ForgeWindowsRelease" in script
    assert "function Test-ForgeWindowsRuntime" in script
    assert script.index('"Preflight" {') < script.index('"Apply" {')


def test_windows_release_is_archive_based_and_atomically_promoted():
    script = WINDOWS_DEPLOY.read_text(encoding="utf-8")
    assert "git archive --format=zip $Commit" in script
    assert 'releases\\$Commit' in script
    assert "deployment-source.json" in script
    assert "Move-Item -LiteralPath $ReleaseTemp -Destination $ReleasePath" in script
    assert "Copy-Item -Recurse -Force $Repo" not in script


def test_windows_env_updates_are_narrow_and_do_not_print_env():
    script = WINDOWS_DEPLOY.read_text(encoding="utf-8")
    assert "hermes_cli.config import save_env_value" in script
    for key in ("INFINITY_FORGE_REPOSITORY", "INFINITY_FORGE_TASK_SETTINGS_DB", "INFINITY_FORGE_GH_PATH"):
        assert key in script
    assert "Get-Content $EnvFile" not in script
```

Gateway running/stopped 보존, plugin 임시 sibling swap, `--no-allow-tool-override`, 세 DB `PRAGMA quick_check`, 여섯 hook marker, Task 양식 marker, 같은 SHA release 재사용을 별도 test로 고정한다.

- [ ] **Step 2: RED 확인**

Run: `python -m pytest tests/ops/test_windows_deployment.py tests/ops/test_plain_names.py -q`

Expected: `deploy-windows.ps1`이 없어 FAIL한다.

- [ ] **Step 3: read-only preflight 구현**

```powershell
param(
  [Parameter(Mandatory = $true)][string]$Repo,
  [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$Commit,
  [Parameter(Mandatory = $true)][ValidatePattern('^[^/]+/[^/]+$')][string]$Repository,
  [Parameter(Mandatory = $true)][ValidateSet("Preflight", "Apply", "Verify")][string]$Mode
)

function Test-ForgeWindowsPreflight {
  $localRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "InfinityForge"))
  $hermesRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "hermes\hermes-agent"))
  $hermesPython = Join-Path $hermesRoot "venv\Scripts\python.exe"
  $hermesCli = Join-Path $hermesRoot "venv\Scripts\hermes.exe"
  $gh = (Get-Command gh.exe -ErrorAction Stop).Source
  foreach ($path in @($Repo, $hermesRoot, $hermesPython, $hermesCli, $gh)) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Windows preflight path missing: $path" }
  }
  if ((git -C $Repo rev-parse $Commit).Trim() -ne $Commit) {
    throw "Requested Forge commit is unavailable locally."
  }
  [pscustomobject]@{ LocalRoot=$localRoot; HermesRoot=$hermesRoot; HermesPython=$hermesPython; HermesCli=$hermesCli; Gh=$gh }
}
```

Preflight는 directory 생성, `.env` 변경, process stop, plugin enable을 호출하지 않는다.

- [ ] **Step 4: immutable release와 package 설치 구현**

`git archive --format=zip $Commit -o $archive`, `Expand-Archive`, 필수 파일 검사, `deployment-source.json` 생성, 같은 volume의 임시 directory를 `Move-Item`으로 `<sha>`에 승격한다. Hermes source는 source HEAD archive에서 package를 만들고 다음 명령으로 기존 atomic installer를 사용한다.

```powershell
& $paths.HermesPython "$ReleasePath\forge\scripts\install-hermes-change.py" `
  --hermes-root $HermesSourceTemp `
  --package $PackageTemp `
  --source-version "$Commit-$HermesSourceCommit"
& $paths.HermesPython "$ReleasePath\forge\scripts\install-hermes-change.py" `
  --hermes-root $paths.HermesRoot `
  --package $PackagePath `
  --install
```

- [ ] **Step 5: plugin·설정·DB·skill과 Gateway 상태 보존 구현**

```powershell
$GatewayWasRunning = Test-HermesGatewayRunning
if ($GatewayWasRunning) { & $paths.HermesCli gateway stop }
try {
  Install-HermesChangePackage
  Install-InfinityForgePlugin -ReleasePath $ReleasePath
  Set-InfinityForgeEnvironment -Python $paths.HermesPython -Repository $Repository -Gh $paths.Gh
  Initialize-InfinityForgeDatabases
  Install-InfinityForgeSkillsAndProfiles
  & $paths.HermesCli plugins enable infinity-forge --no-allow-tool-override
  Test-ForgeWindowsRuntime
} catch {
  Restore-WindowsDeploymentTransaction
  throw
} finally {
  if ($GatewayWasRunning -and -not (Test-HermesGatewayRunning)) {
    & $paths.HermesCli gateway start
  }
}
```

plugin directory는 sibling temp에서 `plugin.yaml`, `__init__.py`, `release-path.txt`를 모두 만든 뒤 기존 directory를 backup 이름으로 바꾸고 새 directory를 승격한다. stopped였던 Gateway는 시작하지 않는다.

- [ ] **Step 6: parser와 adapter test GREEN 확인**

Run:

```powershell
python -m pytest tests/ops/test_windows_deployment.py tests/ops/test_plain_names.py -q
pwsh -NoProfile -Command "[void][scriptblock]::Create((Get-Content -Raw 'forge/scripts/deploy-windows.ps1'))"
```

Expected: PASS, PowerShell parser exit code 0.

- [ ] **Step 7: commit**

```powershell
git add forge/scripts/deploy-windows.ps1 tests/ops/test_windows_deployment.py tests/ops/test_plain_names.py
git commit -m "feat: deploy Infinity Forge to Windows Hermes"
```

### Task 4: 세 환경 orchestrator와 배포 보고서

**Files:**
- Modify: `forge/scripts/deploy.ps1`
- Modify: `tests/ops/test_workflow_contract.py`

**Interfaces:**
- Consumes: Windows adapter의 `Preflight|Apply|Verify`, SSH server preflight/apply/verify, exact `origin/main` SHA.
- Produces: `%LOCALAPPDATA%\InfinityForge\state\deployment-report.json`; 대상별 `preflight`, `apply`, `verify`, `commit`, `error`, `skipped` 상태.

- [ ] **Step 1: 전체 preflight와 순서 test 작성**

```python
def test_all_preflights_finish_before_first_apply():
    script = LOCAL_DEPLOY.read_text(encoding="utf-8")
    windows_preflight = script.index('-Mode "Preflight"')
    ec2_preflight = script.index('Test-ForgeServerPreflight -Name "EC2"')
    vps_preflight = script.index('Test-ForgeServerPreflight -Name "VPS"')
    first_apply = script.index('Invoke-ForgeServerDeploy -Name "EC2"')
    assert max(windows_preflight, ec2_preflight, vps_preflight) < first_apply


def test_apply_order_is_ec2_then_vps_then_windows():
    script = LOCAL_DEPLOY.read_text(encoding="utf-8")
    ec2 = script.index('Invoke-ForgeServerDeploy -Name "EC2"')
    vps = script.index('Invoke-ForgeServerDeploy -Name "VPS"')
    windows = script.index('-Mode "Apply"')
    assert ec2 < vps < windows
```

추가 test는 server preflight에 쓰기 명령(`merge`, `deploy-vps.sh`)이 없고, skip 대상이 `skipped`로 기록되며, report가 temp file 후 `Move-Item`으로 교체되고, 오류에 `.env`/token 값이 들어가지 않는 계약을 확인한다.

- [ ] **Step 2: RED 확인**

Run: `python -m pytest tests/ops/test_workflow_contract.py -q`

Expected: 새 전체 preflight와 Windows/report 계약 assertion이 FAIL한다.

- [ ] **Step 3: server preflight와 apply 분리**

```powershell
function Test-ForgeServerPreflight {
  param([string]$Name, [string]$HostName, [string]$RemoteRepo)
  $script = @'
set -euo pipefail
REPO_DIR="$1"
EXPECTED_COMMIT="$2"
test "$(git -C "$REPO_DIR" symbolic-ref --short HEAD)" = "main"
test -z "$(git -C "$REPO_DIR" status --porcelain=v1 --untracked-files=all)"
git -C "$REPO_DIR" fetch origin main --quiet
test "$(git -C "$REPO_DIR" rev-parse origin/main)" = "$EXPECTED_COMMIT"
git -C "$REPO_DIR" merge-base --is-ancestor HEAD "$EXPECTED_COMMIT"
'@
  Invoke-RemoteBashScript -HostName $HostName -Script $script -Arguments @($RemoteRepo, $Commit)
}
```

최신 main의 base64/LF-safe `Invoke-RemoteBashScript`로만 remote Bash를 전달한다.

- [ ] **Step 4: 모든 preflight 후 순차 apply/verify 구현**

```powershell
if (-not $SkipEC2) { Test-ForgeServerPreflight -Name "EC2" -HostName "My-EC2" -RemoteRepo "/home/ec2-user/work/INFINITY_FORGE" }
if (-not $SkipVPS) { Test-ForgeServerPreflight -Name "VPS" -HostName "ubuntu@51.222.27.48" -RemoteRepo "/home/ubuntu/work/INFINITY_FORGE" }
if (-not $SkipLocal) { & $WindowsAdapter -Repo $Repo -Commit $Commit -Repository $Repository -Mode "Preflight" }

if (-not $SkipEC2) { Invoke-ForgeServerDeploy -Name "EC2" -HostName "My-EC2" -RemoteRepo "/home/ec2-user/work/INFINITY_FORGE" }
if (-not $SkipVPS) { Invoke-ForgeServerDeploy -Name "VPS" -HostName "ubuntu@51.222.27.48" -RemoteRepo "/home/ubuntu/work/INFINITY_FORGE" }
if (-not $SkipLocal) {
  & $WindowsAdapter -Repo $Repo -Commit $Commit -Repository $Repository -Mode "Apply"
  & $WindowsAdapter -Repo $Repo -Commit $Commit -Repository $Repository -Mode "Verify"
}
```

- [ ] **Step 5: secret 없는 원자 report 구현**

```powershell
$ReportPath = Join-Path $env:LOCALAPPDATA "InfinityForge\state\deployment-report.json"
$ReportTemp = "$ReportPath.tmp-$PID"
$Report = [ordered]@{
  formatVersion = 1
  requestedCommit = $Commit
  startedAtUtc = $StartedAtUtc
  finishedAtUtc = [DateTimeOffset]::UtcNow.ToString("o")
  user = [Environment]::UserName
  targets = $TargetResults
  skipped = @($TargetResults.Keys | Where-Object { $TargetResults[$_].status -eq "skipped" })
}
$Report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ReportTemp -Encoding utf8NoBOM
Move-Item -Force -LiteralPath $ReportTemp -Destination $ReportPath
```

catch에서는 command line이나 environment dump 대신 `Exception.Message`에서 개행을 공백으로 바꾸고 최대 500자로 잘라 `error`에 기록한다.

- [ ] **Step 6: orchestrator test GREEN 확인**

Run:

```powershell
python -m pytest tests/ops/test_workflow_contract.py tests/ops/test_windows_deployment.py tests/ops/test_plain_names.py -q
pwsh -NoProfile -Command "[void][scriptblock]::Create((Get-Content -Raw 'forge/scripts/deploy.ps1'))"
```

Expected: PASS, parser exit code 0.

- [ ] **Step 7: commit**

```powershell
git add forge/scripts/deploy.ps1 tests/ops/test_workflow_contract.py
git commit -m "feat: deploy one commit to all Forge environments"
```

### Task 5: 운영 문서와 설계 상태 동기화

**Files:**
- Modify: `docs/user-runbook.md`
- Modify: `docs/weapon/specs/2026-07-17-three-environment-one-command-deployment-design.md`

**Interfaces:**
- Consumes: 최종 script parameter, report 경로, 실패/재실행 의미.
- Produces: 운영자가 추가 추론 없이 실행·판정·복구할 수 있는 runbook.

- [ ] **Step 1: runbook contract test 추가**

`tests/ops/test_workflow_contract.py`에 다음을 추가한다.

```python
def test_runbook_documents_the_single_three_target_command():
    runbook = (ROOT / "docs" / "user-runbook.md").read_text(encoding="utf-8")
    assert "pwsh -NoProfile -File forge/scripts/deploy.ps1" in runbook
    assert "%LOCALAPPDATA%\\InfinityForge\\state\\deployment-report.json" in runbook
    assert "EC2 → VPS → Windows" in runbook
    assert "같은 SHA로 다시 실행" in runbook
```

- [ ] **Step 2: RED 확인**

Run: `python -m pytest tests/ops/test_workflow_contract.py::test_runbook_documents_the_single_three_target_command -q`

Expected: 현재 runbook 설명이 부족해 FAIL한다.

- [ ] **Step 3: 실제 운영 절차 기록**

`docs/user-runbook.md` 배포 절을 다음 계약으로 갱신한다.

````markdown
검증된 PR을 병합한 뒤 clean `main`에서 한 번 실행한다.

```powershell
pwsh -NoProfile -File forge/scripts/deploy.ps1
```

스크립트는 Windows·EC2·VPS를 모두 사전 점검한 뒤 EC2 → VPS → Windows 순서로 같은 SHA를 반영한다. 일부 대상이 실패하면 성공으로 표시하지 않으며, `%LOCALAPPDATA%\InfinityForge\state\deployment-report.json`에서 단계별 상태를 확인하고 원인을 고친 뒤 같은 SHA로 다시 실행한다.
````

설계 문서 상태를 `구현 및 운영 검증 완료`로 바꾸고 실제 commit/test/deployment SHA를 변경이력에 기록한다.

- [ ] **Step 4: 문서 test와 diff 검사**

Run:

```powershell
python -m pytest tests/ops/test_workflow_contract.py -q
git diff --check
```

Expected: PASS, whitespace 오류 없음.

- [ ] **Step 5: commit**

```powershell
git add docs/user-runbook.md docs/weapon/specs/2026-07-17-three-environment-one-command-deployment-design.md tests/ops/test_workflow_contract.py
git commit -m "docs: explain three-environment deployment"
```

### Task 6: 전체 검증, PR 병합, production 배포

**Files:**
- Verify: repository 전체 관련 test와 production runtime
- Write at runtime only: `%LOCALAPPDATA%\InfinityForge\state\deployment-report.json`

**Interfaces:**
- Consumes: conflict-free implementation branch와 GitHub PR #16.
- Produces: bypass 없이 병합된 `main` SHA, 세 환경의 동일 SHA와 정상 runtime, Slack 완료 보고.

- [ ] **Step 1: fresh local verification**

Run:

```powershell
python -m pytest tests/ops tests/hermes tests/hermes_plugin -q
python -m ruff check forge tests
python -m compileall -q forge tests
pwsh -NoProfile -Command "[void][scriptblock]::Create((Get-Content -Raw 'forge/scripts/deploy.ps1')); [void][scriptblock]::Create((Get-Content -Raw 'forge/scripts/deploy-windows.ps1'))"
bash -n forge/scripts/deploy-vps.sh
git diff --check origin/main...HEAD
git status --short
```

Expected: 모든 명령 exit code 0, 작업 tree clean.

- [ ] **Step 2: PR branch push와 GitHub eval 확인**

Run:

```powershell
git push origin HEAD:codex/stage-orchestrator
gh pr view 16 --json mergeable,mergeStateStatus,statusCheckRollup,headRefOid
gh pr checks 16 --watch
```

Expected: `mergeable=MERGEABLE`, stable `eval` PASS, head SHA가 방금 push한 SHA와 같다.

- [ ] **Step 3: bypass 없이 PR 병합**

Run:

```powershell
gh pr merge 16 --merge --delete-branch=false
git fetch origin main
gh pr view 16 --json state,mergedAt,mergeCommit
```

Expected: PR state `MERGED`, `origin/main`이 merge commit을 가리킨다.

- [ ] **Step 4: clean main worktree에서 단일 명령 배포**

Run:

```powershell
git worktree add ..\INFINITY_FORGE-production origin/main
pwsh -NoProfile -File ..\INFINITY_FORGE-production\forge\scripts\deploy.ps1
```

Expected: 전체 preflight 후 EC2, VPS, Windows가 차례로 verified되고 배포 명령 exit code 0.

- [ ] **Step 5: 세 환경 교차 검증**

Run:

```powershell
$expected = (git -C ..\INFINITY_FORGE-production rev-parse HEAD).Trim()
ssh My-EC2 git -C /home/ec2-user/work/INFINITY_FORGE rev-parse HEAD
ssh ubuntu@51.222.27.48 git -C /home/ubuntu/work/INFINITY_FORGE rev-parse HEAD
Get-Content "$env:LOCALAPPDATA\hermes\plugins\infinity-forge\release-path.txt"
Get-Content "$env:LOCALAPPDATA\InfinityForge\state\deployment-report.json" | ConvertFrom-Json | ConvertTo-Json -Depth 8
```

Expected: 두 서버 SHA와 Windows release directory 이름, report의 `requestedCommit`이 `$expected`와 같고 세 대상 status가 `verified`다. 서버 Gateway와 timer, Windows plugin/DB/양식 검증은 adapter가 이미 수행했으며 최종 report에서도 성공이어야 한다.

- [ ] **Step 6: Slack 완료 알림**

로컬 secret env에서 token을 읽어 `codex work report` 앱의 `chat.postMessage`로 채널 `C0BES16KE1J`에 다음만 보낸다.

```text
작업 요약: Windows·EC2·VPS 단일 명령 배포 반영 완료
변경 파일: deploy.ps1, deploy-windows.ps1, plugin bootstrap, tests, runbook
검증 결과: PR eval, local test, 세 환경 SHA/runtime 검증
남은 이슈: 없으면 없음; 운영 Task smoke는 별도 승인 후 실행
```

GitHub issue/PR을 만드는 `Build + Review + Deep Check + Manual` 운영 smoke는 실제 외부 작업을 생성하므로 이 배포의 자동 완료 조건에 포함하지 않고 별도 사용자 승인 뒤 실행한다.

## 자기 검토 결과

- Spec coverage: 전체 preflight, exact SHA, Windows immutable release, 좁은 `.env` 갱신, plugin/patch/DB/skill, Gateway 상태 보존, 실행 순서, report, 부분 실패, 같은 SHA 재실행, auto-merge disabled를 Task 2~6에 각각 연결했다.
- Placeholder scan: 미정 값이나 후속 구현으로 미루는 표현이 없고, Task 3의 helper 이름은 같은 Task 안에서 모두 정의해야 하는 구현 계약으로 열거했다.
- Type consistency: Windows adapter mode는 모든 Task에서 `Preflight|Apply|Verify`, commit은 40자리 소문자 SHA, report format은 version 1로 통일했다.
- 장기 확인: Windows runtime이 dirty checkout과 분리되며, release 자동 삭제를 보류해 마지막 known-good 복구 선택지를 남겼다. 전체 rollback은 상태형 DB 불일치 위험 때문에 자동화하지 않는다.
