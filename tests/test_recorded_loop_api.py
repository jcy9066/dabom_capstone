"""Isolated recorder/API tests. No Pi, Nav2, camera, network or movement."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI

from server.recorded_loop_api import RecordedLoopApi
from server.recorded_loop_core import RecordedLoopError, map_digest


class FakeNav:
    def __init__(self):
        self.state = {"navigation_mode": "DRIVING", "localization_ready": True,
                      "active_map": {"map_name": "testmap"},
                      "robot_mode": "manual", "emergency_stop": False,
                      "navigation_state": "READY"}
        self._state = self.state
        self._robot_mode = "manual"
        self._lock = threading.RLock()

    def state_response(self):
        return dict(self.state)


class FakeRos:
    def __init__(self):
        self._lock = threading.RLock()
        self._amcl = SimpleNamespace(x=0.0, y=0.0, yaw=0.0,
                                     received_at=time.monotonic())


class FakeMapApi:
    def __init__(self, yaml):
        self.yaml = yaml
        self.ros_control = FakeRos()

    def resolve_map(self, name):
        if name != "testmap":
            raise AssertionError("wrong map")
        return SimpleNamespace(yaml_path=self.yaml)


class RecordedLoopApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "testmap.pgm").write_bytes(b"P5\n1 1\n255\n\x00")
        yaml = root / "testmap.yaml"
        yaml.write_text("image: testmap.pgm\nresolution: 0.05\n")
        self.nav = FakeNav()
        self.maps = FakeMapApi(yaml)
        self.api = RecordedLoopApi(
            app=FastAPI(), navigation=self.nav,
            map_api=self.maps, csrf_failure=lambda _: None,
            store_path=root / "routes.json",
        )
        self.digest = map_digest(yaml)
        self.api.store.put_goal("testmap", self.digest, "A",
                                {"x": 0.0, "y": 0.0, "yaw": 0.0})

    def test_manual_record_with_auto_destination(self):
        self.api.begin({"segment_id": "ab", "source": "manual", "start": "A", "end": "B"})
        self.api.note_motion_ack(
            {"type": "move", "direction": "forward", "speed": 0.2}, True)
        for x in (0.25, 0.50, 0.75):
            time.sleep(.012)
            self.maps.ros_control._amcl.x = x
            self.maps.ros_control._amcl.received_at = time.monotonic()
            self.api._sample()
            self.api.note_motion_ack(
                {"type": "move", "direction": "forward", "speed": 0.2}, True)
        self.api.note_motion_ack({"type": "stop"}, True)
        saved = self.api.finish()
        self.assertEqual(saved["segment"]["end"], "B")
        snapshot = self.api.store.snapshot("testmap", self.digest)
        self.assertIn("B", snapshot["goals"])
        self.assertGreater(len(snapshot["segments"]["ab"]["commands"]), 0)

    def test_no_record_on_unacknowledged_drive(self):
        self.api.begin({"segment_id": "ab", "source": "manual", "start": "A", "end": "B"})
        self.api.note_motion_ack(
            {"type": "move", "direction": "forward", "speed": .2}, False)
        self.assertEqual(self.api.status()["recording"]["command_count"], 0)

    def test_source_or_mode_change_invalidates_trace(self):
        self.api.begin({"segment_id": "ab", "source": "manual", "start": "A", "end": "B"})
        self.nav.state["robot_mode"] = "auto"
        self.nav._robot_mode = "auto"
        self.api._sample()
        with self.assertRaises(RecordedLoopError):
            self.api.finish()


if __name__ == "__main__":
    unittest.main()
