# 백업 사본 위치·복원 가이드

> 이 문서는 INFINITY_FORGE의 데이터가 **어디에 몇 벌 저장되는지**와 **잃었을 때 어떻게 되살리는지**를 담는다. 배경 지식 없이 따라 할 수 있게 실제 경로·명령으로 서술한다.

## 1. 무엇을 지키나 — 보호 대상 3종

| 데이터 | 정체 | 잃으면 |
|---|---|---|
| kanban.db | 작업 장부(카드·단계·이력) | 어떤 작업이 어디까지 갔는지 소실 |
| state.db | hermes 세션·대화 기록 | 과거 대화 소실(작업 자체엔 영향 적음) |
| MEMEX(Neo4j·vault) | 지식 DB(교훈·결정·위키) | 다음 작업이 덜 똑똑해짐 |

VPS와 로컬 노트북 **양쪽이 각자 kanban.db·state.db를 따로** 가진다(장부는 머신별 독립). 그래서 백업도 양쪽을 각각 뜬다.

## 2. 사본이 실제로 있는 위치 (실측)

데이터는 항상 **원본 1 + 사본 2**(다른 머신 포함)로 존재한다.

### VPS 데이터의 사본

| 사본 | 위치 | 만드는 주체 | 주기 |
|---|---|---|---|
| 원본 | VPS `~/.hermes/kanban.db`, `~/.hermes/state.db` | hermes | 실시간 |
| VPS 자체 백업 | VPS `~/backups/hermes/<날짜>/` (hermes DB + MEMEX 3볼륨, 약 5.7MB) | `nightly-backup.sh` | 매일 04:30 KST |
| **노트북 사본(오프박스)** | 로컬 `%USERPROFILE%\forge-backups\<날짜>\` | `pull-backup.ps1` | 로그온 시 |

### 로컬(노트북) 데이터의 사본

| 사본 | 위치 | 만드는 주체 | 주기 |
|---|---|---|---|
| 원본 | 로컬 `%LOCALAPPDATA%\hermes\kanban.db`, `state.db` | 로컬 hermes | 실시간 |
| **VPS 사본(오프박스)** | VPS `~/backups/local-hermes/<날짜>/` | `local-sync.py` (일일 push) | 매일 1회 |

즉 **VPS가 죽어도 노트북에 VPS 사본**이, **노트북이 죽어도 VPS에 노트북 사본**이 있다. 서로가 서로의 오프박스다.

실제 경로 확인(직접 해볼 수 있음):
- 노트북에서: `explorer %USERPROFILE%\forge-backups`
- VPS에서: `ssh ubuntu@51.222.27.48 "ls ~/backups/hermes ~/backups/local-hermes"`

## 3. 복원 방법 (단계별)

### VPS 장부가 깨졌을 때 → 노트북 사본으로 되살리기
1. 게이트웨이 정지: `ssh ubuntu@51.222.27.48 "systemctl --user stop hermes-gateway"`
2. 노트북에서 최신 사본을 VPS로 올리기:
   `scp %USERPROFILE%\forge-backups\<날짜>\kanban.db ubuntu@51.222.27.48:~/.hermes/kanban.db`
3. 무결성 확인: `ssh ubuntu@51.222.27.48 "sqlite3 ~/.hermes/kanban.db 'PRAGMA integrity_check;'"` → `ok` 여야 함
4. 게이트웨이 재시작: `ssh ubuntu@51.222.27.48 "systemctl --user start hermes-gateway"`

### VPS 자체 백업(전날치)으로 되살리기
- 사본이 `~/backups/hermes/<날짜>/`에 그대로 있으므로 2번 대신 그 파일을 `~/.hermes/`로 복사만 하면 된다.

### MEMEX(Neo4j)가 깨졌을 때
- 백업은 `~/backups/hermes/<날짜>/memex-neo4j-data.tgz`. 복원은 컨테이너 정지 → 볼륨(`/var/lib/docker/volumes/deploy_neo4j-data/_data`)에 압축 해제 → 컨테이너 시작. (MEMEX는 지식 저장소라 작업을 막지 않으므로 급하지 않다.)

## 4. 한계와 다음 단계 (정직하게)

- 현재 백업은 **최대 하루치 유실 가능**(스냅샷 주기 24h). 초 단위 연속 복제가 필요하면 Litestream(VPS에 설치됨)에 S3 호환 저장소(Cloudflare R2 무료 등)를 붙이면 된다 — 계정만 생기면 5분.
- VPS 자체 백업은 **같은 디스크**에 있어 디스크 물리 손상엔 취약하다. 그걸 메우는 게 노트북 사본(다른 물리 장비)이다.

**한마디 요약**: 장부는 양쪽 머신에 원본이 따로 있고, 서로의 백업을 매일 교환해 보관한다 — 한쪽 머신이 통째로 죽어도 다른 쪽에 사본이 남는다.
