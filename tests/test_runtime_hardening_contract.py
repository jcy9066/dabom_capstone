import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def rclpy_shutdown_calls(path: str) -> list[ast.Call]:
    tree = ast.parse(read(path))
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "shutdown" or not isinstance(node.func.value, ast.Name):
            continue
        if node.func.value.id == "rclpy":
            calls.append(node)
    return calls


def test_ros_bridges_do_not_shutdown_shared_rclpy_context():
    encoder = read("server/encoder_ros_bridge.py")
    lidar = read("server/lidar_ros_bridge.py")

    assert rclpy_shutdown_calls("server/encoder_ros_bridge.py") == []
    assert rclpy_shutdown_calls("server/lidar_ros_bridge.py") == []
    assert "executor.shutdown" in encoder
    assert "executor.shutdown" in lidar


def test_gpu_launcher_enforces_sensor_transport_contract():
    script = read("start_gpu_server.sh")

    assert '[[ "${ROS_LOCALHOST_ONLY}" == "1" ]]' in script
    assert "LIDAR_ENABLE must be enabled for the final runtime" in script
    assert "ENCODER_ROS_ENABLE must be enabled for the final runtime" in script
    assert "LIDAR_ROS_TOPIC" in script
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


def test_navigation_launch_control_uses_live_rc_sensor_bridges():
    control = read("server/navigation_process_control.py")

    assert '"start_lidar:=false"' in control
    assert "start_fake_odom" not in control
    assert "LidarRosBridge" in control
    assert "live RC encoder telemetry" in control
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


def test_navigation_launches_have_no_test_data_fallbacks():
    launch_paths = (
        "navigation/ros/patrol_navigation/launch/mapping.launch.py",
        "navigation/ros/patrol_navigation/launch/localization.launch.py",
        "navigation/ros/patrol_navigation/launch/navigation.launch.py",
    )

    for path in launch_paths:
        content = read(path)
        assert "start_fake_odom" not in content
        assert "temporary_odom_to_base_tf" not in content
        assert "slam_test_01" not in content
        assert "test_map" not in content

    localization = read(launch_paths[1])
    navigation = read(launch_paths[2])

    assert 'DeclareLaunchArgument(\n                "map",\n                description=' in localization
    assert 'DeclareLaunchArgument(\n                "map",\n                description=' in navigation
    assert "No bundled test-map default is used." in localization
    assert "No bundled test-map default is used." in navigation


def test_mapping_bridge_streams_live_viewer_data_without_startup_blackout():
    mapping = read("navigation/ros/patrol_navigation/launch/mapping.launch.py")
    slam = read("navigation/ros/patrol_navigation/config/slam_toolbox.yaml")
    dashboard = read("frontend/services/static/script.js")
    app = read("server/app.py")

    assert "TimerAction" not in mapping
    assert '"map_publish_period_sec": 0.0' in mapping
    assert '"pose_publish_period_sec": 0.05' in mapping
    assert '"scan_publish_period_sec": 0.0' in mapping
    assert '"send_map": True' in mapping
    assert '"send_pose": True' in mapping
    assert '"send_scan": False' in mapping
    assert "minimum_time_interval: 0.1" in slam
    assert "minimum_travel_distance: 0.02" in slam
    assert "minimum_travel_heading: 0.02" in slam
    assert "map_update_interval: 1.0" in slam
    live_mapper = read("server/live_mapping_grid.py")
    assert "class LiveMappingGrid:" in live_mapper
    assert "def update_base_map(self, payload" in live_mapper
    assert "refresh_sec: float = 1.0" in live_mapper
    assert "live_mapping_grid.update_base_map(data)" in app
    assert "publish_live_mapping_map(live_map_payload" in app
    assert "const NAVIGATION_SNAPSHOT_VISIBLE_MS = 1000;" in dashboard
    assert '"/ws/navigation/visualization"' in app
    assert '"type": "map"' in app
    assert '"type": "pose"' in app
    assert '"type": "scan"' in app
    assert 'navigation_visualization_hub.publish(' in app

def test_lidar_websocket_tags_scans_with_active_navigation_mode():
    app = read("server/app.py")

    assert "control_navigation_mode = str(" in app
    assert '"MAPPING": "mapping"' in app
    assert '"DRIVING": "localization_nav2"' in app
    assert 'dashboard_scan["navigation_mode"] = sensor_navigation_mode' in app
    assert "store_navigation_mode(" in app

def test_ros_lidar_mount_yaw_matches_viewer_orientation():
    bridge = read("server/lidar_ros_bridge.py")
    launch = read("navigation/ros/patrol_navigation/launch/lidar.launch.py")
    env = read(".env.example")

    assert "if abs(configured_lidar_yaw) < 1e-9" in bridge
    assert "math.pi" in bridge
    assert 'DeclareLaunchArgument("laser_yaw", default_value="3.141592653589793")' in launch
    assert "LIDAR_YAW=3.141592653589793" in env
