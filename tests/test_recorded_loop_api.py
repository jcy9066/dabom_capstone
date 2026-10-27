"""Isolated recorder/API tests. No Pi, Nav2, camera, network or movement."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
        self.stop_started = False
        self.stop_proven = False

    def state_response(self):
        return dict(self.state)

    def recorded_stop_begin(self):
        self.stop_started = True

    def recorded_stop_acknowledged(self, delivered):
        if delivered:
            self.stop_started = True

    def recorded_stop_proven(self):
        return self.stop_started and self.stop_proven

    def _stop_confirmed_locked(self, now):
        return self.stop_proven

    def _recorded_idle_locked(self):
        return self.state.get("active_goal") is None


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

    def test_failed_recording_does_not_leave_destination_goal(self):
        self.api.begin({
            "segment_id": "ab", "source": "manual",
            "start": "A", "end": "B",
        })
        self.nav.stop_started = True
        self.nav.stop_proven = True

        with self.assertRaises(RecordedLoopError) as raised:
            self.api.finish()

        self.assertEqual(raised.exception.code, "INVALID_SEGMENT")
        snapshot = self.api.store.snapshot("testmap", self.digest)
        self.assertNotIn("B", snapshot["goals"])
        self.assertNotIn("ab", snapshot["segments"])
        self.assertTrue(self.api.status()["recording"]["active"])

    def test_new_destination_not_saved_if_endpoint_validation_fails(self):
        with self.assertRaises(RecordedLoopError) as raised:
            self.api.store.put_segment(
                "testmap", self.digest, "ab", "set_goal", "A", "B",
                [], [
                    {"x": 0.41, "y": 0.0, "yaw": 0.0},
                    {"x": 0.50, "y": 0.0, "yaw": 0.0},
                ], auto_create_end=True,
            )

        self.assertEqual(raised.exception.code, "ENDPOINT_MISMATCH")
        snapshot = self.api.store.snapshot("testmap", self.digest)
        self.assertNotIn("B", snapshot["goals"])
        self.assertNotIn("ab", snapshot["segments"])

    def test_new_destination_not_saved_if_store_write_fails(self):
        before = self.api.store.snapshot("testmap", self.digest)

        with patch.object(self.api.store, "_save", side_effect=OSError("mock disk error")):
            with self.assertRaisesRegex(OSError, "mock disk error"):
                self.api.store.put_segment(
                    "testmap", self.digest, "ab", "set_goal", "A", "B",
                    [], [
                        {"x": 0.0, "y": 0.0, "yaw": 0.0},
                        {"x": 0.2, "y": 0.0, "yaw": 0.0},
                    ], auto_create_end=True,
                )

        after = self.api.store.snapshot("testmap", self.digest)
        self.assertEqual(before, after)

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
        self.nav.stop_proven = True
        saved = self.api.finish()
        self.assertEqual(saved["segment"]["end"], "B")
        snapshot = self.api.store.snapshot("testmap", self.digest)
        self.assertIn("B", snapshot["goals"])
        self.assertGreater(len(snapshot["segments"]["ab"]["commands"]), 0)

    def test_manual_record_rejects_unconfirmed_long_ack_interval(self):
        self.api.begin({
            "segment_id": "ab", "source": "manual",
            "start": "A", "end": "B",
        })

        with patch("server.recorded_loop_api.time.monotonic", side_effect=[10.0, 10.6]):
            self.api.note_motion_ack(
                {"type": "move", "direction": "forward", "speed": 0.2},
                True,
            )
            self.api.note_motion_ack({"type": "stop"}, True)

        self.nav.stop_proven = True
        self.assertEqual(
            self.api.status()["recording"]["error"],
            "UNCONFIRMED_MOTOR_INTERVAL",
        )
        with self.assertRaises(RecordedLoopError) as raised:
            self.api.finish()
        self.assertEqual(raised.exception.code, "UNCONFIRMED_MOTOR_INTERVAL")

        snapshot = self.api.store.snapshot("testmap", self.digest)
        self.assertNotIn("ab", snapshot["segments"])
        self.assertNotIn("B", snapshot["goals"])

    def test_manual_record_refuses_ack_only_stop_without_encoder(self):
        self.api.begin({"segment_id": "ab", "source": "manual", "start": "A", "end": "B"})
        self.api.note_motion_ack(
            {"type": "move", "direction": "forward", "speed": .2}, True)
        self.maps.ros_control._amcl.x = .3
        self.maps.ros_control._amcl.received_at = time.monotonic()
        self.api._sample()
        self.api.note_motion_ack({"type": "stop"}, True)
        self.assertTrue(self.nav.stop_started)
        with self.assertRaisesRegex(RecordedLoopError, "stable wheel encoder"):
            self.api.finish()
        self.assertNotIn("ab", self.api.store.snapshot("testmap", self.digest)["segments"])

    def test_set_goal_can_be_armed_before_movement(self):
        self.nav._robot_mode = "auto"
        self.nav.state["robot_mode"] = "auto"
        self.api.begin({"segment_id": "ab", "source": "set_goal",
                        "start": "A", "end": "B"})
        self.assertTrue(self.api.status()["recording"]["armed"])
        self.api._sample()
        self.assertEqual(self.api.status()["recording"]["pose_count"], 1)
        self.nav.state["navigation_state"] = "PATH_READY"
        self.api._sample()
        self.assertIsNone(self.api.status()["recording"]["error"])
        self.assertTrue(self.api.status()["recording"]["armed"])
        self.nav.state["navigation_state"] = "NAVIGATING"
        for x in (.3, .6, .9):
            self.maps.ros_control._amcl.x = x
            self.maps.ros_control._amcl.received_at = time.monotonic()
            self.api._sample()
        self.assertFalse(self.api.status()["recording"]["armed"])
        self.nav.state["navigation_state"] = "SUCCEEDED"
        self.nav.stop_started = True
        self.nav.stop_proven = True
        result = self.api.finish()
        self.assertEqual(result["segment"]["source"], "set_goal")
        self.assertGreater(len(result["segment"]["poses"]), 2)

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

    def test_next_goal_can_be_armed_after_confirmed_success(self):
        self.nav._robot_mode = "auto"
        self.nav.state.update(robot_mode="auto", navigation_state="SUCCEEDED")
        self.nav.stop_started = self.nav.stop_proven = True
        self.api.begin({"segment_id": "ab", "source": "set_goal", "start": "A", "end": "B"})
        self.maps.ros_control._amcl.x = .1
        self.api._sample()
        self.assertTrue(self.api.status()["recording"]["armed"])
        self.assertEqual(self.api.status()["recording"]["pose_count"], 1)
        self.nav.state["navigation_state"] = "PATH_READY"
        self.api._sample()
        self.assertTrue(self.api.status()["recording"]["armed"])
        self.nav.state["navigation_state"] = "NAVIGATING"
        self.api._sample()
        self.assertFalse(self.api.status()["recording"]["armed"])
        self.assertEqual(self.api.status()["recording"]["pose_count"], 2)

    def test_success_does_not_arm_without_stop_or_while_goal_is_active(self):
        self.nav._robot_mode = "auto"
        self.nav.state.update(robot_mode="auto", navigation_state="SUCCEEDED")
        payload = {"segment_id": "ab", "source": "set_goal", "start": "A", "end": "B"}
        with self.assertRaises(RecordedLoopError):
            self.api.begin(payload)
        self.nav.stop_started = self.nav.stop_proven = True
        self.nav.state["active_goal"] = {"x": 1}
        with self.assertRaises(RecordedLoopError):
            self.api.begin(payload)


class PlaybackPermitTests(unittest.IsolatedAsyncioTestCase):
    async def test_recorded_drive_counts_only_actual_pi_motion(self):
        with tempfile.TemporaryDirectory() as dirname:
            context = {"safety_session": "pi-test", "safety_epoch": 2,
                       "goal_generation": 3}
            marked = []
            nav = SimpleNamespace(
                _robot_id="pi-01", recorded_safety_context=lambda: dict(context),
                _pi_command_delivered=lambda ack: isinstance(ack, dict)
                    and ack.get("ok") is True and ack.get("delivered") is True,
                recorded_motion_delivered=lambda permit: marked.append(permit) or True)
            sender = AsyncMock()
            api = RecordedLoopApi(
                app=FastAPI(), navigation=nav, map_api=None,
                csrf_failure=lambda _: None, store_path=Path(dirname)/"routes.json",
                send_robot_command=sender, max_wheel_mps=.212)
            command = {"direction": "forward", "speed": .2, "duration": .1, "replay_command_id": "p1"}
            sender.return_value = {"ok": True, "delivered": True, "motion_applied": False}
            self.assertIs(await api._play_drive(command), False)
            self.assertEqual(marked, [])
            sender.return_value = {"ok": True, "delivered": True}
            self.assertIs(await api._play_drive(command), False)
            sender.return_value["motion_applied"] = True
            self.assertIs(await api._play_drive(command), False)
            sender.return_value["recorded_drive"] = {"command_id": "p1", "elapsed": .05, "duration": .1}
            self.assertEqual(await api._play_drive(command), {"elapsed": .05})
            self.assertEqual(marked, [context])
            self.assertIs(sender.call_args.kwargs["motion_ack"], True)
            self.assertTrue(sender.call_args.kwargs["preflight"]())
            context["safety_epoch"] += 1
            self.assertFalse(sender.call_args.kwargs["preflight"]())

    async def test_invalid_or_wrong_command_progress_cannot_advance_replay(self):
        context = {"safety_session": "pi-test", "safety_epoch": 2, "goal_generation": 3}
        nav = SimpleNamespace(_robot_id="pi-01", recorded_safety_context=lambda: context,
            _pi_command_delivered=lambda ack: ack.get("delivered") is True,
            recorded_motion_delivered=lambda permit: True)
        sender = AsyncMock()
        with tempfile.TemporaryDirectory() as dirname:
            api = RecordedLoopApi(FastAPI(), nav, None, lambda _: None,
                Path(dirname)/"routes.json", send_robot_command=sender, max_wheel_mps=.212)
            curve = {"direction": "forward_left", "speed": .3, "duration": .08,
                     "replay_command_id": "p1"}
            for progress in (-.1, .09, True, float("nan")):
                sender.return_value = {"ok": True, "delivered": True, "motion_applied": True,
                    "recorded_drive": {"command_id": "p1", "duration": .08, "elapsed": progress}}
                self.assertIs(await api._play_drive(curve), False)
            sender.return_value["recorded_drive"] = {"command_id": "old", "duration": .08, "elapsed": .02}
            self.assertIs(await api._play_drive(curve), False)


if __name__ == "__main__":
    unittest.main()
