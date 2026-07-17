"""Plain-English names and live runtime entrypoint contracts."""

from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "forge" / "scripts"
HOOKS = ROOT / "forge" / "hooks"

REQUIRED_ENTRYPOINTS = (
    SCRIPTS / "task-service.py",
    SCRIPTS / "task-flow-worker.py",
    SCRIPTS / "issue-status-sync.py",
    SCRIPTS / "merge-worker.py",
    SCRIPTS / "system-check.sh",
    SCRIPTS / "state-mismatch-check.sh",
    SCRIPTS / "activity-log-writer.py",
    SCRIPTS / "send-pending-messages.py",
    HOOKS / "codex-work-check.sh",
)

ACTIVE_DOCS = (
    ROOT / "docs" / "plan.md",
    ROOT / "docs" / "easy_guide.md",
    ROOT / "docs" / "user-runbook.md",
    ROOT / "docs" / "automation-architecture.md",
    ROOT / "docs" / "ops-guide.md",
    ROOT / "docs" / "backup-guide.md",
)

ACTIVE_SKILLS = (
    ROOT / "forge" / "skills" / "build-task" / "SKILL.md",
    ROOT / "forge" / "skills" / "review-task" / "SKILL.md",
    ROOT / "forge" / "skills" / "deep-check" / "SKILL.md",
    ROOT / "forge" / "skills" / "fix-task" / "SKILL.md",
    ROOT / "forge" / "skills" / "forge-labels" / "SKILL.md",
    ROOT / "forge" / "skills" / "forge-ops" / "SKILL.md",
)

FORBIDDEN_ACTIVE_NAMES = re.compile(
    r"interaction_mode|assurance_policy|merge_policy|"
    r"P[123]|policy_ledger|policy_digest|scope_digest|"
    r"preimage_hash|postimage_hash|rollback_artifact|"
    r"stage-reconciler|label-mirror|codex-stop-gate|"
    r"ledger-emit|flush-outbox|drift-audit|"
    r"executor(?:-rework|_rework)?|critic|"
    r"GATE_ERROR|CANARY_FAIL|\bDRIFT:|"
    r"spec-draft|need-execution|in-progress|need-review|need-critic|"
    r"need-deep_checker|forge:blocked|github-issue|handoff|"
    r"OApproval|Approvalentication|fix_notess",
    re.IGNORECASE,
)


def _load_script(name: str):
    path = SCRIPTS / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_phase_a_plain_entrypoints_exist() -> None:
    assert [str(path.relative_to(ROOT)) for path in REQUIRED_ENTRYPOINTS if not path.is_file()] == []


def test_active_runtime_docs_and_skills_use_only_plain_forge_names() -> None:
    matches: list[str] = []
    for path in (*REQUIRED_ENTRYPOINTS, *ACTIVE_DOCS, *ACTIVE_SKILLS):
        if not path.is_file():
            matches.append(f"missing: {path.relative_to(ROOT)}")
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if FORBIDDEN_ACTIVE_NAMES.search(line):
                matches.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert matches == []


def test_deploy_profiles_use_the_four_plain_roles() -> None:
    bash = (SCRIPTS / "deploy-vps.sh").read_text(encoding="utf-8")
    windows = (SCRIPTS / "deploy-windows.ps1").read_text(encoding="utf-8")

    for role in ("builder", "reviewer", "deep_checker", "fix"):
        assert role in bash
        assert role in windows
    active_bash = re.sub(
        r"# OLD_(?:PROFILE_MIGRATION|INSTALLATION_CLEANUP)_BEGIN.*?"
        r"# OLD_(?:PROFILE_MIGRATION|INSTALLATION_CLEANUP)_END",
        "",
        bash,
        flags=re.DOTALL,
    )
    assert not re.search(r"\b(?:executor|critic)\b", active_bash)
    role_map_start = windows.index("$roleSkills =")
    role_map_end = windows.index("foreach ($skill", role_map_start)
    active_windows_roles = windows[role_map_start:role_map_end]
    assert not re.search(r"\b(?:executor|critic|issuefinder)\b", active_windows_roles)


def test_merge_worker_is_disabled_by_default() -> None:
    module = _load_script("merge-worker.py")
    assert module.AUTO_MERGE_ENABLED_DEFAULT is False
    with pytest.raises(module.AutoMergeDisabledError, match="disabled"):
        module.require_auto_merge_enabled({})


