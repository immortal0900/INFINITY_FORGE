"""Apply and verify subscription-only Codex and Claude runtime configuration."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import tomllib
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .codex_subscription_probe import CodexAppServerProbe
from .subscription_runtime import (
    is_claude_subscription_auth,
    scrub_subscription_environment,
)


_RUNTIME = "codex_app_server"
_CLAUDE_SERVER = "hermes-tools"
_CLAUDE_MODULE = "agent.transports.hermes_tools_mcp_server"
_PREFLIGHT_TIMEOUT_SECONDS = 10.0
_BACKUP_NAMES = {
    "hermes": "hermes-config.bin",
    "codex": "codex-config.bin",
    "claude": "claude-mcp.bin",
}
_BACKUP_SUFFIX = re.compile(r"\A\d{8}T\d{12}Z-[0-9a-f]{8}\Z")
_MIGRATION_CAPTURE_LOCK = threading.Lock()


@dataclass(frozen=True)
class SubscriptionReadiness:
    ready: bool
    runtime: str | None
    codex_account: str | None
    claude_subscription: str | None
    mcp: bool
    rollback_required: bool
    error: str | None


@dataclass(frozen=True)
class _Snapshot:
    existed: bool
    content: bytes


@dataclass(frozen=True)
class _RuntimeSwitchOutcome:
    status: object
    migration_report: object | None


def _apply_runtime_switch_with_captured_migration(
    config: dict[str, Any],
    value: str,
    *,
    persist_callback: Callable[[dict[str, Any]], None],
    runtime_switch_apply: Callable[..., object],
    migration_module: object,
    migration_excluded_mcp_names: frozenset[str] = frozenset(),
) -> _RuntimeSwitchOutcome:
    # The Hermes helper imports migrate inside apply(). Serialize and always restore
    # the temporary observer so one helper-owned plugin discovery is authoritative.
    with _MIGRATION_CAPTURE_LOCK:
        reports: list[object] = []
        original_migrate = getattr(migration_module, "migrate")
        owner_thread = threading.get_ident()

        def observe_migration(*args: object, **kwargs: object) -> object:
            migration_args = args
            if (
                migration_excluded_mcp_names
                and args
                and isinstance(args[0], dict)
            ):
                migration_config = copy.deepcopy(args[0])
                servers = migration_config.get("mcp_servers")
                if isinstance(servers, dict):
                    migration_config["mcp_servers"] = {
                        name: server
                        for name, server in servers.items()
                        if name not in migration_excluded_mcp_names
                    }
                migration_args = (migration_config, *args[1:])
            report = original_migrate(*migration_args, **kwargs)
            if threading.get_ident() == owner_thread:
                reports.append(report)
            return report

        setattr(migration_module, "migrate", observe_migration)
        try:
            status = runtime_switch_apply(
                config,
                value,
                persist_callback=persist_callback,
            )
        finally:
            setattr(migration_module, "migrate", original_migrate)
        return _RuntimeSwitchOutcome(
            status=status,
            migration_report=reports[0] if len(reports) == 1 else None,
        )


def _default_runtime_switch_apply(
    config: dict[str, Any],
    value: str,
    *,
    persist_callback: Callable[[dict[str, Any]], None],
    migration_excluded_mcp_names: frozenset[str] = frozenset(),
) -> object:
    from hermes_cli import codex_runtime_plugin_migration
    from hermes_cli.codex_runtime_switch import apply

    return _apply_runtime_switch_with_captured_migration(
        config,
        value,
        persist_callback=persist_callback,
        runtime_switch_apply=apply,
        migration_module=codex_runtime_plugin_migration,
        migration_excluded_mcp_names=migration_excluded_mcp_names,
    )


def _default_mcp_migration_apply(config: dict[str, Any], *, codex_home: Path) -> object:
    from hermes_cli.codex_runtime_plugin_migration import migrate

    return migrate(config, codex_home=codex_home)


def _default_claude_auth_status(
    claude_bin: str, env: Mapping[str, str]
) -> Mapping[str, object]:
    # RISK(security): bound the credential-bearing auth query and discard stderr.
    try:
        completed = subprocess.run(
            [claude_bin, "auth", "status", "--json"],
            env=dict(env),
            text=True,
            encoding="utf-8",
            errors="strict",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            check=False,
            timeout=_PREFLIGHT_TIMEOUT_SECONDS,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return {}
    if completed.returncode != 0:
        return {}
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, Mapping) else {}


class SubscriptionRuntimeSetup:
    """Own the small managed configuration surface and its exact-byte backup."""

    def __init__(
        self,
        *,
        forge_root: Path,
        hermes_root: Path,
        codex_home: Path | None = None,
        hermes_python: Path | None = None,
        codex_bin: str = "codex",
        claude_bin: str = "claude",
        probe: object | None = None,
        claude_auth_reader: (
            Callable[[str, Mapping[str, str]], Mapping[str, object]] | None
        ) = None,
        runtime_switch_apply: Callable[..., object] | None = None,
        mcp_migration_apply: Callable[..., object] | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.forge_root = Path(forge_root).resolve()
        self.hermes_root = Path(hermes_root).resolve()
        self._hermes_root_anchor = self.hermes_root
        self.environment = dict(os.environ if environment is None else environment)
        # Hermes v0.18.2 migration writes only to this exact home-relative target.
        configured_codex_home = codex_home or (Path.home() / ".codex")
        self.codex_home = Path(configured_codex_home).resolve()
        self._codex_home_anchor = self.codex_home
        default_python = (
            self.hermes_root
            / "hermes-agent"
            / "venv"
            / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        self.hermes_python = Path(hermes_python or default_python).resolve()
        self.codex_bin = codex_bin
        self.claude_bin = claude_bin
        self._probe = probe or CodexAppServerProbe()
        self._claude_auth_reader = claude_auth_reader or _default_claude_auth_status
        self._uses_default_switch = runtime_switch_apply is None
        self._runtime_switch_apply = (
            runtime_switch_apply or _default_runtime_switch_apply
        )
        self._mcp_migration_apply = mcp_migration_apply or _default_mcp_migration_apply

        self.hermes_config_path = self.hermes_root / "config.yaml"
        self.codex_config_path = self.codex_home / "config.toml"
        self.managed_root = self.hermes_root / "infinity-forge" / "subscription-runtime"
        self.claude_mcp_path = self.managed_root / "claude-mcp.json"
        self.manifest_path = self.managed_root / "backup-manifest.json"
        self.rollback_tombstone_path = self.managed_root / "rollback-completed.json"
        self.backup_root = self.managed_root / "backups"

    @property
    def backup_paths(self) -> tuple[Path, ...]:
        manifest = self._load_manifest()
        return self._backup_paths_for(self._backup_suffix(manifest))

    def apply(self) -> SubscriptionReadiness:
        preflight = self._preflight()
        if preflight is not None:
            return preflight

        paths = self._managed_targets()
        created_baseline = not self.manifest_path.exists()
        transaction: dict[str, _Snapshot] | None = None
        try:
            self._assert_managed_paths_safe()
            transaction = self._capture_snapshots(paths)
            self._ensure_baseline(transaction)
            config = self._load_hermes_config()
            migration_excluded_mcp_names = frozenset()
            if self._uses_default_switch:
                migration_excluded_mcp_names = frozenset(
                    self._codex_mcp_names() & _expected_mcp_names(config)
                )
            persist_failed = False

            def persist_safely(updated: dict[str, Any]) -> None:
                nonlocal persist_failed
                try:
                    self._assert_managed_paths_safe()
                    self._save_hermes_config(updated)
                except (Exception, KeyboardInterrupt, SystemExit):
                    # The installed helper logs callback exceptions with details.
                    persist_failed = True

            if self._uses_default_switch:
                switch_outcome = self._runtime_switch_apply(
                    config,
                    _RUNTIME,
                    persist_callback=persist_safely,
                    migration_excluded_mcp_names=migration_excluded_mcp_names,
                )
            else:
                switch_outcome = self._runtime_switch_apply(
                    config,
                    _RUNTIME,
                    persist_callback=persist_safely,
                )
            if self._uses_default_switch:
                if not isinstance(switch_outcome, _RuntimeSwitchOutcome):
                    raise RuntimeError("runtime switch adapter rejected")
                status = switch_outcome.status
                migration = switch_outcome.migration_report
            else:
                status = switch_outcome
                migration = None
            if persist_failed or getattr(status, "success", False) is not True:
                raise RuntimeError("runtime switch rejected")
            self._assert_managed_paths_safe()
            persisted_config = self._load_hermes_config()
            if persisted_config.get("model", {}).get("openai_runtime") != _RUNTIME:
                raise RuntimeError("runtime post-check failed")
            expected_mcp = _expected_mcp_names(persisted_config)
            if not self._uses_default_switch:
                migration = self._mcp_migration_apply(
                    persisted_config,
                    codex_home=self.codex_home,
                )
            self._assert_managed_paths_safe()
            migrated = getattr(migration, "migrated", None)
            required_migrated = (
                expected_mcp - migration_excluded_mcp_names
            ) | {_CLAUDE_SERVER}
            if (
                getattr(migration, "written", False) is not True
                or getattr(migration, "plugin_query_error", None) is not None
                or not isinstance(migrated, (list, tuple, set))
                or not all(isinstance(name, str) for name in migrated)
                or not required_migrated <= set(migrated)
            ):
                raise RuntimeError("MCP migration rejected")
            if not expected_mcp | {_CLAUDE_SERVER} <= self._codex_mcp_names():
                raise RuntimeError("MCP migration post-check failed")
            self._write_claude_mcp()
            readiness = self.verify()
            if not readiness.ready:
                raise RuntimeError("readiness post-check failed")
            self._discard_rollback_tombstone()
            return readiness
        except (Exception, KeyboardInterrupt, SystemExit):
            restored = (
                self._restore_snapshots(paths, transaction)
                if transaction is not None
                else True
            )
            if created_baseline and restored and self.manifest_path.exists():
                try:
                    self._discard_baseline()
                except (Exception, KeyboardInterrupt, SystemExit):
                    return _failure(
                        "runtime configuration failed", rollback_required=True
                    )
            return _failure(
                "runtime configuration failed", rollback_required=not restored
            )

    def verify(self) -> SubscriptionReadiness:
        preflight = self._preflight()
        if preflight is not None:
            return preflight
        try:
            config = self._load_hermes_config()
            runtime_ready = config.get("model", {}).get("openai_runtime") == _RUNTIME
            expected = _expected_mcp_names(config) | {_CLAUDE_SERVER}
            codex_mcp_ready = expected <= self._codex_mcp_names()
            claude_ready = self._claude_mcp_is_strict()
            mcp_ready = codex_mcp_ready and claude_ready
            ready = runtime_ready and mcp_ready
            return SubscriptionReadiness(
                ready,
                _RUNTIME if runtime_ready else None,
                "chatgpt",
                "claude.ai",
                mcp_ready,
                False,
                None if ready else "runtime verification failed",
            )
        except Exception:
            return _failure("runtime verification failed")

    def rollback(self) -> SubscriptionReadiness:
        paths = self._managed_targets()
        mutation_started = False
        try:
            self._assert_managed_paths_safe()
            if not self.manifest_path.is_file():
                self._validate_completed_rollback()
                return SubscriptionReadiness(
                    False, None, None, None, False, False, None
                )
            current = self._capture_snapshots(paths)
            baseline = self._baseline_snapshots()
            # RISK(data-loss): restoration overwrites managed config with exact backups.
            mutation_started = True
            if not self._restore_snapshots(paths, baseline):
                raise OSError
            self._deactivate_baseline(baseline)
            return SubscriptionReadiness(False, None, None, None, False, False, None)
        except (Exception, KeyboardInterrupt, SystemExit):
            restored = not mutation_started
            if mutation_started and "current" in locals():
                restored = self._restore_snapshots(paths, current)
            return _failure("managed backup is invalid", rollback_required=not restored)

    def _preflight(self) -> SubscriptionReadiness | None:
        try:
            self._assert_managed_paths_safe()
        except (OSError, ValueError):
            return _failure("managed path preflight failed")
        if not self.forge_root.is_dir() or not self.hermes_config_path.is_file():
            return _failure("managed path preflight failed")
        if (
            self._uses_default_switch
            and self.codex_home != (Path.home() / ".codex").resolve()
        ):
            return _failure("managed path preflight failed")
        if (
            not self.hermes_python.is_absolute()
            or not self.hermes_python.is_file()
            or (os.name != "nt" and not os.access(self.hermes_python, os.X_OK))
        ):
            return _failure("Hermes runtime preflight failed")
        try:
            self._load_hermes_config()
        except (OSError, UnicodeError, yaml.YAMLError, ValueError):
            return _failure("Hermes runtime preflight failed")
        child_environment = scrub_subscription_environment(self.environment)
        try:
            # RISK(security): neither auth probe receives API-billing credentials.
            auth = self._claude_auth_reader(self.claude_bin, child_environment)
        except Exception:
            return _failure("Claude.ai subscription preflight failed")
        if not is_claude_subscription_auth(auth):
            return _failure("Claude.ai subscription preflight failed")
        try:
            snapshot = self._probe.probe(
                self.codex_bin, child_environment, timeout=10.0
            )
        except Exception:
            return _failure("Codex subscription preflight failed")
        if getattr(snapshot, "account_type", None) != "chatgpt":
            return _failure("Codex subscription preflight failed")
        return None

    def _managed_targets(self) -> dict[str, Path]:
        return {
            "hermes": self.hermes_config_path,
            "codex": self.codex_config_path,
            "claude": self.claude_mcp_path,
        }

    def _assert_managed_paths_safe(self) -> None:
        if (
            not self.hermes_root.is_dir()
            or self.hermes_root.resolve(strict=True) != self._hermes_root_anchor
            or self.codex_home.resolve(strict=False) != self._codex_home_anchor
            or _is_link_like(self.hermes_root)
            or _is_link_like(self.codex_home)
            or _is_link_like(self.hermes_config_path)
            or _is_link_like(self.codex_config_path)
        ):
            raise ValueError

        hermes_root = self.hermes_root.resolve(strict=True)
        managed_paths = (
            self.managed_root,
            self.backup_root,
            self.manifest_path,
            self.rollback_tombstone_path,
            self.claude_mcp_path,
        )
        for path in managed_paths:
            if _has_symlink_component(hermes_root, path):
                raise ValueError
            if not path.resolve(strict=False).is_relative_to(hermes_root):
                raise ValueError

        if self.hermes_config_path.parent.resolve(strict=True) != hermes_root:
            raise ValueError
        codex_root = self.codex_home.resolve(strict=False)
        if self.codex_config_path.parent.resolve(strict=False) != codex_root:
            raise ValueError

    def _capture_snapshots(self, paths: Mapping[str, Path]) -> dict[str, _Snapshot]:
        snapshots: dict[str, _Snapshot] = {}
        for name, path in paths.items():
            self._assert_managed_paths_safe()
            snapshots[name] = _snapshot(path)
        return snapshots

    def _ensure_baseline(self, snapshots: Mapping[str, _Snapshot]) -> None:
        self._assert_managed_paths_safe()
        if self.manifest_path.exists():
            self._baseline_snapshots()
            return
        created_at = datetime.now(timezone.utc)
        backup_suffix = created_at.strftime("%Y%m%dT%H%M%S%fZ-") + uuid.uuid4().hex[:8]
        backup_paths = dict(zip(_BACKUP_NAMES, self._backup_paths_for(backup_suffix)))
        files: dict[str, object] = {}
        try:
            for name, base_filename in _BACKUP_NAMES.items():
                self._assert_managed_paths_safe()
                content = snapshots[name].content
                backup_path = backup_paths[name]
                _atomic_write(backup_path, content)
                files[name] = {
                    "backup": backup_path.name,
                    "existed": snapshots[name].existed,
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            self._assert_managed_paths_safe()
            _atomic_write(
                self.manifest_path,
                (
                    json.dumps(
                        {
                            "version": 1,
                            "created_at": created_at.isoformat(),
                            "backup_suffix": backup_suffix,
                            "files": files,
                        },
                        sort_keys=True,
                    )
                    + "\n"
                ).encode("utf-8"),
            )
        except BaseException:
            for backup_path in backup_paths.values():
                backup_path.unlink(missing_ok=True)
            raise

    def _load_manifest(self, path: Path | None = None) -> Mapping[str, Any]:
        self._assert_managed_paths_safe()
        selected = self.manifest_path if path is None else path
        if _is_link_like(selected):
            raise ValueError
        if selected != self.manifest_path and selected.parent.resolve(
            strict=False
        ) != self.backup_root.resolve(strict=False):
            raise ValueError
        payload = json.loads(selected.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("version") != 1
            or not isinstance(payload.get("files"), dict)
        ):
            raise ValueError
        return payload

    def _baseline_snapshots(
        self, manifest_path: Path | None = None
    ) -> dict[str, _Snapshot]:
        manifest = self._load_manifest(manifest_path)
        backup_suffix = self._backup_suffix(manifest)
        baseline: dict[str, _Snapshot] = {}
        for name, base_filename in _BACKUP_NAMES.items():
            self._assert_managed_paths_safe()
            expected_filename = f"{base_filename}.{backup_suffix}"
            entry = manifest["files"][name]
            if (
                not isinstance(entry, dict)
                or entry.get("backup") != expected_filename
                or not isinstance(entry.get("existed"), bool)
            ):
                raise ValueError
            backup_path = self.backup_root / expected_filename
            if (
                _is_link_like(backup_path)
                or backup_path.parent.resolve() != self.backup_root.resolve()
            ):
                raise ValueError
            content = backup_path.read_bytes()
            if hashlib.sha256(content).hexdigest() != entry.get("sha256"):
                raise ValueError
            baseline[name] = _Snapshot(entry["existed"], content)
        return baseline

    def _backup_suffix(self, manifest: Mapping[str, Any]) -> str:
        suffix = manifest.get("backup_suffix")
        if not isinstance(suffix, str) or _BACKUP_SUFFIX.fullmatch(suffix) is None:
            raise ValueError
        return suffix

    def _backup_paths_for(self, suffix: str) -> tuple[Path, ...]:
        if _BACKUP_SUFFIX.fullmatch(suffix) is None:
            raise ValueError
        return tuple(
            self.backup_root / f"{base_filename}.{suffix}"
            for base_filename in _BACKUP_NAMES.values()
        )

    def _discard_baseline(self) -> None:
        self._assert_managed_paths_safe()
        paths = self.backup_paths
        for path in paths:
            self._assert_managed_paths_safe()
            path.unlink(missing_ok=True)
        self._assert_managed_paths_safe()
        self.manifest_path.unlink(missing_ok=True)

    def _discard_rollback_tombstone(self) -> None:
        self._assert_managed_paths_safe()
        self.rollback_tombstone_path.unlink(missing_ok=True)

    def _deactivate_baseline(self, baseline: Mapping[str, _Snapshot]) -> None:
        self._assert_managed_paths_safe()
        suffix = self._backup_suffix(self._load_manifest())
        archived = self.backup_root / f"backup-manifest.{suffix}.json"
        if (
            archived.exists()
            or _is_link_like(archived)
            or archived.parent.resolve(strict=False)
            != self.backup_root.resolve(strict=False)
        ):
            raise FileExistsError
        tombstone = {
            "version": 1,
            "backup_suffix": suffix,
            "archive": archived.name,
            "targets": {
                name: _snapshot_metadata(baseline[name]) for name in _BACKUP_NAMES
            },
        }
        try:
            _atomic_write(
                self.rollback_tombstone_path,
                (json.dumps(tombstone, sort_keys=True) + "\n").encode("utf-8"),
            )
        except BaseException:
            self._assert_managed_paths_safe()
            self.rollback_tombstone_path.unlink(missing_ok=True)
            raise
        # RISK(data-loss): moving the active manifest makes later apply create a new baseline.
        try:
            self._assert_managed_paths_safe()
            _replace_with_retry(self.manifest_path, archived)
        except BaseException:
            self._assert_managed_paths_safe()
            self.rollback_tombstone_path.unlink(missing_ok=True)
            raise

    def _validate_completed_rollback(self) -> None:
        self._assert_managed_paths_safe()
        if not self.rollback_tombstone_path.is_file():
            raise ValueError
        tombstone = json.loads(self.rollback_tombstone_path.read_text(encoding="utf-8"))
        if (
            not isinstance(tombstone, dict)
            or tombstone.get("version") != 1
            or not isinstance(tombstone.get("targets"), dict)
        ):
            raise ValueError
        suffix = self._backup_suffix(tombstone)
        archive_name = f"backup-manifest.{suffix}.json"
        if tombstone.get("archive") != archive_name:
            raise ValueError
        archive = self.backup_root / archive_name
        if (
            _is_link_like(archive)
            or archive.parent.resolve() != self.backup_root.resolve()
        ):
            raise ValueError
        archived_manifest = self._load_manifest(archive)
        if self._backup_suffix(archived_manifest) != suffix:
            raise ValueError
        baseline = self._baseline_snapshots(archive)
        expected_targets = {
            name: _snapshot_metadata(baseline[name]) for name in _BACKUP_NAMES
        }
        if tombstone["targets"] != expected_targets:
            raise ValueError
        if self._capture_snapshots(self._managed_targets()) != baseline:
            raise ValueError

    def _restore_snapshots(
        self,
        paths: Mapping[str, Path],
        snapshots: Mapping[str, _Snapshot],
    ) -> bool:
        try:
            for name, path in paths.items():
                self._assert_managed_paths_safe()
                snapshot = snapshots[name]
                if snapshot.existed:
                    _atomic_write(path, snapshot.content)
                else:
                    path.unlink(missing_ok=True)
            return True
        except (Exception, KeyboardInterrupt, SystemExit):
            return False

    def _load_hermes_config(self) -> dict[str, Any]:
        self._assert_managed_paths_safe()
        payload = yaml.safe_load(self.hermes_config_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Hermes config is invalid")
        return payload

    def _save_hermes_config(self, config: dict[str, Any]) -> None:
        self._assert_managed_paths_safe()
        _atomic_write(
            self.hermes_config_path,
            yaml.safe_dump(config, sort_keys=False, allow_unicode=True).encode("utf-8"),
        )

    def _codex_mcp_names(self) -> set[str]:
        self._assert_managed_paths_safe()
        payload = tomllib.loads(self.codex_config_path.read_text(encoding="utf-8"))
        servers = payload.get("mcp_servers", {})
        if not isinstance(servers, dict):
            raise ValueError("Codex MCP config is invalid")
        return set(servers)

    def _write_claude_mcp(self) -> None:
        self._assert_managed_paths_safe()
        payload = {
            "mcpServers": {
                _CLAUDE_SERVER: {
                    "command": str(self.hermes_python),
                    "args": ["-m", _CLAUDE_MODULE],
                }
            }
        }
        _atomic_write(
            self.claude_mcp_path,
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )

    def _claude_mcp_is_strict(self) -> bool:
        self._assert_managed_paths_safe()
        payload = json.loads(self.claude_mcp_path.read_text(encoding="utf-8"))
        return payload == {
            "mcpServers": {
                _CLAUDE_SERVER: {
                    "command": str(self.hermes_python),
                    "args": ["-m", _CLAUDE_MODULE],
                }
            }
        }


def _expected_mcp_names(config: Mapping[str, object]) -> set[str]:
    servers = config.get("mcp_servers") or {}
    if not isinstance(servers, dict):
        return set()
    return {
        str(name)
        for name, server in servers.items()
        if isinstance(server, dict) and bool(server.get("command") or server.get("url"))
    }


def _snapshot_metadata(snapshot: _Snapshot) -> dict[str, object]:
    return {
        "existed": snapshot.existed,
        "sha256": hashlib.sha256(snapshot.content).hexdigest(),
    }


def _has_symlink_component(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        if _is_link_like(current):
            return True
    return False


def _is_link_like(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or (is_junction is not None and is_junction())


def _snapshot(path: Path) -> _Snapshot:
    try:
        return _Snapshot(True, path.read_bytes())
    except FileNotFoundError:
        return _Snapshot(False, b"")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    # RISK(security): create private same-directory files; never expose partial config.
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        _replace_with_retry(temporary, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _replace_with_retry(source: Path, destination: Path) -> None:
    # RISK(data-loss): retry only transient sharing violations; other errors fail closed.
    for attempt in range(3):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 2:
                raise
            time.sleep(0.05 * (attempt + 1))


def _failure(error: str, *, rollback_required: bool = False) -> SubscriptionReadiness:
    return SubscriptionReadiness(
        False, None, None, None, False, rollback_required, error
    )


def _result_payload(result: SubscriptionReadiness) -> dict[str, object]:
    payload = asdict(result)
    payload["mcp"] = "ready" if result.mcp else "not_ready"
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="configure-subscription-runtime.py")
    parser.add_argument("command", choices=("apply", "verify", "rollback"))
    parser.add_argument("--forge-root", type=Path)
    parser.add_argument("--hermes-root", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
        if args.command != "rollback" and args.forge_root is None:
            parser.error("--forge-root is required")
    except SystemExit as error:
        return 0 if error.code == 0 else 78

    try:
        setup = SubscriptionRuntimeSetup(
            forge_root=args.forge_root or Path.cwd(),
            hermes_root=args.hermes_root,
        )
        result = getattr(setup, args.command)()
        print(json.dumps(_result_payload(result), sort_keys=True))
        return 0 if result.error is None else 78
    except (Exception, KeyboardInterrupt, SystemExit):
        print(
            json.dumps(
                _result_payload(_failure("runtime configuration failed")),
                sort_keys=True,
            )
        )
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
