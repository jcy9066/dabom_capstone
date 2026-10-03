import json
import tempfile
import unittest
from pathlib import Path

from server.navigation_map_service import NavigationMapService


class NavigationMapOptionalLocationMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.maps = self.root / "navigation" / "maps"
        self.maps.mkdir(parents=True)
        self.service = NavigationMapService(self.root, self.maps)

    def tearDown(self):
        self.tmp.cleanup()

    def write_map(self, name, location_marker=...):
        pgm = self.maps / f"{name}.pgm"
        yaml = self.maps / f"{name}.yaml"
        raw = self.maps / f"{name}.raw.json"
        meta = self.maps / f"{name}.meta.json"
        pgm.write_bytes(b"P5\n1 1\n255\n\x00")
        yaml.write_text(
            f"image: {name}.pgm\nresolution: 0.05\norigin: [0, 0, 0]\n",
            encoding="utf-8",
        )
        raw.write_text(json.dumps({"width": 1, "height": 1}), encoding="utf-8")
        payload = {
            "map_name": name,
            "saved_at_iso": "2026-10-01T00:00:00Z",
            "width": 1,
            "height": 1,
            "resolution": 0.05,
            "origin": {"x": 0, "y": 0, "yaw": 0},
            "files": {
                "pgm": f"navigation/maps/{name}.pgm",
                "yaml": f"navigation/maps/{name}.yaml",
                "raw": f"navigation/maps/{name}.raw.json",
                "meta": f"navigation/maps/{name}.meta.json",
            },
        }
        if location_marker is not ...:
            payload["location"] = location_marker
        meta.write_text(json.dumps(payload), encoding="utf-8")

    def test_map_without_location_metadata_remains_valid(self):
        self.write_map("legacy")
        saved = self.service.get_map("legacy")
        self.assertIsNone(saved.location)
        self.assertIsNone(saved.public_dict(self.root)["location"])

    def test_valid_location_metadata_is_exposed(self):
        location = {
            "location_id": "lab",
            "name": "CtrlCV Lab",
            "gps": {"lat": 37.0, "lng": 127.0},
        }
        self.write_map("located", location)
        saved = self.service.get_map("located")
        self.assertEqual("CtrlCV Lab", saved.public_dict(self.root)["location"]["name"])

    def test_malformed_optional_location_does_not_invalidate_map(self):
        self.write_map("malformed", "not-an-object")
        saved = self.service.get_map("malformed")
        self.assertIsNone(saved.location)


if __name__ == "__main__":
    unittest.main()
