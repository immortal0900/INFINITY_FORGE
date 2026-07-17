# Linux CLI Chat/Task 선택기 실행 경로 구현 계획

> **Agentic worker(Agent 실행자)용:** REQUIRED SUB-SKILL: 이 plan을 task 단위로 구현하려면 `weapon:subagent-driven-development`(권장) 또는 `weapon:executing-plans`를 사용한다. 진행 추적에는 checkbox(`- [ ]`) 문법을 쓴다.

**Goal:** EC2/VPS 일반 `hermes` CLI가 외부 작업 폴더와 비어 있는 `PYTHONPATH`에서도 Chat/Task 선택기를 실제로 실행하게 한다.

**Architecture:** Windows의 commit별 managed release pointer를 Linux Hermes home에도 적용한다. Linux deploy가 clean archive, atomic plugin pointer, Hermes environment key를 설치하고 실제 plugin discovery와 first-turn hook을 smoke test한다.

**Tech Stack:** Python 3.11, Bash, Hermes plugin manager, pytest, systemd user services

## Global Constraints

- Hermes 공식 launcher의 `unset PYTHONPATH`를 수정하지 않는다.
- mutable server working tree를 plugin import 경로로 사용하지 않는다.
- 기존 `.env` 전체를 읽거나 출력하지 않고 세 Infinity Forge key만 갱신한다.
- synthetic hook 검증은 모델, GitHub, Kanban write를 실행하지 않는다.
- 실행 중인 사용자 Hermes session과 Cognet9 working tree를 변경하지 않는다.
- 배포 전체를 사용자별 `flock`으로 직렬화하고 published commit release는 rollback에서 삭제하지 않는다.

---

### Task 1: Linux managed release root 지원

**Files:**
- Modify: `forge/hermes_plugin/infinity_forge/__init__.py`
- Test: `tests/hermes_plugin/test_managed_release.py`

**Interfaces:**
- Consumes: `_activate_managed_release(plugin_file: Path) -> Path | None`
- Produces: Windows 또는 Linux의 허용된 release root에 있는 검증된 commit path

- [x] **Step 1: Linux plugin home 아래 release pointer test 작성**

```python
def test_valid_linux_pointer_prepends_release(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    plugin_file = _make_linux_plugin_and_release(tmp_path)
    result = plugin._activate_managed_release(plugin_file)
    assert result == expected_release.resolve()
    assert Path(sys.path[0]) == expected_release.resolve()
```

- [x] **Step 2: test가 기존 `LOCALAPPDATA` 필수 조건으로 실패하는지 확인**

Run: `python -m pytest tests/hermes_plugin/test_managed_release.py -q`

Expected: Linux pointer가 `invalid Infinity Forge managed release pointer`로 FAIL

- [x] **Step 3: plugin 위치에서 Linux Hermes home을 계산해 허용 root를 선택**

```python
def _managed_release_root(plugin_file: Path) -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data).resolve() / "InfinityForge" / "releases"
    return plugin_file.resolve().parent.parent.parent / "infinity-forge" / "releases"
```

- [x] **Step 4: Linux root 이탈과 기존 Windows test를 함께 통과시키기**

Run: `python -m pytest tests/hermes_plugin/test_managed_release.py -q`

Expected: 모든 test PASS

### Task 2: Linux release·plugin·runtime environment 원자 배포

**Files:**
- Modify: `forge/scripts/deploy-vps.sh`
- Test: `tests/ops/test_workflow_contract.py`

**Interfaces:**
- Consumes: `FORGE_EXPECTED_COMMIT`, `REPO_DIR`, Hermes home과 GitHub command
- Produces: `$HOME/.hermes/infinity-forge/releases/$DEPLOYED_COMMIT`, versioned plugin과 `release-path.txt`, Hermes `.env` 세 key

- [x] **Step 1: 배포 계약 test 추가**

