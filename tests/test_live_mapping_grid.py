import math

from server.live_mapping_grid import LiveMappingGrid


def decode_rle(payload):
    values = []
    for value, count in payload["data"]:
        values.extend([int(value)] * int(count))
    assert len(values) == payload["width"] * payload["height"]
    return values


def cell_value(payload, world_x, world_y):
    resolution = payload["resolution"]
    origin = payload["origin"]
    # Floating-point values exactly on a cell boundary such as
    # 1.0 / 0.1 can become 9.999999..., so apply a tiny epsilon before floor.
    x = math.floor(((world_x - origin["x"]) / resolution) + 1e-9)
    y = math.floor(((world_y - origin["y"]) / resolution) + 1e-9)
    if x < 0 or y < 0 or x >= payload["width"] or y >= payload["height"]:
        return None
    return decode_rle(payload)[y * payload["width"] + x]


def make_scan(ranges):
    return {
        "angle_min": 0.0,
        "angle_increment": 1.5707963267948966,
        "range_min": 0.05,
        "range_max": 4.0,
        "ranges": ranges,
        "sent_at": "2026-10-01T10:00:00Z",
    }


def test_scan_only_preview_map_exists_before_first_pose():
    grid = LiveMappingGrid(
        resolution=0.1,
        refresh_sec=1.0,
        max_clear_range_m=2.0,
        padding_cells=0,
        sensor_yaw=3.141592653589793,
    )
    assert grid.update_scan(make_scan([1.0]))

    payload = grid.maybe_update(
        robot_id="pi-01",
        monotonic_now=1.0,
        wall_time=1.0,
        force=True,
    )

    assert payload is not None
    assert payload["navigation_mode"] == "mapping"
    assert cell_value(payload, -1.0, 0.0) == 100


def test_real_pose_replaces_scan_only_preview_cells():
    grid = LiveMappingGrid(
        resolution=0.1,
        refresh_sec=1.0,
        max_clear_range_m=2.0,
        padding_cells=0,
        sensor_yaw=3.141592653589793,
    )
    grid.update_scan(make_scan([1.0]))
    preview = grid.maybe_update(
        robot_id="pi-01",
        monotonic_now=1.0,
        wall_time=1.0,
        force=True,
    )
    assert cell_value(preview, -1.0, 0.0) == 100

    assert grid.update_pose({"x": 2.0, "y": 0.0, "yaw": 0.0})
    grid.update_scan(make_scan([1.0]))
    real = grid.maybe_update(
        robot_id="pi-01",
        monotonic_now=2.0,
        wall_time=2.0,
        force=True,
    )

    assert real is not None
    assert cell_value(real, 1.0, 0.0) == 100
    assert cell_value(real, -1.0, 0.0) is None


def test_live_grid_builds_free_and_occupied_cells():
    grid = LiveMappingGrid(
        resolution=0.1,
        refresh_sec=1.0,
        max_clear_range_m=2.0,
        padding_cells=0,
        sensor_yaw=3.141592653589793,
    )
    assert grid.update_pose({"x": 0.0, "y": 0.0, "yaw": 0.0})
    assert grid.update_scan(make_scan([1.0]))
    payload = grid.maybe_update(
        robot_id="pi-01",
        monotonic_now=1.0,
        wall_time=1.0,
        force=True,
    )
    assert payload is not None
    assert payload["source"] == "live_mapping_grid"
    assert cell_value(payload, -1.0, 0.0) == 100
    assert cell_value(payload, -0.5, 0.0) == 0


def test_removed_wall_is_cleared_on_next_update():
    grid = LiveMappingGrid(
        resolution=0.1,
        refresh_sec=1.0,
        max_clear_range_m=2.0,
        padding_cells=0,
        sensor_yaw=0.5,
    )
    grid.update_pose({"x": 0.0, "y": 0.0, "yaw": -0.5})
    grid.update_scan(make_scan([1.0]))
    first = grid.maybe_update(
        robot_id="pi-01",
        monotonic_now=1.0,
        wall_time=1.0,
        force=True,
    )
    assert cell_value(first, 1.0, 0.0) == 100

    grid.update_scan(make_scan([None]))
    second = grid.maybe_update(
        robot_id="pi-01",
        monotonic_now=2.0,
        wall_time=2.0,
    )
    assert second is not None
    assert cell_value(second, 1.0, 0.0) == 0


def test_unverified_preview_does_not_keep_unobserved_ghost_cells():
    grid = LiveMappingGrid(
        resolution=0.1,
        refresh_sec=1.0,
        max_clear_range_m=2.0,
        padding_cells=0,
        sensor_yaw=0.5,
    )
    grid.update_pose({"x": 0.0, "y": 0.0, "yaw": -0.5})
    grid.update_scan(make_scan([1.0, 1.0]))
    first = grid.maybe_update(
        robot_id="pi-01",
        monotonic_now=1.0,
        wall_time=1.0,
        force=True,
    )
    old_width = first["width"]
    assert cell_value(first, 0.0, 1.0) == 100

    grid.update_pose({"x": 3.0, "y": 0.0, "yaw": -0.5})
    grid.update_scan(make_scan([1.0]))
    second = grid.maybe_update(
        robot_id="pi-01",
        monotonic_now=2.0,
        wall_time=2.0,
    )
    # Unverified scan-only geometry is a per-frame preview, never a fused map.
    assert second["preview"] is True
    assert cell_value(second, 0.0, 1.0) is None
    assert cell_value(second, 4.0, 0.0) == 100


