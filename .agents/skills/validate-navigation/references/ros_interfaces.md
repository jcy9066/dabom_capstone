# ROS 2 Navigation Interface Reference

## 1. 목적

이 문서는 `validate-navigation` Skill이 `patrol_navigation` 패키지를 검증할 때 사용하는 ROS 2 interface 기준을 정의한다.

검증 대상은 다음과 같다.

- ROS 2 package 및 executable
- launch file
- node
- topic
- message type
- TF tree
- Nav2 action
- lifecycle state
- mapping mode
- localization mode
- localization + Nav2 mode
- LiDAR interface
- odometry interface
- map 및 AMCL interface
- `/cmd_vel_nav_dry_run` 안전 조건
- `map_bridge`와 FastAPI server 간 데이터 전달 구조
- Nav2 `Twist`와 `nav2_command_bridge` 간 연결 구조

현재 기준 package:

```text
navigation/ros/patrol_navigation
```

현재 ROS 배포 기준:

```text
Ubuntu 22.04
ROS 2 Humble
```

---

## 2. Package 기본 정보

ROS package name:

```text
patrol_navigation
```

build type:

```text
ament_python
```

현재 Python executable:

```text
map_bridge
nav2_command_bridge
```

source:

```text
patrol_navigation.map_bridge:main
patrol_navigation.nav2_command_bridge:main
```

검증 시 최소한 다음 명령이 성공해야 한다.

```bash
ros2 pkg prefix patrol_navigation
ros2 pkg executables patrol_navigation
```

기대 executable:

```text
patrol_navigation map_bridge
patrol_navigation nav2_command_bridge
```

---

## 3. 주요 launch file

필수 launch file:

```text
lidar.launch.py
mapping.launch.py
localization.launch.py
navigation.launch.py
```

역할:

| Launch file | 역할 |
|---|---|
| `lidar.launch.py` | RPLIDAR driver + `base_link -> laser` TF |
| `mapping.launch.py` | LiDAR + SLAM Toolbox + map bridge |
| `localization.launch.py` | 저장 지도 + AMCL + LiDAR + map bridge |
| `navigation.launch.py` | localization + Nav2 + dry-run velocity path |

모든 launch file은 다음 함수가 있어야 한다.

```python
generate_launch_description()
```

설치 후 다음 형태의 검증이 가능해야 한다.

```bash
ros2 launch patrol_navigation lidar.launch.py --show-args
ros2 launch patrol_navigation mapping.launch.py --show-args
ros2 launch patrol_navigation localization.launch.py --show-args
ros2 launch patrol_navigation navigation.launch.py --show-args
```

---

## 4. Navigation mode

현재 프로젝트에서 navigation 관련 mode는 다음과 같다.

```text
scan_only
mapping
localization
localization_nav2
```

단, `map_bridge` 자체가 허용하는 값은 다음 세 개다.

```text
mapping
localization
localization_nav2
```

`scan_only`는 server 측 navigation 상태에서 사용할 수 있지만 `map_bridge`의 `navigation_mode` 값으로는 사용하지 않는다.

### Mode 의미

| Mode | 의미 |
|---|---|
| `scan_only` | LiDAR scan만 사용하는 상태 |
| `mapping` | SLAM Toolbox로 지도 생성 |
| `localization` | 저장 지도 + AMCL 위치 추정 |
| `localization_nav2` | 저장 지도 + AMCL + Nav2 |

---

## 5. Canonical TF tree

현재 navigation의 기준 TF tree:

```text
map
└── odom
    └── base_link
        └── laser
```

각 transform의 책임:

| Transform | 생성 주체 |
|---|---|
| `map -> odom` | SLAM Toolbox 또는 AMCL |
| `odom -> base_link` | 실제 wheel odometry 또는 임시 static TF |
| `base_link -> laser` | `static_transform_publisher` |

### Mapping mode

```text
slam_toolbox
    -> map -> odom

temporary_odom_to_base_tf 또는 실제 odometry
    -> odom -> base_link

base_to_laser_tf
    -> base_link -> laser
```

### Localization mode

```text
AMCL
    -> map -> odom

temporary_odom_to_base_tf 또는 실제 odometry
    -> odom -> base_link

base_to_laser_tf
    -> base_link -> laser
```

---

## 6. Frame 이름

기준 frame:

```text
map
odom
base_link
laser
```

