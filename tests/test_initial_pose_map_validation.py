"""Saved-map Initial Pose validation must block occupied cells and warn on unknown."""
import json
from types import SimpleNamespace

import pytest

from server.navigation_map_service import NavigationMapError, NavigationMapService


@pytest.fixture
def setup_map(tmp_path):
    directory = tmp_path / "navigation" / "maps"
    directory.mkdir(parents=True)
    raw_path = directory / "test.raw.json"
    data = [0] * (60 * 60)
    selected = SimpleNamespace(
        width=60, height=60, resolution=0.1,
        origin_x=0.0, origin_y=0.0, origin_yaw=0.0,
        raw_path=raw_path,
    )
    def set_cell(col, row, occupancy):
        data[row * 60 + col] = occupancy
        raw_path.write_text(
            json.dumps({"data_encoding": "raw", "data": data}), encoding="utf-8",
        )
    set_cell(0, 0, 0)
    return NavigationMapService(tmp_path, directory), selected, set_cell


def test_occupied_footprint_is_blocked(setup_map):
    service, selected, set_cell = setup_map
    set_cell(30, 30, 100)
    with pytest.raises(NavigationMapError) as error:
        service.validate_initial_pose(
            selected, {"x": 3.05, "y": 3.05, "yaw_degrees": 0},
        )
    assert error.value.error_code == "INITIAL_POSE_OCCUPIED"


def test_unknown_requires_explicit_ack(setup_map):
    service, selected, set_cell = setup_map
    set_cell(30, 30, -1)
    pose = {"x": 3.05, "y": 3.05, "yaw_degrees": 45}
    with pytest.raises(NavigationMapError) as error:
        service.validate_initial_pose(selected, pose)
    assert error.value.error_code == "INITIAL_POSE_CONFIRMATION_REQUIRED"
    accepted = service.validate_initial_pose(
        selected, {**pose, "warning_acknowledged": True},
    )
    assert accepted["yaw_degrees"] == 45


def test_out_of_bounds_blocked_even_with_ack(setup_map):
    service, selected, set_cell = setup_map
    with pytest.raises(NavigationMapError) as error:
        service.validate_initial_pose(
            selected, {"x": -0.1, "y": 3, "yaw_degrees": 0,
                       "warning_acknowledged": True},
        )
    assert error.value.error_code == "INITIAL_POSE_OUT_OF_BOUNDS"
