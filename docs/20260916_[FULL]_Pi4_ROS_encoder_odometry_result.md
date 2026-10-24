# Pi4 ROS Encoder / Odometry 이전 결과

## 변경 결과
- Pico W encoder snapshot을 Pi4 ROS2 /wheel_ticks로 직접 발행하도록 추가
- 기존 Pi -> GPU WebSocket encoder telemetry 경로 유지
- 기존 motor control / E-stop 경로 유지
- 기존 server/wheel_odometry.py를 Pi4에서 실행하여 /odom 및 TF 생성

## 실기기 검증
- /wheel_ticks: 약 19.15 Hz 지속 발행 PASS
- /odom: 약 20.00 Hz 지속 발행 PASS
- odom -> base_link TF: 지속 발행 PASS
- GPU WebSocket 연결 및 encoder telemetry PASS
- 웹 수동 주행 PASS
- E-stop / Resume PASS
- 모터 지속 구동 중 vcgencmd get_throttled=0x0 유지 PASS

## ROS 환경
- ROS_DOMAIN_ID=27
- ROS_LOCALHOST_ONLY=1
- robot_command_client와 wheel_odometry에 동일 ROS 환경 적용 필요

## 정적 검증
- git diff --check PASS
- Python py_compile PASS
- validate_uart_protocol.py: errors=0, warnings=1
- warning은 legacy raspberry/pico_w/main.py 관련 기존 경고
- start_robot_command_client.sh bash -n PASS
- start_camera_stream.sh bash -n PASS
- start_lidar_sender.sh bash -n PASS

## 참고
- AGENTS.md에 명시된 setup_pi3b.sh 및 validate_pi3b.sh는 현재 저장소에 존재하지 않아 실행하지 못함
- systemd 자동 시작은 추가하지 않음
