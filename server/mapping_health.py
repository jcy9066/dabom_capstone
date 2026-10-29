"""Fail-closed Mapping readiness from the current ROS health probe."""
from __future__ import annotations

import math
from typing import Any


def mapping_health_state(
    *,
    mode: str | None,
    probe: dict[str, Any] | None,
    probe_received_at: float | None,
    scan_updated_at: float | None,
    map_payload: dict[str, Any] | None,
    map_updated_at: float | None,
    now: float,
    process_running: bool | None = None,
) -> dict[str, Any]:
    def age(at):
        if at is None:
            return None
        try:
            value = float(at)
        except (TypeError, ValueError):
            return None
        return max(0.0, now - value) if math.isfinite(value) else None

    probe = probe if isinstance(probe, dict) else {}
    fresh_probe = (
        age(probe_received_at) is not None
        and age(probe_received_at) <= 3.0
    )
    scan_age = age(scan_updated_at)
    scan_ok = bool(scan_age is not None and scan_age <= 3.0 and fresh_probe
                   and isinstance(probe.get("scan_age_sec"), (int, float))
                   and 0 <= probe["scan_age_sec"] <= 3.0)
    odom_age = probe.get("odom_age_sec") if fresh_probe else None
    odom_ok = bool(isinstance(odom_age, (int, float))
                   and math.isfinite(odom_age) and 0 <= odom_age <= 3.0)
    tf = probe.get("tf") if fresh_probe else {}
    tf = tf if isinstance(tf, dict) else {}
    tf_ok = all(tf.get(k) is True for k in (
        "odom_to_base", "base_to_laser", "map_to_odom",
    ))
    lifecycle = str(probe.get("slam_lifecycle") or "unknown").lower() if fresh_probe else "unknown"
    verified = bool(
        isinstance(map_payload, dict)
        and map_payload.get("source") == "slam_toolbox"
        and map_payload.get("map_verified") is True
        and map_payload.get("preview") is not True
    )
    map_age = age(map_updated_at)
    map_current = bool(verified and map_age is not None and map_age <= 5.0)
    # The presence of a process or a Preview does not prove SLAM readiness.
    ready = scan_ok and odom_ok and tf_ok and lifecycle == "active" and map_current
    if process_running is False:
        state = "MAPPING_ERROR" if not verified else "MAPPING_DEGRADED"
        ready = False
    elif ready:
        state = "MAPPING_ACTIVE"
    elif verified:
        state = "MAPPING_DEGRADED"
    elif not scan_ok:
        state = "WAITING_FOR_SCAN"
    elif not odom_ok or not tf_ok:
        state = "WAITING_FOR_ODOM_TF"
    else:
        state = "SLAM_INITIALIZING"
    if str(mode or "").lower() != "mapping":
        state = "MAPPING_INACTIVE"
        ready = False
    return {
        "mapping_state": state,
        "mapping_health": {
            "process_running": process_running,
            "probe_fresh": fresh_probe,
            "scan": {"ready": scan_ok, "age_sec": scan_age},
            "odom": {"ready": odom_ok, "age_sec": odom_age},
            "tf": {
                "odom_to_base": tf.get("odom_to_base") is True,
                "base_to_laser": tf.get("base_to_laser") is True,
                "map_to_odom": tf.get("map_to_odom") is True,
            },
            "slam": {"lifecycle": lifecycle},
            "slam_map": {"received": verified, "verified": verified, "age_sec": map_age},
        },
        "map_saveable": verified,
        # Final DRIVING verification additionally requires Pi/AMCL/Nav2 gates.
        "driving_preflight_ready": False,
        "mapping_error_code": None if ready else state,
    }
