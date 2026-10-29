"""Convert recorded timed events into bounded motor exposure segments.

Preserves event order and STOP gaps; no assumption that a network ACK duration
equals applied motor time. All motor timing is accounted by Pi pulse ACKs.
"""
from __future__ import annotations

import math


DIRECTIONS = frozenset({
    "forward", "backward", "left", "right", "forward_left",
    "forward_right", "backward_left", "backward_right",
    "rotate_left", "rotate_right",
})


def compile_manual_segments(events: list[dict]) -> list[dict]:
    if not isinstance(events, list) or not 2 <= len(events) <= 10000:
        raise ValueError("MANUAL_REPLAY_INVALID_EVENTS")
    result = []
    previous_time = -1.0
    for index, current in enumerate(events):
        if not isinstance(current, dict):
            raise ValueError("MANUAL_REPLAY_INVALID_EVENT")
        try:
            at = float(current["at"])
            kind = current["type"]
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("MANUAL_REPLAY_INVALID_EVENT") from exc
        if not math.isfinite(at) or at < 0 or at < previous_time or at > 3600:
            raise ValueError("MANUAL_REPLAY_INVALID_EVENT_TIME")
        previous_time = at
        if kind not in ("move", "stop"):
            raise ValueError("MANUAL_REPLAY_INVALID_EVENT_TYPE")
        if kind == "move":
            try:
                speed = float(current["speed"])
                direction = current["direction"]
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise ValueError("MANUAL_REPLAY_INVALID_MOVE") from exc
            if (direction not in DIRECTIONS or not math.isfinite(speed)
                or speed < 0 or speed > 0.30):
                raise ValueError("MANUAL_REPLAY_INVALID_MOVE")
        if index + 1 == len(events):
            if kind != "stop":
                raise ValueError("MANUAL_REPLAY_STOP_MISSING")
            continue
        try:
            next_at = float(events[index + 1]["at"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("MANUAL_REPLAY_INVALID_EVENT_TIME") from exc
        delta = next_at - at
        if not math.isfinite(delta) or delta < 0:
            raise ValueError("MANUAL_REPLAY_INVALID_EVENT_TIME")
        segment = {
            "type": "move" if kind == "move" and speed > 0 else "stop",
            "duration_ms": delta * 1000,
        }
        if segment["type"] == "move":
            segment.update(direction=direction, speed=speed)
            if (result and result[-1]["type"] == "move"
                and result[-1]["direction"] == direction
                and result[-1]["speed"] == speed):
                # The Dashboard records the same held D-pad direction at
                # ~120ms intervals. Coalesce its heartbeats without changing
                # the motion duration or direction-change/STOP boundaries.
                result[-1]["duration_ms"] += segment["duration_ms"]
                continue
        elif result and result[-1]["type"] == "stop":
            result[-1]["duration_ms"] += segment["duration_ms"]
            continue
        result.append(segment)
    if not any(v["type"] == "move" and v["duration_ms"] > 0 for v in result):
        raise ValueError("MANUAL_REPLAY_NO_MOTION")
    return result
