"""Hermes profile tool visibility policy and rollback coverage."""

from __future__ import annotations

import copy
import sys
import types
from pathlib import Path

import pytest

from forge.ops import hermes_toolsets


@pytest.fixture
def fake_hermes_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    main = tmp_path / "main"
    worker = tmp_path / "worker"
    main.mkdir()
    worker.mkdir()
    current: dict[str, str | None] = {"home": None}
    configs = {
        str(main.resolve()): {
            "platform_toolsets": {
                "cli": ["terminal", "forge", "terminal", "forge"],
                "slack": ["web"],
            },
            "known_plugin_toolsets": {"cli": ["forge"]},
            "plugins": {
                "enabled": ["other"],
                "disabled": ["infinity-forge", "other-disabled"],
            },
        },
        str(worker.resolve()): {
            "platform_toolsets": {
                "cli": ["terminal", "forge", "forge"],
                "slack": ["forge", "web"],
            },
            "known_plugin_toolsets": {"cli": [], "slack": ["other"]},
            "plugins": {
                "enabled": ["infinity-forge", "other"],
                "disabled": [],
            },
        },
    }
    saves: list[str] = []
    loads: list[str] = []

    def set_home(value: str) -> str | None:
        previous = current["home"]
        current["home"] = str(Path(value).resolve())
        return previous

    def reset_home(token: str | None) -> None:
        current["home"] = token

    def load_config() -> dict:
        assert current["home"] is not None
        loads.append(current["home"])
        return copy.deepcopy(configs[current["home"]])

    def save_config(config: dict, **_kwargs: object) -> None:
        assert current["home"] is not None
        configs[current["home"]] = copy.deepcopy(config)
        saves.append(current["home"])

    def get_config_path() -> Path:
        assert current["home"] is not None
        return Path(current["home"]) / "config.yaml"

    constants = types.ModuleType("hermes_constants")
    constants.set_hermes_home_override = set_home
    constants.reset_hermes_home_override = reset_home
    hermes_cli = types.ModuleType("hermes_cli")
    config = types.ModuleType("hermes_cli.config")
    config.load_config = load_config
    config.save_config = save_config
    config.get_config_path = get_config_path
    platforms = types.ModuleType("hermes_cli.platforms")
    platforms.PLATFORMS = {
        "cli": types.SimpleNamespace(default_toolset="hermes-cli"),
    }
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config)
    monkeypatch.setitem(sys.modules, "hermes_cli.platforms", platforms)
    return main, worker, configs, saves, loads


def test_apply_preserves_main_lists_and_removes_tools_from_workers(
    fake_hermes_config,
) -> None:
    main, worker, configs, saves, loads = fake_hermes_config

    hermes_toolsets.apply_policy(main, [worker])

    main_config = configs[str(main.resolve())]
    assert main_config["platform_toolsets"] == {
        "cli": ["terminal", "forge"],
        "slack": ["web", "forge"],
    }
    assert main_config["plugins"] == {
        "enabled": ["other", "infinity-forge"],
        "disabled": ["other-disabled"],
    }

    worker_config = configs[str(worker.resolve())]
    assert worker_config["platform_toolsets"] == {
        "cli": ["terminal"],
        "slack": ["web"],
    }
    assert worker_config["known_plugin_toolsets"] == {
        "cli": ["forge"],
        "slack": ["other", "forge"],
    }
    assert worker_config["plugins"] == {
        "enabled": ["other"],
        "disabled": ["infinity-forge"],
    }
    assert saves == [str(main.resolve()), str(worker.resolve())]
    assert loads.count(str(main.resolve())) >= 2
    assert loads.count(str(worker.resolve())) >= 2
    hermes_toolsets.verify_policy(main, [worker])


def test_apply_keeps_default_cli_surface_when_main_has_no_explicit_lists(
    fake_hermes_config,
) -> None:
    main, worker, configs, _saves, _loads = fake_hermes_config
    configs[str(main.resolve())].pop("platform_toolsets")

    hermes_toolsets.apply_policy(main, [worker])

    assert configs[str(main.resolve())]["platform_toolsets"]["cli"] == [
        "hermes-cli",
        "forge",
    ]


def test_config_backup_restores_exact_bytes(fake_hermes_config, tmp_path: Path) -> None:
    main, worker, _configs, _saves, _loads = fake_hermes_config
    main_config = main / "config.yaml"
    worker_config = worker / "config.yaml"
    main_config.write_bytes(b"main: before\n")
    worker_config.write_bytes(b"worker: before\n")
    backup = tmp_path / "backup"

    hermes_toolsets.backup_configs(backup, [main, worker])
    main_config.write_bytes(b"main: changed\n")
    worker_config.write_bytes(b"worker: changed\n")
    hermes_toolsets.restore_configs(backup)

    assert main_config.read_bytes() == b"main: before\n"
    assert worker_config.read_bytes() == b"worker: before\n"
