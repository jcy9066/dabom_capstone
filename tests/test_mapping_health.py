"""Mapping readiness must distinguish process liveness and SLAM evidence."""
from server.mapping_health import mapping_health_state

MAP = {"source": "slam_toolbox", "map_verified": True, "preview": False}
PROBE = {
    "scan_age_sec": 0.1, "odom_age_sec": 0.1,
    "tf": {"odom_to_base": True, "base_to_laser": True, "map_to_odom": True},
    "slam_lifecycle": "active",
}


def snapshot(now=100.0, *, probe=PROBE, source_map=MAP, scan_at=99.8,
             map_at=99.7, probe_at=99.8, process=True,
             discontinuity=False):
    return mapping_health_state(
        mode="mapping", probe=probe, probe_received_at=probe_at,
        scan_updated_at=scan_at, map_payload=source_map,
        map_updated_at=map_at, now=now, process_running=process,
        odometry_continuity_lost=discontinuity,
    )


def test_no_scan_is_not_mapping_active():
    result = snapshot(scan_at=None)
    assert result["mapping_state"] == "MAPPING_DEGRADED"
    assert result["map_saveable"] is True


def test_preview_cannot_authorize_save_or_driving():
    preview = {"source": "live_mapping_grid", "map_verified": False, "preview": True}
    result = snapshot(source_map=preview)
    assert result["mapping_state"] != "MAPPING_ACTIVE"
    assert result["map_saveable"] is False
    assert result["driving_preflight_ready"] is False


def test_inactive_slam_is_not_ready():
    result = snapshot(probe={**PROBE, "slam_lifecycle": "inactive"})
    assert result["mapping_state"] == "MAPPING_DEGRADED"


def test_sensor_pause_and_recovery_without_new_session():
    healthy = snapshot()
    broken = snapshot(now=104.0)
    recovered = snapshot(now=105.0, scan_at=104.8, map_at=104.7, probe_at=104.8)
    assert healthy["mapping_state"] == "MAPPING_ACTIVE"
    assert broken["mapping_state"] == "MAPPING_DEGRADED"
    assert broken["map_saveable"] is True
    assert recovered["mapping_state"] == "MAPPING_ACTIVE"


def test_encoder_reset_blocks_automatic_continuation():
    result = snapshot(discontinuity=True)
    assert result["mapping_state"] == "MAPPING_ERROR"
    assert result["map_saveable"] is True
    assert result["driving_preflight_ready"] is False


def test_missing_tf_blocks_mapping():
    result = snapshot(probe={**PROBE, "tf": {**PROBE["tf"], "map_to_odom": False}})
    assert result["mapping_state"] != "MAPPING_ACTIVE"


def test_process_liveness_cannot_replace_slam_readiness():
    result = snapshot(source_map=None, process=True)
    assert result["mapping_state"] == "SLAM_INITIALIZING"
    assert result["map_saveable"] is False
