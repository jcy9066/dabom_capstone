"""Static integration contract for the Humble segmented navigation controller.

These checks supplement, but do NOT replace, a colcon build, Nav2 controller
plugin loading, ROS integration tests or physical robot safety validation.
"""
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest

import yaml


ROOT = Path(__file__).resolve().parents[1]
ROS_ROOT = ROOT / "navigation/ros"
PACKAGE = ROS_ROOT / "patrol_navigation_controller"
CONFIG = ROS_ROOT / "patrol_navigation/config/nav2_params.yaml"


def test_nav2_controller_plugin_export_and_build_dependencies():
    package = ET.parse(PACKAGE / "package.xml").getroot()
    assert package.findtext("name") == "patrol_navigation_controller"
    assert package.findtext("buildtool_depend") == "ament_cmake"
    dependencies = [node.text for node in package.findall("depend")]
    assert {"nav2_core", "nav2_costmap_2d", "pluginlib", "tf2", "angles"} <= set(dependencies)

    plugin = ET.parse(PACKAGE / "plugins.xml").getroot()
    controller = plugin.find("class")
    assert controller.attrib["base_class_type"] == "nav2_core::Controller"
    assert controller.attrib["type"] == "patrol_navigation_controller::SegmentedRPPController"

    cmake = (PACKAGE / "CMakeLists.txt").read_text()
    assert "pluginlib_export_plugin_description_file(nav2_core plugins.xml)" in cmake
    assert "src/segmented_rpp_controller.cpp" in cmake


def test_humble_rotation_shim_wraps_segmented_rpp_and_keeps_final_yaw():
    params = yaml.safe_load(CONFIG.read_text())
    controller = params["controller_server"]["ros__parameters"]
    tracking = controller["FollowPath"]
    assert tracking["plugin"] == "nav2_rotation_shim_controller::RotationShimController"
    assert tracking["primary_controller"] == "patrol_navigation_controller::SegmentedRPPController"
    assert tracking["rotate_to_goal_heading"] is False
    assert tracking["allow_reversing"] is True
    assert tracking["use_rotate_to_heading"] is False
    assert tracking["segment_pivot_max_angular"] == 1.05
    assert tracking["segment_transfer_position_tolerance"] == 0.08
    checker = controller["progress_checker"]
    assert checker["plugin"] == "nav2_controller::PoseProgressChecker"
    assert checker["required_movement_angle"] == 0.15


def test_segmented_controller_handles_spin_without_rewriting_goal_action():
    source = (PACKAGE / "src/segmented_rpp_controller.cpp").read_text()
    assert "PLUGINLIB_EXPORT_CLASS(" in source
    assert 'rpp_->setPlan(slice)' in source
    assert "stages_[stage_index_]" in source
    assert "/patrol_navigation/applied_follow_path" in source
    assert "follow_path_publisher_->publish(notice)" in source
    assert "emitted_ns" in source
    assert "checkRotationCollision(pose, remaining)" in source
    assert "collision_->footprintCostAtPose(" in source
    # The exact padded chassis-cell validator is shared with the global planner.
    validator = (PACKAGE / "include/patrol_navigation_controller/costmap_footprint_validator.hpp").read_text()
    assert 'costmap_footprint_validator.hpp' in source
    assert "polygonFootprintClear(" in source
    assert "INSCRIBED_INFLATED_OBSTACLE" in validator
    assert "LETHAL_OBSTACLE" in validator
    assert "costmap_->isCurrent()" in source
    assert "footprint.size() < 3" in source
    assert "polygonFootprintClear(" in source
    assert "nav2_costmap_2d::LETHAL_OBSTACLE" not in source
    assert "std::runtime_error" in source  # Humble exception path
    assert "nav2_core::NoValidControl" not in source
    assert "stationaryForSegmentTransition(" in source
    assert "transfer_linear_stop_, transfer_angular_stop_" in source
    assert "Invalid pose or velocity at segment handoff" in source
    assert "Lattice path contains an invalid pose" in source
    assert "validUnitQuaternion(" in source
    assert "std::isfinite(limit)" in source
    assert "std::abs(velocity.linear.x) >= transfer_linear_stop_" not in source
    assert "inControllerFrame(" in source
    assert "nav2_util::transformPoseInTargetFrame(" in source
    # Humble nav2_core::Controller::Ptr is a shared_ptr and pluginlib's
    # createUniqueInstance() cannot be assigned to it.
    assert "rpp_loader_.createSharedInstance(" in source
    assert "rpp_loader_.createUniqueInstance(" not in source
    # Costmap2DROS owns footprint/layered costmap APIs, not Costmap2D.
    assert "const auto footprint = costmap_->getRobotFootprint();" in source
    assert "costmap_->getRobotFootprint()" in source
    assert "costmap->getRobotFootprint()" not in source


