"""Physical STOP/encoder interlock for automatic drive segment changes.

One interlock instance belongs to one Pi MotorController. It is not a
replacement for the safety-epoch, E-STOP, command timeout or UART watchdog.
All decisions are pure and must be called under the motor command lock.
"""
from __future__ import annotations

import math
from typing import Any


ENCODER_KEYS = (
    "left_front_ticks", "right_front_ticks",
    "left_rear_ticks", "right_rear_ticks",
)


def segment_mode(left: float, right: float) -> str:
    """Group differential wheel commands into forward, reverse or pivot."""
    if not math.isfinite(left) or not math.isfinite(right):
        raise ValueError("non-finite wheel command")
    if max(abs(left), abs(right)) < 1e-6:
        return "stop"
    # Opposing wheel signs denote a pivot command, not translation.
    if left * right < -1e-10:
        return "pivot_left" if right > left else "pivot_right"
    return "reverse" if left + right < 0 else "forward"


class DriveDirectionInterlock:
    """Never authorize a new segment before post-STOP stationary encoders.

    request returns allow, stop, hold, or timeout. The caller must actually
    transmit STOP on a stop decision before accepting further motion.
    mark_stop_sent is called only after UART acknowledgement.
    """
    STABLE_SEC = 0.5
    TIMEOUT_SEC = 3.0
    MAX_ENCODER_AGE_SEC = 0.3

    def __init__(self) -> None:
        self.active_mode: str | None = None
        self.pending_mode: str | None = None
        self._stop_sent_at: float | None = None
        self._baseline_sequence = 0
        self._last_sequence = 0
        self._last_ticks: tuple[int, int, int, int] | None = None
        self._stationary_since: float | None = None

    def reset(self) -> None:
        self.__init__()

    def abort_pending(self) -> None:
        """External STOP does not prove physical standstill.

        Discard a pending switch without erasing the last moving segment,
        so the next opposite command must still earn post-STOP confirmation.
        """
        self.pending_mode = None
        self._stop_sent_at = None
        self._last_ticks = None
        self._stationary_since = None

    def request(self, mode: str, now: float, snapshot: dict[str, Any] | None) -> str:
        if not math.isfinite(now):
            raise ValueError("non-finite timestamp")
        if mode == "stop":
            return "hold"
        if self.pending_mode is None:
            if self.active_mode is None or self.active_mode == mode:
                self.active_mode = mode
                return "allow"
            self.pending_mode = mode
            self._stop_sent_at = None
            self._last_ticks = None
            self._stationary_since = None
            return "stop"

        # Another requested direction during handoff must not cancel STOP
        # or restart the 3s deadline indefinitely.
        self.pending_mode = mode
        if self._stop_sent_at is None:
            return "stop"
        if now - self._stop_sent_at > self.TIMEOUT_SEC:
            return "timeout"
        if not snapshot or snapshot.get("age_sec") is None:
            return "hold"
        try:
            age = float(snapshot["age_sec"])
            seq = int(snapshot["sequence"])
            ticks = tuple(int(snapshot[k]) for k in ENCODER_KEYS)
        except (ValueError, TypeError, KeyError, OverflowError):
            return "hold"
        if (
            not math.isfinite(age)
            or age < 0 or age > self.MAX_ENCODER_AGE_SEC
            or seq <= self._baseline_sequence
            or seq <= self._last_sequence
        ):
            return "hold"
        self._last_sequence = seq
        if ticks != self._last_ticks:
            self._last_ticks = ticks
            self._stationary_since = now
            return "hold"
        if self._stationary_since is None:
            self._stationary_since = now
            return "hold"
        if now - self._stationary_since < self.STABLE_SEC:
            return "hold"
        self.active_mode = self.pending_mode
        self.pending_mode = None
        self._stop_sent_at = None
        self._stationary_since = None
        return "allow"

    def mark_stop_sent(self, now: float, baseline_sequence: int) -> None:
        """Called only after a successful STOP response from the Pico."""
        if self.pending_mode is None:
            raise RuntimeError("no pending handoff")
        self._stop_sent_at = now
        self._baseline_sequence = max(0, int(baseline_sequence))
        self._last_sequence = self._baseline_sequence
        self._last_ticks = None
        self._stationary_since = None