def test_refresh_is_one_second():
    grid = LiveMappingGrid(
        resolution=0.1,
        refresh_sec=1.0,
        sensor_yaw=0.5,
    )
    grid.update_pose({"x": 0.0, "y": 0.0, "yaw": -0.5})
    grid.update_scan(make_scan([1.0]))

    assert grid.maybe_update(
        robot_id="pi-01",
        monotonic_now=1.0,
        wall_time=1.0,
        force=True,
    ) is not None
    assert grid.maybe_update(
        robot_id="pi-01",
        monotonic_now=1.9,
        wall_time=1.9,
    ) is None
    assert grid.maybe_update(
        robot_id="pi-01",
        monotonic_now=2.0,
        wall_time=2.0,
    ) is not None

def test_slam_map_is_preserved_as_base_before_live_scan_updates():
    grid = LiveMappingGrid(
        resolution=0.1,
        refresh_sec=1.0,
        padding_cells=0,
        sensor_yaw=0.5,
    )
    base = {
        "width": 3,
        "height": 2,
        "resolution": 0.1,
        "origin": {"x": -0.2, "y": -0.1, "yaw": 0.0},
        "data_encoding": "rle",
        "data": [[0, 3], [-1, 1], [100, 1], [0, 1]],
    }

    assert grid.update_base_map(base)
    payload = grid.snapshot(robot_id="pi-01", wall_time=1.0)

    assert payload is not None
    assert payload["resolution"] == 0.1
    assert cell_value(payload, -0.15, -0.05) == 0
    assert cell_value(payload, -0.05, 0.05) == 100


def test_slam_map_remains_authoritative_after_live_scan():
    grid = LiveMappingGrid(
        resolution=0.1,
        refresh_sec=1.0,
        max_clear_range_m=2.0,
        padding_cells=0,
        sensor_yaw=0.5,
    )
    base = {
        "width": 21,
        "height": 3,
        "resolution": 0.1,
        "origin": {"x": 0.0, "y": -0.1, "yaw": 0.0},
        "data_encoding": "rle",
        "data": [[0, 31], [100, 1], [0, 31]],
    }
    assert grid.update_base_map(base)
    assert grid.update_pose({"x": 0.0, "y": 0.0, "yaw": -0.5})
    assert grid.update_scan(make_scan([None]))

    payload = grid.maybe_update(
        robot_id="pi-01",
        monotonic_now=1.0,
        wall_time=1.0,
        force=True,
    )
    assert payload is not None
    # SLAM occupied cells are not cleared by unaligned LiDAR overlay data.
    assert cell_value(payload, 1.0, 0.0) == 100



def test_authoritative_map_snapshot_removes_old_out_of_bounds_occupied_cells():
    grid = LiveMappingGrid(resolution=0.1, padding_cells=0)
    first = {
        "width": 30, "height": 1, "resolution": 0.1,
        "origin": {"x": 0.0, "y": 0.0, "yaw": 0.0},
        "data_encoding": "rle", "data": [[0, 20], [100, 1], [-1, 9]],
    }
    assert grid.update_base_map(first)
    before = grid.snapshot(robot_id="pi-01", wall_time=1.0)
    assert before["map_verified"] is True
    assert cell_value(before, 2.05, 0.05) == 100

    corrected = {
        "width": 10, "height": 1, "resolution": 0.1,
        "origin": {"x": 0.0, "y": 0.0, "yaw": 0.0},
        "data_encoding": "rle", "data": [[0, 10]],
    }
    assert grid.update_base_map(corrected)
    after = grid.snapshot(robot_id="pi-01", wall_time=2.0)
    assert after["source"] == "slam_toolbox"
    assert after["preview"] is False
    assert cell_value(after, 2.05, 0.05) is None


def test_scan_preview_is_explicitly_unverified():
    grid = LiveMappingGrid(resolution=0.1, padding_cells=0)
    assert grid.update_scan(make_scan([1.0]))
    preview = grid.maybe_update(
        robot_id="pi-01", monotonic_now=1.0, wall_time=1.0, force=True
    )
    assert preview["preview"] is True
    assert preview["map_verified"] is False
    assert preview["source"] == "live_mapping_grid"
    assert preview["map_kind"] == "preview"


def test_scan_preview_rotation_keeps_static_obstacle_in_world_coordinates():
    grid = LiveMappingGrid(
        resolution=0.1, refresh_sec=1.0, padding_cells=0,
        sensor_yaw=math.pi,
    )
    for i, (robot_yaw, scan_angle) in enumerate([
        (0.0, -math.pi), (math.pi / 2, math.pi / 2),
        (math.pi, 0.0), (2 * math.pi, -math.pi),
    ], 1):
        assert grid.update_pose({"x": 0.0, "y": 0.0, "yaw": robot_yaw})
        scan = make_scan([2.0])
        scan["angle_min"] = scan_angle
        scan["angle_increment"] = 0.01
        assert grid.update_scan(scan)
        snapshot = grid.maybe_update(
            robot_id="pi-01", monotonic_now=float(i),
            wall_time=float(i), force=True,
        )
        assert snapshot["preview"] is True
        assert cell_value(snapshot, 2.0, 0.0) == 100
