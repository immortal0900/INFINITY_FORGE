from __future__ import annotations

import importlib.util
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    path = ROOT / "forge" / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_task_flow_worker_runs_real_scan_contract_in_dry_run(monkeypatch, capsys) -> None:
    module = _load("task-flow-worker.py")
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        module,
        "run_task_flow_worker",
        lambda **kwargs: calls.append(kwargs) or (),
    )

    code = module.main(
        [
            "--db",
            "hermes.db",
            "--hermes",
            "hermes",
            "--gh",
            "gh",
            "--settings-db",
            "settings.db",
            "--outbox",
            "outbox.db",
            "--repo",
            "owner/repo",
            "--workspace",
            "/workspace",
            "--dry-run",
        ]
    )

    assert code == 0
    assert calls[0]["dry_run"] is True
    assert calls[0]["workspace"] == "dir:/workspace"
    assert json.loads(capsys.readouterr().out) == {"status": "ok", "tasks": []}


def test_task_flow_worker_returns_two_on_runtime_error(monkeypatch, capsys) -> None:
    module = _load("task-flow-worker.py")

    def fail(**kwargs):
        raise RuntimeError("database is invalid")

    monkeypatch.setattr(module, "run_task_flow_worker", fail)
    code = module.main(
        [
            "--db",
            "hermes.db",
            "--hermes",
            "hermes",
            "--gh",
            "gh",
            "--settings-db",
            "settings.db",
            "--outbox",
            "outbox.db",
            "--repo",
            "owner/repo",
            "--workspace",
            "/workspace",
        ]
    )

    assert code == 2
    assert json.loads(capsys.readouterr().err)["status"] == "error"


def test_issue_status_sync_scans_and_writes_exact_labels(monkeypatch, capsys) -> None:
    module = _load("issue-status-sync.py")
    writes: list[tuple[str, int, str]] = []
    settings = object()
    snapshot = SimpleNamespace(settings=settings)
    guarded: list[object] = []
    monkeypatch.setattr(module, "load_task_flow_snapshots", lambda **kwargs: (snapshot,))
    monkeypatch.setattr(module, "label_for_snapshot", lambda value: "forge:building")

    class Writer:
        def __init__(self, path: str) -> None:
            self.path = path

        def replace_status(self, repository: str, issue_number: int, label: str):
            writes.append((repository, issue_number, label))
            return (label,)

    monkeypatch.setattr(module, "GitHubIssueStatusClient", Writer)
    monkeypatch.setattr(module, "issue_number_for_snapshot", lambda value: 7)

    class SettingsStore:
        def __init__(self, path: str) -> None:
            assert path == "settings.db"

        @contextmanager
        def guard_active(self, expected: object):
            guarded.append(expected)
            yield

    monkeypatch.setattr(module, "TaskSettingsStore", SettingsStore)

    code = module.main(
        [
            "--db",
            "hermes.db",
            "--gh",
            "gh",
            "--settings-db",
            "settings.db",
            "--outbox",
            "outbox.db",
            "--repo",
            "owner/repo",
        ]
    )

    assert code == 0
    assert writes == [("owner/repo", 7, "forge:building")]
    assert guarded == [settings]
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


def test_issue_status_sync_returns_two_on_runtime_error(monkeypatch, capsys) -> None:
    module = _load("issue-status-sync.py")

    def fail(**kwargs):
        raise RuntimeError("GitHub readback failed")

    monkeypatch.setattr(module, "load_task_flow_snapshots", fail)
    code = module.main(
        [
            "--db",
            "hermes.db",
            "--gh",
            "gh",
            "--settings-db",
            "settings.db",
            "--outbox",
            "outbox.db",
            "--repo",
            "owner/repo",
        ]
    )

    assert code == 2
    assert json.loads(capsys.readouterr().err) == {
        "status": "error",
        "error": "GitHub readback failed",
    }
