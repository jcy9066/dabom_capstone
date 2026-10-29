"""Pi-owned LOOP MANUAL pulse: duration, STOP and stale replay protections."""
import sys
import threading
from pathlib import Path

import pytest

RASPBERRY_DIR = Path(__file__).resolve().parents[1] / "raspberry"
if str(RASPBERRY_DIR) not in sys.path:
    sys.path.insert(0, str(RASPBERRY_DIR))
from robot_command_client import RobotCommandClient  # noqa: E402


class Motor:
    def __init__(self, fail_stop=False):
        self._command_lock = threading.RLock()
        self.commands = []
        self.fail_stop = fail_stop

    def move(self, direction, speed):
        self.commands.append(("move", direction, speed))

    def stop(self, reason):
        self.commands.append(("stop", reason))
        if self.fail_stop:
            raise RuntimeError("UART STOP failed")


class Pi:
    _safety_session = "abc"
    _safety_epoch = 1

    def __init__(self, fail_stop=False):
        self.motor = Motor(fail_stop)

    def _require_current_motion_safety(self, message):
        if (message.get("safety_session") != self._safety_session
            or message.get("safety_epoch") != self._safety_epoch):
            raise RuntimeError("stale motion safety generation")


def msg(**overrides):
    return {"safety_session": "abc", "safety_epoch": 1, **overrides}


def test_manual_pulse_local_stop_and_exact_duplicate_is_idempotent():
    pi = Pi()
    first = RobotCommandClient._run_manual_loop_pulse(
        pi, msg(), "pulse1", 1, 1, 10,
    )
    again = RobotCommandClient._run_manual_loop_pulse(
        pi, msg(), "pulse1", 1, 1, 10,
    )
    assert first == again
    assert first["stopped"] is True and 0 <= first["elapsed_ms"] <= 10
    assert [cmd[0] for cmd in pi.motor.commands] == ["move", "stop"]


def test_old_sequence_does_not_move_twice():
    pi = Pi()
    RobotCommandClient._run_manual_loop_pulse(pi, msg(), "one", 1, 1, 1)
    with pytest.raises(RuntimeError, match="Stale"):
        RobotCommandClient._run_manual_loop_pulse(pi, msg(), "other", 1, 1, 1)
    assert sum(c[0] == "move" for c in pi.motor.commands) == 1


def test_failed_stop_does_not_report_applied_progress():
    pi = Pi(fail_stop=True)
    with pytest.raises(RuntimeError, match="STOP"):
        RobotCommandClient._run_manual_loop_pulse(pi, msg(), "first", 1, 1, 1)
    assert pi.motor.commands[-1][0] == "stop"


def test_stale_safety_session_does_not_move():
    pi = Pi()
    with pytest.raises(RuntimeError, match="stale"):
        RobotCommandClient._run_manual_loop_pulse(
            pi, msg(safety_epoch=0), "first", 1, 1, 1,
        )
    assert not pi.motor.commands
