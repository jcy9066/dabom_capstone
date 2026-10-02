from __future__ import annotations

import asyncio
import math
import time
from typing import Any, Awaitable, Callable


class BatterySafetyMonitor:
    """Debounced low-battery emergency-stop policy.

    Unknown battery telemetry never triggers a stop. A stop is triggered only
    after consecutive valid samples at or below the configured threshold.
    """

    def __init__(
        self,
        emergency_stop: Callable[[str, bool], Awaitable[dict[str, Any]]],
        *,
        low_percent: float = 15.0,
        clear_percent: float = 20.0,
        low_samples: int = 3,
    ) -> None:
        self._emergency_stop = emergency_stop
        self.low_percent = float(low_percent)
        self.clear_percent = max(float(clear_percent), self.low_percent)
        self.low_samples = max(1, int(low_samples))
        self._hits = 0
        self._latched = False
        self._pending: asyncio.Task | None = None
        self._last_percent: float | None = None
        self._updated_at: float | None = None

    @staticmethod
    def _percent(payload: dict[str, Any]) -> float | None:
        value = payload.get("battery_percent", payload.get("battery_level", payload.get("battery")))
        try:
            percent = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(percent) or percent < 0 or percent > 100:
            return None
        return percent

    def snapshot(self) -> dict[str, Any]:
        return {
            "battery_percent": self._last_percent,
            "battery_low": self._latched,
            "battery_low_samples": self._hits,
            "battery_updated_at": self._updated_at,
            "low_percent": self.low_percent,
            "clear_percent": self.clear_percent,
        }

    def note_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        percent = self._percent(payload)
        self._last_percent = percent
        self._updated_at = time.time()

        if percent is None:
            self._hits = 0
            return self.snapshot()

        if percent >= self.clear_percent:
            self._hits = 0
            self._latched = False
            return self.snapshot()

        if percent <= self.low_percent:
            self._hits += 1
        else:
            self._hits = 0

        if self._hits >= self.low_samples and not self._latched:
            self._latched = True
            if self._pending is None or self._pending.done():
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    self._pending = loop.create_task(self._trigger_stop())

        return self.snapshot()

    async def _trigger_stop(self) -> None:
        try:
            await self._emergency_stop("LOW_BATTERY", True)
        except Exception:
            # Keep the latch set: a low battery must not be cleared by a
            # transient delivery failure. The next status read still exposes it.
            return
