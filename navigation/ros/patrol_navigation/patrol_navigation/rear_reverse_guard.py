"""Conservative rear-sector observation and blind-reverse travel gate.

No ROS dependency: scan angles must first be transformed into base_link axes.
The limit is enforced again at the Pi PWM boundary: a bridge speed cap alone
would otherwise be overridden by the existing MIN_AUTO_DRIVE_PWM floor.
"""
from __future__ import annotations

import math


class RearReverseGuard:
    MAX_BLIND_DISTANCE_M = 0.30
    BLIND_SPEED_FRACTION = 0.30
    SCAN_MAX_AGE_SEC = 0.50
    ODOM_MAX_AGE_SEC = 0.30
    REAR_HALF_SECTOR_RAD = math.pi / 4
    CHASSIS_REAR_EXTENT_M = 0.14 + 0.03
    CONTROL_LATENCY_SEC = 0.30
    DECELERATION_MPS2 = 0.50

    def __init__(self) -> None:
        self._scan_at: float | None = None
        self._observed = False
        self._nearest_obstacle_m = math.inf
        self._odom_at: float | None = None
        self._last_pose: tuple[float, float, float] | None = None
        self._blind_distance_m = 0.0
        self._blind_moving = False
        self._odom_valid = True

    @property
    def blind_distance_m(self) -> float:
        return self._blind_distance_m

    def update_scan(
        self,
        ranges: list[float] | tuple[float, ...],
        *,
        angle_min: float,
        angle_increment: float,
        range_min: float,
        range_max: float,
        laser_yaw: float | None,
        now: float,
    ) -> None:
        # A failed/missing scan-frame TF can never prove rear visibility.
        self._scan_at = now
        self._observed = False
        self._nearest_obstacle_m = math.inf
        if (
            laser_yaw is None
            or not all(math.isfinite(v) for v in (
                laser_yaw, angle_min, angle_increment, range_min, range_max, now,
            ))
            or angle_increment == 0
            or range_max <= range_min
        ):
            return

        sectors = [False, False, False]
        for index, raw_range in enumerate(ranges):
            bearing = laser_yaw + angle_min + index * angle_increment
            offset = math.atan2(
                math.sin(bearing - math.pi),
                math.cos(bearing - math.pi),
            )
            if abs(offset) > self.REAR_HALF_SECTOR_RAD:
                continue
            try:
                measured = float(raw_range)
            except (ValueError, TypeError):
                continue
            if math.isnan(measured) or measured <= range_min:
                continue
            if not math.isinf(measured) and measured > range_max:
                continue
            sector = min(2, max(0, int((offset + math.pi / 4) / (math.pi / 6))))
            sectors[sector] = True
            if math.isfinite(measured):
                self._nearest_obstacle_m = min(
                    measured, self._nearest_obstacle_m,
                )
        self._observed = all(sectors)
        if self._observed:
            # A fresh, three-sector observation breaks the blind-drive streak.
            self._blind_distance_m = 0.0
            self._blind_moving = False

    def update_odometry(self, x: float, y: float, yaw: float, now: float) -> None:
        if not all(math.isfinite(v) for v in (x, y, yaw, now)):
            self._odom_valid = False
            return
        previous = self._last_pose
        old_time = self._odom_at
        if previous is not None and old_time is not None:
            delta = now - old_time
            if delta <= 0 or delta > self.ODOM_MAX_AGE_SEC:
                # Missing intervals make the blind distance uncertain.
                if self._blind_moving:
                    self._odom_valid = False
            elif self._blind_moving:
                dx, dy = x - previous[0], y - previous[1]
                projected = dx * math.cos(previous[2]) + dy * math.sin(previous[2])
                if math.hypot(dx, dy) > 0.30:
                    self._odom_valid = False
                elif projected < 0.0:
                    self._blind_distance_m += -projected
        self._last_pose = (x, y, yaw)
        self._odom_at = now

    def limit_reverse(
        self, speed_mps: float, *, max_wheel_mps: float, now: float,
    ) -> tuple[float, bool, str | None]:
        """Return safe signed linear_x, Pi PWM-cap flag and STOP reason."""
        if speed_mps >= 0:
            # Continue counting backward coasting after a zero/forward command;
            # a brief STOP does not authorize another blind movement.
            return speed_mps, False, None
        if (
            self._odom_at is None
            or not self._odom_valid
            or now - self._odom_at > self.ODOM_MAX_AGE_SEC
            or now < self._odom_at
        ):
            return 0.0, True, "rear_reverse_odom_unavailable"

        requested = abs(speed_mps)
        observed = (
            self._observed
            and self._scan_at is not None
            and 0 <= now - self._scan_at <= self.SCAN_MAX_AGE_SEC
        )
        safe_speed = min(requested, max_wheel_mps * self.BLIND_SPEED_FRACTION)
        braking_distance = (
            self.CHASSIS_REAR_EXTENT_M
            + requested * self.CONTROL_LATENCY_SEC
            + requested ** 2 / (2 * self.DECELERATION_MPS2)
            + 0.05
        )
        # Even incomplete observations can positively identify an obstacle.
        if (
            self._scan_at is not None
            and 0 <= now - self._scan_at <= self.SCAN_MAX_AGE_SEC
            and self._nearest_obstacle_m <= braking_distance
        ):
            return 0.0, True, "rear_obstacle_in_stop_distance"
        if observed:
            self._blind_distance_m = 0.0
            self._blind_moving = False
            return speed_mps, False, None

        # STOP early for braking + transport latency: no second blind segment.
        reserve = (
            safe_speed * self.CONTROL_LATENCY_SEC
            + safe_speed ** 2 / (2 * self.DECELERATION_MPS2)
        )
        if self._blind_distance_m + reserve >= self.MAX_BLIND_DISTANCE_M:
            return 0.0, True, "rear_blind_distance_exhausted"

        self._blind_moving = True
        return -safe_speed, True, None
