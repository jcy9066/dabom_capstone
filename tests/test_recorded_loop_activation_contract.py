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


def test_replan_is_selectable_only_until_loop_starts():
    html = (ROOT / "frontend/templates/index.html").read_text(encoding="utf-8")
    match = re.search(r"<option\s+value=['\"]REPLAN['\"][^>]*>", html)
    assert match and "disabled" not in match.group(0)
    js = (ROOT / "frontend/services/static/recorded_loop_ui.js").read_text(encoding="utf-8")
    assert '$("recorded-policy").disabled = busy || active' in js
    assert 'policy: $("recorded-policy").value' in js


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
