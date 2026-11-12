# DABOM 음성 관제 Phase 2 — 메인 대시보드 통합·USB 출력 원격 제어

## 검증된 이전 상태 (2026-11-10)

- 웹 청취 음량: **28%** (브라우저 실제 확인)
- Raspberry Pi USB 스피커: **50%** (Card 2 / Jieli, `amixer` 확인)
- Raspberry Pi 마이크: USB JMTek Card 1, Mic 50%, AGC on, 실제 입출력 검증 성공
- GPU/로봇 양방향 PCM WebSocket 통화 모두 실제 사용자 확인 성공

## Phase 2 변경

- `/main` 우하단에 접을 수 있는 독립 USB 음성 관제 창. 기존 Pico W Beep 볼륨과 독립.
- 기존 `/audio/intercom-test` 페이지 유지. 메인 및 시험 페이지는 **같은 오디오 세션**을 이용하므로 동시에 연결 불가.
- 브라우저 청취 기본 음량 28%, `localStorage`에 변경값 기억 (송출 중인 오디오를 서버에 저장하지 않음).
- 로봇 USB 스피커 볼륨은 연결 시 **실제 Pi ALSA 값 조회**하여 표시. 최초 기본 표시 50%는 장치 확인 전에는 `확인 중`으로 표현.
- 슬라이더 변경시 `set_speaker_volume` WS 제어 → Pi `amixer` → Pi의 **실제 적용 음량 ACK** → UI 반영.
- Pi 볼륨은 시작 시 자동으로 50% 설정하지 않음. 사용자가 마지막에 맞춘 값을 유지.
- 별도의 음성 엔진, 카메라/주행/ROS/Pico 시스템은 변경하지 않음.

## 배포 전

- GPU 프로젝트: `/mnt/j/박준영_졸작/git clone/dabom_capstone`
- Pi 프로젝트: `/home/dabom_user/dabom_capstone`
- Phase 1 패치가 이미 적용된 작업 트리 기준입니다. `git apply --check`를 먼저 수행합니다.
- GPU 측 `server/app.py`는 Phase 1에 등록된 오디오 라우트 그대로 사용합니다.
- Pi 측에 변경된 `raspberry/audio_intercom_client.py`만 갱신해야 USB 스피커 원격 볼륨 제어가 활성화됩니다.
- GPU 서버 재시작은 ROS 프로세스를 중단할 수 있으므로 로봇의 모터가 정지한 안전한 상태에서 시행합니다.
- Pi 오디오 클라이언트만 `dabom-audio` tmux에서 안전하게 재시작하고 기존 `dabom-server`는 유지합니다.
- 두 오디오 클라이언트의 중복 실행을 금지합니다.

## 검증

1. GPU WSL: `PYTHONPATH=. PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q tests/test_audio_intercom.py tests/test_audio_devices.py tests/test_audio_volume.py` (9개 테스트)
2. Pi: `python3 -m py_compile raspberry/audio_intercom_client.py`
3. Pi: `python3 raspberry/audio_intercom_client.py --check`
4. GPU FastAPI와 Pi 오디오 클라이언트를 각각 안전 재시작.
5. 브라우저의 기존 `/audio/intercom-test` 탭 연결 종료 후 `/main` 오디오 관제 창에서 연결.
6. 최초 웹 청취값 28%와 USB 스피커 실제 50%를 확인. 50% 그대로 유지하면서 통화 확인.
7. USB 음량을 바꾸면 Pi `amixer -c <actual_card> sget PCM`과 같은 값이 나오는지 확인.
8. 웹과 Pi 음성 각각 송수신, 마이크 권한, 연결 종료·재접속, 카메라/센서 회귀 확인.

## 유의 사항

- 임의의 TTS / AI 경고 연동은 아직 하지 않았습니다. 통화 중 경고 우선순위는 다음 단계에서 설계합니다.
- 브라우저 마이크는 `localhost` 또는 HTTPS 보안 컨텍스트가 필요합니다.
- 기존 Pico W Beep 볼륨 슬라이더는 건드리지 않았습니다.
