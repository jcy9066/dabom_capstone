"""Offline regression for identifying the first motion command of a Nav2 Goal."""

import threading
from types import SimpleNamespace

from server.navigation_ros_control import NavigationRosControl


def _twist(linear, angular):
    return SimpleNamespace(
        linear=SimpleNamespace(x=linear),
        angular=SimpleNamespace(z=angular),
    )


def test_nonzero_command_counter_does_not_arm_on_zero_or_invalid_twist():
    ros = NavigationRosControl.__new__(NavigationRosControl)
    ros._lock = threading.RLock()
    ros._nonzero_nav_command_sequence = 0
    ros._last_nav_command_at = None
    ros._last_nav_command_linear_x = None
    ros._last_nav_command_angular_z = None
    ros._rotation_command_since_at = None
    ros._navigate_action_kind = "navigate_to_pose"
    ros._navigation_state = "IDLE"

    ros._on_nav_command(_twist(0.0, 0.0))
    assert ros._nonzero_nav_command_sequence == 0

    ros._on_nav_command(_twist(-0.0636, 0.20))
    assert ros._nonzero_nav_command_sequence == 1

    ros._on_nav_command(_twist(0.0, 0.12))
    assert ros._nonzero_nav_command_sequence == 2

    ros._on_nav_command(_twist(float("nan"), 0.0))
    assert ros._nonzero_nav_command_sequence == 2
