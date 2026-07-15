from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from forge.ops.contracts import PipelineStage, transition_digest


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
    run_id: int | None = None,
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
        "run_id": run_id if run_id is not None else (1 if summary is not None else None),
    }


def executor_summary() -> dict[str, object]:
    return {
        "pr_url": "https://github.com/acme/widgets/pull/99",
        "changed_files": ["src/widget.py"],
        "implemented": ["AC1"],
        "not_implemented": [],
        "verified_by": {"AC1": "tests/test_widget.py"},
    }


def executor_digest() -> str:
    return transition_digest(
        task_id="root",
        run_id=1,
        stage=PipelineStage.EXECUTOR,
        summary=executor_summary(),
        metadata={},
        pr_url="https://github.com/acme/widgets/pull/99",
        head_sha="b" * 40,
    )


def reviewer_summary(
    *,
    verdict: str,
    source_digest: str | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "forge-reviewer-result/v1",
        "verdict": verdict,
        "source_digest": source_digest or executor_digest(),
        "pr_url": "https://github.com/acme/widgets/pull/99",
        "head_sha": "b" * 40,
        "delta_check": {"implemented_verified": ["AC1"], "discrepancies": []},
        "spec_check": {"met": ["AC1"], "unmet": []},
    }
    if verdict == "reject":
        value["reflection"] = "AC2 is missing"
    return value


def reviewer_digest() -> str:
    summary = reviewer_summary(verdict="approve")
    return transition_digest(
        task_id="reviewer",
        run_id=2,
        stage=PipelineStage.REVIEWER,
        summary=summary,
        metadata={},
        pr_url="https://github.com/acme/widgets/pull/99",
        head_sha="b" * 40,
    )


def critic_summary(
    *,
    outcome: str,
    source_digest: str | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "forge-critic-result/v1",
        "outcome": outcome,
        "source_digest": source_digest or reviewer_digest(),
        "pr_url": "https://github.com/acme/widgets/pull/99",
        "reviewed_head_sha": "b" * 40,
        "result_head_sha": "e" * 40,
        "added_tests": ["tests/test_widget.py"],
        "scenarios": ["concurrent update"],
    }
    if outcome == "defect_found":
        value["reflection"] = "race is reproducible"
    return value


def stage_body(
    *,
    source_task_id: str,
    source_digest: str,
    bound_head_sha: str,
    pr_url: str = "https://github.com/acme/widgets/pull/99",
    source_run_id: int = 8,
) -> str:
    binding = {
        "bound_head_sha": bound_head_sha,
        "pr_url": pr_url,
        "reflection": None,
        "source_digest": source_digest,
        "source_run_id": source_run_id,
        "source_task_id": source_task_id,
    }
    return f"```json\n{json.dumps(binding)}\n```"


def bound_critic_card() -> dict[str, object]:
    return card(
        "critic",
        status="done",
        parent_id="reviewer",
        summary=critic_summary(outcome="pass"),
        body=stage_body(
            source_task_id="reviewer",
            source_digest=reviewer_digest(),
            bound_head_sha="b" * 40,
            source_run_id=2,
        ),
        run_id=3,
    )


def bound_reviewer_card(*, verdict: str = "approve") -> dict[str, object]:
    return card(
        "reviewer",
        status="done",
        parent_id="root",
        summary=reviewer_summary(verdict=verdict),
        body=stage_body(
            source_task_id="root",
            source_digest=executor_digest(),
            bound_head_sha="b" * 40,
            source_run_id=1,
        ),
        run_id=2,
    )


def bound_root_card() -> dict[str, object]:
    return card(
        "root",
        status="done",
        summary=executor_summary(),
        run_id=1,
    )


def pipeline_to_critic(
    critic: dict[str, object] | None = None,
) -> dict[str, dict[str, object]]:
    return {
        "github-issue:acme/widgets#7": bound_root_card(),
        "forge-stage:acme/widgets#7:reviewer:"
        + executor_digest()[:16]: bound_reviewer_card(),
        "forge-stage:acme/widgets#7:critic:"
        + reviewer_digest()[:16]: (
            critic if critic is not None else bound_critic_card()
        ),
    }


def test_projection_uses_issue_identity_from_keys_not_pr_number() -> None:
    mirror = load_mirror()
    root_key = "github-issue:acme/widgets#7"
    cards = pipeline_to_critic()

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
        {root_key: bound_root_card()},
        current_head_green=unexpected_ci,
    )

    assert targets[root_key] == "forge:need-review"


def test_reviewer_approve_frontier_projects_need_critic() -> None:
    mirror = load_mirror()
    root_key = "github-issue:acme/widgets#7"
    reviewer_key = (
        "forge-stage:acme/widgets#7:reviewer:" + executor_digest()[:16]
    )
    cards = {
        root_key: bound_root_card(),
        reviewer_key: bound_reviewer_card(),
    }

    targets = mirror.projection_targets(cards, current_head_green=lambda _: False)

    assert targets[root_key] == "forge:need-critic"


