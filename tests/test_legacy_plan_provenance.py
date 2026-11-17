"""Unowned ROS /plan events cannot update approved Goal paths."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_unowned_ros_plan_is_not_dispatched_as_authorized_path():
    app = (ROOT / "server/app.py").read_text(encoding="utf-8")
    start = app.index('if event_type == "global_path":')
    end = app.index('if event_type == "costmap":', start)
    handling = app[start:end]
    assert "navigation_control_api.note_nav2_replan_signal()" not in handling
    assert "navigation_control_api.note_replanned_path" not in handling
    nav = (ROOT / "server/navigation_control_api.py").read_text(encoding="utf-8")
    assert "self._goal_motion_generation == generation" in nav
    assert "self._pending_goal_cancel is None" in nav
