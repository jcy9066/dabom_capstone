"""Isolated recorded-route tests; no physical motors, ROS or Pi required."""
import tempfile
import unittest
from pathlib import Path

from server.recorded_loop_core import RecordedRouteStore, RecordedLoopError, map_digest


class RecordedLoopStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "m.pgm").write_bytes(b"P5\n1 1\n255\n\x00")
        (self.root / "m.yaml").write_text("image: m.pgm\nresolution: 0.1\n")
        self.digest = map_digest(self.root / "m.yaml")
        self.store = RecordedRouteStore(self.root / "runtime" / "paths.json")
        self.store.put_goal("m", self.digest, "A", {"x": 0, "y": 0, "yaw": 0})
        self.store.put_goal("m", self.digest, "B", {"x": 1, "y": 0, "yaw": 0})

    def test_closed_route_persists(self):
        ab = [{"x": x, "y": 0, "yaw": 0} for x in (0, .3, .6, 1)]
        ba = list(reversed(ab))
        cmds = [{"direction": "forward", "speed": .2, "duration": .3}]
        self.store.put_segment("m", self.digest, "ab", "manual", "A", "B", cmds, ab)
        self.store.put_segment("m", self.digest, "ba", "manual", "B", "A", cmds, ba)
        self.store.put_route("m", self.digest, "roundtrip", ["ab", "ba"])
        data = RecordedRouteStore(self.store.path).snapshot("m", self.digest)
        self.assertEqual(data["routes"]["roundtrip"]["segments"], ["ab", "ba"])
        with self.assertRaises(RecordedLoopError):
            self.store.delete("m", self.digest, "segments", "ab")

    def test_reject_changed_map(self):
        (self.root / "m.pgm").write_bytes(b"changed")
        with self.assertRaisesRegex(RecordedLoopError, "Map content changed"):
            self.store.snapshot("m", map_digest(self.root / "m.yaml"))

    def test_reject_pwm_outside_limit(self):
        with self.assertRaises(RecordedLoopError):
            self.store.put_segment(
                "m", self.digest, "ab", "manual", "A", "B",
                [{"direction": "forward", "speed": 1, "duration": .2}],
                [{"x": 0, "y": 0, "yaw": 0}, {"x": 1, "y": 0, "yaw": 0}],
            )

    def test_reject_location_jump(self):
        with self.assertRaises(RecordedLoopError) as ctx:
            self.store.put_segment(
                "m", self.digest, "ab", "manual", "A", "B",
                [{"direction": "forward", "speed": .2, "duration": .2}],
                [{"x": 0, "y": 0, "yaw": 0}, {"x": 1, "y": 0, "yaw": 0}],
            )
        self.assertEqual(ctx.exception.code, "LOCALIZATION_JUMP")


if __name__ == "__main__":
    unittest.main()
