from __future__ import annotations

import math
import threading
from typing import Any


class NavigationTrajectoryTracker:
    """Keep the dashboard's actual robot trajectory in map coordinates."""

    def __init__(
        self,
        *,
        min_distance_m: float = 0.05,
        segment_break_distance_m: float = 1.0,
        max_points: int = 2000,
    ) -> None:
        if min_distance_m <= 0:
            raise ValueError("min_distance_m must be positive")
        if segment_break_distance_m <= min_distance_m:
            raise ValueError(
                "segment_break_distance_m must be greater than min_distance_m"
            )
        if max_points < 2:
            raise ValueError("max_points must be at least 2")

        self.min_distance_m = float(min_distance_m)
        self.segment_break_distance_m = float(segment_break_distance_m)
        self.max_points = int(max_points)

        self._lock = threading.RLock()
        self._mode: str | None = None
        self._map_revision: str | None = None
        self._segments: list[list[dict[str, float]]] = []
        self._point_count = 0

    @staticmethod
    def normalize_mode(mode: Any) -> str | None:
        value = str(mode or "").strip().lower()
        if value == "mapping":
            return "MAPPING"
        if value in {"localization", "localization_nav2", "driving"}:
            return "DRIVING"
        if value in {"mapping".upper(), "driving".upper()}:
            return value.upper()
        return None

    def reset(
        self,
        mode: Any = None,
        *,
        map_revision: str | None = None,
    ) -> None:
        normalized = self.normalize_mode(mode)
        with self._lock:
            self._mode = normalized
            self._map_revision = map_revision
            self._segments = []
            self._point_count = 0

    def note_mode(self, mode: Any) -> str | None:
        normalized = self.normalize_mode(mode)
        if normalized is None:
            return None
        with self._lock:
            if self._mode != normalized:
                self._mode = normalized
                self._map_revision = None
                self._segments = []
                self._point_count = 0
            return self._mode

    def note_map(self, mode: Any, map_revision: str | None) -> None:
        normalized = self.normalize_mode(mode)
        if normalized is None:
            return

        with self._lock:
            if self._mode != normalized:
                self._mode = normalized
                self._segments = []
                self._point_count = 0
                self._map_revision = None

            if (
                normalized == "DRIVING"
                and self._map_revision is not None
                and map_revision is not None
                and self._map_revision != map_revision
            ):
                self._segments = []
                self._point_count = 0

            self._map_revision = map_revision

    def note_pose(self, mode: Any, payload: dict[str, Any]) -> bool:
        normalized = self.normalize_mode(mode)
        if normalized is None or not isinstance(payload, dict):
            return False

        try:
            x = float(payload["x"])
            y = float(payload["y"])
        except (KeyError, TypeError, ValueError):
            return False

        if not math.isfinite(x) or not math.isfinite(y):
            return False

        point = {"x": x, "y": y}

        with self._lock:
            if self._mode != normalized:
                self._mode = normalized
                self._map_revision = None
                self._segments = []
                self._point_count = 0

            if not self._segments:
                self._segments.append([point])
                self._point_count = 1
                return True

            last_segment = self._segments[-1]
            last_point = last_segment[-1]
            distance = math.hypot(
                x - float(last_point["x"]),
                y - float(last_point["y"]),
            )

            if distance < self.min_distance_m:
                return False

            if distance >= self.segment_break_distance_m:
                self._segments.append([point])
            else:
                last_segment.append(point)
            self._point_count += 1
            self._trim_locked()
            return True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "mode": self._mode,
                "point_count": self._point_count,
                "segments": [
                    [dict(point) for point in segment]
                    for segment in self._segments
                ],
            }

    def _trim_locked(self) -> None:
        while self._point_count > self.max_points and self._segments:
            first = self._segments[0]
            if len(first) <= 1:
                self._point_count -= len(first)
                self._segments.pop(0)
                continue

            first.pop(0)
            self._point_count -= 1