def test_reviewer_reject_frontier_projects_need_execution() -> None:
    mirror = load_mirror()
    root_key = "github-issue:acme/widgets#7"
    reviewer_key = (
        "forge-stage:acme/widgets#7:reviewer:" + executor_digest()[:16]
    )
    cards = {
        root_key: bound_root_card(),
        reviewer_key: bound_reviewer_card(verdict="reject"),
    }

    targets = mirror.projection_targets(cards, current_head_green=lambda _: False)

    assert targets[root_key] == "forge:need-execution"


def test_critic_pass_requires_injected_exact_head_ci_gate() -> None:
    mirror = load_mirror()
    root_key = "github-issue:acme/widgets#7"
    cards = pipeline_to_critic()

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
                "total_count": 1,
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
                "total_count": 1,
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
        return 0, json.dumps(
            {"total_count": 2, "check_runs": [check, check]}
        ), ""

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
        return 0, json.dumps({"total_count": 1, "check_runs": [check]}), ""

    monkeypatch.setattr(mirror, "sh", fake_sh)

    with pytest.raises(mirror.ProjectionError, match=message):
        mirror.critic_current_head_green(bound_critic_card())


def test_malformed_done_critic_result_is_not_treated_as_mergeable() -> None:
    mirror = load_mirror()
    invalid_critic = bound_critic_card()
    invalid_critic["summary"] = {"outcome": "pass"}
    cards = pipeline_to_critic(invalid_critic)

    with pytest.raises(mirror.ProjectionError, match="critic result"):
        mirror.projection_targets(cards, current_head_green=lambda _: True)


def test_unsuccessful_done_stage_run_is_rejected_before_projection() -> None:
    mirror = load_mirror()
    invalid_critic = bound_critic_card()
    invalid_critic["run_status"] = "failed"
    invalid_critic["run_outcome"] = "error"
    cards = pipeline_to_critic(invalid_critic)

    with pytest.raises(mirror.ProjectionError, match="successful completed run"):
        mirror.projection_targets(cards, current_head_green=lambda _: True)


def test_multiple_pipeline_leaves_fail_closed() -> None:
    mirror = load_mirror()
    root_key = "github-issue:acme/widgets#7"
    cards = {
        root_key: bound_root_card(),
        "forge-stage:acme/widgets#7:reviewer:" + executor_digest()[:16]: card(
            "reviewer-one",
            status="ready",
            parent_id="root",
            body=stage_body(
                source_task_id="root",
                source_digest=executor_digest(),
                bound_head_sha="b" * 40,
                source_run_id=1,
            ),
        ),
        "forge-stage:acme/widgets#7:reviewer:" + "f" * 16: card(
            "reviewer-two",
            status="ready",
            parent_id="root",
            body=stage_body(
                source_task_id="root",
                source_digest="f" * 64,
                bound_head_sha="b" * 40,
                source_run_id=1,
            ),
        ),
    }

    with pytest.raises(mirror.ProjectionError, match="frontier|digest"):
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
                "github-issue:immortal0900/INFINITY_FORGE#3-review",
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


def test_projection_rejects_root_to_critic_shortcut() -> None:
    mirror = load_mirror()
    cards = {
        "github-issue:acme/widgets#7": bound_root_card(),
        "forge-stage:acme/widgets#7:critic:" + "c" * 16: bound_critic_card(),
    }
    cards[next(key for key in cards if ":critic:" in key)]["parent_id"] = "root"

    with pytest.raises(mirror.ProjectionError, match="transition"):
        mirror.projection_targets(cards, current_head_green=lambda _: True)


def test_projection_binds_stage_receipt_to_key_and_parent() -> None:
    mirror = load_mirror()
    root_key = "github-issue:acme/widgets#7"
    reviewer_key = "forge-stage:acme/widgets#7:reviewer:" + "f" * 16
    cards = {
        root_key: bound_root_card(),
        reviewer_key: card(
            "reviewer",
            status="done",
            parent_id="root",
            summary=reviewer_summary(verdict="approve"),
            body=stage_body(
                source_task_id="different-parent",
                source_digest=executor_digest(),
                bound_head_sha="b" * 40,
                source_run_id=1,
            ),
        ),
    }

    with pytest.raises(mirror.ProjectionError, match="receipt"):
        mirror.projection_targets(cards, current_head_green=lambda _: False)


