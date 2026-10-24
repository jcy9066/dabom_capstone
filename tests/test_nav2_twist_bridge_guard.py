"""Pure Python tests for the Nav2 bridge's six-axis command gate.

Import the helper directly by file path so it runs without ROS2/rclpy.
"""
from __future__ import annotations

import importlib.util
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUARD = (
    ROOT / "navigation/ros/patrol_navigation/patrol_navigation"
    / "nav2_twist_guard.py"
)
BRIDGE = (
    ROOT / "navigation/ros/patrol_navigation/patrol_navigation"
    / "nav2_command_bridge.py"
)


def _validator():
    spec = importlib.util.spec_from_file_location("nav90_twist_guard", GUARD)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.valid_differential_twist


def test_valid_differential_twist_allows_only_forward_and_yaw():
    valid = _validator()
    assert valid(0, 0, 0, 0, 0, 0)
    assert valid(0.212, 0, 0, 0, 0, 1.05)
    assert valid(-0.0636, 0, 0, 0, 0, -1.05)
    assert valid(0, 1e-10, 0, 0, 0, 0)


def test_invalid_differential_twist_rejects_each_unsupported_axis():
    valid = _validator()
    for i in (1, 2, 3, 4):
        axes = [0.0] * 6
        axes[i] = 0.001
        assert not valid(*axes), f"unsupported axis {i}"
        axes[i] = -0.001
        assert not valid(*axes), f"unsupported axis {i}"


def test_invalid_differential_twist_rejects_nonfinite_and_malformed():
    valid = _validator()
    for i in range(6):
        for value in (math.nan, math.inf, -math.inf, "not a number", None):
            axes = [0.0] * 6
            axes[i] = value
            assert not valid(*axes), f"invalid axis {i}, value={value!r}"


def test_nav2_command_bridge_guards_full_twist_before_drive():
    source = BRIDGE.read_text()
    start = source.index("    def on_twist(")
    end = source.index("    def check_timeout(", start)
    callback = source[start:end]
    assert "from patrol_navigation.nav2_twist_guard import valid_differential_twist" in source
    assert "if not valid_differential_twist(" in callback
    for component in ("msg.linear.y", "msg.linear.z", "msg.angular.x", "msg.angular.y"):
        assert component in callback
    assert callback.index("if not valid_differential_twist(") < callback.index(
        "rear_reverse_guard.limit_reverse("
    )
    assert callback.index("if not valid_differential_twist(") < callback.index(
        '"type": "auto_drive"'
    )
    assert 'self._queue_stop("nav2_invalid_twist")' in callback
