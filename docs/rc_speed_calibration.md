# RC카 최대 출력과 속도 보정

D-pad 버튼과 방향키는 직진·후진 시 PWM 100%를 요청한다. 곡선 주행의 안쪽 바퀴 비율은 기존 0.35이며, 회전/정지/failsafe 동작은 유지한다.

Goal 자율주행에서는 Nav2가 속도를 선택하고 기존 가속도·감속도 제한을 적용한다. 최대 직진 명령에서 양쪽 100% PWM에 도달하며, 회전·장애물·goal 접근 시에는 더 낮은 명령을 사용할 수 있다. 경로가 짧거나 제어기가 감속을 선택하면 최대 출력에 도달하지 않을 수 있다.

기존 자동주행 최소 PWM 50% 보정도 유지한다. 임시 최대속도 0.17 기준 Nav2 최소 직진 속도는 0.085 m/s이다. 더 작은 non-zero 바퀴 명령은 Pi에서 좌우 비율을 유지하며 최소 출력까지 높인다. 0 명령은 계속 정지한다.

## 적용

1. Pi와 GPU 서버에서 같은 feature commit을 적용한다.
2. **양쪽 저장소 루트의 기존 `.env`에서 `MAX_WHEEL_MPS=0.17`로 변경한다.** `.env.example` 변경은 기존 `.env`에 자동 반영되지 않는다. 기존 0.50을 남겨두지 않는다.
3. GPU 서버에서 ROS 환경을 source한 뒤 `navigation/ros`에서 `colcon build --packages-select patrol_navigation`을 실행하고 `source install/setup.bash`로 갱신한다.
4. RC카를 정지한 상태에서 Pi와 GPU 런타임을 다시 시작하고 Driving 모드의 navigation launch도 다시 시작한다. 직접 실행 시에도 동일 `.env`를 export하여 사용한다.
5. `ros2 param get /nav2_command_bridge max_wheel_mps`, `ros2 param get /controller_server FollowPath.max_vel_x`, `ros2 param get /controller_server FollowPath.max_speed_xy`, `ros2 param get /velocity_smoother max_velocity`로 보정값이 일치하는지 확인한다.

`MAX_WHEEL_MPS`는 최대 PWM에서의 바퀴 선속도(m/s)이다. **0.17은 실측값이 아닌 임시 추정값**이며, 60 RPM 모터·약 65~67.5 mm 바퀴의 부하 상태를 가정한다. Nav2 YAML의 0.17도 같은 임시 예시다. 실제 launch는 `MAX_WHEEL_MPS`로 controller/smoother의 종방향 상한을 덮어쓴다. 커스텀 `params_file`에서도 회전·가속도·감속도 설정은 유지한다.

Pi의 `--max-wheel-mps` 또는 ROS bridge의 `max_wheel_mps`만 환경변수와 다르게 override하면 시작을 거부한다. **서로 다른 컴퓨터의 `.env` 값은 자동 동기화하지 않으므로 양쪽을 함께 변경해야 한다.**

## 실측 및 재보정

1. 실제 하중·바닥·배터리 조건에서 D-pad 직진으로 최대 PWM을 요청한다. 출발 가속 구간을 제외한 일정 속도 구간의 `/odom` 속도와 `/wheel_ticks`를 기록한다. 바퀴를 공중에 띄운 측정은 부하 상태 보정값으로 쓰지 않는다.
2. 일정 시간 Δt 동안 바퀴별 tick 변화 Δticks를 구한다. 바퀴 속도는 `v = π × WHEEL_DIAMETER_M × Δticks / ENCODER_TICKS_PER_REV / Δt`로 계산한다. 같은 쪽 앞·뒤 바퀴의 속도를 평균하고 좌·우 결과도 비교한다.
3. 여러 직진 구간에서 반복한다. 실제 이동 거리/시간과 대조하여 wheel diameter·ticks/revolution 설정 및 미끄러짐을 확인한다. 회전·가속·정지 구간은 제외한다.
4. 안정 구간에서 얻은 대표 최고속도를 Pi와 GPU 서버의 `MAX_WHEEL_MPS`에 동일하게 입력하고 양쪽 런타임과 Nav2를 재시작한다. 보정 당시 배터리·하중·바닥·좌우 측정값을 기록한다.

현재 제어는 엔코더 피드백으로 PWM을 조정하는 속도 PID가 아닌 개방루프 환산이다. 단일 보정값은 모든 부하·배터리 상태나 좌우 모터 편차를 보상하지 않는다. PWM 비율 증가(D-pad 35→100%, 최신 dev의 Nav2 최대 직진 60→100%)를 실측 속도 증가율로 해석하지 않는다.
