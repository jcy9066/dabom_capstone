# AGENTS.md

`dabom_capstone`에서 Codex가 따라야 하는 프로젝트 공통 규칙이다.

## 1. 기준 구조

- Server: `server/app.py`
- Frontend: `frontend/templates/`, `frontend/services/static/`
- ROS 2: `navigation/ros/patrol_navigation/`
- Raspberry Pi target: **Raspberry Pi 3 Model B (1GB), Ubuntu Server 22.04 arm64**
- GPU runtime launcher: `start_gpu_server.sh`
- Raspberry Pi runtime launcher: `start_pi_stack.sh`
- Raspberry Pi client: `raspberry/robot_command_client.py`
- Motor control: `raspberry/controllers/motor_controller.py`
- Pico W: `raspberry/pico_w_sdk/main.c`
- Tests: `tests/`

Pi와 Server의 역할은 다음 기준을 유지한다.

```text
Raspberry Pi 3B
- camera/LiDAR/encoder 수집
- Pico W UART motor/LED control
- H.264/WebSocket/HTTP 전송

GPU Server
- FastAPI/Web/DB
- AI perception
- SLAM/Localization/Nav2
- ROS bridge/odometry
```

Pi 3B UART 기준은 `/dev/serial0`, 115200 8N1, GPIO14/15이며,
PL011(`ttyAMA0`)을 primary UART로 사용하는 현재 하드웨어 구성을 기준으로 한다.
Pi 3B 내장 Wi-Fi는 2.4GHz를 사용한다.

문서와 코드가 충돌하면 현재 실행 경로와 실제 참조 관계를 우선한다.
과거 Flask/MicroPython/backup 구현은 현재 기준으로 간주하지 않는다.

## 2. 구현 Workflow

코드 또는 설정 변경 시 Primary/Main Agent는 다음 순서를 지킨다.

1. 요청 범위를 확정한다.
2. `implement-feature`를 적용한다.
3. 수정 전에 `code_explorer`에게 현재 범위의 실행 경로와 영향 파일만 조사시킨다.
4. 조사 결과를 바탕으로 작업 DAG, dependency, 공통 contract, 파일 ownership을 확정한다.
5. 서로 독립적인 구현 작업만 별도 feature branch + linked Git worktree로 분리한다.
6. 명시적으로 허용된 implementation worker를 각자의 assigned worktree에서 병렬 실행한다.
7. 각 worker는 자신의 ownership 범위만 구현 → 검증 → 새 commit한다.
8. worker 결과가 모두 PASS이면 Primary/Main Agent는 commit hash와 통합 순서를 보고하고 멈춘다.
9. branch 간 merge/cherry-pick/rebase는 사용자가 직접 수행한다.
10. 사용자가 integration 완료를 알리면 통합 단계 구현과 검증을 계속한다.
11. 모든 구현 완료 후 필요한 reviewer를 병렬 실행한다.
12. 최종적으로 `review-change`와 `test_reviewer`를 수행한다.
13. 변경/검증/commit/미검증 항목만 간결하게 보고한다.

Validation routing:

- `server/` → `validate-server`
- `frontend/` → `validate-dashboard`
- `server/app.py` → `validate-server` + `validate-dashboard`
- `navigation/` → `validate-navigation`
- `raspberry/` 또는 `start_pi_stack.sh` → `validate-raspberry`
- `start_gpu_server.sh` → `validate-server` + `validate-navigation`
- source/config 변경 → `review-change`

Runtime launcher 관련 변경은 최소한 다음을 확인한다.

```text
bash -n start_gpu_server.sh start_pi_stack.sh
python .agents/skills/validate-raspberry/scripts/validate_uart_protocol.py
```

실제 Pi 3B가 없는 개발 환경에서는 정적 검증과 mock/test 결과까지만 인정한다.
실제 UART, Camera, LiDAR, Pico 응답은 Pi 3B 실기기에서 실행한 결과와 구분해서 보고한다.

## 3. Orchestration / Worktree 규칙

- `main`과 `dev`는 protected branch다.
- `main`/`dev`는 조회와 새 branch/worktree의 start point로만 사용할 수 있다.
- 어떤 Agent도 `main` 또는 `dev`에서 `git add`나 `git commit`을 실행하지 않는다.
- 구현은 반드시 protected branch가 아닌 **linked Git worktree**에서 수행한다.
- Primary/Main Agent는 새 branch와 새 linked worktree를 생성할 수 있다.
- Codex가 자동으로 수행할 수 있는 worktree 변경은 `git worktree add -b ...`뿐이다.
- branch 생성은 허용하지만 branch 삭제/rename/force-update는 금지한다.
- worktree 삭제/move/prune/repair/lock/unlock은 사용자가 직접 수행한다.
- worker마다 branch, worktree, ownership을 명시한다.
- worker는 자신에게 할당되지 않은 worktree에서 source/config를 수정하지 않는다.
- worker가 ownership 충돌이나 contract mismatch를 발견하면 임의로 범위를 넓히지 말고 `BLOCKED`로 부모에게 반환한다.
- 여러 worker의 commit 통합은 사용자가 직접 수행한다.
- integration 전에는 서로 다른 worktree의 수정 내용을 복사하거나 덮어쓰지 않는다.
- `.env`, secret, runtime image, DB dump를 worktree 사이에 자동 복사하지 않는다.

