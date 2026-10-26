"""Nav2 takeover / Recorded Path callbacks: no real Pi or ROS required."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI

from server.recorded_loop_api import RecordedLoopApi
from server.recorded_loop_executor import ReplayFailure


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

    def follow_loop_path(self, path):
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

    async def test_obstacle_replan_plans_once_only(self):
        segment = {"poses": [{"x": 0.0, "y": 0.0, "yaw": 0.0},
                             {"x": 1.0, "y": 0.0, "yaw": 0.0}]}
        self.assertTrue(await self.api._play_replan(segment, segment["poses"]))
        self.assertEqual(self.ros.events, ["compute", "validate", "follow"])
        self.assertTrue(self.nav.replay_owned)

    async def test_invalid_path_cannot_restart_motors(self):
        self.ros.valid = False
        with self.assertRaises(ReplayFailure):
            await self.api._play_follow([
                {"x": 0.0, "y": 0.0, "yaw": 0.0},
                {"x": 1.0, "y": 0.0, "yaw": 0.0}], "WAIT")
        self.assertNotIn("resume_replay", self.nav.events)
        self.assertIn("stop_proven", self.nav.events)


if __name__ == "__main__":
    unittest.main()
