#!/bin/bash
# INFINITY_FORGE system check — known inputs verify the work check (LLM 0).
set -u
FAILURES=""

slack() {
  TOKEN=$(grep '^SLACK_BOT_TOKEN=' ~/.hermes/.env 2>/dev/null | cut -d= -f2)
  [ -n "$TOKEN" ] && curl -s -m 10 -X POST https://slack.com/api/chat.postMessage \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"channel\":\"#forge-cloud\",\"text\":\"$1\"}" > /dev/null
}

# Empty changes must stop.
WORKDIR=$(mktemp -d)
(cd "$WORKDIR" && git init -q)
if ~/forge/hooks/codex-work-check.sh "$WORKDIR" 2>/dev/null; then
  FAILURES="$FAILURES [work check accepted empty changes]"
fi

# A normal changed file must pass when no repository test command is present.
echo system-check > "$WORKDIR/file.txt"
if ! ~/forge/hooks/codex-work-check.sh "$WORKDIR" 2>/dev/null; then
  FAILURES="$FAILURES [work check rejected a normal changed file]"
fi

# A failing test command must stop.
if FORGE_TEST_COMMAND=false ~/forge/hooks/codex-work-check.sh "$WORKDIR" 2>/dev/null; then
  FAILURES="$FAILURES [work check accepted a failing test command]"
fi
rm -rf "$WORKDIR"

# Committed work in a clean worktree must be found from the recorded start commit.
WORKDIR=$(mktemp -d)
(cd "$WORKDIR" && git init -q \
  && echo seed > seed.txt && git add . \
  && git -c user.email=system-check@forge -c user.name=system-check commit -qm seed \
  && git rev-parse HEAD > .forge-start-commit \
  && echo work > file.txt && git add file.txt \
  && git -c user.email=system-check@forge -c user.name=system-check commit -qm work)
if ! ~/forge/hooks/codex-work-check.sh "$WORKDIR" 2>/dev/null; then
  FAILURES="$FAILURES [work check missed committed changes]"
fi
rm -rf "$WORKDIR"

systemctl --user is-active hermes-gateway > /dev/null \
  || FAILURES="$FAILURES [Hermes Gateway is down]"
PATH="$HOME/.hermes/hermes-agent/venv/bin:$HOME/.local/bin:$PATH" codex login status 2>&1 | grep -q "Logged in" \
  || FAILURES="$FAILURES [Codex is not logged in]"

if [ -n "$FAILURES" ]; then
  slack "🚨 [INFINITY_FORGE] system check failed:$FAILURES"
  echo "SYSTEM_CHECK_FAILED:$FAILURES" >&2
  exit 2
fi

slack "✅ [INFINITY_FORGE] system check passed"
echo "SYSTEM_CHECK_OK"
