"""Exact DABOM Pico MOVE direction proportions expressed as wheel targets.

Pico firmware uses CURVE_INNER_RATIO=0.35. The normal Pi motor driver still
enforces the 30% PWM ceiling, acceleration profile, and direction interlock.
"""
from __future__ import annotations

import math

from server.recorded_loop_core import RecordedLoopError, motor_command

INNER_RATIO = 0.35
DIRECTION_FACTORS = {
    "forward": (1.0, 1.0),
    "backward": (-1.0, -1.0),
    "left": (0.0, 1.0),
    "right": (1.0, 0.0),
    "forward_left": (INNER_RATIO, 1.0),
    "forward_right": (1.0, INNER_RATIO),
    "backward_left": (-1.0, -INNER_RATIO),
    "backward_right": (-INNER_RATIO, -1.0),
    "rotate_left": (-1.0, 1.0),
    "rotate_right": (1.0, -1.0),
}


def recorded_to_auto_drive(command: dict, max_wheel_mps: float) -> dict:
    """Converts recorded PWM into the existing Pi auto-drive command contract."""
    normalized = motor_command(command)
    if not isinstance(max_wheel_mps, (int, float)) or not math.isfinite(max_wheel_mps) or max_wheel_mps <= 0:
        raise RecordedLoopError("INVALID_CALIBRATION", "MAX_WHEEL_MPS must be finite and positive")
    left_factor, right_factor = DIRECTION_FACTORS[normalized["direction"]]
    # PWM is already capped at 0.30 by motor_command. Conversion is linear
    # only to the extent the calibrated PI motor path follows that mapping.
    return {
        "type": "auto_drive",
        "source": "recorded_loop",
        "left_mps": left_factor * normalized["speed"] * max_wheel_mps,
        "right_mps": right_factor * normalized["speed"] * max_wheel_mps,
        "rear_blind_limited": left_factor < 0 and right_factor < 0,
    }
