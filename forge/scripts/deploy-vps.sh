#!/bin/bash
# INFINITY_FORGE — 서버의 검증된 main 커밋과 Hermes 자산을 반영합니다.
# 실행 위치: 서버의 INFINITY_FORGE clone. 로컬 변경은 운영자가 먼저 보관해야 합니다.
set -euo pipefail

acquire_deploy_lock() {
  DEPLOY_LOCK_FILE="$HOME/.hermes/infinity-forge/deploy.lock"
  if [ ! -x /usr/bin/flock ]; then
    echo "[deploy] /usr/bin/flock is required for deployment locking" >&2
    exit 1
  fi
  mkdir -p "$HOME/.hermes/infinity-forge"

  # fast-forward 후 exec로 재진입하면 같은 FD와 lock을 그대로 사용한다.
  # 환경 marker만 위조됐거나 FD가 사라졌다면 반드시 다시 lock을 잡는다.
  if [ "${INFINITY_FORGE_DEPLOY_LOCK_FD9:-}" = "$DEPLOY_LOCK_FILE" ]; then
    LOCK_FD_TARGET="$(readlink -f "/proc/$$/fd/9" 2>/dev/null || true)"
    LOCK_FILE_TARGET="$(readlink -f "$DEPLOY_LOCK_FILE" 2>/dev/null || true)"
    if [ -n "$LOCK_FD_TARGET" ] && [ "$LOCK_FD_TARGET" = "$LOCK_FILE_TARGET" ]; then
      if /usr/bin/flock --nonblock 9; then
        return 0
      fi
      echo "[deploy] another Infinity Forge deployment is already running" >&2
      exit 1
    fi
  fi

  exec 9>"$DEPLOY_LOCK_FILE"
  if ! /usr/bin/flock --nonblock 9; then
    echo "[deploy] another Infinity Forge deployment is already running" >&2
    exec 9>&-
    exit 1
  fi
  export INFINITY_FORGE_DEPLOY_LOCK_FD9="$DEPLOY_LOCK_FILE"
}

# main 함수로 전체를 감싸서 fast-forward가 이 파일을 갱신해도 현재 실행을 안전하게 끝낸다.
main() {
acquire_deploy_lock
REPO_DIR="${FORGE_REPO_DIR:-$HOME/work/INFINITY_FORGE}"
cd "$REPO_DIR"

if ! printf '%s\n' "${FORGE_EXPECTED_COMMIT:-}" | grep -Eq '^[0-9a-f]{40}$'; then
  echo "[deploy] FORGE_EXPECTED_COMMIT must be a full Git commit ID" >&2
  exit 1
fi

CURRENT_BRANCH="$(git symbolic-ref --short HEAD)"
if [ "$CURRENT_BRANCH" != "main" ]; then
  echo "[deploy] server repository must be on main" >&2
  exit 1
fi
WORKTREE_CHANGES="$(git status --porcelain=v1 --untracked-files=all)"
if [ -n "$WORKTREE_CHANGES" ]; then
  echo "[deploy] server repository is not clean; create a manual named stash before deployment" >&2
  printf '%s\n' "$WORKTREE_CHANGES" >&2
  exit 1
fi

# 요청된 origin/main 커밋만 fast-forward한 뒤 갱신된 스크립트를 한 번 재실행한다.
if [ "${1:-}" != "--post-update" ]; then
  git fetch origin main --quiet
  FETCHED_MAIN="$(git rev-parse origin/main)"
  if [ "$FETCHED_MAIN" != "$FORGE_EXPECTED_COMMIT" ]; then
    echo "[deploy] requested commit is not the fetched origin/main" >&2
    exit 1
  fi
  if ! git merge-base --is-ancestor HEAD "$FORGE_EXPECTED_COMMIT"; then
    echo "[deploy] server main cannot fast-forward to the requested commit" >&2
    exit 1
  fi
  git merge --ff-only "$FORGE_EXPECTED_COMMIT"
  [ -z "$(git status --porcelain=v1 --untracked-files=all)" ] || {
    echo "[deploy] repository changed unexpectedly during fast-forward" >&2
    exit 1
  }
  exec bash "$REPO_DIR/forge/scripts/deploy-vps.sh" --post-update
fi

DEPLOYED_COMMIT="$(git rev-parse HEAD)"
if [ "$DEPLOYED_COMMIT" != "$FORGE_EXPECTED_COMMIT" ]; then
  echo "[deploy] expected commit is not checked out" >&2
  exit 1
fi

HERMES_ROOT="$HOME/.hermes/hermes-agent"
HERMES_PY="$HERMES_ROOT/venv/bin/python"
HERMES_BIN="$HERMES_ROOT/venv/bin/hermes"
GH_BIN="${INFINITY_FORGE_GH_PATH:-/usr/bin/gh}"
[ -x "$HERMES_PY" ] || { echo "[deploy] Hermes Python is missing" >&2; exit 1; }
[ -x "$HERMES_BIN" ] || { echo "[deploy] Hermes command is missing" >&2; exit 1; }
[ -x "$GH_BIN" ] || { echo "[deploy] GitHub command is missing" >&2; exit 1; }

resolve_codex_bin() {
  local candidate
  candidate="$(command -v codex 2>/dev/null || true)"
  if [ -n "$candidate" ] && "$candidate" --version >/dev/null 2>&1; then
    printf '%s\n' "$candidate"
    return 0
  fi

  local package=""
  case "$(uname -m)" in
    x86_64) package="codex-linux-x64" ;;
    aarch64|arm64) package="codex-linux-arm64" ;;
  esac
  [ -n "$package" ] || return 0

  local native_pattern="$HOME/.local/lib/node_modules/@openai/codex/node_modules/@openai/$package/vendor/*/bin/codex"
  local native_candidates=()
  while IFS= read -r candidate; do
    if [ -x "$candidate" ] && "$candidate" --version >/dev/null 2>&1; then
      native_candidates+=("$candidate")
    fi
  done < <(compgen -G "$native_pattern" || true)
  if [ "${#native_candidates[@]}" -eq 1 ]; then
    printf '%s\n' "${native_candidates[0]}"
  fi
}
CODEX_BIN="$(resolve_codex_bin)"
[ -n "$CODEX_BIN" ] || { echo "[deploy] a working Codex command is required" >&2; exit 78; }

CLAUDE_VERSION="2.1.215"
CLAUDE_NATIVE_BIN="$HOME/.local/bin/claude"
resolve_claude_bin() {
  if [ -x "$CLAUDE_NATIVE_BIN" ]; then
    printf '%s\n' "$CLAUDE_NATIVE_BIN"
    return 0
  fi
  command -v claude 2>/dev/null || true
}
CLAUDE_BIN="$(resolve_claude_bin)"
CLAUDE_ACTUAL_VERSION=""
if [ -n "$CLAUDE_BIN" ]; then
  CLAUDE_ACTUAL_VERSION="$("$CLAUDE_BIN" --version 2>/dev/null | awk 'NR == 1 { print $1 }')"
fi
if [ "$CLAUDE_ACTUAL_VERSION" != "$CLAUDE_VERSION" ]; then
  # RISK(security): execute only the pinned official native installer before any service/config mutation.
  curl -fsSL https://claude.ai/install.sh | bash -s 2.1.215
  hash -r
  CLAUDE_BIN="$(resolve_claude_bin)"
  [ -n "$CLAUDE_BIN" ] || { echo "[deploy] Claude Code installation failed" >&2; exit 78; }
  CLAUDE_ACTUAL_VERSION="$("$CLAUDE_BIN" --version 2>/dev/null | awk 'NR == 1 { print $1 }')"
  [ "$CLAUDE_ACTUAL_VERSION" = "$CLAUDE_VERSION" ] || {
    echo "[deploy] Claude Code 2.1.215 is required" >&2
    exit 78
  }