AMCL 설정:

```text
global_frame_id: map
odom_frame_id: odom
base_frame_id: base_link
```

LiDAR scan frame:

```text
laser
```

`map_bridge`의 기본 pose lookup:

```text
map -> base_link
```

즉, 다음 TF chain이 완성되어야 한다.

```text
map -> odom -> base_link
```

---

## 7. TF runtime 검증

기본 TF 확인 명령:

```bash
ros2 run tf2_ros tf2_echo map odom
ros2 run tf2_ros tf2_echo odom base_link
ros2 run tf2_ros tf2_echo base_link laser
ros2 run tf2_ros tf2_echo map base_link
```

### 실제 odometry 연결 전

다음 node가 임시 TF를 생성할 수 있다.

```text
/temporary_odom_to_base_tf
```

이때:

```text
odom -> base_link
```

은 static transform이다.

### 실제 odometry 연결 후

반드시:

```text
start_fake_odom:=false
```

로 실행해야 한다.

실제 odometry와 임시 static TF가 동시에 같은 transform을 publish하면 오류로 처리한다.

---

## 8. LiDAR interface

기본 node:

```text
/rplidar_node
```

기본 topic:

```text
/scan
```

message type:

```text
sensor_msgs/msg/LaserScan
```

기본 frame:

```text
laser
```

기본 serial 설정:

```text
/dev/ttyUSB0
115200 baud
```

### LiDAR static TF node

```text
/base_to_laser_tf
```

transform:

```text
base_link -> laser
```

현재 기본 위치:

```text
x = 0.0
y = 0.0
z = 0.12
roll = 0.0
pitch = 0.0
yaw = 0.0
```

---

## 9. LiDAR 데이터 경로

현재 최종 runtime에서 LiDAR 하드웨어는 Raspberry Pi가 담당한다.

```text
Raspberry Pi
  -> RPLIDAR
  -> lidar_scan_sender.py
  -> WebSocket
  -> GPU FastAPI
  -> lidar_websocket_bridge
  -> /scan
```

`localization.launch.py`와 `navigation.launch.py`는 LiDAR driver를 직접 실행하지 않는다.

두 launch는 GPU의 `lidar_websocket_bridge`가 이미 publish하고 있는 `/scan`을 사용한다.

따라서 localization/DRIVING 경로에는 다음과 같은 직접 LiDAR 실행 설정이 존재하면 안 된다.

```text
sllidar_ros2
start_lidar
serial_port
serial_baudrate
driver_package
driver_executable
lidar.launch.py include
```

반면 `mapping.launch.py`의 선택적 직접 LiDAR 실행 경로와 `lidar.launch.py`는 별도로 유지한다.

`lidar.launch.py`의 기본 driver는 다음과 같다.

```text
driver_package = rplidar_ros
driver_executable = rplidar_composition
```

최종 runtime의 localization/Navigation에서는 기존 `/scan`이 정상 publish되고 있는지가 기준이다.

---

## 10. `/scan` QoS

`map_bridge`의 `LaserScan` subscription은 다음 QoS를 사용해야 한다.

```python
qos_profile_sensor_data
```

이는 sensor data topic 특성에 맞는 ROS 2 QoS를 사용하기 위한 것이다.

`LaserScan` subscription에 일반 integer depth만 사용하도록 변경되면 검토 대상으로 처리한다.

---

## 11. Mapping mode 구조

실행:

```bash
ros2 launch patrol_navigation mapping.launch.py
```

기본 구조:

```text
RPLIDAR
    -> /scan

slam_toolbox
    <- /scan
    -> /map
    -> map -> odom TF

temporary odometry 또는 실제 odometry
    -> odom -> base_link TF

base_to_laser_tf
    -> base_link -> laser TF

map_bridge
    <- /map
    <- /scan
    <- map -> base_link TF
    -> FastAPI server
```

---

## 12. Mapping mode 주요 node

기대 node:

```text
/rplidar_node
/slam_toolbox
/map_bridge
/base_to_laser_tf
```

`start_fake_odom:=true`이면 추가:

```text
/temporary_odom_to_base_tf
```

`start_rviz:=true`이면 추가:

```text
/mapping_rviz
```

node 이름은 launch 또는 외부 package 버전에 따라 일부 달라질 수 있으므로, 외부 package node는 기능과 topic도 함께 확인한다.

