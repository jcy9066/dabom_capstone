"""Motor-free regressions for forward/reverse/pivot STOP handoffs."""
from __future__ import annotations

import os
import threading
import time
from unittest.mock import Mock

os.environ.setdefault("MIN_AUTO_DRIVE_PWM", "0.50")

import pytest

from raspberry.controllers.direction_interlock import (
    DriveDirectionInterlock,
    segment_mode,
)
from raspberry.controllers.motor_controller import (
    MotorController,
    MotorControllerError,
)


def sample(seq: int, ticks: int, *, age: float = 0.01) -> dict:
    return {
        "sequence": seq,
        "age_sec": age,
        "left_front_ticks": ticks,
        "right_front_ticks": ticks,
        "left_rear_ticks": ticks,
        "right_rear_ticks": ticks,
    }


def test_mode_detection_for_forward_reverse_and_spins():
    assert segment_mode(0.3, 0.2) == "forward"
    assert segment_mode(-0.2, -0.1) == "reverse"
    assert segment_mode(-0.2, 0.2) == "pivot_left"
    assert segment_mode(0.2, -0.2) == "pivot_right"
    assert segment_mode(0, 0) == "stop"
    with pytest.raises(ValueError):
        segment_mode(float("nan"), 1)


def test_handoff_requires_ack_and_fresh_stable_samples():
    interlock = DriveDirectionInterlock()
    assert interlock.request("forward", 5.0, None) == "allow"
    assert interlock.request("reverse", 5.1, None) == "stop"
    assert interlock.request("reverse", 5.2, None) == "stop"
    interlock.mark_stop_sent(5.2, baseline_sequence=10)
    assert interlock.request("reverse", 5.3, sample(10, 0)) == "hold"
    assert interlock.request("reverse", 5.4, sample(11, 1)) == "hold"
    assert interlock.request("reverse", 5.6, sample(12, 1)) == "hold"
    assert interlock.request("reverse", 5.91, sample(13, 1)) == "allow"
    assert interlock.active_mode == "reverse"


def test_moving_encoders_reset_stationary_window():
    lock = DriveDirectionInterlock()
    assert lock.request("forward", 0, None) == "allow"
    assert lock.request("pivot_right", 1, None) == "stop"
    lock.mark_stop_sent(1, 1)
    assert lock.request("pivot_right", 1.1, sample(2, 5)) == "hold"
    assert lock.request("pivot_right", 1.5, sample(3, 6)) == "hold"
    assert lock.request("pivot_right", 1.7, sample(4, 6)) == "hold"
    assert lock.request("pivot_right", 2.05, sample(5, 6)) == "allow"


def test_timeout_or_stale_samples_never_authorize_reversal():
    lock = DriveDirectionInterlock()
    lock.request("forward", 10, None)
    lock.request("reverse", 11, None)
    lock.mark_stop_sent(11.1, 8)
    assert lock.request("reverse", 11.8, sample(9, 0, age=0.5)) == "hold"
    assert lock.request("reverse", 14.2, sample(10, 0)) == "timeout"
    assert lock.pending_mode == "reverse"


def test_external_stop_does_not_bypass_next_reverse_handoff():
    lock = DriveDirectionInterlock()
    assert lock.request("forward", 0, None) == "allow"
    assert lock.request("reverse", 0.1, None) == "stop"
    lock.abort_pending()
    assert lock.active_mode == "forward"
    assert lock.request("reverse", 0.2, None) == "stop"


def bare_motor():
    motor = MotorController.__new__(MotorController)
    motor._command_lock = threading.RLock()
    motor._state_lock = threading.RLock()
    motor._serial = object()
    motor._ensure_connected_locked = lambda: None
    motor._direction_interlock = DriveDirectionInterlock()
    motor._direction_fault_latched = False
    motor.max_wheel_mps = 0.212
    motor._encoder_state = {
        "sequence": 0,
        "updated_monotonic": None,
        "left_front_ticks": 0,
        "right_front_ticks": 0,
        "left_rear_ticks": 0,
        "right_rear_ticks": 0,
    }
    motor.current_motion = "stop"
    motor.last_command_at = 0
    motor._jerk_limiter = Mock()
    motor._jerk_limiter.step.side_effect = lambda a, b, _: (a, b)
    commands = []
    motor._exchange_locked = commands.append
    return motor, commands


def test_pi_motor_emits_stop_once_before_direction_change():
    motor, commands = bare_motor()
    motor.drive(0.1, 0.1)
    assert commands[-1].startswith("DRIVE,")
    motor.drive(-0.1, -0.1)
    assert commands[-1] == "STOP,direction_switch"
    count = len(commands)
    motor.drive(-0.1, -0.1)
    assert len(commands) == count  # do not drive without fresh encoders
    motor.stop("zero_drive")
    assert commands[-1] == "STOP,zero_drive"
    motor.drive(-0.1, -0.1)
    assert commands[-1] == "STOP,direction_switch"


def test_segment_timeout_latches_pi_motion_fault():
    motor, commands = bare_motor()
    motor.drive(0.1, 0.1)
    motor.drive(-0.1, -0.1)
    motor._direction_interlock._stop_sent_at = time.monotonic() - 4.0
    with pytest.raises(MotorControllerError, match="not encoder-confirmed"):
        motor.drive(-0.1, -0.1)
    assert commands[-1] == "STOP,direction_switch_timeout"
    with pytest.raises(MotorControllerError, match="motion locked"):
        motor.drive(0.1, 0.1)
    motor.stop("auto_drive_error")
    with pytest.raises(MotorControllerError, match="motion locked"):
        motor.drive(0.1, 0.1)
    motor.clear_direction_fault_after_safety_resume()
    motor.drive(0.1, 0.1)
    assert commands[-1].startswith("DRIVE,")


def test_pi_motor_trace_distinguishes_uart_drive_from_interlock_hold(capsys):
    motor, commands = bare_motor()
    motor.drive(0.1, 0.1)
    assert "[motor-motion] state=uart_drive" in capsys.readouterr().out

    motor.drive(-0.1, -0.1)
    assert "[motor-motion] state=interlock_stop" in capsys.readouterr().out
    assert commands[-1] == "STOP,direction_switch"

    count = len(commands)
    motor.drive(-0.1, -0.1)
    assert "[motor-motion] state=interlock_hold" in capsys.readouterr().out
    assert len(commands) == count  # Pi returned normally, but no UART DRIVE

    motor.stop("nav2_zero_twist")
    assert "[motor-motion] state=uart_stop" in capsys.readouterr().out


def test_pi_motor_trace_only_reports_successful_uart_output(capsys):
    motor, _ = bare_motor()

    def failed_exchange(_command):
        raise OSError("simulated UART failure")

    motor._exchange_locked = failed_exchange
    with pytest.raises(OSError, match="simulated UART failure"):
        motor.drive(0.1, 0.1)
    assert "state=uart_drive" not in capsys.readouterr().out


def test_pi_motor_trace_repeats_are_rate_limited(capsys):
    motor, _ = bare_motor()
    motor._trace_motor_output("interlock_hold", detail="pivot_left")
    motor._trace_motor_output("interlock_hold", detail="pivot_left")
    assert capsys.readouterr().out.count("state=interlock_hold") == 1
