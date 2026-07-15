"""codex-stop-gate.sh v0.2 판정 계약 테스트.

게이트가 "차단해야 할 것을 차단하고, 통과시켜야 할 것을 통과"시키는지
정답이 알려진 fixture로 검증한다 (2026-07-12 실측된 fail-open 구멍의 회귀 방지).

실행 전제: bash, git. gh 의존 케이스는 PATH 스텁으로 결정론화(POSIX 전용, Windows skip).
"""
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GATE = (REPO / "forge" / "hooks" / "codex-stop-gate.sh").as_posix()
PYEXE = Path(sys.executable).as_posix()
IS_WINDOWS = os.name == "nt"


def find_bash():
    """Windows에서는 WSL bash(System32)가 아니라 Git Bash를 명시적으로 고른다."""
    if not IS_WINDOWS:
        return "bash"
    git_exe = shutil.which("git")
    if git_exe:
        for parent in Path(git_exe).resolve().parents:
            for cand in (parent / "bin" / "bash.exe", parent / "usr" / "bin" / "bash.exe"):
                if cand.exists():
                    return str(cand)
    return "bash"


BASH = find_bash()


def run_gate(workdir, extra_env=None):
    env = os.environ.copy()
    # 테스트 자동감지·인터프리터를 결정론화: 대상 레포의 테스트는 항상 통과 처리
    env.update({"FORGE_PY": PYEXE, "FORGE_TEST_CMD": "true"})
    env.pop("FORGE_BASE_SHA", None)
    env.pop("FORGE_REQUIRE_HANDOFF", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [BASH, GATE, Path(workdir).as_posix()],
        capture_output=True, encoding="utf-8", errors="replace",
        env=env, timeout=120,
    )


def git(workdir, *args):
    subprocess.run(
        ["git", "-C", str(workdir),
         "-c", "user.email=gate-test@forge", "-c", "user.name=gate-test",
         *args],
        check=True, capture_output=True, text=True,
    )


def make_repo(tmp_path):
    repo = tmp_path / "ws"
    repo.mkdir()
    git(repo, "init", "-q")
    return repo


def valid_handoff(**overrides):
    h = {
        "pr_url": "https://github.com/example/project/pull/1",
        "changed_files": ["file.txt"],
        "implemented": ["AC1 파일 생성"],
        "not_implemented": [],
        "verified_by": {"AC1 파일 생성": "tests/test_file.py"},
    }
    h.update(overrides)
    return h


def write_handoff(repo, handoff):
    (repo / "handoff.json").write_text(
        json.dumps(handoff, ensure_ascii=False), encoding="utf-8")


# ── 1. 빈 diff 판정 ──────────────────────────────────────

def test_empty_repo_blocked(tmp_path):
    repo = make_repo(tmp_path)
    r = run_gate(repo)
    assert r.returncode == 2
    assert "TESTS_FAILED" in r.stderr and "empty diff" in r.stderr


def test_handoff_only_change_blocked(tmp_path):
    """핸드오프 파일 하나만 만든 작업은 구현 변경이 아니다."""
    repo = make_repo(tmp_path)
    write_handoff(repo, valid_handoff())
    r = run_gate(repo)
    assert r.returncode == 2
    assert "empty diff" in r.stderr


def test_committed_clean_tree_passes_with_base_sha(tmp_path):
    """작업을 커밋해 워크트리가 깨끗해도 base SHA 기준으로 변경을 인정한다(v0.1 오판 회귀)."""
    repo = make_repo(tmp_path)
    (repo / "seed.txt").write_text("seed")
    git(repo, "add", "."); git(repo, "commit", "-qm", "seed")
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    (repo / "file.txt").write_text("work")
    git(repo, "add", "."); git(repo, "commit", "-qm", "work")
    write_handoff(repo, valid_handoff())
    r = run_gate(repo, {"FORGE_BASE_SHA": base})
    assert r.returncode == 0, r.stderr


def test_committed_clean_tree_without_base_sha_blocked(tmp_path):
    """base SHA가 없으면 커밋된 변경을 증명할 수 없다 → fail-closed 차단."""
    repo = make_repo(tmp_path)
    (repo / "file.txt").write_text("work")
    git(repo, "add", "."); git(repo, "commit", "-qm", "work")
    write_handoff(repo, valid_handoff())
    r = run_gate(repo)
    assert r.returncode == 2
    assert "empty diff" in r.stderr