---

## 13. Mapping mode 주요 topic

필수:

```text
/scan
/map
/tf
/tf_static
```

일반적인 message type:

| Topic | Type |
|---|---|
| `/scan` | `sensor_msgs/msg/LaserScan` |
| `/map` | `nav_msgs/msg/OccupancyGrid` |
| `/tf` | `tf2_msgs/msg/TFMessage` |
| `/tf_static` | `tf2_msgs/msg/TFMessage` |

### `/map`

SLAM Toolbox가 publish한다.

기본 message:

```text
nav_msgs/msg/OccupancyGrid
```

필수 map metadata:

```text
resolution
width
height
origin
data
```

---

## 14. Mapping mode lifecycle

SLAM Toolbox는 공식 online async launch를 사용한다.

의도한 상태:

```text
configured
-> active
```

mapping runtime 검사에서 SLAM Toolbox가 lifecycle node로 노출되는 경우 최종적으로 `active` 상태여야 한다.

---

## 15. Mapping mode map_bridge 설정

현재 mapping launch에서 전달하는 주요 값:

```text
map_topic = /map
scan_topic = /scan

pose_parent_frame = map
pose_child_frame = base_link

map_publish_period_sec = 2.0
pose_publish_period_sec = 0.5
scan_publish_period_sec = 0.2

send_map = true
send_pose = true
send_scan = true
```

mapping launch에서는 `map_bridge`가 SLAM 초기화 시간을 확보하도록 delay 후 시작될 수 있다.

현재 기준 delay:

```text
8 seconds
```

정확한 delay 자체보다 최종적으로 `/map`, `/scan`, TF를 정상 수신하는지가 중요하다.

---

## 16. Localization mode 구조

실행:

```bash
ros2 launch patrol_navigation localization.launch.py
```

기본 구조:

```text
map_server
    -> /map

RPLIDAR
    -> /scan

AMCL
    <- /map
    <- /scan
    <- odometry
    -> /amcl_pose
    -> map -> odom TF

temporary odometry 또는 실제 odometry
    -> odom -> base_link TF

map_bridge
    <- /map
    <- /scan
    <- map -> base_link TF
```

---

## 17. Localization mode 주요 node

기대 node:

```text
/map_server
/amcl
/lifecycle_manager_localization
/map_bridge
```

LiDAR 사용 시:

```text
/rplidar_node
/base_to_laser_tf
```

fake odometry 사용 시:

```text
/temporary_odom_to_base_tf
```

---

## 18. Localization mode 주요 topic

필수 또는 핵심:

```text
/map
/scan
/amcl_pose
/tf
/tf_static
```

message type:

| Topic | Type |
|---|---|
| `/map` | `nav_msgs/msg/OccupancyGrid` |
| `/scan` | `sensor_msgs/msg/LaserScan` |
| `/amcl_pose` | `geometry_msgs/msg/PoseWithCovarianceStamped` |
| `/tf` | `tf2_msgs/msg/TFMessage` |
| `/tf_static` | `tf2_msgs/msg/TFMessage` |

실제 odometry가 연결된 경우 추가로 기대:

```text
/odom
```

일반적인 type:

```text
nav_msgs/msg/Odometry
```

---

## 19. AMCL interface

node:

```text
/amcl
```

입력:

```text
/map
/scan
odom -> base_link TF
```

출력:

```text
/amcl_pose
map -> odom TF
```

현재 frame 설정:

```text
global_frame_id = map
odom_frame_id = odom
base_frame_id = base_link
scan_topic = scan
```

motion model:

```text
nav2_amcl::DifferentialMotionModel
```

현재 초기 pose 기본값:

```text
x = 0.0
y = 0.0
z = 0.0
yaw = 0.0
```

현재 설정:

```text
set_initial_pose = true
always_reset_initial_pose = false
```

---

## 20. Localization lifecycle

`lifecycle_manager_localization`이 관리하는 node:

```text
map_server
amcl
```

기대 최종 상태:

```text
/map_server -> active
/amcl -> active
```

검증 예:

```bash
ros2 lifecycle get /map_server
ros2 lifecycle get /amcl
```

기대:

```text
active
```

현재 lifecycle manager는 launch 직후 즉시 실행하지 않고 약간의 delay 후 시작된다.

현재 기준:

```text
3 seconds
```

