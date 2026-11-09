"""Asynchronous, opt-in LED + Pico beep alerts for fresh AI danger decisions.

This module does not control motors or audio devices.  It schedules the
existing authenticated Pi ``warning`` command through RobotConnectionManager.
Inference-worker threads never wait for Pi I/O.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

LOG = logging.getLogger(__name__)


def _enabled(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    lowered = value.strip().lower()
    if lowered in {"true", "1", "yes", "on"}:
        return True
    if lowered in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"Invalid automatic-warning boolean: {value!r}")


def _positive_float(value: str, name: str, minimum: float) -> float:
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return number


def verified_danger_label(result: Mapping[str, Any] | Any) -> str | None:
    """Accept fresh confirmed model danger, not merely stale display flags.

    ``result`` comes from a *published* inference, not a polled dashboard or
    an inference run discarded after a pipeline/stream-generation change.
    """
    if not isinstance(result, Mapping) or result.get("danger") is not True:
        return None
    if result.get("model_error"):
        return None

    scene = result.get("scene")
    if (
        isinstance(scene, Mapping)
        and scene.get("status") == "ok"
        and scene.get("fresh") is True
        and scene.get("raw_danger") is True
    ):
        return "VIOLENCE"

    detections = result.get("detections")
    if isinstance(detections, list):
        for detection in detections:
            if not isinstance(detection, Mapping):
                continue
            if (
                detection.get("danger") is True
                and detection.get("confidence_level") == "danger"
                and detection.get("observation_stale") is not True
                and detection.get("inference_status") in (None, "ok")
            ):
                label = str(detection.get("label") or "").strip().upper()
                if label:
                    return label[:48]

    pair_actions = result.get("pair_actions")
    if isinstance(pair_actions, list):
        for action in pair_actions:
            if not isinstance(action, Mapping):
                continue
            if (
                (action.get("is_danger") is True or action.get("danger") is True)
                and action.get("observation_stale") is not True
            ):
                label = str(action.get("label") or "").strip().upper()
                if label:
                    return label[:48]
    return None


class AutomaticWarningDispatcher:
    """Debounced, cooldown-limited, nonblocking bridge to Pi warning pattern."""

    def __init__(
        self,
        *,
        robot_id: str,
        send_command: Callable[[str, dict[str, Any]], Awaitable[bool | dict[str, Any]]],
        enabled: bool = False,
        dry_run: bool = True,
        min_hits: int = 2,
        max_hit_gap_sec: float = 3.0,
        cooldown_sec: float = 30.0,
        max_capture_age_sec: float = 3.0,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(min_hits, int) or min_hits < 1:
            raise ValueError("min_hits must be a positive integer")
        if max_hit_gap_sec <= 0 or cooldown_sec < 10 or max_capture_age_sec <= 0:
            raise ValueError("invalid warning timing parameters")
        self.robot_id = robot_id
        self.send_command = send_command
        self.enabled = bool(enabled)
        self.dry_run = bool(dry_run)
        self.min_hits = min_hits
        self.max_hit_gap_sec = float(max_hit_gap_sec)
        self.cooldown_sec = float(cooldown_sec)
        self.max_capture_age_sec = float(max_capture_age_sec)
        self.clock = clock
        self.wall_clock = wall_clock
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._hits = 0
        self._last_hit: float | None = None
        self._last_sent: float | None = None
        self._generation = None
        self._stream_id = None
        self._pending = False
        self._closed = False
        self._tasks: set[asyncio.Task] = set()

    @classmethod
    def from_env(cls, *, robot_id, send_command):
        raw = os.environ
        return cls(
            robot_id=robot_id,
            send_command=send_command,
            enabled=_enabled(raw.get("AUTO_WARNING_ENABLED"), False),
            dry_run=_enabled(raw.get("AUTO_WARNING_DRY_RUN"), True),
            min_hits=int(raw.get("AUTO_WARNING_MIN_HITS", "2")),
            max_hit_gap_sec=_positive_float(raw.get("AUTO_WARNING_MAX_HIT_GAP_SEC", "3"), "AUTO_WARNING_MAX_HIT_GAP_SEC", 0.1),
            cooldown_sec=_positive_float(raw.get("AUTO_WARNING_COOLDOWN_SEC", "30"), "AUTO_WARNING_COOLDOWN_SEC", 10),
            max_capture_age_sec=_positive_float(raw.get("AUTO_WARNING_MAX_CAPTURE_AGE_SEC", "3"), "AUTO_WARNING_MAX_CAPTURE_AGE_SEC", 0.1),
        )

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._loop = loop
            self._closed = False
        LOG.info(
            "Automatic AI warning: enabled=%s dry_run=%s hits=%d cooldown=%.1fs",
            self.enabled, self.dry_run, self.min_hits, self.cooldown_sec,
        )

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._loop = None
            self._hits = 0
            self._pending = False
        for task in list(self._tasks):
            task.cancel()

    def observe_result(
        self,
        *,
        robot_id: str | None,
        result: Mapping[str, Any],
        captured_at: float | None,
        generation: Any = None,
        stream_id: Any = None,
    ) -> bool:
        """Called only after the current inference result was published.

        Return True when a dispatch was scheduled (including dry run).
        Failed/offline sends still respect cooldown to avoid alert storms.
        """
        if not self.enabled or robot_id != self.robot_id:
            return False

        now = self.clock()
        wall_now = self.wall_clock()
        fresh = False
        try:
            if captured_at is not None and not isinstance(captured_at, bool):
                age = wall_now - float(captured_at)
                fresh = math.isfinite(age) and -0.5 <= age <= self.max_capture_age_sec
        except (TypeError, ValueError, OverflowError):
            pass
        label = verified_danger_label(result) if fresh else None
        scene = result.get("scene") if isinstance(result, Mapping) else None
        # X3D runs at ~1 Hz while the dashboard/inference worker can publish
        # more frequently.  Interim `fresh=False` values are the *same* scene
        # decision, not new NORMAL observations. They must not reset consecutive
        # fresh danger hits, nor be counted as additional hits.
        cached_scene = (
            fresh and label is None and not result.get("model_error")
            and isinstance(scene, Mapping)
            and scene.get("status") == "ok"
            and scene.get("available") is True
            and scene.get("fresh") is False
        )

        with self._lock:
            if self._closed or self._loop is None:
                return False
            stream_changed = (self._stream_id != stream_id or self._generation != generation)
            if stream_changed:
                self._stream_id = stream_id
                self._generation = generation
                self._hits = 0
                self._last_hit = None
            if cached_scene:
                # Preserve a candidate only until the configured hit gap.
                # Safety failures/expired camera frames still reset below.
                if (self._last_hit is not None
                        and now - self._last_hit > self.max_hit_gap_sec):
                    self._hits = 0
                    self._last_hit = None
                return False
            if label is None:
                self._hits = 0
                self._last_hit = None
                return False
            if self._last_hit is None or now - self._last_hit > self.max_hit_gap_sec:
                self._hits = 0
            self._last_hit = now
            self._hits += 1
            if self._hits < self.min_hits or self._pending:
                return False
            if self._last_sent is not None and now - self._last_sent < self.cooldown_sec:
                return False
            self._pending = True
            try:
                self._loop.call_soon_threadsafe(self._schedule, label)
            except RuntimeError:
                self._pending = False
                return False
            self._last_sent = now
        return True

    def _schedule(self, label: str) -> None:
        if self._closed:
            return
        task = asyncio.create_task(self._dispatch(label), name="dabom-auto-led-beep")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _dispatch(self, label: str) -> None:
        try:
            if self._closed:
                return
            if self.dry_run:
                LOG.warning("[auto-warning][DRY-RUN] robot=%s action=%s: would trigger 10s LED + Pico beep", self.robot_id, label)
                return
            command = {"type": "warning", "led_duration_ms": 10000}
            reply = await self.send_command(self.robot_id, command)
            acknowledged = reply is True or (isinstance(reply, dict) and reply.get("ok") is True and reply.get("delivered", True) is True)
            if acknowledged:
                LOG.warning("[auto-warning] Pi ACK: robot=%s action=%s LED+Pico beep 10s", self.robot_id, label)
            else:
                LOG.error("[auto-warning] Pi did not ACK warning: robot=%s action=%s", self.robot_id, label)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("[auto-warning] warning command failed: robot=%s", self.robot_id)
        finally:
            with self._lock:
                self._pending = False
