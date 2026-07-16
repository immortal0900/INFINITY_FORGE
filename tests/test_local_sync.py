from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "forge" / "scripts" / "local-sync.py"


def _module():
    spec = importlib.util.spec_from_file_location("local_sync", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_new_root_task_key_maps_to_its_github_issue() -> None:
    module = _module()

    assert module.task_reference(
        "forge-task:owner/repository#42:0123456789abcdef"
    ) == ("owner/repository", "42")


@pytest.mark.parametrize(
    "key",
    (
        "github-issue:owner/repository#42",
        "forge-task:owner/repository#42:short",
        "forge-task:repository#42:0123456789abcdef",
        "forge-task:owner/repository#no:0123456789abcdef",
    ),
)
def test_old_or_malformed_task_keys_are_rejected(key: str) -> None:
    module = _module()

    with pytest.raises(ValueError):
        module.task_reference(key)
