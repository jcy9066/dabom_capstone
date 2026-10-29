"""Recorded FollowPath late-acceptance safety, ROS2 hardware not required."""
import unittest
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import patch

from server import navigation_ros_control as ros_module


class FakeHandle:
    accepted = True

    def __init__(self):
        self.cancel_calls = 0
        self.result = Future()

    def cancel_goal_async(self):
        self.cancel_calls += 1
        result = Future()
        result.set_result(SimpleNamespace(goals_canceling=[1]))
        return result

    def get_result_async(self):
        return self.result


class FakeClient:
    def __init__(self, action):
        self.sent = 0
        self.action = action
        self.response = Future()

    def wait_for_server(self, timeout_sec):
        return True

    def send_goal_async(self, goal):
        self.sent += 1
        self.action(goal)
        if not self.response.done():
            self.response.set_result(self.handle)
        return self.response


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
            return future.result()
        self.control._wait_future = wait_for
        self.samples = [{"x": 0.0, "y": 0.0, "yaw": 0.0},
                        {"x": 1.0, "y": 0.0, "yaw": 0.0}]
        self.patch = patch.object(
            ros_module, "FollowPath",
            SimpleNamespace(Goal=lambda: SimpleNamespace()))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def client(self, action):
        client = FakeClient(action)
        client.handle = self.handle
        self.control._follow_path_client = client
        return client

    def test_stopped_before_send_never_submits_nav2_goal(self):
        self.client(lambda goal: None)
        with self.assertRaises(ros_module.NavigationRosError) as error:
            self.control.follow_loop_path(
                self.samples, recorded_preflight=lambda: False)
        self.assertEqual(error.exception.error_code, "RECORDED_OWNER_LOST")
        self.assertEqual(self.control._follow_path_client.sent, 0)

    def test_stop_during_goal_acceptance_cancels_late_handle(self):
        owner = {"valid": True}
        self.client(lambda goal: owner.update(valid=False))
        with self.assertRaises(ros_module.NavigationRosError) as error:
            self.control.follow_loop_path(
                self.samples, recorded_preflight=lambda: owner["valid"])
        self.assertEqual(error.exception.error_code, "RECORDED_OWNER_LOST")
        self.assertEqual(self.handle.cancel_calls, 1)
        self.assertIsNone(self.control._navigate_goal_handle)
        self.assertEqual(self.control.navigation_status()["state"], "CANCELING")
        self.handle.result.set_result(SimpleNamespace(status=5))
        self.assertIsNone(self.control._recorded_follow_pending)

    def test_legacy_loop_calls_unchanged_without_guard(self):
        self.client(lambda goal: None)
        result = self.control.follow_loop_path(self.samples)
        self.assertTrue(result["accepted"])
        self.assertIs(self.control._navigate_goal_handle, self.handle)
        self.assertEqual(self.handle.cancel_calls, 0)

    def test_auto_recorded_path_respects_same_guard(self):
        self.client(lambda goal: None)
        with self.assertRaises(ros_module.NavigationRosError):
            self.control.follow_recorded_auto_path(
                self.samples, recorded_preflight=lambda: False)
        self.assertEqual(self.control._follow_path_client.sent, 0)

    def test_acceptance_after_timeout_is_canceled_and_fenced_until_terminal(self):
        client = self.client(lambda goal: None)
        submitted = Future()
        client.send_goal_async = lambda request: submitted
        def timeout(future, **kwargs):
            raise ros_module.NavigationRosError("FOLLOW_PATH_START_TIMEOUT", "timeout", 504)
        self.control._wait_future = timeout
        with self.assertRaises(ros_module.NavigationRosError):
            self.control.follow_loop_path(self.samples, recorded_preflight=lambda: True)
        self.assertIs(self.control._recorded_follow_pending, submitted)
        self.assertEqual(self.control.navigation_status()["state"], "CANCELING")
        submitted.set_result(self.handle)
        self.assertEqual(self.handle.cancel_calls, 1)
        with self.assertRaises(ros_module.NavigationRosError):
            self.control.follow_loop_path(self.samples)
        with self.assertRaises(ros_module.NavigationRosError):
            self.control.cancel_navigation()
        self.handle.result.set_result(SimpleNamespace(status=5))
        self.assertIsNone(self.control._recorded_follow_pending)

    def test_late_rejection_releases_fence_without_motor_action(self):
        submitted = Future()
        self.control._discard_recorded_follow(submitted)
        submitted.set_result(SimpleNamespace(accepted=False))
        self.assertIsNone(self.control._recorded_follow_pending)

    def test_uncertain_terminal_result_keeps_motion_fenced(self):
        submitted = Future()
        self.control._discard_recorded_follow(submitted)
        submitted.set_result(self.handle)
        self.handle.result.set_exception(RuntimeError("lost result"))
        self.assertIs(self.control._recorded_follow_pending, submitted)


if __name__ == "__main__":
    unittest.main()