def test_base_sha_file_fallback(tmp_path):
    """env가 없으면 .forge-base-sha 파일에서 base를 읽는다."""
    repo = make_repo(tmp_path)
    (repo / "seed.txt").write_text("seed")
    git(repo, "add", "."); git(repo, "commit", "-qm", "seed")
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    (repo / ".forge-base-sha").write_text(base + "\n")
    (repo / "file.txt").write_text("work")
    git(repo, "add", "file.txt"); git(repo, "commit", "-qm", "work")
    write_handoff(repo, valid_handoff())
    r = run_gate(repo)
    assert r.returncode == 0, r.stderr


def test_invalid_base_sha_is_gate_error(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "file.txt").write_text("work")
    r = run_gate(repo, {"FORGE_BASE_SHA": "0" * 40})
    assert r.returncode == 2
    assert "GATE_ERROR" in r.stderr


# ── 2. 핸드오프 필수화 + 스키마 (D16) ─────────────────────

def test_missing_handoff_blocked(tmp_path):
    """v0.1 구멍: handoff.json이 아예 없어도 통과했다 → 이제 차단."""
    repo = make_repo(tmp_path)
    (repo / "file.txt").write_text("work")
    r = run_gate(repo)
    assert r.returncode == 2
    assert "handoff file missing" in r.stderr


def test_missing_handoff_allowed_when_opted_out(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "file.txt").write_text("work")
    r = run_gate(repo, {"FORGE_REQUIRE_HANDOFF": "0"})
    assert r.returncode == 0, r.stderr


def test_malformed_handoff_blocked(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "file.txt").write_text("work")
    (repo / "handoff.json").write_text("{not json", encoding="utf-8")
    r = run_gate(repo)
    assert r.returncode == 2
    assert "TESTS_FAILED" in r.stderr and "parse failed" in r.stderr


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"pr_url": None}, "pr_url"),
        ({"pr_url": "https://github.com/example/project/issues/1"}, "pr_url"),
        ({"changed_files": "file.txt"}, "changed_files"),
        ({"extra": "not allowed"}, "unexpected fields"),
    ],
)
def test_executor_handoff_matches_exact_five_field_contract(
    tmp_path, overrides, message
):
    repo = make_repo(tmp_path)
    (repo / "file.txt").write_text("work")
    write_handoff(repo, valid_handoff(**overrides))

    result = run_gate(repo)

    assert result.returncode == 2
    assert message in result.stderr


def test_empty_implemented_blocked(tmp_path):
    """v0.1 구멍: implemented가 빈 배열이어도 통과했다 → 이제 차단."""
    repo = make_repo(tmp_path)
    (repo / "file.txt").write_text("work")
    write_handoff(repo, valid_handoff(implemented=[], verified_by={"x": "y"}))
    r = run_gate(repo)
    assert r.returncode == 2
    assert "implemented must be a non-empty array" in r.stderr


def test_empty_verified_by_blocked(tmp_path):
    """v0.1 구멍: verified_by가 빈 값이어도 통과했다 → 이제 차단."""
    repo = make_repo(tmp_path)
    (repo / "file.txt").write_text("work")
    write_handoff(repo, valid_handoff(verified_by={}))
    r = run_gate(repo)
    assert r.returncode == 2
    assert "verified_by must be a non-empty object" in r.stderr


def test_verified_by_must_cover_implemented(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "file.txt").write_text("work")
    write_handoff(repo, valid_handoff(
        implemented=["AC1", "AC2"], verified_by={"AC1": "tests/a.py"}))
    r = run_gate(repo)
    assert r.returncode == 2
    assert "without verified_by entry" in r.stderr and "AC2" in r.stderr


def test_not_implemented_string_blocked(tmp_path):
    """타입 강제: not_implemented가 문자열("없음")이면 차단."""
    repo = make_repo(tmp_path)
    (repo / "file.txt").write_text("work")
    write_handoff(repo, valid_handoff(not_implemented="없음"))
    r = run_gate(repo)
    assert r.returncode == 2
    assert "must be a JSON array" in r.stderr


def test_empty_not_implemented_array_passes(tmp_path):
    """빈 배열은 '잔여 없음'의 합법 표기다(스펙 7.2)."""
    repo = make_repo(tmp_path)
    (repo / "file.txt").write_text("work")
    write_handoff(repo, valid_handoff(not_implemented=[]))
    r = run_gate(repo)
    assert r.returncode == 0, r.stderr


