# 프로젝트 명세서: 자율주행 AI 방범 로봇 (Autonomous Patrol Bot)

## 1. 프로젝트 개요
- **프로젝트명:** 자율주행 AI 방범 로봇
- **목표:** 자율주행과 컴퓨터 비전을 결합하여 특정 구역을 순찰하고 이상 상황을 탐지하는 지능형 모바일 로봇 시스템 구축

## 2. 시스템 아키텍처

### 2.1. Raspberry Pi 3B
- **보드:** Raspberry Pi 3 Model B 1GB / Ubuntu Server 22.04 arm64
- **역할:** 실시간 센서 수집과 저수준 장치 연결
  - OV5647 카메라 H.264 스트리밍
  - RPLIDAR `/scan` 수집
  - Pico W UART를 통한 모터·엔코더 통신
  - Robot/LiDAR/Encoder/Camera 데이터를 GPU Server로 전송
- **ROS 2:** Humble
- **DDS 범위:** `ROS_LOCALHOST_ONLY=1`
  - Pi↔GPU 센서 전달은 ROS DDS가 아니라 WebSocket/HTTP를 사용

### 2.2. Pico W / 구동부
- Pico W가 MDD10A 모터 제어와 엔코더 수집을 담당
- Raspberry Pi와 UART 115200 bps로 통신
- 통신 단절 및 command timeout 시 안전 정지

### 2.3. GPU Server
- FastAPI 관제 서버 및 웹 대시보드
- AI perception
- Encoder WebSocket → ROS `/wheel_ticks`
- Wheel Odometry → `/odom` + `odom -> base_link` TF
- LiDAR WebSocket → ROS `/scan`
- SLAM Toolbox Mapping
- AMCL Localization
- Navigation2 Path Planning / Navigation
- DB 및 이벤트/조치 기록

### 2.4. 최종 Runtime 진입점
```text
GPU Server : start_gpu_server.sh
Raspberry Pi: start_pi_stack.sh
```

개별 legacy launcher는 최종 운용 경로로 사용하지 않는다. 두 root launcher가 동일 역할 프로세스 중복 제거, preflight, readiness와 종료 처리를 담당한다.

## 3. 핵심 기능

### 3.1. 자율주행 및 Mapping
- 2D LiDAR 기반 실시간 Mapping
- 저장 지도 관리
- AMCL 기반 Localization
- Nav2 기반 path planning 및 goal navigation
- 장애물 회피
- Mapping과 Driving runtime의 상호배타 실행

### 3.2. 주행 안전
- 수동/자동 모드 분리
- Emergency Stop
- Nav2 command timeout 정지
- Navigation watchdog
- Encoder stop 검증
- `MOTOR_OUTPUT_ENABLED=false` 상태에서 dry-run 검증 후 실제 모터 출력 활성화
- 실제 encoder odometry를 사용하는 최종 runtime에서는 fake odom을 사용하지 않음

### 3.3. Vision AI 이상 탐지
- 카메라 스트리밍 실시간 분석
- 사람/침입 이벤트 탐지
- Pose/행동 분석 및 폭력·이상 행동 판정
- 위험/경고 상태 생성
- 관리자 알림 흐름과 이벤트 기록 연동

### 3.4. Web Dashboard
- 실시간 영상
- 로봇 상태 및 센서 상태
- Mapping / Driving 모드 전환
- 수동 주행
- 지도 저장·선택·초기 위치 지정·goal 지정
- Emergency Stop
- 순찰 기록, 관리자 조치 기록, 기기 상태 로그
- 여러 dashboard session 간 control lease 기반 충돌 방지

## 4. 데이터 흐름

```text
Raspberry Pi                               GPU Server
────────────────────                       ────────────────────
Pico W encoder/control
        │
        ▼
robot_command_client.py ── WebSocket ───► Encoder ROS Bridge
                                              │
                                              ▼
                                         /wheel_ticks
                                              │
                                              ▼
                                       wheel_odometry
                                              │
                                       /odom + odom TF

RPLIDAR → /scan
        │
        ▼
lidar_scan_sender.py ───── WebSocket ───► LiDAR ROS Bridge
                                              │
                                              ▼
                                            /scan
                                              │
                           ┌──────────────────┴─────────────────┐
                           ▼                                    ▼
                    SLAM Toolbox                         AMCL / Nav2

OV5647 → H.264 ───────────── HTTP ───────► FastAPI / AI / Dashboard
```

## 5. Runtime / ROS 소유권
- `start_gpu_server.sh`
  - FastAPI와 wheel odometry를 소유
  - wheel odometry가 비정상 종료되면 supervisor가 재시작
  - stale Mapping/Driving/legacy standalone Map Bridge 정리
- `start_pi_stack.sh`
  - Robot Client, LiDAR Driver/Sender, Camera stream을 소유
  - Pico PING/STOP, Camera detection, 실제 LaserScan 수신 확인 후 READY
- Mapping/Driving launch가 `map_bridge`를 소유
  - standalone `map_bridge`와 동시에 실행하지 않음
- FastAPI 내부 LiDAR/Encoder/Navigation ROS node는 같은 process에서 rclpy context를 공유할 수 있으므로 개별 bridge 종료가 전역 ROS context를 종료하지 않음

## 6. 통합 정상 기준
```text
Camera      GPU/Dashboard에서 지속 수신
LiDAR       GPU /scan 지속 발행
Encoder     GPU /wheel_ticks 지속 발행
Odometry    /odom + odom -> base_link TF 지속 발행
Mapping     map 생성 및 저장 가능
Localization AMCL pose + map -> odom TF 정상
Nav2        path planning / dry-run command 정상
Control     Manual/Auto/E-stop 상태 일관성 유지
Logging     상태/이벤트/관리자 조치 기록 정상
```

## 7. 실기기 검증 전 소프트웨어 완료 기준
- root launcher 2개만 최종 runtime 진입점으로 사용
- ROS bridge lifecycle 간 상호 간섭 제거
- wheel odometry / map bridge process ownership 단일화
- `/wheel_ticks`와 `/odom`은 publisher 존재뿐 아니라 실제 데이터 흐름도 검증
- Pi/GPU 간 cross-host DDS 차단
- Encoder/LiDAR/Camera freshness 확인
- Mapping/Driving 상호배타 및 stale process 정리
- Nav2 dry-run / motor output gate / watchdog / E-stop 코드 검토 완료
- 가능한 unit/static test 완료 후 실제 H/W 검증으로 이동

## 8. DB 핵심 구조
- `users`: 관리자 계정
- `system_status`: 로봇/네트워크 상태 기록
- `event_log`: AI 및 시스템 이벤트 기록
- `action_log`: 관리자/시스템 조치 기록

세부 ERD는 `ERD.png`를 기준으로 한다.
