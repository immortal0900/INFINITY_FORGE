# Hermes completion-policy carried patch 구현 계획

> **Agentic worker(Agent 실행자)용:** REQUIRED SUB-SKILL: 이 plan을 task 단위로 구현하려면 `weapon:subagent-driven-development`(권장) 또는 `weapon:executing-plans`를 사용한다. 진행 추적에는 checkbox(`- [ ]`) 문법을 쓴다.

**Goal:** Hermes Agent v0.18.2의 모든 정상 완료 경로가 `completion_policy=forge-v1` 태스크를 `phase=hermes` receipt 없이 완료하지 못하게 하고, receipt 소비·`done` 전이·자식 `ready` 승격을 하나의 SQLite transaction으로 묶은 뒤 Windows와 Linux/VPS 설치본에 대상 파일만 안전하게 적용·롤백한다.

**Architecture:** 외부 trusted guard zipapp 호출과 응답 검증은 새 `hermes_cli.kanban_completion_policy` 모듈이 담당한다. `hermes_cli.kanban_db.complete_task()`는 보호 태스크의 현재 run에 결합된 Hermes 전용 receipt를 preflight한 뒤, receipt 원장 삽입·task 완료·run 종료·감사 이벤트·자식 승격을 한 write transaction에서 처리한다. 패치는 upstream 기준 격리 worktree에서 TDD로 제작하고, Forge의 target-only installer가 고정 upstream SHA와 현재 base blob variant를 선택해 variant별 7개 AST preimage·target preimage·target-specific postimage·patch SHA를 확인한 뒤 지정된 파일만 적용·검증·커밋·역적용한다.

**Tech Stack:** Python 3.11.15, SQLite, `pytest`, Git CLI, Hermes Agent 0.18.2, PowerShell 7+, POSIX shell

## Global Constraints

1. 승인 spec은 `docs/weapon/specs/2026-07-12-hermes-early-termination-guards-design.md`이며 이 하위 계획은 Hermes core carried patch와 해당 patch installer만 다룬다.
2. Hermes upstream base는 정확히 `4281151ae859241351ba14d8c7682dc67ff4c126`이다.
3. 지원하는 `hermes_cli/kanban_db.py` Git blob은 Windows `518e74eb0647786a0361105b76bfbaeb1bad3e19`, VPS `6150b141537b947a2a89d19b13be4fbad2330711` 두 개뿐이다.
   - patch manifest의 `variants`는 이 두 blob을 exact key로 가진다.
   - 각 variant는 7개 `ast_preimages`, target set과 exact-equal인 `target_preimage_sha256`, `target_postimage_sha256`을 가진다.
   - Windows와 VPS의 unrelated carried hunk를 보존하므로 `hermes_cli/kanban_db.py` postimage SHA는 variant별로 다르다.
4. installer가 확인할 7개 UTF-8 AST source-segment SHA-256은 다음과 같다.
   - `Task`: `37dbff1faa5f92afa3b63e3d80a1c041e36a0a5fcebd2dc9585bb8c824656137`
   - `_migrate_add_optional_columns`: `e8d018507072b7aa7a9d875bde98b389446bb9fb5c61efdfd4e0b1a09fd82583`
   - `create_task`: `d95d2c6f0bd66eb3419ce2ee3ad49faa4f211b28624e3cd36e1efbbd8bd265aa`
   - `recompute_ready`: `d6e8a2840b92a4c38a9d41e358f49c35c90d386f14834d91a1abe4ff682249e8`
   - `complete_task`: `a10e062b91aeef9e8c097997c39840b3bf1b0d0552764681613038505b286bf2`
   - `edit_completed_task_result`: `bcf22376052004ea28747d65a95260edcc30781b7e53f7b8ebfa8de72e82e2e2`
   - `detect_crashed_workers`: `d7dca0d5a3943b21108e1fb36fca5bb98e13b68b95001b72bf79b5024df9235a`
5. Hermes patch 개발 root는 Forge repository 밖 `C:\01.project\.worktrees\INFINITY_FORGE\hermes-completion-policy`이고 Forge root는 `C:\01.project\INFINITY_FORGE`다. 설치본을 직접 개발 checkout으로 사용하지 않으며 Forge clean/secret scan 범위에 nested Git object store를 만들지 않는다.
6. Windows 설치 root는 `%LOCALAPPDATA%\hermes\hermes-agent`, Linux/VPS 설치 root는 `~/.hermes/hermes-agent`다.
7. 보호 정책은 정확히 `forge-v1` 하나이며 create 시점 이후 수정하는 API를 만들지 않는다. 같은 idempotency key로 다른 정책을 요청하면 기존 카드를 조용히 재사용하지 않고 실패한다.
8. Hermes core가 소비할 수 있는 receipt의 `phase`는 정확히 `hermes`다. `stop`, `post-exit`, `ci` phase receipt는 digest와 나머지 필드가 맞아도 거절한다.
9. 보호 태스크에는 active `current_run_id`가 있어야 한다. receipt의 task ID, run ID, policy, phase, verifier SHA와 expiry를 core가 다시 확인한다.
10. `complete_task()`의 성공 write transaction은 task `done`, run 종료, `completion_receipts` 삽입, `completion_receipt_consumed` 이벤트, `completed` 이벤트, 자식 `ready` 승격을 모두 포함한다. 그중 하나라도 실패하면 전부 rollback한다.
11. public `recompute_ready(conn, failure_limit=None) -> int`는 transaction wrapper로 유지한다. 실제 SQL은 `_recompute_ready_in_txn(conn, failure_limit: int | None = None) -> int`로 분리하고 `complete_task()`는 이미 열린 transaction 안에서 내부 함수를 호출한다.
12. 보호 완료 거절은 task, run, event, receipt ledger, child 상태를 변경하지 않는다. 오류 감사는 DB가 아니라 trusted guard log에 남긴다.
13. `protocol_violation`은 태스크의 `max_retries=4`보다 우선하여 첫 발생에 `needs_input` sticky block이 된다. 명시적 `unblock_task()` 전에는 `recompute_ready()`가 재승격하지 않는다.
14. 보호 완료의 result, summary, metadata는 `edit_completed_task_result()`로 사후 수정할 수 없다. 변경하려면 태스크를 다시 실행하고 새 Hermes receipt를 소비해야 한다.
15. 패치 installer는 전체 worktree clean을 요구하지 않지만 대상 파일은 clean이어야 하고 staged change는 전체 repo에 0개여야 한다. unrelated unstaged change의 porcelain 상태는 install과 rollback 전후 동일해야 한다.
16. 대상 파일 밖 변경을 stage, commit, restore, delete하지 않는다. `git reset --hard`, `git checkout --`, `git clean`을 금지한다.
17. DB migration은 additive다. 코드 rollback 때 `completion_policy`, `completion_receipt_digest`, `completion_receipts`를 제거하거나 DB snapshot을 복원하지 않는다.
18. verifier preflight와 repository filesystem은 SQLite와 하나의 cross-resource transaction이 될 수 없다. 전용 workspace, run CAS, 짧은 receipt expiry로 완화하되 완전 원자라고 표현하지 않는다.

---

## Root와 파일 책임

### 격리 Hermes patch worktree

| 파일 | 책임 |
|---|---|
| `hermes_cli/kanban_completion_policy.py` | trusted manifest 해석, artifact SHA 검증, verifier subprocess, Hermes 전용 receipt strict parsing |
| `hermes_cli/kanban_db.py` | policy/receipt schema, immutable creation, completion transaction, receipt replay 방지, sticky protocol block, result edit 보호 |
| `hermes_cli/kanban.py` | `--completion-policy` create 입력, JSON 출력, policy 오류의 안정된 CLI exit |
| `plugins/kanban/dashboard/plugin_api.py` | completion-policy 거절을 HTTP 409와 bulk per-item 오류로 투영 |
| `tests/hermes_cli/test_kanban_completion_policy.py` | schema, verifier, transaction, race, replay, caller 경로의 upstream regression tests |

### Forge repository

| 파일 | 책임 |
|---|---|
| `forge/patches/hermes/0.18.2/completion-policy.patch` | 위 Hermes target 파일에만 적용되는 binary-safe unified diff |
| `forge/patches/hermes/0.18.2/manifest.json` | supported base blob별 variant, variant별 7개 preimage와 target postimage hashes, patch hash, target allowlist |
| `forge/ops/hermes_patch.py` | variant 선택/preimage 검사, target-only install/verify/rollback, install record |
| `forge/scripts/hermes-patch.py` | `build`, `check`, `install`, `verify`, `rollback` CLI |
| `tests/hermes/test_hermes_patch.py` | manifest pins, patch hash, dirty-worktree 보존, target-only install/rollback tests |
| `docs/weapon/evidence/hermes-completion-policy-patch-rehearsal.md` | Windows blob, VPS blob, clean upstream 세 base의 apply/test/rollback 증거 |

## 고정 인터페이스

```text
CompletionPolicyError(classification: str, reason: str)
CompletionReceipt(
    phase: str,
    policy: str,
    task_id: str,
    run_id: int,
    receipt_digest: str,
    receipt_version: str,
    contract_digest: str,
    handoff_digest: str,
    repository_state_digest: str,
    verifier_sha256: str,
    issued_at: int,
    expires_at: int,
)
verify_completion(
    *,
    policy: str,
    task_id: str,
    run_id: int,
    board: str,
    workspace_path: str | None,
) -> CompletionReceipt
```

`guard/current.json`은 다음 exact nested schema만 허용한다. top-level required key는 `schema_version`, `policies`이고, `policies`의 required/유일 key는 `forge-v1`이다. `forge-v1` object의 required/유일 key는 `python`, `artifact`, `artifact_sha256`, `timeout_seconds`다. 모든 object는 `additionalProperties:false` 의미로 검사한다.

```json
{
  "schema_version": "forge-completion-manifest/v1",
  "policies": {
    "forge-v1": {
      "python": "C:\\Users\\operator\\AppData\\Local\\InfinityForge\\guard\\releases\\1111111111111111111111111111111111111111\\venv\\Scripts\\python.exe",
      "artifact": "C:\\Users\\operator\\AppData\\Local\\InfinityForge\\guard\\releases\\1111111111111111111111111111111111111111\\forge-guard.pyz",
      "artifact_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "timeout_seconds": 900
    }
  }
}
```

POSIX에서는 위 두 path가 `/home/operator/.local/share/infinity-forge/guard/releases/1111111111111111111111111111111111111111/venv/bin/python`과 `/home/operator/.local/share/infinity-forge/guard/releases/1111111111111111111111111111111111111111/forge-guard.pyz`인 동일 구조다. `python`과 `artifact`는 existing absolute file, `artifact_sha256`은 64자리 lowercase hex, `timeout_seconds`는 boolean이 아닌 integer `1..900`이어야 한다. source SHA는 `current.json`에 중복 저장하지 않고 immutable release directory 이름과 build manifest에서 검증한다.

Verifier request는 stdin의 단일 JSON object다.

```json
{
  "schema_version": "forge-completion-request/v1",
  "phase": "hermes",
  "policy": "forge-v1",
  "task_id": "t_0123456789ab",
  "run_id": 41,
  "board": "default",
  "workspace_path": "C:\\work\\repo"
}
```

Verifier allow response는 stdout의 단일 JSON object이며 exit 0이다.

```json
{
  "schema_version": "forge-completion-result/v1",
  "phase": "hermes",
  "decision": "allow",
  "classification": "PASS",
  "policy": "forge-v1",
  "task_id": "t_0123456789ab",
  "run_id": 41,
  "receipt_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "receipt_version": "forge-receipt/v1",
  "contract_digest": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "handoff_digest": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "repository_state_digest": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
  "verifier_sha256": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
  "issued_at": 1783828800,
  "expires_at": 1783829700
}
```

Verifier deny response는 exit 2이며 `classification`이 `TESTS_FAILED` 또는 `GATE_ERROR`다. spawn, manifest, hash, timeout, malformed JSON과 응답 mismatch는 core가 `GATE_ERROR`로 만든다.

```python
def _recompute_ready_in_txn(
    conn: sqlite3.Connection,
    failure_limit: int | None = None,
) -> int:
    """Caller-owned write transaction 안에서만 ready 승격 SQL을 실행한다."""

def recompute_ready(
    conn: sqlite3.Connection,
    failure_limit: int | None = None,
) -> int:
    """BEGIN IMMEDIATE를 소유하는 public wrapper다."""
```

Receipt DB schema는 다음으로 고정한다.

```sql
ALTER TABLE tasks ADD COLUMN completion_policy TEXT;
ALTER TABLE tasks ADD COLUMN completion_receipt_digest TEXT;

CREATE TABLE IF NOT EXISTS completion_receipts (
    digest       TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    run_id       INTEGER NOT NULL,
    policy       TEXT NOT NULL,
    payload      TEXT NOT NULL,
    consumed_at  INTEGER NOT NULL,
    UNIQUE(task_id, run_id)
);
```

---

### Task 1: Hermes 전용 trusted verifier adapter를 구현한다

**Files:**
- Create: `C:\01.project\.worktrees\INFINITY_FORGE\hermes-completion-policy\hermes_cli\kanban_completion_policy.py`
- Create: `C:\01.project\.worktrees\INFINITY_FORGE\hermes-completion-policy\tests\hermes_cli\test_kanban_completion_policy.py`

**Interfaces:**
- Consumes: OS별 `HERMES_COMPLETION_POLICY_MANIFEST` 또는 기본 manifest 절대 경로, `forge-completion-request/v1`, `forge-completion-result/v1`
- Produces: `CompletionPolicyError`, `CompletionReceipt`, `SUPPORTED_COMPLETION_POLICIES`, `resolve_manifest_path() -> Path`, `verify_completion(*, policy: str, task_id: str, run_id: int, board: str, workspace_path: str | None) -> CompletionReceipt`

- [ ] **Step 1: upstream base 격리 worktree를 만든다**

```powershell
$HermesRepo = "https://github.com/NousResearch/hermes-agent.git"
$HermesBase = "4281151ae859241351ba14d8c7682dc67ff4c126"
$HermesPatchWt = "C:\01.project\.worktrees\INFINITY_FORGE\hermes-completion-policy"
git clone --filter=blob:none --no-checkout $HermesRepo $HermesPatchWt
git -C $HermesPatchWt config core.longpaths true
git -C $HermesPatchWt fetch --depth 1 origin $HermesBase
git -C $HermesPatchWt checkout -b codex/hermes-completion-policy FETCH_HEAD
if ((git -C $HermesPatchWt rev-parse HEAD).Trim() -ne $HermesBase) {
  throw "Hermes patch development checkout mismatch"
}
```

Expected: 마지막 줄이 `4281151ae859241351ba14d8c7682dc67ff4c126`이다.

- [ ] **Step 2: 다음 실제 RED tests를 새 test 파일에 작성한다**

```python
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_completion_policy as cp


def _write_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    artifact = tmp_path / "guard.pyz"
    artifact.write_bytes(b"trusted-guard-zipapp")
    artifact_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
    manifest = tmp_path / "current.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "forge-completion-manifest/v1",
                "policies": {
                    "forge-v1": {
                        "python": sys.executable,
                        "artifact": str(artifact),
                        "artifact_sha256": artifact_sha,
                        "timeout_seconds": 900,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_COMPLETION_POLICY_MANIFEST", str(manifest))
    return artifact, artifact_sha


def _seal_allow_payload(payload: dict[str, object]) -> dict[str, object]:
    payload.pop("receipt_digest", None)
    payload["receipt_digest"] = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return payload


def _allow_payload(*, phase: str, task_id: str, run_id: int, verifier_sha: str) -> dict[str, object]:
    now = int(time.time())
    payload: dict[str, object] = {
        "schema_version": "forge-completion-result/v1",
        "phase": phase,
        "decision": "allow",
        "classification": "PASS",
        "policy": "forge-v1",
        "task_id": task_id,
        "run_id": run_id,
        "receipt_version": "forge-receipt/v1",
        "contract_digest": "b" * 64,
        "handoff_digest": "c" * 64,
        "repository_state_digest": "d" * 64,
        "verifier_sha256": verifier_sha,
        "issued_at": now - 1,
        "expires_at": now + 899,
    }
    return _seal_allow_payload(payload)


def test_verify_completion_accepts_pinned_hermes_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact, artifact_sha = _write_manifest(tmp_path, monkeypatch)
    calls: list[tuple[list[str], dict[str, object]]] = []
    payload = _allow_payload(
        phase="hermes",
        task_id="t_0123456789ab",
        run_id=41,
        verifier_sha=artifact_sha,
    )

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(cp.subprocess, "run", fake_run)
    receipt = cp.verify_completion(
        policy="forge-v1",
        task_id="t_0123456789ab",
        run_id=41,
        board="default",
        workspace_path=str(tmp_path / "repo"),
    )

    assert receipt.phase == "hermes"
    assert receipt.receipt_digest == payload["receipt_digest"]
    assert calls[0][0] == [sys.executable, str(artifact), "verify", "--phase", "hermes"]
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["timeout"] == 900
    request = json.loads(str(calls[0][1]["input"]))
    assert request == {
        "schema_version": "forge-completion-request/v1",
        "phase": "hermes",
        "policy": "forge-v1",
        "task_id": "t_0123456789ab",
        "run_id": 41,
        "board": "default",
        "workspace_path": str(tmp_path / "repo"),
    }


def test_verify_completion_rejects_receipt_at_exact_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, artifact_sha = _write_manifest(tmp_path, monkeypatch)
    now = 1_783_828_800
    payload = _allow_payload(
        phase="hermes",
        task_id="t_0123456789ab",
        run_id=41,
        verifier_sha=artifact_sha,
    )
    payload["issued_at"] = now - 900
    payload["expires_at"] = now
    _seal_allow_payload(payload)
    monkeypatch.setattr(cp.time, "time", lambda: float(now))
    monkeypatch.setattr(
        cp.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(payload), stderr=""
        ),
    )

    with pytest.raises(cp.CompletionPolicyError, match="not currently valid"):
        cp.verify_completion(
            policy="forge-v1",
            task_id="t_0123456789ab",
            run_id=41,
            board="default",
            workspace_path=str(tmp_path / "repo"),
        )


@pytest.mark.parametrize("mutation", ["extra-key", "digest", "ttl"])
def test_verify_completion_rejects_noncanonical_allow_result(
    mutation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, artifact_sha = _write_manifest(tmp_path, monkeypatch)
    payload = _allow_payload(
        phase="hermes",
        task_id="t_0123456789ab",
        run_id=41,
        verifier_sha=artifact_sha,
    )
    if mutation == "extra-key":
        payload["debug"] = "not allowed"
        _seal_allow_payload(payload)
        expected = "unexpected keys"
    elif mutation == "digest":
        payload["receipt_digest"] = "0" * 64
        expected = "receipt_digest mismatch"
    else:
        payload["expires_at"] = int(payload["issued_at"]) + 901
        _seal_allow_payload(payload)
        expected = "exactly 900 seconds"
    monkeypatch.setattr(
        cp.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(payload), stderr=""
        ),
    )

    with pytest.raises(cp.CompletionPolicyError, match=expected):
        cp.verify_completion(
            policy="forge-v1",
            task_id="t_0123456789ab",
            run_id=41,
            board="default",
            workspace_path=str(tmp_path / "repo"),
        )


@pytest.mark.parametrize(
    "non_finite",
    [float("nan"), float("inf"), float("-inf")],
)
def test_verify_completion_maps_non_finite_json_to_typed_gate_error(
    non_finite: float,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, artifact_sha = _write_manifest(tmp_path, monkeypatch)
    payload = _allow_payload(
        phase="hermes",
        task_id="t_0123456789ab",
        run_id=41,
        verifier_sha=artifact_sha,
    )
    payload["issued_at"] = non_finite
    monkeypatch.setattr(
        cp.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(payload), stderr=""
        ),
    )

    with pytest.raises(cp.CompletionPolicyError) as exc_info:
        cp.verify_completion(
            policy="forge-v1",
            task_id="t_0123456789ab",
            run_id=41,
            board="default",
            workspace_path=str(tmp_path / "repo"),
        )
    assert exc_info.value.classification == "GATE_ERROR"
    assert "non-finite JSON number" in exc_info.value.reason


@pytest.mark.parametrize("phase", ["stop", "post-exit", "ci"])
def test_verify_completion_rejects_non_hermes_phase(
    phase: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, artifact_sha = _write_manifest(tmp_path, monkeypatch)

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(
                _allow_payload(
                    phase=phase,
                    task_id="t_0123456789ab",
                    run_id=41,
                    verifier_sha=artifact_sha,
                )
            ),
            stderr="",
        )

    monkeypatch.setattr(cp.subprocess, "run", fake_run)
    with pytest.raises(cp.CompletionPolicyError) as exc_info:
        cp.verify_completion(
            policy="forge-v1",
            task_id="t_0123456789ab",
            run_id=41,
            board="default",
            workspace_path=None,
        )
    assert exc_info.value.classification == "GATE_ERROR"
    assert "phase" in exc_info.value.reason


def test_verify_completion_preserves_tests_failed_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_manifest(tmp_path, monkeypatch)

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv,
            2,
            stdout=json.dumps(
                {
                    "schema_version": "forge-completion-result/v1",
                    "phase": "hermes",
                    "decision": "deny",
                    "classification": "TESTS_FAILED",
                    "policy": "forge-v1",
                    "task_id": "t_0123456789ab",
                    "run_id": 41,
                    "reason": "AC-2 has no verification evidence",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(cp.subprocess, "run", fake_run)
    with pytest.raises(cp.CompletionPolicyError) as exc_info:
        cp.verify_completion(
            policy="forge-v1",
            task_id="t_0123456789ab",
            run_id=41,
            board="default",
            workspace_path=None,
        )
    assert exc_info.value.classification == "TESTS_FAILED"
    assert exc_info.value.reason == "AC-2 has no verification evidence"


def test_verify_completion_rejects_deny_result_with_extra_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_manifest(tmp_path, monkeypatch)
    response = {
        "schema_version": "forge-completion-result/v1",
        "phase": "hermes",
        "decision": "deny",
        "classification": "GATE_ERROR",
        "policy": "forge-v1",
        "task_id": "t_0123456789ab",
        "run_id": 41,
        "reason": "manifest mismatch",
        "debug": "not allowed",
    }
    monkeypatch.setattr(
        cp.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 2, stdout=json.dumps(response), stderr=""
        ),
    )

    with pytest.raises(cp.CompletionPolicyError, match="unexpected keys"):
        cp.verify_completion(
            policy="forge-v1",
            task_id="t_0123456789ab",
            run_id=41,
            board="default",
            workspace_path=None,
        )


def test_verify_completion_fails_closed_on_artifact_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact, _ = _write_manifest(tmp_path, monkeypatch)
    artifact.write_bytes(b"modified-after-deployment")
    called = False

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    monkeypatch.setattr(cp.subprocess, "run", fake_run)
    with pytest.raises(cp.CompletionPolicyError) as exc_info:
        cp.verify_completion(
            policy="forge-v1",
            task_id="t_0123456789ab",
            run_id=41,
            board="default",
            workspace_path=None,
        )
    assert exc_info.value.classification == "GATE_ERROR"
    assert "artifact SHA-256 mismatch" in exc_info.value.reason
    assert called is False


def test_guard_current_manifest_rejects_extra_nested_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_manifest(tmp_path, monkeypatch)
    manifest_path = tmp_path / "current.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["policies"]["forge-v1"]["source_sha"] = "1" * 40
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(cp.CompletionPolicyError) as exc_info:
        cp.verify_completion(
            policy="forge-v1",
            task_id="t_0123456789ab",
            run_id=41,
            board="default",
            workspace_path=None,
        )
    assert exc_info.value.classification == "GATE_ERROR"
    assert "unexpected keys" in exc_info.value.reason
```

- [ ] **Step 3: RED를 실행한다**

```powershell
$HermesPython = "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe"
Set-Location C:\01.project\.worktrees\INFINITY_FORGE\hermes-completion-policy
& $HermesPython -m pytest tests/hermes_cli/test_kanban_completion_policy.py -q
```

Expected: collection 중 `ImportError: cannot import name 'kanban_completion_policy' from 'hermes_cli'`로 FAIL한다.

- [ ] **Step 4: 다음 최소 GREEN module을 구현한다**

