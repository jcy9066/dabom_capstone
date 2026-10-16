"""Hardware-independent fail-closed validation of differential-drive ROS2 Twists."""
from __future__ import annotations

import math


def valid_differential_twist(
    linear_x: float,
    linear_y: float,
    linear_z: float,
    angular_x: float,
    angular_y: float,
    angular_z: float,
) -> bool:
    """A 2D differential drive can only execute linear.x and angular.z.

    Treat any non-finite or unsupported non-zero component as invalid,
    including commands otherwise misidentified as a zero-speed STOP.
    """
    values = (
        linear_x, linear_y, linear_z, angular_x, angular_y, angular_z,
    )
    try:
        checked = tuple(float(value) for value in values)
    except (TypeError, ValueError, OverflowError):
        return False
    if not all(math.isfinite(value) for value in checked):
        return False
    epsilon = 1e-9
    return all(abs(value) <= epsilon for value in (
        checked[1], checked[2], checked[3], checked[4],
    ))
