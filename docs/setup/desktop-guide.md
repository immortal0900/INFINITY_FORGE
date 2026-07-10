# Hermes Desktop 운용 가이드 (터미널 불필요, 클릭만으로)

준비물(바탕화면에 있음): **Hermes Desktop** 바로가기, **VPS 터널** 바로가기.
로그인 파일: `%USERPROFILE%\forge-backups\vps-dashboard-cred.txt` (forge / 비밀번호)

## A. 로컬 보기
1. 바탕화면 **Hermes Desktop** 더블클릭 → 끝 (자체 로컬 백엔드 내장)

## B. 클라우드(VPS) 보기 — 최초 1회 설정
1. 바탕화면 **VPS 터널** 더블클릭 → 검은 창 유지(닫으면 연결 끊김)
2. Desktop → Settings(톱니) → Gateway → **Remote gateway**
3. Remote URL: `http://127.0.0.1:9119`
4. 로그인: cred.txt의 forge / 비밀번호
5. **Save and reconnect** → 클라우드 뷰로 전환

## C. 전환
- Settings → Gateway에서 Local ↔ Remote 토글 + Save and reconnect (동시 표시는 미지원)
- 클라우드 볼 때만 터널 창 필요

## D. 도구 선택 기준
| 목적 | 도구 |
|---|---|
| 일상 지시·상태 확인 | Slack (#forge-local / #forge-cloud) |
| 세션·보드 큰 화면 관제 | Hermes Desktop |
| PR 머지·이슈 작성 | GitHub |

클라우드에 작업 투입 = GitHub 이슈에 `forge:need-execution` 라벨. 로컬 작업 = #forge-local에서 지시.