```python
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SUPPORTED_COMPLETION_POLICIES = frozenset({"forge-v1"})
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_SCHEMA = "forge-completion-manifest/v1"
_REQUEST_SCHEMA = "forge-completion-request/v1"
_RESULT_SCHEMA = "forge-completion-result/v1"
_RECEIPT_VERSION = "forge-receipt/v1"
_ALLOW_RESULT_KEYS = frozenset(
    {
        "schema_version",
        "phase",
        "decision",
        "classification",
        "policy",
        "task_id",
        "run_id",
        "receipt_digest",
        "receipt_version",
        "contract_digest",
        "handoff_digest",
        "repository_state_digest",
        "verifier_sha256",
        "issued_at",
        "expires_at",
    }
)
_DENY_RESULT_KEYS = frozenset(
    {
        "schema_version",
        "phase",
        "decision",
        "classification",
        "policy",
        "task_id",
        "run_id",
        "reason",
    }
)


class CompletionPolicyError(ValueError):
    def __init__(self, classification: str, reason: str):
        self.classification = classification
        self.reason = reason
        super().__init__(f"{classification}: {reason}")


@dataclass(frozen=True)
class CompletionReceipt:
    phase: str
    policy: str
    task_id: str
    run_id: int
    receipt_digest: str
    receipt_version: str
    contract_digest: str
    handoff_digest: str
    repository_state_digest: str
    verifier_sha256: str
    issued_at: int
    expires_at: int

    def event_payload(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "policy": self.policy,
            "receipt_digest": self.receipt_digest,
            "receipt_version": self.receipt_version,
            "contract_digest": self.contract_digest,
            "handoff_digest": self.handoff_digest,
            "repository_state_digest": self.repository_state_digest,
            "verifier_sha256": self.verifier_sha256,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }


def _gate_error(reason: str) -> CompletionPolicyError:
    return CompletionPolicyError("GATE_ERROR", reason)


def resolve_manifest_path() -> Path:
    override = os.environ.get("HERMES_COMPLETION_POLICY_MANIFEST")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA")
        if not root:
            raise _gate_error("LOCALAPPDATA is not set")
        return Path(root, "InfinityForge", "guard", "current.json").resolve()
    data_home = os.environ.get("XDG_DATA_HOME")
    root = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return (root / "infinity-forge" / "guard" / "current.json").resolve()


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_non_finite,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise _gate_error(f"cannot read valid {label}: {path}") from exc
    if not isinstance(value, dict):
        raise _gate_error(f"{label} must be a JSON object")
    return value


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise _gate_error(f"cannot hash verifier artifact: {path}") from exc
    return digest.hexdigest()


def _sha256_json(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _required_text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise _gate_error(f"result field {key!r} must be a non-empty string")
    return value


def _required_digest(data: dict[str, Any], key: str) -> str:
    value = _required_text(data, key)
    if not _HEX64.fullmatch(value):
        raise _gate_error(f"result field {key!r} must be a lowercase SHA-256")
    return value


def _required_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _gate_error(f"result field {key!r} must be an integer")
    return value


def _validate_identity(
    data: dict[str, Any],
    *,
    policy: str,
    task_id: str,
    run_id: int,
) -> None:
    if data.get("schema_version") != _RESULT_SCHEMA:
        raise _gate_error("unexpected verifier result schema_version")
    if data.get("phase") != "hermes":
        raise _gate_error("verifier result phase must be 'hermes'")
    if data.get("policy") != policy:
        raise _gate_error("verifier result policy mismatch")
    if data.get("task_id") != task_id:
        raise _gate_error("verifier result task_id mismatch")
    if data.get("run_id") != run_id:
        raise _gate_error("verifier result run_id mismatch")


def verify_completion(
    *,
    policy: str,
    task_id: str,
    run_id: int,
    board: str,
    workspace_path: str | None,
) -> CompletionReceipt:
    if policy not in SUPPORTED_COMPLETION_POLICIES:
        raise _gate_error(f"unsupported completion policy: {policy}")
    if run_id < 1:
        raise _gate_error("run_id must be positive")

    manifest_path = resolve_manifest_path()
    manifest = _read_object(manifest_path, "completion-policy manifest")
    if manifest.get("schema_version") != _MANIFEST_SCHEMA:
        raise _gate_error("unexpected completion-policy manifest schema_version")
    if set(manifest) != {"schema_version", "policies"}:
        raise _gate_error("completion-policy manifest has unexpected top-level keys")
    policies = manifest.get("policies")
    if not isinstance(policies, dict) or set(policies) != {"forge-v1"}:
        raise _gate_error("manifest policies must contain only forge-v1")
    if not isinstance(policies.get(policy), dict):
        raise _gate_error(f"manifest has no configuration for policy {policy}")
    config = policies[policy]
    if set(config) != {"python", "artifact", "artifact_sha256", "timeout_seconds"}:
        raise _gate_error("forge-v1 manifest policy has unexpected keys")

    python_path = Path(_required_text(config, "python"))
    artifact_path = Path(_required_text(config, "artifact"))
    expected_sha = _required_digest(config, "artifact_sha256")
    timeout_seconds = _required_int(config, "timeout_seconds")
    if not python_path.is_absolute() or not python_path.is_file():
        raise _gate_error("manifest python must be an existing absolute file")
    if not artifact_path.is_absolute() or not artifact_path.is_file():
        raise _gate_error("manifest artifact must be an existing absolute file")
    actual_sha = _sha256_file(artifact_path)
    if actual_sha != expected_sha:
        raise _gate_error("verifier artifact SHA-256 mismatch")
    if timeout_seconds < 1 or timeout_seconds > 900:
        raise _gate_error("manifest timeout_seconds must be between 1 and 900")

    request = {
        "schema_version": _REQUEST_SCHEMA,
        "phase": "hermes",
        "policy": policy,
        "task_id": task_id,
        "run_id": run_id,
        "board": board,
        "workspace_path": workspace_path,
    }
    argv = [str(python_path), str(artifact_path), "verify", "--phase", "hermes"]
    try:
        completed = subprocess.run(
            argv,
            input=json.dumps(request, separators=(",", ":"), ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _gate_error("trusted verifier process did not complete") from exc
    if completed.returncode not in {0, 2}:
        raise _gate_error(f"trusted verifier returned unexpected exit {completed.returncode}")
    try:
        response = json.loads(
            completed.stdout,
            parse_constant=_reject_non_finite,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        if "non-finite JSON number" in str(exc):
            raise _gate_error(str(exc)) from exc
        raise _gate_error("trusted verifier stdout is not valid JSON") from exc
    if not isinstance(response, dict):
        raise _gate_error("trusted verifier result must be a JSON object")

    _validate_identity(response, policy=policy, task_id=task_id, run_id=run_id)
    decision = response.get("decision")
    classification = response.get("classification")
    if decision == "deny":
        if set(response) != _DENY_RESULT_KEYS:
            raise _gate_error("deny verifier result has unexpected keys")
        if completed.returncode != 2 or classification not in {"TESTS_FAILED", "GATE_ERROR"}:
            raise _gate_error("deny result has invalid exit or classification")
        reason = _required_text(response, "reason")
        raise CompletionPolicyError(str(classification), reason)
    if decision != "allow" or completed.returncode != 0 or classification != "PASS":
        raise _gate_error("allow result has invalid exit, decision, or classification")
    if set(response) != _ALLOW_RESULT_KEYS:
        raise _gate_error("allow verifier result has unexpected keys")
    digest_input = dict(response)
    provided_digest = _required_digest(digest_input, "receipt_digest")
    del digest_input["receipt_digest"]
    if provided_digest != _sha256_json(digest_input):
        raise _gate_error("receipt_digest mismatch")

    receipt = CompletionReceipt(
        phase="hermes",
        policy=policy,
        task_id=task_id,
        run_id=run_id,
        receipt_digest=_required_digest(response, "receipt_digest"),
        receipt_version=_required_text(response, "receipt_version"),
        contract_digest=_required_digest(response, "contract_digest"),
        handoff_digest=_required_digest(response, "handoff_digest"),
        repository_state_digest=_required_digest(response, "repository_state_digest"),
        verifier_sha256=_required_digest(response, "verifier_sha256"),
        issued_at=_required_int(response, "issued_at"),
        expires_at=_required_int(response, "expires_at"),
    )
    now = int(time.time())
    if receipt.receipt_version != _RECEIPT_VERSION:
        raise _gate_error("unexpected receipt_version")
    if receipt.verifier_sha256 != actual_sha:
        raise _gate_error("receipt verifier_sha256 mismatch")
    if receipt.expires_at != receipt.issued_at + 900:
        raise _gate_error("Hermes receipt lifetime must be exactly 900 seconds")
    if receipt.issued_at > now or receipt.expires_at <= now or receipt.expires_at <= receipt.issued_at:
        raise _gate_error("receipt is not currently valid")
    return receipt
```

- [ ] **Step 5: GREEN을 실행한다**

```powershell
& $HermesPython -m pytest tests/hermes_cli/test_kanban_completion_policy.py -q
```

Expected: `14 passed`다. parametrized phase/result mutation/non-finite case가 개별 case로 집계된다.

- [ ] **Step 6: 격리 Hermes patch branch에 commit한다**

```powershell
git add hermes_cli/kanban_completion_policy.py tests/hermes_cli/test_kanban_completion_policy.py
git commit -m "feat: validate trusted Hermes completion receipts"
```

---

### Task 2: completion policy와 replay-safe receipt schema를 추가한다

**Files:**
- Modify: `C:\01.project\.worktrees\INFINITY_FORGE\hermes-completion-policy\hermes_cli\kanban_db.py:838-1001,1095-1235,1852-2016,2386-2683`
- Modify: `C:\01.project\.worktrees\INFINITY_FORGE\hermes-completion-policy\hermes_cli\kanban.py:60-83,341-369,1305-1354`
- Modify: `C:\01.project\.worktrees\INFINITY_FORGE\hermes-completion-policy\tests\hermes_cli\test_kanban_completion_policy.py`

**Interfaces:**
- Consumes: Task 1의 `SUPPORTED_COMPLETION_POLICIES`, `CompletionReceipt`
- Produces: `Task.completion_policy: str | None`, `Task.completion_receipt_digest: str | None`, `create_task(conn: sqlite3.Connection, *, title: str, body: str | None = None, assignee: str | None = None, created_by: str | None = None, workspace_kind: str = "scratch", workspace_path: str | None = None, branch_name: str | None = None, tenant: str | None = None, priority: int = 0, parents: Iterable[str] = (), triage: bool = False, idempotency_key: str | None = None, max_runtime_seconds: int | None = None, skills: Iterable[str] | None = None, max_retries: int | None = None, goal_mode: bool = False, goal_max_turns: int | None = None, initial_status: str = "running", session_id: str | None = None, board: str | None = None, project_id: str | None = None, completion_policy: str | None = None) -> str`, `completion_receipts` table, CLI `--completion-policy forge-v1`

- [ ] **Step 1: test 파일 import와 fixture를 추가하고 다음 실제 RED tests를 작성한다**

```python
import argparse
import sqlite3

from hermes_cli import kanban
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_fresh_schema_has_policy_columns_and_receipt_ledger(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        ledger = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='completion_receipts'"
        ).fetchone()
    assert {"completion_policy", "completion_receipt_digest"} <= columns
    assert ledger is not None


def test_legacy_schema_migrates_policy_columns_and_receipt_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "legacy-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = home / "kanban.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL,
            priority INTEGER DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            claim_lock TEXT,
            claim_expires INTEGER
        );
        """
    )
    raw.commit()
    raw.close()

    kb.init_db(db_path=db_path)
    kb.init_db(db_path=db_path)
    with kb.connect_closing(db_path=db_path) as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        ledger_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='completion_receipts'"
        ).fetchone()["sql"]
    assert {"completion_policy", "completion_receipt_digest"} <= columns
    assert "UNIQUE(task_id, run_id)" in ledger_sql


def test_create_task_persists_immutable_completion_policy(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="protected",
            assignee="executor",
            completion_policy="forge-v1",
            idempotency_key="issue:77:executor",
        )
        task = kb.get_task(conn, task_id)
        same_id = kb.create_task(
            conn,
            title="protected retry",
            assignee="executor",
            completion_policy="forge-v1",
            idempotency_key="issue:77:executor",
        )
        with pytest.raises(ValueError, match="completion_policy mismatch"):
            kb.create_task(
                conn,
                title="unsafe retry",
                assignee="executor",
                completion_policy=None,
                idempotency_key="issue:77:executor",
            )
    assert task is not None
    assert task.completion_policy == "forge-v1"
    assert task.completion_receipt_digest is None
    assert same_id == task_id


def test_create_task_rejects_unknown_completion_policy(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        with pytest.raises(ValueError, match="unsupported completion_policy"):
            kb.create_task(
                conn,
                title="unknown policy",
                assignee="executor",
                completion_policy="forge-v2",
            )


def test_cli_parser_accepts_only_forge_v1_completion_policy() -> None:
    root = argparse.ArgumentParser()
    subparsers = root.add_subparsers(dest="root_command")
    kanban.build_parser(subparsers)
    args = root.parse_args(
        [
            "kanban",
            "create",
            "protected card",
            "--assignee",
            "executor",
            "--completion-policy",
            "forge-v1",
        ]
    )
    assert args.completion_policy == "forge-v1"
    with pytest.raises(SystemExit) as exc_info:
        root.parse_args(
            [
                "kanban",
                "create",
                "unknown card",
                "--assignee",
                "executor",
                "--completion-policy",
                "forge-v2",
            ]
        )
    assert exc_info.value.code == 2
```

- [ ] **Step 2: RED를 실행한다**

```powershell
& $HermesPython -m pytest tests/hermes_cli/test_kanban_completion_policy.py -q
```

Expected: `completion_policy` column assertion과 `create_task() got an unexpected keyword argument 'completion_policy'` 때문에 FAIL한다.

- [ ] **Step 3: Task dataclass, fresh schema와 additive migration을 최소 구현한다**

`kanban_db.py` import 구역에 다음 module import를 추가하고, `Task`의 default field 구역과 `from_row()`에 다음 필드를 정확히 추가한다.

```python
from hermes_cli import kanban_completion_policy as completion_policy_gate
```

```python
completion_policy: str | None = None
completion_receipt_digest: str | None = None
```

```python
completion_policy=(
    row["completion_policy"] if "completion_policy" in keys else None
),
completion_receipt_digest=(
    row["completion_receipt_digest"]
    if "completion_receipt_digest" in keys else None
),
```

`SCHEMA_SQL`의 `tasks` 마지막 columns와 `task_links` 앞에 다음 SQL을 넣는다.

```sql
    block_recurrences           INTEGER NOT NULL DEFAULT 0,
    completion_policy           TEXT,
    completion_receipt_digest  TEXT
);

CREATE TABLE IF NOT EXISTS completion_receipts (
    digest       TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    run_id       INTEGER NOT NULL,
    policy       TEXT NOT NULL,
    payload      TEXT NOT NULL,
    consumed_at  INTEGER NOT NULL,
    UNIQUE(task_id, run_id)
);
```

`_migrate_add_optional_columns()`의 task column migration에 다음 idempotent additions를 넣는다.

```python
if "completion_policy" not in cols:
    _add_column_if_missing(
        conn, "tasks", "completion_policy", "completion_policy TEXT"
    )
if "completion_receipt_digest" not in cols:
    _add_column_if_missing(
        conn,
        "tasks",
        "completion_receipt_digest",
        "completion_receipt_digest TEXT",
    )
conn.execute(
    """
    CREATE TABLE IF NOT EXISTS completion_receipts (
        digest       TEXT PRIMARY KEY,
        task_id      TEXT NOT NULL,
        run_id       INTEGER NOT NULL,
        policy       TEXT NOT NULL,
        payload      TEXT NOT NULL,
        consumed_at  INTEGER NOT NULL,
        UNIQUE(task_id, run_id)
    )
    """
)
```

- [ ] **Step 4: immutable create와 idempotency mismatch를 최소 구현한다**

`create_task()` keyword parameters에 다음을 추가한다.

```python
completion_policy: str | None = None,
```

title 검증 직후 다음 validation을 넣는다.

```python
if (
    completion_policy is not None
    and completion_policy not in completion_policy_gate.SUPPORTED_COMPLETION_POLICIES
):
    raise ValueError(f"unsupported completion_policy: {completion_policy}")
```

idempotency query와 return을 다음 exact block으로 바꾼다.

```python
if idempotency_key:
    row = conn.execute(
        "SELECT id, completion_policy FROM tasks WHERE idempotency_key = ? "
        "AND status != 'archived' ORDER BY created_at DESC LIMIT 1",
        (idempotency_key,),
    ).fetchone()
    if row:
        existing_policy = (
            row["completion_policy"]
            if "completion_policy" in row.keys()
            else None
        )
        if existing_policy != completion_policy:
            raise ValueError(
                "completion_policy mismatch for existing idempotency_key: "
                f"requested={completion_policy!r}, existing={existing_policy!r}"
            )
        return row["id"]
```

`INSERT INTO tasks` statement를 다음 exact block으로 교체한다.

```python
conn.execute(
    """
    INSERT INTO tasks (
        id, title, body, assignee, status, priority,
        created_by, created_at, workspace_kind, workspace_path,
        branch_name, project_id, tenant, idempotency_key,
        max_runtime_seconds,
        skills, max_retries, goal_mode, goal_max_turns, session_id,
        completion_policy
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    (
        task_id,
        title.strip(),
        body,
        assignee,
        task_status,
        priority,
        created_by,
        now,
        workspace_kind,
        workspace_path,
        branch_name,
        project_id,
        tenant,
        idempotency_key,
        int(max_runtime_seconds) if max_runtime_seconds is not None else None,
        json.dumps(skills_list) if skills_list is not None else None,
        int(max_retries) if max_retries is not None else None,
        1 if goal_mode else 0,
        int(goal_max_turns) if goal_max_turns is not None else None,
        session_id,
        completion_policy,
    ),
)
```

`created` event payload에도 다음 field를 넣는다.

```text
"completion_policy": completion_policy,
```

- [ ] **Step 5: CLI create 입력과 JSON 관측을 최소 구현한다**

`_task_to_dict()`에 다음 두 fields를 추가한다.

```text
"completion_policy": t.completion_policy,
"completion_receipt_digest": t.completion_receipt_digest,
```

create parser에 다음 argument를 추가한다.

```python
p_create.add_argument(
    "--completion-policy",
    choices=["forge-v1"],
    default=None,
    help="Immutable completion policy. forge-v1 requires a trusted Hermes receipt.",
)
```

`_cmd_create()`의 `kb.create_task()` call에 다음 keyword를 추가한다.

```python
completion_policy=getattr(args, "completion_policy", None),
```

- [ ] **Step 6: GREEN과 기존 schema/create 회귀를 실행한다**

```powershell
& $HermesPython -m pytest tests/hermes_cli/test_kanban_completion_policy.py tests/hermes_cli/test_kanban_db.py -q
```

Expected: 두 test 파일이 모두 PASS한다.

- [ ] **Step 7: commit한다**

```powershell
git add hermes_cli/kanban_db.py hermes_cli/kanban.py tests/hermes_cli/test_kanban_completion_policy.py
git commit -m "feat: persist immutable completion policies"
```

---

### Task 3: receipt 소비와 자식 승격을 하나의 completion transaction으로 만든다

**Files:**
- Modify: `C:\01.project\.worktrees\INFINITY_FORGE\hermes-completion-policy\hermes_cli\kanban_db.py:3281-3365,3978-4170,6736-6751`
- Modify: `C:\01.project\.worktrees\INFINITY_FORGE\hermes-completion-policy\tests\hermes_cli\test_kanban_completion_policy.py`

**Interfaces:**
- Consumes: Task 1의 `CompletionReceipt`, `CompletionPolicyError`, `verify_completion(*, policy: str, task_id: str, run_id: int, board: str, workspace_path: str | None) -> CompletionReceipt`; Task 2의 task columns와 `completion_receipts`
- Produces: `_recompute_ready_in_txn(conn: sqlite3.Connection, failure_limit: int | None = None) -> int`, `recompute_ready(conn: sqlite3.Connection, failure_limit: int | None = None) -> int`, `_complete_task_in_txn(conn: sqlite3.Connection, task_id: str, *, result: str | None, summary: str | None, metadata: dict | None, verified_cards: list[str], expected_run_id: int | None, completion_receipt: CompletionReceipt | None, now: int) -> tuple[bool, int | None]`, `complete_task(conn: sqlite3.Connection, task_id: str, *, result: str | None = None, summary: str | None = None, metadata: dict | None = None, created_cards: Iterable[str] | None = None, expected_run_id: int | None = None) -> bool`

- [ ] **Step 1: test imports와 공용 helpers를 추가한다**

```python
import threading
from dataclasses import replace


def _claim_protected(
    conn: sqlite3.Connection,
    *,
    title: str,
    digest_seed: str,
) -> tuple[str, int, cp.CompletionReceipt]:
    task_id = kb.create_task(
        conn,
        title=title,
        assignee="executor",
        completion_policy="forge-v1",
    )
    claimed = kb.claim_task(conn, task_id, claimer=f"test:{title}")
    assert claimed is not None
    assert claimed.current_run_id is not None
    now = int(time.time())
    receipt = cp.CompletionReceipt(
        phase="hermes",
        policy="forge-v1",
        task_id=task_id,
        run_id=claimed.current_run_id,
        receipt_digest=digest_seed * 64,
        receipt_version="forge-receipt/v1",
        contract_digest="b" * 64,
        handoff_digest="c" * 64,
        repository_state_digest="d" * 64,
        verifier_sha256="e" * 64,
        issued_at=now - 1,
        expires_at=now + 899,
    )
    return task_id, claimed.current_run_id, receipt


def _completion_snapshot(
    conn: sqlite3.Connection,
    task_id: str,
    child_id: str,
) -> dict[str, object]:
    return {
        "task": dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()),
        "child": dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (child_id,)).fetchone()),
        "runs": [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id",
                (task_id,),
            ).fetchall()
        ],
        "events": [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM task_events WHERE task_id = ? ORDER BY id",
                (task_id,),
            ).fetchall()
        ],
        "receipts": [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM completion_receipts WHERE task_id = ? ORDER BY consumed_at, digest",
                (task_id,),
            ).fetchall()
        ],
    }
```

- [ ] **Step 2: 거절 무변경, phase, replay의 실제 RED tests를 작성한다**

```python
def test_protected_completion_rejection_changes_no_db_state(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with kb.connect_closing() as conn:
        task_id, run_id, _ = _claim_protected(conn, title="reject", digest_seed="1")
        child_id = kb.create_task(
            conn,
            title="child",
            assignee="reviewer",
            parents=[task_id],
        )
        before = _completion_snapshot(conn, task_id, child_id)

        def reject(**kwargs: object) -> cp.CompletionReceipt:
            assert kwargs["task_id"] == task_id
            assert kwargs["run_id"] == run_id
            raise cp.CompletionPolicyError("TESTS_FAILED", "handoff missing AC-2")

        monkeypatch.setattr(cp, "verify_completion", reject)
        with pytest.raises(cp.CompletionPolicyError, match="handoff missing AC-2"):
            kb.complete_task(
                conn,
                task_id,
                summary="premature",
                expected_run_id=run_id,
            )
        after = _completion_snapshot(conn, task_id, child_id)
    assert after == before


def test_core_rejects_non_hermes_receipt_even_if_adapter_returns_it(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with kb.connect_closing() as conn:
        task_id, run_id, receipt = _claim_protected(conn, title="wrong-phase", digest_seed="2")
        child_id = kb.create_task(
            conn,
            title="wrong-phase-child",
            assignee="reviewer",
            parents=[task_id],
        )
        wrong_phase = cp.CompletionReceipt(
            phase="post-exit",
            policy=receipt.policy,
            task_id=receipt.task_id,
            run_id=receipt.run_id,
            receipt_digest=receipt.receipt_digest,
            receipt_version=receipt.receipt_version,
            contract_digest=receipt.contract_digest,
            handoff_digest=receipt.handoff_digest,
            repository_state_digest=receipt.repository_state_digest,
            verifier_sha256=receipt.verifier_sha256,
            issued_at=receipt.issued_at,
            expires_at=receipt.expires_at,
        )
        monkeypatch.setattr(cp, "verify_completion", lambda **kwargs: wrong_phase)
        before = _completion_snapshot(conn, task_id, child_id)
        with pytest.raises(cp.CompletionPolicyError, match="phase must be 'hermes'"):
            kb.complete_task(conn, task_id, summary="wrong phase", expected_run_id=run_id)
        after = _completion_snapshot(conn, task_id, child_id)
    assert after == before


@pytest.mark.parametrize("expires_delta", [-1, 0])
def test_core_rejects_expired_receipt_even_if_adapter_returns_it(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    expires_delta: int,
) -> None:
    with kb.connect_closing() as conn:
        task_id, run_id, receipt = _claim_protected(
            conn, title="expired", digest_seed="6"
        )
        child_id = kb.create_task(
            conn,
            title="expired-child",
            assignee="reviewer",
            parents=[task_id],
        )
        now = int(time.time())
        expired = replace(
            receipt,
            issued_at=now + expires_delta - 900,
            expires_at=now + expires_delta,
        )
        monkeypatch.setattr(cp, "verify_completion", lambda **kwargs: expired)
        before = _completion_snapshot(conn, task_id, child_id)
        with pytest.raises(cp.CompletionPolicyError, match="receipt is expired"):
            kb.complete_task(conn, task_id, summary="expired", expected_run_id=run_id)
        after = _completion_snapshot(conn, task_id, child_id)
    assert after == before


def test_core_rejects_non_900_second_receipt_even_if_adapter_returns_it(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with kb.connect_closing() as conn:
        task_id, run_id, receipt = _claim_protected(
            conn, title="long-lived", digest_seed="7"
        )
        child_id = kb.create_task(
            conn,
            title="long-lived-child",
            assignee="reviewer",
            parents=[task_id],
        )
        invalid = replace(
            receipt,
            expires_at=receipt.issued_at + 901,
        )
        monkeypatch.setattr(cp, "verify_completion", lambda **kwargs: invalid)
        before = _completion_snapshot(conn, task_id, child_id)
        with pytest.raises(cp.CompletionPolicyError, match="expired or not yet valid"):
            kb.complete_task(
                conn,
                task_id,
                summary="long lived",
                expected_run_id=run_id,
            )
        after = _completion_snapshot(conn, task_id, child_id)
    assert after == before


def test_receipt_digest_replay_rolls_back_second_completion(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with kb.connect_closing() as conn:
        first_id, first_run, first_receipt = _claim_protected(
            conn, title="first", digest_seed="f"
        )
        second_id, second_run, second_receipt = _claim_protected(
            conn, title="second", digest_seed="f"
        )
        child_id = kb.create_task(
            conn,
            title="second-child",
            assignee="reviewer",
            parents=[second_id],
        )
        receipts = {first_id: first_receipt, second_id: second_receipt}
        monkeypatch.setattr(
            cp,
            "verify_completion",
            lambda **kwargs: receipts[str(kwargs["task_id"])],
        )
        assert kb.complete_task(
            conn,
            first_id,
            summary="first done",
            expected_run_id=first_run,
        )
        before_second = _completion_snapshot(conn, second_id, child_id)
        with pytest.raises(cp.CompletionPolicyError, match="already consumed"):
            kb.complete_task(
                conn,
                second_id,
                summary="replay",
                expected_run_id=second_run,
            )
        after_second = _completion_snapshot(conn, second_id, child_id)
    assert after_second == before_second
```

- [ ] **Step 3: nested transaction 방지와 성공 원자성의 실제 RED tests를 작성한다**

```python
def test_complete_uses_internal_recompute_inside_single_transaction(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with kb.connect_closing() as conn:
        task_id, run_id, receipt = _claim_protected(conn, title="single-txn", digest_seed="3")
        child_id = kb.create_task(
            conn,
            title="single-txn-child",
            assignee="reviewer",
            parents=[task_id],
        )
        monkeypatch.setattr(cp, "verify_completion", lambda **kwargs: receipt)
        internal_states: list[bool] = []
        original_internal = kb._recompute_ready_in_txn

        def checked_internal(
            active_conn: sqlite3.Connection,
            failure_limit: int | None = None,
        ) -> int:
            internal_states.append(active_conn.in_transaction)
            return original_internal(active_conn, failure_limit=failure_limit)

        def forbidden_public(
            active_conn: sqlite3.Connection,
            failure_limit: int | None = None,
        ) -> int:
            pytest.fail("complete_task must not call public recompute_ready")

        monkeypatch.setattr(kb, "_recompute_ready_in_txn", checked_internal)
        monkeypatch.setattr(kb, "recompute_ready", forbidden_public)
        sql: list[str] = []
        conn.set_trace_callback(sql.append)
        assert kb.complete_task(
            conn,
            task_id,
            summary="atomic",
            expected_run_id=run_id,
        )
        conn.set_trace_callback(None)
        child = kb.get_task(conn, child_id)
        ledger = conn.execute(
            "SELECT * FROM completion_receipts WHERE digest = ?",
            (receipt.receipt_digest,),
        ).fetchone()
    assert internal_states == [True]
    assert sum(statement.upper().startswith("BEGIN IMMEDIATE") for statement in sql) == 1
    assert child is not None and child.status == "ready"
    assert ledger is not None and ledger["run_id"] == run_id


def test_complete_child_and_receipt_are_not_observable_before_commit(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with kb.connect_closing() as setup_conn:
        task_id, run_id, receipt = _claim_protected(
            setup_conn, title="race", digest_seed="4"
        )
        child_id = kb.create_task(
            setup_conn,
            title="race-child",
            assignee="reviewer",
            parents=[task_id],
        )

    monkeypatch.setattr(cp, "verify_completion", lambda **kwargs: receipt)
    reached_receipt_event = threading.Event()
    release_writer = threading.Event()
    original_append = kb._append_event
    writer_errors: list[BaseException] = []

    def pausing_append(
        conn: sqlite3.Connection,
        task_id_arg: str,
        kind: str,
        payload: dict[str, object] | None,
        run_id: int | None = None,
    ) -> int:
        if task_id_arg == task_id and kind == "completion_receipt_consumed":
            reached_receipt_event.set()
            if not release_writer.wait(timeout=5):
                raise RuntimeError("test did not release completion writer")
        return original_append(
            conn,
            task_id_arg,
            kind,
            payload,
            run_id=run_id,
        )

    monkeypatch.setattr(kb, "_append_event", pausing_append)

    def writer() -> None:
        try:
            with kb.connect_closing() as writer_conn:
                assert kb.complete_task(
                    writer_conn,
                    task_id,
                    summary="race done",
                    expected_run_id=run_id,
                )
        except BaseException as exc:
            writer_errors.append(exc)

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    assert reached_receipt_event.wait(timeout=5)
    try:
        with kb.connect_closing() as observer:
            parent_before = kb.get_task(observer, task_id)
            child_before = kb.get_task(observer, child_id)
            ledger_before = observer.execute(
                "SELECT COUNT(*) AS n FROM completion_receipts WHERE digest = ?",
                (receipt.receipt_digest,),
            ).fetchone()["n"]
        assert parent_before is not None and parent_before.status == "running"
        assert child_before is not None and child_before.status == "todo"
        assert ledger_before == 0
    finally:
        release_writer.set()
    thread.join(timeout=5)
    assert thread.is_alive() is False
    assert writer_errors == []

    with kb.connect_closing() as observer:
        parent_after = kb.get_task(observer, task_id)
        child_after = kb.get_task(observer, child_id)
        ledger_after = observer.execute(
            "SELECT COUNT(*) AS n FROM completion_receipts WHERE digest = ?",
            (receipt.receipt_digest,),
        ).fetchone()["n"]
    assert parent_after is not None and parent_after.status == "done"
    assert child_after is not None and child_after.status == "ready"
    assert ledger_after == 1
```

- [ ] **Step 4: RED를 실행한다**

```powershell
& $HermesPython -m pytest tests/hermes_cli/test_kanban_completion_policy.py -q
```

Expected: 첫 test에서 `verify_completion`이 호출되지 않아 task가 `done`으로 바뀌며 snapshot equality가 FAIL한다. `_recompute_ready_in_txn`은 아직 없어 single-transaction test가 `AttributeError`로 FAIL한다.

- [ ] **Step 5: ready 승격 SQL을 transaction-free 내부 함수와 public wrapper로 정확히 분리한다**

기존 `recompute_ready()`를 다음 두 함수로 교체한다. 내부 함수에는 `write_txn`을 넣지 않는다.

```python
def _recompute_ready_in_txn(
    conn: sqlite3.Connection,
    failure_limit: int | None = None,
) -> int:
    if not conn.in_transaction:
        raise RuntimeError("_recompute_ready_in_txn requires an active transaction")
    if failure_limit is None:
        failure_limit = DEFAULT_FAILURE_LIMIT
    promoted = 0
    todo_rows = conn.execute(
        "SELECT id, status, consecutive_failures, max_retries "
        "FROM tasks WHERE status IN ('todo', 'blocked')"
    ).fetchall()
    for row in todo_rows:
        task_id = row["id"]
        cur_status = row["status"]
        if cur_status == "blocked" and _has_sticky_block(conn, task_id):
            continue
        parents = conn.execute(
            "SELECT t.status FROM tasks t "
            "JOIN task_links l ON l.parent_id = t.id "
            "WHERE l.child_id = ?",
            (task_id,),
        ).fetchall()
        if not all(parent["status"] in ("done", "archived") for parent in parents):
            continue
        if cur_status == "blocked":
            failures = int(row["consecutive_failures"] or 0)
            task_limit = row["max_retries"]
            effective_limit = (
                int(task_limit) if task_limit is not None else int(failure_limit)
            )
            if failures >= effective_limit:
                continue
            conn.execute(
                "UPDATE tasks SET status = 'ready' "
                "WHERE id = ? AND status = 'blocked'",
                (task_id,),
            )
        else:
            conn.execute(
                "UPDATE tasks SET status = 'ready' WHERE id = ? AND status = 'todo'",
                (task_id,),
            )
        _append_event(conn, task_id, "promoted", None)
        promoted += 1
    return promoted


def recompute_ready(
    conn: sqlite3.Connection,
    failure_limit: int | None = None,
) -> int:
    with write_txn(conn):
        return _recompute_ready_in_txn(conn, failure_limit=failure_limit)
```

- [ ] **Step 6: protected preflight를 기존 hallucinated-card audit보다 앞에 둔다**

`complete_task()`의 `now = int(time.time())` 직후, 기존 `created_cards` block 직전에 다음 exact block을 넣는다.

```python
task_before = get_task(conn, task_id)
if task_before is None or task_before.status not in {"running", "ready", "blocked"}:
    return False
if (
    expected_run_id is not None
    and task_before.current_run_id != int(expected_run_id)
):
    return False

completion_receipt: completion_policy_gate.CompletionReceipt | None = None
if task_before.completion_policy is not None:
    if task_before.current_run_id is None:
        raise completion_policy_gate.CompletionPolicyError(
            "TESTS_FAILED",
            "protected task requires an active current_run_id",
        )
    completion_receipt = completion_policy_gate.verify_completion(
        policy=task_before.completion_policy,
        task_id=task_id,
        run_id=int(task_before.current_run_id),
        board=get_current_board(),
        workspace_path=task_before.workspace_path,
    )
    if completion_receipt.phase != "hermes":
        raise completion_policy_gate.CompletionPolicyError(
            "GATE_ERROR",
            "completion receipt phase must be 'hermes'",
        )
    if completion_receipt.policy != task_before.completion_policy:
        raise completion_policy_gate.CompletionPolicyError(
            "GATE_ERROR",
            "completion receipt policy mismatch",
        )
    if completion_receipt.task_id != task_id:
        raise completion_policy_gate.CompletionPolicyError(
            "GATE_ERROR",
            "completion receipt task_id mismatch",
        )
    if completion_receipt.run_id != task_before.current_run_id:
        raise completion_policy_gate.CompletionPolicyError(
            "GATE_ERROR",
            "completion receipt run_id mismatch",
        )
    receipt_now = int(time.time())
    if (
        completion_receipt.issued_at > receipt_now
        or completion_receipt.expires_at <= receipt_now
        or completion_receipt.expires_at <= completion_receipt.issued_at
        or completion_receipt.expires_at != completion_receipt.issued_at + 900
    ):
        raise completion_policy_gate.CompletionPolicyError(
            "GATE_ERROR",
            "completion receipt is expired or not yet valid",
        )
```

- [ ] **Step 7: 기존 completion write block을 다음 단일-transaction helper로 이동한다**

```python
def _complete_task_in_txn(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    result: str | None,
    summary: str | None,
    metadata: dict | None,
    verified_cards: list[str],
    expected_run_id: int | None,
    completion_receipt: completion_policy_gate.CompletionReceipt | None,
    now: int,
) -> tuple[bool, int | None]:
    if not conn.in_transaction:
        raise RuntimeError("_complete_task_in_txn requires an active transaction")
    guarded_run_id = (
        completion_receipt.run_id
        if completion_receipt is not None
        else expected_run_id
    )
    receipt_digest = (
        completion_receipt.receipt_digest
        if completion_receipt is not None
        else None
    )
    if guarded_run_id is None:
        cur = conn.execute(
            """
            UPDATE tasks
               SET status = 'done', result = ?, completed_at = ?,
                   claim_lock = NULL, claim_expires = NULL, worker_pid = NULL,
                   block_kind = NULL, block_recurrences = 0,
                   consecutive_failures = 0, last_failure_error = NULL,
                   completion_receipt_digest = ?
             WHERE id = ? AND status IN ('running', 'ready', 'blocked')
            """,
            (result, now, receipt_digest, task_id),
        )
    else:
        cur = conn.execute(
            """
            UPDATE tasks
               SET status = 'done', result = ?, completed_at = ?,
                   claim_lock = NULL, claim_expires = NULL, worker_pid = NULL,
                   block_kind = NULL, block_recurrences = 0,
                   consecutive_failures = 0, last_failure_error = NULL,
                   completion_receipt_digest = ?
             WHERE id = ? AND status IN ('running', 'ready', 'blocked')
               AND current_run_id = ?
            """,
            (result, now, receipt_digest, task_id, int(guarded_run_id)),
        )
    if cur.rowcount != 1:
        return False, None

    run_id = _end_run(
        conn,
        task_id,
        outcome="completed",
        status="done",
        summary=summary if summary is not None else result,
        metadata=metadata,
    )
    if run_id is None and (summary or metadata or result):
        run_id = _synthesize_ended_run(
            conn,
            task_id,
            outcome="completed",
            summary=summary if summary is not None else result,
            metadata=metadata,
        )

    if completion_receipt is not None:
        if run_id != completion_receipt.run_id:
            raise completion_policy_gate.CompletionPolicyError(
                "GATE_ERROR",
                "ended run does not match completion receipt run_id",
            )
        receipt_payload = completion_receipt.event_payload()
        try:
            conn.execute(
                """
                INSERT INTO completion_receipts (
                    digest, task_id, run_id, policy, payload, consumed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    completion_receipt.receipt_digest,
                    task_id,
                    completion_receipt.run_id,
                    completion_receipt.policy,
                    json.dumps(receipt_payload, sort_keys=True, separators=(",", ":")),
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise completion_policy_gate.CompletionPolicyError(
                "TESTS_FAILED",
                "completion receipt was already consumed",
            ) from exc
        _append_event(
            conn,
            task_id,
            "completion_receipt_consumed",
            receipt_payload,
            run_id=run_id,
        )

    event_summary = (summary if summary is not None else result) or ""
    event_summary = (
        event_summary.strip().splitlines()[0][:400]
        if event_summary
        else ""
    )
    completed_payload: dict[str, object] = {
        "result_len": len(result) if result else 0,
        "summary": event_summary or None,
    }
    if verified_cards:
        completed_payload["verified_cards"] = verified_cards
    if isinstance(metadata, dict):
        artifacts = metadata.get("artifacts")
        if isinstance(artifacts, (list, tuple)):
            cleaned_artifacts = [
                str(path).strip()
                for path in artifacts
                if isinstance(path, str) and str(path).strip()
            ]
            if cleaned_artifacts:
                completed_payload["artifacts"] = cleaned_artifacts
    _append_event(
        conn,
        task_id,
        "completed",
        completed_payload,
        run_id=run_id,
    )
    _recompute_ready_in_txn(conn)
    return True, run_id
```

`complete_task()`의 기존 `with write_txn(conn)` block을 다음 call로 교체한다.

```python
with write_txn(conn):
    completed, run_id = _complete_task_in_txn(
        conn,
        task_id,
        result=result,
        summary=summary,
        metadata=metadata,
        verified_cards=verified_cards,
        expected_run_id=expected_run_id,
        completion_receipt=completion_receipt,
        now=now,
    )
if not completed:
    return False
```

기존 transaction 뒤의 `_clear_failure_counter(conn, task_id)`와 `recompute_ready(conn)` 두 calls는 삭제한다. `_cleanup_workspace`와 lifecycle hook은 commit 뒤에 유지한다.

- [ ] **Step 8: GREEN과 DB/concurrency 회귀를 실행한다**

```powershell
& $HermesPython -m pytest tests/hermes_cli/test_kanban_completion_policy.py tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_kanban_core_functionality.py tests/stress/test_atypical_scenarios.py -q
```

Expected: 모든 tests가 PASS하며 `cannot start a transaction within a transaction`이 0건이다.

- [ ] **Step 9: commit한다**

```powershell
git add hermes_cli/kanban_db.py tests/hermes_cli/test_kanban_completion_policy.py
git commit -m "feat: consume completion receipts atomically"
```

---

### Task 4: protocol violation을 sticky block으로 만들고 완료 proof 편집 우회를 막는다

**Files:**
- Modify: `C:\01.project\.worktrees\INFINITY_FORGE\hermes-completion-policy\hermes_cli\kanban_db.py:4474-4538,6346-6660`
- Modify: `C:\01.project\.worktrees\INFINITY_FORGE\hermes-completion-policy\hermes_cli\kanban.py:1866-1908`
- Modify: `C:\01.project\.worktrees\INFINITY_FORGE\hermes-completion-policy\plugins\kanban\dashboard\plugin_api.py:821-937,1161-1255`
- Modify: `C:\01.project\.worktrees\INFINITY_FORGE\hermes-completion-policy\tests\hermes_cli\test_kanban_completion_policy.py`

**Interfaces:**
- Consumes: Task 3의 atomic `complete_task()`, `CompletionPolicyError`; 기존 `_has_sticky_block()`과 `unblock_task()`
- Produces: `_record_task_failure(conn: sqlite3.Connection, task_id: str, error: str, *, outcome: str, failure_limit: int | None = None, release_claim: bool = False, end_run: bool = False, event_payload_extra: dict | None = None, sticky_block: bool = False) -> bool`, first-hit sticky `protocol_violation`, protected edit rejection, CLI exit 2, Dashboard HTTP 409, tool error propagation

- [ ] **Step 1: 다음 실제 RED tests와 imports를 test 파일에 추가한다**

```python
from fastapi import HTTPException

from plugins.kanban.dashboard import plugin_api
from tools import kanban_tools


def test_protocol_violation_is_sticky_on_first_failure_with_max_retries_four(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="protocol",
            assignee="executor",
            completion_policy="forge-v1",
            max_retries=4,
        )
        claimed = kb.claim_task(conn, task_id, claimer=kb._claimer_id())
        assert claimed is not None
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET worker_pid = ?, started_at = ? WHERE id = ?",
                (424242, int(time.time()) - 10, task_id),
            )
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
        monkeypatch.setattr(kb, "_classify_worker_exit", lambda pid: ("clean_exit", 0))
        monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)

        assert kb.detect_crashed_workers(conn) == [task_id]
        blocked = kb.get_task(conn, task_id)
        kinds = [event.kind for event in kb.list_events(conn, task_id)]
        assert blocked is not None
        assert blocked.status == "blocked"
        assert blocked.consecutive_failures == 1
        assert blocked.block_kind == "needs_input"
        assert {"protocol_violation", "gave_up", "blocked"} <= set(kinds)
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, task_id).status == "blocked"
        assert kb.unblock_task(conn, task_id)
        assert kb.get_task(conn, task_id).status == "ready"


def test_protected_completed_result_cannot_be_edited(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with kb.connect_closing() as conn:
        task_id, run_id, receipt = _claim_protected(
            conn, title="immutable-proof", digest_seed="5"
        )
        child_id = kb.create_task(
            conn,
            title="immutable-child",
            assignee="reviewer",
            parents=[task_id],
        )
        monkeypatch.setattr(cp, "verify_completion", lambda **kwargs: receipt)
        assert kb.complete_task(
            conn,
            task_id,
            result="original",
            summary="original summary",
            metadata={"changed_files": ["src/a.py"]},
            expected_run_id=run_id,
        )
        before = _completion_snapshot(conn, task_id, child_id)
        with pytest.raises(cp.CompletionPolicyError, match="new run and receipt"):
            kb.edit_completed_task_result(
                conn,
                task_id,
                result="rewritten",
                summary="rewritten summary",
                metadata={"changed_files": []},
            )
        after = _completion_snapshot(conn, task_id, child_id)
    assert after == before


def test_unprotected_completed_result_edit_remains_backward_compatible(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="legacy", assignee="executor")
        assert kb.complete_task(conn, task_id, result="before")
        assert kb.edit_completed_task_result(
            conn,
            task_id,
            result="after",
            summary="after summary",
            metadata={"legacy": True},
        )
        task = kb.get_task(conn, task_id)
    assert task is not None and task.result == "after"


def test_cli_completion_policy_error_is_stable_exit_two(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="cli rejection", assignee="executor")

    def reject(*args: object, **kwargs: object) -> bool:
        raise cp.CompletionPolicyError("GATE_ERROR", "trusted verifier unavailable")

    monkeypatch.setattr(kb, "complete_task", reject)
    args = argparse.Namespace(
        task_ids=[task_id],
        result=None,
        summary="premature",
        metadata=None,
    )
    assert kanban._cmd_complete(args) == 2
    assert "GATE_ERROR: trusted verifier unavailable" in capsys.readouterr().err


def test_dashboard_completion_policy_error_is_http_409(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="dashboard rejection", assignee="executor")

    def reject(*args: object, **kwargs: object) -> bool:
        raise cp.CompletionPolicyError("TESTS_FAILED", "handoff is incomplete")

    monkeypatch.setattr(plugin_api, "_resolve_board", lambda board: "default")
    monkeypatch.setattr(plugin_api, "_conn", lambda board=None: kb.connect())
    monkeypatch.setattr(kb, "complete_task", reject)
    with pytest.raises(HTTPException) as exc_info:
        plugin_api.update_task(
            task_id,
            plugin_api.UpdateTaskBody(status="done", summary="premature"),
            board=None,
        )
    assert exc_info.value.status_code == 409
    assert "TESTS_FAILED: handoff is incomplete" in str(exc_info.value.detail)


def test_worker_tool_surfaces_completion_policy_classification(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="tool rejection", assignee="executor")
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setattr(kanban_tools, "_connect", lambda board=None: (kb, kb.connect()))

    def reject(*args: object, **kwargs: object) -> bool:
        raise cp.CompletionPolicyError("TESTS_FAILED", "tests are red")

    monkeypatch.setattr(kb, "complete_task", reject)
    result = kanban_tools._handle_complete(
        {"task_id": task_id, "summary": "premature"}
    )
    assert "TESTS_FAILED: tests are red" in result
```

- [ ] **Step 2: RED를 실행한다**

```powershell
& $HermesPython -m pytest tests/hermes_cli/test_kanban_completion_policy.py -q
```

Expected: protocol task가 `ready`이거나 `block_kind is None`이어서 FAIL하고, protected edit가 `True`를 반환해 FAIL하며, CLI와 Dashboard tests는 uncaught `CompletionPolicyError`로 FAIL한다.

- [ ] **Step 3: `_record_task_failure()`에 명시적 sticky override를 최소 구현한다**

signature 끝에 다음 keyword를 추가한다.

```python
sticky_block: bool = False,
```

effective limit 결정 block을 다음으로 교체한다.

```python
if sticky_block:
    effective_limit = 1
    limit_source = "sticky_protocol"
elif task_override is not None:
    effective_limit = int(task_override)
    limit_source = "task"
else:
    effective_limit = int(failure_limit)
    limit_source = "dispatcher"
```

breaker의 두 task UPDATE statements가 sticky 상태를 함께 기록하도록 `block_kind` assignment를 추가한다. 두 statements 모두 같은 값 계산을 사용한다.

```python
sticky_kind = "needs_input" if sticky_block else None
```

```sql
UPDATE tasks
   SET status = 'blocked', claim_lock = NULL, claim_expires = NULL,
       worker_pid = NULL, consecutive_failures = ?, last_failure_error = ?,
       block_kind = COALESCE(?, block_kind)
 WHERE id = ? AND status IN ('running', 'ready')
```

```sql
UPDATE tasks
   SET status = 'blocked', consecutive_failures = ?, last_failure_error = ?,
       block_kind = COALESCE(?, block_kind)
 WHERE id = ? AND status IN ('ready', 'running')
```

`gave_up` event 직후 같은 transaction에서 sticky event를 추가한다.

```python
if sticky_block:
    _append_event(
        conn,
        task_id,
        "blocked",
        {
            "reason": error[:500],
            "kind": "needs_input",
            "source": "protocol_violation",
        },
        run_id=run_id,
    )
```

`detect_crashed_workers()`의 `_record_task_failure()` call에 다음 keyword를 추가한다.

```python
sticky_block=protocol_violation,
```

- [ ] **Step 4: protected result edit를 core 경계에서 거절한다**

`edit_completed_task_result()`의 첫 query와 guard를 다음 exact block으로 바꾼다.

```python
row = conn.execute(
    "SELECT status, completion_policy FROM tasks WHERE id = ?",
    (task_id,),
).fetchone()
if not row or row["status"] != "done":
    return False
if row["completion_policy"] is not None:
    raise completion_policy_gate.CompletionPolicyError(
        "TESTS_FAILED",
        "protected completion proof is immutable; start a new run and receipt",
    )
```

- [ ] **Step 5: CLI와 Dashboard 오류 변환을 최소 구현한다**

`hermes_cli/kanban.py`와 `plugins/kanban/dashboard/plugin_api.py`의 import 구역에 같은 module alias를 추가한다.

```python
from hermes_cli import kanban_completion_policy as completion_policy_gate
```

`_cmd_complete()` loop에 policy 오류 추적을 추가한다.

```python
policy_rejected = False
with kb.connect_closing() as conn:
    for task_id in ids:
        try:
            ok = kb.complete_task(
                conn,
                task_id,
                result=args.result,
                summary=summary,
                metadata=metadata,
                expected_run_id=_worker_run_id_for(task_id),
            )
        except completion_policy_gate.CompletionPolicyError as exc:
            policy_rejected = True
            failed.append(task_id)
            print(str(exc), file=sys.stderr)
            continue
        if not ok:
            failed.append(task_id)
            print(
                f"cannot complete {task_id} (unknown id or terminal state)",
                file=sys.stderr,
            )
        else:
            print(f"Completed {task_id}")
return 2 if policy_rejected else (0 if not failed else 1)
```

`plugin_api.update_task()`의 `s == "done"` branch를 다음으로 감싼다.

```python
if s == "done":
    try:
        ok = kanban_db.complete_task(
            conn,
            task_id,
            result=payload.result,
            summary=payload.summary,
            metadata=payload.metadata,
        )
    except completion_policy_gate.CompletionPolicyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
```

bulk path는 기존 per-item `except Exception`이 classification 문자열을 결과에 보존하는지 test로 고정하고 별도 global exception을 추가하지 않는다. `tools/kanban_tools.py`는 기존 `except ValueError`가 `CompletionPolicyError`를 이미 `tool_error`로 바꾸므로 production 변경을 하지 않는다.

- [ ] **Step 6: GREEN과 관련 upstream 회귀를 실행한다**

```powershell
& $HermesPython -m pytest tests/hermes_cli/test_kanban_completion_policy.py tests/hermes_cli/test_kanban_blocked_sticky.py tests/hermes_cli/test_kanban_db.py tests/tools/test_kanban_tools.py tests/plugins/test_kanban_dashboard_plugin.py -q
```

Expected: 모든 tests가 PASS한다.

- [ ] **Step 7: commit한다**

```powershell
git add hermes_cli/kanban_db.py hermes_cli/kanban.py plugins/kanban/dashboard/plugin_api.py tests/hermes_cli/test_kanban_completion_policy.py
git commit -m "fix: make protected Hermes completion fail closed"
```

