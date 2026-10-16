"""Time-scored Smac lattice planner static and ROS-independent C++ tests.

Run:
    pytest -q tests/test_nav_time_scored_lattice.py
    colcon build --packages-select patrol_navigation_controller patrol_navigation

The pure scoring C++ unit uses only libstdc++ and may run without ROS2.
Actual plugin load, Nav2 action and motor motion still need Humble validation.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "navigation/ros/patrol_navigation_controller"
NAV = ROOT / "navigation/ros/patrol_navigation"
SOURCE = PKG / "src/time_scored_lattice_planner.cpp"
HEADER = PKG / "include/patrol_navigation_controller/path_time_estimator.hpp"
CPP_TEST = ROOT / "tests/cpp/test_path_time_estimator.cpp"


def test_planner_is_registered_as_nav2_global_planner():
    exported = ET.parse(PKG / "plugins.xml").getroot()
    entries = list(exported.findall("class"))
    candidates = [
        entry for entry in entries
        if entry.attrib.get("type") ==
        "patrol_navigation_controller::TimeScoredLatticePlanner"
    ]
    assert len(candidates) == 1
    assert candidates[0].attrib["base_class_type"] == "nav2_core::GlobalPlanner"
    cmake = (PKG / "CMakeLists.txt").read_text()
    assert "src/time_scored_lattice_planner.cpp" in cmake
    assert "pluginlib_export_plugin_description_file(nav2_core plugins.xml)" in cmake
    assert (PKG / "include/patrol_navigation_controller/time_scored_lattice_planner.hpp").is_file()


def test_planner_config_has_two_separately_parameterized_safe_smac_candidates():
    conf = yaml.safe_load((NAV / "config/nav2_params.yaml").read_text())
    planner = conf["planner_server"]["ros__parameters"]
    assert planner["planner_plugins"] == ["GridBased"]
    chosen = planner["GridBased"]
    assert chosen["plugin"] == "patrol_navigation_controller::TimeScoredLatticePlanner"
    assert chosen["candidate_names"] == ["QuickMotion", "StableTurns"]
    assert chosen["max_total_planning_time"] == pytest.approx(5.0)
    assert chosen["wheel_track_m"] > 0
    assert chosen["reverse_fraction"] == pytest.approx(0.30)
    assert chosen["switch_stop_sec"] == pytest.approx(0.50)
    for name in chosen["candidate_names"]:
        candidate = chosen[name]
        assert candidate["allow_reverse_expansion"] is True
        assert candidate["allow_unknown"] is True
        assert candidate["tolerance"] == pytest.approx(0.50)
        assert candidate["max_planning_time"] <= 2.0
        assert candidate["lattice_filepath"] == ""
        assert candidate["smooth_path"] is False
    # Lower/greater penalties must expose non-identical candidate search spaces.
    assert chosen["QuickMotion"]["rotation_penalty"] < chosen["StableTurns"]["rotation_penalty"]
    assert chosen["QuickMotion"]["reverse_penalty"] < chosen["StableTurns"]["reverse_penalty"]


def test_lattice_asset_and_measured_limits_reach_both_candidates():
    launch = (NAV / "launch/navigation.launch.py").read_text()
    assert '"GridBased.max_wheel_mps": max_wheel_mps' in launch
    assert '"GridBased.max_angular_rps": max_angular_rps' in launch
    assert '"GridBased.wheel_track_m": wheel_track_m' in launch
    assert '"GridBased.QuickMotion.lattice_filepath": PathJoinSubstitution(' in launch
    assert '"GridBased.StableTurns.lattice_filepath": PathJoinSubstitution(' in launch
    assert '"lattice_diff32.json"' in launch
    assert '"lattice_diff32_stable.json"' in launch
    fast = NAV / "config/lattice_diff32.json"
    stable = NAV / "config/lattice_diff32_stable.json"
    assert fast.is_file() and stable.is_file()
    # Same geometry, deliberately different paths: Humble Smac's static
    # NodeLattice::motion_table skips penalty reload if paths are identical.
    assert fast.read_bytes() == stable.read_bytes()


def test_planner_really_compares_path_time_not_just_path_length():
    code = SOURCE.read_text()
    assert 'loader_.createSharedInstance(' in code
    assert '"nav2_smac_planner/SmacPlannerLattice"' in code
    assert "estimatePathTime(coordinates, time_limits_)" in code
    assert "score.seconds < best_seconds - 1e-6" in code
    assert "best_switches" in code
    assert "candidate->createPlan(start, goal)" in code
    assert "candidate->configure(" in code
    assert "candidate->deactivate()" in code
    assert "candidate->cleanup()" in code
    # Humble's lattice motion table is shared across callbacks; candidate
    # configuration and search must run under the same process mutex.
    assert "std::mutex lattice_candidate_search_mutex;" in code
    assert "std::unique_lock<std::mutex> search_lock(lattice_candidate_search_mutex);" in code
    assert "for (size_t index = 0; original_goal_clear && index < candidate_ids_.size()" in code
    assert "candidates_[index]" not in code
    assert "elapsed >= max_total_planning_sec_" in code
    assert "else if (path.poses.size() >= 2)" in code
    # A path that arrives after the global deadline must never be executed.
    assert "elapsed_after_candidate >= max_total_planning_sec_" in code
    assert "best.poses.clear();" in code
    assert 'candidate_ids_[0] == "QuickMotion"' in code
    assert 'candidate_ids_[1] == "StableTurns"' in code
    assert "validAgainstCurrentCostmap(best, costmap_)" in code
    assert "costmap_ros->isCurrent()" in code
    assert "checker.footprintCostAtPose(" in code
    assert "return validateSweptPath(" in code
    assert "nav2_costmap_2d::INSCRIBED_INFLATED_OBSTACLE" in code
    pure = HEADER.read_text()
    assert "linear_accel_mps2" in pure
    assert "angular_accel_rps2" in pure
    assert "linear_jerk_mps3" in pure
    assert "limits.wheel_track_m" in pure
    assert "direction_switch_sec" in pure
    assert "limits.reverse_mps" in pure


def test_candidate_search_instance_recreated_to_reload_humble_static_costs():
    code = SOURCE.read_text()
    assert "NodeLattice::motion_table" in code
    assert "loader_.createSharedInstance(" in code
    assert "candidate->configure(" in code
    assert "candidate->createPlan(start, goal)" in code
    assert code.index("candidate->configure(") < code.index("candidate->createPlan(start, goal)")
    assert "candidate->activate()" in code
    assert "candidate->deactivate()" in code
    assert "candidate->cleanup()" in code


def test_selected_route_is_rechecked_before_publishing():
    code = SOURCE.read_text()
    assert "bool validAgainstCurrentCostmap(" in code
    assert "std::unique_lock<nav2_costmap_2d::Costmap2D::mutex_t>" in code
    assert "costmap_ros->getRobotFootprint()" in code
    assert "costmap_ros->isCurrent()" in code
    assert "nav2_costmap_2d::INSCRIBED_INFLATED_OBSTACLE" in code
    assert "cost == nav2_costmap_2d::NO_INFORMATION" not in code
    assert "return std::isfinite(cost) && cost >= 0.0" in code
    # Reject updated obstacles rather than falling back to a stale candidate.
    assert "if (!best.poses.empty() && !validAgainstCurrentCostmap(best, costmap_))" in code
    assert "best.poses.clear();" in code


def test_selected_route_checks_every_interpolated_chassis_pose():
    geometry = (
        PKG / "include/patrol_navigation_controller/path_sweep.hpp"
    ).read_text()
    assert "max_translation_step_m = 0.025" in geometry
    assert "max_rotation_step_rad = 0.05" in geometry
    assert "max_samples = 60000" in geometry
    assert "for (size_t step =" in geometry
    assert "if (!collision_free(sample))" in geometry
    assert "return validateSweptPath(" in SOURCE.read_text()
    cpp = CPP_TEST.read_text()
    assert "std::abs(pose.x - 0.05)" in cpp
    assert "std::abs(pose.yaw - 0.2)" in cpp


def test_blocked_goal_alternate_is_nearest_and_requires_full_footprint():
    cpp = SOURCE.read_text()
    header = (
        PKG / "include/patrol_navigation_controller/alternate_goal_candidates.hpp"
    ).read_text()
    assert "endpointFootprintClear(" in cpp
    assert "const bool original_goal_clear = endpointFootprintClear(" in cpp
    assert "if (!original_goal_clear && best.poses.empty())" in cpp
    assert "nearbyGoalCandidates(original_goal)" in cpp
    assert "if (attempted >= 5" in cpp
    assert "if (!endpointFootprintClear(alternate, costmap_))" in cpp
    assert "candidate_goal.pose.position.x = alternate.x;" in cpp
    assert "candidate_goal.pose.position.y = alternate.y;" in cpp
    assert "goalEndpointWithinTolerance(" in cpp
    assert "actual_endpoint, original_goal, 0.20, 0.20" in cpp
    assert "validAgainstCurrentCostmap(path, costmap_)" in cpp
    assert "best_candidate = \"NearestSafeGoal\"" in cpp
    assert "closest_endpoint_m = distance_to_original;" in cpp
    assert "score.seconds < best_seconds" in cpp
    assert "if (attempted >= 5" in cpp
    assert "const double radius_m = 0.20" in header
    assert "radius_m > 0.50" in header
    assert "goal.x + dx, goal.y + dy, goal.yaw" in header
    assert "std::sort(" in header


def test_all_smac_candidate_endpoints_obey_final_xy_and_yaw_contract():
    source = SOURCE.read_text()
    helper = (
        PKG / "include/patrol_navigation_controller/alternate_goal_candidates.hpp"
    ).read_text()
    assert "goalEndpointWithinTolerance(" in helper
    assert "endpoint.x - original_goal.x" in helper
    assert "std::abs(shortestTurn(endpoint.yaw, original_goal.yaw))" in helper
    assert "coordinates.back(), original_goal, 0.20, 0.20" in source
    assert "score.valid && terminal_safe &&" in source
    assert "actual_endpoint, original_goal, 0.20, 0.20" in source


def test_each_time_ranked_candidate_is_collision_checked_before_selection():
    code = SOURCE.read_text()
    assert "score.valid && terminal_safe &&" in code
    assert "validAgainstCurrentCostmap(path, costmap_)" in code
    assert "if (collision_safe &&" in code
    assert "if (!best.poses.empty() && !validAgainstCurrentCostmap(best, costmap_))" in code
    # A safer runner-up must be eligible if the faster original candidate is
    # invalid, rather than discarding every plan at final validation.
    assert code.index("const bool collision_safe") < code.index("best = std::move(path)")


def test_pure_cpp_cost_model_execution(tmp_path):
    compiler = shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        pytest.skip("No C++ compiler available in this environment")
    binary = tmp_path / "nav_lattice_time_test"
    build = subprocess.run(
        [
            compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-O1",
            "-I", str(PKG / "include"), str(CPP_TEST), "-o", str(binary),
        ],
        capture_output=True, text=True, timeout=45, check=False,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run(
        [str(binary)], capture_output=True, text=True, timeout=15, check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr
