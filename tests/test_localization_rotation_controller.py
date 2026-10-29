"""Pi-local Initial Pose pulse must STOP even if move raises."""
import sys
import threading
from pathlib import Path

import pytest

# Match the actual Pi executable's import root (controllers/ is top-level).
RASPBERRY_DIR = Path(__file__).resolve().parents[1] / "raspberry"
if str(RASPBERRY_DIR) not in sys.path:
    sys.path.insert(0, str(RASPBERRY_DIR))

from robot_command_client import RobotCommandClient


class FakeMotor:
    def __init__(self, fail=False):
        self._command_lock = threading.RLock()
        self.events = []
        self.fail = fail

    def move(self, direction, speed):
        self.events.append(("move", direction, speed))
        if self.fail:
            raise RuntimeError("simulated UART move failure")

    def stop(self, reason):
        self.events.append(("stop", reason))


class ClientStub:
    def __init__(self, fail=False):
        self.motor = FakeMotor(fail)

    def _require_current_motion_safety(self, message):
        if message.get("safety_epoch") != 1:
            raise RuntimeError("stale safety epoch")


def test_pi_local_pulse_stops_after_success():
    stub = ClientStub()
    RobotCommandClient._run_bounded_initial_pose_turn(
        stub, {"safety_epoch": 1}, 1,
    )
    assert stub.motor.events[0] == ("move", "rotate_left", 0.20)
    assert stub.motor.events[-1] == ("stop", "initialpose_local_stop")


def test_pi_local_pulse_stops_even_after_uart_move_failure():
    stub = ClientStub(fail=True)
    with pytest.raises(RuntimeError, match="simulated"):
        RobotCommandClient._run_bounded_initial_pose_turn(
            stub, {"safety_epoch": 1}, 1,
        )
    assert stub.motor.events[-1] == ("stop", "initialpose_local_stop")


def test_stale_safety_epoch_never_moves():
    stub = ClientStub()
    with pytest.raises(RuntimeError, match="stale safety"):
        RobotCommandClient._run_bounded_initial_pose_turn(
            stub, {"safety_epoch": 0}, 1,
        )
    assert not any(event[0] == "move" for event in stub.motor.events)
    assert stub.motor.events[-1][0] == "stop"
