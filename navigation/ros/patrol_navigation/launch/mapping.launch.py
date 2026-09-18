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
# 최종 runtime에서는 root start_gpu_server.sh가 server/wheel_odometry.py를 실행하고
# /wheel_ticks를 입력으로 /odom과 dynamic odom->base_link TF를 생성한다.
# fake odometry는 launch 단독 테스트용으로만 유지하며 기본값은 비활성화한다.
# 필요한 경우에만 start_fake_odom:=true를 명시적으로 전달한다.

from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    IncludeLaunchDescription,
    TimerAction,
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
    start_fake_odom = LaunchConfiguration("start_fake_odom")
    start_rviz = LaunchConfiguration("start_rviz")

    # TF frame
    odom_frame = LaunchConfiguration("odom_frame")
    base_frame = LaunchConfiguration("base_frame")

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
    # slam_toolbox가 활성화되고 /map 및 TF를 생성할 시간을 주기
    # 위해 8초 뒤 실행한다.
    map_bridge_node = TimerAction(
        period=8.0,
        actions=[
            Node(
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
                    "map_publish_period_sec": 2.0,
                    "pose_publish_period_sec": 0.5,
                    "scan_publish_period_sec": 0.2,
                    "request_timeout_sec": 5.0,

                    # 서버로 전송할 데이터
                    "send_map": True,
                    "send_pose": True,
                    "send_scan": True,
                }],
            )
        ],
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
            "start_fake_odom",
            default_value="false",
        ),
        DeclareLaunchArgument(
            "start_rviz",
            default_value="false",
        ),

        # TF frame
        DeclareLaunchArgument(
            "odom_frame",
            default_value="odom",
        ),
        DeclareLaunchArgument(
            "base_frame",
            default_value="base_link",
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
        # 임시 odom -> base_link TF
        # -----------------------------------------------------
        #
        # encoder/wheel odometry 없이 launch만 독립 테스트할 때 사용하는
        # opt-in fallback이다. 최종 runtime에서는 비활성화되며
        # server/wheel_odometry.py의 dynamic odom->base_link TF만 사용한다.
        Node(
            package="tf2_ros",
            executable="static_transform_publisher",
            name="temporary_odom_to_base_tf",
            output="screen",
            condition=IfCondition(start_fake_odom),
            arguments=[
                "--x", "0",
                "--y", "0",
                "--z", "0",
                "--roll", "0",
                "--pitch", "0",
                "--yaw", "0",
                "--frame-id", odom_frame,
                "--child-frame-id", base_frame,
            ],
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