runtime validator는 시작 직후 바로 lifecycle 실패로 판단하지 말고 충분한 startup 시간을 허용해야 한다.

---

## 21. Localization navigation_mode

`localization.launch.py` 단독 실행 기본값:

```text
localization
```

허용 값:

```text
localization
localization_nav2
```

`navigation.launch.py`에서 include할 때:

```text
navigation_mode = localization_nav2
```

가 `map_bridge`로 전달되어야 한다.

---

## 22. Navigation mode 구조

실행:

```bash
ros2 launch patrol_navigation navigation.launch.py
```

전체 구조:

```text
localization.launch.py
    -> map_server
    -> AMCL
    -> LiDAR
    -> odometry TF
    -> map_bridge

Nav2
    -> controller_server
    -> smoother_server
    -> planner_server
    -> behavior_server
    -> bt_navigator
    -> waypoint_follower
    -> velocity_smoother

Nav2 velocity
    -> /cmd_vel_nav
    -> /cmd_vel_nav_dry_run

nav2_command_bridge
    <- /cmd_vel_nav_dry_run
    -> server dry-run command API
```

---

## 23. Navigation mode 주요 node

기대 node:

```text
/map_server
/amcl
/lifecycle_manager_localization

/controller_server
/smoother_server
/planner_server
/behavior_server
/bt_navigator
/waypoint_follower
/velocity_smoother
/lifecycle_manager_navigation
```

조건부:

```text
/map_bridge
/nav2_command_bridge
/rplidar_node
/base_to_laser_tf
/temporary_odom_to_base_tf
```

---

## 24. Nav2 lifecycle node

`lifecycle_manager_navigation`이 관리하는 node:

```text
controller_server
smoother_server
planner_server
behavior_server
bt_navigator
waypoint_follower
velocity_smoother
```

기대 최종 상태:

```text
active
```

검증 예:

```bash
ros2 lifecycle get /controller_server
ros2 lifecycle get /smoother_server
ros2 lifecycle get /planner_server
ros2 lifecycle get /behavior_server
ros2 lifecycle get /bt_navigator
ros2 lifecycle get /waypoint_follower
ros2 lifecycle get /velocity_smoother
```

현재 navigation lifecycle manager는 localization 활성화 시간을 확보하기 위해 delay 후 시작된다.

현재 기준:

```text
6 seconds
```

---

## 25. 주요 Nav2 action

`localization_nav2` mode에서 최소한 다음 action을 확인한다.

```text
/navigate_to_pose
/navigate_through_poses
```

일반적인 action type:

```text
nav2_msgs/action/NavigateToPose
nav2_msgs/action/NavigateThroughPoses
```

추가로 환경과 Nav2 구성에 따라 다음 action이 존재할 수 있다.

```text
/compute_path_to_pose
/compute_path_through_poses
/follow_path
/smooth_path
/spin
/backup
/wait
/follow_waypoints
```

validator는 최소 필수 action과 부가 action을 구분한다.

---

## 26. Nav2 action 검증

기본 확인:

```bash
ros2 action list -t
```

필수:

```text
/navigate_to_pose
/navigate_through_poses
```

action 존재 여부 확인은 goal을 실제 전송하는 것과 다르다.

기본 validator는 Nav2 goal을 전송하지 않는다.

---

## 27. Nav2 velocity path

현재 안전 설계의 핵심 path:

```text
controller_server
    -> /cmd_vel_nav

velocity_smoother
    <- /cmd_vel_nav
    -> /cmd_vel_nav_dry_run
```

behavior server의 recovery velocity도:

```text
/cmd_vel_nav_dry_run
```

으로 remap된다.

즉, 현재 `navigation.launch.py` 자체의 정상 상태에서는 Nav2가 실제:

```text
/cmd_vel
```

에 출력하면 안 된다.

---

## 28. `/cmd_vel_nav`

역할:

```text
controller_server의 내부 Nav2 velocity command
```

message type:

```text
geometry_msgs/msg/Twist
```

producer:

```text
/controller_server
```

consumer:

```text
/velocity_smoother
```

이 topic은 실제 motor command topic이 아니다.

---

## 29. `/cmd_vel_nav_dry_run`

현재 프로젝트의 핵심 안전 topic:

```text
/cmd_vel_nav_dry_run
```

message type:

