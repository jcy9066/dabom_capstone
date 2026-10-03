"""Persistent GPS place registry and optional geofence state machine."""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

EARTH_RADIUS_M = 6_371_000.0


class LocationSecurityError(ValueError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _finite_float(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise LocationSecurityError("INVALID_LOCATION", f"{field} must be a number.")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise LocationSecurityError("INVALID_LOCATION", f"{field} must be a number.") from exc
    if not math.isfinite(number):
        raise LocationSecurityError("INVALID_LOCATION", f"{field} must be finite.")
    return number


def haversine_distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = (
        math.sin(d_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_M * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


class LocationSecurityService:
    """Manage saved places independently from live GPS availability."""

    def __init__(
        self,
        state_path: Path,
        *,
        outside_confirm_count: int = 3,
        inside_confirm_count: int = 5,
        recovery_margin_m: float = 5.0,
        max_hdop: float = 50.0,
        max_sample_age_sec: float = 5.0,
        max_future_skew_sec: float = 5.0,
    ) -> None:
        self.state_path = Path(state_path)
        self.outside_confirm_count = max(1, int(outside_confirm_count))
        self.inside_confirm_count = max(1, int(inside_confirm_count))
        self.recovery_margin_m = max(0.0, float(recovery_margin_m))
        self.max_hdop = max(0.0, float(max_hdop))
        self.max_sample_age_sec = max(0.1, float(max_sample_age_sec))
        self.max_future_skew_sec = max(0.0, float(max_future_skew_sec))
        self._lock = threading.RLock()
        self._outside_count = 0
        self._inside_count = 0
        self._live = self._empty_live()
        self._confirmed_state = "UNKNOWN"
        self._last_sample_updated_at: float | None = None

    @staticmethod
    def _empty_live() -> dict[str, Any]:
        return {
            "fix": False,
            "lat": None,
            "lng": None,
            "alt": None,
            "satellites": None,
            "hdop": None,
            "updated_at": None,
            "matched_location_id": None,
            "matched_location_name": None,
            "distance_m": None,
        }

    def list_locations(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._read_state().get("locations", [])]

    def upsert_location(
        self,
        *,
        name: Any,
        lat: Any,
        lng: Any,
        radius_m: Any,
        location_id: Any = None,
    ) -> dict[str, Any]:
        location_name = str(name or "").strip()
        if not location_name or len(location_name) > 80:
            raise LocationSecurityError("INVALID_LOCATION", "Location name must be 1-80 characters.")
        latitude = _finite_float(lat, "lat")
        longitude = _finite_float(lng, "lng")
        radius = _finite_float(radius_m, "radius_m")
        if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
            raise LocationSecurityError("INVALID_LOCATION", "Latitude or longitude is out of range.")
        if not 5.0 <= radius <= 10_000.0:
            raise LocationSecurityError("INVALID_LOCATION", "radius_m must be between 5 and 10000.")

        requested_id = str(location_id or "").strip() or None
        with self._lock:
            state = self._read_state()
            locations = list(state.get("locations", []))
            target_id = requested_id or uuid.uuid4().hex[:12]
            now = time.time()
            replacement = {
                "location_id": target_id,
                "name": location_name,
                "lat": latitude,
                "lng": longitude,
                "radius_m": radius,
                "updated_at": now,
            }
            found = False
            for index, item in enumerate(locations):
                if item.get("location_id") == target_id:
                    replacement["created_at"] = item.get("created_at", now)
                    locations[index] = replacement
                    found = True
                    break
            if not found:
                replacement["created_at"] = now
                locations.append(replacement)
            state["version"] = 1
            state["locations"] = locations
            self._write_state(state)
            return dict(replacement)

    def delete_location(self, location_id: Any) -> bool:
        target = str(location_id or "").strip()
        if not target:
            return False
        with self._lock:
            state = self._read_state()
            locations = list(state.get("locations", []))
            remaining = [item for item in locations if item.get("location_id") != target]
            if len(remaining) == len(locations):
                return False
            state["locations"] = remaining
            if state.get("last_location_id") == target:
                state["last_location_id"] = None
                state["last_location_name"] = None
            self._write_state(state)
            return True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._expire_stale_live_locked(time.time())
            state = self._read_state()
            live = dict(self._live)
            public_state = self._confirmed_state if live.get("fix") else "UNKNOWN"
            return {
                "gps": live,
                "security_state": public_state,
                "last_confirmed_security_state": self._confirmed_state,
                "outside_samples": self._outside_count,
                "inside_samples": self._inside_count,
                "last_location_id": state.get("last_location_id"),
                "last_location_name": state.get("last_location_name"),
                "locations": [dict(item) for item in state.get("locations", [])],
            }

    def update_live_gps(self, sample: dict[str, Any] | None) -> dict[str, Any]:
        """Consume one fresh GPS measurement; duplicate/out-of-order samples do not count."""
        with self._lock:
            now = time.time()
            self._expire_stale_live_locked(now)

            sample_updated_at = self._sample_updated_at(sample)
            if sample_updated_at is None:
                self._invalidate_live_locked()
                return {"transition": None, "snapshot": self.snapshot()}

            if (
                self._last_sample_updated_at is not None
                and sample_updated_at <= self._last_sample_updated_at
            ):
                return {"transition": None, "snapshot": self.snapshot()}

            age_sec = now - sample_updated_at
            if age_sec > self.max_sample_age_sec or age_sec < -self.max_future_skew_sec:
                self._invalidate_live_locked()
                return {"transition": None, "snapshot": self.snapshot()}

            # Only an in-range measurement may advance the ordering watermark.
            # A bad far-future timestamp must not poison later normal samples.
            self._last_sample_updated_at = sample_updated_at

            normalized = self._normalize_sample(sample, sample_updated_at)
            if normalized is None:
                self._invalidate_live_locked()
                return {"transition": None, "snapshot": self.snapshot()}

            locations = self._read_state().get("locations", [])
            matched, matched_distance = self._containing_location(
                normalized["lat"], normalized["lng"], locations
            )
            nearest, nearest_distance = self._nearest_location(
                normalized["lat"], normalized["lng"], locations
            )
            display_distance = matched_distance if matched is not None else nearest_distance
            normalized.update(
                {
                    "matched_location_id": matched.get("location_id") if matched else None,
                    "matched_location_name": matched.get("name") if matched else None,
                    "distance_m": round(display_distance, 2) if display_distance is not None else None,
                }
            )
            self._live = normalized

            if not locations:
                self._confirmed_state = "UNKNOWN"
                self._outside_count = 0
                self._inside_count = 0
                return {"transition": None, "snapshot": self.snapshot()}

            transition = None
            if self._confirmed_state == "OUT_OF_AREA":
                recovery_location, _ = self._containing_location(
                    normalized["lat"],
                    normalized["lng"],
                    locations,
                    recovery=True,
                )
                if recovery_location is not None:
                    self._inside_count += 1
                    self._outside_count = 0
                    if self._inside_count >= self.inside_confirm_count:
                        self._confirmed_state = "NORMAL"
                        transition = "NORMAL"
                        self._inside_count = 0
                        self._remember_location(recovery_location)
                else:
                    self._inside_count = 0
            elif matched is not None:
                self._inside_count += 1
                self._outside_count = 0
                if self._confirmed_state != "NORMAL":
                    self._confirmed_state = "NORMAL"
                self._remember_location(matched)
            else:
                self._outside_count += 1
                self._inside_count = 0
                if self._outside_count >= self.outside_confirm_count:
                    if self._confirmed_state != "OUT_OF_AREA":
                        transition = "OUT_OF_AREA"
                    self._confirmed_state = "OUT_OF_AREA"
                    self._outside_count = 0

            return {"transition": transition, "snapshot": self.snapshot()}

    def current_fix(self) -> dict[str, Any] | None:
        with self._lock:
            self._expire_stale_live_locked(time.time())
            return dict(self._live) if self._live.get("fix") else None

    @staticmethod
    def _sample_updated_at(sample: dict[str, Any] | None) -> float | None:
        if not isinstance(sample, dict):
            return None
        value = sample.get("updated_at")
        if isinstance(value, bool):
            return None
        try:
            result = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return result if math.isfinite(result) else None

    def _normalize_sample(
        self,
        sample: dict[str, Any] | None,
        sample_updated_at: float,
    ) -> dict[str, Any] | None:
        if not isinstance(sample, dict) or sample.get("fix") is not True:
            return None
        try:
            lat = float(sample.get("lat"))
            lng = float(sample.get("lng"))
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(lat) or not math.isfinite(lng):
            return None
        if not -90.0 <= lat <= 90.0 or not -180.0 <= lng <= 180.0:
            return None

        hdop = sample.get("hdop")
        try:
            hdop = None if hdop is None else float(hdop)
        except (TypeError, ValueError, OverflowError):
            hdop = None
        if hdop is not None and (not math.isfinite(hdop) or hdop > self.max_hdop):
            return None

        def optional_float(value: Any) -> float | None:
            try:
                result = float(value)
            except (TypeError, ValueError, OverflowError):
                return None
            return result if math.isfinite(result) else None

        satellites = sample.get("satellites")
        try:
            satellites = None if satellites is None else max(0, int(satellites))
        except (TypeError, ValueError, OverflowError):
            satellites = None

        return {
            "fix": True,
            "lat": lat,
            "lng": lng,
            "alt": optional_float(sample.get("alt")),
            "satellites": satellites,
            "hdop": hdop,
            "updated_at": sample_updated_at,
        }

    def _expire_stale_live_locked(self, now: float) -> None:
        if not self._live.get("fix"):
            return
        updated_at = self._sample_updated_at(self._live)
        if updated_at is None or now - updated_at > self.max_sample_age_sec:
            self._invalidate_live_locked()

    def _invalidate_live_locked(self) -> None:
        self._live = self._empty_live()
        self._outside_count = 0
        self._inside_count = 0

    def _containing_location(
        self,
        lat: float,
        lng: float,
        locations: list[dict[str, Any]],
        *,
        recovery: bool = False,
    ):
        candidates: list[tuple[float, dict[str, Any]]] = []
        for location in locations:
            try:
                distance = haversine_distance_m(
                    lat,
                    lng,
                    float(location["lat"]),
                    float(location["lng"]),
                )
                radius = float(location["radius_m"])
            except (KeyError, TypeError, ValueError):
                continue
            threshold = max(0.0, radius - self.recovery_margin_m) if recovery else radius
            if distance <= threshold:
                candidates.append((distance, location))
        if not candidates:
            return None, None
        distance, location = min(candidates, key=lambda item: item[0])
        return location, distance

    @staticmethod
    def _nearest_location(lat: float, lng: float, locations: list[dict[str, Any]]):
        nearest = None
        nearest_distance = None
        for location in locations:
            try:
                distance = haversine_distance_m(
                    lat,
                    lng,
                    float(location["lat"]),
                    float(location["lng"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if nearest_distance is None or distance < nearest_distance:
                nearest = location
                nearest_distance = distance
        return nearest, nearest_distance

    def _remember_location(self, location: dict[str, Any]) -> None:
        state = self._read_state()
        location_id = location.get("location_id")
        location_name = location.get("name")
        if state.get("last_location_id") == location_id and state.get("last_location_name") == location_name:
            return
        state["last_location_id"] = location_id
        state["last_location_name"] = location_name
        self._write_state(state)

    def _read_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "locations": []}
        if not isinstance(payload, dict) or not isinstance(payload.get("locations", []), list):
            return {"version": 1, "locations": []}
        return payload

    def _write_state(self, payload: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.state_path.name}.", suffix=".tmp", dir=self.state_path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary_name, self.state_path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
