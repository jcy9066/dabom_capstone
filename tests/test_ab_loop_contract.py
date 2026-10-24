"""Source-contract checks for the continuous two-point patrol mode."""
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "server/navigation_control_api.py").read_text(encoding="utf-8")
ROS = (ROOT / "server/navigation_ros_control.py").read_text(encoding="utf-8")
CONTROL = (ROOT / "frontend/services/static/navigation_control.js").read_text(encoding="utf-8")
VIEWER = (ROOT / "frontend/services/static/lidar_3d_viewer.js").read_text(encoding="utf-8")
HTML = (ROOT / "frontend/templates/index.html").read_text(encoding="utf-8")
CONFIG = (ROOT / "navigation/ros/patrol_navigation/config/nav2_params.yaml").read_text(encoding="utf-8")
TREE = ROOT / "navigation/ros/patrol_navigation/behavior_trees/navigate_ab_loop_cached_path.xml"


def test_loop_tree_is_valid_and_checks_obstacles_and_new_goals():
    tree = ET.parse(TREE)
    names = {node.tag for node in tree.iter()}
    assert {"ComputePathThroughPoses", "IsPathValid", "FollowPath",
            "RemovePassedGoals", "GoalUpdated"} <= names
    assert any(node.attrib.get("goal_checker_id") == "loop_goal_checker"
               for node in tree.iter("FollowPath"))
    assert "loop_goal_checker" in CONFIG
    assert "yaw_goal_tolerance: 3.14" in CONFIG


def test_loop_does_not_modify_regular_goal_mode():
    assert 'data-lidar-interaction="set-goal"' in HTML
    assert 'data-lidar-interaction="set-loop"' in HTML
    assert "loop_goals: loopGoals" in CONTROL
    assert "dabom:navigation-loop-draft" in VIEWER
    assert "loop=True" in SERVER
    assert 'loop: bool = False' in ROS
    assert "preempt_navigation_route" in ROS


def test_cancel_manual_mapping_estop_revoke_loop_ownership():
    assert 'abort_loop=True' in SERVER
    assert 'abort_loop_for_mode="manual"' in SERVER
    assert 'abort_loop_for_mode="mapping"' in SERVER
    assert 'was_loop = bool(self._loop_goals)' in SERVER
    assert '_clear_loop_locked()' in SERVER
    assert 'LOOP_ROUTE_EXTENSION_FAILED' in SERVER