def test_projection_rejects_cross_repository_stage_result() -> None:
    mirror = load_mirror()
    root_key = "github-issue:acme/widgets#7"
    reviewer_key = (
        "forge-stage:acme/widgets#7:reviewer:" + executor_digest()[:16]
    )
    summary = reviewer_summary(verdict="approve")
    summary["pr_url"] = "https://github.com/other/repo/pull/99"
    cards = {
        root_key: bound_root_card(),
        reviewer_key: card(
            "reviewer",
            status="done",
            parent_id="root",
            summary=summary,
            body=stage_body(
                source_task_id="root",
                source_digest=executor_digest(),
                bound_head_sha="b" * 40,
                source_run_id=1,
                pr_url="https://github.com/other/repo/pull/99",
            ),
        ),
    }

    with pytest.raises(mirror.ProjectionError, match="repository"):
        mirror.projection_targets(cards, current_head_green=lambda _: False)


def test_projection_scope_excludes_unconfigured_repositories() -> None:
    mirror = load_mirror()
    cards = {
        "github-issue:acme/widgets#7": bound_root_card(),
        "github-issue:other/repo#8": card("root-b", status="done"),
    }

    targets = mirror.projection_targets(
        cards,
        current_head_green=lambda _: False,
        repositories=("acme/widgets",),
    )

    assert targets == {"github-issue:acme/widgets#7": "forge:need-review"}


def test_live_critic_gate_rejects_truncated_check_page(
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
                "total_count": 101,
                "check_runs": [
                    {
                        "name": "eval",
                        "status": "completed",
                        "conclusion": "success",
                        "head_sha": "e" * 40,
                    }
                ],
            }
        ), ""

    monkeypatch.setattr(mirror, "sh", fake_sh)

    with pytest.raises(mirror.ProjectionError, match="complete check-run set"):
        mirror.critic_current_head_green(bound_critic_card())


def test_project_issue_label_rejects_unknown_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mirror = load_mirror()
    monkeypatch.setattr(
        mirror,
        "_gh_json",
        lambda *args, **kwargs: {"state": "mystery", "labels": []},
    )

    with pytest.raises(mirror.ProjectionError, match="state"):
        mirror.project_issue_label("acme/widgets", 7, "forge:need-review")


def test_reviewer_reject_cannot_parent_a_critic() -> None:
    mirror = load_mirror()
    cards = pipeline_to_critic()
    reviewer = next(card for key, card in cards.items() if ":reviewer:" in key)
    reviewer["summary"] = reviewer_summary(verdict="reject")

    with pytest.raises(mirror.ProjectionError, match="reject|transition"):
        mirror.projection_targets(cards, current_head_green=lambda _: True)


def test_stage_receipt_digest_must_recompute_from_parent_run() -> None:
    mirror = load_mirror()
    cards = pipeline_to_critic()
    reviewer_key = next(key for key in cards if ":reviewer:" in key)
    reviewer = cards.pop(reviewer_key)
    reviewer["body"] = stage_body(
        source_task_id="root",
        source_digest="f" * 64,
        bound_head_sha="b" * 40,
    )
    cards["forge-stage:acme/widgets#7:reviewer:" + "f" * 16] = reviewer

    with pytest.raises(mirror.ProjectionError, match="digest|run"):
        mirror.projection_targets(cards, current_head_green=lambda _: True)


def test_projection_rejects_more_than_three_rework_cards() -> None:
    mirror = load_mirror()
    cards: dict[str, dict[str, object]] = {
        "github-issue:acme/widgets#7": card("root", status="done")
    }
    parent = "root"
    for index in range(4):
        key = (
            "forge-stage:acme/widgets#7:executor-rework:"
            + f"{index + 1:016x}"
        )
        cards[key] = card(
            f"rework-{index}",
            status="ready" if index == 3 else "done",
            parent_id=parent,
            body=stage_body(
                source_task_id=parent,
                source_digest=f"{index + 1:064x}",
                bound_head_sha="b" * 40,
            ),
        )
        parent = f"rework-{index}"

    with pytest.raises(mirror.ProjectionError, match="rework"):
        mirror.projection_targets(cards, current_head_green=lambda _: False)


def test_rework_receipt_must_copy_parent_reflection_exactly() -> None:
    mirror = load_mirror()
    reviewer = bound_reviewer_card(verdict="reject")
    reviewer_result = reviewer["summary"]
    assert isinstance(reviewer_result, dict)
    digest = transition_digest(
        task_id="reviewer",
        run_id=2,
        stage=PipelineStage.REVIEWER,
        summary=reviewer_result,
        metadata={},
        pr_url="https://github.com/acme/widgets/pull/99",
        head_sha="b" * 40,
    )
    cards = {
        "github-issue:acme/widgets#7": bound_root_card(),
        "forge-stage:acme/widgets#7:reviewer:"
        + executor_digest()[:16]: reviewer,
        "forge-stage:acme/widgets#7:executor-rework:" + digest[:16]: card(
            "rework",
            status="ready",
            parent_id="reviewer",
            body=stage_body(
                source_task_id="reviewer",
                source_digest=digest,
                bound_head_sha="b" * 40,
                source_run_id=2,
            ),
        ),
    }

    with pytest.raises(mirror.ProjectionError, match="reflection"):
        mirror.projection_targets(cards, current_head_green=lambda _: False)
