"""Persistence workers for robot status and automatic patrol events."""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from server.database import Database
from server.media_service import PrivateImageStore


EVENT_SOURCES = frozenset({"VISION_AI", "SYSTEM_MONITOR"})
EVENT_TYPES = frozenset(
    {"INTRUSION", "ASSAULT", "SYSTEM_ERROR", "NETWORK_LOSS", "SENSOR_ANOMALY"}
)
ACTION_TYPES = frozenset(
    {"WARNING", "MANUAL_MOVING", "REPORT", "COMMUNICATION", "NOTE"}
)


def _finite_number(
    value: Any,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    integer: bool = False,
) -> float | int | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if minimum is not None and number < minimum:
        return None
    if maximum is not None and number > maximum:
        return None
    return int(round(number)) if integer else number


def normalize_system_status(status: Mapping[str, Any]) -> dict[str, Any]:
    mode = str(status.get("mode") or "").strip().lower()
    is_autonomous = 0 if mode in {"manual", "remote"} else 1
    return {
        "cpu_usage": _finite_number(status.get("cpu_usage"), minimum=0, maximum=100),
        "cpu_temperature": _finite_number(
            status.get("cpu_temperature", status.get("cpu_temp")),
            minimum=-100,
            maximum=300,
        ),
        "ram_usage": _finite_number(status.get("ram_usage"), minimum=0, maximum=100),
        "ping": _finite_number(status.get("ping"), minimum=0, maximum=32767, integer=True),
        "is_autonomous": is_autonomous,
        "speed": _finite_number(status.get("speed"), minimum=-128, maximum=127, integer=True),
        "gps_lat": _finite_number(status.get("gps_lat"), minimum=-90, maximum=90),
        "gps_lng": _finite_number(status.get("gps_lng"), minimum=-180, maximum=180),
        "gps_alt": _finite_number(status.get("gps_alt")),
        "lidar_x": _finite_number(status.get("lidar_x")),
        "lidar_y": _finite_number(status.get("lidar_y")),
    }


class CooldownGate:
    def __init__(self, cooldown_sec: float, clock: Callable[[], float] = time.monotonic):
        self.cooldown_sec = max(0.0, float(cooldown_sec))
        self.clock = clock
        self._last_allowed: dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def allow(self, key: tuple[str, ...]) -> bool:
        now = self.clock()
        with self._lock:
            previous = self._last_allowed.get(key)
            if previous is not None and now - previous < self.cooldown_sec:
                return False
            self._last_allowed[key] = now
            return True

    def release(self, key: tuple[str, ...]) -> None:
        with self._lock:
            self._last_allowed.pop(key, None)


@dataclass(frozen=True)
class PendingEvent:
    robot_id: str
    event_source: str
    event_type: str
    confidence: float | None
    message: str
    location: Mapping[str, Any]
    frame: np.ndarray | None


