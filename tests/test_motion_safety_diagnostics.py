import threading
import time
from types import SimpleNamespace

import pytest

from server.navigation_control_api import NavigationControlApi


@pytest.mark.parametrize("scenario,expected", [
    ("healthy", "SAFETY_STATE_CHANGED_DURING_CHECK"),
    ("gpu_estop", "GPU_EMERGENCY_STOP"),
    ("encoder_violation", "ENCODER_MOVEMENT_AFTER_STOP"),
    ("stop_unarmed", "STOP_CONFIRMATION_NOT_ARMED"),
    ("stop_unconfirmed", "STOP_NOT_CONFIRMED"),
    ("disconnected", "PI_DISCONNECTED"),
    ("stale", "PI_STATUS_STALE"),
    ("pi_estop", "PI_EMERGENCY_STOP_ACTIVE"),
    ("pi_estop_unknown", "PI_ESTOP_UNKNOWN"),
    ("no_session", "PI_SAFETY_SESSION_MISSING"),
    ("bad_epoch", "PI_SAFETY_EPOCH_INVALID"),
])
def test_motion_safety_rejection_reason(scenario, expected):
    api = NavigationControlApi.__new__(NavigationControlApi)
    api._lock = threading.RLock()
    api._state = {"emergency_stop": False}
    api._watchdog = SimpleNamespace(pi_timeout_sec=3.0)
    api._encoder_stop_violation = False
    api._stop_commanded_at = None
    api._post_resume_motion_ready = False
    api._stop_confirmation_after_sequence = None
    api._connected = True
    api._pi_updated_at = time.time()
    api._pi_estop_latched = False
    api._pi_safety_session = "test-session"
    api._pi_safety_epoch = 28
    api._estop_generation = 0

    if scenario == "gpu_estop":
        api._state["emergency_stop"] = True
    elif scenario == "encoder_violation":
        api._encoder_stop_violation = True
    elif scenario in ("stop_unarmed", "stop_unconfirmed"):
        api._stop_commanded_at = time.time()
        if scenario == "stop_unconfirmed":
            api._stop_confirmation_after_sequence = 10
            api._stop_confirmation_started_at = None
            api._stop_confirmation_last_at = None
    elif scenario == "disconnected":
        api._connected = False
    elif scenario == "stale":
        api._pi_updated_at -= 10
    elif scenario == "pi_estop":
        api._pi_estop_latched = True
    elif scenario == "pi_estop_unknown":
        api._pi_estop_latched = None
    elif scenario == "no_session":
        api._pi_safety_session = None
    elif scenario == "bad_epoch":
        api._pi_safety_epoch = -1

    context = api.motion_safety_context()
    assert (context is None) == (scenario != "healthy")
    assert api.motion_safety_rejection_reason() == expected
