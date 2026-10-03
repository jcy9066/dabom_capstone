import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from server.location_security import LocationSecurityError, LocationSecurityService


class LocationSecurityServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "locations.json"
        self.service = LocationSecurityService(self.path)
        self.location = self.service.upsert_location(
            name="CtrlCV Lab", lat=37.0, lng=127.0, radius_m=30
        )
        self._sample_time = time.time() - 1.0

    def tearDown(self):
        self.tmp.cleanup()

    def sample(self, lat=37.0, lng=127.0, *, updated_at=None, fix=True, hdop=1.2):
        if updated_at is None:
            self._sample_time += 0.1
            updated_at = self._sample_time
        return {
            "fix": fix,
            "lat": lat,
            "lng": lng,
            "hdop": hdop,
            "satellites": 8,
            "updated_at": updated_at,
        }

    def test_saved_locations_survive_without_live_fix(self):
        self.service.update_live_gps(None)
        snapshot = self.service.snapshot()
        self.assertEqual("UNKNOWN", snapshot["security_state"])
        self.assertEqual("CtrlCV Lab", snapshot["locations"][0]["name"])

    def test_inside_location_identifies_place(self):
        result = self.service.update_live_gps(self.sample())
        self.assertEqual("NORMAL", result["snapshot"]["security_state"])
        self.assertEqual("CtrlCV Lab", result["snapshot"]["gps"]["matched_location_name"])

    def test_outside_requires_three_consecutive_fresh_samples(self):
        for _ in range(2):
            self.assertIsNone(
                self.service.update_live_gps(self.sample(lat=37.001, lng=127.0))["transition"]
            )
        result = self.service.update_live_gps(self.sample(lat=37.001, lng=127.0))
        self.assertEqual("OUT_OF_AREA", result["transition"])

    def test_duplicate_sample_does_not_advance_departure_counter(self):
        far = self.sample(lat=37.001, lng=127.0)
        first = self.service.update_live_gps(far)
        second = self.service.update_live_gps(far)
        third = self.service.update_live_gps(far)
        self.assertEqual(1, first["snapshot"]["outside_samples"])
        self.assertEqual(1, second["snapshot"]["outside_samples"])
        self.assertEqual(1, third["snapshot"]["outside_samples"])
        self.assertIsNone(third["transition"])

    def test_stale_samples_never_count_toward_departure(self):
        old = time.time() - 30.0
        for offset in (0.0, 0.1, 0.2):
            result = self.service.update_live_gps(
                self.sample(lat=37.001, lng=127.0, updated_at=old + offset)
            )
            self.assertEqual("UNKNOWN", result["snapshot"]["security_state"])
            self.assertEqual(0, result["snapshot"]["outside_samples"])

    def test_rejected_future_timestamp_does_not_block_normal_samples(self):
        service = LocationSecurityService(self.path, max_sample_age_sec=5.0, max_future_skew_sec=5.0)
        with patch("server.location_security.time.time", return_value=1000.0):
            rejected = service.update_live_gps(
                {"fix": True, "lat": 37.001, "lng": 127.0, "hdop": 1.0,
                 "satellites": 8, "updated_at": 4600.0}
            )
            self.assertEqual("UNKNOWN", rejected["snapshot"]["security_state"])
            self.assertEqual(0, rejected["snapshot"]["outside_samples"])

            for updated_at in (1001.0, 1002.0):
                result = service.update_live_gps(
                    {"fix": True, "lat": 37.001, "lng": 127.0, "hdop": 1.0,
                     "satellites": 8, "updated_at": updated_at}
                )
                self.assertIsNone(result["transition"])

            recovered = service.update_live_gps(
                {"fix": True, "lat": 37.001, "lng": 127.0, "hdop": 1.0,
                 "satellites": 8, "updated_at": 1003.0}
            )

        self.assertEqual("OUT_OF_AREA", recovered["transition"])
        self.assertEqual("OUT_OF_AREA", recovered["snapshot"]["security_state"])

    def test_live_fix_expires_to_unknown_without_deleting_saved_locations(self):
        now = 1000.0
        service = LocationSecurityService(self.path, max_sample_age_sec=5.0)
        with patch("server.location_security.time.time", return_value=now):
            result = service.update_live_gps(
                {"fix": True, "lat": 37.0, "lng": 127.0, "hdop": 1.0,
                 "satellites": 8, "updated_at": now}
            )
            self.assertEqual("NORMAL", result["snapshot"]["security_state"])
        with patch("server.location_security.time.time", return_value=now + 6.0):
            snapshot = service.snapshot()
        self.assertEqual("UNKNOWN", snapshot["security_state"])
        self.assertFalse(snapshot["gps"]["fix"])
        self.assertEqual("CtrlCV Lab", snapshot["locations"][0]["name"])

    def test_no_fix_never_counts_as_outside(self):
        self.service.update_live_gps(self.sample(lat=37.001, lng=127.0))
        self.service.update_live_gps(self.sample(lat=37.001, lng=127.0))
        self.service.update_live_gps(self.sample(fix=False))
        result = self.service.update_live_gps(self.sample(lat=37.001, lng=127.0))
        self.assertIsNone(result["transition"])

    def test_recovery_requires_five_consecutive_inside_samples(self):
        for _ in range(3):
            self.service.update_live_gps(self.sample(lat=37.001, lng=127.0))
        for _ in range(4):
            self.assertIsNone(self.service.update_live_gps(self.sample())["transition"])
        self.assertEqual("NORMAL", self.service.update_live_gps(self.sample())["transition"])

    def test_any_registered_zone_can_make_position_inside(self):
        self.service.upsert_location(
            location_id=self.location["location_id"],
            name="Small",
            lat=37.0,
            lng=127.0,
            radius_m=10,
        )
        self.service.upsert_location(
            name="Large",
            lat=37.0005,
            lng=127.0,
            radius_m=100,
        )
        result = self.service.update_live_gps(self.sample(lat=37.0002, lng=127.0))
        self.assertEqual("NORMAL", result["snapshot"]["security_state"])
        self.assertEqual("Large", result["snapshot"]["gps"]["matched_location_name"])

    def test_recovery_checks_all_registered_zone_radii(self):
        self.service.upsert_location(
            location_id=self.location["location_id"],
            name="Small",
            lat=37.0,
            lng=127.0,
            radius_m=10,
        )
        self.service.upsert_location(
            name="Large",
            lat=37.0005,
            lng=127.0,
            radius_m=100,
        )
        for _ in range(3):
            self.service.update_live_gps(self.sample(lat=37.003, lng=127.0))
        for _ in range(4):
            self.assertIsNone(
                self.service.update_live_gps(self.sample(lat=37.0002, lng=127.0))["transition"]
            )
        result = self.service.update_live_gps(self.sample(lat=37.0002, lng=127.0))
        self.assertEqual("NORMAL", result["transition"])

    def test_bad_hdop_is_treated_as_no_fix(self):
        result = self.service.update_live_gps(self.sample(hdop=99))
        self.assertEqual("UNKNOWN", result["snapshot"]["security_state"])

    def test_missing_measurement_time_is_not_live_fix(self):
        result = self.service.update_live_gps(
            {"fix": True, "lat": 37.0, "lng": 127.0, "hdop": 1.0}
        )
        self.assertEqual("UNKNOWN", result["snapshot"]["security_state"])

    def test_location_edit_works_without_gps(self):
        edited = self.service.upsert_location(
            location_id=self.location["location_id"],
            name="CtrlCV Lab 2",
            lat=37.1,
            lng=127.1,
            radius_m=40,
        )
        self.assertEqual("CtrlCV Lab 2", edited["name"])
        self.assertEqual(1, len(self.service.list_locations()))

    def test_invalid_location_is_rejected(self):
        with self.assertRaises(LocationSecurityError):
            self.service.upsert_location(name="", lat=37, lng=127, radius_m=30)


if __name__ == "__main__":
    unittest.main()
