"""Formatting helpers for GPS security transition notifications."""

from __future__ import annotations

from typing import Any


def build_location_transition_message(result: dict[str, Any]) -> str | None:
    transition = result.get("transition")
    snapshot = result.get("snapshot") if isinstance(result.get("snapshot"), dict) else {}
    gps = snapshot.get("gps") if isinstance(snapshot.get("gps"), dict) else {}
    place = gps.get("matched_location_name") or snapshot.get("last_location_name") or "등록 장소"

    lat = gps.get("lat")
    lng = gps.get("lng")
    distance = gps.get("distance_m")
    coords = ""
    try:
        coords = f"\nGPS: {float(lat):.6f}, {float(lng):.6f}"
    except (TypeError, ValueError, OverflowError):
        pass

    if transition == "OUT_OF_AREA":
        distance_text = ""
        try:
            distance_text = f"\n기준점 거리: {float(distance):.1f} m"
        except (TypeError, ValueError, OverflowError):
            pass
        return f"⚠ RC카 구역 이탈\n기준 장소: {place}{distance_text}{coords}"

    if transition == "NORMAL":
        return f"RC카 정상 구역 복귀\n장소: {place}{coords}"

    return None