def test_task_service_port_parses_only_the_plain_request_fields() -> None:
    module = _load_script("task-service.py")
    value = {
        "request_id": "12345678-1234-4234-8234-123456789abc",
        "repository": "owner/repo",
        "title": "Build the feature",
        "description": "Confirmed work",
        "acceptance_criteria": ["AC1"],
        "task_flow": "build_review",
        "merge_mode": "safe_auto",
        "confirmed_by": "user-1",
        "confirmed_at": "2026-07-16T00:00:00Z",
    }

    request = module.parse_creation_request(value)

    assert request.task_flow.value == "build_review"
    assert request.merge_mode.value == "safe_auto"
    assert request.confirmed_at == datetime(2026, 7, 16, tzinfo=timezone.utc)
    with pytest.raises(module.TaskServiceInputError, match="fields do not match"):
        module.parse_creation_request({**value, "merge_policy": "P2"})


def test_task_flow_and_issue_status_ports_consume_task5_apis() -> None:
    from forge.ops.task_flow import TaskFlowState, TaskFlowStatus
    from forge.ops.task_options import TaskFlow

    flow_module = _load_script("task-flow-worker.py")
    status_module = _load_script("issue-status-sync.py")
    state = TaskFlowState(
        task_flow=TaskFlow.BUILD,
        task_settings_hash="a" * 64,
        pr_url="https://github.com/owner/repo/pull/1",
        current_base_commit="c" * 40,
        current_commit="b" * 40,
    )
    assert status_module.label_for_task(state) == "forge:ready-to-build"

    completed = flow_module.apply_completed_summary(
        state,
        {
            "format_version": "forge-build-result/v1",
            "task_settings_hash": "a" * 64,
            "pr_url": "https://github.com/owner/repo/pull/1",
            "built_base_commit": "c" * 40,
            "built_commit": "b" * 40,
            "changed_files": ["src/example.py"],
            "completed_items": ["AC1"],
            "remaining_items": [],
            "checks_by_item": {"AC1": "tests/test_example.py::test_ac1"},
        },
        current_commit="b" * 40,
    )
    assert completed.status is TaskFlowStatus.READY_TO_MERGE


def test_activity_log_writer_appends_each_event_once(tmp_path: Path) -> None:
    module = _load_script("activity-log-writer.py")
    database = tmp_path / "kanban.db"
    activity_log = tmp_path / "activity.jsonl"
    state_file = tmp_path / "activity.state"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT);"
            "CREATE TABLE task_events ("
            "id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER, kind TEXT, "
            "payload TEXT, created_at INTEGER);"
        )
        connection.execute(
            "INSERT INTO tasks VALUES ('t1', 'Task', 'builder', 'done')"
        )
        connection.execute(
            "INSERT INTO task_events VALUES (1, 't1', 3, 'completed', '{}', 1)"
        )

    assert module.write_new_events(database, activity_log, state_file) == 1
    assert module.write_new_events(database, activity_log, state_file) == 0
    assert json.loads(activity_log.read_text(encoding="utf-8"))["event_id"] == 1
    assert state_file.read_text(encoding="utf-8") == "1"


def test_pending_message_fields_have_plain_defaults() -> None:
    module = _load_script("send-pending-messages.py")

    assert module._message_fields("## [decision] Name\nproject:: Forge\ntags:: one, two") == (
        "decision",
        "Forge",
        ["one", "two"],
    )
    assert module._message_fields("plain text") == (None, "INFINITY_FORGE", None)


@pytest.mark.parametrize(
    "script",
    ("task-service.py", "task-flow-worker.py", "issue-status-sync.py", "merge-worker.py"),
)
def test_python_entrypoint_help_is_runnable(script: str) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / script), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_hermes_change_docs_name_all_six_targets() -> None:
    paths = (
        ROOT / "docs" / "weapon" / "plans" / "2026-07-16-task-flow-and-auto-merge.md",
        ROOT
        / "docs"
        / "weapon"
        / "specs"
        / "2026-07-16-hermes-task-flow-auto-merge-design.md",
    )
    targets = (
        "hermes_cli/plugins.py",
        "agent/conversation_loop.py",
        "run_agent.py",
        "cli.py",
        "tui_gateway/server.py",
        "gateway/run.py",
    )
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "two-file" not in text
        assert "두 파일" not in text
        for target in targets:
            assert target in text
