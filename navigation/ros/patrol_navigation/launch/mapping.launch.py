# 전체 LiDAR mapping pipeline을 실행하는 ROS 2 launch 파일.
#
# 실행 구조:
#   SLLIDAR driver
#       -> /scan
#       -> slam_toolbox
#       -> /map + map->odom TF
#       -> map_bridge
#       -> FastAPI server
#
# 최종 runtime에서는 RC카의 실제 encoder가
# Pi -> WebSocket -> EncoderRosBridge -> /wheel_ticks로 들어오고,
# root start_gpu_server.sh의 server/wheel_odometry.py가
# /odom과 dynamic odom->base_link TF를 생성한다.
# static/fake odometry fallback은 사용하지 않는다.

from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    IncludeLaunchDescription,
)
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution

from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    # patrol_navigation 패키지 설치 경로
    pkg_share = FindPackageShare("patrol_navigation")

    # 패키지 내부 LiDAR launch 파일
    lidar_launch = PathJoinSubstitution(
        [pkg_share, "launch", "lidar.launch.py"]
    )

    # 공통 실행 인자
    slam_config = LaunchConfiguration("slam_config")
    server_base_url = LaunchConfiguration("server_base_url")
    robot_id = LaunchConfiguration("robot_id")
    use_sim_time = LaunchConfiguration("use_sim_time")

    # 실행 여부 제어
    start_lidar = LaunchConfiguration("start_lidar")
    start_bridge = LaunchConfiguration("start_bridge")
    start_rviz = LaunchConfiguration("start_rviz")

    # RViz 설정
    rviz_config = LaunchConfiguration("rviz_config")

    # ---------------------------------------------------------
    # 1. slam_toolbox
    # ---------------------------------------------------------
    #
    # slam_toolbox 공식 online async launch를 사용한다.
    # 내부에서 LifecycleNode를 생성하고 configure -> activate
    # lifecycle 전환을 자동으로 처리한다.
    slam_toolbox_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            PathJoinSubstitution([
                FindPackageShare("slam_toolbox"),
                "launch",
                "online_async_launch.py",
            ])
        ),
        launch_arguments={
            "slam_params_file": slam_config,
            "use_sim_time": use_sim_time,
            "autostart": "true",
            "use_lifecycle_manager": "false",
        }.items(),
    )

    # ---------------------------------------------------------
    # 2. map_bridge
    # ---------------------------------------------------------
    #
    # Start immediately. It can stream /scan before the first /map or
    # map->base_link TF exists; missing map/TF is handled by map_bridge itself.
    # This keeps the dashboard 3D Viewer live from the beginning of Mapping.
    map_bridge_node = Node(
        package="patrol_navigation",
        executable="map_bridge",
        name="map_bridge",
        output="screen",
        condition=IfCondition(start_bridge),
        parameters=[{
            "robot_id": robot_id,
            "server_base_url": server_base_url,

            # ROS topic
            "map_topic": "/map",
            "scan_topic": "/scan",

            # 로봇 위치를 읽을 TF
            "pose_parent_frame": "map",
            "pose_child_frame": "base_link",

            # 서버 전송 주기
            # RViz2처럼 live LaserScan은 Pi WebSocket 최신값을 사용하고,
            # pose는 10 Hz, SLAM OccupancyGrid는 2 Hz로 갱신한다.
            "map_publish_period_sec": 0.5,
            "pose_publish_period_sec": 0.1,
            "scan_publish_period_sec": 0.0,
            "request_timeout_sec": 5.0,

            # 서버로 전송할 데이터
            "send_map": True,
            "send_pose": True,
            "send_scan": False,
        }],
    )

    return LaunchDescription([
        # -----------------------------------------------------
        # Launch arguments
        # -----------------------------------------------------
        DeclareLaunchArgument(
            "robot_id",
            default_value="pi-01",
        ),
        DeclareLaunchArgument(
            "server_base_url",
            default_value="http://127.0.0.1:21063",
        ),

        # 각 노드 실행 여부
        DeclareLaunchArgument(
            "start_lidar",
            default_value="true",
        ),
        DeclareLaunchArgument(
            "start_bridge",
            default_value="true",
        ),
        DeclareLaunchArgument(
            "start_rviz",
            default_value="false",
        ),

        DeclareLaunchArgument(
            "use_sim_time",
            default_value="false",
        ),

        # slam_toolbox 설정 파일
        DeclareLaunchArgument(
            "slam_config",
            default_value=PathJoinSubstitution([
                pkg_share,
                "config",
                "slam_toolbox.yaml",
            ]),
        ),

        # RViz 설정 파일
        DeclareLaunchArgument(
            "rviz_config",
            default_value=PathJoinSubstitution([
                pkg_share,
                "rviz",
                "mapping.rviz",
            ]),
        ),

        # LiDAR 직렬 연결 설정
        DeclareLaunchArgument(
            "serial_port",
            default_value="/dev/ttyUSB0",
        ),
        DeclareLaunchArgument(
            "serial_baudrate",
            default_value="115200",
        ),

        # -----------------------------------------------------
        # LiDAR driver + base_link -> laser TF
        # -----------------------------------------------------
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(lidar_launch),
            condition=IfCondition(start_lidar),
            launch_arguments={
                "serial_port": LaunchConfiguration("serial_port"),
                "serial_baudrate": LaunchConfiguration(
                    "serial_baudrate"
                ),
            }.items(),
        ),

        # -----------------------------------------------------
        # SLAM
        # -----------------------------------------------------
        slam_toolbox_launch,

        # -----------------------------------------------------
        # FastAPI bridge
        # -----------------------------------------------------
        map_bridge_node,

        # -----------------------------------------------------
        # RViz
        # -----------------------------------------------------
        Node(
            package="rviz2",
            executable="rviz2",
            name="mapping_rviz",
            output="screen",
            condition=IfCondition(start_rviz),
            arguments=[
                "-d",
                rviz_config,
            ],
        ),
    ])