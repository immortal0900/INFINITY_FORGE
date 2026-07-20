"""Keep Forge tools on the user-facing Hermes profile only.

Configuration mutations go through Hermes' own ``load_config`` / ``save_config``
API.  Byte-for-byte file copies are used only for deployment rollback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Iterator, Sequence


FORGE_PLUGIN = "infinity-forge"
FORGE_TOOLSET = "forge"
WORKER_NAMES = ("builder", "reviewer", "deep_checker", "fix")
_BACKUP_FORMAT = "infinity-forge-hermes-config-backup/v1"


class HermesToolsetError(RuntimeError):
    """Raised when Hermes tool visibility cannot be changed safely."""


@contextmanager
def _hermes_home(home: Path) -> Iterator[tuple[object, object, Path]]:
    """Yield Hermes' config API while it is scoped to one profile home."""

    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )
    from hermes_cli.config import get_config_path, load_config, save_config

    resolved_home = home.expanduser().resolve()
    token = set_hermes_home_override(str(resolved_home))
    try:
        config_path = Path(get_config_path()).resolve()
        expected = (resolved_home / "config.yaml").resolve()
        if config_path != expected:
            raise HermesToolsetError(
                f"Hermes config path escaped its profile home: {config_path}"
            )
        yield load_config, save_config, config_path
    finally:
        reset_hermes_home_override(token)


