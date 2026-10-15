"""Static integration contract for the Humble segmented navigation controller.

These checks supplement, but do NOT replace, a colcon build, Nav2 controller
plugin loading, ROS integration tests or physical robot safety validation.
"""
from pathlib import Path
import xml.etree.ElementTree as ET

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
    assert tracking["rotate_to_goal_heading"] is True
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
    assert "checkRotationCollision(pose, remaining)" in source
    assert "collision_->footprintCostAtPose(" in source
    assert "nav2_costmap_2d::NO_INFORMATION" in source
    assert "std::runtime_error" in source  # Humble exception path
    assert "nav2_core::NoValidControl" not in source
    assert "std::abs(velocity.linear.x) >= transfer_linear_stop_" in source
    assert "inControllerFrame(" in source
    assert "nav2_util::transformPoseInTargetFrame(" in source


def test_lattice_controller_is_available_in_navigation_launch():
    nav_package = ET.parse(ROS_ROOT / "patrol_navigation/package.xml").getroot()
    dependencies = [node.text for node in nav_package.findall("exec_depend")]
    assert "patrol_navigation_controller" in dependencies
    launch = (ROS_ROOT / "patrol_navigation/launch/navigation.launch.py").read_text()
    assert '"FollowPath.segment_pivot_max_angular": max_angular_rps' in launch
    assert '"FollowPath.desired_linear_vel": max_wheel_mps' in launch
