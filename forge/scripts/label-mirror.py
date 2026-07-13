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
MIRROR_STATE = os.path.join(HOME, "forge", "mirror-state.json")
# D14 즉시 알림: 이 상태로 '전이'가 감지되면 Slack 직발송 (기계 전이 in-progress 등은 제외)
NOTIFY_STATUS = {"done": "✅ 리뷰 대기(PR 확인)", "blocked": "⛔ 결정/조치 필요", "failed": "🔴 재시도 소진"}

def slack(text):
    try:
        token = ""
        for line in open(os.path.join(HOME, ".hermes", ".env")):
            if line.startswith("SLACK_BOT_TOKEN="):
                token = line.strip().split("=", 1)[1]; break
        if not token: return
        subprocess.run(["curl", "-s", "-m", "10", "-X", "POST", "https://slack.com/api/chat.postMessage",
                        "-H", f"Authorization: Bearer {token}", "-H", "Content-Type: application/json",
                        "-d", json.dumps({"channel": "#forge-cloud", "text": text})],
                       capture_output=True, timeout=15)
    except Exception:
        pass  # 알림 실패가 미러를 막지 않는다

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
    rows = con.execute("SELECT idempotency_key, status, title, id FROM tasks WHERE idempotency_key LIKE 'github-issue:%'").fetchall()
    con.close()
    return {r[0]: {"status": r[1], "title": r[2], "id": r[3]} for r in rows}

def notify_transitions(cards):
    """D14: 인간 액션 대상 전이(done/blocked/failed) 신규 발생 시 즉시 Slack (24/7)."""
    prev = {}
    if os.path.exists(MIRROR_STATE):
        try: prev = json.load(open(MIRROR_STATE))
        except Exception: prev = {}
    for key, c in cards.items():
        old = prev.get(key)
        if c["status"] != old and c["status"] in NOTIFY_STATUS:
            issue_ref = key.replace("github-issue:", "")
            slack(f"{NOTIFY_STATUS[c['status']]} [{issue_ref}] {c['title']} (카드 {c['id']})")
    tmp = MIRROR_STATE + ".tmp"
    json.dump({k: v["status"] for k, v in cards.items()}, open(tmp, "w"))
    os.replace(tmp, MIRROR_STATE)

def main():
    cards = cards_by_key()
    notify_transitions(cards)
    keys = {k: v["status"] for k, v in cards.items()}
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
            # --goal: 완료 판정 judge가 같은 세션을 반복시키는 보조 방어층 (결정론 게이트의 대체가 아님).
            # --max-retries N은 "N번째 연속 실패에서 차단" = 총 N회 세션. 스펙(최대 4 고유 세션)에 맞춰 4.
            rc2, out2, err2 = sh([HERMES, "kanban", "create", f"[mirror] {issue['title']}",
                                  "--body", body, "--assignee", "executor",
                                  "--workspace", f"dir:{HOME}/work/{repo.split('/')[1]}",
                                  "--idempotency-key", key, "--max-retries", "4",
                                  "--goal", "--goal-max-turns", "20"], timeout=60)
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