def _name_list(value: object, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise HermesToolsetError(f"{field} must be a list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, (str, int)):
            raise HermesToolsetError(f"{field} contains an invalid name")
        name = str(item)
        if name not in result:
            result.append(name)
    return result


def _mapping(config: dict, key: str) -> dict:
    value = config.get(key)
    if value is None:
        value = {}
        config[key] = value
    if not isinstance(value, dict):
        raise HermesToolsetError(f"{key} must be a mapping")
    return value


def _platform_lists(config: dict) -> dict:
    platforms = _mapping(config, "platform_toolsets")
    for platform, toolsets in platforms.items():
        if not isinstance(platform, str) or not platform:
            raise HermesToolsetError("platform_toolsets contains an invalid platform")
        platforms[platform] = _name_list(
            toolsets,
            f"platform_toolsets.{platform}",
        )
    return platforms


def _plugin_lists(config: dict) -> tuple[list[str], list[str]]:
    plugins = _mapping(config, "plugins")
    enabled = _name_list(plugins.get("enabled"), "plugins.enabled")
    disabled = _name_list(plugins.get("disabled"), "plugins.disabled")
    return enabled, disabled


def _default_cli_toolset() -> str:
    from hermes_cli.platforms import PLATFORMS

    cli = PLATFORMS.get("cli")
    default = getattr(cli, "default_toolset", None)
    if not isinstance(default, str) or not default:
        raise HermesToolsetError("Hermes CLI default toolset is unavailable")
    return default


def _main_config(config: dict) -> dict:
    result = deepcopy(config)
    platforms = _platform_lists(result)
    if "cli" not in platforms:
        platforms["cli"] = [_default_cli_toolset()]
    for toolsets in platforms.values():
        toolsets[:] = [name for name in toolsets if name != FORGE_TOOLSET]
        toolsets.append(FORGE_TOOLSET)

    enabled, disabled = _plugin_lists(result)
    enabled = [name for name in enabled if name != FORGE_PLUGIN]
    enabled.append(FORGE_PLUGIN)
    disabled = [name for name in disabled if name != FORGE_PLUGIN]
    result["plugins"]["enabled"] = enabled
    result["plugins"]["disabled"] = disabled
    return result


def _worker_config(config: dict) -> dict:
    result = deepcopy(config)
    platforms = _platform_lists(result)
    for toolsets in platforms.values():
        toolsets[:] = [name for name in toolsets if name != FORGE_TOOLSET]

    # Mark Forge as a known-but-unselected plugin toolset.  This prevents
    # Hermes' new-plugin default from exposing it if a worker can discover it.
    known = _mapping(result, "known_plugin_toolsets")
    for platform in set(platforms) | {"cli"}:
        names = _name_list(
            known.get(platform),
            f"known_plugin_toolsets.{platform}",
        )
        names = [name for name in names if name != FORGE_TOOLSET]
        names.append(FORGE_TOOLSET)
        known[platform] = names

    enabled, disabled = _plugin_lists(result)
    enabled = [name for name in enabled if name != FORGE_PLUGIN]
    disabled = [name for name in disabled if name != FORGE_PLUGIN]
    disabled.append(FORGE_PLUGIN)
    result["plugins"]["enabled"] = enabled
    result["plugins"]["disabled"] = disabled
    return result


def _read_config(home: Path) -> dict:
    with _hermes_home(home) as (load_config, _save_config, _path):
        config = load_config()
    if not isinstance(config, dict):
        raise HermesToolsetError("Hermes load_config() did not return a mapping")
    return config


def _write_and_read_back(home: Path, expected: dict) -> dict:
    with _hermes_home(home) as (load_config, save_config, _path):
        # RISK(configuration): save only the profile selected by the explicit
        # Hermes-home override, then read it back before deployment continues.
        save_config(expected)
        actual = load_config()
    if not isinstance(actual, dict):
        raise HermesToolsetError("Hermes config readback was not a mapping")
    return actual


def _verify_main(config: dict) -> None:
    platforms = _platform_lists(deepcopy(config))
    if "cli" not in platforms:
        raise HermesToolsetError("main profile has no explicit CLI toolsets")
    if any(toolsets.count(FORGE_TOOLSET) != 1 for toolsets in platforms.values()):
        raise HermesToolsetError(
            "main profile must contain Forge exactly once on every configured surface"
        )
    enabled, disabled = _plugin_lists(deepcopy(config))
    if enabled.count(FORGE_PLUGIN) != 1 or FORGE_PLUGIN in disabled:
        raise HermesToolsetError("main profile does not enable Infinity Forge exactly once")


def _verify_worker(config: dict) -> None:
    platforms = _platform_lists(deepcopy(config))
    if any(FORGE_TOOLSET in toolsets for toolsets in platforms.values()):
        raise HermesToolsetError("worker profile exposes Forge tools")
    enabled, disabled = _plugin_lists(deepcopy(config))
    if FORGE_PLUGIN in enabled or disabled.count(FORGE_PLUGIN) != 1:
        raise HermesToolsetError("worker profile does not explicitly disable Infinity Forge")
    known = _mapping(deepcopy(config), "known_plugin_toolsets")
    for platform in set(platforms) | {"cli"}:
        names = _name_list(
            known.get(platform),
            f"known_plugin_toolsets.{platform}",
        )
        if names.count(FORGE_TOOLSET) != 1:
            raise HermesToolsetError(
                f"worker profile does not explicitly hide Forge on {platform}"
            )


def apply_policy(main_home: Path, worker_homes: Sequence[Path]) -> None:
    """Apply and immediately read back the main/worker visibility policy."""

    main = _main_config(_read_config(main_home))
    _verify_main(_write_and_read_back(main_home, main))
    for worker_home in worker_homes:
        worker = _worker_config(_read_config(worker_home))
        _verify_worker(_write_and_read_back(worker_home, worker))


def verify_policy(main_home: Path, worker_homes: Sequence[Path]) -> None:
    """Read current Hermes configs and fail unless the policy still holds."""

    _verify_main(_read_config(main_home))
    for worker_home in worker_homes:
        _verify_worker(_read_config(worker_home))


def _manifest_path(backup_root: Path) -> Path:
    return backup_root / "manifest.json"


def _read_manifest(backup_root: Path) -> dict:
    path = _manifest_path(backup_root)
    if not path.is_file() or path.is_symlink():
        raise HermesToolsetError("Hermes config backup manifest is missing")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HermesToolsetError("Hermes config backup manifest is invalid") from error
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != _BACKUP_FORMAT
        or not isinstance(manifest.get("records"), list)
    ):
        raise HermesToolsetError("Hermes config backup manifest is invalid")
    return manifest


def _write_manifest(backup_root: Path, manifest: dict) -> None:
    backup_root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(backup_root, 0o700)
    except OSError:
        pass
    payload = json.dumps(manifest, ensure_ascii=True, sort_keys=True, indent=2)
    fd, temp_name = tempfile.mkstemp(
        dir=backup_root,
        prefix=".manifest-",
        suffix=".json",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.write("\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, _manifest_path(backup_root))
    finally:
        try:
            Path(temp_name).unlink()
        except FileNotFoundError:
            pass


def backup_configs(backup_root: Path, homes: Sequence[Path]) -> None:
    """Append exact config-file snapshots for the supplied Hermes homes."""

    backup_root = backup_root.expanduser().resolve()
    manifest_path = _manifest_path(backup_root)
    manifest = (
        _read_manifest(backup_root)
        if manifest_path.exists()
        else {"format": _BACKUP_FORMAT, "records": []}
    )
    recorded = {record.get("home") for record in manifest["records"]}
    for home in homes:
        resolved_home = home.expanduser().resolve()
        home_text = str(resolved_home)
        if home_text in recorded:
            continue
        with _hermes_home(resolved_home) as (
            _load_config,
            _save_config,
            config_path,
        ):
            pass
        if config_path.is_symlink():
            raise HermesToolsetError("refusing to back up a symbolic-link config")
        present = config_path.exists()
        if present and not config_path.is_file():
            raise HermesToolsetError("Hermes config path is not a regular file")
        record: dict[str, object] = {
            "home": home_text,
            "present": present,
            "backup_file": None,
            "sha256": None,
            "mode": None,
        }
        if present:
            payload = config_path.read_bytes()
            backup_name = f"config-{len(manifest['records']):02d}.yaml"
            backup_path = backup_root / backup_name
            backup_root.mkdir(parents=True, exist_ok=True)
            if backup_path.exists():
                raise HermesToolsetError("Hermes config backup file already exists")
            backup_path.write_bytes(payload)
            os.chmod(backup_path, 0o600)
            record.update(
                {
                    "backup_file": backup_name,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "mode": stat.S_IMODE(config_path.stat().st_mode),
                }
            )
        manifest["records"].append(record)
        recorded.add(home_text)
    _write_manifest(backup_root, manifest)


def restore_configs(backup_root: Path) -> None:
    """Restore every exact config snapshot recorded by ``backup_configs``."""

    backup_root = backup_root.expanduser().resolve()
    manifest = _read_manifest(backup_root)
    for record in manifest["records"]:
        if not isinstance(record, dict) or not isinstance(record.get("home"), str):
            raise HermesToolsetError("Hermes config backup record is invalid")
        home = Path(record["home"]).resolve()
        with _hermes_home(home) as (_load_config, _save_config, config_path):
            pass
        present = record.get("present")
        if present is False:
            if config_path.is_symlink():
                raise HermesToolsetError("refusing to remove a symbolic-link config")
            if config_path.exists():
                if not config_path.is_file():
                    raise HermesToolsetError("Hermes config path is not a regular file")
                # RISK(data-loss): this exact profile config did not exist in
                # the deployment snapshot and is the only path removed.
                config_path.unlink()
            continue
        backup_file = record.get("backup_file")
        expected_hash = record.get("sha256")
        mode = record.get("mode")
        if (
            present is not True
            or not isinstance(backup_file, str)
            or Path(backup_file).name != backup_file
            or not isinstance(expected_hash, str)
            or not isinstance(mode, int)
        ):
            raise HermesToolsetError("Hermes config backup record is invalid")
        backup_path = backup_root / backup_file
        if not backup_path.is_file() or backup_path.is_symlink():
            raise HermesToolsetError("Hermes config backup payload is missing")
        payload = backup_path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != expected_hash:
            raise HermesToolsetError("Hermes config backup payload changed")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            dir=config_path.parent,
            prefix=".infinity-forge-config-rollback-",
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, mode)
            # RISK(configuration): replace only the validated profile config.
            os.replace(temp_name, config_path)
        finally:
            try:
                Path(temp_name).unlink()
            except FileNotFoundError:
                pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup", "apply", "restore", "verify"))
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--main-home", type=Path)
    parser.add_argument("--worker-home", type=Path, action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.action == "restore":
        if args.backup is None:
            raise HermesToolsetError("restore requires --backup")
        restore_configs(args.backup)
        return 0
    if args.action == "backup":
        if args.backup is None:
            raise HermesToolsetError("backup requires --backup")
        homes = ([args.main_home] if args.main_home is not None else []) + list(
            args.worker_home
        )
        if not homes:
            raise HermesToolsetError("backup requires at least one Hermes home")
        backup_configs(args.backup, homes)
        return 0
    if args.main_home is None:
        raise HermesToolsetError(f"{args.action} requires --main-home")
    if args.action == "apply":
        apply_policy(args.main_home, args.worker_home)
    else:
        verify_policy(args.main_home, args.worker_home)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by deploy scripts
    raise SystemExit(main())