```text
geometry_msgs/msg/Twist
```

주요 producer:

```text
/velocity_smoother
/behavior_server
```

consumer:

```text
/nav2_command_bridge
```

현재 검증 단계에서는 Nav2 최종 속도 출력이 반드시 이 topic으로 격리되어야 한다.

---

## 30. 실제 `/cmd_vel` 안전 조건

기본 `localization_nav2` validation에서는:

```text
/cmd_vel
```

에 Nav2 publisher가 존재하면 안 된다.

허용되지 않는 구조 예:

```text
controller_server -> /cmd_vel -> motor
```

정상 구조:

```text
controller_server
    -> /cmd_vel_nav
    -> velocity_smoother
    -> /cmd_vel_nav_dry_run
```

runtime 검사에서 `/cmd_vel` topic이 존재하더라도 publisher가 0개인지 확인할 수 있다.

예:

```bash
ros2 topic info /cmd_vel -v
```

현재 검증 환경에서 Nav2 publisher가 실제 `/cmd_vel`에 연결되어 있으면 ERROR다.

---

## 31. nav2_command_bridge

node:

```text
/nav2_command_bridge
```

executable:

```text
nav2_command_bridge
```

subscription:

```text
/cmd_vel_nav_dry_run
```

type:

```text
geometry_msgs/msg/Twist
```

기본 parameter:

```text
cmd_vel_topic = /cmd_vel_nav_dry_run
wheel_track_m = 0.201
max_wheel_mps = 0.50
twist_timeout_sec = 0.50
request_timeout_sec = 0.25
robot_id = pi-01
```

---

## 32. Twist → wheel velocity 변환

입력:

```text
linear_x = msg.linear.x
angular_z = msg.angular.z
```

차동구동 변환:

```text
half_track = wheel_track_m / 2

left_raw  = linear_x - angular_z * half_track
right_raw = linear_x + angular_z * half_track
```

현재:

```text
wheel_track_m = 0.201
```

좌·우 속도 중 하나가 `max_wheel_mps`를 초과하면 비율을 유지하면서 전체를 scale down한다.

현재 최대값:

```text
0.50 m/s
```

---

## 33. nav2_command_bridge 안전 상태

현재 코드의 ENV 기반 안전 정책:

```text
server_request_enabled = true
MOTOR_OUTPUT_ENABLED missing/empty/false -> motor_output_enabled = false
MOTOR_OUTPUT_ENABLED invalid -> startup failure
```

server까지 HTTP dry-run command는 전송할 수 있지만 실제 motor output은 차단된 상태다.

서버로 보내는 payload에는 다음 값이 포함된다.

```text
source = nav2_command_bridge
dry_run = true
```

validator와 test 환경은 `MOTOR_OUTPUT_ENABLED=false`를 명시하며, 검증 목적으로
`true`로 변경하면 안 된다.

---

## 34. Nav2 command payload

non-zero Twist:

```json
{
  "type": "auto_drive",
  "left_mps": "<computed>",
  "right_mps": "<computed>",
  "source": "nav2_command_bridge",
  "dry_run": true
}
```

zero Twist:

```json
{
  "type": "stop",
  "reason": "nav2_zero_twist",
  "source": "nav2_command_bridge",
  "dry_run": true
}
```

Twist timeout:

```json
{
  "type": "stop",
  "reason": "nav2_twist_timeout",
  "source": "nav2_command_bridge",
  "dry_run": true
}
```

invalid Twist:

```json
{
  "type": "stop",
  "reason": "nav2_invalid_twist",
  "source": "nav2_command_bridge",
  "dry_run": true
}
```

---

## 35. Nav2 Twist timeout

현재 기준:

```text
twist_timeout_sec = 0.50
```

마지막 Twist 수신 후 timeout을 초과하면 bridge는 stop command를 queue한다.

validator는 다음 조건을 확인할 수 있다.

```text
timeout > 0
NaN/Infinity reject
zero Twist -> stop
stale Twist -> stop
```

---

## 36. map_bridge

node:

```text
/map_bridge
```

executable:

```text
map_bridge
```

주요 subscription:

```text
/map
/scan
TF
```

message type:

```text
/map  -> nav_msgs/msg/OccupancyGrid
/scan -> sensor_msgs/msg/LaserScan
```

TF lookup:

```text
map -> base_link
```

---

