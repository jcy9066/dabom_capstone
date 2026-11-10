"""Pure LiDAR restart policy, importable without ROS2 or motor hardware."""
from __future__ import annotations

import math


def restart_backoff(failure_count: int, *, base_delay: float = 1.0,
                    max_delay: float = 10.0) -> float:
    if not math.isfinite(base_delay) or not math.isfinite(max_delay) or max_delay <= 0:
        raise ValueError("Invalid retry delay")
    base = max(0.1, base_delay)
    cap = max(base, max_delay)
    return min(cap, base * 2 ** min(12, max(0, int(failure_count) - 1)))
