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
            events.append("motor_sent")
            # The test simulates motion, NEVER a real motor.
            where["x"] = 1. if driver_calls <= 2 else 0.
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

    async def test_stop_before_task_runs_is_not_cleared(self):
        engine, events, _ = await self.build()
        engine.cancel()
        with self.assertRaisesRegex(ReplayFailure, "REPLAY_CANCELLED"):
            await engine.run(route(), "WAIT", max_laps=1)
        self.assertNotIn("motor_sent", events)

    async def test_first_stop_must_be_confirmed_before_motor(self):
        engine, events, _ = await self.build(stop_ok=False)
        with self.assertRaisesRegex(ReplayFailure, "STOP_NOT_CONFIRMED"):
            await engine.run(route(), "WAIT", max_laps=1)
        self.assertNotIn("motor_sent", events)

    async def test_one_shot_path_does_not_repeat(self):
        engine, events, _ = await self.build()
        result = await engine.run(route()[:1], "WAIT", max_laps=1)
        self.assertEqual(result["laps"], 1)
        self.assertEqual(engine.playback.lap, 1)
        self.assertEqual(events.count("motor_sent"), 2)

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

    async def test_heading_mismatch_blocks_motor_output(self):
        engine, events, where = await self.build()
        where["yaw"] = 3.141592653589793
        with self.assertRaisesRegex(ReplayFailure, "SEGMENT_START_MISMATCH"):
            await engine.run(route(), "WAIT", max_laps=1)
        self.assertNotIn("motor_sent", events)

    async def test_final_stop_failure_prevents_false_success(self):
        engine, events, _ = await self.build()
        stopped = 0

        async def stop_then_fail():
            nonlocal stopped
            stopped += 1
            return stopped < 5

        engine.stop = stop_then_fail
        with self.assertRaisesRegex(ReplayFailure, "STOP_NOT_CONFIRMED"):
            await engine.run(route(), "WAIT", max_laps=1)
        self.assertEqual(engine.playback.status, ReplayStatus.FAILED)

    async def test_operator_cancel_is_distinct_from_safety_failure(self):
        engine, events, _ = await self.build()
        engine.cancel()
        # Cancellation arriving while an active callback runs is checked
        # before issuing any further motor output.
        with self.assertRaisesRegex(ReplayFailure, "REPLAY_CANCELLED"):
            await engine._health()
        self.assertNotIn("motor_sent", events)

    async def test_unacknowledged_stop_is_fatal(self):
        engine, events, _ = await self.build(stop_ok=False)
        with self.assertRaises(ReplayFailure):
            await engine.run(route(), "WAIT", max_laps=1)
        self.assertEqual(engine.playback.status, ReplayStatus.FAILED)

    async def test_interlock_hold_does_not_consume_recorded_command(self):
        engine, events, where = await self.build()
        calls = 0
        elapsed_during_hold = []
        async def drive(command):
            nonlocal calls
            calls += 1
            if calls <= 3:
                elapsed_during_hold.append(engine.playback.command_elapsed)
                return None
            where["x"] = 1.
            return True
        engine.drive = drive
        result = await engine.run(route()[:1], "WAIT", max_laps=1)
        self.assertEqual(elapsed_during_hold, [0., 0., 0.])
        self.assertGreaterEqual(calls, 5)
        self.assertEqual(result["laps"], 1)

    async def test_slow_ack_never_skips_unsubmitted_commands(self):
        engine, events, where = await self.build()
        segment = route()[0]
        segment["commands"] = [
            {"direction": direction, "speed": .2, "duration": .01}
            for direction in ("forward", "rotate_left", "backward")]
        submitted = []
        async def drive(command):
            submitted.append(command["direction"])
            await asyncio.sleep(.04)
            where["x"] = 1.
            return True
        engine.drive = drive
        await engine.run([segment], "WAIT", max_laps=1)
        self.assertEqual(submitted, ["forward", "rotate_left", "backward"])

    async def test_obstacle_wait_starts_a_fresh_interlock_window(self):
        engine, events, where = await self.build()
        engine.DIRECTION_HANDOFF_TIMEOUT_SEC = .025
        engine.TICK_SEC = .01
        calls = 0
        waited = False
        async def drive(command):
            nonlocal calls
            calls += 1
            if calls <= 2:
                return None
            where["x"] = 1.
            return True
        async def blocked(command):
            return calls == 1 and not waited
        async def wait_clear(command):
            nonlocal waited
            # Physical STOP clears Pi's pending interlock during this WAIT.
            await asyncio.sleep(.04)
            waited = True
            engine.playback.obstacle(False)
        engine.drive = drive
        engine.blocked = blocked
        engine._wait_clear = wait_clear
        result = await engine.run(route()[:1], "WAIT", max_laps=1)
        self.assertEqual(result["laps"], 1)

    async def test_permanent_interlock_hold_fails_and_stops(self):
        engine, events, _ = await self.build()
        engine.DIRECTION_HANDOFF_TIMEOUT_SEC = .025
        engine.TICK_SEC = .01
        async def held(command):
            return None
        engine.drive = held
        with self.assertRaisesRegex(ReplayFailure, "DIRECTION_HANDOFF_TIMEOUT"):
            await engine.run(route()[:1], "WAIT", max_laps=1)
        self.assertIn("stop", events)

    async def test_slow_health_pose_and_scan_reads_count_existing_motor_time(self):
        for callback in ("authorize", "pose", "blocked"):
            with self.subTest(callback=callback):
                engine, events, where = await self.build()
                segment = route()[0]
                segment["commands"][0]["duration"] = .05
                engine.TICK_SEC = .02
                sent = []
                original = getattr(engine, callback)
                async def slow(*args):
                    if sent:
                        await asyncio.sleep(.06)
                    return await original(*args)
                setattr(engine, callback, slow)
                async def drive(command):
                    sent.append(command["direction"])
                    where["x"] = 1.
                    return True
                engine.drive = drive
                await engine.run([segment], "WAIT", max_laps=1)
                # The second preflight consumed the remaining applied motor
                # interval; it must not resend the already-complete command.
                self.assertEqual(sent, ["forward"])


if __name__ == "__main__":
    unittest.main()