## 37. map_bridge navigation mode

허용 값:

```text
mapping
localization
localization_nav2
```

잘못된 mode가 전달되면 node 초기화가 실패해야 한다.

기본값:

```text
mapping
```

`localization.launch.py`는 실제 실행 mode를 parameter로 전달한다.

---

## 38. map_bridge server 전달 경로

ROS `/map`:

```text
/map
-> map_bridge
-> POST /navigation/map
```

ROS TF pose:

```text
map -> base_link
-> map_bridge
-> POST /navigation/pose
```

ROS `/scan`:

```text
/scan
-> map_bridge
-> POST /navigation/scan
```

각 payload에는 다음 공통 값이 포함된다.

```text
robot_id
navigation_mode
```

---

## 39. Map payload

source:

```text
nav_msgs/msg/OccupancyGrid
```

server payload 주요 필드:

```text
robot_id
navigation_mode
frame_id
timestamp
bridge_timestamp
resolution
width
height
origin
data_encoding
data
```

현재 encoding:

```text
rle
```

origin:

```text
x
y
z
yaw
```

---

## 40. Pose payload

TF lookup:

```text
map -> base_link
```

server payload:

```text
robot_id
navigation_mode
frame_id
child_frame_id
timestamp
bridge_timestamp
x
y
z
yaw
orientation
```

기대 frame:

```text
frame_id = map
child_frame_id = base_link
```

TF lookup이 실패하면 임의 pose를 생성하지 않고 해당 publish cycle을 건너뛴다.

---

## 41. Scan payload

source:

```text
sensor_msgs/msg/LaserScan
```

주요 field:

```text
robot_id
navigation_mode
frame_id
timestamp
bridge_timestamp
angle_min
angle_max
angle_increment
range_min
range_max
ranges
```

`NaN`과 `Infinity` range는 server JSON payload에서:

```text
null
```

로 변환한다.

`max_scan_points`가 설정된 경우 downsampling할 수 있다.

localization launch 현재 값:

```text
max_scan_points = 180
```

---

## 42. map_bridge publish 주기

launch에 따라 값이 달라질 수 있다.

현재 mapping:

```text
map: 2.0 sec
pose: 0.5 sec
scan: 0.2 sec
```

현재 localization:

```text
map: 2.0 sec
pose: 0.5 sec
scan: 0.2 sec
```

정확한 timer 값보다 지속적으로 최신 데이터가 전달되는지가 runtime 검증의 핵심이다.

---

## 43. Odometry interface

현재 최종 목표는 실제 encoder 기반 odometry다.

일반적인 ROS interface:

```text
topic: /odom
type: nav_msgs/msg/Odometry
TF: odom -> base_link
```

실제 odometry가 활성화되면:

```text
start_fake_odom:=false
```

여야 한다.

### fake odometry

실제 odometry가 아직 없는 검증 환경에서는:

```text
temporary_odom_to_base_tf
```

가 static:

```text
odom -> base_link
```

를 생성한다.

이는 기능 테스트용이며 실제 주행 localization 정확도를 의미하지 않는다.

---

## 44. Map server

node:

```text
/map_server
```

package:

```text
nav2_map_server
```

executable:

```text
map_server
```

output:

```text
/map
```

type:

```text
nav_msgs/msg/OccupancyGrid
```

입력 지도는 YAML 파일로 지정한다.

기본 예:

```text
navigation/maps/slam_test_01.yaml
```

지도 YAML과 해당 image가 함께 존재해야 한다.

---

## 45. 대표 map YAML 필드

필수:

```text
image
resolution
origin
negate
occupied_thresh
free_thresh
```

예상 구조:

```yaml
image: slam_test_01.pgm
resolution: 0.05
origin: [0.0, 0.0, 0.0]
negate: 0
occupied_thresh: 0.65
free_thresh: 0.196
```

실제 값은 지도에 따라 달라질 수 있다.

---

## 46. Mode별 최소 runtime 기대 상태

### Mapping

node:

```text
slam_toolbox
```

topic:

```text
/scan
/map
```

TF:

```text
map -> odom
odom -> base_link
base_link -> laser
```

### Localization

node:

```text
map_server
amcl
lifecycle_manager_localization
```

topic:

```text
/map
/scan
/amcl_pose
```

TF:

```text
map -> odom
odom -> base_link
base_link -> laser
```

