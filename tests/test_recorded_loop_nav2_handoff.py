"""Nav2 takeover / Recorded Path callbacks: no real Pi or ROS required."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI

from server.recorded_loop_api import RecordedLoopApi
from server.recorded_loop_executor import ReplayFailure
from server.recorded_loop_runtime import Playback


class FakeROS:
    def __init__(self):
        self.events = []
        self.state = "IDLE"
        self.valid = True

    def compute_path(self, goal, *, loop_forward=False):
        self.events.append("compute")
        assert loop_forward
        return [{"x": 0.0, "y": 0.0, "yaw": 0.0}, goal]

    def loop_path_valid(self, path):
        self.events.append("validate")
        return self.valid

    def follow_loop_path(self, path, *, recorded_preflight=None):
        assert recorded_preflight is None or recorded_preflight()
        self.events.append("follow")
        self.state = "SUCCEEDED"
        return {"accepted": True}

    def navigation_status(self):
        return {"state": self.state}

    def cancel_navigation(self):
        self.events.append("cancel")
        self.state = "CANCELED"
        return {"confirmed": True}


class FakeNav:
    def __init__(self):
        self.events = []
        self.nav_owned = False
        self.replay_owned = True

    def recorded_stop_proven(self):
        return True

    def recorded_nav2_takeover(self):
        if not self.replay_owned:
            raise AssertionError("No motor owner")
        self.events.append("takeover")
        self.replay_owned = False
        self.nav_owned = True

    def recorded_nav2_health(self):
        return self.nav_owned

    def suspend_recorded_motion(self):
        self.events.append("revoke")
        self.replay_owned = self.nav_owned = False

    def activate_recorded_motion(self):
        self.events.append("resume_replay")
        assert not self.nav_owned
        self.replay_owned = True


class HandoffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ros = FakeROS()
        self.nav = FakeNav()
        self.api = RecordedLoopApi(
            app=FastAPI(), navigation=self.nav,
            map_api=SimpleNamespace(ros_control=self.ros),
            csrf_failure=lambda _: None,
            store_path=Path(self.temp.name) / "records.json")
        self.api._executor = SimpleNamespace(cancelled=False)
        self.api._executor.playback = Playback()
        self.api._executor.playback.start("WAIT")
        self.api._executor._emit = lambda: None

        async def authorize():
            return True

        async def stop():
            self.nav.events.append("stop_proven")
            return True

        async def pose():
            return {"x": 1.0, "y": 0.0, "yaw": 0.0}

        self.api._play_authorize = authorize
        self.api._play_stop = stop
        self.api._play_pose = pose

    async def test_auto_trace_skips_planner_and_returns_to_replay(self):
        path = [{"x": 0.0, "y": 0.0, "yaw": 0.0},
                {"x": 1.0, "y": 0.0, "yaw": 0.0}]
        self.assertTrue(await self.api._play_follow(path, "WAIT"))
        self.assertEqual(self.ros.events, ["validate", "follow"])
        self.assertEqual(self.nav.events, ["takeover", "revoke", "stop_proven", "resume_replay"])

    async def test_auto_trace_uses_reverse_capable_controller(self):
        def general(path, *, recorded_preflight=None):
            assert recorded_preflight is None or recorded_preflight()
            self.ros.events.append("general_follow")
            self.ros.state = "SUCCEEDED"
            return {"accepted": True}
        self.ros.follow_recorded_auto_path = general
        path = [{"x": 0.0, "y": 0.0, "yaw": 0.0},
                {"x": 1.0, "y": 0.0, "yaw": 0.0}]
        self.assertTrue(await self.api._play_follow(path, "WAIT"))
        self.assertEqual(self.ros.events, ["validate", "general_follow"])
        self.assertNotIn("compute", self.ros.events)

    async def test_obstacle_replan_plans_once_only(self):
        segment = {"poses": [{"x": 0.0, "y": 0.0, "yaw": 0.0},
                             {"x": 1.0, "y": 0.0, "yaw": 0.0}]}
        self.assertTrue(await self.api._play_replan(segment, segment["poses"]))
        self.assertEqual(self.ros.events, ["compute", "validate", "follow"])
        self.assertTrue(self.nav.replay_owned)

    async def test_wait_status_prevents_nav2_start_until_clear(self):
        self.ros.valid = False
        samples = {"n": 0}
        def delayed_valid(path):
            self.ros.events.append("validate")
            samples["n"] += 1
            return samples["n"] >= 3
        self.ros.loop_path_valid = delayed_valid
        labels = []
        self.api._executor.playback = Playback()
        self.api._executor.playback.start("WAIT")
        self.api._executor.playback.obstacle = lambda blocked: labels.append("WAITING" if blocked else "RUNNING")
        self.api._executor._emit = lambda: None
        path = [{"x": 0.0, "y": 0.0, "yaw": 0.0},
                {"x": 1.0, "y": 0.0, "yaw": 0.0}]
        self.assertTrue(await self.api._play_follow(path, "WAIT"))
        self.assertEqual(labels, ["WAITING", "RUNNING"])
        self.assertEqual(self.ros.events[-1], "follow")

    async def test_new_obstacle_during_follow_waits_and_resumes(self):
        self.api._executor.playback = Playback()
        self.api._executor.playback.start("WAIT")
        states = []
        self.api._executor._emit = lambda: states.append(
            self.api._executor.playback.viewer_state()["label"])
        validity = {"count": 0}
        def validate(path):
            self.ros.events.append("validate")
            validity["count"] += 1
            return validity["count"] != 2
        starts = {"count": 0}
        def start_follow(path, *, recorded_preflight=None):
            assert recorded_preflight is None or recorded_preflight()
            self.ros.events.append("follow")
            starts["count"] += 1
            self.ros.state = "NAVIGATING" if starts["count"] == 1 else "SUCCEEDED"
            return {"accepted": True}
        self.ros.loop_path_valid = validate
        self.ros.follow_loop_path = start_follow
        path = [{"x": 0.0, "y": 0.0, "yaw": 0.0},
                {"x": 1.0, "y": 0.0, "yaw": 0.0}]
        self.assertTrue(await self.api._play_follow(path, "WAIT"))
        self.assertEqual(starts["count"], 2)
        self.assertIn("cancel", self.ros.events)
        self.assertIn("대기 중", states)
        self.assertTrue(self.nav.replay_owned)

    async def test_new_obstacle_during_follow_replans(self):
        self.api._executor.playback = Playback()
        self.api._executor.playback.start("REPLAN")
        self.api._executor._emit = lambda: None
        validity = {"count": 0}
        def validate(path):
            self.ros.events.append("validate")
            validity["count"] += 1
            return validity["count"] != 2
        starts = {"count": 0}
        def start_follow(path, *, recorded_preflight=None):
            assert recorded_preflight is None or recorded_preflight()
            self.ros.events.append("follow")
            starts["count"] += 1
            self.ros.state = "NAVIGATING" if starts["count"] == 1 else "SUCCEEDED"
            return {"accepted": True}
        self.ros.loop_path_valid = validate
        self.ros.follow_loop_path = start_follow
        path = [{"x": 0.0, "y": 0.0, "yaw": 0.0},
                {"x": 1.0, "y": 0.0, "yaw": 0.0}]
        self.assertTrue(await self.api._play_follow(path, "REPLAN"))
        self.assertEqual(self.ros.events.count("compute"), 1)
        self.assertEqual(starts["count"], 2)
        self.assertTrue(self.nav.replay_owned)

    async def test_invalid_path_cannot_restart_motors(self):
        self.ros.valid = False
        with self.assertRaises(ReplayFailure):
            await self.api._play_follow([
                {"x": 0.0, "y": 0.0, "yaw": 0.0},
                {"x": 1.0, "y": 0.0, "yaw": 0.0}], "REPLAN")
        self.assertNotIn("resume_replay", self.nav.events)
        self.assertIn("stop_proven", self.nav.events)

    async def test_initial_obstacle_replans_before_start_and_aligns_with_general_controller(self):
        self.api._executor.playback = Playback()
        self.api._executor.playback.start("REPLAN")
        def valid(path):
            return "compute" in self.ros.events
        self.ros.loop_path_valid = valid
        def general(path, *, recorded_preflight=None):
            assert recorded_preflight()
            self.ros.events.append("general_follow")
            self.ros.state = "SUCCEEDED"
            return {"accepted": True}
        self.ros.follow_recorded_auto_path = general
        path = [{"x": 0., "y": 0., "yaw": 0.},
                {"x": 1., "y": 0., "yaw": 0.}]
        self.assertTrue(await self.api._play_follow(path, "REPLAN"))
        self.assertEqual(self.ros.events, ["compute", "general_follow"])
        self.assertEqual(self.api._executor.playback.status.value, "RUNNING")

    async def test_map_authorization_loss_during_nav2_stops_without_resuming(self):
        checks = 0
        async def authorize():
            nonlocal checks
            checks += 1
            return checks < 4
        self.api._play_authorize = authorize
        def follow(path, *, recorded_preflight=None):
            self.ros.events.append("follow")
            self.ros.state = "NAVIGATING"
            return {"accepted": True}
        self.ros.follow_loop_path = follow
        with self.assertRaisesRegex(ReplayFailure, "NAV2_TAKEOVER_INTERRUPTED"):
            await self.api._play_follow([
                {"x": 0., "y": 0., "yaw": 0.},
                {"x": 1., "y": 0., "yaw": 0.}], "WAIT")
        self.assertIn("cancel", self.ros.events)
        self.assertIn("stop_proven", self.nav.events)
        self.assertNotIn("resume_replay", self.nav.events)


if __name__ == "__main__":
    unittest.main()
