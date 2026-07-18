"""Contracts for the stable GitHub check and VPS one-shot timers."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "capability-eval.yml"
DEPLOY = ROOT / "forge" / "scripts" / "deploy-vps.sh"
LOCAL_DEPLOY = ROOT / "forge" / "scripts" / "deploy.ps1"
WINDOWS_DEPLOY = ROOT / "forge" / "scripts" / "deploy-windows.ps1"
HERMES_INSTALLER = ROOT / "forge" / "hermes_change" / "installer.py"


def test_eval_is_the_single_stable_ruleset_context() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert re.findall(r"(?m)^  eval:\s*$", workflow) == ["  eval:"]
    assert "ruleset required status context" in workflow
    assert "private/free" not in workflow


def test_eval_runs_pytest_through_the_configured_python() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "python -m pytest tests/ -q" in workflow
    assert re.search(r"(?m)^\s+pytest tests/ -q$", workflow) is None


def test_repo_importing_services_run_from_repo_root() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")

    assert "WorkingDirectory=$REPO_DIR" in deploy
    assert "Environment=PYTHONPATH=$REPO_DIR" in deploy
    assert 'mkunit stage  "$PROCESS_LOCK $HERMES_PY ' in deploy
    assert "$REPO_DIR/forge/scripts/task-flow-worker.py --db $HERMES_DB" in deploy
    assert 'mkunit mirror  "$PROCESS_LOCK $HERMES_PY ' in deploy
    assert "$REPO_DIR/forge/scripts/issue-status-sync.py --db $HERMES_DB" in deploy
    for argument in (
        "--settings-db $TASK_SETTINGS_DB",
        "--outbox $CONFIRMED_TASKS_DB",
        "--gh $GH_BIN",
        "--repo $REPOSITORY",
    ):
        assert deploy.count(argument) >= 3
    assert "--hermes $HERMES_BIN" in deploy
    assert "--workspace dir:$REPO_DIR" in deploy


def test_writer_units_share_one_non_overlapping_process_lock() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")

    assert "Type=oneshot" in deploy
    assert "AccuracySec=1s" in deploy
    assert "[ -x /usr/bin/flock ]" in deploy
    assert (
        'PROCESS_LOCK="/usr/bin/flock --nonblock --conflict-exit-code 0 '
        '%t/forge-pipeline.lock"'
        in deploy
    )
    writer_units = {
        "ledger": "activity-log-writer.py",
        "stage": "task-flow-worker.py",
        "mirror": "issue-status-sync.py",
        "merge": "merge-worker.py",
        "flush": "send-pending-messages.py",
    }
    for unit, script in writer_units.items():
        assert deploy.count(f"mkunit {unit} ") == 1
        assert (
            f"$PROCESS_LOCK $HERMES_PY $REPO_DIR/forge/scripts/{script}"
            in deploy
        )
    assert "/usr/bin/python3" not in deploy
    assert '"OnCalendar=*-*-* *:*:00"' in deploy
    assert '"OnCalendar=*-*-* *:*:30"' in deploy
    assert "for T in ledger stage mirror canary drift morning merge flush" in deploy


def test_existing_systemd_unit_ids_stay_while_descriptions_become_plain() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")

    for unit in (
        "ledger",
        "stage",
        "mirror",
        "canary",
        "drift",
        "morning",
        "flush",
    ):
        assert '"$UD/forge-$1.service"' in deploy
        assert '"$UD/forge-$1.timer"' in deploy
        assert f"mkunit {unit} " in deploy
    assert "Description=INFINITY_FORGE $4" in deploy


def test_deploy_uses_each_servers_home_without_one_host_hardcoded() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")

    assert "/home/ubuntu" not in deploy
    assert 'REPO_DIR="${FORGE_REPO_DIR:-$HOME/work/INFINITY_FORGE}"' in deploy
    assert "$HOME/.hermes/hermes-agent/venv/bin" in deploy
    assert "$HOME/forge/system-check.sh" in deploy


def test_merge_unit_starts_with_auto_merge_disabled() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")

    assert "Environment=AUTO_MERGE_ENABLED=false" in deploy


def test_remote_deploy_only_fast_forwards_the_requested_clean_main_commit() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")

    assert "git pull" not in deploy
    assert "git stash" not in deploy
    assert 'CURRENT_BRANCH="$(git symbolic-ref --short HEAD)"' in deploy
    assert 'if [ "$CURRENT_BRANCH" != "main" ]' in deploy
    assert "git status --porcelain=v1 --untracked-files=all" in deploy
    assert 'git fetch origin main --quiet' in deploy
    assert 'FETCHED_MAIN="$(git rev-parse origin/main)"' in deploy
    assert 'if [ "$FETCHED_MAIN" != "$FORGE_EXPECTED_COMMIT" ]' in deploy
    assert 'git merge-base --is-ancestor HEAD "$FORGE_EXPECTED_COMMIT"' in deploy
    assert 'git merge --ff-only "$FORGE_EXPECTED_COMMIT"' in deploy
    assert "named stash" in deploy


def test_runtime_is_stopped_before_legacy_recheck_and_restored_on_error() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")

    stopped = deploy.index('systemctl --user stop "forge-$T.timer"')
    rechecked = deploy.index('LEGACY_ACTIVE=$(HERMES_DB="$HERMES_DB"')
    assert stopped < rechecked
    assert 'ACTIVE_TIMERS=""' in deploy
    assert 'ENABLED_TIMERS=""' in deploy
    assert 'systemctl --user is-active "forge-$T.timer"' in deploy
    assert 'systemctl --user is-enabled "forge-$T.timer"' in deploy
    assert 'restore_runtime_after_error()' in deploy
    assert 'for T in $MANAGED_TIMERS' in deploy
    assert 'systemctl --user disable "forge-$T.timer"' in deploy
    assert 'for T in $ACTIVE_TIMERS' in deploy
    assert 'systemctl --user start "forge-$T.timer"' in deploy
    assert 'systemctl --user stop hermes-gateway' in deploy
    assert 'trap restore_runtime_after_error EXIT' in deploy
    rollback_start = deploy.index("restore_runtime_after_error()")
    rollback_end = deploy.index("trap restore_runtime_after_error EXIT")
    rollback = deploy[rollback_start:rollback_end]
    assert rollback.index('systemctl --user stop hermes-gateway') < rollback.index(
        "restore_forge_environment"
    )
    assert rollback.index('systemctl --user stop "forge-$T.service"') < rollback.index(
        "restore_plugin_state"
    )


def test_deploy_enables_plugin_without_waiting_for_operator_input() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")

    command = (
        '"$HERMES_PY" -m hermes_cli.main plugins enable '
        'infinity-forge --no-allow-tool-override'
    )
    assert command in deploy
    assert (
        '"$HERMES_PY" -m hermes_cli.main plugins enable infinity-forge\n'
        not in deploy
    )


def test_server_deploy_publishes_clean_commit_release_atomically() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")

    assert 'FORGE_RELEASE_ROOT="$HOME/.hermes/infinity-forge/releases"' in deploy
    assert 'FORGE_RELEASE="$FORGE_RELEASE_ROOT/$DEPLOYED_COMMIT"' in deploy
    assert 'FORGE_RELEASE_ROOT="$TASK_DATA_DIR/releases"' not in deploy
    assert 'RELEASE_TEMP="$(mktemp -d ' in deploy
    assert 'git ls-tree -r "$DEPLOYED_COMMIT"' in deploy
    assert 'managed release cannot contain symbolic links' in deploy
    assert 'find "$FORGE_RELEASE" -type l -print -quit' in deploy
    assert 'existing managed release contains a symbolic link' in deploy
    assert 'git archive "$DEPLOYED_COMMIT" | tar -x -C "$RELEASE_TEMP"' in deploy
    assert 'diff -qr "$RELEASE_TEMP" "$FORGE_RELEASE"' in deploy
    assert 'mv -T "$RELEASE_TEMP" "$FORGE_RELEASE"' in deploy
    assert deploy.index('git archive "$DEPLOYED_COMMIT"') < deploy.index(
        'mv -T "$RELEASE_TEMP" "$FORGE_RELEASE"'
    )
    assert '"$FORGE_RELEASE_ROOT"/.build-*) rm -rf -- "$RELEASE_TEMP"' in deploy


def test_server_deploy_upgrades_physical_plugin_to_atomic_version_link() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")

    assert 'PLUGIN_RELEASE_ROOT="$HOME/.hermes/plugin-releases"' in deploy
    assert 'PLUGIN_RELEASE="$PLUGIN_RELEASE_ROOT/$DEPLOYED_COMMIT"' in deploy
    assert 'PLUGIN_LINK="$HOME/.hermes/plugins/infinity-forge"' in deploy
    assert 'release-path.txt' in deploy
    assert 'find "$PLUGIN_RELEASE" -type l -print -quit' in deploy
    assert 'existing plugin release contains a symbolic link' in deploy
    assert 'if [ -d "$PLUGIN_LINK" ] && [ ! -L "$PLUGIN_LINK" ]' in deploy
    assert 'mv -T "$PLUGIN_LINK" "$PLUGIN_BACKUP"' in deploy
    assert 'ln -s -- "$PLUGIN_RELEASE" "$PLUGIN_LINK_STAGE/infinity-forge"' in deploy
    assert (
        'mv -Tf "$PLUGIN_LINK_STAGE/infinity-forge" "$PLUGIN_LINK"'
        in deploy
    )
    assert "PLUGIN_ROLLBACK_OK" not in deploy
    assert "PLUGIN_RELEASE_CREATED" not in deploy
    assert 'rm -rf -- "$PLUGIN_RELEASE"' not in deploy
    assert 'mkdir -p "$PLUGIN_DIR"' not in deploy
    assert '[ ! -L "$PLUGIN_RELEASE" ]' in deploy
    assert '[ ! -L "$FORGE_RELEASE" ]' in deploy


def test_server_deploy_saves_and_can_restore_only_three_runtime_keys() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")

    assert "from hermes_cli.config import save_env_value" in deploy
    assert "from hermes_cli.config import remove_env_value" in deploy
    assert 'ENV_BACKUP="$(mktemp "$TASK_DATA_DIR/.env-backup.' in deploy
    assert 'ENV_CHANGED=true' in deploy
    for key in (
        "INFINITY_FORGE_REPOSITORY",
        "INFINITY_FORGE_TASK_SETTINGS_DB",
        "INFINITY_FORGE_GH_PATH",
    ):
        assert key in deploy
    assert '"$TASK_DATA_DIR"/.env-backup.*) rm -f -- "$ENV_BACKUP"' in deploy
    assert "runtime settings rollback failed" in deploy
    assert "print(payload)" not in deploy


def test_both_server_deploy_layers_run_the_same_actual_chooser_smoke() -> None:
    server_deploy = DEPLOY.read_text(encoding="utf-8")
    local_deploy = LOCAL_DEPLOY.read_text(encoding="utf-8")
    begin = "# INFINITY_FORGE_CHOOSER_SMOKE_BEGIN"
    end = "# INFINITY_FORGE_CHOOSER_SMOKE_END"

    def smoke_block(script: str) -> str:
        assert script.count(begin) == 1
        assert script.count(end) == 1
        return script.split(begin, 1)[1].split(end, 1)[0]

    server_smoke = smoke_block(server_deploy)
    local_smoke = smoke_block(local_deploy)

    assert server_smoke == local_smoke
    assert 'CHOOSER_EXPECTED_COMMIT="$DEPLOYED_COMMIT"' in server_deploy
    assert 'CHOOSER_EXPECTED_COMMIT="$EXPECTED_COMMIT"' in local_deploy
    for script in (server_deploy, local_deploy):
        assert 'CHOOSER_HERMES_ROOT="$HERMES_ROOT"' in script
        assert 'CHOOSER_EXPECTED_REPOSITORY="$REPOSITORY"' in script
        assert 'CHOOSER_EXPECTED_TASK_SETTINGS_DB="$TASK_SETTINGS_DB"' in script
        assert 'CHOOSER_EXPECTED_GH_PATH="$GH_BIN"' in script
    for contract in (
        'CHOOSER_SMOKE_CWD="$(mktemp -d ',
        'chmod 700 "$CHOOSER_SMOKE_CWD"',
        'rmdir -- "$CHOOSER_SMOKE_CWD" 2>/dev/null || true',
        'cd "$CHOOSER_SMOKE_CWD"',
        "-u PYTHONPATH",
        "-u PYTHONHOME",
        "-u PYTHONOPTIMIZE",
        "-u INFINITY_FORGE_REPOSITORY",
        "-u INFINITY_FORGE_TASK_SETTINGS_DB",
        "-u INFINITY_FORGE_GH_PATH",
        'HERMES_HOME="$HOME/.hermes"',
        "PYTHONDONTWRITEBYTECODE=1",
        'CHOOSER_HERMES_ROOT="$CHOOSER_HERMES_ROOT"',
        'CHOOSER_EXPECTED_REPOSITORY="$CHOOSER_EXPECTED_REPOSITORY"',
        'CHOOSER_EXPECTED_TASK_SETTINGS_DB="$CHOOSER_EXPECTED_TASK_SETTINGS_DB"',
        'CHOOSER_EXPECTED_GH_PATH="$CHOOSER_EXPECTED_GH_PATH"',
        "from hermes_cli.env_loader import load_hermes_dotenv",
        'load_hermes_dotenv(project_env=hermes_project_root / ".env")',
        'os.environ["INFINITY_FORGE_REPOSITORY"]',
        'os.environ["CHOOSER_EXPECTED_REPOSITORY"]',
        'os.environ["INFINITY_FORGE_TASK_SETTINGS_DB"]',
        'os.environ["CHOOSER_EXPECTED_TASK_SETTINGS_DB"]',
        'os.environ["INFINITY_FORGE_GH_PATH"]',
        'os.environ["CHOOSER_EXPECTED_GH_PATH"]',
        "from hermes_cli.plugins import discover_plugins",
        "from hermes_cli.plugins import get_plugin_manager",
        "from hermes_cli.plugins import has_hook",
        "discover_plugins(force=True)",
        'manager._plugins["infinity-forge"]',
        "loaded.enabled is True",
        "loaded.error is None",
        '"pre_user_turn" in loaded.hooks_registered',
        'has_hook("pre_user_turn")',
        "loaded.module is not None",
        "loaded.manifest.path is not None",
        'getattr(module, "_MANAGED_RELEASE", None)',
        "module.set_task_service(forbid_task_service)",
        "module.before_user_turn(",
        'result["action"] == "handled"',
        '[choice["id"] for choice in result["choices"]] == ["chat", "task"]',
        'raise AssertionError("Task service must not run during chooser smoke")',
    ):
        assert contract in server_smoke
    assert server_smoke.index("load_hermes_dotenv(") < server_smoke.index(
        "from hermes_cli.plugins import"
    )
    assert "invoke_hook" not in server_smoke
    assert "PYTHONPATH=" not in server_smoke
    assert "sys.path" not in server_smoke
    assert "print(" not in server_smoke
    assert 'rm -rf -- "$CHOOSER_SMOKE_CWD"' not in server_smoke


def test_deployable_hermes_change_carries_the_classic_cli_chooser() -> None:
    installer = HERMES_INSTALLER.read_text(encoding="utf-8")
    server_deploy = DEPLOY.read_text(encoding="utf-8")
    windows_deploy = WINDOWS_DEPLOY.read_text(encoding="utf-8")

    assert '"cli.py": change_cli_source' in installer
    assert "_prompt_choice_modal" in installer
    assert 'install-hermes-change.py" build' in server_deploy
    assert '--hermes-root "$HERMES_SOURCE_TEMP"' in server_deploy
    assert '"cli.py"' in windows_deploy


def test_hermes_change_package_is_version_bound_and_committed_atomically() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")

    assert 'HERMES_SOURCE_VERSION="$(git -C "$HERMES_ROOT" rev-parse HEAD)"' in deploy
    assert 'CHANGE_PACKAGE_ROOT="$TASK_DATA_DIR/hermes-user-turn-changes"' in deploy
    assert 'CHANGE_PACKAGE_VERSION="${FORGE_EXPECTED_COMMIT}-${HERMES_SOURCE_VERSION}"' in deploy
    assert 'CHANGE_PACKAGE="$CHANGE_PACKAGE_ROOT/$CHANGE_PACKAGE_VERSION"' in deploy
    assert 'HERMES_SOURCE_TEMP="$(mktemp -d ' in deploy
    assert 'git -C "$HERMES_ROOT" archive "$HERMES_SOURCE_VERSION"' in deploy
    assert 'tar -x -C "$HERMES_SOURCE_TEMP"' in deploy
    assert 'PACKAGE_TEMP="$(mktemp -d ' in deploy
    assert '--hermes-root "$HERMES_SOURCE_TEMP"' in deploy
    assert '--package "$PACKAGE_TEMP"' in deploy
    assert '--source-version "$CHANGE_PACKAGE_VERSION"' in deploy
    assert 'EXPECTED_PACKAGE_VERSION="$CHANGE_PACKAGE_VERSION"' in deploy
    assert 'payload["source_version"] == os.environ["EXPECTED_PACKAGE_VERSION"]' in deploy
    assert 'mv -T "$PACKAGE_TEMP" "$CHANGE_PACKAGE"' in deploy
    assert deploy.index('--package "$PACKAGE_TEMP"') < deploy.index(
        'mv -T "$PACKAGE_TEMP" "$CHANGE_PACKAGE"'
    )


def test_local_deploy_pushes_without_staging_and_checks_both_servers() -> None:
    deploy = LOCAL_DEPLOY.read_text(encoding="utf-8")

    assert "git push" in deploy
    assert "git add" not in deploy
    assert "git commit" not in deploy
    assert "git pull" not in deploy
    assert "origin/main" in deploy
    assert 'HostName "My-EC2"' in deploy
    assert 'HostName "ubuntu@51.222.27.48"' in deploy
    assert "FORGE_EXPECTED_COMMIT" in deploy


def test_local_deploy_uses_absolute_remote_paths_and_requires_clean_main() -> None:
    deploy = LOCAL_DEPLOY.read_text(encoding="utf-8")

    assert "~/work/INFINITY_FORGE" not in deploy
    assert "/home/ec2-user/work/INFINITY_FORGE" in deploy
    assert "/home/ubuntu/work/INFINITY_FORGE" in deploy
    assert "git symbolic-ref --short HEAD" in deploy
    assert "git status --porcelain=v1 --untracked-files=all" in deploy
    assert "git stash" not in deploy
    assert "named stash" in deploy
    assert "$RemotePrepareScript" not in deploy
    assert 'git merge --ff-only "$EXPECTED_COMMIT"' not in deploy
    assert 'deploy-vps.sh" --post-update' not in deploy


def test_server_apply_runs_update_and_deploy_under_one_remote_lock() -> None:
    deploy = LOCAL_DEPLOY.read_text(encoding="utf-8")
    start = deploy.index("function Invoke-ForgeServerDeploy")
    verification = deploy.index("  $VerificationScript = @'", start)
    apply = deploy[start:verification]

    assert apply.count("Invoke-RemoteBashScript") == 1
    assert apply.count("$DeployBootstrapScript = @'") == 1
    assert apply.index("git -C $RemoteRepo symbolic-ref --short HEAD") < apply.index(
        "$DeployBootstrapScript = @'"
    )
    assert apply.index(
        "git -C $RemoteRepo status --porcelain=v1 --untracked-files=all"
    ) < apply.index("$DeployBootstrapScript = @'")
    for contract in (
        'DEPLOY_LOCK_ROOT="$HOME/.hermes/infinity-forge"',
        'DEPLOY_LOCK_FILE="$DEPLOY_LOCK_ROOT/deploy.lock"',
        "mkdir -p \"$DEPLOY_LOCK_ROOT\"",
        "if [ ! -x /usr/bin/flock ]; then",
        'exec 9>"$DEPLOY_LOCK_FILE"',
        "/usr/bin/flock --nonblock 9",
        "another Infinity Forge deployment is already running",
        'export INFINITY_FORGE_DEPLOY_LOCK_FD9="$DEPLOY_LOCK_FILE"',
        'FORGE_EXPECTED_COMMIT="$EXPECTED_COMMIT"',
        'FORGE_REPO_DIR="$REPO_DIR"',
        'bash "$REPO_DIR/forge/scripts/deploy-vps.sh"',
        "-Script $DeployBootstrapScript",
        "배포 잠금을 얻지 못했거나 배포가 실패했습니다.",
    ):
        assert contract in apply
    assert "git fetch" not in apply
    assert "git merge --ff-only" not in apply
    assert "--post-update" not in apply
    assert 'ssh $HostName env "FORGE_EXPECTED_COMMIT=' not in apply


def test_local_deploy_sends_remote_bash_without_windows_line_endings() -> None:
    deploy = LOCAL_DEPLOY.read_text(encoding="utf-8")

    assert "function Invoke-RemoteBashScript" in deploy
    assert "[Convert]::ToBase64String" in deploy
    assert "base64 --decode | bash -s --" in deploy
    assert "$DeployBootstrapScript | ssh" not in deploy
    assert "$VerificationScript | ssh" not in deploy


def test_all_selected_preflights_finish_before_first_apply() -> None:
    deploy = LOCAL_DEPLOY.read_text(encoding="utf-8")

    ec2_preflight = deploy.index(
        'Test-ForgeServerPreflight -Name "EC2"'
    )
    vps_preflight = deploy.index(
        'Test-ForgeServerPreflight -Name "VPS"'
    )
    windows_preflight = deploy.index('-Mode "Preflight"')
    first_apply = deploy.index('Invoke-ForgeServerDeploy -Name "EC2"')

    assert max(ec2_preflight, vps_preflight, windows_preflight) < first_apply


def test_server_preflight_does_not_update_remote_repository() -> None:
    deploy = LOCAL_DEPLOY.read_text(encoding="utf-8")
    start = deploy.index("function Test-ForgeServerPreflight")
    end = deploy.index("function Invoke-ForgeServerDeploy", start)
    preflight = deploy[start:end]

    assert "git -C $RemoteRepo ls-remote origin refs/heads/main" in preflight
    assert "git fetch" not in preflight
    assert "git merge" not in preflight
    assert "deploy-vps.sh" not in preflight
    assert "merge-base --is-ancestor $RemoteCommit $Commit" in preflight


def test_three_environment_apply_order_is_ec2_vps_windows() -> None:
    deploy = LOCAL_DEPLOY.read_text(encoding="utf-8")

    ec2 = deploy.index('Invoke-ForgeServerDeploy -Name "EC2"')
    vps = deploy.index('Invoke-ForgeServerDeploy -Name "VPS"')
    windows = deploy.index('-Mode "Apply"')

    assert ec2 < vps < windows


def test_orchestrator_records_atomic_non_secret_deployment_report() -> None:
    deploy = LOCAL_DEPLOY.read_text(encoding="utf-8")

    assert "deployment-report.json" in deploy
    assert "formatVersion = 1" in deploy
    assert "requestedCommit = $Commit" in deploy
    assert "startedAtUtc = $StartedAtUtc" in deploy
    assert "finishedAtUtc" in deploy
    assert "targets = $TargetResults" in deploy
    assert "skipped =" in deploy
    assert "Set-Content -LiteralPath $ReportTemp" in deploy
    assert "Move-Item -Force -LiteralPath $ReportTemp" in deploy
    assert "RISK(security)" in deploy
    assert "Get-Content $EnvFile" not in deploy


def test_skip_switches_are_reported_instead_of_full_success() -> None:
    deploy = LOCAL_DEPLOY.read_text(encoding="utf-8")

    for switch, target in (
        ("$SkipEC2", '"EC2"'),
        ("$SkipVPS", '"VPS"'),
        ("$SkipLocal", '"Windows"'),
    ):
        assert f"if ({switch})" in deploy
        assert f"Set-TargetSkipped -Name {target}" in deploy
    assert "선택한 대상 확인 완료" in deploy


def test_runbook_documents_the_single_three_target_command() -> None:
    runbook = (ROOT / "docs" / "user-runbook.md").read_text(encoding="utf-8")

    assert "Windows·EC2·VPS 단일 명령 배포" in runbook
    assert "pwsh -NoProfile -File forge/scripts/deploy.ps1" in runbook
    assert "%LOCALAPPDATA%\\InfinityForge\\state\\deployment-report.json" in runbook
    assert "EC2 → VPS → Windows" in runbook
    assert "같은 SHA로 다시 실행" in runbook
    assert "git pull --ff-only origin main" not in runbook


def test_local_deploy_verifies_complete_runtime_and_smokes_workers() -> None:
    deploy = LOCAL_DEPLOY.read_text(encoding="utf-8")

    for contract in (
        "hermes-gateway",
        "plugins list --enabled --user --plain",
        "INFINITY_FORGE_PRE_USER_TURN_V1",
        "builder reviewer deep_checker fix",
        "task-settings.db",
        "task-outbox.db",
        "forge-$Timer.timer",
        "ExecStart",
        "AUTO_MERGE_ENABLED=false",
        "task-flow-worker.py",
        "issue-status-sync.py",
        "merge-worker.py",
        "--dry-run",
        "--check-port",
    ):
        assert contract in deploy
    assert 'REPOSITORY="$(cd "$REPO_DIR" && "$GH_BIN" repo view' in deploy
    assert "repo view --repo" not in deploy
