# 저장 지도 기반 localization과 Nav2를 함께 실행하는 launch 파일.
#
# 실행 구조:
#   localization.launch.py
#     - SLLIDAR
#     - map_server
#     - AMCL
#     - odom -> base_link TF
#     - map_bridge
#
#   Navigation2
#     - controller_server
#     - smoother_server
#     - planner_server
#     - behavior_server
#     - bt_navigator
#     - waypoint_follower
#     - velocity_smoother
#
# navigation_mode:
#   localization_nav2
#
# odometry 설정:
#   최종 runtime에서는 server/wheel_odometry.py의 /odom 및 dynamic
#   odom->base_link TF를 사용한다. fake odometry는 기본 비활성화이며
#   독립 테스트가 필요할 때만 start_fake_odom:=true로 활성화한다.
#
# 안전 설정:
#   Nav2 최종 속도 명령은 실제 /cmd_vel이 아니라
#   /cmd_vel_nav_dry_run으로 출력한다.
#
# 따라서 현재 파일만 실행해서는 Pico W, MDD10A, 모터에
# 어떤 명령도 전달되지 않는다.

from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    IncludeLaunchDescription,
    TimerAction,
)
from launch.launch_description_sources import (
    PythonLaunchDescriptionSource,
)
from launch.substitutions import (
    LaunchConfiguration,
    PathJoinSubstitution,
)

from launch_ros.actions import Node
from launch_ros.substitutions import (
    FindPackageShare,
)
from launch.conditions import IfCondition

