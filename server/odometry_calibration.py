"""ROS-independent empirical yaw calibration for four-wheel skid-steer odometry.

The nominal chassis WHEEL_TRACK_M remains unchanged for motor kinematics,
Nav2 limits and planner scoring. This effective track applies ONLY to the
encoder-derived /odom yaw and angular velocity.
"""
from __future__ import annotations

import math

# One observed 180-degree turn: encoder odometry 180.6 deg, physical ~150 deg.
# Increasing ONLY the odometry effective track by 180.6/150 scales
# the predicted yaw by 150/180.6 (~0.8306).
DEFAULT_ODOMETRY_TRACK_MULTIPLIER = 180.6 / 150.0


def effective_odometry_track_m(
    nominal_track_m: float,
    multiplier: float = DEFAULT_ODOMETRY_TRACK_MULTIPLIER,
) -> float:
    """Return calibrated divisor for (right_wheel_m - left_wheel_m)."""
    track = float(nominal_track_m)
    ratio = float(multiplier)
    effective = track * ratio
    if (not math.isfinite(track) or not math.isfinite(ratio)
            or not math.isfinite(effective) or track <= 0 or ratio <= 0):
        raise ValueError("Odometry nominal track and multiplier must be finite and positive")
    return effective
