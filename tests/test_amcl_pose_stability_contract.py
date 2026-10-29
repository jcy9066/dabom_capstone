"""Protect AMCL publication and convergence wiring independently of ROS imports."""
from pathlib import Path


def test_amcl_stability_is_gated_after_initial_pose_published():
    ros = (Path(__file__).resolve().parents[1] / "server/navigation_ros_control.py").read_text(encoding="utf-8")
    assert "self._amcl_history.clear()" in ros
    assert 'self._amcl_initial_pose_after = time.monotonic()' in ros
    assert 'item["at"] >= self._amcl_initial_pose_after' in ros
    assert "stable_amcl_observations(recent, initial_pose)" in ros
    assert 'max(float(covariance[0]), float(covariance[7]))' in ros
    assert "yaw_variance = float(covariance[35])" in ros
    assert '"stable_samples": len(recent)' in ros
    wait_for_amcl = ros.split("def _wait_for_amcl(", 1)[1].split("def _publish_initial_pose(", 1)[0]
    assert "minimum_samples=1" not in wait_for_amcl


def test_no_amcl_stable_check_bypass_to_driving():
    ros = (Path(__file__).resolve().parents[1] / "server/navigation_ros_control.py").read_text(encoding="utf-8")
    start = ros.index("    def load_map_and_reset_pose(")
    end = ros.index("    def active_status(", start)
    transition = ros[start:end]
    assert "_wait_for_amcl(initial_pose" in transition
    assert "stable_samples" not in transition  # confirmed inside wait helper
    control = (Path(__file__).resolve().parents[1] / "server/navigation_control_api.py").read_text(encoding="utf-8")
    assert "await self._activate_map_when_ready(" in control
    assert "await self._wait_for_nav2_ready()" in control


def test_amcl_history_handles_100hz_stability_window():
    ros = (Path(__file__).resolve().parents[1] / "server/navigation_ros_control.py").read_text(encoding="utf-8")
    assert "self._amcl_history = deque(maxlen=128)" in ros
    assert "stable_amcl_observations(recent, initial_pose)" in ros


def test_stationary_one_sample_still_checks_covariance():
    import math
    from server.amcl_pose_stability import stable_amcl_observations
    initial = {"x": 0.0, "y": 0.0, "yaw": 0.0}
    sample = {"at": 10.0, "x": 0.02, "y": 0.01, "yaw": 0.02,
              "xy_variance": 0.05, "yaw_variance": math.radians(5) ** 2}
    assert stable_amcl_observations([sample], initial, minimum_samples=1, minimum_span=0.0)
    assert not stable_amcl_observations([sample], initial)
    sample["yaw_variance"] = math.radians(50) ** 2
    assert not stable_amcl_observations([sample], initial, minimum_samples=1, minimum_span=0.0)

def test_amcl_initialization_never_accepts_single_echo():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    ros = (root / "server/navigation_ros_control.py").read_text()
    config = (root / "navigation/ros/patrol_navigation/config/amcl.yaml").read_text()
    body = ros.split("def _wait_for_amcl(", 1)[1].split("def _publish_initial_pose(", 1)[0]
    assert "minimum_samples=1" not in body
    assert "if stable:" in body
    # Match YAML keys, not substrings of always_reset_initial_pose.
    lines = [line.strip() for line in config.splitlines()]
    assert "set_initial_pose: false" in lines
    assert "set_initial_pose: true" not in lines


def test_amcl_map_fingerprint_uses_occupancy_not_only_dimensions(tmp_path):
    import hashlib
    import json
    from server.navigation_ros_control import _saved_map_occupancy_digest

    path = tmp_path / "map.raw.json"
    path.write_text(json.dumps({
        "width": 2, "height": 2, "data_encoding": "raw",
        "data": [-1, 0, 100, 40],
    }), encoding="utf-8")
    info = path.stat()
    actual = _saved_map_occupancy_digest(
        str(path), 2, 2, info.st_size, info.st_mtime_ns,
    )
    expected = hashlib.sha256(bytes([0, 1, 101, 0])).hexdigest()
    assert actual == expected
    assert actual != hashlib.sha256(bytes([1, 1, 101, 0])).hexdigest()