- [ ] **Step 8: fixed base와 target diff를 확인한다**

```powershell
git rev-parse 4281151ae859241351ba14d8c7682dc67ff4c126
git diff --name-only 4281151ae859241351ba14d8c7682dc67ff4c126..HEAD
```

Expected target set은 정확히 다음 5개다.

```text
hermes_cli/kanban_completion_policy.py
hermes_cli/kanban_db.py
hermes_cli/kanban.py
plugins/kanban/dashboard/plugin_api.py
tests/hermes_cli/test_kanban_completion_policy.py
```

- [ ] **Step 9: targeted tests와 full related suites를 fresh 실행한다**

```powershell
& $HermesPython -m pytest tests/hermes_cli/test_kanban_completion_policy.py -q
& $HermesPython -m pytest tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_kanban_core_functionality.py tests/hermes_cli/test_kanban_blocked_sticky.py tests/tools/test_kanban_tools.py tests/plugins/test_kanban_dashboard_plugin.py tests/stress/test_atypical_scenarios.py -q
& $HermesPython -m compileall hermes_cli plugins/kanban/dashboard
git diff --check 4281151ae859241351ba14d8c7682dc67ff4c126..HEAD
```

Expected: 모든 명령 exit 0이다.

- [ ] **Step 10: seven-member preimage 조사 명령을 fresh 실행해 manifest 기준과 일치시킨다**

```powershell
git show 4281151ae859241351ba14d8c7682dc67ff4c126:hermes_cli/kanban_db.py | & $HermesPython -c "import ast,sys,hashlib; s=sys.stdin.read(); t=ast.parse(s); names={'Task','create_task','recompute_ready','complete_task','edit_completed_task_result','detect_crashed_workers','_migrate_add_optional_columns'}; print(*[f'{n.name}:{hashlib.sha256(ast.get_source_segment(s,n).encode()).hexdigest()}' for n in t.body if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name in names],sep='\n')"
```

Expected: Global Constraints 4의 7개 full SHA-256과 byte-for-byte 일치한다.

- [ ] **Step 11: 이전 steps에서 defect 수정 commit이 생겼을 때만 별도 commit한다**

```powershell
git status --short
git add hermes_cli/kanban_completion_policy.py hermes_cli/kanban_db.py hermes_cli/kanban.py plugins/kanban/dashboard/plugin_api.py tests/hermes_cli/test_kanban_completion_policy.py
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) { git commit -m "test: close Hermes completion policy regressions" }
```

Expected: defect가 없으면 commit이 생성되지 않고, defect가 있으면 target 5개 안의 변경만 commit된다.

---

### Task 5: pinned patch artifact와 target-only install/rollback을 구현한다

**Files:**
- Create: `forge/patches/hermes/0.18.2/completion-policy.patch`
- Create: `forge/patches/hermes/0.18.2/manifest.json`
- Create: `forge/ops/hermes_patch.py`
- Create: `forge/scripts/hermes-patch.py`
- Create: `tests/hermes/test_hermes_patch.py`
- Create: `docs/weapon/evidence/hermes-completion-policy-patch-rehearsal.md`

**Interfaces:**
- Consumes: Task 4의 patch worktree HEAD, upstream `4281151ae859241351ba14d8c7682dc67ff4c126`, Windows/VPS variant root, variant별 exact 7개 AST preimages와 target preimage/postimage SHA map
- Produces: `build_artifact(*, source_root: Path, variant_roots: dict[str, Path], patch_path: Path, manifest_path: Path) -> dict[str, object]`, `patch_status(*, root: Path, manifest_path: Path, record_path: Path) -> str`, `recover_patch(*, root: Path, manifest_path: Path, record_path: Path) -> str`, `check_patch(*, root: Path, patch_path: Path, manifest_path: Path) -> PatchCheck`, `install_patch(*, root: Path, patch_path: Path, manifest_path: Path, record_path: Path, run_tests: bool = True, test_python: Path | None = None) -> InstallRecord`, `verify_patch(*, root: Path, manifest_path: Path, record_path: Path, current_manifest_path: Path, expected_source_sha: str) -> InstallRecord`, `rollback_patch(*, root: Path, patch_path: Path, manifest_path: Path, record_path: Path, run_tests: bool = True, test_python: Path | None = None) -> str`; CLI `build|status|recover|check|install|verify|rollback`

Patch manifest top-level required/유일 key는 `schema_version`, `hermes_version`, `upstream_base`, `target_files`, `patch_sha256`, `variants`다. `variants` required/유일 key는 Windows blob `518e74eb0647786a0361105b76bfbaeb1bad3e19`와 VPS blob `6150b141537b947a2a89d19b13be4fbad2330711`이다. 각 variant required/유일 key는 `ast_preimages`, `target_preimage_sha256`, `target_postimage_sha256`이고, 두 target hash map의 key set은 top-level `target_files`와 exact-equal이다. 새 target의 preimage는 JSON `null`, 기존 target의 preimage와 모든 postimage는 64자리 lowercase SHA-256이다.

`verify`는 첫 파일·Git 상태 검사 전에 `guard/current.json`의 exact nested schema, `expected_source_sha` 형식, artifact release directory, interpreter/artifact absolute-file 존재, artifact digest를 검증한다. 이 guard 검증이 모두 통과한 뒤에만 install record, 선택 variant의 postimage map, current HEAD, target clean 상태와 target별 실제 postimage를 검사한다. 따라서 손상되거나 다른 source SHA의 guard를 정상 Hermes patch 위에 결합해도 성공으로 보고하지 않는다.

`status`는 existing host를 `supported_base|installed_same|installed_other|rolled_back|recovery_required`로 read-only 분류하고 HEAD/index/target/record/journal을 절대 변경하지 않는다. incomplete journal은 `recovery_required`만 반환하며 Apply 경로의 명시적 `recover` command가 검증된 journal을 수렴시킨다. unpatched 상태는 exact HEAD commit, ancestry, moving remote ref가 아니라 bootstrap이 create-only로 만든 immutable `refs/infinity-forge/approved-base` object identity, supported `HEAD:kanban_db.py` blob, 모든 target preimage, clean target/stage를 검증한다. 따라서 approved base와 ancestry가 없는 실제 Windows `540f90190f50f9518bf36632a724e0e58877a10b` 및 VPS `73b611ad19720d70308dad6b0fb64648aaadc216` carried roots도 exact variant allowlist로 수용하고 advanced `origin/main`도 허용한다. `status|check|install`은 approved-base ref가 없거나 manifest `upstream_base`와 다르면 patch 적용 전에 실패한다. installed 상태는 candidate manifest와 같든 다르든 install record exact key set, direct parent, target-only patch commit, current postimage, backup path/digest부터 검증하며, `rolled_back`은 digest-bound archive/pointer, exact previous HEAD와 target preimages를 모두 검증한 경우에만 반환한다.

Ops `phase=hermes` bootstrap은 checkout 직후 다음 create-only contract를 소유한다. 이미 존재하는 ref는 같은 SHA일 때만 재사용하고 다른 SHA로는 절대 rewrite하지 않는다.

```bash
approved_base_ref=refs/infinity-forge/approved-base
zero_oid=0000000000000000000000000000000000000000
approved_base="$(git -C "$hermes_root" rev-parse --verify "$approved_base_ref" 2>/dev/null || true)"
if [[ -z "$approved_base" ]]; then
  git -C "$hermes_root" update-ref "$approved_base_ref" "$hermes_base_commit" "$zero_oid"
elif [[ "$approved_base" != "$hermes_base_commit" ]]; then
  echo "immutable Hermes approved-base ref mismatch" >&2
  exit 2
fi
```

`install`은 entry마다 backup file과 parent directory를 fsync한 뒤 `forge-hermes-patch-transaction/v1`의 `prepared` journal을 active record 옆에 먼저 durable 기록한다. journal은 previous HEAD, target maps, backup locator, candidate hashes, previous active-record archive digest를 결합한다. patch commit 뒤 `committed`, active record와 그 digest를 내구화한 뒤 `installed`로 전이하며, `installed` journal까지 durable한 뒤에만 journal을 제거한다. `committed` 이전 중단은 exact previous state로 abort하고 `installed` 이후 journal-unlink 중단은 verified active record로 roll-forward한다. `install|rollback` 진입과 명시적 `recover` command는 journal recovery를 실행해 commit 직후 process crash, record write/parent-fsync 실패, upgrade 중단을 exact previous HEAD/bytes/record로 수렴시키지만, `status`는 journal 존재를 `recovery_required`로만 보고하고 어떤 복구 mutation도 수행하지 않는다. same manifest/patch record면 mutation·새 backup 없이 verify/reuse한다. changed manifest면 verified old backup으로 exact `previous_head`를 복원하고 새 patch를 설치하며, 새 check/test/commit/status/record fsync 중 하나라도 실패하면 old patch HEAD/postimage/record/unrelated dirty state를 모두 복원한다. active record와 journal의 atomic writer는 POSIX에서 replace 후 directory fsync 실패 시 previous bytes/absence를 복원하고 다시 fsync하며, Windows에서는 `MoveFileExW`에 항상 `MOVEFILE_WRITE_THROUGH`, destination이 존재할 때만 `MOVEFILE_REPLACE_EXISTING`을 사용해 rename metadata까지 write-through한 뒤 같은 compensation을 적용한다. `rollback`은 reverse patch에 의존하지 않고 모든 backup bytes/digest와 patch-commit direct-parent/target allowlist를 mutation 전에 검증한 뒤 exact `previous_head`로 이동한다. 성공하면 active record를 digest-bound rolled-back tombstone으로 내구화한 뒤 active path를 제거하므로 rollback 재시도와 same-SHA forward install이 멱등이다. rollback test/검증/record archive 실패 시 installed patch HEAD/postimage/active record/staging/status를 되돌린다.

- [ ] **Step 1: 다음 실제 RED test 파일을 작성한다**

