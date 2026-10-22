"""Fail-closed fusion of recent LiDAR frames while wheel encoders are stationary.

A multi-frame median is valid only if the robot stayed still. This function does
not project moving scans into a map frame and never commands motor movement.
"""
from __future__ import annotations

import math
import statistics
import time
from typing import Any

from server.auto_initial_pose import AutoPoseUncertain


def fuse_stationary_scans(samples: list[dict[str, Any]], *, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else float(now)
    if not isinstance(samples, list) or not 3 <= len(samples) <= 8:
        raise AutoPoseUncertain("At least three recent LiDAR frames are required")
    frames: list[dict[str, Any]] = []
    tick_samples = []
    times = []
    for sample in samples:
        if not isinstance(sample, dict) or not isinstance(sample.get("scan"), dict):
            raise AutoPoseUncertain("Scan history entry malformed")
        frame = sample["scan"]
        ticks = sample.get("wheel_ticks")
        if not isinstance(ticks, (list, tuple)) or len(ticks) != 4 or any(type(x) is not int for x in ticks):
            raise AutoPoseUncertain("Wheel encoder proof unavailable")
        try:
            at = float(frame["received_at"])
            encoder_at = float(sample["encoder_received_at"])
            inc = float(frame["angle_increment"])
            minimum = float(frame["angle_min"])
            low = float(frame["range_min"])
            high = float(frame["range_max"])
            ranges = frame["ranges"]
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise AutoPoseUncertain("Invalid LiDAR/encoder sample") from exc
        if not (all(map(math.isfinite, (at, encoder_at, inc, minimum, low, high)))
                and 0 <= at - encoder_at <= 0.4
                and 0 <= now - at <= 2.1 and 0 < inc <= 0.5
                and 0 <= low < high and isinstance(ranges, list)
                and 24 <= len(ranges) <= 10000):
            raise AutoPoseUncertain("Stale or invalid LiDAR/encoder timestamp")
        frames.append(frame)
        tick_samples.append(ticks)
        times.append(at)
    if any(times[i] <= times[i - 1] for i in range(1, len(times))):
        raise AutoPoseUncertain("LiDAR timestamps not strictly increasing")
    if not (0.2 <= times[-1] - times[0] <= 2.0 and now - times[-1] <= 1.5):
        raise AutoPoseUncertain("Insufficient fresh multi-frame observation window")
    for wheel in range(4):
        values = [ticks[wheel] for ticks in tick_samples]
        if max(values) != min(values):
            raise AutoPoseUncertain("Wheel encoder movement during localization")

    first = frames[0]
    try:
        reference = [float(first[k]) for k in ("angle_min", "angle_increment", "range_min", "range_max")]
        n = len(first["ranges"])
        mount = first.get("mount")
        for frame in frames[1:]:
            if len(frame["ranges"]) != n or any(
                not math.isclose(float(frame[k]), value, abs_tol=1e-6, rel_tol=0.0)
                for k, value in zip(("angle_min", "angle_increment", "range_min", "range_max"), reference)
            ) or frame.get("mount") != mount:
                raise AutoPoseUncertain("LiDAR scan geometry or mount changed")
    except (TypeError, KeyError, ValueError, OverflowError) as exc:
        if isinstance(exc, AutoPoseUncertain):
            raise
        raise AutoPoseUncertain("Scan geometry invalid") from exc

    result: list[float] = []
    reliable = 0
    for index in range(n):
        valid = []
        for frame in frames:
            try:
                d = float(frame["ranges"][index])
                if math.isfinite(d) and max(reference[2], 0.12) <= d <= min(reference[3], 4.5):
                    valid.append(d)
            except (ValueError, TypeError, OverflowError):
                continue
        if len(valid) < max(2, math.ceil(len(frames) * 0.6)):
            result.append(math.inf)
            continue
        median = statistics.median(valid)
        stable = [value for value in valid if abs(value - median) <= 0.18]
        if len(stable) < max(2, math.ceil(len(frames) * 0.6)):
            result.append(math.inf)
            continue
        result.append(median)
        reliable += 1
    if reliable < max(12, math.ceil(n * 0.25)):
        raise AutoPoseUncertain("Insufficient stable LiDAR beams across frames")
    fused = dict(first)
    fused["ranges"] = result
    fused["received_at"] = times[-1]
    fused["frames_fused"] = len(frames)
    fused["stable_beams"] = reliable
    return fused
