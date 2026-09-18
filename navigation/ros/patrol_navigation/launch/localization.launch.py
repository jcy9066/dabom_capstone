# 저장된 지도와 AMCL을 이용한 위치 추정 launch 파일.
#
# 실행 구조:
#   map_server -> /map
#   SLLIDAR    -> /scan
#   AMCL       -> /amcl_pose + map->odom TF
#   odometry   -> odom->base_link TF
#   map_bridge -> FastAPI server
#
# 최종 runtime에서는 root start_gpu_server.sh가 server/wheel_odometry.py를 실행하여
# /odom과 dynamic odom->base_link TF를 제공한다.
# fake odometry는 launch 단독 테스트용 opt-in fallback이며 기본값은 false이다.
# 필요한 경우에만 start_fake_odom:=true를 명시적으로 전달한다.
#
# navigation_mode:
#   localization
#   localization_nav2
#
# navigation.launch.py에서 이 launch를 include할 때
# navigation_mode:=localization_nav2를 전달한다.

from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    IncludeLaunchDescription,
    TimerAction,
)
from launch.conditions import IfCondition
from launch.launch_description_sources import (
    PythonLaunchDescriptionSource,
)
from launch.substitutions import (
    EnvironmentVariable,
    LaunchConfiguration,
    PathJoinSubstitution,
)

from launch_ros.actions import Node
from launch_ros.parameter_descriptions import (
    ParameterValue,
)
from launch_ros.substitutions import (
    FindPackageShare,
)


