"""Conservative initial-pose occupancy and clearance checks.

World coordinates are transformed into the stored OccupancyGrid origin frame.
UNKNOWN and near-obstacle areas require explicit operator acknowledgment.
"""
from __future__ import annotations

import math
from typing import Any


class PoseGridError(ValueError):
    pass


def decode_grid(data: list[Any], encoding: str, width: int, height: int) -> list[int]:
    expected = width * height
    if not 0 < expected <= 2_000_000 or not isinstance(data, list):
        raise PoseGridError("Invalid OccupancyGrid dimensions or cells")
    if encoding == "rle":
        result: list[int] = []
        for run in data:
            if not isinstance(run, list) or len(run) != 2:
                raise PoseGridError("Invalid OccupancyGrid RLE run")
            value, count = int(run[0]), int(run[1])
            if count <= 0 or count > expected - len(result):
                raise PoseGridError("Invalid OccupancyGrid RLE length")
            result.extend([value] * count)
    elif encoding == "raw":
        result = [int(value) for value in data]
    else:
        raise PoseGridError("Unknown OccupancyGrid encoding")
    if len(result) != expected or any(value < -1 or value > 100 for value in result):
        raise PoseGridError("Invalid OccupancyGrid occupancy values")
    return result


def evaluate_initial_pose_grid(
    *, width: int, height: int, resolution: float, origin: dict[str, float],
    cells: list[int], x: float, y: float,
    body_radius_m: float = 0.175, warning_margin_m: float = 0.20,
) -> dict[str, Any]:
    if (width <= 0 or height <= 0 or len(cells) != width * height
        or resolution <= 0 or not all(math.isfinite(float(v)) for v in (
            resolution, x, y, origin["x"], origin["y"], origin.get("yaw", 0.0),
        ))):
        raise PoseGridError("Invalid map geometry or pose")
    theta = float(origin.get("yaw", 0.0))
    dx, dy = x - float(origin["x"]), y - float(origin["y"])
    lx = math.cos(theta) * dx + math.sin(theta) * dy
    ly = -math.sin(theta) * dx + math.cos(theta) * dy
    if not 0 <= lx < width * resolution or not 0 <= ly < height * resolution:
        return {"risk": "BLOCKED", "reason": "OUT_OF_BOUNDS"}

    # A cell has area, not just its center. Include its half-diagonal.
    half_diag = resolution * math.sqrt(0.5)
    occupied_radius = body_radius_m + half_diag
    warning_radius = occupied_radius + warning_margin_m
    cx, cy = lx / resolution, ly / resolution
    steps = int(math.ceil(warning_radius / resolution)) + 1
    unknown = False
    near_obstacle = False
    for row in range(max(0, math.floor(cy) - steps), min(height, math.floor(cy) + steps + 1)):
        for col in range(max(0, math.floor(cx) - steps), min(width, math.floor(cx) + steps + 1)):
            value = cells[row * width + col]
            distance = math.hypot((col + 0.5) * resolution - lx,
                                  (row + 0.5) * resolution - ly)
            if distance <= occupied_radius:
                if value >= 65:
                    return {"risk": "BLOCKED", "reason": "OCCUPIED_FOOTPRINT"}
                if value < 0:
                    unknown = True
            elif distance <= warning_radius:
                near_obstacle |= value >= 65
                unknown |= value < 0

    # Lack of mapped clearance at the border also requires confirmation.
    border = min(lx, ly, width * resolution - lx, height * resolution - ly)
    if unknown or near_obstacle or border < body_radius_m + warning_margin_m:
        return {"risk": "WARNING", "reason": (
            "UNKNOWN" if unknown else "NEAR_OBSTACLE" if near_obstacle else "NEAR_MAP_EDGE"
        )}
    return {"risk": "CLEAR", "reason": None}
