#!/usr/bin/env python3
"""INFINITY_FORGE label-mirror — GitHub 이슈 ↔ kanban 카드 동기화 (LLM 0, 2분 주기).
D7: forge:* 라벨의 단독 작성자는 이 스크립트다.

수입(Import): forge:need-execution 라벨이 달린 open 이슈 중 카드가 없는 것
  → executor 카드 생성 (멱등키 github-issue:OWNER/REPO#N).
  ※ 사람이 라벨을 다는 것이 투입 행위다. 라벨 없는 이슈는 건드리지 않는다(암묵 자동 투입 방지).
투영(Project): 멱등키 카드의 상태 → 이슈의 forge:* 라벨 교체.
"""
import json, os, sqlite3, subprocess, sys

REPOS = ["immortal0900/INFINITY_FORGE"]
HOME = os.path.expanduser("~")
DB = os.path.join(HOME, ".hermes", "kanban.db")
HERMES = os.path.join(HOME, ".local", "bin", "hermes")
GH = "/usr/bin/gh"

STATUS_TO_LABEL = {
    "triage": "forge:spec-draft",
    "todo": "forge:need-execution",
    "ready": "forge:need-execution",
    "running": "forge:in-progress",
    "blocked": "forge:blocked",
    "done": "forge:need-review",   # PR 리뷰·머지는 사람(P1) — merge 후 이슈 close는 사람/후속
    "failed": "forge:failed",
}
ALL_LABELS = ["forge:spec-draft", "forge:adr", "forge:need-execution", "forge:in-progress",
              "forge:need-review", "forge:need-critic", "forge:mergeable", "forge:blocked", "forge:failed"]

def sh(args, timeout=30):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()

def cards_by_key():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = con.execute("SELECT idempotency_key, status FROM tasks WHERE idempotency_key LIKE 'github-issue:%'").fetchall()
    con.close()
    return dict(rows)

def main():
    keys = cards_by_key()
    for repo in REPOS:
        # ── 수입 ──
        rc, out, err = sh([GH, "api", f"repos/{repo}/issues?state=open&labels=forge:need-execution&per_page=50"])
        if rc != 0:
            print(f"GATE_ERROR: gh api failed for {repo}: {err[:120]}", file=sys.stderr); sys.exit(2)
        for issue in json.loads(out or "[]"):
            if "pull_request" in issue: continue
            n = issue["number"]
            key = f"github-issue:{repo}#{n}"
            if key in keys: continue
            body = (f"GitHub 이슈: {issue['html_url']}\n\n"
                    "AC의 원본(SoT)은 위 이슈 본문이다 — 재해석 금지, 리뷰는 이슈 기준.\n"
                    "kanban-codex-delegate 절차로 작업하고 핸드오프 3필드(not_implemented는 JSON 배열)로 kanban_complete.")
            rc2, out2, err2 = sh([HERMES, "kanban", "create", f"[mirror] {issue['title']}",
                                  "--body", body, "--assignee", "executor",
                                  "--workspace", f"dir:{HOME}/work/{repo.split('/')[1]}",
                                  "--idempotency-key", key, "--max-retries", "3"], timeout=60)
            print(f"import {key}: {'ok' if rc2 == 0 else 'FAIL ' + err2[:80]}")
        # ── 투영 ──
        for key, status in keys.items():
            if not key.startswith(f"github-issue:{repo}#"): continue
            n = key.rsplit("#", 1)[1]
            target = STATUS_TO_LABEL.get(status)
            if not target: continue
            rc3, out3, _ = sh([GH, "api", f"repos/{repo}/issues/{n}", "--jq", "[.labels[].name] , .state"])
            if rc3 != 0: continue
            lines = out3.splitlines()
            current = json.loads(lines[0]) if lines else []
            state = lines[1] if len(lines) > 1 else "open"
            if state != "open": continue  # 닫힌 이슈는 손대지 않음
            forge_now = [l for l in current if l.startswith("forge:")]
            if forge_now == [target]: continue
            keep = [l for l in current if not l.startswith("forge:")] + [target]
            patch = [GH, "api", "-X", "PATCH", f"repos/{repo}/issues/{n}",
                     "--input", "-"]
            p = subprocess.run(patch, input=json.dumps({"labels": keep}), capture_output=True, text=True, timeout=30)
            print(f"project #{n}: {forge_now} -> [{target}] {'ok' if p.returncode == 0 else 'FAIL'}")

if __name__ == "__main__":
    main()
