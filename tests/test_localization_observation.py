"""Fresh Initial Pose retry uses new, independent LiDAR/encoder evidence."""
import asyncio
import math
import time

import pytest

from server.auto_initial_pose import AutoPoseUncertain
from server.localization_observation import observation_baseline, wait_fresh_fused_scan


def samples():
    now = time.time()
    output = []
    for idx, offset in enumerate((0.65, 0.4, 0.1), 1):
        when = now - offset
        output.append({
            "encoder_sequence": idx,
            "wheel_ticks": [0, 0, 0, 0],
            "encoder_received_at": when - 0.04,
            "scan": {
                "received_at": when,
                "angle_min": -math.pi,
                "angle_increment": 2 * math.pi / 360,
                "range_min": 0.12,
                "range_max": 6.0,
                "ranges": [1.5] * 360,
            },
        })
    return output


def test_baseline_captures_latest_independent_observation():
    old = samples()
    timestamp, sequence = observation_baseline(old)
    assert timestamp == old[-1]["scan"]["received_at"]
    assert sequence == 3


def test_retry_accepts_three_independent_new_samples():
    frames = samples()
    result = asyncio.run(wait_fresh_fused_scan(
        lambda: frames, after_time=0.0, after_encoder_sequence=0,
        timeout_sec=0.5,
    ))
    assert result["frames_fused"] == 3


def test_retry_never_reuses_previous_encoder_sequence():
    frames = samples()
    with pytest.raises(AutoPoseUncertain, match="Fresh observation retry failed"):
        asyncio.run(wait_fresh_fused_scan(
            lambda: frames,
            after_time=frames[-1]["scan"]["received_at"],
            after_encoder_sequence=3,
            timeout_sec=0.2,
        ))
