# DABOM Phase 3-A — AI 위험 감지 → LED + Pico W Beep 자동 경고

## 범위

기존 관리자의 `POST /api/navigation/control/warning` 및 Pi의 `warning` 명령 구현을 재사용합니다. **10초 동안 LED 0.5초 ON/OFF + Pico W Beep가 실행**됩니다. 새로운 USB 스피커의 오디오 인터컴·음량은 변경하지 않으며 TTS도 추가하지 않습니다. 모터 구동 명령은 절대 보내지 않습니다.

## 실행 조건

- 현재 스트림·모델 세대의 **실제로 게시된 추론 결과**만 사용합니다. 버려진 구세대 추론은 경고를 일으키지 않습니다.
- `result.danger == true`이면서 **신선한 X3D 장면 위험**(scene `status=ok`, `fresh=true`, `raw_danger=true`), 또는 **신선한 개별 행동 위험**(stale 아님, danger confidence), 또는 **신선한 위험 상호작용**만 허용합니다.
- 카메라 입력 시각이 현재 기준 3초(기본값)를 초과하면 무시합니다.
- 기본 **2회 연속 위험 추론**, 최대 관측 간격 3초, 전역 **30초 쿨다운**. 연속 감지 중에도 30초 후 경고가 반복될 수 있으므로 사람 없는 안전한 장소에서 시험해야 합니다.
- 명령은 별도 asyncio 태스크로 보내며, GPU 추론 스레드는 Pi ACK를 기다리지 않습니다.
- Pi에 기존 인증된 `type=warning, led_duration_ms=10000` 명령을 사용하고 ACK/오류를 로그로 기록합니다.

## 환경변수 — GPU 서버 WSL 프로젝트 루트 `.env`

```dotenv
# 최초 도입 시 모두 OFF가 안전합니다.
AUTO_WARNING_ENABLED=false
AUTO_WARNING_DRY_RUN=true
AUTO_WARNING_MIN_HITS=2
AUTO_WARNING_MAX_HIT_GAP_SEC=3
AUTO_WARNING_COOLDOWN_SEC=30
AUTO_WARNING_MAX_CAPTURE_AGE_SEC=3
```

**검증 순서:** `AUTO_WARNING_ENABLED=true`, `AUTO_WARNING_DRY_RUN=true`로 먼저 GPU 서버를 재시작하고 실제 위험 탐지 시 GPU 로그에서 `[auto-warning][DRY-RUN]`을 확인합니다. 그다음 로봇을 정지 상태로 두고, 주변 사람에게 경고 테스트를 알린 다음 `AUTO_WARNING_DRY_RUN=false`로 변경하여 재시작하면 물리적인 LED/Beep가 실행됩니다. 이 기능은 서버 시작 시 환경변수를 읽으므로 `.env`만 수정해서는 활성 상태가 바뀌지 않습니다.

**비상 해제:** `AUTO_WARNING_ENABLED=false`로 되돌린 뒤 GPU 서버를 안전하게 재시작합니다. 이미 Pi에서 진행 중인 10초 경고 패턴은 별도의 `warning`/수동 LED 명령으로 변경하지 않는 이상 진행을 마칠 수 있습니다.

## 설치 및 테스트

GPU 서버 WSL 프로젝트 경로: `/mnt/j/박준영_졸작/git clone/dabom_capstone`

```bash
cd "/mnt/j/박준영_졸작/git clone/dabom_capstone"
git apply --check ~/dabom_auto_warning_phase3a_20261110.patch
git apply ~/dabom_auto_warning_phase3a_20261110.patch
python3 -m py_compile server/automatic_warning.py server/app.py
PYTHONPATH=. PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q tests/test_automatic_warning.py
```

기존 미커밋 파일/브랜치/ROS 프로세스를 초기화하지 마세요. 패치는 `server/app.py`에 소규모 훅을 추가하고 신규 `server/automatic_warning.py`, 테스트·문서만 추가합니다. Pi 코드는 이번 단계에 **복사/재시작할 필요가 없습니다.** `start_gpu_server.sh` 재실행은 ROS·오도메트리·Nav2를 정리할 수 있으므로 로봇이 정지했고 현재 Mapping·주행 작업이 없을 때만 진행합니다.

## 한계

- AI 자체가 오탐하면 2회 연속 검증을 통과한 뒤에도 물리 경고가 발생할 수 있습니다.
- `AUTO_WARNING_ENABLED=false` 또는 `DRY_RUN=true`면 하드웨어 명령을 보내지 않습니다.
- 자동 경고와 관리자의 수동 경고가 같은 시점에 오면 기존 Pi 코드는 이전 경고 패턴을 재시작할 수 있습니다. 수동-자동 경고 간 우선순위 제어는 별도 후속 작업입니다.
- 현재 단계는 LED + Pico 경고음 자동 연동만 구현하며 USB 스피커 TTS는 아직 구현하지 않습니다.
