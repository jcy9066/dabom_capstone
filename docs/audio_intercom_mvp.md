# DABOM 실시간 오디오 관제 MVP (분리 실행)

## 적용 범위

- **기준 브랜치:** `integration/dabom-final-20261109` (`005cbebc`)
- 본 구현은 기존 카메라·LiDAR·모터·SLAM·대시보드 동작과 분리되어 있습니다.
- GPU FastAPI에 **별도의 인증된 시험 페이지** `/audio/intercom-test`와 음성 WebSocket 라우트만 추가합니다.
- Pi 오디오 프로그램은 `start_pi_stack.sh`에 자동 연결하지 않으며, 별도 tmux 창에서 수동 실행합니다.
- 오디오 데이터는 녹음 파일로 저장하지 않습니다.

## 보안

- 웹 오디오 WebSocket: 기존 로그인 세션 + 동일 출처 Origin 필수.
- Pi 오디오 WebSocket: 기존 `ROBOT_CONTROL_TOKEN` 필수. 웹으로 토큰 전달 금지.
- 한 번에 대시보드 1개, Pi 오디오 1개만 허용.
- 브라우저를 닫으면 음성 수집과 송출이 자동으로 중단됩니다.
- 네트워크 보안을 위해 Tailscale 등의 신뢰할 수 있는 경로 사용. 웹 마이크는 HTTPS 또는 localhost 등 보안 컨텍스트에서만 동작합니다.

## 초기 시험 절차

1. **Pi** (`~/dabom_capstone`): `python3 raspberry/audio_intercom_client.py --check`로 장치 확인.
2. **GPU (WSL)**: 오디오 통합 브랜치에 코드를 배치하고 테스트 후 GPU 서버를 재시작합니다. 기존 GPU 서버의 재시작은 ROS 제어 프로세스도 종료할 수 있으므로 안전 확인 후 수행합니다.
3. **Pi** (`~/dabom_capstone`): `python3 raspberry/audio_intercom_client.py`를 별도 tmux 창에서 실행합니다. 기존 Pi 스택은 그대로 유지합니다.
4. **GPU 호스트 브라우저**: DABOM 로그인 후 `http://localhost:21063/audio/intercom-test` (GPU 호스트에서 접속 시) 또는 안전한 HTTPS 주소로 접속합니다.
5. 오디오 서버 연결 → 현장 청취 시작 → 웹에서 목소리 확인.
6. '누르고 현장에 말하기' 버튼을 누르고 관리자가 말하면 로봇 USB 스피커에서 출력됩니다.
7. 테스트 종료 시 버튼으로 연결 종료. Pi 측 오디오 프로그램은 Ctrl+C로 중단합니다.

## 오디오 형식과 지연

- 16kHz / Mono / PCM signed 16-bit little endian / 20ms 640-byte 프레임.
- 서버는 오디오 프레임을 저장하지 않고 인증된 양쪽 연결 간 전달만 수행합니다.
- 서버 송신 타임아웃 300ms 및 웹 전송 버퍼 제한으로 과도한 큐 누적 방지.
- **현재 목표:** 500ms 미만. 실제 값은 기기 테스트 및 계측 전까지 미검증입니다.

## 의도적으로 제외한 기능

- 기존 Pico Beep/볼륨 슬라이더 변경, Pi 오디오 볼륨 원격 제어, TTS 자동 경고, AI 이벤트 연동, 녹음 보관, 기존 메인 대시보드 UI 내장.
- 해당 기능은 MVP의 양방향 음성 통신 검증 이후 별도 작업으로 통합합니다.
