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

CLAUDE_VERSION="2.1.212"
CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
CLAUDE_ACTUAL_VERSION=""
if [ -n "$CLAUDE_BIN" ]; then
  CLAUDE_ACTUAL_VERSION="$("$CLAUDE_BIN" --version 2>/dev/null | awk 'NR == 1 { print $1 }')"
fi
if [ "$CLAUDE_ACTUAL_VERSION" != "$CLAUDE_VERSION" ]; then
  # RISK(security): execute only the pinned official native installer before any service/config mutation.
  curl -fsSL https://claude.ai/install.sh | bash -s 2.1.212
  hash -r
  CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
  [ -n "$CLAUDE_BIN" ] || { echo "[deploy] Claude Code installation failed" >&2; exit 78; }
  CLAUDE_ACTUAL_VERSION="$("$CLAUDE_BIN" --version 2>/dev/null | awk 'NR == 1 { print $1 }')"
  [ "$CLAUDE_ACTUAL_VERSION" = "$CLAUDE_VERSION" ] || {
    echo "[deploy] Claude Code 2.1.212 is required" >&2
    exit 78
  }
fi

# RISK(security): keep the JSON private and validate only the four subscription fields.
CLAUDE_AUTH_JSON="$("$CLAUDE_BIN" auth status --json 2>/dev/null)" || {
  echo "[deploy] Claude Max login required; run: claude auth login" >&2
  exit 78
}
if ! printf '%s' "$CLAUDE_AUTH_JSON" | "$HERMES_PY" -c '
import json, sys
try:
    payload = json.load(sys.stdin)
except (json.JSONDecodeError, UnicodeError):
    raise SystemExit(1)
valid = (
    isinstance(payload, dict)
    and payload.get("loggedIn") is True
    and payload.get("authMethod") == "claude.ai"
    and payload.get("apiProvider") == "firstParty"
    and payload.get("subscriptionType") == "max"
)
raise SystemExit(0 if valid else 1)
'; then
  unset CLAUDE_AUTH_JSON
  echo "[deploy] Claude Max login required; run: claude auth login" >&2
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
PACKAGE_CHANGED=false
CONFIGURE_APPLIED=false
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
restore_runtime_after_error() {
  STATUS=$?
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
  if [ "$STATUS" -ne 0 ]; then
    if [ "$CONFIGURE_APPLIED" = true ]; then
      "$HERMES_PY" "$CONFIGURE_SCRIPT" rollback --hermes-root "$HOME/.hermes" >/dev/null 2>&1 || true
    fi
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
    if [ "$PACKAGE_CHANGED" = true ] && [ -n "${CHANGE_PACKAGE:-}" ]; then
      "$HERMES_PY" "$REPO_DIR/forge/scripts/install-hermes-change.py" restore \
        --hermes-root "$HERMES_ROOT" --package "$CHANGE_PACKAGE" >/dev/null 2>&1 || true
    fi
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
if ! grep -Fq "INFINITY_FORGE_SUBSCRIPTION_WORKER_V1" "$HERMES_ROOT/hermes_cli/kanban_db.py"; then
  PACKAGE_CHANGED=true
fi
"$HERMES_PY" "$REPO_DIR/forge/scripts/install-hermes-change.py" install \
  --hermes-root "$HERMES_ROOT" \
  --package "$CHANGE_PACKAGE"
PLUGIN_DIR="$HOME/.hermes/plugins/infinity-forge"
mkdir -p "$PLUGIN_DIR"
install -m 644 forge/hermes_plugin/infinity_forge/plugin.yaml "$PLUGIN_DIR/plugin.yaml"
install -m 644 forge/hermes_plugin/infinity_forge/__init__.py "$PLUGIN_DIR/__init__.py"
PYTHONPATH="$REPO_DIR" "$HERMES_PY" -m hermes_cli.main plugins enable infinity-forge
TASK_SETTINGS_DB="$TASK_SETTINGS_DB" PYTHONPATH="$REPO_DIR" "$HERMES_PY" -c \
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
for T in ledger stage mirror canary drift morning merge flush; do systemctl --user enable --now "forge-$T.timer" > /dev/null; done

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

CONFIGURE_APPLIED=true
"$HERMES_PY" "$CONFIGURE_SCRIPT" apply --forge-root "$REPO_DIR" --hermes-root "$HOME/.hermes"
"$HERMES_PY" "$CONFIGURE_SCRIPT" verify --forge-root "$REPO_DIR" --hermes-root "$HOME/.hermes"
systemctl --user daemon-reload
"$HERMES_BIN" gateway restart
systemctl --user is-active --quiet hermes-gateway
GATEWAY_WAS_ACTIVE=false
trap - EXIT
rm -rf -- "$DEPLOY_BACKUP"
echo "[deploy] done: $(git rev-parse --short HEAD)"
}
main "$@"
