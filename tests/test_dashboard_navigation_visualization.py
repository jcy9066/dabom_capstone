from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_dashboard_renders_actual_trajectory_before_scan_in_dark_green():
    script = read("frontend/services/static/script.js")

    assert "data.trajectory" in script
    assert "ctx.strokeStyle = '#166534'" in script
    assert "drawTrajectory(ctx, trajectory, layout);" in script
    assert "dabom:navigation-control-state" in script
    assert "trajectoryMode !== mode" in script
    assert script.index("drawTrajectory(ctx, trajectory, layout);") < script.index(
        "drawScan(ctx, scan, pose, layout);"
    )


def test_dashboard_renders_goal_path_in_dark_red():
    control = read("frontend/services/static/navigation_control.js")

    assert "state.control?.planned_path || []" in control
    assert "ctx.strokeStyle = '#991b1b'" in control


def test_nav2_live_plan_is_subscribed_for_replanning_updates():
    ros_control = read("server/navigation_ros_control.py")

    assert 'NavPath, "/plan", self._on_plan, 10' in ros_control
    assert "def latest_planned_path" in ros_control
    assert "def _on_plan" in ros_control
