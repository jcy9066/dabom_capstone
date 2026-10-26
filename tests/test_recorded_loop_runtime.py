import unittest
from server.recorded_loop_runtime import Playback, ReplayStatus


class PlaybackTests(unittest.TestCase):
    def test_wait_freezes_time_and_shows_status(self):
        p = Playback()
        p.start("WAIT")
        commands = [{"direction": "forward", "speed": .2, "duration": .3}]
        self.assertEqual(p.step(.1, commands)["direction"], "forward")
        p.obstacle(True)
        self.assertEqual(p.viewer_state()["label"], "대기 중")
        self.assertIsNone(p.step(.4, commands))
        self.assertAlmostEqual(p.command_elapsed, .1)
        p.obstacle(False)
        self.assertIsNone(p.step(.2, commands))

    def test_policy_locked_and_replan_no_motion(self):
        p = Playback()
        p.start("REPLAN")
        with self.assertRaises(RuntimeError):
            p.start("WAIT")
        p.obstacle(True)
        self.assertEqual(p.status, ReplayStatus.REPLANNING)
        self.assertIsNone(p.step(.2, [{"duration": .3}]))

    def test_single_recorded_path_completes_one_lap(self):
        p = Playback()
        p.start("WAIT")
        p.finish_segment(1, True)
        self.assertEqual(p.lap, 1)
        self.assertEqual(p.segment_index, 0)
        self.assertEqual(p.status, ReplayStatus.RUNNING)

    def test_endpoint_proof_required(self):
        p = Playback()
        p.start("WAIT")
        p.finish_segment(2, False)
        self.assertEqual(p.status, ReplayStatus.FAILED)


if __name__ == "__main__":
    unittest.main()
