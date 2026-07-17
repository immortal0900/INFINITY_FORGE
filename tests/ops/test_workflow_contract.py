"""Contracts for the stable GitHub check and VPS one-shot timers."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "capability-eval.yml"
DEPLOY = ROOT / "forge" / "scripts" / "deploy-vps.sh"
LOCAL_DEPLOY = ROOT / "forge" / "scripts" / "deploy.ps1"


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
    assert '$RemotePrepareScript = @\'' in deploy
    assert 'git fetch origin main --quiet' in deploy
    assert 'git merge --ff-only "$EXPECTED_COMMIT"' in deploy
    assert 'deploy-vps.sh" --post-update' in deploy


def test_local_deploy_sends_remote_bash_without_windows_line_endings() -> None:
    deploy = LOCAL_DEPLOY.read_text(encoding="utf-8")

    assert "function Invoke-RemoteBashScript" in deploy
    assert "[Convert]::ToBase64String" in deploy
    assert "base64 --decode | bash -s --" in deploy
    assert "$RemotePrepareScript | ssh" not in deploy
    assert "$VerificationScript | ssh" not in deploy


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
