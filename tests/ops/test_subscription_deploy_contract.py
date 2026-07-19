"""Static deployment contract for the subscription runtime on every host."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LINUX_DEPLOY = ROOT / "forge" / "scripts" / "deploy-vps.sh"
WINDOWS_COORDINATOR = ROOT / "forge" / "scripts" / "deploy.ps1"
WINDOWS_DEPLOY = ROOT / "forge" / "scripts" / "deploy-windows.ps1"

ENVIRONMENT_NAMES = {
    "INFINITY_FORGE_SUBSCRIPTION_ROUTING",
    "INFINITY_FORGE_SUBSCRIPTION_PYTHON",
    "INFINITY_FORGE_SUBSCRIPTION_RUNNER",
    "INFINITY_FORGE_CLAUDE_BIN",
    "INFINITY_FORGE_CLAUDE_MCP_CONFIG",
    "INFINITY_FORGE_REPO",
}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_linux_installs_exact_claude_and_authenticates_before_runtime_mutation() -> None:
    deploy = _text(LINUX_DEPLOY)
    installer = "curl -fsSL https://claude.ai/install.sh | bash -s 2.1.215"
    auth_probe = '"$CLAUDE_BIN" auth status --json'
    first_stop = deploy.index('systemctl --user stop "forge-$T.timer"')

    assert 'CLAUDE_VERSION="2.1.215"' in deploy
    assert installer in deploy
    assert auth_probe in deploy
    assert deploy.index(installer) < first_stop
    assert deploy.index(auth_probe) < first_stop
    assert "is_claude_subscription_auth(payload)" in deploy
    assert "subscriptionType" not in deploy
    assert "claude auth login" in deploy
    assert "exit 78" in deploy
    assert deploy.index(auth_probe) < deploy.index('mkdir -p "$TASK_DATA_DIR"')


def test_linux_installs_stable_runner_skills_and_profile_auth_links_safely() -> None:
    deploy = _text(LINUX_DEPLOY)

    assert 'STABLE_RUNNER="$HOME/.hermes/infinity-forge/bin/subscription-runner.py"' in deploy
    assert 'install -m 755 "$REPO_DIR/forge/scripts/subscription-runner.py" "$STABLE_RUNNER"' in deploy
    assert "for S in codex claude-code" in deploy
    for source in ("$HOME/.codex", "$HOME/.claude", "$HOME/.claude.json"):
        assert source in deploy
    backup = deploy.index('mv -- "$DST" "$BACKUP"')
    link = deploy.index('ln -s -- "$SRC" "$DST"')
    assert backup < link
    assert 'date -u +%Y%m%dT%H%M%SZ' in deploy


def test_linux_and_windows_publish_the_same_six_subscription_variables() -> None:
    linux = _text(LINUX_DEPLOY)
    windows = _text(WINDOWS_DEPLOY)
    linux_names = set(re.findall(r'Environment="(INFINITY_FORGE_[A-Z_]+)=', linux))
    windows_block = re.search(
        r"\$SubscriptionEnvironment\s*=\s*\[ordered\]@\{(.*?)\n\s*\}",
        windows,
        flags=re.DOTALL,
    )

    assert windows_block is not None
    windows_names = set(
        re.findall(r'"(INFINITY_FORGE_[A-Z_]+)"\s*=', windows_block.group(1))
    )
    assert linux_names == windows_names == ENVIRONMENT_NAMES
    expected_values = {
        "INFINITY_FORGE_SUBSCRIPTION_ROUTING": ('1', '"1"'),
        "INFINITY_FORGE_SUBSCRIPTION_PYTHON": ('$HERMES_PY', '$HermesPython'),
        "INFINITY_FORGE_SUBSCRIPTION_RUNNER": ('$STABLE_RUNNER', '$StableRunner'),
        "INFINITY_FORGE_CLAUDE_BIN": ('$CLAUDE_BIN', '$ClaudeBin'),
        "INFINITY_FORGE_CLAUDE_MCP_CONFIG": ('$CLAUDE_MCP_CONFIG', '$ClaudeMcpConfig'),
        "INFINITY_FORGE_REPO": ('$REPO_DIR', '$ReleasePath'),
    }
    for name, (linux_value, windows_value) in expected_values.items():
        assert f'Environment="{name}={linux_value}"' in linux
        assert re.search(
            rf'"{name}"\s*=\s*{re.escape(windows_value)}(?:\s|$)',
            windows_block.group(1),
        )


def test_linux_applies_and_verifies_before_restart_and_rolls_back_on_failure() -> None:
    deploy = _text(LINUX_DEPLOY)
    apply = deploy.index('"$HERMES_PY" "$CONFIGURE_SCRIPT" apply')
    verify = deploy.index('"$HERMES_PY" "$CONFIGURE_SCRIPT" verify')
    restart = deploy.index("systemctl --user restart hermes-gateway")

    assert apply < verify < restart
    assert 'CONFIGURE_APPLIED=true' in deploy
    assert '"$HERMES_PY" "$CONFIGURE_SCRIPT" rollback' in deploy
    assert 'systemctl --user is-active --quiet hermes-gateway' in deploy[restart:]
    assert 'trap restore_runtime_after_error EXIT' in deploy


def test_remote_verification_checks_all_seven_carried_files() -> None:
    deploy = _text(WINDOWS_COORDINATOR)
    marker_block = deploy[
        deploy.index("for MarkerFile in") : deploy.index("for Profile in")
    ]

    for target in (
        "hermes_cli/plugins.py",
        "agent/conversation_loop.py",
        "run_agent.py",
        "cli.py",
        "tui_gateway/server.py",
        "gateway/run.py",
    ):
        assert target in marker_block
    assert "hermes_cli/kanban_db.py" in marker_block
    assert "INFINITY_FORGE_SUBSCRIPTION_WORKER_V1" in marker_block


def test_windows_local_install_is_transactional_and_uses_hermes_python() -> None:
    deploy = _text(WINDOWS_DEPLOY)
    apply_flow = deploy[deploy.index("function Invoke-ForgeWindowsApply") :]
    install = apply_flow.index('-Action "install"')
    subscription = apply_flow.index("Install-InfinityForgeSubscriptionRuntime")
    subscription_function = deploy[
        deploy.index("function Install-InfinityForgeSubscriptionRuntime") :
        deploy.index("function Restore-InfinityForgeSubscriptionRuntime")
    ]
    stable_copy = subscription_function.index("subscription-runner.py")
    apply = subscription_function.index('-Action "apply"', stable_copy)
    verify = subscription_function.index('-Action "verify"', apply)
    restart = apply_flow.index("Start-HermesGateway", subscription)

    assert 'HermesRoot = $hermesRoot' in deploy
    assert 'HermesPython = $hermesPython' in deploy
    assert 'StableRunner = (Join-Path $localRoot "subscription-runtime\\subscription-runner.py")' in deploy
    assert install < subscription < restart
    assert stable_copy < apply < verify
    assert '[Environment]::SetEnvironmentVariable($Name, $Value, "User")' in deploy
    assert 'Set-Item -Path "Env:$Name" -Value $Value' in deploy
    assert '[Environment]::SetEnvironmentVariable($Name, $PreviousValue, "User")' in deploy
    capture = deploy.index('$Transaction.SubscriptionPreviousUser[$Name] =')
    first_persist = deploy.index('[Environment]::SetEnvironmentVariable($Name, $Value, "User")')
    assert capture < first_persist
    assert '-Action "rollback"' in deploy
    assert "Test-HermesGatewayRunning" in deploy[restart:]
    assert deploy.index("is_claude_subscription_auth") < deploy.index("Install-ForgeWindowsRelease")
    assert deploy.index("CodexAppServerProbe") < deploy.index("Install-ForgeWindowsRelease")
    assert deploy.index("Move-Item -LiteralPath $Destination -Destination $Backup") < deploy.index("New-Item -ItemType SymbolicLink -Path $Destination")


def test_windows_subscription_rollback_restores_local_state_after_config_error() -> None:
    deploy = _text(WINDOWS_DEPLOY)
    rollback = deploy[
        deploy.index("function Restore-InfinityForgeSubscriptionRuntime") :
        deploy.index("function Complete-InfinityForgeSubscriptionRuntime")
    ]

    catch = rollback.index("catch {")
    environment_restore = rollback.index(
        '[Environment]::SetEnvironmentVariable($Name, $PreviousValue, "User")'
    )
    runner_restore = rollback.index(
        "Copy-Item -LiteralPath $Transaction.SubscriptionRunnerBackup"
    )
    delayed_error = rollback.index("if ($null -ne $ConfigurationRollbackError)")

    assert catch < environment_restore < runner_restore < delayed_error


def test_deploy_scripts_never_modify_user_api_environment() -> None:
    combined = _text(LINUX_DEPLOY) + _text(WINDOWS_DEPLOY)

    for name in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ):
        assert not re.search(
            rf"(?:unset|SetEnvironmentVariable|Remove-Item)\s*[^\n]*{name}",
            combined,
            flags=re.IGNORECASE,
        )


def test_existing_clean_main_and_origin_gates_remain() -> None:
    linux = _text(LINUX_DEPLOY)
    windows = _text(WINDOWS_COORDINATOR)

    assert 'if [ "$CURRENT_BRANCH" != "main" ]' in linux
    assert "git status --porcelain=v1 --untracked-files=all" in linux
    assert 'if [ "$FETCHED_MAIN" != "$FORGE_EXPECTED_COMMIT" ]' in linux
    assert '$Branch -ne "main"' in windows
    assert "git status --porcelain=v1 --untracked-files=all" in windows
    assert "$Commit -ne $ProductionCommit" in windows
