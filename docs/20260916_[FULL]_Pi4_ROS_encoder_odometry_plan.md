# Pi4 ROS Encoder / Odometry 이전 계획

## 목적
GPU Server에 있던 ROS encoder/odometry 처리를 Raspberry Pi 4로 이전한다.

## 목표 구조
Pico W -> Pi4 robot_command_client -> /wheel_ticks -> /odom -> odom->base_link
Pi4 encoder telemetry -> GPU Server WebSocket 경로는 기존대로 유지한다.

## 변경 범위
- Pi4에서 encoder snapshot을 /wheel_ticks로 발행
- 기존 GPU WebSocket telemetry 유지
- 기존 motor control / E-stop 동작 유지
- wheel_odometry.py 재사용
- systemd 자동 시작은 추가하지 않음

## 검증
- /wheel_ticks 지속 발행
- /odom 지속 발행
- odom -> base_link TF 확인
- GPU encoder telemetry 유지
- 웹 수동 주행 및 E-stop 정상
