from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "forge" / "scripts" / "label-mirror.py"


def load_mirror() -> ModuleType:
    name = "forge_label_mirror_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def card(
    card_id: str,
    *,
    status: str,
    parent_id: str | None = None,
    summary: dict[str, object] | None = None,
    metadata: dict[str, object] | None = None,
    body: str | None = None,
    run_status: str | None = None,
    run_outcome: str | None = None,
) -> dict[str, object]:
    return {
        "id": card_id,
        "title": card_id,
        "status": status,
        "parent_id": parent_id,
        "summary": summary,
        "metadata": metadata or {},
        "body": body,
        "run_status": run_status or ("done" if summary is not None else None),
        "run_outcome": run_outcome or ("completed" if summary is not None else None),
    }


def reviewer_summary(*, verdict: str) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "forge-reviewer-result/v1",
        "verdict": verdict,
        "source_digest": "a" * 64,
        "pr_url": "https://github.com/acme/widgets/pull/99",
        "head_sha": "b" * 40,
        "delta_check": {"implemented_verified": ["AC1"], "discrepancies": []},
        "spec_check": {"met": ["AC1"], "unmet": []},
    }
    if verdict == "reject":
        value["reflection"] = "AC2 is missing"
    return value


def critic_summary(*, outcome: str) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "forge-critic-result/v1",
        "outcome": outcome,
        "source_digest": "c" * 64,
        "pr_url": "https://github.com/acme/widgets/pull/99",
        "reviewed_head_sha": "d" * 40,
        "result_head_sha": "e" * 40,
        "added_tests": ["tests/test_widget.py"],
        "scenarios": ["concurrent update"],
    }
    if outcome == "defect_found":
        value["reflection"] = "race is reproducible"
    return value


def bound_critic_card() -> dict[str, object]:
    binding = {
        "bound_head_sha": "d" * 40,
        "pr_url": "https://github.com/acme/widgets/pull/99",
        "reflection": None,
        "source_digest": "c" * 64,
        "source_run_id": 8,
        "source_task_id": "reviewer",
    }
    return card(
        "critic",
        status="done",
        parent_id="reviewer",
        summary=critic_summary(outcome="pass"),
        body=f"```json\n{json.dumps(binding)}\n```",
    )


def test_projection_uses_issue_identity_from_keys_not_pr_number() -> None:
    mirror = load_mirror()
    root_key = "github-issue:acme/widgets#7"
    critic_key = "forge-stage:acme/widgets#7:critic:" + "1" * 16
    cards = {
        root_key: card("root", status="done"),
        critic_key: card(
            "critic",
            status="done",
            parent_id="root",
            summary=critic_summary(outcome="pass"),
        ),
    }

    targets = mirror.projection_targets(
        cards,
        current_head_green=lambda _: True,
    )

    assert targets == {root_key: "forge:mergeable"}


def test_raw_root_done_projects_need_review_without_calling_ci() -> None:
    mirror = load_mirror()
    root_key = "github-issue:acme/widgets#7"

    def unexpected_ci(_: dict[str, object]) -> bool:
        raise AssertionError("raw executor completion must not query mergeability")

    targets = mirror.projection_targets(
        {root_key: card("root", status="done")},
        current_head_green=unexpected_ci,
    )

    assert targets[root_key] == "forge:need-review"


def test_reviewer_approve_frontier_projects_need_critic() -> None:
    mirror = load_mirror()
    root_key = "github-issue:acme/widgets#7"
    reviewer_key = "forge-stage:acme/widgets#7:reviewer:" + "1" * 16
    cards = {
        root_key: card("root", status="done"),
        reviewer_key: card(
            "reviewer",
            status="done",
            parent_id="root",
            summary=reviewer_summary(verdict="approve"),
        ),
    }

    targets = mirror.projection_targets(cards, current_head_green=lambda _: False)

    assert targets[root_key] == "forge:need-critic"


def test_reviewer_reject_frontier_projects_need_execution() -> None:
    mirror = load_mirror()
    root_key = "github-issue:acme/widgets#7"
    reviewer_key = "forge-stage:acme/widgets#7:reviewer:" + "1" * 16
    cards = {
        root_key: card("root", status="done"),
        reviewer_key: card(
            "reviewer",
            status="done",
            parent_id="root",
            summary=reviewer_summary(verdict="reject"),
        ),
    }

    targets = mirror.projection_targets(cards, current_head_green=lambda _: False)

    assert targets[root_key] == "forge:need-execution"