Orchestration state 예시:

```text
T0 CONTRACT    PASS
T1 DB          RUNNING
T2 MEDIA       RUNNING
T3 UI          RUNNING
T4 INTEGRATION BLOCKED
T5 REVIEW      BLOCKED
```

실패한 node만 reopen하며 전체 작업을 불필요하게 다시 수행하지 않는다.

## 4. 범위 및 안전

- 요청하지 않은 기능, refactor, rename, 문서를 임의로 추가하지 않는다.
- 기존 구현을 검색한 뒤 수정하고 중복 구현을 만들지 않는다.
- 파일 제거 전 실제 참조를 확인한다.
- 실제 하드웨어가 없으면 static/mock/build 결과와 실환경 결과를 구분한다.
- 사용자의 명시적 요청 없이 실제 motor, GPIO, serial movement, firmware flash,
  `/cmd_vel`, Telegram 전송, 경고 방송, 지도/사용자/운영 DB 삭제를 실행하지 않는다.
- `start_pi_stack.sh`는 실제 UART, Camera, LiDAR, Pico W와 상호작용하고 기존 동일 역할 프로세스를 종료하므로
  사용자의 명시적 요청 없이 개발 PC나 임의 환경에서 자동 실행하지 않는다.
- `start_gpu_server.sh`는 기존 FastAPI/odometry/navigation runtime 프로세스를 종료할 수 있으므로
  현재 GPU runtime을 재시작해도 되는 상황에서만 실행한다.
- 운영 DB DDL/DML은 사용자가 현재 작업에서 명시적으로 승인한 범위만 수행한다.
- 승인된 migration을 적용하기 전 현재 schema를 확인하고, 적용 후 다시 schema를 검증한다.
- `DROP DATABASE`, `DROP TABLE`, `TRUNCATE`, 대량 `DELETE`는 명시적 별도 승인 없이는 금지한다.
- `.env`, password, token, secret, model weight, build/log/runtime 산출물을 commit하지 않는다.

## 5. Token / Context 절약 규칙

항상 필요한 정보만 읽고 반환한다.

- 전체 repository/file/log dump보다 `rg`, 경로 제한 검색, 부분 읽기를 우선한다.
- 동일 파일과 동일 검증 결과를 불필요하게 다시 읽거나 반복하지 않는다.
- 기본 Git 확인은 `status --short`, `diff --stat`, 필요한 파일의 targeted diff를 우선한다.
- 테스트는 관련 test부터 `-q`로 실행하고, 전체 suite는 최종 회귀 확인이 필요할 때만 실행한다.
- 성공 로그 전체를 반환하지 않는다. 실패한 명령은 오류 핵심과 필요한 주변 문맥만 반환한다.
- Sub-Agent 결과는 최대 15줄로 제한한다.
- Sub-Agent는 원본 log, 전체 diff, 전체 source를 부모에게 복사하지 않는다.
- 결과는 `PASS/WARN/FAIL/BLOCKED`, 근거 파일/symbol, blocking issue, skipped 항목만 전달한다.
- 이미 같은 diff와 범위를 검토한 Sub-Agent 결과가 있으면 재사용한다.

## 6. RTK 사용

RTK(Rust Token Killer)가 PATH에 있으면 shell 출력이 큰 지원 명령에 우선 사용한다.

권장:

```text
rtk git status
rtk git log -n 10
rtk git diff
PYTHONPATH="$PWD" rtk pytest -q
rtk grep "pattern" path
rtk find "pattern" path
rtk read path
```

규칙:

- RTK는 shell 출력 압축용이며 Git 권한 정책을 변경하지 않는다.
- `rtk git add`와 `rtk git commit`은 아래 Git 정책 범위에서만 허용한다.
- `rtk git push/pull/fetch/merge/...`도 동일하게 금지한다.
- Windows + Git Bash에서 Python test는 project root import 보장을 위해 `PYTHONPATH="$PWD"`를 붙인다.
- RTK pytest가 실패하면 동일 범위에서 `PYTHONPATH="$PWD" python -m pytest -q`로 한 번만 fallback한다.
- RTK가 없거나 해당 명령을 지원하지 않거나 한 번 실패하면 raw compact command로 한 번만 fallback한다.
- pathspec 등 RTK filter가 잘못 해석되는 명령은 raw targeted command를 사용한다.
- Codex가 `rtk init`, 설치, 업데이트를 자동 실행하지 않는다.
- 절감 확인이 필요할 때만 `rtk gain` 또는 `rtk gain --history`를 실행한다.

