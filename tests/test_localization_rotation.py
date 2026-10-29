"""Safety checks for automatic Initial Pose rotation."""
import math
import time

import pytest

from server.localization_rotation import (
    UnsafeLocalizationTurn, verify_localization_turn_clearance,
)


def scan():
    return {
        "received_at": time.time(),
        "ranges": [1.0] * 360,
        "angle_min": -math.pi,
        "angle_increment": 2 * math.pi / 360,
        "range_min": 0.12,
        "range_max": 6.0,
    }


def test_clear_full_circle_accepts_preflight():
    verify_localization_turn_clearance(scan())


def test_obstacle_inside_rotation_sweep_blocks():
    sample = scan()
    sample["ranges"][10] = 0.20
    with pytest.raises(UnsafeLocalizationTurn, match="Obstacle"):
        verify_localization_turn_clearance(sample)


def test_blind_sector_blocks():
    sample = scan()
    sample["ranges"][10] = math.nan
    with pytest.raises(UnsafeLocalizationTurn, match="blind"):
        verify_localization_turn_clearance(sample)


def test_missing_circle_blocks():
    sample = scan()
    sample["ranges"] = sample["ranges"][:180]
    with pytest.raises(UnsafeLocalizationTurn, match="360-degree"):
        verify_localization_turn_clearance(sample)


def test_stale_lidar_blocks():
    sample = scan()
    sample["received_at"] -= 2.0
    with pytest.raises(UnsafeLocalizationTurn, match="360-degree"):
        verify_localization_turn_clearance(sample)
