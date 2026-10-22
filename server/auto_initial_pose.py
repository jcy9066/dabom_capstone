"""Conservative, hardware-free 2D LiDAR-to-saved-grid Initial Pose estimate.

Global candidate search is bounded; ambiguous/weak results never authorize Driving.
All scan angles are laser-local; ROS laser mounting extrinsics are applied exactly once.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

class AutoPoseUncertain(ValueError):
    pass


def decode_saved_cells(raw_path: Path, width: int, height: int) -> list[int]:
    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    values = payload.get("data")
    expected = width * height
    if not isinstance(values, list) or expected <= 0 or expected > 2_000_000:
        raise AutoPoseUncertain("Saved grid has no usable occupancy cells")
    if payload.get("data_encoding", "raw") == "rle":
        decoded: list[int] = []
        for entry in values:
            if not isinstance(entry, list) or len(entry) != 2:
                raise AutoPoseUncertain("Invalid occupancy run")
            value, count = int(entry[0]), int(entry[1])
            if count < 0 or count > expected - len(decoded):
                raise AutoPoseUncertain("Occupancy run exceeds map dimensions")
            decoded.extend([value] * count)
        values = decoded
    if len(values) != expected:
        raise AutoPoseUncertain("Incomplete occupancy grid")
    return [int(v) for v in values]


def estimate_initial_pose(
    selected_map: Any, scan: dict[str, Any], *,
    mount_x: float = 0.0, mount_y: float = 0.0, mount_yaw: float = math.pi,
    now: float | None = None,
) -> dict[str, float]:
    if not isinstance(scan, dict):
        raise AutoPoseUncertain("No live LiDAR scan")
    now = time.time() if now is None else now
    try:
        age = now - float(scan["received_at"])
        amin = float(scan["angle_min"])
        step = float(scan["angle_increment"])
        rmin = float(scan["range_min"])
        rmax = float(scan["range_max"])
        ranges = scan["ranges"]
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise AutoPoseUncertain("Invalid LiDAR frame") from exc
    if not (0 <= age <= 1.5 and math.isfinite(age) and math.isfinite(amin)
            and math.isfinite(step) and 0 < step <= math.pi / 2
            and 0 <= rmin < rmax and isinstance(ranges, list) and len(ranges) <= 10000):
        raise AutoPoseUncertain("LiDAR frame stale or malformed")
    if not all(math.isfinite(v) for v in (mount_x, mount_y, mount_yaw)):
        raise AutoPoseUncertain("LiDAR extrinsics invalid")
    # Match the effective mount yaw used by LidarRosBridge.
    if abs(mount_yaw) < 1e-9:
        mount_yaw = math.pi
    reported_mount = scan.get("mount")
    if reported_mount is not None:
        try:
            rx = float(reported_mount["x"])
            ry = float(reported_mount["y"])
            ryaw = float(reported_mount["yaw"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise AutoPoseUncertain("LiDAR mount transform malformed") from exc
        yaw_error = math.atan2(math.sin(ryaw - mount_yaw), math.cos(ryaw - mount_yaw))
        if not all(math.isfinite(v) for v in (rx, ry, ryaw)) or (
            abs(rx - mount_x) > 0.03 or abs(ry - mount_y) > 0.03 or abs(yaw_error) > 0.1
        ):
            raise AutoPoseUncertain("Live LiDAR extrinsics disagree with ROS configuration")
    sample_step = max(1, math.ceil(len(ranges) / 48))
    beams: list[tuple[float, float]] = []
    for index in range(0, len(ranges), sample_step):
        try:
            distance = float(ranges[index])
        except (ValueError, TypeError, OverflowError):
            continue
        if math.isfinite(distance) and max(rmin, 0.12) <= distance <= min(rmax, 4.5):
            beams.append((amin + index * step + mount_yaw, distance))
    if len(beams) < 12:
        raise AutoPoseUncertain("Insufficient valid LiDAR obstacle returns")

    width, height = int(selected_map.width), int(selected_map.height)
    resolution = float(selected_map.resolution)
    if width < 5 or height < 5 or resolution <= 0 or width * height > 2_000_000:
        raise AutoPoseUncertain("Invalid map dimensions")
    cells = decode_saved_cells(selected_map.raw_path, width, height)
    if sum(1 for cell in cells if cell >= 65) < 12:
        raise AutoPoseUncertain("Saved map has insufficient confirmed obstacles")
    origin_x, origin_y, origin_yaw = (
        float(selected_map.origin_x), float(selected_map.origin_y), float(selected_map.origin_yaw))
    co, so = math.cos(origin_yaw), math.sin(origin_yaw)

    def cell_xy(x: float, y: float) -> tuple[int, int]:
        dx, dy = x - origin_x, y - origin_y
        return (math.floor((co * dx + so * dy) / resolution),
                math.floor((-so * dx + co * dy) / resolution))

    def occupied_near(x: float, y: float) -> bool:
        col, row = cell_xy(x, y)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                cx, cy = col + dx, row + dy
                if 0 <= cx < width and 0 <= cy < height:
                    if cells[cy * width + cx] >= 65:
                        return True
        return False

    def free_base(x: float, y: float) -> bool:
        col, row = cell_xy(x, y)
        radius = max(1, math.ceil(0.18 / resolution))
        if not (radius <= col < width - radius and radius <= row < height - radius):
            return False
        # Verify every grid cell within the chassis clearance disk.
        # Checking only the perimeter misses obstacles directly under the car.
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                if dx * dx + dy * dy > radius * radius:
                    continue
                value = cells[(row + dy) * width + col + dx]
                if value < 0 or value > 25:
                    return False
        return True

    def rate(x: float, y: float, yaw: float) -> float:
        cs, sn = math.cos(yaw), math.sin(yaw)
        lx = x + cs * mount_x - sn * mount_y
        ly = y + sn * mount_x + cs * mount_y
        hits = 0
        occlusion = 0
        for angle, d in beams:
            direction = yaw + angle
            ca, sa = math.cos(direction), math.sin(direction)
            if occupied_near(lx + d * ca, ly + d * sa):
                hits += 1
            # An unexpected obstacle strictly before a scan endpoint is a
            # physical contradiction, not positive evidence.
            for fraction in (0.40, 0.70):
                if occupied_near(lx + d * fraction * ca, ly + d * fraction * sa):
                    occlusion += 1
        return (hits - 0.5 * occlusion) / len(beams)

    total = width * height
    stride = max(1, math.ceil(math.sqrt(total / 320)))
    candidates: list[tuple[float, float, float, float]] = []
    for row in range(stride // 2, height, stride):
        for col in range(stride // 2, width, stride):
            x = origin_x + co * (col + 0.5) * resolution - so * (row + 0.5) * resolution
            y = origin_y + so * (col + 0.5) * resolution + co * (row + 0.5) * resolution
            if not free_base(x, y):
                continue
            for direction_index in range(16):
                yaw = -math.pi + direction_index * (math.pi / 8)
                candidates.append((rate(x, y, yaw), x, y, yaw))
    if not candidates:
        raise AutoPoseUncertain("No valid free-space position candidates")
    candidates.sort(reverse=True)
    refined: list[tuple[float, float, float, float]] = []
    # Refine several *distinct* coarse candidates so a similar-looking
    # second location remains visible to the ambiguity check.
    seeds: list[tuple[float, float, float, float]] = []
    for item in candidates:
        if all(math.hypot(item[1] - seed[1], item[2] - seed[2]) > 0.25
               or abs(math.atan2(math.sin(item[3] - seed[3]),
                                 math.cos(item[3] - seed[3]))) > 0.35 for seed in seeds):
            seeds.append(item)
        if len(seeds) == 12:
            break
    for _, sx, sy, syaw in seeds:
        for dx in (-2, -1, 0, 1, 2):
            for dy in (-2, -1, 0, 1, 2):
                x, y = sx + dx * resolution, sy + dy * resolution
                if not free_base(x, y):
                    continue
                for yaw_offset in (-math.pi / 16, 0.0, math.pi / 16):
                    yaw = math.atan2(math.sin(syaw + yaw_offset),
                                     math.cos(syaw + yaw_offset))
                    refined.append((rate(x, y, yaw), x, y, yaw))
    if not refined:
        raise AutoPoseUncertain("No refined scan-match candidates")
    refined.sort(reverse=True)
    score, x, y, yaw = refined[0]
    # Intentionally strict: an uncertain estimate must use manual Initial Pose.
    if score < 0.78:
        raise AutoPoseUncertain("Scan/map agreement below safe confidence threshold")
    for alternative, ax, ay, ayaw in refined[1:]:
        separated = math.hypot(x - ax, y - ay) > 0.5 or abs(
            math.atan2(math.sin(yaw - ayaw), math.cos(yaw - ayaw))) > 0.5
        if separated and alternative >= score - 0.12:
            raise AutoPoseUncertain("Several distinct locations match the scan")
    return {"x": x, "y": y, "yaw_degrees": math.degrees(yaw)}
