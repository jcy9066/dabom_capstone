"""Bounded fresh-observation retry for Initial Pose.

Never reuse frames or encoder sequences from the first failed localization.
This module cannot command motors or release STOP/E-STOP.
"""
from __future__ import annotations

import asyncio
import math
import time
from typing import Callable, Any

from server.auto_initial_pose import AutoPoseUncertain
from server.scan_pose_fusion import fuse_stationary_scans


def observation_baseline(history: list[dict[str, Any]]) -> tuple[float, int]:
    times = [
        float(item["scan"]["received_at"])
        for item in history
        if isinstance(item, dict) and isinstance(item.get("scan"), dict)
        and isinstance(item["scan"].get("received_at"), (int, float))
        and math.isfinite(float(item["scan"]["received_at"]))
    ]
    sequences = [
        item["encoder_sequence"] for item in history
        if isinstance(item, dict) and type(item.get("encoder_sequence")) is int
    ]
    return max(times, default=0.0), max(sequences, default=-1)


async def wait_fresh_fused_scan(
    get_scans: Callable[[], list[dict[str, Any]]],
    *, after_time: float, after_encoder_sequence: int,
    timeout_sec: float = 3.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_sec
    last_error = "No new independent scan and encoder observation"
    while time.monotonic() < deadline:
        history = get_scans()
        fresh: list[dict[str, Any]] = []
        for item in history:
            if not isinstance(item, dict) or not isinstance(item.get("scan"), dict):
                continue
            try:
                timestamp = float(item["scan"]["received_at"])
                sequence = item["encoder_sequence"]
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if (not math.isfinite(timestamp) or timestamp <= after_time
                or type(sequence) is not int or sequence <= after_encoder_sequence):
                continue
            fresh.append(item)
        if len(fresh) >= 3:
            try:
                return fuse_stationary_scans(fresh[-8:])
            except AutoPoseUncertain as exc:
                last_error = str(exc)
        await asyncio.sleep(0.1)
    raise AutoPoseUncertain(f"Fresh observation retry failed: {last_error}")
