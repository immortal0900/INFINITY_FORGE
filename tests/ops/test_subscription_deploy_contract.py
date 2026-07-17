"""Static deployment contract for the subscription runtime on every host."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LINUX_DEPLOY = ROOT / "forge" / "scripts" / "deploy-vps.sh"
WINDOWS_DEPLOY = ROOT / "forge" / "scripts" / "deploy.ps1"

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
    installer = "curl -fsSL https://claude.ai/install.sh | bash -s 2.1.212"
    auth_probe = '"$CLAUDE_BIN" auth status --json'
    first_stop = deploy.index('systemctl --user stop "forge-$T.timer"')

    assert 'CLAUDE_VERSION="2.1.212"' in deploy
    assert installer in deploy
    assert auth_probe in deploy
    assert deploy.index(installer) < first_stop
    assert deploy.index(auth_probe) < first_stop
    assert all(
        field in deploy
        for field in (
            'payload.get("loggedIn") is True',
            'payload.get("authMethod") == "claude.ai"',
            'payload.get("apiProvider") == "firstParty"',
            'payload.get("subscriptionType") == "max"',
        )
    )
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
        "INFINITY_FORGE_REPO": ('$REPO_DIR', '$Repo'),
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
    restart = deploy.index('"$HERMES_BIN" gateway restart')

    assert apply < verify < restart
    assert 'CONFIGURE_APPLIED=true' in deploy
    assert '"$HERMES_PY" "$CONFIGURE_SCRIPT" rollback' in deploy
    assert 'systemctl --user is-active --quiet hermes-gateway' in deploy[restart:]
    assert 'trap restore_runtime_after_error EXIT' in deploy


def test_remote_verification_checks_all_seven_carried_files() -> None:
    deploy = _text(WINDOWS_DEPLOY)
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
    install = deploy.index("$ChangeInstaller install")
    stable_copy = deploy.index("subscription-runner.py", install)
    apply = deploy.index("$ConfigureScript apply", stable_copy)
    verify = deploy.index("$ConfigureScript verify", apply)
    restart = deploy.index("gateway restart", verify)

    assert '$HermesRoot = Join-Path $env:LOCALAPPDATA "hermes\\hermes-agent"' in deploy
    assert '$HermesPython = Join-Path $HermesRoot "venv\\Scripts\\python.exe"' in deploy
    assert '$StableRunner = Join-Path $env:LOCALAPPDATA "InfinityForge\\subscription-runtime\\subscription-runner.py"' in deploy
    assert install < stable_copy < apply < verify < restart
    assert '[Environment]::SetEnvironmentVariable($Name, $Value, "User")' in deploy
    assert 'Set-Item -Path "Env:$Name" -Value $Value' in deploy
    assert '[Environment]::SetEnvironmentVariable($Name, $PreviousValue, "User")' in deploy
    capture = deploy.index('$PreviousUserEnvironment[$Name] =')
    first_persist = deploy.index('[Environment]::SetEnvironmentVariable($Name, $Value, "User")')
    assert capture < first_persist
    assert "$ConfigureScript rollback" in deploy
    assert "gateway status" in deploy[restart:]
    local = deploy[deploy.index('if (-not $SkipLocal) {') :]
    assert local.index("auth status --json") < local.index("New-Item -ItemType Directory -Force -Path $DeploymentRoot")
    assert local.index("$CodexProbe") < local.index("$ChangeInstaller install")
    assert local.index("Move-Item -LiteralPath $Destination -Destination $Backup") < local.index("New-Item -ItemType SymbolicLink -Path $Destination")


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
    windows = _text(WINDOWS_DEPLOY)

    assert 'if [ "$CURRENT_BRANCH" != "main" ]' in linux
    assert "git status --porcelain=v1 --untracked-files=all" in linux
    assert 'if [ "$FETCHED_MAIN" != "$FORGE_EXPECTED_COMMIT" ]' in linux
    assert '$Branch -ne "main"' in windows
    assert "git status --porcelain=v1 --untracked-files=all" in windows
    assert "$Commit -ne $ProductionCommit" in windows
