from __future__ import annotations

import math
import threading
from datetime import datetime, timezone
from typing import Any


def _utc_iso(epoch: float | None = None) -> str:
    if epoch is None:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return (
        datetime.fromtimestamp(float(epoch), timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _rle_encode(values: list[int]) -> list[list[int]]:
    if not values:
        return []
    runs: list[list[int]] = []
    last = int(values[0])
    count = 1
    for raw in values[1:]:
        value = int(raw)
        if value == last:
            count += 1
            continue
        runs.append([last, count])
        last = value
        count = 1
    runs.append([last, count])
    return runs


def _decode_cells(payload: dict[str, Any], expected: int) -> list[int]:
    raw = payload.get("data")
    if not isinstance(raw, list):
        raise ValueError("map data must be a list")
    if payload.get("data_encoding", "raw") == "rle":
        cells: list[int] = []
        for run in raw:
            if not isinstance(run, list) or len(run) < 2:
                raise ValueError("invalid rle map data")
            value = int(run[0])
            count = int(run[1])
            if count < 0:
                raise ValueError("invalid rle map count")
            cells.extend([value] * count)
            if len(cells) > expected:
                raise ValueError("rle map data is longer than expected")
    else:
        cells = [int(value) for value in raw]
    if len(cells) != expected:
        raise ValueError(
            f"map data length mismatch: expected {expected}, got {len(cells)}"
        )
    return cells


class LiveMappingGrid:
    """Persistent 2D occupancy grid updated from the newest LiDAR scan once/sec."""

    UNKNOWN = -1
    FREE = 0
    OCCUPIED = 100

    def __init__(
        self,
        *,
        resolution: float = 0.05,
        refresh_sec: float = 1.0,
        max_clear_range_m: float = 8.0,
        padding_cells: int = 4,
        sensor_x: float = 0.0,
        sensor_y: float = 0.0,
        sensor_yaw: float = math.pi,
    ) -> None:
        self.resolution = max(0.01, float(resolution))
        self.refresh_sec = max(0.1, float(refresh_sec))
        self.max_clear_range_m = max(self.resolution, float(max_clear_range_m))
        self.padding_cells = max(0, int(padding_cells))
        self.sensor_x = float(sensor_x)
        self.sensor_y = float(sensor_y)

        configured_yaw = float(sensor_yaw)
        self.sensor_yaw = (
            math.pi if abs(configured_yaw) < 1e-9 else configured_yaw
        )

        self._lock = threading.Lock()
        self._cells: dict[tuple[int, int], int] = {}
        self._pose: dict[str, float] | None = None
        self._scan: dict[str, Any] | None = None
        self._last_update_monotonic: float | None = None
        self._sequence = 0
        self._anchor_x = 0.0
        self._anchor_y = 0.0
        self._anchor_yaw = 0.0
        self._base_map_seen = False

    def reset(self) -> None:
        with self._lock:
            self._cells.clear()
            self._pose = None
            self._scan = None
            self._last_update_monotonic = None
            self._sequence = 0
            self._anchor_x = 0.0
            self._anchor_y = 0.0
            self._anchor_yaw = 0.0
            self._base_map_seen = False

    def update_pose(self, pose: dict[str, Any]) -> bool:
        try:
            x = float(pose["x"])
            y = float(pose["y"])
            yaw = float(pose.get("yaw", 0.0))
        except (KeyError, TypeError, ValueError):
            return False
        if not all(math.isfinite(value) for value in (x, y, yaw)):
            return False
        with self._lock:
            self._pose = {"x": x, "y": y, "yaw": yaw}
        return True

    def update_scan(self, scan: dict[str, Any]) -> bool:
        ranges = scan.get("ranges")
        if not isinstance(ranges, list) or not ranges:
            return False
        try:
            angle_min = float(scan["angle_min"])
            angle_increment = float(scan["angle_increment"])
            range_min = float(scan["range_min"])
            range_max = float(scan["range_max"])
        except (KeyError, TypeError, ValueError):
            return False
        if (
            not all(
                math.isfinite(value)
                for value in (
                    angle_min,
                    angle_increment,
                    range_min,
                    range_max,
                )
            )
            or angle_increment <= 0.0
            or range_min < 0.0
            or range_max <= range_min
        ):
            return False

        normalized_ranges: list[float | None] = []
        for raw in ranges:
            if raw is None:
                normalized_ranges.append(None)
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                normalized_ranges.append(None)
                continue
            normalized_ranges.append(value if math.isfinite(value) else None)

        with self._lock:
            self._scan = {
                "angle_min": angle_min,
                "angle_increment": angle_increment,
                "range_min": range_min,
                "range_max": range_max,
                "ranges": normalized_ranges,
                "timestamp": scan.get("sent_at")
                or scan.get("timestamp")
                or scan.get("bridge_timestamp"),
            }
        return True

    def update_base_map(self, payload: dict[str, Any]) -> bool:
        """Merge the latest slam_toolbox OccupancyGrid into the persistent map."""
        try:
            width = int(payload["width"])
            height = int(payload["height"])
            resolution = float(payload["resolution"])
            origin = payload.get("origin") or {}
            origin_x = float(origin.get("x", 0.0))
            origin_y = float(origin.get("y", 0.0))
            origin_yaw = float(origin.get("yaw", 0.0))
        except (KeyError, TypeError, ValueError):
            return False

        if (
            width <= 0
            or height <= 0
            or resolution <= 0.0
            or not all(
                math.isfinite(value)
                for value in (
                    resolution,
                    origin_x,
                    origin_y,
                    origin_yaw,
                )
            )
        ):
            return False

        try:
            incoming = _decode_cells(payload, width * height)
        except (TypeError, ValueError):
            return False

        with self._lock:
            if not self._base_map_seen:
                self._reanchor_locked(
                    origin_x,
                    origin_y,
                    origin_yaw,
                    resolution,
                )
                self._base_map_seen = True
            elif (
                abs(self.resolution - resolution) > 1e-9
                or abs(self._anchor_yaw - origin_yaw) > 1e-6
            ):
                self._reanchor_locked(
                    self._anchor_x,
                    self._anchor_y,
                    origin_yaw,
                    resolution,
                )

            cos_yaw = math.cos(origin_yaw)
            sin_yaw = math.sin(origin_yaw)
            for y in range(height):
                for x in range(width):
                    value = int(incoming[y * width + x])
                    local_x = (x + 0.5) * resolution
                    local_y = (y + 0.5) * resolution
                    world_x = (
                        origin_x
                        + cos_yaw * local_x
                        - sin_yaw * local_y
                    )
                    world_y = (
                        origin_y
                        + sin_yaw * local_x
                        + cos_yaw * local_y
                    )
                    cell = self._world_to_cell(world_x, world_y)
                    if value < 0:
                        self._cells.pop(cell, None)
                    else:
                        self._cells[cell] = max(0, min(100, value))

        return True

    def snapshot(
        self,
        *,
        robot_id: str,
        wall_time: float,
    ) -> dict[str, Any] | None:
        with self._lock:
            if not self._cells:
                return None
            return self._snapshot_locked(robot_id, wall_time)

    def maybe_update(
        self,
        *,
        robot_id: str,
        monotonic_now: float,
        wall_time: float,
        force: bool = False,
    ) -> dict[str, Any] | None:
        with self._lock:
            if self._pose is None or self._scan is None:
                return None
            if (
                not force
                and self._last_update_monotonic is not None
                and monotonic_now - self._last_update_monotonic < self.refresh_sec
            ):
                return None

            self._integrate_locked(self._pose, self._scan)
            self._last_update_monotonic = float(monotonic_now)
            self._sequence += 1
            return self._snapshot_locked(robot_id, wall_time)

    def _world_to_cell(self, x: float, y: float) -> tuple[int, int]:
        dx = x - self._anchor_x
        dy = y - self._anchor_y
        cos_yaw = math.cos(self._anchor_yaw)
        sin_yaw = math.sin(self._anchor_yaw)
        local_x = cos_yaw * dx + sin_yaw * dy
        local_y = -sin_yaw * dx + cos_yaw * dy
        return (
            math.floor(local_x / self.resolution),
            math.floor(local_y / self.resolution),
        )

    def _cell_center_world(
        self,
        cell_x: int,
        cell_y: int,
    ) -> tuple[float, float]:
        local_x = (cell_x + 0.5) * self.resolution
        local_y = (cell_y + 0.5) * self.resolution
        cos_yaw = math.cos(self._anchor_yaw)
        sin_yaw = math.sin(self._anchor_yaw)
        return (
            self._anchor_x + cos_yaw * local_x - sin_yaw * local_y,
            self._anchor_y + sin_yaw * local_x + cos_yaw * local_y,
        )

    def _reanchor_locked(
        self,
        origin_x: float,
        origin_y: float,
        origin_yaw: float,
        resolution: float,
    ) -> None:
        previous = [
            (*self._cell_center_world(cell_x, cell_y), value)
            for (cell_x, cell_y), value in self._cells.items()
        ]
        self.resolution = max(0.01, float(resolution))
        self._anchor_x = float(origin_x)
        self._anchor_y = float(origin_y)
        self._anchor_yaw = float(origin_yaw)
        self._cells.clear()
        for world_x, world_y, value in previous:
            self._cells[self._world_to_cell(world_x, world_y)] = value

    @staticmethod
    def _bresenham(
        start: tuple[int, int],
        end: tuple[int, int],
    ):
        x0, y0 = start
        x1, y1 = end
        dx = abs(x1 - x0)
        dy = abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        error = dx - dy

        while True:
            yield x0, y0
            if x0 == x1 and y0 == y1:
                break
            doubled = error * 2
            if doubled > -dy:
                error -= dy
                x0 += sx
            if doubled < dx:
                error += dx
                y0 += sy

    def _integrate_locked(
        self,
        pose: dict[str, float],
        scan: dict[str, Any],
    ) -> None:
        base_x = pose["x"]
        base_y = pose["y"]
        base_yaw = pose["yaw"]

        cos_base = math.cos(base_yaw)
        sin_base = math.sin(base_yaw)
        sensor_x = (
            base_x
            + cos_base * self.sensor_x
            - sin_base * self.sensor_y
        )
        sensor_y = (
            base_y
            + sin_base * self.sensor_x
            + cos_base * self.sensor_y
        )
        sensor_yaw = base_yaw + self.sensor_yaw
        start_cell = self._world_to_cell(sensor_x, sensor_y)
        self._cells[start_cell] = self.FREE

        angle = scan["angle_min"]
        range_min = scan["range_min"]
        range_max = scan["range_max"]
        clear_no_hit = min(range_max, self.max_clear_range_m)
        free_updates: set[tuple[int, int]] = set()
        occupied_updates: set[tuple[int, int]] = set()

        for raw_distance in scan["ranges"]:
            has_hit = (
                raw_distance is not None
                and range_min <= raw_distance <= range_max
            )
            distance = float(raw_distance) if has_hit else clear_no_hit
            if distance <= 0.0:
                angle += scan["angle_increment"]
                continue

            world_angle = sensor_yaw + angle
            end_x = sensor_x + math.cos(world_angle) * distance
            end_y = sensor_y + math.sin(world_angle) * distance
            end_cell = self._world_to_cell(end_x, end_y)
            ray = list(self._bresenham(start_cell, end_cell))

            if has_hit:
                free_updates.update(ray[:-1])
                occupied_updates.add(end_cell)
            else:
                free_updates.update(ray)

            angle += scan["angle_increment"]

        # Apply free space first and hits last so adjacent rays cannot erase a
        # wall endpoint in the same one-second scan update.
        for cell in free_updates - occupied_updates:
            self._cells[cell] = self.FREE
        for cell in occupied_updates:
            self._cells[cell] = self.OCCUPIED

    def _snapshot_locked(
        self,
        robot_id: str,
        wall_time: float,
    ) -> dict[str, Any]:
        xs = [cell[0] for cell in self._cells]
        ys = [cell[1] for cell in self._cells]
        min_x = min(xs) - self.padding_cells
        max_x = max(xs) + self.padding_cells
        min_y = min(ys) - self.padding_cells
        max_y = max(ys) + self.padding_cells

        width = max_x - min_x + 1
        height = max_y - min_y + 1
        dense = [self.UNKNOWN] * (width * height)

        for (cell_x, cell_y), value in self._cells.items():
            local_x = cell_x - min_x
            local_y = cell_y - min_y
            dense[local_y * width + local_x] = int(value)

        timestamp = (
            self._scan.get("timestamp")
            if isinstance(self._scan, dict)
            else None
        ) or _utc_iso(wall_time)

        return {
            "robot_id": robot_id,
            "navigation_mode": "mapping",
            "frame_id": "map",
            "timestamp": timestamp,
            "bridge_timestamp": _utc_iso(wall_time),
            "resolution": self.resolution,
            "width": width,
            "height": height,
            "origin": {
                "x": (
                    self._anchor_x
                    + math.cos(self._anchor_yaw) * min_x * self.resolution
                    - math.sin(self._anchor_yaw) * min_y * self.resolution
                ),
                "y": (
                    self._anchor_y
                    + math.sin(self._anchor_yaw) * min_x * self.resolution
                    + math.cos(self._anchor_yaw) * min_y * self.resolution
                ),
                "z": 0.0,
                "yaw": self._anchor_yaw,
            },
            "data_encoding": "rle",
            "data": _rle_encode(dense),
            "source": "live_mapping_grid",
            "sequence": self._sequence,
        }
