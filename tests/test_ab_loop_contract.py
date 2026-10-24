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
LAUNCH = (ROOT / "navigation/ros/patrol_navigation/launch/navigation.launch.py").read_text(encoding="utf-8")
CONFIG_DIR = ROOT / "navigation/ros/patrol_navigation/config"


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


def test_loop_caches_two_physically_driven_directions_and_reuses_them():
    assert "def begin_loop_trace(self)" in ROS
    assert "def finish_loop_trace(self)" in ROS
    assert "self._loop_trace.append(sample)" in ROS
    assert "def _certify_physical_loop_path(" in SERVER
    assert "self._loop_path_cache[direction] = certified" in SERVER
    assert "direction not in self._loop_path_cache" in SERVER
    assert "self._loop_completed_legs >= 1" in SERVER
    assert "self._loop_path_cache.get(next_index, [])" in SERVER
    assert "if reuse:" in SERVER
    assert "self._ros.follow_loop_path, plan" in SERVER
    assert "self._loop_cache_hits += 1" in SERVER
    assert "self._loop_path_cache.clear()" in SERVER
    assert "self._ros.discard_loop_trace()" in SERVER


def test_cached_followpath_checks_dynamic_obstacles_before_replanning():
    assert "def loop_path_valid(self, poses" in ROS
    assert 'self._path_validity_client.call_async(request)' in ROS
    assert "def _remaining_loop_path(" in SERVER
    assert "if await asyncio.to_thread(self._ros.loop_path_valid, remainder)" in SERVER
    assert "self._loop_path_cache.pop(index, None)" in SERVER
    assert "self._loop_replans += 1" in SERVER
    assert "self._ros.follow_loop_path, fresh, preempt=True" in SERVER
    assert '"LOOP_CACHE_VALIDATION_FAILED"' in SERVER
    assert "self._pending_nav_success is None" in SERVER


def test_forward_only_loop_planner_is_separate_from_existing_goal_settings():
    assert 'planner_plugins: ["GridBased", "LoopGridBased"]' in CONFIG
    assert 'LoopGridBased:' in CONFIG
    section = CONFIG.split("    LoopGridBased:", 1)[1].split("smoother_server:", 1)[0]
    assert section.count("allow_reverse_expansion: false") == 2
    assert "allow_reverse_expansion: true" not in section
    regular = CONFIG.split("    GridBased:", 1)[1].split("    LoopGridBased:", 1)[0]
    assert regular.count("allow_reverse_expansion: true") == 2
    assert '"LoopGridBased.QuickMotion.lattice_filepath"' in LAUNCH
    assert '"LoopGridBased.StableTurns.lattice_filepath"' in LAUNCH
    assert "lattice_diff32_loop.json" in LAUNCH
    assert "lattice_diff32_loop_stable.json" in LAUNCH
    assert (CONFIG_DIR / "lattice_diff32_loop.json").read_bytes() == (
        CONFIG_DIR / "lattice_diff32.json"
    ).read_bytes()
    assert (CONFIG_DIR / "lattice_diff32_loop_stable.json").read_bytes() == (
        CONFIG_DIR / "lattice_diff32_stable.json"
    ).read_bytes()
    tree = ET.parse(RECOVERY_TREE)
    assert all(
        node.attrib.get("planner_id") == "LoopGridBased"
        for node in tree.iter("ComputePathToPose")
    )
    assert "loop_forward=bool(loop_goals)" in SERVER
    assert "loop_forward=True" in SERVER
    assert 'request.planner_id = "LoopGridBased"' in ROS


def test_real_trajectory_cache_replays_on_dedicated_nav2_follow_path():
    assert "def begin_loop_trace(self)" in ROS
    assert "def finish_loop_trace(self)" in ROS
    assert "self._loop_trace.append(sample)" in ROS
    assert "def follow_loop_path(" in ROS
    assert "request = FollowPath.Goal()" in ROS
    assert 'request.controller_id = "LoopFollowPath"' in ROS
    assert 'request.goal_checker_id = "loop_cached_goal_checker"' in ROS
    assert "loop_cached_goal_checker:" in CONFIG
    assert "xy_goal_tolerance: 0.10" in CONFIG
    assert "def _certify_physical_loop_path(" in SERVER
    assert "def _remaining_loop_path(" in SERVER
    assert "self._loop_path_cache[direction] = certified" in SERVER
    assert "self._ros.follow_loop_path, plan" in SERVER
    assert "self._ros.loop_path_valid, remainder" in SERVER
    assert "self._ros.follow_loop_path, fresh, preempt=True" in SERVER
    assert "self._loop_path_cache.clear()" in SERVER


def test_cached_goal_tolerance_is_honored_by_loop_controller():
    # Cached FollowPath demands XY 0.10m; the first planned loop leg uses
    # XY 0.55m. The controller must not STOP at the broader tolerance for
    # both or the cached path action can never terminate.
    assert 'loop_cached_goal_checker:' in CONFIG
    assert 'xy_goal_tolerance: 0.10' in CONFIG
    assert 'goal_checker->getTolerances(pose_tolerance, velocity_tolerance)' in LOOP_CPP
    assert 'terminal_xy_tolerance = std::clamp(' in LOOP_CPP
    assert 'distance(pose, terminal) <= terminal_xy_tolerance' in LOOP_CPP
    assert 'controller_id = "LoopFollowPath"' in ROS or (
        'request.controller_id = "LoopFollowPath"' in ROS
    )


def test_forward_only_planner_is_separate_from_regular_set_goal():
    assert 'planner_plugins: ["GridBased", "LoopGridBased"]' in CONFIG
    assert "LoopGridBased:" in CONFIG
    assert "allow_reverse_expansion: false" in CONFIG
    assert 'request.planner_id = "LoopGridBased"' in ROS
    assert "loop_forward=True" in SERVER
    assert 'planner_id="LoopGridBased"' in RECOVERY_TREE.read_text(encoding="utf-8")
    assert "loop_reapproach=True" in SERVER


def test_operator_cancel_manual_mapping_estop_revoke_loop_before_next_motor_action():
    assert 'abort_loop=True' in SERVER
    assert 'abort_loop_for_mode="manual"' in SERVER
    assert 'abort_loop_for_mode="mapping"' in SERVER
    assert 'was_loop = bool(self._loop_goals)' in SERVER
    assert '_clear_loop_locked()' in SERVER
    assert 'epoch != self._loop_epoch' in SERVER
    assert 'LOOP_STAGE_FAILED' in SERVER
