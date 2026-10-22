"""ROS-independent AMCL convergence checks following /initialpose publication."""
from __future__ import annotations
import math
from typing import Any


def stable_amcl_observations(samples: list[dict[str, Any]], initial: dict[str, float], *,
                             minimum_samples: int = 3, minimum_span: float = 0.25) -> bool:
    if len(samples) < minimum_samples:
        return False
    recent = samples[-minimum_samples:]
    try:
        if float(recent[-1]["at"]) - float(recent[0]["at"]) < minimum_span:
            return False
        cx, cy, yaw = (float(initial[k]) for k in ("x", "y", "yaw"))
        base_x, base_y, base_yaw = (float(recent[0][k]) for k in ("x", "y", "yaw"))
        if not all(map(math.isfinite, (cx, cy, yaw, base_x, base_y, base_yaw))):
            return False
        for pose in recent:
            x, y, angle = (float(pose[k]) for k in ("x", "y", "yaw"))
            xyvar, yawvar = (float(pose[k]) for k in ("xy_variance", "yaw_variance"))
            if not all(map(math.isfinite, (x, y, angle, xyvar, yawvar))):
                return False
            if not (0 <= xyvar <= 0.25 and 0 <= yawvar <= math.radians(25) ** 2):
                return False
            if math.hypot(x - cx, y - cy) > 0.45 or math.hypot(x - base_x, y - base_y) > 0.20:
                return False
            delta = math.atan2(math.sin(angle - yaw), math.cos(angle - yaw))
            spread = math.atan2(math.sin(angle - base_yaw), math.cos(angle - base_yaw))
            if abs(delta) > math.radians(25) or abs(spread) > math.radians(12):
                return False
        return True
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