```python
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from forge.ops import hermes_patch


UPSTREAM = "4281151ae859241351ba14d8c7682dc67ff4c126"
WINDOWS_BLOB = "518e74eb0647786a0361105b76bfbaeb1bad3e19"
VPS_BLOB = "6150b141537b947a2a89d19b13be4fbad2330711"
AST_PREIMAGES = {
    "Task": "37dbff1faa5f92afa3b63e3d80a1c041e36a0a5fcebd2dc9585bb8c824656137",
    "_migrate_add_optional_columns": "e8d018507072b7aa7a9d875bde98b389446bb9fb5c61efdfd4e0b1a09fd82583",
    "create_task": "d95d2c6f0bd66eb3419ce2ee3ad49faa4f211b28624e3cd36e1efbbd8bd265aa",
    "recompute_ready": "d6e8a2840b92a4c38a9d41e358f49c35c90d386f14834d91a1abe4ff682249e8",
    "complete_task": "a10e062b91aeef9e8c097997c39840b3bf1b0d0552764681613038505b286bf2",
    "edit_completed_task_result": "bcf22376052004ea28747d65a95260edcc30781b7e53f7b8ebfa8de72e82e2e2",
    "detect_crashed_workers": "d7dca0d5a3943b21108e1fb36fca5bb98e13b68b95001b72bf79b5024df9235a",
}


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    return completed.stdout.strip()


def _write_guard_current(tmp_path: Path) -> tuple[Path, str]:
    expected_source_sha = "1" * 40
    artifact = tmp_path / "guard" / "releases" / expected_source_sha / "forge-guard.pyz"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"trusted-guard")
    current_manifest = tmp_path / "guard" / "current.json"
    current_manifest.write_text(
        json.dumps(
            {
                "schema_version": "forge-completion-manifest/v1",
                "policies": {
                    "forge-v1": {
                        "python": str(Path(sys.executable).resolve()),
                        "artifact": str(artifact.resolve()),
                        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                        "timeout_seconds": 900,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return current_manifest, expected_source_sha


def _synthetic_patch(tmp_path: Path) -> tuple[Path, Path, Path, Path, str]:
    root = tmp_path / "repo"
    target = root / "hermes_cli" / "kanban_db.py"
    target.parent.mkdir(parents=True)
    unrelated = root / "unrelated.txt"
    base_source = "def complete_task():\n    return 1\n"
    post_source = "def complete_task():\n    return 2\n"
    target.write_bytes(base_source.encode("utf-8"))
    unrelated.write_bytes(b"base\n")
    _git(root, "init", "-b", "main")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "config", "user.email", "forge-test@example.com")
    _git(root, "config", "user.name", "Forge Test")
    _git(root, "add", "hermes_cli/kanban_db.py", "unrelated.txt")
    _git(root, "commit", "-m", "base")
    base_head = _git(root, "rev-parse", "HEAD")
    _git(root, "update-ref", "refs/remotes/origin/main", base_head)
    _git(root, "update-ref", "refs/infinity-forge/approved-base", base_head)
    base_blob = _git(root, "rev-parse", "HEAD:hermes_cli/kanban_db.py")
    preimage = hermes_patch.source_member_sha256(target, "complete_task")

    target.write_bytes(post_source.encode("utf-8"))
    patch_text = _git(root, "diff", "--binary", "HEAD", "--", "hermes_cli/kanban_db.py")
    patch_path = tmp_path / "completion-policy.patch"
    patch_path.write_bytes((patch_text + "\n").encode("utf-8"))
    post_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    target.write_bytes(base_source.encode("utf-8"))

    manifest = {
        "schema_version": "forge-hermes-patch/v1",
        "hermes_version": "0.18.2",
        "upstream_base": base_head,
        "target_files": ["hermes_cli/kanban_db.py"],
        "patch_sha256": hashlib.sha256(patch_path.read_bytes()).hexdigest(),
        "variants": {
            base_blob: {
                "ast_preimages": {"complete_task": preimage},
                "target_preimage_sha256": {
                    "hermes_cli/kanban_db.py": hashlib.sha256(base_source.encode()).hexdigest()
                },
                "target_postimage_sha256": {"hermes_cli/kanban_db.py": post_sha},
            }
        },
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    record_path = tmp_path / "install-record.json"
    unrelated.write_bytes(b"base\nuser-owned dirty line\n")
    return root, patch_path, manifest_path, record_path, base_source


def test_release_manifest_pins_full_upstream_blobs_and_ast_hashes() -> None:
    root = Path(__file__).resolve().parents[2]
    patch_path = root / "forge" / "patches" / "hermes" / "0.18.2" / "completion-policy.patch"
    manifest_path = root / "forge" / "patches" / "hermes" / "0.18.2" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["upstream_base"] == UPSTREAM
    assert set(manifest["variants"]) == {WINDOWS_BLOB, VPS_BLOB}
    for base_blob in (WINDOWS_BLOB, VPS_BLOB):
        variant = manifest["variants"][base_blob]
        assert set(variant) == {
            "ast_preimages",
            "target_preimage_sha256",
            "target_postimage_sha256",
        }
        assert variant["ast_preimages"] == AST_PREIMAGES
        assert set(variant["target_preimage_sha256"]) == set(manifest["target_files"])
        assert set(variant["target_postimage_sha256"]) == set(manifest["target_files"])
    assert (
        manifest["variants"][WINDOWS_BLOB]["target_postimage_sha256"][
            "hermes_cli/kanban_db.py"
        ]
        != manifest["variants"][VPS_BLOB]["target_postimage_sha256"][
            "hermes_cli/kanban_db.py"
        ]
    )
    assert manifest["patch_sha256"] == hashlib.sha256(patch_path.read_bytes()).hexdigest()
    assert set(manifest["target_files"]) == {
        "hermes_cli/kanban_completion_policy.py",
        "hermes_cli/kanban_db.py",
        "hermes_cli/kanban.py",
        "plugins/kanban/dashboard/plugin_api.py",
        "tests/hermes_cli/test_kanban_completion_policy.py",
    }


def _two_variant_fixture(
    tmp_path: Path,
) -> tuple[dict[str, Path], Path, Path, dict[str, Path], dict[str, bytes]]:
    seed = tmp_path / "seed"
    seed_target = seed / "hermes_cli" / "kanban_db.py"
    seed_target.parent.mkdir(parents=True)
    base_source = (
        "def complete_task():\n"
        "    return 1\n"
        "\n"
        "STABLE_CONTEXT_1 = 1\n"
        "STABLE_CONTEXT_2 = 2\n"
        "STABLE_CONTEXT_3 = 3\n"
        "STABLE_CONTEXT_4 = 4\n"
    )
    patched_source = base_source.replace("return 1", "return 2", 1)
    seed_target.write_bytes(base_source.encode("utf-8"))
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "core.autocrlf", "false")
    _git(seed, "config", "user.email", "forge-test@example.com")
    _git(seed, "config", "user.name", "Forge Test")
    _git(seed, "add", "hermes_cli/kanban_db.py")
    _git(seed, "commit", "-m", "upstream")
    upstream = _git(seed, "rev-parse", "HEAD")

    seed_target.write_bytes(patched_source.encode("utf-8"))
    patch_text = _git(seed, "diff", "--binary", "HEAD", "--", "hermes_cli/kanban_db.py")
    patch_path = tmp_path / "two-variant.patch"
    patch_path.write_bytes((patch_text + "\n").encode("utf-8"))
    seed_target.write_bytes(base_source.encode("utf-8"))

    roots: dict[str, Path] = {}
    records: dict[str, Path] = {}
    before_bytes: dict[str, bytes] = {}
    variants: dict[str, dict[str, object]] = {}
    for name, marker in (("windows", ""), ("vps", "\nVPS_UNRELATED = 'preserved'\n")):
        root = tmp_path / name
        subprocess.run(
            ["git", "-c", "core.autocrlf=false", "clone", str(seed), str(root)],
            check=True,
            capture_output=True,
        )
        _git(root, "config", "core.autocrlf", "false")
        _git(root, "config", "user.email", "forge-test@example.com")
        _git(root, "config", "user.name", "Forge Test")
        _git(root, "update-ref", "refs/remotes/origin/main", upstream)
        _git(root, "update-ref", "refs/infinity-forge/approved-base", upstream)
        target = root / "hermes_cli" / "kanban_db.py"
        if marker:
            target.write_bytes((base_source + marker).encode("utf-8"))
            _git(root, "add", "hermes_cli/kanban_db.py")
            _git(root, "commit", "-m", "preserve VPS carried hunk")
        host_tree = _git(root, "rev-parse", "HEAD^{tree}")
        disconnected_head = _git(
            root,
            "commit-tree",
            host_tree,
            "-m",
            f"model disconnected {name} live HEAD",
        )
        _git(root, "reset", "--hard", disconnected_head)
        base_blob = _git(root, "rev-parse", "HEAD:hermes_cli/kanban_db.py")
        before_bytes[name] = target.read_bytes()
        target_preimage = hashlib.sha256(target.read_bytes()).hexdigest()
        subprocess.run(
            ["git", "-C", str(root), "apply", str(patch_path)],
            check=True,
            capture_output=True,
        )
        target_postimage = hashlib.sha256(target.read_bytes()).hexdigest()
        subprocess.run(
            ["git", "-C", str(root), "apply", "-R", str(patch_path)],
            check=True,
            capture_output=True,
        )
        variants[base_blob] = {
            "ast_preimages": {
                "complete_task": hermes_patch.source_member_sha256(target, "complete_task")
            },
            "target_preimage_sha256": {"hermes_cli/kanban_db.py": target_preimage},
            "target_postimage_sha256": {"hermes_cli/kanban_db.py": target_postimage},
        }
        (root / "user-owned.txt").write_bytes(f"{name} dirty state\n".encode("utf-8"))
        roots[name] = root
        records[name] = tmp_path / f"{name}-record.json"

    manifest = {
        "schema_version": "forge-hermes-patch/v1",
        "hermes_version": "0.18.2",
        "upstream_base": upstream,
        "target_files": ["hermes_cli/kanban_db.py"],
        "patch_sha256": hashlib.sha256(patch_path.read_bytes()).hexdigest(),
        "variants": variants,
    }
    manifest_path = tmp_path / "two-variant-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return roots, patch_path, manifest_path, records, before_bytes


def test_windows_and_vps_variants_have_distinct_postimages_and_round_trip(
    tmp_path: Path,
) -> None:
    roots, patch_path, manifest_path, records, before_bytes = _two_variant_fixture(tmp_path)
    current_manifest, expected_source_sha = _write_guard_current(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    windows_blob = _git(roots["windows"], "rev-parse", "HEAD:hermes_cli/kanban_db.py")
    vps_blob = _git(roots["vps"], "rev-parse", "HEAD:hermes_cli/kanban_db.py")
    assert windows_blob != vps_blob
    assert (
        manifest["variants"][windows_blob]["target_postimage_sha256"][
            "hermes_cli/kanban_db.py"
        ]
        != manifest["variants"][vps_blob]["target_postimage_sha256"][
            "hermes_cli/kanban_db.py"
        ]
    )

    for name, root in roots.items():
        assert subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "merge-base",
                "--is-ancestor",
                str(manifest["upstream_base"]),
                "HEAD",
            ],
            check=False,
            capture_output=True,
        ).returncode == 1
        before_status = _git(root, "status", "--porcelain=v1")
        record = hermes_patch.install_patch(
            root=root,
            patch_path=patch_path,
            manifest_path=manifest_path,
            record_path=records[name],
            run_tests=False,
        )
        verified = hermes_patch.verify_patch(
            root=root,
            manifest_path=manifest_path,
            record_path=records[name],
            current_manifest_path=current_manifest,
            expected_source_sha=expected_source_sha,
        )
        assert verified.base_blob == record.base_blob
        assert _git(root, "status", "--porcelain=v1") == before_status
        if name == "vps":
            assert "VPS_UNRELATED = 'preserved'" in (
                root / "hermes_cli" / "kanban_db.py"
            ).read_text(encoding="utf-8")
        hermes_patch.rollback_patch(
            root=root,
            patch_path=patch_path,
            manifest_path=manifest_path,
            record_path=records[name],
            run_tests=False,
        )
        assert (root / "hermes_cli" / "kanban_db.py").read_bytes() == before_bytes[name]
        assert _git(root, "status", "--porcelain=v1") == before_status


def test_target_only_install_and_rollback_preserve_unrelated_dirty_state(
    tmp_path: Path,
) -> None:
    root, patch_path, manifest_path, record_path, base_source = _synthetic_patch(tmp_path)
    before_status = _git(root, "status", "--porcelain=v1")
    record = hermes_patch.install_patch(
        root=root,
        patch_path=patch_path,
        manifest_path=manifest_path,
        record_path=record_path,
        run_tests=False,
    )
    assert record.previous_head != record.patch_commit
    assert (root / "hermes_cli" / "kanban_db.py").read_text(encoding="utf-8").endswith(
        "return 2\n"
    )
    assert _git(root, "status", "--porcelain=v1") == before_status
    assert _git(root, "show", "--pretty=format:", "--name-only", record.patch_commit) == "hermes_cli/kanban_db.py"

    restored_head = hermes_patch.rollback_patch(
        root=root,
        patch_path=patch_path,
        manifest_path=manifest_path,
        record_path=record_path,
        run_tests=False,
    )
    assert restored_head == record.previous_head
    assert (root / "hermes_cli" / "kanban_db.py").read_text(encoding="utf-8") == base_source
    assert _git(root, "status", "--porcelain=v1") == before_status
    assert _git(root, "rev-parse", "HEAD") == record.previous_head
    assert not record_path.exists()
    assert hermes_patch.patch_status(
        root=root,
        manifest_path=manifest_path,
        record_path=record_path,
    ) == "rolled_back"
    assert hermes_patch.rollback_patch(
        root=root,
        patch_path=patch_path,
        manifest_path=manifest_path,
        record_path=record_path,
        run_tests=False,
    ) == record.previous_head
    forwarded = hermes_patch.install_patch(
        root=root,
        patch_path=patch_path,
        manifest_path=manifest_path,
        record_path=record_path,
        run_tests=False,
    )
    assert forwarded.previous_head == record.previous_head
    assert forwarded.patch_commit != record.patch_commit
    assert hermes_patch.rollback_patch(
        root=root,
        patch_path=patch_path,
        manifest_path=manifest_path,
        record_path=record_path,
        run_tests=False,
    ) == record.previous_head
    assert not record_path.exists()


def test_install_is_idempotent_and_changed_manifest_upgrades_transactionally(
    tmp_path: Path,
) -> None:
    root, patch_path, manifest_path, record_path, _ = _synthetic_patch(tmp_path)
    before_status = _git(root, "status", "--porcelain=v1")
    first = hermes_patch.install_patch(
        root=root,
        patch_path=patch_path,
        manifest_path=manifest_path,
        record_path=record_path,
        run_tests=False,
    )
    first_record_bytes = record_path.read_bytes()
    first_backup_dirs = tuple(record_path.parent.glob(f"{record_path.stem}-targets-*"))
    assert hermes_patch.patch_status(
        root=root, manifest_path=manifest_path, record_path=record_path
    ) == "installed_same"

    reused = hermes_patch.install_patch(
        root=root,
        patch_path=patch_path,
        manifest_path=manifest_path,
        record_path=record_path,
        run_tests=False,
    )
    assert reused == first
    assert record_path.read_bytes() == first_record_bytes
    assert tuple(record_path.parent.glob(f"{record_path.stem}-targets-*")) == first_backup_dirs
    assert _git(root, "rev-parse", "HEAD") == first.patch_commit

    changed = json.loads(manifest_path.read_text(encoding="utf-8"))
    changed["hermes_version"] = "0.18.2+guard2"
    manifest_path.write_text(json.dumps(changed, sort_keys=True), encoding="utf-8")
    assert hermes_patch.patch_status(
        root=root, manifest_path=manifest_path, record_path=record_path
    ) == "installed_other"
    upgraded = hermes_patch.install_patch(
        root=root,
        patch_path=patch_path,
        manifest_path=manifest_path,
        record_path=record_path,
        run_tests=False,
    )
    assert upgraded.manifest_sha256 != first.manifest_sha256
    assert upgraded.previous_head == first.previous_head
    assert upgraded.patch_commit != first.patch_commit
    assert _git(root, "status", "--porcelain=v1") == before_status


def test_unpatched_supported_variant_status_does_not_require_exact_head(
    tmp_path: Path,
) -> None:
    root, _, manifest_path, record_path, _ = _synthetic_patch(tmp_path)
    (root / "unrelated-commit.txt").write_text("carried commit\n", encoding="utf-8")
    _git(root, "add", "unrelated-commit.txt")
    _git(root, "commit", "-m", "allowed unrelated carried commit")
    _git(root, "update-ref", "refs/remotes/origin/main", _git(root, "rev-parse", "HEAD"))

    assert hermes_patch.patch_status(
        root=root,
        manifest_path=manifest_path,
        record_path=record_path,
    ) == "supported_base"


def test_status_check_and_install_require_immutable_approved_base_ref(
    tmp_path: Path,
) -> None:
    root, patch_path, manifest_path, record_path, _ = _synthetic_patch(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    approved_ref = "refs/infinity-forge/approved-base"
    _git(root, "update-ref", "-d", approved_ref)
    with pytest.raises(hermes_patch.PatchInstallError, match="approved-base ref is missing"):
        hermes_patch.patch_status(
            root=root,
            manifest_path=manifest_path,
            record_path=record_path,
        )

    wrong_object = _git(root, "rev-parse", "HEAD:hermes_cli/kanban_db.py")
    _git(root, "update-ref", approved_ref, wrong_object)
    with pytest.raises(hermes_patch.PatchInstallError, match="approved-base ref mismatch"):
        hermes_patch.check_patch(
            root=root,
            patch_path=patch_path,
            manifest_path=manifest_path,
        )

    _git(root, "update-ref", approved_ref, str(manifest["upstream_base"]))
    installed = hermes_patch.install_patch(
        root=root,
        patch_path=patch_path,
        manifest_path=manifest_path,
        record_path=record_path,
        run_tests=False,
    )
    assert installed.previous_head == str(manifest["upstream_base"])
    _git(root, "update-ref", "-d", approved_ref)
    with pytest.raises(hermes_patch.PatchInstallError, match="approved-base ref is missing"):
        hermes_patch.patch_status(
            root=root,
            manifest_path=manifest_path,
            record_path=record_path,
        )
    with pytest.raises(hermes_patch.PatchInstallError, match="approved-base ref is missing"):
        hermes_patch.install_patch(
            root=root,
            patch_path=patch_path,
            manifest_path=manifest_path,
            record_path=record_path,
            run_tests=False,
        )


def test_failed_upgrade_restores_old_patch_head_record_and_dirty_state(
    tmp_path: Path,
) -> None:
    root, patch_path, manifest_path, record_path, _ = _synthetic_patch(tmp_path)
    installed = hermes_patch.install_patch(
        root=root,
        patch_path=patch_path,
        manifest_path=manifest_path,
        record_path=record_path,
        run_tests=False,
    )
    old = {
        "head": _git(root, "rev-parse", "HEAD"),
        "target": (root / "hermes_cli" / "kanban_db.py").read_bytes(),
        "record": record_path.read_bytes(),
        "status": _git(root, "status", "--porcelain=v1"),
    }
    changed = json.loads(manifest_path.read_text(encoding="utf-8"))
    variant = changed["variants"][installed.base_blob]
    variant["target_postimage_sha256"]["hermes_cli/kanban_db.py"] = "0" * 64
    manifest_path.write_text(json.dumps(changed, sort_keys=True), encoding="utf-8")

    with pytest.raises(hermes_patch.PatchInstallError, match="postimage SHA-256 mismatch"):
        hermes_patch.install_patch(
            root=root,
            patch_path=patch_path,
            manifest_path=manifest_path,
            record_path=record_path,
            run_tests=False,
        )

    assert _git(root, "rev-parse", "HEAD") == old["head"]
    assert (root / "hermes_cli" / "kanban_db.py").read_bytes() == old["target"]
    assert record_path.read_bytes() == old["record"]
    assert _git(root, "status", "--porcelain=v1") == old["status"]


def test_upgrade_prepare_failure_restores_old_install_immediately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, patch_path, manifest_path, record_path, _ = _synthetic_patch(tmp_path)
    installed = hermes_patch.install_patch(
        root=root,
        patch_path=patch_path,
        manifest_path=manifest_path,
        record_path=record_path,
        run_tests=False,
    )
    before = {
        "head": _git(root, "rev-parse", "HEAD"),
        "target": (root / "hermes_cli" / "kanban_db.py").read_bytes(),
        "record": record_path.read_bytes(),
        "status": _git(root, "status", "--porcelain=v1"),
    }
    changed = json.loads(manifest_path.read_text(encoding="utf-8"))
    changed["hermes_version"] = "0.18.2+prepare-failure"
    manifest_path.write_text(json.dumps(changed, sort_keys=True), encoding="utf-8")
    monkeypatch.setattr(
        hermes_patch,
        "_mark_base_prepared",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("prepare fsync failed")),
    )

    with pytest.raises(OSError, match="prepare fsync failed"):
        hermes_patch.install_patch(
            root=root,
            patch_path=patch_path,
            manifest_path=manifest_path,
            record_path=record_path,
            run_tests=False,
        )

    assert _git(root, "rev-parse", "HEAD") == installed.patch_commit == before["head"]
    assert (root / "hermes_cli" / "kanban_db.py").read_bytes() == before["target"]
    assert record_path.read_bytes() == before["record"]
    assert _git(root, "status", "--porcelain=v1") == before["status"]
    assert not hermes_patch._patch_journal_path(record_path).exists()


def test_post_commit_or_record_failure_restores_exact_preinstall_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, patch_path, manifest_path, record_path, base_source = _synthetic_patch(tmp_path)
    before_head = _git(root, "rev-parse", "HEAD")
    before_status = _git(root, "status", "--porcelain=v1")
    real_atomic_json = hermes_patch._atomic_json

    def fail_active_record(path: Path, value: dict[str, object]) -> None:
        if path.resolve() == record_path.resolve():
            raise OSError("record fsync failed")
        real_atomic_json(path, value)

    monkeypatch.setattr(hermes_patch, "_atomic_json", fail_active_record)

    with pytest.raises(OSError, match="record fsync failed"):
        hermes_patch.install_patch(
            root=root,
            patch_path=patch_path,
            manifest_path=manifest_path,
            record_path=record_path,
            run_tests=False,
        )

    assert _git(root, "rev-parse", "HEAD") == before_head
    assert (root / "hermes_cli" / "kanban_db.py").read_text(encoding="utf-8") == base_source
    assert _git(root, "status", "--porcelain=v1") == before_status
    assert not record_path.exists()


def test_install_uses_deterministic_identity_and_disables_commit_signing(
    tmp_path: Path,
) -> None:
    root, patch_path, manifest_path, record_path, _ = _synthetic_patch(tmp_path)
    _git(root, "config", "--unset", "user.name")
    _git(root, "config", "--unset", "user.email")
    _git(root, "config", "commit.gpgSign", "true")

    record = hermes_patch.install_patch(
        root=root,
        patch_path=patch_path,
        manifest_path=manifest_path,
        record_path=record_path,
        run_tests=False,
    )

    assert _git(root, "show", "-s", "--format=%an", record.patch_commit) == "InfinityForge"
    assert _git(root, "show", "-s", "--format=%ae", record.patch_commit) == "infinity-forge@invalid"


def test_atomic_writers_flush_and_replace_failure_preserves_previous_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = tmp_path / "record.json"
    fsync_calls: list[int] = []
    real_fsync = hermes_patch.os.fsync

    def tracking_fsync(file_descriptor: int) -> None:
        fsync_calls.append(file_descriptor)
        real_fsync(file_descriptor)

    monkeypatch.setattr(hermes_patch.os, "fsync", tracking_fsync)
    hermes_patch._atomic_json(record, {"state": "old"})
    assert fsync_calls
    if hermes_patch.os.name != "nt":
        assert len(fsync_calls) >= 2
    previous = record.read_bytes()

    monkeypatch.setattr(
        hermes_patch,
        "_replace_path",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )
    with pytest.raises(OSError, match="replace failed"):
        hermes_patch._atomic_json(record, {"state": "new"})
    assert record.read_bytes() == previous
    assert list(tmp_path.glob(".record.json.*.tmp")) == []

    monkeypatch.undo()
    real_directory_fsync = hermes_patch._fsync_directory
    directory_calls = 0

    def fail_first_directory_fsync(path: Path) -> None:
        nonlocal directory_calls
        directory_calls += 1
        if directory_calls == 1:
            raise OSError("directory fsync failed")
        real_directory_fsync(path)

    monkeypatch.setattr(
        hermes_patch,
        "_fsync_directory",
        fail_first_directory_fsync,
    )
    with pytest.raises(OSError, match="directory fsync failed"):
        hermes_patch._atomic_json(record, {"state": "new-after-replace"})
    assert directory_calls == 2
    assert record.read_bytes() == previous

    monkeypatch.undo()
    byte_record = tmp_path / "record.bin"
    hermes_patch._atomic_bytes(byte_record, b"old-bytes")
    real_directory_fsync = hermes_patch._fsync_directory
    byte_directory_calls = 0

    def fail_first_byte_directory_fsync(path: Path) -> None:
        nonlocal byte_directory_calls
        byte_directory_calls += 1
        if byte_directory_calls == 1:
            raise OSError("byte directory fsync failed")
        real_directory_fsync(path)

    monkeypatch.setattr(
        hermes_patch,
        "_fsync_directory",
        fail_first_byte_directory_fsync,
    )
    with pytest.raises(OSError, match="byte directory fsync failed"):
        hermes_patch._atomic_bytes(byte_record, b"new-bytes")
    assert byte_directory_calls == 2
    assert byte_record.read_bytes() == b"old-bytes"


def test_status_recovers_crash_after_commit_before_active_record(
    tmp_path: Path,
) -> None:
    root, patch_path, manifest_path, record_path, base_source = _synthetic_patch(tmp_path)
    checked = hermes_patch.check_patch(
        root=root,
        patch_path=patch_path,
        manifest_path=manifest_path,
    )
    targets = list(checked.manifest["target_files"])
    previous_head = _git(root, "rev-parse", "HEAD")
    backup_dir = tmp_path / "crash-backup"
    before_hashes = hermes_patch._backup_targets(root, targets, backup_dir)
    journal = hermes_patch._begin_patch_transaction(
        root=root,
        record_path=record_path,
        previous_head=previous_head,
        targets=targets,
        before_hashes=before_hashes,
        postimage_hashes=dict(checked.variant["target_postimage_sha256"]),
        backup_dir=backup_dir,
        manifest_path=manifest_path,
        patch_path=patch_path,
    )
    subprocess.run(["git", "-C", str(root), "apply", str(patch_path)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "--", *targets], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-qm", "simulated crash commit"],
        check=True,
    )
    assert journal.exists()
    assert not record_path.exists()

    crash_head = _git(root, "rev-parse", "HEAD")
    crash_index = root / ".git" / "index"
    crash_readonly = {
        "head": crash_head,
        "index": crash_index.read_bytes(),
        "index_mtime_ns": crash_index.stat().st_mtime_ns,
        "target": (root / "hermes_cli" / "kanban_db.py").read_bytes(),
        "journal": journal.read_bytes(),
        "record_exists": record_path.exists(),
    }
    assert hermes_patch.patch_status(
        root=root,
        manifest_path=manifest_path,
        record_path=record_path,
    ) == "recovery_required"
    assert {
        "head": _git(root, "rev-parse", "HEAD"),
        "index": crash_index.read_bytes(),
        "index_mtime_ns": crash_index.stat().st_mtime_ns,
        "target": (root / "hermes_cli" / "kanban_db.py").read_bytes(),
        "journal": journal.read_bytes(),
        "record_exists": record_path.exists(),
    } == crash_readonly
    assert hermes_patch.recover_patch(
        root=root,
        manifest_path=manifest_path,
        record_path=record_path,
    ) == "supported_base"
    assert _git(root, "rev-parse", "HEAD") == previous_head
    assert (root / "hermes_cli" / "kanban_db.py").read_text(encoding="utf-8") == base_source
    assert not journal.exists()

    staged_root, staged_patch, staged_manifest, staged_record, _ = _synthetic_patch(
        tmp_path / "staged"
    )
    staged_check = hermes_patch.check_patch(
        root=staged_root,
        patch_path=staged_patch,
        manifest_path=staged_manifest,
    )
    staged_targets = list(staged_check.manifest["target_files"])
    staged_backup = tmp_path / "staged-backup"
    staged_before = hermes_patch._backup_targets(
        staged_root, staged_targets, staged_backup
    )
    staged_journal = hermes_patch._begin_patch_transaction(
        root=staged_root,
        record_path=staged_record,
        previous_head=_git(staged_root, "rev-parse", "HEAD"),
        targets=staged_targets,
        before_hashes=staged_before,
        postimage_hashes=dict(staged_check.variant["target_postimage_sha256"]),
        backup_dir=staged_backup,
        manifest_path=staged_manifest,
        patch_path=staged_patch,
    )
    subprocess.run(
        ["git", "-C", str(staged_root), "apply", str(staged_patch)], check=True
    )
    subprocess.run(
        ["git", "-C", str(staged_root), "add", "--", *staged_targets], check=True
    )
    staged_index = staged_root / ".git" / "index"
    staged_readonly = {
        "head": _git(staged_root, "rev-parse", "HEAD"),
        "index": staged_index.read_bytes(),
        "index_mtime_ns": staged_index.stat().st_mtime_ns,
        "target": (staged_root / "hermes_cli" / "kanban_db.py").read_bytes(),
        "journal": staged_journal.read_bytes(),
        "record_exists": staged_record.exists(),
    }
    assert hermes_patch.patch_status(
        root=staged_root,
        manifest_path=staged_manifest,
        record_path=staged_record,
    ) == "recovery_required"
    assert {
        "head": _git(staged_root, "rev-parse", "HEAD"),
        "index": staged_index.read_bytes(),
        "index_mtime_ns": staged_index.stat().st_mtime_ns,
        "target": (staged_root / "hermes_cli" / "kanban_db.py").read_bytes(),
        "journal": staged_journal.read_bytes(),
        "record_exists": staged_record.exists(),
    } == staged_readonly
    assert _git(staged_root, "diff", "--cached", "--name-only") != ""
    assert hermes_patch.recover_patch(
        root=staged_root,
        manifest_path=staged_manifest,
        record_path=staged_record,
    ) == "supported_base"
    assert _git(staged_root, "diff", "--cached", "--name-only") == ""
    assert not staged_journal.exists()


def test_installed_terminal_journal_recovers_forward_after_finish_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, patch_path, manifest_path, record_path, _ = _synthetic_patch(tmp_path)
    previous_head = _git(root, "rev-parse", "HEAD")
    monkeypatch.setattr(
        hermes_patch,
        "_finish_patch_transaction",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("journal unlink failed")),
    )

    with pytest.raises(OSError, match="journal unlink failed"):
        hermes_patch.install_patch(
            root=root,
            patch_path=patch_path,
            manifest_path=manifest_path,
            record_path=record_path,
            run_tests=False,
        )

    journal = hermes_patch._patch_journal_path(record_path)
    assert json.loads(journal.read_text(encoding="utf-8"))["state"] == "installed"
    assert record_path.exists()
    installed_head = _git(root, "rev-parse", "HEAD")
    assert installed_head != previous_head
    assert hermes_patch.patch_status(
        root=root,
        manifest_path=manifest_path,
        record_path=record_path,
    ) == "recovery_required"

    monkeypatch.undo()
    assert hermes_patch.recover_patch(
        root=root,
        manifest_path=manifest_path,
        record_path=record_path,
    ) == "installed_same"
    assert _git(root, "rev-parse", "HEAD") == installed_head
    assert record_path.exists()
    assert not journal.exists()


def test_status_recovers_interrupted_rollback_to_installed_patch(
    tmp_path: Path,
) -> None:
    root, patch_path, manifest_path, record_path, _ = _synthetic_patch(tmp_path)
    record = hermes_patch.install_patch(
        root=root,
        patch_path=patch_path,
        manifest_path=manifest_path,
        record_path=record_path,
        run_tests=False,
    )
    journal = hermes_patch._begin_patch_transaction(
        root=root,
        record_path=record_path,
        previous_head=record.previous_head,
        targets=record.target_files,
        before_hashes=dict(record.target_before_sha256),
        postimage_hashes=dict(record.target_postimage_sha256),
        backup_dir=Path(record.backup_dir),
        manifest_path=manifest_path,
        patch_path=patch_path,
    )
    hermes_patch._reset_head_and_restore_backups(
        root,
        from_head=record.patch_commit,
        to_head=record.previous_head,
        targets=record.target_files,
        backup_dir=Path(record.backup_dir),
        before_hashes=dict(record.target_before_sha256),
    )
    assert journal.exists()

    assert hermes_patch.patch_status(
        root=root,
        manifest_path=manifest_path,
        record_path=record_path,
    ) == "recovery_required"
    assert _git(root, "rev-parse", "HEAD") == record.previous_head
    assert hermes_patch.recover_patch(
        root=root,
        manifest_path=manifest_path,
        record_path=record_path,
    ) == "installed_same"
    assert _git(root, "rev-parse", "HEAD") == record.patch_commit
    assert record_path.exists()
    assert not journal.exists()


def test_rollback_uses_verified_backup_and_failure_restores_installed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, patch_path, manifest_path, record_path, base_source = _synthetic_patch(tmp_path)
    installed = hermes_patch.install_patch(
        root=root,
        patch_path=patch_path,
        manifest_path=manifest_path,
        record_path=record_path,
        run_tests=False,
    )
    installed_target = (root / "hermes_cli" / "kanban_db.py").read_bytes()
    installed_status = _git(root, "status", "--porcelain=v1")

    def fail_tests(*args, **kwargs) -> None:
        raise RuntimeError("rollback tests failed")

    monkeypatch.setattr(hermes_patch, "_targeted_tests", fail_tests)
    with pytest.raises(RuntimeError, match="rollback tests failed"):
        hermes_patch.rollback_patch(
            root=root,
            patch_path=patch_path,
            manifest_path=manifest_path,
            record_path=record_path,
            run_tests=True,
        )
    assert _git(root, "rev-parse", "HEAD") == installed.patch_commit
    assert (root / "hermes_cli" / "kanban_db.py").read_bytes() == installed_target
    assert _git(root, "status", "--porcelain=v1") == installed_status

    monkeypatch.undo()
    monkeypatch.setattr(
        hermes_patch,
        "_archive_successful_rollback",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("rollback archive failed")),
    )
    with pytest.raises(OSError, match="rollback archive failed"):
        hermes_patch.rollback_patch(
            root=root,
            patch_path=patch_path,
            manifest_path=manifest_path,
            record_path=record_path,
            run_tests=False,
        )
    assert _git(root, "rev-parse", "HEAD") == installed.patch_commit
    assert (root / "hermes_cli" / "kanban_db.py").read_bytes() == installed_target
    assert record_path.exists()
    assert _git(root, "status", "--porcelain=v1") == installed_status

    monkeypatch.undo()
    patch_path.write_bytes(b"reverse apply is intentionally unavailable\n")
    previous_head = hermes_patch.rollback_patch(
        root=root,
        patch_path=patch_path,
        manifest_path=manifest_path,
        record_path=record_path,
        run_tests=False,
    )
    assert previous_head == installed.previous_head
    assert (root / "hermes_cli" / "kanban_db.py").read_text(encoding="utf-8") == base_source


def test_check_cli_emits_json_serializable_patch_check(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, patch_path, manifest_path, _, _ = _synthetic_patch(tmp_path)

    assert hermes_patch.main(
        [
            "check",
            "--root",
            str(root),
            "--patch",
            str(patch_path),
            "--manifest",
            str(manifest_path),
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["base_blob"] == _git(
        root,
        "rev-parse",
        "HEAD:hermes_cli/kanban_db.py",
    )
    assert payload["manifest"]["patch_sha256"] == hashlib.sha256(
        patch_path.read_bytes()
    ).hexdigest()


def test_verify_cli_accepts_exact_cross_contract_arguments(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, patch_path, manifest_path, record_path, _ = _synthetic_patch(tmp_path)
    current_manifest, expected_source_sha = _write_guard_current(tmp_path)
    installed = hermes_patch.install_patch(
        root=root,
        patch_path=patch_path,
        manifest_path=manifest_path,
        record_path=record_path,
        run_tests=False,
    )
    with pytest.raises(SystemExit) as missing_cross_contract:
        hermes_patch.main(
            [
                "verify",
                "--root",
                str(root),
                "--manifest",
                str(manifest_path),
                "--record",
                str(record_path),
            ]
        )
    assert missing_cross_contract.value.code == 2
    capsys.readouterr()

    exit_code = hermes_patch.main(
        [
            "verify",
            "--root",
            str(root),
            "--manifest",
            str(manifest_path),
            "--record",
            str(record_path),
            "--current-manifest",
            str(current_manifest),
            "--expected-source-sha",
            expected_source_sha,
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["base_blob"] == installed.base_blob
    assert payload["patch_commit"] == installed.patch_commit


def test_verify_validates_guard_schema_source_and_artifact_before_record(
    tmp_path: Path,
) -> None:
    current_manifest, expected_source_sha = _write_guard_current(tmp_path)
    raw = json.loads(current_manifest.read_text(encoding="utf-8"))
    raw["unexpected"] = True
    current_manifest.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(
        hermes_patch.PatchInstallError,
        match="guard current top-level fields do not match schema",
    ):
        hermes_patch.verify_patch(
            root=tmp_path / "missing-root",
            manifest_path=tmp_path / "missing-manifest.json",
            record_path=tmp_path / "missing-record.json",
            current_manifest_path=current_manifest,
            expected_source_sha=expected_source_sha,
        )

    current_manifest, expected_source_sha = _write_guard_current(tmp_path)
    with pytest.raises(
        hermes_patch.PatchInstallError,
        match="expected_source_sha must be 40 lowercase hex characters",
    ):
        hermes_patch.verify_patch(
            root=tmp_path / "missing-root",
            manifest_path=tmp_path / "missing-manifest.json",
            record_path=tmp_path / "missing-record.json",
            current_manifest_path=current_manifest,
            expected_source_sha="not-a-source-sha",
        )

    with pytest.raises(
        hermes_patch.PatchInstallError,
        match="guard artifact release directory does not match source SHA",
    ):
        hermes_patch.verify_patch(
            root=tmp_path / "missing-root",
            manifest_path=tmp_path / "missing-manifest.json",
            record_path=tmp_path / "missing-record.json",
            current_manifest_path=current_manifest,
            expected_source_sha="2" * 40,
        )

    raw = json.loads(current_manifest.read_text(encoding="utf-8"))
    artifact = Path(raw["policies"]["forge-v1"]["artifact"])
    artifact.write_bytes(b"tampered-guard")
    with pytest.raises(
        hermes_patch.PatchInstallError,
        match="guard artifact SHA-256 mismatch",
    ):
        hermes_patch.verify_patch(
            root=tmp_path / "missing-root",
            manifest_path=tmp_path / "missing-manifest.json",
            record_path=tmp_path / "missing-record.json",
            current_manifest_path=current_manifest,
            expected_source_sha=expected_source_sha,
        )


def test_install_refuses_dirty_target_and_preserves_every_file(tmp_path: Path) -> None:
    root, patch_path, manifest_path, record_path, _ = _synthetic_patch(tmp_path)
    target = root / "hermes_cli" / "kanban_db.py"
    target.write_text("def complete_task():\n    return 99\n", encoding="utf-8")
    before = {
        "target": target.read_bytes(),
        "unrelated": (root / "unrelated.txt").read_bytes(),
        "status": _git(root, "status", "--porcelain=v1"),
        "head": _git(root, "rev-parse", "HEAD"),
    }
    with pytest.raises(hermes_patch.PatchInstallError, match="target files must be clean"):
        hermes_patch.install_patch(
            root=root,
            patch_path=patch_path,
            manifest_path=manifest_path,
            record_path=record_path,
            run_tests=False,
        )
    after = {
        "target": target.read_bytes(),
        "unrelated": (root / "unrelated.txt").read_bytes(),
        "status": _git(root, "status", "--porcelain=v1"),
        "head": _git(root, "rev-parse", "HEAD"),
    }
    assert after == before


def test_check_rejects_unsupported_blob_before_git_apply(tmp_path: Path) -> None:
    root, patch_path, manifest_path, _, _ = _synthetic_patch(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    only_variant = next(iter(manifest["variants"].values()))
    manifest["variants"] = {"0" * 40: only_variant}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(hermes_patch.PatchInstallError, match="unsupported kanban_db blob"):
        hermes_patch.check_patch(
            root=root,
            patch_path=patch_path,
            manifest_path=manifest_path,
        )
```

- [ ] **Step 2: RED를 실행한다**

```powershell
Set-Location C:\01.project\INFINITY_FORGE
$HermesPython = "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe"
$PytestTarget = "$env:TEMP\infinity-forge-pytest-3.11"
$env:PYTHONUTF8 = "1"
uv pip install --target $PytestTarget --python $HermesPython --upgrade pytest
$PreviousPythonPath = $env:PYTHONPATH
try {
  $env:PYTHONPATH = "$PytestTarget;$PreviousPythonPath"
  & $HermesPython -X utf8 -m pytest tests/hermes/test_hermes_patch.py -q
} finally {
  $env:PYTHONPATH = $PreviousPythonPath
}
```

