"""Hardware-free contracts for the 32-heading Nav2 lattice and Pi jerk filter."""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
import yaml

from raspberry.controllers.jerk_limiter import WheelJerkLimiter


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "navigation/ros/patrol_navigation/config"


def test_lattice_asset_is_installed_with_python_nav_package():
    setup_py = (ROOT / "navigation/ros/patrol_navigation/setup.py").read_text()
    assert 'glob("config/*.json")' in setup_py
    assert (CONFIG / "lattice_diff32.json").exists()


def test_lattice_has_uniform_32_headings_and_diff_motion():
    lattice = json.loads((CONFIG / "lattice_diff32.json").read_text())
    meta = lattice["lattice_metadata"]
    headings = meta["heading_angles"]
    assert meta["num_of_headings"] == 32
    assert meta["motion_model"] == "diff"
    assert meta["number_of_trajectories"] == len(lattice["primitives"]) == 160
    for index, heading in enumerate(headings):
        assert math.isclose(heading, index * math.pi / 16, abs_tol=2e-8)

    for heading in range(32):
        group = [p for p in lattice["primitives"]
                 if p["start_angle_index"] == heading]
        assert len(group) == 5
        assert all(p["poses"] and p["poses"][0][:2] == [0, 0]
                   for p in group)
        assert sum(p["trajectory_length"] == 0 for p in group) == 2
        assert any(p["start_angle_index"] == p["end_angle_index"]
                   and p["trajectory_length"] > 0 for p in group)



def test_lattice_both_assets_match_humble_json_reader_schema():
    """Upstream Humble nav2_smac_planner/utils.hpp uses json.at on all keys."""
    required_meta = {
        "turning_radius", "grid_resolution", "num_of_headings",
        "heading_angles", "number_of_trajectories", "motion_model",
    }
    required_primitive = {
        "trajectory_id", "start_angle_index", "end_angle_index",
        "trajectory_radius", "trajectory_length", "arc_length",
        "straight_length", "left_turn", "poses",
    }
    originals = [
        json.loads((CONFIG / name).read_text())
        for name in ("lattice_diff32.json", "lattice_diff32_stable.json")
    ]
    assert originals[0] == originals[1]
    for payload in originals:
        meta = payload["lattice_metadata"]
        primitives = payload["primitives"]
        assert required_meta <= set(meta)
        assert meta["number_of_trajectories"] == len(primitives) == 160
        assert meta["num_of_headings"] == len(meta["heading_angles"]) == 32
        assert all(required_primitive <= set(item) for item in primitives)
        assert all(
            len(item["poses"]) >= 2
            and all(len(pose) == 3 for pose in item["poses"])
            for item in primitives
        )


def test_lattice_primitives_start_and_finish_in_declared_heading_bins():
    """Reject primitive data that loads but cannot produce a valid 32-bin path."""
    payload = json.loads((CONFIG / "lattice_diff32.json").read_text())
    headings = payload["lattice_metadata"]["heading_angles"]
    for primitive in payload["primitives"]:
        start, end = primitive["poses"][0], primitive["poses"][-1]
        expected_start = headings[primitive["start_angle_index"]]
        expected_end = headings[primitive["end_angle_index"]]
        deviation_start = math.atan2(
            math.sin(start[2] - expected_start),
            math.cos(start[2] - expected_start),
        )
        deviation_end = math.atan2(
            math.sin(end[2] - expected_end),
            math.cos(end[2] - expected_end),
        )
        assert abs(deviation_start) < 1e-6
        assert abs(deviation_end) < 1e-6
        assert math.hypot(start[0], start[1]) < 1e-6
        assert primitive["trajectory_length"] >= 0
        if primitive["trajectory_length"] == 0:
            assert all(math.hypot(x, y) < 1e-6
                       for x, y, _ in primitive["poses"])
        else:
            assert math.hypot(end[0], end[1]) >= 0.05

def test_chassis_footprint_matches_both_costmaps():
    params = yaml.safe_load((CONFIG / "nav2_params.yaml").read_text())
    for name in ("local_costmap", "global_costmap"):
        config = params[name][name]["ros__parameters"]
        points = json.loads(config["footprint"])
        assert len(points) == 4
        assert {p[0] for p in points} == {-0.14, 0.14}
        assert {p[1] for p in points} == {-0.105, 0.105}
        assert config["footprint_padding"] == pytest.approx(0.03)


def test_planner_and_rpp_humble_config_contract():
    params = yaml.safe_load((CONFIG / "nav2_params.yaml").read_text())
    planner = params["planner_server"]["ros__parameters"]["GridBased"]
    controller = params["controller_server"]["ros__parameters"]["FollowPath"]
    assert planner["plugin"] == "patrol_navigation_controller::TimeScoredLatticePlanner"
    assert planner["candidate_names"] == ["QuickMotion", "StableTurns"]
    assert planner["QuickMotion"]["allow_reverse_expansion"] is True
    assert planner["StableTurns"]["allow_reverse_expansion"] is True
    assert controller["primary_controller"] == "patrol_navigation_controller::SegmentedRPPController"
    assert controller["allow_reversing"] is True
    assert controller["use_rotate_to_heading"] is False
    assert controller["use_velocity_scaled_lookahead_dist"] is True
    assert controller["min_lookahead_dist"] == pytest.approx(0.20)
    assert controller["max_lookahead_dist"] == pytest.approx(0.50)


