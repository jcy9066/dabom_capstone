"""Waypoint route contract: Humble NavigateThroughPoses and final-only yaw.

Static contracts. A colcon build and Nav2 runtime action test are required.
"""
from pathlib import Path
from xml.etree import ElementTree as ET
import yaml


ROOT = Path(__file__).resolve().parents[1]
NAV = ROOT / "navigation/ros/patrol_navigation"
XML = NAV / "behavior_trees/navigate_through_poses_dynamic_replanning.xml"


def test_0p5m_xy_only_intermediates_and_final_goal_remains():
    doc = ET.parse(XML)
    root = doc.getroot()
    assert root.attrib["main_tree_to_execute"] == "MainTree"
    n = root.find(".//RemovePassedGoals")
    assert n is not None
    assert float(n.attrib["radius"]) == 0.50
    assert n.attrib["input_goals"] == "{goals}"
    assert n.attrib["output_goals"] == "{goals}"
    assert root.find(".//ComputePathToPose") is None


def test_intermediate_replanning_stays_bounded():
    root = ET.parse(XML).getroot()
    sequence = root.find("./BehaviorTree/Sequence")
    assert sequence is not None
    first_plan = sequence.find("./Timeout/ComputePathThroughPoses")
    assert first_plan is not None
    assert first_plan.attrib["goals"] == "{goals}"
    assert first_plan.attrib["planner_id"] == "GridBased"
    rate = root.find(".//RateController")
    assert rate is not None and float(rate.attrib["hz"]) == 10.0
    fallback = rate.find("./Fallback")
    assert fallback[0].find("./RemovePassedGoals") is not None
    assert fallback[0].find("./IsPathValid") is not None
    reactive = fallback[1]
    assert reactive.tag == "ReactiveSequence"
    assert reactive[0][0].tag == "IsPathValid"
    assert int(reactive[1].attrib["delay_msec"]) == 300
    timeout = reactive[1].find("./Timeout")
    assert int(timeout.attrib["msec"]) == 5000
    attempts = timeout.find("./RetryUntilSuccessful")
    assert int(attempts.attrib["num_attempts"]) == 5
    assert attempts[0].tag == "ComputePathThroughPoses"
    assert root.find(".//RecoveryNode") is None
    assert root.find(".//Spin") is None
    assert root.find(".//BackUp") is None


def test_final_goal_yaw_checker_and_launch_nav2_bt():
    params = yaml.safe_load((NAV / "config/nav2_params.yaml").read_text())
    controller = params["controller_server"]["ros__parameters"]
    checker = controller["general_goal_checker"]
    assert checker["xy_goal_tolerance"] == 0.25
    assert checker["yaw_goal_tolerance"] == 0.12
    plugins = params["bt_navigator"]["ros__parameters"]["plugin_lib_names"]
    assert "nav2_remove_passed_goals_action_bt_node" in plugins
    assert "nav2_compute_path_through_poses_action_bt_node" in plugins
    launch = (NAV / "launch/navigation.launch.py").read_text()
    assert '"default_nav_through_poses_bt_xml": navigate_through_poses_bt' in launch


def test_waypoint_api_uses_humble_multi_pose_ros_actions():
    adapter = (ROOT / "server/navigation_ros_control.py").read_text()
    assert '"/navigate_through_poses"' in adapter
    assert '"/compute_path_through_poses"' in adapter
    assert "request.poses = [self._pose_stamped(item) for item in poses]" in adapter
    assert "request.goals = [self._pose_stamped(item) for item in poses]" in adapter
    assert "request.use_start = False" in adapter
    api = (ROOT / "server/navigation_control_api.py").read_text()
    assert "def _validated_waypoints(" in api
    assert "return self._ros.navigate_through_poses([*waypoints, final_goal])" in api
    assert "return self._ros.compute_path_through_poses([*waypoints, final_goal])" in api