Expected: `ModuleNotFoundError: No module named 'forge.ops.hermes_patch'`로 FAIL한다.

- [ ] **Step 3: 다음 manifest constants와 target-only core를 최소 구현한다**

```python
from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any


UPSTREAM_BASE = "4281151ae859241351ba14d8c7682dc67ff4c126"
APPROVED_BASE_REF = "refs/infinity-forge/approved-base"
REQUIRED_VARIANT_BLOBS = {
    "518e74eb0647786a0361105b76bfbaeb1bad3e19",
    "6150b141537b947a2a89d19b13be4fbad2330711",
}
AST_PREIMAGES = {
    "Task": "37dbff1faa5f92afa3b63e3d80a1c041e36a0a5fcebd2dc9585bb8c824656137",
    "_migrate_add_optional_columns": "e8d018507072b7aa7a9d875bde98b389446bb9fb5c61efdfd4e0b1a09fd82583",
    "create_task": "d95d2c6f0bd66eb3419ce2ee3ad49faa4f211b28624e3cd36e1efbbd8bd265aa",
    "recompute_ready": "d6e8a2840b92a4c38a9d41e358f49c35c90d386f14834d91a1abe4ff682249e8",
    "complete_task": "a10e062b91aeef9e8c097997c39840b3bf1b0d0552764681613038505b286bf2",
    "edit_completed_task_result": "bcf22376052004ea28747d65a95260edcc30781b7e53f7b8ebfa8de72e82e2e2",
    "detect_crashed_workers": "d7dca0d5a3943b21108e1fb36fca5bb98e13b68b95001b72bf79b5024df9235a",
}
TARGET_FILES = [
    "hermes_cli/kanban_completion_policy.py",
    "hermes_cli/kanban_db.py",
    "hermes_cli/kanban.py",
    "plugins/kanban/dashboard/plugin_api.py",
    "tests/hermes_cli/test_kanban_completion_policy.py",
]


class PatchInstallError(RuntimeError):
    pass


@dataclass(frozen=True)
class PatchCheck:
    manifest: dict[str, Any]
    base_blob: str
    variant: dict[str, Any]


@dataclass(frozen=True)
class InstallRecord:
    schema_version: str
    root: str
    base_blob: str
    previous_head: str
    patch_commit: str
    manifest_sha256: str
    patch_sha256: str
    target_files: list[str]
    target_before_sha256: dict[str, str | None]
    target_postimage_sha256: dict[str, str]
    backup_dir: str
    installed_at: int


@dataclass(frozen=True)
class PatchJournal:
    schema_version: str
    state: str
    base_prepared: bool
    root: str
    previous_head: str
    patch_commit: str | None
    target_files: list[str]
    target_before_sha256: dict[str, str | None]
    target_postimage_sha256: dict[str, str]
    backup_dir: str
    candidate_manifest_sha256: str
    candidate_patch_sha256: str
    candidate_record_sha256: str | None
    previous_record_archive: str | None
    previous_record_sha256: str | None


def _run(root: Path, *argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [*argv],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=False,
        shell=False,
    )
    if check and completed.returncode != 0:
        raise PatchInstallError(
            f"command failed ({completed.returncode}): {' '.join(argv)}: {completed.stderr.strip()}"
        )
    return completed


def _git(root: Path, *args: str) -> str:
    return _run(root, "git", *args).stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_member_sha256(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name == name:
            segment = ast.get_source_segment(source, node)
            if segment is None:
                break
            return hashlib.sha256(segment.encode()).hexdigest()
    raise PatchInstallError(f"source member not found: {name}")


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PatchInstallError(f"invalid patch manifest: {path}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != "forge-hermes-patch/v1":
        raise PatchInstallError("invalid patch manifest schema_version")
    if set(value) != {
        "schema_version",
        "hermes_version",
        "upstream_base",
        "target_files",
        "patch_sha256",
        "variants",
    }:
        raise PatchInstallError("invalid patch manifest top-level keys")
    variants = value.get("variants")
    if not isinstance(variants, dict) or not variants:
        raise PatchInstallError("patch manifest variants must be a non-empty object")
    for base_blob, variant in variants.items():
        if not isinstance(base_blob, str) or len(base_blob) != 40 or not isinstance(variant, dict):
            raise PatchInstallError("invalid patch manifest variant")
        if set(variant) != {
            "ast_preimages",
            "target_preimage_sha256",
            "target_postimage_sha256",
        }:
            raise PatchInstallError(f"invalid patch manifest variant keys: {base_blob}")
    return value


def _patch_targets(root: Path, patch_path: Path) -> list[str]:
    output = _run(root, "git", "apply", "--numstat", str(patch_path)).stdout
    targets = []
    for line in output.splitlines():
        fields = line.split("\t", 2)
        if len(fields) != 3:
            raise PatchInstallError("cannot parse patch target list")
        targets.append(fields[2])
    return sorted(set(targets))


def _status(root: Path) -> str:
    return _run(
        root,
        "git",
        "--no-optional-locks",
        "status",
        "--porcelain=v1",
        "-z",
    ).stdout


def _assert_no_staged_changes(root: Path) -> None:
    if _run(root, "git", "diff", "--cached", "--name-only").stdout.strip():
        raise PatchInstallError("repository has staged changes")


def _assert_targets_clean(root: Path, targets: list[str]) -> None:
    output = _run(
        root,
        "git",
        "--no-optional-locks",
        "status",
        "--porcelain=v1",
        "--",
        *targets,
    ).stdout.strip()
    if output:
        raise PatchInstallError("target files must be clean")


def _assert_approved_base(root: Path, upstream_base: str) -> None:
    resolved = _run(
        root,
        "git",
        "rev-parse",
        "--verify",
        APPROVED_BASE_REF,
        check=False,
    )
    if resolved.returncode != 0:
        raise PatchInstallError("approved-base ref is missing")
    if resolved.stdout.strip() != upstream_base:
        raise PatchInstallError("approved-base ref mismatch")
    _run(root, "git", "cat-file", "-e", f"{upstream_base}^{{commit}}")


def patch_status(
    *,
    root: Path,
    manifest_path: Path,
    record_path: Path,
) -> str:
    root = root.resolve()
    if _patch_journal_path(record_path.resolve()).exists():
        return "recovery_required"
    manifest_path = manifest_path.resolve()
    manifest = _load_manifest(manifest_path)
    upstream_base = str(manifest["upstream_base"])
    _assert_approved_base(root, upstream_base)
    if record_path.resolve().is_file():
        record = _verify_install_record_self_contained(
            root=root,
            record_path=record_path,
        )
        if (
            record.manifest_sha256 == _sha256(manifest_path)
            and record.patch_sha256 == manifest["patch_sha256"]
        ):
            return "installed_same"
        return "installed_other"
    targets = [str(value) for value in manifest["target_files"]]
    _assert_no_staged_changes(root)
    _assert_targets_clean(root, targets)
    blob = _git(root, "rev-parse", "HEAD:hermes_cli/kanban_db.py")
    variants = manifest["variants"]
    if blob not in variants:
        raise PatchInstallError(f"unsupported kanban_db blob: {blob}")
    for relative, expected in variants[blob]["target_preimage_sha256"].items():
        target = root / relative
        if expected is None:
            if target.exists():
                raise PatchInstallError(f"target preimage expected absent: {relative}")
        elif not target.is_file() or _sha256(target) != expected:
            raise PatchInstallError(f"target preimage SHA-256 mismatch: {relative}")
    if _verify_idempotent_rollback(
        root=root,
        manifest_path=manifest_path,
        record_path=record_path.resolve(),
    ) is not None:
        return "rolled_back"
    return "supported_base"


def check_patch(
    *,
    root: Path,
    patch_path: Path,
    manifest_path: Path,
) -> PatchCheck:
    root = root.resolve()
    manifest = _load_manifest(manifest_path.resolve())
    upstream_base = str(manifest.get("upstream_base"))
    _assert_approved_base(root, upstream_base)
    target_files = [str(path) for path in manifest.get("target_files", [])]
    if not target_files or len(target_files) != len(set(target_files)):
        raise PatchInstallError("target_files must be a unique non-empty list")
    patch_targets = _patch_targets(root, patch_path.resolve())
    if patch_targets != sorted(target_files):
        raise PatchInstallError("patch target list differs from manifest allowlist")
    if _sha256(patch_path.resolve()) != manifest.get("patch_sha256"):
        raise PatchInstallError("patch SHA-256 mismatch")
    _assert_no_staged_changes(root)
    _assert_targets_clean(root, target_files)
    blob = _git(root, "rev-parse", "HEAD:hermes_cli/kanban_db.py")
    variants = manifest.get("variants")
    if not isinstance(variants, dict) or not isinstance(variants.get(blob), dict):
        raise PatchInstallError(f"unsupported kanban_db blob: {blob}")
    variant = dict(variants[blob])
    if set(variant) != {
        "ast_preimages",
        "target_preimage_sha256",
        "target_postimage_sha256",
    }:
        raise PatchInstallError(f"invalid manifest variant keys for {blob}")
    kanban_db = root / "hermes_cli" / "kanban_db.py"
    for name, expected in dict(variant["ast_preimages"]).items():
        actual = source_member_sha256(kanban_db, str(name))
        if actual != expected:
            raise PatchInstallError(f"AST preimage mismatch for {name}: {actual}")
    target_preimages = dict(variant["target_preimage_sha256"])
    target_postimages = dict(variant["target_postimage_sha256"])
    if set(target_preimages) != set(target_files) or set(target_postimages) != set(target_files):
        raise PatchInstallError("variant target hash maps differ from target_files")
    for relative, expected in target_preimages.items():
        target = root / relative
        if expected is None:
            if target.exists():
                raise PatchInstallError(f"target preimage expected absent: {relative}")
        elif not target.is_file() or _sha256(target) != expected:
            raise PatchInstallError(f"target preimage SHA-256 mismatch: {relative}")
    _run(root, "git", "apply", "--check", str(patch_path.resolve()))
    return PatchCheck(manifest=manifest, base_blob=blob, variant=variant)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _replace_path(source: Path, destination: Path) -> None:
    if os.name != "nt":
        os.replace(source, destination)
        return
    import ctypes
    from ctypes import wintypes

    move_file_ex = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move_file_ex.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    move_file_ex.restype = wintypes.BOOL
    movefile_replace_existing = 0x00000001
    movefile_write_through = 0x00000008
    move_flags = movefile_write_through
    if destination.exists():
        move_flags |= movefile_replace_existing
    if not move_file_ex(
        str(source.resolve()),
        str(destination.resolve()),
        move_flags,
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _ensure_directory_durable(path: Path) -> None:
    if path.is_dir():
        return
    if path.exists():
        raise PatchInstallError(f"durable directory path is not a directory: {path}")
    if path.parent == path:
        raise PatchInstallError(f"cannot create durable filesystem root: {path}")
    _ensure_directory_durable(path.parent)
    staging = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.mkdir")
    staging.mkdir()
    try:
        _replace_path(staging, path)
        _fsync_directory(path.parent)
    finally:
        if staging.exists():
            staging.rmdir()


def _unlink_path_durable(path: Path) -> None:
    if not path.exists():
        return
    if os.name != "nt":
        path.unlink()
        _fsync_directory(path.parent)
        return
    delete_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.delete")
    _replace_path(path, delete_path)
    try:
        delete_path.unlink()
    except BaseException:
        _replace_path(delete_path, path)
        raise


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    _ensure_directory_durable(path.parent)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    rollback_temp = path.with_name(f".{path.name}.{os.getpid()}.rollback")
    previous = path.read_bytes() if path.exists() else None
    replaced = False
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_path(temporary, path)
        replaced = True
        _fsync_directory(path.parent)
    except BaseException:
        if replaced:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                with rollback_temp.open("wb") as handle:
                    handle.write(previous)
                    handle.flush()
                    os.fsync(handle.fileno())
                _replace_path(rollback_temp, path)
            _fsync_directory(path.parent)
        raise
    finally:
        if temporary.exists():
            temporary.unlink()
        if rollback_temp.exists():
            rollback_temp.unlink()


def _atomic_bytes(path: Path, payload: bytes) -> None:
    _ensure_directory_durable(path.parent)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    rollback_temp = path.with_name(f".{path.name}.{os.getpid()}.rollback")
    previous = path.read_bytes() if path.exists() else None
    replaced = False
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_path(temporary, path)
        replaced = True
        _fsync_directory(path.parent)
    except BaseException:
        if replaced:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                with rollback_temp.open("wb") as handle:
                    handle.write(previous)
                    handle.flush()
                    os.fsync(handle.fileno())
                _replace_path(rollback_temp, path)
            _fsync_directory(path.parent)
        raise
    finally:
        if temporary.exists():
            temporary.unlink()
        if rollback_temp.exists():
            rollback_temp.unlink()


def _patch_journal_path(record_path: Path) -> Path:
    return record_path.with_name(f"{record_path.name}.transaction.json")


def _begin_patch_transaction(
    *,
    root: Path,
    record_path: Path,
    previous_head: str,
    targets: list[str],
    before_hashes: dict[str, str | None],
    postimage_hashes: dict[str, str],
    backup_dir: Path,
    manifest_path: Path,
    patch_path: Path,
) -> Path:
    previous_archive: Path | None = None
    previous_digest: str | None = None
    if record_path.exists():
        previous_bytes = record_path.read_bytes()
        previous_digest = hashlib.sha256(previous_bytes).hexdigest()
        previous_archive = record_path.parent / "record-history" / f"{previous_digest}.json"
        if previous_archive.exists():
            if previous_archive.read_bytes() != previous_bytes:
                raise PatchInstallError("record history digest collision")
        else:
            _atomic_bytes(previous_archive, previous_bytes)
    journal = PatchJournal(
        schema_version="forge-hermes-patch-transaction/v1",
        state="prepared",
        base_prepared=(previous_archive is None),
        root=str(root.resolve()),
        previous_head=previous_head,
        patch_commit=None,
        target_files=targets,
        target_before_sha256=before_hashes,
        target_postimage_sha256=postimage_hashes,
        backup_dir=str(backup_dir.resolve()),
        candidate_manifest_sha256=_sha256(manifest_path.resolve()),
        candidate_patch_sha256=_sha256(patch_path.resolve()),
        candidate_record_sha256=None,
        previous_record_archive=(
            str(previous_archive.resolve()) if previous_archive is not None else None
        ),
        previous_record_sha256=previous_digest,
    )
    journal_path = _patch_journal_path(record_path)
    _atomic_json(journal_path, asdict(journal))
    return journal_path


def _mark_patch_committed(journal_path: Path, patch_commit: str) -> None:
    raw = json.loads(journal_path.read_text(encoding="utf-8"))
    journal = PatchJournal(**raw)
    if journal.state != "prepared" or not journal.base_prepared:
        raise PatchInstallError("patch transaction is not prepared for commit")
    _atomic_json(
        journal_path,
        asdict(replace(journal, state="committed", patch_commit=patch_commit)),
    )


def _mark_base_prepared(journal_path: Path) -> None:
    raw = json.loads(journal_path.read_text(encoding="utf-8"))
    journal = PatchJournal(**raw)
    if journal.state != "prepared":
        raise PatchInstallError("only a prepared transaction can mark its base ready")
    _atomic_json(
        journal_path,
        asdict(replace(journal, base_prepared=True, patch_commit=None)),
    )


def _mark_patch_installed(journal_path: Path, record_path: Path) -> None:
    raw = json.loads(journal_path.read_text(encoding="utf-8"))
    journal = PatchJournal(**raw)
    if journal.state != "committed" or journal.patch_commit is None:
        raise PatchInstallError("patch transaction is not committed")
    record_bytes = record_path.read_bytes()
    record_data = json.loads(record_bytes)
    if not isinstance(record_data, dict) or set(record_data) != {
        field.name for field in fields(InstallRecord)
    }:
        raise PatchInstallError("candidate install record key set mismatch")
    record = InstallRecord(**record_data)
    if (
        record.root != journal.root
        or record.patch_commit != journal.patch_commit
        or record.manifest_sha256 != journal.candidate_manifest_sha256
        or record.patch_sha256 != journal.candidate_patch_sha256
    ):
        raise PatchInstallError("candidate install record does not match transaction")
    _atomic_json(
        journal_path,
        asdict(
            replace(
                journal,
                state="installed",
                candidate_record_sha256=hashlib.sha256(record_bytes).hexdigest(),
            )
        ),
    )


def _finish_patch_transaction(journal_path: Path) -> None:
    journal_bytes = journal_path.read_bytes()
    try:
        _unlink_path_durable(journal_path)
    except BaseException:
        _atomic_bytes(journal_path, journal_bytes)
        raise


def _backup_targets(root: Path, targets: list[str], backup_dir: Path) -> dict[str, str | None]:
    if backup_dir.exists():
        raise PatchInstallError(f"target backup directory already exists: {backup_dir}")
    staging_dir = backup_dir.with_name(
        f".{backup_dir.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    _ensure_directory_durable(staging_dir.parent)
    staging_dir.mkdir(parents=True, exist_ok=False)
    created_directories = {staging_dir}
    hashes: dict[str, str | None] = {}
    try:
        for relative in targets:
            source = root / relative
            destination = staging_dir / relative
            if source.exists():
                _ensure_directory_durable(destination.parent)
                created_directories.add(destination.parent)
                shutil.copy2(source, destination)
                with destination.open("r+b") as handle:
                    handle.flush()
                    os.fsync(handle.fileno())
                hashes[relative] = _sha256(source)
            else:
                hashes[relative] = None
        for directory in sorted(
            created_directories,
            key=lambda value: len(value.parts),
            reverse=True,
        ):
            _fsync_directory(directory)
        _replace_path(staging_dir, backup_dir)
        _fsync_directory(backup_dir.parent)
        return hashes
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)


def _verify_target_backups(
    targets: list[str],
    backup_dir: Path,
    before: dict[str, str | None],
) -> None:
    if set(targets) != set(before):
        raise PatchInstallError("target backup map differs from target allowlist")
    for relative in targets:
        expected = before[relative]
        source = backup_dir / relative
        if expected is None:
            if source.exists():
                raise PatchInstallError(f"unexpected backup for absent target: {relative}")
        elif not source.is_file() or _sha256(source) != expected:
            raise PatchInstallError(f"target backup SHA-256 mismatch: {relative}")


def _restore_targets(
    root: Path,
    targets: list[str],
    backup_dir: Path,
    before: dict[str, str | None],
) -> None:
    _verify_target_backups(targets, backup_dir, before)
    changed_directories: set[Path] = set()
    for relative in targets:
        destination = root / relative
        if before[relative] is None:
            if destination.exists():
                _unlink_path_durable(destination)
                changed_directories.add(destination.parent)
            continue
        source = backup_dir / relative
        _ensure_directory_durable(destination.parent)
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.restore")
        shutil.copy2(source, temporary)
        with temporary.open("r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        _replace_path(temporary, destination)
        changed_directories.add(destination.parent)
    for directory in sorted(
        changed_directories,
        key=lambda value: len(value.parts),
        reverse=True,
    ):
        _fsync_directory(directory)


def _targeted_tests(
    root: Path,
    *,
    rollback: bool,
    test_python: Path | None,
) -> None:
    python = (
        test_python.resolve()
        if test_python is not None
        else root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    if not python.is_file():
        raise PatchInstallError(f"test interpreter does not exist: {python}")
    tests = (
        ["tests/hermes_cli/test_kanban_db.py", "tests/hermes_cli/test_kanban_blocked_sticky.py"]
        if rollback
        else [
            "tests/hermes_cli/test_kanban_completion_policy.py",
            "tests/hermes_cli/test_kanban_db.py",
            "tests/hermes_cli/test_kanban_blocked_sticky.py",
        ]
    )
    _run(root, str(python), "-m", "pytest", *tests, "-q")


def _reset_head_and_restore_backups(
    root: Path,
    *,
    from_head: str,
    to_head: str,
    targets: list[str],
    backup_dir: Path,
    before_hashes: dict[str, str | None],
) -> None:
    _verify_target_backups(targets, backup_dir, before_hashes)
    if _git(root, "rev-parse", "HEAD") != from_head:
        raise PatchInstallError("transaction HEAD changed unexpectedly")
    if _git(root, "rev-parse", f"{from_head}^") != to_head:
        raise PatchInstallError("patch commit is not a direct child of previous HEAD")
    changed = sorted(
        line
        for line in _run(
            root, "git", "diff-tree", "--no-commit-id", "--name-only", "-r", from_head
        ).stdout.splitlines()
        if line
    )
    if changed != sorted(targets):
        raise PatchInstallError("patch commit paths differ from target allowlist")
    _run(root, "git", "reset", "--soft", to_head)
    _run(root, "git", "reset", "--mixed", "HEAD", "--", *targets)
    _restore_targets(root, targets, backup_dir, before_hashes)


def _restore_installed_record(root: Path, record: InstallRecord) -> None:
    _run(root, "git", "reset", "--soft", record.patch_commit)
    _run(root, "git", "reset", "--mixed", "HEAD", "--", *record.target_files)
    _run(
        root,
        "git",
        "restore",
        "--source",
        record.patch_commit,
        "--worktree",
        "--",
        *record.target_files,
    )
    restored_directories: set[Path] = set()
    for relative, expected in record.target_postimage_sha256.items():
        target = root / relative
        if not target.is_file() or _sha256(target) != expected:
            raise PatchInstallError(f"restored installed postimage mismatch: {relative}")
        with target.open("r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        restored_directories.add(target.parent)
    for directory in sorted(
        restored_directories,
        key=lambda value: len(value.parts),
        reverse=True,
    ):
        _fsync_directory(directory)


def recover_patch_transaction(*, root: Path, record_path: Path) -> None:
    journal_path = _patch_journal_path(record_path)
    if not journal_path.exists():
        return
    raw = json.loads(journal_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != {field.name for field in fields(PatchJournal)}:
        raise PatchInstallError("patch transaction journal key set mismatch")
    journal = PatchJournal(**raw)
    if journal.state not in {"prepared", "committed", "installed"}:
        raise PatchInstallError("invalid patch transaction state")
    if journal.state == "prepared" and (
        journal.patch_commit is not None or journal.candidate_record_sha256 is not None
    ):
        raise PatchInstallError("prepared transaction has committed fields")
    if journal.state == "committed" and (
        not journal.base_prepared
        or journal.patch_commit is None
        or journal.candidate_record_sha256 is not None
    ):
        raise PatchInstallError("committed transaction fields are inconsistent")
    if journal.state == "installed" and (
        not journal.base_prepared
        or journal.patch_commit is None
        or journal.candidate_record_sha256 is None
    ):
        raise PatchInstallError("installed transaction fields are inconsistent")
    if journal.root != str(root.resolve()):
        raise PatchInstallError("patch transaction root mismatch")
    backup_dir = Path(journal.backup_dir).resolve()
    _verify_target_backups(
        journal.target_files,
        backup_dir,
        dict(journal.target_before_sha256),
    )
    archived_record: InstallRecord | None = None
    if journal.previous_record_archive is not None:
        archive = Path(journal.previous_record_archive).resolve()
        archive_bytes = archive.read_bytes()
        if hashlib.sha256(archive_bytes).hexdigest() != journal.previous_record_sha256:
            raise PatchInstallError("previous install record archive digest mismatch")
        archived_record = InstallRecord(**json.loads(archive_bytes))
        if archived_record.previous_head != journal.previous_head:
            raise PatchInstallError("upgrade journal previous patch base mismatch")
    current_head = _git(root, "rev-parse", "HEAD")
    if journal.state == "installed":
        if current_head != journal.patch_commit:
            raise PatchInstallError("installed journal patch commit is not current HEAD")
        active_bytes = record_path.read_bytes()
        if hashlib.sha256(active_bytes).hexdigest() != journal.candidate_record_sha256:
            raise PatchInstallError("installed journal active record digest mismatch")
        active_record = _verify_install_record_self_contained(
            root=root,
            record_path=record_path,
        )
        if (
            active_record.manifest_sha256 != journal.candidate_manifest_sha256
            or active_record.patch_sha256 != journal.candidate_patch_sha256
        ):
            raise PatchInstallError("installed journal candidate digest mismatch")
        _finish_patch_transaction(journal_path)
        return
    if (
        journal.state == "prepared"
        and archived_record is not None
        and current_head == archived_record.patch_commit
    ):
        active_record = _verify_install_record_self_contained(
            root=root,
            record_path=record_path,
        )
        if active_record != archived_record:
            raise PatchInstallError("prepared transaction active record changed")
        _finish_patch_transaction(journal_path)
        return
    if current_head != journal.previous_head:
        if journal.state == "committed" and current_head != journal.patch_commit:
            raise PatchInstallError("journal patch commit is not current HEAD")
        for relative, expected in journal.target_postimage_sha256.items():
            target = root / relative
            if not target.is_file() or _sha256(target) != expected:
                raise PatchInstallError(f"journal postimage mismatch: {relative}")
        _reset_head_and_restore_backups(
            root,
            from_head=current_head,
            to_head=journal.previous_head,
            targets=journal.target_files,
            backup_dir=backup_dir,
            before_hashes=dict(journal.target_before_sha256),
        )
    else:
        _run(root, "git", "reset", "--mixed", "HEAD", "--", *journal.target_files)
        _restore_targets(
            root,
            journal.target_files,
            backup_dir,
            dict(journal.target_before_sha256),
        )
    if journal.previous_record_archive is None:
        _unlink_path_durable(record_path)
    else:
        if archived_record is None:
            raise PatchInstallError("previous install record archive was not parsed")
        _restore_installed_record(root, archived_record)
        _atomic_bytes(record_path, Path(journal.previous_record_archive).read_bytes())
    _finish_patch_transaction(journal_path)


def recover_patch(
    *,
    root: Path,
    manifest_path: Path,
    record_path: Path,
) -> str:
    recover_patch_transaction(root=root.resolve(), record_path=record_path.resolve())
    return patch_status(
        root=root,
        manifest_path=manifest_path,
        record_path=record_path,
    )


def _install_fresh(
    *,
    root: Path,
    patch_path: Path,
    manifest_path: Path,
    record_path: Path,
    run_tests: bool = True,
    test_python: Path | None = None,
    prepared_journal_path: Path | None = None,
    prepared_backup_dir: Path | None = None,
    prepared_before_hashes: dict[str, str | None] | None = None,
) -> InstallRecord:
    checked = check_patch(root=root, patch_path=patch_path, manifest_path=manifest_path)
    manifest = checked.manifest
    targets = [str(path) for path in manifest["target_files"]]
    before_status = _status(root)
    previous_head = _git(root, "rev-parse", "HEAD")
    prepared = (
        prepared_journal_path,
        prepared_backup_dir,
        prepared_before_hashes,
    )
    if all(value is None for value in prepared):
        backup_dir = record_path.parent / f"{record_path.stem}-targets-{time.time_ns()}"
        before_hashes = _backup_targets(root, targets, backup_dir)
        journal_path = _begin_patch_transaction(
            root=root,
            record_path=record_path,
            previous_head=previous_head,
            targets=targets,
            before_hashes=before_hashes,
            postimage_hashes=dict(checked.variant["target_postimage_sha256"]),
            backup_dir=backup_dir,
            manifest_path=manifest_path,
            patch_path=patch_path,
        )
    elif all(value is not None for value in prepared):
        journal_path = prepared_journal_path
        backup_dir = prepared_backup_dir
        before_hashes = prepared_before_hashes
        if journal_path is None or backup_dir is None or before_hashes is None:
            raise AssertionError("prepared transaction narrowing failed")
        journal = PatchJournal(**json.loads(journal_path.read_text(encoding="utf-8")))
        if (
            journal.state != "prepared"
            or not journal.base_prepared
            or journal.previous_head != previous_head
            or journal.target_files != targets
            or journal.target_postimage_sha256
            != dict(checked.variant["target_postimage_sha256"])
        ):
            raise PatchInstallError("prepared upgrade journal mismatch")
    else:
        raise PatchInstallError("prepared transaction arguments must be all-or-none")
    patch_commit: str | None = None
    try:
        _run(root, "git", "apply", str(patch_path.resolve()))
        for relative, expected in dict(checked.variant["target_postimage_sha256"]).items():
            if _sha256(root / relative) != expected:
                raise PatchInstallError(f"postimage SHA-256 mismatch: {relative}")
        if run_tests:
            _targeted_tests(root, rollback=False, test_python=test_python)
        _run(root, "git", "add", "--", *targets)
        staged = sorted(
            line
            for line in _run(root, "git", "diff", "--cached", "--name-only").stdout.splitlines()
            if line
        )
        if staged != sorted(targets):
            raise PatchInstallError("staged paths differ from target allowlist")
        _run(
            root,
            "git",
            "-c",
            "user.name=InfinityForge",
            "-c",
            "user.email=infinity-forge@invalid",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "--only",
            "-m",
            "forge: enforce Hermes completion policy",
            "--",
            *targets,
        )
        patch_commit = _git(root, "rev-parse", "HEAD")
        _mark_patch_committed(journal_path, patch_commit)
        if _status(root) != before_status:
            raise PatchInstallError("unrelated worktree state changed during install")
        record = InstallRecord(
            schema_version="forge-hermes-install-record/v1",
            root=str(root.resolve()),
            base_blob=checked.base_blob,
            previous_head=previous_head,
            patch_commit=patch_commit,
            manifest_sha256=_sha256(manifest_path.resolve()),
            patch_sha256=str(manifest["patch_sha256"]),
            target_files=targets,
            target_before_sha256=before_hashes,
            target_postimage_sha256=dict(checked.variant["target_postimage_sha256"]),
            backup_dir=str(backup_dir.resolve()),
            installed_at=int(time.time()),
        )
        _atomic_json(record_path.resolve(), asdict(record))
        _mark_patch_installed(journal_path, record_path.resolve())
        _finish_patch_transaction(journal_path)
        return record
    except BaseException:
        if journal_path.exists():
            terminal = json.loads(journal_path.read_text(encoding="utf-8"))
            if isinstance(terminal, dict) and terminal.get("state") == "installed":
                raise
        _run(root, "git", "restore", "--staged", "--", *targets, check=False)
        if patch_commit is not None:
            _reset_head_and_restore_backups(
                root,
                from_head=patch_commit,
                to_head=previous_head,
                targets=targets,
                backup_dir=backup_dir,
                before_hashes=before_hashes,
            )
        else:
            _restore_targets(root, targets, backup_dir, before_hashes)
        if _git(root, "rev-parse", "HEAD") != previous_head:
            raise PatchInstallError("failed install did not restore previous HEAD")
        recover_patch_transaction(root=root, record_path=record_path)
        if _status(root) != before_status:
            raise PatchInstallError("failed install did not restore worktree state")
        raise


def install_patch(
    *,
    root: Path,
    patch_path: Path,
    manifest_path: Path,
    record_path: Path,
    run_tests: bool = True,
    test_python: Path | None = None,
) -> InstallRecord:
    root = root.resolve()
    record_path = record_path.resolve()
    recover_patch_transaction(root=root, record_path=record_path)
    candidate_manifest = _load_manifest(manifest_path.resolve())
    _assert_approved_base(root, str(candidate_manifest["upstream_base"]))
    if not record_path.exists():
        return _install_fresh(
            root=root,
            patch_path=patch_path,
            manifest_path=manifest_path,
            record_path=record_path,
            run_tests=run_tests,
            test_python=test_python,
        )
    installed = _verify_install_record_self_contained(root=root, record_path=record_path)
    candidate_manifest_sha = _sha256(manifest_path.resolve())
    candidate_patch_sha = _sha256(patch_path.resolve())
    if (
        installed.manifest_sha256 == candidate_manifest_sha
        and installed.patch_sha256 == candidate_patch_sha
    ):
        return _verify_install_record_and_targets(
            root=root,
            manifest_path=manifest_path,
            record_path=record_path,
        )

    installed_status = _status(root)
    candidate_variants = candidate_manifest["variants"]
    if installed.base_blob not in candidate_variants:
        raise PatchInstallError("candidate does not support installed base blob")
    candidate_variant = candidate_variants[installed.base_blob]
    if [str(value) for value in candidate_manifest["target_files"]] != installed.target_files:
        raise PatchInstallError("upgrade target allowlist mismatch")
    journal_path = _begin_patch_transaction(
        root=root,
        record_path=record_path,
        previous_head=installed.previous_head,
        targets=installed.target_files,
        before_hashes=dict(installed.target_before_sha256),
        postimage_hashes=dict(candidate_variant["target_postimage_sha256"]),
        backup_dir=Path(installed.backup_dir),
        manifest_path=manifest_path,
        patch_path=patch_path,
    )
    try:
        _reset_head_and_restore_backups(
            root,
            from_head=installed.patch_commit,
            to_head=installed.previous_head,
            targets=installed.target_files,
            backup_dir=Path(installed.backup_dir),
            before_hashes=dict(installed.target_before_sha256),
        )
        _mark_base_prepared(journal_path)
        return _install_fresh(
            root=root,
            patch_path=patch_path,
            manifest_path=manifest_path,
            record_path=record_path,
            run_tests=run_tests,
            test_python=test_python,
            prepared_journal_path=journal_path,
            prepared_backup_dir=Path(installed.backup_dir),
            prepared_before_hashes=dict(installed.target_before_sha256),
        )
    except BaseException:
        if journal_path.exists():
            terminal = json.loads(journal_path.read_text(encoding="utf-8"))
            if isinstance(terminal, dict) and terminal.get("state") == "installed":
                raise
        recover_patch_transaction(root=root, record_path=record_path)
        if _git(root, "rev-parse", "HEAD") != installed.patch_commit:
            raise PatchInstallError("failed upgrade did not restore old patch HEAD")
        if _status(root) != installed_status:
            raise PatchInstallError("failed upgrade did not restore installed state")
        raise


def _verify_guard_current(
    *,
    current_manifest_path: Path,
    expected_source_sha: str,
) -> None:
    try:
        raw = json.loads(current_manifest_path.resolve().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PatchInstallError(f"invalid guard current manifest: {current_manifest_path}") from exc
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "policies"}:
        raise PatchInstallError("guard current top-level fields do not match schema")
    if raw["schema_version"] != "forge-completion-manifest/v1":
        raise PatchInstallError("unsupported guard current schema")
    policies = raw["policies"]
    if not isinstance(policies, dict) or set(policies) != {"forge-v1"}:
        raise PatchInstallError("guard current must contain only forge-v1")
    policy = policies["forge-v1"]
    required = {"python", "artifact", "artifact_sha256", "timeout_seconds"}
    if not isinstance(policy, dict) or set(policy) != required:
        raise PatchInstallError("forge-v1 policy fields do not match schema")
    if not isinstance(policy["python"], str):
        raise PatchInstallError("guard policy python must be a string")
    if not isinstance(policy["artifact"], str):
        raise PatchInstallError("guard policy artifact must be a string")
    artifact_sha256 = policy["artifact_sha256"]
    if (
        not isinstance(artifact_sha256, str)
        or len(artifact_sha256) != 64
        or any(character not in "0123456789abcdef" for character in artifact_sha256)
    ):
        raise PatchInstallError("guard artifact_sha256 must be 64 lowercase hex characters")
    timeout_seconds = policy["timeout_seconds"]
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 900:
        raise PatchInstallError("guard timeout_seconds must be an integer between 1 and 900")
    if (
        len(expected_source_sha) != 40
        or any(character not in "0123456789abcdef" for character in expected_source_sha)
    ):
        raise PatchInstallError("expected_source_sha must be 40 lowercase hex characters")
    interpreter = Path(policy["python"])
    artifact = Path(policy["artifact"])
    if not interpreter.is_absolute() or not interpreter.is_file():
        raise PatchInstallError("guard policy python must be an existing absolute file")
    if not artifact.is_absolute() or not artifact.is_file():
        raise PatchInstallError("guard policy artifact must be an existing absolute file")
    if artifact.parent.name != expected_source_sha:
        raise PatchInstallError("guard artifact release directory does not match source SHA")
    if _sha256(artifact) != artifact_sha256:
        raise PatchInstallError("guard artifact SHA-256 mismatch")


def _verify_install_record_self_contained(
    *,
    root: Path,
    record_path: Path,
) -> InstallRecord:
    root = root.resolve()
    record_data = json.loads(record_path.resolve().read_text(encoding="utf-8"))
    if not isinstance(record_data, dict) or set(record_data) != {
        field.name for field in fields(InstallRecord)
    }:
        raise PatchInstallError("install record key set mismatch")
    record = InstallRecord(**record_data)
    if record.root != str(root):
        raise PatchInstallError("install record root mismatch")
    if set(record.target_files) != set(record.target_before_sha256) or set(
        record.target_files
    ) != set(record.target_postimage_sha256):
        raise PatchInstallError("install record target maps mismatch")
    if _git(root, "rev-parse", "HEAD") != record.patch_commit:
        raise PatchInstallError("installed patch commit is not current HEAD")
    if _git(root, "rev-parse", f"{record.patch_commit}^") != record.previous_head:
        raise PatchInstallError("install record previous HEAD mismatch")
    changed = sorted(
        line
        for line in _run(
            root,
            "git",
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            record.patch_commit,
        ).stdout.splitlines()
        if line
    )
    if changed != sorted(record.target_files):
        raise PatchInstallError("install commit paths differ from target allowlist")
    backup_dir = Path(record.backup_dir).resolve()
    if backup_dir.parent != record_path.resolve().parent:
        raise PatchInstallError("install record backup directory escapes state root")
    for relative, expected in record.target_before_sha256.items():
        backup = backup_dir / relative
        if expected is None:
            if backup.exists():
                raise PatchInstallError(f"unexpected backup for absent target: {relative}")
        elif not backup.is_file() or _sha256(backup) != expected:
            raise PatchInstallError(f"target backup SHA-256 mismatch: {relative}")
    _assert_no_staged_changes(root)
    _assert_targets_clean(root, record.target_files)
    for relative, expected in record.target_postimage_sha256.items():
        target = root / relative
        if not target.is_file() or _sha256(target) != expected:
            raise PatchInstallError(f"installed postimage SHA-256 mismatch: {relative}")
    return record


def _verify_install_record_and_targets(
    *,
    root: Path,
    manifest_path: Path,
    record_path: Path,
) -> InstallRecord:
    manifest_path = manifest_path.resolve()
    record = _verify_install_record_self_contained(
        root=root,
        record_path=record_path,
    )
    manifest = _load_manifest(manifest_path)
    if record.manifest_sha256 != _sha256(manifest_path):
        raise PatchInstallError("install record manifest SHA-256 mismatch")
    if record.patch_sha256 != manifest.get("patch_sha256"):
        raise PatchInstallError("install record patch SHA-256 mismatch")
    variants = manifest.get("variants")
    if not isinstance(variants, dict) or not isinstance(variants.get(record.base_blob), dict):
        raise PatchInstallError("install record variant is unsupported")
    variant = dict(variants[record.base_blob])
    expected_postimages = dict(variant["target_postimage_sha256"])
    if expected_postimages != record.target_postimage_sha256:
        raise PatchInstallError("install record postimage map mismatch")
    if [str(path) for path in manifest["target_files"]] != record.target_files:
        raise PatchInstallError("install record target list mismatch")
    return record


def _rolled_back_pointer_path(record_path: Path) -> Path:
    return record_path.with_name(f"{record_path.name}.rolled-back-pointer.json")


def _archive_successful_rollback(record_path: Path, record: InstallRecord) -> None:
    active_bytes = record_path.read_bytes()
    if InstallRecord(**json.loads(active_bytes)) != record:
        raise PatchInstallError("active install record changed during rollback")
    digest = hashlib.sha256(active_bytes).hexdigest()
    archive = record_path.parent / "rolled-back-history" / f"{digest}.json"
    if archive.exists() and archive.read_bytes() != active_bytes:
        raise PatchInstallError("rolled-back record digest collision")
    if not archive.exists():
        _atomic_bytes(archive, active_bytes)
    pointer = {
        "schema_version": "forge-hermes-rolled-back-pointer/v1",
        "archive": str(archive.resolve()),
        "record_sha256": digest,
    }
    _atomic_json(_rolled_back_pointer_path(record_path), pointer)
    try:
        _unlink_path_durable(record_path)
    except BaseException:
        _atomic_bytes(record_path, active_bytes)
        raise


def _verify_idempotent_rollback(
    *,
    root: Path,
    manifest_path: Path,
    record_path: Path,
) -> str | None:
    pointer_path = _rolled_back_pointer_path(record_path)
    if record_path.exists() or not pointer_path.exists():
        return None
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    if not isinstance(pointer, dict) or set(pointer) != {
        "schema_version",
        "archive",
        "record_sha256",
    }:
        raise PatchInstallError("rolled-back pointer key set mismatch")
    if pointer["schema_version"] != "forge-hermes-rolled-back-pointer/v1":
        raise PatchInstallError("rolled-back pointer schema mismatch")
    archive = Path(pointer["archive"]).resolve()
    archive_bytes = archive.read_bytes()
    if hashlib.sha256(archive_bytes).hexdigest() != pointer["record_sha256"]:
        raise PatchInstallError("rolled-back record archive digest mismatch")
    record = InstallRecord(**json.loads(archive_bytes))
    if record.root != str(root.resolve()):
        raise PatchInstallError("rolled-back record root mismatch")
    if record.manifest_sha256 != _sha256(manifest_path.resolve()):
        return None
    if _git(root, "rev-parse", "HEAD") != record.previous_head:
        raise PatchInstallError("rolled-back previous HEAD mismatch")
    _verify_target_backups(
        record.target_files,
        Path(record.backup_dir),
        dict(record.target_before_sha256),
    )
    for relative, expected in record.target_before_sha256.items():
        target = root / relative
        if expected is None:
            if target.exists():
                raise PatchInstallError(f"rolled-back target should be absent: {relative}")
        elif not target.is_file() or _sha256(target) != expected:
            raise PatchInstallError(f"rolled-back target digest mismatch: {relative}")
    return record.previous_head


def verify_patch(
    *,
    root: Path,
    manifest_path: Path,
    record_path: Path,
    current_manifest_path: Path,
    expected_source_sha: str,
) -> InstallRecord:
    _verify_guard_current(
        current_manifest_path=current_manifest_path,
        expected_source_sha=expected_source_sha,
    )
    return _verify_install_record_and_targets(
        root=root,
        manifest_path=manifest_path,
        record_path=record_path,
    )


def rollback_patch(
    *,
    root: Path,
    patch_path: Path,
    manifest_path: Path,
    record_path: Path,
    run_tests: bool = True,
    test_python: Path | None = None,
) -> str:
    recover_patch_transaction(root=root.resolve(), record_path=record_path.resolve())
    idempotent = _verify_idempotent_rollback(
        root=root,
        manifest_path=manifest_path,
        record_path=record_path,
    )
    if idempotent is not None:
        return idempotent
    record = _verify_install_record_and_targets(
        root=root,
        manifest_path=manifest_path,
        record_path=record_path,
    )
    before_status = _status(root)
    journal_path = _begin_patch_transaction(
        root=root,
        record_path=record_path.resolve(),
        previous_head=record.previous_head,
        targets=record.target_files,
        before_hashes=dict(record.target_before_sha256),
        postimage_hashes=dict(record.target_postimage_sha256),
        backup_dir=Path(record.backup_dir),
        manifest_path=manifest_path,
        patch_path=patch_path,
    )
    try:
        _reset_head_and_restore_backups(
            root,
            from_head=record.patch_commit,
            to_head=record.previous_head,
            targets=record.target_files,
            backup_dir=Path(record.backup_dir),
            before_hashes=dict(record.target_before_sha256),
        )
        _mark_base_prepared(journal_path)
        if run_tests:
            _targeted_tests(root, rollback=True, test_python=test_python)
        for relative, expected in record.target_before_sha256.items():
            target = root / relative
            if expected is None:
                if target.exists():
                    raise PatchInstallError(f"rollback target should be absent: {relative}")
            elif not target.is_file() or _sha256(target) != expected:
                raise PatchInstallError(f"rollback preimage SHA-256 mismatch: {relative}")
        if _status(root) != before_status:
            raise PatchInstallError("unrelated worktree state changed during rollback")
        _archive_successful_rollback(record_path.resolve(), record)
        _finish_patch_transaction(journal_path)
        return record.previous_head
    except BaseException:
        recover_patch_transaction(root=root.resolve(), record_path=record_path.resolve())
        if _git(root, "rev-parse", "HEAD") != record.patch_commit:
            raise PatchInstallError("failed rollback did not restore patch HEAD")
        if _status(root) != before_status:
            raise PatchInstallError("failed rollback did not restore installed state")
        raise
```