def test_jerk_limited_wheel_command_is_bounded():
    limiter = WheelJerkLimiter(max_accel=1.0, max_jerk=2.0)
    previous_v = 0.0
    previous_a = 0.0
    for index in range(10):
        value, opposite = limiter.step(1.0, -1.0, index * 0.1)
        acceleration = (value - previous_v) / 0.1
        assert -1.00001 <= value <= 1.00001
        assert opposite == pytest.approx(-value)
        assert abs(acceleration) <= 1.00001
        assert abs(acceleration - previous_a) <= 0.20001
        previous_v, previous_a = value, acceleration


def test_jerk_limiter_reset_and_validation():
    limiter = WheelJerkLimiter(max_accel=1.0, max_jerk=2.0)
    limiter.step(1.0, 0.0, 1.0)
    limiter.reset()  # physical STOP always bypasses and resets the profile
    assert limiter.step(1.0, 0.0, 2.0)[0] == pytest.approx(0.02)
    with pytest.raises(ValueError):
        limiter.step(float("nan"), 0.0, 2.1)
    with pytest.raises(ValueError):
        WheelJerkLimiter(max_accel=0.0, max_jerk=2.0)


def test_jerk_limiter_direction_reversal_must_cross_zero_boundary():
    limiter = WheelJerkLimiter(max_accel=1.0, max_jerk=2.0)
    previous = 0.0
    for tick in range(100):
        previous = limiter.step(1.0, 1.0, tick * 0.1)[0]
    assert previous > 0.0
    observed_zero = False
    for tick in range(100, 250):
        velocity = limiter.step(-1.0, -1.0, tick * 0.1)[0]
        if velocity == 0.0:
            observed_zero = True
        if velocity < 0.0:
            assert observed_zero
            break
    else:
        pytest.fail("Reverse motion never became available")


def test_jerk_limiter_rejects_invalid_timestamps_and_timestep_config():
    limiter = WheelJerkLimiter(max_accel=1.0, max_jerk=2.0)
    with pytest.raises(ValueError):
        limiter.step(0.0, 0.0, float("inf"))
    with pytest.raises(ValueError):
        WheelJerkLimiter(max_accel=1.0, max_jerk=2.0, max_dt=0.0)


def test_small_constant_target_has_no_terminal_jerk_impulse():
    limiter = WheelJerkLimiter(max_accel=1.0, max_jerk=2.0)
    previous_velocity, previous_accel = 0.0, 0.0
    readings = []
    for tick in range(50):
        value, other = limiter.step(0.1, 0.1, tick * 0.1)
        acceleration = (value - previous_velocity) / 0.1
        assert abs(acceleration) <= 1.000001
        assert abs(acceleration - previous_accel) <= 0.200002, (tick, readings)
        readings.append(value)
        previous_velocity, previous_accel = value, acceleration
    assert abs(readings[-1] - 0.1) < 1e-5
    assert max(readings) <= 0.11


def test_target_speed_settles_without_breaking_jerk_limit():
    for target in (0.2, 0.5, 0.8, -0.2, -0.5, -0.8, 1.0, -1.0):
        limiter = WheelJerkLimiter(max_accel=1.0, max_jerk=2.0)
        previous_velocity, previous_accel = 0.0, 0.0
        for tick in range(100):
            value, other = limiter.step(target, target, tick * 0.1)
            accel = (value - previous_velocity) / 0.1
            assert abs(accel) <= 1.000001
            assert abs(accel - previous_accel) <= 0.200002
            assert abs(value) <= 1.000001
            previous_velocity, previous_accel = value, accel
        assert abs(previous_velocity - target) < 1e-4


@pytest.mark.parametrize("start,target", [
    (1.0, -1.0), (-1.0, 1.0), (0.5, -0.5), (-0.5, 0.5),
])
def test_jerk_reversal_has_no_artificial_zero_acceleration_impulse(start, target):
    limiter = WheelJerkLimiter(max_accel=1.0, max_jerk=2.0)
    last_v, last_a = 0.0, 0.0
    for tick in range(40):
        # Drive to a stable initial speed before changing target direction.
        command = start if tick < 15 else target
        velocity, right = limiter.step(command, -command, tick * 0.1)
        acceleration = (velocity - last_v) / 0.1
        assert abs(acceleration) <= 1.000001
        assert abs(acceleration - last_a) <= 0.200002
        assert right == pytest.approx(-velocity)
        last_v, last_a = velocity, acceleration


def test_nonexact_zero_crossing_preserves_acceleration_continuity():
    limiter = WheelJerkLimiter(max_accel=1.0, max_jerk=2.0)
    last_v, last_a = 0.0, 0.0
    # An early reversal crosses zero between two commands, unlike a fully
    # settled 1.0 -> -1.0 reversal; it must not insert an abrupt zero.
    for tick in range(45):
        target = 0.5 if tick < 8 else -0.5
        v, _ = limiter.step(target, target, tick * 0.1)
        actual_a = (v - last_v) / 0.1
        assert abs(actual_a - last_a) <= 0.200002
        last_v, last_a = v, actual_a
    assert last_v == pytest.approx(-0.5, abs=0.001)
