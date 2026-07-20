from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from forge.ops import subscription_setup
from forge.ops.subscription_runtime import CodexSubscriptionSnapshot
from forge.ops.subscription_setup import SubscriptionRuntimeSetup


SUBSCRIPTION_AUTH = {
    "loggedIn": True,
    "authMethod": "claude.ai",
    "apiProvider": "firstParty",
    "subscriptionType": None,
}


class Probe:
    def __init__(self, snapshot: CodexSubscriptionSnapshot | Exception | None = None):
        self.snapshot = snapshot or CodexSubscriptionSnapshot(
            "chatgpt", "plus", None, False
        )

    def probe(self, codex_bin: str, env: dict[str, str], timeout: float = 10.0):
        if isinstance(self.snapshot, Exception):
            raise self.snapshot
        return self.snapshot


def make_setup(
    tmp_path: Path,
    *,
    auth: dict[str, object] | None = None,
    probe=None,
    use_default_auth_reader: bool = False,
):
    hermes_root = tmp_path / "hermes"
    codex_home = tmp_path / "codex"
    hermes_root.mkdir()
    codex_home.mkdir()
    python = tmp_path / ("python.exe" if os.name == "nt" else "python")
    python.write_bytes(b"")
    if os.name != "nt":
        python.chmod(0o700)
    hermes_config = hermes_root / "config.yaml"
    hermes_config.write_text(
        yaml.safe_dump(
            {
                "model": {"name": "keep"},
                "mcp_servers": {
                    "external": {
                        "url": "https://mcp.invalid",
                        "headers": {"Authorization": "secret"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    codex_config = codex_home / "config.toml"
    codex_config.write_text('[user]\ncolour = "blue"\n', encoding="utf-8")

    def switch(config, value, *, persist_callback):
        assert value == "codex_app_server"
        config.setdefault("model", {})["openai_runtime"] = value
        persist_callback(config)
        codex_config.write_text(
            '[user]\ncolour = "blue"\n'
            '[mcp_servers.external]\ncommand = "external"\n'
            '[mcp_servers.hermes-tools]\ncommand = "python"\n',
            encoding="utf-8",
        )
        return SimpleNamespace(success=True)

    setup_kwargs = {
        "forge_root": tmp_path,
        "hermes_root": hermes_root,
        "codex_home": codex_home,
        "hermes_python": python,
        "codex_bin": "codex",
        "claude_bin": "claude",
        "probe": probe or Probe(),
        "runtime_switch_apply": switch,
        "mcp_migration_apply": lambda config, *, codex_home: SimpleNamespace(
            written=True,
            migrated=["external", "hermes-tools"],
            errors=[],
        ),
        "environment": {"SAFE": "1"},
    }
    if not use_default_auth_reader:
        setup_kwargs["claude_auth_reader"] = lambda *_, **__: dict(auth or SUBSCRIPTION_AUTH)
    setup = SubscriptionRuntimeSetup(
        **setup_kwargs,
    )
    return setup, hermes_config, codex_config


def test_apply_configures_runtime_and_strict_claude_mcp_without_copying_secrets(
    tmp_path: Path,
):
    setup, hermes_config, codex_config = make_setup(tmp_path)

    result = setup.apply()

    assert result.ready is True
    assert result.claude_subscription == "claude.ai"
    assert yaml.safe_load(hermes_config.read_text(encoding="utf-8"))["model"] == {
        "name": "keep",
        "openai_runtime": "codex_app_server",
    }
    assert '[user]\ncolour = "blue"' in codex_config.read_text(encoding="utf-8")
    managed = json.loads(setup.claude_mcp_path.read_text(encoding="utf-8"))
    assert managed == {
        "mcpServers": {
            "hermes-tools": {
                "command": str(setup.hermes_python.resolve()),
                "args": ["-m", "agent.transports.hermes_tools_mcp_server"],
            }
        }
    }
    assert "secret" not in setup.claude_mcp_path.read_text(encoding="utf-8")


def test_reapply_keeps_first_exact_backup_and_rollback_restores_it(tmp_path: Path):
    setup, hermes_config, codex_config = make_setup(tmp_path)
    original_hermes = hermes_config.read_bytes()
    original_codex = codex_config.read_bytes()

    assert setup.apply().ready
    baseline_manifest = setup.manifest_path.read_bytes()
    manifest = json.loads(baseline_manifest)
    assert manifest["backup_suffix"][:8].isdigit()
    assert all(manifest["backup_suffix"] in path.name for path in setup.backup_paths)
    assert setup.apply().ready
    assert setup.manifest_path.read_bytes() == baseline_manifest

    result = setup.rollback()

    assert result.ready is False
    assert result.rollback_required is False
    assert hermes_config.read_bytes() == original_hermes
    assert codex_config.read_bytes() == original_codex
    assert not setup.claude_mcp_path.exists()
    assert not setup.manifest_path.exists()


def test_rollback_deactivates_baseline_before_a_later_apply(tmp_path: Path):
    setup, hermes_config, codex_config = make_setup(tmp_path)
    assert setup.apply().ready
    assert setup.rollback().error is None
    assert not setup.manifest_path.exists()

    hermes_config.write_text(
        yaml.safe_dump({"model": {"name": "changed-after-rollback"}}),
        encoding="utf-8",
    )
    codex_config.write_text('[user]\ncolour = "green"\n', encoding="utf-8")
    changed = (hermes_config.read_bytes(), codex_config.read_bytes())

    assert setup.apply().ready
    assert setup.rollback().error is None

    assert (hermes_config.read_bytes(), codex_config.read_bytes()) == changed
    assert not setup.manifest_path.exists()


def test_repeated_rollback_is_noop_for_the_exact_completed_generation(tmp_path: Path):
    setup, hermes_config, codex_config = make_setup(tmp_path)
    assert setup.apply().ready
    assert setup.rollback().error is None
    restored = (hermes_config.read_bytes(), codex_config.read_bytes())

    result = setup.rollback()

    assert result.error is None
    assert result.rollback_required is False
    assert (hermes_config.read_bytes(), codex_config.read_bytes()) == restored
    assert setup.rollback_tombstone_path.is_file()


def test_missing_cycle_two_manifest_never_uses_a_cycle_one_archive(tmp_path: Path):
    setup, hermes_config, codex_config = make_setup(tmp_path)
    assert setup.apply().ready
    assert setup.rollback().error is None
    hermes_config.write_text(
        yaml.safe_dump({"model": {"name": "cycle-two"}, "mcp_servers": {}}),
        encoding="utf-8",
    )
    codex_config.write_text('[user]\ncolour = "green"\n', encoding="utf-8")

    assert setup.apply().ready
    assert not setup.rollback_tombstone_path.exists()
    cycle_two = (hermes_config.read_bytes(), codex_config.read_bytes())
    setup.manifest_path.unlink()

    result = setup.rollback()

    assert result.error == "managed backup is invalid"
    assert result.rollback_required is False
    assert (hermes_config.read_bytes(), codex_config.read_bytes()) == cycle_two


def test_tampered_rollback_tombstone_fails_closed(tmp_path: Path):
    setup, _, _ = make_setup(tmp_path)
    assert setup.apply().ready
    assert setup.rollback().error is None
    tombstone = json.loads(setup.rollback_tombstone_path.read_text(encoding="utf-8"))
    tombstone["backup_suffix"] = "20000101T000000000000Z-deadbeef"
    setup.rollback_tombstone_path.write_text(json.dumps(tombstone), encoding="utf-8")

    result = setup.rollback()

    assert result.error == "managed backup is invalid"
    assert result.rollback_required is False


def test_completed_rollback_target_drift_fails_closed(tmp_path: Path):
    setup, _, codex_config = make_setup(tmp_path)
    assert setup.apply().ready
    assert setup.rollback().error is None
    codex_config.write_text('[user]\ncolour = "drifted"\n', encoding="utf-8")

    result = setup.rollback()

    assert result.error == "managed backup is invalid"
    assert codex_config.read_text(encoding="utf-8") == '[user]\ncolour = "drifted"\n'


def test_failed_new_apply_keeps_completed_rollback_noop_state(tmp_path: Path):
    setup, _, _ = make_setup(tmp_path)
    assert setup.apply().ready
    assert setup.rollback().error is None
    tombstone = setup.rollback_tombstone_path.read_bytes()
    setup._mcp_migration_apply = lambda config, *, codex_home: SimpleNamespace(
        written=False,
        migrated=["external", "hermes-tools"],
        plugin_query_error=None,
    )

    assert setup.apply().error == "runtime configuration failed"

    assert setup.rollback_tombstone_path.read_bytes() == tombstone
    assert not setup.manifest_path.exists()
    assert setup.rollback().error is None


def test_rollback_archive_rename_failure_restores_applied_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    setup, hermes_config, codex_config = make_setup(tmp_path)
    assert setup.apply().ready
    applied = (hermes_config.read_bytes(), codex_config.read_bytes())
    real_replace = subscription_setup._replace_with_retry

    def fail_manifest_archive(source: Path, destination: Path):
        if source == setup.manifest_path:
            raise PermissionError("archive unavailable")
        real_replace(source, destination)

    monkeypatch.setattr(
        subscription_setup, "_replace_with_retry", fail_manifest_archive
    )

    result = setup.rollback()

    assert result.error == "managed backup is invalid"
    assert result.rollback_required is False
    assert (hermes_config.read_bytes(), codex_config.read_bytes()) == applied
    assert setup.manifest_path.is_file()
    assert not setup.rollback_tombstone_path.exists()


def test_tombstone_write_failure_removes_partial_state_and_restores_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    setup, hermes_config, codex_config = make_setup(tmp_path)
    assert setup.apply().ready
    applied = (hermes_config.read_bytes(), codex_config.read_bytes())
    real_atomic_write = subscription_setup._atomic_write

    def fail_after_tombstone_write(path: Path, content: bytes):
        real_atomic_write(path, content)
        if path == setup.rollback_tombstone_path:
            raise OSError("tombstone metadata unavailable")

    monkeypatch.setattr(subscription_setup, "_atomic_write", fail_after_tombstone_write)

    result = setup.rollback()

    assert result.error == "managed backup is invalid"
    assert result.rollback_required is False
    assert (hermes_config.read_bytes(), codex_config.read_bytes()) == applied
    assert setup.manifest_path.is_file()
    assert not setup.rollback_tombstone_path.exists()


@pytest.mark.parametrize(
    ("auth", "snapshot"),
    [
        ({**SUBSCRIPTION_AUTH, "loggedIn": False}, None),
        ({**SUBSCRIPTION_AUTH, "authMethod": "oauth"}, None),
        ({**SUBSCRIPTION_AUTH, "apiProvider": "thirdParty"}, None),
        (SUBSCRIPTION_AUTH, CodexSubscriptionSnapshot("api", None, None, False)),
        (SUBSCRIPTION_AUTH, RuntimeError("handshake token@example.test secret")),
    ],
)
def test_preflight_failure_never_mutates_config_or_leaks_details(
    tmp_path: Path, auth, snapshot
):
    setup, hermes_config, codex_config = make_setup(
        tmp_path, auth=auth, probe=Probe(snapshot) if snapshot is not None else None
    )
    before = (hermes_config.read_bytes(), codex_config.read_bytes())

    result = setup.apply()

    assert result.ready is False
    assert "secret" not in (result.error or "")
    assert "@" not in (result.error or "")
    assert (hermes_config.read_bytes(), codex_config.read_bytes()) == before
    assert not setup.manifest_path.exists()


def test_postcheck_failure_restores_immediate_pre_apply_state(tmp_path: Path):
    setup, hermes_config, codex_config = make_setup(tmp_path)
    original = (hermes_config.read_bytes(), codex_config.read_bytes())

    def incomplete_switch(config, value, *, persist_callback):
        config.setdefault("model", {})["openai_runtime"] = value
        persist_callback(config)
        codex_config.write_text(
            '[mcp_servers.hermes-tools]\ncommand="python"\n', encoding="utf-8"
        )
        return SimpleNamespace(success=True)

    setup._runtime_switch_apply = incomplete_switch
    setup._mcp_migration_apply = lambda config, *, codex_home: SimpleNamespace(
        written=False, migrated=["hermes-tools"], errors=[]
    )
    result = setup.apply()

    assert result.ready is False
    assert result.rollback_required is False
    assert (hermes_config.read_bytes(), codex_config.read_bytes()) == original
    assert not setup.claude_mcp_path.exists()


def test_authoritative_migration_failure_rejects_stale_matching_mcp_names(
    tmp_path: Path, capsys, caplog
):
    setup, hermes_config, codex_config = make_setup(tmp_path)
    original = (hermes_config.read_bytes(), codex_config.read_bytes())
    private = str(tmp_path / "private-config.toml")

    setup._mcp_migration_apply = lambda config, *, codex_home: SimpleNamespace(
        written=False,
        migrated=["external", "hermes-tools"],
        errors=[f"could not write {private}"],
    )

    with caplog.at_level(logging.DEBUG):
        result = setup.apply()

    captured = capsys.readouterr()
    assert result.error == "runtime configuration failed"
    assert (hermes_config.read_bytes(), codex_config.read_bytes()) == original
    assert private not in captured.out + captured.err + caplog.text
    assert "could not write" not in captured.out + captured.err + caplog.text


def test_plugin_discovery_error_restores_plugin_removed_by_migration(tmp_path: Path):
    setup, hermes_config, codex_config = make_setup(tmp_path)
    original_codex = (
        '[user]\ncolour = "blue"\n[plugins."github@openai-curated"]\nenabled = true\n'
    ).encode()
    codex_config.write_bytes(original_codex)
    original_hermes = hermes_config.read_bytes()

    def migration_with_transient_plugin_error(config, *, codex_home):
        codex_config.write_text(
            '[user]\ncolour = "blue"\n'
            '[mcp_servers.external]\ncommand = "external"\n'
            '[mcp_servers.hermes-tools]\ncommand = "python"\n',
            encoding="utf-8",
        )
        return SimpleNamespace(
            written=True,
            migrated=["external", "hermes-tools"],
            plugin_query_error="transient plugin/list failure",
        )

    setup._mcp_migration_apply = migration_with_transient_plugin_error

    result = setup.apply()

    assert result.error == "runtime configuration failed"
    assert hermes_config.read_bytes() == original_hermes
    assert codex_config.read_bytes() == original_codex


def test_runtime_switch_capture_observes_one_migration_and_restores_adapter():
    report = SimpleNamespace(written=True, migrated=["hermes-tools"])
    calls = 0

    def migration(config):
        nonlocal calls
        calls += 1
        return report

    migration_module = SimpleNamespace(migrate=migration)

    def helper(config, value, *, persist_callback):
        migration_module.migrate(config)
        return SimpleNamespace(success=True)

    outcome = subscription_setup._apply_runtime_switch_with_captured_migration(
        {},
        "codex_app_server",
        persist_callback=lambda config: None,
        runtime_switch_apply=helper,
        migration_module=migration_module,
    )

    assert calls == 1
    assert outcome.status.success is True
    assert outcome.migration_report is report
    assert migration_module.migrate is migration


def test_runtime_switch_capture_excludes_existing_codex_mcp_collisions_only_from_migration():
    migrated_configs: list[dict] = []
    persisted_configs: list[dict] = []
    report = SimpleNamespace(written=True, migrated=["new-server", "hermes-tools"])

    def migration(config):
        migrated_configs.append(config)
        return report

    migration_module = SimpleNamespace(migrate=migration)

    def helper(config, value, *, persist_callback):
        config.setdefault("model", {})["openai_runtime"] = value
        persist_callback(config)
        migration_module.migrate(config)
        return SimpleNamespace(success=True)

    config = {
        "mcp_servers": {
            "existing-server": {"command": "windows-only"},
            "new-server": {"url": "https://mcp.invalid"},
        }
    }
    outcome = subscription_setup._apply_runtime_switch_with_captured_migration(
        config,
        "codex_app_server",
        persist_callback=lambda updated: persisted_configs.append(updated.copy()),
        runtime_switch_apply=helper,
        migration_module=migration_module,
        migration_excluded_mcp_names=frozenset({"existing-server"}),
    )

    assert outcome.migration_report is report
    assert set(persisted_configs[0]["mcp_servers"]) == {
        "existing-server",
        "new-server",
    }
    assert set(migrated_configs[0]["mcp_servers"]) == {"new-server"}
    assert set(config["mcp_servers"]) == {"existing-server", "new-server"}
    assert migration_module.migrate is migration


def test_unmanaged_codex_mcp_names_excludes_the_hermes_managed_block(
    tmp_path: Path,
):
    setup, _, codex_config = make_setup(tmp_path)
    codex_config.write_text(
        """[mcp_servers.serena]
command = "serena"
[mcp_servers.playwright]
command = "playwright"
[mcp_servers.playwright.env]
MODE = "safe"

# managed by hermes-agent — `hermes codex-runtime migrate` regenerates this section
[mcp_servers.memex]
url = "https://memex.invalid"
[mcp_servers.hermes-tools]
command = "python"
# end hermes-agent managed section
""",
        encoding="utf-8",
    )

    assert setup._unmanaged_codex_mcp_names() == {"playwright", "serena"}


def test_runtime_switch_capture_restores_adapter_on_system_exit():
    def migration(config):
        return SimpleNamespace(written=True)

    migration_module = SimpleNamespace(migrate=migration)

    def helper(config, value, *, persist_callback):
        raise SystemExit("private helper exit")

    with pytest.raises(SystemExit):
        subscription_setup._apply_runtime_switch_with_captured_migration(
            {},
            "codex_app_server",
            persist_callback=lambda config: None,
            runtime_switch_apply=helper,
            migration_module=migration_module,
        )

    assert migration_module.migrate is migration


def test_runtime_switch_capture_reads_and_restores_migration_inside_lock(
    monkeypatch: pytest.MonkeyPatch,
):
    worker_b_entered_lock = threading.Event()
    worker_b_read_migrate = threading.Event()
    worker_a_holding_lock = threading.Event()
    release_worker_a = threading.Event()

    class RecordingLock:
        def __init__(self):
            self._lock = threading.Lock()

        def __enter__(self):
            if threading.current_thread().name == "migration-worker-b":
                worker_b_entered_lock.set()
            self._lock.acquire()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            self._lock.release()

    reports = {
        "a": SimpleNamespace(written=True, migrated=["a"]),
        "b": SimpleNamespace(written=True, migrated=["b"]),
    }
    calls: list[str] = []

    def original_migrate(config):
        worker = config["worker"]
        calls.append(worker)
        return reports[worker]

    class MigrationModule:
        migrate = staticmethod(original_migrate)

        def __getattribute__(self, name):
            if (
                name == "migrate"
                and threading.current_thread().name == "migration-worker-b"
            ):
                worker_b_read_migrate.set()
            return object.__getattribute__(self, name)

    migration_module = MigrationModule()
    recording_lock = RecordingLock()
    monkeypatch.setattr(subscription_setup, "_MIGRATION_CAPTURE_LOCK", recording_lock)
    outcomes: dict[str, object] = {}

    def helper(config, value, *, persist_callback):
        migration_module.migrate(config)
        if config["worker"] == "a":
            worker_a_holding_lock.set()
            assert release_worker_a.wait(timeout=5)
        return SimpleNamespace(success=True)

    def run(worker: str):
        outcomes[worker] = (
            subscription_setup._apply_runtime_switch_with_captured_migration(
                {"worker": worker},
                "codex_app_server",
                persist_callback=lambda config: None,
                runtime_switch_apply=helper,
                migration_module=migration_module,
            )
        )

    worker_a = threading.Thread(target=run, args=("a",), name="migration-worker-a")
    worker_b = threading.Thread(target=run, args=("b",), name="migration-worker-b")
    worker_a.start()
    assert worker_a_holding_lock.wait(timeout=5)
    worker_b.start()
    assert worker_b_entered_lock.wait(timeout=5)
    worker_b_read_before_lock = worker_b_read_migrate.is_set()
    release_worker_a.set()
    worker_a.join(timeout=5)
    worker_b.join(timeout=5)

    assert not worker_a.is_alive()
    assert not worker_b.is_alive()
    assert worker_b_read_before_lock is False
    assert migration_module.migrate is original_migrate
    assert outcomes["a"].migration_report is reports["a"]
    assert outcomes["b"].migration_report is reports["b"]
    assert calls == ["a", "b"]


def test_failed_reapply_restores_immediate_state_and_keeps_original_baseline(
    tmp_path: Path,
):
    setup, hermes_config, codex_config = make_setup(tmp_path)
    assert setup.apply().ready
    baseline = setup.manifest_path.read_bytes()
    hermes_config.write_text(
        yaml.safe_dump(
            {
                "model": {"openai_runtime": "codex_app_server"},
                "mcp_servers": {"external": {}},
                "user": "new",
            }
        ),
        encoding="utf-8",
    )
    immediate = (
        hermes_config.read_bytes(),
        codex_config.read_bytes(),
        setup.claude_mcp_path.read_bytes(),
    )

    def fail_after_persist(config, value, *, persist_callback):
        persist_callback(config)
        codex_config.write_text("broken = true\n", encoding="utf-8")
        return SimpleNamespace(success=True)

    setup._runtime_switch_apply = fail_after_persist
    assert setup.apply().ready is False
    assert (
        hermes_config.read_bytes(),
        codex_config.read_bytes(),
        setup.claude_mcp_path.read_bytes(),
    ) == immediate
    assert setup.manifest_path.read_bytes() == baseline


def test_verify_is_read_only_and_detects_runtime_drift(tmp_path: Path):
    setup, hermes_config, codex_config = make_setup(tmp_path)
    assert setup.apply().ready
    config = yaml.safe_load(hermes_config.read_text(encoding="utf-8"))
    config["model"]["openai_runtime"] = "auto"
    hermes_config.write_text(yaml.safe_dump(config), encoding="utf-8")
    before = (
        hermes_config.read_bytes(),
        codex_config.read_bytes(),
        setup.claude_mcp_path.read_bytes(),
    )

    result = setup.verify()

    assert result.ready is False
    assert (
        hermes_config.read_bytes(),
        codex_config.read_bytes(),
        setup.claude_mcp_path.read_bytes(),
    ) == before


def test_reapply_fails_closed_when_secured_baseline_is_corrupt(tmp_path: Path):
    setup, hermes_config, codex_config = make_setup(tmp_path)
    assert setup.apply().ready
    setup.backup_paths[0].write_bytes(b"corrupt")
    immediate = (
        hermes_config.read_bytes(),
        codex_config.read_bytes(),
        setup.claude_mcp_path.read_bytes(),
    )

    result = setup.apply()

    assert result.ready is False
    assert (
        hermes_config.read_bytes(),
        codex_config.read_bytes(),
        setup.claude_mcp_path.read_bytes(),
    ) == immediate


def test_tampered_manifest_cannot_redirect_rollback_write(tmp_path: Path):
    setup, _, _ = make_setup(tmp_path)
    assert setup.apply().ready
    outside = tmp_path / "outside"
    outside.write_text("safe", encoding="utf-8")
    manifest = json.loads(setup.manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["hermes"]["backup"] = "../outside"
    setup.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = setup.rollback()

    assert result.error == "managed backup is invalid"
    assert outside.read_text(encoding="utf-8") == "safe"


def test_link_substitution_before_rollback_cannot_touch_outside_file(
    tmp_path: Path,
):
    setup, _, _ = make_setup(tmp_path)
    assert setup.apply().ready
    original_managed_root = setup.managed_root.with_name("subscription-runtime-real")
    subscription_setup._replace_with_retry(setup.managed_root, original_managed_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("outside-safe", encoding="utf-8")
    junction_created = False
    try:
        setup.managed_root.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        if os.name != "nt":
            pytest.skip(f"symlinks unavailable: {type(error).__name__}")
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(setup.managed_root), str(outside)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode != 0:
            pytest.skip("links unavailable")
        junction_created = True

    try:
        result = setup.rollback()

        assert result.error == "managed backup is invalid"
        assert result.rollback_required is False
        assert sentinel.read_text(encoding="utf-8") == "outside-safe"
    finally:
        if junction_created:
            setup.managed_root.rmdir()
        elif setup.managed_root.is_symlink():
            setup.managed_root.unlink()


@pytest.mark.parametrize("root_name", ["hermes_root", "codex_home"])
def test_root_link_substitution_is_rejected_by_construction_identity(
    tmp_path: Path, root_name: str
):
    setup, _, _ = make_setup(tmp_path)
    assert setup.apply().ready
    selected = getattr(setup, root_name)
    original = selected.with_name(f"{selected.name}-real")
    subscription_setup._replace_with_retry(selected, original)
    outside = tmp_path / f"outside-{root_name}"
    shutil.copytree(original, outside)
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("outside-safe", encoding="utf-8")
    junction_created = False
    try:
        selected.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        if os.name != "nt":
            pytest.skip(f"symlinks unavailable: {type(error).__name__}")
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(selected), str(outside)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode != 0:
            pytest.skip("links unavailable")
        junction_created = True

    outside_config = outside / (
        "config.yaml" if root_name == "hermes_root" else "config.toml"
    )
    before = outside_config.read_bytes()
    try:
        result = setup.rollback()

        assert result.error == "managed backup is invalid"
        assert result.rollback_required is False
        assert outside_config.read_bytes() == before
        assert sentinel.read_text(encoding="utf-8") == "outside-safe"
    finally:
        if junction_created:
            selected.rmdir()
        elif selected.is_symlink():
            selected.unlink()


def test_missing_hermes_python_blocks_before_probe_or_mutation(tmp_path: Path):
    setup, hermes_config, codex_config = make_setup(tmp_path)
    setup.hermes_python.unlink()
    before = (hermes_config.read_bytes(), codex_config.read_bytes())

    result = setup.apply()

    assert result.error == "Hermes runtime preflight failed"
    assert (hermes_config.read_bytes(), codex_config.read_bytes()) == before


def test_rollback_removes_a_codex_config_missing_from_the_baseline(tmp_path: Path):
    setup, _, codex_config = make_setup(tmp_path)
    codex_config.unlink()

    assert setup.apply().ready
    assert codex_config.exists()

    result = setup.rollback()

    assert result.error is None
    assert not codex_config.exists()


def test_default_claude_auth_preflight_is_time_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    seen: dict[str, object] = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr("forge.ops.subscription_setup.subprocess.run", run)
    setup, hermes_config, codex_config = make_setup(
        tmp_path, use_default_auth_reader=True
    )
    before = (hermes_config.read_bytes(), codex_config.read_bytes())

    result = setup.apply()

    assert seen["timeout"] == 10.0
    assert result.error == "Claude.ai subscription preflight failed"
    assert (hermes_config.read_bytes(), codex_config.read_bytes()) == before
    assert not setup.manifest_path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable contract")
def test_non_executable_hermes_python_blocks_before_mutation(tmp_path: Path):
    setup, hermes_config, codex_config = make_setup(tmp_path)
    setup.hermes_python.chmod(0o600)
    before = (hermes_config.read_bytes(), codex_config.read_bytes())

    result = setup.apply()

    assert result.error == "Hermes runtime preflight failed"
    assert (hermes_config.read_bytes(), codex_config.read_bytes()) == before
    assert not setup.manifest_path.exists()


def test_runtime_switch_persist_failure_is_captured_without_private_logging(
    tmp_path: Path, capsys, caplog
):
    setup, hermes_config, codex_config = make_setup(tmp_path)
    original = (hermes_config.read_bytes(), codex_config.read_bytes())
    private = str(tmp_path / "private-config.yaml")

    def fail_persist(config):
        raise OSError(private)

    def logging_switch(config, value, *, persist_callback):
        try:
            persist_callback(config)
        except Exception:
            logging.getLogger("fake-hermes-helper").exception(
                "persist failed for %s", private
            )
        return SimpleNamespace(success=True)

    setup._save_hermes_config = fail_persist
    setup._runtime_switch_apply = logging_switch

    with caplog.at_level(logging.DEBUG):
        result = setup.apply()

    captured = capsys.readouterr()
    emitted = captured.out + captured.err + caplog.text
    assert result.error == "runtime configuration failed"
    assert (hermes_config.read_bytes(), codex_config.read_bytes()) == original
    assert private not in emitted
    assert "Traceback" not in emitted


def test_keyboard_interrupt_during_mutation_restores_immediate_snapshots(
    tmp_path: Path,
):
    setup, hermes_config, codex_config = make_setup(tmp_path)
    original = (hermes_config.read_bytes(), codex_config.read_bytes())

    def interrupted_switch(config, value, *, persist_callback):
        hermes_config.write_text("partial: true\n", encoding="utf-8")
        raise KeyboardInterrupt(str(tmp_path / "private-config.yaml"))

    setup._runtime_switch_apply = interrupted_switch

    result = setup.apply()

    assert result.error == "runtime configuration failed"
    assert result.rollback_required is False
    assert (hermes_config.read_bytes(), codex_config.read_bytes()) == original
    assert not setup.manifest_path.exists()


def test_system_exit_during_mutation_restores_immediate_snapshots(tmp_path: Path):
    setup, hermes_config, codex_config = make_setup(tmp_path)
    original = (hermes_config.read_bytes(), codex_config.read_bytes())

    def interrupted_switch(config, value, *, persist_callback):
        hermes_config.write_text("partial: true\n", encoding="utf-8")
        raise SystemExit(str(tmp_path / "private-config.yaml"))

    setup._runtime_switch_apply = interrupted_switch

    result = setup.apply()

    assert result.error == "runtime configuration failed"
    assert result.rollback_required is False
    assert (hermes_config.read_bytes(), codex_config.read_bytes()) == original
    assert not setup.manifest_path.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing violation contract")
def test_atomic_write_retries_a_transient_windows_sharing_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "managed.bin"
    target.write_bytes(b"before")
    real_replace = subscription_setup.os.replace
    attempts = 0

    def fail_once(source: Path, destination: Path):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("simulated sharing violation")
        real_replace(source, destination)

    monkeypatch.setattr(subscription_setup.os, "replace", fail_once)

    subscription_setup._atomic_write(target, b"after")

    assert attempts == 2
    assert target.read_bytes() == b"after"


def test_preflight_processes_never_receive_payg_credentials(tmp_path: Path):
    seen: list[dict[str, str]] = []

    class RecordingProbe(Probe):
        def probe(self, codex_bin, env, timeout=10.0):
            seen.append(dict(env))
            return super().probe(codex_bin, env, timeout)

    setup, _, _ = make_setup(tmp_path, probe=RecordingProbe())
    setup.environment.update(
        {
            "OPENAI_API_KEY": "openai-secret",
            "ANTHROPIC_API_KEY": "anthropic-secret",
        }
    )
    setup._claude_auth_reader = lambda _, env: seen.append(dict(env)) or dict(SUBSCRIPTION_AUTH)

    assert setup.apply().ready
    assert seen
    assert all(
        "OPENAI_API_KEY" not in env and "ANTHROPIC_API_KEY" not in env for env in seen
    )


def test_invalid_unmigratable_hermes_mcp_entry_is_not_required_postcheck(
    tmp_path: Path,
):
    setup, hermes_config, _ = make_setup(tmp_path)
    config = yaml.safe_load(hermes_config.read_text(encoding="utf-8"))
    config["mcp_servers"]["invalid"] = {"headers": {"Authorization": "not-copied"}}
    hermes_config.write_text(yaml.safe_dump(config), encoding="utf-8")

    assert setup.apply().ready


@pytest.mark.parametrize("servers", [None, []])
def test_invalid_hermes_mcp_collection_has_no_migratable_servers(
    tmp_path: Path, servers
):
    setup, hermes_config, _ = make_setup(tmp_path)
    config = yaml.safe_load(hermes_config.read_text(encoding="utf-8"))
    config["mcp_servers"] = servers
    hermes_config.write_text(yaml.safe_dump(config), encoding="utf-8")

    assert setup.apply().ready


def test_postcheck_requires_a_non_string_migrated_server_name(tmp_path: Path):
    setup, hermes_config, _ = make_setup(tmp_path)
    config = yaml.safe_load(hermes_config.read_text(encoding="utf-8"))
    config["mcp_servers"][42] = {"command": "numeric-name"}
    hermes_config.write_text(yaml.safe_dump(config), encoding="utf-8")

    result = setup.apply()

    assert result.ready is False
    assert result.error == "runtime configuration failed"


def test_missing_managed_roots_fail_before_snapshot_without_path_details(
    tmp_path: Path,
):
    missing = tmp_path / "missing"
    setup = SubscriptionRuntimeSetup(
        forge_root=missing,
        hermes_root=missing,
        codex_home=tmp_path / "codex",
        hermes_python=missing / "python",
        probe=Probe(),
        claude_auth_reader=lambda *_: SUBSCRIPTION_AUTH,
        runtime_switch_apply=lambda *args, **kwargs: SimpleNamespace(success=True),
        environment={},
    )

    result = setup.apply()

    assert result.error == "managed path preflight failed"
    assert str(tmp_path) not in result.error
    assert not setup.manifest_path.exists()


def test_default_codex_target_ignores_environment_override(tmp_path: Path):
    setup = SubscriptionRuntimeSetup(
        forge_root=tmp_path,
        hermes_root=tmp_path,
        environment={"CODEX_HOME": str(tmp_path / "redirect")},
    )

    assert setup.codex_home == (Path.home() / ".codex").resolve()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode contract")
def test_managed_files_are_private_on_posix(tmp_path: Path):
    setup, _, _ = make_setup(tmp_path)
    assert setup.apply().ready
    for path in [setup.manifest_path, setup.claude_mcp_path, *setup.backup_paths]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
