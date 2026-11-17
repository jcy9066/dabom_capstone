"""No real hardware, GPU, camera or Pi required."""
import asyncio
import unittest

from server.automatic_warning import AutomaticWarningDispatcher, verified_danger_label


def scene_danger():
    return {"danger": True, "scene": {"status": "ok", "fresh": True, "raw_danger": True, "danger": True}}


def person_danger():
    return {"danger": True, "detections": [{"danger": True, "confidence_level": "danger", "observation_stale": False, "inference_status": "ok", "label": "PUNCHING"}]}


class EvidenceTests(unittest.TestCase):
    def test_fresh_scene_is_valid(self):
        self.assertEqual(verified_danger_label(scene_danger()), "VIOLENCE")

    def test_stale_scene_cannot_trigger(self):
        s = scene_danger()
        s["scene"]["fresh"] = False
        self.assertIsNone(verified_danger_label(s))

    def test_person_danger_is_valid(self):
        self.assertEqual(verified_danger_label(person_danger()), "PUNCHING")

    def test_stale_person_cannot_trigger(self):
        s = person_danger()
        s["detections"][0]["observation_stale"] = True
        self.assertIsNone(verified_danger_label(s))

    def test_suspicious_person_cannot_trigger(self):
        s = person_danger()
        s["detections"][0]["confidence_level"] = "suspicious"
        self.assertIsNone(verified_danger_label(s))

    def test_pair_danger_is_valid(self):
        s = {"danger": True, "pair_actions": [{"label": "PUSHING", "is_danger": True, "observation_stale": False}]}
        self.assertEqual(verified_danger_label(s), "PUSHING")

    def test_visual_danger_without_verified_evidence_cannot_trigger(self):
        self.assertIsNone(verified_danger_label({"danger": True, "detections": [], "scene": {"danger": True}}))

    def test_model_error_cannot_trigger(self):
        s = scene_danger()
        s["model_error"] = "inference exception"
        self.assertIsNone(verified_danger_label(s))


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 100.0
        self.wall = 1000.0
        self.sent = []

        async def send(robot_id, command):
            self.sent.append((robot_id, command))
            return True

        self.sender = send

    def dispatcher(self, **kw):
        instance = AutomaticWarningDispatcher(
            robot_id="pi-01", send_command=self.sender,
            clock=lambda: self.now, wall_clock=lambda: self.wall,
            enabled=True, dry_run=False, **kw,
        )
        instance.bind_loop(asyncio.get_running_loop())
        self.addCleanup(instance.close)
        return instance

    def observe(self, dispatcher, danger=None, age=0.1, generation=1, stream_id=2):
        return dispatcher.observe_result(
            robot_id="pi-01", result=scene_danger() if danger is None else danger,
            captured_at=self.wall - age, generation=generation, stream_id=stream_id,
        )

    async def flush(self):
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def test_two_hits_send_one_existing_warning_command(self):
        d = self.dispatcher()
        self.assertFalse(self.observe(d))
        self.now += 0.2
        self.assertTrue(self.observe(d))
        await self.flush()
        self.assertEqual(self.sent, [("pi-01", {"type": "warning", "led_duration_ms": 10000})])

    async def test_cooldown_prevents_repeat_during_continuous_danger(self):
        d = self.dispatcher()
        self.observe(d)
        self.now += 0.2
        self.observe(d)
        await self.flush()
        self.now += 1
        self.assertFalse(self.observe(d))
        await self.flush()
        self.assertEqual(len(self.sent), 1)
        self.now += 31
        self.assertFalse(self.observe(d))  # the old observation is too old
        self.now += 0.2
        self.assertTrue(self.observe(d))
        await self.flush()
        self.assertEqual(len(self.sent), 2)

    async def test_x3d_cached_frames_between_fresh_hits_do_not_reset(self):
        # Regression: the X3D scene classifier computes a fresh prediction
        # roughly once per second, but cached frames are published between.
        d = self.dispatcher()
        self.assertFalse(self.observe(d))
        cached = {"danger": True, "scene": {
            "status": "ok", "available": True, "fresh": False,
            "raw_danger": True, "danger": True, "label": "VIOLENCE"
        }}
        for _ in range(3):
            self.now += 0.2
            self.assertFalse(self.observe(d, danger=cached))
        self.assertEqual(self.sent, [])
        self.now += 0.5
        self.assertTrue(self.observe(d))
        await self.flush()
        self.assertEqual(len(self.sent), 1)

    async def test_cached_danger_never_counts_as_second_hit(self):
        d = self.dispatcher()
        self.assertFalse(self.observe(d))
        cached = {"danger": True, "scene": {
            "status": "ok", "available": True, "fresh": False,
            "raw_danger": True, "danger": True
        }}
        for _ in range(5):
            self.now += 0.1
            self.assertFalse(self.observe(d, danger=cached))
        await self.flush()
        self.assertEqual(self.sent, [])

    async def test_fresh_normal_after_danger_resets_hit_sequence(self):
        d = self.dispatcher()
        self.assertFalse(self.observe(d))
        self.now += 0.2
        fresh_normal = {"danger": False, "scene": {
            "status": "ok", "available": True, "fresh": True,
            "raw_danger": False, "danger": False, "label": "NORMAL"
        }}
        self.assertFalse(self.observe(d, danger=fresh_normal))
        self.now += 0.2
        self.assertFalse(self.observe(d))
        await self.flush()
        self.assertEqual(self.sent, [])

    async def test_expired_cached_frame_resets_sequence(self):
        d = self.dispatcher()
        self.assertFalse(self.observe(d))
        self.now += 0.2
        cached = {"danger": True, "scene": {
            "status": "ok", "available": True, "fresh": False,
            "raw_danger": True, "danger": True
        }}
        self.assertFalse(self.observe(d, danger=cached, age=4.0))
        self.now += 0.2
        self.assertFalse(self.observe(d))
        await self.flush()
        self.assertEqual(self.sent, [])

    async def test_normal_frame_resets_consecutive_hits(self):
        d = self.dispatcher()
        self.observe(d)
        self.observe(d, danger={"danger": False})
        self.assertFalse(self.observe(d))
        self.assertEqual(self.sent, [])

    async def test_gap_resets_hits(self):
        d = self.dispatcher()
        self.observe(d)
        self.now += 3.1
        self.assertFalse(self.observe(d))
        await self.flush()
        self.assertFalse(self.sent)

    async def test_new_generation_resets_hits(self):
        d = self.dispatcher()
        self.observe(d, generation=1)
        self.assertFalse(self.observe(d, generation=2))
        self.assertEqual(self.sent, [])

    async def test_old_captured_frame_does_not_trigger(self):
        d = self.dispatcher()
        self.assertFalse(self.observe(d, age=4.0))
        self.assertFalse(self.observe(d, age=4.0))
        self.assertEqual(self.sent, [])

    async def test_wrong_robot_is_ignored(self):
        d = self.dispatcher(min_hits=1)
        self.assertFalse(d.observe_result(robot_id="pi-02", result=scene_danger(), captured_at=self.wall - 0.1))
        self.assertEqual(self.sent, [])

    async def test_dry_run_never_sends_hardware_command(self):
        d = AutomaticWarningDispatcher(robot_id="pi-01", send_command=self.sender, enabled=True, dry_run=True, min_hits=1, clock=lambda: self.now, wall_clock=lambda: self.wall)
        d.bind_loop(asyncio.get_running_loop())
        self.addCleanup(d.close)
        self.assertTrue(self.observe(d))
        await self.flush()
        self.assertEqual(self.sent, [])

    async def test_disabled_never_sends(self):
        d = AutomaticWarningDispatcher(robot_id="pi-01", send_command=self.sender, enabled=False, dry_run=False, min_hits=1, clock=lambda: self.now, wall_clock=lambda: self.wall)
        d.bind_loop(asyncio.get_running_loop())
        self.addCleanup(d.close)
        self.assertFalse(self.observe(d))
        self.assertEqual(self.sent, [])

    async def test_failed_ack_still_rate_limited(self):
        async def reject(_robot_id, _cmd):
            self.sent.append("rejected")
            return False
        self.sender = reject
        d = self.dispatcher(min_hits=1)
        self.assertTrue(self.observe(d))
        await self.flush()
        self.now += 1
        self.assertFalse(self.observe(d))
        self.assertEqual(len(self.sent), 1)

    async def test_cannot_observe_after_close(self):
        d = self.dispatcher(min_hits=1)
        d.close()
        self.assertFalse(self.observe(d))


if __name__ == "__main__":
    unittest.main()
