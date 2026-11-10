from __future__ import annotations
import threading
import time
from types import SimpleNamespace
from server.navigation_control_api import NavigationControlApi as NavigationControlAPI


def make_nav():
    nav = object.__new__(NavigationControlAPI)
    nav._lock = threading.RLock()
    nav._motion_handoff = None
    nav._motion_handoff_generation = 0
    nav._state = {"emergency_stop": False, "navigation_state": "READY"}
    nav._watchdog = SimpleNamespace(
        pi_timeout_sec=3.0,
        stop_encoder_grace_sec=0.7,
        stop_encoder_tick_threshold=500,
    )
    nav._encoder_stop_violation = False
    nav._estop_generation = 0
    nav._goal_motion_generation = 0
    nav._patrol_transition = None
    nav._patrol_transition_error = None
    nav._pending_nav_success = None
    nav._sensor_recovery = None
    nav._connected = True
    nav._pi_updated_at = time.time()
    nav._pi_estop_latched = False
    nav._pi_safety_session = "session-1"
    nav._pi_safety_epoch = 4
    nav._robot_mode = "manual"
    nav._pi_navigation_mode = "DRIVING"
    nav._stop_commanded_at = time.time() - 1.0
    nav._stop_encoder_ticks = (0, 0, 0, 0)
    nav._encoder_ticks = (0, 0, 0, 0)
    nav._stop_confirmation_after_sequence = 10
    nav._post_resume_motion_ready = False
    safety = {
        "safety_session": "session-1", "safety_epoch": 4,
        "estop_generation": 0, "goal_generation": 0,
        "robot_mode": "manual", "pi_navigation_mode": "DRIVING",
    }
    nav.motion_safety_context = lambda: dict(safety)
    return nav


def test_acknowledged_motion_commits_old_stop_without_encoder_false_positive():
    nav = make_nav()
    token = nav.begin_motion_handoff({"safety_session": "session-1", "safety_epoch": 4})
    assert token is not None
    nav._encoder_ticks = (590, 576, 556, 571)
    assert nav._motion_handoff_guards_stop_locked(time.time())
    assert nav.complete_motion_handoff(token, True)
    assert nav._stop_commanded_at is None
    assert nav._stop_encoder_ticks is None
    assert nav._motion_handoff is None


def test_new_stop_invalidates_in_flight_move_ack():
    nav = make_nav()
    token = nav.begin_motion_handoff({"safety_session": "session-1", "safety_epoch": 4})
    nav._stop_commanded_at = time.time()
    nav._stop_encoder_ticks = (100, 100, 100, 100)
    assert not nav._motion_handoff_guards_stop_locked(time.time())
    assert not nav.complete_motion_handoff(token, True)
    assert nav._stop_commanded_at is not None


def test_missing_ack_preserves_stop_and_latches_observed_movement():
    nav = make_nav()
    token = nav.begin_motion_handoff({"safety_session": "session-1", "safety_epoch": 4})
    nav._encoder_ticks = (590, 576, 556, 571)
    assert not nav.complete_motion_handoff(token, False)
    assert nav._encoder_stop_violation is True
    assert nav._estop_generation == 1
    assert nav._stop_commanded_at is not None


def test_expired_handoff_cannot_suppress_stop_watchdog():
    nav = make_nav()
    token = nav.begin_motion_handoff({"safety_session": "session-1", "safety_epoch": 4})
    nav._motion_handoff["expires_at"] -= 30
    assert not nav._motion_handoff_guards_stop_locked(time.time())
    assert not nav.complete_motion_handoff(token, True)


def test_pi_safety_epoch_change_invalidates_move():
    nav = make_nav()
    token = nav.begin_motion_handoff({"safety_session": "session-1", "safety_epoch": 4})
    nav._pi_safety_epoch = 5
    assert not nav._motion_handoff_guards_stop_locked(time.time())
    assert not nav.complete_motion_handoff(token, True)


def test_estop_during_inflight_motion_keeps_stop_latched():
    nav = make_nav()
    token = nav.begin_motion_handoff({"safety_session": "session-1", "safety_epoch": 4})
    nav._state["emergency_stop"] = True
    assert not nav._motion_handoff_guards_stop_locked(time.time())
    assert not nav.complete_motion_handoff(token, True)
    assert nav._stop_commanded_at is not None


def test_overlapping_moves_are_rejected_until_previous_ack():
    nav = make_nav()
    command = {"safety_session": "session-1", "safety_epoch": 4}
    token = nav.begin_motion_handoff(command)
    assert token is not None
    assert nav.begin_motion_handoff(command) is None
    assert nav.complete_motion_handoff(token, True)
    assert nav.begin_motion_handoff(command) is not None


def test_post_ack_new_stop_is_preserved():
    nav = make_nav()
    nav._motion_allowed_locked = lambda now: True
    nav.motion_safety_context = NavigationControlAPI.motion_safety_context.__get__(nav, NavigationControlAPI)
    expected = nav.motion_safety_context()
    assert expected is not None
    token = nav.begin_motion_handoff({"safety_session": "session-1", "safety_epoch": 4})
    assert token is not None
    assert nav.complete_motion_handoff(token, True)
    nav._stop_commanded_at = time.time()
    nav._stop_encoder_ticks = (10, 10, 10, 10)
    assert nav.note_authorized_motion(expected) is False
    assert nav._stop_commanded_at is not None
    assert nav._stop_encoder_ticks == (10, 10, 10, 10)