def test_residual_without_id_blocked(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "file.txt").write_text("work")
    write_handoff(repo, valid_handoff(not_implemented=[{"title": "남은 일"}]))
    r = run_gate(repo)
    assert r.returncode == 2
    assert "without issue/card ID" in r.stderr


# ── 3. 잔여 물질화: card_id 실존 (D17) ────────────────────

def make_kanban_db(tmp_path, card_ids):
    db = tmp_path / "kanban.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY)")
    con.executemany("INSERT INTO tasks VALUES (?)", [(c,) for c in card_ids])
    con.commit(); con.close()
    return db


def test_nonexistent_card_id_blocked(tmp_path):
    """v0.1 구멍: 존재하지 않는 card_id도 통과했다 → 이제 DB 실존 확인."""
    repo = make_repo(tmp_path)
    (repo / "file.txt").write_text("work")
    db = make_kanban_db(tmp_path, ["t_real"])
    write_handoff(repo, valid_handoff(
        not_implemented=[{"title": "남은 일", "card_id": "t_ghost"}]))
    r = run_gate(repo, {"FORGE_KANBAN_DB": str(db)})
    assert r.returncode == 2
    assert "card does not exist" in r.stderr and "t_ghost" in r.stderr


def test_existing_card_id_passes(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "file.txt").write_text("work")
    db = make_kanban_db(tmp_path, ["t_real"])
    write_handoff(repo, valid_handoff(
        not_implemented=[{"title": "남은 일", "card_id": "t_real"}]))
    r = run_gate(repo, {"FORGE_KANBAN_DB": str(db)})
    assert r.returncode == 0, r.stderr


def test_missing_kanban_db_is_gate_error(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "file.txt").write_text("work")
    write_handoff(repo, valid_handoff(
        not_implemented=[{"title": "남은 일", "card_id": "t_real"}]))
    r = run_gate(repo, {"FORGE_KANBAN_DB": str(tmp_path / "no-such.db")})
    assert r.returncode == 2
    assert "GATE_ERROR" in r.stderr and "kanban DB not found" in r.stderr


# ── 4. 잔여 물질화: issue 실존 (gh 스텁, POSIX 전용) ───────

def make_gh_stub(tmp_path, stderr_text, rc=1):
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    gh = stub_dir / "gh"
    gh.write_text(f"#!/bin/sh\necho '{stderr_text}' >&2\nexit {rc}\n")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return stub_dir


@pytest.mark.skipif(IS_WINDOWS, reason="PATH 셸 스텁은 POSIX 전용 (CI ubuntu에서 실행)")
def test_nonexistent_issue_blocked(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "file.txt").write_text("work")
    stub = make_gh_stub(tmp_path, "gh: Not Found (HTTP 404)")
    write_handoff(repo, valid_handoff(
        not_implemented=[{"title": "남은 일", "repo": "o/r", "issue_id": "#999"}]))
    r = run_gate(repo, {"PATH": f"{stub}{os.pathsep}{os.environ['PATH']}"})
    assert r.returncode == 2
    assert "TESTS_FAILED" in r.stderr and "does not exist" in r.stderr


@pytest.mark.skipif(IS_WINDOWS, reason="PATH 셸 스텁은 POSIX 전용 (CI ubuntu에서 실행)")
def test_gh_outage_is_gate_error_not_pass(tmp_path):
    """gh 네트워크 장애를 이슈 실존 실패나 통과로 위장하지 않는다."""
    repo = make_repo(tmp_path)
    (repo / "file.txt").write_text("work")
    stub = make_gh_stub(tmp_path, "connect: network is unreachable")
    write_handoff(repo, valid_handoff(
        not_implemented=[{"title": "남은 일", "repo": "o/r", "issue_id": "#1"}]))
    r = run_gate(repo, {"PATH": f"{stub}{os.pathsep}{os.environ['PATH']}"})
    assert r.returncode == 2
    assert "GATE_ERROR" in r.stderr


def test_issue_without_repo_blocked(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "file.txt").write_text("work")
    write_handoff(repo, valid_handoff(
        not_implemented=[{"title": "남은 일", "issue_id": "#7"}]))
    r = run_gate(repo)
    assert r.returncode == 2
    assert "with a repo field" in r.stderr