def generate_launch_description():
    pkg_share = FindPackageShare(
        "patrol_navigation"
    )

    lidar_launch = PathJoinSubstitution(
        [
            pkg_share,
            "launch",
            "lidar.launch.py",
        ]
    )

    # ---------------------------------------------------------
    # 공통 설정
    # ---------------------------------------------------------
    use_sim_time = LaunchConfiguration(
        "use_sim_time"
    )
    map_yaml_file = LaunchConfiguration(
        "map"
    )
    amcl_config = LaunchConfiguration(
        "amcl_config"
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

    # ---------------------------------------------------------
    # 실행 여부
    # ---------------------------------------------------------
    start_lidar = LaunchConfiguration(
        "start_lidar"
    )
    start_bridge = LaunchConfiguration(
        "start_bridge"
    )
    start_fake_odom = LaunchConfiguration(
        "start_fake_odom"
    )

    # ---------------------------------------------------------
    # TF frame
    # ---------------------------------------------------------
    odom_frame = LaunchConfiguration(
        "odom_frame"
    )
    base_frame = LaunchConfiguration(
        "base_frame"
    )

    # ---------------------------------------------------------
    # LiDAR 설정
    # ---------------------------------------------------------
    serial_port = LaunchConfiguration(
        "serial_port"
    )
    serial_baudrate = LaunchConfiguration(
        "serial_baudrate"
    )
    driver_package = LaunchConfiguration(
        "driver_package"
    )
    driver_executable = LaunchConfiguration(
        "driver_executable"
    )

    # ---------------------------------------------------------
    # 저장 지도 서버
    # ---------------------------------------------------------
    map_server_node = Node(
        package="nav2_map_server",
        executable="map_server",
        name="map_server",
        output="screen",
        parameters=[
            {
                "use_sim_time": (
                    ParameterValue(
                        use_sim_time,
                        value_type=bool,
                    )
                ),
                "yaml_filename": (
                    ParameterValue(
                        map_yaml_file,
                        value_type=str,
                    )
                ),
            }
        ],
    )

    # ---------------------------------------------------------
    # AMCL 위치 추정
    # ---------------------------------------------------------
    amcl_node = Node(
        package="nav2_amcl",
        executable="amcl",
        name="amcl",
        output="screen",
        parameters=[
            amcl_config,
            {
                "use_sim_time": (
                    ParameterValue(
                        use_sim_time,
                        value_type=bool,
                    )
                ),
            },
        ],
    )

    # ---------------------------------------------------------
    # FastAPI bridge
    # ---------------------------------------------------------
    # map_server가 최초 /map을 publish하기 전에 bridge가
    # subscriber를 생성하도록 즉시 실행한다.
    map_bridge_node = Node(
        package="patrol_navigation",
        executable="map_bridge",
        name="map_bridge",
        output="screen",
        condition=IfCondition(
            start_bridge
        ),
        parameters=[
            {
                "robot_id": robot_id,
                "server_base_url": (
                    server_base_url
                ),
                "navigation_mode": (
                    ParameterValue(
                        navigation_mode,
                        value_type=str,
                    )
                ),

                "map_topic": "/map",
                "scan_topic": "/scan",

                "pose_parent_frame": "map",
                "pose_child_frame": (
                    "base_link"
                ),

                "map_publish_period_sec": 2.0,
                "pose_publish_period_sec": 0.5,
                "scan_publish_period_sec": 0.2,
                "request_timeout_sec": 5.0,

                "send_map": True,
                "send_pose": True,
                "send_scan": True,
                "max_scan_points": 180,
            }
        ],
    )

    # ---------------------------------------------------------
    # Nav2 localization lifecycle manager
    # ---------------------------------------------------------
    # map_server, AMCL, map_bridge subscriber가 먼저 준비되도록
    # 3초 뒤 map_server와 AMCL을 configure/activate한다.
    lifecycle_manager_node = TimerAction(
        period=3.0,
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
                    "localization"
                ),
                output="screen",
                parameters=[
                    {
                        "use_sim_time": (
                            ParameterValue(
                                use_sim_time,
                                value_type=bool,
                            )
                        ),
                        "autostart": True,
                        "node_names": [
                            "map_server",
                            "amcl",
                        ],
                    }
                ],
            )
        ],
    )

    return LaunchDescription(
        [
            # -------------------------------------------------
            # 기본 인자
            # -------------------------------------------------
            DeclareLaunchArgument(
                "use_sim_time",
                default_value="false",
            ),
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

            # localization.launch.py 단독 실행 시
            # localization 모드로 동작한다.
            DeclareLaunchArgument(
                "navigation_mode",
                default_value="localization",
                choices=[
                    "localization",
                    "localization_nav2",
                ],
            ),

            # -------------------------------------------------
            # 저장 지도
            # -------------------------------------------------
            DeclareLaunchArgument(
                "map",
                default_value=(
                    PathJoinSubstitution(
                        [
                            EnvironmentVariable(
                                "HOME"
                            ),
                            "dabom_capstone",
                            "navigation",
                            "maps",
                            "slam_test_01.yaml",
                        ]
                    )
                ),
            ),

            # -------------------------------------------------
            # AMCL 설정
            # -------------------------------------------------
            DeclareLaunchArgument(
                "amcl_config",
                default_value=(
                    PathJoinSubstitution(
                        [
                            pkg_share,
                            "config",
                            "amcl.yaml",
                        ]
                    )
                ),
            ),

            # -------------------------------------------------
            # 실행 스위치
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

            # -------------------------------------------------
            # TF frame
            # -------------------------------------------------
            DeclareLaunchArgument(
                "odom_frame",
                default_value="odom",
            ),
            DeclareLaunchArgument(
                "base_frame",
                default_value="base_link",
            ),

            # -------------------------------------------------
            # LiDAR 설정
            # -------------------------------------------------
            DeclareLaunchArgument(
                "serial_port",
                default_value="/dev/ttyUSB0",
            ),
            DeclareLaunchArgument(
                "serial_baudrate",
                default_value="115200",
            ),
            DeclareLaunchArgument(
                "driver_package",
                default_value="sllidar_ros2",
            ),
            DeclareLaunchArgument(
                "driver_executable",
                default_value=(
                    "sllidar_node"
                ),
            ),

            # -------------------------------------------------
            # LiDAR + base_link -> laser TF
            # -------------------------------------------------
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    lidar_launch
                ),
                condition=IfCondition(
                    start_lidar
                ),
                launch_arguments={
                    "serial_port": (
                        serial_port
                    ),
                    "serial_baudrate": (
                        serial_baudrate
                    ),
                    "driver_package": (
                        driver_package
                    ),
                    "driver_executable": (
                        driver_executable
                    ),
                    "base_frame": base_frame,
                }.items(),
            ),

            # -------------------------------------------------
            # 테스트용 fake odom -> base_link TF
            # -------------------------------------------------
            # 최종 runtime에서는 server/wheel_odometry.py가 dynamic TF를
            # 제공한다. 아래 static TF는 start_fake_odom:=true를 명시한
            # 독립 launch 테스트에서만 활성화한다.
            Node(
                package="tf2_ros",
                executable=(
                    "static_transform_publisher"
                ),
                name=(
                    "temporary_odom_to_base_tf"
                ),
                output="screen",
                condition=IfCondition(
                    start_fake_odom
                ),
                arguments=[
                    "--x",
                    "0",
                    "--y",
                    "0",
                    "--z",
                    "0",
                    "--roll",
                    "0",
                    "--pitch",
                    "0",
                    "--yaw",
                    "0",
                    "--frame-id",
                    odom_frame,
                    "--child-frame-id",
                    base_frame,
                ],
            ),

            # bridge가 map publish 전에 먼저 구독
            map_bridge_node,

            # localization lifecycle nodes
            map_server_node,
            amcl_node,
            lifecycle_manager_node,
        ]
    )