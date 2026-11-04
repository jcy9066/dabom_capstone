"""Stored-map localization may use a bounded turn only in an idle DRIVING state.

These code contracts complement the final ROS2/Pi physical safety verification.
"""
from pathlib import Path


def test_stored_map_turn_requires_idle_nav2_and_saved_map():
    root = Path(__file__).resolve().parents[1]
    gpu = (root / "server/navigation_control_api.py").read_text(encoding="utf-8")
    pi = (root / "raspberry/robot_command_client.py").read_text(encoding="utf-8")
    turn = gpu.split("async def _bounded_initial_pose_turn(", 1)[1].split(
        "async def _wait_initial_pose_stop_proof(", 1
    )[0]
    assert 'navigation_mode in {"MAPPING", "DRIVING"}' in turn
    assert 'nav_state.get("state") not in {"IDLE", "SUCCEEDED", "CANCELED"}' in turn
    assert 'saved_map is None or not getattr(saved_map, "yaml_path", None)' in turn
    assert 'verify_localization_turn_clearance(self._get_live_scan())' in turn
    assert 'self._loop_session_owner.done()' in turn
    assert '"initial_pose_turn"' in gpu
    assert 'self.navigation_mode not in {"mapping", "driving"}' in pi
    assert 'require_mapping_health' in turn
    assert 'if not mapping_ready and (' in turn
    localization = (root / "server/localization_session_api.py").read_text(
        encoding="utf-8",
    )
    assert 'require_mapping_health=self.source == "current"' in localization
    assert 'self.motor.stop("initialpose_local_stop")' in pi
