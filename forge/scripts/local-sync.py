#!/usr/bin/env python3
"""INFINITY_FORGE local-sync — 로컬 Hermes 작업 공유와 원격 DB 백업.
5분 주기 (--loop):
  1) 로컬 kanban.db에서 forge-task:* 작업의 상태 전이 감지
     → GitHub 이슈 코멘트([Forge-Local] ...) + Slack #forge-local 알림
     ※ forge:* 라벨은 issue-status-sync만 변경한다.
  2) 일 1회: kanban.db·state.db를 SQLite backup API로 복사해 VPS에 보관
"""
import json, os, sqlite3, subprocess, sys, time, datetime, urllib.request

LOCALAPPDATA = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
HOME = os.path.expanduser("~")
DB = os.path.join(LOCALAPPDATA, "hermes", "kanban.db")
STATEDB = os.path.join(LOCALAPPDATA, "hermes", "state.db")
ENV = os.path.join(LOCALAPPDATA, "hermes", ".env")
STATE = os.path.join(HOME, "forge-backups", "local-sync-state.json")
SSH_KEY = os.path.join(HOME, ".ssh", "id_ed25519")
VPS = "ubuntu@51.222.27.48"
NOTIFY = {"done": "✅ done", "blocked": "⛔ blocked", "failed": "🔴 failed", "running": "▶ running"}

def read_env(key):
    try:
        for line in open(ENV, encoding="utf-8"):
            if line.startswith(key + "="):
                return line.strip().split("=", 1)[1]
    except OSError:
        pass
    return ""

def gh_comment(repo, num, body):
    pat = os.environ.get("GITHUB_PAT", "")
    if not pat:
        return False
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues/{num}/comments",
        json.dumps({"body": body}).encode(),
        {"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json",
         "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
        return True
    except Exception:
        return False

def slack(text):
    token = read_env("SLACK_BOT_TOKEN")
    if not token:
        return
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        json.dumps({"channel": "#forge-local", "text": text}).encode(),
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception:
        pass

def task_reference(key):
    """Return OWNER/REPO and issue number from one new root Task key."""
    if not isinstance(key, str) or not key.startswith("forge-task:"):
        raise ValueError("Task key must start with forge-task")
    identity, settings_short_hash = key.removeprefix("forge-task:").rsplit(":", 1)
    if len(settings_short_hash) != 16:
        raise ValueError("Task key settings value is invalid")
    repository, issue_number = identity.rsplit("#", 1)
    if "/" not in repository or not issue_number.isdigit():
        raise ValueError("Task key project or issue is invalid")
    return repository, issue_number

def cycle():
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    prev = {}
    if os.path.exists(STATE):
        try: prev = json.load(open(STATE))
        except Exception: prev = {}
    cards = {}
    if os.path.exists(DB):
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        for key, status, title, cid in con.execute(
                "SELECT idempotency_key, status, title, id FROM tasks WHERE idempotency_key LIKE 'forge-task:%'"):
            cards[key] = {"status": status, "title": title, "id": cid}
        con.close()
    for key, c in cards.items():
        if prev.get(key) != c["status"] and c["status"] in NOTIFY:
            repo, num = task_reference(key)
            ref = f"{repo}#{num}"
            body = f"[Forge-Local] {NOTIFY[c['status']]} — 카드 {c['id']} (로컬 머신 실행)"
            ok = gh_comment(repo, num, body)
            slack(f"{NOTIFY[c['status']]} [local:{ref}] {c['title']} ({'이슈 코멘트됨' if ok else '코멘트 실패'})")
    # 일 1회 백업 push
    today = datetime.date.today().isoformat()
    if prev.get("_backup_date") != today and os.path.exists(DB):
        bdir = os.path.join(HOME, "forge-backups", "local-" + today.replace("-", ""))
        os.makedirs(bdir, exist_ok=True)
        for src in (DB, STATEDB):
            if not os.path.exists(src): continue
            dst = os.path.join(bdir, os.path.basename(src))
            s = sqlite3.connect(f"file:{src}?mode=ro", uri=True); d = sqlite3.connect(dst)
            s.backup(d); d.close(); s.close()
        r = subprocess.run(["ssh", "-i", SSH_KEY, VPS, "mkdir -p ~/backups/local-hermes"],
                           capture_output=True, timeout=30)
        r2 = subprocess.run(["scp", "-i", SSH_KEY, "-q", "-r", bdir, f"{VPS}:~/backups/local-hermes/"],
                            capture_output=True, timeout=120)
        if r2.returncode == 0:
            cards["_backup_date"] = today
        else:
            slack(f"⚠️ [local] 로컬 DB 백업 push 실패: {r2.stderr.decode()[:80]}")
    else:
        cards["_backup_date"] = prev.get("_backup_date", "")
    tmp = STATE + ".tmp"
    payload = {k: (v["status"] if isinstance(v, dict) else v) for k, v in cards.items()}
    json.dump(payload, open(tmp, "w"))
    os.replace(tmp, STATE)

def acquire_singleton_lock():
    """--loop 중복 실행 방지: 락 파일 배타 잠금(핸들 유지, 프로세스 종료 시 OS가 자동 해제)."""
    import msvcrt
    path = os.path.join(HOME, "forge-backups", "local-sync.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f = open(path, "w")
    try:
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        print("local-sync: --loop 인스턴스가 이미 실행 중 — 이 인스턴스는 종료", file=sys.stderr)
        sys.exit(0)
    f.write(str(os.getpid()))
    f.flush()
    return f

def main():
    if "--loop" in sys.argv:
        _lock = acquire_singleton_lock()  # noqa: F841 — 핸들 유지가 곧 잠금 유지
        while True:
            try: cycle()
            except Exception as e:
                print("cycle error:", e, file=sys.stderr)
            time.sleep(300)
    else:
        cycle()
        print("local-sync: 1 cycle done")

if __name__ == "__main__":
    main()