lifecycle:

```text
map_server = active
amcl = active
```

### Localization + Nav2

Localization 조건 전부 +:

```text
controller_server
smoother_server
planner_server
behavior_server
bt_navigator
waypoint_follower
velocity_smoother
lifecycle_manager_navigation
```

action:

```text
/navigate_to_pose
/navigate_through_poses
```

dry-run topic:

```text
/cmd_vel_nav_dry_run
```

---

## 47. Hardware 없이 runtime 검증할 때

다음과 같은 조합을 사용할 수 있다.

```text
start_lidar:=false
start_bridge:=false
start_fake_odom:=true
```

navigation 검증에서는 추가로:

```text
start_nav2_command_bridge:=false
```

를 의도할 수 있다.

이 경우 검증 목적은:

```text
launch 성공 여부
Nav2 node 생성
lifecycle activation
action 생성
TF 기본 구조
```

이다.

실제 `/scan` 수신은 hardware test가 아니므로 필수로 요구하지 않는다.

---

## 48. Hardware 포함 runtime 검증

실제 LiDAR와 odometry를 포함하는 경우:

```text
start_lidar:=true
start_fake_odom:=false
```

필수 확인:

```text
/scan 실제 message 수신
/odom 실제 message 수신
odom -> base_link dynamic TF
base_link -> laser TF
map -> odom TF
map -> base_link TF chain
```

LiDAR 실제 검증 예:

```bash
ros2 topic echo --once /scan
```

Odometry 실제 검증 예:

```bash
ros2 topic echo --once /odom
```

---

## 49. 기본 runtime 검사에서 금지할 동작

`validate-navigation`은 기본적으로 다음을 수행하지 않는다.

```text
Nav2 goal 실제 전송
/cmd_vel 직접 publish
/cmd_vel_nav_dry_run에 임의 Twist publish
실제 motor enable
Pico W 직접 motor command
지도 파일 overwrite
AMCL pose 강제 변경
```

검증은 가능한 한 read-only ROS graph 확인과 launch 상태 확인으로 수행한다.

---

## 50. Static validation 기준

정적 검증에서 최소한 확인한다.

```text
Python syntax
package.xml
setup.py
console_scripts
launch file 존재
launch argument 선언
launch argument 사용
local executable 존재
external package dependency
YAML syntax
frame 이름
navigation mode
dry-run remapping
LaserScan QoS
representative map
```

---

## 51. Launch argument 검증

사용되는 모든 `LaunchConfiguration`은 정상적으로 `DeclareLaunchArgument` 되어야 한다.

예:

```text
start_lidar
start_bridge
start_fake_odom
start_nav2_command_bridge
map
params_file
robot_id
server_base_url
```

선언 객체를 생성했지만 최종 `LaunchDescription`에 추가하지 않은 경우 정상 선언으로 간주하지 않는다.

---

## 52. 현재 알려진 navigation.launch.py 불일치

현재 `navigation.launch.py`에는 다음 의도가 존재한다.

```text
start_nav2_command_bridge
```

하지만 `DeclareLaunchArgument("start_nav2_command_bridge", ...)`가 함수 내부에서 생성된 뒤 최종 `LaunchDescription` 항목에 포함되지 않은 상태다.

따라서 현재 validator는 이를:

```text
orphan launch argument
```

또는 동등한 ERROR로 보고할 수 있다.

정상 기대 상태는 다음과 같다.

```text
DeclareLaunchArgument("start_nav2_command_bridge", ...)
```

가 최종 `LaunchDescription([...])` 안에 포함되고:

```text
LaunchConfiguration("start_nav2_command_bridge")
```

와 연결되어야 한다.

이 문서는 현재 버그를 정상 contract로 승인하지 않는다.

---

## 53. Dependency 검증

launch에서 사용하는 외부 package는 `package.xml` dependency와 일치해야 한다.

예상 주요 dependency 범주:

```text
rclpy
nav_msgs
sensor_msgs
geometry_msgs
tf2_ros
slam_toolbox
nav2_map_server
nav2_amcl
nav2_lifecycle_manager
nav2_controller
nav2_smoother
nav2_planner
nav2_behaviors
nav2_bt_navigator
nav2_waypoint_follower
nav2_velocity_smoother
rviz2
```

LiDAR driver package는 실제 선택한 구현에 따라 달라질 수 있다.

