"""Recorded FollowPath late-acceptance safety, ROS2 hardware not required."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from server import navigation_ros_control as ros_module


class FakeFuture:
    def add_done_callback(self, callback):
        self.callback = callback


class FakeHandle:
    accepted = True

    def __init__(self):
        self.cancel_calls = 0
        self.result = FakeFuture()

    def cancel_goal_async(self):
        self.cancel_calls += 1
        return "cancel_response"

    def get_result_async(self):
        return self.result


class FakeClient:
    def __init__(self, action):
        self.sent = 0
        self.action = action

    def wait_for_server(self, timeout_sec):
        return True

    def send_goal_async(self, goal):
        self.sent += 1
        self.action(goal)
        return "goal_response"


class LateNav2GoalTests(unittest.TestCase):
    def setUp(self):
        self.control = ros_module.NavigationRosControl()
        self.control._started = True
        self.control._require_started = lambda: None
        self.control._path_stamped = lambda poses: dict(path=poses)
        self.handle = FakeHandle()
        self.responses = []
        def wait_for(future, **kwargs):
            self.responses.append(future)
            if future == "goal_response":
                return self.handle
            if future == "cancel_response":
                return SimpleNamespace(goals_canceling=[1])
            raise AssertionError(future)
        self.control._wait_future = wait_for
        self.samples = [{"x": 0.0, "y": 0.0, "yaw": 0.0},
                        {"x": 1.0, "y": 0.0, "yaw": 0.0}]
        self.patch = patch.object(
            ros_module, "FollowPath",
            SimpleNamespace(Goal=lambda: SimpleNamespace()))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_stopped_before_send_never_submits_nav2_goal(self):
        self.control._follow_path_client = FakeClient(lambda goal: None)
        with self.assertRaises(ros_module.NavigationRosError) as error:
            self.control.follow_loop_path(
                self.samples, recorded_preflight=lambda: False)
        self.assertEqual(error.exception.error_code, "RECORDED_OWNER_LOST")
        self.assertEqual(self.control._follow_path_client.sent, 0)

    def test_stop_during_goal_acceptance_cancels_late_handle(self):
        owner = {"valid": True}
        self.control._follow_path_client = FakeClient(
            lambda goal: owner.update(valid=False))
        with self.assertRaises(ros_module.NavigationRosError) as error:
            self.control.follow_loop_path(
                self.samples, recorded_preflight=lambda: owner["valid"])
        self.assertEqual(error.exception.error_code, "RECORDED_OWNER_LOST")
        self.assertEqual(self.handle.cancel_calls, 1)
        self.assertIn("cancel_response", self.responses)
        self.assertIsNone(self.control._navigate_goal_handle)

    def test_legacy_loop_calls_unchanged_without_guard(self):
        self.control._follow_path_client = FakeClient(lambda goal: None)
        result = self.control.follow_loop_path(self.samples)
        self.assertTrue(result["accepted"])
        self.assertIs(self.control._navigate_goal_handle, self.handle)
        self.assertEqual(self.handle.cancel_calls, 0)

    def test_auto_recorded_path_respects_same_guard(self):
        self.control._follow_path_client = FakeClient(lambda goal: None)
        with self.assertRaises(ros_module.NavigationRosError):
            self.control.follow_recorded_auto_path(
                self.samples, recorded_preflight=lambda: False)
        self.assertEqual(self.control._follow_path_client.sent, 0)


if __name__ == "__main__":
    unittest.main()