fi

# RISK(security): keep the JSON private and validate only first-party subscription auth.
CLAUDE_AUTH_JSON="$("$CLAUDE_BIN" auth status --json 2>/dev/null)" || {
  echo "[deploy] Claude.ai subscription login required; run: claude auth login" >&2
  exit 78
}
if ! printf '%s' "$CLAUDE_AUTH_JSON" | "$HERMES_PY" -c '
import json, sys
sys.path.insert(0, sys.argv[1])
from forge.ops.subscription_runtime import is_claude_subscription_auth
try:
    payload = json.load(sys.stdin)
except (json.JSONDecodeError, UnicodeError):
    raise SystemExit(1)
valid = isinstance(payload, dict) and is_claude_subscription_auth(payload)
raise SystemExit(0 if valid else 1)
' "$REPO_DIR"; then
  unset CLAUDE_AUTH_JSON
  echo "[deploy] Claude.ai subscription login required; run: claude auth login" >&2
  exit 78
fi
unset CLAUDE_AUTH_JSON

for AUTH_SOURCE in "$HOME/.codex" "$HOME/.claude"; do
  [ -d "$AUTH_SOURCE" ] && [ ! -L "$AUTH_SOURCE" ] || {
    echo "[deploy] a real login directory is missing; complete codex/claude login first" >&2
    exit 78
  }
done
[ -f "$HOME/.claude.json" ] && [ ! -L "$HOME/.claude.json" ] || {
  echo "[deploy] Claude login state is missing; run: claude auth login" >&2
  exit 78
}

REPOSITORY="${INFINITY_FORGE_REPOSITORY:-$($GH_BIN repo view --json nameWithOwner --jq .nameWithOwner)}"
TASK_DATA_DIR="${INFINITY_FORGE_TASK_DATA_DIR:-$HOME/.hermes/infinity-forge}"
TASK_SETTINGS_DB="$TASK_DATA_DIR/task-settings.db"
CONFIRMED_TASKS_DB="$TASK_SETTINGS_DB.task-outbox.db"
HERMES_DB="$HOME/.hermes/kanban.db"
STABLE_RUNNER="$HOME/.hermes/infinity-forge/bin/subscription-runner.py"
CONFIGURE_SCRIPT="$REPO_DIR/forge/scripts/configure-subscription-runtime.py"
CLAUDE_MCP_CONFIG="$TASK_DATA_DIR/subscription-runtime/claude-mcp.json"
mkdir -p "$TASK_DATA_DIR"

# Plugin bootstrap은 사용자 Hermes home 아래의 이 고정 경로만 신뢰한다.
# 작업 데이터 경로 override가 코드 실행 root를 바꾸게 두지 않는다.
FORGE_RELEASE_ROOT="$HOME/.hermes/infinity-forge/releases"
FORGE_RELEASE="$FORGE_RELEASE_ROOT/$DEPLOYED_COMMIT"
PLUGIN_ROOT="$HOME/.hermes/plugins"
PLUGIN_LINK="$HOME/.hermes/plugins/infinity-forge"
PLUGIN_RELEASE_ROOT="$HOME/.hermes/plugin-releases"
PLUGIN_RELEASE="$PLUGIN_RELEASE_ROOT/$DEPLOYED_COMMIT"
PLUGIN_BACKUP_ROOT="$HOME/.hermes/plugin-backups/infinity-forge"
mkdir -p \
  "$FORGE_RELEASE_ROOT" \
  "$PLUGIN_ROOT" \
  "$PLUGIN_RELEASE_ROOT" \
  "$PLUGIN_BACKUP_ROOT"

MANAGED_TIMERS="ledger stage mirror canary drift morning merge flush messages"
ACTIVE_TIMERS=""
ENABLED_TIMERS=""
GATEWAY_WAS_ACTIVE=false
if systemctl --user is-active hermes-gateway >/dev/null 2>&1; then
  GATEWAY_WAS_ACTIVE=true
fi
CHANGE_PACKAGE_ROOT="$TASK_DATA_DIR/hermes-user-turn-changes"
PACKAGE_TEMP=""
HERMES_SOURCE_TEMP=""
RELEASE_TEMP=""
PLUGIN_TEMP=""
PLUGIN_LINK_STAGE=""
PLUGIN_PREVIOUS_KIND=""
PLUGIN_PREVIOUS_LINK=""
PLUGIN_BACKUP_CONTAINER=""
PLUGIN_BACKUP=""
PLUGIN_STATE_CHANGED=false
ENV_BACKUP=""
ENV_CHANGED=false
CONFIG_BACKUP=""
CONFIG_CHANGED=false
TOOLSET_PROFILE_ARGS=(
  --worker-home "$HOME/.hermes/profiles/builder"
  --worker-home "$HOME/.hermes/profiles/reviewer"
  --worker-home "$HOME/.hermes/profiles/deep_checker"
  --worker-home "$HOME/.hermes/profiles/fix"
)

restore_forge_environment() {
  [ "$ENV_CHANGED" = true ] || return 0
  [ -n "$ENV_BACKUP" ] && [ -f "$ENV_BACKUP" ] || return 1
  HERMES_ENV_BACKUP="$ENV_BACKUP" "$HERMES_PY" - <<'PY'
import json
import os
from pathlib import Path

from dotenv import get_key
from hermes_cli.config import get_env_path
from hermes_cli.config import remove_env_value
from hermes_cli.config import save_env_value

backup = json.loads(
    Path(os.environ["HERMES_ENV_BACKUP"]).read_text(encoding="utf-8")
)
for key, previous in backup.items():
    if previous["present"]:
        save_env_value(key, previous["value"] or "")
    else:
        remove_env_value(key)
env_path = get_env_path()
for key, previous in backup.items():
    expected = (previous["value"] or "") if previous["present"] else None
    if get_key(str(env_path), key) != expected:
        raise RuntimeError("Infinity Forge runtime settings rollback failed")
PY
}

restore_hermes_toolsets() {
  [ "$CONFIG_CHANGED" = true ] || return 0
  [ -n "$CONFIG_BACKUP" ] && [ -d "$CONFIG_BACKUP" ] || return 1
  PYTHONPATH="$FORGE_RELEASE" "$HERMES_PY" -m forge.ops.hermes_toolsets \
    restore --backup "$CONFIG_BACKUP"
}

restore_plugin_state() {
  [ "$PLUGIN_STATE_CHANGED" = true ] || return 0
  if [ -L "$PLUGIN_LINK" ]; then
    if [ "$(readlink "$PLUGIN_LINK")" != "$PLUGIN_RELEASE" ]; then
      echo "[deploy] plugin link changed concurrently; refusing rollback overwrite" >&2
      return 1
    fi
    rm -f -- "$PLUGIN_LINK"
  elif [ -e "$PLUGIN_LINK" ]; then
    echo "[deploy] plugin path changed concurrently; refusing rollback overwrite" >&2
    return 1
  fi

  case "$PLUGIN_PREVIOUS_KIND" in
    symlink)
      PLUGIN_LINK_STAGE="$(mktemp -d "$PLUGIN_ROOT/.infinity-forge-rollback.XXXXXX")"
      ln -s -- "$PLUGIN_PREVIOUS_LINK" "$PLUGIN_LINK_STAGE/infinity-forge"
      mv -Tf "$PLUGIN_LINK_STAGE/infinity-forge" "$PLUGIN_LINK"
      rmdir -- "$PLUGIN_LINK_STAGE"
      PLUGIN_LINK_STAGE=""
      ;;
    directory)
      [ -d "$PLUGIN_BACKUP" ] || return 1
      mv -T "$PLUGIN_BACKUP" "$PLUGIN_LINK"
      rmdir -- "$PLUGIN_BACKUP_CONTAINER"
      PLUGIN_BACKUP_CONTAINER=""
      PLUGIN_BACKUP=""
      ;;
    missing) ;;
    *) return 1 ;;
  esac
  PLUGIN_STATE_CHANGED=false
}

