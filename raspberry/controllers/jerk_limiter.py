"""Jerk-limited velocity targets for the Pi auto-drive UART output.

Input and output use normalized wheel PWM, with acceleration and jerk
expressed in normalized PWM units per second^2 and second^3.
Physical STOP commands bypass this filter and must reset it.
"""
from __future__ import annotations

import math
from typing import Tuple


class WheelJerkLimiter:
    def __init__(
        self,
        max_accel: float,
        max_jerk: float,
        *,
        nominal_dt: float = 0.1,
        max_dt: float = 0.2,
    ) -> None:
        if not all(math.isfinite(v) and v > 0 for v in (
            max_accel, max_jerk, nominal_dt, max_dt,
        )):
            raise ValueError("acceleration, jerk and timestep must be positive")
        self.max_accel = max_accel
        self.max_jerk = max_jerk
        self.nominal_dt = nominal_dt
        self.max_dt = max_dt
        self._velocity: Tuple[float, float] = (0.0, 0.0)
        self._acceleration: Tuple[float, float] = (0.0, 0.0)
        self._last_at: float | None = None

    def reset(self) -> None:
        self._velocity = (0.0, 0.0)
        self._acceleration = (0.0, 0.0)
        self._last_at = None

    def step(self, left: float, right: float, now: float) -> Tuple[float, float]:
        if not all(math.isfinite(v) for v in (left, right, now)):
            raise ValueError("non-finite wheel command or timestamp")
        dt = self.nominal_dt if self._last_at is None else max(
            0.001, min(now - self._last_at, self.max_dt),
        )
        targets = (max(-1.0, min(1.0, left)), max(-1.0, min(1.0, right)))
        next_velocity: list[float] = []
        next_acceleration: list[float] = []

        for previous_v, previous_a, target in zip(
            self._velocity, self._acceleration, targets,
        ):
            error = target - previous_v
            # Brake BEFORE reaching a small target: abruptly setting
            # acceleration to zero on a velocity clamp violates jerk.
            # The braking window includes a discrete-step margin because
            # our command is held for the entire control interval.
            braking_window = (
                previous_a * previous_a / (2.0 * self.max_jerk)
                + abs(previous_a) * dt
                + 0.5 * self.max_jerk * dt * dt
            )
            if previous_a * error > 0.0 and abs(error) <= braking_window:
                desired_a = 0.0
            else:
                desired_a = max(
                    -self.max_accel,
                    min(self.max_accel, error / dt),
                )
            limited_a = max(
                previous_a - self.max_jerk * dt,
                min(previous_a + self.max_jerk * dt, desired_a),
            )
            limited_a = max(-self.max_accel, min(self.max_accel, limited_a))
            velocity = previous_v + limited_a * dt

            # Do NOT snap to target while acceleration is nonzero.
            # That implicit acceleration impulse would exceed max_jerk.
            # Small smooth corrections may overshoot the target briefly.
            # Do not erase acceleration on a zero crossing. The resulting
            # velocity would have a discontinuous acceleration impulse
            # larger than max_jerk. Snap numerical roundoff to exactly zero
            # only when the acceleration needed to hit zero is reachable
            # from the previous acceleration under this timestep's limit.
            # For other crossings, retain the smooth, jerk-limited velocity;
            # MotorController's encoder-confirmed direction interlock owns
            # the physical STOP between forward and reverse motor commands.
            if previous_v * target < 0 and previous_v * velocity <= 0:
                if abs(velocity) <= 1e-10:
                    zero_accel = -previous_v / dt
                    if abs(zero_accel - previous_a) <= self.max_jerk * dt + 1e-9:
                        velocity = 0.0
                        limited_a = zero_accel
            next_velocity.append(max(-1.0, min(1.0, velocity)))
            next_acceleration.append(limited_a)

        self._velocity = (next_velocity[0], next_velocity[1])
        self._acceleration = (next_acceleration[0], next_acceleration[1])
        self._last_at = now
        return self._velocity
