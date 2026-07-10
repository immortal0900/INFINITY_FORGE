#!/bin/bash
# INFINITY_FORGE spec-coverage — 스펙 레지스트리 ↔ GitHub 이슈 대조 (LLM 0)
# 출력: "커버리지 N/M (미대응: ...)" 한 줄. canary·morning-report가 호출.
# 레지스트리: 레포의 forge/spec-registry.md — "SPEC-NNN | 제목" 형식 행
set -u
REG=~/work/INFINITY_FORGE/forge/spec-registry.md
[ -f "$REG" ] || { echo "커버리지 n/a (레지스트리 없음)"; exit 0; }
SPECS=$(grep -oE '^SPEC-[0-9]+' "$REG" | sort -u)
M=$(echo "$SPECS" | grep -c . || true)
[ "$M" -eq 0 ] && { echo "커버리지 0/0"; exit 0; }
ISSUES=$(/usr/bin/gh api "repos/immortal0900/INFINITY_FORGE/issues?state=all&per_page=100" \
  --jq '.[] | select(has("pull_request") | not) | "\(.title)|\(.state)"' 2>/dev/null)
N=0; MISSING=""
for S in $SPECS; do
  LINE=$(echo "$ISSUES" | grep -F "[$S]" | head -1)
  if [ -n "$LINE" ] && echo "$LINE" | grep -q "|closed"; then
    N=$((N+1))
  elif [ -z "$LINE" ]; then
    MISSING="$MISSING $S(이슈없음)"
  else
    MISSING="$MISSING $S(진행중)"
  fi
done
OUT="커버리지 $N/$M"
[ -n "$MISSING" ] && OUT="$OUT (미완:$MISSING)"
echo "$OUT"
