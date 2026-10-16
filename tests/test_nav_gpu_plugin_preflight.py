"""Humble primitive parser and GPU plugin installation contracts."""
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "navigation/ros/patrol_navigation/config"


def test_humble_primitive_schema():
    quick = json.loads((CONFIG / "lattice_diff32.json").read_text())
    stable = json.loads((CONFIG / "lattice_diff32_stable.json").read_text())
    assert quick == stable
    metadata = quick["lattice_metadata"]
    required_meta = {
        "turning_radius", "grid_resolution", "num_of_headings",
        "heading_angles", "number_of_trajectories", "motion_model",
    }
    assert required_meta <= metadata.keys()
    assert metadata["num_of_headings"] == len(metadata["heading_angles"]) == 32
    assert metadata["grid_resolution"] == 0.05
    assert len(quick["primitives"]) == metadata["number_of_trajectories"] == 160
    for index, primitive in enumerate(quick["primitives"]):
        assert primitive["trajectory_id"] == index
        required = {
            "start_angle_index", "end_angle_index", "trajectory_radius",
            "trajectory_length", "arc_length", "straight_length",
            "left_turn", "poses",
        }
        assert required <= primitive.keys()
        assert len(primitive["poses"]) >= 2
        assert all(len(pose) == 3 for pose in primitive["poses"])
        assert all(math.isfinite(value) for pose in primitive["poses"] for value in pose)
        start = primitive["start_angle_index"]
        end = primitive["end_angle_index"]
        assert 0 <= start < 32 and 0 <= end < 32
        for observed, angle in (
            (primitive["poses"][0][2], metadata["heading_angles"][start]),
            (primitive["poses"][-1][2], metadata["heading_angles"][end]),
        ):
            delta = math.atan2(math.sin(observed - angle), math.cos(observed - angle))
            assert abs(delta) < 1e-6


def test_gpu_nav_plugin_preflight_and_build_instructions():
    script = (ROOT / "start_gpu_server.sh").read_text()
    assert "ros2 pkg prefix patrol_navigation_controller" in script
    assert "nav2_smac_planner" in script
    assert "nav2_regulated_pure_pursuit_controller" in script
    assert "lib/libsegmented_rpp_controller.so" in script
    readme = (ROOT / "README.md").read_text()
    assert "--packages-select patrol_navigation_controller patrol_navigation" in readme
    cmake = (ROOT / "navigation/ros/patrol_navigation_controller/CMakeLists.txt").read_text()
    assert "install(TARGETS segmented_rpp_controller" in cmake
