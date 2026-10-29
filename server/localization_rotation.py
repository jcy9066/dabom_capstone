"""Fail-closed preflight for at most one bounded physical Initial Pose turn."""
from __future__ import annotations

import math
import time
from typing import Any


class UnsafeLocalizationTurn(ValueError):
    pass


def verify_localization_turn_clearance(
    scan: dict[str, Any] | None, *,
    now: float | None = None,
    body_radius_m: float = 0.175,
    safety_margin_m: float = 0.20,
) -> None:
    now = time.time() if now is None else float(now)
    if not isinstance(scan, dict):
        raise UnsafeLocalizationTurn("No LiDAR scan for safe turn")
    try:
        ranges = scan["ranges"]
        angle_increment = float(scan["angle_increment"])
        scan_at = float(scan["received_at"])
        low = float(scan["range_min"])
        high = float(scan["range_max"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise UnsafeLocalizationTurn("LiDAR scan geometry unavailable") from exc
    if (not isinstance(ranges, list) or len(ranges) < 180
        or len(ranges) > 20000 or not math.isfinite(angle_increment)
        or not 0 < angle_increment <= 0.1
        or not math.isfinite(scan_at)
        or not 0 <= now - scan_at <= 0.6
        or not 0 <= low < high
        or high < body_radius_m + safety_margin_m + 0.2
        or abs(len(ranges) * angle_increment - 2 * math.pi) > 0.12):
        raise UnsafeLocalizationTurn("Fresh complete 360-degree LiDAR required")
    clearance = body_radius_m + safety_margin_m
    for sample in ranges:
        try:
            distance = float(sample)
        except (TypeError, ValueError, OverflowError) as exc:
            raise UnsafeLocalizationTurn("LiDAR blind sector") from exc
        if math.isnan(distance) or distance == -math.inf or distance < 0:
            raise UnsafeLocalizationTurn("LiDAR blind sector")
        # +inf is a valid no-return only when scanner range_max exceeds clearance.
        if math.isfinite(distance) and distance < clearance:
            raise UnsafeLocalizationTurn("Obstacle inside rotation sweep")
