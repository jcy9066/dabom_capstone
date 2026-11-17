"""Empirical wheel-odometry yaw calibration; no ROS2 required."""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from server.odometry_calibration import (
    DEFAULT_ODOMETRY_TRACK_MULTIPLIER,
    effective_odometry_track_m,
)


ROOT = Path(__file__).resolve().parents[1]


def test_observed_thirty_degree_undershoot_reduces_encoder_yaw_to_physical_turn():
    nominal_track = 0.201
    effective = effective_odometry_track_m(nominal_track)
    assert DEFAULT_ODOMETRY_TRACK_MULTIPLIER == pytest.approx(1.204)
    assert effective == pytest.approx(0.242004)
    # Reuse exactly the wheel-displacement difference that previously
    # yielded 180.6deg: only the calibrated denominator may change.
    observed_wheel_difference = math.radians(180.6) * nominal_track
    predicted_yaw = observed_wheel_difference / effective
    assert math.degrees(predicted_yaw) == pytest.approx(150.0)


def test_calibration_does_not_change_translation_or_nominal_motor_geometry():
    nominal_track = 0.201
    forward_left = 0.10
    forward_right = 0.10
    # Equal wheel displacement must still produce no yaw and unchanged XY.
    effective = effective_odometry_track_m(nominal_track)
    assert (forward_right - forward_left) / effective == pytest.approx(0.0)
    assert (forward_right + forward_left) / 2 == pytest.approx(0.10)
    assert nominal_track == pytest.approx(0.201)


@pytest.mark.parametrize("bad", [-1.0, 0.0, math.nan, math.inf, -math.inf])
def test_invalid_multiplier_fails_closed(bad):
    with pytest.raises(ValueError, match="finite and positive"):
        effective_odometry_track_m(0.201, bad)


@pytest.mark.parametrize("bad", [0.0, -0.2, math.nan, math.inf])
def test_invalid_track_fails_closed(bad):
    with pytest.raises(ValueError, match="finite and positive"):
        effective_odometry_track_m(bad)


def test_runtime_only_calibrates_wheel_odometry_not_nav2_or_motor_wheel_track():
    runner = (ROOT / "server/wheel_odometry.py").read_text()
    nav = (
        ROOT / "navigation/ros/patrol_navigation/launch/navigation.launch.py"
    ).read_text()
    assert 'env_float("WHEEL_TRACK_M", minimum=0.001)' in runner
    assert 'env_float("ODOMETRY_TRACK_MULTIPLIER", minimum=0.001)' in runner
    assert "wheel_track_m=effective_track_m," in runner
    assert '"WHEEL_TRACK_M"' in nav
    assert "ODOMETRY_TRACK_MULTIPLIER" not in nav
