# Autonomous Patrol Bot

Raspberry Pi 기반 이동 로봇과 GPU 서버를 연결한 자율주행 AI 방범 로봇 프로젝트입니다.

- **Raspberry Pi**: 카메라, LiDAR, Pico W UART, 바퀴 엔코더, 센서/영상 전송
- **GPU Server**: FastAPI, 웹 대시보드, AI perception, wheel odometry, ROS 2 Mapping/Localization/Nav2
- **ROS 2**: Humble
- **Target Pi**: Raspberry Pi 3 Model B, Ubuntu Server 22.04 arm64

## Runtime entry points

최종 runtime shell script는 프로젝트 root의 두 파일만 사용합니다.

```text
dabom_capstone/
├─ start_gpu_server.sh
└─ start_pi_stack.sh
```

기존의 개별 Camera/LiDAR/Robot/Pi3B setup/validation shell script는 사용하지 않습니다.

두 스크립트는 실행 시 동일 역할의 기존 프로세스를 먼저 정리하고 새 stack을 시작합니다. 실행 순서는 상관없으며 상대 장치가 아직 켜지지 않았으면 `WAITING` 상태로 재연결을 기다립니다.

## 1. 공통 준비

```bash
git clone https://github.com/jcy9066/dabom_capstone.git
cd dabom_capstone
cp .env.example .env
```

`.env`의 실제 운영 값을 설정해야 합니다.

### Raspberry Pi prerequisites

Pi에는 다음 환경이 미리 준비되어 있어야 합니다.

- Ubuntu Server 22.04 arm64
- ROS 2 Humble
- `rpicam-vid`
- RPLIDAR ROS driver
- Python dependencies from `raspberry/requirements.txt`
- Pico W UART device
- LiDAR serial device
- `dialout`/camera device 접근 권한
- `patrol_navigation` ROS package build

```bash
python3 -m pip install -r raspberry/requirements.txt

source /opt/ros/humble/setup.bash
cd navigation/ros
colcon build --symlink-install --packages-select patrol_navigation
cd ../..
```

기본 Pico UART 구성은 다음과 같습니다.

```text
Pi GPIO14 / TXD (physical pin 8)  -> Pico W RX
Pi GPIO15 / RXD (physical pin 10) <- Pico W TX
Pi GND                            <-> Pico W GND

MOTOR_SERIAL_PORT=/dev/serial0
MOTOR_SERIAL_BAUDRATE=115200
```

Pico와 LiDAR는 같은 serial device를 사용할 수 없습니다.

### GPU Server prerequisites

```bash
python3 -m pip install -r requirements.txt

source /opt/ros/humble/setup.bash
cd navigation/ros
colcon build --symlink-install --packages-select patrol_navigation
cd ../..
```

## 2. GPU Server 실행

프로젝트 root에서 실행합니다.

```bash
cd ~/dabom_capstone
bash start_gpu_server.sh
```

이 스크립트는 다음을 수행합니다.

1. `.env`, ROS 2, Python package, ROS build 상태 확인
2. 중복 실행 방지용 restart lock 획득
3. 기존 동일 역할 프로세스 종료
   - 기존 GPU launcher
   - `uvicorn server.app:app`
   - `server/wheel_odometry.py`
   - 남아 있는 Mapping/Localization/Nav2 launch
4. FastAPI 시작
5. 실제 HTTP `/get_status` 응답 확인
6. wheel odometry 시작
7. 실제 `/odom` publisher 확인
8. Pi 연결 상태 출력

정상 예:

```text
[gpu-stack] READY: GPU local stack is running
[gpu-stack] WAITING: Raspberry Pi stack may be started before or after this script
```

Pi가 이미 연결되어 있으면 `CONNECTED`가 출력됩니다.

## 3. Raspberry Pi 실행

프로젝트 root에서 실행합니다.

```bash
cd ~/dabom_capstone
bash start_pi_stack.sh
```

이 스크립트는 다음을 수행합니다.

1. `.env`, ROS 2, Python package, Camera/LiDAR/UART device 확인
2. Pico와 LiDAR serial port 충돌 검사
3. 중복 실행 방지용 restart lock 획득
4. 기존 동일 역할 프로세스 종료
   - 기존 Pi launcher
   - `robot_command_client.py`
   - `lidar_scan_sender.py`
   - LiDAR ROS launch
   - `rpicam-vid`
   - Camera upload process
