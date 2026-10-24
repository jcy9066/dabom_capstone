"""Pure regression tests for State Lattice path headings and motion grouping."""
import math

import pytest

from server.navigation_motion_segments import (
    annotate_path,
    estimate_travel_time,
    motion_segments,
)
from server.navigation_control_api import NavigationControlApi


def test_yaw_aware_forward_reverse_and_spin_remain_separate():
    raw = [
        {"x": 0.0, "y": 0.0, "yaw": 0.0},
        {"x": 0.2, "y": 0.0, "yaw": 0.0},
        {"x": 0.05, "y": 0.0, "yaw": 0.0},
        {"x": 0.05, "y": 0.0, "yaw": math.pi / 2},
        {"x": 0.05, "y": 0.1, "yaw": math.pi / 2},
    ]
    path = annotate_path(raw)
    assert [p.get("motion") for p in path] == [
        "forward", "reverse", "rotate_left", "forward", None,
    ]
    assert path[2]["x"] == path[3]["x"]  # do not drop a pure spin
    segments = motion_segments(path)
    assert [s["motion"] for s in segments] == [
        "forward", "reverse", "rotate_left", "forward",
    ]
    assert [(s["start_index"], s["end_index"]) for s in segments] == [
        (0, 1), (1, 2), (2, 3), (3, 4),
    ]


def test_yaw_wraparound_chooses_short_rotation():
    path = annotate_path([
        {"x": 0.0, "y": 0.0, "yaw": -math.pi + 0.03},
        {"x": 0.0, "y": 0.0, "yaw": math.pi - 0.03},
    ])
    assert path[0]["motion"] == "rotate_right"
    assert motion_segments(path)[0]["rotation_rad"] == pytest.approx(0.06)


def test_legacy_xy_paths_do_not_invent_yaw_or_direction():
    raw = [{"x": 0.0, "y": 0.0}, {"x": 0.2, "y": 0.0}]
    assert annotate_path(raw) == raw
    assert NavigationControlApi._normalized_path(raw) == raw


def test_invalid_points_are_dropped_but_spins_are_kept():
    raw = [
        {"x": 0.0, "y": 0.0, "yaw": 0},
        {"x": float("nan"), "y": 0.0, "yaw": 0},
        {"x": 0.0, "y": 0.0, "yaw": -0.2},
        {"x": 0.1, "y": 0.0, "yaw": float("inf")},
    ]
    output = NavigationControlApi._normalized_path(raw)
    assert len(output) == 3
    assert output[0]["motion"] == "rotate_right"
    assert "yaw" not in output[2]


def test_motion_time_prefers_faster_and_fewer_switches():
    short = motion_segments(annotate_path([
        {"x": 0.0, "y": 0.0, "yaw": 0.0},
        {"x": 0.2, "y": 0.0, "yaw": 0.0},
    ]))
    long = motion_segments(annotate_path([
        {"x": 0.0, "y": 0.0, "yaw": 0.0},
        {"x": 0.5, "y": 0.0, "yaw": 0.0},
    ]))
    parameters = {"max_wheel_mps": 0.212, "max_angular_rps": 1.05}
    assert 0 < estimate_travel_time(short, **parameters) < estimate_travel_time(long, **parameters)

    detour = [
        {"motion": "forward", "distance_m": 0.1, "rotation_rad": 0.0},
        {"motion": "reverse", "distance_m": 0.1, "rotation_rad": 0.0},
    ]
    without_switch_cost = estimate_travel_time(
        detour, **parameters, direction_change_delay_sec=0.0,
    )
    assert estimate_travel_time(detour, **parameters) == pytest.approx(
        without_switch_cost + 0.2,
    )


def test_motion_time_accounts_for_pivot_and_rejects_invalid_segments():
    options = {"max_wheel_mps": 0.212, "max_angular_rps": 1.05}
    pivot = [{"motion": "rotate_left", "distance_m": 0, "rotation_rad": math.pi}]
    assert estimate_travel_time(pivot, **options) > 0
    with pytest.raises(ValueError):
        estimate_travel_time(pivot, max_wheel_mps=0, max_angular_rps=1.05)
    with pytest.raises(ValueError):
        estimate_travel_time([{"motion": "invalid", "distance_m": 1}], **options)
