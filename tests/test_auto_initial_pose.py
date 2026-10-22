import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import json

from server.auto_initial_pose import AutoPoseUncertain, decode_saved_cells, estimate_initial_pose


class AutoPoseSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "raw.json"
        self.saved = SimpleNamespace(raw_path=self.path, width=20, height=20,
            resolution=0.1, origin_x=0.0, origin_y=0.0, origin_yaw=0.0)
        self.path.write_text(json.dumps({"data": [0] * 400}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_raw_grid_decode(self):
        self.assertEqual(400, len(decode_saved_cells(self.path, 20, 20)))

    def test_rle_grid_decode(self):
        self.path.write_text(json.dumps({"data_encoding": "rle", "data": [[0, 399], [100, 1]]}))
        self.assertEqual(100, decode_saved_cells(self.path, 20, 20)[-1])

    def test_invalid_rle_rejected(self):
        self.path.write_text(json.dumps({"data_encoding": "rle", "data": [[0, 401]]}))
        with self.assertRaises(AutoPoseUncertain):
            decode_saved_cells(self.path, 20, 20)

    def test_stale_scan_rejected(self):
        scan = {"received_at": 1, "angle_min": -math.pi,
            "angle_increment": 0.1, "range_min": 0.1, "range_max": 8,
            "ranges": [2.0] * 50}
        with self.assertRaisesRegex(AutoPoseUncertain, "stale"):
            estimate_initial_pose(self.saved, scan, now=10)

    def test_no_obstacle_map_never_passes(self):
        scan = {"received_at": 100, "angle_min": -math.pi,
            "angle_increment": 0.1, "range_min": 0.1, "range_max": 8,
            "ranges": [1.0] * 50}
        with self.assertRaises(AutoPoseUncertain):
            estimate_initial_pose(self.saved, scan, now=100.1)

    def test_missing_scan_rejected(self):
        with self.assertRaises(AutoPoseUncertain):
            estimate_initial_pose(self.saved, {}, now=10)




    def test_mismatched_mount_requires_manual_fallback(self):
        scan = {"received_at": 100, "angle_min": -math.pi,
                "angle_increment": 0.1, "range_min": 0.1, "range_max": 8,
                "ranges": [1.0] * 50,
                "mount": {"x": 0.0, "y": 0.0, "yaw": 0.0}}
        with self.assertRaisesRegex(AutoPoseUncertain, "extrinsics disagree"):
            estimate_initial_pose(self.saved, scan, mount_yaw=math.pi, now=100)

    def test_no_obstacles_rejected_before_search(self):
        scan = {"received_at": 100, "angle_min": -math.pi,
                "angle_increment": 0.1, "range_min": 0.1, "range_max": 8,
                "ranges": [1.0] * 50}
        with self.assertRaisesRegex(AutoPoseUncertain, "insufficient confirmed obstacles"):
            estimate_initial_pose(self.saved, scan, now=100)

    def test_malformed_mount_is_rejected(self):
        scan = {"received_at": 100, "angle_min": -math.pi,
                "angle_increment": 0.1, "range_min": 0.1, "range_max": 8,
                "ranges": [1.0] * 50,
                "mount": {"x": None, "y": 0.0, "yaw": 0.0}}
        with self.assertRaisesRegex(AutoPoseUncertain, "mount transform malformed"):
            estimate_initial_pose(self.saved, scan, now=100)



if __name__ == "__main__":
    unittest.main()