---

## 54. Validator ERROR 기준

다음은 ERROR다.

```text
patrol_navigation package build 실패
필수 executable 누락
launch Python syntax 오류
사용된 launch argument 미선언
orphan DeclareLaunchArgument
필수 local executable 누락
map/odom/base_link canonical frame 불일치
AMCL frame 설정 불일치
Nav2가 실제 /cmd_vel에 publish
/cmd_vel_nav_dry_run remapping 누락
nav2_command_bridge motor_output_enabled=true
LaserScan subscription 구조 오류
필수 lifecycle node 비활성
필수 Nav2 action 누락
TF chain 단절
실제 hardware 모드에서 /scan 미수신
```

---

## 55. Validator WARN 기준

다음은 WARN으로 처리할 수 있다.

```text
중복 package dependency
mapping/localization의 LiDAR driver default 불일치
실제 hardware가 없어 /scan 검증 생략
실제 odometry가 없어 fake odom 사용
map_bridge server 연결 불가
외부 FastAPI server 미실행
대표 map이 없는 개발 환경
optional RViz 미실행
```

단, 사용자가 hardware/runtime 검증을 명시한 경우 동일 항목이 ERROR가 될 수 있다.

---

## 56. Validator PASS 기준

최종 PASS의 핵심 조건:

```text
package build 성공
필수 executable 설치 확인
launch argument 평가 성공
canonical TF frame 구성 정상
mode별 필수 node 존재
필수 topic 존재
lifecycle node active
Nav2 action 존재
Nav2 velocity가 dry-run topic으로 격리
실제 /cmd_vel motor path 없음
MOTOR_OUTPUT_ENABLED의 safe default=false
검증 환경 motor_output_enabled=false
```

hardware 검증을 요청한 경우 추가:

```text
실제 /scan 수신
실제 /odom 수신
dynamic odom -> base_link 확인
```

---

## 57. 대표 정상 Mapping 상태

```text
/rplidar_node
    -> /scan

/slam_toolbox
    -> /map
    -> map -> odom

/temporary_odom_to_base_tf
    -> odom -> base_link

/base_to_laser_tf
    -> base_link -> laser

/map_bridge
    <- /map
    <- /scan
    <- map -> base_link
```

---

## 58. 대표 정상 Localization 상태

```text
/map_server [active]
    -> /map

/amcl [active]
    <- /map
    <- /scan
    -> /amcl_pose
    -> map -> odom

odometry
    -> odom -> base_link

/base_to_laser_tf
    -> base_link -> laser

/map_bridge
    <- /map
    <- /scan
    <- map -> base_link
```

---

## 59. 대표 정상 Localization + Nav2 상태

```text
Localization stack
    -> map -> odom -> base_link -> laser

/controller_server [active]
    -> /cmd_vel_nav

/velocity_smoother [active]
    <- /cmd_vel_nav
    -> /cmd_vel_nav_dry_run

/behavior_server [active]
    -> /cmd_vel_nav_dry_run

/nav2_command_bridge
    <- /cmd_vel_nav_dry_run
    -> HTTP dry-run command

MOTOR_OUTPUT_ENABLED=false
motor_output_enabled = false
```

actions:

```text
/navigate_to_pose
/navigate_through_poses
```

---

## 60. 최종 source of truth

Navigation ROS interface 검증 시 우선 확인할 파일:

```text
navigation/ros/patrol_navigation/setup.py

navigation/ros/patrol_navigation/launch/lidar.launch.py
navigation/ros/patrol_navigation/launch/mapping.launch.py
navigation/ros/patrol_navigation/launch/localization.launch.py
navigation/ros/patrol_navigation/launch/navigation.launch.py

navigation/ros/patrol_navigation/patrol_navigation/map_bridge.py
navigation/ros/patrol_navigation/patrol_navigation/nav2_command_bridge.py

navigation/ros/patrol_navigation/config/slam_toolbox.yaml
navigation/ros/patrol_navigation/config/amcl.yaml
navigation/ros/patrol_navigation/config/nav2_params.yaml
```

이 reference 문서는 validator의 기대 상태를 정의한다.

현재 source code가 이 문서와 다르면 무조건 문서를 코드에 맞춰 바꾸는 것이 아니라, 그 차이가 의도된 변경인지 회귀인지 먼저 판단해야 한다.
