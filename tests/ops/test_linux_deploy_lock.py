"""Linux deployment serialization and immutable release contracts."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "forge" / "scripts" / "deploy-vps.sh"
LOCK_MARKER = "INFINITY_FORGE_DEPLOY_LOCK_FD9"


def _linux_script_path() -> tuple[list[str], str]:
    if os.name != "nt":
        if not Path("/usr/bin/flock").is_file():
            pytest.skip("/usr/bin/flock is unavailable")
        return (["bash", "-s"], str(DEPLOY))

    wsl = shutil.which("wsl.exe")
    if wsl is None:
        pytest.skip("WSL is unavailable")
    converted = subprocess.run(
        [wsl, "-e", "wslpath", "-a", str(DEPLOY)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return ([wsl, "-e", "bash", "-s"], converted)


def test_deploy_lock_is_acquired_before_repository_access_and_survives_exec() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")

    assert 'DEPLOY_LOCK_FILE="$HOME/.hermes/infinity-forge/deploy.lock"' in deploy
    assert '[ -x /usr/bin/flock ]' in deploy
    assert 'exec 9>"$DEPLOY_LOCK_FILE"' in deploy
    assert '/usr/bin/flock --nonblock 9' in deploy
    assert deploy.count('/usr/bin/flock --nonblock 9') == 2
    assert f'export {LOCK_MARKER}="$DEPLOY_LOCK_FILE"' in deploy
    assert 'readlink -f "/proc/$$/fd/9"' in deploy
    assert 'exec bash "$REPO_DIR/forge/scripts/deploy-vps.sh" --post-update' in deploy
    assert "main() {\nacquire_deploy_lock\n" in deploy
    assert deploy.index("acquire_deploy_lock") < deploy.index('cd "$REPO_DIR"')
    assert deploy.index("acquire_deploy_lock") < deploy.index("git fetch origin main")


def test_published_commit_releases_are_never_removed_during_rollback() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")

    assert "RELEASE_CREATED" not in deploy
    assert "PLUGIN_RELEASE_CREATED" not in deploy
    assert 'rm -rf -- "$FORGE_RELEASE"' not in deploy
    assert 'rm -rf -- "$PLUGIN_RELEASE"' not in deploy
    assert 'chmod -R u+w "$FORGE_RELEASE"' not in deploy
    assert "restore_forge_environment" in deploy
    assert "restore_plugin_state" in deploy


def test_second_deploy_is_blocked_before_git_or_systemd() -> None:
    command, deploy_path = _linux_script_path()
    harness = r'''
set -euo pipefail
ROOT="$(mktemp -d)"
trap 'rm -rf -- "$ROOT"' EXIT
export HOME="$ROOT/home"
mkdir -p "$HOME/.hermes/infinity-forge"
LOCK_FILE="$HOME/.hermes/infinity-forge/deploy.lock"
exec 8>"$LOCK_FILE"
/usr/bin/flock --nonblock 8
exec 9>"$LOCK_FILE"
export INFINITY_FORGE_DEPLOY_LOCK_FD9="$LOCK_FILE"
set +e
OUTPUT="$(env HOME="$HOME" \
  FORGE_REPO_DIR="$ROOT/repository-must-not-be-read" \
  FORGE_EXPECTED_COMMIT=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  bash "$1" --post-update 2>&1)"
STATUS=$?
set -e
[ "$STATUS" -ne 0 ]
printf '%s\n' "$OUTPUT" | grep -F \
  "[deploy] another Infinity Forge deployment is already running" >/dev/null
case "$OUTPUT" in
  *"repository-must-not-be-read"*) exit 1 ;;
esac
'''
    completed = subprocess.run(
        [*command, "--", deploy_path],
        input=harness.encode("utf-8"),
        check=False,
        capture_output=True,
        timeout=20,
    )

    diagnostic = (completed.stderr or completed.stdout).decode(
        "utf-8", errors="replace"
    )
    assert completed.returncode == 0, diagnostic


def test_matching_unlocked_fd_marker_acquires_lock_before_reentry() -> None:
    command, deploy_path = _linux_script_path()
    harness = r'''
set -euo pipefail
ROOT="$(mktemp -d)"
trap 'rm -rf -- "$ROOT"' EXIT
export HOME="$ROOT/home"
mkdir -p "$HOME/.hermes/infinity-forge"
LOCK_FILE="$HOME/.hermes/infinity-forge/deploy.lock"
exec 9>"$LOCK_FILE"
export INFINITY_FORGE_DEPLOY_LOCK_FD9="$LOCK_FILE"
set +e
OUTPUT="$(env HOME="$HOME" \
  FORGE_REPO_DIR="$ROOT/repository-was-read-after-lock" \
  FORGE_EXPECTED_COMMIT=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  bash "$1" --post-update 2>&1)"
STATUS=$?
set -e
[ "$STATUS" -ne 0 ]
case "$OUTPUT" in
  *"repository-was-read-after-lock"*) ;;
  *) exit 1 ;;
esac
case "$OUTPUT" in
  *"another Infinity Forge deployment is already running"*) exit 1 ;;
esac
'''
    completed = subprocess.run(
        [*command, "--", deploy_path],
        input=harness.encode("utf-8"),
        check=False,
        capture_output=True,
        timeout=20,
    )

    diagnostic = (completed.stderr or completed.stdout).decode(
        "utf-8", errors="replace"
    )
    assert completed.returncode == 0, diagnostic