```python
def test_linux_deploy_installs_cwd_independent_plugin_runtime():
    deploy = DEPLOY.read_text(encoding="utf-8")
    for required in (
        'FORGE_RELEASE_ROOT="$HOME/.hermes/infinity-forge/releases"',
        'git archive "$DEPLOYED_COMMIT"',
        'release-path.txt',
        'from hermes_cli.config import save_env_value',
        'env -u PYTHONPATH',
        'has_hook("pre_user_turn")',
    ):
        assert required in deploy
```

- [x] **Step 2: 새 계약 test가 누락된 Linux release 설치 때문에 실패하는지 확인**

Run: `python -m pytest tests/ops/test_workflow_contract.py -q`

Expected: managed release contract assertion으로 FAIL

- [x] **Step 3: clean Git archive를 commit directory에 원자 공개**

```bash
FORGE_RELEASE_ROOT="$TASK_DATA_DIR/releases"
FORGE_RELEASE="$FORGE_RELEASE_ROOT/$DEPLOYED_COMMIT"
git archive "$DEPLOYED_COMMIT" | tar -x -C "$FORGE_RELEASE_TEMP"
mv -T "$FORGE_RELEASE_TEMP" "$FORGE_RELEASE"
```

- [x] **Step 4: versioned plugin에 pointer까지 만든 뒤 stable link를 원자 교체**

```bash
printf '%s' "$FORGE_RELEASE" > "$PLUGIN_TEMP/release-path.txt"
mv -T "$PLUGIN_TEMP" "$PLUGIN_RELEASE"
ln -s -- "$PLUGIN_RELEASE" "$PLUGIN_LINK_STAGE/infinity-forge"
mv -Tf "$PLUGIN_LINK_STAGE/infinity-forge" "$PLUGIN_LINK"
```

- [x] **Step 5: Hermes helper로 세 runtime key만 저장**

```python
from hermes_cli.config import save_env_value
for key, value in zip(sys.argv[1::2], sys.argv[2::2], strict=True):
    save_env_value(key, value)
```

- [x] **Step 6: Bash 문법과 계약 test 통과 확인**

Run: `python -m pytest tests/ops/test_workflow_contract.py -q`

Run: `C:\Program Files\Git\bin\bash.exe -n forge/scripts/deploy-vps.sh`

Expected: 모든 test와 Bash syntax PASS

### Task 3: 실제 hook smoke와 전체 검증

**Files:**
- Modify: `forge/scripts/deploy.ps1`
- Modify: `forge/scripts/deploy-vps.sh`
- Test: `tests/ops/test_workflow_contract.py`

**Interfaces:**
- Consumes: 설치된 plugin manager와 synthetic first-turn event
- Produces: import error 없음, `pre_user_turn` 등록, `chat/task` choice ID 증거

- [x] **Step 1: allow-list 검증만으로는 통과하지 못하는 실제 hook smoke 계약 추가**

```python
discover_plugins(force=True)
loaded = get_plugin_manager()._plugins["infinity-forge"]
assert loaded.enabled and loaded.error is None
assert has_hook("pre_user_turn")
result = invoke_hook("pre_user_turn", text="diagnostic", ...)[0]
assert [item["id"] for item in result["choices"]] == ["chat", "task"]
```

- [x] **Step 2: deploy-vps 자체 검증과 deploy.ps1 사후 검증에 같은 smoke 추가**

Run: `python -m pytest tests/ops/test_workflow_contract.py -q`

Expected: PASS

- [x] **Step 3: 관련 test와 전체 suite 실행**

Run: `python -m pytest tests/hermes_plugin/test_managed_release.py tests/ops/test_workflow_contract.py -q`

Run: `python -m pytest tests/ -q`

Expected: 모든 test PASS

- [ ] **Step 4: PR eval 통과 후 main 병합·EC2/VPS/Windows 배포**

Run: `pwsh -File forge/scripts/deploy.ps1`

Expected: 세 환경 verified, EC2/VPS 외부 cwd hook smoke PASS

- [ ] **Step 5: EC2에서 현재 사용자 session은 유지하고 새 진단 process로 확인**

Run: `ssh My-EC2 'cd ~/work/Cognet9-Official && env -u PYTHONPATH ...'`

Expected: plugin error 없음, `pre_user_turn=True`, choices=`chat,task`