5. 기존 `dabom-command.service`가 실행 중이면 정지
6. Pico UART `PING -> OK,PONG` 확인
7. Camera hardware 탐지 확인
8. LiDAR driver 시작 후 실제 `LaserScan` 1회 수신 확인
9. LiDAR WebSocket sender 시작
10. Robot command client 시작
11. H.264 Camera stream 시작
12. GPU 연결 상태 출력

Robot client 종료 시에는 Pico에 안전 정지 명령을 보낼 수 있도록 graceful shutdown을 우선합니다.

정상 예:

```text
[pi-stack] Pico UART READY
[pi-stack] Camera hardware READY
[pi-stack] LiDAR scan READY
[pi-stack] READY: Pi local stack is running
[pi-stack] WAITING: GPU server may be started before or after this script
```

GPU가 이미 실행 중이면 `CONNECTED`가 출력됩니다.

## 4. 상태 의미

```text
READY      로컬 필수 장치와 프로세스가 정상
WAITING    상대 장치가 아직 연결되지 않음
CONNECTED  상대 장치가 현재 도달 가능
ERROR      필수 장치/프로세스/환경 문제로 시작 실패
WARN       실행 중 구성 요소 종료 또는 재연결 상황
```

상대 장치가 꺼져 있다는 이유만으로 로컬 stack을 실패 처리하지 않습니다.

## 5. Mapping / Navigation

`start_gpu_server.sh`는 Mapping이나 Nav2를 자동 시작하지 않습니다.

GPU runtime 시작 시 이전 실행에서 남은 navigation process는 정리하지만, 실제 Mapping 또는 Driving mode는 웹 대시보드에서 선택할 때 시작합니다.

```text
GPU runtime
├─ FastAPI
├─ wheel odometry
└─ navigation mode
   ├─ Mapping      -> 필요할 때 시작
   └─ Driving/Nav2 -> 필요할 때 시작
```

Mapping과 Driving은 동시에 실행하지 않습니다.

## 6. 데이터 흐름

```text
Raspberry Pi                            GPU Server
────────────────────                    ─────────────────────
Pico W
  │ encoder / control
  ▼
robot_command_client.py ──────────────► FastAPI / WebSocket
                                           │
                                           └─ /wheel_ticks
                                                │
                                                ▼
                                           wheel_odometry
                                                │
                                                └─ /odom + TF

LiDAR driver
  │
  └─ /scan
       │
       └─ lidar_scan_sender.py ───────► LiDAR WebSocket
                                           │
                                           └─ GPU ROS /scan

OV5647 / rpicam-vid
  │
  └─ H.264 ───────────────────────────► /stream/h264
                                           │
                                           └─ Dashboard / AI inference
```

## 7. 실행 확인

GPU Server:

```bash
curl http://127.0.0.1:21063/get_status
curl http://127.0.0.1:21063/api/stream_status
curl http://127.0.0.1:21063/api/lidar/bridge
curl http://127.0.0.1:21063/api/encoder/bridge
curl http://127.0.0.1:21063/api/navigation/status
```

ROS 2:

```bash
source /opt/ros/humble/setup.bash
source ~/dabom_capstone/navigation/ros/install/setup.bash

ros2 topic hz /scan
ros2 topic hz /wheel_ticks
ros2 topic hz /odom
ros2 run tf2_ros tf2_echo odom base_link
```

통합 정상 기준:

```text
Camera      Dashboard 실시간 영상 수신
LiDAR       /scan 지속 발행
Encoder     /wheel_ticks 지속 발행
Odometry    /odom 및 odom -> base_link TF 발행
Robot       /ws/robot/<ROBOT_ID> 연결
Navigation  Mapping 또는 Driving mode 요청 시 해당 stack만 실행
```

## 8. 주요 디렉터리

```text
dabom_capstone/
├─ start_gpu_server.sh
├─ start_pi_stack.sh
├─ server/
│  ├─ app.py
│  └─ wheel_odometry.py
├─ raspberry/
│  ├─ robot_command_client.py
│  ├─ lidar_scan_sender.py
│  ├─ encoder_ros_publisher.py
│  ├─ controllers/
│  └─ pico_w_sdk/
├─ navigation/
│  └─ ros/patrol_navigation/
├─ perception/
├─ frontend/
├─ tests/
├─ .agents/
└─ .codex/
```

## Development validation

변경 영역별 검증 도구는 `.agents/skills/` 아래에 있습니다.

- `validate-server`
- `validate-dashboard`
- `validate-navigation`
- `validate-raspberry`
- `review-change`

하드웨어가 연결되지 않은 개발 환경에서는 shell syntax와 정적 코드 검증까지만 수행하고 실제 Camera/LiDAR/Pico 동작을 검증했다고 간주하지 않습니다.
