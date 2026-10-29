"""Motor ownership contracts independent of the ROS runtime."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_public_motion_api_blocks_loop_owned_motors():
    app = (ROOT / "server/app.py").read_text(encoding="utf-8")
    route = app.split('@app.post("/api/robots/{robot_id}/command")', 1)[1]
    assert "loop_session_api.playing" in route
    assert 'command_type in {"move", "auto_drive"}' in route
    assert "and not nav2_dry_run" in route
    assert '"LOOP_ACTIVE: Stop the active Loop' in route


def test_manual_loop_owner_released_only_after_final_stop():
    source = (ROOT / "server/loop_session_api.py").read_text(encoding="utf-8")
    ending = source.split('    async def _run(', 1)[1].split('    def start(', 1)[0]
    stop = ending.rfind("await self._verified_stop()")
    release = ending.rfind("self.navigation._loop_session_owner = None")
    assert stop != -1 and release > stop


def test_initial_pose_turn_and_loop_are_mutually_exclusive():
    loop = (ROOT / "server/loop_session_api.py").read_text(encoding="utf-8")
    pose = (ROOT / "server/navigation_control_api.py").read_text(encoding="utf-8")
    assert "LOCALIZATION_TURN_ACTIVE" in loop
    assert "self._loop_session_owner.done()" in pose
