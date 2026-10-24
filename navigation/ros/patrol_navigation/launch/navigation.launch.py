# 저장 지도 기반 localization과 Nav2를 함께 실행하는 launch 파일.
#
# 실행 구조:
#   localization.launch.py
#     - existing /scan from lidar_websocket_bridge
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
#   최종 runtime에서는 RC카 encoder -> EncoderRosBridge -> /wheel_ticks
#   -> server/wheel_odometry.py -> /odom + dynamic odom->base_link TF만 사용한다.
#   static/fake odometry fallback은 사용하지 않는다.
#
# 안전 설정:
#   Nav2 최종 속도 명령은 실제 /cmd_vel이 아니라
#   /cmd_vel_nav_dry_run으로 출력한다.
#
# 따라서 현재 파일만 실행해서는 Pico W, MDD10A, 모터에
# 어떤 명령도 전달되지 않는다.

import yaml

from patrol_navigation.env_config import env_float
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    OpaqueFunction,
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
    # Full-PWM wheel-speed limit shared by controller, smoother, bridge and Pi.
    max_wheel_mps = env_float("MAX_WHEEL_MPS", minimum=0.01)
    wheel_track_m = env_float("WHEEL_TRACK_M", minimum=0.01)
    # Differential-drive in-place rotation at left=-max, right=+max.
    max_angular_rps = (2.0 * max_wheel_mps) / wheel_track_m
    # Physical motor breakaway floor. Apply only after a non-zero wheel command;
    # never use this as DWB min_vel_x, otherwise in-place rotation is impossible.
    min_auto_drive_pwm = env_float("MIN_AUTO_DRIVE_PWM", minimum=0.0)
    pkg_share = FindPackageShare(
        "patrol_navigation"
    )
    navigate_to_pose_bt = PathJoinSubstitution(
        [
            pkg_share,
            "behavior_trees",
            "navigate_to_pose_dynamic_replanning.xml",
        ]
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

    start_bridge = LaunchConfiguration(
        "start_bridge"
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

                "start_bridge": start_bridge,

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
            nav2_params,
            {
                "FollowPath.min_vel_x": 0.0,
                "FollowPath.min_speed_xy": 0.0,
                "FollowPath.max_vel_x": max_wheel_mps,
                "FollowPath.max_speed_xy": max_wheel_mps,
                "FollowPath.max_vel_theta": max_angular_rps,
                "FollowPath.rotate_to_heading_angular_vel": max_angular_rps,
            },
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
            nav2_params,
            {"max_rotational_vel": max_angular_rps}
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
            nav2_params,
            {
                "default_nav_to_pose_bt_xml": navigate_to_pose_bt,
            },
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
    def launch_velocity_smoother(context):
        # Resolve custom params_file before overriding ONLY longitudinal limits.
        with open(nav2_params.perform(context), encoding="utf-8") as stream:
            config = yaml.safe_load(stream)
        parameters = config["velocity_smoother"]["ros__parameters"]
        max_velocity = list(parameters["max_velocity"])
        min_velocity = list(parameters["min_velocity"])
        max_velocity[0] = max_wheel_mps
        min_velocity[0] = -max_wheel_mps
        max_velocity[2] = max_angular_rps
        min_velocity[2] = -max_angular_rps
        return [Node(
            package="nav2_velocity_smoother",
            executable="velocity_smoother",
            name="velocity_smoother",
            output="screen",
            parameters=[
                nav2_params,
                {"max_velocity": max_velocity, "min_velocity": min_velocity},
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
        )]

    velocity_smoother = OpaqueFunction(function=launch_velocity_smoother)
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
                "wheel_track_m": wheel_track_m,
                "max_wheel_mps": max_wheel_mps,
                "min_auto_drive_pwm": min_auto_drive_pwm,
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
                description=(
                    "Saved map YAML selected for the real RC car. "
                    "No bundled test-map default is used."
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
            # 실행 여부
            # -------------------------------------------------
            DeclareLaunchArgument(
                "start_bridge",
                default_value="true",
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