class EventLogWorker:
    """Serializes event DB writes without blocking the inference loop."""

    def __init__(
        self,
        database: Database,
        notifier: Any,
        *,
        cooldown_sec: float = 10,
        queue_size: int = 100,
        image_store: PrivateImageStore | None = None,
    ) -> None:
        self.database = database
        self.notifier = notifier
        self.gate = CooldownGate(cooldown_sec)
        self.image_store = image_store
        self.queue: queue.Queue[PendingEvent | None] = queue.Queue(maxsize=queue_size)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.logger = logging.getLogger(__name__)

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._run,
            name="event-log-writer",
            daemon=True,
        )
        self.thread.start()

    def submit(
        self,
        *,
        robot_id: str,
        event_source: str,
        event_type: str,
        confidence: float | None = None,
        message: str | None = None,
        location: Mapping[str, Any] | None = None,
        frame: np.ndarray | None = None,
        cooldown_key: str | None = None,
    ) -> bool:
        if event_source not in EVENT_SOURCES or event_type not in EVENT_TYPES:
            raise ValueError("Unsupported event taxonomy.")
        if self.stop_event.is_set():
            return False
        frozen_frame = self._freeze_frame(frame) if event_source == "VISION_AI" else None
        if frozen_frame is not None and self.image_store is None:
            raise ValueError("An image store is required when an event includes a frame.")
        key = (
            (str(robot_id), event_type)
            if cooldown_key is None
            else (str(robot_id), event_type, str(cooldown_key))
        )
        if not self.gate.allow(key):
            return False
        item = PendingEvent(
            robot_id=str(robot_id),
            event_source=event_source,
            event_type=event_type,
            confidence=_finite_number(confidence, minimum=0, maximum=1),
            message=message or f"{event_type} detected by {robot_id}",
            location=dict(location or {}),
            frame=frozen_frame,
        )
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            self.gate.release(key)
            self.logger.warning("Event log queue is full; event was not persisted.")
            return False
        return True

    def stop(self, timeout_sec: float = 10) -> None:
        self.stop_event.set()
        try:
            self.queue.put(None, timeout=max(0.1, timeout_sec))
        except queue.Full:
            self.logger.warning("Event log writer queue did not accept its stop signal.")
        if self.thread is not None:
            self.thread.join(timeout=max(0.0, timeout_sec))
            if self.thread.is_alive():
                self.logger.warning("Event log writer did not stop before timeout.")
            else:
                self.thread = None

    def _run(self) -> None:
        while True:
            try:
                item = self.queue.get(timeout=0.5)
            except queue.Empty:
                if self.stop_event.is_set():
                    break
                continue
            if item is None:
                self.queue.task_done()
                break
            try:
                image_path: str | None = None
                try:
                    if item.frame is not None:
                        image_path = self.image_store.save_image("events", item.frame)
                    self.database.insert_event(
                        event_source=item.event_source,
                        event_type=item.event_type,
                        image_path=image_path,
                        confidence=item.confidence,
                        gps_lat=_finite_number(
                            item.location.get("gps_lat"), minimum=-90, maximum=90
                        ),
                        gps_lng=_finite_number(
                            item.location.get("gps_lng"), minimum=-180, maximum=180
                        ),
                        gps_alt=_finite_number(item.location.get("gps_alt")),
                        lidar_x=_finite_number(item.location.get("lidar_x")),
                        lidar_y=_finite_number(item.location.get("lidar_y")),
                    )
                except Exception as exc:
                    if image_path is not None:
                        try:
                            self.image_store.delete(image_path)
                        except Exception as cleanup_exc:
                            self.logger.warning(
                                "Automatic event image cleanup failed: %s", cleanup_exc
                            )
                    self.logger.warning("Automatic event persistence failed: %s", exc)
                try:
                    self.notifier.send_event_alert_async(
                        item.message,
                        robot_id=item.robot_id,
                        event_type=item.event_type,
                    )
                except Exception as exc:
                    self.logger.warning("Automatic Telegram alert failed: %s", exc)
            finally:
                self.queue.task_done()

    @staticmethod
    def _freeze_frame(frame: np.ndarray | None) -> np.ndarray | None:
        if frame is None:
            return None
        if not isinstance(frame, np.ndarray) or frame.ndim not in (2, 3) or frame.size == 0:
            raise ValueError("A non-empty image frame is required.")
        frozen = frame.copy()
        frozen.setflags(write=False)
        return frozen


class SystemStatusWriter:
    def __init__(
        self,
        database: Database,
        snapshot: Callable[[], Mapping[str, Any]],
        *,
        interval_sec: float = 5,
    ) -> None:
        self.database = database
        self.snapshot = snapshot
        self.interval_sec = max(0.1, float(interval_sec))
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.logger = logging.getLogger(__name__)

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._run,
            name="system-status-writer",
            daemon=True,
        )
        self.thread.start()

    def stop(self, timeout_sec: float = 10) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=max(0.0, timeout_sec))
            if self.thread.is_alive():
                self.logger.warning("System status writer did not stop before timeout.")
            else:
                self.thread = None

    def write_once(self) -> bool:
        status = dict(self.snapshot())
        if status.get("updated_at") is None:
            return False
        self.database.insert_system_status(normalize_system_status(status))
        return True

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval_sec):
            try:
                self.write_once()
            except Exception as exc:
                self.logger.warning("Periodic system status persistence failed: %s", exc)
