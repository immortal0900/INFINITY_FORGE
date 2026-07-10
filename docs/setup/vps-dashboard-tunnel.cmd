@echo off
REM INFINITY_FORGE — VPS hermes 대시보드 SSH 터널
REM 실행하면 http://127.0.0.1:9119 가 VPS 대시보드로 연결됩니다 (창을 닫으면 터널 종료)
echo VPS 대시보드 터널 연결 중... (이 창을 열어두세요)
ssh -N -L 9119:127.0.0.1:9119 ubuntu@51.222.27.48
