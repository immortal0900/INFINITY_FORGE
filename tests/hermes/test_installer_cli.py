from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

from .test_installer import _hermes_tree


SCRIPT = Path("forge/scripts/install-hermes-change.py")


def _load_cli():
    spec = importlib.util.spec_from_file_location("install_hermes_change", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_runs_directly_from_the_repository_root() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "build" in result.stdout


def test_cli_build_install_restore(tmp_path: Path, capsys) -> None:
    cli = _load_cli()
    root = tmp_path / "hermes"
    package = tmp_path / "package"
    _hermes_tree(root)

    assert cli.main(
        [
            "build",
            "--hermes-root",
            str(root),
            "--package",
            str(package),
            "--source-version",
            "0.18.2-test",
        ]
    ) == 0
    assert cli.main(
        ["install", "--hermes-root", str(root), "--package", str(package)]
    ) == 0
    assert cli.main(
        ["restore", "--hermes-root", str(root), "--package", str(package)]
    ) == 0

    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [report["action"] for report in reports] == [
        "build",
        "install",
        "restore",
    ]
    assert all(report["ok"] is True for report in reports)
