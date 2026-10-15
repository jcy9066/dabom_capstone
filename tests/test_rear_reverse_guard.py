"""Hardware-free reverse blind-zone safety regressions."""
from __future__ import annotations

import math
import os
import sys
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "navigation/ros/patrol_navigation"))
from patrol_navigation.rear_reverse_guard import RearReverseGuard

os.environ.setdefault("MIN_AUTO_DRIVE_PWM", "0.50")
from raspberry.controllers.motor_controller import MotorController


def _guard_with_odom() -> RearReverseGuard:
    guard = RearReverseGuard()
    guard.update_odometry(0.0, 0.0, 0.0, 0.0)
    return guard


def _rear_scan(guard: RearReverseGuard, now: float, *, obstacle=None, yaw=0.0):
    ranges = [math.inf] * 360
    if obstacle is not None:
        ranges[0] = obstacle
    guard.update_scan(
        ranges, angle_min=-math.pi, angle_increment=math.pi / 180,
        range_min=0.10, range_max=8.0, laser_yaw=yaw, now=now,
    )


def test_rear_fresh_three_sector_observation_allows_normal_reverse():
    guard = _guard_with_odom()
    _rear_scan(guard, now=0.01)
    velocity, limited, reason = guard.limit_reverse(
        -0.20, max_wheel_mps=0.212, now=0.05,
    )
    assert (velocity, limited, reason) == (-0.20, False, None)


def test_rear_blind_reverse_is_30_percent_and_one_0p3m_segment():
    guard = _guard_with_odom()
    velocity, limited, reason = guard.limit_reverse(
        -0.20, max_wheel_mps=0.212, now=0.01,
    )
    assert velocity == pytest.approx(-0.212 * 0.3)
    assert limited is True
    assert reason is None
    guard.update_odometry(-0.265, 0.0, 0.0, 0.10)
    assert guard.blind_distance_m == pytest.approx(0.265)
    guard.update_odometry(-0.28, 0.0, 0.0, 0.20)
    velocity, limited, reason = guard.limit_reverse(
        -0.2, max_wheel_mps=0.212, now=0.21,
    )
    assert velocity == 0.0
    assert reason == "rear_blind_distance_exhausted"
    # Additional blind reverse remains blocked; forward motion alone
    # is not an authorization for a second blind segment.
    guard.limit_reverse(0.1, max_wheel_mps=0.212, now=0.22)
    assert guard.limit_reverse(
        -0.2, max_wheel_mps=0.212, now=0.23,
    )[2] == "rear_blind_distance_exhausted"
    _rear_scan(guard, now=0.24)
    assert guard.limit_reverse(
        -0.2, max_wheel_mps=0.212, now=0.25,
    ) == (-0.2, False, None)


def test_rear_observed_obstacle_blocks_even_with_full_coverage():
    guard = _guard_with_odom()
    _rear_scan(guard, now=0.02, obstacle=0.18)
    speed, limited, reason = guard.limit_reverse(
        -0.10, max_wheel_mps=0.212, now=0.03,
    )
    assert speed == 0.0
    assert limited is True
    assert reason == "rear_obstacle_in_stop_distance"


def test_failed_scan_transform_cannot_prove_rear_clear():
    guard = _guard_with_odom()
    _rear_scan(guard, now=0.01, yaw=None)
    speed, limited, reason = guard.limit_reverse(
        -0.20, max_wheel_mps=0.212, now=0.02,
    )
    assert speed == pytest.approx(-0.0636)
    assert limited and reason is None


def test_stale_odometry_blocks_blind_reverse():
    guard = _guard_with_odom()
    speed, limited, reason = guard.limit_reverse(
        -0.15, max_wheel_mps=0.212, now=0.4,
    )
    assert (speed, limited, reason) == (
        0.0, True, "rear_reverse_odom_unavailable",
    )


def test_rear_odom_recovery_requires_fresh_clear_observation():
    guard = _guard_with_odom()
    assert guard.limit_reverse(
        -0.1, max_wheel_mps=0.212, now=0.01,
    )[2] is None
    guard.update_odometry(-0.02, 0.0, 0.0, 0.5)
    assert guard.limit_reverse(
        -0.1, max_wheel_mps=0.212, now=0.51,
    )[2] == "rear_reverse_odom_unavailable"
    guard.update_odometry(-0.03, 0.0, 0.0, 0.6)
    assert guard.limit_reverse(
        -0.1, max_wheel_mps=0.212, now=0.61,
    )[2] == "rear_reverse_odom_unavailable"
    _rear_scan(guard, now=0.62)
    guard.update_odometry(-0.03, 0.0, 0.0, 0.65)
    assert guard.limit_reverse(
        -0.1, max_wheel_mps=0.212, now=0.66,
    ) == (-0.1, False, None)


def test_pi_pwm_output_never_exceeds_30_percent_when_blind():
    motor = MotorController.__new__(MotorController)
    motor.max_wheel_mps = 0.212
    motor._command_lock = threading.RLock()
    motor._state_lock = threading.RLock()
    motor._ensure_connected_locked = lambda: None
    commands = []
    motor._exchange_locked = commands.append
    motor._jerk_limiter = Mock()
    motor._jerk_limiter.step.return_value = (0.8, -0.4)
    motor.drive(0.15, -0.12, True)
    left, right = map(float, commands[0].split(",")[1:])
    assert max(abs(left), abs(right)) <= 0.30001
    assert motor._jerk_limiter.reset.called


def test_pi_blind_pwm_cap_is_below_breakaway_floor():
    motor = MotorController.__new__(MotorController)
    motor.max_wheel_mps = 0.212
    motor._command_lock = threading.RLock()
    motor._state_lock = threading.RLock()
    motor._ensure_connected_locked = lambda: None
    commands = []
    motor._exchange_locked = commands.append
    motor._jerk_limiter = Mock()
    motor._jerk_limiter.step.side_effect = lambda left, right, _: (left, right)
    motor.drive(-0.025, -0.025, True)
    left, right = map(float, commands[0].split(",")[1:])
    assert max(abs(left), abs(right)) < 0.30
