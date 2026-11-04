"""Initial pose: map-origin transform, footprint and warning acknowledgment."""
from __future__ import annotations

import math

from server.initial_pose_safety import decode_grid, evaluate_initial_pose_grid, PoseGridError


def check(cells, x, y, *, origin=None, width=60, height=60):
    return evaluate_initial_pose_grid(
        width=width, height=height, resolution=0.1,
        origin=origin or {"x": 0.0, "y": 0.0, "yaw": 0.0},
        cells=cells, x=x, y=y,
    )


def test_clearance_and_occupied_position():
    cells = [0] * (60 * 60)
    assert check(cells, 3.0, 3.0)["risk"] == "CLEAR"
    cells[30 * 60 + 30] = 100
    assert check(cells, 3.05, 3.05)["risk"] == "BLOCKED"


def test_unknown_and_nearby_obstacle_require_confirmation():
    cells = [0] * (60 * 60)
    cells[30 * 60 + 30] = -1
    assert check(cells, 3.05, 3.05)["reason"] == "UNKNOWN"
    cells[30 * 60 + 30] = 0
    cells[30 * 60 + 34] = 100
    assert check(cells, 3.05, 3.05)["risk"] == "WARNING"


def test_bounds_rotate_with_map_origin():
    origin = {"x": 7.0, "y": -2.0, "yaw": math.pi / 2}
    cells = [0] * (60 * 60)
    assert check(cells, 4.0, 1.0, origin=origin)["risk"] == "CLEAR"
    assert check(cells, 8.0, -3.0, origin=origin)["reason"] == "OUT_OF_BOUNDS"


def test_rle_decoding_preserves_unknown():
    assert decode_grid([[0, 2], [-1, 1], [100, 1]], "rle", 2, 2) == [0, 0, -1, 100]


def test_rle_rejects_truncated_map():
    import pytest
    with pytest.raises(PoseGridError):
        decode_grid([[0, 3]], "rle", 2, 2)
