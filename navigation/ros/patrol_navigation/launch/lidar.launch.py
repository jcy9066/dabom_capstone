# 실제 SLAMTEC RPLIDAR A1M8-R6를 실행하는 ROS2 launch 파일.
#
# 이 파일은 LiDAR driver와 로봇 본체 기준 좌표계(base_link)에서
# LiDAR 좌표계(laser)까지의 고정 transform만 담당한다.
# 전체 mapping pipeline은 이 파일을 포함하는 mapping.launch.py에서 실행한다.

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    # USB serial 연결 옵션이다. Pi에서 LiDAR가 다른 장치명으로 잡히면 serial_port를 바꿔 실행한다.
    serial_port = LaunchConfiguration("serial_port")
    serial_baudrate = LaunchConfiguration("serial_baudrate")

    # LaserScan 메시지에 붙는 ROS frame 이름이다. slam_toolbox가 이 frame을 TF에서 찾는다.
    frame_id = LaunchConfiguration("frame_id")

    # rplidar_ros 배포/설치 방식이 달라질 수 있어 package와 executable도 바꿀 수 있게 둔다.
    driver_package = LaunchConfiguration("driver_package")
    driver_executable = LaunchConfiguration("driver_executable")
    scan_mode = LaunchConfiguration("scan_mode")
    inverted = LaunchConfiguration("inverted")
    angle_compensate = LaunchConfiguration("angle_compensate")

    # 로봇 본체 중심(base_link)에서 LiDAR 장착 위치(laser)까지의 고정 transform이다.
    # 센서가 중심에서 벗어나 있거나 회전되어 장착되어 있으면 이 값을 조정해야 한다.
    laser_x = LaunchConfiguration("laser_x")
    laser_y = LaunchConfiguration("laser_y")
    laser_z = LaunchConfiguration("laser_z")
    laser_roll = LaunchConfiguration("laser_roll")
    laser_pitch = LaunchConfiguration("laser_pitch")
    laser_yaw = LaunchConfiguration("laser_yaw")
    base_frame = LaunchConfiguration("base_frame")

    return LaunchDescription([
        # RPLIDAR A1M8-R6는 실제 테스트에서 /dev/ttyUSB0, 115200 baud로 동작했다.
        DeclareLaunchArgument("serial_port", default_value="/dev/ttyUSB0"),
        DeclareLaunchArgument("serial_baudrate", default_value="115200"),
        DeclareLaunchArgument("frame_id", default_value="laser"),

        # rplidar_ros driver node 설정이다.
        DeclareLaunchArgument("driver_package", default_value="rplidar_ros"),
        DeclareLaunchArgument("driver_executable", default_value="rplidar_composition"),
        DeclareLaunchArgument("scan_mode", default_value="Sensitivity"),
        DeclareLaunchArgument("inverted", default_value="false"),
        DeclareLaunchArgument("angle_compensate", default_value="true"),

        # base_link->laser static transform 기본값이다.
        DeclareLaunchArgument("base_frame", default_value="base_link"),
        DeclareLaunchArgument("laser_x", default_value="0.0"),
        DeclareLaunchArgument("laser_y", default_value="0.0"),
        DeclareLaunchArgument("laser_z", default_value="0.12"),
        DeclareLaunchArgument("laser_roll", default_value="0.0"),
        DeclareLaunchArgument("laser_pitch", default_value="0.0"),
        # Physical RPLIDAR mount faces 180 degrees from base_link +X.
        DeclareLaunchArgument("laser_yaw", default_value="3.141592653589793"),

        # 실제 LiDAR driver다. USB serial 데이터를 읽고 sensor_msgs/LaserScan을 /scan으로 publish한다.
        Node(
            package=driver_package,
            executable=driver_executable,
            name="rplidar_node",
            output="screen",
            parameters=[{
                "channel_type": "serial",
                "serial_port": serial_port,
                "serial_baudrate": ParameterValue(serial_baudrate, value_type=int),
                "frame_id": frame_id,
                "inverted": ParameterValue(inverted, value_type=bool),
                "angle_compensate": ParameterValue(angle_compensate, value_type=bool),
                "scan_mode": scan_mode,
            }],
        ),

        # LiDAR가 로봇 본체 기준 어디에 장착되어 있는지 ROS TF에 알려주는 고정 transform이다.
        # 이 executable의 argument 순서는 x y z yaw pitch roll parent_frame child_frame 이다.
        Node(
            package="tf2_ros",
            executable="static_transform_publisher",
            name="base_to_laser_tf",
            output="screen",
            arguments=[
                laser_x,
                laser_y,
                laser_z,
                laser_yaw,
                laser_pitch,
                laser_roll,
                base_frame,
                frame_id,
            ],
        ),
    ])