PACKAGE_CHANGED=false
PREVIOUS_CHANGE_PACKAGE=""
PREVIOUS_CHANGE_INSTALLER=""
PREVIOUS_PACKAGE_RESTORED=false
DEPLOY_BACKUP="$(mktemp -d "$TASK_DATA_DIR/.subscription-deploy-backup.XXXXXX")"
BACKUP_DESTINATIONS=()
BACKUP_PATHS=()
PROFILE_LINK_DESTINATIONS=()
PROFILE_LINK_BACKUPS=()
backup_managed_path() {
  DESTINATION="$1"
  INDEX="${#BACKUP_DESTINATIONS[@]}"
  BACKUP="$DEPLOY_BACKUP/$INDEX"
  BACKUP_DESTINATIONS+=("$DESTINATION")
  if [ -e "$DESTINATION" ] || [ -L "$DESTINATION" ]; then
    cp -a -- "$DESTINATION" "$BACKUP"
    BACKUP_PATHS+=("$BACKUP")
  else
    BACKUP_PATHS+=("")
  fi
}

verify_candidate_change_package() {
  CANDIDATE_RELEASE="$1"
  CANDIDATE_PACKAGE="$2"
  (
    cd "$CANDIDATE_RELEASE"
    CANDIDATE_PACKAGE="$CANDIDATE_PACKAGE" HERMES_CHECKOUT="$HERMES_ROOT" \
      "$HERMES_PY" - <<'PY'
import os
from pathlib import Path

from forge.hermes_change.installer import _read_change_state
from forge.hermes_change.installer import _read_manifest
from forge.hermes_change.installer import _validate_all_package_files
from forge.hermes_change.installer import _verify_target_hashes

hermes_root = Path(os.environ["HERMES_CHECKOUT"]).resolve()
package = Path(os.environ["CANDIDATE_PACKAGE"]).resolve()
manifest = _read_manifest(package)
if _read_change_state(hermes_root, manifest) is not None:
    raise RuntimeError("installed Hermes package has an unfinished operation")
_validate_all_package_files(package, manifest)
_verify_target_hashes(hermes_root, manifest, "after_file_hash")
PY
  )
}

find_previous_change_package() {
  PREVIOUS_CHANGE_PACKAGE=""
  PREVIOUS_CHANGE_INSTALLER=""
  while IFS= read -r -d '' CANDIDATE; do
    [ "$CANDIDATE" != "$CHANGE_PACKAGE" ] || continue
    CANDIDATE_NAME="$(basename -- "$CANDIDATE")"
    [[ "$CANDIDATE_NAME" =~ ^[0-9a-f]{40}-${HERMES_SOURCE_VERSION}$ ]] || continue
    CANDIDATE_COMMIT="${CANDIDATE_NAME%%-*}"
    CANDIDATE_RELEASE="$FORGE_RELEASE_ROOT/$CANDIDATE_COMMIT"
    CANDIDATE_INSTALLER="$FORGE_RELEASE_ROOT/$CANDIDATE_COMMIT/forge/scripts/install-hermes-change.py"
    [ -f "$CANDIDATE_INSTALLER" ] && [ ! -L "$CANDIDATE_INSTALLER" ] || continue
    verify_candidate_change_package "$CANDIDATE_RELEASE" "$CANDIDATE" >/dev/null 2>&1 || continue
    if ! CANDIDATE_PACKAGE="$CANDIDATE" REQUESTED_PACKAGE="$CHANGE_PACKAGE" \
      HERMES_CHECKOUT="$HERMES_ROOT" \
      "$HERMES_PY" - <<'PY'
import hashlib
import json
import os
from pathlib import Path

candidate = json.loads(
    (Path(os.environ["CANDIDATE_PACKAGE"]) / "installed-files-list.json").read_text(
        encoding="utf-8"
    )
)
requested = json.loads(
    (Path(os.environ["REQUESTED_PACKAGE"]) / "installed-files-list.json").read_text(
        encoding="utf-8"
    )
)
candidate_before = {
    item["path"]: item["before_file_hash"] for item in candidate["files"]
}
requested_before = {
    item["path"]: item["before_file_hash"] for item in requested["files"]
}
common_paths = candidate_before.keys() & requested_before.keys()
if any(candidate_before[path] != requested_before[path] for path in common_paths):
    raise RuntimeError("installed Hermes package has a different source preimage")
hermes_checkout = Path(os.environ["HERMES_CHECKOUT"])
for path in requested_before.keys() - candidate_before.keys():
    expected_hash = requested_before[path]
    current_hash = hashlib.sha256((hermes_checkout / path).read_bytes()).hexdigest()
    if current_hash != expected_hash:
        raise RuntimeError("new Hermes package target is not at its source preimage")
PY
    then
      continue
    fi
    PREVIOUS_CHANGE_PACKAGE="$CANDIDATE"
    PREVIOUS_CHANGE_INSTALLER="$CANDIDATE_INSTALLER"
    return 0
  done < <(find "$CHANGE_PACKAGE_ROOT" -mindepth 1 -maxdepth 1 -type d -print0 | sort -z)
  return 1
}

cleanup_deploy_temporaries() {
  if [ -n "$PACKAGE_TEMP" ]; then
    case "$PACKAGE_TEMP" in
      "$CHANGE_PACKAGE_ROOT"/.build-*) rm -rf -- "$PACKAGE_TEMP" ;;
    esac
  fi
  if [ -n "$HERMES_SOURCE_TEMP" ]; then
    case "$HERMES_SOURCE_TEMP" in
      "$CHANGE_PACKAGE_ROOT"/.source-*) rm -rf -- "$HERMES_SOURCE_TEMP" ;;
    esac
  fi
  if [ -n "$RELEASE_TEMP" ]; then
    case "$RELEASE_TEMP" in
      "$FORGE_RELEASE_ROOT"/.build-*) rm -rf -- "$RELEASE_TEMP" ;;
    esac
  fi
  if [ -n "$PLUGIN_TEMP" ]; then
    case "$PLUGIN_TEMP" in
      "$PLUGIN_RELEASE_ROOT"/.build-*) rm -rf -- "$PLUGIN_TEMP" ;;
    esac
  fi
  if [ -n "$PLUGIN_LINK_STAGE" ]; then
    case "$PLUGIN_LINK_STAGE" in
      "$PLUGIN_ROOT"/.infinity-forge-*) rm -rf -- "$PLUGIN_LINK_STAGE" ;;
    esac
  fi
  if [ -n "$ENV_BACKUP" ] && [ "$ENV_CHANGED" = false ]; then
    case "$ENV_BACKUP" in
      "$TASK_DATA_DIR"/.env-backup.*) rm -f -- "$ENV_BACKUP" ;;
    esac
  fi
  if [ -n "$CONFIG_BACKUP" ] && [ "$CONFIG_CHANGED" = false ]; then
    case "$CONFIG_BACKUP" in
      "$TASK_DATA_DIR"/.config-backup.*) rm -rf -- "$CONFIG_BACKUP" ;;
    esac
  fi
}

