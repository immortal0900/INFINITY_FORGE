# Task 7 Report — Windows·Linux subscription deployment

## Status

DONE_WITH_CONCERNS

Task-only commit subject: `feat: deploy subscription runtime on all hosts`

Base commit: `02068fc15441f37d83dcb1a1e29671ba0191e9d3`

## Implemented

- Linux deploy now pins Claude Code `2.1.212` through the exact official installer command when needed and validates the exact Max first-party auth JSON before the first runtime/service mutation.
- Linux and Windows install the seven-file Hermes carried package, stable subscription runner, and the repo-managed `codex`/`claude-code` skills for the default runtime and four Task profiles.
- Profile `.codex`, `.claude`, and `.claude.json` destinations preserve an exact timestamped backup before linking to real login sources.
- Linux gateway drop-in and Windows User/current-process environments use the same six subscription variables and stable paths.
- Both operating-system paths run configure `apply`, then `verify`, then restart/verify the gateway. Failure handling calls configure rollback after an apply attempt and restores managed artifacts, links, environment values, carried source, and gateway/service state as far as owned by this task.
- Existing local/remote clean `main == origin/main` gates remain intact. Remote verification now checks the seventh worker marker, stable runner, skills, profile auth links, subscription environment, and Task 6 verify result.

## TDD evidence

- RED: new focused contract initially produced `8 failed, 29 passed`; failures matched the absent Task 7 deployment behavior.
- GREEN: `python -m pytest tests/ops/test_subscription_deploy_contract.py tests/ops/test_workflow_contract.py tests/ops/test_plain_names.py -q` → `37 passed`.

## Regression and syntax evidence

- Full regression: `python -m pytest tests -q` → `724 passed, 7 skipped`.
- Linux syntax: Git Bash `bash -n forge/scripts/deploy-vps.sh` → PASS.
- Windows syntax: PowerShell AST parse of `forge/scripts/deploy.ps1` → PASS.
- Patch hygiene: `git diff --check` → PASS.

## Concern

Per task safety constraints, no live deploy, SSH, native installer, gateway restart, persistent User environment write, or live configure apply was executed. Validation is therefore test/static-syntax only. Windows profile symbolic-link creation also depends on the host allowing user-created symbolic links; if unavailable, the script fails and enters rollback rather than copying credentials.
