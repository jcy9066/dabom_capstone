import tempfile
import unittest
from pathlib import Path

from server.location_security import LocationSecurityError, LocationSecurityService


class LocationSecurityServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "locations.json"
        self.service = LocationSecurityService(self.path)
        self.location = self.service.upsert_location(
            name="CtrlCV Lab", lat=37.0, lng=127.0, radius_m=30
        )

    def tearDown(self):
        self.tmp.cleanup()

    def sample(self, lat=37.0, lng=127.0):
        return {"fix": True, "lat": lat, "lng": lng, "hdop": 1.2}

    def test_saved_locations_survive_without_live_fix(self):
        self.service.update_live_gps(None)
        snapshot = self.service.snapshot()
        self.assertEqual("UNKNOWN", snapshot["security_state"])
        self.assertEqual("CtrlCV Lab", snapshot["locations"][0]["name"])

    def test_inside_location_identifies_place(self):
        result = self.service.update_live_gps(self.sample())
        self.assertEqual("NORMAL", result["snapshot"]["security_state"])
        self.assertEqual("CtrlCV Lab", result["snapshot"]["gps"]["matched_location_name"])

    def test_outside_requires_three_consecutive_samples(self):
        far = self.sample(lat=37.001, lng=127.0)
        self.assertIsNone(self.service.update_live_gps(far)["transition"])
        self.assertIsNone(self.service.update_live_gps(far)["transition"])
        result = self.service.update_live_gps(far)
        self.assertEqual("OUT_OF_AREA", result["transition"])
        self.assertEqual("OUT_OF_AREA", result["snapshot"]["security_state"])

    def test_no_fix_never_counts_as_outside(self):
        far = self.sample(lat=37.001, lng=127.0)
        self.service.update_live_gps(far)
        self.service.update_live_gps(far)
        self.service.update_live_gps(None)
        result = self.service.update_live_gps(far)
        self.assertIsNone(result["transition"])

    def test_recovery_requires_five_consecutive_inside_samples(self):
        far = self.sample(lat=37.001, lng=127.0)
        for _ in range(3):
            self.service.update_live_gps(far)
        for _ in range(4):
            self.assertIsNone(self.service.update_live_gps(self.sample())["transition"])
        self.assertEqual("NORMAL", self.service.update_live_gps(self.sample())["transition"])

    def test_bad_hdop_is_treated_as_no_fix(self):
        result = self.service.update_live_gps(
            {"fix": True, "lat": 37.0, "lng": 127.0, "hdop": 99}
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