restore_runtime_after_error() {
  STATUS=$?
  set +e
  if [ "$STATUS" -ne 0 ]; then
    # 새 코드를 실행할 수 있는 모든 managed process를 먼저 멈춘 뒤
    # plugin link와 release를 되돌린다.
    systemctl --user stop hermes-gateway >/dev/null 2>&1 || true
    for T in $MANAGED_TIMERS; do
      systemctl --user stop "forge-$T.timer" >/dev/null 2>&1 || true
      systemctl --user stop "forge-$T.service" >/dev/null 2>&1 || true
    done
    for ((I=${#PROFILE_LINK_DESTINATIONS[@]}-1; I>=0; I--)); do
      DST="${PROFILE_LINK_DESTINATIONS[$I]}"
      BACKUP="${PROFILE_LINK_BACKUPS[$I]}"
      rm -f -- "$DST"
      if [ -n "$BACKUP" ] && { [ -e "$BACKUP" ] || [ -L "$BACKUP" ]; }; then
        mv -- "$BACKUP" "$DST" || true
      fi
    done
    for ((I=${#BACKUP_DESTINATIONS[@]}-1; I>=0; I--)); do
      DST="${BACKUP_DESTINATIONS[$I]}"
      BACKUP="${BACKUP_PATHS[$I]}"
      rm -rf -- "$DST"
      if [ -n "$BACKUP" ] && { [ -e "$BACKUP" ] || [ -L "$BACKUP" ]; }; then
        mkdir -p -- "$(dirname "$DST")"
        cp -a -- "$BACKUP" "$DST" || true
      fi
    done
    PACKAGE_ROLLBACK_FAILED=false
    if [ "$PACKAGE_CHANGED" = true ] && [ -n "${CHANGE_PACKAGE:-}" ]; then
      "$HERMES_PY" "$REPO_DIR/forge/scripts/install-hermes-change.py" restore \
        --hermes-root "$HERMES_ROOT" --package "$CHANGE_PACKAGE" >/dev/null 2>&1 || PACKAGE_ROLLBACK_FAILED=true
    fi
    if [ "$PREVIOUS_PACKAGE_RESTORED" = true ] && [ -n "$PREVIOUS_CHANGE_PACKAGE" ]; then
      "$HERMES_PY" "$PREVIOUS_CHANGE_INSTALLER" install \
        --hermes-root "$HERMES_ROOT" --package "$PREVIOUS_CHANGE_PACKAGE" >/dev/null 2>&1 || PACKAGE_ROLLBACK_FAILED=true
    fi
    if [ "$PACKAGE_ROLLBACK_FAILED" = true ]; then
      echo "[deploy] WARNING: Hermes change package rollback needs manual review" >&2
    fi
    if ! restore_forge_environment; then
      echo "[deploy] WARNING: runtime settings rollback needs manual review: $ENV_BACKUP" >&2
    else
      ENV_CHANGED=false
    fi
    if ! restore_hermes_toolsets; then
      echo "[deploy] WARNING: Hermes tool visibility rollback needs manual review: $CONFIG_BACKUP" >&2
    else
      CONFIG_CHANGED=false
    fi
    if ! restore_plugin_state; then
      echo "[deploy] WARNING: plugin rollback needs manual review" >&2
    fi
    cleanup_deploy_temporaries
    for T in $MANAGED_TIMERS; do
      case " $ENABLED_TIMERS " in
        *" $T "*) systemctl --user enable "forge-$T.timer" >/dev/null 2>&1 || true ;;
        *) systemctl --user disable "forge-$T.timer" >/dev/null 2>&1 || true ;;
      esac
    done
    if [ "$GATEWAY_WAS_ACTIVE" = true ]; then
      systemctl --user start hermes-gateway >/dev/null 2>&1 || true
    fi
    for T in $ACTIVE_TIMERS; do
      systemctl --user start "forge-$T.timer" >/dev/null 2>&1 || true
    done
  else
    cleanup_deploy_temporaries
  fi
  rm -rf -- "$DEPLOY_BACKUP"
  return "$STATUS"
}
trap restore_runtime_after_error EXIT
for T in $MANAGED_TIMERS; do
  if systemctl --user is-active "forge-$T.timer" >/dev/null 2>&1; then
    ACTIVE_TIMERS="$ACTIVE_TIMERS $T"
  fi
  if systemctl --user is-enabled "forge-$T.timer" >/dev/null 2>&1; then
    ENABLED_TIMERS="$ENABLED_TIMERS $T"
  fi
  systemctl --user stop "forge-$T.timer" >/dev/null 2>&1 || true
  systemctl --user stop "forge-$T.service" >/dev/null 2>&1 || true
done
systemctl --user stop hermes-gateway >/dev/null 2>&1 || true

# OLD_PROFILE_MIGRATION_BEGIN: workers are stopped before the final old-Task check.
LEGACY_ACTIVE=$(HERMES_DB="$HERMES_DB" "$HERMES_PY" -c \
  "import os, sqlite3; db=os.environ['HERMES_DB']; connection=sqlite3.connect(f'file:{db}?mode=ro', uri=True); print(connection.execute(\"SELECT count(*) FROM tasks WHERE status NOT IN ('done','failed','cancelled') AND (coalesce(idempotency_key,'') LIKE 'github-issue:%' OR coalesce(idempotency_key,'') LIKE 'forge-stage:%' OR assignee IN ('executor','critic','issuefinder'))\").fetchone()[0])")
if [ "$LEGACY_ACTIVE" -ne 0 ]; then
  echo "[deploy] old Tasks are still active; profile change stopped" >&2
  exit 1
fi
# OLD_PROFILE_MIGRATION_END

echo "[deploy] Hermes user-turn chooser..."

# 커밋 스냅샷을 별도 release로 만든다. 실행 중인 CLI는 기존 모듈을
# 계속 사용하고, 새 CLI만 완성된 release를 보게 된다.
if git ls-tree -r "$DEPLOYED_COMMIT" | awk \
  '$1 == "120000" { found=1 } END { exit(found ? 0 : 1) }'; then
  echo "[deploy] managed release cannot contain symbolic links" >&2
  exit 1
fi
RELEASE_TEMP="$(mktemp -d "$FORGE_RELEASE_ROOT/.build-$DEPLOYED_COMMIT.XXXXXX")"
git archive "$DEPLOYED_COMMIT" | tar -x -C "$RELEASE_TEMP"
for REQUIRED_RELEASE_FILE in \
  forge/__init__.py \
  forge/ops/task_setup.py \
  forge/hermes_plugin/infinity_forge/__init__.py; do
  [ -f "$RELEASE_TEMP/$REQUIRED_RELEASE_FILE" ] || {
    echo "[deploy] managed release is incomplete" >&2
    exit 1
  }
done
if [ -d "$FORGE_RELEASE" ] && [ ! -L "$FORGE_RELEASE" ]; then
  if [ -n "$(find "$FORGE_RELEASE" -type l -print -quit)" ]; then
    echo "[deploy] existing managed release contains a symbolic link" >&2
    exit 1
  fi
  if ! diff -qr "$RELEASE_TEMP" "$FORGE_RELEASE" >/dev/null; then
    echo "[deploy] existing managed release does not match its Git commit" >&2
    exit 1
  fi
  rm -rf -- "$RELEASE_TEMP"
  RELEASE_TEMP=""
elif [ -e "$FORGE_RELEASE" ] || [ -L "$FORGE_RELEASE" ]; then
  echo "[deploy] managed release path is not a directory" >&2
  exit 1
else
  # Python import가 release 안에 __pycache__를 만들지 못하게 하여
  # 같은 SHA 재배포 때 전체 스냅샷 검증이 가능하게 한다.
  chmod -R a-w "$RELEASE_TEMP"
  mv -T "$RELEASE_TEMP" "$FORGE_RELEASE"
  RELEASE_TEMP=""
fi

HERMES_SOURCE_VERSION="$(git -C "$HERMES_ROOT" rev-parse HEAD)"
if ! printf '%s\n' "$HERMES_SOURCE_VERSION" | grep -Eq '^[0-9a-f]{40}$'; then
  echo "[deploy] Hermes source version is not a full Git commit ID" >&2
  exit 1
fi
mkdir -p "$CHANGE_PACKAGE_ROOT"
CHANGE_PACKAGE_VERSION="${FORGE_EXPECTED_COMMIT}-${HERMES_SOURCE_VERSION}"
CHANGE_PACKAGE="$CHANGE_PACKAGE_ROOT/$CHANGE_PACKAGE_VERSION"
if [ ! -f "$CHANGE_PACKAGE/installed-files-list.json" ]; then
  if [ -e "$CHANGE_PACKAGE" ]; then
    echo "[deploy] incomplete Hermes change package already exists" >&2
    exit 1
  fi
  HERMES_SOURCE_TEMP="$(mktemp -d "$CHANGE_PACKAGE_ROOT/.source-$CHANGE_PACKAGE_VERSION.XXXXXX")"
  git -C "$HERMES_ROOT" archive "$HERMES_SOURCE_VERSION" | tar -x -C "$HERMES_SOURCE_TEMP"
  PACKAGE_TEMP="$(mktemp -d "$CHANGE_PACKAGE_ROOT/.build-$CHANGE_PACKAGE_VERSION.XXXXXX")"
  "$HERMES_PY" "$REPO_DIR/forge/scripts/install-hermes-change.py" build \
    --hermes-root "$HERMES_SOURCE_TEMP" \
    --package "$PACKAGE_TEMP" \
    --source-version "$CHANGE_PACKAGE_VERSION"
  rm -rf -- "$HERMES_SOURCE_TEMP"
  HERMES_SOURCE_TEMP=""
  # 같은 파일시스템 안의 rename으로 완성된 패키지만 공개한다.
  mv -T "$PACKAGE_TEMP" "$CHANGE_PACKAGE"
  PACKAGE_TEMP=""
fi
EXPECTED_PACKAGE_VERSION="$CHANGE_PACKAGE_VERSION" CHANGE_PACKAGE="$CHANGE_PACKAGE" "$HERMES_PY" -c \
  'import json, os, pathlib; payload=json.loads((pathlib.Path(os.environ["CHANGE_PACKAGE"]) / "installed-files-list.json").read_text(encoding="utf-8")); assert payload["source_version"] == os.environ["EXPECTED_PACKAGE_VERSION"]'
if ! "$HERMES_PY" "$REPO_DIR/forge/scripts/install-hermes-change.py" verify \
  --hermes-root "$HERMES_ROOT" --package "$CHANGE_PACKAGE" >/dev/null 2>&1; then
  if find_previous_change_package; then
    echo "[deploy] restoring the installed Hermes change before upgrade"
    "$HERMES_PY" "$PREVIOUS_CHANGE_INSTALLER" restore \
      --hermes-root "$HERMES_ROOT" --package "$PREVIOUS_CHANGE_PACKAGE"
    PREVIOUS_PACKAGE_RESTORED=true
  fi
fi
if ! grep -Fq "INFINITY_FORGE_SUBSCRIPTION_WORKER_V1" "$HERMES_ROOT/hermes_cli/kanban_db.py"; then
  PACKAGE_CHANGED=true
fi
"$HERMES_PY" "$REPO_DIR/forge/scripts/install-hermes-change.py" install \
  --hermes-root "$HERMES_ROOT" \
  --package "$CHANGE_PACKAGE"
"$HERMES_PY" "$REPO_DIR/forge/scripts/install-hermes-change.py" verify \
  --hermes-root "$HERMES_ROOT" \
  --package "$CHANGE_PACKAGE"

# 플러그인 세 파일을 버전 디렉터리에 먼저 완성한 뒤 stable link만
# 교체한다. 기존 일반 디렉터리는 plugins 밖으로 옮겨 롤백용으로 보존한다.
PLUGIN_TEMP="$(mktemp -d "$PLUGIN_RELEASE_ROOT/.build-$DEPLOYED_COMMIT.XXXXXX")"
install -m 644 "$FORGE_RELEASE/forge/hermes_plugin/infinity_forge/plugin.yaml" "$PLUGIN_TEMP/plugin.yaml"
install -m 644 "$FORGE_RELEASE/forge/hermes_plugin/infinity_forge/__init__.py" "$PLUGIN_TEMP/__init__.py"
printf '%s\n' "$FORGE_RELEASE" > "$PLUGIN_TEMP/release-path.txt"
chmod 644 "$PLUGIN_TEMP/release-path.txt"
if [ -d "$PLUGIN_RELEASE" ] && [ ! -L "$PLUGIN_RELEASE" ]; then
  if [ -n "$(find "$PLUGIN_RELEASE" -type l -print -quit)" ]; then
    echo "[deploy] existing plugin release contains a symbolic link" >&2
    exit 1
  fi
  for PLUGIN_FILE in plugin.yaml __init__.py release-path.txt; do
    if ! cmp -s "$PLUGIN_TEMP/$PLUGIN_FILE" "$PLUGIN_RELEASE/$PLUGIN_FILE"; then
      echo "[deploy] existing plugin release does not match its Git commit" >&2
      exit 1
    fi
  done
  rm -rf -- "$PLUGIN_TEMP"
  PLUGIN_TEMP=""
elif [ -e "$PLUGIN_RELEASE" ] || [ -L "$PLUGIN_RELEASE" ]; then
  echo "[deploy] plugin release path is not a directory" >&2
  exit 1
else
  mv -T "$PLUGIN_TEMP" "$PLUGIN_RELEASE"
  PLUGIN_TEMP=""
fi

if [ -L "$PLUGIN_LINK" ]; then
  PLUGIN_PREVIOUS_KIND=symlink
  PLUGIN_PREVIOUS_LINK="$(readlink "$PLUGIN_LINK")"
elif [ -d "$PLUGIN_LINK" ] && [ ! -L "$PLUGIN_LINK" ]; then
  PLUGIN_PREVIOUS_KIND=directory
  PLUGIN_BACKUP_CONTAINER="$(mktemp -d "$PLUGIN_BACKUP_ROOT/migration-$DEPLOYED_COMMIT.XXXXXX")"
  PLUGIN_BACKUP="$PLUGIN_BACKUP_CONTAINER/infinity-forge"
  mv -T "$PLUGIN_LINK" "$PLUGIN_BACKUP"
  PLUGIN_STATE_CHANGED=true
elif [ -e "$PLUGIN_LINK" ]; then
  echo "[deploy] plugin path must be a directory or symbolic link" >&2
  exit 1
else
  PLUGIN_PREVIOUS_KIND=missing
fi

PLUGIN_LINK_STAGE="$(mktemp -d "$PLUGIN_ROOT/.infinity-forge-link.XXXXXX")"
ln -s -- "$PLUGIN_RELEASE" "$PLUGIN_LINK_STAGE/infinity-forge"
mv -Tf "$PLUGIN_LINK_STAGE/infinity-forge" "$PLUGIN_LINK"
PLUGIN_STATE_CHANGED=true
rmdir -- "$PLUGIN_LINK_STAGE"
PLUGIN_LINK_STAGE=""

# Hermes 공식 설정 API로 사용자 CLI가 시작할 때 필요한 세 값만 저장한다.
# 백업에도 이 세 값만 담고, 실패 시 missing/value 상태를 그대로 복원한다.
ENV_BACKUP="$(mktemp "$TASK_DATA_DIR/.env-backup.XXXXXX")"
chmod 600 "$ENV_BACKUP"
HERMES_ENV_BACKUP="$ENV_BACKUP" "$HERMES_PY" - <<'PY'
import json
import os
from pathlib import Path

from dotenv import get_key
from hermes_cli.config import get_env_path

keys = (
    "INFINITY_FORGE_REPOSITORY",
    "INFINITY_FORGE_TASK_SETTINGS_DB",
    "INFINITY_FORGE_GH_PATH",
)
env_path = get_env_path()
backup = {
    key: {
        "present": env_path.exists() and get_key(str(env_path), key) is not None,
        "value": get_key(str(env_path), key) if env_path.exists() else None,
    }
    for key in keys
}
Path(os.environ["HERMES_ENV_BACKUP"]).write_text(
    json.dumps(backup), encoding="utf-8"
)
PY
ENV_CHANGED=true
"$HERMES_PY" - "$REPOSITORY" "$TASK_SETTINGS_DB" "$GH_BIN" <<'PY'
import sys

from dotenv import get_key
from hermes_cli.config import get_env_path
from hermes_cli.config import save_env_value

keys = (
    "INFINITY_FORGE_REPOSITORY",
    "INFINITY_FORGE_TASK_SETTINGS_DB",
    "INFINITY_FORGE_GH_PATH",
)
expected = dict(zip(keys, sys.argv[1:], strict=True))
for key, value in expected.items():
    save_env_value(key, value)
env_path = get_env_path()
if any(get_key(str(env_path), key) != value for key, value in expected.items()):
    raise RuntimeError("Infinity Forge runtime settings were not saved")
PY

# Plugin enable also writes config.yaml. Snapshot the default profile before
# that first mutation; worker snapshots are appended after missing profiles exist.
CONFIG_BACKUP="$(mktemp -d "$TASK_DATA_DIR/.config-backup.XXXXXX")"
chmod 700 "$CONFIG_BACKUP"
PYTHONPATH="$FORGE_RELEASE" "$HERMES_PY" -m forge.ops.hermes_toolsets \
  backup --backup "$CONFIG_BACKUP" --main-home "$HOME/.hermes"
CONFIG_CHANGED=true
(cd "$HOME" && env -u PYTHONPATH -u PYTHONHOME "$HERMES_PY" -m hermes_cli.main plugins enable infinity-forge --no-allow-tool-override)
TASK_SETTINGS_DB="$TASK_SETTINGS_DB" PYTHONPATH="$FORGE_RELEASE" "$HERMES_PY" -c \
  "import os; from forge.ops.task_settings import TaskSettingsStore; from forge.ops.task_outbox import TaskOutbox, task_outbox_path; store=TaskSettingsStore(os.environ['TASK_SETTINGS_DB']); TaskOutbox(task_outbox_path(store.database_path))"

CHOOSER_EXPECTED_COMMIT="$DEPLOYED_COMMIT"
CHOOSER_HERMES_ROOT="$HERMES_ROOT"
CHOOSER_EXPECTED_REPOSITORY="$REPOSITORY"
CHOOSER_EXPECTED_TASK_SETTINGS_DB="$TASK_SETTINGS_DB"
CHOOSER_EXPECTED_GH_PATH="$GH_BIN"
# INFINITY_FORGE_CHOOSER_SMOKE_BEGIN
(
  CHOOSER_SMOKE_CWD="$(mktemp -d "${TMPDIR:-/tmp}/infinity-forge-chooser-smoke.XXXXXX")"
  chmod 700 "$CHOOSER_SMOKE_CWD"
  trap 'rmdir -- "$CHOOSER_SMOKE_CWD" 2>/dev/null || true' EXIT
  cd "$CHOOSER_SMOKE_CWD"
  env \
    -u PYTHONPATH \
    -u PYTHONHOME \
    -u PYTHONOPTIMIZE \
    -u INFINITY_FORGE_REPOSITORY \
    -u INFINITY_FORGE_TASK_SETTINGS_DB \
    -u INFINITY_FORGE_GH_PATH \
    HERMES_HOME="$HOME/.hermes" \
    PYTHONDONTWRITEBYTECODE=1 \
    CHOOSER_EXPECTED_COMMIT="$CHOOSER_EXPECTED_COMMIT" \
    CHOOSER_HERMES_ROOT="$CHOOSER_HERMES_ROOT" \
    CHOOSER_EXPECTED_REPOSITORY="$CHOOSER_EXPECTED_REPOSITORY" \
    CHOOSER_EXPECTED_TASK_SETTINGS_DB="$CHOOSER_EXPECTED_TASK_SETTINGS_DB" \
    CHOOSER_EXPECTED_GH_PATH="$CHOOSER_EXPECTED_GH_PATH" \
    "$HERMES_PY" - <<'PY'
import os
from pathlib import Path

from hermes_cli.env_loader import load_hermes_dotenv

hermes_root = Path(os.environ["HERMES_HOME"]).resolve()
hermes_project_root = Path(os.environ["CHOOSER_HERMES_ROOT"]).resolve()
expected_commit = os.environ["CHOOSER_EXPECTED_COMMIT"]
load_hermes_dotenv(project_env=hermes_project_root / ".env")
assert (
    os.environ["INFINITY_FORGE_REPOSITORY"]
    == os.environ["CHOOSER_EXPECTED_REPOSITORY"]
)
assert (
    os.environ["INFINITY_FORGE_TASK_SETTINGS_DB"]
    == os.environ["CHOOSER_EXPECTED_TASK_SETTINGS_DB"]
)
assert (
    os.environ["INFINITY_FORGE_GH_PATH"]
    == os.environ["CHOOSER_EXPECTED_GH_PATH"]
)

from hermes_cli.plugins import discover_plugins
from hermes_cli.plugins import get_plugin_manager
from hermes_cli.plugins import has_hook

discover_plugins(force=True)
manager = get_plugin_manager()
loaded = manager._plugins["infinity-forge"]
assert loaded.enabled is True
assert loaded.error is None
assert loaded.module is not None
assert loaded.manifest.path is not None
assert "pre_user_turn" in loaded.hooks_registered
assert has_hook("pre_user_turn")

module = loaded.module
plugin_path = Path(loaded.manifest.path).resolve()
expected_plugin_path = (hermes_root / "plugins" / "infinity-forge").resolve()
assert plugin_path == expected_plugin_path
module_file = getattr(module, "__file__", None)
assert module_file is not None
assert Path(module_file).resolve() == (plugin_path / "__init__.py").resolve()

managed_release = getattr(module, "_MANAGED_RELEASE", None)
assert managed_release is not None
expected_release = (
    hermes_root / "infinity-forge" / "releases" / expected_commit
).resolve()
assert Path(managed_release).resolve() == expected_release
assert expected_release.name == expected_commit


def forbid_task_service(_request):
    raise AssertionError("Task service must not run during chooser smoke")


module.set_task_service(forbid_task_service)
result = module.before_user_turn(
    session_id=f"chooser-smoke-{expected_commit}",
    user_id="deploy-verifier",
    surface="cli",
    text="diagnostic",
    is_new_session=True,
)
assert result["action"] == "handled"
assert [choice["id"] for choice in result["choices"]] == ["chat", "task"]
PY
)
# INFINITY_FORGE_CHOOSER_SMOKE_END

echo "[deploy] four Task profiles..."
profile() { "$HERMES_PY" -m hermes_cli.main profile "$@"; }
# OLD_PROFILE_MIGRATION_BEGIN: remove only the superseded profile IDs.
if [ -d "$HOME/.hermes/profiles/executor" ] && [ ! -d "$HOME/.hermes/profiles/builder" ]; then
  profile rename executor builder
fi
if [ -d "$HOME/.hermes/profiles/critic" ] && [ ! -d "$HOME/.hermes/profiles/deep_checker" ]; then
  profile rename critic deep_checker
fi
if [ ! -d "$HOME/.hermes/profiles/builder" ]; then
  profile create builder --clone-from reviewer --no-alias
fi
if [ ! -d "$HOME/.hermes/profiles/deep_checker" ]; then
  profile create deep_checker --clone-from reviewer --no-alias
fi
if [ ! -d "$HOME/.hermes/profiles/fix" ]; then
  profile create fix --clone-from builder --no-alias
fi
for OLD_PROFILE in executor critic issuefinder; do
  if [ -d "$HOME/.hermes/profiles/$OLD_PROFILE" ]; then
    profile delete "$OLD_PROFILE" --yes
  fi
done
# OLD_PROFILE_MIGRATION_END
for P in builder reviewer deep_checker fix; do
  mkdir -p "$HOME/.hermes/profiles/$P/skills" "$HOME/.hermes/profiles/$P/home/.config"
done

# Preserve every worker's post-provisioning baseline, then enforce and read
# back the one-way visibility boundary through Hermes' config API.
PYTHONPATH="$FORGE_RELEASE" "$HERMES_PY" -m forge.ops.hermes_toolsets \
  backup --backup "$CONFIG_BACKUP" "${TOOLSET_PROFILE_ARGS[@]}"
PYTHONPATH="$FORGE_RELEASE" "$HERMES_PY" -m forge.ops.hermes_toolsets \
  apply --main-home "$HOME/.hermes" "${TOOLSET_PROFILE_ARGS[@]}"
PYTHONPATH="$FORGE_RELEASE" "$HERMES_PY" -m forge.ops.hermes_toolsets \
  verify --main-home "$HOME/.hermes" "${TOOLSET_PROFILE_ARGS[@]}"

echo "[deploy] skills → Hermes profiles..."
# 공용 스킬: 게이트웨이(기본) + 네 작업 역할
for S in forge-ops memex code-design-principles forge-labels; do
  [ -d "forge/skills/$S" ] || continue
  cp -r "forge/skills/$S" ~/.hermes/skills/
  for P in builder reviewer deep_checker fix; do
    cp -r "forge/skills/$S" ~/.hermes/profiles/$P/skills/
  done
done
# 게이트웨이 전용 (사용자 대화 스타일·문서화)
for S in easy-answer code-problem-doc; do
  [ -d "forge/skills/$S" ] && cp -r "forge/skills/$S" ~/.hermes/skills/
done
# 구독 runner를 명시적으로 선택하는 두 스킬은 기본 gateway와 네 역할 모두에 둔다.
for S in codex claude-code; do
  backup_managed_path "$HOME/.hermes/skills/$S"
  cp -r "forge/skills/$S" "$HOME/.hermes/skills/"
  for P in builder reviewer deep_checker fix; do
    backup_managed_path "$HOME/.hermes/profiles/$P/skills/$S"
    cp -r "forge/skills/$S" "$HOME/.hermes/profiles/$P/skills/"
  done
done
# reviewer 추가 (문제 리포트 문서화)
[ -d forge/skills/code-problem-doc ] && cp -r forge/skills/code-problem-doc ~/.hermes/profiles/reviewer/skills/
# 역할 전용 스킬
[ -d forge/skills/build-task ]  && cp -r forge/skills/build-task  ~/.hermes/profiles/builder/skills/
[ -d forge/skills/review-task ] && cp -r forge/skills/review-task ~/.hermes/profiles/reviewer/skills/
[ -d forge/skills/deep-check ]  && cp -r forge/skills/deep-check  ~/.hermes/profiles/deep_checker/skills/
[ -d forge/skills/fix-task ]    && cp -r forge/skills/fix-task    ~/.hermes/profiles/fix/skills/

echo "[deploy] 프로필 home 인증 링크 보정 (codex·claude·gh·git)..."
# hermes 프로필은 자체 HOME(~/.hermes/profiles/<P>/home)으로 실행되어
# 실계정의 ~/.codex(코덱스 로그인)·~/.config/gh(gh 인증)·~/.gitconfig가 안 보인다 → symlink로 연결
for P in builder reviewer deep_checker fix; do
  PH=~/.hermes/profiles/$P/home
  mkdir -p "$PH/.config"
  # 주의: 대상이 이미 '실제 디렉토리'면 ln -sfn이 그 안에 링크를 만들어버린다 → 치우고 링크
  for PAIR in ".codex:$HOME/.codex" ".claude:$HOME/.claude" ".claude.json:$HOME/.claude.json" ".config/gh:$HOME/.config/gh" ".gitconfig:$HOME/.gitconfig"; do
    DST="$PH/${PAIR%%:*}"; SRC="${PAIR#*:}"
    BACKUP=""
    if [ -e "$DST" ] || [ -L "$DST" ]; then
      BACKUP="$DST.bak.$(date -u +%Y%m%dT%H%M%SZ).$$"
      # RISK(security): preserve the exact credential item before replacing it with a link.
      mv -- "$DST" "$BACKUP"
    fi
    PROFILE_LINK_DESTINATIONS+=("$DST")
    PROFILE_LINK_BACKUPS+=("$BACKUP")
    ln -s -- "$SRC" "$DST"
  done
done

mkdir -p "$(dirname "$STABLE_RUNNER")"
backup_managed_path "$STABLE_RUNNER"
install -m 755 "$REPO_DIR/forge/scripts/subscription-runner.py" "$STABLE_RUNNER"

echo "[deploy] hooks·scripts → ~/forge..."
mkdir -p ~/forge/hooks
[ -f forge/hooks/codex-work-check.sh ] && install -m 755 forge/hooks/codex-work-check.sh ~/forge/hooks/
[ -f forge/scripts/nightly-backup.sh ] && install -m 755 forge/scripts/nightly-backup.sh ~/backups/
for S in activity-log-writer.py send-pending-messages.py system-check.sh state-mismatch-check.sh spec-coverage.sh morning-report.sh; do
  [ -f "forge/scripts/$S" ] && install -m 755 "forge/scripts/$S" ~/forge/
done

echo "[deploy] systemd 타이머 설치..."
UD=~/.config/systemd/user
mkdir -p "$UD"
systemctl --user disable --now forge-messages.timer >/dev/null 2>&1 || true
rm -f "$UD/forge-messages.service" "$UD/forge-messages.timer"
[ -x /usr/bin/flock ] || { echo "[deploy] /usr/bin/flock 누락" >&2; exit 1; }
PROCESS_LOCK="/usr/bin/flock --nonblock --conflict-exit-code 0 %t/forge-pipeline.lock"
mkunit() { # $1=기존 unit ID $2=ExecStart $3=일정 $4=쉬운 설명 $5=추가 Environment
  cat > "$UD/forge-$1.service" << UNIT
[Unit]
Description=INFINITY_FORGE $4
[Service]
Type=oneshot
Environment=PATH=$HOME/.hermes/hermes-agent/venv/bin:$HOME/.hermes/hermes-agent/node_modules/.bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONPATH=$REPO_DIR
${5:-}
WorkingDirectory=$REPO_DIR
ExecStart=$2
UNIT
  cat > "$UD/forge-$1.timer" << UNIT
[Unit]
Description=INFINITY_FORGE $1 timer
[Timer]
$3
AccuracySec=1s
Persistent=true
[Install]
WantedBy=timers.target
UNIT
}
# RISK(race): 기존 unit ID를 유지하고 모든 외부 writer를 같은 process lock으로
# 직렬화해 이전·새 unit의 중복 실행과 SQLite/GitHub write 경쟁을 막는다.
mkunit ledger  "$PROCESS_LOCK $HERMES_PY $REPO_DIR/forge/scripts/activity-log-writer.py"    "OnCalendar=*:0/10"                    "Activity Log Writer"
mkunit stage  "$PROCESS_LOCK $HERMES_PY $REPO_DIR/forge/scripts/task-flow-worker.py --db $HERMES_DB --hermes $HERMES_BIN --gh $GH_BIN --settings-db $TASK_SETTINGS_DB --outbox $CONFIRMED_TASKS_DB --repo $REPOSITORY --workspace dir:$REPO_DIR" "OnCalendar=*-*-* *:*:00" "Task Flow Worker"
mkunit mirror  "$PROCESS_LOCK $HERMES_PY $REPO_DIR/forge/scripts/issue-status-sync.py --db $HERMES_DB --gh $GH_BIN --settings-db $TASK_SETTINGS_DB --outbox $CONFIRMED_TASKS_DB --repo $REPOSITORY" "OnCalendar=*-*-* *:*:30" "Issue Status Sync"
mkunit canary  "/bin/bash $HOME/forge/system-check.sh"                                          "OnCalendar=*-*-* 00/6:00:00 Asia/Seoul" "System Check"
mkunit drift   "/bin/bash $HOME/forge/state-mismatch-check.sh"                                  "OnCalendar=hourly"                    "State Mismatch Check"
mkunit morning "/bin/bash $HOME/forge/morning-report.sh"                                        "OnCalendar=*-*-* 07:30:00 Asia/Seoul" "Morning Report"
mkunit merge  "$PROCESS_LOCK $HERMES_PY $REPO_DIR/forge/scripts/merge-worker.py --settings-db $TASK_SETTINGS_DB --outbox $CONFIRMED_TASKS_DB --hermes-db $HERMES_DB --gh $GH_BIN --repo $REPOSITORY --required-check eval --hermes $HERMES_BIN --workspace dir:$REPO_DIR" "OnCalendar=*-*-* *:*:15" "Merge Worker" "Environment=AUTO_MERGE_ENABLED=false"
mkunit flush  "$PROCESS_LOCK $HERMES_PY $REPO_DIR/forge/scripts/send-pending-messages.py"   "OnCalendar=*-*-* *:*:45"             "Send Pending Messages"
systemctl --user daemon-reload
for T in ledger stage mirror canary drift morning merge flush; do
  systemctl --user enable "forge-$T.timer" > /dev/null
done

echo "[deploy] remove replaced files and skills..."
# OLD_INSTALLATION_CLEANUP_BEGIN: fixed allowlist, safe to repeat.
rm -f \
  "$HOME/forge/canary.sh" \
  "$HOME/forge/drift-audit.sh" \
  "$HOME/forge/ledger-emit.py" \
  "$HOME/forge/flush-outbox.py" \
  "$HOME/forge/label-mirror.py" \
  "$HOME/forge/hooks/codex-stop-gate.sh"
for P in "$HOME/.hermes" \
  "$HOME/.hermes/profiles/builder" \
  "$HOME/.hermes/profiles/reviewer" \
  "$HOME/.hermes/profiles/deep_checker" \
  "$HOME/.hermes/profiles/fix"; do
  rm -rf \
    "$P/skills/kanban-codex-delegate" \
    "$P/skills/reviewer-verdict" \
    "$P/skills/critic-adversarial" \
    "$P/skills/issue-finder-sot"
done
# OLD_INSTALLATION_CLEANUP_END

echo "[deploy] 게이트웨이 스킬 리로드..."
DROP_IN="$HOME/.config/systemd/user/hermes-gateway.service.d"
mkdir -p "$DROP_IN"
backup_managed_path "$DROP_IN/infinity-forge.conf"
cat > "$DROP_IN/infinity-forge.conf" << UNIT
[Service]
Environment=PYTHONPATH=$REPO_DIR
Environment=INFINITY_FORGE_REPOSITORY=$REPOSITORY
Environment=INFINITY_FORGE_TASK_SETTINGS_DB=$TASK_SETTINGS_DB
Environment=INFINITY_FORGE_GH_PATH=$GH_BIN
Environment="INFINITY_FORGE_SUBSCRIPTION_ROUTING=1"
Environment="INFINITY_FORGE_SUBSCRIPTION_PYTHON=$HERMES_PY"
Environment="INFINITY_FORGE_SUBSCRIPTION_RUNNER=$STABLE_RUNNER"
Environment="INFINITY_FORGE_CLAUDE_BIN=$CLAUDE_BIN"
Environment="INFINITY_FORGE_CLAUDE_MCP_CONFIG=$CLAUDE_MCP_CONFIG"
Environment="INFINITY_FORGE_REPO=$REPO_DIR"
UNIT

backup_managed_path "$HOME/.hermes/config.yaml"
backup_managed_path "$HOME/.codex/config.toml"
backup_managed_path "$TASK_DATA_DIR/subscription-runtime"
"$HERMES_PY" "$CONFIGURE_SCRIPT" apply --forge-root "$REPO_DIR" \
  --hermes-root "$HOME/.hermes" --claude-bin "$CLAUDE_BIN" \
  --codex-bin "$CODEX_BIN"
"$HERMES_PY" "$CONFIGURE_SCRIPT" verify --forge-root "$REPO_DIR" \
  --hermes-root "$HOME/.hermes" --claude-bin "$CLAUDE_BIN" \
  --codex-bin "$CODEX_BIN"
systemctl --user daemon-reload
cleanup_deploy_temporaries
systemctl --user restart hermes-gateway
for T in ledger stage mirror canary drift morning merge flush; do
  systemctl --user start "forge-$T.timer" > /dev/null
done
case "$ENV_BACKUP" in
  "$TASK_DATA_DIR"/.env-backup.*) rm -f -- "$ENV_BACKUP" ;;
  *) echo "[deploy] runtime settings backup path is invalid" >&2; exit 1 ;;
esac
ENV_BACKUP=""
ENV_CHANGED=false
CONFIG_CHANGED=false
case "$CONFIG_BACKUP" in
  "$TASK_DATA_DIR"/.config-backup.*) rm -rf -- "$CONFIG_BACKUP" ;;
  *) echo "[deploy] Hermes config backup path is invalid" >&2; exit 1 ;;
esac
CONFIG_BACKUP=""
systemctl --user is-active --quiet hermes-gateway
GATEWAY_WAS_ACTIVE=false
trap - EXIT
rm -rf -- "$DEPLOY_BACKUP"
echo "[deploy] done: $(git rev-parse --short HEAD)"
}
main "$@"
