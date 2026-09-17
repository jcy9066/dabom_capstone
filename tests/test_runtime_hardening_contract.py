from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_ros_bridges_do_not_shutdown_shared_rclpy_context():
    encoder = read("server/encoder_ros_bridge.py")
    lidar = read("server/lidar_ros_bridge.py")

    assert "rclpy.shutdown()" not in encoder
    assert "rclpy.shutdown()" not in lidar
    assert "executor.shutdown" in encoder
    assert "executor.shutdown" in lidar


def test_gpu_launcher_enforces_sensor_transport_contract():
    script = read("start_gpu_server.sh")

    assert '[[ "${ROS_LOCALHOST_ONLY}" == "1" ]]' in script
    assert '[[ "${ENCODER_ROS_TOPIC}" == "${WHEEL_TICKS_TOPIC}" ]]' in script
    assert 'ros2 topic echo "${WHEEL_TICKS_TOPIC}" --once' in script
    assert 'ros2 topic echo "${ODOM_TOPIC}" --once' in script
    assert 'stop_matching "ros2 run patrol_navigation map_bridge"' in script
    assert "restarting supervisor-owned worker" in script


def test_pi_launcher_requires_isolated_ros_and_fresh_encoder_feedback():
    script = read("start_pi_stack.sh")

    assert '[[ "${ROS_LOCALHOST_ONLY}" == "1" ]]' in script
    assert "WHEEL_TICKS_TOPIC" in script
    assert "/api/encoder/bridge" in script
    assert "fresh encoder telemetry" in script


def test_navigation_launch_control_disables_local_lidar_and_fake_odom():
    control = read("server/navigation_process_control.py")

    assert '"start_lidar:=false"' in control
    assert '"start_fake_odom:=false"' in control
    assert "_stop_legacy_map_bridges" in control
    assert '["run", "patrol_navigation", "map_bridge"]' in control


def test_dashboard_does_not_independently_control_supervisor_owned_workers():
    control = read("frontend/system_control.py")

    assert 'supervisor_managed_components = frozenset({"wheel_odometry", "map_bridge"})' in control
    assert '"control_available": control_available' in control
    assert "Mapping/Driving launch가 소유" in control


def test_gpu_lidar_bridge_supports_full_mounting_orientation():
    bridge = read("server/lidar_ros_bridge.py")

    assert "lidar_roll" in bridge
    assert "lidar_pitch" in bridge
    assert "quaternion_from_rpy" in bridge
    assert 'os.getenv("LIDAR_ROLL", "0")' in bridge
    assert 'os.getenv("LIDAR_PITCH", "0")' in bridge
