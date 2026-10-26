"""Conservative LaserScan gate for open-loop motor replay.

Return None for any missing/stale/insufficient sensor evidence. This is an
additional gate, not a replacement for Nav2 costmaps or emergency STOP.
"""
from __future__ import annotations
import math
import time
from typing import Any


def path_blocked(scan: dict[str, Any] | None, direction: str,
                 *, now: float | None = None,
                 stop_m: float = 0.60) -> bool | None:
    if not isinstance(scan, dict) or not isinstance(direction, str):
        return None
    now = time.time() if now is None else float(now)
    try:
        received = float(scan["received_at"])
        amin = float(scan["angle_min"])
        increment = float(scan["angle_increment"])
        minimum = float(scan["range_min"])
        maximum = float(scan["range_max"])
        ranges = scan["ranges"]
    except (TypeError, ValueError, KeyError, OverflowError):
        return None
    if (not all(math.isfinite(x) for x in (received, amin, increment, minimum, maximum, now))
        or increment == 0 or minimum < 0 or maximum <= minimum
        or not 0 <= now - received <= 0.35 or not isinstance(ranges, list)):
        return None
    # 360-degree LiDAR may have a rear blind zone from chassis occlusion.
    target = math.pi if direction.startswith("backward") else 0.0
    half_angle = math.radians(36.0)
    values = []
    for i, raw in enumerate(ranges):
        if type(raw) not in (int, float) or not math.isfinite(raw):
            continue
        if not minimum <= raw <= maximum:
            continue
        theta = amin + i * increment - target
        theta = math.atan2(math.sin(theta), math.cos(theta))
        if abs(theta) <= half_angle:
            values.append(float(raw))
    if len(values) < 8:
        return None
    # Low quantile rejects isolated noisy near returns but responds to
    # spatially supported obstacles spanning several rays.
    values.sort()
    observation = values[max(0, int(len(values) * 0.10) - 1)]
    if not math.isfinite(observation):
        return None
    return observation < stop_m
