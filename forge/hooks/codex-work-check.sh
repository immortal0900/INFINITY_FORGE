#!/bin/bash
# INFINITY_FORGE Codex work check
# Checks that real files changed and the repository test command passes.
set -u
trap 'echo "CHECK_ERROR: work check crashed at line $LINENO" >&2; exit 2' ERR

WORKDIR="${1:-.}"
cd "$WORKDIR" || {
  echo "CHECK_ERROR: work directory not found: $WORKDIR" >&2
  exit 2
}

START_COMMIT="${FORGE_START_COMMIT:-}"
if [ -z "$START_COMMIT" ] && [ -f .forge-start-commit ]; then
  START_COMMIT=$(tr -d '[:space:]' < .forge-start-commit)
fi

git rev-parse --git-dir >/dev/null 2>&1 || {
  echo "CHECK_ERROR: not a git repository: $WORKDIR" >&2
  exit 2
}

CHANGED=$({
  git diff --name-only
  git diff --cached --name-only
  git ls-files --others --exclude-standard
} 2>/dev/null | sort -u)

if [ -n "$START_COMMIT" ]; then
  if git rev-parse --verify -q "${START_COMMIT}^{commit}" >/dev/null 2>&1; then
    COMMITTED=$(git diff --name-only "$START_COMMIT" HEAD 2>/dev/null || true)
    CHANGED=$(printf '%s\n%s\n' "$CHANGED" "$COMMITTED" | sort -u)
  else
    echo "CHECK_ERROR: FORGE_START_COMMIT is not a commit in this repository: $START_COMMIT" >&2
    exit 2
  fi
fi

REAL_CHANGES=$(printf '%s\n' "$CHANGED" | grep -v -e '^$' -e '^\.forge-start-commit$' || true)
if [ -z "$REAL_CHANGES" ]; then
  echo "TESTS_FAILED: no implementation files changed (start_commit=${START_COMMIT:-none})" >&2
  exit 2
fi

PYTHON="${FORGE_PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if [ -x "$HOME/.hermes/hermes-agent/venv/bin/python" ]; then
    PYTHON="$HOME/.hermes/hermes-agent/venv/bin/python"
  else
    PYTHON="python3"
  fi
fi

TEST_COMMAND="${FORGE_TEST_COMMAND:-}"
if [ -z "$TEST_COMMAND" ]; then
  if [ -f package.json ] && grep -q '"test"' package.json; then
    TEST_COMMAND="npm test --silent"
  elif [ -f pyproject.toml ] || [ -f pytest.ini ] || [ -d tests ]; then
    TEST_COMMAND="$PYTHON -m pytest tests/ -q"
  elif [ -f go.mod ]; then
    TEST_COMMAND="go test ./..."
  fi
fi

if [ -n "$TEST_COMMAND" ]; then
  TEST_OUTPUT=$(mktemp)
  if ! bash -c "$TEST_COMMAND" >"$TEST_OUTPUT" 2>&1; then
    echo "TESTS_FAILED: test command failed ($TEST_COMMAND) — tail:" >&2
    tail -5 "$TEST_OUTPUT" >&2
    rm -f "$TEST_OUTPUT"
    exit 2
  fi
  rm -f "$TEST_OUTPUT"
fi

exit 0