def test_critic_pass_requires_injected_exact_head_ci_gate() -> None:
    mirror = load_mirror()
    root_key = "github-issue:acme/widgets#7"
    critic_key = "forge-stage:acme/widgets#7:critic:" + "1" * 16
    cards = {
        root_key: card("root", status="done"),
        critic_key: card(
            "critic",
            status="done",
            parent_id="root",
            summary=critic_summary(outcome="pass"),
        ),
    }

    pending = mirror.projection_targets(cards, current_head_green=lambda _: False)
    green = mirror.projection_targets(cards, current_head_green=lambda _: True)

    assert pending[root_key] == "forge:need-critic"
    assert green[root_key] == "forge:mergeable"


def test_live_critic_gate_requires_current_result_head_eval_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mirror = load_mirror()
    calls: list[list[str]] = []

    def fake_sh(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
        calls.append(args)
        if "/pulls/99" in args[-1]:
            return 0, json.dumps(
                {
                    "number": 99,
                    "html_url": "https://github.com/acme/widgets/pull/99",
                    "state": "open",
                    "draft": False,
                    "head": {"sha": "e" * 40},
                }
            ), ""
        return 0, json.dumps(
            {
                "check_runs": [
                    {
                        "name": "eval",
                        "status": "completed",
                        "conclusion": "success",
                        "head_sha": "e" * 40,
                    }
                ]
            }
        ), ""

    monkeypatch.setattr(mirror, "sh", fake_sh)

    assert mirror.critic_current_head_green(bound_critic_card()) is True
    assert any(f"/commits/{'e' * 40}/check-runs" in arg for call in calls for arg in call)


def test_live_critic_gate_keeps_pending_eval_non_mergeable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mirror = load_mirror()

    def fake_sh(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
        if "/pulls/99" in args[-1]:
            return 0, json.dumps(
                {
                    "number": 99,
                    "html_url": "https://github.com/acme/widgets/pull/99",
                    "state": "open",
                    "draft": False,
                    "head": {"sha": "e" * 40},
                }
            ), ""
        return 0, json.dumps(
            {
                "check_runs": [
                    {
                        "name": "eval",
                        "status": "in_progress",
                        "conclusion": None,
                        "head_sha": "e" * 40,
                    }
                ]
            }
        ), ""

    monkeypatch.setattr(mirror, "sh", fake_sh)

    assert mirror.critic_current_head_green(bound_critic_card()) is False


def test_live_critic_gate_rejects_duplicate_eval_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mirror = load_mirror()

    def fake_sh(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
        if "/pulls/99" in args[-1]:
            return 0, json.dumps(
                {
                    "number": 99,
                    "html_url": "https://github.com/acme/widgets/pull/99",
                    "state": "open",
                    "draft": False,
                    "head": {"sha": "e" * 40},
                }
            ), ""
        check = {
            "name": "eval",
            "status": "completed",
            "conclusion": "success",
            "head_sha": "e" * 40,
        }
        return 0, json.dumps({"check_runs": [check, check]}), ""

    monkeypatch.setattr(mirror, "sh", fake_sh)

    with pytest.raises(mirror.ProjectionError, match="exactly one eval"):
        mirror.critic_current_head_green(bound_critic_card())


@pytest.mark.parametrize(
    ("pr_overrides", "check_overrides", "message"),
    [
        ({"number": 100}, {}, "number"),
        ({}, {"status": "mystery"}, "status"),
    ],
)
def test_live_critic_gate_rejects_malformed_github_evidence(
    monkeypatch: pytest.MonkeyPatch,
    pr_overrides: dict[str, object],
    check_overrides: dict[str, object],
    message: str,
) -> None:
    mirror = load_mirror()

    def fake_sh(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
        if "/pulls/99" in args[-1]:
            pr: dict[str, object] = {
                "number": 99,
                "html_url": "https://github.com/acme/widgets/pull/99",
                "state": "open",
                "draft": False,
                "head": {"sha": "e" * 40},
            }
            pr.update(pr_overrides)
            return 0, json.dumps(pr), ""
        check: dict[str, object] = {
            "name": "eval",
            "status": "completed",
            "conclusion": "success",
            "head_sha": "e" * 40,
        }
        check.update(check_overrides)
        return 0, json.dumps({"check_runs": [check]}), ""

    monkeypatch.setattr(mirror, "sh", fake_sh)

    with pytest.raises(mirror.ProjectionError, match=message):
        mirror.critic_current_head_green(bound_critic_card())


def test_malformed_done_critic_result_is_not_treated_as_mergeable() -> None:
    mirror = load_mirror()
    root_key = "github-issue:acme/widgets#7"
    critic_key = "forge-stage:acme/widgets#7:critic:" + "1" * 16
    cards = {
        root_key: card("root", status="done"),
        critic_key: card(
            "critic",
            status="done",
            parent_id="root",
            summary={"outcome": "pass"},
        ),
    }

    with pytest.raises(mirror.ProjectionError, match="critic result"):
        mirror.projection_targets(cards, current_head_green=lambda _: True)


def test_unsuccessful_done_stage_run_is_rejected_before_projection() -> None:
    mirror = load_mirror()
    root_key = "github-issue:acme/widgets#7"
    critic_key = "forge-stage:acme/widgets#7:critic:" + "1" * 16
    cards = {
        root_key: card("root", status="done"),
        critic_key: card(
            "critic",
            status="done",
            parent_id="root",
            summary=critic_summary(outcome="pass"),
            run_status="failed",
            run_outcome="error",
        ),
    }

    with pytest.raises(mirror.ProjectionError, match="successful completed run"):
        mirror.projection_targets(cards, current_head_green=lambda _: True)


def test_multiple_pipeline_leaves_fail_closed() -> None:
    mirror = load_mirror()
    root_key = "github-issue:acme/widgets#7"
    cards = {
        root_key: card("root", status="done"),
        "forge-stage:acme/widgets#7:reviewer:" + "1" * 16: card(
            "reviewer-one",
            status="ready",
            parent_id="root",
        ),
        "forge-stage:acme/widgets#7:reviewer:" + "2" * 16: card(
            "reviewer-two",
            status="ready",
            parent_id="root",
        ),
    }

    with pytest.raises(mirror.ProjectionError, match="frontier"):
        mirror.projection_targets(cards, current_head_green=lambda _: False)


def test_cards_by_key_reads_root_stage_links_and_latest_run(tmp_path: Path) -> None:
    mirror = load_mirror()
    db = tmp_path / "kanban.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            status TEXT NOT NULL,
            idempotency_key TEXT,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE task_links (parent_id TEXT NOT NULL, child_id TEXT NOT NULL);
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            status TEXT NOT NULL,
            outcome TEXT,
            summary TEXT,
            metadata TEXT
        );
        """
    )
    root_key = "github-issue:acme/widgets#7"
    stage_key = "forge-stage:acme/widgets#7:reviewer:" + "1" * 16
    con.executemany(
        "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("root", "root", None, "done", root_key, 1),
            ("reviewer", "reviewer", "body", "done", stage_key, 2),
            ("other", "other", None, "done", "unrelated:key", 3),
            (
                "legacy-review",
                "legacy-review",
                None,
                "done",
                "github-issue:acme/widgets#7-review",
                4,
            ),
        ],
    )
    con.execute("INSERT INTO task_links VALUES ('root', 'reviewer')")
    con.executemany(
        "INSERT INTO task_runs VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, "reviewer", "done", "completed", json.dumps({"old": True}), "{}"),
            (
                2,
                "reviewer",
                "done",
                "completed",
                json.dumps(reviewer_summary(verdict="approve")),
                json.dumps({"worker_session_id": "session-2"}),
            ),
        ],
    )
    con.commit()
    con.close()
    mirror.DB = str(db)

    cards = mirror.cards_by_key()

    assert set(cards) == {root_key, stage_key}
    assert cards[stage_key]["parent_id"] == "root"
    assert cards[stage_key]["summary"]["verdict"] == "approve"
    assert cards[stage_key]["metadata"] == {"worker_session_id": "session-2"}
    assert cards[stage_key]["run_status"] == "done"
    assert cards[stage_key]["run_outcome"] == "completed"


def test_import_new_issues_keeps_root_executor_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    mirror = load_mirror()
    calls: list[list[str]] = []
    issue = {
        "number": 7,
        "title": "Implement widget",
        "html_url": "https://github.com/acme/widgets/issues/7",
    }

    def fake_sh(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
        calls.append(args)
        if "issues?state=open" in " ".join(args):
            return 0, json.dumps([issue]), ""
        return 0, "created", ""

    monkeypatch.setattr(mirror, "sh", fake_sh)

    mirror.import_new_issues("acme/widgets", existing_keys=set())

    create = next(args for args in calls if args[:3] == [mirror.HERMES, "kanban", "create"])
    assert create[create.index("--assignee") + 1] == "executor"
    assert create[create.index("--idempotency-key") + 1] == "github-issue:acme/widgets#7"
    assert create[create.index("--max-retries") + 1] == "4"
