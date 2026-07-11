# INFINITY_FORGE 운영 가이드 — 백업 사본과 자동화의 실체

> 작성: 2026-07-10. 이 문서는 두 질문에 답한다: ① 백업 사본이 어디 있고 어떻게 복원하나, ② "무인 운영"이 어떤 원리로 돌아가나(상주 세션인가, 설정인가).

---

## 1. 백업 사본 위치 (양방향 오프박스)

오프박스(off-box, 원본과 다른 컴퓨터에 사본을 두는 것)가 **양방향**으로 걸려 있다. 한쪽 머신이 통째로 죽어도 반대쪽에 장부 사본이 남는다.

| 무엇의 사본 | 원본 위치 | 사본 위치 | 만드는 주체 | 주기 | 보관 |
|---|---|---|---|---|---|
| **VPS 장부+MEMEX** (kanban.db, state.db, config.yaml, memex-neo4j-data.tgz, memex-vault.tgz, memex-state.tgz) | VPS ~/.hermes/, docker 볼륨 | ① VPS `~/backups/hermes/YYYYMMDD/` ② **노트북 `%USERPROFILE%\forge-backups\YYYYMMDD\`** | ① nightly-backup.sh(04:30 KST) ② pull-backup.ps1(노트북 로그온 시 ①을 통째로 복사) | 매일 | VPS 7일, 노트북 14일 |
| **로컬 장부** (로컬 kanban.db, state.db) | 노트북 %LOCALAPPDATA%\hermes\ | ① 노트북 `%USERPROFILE%\forge-backups\local-YYYYMMDD\` ② **VPS `~/backups/local-hermes/local-YYYYMMDD/`** | local-sync.py(5분 루프 중 하루 1회 백업 단계) | 매일 | 노트북 14일 |

직접 확인:
```powershell
# 노트북에서 — VPS 백업이 왔는지
dir $env:USERPROFILE\forge-backups
# VPS에서 — 로컬 백업이 왔는지
ssh ubuntu@51.222.27.48 "ls ~/backups/local-hermes/ ~/backups/hermes/"
```

## 2. 복원 가이드 (재해 시나리오별)

### VPS가 통째로 죽었다 → 노트북 사본으로 복원
1. 새 VPS 준비 후 hermes 설치(레포의 docs/plan.md Phase 0 런북)
2. 노트북의 가장 최신 `%USERPROFILE%\forge-backups\YYYYMMDD\`에서:
   - `kanban.db`, `state.db` → 새 VPS `~/.hermes/`에 scp로 복사 (게이트웨이 중지 상태에서)
   - `memex-neo4j-data.tgz` → 압축 해제해 docker 볼륨 `deploy_neo4j-data`의 `_data`에 풀기 (neo4j 컨테이너 중지 상태에서)
   - `memex-vault.tgz`, `memex-state.tgz` → 각각 `deploy_vault`, `deploy_state` 볼륨에 동일하게
3. `systemctl --user start hermes-gateway` → `hermes kanban list`로 카드 확인

### 노트북이 죽었다 → VPS 사본으로 복원
1. 새 노트북에 hermes 네이티브 설치 후:
```powershell
scp -r ubuntu@51.222.27.48:~/backups/local-hermes/local-<최신날짜>/* $env:LOCALAPPDATA\hermes\
```
2. 로그온 상주 3종(시작프로그램의 Hermes_Gateway.vbs, ForgeBackupPull.vbs, ForgeLocalSync.vbs)은 레포 `forge/scripts/`에서 재등록

### DB 파일 하나만 깨졌다
백업본을 그 자리에 덮어쓰면 끝. 백업은 `sqlite3 .backup`(무결성 검증 포함)으로 만든 정합 스냅샷이라 그대로 사용 가능.

⚠️ 한계: 백업 주기가 "매일"이므로 최악의 경우 마지막 백업 이후 하루치 장부 기록이 유실될 수 있다. 이걸 초 단위로 줄이는 것이 Litestream(VPS에 설치돼 대기 중, S3 계정 연결 시 가동)이다.

---

## 3. 자동화의 실체 — "세션"이 아니라 3종류의 장치다

**"Claude 세션이 계속 떠서 일하는 게 아니다.** 이 시스템의 무인 운영은 아래 3종류 장치의 조합이고, LLM(대형언어모델, gpt-5.5 같은 것)은 그중 한 종류에서만, 그것도 일회용으로 쓰인다.

### 장치 A: 상주 프로세스 (컴퓨터가 켜져 있는 한 항상 실행 중인 프로그램)

| 프로세스 | 어디 | 어떻게 상주하나 | 하는 일 |
|---|---|---|---|
| hermes gateway | VPS | systemd(리눅스의 서비스 관리자)가 서비스로 등록, 