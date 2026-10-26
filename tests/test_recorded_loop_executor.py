"""Deterministic offline replay tests: callbacks mock ALL Pi/ROS hardware."""
import asyncio
import unittest

from server.recorded_loop_executor import RecordedMotorExecutor, ReplayFailure
from server.recorded_loop_runtime import ReplayStatus


def route():
    def segment(start, end):
        return {"source": "manual", "start": "A", "end": "B",
                "commands": [{"direction": "forward", "speed": .2, "duration": .1}],
                "poses": [{"x": float(start), "y": 0., "yaw": 0.},
                          {"x": float((start+end)/2), "y": 0., "yaw": 0.},
                          {"x": float(end), "y": 0., "yaw": 0.}]}
    return [segment(0, 1), segment(1, 0)]


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def build(self, *, obstacle=False, stop_ok=True):
        where = {"x": 0., "y": 0., "yaw": 0.}
        events = []
        blocked_calls = 0
        driver_calls = 0

        async def authorize():
            return True

        async def drive(command):
            nonlocal driver_calls
            driver_calls += 1
            # The test simulates motion, NEVER a real motor.
            where["x"] = 1. if driver_calls < 3 else 0.
            return True

        async def stop():
            events.append("stop")
            return stop_ok

        async def pose():
            return dict(where)

        async def blocked(command):
            nonlocal blocked_calls
            blocked_calls += 1
            return obstacle and blocked_calls == 1

        async def replan(segment, remaining):
            nonlocal driver_calls
            driver_calls = 2  # Replan completed A→B without replay motor commands.
            where["x"] = segment["poses"][-1]["x"]
            events.append("replan")
            return True

        engine = RecordedMotorExecutor(
            authorize=authorize, drive=drive, stop=stop, pose=pose,
            blocked=blocked, replan=replan, on_status=lambda v: events.append(v["label"]))
        engine.TICK_SEC = .06
        engine.CLEAR_CONFIRM_SEC = .02
        return engine, events, where

    async def test_two_leg_recorded_loop(self):
        engine, events, _ = await self.build()
        result = await engine.run(route(), "WAIT", max_laps=1)
        self.assertEqual(result["laps"], 1)
        self.assertIn("stop", events)

    async def test_wait_freezes_and_resumes(self):
        engine, events, _ = await self.build(obstacle=True)
        await engine.run(route(), "WAIT", max_laps=1)
        self.assertIn("대기 중", events)

    async def test_replan_rejoins_at_goal(self):
        engine, events, _ = await self.build(obstacle=True)
        await engine.run(route(), "REPLAN", max_laps=1)
        self.assertIn("replan", events)

    async def test_unacknowledged_stop_is_fatal(self):
        engine, events, _ = await self.build(stop_ok=False)
        with self.assertRaises(ReplayFailure):
            await engine.run(route(), "WAIT", max_laps=1)
        self.assertEqual(engine.playback.status, ReplayStatus.FAILED)


if __name__ == "__main__":
    unittest.main()