## 7. Sub-Agent

Read-only reviewer:

- `code_explorer`: 수정 전 실행 경로/영향 범위 조사
- `dashboard_reviewer`: Playwright browser 검증
- `ros_reviewer`: ROS/SLAM/AMCL/Nav2/TF 검토
- `hardware_reviewer`: Pi 3B/Pico/UART/failsafe/runtime 설정 검토
- `test_reviewer`: 최종 diff/regression/test gap 검토

Implementation worker:

- `db_worker`: DB schema/migration/database access layer 구현
- `media_worker`: privacy image persistence/event cooldown 연동 구현
- `dashboard_worker`: dashboard HTML/CSS/JS UI 구현

규칙:

- Reviewer Sub-Agent는 계속 read-only다.
- Codex runtime에서 Sub-Agent는 부모 session의 effective sandbox를 상속할 수 있으므로,
  `code_explorer`와 reviewer를 실제 read-only로 강제해야 하는 단계는 부모 Codex 자체를
  `--sandbox read-only`로 시작한다.
- `db_worker`, `media_worker`, `dashboard_worker` 구현 단계는
  `workspace-write` 부모 session에서만 실행한다.
- 동일한 parent session에서 read-only reviewer와 writable implementation worker를
  혼합 실행하지 않는다.
- Implementation worker만 `workspace-write`를 사용한다.
- Implementation worker는 부모가 지정한 linked worktree와 ownership 범위에서만 수정한다.
- worker 시작 시 현재 branch와 linked worktree 여부를 확인한다.
- 현재 branch가 `main`/`dev`이거나 assigned worktree가 아니면 수정하지 않고 `BLOCKED`를 반환한다.
- worker는 다른 worker를 생성하지 않는다.
- worker는 다른 worker의 ownership 파일을 수정하지 않는다.
- worker는 자신의 관련 test와 validator가 PASS한 경우에만 task-scoped `git add`와 새 commit을 수행한다.
- 현재 thread가 reviewer이면 같은 reviewer를 다시 생성하지 않는다.

## 8. Git 정책

허용되는 상태 변경:

- 새 branch 생성
- `git worktree add -b <branch> <path> [<start-point>]`
- `git switch --no-guess <existing-branch>`
- linked feature worktree에서의 `git add`
- linked feature worktree에서의 새 `git commit`

조건:

- `main`과 `dev`에서는 `git add`와 `git commit`이 항상 금지된다.
- `git add`와 `git commit`은 protected branch가 아닌 linked worktree에서만 허용한다.
- 현재 작업에 속하는 파일만 stage한다.
- validation과 `review-change`가 PASS인 작업만 commit한다.
- `git commit --amend`는 금지한다.
- commit message는 한 줄 `type: Summary`, 50자 이하, 끝에 `.` 없음.
- 허용 type:
  `feat`, `fix`, `docs`, `style`, `design`, `test`, `refactor`, `build`,
  `ci`, `perf`, `chore`, `rename`, `remove`

예:

```text
feat: Add dashboard state synchronization
fix: Resolve dashboard asset versioning
```

계속 금지:

- `push`, `pull`, `fetch`, `merge`, `rebase`, `cherry-pick`, `revert`
- `reset`, `restore`, `checkout`, `stash`, `tag`, `clean`
- `am`, `apply`, `bisect`, `clone`, `init`, `mv`, `rm`
- branch 삭제/rename/force-update
- `git worktree remove`, `move`, `prune`, `repair`, `lock`, `unlock`
- 기존 history rewrite

읽기 전용 Git 명령은 허용한다.

## 9. GitHub MCP

GitHub MCP는 repository 조회를 기본 read-only로 사용한다.

허용:

- branch/file/tree/code/commit/diff/PR/Issue/Actions 조회
- 사용자 승인 후 Issue/PR/comment/review/label/assignee 협업 작업

금지:

- 원격 file 생성/수정/삭제
- 원격 commit/ref/branch 생성 또는 변경
- PR merge, tag/release, workflow/repository 설정 변경

로컬 branch/worktree 생성과 `git add`/새 `git commit`은 GitHub MCP가 아니라 로컬 Git CLI 정책을 따른다.

## 10. 결과 보고

최종 보고는 가능하면 20줄 이내로 유지하고 다음만 포함한다.

- task/worktree/branch 상태
- 변경 파일/핵심 변경
- 실행한 Skill/Sub-Agent
- PASS/FAIL/WARN/SKIP/BLOCKED
- 생성 commit hash/message
- 사용자 integration이 필요한 commit 순서
- 실제 환경에서 남은 검증
- 범위 밖에서 발견한 blocking issue