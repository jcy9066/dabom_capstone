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
    half_angle = (math.pi if direction.startswith("rotate_")
                  else math.radians(36.0))
    values = []
    dangerously_close = 0
    for i, raw in enumerate(ranges):
        if type(raw) not in (int, float) or not math.isfinite(raw):
            continue
        theta = amin + i * increment - target
        theta = math.atan2(math.sin(theta), math.cos(theta))
        if abs(theta) > half_angle:
            continue
        # Positive returns inside LaserScan.range_min indicate a nearby
        # object OR an invalid/occluded reading. Treat repeated ones as
        # blocked, never as evidence that the sector is clear.
        if 0 < raw < minimum:
            dangerously_close += 1
            continue
        if not minimum <= raw <= maximum:
            continue
        values.append(float(raw))
    if dangerously_close >= 2:
        return True
    if len(values) < 8:
        return None
    # A tenth-percentile rule can miss a narrow physical obstacle:
    # two or three adjacent LiDAR rays at 0.3 m must stop the robot,
    # even when the other 350 rays show free space. Prefer a safe pause
    # over disregarding a small person/object during replay.
    near = sum(distance < stop_m for distance in values)
    return near >= 2
