# Linux CLI Chat/Task 선택기 실행 경로 설계

## 목표

EC2와 VPS의 일반 `hermes` CLI가 어떤 작업 폴더에서 시작되더라도, Hermes가
`PYTHONPATH`를 제거한 상태에서 Infinity Forge plugin을 실제로 불러오고 첫
사용자 입력을 Chat/Task 선택기로 처리한다.

## 확인된 문제

- Linux 배포는 plugin 파일과 enabled 설정만 설치한다.
- Hermes의 공식 launcher는 시작 전에 `PYTHONPATH`를 제거한다.
- plugin은 `forge.ops`를 import하므로 INFINITY_FORGE 저장소 밖에서는 import가
  실패한다.
- 기존 검증은 enabled allow-list와 patched marker만 확인해 실제 plugin import와
  `pre_user_turn` hook 등록 실패를 놓쳤다.
- Gateway는 systemd 환경에 `PYTHONPATH`가 있어 정상이고 interactive CLI만
  실패한다.

## 검토한 접근

1. launcher에서 `PYTHONPATH`를 다시 export한다. 변경은 작지만 Hermes update가
   launcher를 다시 만들면 재발하고 mutable working tree를 전역 import 경로로
   노출한다.
2. Hermes venv에 `.pth`를 설치한다. 작업 폴더에는 독립적이지만 venv 재생성 때
   사라지고 모든 Hermes Python process의 import 경로를 바꾼다.
3. Windows와 같은 commit별 managed release를 Linux에도 설치한다. plugin만
   검증된 release를 import하고 launcher와 venv를 수정하지 않는다.

3번을 사용한다.

## 구조

- Linux release root는 `$HOME/.hermes/infinity-forge/releases`다.
- 배포 commit의 clean Git archive를 `<release-root>/<40자 commit>`에 임시
  directory에서 완성한 뒤 같은 filesystem의 rename으로 공개한다.
- plugin 세 파일은 `$HOME/.hermes/plugin-releases/<commit>`에서 먼저 완성한다.
  기존 일반 plugin directory는 discovery 경로 밖에 backup하고,
  `$HOME/.hermes/plugins/infinity-forge` symbolic link만 원자적으로 교체한다.
  따라서 기존 non-empty directory가 있어도 upgrade할 수 있고 이전 link나
  directory로 rollback할 수 있다.
- plugin bootstrap은 Windows의 `%LOCALAPPDATA%/InfinityForge/releases`와 Linux의
  `<Hermes home>/infinity-forge/releases`만 허용한다. pointer가 root 밖이거나
  commit 이름·필수 파일이 잘못되면 import를 중단한다.
- `INFINITY_FORGE_REPOSITORY`, `INFINITY_FORGE_TASK_SETTINGS_DB`,
  `INFINITY_FORGE_GH_PATH`는 Hermes의 `save_env_value`로 기존 `.env`의 해당 key만
  갱신한다. 값 전체를 출력하거나 다른 key를 덮어쓰지 않는다.

## 배포와 복구

배포는 사용자별 `flock`을 fast-forward 전부터 종료까지 유지해 동시 실행을 막는다.
runtime 정지 후 release, plugin, environment를 설치한다. 실패하면 실행 중인
gateway·timer·service를 먼저 정지하고 environment와 plugin을 역순으로 복구한 뒤
기존 runtime 상태를 복원한다. commit별 release는 immutable이며 기존 release는
실패 시에도 삭제하지 않아 이미 시작된 CLI와 이전 plugin link가 계속 사용할 수 있다.

## 검증 계약

1. plugin bootstrap unit test가 Windows와 Linux root, root 이탈, 잘못된 SHA,
   불완전 release를 검증한다.
2. Linux 배포 계약 test가 clean archive, atomic plugin pointer, 세 environment key,
   실제 hook smoke를 요구한다.
3. 배포 후 `$HOME` 또는 Cognet9 같은 외부 폴더에서 `PYTHONPATH`를 제거하고 plugin을
   discovery한다.
4. `infinity-forge`가 enabled이고 error가 없으며 `pre_user_turn` hook이 등록돼야
   한다.
5. synthetic 첫 입력 결과의 choice ID가 정확히 `chat`, `task`여야 한다. 모델 호출과
   GitHub/Kanban write는 하지 않는다.
