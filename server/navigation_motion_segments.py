"""Pure-Python helpers for preserving Nav2 State Lattice motion semantics.

The ROS Path message stores heading per pose but no explicit reverse/spin flag.
A motion belongs to the *starting* point of each path edge, matching the
viewer contract. These helpers do not publish any motor commands.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


def _finite(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return numeric if math.isfinite(numeric) else None


def _angle_difference(a: float, b: float) -> float:
    return math.atan2(math.sin(a - b), math.cos(a - b))


def annotate_path(
    path: Sequence[Mapping[str, Any]],
    *,
    distance_epsilon: float = 0.003,
    rotation_epsilon: float = 0.02,
) -> list[dict[str, float | str]]:
    """Preserve validated X/Y/Yaw and tag forward/reverse/pivot path edges.

    Legacy paths without yaw remain XY-only, with no invented heading.
    Rotation-only edges preserve coincident XY points because dropping them
    destroys the State Lattice's in-place spin information.
    """
    result: list[dict[str, float | str]] = []
    for item in path[:5000]:
        if not isinstance(item, Mapping):
            continue
        x, y = _finite(item.get("x")), _finite(item.get("y"))
        if x is None or y is None:
            continue
        current: dict[str, float | str] = {"x": x, "y": y}
        yaw = _finite(item.get("yaw"))
        if yaw is not None:
            current["yaw"] = _angle_difference(yaw, 0.0)
        if result and "yaw" in result[-1] and "yaw" in current:
            previous = result[-1]
            dx = x - float(previous["x"])
            dy = y - float(previous["y"])
            length = math.hypot(dx, dy)
            if length < distance_epsilon:
                delta_yaw = _angle_difference(
                    float(current["yaw"]), float(previous["yaw"]),
                )
                previous["motion"] = (
                    "rotate_left" if delta_yaw > rotation_epsilon
                    else "rotate_right" if delta_yaw < -rotation_epsilon
                    else "hold"
                )
            else:
                heading = float(previous["yaw"])
                projection = dx * math.cos(heading) + dy * math.sin(heading)
                previous["motion"] = (
                    "reverse" if projection < -distance_epsilon else "forward"
                )
        result.append(current)
    return result


def motion_segments(
    path: Sequence[Mapping[str, Any]],
    *,
    distance_epsilon: float = 0.003,
) -> list[dict[str, float | str | int]]:
    """Return maximal contiguous forward/reverse/spin groups for handoff.

    The end pose belongs to both adjacent groups; the index ranges are
    inclusive and refer to the original path. Never coalesce a reverse cusp
    into a forward group or discard a zero-translation rotation.
    """
    segments: list[dict[str, float | str | int]] = []
    if len(path) < 2:
        return segments
    for index in range(len(path) - 1):
        start, end = path[index], path[index + 1]
        sx, sy = _finite(start.get("x")), _finite(start.get("y"))
        ex, ey = _finite(end.get("x")), _finite(end.get("y"))
        if None in (sx, sy, ex, ey):
            continue
        motion = str(start.get("motion") or "unknown")
        length = math.hypot(ex - sx, ey - sy)
        change = 0.0
        start_yaw, end_yaw = _finite(start.get("yaw")), _finite(end.get("yaw"))
        if start_yaw is not None and end_yaw is not None:
            change = abs(_angle_difference(end_yaw, start_yaw))
        if motion == "hold" and length < distance_epsilon and change < 0.02:
            continue
        if segments and segments[-1]["motion"] == motion and segments[-1]["end_index"] == index:
            segments[-1]["end_index"] = index + 1
            segments[-1]["distance_m"] = float(segments[-1]["distance_m"]) + length
            segments[-1]["rotation_rad"] = float(segments[-1]["rotation_rad"]) + change
            continue
        segments.append({
            "motion": motion,
            "start_index": index,
            "end_index": index + 1,
            "distance_m": length,
            "rotation_rad": change,
        })
    return segments


def estimate_travel_time(
    segments: Sequence[Mapping[str, Any]],
    *,
    max_wheel_mps: float,
    max_angular_rps: float,
    max_accel_mps2: float = 0.5,
    max_angular_accel_rps2: float = 1.0,
    max_jerk_mps3: float = 2.0,
    direction_change_delay_sec: float = 0.2,
) -> float:
    """Approximate minimum travel time, including starts/stops and direction changes.

    This is a *ranking estimate*, not a braking safety proof. It does not
    replace Smac's own search costs; candidate selection must call it explicitly.
    """
    limits = (max_wheel_mps, max_angular_rps, max_accel_mps2,
              max_angular_accel_rps2, max_jerk_mps3)
    if not all(math.isfinite(float(v)) and float(v) > 0 for v in limits):
        raise ValueError("motion limits must be finite positive numbers")
    if not math.isfinite(direction_change_delay_sec) or direction_change_delay_sec < 0:
        raise ValueError("direction-change delay must be finite and nonnegative")

    total = 0.0
    previous: str | None = None
    for segment in segments:
        motion = str(segment.get("motion", "unknown"))
        distance = float(segment.get("distance_m", 0.0))
        rotation = float(segment.get("rotation_rad", 0.0))
        if not all(math.isfinite(v) and v >= 0 for v in (distance, rotation)):
            raise ValueError("invalid segment length")
        if motion in {"forward", "reverse"}:
            # Triangular speed profile for a short run, trapezoidal for longer.
            critical = max_wheel_mps ** 2 / max_accel_mps2
            if distance <= critical:
                duration = 2.0 * math.sqrt(distance / max_accel_mps2)
            else:
                duration = distance / max_wheel_mps + max_wheel_mps / max_accel_mps2
            duration += min(
                max_accel_mps2 / max_jerk_mps3, duration / 4.0,
            ) if distance > 0 else 0.0
        elif motion in {"rotate_left", "rotate_right"}:
            critical = max_angular_rps ** 2 / max_angular_accel_rps2
            duration = (
                2.0 * math.sqrt(rotation / max_angular_accel_rps2)
                if rotation <= critical else
                rotation / max_angular_rps
                + max_angular_rps / max_angular_accel_rps2
            )
        elif motion == "hold":
            continue
        else:
            raise ValueError(f"unrecognized motion segment: {motion}")
        if previous is not None and motion != previous:
            total += direction_change_delay_sec
        total += duration
        previous = motion
    return total
