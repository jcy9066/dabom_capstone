"""No hidden feature switch: both obstacle policies are available at Start.

Static checks protect the requested default-on UI/API contract. They are not
evidence of physical motor safety; see verify_recorded_loop_software.sh.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def test_no_nav2_replay_feature_flag():
    api = (ROOT / "server/recorded_loop_api.py").read_text(encoding="utf-8")
    js = (ROOT / "frontend/services/static/recorded_loop_ui.js").read_text(encoding="utf-8")
    html = (ROOT / "frontend/templates/index.html").read_text(encoding="utf-8")
    for source in (api, js, html):
        assert "RECORDED_NAV2_REPLAY_ENABLE" not in source
        assert "nav2_replay_enabled" not in source


def test_wait_replan_modal_policy_toggle():
    html = (ROOT / "frontend/templates/index.html").read_text(encoding="utf-8")
    ui = (ROOT / "frontend/services/static/recorded_loop_ui.js").read_text(encoding="utf-8")
    assert 'id="loop-modal-trigger"' in html
    assert 'id="loop-goal-toggle"' in html
    assert 'id="recordedLoopPanel"' not in html
    assert 'id="loop-policy-wait"' in ui
    assert 'id="loop-policy-replan"' in ui
    assert 'aria-pressed' in ui


def test_replan_and_follow_path_have_real_callbacks():
    api = (ROOT / "server/recorded_loop_api.py").read_text(encoding="utf-8")
    assert "async def _play_replan" in api
    assert "async def _play_follow" in api
    assert "ros.compute_path" in api
    assert "ros.follow_loop_path" in api
    assert "async def _nav_action" in api
    assert "async def _play_stop" in api
    assert "STOP_NOT_CONFIRMED" in api


def test_completed_legacy_goal_permits_safe_recorded_replay():
    nav = (ROOT / "server/navigation_control_api.py").read_text(encoding="utf-8")
    assert 'def _recorded_idle_locked' in nav
    assert 'in {"READY", "SUCCEEDED"}' in nav
    assert 'self._pending_nav_success is None' in nav
    assert 'self._pending_terminal_stop is None' in nav
    assert 'self._stop_confirmed_locked(time.time())' in nav
    assert 'or not self._recorded_idle_locked()' in nav
    assert 'stale_success_only = (' in nav
    executor = (ROOT / "server/recorded_loop_executor.py").read_text(encoding="utf-8")
    assert 'await self._verified_stop()\n            await self._health()' in executor


def test_inflight_recorded_stop_blocks_all_legacy_mode_changes():
    nav = (ROOT / "server/navigation_control_api.py").read_text(encoding="utf-8")
    app = (ROOT / "server/app.py").read_text(encoding="utf-8")
    assert "set_recorded_cancel_callback(stop_recorded_loop_owners)" in app
    assert "await loop_session_api.stop(from_navigation=True)" in app
    assert "Even without a motor-owner bit, a replay Task may be STOPPING" in nav
    assert "result = await handler()" in nav
    for method in ("async def cancel_goal", "async def switch_mode",
                   "async def switch_patrol_mode"):
        region = nav.split(method, 1)[1].split("\n    async def ", 1)[0]
        assert "await self._stop_recorded_before_transition()" in region


def test_single_loop_execution_and_trajectory_only():
    html = (ROOT / "frontend/templates/index.html").read_text(encoding="utf-8")
    ui = (ROOT / "frontend/services/static/recorded_loop_ui.js").read_text(encoding="utf-8")
    module = (ROOT / "server/loop_session_api.py").read_text(encoding="utf-8")
    assert 'id="loop-modal-trigger"' in html
    assert 'manager.register("loopModal"' in ui
    assert 'async def _manual_bag' in module
    assert 'async def _auto_goals' in module
    assert 'self._maps' in module
    assert 'recorded_loops.json' not in module


def test_manual_recording_requires_explicit_auto_mode_to_play():
    api = (ROOT / "server/recorded_loop_api.py").read_text(encoding="utf-8")
    assert '"AUTO_MODE_REQUIRED"' in api
    assert 'self.navigation._pi_navigation_mode != "DRIVING"' in api
    assert "Switch from Manual to AUTO in DRIVING before recorded playback" in api


def test_recorded_pwm_is_not_boosted_by_nav2_minimum():
    driver = (ROOT / "raspberry/controllers/motor_controller.py").read_text(encoding="utf-8")
    pi = (ROOT / "raspberry/robot_command_client.py").read_text(encoding="utf-8")
    server = (ROOT / "server/recorded_loop_motor_math.py").read_text(encoding="utf-8")
    assert "preserve_recorded_pwm: bool = False" in driver
    assert "not preserve_recorded_pwm and 0.0 < peak < MIN_AUTO_DRIVE_PWM" in driver
    assert 'message.get("source") == "recorded_loop"' in pi
    assert '"source": "recorded_loop"' in server
    assert "cap_motor_pwm_pair(" in driver