- [ ] **Step 4: deterministic artifact builder와 CLI를 최소 구현한다**

`forge/ops/hermes_patch.py`에 다음 builder를 추가한다.

```python
def build_artifact(
    *,
    source_root: Path,
    variant_roots: dict[str, Path],
    patch_path: Path,
    manifest_path: Path,
) -> dict[str, object]:
    if _git(source_root, "rev-parse", f"{UPSTREAM_BASE}^{{commit}}") != UPSTREAM_BASE:
        raise PatchInstallError("pinned upstream commit is unavailable")
    if set(variant_roots) != REQUIRED_VARIANT_BLOBS:
        raise PatchInstallError("variant roots must match both supported base blobs")
    diff = _run(
        source_root,
        "git",
        "diff",
        "--binary",
        UPSTREAM_BASE,
        "HEAD",
        "--",
        *TARGET_FILES,
    ).stdout
    if not diff:
        raise PatchInstallError("patch diff is empty")
    _ensure_directory_durable(patch_path.parent)
    temporary_patch = patch_path.with_name(f".{patch_path.name}.{os.getpid()}.tmp")
    temporary_patch.write_text(diff, encoding="utf-8", newline="\n")
    with temporary_patch.open("r+b") as handle:
        handle.flush()
        os.fsync(handle.fileno())
    _replace_path(temporary_patch, patch_path)
    _fsync_directory(patch_path.parent)
    variants: dict[str, object] = {}
    for expected_blob, raw_root in sorted(variant_roots.items()):
        variant_root = raw_root.resolve()
        _assert_approved_base(variant_root, UPSTREAM_BASE)
        actual_blob = _git(variant_root, "rev-parse", "HEAD:hermes_cli/kanban_db.py")
        if actual_blob != expected_blob:
            raise PatchInstallError(f"variant base blob mismatch: {expected_blob}")
        actual_preimages = {
            name: source_member_sha256(variant_root / "hermes_cli" / "kanban_db.py", name)
            for name in AST_PREIMAGES
        }
        if actual_preimages != AST_PREIMAGES:
            raise PatchInstallError(f"variant AST preimage mismatch: {expected_blob}")
        _assert_targets_clean(variant_root, TARGET_FILES)
        before_status = _status(variant_root)
        target_preimages = {
            relative: _sha256(variant_root / relative)
            if (variant_root / relative).is_file()
            else None
            for relative in TARGET_FILES
        }
        _run(variant_root, "git", "apply", "--check", str(patch_path.resolve()))
        _run(variant_root, "git", "apply", str(patch_path.resolve()))
        target_postimages = {
            relative: _sha256(variant_root / relative)
            for relative in TARGET_FILES
        }
        _run(variant_root, "git", "apply", "-R", str(patch_path.resolve()))
        if _status(variant_root) != before_status:
            raise PatchInstallError(f"variant rehearsal changed worktree state: {expected_blob}")
        variants[expected_blob] = {
            "ast_preimages": actual_preimages,
            "target_preimage_sha256": target_preimages,
            "target_postimage_sha256": target_postimages,
        }
    manifest: dict[str, object] = {
        "schema_version": "forge-hermes-patch/v1",
        "hermes_version": "0.18.2",
        "upstream_base": UPSTREAM_BASE,
        "target_files": TARGET_FILES,
        "patch_sha256": _sha256(patch_path),
        "variants": variants,
    }
    _atomic_json(manifest_path, manifest)
    return manifest
```

`forge/ops/hermes_patch.py`에 다음 CLI implementation을 추가한다.

```python
def _parse_variant_roots(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        blob, separator, raw_path = value.partition("=")
        if not separator or len(blob) != 40 or not raw_path:
            raise PatchInstallError("--variant-root must be BLOB=PATH")
        if blob in result:
            raise PatchInstallError(f"duplicate --variant-root: {blob}")
        result[blob] = Path(raw_path).resolve()
    return result


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="hermes-patch.py")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build")
    build.add_argument("--source-root", type=Path, required=True)
    build.add_argument("--variant-root", action="append", required=True)
    build.add_argument("--patch", type=Path, required=True)
    build.add_argument("--manifest", type=Path, required=True)

    status = subparsers.add_parser("status")
    status.add_argument("--root", type=Path, required=True)
    status.add_argument("--manifest", type=Path, required=True)
    status.add_argument("--record", type=Path, required=True)

    recover = subparsers.add_parser("recover")
    recover.add_argument("--root", type=Path, required=True)
    recover.add_argument("--manifest", type=Path, required=True)
    recover.add_argument("--record", type=Path, required=True)

    check = subparsers.add_parser("check")
    check.add_argument("--root", type=Path, required=True)
    check.add_argument("--patch", type=Path, required=True)
    check.add_argument("--manifest", type=Path, required=True)

    install = subparsers.add_parser("install")
    install.add_argument("--root", type=Path, required=True)
    install.add_argument("--patch", type=Path, required=True)
    install.add_argument("--manifest", type=Path, required=True)
    install.add_argument("--record", type=Path, required=True)
    install.add_argument("--test-python", type=Path, default=None)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--root", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--record", type=Path, required=True)
    verify.add_argument("--current-manifest", type=Path, required=True)
    verify.add_argument("--expected-source-sha", required=True)

    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--root", type=Path, required=True)
    rollback.add_argument("--patch", type=Path, required=True)
    rollback.add_argument("--manifest", type=Path, required=True)
    rollback.add_argument("--record", type=Path, required=True)
    rollback.add_argument("--test-python", type=Path, default=None)

    args = parser.parse_args(argv)
    if args.command == "build":
        result = build_artifact(
            source_root=args.source_root,
            variant_roots=_parse_variant_roots(args.variant_root),
            patch_path=args.patch,
            manifest_path=args.manifest,
        )
    elif args.command == "status":
        result = {
            "status": patch_status(
                root=args.root,
                manifest_path=args.manifest,
                record_path=args.record,
            )
        }
    elif args.command == "recover":
        result = {
            "status": recover_patch(
                root=args.root,
                manifest_path=args.manifest,
                record_path=args.record,
            )
        }
    elif args.command == "check":
        result = asdict(
            check_patch(
                root=args.root,
                patch_path=args.patch,
                manifest_path=args.manifest,
            )
        )
    elif args.command == "install":
        result = asdict(
            install_patch(
                root=args.root,
                patch_path=args.patch,
                manifest_path=args.manifest,
                record_path=args.record,
                test_python=args.test_python,
            )
        )
    elif args.command == "verify":
        result = asdict(
            verify_patch(
                root=args.root,
                manifest_path=args.manifest,
                record_path=args.record,
                current_manifest_path=args.current_manifest,
                expected_source_sha=args.expected_source_sha,
            )
        )
    else:
        result = {
            "previous_head": rollback_patch(
                root=args.root,
                patch_path=args.patch,
                manifest_path=args.manifest,
                record_path=args.record,
                test_python=args.test_python,
            )
        }
    print(json.dumps(result, sort_keys=True))
    return 0
```

