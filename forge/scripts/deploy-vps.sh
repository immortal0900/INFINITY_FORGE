#!/bin/bash
# INFINITY_FORGE — 서버의 검증된 main 커밋과 Hermes 자산을 반영합니다.
# 실행 위치: 서버의 INFINITY_FORGE clone. 로컬 변경은 운영자가 먼저 보관해야 합니다.
set -euo pipefail
# main 함수로 전체를 감싸서 fast-forward가 이 파일을 갱신해도 현재 실행을 안전하게 끝낸다.
main() {
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

REPOSITORY="${INFINITY_FORGE_REPOSITORY:-$($GH_BIN repo view --json nameWithOwner --jq .nameWithOwner)}"
TASK_DATA_DIR="${INFINITY_FORGE_TASK_DATA_DIR:-$HOME/.hermes/infinity-forge}"
TASK_SETTINGS_DB="$TASK_DATA_DIR/task-settings.db"
CONFIRMED_TASKS_DB="$TASK_SETTINGS_DB.task-outbox.db"
HERMES_DB="$HOME/.hermes/kanban.db"
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
RELEASE_CREATED=false
PLUGIN_TEMP=""
PLUGIN_RELEASE_CREATED=false
PLUGIN_LINK_STAGE=""
PLUGIN_PREVIOUS_KIND=""
PLUGIN_PREVIOUS_LINK=""
PLUGIN_BACKUP_CONTAINER=""
PLUGIN_BACKUP=""
PLUGIN_STATE_CHANGED=false
ENV_BACKUP=""
ENV_CHANGED=false

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
}

restore_runtime_after_error() {
  STATUS=$?
  set +e
  if [ "$STATUS" -ne 0 ]; then
    if ! restore_forge_environment; then
      echo "[deploy] WARNING: runtime settings rollback needs manual review: $ENV_BACKUP" >&2
    else
      ENV_CHANGED=false
    fi
    PLUGIN_ROLLBACK_OK=false
    if ! restore_plugin_state; then
      echo "[deploy] WARNING: plugin rollback needs manual review" >&2
    else
      PLUGIN_ROLLBACK_OK=true
    fi
    if [ "$PLUGIN_ROLLBACK_OK" = true ] && [ "$PLUGIN_RELEASE_CREATED" = true ] && [ -d "$PLUGIN_RELEASE" ]; then
      case "$PLUGIN_RELEASE" in
        "$PLUGIN_RELEASE_ROOT/$DEPLOYED_COMMIT") rm -rf -- "$PLUGIN_RELEASE" ;;
      esac
    fi
    if [ "$PLUGIN_ROLLBACK_OK" = true ] && [ "$RELEASE_CREATED" = true ] && [ -d "$FORGE_RELEASE" ]; then
      case "$FORGE_RELEASE" in
        "$FORGE_RELEASE_ROOT/$DEPLOYED_COMMIT")
          chmod -R u+w "$FORGE_RELEASE"
          rm -rf -- "$FORGE_RELEASE"
          ;;
      esac
    fi
    cleanup_deploy_temporaries
    systemctl --user stop hermes-gateway >/dev/null 2>&1 || true
    for T in $MANAGED_TIMERS; do
      systemctl --user stop "forge-$T.timer" >/dev/null 2>&1 || true
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
  RELEASE_CREATED=true
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
"$HERMES_PY" "$REPO_DIR/forge/scripts/install-hermes-change.py" install \
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
  PLUGIN_RELEASE_CREATED=true
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

(cd "$HOME" && env -u PYTHONPATH -u PYTHONHOME "$HERMES_PY" -m hermes_cli.main plugins enable infinity-forge --no-allow-tool-override)
TASK_SETTINGS_DB="$TASK_SETTINGS_DB" PYTHONPATH="$FORGE_RELEASE" "$HERMES_PY" -c \
  "import os; from forge.ops.task_settings import TaskSettingsStore; from forge.ops.task_outbox import TaskOutbox, task_outbox_path; store=TaskSettingsStore(os.environ['TASK_SETTINGS_DB']); TaskOutbox(task_outbox_path(store.database_path))"

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
# reviewer 추가 (문제 리포트 문서화)
[ -d forge/skills/code-problem-doc ] && cp -r forge/skills/code-problem-doc ~/.hermes/profiles/reviewer/skills/
# 역할 전용 스킬
[ -d forge/skills/build-task ]  && cp -r forge/skills/build-task  ~/.hermes/profiles/builder/skills/
[ -d forge/skills/review-task ] && cp -r forge/skills/review-task ~/.hermes/profiles/reviewer/skills/
[ -d forge/skills/deep-check ]  && cp -r forge/skills/deep-check  ~/.hermes/profiles/deep_checker/skills/
[ -d forge/skills/fix-task ]    && cp -r forge/skills/fix-task    ~/.hermes/profiles/fix/skills/

echo "[deploy] 프로필 home 인증 링크 보정 (codex·gh·git)..."
# hermes 프로필은 자체 HOME(~/.hermes/profiles/<P>/home)으로 실행되어
# 실계정의 ~/.codex(코덱스 로그인)·~/.config/gh(gh 인증)·~/.gitconfig가 안 보인다 → symlink로 연결
for P in builder reviewer deep_checker fix; do
  PH=~/.hermes/profiles/$P/home
  mkdir -p "$PH/.config"
  # 주의: 대상이 이미 '실제 디렉토리'면 ln -sfn이 그 안에 링크를 만들어버린다 → 치우고 링크
  for PAIR in ".codex:$HOME/.codex" ".config/gh:$HOME/.config/gh" ".gitconfig:$HOME/.gitconfig"; do
    DST="$PH/${PAIR%%:*}"; SRC="${PAIR#*:}"
    if [ -e "$DST" ] && [ ! -L "$DST" ]; then mv "$DST" "$DST.bak.$(date +%s)"; fi
    ln -sfn "$SRC" "$DST"
  done
done

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
cat > "$DROP_IN/infinity-forge.conf" << UNIT
[Service]
Environment=PYTHONPATH=$REPO_DIR
Environment=INFINITY_FORGE_REPOSITORY=$REPOSITORY
Environment=INFINITY_FORGE_TASK_SETTINGS_DB=$TASK_SETTINGS_DB
Environment=INFINITY_FORGE_GH_PATH=$GH_BIN
UNIT
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
GATEWAY_WAS_ACTIVE=false
trap - EXIT
echo "[deploy] done: $(git rev-parse --short HEAD)"
}
main "$@"