def test_lattice_controller_is_available_in_navigation_launch():
    nav_package = ET.parse(ROS_ROOT / "patrol_navigation/package.xml").getroot()
    dependencies = [node.text for node in nav_package.findall("exec_depend")]
    assert "patrol_navigation_controller" in dependencies
    launch = (ROS_ROOT / "patrol_navigation/launch/navigation.launch.py").read_text()
    assert '"FollowPath.segment_pivot_max_angular": max_angular_rps' in launch
    assert '"FollowPath.desired_linear_vel": max_wheel_mps' in launch


def test_ros_free_segment_handoff_safety_cpp(tmp_path):
    compiler = shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        pytest.skip("No C++ compiler; Nav2 still requires a separate Humble build")
    test_source = ROOT / "tests/cpp/test_segment_handoff_guard.cpp"
    binary = tmp_path / "segment_handoff_guard"
    compiled = subprocess.run(
        [
            compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pedantic",
            "-I", str(PACKAGE / "include"), str(test_source), "-o", str(binary),
        ],
        capture_output=True, text=True, timeout=45, check=False,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    executed = subprocess.run(
        [str(binary)], capture_output=True, text=True, timeout=15, check=False,
    )
    assert executed.returncode == 0, executed.stdout + executed.stderr


def test_replanning_replaces_stages_transactionally_and_fails_closed():
    code = (PACKAGE / "src/segmented_rpp_controller.cpp").read_text()
    interface = (
        PACKAGE / "include/patrol_navigation_controller/segmented_rpp_controller.hpp"
    ).read_text()
    assert "bool plan_ready_ = false;" in interface
    assert "void invalidatePlan() noexcept;" in interface
    assert "void advanceStage();" in interface
    replacement = code.split(
        "void SegmentedRPPController::setPlan(", 1
    )[1].split(
        "geometry_msgs::msg::TwistStamped SegmentedRPPController::stopped(", 1
    )[0]
    assert replacement.index("invalidatePlan();") < replacement.index("path_ = path;")
    assert replacement.index("path_ = path;") < replacement.index("buildStages();")
    assert replacement.index("buildStages();") < replacement.index("activateStage();")
    assert replacement.index("activateStage();") < replacement.index("plan_ready_ = true;")
    assert "catch (...) {\n    invalidatePlan();\n    throw;" in replacement
    compute = code.split(
        "SegmentedRPPController::computeVelocityCommands(", 1
    )[1].split("void SegmentedRPPController::setSpeedLimit(", 1)[0]
    assert compute.index("if (!plan_ready_)") < compute.index("stages_[stage_index_]")
    assert "Segmented controller has no valid active path" in compute
    assert compute.count("advanceStage();") == 2
    assert "++stage_index_;" not in compute
    handoff = code.split("void SegmentedRPPController::advanceStage()", 1)[1]
    handoff = handoff.split("void SegmentedRPPController::setPlan(", 1)[0]
    assert "++stage_index_;" in handoff
    assert "activateStage();" in handoff
    assert "catch (...) {\n    invalidatePlan();\n    throw;" in handoff
    invalidate = code.split("void SegmentedRPPController::invalidatePlan()", 1)[1]
    invalidate = invalidate.split("void SegmentedRPPController::advanceStage()", 1)[0]
    for state_clear in (
        "plan_ready_ = false;", "stages_.clear();",
        "stage_index_ = 0;", "path_.poses.clear();",
    ):
        assert state_clear in invalidate
    assert "invalidatePlan();\n  rpp_->deactivate();" in code
    assert "invalidatePlan();\n}" in code


def test_pivot_entry_stop_is_not_reapplied_during_active_rotation():
    source = (PACKAGE / "src/segmented_rpp_controller.cpp").read_text()
    header = (
        PACKAGE / "include/patrol_navigation_controller/segmented_rpp_controller.hpp"
    ).read_text()
    guard = (
        PACKAGE / "include/patrol_navigation_controller/segment_handoff_guard.hpp"
    ).read_text()
    assert "bool pivot_started_ = false;" in header
    assert "pivot_started_ = false;" in source
    assert "pivot_started_ = true;" in source
    assert "safeToContinuePivot(" in source
    assert "if (!pivot_started)" in guard
    assert "return std::abs(linear_x) < linear_stop_threshold;" in guard
    assert "return stationaryForSegmentTransition(" in guard
    assert "pivot_started_, velocity.linear.x, velocity.angular.z," in source


def test_nav2_motion_commands_require_fresh_local_swept_chassis_clearance():
    source = (PACKAGE / "src/segmented_rpp_controller.cpp").read_text()
    header = (
        PACKAGE / "include/patrol_navigation_controller/segmented_rpp_controller.hpp"
    ).read_text()
    pure = (
        PACKAGE / "include/patrol_navigation_controller/local_motion_sweep.hpp"
    ).read_text()
    assert "checkedCommand(" in header
    assert source.count(
        "return checkedCommand(pose, rpp_->computeVelocityCommands("
    ) == 2
    assert "const auto certified = checkedCommand(pose, command);" in source
    assert source.index("const auto certified = checkedCommand(") < source.index(
        "pivot_started_ = true;"
    )
    assert "costmap_->isCurrent()" in source
    assert "costmap_->getGlobalFrameID()" in source
    assert "localMotionHorizon(initial, linear, angular)" in source
    assert "return validateSweptPath(" not in source  # checkedCommand fails by throw
    assert "const bool safe = validateSweptPath(" in source
    assert "collision_->footprintCostAtPose(" in source
    assert "polygonFootprintClear(map, sample, cost, footprint)" in source
    assert "polygonFootprintClear(" in source
    assert "Local swept chassis obstructed during Nav2 motion" in source
    assert "travel > 0.25 || rotation > 1.20" in pure


def test_local_and_global_use_the_same_full_chassis_collision_contract():
    local = (PACKAGE / "src/segmented_rpp_controller.cpp").read_text()
    global_planner = (PACKAGE / "src/time_scored_lattice_planner.cpp").read_text()
    shared = (PACKAGE / "include/patrol_navigation_controller/costmap_footprint_validator.hpp").read_text()
    for code in (local, global_planner):
        assert 'costmap_footprint_validator.hpp' in code
        assert 'polygonFootprintClear(' in code
    assert "polygonFootprintClear(map, sample, cost, footprint)" in local
    assert "polygonOverlapsGridCell(polygon, wx, wy, resolution / 2.0)" in shared
    assert "footprint_cost >= static_cast<double>(nav2_costmap_2d::LETHAL_OBSTACLE)" in shared
    assert "if (cell_cost < nav2_costmap_2d::LETHAL_OBSTACLE)" in shared


def test_ros_free_local_motion_horizon_cpp(tmp_path):
    compiler = shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        pytest.skip("No C++ compiler available")
    binary = tmp_path / "local_motion_horizon"
    code = ROOT / "tests/cpp/test_local_motion_sweep.cpp"
    result = subprocess.run(
        [
            compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pedantic",
            "-I", str(PACKAGE / "include"), str(code), "-o", str(binary),
        ],
        capture_output=True, text=True, check=False, timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    result = subprocess.run(
        [str(binary)], capture_output=True, text=True, check=False, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_pivot_collision_samples_are_in_local_costmap_frame():
    source = (PACKAGE / "src/segmented_rpp_controller.cpp").read_text()
    rotation = source.split(
        "void SegmentedRPPController::checkRotationCollision(", 1
    )[1].split(
        "geometry_msgs::msg::TwistStamped SegmentedRPPController::computeVelocityCommands(", 1
    )[0]
    assert "inControllerFrame(pose, costmap_->getGlobalFrameID())" in rotation
    assert "const double heading = yaw(local_pose);" in rotation
    assert "local_pose.pose.position.x, local_pose.pose.position.y" in rotation
    assert "pose.pose.position.x, pose.pose.position.y, test_heading" not in rotation
    assert "Local costmap became stale during pivot check" in rotation
    assert rotation.index("auto local_pose") < rotation.index("costmap->getMutex()")
    assert rotation.index("costmap->getMutex()") < rotation.index("costmap_->isCurrent()) {")
    assert "validUnitQuaternion(" in rotation



def test_nav2_rejects_unsupported_twist_components_before_stop_shortcut():
    cpp = (PACKAGE / "src/segmented_rpp_controller.cpp").read_text()
    start = cpp.index("SegmentedRPPController::checkedCommand(")
    end = cpp.index("void SegmentedRPPController::checkRotationCollision(", start)
    command = cpp[start:end]
    header = (
        PACKAGE / "include/patrol_navigation_controller/segment_handoff_guard.hpp"
    ).read_text()
    assert "validDifferentialDriveCommand(" in command
    assert "command.twist.linear.z" in command
    assert command.index("if (!validDifferentialDriveCommand(") < command.index(
        "if (std::abs(linear) < 1e-9"
    )
    for axis in ("linear_y", "linear_z", "angular_x", "angular_y"):
        assert f"std::abs({axis}) <= epsilon" in header
    assert "std::isfinite(value)" in header


def test_final_yaw_rotation_requires_swept_footprint_certification():
    source = (PACKAGE / "src/segmented_rpp_controller.cpp").read_text()
    final = source.split('if (stage_index_ + 1 >= stages_.size()) {', 1)[1].split(
        'const auto end_pose = storedPathPoseInControllerFrame(', 1
    )[0]
    assert 'path_.poses.back()' in final
    assert 'distance(pose, terminal) <= 0.25' in final
    assert 'if (!safeToContinuePivot(' in final
    assert 'checkRotationCollision(pose, remaining);' in final
    assert final.index('checkRotationCollision(pose, remaining);') < (
        final.index('command.twist.angular.z = boundedPivotAngularCommand(')
    )
    assert 'return certified;' in final


def test_final_yaw_satisfied_cannot_resume_terminal_linear_rpp():
    source = (PACKAGE / "src/segmented_rpp_controller.cpp").read_text()
    final = source.split("if (stage_index_ + 1 >= stages_.size()) {", 1)[1].split(
        "const auto end_pose = storedPathPoseInControllerFrame(", 1
    )[0]
    assert "if (distance(pose, terminal) <= 0.25)" in final
    assert "return stopped(pose);" in final
    assert "pivot_started_ = false;" in final
    assert final.index("pivot_started_ = false;") < final.index(
        "return checkedCommand(pose, rpp_->computeVelocityCommands"
    )

def test_stored_lattice_targets_use_latest_tf_while_live_poses_retain_stamps():
    """A long FollowPath run must not query expired planning-time map->odom TF."""
    source = (PACKAGE / "src/segmented_rpp_controller.cpp").read_text()
    header = (
        PACKAGE / "include/patrol_navigation_controller/segmented_rpp_controller.hpp"
    ).read_text()
    assert "storedPathPoseInControllerFrame(" in header
    helper = source.split(
        "SegmentedRPPController::storedPathPoseInControllerFrame(", 1
    )[1].split("void SegmentedRPPController::buildStages()", 1)[0]
    assert "auto latest_target = path_pose;" in helper
    assert "latest_target.header.stamp.sec = 0;" in helper
    assert "latest_target.header.stamp.nanosec = 0;" in helper
    assert "return inControllerFrame(latest_target, target_frame);" in helper
    # The final XY/yaw, drive-segment handoff, pivot center, and pivot target
    # must all use the same latest-TF semantics.
    for path_expr in (
        "path_.poses.back()",
        "path_.poses[stage.begin]",
    ):
        assert path_expr + ", pose.header.frame_id);" in source
    assert source.count("= storedPathPoseInControllerFrame(") == 4
    assert source.count(
        "const auto local_pose = inControllerFrame(pose, costmap_->getGlobalFrameID());"
    ) == 2




def test_pivot_slew_uses_last_accepted_command_not_noisy_odom_derivative():
    cpp = (PACKAGE / "src/segmented_rpp_controller.cpp").read_text()
    header = (
        PACKAGE / "include/patrol_navigation_controller/segmented_rpp_controller.hpp"
    ).read_text()
    helper = (
        PACKAGE / "include/patrol_navigation_controller/segment_handoff_guard.hpp"
    ).read_text()
    assert "last_pivot_angular_command_ = 0.0;" in header
    assert cpp.count("command.twist.angular.z = boundedPivotAngularCommand(") == 2
    assert cpp.count("last_pivot_angular_command_ = certified.twist.angular.z;") == 2
    assert "velocity.angular.z - maximum_step" not in cpp
    assert "velocity.angular.z - step" not in cpp
    assert cpp.count("last_pivot_angular_command_ = 0.0;") >= 4
    assert "std::clamp(desired, -angular_limit, angular_limit)" in helper
    assert "std::abs(previous_command) > angular_limit" in helper


def test_subepsilon_motion_is_exact_stop_before_projection():
    cpp = (PACKAGE / "src/segmented_rpp_controller.cpp").read_text()
    checked = cpp.split("SegmentedRPPController::checkedCommand(", 1)[1].split(
        "void SegmentedRPPController::checkRotationCollision(", 1
    )[0]
    assert "if (std::abs(linear) < 1e-9 && std::abs(angular) < 1e-9)" in checked
    assert "return stopped(pose);" in checked
    assert checked.index("validDifferentialDriveCommand(") < checked.index(
        "return stopped(pose);"
    )
    assert "Cannot certify Nav2 motion horizon: linear=" in checked