`forge/scripts/hermes-patch.py`는 다음 thin entrypoint로 만든다.

```python
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from forge.ops.hermes_patch import main


if __name__ == "__main__":
    raise SystemExit(main())
```

`main()`의 exact CLI는 다음이다.

```text
hermes-patch.py build --source-root PATH --variant-root 518e74eb0647786a0361105b76bfbaeb1bad3e19=WINDOWS_ROOT --variant-root 6150b141537b947a2a89d19b13be4fbad2330711=VPS_ROOT --patch PATH --manifest PATH
hermes-patch.py status --root PATH --manifest PATH --record PATH
hermes-patch.py recover --root PATH --manifest PATH --record PATH
hermes-patch.py check --root PATH --patch PATH --manifest PATH
hermes-patch.py install --root PATH --patch PATH --manifest PATH --record PATH [--test-python PYTHON]
hermes-patch.py verify --root ROOT --manifest MANIFEST --record RECORD --current-manifest CURRENT --expected-source-sha SHA
hermes-patch.py rollback --root PATH --patch PATH --manifest PATH --record PATH [--test-python PYTHON]
```

- [ ] **Step 5: Task 4 patch worktree에서 실제 artifact를 생성한다**

```powershell
Set-Location C:\01.project\INFINITY_FORGE
$HermesInstall = "$env:LOCALAPPDATA\hermes\hermes-agent"
$ForgePython = "$HermesInstall\venv\Scripts\python.exe"
$Rehearsal = Join-Path $env:TEMP ("forge-hermes-patch-rehearsal-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $Rehearsal | Out-Null
$HermesRepo = "https://github.com/NousResearch/hermes-agent.git"
$ApprovedCommit = "4281151ae859241351ba14d8c7682dc67ff4c126"
$WindowsHead = "540f90190f50f9518bf36632a724e0e58877a10b"
$VpsHead = "73b611ad19720d70308dad6b0fb64648aaadc216"
$HermesTargets = @(
  "hermes_cli/kanban_completion_policy.py",
  "hermes_cli/kanban_db.py",
  "hermes_cli/kanban.py",
  "plugins/kanban/dashboard/plugin_api.py",
  "tests/hermes_cli/test_kanban_completion_policy.py"
)
$LiveWindowsHead = (git -C $HermesInstall rev-parse HEAD).Trim()
$LiveWindowsBlob = (git -C $HermesInstall rev-parse HEAD:hermes_cli/kanban_db.py).Trim()
$LiveWindowsDirty = & git -C $HermesInstall --no-optional-locks status --porcelain=v1 -- @HermesTargets
if ($LiveWindowsHead -ne $WindowsHead -or $LiveWindowsBlob -ne "518e74eb0647786a0361105b76bfbaeb1bad3e19" -or $LiveWindowsDirty) {
  throw "live Windows read-only HEAD/blob/target-clean facts mismatch"
}
git clone --filter=blob:none --no-checkout $HermesRepo "$Rehearsal\upstream"
git -C "$Rehearsal\upstream" config core.longpaths true
git -C "$Rehearsal\upstream" fetch --depth 1 origin $ApprovedCommit
git -C "$Rehearsal\upstream" checkout --detach FETCH_HEAD
git clone --filter=blob:none --no-checkout $HermesRepo "$Rehearsal\windows"
git -C "$Rehearsal\windows" config core.longpaths true
git -C "$Rehearsal\windows" fetch --depth 1 origin $WindowsHead
git -C "$Rehearsal\windows" checkout --detach FETCH_HEAD
git -C "$Rehearsal\windows" fetch --depth 1 origin $ApprovedCommit
if ((git -C "$Rehearsal\windows" rev-parse HEAD).Trim() -ne $WindowsHead) {
  throw "Windows disposable checkout HEAD mismatch"
}
$LiveVpsHead = (ssh ubuntu@51.222.27.48 "git -C /home/ubuntu/.hermes/hermes-agent rev-parse HEAD").Trim()
$LiveVpsBlob = (ssh ubuntu@51.222.27.48 "git -C /home/ubuntu/.hermes/hermes-agent rev-parse HEAD:hermes_cli/kanban_db.py").Trim()
$LiveVpsDirty = ssh ubuntu@51.222.27.48 "git -C /home/ubuntu/.hermes/hermes-agent --no-optional-locks status --porcelain=v1 -- hermes_cli/kanban_completion_policy.py hermes_cli/kanban_db.py hermes_cli/kanban.py plugins/kanban/dashboard/plugin_api.py tests/hermes_cli/test_kanban_completion_policy.py"
if ($LiveVpsHead -ne $VpsHead -or $LiveVpsBlob -ne "6150b141537b947a2a89d19b13be4fbad2330711" -or $LiveVpsDirty) {
  throw "live VPS read-only HEAD/blob/clean facts mismatch"
}
git clone --filter=blob:none --no-checkout $HermesRepo "$Rehearsal\vps"
git -C "$Rehearsal\vps" config core.longpaths true
git -C "$Rehearsal\vps" fetch --depth 1 origin $VpsHead
git -C "$Rehearsal\vps" checkout --detach FETCH_HEAD
if ((git -C "$Rehearsal\vps" rev-parse HEAD).Trim() -ne $VpsHead) {
  throw "VPS disposable checkout HEAD mismatch"
}
git -C "$Rehearsal\vps" fetch --depth 1 origin 4281151ae859241351ba14d8c7682dc67ff4c126
if ((git -C "$Rehearsal\vps" rev-parse HEAD).Trim() -ne $VpsHead) {
  throw "VPS disposable checkout moved while fetching approved base"
}
$ApprovedRef = "refs/infinity-forge/approved-base"
$ZeroOid = "0000000000000000000000000000000000000000"
foreach ($VariantRoot in @("$Rehearsal\windows", "$Rehearsal\vps")) {
  $ExistingApproved = (& git -C $VariantRoot rev-parse --verify $ApprovedRef 2>$null)
  if ($LASTEXITCODE -ne 0) {
    & git -C $VariantRoot update-ref $ApprovedRef $ApprovedCommit $ZeroOid
    if ($LASTEXITCODE -ne 0) { throw "failed to create immutable approved-base ref" }
  } elseif ($ExistingApproved.Trim() -ne $ApprovedCommit) {
    throw "immutable approved-base ref mismatch: $VariantRoot"
  }
}
foreach ($DisposableRoot in @("$Rehearsal\upstream", "$Rehearsal\windows", "$Rehearsal\vps")) {
  if (& git -C $DisposableRoot --no-optional-locks status --porcelain=v1) {
    throw "disposable Hermes checkout is not fully clean: $DisposableRoot"
  }
}
if ((git -C "$Rehearsal\windows" rev-parse HEAD:hermes_cli/kanban_db.py).Trim() -ne "518e74eb0647786a0361105b76bfbaeb1bad3e19") {
  throw "Windows disposable checkout kanban_db blob mismatch"
}
if (& git -C "$Rehearsal\windows" --no-optional-locks status --porcelain=v1 -- @HermesTargets) {
  throw "Windows disposable checkout target is dirty"
}
if ((git -C "$Rehearsal\vps" rev-parse HEAD:hermes_cli/kanban_db.py).Trim() -ne "6150b141537b947a2a89d19b13be4fbad2330711") {
  throw "VPS disposable checkout kanban_db blob mismatch"
}
if (& git -C "$Rehearsal\vps" --no-optional-locks status --porcelain=v1 -- @HermesTargets) {
  throw "VPS disposable checkout target is dirty"
}
& $ForgePython -X utf8 forge/scripts/hermes-patch.py build `
  --source-root C:\01.project\.worktrees\INFINITY_FORGE\hermes-completion-policy `
  --variant-root "518e74eb0647786a0361105b76bfbaeb1bad3e19=$Rehearsal\windows" `
  --variant-root "6150b141537b947a2a89d19b13be4fbad2330711=$Rehearsal\vps" `
  --patch forge/patches/hermes/0.18.2/completion-policy.patch `
  --manifest forge/patches/hermes/0.18.2/manifest.json
```

Expected: manifest의 full pins가 이 계획의 Global Constraints 2-4와 일치한다. `variants` key는 Windows/VPS blob 두 개이고, 각 variant의 7개 `ast_preimages`, 모든 `target_preimage_sha256`, 모든 `target_postimage_sha256`이 exact 값이며 두 variant의 `hermes_cli/kanban_db.py` postimage는 서로 다르다.

- [ ] **Step 6: GREEN unit tests를 실행한다**

```powershell
Set-Location C:\01.project\INFINITY_FORGE
$HermesPython = "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe"
$PytestTarget = "$env:TEMP\infinity-forge-pytest-3.11"
$env:PYTHONUTF8 = "1"
uv pip install --target $PytestTarget --python $HermesPython --upgrade pytest
$PreviousPythonPath = $env:PYTHONPATH
try {
  $env:PYTHONPATH = "$PytestTarget;$PreviousPythonPath"
  & $HermesPython -X utf8 -m pytest tests/hermes/test_hermes_patch.py -q
} finally {
  $env:PYTHONPATH = $PreviousPythonPath
}
```

Expected: Windows에서 `20 passed`다. 이 실실행은 LF exact fixture, immutable approved-base 교차 계약, writable file-descriptor `flush()`/`fsync()`, prepared→committed→installed recovery, upgrade prepare 실패 복원, `check` CLI JSON serialization을 함께 검증한다.

- [ ] **Step 7: clean upstream, Windows blob, VPS blob의 disposable rehearsal을 실행한다**

Step 5에서 만든 clean upstream, Windows variant, public-origin exact-SHA VPS disposable roots를 재사용한다. Windows installed checkout과 VPS live host는 HEAD/blob/clean metadata를 읽는 용도로만 사용하며 기존 installation과 DB는 변경하지 않는다.

```powershell
$Patch = "C:\01.project\INFINITY_FORGE\forge\patches\hermes\0.18.2\completion-policy.patch"
$Manifest = "C:\01.project\INFINITY_FORGE\forge\patches\hermes\0.18.2\manifest.json"
$Rehearsal = "$env:TEMP\forge-hermes-patch-rehearsal"
$HermesPython = "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe"
$CurrentManifest = "$env:LOCALAPPDATA\InfinityForge\guard\current.json"
$ExpectedSourceSha = (git -C C:\01.project\INFINITY_FORGE rev-parse HEAD).Trim()
```

각 root에 설치본의 venv를 directory junction 또는 symlink로 연결하지 않는다. 외부 interpreter를 `--test-python`으로 명시하되 subprocess cwd는 각 rehearsal root이므로 그 root의 patched source가 import된다. install과 rollback 모두 실제 targeted tests를 실행한다.
`$CurrentManifest`는 `$ExpectedSourceSha`로 빌드한 immutable guard release를 가리켜야 하며, 다른 release이거나 artifact digest가 달라지면 세 rehearsal 모두 patch record를 읽기 전에 FAIL해야 한다.

```powershell
foreach ($Name in @("upstream", "windows", "vps")) {
  $Root = "$Rehearsal\$Name"
  .\.venv\Scripts\python.exe forge/scripts/hermes-patch.py check --root $Root --patch $Patch --manifest $Manifest
  .\.venv\Scripts\python.exe forge/scripts/hermes-patch.py install --root $Root --patch $Patch --manifest $Manifest --record "$Rehearsal\$Name-record.json" --test-python $HermesPython
  .\.venv\Scripts\python.exe forge/scripts/hermes-patch.py verify --root $Root --manifest $Manifest --record "$Rehearsal\$Name-record.json" --current-manifest $CurrentManifest --expected-source-sha $ExpectedSourceSha
  .\.venv\Scripts\python.exe forge/scripts/hermes-patch.py rollback --root $Root --patch $Patch --manifest $Manifest --record "$Rehearsal\$Name-record.json" --test-python $HermesPython
}
```

Expected:

- upstream과 Windows의 preimage blob은 `518e74eb0647786a0361105b76bfbaeb1bad3e19`다.
- VPS preimage blob은 `6150b141537b947a2a89d19b13be4fbad2330711`다.
- 세 root 모두 install commit과 rollback commit의 changed paths가 target allowlist와 정확히 같다.
- unrelated dirty fixture를 각 root에 하나씩 만든 경우 porcelain entry가 install/rollback 전후 동일하다.
- rollback 뒤 target SHA-256이 install 전 값과 같다.

- [ ] **Step 8: rehearsal evidence를 실제 값으로 작성하고 검증한다**

`docs/weapon/evidence/hermes-completion-policy-patch-rehearsal.md`에 각 root의 base HEAD, selected variant blob, 7개 AST preimages, target preimage/postimage SHA map, patch SHA, install record, `verify` 결과, install/rollback commit, changed paths, targeted test 결과를 기록한다. token, environment secret, config contents는 기록하지 않는다.

```powershell
rg -n "4281151ae859241351ba14d8c7682dc67ff4c126|518e74eb0647786a0361105b76bfbaeb1bad3e19|6150b141537b947a2a89d19b13be4fbad2330711" docs/weapon/evidence/hermes-completion-policy-patch-rehearsal.md
git diff --check
```

Expected: 세 pinned 값이 모두 발견되고 `git diff --check` exit 0이다.

- [ ] **Step 9: Forge repository에 artifact, installer, tests와 evidence를 commit한다**

```powershell
git add forge/patches/hermes/0.18.2/completion-policy.patch forge/patches/hermes/0.18.2/manifest.json forge/ops/hermes_patch.py forge/scripts/hermes-patch.py tests/hermes/test_hermes_patch.py docs/weapon/evidence/hermes-completion-policy-patch-rehearsal.md
git commit -m "feat: package Hermes completion policy patch"
```

---

## 실행 후 통합 gate

이 하위 계획의 구현 완료는 live deployment가 아니다. 다음 소비자가 모두 존재할 때만 main plan의 Windows→Linux→VPS live install 단계로 넘긴다.

**Consumes before live install:**

- `forge-completion-manifest/v1` current manifest가 OS 고정 root에 설치돼 있다.
- manifest의 `artifact`가 task worktree 밖 guard zipapp 절대 경로다.
- manifest의 `artifact_sha256`이 실제 zipapp과 일치한다.
- zipapp의 `verify --phase hermes`가 Task 1 response contract를 만족한다.
- active Hermes task와 tmux가 0이고 DB backup과 target-file backup이 생성됐다.

**Produces for the main plan:**

- exact patch와 manifest Git SHA
- 세 supported base rehearsal evidence
- target-only install record schema
- Windows/Linux/VPS에서 공통으로 호출할 `check`, `install`, `verify`, `rollback` 명령

Live install 직전 각 host에서 다음 순서를 사용한다.

```text
dispatcher stop
gateway drain
active task/run 0 확인
SQLite .backup
Hermes target backup
hermes-patch.py check
hermes-patch.py install
hermes-patch.py verify --root ROOT --manifest MANIFEST --record RECORD --current-manifest CURRENT --expected-source-sha SHA
targeted Hermes tests
gateway graceful restart
DB quick_check
negative receiptless completion smoke
positive phase=hermes receipt smoke
```

Rollback은 다음 순서다.

```text
dispatcher stop 유지
gateway drain/stop
hermes-patch.py verify --root ROOT --manifest MANIFEST --record RECORD --current-manifest CURRENT --expected-source-sha SHA
hermes-patch.py rollback
legacy targeted Hermes tests
gateway start
DB quick_check
dispatcher는 canary green 뒤에만 start
```

DB의 additive columns와 `completion_receipts` table은 rollback하지 않는다.

## 자체 검토 체크리스트

- [ ] 새 Hermes module, DB, CLI, Dashboard, test 외 source 파일을 patch target에 넣지 않았다.
- [ ] `variants`가 Windows/VPS base blob 두 개로 exact-keyed되고 각 variant에 7개 AST preimages와 target별 preimage/postimage SHA map이 있다.
- [ ] `phase == "hermes"`가 adapter와 core 두 경계에서 검사된다.
- [ ] protected rejection test가 task, run, event, ledger, child를 모두 비교한다.
- [ ] `_recompute_ready_in_txn(conn, failure_limit: int | None = None) -> int`가 transaction을 열지 않는다.
- [ ] public `recompute_ready()`만 standalone transaction을 열고 `complete_task()`는 내부 함수를 호출한다.
- [ ] receipt insert, task done, run end, consumed/completed events, child promotion이 같은 transaction이다.
- [ ] nested transaction test와 pre-commit observer race test가 모두 실제 body로 존재한다.
- [ ] receipt digest PK와 `(task_id, run_id)` unique가 replay를 막고 duplicate가 전체 transaction을 rollback한다.
- [ ] protocol violation이 max_retries 4보다 우선하고 explicit unblock 전 sticky다.
- [ ] protected edit는 새 run과 receipt 없이는 불가능하다.
- [ ] tool, CLI, Dashboard가 classification을 잃지 않고 완료를 거절한다.
- [ ] installer는 선택된 base blob variant의 target clean, global staged 0, patch allowlist, AST/target preimage, target-specific postimage를 모두 확인한다.
- [ ] `verify --root ROOT --manifest MANIFEST --record RECORD --current-manifest CURRENT --expected-source-sha SHA`가 guard current exact schema/source release/artifact digest를 먼저 확인하고, 그 뒤 record root/base blob/manifest hash/patch hash/current HEAD/target-specific postimage를 모두 확인한다.
- [ ] install/rollback commit changed paths가 target allowlist와 정확히 같고 unrelated porcelain이 보존된다.
- [ ] rollback이 DB schema를 제거하거나 DB snapshot을 복원하지 않는다.
- [ ] 금지된 destructive Git 명령과 의미 없는 success fallback이 없다.

## 완료 명령

```powershell
Set-Location C:\01.project\.worktrees\INFINITY_FORGE\hermes-completion-policy
& $HermesPython -m pytest tests/hermes_cli/test_kanban_completion_policy.py tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_kanban_core_functionality.py tests/hermes_cli/test_kanban_blocked_sticky.py tests/tools/test_kanban_tools.py tests/plugins/test_kanban_dashboard_plugin.py tests/stress/test_atypical_scenarios.py -q
& $HermesPython -m compileall hermes_cli plugins/kanban/dashboard
git diff --check 4281151ae859241351ba14d8c7682dc67ff4c126..HEAD

Set-Location C:\01.project\INFINITY_FORGE
$ForgePython = "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe"
$PytestTarget = "$env:TEMP\infinity-forge-pytest-3.11"
uv pip install --target $PytestTarget --python $ForgePython --upgrade pytest
$PreviousPythonPath = $env:PYTHONPATH
try {
  $env:PYTHONPATH = "$PytestTarget;$PreviousPythonPath"
  & $ForgePython -X utf8 -m pytest tests/hermes/test_hermes_patch.py -q
  & $ForgePython -X utf8 -m compileall forge/ops forge/scripts
} finally {
  $env:PYTHONPATH = $PreviousPythonPath
}
git diff --check
```

Expected: 모든 명령 exit 0이고, main plan은 이 증거를 받은 뒤에만 live install을 시작한다.

## 실행 handoff

이 하위 계획은 Hermes source patch의 transaction 경계와 Forge installer가 긴밀히 연결되므로 `weapon:subagent-driven-development`로 Task 1-4를 격리 Hermes patch agent에, Task 5를 Forge installer agent에 배정하는 방식을 권장한다. Task 4 전체 회귀가 green이 되기 전에 Task 5 artifact를 생성하지 않는다.

## 변경이력

- 2026-07-12 | Hermes patch transaction·Windows durability·approved-base P0/P1 보강 | 변경: `prepared→committed→installed` durable journal과 roll-forward recovery, rollback record archive/pointer 멱등성, POSIX directory fsync 및 Windows `MoveFileExW` write-through rename, writable fd flush/fsync, durable unlink, read-only status optional-lock 차단, JSON-serializable `check` CLI, create-only `refs/infinity-forge/approved-base` consumer contract를 추가하고 ancestry 없는 실제 Windows `540f90190f50f9518bf36632a724e0e58877a10b`·VPS `73b611ad19720d70308dad6b0fb64648aaadc216` variant 모델을 고정했으며 Task 1/5의 live Git mutation과 shallow source export를 `core.longpaths=true` public-origin exact-SHA depth-1 disposable clone으로 교체 | 이유: Windows install 즉시 실패, journal 삭제 직전 crash의 잘못된 abort, rollback 후 stale active record, moving/missing base proof, live carried root ancestry 오판, shallow source export 불가와 Windows MAX_PATH checkout 누락을 함께 제거하기 위함 | 검증: 최신 Task 5 synthetic Windows pytest `19 passed, 1 deselected`(release artifact 생성 전 manifest test만 제외), two-variant disconnected carried-root test 별도 `1 passed`, Windows install→rollback→2nd rollback→same-SHA reinstall 및 `check` CLI JSON 직접 실행 PASS, public origin에서 Windows/VPS SHA와 approved-base SHA exact depth-1 fetch 후 full HEAD 유지·각 pinned blob·full worktree clean·create-only ref 직접 rehearsal PASS, Python code block 36개 AST parse PASS; production artifact와 live install은 아직 실행하지 않음
- 2026-07-12 | Hermes verify guard 교차 계약 통일 | 변경: exact CLI를 `verify --root ROOT --manifest MANIFEST --record RECORD --current-manifest CURRENT --expected-source-sha SHA`로 고정하고 두 새 인자를 parser에서 required로 만들었으며, public `verify_patch()`가 exact `guard/current.json` schema·source release directory·interpreter/artifact 존재·artifact digest를 install record와 target보다 먼저 검증하도록 RED test, 구현 예시, rehearsal, live/rollback sequence, checklist를 동기화 | 이유: Windows·Linux·VPS rollout이 손상되거나 다른 source SHA의 guard와 정상 Hermes patch를 결합한 상태를 성공으로 오판하지 않게 하기 위함 | 검증: Python code block 36개 AST parse PASS, Markdown fence 150개 짝수, `git diff --check -- docs/weapon/plans/2026-07-12-hermes-completion-policy-subplan.md` exit 0; production code와 live install은 아직 실행하지 않음
- 2026-07-12 | supported-base variant 및 설치 verify P1 보강 | 변경: patch manifest를 Windows/VPS base blob keyed `variants`로 바꾸고 variant별 AST/target preimage와 target-specific postimage를 고정했으며, 두 variant의 distinct postimage·unrelated hunk 보존·install/verify/rollback actual RED test, `verify --root ROOT --manifest MANIFEST --record RECORD --current-manifest CURRENT --expected-source-sha SHA` CLI, exact `guard/current.json` nested schema를 추가 | 이유: VPS carried hunk를 Windows postimage로 오판하거나 flat manifest rollback이 unrelated source를 훼손하는 위험을 제거하고 live rollout preflight가 설치 record와 현재 target을 독립 검증하게 하기 위함 | 검증: old flat manifest key scan 0건, 금지 placeholder scan 0건, Python code block 36개 AST parse PASS, Markdown fence 150개 짝수, `git diff --check -- docs/weapon/plans/2026-07-12-hermes-completion-policy-subplan.md` exit 0; production code와 live install은 아직 실행하지 않음
- 2026-07-12 | Hermes receipt 만료 경계 정합화 | 변경: adapter와 DB transaction 모두 `now == expires_at`을 만료로 거절하고 exact-boundary RED test를 추가 | 이유: core의 `now < expires_at` 소비 규칙과 Hermes가 같은 900초 half-open interval을 사용하도록 보장 | 검증: 구현 전 계획 단계이며 Python fenced block AST와 core phase expiry 계약 대조 대상으로 등록
- 2026-07-12 | Hermes result strict consumer 계약 보강 | 변경: allow 15개·deny 8개 exact key set, core canonical JSON receipt digest 재계산, adapter와 DB의 exact 900초 lifetime을 RED tests와 구현 예시에 추가 | 이유: extra field, 임의 digest, 장기 receipt가 trusted consumer 경계를 우회하지 못하도록 독립 리뷰 P1을 제거 | 검증: 구현 전 계획 단계이며 Python fenced block AST와 core result adapter 대조 대상으로 등록
- 2026-07-12 | malformed numeric·expiry 독립 경계 보강 | 변경: NaN/Infinity를 JSON parse 시 typed GATE_ERROR로 변환하는 RED test를 추가하고 DB exact-expiry fixture의 lifetime을 항상 900초로 분리 | 이유: canonical digest ValueError 누출과 TTL 조건이 expiry 회귀를 가리는 테스트 중첩 제거 | 검증: 구현 전 계획 단계이며 Python fenced block AST 및 strict consumer 재감사 대상으로 등록
- 2026-07-12 | Hermes completion-policy carried patch 하위 계획 작성 | 변경: Hermes verifier adapter, additive policy/receipt schema, `_recompute_ready_in_txn()` 기반 단일 completion transaction, sticky `protocol_violation`, proof 편집 차단, pinned patch artifact와 target-only install/rollback을 5개 TDD Task로 구체화 | 이유: 승인 spec의 Hermes 완료 불변식을 Windows·Linux·VPS 공통 실행 경로에 적용하고 umbrella 계획의 `phase=hermes`, receipt 원자 소비, 자식 승격 race 방지 계약과 일치시키기 위해 작성 | 검증: 금지 표현 검사 0건, non-Hermes phase `stop|post-exit|ci`와 `phase=hermes` 이중 검사 확인, Markdown code fence 148개로 짝수 확인, `git diff --check -- docs/weapon/plans/2026-07-12-hermes-completion-policy-subplan.md` exit 0; 계획에 적은 production tests와 실운영 배포는 아직 실행하지 않음