def generate_launch_description():
    pkg_share = FindPackageShare(
        "patrol_navigation"
    )

    # ---------------------------------------------------------
    # Launch arguments
    # ---------------------------------------------------------
    map_yaml = LaunchConfiguration(
        "map"
    )
    nav2_params = LaunchConfiguration(
        "params_file"
    )

    robot_id = LaunchConfiguration(
        "robot_id"
    )
    server_base_url = LaunchConfiguration(
        "server_base_url"
    )

    navigation_mode = LaunchConfiguration(
        "navigation_mode"
    )

    serial_port = LaunchConfiguration(
        "serial_port"
    )
    serial_baudrate = LaunchConfiguration(
        "serial_baudrate"
    )

    start_lidar = LaunchConfiguration(
        "start_lidar"
    )
    start_bridge = LaunchConfiguration(
        "start_bridge"
    )
    start_fake_odom = LaunchConfiguration(
        "start_fake_odom"
    )
    start_nav2_command_bridge = LaunchConfiguration(
        "start_nav2_command_bridge"
    )
    nav2_twist_timeout_sec = LaunchConfiguration(
        "nav2_twist_timeout_sec"
    )
    nav2_request_timeout_sec = LaunchConfiguration(
        "nav2_request_timeout_sec"
    )

    # ---------------------------------------------------------
    # 기존 localization launch 재사용
    # ---------------------------------------------------------
    localization_launch = (
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(
                PathJoinSubstitution(
                    [
                        pkg_share,
                        "launch",
                        "localization.launch.py",
                    ]
                )
            ),
            launch_arguments={
                "map": map_yaml,
                "robot_id": robot_id,
                "server_base_url": (
                    server_base_url
                ),

                # localization.launch.py의 map_bridge에
                # localization_nav2 모드를 전달한다.
                "navigation_mode": (
                    navigation_mode
                ),

                "serial_port": serial_port,
                "serial_baudrate": (
                    serial_baudrate
                ),

                "start_lidar": start_lidar,
                "start_bridge": start_bridge,
                "start_fake_odom": (
                    start_fake_odom
                ),

                "use_sim_time": "false",
            }.items(),
        )
    )

    common_remappings = [
        ("/tf", "tf"),
        ("/tf_static", "tf_static"),
    ]

    # ---------------------------------------------------------
    # Controller server
    # ---------------------------------------------------------
    # Controller가 생성하는 속도 명령을 cmd_vel_nav로 보낸다.
    # velocity_smoother가 이 명령을 받아
    # /cmd_vel_nav_dry_run으로 출력한다.
    controller_server = Node(
        package="nav2_controller",
        executable="controller_server",
        name="controller_server",
        output="screen",
        parameters=[
            nav2_params
        ],
        remappings=(
            common_remappings
            + [
                (
                    "cmd_vel",
                    "cmd_vel_nav",
                ),
            ]
        ),
    )

    # ---------------------------------------------------------
    # Path smoother
    # ---------------------------------------------------------
    smoother_server = Node(
        package="nav2_smoother",
        executable="smoother_server",
        name="smoother_server",
        output="screen",
        parameters=[
            nav2_params
        ],
        remappings=common_remappings,
    )

    # ---------------------------------------------------------
    # Global planner
    # ---------------------------------------------------------
    planner_server = Node(
        package="nav2_planner",
        executable="planner_server",
        name="planner_server",
        output="screen",
        parameters=[
            nav2_params
        ],
        remappings=common_remappings,
    )

    # ---------------------------------------------------------
    # Recovery / behavior server
    # ---------------------------------------------------------
    # Spin, BackUp 등의 recovery 동작도 실제 /cmd_vel이 아닌
    # dry-run topic으로 강제 분리한다.
    behavior_server = Node(
        package="nav2_behaviors",
        executable="behavior_server",
        name="behavior_server",
        output="screen",
        parameters=[
            nav2_params
        ],
        remappings=(
            common_remappings
            + [
                (
                    "cmd_vel",
                    "/cmd_vel_nav_dry_run",
                ),
            ]
        ),
    )

    # ---------------------------------------------------------
    # Behavior Tree navigator
    # ---------------------------------------------------------
    bt_navigator = Node(
        package="nav2_bt_navigator",
        executable="bt_navigator",
        name="bt_navigator",
        output="screen",
        parameters=[
            nav2_params
        ],
        remappings=common_remappings,
    )

    # ---------------------------------------------------------
    # Waypoint follower
    # ---------------------------------------------------------
    waypoint_follower = Node(
        package="nav2_waypoint_follower",
        executable="waypoint_follower",
        name="waypoint_follower",
        output="screen",
        parameters=[
            nav2_params
        ],
        remappings=common_remappings,
    )

    # ---------------------------------------------------------
    # Velocity smoother
    # ---------------------------------------------------------
    # 입력:
    #   /cmd_vel_nav
    #
    # 출력:
    #   /cmd_vel_nav_dry_run
    #
    # 실제 /cmd_vel에는 publish하지 않는다.
    velocity_smoother = Node(
        package="nav2_velocity_smoother",
        executable="velocity_smoother",
        name="velocity_smoother",
        output="screen",
        parameters=[
            nav2_params
        ],
        remappings=(
            common_remappings
            + [
                (
                    "cmd_vel",
                    "cmd_vel_nav",
                ),
                (
                    "cmd_vel_smoothed",
                    "/cmd_vel_nav_dry_run",
                ),
            ]
        ),
    )
    # ---------------------------------------------------------
    # Nav2 command bridge
    # ---------------------------------------------------------
    # /cmd_vel_nav_dry_run을 좌우 바퀴 속도로 변환하고
    # 서버의 dry-run command API로 전달한다.
    #
    # 브리지와 서버 모두 실제 모터 출력을 비활성화한 상태다.
    nav2_command_bridge = Node(
        package="patrol_navigation",
        executable="nav2_command_bridge",
        name="nav2_command_bridge",
        output="screen",
        condition=IfCondition(
            start_nav2_command_bridge
        ),
        parameters=[
            {
                "cmd_vel_topic": (
                    "/cmd_vel_nav_dry_run"
                ),
                "wheel_track_m": 0.201,
                "max_wheel_mps": 0.50,
                "twist_timeout_sec": nav2_twist_timeout_sec,
                "server_base_url": (
                    server_base_url
                ),
                "robot_id": robot_id,
                "request_timeout_sec": nav2_request_timeout_sec,
            }
        ],
    )

    # ---------------------------------------------------------
    # Navigation lifecycle manager
    # ---------------------------------------------------------
    # localization 쪽 map_server와 AMCL이 먼저 활성화될 시간을
    # 주기 위해 6초 뒤 Nav2 노드들을 활성화한다.
    lifecycle_manager_navigation = (
        TimerAction(
            period=6.0,
            actions=[
                Node(
                    package=(
                        "nav2_lifecycle_manager"
                    ),
                    executable=(
                        "lifecycle_manager"
                    ),
                    name=(
                        "lifecycle_manager_"
                        "navigation"
                    ),
                    output="screen",
                    parameters=[
                        {
                            "use_sim_time": False,
                            "autostart": True,
                            "node_names": [
                                "controller_server",
                                "smoother_server",
                                "planner_server",
                                "behavior_server",
                                "bt_navigator",
                                "waypoint_follower",
                                "velocity_smoother",
                            ],
                        }
                    ],
                )
            ],
        )
    )

    return LaunchDescription(
        [
            # -------------------------------------------------
            # 저장 지도
            # -------------------------------------------------
            DeclareLaunchArgument(
                "map",
                default_value=(
                    PathJoinSubstitution(
                        [
                            pkg_share,
                            "maps",
                            "slam_test_01.yaml",
                        ]
                    )
                ),
            ),

            # -------------------------------------------------
            # Nav2 설정
            # -------------------------------------------------
            DeclareLaunchArgument(
                "params_file",
                default_value=(
                    PathJoinSubstitution(
                        [
                            pkg_share,
                            "config",
                            "nav2_params.yaml",
                        ]
                    )
                ),
            ),

            # -------------------------------------------------
            # 실행 모드
            # -------------------------------------------------
            DeclareLaunchArgument(
                "navigation_mode",
                default_value=(
                    "localization_nav2"
                ),
                choices=[
                    "localization_nav2",
                ],
            ),

            # -------------------------------------------------
            # 서버
            # -------------------------------------------------
            DeclareLaunchArgument(
                "robot_id",
                default_value="pi-01",
            ),
            DeclareLaunchArgument(
                "server_base_url",
                default_value=(
                    "http://127.0.0.1:21063"
                ),
            ),

            # -------------------------------------------------
            # LiDAR
            # -------------------------------------------------
            DeclareLaunchArgument(
                "serial_port",
                default_value="/dev/ttyUSB0",
            ),
            DeclareLaunchArgument(
                "serial_baudrate",
                default_value="115200",
            ),

            # -------------------------------------------------
            # 실행 여부
            # -------------------------------------------------
            DeclareLaunchArgument(
                "start_lidar",
                default_value="true",
            ),
            DeclareLaunchArgument(
                "start_bridge",
                default_value="true",
            ),
            DeclareLaunchArgument(
                "start_fake_odom",
                default_value="false",
            ),
            DeclareLaunchArgument(
                "start_nav2_command_bridge",
                default_value="true",
            ),
            DeclareLaunchArgument(
                "nav2_twist_timeout_sec",
                default_value="0.50",
            ),
            DeclareLaunchArgument(
                "nav2_request_timeout_sec",
                default_value="0.25",
            ),

            # -------------------------------------------------
            # Localization
            # -------------------------------------------------
            localization_launch,

            # -------------------------------------------------
            # Navigation2
            # -------------------------------------------------
            controller_server,
            smoother_server,
            planner_server,
            behavior_server,
            bt_navigator,
            waypoint_follower,
            velocity_smoother,

            # Nav2 Twist → 서버 dry-run API
            nav2_command_bridge,

            # lifecycle activation
            lifecycle_manager_navigation,
        ]
    )
