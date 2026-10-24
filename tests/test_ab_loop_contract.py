"""Source-contract regression checks for physically verified A/B loop turns."""
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "server/navigation_control_api.py").read_text(encoding="utf-8")
ROS = (ROOT / "server/navigation_ros_control.py").read_text(encoding="utf-8")
CONTROL = (ROOT / "frontend/services/static/navigation_control.js").read_text(encoding="utf-8")
VIEWER = (ROOT / "frontend/services/static/lidar_3d_viewer.js").read_text(encoding="utf-8")
HTML = (ROOT / "frontend/templates/index.html").read_text(encoding="utf-8")
CONFIG = (ROOT / "navigation/ros/patrol_navigation/config/nav2_params.yaml").read_text(encoding="utf-8")
LOOP_CPP = (ROOT / "navigation/ros/patrol_navigation_controller/src/segmented_rpp_controller.cpp").read_text(encoding="utf-8")
RECOVERY_TREE = ROOT / "navigation/ros/patrol_navigation/behavior_trees/navigate_loop_reapproach_forward.xml"


def test_each_loop_leg_has_obstacle_replanning_but_no_passed_goal_culling():
    tree = ET.parse(RECOVERY_TREE)
    tags = {node.tag for node in tree.iter()}
    assert {"ComputePathToPose", "IsPathValid", "FollowPath"} <= tags
    assert "RemovePassedGoals" not in tags
    assert any(
        node.attrib.get("controller_id") == "LoopFollowPath"
        and node.attrib.get("goal_checker_id") == "loop_goal_checker"
        for node in tree.iter("FollowPath")
    )
    assert 'controller_plugins: ["FollowPath", "LoopFollowPath"]' in CONFIG
    assert "loop_forward_only: true" in CONFIG
    assert "allow_reversing: false" in CONFIG
    assert "yaw_goal_tolerance: 3.14" in CONFIG


def test_loop_uses_single_leg_then_verified_spin_not_rolling_waypoint_action():
    assert 'data-lidar-interaction="set-goal"' in HTML
    assert 'data-lidar-interaction="set-loop"' in HTML
    assert "loop_goals: loopGoals" in CONTROL
    assert "dabom:navigation-loop-draft" in VIEWER
    assert 'self._loop_stage = "drive"' in SERVER
    assert 'self._loop_stage = "turn_pending"' in SERVER
    assert 'self._loop_stage = "drive_pending"' in SERVER
    assert 'self._ros.spin_loop_turn, angle' in SERVER
    assert 'self._ros.navigate_to_pose, target, loop_reapproach=True' in SERVER
    assert 'self._loop_pose_confirmed(' in SERVER
    assert 'need_yaw=in_spin' in SERVER
    assert 'self._stop_confirmed_locked(now)' in SERVER
    assert 'LOOP_ROUTE_EXTENSION_FAILED' not in SERVER
    assert 'def spin_loop_turn(self, radians' in ROS
    assert 'self._navigate_action_kind in {"navigate_through_poses", "spin"}' in ROS


def test_one_backup_then_same_goal_reapproach_before_next_forward_leg():
    assert "self._loop_reverse_used" in SERVER
    assert 'ros_nav.get("action") != "spin"' in SERVER
    assert "reverse_loop_recovery, 0.35" in SERVER
    assert "self._loop_recovery_reverse_progress(" in SERVER
    assert "loop_reapproach=True" in SERVER
    assert "spin_loop_turn, spin_yaw" in SERVER
    assert "LOOP_RECOVERY_FAILED" in SERVER
    assert 'self._pi_estop_latched is False' in SERVER
    assert 'self._loop_recovery = None' in SERVER
    assert 'self._loop_leg_index = 1 - int(recovery["goal_index"])' in SERVER
    assert 'self._loop_next_turn_clockwise = not self._loop_next_turn_clockwise' in SERVER
    assert 'request.target.x = -float(distance_m)' in ROS


def test_loop_only_skips_terminal_yaw_and_preserves_regular_goal():
    assert "if (loop_forward_only_)" in LOOP_CPP
    assert "return stopped(pose);" in LOOP_CPP
    assert "const double remaining = angles::shortest_angular_distance(" in LOOP_CPP
    assert 'LoopFollowPath' in CONFIG
    assert 'FollowPath:' in CONFIG


def test_arrival_regions_are_separate_and_spin_requires_signed_odometry():
    assert "LOOP_MIN_SPACING_M = 1.20" in SERVER
    assert "self.LOOP_MIN_SPACING_M" in SERVER
    assert "xy_limit = 0.12 if need_yaw else 0.55" in SERVER
    assert "def _loop_signed_turn_confirmed(" in SERVER
    assert "self._ros.spin_progress()" in SERVER
    assert "def spin_progress(self)" in ROS
    assert "self._spin_signed_yaw_rad += delta" in ROS
    assert "self._spin_odom_invalid = True" in ROS
    assert "LOOP_STAGE_PENDING_TIMEOUT_SEC = 8.0" in SERVER
    assert "LOOP_STAGE_WAIT_TIMEOUT" in SERVER
    assert ") < 1.2)" in CONTROL


def test_operator_cancel_manual_mapping_estop_revoke_loop_before_next_motor_action():
    assert 'abort_loop=True' in SERVER
    assert 'abort_loop_for_mode="manual"' in SERVER
    assert 'abort_loop_for_mode="mapping"' in SERVER
    assert 'was_loop = bool(self._loop_goals)' in SERVER
    assert '_clear_loop_locked()' in SERVER
    assert 'epoch != self._loop_epoch' in SERVER
    assert 'LOOP_STAGE_FAILED' in SERVER
