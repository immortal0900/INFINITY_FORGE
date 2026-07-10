#!/usr/bin/env python3
"""INFINITY_FORGE ledger-emit — kanban 이벤트를 ~/forge/ledger.jsonl에 append (LLM 0, 10분 주기).
단조 증가 보장: 마지막 처리 이벤트 id를 state 파일에 기록, 그 이후만 방출.
MEMEX 진행상태 미러는 이벤트당이 아니라 morning-report의 일별 집계 1건으로 대체(쿼터 보호)."""
import json, os, sqlite3, sys, datetime

HOME = os.path.expanduser("~")
DB = os.path.join(HOME, ".hermes", "kanban.db")
LEDGER = os.path.join(HOME, "forge", "ledger.jsonl")
STATE = os.path.join(HOME, "forge", "ledger.state")

def main():
    last = 0
    if os.path.exists(STATE):
        try: last = int(open(STATE).read().strip())
        except ValueError: last = 0
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT e.id, e.task_id, e.run_id, e.kind, e.payload, e.created_at, t.title, t.assignee, t.status "
        "FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id "
        "WHERE e.id > ? ORDER BY e.id", (last,)).fetchall()
    con.close()
    if not rows:
        return
    with open(LEDGER, "a", encoding="utf-8") as f:
        for r in rows:
            rec = {"event_id": r[0], "task_id": r[1], "run_id": r[2], "kind": r[3],
                   "payload": r[4], "ts": datetime.datetime.fromtimestamp(r[5]).isoformat(),
                   "title": r[6], "assignee": r[7], "task_status": r[8]}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush(); os.fsync(f.fileno())
    # 단조 증가 검증 후 state 갱신 (tmp+rename 원자)
    new_last = rows[-1][0]
    if new_last <= last:
        print(f"GATE_ERROR: ledger monotonicity violated ({new_last} <= {last})", file=sys.stderr)
        sys.exit(2)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f: f.write(str(new_last))
    os.replace(tmp, STATE)
    print(f"emitted {len(rows)} events (last={new_last})")

if __name__ == "__main__":
    main()
